"""Small seam around the developer-preview official Python SDK."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import threading
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, cast

from xdsh.core.errors import RunnerConfigurationError
from xdsh.core.events import JsonValue
from xdsh.core.execution_policy import DEFAULT_MAX_PARALLEL_TOOL_CALLS
from xdsh.core.rate_limit import RateLimitPolicy
from xdsh.core.runner import ReasoningEffort
from xdsh.runners.compat import (
    CONFIG_ALIASES,
    DshSdkCompatibilityError,
    compatible_kwargs,
    inspect_sdk_surface,
    invoke_compatible_close,
    invoke_compatible_run,
)
from xdsh.runners.runtime import (
    append_profile_patch,
    classify_runtime_command,
    materialize_profile_files,
)


class DshRuntimeInitializationError(RunnerConfigurationError):
    """SDK surface exists, but its runtime failed the initialization handshake."""


class HarnessRunResult(Protocol):
    session_id: str
    final_response: str
    finish_reason: str | None
    events: list[dict[str, JsonValue]]


class Harness(Protocol):
    def run(
        self,
        input: str,
        *,
        session_id: str,
        on_notification: Callable[[object], None] | None = None,
    ) -> HarnessRunResult: ...

    def close(self) -> None: ...


class NotificationSubscription(Protocol):
    """Official SDK's non-blocking, independently owned notification reader."""

    def drain(self, on_notification: Callable[[object], None]) -> None: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class _NormalizedRunResult:
    """Stable result shape used when an SDK changes result field names."""

    session_id: str
    final_response: str
    finish_reason: str | None
    events: list[dict[str, JsonValue]]


class _OfficialHarnessAdapter:
    """Keep SDK reflection and result-shape drift outside ``DshRunner``."""

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self._subscription_unavailable = False

    def start(self) -> bool:
        """Eagerly initialize SDK transports that expose a zero-argument start.

        RC2 otherwise defers process launch and JSON-RPC ``initialize`` until
        the first model run. Starting inside the factory keeps that transport
        phase under the cold-start deadline without putting model latency under
        the same retry policy. Future eager constructors may omit this method.
        """

        raw_start = getattr(self._inner, "start", None)
        if not callable(raw_start):
            return False
        try:
            parameters = tuple(inspect.signature(raw_start).parameters.values())
        except (TypeError, ValueError):
            parameters = ()
        if any(
            parameter.default is inspect.Parameter.empty
            and parameter.kind
            in {
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            }
            for parameter in parameters
        ):
            return False
        provider = getattr(getattr(self._inner, "config", None), "provider", None)
        for attempt in range(7):
            try:
                result = cast(Callable[[], object], raw_start)()
                if inspect.isawaitable(result):
                    _wait_for_awaitable(result)
                return True
            except Exception as error:
                # RC2 can accept initialize before asynchronous provider registration
                # settles when MCP is enabled. Repeat only this pre-task handshake,
                # on the same process; never replay prompts or arbitrary failures.
                if (attempt == 6 or not isinstance(provider, str)
                        or type(error).__name__ != "JsonRpcError"
                        or str(error) != f'no adapter registered for provider "{provider}"'):
                    raise
                time.sleep(min(0.25 * 2**attempt, 1.0))
        return False  # unreachable; every attempt returns or raises

    def run(
        self,
        input: str,
        *,
        session_id: str,
        on_notification: Callable[[object], None] | None = None,
    ) -> HarnessRunResult:
        callback = on_notification or (lambda _notification: None)
        raw = invoke_compatible_run(
            self._inner,
            input,
            session_id=session_id,
            on_notification=callback,
        )
        if inspect.isawaitable(raw):
            raw = _wait_for_awaitable(raw)
        return _normalize_run_result(raw, fallback_session_id=session_id)

    def close(self) -> None:
        result = invoke_compatible_close(self._inner)
        if inspect.isawaitable(result):
            _drain_awaitable(result)

    def subscribe_session_notifications(self, session_id: str) -> NotificationSubscription | None:
        """Feature-detect the official read-only surface; never inspect stdout."""

        if self._subscription_unavailable:
            return None
        try:
            client = getattr(self._inner, "client", None)
            subscribe = getattr(client, "subscribe_session_notifications", None)
            if not callable(subscribe) or inspect.iscoroutinefunction(subscribe):
                self._subscription_unavailable = True
                return None
            subscription = subscribe(session_id)
        except Exception as error:
            # Optional observation must not break an otherwise compatible SDK
            # prompt surface. Do not retry prompts or expose exception bodies.
            self._subscription_unavailable = True
            logging.getLogger(__name__).warning(
                "Optional SDK notification subscription unavailable: %s", type(error).__name__,
            )
            return None
        if not callable(getattr(subscription, "drain", None)) or not callable(
            getattr(subscription, "close", None),
        ):
            self._subscription_unavailable = True
            close = getattr(subscription, "close", None)
            if callable(close):
                close()
            return None
        return cast(NotificationSubscription, subscription)


