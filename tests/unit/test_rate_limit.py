from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from xdsh.core.events import (
    ActivityEvent,
    ErrorEvent,
    FinishEvent,
    GuardEvent,
    GuardReason,
    RetryEvent,
    RunnerEventT,
    TextEvent,
    ToolStartEvent,
    UsageEvent,
)
from xdsh.core.rate_limit import (
    RateLimitedRunner,
    RateLimitGate,
    RateLimitPolicy,
    classify_rate_limit,
    parse_retry_after,
    rate_limit_policy_from_env,
)
from xdsh.core.run_guard import GuardedRunner
from xdsh.core.runner import ReasoningEffort
from xdsh.runners.dsh_events import DshEventNormalizer
from xdsh.telegram.render import ProgressRenderer, TurnRenderer


class _ScriptedRunner:
    def __init__(self, events: tuple[RunnerEventT, ...]) -> None:
        self.events = events
        self.cancelled: list[str] = []
        self.started = 0

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
        self.started += 1
        for event in self.events:
            yield event

    async def cancel(self, session_id: str) -> bool:
        self.cancelled.append(session_id)
        return True

    async def reset(self, session_id: str) -> None:
        del session_id

    async def close(self) -> None:
        return


def _policy(
    *,
    max_rate_limit_retries: int = 2,
    acquire_timeout_seconds: float = 1,
) -> RateLimitPolicy:
    return RateLimitPolicy(
        max_concurrent_requests=1,
        min_interval_seconds=0,
        max_rate_limit_retries=max_rate_limit_retries,
        base_backoff_seconds=1,
        max_backoff_seconds=5,
        max_retry_after_seconds=5,
        acquire_timeout_seconds=acquire_timeout_seconds,
        jitter_ratio=0,
    )


def test_default_429_recovery_window_can_cross_a_minute_boundary() -> None:
    policy = rate_limit_policy_from_env({})
    delays = [min(policy.max_backoff_seconds, policy.base_backoff_seconds * 2**i)
              for i in range(policy.max_rate_limit_retries)]
    assert sum(delays) >= 60
    assert sum(delays) <= 240  # finite recovery episode, not indefinite task replay
    assert policy == RateLimitPolicy()


@pytest.mark.parametrize("language", ["zh-CN", "zh-TW", "en"])
def test_scheduled_retry_wait_is_visible_even_without_provider_hint(language: str) -> None:
    event = RetryEvent(session_id="s", rate_limited=True, retry_delay_seconds=40,
                       retry_after_seconds=90, attempt=4, max_attempts=5)
    progress = ProgressRenderer(language=language)
    progress.accept(event)
    card = progress.render(final=False, elapsed_seconds=120) or ""
    message = TurnRenderer(language=language, max_answer_chars=1000).accept(event) or ""
    assert "40.0s" in card and "40.0s" in message
    assert "90.0s" not in card + message  # chosen delay wins, not an unconsumed hint


@pytest.mark.asyncio
async def test_default_retry_chain_can_recover_after_four_429s_without_task_replay() -> None:
    delegate = _ScriptedRunner((
        TextEvent(session_id="s", text="completed earlier work"),
        *(RetryEvent(session_id="s", rate_limited=True, status_code=429,
                     attempt=i, max_attempts=6) for i in range(1, 5)),
        ToolStartEvent(session_id="s", call_id="next", name="read"),
        FinishEvent(session_id="s", reason="completed"),
    ))
    runner = GuardedRunner(RateLimitedRunner(delegate))
    events = [e async for e in runner.run("task", session_id="s", cwd=Path.cwd())]
    assert not delegate.cancelled
    assert isinstance(events[-1], FinishEvent) and events[-1].reason == "completed"
    assert sum(isinstance(e, ToolStartEvent) for e in events) == 1
    assert delegate.started == 1


