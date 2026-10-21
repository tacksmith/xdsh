"""Interactive and headless XDSH CLI over the runner-neutral agent seam.

The command intentionally keeps progress on stderr and the final Markdown
answer on stdout, so it works both as a human terminal and in scripts.  DSH is
constructed only through :class:`DshRunner`; no prompt text is sent to emulate
unsupported Host commands.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shlex
import signal
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal, TextIO, cast
from urllib.parse import urlsplit

from xdsh.core.context import ContextCompactionPolicy
from xdsh.core.errors import RunnerConfigurationError
from xdsh.core.events import (
    CompactionEvent,
    ErrorEvent,
    FinishEvent,
    GuardEvent,
    ReasoningEvent,
    RunnerEventT,
    ToolStartEvent,
    UsageEvent,
)
from xdsh.core.execution_policy import (
    DEFAULT_MAX_PARALLEL_TOOL_CALLS,
    validate_parallel_tool_limit,
)
from xdsh.core.fallback import FallbackRunner
from xdsh.core.heartbeat import HeartbeatError, HeartbeatStore
from xdsh.core.metrics import RunMetric, RunMetricsStore
from xdsh.core.models import (
    LiteLlmModelCatalog,
    ModelAvailability,
    ModelCatalog,
    ModelCatalogError,
    ModelDirectory,
)
from xdsh.core.plugins import (
    DshPluginManager,
    PluginControl,
    PluginError,
    PluginInstallPlan,
    PluginInstallResult,
    PluginInvocationState,
    derive_plugin_command,
)
from xdsh.core.presentation import (
    ConsoleProgressRenderer,
    ConsoleTurnRenderer,
    safe_error_code,
)
from xdsh.core.product_control import (
    ProductControlError,
    ProductSessionControl,
    TurnUsage,
    safe_exception_text,
    safe_outbound_text,
)
from xdsh.core.rate_limit import (
    RateLimitedRunner,
    RateLimitPolicy,
    rate_limit_policy_from_env,
)
from xdsh.core.run_guard import GuardedRunner, RunGuardPolicy
from xdsh.core.runner import (
    REASONING_EFFORTS,
    AgentRunner,
    ReasoningEffort,
    has_native_capability,
)
from xdsh.core.sessions import NamedSession, SessionRegistry, SessionRegistryError
from xdsh.core.settings import (
    ConfiguredModelCatalog,
    JsonSettingsStore,
    SettingsError,
    validate_settings_path,
)
from xdsh.core.skill_curator import SkillArchiveStore, SkillCurationError, SkillCurator
from xdsh.core.skill_evolution import SkillEvolutionError, SkillEvolutionStore
from xdsh.core.skills import (
    BuiltinSkill,
    builtin_skill_bundle_version,
    builtin_skill_catalog,
    read_builtin_skill,
)
from xdsh.ops.reconciler import ReconcilerConfig, ReconciliationReport, ServiceReconciler
from xdsh.runners.dsh import _DEFAULT_CORDIS, DshRunner, DshRunnerConfig
from xdsh.runners.runtime import classify_runtime_command
from xdsh.version import DSH_COMPATIBLE_VERSION, XDSH_VERSION, XDSH_VERSION_DISPLAY

LOGGER = logging.getLogger(__name__)

ProgressMode = Literal["auto", "always", "never"]
_MODEL_ID = r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}$"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_SUCCESS_REASONS = frozenset({"completed", "complete", "success", "succeeded"})
_DEFAULT_MODEL = "ark-code-latest"
_DEFAULT_ROOT = Path("~/.xdsh/cli")
_DEFAULT_SYSTEM_PROMPT = "You are a software engineering agent working through XDSH."


class CliConfigurationError(ValueError):
    """The CLI cannot start with the supplied environment or arguments."""


@dataclass(frozen=True, slots=True, kw_only=True)
class CliConfig:
    """Validated process settings for the local CLI transport."""

    state_root: Path
    workspace_root: Path
    dsh_home: Path
    litellm_base_url: str
    model: str
    runtime_command: tuple[str, ...]
    # Optional official DSH CLI argv used for explicit local plugin changes.
    plugin_command: tuple[str, ...] | None = None
    provider: str = "litellm"
    settings_path: Path | None = None
    system_prompt: str = _DEFAULT_SYSTEM_PROMPT
    cordis: Path = _DEFAULT_CORDIS
    task_timeout_seconds: float | None = None
    max_tokens: int | None = None
    model_max_tokens: int = 32_768
    max_answer_chars: int = 60_000
    max_thinking_chars: int = 20_000
    max_parallel_tool_calls: int = DEFAULT_MAX_PARALLEL_TOOL_CALLS
    context_window_tokens: int = 262_144
    auto_compact_enabled: bool = True
    native_compaction_enabled: bool = True
    auto_compact_warning_ratio: float = 0.20
    auto_compact_threshold_ratio: float = 0.50
    auto_compact_target_ratio: float = 0.20
    auto_compact_retain_ratio: float = 0.08
    auto_compact_threshold_tokens: int | None = 64_000
    auto_compact_min_turns: int = 1
    auto_compact_cooldown_turns: int = 1
    progress_mode: ProgressMode = "auto"
    progress_interval_seconds: float = 2.0
    show_thinking: bool = False
    reasoning_effort: ReasoningEffort = "off"
    run_guard_policy: RunGuardPolicy = field(default_factory=RunGuardPolicy)
    rate_limit_policy: RateLimitPolicy = field(default_factory=RateLimitPolicy)
    max_sessions: int = 20
    api_key_env: str = "XDSH_LITELLM_API_KEY"

    def __post_init__(self) -> None:
        state_root = _safe_root(self.state_root, "state root")
        workspace_root = _safe_root(self.workspace_root, "workspace root")
        dsh_home = _safe_root(self.dsh_home, "DSH home")
        if state_root == workspace_root or state_root == dsh_home or workspace_root == dsh_home:
            raise CliConfigurationError("state, workspace, and DSH roots must be separate")
        if not self.model.strip() or not _valid_model(self.model):
            raise CliConfigurationError("model ID is invalid")
        if not self.provider.strip() or any(ch.isspace() for ch in self.provider):
            raise CliConfigurationError("provider ID is invalid")
        if not self.runtime_command or any(not item for item in self.runtime_command):
            raise CliConfigurationError("an explicit non-empty DSH runtime command is required")
        if self.plugin_command is not None and (
            not self.plugin_command or any(not item for item in self.plugin_command)
        ):
            raise CliConfigurationError("plugin command must use non-empty argv")
        _validate_loopback_url(self.litellm_base_url)
        if not self.api_key_env or any(ch.isspace() for ch in self.api_key_env):
            raise CliConfigurationError("API-key environment variable name is invalid")
        if not self.cordis.expanduser().resolve().is_file():
            raise CliConfigurationError(f"Cordis config not found: {self.cordis}")
        if self.task_timeout_seconds is not None and self.task_timeout_seconds <= 0:
            raise CliConfigurationError("task timeout must be positive when set")
        if self.max_tokens is not None and self.max_tokens <= 0:
            raise CliConfigurationError("max tokens must be positive when set")
        if self.model_max_tokens <= 0:
            raise CliConfigurationError("model max tokens must be positive")
        if self.context_window_tokens <= 0:
            raise CliConfigurationError("context window must be positive")
        try:
            ContextCompactionPolicy(
                enabled=self.auto_compact_enabled,
                warning_ratio=self.auto_compact_warning_ratio,
                threshold_ratio=self.auto_compact_threshold_ratio,
                target_ratio=self.auto_compact_target_ratio,
                threshold_tokens=self.auto_compact_threshold_tokens,
                min_turns=self.auto_compact_min_turns,
                cooldown_turns=self.auto_compact_cooldown_turns,
            )
        except ValueError as error:
            raise CliConfigurationError(str(error)) from error
        if not 0.05 <= self.auto_compact_retain_ratio < self.auto_compact_threshold_ratio:
            raise CliConfigurationError(
                "XDSH_AUTO_COMPACT_RETAIN_RATIO must be at least 0.05 and below the threshold"
            )
        if not 1_000 <= self.max_answer_chars <= 200_000:
            raise CliConfigurationError("max answer characters must be between 1000 and 200000")
        if not 1_000 <= self.max_thinking_chars <= 100_000:
            raise CliConfigurationError("max thinking characters must be between 1000 and 100000")
        try:
            validate_parallel_tool_limit(self.max_parallel_tool_calls)
        except ValueError as error:
            raise CliConfigurationError(str(error)) from error
        if self.progress_mode not in {"auto", "always", "never"}:
            raise CliConfigurationError("progress mode must be auto, always, or never")
        if not 0.2 <= self.progress_interval_seconds <= 30.0:
            raise CliConfigurationError("progress interval must be between 0.2 and 30 seconds")
        if self.reasoning_effort not in REASONING_EFFORTS:
            raise CliConfigurationError("reasoning effort is invalid")
        if not 1 <= self.max_sessions <= 100:
            raise CliConfigurationError("max sessions must be between 1 and 100")
        if self.settings_path is not None:
            try:
                validate_settings_path(self.settings_path)
            except SettingsError as error:
                raise CliConfigurationError(str(error)) from error

    @property
    def effective_guard_policy(self) -> RunGuardPolicy:
        if self.task_timeout_seconds is None:
            return self.run_guard_policy
        return replace(
            self.run_guard_policy,
            max_runtime_seconds=(
                self.task_timeout_seconds
                if self.run_guard_policy.max_runtime_seconds is None
                else min(
                    self.task_timeout_seconds,
                    self.run_guard_policy.max_runtime_seconds,
                )
            ),
        )

    @property
    def context_compaction_policy(self) -> ContextCompactionPolicy:
        return ContextCompactionPolicy(
            enabled=self.auto_compact_enabled,
            warning_ratio=self.auto_compact_warning_ratio,
            threshold_ratio=self.auto_compact_threshold_ratio,
            target_ratio=self.auto_compact_target_ratio,
            threshold_tokens=self.auto_compact_threshold_tokens,
            min_turns=self.auto_compact_min_turns,
            cooldown_turns=self.auto_compact_cooldown_turns,
        )

    @classmethod
    def from_env(
        cls,
        environment: Mapping[str, str] | None = None,
        *,
        state_root: Path | None = None,
        workspace_root: Path | None = None,
        dsh_home: Path | None = None,
        model: str | None = None,
        provider: str | None = None,
        runtime_command: tuple[str, ...] | None = None,
        plugin_command: tuple[str, ...] | None = None,
        progress_mode: ProgressMode | None = None,
        progress_interval_seconds: float | None = None,
        reasoning_effort: ReasoningEffort | None = None,
        show_thinking: bool | None = None,
        context_window_tokens: int | None = None,
    ) -> CliConfig:
        """Load only XDSH-owned environment variables and explicit overrides."""

        env = os.environ if environment is None else environment
        base_url = _required(env, "XDSH_LITELLM_BASE_URL")
        _validate_loopback_url(base_url)
        _required(env, "XDSH_LITELLM_API_KEY")
        root = Path(env.get("XDSH_CLI_ROOT", str(_DEFAULT_ROOT)))
        selected_state = state_root or Path(env.get("XDSH_CLI_STATE_ROOT", str(root / "state")))
        selected_workspace = workspace_root or Path(
            env.get("XDSH_CLI_WORKSPACE_ROOT", str(root / "workspaces"))
        )
        selected_home = dsh_home or Path(
            env.get("XDSH_CLI_DSH_HOME", str(root / "dsh-home"))
        )
        raw_runtime = env.get("XDSH_DSH_RUNTIME_COMMAND", "")
        selected_runtime = runtime_command or _parse_runtime(raw_runtime)
        plugin_command_text = env.get("XDSH_DSH_PLUGIN_COMMAND", "").strip()
        try:
            parsed_plugin_command = (
                tuple(shlex.split(plugin_command_text)) if plugin_command_text else None
            )
        except ValueError as error:
            raise CliConfigurationError(
                "XDSH_DSH_PLUGIN_COMMAND has invalid quoting"
            ) from error
        if plugin_command_text and not parsed_plugin_command:
            raise CliConfigurationError("XDSH_DSH_PLUGIN_COMMAND must not be empty")
        selected_plugin_command = (
            plugin_command if plugin_command is not None else parsed_plugin_command
        )
        raw_model = model if model is not None else env.get("XDSH_LITELLM_MODEL", _DEFAULT_MODEL)
        raw_provider = provider if provider is not None else env.get(
            "XDSH_DSH_PROVIDER", "litellm"
        )
        selected_model = raw_model.strip()
        selected_provider = raw_provider.strip()
        timeout_text = env.get("XDSH_CLI_TASK_TIMEOUT_SECONDS", "").strip()
        task_timeout = float(timeout_text) if timeout_text else None
        selected_progress = progress_mode or cast(
            ProgressMode,
            env.get("XDSH_CLI_PROGRESS", "auto"),
        )
        selected_interval = (
            progress_interval_seconds
            if progress_interval_seconds is not None
            else float(env.get("XDSH_CLI_PROGRESS_INTERVAL_SECONDS", "2"))
        )
        selected_reasoning = reasoning_effort or cast(
            ReasoningEffort,
            env.get("XDSH_REASONING_EFFORT", "off"),
        )
        selected_thinking = (
            show_thinking
            if show_thinking is not None
            else _parse_bool(env.get("XDSH_CLI_SHOW_THINKING", "0"), "XDSH_CLI_SHOW_THINKING")
        )
        try:
            selected_context_window = (
                context_window_tokens
                if context_window_tokens is not None
                else int(env.get("XDSH_CONTEXT_WINDOW_TOKENS", "262144"))
            )
            selected_auto_enabled = _parse_bool(
                env.get("XDSH_AUTO_COMPACT_ENABLED", "1"),
                "XDSH_AUTO_COMPACT_ENABLED",
            )
            selected_native_enabled = _parse_bool(
                env.get("XDSH_NATIVE_COMPACTION_ENABLED", "1"),
                "XDSH_NATIVE_COMPACTION_ENABLED",
            )
            selected_auto_warning = float(
                env.get("XDSH_AUTO_COMPACT_WARNING_RATIO", "0.20")
            )
            selected_auto_threshold = float(
                env.get("XDSH_AUTO_COMPACT_THRESHOLD_RATIO", "0.50")
            )
            selected_auto_target = float(env.get("XDSH_AUTO_COMPACT_TARGET_RATIO", "0.20"))
            selected_auto_retain = float(env.get("XDSH_AUTO_COMPACT_RETAIN_RATIO", "0.08"))
            selected_auto_threshold_tokens = _optional_int(
                env.get("XDSH_AUTO_COMPACT_THRESHOLD_TOKENS"),
                "XDSH_AUTO_COMPACT_THRESHOLD_TOKENS",
                default=64_000,
            )
            selected_auto_min_turns = int(env.get("XDSH_AUTO_COMPACT_MIN_TURNS", "1"))
            selected_auto_cooldown = int(env.get("XDSH_AUTO_COMPACT_COOLDOWN_TURNS", "1"))
            max_tokens = _optional_int(env.get("XDSH_MAX_TOKENS"), "XDSH_MAX_TOKENS")
            model_max_tokens = int(env.get("XDSH_MODEL_MAX_TOKENS", "32768"))
            max_answer = int(env.get("XDSH_CLI_MAX_ANSWER_CHARS", "60000"))
            max_thinking = int(env.get("XDSH_CLI_MAX_THINKING_CHARS", "20000"))
            max_parallel_tool_calls = int(
                env.get(
                    "XDSH_MAX_PARALLEL_TOOL_CALLS",
                    str(DEFAULT_MAX_PARALLEL_TOOL_CALLS),
                )
            )
            max_sessions = int(env.get("XDSH_CLI_MAX_SESSIONS", "20"))
        except ValueError as error:
            raise CliConfigurationError(f"invalid numeric CLI setting: {error}") from error
        return cls(
            state_root=selected_state,
            workspace_root=selected_workspace,
            dsh_home=selected_home,
            litellm_base_url=base_url,
            model=selected_model,
            runtime_command=selected_runtime,
            plugin_command=selected_plugin_command,
            provider=selected_provider,
            settings_path=(
                Path(env["XDSH_SETTINGS_PATH"])
                if env.get("XDSH_SETTINGS_PATH", "").strip()
                else None
            ),
            system_prompt=env.get("XDSH_SYSTEM_PROMPT", _DEFAULT_SYSTEM_PROMPT),
            cordis=(
                Path(env["XDSH_DSH_CORDIS"])
                if env.get("XDSH_DSH_CORDIS")
                else _DEFAULT_CORDIS
            ),
            task_timeout_seconds=task_timeout,
            max_tokens=max_tokens,
            model_max_tokens=model_max_tokens,
            max_answer_chars=max_answer,
            max_thinking_chars=max_thinking,
            max_parallel_tool_calls=max_parallel_tool_calls,
            context_window_tokens=selected_context_window,
            auto_compact_enabled=selected_auto_enabled,
            native_compaction_enabled=selected_native_enabled,
            auto_compact_warning_ratio=selected_auto_warning,
            auto_compact_threshold_ratio=selected_auto_threshold,
            auto_compact_target_ratio=selected_auto_target,
            auto_compact_retain_ratio=selected_auto_retain,
            auto_compact_threshold_tokens=selected_auto_threshold_tokens,
            auto_compact_min_turns=selected_auto_min_turns,
            auto_compact_cooldown_turns=selected_auto_cooldown,
            progress_mode=selected_progress,
            progress_interval_seconds=selected_interval,
            show_thinking=selected_thinking,
            reasoning_effort=selected_reasoning,
            run_guard_policy=_guard_policy_from_env(env),
            rate_limit_policy=_rate_limit_policy_from_env(env),
            max_sessions=max_sessions,
            api_key_env="XDSH_LITELLM_API_KEY",
        )


@dataclass(frozen=True, slots=True)
class CliTurnResult:
    """Bounded result returned by one CLI turn."""

    session: NamedSession
    events: tuple[RunnerEventT, ...]
    answer: str
    thinking: str
    finish: FinishEvent
    succeeded: bool
    terminal_notice: str | None
    elapsed_seconds: int


class InteractiveCli:
    """Interactive shell that delegates all agent work to ``AgentRunner``."""

    def __init__(
        self,
        config: CliConfig,
        *,
        runner: AgentRunner | None = None,
        sessions: SessionRegistry | None = None,
        controls: ProductSessionControl | None = None,
        models: ModelCatalog | None = None,
        plugin_control: PluginControl | None = None,
        skill_evolution: SkillEvolutionStore | None = None,
        skill_archive: SkillArchiveStore | None = None,
        skill_curator: SkillCurator | None = None,
        metrics: RunMetricsStore | None = None,
        input_stream: TextIO | None = None,
        output: TextIO | None = None,
        progress_output: TextIO | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.input_stream = input_stream or sys.stdin
        self.output = output or sys.stdout
        self.progress_output = progress_output or sys.stderr
        self._clock = clock
        self._runner = runner or self._build_runner(config)
        live_models: ModelCatalog = LiteLlmModelCatalog(
            config.litellm_base_url,
            api_key_env=config.api_key_env,
            configured_model=config.model,
            include_configured_aliases=True,
        )
        self._models = models or (
            ConfiguredModelCatalog(
                live_models,
                JsonSettingsStore(config.settings_path),
                include_unlisted_models=True,
            )
            if config.settings_path is not None
            else live_models
        )
        self._sessions = sessions or SessionRegistry(
            config.state_root,
            default_model=config.model,
            namespace="cli",
            max_sessions=config.max_sessions,
        )
        self._controls = controls or ProductSessionControl(config.state_root)
        # CLI is a single local owner.  A negative namespace cannot collide
        # with Telegram chat IDs and the workspace binding is rechecked on
        # every mutation.
        self._local_owner_id = -1
        self._skill_evolution = skill_evolution or SkillEvolutionStore(config.state_root)
        self._skill_archive = skill_archive or SkillArchiveStore(config.state_root)
        self._skill_curator = skill_curator or SkillCurator()
        self._metrics = metrics or RunMetricsStore(config.state_root / "metrics.sqlite3")
        self._plugin_control = plugin_control
        if self._plugin_control is None:
            try:
                runtime_spec = classify_runtime_command(config.runtime_command)
                self._plugin_control = PluginControl(
                    DshPluginManager(
                        config.dsh_home,
                        dsh_command=(
                            config.plugin_command
                            or derive_plugin_command(config.runtime_command)
                        ),
                        default_profile=runtime_spec.profile or "xdsh",
                    )
                )
            except PluginError as error:
                LOGGER.warning(
                    "CLI DSH plugin control disabled: %s",
                    safe_exception_text(error, max_chars=400),
                )
        self._heartbeat = HeartbeatStore(config.state_root, "cli")
        self._active_task: asyncio.Task[CliTurnResult] | None = None
        self._active_session_id: str | None = None
        self._cancel_requested: set[str] = set()
        self._pending_context_resets: set[str] = set()
        self._branch_initialized: set[str] = set()
        self._closed = False
        self._stop_requested = asyncio.Event()
        self._progress_is_tty = bool(getattr(self.progress_output, "isatty", lambda: False)())
        self._last_progress_at = 0.0

    @property
    def sessions(self) -> SessionRegistry:
        return self._sessions

    @property
    def runner(self) -> AgentRunner:
        return self._runner

    @property
    def active(self) -> bool:
        return self._active_task is not None and not self._active_task.done()

    async def run_and_display(self, prompt: str) -> CliTurnResult:
        """Run one task and write progress/answer to their respective streams."""

        if self.active:
            raise CliError("a task is already running; use /cancel first")
        self._active_session_id = self._sessions.current().runner_session_id
        task = asyncio.create_task(self.run_turn(prompt), name="xdsh-cli-turn")
        self._active_task = task
        try:
            result = await task
        finally:
            if self._active_task is task:
                self._active_task = None
                self._active_session_id = None
        self._display_result(result)
        return result

    async def run_turn(self, prompt: str) -> CliTurnResult:
        """Execute a single turn and retain bounded partial output on failure."""

        if not prompt.strip():
            raise CliError("task cannot be empty")
        self._touch_heartbeat()
        session = self._sessions.current()
        self._active_session_id = session.runner_session_id
        workspace = self._workspace_for(session)
        if (
            session.branch_source_session_id is not None
            and session.runner_session_id not in self._branch_initialized
        ):
            self._controls.branch_session(
                session.branch_source_session_id,
                session.runner_session_id,
            )
            self._branch_initialized.add(session.runner_session_id)
        if not await self._maybe_auto_compact(session.runner_session_id, block_on_failure=True):
            raise CliError("automatic context compaction could not advance the DSH generation")
        try:
            prepared = self._controls.prepare_task(
                session.runner_session_id,
                prompt,
                workspace=workspace,
            )
        except ProductControlError as error:
            raise CliError(str(error)) from error

        renderer = ConsoleTurnRenderer(
            max_answer_chars=self.config.max_answer_chars,
            show_reasoning=session.show_thinking,
            max_reasoning_chars=self.config.max_thinking_chars,
        )
        progress = ConsoleProgressRenderer(
            native_compaction_enabled=has_native_capability(
                self._runner, "automatic_compaction"
            )
        )
        events: list[RunnerEventT] = []
        started = self._clock()
        heartbeat_stop = asyncio.Event()
        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(heartbeat_stop),
            name="xdsh-cli-heartbeat",
        )

        async def consume() -> None:
            stream = self._runner.run(
                prepared.task,
                session_id=session.runner_session_id,
                cwd=workspace,
                model=session.model,
                reasoning_effort=session.reasoning_effort,
                fallback=session.fallback_model,
                system_prompt=self.config.system_prompt,
                resume_context=prepared.resume_context,
            )
            async for event in stream:
                events.append(event)
                renderer.accept(event)
                progress.accept(event)
                self._maybe_progress(progress, started, force=_force_progress(event))

        try:
            try:
                if self.config.task_timeout_seconds is None:
                    await consume()
                else:
                    await asyncio.wait_for(consume(), timeout=self.config.task_timeout_seconds)
            except asyncio.CancelledError:
                await self._cancel_session(session.runner_session_id)
                _append_synthetic_event(
                    events,
                    renderer,
                    progress,
                    FinishEvent(
                        session_id=session.runner_session_id,
                        reason="cancelled",
                        cancelled=True,
                    ),
                )
            except asyncio.TimeoutError:
                await self._cancel_session(session.runner_session_id)
                _append_synthetic_event(
                    events,
                    renderer,
                    progress,
                    ErrorEvent(
                        session_id=session.runner_session_id,
                        message="CLI task timeout",
                        code="CLI_TASK_TIMEOUT",
                    ),
                )
                _append_synthetic_event(
                    events,
                    renderer,
                    progress,
                    FinishEvent(session_id=session.runner_session_id, reason="error"),
                )
            except Exception as error:
                LOGGER.warning("XDSH CLI runner failed with %s", type(error).__name__)
                _append_synthetic_event(
                    events,
                    renderer,
                    progress,
                    ErrorEvent(
                        session_id=session.runner_session_id,
                        message="CLI runner failure",
                        code=type(error).__name__,
                    ),
                )
                _append_synthetic_event(
                    events,
                    renderer,
                    progress,
                    FinishEvent(session_id=session.runner_session_id, reason="error"),
                )
        finally:
            heartbeat_stop.set()
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)

        finish = next(
            (event for event in reversed(events) if isinstance(event, FinishEvent)),
            None,
        )
        if (
            finish is not None
            and session.runner_session_id in self._cancel_requested
            and not finish.cancelled
        ):
            normalized_finish = replace(finish, reason="cancelled", cancelled=True)
            events[events.index(finish)] = normalized_finish
            renderer.finish = normalized_finish
            progress.accept(normalized_finish)
            finish = normalized_finish
        if finish is None:
            if session.runner_session_id in self._cancel_requested:
                finish = FinishEvent(
                    session_id=session.runner_session_id,
                    reason="cancelled",
                    cancelled=True,
                )
            else:
                _append_synthetic_event(
                    events,
                    renderer,
                    progress,
                    ErrorEvent(
                        session_id=session.runner_session_id,
                        message="runner ended without finish",
                        code="MISSING_FINISH",
                    ),
                )
                finish = FinishEvent(session_id=session.runner_session_id, reason="error")
            _append_synthetic_event(events, renderer, progress, finish)

        elapsed = max(0, int(self._clock() - started))
        self._maybe_progress(progress, started, force=True, final=True)
        self._record_turn(
            session,
            prompt,
            renderer,
            events,
            started_at=started,
            selected_skills=prepared.selected_skills,
        )
        if renderer.succeeded:
            await self._maybe_auto_compact(session.runner_session_id)
        result = CliTurnResult(
            session=session,
            events=tuple(events),
            answer=renderer.answer,
            thinking=renderer.thinking,
            finish=finish,
            succeeded=renderer.succeeded,
            terminal_notice=renderer.terminal_notice(),
            elapsed_seconds=elapsed,
        )
        self._cancel_requested.discard(session.runner_session_id)
        return result

    async def cancel_active(self) -> bool:
        """Cancel only the current session; no fallback is attempted."""

        if not self.active or self._active_session_id is None:
            return False
        session_id = self._active_session_id
        self._cancel_requested.add(session_id)
        cancelled = await self._cancel_session(session_id)
        if not cancelled:
            self._cancel_requested.discard(session_id)
        return cancelled

    async def interactive(self) -> int:
        """Run the readline loop; prompts can be cancelled while they run."""

        self._touch_heartbeat()
        self._install_signal_handlers()
        self._write_info("XDSH CLI · 使用 /help 查看命令。")
        input_task: asyncio.Task[str] | None = None
        active_task: asyncio.Task[CliTurnResult] | None = None
        try:
            while not self._stop_requested.is_set():
                if input_task is None:
                    self._write_prompt()
                    input_task = asyncio.create_task(
                        asyncio.to_thread(self._readline),
                        name="xdsh-cli-input",
                    )
                wait_set: set[asyncio.Task[object]] = {cast(asyncio.Task[object], input_task)}
                if active_task is not None:
                    wait_set.add(cast(asyncio.Task[object], active_task))
                done, _ = await asyncio.wait(wait_set, return_when=asyncio.FIRST_COMPLETED)
                if active_task is not None and cast(asyncio.Task[object], active_task) in done:
                    try:
                        result = active_task.result()
                    except Exception as error:
                        self._write_info(f"❌ 任務執行失敗 ({type(error).__name__})")
                    else:
                        self._display_result(result)
                    active_task = None
                    self._active_task = None
                    self._active_session_id = None
                    continue
                if cast(asyncio.Task[object], input_task) not in done:
                    continue
                try:
                    line = input_task.result()
                except (EOFError, KeyboardInterrupt):
                    line = ""
                    self._stop_requested.set()
                input_task = None
                stripped = line.strip()
                if not stripped:
                    if self._stop_requested.is_set():
                        break
                    continue
                if stripped.startswith("/"):
                    if active_task is not None:
                        parsed = _parse_command(stripped)
                        if parsed is not None and parsed[0] == "cancel":
                            if await self.cancel_active():
                                self._write_info("🛑 正在取消目前任務…")
                            else:
                                self._write_info("目前沒有可取消的任務。")
                        else:
                            self._write_info("目前任務仍在執行; 先使用 /cancel。")
                        continue
                    try:
                        should_continue = await self._handle_command(stripped)
                    except (
                        CliError,
                        ProductControlError,
                        SessionRegistryError,
                        OSError,
                        ValueError,
                    ) as error:
                        self._write_info(
                            f"⚠️ 命令未執行 ({type(error).__name__}): {error}"
                        )
                        should_continue = True
                    if not should_continue:
                        break
                else:
                    if active_task is not None:
                        self._write_info("目前任務仍在執行; 先使用 /cancel。")
                    else:
                        active_task = asyncio.create_task(
                            self.run_turn(stripped),
                            name="xdsh-cli-turn",
                        )
                        self._active_task = active_task
            if active_task is not None and not active_task.done():
                await self.cancel_active()
                final_result: CliTurnResult | None = None
                try:
                    final_result = await active_task
                except Exception:
                    pass
                if final_result is not None:
                    self._display_result(final_result)
        finally:
            if input_task is not None and not input_task.done():
                input_task.cancel()
                await asyncio.gather(input_task, return_exceptions=True)
        return 0

    async def handle_command(self, line: str) -> bool:
        """Public command seam useful to adapters and tests."""

        if self.active:
            raise CliError("commands other than /cancel are unavailable while a task runs")
        return await self._handle_command(line)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._runner.close()

    async def _handle_command(self, line: str) -> bool:
        parsed = _parse_command(line)
        if parsed is None:
            self._write_info("命令格式無效; 使用 /help。")
            return True
        name, arguments = parsed
        tokens = arguments
        if name in {"quit", "exit"}:
            return False
        if name in {"help", "?"}:
            self._write_info(_HELP_TEXT)
            return True
        if name in {"status", "info"}:
            self._write_info(self._status_text())
            return True
        if name == "doctor":
            await self._doctor_command()
            return True
        if name == "session":
            await self._command_session(tokens)
            return True
        if name == "model":
            await self._command_model(tokens)
            return True
        if name == "provider":
            self._command_provider(tokens)
            return True
        if name == "reasoning":
            self._command_reasoning(tokens)
            return True
        if name == "thinking":
            self._command_thinking(tokens)
            return True
        if name == "fallback":
            self._command_fallback(tokens)
            return True
        if name == "cancel":
            self._write_info("目前沒有正在執行的任務。")
            return True
        if name == "reset":
            await self._reset_current()
            return True
        if name == "compact":
            self._compact_current()
            return True
        if name == "goal":
            self._goal_command(tokens)
            return True
        if name == "plan":
            self._plan_command(tokens)
            return True
        if name in {"cost", "context"}:
            self._write_info(self._status_text(detail=name))
            return True
        if name == "skills":
            self._skills_command(tokens)
            return True
        if name == "plugins":
            await self._plugins_command(tokens)
            return True
        if name == "export":
            self._export_current()
            return True
        self._write_info(f"未知命令 /{name}; 使用 /help。")
        return True

    async def _command_session(self, arguments: tuple[str, ...]) -> None:
        if not arguments or arguments[0] in {"list", "ls"}:
            self._write_info(self._session_list_text())
            return
        action = arguments[0]
        if action in {"new", "create"}:
            name = " ".join(arguments[1:]) or None
            session = self._sessions.create(name)
            self._write_info(f"✅ 已建立並切換會話 {session.name} ({session.key})。")
            return
        if action == "branch":
            name = " ".join(arguments[1:]) or None
            session = self._sessions.branch(name)
            self._write_info(f"🌿 已建立分支會話 {session.name} ({session.key})。")
            return
        if action in {"switch", "use"} and len(arguments) >= 2:
            session = self._sessions.switch(arguments[1])
            self._write_info(f"🔀 已切換到 {session.name} ({session.key})。")
            return
        if action == "rename" and len(arguments) >= 3:
            session = self._sessions.rename(arguments[1], " ".join(arguments[2:]))
            self._write_info(f"✏️ 已重新命名為 {session.name}。")
            return
        if action in {"delete", "rm"} and len(arguments) == 2:
            deleted = self._sessions.delete(arguments[1])
            await self._runner.reset(deleted.deleted.runner_session_id)
            self._write_info(f"🗑️ 已移除 {deleted.deleted.name}; 目前為 {deleted.current.name}。")
            return
        if action == "reset":
            await self._reset_current()
            return
        raise CliError("用法: /session list|new|branch|switch|rename|delete|reset …")

    async def _command_model(self, arguments: tuple[str, ...]) -> None:
        if not arguments:
            session = self._sessions.current()
            self._write_info(f"模型: {session.model}\nProvider: {self.config.provider}")
            return
        try:
            directory = await self._models.snapshot()
        except (ModelCatalogError, ValueError) as error:
            # A gateway outage must not erase the current session.  Keep
            # explicit model typing available for offline/test adapters, but
            # label the route unverified so the next real turn remains the
            # authority on executability.
            if arguments[0] in {"list", "ls"}:
                self._write_info(
                    f"模型目錄暫不可用 ({type(error).__name__}); 保留目前模型。"
                )
                return
            selected = self._sessions.select_model(arguments[0])
            self._write_info(
                f"✅ 本會話模型已設為 {selected.model}。"
                "模型目錄暫不可用, route 尚未驗證。"
            )
            return
        if arguments[0] in {"list", "ls"}:
            self._write_info(_model_directory_text(directory, self._sessions.current().model))
            return
        model = arguments[0]
        if model not in directory.models:
            self._write_info("模型不在目前目錄; 請用 /model list 或設定 settings.json。")
            return
        health = directory.health_for(model)
        if health.availability is ModelAvailability.UNAVAILABLE:
            self._write_info(
                f"⚠️ {model} 的最小執行探測失敗 ({health.detail or 'route_error'}); "
                "模型未切換。"
            )
            return
        selected = self._sessions.select_model(model)
        self._write_info(f"✅ 本會話模型已設為 {selected.model}。")

    def _command_provider(self, arguments: tuple[str, ...]) -> None:
        if arguments:
            self._write_info(
                "Provider 在 CLI 進程啟動時固定。請用 XDSH_DSH_PROVIDER 或 --provider 重啟; "
                "不會在執行中的 DSH 進程中假裝熱切換。"
            )
            return
        self._write_info(f"目前 Provider: {self.config.provider}\n用 /model list 查看路由目錄。")

    def _command_reasoning(self, arguments: tuple[str, ...]) -> None:
        session = self._sessions.current()
        if not arguments:
            self._write_info(f"推理強度: {session.reasoning_effort}")
            return
        value = arguments[0]
        if value not in REASONING_EFFORTS:
            self._write_info("可用值: " + ", ".join(REASONING_EFFORTS))
            return
        selected = self._sessions.select_reasoning(cast(ReasoningEffort, value))
        self._write_info(f"✅ 推理強度已設為 {selected.reasoning_effort}。")

    def _command_thinking(self, arguments: tuple[str, ...]) -> None:
        session = self._sessions.current()
        if not arguments:
            self._write_info(f"Thinking 輸出: {'on' if session.show_thinking else 'off'}")
            return
        value = arguments[0].casefold()
        if value not in {"on", "off", "show", "hide", "1", "0"}:
            self._write_info("用法: /thinking on|off")
            return
        show = value in {"on", "show", "1"}
        self._sessions.set_show_thinking(show)
        self._write_info(f"✅ Thinking 輸出已{'開啟' if show else '關閉'}。")

    def _command_fallback(self, arguments: tuple[str, ...]) -> None:
        if not arguments:
            self._write_info(f"備用模型: {self._sessions.current().fallback_model or 'off'}")
            return
        value = None if arguments[0].casefold() in {"off", "none", "-"} else arguments[0]
        selected = self._sessions.select_fallback(value)
        self._controls.set_fallback(selected.runner_session_id, value)
        self._write_info(f"✅ 備用模型已設為 {value or 'off'}。")

    async def _reset_current(self) -> None:
        session = self._sessions.current()
        await self._runner.reset(session.runner_session_id)
        self._controls.clear_context(session.runner_session_id)
        self._branch_initialized.discard(session.runner_session_id)
        self._write_info(f"🔄 已重置會話 {session.name}; DSH generation 已前進。")

    def _compact_current(self) -> None:
        try:
            result = self._controls.compact(self._sessions.current().runner_session_id)
        except ProductControlError as error:
            self._write_info(f"壓縮失敗: {error}")
            return
        self._write_info(
            f"🗜️ 已壓縮 {result.compacted_items} 項; 保留 {result.after_turns} 個近期回合。"
        )

    async def _maybe_auto_compact(
        self,
        session_id: str,
        *,
        block_on_failure: bool = False,
    ) -> bool:
        """Apply the shared context policy before another CLI task."""

        # The official rc.2 backend compacts inside the same DSH runtime at
        # agent step boundaries.  Resetting here would discard its preserved
        # tail and duplicate the summary work.
        if has_native_capability(self._runner, "automatic_compaction"):
            self._pending_context_resets.discard(session_id)
            return True

        if session_id in self._pending_context_resets:
            try:
                await self._runner.reset(session_id)
            except Exception as error:
                LOGGER.warning(
                    "Pending CLI context reset failed with %s",
                    type(error).__name__,
                )
                if block_on_failure:
                    self._write_info("⚠️ 上下文換代失敗; 本次任務未開始。")
                    return False
            else:
                self._pending_context_resets.discard(session_id)

        try:
            decision = self._controls.auto_compaction_decision(
                session_id,
                policy=self.config.context_compaction_policy,
                context_window_tokens=self.config.context_window_tokens,
            )
        except ProductControlError as error:
            LOGGER.warning(
                "CLI automatic context decision failed: %s",
                safe_exception_text(error, max_chars=400),
            )
            return not block_on_failure
        if not decision.should_compact:
            return True
        try:
            await self._runner.reset(session_id)
        except Exception as error:
            self._pending_context_resets.add(session_id)
            LOGGER.warning("CLI automatic context reset failed with %s", type(error).__name__)
            if block_on_failure:
                self._write_info("⚠️ 上下文已達壓縮門檻, DSH generation 換代失敗; 任務未開始。")
                return False
            self._write_info("⚠️ 上下文已達壓縮門檻, generation 換代將在下一輪重試。")
            return True
        try:
            applied = self._controls.compact_if_needed(
                session_id,
                policy=self.config.context_compaction_policy,
                context_window_tokens=self.config.context_window_tokens,
            )
        except ProductControlError as error:
            LOGGER.warning(
                "CLI automatic context checkpoint failed: %s",
                safe_exception_text(error, max_chars=400),
            )
            self._write_info("⚠️ generation 已換代, 但相容歷史摘要保存失敗。")
            return True
        if applied.compaction is None:
            return True
        result = applied.compaction
        self._write_info(
            "🗜️ 已自動壓縮上下文並切換 DSH generation。"
            f"本回合峰值輸入 {decision.latest_input_tokens:,}/{decision.context_window_tokens:,} "
            f"({decision.percent:.1f}% , 門檻 {decision.threshold_percent:.0f}%) ;"
            f"保留 {result.after_turns} 回合、摘要 {result.compacted_items} 項。"
        )
        return True

    def _goal_command(self, arguments: tuple[str, ...]) -> None:
        session_id = self._sessions.current().runner_session_id
        if not arguments or arguments[0] == "show":
            snapshot = self._controls.snapshot(session_id)
            self._write_info(
                f"目標: {snapshot.goal or '未設定'} ({snapshot.goal_status or 'none'})"
            )
            return
        if arguments[0] in {"clear", "off"}:
            self._controls.set_goal(session_id, None)
            self._write_info("✅ 已清除長任務目標。")
            return
        snapshot = self._controls.set_goal(session_id, " ".join(arguments))
        self._write_info(f"🎯 目標已設定: {snapshot.goal}")

    def _plan_command(self, arguments: tuple[str, ...]) -> None:
        session_id = self._sessions.current().runner_session_id
        if not arguments:
            enabled = self._controls.snapshot(session_id).plan_mode
            self._write_info(f"Plan mode: {'on' if enabled else 'off'}")
            return
        value = arguments[0].casefold()
        if value not in {"on", "off"}:
            self._write_info("用法: /plan on|off")
            return
        self._controls.set_plan_mode(session_id, value == "on")
        self._write_info(f"✅ Plan mode 已{ '開啟' if value == 'on' else '關閉'}。")

    def _skills_command(self, arguments: tuple[str, ...]) -> None:
        session = self._sessions.current()
        workspace = self._workspace_for(session)
        try:
            bundled = builtin_skill_catalog()
            bundle_version = builtin_skill_bundle_version()
            bundle_error: str | None = None
        except RunnerConfigurationError as error:
            # A package-integrity failure must be visible, but it must not
            # make read-only project Skill inspection disappear.
            LOGGER.exception("XDSH built-in Skill bundle failed verification")
            bundled = ()
            bundle_version = None
            bundle_error = str(error)
        try:
            skills = self._controls.list_skills(session.runner_session_id, workspace=workspace)
        except ProductControlError as error:
            self._write_info(f"Skills 不可用: {error}")
            return
        action = arguments[0].casefold() if arguments else "list"
        if action in {"pending", "history"}:
            self._show_skill_candidates()
            return
        if action == "stats":
            self._show_skill_stats()
            return
        if action == "curate":
            self._show_skill_curator(session.runner_session_id, skills)
            return
        if action == "archive":
            self._skill_archive_command(arguments[1:], workspace=workspace)
            return
        if action == "diff" and len(arguments) == 2:
            self._show_skill_candidate_diff(arguments[1])
            return
        if action in {"evaluate", "approve", "reject", "rollback"}:
            if len(arguments) != 2:
                self._write_info(f"用法: /skills {action} <ID>")
                return
            self._decide_skill_candidate(arguments[1], action=action, workspace=workspace)
            return
        if action in {"builtin", "builtins", "bundle"}:
            self._write_info(
                "\n".join(
                    self._render_builtin_skill_lines(
                        bundled,
                        bundle_version=bundle_version,
                        bundle_error=bundle_error,
                    )
                )
            )
            return
        if action in {"list", "ls"}:
            lines = self._render_builtin_skill_lines(
                bundled,
                bundle_version=bundle_version,
                bundle_error=bundle_error,
            )
            lines.extend(("", "🧩 專案 Skills"))
            lines.extend(
                f"{'✅' if skill.enabled else '⏸️'} {skill.name} · {skill.description}"
                for skill in skills
            )
            if not skills:
                lines.append("目前沒有專案 Skill。")
            else:
                lines.append("專案 Skill 可用 /skills on|off NAME 管理。")
            self._write_info("\n".join(lines))
            return
        if action == "show" and len(arguments) == 2:
            requested_name = arguments[1].casefold()
            selected_builtin = next(
                (item for item in bundled if item.name == requested_name),
                None,
            )
            if selected_builtin is not None:
                try:
                    content = read_builtin_skill(selected_builtin.name)
                except RunnerConfigurationError as error:
                    self._write_info(f"內置 Skill 驗證失敗: {error}")
                    return
                self._write_info(
                    self._render_builtin_skill(
                        selected_builtin,
                        content,
                        bundle_version=bundle_version or "unknown",
                    )
                )
                return
            try:
                selected = next(
                    (skill for skill in skills if skill.name == requested_name),
                    None,
                )
            except ProductControlError as error:
                self._write_info(f"Skill 不可用: {error}")
                return
            if selected is None:
                self._write_info(f"Skill 不存在: {arguments[1]}")
                return
            support = (
                "\n支援檔案 (需要時由 DSH native Skill tool 讀取): "
                + ", ".join(selected.support_files)
                + "\n"
                if selected.support_files
                else ""
            )
            self._write_info(
                f"# Skill: {selected.name}\n\n"
                f"狀態: {'啟用' if selected.enabled else '停用'}\n"
                f"{support}\n{selected.content}"
            )
            return
        if len(arguments) == 2 and action in {"on", "off"}:
            requested_name = arguments[1].casefold()
            if any(item.name == requested_name for item in bundled):
                self._write_info(
                    f"📦 {requested_name} 是 XDSH 內置 Skill, "
                    "由已驗證的 package 唯讀管理, 不能覆寫。"
                )
                return
            try:
                selected = self._controls.set_skill_enabled(
                    session.runner_session_id,
                    workspace=workspace,
                    name=arguments[1],
                    enabled=action == "on",
                )
            except ProductControlError as error:
                self._write_info(f"Skill 設定失敗: {error}")
                return
            self._write_info(f"✅ {selected.name} 已{'啟用' if selected.enabled else '停用'}。")
            return
        self._write_info("用法: /skills list|builtin|show NAME|on NAME|off NAME")

    def _show_skill_candidates(self) -> None:
        try:
            snapshot = self._skill_evolution.snapshot(self._local_owner_id)
        except SkillEvolutionError as error:
            self._write_info(f"Skill 演化記錄不可用: {error}")
            return
        lines = [
            "🧩 Skill 演化記錄",
            f"⏳ pending {len(snapshot.pending)} · ✅ applied {len(snapshot.applied)}",
        ]
        for entry in snapshot.entries[-10:]:
            evaluation = (
                "pass"
                if entry.evaluation_passed
                else "fail"
                if entry.evaluation_passed is False
                else "not-run"
            )
            lines.append(
                f"{entry.status} {entry.id} · {entry.action} {entry.name} · "
                f"🧪 {evaluation} · support={len(entry.support_files)} · {entry.summary}"
            )
        if not snapshot.entries:
            lines.append("目前沒有候選。候選永遠不會自動套用。")
        self._write_info("\n".join(lines))

    def _show_skill_stats(self) -> None:
        try:
            summary = self._metrics.summary(since=time.time() - 30 * 86_400)
        except (OSError, ValueError, TypeError, RuntimeError) as error:
            self._write_info(f"Skill stats 暫不可用 ({type(error).__name__})。")
            return
        lines = ["📈 Skill 使用成效 (最近 30 天, 僅觀測不代表因果)"]
        for outcome in summary.skill_outcomes:
            lines.append(
                f"{outcome.skill} · {outcome.succeeded}/{outcome.runs} "
                f"({outcome.success_rate * 100:.0f}%)"
            )
        if not summary.skill_outcomes:
            lines.append("尚無已記錄的 Skill 使用成效。")
        self._write_info("\n".join(lines))

    def _show_skill_candidate_diff(self, entry_id: str) -> None:
        try:
            self._write_info(self._skill_evolution.diff(self._local_owner_id, entry_id))
        except SkillEvolutionError as error:
            self._write_info(f"Skill diff 不可用: {error}")

    def _decide_skill_candidate(
        self,
        entry_id: str,
        *,
        action: str,
        workspace: Path,
    ) -> None:
        try:
            if action == "evaluate":
                report = self._skill_evolution.evaluate(
                    self._local_owner_id,
                    entry_id,
                    workspace=workspace,
                )
                self._write_info(
                    f"{'✅' if report.passed else '⚠️'} Skill 候選 {entry_id} "
                    f"隔離評估{'通過' if report.passed else '未通過'} "
                    f"({len(report.passed_checks)}/5)。"
                )
                return
            if action == "approve":
                result = self._skill_evolution.approve(
                    self._local_owner_id,
                    entry_id,
                    workspace=workspace,
                )
                self._write_info(
                    f"✅ 已套用 Skill {result.candidate.name} ({entry_id}); "
                    "before-state/hash/audit 已保留。"
                )
                return
            if action == "reject":
                candidate = self._skill_evolution.reject(self._local_owner_id, entry_id)
                self._write_info(f"❌ 已拒絕 Skill 候選 {candidate.id}; 工作區未變更。")
                return
            if action == "rollback":
                result = self._skill_evolution.rollback(
                    self._local_owner_id,
                    entry_id,
                    workspace=workspace,
                )
                self._write_info(f"↩️ 已回滾 Skill {result.candidate.name} ({entry_id})。")
                return
            raise SkillEvolutionError("未知 Skill 候選操作")
        except SkillEvolutionError as error:
            self._write_info(f"Skill 操作未完成: {error}")

    def _show_skill_curator(self, session_id: str, skills: Sequence[object]) -> None:
        del session_id
        try:
            outcomes = self._metrics.summary(
                since=time.time() - 30 * 86_400,
            ).skill_outcomes
            names = tuple(
                name
                for skill in skills
                if isinstance((name := getattr(skill, "name", None)), str)
            )
            suggestions = self._skill_curator.suggest(names, outcomes, now=time.time())
        except (OSError, ValueError, TypeError, RuntimeError) as error:
            self._write_info(f"Skill curator 暫不可用 ({type(error).__name__})。")
            return
        lines = [
            "🧭 Skill curator (唯讀建議)",
            "資料: 最近 30 天實際選中的 Skill; 低成功率不代表因果。",
        ]
        for item in suggestions:
            rate = "未知" if item.success_rate is None else f"{item.success_rate * 100:.0f}%"
            icon = "🗄️" if item.action == "archive" else "🔎"
            lines.append(
                f"{icon} {item.name} · {item.action} · {item.succeeded}/{item.runs} "
                f"({rate}) · {item.reason}"
            )
        if not suggestions:
            lines.append("目前沒有達到整理門檻的專案 Skill。")
        lines.append("archive 只會建立待批准候選; 批准後仍可 rollback。")
        self._write_info("\n".join(lines))

    def _skill_archive_command(self, arguments: tuple[str, ...], *, workspace: Path) -> None:
        action = arguments[0].casefold() if arguments else "list"
        owner = self._local_owner_id
        try:
            if action in {"list", "ls", "pending", "history"}:
                snapshot = self._skill_archive.snapshot(owner)
                lines = [
                    "🗄️ Skill 歸檔記錄",
                    f"⏳ pending {len(snapshot.pending)} · ✅ archived {len(snapshot.archived)}",
                ]
                lines.extend(
                    f"{entry.status} {entry.id} · {entry.name} · "
                    f"{entry.file_count} files · {entry.total_bytes} bytes"
                    for entry in snapshot.entries[-20:]
                )
                if not snapshot.entries:
                    lines.append("目前沒有歸檔候選。")
                self._write_info("\n".join(lines))
                return
            if action == "diff" and len(arguments) == 2:
                self._write_info(self._skill_archive.diff(owner, arguments[1]))
                return
            if action in {"approve", "reject", "rollback"} and len(arguments) == 2:
                entry_id = arguments[1]
                if action == "approve":
                    result = self._skill_archive.approve(owner, entry_id, workspace=workspace)
                    self._write_info(
                        f"🗄️ 已歸檔 Skill {result.candidate.name} ({entry_id}); "
                        "可用 /skills archive rollback <ID> 回復。"
                    )
                elif action == "reject":
                    candidate = self._skill_archive.reject(owner, entry_id)
                    self._write_info(f"❌ 已拒絕歸檔候選 {candidate.id}; 工作區未變更。")
                else:
                    result = self._skill_archive.rollback(owner, entry_id, workspace=workspace)
                    self._write_info(f"↩️ 已回復 Skill {result.candidate.name} ({entry_id})。")
                return
            if len(arguments) == 1:
                candidate = self._skill_archive.stage(
                    owner,
                    workspace=workspace,
                    name=arguments[0],
                )
                self._write_info(
                    f"🗄️ 已建立歸檔候選 {candidate.id} ({candidate.name}); "
                    "未變更工作區, 請先 /skills archive diff ID 再 approve。"
                )
                return
        except SkillCurationError as error:
            self._write_info(f"Skill 歸檔操作未完成: {error}")
            return
        self._write_info(
            "用法: /skills archive NAME|list|diff ID|approve ID|reject ID|rollback ID"
        )

    async def _plugins_command(self, arguments: tuple[str, ...]) -> None:
        """Inspect or explicitly approve DSH profile plugin operations locally."""

        control = self._plugin_control
        if control is None:
            self._write_info(
                "🧩 DSH 插件管理未启用; 请配置官方 XDSH_DSH_PLUGIN_COMMAND。"
            )
            return
        action = arguments[0].casefold() if arguments else "list"
        if action in {"list", "status", "inventory"}:
            try:
                profiles = control.manager.profiles()
                selected = profiles or (control.manager.default_profile,)
                lines = [
                    "🧩 DSH 原生插件 inventory",
                    "来源为官方 DSH profile; DSH Web/CLI 的变更会在下一回合被发现。",
                    "route code: direct_gateway / agent_tool_only / "
                    "direct_unavailable / gateway_invalid",
                ]
                for profile_name in selected[:20]:
                    report = control.invocation_report(profile_name)
                    inventory = report.inventory
                    if inventory is None:
                        # ``PluginControl`` always attaches the coherent
                        # inventory; keep a defensive branch for custom
                        # adapters used by downstream callers.
                        inventory = control.inventory(profile_name)
                    lines.append(
                        f"\n📦 profile {profile_name} · {len(inventory.plugins)} packages · "
                        f"{len(inventory.bundles)} mounted bundles · "
                        f"routes {report.counts[PluginInvocationState.AVAILABLE.value]} direct/"
                        f"{report.counts[PluginInvocationState.AGENT_TOOL.value]} agent-tool/"
                        f"{report.counts[PluginInvocationState.UNAVAILABLE.value]} no-direct"
                    )
                    for item in inventory.plugins[:40]:
                        invocation = report.status_for(item.package)
                        if invocation is None:
                            continue
                        invocation_label = {
                            PluginInvocationState.AVAILABLE: "direct",
                            PluginInvocationState.AGENT_TOOL: "agent-tool",
                            PluginInvocationState.UNAVAILABLE: "no-direct",
                            PluginInvocationState.NOT_INSTALLED: "missing",
                        }[invocation.state]
                        lines.append(
                            f"{'🔌' if item.bundle else '📚'} {item.package}@{item.version} · "
                            f"{item.surface.value}{' · mounted' if item.bundle else ''}"
                            f" · {invocation_label} ({invocation.reason_code})"
                        )
                if not profiles:
                    lines.append("目前没有可读 profile。")
                self._write_info("\n".join(lines))
            except PluginError as error:
                self._write_info(f"🧩 DSH plugin inventory 读取失败: {error}")
            return
        if action in {"profiles", "profile"}:
            try:
                profiles = control.manager.profiles()
            except PluginError as error:
                self._write_info(f"🧩 DSH profile 读取失败: {error}")
                return
            self._write_info(
                "🧩 DSH profiles\n"
                + (
                    "\n".join(
                        f"{'✓' if name == control.manager.default_profile else '·'} {name}"
                        for name in profiles
                    )
                    if profiles
                    else "尚未发现 DSH profile。"
                )
            )
            return
        if action not in {"install", "add", "remove", "uninstall", "rm", "sync"}:
            self._write_info(
                "用法: /plugins [list|profiles|install SPEC [--profile NAME] [--approve]|"
                "remove PACKAGE [--profile NAME] [--approve]|sync SOURCE [TARGET] [--approve]]"
            )
            return
        try:
            values, profile, approved = _parse_cli_plugin_arguments(arguments[1:])
            plans: tuple[PluginInstallPlan, ...]
            if action in {"install", "add"}:
                if len(values) != 1:
                    raise PluginError("用法: /plugins install NAME@EXACT-VERSION [--profile NAME]")
                plan = control.manager.plan_install(values[0], profile=profile)
                plans = (plan,)
                operation = "install"
            elif action in {"remove", "uninstall", "rm"}:
                if len(values) != 1:
                    raise PluginError("用法: /plugins remove PACKAGE [--profile NAME]")
                plans = (control.manager.plan_remove(values[0], profile=profile),)
                operation = "remove"
            else:
                if not 1 <= len(values) <= 2:
                    raise PluginError("用法: /plugins sync SOURCE [TARGET]")
                plans = control.manager.sync_plan(
                    source_profile=values[0],
                    target_profile=profile or (values[1] if len(values) == 2 else None),
                )
                operation = "install"
                if not plans:
                    self._write_info("🧩 没有需要同步的已锁定 bundle。")
                    return
        except (PluginError, ValueError) as error:
            self._write_info(f"🧩 插件操作无效: {error}")
            return
        if not approved:
            specs = ", ".join(plan.package_spec for plan in plans)
            self._write_info(
                f"🧩 已生成插件{('安装' if operation == 'install' else '移除')}计划: {specs}\n"
                "这是本地显式审批边界; 确认无误后在同一命令末尾加入 --approve 再执行。"
            )
            return
        if len(plans) > 32:
            self._write_info("🧩 同步计划超过 32 项上限; 未执行任何变更。")
            return
        results: list[PluginInstallResult] = []
        try:
            for plan in plans:
                result = (
                    await control.manager.install(plan, approved=True)
                    if operation == "install"
                    else await control.manager.remove(
                        plan.package_name,
                        profile=plan.profile,
                        approved=True,
                    )
                )
                results.append(result)
        except (PluginError, OSError, ValueError) as error:
            self._write_info(f"🧩 官方 DSH plugin 操作未完成: {error}")
            return
        successful = sum(result.succeeded for result in results)
        if successful:
            try:
                await self._runner.reset(self._sessions.current().runner_session_id)
            except Exception as error:
                LOGGER.warning(
                    "CLI runtime reload after plugin mutation failed: %s",
                    type(error).__name__,
                )
        output_parts = [
            f"🧩 插件操作完成: {successful}/{len(results)} 成功。",
            *(
                f"{result.plan.package_spec} · exit={result.exit_code}"
                for result in results
            ),
        ]
        self._write_info("\n".join(output_parts))

    @staticmethod
    def _render_builtin_skill_lines(
        bundled: tuple[BuiltinSkill, ...],
        *,
        bundle_version: str | None,
        bundle_error: str | None,
    ) -> list[str]:
        lines = ["📦 XDSH 內置 Skills"]
        if bundle_error is not None:
            lines.append(f"⚠️ package 驗證失敗: {bundle_error}")
            return lines
        lines.append(
            f"Bundle v{bundle_version or 'unknown'} · XDSH package source · 唯讀"
        )
        lines.extend(
            f"✅ {item.name} · {item.description}"
            for item in bundled
        )
        lines.append("DSH runner 首次使用時將它們投影到獨立 DSH_HOME。")
        return lines

    @staticmethod
    def _render_builtin_skill(
        skill: BuiltinSkill,
        content: str,
        *,
        bundle_version: str,
    ) -> str:
        return (
            f"# XDSH 內置 Skill: {skill.name}\n\n"
            f"來源: XDSH package bundle v{bundle_version}\n"
            f"版本: {skill.version} · provenance: {skill.provenance}\n"
            "狀態: ✅ DSH runner 自動載入 · 唯讀\n\n"
            f"{content}"
        )

    def _export_current(self) -> None:
        try:
            document = self._controls.export(self._sessions.current().runner_session_id)
            destination = self.config.state_root / document.filename
            if destination.exists() or destination.is_symlink():
                self._write_info("匯出檔已存在; 為避免覆寫, 請先移走它。")
                return
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(document.content)
                handle.flush()
                os.fsync(handle.fileno())
            self._write_info(f"📄 已匯出至 XDSH state root: {destination.name}")
        except (OSError, ProductControlError) as error:
            self._write_info(f"匯出失敗 ({type(error).__name__})")

    def _record_turn(
        self,
        session: NamedSession,
        prompt: str,
        renderer: ConsoleTurnRenderer,
        events: Sequence[RunnerEventT],
        *,
        started_at: float | None = None,
        selected_skills: tuple[str, ...] = (),
    ) -> None:
        input_tokens = output_tokens = cache_tokens = reasoning_tokens = latest_input = 0
        tool_calls = 0
        guard_reason: str | None = None
        error_code: str | None = None
        verification_issues: tuple[str, ...] = ()
        for event in events:
            if isinstance(event, UsageEvent):
                input_tokens += max(0, event.input_tokens)
                output_tokens += max(0, event.output_tokens)
                cache_tokens += max(0, event.cache_read_tokens + event.cache_write_tokens)
                reasoning_tokens += max(0, event.reasoning_tokens)
                latest_input = max(
                    latest_input,
                    max(0, event.input_tokens)
                    + max(0, event.cache_read_tokens)
                    + max(0, event.cache_write_tokens),
                )
            elif isinstance(event, ToolStartEvent):
                tool_calls += 1
            elif isinstance(event, GuardEvent) and event.action.value == "stop":
                guard_reason = event.reason.value
            elif isinstance(event, ErrorEvent):
                error_code = safe_error_code(event.code)
                verification_issues = event.verification_issues
        try:
            self._controls.record_turn(
                session.runner_session_id,
                user_text=prompt,
                assistant_text=renderer.answer,
                model=session.model,
                usage=TurnUsage(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_tokens=cache_tokens,
                    reasoning_tokens=reasoning_tokens,
                    latest_input_tokens=latest_input,
                ),
                succeeded=renderer.succeeded,
                verification_issues=verification_issues,
            )
        except ProductControlError:
            LOGGER.warning("could not persist CLI product-session turn")
        try:
            finish = next(
                (event for event in reversed(events) if isinstance(event, FinishEvent)),
                None,
            )
            self._metrics.record(
                RunMetric(
                    finished_at=time.time(),
                    model=session.model,
                    succeeded=renderer.succeeded,
                    finish_reason=(
                        safe_error_code(finish.reason)
                        if finish is not None
                        else "unknown"
                    ),
                    duration_ms=(
                        max(0, int((self._clock() - started_at) * 1000))
                        if started_at is not None
                        else 0
                    ),
                    first_event_ms=None,
                    cold_start_ms=None,
                    cold_start_retries=0,
                    tool_calls=tool_calls,
                    total_tokens=input_tokens + output_tokens + cache_tokens,
                    guard_reason=guard_reason,
                    error_code=error_code,
                    selected_skills=selected_skills,
                )
            )
        except (OSError, ValueError, TypeError):
            LOGGER.warning("could not persist CLI run metric")

    async def _cancel_session(self, session_id: str) -> bool:
        try:
            return await self._runner.cancel(session_id)
        except Exception as error:
            LOGGER.warning("CLI cancellation failed with %s", type(error).__name__)
            return False

    def _workspace_for(self, session: NamedSession) -> Path:
        workspace = (self.config.workspace_root / session.key).resolve()
        if not workspace.is_relative_to(self.config.workspace_root.resolve()):
            raise CliError("session workspace escaped the configured root")
        if workspace.is_symlink():
            raise CliError("session workspace cannot be a symlink")
        workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
        workspace.chmod(0o700)
        return workspace

    def _touch_heartbeat(self) -> None:
        try:
            self._heartbeat.touch()
        except HeartbeatError:
            # A heartbeat is observability only; never turn a healthy task into
            # a failure because an operator made the state root read-only.
            LOGGER.warning("XDSH CLI heartbeat could not be updated")

    async def _heartbeat_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await asyncio.sleep(30.0)
            if stop.is_set():
                return
            self._touch_heartbeat()

    def _maybe_progress(
        self,
        progress: ConsoleProgressRenderer,
        started: float,
        *,
        force: bool = False,
        final: bool = False,
    ) -> None:
        if self.config.progress_mode == "never":
            return
        if self.config.progress_mode == "auto" and not self._progress_is_tty and not final:
            return
        now = self._clock()
        if not force and now - self._last_progress_at < self.config.progress_interval_seconds:
            return
        elapsed = max(0, int(now - started))
        rendered = progress.render(
            elapsed_seconds=elapsed,
            final=final,
            one_line=self._progress_is_tty,
        )
        if self._progress_is_tty:
            self.progress_output.write("\r\033[2K" + rendered)
            if final:
                self.progress_output.write("\n")
        else:
            self.progress_output.write("⏳ " + rendered + "\n")
        self.progress_output.flush()
        self._last_progress_at = now

    def _display_result(self, result: CliTurnResult) -> None:
        if result.thinking:
            for chunk in _chunks(result.thinking, 30_000, header="### 🧠 Thinking"):
                self.output.write(chunk + "\n\n")
        if result.answer:
            self.output.write(result.answer + "\n")
        if result.terminal_notice:
            self.progress_output.write(result.terminal_notice + "\n")
            self.progress_output.flush()
        self.output.flush()

    def _status_text(self, *, detail: str | None = None) -> str:
        session = self._sessions.current()
        snapshot = self._controls.snapshot(session.runner_session_id)
        if detail == "cost":
            usage = snapshot.usage
            return (
                f"📊 turns={usage.turns} · input={usage.input_tokens} · "
                f"output={usage.output_tokens} · cache={usage.cache_tokens} · "
                f"reasoning={usage.reasoning_tokens}"
            )
        if detail == "context":
            latest = snapshot.usage.latest_input_tokens
            policy = self.config.context_compaction_policy
            native_compaction = has_native_capability(
                self._runner, "automatic_compaction"
            )
            decision = policy.evaluate(
                latest_input_tokens=latest,
                context_window_tokens=self.config.context_window_tokens,
                retained_turns=max(
                    len(snapshot.turns),
                    max(0, snapshot.usage.turns - snapshot.last_compaction_turn),
                ),
                turns_since_compaction=max(
                    0,
                    snapshot.usage.turns - snapshot.last_compaction_turn,
                ),
            )
            state = (
                "native trigger reached"
                if native_compaction and latest >= decision.threshold_tokens
                else "native monitoring"
                if native_compaction
                else "ready"
                if decision.should_compact
                else decision.reason.replace("_", " ")
            )
            return (
                f"🧠 session={session.name} · peak_context={latest:,}/"
                f"{self.config.context_window_tokens:,} tokens ({decision.percent:.1f}%) · "
                f"auto_compact={'on' if policy.enabled else 'off'} "
                f"mode={'native' if native_compaction else 'fallback'} · state={state} · "
                f"warn {decision.warning_percent:.0f}% / "
                f"trigger {decision.threshold_percent:.0f}% · "
                f"native_retain {self.config.auto_compact_retain_ratio:.0%} · "
                f"retained={snapshot.resume_characters} chars · "
                f"fallback_compactions={snapshot.compaction_count} · goal={snapshot.goal or 'none'}"
            )
        return (
            f"📌 {session.name} ({session.key})\n"
            f"model={session.model} · provider={self.config.provider}\n"
            f"reasoning={session.reasoning_effort} · "
            f"thinking={'on' if session.show_thinking else 'off'}\n"
            f"workspace={self._workspace_for(session)}"
        )

    async def _doctor_command(self) -> None:
        """Show and explicitly persist a content-free local health snapshot."""

        report = await ServiceReconciler(
            ReconcilerConfig(
                state_root=self.config.state_root,
                record_trends=True,
            )
        ).run_once()
        self._write_info(_doctor_text(report))

    def _session_list_text(self) -> str:
        current = self._sessions.current().key
        lines = ["會話:"]
        for session in self._sessions.list_sessions():
            marker = "*" if session.key == current else " "
            lines.append(
                f"{marker} {session.name} [{session.key}] · {session.model} · "
                f"reasoning={session.reasoning_effort}"
            )
        return "\n".join(lines)

    def _write_prompt(self) -> None:
        session = self._sessions.current()
        self.output.write(f"xdsh[{session.name}]> ")
        self.output.flush()

    def _write_info(self, text: str) -> None:
        self.output.write(safe_outbound_text(text, max_chars=60_000).rstrip() + "\n")
        self.output.flush()

    def _readline(self) -> str:
        line = self.input_stream.readline()
        if line == "":
            raise EOFError
        return line.rstrip("\n")

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for signal_number in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signal_number, self._stop_requested.set)
            except (NotImplementedError, RuntimeError):
                pass

    @staticmethod
    def _build_runner(config: CliConfig) -> AgentRunner:
        dsh_config = DshRunnerConfig(
            dsh_home=config.dsh_home,
            model=config.model,
            provider=config.provider,
            cordis=config.cordis,
            runtime_command=config.runtime_command,
            max_tokens=config.max_tokens,
            model_max_tokens=config.model_max_tokens,
            max_parallel_tool_calls=config.max_parallel_tool_calls,
            default_system_prompt=config.system_prompt,
            context_window=config.context_window_tokens,
            auto_compaction_enabled=config.auto_compact_enabled,
            native_compaction_enabled=config.native_compaction_enabled,
            auto_compaction_threshold_ratio=config.auto_compact_threshold_ratio,
            auto_compaction_retain_ratio=config.auto_compact_retain_ratio,
            auto_compaction_threshold_tokens=config.auto_compact_threshold_tokens,
            request_timeout_seconds=config.task_timeout_seconds,
            rate_limit_policy=config.rate_limit_policy,
        )
        rate_limited = RateLimitedRunner(
            DshRunner(dsh_config),
            provider=config.provider,
            policy=config.rate_limit_policy,
        )
        return GuardedRunner(
            FallbackRunner(rate_limited),
            policy=config.effective_guard_policy,
            acceptance_root=config.state_root / "acceptance",
        )


class CliError(RuntimeError):
    """A user-facing CLI operation failed without exposing secrets."""


def _append_synthetic_event(
    events: list[RunnerEventT],
    renderer: ConsoleTurnRenderer,
    progress: ConsoleProgressRenderer,
    event: RunnerEventT,
) -> None:
    events.append(event)
    renderer.accept(event)
    progress.accept(event)


def _force_progress(event: RunnerEventT) -> bool:
    return isinstance(
        event,
        GuardEvent | ErrorEvent | FinishEvent | ReasoningEvent | CompactionEvent,
    )


def _chunks(text: str, limit: int, *, header: str) -> tuple[str, ...]:
    if not text:
        return ()
    chunks: list[str] = []
    remaining = text.strip()
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        boundary = remaining.rfind("\n", 0, limit)
        if boundary < limit // 2:
            boundary = limit
        chunks.append(remaining[:boundary].rstrip())
        remaining = remaining[boundary:].lstrip()
    return tuple(
        f"{header}{' (續)' if index else ''}\n\n{chunk}"
        for index, chunk in enumerate(chunks)
    )


def _doctor_text(report: ReconciliationReport) -> str:
    lines = [f"🩺 XDSH doctor: {report.overall} · checked={report.checked_at:.3f}"]
    for component in report.components:
        age = f" · age={component.age_seconds:.1f}s" if component.age_seconds is not None else ""
        lines.append(f"{component.name}: {component.status} · {component.reason}{age}")
    for trend in report.trends:
        lines.append(
            f"📈 {trend.days}d · samples={trend.samples} · "
            f"unknown={trend.unknown_ratio:.1%} · degraded={trend.degraded_ratio:.1%}"
        )
        for alert in trend.alerts[:8]:
            route = f" · {alert.route}" if alert.route else ""
            lines.append(
                f"⚠️ {alert.code} · {alert.component}{route} · \u00d7{alert.occurrences}"
            )
    if report.actions:
        lines.append("actions: " + ", ".join(report.actions))
    return "\n".join(lines)


def _parse_command(line: str) -> tuple[str, tuple[str, ...]] | None:
    stripped = line.strip()
    if not stripped.startswith("/"):
        return None
    try:
        tokens = shlex.split(stripped[1:])
    except ValueError:
        return None
    if not tokens:
        return None
    name = tokens[0].casefold()
    if name.endswith("@xdsh"):
        name = name[:-5]
    if not name.replace("_", "").isalnum():
        return None
    return name, tuple(tokens[1:])


def _parse_cli_plugin_arguments(
    arguments: tuple[str, ...],
) -> tuple[tuple[str, ...], str | None, bool]:
    """Parse the finite local plugin approval grammar without forwarding flags."""

    if len(arguments) > 8 or sum(len(item) for item in arguments) > 1_000:
        raise PluginError("插件命令参数超过限制")
    values: list[str] = []
    profile: str | None = None
    approved = False
    index = 0
    while index < len(arguments):
        item = arguments[index]
        if item == "--approve":
            if approved:
                raise PluginError("--approve 不能重复")
            approved = True
            index += 1
            continue
        if item in {"--profile", "-p"}:
            if profile is not None or index + 1 >= len(arguments):
                raise PluginError("--profile 必须且只能提供一次名称")
            profile = arguments[index + 1]
            index += 2
            continue
        if item.startswith("-"):
            raise PluginError(f"不支持的插件选项: {item}")
        values.append(item)
        index += 1
    return tuple(values), profile, approved


def _safe_root(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if resolved in {Path(resolved.anchor), Path.home().resolve()}:
        raise CliConfigurationError(f"{label} must be a dedicated directory")
    if ".xclaude" in resolved.parts or ".dsh" in resolved.parts:
        raise CliConfigurationError(f"{label} is reserved or unsafe")
    return resolved


def _valid_model(model: str) -> bool:
    import re

    return re.fullmatch(_MODEL_ID, model) is not None


def _validate_loopback_url(url: str) -> None:
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError as error:
        raise CliConfigurationError("LiteLLM URL has an invalid port") from error
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in _LOOPBACK_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or (parsed.scheme == "http" and port is None)
    ):
        raise CliConfigurationError("LiteLLM gateway must be a credential-free loopback URL")


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
    if not value:
        raise CliConfigurationError(f"required environment variable is unset: {name}")
    return value


def _parse_runtime(raw: str) -> tuple[str, ...]:
    if not raw.strip():
        raise CliConfigurationError("XDSH_DSH_RUNTIME_COMMAND must be set explicitly")
    try:
        command = tuple(shlex.split(raw))
    except ValueError as error:
        raise CliConfigurationError("XDSH_DSH_RUNTIME_COMMAND has invalid quoting") from error
    if not command or any(not item for item in command):
        raise CliConfigurationError("XDSH_DSH_RUNTIME_COMMAND must contain argv entries")
    return command


def _parse_plugin_command(raw: str) -> tuple[str, ...]:
    if not raw.strip():
        raise CliConfigurationError("XDSH_DSH_PLUGIN_COMMAND must not be empty")
    try:
        command = tuple(shlex.split(raw))
    except ValueError as error:
        raise CliConfigurationError("XDSH_DSH_PLUGIN_COMMAND has invalid quoting") from error
    if not command or any(not item for item in command):
        raise CliConfigurationError("XDSH_DSH_PLUGIN_COMMAND must contain argv entries")
    return command


def _parse_bool(raw: str, name: str) -> bool:
    normalized = raw.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise CliConfigurationError(f"{name} must be a boolean")


def _optional_int(
    raw: str | None,
    name: str,
    *,
    default: int | None = None,
) -> int | None:
    if raw is None:
        return default
    if not raw.strip():
        return None
    try:
        return int(raw)
    except ValueError as error:
        raise CliConfigurationError(f"{name} must be an integer") from error


def _guard_policy_from_env(environment: Mapping[str, str]) -> RunGuardPolicy:
    def resource_limit(name: str, default: int) -> int | None:
        if environment.get(name, "").strip().casefold() == "unlimited":
            return None
        return integer(name, default)

    def integer(name: str, default: int) -> int:
        try:
            return int(environment.get(name, str(default)))
        except ValueError as error:
            raise CliConfigurationError(f"{name} must be an integer") from error

    def number(name: str, default: float) -> float:
        try:
            return float(environment.get(name, str(default)))
        except ValueError as error:
            raise CliConfigurationError(f"{name} must be a number") from error

    def optional_runtime() -> float | None:
        raw = environment.get("XDSH_GUARD_MAX_RUNTIME_SECONDS", "").strip()
        if raw.casefold() in {"", "0", "off", "none", "unlimited"}:
            return None
        try:
            return float(raw)
        except ValueError as error:
            raise CliConfigurationError(
                "XDSH_GUARD_MAX_RUNTIME_SECONDS must be a number or unlimited"
            ) from error

    try:
        return RunGuardPolicy(
            max_model_steps=resource_limit("XDSH_GUARD_MAX_MODEL_STEPS", 120),
            max_tool_calls=resource_limit("XDSH_GUARD_MAX_TOOL_CALLS", 160),
            max_same_tool_calls=integer("XDSH_GUARD_MAX_REPEATED_TOOL_CALLS", 8),
            max_stagnant_poll_calls=integer("XDSH_GUARD_MAX_STAGNANT_POLL_CALLS", 4),
            max_same_tool_errors=integer("XDSH_GUARD_MAX_REPEATED_TOOL_ERRORS", 3),
            max_consecutive_tool_errors=integer("XDSH_GUARD_MAX_CONSECUTIVE_TOOL_ERRORS", 8),
            max_retries_without_progress=integer("XDSH_GUARD_MAX_RETRIES_WITHOUT_PROGRESS", 6),
            max_transient_retries=integer("XDSH_GUARD_MAX_TRANSIENT_RETRIES", 2),
            max_total_tokens=resource_limit("XDSH_GUARD_MAX_TOTAL_TOKENS", 2_000_000),
            max_parallel_subagents=resource_limit("XDSH_GUARD_MAX_PARALLEL_SUBAGENTS", 3),
            stall_timeout_seconds=number("XDSH_GUARD_STALL_SECONDS", 600.0),
            active_tool_stall_timeout_seconds=number(
                "XDSH_GUARD_ACTIVE_TOOL_STALL_SECONDS", 1800.0
            ),
            max_runtime_seconds=optional_runtime(),
            warning_ratio=number("XDSH_GUARD_WARNING_RATIO", 0.8),
            cancel_timeout_seconds=number("XDSH_GUARD_CANCEL_TIMEOUT_SECONDS", 10.0),
        )
    except ValueError as error:
        raise CliConfigurationError(f"invalid task protection policy: {error}") from error


def _rate_limit_policy_from_env(environment: Mapping[str, str]) -> RateLimitPolicy:
    try:
        return rate_limit_policy_from_env(environment)
    except ValueError as error:
        raise CliConfigurationError(f"invalid upstream rate-limit policy: {error}") from error


def _model_directory_text(directory: ModelDirectory, current_model: str) -> str:
    """Render a bounded, capability-aware model catalog for the CLI."""

    lines = [
        "可用模型 (🟢已探測 · ⚪未知/待探測 · 🔴不可用; 能力徽章只代表明確元資料):",
    ]
    rendered = 0
    total = len(directory.models)
    for provider in directory.providers:
        lines.append(provider.display_label + ":")
        for model in provider.models:
            if rendered >= 256:
                break
            health = directory.health_for(model).availability
            icon = {
                ModelAvailability.AVAILABLE: "🟢",
                ModelAvailability.UNAVAILABLE: "🔴",
                ModelAvailability.UNKNOWN: "⚪",
            }[health]
            profile = directory.profile_for(model)
            badges = "".join(
                badge
                for capability, badge in (
                    ("reasoning", "🧠"),
                    ("thinking", "💭"),
                    ("tools", "🛠️"),
                    ("vision", "👁️"),
                )
                if profile.capability(capability) is True
            )
            marker = "✓ " if model == current_model else "  "
            lines.append(f"  {marker}{icon} {model}{badges}")
            rendered += 1
        if rendered >= 256:
            break
    if total > rendered:
        lines.append(f"…另有 {total - rendered} 个模型未展开。")
    return "\n".join(lines)


_HELP_TEXT = """可用命令:
/session list|new NAME|branch [NAME]|switch ID|rename ID NAME|delete ID|reset
/model [MODEL|list]    /provider    /reasoning LEVEL    /thinking on|off
/fallback MODEL|off    /cancel     /reset              /compact
/goal [TEXT|show|clear] /plan on|off /cost /context
/skills list|builtin|show NAME|on NAME|off NAME
        pending|diff|evaluate|approve|reject|rollback ID|curate
        archive NAME|list|diff ID|approve ID|reject ID|rollback ID