@dataclass(frozen=True, slots=True, kw_only=True)
class HarnessLaunch:
    cwd: Path
    dsh_home: Path
    session_root: Path
    cordis: Path
    provider: str
    model: str
    reasoning_effort: ReasoningEffort
    system_prompt: str
    context_window: int
    model_max_tokens: int
    max_tokens: int | None
    runtime_command: tuple[str, ...]
    request_timeout_seconds: float | None
    shutdown_timeout_seconds: float
    stream_idle_timeout_seconds: float = 120.0
    max_parallel_tool_calls: int = DEFAULT_MAX_PARALLEL_TOOL_CALLS
    rate_limit_policy: RateLimitPolicy = field(default_factory=RateLimitPolicy)
    auto_compaction_enabled: bool = True
    native_compaction_enabled: bool = True
    auto_compaction_threshold_ratio: float = 0.50
    auto_compaction_retain_ratio: float = 0.08
    auto_compaction_threshold_tokens: int | None = 64_000
    litellm_base_url_env: str = "XDSH_LITELLM_BASE_URL"
    litellm_api_key_env: str = "XDSH_LITELLM_API_KEY"


class HarnessFactory(Protocol):
    def __call__(self, launch: HarnessLaunch) -> Harness: ...


def create_official_harness(launch: HarnessLaunch) -> Harness:
    """Create the official SDK client through the forward-compatible seam."""

    try:
        import deepseek_harness  # type: ignore[import-untyped]
    except ImportError as error:
        raise DshSdkCompatibilityError("official DSH Python SDK is not installed") from error

    surface = inspect_sdk_surface(deepseek_harness)
    if not surface.ready:
        raise DshSdkCompatibilityError(
            "installed DSH Python SDK does not expose the required run/close surface"
        )
    harness_type = _first_callable_object(
        deepseek_harness,
        ("DeepSeekHarness", "Harness"),
    )
    config_type = _first_callable_object(
        deepseek_harness,
        ("DeepSeekHarnessConfig", "HarnessConfig", "Config"),
    )
    if not callable(harness_type):
        raise DshSdkCompatibilityError("installed DSH Python SDK has no harness constructor")

    runtime_spec = classify_runtime_command(launch.runtime_command)
    runtime_command = launch.runtime_command
    if runtime_spec.is_profile:
        try:
            base_url = os.environ.get(launch.litellm_base_url_env, "")
            profile_files = materialize_profile_files(
                dsh_home=launch.dsh_home,
                provider=launch.provider,
                model=launch.model,
                base_url=base_url,
                api_key_env=launch.litellm_api_key_env,
                context_window=launch.context_window,
                model_max_tokens=launch.max_tokens or launch.model_max_tokens,
                reasoning_effort=launch.reasoning_effort,
                max_parallel_tool_calls=launch.max_parallel_tool_calls,
                system_prompt=launch.system_prompt,
                workspace_root=launch.cwd,
                auto_compaction_enabled=launch.auto_compaction_enabled,
                native_compaction_enabled=launch.native_compaction_enabled,
                threshold_ratio=launch.auto_compaction_threshold_ratio,
                retain_ratio=launch.auto_compaction_retain_ratio,
                rate_limit_policy=launch.rate_limit_policy,
                stream_idle_timeout_seconds=launch.stream_idle_timeout_seconds,
            )
            runtime_command = append_profile_patch(
                runtime_command,
                profile_files.patch_path,
            )
        except (OSError, ValueError) as error:
            raise DshSdkCompatibilityError(
                "cannot prepare the official DSH profile compatibility overlay"
            ) from error

    environment = {
        "DSH_HOME": str(launch.dsh_home),
        "DSH_CWD": str(launch.cwd),
        "DSH_SESSION_ROOT": str(launch.session_root),
        "DSH_SYSTEM_PROMPT": launch.system_prompt,
        "XDSH_CONTEXT_WINDOW": str(launch.context_window),
        "XDSH_LITELLM_MODEL": launch.model,
        "XDSH_MODEL_MAX_TOKENS": str(launch.model_max_tokens),
        "XDSH_MAX_PARALLEL_TOOL_CALLS": str(launch.max_parallel_tool_calls),
        "XDSH_REASONING_EFFORT": launch.reasoning_effort,
        "XDSH_LLM_STREAM_IDLE_TIMEOUT_MS": str(
            round(launch.stream_idle_timeout_seconds * 1000)
        ),
        # Keep the official dsh-llm-retry plugin bounded in the legacy rc.2
        # Cordis composition too. These are policy knobs, never credentials or
        # provider response text.
        "XDSH_RATE_LIMIT_MAX_RETRIES": str(
            launch.rate_limit_policy.max_rate_limit_retries
        ),
        "XDSH_RATE_LIMIT_BASE_BACKOFF_SECONDS": str(
            launch.rate_limit_policy.base_backoff_seconds
        ),
        "XDSH_RATE_LIMIT_MAX_BACKOFF_SECONDS": str(
            launch.rate_limit_policy.max_backoff_seconds
        ),
        "XDSH_RATE_LIMIT_JITTER_RATIO": str(launch.rate_limit_policy.jitter_ratio),
        # These flags are normalized to 0/1 before crossing into Cordis so
        # YAML/JS config cannot disagree with the Python transport policy.
        "XDSH_AUTO_COMPACT_ENABLED": "1" if launch.auto_compaction_enabled else "0",
        "XDSH_NATIVE_COMPACTION_ENABLED": (
            "1" if launch.native_compaction_enabled else "0"
        ),
        "XDSH_AUTO_COMPACT_THRESHOLD_RATIO": str(
            launch.auto_compaction_threshold_ratio
        ),
        "XDSH_AUTO_COMPACT_RETAIN_RATIO": str(launch.auto_compaction_retain_ratio),
        "XDSH_AUTO_COMPACT_THRESHOLD_TOKENS": (
            str(launch.auto_compaction_threshold_tokens)
            if launch.auto_compaction_threshold_tokens is not None
            else ""
        ),
    }
    config_values: dict[str, object] = {
        "provider": launch.provider,
        "model": launch.model,
        "max_tokens": launch.max_tokens,
        "cwd": str(launch.cwd),
        "runtime_cwd": str(launch.cwd),
        "session_root": str(launch.session_root),
        "cordis": str(launch.cordis),
        "env": environment,
        "launch_args_override": runtime_command,
        "request_timeout_seconds": launch.request_timeout_seconds,
        "shutdown_timeout_seconds": launch.shutdown_timeout_seconds,
    }
    try:
        if callable(config_type):
            config_kwargs = compatible_kwargs(
                config_type,
                config_values,
                aliases=CONFIG_ALIASES,
                required=frozenset({"provider", "model", "cwd", "env"}),
            )
            config = cast(object, config_type(**config_kwargs))
            harness = _construct_harness(harness_type, config, config_values)
        else:
            harness_kwargs = compatible_kwargs(
                harness_type,
                config_values,
                aliases=CONFIG_ALIASES,
                required=frozenset({"provider", "model", "cwd"}),
            )
            harness = cast(object, harness_type(**harness_kwargs))
    except DshSdkCompatibilityError:
        raise
    except BaseException as error:
        raise DshSdkCompatibilityError(
            "installed DSH SDK rejected the XDSH launch options"
        ) from error
    adapter = _OfficialHarnessAdapter(harness)
    try:
        adapter.start()
    except BaseException as error:
        try:
            adapter.close()
        except BaseException:
            pass
        raise DshRuntimeInitializationError(
            "installed DSH SDK could not initialize its runtime transport"
        ) from error
    return adapter


