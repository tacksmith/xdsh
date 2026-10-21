"""Validated environment configuration for the Telegram transport."""

from __future__ import annotations

import os
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from xdsh.core.context import ContextCompactionPolicy
from xdsh.core.decision_router import DecisionMode
from xdsh.core.execution_policy import (
    DEFAULT_MAX_PARALLEL_TOOL_CALLS,
    validate_parallel_tool_limit,
)
from xdsh.core.i18n import UPGRADE_DEFAULT_LANGUAGE, canonical_language
from xdsh.core.models import ModelProvider, parse_model_provider_aliases
from xdsh.core.rate_limit import RateLimitPolicy, rate_limit_policy_from_env
from xdsh.core.run_guard import RunGuardPolicy
from xdsh.core.settings import SettingsError, validate_settings_path


class TelegramConfigurationError(ValueError):
    """Telegram startup configuration is missing or unsafe."""


@dataclass(frozen=True, slots=True, kw_only=True)
class TelegramConfig:
    """All process-level settings required by the Telegram vertical slice."""

    token: str = field(repr=False)
    allowed_user_ids: frozenset[int]
    state_root: Path
    workspace_root: Path
    dsh_home: Path
    litellm_base_url: str
    model: str
    runtime_command: tuple[str, ...]
    decision_mode: DecisionMode = "rules"
    decision_model: str | None = None
    decision_timeout_seconds: float = 1.2
    # Optional official DSH CLI argv used only for approval-gated plugin
    # management.  When omitted, the service derives it for alpha profile
    # launchers and otherwise keeps plugin mutation unavailable.
    plugin_command: tuple[str, ...] | None = None
    model_provider_aliases: tuple[ModelProvider, ...] = ()
    settings_path: Path | None = None
    proxy_url: str | None = None
    api_base_url: str = "https://api.telegram.org"
    provider: str = "litellm"
    workspace_scope: str = "session"
    system_prompt: str = "You are a software engineering agent working through XDSH."
    default_language: str = UPGRADE_DEFAULT_LANGUAGE
    poll_timeout_seconds: int = 15
    progress_update_seconds: float = 4.0
    progress_heartbeat_seconds: float = 4.0
    task_timeout_seconds: float | None = None
    dsh_cold_start_timeout_seconds: float = 90.0
    dsh_cold_start_retries: int = 1
    dsh_stream_idle_timeout_seconds: float = 120.0
    max_answer_chars: int = 200_000
    max_thinking_chars: int = 20_000
    max_attachment_bytes: int = 10_000_000
    max_parallel_tool_calls: int = DEFAULT_MAX_PARALLEL_TOOL_CALLS
    context_window_tokens: int = 262_144
    auto_compact_enabled: bool = True
    native_compaction_enabled: bool = True
    auto_compact_warning_ratio: float = 0.20
    auto_compact_threshold_ratio: float = 0.50
    auto_compact_target_ratio: float = 0.20
    # DSH native compaction keeps this recent surface fraction verbatim.  It
    # is separate from the outer fallback target, which is a resume-payload
    # objective rather than a DSH surface-retention budget.
    auto_compact_retain_ratio: float = 0.08
    auto_compact_threshold_tokens: int | None = 64_000
    auto_compact_min_turns: int = 1
    auto_compact_cooldown_turns: int = 1
    model_probe_timeout_seconds: float = 20.0
    model_health_ttl_seconds: float = 900.0
    learning_review_model: str | None = None
    learning_review_interval: int = 10
    skill_review_interval: int = 15
    learning_review_timeout_seconds: float = 45.0
    scheduler_timezone: str = "Asia/Shanghai"
    max_background_jobs: int = 3
    job_poll_seconds: float = 15.0
    run_guard_policy: RunGuardPolicy = field(default_factory=RunGuardPolicy)
    rate_limit_policy: RateLimitPolicy = field(default_factory=RateLimitPolicy)

    def __post_init__(self) -> None:
        if self.workspace_scope not in {"session", "owner"}:
            raise TelegramConfigurationError("workspace scope must be session or owner")
        if self.workspace_scope == "owner":
            if len(self.allowed_user_ids) != 1:
                raise TelegramConfigurationError(
                    "owner workspace requires exactly one allowed user"
                )
            workspace = self.workspace_root.expanduser().resolve()
            forbidden = {
                Path(p) for p in ("/", "/home", "/tmp", "/var", "/etc", "/usr", "/opt", "/run")
            }
            forbidden.add(Path.home().resolve())
            if workspace in forbidden:
                raise TelegramConfigurationError("owner workspace must be a dedicated directory")
            for protected in (self.state_root, self.dsh_home):
                root = protected.expanduser().resolve()
                if workspace.is_relative_to(root) or root.is_relative_to(workspace):
                    raise TelegramConfigurationError(
                        "owner workspace must not overlap service state"
                    )
        if not self.token.strip():
            raise TelegramConfigurationError("Telegram bot token must be non-empty")
        if not self.model.strip() or not self.provider.strip():
            raise TelegramConfigurationError("model and provider must be non-empty")
        if self.decision_mode not in {"rules", "shadow", "active", "integrated"}:
            raise TelegramConfigurationError("decision mode is unsupported")
        if self.decision_model is not None and (
            not self.decision_model.strip() or any(char.isspace() for char in self.decision_model)
        ):
            raise TelegramConfigurationError("decision model is invalid")
        if not 0.2 <= self.decision_timeout_seconds <= 2.0:
            raise TelegramConfigurationError("decision timeout must be between 0.2 and 2 seconds")
        try:
            canonical_language(self.default_language)
        except ValueError as error:
            raise TelegramConfigurationError("default language is unsupported") from error
        if not self.runtime_command or any(not argument for argument in self.runtime_command):
            raise TelegramConfigurationError(
                "an explicit non-empty DSH runtime command is required"
            )
        if self.plugin_command is not None and (
            not self.plugin_command or any(not argument for argument in self.plugin_command)
        ):
            raise TelegramConfigurationError("plugin command must use non-empty argv")
        if not 1 <= self.poll_timeout_seconds <= 50:
            raise TelegramConfigurationError("poll timeout must be between 1 and 50 seconds")
        if not 1.0 <= self.progress_update_seconds <= 30.0:
            raise TelegramConfigurationError(
                "progress update interval must be between 1 and 30 seconds"
            )
        if not 1.0 <= self.progress_heartbeat_seconds <= 300.0:
            raise TelegramConfigurationError(
                "progress heartbeat interval must be between 1 and 300 seconds"
            )
        if self.task_timeout_seconds is not None and self.task_timeout_seconds <= 0:
            raise TelegramConfigurationError("task timeout must be positive when set")
        if not 5.0 <= self.dsh_cold_start_timeout_seconds <= 300.0:
            raise TelegramConfigurationError(
                "DSH cold-start timeout must be between 5 and 300 seconds"
            )
        if not 0 <= self.dsh_cold_start_retries <= 3:
            raise TelegramConfigurationError("DSH cold-start retries must be between 0 and 3")
        if not 30.0 <= self.dsh_stream_idle_timeout_seconds <= 3_600.0:
            raise TelegramConfigurationError(
                "DSH stream-idle timeout must be between 30 and 3600 seconds"
            )
        if not 1_000 <= self.max_answer_chars <= 200_000:
            raise TelegramConfigurationError(
                "max answer characters must be between 1000 and 200000"
            )
        if not 1_000 <= self.max_thinking_chars <= 100_000:
            raise TelegramConfigurationError(
                "max thinking characters must be between 1000 and 100000"
            )
        if not 1_024 <= self.max_attachment_bytes <= 20_000_000:
            raise TelegramConfigurationError(
                "maximum attachment size must be between 1024 and 20000000 bytes"
            )
        try:
            validate_parallel_tool_limit(self.max_parallel_tool_calls)
        except ValueError as error:
            raise TelegramConfigurationError(str(error)) from error
        if not 8_192 <= self.context_window_tokens <= 10_000_000:
            raise TelegramConfigurationError(
                "context window must be between 8192 and 10000000 tokens"
            )
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
            raise TelegramConfigurationError(str(error)) from error
        if not 0.05 <= self.auto_compact_retain_ratio < self.auto_compact_threshold_ratio:
            raise TelegramConfigurationError(
                "XDSH_AUTO_COMPACT_RETAIN_RATIO must be at least 0.05 and below the threshold"
            )
        if not 2.0 <= self.model_probe_timeout_seconds <= 120.0:
            raise TelegramConfigurationError(
                "model probe timeout must be between 2 and 120 seconds"
            )
        if not 60.0 <= self.model_health_ttl_seconds <= 86_400.0:
            raise TelegramConfigurationError(
                "model health TTL must be between 60 and 86400 seconds"
            )
        if self.learning_review_model is not None and (
            not self.learning_review_model.strip()
            or any(character.isspace() for character in self.learning_review_model)
        ):
            raise TelegramConfigurationError("learning review model ID is invalid")
        if not 1 <= self.learning_review_interval <= 1_000:
            raise TelegramConfigurationError(
                "learning review interval must be between 1 and 1000"
            )
        if not 1 <= self.skill_review_interval <= 10_000:
            raise TelegramConfigurationError(
                "skill review interval must be between 1 and 10000"
            )
        if not 5.0 <= self.learning_review_timeout_seconds <= 300.0:
            raise TelegramConfigurationError(
                "learning review timeout must be between 5 and 300 seconds"
            )
        try:
            ZoneInfo(self.scheduler_timezone)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise TelegramConfigurationError(
                "scheduler timezone must be a valid IANA timezone"
            ) from error
        if not 1 <= self.max_background_jobs <= 3:
            raise TelegramConfigurationError(
                "maximum background jobs must be between 1 and 3"
            )
        if not 0.05 <= self.job_poll_seconds <= 60.0:
            raise TelegramConfigurationError(
                "background job poll interval must be between 0.05 and 60 seconds"
            )

        state_root = _safe_root(self.state_root, "state root")
        workspace_root = _safe_root(self.workspace_root, "workspace root")
        dsh_home = _safe_root(self.dsh_home, "DSH home")
        if not dsh_home.is_relative_to(state_root):
            raise TelegramConfigurationError("DSH home must be contained by the state root")
        if state_root == workspace_root:
            raise TelegramConfigurationError("state and workspace roots must be separate")
        _validate_telegram_api_url(self.api_base_url)
        if self.proxy_url is not None:
            _validate_proxy_url(self.proxy_url)
        if self.settings_path is not None:
            try:
                validate_settings_path(self.settings_path)
            except SettingsError as error:
                raise TelegramConfigurationError(str(error)) from error

    @property
    def effective_run_guard_policy(self) -> RunGuardPolicy:
        """Fold the legacy total timeout into runner-neutral task protection."""

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
        """Return the immutable policy shared by every runner transport."""

        return ContextCompactionPolicy(
            enabled=self.auto_compact_enabled,
            warning_ratio=self.auto_compact_warning_ratio,
            threshold_ratio=self.auto_compact_threshold_ratio,
            target_ratio=self.auto_compact_target_ratio,
            threshold_tokens=self.auto_compact_threshold_tokens,
            min_turns=self.auto_compact_min_turns,
            cooldown_turns=self.auto_compact_cooldown_turns,
        )

    @property
    def effective_native_retain_ratio(self) -> float:
        """Return the native tail ratio after applying an absolute trigger cap."""

        threshold_ratio = self.auto_compact_threshold_ratio
        if self.auto_compact_threshold_tokens is not None:
            threshold_ratio = min(
                threshold_ratio,
                self.auto_compact_threshold_tokens / self.context_window_tokens,
            )
        return min(self.auto_compact_retain_ratio, threshold_ratio / 2)

    @classmethod
    def from_env(cls, environment: Mapping[str, str] | None = None) -> TelegramConfig:
        """Load XDSH credentials only from the supplied environment."""

        env = os.environ if environment is None else environment
        litellm_base_url = _required(env, "XDSH_LITELLM_BASE_URL")
        _validate_litellm_url(litellm_base_url)
        _required(env, "XDSH_LITELLM_API_KEY")
        runtime_command = tuple(shlex.split(_required(env, "XDSH_DSH_RUNTIME_COMMAND")))
        if not runtime_command:
            raise TelegramConfigurationError("XDSH_DSH_RUNTIME_COMMAND must not be empty")
        plugin_command_text = env.get("XDSH_DSH_PLUGIN_COMMAND", "").strip()
        try:
            plugin_command = (
                tuple(shlex.split(plugin_command_text)) if plugin_command_text else None
            )
        except ValueError as error:
            raise TelegramConfigurationError(
                "XDSH_DSH_PLUGIN_COMMAND has invalid quoting"
            ) from error
        if plugin_command_text and not plugin_command:
            raise TelegramConfigurationError("XDSH_DSH_PLUGIN_COMMAND must not be empty")
        try:
            model_provider_aliases = parse_model_provider_aliases(
                env.get("XDSH_MODEL_PROVIDER_ALIASES", "")
            )
        except ValueError as error:
            raise TelegramConfigurationError(
                f"invalid XDSH_MODEL_PROVIDER_ALIASES: {error}"
            ) from error
        task_timeout_text = env.get("XDSH_TASK_TIMEOUT_SECONDS", "").strip()
        return cls(
            token=_required(env, "XDSH_TELEGRAM_BOT_TOKEN"),
            allowed_user_ids=_parse_allowed_users(
                env.get("XDSH_TELEGRAM_ALLOWED_USERS", "")
            ),
            state_root=Path(env.get("XDSH_STATE_ROOT", "/state")),
            workspace_root=Path(env.get("XDSH_WORKSPACE_ROOT", "/workspace")),
            workspace_scope=env.get("XDSH_WORKSPACE_SCOPE", "session"),
            dsh_home=Path(env.get("XDSH_DSH_HOME", "/state/dsh-home")),
            litellm_base_url=litellm_base_url,
            model=_required(env, "XDSH_LITELLM_MODEL"),
            decision_mode=_parse_decision_mode(env.get("XDSH_DECISION_MODE", "rules")),
            decision_model=env.get("XDSH_DECISION_MODEL", "").strip() or None,
            decision_timeout_seconds=_parse_float(
                env.get("XDSH_DECISION_TIMEOUT_SECONDS", "1.2"),
                "XDSH_DECISION_TIMEOUT_SECONDS",
            ),
            runtime_command=runtime_command,
            plugin_command=plugin_command,
            model_provider_aliases=model_provider_aliases,
            settings_path=(
                Path(env["XDSH_SETTINGS_PATH"])
                if env.get("XDSH_SETTINGS_PATH", "").strip()
                else None
            ),
            proxy_url=env.get("XDSH_TELEGRAM_PROXY_URL", "").strip() or None,
            api_base_url=env.get("XDSH_TELEGRAM_API_BASE_URL", "https://api.telegram.org"),
            provider={"litellm-anthropic": "litellm"}.get(
                env.get("XDSH_DSH_PROVIDER", "litellm"),
                env.get("XDSH_DSH_PROVIDER", "litellm"),
            ),
            system_prompt=env.get(
                "XDSH_SYSTEM_PROMPT",
                "You are a software engineering agent working through XDSH.",
            ),
            default_language=_parse_language(env.get("XDSH_DEFAULT_LANGUAGE", "zh-TW")),
            poll_timeout_seconds=_parse_int(env, "XDSH_TELEGRAM_POLL_TIMEOUT", 15),
            progress_update_seconds=_parse_float(
                env.get("XDSH_TELEGRAM_PROGRESS_INTERVAL_SECONDS", "4"),
                "XDSH_TELEGRAM_PROGRESS_INTERVAL_SECONDS",
            ),
            progress_heartbeat_seconds=_parse_float(
                env.get("XDSH_TELEGRAM_PROGRESS_HEARTBEAT_SECONDS", "4"),
                "XDSH_TELEGRAM_PROGRESS_HEARTBEAT_SECONDS",
            ),
            task_timeout_seconds=(
                _parse_float(task_timeout_text, "XDSH_TASK_TIMEOUT_SECONDS")
                if task_timeout_text
                else None
            ),
            dsh_cold_start_timeout_seconds=_parse_float(
                env.get("XDSH_DSH_COLD_START_TIMEOUT_SECONDS", "90"),
                "XDSH_DSH_COLD_START_TIMEOUT_SECONDS",
            ),
            dsh_cold_start_retries=_parse_int(
                env,
                "XDSH_DSH_COLD_START_RETRIES",
                1,
            ),
            dsh_stream_idle_timeout_seconds=_parse_float(
                env.get("XDSH_DSH_STREAM_IDLE_TIMEOUT_SECONDS", "120"),
                "XDSH_DSH_STREAM_IDLE_TIMEOUT_SECONDS",
            ),
            max_answer_chars=_parse_int(env, "XDSH_TELEGRAM_MAX_ANSWER_CHARS", 200_000),
            max_thinking_chars=_parse_int(
                env,
                "XDSH_TELEGRAM_MAX_THINKING_CHARS",
                20_000,
            ),
            max_attachment_bytes=_parse_int(
                env,
                "XDSH_TELEGRAM_MAX_ATTACHMENT_BYTES",
                10_000_000,
            ),
            max_parallel_tool_calls=_parse_int(
                env,
                "XDSH_MAX_PARALLEL_TOOL_CALLS",
                DEFAULT_MAX_PARALLEL_TOOL_CALLS,
            ),
            context_window_tokens=_parse_int(
                env,
                "XDSH_CONTEXT_WINDOW_TOKENS",
                262_144,
            ),
            auto_compact_enabled=_parse_bool(
                env.get("XDSH_AUTO_COMPACT_ENABLED", "1"),
                "XDSH_AUTO_COMPACT_ENABLED",
            ),
            native_compaction_enabled=_parse_bool(
                env.get("XDSH_NATIVE_COMPACTION_ENABLED", "1"),
                "XDSH_NATIVE_COMPACTION_ENABLED",
            ),
            auto_compact_warning_ratio=_parse_float(
                env.get("XDSH_AUTO_COMPACT_WARNING_RATIO", "0.20"),
                "XDSH_AUTO_COMPACT_WARNING_RATIO",
            ),
            auto_compact_threshold_ratio=_parse_float(
                env.get("XDSH_AUTO_COMPACT_THRESHOLD_RATIO", "0.50"),
                "XDSH_AUTO_COMPACT_THRESHOLD_RATIO",
            ),
            auto_compact_target_ratio=_parse_float(
                env.get("XDSH_AUTO_COMPACT_TARGET_RATIO", "0.20"),
                "XDSH_AUTO_COMPACT_TARGET_RATIO",
            ),
            auto_compact_retain_ratio=_parse_float(
                env.get("XDSH_AUTO_COMPACT_RETAIN_RATIO", "0.08"),
                "XDSH_AUTO_COMPACT_RETAIN_RATIO",
            ),
            auto_compact_threshold_tokens=_optional_int(
                env,
                "XDSH_AUTO_COMPACT_THRESHOLD_TOKENS",
                default=64_000,
            ),
            auto_compact_min_turns=_parse_int(
                env,
                "XDSH_AUTO_COMPACT_MIN_TURNS",
                1,
            ),
            auto_compact_cooldown_turns=_parse_int(
                env,
                "XDSH_AUTO_COMPACT_COOLDOWN_TURNS",
                1,
            ),
            model_probe_timeout_seconds=_parse_float(
                env.get("XDSH_MODEL_PROBE_TIMEOUT_SECONDS", "20"),
                "XDSH_MODEL_PROBE_TIMEOUT_SECONDS",
            ),
            model_health_ttl_seconds=_parse_float(
                env.get("XDSH_MODEL_HEALTH_TTL_SECONDS", "900"),
                "XDSH_MODEL_HEALTH_TTL_SECONDS",
            ),
            learning_review_model=(
                env.get("XDSH_LEARNING_REVIEW_MODEL", "").strip() or None
            ),
            learning_review_interval=_parse_int(
                env,
                "XDSH_LEARNING_REVIEW_INTERVAL",
                10,
            ),
            skill_review_interval=_parse_int(
                env,
                "XDSH_SKILL_REVIEW_INTERVAL",
                15,
            ),
            learning_review_timeout_seconds=_parse_float(
                env.get("XDSH_LEARNING_REVIEW_TIMEOUT_SECONDS", "45"),
                "XDSH_LEARNING_REVIEW_TIMEOUT_SECONDS",
            ),
            scheduler_timezone=env.get("XDSH_TIMEZONE", "Asia/Shanghai").strip(),
            max_background_jobs=_parse_int(
                env,
                "XDSH_MAX_BACKGROUND_JOBS",
                3,
            ),
            job_poll_seconds=_parse_float(
                env.get("XDSH_JOB_POLL_SECONDS", "15"),
                "XDSH_JOB_POLL_SECONDS",
            ),
            run_guard_policy=_run_guard_policy(env),
            rate_limit_policy=_rate_limit_policy(env),
        )


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
    if not value:
        raise TelegramConfigurationError(f"required environment variable is unset: {name}")
    return value


