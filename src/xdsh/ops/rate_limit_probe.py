"""Credential-free synthetic probe for the runner-neutral 429 boundary.

The probe deliberately does not contact a provider, start DSH, or inspect a
response body.  It feeds typed retry notifications into ``RateLimitedRunner``
and verifies the two production invariants that are easiest to regress:

* a structured 429 storm is bounded without replaying the task, while partial
  output is retained; and
* a route that is already occupied times out before the underlying runner is
  started.

Only aggregate counters and terminal reasons are printed, so the command is
safe to run in a candidate image or in CI.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

from xdsh.core.events import (
    ErrorEvent,
    FinishEvent,
    RetryEvent,
    RunnerEventT,
    TextEvent,
)
from xdsh.core.presentation import safe_error_code
from xdsh.core.rate_limit import RateLimitedRunner, RateLimitGate, RateLimitPolicy
from xdsh.core.runner import ReasoningEffort


class _ScriptedRunner:
    """Minimal in-memory AgentRunner used only by the synthetic probe."""

    def __init__(self, *, storm: bool) -> None:
        self.storm = storm
        self.calls = 0
        self.cancelled: list[str] = []

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
        self.calls += 1
        if not self.storm:
            yield FinishEvent(session_id=session_id, reason="completed")
            return
        yield TextEvent(session_id=session_id, text="partial")
        for _ in range(4):
            yield RetryEvent(
                session_id=session_id,
                provider="synthetic-gateway",
                status_code=429,
                retry_after_seconds=999.0,
                rate_limited=True,
            )
        # The wrapper must stop before this sentinel can escape.
        yield TextEvent(session_id=session_id, text="must-not-escape")

    async def cancel(self, session_id: str) -> bool:
        self.cancelled.append(session_id)
        return True

    async def reset(self, session_id: str) -> None:
        del session_id

    async def close(self) -> None:
        return


def _policy(*, retries: int = 2, queue_timeout: float = 0.05) -> RateLimitPolicy:
    return RateLimitPolicy(
        max_concurrent_requests=1,
        min_interval_seconds=0,
        max_rate_limit_retries=retries,
        base_backoff_seconds=0.001,
        max_backoff_seconds=0.01,
        max_retry_after_seconds=0.05,
        acquire_timeout_seconds=queue_timeout,
        jitter_ratio=0,
    )


async def _collect(stream: AsyncIterator[RunnerEventT]) -> list[RunnerEventT]:
    return [event async for event in stream]


async def run_probe() -> dict[str, object]:
    """Run the bounded storm and queue checks and return aggregate evidence."""

    storm_policy = _policy()
    storm_delegate = _ScriptedRunner(storm=True)
    storm_gate = RateLimitGate(storm_policy)
    storm_runner = RateLimitedRunner(
        storm_delegate,
        provider="synthetic",
        policy=storm_policy,
        gate=storm_gate,
    )
    storm_events = await _collect(
        storm_runner.run(
            "synthetic task",
            session_id="probe-storm",
            cwd=Path.cwd(),
            model="synthetic-model",
        )
    )
    partial = "".join(
        event.text for event in storm_events if isinstance(event, TextEvent)
    )
    storm_finish = next(
        (event for event in reversed(storm_events) if isinstance(event, FinishEvent)),
        None,
    )
    storm_error = next(
        (event for event in storm_events if isinstance(event, ErrorEvent)),
        None,
    )
    storm_snapshot = storm_gate.snapshot("synthetic/synthetic-model")
    storm_ok = (
        storm_delegate.calls == 1
        and storm_delegate.cancelled == ["probe-storm"]
        and partial == "partial"
        and isinstance(storm_error, ErrorEvent)
        and storm_error.code == "RATE_LIMIT_EXHAUSTED"
        and isinstance(storm_finish, FinishEvent)
        and storm_finish.reason == "rate_limited"
        and float(storm_snapshot["cooldown_remaining_seconds"]) <= 0.05
    )

    queue_policy = _policy(queue_timeout=0.02)
    queue_delegate = _ScriptedRunner(storm=False)
    queue_gate = RateLimitGate(queue_policy)
    held = await queue_gate.acquire("synthetic/queue-model")
    try:
        queue_runner = RateLimitedRunner(
            queue_delegate,
            provider="synthetic",
            policy=queue_policy,
            gate=queue_gate,
        )
        queue_events = await _collect(
            queue_runner.run(
                "must not start",
                session_id="probe-queue",
                cwd=Path.cwd(),
                model="queue-model",
            )
        )
    finally:
        held.release()
    queue_error = next(
        (event for event in queue_events if isinstance(event, ErrorEvent)),
        None,
    )
    queue_finish = next(
        (event for event in reversed(queue_events) if isinstance(event, FinishEvent)),
        None,
    )
    queue_ok = (
        queue_delegate.calls == 0
        and isinstance(queue_error, ErrorEvent)
        and queue_error.code == "RATE_LIMIT_QUEUE_TIMEOUT"
        and isinstance(queue_finish, FinishEvent)
        and queue_finish.reason == "rate_limited"
    )

    return {
        "schema_version": 1,
        "ok": storm_ok and queue_ok,
        "cases": {
            "bounded_storm": {
                "ok": storm_ok,
                "delegate_calls": storm_delegate.calls,
                "cancel_calls": len(storm_delegate.cancelled),
                "partial_text_chars": len(partial),
                "terminal": (
                    safe_error_code(storm_finish.reason) if storm_finish else "missing"
                ),
                "retry_exhausted": storm_error.code == "RATE_LIMIT_EXHAUSTED"
                if storm_error
                else False,
                "cooldown_cap_seconds": round(
                    float(storm_snapshot["cooldown_remaining_seconds"]),
                    6,
                ),
            },
            "queue_timeout": {
                "ok": queue_ok,
                "delegate_calls": queue_delegate.calls,
                "terminal": (
                    safe_error_code(queue_finish.reason) if queue_finish else "missing"
                ),
                "queue_error": (
                    safe_error_code(queue_error.code) if queue_error else "missing"
                ),
            },
        },
    }


def main() -> int:
    """Print aggregate probe evidence and return a release-gate status."""

    result = asyncio.run(run_probe())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "run_probe"]
