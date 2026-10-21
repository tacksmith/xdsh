"""Long-duration, content-free XDSH soak testing over a real agent runner.

The durable artifacts contain only aggregate event metadata. Prompts, model
answers, tool arguments/results, credentials, and workspace paths never cross
the reporting boundary.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import shlex
import time
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from xdsh.core.events import (
    ErrorEvent,
    FinishEvent,
    GuardAction,
    GuardEvent,
    ReasoningEvent,
    RetryEvent,
    RunnerEventT,
    TextEvent,
    ToolResultEvent,
    ToolStartEvent,
    UsageEvent,
)
from xdsh.core.presentation import safe_error_code
from xdsh.core.product_control import safe_exception_text
from xdsh.core.rate_limit import RateLimitedRunner, RateLimitPolicy
from xdsh.core.run_guard import GuardedRunner, RunGuardPolicy
from xdsh.core.runner import AgentRunner
from xdsh.runners.dsh import DshRunner, DshRunnerConfig

MINIMUM_SOAK_SECONDS = 10 * 60 * 60
MAXIMUM_SOAK_SECONDS = 7 * 24 * 60 * 60
_SUCCESS_REASONS = frozenset({"completed", "complete", "success", "succeeded"})
_DEFAULT_RUNTIME_COMMAND = (
    "/usr/local/bin/node",
    "/opt/xdsh/runtime/node_modules/@deepseek-ai/dsh-sdk-jsonrpc-demo/lib/packaged-bin.js",
)

Clock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True, kw_only=True)
class SoakConfig:
    """Validated public contract for one isolated local-model soak run."""

    root: Path
    model: str
    required_model: str
    runtime_command: tuple[str, ...]
    duration_seconds: float = MINIMUM_SOAK_SECONDS
    cycle_interval_seconds: float = 60.0
    turn_timeout_seconds: float = 900.0
    max_total_tokens: int = 10_000_000
    max_consecutive_failures: int = 3
    session_rotation_cycles: int = 10
    cancel_every_cycles: int = 30
    max_parallel_tool_calls: int = 4
    resume: bool = False
    provider: str = "litellm"
    max_tokens: int = 1_024
    model_max_tokens: int = 32_768
    context_window: int = 262_144
    max_concurrent_requests: int = 2

    def __post_init__(self) -> None:
        root = self.root.expanduser().resolve()
        if (
            root == Path(root.anchor)
            or root == Path.home().resolve()
            or ".xclaude" in root.parts
            or ".dsh" in root.parts
        ):
            raise ValueError("soak root must be a private non-reserved directory")
        if not math.isfinite(self.duration_seconds) or not (
            MINIMUM_SOAK_SECONDS <= self.duration_seconds <= MAXIMUM_SOAK_SECONDS
        ):
            raise ValueError("soak duration must be at least 10 hours")
        if not math.isfinite(self.cycle_interval_seconds) or not (
            1 <= self.cycle_interval_seconds <= 3_600
        ):
            raise ValueError("soak cycle interval must be between 1 and 3600 seconds")
        if not math.isfinite(self.turn_timeout_seconds) or not (
            60 <= self.turn_timeout_seconds <= 1_800
        ):
            raise ValueError("soak turn timeout must be between 60 and 1800 seconds")
        if self.model != self.required_model:
            raise ValueError("selected model does not match the required local model")
        if not self.runtime_command or any(not argument for argument in self.runtime_command):
            raise ValueError("soak runtime command must be explicit and non-empty")
        if not 100_000 <= self.max_total_tokens <= 100_000_000:
            raise ValueError("soak token budget must be between 100000 and 100000000")
        if not 1 <= self.max_consecutive_failures <= 10:
            raise ValueError("soak consecutive failure limit must be between 1 and 10")
        if not 2 <= self.session_rotation_cycles <= 120:
            raise ValueError("soak session rotation must be between 2 and 120 cycles")
        if not 0 <= self.cancel_every_cycles <= 120:
            raise ValueError("soak cancellation cadence must be between 0 and 120 cycles")
        if not 2 <= self.max_parallel_tool_calls <= 4:
            raise ValueError("soak parallel tool limit must be between 2 and 4")
        if not self.provider.strip():
            raise ValueError("soak provider must be non-empty")
        if self.max_tokens <= 0 or self.model_max_tokens <= self.max_tokens:
            raise ValueError("soak token limits are invalid")
        if self.context_window <= self.model_max_tokens:
            raise ValueError("soak context window must exceed model output capacity")
        if not 1 <= self.max_concurrent_requests <= 2:
            raise ValueError("soak request concurrency must be one or two")


@dataclass(frozen=True, slots=True)
class _CaseObservation:
    name: str
    passed: bool
    elapsed_seconds: float
    total_tokens: int
    max_parallel_tools: int
    event_counts: dict[str, int]
    finish_reason: str
    error_codes: tuple[str, ...]
    guard_reasons: tuple[str, ...]
    rate_limit_retries: int

    def as_record(self, *, cycle: int) -> dict[str, object]:
        return {
            "cycle": cycle,
            "case": self.name,
            "passed": self.passed,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "total_tokens": self.total_tokens,
            "max_parallel_tools": self.max_parallel_tools,
            "event_counts": dict(sorted(self.event_counts.items())),
            "finish_reason": self.finish_reason,
            "error_codes": list(self.error_codes),
            "guard_reasons": list(self.guard_reasons),
            "rate_limit_retries": self.rate_limit_retries,
        }


async def run_soak(
    config: SoakConfig,
    *,
    runner: AgentRunner,
    clock: Clock = time.monotonic,
    sleep: Sleeper = asyncio.sleep,
) -> dict[str, object]:
    """Run one ten-hour-or-longer campaign and return aggregate evidence."""

    root = config.root.expanduser().resolve()
    previous: dict[str, object] | None = None
    if root.exists() and any(root.iterdir()):
        if not config.resume:
            raise ValueError("soak root must be empty for a new run")
        previous = read_soak_status(root)
        if previous.get("config_fingerprint") != _config_fingerprint(config):
            raise ValueError("soak resume configuration does not match persisted state")
        if previous.get("status") in {"passed", "degraded", "failed", "cancelled"}:
            await runner.close()
            return previous
        if previous.get("status") not in {"running", "interrupted"}:
            raise ValueError("soak state is not resumable")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    workspace = root / "workspace"
    workspace.mkdir(mode=0o700, exist_ok=True)
    workspace.chmod(0o700)
    _write_read_fixtures(workspace)

    run_id = (
        _string_value(previous, "run_id") if previous is not None else uuid.uuid4().hex[:12]
    )
    active_base = _float_value(previous, "active_seconds") if previous is not None else 0.0
    invocation_started = clock()
    epoch_started = (
        _float_value(previous, "started_at_epoch") if previous is not None else time.time()
    )
    invocations = (_int_value(previous, "invocations") if previous is not None else 0) + 1
    cycles_completed = _int_value(previous, "cycles_completed") if previous is not None else 0
    cases_total = _int_value(previous, "cases_total") if previous is not None else 0
    cases_passed = _int_value(previous, "cases_passed") if previous is not None else 0
    cases_failed = _int_value(previous, "cases_failed") if previous is not None else 0
    consecutive_failures = (
        _int_value(previous, "consecutive_failures") if previous is not None else 0
    )
    max_consecutive_failures_observed = (
        _int_value(previous, "max_consecutive_failures_observed")
        if previous is not None
        else 0
    )
    total_tokens = _int_value(previous, "total_tokens") if previous is not None else 0
    max_parallel_tools_observed = (
        _int_value(previous, "max_parallel_tools_observed") if previous is not None else 0
    )
    event_counts = _counter_value(previous, "event_counts")
    finish_reasons = _counter_value(previous, "finish_reasons")
    error_codes = _counter_value(previous, "error_codes")
    guard_reasons = _counter_value(previous, "guard_reasons")
    rate_limit_retries = (
        _int_value(previous, "rate_limit_retries") if previous is not None else 0
    )
    planned_cancellations = (
        _int_value(previous, "planned_cancellations") if previous is not None else 0
    )
    stop_reason = ""
    status = "running"
    needs_continuation_seed = True

    def active_seconds() -> float:
        return active_base + max(0.0, clock() - invocation_started)

    def report() -> dict[str, object]:
        return {
            "schema_version": 1,
            "suite": "xdsh-local-llm-soak",
            "status": status,
            "run_id": run_id,
            "config_fingerprint": _config_fingerprint(config),
            "model": config.model,
            "required_duration_seconds": config.duration_seconds,
            "active_seconds": round(active_seconds(), 3),
            "wall_seconds": round(max(0.0, time.time() - epoch_started), 3),
            "started_at_epoch": round(epoch_started, 3),
            "invocations": invocations,
            "cycles_completed": cycles_completed,
            "cases_total": cases_total,
            "cases_passed": cases_passed,
            "cases_failed": cases_failed,
            "consecutive_failures": consecutive_failures,
            "max_consecutive_failures_observed": max_consecutive_failures_observed,
            "total_tokens": total_tokens,
            "max_total_tokens": config.max_total_tokens,
            "max_parallel_tools_observed": max_parallel_tools_observed,
            "configured_parallel_tool_limit": config.max_parallel_tool_calls,
            "rate_limit_retries": rate_limit_retries,
            "planned_cancellations": planned_cancellations,
            "event_counts": dict(sorted(event_counts.items())),
            "finish_reasons": dict(sorted(finish_reasons.items())),
            "error_codes": dict(sorted(error_codes.items())),
            "guard_reasons": dict(sorted(guard_reasons.items())),
            "stop_reason": stop_reason,
            "updated_at_epoch": round(time.time(), 3),
        }

    _write_json_atomic(root / "state.json", report())
    try:
        while active_seconds() < config.duration_seconds:
            if (root / "cancel.request").is_file():
                status = "cancelled"
                stop_reason = "operator_cancel"
                break
            cycle_started = clock()
            read_marker = f"XDSH_SOAK_READ_{run_id.upper()}_{cycles_completed}"
            generation = cycles_completed // config.session_rotation_cycles
            if cycles_completed and cycles_completed % config.session_rotation_cycles == 0:
                previous_generation = generation - 1
                await asyncio.gather(
                    runner.reset(f"soak-answer-{run_id}-{previous_generation}"),
                    runner.reset(f"soak-read-{run_id}-{previous_generation}"),
                )
                needs_continuation_seed = True
            anchor = f"XDSH_SOAK_ANCHOR_{run_id.upper()}_{generation}"
            if needs_continuation_seed:
                continuation_task = (
                    f'Remember the opaque anchor "{anchor}". '
                    'Reply with exactly "STORED". Do not use tools.'
                )
                continuation_marker = "STORED"
            else:
                continuation_task = (
                    "What opaque anchor did I ask you to remember? "
                    "Reply with that anchor only. Do not use tools."
                )
                continuation_marker = anchor
            exercise_cancel = bool(
                config.cancel_every_cycles
                and (cycles_completed + 1) % config.cancel_every_cycles == 0
                and not needs_continuation_seed
            )
            primary_case = (
                _run_cancel_case(
                    runner,
                    session_id=f"soak-cancel-{run_id}-{cycles_completed}",
                    workspace=workspace,
                    timeout_seconds=config.turn_timeout_seconds,
                    clock=clock,
                )
                if exercise_cancel
                else _run_case(
                    runner,
                    name="continuation",
                    task=continuation_task,
                    expected_marker=continuation_marker,
                    session_id=f"soak-answer-{run_id}-{generation}",
                    workspace=workspace,
                    timeout_seconds=config.turn_timeout_seconds,
                    required_parallel_tools=0,
                    clock=clock,
                )
            )
            observations = await asyncio.gather(
                primary_case,
                _run_case(
                    runner,
                    name="parallel_read",
                    task=(
                        "Use the read tool once for each of the four independent files "
                        "soak-read-1.txt, soak-read-2.txt, soak-read-3.txt, and "
                        "soak-read-4.txt. Issue all four calls together, then "
                        f'Reply with exactly "{read_marker}".'
                    ),
                    expected_marker=read_marker,
                    session_id=f"soak-read-{run_id}-{generation}",
                    workspace=workspace,
                    timeout_seconds=config.turn_timeout_seconds,
                    required_parallel_tools=config.max_parallel_tool_calls,
                    clock=clock,
                ),
            )
            cycle_failed = False
            for observation_index, observation in enumerate(observations):
                cases_total += 1
                cases_passed += int(observation.passed)
                cases_failed += int(not observation.passed)
                cycle_failed = cycle_failed or not observation.passed
                total_tokens += observation.total_tokens
                max_parallel_tools_observed = max(
                    max_parallel_tools_observed,
                    observation.max_parallel_tools,
                )
                event_counts.update(observation.event_counts)
                finish_reasons[observation.finish_reason] += 1
                error_codes.update(observation.error_codes)
                guard_reasons.update(observation.guard_reasons)
                rate_limit_retries += observation.rate_limit_retries
                if observation.name == "cancel" and observation.passed:
                    planned_cancellations += 1
                if observation_index == 0 and observation.name == "continuation":
                    needs_continuation_seed = not observation.passed
                _append_json_line(
                    root / "events.jsonl",
                    observation.as_record(cycle=cycles_completed),
                )
            cycles_completed += 1
            consecutive_failures = consecutive_failures + 1 if cycle_failed else 0
            max_consecutive_failures_observed = max(
                max_consecutive_failures_observed,
                consecutive_failures,
            )
            if total_tokens >= config.max_total_tokens:
                status = "failed"
                stop_reason = "token_budget"
            elif consecutive_failures >= config.max_consecutive_failures:
                status = "failed"
                stop_reason = "consecutive_failures"
            _write_json_atomic(root / "state.json", report())
            if status == "failed":
                break

            cycle_elapsed = max(0.0, clock() - cycle_started)
            remaining = config.duration_seconds - active_seconds()
            delay = min(max(0.0, config.cycle_interval_seconds - cycle_elapsed), remaining)
            if delay > 0:
                await sleep(delay)
                _write_json_atomic(root / "state.json", report())

        if status == "running":
            status = "passed" if cases_failed == 0 else "degraded"
            stop_reason = "duration_complete"
        final = report()
        _write_json_atomic(root / "state.json", final)
        _write_json_atomic(root / "summary.json", final)
        return final
    except BaseException:
        status = "interrupted"
        stop_reason = "exception"
        _write_json_atomic(root / "state.json", report())
        raise
    finally:
        await runner.close()


async def _run_case(
    runner: AgentRunner,
    *,
    name: str,
    task: str,
    expected_marker: str,
    session_id: str,
    workspace: Path,
    timeout_seconds: float,
    required_parallel_tools: int,
    clock: Clock,
) -> _CaseObservation:
    started = clock()
    events: list[RunnerEventT] = []

    async def consume() -> None:
        async for event in runner.run(task, session_id=session_id, cwd=workspace):
            events.append(event)

    try:
        await asyncio.wait_for(consume(), timeout=timeout_seconds)
    except (TimeoutError, asyncio.TimeoutError):
        await runner.cancel(session_id)

    counts = Counter(event.kind.value for event in events)
    text = "".join(
        event.text for event in events if isinstance(event, TextEvent | ReasoningEvent)
    ) + "".join(
        event.final_text for event in events if isinstance(event, FinishEvent)
    )
    total_tokens = sum(
        event.total_tokens for event in events if isinstance(event, UsageEvent)
    )
    outstanding: set[str] = set()
    max_parallel = 0
    tool_results = 0
    tool_errors = 0
    for event in events:
        if isinstance(event, ToolStartEvent):
            outstanding.add(event.call_id)
            max_parallel = max(max_parallel, len(outstanding))
        elif isinstance(event, ToolResultEvent):
            tool_results += 1
            tool_errors += int(event.is_error)
            outstanding.discard(event.call_id)
    finishes = [event for event in events if isinstance(event, FinishEvent)]
    finish = finishes[-1] if finishes else None
    finish_reason = safe_error_code(finish.reason if finish else "missing")
    error_values = tuple(
        safe_error_code(event.code) for event in events if isinstance(event, ErrorEvent)
    )
    guard_values = tuple(
        event.reason.value
        for event in events
        if isinstance(event, GuardEvent) and event.action is GuardAction.STOP
    )
    successful_finish = bool(
        finish
        and not finish.cancelled
        and finish.reason.casefold() in _SUCCESS_REASONS
    )
    tool_contract = required_parallel_tools == 0 or (
        counts["tool_start"] >= required_parallel_tools
        and tool_results >= required_parallel_tools
        and max_parallel >= required_parallel_tools
        and tool_errors == 0
    )
    passed = bool(
        successful_finish
        and expected_marker in text
        and not error_values
        and not guard_values
        and tool_contract
    )
    return _CaseObservation(
        name=name,
        passed=passed,
        elapsed_seconds=max(0.0, clock() - started),
        total_tokens=total_tokens,
        max_parallel_tools=max_parallel,
        event_counts=dict(counts),
        finish_reason=finish_reason,
        error_codes=error_values,
        guard_reasons=guard_values,
        rate_limit_retries=sum(
            isinstance(event, RetryEvent) and event.rate_limited for event in events
        ),
    )


async def _run_cancel_case(
    runner: AgentRunner,
    *,
    session_id: str,
    workspace: Path,
    timeout_seconds: float,
    clock: Clock,
) -> _CaseObservation:
    """Exercise process-scoped cancellation and retain metadata only."""

    started = clock()
    events: list[RunnerEventT] = []
    tool_started = asyncio.Event()

    async def consume() -> None:
        async for event in runner.run(
            "Use Bash to run sleep 30, wait for it, then reply XDSH_SOAK_CANCEL_MISSED.",
            session_id=session_id,
            cwd=workspace,
        ):
            events.append(event)
            if isinstance(event, ToolStartEvent):
                tool_started.set()

    collector = asyncio.create_task(consume())
    cancel_reported = False
    try:
        await asyncio.wait_for(tool_started.wait(), timeout=min(120.0, timeout_seconds))
        cancel_reported = await runner.cancel(session_id)
        await asyncio.wait_for(collector, timeout=min(30.0, timeout_seconds))
    except (TimeoutError, asyncio.TimeoutError):
        await runner.cancel(session_id)
    finally:
        if not collector.done():
            collector.cancel()
            await asyncio.gather(collector, return_exceptions=True)

    counts = Counter(event.kind.value for event in events)
    finishes = [event for event in events if isinstance(event, FinishEvent)]
    finish = finishes[-1] if finishes else None
    finish_reason = safe_error_code(finish.reason if finish else "missing")
    error_values = tuple(
        safe_error_code(event.code) for event in events if isinstance(event, ErrorEvent)
    )
    guard_values = tuple(
        event.reason.value
        for event in events
        if isinstance(event, GuardEvent) and event.action is GuardAction.STOP
    )
    passed = bool(
        cancel_reported
        and counts["tool_start"] >= 1
        and finish is not None
        and finish.cancelled
        and not error_values
        and not guard_values
    )
    return _CaseObservation(
        name="cancel",
        passed=passed,
        elapsed_seconds=max(0.0, clock() - started),
        total_tokens=sum(
            event.total_tokens for event in events if isinstance(event, UsageEvent)
        ),
        max_parallel_tools=1 if counts["tool_start"] else 0,
        event_counts=dict(counts),
        finish_reason=finish_reason,
        error_codes=error_values,
        guard_reasons=guard_values,
        rate_limit_retries=sum(
            isinstance(event, RetryEvent) and event.rate_limited for event in events
        ),
    )


def read_soak_status(root: Path) -> dict[str, object]:
    """Read and validate the content-free public status artifact."""

    path = root.expanduser().resolve() / "state.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("suite") != "xdsh-local-llm-soak":
        raise ValueError("invalid XDSH soak status document")
    return cast(dict[str, object], raw)


def soak_health(
    root: Path,
    *,
    max_age_seconds: float = 1_200.0,
    now: float | None = None,
) -> dict[str, object]:
    """Return a content-free liveness verdict for Docker/operator probes."""

    if not math.isfinite(max_age_seconds) or not 30 <= max_age_seconds <= 3_600:
        raise ValueError("soak health max age must be between 30 and 3600 seconds")
    document = read_soak_status(root)
    status = _string_value(document, "status")
    updated_at = _float_value(document, "updated_at_epoch")
    observed_at = time.time() if now is None else now
    age = round(max(0.0, observed_at - updated_at), 3)
    healthy = status in {"passed", "degraded"} or (
        status == "running" and age <= max_age_seconds
    )
    return {"healthy": healthy, "status": status, "age_seconds": age}


def _write_read_fixtures(workspace: Path) -> None:
    for index in range(1, 5):
        path = workspace / f"soak-read-{index}.txt"
        path.write_text(f"XDSH_SOAK_FILE_{index}\n", encoding="utf-8")
        path.chmod(0o600)


def _write_json_atomic(path: Path, value: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    payload = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    try:
        temporary.write_text(payload + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _append_json_line(path: Path, value: dict[str, object]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        payload = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        os.write(descriptor, (payload + "\n").encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _config_fingerprint(config: SoakConfig) -> str:
    payload = {
        "model": config.model,
        "required_model": config.required_model,
        "runtime_command": list(config.runtime_command),
        "duration_seconds": config.duration_seconds,
        "cycle_interval_seconds": config.cycle_interval_seconds,
        "turn_timeout_seconds": config.turn_timeout_seconds,
        "max_total_tokens": config.max_total_tokens,
        "max_consecutive_failures": config.max_consecutive_failures,
        "session_rotation_cycles": config.session_rotation_cycles,
        "cancel_every_cycles": config.cancel_every_cycles,
        "max_parallel_tool_calls": config.max_parallel_tool_calls,
        "provider": config.provider,
        "max_tokens": config.max_tokens,
        "model_max_tokens": config.model_max_tokens,
        "context_window": config.context_window,
        "max_concurrent_requests": config.max_concurrent_requests,
    }
    canonical = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _int_value(document: dict[str, object] | None, key: str) -> int:
    if document is None:
        return 0
    value = document.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"invalid persisted soak integer: {key}")
    return value


def _float_value(document: dict[str, object] | None, key: str) -> float:
    if document is None:
        return 0.0
    value = document.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"invalid persisted soak duration: {key}")
    rendered = float(value)
    if rendered < 0 or not math.isfinite(rendered):
        raise ValueError(f"invalid persisted soak duration: {key}")
    return rendered


def _string_value(document: dict[str, object], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"invalid persisted soak string: {key}")
    return value


def _counter_value(document: dict[str, object] | None, key: str) -> Counter[str]:
    if document is None:
        return Counter()
    value = document.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"invalid persisted soak counter: {key}")
    counter: Counter[str] = Counter()
    for raw_name, raw_count in value.items():
        if (
            not isinstance(raw_name, str)
            or not raw_name
            or isinstance(raw_count, bool)
            or not isinstance(raw_count, int)
            or raw_count < 0
        ):
            raise ValueError(f"invalid persisted soak counter: {key}")
        counter[raw_name] = raw_count
    return counter


def build_soak_runner(config: SoakConfig) -> AgentRunner:
    """Build the production runner stack used by the endurance campaign."""

    rate_policy = RateLimitPolicy(
        max_concurrent_requests=config.max_concurrent_requests,
        min_interval_seconds=0.25,
        max_rate_limit_retries=3,
        base_backoff_seconds=1.0,
        max_backoff_seconds=30.0,
        max_retry_after_seconds=120.0,
        acquire_timeout_seconds=120.0,
    )
    dsh = DshRunner(
        DshRunnerConfig(
            dsh_home=config.root.expanduser().resolve() / "dsh-home",
            model=config.model,
            provider=config.provider,
            runtime_command=config.runtime_command,
            max_tokens=config.max_tokens,
            model_max_tokens=config.model_max_tokens,
            context_window=config.context_window,
            max_parallel_tool_calls=config.max_parallel_tool_calls,
            request_timeout_seconds=config.turn_timeout_seconds,
            rate_limit_policy=rate_policy,
        )
    )
    rate_limited = RateLimitedRunner(
        dsh,
        provider=config.provider,
        policy=rate_policy,
    )
    return GuardedRunner(
        rate_limited,
        policy=RunGuardPolicy(
            max_model_steps=120,
            max_tool_calls=160,
            max_same_tool_calls=8,
            max_consecutive_tool_errors=8,
            max_retries_without_progress=6,
            max_total_tokens=2_000_000,
            max_parallel_subagents=1,
            stall_timeout_seconds=600.0,
            max_runtime_seconds=None,
            cancel_timeout_seconds=10.0,
        ),
    )


def request_soak_cancel(root: Path) -> dict[str, object]:
    """Request a bounded stop at the next campaign cycle boundary."""

    document = read_soak_status(root)
    if document.get("status") != "running":
        raise ValueError("only a running soak can be cancelled")
    path = root.expanduser().resolve() / "cancel.request"
    _write_json_atomic(
        path,
        {
            "schema_version": 1,
            "action": "cancel",
            "requested_at_epoch": round(time.time(), 3),
        },
    )
    return {"requested": True, "status": "running"}


def _runtime_command(raw: str | None) -> tuple[str, ...]:
    if raw is None or not raw.strip():
        return _DEFAULT_RUNTIME_COMMAND
    command = tuple(shlex.split(raw))
    if not command or any(not argument for argument in command):
        raise ValueError("soak runtime command must contain non-empty argv")
    return command


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="run or resume a local-model endurance campaign")
    run.add_argument("--root", type=Path, required=True)
    run.add_argument("--model", default=os.environ.get("XDSH_LITELLM_MODEL", ""))
    run.add_argument("--required-model", default="qwen-3.8-27b-draft-mtp")
    run.add_argument("--provider", default="litellm")
    run.add_argument("--runtime-command", default=os.environ.get("XDSH_DSH_RUNTIME_COMMAND"))
    run.add_argument("--duration-hours", type=float, default=10.0)
    run.add_argument("--cycle-seconds", type=float, default=60.0)
    run.add_argument("--turn-timeout-seconds", type=float, default=900.0)
    run.add_argument("--max-total-tokens", type=int, default=10_000_000)
    run.add_argument("--max-consecutive-failures", type=int, default=3)
    run.add_argument("--session-rotation-cycles", type=int, default=10)
    run.add_argument("--cancel-every-cycles", type=int, default=30)
    run.add_argument("--max-parallel-tools", type=int, default=4)
    run.add_argument("--max-concurrent-requests", type=int, default=2)
    run.add_argument("--resume", action="store_true")

    status = commands.add_parser("status", help="read the aggregate campaign liveness")
    status.add_argument("--root", type=Path, required=True)
    status.add_argument("--max-age-seconds", type=float, default=1_200.0)

    cancel = commands.add_parser("cancel", help="request a safe cycle-boundary stop")
    cancel.add_argument("--root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the soak CLI without exposing prompts, responses, or credentials."""

    args = _parser().parse_args(argv)
    try:
        if args.command == "status":
            result = soak_health(args.root, max_age_seconds=args.max_age_seconds, now=time.time())
            print(json.dumps(result, ensure_ascii=True, sort_keys=True))
            return 0 if result["healthy"] is True else 1
        if args.command == "cancel":
            print(json.dumps(request_soak_cancel(args.root), ensure_ascii=True, sort_keys=True))
            return 0

        config = SoakConfig(
            root=args.root,
            model=args.model,
            required_model=args.required_model,
            runtime_command=_runtime_command(args.runtime_command),
            duration_seconds=args.duration_hours * 3_600,
            cycle_interval_seconds=args.cycle_seconds,
            turn_timeout_seconds=args.turn_timeout_seconds,
            max_total_tokens=args.max_total_tokens,
            max_consecutive_failures=args.max_consecutive_failures,
            session_rotation_cycles=args.session_rotation_cycles,
            cancel_every_cycles=args.cancel_every_cycles,
            max_parallel_tool_calls=args.max_parallel_tools,
            max_concurrent_requests=args.max_concurrent_requests,
            provider=args.provider,
            resume=args.resume,
        )
        result = asyncio.run(run_soak(config, runner=build_soak_runner(config)))
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        if result["status"] == "passed":
            return 0
        if result["status"] == "degraded":
            return 2
        return 1
    except Exception as error:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_type": type(error).__name__,
                    "error": safe_exception_text(error, max_chars=400),
                },
                ensure_ascii=True,
                sort_keys=True,
            )
        )
        return 1


__all__ = [
    "MAXIMUM_SOAK_SECONDS",
    "MINIMUM_SOAK_SECONDS",
    "SoakConfig",
    "build_soak_runner",
    "main",
    "read_soak_status",
    "request_soak_cancel",
    "run_soak",
    "soak_health",
]


if __name__ == "__main__":
    raise SystemExit(main())
