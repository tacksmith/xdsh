"""Stable, runner-neutral events exposed to product transports."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TypeAlias

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | dict[str, "JsonValue"] | list["JsonValue"]

# Content-free normalized request failure classes, never provider error bodies.
TRANSIENT_REQUEST_FAILURES = frozenset({"TIMEOUT", "TRANSPORT", "SERVER", "EMPTY_RESPONSE"})


class EventKind(str, Enum):
    """Event names are stable product vocabulary, not provider vocabulary."""

    TEXT = "text"
    REASONING = "reasoning"
    ACTIVITY = "activity"
    RETRY = "retry"
    VERIFICATION = "verification"
    TOOL_START = "tool_start"
    TOOL_RESULT = "tool_result"
    TODO = "todo"
    USAGE = "usage"
    ARTIFACT = "artifact"
    SUBAGENT = "subagent"
    RUNTIME = "runtime"
    GUARD = "guard"
    COMPACTION = "compaction"
    FINISH = "finish"
    ERROR = "error"


class GuardAction(str, Enum):
    """Whether a task protection event warns or authoritatively stops a run."""

    WARNING = "warning"
    STOP = "stop"


class GuardReason(str, Enum):
    """Stable reasons emitted by runner-neutral task protection."""

    RETRY_BUDGET = "retry_budget"
    MODEL_STEP_BUDGET = "model_step_budget"
    TOOL_BUDGET = "tool_budget"
    REPEATED_TOOL = "repeated_tool"
    TOOL_ERROR_BUDGET = "tool_error_budget"
    TOKEN_BUDGET = "token_budget"
    SUBAGENT_BUDGET = "subagent_budget"
    STALLED = "stalled"
    TIME_BUDGET = "time_budget"


class CompactionAction(str, Enum):
    """Lifecycle phase of a structured runtime compaction event."""

    START = "start"
    SUMMARY = "summary"
    END = "end"
    PRUNE = "prune"


class ActivityPhase(str, Enum):
    """Observed stream phase, never a claim that work succeeded."""

    STREAM = "stream"
    TOOL_ARGUMENTS = "tool_arguments"


class SubagentAction(str, Enum):
    STARTED = "started"
    FINISHED = "finished"


@dataclass(frozen=True, slots=True, kw_only=True)
class RunnerEvent:
    """Fields shared by all normalized runner events."""

    session_id: str
    sequence: int | None = None
    timestamp_ms: int | None = None
    # XDSH-owned provenance for the runtime process that produced the event.
    # DSH RC2 does not provide a connection/generation field; this marker is
    # intentionally optional so older checkpoints and test doubles remain
    # compatible.
    runtime_generation: str | None = None
    kind: EventKind = field(init=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class TextEvent(RunnerEvent):
    kind: EventKind = field(init=False, default=EventKind.TEXT)
    text: str
    delta: bool = True


@dataclass(frozen=True, slots=True, kw_only=True)
class ReasoningEvent(RunnerEvent):
    kind: EventKind = field(init=False, default=EventKind.REASONING)
    text: str
    delta: bool = True


@dataclass(frozen=True, slots=True, kw_only=True)
class ActivityEvent(RunnerEvent):
    """Content-free observation of a structured runner stream.

    Some runtimes stream sensitive or very large tool arguments before they
    emit a complete tool call.  Product guards need the liveness signal, but
    transports and durable state must never receive those argument fragments.
    """

    kind: EventKind = field(init=False, default=EventKind.ACTIVITY)
    phase: ActivityPhase = ActivityPhase.STREAM
    # None preserves the legacy adapter contract; zero explicitly means no
    # content was observed. Neither a positive count nor legacy activity is
    # evidence of semantic correctness, tool success, or task completion.
    output_characters: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.phase, ActivityPhase):
            raise ValueError("activity phase must be normalized")
        value = self.output_characters
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 2**63
        ):
            raise ValueError("activity character count must be a nonnegative integer")

    @property
    def has_output(self) -> bool:
        """Whether to refresh activity under this adapter's explicit contract."""

        return self.output_characters is None or self.output_characters > 0