def _parse_language(value: str) -> str:
    try:
        return canonical_language(value)
    except ValueError as error:
        raise TelegramConfigurationError("XDSH_DEFAULT_LANGUAGE is unsupported") from error


def _parse_allowed_users(value: str) -> frozenset[int]:
    if not value.strip():
        return frozenset()
    users: set[int] = set()
    for item in value.split(","):
        try:
            user_id = int(item.strip())
        except ValueError as error:
            raise TelegramConfigurationError(
                "XDSH_TELEGRAM_ALLOWED_USERS must contain comma-separated integers"
            ) from error
        if user_id <= 0:
            raise TelegramConfigurationError("Telegram allowed user IDs must be positive")
        users.add(user_id)
    return frozenset(users)


def _parse_int(environment: Mapping[str, str], name: str, default: int) -> int:
    raw = environment.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as error:
        raise TelegramConfigurationError(f"{name} must be an integer") from error


def _optional_int(
    environment: Mapping[str, str],
    name: str,
    *,
    default: int | None = None,
) -> int | None:
    raw_value = environment.get(name)
    if raw_value is None:
        return default
    raw = raw_value.strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as error:
        raise TelegramConfigurationError(f"{name} must be an integer") from error


def _parse_float(value: str, name: str) -> float:
    try:
        return float(value)
    except ValueError as error:
        raise TelegramConfigurationError(f"{name} must be a number") from error


