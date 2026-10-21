from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

import pytest

from xdsh.core.events import (
    ActivityEvent,
    ArtifactEvent,
    ErrorEvent,
    FinishEvent,
    GuardAction,
    GuardEvent,
    GuardReason,
    RetryEvent,
    RunnerEventT,
    SubagentAction,
    SubagentEvent,
    TextEvent,
    ToolResultEvent,
    ToolStartEvent,
    UsageEvent,
)
from xdsh.core.run_guard import GuardedRunner, RunGuardPolicy, _RunGuardState
from xdsh.core.runner import ReasoningEffort


@pytest.mark.asyncio
async def test_failed_acceptance_retains_finish_only_partial_answer(tmp_path: Path) -> None:
    (tmp_path / "result.json").write_text('{"ok": false}')
    task = "XDSH-VERIFY: " + json.dumps({"unchanged": [], "json": [{
        "path": "result.json", "equals": {"/ok": True},
    }]})
    runner = GuardedRunner(_ScriptedRunner((FinishEvent(
        session_id="test", reason="completed", final_text="Saved measurements; checks remain.",
    ),)))
    result = [e async for e in runner.run(task, session_id="test", cwd=tmp_path)]
    assert isinstance(result[-1], FinishEvent)
    assert result[-1].reason == "error"
    assert result[-1].final_text == "Saved measurements; checks remain."


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="safe artifact I/O requires POSIX")
async def test_declared_array_check_reaches_guarded_runner_without_replaying(
    tmp_path: Path,
) -> None:
    task = "XDSH-VERIFY: " + json.dumps({"unchanged": [], "json": [], "arrays": [{
        "path": "embedding.npy", "shape": [None, 2],
    }]})
    (tmp_path / "embedding.npy").write_bytes(b"invalid numeric array")
    runner = GuardedRunner(_ScriptedRunner((
        TextEvent(session_id="test", text="Saved partial measurements."),
        FinishEvent(session_id="test", reason="completed", final_text="Review the embedding."),
    )))
    result = [e async for e in runner.run(task, session_id="test", cwd=tmp_path)]
    assert len([e for e in result if isinstance(e, TextEvent)]) == 1
    assert len([e for e in result if isinstance(e, ErrorEvent)
                and e.code == "ACCEPTANCE_FAILED"]) == 1
    assert isinstance(result[-1], FinishEvent)
    assert result[-1].reason == "error" and result[-1].final_text == "Review the embedding."


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="safe artifact I/O requires POSIX")
async def test_understated_maximum_difference_retains_answer_without_replaying(
    tmp_path: Path,
) -> None:
    task = "XDSH-VERIFY: " + json.dumps({"unchanged": [], "json": [{
        "path": "result.json", "max_abs_differences": [{
            "value": "/max_error_pp", "actual": "/actual", "reference": "/reference",
            "scale": 100, "atol": 0.00001,
        }],
    }]})
    (tmp_path / "result.json").write_text(json.dumps({
        "actual": {"a": 0.42, "b": 0.73}, "reference": {"a": 0.42, "b": 0.70},
        "max_error_pp": 0.3,
    }))
    runner = GuardedRunner(_ScriptedRunner((FinishEvent(
        session_id="test", reason="completed", final_text="Measurements saved; review needed.",
    ),)))
    result = [e async for e in runner.run(task, session_id="test", cwd=tmp_path)]
    assert len([e for e in result if isinstance(e, ErrorEvent)
                and e.code == "ACCEPTANCE_FAILED"]) == 1
    assert isinstance(result[-1], FinishEvent) and result[-1].reason == "error"
    assert result[-1].final_text == "Measurements saved; review needed."


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="safe artifact I/O requires POSIX")
async def test_explicit_artifact_contract_rejects_wrong_source_hash(tmp_path: Path) -> None:
    original = b'[{"id":"A"},{"id":"A"},]'
    (tmp_path / "orders.json").write_bytes(original)
    (tmp_path / "report.json").write_text(json.dumps({
        "duplicate_ids": ["A"], "before": hashlib.sha256(original).hexdigest(),
        "after": hashlib.sha256(b"repaired file").hexdigest(),
    }))
    spec = {"unchanged": ["orders.json"], "json": [{"path": "report.json",
            "equals": {"/duplicate_ids": ["A"]},
            "source_sha256": {"/before": "orders.json", "/after": "orders.json"}}]}
    runner = GuardedRunner(_ScriptedRunner((
        TextEvent(session_id="test", text="claimed completed"),
        FinishEvent(session_id="test", reason="completed"),
    )))
    result = [e async for e in runner.run("XDSH-VERIFY: " + json.dumps(spec) + "\nRepair",
                                         session_id="test", cwd=tmp_path)]
    assert any(isinstance(e, ErrorEvent) and e.code == "ACCEPTANCE_FAILED" for e in result)
    assert isinstance(result[-1], FinishEvent) and result[-1].reason == "error"


