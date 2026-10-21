"""Four-case smoke test for the external DSH runner."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import time
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from xdsh.core.events import (
    ErrorEvent,
    FinishEvent,
    ReasoningEvent,
    RetryEvent,
    RunnerEvent,
    RunnerEventT,
    TextEvent,
    ToolResultEvent,
    ToolStartEvent,
)
from xdsh.core.presentation import safe_error_code
from xdsh.core.product_control import safe_exception_text
from xdsh.core.rate_limit import RateLimitedRunner, RateLimitPolicy
from xdsh.runners.dsh import DshRunner, DshRunnerConfig


class SmokeFailure(RuntimeError):
    """A smoke invariant did not hold."""


# Keep failure diagnostics useful in Telegram/CI logs without allowing an
# event body, prompt, tool argument, or provider error message to cross the
# smoke boundary.  This is deliberately smaller than the normal progress
# excerpt limit because it is often embedded in a one-line JSON failure.
_MAX_DIAGNOSTIC_SUMMARY_CHARS = 640


@dataclass(frozen=True, slots=True)
class SmokeCase:
    name: str
    session_id: str
    elapsed_seconds: float
    event_kinds: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "status": "passed",
            "session_id": self.session_id,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "event_kinds": list(self.event_kinds),
        }


def _require_pty() -> None:
    """Fail before model work when the current process cannot allocate a PTY."""

    try:
        master_fd, slave_fd = os.openpty()
    except OSError as error:
        raise SmokeFailure(
            "PTY allocation is unavailable in the current execution context. "
            "Run xdsh-smoke as a top-level process in an isolated container; do not launch "
            "it from a DSH Bash tool because the inherited workspace-write sandbox blocks "
            "nested PTY allocation."
        ) from error
    os.close(master_fd)
    os.close(slave_fd)


async def _collect(stream: AsyncIterator[RunnerEventT]) -> list[RunnerEventT]:
    return [event async for event in stream]


def _text(events: Sequence[RunnerEventT]) -> str:
    finishes = [event.final_text for event in events if isinstance(event, FinishEvent)]
    return "\n".join(finishes)


def _continuation_prompts(marker: str) -> tuple[str, str]:
    boundary = "Do not use tools, files, Skills or durable memory."
    return (
        f'For this conversation only, the opaque marker is "{marker}". '
        f'{boundary} Keep it in conversation context for my next message. Reply exactly "STORED".',
        f"What opaque marker did I give in the previous message? {boundary} "
        "Reply with that marker only, using this conversation context.",
    )


def _require_context_only(events: Sequence[RunnerEventT]) -> None:
    if any(isinstance(event, ToolStartEvent) for event in events):
        raise _invariant_failure("context-only continuation unexpectedly invoked a tool", events)


def _diagnostic_atom(value: str, *, limit: int = 32) -> str:
    """Return a bounded, content-free label for failure metadata."""

    # Provider error codes/reasons are metadata, but some runtimes put free
    # text in those fields.  Reuse the product-wide redactor before applying
    # the short enum-like diagnostic bound.
    return safe_error_code(value)[:limit] or "unknown"


def _event_diagnostic_summary(events: Iterable[RunnerEvent]) -> str:
    """Summarize a failed smoke stream without exposing event content.

    The summary is intentionally based only on structured event metadata and
    lengths.  It is safe to put in the JSON emitted by :func:`main`, where a
    marker mismatch otherwise gives operators no clue whether the runtime
    started, produced text, retried, or finished.
    """

    materialized = tuple(events)
    if not materialized:
        return "events=none"

    counts = Counter(event.kind.value for event in materialized)
    count_text = ",".join(f"{kind}={counts[kind]}" for kind in sorted(counts))
    parts = [f"events={count_text}"]

    text_chars = sum(
        len(event.text)
        for event in materialized
        if isinstance(event, TextEvent)
    )
    reasoning_chars = sum(
        len(event.text) for event in materialized if isinstance(event, ReasoningEvent)
    )
    if text_chars:
        parts.append(f"text_chars={text_chars}")
    if reasoning_chars:
        parts.append(f"reasoning_chars={reasoning_chars}")

    finishes = [event for event in materialized if isinstance(event, FinishEvent)]
    if finishes:
        last_finish = finishes[-1]
        reason = _diagnostic_atom(last_finish.reason)
        parts.append(
            f"finish_reason={reason},cancelled={str(last_finish.cancelled).lower()},"
            f"final_chars={len(last_finish.final_text)}"
        )

    errors = [event for event in materialized if isinstance(event, ErrorEvent)]
    retries = [event for event in materialized if isinstance(event, RetryEvent)]
    if errors:
        codes = sorted({_diagnostic_atom(event.code) for event in errors})
        parts.append(f"error_codes={','.join(codes[:8])}")
    statuses = sorted(
        {
            *(str(event.status_code) for event in errors if event.status_code is not None),
            *(str(event.status_code) for event in retries if event.status_code is not None),
        }
    )
    if statuses:
        parts.append(f"upstream_statuses={','.join(statuses[:8])}")

    tool_errors = sum(
        1 for event in materialized if isinstance(event, ToolResultEvent) and event.is_error
    )
    if tool_errors:
        parts.append(f"tool_errors={tool_errors}")

    summary = "; ".join(parts)
    if len(summary) <= _MAX_DIAGNOSTIC_SUMMARY_CHARS:
        return summary
    return summary[: _MAX_DIAGNOSTIC_SUMMARY_CHARS - 3] + "..."


def _invariant_failure(message: str, events: Iterable[RunnerEvent]) -> SmokeFailure:
    """Build a safe, actionable failure while retaining the strict invariant."""

    return SmokeFailure(f"{message}; {_event_diagnostic_summary(events)}")


def _case(name: str, session_id: str, started: float, events: Sequence[RunnerEventT]) -> SmokeCase:
    return SmokeCase(
        name=name,
        session_id=session_id,
        elapsed_seconds=time.monotonic() - started,
        event_kinds=tuple(event.kind.value for event in events),
    )


async def _run_smoke(args: argparse.Namespace) -> list[SmokeCase]:
    root = Path(args.root).expanduser().resolve()
    if ".xclaude" in root.parts:
        raise SmokeFailure("smoke root must not be inside a reserved legacy root")
    if root == Path(root.anchor) or root == Path.home().resolve():
        raise SmokeFailure("smoke root must be a dedicated subdirectory")
    _require_pty()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    workspace = root / "workspace"
    dsh_home = root / "dsh-home"
    workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
    workspace.chmod(0o700)
    dsh_home.mkdir(mode=0o700, parents=True, exist_ok=True)
    dsh_home.chmod(0o700)

    runtime_command = tuple(shlex.split(args.runtime_command or ""))
    if not runtime_command:
        raise SmokeFailure("an explicit DSH runtime command is required")
    rate_limit_policy = RateLimitPolicy()
    runner = RateLimitedRunner(
        DshRunner(
            DshRunnerConfig(
                dsh_home=dsh_home,
                model=args.model,
                runtime_command=runtime_command,
                request_timeout_seconds=args.task_timeout,
                rate_limit_policy=rate_limit_policy,
            )
        ),
        provider="litellm",
        policy=rate_limit_policy,
    )
    run_id = uuid.uuid4().hex[:10]
    cases: list[SmokeCase] = []

    async def turn(prompt: str, session_id: str) -> list[RunnerEventT]:
        return await asyncio.wait_for(
            _collect(runner.run(prompt, session_id=session_id, cwd=workspace)),
            timeout=args.task_timeout,
        )

    try:
        once_marker = f"XDSH_ONCE_{run_id}"
        once_session = f"smoke-once-{run_id}"
        started = time.monotonic()
        once_events = await turn(
            f'Reply with exactly "{once_marker}". Do not use a tool.', once_session
        )
        if once_marker not in _text(once_events):
            raise _invariant_failure("one-task response did not contain its marker", once_events)
        cases.append(_case("one_task", once_session, started, once_events))

        memory_marker = f"XDSH_MEMORY_{run_id}"
        continuation_session = f"smoke-continuation-{run_id}"
        started = time.monotonic()
        seed_prompt, recall_prompt = _continuation_prompts(memory_marker)
        first_events = await turn(seed_prompt, continuation_session)
        _require_context_only(first_events)
        if "STORED" not in _text(first_events):
            raise _invariant_failure(
                "continuation seed turn did not finish as requested", first_events
            )
        second_events = await turn(recall_prompt, continuation_session)
        _require_context_only(second_events)
        if memory_marker not in _text(second_events):
            raise _invariant_failure(
                "stable session did not recall the prior-turn marker", second_events
            )
        cases.append(
            _case(
                "multi_turn_continuation",
                continuation_session,
                started,
                [*first_events, *second_events],
            )
        )

        tool_marker = f"XDSH_TOOL_{run_id}"
        tool_session = f"smoke-tool-{run_id}"
        started = time.monotonic()
        tool_events = await turn(
            f'Use Bash to run pwd. Then reply with "{tool_marker}" and the directory.',
            tool_session,
        )
        if not any(isinstance(event, ToolStartEvent) for event in tool_events):
            raise _invariant_failure(
                "tool task emitted no structured tool_start notification", tool_events
            )
        if not any(
            isinstance(event, ToolResultEvent) and not event.is_error for event in tool_events
        ):
            raise _invariant_failure(
                "tool task emitted no successful structured tool_result notification", tool_events
            )
        if tool_marker not in _text(tool_events):
            raise _invariant_failure("tool task did not complete with its marker", tool_events)
        cases.append(_case("tool_call", tool_session, started, tool_events))

        cancel_session = f"smoke-cancel-{run_id}"
        cancel_events: list[RunnerEventT] = []
        tool_started = asyncio.Event()

        async def collect_cancel() -> None:
            async for event in runner.run(
                "Use Bash to run sleep 30, wait for it, then reply XDSH_CANCEL_MISSED.",
                session_id=cancel_session,
                cwd=workspace,
            ):
                cancel_events.append(event)
                if isinstance(event, ToolStartEvent):
                    tool_started.set()

        started = time.monotonic()
        cancel_task = asyncio.create_task(collect_cancel())
        try:
            await asyncio.wait_for(tool_started.wait(), timeout=args.task_timeout)
            if not await runner.cancel(cancel_session):
                raise _invariant_failure(
                    "runner reported no active task to cancel", cancel_events
                )
            await asyncio.wait_for(cancel_task, timeout=15.0)
        finally:
            if not cancel_task.done():
                cancel_task.cancel()
                await asyncio.gather(cancel_task, return_exceptions=True)
        if not any(
            isinstance(event, FinishEvent) and event.cancelled for event in cancel_events
        ):
            raise _invariant_failure(
                "cancel task emitted no cancelled finish event", cancel_events
            )
        cases.append(_case("cancel", cancel_session, started, cancel_events))
        return cases
    finally:
        await runner.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="isolated smoke state/workspace root")
    parser.add_argument(
        "--model",
        default=os.environ.get("XDSH_LITELLM_MODEL"),
        required=os.environ.get("XDSH_LITELLM_MODEL") is None,
    )
    runtime_command = os.environ.get("XDSH_DSH_RUNTIME_COMMAND")
    parser.add_argument(
        "--runtime-command",
        default=runtime_command,
        required=runtime_command is None,
        help="explicit DSH JSON-RPC runtime command; parsed as argv without a shell",
    )
    parser.add_argument("--task-timeout", type=float, default=300.0)
    return parser


def main() -> None:
    args = _parser().parse_args()
    try:
        cases = asyncio.run(_run_smoke(args))
    except BaseException as error:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_type": type(error).__name__,
                    "error": safe_exception_text(error, max_chars=640),
                },
                ensure_ascii=False,
            )
        )
        raise SystemExit(1) from error
    print(
        json.dumps(
            {"status": "passed", "cases": [case.as_dict() for case in cases]},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
