"""External DeepSeek Harness runner over the official Python SDK/JSON-RPC API."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import math
import os
import re
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeAlias, cast
from urllib.parse import urlsplit

from xdsh.core.deferred_results import current_deferred_context
from xdsh.core.errors import (
    RunnerClosedError,
    RunnerConfigurationError,
    SessionBusyError,
    SessionConfigurationError,
)
from xdsh.core.events import (
    ErrorEvent,
    FinishEvent,
    JsonValue,
    RetryEvent,
    RunnerEventT,
    RuntimeEvent,
)
from xdsh.core.evidence import with_evidence_decision_gate
from xdsh.core.execution_policy import (
    DEFAULT_MAX_PARALLEL_TOOL_CALLS,
    validate_parallel_tool_limit,
    with_direct_execution_policy,
    with_optional_skill_routing_policy,
)
from xdsh.core.models import LiteLlmRouteVerifier, ModelAvailability, ModelRouteVerifier
from xdsh.core.product_control import safe_exception_text
from xdsh.core.rate_limit import RateLimitPolicy, classify_exception
from xdsh.core.runner import REASONING_EFFORTS, ReasoningEffort, RunnerCapabilities
from xdsh.core.skills import ensure_builtin_skills
from xdsh.runners.compat import DshSdkCompatibilityError
from xdsh.runners.dsh_deferred import DshDeferredObserver
from xdsh.runners.dsh_events import DshEventNormalizer
from xdsh.runners.runtime import (
    ALPHA2_DSH_NPM_VERSION,
    LEGACY_DSH_NPM_VERSION,
    RuntimeMode,
    classify_runtime_command,
    validate_packaged_release_runtime,
)
from xdsh.runners.sdk import (
    DshRuntimeInitializationError,
    Harness,
    HarnessFactory,
    HarnessLaunch,
    HarnessRunResult,
    create_official_harness,
)
from xdsh.version import DSH_PYTHON_SDK_VERSION as _PINNED_DSH_PYTHON_SDK_VERSION

# Kept as the legacy compatibility alias for callers that imported the rc.2
# marker.  The active runtime package is derived from its explicit argv below.
DSH_NPM_VERSION = LEGACY_DSH_NPM_VERSION
DSH_PYTHON_SDK_VERSION = _PINNED_DSH_PYTHON_SDK_VERSION

_DEFAULT_CORDIS = Path(__file__).with_name("anthropic_litellm.cordis.yml")
_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}$")
_CORDIS_PLUGIN_NAME = re.compile(
    r"^\s*name:\s*['\"]?(?P<name>@deepseek-ai/[A-Za-z0-9._/-]+)['\"]?\s*$",
    re.MULTILINE,
)
_CORDIS_ROW_START = re.compile(r"^(?:- id:|- name:)", re.MULTILINE)
_CORDIS_OPTION = re.compile(
    r"^\s*(?P<key>disabled|auto):\s*(?P<value>.+?)\s*$",
    re.MULTILINE,
)
_COMPACTION_PLUGIN = "@deepseek-ai/dsh-compaction-basic"
_TOKEN_METER_PLUGIN = "@deepseek-ai/dsh-token-meter"
_TOOL_RESULT_PRUNER_PLUGIN = "@deepseek-ai/dsh-compaction-tool-result-pruner"
_COMPACTION_AUTO_EXPRESSION = (
    "process.env.XDSH_AUTO_COMPACT_ENABLED === '1' && "
    "process.env.XDSH_NATIVE_COMPACTION_ENABLED === '1'"
)


@dataclass(frozen=True, slots=True, kw_only=True)
class DshRunnerConfig:
    """Immutable process-level DSH configuration."""

    dsh_home: Path
    model: str
    provider: str = "litellm"
    cordis: Path = _DEFAULT_CORDIS
    runtime_command: tuple[str, ...] = ()
    max_tokens: int | None = None
    max_parallel_tool_calls: int = DEFAULT_MAX_PARALLEL_TOOL_CALLS
    context_window: int = 262_144
    model_max_tokens: int = 32_768
    auto_compaction_enabled: bool = True
    native_compaction_enabled: bool = True
    auto_compaction_threshold_ratio: float = 0.50
    auto_compaction_retain_ratio: float = 0.08
    auto_compaction_threshold_tokens: int | None = 64_000
    default_system_prompt: str = "You are a software engineering agent working through XDSH."
    request_timeout_seconds: float | None = None
    # Per-event silence, NOT total task time. Official RC2 can recover a failed
    # model step before its tools execute; the outer guard bounds attempts.
    # Dedicated local-model endurance launchers can use a longer idle window.
    stream_idle_timeout_seconds: float = 120.0
    cold_start_timeout_seconds: float = 90.0
    cold_start_retries: int = 1
    shutdown_timeout_seconds: float = 1.0
    rate_limit_policy: RateLimitPolicy = field(default_factory=RateLimitPolicy)
    litellm_base_url_env: str = "XDSH_LITELLM_BASE_URL"
    litellm_api_key_env: str = "XDSH_LITELLM_API_KEY"

    def __post_init__(self) -> None:
        home = self.dsh_home.expanduser().resolve()
        if ".xclaude" in home.parts or ".dsh" in home.parts:
            raise RunnerConfigurationError(
                "dsh_home must be isolated from reserved legacy and default DSH roots"
            )
        if not self.model.strip() or not self.provider.strip():
            raise RunnerConfigurationError("provider and model must be non-empty")
        if self.max_tokens is not None and self.max_tokens <= 0:
            raise RunnerConfigurationError("max_tokens must be positive when set")
        try:
            validate_parallel_tool_limit(self.max_parallel_tool_calls)
        except ValueError as error:
            raise RunnerConfigurationError(str(error)) from error
        if self.context_window <= 0 or self.model_max_tokens <= 0:
            raise RunnerConfigurationError("model token capacities must be positive")
        if self.model_max_tokens >= self.context_window:
            raise RunnerConfigurationError(
                "model_max_tokens must be below the configured context_window"
            )
        if not math.isfinite(self.auto_compaction_threshold_ratio) or not (
            0.30 <= self.auto_compaction_threshold_ratio < 0.95
        ):
            raise RunnerConfigurationError(
                "auto_compaction_threshold_ratio must be between 0.30 and 0.95"
            )
        if not math.isfinite(self.auto_compaction_retain_ratio) or not (
            0.05 <= self.auto_compaction_retain_ratio
            < self.auto_compaction_threshold_ratio
        ):
            raise RunnerConfigurationError(
                "auto_compaction_retain_ratio must be at least 0.05 and below the threshold"
            )
        if self.auto_compaction_threshold_tokens is not None:
            if self.auto_compaction_threshold_tokens <= 0:
                raise RunnerConfigurationError(
                    "auto_compaction_threshold_tokens must be positive when set"
                )
        if self.cold_start_timeout_seconds <= 0:
            raise RunnerConfigurationError("cold_start_timeout_seconds must be positive")
        if not math.isfinite(self.stream_idle_timeout_seconds) or not (
            30.0 <= self.stream_idle_timeout_seconds <= 3_600.0
        ):
            raise RunnerConfigurationError(
                "stream_idle_timeout_seconds must be finite and between 30 and 3600"
            )
        if not 0 <= self.cold_start_retries <= 3:
            raise RunnerConfigurationError("cold_start_retries must be between 0 and 3")
        if not self.cordis.expanduser().resolve().is_file():
            raise RunnerConfigurationError(f"Cordis config not found: {self.cordis}")
        if not self.runtime_command:
            raise RunnerConfigurationError(
                "explicit runtime_command is required; bundled preview runtime fallback is disabled"
            )
        if any(not argument for argument in self.runtime_command):
            raise RunnerConfigurationError("each runtime_command argument must be non-empty")
        try:
            validate_packaged_release_runtime(self.runtime_command)
        except ValueError as error:
            raise RunnerConfigurationError(str(error)) from error

    @property
    def effective_auto_compaction_threshold_ratio(self) -> float:
        """Resolve an optional absolute cap into the native ratio setting."""

        if self.auto_compaction_threshold_tokens is None:
            return self.auto_compaction_threshold_ratio
        return min(
            self.auto_compaction_threshold_ratio,
            self.auto_compaction_threshold_tokens / self.context_window,
        )

    @property
    def effective_auto_compaction_retain_ratio(self) -> float:
        """Keep at least half of an absolute pressure budget for summary/header."""

        return min(
            self.auto_compaction_retain_ratio,
            self.effective_auto_compaction_threshold_ratio / 2,
        )


@dataclass(frozen=True, slots=True)
class _RuntimeKey:
    dsh_session_id: str
    cwd: Path
    model: str
    reasoning_effort: ReasoningEffort
    system_prompt: str
    # A profile package/lockfile fingerprint makes an independently managed
    # ``dsh plugin`` change visible to the next turn without requiring a
    # process restart or an XDSH-specific install hook.
    plugin_fingerprint: str = "none"


@dataclass(slots=True)
class _SessionRuntime:
    key: _RuntimeKey
    harness: Harness
    runtime_generation: str
    worker: asyncio.Task[None] | None = None
    cancel_requested: bool = False
    deferred: DshDeferredObserver | None = None


@dataclass(frozen=True, slots=True)
class _RuntimeSelection:
    runtime: _SessionRuntime
    cold: bool


class _DshHomeLock:
    """Own one isolated DSH_HOME across XDSH processes."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._descriptor: int | None = None

    def acquire(self) -> None:
        if self._descriptor is not None:
            return
        descriptor = os.open(
            self._path,
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            raise RunnerConfigurationError(
                "another XDSH DSH runner owns this isolated DSH_HOME"
            ) from None
        self._descriptor = descriptor

    def release(self) -> None:
        descriptor = self._descriptor
        if descriptor is None:
            return
        self._descriptor = None
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


@dataclass(frozen=True, slots=True)
class _NotificationItem:
    notification: object
    runtime_generation: str


@dataclass(frozen=True, slots=True)
class _ResultItem:
    result: HarnessRunResult


@dataclass(frozen=True, slots=True)
class _FailureItem:
    error: BaseException


_WorkerItem: TypeAlias = _NotificationItem | _ResultItem | _FailureItem


class _ColdStartTimeout(TimeoutError):
    """A fresh DSH process could not be constructed before its deadline."""


class DshRunner:
    """Own one reusable SDK runtime per product session.

    The official DSH JSON-RPC protocol currently has no cancel method or
    external compact request. Keeping one runtime per product session makes
    process closure a session-scoped, reversible cancel operation. Both the
    legacy composition and the alpha ``sdk`` profile expose native compaction
    at safe step boundaries. A fresh process gets only the caller's bounded
    compatibility resume context because the SDK protocol cannot reopen its
    durable JSONL session.
    """

    def __init__(
        self,
        config: DshRunnerConfig,
        *,
        harness_factory: HarnessFactory = create_official_harness,
        route_verifier: ModelRouteVerifier | None = None,
    ) -> None:
        self.config = config
        self._harness_factory = harness_factory
        self._route_verifier = route_verifier or LiteLlmRouteVerifier(
            base_url_env=config.litellm_base_url_env,
            api_key_env=config.litellm_api_key_env,
        )
        self._dsh_home = config.dsh_home.expanduser().resolve()
        self._session_root = self._dsh_home / "sessions"
        self._session_map_path = self._dsh_home / "xdsh-session-generations.json"
        self._home_instance_lock = _DshHomeLock(self._dsh_home / ".xdsh-runner.lock")
        # DSH rc.2 has no connection/generation fencing field.  This random
        # process marker is XDSH-owned provenance only; it never crosses the
        # official JSON-RPC boundary.
        self._host_runtime_id = uuid.uuid4().hex
        self._runtimes: dict[str, _SessionRuntime] = {}
        # Keep the last launched runtime's marker after cancellation so a
        # checkpoint still identifies the execution that produced its state.
        self._runtime_markers: dict[str, str] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._session_cwds: dict[str, Path] = {}
        self._generations: dict[str, int] = {}
        self._generations_loaded = False
        self._state_lock = asyncio.Lock()
        self._home_lock = asyncio.Lock()
        self._generation_write_lock = asyncio.Lock()
        self._cleanup_tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    @property
    def capabilities(self) -> RunnerCapabilities:
        """Declare capabilities proven for the selected official runtime shape."""

        native = {
            "prompt",
            "structured_events",
            "multi_turn_process_local",
            "shutdown",
        }
        if any(runtime.deferred is not None for runtime in self._runtimes.values()):
            native.add("deferred_reports")
        runtime_spec = classify_runtime_command(self.config.runtime_command)
        mounted_plugins = _cordis_plugin_names(self.config.cordis)
        profile_has_native_compaction = (
            runtime_spec.mode is RuntimeMode.PROFILE
            and runtime_spec.profile == "sdk"
        )
        native_compaction = self.config.auto_compaction_enabled and (
            self.config.native_compaction_enabled
            and (
                profile_has_native_compaction
                or (
                    _COMPACTION_PLUGIN in mounted_plugins
                    and _TOKEN_METER_PLUGIN in mounted_plugins
                )
            )
        )
        if native_compaction:
            native.add("automatic_compaction")
            if profile_has_native_compaction or _TOOL_RESULT_PRUNER_PLUGIN in mounted_plugins:
                native.add("tool_result_pruning")
        runtime_package = runtime_spec.version
        if runtime_package is None and runtime_spec.mode is RuntimeMode.PROFILE:
            # A PATH-only ``dsh --profile sdk`` wrapper has no adjacent
            # package.json.  Keep the capability label useful while the
            # doctor command performs the authoritative version probe.
            runtime_package = f"{ALPHA2_DSH_NPM_VERSION} (profile)"
        if runtime_package is None:
            # Do not claim rc.2 for an arbitrary test/custom launcher.  The
            # Telegram doctor retains its historical rc.2 display only for
            # transparent test doubles that report ``unknown`` metadata.
            runtime_package = "unknown"
        return RunnerCapabilities(
            protocol="sdk-jsonrpc-stdio",
            runtime_package=runtime_package,
            runtime_identity="deepseek-harness-sdk-runtime",
            native=frozenset(native),
            unsupported_native=frozenset(
                {
                    "prompt_cancel",
                    "session_resume",
                    "compact",
                    "feedback",
                    "goal",
                    "plan",
                    "permission",
                    "export",
                }
            ),
            source="forward_compatible_adapter",
        )

    async def run(
        self,
        task: str,
        *,
        session_id: str,
        cwd: Path,
        model: str | None = None,
        reasoning_effort: ReasoningEffort = "off",
        fallback: str | None = None,
        system_prompt: str | None = None,
        resume_context: str | None = None,
    ) -> AsyncIterator[RunnerEventT]:
        """Run one turn and stream only normalized DSH notifications."""

        del fallback  # Fallback policy belongs to the runner-neutral outer shell.
        normalized_session_id = _validate_session_id(session_id)
        if not task.strip():
            raise RunnerConfigurationError("task must be non-empty")
        if resume_context is not None and len(resume_context) > 64_000:
            raise RunnerConfigurationError("resume_context must not exceed 64000 characters")
        resolved_cwd = cwd.expanduser().resolve()
        if not resolved_cwd.is_dir():
            raise RunnerConfigurationError(f"cwd is not a directory: {resolved_cwd}")
        selected_model = (model or self.config.model).strip()
        if not selected_model:
            raise RunnerConfigurationError("model must be non-empty")
        selected_reasoning_effort = _validate_reasoning_effort(reasoning_effort)
        selected_system_prompt = with_optional_skill_routing_policy(
            with_direct_execution_policy(system_prompt or self.config.default_system_prompt)
        )
        self._validate_gateway_environment()

        session_lock = await self._lock_for(normalized_session_id)
        if session_lock.locked():
            raise SessionBusyError(f"session {normalized_session_id!r} already has an active task")
        await session_lock.acquire()
        try:
            routing = await self._route_verifier.check(selected_model)
            if routing is not None and routing.availability is ModelAvailability.UNAVAILABLE:
                yield ErrorEvent(
                    session_id=normalized_session_id,
                    code="MODEL_ROUTE_UNVERIFIED", retryable=False,
                    message=(
                        "Selected model has no explicit gateway route; "
                        "wildcard substitution refused."
                    ),
                )
                yield FinishEvent(
                    session_id=normalized_session_id, reason="route_unavailable",
                    final_text=(
                        "The selected model has no explicit gateway route. "
                        "The task was not started because the gateway would substitute "
                        "another model. Configure this exact model route or use /model "
                        "to select a configured route."
                    ),
                )
                return
            cold_attempt = 0
            while True:
                startup_started_at = asyncio.get_running_loop().time()
                try:
                    selection = await self._runtime_for(
                        session_id=normalized_session_id,
                        cwd=resolved_cwd,
                        model=selected_model,
                        reasoning_effort=selected_reasoning_effort,
                        system_prompt=selected_system_prompt,
                    )
                except DshRuntimeInitializationError:
                    yield ErrorEvent(
                        session_id=normalized_session_id,
                        message="DSH runtime initialization failed; SDK mismatch not established",
                        code="DSH_RUNTIME_INITIALIZATION_FAILED",
                        retryable=False,
                    )
                    yield FinishEvent(session_id=normalized_session_id, reason="error")
                    break
                except DshSdkCompatibilityError:
                    yield ErrorEvent(
                        session_id=normalized_session_id,
                        message="installed DSH SDK is incompatible with the XDSH adapter",
                        code="DSH_SDK_INCOMPATIBLE",
                        retryable=False,
                    )
                    yield FinishEvent(session_id=normalized_session_id, reason="error")
                    break
                except _ColdStartTimeout:
                    if cold_attempt < self.config.cold_start_retries:
                        cold_attempt += 1
                        yield RetryEvent(
                            session_id=normalized_session_id,
                            provider="dsh-cold-start",
                        )
                        continue
                    yield ErrorEvent(
                        session_id=normalized_session_id,
                        message="DSH runtime initialization timed out",
                        code="DSH_COLD_START_TIMEOUT",
                        retryable=False,
                    )
                    yield FinishEvent(session_id=normalized_session_id, reason="error")
                    break

                runtime_task = with_evidence_decision_gate(task)
                if selection.cold and resume_context:
                    runtime_task = (
                        f"{resume_context}\n\n"
                        "End of XDSH compatibility resume context.\n\n"
                        f"Current user request:\n{runtime_task}"
                    )
                stream = self._run_with_runtime(
                    selection.runtime,
                    task=runtime_task,
                    session_id=normalized_session_id,
                )
                if selection.cold:
                    yield RuntimeEvent(
                        session_id=normalized_session_id,
                        runtime_generation=selection.runtime.runtime_generation,
                        cold=True,
                        startup_ms=max(
                            0,
                            round(
                                (asyncio.get_running_loop().time() - startup_started_at)
                                * 1000
                            ),
                        ),
                        attempt=cold_attempt + 1,
                    )
                try:
                    async for event in stream:
                        yield event
                finally:
                    # Outer guards may stop reading exactly at FinishEvent.
                    # Finish this worker before releasing the session lock;
                    # a GC-delayed finalizer must not clear a subsequent worker.
                    await stream.aclose()
                break
        finally:
            session_lock.release()

    async def cancel(self, session_id: str) -> bool:
        """Cancel only the named session by closing its SDK runtime process."""

        normalized_session_id = _validate_session_id(session_id)
        async with self._state_lock:
            runtime = self._runtimes.get(normalized_session_id)
            if runtime is None or (
                runtime.deferred is None and (runtime.worker is None or runtime.worker.done())
            ):
                return False
            runtime.cancel_requested = True
            self._runtimes.pop(normalized_session_id, None)
        if runtime.deferred is not None:
            runtime.deferred.close()
        await asyncio.to_thread(runtime.harness.close)
        return True

    async def runtime_generation(self, session_id: str) -> str:
        """Return the current XDSH-owned host/generation marker.

        The marker is intentionally opaque and separate from DSH's
        ``sessionId``.  It lets durable product checkpoints identify their
        source process after a restart while remaining honest about RC2's lack
        of native cross-process resume or cancellation.
        """

        normalized_session_id = _validate_session_id(session_id)
        await self._ensure_home()
        async with self._state_lock:
            if self._closed:
                raise RunnerClosedError("DshRunner is closed")
            generation = self._generations.get(normalized_session_id, 0)
            marker = self._runtime_markers.get(normalized_session_id)
        if marker is not None:
            return marker
        return f"{self._host_runtime_id}:{generation}"

    async def reset(self, session_id: str) -> None:
        """Route future turns to a fresh internal DSH session without deleting logs."""

        normalized_session_id = _validate_session_id(session_id)
        await self.cancel(normalized_session_id)
        session_lock = await self._lock_for(normalized_session_id)
        async with session_lock:
            async with self._state_lock:
                runtime = self._runtimes.pop(normalized_session_id, None)
            if runtime is not None:
                if runtime.deferred is not None:
                    runtime.deferred.close()
                await asyncio.to_thread(runtime.harness.close)
            await self._ensure_home()
            async with self._state_lock:
                self._generations[normalized_session_id] = (
                    self._generations.get(normalized_session_id, 0) + 1
                )
                self._runtime_markers.pop(normalized_session_id, None)
                self._session_cwds.pop(normalized_session_id, None)
            await self._persist_generations()

    async def close(self) -> None:
        """Close every XDSH-owned runtime; idempotent."""

        async with self._state_lock:
            if self._closed:
                return
            self._closed = True
            runtimes = list(self._runtimes.values())
            self._runtimes.clear()
            for runtime in runtimes:
                runtime.cancel_requested = runtime.worker is not None and not runtime.worker.done()
                if runtime.deferred is not None:
                    runtime.deferred.close(persist_received=True)
        await asyncio.gather(
            *(asyncio.to_thread(runtime.harness.close) for runtime in runtimes),
            return_exceptions=True,
        )
        await asyncio.to_thread(self._home_instance_lock.release)

    async def _run_with_runtime(
        self,
        runtime: _SessionRuntime,
        *,
        task: str,
        session_id: str,
    ) -> AsyncGenerator[RunnerEventT, None]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[_WorkerItem] = asyncio.Queue()
        normalizer = DshEventNormalizer(
            session_id=session_id,
            dsh_session_id=runtime.key.dsh_session_id,
            runtime_generation=runtime.runtime_generation,
        )
        pending_errors: list[ErrorEvent] = []
        notification_seen = False
        context = current_deferred_context()
        if context is not None and runtime.deferred is None:
            subscribe = getattr(runtime.harness, "subscribe_session_notifications", None)
            if callable(subscribe):
                subscription = subscribe(runtime.key.dsh_session_id)
                if subscription is not None:
                    runtime.deferred = DshDeferredObserver(
                        subscription, session_id=session_id,
                        native_session_id=runtime.key.dsh_session_id,
                        runtime_generation=runtime.runtime_generation,
                    )
        if runtime.deferred is not None:
            runtime.deferred.begin(context)

        def enqueue_notification(notification: object) -> None:
            nonlocal notification_seen
            notification_seen = True
            loop.call_soon_threadsafe(
                queue.put_nowait,
                _NotificationItem(
                    notification=notification,
                    runtime_generation=runtime.runtime_generation,
                ),
            )

        def invoke() -> None:
            try:
                result = runtime.harness.run(
                    task,
                    session_id=runtime.key.dsh_session_id,
                    on_notification=enqueue_notification,
                )
            except BaseException as error:
                loop.call_soon_threadsafe(queue.put_nowait, _FailureItem(error))
            else:
                loop.call_soon_threadsafe(queue.put_nowait, _ResultItem(result))

        worker = asyncio.create_task(asyncio.to_thread(invoke), name=f"dsh:{session_id}")
        runtime.worker = worker
        try:
            while True:
                item = await queue.get()
                if isinstance(item, _NotificationItem):
                    # A harness may enqueue a notification concurrently with
                    # close().  Once this runtime has been cancelled, those
                    # late events belong to the retired turn and must not
                    # reach the product event stream or durable checkpoints.
                    # The terminal FinishEvent below remains authoritative.
                    if runtime.cancel_requested:
                        continue
                    if runtime.deferred is not None:
                        runtime.deferred.record_foreground_notification(item.notification)
                    for event in normalizer.feed(
                        item.notification,
                        runtime_generation=item.runtime_generation,
                    ):
                        if isinstance(event, ErrorEvent):
                            pending_errors.append(event)
                        else:
                            yield event
                    continue
                if isinstance(item, _ResultItem):
                    await worker
                    if runtime.deferred is not None:
                        runtime.deferred.foreground_complete()
                    if not notification_seen:
                        for raw_event in item.result.events:
                            notification = _result_event_notification(
                                raw_event,
                                dsh_session_id=runtime.key.dsh_session_id,
                            )
                            for event in normalizer.feed(
                                notification,
                                runtime_generation=runtime.runtime_generation,
                            ):
                                if isinstance(event, ErrorEvent):
                                    pending_errors.append(event)
                                else:
                                    yield event
                    reason = item.result.finish_reason or normalizer.finish_reason or "unknown"
                    if reason == "error":
                        if pending_errors:
                            yield pending_errors[-1]
                        else:
                            yield ErrorEvent(
                                session_id=session_id,
                                runtime_generation=runtime.runtime_generation,
                                message="DSH turn failed without structured error details",
                                code="DSH_TURN_ERROR",
                            )
                    yield FinishEvent(
                        session_id=session_id,
                        runtime_generation=runtime.runtime_generation,
                        reason=reason,
                        final_text=item.result.final_response,
                        cancelled=runtime.cancel_requested or reason == "aborted",
                    )
                    break
                await worker
                if runtime.deferred is not None:
                    runtime.deferred.foreground_complete()
                if runtime.cancel_requested:
                    yield FinishEvent(
                        session_id=session_id,
                        runtime_generation=runtime.runtime_generation,
                        reason=normalizer.finish_reason or "aborted",
                        cancelled=True,
                    )
                else:
                    if isinstance(item.error, DshSdkCompatibilityError):
                        yield ErrorEvent(
                            session_id=session_id,
                            runtime_generation=runtime.runtime_generation,
                            message="installed DSH SDK run surface is incompatible",
                            code="DSH_SDK_INCOMPATIBLE",
                            retryable=False,
                        )
                        yield FinishEvent(
                            session_id=session_id,
                            runtime_generation=runtime.runtime_generation,
                            reason="error",
                        )
                        break
                    rate_limit = classify_exception(item.error)
                    yield ErrorEvent(
                        session_id=session_id,
                        runtime_generation=runtime.runtime_generation,
                        message=(
                            "DSH upstream provider returned HTTP 429 Too Many Requests."
                            if rate_limit is not None
                            else _safe_error_message(item.error)
                        ),
                        code="RATE_LIMIT" if rate_limit is not None else type(item.error).__name__,
                        retryable=rate_limit is not None,
                        status_code=rate_limit.status_code if rate_limit is not None else None,
                        retry_after_seconds=(
                            rate_limit.retry_after_seconds if rate_limit is not None else None
                        ),
                    )
                    yield FinishEvent(
                        session_id=session_id,
                        runtime_generation=runtime.runtime_generation,
                        reason="error",
                    )
                break
        finally:
            if not worker.done():
                await self.cancel(session_id)
                await asyncio.gather(worker, return_exceptions=True)
            if runtime.worker is worker:
                runtime.worker = None

    async def _runtime_for(
        self,
        *,
        session_id: str,
        cwd: Path,
        model: str,
        reasoning_effort: ReasoningEffort,
        system_prompt: str,
    ) -> _RuntimeSelection:
        await self._ensure_home()
        plugin_fingerprint = _profile_plugin_fingerprint(
            self._dsh_home,
            self.config.runtime_command,
        )
        generation_changed = False
        async with self._state_lock:
            if self._closed:
                raise RunnerClosedError("DshRunner is closed")
            bound_cwd = self._session_cwds.get(session_id)
            if bound_cwd is not None and bound_cwd != cwd:
                raise SessionConfigurationError(
                    f"session {session_id!r} is bound to {bound_cwd}, not {cwd}"
                )
            self._session_cwds[session_id] = cwd
            current = self._runtimes.get(session_id)
            if current is not None:
                desired_key = _RuntimeKey(
                    dsh_session_id=current.key.dsh_session_id,
                    cwd=cwd,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    system_prompt=system_prompt,
                    plugin_fingerprint=plugin_fingerprint,
                )
                if current.key == desired_key:
                    return _RuntimeSelection(current, cold=False)
                self._runtimes.pop(session_id, None)
            generation = self._generations.get(session_id, 0)
            dsh_session_id = _generation_session_id(session_id, generation)
            while self._session_log_exists(dsh_session_id):
                generation += 1
                dsh_session_id = _generation_session_id(session_id, generation)
            if generation != self._generations.get(session_id, 0):
                self._generations[session_id] = generation
                generation_changed = True
            key = _RuntimeKey(
                dsh_session_id=dsh_session_id,
                cwd=cwd,
                model=model,
                reasoning_effort=reasoning_effort,
                system_prompt=system_prompt,
                plugin_fingerprint=plugin_fingerprint,
            )
        if generation_changed:
            await self._persist_generations()
        if current is not None:
            if current.deferred is not None:
                current.deferred.close(persist_received=True)
            await asyncio.to_thread(current.harness.close)

        launch = HarnessLaunch(
            cwd=cwd,
            dsh_home=self._dsh_home,
            session_root=self._session_root,
            cordis=self.config.cordis.expanduser().resolve(),
            provider=self.config.provider,
            model=model,
            reasoning_effort=reasoning_effort,
            system_prompt=system_prompt,
            context_window=self.config.context_window,
            model_max_tokens=self.config.model_max_tokens,
            max_tokens=self.config.max_tokens,
            max_parallel_tool_calls=self.config.max_parallel_tool_calls,
            runtime_command=self.config.runtime_command,
            request_timeout_seconds=self.config.request_timeout_seconds,
            shutdown_timeout_seconds=self.config.shutdown_timeout_seconds,
            stream_idle_timeout_seconds=self.config.stream_idle_timeout_seconds,
            rate_limit_policy=self.config.rate_limit_policy,
            auto_compaction_enabled=self.config.auto_compaction_enabled,
            native_compaction_enabled=self.config.native_compaction_enabled,
            auto_compaction_threshold_ratio=self.config.effective_auto_compaction_threshold_ratio,
            auto_compaction_retain_ratio=(
                self.config.effective_auto_compaction_retain_ratio
            ),
            auto_compaction_threshold_tokens=self.config.auto_compaction_threshold_tokens,
            litellm_base_url_env=self.config.litellm_base_url_env,
            litellm_api_key_env=self.config.litellm_api_key_env,
        )
        creation = asyncio.create_task(
            asyncio.to_thread(self._harness_factory, launch),
            name=f"dsh-start:{session_id}",
        )
        try:
            harness = await asyncio.wait_for(
                asyncio.shield(creation),
                timeout=self.config.cold_start_timeout_seconds,
            )
        except asyncio.TimeoutError as error:
            creation.add_done_callback(self._close_late_harness)
            raise _ColdStartTimeout from error
        runtime = _SessionRuntime(
            key=key,
            harness=harness,
            # A config/plugin reload can replace a process before it writes
            # any session log. A launch nonce fences even that same-ID case.
            runtime_generation=f"{self._host_runtime_id}:{generation}:{uuid.uuid4().hex}",
        )
        runner_closed = False
        async with self._state_lock:
            if self._closed:
                runner_closed = True
            else:
                self._runtimes[session_id] = runtime
                self._runtime_markers[session_id] = runtime.runtime_generation
        if runner_closed:
            await asyncio.to_thread(runtime.harness.close)
            raise RunnerClosedError("DshRunner is closed")
        return _RuntimeSelection(runtime, cold=True)

    def _close_late_harness(self, creation: asyncio.Task[Harness]) -> None:
        """Close a constructor result that arrived after its startup deadline."""

        try:
            harness = creation.result()
        except BaseException:
            return
        cleanup = asyncio.create_task(asyncio.to_thread(harness.close))
        self._cleanup_tasks.add(cleanup)
        cleanup.add_done_callback(self._cleanup_tasks.discard)

    def _session_log_exists(self, dsh_session_id: str) -> bool:
        """Detect plain or compressed logs that a fresh SDK cannot resume."""

        # rc.2 writes ``session.jsonl``; alpha.2's profile persists the same
        # surface as ``session.jsonl.zstd``.  Match the stable basename rather
        # than one compression suffix so an upgrade cannot reuse a colliding
        # product session ID and turn a valid task into DSH_SESSION_ID_COLLISION.
        return any(
            path.parent.name == dsh_session_id
            and path.is_file()
            and path.name.startswith("session.jsonl")
            for path in self._session_root.rglob("*")
        )

    async def _persist_generations(self) -> None:
        """Serialize snapshots so concurrent sessions cannot lose one another."""

        async with self._generation_write_lock:
            async with self._state_lock:
                generations = dict(self._generations)
            await asyncio.to_thread(self._write_generations, generations)

    async def _lock_for(self, session_id: str) -> asyncio.Lock:
        async with self._state_lock:
            return self._session_locks.setdefault(session_id, asyncio.Lock())

    async def _ensure_home(self) -> None:
        async with self._home_lock:
            async with self._state_lock:
                if self._generations_loaded:
                    return
            try:
                await asyncio.to_thread(self._prepare_home)
                generations = await asyncio.to_thread(self._read_generations)
            except BaseException:
                await asyncio.to_thread(self._home_instance_lock.release)
                raise
            async with self._state_lock:
                self._generations.update(generations)
                self._generations_loaded = True

    def _prepare_home(self) -> None:
        self._dsh_home.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._dsh_home.chmod(0o700)
        self._home_instance_lock.acquire()
        try:
            self._session_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._session_root.chmod(0o700)
            report = ensure_builtin_skills(self._dsh_home)
            if not report.ready:
                names = ", ".join(report.conflicts)
                raise RunnerConfigurationError(
                    f"XDSH built-in Skill projection has user-owned conflicts: {names}"
                )
        except BaseException:
            self._home_instance_lock.release()
            raise

    def _read_generations(self) -> dict[str, int]:
        if not self._session_map_path.exists():
            return {}
        try:
            payload = json.loads(self._session_map_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RunnerConfigurationError("invalid XDSH session generation file") from error
        if not isinstance(payload, dict):
            raise RunnerConfigurationError("XDSH session generation file must be an object")
        generations: dict[str, int] = {}
        for key, value in payload.items():
            if not isinstance(key, str) or not isinstance(value, int) or value < 0:
                raise RunnerConfigurationError("invalid XDSH session generation entry")
            generations[key] = value
        return generations

    def _write_generations(self, generations: dict[str, int]) -> None:
        temporary = self._session_map_path.with_name(
            f".{self._session_map_path.name}.{uuid.uuid4().hex}.tmp"
        )
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(generations, handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._session_map_path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _validate_gateway_environment(self) -> None:
        base_url = os.environ.get(self.config.litellm_base_url_env)
        if not base_url:
            variable = self.config.litellm_base_url_env
            raise RunnerConfigurationError(
                f"required gateway environment variable is unset: {variable}"
            )
        parsed = urlsplit(base_url)
        try:
            _port = parsed.port
        except ValueError as error:
            raise RunnerConfigurationError("LiteLLM gateway URL has an invalid port") from error
        loopback = parsed.scheme in {"http", "https"} and parsed.hostname in {
            "127.0.0.1",
            "::1",
            "localhost",
        }
        if not loopback:
            raise RunnerConfigurationError("LiteLLM gateway must use an HTTP loopback URL")
        if (
            parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise RunnerConfigurationError(
                "LiteLLM gateway must use a credential-free loopback root URL"
            )
        if not os.environ.get(self.config.litellm_api_key_env):
            variable = self.config.litellm_api_key_env
            raise RunnerConfigurationError(
                f"required credential environment variable is unset: {variable}"
            )


def _cordis_plugin_names(cordis: Path) -> frozenset[str]:
    """Read bounded active ``name:`` rows from one Cordis file.

    Capability declarations must describe the composition that will be booted,
    not just the Python-side feature flag. A caller may intentionally provide a
    custom Cordis file; if it omits the official token meter or compaction
    backend, the outer compatibility path remains active. This is a
    fail-closed, credential-free line-level check; the runtime still validates
    YAML and plugin schemas at boot.
    """

    try:
        text = cordis.expanduser().resolve().read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return frozenset()
    if len(text) > 1_000_000:
        return frozenset()
    names: set[str] = set()
    rows = _split_cordis_rows(text)
    for row in rows:
        match = _CORDIS_PLUGIN_NAME.search(row)
        if match is None:
            continue
        name = match.group("name")
        if _cordis_row_enabled(row, name):
            names.add(name)
    return frozenset(names)


def _profile_plugin_fingerprint(
    dsh_home: Path,
    runtime_command: tuple[str, ...],
) -> str:
    """Return a bounded fingerprint of the active DSH profile dependencies.

    DSH can install plugins independently through its CLI/Web UI.  The SDK
    process is intentionally not mutated in place; comparing this fingerprint
    on every turn lets the runner retire a stale process and load the changed
    profile on the next safe boundary.  Only package metadata and lockfiles
    are read, never credentials or plugin source files.
    """

    try:
        runtime = classify_runtime_command(runtime_command)
        profile = runtime.profile
    except (TypeError, ValueError):
        return "unsafe"
    if profile is None:
        return "none"
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,47}", profile):
        return "unsafe"
    home = dsh_home.expanduser().resolve()
    profiles = home / "profiles"
    if profiles.is_symlink() or (profiles.exists() and not profiles.is_dir()):
        return "unsafe"
    root = profiles / profile
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        return "unsafe"
    try:
        resolved_root = root.resolve()
        if not resolved_root.is_relative_to(home):
            return "unsafe"
    except OSError:
        return "unsafe"
    digest = hashlib.sha256()
    present = False
    for filename in ("package.json", "pnpm-lock.yaml", "package-lock.json", "yarn.lock"):
        path = resolved_root / filename
        if not path.exists():
            digest.update(f"{filename}:missing\n".encode("ascii"))
            continue
        if path.is_symlink() or not path.is_file():
            return "unsafe"
        try:
            size = path.stat().st_size
            if size > 1_000_000:
                return "unsafe"
            content = path.read_bytes()
        except OSError:
            return "unsafe"
        present = True
        digest.update(filename.encode("ascii"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\n")
    if not present:
        return "missing"
    return f"sha256:{digest.hexdigest()}"


def _split_cordis_rows(text: str) -> tuple[str, ...]:
    """Split bounded top-level Cordis rows without importing a YAML parser."""

    starts = tuple(match.start() for match in _CORDIS_ROW_START.finditer(text))
    if not starts:
        return ()
    return tuple(
        text[start:end]
        for start, end in zip(starts, (*starts[1:], len(text)), strict=True)
    )


def _cordis_row_enabled(row: str, name: str) -> bool:
    """Accept only literal/known-safe activation forms for capability claims."""

    options = {
        match.group("key"): _normalize_cordis_scalar(match.group("value"))
        for match in _CORDIS_OPTION.finditer(row)
    }
    disabled = options.get("disabled")
    if disabled is not None and disabled != "false":
        # Unknown JavaScript expressions are deliberately treated as disabled;
        # the runtime may still boot them, but Python must not suppress its
        # compatibility fallback without a proof of activation.
        return False
    if name != _COMPACTION_PLUGIN:
        return True
    auto = options.get("auto")
    if auto is None or auto == "true":
        return True
    return auto == _COMPACTION_AUTO_EXPRESSION


def _normalize_cordis_scalar(value: str) -> str:
    normalized = value.strip()
    if normalized.startswith("!!js"):
        normalized = normalized[4:].strip()
    if len(normalized) >= 2 and normalized[0] == normalized[-1] and normalized[0] in "'\"":
        normalized = normalized[1:-1].strip()
    return normalized.casefold() if normalized in {"true", "false"} else normalized


def _validate_session_id(session_id: str) -> str:
    normalized = session_id.strip()
    if _SESSION_ID.fullmatch(normalized) is None:
        raise RunnerConfigurationError(
            "session_id must use 1-200 ASCII letters, digits, or ._:@+- characters"
        )
    return normalized


def _validate_reasoning_effort(reasoning_effort: str) -> ReasoningEffort:
    normalized = reasoning_effort.strip().casefold()
    if normalized not in REASONING_EFFORTS:
        supported = ", ".join(REASONING_EFFORTS)
        raise RunnerConfigurationError(f"reasoning_effort must be one of: {supported}")
    return cast(ReasoningEffort, normalized)


def _generation_session_id(session_id: str, generation: int) -> str:
    return session_id if generation == 0 else f"{session_id}--xdsh-reset-{generation}"


def _result_event_notification(
    raw_event: dict[str, JsonValue],
    *,
    dsh_session_id: str,
) -> dict[str, JsonValue]:
    """Wrap result events for SDKs that omit callback notifications."""

    method = raw_event.get("method")
    if method == "session.event" and isinstance(raw_event.get("payload"), dict):
        return raw_event
    return {
        "method": "session.event",
        "payload": {
            "sessionId": dsh_session_id,
            "event": raw_event,
        },
    }


def _safe_error_message(error: BaseException) -> str:
    """Keep the structured runner error useful without leaking exception text."""

    return safe_exception_text(error, max_chars=4_000)