@pytest.mark.asyncio
async def test_dedicated_429_budget_is_not_preempted_by_generic_retry_guard() -> None:
    delegate = _ScriptedRunner((
        *(RetryEvent(session_id="s", rate_limited=True, status_code=429,
                     attempt=i, max_attempts=8) for i in range(1, 9)),
        TextEvent(session_id="s", text="recovered"),
        FinishEvent(session_id="s", reason="completed"),
    ))
    runner = GuardedRunner(RateLimitedRunner(delegate, policy=_policy(max_rate_limit_retries=8)))
    events = [e async for e in runner.run("task", session_id="s", cwd=Path.cwd())]
    assert not delegate.cancelled
    assert not any(isinstance(e, GuardEvent) for e in events)
    assert isinstance(events[-1], FinishEvent) and events[-1].reason == "completed"
    assert delegate.started == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("protected", [False, True])
async def test_generic_retries_remain_bounded_when_interleaved_with_429s(protected: bool) -> None:
    sequence: list[RunnerEventT] = []
    for _ in range(6):
        sequence.append(RetryEvent(session_id="s"))
        sequence.append(RetryEvent(session_id="s", rate_limited=True, status_code=429))
    delegate = _ScriptedRunner(tuple(sequence))
    runner = GuardedRunner(
        RateLimitedRunner(delegate, policy=_policy(max_rate_limit_retries=10))
        if protected else delegate
    )
    events = [e async for e in runner.run("task", session_id="s", cwd=Path.cwd())]
    assert delegate.cancelled == ["s"]
    assert any(isinstance(e, GuardEvent) and e.reason == GuardReason.RETRY_BUDGET
               for e in events)
    assert isinstance(events[-1], FinishEvent) and events[-1].reason == "guarded"


@pytest.mark.asyncio
async def test_standalone_generic_guard_still_bounds_429_storm() -> None:
    delegate = _ScriptedRunner(tuple(
        RetryEvent(session_id="s", status_code=429) for _ in range(7)
    ))
    events = [e async for e in GuardedRunner(delegate).run("task", session_id="s", cwd=Path.cwd())]
    assert delegate.cancelled == ["s"]
    assert isinstance(events[-1], FinishEvent) and events[-1].reason == "guarded"


def test_retry_after_accepts_seconds_and_http_date() -> None:
    assert parse_retry_after("2.5") == 2.5
    origin = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert parse_retry_after("Fri, 01 Jan 2026 00:00:05 GMT", now_wall=origin) == 5.0
    assert parse_retry_after("-1") is None
    assert parse_retry_after("not-a-date") is None


def test_rate_limit_classifier_requires_429_evidence() -> None:
    assert classify_rate_limit(code="RATE_LIMIT", message="retry") is not None
    assert classify_rate_limit(message="last status 429 Too Many Requests") is not None
    assert classify_rate_limit(code="TIMEOUT", message="upstream timeout") is None
    assert classify_rate_limit(code="RATE_LIMIT", status_code=999) is not None


def test_rate_limit_policy_reads_bounded_environment_knobs() -> None:
    policy = rate_limit_policy_from_env(
        {
            "XDSH_RATE_LIMIT_MAX_CONCURRENT_REQUESTS": "1",
            "XDSH_RATE_LIMIT_MIN_INTERVAL_SECONDS": "0.5",
            "XDSH_RATE_LIMIT_MAX_RETRIES": "2",
            "XDSH_RATE_LIMIT_BASE_BACKOFF_SECONDS": "2",
            "XDSH_RATE_LIMIT_MAX_BACKOFF_SECONDS": "12",
            "XDSH_RATE_LIMIT_MAX_RETRY_AFTER_SECONDS": "60",
            "XDSH_RATE_LIMIT_QUEUE_TIMEOUT_SECONDS": "90",
            "XDSH_RATE_LIMIT_JITTER_RATIO": "0",
            "XDSH_RATE_LIMIT_ALLOW_FALLBACK": "yes",
        }
    )
    assert policy.max_concurrent_requests == 1
    assert policy.min_interval_seconds == 0.5
    assert policy.max_rate_limit_retries == 2
    assert policy.max_retry_after_seconds == 60
    assert policy.allow_fallback_on_rate_limit is True
    assert policy.summary == "1inflight/500ms/2retries/2-12s-backoff/cap60s/90s-queue+fallback"