@pytest.mark.asyncio
async def test_repeated_failed_call_cannot_hide_behind_successful_todo(tmp_path: Path) -> None:
    events: list[RunnerEventT] = [TextEvent(session_id="test", text="partial evidence")]
    for i in range(3):
        events.extend((
            ToolStartEvent(session_id="test", call_id=f"web-{i}", name="web_fetch",
                           arguments={"url": "https://example.org/missing"}),
            ToolResultEvent(session_id="test", call_id=f"web-{i}", is_error=True),
            ToolStartEvent(session_id="test", call_id=f"todo-{i}", name="todo",
                           arguments={"step": i}),
            ToolResultEvent(session_id="test", call_id=f"todo-{i}", is_error=False),
        ))
    events.append(FinishEvent(session_id="test", reason="completed"))
    delegate = _ScriptedRunner(tuple(events))
    runner = GuardedRunner(delegate)
    result = [e async for e in runner.run("test", session_id="test", cwd=tmp_path)]
    assert delegate.cancelled == ["test"]
    assert any(isinstance(e, GuardEvent) and e.action is GuardAction.STOP
               and e.reason is GuardReason.REPEATED_TOOL and e.limit == 3 for e in result)
    assert any(isinstance(e, TextEvent) and e.text == "partial evidence" for e in result)


@pytest.mark.asyncio
async def test_stagnant_job_output_requires_elapsed_silence_not_only_call_count() -> None:
    state = _RunGuardState(
        policy=RunGuardPolicy(max_stagnant_poll_calls=2,
                              active_tool_stall_timeout_seconds=60),
        session_id="test", started_at=0, last_event_at=0,
    )
    for index, now in enumerate((0, 10, 20, 59, 60)):
        state.observe(ToolStartEvent(session_id="test", call_id=str(index), name="job_output",
                                     arguments={"job_id": "build-1"}), now=now)
        result = state.observe(ToolResultEvent(session_id="test", call_id=str(index),
                                              content="still downloading dependencies"), now=now)
        if now < 60:
            assert not any(e.action is GuardAction.STOP for e in result)
    stop = next(e for e in result if e.action is GuardAction.STOP)
    assert stop.reason is GuardReason.REPEATED_TOOL
    assert stop.limit == 2


@pytest.mark.asyncio
async def test_quiet_background_poll_does_not_kill_other_productive_work(tmp_path: Path) -> None:
    events: list[RunnerEventT] = []
    for index in range(12):
        events.extend((
            ToolStartEvent(session_id="test", call_id=f"work-{index}", name="bash",
                           arguments={"command": f"inspect-result-{index}"}),
            ToolResultEvent(session_id="test", call_id=f"work-{index}",
                            content=f"new result {index}"),
            ToolStartEvent(session_id="test", call_id=f"poll-{index}", name="job_output",
                           arguments={"job_id": "background"}),
            ToolResultEvent(session_id="test", call_id=f"poll-{index}",
                            content="(no new output)\n[status: running]"),
        ))
    events.append(FinishEvent(session_id="test", reason="completed"))
    delegate = _ScriptedRunner(tuple(events))
    runner = GuardedRunner(delegate)
    result = [e async for e in runner.run("test", session_id="test", cwd=tmp_path)]
    assert not delegate.cancelled
    assert isinstance(result[-1], FinishEvent) and result[-1].reason == "completed"


