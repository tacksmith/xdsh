"""Research delivery repairs resume evidence work, never replay the original task."""
from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncGenerator
from contextlib import aclosing
from pathlib import Path

import pytest

from xdsh.core.events import (
    ErrorEvent,
    FinishEvent,
    RunnerEventT,
    TextEvent,
    ToolResultEvent,
    UsageEvent,
    VerificationEvent,
)
from xdsh.core.product_control import ProductSessionControl
from xdsh.core.research_feasibility import unknown_feasibility
from xdsh.core.run_guard import GuardedRunner, RunGuardPolicy


class RepairableResearch:
    def __init__(self) -> None:
        self.tasks: list[str] = []
        self.sessions: list[str] = []
        self.options: list[dict[str, object]] = []
        self.closed_segments = 0
        self.side_effects = 0

    async def run(self, task: str, *, session_id: str, cwd: Path,
                  **options: object) -> AsyncGenerator[RunnerEventT, None]:
        self.tasks.append(task)
        self.sessions.append(session_id)
        self.options.append(options)
        try:
            if len(self.tasks) == 1:
                self.side_effects += 1
                manifest = json.loads(task.splitlines()[0].split(": ", 1)[1])["deferred"]["path"]
                (cwd / "method.json").write_text('{"parameters":{"scale":2}}')
                (cwd / manifest).write_text(json.dumps({"research_revision": 2,
                    "feasibility": unknown_feasibility(), "unchanged": ["method.json"], "json": [{
                    "path": "result.json", "source_json": {"/parameters": {
                        "path": "method.json", "pointer": "/parameters"}},
                    "ratios": [{"value": "/rate", "numerator": "/count", "denominator": "/n"}],
                }]}))
                yield ToolResultEvent(session_id=session_id, call_id="declaration", name="write")
                (cwd / "result.json").write_text(
                    '{"parameters":{"scale":2},"count":1,"n":4,"rate":0.5}')
            else:
                assert self.closed_segments == len(self.tasks) - 1
                (cwd / "result.json").write_text(
                    '{"parameters":{"scale":2},"count":1,"n":4,"rate":0.25}')
                yield ToolResultEvent(session_id=session_id, call_id="repair", name="write")
            yield UsageEvent(session_id=session_id, input_tokens=10, output_tokens=5)
            yield FinishEvent(session_id=session_id, reason="completed",
                              final_text="Measurements saved." if len(self.tasks) == 1
                              else "Verified: 1/4 = 0.25.")
        finally:
            self.closed_segments += 1

    async def cancel(self, session_id: str) -> bool:
        return True

    async def reset(self, session_id: str) -> None:
        raise AssertionError("repair must not reset the session")

    async def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_research_delivery_repairs_within_same_task_without_manual_continue(
    tmp_path: Path,
) -> None:
    prepared = ProductSessionControl(tmp_path / "state").prepare_task(
        "s", "Please reproduce the paper analysis.", workspace=tmp_path)
    delegate = RepairableResearch()
    events = [event async for event in GuardedRunner(delegate).run(
        prepared.task, session_id="s", cwd=tmp_path, model="chosen", reasoning_effort="high")]
    assert isinstance(events[-1], FinishEvent) and events[-1].reason == "completed"
    assert events[-1].final_text == "Verified: 1/4 = 0.25."
    assert len(delegate.tasks) == 2
    assert delegate.sessions == ["s", "s"] and delegate.side_effects == 1
    assert delegate.options[1]["model"] == "chosen"
    assert delegate.options[1]["reasoning_effort"] == "high"
    assert delegate.tasks[1] != delegate.tasks[0]
    assert "Do not replay completed operations" in delegate.tasks[1]
    assert "ratio_mismatch" in delegate.tasks[1]
    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert sum(isinstance(event, FinishEvent) for event in events) == 1
    assert sum(event.input_tokens for event in events if isinstance(event, UsageEvent)) == 20
    report = next(index for index, event in enumerate(events) if isinstance(event, TextEvent))
    repair = next(index for index, event in enumerate(events)
                  if isinstance(event, VerificationEvent))
    # Finish-only reports reach durable consumers before continuation.
    assert report < repair