def _construct_harness(
    harness_type: object,
    config: object,
    config_values: Mapping[str, object],
) -> object:
    """Pass a config object using whichever constructor shape the SDK exposes."""

    try:
        signature = inspect.signature(cast(Callable[..., object], harness_type))
    except (TypeError, ValueError) as error:
        raise DshSdkCompatibilityError(
            "DSH harness constructor signature is unavailable"
        ) from error
    parameters = signature.parameters
    for name in ("config", "options", "settings"):
        parameter = parameters.get(name)
        if parameter is not None and parameter.kind in {
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        }:
            return cast(Callable[..., object], harness_type)(**{name: config})
    if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
        return cast(Callable[..., object], harness_type)(config=config)
    kwargs = compatible_kwargs(
        harness_type,
        config_values,
        aliases=CONFIG_ALIASES,
        required=frozenset({"provider", "model", "cwd"}),
    )
    return cast(Callable[..., object], harness_type)(**kwargs)


def _normalize_run_result(raw: object, *, fallback_session_id: str) -> HarnessRunResult:
    """Coerce current and forward-compatible SDK result objects to one shape."""

    session_id = _result_value(raw, ("session_id", "sessionId", "id"))
    final_response = _result_value(
        raw,
        ("final_response", "finalResponse", "final_text", "finalText", "message"),
    )
    finish_reason = _result_value(raw, ("finish_reason", "finishReason", "reason"))
    raw_events = _result_value(raw, ("events", "session_events", "notifications"))
    events: list[dict[str, JsonValue]] = []
    if isinstance(raw_events, Sequence) and not isinstance(raw_events, str | bytes):
        for item in raw_events:
            if isinstance(item, Mapping) and all(isinstance(key, str) for key in item):
                events.append(cast(dict[str, JsonValue], dict(item)))
    return cast(
        HarnessRunResult,
        _NormalizedRunResult(
            session_id=(
                session_id
                if isinstance(session_id, str) and session_id
                else fallback_session_id
            ),
            final_response=final_response if isinstance(final_response, str) else "",
            finish_reason=finish_reason if isinstance(finish_reason, str) else None,
            events=events,
        ),
    )


