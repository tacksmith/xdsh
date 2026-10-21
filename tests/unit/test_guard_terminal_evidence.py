"""Confirmed terminal states must not discard already observed answer evidence."""
from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from xdsh.core.events import (
    ErrorEvent,
    FinishEvent,
    GuardAction,
    GuardEvent,
    RunnerEventT,
    TextEvent,
    ToolResultEvent,
    UsageEvent,
)
from xdsh.core.run_guard import GuardedRunner, RunGuardPolicy


class TerminalRunner:
    """External runner seam; no XDSH implementation is replaced by a mock."""

    def __init__(self, events: tuple[RunnerEventT, ...], *, raises: bool = False) -> None:
        self.events = events
        self.raises = raises
        self.runs = 0

    async def run(self, task: str, *, session_id: str, cwd: Path,
                  **options: object) -> AsyncIterator[RunnerEventT]:
        self.runs += 1
        for event in self.events:
            yield event
        if self.raises:
            raise RuntimeError("DO_NOT_EXPOSE_THE_EXTERNAL_EXCEPTION")

    async def cancel(self, session_id: str) -> bool:
        return True

    async def reset(self, session_id: str) -> None:
        pass

    async def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_unconfirmed_stream_preserves_observed_text_without_replaying(tmp_path: Path) -> None:
    delegate = TerminalRunner((
        TextEvent(session_id="s", text="Measured "),
        TextEvent(session_id="s", text="signals."),
    ))
    events = [event async for event in GuardedRunner(delegate).run(
        "Analyze the saved measurement.", session_id="s", cwd=tmp_path,
    )]
    assert delegate.runs == 1
    assert sum(isinstance(event, FinishEvent) for event in events) == 1
    assert isinstance(events[-1], FinishEvent)
    assert events[-1].reason == "error"
    assert events[-1].final_text == "Measured signals."
    assert [event.code for event in events if isinstance(event, ErrorEvent)] == [
        "RUNNER_STREAM_LOST",
    ]


@pytest.mark.asyncio
async def test_error_cannot_be_relabelled_completed_by_a_later_finish(tmp_path: Path) -> None:
    delegate = TerminalRunner((
        TextEvent(session_id="s", text="Measured signals."),
        ErrorEvent(session_id="s", code="TIMEOUT", message="Execution not confirmed"),
        FinishEvent(session_id="s", reason="completed"),
    ))
    events = [event async for event in GuardedRunner(delegate).run(
        "Analyze the saved measurement.", session_id="s", cwd=tmp_path,
    )]
    assert delegate.runs == 1
    assert isinstance(events[-1], FinishEvent)
    assert events[-1].reason == "error"
    assert events[-1].final_text == "Measured signals."
    assert [event.code for event in events if isinstance(event, ErrorEvent)] == ["TIMEOUT"]


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["exception", "error", "cancelled", "guarded", "completed"])
async def test_observed_snapshot_replaces_deltas_and_survives_each_ending(
    tmp_path: Path, ending: str,
) -> None:
    emitted: list[RunnerEventT] = [
        TextEvent(session_id="s", text="Outdated draft."),
        TextEvent(session_id="s", text="Measured", delta=False),
        TextEvent(session_id="s", text=" "),
        TextEvent(session_id="s", text="signals."),
    ]
    if ending == "guarded":
        emitted.append(UsageEvent(session_id="s", input_tokens=2, output_tokens=0))
    elif ending != "exception":
        if ending == "error":
            emitted.append(ErrorEvent(session_id="s", code="INVALID_REQUEST",
                                      message="Permanent failure"))
        emitted.append(FinishEvent(session_id="s", reason=ending, cancelled=ending == "cancelled"))
    delegate = TerminalRunner(tuple(emitted), raises=ending == "exception")
    policy = RunGuardPolicy(max_total_tokens=1) if ending == "guarded" else None
    events = [event async for event in GuardedRunner(delegate, policy=policy).run(
        "Analyze the saved measurement.", session_id="s", cwd=tmp_path,
    )]
    assert delegate.runs == 1
    assert isinstance(events[-1], FinishEvent)
    assert events[-1].reason == ("error" if ending == "exception" else ending)
    assert events[-1].final_text == "Measured signals."
    assert sum(isinstance(event, FinishEvent) for event in events) == 1
    assert "DO_NOT_EXPOSE_THE_EXTERNAL_EXCEPTION" not in repr(events)