def test_only_substantive_tool_progress_extends_background_poll_grace() -> None:
    state = _RunGuardState(policy=RunGuardPolicy(max_stagnant_poll_calls=2,
                            active_tool_stall_timeout_seconds=60),
                          session_id="test", started_at=0, last_event_at=0)

    def poll(now: float, text: str = "unchanged") -> tuple[GuardEvent, ...]:
        start = state.observe(ToolStartEvent(session_id="test", call_id=str(now),
                               name="job_output", arguments={"job_id": "work"}), now=now)
        result = state.observe(ToolResultEvent(session_id="test", call_id=str(now),
                                               content=text), now=now)
        return start + result

    for now in (0, 10, 20):
        assert not any(e.action is GuardAction.STOP for e in poll(now))
    state.observe(ToolStartEvent(session_id="test", call_id="work", name="bash",
                                  arguments={"command": "analysis"}), now=59)
    state.observe(ToolResultEvent(session_id="test", call_id="work", content="new evidence"),
                  now=59)
    assert not any(e.action is GuardAction.STOP for e in poll(100))
    state.observe(TextEvent(session_id="test", text="still busy"), now=110)
    state.observe(ActivityEvent(session_id="test"), now=111)
    state.observe(UsageEvent(session_id="test", input_tokens=1, output_tokens=1), now=112)
    state.observe(ToolStartEvent(session_id="test", call_id="todo", name="todo_write"), now=113)
    state.observe(ToolResultEvent(session_id="test", call_id="todo", content="updated"), now=114)
    assert any(e.action is GuardAction.STOP for e in poll(120))


def test_new_poll_output_at_deadline_is_evaluated_before_stopping() -> None:
    state = _RunGuardState(policy=RunGuardPolicy(max_stagnant_poll_calls=2,
                            active_tool_stall_timeout_seconds=60),
                          session_id="test", started_at=0, last_event_at=0)
    for now in (0, 10, 20, 60):
        assert not any(e.action is GuardAction.STOP for e in state.observe(
            ToolStartEvent(session_id="test", call_id=str(now), name="job_output",
                           arguments={"job_id": "work"}), now=now))
        assert not any(e.action is GuardAction.STOP for e in state.observe(
            ToolResultEvent(session_id="test", call_id=str(now),
                            content="new output" if now == 60 else "same"), now=now))


@pytest.mark.asyncio
async def test_changed_job_output_keeps_long_running_work_alive(tmp_path: Path) -> None:
    events: list[RunnerEventT] = []
    for index in range(10):
        events.extend((
            ToolStartEvent(
                session_id="test", call_id=f"poll-{index}", name="job_output",
                arguments={"job_id": "build-1"},
            ),
            ToolResultEvent(
                session_id="test", call_id=f"poll-{index}", name="job_output",
                content=f"downloaded {index + 1} wheels",
            ),
        ))
    events.append(FinishEvent(session_id="test", reason="completed"))
    delegate = _ScriptedRunner(tuple(events))
    runner = GuardedRunner(delegate, policy=RunGuardPolicy(max_stagnant_poll_calls=2))

    result = [event async for event in runner.run("test", session_id="test", cwd=tmp_path)]

    assert not any(
        isinstance(event, GuardEvent) and event.action is GuardAction.STOP for event in result
    )
    assert not delegate.cancelled


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery", ["success", "artifact", "different_arguments"])
async def test_repeat_failure_budget_respects_actual_recovery(
    tmp_path: Path, recovery: str,
) -> None:
    events: list[RunnerEventT] = []
    for i in range(5):
        target = i if recovery == "different_arguments" else 0
        if recovery == "artifact" and i:
            events.append(ArtifactEvent(session_id="test", path="fixed.txt", operation="write"))
        events.extend((
            ToolStartEvent(session_id="test", call_id=str(i), name="web_fetch",
                           arguments={"url": f"https://example.org/{target}"}),
            ToolResultEvent(session_id="test", call_id=str(i),
                            is_error=not (recovery == "success" and i % 2 == 1)),
        ))
    events.append(FinishEvent(session_id="test", reason="completed"))
    delegate = _ScriptedRunner(tuple(events))
    runner = GuardedRunner(delegate)
    # Reusing the guarded runner must not carry counters across turns.
    for session_id in ("first", "second"):
        result = [e async for e in runner.run("test", session_id=session_id, cwd=tmp_path)]
        assert not any(isinstance(e, GuardEvent) and e.action is GuardAction.STOP for e in result)
    assert not delegate.cancelled


@pytest.mark.parametrize("invalid", [0, -1, True, 1.5])
def test_repeated_tool_error_budget_must_be_positive_integer(invalid: object) -> None:
    with pytest.raises(ValueError, match="max_same_tool_errors"):
        RunGuardPolicy(max_same_tool_errors=invalid)  # type: ignore[arg-type]