@pytest.mark.asyncio
async def test_gate_preserves_cancellation_when_a_slot_becomes_available() -> None:
    # Cancellation and admission can arrive in the same loop turn. Python 3.10's
    # wait_for must not turn a cancelled caller into an admitted model request.
    gate = RateLimitGate(replace(_policy(), min_interval_seconds=0.1))
    first = await gate.acquire("provider/model")
    waiting = asyncio.create_task(gate.acquire("provider/model"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    first.release()
    await asyncio.sleep(0)
    waiting.cancel()
    try:
        lease = await waiting
    except asyncio.CancelledError:
        pass
    else:
        lease.release()
        pytest.fail("a cancelled admission returned a request lease")

    # Neither the cancelled acquisition nor its cleanup may leak the only slot.
    following = await gate.acquire("other/model")
    following.release()


@pytest.mark.asyncio
async def test_gate_limits_concurrent_route_leases() -> None:
    gate = RateLimitGate(_policy(acquire_timeout_seconds=2))
    first = await gate.acquire("provider/model")
    waiting = asyncio.create_task(gate.acquire("provider/model"))
    await asyncio.sleep(0)
    assert not waiting.done()
    first.release()
    second = await waiting
    second.release()


@pytest.mark.asyncio
async def test_retry_storm_stops_before_provider_retry_limit_and_keeps_partial() -> None:
    session = "s1"
    delegate = _ScriptedRunner(
        (
            TextEvent(session_id=session, text="partial"),
            RetryEvent(session_id=session, provider="litellm", status_code=429, rate_limited=True),
            RetryEvent(session_id=session, provider="litellm", status_code=429, rate_limited=True),
            RetryEvent(session_id=session, provider="litellm", status_code=429, rate_limited=True),
            TextEvent(session_id=session, text="must-not-leak"),
        )
    )
    runner = RateLimitedRunner(
        delegate,
        provider="litellm-anthropic",
        policy=_policy(max_rate_limit_retries=2),
    )

    events = [
        event
        async for event in runner.run(
            "task",
            session_id=session,
            cwd=Path.cwd(),
            model="ark-code-latest",
        )
    ]

    assert delegate.cancelled == [session]
    assert "".join(event.text for event in events if isinstance(event, TextEvent)) == "partial"
    assert any(
        isinstance(event, ErrorEvent) and event.code == "RATE_LIMIT_EXHAUSTED"
        for event in events
    )
    assert isinstance(events[-1], FinishEvent)
    assert events[-1].reason == "rate_limited"


@pytest.mark.asyncio
async def test_terminal_429_is_normalized_without_replaying_task() -> None:
    session = "s2"
    delegate = _ScriptedRunner(
        (
            TextEvent(session_id=session, text="answer before failure"),
            ErrorEvent(
                session_id=session,
                message="last status 429 too many requests",
                code="DSH_TURN_ERROR",
            ),
            FinishEvent(session_id=session, reason="error"),
        )
    )
    gate = RateLimitGate(_policy())
    runner = RateLimitedRunner(delegate, policy=_policy(), gate=gate)
    events = [
        event
        async for event in runner.run(
            "task",
            session_id=session,
            cwd=Path.cwd(),
            model="model",
        )
    ]
    error = next(event for event in events if isinstance(event, ErrorEvent))
    assert error.code == "RATE_LIMIT"
    assert error.status_code == 429
    assert error.retryable is True
    assert isinstance(events[-1], FinishEvent)
    assert events[-1].reason == "rate_limited"
    assert gate.snapshot("unknown/model")["failures"] == 1
    assert gate.snapshot("unknown/model")["cooldown_remaining_seconds"] >= 0


@pytest.mark.asyncio
async def test_successful_model_tool_calls_reset_rate_limit_episode() -> None:
    session = "long-task"
    retries = tuple(
        event
        for index in range(4)
        for event in (
            RetryEvent(session_id=session, status_code=429, rate_limited=True,
                       attempt=1, max_attempts=3),
            ToolStartEvent(session_id=session, call_id=f"call-{index}", name="read"),
        )
    )
    delegate = _ScriptedRunner((*retries, FinishEvent(session_id=session, reason="completed")))
    runner = RateLimitedRunner(delegate, policy=_policy(max_rate_limit_retries=2))
    events = [event async for event in runner.run("task", session_id=session, cwd=Path.cwd())]
    assert delegate.cancelled == []
    assert isinstance(events[-1], FinishEvent) and events[-1].reason == "completed"
    # One upstream notification must appear once, including its successful recovery.
    assert sum(isinstance(event, RetryEvent) for event in events) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 2])
