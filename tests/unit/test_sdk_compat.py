from __future__ import annotations

from dataclasses import dataclass

from xdsh.runners.compat import (
    ADAPTER_CONTRACT,
    DshSdkCompatibilityError,
    compatible_kwargs,
    inspect_sdk_surface,
    invoke_compatible_close,
    invoke_compatible_run,
)
from xdsh.runners.sdk import _normalize_run_result, _OfficialHarnessAdapter


class FutureConfig:
    def __init__(
        self,
        provider: str,
        model: str,
        working_directory: str,
        environment: dict[str, str],
        runtime_command: tuple[str, ...],
    ) -> None:
        self.values = (provider, model, working_directory, environment, runtime_command)


class FutureHarness:
    def __init__(self, **_kwargs: object) -> None:
        self.closed = False

    def prompt(
        self,
        prompt: str,
        *,
        sessionId: str,
        on_event: object,
    ) -> dict[str, object]:
        assert callable(on_event)
        return {
            "sessionId": sessionId,
            "finalText": prompt,
            "finishReason": "completed",
            "events": [
                {
                    "type": "turn/end",
                    "data": {"reason": {"kind": "completed"}},
                }
            ],
        }

    def shutdown(self) -> None:
        self.closed = True

    def interrupt(self, session_id: str) -> None:
        del session_id

    def compact(self, session_id: str) -> None:
        del session_id


class SessionOnlyHarness:
    session_id: str

    def start_session(self, session_id: str) -> SessionOnlyHarness:
        self.session_id = session_id
        return self

    def run(self, prompt: str, *, on_event: object) -> dict[str, object]:
        assert callable(on_event)
        return {"finalText": prompt, "finishReason": "completed"}

    def close(self) -> None:
        pass

    def interrupt(self, session_id: str) -> None:
        del session_id

    def compact(self, session_id: str) -> None:
        del session_id


@dataclass
class NoCallbackResult:
    finalText: str
    finishReason: str
    sessionId: str
    events: list[dict[str, object]]


def test_compatible_kwargs_maps_known_future_aliases_and_drops_unknown() -> None:
    values = {
        "provider": "litellm-anthropic",
        "model": "ark-code-latest",
        "cwd": "/tmp/workspace",
        "env": {"XDSH_CONTEXT_WINDOW": "16384"},
        "launch_args_override": ("node", "runtime.js"),
        "future_only_option": "ignored",
    }

    selected = compatible_kwargs(
        FutureConfig,
        values,
        required=frozenset({"provider", "model", "cwd", "env"}),
    )

    assert selected == {
        "provider": "litellm-anthropic",
        "model": "ark-code-latest",
        "working_directory": "/tmp/workspace",
        "environment": {"XDSH_CONTEXT_WINDOW": "16384"},
        "runtime_command": ("node", "runtime.js"),
    }


def test_future_prompt_and_shutdown_names_are_normalized() -> None:
    harness = FutureHarness()
    seen: list[object] = []

    result = invoke_compatible_run(
        harness,
        "hello",
        session_id="session-1",
        on_notification=seen.append,
    )
    invoke_compatible_close(harness)

    assert isinstance(result, dict)
    assert result["sessionId"] == "session-1"
    assert harness.closed is True
    assert seen == []


def test_optional_deferred_subscription_drift_does_not_break_foreground() -> None:
    class SubscriptionDrift(FutureHarness):
        @property
        def client(self) -> SubscriptionDrift:
            return self

        def subscribe_session_notifications(self, *, new_argument: str) -> None:
            raise NotImplementedError

    adapter = _OfficialHarnessAdapter(SubscriptionDrift())
    assert adapter.subscribe_session_notifications("session") is None
    assert adapter.run("hello", session_id="session").final_response == "hello"


def test_sdk_surface_reports_contract_without_starting_runtime() -> None:
    surface = inspect_sdk_surface(
        type(
            "FutureSdk",
            (),
            {"DeepSeekHarness": FutureHarness, "DeepSeekHarnessConfig": FutureConfig},
        )
    )

    assert surface.ready is True
    assert surface.run_method == "prompt"
    assert surface.close_method == "shutdown"
    assert surface.contract == ADAPTER_CONTRACT
    assert surface.operations == frozenset({"run", "close", "cancel", "compact"})


def test_sdk_surface_accepts_public_harness_aliases() -> None:
    surface = inspect_sdk_surface(
        type("FutureSdk", (), {"Harness": FutureHarness, "HarnessConfig": FutureConfig})
    )
    assert surface.ready


def test_async_future_run_is_resolved_once() -> None:
    class AsyncHarness:
        async def run(
            self,
            prompt: str,
            *,
            session_id: str,
            on_notification: object,
        ) -> dict[str, object]:
            assert callable(on_notification)
            return {
                "session_id": session_id,
                "final_response": prompt,
                "finish_reason": "completed",
                "events": [],
            }

        async def close(self) -> None:
            return None

    adapter = _OfficialHarnessAdapter(AsyncHarness())
    result = adapter.run("async prompt", session_id="async-session")
    assert result.session_id == "async-session"
    assert result.final_response == "async prompt"
    adapter.close()


def test_optional_sdk_start_is_eager_and_zero_argument_only() -> None:
    class StartableHarness:
        def __init__(self) -> None:
            self.started = 0

        async def start(self) -> None:
            self.started += 1

        def close(self) -> None:
            return None

    class ArgumentStartHarness:
        def start(self, runtime: str) -> None:
            del runtime

        def close(self) -> None:
            return None

    startable = StartableHarness()
    assert _OfficialHarnessAdapter(startable).start() is True
    assert startable.started == 1
    assert _OfficialHarnessAdapter(ArgumentStartHarness()).start() is False


def test_result_field_aliases_are_coerced_to_stable_shape() -> None:
    result = _normalize_run_result(
        NoCallbackResult(
            finalText="answer",
            finishReason="completed",
            sessionId="future-session",
            events=[{"type": "turn/end"}],
        ),
        fallback_session_id="fallback",
    )

    assert result.session_id == "future-session"
    assert result.final_response == "answer"
    assert result.finish_reason == "completed"
    assert result.events == [{"type": "turn/end"}]


def test_missing_required_future_run_argument_fails_closed() -> None:
    class BrokenHarness:
        def run(self, prompt: str) -> None:
            del prompt

    try:
        invoke_compatible_run(
            BrokenHarness(),
            "hello",
            session_id="s",
            on_notification=lambda _item: None,
        )
    except DshSdkCompatibilityError as error:
        assert "required option" in str(error)
    else:  # pragma: no cover - assertion branch
        raise AssertionError("missing session argument must fail closed")


def test_session_factory_shape_preserves_named_session() -> None:
    harness = SessionOnlyHarness()

    result = invoke_compatible_run(
        harness,
        "hello",
        session_id="session-2",
        on_notification=lambda _item: None,
    )

    assert isinstance(result, dict)
    assert result["finalText"] == "hello"
    assert harness.session_id == "session-2"