@pytest.mark.parametrize("invalid", [0, -1, True, 1.5])
def test_stagnant_poll_budget_must_be_positive_integer(invalid: object) -> None:
    with pytest.raises(ValueError, match="max_stagnant_poll_calls"):
        RunGuardPolicy(max_stagnant_poll_calls=invalid)  # type: ignore[arg-type]


class _ScriptedRunner:
    def __init__(self, events: tuple[RunnerEventT, ...]) -> None:
        self.events = events
        self.cancelled: list[str] = []
        self.reset_sessions: list[str] = []
        self.closed = False

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
        del task, cwd, model, reasoning_effort, fallback, system_prompt, resume_context
        for event in self.events:
            yield event

    async def cancel(self, session_id: str) -> bool:
        self.cancelled.append(session_id)
        return True

    async def reset(self, session_id: str) -> None:
        self.reset_sessions.append(session_id)

    async def close(self) -> None:
        self.closed = True


class _BlockingRunner(_ScriptedRunner):
    def __init__(self) -> None:
        super().__init__(())
        self.release = asyncio.Event()

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
        del task, cwd, model, reasoning_effort, fallback, system_prompt, resume_context
        yield TextEvent(session_id=session_id, text="partial answer")
        await self.release.wait()


class _BlockingToolRunner(_ScriptedRunner):
    def __init__(self) -> None:
        super().__init__(())
        self.release = asyncio.Event()

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
        del task, cwd, model, reasoning_effort, fallback, system_prompt, resume_context
        yield ToolStartEvent(session_id=session_id, call_id="call-1", name="bash")
        await self.release.wait()


class _ManualTime:
    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        await asyncio.sleep(0)


class _TimedActivityRunner(_ScriptedRunner):
    def __init__(
        self,
        manual: _ManualTime,
        *,
        activity_times: tuple[float, ...] = (7.0,),
        finish_at: float = 14.0,
    ) -> None:
        super().__init__(())
        self.manual = manual
        self.activity_times = activity_times
        self.finish_at = finish_at

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
        del task, cwd, model, reasoning_effort, fallback, system_prompt, resume_context
        for activity_at in self.activity_times:
            self.manual.now = activity_at
            yield ActivityEvent(session_id=session_id)
        self.manual.now = self.finish_at
        yield FinishEvent(session_id=session_id, reason="completed")


class _ParameterFramesRunner(_ScriptedRunner):
    def __init__(self, manual: _ManualTime, fragment: str) -> None:
        super().__init__(())
        self.manual = manual
        self.fragment = fragment

    async def run(
        self, task: str, *, session_id: str, cwd: Path, model: str | None = None,
        reasoning_effort: ReasoningEffort = "off", fallback: str | None = None,
        system_prompt: str | None = None, resume_context: str | None = None,
    ) -> AsyncIterator[RunnerEventT]:
        from types import SimpleNamespace

        from xdsh.runners.dsh_events import DshEventNormalizer

        del task, cwd, model, reasoning_effort, fallback, system_prompt, resume_context
        normalizer = DshEventNormalizer(session_id=session_id, dsh_session_id="native")
        for sequence in range(1, 11):
            self.manual.now = float(sequence * 2)
            notification = SimpleNamespace(method="session.event", payload={
                "sessionId": "native", "event": {
                    "type": "assistant/chunk", "seq": sequence, "data": {
                        "chunk": {"type": "tool-call-delta", "index": 0,
                                  "id": "call-1", "argumentsDelta": self.fragment},
                    },
                },
            })
            for event in normalizer.feed(notification):
                yield event
        self.manual.now = 22.0
        yield FinishEvent(session_id=session_id, reason="completed")


@pytest.mark.asyncio
@pytest.mark.parametrize("fragment,expected", [("", "guarded"), ("new output", "completed")])
async def test_parameter_frames_keep_alive_only_when_content_is_observed(
    tmp_path: Path, fragment: str, expected: str,
) -> None:
    manual = _ManualTime()
    delegate = _ParameterFramesRunner(manual, fragment)
    runner = GuardedRunner(delegate, policy=RunGuardPolicy(stall_timeout_seconds=10),
                           clock=manual.clock, sleep=manual.sleep)
    events = [event async for event in runner.run("test", session_id="test", cwd=tmp_path)]

    assert isinstance(events[-1], FinishEvent)
    assert events[-1].reason == expected
    stops = [event for event in events if isinstance(event, GuardEvent)
             and event.action is GuardAction.STOP]
    if fragment:
        assert not stops
        assert delegate.cancelled == []
    else:
        assert len(stops) == 1
        assert stops[0].reason is GuardReason.STALLED
        assert delegate.cancelled == ["test"]