class BrokenRepair(RepairableResearch):
    def __init__(self, mode: str) -> None:
        super().__init__()
        self.mode = mode

    async def run(self, task: str, *, session_id: str, cwd: Path,
                  **options: object) -> AsyncGenerator[RunnerEventT, None]:
        async with aclosing(super().run(task, session_id=session_id, cwd=cwd, **options)) as stream:
            async for event in stream:
                if len(self.tasks) > 1:
                    if self.mode == "exception":
                        raise RuntimeError("SECRET_MUST_NOT_LEAK")
                    if self.mode == "eof":
                        return
                    if isinstance(event, ToolResultEvent):
                        if self.mode == "mutate_source":
                            (cwd / "method.json").write_text('{"parameters":{"scale":3}}')
                        elif self.mode == "mutate_checklist":
                            manifest = next(cwd.glob(".xdsh-research-*.json"))
                            manifest.write_text('{"unchanged":[],"json":[]}')
                        elif self.mode in {"unchanged", "changing"}:
                            value = 0.5 if self.mode == "unchanged" else len(self.tasks)
                            (cwd / "result.json").write_text(json.dumps({
                                "parameters": {"scale": 2}, "count": 1, "n": 4, "rate": value},
                                separators=(",", ":")))
                        elif self.mode == "error":
                            yield ErrorEvent(session_id=session_id, code="INVALID_REQUEST",
                                             message="Permanent rejection")
                            yield FinishEvent(session_id=session_id, reason="error")
                            return
                yield event


async def exercise(tmp_path: Path, delegate: RepairableResearch,
                   *, policy: RunGuardPolicy | None = None) -> list[RunnerEventT]:
    task = ProductSessionControl(tmp_path / "state").prepare_task(
        "s", "Please reproduce the paper analysis.", workspace=tmp_path).task
    return [e async for e in GuardedRunner(delegate, policy=policy).run(
        task, session_id="s", cwd=tmp_path)]


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode", "calls"), [("unchanged", 2), ("changing", 3),
                                             ("mutate_source", 2), ("mutate_checklist", 2)])
async def test_repair_is_bounded_and_never_recaptures_before_state(
    tmp_path: Path, mode: str, calls: int,
) -> None:
    delegate = BrokenRepair(mode)
    events = await exercise(tmp_path, delegate)
    assert len(delegate.tasks) == calls and delegate.side_effects == 1
    assert isinstance(events[-1], FinishEvent) and events[-1].reason == "error"
    assert sum(isinstance(e, FinishEvent) for e in events) == 1
    if mode.startswith("mutate"):
        assert any(isinstance(e, ErrorEvent) and "inputs_changed" in e.verification_issues
                   for e in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["exception", "eof", "error"])
async def test_uncertain_execution_never_replayed_and_previous_report_retained(
    tmp_path: Path, mode: str,
) -> None:
    delegate = BrokenRepair(mode)
    events = await exercise(tmp_path, delegate)
    assert len(delegate.tasks) == 2 and delegate.side_effects == 1
    assert isinstance(events[-1], FinishEvent) and events[-1].reason == "error"
    assert events[-1].final_text == "Measurements saved."
    assert sum(isinstance(e, ErrorEvent) for e in events) == 1
    assert "SECRET_MUST_NOT_LEAK" not in repr(events)


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["source", "authority", "cancel"])
async def test_changed_sources_at_repair_handoff_prevent_new_execution(
    tmp_path: Path, changed: str,
) -> None:
    workspace = tmp_path / "work"
    workspace.mkdir()
    task = ProductSessionControl(tmp_path / "state").prepare_task(
        "s", "Please reproduce the paper analysis.", workspace=workspace).task
    delegate = RepairableResearch()
    events: list[RunnerEventT] = []
    guard = GuardedRunner(delegate, acceptance_root=tmp_path / "authority")
    async for event in guard.run(
        task, session_id="s", cwd=workspace,
    ):
        events.append(event)
        if isinstance(event, VerificationEvent):
            # A consumer/other actor can change sources while the normalized
            # stream is yielded. The completed check is no longer sufficient
            # authority for dispatching another execution segment.
            if changed == "authority":
                from xdsh.core.acceptance_store import AcceptanceStore

                contract = json.loads(task.splitlines()[0].split(": ", 1)[1])["deferred"]["path"]
                AcceptanceStore(tmp_path / "authority").path_for(workspace, contract).unlink()
            else:
                (workspace / "method.json").write_text('{"parameters":{"scale":3}}')
                if changed == "cancel":
                    assert await guard.cancel("s")
    assert delegate.side_effects == 1
    assert len(delegate.tasks) == 1
    assert isinstance(events[-1], FinishEvent)
    assert events[-1].reason == ("cancelled" if changed == "cancel" else "error")
    assert events[-1].final_text == "Measurements saved."
    if changed == "source":
        assert any(isinstance(event, ErrorEvent) and "inputs_changed" in event.verification_issues
                   for event in events)
    elif changed == "authority":
        assert any(isinstance(event, ErrorEvent) and event.code == "ACCEPTANCE_INVALID"
                   for event in events)
    else:
        assert not any(isinstance(event, ErrorEvent) for event in events)