def _parse_decision_mode(value: str) -> DecisionMode:
    normalized = value.strip().casefold()
    if normalized == "rules":
        return "rules"
    if normalized == "shadow":
        return "shadow"
    if normalized == "active":
        return "active"
    if normalized == "integrated":
        return "integrated"
    raise TelegramConfigurationError(
        "XDSH_DECISION_MODE must be rules, shadow, active, or integrated"
    )


def _optional_guard_runtime(environment: Mapping[str, str]) -> float | None:
    raw = environment.get("XDSH_GUARD_MAX_RUNTIME_SECONDS", "").strip()
    if raw.casefold() in {"", "0", "off", "none", "unlimited"}:
        return None
    return _parse_float(raw, "XDSH_GUARD_MAX_RUNTIME_SECONDS")


def _parse_bool(value: str, name: str) -> bool:
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise TelegramConfigurationError(f"{name} must be a boolean")


def _run_guard_policy(environment: Mapping[str, str]) -> RunGuardPolicy:
    def resource_limit(name: str, default: int) -> int | None:
        if environment.get(name, "").strip().casefold() == "unlimited":
            return None
        return _parse_int(environment, name, default)

    try:
        return RunGuardPolicy(
            max_model_steps=resource_limit(
                "XDSH_GUARD_MAX_MODEL_STEPS",
                120,
            ),
            max_tool_calls=resource_limit(
                "XDSH_GUARD_MAX_TOOL_CALLS",
                160,
            ),
            max_same_tool_calls=_parse_int(
                environment,
                "XDSH_GUARD_MAX_REPEATED_TOOL_CALLS",
                8,
            ),
            max_stagnant_poll_calls=_parse_int(
                environment,
                "XDSH_GUARD_MAX_STAGNANT_POLL_CALLS",
                4,
            ),
            max_consecutive_tool_errors=_parse_int(
                environment,
                "XDSH_GUARD_MAX_CONSECUTIVE_TOOL_ERRORS",
                8,
            ),
            max_same_tool_errors=_parse_int(
                environment, "XDSH_GUARD_MAX_REPEATED_TOOL_ERRORS", 3,
            ),
            max_retries_without_progress=_parse_int(
                environment,
                "XDSH_GUARD_MAX_RETRIES_WITHOUT_PROGRESS",
                6,
            ),
            max_transient_retries=_parse_int(environment, "XDSH_GUARD_MAX_TRANSIENT_RETRIES", 2),
            max_total_tokens=resource_limit(
                "XDSH_GUARD_MAX_TOTAL_TOKENS",
                2_000_000,
            ),
            max_parallel_subagents=resource_limit(
                "XDSH_GUARD_MAX_PARALLEL_SUBAGENTS",
                3,
            ),
            stall_timeout_seconds=_parse_float(
                environment.get("XDSH_GUARD_STALL_SECONDS", "600"),
                "XDSH_GUARD_STALL_SECONDS",
            ),
            active_tool_stall_timeout_seconds=_parse_float(
                environment.get("XDSH_GUARD_ACTIVE_TOOL_STALL_SECONDS", "1800"),
                "XDSH_GUARD_ACTIVE_TOOL_STALL_SECONDS",
            ),
            max_runtime_seconds=_optional_guard_runtime(environment),
            warning_ratio=_parse_float(
                environment.get("XDSH_GUARD_WARNING_RATIO", "0.8"),
                "XDSH_GUARD_WARNING_RATIO",
            ),
            cancel_timeout_seconds=_parse_float(
                environment.get("XDSH_GUARD_CANCEL_TIMEOUT_SECONDS", "10"),
                "XDSH_GUARD_CANCEL_TIMEOUT_SECONDS",
            ),
        )
    except ValueError as error:
        raise TelegramConfigurationError(f"invalid task-protection policy: {error}") from error