def test_guard_policy_rejects_non_integer_counter_limits() -> None:
    with pytest.raises(ValueError, match="max_model_steps"):
        RunGuardPolicy(max_model_steps=cast(int, True))


def test_guard_policy_rejects_non_finite_duration_limits() -> None:
    with pytest.raises(ValueError, match="stall_timeout_seconds"):
        RunGuardPolicy(stall_timeout_seconds=float("nan"))
    with pytest.raises(ValueError, match="max_runtime_seconds"):
        RunGuardPolicy(max_runtime_seconds=float("inf"))
    with pytest.raises(ValueError, match="cancel_timeout_seconds"):
        RunGuardPolicy(cancel_timeout_seconds=float("nan"))


def test_default_policy_has_no_absolute_wall_clock_limit() -> None:
    assert RunGuardPolicy().max_runtime_seconds is None


async def _collect(runner: GuardedRunner) -> list[RunnerEventT]:
    return [
        event
        async for event in runner.run(
            "task",
            session_id="session-1",
            cwd=Path.cwd(),
        )
    ]


@pytest.mark.asyncio
async def test_retry_storm_is_stopped_and_partial_answer_is_retained() -> None:
    session_id = "session-1"
    delegate = _ScriptedRunner(
        (
            TextEvent(session_id=session_id, text="useful partial"),
            RetryEvent(session_id=session_id, provider="one"),
            RetryEvent(session_id=session_id, provider="one"),
            RetryEvent(session_id=session_id, provider="one"),
            TextEvent(session_id=session_id, text="must not leak"),
            FinishEvent(session_id=session_id, reason="completed"),
        )
    )
    runner = GuardedRunner(
        delegate,
        policy=RunGuardPolicy(max_retries_without_progress=3, warning_ratio=0.5),
    )

    events = await _collect(runner)

    assert delegate.cancelled == [session_id]
    assert "".join(event.text for event in events if isinstance(event, TextEvent)) == (
        "useful partial"
    )
    guards = [event for event in events if isinstance(event, GuardEvent)]
    assert [(event.action, event.reason) for event in guards] == [
        (GuardAction.WARNING, GuardReason.RETRY_BUDGET),
        (GuardAction.STOP, GuardReason.RETRY_BUDGET),
    ]
    assert isinstance(events[-1], FinishEvent)
    assert events[-1].reason == "guarded"
    assert events[-1].cancelled is False


@pytest.mark.asyncio
async def test_transient_step_retries_cannot_reset_budget_with_partial_activity() -> None:
    source: list[RunnerEventT] = []
    for attempt in (1, 2, 3):
        source.extend((TextEvent(session_id="session-1", text="partial"),
                       ActivityEvent(session_id="session-1"),
                       UsageEvent(session_id="session-1", input_tokens=10, output_tokens=1),
                       RetryEvent(session_id="session-1", failure_code="TIMEOUT",
                                  attempt=attempt, max_attempts=5)))
    source.append(FinishEvent(session_id="session-1", reason="completed"))
    delegate = _ScriptedRunner(tuple(source))
    events = await _collect(GuardedRunner(delegate))
    stops = [e for e in events if isinstance(e, GuardEvent) and e.action is GuardAction.STOP]
    assert len(stops) == 1 and stops[0].reason is GuardReason.RETRY_BUDGET
    assert stops[0].limit == 2 and stops[0].observed == 3
    assert delegate.cancelled == ["session-1"]
    assert isinstance(events[-1], FinishEvent) and events[-1].reason == "guarded"


@pytest.mark.asyncio
async def test_transient_recovery_budget_is_not_a_whole_long_task_limit() -> None:
    source: list[RunnerEventT] = []
    for _ in range(20):
        for attempt in (1, 2):
            source.append(RetryEvent(session_id="session-1", failure_code="TRANSPORT",
                                     attempt=attempt, max_attempts=5))
        source.append(TextEvent(session_id="session-1", text="completed model step", delta=False))
    source.append(FinishEvent(session_id="session-1", reason="completed"))
    delegate = _ScriptedRunner(tuple(source))
    events = await _collect(GuardedRunner(delegate))
    assert not delegate.cancelled
    assert isinstance(events[-1], FinishEvent) and events[-1].reason == "completed"