async def test_allowed_retry_is_forwarded_once_and_last_budgeted_retry_can_succeed(
    limit: int,
) -> None:
    delegate = _ScriptedRunner((
        RetryEvent(session_id="s", rate_limited=True, attempt=1, max_attempts=1),
        FinishEvent(session_id="s", reason="completed"),
    ))
    runner = RateLimitedRunner(delegate, policy=_policy(max_rate_limit_retries=limit))
    events = [e async for e in runner.run("task", session_id="s", cwd=Path.cwd())]
    assert delegate.cancelled == []
    assert sum(isinstance(e, RetryEvent) for e in events) == 1
    assert isinstance(events[-1], FinishEvent) and events[-1].reason == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("reported_output_tokens", [0, 10])
async def test_heartbeat_or_partial_delta_does_not_reset_retry_storm(
    reported_output_tokens: int,
) -> None:
    delegate = _ScriptedRunner((
        RetryEvent(session_id="s", rate_limited=True),
        ActivityEvent(session_id="s"),
        TextEvent(session_id="s", text="partial", delta=True),
        UsageEvent(session_id="s", input_tokens=10, output_tokens=reported_output_tokens),
        RetryEvent(session_id="s", rate_limited=True),
        FinishEvent(session_id="s", reason="completed"),
    ))
    runner = RateLimitedRunner(delegate, policy=_policy(max_rate_limit_retries=1))
    events = [e async for e in runner.run("task", session_id="s", cwd=Path.cwd())]
    assert delegate.cancelled == ["s"]
    assert isinstance(events[-1], FinishEvent) and events[-1].reason == "rate_limited"


@pytest.mark.asyncio
async def test_recovered_retry_does_not_relabel_a_later_unrelated_error() -> None:
    delegate = _ScriptedRunner((
        RetryEvent(session_id="s", rate_limited=True),
        ToolStartEvent(session_id="s", call_id="a", name="read"),
        ErrorEvent(session_id="s", code="TIMEOUT", message="unrelated tool failure"),
        FinishEvent(session_id="s", reason="error"),
    ))
    runner = RateLimitedRunner(delegate, policy=_policy())
    events = [e async for e in runner.run("task", session_id="s", cwd=Path.cwd())]
    assert isinstance(events[-1], FinishEvent) and events[-1].reason == "error"


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["TIMEOUT", "AUTH", "INVALID_REQUEST", "QUOTA_EXCEEDED"])
async def test_later_explicit_failure_is_not_assumed_to_be_another_429(code: str) -> None:
    delegate = _ScriptedRunner((
        RetryEvent(session_id="s", rate_limited=True),
        ErrorEvent(session_id="s", code=code, message="typed terminal failure"),
        FinishEvent(session_id="s", reason="error"),
    ))
    events = [e async for e in RateLimitedRunner(delegate).run(
        "task", session_id="s", cwd=Path.cwd(),
    )]
    assert isinstance(events[-1], FinishEvent) and events[-1].reason == "error"
    assert delegate.started == 1 and not delegate.cancelled