@dataclass(frozen=True, slots=True, kw_only=True)
class RetryEvent(RunnerEvent):
    """A recoverable upstream retry that should remain visible to transports."""

    kind: EventKind = field(init=False, default=EventKind.RETRY)
    provider: str | None = None
    # Provider metadata is optional because rc.2 only exposed a provider name
    # on some retry notifications.  When present, these fields let the outer
    # rate-limit guard distinguish a 429 from a cold-start/fallback retry
    # without scraping human-readable stdout.
    status_code: int | None = None
    retry_after_seconds: float | None = None
    # Actual scheduled wait, when reported by the runtime. This is distinct
    # from a provider hint, which an adapter may not honor or may discard.
    retry_delay_seconds: float | None = None
    attempt: int | None = None
    max_attempts: int | None = None
    rate_limited: bool = False
    failure_code: str | None = None

    def __post_init__(self) -> None:
        _validate_status_code(self.status_code)
        _validate_retry_after(self.retry_after_seconds)
        _validate_retry_after(self.retry_delay_seconds)
        _validate_attempt(self.attempt, "retry attempt")
        _validate_attempt(self.max_attempts, "retry maximum")
        if self.failure_code is not None and (
            not isinstance(self.failure_code, str)
            or self.failure_code not in TRANSIENT_REQUEST_FAILURES | {"RATE_LIMIT"}
        ):
            raise ValueError("retry failure code must be a normalized request failure")
        if (
            self.attempt is not None
            and self.max_attempts is not None
            and self.attempt > self.max_attempts
        ):
            raise ValueError("retry attempt cannot exceed its maximum")


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolStartEvent(RunnerEvent):
    kind: EventKind = field(init=False, default=EventKind.TOOL_START)
    call_id: str
    name: str
    arguments: JsonValue | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolResultEvent(RunnerEvent):
    kind: EventKind = field(init=False, default=EventKind.TOOL_RESULT)
    call_id: str
    name: str | None = None
    content: str = ""
    is_error: bool = False


@dataclass(frozen=True, slots=True)
class TodoItem:
    content: str
    status: str