@pytest.mark.asyncio
async def test_accumulated_token_budget_stops_before_later_events() -> None:
    session_id = "session-1"
    delegate = _ScriptedRunner(
        (
            UsageEvent(session_id=session_id, input_tokens=40, output_tokens=10),
            UsageEvent(session_id=session_id, input_tokens=45, output_tokens=10),
            TextEvent(session_id=session_id, text="too late"),
        )
    )
    runner = GuardedRunner(
        delegate,
        policy=RunGuardPolicy(max_total_tokens=100, warning_ratio=0.8),
    )

    events = await _collect(runner)

    stops = [
        event
        for event in events
        if isinstance(event, GuardEvent) and event.action is GuardAction.STOP
    ]
    assert len(stops) == 1
    assert stops[0].reason is GuardReason.TOKEN_BUDGET
    assert stops[0].observed == 105
    assert not any(
        isinstance(event, TextEvent) and event.text == "too late" for event in events
    )


@pytest.mark.asyncio
async def test_same_structured_tool_call_flail_is_stopped() -> None:
    session_id = "session-1"
    delegate = _ScriptedRunner(
        tuple(
            ToolStartEvent(
                session_id=session_id,
                call_id=f"call-{index}",
                name="bash",
                arguments={"command": "pwd"},
            )
            for index in range(1, 5)
        )
    )
    runner = GuardedRunner(
        delegate,
        policy=RunGuardPolicy(max_same_tool_calls=4, warning_ratio=0.75),
    )

    events = await _collect(runner)

    guards = [event for event in events if isinstance(event, GuardEvent)]
    assert [event.action for event in guards] == [GuardAction.WARNING, GuardAction.STOP]
    assert all(event.reason is GuardReason.REPEATED_TOOL for event in guards)
    assert guards[-1].observed == 4


@pytest.mark.asyncio
async def test_interleaved_identical_calls_cannot_evade_repeat_budget() -> None:
    session_id = "session-1"
    delegate = _ScriptedRunner(
        (
            ToolStartEvent(
                session_id=session_id,
                call_id="same-1",
                name="bash",
                arguments={"command": "touch /outside"},
            ),
            ToolStartEvent(
                session_id=session_id,
                call_id="different-1",
                name="bash",
                arguments={"command": "pwd"},
            ),
            ToolStartEvent(
                session_id=session_id,
                call_id="same-2",
                name="bash",
                arguments={"command": "touch /outside"},
            ),
            ToolStartEvent(
                session_id=session_id,
                call_id="different-2",
                name="bash",
                arguments={"command": "ls"},
            ),
            ToolStartEvent(
                session_id=session_id,
                call_id="same-3",
                name="bash",
                arguments={"command": "touch /outside"},
            ),
        )
    )
    runner = GuardedRunner(
        delegate,
        policy=RunGuardPolicy(max_same_tool_calls=3),
    )

    events = await _collect(runner)

    stop = next(
        event
        for event in events
        if isinstance(event, GuardEvent) and event.action is GuardAction.STOP
    )
    assert stop.reason is GuardReason.REPEATED_TOOL
    assert stop.observed == 3


@pytest.mark.asyncio
async def test_durable_artifact_starts_a_new_repeat_epoch() -> None:
    session_id = "session-1"

    def repeated_call(call_id: str) -> ToolStartEvent:
        return ToolStartEvent(
            session_id=session_id,
            call_id=call_id,
            name="read",
            arguments={"path": "result.txt"},
        )

    delegate = _ScriptedRunner(
        (
            repeated_call("before-1"),
            repeated_call("before-2"),
            ArtifactEvent(
                session_id=session_id,
                path="result.txt",
                operation="write",
            ),
            repeated_call("after-1"),
            repeated_call("after-2"),
            FinishEvent(session_id=session_id, reason="completed"),
        )
    )
    runner = GuardedRunner(
        delegate,
        policy=RunGuardPolicy(max_same_tool_calls=3),
    )

    events = await _collect(runner)

    assert delegate.cancelled == []
    assert not any(
        isinstance(event, GuardEvent) and event.action is GuardAction.STOP
        for event in events
    )