def _result_value(raw: object, names: tuple[str, ...]) -> object:
    if isinstance(raw, Mapping):
        for name in names:
            if name in raw:
                return raw[name]
        return None
    for name in names:
        value = getattr(raw, name, None)
        if value is not None:
            return value
    return None


def _drain_awaitable(awaitable: object) -> None:
    """Support an async close method while keeping the runner sync-facing."""

    _wait_for_awaitable(awaitable)


def _wait_for_awaitable(awaitable: object) -> object:
    """Resolve an SDK coroutine from a worker thread or an active event loop."""

    async_value = cast(Awaitable[object], awaitable)

    async def wait_for_value() -> object:
        return await async_value

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(wait_for_value())
    error: list[BaseException] = []
    result: list[object] = []

    def run() -> None:
        try:
            result.append(asyncio.run(wait_for_value()))
        except BaseException as exc:  # pragma: no cover - defensive SDK drift path
            error.append(exc)

    thread = threading.Thread(target=run, name="dsh-sdk-close", daemon=True)
    thread.start()
    thread.join()
    if error:
        raise DshSdkCompatibilityError("DSH SDK async close operation failed") from error[0]
    return result[0] if result else None


def _first_callable_object(module: object, names: tuple[str, ...]) -> object | None:
    for name in names:
        candidate = getattr(module, name, None)
        if callable(candidate):
            return cast(object, candidate)
    return None