@pytest.mark.asyncio
async def test_observed_rc2_episodes_reach_progress_once_and_keep_partial_answer() -> None:
    # Sanitized shape/counts of R2: steps 63/64/78 recovered, step 79 ended in 429.
    # No credentials, prompt, reasoning or workspace content from the real task.
    normalizer = DshEventNormalizer(session_id="s", dsh_session_id="internal")
    native: list[RunnerEventT] = [TextEvent(session_id="s", text="saved partial")]
    sequence = 0

    def feed(kind: str, data: dict[str, object]) -> None:
        nonlocal sequence
        sequence += 1
        native.extend(normalizer.feed(SimpleNamespace(method="session.event", payload={
            "sessionId": "internal", "event": {"type": kind, "seq": sequence, "data": data},
        })))

    for step, count in ((63, 3), (64, 2), (78, 1), (79, 3)):
        for attempt in range(1, count + 1):
            data: dict[str, object] = {
                "step": step, "retry": attempt, "maxRetries": 3, "delayMs": 1000,
                "failure": {"code": "RATE_LIMIT", "message": "rate limited"},
            }
            feed("llm/retry", data)
            feed("llm/retry-started", data)
        if step != 79:
            feed("tool/call", {"callId": str(step), "name": "read", "arguments": {}})
    feed("turn/end", {"reason": {"kind": "error", "error": {
        "code": "RATE_LIMIT", "message": "rate limited"}}})
    native.append(FinishEvent(session_id="s", reason="error"))
    delegate = _ScriptedRunner(tuple(native))
    runner = RateLimitedRunner(delegate, policy=_policy(max_rate_limit_retries=3))
    events = [e async for e in runner.run("task", session_id="s", cwd=Path.cwd())]
    assert delegate.cancelled == []  # the native error, not a premature host stop
    assert sum(isinstance(e, RetryEvent) for e in events) == 9
    assert "".join(e.text for e in events if isinstance(e, TextEvent)) == "saved partial"
    assert isinstance(events[-1], FinishEvent) and events[-1].reason == "rate_limited"
    progress = ProgressRenderer(language="zh-CN")
    for event in events:
        progress.accept(event)
    assert progress.rate_limit_retry_count == 9
    assert "重试 9 次" in (progress.render(final=True, elapsed_seconds=1575) or "")


@pytest.mark.asyncio
async def test_gate_queue_timeout_emits_bounded_terminal_state() -> None:
    gate = RateLimitGate(_policy(acquire_timeout_seconds=0.05))
    lease = await gate.acquire("p/m")
    runner = RateLimitedRunner(
        _ScriptedRunner(()),
        policy=_policy(acquire_timeout_seconds=0.05),
        gate=gate,
    )
    events = [
        event
        async for event in runner.run(
            "task",
            session_id="s3",
            cwd=Path.cwd(),
            model="m",
        )
    ]
    lease.release()
    assert isinstance(events[0], ErrorEvent)
    assert events[0].code == "RATE_LIMIT_QUEUE_TIMEOUT"
    assert isinstance(events[-1], FinishEvent)
    assert events[-1].reason == "rate_limited"


@pytest.mark.asyncio
async def test_default_pacing_does_not_false_timeout_a_sequential_turn() -> None:
    """A normal pacing delay must not be mistaken for a queue deadline."""

    session = "paced"
    delegate = _ScriptedRunner(
        (FinishEvent(session_id=session, reason="completed", final_text="ok"),)
    )
    policy = RateLimitPolicy(
        max_concurrent_requests=1,
        min_interval_seconds=0.01,
        max_rate_limit_retries=1,
        base_backoff_seconds=1,
        max_backoff_seconds=2,
        max_retry_after_seconds=2,
        acquire_timeout_seconds=1,
        jitter_ratio=0,
    )
    runner = RateLimitedRunner(delegate, provider="p", policy=policy)

    for index in range(2):
        events = [
            event
            async for event in runner.run(
                "task",
                session_id=f"{session}-{index}",
                cwd=Path.cwd(),
                model="m",
            )
        ]
        assert isinstance(events[-1], FinishEvent)
        assert events[-1].reason == "completed"


@pytest.mark.asyncio
async def test_rate_signal_without_finish_cannot_trigger_an_implicit_replay() -> None:
    class AbruptRunner(_ScriptedRunner):
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
            yield RetryEvent(
                session_id=session_id,
                status_code=429,
                rate_limited=True,
            )

    runner = RateLimitedRunner(
        AbruptRunner(()),
        policy=_policy(max_rate_limit_retries=3),
    )
    events = [
        event
        async for event in runner.run(
            "task",
            session_id="abrupt",
            cwd=Path.cwd(),
            model="m",
        )
    ]
    assert isinstance(events[-1], FinishEvent)
    assert events[-1].reason == "rate_limited"
    assert any(
        isinstance(event, ErrorEvent) and event.code == "RATE_LIMIT_EXHAUSTED"
        for event in events
    )