@pytest.mark.asyncio
async def test_silent_event_stream_is_warned_then_stopped_without_real_wait() -> None:
    delegate = _BlockingRunner()
    manual = _ManualTime()
    runner = GuardedRunner(
        delegate,
        policy=RunGuardPolicy(stall_timeout_seconds=10, warning_ratio=0.8),
        clock=manual.clock,
        sleep=manual.sleep,
    )

    events = await _collect(runner)

    assert isinstance(events[0], TextEvent)
    guards = [event for event in events if isinstance(event, GuardEvent)]
    assert [(event.action, event.reason, event.observed) for event in guards] == [
        (GuardAction.WARNING, GuardReason.STALLED, 8),
        (GuardAction.STOP, GuardReason.STALLED, 10),
    ]
    assert delegate.cancelled == ["session-1"]


@pytest.mark.asyncio
async def test_active_tool_gets_longer_stall_grace_without_disabling_stall_guard() -> None:
    delegate = _BlockingToolRunner()
    manual = _ManualTime()
    runner = GuardedRunner(
        delegate,
        policy=RunGuardPolicy(
            stall_timeout_seconds=10,
            active_tool_stall_timeout_seconds=30,
            warning_ratio=0.8,
        ),
        clock=manual.clock,
        sleep=manual.sleep,
    )

    events = await _collect(runner)

    guards = [event for event in events if isinstance(event, GuardEvent)]
    assert [(event.action, event.reason, event.observed, event.limit) for event in guards] == [
        (GuardAction.WARNING, GuardReason.STALLED, 24, 30),
        (GuardAction.STOP, GuardReason.STALLED, 30, 30),
    ]
    assert delegate.cancelled == ["session-1"]


@pytest.mark.asyncio
async def test_content_free_activity_extends_the_stall_deadline() -> None:
    manual = _ManualTime()
    delegate = _TimedActivityRunner(manual)
    runner = GuardedRunner(
        delegate,
        policy=RunGuardPolicy(
            stall_timeout_seconds=10,
            max_runtime_seconds=30,
            warning_ratio=0.8,
        ),
        clock=manual.clock,
        sleep=manual.sleep,
    )

    events = await _collect(runner)

    assert delegate.cancelled == []
    assert any(isinstance(event, ActivityEvent) for event in events)
    assert isinstance(events[-1], FinishEvent)
    assert not any(
        isinstance(event, GuardEvent) and event.reason is GuardReason.STALLED
        for event in events
    )


@pytest.mark.asyncio
async def test_healthy_two_day_run_is_not_stopped_by_wall_clock_age() -> None:
    manual = _ManualTime()
    activity_times = tuple(float(second) for second in range(300, 172_801, 300))
    delegate = _TimedActivityRunner(
        manual,
        activity_times=activity_times,
        finish_at=172_900.0,
    )
    runner = GuardedRunner(
        delegate,
        policy=RunGuardPolicy(stall_timeout_seconds=600),
        clock=manual.clock,
        sleep=manual.sleep,
    )

    events = await _collect(runner)

    assert delegate.cancelled == []
    assert isinstance(events[-1], FinishEvent)
    assert not any(isinstance(event, GuardEvent) for event in events)


@pytest.mark.asyncio
async def test_normal_completion_keeps_single_authoritative_finish() -> None:
    session_id = "session-1"
    delegate = _ScriptedRunner(
        (
            TextEvent(session_id=session_id, text="done"),
            UsageEvent(session_id=session_id, input_tokens=10, output_tokens=2),
            FinishEvent(session_id=session_id, reason="completed", final_text="done"),
        )
    )
    runner = GuardedRunner(delegate)

    events = await _collect(runner)

    assert delegate.cancelled == []
    assert sum(isinstance(event, FinishEvent) for event in events) == 1
    assert not any(isinstance(event, GuardEvent) for event in events)


@pytest.mark.asyncio
async def test_model_step_budget_stops_before_the_observed_203_step_pattern() -> None:
    session_id = "session-1"
    delegate = _ScriptedRunner(
        tuple(
            UsageEvent(session_id=session_id, input_tokens=10, output_tokens=1)
            for _ in range(203)
        )
    )
    runner = GuardedRunner(
        delegate,
        policy=RunGuardPolicy(max_model_steps=120),
    )

    events = await _collect(runner)

    usage = [event for event in events if isinstance(event, UsageEvent)]
    stop = next(
        event
        for event in events
        if isinstance(event, GuardEvent) and event.action is GuardAction.STOP
    )
    assert len(usage) == 120
    assert stop.reason is GuardReason.MODEL_STEP_BUDGET
    assert stop.observed == 120