def _rate_limit_policy(environment: Mapping[str, str]) -> RateLimitPolicy:
    try:
        return rate_limit_policy_from_env(environment)
    except ValueError as error:
        raise TelegramConfigurationError(f"invalid upstream rate-limit policy: {error}") from error


def _safe_root(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if resolved == Path(resolved.anchor):
        raise TelegramConfigurationError(f"{label} must not be the filesystem root")
    if ".xclaude" in resolved.parts or ".dsh" in resolved.parts:
        raise TelegramConfigurationError(
            f"{label} must be isolated from reserved legacy and default DSH roots"
        )
    return resolved


def _validate_telegram_api_url(url: str) -> None:
    parsed = urlsplit(url)
    loopback_hosts = {"127.0.0.1", "::1", "localhost"}
    try:
        port = parsed.port
    except ValueError as error:
        raise TelegramConfigurationError("Telegram API URL has an invalid port") from error
    safe_authority = (
        parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and parsed.path in {"", "/"}
    )
    if (
        safe_authority
        and parsed.scheme == "https"
        and parsed.hostname == "api.telegram.org"
        and port in {None, 443}
    ):
        return
    if safe_authority and parsed.scheme == "http" and parsed.hostname in loopback_hosts:
        return
    raise TelegramConfigurationError(
        "Telegram API must use the official HTTPS endpoint or an HTTP loopback URL"
    )


def _validate_litellm_url(url: str) -> None:
    parsed = urlsplit(url)
    try:
        _port = parsed.port
    except ValueError as error:
        raise TelegramConfigurationError("LiteLLM gateway URL has an invalid port") from error
    loopback = parsed.scheme in {"http", "https"} and parsed.hostname in {
        "127.0.0.1",
        "::1",
        "localhost",
    }
    if not loopback:
        raise TelegramConfigurationError("LiteLLM gateway must use an HTTP loopback URL")
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise TelegramConfigurationError(
            "LiteLLM gateway must use a credential-free loopback root URL"
        )


def _validate_proxy_url(url: str) -> None:
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError as error:
        raise TelegramConfigurationError("Telegram proxy URL has an invalid port") from error
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise TelegramConfigurationError(
            "Telegram proxy must be an unauthenticated HTTP loopback URL with an explicit port"
        )