/plugins list|profiles|install|remove|sync (变更需 --approve)
/status /doctor /export /quit
輸入普通文字即可執行任務; 進度在 stderr, 最終 Markdown 答案在 stdout。"""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--version",
        action="version",
        version=(
            f"XDSH {XDSH_VERSION_DISPLAY} "
            f"(package {XDSH_VERSION}; DSH {DSH_COMPATIBLE_VERSION})"
        ),
    )
    parser.add_argument("--prompt", help="run one task and exit")
    parser.add_argument("--prompt-file", type=Path, help="read one task from a UTF-8 file")
    parser.add_argument("--session", help="initial session key or name")
    parser.add_argument("--state-root", type=Path)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--dsh-home", type=Path)
    parser.add_argument("--model", default=None)
    parser.add_argument("--provider", default=None)
    parser.add_argument("--runtime-command", default=None)
    parser.add_argument("--plugin-command", default=None)
    parser.add_argument("--reasoning-effort", choices=REASONING_EFFORTS, default=None)
    parser.add_argument("--show-thinking", action="store_true")
    parser.add_argument("--progress", choices=("auto", "always", "never"), default=None)
    parser.add_argument("--progress-interval", type=float, default=None)
    parser.add_argument("--task-timeout", type=float, default=None)
    parser.add_argument("--max-tokens", type=int, default=None)
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="show and record a content-free local health snapshot",
    )
    parser.add_argument(
        "--doctor-json",
        action="store_true",
        help="emit the --doctor snapshot as JSON",
    )
    parser.add_argument("--check", action="store_true", help="validate environment without running")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    if args.prompt is not None and args.prompt_file is not None:
        raise SystemExit("--prompt and --prompt-file are mutually exclusive")
    try:
        runtime = _parse_runtime(args.runtime_command) if args.runtime_command else None
        plugin_command = (
            _parse_plugin_command(args.plugin_command) if args.plugin_command else None
        )
        config = CliConfig.from_env(
            state_root=args.state_root,
            workspace_root=args.workspace,
            dsh_home=args.dsh_home,
            model=args.model,
            provider=args.provider,
            runtime_command=runtime,
            plugin_command=plugin_command,
            progress_mode=args.progress,
            progress_interval_seconds=args.progress_interval,
            reasoning_effort=args.reasoning_effort,
            show_thinking=True if args.show_thinking else None,
        )
        if args.task_timeout is not None:
            config = replace(config, task_timeout_seconds=args.task_timeout)
        if args.max_tokens is not None:
            config = replace(config, max_tokens=args.max_tokens)
        if args.check:
            print("XDSH CLI configuration valid (credentials referenced by environment only).")
            return
        if args.doctor or args.doctor_json:
            report = asyncio.run(
                ServiceReconciler(
                    ReconcilerConfig(
                        state_root=config.state_root,
                        record_trends=True,
                    )
                ).run_once()
            )
            if args.doctor_json:
                import json

                print(json.dumps(report.as_dict(), ensure_ascii=False, sort_keys=True))
            else:
                print(_doctor_text(report))
            return
        app = InteractiveCli(config)

        async def run() -> int:
            try:
                if args.session:
                    app.sessions.switch(args.session)
                if args.prompt_file is not None:
                    prompt_path = args.prompt_file.expanduser().resolve()
                    if not prompt_path.is_file():
                        raise CliError("prompt file is not a regular file")
                    prompt = prompt_path.read_text(encoding="utf-8")
                elif args.prompt is not None:
                    prompt = args.prompt
                else:
                    return await app.interactive()
                result = await app.run_and_display(prompt)
                return 0 if result.succeeded else 1
            finally:
                await app.close()

        raise SystemExit(asyncio.run(run()))
    except (CliConfigurationError, CliError, SessionRegistryError, OSError, ValueError) as error:
        raise SystemExit(safe_exception_text(error, max_chars=600)) from None


if __name__ == "__main__":
    main()