@pytest.mark.asyncio
async def test_forward_progress_breaks_a_retry_streak() -> None:
    session_id = "session-1"
    delegate = _ScriptedRunner(
        (
            RetryEvent(session_id=session_id),
            RetryEvent(session_id=session_id),
            TextEvent(session_id=session_id, text="recovered"),
            RetryEvent(session_id=session_id),
            RetryEvent(session_id=session_id),
            FinishEvent(session_id=session_id, reason="completed", final_text="done"),
        )
    )
    runner = GuardedRunner(
        delegate,
        policy=RunGuardPolicy(max_retries_without_progress=3),
    )

    events = await _collect(runner)

    assert delegate.cancelled == []
    assert not any(
        isinstance(event, GuardEvent) and event.action is GuardAction.STOP
        for event in events
    )


@pytest.mark.asyncio
async def test_different_tool_arguments_are_not_a_repeated_call_flail() -> None:
    session_id = "session-1"
    delegate = _ScriptedRunner(
        (
            *(
                ToolStartEvent(
                    session_id=session_id,
                    call_id=f"call-{index}",
                    name="read",
                    arguments={"path": f"file-{index}.py"},
                )
                for index in range(8)
            ),
            FinishEvent(session_id=session_id, reason="completed"),
        )
    )
    runner = GuardedRunner(
        delegate,
        policy=RunGuardPolicy(max_same_tool_calls=3),
    )

    events = await _collect(runner)

    assert delegate.cancelled == []
    assert not any(
        isinstance(event, GuardEvent)
        and event.reason is GuardReason.REPEATED_TOOL
        for event in events
    )


@pytest.mark.asyncio
async def test_consecutive_tool_errors_are_stopped() -> None:
    session_id = "session-1"
    delegate = _ScriptedRunner(
        tuple(
            ToolResultEvent(
                session_id=session_id,
                call_id=f"call-{index}",
                name="bash",
                is_error=True,
            )
            for index in range(3)
        )
    )
    runner = GuardedRunner(
        delegate,
        policy=RunGuardPolicy(max_consecutive_tool_errors=3),
    )

    events = await _collect(runner)

    stop = next(
        event
        for event in events
        if isinstance(event, GuardEvent) and event.action is GuardAction.STOP
    )
    assert stop.reason is GuardReason.TOOL_ERROR_BUDGET


@pytest.mark.asyncio
async def test_hard_runtime_budget_stops_even_before_stall_deadline() -> None:
    delegate = _BlockingRunner()
    manual = _ManualTime()
    runner = GuardedRunner(
        delegate,
        policy=RunGuardPolicy(
            stall_timeout_seconds=100,
            max_runtime_seconds=10,
            warning_ratio=0.8,
        ),
        clock=manual.clock,
        sleep=manual.sleep,
    )

    events = await _collect(runner)

    guards = [event for event in events if isinstance(event, GuardEvent)]
    assert [(event.action, event.reason) for event in guards] == [
        (GuardAction.WARNING, GuardReason.TIME_BUDGET),
        (GuardAction.STOP, GuardReason.TIME_BUDGET),
    ]


@pytest.mark.asyncio
async def test_fourth_concurrent_subagent_stops_parent_at_three_agent_ceiling() -> None:
    session_id = "session-1"
    delegate = _ScriptedRunner(
        tuple(
            SubagentEvent(
                session_id=session_id,
                child_session_id=f"child-{index}",
                action=SubagentAction.STARTED,
            )
            for index in range(1, 5)
        )
    )
    runner = GuardedRunner(
        delegate,
        policy=RunGuardPolicy(max_parallel_subagents=3),
    )

    events = await _collect(runner)

    guards = [event for event in events if isinstance(event, GuardEvent)]
    assert [(event.action, event.reason, event.observed, event.limit) for event in guards] == [
        (GuardAction.WARNING, GuardReason.SUBAGENT_BUDGET, 3, 3),
        (GuardAction.STOP, GuardReason.SUBAGENT_BUDGET, 4, 3),
    ]
    assert delegate.cancelled == [session_id]


@pytest.mark.asyncio
async def test_lifecycle_operations_delegate_unchanged() -> None:
    delegate = _ScriptedRunner(())
    runner = GuardedRunner(delegate)

    assert await runner.cancel("session-1") is True
    await runner.reset("session-1")
    await runner.close()

    assert delegate.cancelled == ["session-1"]
    assert delegate.reset_sessions == ["session-1"]
    assert delegate.closed is True