@pytest.mark.asyncio
async def test_authoritative_finish_text_is_not_appended_or_replaced(tmp_path: Path) -> None:
    delegate = TerminalRunner((
        TextEvent(session_id="s", text="Observed partial."),
        FinishEvent(session_id="s", reason="completed", final_text="Authoritative final."),
    ))
    events = [event async for event in GuardedRunner(delegate).run(
        "Analyze the saved measurement.", session_id="s", cwd=tmp_path,
    )]
    assert isinstance(events[-1], FinishEvent)
    assert events[-1].final_text == "Authoritative final."


@pytest.mark.asyncio
async def test_old_generation_text_cannot_be_laundered_into_current_finish(tmp_path: Path) -> None:
    class Generations(TerminalRunner):
        generation = "runtime-a"

        async def runtime_generation(self, session_id: str) -> str:
            return self.generation

        async def run(self, task: str, *, session_id: str, cwd: Path,
                      **options: object) -> AsyncIterator[RunnerEventT]:
            self.runs += 1
            yield TextEvent(session_id=session_id, text="Measured ", runtime_generation="runtime-a")
            self.generation = "runtime-b"
            yield TextEvent(session_id=session_id, text="STALE", runtime_generation="runtime-a")
            yield TextEvent(session_id=session_id, text="signals.", runtime_generation="runtime-b")
            yield FinishEvent(session_id=session_id, reason="completed",
                              runtime_generation="runtime-b")

    delegate = Generations(())
    events = [event async for event in GuardedRunner(delegate).run(
        "Analyze the saved measurement.", session_id="s", cwd=tmp_path,
    )]
    assert isinstance(events[-1], FinishEvent)
    assert events[-1].final_text == "Measured signals."
    assert delegate.runs == 1


@pytest.mark.asyncio
async def test_nontext_sequence_and_old_errors_cannot_poison_terminal_evidence(
    tmp_path: Path,
) -> None:
    class Generations(TerminalRunner):
        async def runtime_generation(self, session_id: str) -> str:
            return "runtime-b"

    delegate = Generations((
        TextEvent(session_id="s", text="Measured ", sequence=1, runtime_generation="runtime-b"),
        ToolResultEvent(session_id="s", call_id="c", name="read", sequence=5,
                        runtime_generation="runtime-b"),
        TextEvent(session_id="s", text="STALE", sequence=4, runtime_generation="runtime-b"),
        ErrorEvent(session_id="s", code="TIMEOUT", message="OLD_ERROR", sequence=100,
                   runtime_generation="runtime-a"),
        TextEvent(session_id="s", text="signals.", sequence=6, runtime_generation="runtime-b"),
        FinishEvent(session_id="s", reason="completed", sequence=7, runtime_generation="runtime-b"),
    ))
    events = [event async for event in GuardedRunner(delegate).run(
        "Analyze the saved measurement.", session_id="s", cwd=tmp_path,
    )]
    assert isinstance(events[-1], FinishEvent)
    assert events[-1].reason == "completed"
    assert events[-1].final_text == "Measured signals."
    assert not any(isinstance(event, ErrorEvent) for event in events)


@pytest.mark.asyncio
async def test_rejected_frame_flood_does_not_disable_the_stall_clock(tmp_path: Path) -> None:
    now = [0.0]

    class Generations(TerminalRunner):
        async def runtime_generation(self, session_id: str) -> str:
            return "runtime-b"

        async def run(self, task: str, *, session_id: str, cwd: Path,
                      **options: object) -> AsyncIterator[RunnerEventT]:
            self.runs += 1
            yield TextEvent(session_id=session_id, text="Measured signals.",
                            runtime_generation="runtime-b")
            now[0] = 2.0
            for _ in range(4):
                yield TextEvent(session_id=session_id, text="STALE", runtime_generation="runtime-a")
            yield FinishEvent(session_id=session_id, reason="completed",
                              runtime_generation="runtime-b")

    delegate = Generations(())
    guard = GuardedRunner(delegate, clock=lambda: now[0],
                          policy=RunGuardPolicy(stall_timeout_seconds=1))
    events = [event async for event in guard.run(
        "Analyze the saved measurement.", session_id="s", cwd=tmp_path,
    )]
    assert isinstance(events[-1], FinishEvent)
    assert events[-1].reason == "guarded"
    assert events[-1].final_text == "Measured signals."
    assert any(isinstance(event, GuardEvent) and event.action is GuardAction.STOP
               for event in events)