@pytest.mark.asyncio
async def test_cancel_at_segment_boundary_prevents_repair_launch(tmp_path: Path) -> None:
    delegate = RepairableResearch()
    runner = GuardedRunner(delegate)
    task = ProductSessionControl(tmp_path / "state").prepare_task(
        "s", "Please reproduce the paper analysis.", workspace=tmp_path).task
    events = []
    async for event in runner.run(task, session_id="s", cwd=tmp_path):
        events.append(event)
        if isinstance(event, VerificationEvent):
            assert await runner.cancel("s")
    assert len(delegate.tasks) == 1
    assert isinstance(events[-1], FinishEvent) and events[-1].cancelled
    assert events[-1].final_text == "Measurements saved."


@pytest.mark.asyncio
async def test_cancel_acknowledged_during_validation_is_not_reported_as_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from xdsh.core.artifact_acceptance import AcceptanceResult, ArtifactAcceptance

    entered, release = threading.Event(), threading.Event()
    evaluate = ArtifactAcceptance.evaluate

    def slow_check(snapshot: ArtifactAcceptance) -> AcceptanceResult:
        entered.set()
        assert release.wait(3)
        return evaluate(snapshot)

    monkeypatch.setattr(ArtifactAcceptance, "evaluate", slow_check)
    delegate = RepairableResearch()
    runner = GuardedRunner(delegate, research_repair_attempts=0)
    task = ProductSessionControl(tmp_path / "state").prepare_task(
        "s", "Please reproduce the paper analysis.", workspace=tmp_path).task

    async def consume() -> list[RunnerEventT]:
        return [e async for e in runner.run(task, session_id="s", cwd=tmp_path)]

    pending = asyncio.create_task(consume())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        assert await runner.cancel("s")
    finally:
        release.set()
    events = await asyncio.wait_for(pending, 2)
    assert isinstance(events[-1], FinishEvent) and events[-1].reason == "cancelled"
    assert events[-1].cancelled and events[-1].final_text == "Measurements saved."
    assert not any(isinstance(e, ErrorEvent) for e in events)
    assert len(delegate.tasks) == 1


@pytest.mark.asyncio
async def test_repair_does_not_reset_token_budget_or_discard_partial_report(tmp_path: Path) -> None:
    delegate = RepairableResearch()
    events = await exercise(tmp_path, delegate, policy=RunGuardPolicy(max_total_tokens=25))
    assert len(delegate.tasks) == 2
    assert isinstance(events[-1], FinishEvent) and events[-1].reason == "guarded"
    assert events[-1].final_text == "Measurements saved."


@pytest.mark.parametrize("invalid", [-1, 3, True, 0.5])
def test_repair_configuration_cannot_be_unbounded(invalid: int) -> None:
    with pytest.raises(ValueError):
        GuardedRunner(RepairableResearch(), research_repair_attempts=invalid)


def test_repair_progress_is_localized_without_false_error_or_premature_finish() -> None:
    from xdsh.core.i18n import language_codes
    from xdsh.telegram.render import ProgressRenderer, TurnRenderer

    event = VerificationEvent(session_id="s", attempt=1, max_attempts=2, issues=("numbers",))
    for language in language_codes():
        renderer = TurnRenderer(language=language, max_answer_chars=60000)
        progress = ProgressRenderer(language=language)
        notice = renderer.accept(event)
        progress.accept(event, elapsed_seconds=123)
        card = progress.render(final=False, elapsed_seconds=123)
        assert notice and "1/2" in notice and card and notice in card
        assert renderer.error is None and renderer.finish is None and not progress.finished
        if language == "en":
            assert "Repairing delivery" in card and "修复" not in card


@pytest.mark.asyncio
async def test_production_wrapper_chain_releases_native_session_before_repair(
    tmp_path: Path,
) -> None:
    from xdsh.core.fallback import FallbackRunner
    from xdsh.core.rate_limit import RateLimitedRunner, RateLimitPolicy

    delegate = RepairableResearch()
    limited = RateLimitedRunner(delegate, policy=RateLimitPolicy(min_interval_seconds=0))
    runner = GuardedRunner(FallbackRunner(limited))
    task = ProductSessionControl(tmp_path / "state").prepare_task(
        "s", "Please reproduce the paper analysis.", workspace=tmp_path).task
    events = [e async for e in runner.run(task, session_id="s", cwd=tmp_path)]
    assert isinstance(events[-1], FinishEvent) and events[-1].reason == "completed"
    assert delegate.closed_segments == 2 and delegate.side_effects == 1
