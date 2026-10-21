"""Runner-neutral protection against flailing, stalls, and runaway usage."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import shlex
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import cast

from xdsh.core.acceptance_store import AcceptanceStore, StoredAcceptance
from xdsh.core.artifact_acceptance import (
    AcceptanceError,
    ArtifactAcceptance,
    capture_acceptance,
    contract_header,
    issue_categories,
)
from xdsh.core.decision_router import DecisionOption, DecisionRequest
from xdsh.core.events import (
    TRANSIENT_REQUEST_FAILURES,
    ActivityEvent,
    ArtifactEvent,
    CompactionEvent,
    ErrorEvent,
    FinishEvent,
    GuardAction,
    GuardEvent,
    GuardReason,
    ReasoningEvent,
    RetryEvent,
    RunnerEventT,
    RuntimeEvent,
    SubagentAction,
    SubagentEvent,
    TextEvent,
    TodoEvent,
    ToolResultEvent,
    ToolStartEvent,
    UsageEvent,
    VerificationEvent,
)
from xdsh.core.rate_limit import is_rate_limit_retry
from xdsh.core.research_recovery import ResearchRepair
from xdsh.core.runner import (
    AgentRunner,
    CapabilityAwareRunner,
    ReasoningEffort,
    RunnerCapabilities,
)

LOGGER = logging.getLogger(__name__)

Clock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True, kw_only=True)
class RunGuardPolicy:
    """Bound one task using only stable runner events and monotonic time.

    Counter, retry, and silence bounds stop runaway work.  Healthy activity is
    not stopped merely because wall-clock time passes; operators may opt into
    an absolute runtime ceiling when their environment requires one.
    """

    max_model_steps: int | None = 120
    max_tool_calls: int | None = 160
    max_same_tool_calls: int = 8
    max_stagnant_poll_calls: int = 4
    max_same_tool_errors: int = 3
    max_consecutive_tool_errors: int = 8
    max_retries_without_progress: int = 6
    # Per model request, independent of task length and partial token activity.
    max_transient_retries: int = 2
    max_total_tokens: int | None = 2_000_000
    max_parallel_subagents: int | None = 3
    stall_timeout_seconds: float = 600.0
    # A started tool may legitimately spend much longer than the normal
    # stream-silence window (for example a build, crawl, or large file write).
    # Keep a separate bounded grace period so long tools are not mistaken for
    # a dead model, while a genuinely silent turn still stops promptly.
    active_tool_stall_timeout_seconds: float = 1_800.0
    max_runtime_seconds: float | None = None
    warning_ratio: float = 0.8
    cancel_timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        integer_limits = {
            "max_model_steps": self.max_model_steps,
            "max_tool_calls": self.max_tool_calls,
            "max_same_tool_calls": self.max_same_tool_calls,
            "max_stagnant_poll_calls": self.max_stagnant_poll_calls,
            "max_same_tool_errors": self.max_same_tool_errors,
            "max_consecutive_tool_errors": self.max_consecutive_tool_errors,
            "max_retries_without_progress": self.max_retries_without_progress,
            "max_transient_retries": self.max_transient_retries,
            "max_total_tokens": self.max_total_tokens,
            "max_parallel_subagents": self.max_parallel_subagents,
        }
        for name, value in integer_limits.items():
            if value is None and name in {
                "max_model_steps", "max_tool_calls", "max_total_tokens", "max_parallel_subagents",
            }:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be positive")
        duration_limits: dict[str, float] = {
            "stall_timeout_seconds": self.stall_timeout_seconds,
            "active_tool_stall_timeout_seconds": self.active_tool_stall_timeout_seconds,
            "cancel_timeout_seconds": self.cancel_timeout_seconds,
        }
        if self.max_runtime_seconds is not None:
            duration_limits["max_runtime_seconds"] = self.max_runtime_seconds
        for name, duration_value in duration_limits.items():
            if (
                isinstance(duration_value, bool)
                or not isinstance(duration_value, int | float)
                or not math.isfinite(float(duration_value))
                or duration_value <= 0
            ):
                raise ValueError(f"{name} must be a finite positive number")
        if not math.isfinite(self.warning_ratio):
            raise ValueError("warning_ratio must be finite")
        if not 0.5 <= self.warning_ratio < 1.0:
            raise ValueError("warning_ratio must be at least 0.5 and less than 1")


@dataclass(slots=True)
class _RunGuardState:
    policy: RunGuardPolicy
    session_id: str
    started_at: float
    last_event_at: float
    rate_limit_protected: bool = False
    model_steps: int = 0
    tool_calls: int = 0
    same_tool_calls: int = 0
    same_tool_errors: int = 0
    consecutive_tool_errors: int = 0
    retries_without_progress: int = 0
    total_tokens: int = 0
    tool_fingerprint_counts: dict[str, int] = field(default_factory=dict)
    pending_fingerprints: dict[str, str] = field(default_factory=dict)
    pending_poll_calls: set[str] = field(default_factory=set)
    pending_progress_calls: set[str] = field(default_factory=set)
    tool_failure_counts: dict[str, int] = field(default_factory=dict)
    poll_result_hashes: dict[str, str] = field(default_factory=dict)
    stagnant_poll_calls: dict[str, int] = field(default_factory=dict)
    poll_changed_at: dict[str, float] = field(default_factory=dict)
    last_substantive_progress_at: float = field(init=False)
    active_subagents: set[str] = field(default_factory=set)
    warned: set[GuardReason] | None = None
    stopped: bool = False

    def __post_init__(self) -> None:
        self.warned = set()
        self.last_substantive_progress_at = self.started_at

    def observe(self, event: RunnerEventT, *, now: float) -> tuple[GuardEvent, ...]:
        """Record one normalized event and return any newly crossed bounds."""

        if (isinstance(event, ActivityEvent) and not event.has_output) or (
            isinstance(event, TextEvent | ReasoningEvent) and not event.text
        ):
            # Transport activity and legitimate empty preludes may be shown,
            # but cannot erase retry pressure or postpone silence detection.
            return ()
        self.last_event_at = now
        assert self.warned is not None
        self.warned.discard(GuardReason.STALLED)
        eligible_stagnant_polls = 0

        if isinstance(event, RetryEvent):
            if (event.failure_code in TRANSIENT_REQUEST_FAILURES
                    and event.attempt is not None
                    and event.attempt > self.policy.max_transient_retries):
                self.stopped = True
                return (self._event(GuardAction.STOP, GuardReason.RETRY_BUDGET,
                                    event.attempt, self.policy.max_transient_retries),)
            # A dedicated inner guard owns the 429 episode budget. Counting it
            # again here preempts a configured 6-10-attempt recovery. Do not
            # reset the generic counter: interleaving 429s is not progress.
            if not (self.rate_limit_protected and is_rate_limit_retry(event)):
                self.retries_without_progress += 1
        elif isinstance(
            event,
            TextEvent
            | ReasoningEvent
            | ActivityEvent
            | ToolStartEvent
            | ToolResultEvent
            | TodoEvent
            | UsageEvent
            | ArtifactEvent
            | SubagentEvent
            | RuntimeEvent
            | CompactionEvent,
        ):
            self.retries_without_progress = 0

        # A successfully normalized artifact is durable evidence that the
        # workspace changed.  Start a new repeat epoch so a legitimate
        # inspect-edit-inspect cycle does not inherit the earlier read count.
        # Text and generic tool results deliberately do not reset this state:
        # rc.2 reports Bash non-zero exits and sandbox denials as successful
        # text results, and alternating another call must not evade the guard.
        if isinstance(event, ArtifactEvent):
            self.tool_fingerprint_counts.clear()
            self.tool_failure_counts.clear()
            self.poll_result_hashes.clear()
            self.stagnant_poll_calls.clear()
            self.poll_changed_at.clear()
            self.last_substantive_progress_at = now
            self.same_tool_calls = 0
            self.same_tool_errors = 0

        if isinstance(event, ToolStartEvent):
            self.tool_calls += 1
            fingerprint = _tool_fingerprint(event)
            self.pending_fingerprints[event.call_id] = fingerprint
            if _is_poll_tool(event.name):
                self.pending_poll_calls.add(event.call_id)
                # Polling the same background process is expected. Evaluate
                # unchanged results plus elapsed silence, not the generic
                # repeated-operation counter, and only after its result arrives.
                self.same_tool_calls = 0
            else:
                count = self.tool_fingerprint_counts.get(fingerprint, 0) + 1
                self.tool_fingerprint_counts[fingerprint] = count
                self.same_tool_calls = count
                if event.name.strip().casefold() not in {
                    "todo", "todo_write", "update_plan", "plan", "progress",
                }:
                    self.pending_progress_calls.add(event.call_id)

        if isinstance(event, ToolResultEvent):
            if event.call_id in self.pending_progress_calls:
                self.pending_progress_calls.discard(event.call_id)
                if not event.is_error:
                    self.last_substantive_progress_at = now
            completed_fingerprint = self.pending_fingerprints.pop(event.call_id, None)
            if completed_fingerprint is not None:
                if event.call_id in self.pending_poll_calls:
                    self.pending_poll_calls.discard(event.call_id)
                    result_hash = _content_hash(event.content)
                    previous_hash = self.poll_result_hashes.get(completed_fingerprint)
                    if event.is_error or previous_hash == result_hash:
                        self.stagnant_poll_calls[completed_fingerprint] = (
                            self.stagnant_poll_calls.get(completed_fingerprint, 0) + 1
                        )
                    else:
                        # A changed poll result is observable progress. It is
                        # safe to keep a long-running external build alive and
                        # restart only this poll epoch, not all guard counters.
                        self.stagnant_poll_calls.pop(completed_fingerprint, None)
                        self.tool_fingerprint_counts.pop(completed_fingerprint, None)
                        self.same_tool_calls = 0
                        self.poll_changed_at[completed_fingerprint] = now
                    quiet_since = max(
                        self.poll_changed_at.get(completed_fingerprint, self.started_at),
                        self.last_substantive_progress_at,
                    )
                    if now - quiet_since >= self.policy.active_tool_stall_timeout_seconds:
                        eligible_stagnant_polls = self.stagnant_poll_calls.get(
                            completed_fingerprint, 0,
                        )
                    self.poll_result_hashes[completed_fingerprint] = result_hash
                if event.is_error:
                    count = self.tool_failure_counts.get(completed_fingerprint, 0) + 1
                    self.tool_failure_counts[completed_fingerprint] = count
                    self.same_tool_errors = count
                else:
                    # Only success of this exact call clears its failure streak;
                    # unrelated Todo/text activity is not recovery evidence.
                    self.tool_failure_counts.pop(completed_fingerprint, None)
                    self.same_tool_errors = 0
            if event.is_error:
                self.consecutive_tool_errors += 1
            else:
                self.consecutive_tool_errors = 0

        if isinstance(event, UsageEvent):
            self.model_steps += 1
            self.total_tokens += _usage_tokens(event)

        subagent_events = self._subagent_events(event)
        if any(item.action is GuardAction.STOP for item in subagent_events):
            return subagent_events
        return subagent_events + self._counter_events(stagnant_polls=eligible_stagnant_polls)

    def _subagent_events(self, event: RunnerEventT) -> tuple[GuardEvent, ...]:
        if not isinstance(event, SubagentEvent):
            return ()
        if event.action is SubagentAction.STARTED:
            self.active_subagents.add(event.child_session_id)
        else:
            self.active_subagents.discard(event.child_session_id)
        observed = len(self.active_subagents)
        limit = self.policy.max_parallel_subagents
        if limit is None:
            return ()
        if observed > limit:
            self.stopped = True
            return (
                self._event(
                    GuardAction.STOP,
                    GuardReason.SUBAGENT_BUDGET,
                    observed,
                    limit,
                ),
            )
        assert self.warned is not None
        if (
            observed == limit
            and GuardReason.SUBAGENT_BUDGET not in self.warned
        ):
            self.warned.add(GuardReason.SUBAGENT_BUDGET)
            return (
                self._event(
                    GuardAction.WARNING,
                    GuardReason.SUBAGENT_BUDGET,
                    observed,
                    limit,
                ),
            )
        return ()

    def inspect_time(self, *, now: float) -> tuple[GuardEvent, ...]:
        """Return warnings or a stop caused solely by elapsed monotonic time."""

        if self.stopped:
            return ()
        elapsed = max(0.0, now - self.started_at)
        quiet = max(0.0, now - self.last_event_at)
        # Once DSH has emitted a structured tool start, the model is waiting
        # on an external operation.  Use the larger, still finite tool grace
        # period; before that point retain the short no-event protection.
        stall_limit = (
            self.policy.active_tool_stall_timeout_seconds
            if self.pending_fingerprints
            else self.policy.stall_timeout_seconds
        )
        specs: list[tuple[GuardReason, float, float]] = [
            (GuardReason.STALLED, quiet, stall_limit)
        ]
        if self.policy.max_runtime_seconds is not None:
            specs.insert(
                0,
                (GuardReason.TIME_BUDGET, elapsed, self.policy.max_runtime_seconds),
            )
        for reason, observed, limit in specs:
            if observed >= limit:
                self.stopped = True
                return (self._event(GuardAction.STOP, reason, observed, limit),)

        assert self.warned is not None
        warnings: list[GuardEvent] = []
        for reason, observed, limit in specs:
            if (
                reason not in self.warned
                and observed >= limit * self.policy.warning_ratio
            ):
                self.warned.add(reason)
                warnings.append(self._event(GuardAction.WARNING, reason, observed, limit))
        return tuple(warnings)

    def seconds_until_time_check(self, *, now: float) -> float:
        """Return the next warning or hard time deadline."""

        assert self.warned is not None
        stall_limit = (
            self.policy.active_tool_stall_timeout_seconds
            if self.pending_fingerprints
            else self.policy.stall_timeout_seconds
        )
        deadlines = [
            (
                GuardReason.STALLED,
                self.last_event_at,
                stall_limit,
            )
        ]
        if self.policy.max_runtime_seconds is not None:
            deadlines.append(
                (
                    GuardReason.TIME_BUDGET,
                    self.started_at,
                    self.policy.max_runtime_seconds,
                )
            )
        next_deadlines: list[float] = []
        for reason, origin, limit in deadlines:
            fraction = 1.0 if reason in self.warned else self.policy.warning_ratio
            next_deadlines.append(origin + limit * fraction)
        return max(0.0, min(next_deadlines) - now)

    def _counter_events(self, *, stagnant_polls: int = 0) -> tuple[GuardEvent, ...]:
        if self.stopped:
            return ()
        specs = (
            (
                GuardReason.REPEATED_TOOL,
                stagnant_polls,
                self.policy.max_stagnant_poll_calls,
            ),
            (
                GuardReason.REPEATED_TOOL,
                self.same_tool_errors,
                self.policy.max_same_tool_errors,
            ),
            (
                GuardReason.RETRY_BUDGET,
                self.retries_without_progress,
                self.policy.max_retries_without_progress,
            ),
            (
                GuardReason.REPEATED_TOOL,
                self.same_tool_calls,
                self.policy.max_same_tool_calls,
            ),
            (
                GuardReason.TOOL_ERROR_BUDGET,
                self.consecutive_tool_errors,
                self.policy.max_consecutive_tool_errors,
            ),
            (GuardReason.TOKEN_BUDGET, self.total_tokens, self.policy.max_total_tokens),
            (
                GuardReason.MODEL_STEP_BUDGET,
                self.model_steps,
                self.policy.max_model_steps,
            ),
            (GuardReason.TOOL_BUDGET, self.tool_calls, self.policy.max_tool_calls),
        )
        for reason, observed, limit in specs:
            if limit is None:
                continue
            if observed >= limit:
                self.stopped = True
                return (
                    GuardEvent(
                        session_id=self.session_id,
                        action=GuardAction.STOP,
                        reason=reason,
                        observed=observed,
                        limit=limit,
                    ),
                )

        assert self.warned is not None
        warnings: list[GuardEvent] = []
        for reason, observed, limit in specs:
            if limit is None:
                continue
            warning_at = math.ceil(limit * self.policy.warning_ratio)
            if reason not in self.warned and observed >= warning_at:
                self.warned.add(reason)
                warnings.append(
                    GuardEvent(
                        session_id=self.session_id,
                        action=GuardAction.WARNING,
                        reason=reason,
                        observed=observed,
                        limit=limit,
                    )
                )
        return tuple(warnings)

    def _event(
        self,
        action: GuardAction,
        reason: GuardReason,
        observed: float,
        limit: float,
    ) -> GuardEvent:
        return GuardEvent(
            session_id=self.session_id,
            action=action,
            reason=reason,
            observed=max(0, round(observed)),
            limit=max(1, round(limit)),
        )


class GuardedRunner:
    """Deep Adapter that preserves ``AgentRunner`` while enforcing one policy."""

    def __init__(
        self,
        runner: AgentRunner,
        *,
        policy: RunGuardPolicy | None = None,
        clock: Clock = time.monotonic,
        sleep: Sleeper = asyncio.sleep,
        research_repair_attempts: int = 2,
        acceptance_root: Path | None = None,
    ) -> None:
        if (type(research_repair_attempts) is not int
                or not 0 <= research_repair_attempts <= 2):
            raise ValueError("research_repair_attempts must be 0, 1 or 2")
        self._runner = runner
        self.policy = policy or RunGuardPolicy()
        self._clock = clock
        self._sleep = sleep
        self._research_repair_attempts = research_repair_attempts
        self._acceptance_root = acceptance_root
        self._cancel_signals: dict[str, asyncio.Event] = {}
        self._repair_boundaries: set[str] = set()

    @property
    def capabilities(self) -> RunnerCapabilities:
        if isinstance(self._runner, CapabilityAwareRunner):
            return self._runner.capabilities
        return RunnerCapabilities(
            protocol="unknown",
            runtime_package="unknown",
            runtime_identity="unknown",
            native=frozenset(),
            unsupported_native=frozenset(),
            source="unknown_adapter",
        )

    @property
    def rate_limit_protected(self) -> bool:
        """Preserve the nested 429 boundary marker across transparent guards."""

        return bool(getattr(self._runner, "rate_limit_protected", False))

    @property
    def allow_fallback_on_rate_limit(self) -> bool:
        """Forward the explicit nested fallback opt-in, if present."""

        return bool(getattr(self._runner, "allow_fallback_on_rate_limit", False))

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
        guard_policy: RunGuardPolicy | None = None,
    ) -> AsyncIterator[RunnerEventT]:
        """Yield delegate events, warnings, and one guarded terminal event."""

        started_at = self._clock()
        if session_id in self._cancel_signals:
            raise RuntimeError("session already has an active guarded task")
        store: AcceptanceStore | None = None
        binding: StoredAcceptance | None = None
        checklist: str | None = None
        try:
            acceptance = (
                await asyncio.to_thread(capture_acceptance, task, cwd)
                if contract_header(task) is not None else None
            )
            if acceptance is not None and acceptance.deferred is not None:
                checklist = acceptance.deferred
                if self._acceptance_root is not None:
                    store = AcceptanceStore(self._acceptance_root)
                    binding = await asyncio.to_thread(store.begin, acceptance, session_id)
                    acceptance = binding.snapshot or acceptance
                    task = _bound_verification_task(task, acceptance.root, binding)
        except AcceptanceError:
            yield ErrorEvent(session_id=session_id, code="ACCEPTANCE_INVALID",
                             message="Artifact acceptance contract is invalid or unreadable")
            yield FinishEvent(session_id=session_id, reason="error")
            return
        state = _RunGuardState(
            policy=guard_policy or self.policy,
            session_id=session_id,
            started_at=started_at,
            last_event_at=started_at,
            rate_limit_protected=self.rate_limit_protected,
        )
        cancel_signal = asyncio.Event()
        # capture_acceptance above yields to filesystem work; another run may
        # have acquired the same session meanwhile. Reserve without an await.
        if session_id in self._cancel_signals:
            raise RuntimeError("session already has an active guarded task")
        self._cancel_signals[session_id] = cancel_signal
        repair = (ResearchRepair(checklist, limit=self._research_repair_attempts)
                  if checklist is not None else None)
        saw_work = False
        saw_error = False
        retained_text = ""
        segment_text = ""
        segment_has_text = False
        observed_generation: str | None = None
        observed_sequence: int | None = None
        iterator: AsyncIterator[RunnerEventT] | None = None
        next_event: asyncio.Task[RunnerEventT] | None = None
        timer: asyncio.Task[None] | None = None
        guarded = False
        try:
            iterator = self._runner.run(
                task, session_id=session_id, cwd=cwd, model=model,
                reasoning_effort=reasoning_effort, fallback=fallback,
                system_prompt=system_prompt, resume_context=resume_context,
            )
            while True:
                if next_event is None:
                    next_event = asyncio.create_task(_read_next(iterator))
                    await asyncio.sleep(0)

                if not next_event.done():
                    delay = state.seconds_until_time_check(now=self._clock())
                    timer = asyncio.create_task(_sleep_for(self._sleep, delay))
                    done, _ = await asyncio.wait(
                        (next_event, timer),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if next_event not in done:
                        timer = None
                        guard_events = state.inspect_time(now=self._clock())
                        for guard_event in guard_events:
                            yield guard_event
                        if _has_stop(guard_events):
                            guarded = True
                            await self._cancel_guarded_run(session_id)
                            yield FinishEvent(session_id=session_id, reason="guarded",
                                              final_text=retained_text)
                            return
                        continue
                    timer.cancel()
                    await asyncio.gather(timer, return_exceptions=True)
                    timer = None

                try:
                    event = next_event.result()
                except StopAsyncIteration:
                    if not saw_error and not cancel_signal.is_set():
                        yield ErrorEvent(session_id=session_id, code="RUNNER_STREAM_LOST",
                                         message="Runner ended without a confirmed final event")
                    yield FinishEvent(session_id=session_id,
                                      reason="cancelled" if cancel_signal.is_set() else "error",
                                      cancelled=cancel_signal.is_set(),
                                      final_text=retained_text)
                    return
                except Exception as error:
                    # Never auto-resubmit after an unconfirmed execution; preserve
                    # observed answer text, without exposing raw exceptions or secrets.
                    LOGGER.warning("Runner stream failed with %s", type(error).__name__)
                    if not saw_error and not cancel_signal.is_set():
                        yield ErrorEvent(session_id=session_id, code="RUNNER_STREAM_LOST",
                                         message="Runner stream failed; execution not confirmed")
                    yield FinishEvent(session_id=session_id,
                                      reason="cancelled" if cancel_signal.is_set() else "error",
                                      cancelled=cancel_signal.is_set(), final_text=retained_text)
                    return
                next_event = None
                if event.runtime_generation is not None:
                    trusted_frame = True
                    try:
                        current_generation = await asyncio.wait_for(
                            self.runtime_generation(session_id),
                            timeout=self.policy.cancel_timeout_seconds,
                        )
                    except Exception:
                        # An unverified frame cannot extend liveness, become a
                        # terminal error, or be repackaged as current answer text.
                        trusted_frame = False
                    else:
                        if current_generation is not None:
                            trusted_frame = event.runtime_generation == current_generation
                        elif observed_generation not in (None, event.runtime_generation):
                            trusted_frame = False  # A changed source needs a current marker.
                    if trusted_frame:
                        if observed_generation != event.runtime_generation:
                            observed_generation = event.runtime_generation
                            observed_sequence = None
                        if event.sequence is not None:
                            trusted_frame = (
                                observed_sequence is None or event.sequence >= observed_sequence
                            )
                            if trusted_frame:
                                observed_sequence = event.sequence
                    if not trusted_frame:
                        # A rapid stream of rejected frames must not bypass time
                        # checks merely because every next_event is already done.
                        guard_events = state.inspect_time(now=self._clock())
                        for guard_event in guard_events:
                            yield guard_event
                        if _has_stop(guard_events):
                            guarded = True
                            await self._cancel_guarded_run(session_id)
                            yield FinishEvent(session_id=session_id, reason="guarded",
                                              final_text=retained_text)
                            return
                        continue
                if isinstance(event, ErrorEvent):
                    saw_error = True
                if isinstance(event, TextEvent):
                    segment_text = segment_text + event.text if event.delta else event.text
                    retained_text = segment_text or retained_text
                    if event.text.strip():
                        segment_has_text = True
                if isinstance(event, ToolResultEvent | ArtifactEvent):
                    saw_work = True
                if isinstance(event, ToolResultEvent) and acceptance is not None:
                    if acceptance.deferred is not None:
                        try:
                            acceptance, binding = await _freeze_research(
                                acceptance, store, binding, "tool_boundary",
                            )
                        except _BaselineUnavailable:
                            if cancel_signal.is_set():
                                yield FinishEvent(session_id=session_id, reason="cancelled",
                                                  cancelled=True, final_text=retained_text)
                                return
                            await self._cancel_guarded_run(session_id)
                            yield ErrorEvent(session_id=session_id, code="ACCEPTANCE_INVALID",
                                             message="Host verification baseline unavailable")
                            yield FinishEvent(session_id=session_id, reason="error",
                                              final_text=retained_text)
                            return
                        except AcceptanceError:
                            # One known path, not a workspace scan. A missing/incomplete
                            # declaration is reported at completion, not mid-write.
                            pass
                if isinstance(event, FinishEvent):
                    # No new work may be dispatched while the confirmed terminal
                    # segment is being checked/closed. Cancellation here is local.
                    self._repair_boundaries.add(session_id)
                    if cancel_signal.is_set():
                        yield replace(event, reason="cancelled", cancelled=True,
                                      final_text=event.final_text or retained_text)
                        return
                    if saw_error and not event.cancelled:
                        # Normalized ErrorEvent is a terminal failure, not a retry
                        # notification. A contradictory later success must not
                        # authorize verification, repair or successful delivery.
                        yield replace(event, reason="error",
                                      final_text=event.final_text or retained_text)
                        return
                    if acceptance is not None and not event.cancelled and not saw_error \
                            and event.reason.casefold() in {
                        "completed", "complete", "success", "succeeded",
                    }:
                        # Freeze a late declaration too; never discard before-state
                        # between repair segments or capture a new baseline on resume.
                        if acceptance.deferred is not None:
                            try:
                                acceptance, binding = await _freeze_research(
                                    acceptance, store, binding, "completion",
                                )
                            except _BaselineUnavailable:
                                if cancel_signal.is_set():
                                    yield replace(event, reason="cancelled", cancelled=True,
                                                  final_text=event.final_text or retained_text)
                                    return
                                yield ErrorEvent(session_id=session_id, code="ACCEPTANCE_INVALID",
                                                 message="Host verification baseline unavailable")
                                yield replace(event, reason="error",
                                              final_text=event.final_text or retained_text)
                                return
                            except AcceptanceError:
                                pass
                        if store is not None and binding is not None:
                            try:
                                current = await asyncio.to_thread(
                                    store.load, acceptance.root, binding.contract,
                                )
                                if current.baseline_id != binding.baseline_id:
                                    raise AcceptanceError("host baseline identity changed")
                            except AcceptanceError:
                                if cancel_signal.is_set():
                                    yield replace(event, reason="cancelled", cancelled=True,
                                                  final_text=event.final_text or retained_text)
                                    return
                                yield ErrorEvent(session_id=session_id, code="ACCEPTANCE_INVALID",
                                                 message="Host verification baseline unavailable")
                                yield replace(event, reason="error",
                                              final_text=event.final_text or retained_text)
                                return
                        checked = await asyncio.to_thread(acceptance.evaluate)
                        if cancel_signal.is_set():
                            yield replace(event, reason="cancelled", cancelled=True,
                                          final_text=event.final_text or retained_text)
                            return
                        issues = checked.issues
                        advisories = list(acceptance.feasibility_notices())
                        if any(not item.matched and not item.blocking and item.actual is not None
                               for item in checked.expectations):
                            advisories.insert(0, "scientific_difference")
                        event = replace(event, research_advisories=tuple(advisories))
                        if issues:
                            prompt = (await asyncio.to_thread(
                                repair.next_prompt, acceptance, checked, saw_work=saw_work,
                            ) if repair is not None else None)
                            if cancel_signal.is_set():
                                yield replace(event, reason="cancelled", cancelled=True,
                                              final_text=event.final_text or retained_text)
                                return
                            if prompt is not None:
                                if binding is not None:
                                    prompt = _bound_verification_task(
                                        prompt, acceptance.root, binding,
                                    )
                                retained_text = event.final_text or retained_text
                                # FinishEvent may be yielded before the delegate releases
                                # its session lock. Close it BEFORE same-session continuation.
                                close_segment = getattr(iterator, "aclose", None)
                                if close_segment is not None:
                                    await close_segment()
                                if cancel_signal.is_set():
                                    yield FinishEvent(session_id=session_id, reason="cancelled",
                                                      cancelled=True, final_text=retained_text)
                                    return
                                assert repair is not None
                                if event.final_text and not segment_has_text:
                                    # Some adapters return their report only at finish.
                                    # Checkpoint it before starting the repair, not solely
                                    # in this generator's in-memory fallback variable.
                                    yield TextEvent(session_id=session_id, text=event.final_text,
                                                    delta=False)
                                yield VerificationEvent(
                                    session_id=session_id, attempt=repair.attempts,
                                    max_attempts=repair.limit, issues=issue_categories(issues),
                                )
                                # Consumer can cancel while processing the repair event.
                                if cancel_signal.is_set():
                                    yield FinishEvent(session_id=session_id, reason="cancelled",
                                                      cancelled=True, final_text=retained_text)
                                    return
                                # A yielded verification notice is an asynchronous
                                # handoff: sources/authority may have changed since
                                # evaluate(). Recheck immediately before dispatch.
                                try:
                                    if store is not None and binding is not None:
                                        current = await asyncio.to_thread(
                                            store.load, acceptance.root, binding.contract,
                                        )
                                        if current.baseline_id != binding.baseline_id:
                                            raise AcceptanceError("host baseline identity changed")
                                    source_issues = await asyncio.to_thread(
                                        acceptance.verify_sources,
                                    )
                                except AcceptanceError:
                                    if cancel_signal.is_set():
                                        yield FinishEvent(session_id=session_id, reason="cancelled",
                                                          cancelled=True, final_text=retained_text)
                                        return
                                    yield ErrorEvent(session_id=session_id,
                                                     code="ACCEPTANCE_INVALID",
                                                     message="Host verification baseline "
                                                     "unavailable")
                                    yield FinishEvent(session_id=session_id, reason="error",
                                                      final_text=retained_text)
                                    return
                                if cancel_signal.is_set():
                                    yield FinishEvent(session_id=session_id, reason="cancelled",
                                                      cancelled=True, final_text=retained_text)
                                    return
                                if source_issues:
                                    yield ErrorEvent(
                                        session_id=session_id, code="ACCEPTANCE_FAILED",
                                        message="Frozen inputs changed before delivery repair",
                                        verification_issues=issue_categories(source_issues),
                                    )
                                    yield FinishEvent(session_id=session_id, reason="error",
                                                      final_text=retained_text)
                                    return
                                self._repair_boundaries.discard(session_id)
                                segment_text = ""
                                segment_has_text = False
                                iterator = self._runner.run(
                                    prompt, session_id=session_id, cwd=cwd, model=model,
                                    reasoning_effort=reasoning_effort, fallback=fallback,
                                    system_prompt=system_prompt, resume_context=None,
                                )
                                # Same state, counters, clock and acceptance snapshot.
                                continue
                            yield ErrorEvent(
                                session_id=session_id, code="ACCEPTANCE_FAILED",
                                message="Artifact checks failed: " + "; ".join(issues)[:2000],
                                verification_issues=issue_categories(issues),
                            )
                            yield replace(event, reason="error",
                                          final_text=event.final_text or retained_text)
                            return
                    yield replace(event, final_text=event.final_text or retained_text)
                    return
                yield event
                guard_events = state.inspect_time(now=self._clock())
                if not _has_stop(guard_events):
                    guard_events += state.observe(event, now=self._clock())
                for guard_event in guard_events:
                    yield guard_event
                if _has_stop(guard_events):
                    guarded = True
                    await self._cancel_guarded_run(session_id)
                    yield FinishEvent(session_id=session_id, reason="guarded",
                                      final_text=retained_text)
                    return
        finally:
            if timer is not None and not timer.done():
                timer.cancel()
            if next_event is not None and not next_event.done():
                next_event.cancel()
            pending = tuple(task for task in (timer, next_event) if task is not None)
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            try:
                close = getattr(iterator, "aclose", None)
                if close is not None:
                    try:
                        await close()
                    except (RuntimeError, StopAsyncIteration):
                        LOGGER.debug("Runner stream was already closed")
            finally:
                self._cancel_signals.pop(session_id, None)
                self._repair_boundaries.discard(session_id)
            if guarded:
                LOGGER.info("Task protection stopped session %s", session_id)

    async def cancel(self, session_id: str) -> bool:
        signal = self._cancel_signals.get(session_id)
        if signal is not None and session_id in self._repair_boundaries:
            signal.set()
            return True
        cancelled = await self._runner.cancel(session_id)
        if cancelled and signal is not None:
            signal.set()
        return cancelled

    async def runtime_generation(self, session_id: str) -> str | None:
        """Forward optional host-generation provenance through the guard."""

        method = getattr(self._runner, "runtime_generation", None)
        if method is None:
            return None
        return cast(str | None, await method(session_id))

    async def reset(self, session_id: str) -> None:
        await self._runner.reset(session_id)

    async def close(self) -> None:
        await self._runner.close()

    async def _cancel_guarded_run(self, session_id: str) -> None:
        try:
            await asyncio.wait_for(
                self._runner.cancel(session_id),
                timeout=self.policy.cancel_timeout_seconds,
            )
        except asyncio.TimeoutError:
            LOGGER.warning("Task-protection cancellation timed out")
        except Exception as error:
            LOGGER.warning(
                "Task-protection cancellation failed with %s",
                type(error).__name__,
            )


class _BaselineUnavailable(AcceptanceError):
    """Persistence failure is terminal, not a draft the model should rewrite."""


async def _freeze_research(
    acceptance: ArtifactAcceptance, store: AcceptanceStore | None,
    binding: StoredAcceptance | None, capture_point: str,
) -> tuple[ArtifactAcceptance, StoredAcceptance | None]:
    candidate = await asyncio.to_thread(acceptance.capture_deferred)
    if store is not None and binding is not None:
        try:
            binding = await asyncio.to_thread(
                store.freeze, binding, candidate, capture_point=capture_point,
            )
        except AcceptanceError:
            raise _BaselineUnavailable("host verification baseline unavailable") from None
        assert binding.snapshot is not None
        candidate = binding.snapshot
    return candidate, binding


def _bound_verification_task(task: str, workspace: Path, binding: StoredAcceptance) -> str:
    command = (f"xdsh-verify --workspace {shlex.quote(str(workspace))} "
               f"--contract {shlex.quote(binding.contract)} --profile research")
    authoritative = command + " --baseline " + shlex.quote(str(binding.path))
    revised_task = task.replace(command, authoritative)
    if binding.required_revision == 2:
        authoritative += " --research-revision 2"
    return revised_task + (
        "\nHost verification command: " + authoritative + "\n"
        "Use this host baseline for preflight and final --details checks, including after "
        "compaction or delivery repair. A standalone current-file check is not host acceptance. "
        "The baseline is read-only outside the workspace; never edit, delete or replace it. "
        "A draft freezes after its source references validate at a tool boundary. This does "
        "not prove pre-experiment registration. Missing/corrupt host state must be reported, "
        "not bypassed by running without --baseline."
    )


async def _read_next(iterator: AsyncIterator[RunnerEventT]) -> RunnerEventT:
    return await anext(iterator)


async def _sleep_for(sleep: Sleeper, seconds: float) -> None:
    await sleep(seconds)


def _has_stop(events: tuple[GuardEvent, ...]) -> bool:
    return any(event.action is GuardAction.STOP for event in events)


def _usage_tokens(event: UsageEvent) -> int:
    return sum(
        max(0, value)
        for value in (
            event.input_tokens,
            event.output_tokens,
            event.cache_read_tokens,
            event.cache_write_tokens,
        )
    )


def _tool_fingerprint(event: ToolStartEvent) -> str:
    try:
        arguments = json.dumps(
            event.arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        arguments = repr(event.arguments)
    payload = f"{event.name.strip().casefold()}\0{arguments}".encode(
        "utf-8",
        errors="replace",
    )
    return hashlib.sha256(payload).hexdigest()


def _is_poll_tool(name: str) -> bool:
    """Recognize status/output polling without coupling to one runtime plugin."""

    normalized = name.strip().casefold().replace("-", "_").replace(".", "_")
    return normalized in {
        "job_output",
        "job_status",
        "process_output",
        "process_status",
        "task_output",
        "task_status",
    }


def _content_hash(content: str) -> str:
    """Keep only a fixed digest of untrusted poll output in guard memory."""

    return hashlib.sha256(content[:64_000].encode("utf-8", errors="replace")).hexdigest()


def poll_decision_request(
    *,
    tool_name: str,
    stagnant_calls: int,
    max_stagnant_calls: int,
    scope_id: str = "poll",
) -> DecisionRequest:
    """Project poll pressure for optional observation; RunGuard stays decisive."""

    if stagnant_calls < 0 or max_stagnant_calls <= 0:
        raise ValueError("poll counts must be non-negative and bounded")
    normalized = tool_name.strip().casefold().replace("-", "_")[:64]
    if not normalized:
        raise ValueError("poll tool name is required")
    fallback = "stop" if stagnant_calls >= max_stagnant_calls else "wait"
    return DecisionRequest(
        kind="poll_action",
        scope_id=hashlib.sha256(scope_id.encode("utf-8")).hexdigest()[:16],
        options=(
            DecisionOption("wait", "Wait briefly for new structured output."),
            DecisionOption("inspect", "Inspect the job once for changed output."),
            DecisionOption("stop", "Stop after the host guard limit is reached."),
        ),
        fallback=fallback,
        context=(
            ("tool", normalized),
            ("stagnant_calls", stagnant_calls),
            ("max_stagnant_calls", max_stagnant_calls),
        ),
    )