@dataclass(frozen=True, slots=True, kw_only=True)
class TodoEvent(RunnerEvent):
    kind: EventKind = field(init=False, default=EventKind.TODO)
    items: tuple[TodoItem, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class UsageEvent(RunnerEvent):
    kind: EventKind = field(init=False, default=EventKind.USAGE)
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ArtifactEvent(RunnerEvent):
    kind: EventKind = field(init=False, default=EventKind.ARTIFACT)
    path: str
    operation: str


@dataclass(frozen=True, slots=True, kw_only=True)
class SubagentEvent(RunnerEvent):
    """Lifecycle-only view of an official DSH child agent."""

    kind: EventKind = field(init=False, default=EventKind.SUBAGENT)
    child_session_id: str
    action: SubagentAction
    status: str | None = None
    final_text: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeEvent(RunnerEvent):
    """A fresh runner process reached its first structured event."""

    kind: EventKind = field(init=False, default=EventKind.RUNTIME)
    cold: bool
    startup_ms: int
    attempt: int = 1

    def __post_init__(self) -> None:
        if self.startup_ms < 0:
            raise ValueError("runtime startup duration cannot be negative")
        if self.attempt <= 0:
            raise ValueError("runtime startup attempt must be positive")


@dataclass(frozen=True, slots=True, kw_only=True)
class GuardEvent(RunnerEvent):
    """A warning or stop produced from normalized events and elapsed time."""

    kind: EventKind = field(init=False, default=EventKind.GUARD)
    action: GuardAction
    reason: GuardReason
    observed: int
    limit: int


@dataclass(frozen=True, slots=True, kw_only=True)
class CompactionEvent(RunnerEvent):
    """A structured DSH compaction/pruning lifecycle notification.

    The event deliberately carries only bounded metadata.  The summary body
    itself is private runtime context and must never be copied into Telegram
    progress or the runner-neutral event stream.
    """

    kind: EventKind = field(init=False, default=EventKind.COMPACTION)
    action: CompactionAction
    compaction_id: str | None = None
    turn: int | None = None
    shadowed_items: int = 0
    shadowed_tokens: int = 0
    pruned_characters: int = 0
    provider: str | None = None
    model: str | None = None
    error: bool = False

    def __post_init__(self) -> None:
        if self.turn is not None and self.turn < 0:
            raise ValueError("compaction turn cannot be negative")
        if any(
            value < 0
            for value in (
                self.shadowed_items,
                self.shadowed_tokens,
                self.pruned_characters,
            )
        ):
            raise ValueError("compaction measurements cannot be negative")
        if self.compaction_id is not None and len(self.compaction_id) > 200:
            raise ValueError("compaction ID is too long")


RESEARCH_ADVISORIES = (
    "scientific_difference", "partial_scope", "scope_unknown", "resource_estimate_unknown",
    "memory_risk", "strategy_undecided",
)


@dataclass(frozen=True, slots=True, kw_only=True)
class FinishEvent(RunnerEvent):
    kind: EventKind = field(init=False, default=EventKind.FINISH)
    reason: str
    final_text: str = ""
    cancelled: bool = False
    # Content-free host findings, separate from the model's answer/JSON contract.
    research_advisories: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (not isinstance(self.research_advisories, tuple)
                or len(self.research_advisories) > len(RESEARCH_ADVISORIES)
                or any(item not in RESEARCH_ADVISORIES for item in self.research_advisories)):
            raise ValueError("invalid research advisory categories")


@dataclass(frozen=True, slots=True, kw_only=True)
class ErrorEvent(RunnerEvent):
    kind: EventKind = field(init=False, default=EventKind.ERROR)
    message: str
    code: str = "UNKNOWN"
    retryable: bool = False
    status_code: int | None = None
    retry_after_seconds: float | None = None
    # Host-generated categories only: never file bodies, paths or provider text.
    verification_issues: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_status_code(self.status_code)
        _validate_retry_after(self.retry_after_seconds)
        if (not isinstance(self.verification_issues, tuple)
                or len(self.verification_issues) > 7
                or any(issue not in VERIFICATION_ISSUES for issue in self.verification_issues)):
            raise ValueError("invalid verification issue categories")


VERIFICATION_ISSUES = (
    "checklist", "parameters", "fields", "numbers", "arrays", "inputs_changed", "missing_files",
)


@dataclass(frozen=True, slots=True, kw_only=True)
class VerificationEvent(RunnerEvent):
    """Host delivery repair in progress, not an execution error or model claim."""

    kind: EventKind = field(init=False, default=EventKind.VERIFICATION)
    attempt: int
    max_attempts: int
    issues: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_attempt(self.attempt, "repair attempt")
        _validate_attempt(self.max_attempts, "repair maximum")
        if self.attempt > self.max_attempts or self.max_attempts > 2:
            raise ValueError("invalid repair bound")
        if (not isinstance(self.issues, tuple) or len(self.issues) > 7
                or any(issue not in VERIFICATION_ISSUES for issue in self.issues)):
            raise ValueError("invalid repair issue categories")


def _validate_status_code(value: int | None) -> None:
    if value is not None and (isinstance(value, bool) or not 100 <= value <= 599):
        raise ValueError("HTTP status code must be between 100 and 599")


def _validate_retry_after(value: float | None) -> None:
    if value is not None and (value < 0 or value != value or value == float("inf")):
        raise ValueError("retry-after duration must be a finite non-negative number")


def _validate_attempt(value: int | None, label: str) -> None:
    if value is not None and (isinstance(value, bool) or value <= 0):
        raise ValueError(f"{label} must be positive")


RunnerEventT: TypeAlias = (
    TextEvent
    | ReasoningEvent
    | ActivityEvent
    | RetryEvent
    | ToolStartEvent
    | ToolResultEvent
    | TodoEvent
    | UsageEvent
    | ArtifactEvent
    | SubagentEvent
    | RuntimeEvent
    | GuardEvent
    | CompactionEvent
    | FinishEvent
    | ErrorEvent
    | VerificationEvent
)
