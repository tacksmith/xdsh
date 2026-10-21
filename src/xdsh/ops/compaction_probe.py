"""Bounded, answer-free live probe for the official DSH compaction path.

The probe deliberately records only event kinds, bounded usage measurements,
terminal reasons, and elapsed time.  It never writes a prompt, model output,
tool argument, tool result, or credential to its JSON report.  A fresh root is
required so an old session log cannot make a missing compaction event look
healthy.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import shlex
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from xdsh.core.events import (
    CompactionAction,
    CompactionEvent,
    ErrorEvent,
    FinishEvent,
    GuardAction,
    GuardEvent,
    RunnerEventT,
    UsageEvent,
)
from xdsh.core.presentation import safe_error_code
from xdsh.core.run_guard import GuardedRunner, RunGuardPolicy
from xdsh.runners.dsh import DshRunner, DshRunnerConfig

_SUCCESS_REASONS = frozenset({"completed", "complete", "success", "succeeded"})
_MIN_CONTEXT_WINDOW = 8_192
_MAX_CONTEXT_WINDOW = 10_000_000
_MIN_PAYLOAD_CHARS = 1_000
_MAX_PAYLOAD_CHARS = 20_000
_MIN_TURNS = 2
_MAX_TURNS = 12


@dataclass(frozen=True, slots=True, kw_only=True)
class CompactionProbeConfig:
    """Validated limits for one isolated native-compaction probe."""

    root: Path
    model: str
    runtime_command: tuple[str, ...]
    context_window: int = 16_384
    model_max_tokens: int = 1_024
    max_tokens: int = 512
    threshold_ratio: float = 0.50
    retain_ratio: float = 0.16
    turns: int = 4
    payload_chars: int = 12_000
    turn_timeout_seconds: float = 180.0
    max_recoverable_compaction_errors: int = 1

    def __post_init__(self) -> None:
        root = self.root.expanduser().resolve()
        if root == Path(root.anchor) or ".xclaude" in root.parts or ".dsh" in root.parts:
            raise ValueError("probe root must be a private non-reserved directory")
        if not self.model.strip() or any(character.isspace() for character in self.model):
            raise ValueError("probe model must be a non-empty ID")
        if not self.runtime_command or any(not item for item in self.runtime_command):
            raise ValueError("probe runtime command must be explicit and non-empty")
        if not _MIN_CONTEXT_WINDOW <= self.context_window <= _MAX_CONTEXT_WINDOW:
            raise ValueError("probe context window is outside the safe range")
        if self.model_max_tokens <= 0 or self.model_max_tokens >= self.context_window:
            raise ValueError("probe model_max_tokens must be below the context window")
        if self.max_tokens <= 0 or self.max_tokens > self.model_max_tokens:
            raise ValueError("probe max_tokens must be positive and fit model_max_tokens")
        if not math.isfinite(self.threshold_ratio) or not 0.30 <= self.threshold_ratio < 0.90:
            raise ValueError("probe threshold ratio must be between 0.30 and 0.90")
        if not math.isfinite(self.retain_ratio) or not (
            0.05 <= self.retain_ratio < self.threshold_ratio
        ):
            raise ValueError("probe retain ratio must be below the threshold")
        if not _MIN_TURNS <= self.turns <= _MAX_TURNS:
            raise ValueError("probe turns must be between 2 and 12")
        if not _MIN_PAYLOAD_CHARS <= self.payload_chars <= _MAX_PAYLOAD_CHARS:
            raise ValueError("probe payload_chars is outside the safe range")
        if not 15.0 <= self.turn_timeout_seconds <= 600.0:
            raise ValueError("probe turn timeout must be between 15 and 600 seconds")
        if not 0 <= self.max_recoverable_compaction_errors <= 3:
            raise ValueError("probe recoverable compaction errors must be between 0 and 3")


@dataclass(frozen=True, slots=True, kw_only=True)
class CompactionProbeAnalysis:
    """Content-free assessment of one normalized event stream."""

    passed: bool
    complete_cycles: int
    compaction_cycles: int
    compaction_events: int
    compaction_errors: int
    recoverable_compaction_errors: int
    unrecovered_compaction_errors: int
    max_recoverable_error_burst: int
    malformed_cycles: int
    peak_context_tokens: int
    pre_compaction_peak_tokens: int
    post_compaction_min_tokens: int | None
    post_compaction_below_trigger: bool
    finish_reasons: tuple[str, ...]
    error_codes: tuple[str, ...]
    guard_reasons: tuple[str, ...]
    event_counts: dict[str, int]

    def as_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "complete_cycles": self.complete_cycles,
            "compaction_cycles": self.compaction_cycles,
            "compaction_events": self.compaction_events,
            "compaction_errors": self.compaction_errors,
            "recoverable_compaction_errors": self.recoverable_compaction_errors,
            "unrecovered_compaction_errors": self.unrecovered_compaction_errors,
            "max_recoverable_error_burst": self.max_recoverable_error_burst,
            "malformed_cycles": self.malformed_cycles,
            "peak_context_tokens": self.peak_context_tokens,
            "pre_compaction_peak_tokens": self.pre_compaction_peak_tokens,
            "post_compaction_min_tokens": self.post_compaction_min_tokens,
            "post_compaction_below_trigger": self.post_compaction_below_trigger,
            "finish_reasons": list(self.finish_reasons),
            "error_codes": list(self.error_codes),
            "guard_reasons": list(self.guard_reasons),
            "event_counts": dict(self.event_counts),
        }


def analyze_compaction_events(
    events: Sequence[RunnerEventT],
    *,
    threshold_tokens: int,
    max_recoverable_compaction_errors: int = 1,
) -> CompactionProbeAnalysis:
    """Assess lifecycle/order and pressure reduction without reading content.

    ``max_recoverable_compaction_errors`` is a consecutive-attempt budget. The
    official compaction backend applies ``compactionRetries`` independently to
    each pressure/overflow trigger, so a process-wide total would incorrectly
    reject two healthy triggers that each recovered on their first retry.
    """

    if threshold_tokens <= 0:
        raise ValueError("threshold_tokens must be positive")
    if not 0 <= max_recoverable_compaction_errors <= 3:
        raise ValueError("max_recoverable_compaction_errors must be between 0 and 3")

    compaction_events = [event for event in events if isinstance(event, CompactionEvent)]
    usage_events = [event for event in events if isinstance(event, UsageEvent)]
    raw_finish_reasons = tuple(
        event.reason for event in events if isinstance(event, FinishEvent)
    )
    finish_reasons = tuple(safe_error_code(reason) for reason in raw_finish_reasons)
    error_codes = tuple(
        safe_error_code(event.code) for event in events if isinstance(event, ErrorEvent)
    )
    guard_reasons = tuple(
        event.reason.value
        for event in events
        if isinstance(event, GuardEvent) and event.action is GuardAction.STOP
    )
    event_counts: dict[str, int] = {}
    for event in events:
        name = event.kind.value
        event_counts[name] = event_counts.get(name, 0) + 1
    contexts = [_context_tokens(event) for event in usage_events]
    peak_context = max(contexts, default=0)

    cycles: list[tuple[list[CompactionEvent], int, int]] = []
    malformed = 0
    current: list[CompactionEvent] | None = None
    current_start: int | None = None
    for index, event in enumerate(events):
        if not isinstance(event, CompactionEvent):
            continue
        if event.action is CompactionAction.START:
            if current is not None:
                malformed += 1
            current = [event]
            current_start = index
            continue
        if current is None:
            malformed += 1
            continue
        current.append(event)
        if event.action is CompactionAction.END:
            assert current_start is not None
            cycles.append((current, current_start, index))
            current = None
            current_start = None
    if current is not None:
        malformed += 1

    compaction_errors = sum(event.error for event in compaction_events)
    complete_cycles = 0
    failed_cycles: list[tuple[int, int]] = []
    first_start_index: int | None = None
    first_end_index: int | None = None
    for cycle, start, end in cycles:
        valid_lifecycle = _is_valid_compaction_lifecycle(cycle)
        if not valid_lifecycle:
            malformed += 1
        elif any(event.error for event in cycle):
            failed_cycles.append((start, end))
        else:
            complete_cycles += 1
        if first_start_index is None:
            first_start_index = start
            first_end_index = end

    pre_contexts: list[int] = []
    post_contexts: list[int] = []
    if first_start_index is not None and first_end_index is not None:
        pre_contexts = [
            _context_tokens(event)
            for event in events[:first_start_index]
            if isinstance(event, UsageEvent)
        ]
        post_contexts = [
            _context_tokens(event)
            for event in events[first_end_index + 1 :]
            if isinstance(event, UsageEvent)
        ]
    pre_peak = max(pre_contexts, default=0)
    post_min = min(post_contexts) if post_contexts else None
    below_trigger = post_min is not None and post_min < threshold_tokens
    successful_starts = [
        start
        for cycle, start, _end in cycles
        if not any(event.error for event in cycle)
        and _is_valid_compaction_lifecycle(cycle)
    ]
    all_finished = bool(raw_finish_reasons) and all(
        reason.casefold() in _SUCCESS_REASONS for reason in finish_reasons
    )
    recoverable_compaction_errors = sum(
        any(success_start > failed_end for success_start in successful_starts)
        or (complete_cycles > 0 and below_trigger and all_finished)
        for _failed_start, failed_end in failed_cycles
    )
    # Count failed *lifecycles*, not error-bearing fields. The official wire
    # normally emits one ``end(error)`` marker, but a forward-compatible peer
    # may attach the same failure to more than one phase. Such duplication must
    # not inflate the unrecovered count or turn a bounded retry into a false
    # failure.
    unrecovered_compaction_errors = sum(
        1
        for failed_start, failed_end in failed_cycles
        if not (
            any(success_start > failed_end for success_start in successful_starts)
            or (complete_cycles > 0 and below_trigger and all_finished)
        )
    )
    # A retry budget is per trigger. With no explicit trigger ID in the
    # runner-neutral event contract, the safest portable approximation is the
    # largest consecutive run of failed closed lifecycles before a successful
    # lifecycle. This still rejects retry storms while allowing independent
    # triggers to each use one bounded retry.
    max_recoverable_error_burst = 0
    current_error_burst = 0
    current_turn: int | None = None
    for cycle, _start, _end in cycles:
        cycle_turns = {event.turn for event in cycle if event.turn is not None}
        cycle_turn = next(iter(cycle_turns)) if len(cycle_turns) == 1 else None
        if any(event.error for event in cycle):
            # Alpha.2 repeats an automatic attempt under the same open turn.
            # Prefer that explicit owner when present; if a peer omits it,
            # retain the conservative event-order burst below.
            if cycle_turn is not None and current_turn not in {None, cycle_turn}:
                current_error_burst = 0
            elif cycle_turn is None and current_turn is not None:
                current_error_burst = 0
            current_turn = cycle_turn
            current_error_burst += 1
            continue
        max_recoverable_error_burst = max(
            max_recoverable_error_burst,
            current_error_burst,
        )
        current_error_burst = 0
        current_turn = cycle_turn
    max_recoverable_error_burst = max(
        max_recoverable_error_burst,
        current_error_burst,
    )
    passed = bool(
        complete_cycles
        and not malformed
        and max_recoverable_error_burst <= max_recoverable_compaction_errors
        and not unrecovered_compaction_errors
        and not error_codes
        and not guard_reasons
        and all_finished
        and below_trigger
    )
    return CompactionProbeAnalysis(
        passed=passed,
        complete_cycles=complete_cycles,
        compaction_cycles=len(cycles),
        compaction_events=len(compaction_events),
        compaction_errors=compaction_errors,
        recoverable_compaction_errors=recoverable_compaction_errors,
        unrecovered_compaction_errors=unrecovered_compaction_errors,
        max_recoverable_error_burst=max_recoverable_error_burst,
        malformed_cycles=malformed,
        peak_context_tokens=peak_context,
        pre_compaction_peak_tokens=pre_peak,
        post_compaction_min_tokens=post_min,
        post_compaction_below_trigger=below_trigger,
        finish_reasons=finish_reasons,
        error_codes=error_codes,
        guard_reasons=guard_reasons,
        event_counts=dict(sorted(event_counts.items())),
    )


def _is_valid_compaction_lifecycle(cycle: Sequence[CompactionEvent]) -> bool:
    """Return whether one cycle follows rc.2 success or failure ordering.

    The official automatic backend records a failed summary as
    ``start -> end(error)`` with no summary event. That is a closed,
    recoverable attempt rather than a malformed stream. Successful cycles
    still require the full ``start -> prune* -> summary -> end`` sequence.
    """

    actions = [event.action for event in cycle]
    if not actions or actions[0] is not CompactionAction.START:
        return False
    if actions[-1] is not CompactionAction.END:
        return False
    summary_count = actions.count(CompactionAction.SUMMARY)
    if any(event.error for event in cycle) and summary_count == 0:
        return all(action is CompactionAction.PRUNE for action in actions[1:-1])
    if summary_count != 1:
        return False
    summary_index = actions.index(CompactionAction.SUMMARY)
    return summary_index == len(actions) - 2 and all(
        action is CompactionAction.PRUNE for action in actions[1:summary_index]
    )


async def _collect(stream: AsyncIterator[RunnerEventT]) -> tuple[RunnerEventT, ...]:
    return tuple([event async for event in stream])


async def run_probe(config: CompactionProbeConfig) -> dict[str, object]:
    """Run bounded pressure turns in a fresh XDSH-owned root."""

    root = config.root.expanduser().resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError("probe root must be empty; use a fresh isolated path")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    workspace = root / "workspace"
    workspace.mkdir(mode=0o700, exist_ok=True)
    session_id = f"compact-probe-{uuid.uuid4().hex[:12]}"
    threshold_tokens = max(1, math.floor(config.context_window * config.threshold_ratio))
    runner = GuardedRunner(
        DshRunner(
            DshRunnerConfig(
                dsh_home=root / "dsh-home",
                model=config.model,
                runtime_command=config.runtime_command,
                context_window=config.context_window,
                model_max_tokens=config.model_max_tokens,
                max_tokens=config.max_tokens,
                auto_compaction_threshold_ratio=config.threshold_ratio,
                auto_compaction_retain_ratio=config.retain_ratio,
                request_timeout_seconds=config.turn_timeout_seconds,
                cold_start_timeout_seconds=min(120.0, config.turn_timeout_seconds),
            )
        ),
        policy=RunGuardPolicy(
            max_model_steps=24,
            max_tool_calls=24,
            max_same_tool_calls=8,
            max_consecutive_tool_errors=4,
            max_retries_without_progress=4,
            max_total_tokens=200_000,
            max_parallel_subagents=1,
            stall_timeout_seconds=min(180.0, config.turn_timeout_seconds),
            max_runtime_seconds=config.turn_timeout_seconds,
            cancel_timeout_seconds=8.0,
        ),
    )
    all_events: list[RunnerEventT] = []
    started = time.monotonic()
    completed_turns = 0
    try:
        if "automatic_compaction" not in runner.capabilities.native:
            raise ValueError("selected DSH composition does not advertise native compaction")
        for turn in range(config.turns):
            prompt = _pressure_prompt(turn, config.payload_chars)
            events = await asyncio.wait_for(
                _collect(
                    runner.run(
                        prompt,
                        session_id=session_id,
                        cwd=workspace,
                    )
                ),
                timeout=config.turn_timeout_seconds,
            )
            all_events.extend(events)
            finishes = [event for event in events if isinstance(event, FinishEvent)]
            if (
                len(finishes) != 1
                or finishes[0].reason.casefold() not in _SUCCESS_REASONS
                or finishes[0].cancelled
            ):
                break
            completed_turns += 1
    except (asyncio.TimeoutError, TimeoutError):
        # The analyzer reports a failed bounded run without echoing exception
        # text; cancellation is best effort and remains scoped to this ID.
        await runner.cancel(session_id)
    finally:
        await runner.close()

    analysis = analyze_compaction_events(
        all_events,
        threshold_tokens=threshold_tokens,
        max_recoverable_compaction_errors=config.max_recoverable_compaction_errors,
    )
    result = {
        "suite": "xdsh-native-compaction-probe",
        "passed": analysis.passed and completed_turns == config.turns,
        "model": config.model,
        "context_window_tokens": config.context_window,
        "threshold_tokens": threshold_tokens,
        "threshold_ratio": config.threshold_ratio,
        "retain_ratio": config.retain_ratio,
        "max_recoverable_compaction_errors": config.max_recoverable_compaction_errors,
        "turns_requested": config.turns,
        "turns_completed": completed_turns,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "native_capability": True,
        **analysis.as_dict(),
    }
    return result


def _pressure_prompt(turn: int, payload_chars: int) -> str:
    """Create deterministic, non-secret pressure without asking for tools."""

    prefix = (
        f"Compaction pressure sample {turn + 1}. Keep the marker "
        "XDSH_COMPACTION_MARKER stable and reply exactly XDSH_COMPACTION_OK. "
        "Do not call tools.\n"
    )
    words = (
        "amber cedar cobalt delta ember fern granite harbor indigo juniper "
        "kiwi linen maple nickel olive peach quartz river saffron thistle "
        "umber violet willow xenon yellow zephyr"
    ).split()
    seed = " ".join(words[(index + turn) % len(words)] for index in range(len(words)))
    body = (seed + " ") * ((payload_chars // (len(seed) + 1)) + 2)
    body = body[:payload_chars]
    return prefix + body


def _context_tokens(event: UsageEvent) -> int:
    return max(
        0,
        event.input_tokens,
    ) + max(0, event.cache_read_tokens) + max(0, event.cache_write_tokens)


def _runtime_command(value: str | None) -> tuple[str, ...]:
    raw = value or os.environ.get("XDSH_DSH_RUNTIME_COMMAND") or ""
    command = tuple(shlex.split(raw))
    if not command:
        raise ValueError("XDSH_DSH_RUNTIME_COMMAND is required")
    return command


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--model", default=os.environ.get("XDSH_LITELLM_MODEL", ""))
    parser.add_argument("--runtime-command")
    parser.add_argument("--context-window", type=int, default=16_384)
    parser.add_argument("--model-max-tokens", type=int, default=1_024)
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--threshold-ratio", type=float, default=0.50)
    parser.add_argument("--retain-ratio", type=float, default=0.16)
    parser.add_argument("--turns", type=int, default=4)
    parser.add_argument("--payload-chars", type=int, default=12_000)
    parser.add_argument("--turn-timeout-seconds", type=float, default=180.0)
    parser.add_argument("--max-recoverable-compaction-errors", type=int, default=1)
    args = parser.parse_args(argv)
    try:
        config = CompactionProbeConfig(
            root=args.root,
            model=args.model,
            runtime_command=_runtime_command(args.runtime_command),
            context_window=args.context_window,
            model_max_tokens=args.model_max_tokens,
            max_tokens=args.max_tokens,
            threshold_ratio=args.threshold_ratio,
            retain_ratio=args.retain_ratio,
            turns=args.turns,
            payload_chars=args.payload_chars,
            turn_timeout_seconds=args.turn_timeout_seconds,
            max_recoverable_compaction_errors=args.max_recoverable_compaction_errors,
        )
        result = asyncio.run(run_probe(config))
    except (OSError, ValueError, TimeoutError, asyncio.TimeoutError) as error:
        result = {
            "suite": "xdsh-native-compaction-probe",
            "passed": False,
            "error_type": type(error).__name__,
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("passed") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
