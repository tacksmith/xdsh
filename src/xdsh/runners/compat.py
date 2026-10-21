"""Forward-compatible, fail-closed helpers for the official DSH SDK seam.

The SDK is a developer preview and its Python names are allowed to move
between releases.  XDSH therefore keeps all version-sensitive reflection in
this module.  Product code still receives the small ``Harness`` protocol from
``sdk.py``; unknown optional SDK fields are ignored, while missing required
operations fail with a stable configuration error before a task is submitted.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import TypeAlias, cast

from xdsh.core.errors import RunnerConfigurationError

CanonicalArguments: TypeAlias = Mapping[str, object]

ADAPTER_CONTRACT = "dsh-sdk-jsonrpc-session-events-v1"

# Canonical XDSH names are kept stable.  Aliases cover observed and likely
# spelling changes without passing arbitrary user-controlled values to the
# SDK constructor.
CONFIG_ALIASES: dict[str, tuple[str, ...]] = {
    "provider": ("provider",),
    "model": ("model",),
    "max_tokens": ("max_tokens", "maxTokens", "output_tokens"),
    "cwd": ("cwd", "working_directory"),
    "runtime_cwd": ("runtime_cwd", "runtime_working_directory"),
    "session_root": ("session_root", "dsh_session_root"),
    "cordis": ("cordis", "cordis_config", "composition"),
    "env": ("env", "environment"),
    "runtime_bin": ("runtime_bin", "runtime_command"),
    "launch_args_override": (
        "launch_args_override",
        "runtime_argv",
        "runtime_command",
    ),
    "request_timeout_seconds": (
        "request_timeout_seconds",
        "request_timeout",
        "timeout_seconds",
    ),
    "shutdown_timeout_seconds": (
        "shutdown_timeout_seconds",
        "shutdown_timeout",
    ),
    "base_url": ("base_url",),
    "api_key": ("api_key",),
}

RUN_INPUT_ALIASES = ("input", "prompt", "task", "message", "content")
RUN_SESSION_ALIASES = ("session_id", "sessionId", "session")
RUN_CALLBACK_ALIASES = (
    "on_notification",
    "onNotification",
    "on_event",
    "notification_handler",
)


class DshSdkCompatibilityError(RunnerConfigurationError):
    """The installed SDK cannot satisfy the XDSH adapter contract."""


@dataclass(frozen=True, slots=True, kw_only=True)
class DshSdkSurface:
    """A content-free snapshot of the imported SDK surface."""

    sdk_version: str | None
    config_available: bool
    harness_available: bool
    run_method: str | None
    close_method: str | None
    contract: str = ADAPTER_CONTRACT
    operations: frozenset[str] = frozenset()

    @property
    def ready(self) -> bool:
        return (
            self.harness_available
            and self.run_method is not None
            and self.close_method is not None
        )

    def supports(self, operation: str) -> bool:
        """Return whether an optional operation is visible on this SDK surface."""

        return operation in self.operations


def installed_sdk_version() -> str | None:
    """Return the installed SDK version without starting a runtime process."""

    try:
        return version("deepseek-harness-sdk")
    except PackageNotFoundError:
        return None


def inspect_sdk_surface(module: object) -> DshSdkSurface:
    """Inspect only names and callable methods of an imported SDK module."""

    harness = _first_callable_object(module, ("DeepSeekHarness", "Harness"))
    config = _first_callable_object(
        module,
        ("DeepSeekHarnessConfig", "HarnessConfig", "Config"),
    )
    run_method = _first_callable_name(harness, ("run", "prompt", "session_prompt"))
    close_method = _first_callable_name(harness, ("close", "shutdown", "stop"))
    operations: set[str] = set()
    if run_method is not None:
        operations.add("run")
    if close_method is not None:
        operations.add("close")
    if _first_callable_name(harness, ("cancel", "interrupt", "abort")) is not None:
        operations.add("cancel")
    if _first_callable_name(harness, ("compact", "compact_session")) is not None:
        operations.add("compact")
    if _first_callable_name(harness, ("reset", "reset_session", "new_session")) is not None:
        operations.add("reset")
    if _first_callable_name(harness, ("start_session", "session", "open_session")) is not None:
        operations.add("session_factory")
    return DshSdkSurface(
        sdk_version=installed_sdk_version(),
        config_available=callable(config),
        harness_available=callable(harness),
        run_method=run_method,
        close_method=close_method,
        operations=frozenset(operations),
    )


def compatible_kwargs(
    factory: object,
    values: CanonicalArguments,
    *,
    aliases: Mapping[str, tuple[str, ...]] | None = None,
    required: frozenset[str] = frozenset(),
) -> dict[str, object]:
    """Map canonical values to parameters accepted by one SDK callable.

    A callable with ``**kwargs`` receives the canonical names.  For a strict
    signature, the first known alias is selected.  No retry is performed after
    invocation, so a task cannot be submitted twice because of a reflection
    mistake.
    """

    callable_factory = cast(Callable[..., object], factory)
    try:
        signature = inspect.signature(callable_factory)
    except (TypeError, ValueError) as error:
        raise DshSdkCompatibilityError("DSH SDK callable signature is unavailable") from error
    parameters = signature.parameters
    accepts_any = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    selected: dict[str, object] = {}
    alias_map = aliases or CONFIG_ALIASES
    for canonical, value in values.items():
        if accepts_any:
            selected[canonical] = value
            continue
        names = alias_map.get(canonical, (canonical,))
        target = next(
            (
                name
                for name in names
                if name in parameters
                and parameters[name].kind
                in {
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    inspect.Parameter.KEYWORD_ONLY,
                }
            ),
            None,
        )
        if target is None:
            if canonical in required:
                raise DshSdkCompatibilityError(
                    f"installed DSH SDK lacks required option: {canonical}"
                )
            continue
        selected[target] = value
    return selected


def invoke_compatible_run(
    harness: object,
    input_text: str,
    *,
    session_id: str,
    on_notification: Callable[[object], None],
) -> object:
    """Invoke one SDK run/prompt method without guessing after side effects."""

    try:
        return _invoke_run_method(
            harness,
            input_text,
            session_id=session_id,
            on_notification=on_notification,
            require_session=True,
        )
    except DshSdkCompatibilityError as error:
        # A few SDK revisions expose ``start_session(id).run(prompt)`` rather
        # than ``harness.run(prompt, session_id=id)``.  Enter that shape only
        # after signature inspection proves the direct method lacks the
        # session option; never retry a method that may already have submitted.
        session_factory = _first_callable_name(
            harness,
            ("start_session", "session", "open_session"),
        )
        if session_factory is None:
            raise error
        session = _invoke_session_factory(
            cast(Callable[..., object], getattr(harness, session_factory)),
            session_id,
        )
        return _invoke_run_method(
            session,
            input_text,
            session_id=session_id,
            on_notification=on_notification,
            require_session=False,
        )


def _invoke_run_method(
    harness: object,
    input_text: str,
    *,
    session_id: str,
    on_notification: Callable[[object], None],
    require_session: bool,
) -> object:
    """Invoke a discovered run method once, optionally on a session object."""

    method_name = _first_callable_name(harness, ("run", "prompt", "session_prompt"))
    if method_name is None:
        raise DshSdkCompatibilityError("installed DSH SDK has no run/prompt operation")
    method = cast(Callable[..., object], getattr(harness, method_name))
    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError) as error:
        raise DshSdkCompatibilityError("DSH SDK run signature is unavailable") from error

    parameters = signature.parameters
    accepts_any = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    kwargs: dict[str, object] = {}
    positional: list[object] = []
    input_name = next((name for name in RUN_INPUT_ALIASES if name in parameters), None)
    positional_parameter = next(
        (
            parameter
            for parameter in parameters.values()
            if parameter.kind
            in {
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            }
        ),
        None,
    )
    if positional_parameter is not None and positional_parameter.name not in {
        "self",
    }:
        positional.append(input_text)
    elif input_name is not None:
        kwargs[input_name] = input_text
    elif accepts_any:
        kwargs["input"] = input_text
    else:
        raise DshSdkCompatibilityError("installed DSH SDK run operation lacks input argument")

    _select_run_kwarg(
        parameters,
        accepts_any,
        kwargs,
        RUN_SESSION_ALIASES,
        session_id,
        required=require_session,
    )
    _select_run_kwarg(
        parameters,
        accepts_any,
        kwargs,
        RUN_CALLBACK_ALIASES,
        on_notification,
        required=False,
    )
    return method(*positional, **kwargs)


def _invoke_session_factory(
    factory: Callable[..., object],
    session_id: str,
) -> object:
    """Create a named SDK session using common current/future parameter names."""

    try:
        parameters = inspect.signature(factory).parameters
    except (TypeError, ValueError) as error:
        raise DshSdkCompatibilityError("DSH session factory signature is unavailable") from error
    accepts_any = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    positional = next(
        (
            parameter
            for parameter in parameters.values()
            if parameter.kind
            in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
        ),
        None,
    )
    if positional is not None:
        return factory(session_id)
    for name in RUN_SESSION_ALIASES:
        if name in parameters:
            return factory(**{name: session_id})
    if accepts_any:
        return factory(session_id=session_id)
    raise DshSdkCompatibilityError("DSH session factory lacks a session ID argument")


def invoke_compatible_close(harness: object) -> object:
    """Invoke the first supported close operation exactly once."""

    method_name = _first_callable_name(harness, ("close", "shutdown", "stop"))
    if method_name is None:
        raise DshSdkCompatibilityError("installed DSH SDK has no close/shutdown operation")
    return cast(Callable[[], object], getattr(harness, method_name))()


def _first_callable_name(target: object, names: tuple[str, ...]) -> str | None:
    if target is None:
        return None
    for name in names:
        if callable(getattr(target, name, None)):
            return name
    return None


def _first_callable_object(module: object, names: tuple[str, ...]) -> object | None:
    for name in names:
        candidate = getattr(module, name, None)
        if callable(candidate):
            return cast(object, candidate)
    return None


def _select_run_kwarg(
    parameters: Mapping[str, inspect.Parameter],
    accepts_any: bool,
    output: dict[str, object],
    names: tuple[str, ...],
    value: object,
    *,
    required: bool = True,
) -> None:
    if accepts_any:
        output[names[0]] = value
        return
    target = next(
        (
            name
            for name in names
            if name in parameters
            and parameters[name].kind
            in {inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY}
        ),
        None,
    )
    if target is None:
        if required:
            raise DshSdkCompatibilityError(
                f"installed DSH SDK run operation lacks required option: {names[0]}"
            )
        return
    output[target] = value
