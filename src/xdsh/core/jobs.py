"""Runner-neutral durable jobs with dependency gates and expiring leases."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import time
import uuid
from collections import Counter
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from xdsh.core.product_control import redact_sensitive_text
from xdsh.core.runner import ReasoningEffort

JobKind = Literal["background", "scheduled"]
JobStatus = Literal[
    "blocked",
    "queued",
    "running",
    "completed",
    "failed",
    "cancelled",
    "unknown",
    "manual_review",
]
JobOutcome = Literal["completed", "failed", "cancelled"]
JobInteractionKind = Literal["clarify", "steer"]
JobInteractionStatus = Literal["pending", "acknowledged", "rejected", "expired"]
JobInteractionAction = Literal["acknowledge", "reject"]
JobInteractionIntegrityStatus = Literal["empty", "verified", "legacy_unverified", "tampered"]
VerificationOutcome = Literal["accepted", "rejected"]
ScheduleKind = Literal["at", "interval", "cron"]
ScheduleControlAction = Literal["disable", "enable"]
JobControlAction = Literal[
    "cancel",
    "retry",
    "resolve_completed",
    "resolve_failed",
]

_ID = re.compile(r"^[a-f0-9]{8}$")
_HASH = re.compile(r"^[a-f0-9]{64}$")
_SESSION_KEY = re.compile(r"^(?:main|[a-z0-9]{8,16})$")
_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}$")
_WORKER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$")
_MAX_TASK_CHARS = 12_000
_MAX_RESULT_CHARS = 16_000
_MAX_INTERACTION_CHARS = 4_000
_MAX_INTERACTIONS_PER_JOB = 20
_MAX_INTERACTIONS_PER_OWNER = 100
_MAX_INTERACTION_EXPORT_BYTES = 32_000
_MAX_VERIFICATION_EVENTS_PER_JOB = 20
_MAX_JOB_EVENT_AUDIT_BYTES = 32_000
_SAFE_JOB_EVENT_ACTIONS = frozenset(
    {
        "submitted",
        "leased",
        "heartbeat",
        "completed",
        "failed",
        "cancelled",
        "cancel_requested",
        "retry_requested",
        "recovered_unknown",
        "dependencies_completed",
        "dependency_manual_review",
        "expired",
    }
)
_SAFE_JOB_EVENT_STATUS = frozenset(
    {"blocked", "queued", "running", "completed", "failed", "cancelled", "unknown", "manual_review"}
)


class DurableJobError(RuntimeError):
    """A durable job request or state transition is invalid."""


@dataclass(frozen=True, slots=True, kw_only=True)
class JobRequest:
    """Immutable execution settings captured when a job enters the board."""

    owner_user_id: int
    chat_id: int
    session_key: str
    source_session_id: str
    task: str
    model: str
    fallback_model: str | None = None
    reasoning_effort: ReasoningEffort = "off"
    show_thinking: bool = False
    kind: JobKind = "background"
    dependencies: tuple[str, ...] = ()
    scheduled_for: float | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ScheduleRequest:
    owner_user_id: int
    chat_id: int
    session_key: str
    source_session_id: str
    task: str
    model: str
    kind: ScheduleKind
    expression: str
    timezone: str
    fallback_model: str | None = None
    reasoning_effort: ReasoningEffort = "off"
    show_thinking: bool = False


@dataclass(frozen=True, slots=True)
class DurableJob:
    id: str
    owner_user_id: int
    chat_id: int
    session_key: str
    source_session_id: str
    runner_session_id: str
    task: str
    model: str
    fallback_model: str | None
    reasoning_effort: ReasoningEffort
    show_thinking: bool
    kind: JobKind
    status: JobStatus
    dependencies: tuple[str, ...]
    partial_text: str
    result_text: str
    error_text: str
    created_at: float
    scheduled_for: float
    updated_at: float
    attempts: int
    cancel_requested: bool
    schedule_id: str | None
    result_enqueued: bool


@dataclass(frozen=True, slots=True)
class DurableSchedule:
    id: str
    owner_user_id: int
    chat_id: int
    session_key: str
    source_session_id: str
    task: str
    model: str
    fallback_model: str | None
    reasoning_effort: ReasoningEffort
    show_thinking: bool
    kind: ScheduleKind
    expression: str
    timezone: str
    enabled: bool
    next_run_at: float | None
    last_scheduled_for: float | None
    created_at: float
    updated_at: float


@dataclass(frozen=True, slots=True)
class JobLease:
    job: DurableJob
    worker_id: str
    token: str
    expires_at: float


@dataclass(frozen=True, slots=True)
class BoardTick:
    leases: tuple[JobLease, ...]
    unknown_jobs: tuple[DurableJob, ...] = ()


@dataclass(frozen=True, slots=True)
class JobBoardSnapshot:
    jobs: tuple[DurableJob, ...]
    counts: dict[str, int]
    schedules: tuple[DurableSchedule, ...] = ()


@dataclass(frozen=True, slots=True)
class JobCard:
    """Content-free Kanban card for bounded status views."""

    id: str
    kind: JobKind
    status: JobStatus
    attempts: int
    created_at: float
    updated_at: float
    scheduled_for: float
    lease_remaining_seconds: float | None
    cancel_requested: bool
    dependency_count: int
    pending_interaction_count: int
    partial_characters: int
    result_characters: int
    age_seconds: float


@dataclass(frozen=True, slots=True)
class JobBoardProjection:
    """Owner-scoped Kanban projection without task/result payloads."""

    cards: tuple[JobCard, ...] = ()
    counts: tuple[tuple[str, int], ...] = ()
    total: int = 0
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class JobHealthSummary:
    """Content-free job counts and lease age for operational checks."""

    counts: tuple[tuple[str, int], ...] = ()
    running_expired: int = 0
    oldest_running_age_seconds: float | None = None
    pending_interactions: int = 0
    oldest_pending_interaction_age_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class JobInteractionRequest:
    """A durable clarify/steer request that is never implicit runner input."""

    id: str
    job_id: str
    owner_user_id: int
    chat_id: int
    kind: JobInteractionKind
    text: str
    job_status: JobStatus
    status: JobInteractionStatus
    created_at: float
    updated_at: float
    decided_at: float | None = None
    decision_actor: str | None = None


@dataclass(frozen=True, slots=True)
class JobInteractionEvent:
    """Append-only audit event for one clarify/steer request."""

    sequence: int
    request_id: str
    action: str
    status: JobInteractionStatus
    actor: str
    created_at: float


@dataclass(frozen=True, slots=True)
class JobInteractionHealthSummary:
    """Owner-scoped, content-free pending request summary."""

    pending: int = 0
    oldest_age_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class JobInteractionIntegrity:
    """Content-free result of verifying the append-only interaction chain."""

    status: JobInteractionIntegrityStatus
    checked_events: int = 0
    first_bad_sequence: int | None = None


@dataclass(frozen=True, slots=True)
class JobEventIntegrity:
    """Content-free integrity result for the append-only job event stream."""

    status: JobInteractionIntegrityStatus
    checked_events: int = 0
    first_bad_sequence: int | None = None


@dataclass(frozen=True, slots=True)
class JobEvent:
    sequence: int
    job_id: str
    action: str
    status: JobStatus
    actor: str
    created_at: float
    prev_hash: str = ""
    event_hash: str = ""


@dataclass(frozen=True, slots=True)
class JobEventAuditEntry:
    """Safe, content-free projection of one durable job event."""

    sequence: int
    job_id: str
    action: str
    status: JobStatus
    actor: str
    created_at: float


@dataclass(frozen=True, slots=True)
class JobEventAudit:
    """Bounded owner-scoped projection of the durable job-event stream."""

    events: tuple[JobEventAuditEntry, ...] = ()
    total: int = 0
    omitted: int = 0
    truncated: bool = False
    integrity: JobEventIntegrity = JobEventIntegrity(status="empty")


@dataclass(frozen=True, slots=True)
class ScheduleEvent:
    sequence: int
    schedule_id: str
    action: str
    actor: str
    created_at: float


class DurableJobBoard:
    """Own dependency, capacity, lease, and terminal job transitions.

    A claimed job is never automatically re-run after its lease becomes
    ambiguous. Recovery moves it to ``unknown`` for an explicit user decision.
    """

    def __init__(
        self,
        state_root: Path,
        *,
        id_factory: Callable[[], str] | None = None,
        clock: Callable[[], float] | None = None,
        max_parallel_jobs: int = 3,
        max_pending_jobs: int = 50,
        max_schedules: int = 50,
    ) -> None:
        root = state_root.expanduser()
        if (
            root == Path(root.anchor)
            or root.is_symlink()
            or ".xclaude" in root.parts
            or ".dsh" in root.parts
        ):
            raise DurableJobError("持久工作狀態根目錄不安全")
        if not 1 <= max_parallel_jobs <= 3:
            raise ValueError("parallel job limit must be between 1 and 3")
        if not 1 <= max_pending_jobs <= 100:
            raise ValueError("pending job limit must be between 1 and 100")
        if not 1 <= max_schedules <= 100:
            raise ValueError("schedule limit must be between 1 and 100")
        self.root = root.resolve() / "jobs"
        self.path = self.root / "board.sqlite3"
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex[:8])
        self._clock = clock or time.time
        self.max_parallel_jobs = max_parallel_jobs
        self.max_pending_jobs = max_pending_jobs
        self.max_schedules = max_schedules

    def submit(self, request: JobRequest) -> DurableJob:
        """Persist one bounded job; dependencies must already exist in scope."""

        normalized = _request(request)
        now = self._timestamp()
        due = now if normalized.scheduled_for is None else normalized.scheduled_for
        with closing(self._connect(create=True)) as connection:
            with connection:
                pending = self._pending_count(
                    connection,
                    owner_user_id=normalized.owner_user_id,
                )
                if pending >= self.max_pending_jobs:
                    raise DurableJobError(
                        f"每個 owner 最多保留 {self.max_pending_jobs} 個未結案工作"
                    )
                dependencies = tuple(_entry_id(item) for item in normalized.dependencies)
                if len(set(dependencies)) != len(dependencies) or len(dependencies) > 8:
                    raise DurableJobError("工作依賴必須唯一且最多 8 個")
                for dependency_id in dependencies:
                    row = connection.execute(
                        "SELECT owner_user_id, chat_id FROM jobs WHERE id = ?",
                        (dependency_id,),
                    ).fetchone()
                    if row is None or (int(row[0]), int(row[1])) != (
                        normalized.owner_user_id,
                        normalized.chat_id,
                    ):
                        raise DurableJobError(f"工作依賴不存在或不屬於目前 owner: {dependency_id}")
                job_id = self._new_id(connection)
                runner_session_id = _runner_session_id(normalized.chat_id, job_id)
                status: JobStatus = "blocked" if dependencies else "queued"
                connection.execute(
                    """
                    INSERT INTO jobs (
                        id, owner_user_id, chat_id, session_key, source_session_id,
                        runner_session_id, task, model, fallback_model,
                        reasoning_effort, show_thinking, kind, status, partial_text,
                        result_text, error_text, created_at, scheduled_for, updated_at,
                        attempts, cancel_requested, worker_id, lease_token, lease_expires,
                        schedule_id, result_enqueued
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', '', '',
                              ?, ?, ?, 0, 0, NULL, NULL, NULL, NULL, 0)
                    """,
                    (
                        job_id,
                        normalized.owner_user_id,
                        normalized.chat_id,
                        normalized.session_key,
                        normalized.source_session_id,
                        runner_session_id,
                        normalized.task,
                        normalized.model,
                        normalized.fallback_model,
                        normalized.reasoning_effort,
                        int(normalized.show_thinking),
                        normalized.kind,
                        status,
                        now,
                        due,
                        now,
                    ),
                )
                connection.executemany(
                    "INSERT INTO job_dependencies (job_id, dependency_id) VALUES (?, ?)",
                    ((job_id, dependency_id) for dependency_id in dependencies),
                )
                self._record_event(
                    connection,
                    job_id=job_id,
                    action="submitted",
                    status=status,
                    actor=f"owner:{normalized.owner_user_id}",
                    now=now,
                )
                row = self._job_row(connection, job_id)
        self._secure()
        return self._job(connection=None, row=row, dependencies=dependencies)

    def schedule(self, request: ScheduleRequest) -> DurableSchedule:
        """Persist an owner-scoped schedule using an explicit IANA timezone."""

        execution = _request(
            JobRequest(
                owner_user_id=request.owner_user_id,
                chat_id=request.chat_id,
                session_key=request.session_key,
                source_session_id=request.source_session_id,
                task=request.task,
                model=request.model,
                fallback_model=request.fallback_model,
                reasoning_effort=request.reasoning_effort,
                show_thinking=request.show_thinking,
                kind="scheduled",
            )
        )
        if request.kind not in {"at", "interval", "cron"}:
            raise DurableJobError("排程類型無效")
        expression = request.expression.strip()
        if not expression or len(expression) > 200:
            raise DurableJobError("排程 expression 無效")
        timezone = request.timezone.strip()
        try:
            zone = ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise DurableJobError("排程 timezone 必須是有效 IANA 名稱") from error
        now = self._timestamp()
        if request.kind == "at":
            next_run_at = _at_timestamp(expression, zone)
        elif request.kind == "interval":
            next_run_at = now + _interval_seconds(expression)
        else:
            next_run_at = _next_cron_timestamp(expression, zone, now)
        if next_run_at < now + 60.0:
            raise DurableJobError("排程首次執行必須至少在 60 秒後")
        with closing(self._connect(create=True)) as connection:
            with connection:
                enabled_count = int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM schedules
                        WHERE owner_user_id = ? AND enabled = 1
                        """,
                        (execution.owner_user_id,),
                    ).fetchone()[0]
                )
                if enabled_count >= self.max_schedules:
                    raise DurableJobError(
                        f"每個 owner 最多啟用 {self.max_schedules} 個排程"
                    )
                schedule_id = self._new_id(connection)
                connection.execute(
                    """
                    INSERT INTO schedules (
                        id, owner_user_id, chat_id, session_key, source_session_id,
                        task, model, fallback_model, reasoning_effort, show_thinking,
                        kind, expression, timezone, enabled, next_run_at,
                        last_scheduled_for, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, NULL, ?, ?)
                    """,
                    (
                        schedule_id,
                        execution.owner_user_id,
                        execution.chat_id,
                        execution.session_key,
                        execution.source_session_id,
                        execution.task,
                        execution.model,
                        execution.fallback_model,
                        execution.reasoning_effort,
                        int(execution.show_thinking),
                        request.kind,
                        expression,
                        timezone,
                        next_run_at,
                        now,
                        now,
                    ),
                )
                self._record_schedule_event(
                    connection,
                    schedule_id=schedule_id,
                    action="created",
                    actor=f"owner:{execution.owner_user_id}",
                    now=now,
                )
                row = self._schedule_row(connection, schedule_id)
        self._secure()
        return _schedule(row)

    def control_schedule(
        self,
        owner_user_id: int,
        chat_id: int,
        schedule_id: str,
        *,
        action: ScheduleControlAction,
    ) -> DurableSchedule:
        owner = _owner_id(owner_user_id)
        chat = _chat_id(chat_id)
        item_id = _entry_id(schedule_id)
        now = self._timestamp()
        with closing(self._connect(create=False)) as connection:
            with connection:
                row = self._schedule_row(connection, item_id)
                schedule = _schedule(row)
                if (schedule.owner_user_id, schedule.chat_id) != (owner, chat):
                    raise DurableJobError(
                        f"排程不存在或不屬於目前 owner: {item_id}"
                    )
                if action == "disable":
                    if not schedule.enabled:
                        raise DurableJobError(f"排程 {item_id} 已停用")
                    enabled = 0
                    next_run_at = schedule.next_run_at
                elif action == "enable":
                    if schedule.enabled:
                        raise DurableJobError(f"排程 {item_id} 已啟用")
                    enabled_count = int(
                        connection.execute(
                            """
                            SELECT COUNT(*) FROM schedules
                            WHERE owner_user_id = ? AND enabled = 1
                            """,
                            (owner,),
                        ).fetchone()[0]
                    )
                    if enabled_count >= self.max_schedules:
                        raise DurableJobError(
                            f"每個 owner 最多啟用 {self.max_schedules} 個排程"
                        )
                    enabled = 1
                    if schedule.kind == "at":
                        next_run_at = schedule.next_run_at
                        if next_run_at is None or next_run_at < now + 60.0:
                            raise DurableJobError("已過期的一次性排程不能重新啟用")
                    elif schedule.kind == "interval":
                        next_run_at = now + _interval_seconds(schedule.expression)
                    else:
                        try:
                            zone = ZoneInfo(schedule.timezone)
                        except (ZoneInfoNotFoundError, ValueError) as error:
                            raise DurableJobError("排程 timezone 已不可用") from error
                        next_run_at = _next_cron_timestamp(
                            schedule.expression,
                            zone,
                            now,
                        )
                else:
                    raise ValueError("schedule control action is invalid")
                connection.execute(
                    """
                    UPDATE schedules SET enabled = ?, next_run_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (enabled, next_run_at, now, item_id),
                )
                self._record_schedule_event(
                    connection,
                    schedule_id=item_id,
                    action="enabled" if enabled else "disabled",
                    actor=f"owner:{owner}",
                    now=now,
                )
                result = self._schedule_row(connection, item_id)
        return _schedule(result)

    def schedule_history(
        self,
        owner_user_id: int,
        chat_id: int,
        schedule_id: str,
    ) -> tuple[ScheduleEvent, ...]:
        owner = _owner_id(owner_user_id)
        chat = _chat_id(chat_id)
        item_id = _entry_id(schedule_id)
        with closing(self._connect(create=False)) as connection:
            schedule = _schedule(self._schedule_row(connection, item_id))
            if (schedule.owner_user_id, schedule.chat_id) != (owner, chat):
                raise DurableJobError(
                    f"排程不存在或不屬於目前 owner: {item_id}"
                )
            rows = connection.execute(
                """
                SELECT sequence, schedule_id, action, actor, created_at
                FROM schedule_events WHERE schedule_id = ? ORDER BY sequence
                """,
                (item_id,),
            ).fetchall()
        return tuple(
            ScheduleEvent(
                sequence=int(row[0]),
                schedule_id=str(row[1]),
                action=str(row[2]),
                actor=str(row[3]),
                created_at=float(row[4]),
            )
            for row in rows
        )

    def get(self, owner_user_id: int, chat_id: int, job_id: str) -> DurableJob:
        owner = _owner_id(owner_user_id)
        chat = _chat_id(chat_id)
        item_id = _entry_id(job_id)
        with closing(self._connect(create=False)) as connection:
            row = self._job_row(connection, item_id)
            if (cast(int, row[1]), cast(int, row[2])) != (owner, chat):
                raise DurableJobError(f"工作不存在或不屬於目前 owner: {item_id}")
            dependencies = self._dependencies(connection, item_id)
        return self._job(connection=None, row=row, dependencies=dependencies)

    def snapshot(self, owner_user_id: int, chat_id: int) -> JobBoardSnapshot:
        owner = _owner_id(owner_user_id)
        chat = _chat_id(chat_id)
        if not self.path.is_file():
            return JobBoardSnapshot((), {}, ())
        with closing(self._connect(create=False)) as connection:
            rows = connection.execute(
                """
                SELECT id, owner_user_id, chat_id, session_key, source_session_id,
                       runner_session_id, task, model, fallback_model,
                       reasoning_effort, show_thinking, kind, status, partial_text,
                       result_text, error_text, created_at, scheduled_for, updated_at,
                       attempts, cancel_requested, schedule_id, result_enqueued
                FROM jobs WHERE owner_user_id = ? AND chat_id = ?
                ORDER BY created_at DESC, id DESC
                """,
                (owner, chat),
            ).fetchall()
            jobs = tuple(
                self._job(
                    connection=None,
                    row=cast(tuple[object, ...], row),
                    dependencies=self._dependencies(connection, str(row[0])),
                )
                for row in rows
            )
            schedule_rows = connection.execute(
                """
                SELECT id, owner_user_id, chat_id, session_key, source_session_id,
                       task, model, fallback_model, reasoning_effort, show_thinking,
                       kind, expression, timezone, enabled, next_run_at,
                       last_scheduled_for, created_at, updated_at
                FROM schedules WHERE owner_user_id = ? AND chat_id = ?
                ORDER BY created_at DESC, id DESC
                """,
                (owner, chat),
            ).fetchall()
            schedules = tuple(
                _schedule(cast(tuple[object, ...], row)) for row in schedule_rows
            )
        return JobBoardSnapshot(
            jobs=jobs,
            counts=dict(Counter(job.status for job in jobs)),
            schedules=schedules,
        )

    def projection(
        self,
        owner_user_id: int,
        chat_id: int,
        *,
        status: JobStatus | None = None,
        kind: JobKind | None = None,
        limit: int = 20,
        now: float | None = None,
    ) -> JobBoardProjection:
        """Return a bounded owner-scoped Kanban view without task text.

        The projection is intentionally read-only and suitable for a status
        card or watchdog.  Operators can request full task/result text only
        through the existing owner-scoped ``get`` path for one job.
        """

        owner = _owner_id(owner_user_id)
        chat = _chat_id(chat_id)
        if status is not None and status not in {
            "blocked",
            "queued",
            "running",
            "completed",
            "failed",
            "cancelled",
            "unknown",
            "manual_review",
        }:
            raise DurableJobError("工作狀態篩選無效")
        if kind is not None and kind not in {"background", "scheduled"}:
            raise DurableJobError("工作類型篩選無效")
        if not 1 <= limit <= 100:
            raise ValueError("projection limit must be between 1 and 100")
        timestamp = self._timestamp() if now is None else float(now)
        if not math.isfinite(timestamp) or timestamp < 0:
            raise DurableJobError("持久工作時間無效")
        if not self.path.is_file():
            return JobBoardProjection()
        clauses = ["owner_user_id = ?", "chat_id = ?"]
        parameters: list[object] = [owner, chat]
        if status is not None:
            clauses.append("status = ?")
            parameters.append(status)
        if kind is not None:
            clauses.append("kind = ?")
            parameters.append(kind)
        where = " AND ".join(clauses)
        try:
            with closing(self._connect_read_only()) as connection:
                interactions_table = connection.execute(
                    """
                    SELECT 1 FROM sqlite_master
                    WHERE type = 'table' AND name = 'job_interactions'
                    """
                ).fetchone() is not None
                count_rows = connection.execute(
                    f"SELECT status, COUNT(*) FROM jobs WHERE {where} GROUP BY status",
                    tuple(parameters),
                ).fetchall()
                pending_expression = (
                    "(SELECT COUNT(*) FROM job_interactions AS i "
                    "WHERE i.job_id = j.id AND i.status = 'pending')"
                    if interactions_table
                    else "0"
                )
                rows = connection.execute(
                    f"""
                    SELECT j.id, j.kind, j.status, j.attempts, j.created_at,
                           j.updated_at, j.scheduled_for, j.lease_expires,
                           j.cancel_requested, COUNT(d.dependency_id),
                           {pending_expression},
                           LENGTH(j.partial_text), LENGTH(j.result_text)
                    FROM jobs AS j
                    LEFT JOIN job_dependencies AS d ON d.job_id = j.id
                    WHERE {where}
                    GROUP BY j.id
                    ORDER BY CASE j.status
                        WHEN 'running' THEN 0
                        WHEN 'queued' THEN 1
                        WHEN 'blocked' THEN 2
                        WHEN 'manual_review' THEN 3
                        WHEN 'unknown' THEN 4
                        WHEN 'failed' THEN 5
                        WHEN 'cancelled' THEN 6
                        ELSE 7 END,
                        j.updated_at DESC, j.id DESC
                    LIMIT ?
                    """,
                    (*parameters, limit),
                ).fetchall()
        except sqlite3.Error as error:
            raise DurableJobError("持久工作資料庫不可讀") from error
        counts = tuple(sorted((str(raw_status), int(count)) for raw_status, count in count_rows))
        cards = tuple(
            JobCard(
                id=str(row[0]),
                kind=cast(JobKind, str(row[1])),
                status=cast(JobStatus, str(row[2])),
                attempts=int(row[3]),
                created_at=float(row[4]),
                updated_at=float(row[5]),
                scheduled_for=float(row[6]),
                lease_remaining_seconds=(
                    float(row[7]) - timestamp if row[7] is not None else None
                ),
                cancel_requested=bool(row[8]),
                dependency_count=int(row[9]),
                pending_interaction_count=int(row[10]),
                partial_characters=max(0, int(row[11] or 0)),
                result_characters=max(0, int(row[12] or 0)),
                age_seconds=max(0.0, timestamp - float(row[5])),
            )
            for row in rows
        )
        total = sum(count for _status, count in counts)
        return JobBoardProjection(
            cards=cards,
            counts=counts,
            total=total,
            truncated=total > len(cards),
        )

    def card(
        self,
        owner_user_id: int,
        chat_id: int,
        job_id: str,
        *,
        now: float | None = None,
    ) -> JobCard:
        """Return one owner-authorized content-free card without task/result text."""

        owner = _owner_id(owner_user_id)
        chat = _chat_id(chat_id)
        item_id = _entry_id(job_id)
        timestamp = self._timestamp() if now is None else float(now)
        if timestamp < 0:
            raise DurableJobError("持久工作時間無效")
        if not self.path.is_file():
            raise DurableJobError("工作不存在或不屬於目前 owner")
        try:
            with closing(self._connect_read_only()) as connection:
                row = connection.execute(
                    """
                    SELECT id, kind, status, attempts, created_at, updated_at,
                           scheduled_for, lease_expires, cancel_requested,
                           LENGTH(partial_text), LENGTH(result_text)
                    FROM jobs
                    WHERE id = ? AND owner_user_id = ? AND chat_id = ?
                    """,
                    (item_id, owner, chat),
                ).fetchone()
                if row is None:
                    raise DurableJobError("工作不存在或不屬於目前 owner")
                dependency_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM job_dependencies WHERE job_id = ?",
                        (item_id,),
                    ).fetchone()[0]
                )
                interactions_table = connection.execute(
                    """
                    SELECT 1 FROM sqlite_master
                    WHERE type = 'table' AND name = 'job_interactions'
                    """
                ).fetchone() is not None
                if interactions_table:
                    pending_interactions = int(
                        connection.execute(
                            """
                            SELECT COUNT(*) FROM job_interactions
                            WHERE job_id = ? AND status = 'pending'
                            """,
                            (item_id,),
                        ).fetchone()[0]
                    )
                else:
                    pending_interactions = 0
        except DurableJobError:
            raise
        except sqlite3.Error as error:
            raise DurableJobError("持久工作資料庫不可讀") from error
        return JobCard(
            id=str(row[0]),
            kind=cast(JobKind, str(row[1])),
            status=cast(JobStatus, str(row[2])),
            attempts=int(row[3]),
            created_at=float(row[4]),
            updated_at=float(row[5]),
            scheduled_for=float(row[6]),
            lease_remaining_seconds=(
                float(row[7]) - timestamp if row[7] is not None else None
            ),
            cancel_requested=bool(row[8]),
            dependency_count=dependency_count,
            pending_interaction_count=pending_interactions,
            partial_characters=max(0, int(row[9] or 0)),
            result_characters=max(0, int(row[10] or 0)),
            age_seconds=max(0.0, timestamp - float(row[5])),
        )

    def health_summary(
        self,
        *,
        now: float | None = None,
        owner_user_id: int | None = None,
        chat_id: int | None = None,
    ) -> JobHealthSummary:
        """Read status/lease metadata, optionally scoped to one owner/chat.

        The default remains a global aggregate for the reconciler. Supplying
        both identity fields gives an owner-scoped view for diagnostics without
        selecting task or result text.
        """

        if (owner_user_id is None) != (chat_id is None):
            raise ValueError("owner_user_id and chat_id must be supplied together")
        owner = _owner_id(owner_user_id) if owner_user_id is not None else None
        chat = _chat_id(chat_id) if chat_id is not None else None
        if not self.path.is_file():
            return JobHealthSummary()
        timestamp = self._timestamp() if now is None else float(now)
        if timestamp < 0:
            raise DurableJobError("持久工作時間無效")
        job_scope = ""
        job_parameters: tuple[object, ...] = ()
        if owner is not None and chat is not None:
            job_scope = " AND owner_user_id = ? AND chat_id = ?"
            job_parameters = (owner, chat)
        try:
            with closing(self._connect_read_only()) as connection:
                rows = connection.execute(
                    f"SELECT status, COUNT(*) FROM jobs "
                    f"WHERE 1 = 1{job_scope} GROUP BY status",
                    job_parameters,
                ).fetchall()
                expired = connection.execute(
                    f"""
                    SELECT COUNT(*) FROM jobs
                    WHERE status = 'running' AND lease_expires IS NOT NULL AND lease_expires <= ?
                    {job_scope}
                    """,
                    (timestamp, *job_parameters),
                ).fetchone()
                oldest = connection.execute(
                    f"""
                    SELECT MIN(updated_at) FROM jobs
                    WHERE status = 'running'{job_scope}
                    """,
                    job_parameters,
                ).fetchone()
                interactions_table = connection.execute(
                    """
                    SELECT 1 FROM sqlite_master
                    WHERE type = 'table' AND name = 'job_interactions'
                    """
                ).fetchone() is not None
                if interactions_table:
                    pending_interactions_row = connection.execute(
                        f"""
                        SELECT COUNT(*), MIN(created_at)
                        FROM job_interactions WHERE status = 'pending'
                        {job_scope}
                        """,
                        job_parameters,
                    ).fetchone()
                else:
                    pending_interactions_row = (0, None)
        except sqlite3.Error as error:
            raise DurableJobError("持久工作資料庫不可讀") from error
        oldest_updated = oldest[0] if oldest is not None else None
        oldest_age = (
            max(0.0, timestamp - float(oldest_updated))
            if oldest_updated is not None
            else None
        )
        pending_interactions = int(pending_interactions_row[0] or 0)
        oldest_pending_created = pending_interactions_row[1]
        oldest_pending_age = (
            max(0.0, timestamp - float(oldest_pending_created))
            if oldest_pending_created is not None
            else None
        )
        return JobHealthSummary(
            counts=tuple(sorted((str(status), int(count)) for status, count in rows)),
            running_expired=int(expired[0]) if expired is not None else 0,
            oldest_running_age_seconds=oldest_age,
            pending_interactions=pending_interactions,
            oldest_pending_interaction_age_seconds=oldest_pending_age,
        )

    def pending_results(
        self,
        allowed_owner_ids: frozenset[int] | None = None,
    ) -> tuple[DurableJob, ...]:
        """Return terminal states that still need durable transport enqueue."""

        if not self.path.is_file():
            return ()
        owners = _allowed_owners(allowed_owner_ids)
        owner_clause, owner_parameters = _owner_filter(
            owners,
            column="owner_user_id",
        )
        with closing(self._connect(create=False)) as connection:
            rows = connection.execute(
                f"""
                SELECT id, owner_user_id, chat_id, session_key, source_session_id,
                       runner_session_id, task, model, fallback_model,
                       reasoning_effort, show_thinking, kind, status, partial_text,
                       result_text, error_text, created_at, scheduled_for, updated_at,
                       attempts, cancel_requested, schedule_id, result_enqueued
                FROM jobs
                WHERE result_enqueued = 0
                  AND status IN ('completed', 'failed', 'cancelled', 'unknown',
                                 'manual_review')
                  {owner_clause}
                ORDER BY updated_at, id
                """,
                owner_parameters,
            ).fetchall()
            return tuple(
                self._job(
                    connection=None,
                    row=cast(tuple[object, ...], row),
                    dependencies=self._dependencies(connection, str(row[0])),
                )
                for row in rows
            )

    def acknowledge_result(self, job: DurableJob) -> DurableJob:
        """Mark one exact terminal attempt as present in the durable outbox."""

        with closing(self._connect(create=False)) as connection:
            with connection:
                cursor = connection.execute(
                    """
                    UPDATE jobs SET result_enqueued = 1, updated_at = ?
                    WHERE id = ? AND status = ? AND attempts = ?
                      AND result_enqueued = 0
                    """,
                    (self._timestamp(), job.id, job.status, job.attempts),
                )
                if cursor.rowcount != 1:
                    raise DurableJobError(
                        f"工作結果已入列或狀態已改變: {job.id}"
                    )
                row = self._job_row(connection, job.id)
                dependencies = self._dependencies(connection, job.id)
        return self._job(connection=None, row=row, dependencies=dependencies)

    def history(
        self,
        owner_user_id: int,
        chat_id: int,
        job_id: str,
    ) -> tuple[JobEvent, ...]:
        owner = _owner_id(owner_user_id)
        chat = _chat_id(chat_id)
        item_id = _entry_id(job_id)
        with closing(self._connect(create=False)) as connection:
            job_row = self._job_row(connection, item_id)
            if (cast(int, job_row[1]), cast(int, job_row[2])) != (owner, chat):
                raise DurableJobError(
                    f"工作不存在或不屬於目前 owner: {item_id}"
                )
            rows = connection.execute(
                """
                SELECT sequence, job_id, action, status, actor, created_at,
                       prev_hash, event_hash
                FROM job_events WHERE job_id = ? ORDER BY sequence
                """,
                (item_id,),
            ).fetchall()
        return tuple(_job_event(cast(tuple[object, ...], row)) for row in rows)

    def job_event_audit(
        self,
        owner_user_id: int,
        chat_id: int,
        job_id: str | None = None,
        *,
        limit: int = 20,
    ) -> JobEventAudit:
        """Return a bounded, redacted owner-scoped job-event projection.

        The raw append-only history remains available to the integrity checker,
        but this seam deliberately never returns action arguments (including
        bundle fingerprints), worker IDs, task text, or result text.  A
        missing or cross-owner job is indistinguishable from an unauthorized
        lookup and fails closed when a specific ID is requested.
        """

        owner = _owner_id(owner_user_id)
        chat = _chat_id(chat_id)
        if not 1 <= limit <= 100:
            raise ValueError("job event audit limit must be between 1 and 100")
        item_id = None if job_id is None else _entry_id(job_id)
        if not self.path.is_file():
            return JobEventAudit()
        try:
            with closing(self._connect_read_only()) as connection:
                jobs_table = connection.execute(
                    """
                    SELECT 1 FROM sqlite_master
                    WHERE type = 'table' AND name = 'jobs'
                    """
                ).fetchone() is not None
                events_table = connection.execute(
                    """
                    SELECT 1 FROM sqlite_master
                    WHERE type = 'table' AND name = 'job_events'
                    """
                ).fetchone() is not None
                if not jobs_table or not events_table:
                    if item_id is not None and not jobs_table:
                        raise DurableJobError(
                            f"工作不存在或不屬於目前 owner: {item_id}"
                        )
                    return JobEventAudit()
                if item_id is not None:
                    job_row = connection.execute(
                        "SELECT owner_user_id, chat_id FROM jobs WHERE id = ?",
                        (item_id,),
                    ).fetchone()
                    if job_row is None or (int(job_row[0]), int(job_row[1])) != (
                        owner,
                        chat,
                    ):
                        raise DurableJobError(
                            f"工作不存在或不屬於目前 owner: {item_id}"
                        )
                    where = "e.job_id = ?"
                    parameters: tuple[object, ...] = (item_id,)
                else:
                    where = "j.owner_user_id = ? AND j.chat_id = ?"
                    parameters = (owner, chat)
                total_row = connection.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM job_events AS e
                    JOIN jobs AS j ON j.id = e.job_id
                    WHERE {where}
                    """,
                    parameters,
                ).fetchone()
                total = int(total_row[0] or 0)
                rows = connection.execute(
                    f"""
                    SELECT e.sequence, e.job_id, e.action, e.status, e.actor, e.created_at
                    FROM job_events AS e
                    JOIN jobs AS j ON j.id = e.job_id
                    WHERE {where}
                    ORDER BY e.sequence DESC LIMIT ?
                    """,
                    (*parameters, limit),
                ).fetchall()
        except sqlite3.Error as error:
            raise DurableJobError("工作事件 audit 資料庫不可讀") from error
        safe_entries: list[JobEventAuditEntry] = []
        for raw in reversed(rows):
            created_at = float(raw[5])
            if not math.isfinite(created_at) or created_at < 0:
                raise DurableJobError("工作事件 audit 時間無效")
            safe_entries.append(
                JobEventAuditEntry(
                    sequence=int(raw[0]),
                    job_id=str(raw[1]),
                    action=_safe_job_event_action(str(raw[2])),
                    status=_safe_job_event_status(str(raw[3])),
                    actor=_safe_job_event_actor(str(raw[4])),
                    created_at=created_at,
                )
            )
        entries = tuple(safe_entries)
        omitted = max(0, total - len(entries))
        return JobEventAudit(
            events=entries,
            total=total,
            omitted=omitted,
            truncated=omitted > 0,
            integrity=self.verify_job_event_integrity(),
        )

    def export_job_events(
        self,
        owner_user_id: int,
        chat_id: int,
        job_id: str | None = None,
        *,
        limit: int = 20,
        max_bytes: int = _MAX_JOB_EVENT_AUDIT_BYTES,
        now: float | None = None,
    ) -> str:
        """Serialize a bounded, redacted job-event audit for operators."""

        if not 4_000 <= max_bytes <= _MAX_JOB_EVENT_AUDIT_BYTES:
            raise ValueError("job event audit size must be between 4000 and 32000 bytes")
        timestamp = self._timestamp() if now is None else float(now)
        if not math.isfinite(timestamp) or timestamp < 0:
            raise DurableJobError("持久工作時間無效")
        audit = self.job_event_audit(
            owner_user_id,
            chat_id,
            job_id,
            limit=limit,
        )

        def build(items: tuple[JobEventAuditEntry, ...], truncated: bool) -> str:
            payload: dict[str, object] = {
                "schema_version": 1,
                "kind": "jobs_events",
                "scope": "owner-chat",
                "owner_user_id": _owner_id(owner_user_id),
                "chat_id": _chat_id(chat_id),
                "job_id": None if job_id is None else _entry_id(job_id),
                "generated_at": timestamp,
                "total": audit.total,
                "omitted": max(0, audit.total - len(items)),
                "truncated": truncated,
                "integrity": {
                    "status": audit.integrity.status,
                    "checked_events": audit.integrity.checked_events,
                    "first_bad_sequence": audit.integrity.first_bad_sequence,
                },
                "events": [
                    {
                        "sequence": event.sequence,
                        "job_id": event.job_id,
                        "action": event.action,
                        "status": event.status,
                        "actor": event.actor,
                        "created_at": event.created_at,
                    }
                    for event in items
                ],
            }
            # Bind the redacted document itself to a deterministic digest.  The
            # database hash chain proves the source state; this envelope digest
            # lets an offline consumer detect edits to otherwise-valid fields.
            payload["fingerprint"] = _snapshot_fingerprint(payload)
            return json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )

        selected = audit.events
        truncated = audit.truncated
        rendered = build(selected, truncated)
        while len(rendered.encode("utf-8")) > max_bytes and selected:
            selected = selected[1:]
            truncated = True
            rendered = build(selected, truncated)
        if len(rendered.encode("utf-8")) > max_bytes:
            raise DurableJobError("工作事件 audit 無法在大小上限內序列化")
        return rendered

    def verify_job_event_integrity(self) -> JobEventIntegrity:
        """Verify the append-only job-event hash chain without writing state."""

        if not self.path.is_file():
            return JobEventIntegrity(status="empty")
        try:
            with closing(self._connect_read_only()) as connection:
                table_exists = connection.execute(
                    """
                    SELECT 1 FROM sqlite_master
                    WHERE type = 'table' AND name = 'job_events'
                    """
                ).fetchone() is not None
                if not table_exists:
                    return JobEventIntegrity(status="empty")
                columns = {
                    str(row[1]) for row in connection.execute("PRAGMA table_info(job_events)")
                }
                if not {"prev_hash", "event_hash"}.issubset(columns):
                    # A pre-hash database must remain readable in a strict
                    # read-only path.  Do not issue a SELECT against columns
                    # that do not exist; callers get an explicit legacy
                    # status and can decide whether to require migration.
                    rows = connection.execute(
                        """
                        SELECT sequence, job_id, action, status, actor, created_at
                        FROM job_events ORDER BY sequence
                        """
                    ).fetchall()
                else:
                    rows = connection.execute(
                        """
                        SELECT sequence, job_id, action, status, actor, created_at,
                               prev_hash, event_hash
                        FROM job_events ORDER BY sequence
                        """
                    ).fetchall()
        except sqlite3.Error as error:
            raise DurableJobError("工作 audit 狀態不可讀") from error
        if not rows:
            return JobEventIntegrity(status="empty")
        if not {"prev_hash", "event_hash"}.issubset(columns):
            return JobEventIntegrity(
                status="legacy_unverified",
                checked_events=len(rows),
                first_bad_sequence=int(rows[0][0]),
            )
        legacy = False
        tampered_sequence: int | None = None
        previous_hash = ""
        for raw in rows:
            sequence = int(raw[0])
            prev_hash = str(raw[6] or "")
            event_hash = str(raw[7] or "")
            if (
                not prev_hash and sequence != int(rows[0][0])
            ) or not event_hash or _HASH.fullmatch(event_hash) is None:
                legacy = True
                previous_hash = event_hash if _HASH.fullmatch(event_hash) else ""
                continue
            if not legacy:
                if prev_hash != previous_hash:
                    tampered_sequence = sequence
                    break
                expected = _job_event_hash(
                    sequence=sequence,
                    job_id=str(raw[1]),
                    action=str(raw[2]),
                    status=cast(JobStatus, str(raw[3])),
                    actor=str(raw[4]),
                    created_at=float(raw[5]),
                    previous_hash=prev_hash,
                )
                if event_hash != expected:
                    tampered_sequence = sequence
                    break
            previous_hash = event_hash
        if tampered_sequence is not None:
            return JobEventIntegrity(
                status="tampered",
                checked_events=len(rows),
                first_bad_sequence=tampered_sequence,
            )
        if legacy:
            first_legacy = next(
                int(raw[0])
                for raw in rows
                if not str(raw[6] or "") or not str(raw[7] or "")
            )
            return JobEventIntegrity(
                status="legacy_unverified",
                checked_events=len(rows),
                first_bad_sequence=first_legacy,
            )
        return JobEventIntegrity(status="verified", checked_events=len(rows))

    def record_verification(
        self,
        owner_user_id: int,
        chat_id: int,
        job_id: str,
        *,
        expected_fingerprint: str,
        actual_fingerprint: str,
        outcome: VerificationOutcome,
    ) -> JobEvent:
        """Record one explicit bundle-verification outcome in job history.

        This uses the existing append-only ``job_events`` audit stream rather
        than introducing another ledger.  The compact action key contains only
        canonical fingerprints, is owner-scoped, and is idempotent for the
        same outcome.  It never changes job state or runner input.
        """

        owner = _owner_id(owner_user_id)
        chat = _chat_id(chat_id)
        item_id = _entry_id(job_id)
        _validate_fingerprint(expected_fingerprint, "expected")
        _validate_fingerprint(actual_fingerprint, "actual")
        if outcome not in {"accepted", "rejected"}:
            raise ValueError("verification outcome is invalid")
        action = (
            f"bundle_verify:{outcome}:{expected_fingerprint}:{actual_fingerprint}"
        )
        actor = f"owner:{owner}"
        now = self._timestamp()
        with closing(self._connect(create=False)) as connection:
            with connection:
                row = self._job_row(connection, item_id)
                if (cast(int, row[1]), cast(int, row[2])) != (owner, chat):
                    raise DurableJobError(
                        f"工作不存在或不屬於目前 owner: {item_id}"
                    )
                existing = connection.execute(
                    """
                    SELECT sequence, job_id, action, status, actor, created_at,
                           prev_hash, event_hash
                    FROM job_events
                    WHERE job_id = ? AND action = ? AND actor = ?
                    ORDER BY sequence DESC LIMIT 1
                    """,
                    (item_id, action, actor),
                ).fetchone()
                if existing is not None:
                    return _job_event(cast(tuple[object, ...], existing))
                count = int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM job_events
                        WHERE job_id = ? AND action LIKE 'bundle_verify:%'
                        """,
                        (item_id,),
                    ).fetchone()[0]
                )
                if count >= _MAX_VERIFICATION_EVENTS_PER_JOB:
                    raise DurableJobError(
                        f"工作最多保留 {_MAX_VERIFICATION_EVENTS_PER_JOB} 個 bundle 驗證紀錄"
                    )
                status = cast(JobStatus, str(row[12]))
                self._record_event(
                    connection,
                    job_id=item_id,
                    action=action,
                    status=status,
                    actor=actor,
                    now=now,
                )
                event_row = connection.execute(
                    """
                    SELECT sequence, job_id, action, status, actor, created_at,
                           prev_hash, event_hash
                    FROM job_events
                    WHERE job_id = ? AND action = ? AND actor = ?
                    ORDER BY sequence DESC LIMIT 1
                    """,
                    (item_id, action, actor),
                ).fetchone()
                if event_row is None:
                    raise DurableJobError("bundle 驗證 audit 無法保存")
        return _job_event(cast(tuple[object, ...], event_row))

    def request_interaction(
        self,
        owner_user_id: int,
        chat_id: int,
        job_id: str,
        *,
        kind: JobInteractionKind,
        text: str,
    ) -> JobInteractionRequest:
        """Persist a clarify/steer request without mutating the running job.

        rc.2 has no official in-place steering method.  This ledger therefore
        records user intent and its provenance only; a future adapter may
        consume an explicitly acknowledged entry through a proven runner seam.
        """

        owner = _owner_id(owner_user_id)
        chat = _chat_id(chat_id)
        item_id = _entry_id(job_id)
        if kind not in {"clarify", "steer"}:
            raise DurableJobError("工作互動類型無效")
        stored_text = _interaction_text(text)
        now = self._timestamp()
        with closing(self._connect(create=False)) as connection:
            with connection:
                job_row = self._job_row(connection, item_id)
                if (cast(int, job_row[1]), cast(int, job_row[2])) != (owner, chat):
                    raise DurableJobError(f"工作不存在或不屬於目前 owner: {item_id}")
                job_status = cast(JobStatus, str(job_row[12]))
                if job_status not in {"queued", "blocked", "running", "unknown"}:
                    raise DurableJobError(
                        f"工作 {item_id} 目前狀態為 {job_status}, 不能新增互動請求"
                    )
                pending_for_job = int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM job_interactions
                        WHERE job_id = ? AND status = 'pending'
                        """,
                        (item_id,),
                    ).fetchone()[0]
                )
                if pending_for_job >= _MAX_INTERACTIONS_PER_JOB:
                    raise DurableJobError(
                        f"工作 {item_id} 最多保留 {_MAX_INTERACTIONS_PER_JOB} 個待處理請求"
                    )
                pending_for_owner = int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM job_interactions
                        WHERE owner_user_id = ? AND status = 'pending'
                        """,
                        (owner,),
                    ).fetchone()[0]
                )
                if pending_for_owner >= _MAX_INTERACTIONS_PER_OWNER:
                    raise DurableJobError(
                        f"每個 owner 最多保留 {_MAX_INTERACTIONS_PER_OWNER} 個待處理請求"
                    )
                duplicate = connection.execute(
                    """
                    SELECT 1 FROM job_interactions
                    WHERE job_id = ? AND kind = ? AND text = ? AND status = 'pending'
                    LIMIT 1
                    """,
                    (item_id, kind, stored_text),
                ).fetchone()
                if duplicate is not None:
                    raise DurableJobError("相同互動請求已在等待處理")
                request_id = self._new_id(connection)
                connection.execute(
                    """
                    INSERT INTO job_interactions (
                        id, job_id, owner_user_id, chat_id, kind, text, job_status,
                        status, created_at, updated_at, decided_at, decision_actor
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, NULL, NULL)
                    """,
                    (
                        request_id,
                        item_id,
                        owner,
                        chat,
                        kind,
                        stored_text,
                        job_status,
                        now,
                        now,
                    ),
                )
                self._record_interaction_event(
                    connection,
                    request_id=request_id,
                    action=f"{kind}_requested",
                    status="pending",
                    actor=f"owner:{owner}",
                    now=now,
                )
                row = self._interaction_row(connection, request_id)
        self._secure()
        return _interaction(row)

    def interactions(
        self,
        owner_user_id: int,
        chat_id: int,
        job_id: str | None = None,
        *,
        status: JobInteractionStatus | None = None,
        limit: int = 20,
    ) -> tuple[JobInteractionRequest, ...]:
        """List bounded owner-scoped clarify/steer requests."""

        owner = _owner_id(owner_user_id)
        chat = _chat_id(chat_id)
        if job_id is not None:
            job_filter = _entry_id(job_id)
        else:
            job_filter = None
        if status is not None and status not in {
            "pending",
            "acknowledged",
            "rejected",
            "expired",
        }:
            raise DurableJobError("互動請求狀態篩選無效")
        if not 1 <= limit <= 100:
            raise ValueError("interaction limit must be between 1 and 100")
        if not self.path.is_file():
            return ()
        clauses = ["owner_user_id = ?", "chat_id = ?"]
        parameters: list[object] = [owner, chat]
        if job_filter is not None:
            clauses.append("job_id = ?")
            parameters.append(job_filter)
        if status is not None:
            clauses.append("status = ?")
            parameters.append(status)
        where = " AND ".join(clauses)
        try:
            with closing(self._connect_read_only()) as connection:
                table_exists = connection.execute(
                    """
                    SELECT 1 FROM sqlite_master
                    WHERE type = 'table' AND name = 'job_interactions'
                    """
                ).fetchone() is not None
                if not table_exists:
                    return ()
                rows = connection.execute(
                    f"""
                    SELECT id, job_id, owner_user_id, chat_id, kind, text, job_status,
                           status, created_at, updated_at, decided_at, decision_actor
                    FROM job_interactions
                    WHERE {where}
                    ORDER BY created_at DESC, id DESC LIMIT ?
                    """,
                    (*parameters, limit),
                ).fetchall()
        except sqlite3.Error as error:
            raise DurableJobError("持久互動請求資料庫不可讀") from error
        return tuple(_interaction(cast(tuple[object, ...], row)) for row in rows)

    def control_interaction(
        self,
        owner_user_id: int,
        chat_id: int,
        request_id: str,
        *,
        action: JobInteractionAction,
    ) -> JobInteractionRequest:
        """Acknowledge or reject a request; neither action applies runner input."""

        owner = _owner_id(owner_user_id)
        chat = _chat_id(chat_id)
        item_id = _entry_id(request_id)
        if action not in {"acknowledge", "reject"}:
            raise ValueError("interaction control action is invalid")
        now = self._timestamp()
        with closing(self._connect(create=False)) as connection:
            with connection:
                row = self._interaction_row(connection, item_id)
                if (cast(int, row[2]), cast(int, row[3])) != (owner, chat):
                    raise DurableJobError(
                        f"互動請求不存在或不屬於目前 owner: {item_id}"
                    )
                if str(row[7]) != "pending":
                    raise DurableJobError(
                        f"互動請求 {item_id} 目前狀態為 {row[7]}, 不能再次處理"
                    )
                target: JobInteractionStatus = (
                    "acknowledged" if action == "acknowledge" else "rejected"
                )
                connection.execute(
                    """
                    UPDATE job_interactions
                    SET status = ?, updated_at = ?, decided_at = ?, decision_actor = ?
                    WHERE id = ? AND status = 'pending'
                    """,
                    (target, now, now, f"owner:{owner}", item_id),
                )
                self._record_interaction_event(
                    connection,
                    request_id=item_id,
                    action=action,
                    status=target,
                    actor=f"owner:{owner}",
                    now=now,
                )
                result = self._interaction_row(connection, item_id)
        return _interaction(result)

    def interaction_health(
        self,
        owner_user_id: int,
        chat_id: int,
        *,
        now: float | None = None,
    ) -> JobInteractionHealthSummary:
        """Summarize one owner's pending requests without returning their text."""

        owner = _owner_id(owner_user_id)
        chat = _chat_id(chat_id)
        timestamp = self._timestamp() if now is None else float(now)
        if timestamp < 0:
            raise DurableJobError("持久工作時間無效")
        if not self.path.is_file():
            return JobInteractionHealthSummary()
        try:
            with closing(self._connect_read_only()) as connection:
                table_exists = connection.execute(
                    """
                    SELECT 1 FROM sqlite_master
                    WHERE type = 'table' AND name = 'job_interactions'
                    """
                ).fetchone() is not None
                if not table_exists:
                    return JobInteractionHealthSummary()
                row = connection.execute(
                    """
                    SELECT COUNT(*), MIN(created_at)
                    FROM job_interactions
                    WHERE owner_user_id = ? AND chat_id = ? AND status = 'pending'
                    """,
                    (owner, chat),
                ).fetchone()
        except sqlite3.Error as error:
            raise DurableJobError("持久互動請求資料庫不可讀") from error
        pending = int(row[0] or 0)
        oldest = row[1]
        age = max(0.0, timestamp - float(oldest)) if oldest is not None else None
        return JobInteractionHealthSummary(pending=pending, oldest_age_seconds=age)

    def interaction_history(
        self,
        owner_user_id: int,
        chat_id: int,
        request_id: str,
    ) -> tuple[JobInteractionEvent, ...]:
        """Return append-only audit events for one owner-scoped request."""

        owner = _owner_id(owner_user_id)
        chat = _chat_id(chat_id)
        item_id = _entry_id(request_id)
        if not self.path.is_file():
            return ()
        try:
            connection = self._connect_read_only()
        except DurableJobError:
            raise
        with closing(connection):
            table_exists = connection.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = 'job_interaction_events'
                """
            ).fetchone() is not None
            # Ownership is checked against the request table before returning
            # any event metadata.  A missing legacy event table is readable as
            # an empty history, never as a write-triggering migration.
            try:
                row = self._interaction_row(connection, item_id)
            except sqlite3.Error as error:
                raise DurableJobError("持久互動請求資料庫不可讀") from error
            if (cast(int, row[2]), cast(int, row[3])) != (owner, chat):
                raise DurableJobError(
                    f"互動請求不存在或不屬於目前 owner: {item_id}"
                )
            if not table_exists:
                return ()
            try:
                rows = connection.execute(
                    """
                    SELECT sequence, request_id, action, status, actor, created_at
                    FROM job_interaction_events
                    WHERE request_id = ? ORDER BY sequence
                    """,
                    (item_id,),
                ).fetchall()
            except sqlite3.Error as error:
                raise DurableJobError("持久互動請求資料庫不可讀") from error
        return tuple(
            JobInteractionEvent(
                sequence=int(raw[0]),
                request_id=str(raw[1]),
                action=str(raw[2]),
                status=cast(JobInteractionStatus, str(raw[3])),
                actor=str(raw[4]),
                created_at=float(raw[5]),
            )
            for raw in rows
        )

    def export_interactions(
        self,
        owner_user_id: int,
        chat_id: int,
        job_id: str | None = None,
        *,
        status: JobInteractionStatus | None = None,
        limit: int = 20,
        max_bytes: int = _MAX_INTERACTION_EXPORT_BYTES,
        now: float | None = None,
    ) -> str:
        """Serialize a bounded request audit without task/result bodies.

        The export includes only the explicitly requested interaction text and
        its append-only decisions.  Job prompts, answers, session IDs, models,
        lease tokens, and credentials are intentionally absent.
        """

        if not 4_000 <= max_bytes <= _MAX_INTERACTION_EXPORT_BYTES:
            raise ValueError("interaction export size must be between 4000 and 32000 bytes")
        timestamp = self._timestamp() if now is None else float(now)
        if timestamp < 0:
            raise DurableJobError("持久工作時間無效")
        entries = self.interactions(
            owner_user_id,
            chat_id,
            job_id,
            status=status,
            limit=limit,
        )
        integrity = self.verify_interaction_integrity()

        def build(items: tuple[JobInteractionRequest, ...], truncated: bool) -> str:
            payload: dict[str, object] = {
                "schema_version": 1,
                "scope": "owner-chat",
                "generated_at": timestamp,
                "truncated": truncated,
                "omitted": max(0, len(entries) - len(items)),
                "integrity": {
                    "status": integrity.status,
                    "checked_events": integrity.checked_events,
                    "first_bad_sequence": integrity.first_bad_sequence,
                },
                "requests": [
                    {
                        "id": entry.id,
                        "job_id": entry.job_id,
                        "kind": entry.kind,
                        "status": entry.status,
                        "job_status_at_request": entry.job_status,
                        "text": redact_sensitive_text(entry.text),
                        "created_at": entry.created_at,
                        "updated_at": entry.updated_at,
                        "decided_at": entry.decided_at,
                        "decision_actor": entry.decision_actor,
                        "age_seconds": max(0.0, timestamp - entry.created_at),
                        "events": [
                            {
                                "sequence": event.sequence,
                                "action": event.action,
                                "status": event.status,
                                "actor": event.actor,
                                "created_at": event.created_at,
                            }
                            for event in self.interaction_history(
                                owner_user_id,
                                chat_id,
                                entry.id,
                            )
                        ],
                    }
                    for entry in items
                ],
            }
            return json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )

        selected = entries
        truncated = False
        rendered = build(selected, truncated)
        while len(rendered.encode("utf-8")) > max_bytes and selected:
            selected = selected[:-1]
            truncated = True
            rendered = build(selected, truncated)
        if len(rendered.encode("utf-8")) > max_bytes:
            raise DurableJobError("互動請求 audit 無法在大小上限內序列化")
        return rendered

    def verify_interaction_integrity(self) -> JobInteractionIntegrity:
        """Verify the global append-only interaction hash chain read-only."""

        if not self.path.is_file():
            return JobInteractionIntegrity(status="empty")
        try:
            with closing(self._connect_read_only()) as connection:
                table_exists = connection.execute(
                    """
                    SELECT 1 FROM sqlite_master
                    WHERE type = 'table' AND name = 'job_interaction_events'
                    """
                ).fetchone() is not None
                if not table_exists:
                    return JobInteractionIntegrity(status="empty")
                columns = {
                    str(raw[1])
                    for raw in connection.execute(
                        "PRAGMA table_info(job_interaction_events)"
                    )
                }
                if not {"prev_hash", "event_hash"}.issubset(columns):
                    count = int(
                        connection.execute(
                            "SELECT COUNT(*) FROM job_interaction_events"
                        ).fetchone()[0]
                    )
                    return JobInteractionIntegrity(
                        status="legacy_unverified" if count else "empty",
                        checked_events=count,
                    )
                rows = connection.execute(
                    """
                    SELECT sequence, request_id, action, status, actor, created_at,
                           prev_hash, event_hash
                    FROM job_interaction_events ORDER BY sequence
                    """
                ).fetchall()
        except sqlite3.Error as error:
            raise DurableJobError("互動 audit chain 資料庫不可讀") from error
        if not rows:
            return JobInteractionIntegrity(status="empty")

        previous_hash: str | None = None
        legacy = False
        for index, raw in enumerate(rows):
            sequence = int(raw[0])
            previous = str(raw[6] or "")
            actual = str(raw[7] or "")
            if not actual:
                legacy = True
                previous_hash = None
                continue
            if not _HASH.fullmatch(actual):
                return JobInteractionIntegrity(
                    status="tampered",
                    checked_events=index + 1,
                    first_bad_sequence=sequence,
                )
            if previous:
                if not _HASH.fullmatch(previous):
                    return JobInteractionIntegrity(
                        status="tampered",
                        checked_events=index + 1,
                        first_bad_sequence=sequence,
                    )
                if index == 0:
                    return JobInteractionIntegrity(
                        status="tampered",
                        checked_events=index + 1,
                        first_bad_sequence=sequence,
                    )
                if previous_hash is not None and previous != previous_hash:
                    return JobInteractionIntegrity(
                        status="tampered",
                        checked_events=index + 1,
                        first_bad_sequence=sequence,
                    )
            elif previous_hash is not None:
                return JobInteractionIntegrity(
                    status="tampered",
                    checked_events=index + 1,
                    first_bad_sequence=sequence,
                )
            elif index > 0:
                # A hashed segment after legacy rows is valid but cannot prove
                # the historical prefix; keep the explicit legacy marker.
                legacy = True
            expected = _interaction_event_hash(
                request_id=str(raw[1]),
                action=str(raw[2]),
                status=cast(JobInteractionStatus, str(raw[3])),
                actor=str(raw[4]),
                created_at=float(raw[5]),
                previous_hash=previous,
            )
            if actual != expected:
                return JobInteractionIntegrity(
                    status="tampered",
                    checked_events=index + 1,
                    first_bad_sequence=sequence,
                )
            previous_hash = actual
        return JobInteractionIntegrity(
            status="legacy_unverified" if legacy else "verified",
            checked_events=len(rows),
        )

    def tick(
        self,
        worker_id: str,
        *,
        lease_seconds: float = 90.0,
        takeover: bool = False,
        allowed_owner_ids: frozenset[int] | None = None,
    ) -> BoardTick:
        """Advance dependency gates and atomically lease available jobs."""

        worker = _worker_id(worker_id)
        if not 30.0 <= lease_seconds <= 900.0:
            raise ValueError("job lease must be between 30 and 900 seconds")
        if not self.path.is_file():
            return BoardTick(())
        now = self._timestamp()
        owners = _allowed_owners(allowed_owner_ids)
        leases: list[JobLease] = []
        unknown_jobs: list[DurableJob] = []
        with closing(self._connect(create=False)) as connection:
            with connection:
                self._materialize_due_schedules(connection, now, owners)
                ambiguous_rows = connection.execute(
                    """
                    SELECT id FROM jobs
                    WHERE status = 'running'
                      AND (
                          (lease_expires IS NOT NULL AND lease_expires <= ?)
                          OR (? = 1 AND worker_id <> ?)
                      )
                    ORDER BY created_at, id
                    """,
                    (now, int(takeover), worker),
                ).fetchall()
                for ambiguous in ambiguous_rows:
                    job_id = str(ambiguous[0])
                    connection.execute(
                        """
                        UPDATE jobs
                        SET status = 'unknown',
                            error_text = 'worker ownership or lease expired; side effects unknown',
                            updated_at = ?, worker_id = NULL, lease_token = NULL,
                            lease_expires = NULL, cancel_requested = 0
                        WHERE id = ? AND status = 'running'
                        """,
                        (now, job_id),
                    )
                    self._record_event(
                        connection,
                        job_id=job_id,
                        action="recovered_unknown",
                        status="unknown",
                        actor=f"worker:{worker}",
                        now=now,
                    )
                    unknown_jobs.append(
                        self._job(
                            connection=None,
                            row=self._job_row(connection, job_id),
                            dependencies=self._dependencies(connection, job_id),
                        )
                    )
                self._advance_dependencies(connection, now)
                running = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM jobs WHERE status = 'running'"
                    ).fetchone()[0]
                )
                available = max(0, self.max_parallel_jobs - running)
                owner_clause, owner_parameters = _owner_filter(
                    owners,
                    column="owner_user_id",
                )
                rows = connection.execute(
                    f"""
                    SELECT id, chat_id, attempts FROM jobs
                    WHERE status = 'queued' AND scheduled_for <= ?
                      {owner_clause}
                    ORDER BY scheduled_for, created_at, id LIMIT ?
                    """,
                    (now, *owner_parameters, available),
                ).fetchall()
                for candidate in rows:
                    job_id = str(candidate[0])
                    job_chat_id = cast(int, candidate[1])
                    next_attempt = cast(int, candidate[2]) + 1
                    runner_session_id = _runner_session_id(
                        job_chat_id,
                        job_id,
                        attempt=next_attempt,
                    )
                    token = self._new_id(connection)
                    expires_at = now + lease_seconds
                    cursor = connection.execute(
                        """
                        UPDATE jobs
                        SET status = 'running', worker_id = ?, lease_token = ?,
                            lease_expires = ?, attempts = attempts + 1, updated_at = ?,
                            runner_session_id = ?
                        WHERE id = ? AND status = 'queued'
                        """,
                        (
                            worker,
                            token,
                            expires_at,
                            now,
                            runner_session_id,
                            job_id,
                        ),
                    )
                    if cursor.rowcount != 1:
                        continue
                    self._record_event(
                        connection,
                        job_id=job_id,
                        action="leased",
                        status="running",
                        actor=f"worker:{worker}",
                        now=now,
                    )
                    row = self._job_row(connection, job_id)
                    job = self._job(
                        connection=None,
                        row=row,
                        dependencies=self._dependencies(connection, job_id),
                    )
                    leases.append(JobLease(job, worker, token, expires_at))
        return BoardTick(tuple(leases), tuple(unknown_jobs))

    def control(
        self,
        owner_user_id: int,
        chat_id: int,
        job_id: str,
        *,
        action: JobControlAction,
    ) -> DurableJob:
        """Apply one explicit owner decision to a non-terminal or ambiguous job."""

        owner = _owner_id(owner_user_id)
        chat = _chat_id(chat_id)
        item_id = _entry_id(job_id)
        now = self._timestamp()
        with closing(self._connect(create=False)) as connection:
            with connection:
                row = self._job_row(connection, item_id)
                if (cast(int, row[1]), cast(int, row[2])) != (owner, chat):
                    raise DurableJobError(
                        f"工作不存在或不屬於目前 owner: {item_id}"
                    )
                status = str(row[12])
                if action == "retry":
                    if status not in {"unknown", "failed", "manual_review"}:
                        raise DurableJobError(f"工作 {item_id} 目前不能重跑")
                    connection.execute(
                        """
                        UPDATE jobs
                        SET status = 'queued', partial_text = '', result_text = '',
                            error_text = '',
                            updated_at = ?, cancel_requested = 0, result_enqueued = 0
                        WHERE id = ?
                        """,
                        (now, item_id),
                    )
                    event_action = "retry_requested"
                    event_status: JobStatus = "queued"
                elif action == "cancel":
                    if status in {"queued", "blocked", "manual_review"}:
                        connection.execute(
                            """
                            UPDATE jobs SET status = 'cancelled', updated_at = ?
                            WHERE id = ?
                            """,
                            (now, item_id),
                        )
                        event_action = "cancelled"
                        event_status = "cancelled"
                    elif status == "running":
                        connection.execute(
                            """
                            UPDATE jobs SET cancel_requested = 1, updated_at = ?
                            WHERE id = ?
                            """,
                            (now, item_id),
                        )
                        event_action = "cancel_requested"
                        event_status = "running"
                    else:
                        raise DurableJobError(f"工作 {item_id} 目前不能取消")
                elif action in {"resolve_completed", "resolve_failed"}:
                    if status not in {"unknown", "manual_review"}:
                        raise DurableJobError(f"工作 {item_id} 目前不能人工結案")
                    resolved = "completed" if action == "resolve_completed" else "failed"
                    connection.execute(
                        """
                        UPDATE jobs SET status = ?, updated_at = ?, cancel_requested = 0
                        WHERE id = ?
                        """,
                        (resolved, now, item_id),
                    )
                    event_action = f"resolved_{resolved}"
                    event_status = cast(JobStatus, resolved)
                else:
                    raise ValueError("job control action is invalid")
                self._record_event(
                    connection,
                    job_id=item_id,
                    action=event_action,
                    status=event_status,
                    actor=f"owner:{owner}",
                    now=now,
                )
                if event_status in {"completed", "failed", "cancelled"}:
                    self._expire_interactions(
                        connection,
                        job_id=item_id,
                        now=now,
                        actor=f"owner:{owner}",
                    )
                result_row = self._job_row(connection, item_id)
                dependencies = self._dependencies(connection, item_id)
        return self._job(
            connection=None,
            row=result_row,
            dependencies=dependencies,
        )

    def heartbeat(
        self,
        lease: JobLease,
        *,
        partial_text: str,
        lease_seconds: float = 90.0,
    ) -> DurableJob:
        if not 30.0 <= lease_seconds <= 900.0:
            raise ValueError("job lease must be between 30 and 900 seconds")
        now = self._timestamp()
        partial = _bounded_text(partial_text)
        with closing(self._connect(create=False)) as connection:
            with connection:
                self._lease_transition(
                    connection,
                    lease,
                    """
                    UPDATE jobs SET partial_text = ?, lease_expires = ?, updated_at = ?
                    WHERE id = ? AND status = 'running' AND worker_id = ?
                      AND lease_token = ?
                    """,
                    (partial, now + lease_seconds, now),
                )
                row = self._job_row(connection, lease.job.id)
                dependencies = self._dependencies(connection, lease.job.id)
        return self._job(connection=None, row=row, dependencies=dependencies)

    def finish(
        self,
        lease: JobLease,
        *,
        outcome: JobOutcome,
        result_text: str = "",
        error_text: str = "",
    ) -> DurableJob:
        if outcome not in {"completed", "failed", "cancelled"}:
            raise ValueError("job outcome is invalid")
        now = self._timestamp()
        result = _bounded_text(result_text)
        error = _bounded_text(error_text)
        with closing(self._connect(create=False)) as connection:
            with connection:
                self._lease_transition(
                    connection,
                    lease,
                    """
                    UPDATE jobs
                    SET status = ?, result_text = ?, error_text = ?, updated_at = ?,
                        worker_id = NULL, lease_token = NULL, lease_expires = NULL,
                        cancel_requested = 0
                    WHERE id = ? AND status = 'running' AND worker_id = ?
                      AND lease_token = ?
                    """,
                    (outcome, result, error, now),
                )
                self._record_event(
                    connection,
                    job_id=lease.job.id,
                    action=outcome,
                    status=outcome,
                    actor=f"worker:{lease.worker_id}",
                    now=now,
                )
                self._expire_interactions(
                    connection,
                    job_id=lease.job.id,
                    now=now,
                    actor=f"worker:{lease.worker_id}",
                )
                row = self._job_row(connection, lease.job.id)
                dependencies = self._dependencies(connection, lease.job.id)
        return self._job(connection=None, row=row, dependencies=dependencies)

    def _advance_dependencies(self, connection: sqlite3.Connection, now: float) -> None:
        rows = connection.execute(
            "SELECT id FROM jobs WHERE status = 'blocked' ORDER BY created_at, id"
        ).fetchall()
        for row in rows:
            job_id = str(row[0])
            statuses = [
                str(item[0])
                for item in connection.execute(
                    """
                    SELECT dependency.status
                    FROM job_dependencies AS edge
                    JOIN jobs AS dependency ON dependency.id = edge.dependency_id
                    WHERE edge.job_id = ?
                    """,
                    (job_id,),
                ).fetchall()
            ]
            if statuses and all(status == "completed" for status in statuses):
                connection.execute(
                    "UPDATE jobs SET status = 'queued', updated_at = ? WHERE id = ?",
                    (now, job_id),
                )
                self._record_event(
                    connection,
                    job_id=job_id,
                    action="dependencies_completed",
                    status="queued",
                    actor="system",
                    now=now,
                )
            elif any(
                status in {"failed", "cancelled", "unknown", "manual_review"}
                for status in statuses
            ):
                connection.execute(
                    """
                    UPDATE jobs SET status = 'manual_review',
                                    error_text = 'dependency requires manual review',
                                    updated_at = ? WHERE id = ?
                    """,
                    (now, job_id),
                )
                self._record_event(
                    connection,
                    job_id=job_id,
                    action="dependency_manual_review",
                    status="manual_review",
                    actor="system",
                    now=now,
                )

    def _materialize_due_schedules(
        self,
        connection: sqlite3.Connection,
        now: float,
        allowed_owner_ids: frozenset[int] | None,
    ) -> None:
        owner_clause, owner_parameters = _owner_filter(
            allowed_owner_ids,
            column="owner_user_id",
        )
        rows = connection.execute(
            f"""
            SELECT id, owner_user_id, chat_id, session_key, source_session_id,
                   task, model, fallback_model, reasoning_effort, show_thinking,
                   kind, expression, timezone, enabled, next_run_at,
                   last_scheduled_for, created_at, updated_at
            FROM schedules
            WHERE enabled = 1 AND next_run_at IS NOT NULL AND next_run_at <= ?
              {owner_clause}
            ORDER BY next_run_at, created_at, id
            """,
            (now, *owner_parameters),
        ).fetchall()
        for raw_row in rows:
            schedule = _schedule(cast(tuple[object, ...], raw_row))
            if schedule.next_run_at is None:
                continue
            if (
                self._pending_count(
                    connection,
                    owner_user_id=schedule.owner_user_id,
                )
                >= self.max_pending_jobs
            ):
                connection.execute(
                    """
                    UPDATE schedules SET next_run_at = ?, updated_at = ?
                    WHERE id = ? AND enabled = 1
                    """,
                    (now + 60.0, now, schedule.id),
                )
                self._record_schedule_event(
                    connection,
                    schedule_id=schedule.id,
                    action="capacity_delayed",
                    actor="system",
                    now=now,
                )
                continue
            job_id = self._new_id(connection)
            runner_session_id = _runner_session_id(schedule.chat_id, job_id)
            connection.execute(
                """
                INSERT INTO jobs (
                    id, owner_user_id, chat_id, session_key, source_session_id,
                    runner_session_id, task, model, fallback_model,
                    reasoning_effort, show_thinking, kind, status, partial_text,
                    result_text, error_text, created_at, scheduled_for, updated_at,
                    attempts, cancel_requested, worker_id, lease_token, lease_expires,
                    schedule_id, result_enqueued
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'scheduled', 'queued',
                          '', '', '', ?, ?, ?, 0, 0, NULL, NULL, NULL, ?, 0)
                """,
                (
                    job_id,
                    schedule.owner_user_id,
                    schedule.chat_id,
                    schedule.session_key,
                    schedule.source_session_id,
                    runner_session_id,
                    schedule.task,
                    schedule.model,
                    schedule.fallback_model,
                    schedule.reasoning_effort,
                    int(schedule.show_thinking),
                    now,
                    schedule.next_run_at,
                    now,
                    schedule.id,
                ),
            )
            self._record_event(
                connection,
                job_id=job_id,
                action="scheduled",
                status="queued",
                actor=f"schedule:{schedule.id}",
                now=now,
            )
            self._record_schedule_event(
                connection,
                schedule_id=schedule.id,
                action="materialized",
                actor=f"job:{job_id}",
                now=now,
            )
            next_run_at: float | None
            enabled = 1
            if schedule.kind == "at":
                next_run_at = None
                enabled = 0
            elif schedule.kind == "interval":
                interval = _interval_seconds(schedule.expression)
                skipped = int((now - schedule.next_run_at) // interval) + 1
                next_run_at = schedule.next_run_at + skipped * interval
            else:
                try:
                    zone = ZoneInfo(schedule.timezone)
                except (ZoneInfoNotFoundError, ValueError) as error:
                    raise DurableJobError(
                        f"排程 timezone 已不可用: {schedule.id}"
                    ) from error
                next_run_at = _next_cron_timestamp(schedule.expression, zone, now)
            connection.execute(
                """
                UPDATE schedules
                SET enabled = ?, last_scheduled_for = next_run_at,
                    next_run_at = ?, updated_at = ?
                WHERE id = ? AND enabled = 1
                """,
                (enabled, next_run_at, now, schedule.id),
            )

    def _lease_transition(
        self,
        connection: sqlite3.Connection,
        lease: JobLease,
        statement: str,
        values: tuple[object, ...],
    ) -> None:
        cursor = connection.execute(
            statement,
            (*values, lease.job.id, lease.worker_id, lease.token),
        )
        if cursor.rowcount != 1:
            raise DurableJobError(f"工作 lease 已失效或狀態不允許: {lease.job.id}")

    def _expire_interactions(
        self,
        connection: sqlite3.Connection,
        *,
        job_id: str,
        now: float,
        actor: str,
    ) -> None:
        """Close requests that can no longer target a terminal job."""

        rows = connection.execute(
            """
            SELECT id FROM job_interactions
            WHERE job_id = ? AND status = 'pending'
            ORDER BY created_at, id
            """,
            (job_id,),
        ).fetchall()
        if not rows:
            return
        connection.execute(
            """
            UPDATE job_interactions
            SET status = 'expired', updated_at = ?, decided_at = ?, decision_actor = ?
            WHERE job_id = ? AND status = 'pending'
            """,
            (now, now, actor, job_id),
        )
        for row in rows:
            self._record_interaction_event(
                connection,
                request_id=str(row[0]),
                action="expired",
                status="expired",
                actor=actor,
                now=now,
            )

    def _pending_count(
        self,
        connection: sqlite3.Connection,
        *,
        owner_user_id: int,
    ) -> int:
        return int(
            connection.execute(
                """
                SELECT COUNT(*) FROM jobs
                WHERE owner_user_id = ?
                  AND status IN ('blocked', 'queued', 'running', 'unknown',
                                 'manual_review')
                """,
                (owner_user_id,),
            ).fetchone()[0]
        )

    def _new_id(self, connection: sqlite3.Connection) -> str:
        for _ in range(40):
            try:
                candidate = self._id_factory()
            except (StopIteration, RuntimeError) as error:
                raise DurableJobError("無法產生持久工作 ID") from error
            if _ID.fullmatch(candidate) is None:
                continue
            exists = connection.execute(
                """
                SELECT 1 FROM jobs WHERE id = ? OR lease_token = ?
                UNION ALL SELECT 1 FROM schedules WHERE id = ?
                UNION ALL SELECT 1 FROM job_interactions WHERE id = ?
                """,
                (candidate, candidate, candidate, candidate),
            ).fetchone()
            if exists is None:
                return candidate
        raise DurableJobError("無法產生唯一持久工作 ID")

    def _timestamp(self) -> float:
        value = self._clock()
        if not isinstance(value, int | float) or isinstance(value, bool) or value < 0:
            raise DurableJobError("持久工作時間無效")
        return float(value)

    def _connect(self, *, create: bool) -> sqlite3.Connection:
        if self.path.is_symlink() or (self.path.exists() and not self.path.is_file()):
            raise DurableJobError("持久工作資料庫路徑不安全")
        if create:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self.root.is_symlink() or not self.root.is_dir():
                raise DurableJobError("持久工作狀態根目錄不安全")
            self.root.chmod(0o700)
        try:
            connection = sqlite3.connect(self.path, timeout=5.0)
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    owner_user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    session_key TEXT NOT NULL,
                    source_session_id TEXT NOT NULL,
                    runner_session_id TEXT NOT NULL UNIQUE,
                    task TEXT NOT NULL,
                    model TEXT NOT NULL,
                    fallback_model TEXT,
                    reasoning_effort TEXT NOT NULL,
                    show_thinking INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    partial_text TEXT NOT NULL,
                    result_text TEXT NOT NULL,
                    error_text TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    scheduled_for REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    attempts INTEGER NOT NULL,
                    cancel_requested INTEGER NOT NULL,
                    worker_id TEXT,
                    lease_token TEXT UNIQUE,
                    lease_expires REAL,
                    schedule_id TEXT,
                    result_enqueued INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(schedule_id, scheduled_for)
                );
                CREATE INDEX IF NOT EXISTS jobs_status_due
                    ON jobs(status, scheduled_for, created_at);
                CREATE INDEX IF NOT EXISTS jobs_owner_chat
                    ON jobs(owner_user_id, chat_id, created_at);
                CREATE TABLE IF NOT EXISTS job_dependencies (
                    job_id TEXT NOT NULL REFERENCES jobs(id),
                    dependency_id TEXT NOT NULL REFERENCES jobs(id),
                    PRIMARY KEY (job_id, dependency_id)
                );
                CREATE TABLE IF NOT EXISTS job_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES jobs(id),
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    prev_hash TEXT NOT NULL DEFAULT '',
                    event_hash TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS job_events_job
                    ON job_events(job_id, sequence);
                CREATE TABLE IF NOT EXISTS job_interactions (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES jobs(id),
                    owner_user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    text TEXT NOT NULL,
                    job_status TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    decided_at REAL,
                    decision_actor TEXT
                );
                CREATE INDEX IF NOT EXISTS job_interactions_job
                    ON job_interactions(job_id, created_at, id);
                CREATE INDEX IF NOT EXISTS job_interactions_owner
                    ON job_interactions(owner_user_id, chat_id, status, created_at);
                CREATE TABLE IF NOT EXISTS job_interaction_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL REFERENCES job_interactions(id),
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    prev_hash TEXT NOT NULL DEFAULT '',
                    event_hash TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS job_interaction_events_request
                    ON job_interaction_events(request_id, sequence);
                CREATE TABLE IF NOT EXISTS schedules (
                    id TEXT PRIMARY KEY,
                    owner_user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    session_key TEXT NOT NULL,
                    source_session_id TEXT NOT NULL,
                    task TEXT NOT NULL,
                    model TEXT NOT NULL,
                    fallback_model TEXT,
                    reasoning_effort TEXT NOT NULL,
                    show_thinking INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    expression TEXT NOT NULL,
                    timezone TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    next_run_at REAL,
                    last_scheduled_for REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS schedules_due
                    ON schedules(enabled, next_run_at);
                CREATE TABLE IF NOT EXISTS schedule_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    schedule_id TEXT NOT NULL REFERENCES schedules(id),
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS schedule_events_schedule
                    ON schedule_events(schedule_id, sequence);
                """
            )
            job_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(jobs)")
            }
            if "schedule_id" not in job_columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN schedule_id TEXT")
            if "result_enqueued" not in job_columns:
                connection.execute(
                    """
                    ALTER TABLE jobs ADD COLUMN result_enqueued INTEGER NOT NULL DEFAULT 0
                    """
                )
            interaction_event_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(job_interaction_events)"
                )
            }
            if "prev_hash" not in interaction_event_columns:
                connection.execute(
                    """
                    ALTER TABLE job_interaction_events
                    ADD COLUMN prev_hash TEXT NOT NULL DEFAULT ''
                    """
                )
            if "event_hash" not in interaction_event_columns:
                connection.execute(
                    """
                    ALTER TABLE job_interaction_events
                    ADD COLUMN event_hash TEXT NOT NULL DEFAULT ''
                    """
                )
            job_event_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(job_events)")
            }
            if "prev_hash" not in job_event_columns:
                connection.execute(
                    """
                    ALTER TABLE job_events
                    ADD COLUMN prev_hash TEXT NOT NULL DEFAULT ''
                    """
                )
            if "event_hash" not in job_event_columns:
                connection.execute(
                    """
                    ALTER TABLE job_events
                    ADD COLUMN event_hash TEXT NOT NULL DEFAULT ''
                    """
                )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS jobs_schedule_occurrence
                ON jobs(schedule_id, scheduled_for)
                WHERE schedule_id IS NOT NULL
                """
            )
        except sqlite3.Error as error:
            raise DurableJobError("SQLite 持久工作狀態不可用") from error
        if create:
            self._secure()
        return connection

    def _connect_read_only(self) -> sqlite3.Connection:
        """Open an existing board without creating tables or changing state."""

        if self.path.is_symlink() or not self.path.is_file():
            raise DurableJobError("持久工作資料庫路徑不安全")
        try:
            connection = sqlite3.connect(
                f"{self.path.as_uri()}?mode=ro",
                uri=True,
                timeout=5.0,
            )
            connection.execute("PRAGMA query_only = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            return connection
        except sqlite3.Error as error:
            raise DurableJobError("SQLite 持久工作狀態不可讀") from error

    def _job_row(self, connection: sqlite3.Connection, job_id: str) -> tuple[object, ...]:
        row = connection.execute(
            """
            SELECT id, owner_user_id, chat_id, session_key, source_session_id,
                   runner_session_id, task, model, fallback_model,
                   reasoning_effort, show_thinking, kind, status, partial_text,
                   result_text, error_text, created_at, scheduled_for, updated_at,
                   attempts, cancel_requested, schedule_id, result_enqueued
            FROM jobs WHERE id = ?
            """,
            (job_id,),
        ).fetchone()
        if row is None:
            raise DurableJobError(f"工作不存在: {job_id}")
        return cast(tuple[object, ...], row)

    def _interaction_row(
        self,
        connection: sqlite3.Connection,
        request_id: str,
    ) -> tuple[object, ...]:
        row = connection.execute(
            """
            SELECT id, job_id, owner_user_id, chat_id, kind, text, job_status,
                   status, created_at, updated_at, decided_at, decision_actor
            FROM job_interactions WHERE id = ?
            """,
            (request_id,),
        ).fetchone()
        if row is None:
            raise DurableJobError(f"互動請求不存在: {request_id}")
        return cast(tuple[object, ...], row)

    def _schedule_row(
        self,
        connection: sqlite3.Connection,
        schedule_id: str,
    ) -> tuple[object, ...]:
        row = connection.execute(
            """
            SELECT id, owner_user_id, chat_id, session_key, source_session_id,
                   task, model, fallback_model, reasoning_effort, show_thinking,
                   kind, expression, timezone, enabled, next_run_at,
                   last_scheduled_for, created_at, updated_at
            FROM schedules WHERE id = ?
            """,
            (schedule_id,),
        ).fetchone()
        if row is None:
            raise DurableJobError(f"排程不存在: {schedule_id}")
        return cast(tuple[object, ...], row)

    def _record_event(
        self,
        connection: sqlite3.Connection,
        *,
        job_id: str,
        action: str,
        status: JobStatus,
        actor: str,
        now: float,
    ) -> None:
        previous = connection.execute(
            "SELECT event_hash FROM job_events ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        previous_hash = "" if previous is None else str(previous[0] or "")
        cursor = connection.execute(
            """
            INSERT INTO job_events (
                job_id, action, status, actor, created_at, prev_hash, event_hash
            ) VALUES (?, ?, ?, ?, ?, '', '')
            """,
            (job_id, action, status, actor, now),
        )
        sequence = int(cursor.lastrowid or 0)
        if sequence <= 0:
            raise DurableJobError("工作 audit sequence 無效")
        event_hash = _job_event_hash(
            sequence=sequence,
            job_id=job_id,
            action=action,
            status=status,
            actor=actor,
            created_at=now,
            previous_hash=previous_hash,
        )
        connection.execute(
            """
            UPDATE job_events SET prev_hash = ?, event_hash = ?
            WHERE sequence = ?
            """,
            (previous_hash, event_hash, sequence),
        )

    def _record_interaction_event(
        self,
        connection: sqlite3.Connection,
        *,
        request_id: str,
        action: str,
        status: JobInteractionStatus,
        actor: str,
        now: float,
    ) -> None:
        previous = connection.execute(
            """
            SELECT event_hash FROM job_interaction_events
            ORDER BY sequence DESC LIMIT 1
            """
        ).fetchone()
        previous_hash = "" if previous is None else str(previous[0] or "")
        event_hash = _interaction_event_hash(
            request_id=request_id,
            action=action,
            status=status,
            actor=actor,
            created_at=now,
            previous_hash=previous_hash,
        )
        connection.execute(
            """
            INSERT INTO job_interaction_events (
                request_id, action, status, actor, created_at, prev_hash, event_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (request_id, action, status, actor, now, previous_hash, event_hash),
        )

    def _record_schedule_event(
        self,
        connection: sqlite3.Connection,
        *,
        schedule_id: str,
        action: str,
        actor: str,
        now: float,
    ) -> None:
        connection.execute(
            """
            INSERT INTO schedule_events (
                schedule_id, action, actor, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (schedule_id, action, actor, now),
        )

    def _dependencies(
        self,
        connection: sqlite3.Connection,
        job_id: str,
    ) -> tuple[str, ...]:
        rows = connection.execute(
            """
            SELECT dependency_id FROM job_dependencies
            WHERE job_id = ? ORDER BY dependency_id
            """,
            (job_id,),
        ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def _job(
        self,
        *,
        connection: None,
        row: tuple[object, ...],
        dependencies: tuple[str, ...],
    ) -> DurableJob:
        del connection
        return DurableJob(
            id=str(row[0]),
            owner_user_id=cast(int, row[1]),
            chat_id=cast(int, row[2]),
            session_key=str(row[3]),
            source_session_id=str(row[4]),
            runner_session_id=str(row[5]),
            task=str(row[6]),
            model=str(row[7]),
            fallback_model=None if row[8] is None else str(row[8]),
            reasoning_effort=cast(ReasoningEffort, str(row[9])),
            show_thinking=bool(row[10]),
            kind=cast(JobKind, str(row[11])),
            status=cast(JobStatus, str(row[12])),
            dependencies=dependencies,
            partial_text=str(row[13]),
            result_text=str(row[14]),
            error_text=str(row[15]),
            created_at=cast(float, row[16]),
            scheduled_for=cast(float, row[17]),
            updated_at=cast(float, row[18]),
            attempts=cast(int, row[19]),
            cancel_requested=bool(row[20]),
            schedule_id=None if row[21] is None else str(row[21]),
            result_enqueued=bool(row[22]),
        )

    def _secure(self) -> None:
        self.root.chmod(0o700)
        os.chmod(self.path, 0o600)


def _interaction(row: tuple[object, ...]) -> JobInteractionRequest:
    return JobInteractionRequest(
        id=str(row[0]),
        job_id=str(row[1]),
        owner_user_id=cast(int, row[2]),
        chat_id=cast(int, row[3]),
        kind=cast(JobInteractionKind, str(row[4])),
        text=str(row[5]),
        job_status=cast(JobStatus, str(row[6])),
        status=cast(JobInteractionStatus, str(row[7])),
        created_at=cast(float, row[8]),
        updated_at=cast(float, row[9]),
        decided_at=None if row[10] is None else cast(float, row[10]),
        decision_actor=None if row[11] is None else str(row[11]),
    )


def _job_event(row: tuple[object, ...]) -> JobEvent:
    return JobEvent(
        sequence=cast(int, row[0]),
        job_id=str(row[1]),
        action=str(row[2]),
        status=cast(JobStatus, str(row[3])),
        actor=str(row[4]),
        created_at=cast(float, row[5]),
        prev_hash="" if len(row) < 7 else str(row[6] or ""),
        event_hash="" if len(row) < 8 else str(row[7] or ""),
    )


def _validate_fingerprint(value: str, label: str) -> None:
    if not isinstance(value, str) or len(value) != 71 or not value.startswith("sha256:"):
        raise DurableJobError(f"{label} fingerprint 格式無效")
    if _HASH.fullmatch(value[7:]) is None:
        raise DurableJobError(f"{label} fingerprint 格式無效")


def _safe_job_event_action(action: str) -> str:
    """Reduce an untrusted raw action to a stable, argument-free label."""

    normalized = action.strip().casefold()
    if normalized.startswith("bundle_verify:"):
        outcome = normalized.split(":", 2)[1] if ":" in normalized else "unknown"
        if outcome in {"accepted", "rejected"}:
            return f"bundle_verify:{outcome}"
        return "bundle_verify:unknown"
    if normalized in _SAFE_JOB_EVENT_ACTIONS:
        return normalized
    if normalized.startswith("resolved_") and normalized.removeprefix("resolved_") in {
        "completed",
        "failed",
    }:
        return normalized
    return "other"


def _safe_job_event_status(status: str) -> JobStatus:
    normalized = status.strip().casefold()
    if normalized in _SAFE_JOB_EVENT_STATUS:
        return cast(JobStatus, normalized)
    return "unknown"


def _safe_job_event_actor(actor: str) -> str:
    """Expose provenance class without worker IDs or arbitrary actor text."""

    category = actor.partition(":")[0].strip().casefold()
    if category in {"owner", "worker", "system", "schedule", "job"}:
        return category
    return "other"


def _snapshot_fingerprint(payload: dict[str, object]) -> str:
    """Hash a redacted JSON envelope without importing the snapshot layer.

    ``snapshots`` depends on the durable job dataclasses, so this tiny
    canonicalization seam intentionally lives here for the core exporter and
    mirrors its UTF-8 JSON rules.  The field is excluded before hashing by
    construction; callers add it only after this function returns.
    """

    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _interaction_event_hash(
    *,
    request_id: str,
    action: str,
    status: JobInteractionStatus,
    actor: str,
    created_at: float,
    previous_hash: str,
) -> str:
    canonical = json.dumps(
        {
            "action": action,
            "actor": actor,
            "created_at": format(created_at, ".17g"),
            "prev_hash": previous_hash,
            "request_id": request_id,
            "status": status,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _job_event_hash(
    *,
    sequence: int,
    job_id: str,
    action: str,
    status: JobStatus,
    actor: str,
    created_at: float,
    previous_hash: str,
) -> str:
    canonical = json.dumps(
        {
            "action": action,
            "actor": actor,
            "created_at": format(created_at, ".17g"),
            "job_id": job_id,
            "prev_hash": previous_hash,
            "sequence": sequence,
            "status": status,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _request(request: JobRequest) -> JobRequest:
    owner = _owner_id(request.owner_user_id)
    chat = _chat_id(request.chat_id)
    session_key = request.session_key.strip()
    if _SESSION_KEY.fullmatch(session_key) is None:
        raise DurableJobError("持久工作的會話 key 無效")
    source_session_id = request.source_session_id.strip()
    if _SESSION_ID.fullmatch(source_session_id) is None:
        raise DurableJobError("持久工作的來源 session ID 無效")
    task = _task(request.task)
    model = request.model.strip()
    if not model or len(model) > 200 or any(character.isspace() for character in model):
        raise DurableJobError("持久工作的模型 ID 無效")
    fallback = request.fallback_model
    if fallback is not None:
        fallback = fallback.strip()
        if not fallback or len(fallback) > 200 or any(
            character.isspace() for character in fallback
        ):
            raise DurableJobError("持久工作的備用模型 ID 無效")
    if request.reasoning_effort not in {
        "off",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    }:
        raise DurableJobError("持久工作的推理強度無效")
    if request.kind not in {"background", "scheduled"}:
        raise DurableJobError("持久工作類型無效")
    scheduled_for = request.scheduled_for
    if scheduled_for is not None and (
        not isinstance(scheduled_for, int | float)
        or isinstance(scheduled_for, bool)
        or scheduled_for < 0
    ):
        raise DurableJobError("持久工作的預定時間無效")
    return JobRequest(
        owner_user_id=owner,
        chat_id=chat,
        session_key=session_key,
        source_session_id=source_session_id,
        task=task,
        model=model,
        fallback_model=fallback,
        reasoning_effort=request.reasoning_effort,
        show_thinking=request.show_thinking,
        kind=request.kind,
        dependencies=request.dependencies,
        scheduled_for=None if scheduled_for is None else float(scheduled_for),
    )


def _owner_id(value: int) -> int:
    if value <= 0 or isinstance(value, bool):
        raise DurableJobError("工作 owner user ID 無效")
    return value


def _allowed_owners(
    values: frozenset[int] | None,
) -> frozenset[int] | None:
    if values is None:
        return None
    return frozenset(_owner_id(value) for value in values)


def _owner_filter(
    values: frozenset[int] | None,
    *,
    column: str,
) -> tuple[str, tuple[int, ...]]:
    if values is None:
        return "", ()
    if not values:
        return "AND 1 = 0", ()
    ordered = tuple(sorted(values))
    placeholders = ", ".join("?" for _ in ordered)
    return f"AND {column} IN ({placeholders})", ordered


def _chat_id(value: int) -> int:
    if value == 0 or isinstance(value, bool):
        raise DurableJobError("Telegram chat ID 無效")
    return value


def _entry_id(value: str) -> str:
    normalized = value.strip().casefold()
    if _ID.fullmatch(normalized) is None:
        raise DurableJobError("持久工作 ID 無效")
    return normalized


def _worker_id(value: str) -> str:
    normalized = value.strip()
    if _WORKER_ID.fullmatch(normalized) is None:
        raise DurableJobError("持久工作 worker ID 無效")
    return normalized


def _task(value: str) -> str:
    normalized = value.strip()
    if not 1 <= len(normalized) <= _MAX_TASK_CHARS:
        raise DurableJobError("持久工作內容必須是 1-12000 個字元")
    if any(
        (ord(character) < 32 and character not in "\n\t") or ord(character) == 127
        for character in normalized
    ):
        raise DurableJobError("持久工作內容含有不安全控制字元")
    if redact_sensitive_text(normalized) != normalized:
        raise DurableJobError("疑似包含憑據, 不寫入持久工作")
    return normalized


def _bounded_text(value: str) -> str:
    redacted = redact_sensitive_text(value).strip()
    if len(redacted) <= _MAX_RESULT_CHARS:
        return redacted
    return redacted[: _MAX_RESULT_CHARS - 1] + "…"


def _interaction_text(value: str) -> str:
    normalized = value.strip()
    if not 1 <= len(normalized) <= _MAX_INTERACTION_CHARS:
        raise DurableJobError("互動請求內容必須是 1-4000 個字元")
    if any(
        (ord(character) < 32 and character not in "\n\t") or ord(character) == 127
        for character in normalized
    ):
        raise DurableJobError("互動請求含有不安全控制字元")
    if redact_sensitive_text(normalized) != normalized:
        raise DurableJobError("疑似包含憑據, 不寫入互動請求")
    return normalized


def _runner_session_id(
    chat_id: int,
    job_id: str,
    *,
    attempt: int = 1,
) -> str:
    chat_token = f"n{abs(chat_id)}" if chat_id < 0 else str(chat_id)
    suffix = "" if attempt == 1 else f"-a{attempt}"
    return f"telegram-job-{chat_token}-{job_id}{suffix}"


def _at_timestamp(expression: str, timezone: ZoneInfo) -> float:
    try:
        value = datetime.fromisoformat(expression)
    except ValueError as error:
        raise DurableJobError("at 排程必須使用 YYYY-MM-DDTHH:MM") from error
    if value.tzinfo is not None or value.second or value.microsecond:
        raise DurableJobError("at 排程必須是指定 timezone 的分鐘時間")
    first = value.replace(tzinfo=timezone, fold=0)
    second = value.replace(tzinfo=timezone, fold=1)
    if first.utcoffset() != second.utcoffset():
        raise DurableJobError("at 排程時間落在 DST 歧義時段")
    timestamp = first.timestamp()
    if datetime.fromtimestamp(timestamp, timezone).replace(tzinfo=None) != value:
        raise DurableJobError("at 排程時間在指定 timezone 不存在")
    return timestamp


def _interval_seconds(expression: str) -> float:
    try:
        minutes = int(expression)
    except ValueError as error:
        raise DurableJobError("interval 排程必須是整數分鐘") from error
    if not 1 <= minutes <= 525_600:
        raise DurableJobError("interval 排程必須介於 1 分鐘與 1 年")
    return float(minutes * 60)


@dataclass(frozen=True, slots=True)
class _CronExpression:
    minutes: frozenset[int]
    hours: frozenset[int]
    month_days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    month_day_wildcard: bool
    weekday_wildcard: bool

    def matches(self, value: datetime) -> bool:
        if (
            value.minute not in self.minutes
            or value.hour not in self.hours
            or not self.matches_date(value)
        ):
            return False
        return True

    def matches_date(self, value: datetime) -> bool:
        if value.month not in self.months:
            return False
        month_day_matches = value.day in self.month_days
        cron_weekday = (value.weekday() + 1) % 7
        weekday_matches = cron_weekday in self.weekdays
        if self.month_day_wildcard and self.weekday_wildcard:
            day_matches = True
        elif self.month_day_wildcard:
            day_matches = weekday_matches
        elif self.weekday_wildcard:
            day_matches = month_day_matches
        else:
            day_matches = month_day_matches or weekday_matches
        return day_matches


def _next_cron_timestamp(expression: str, timezone: ZoneInfo, after: float) -> float:
    cron = _parse_cron(expression)
    start = datetime.fromtimestamp(after, timezone)
    for day_offset in range(5 * 366 + 1):
        day = (start + timedelta(days=day_offset)).date()
        date_value = datetime(day.year, day.month, day.day)
        if not cron.matches_date(date_value):
            continue
        candidates: list[float] = []
        for hour in sorted(cron.hours):
            for minute in sorted(cron.minutes):
                naive = datetime(day.year, day.month, day.day, hour, minute)
                for fold in (0, 1):
                    aware = naive.replace(tzinfo=timezone, fold=fold)
                    candidate = aware.timestamp()
                    if candidate <= after:
                        continue
                    round_trip = datetime.fromtimestamp(candidate, timezone)
                    if (
                        round_trip.replace(tzinfo=None) == naive
                        and cron.matches(round_trip)
                    ):
                        candidates.append(candidate)
        if candidates:
            return min(candidates)
    raise DurableJobError("cron 排程在五年內沒有可執行時間")


def _parse_cron(expression: str) -> _CronExpression:
    fields = expression.split()
    if len(fields) != 5:
        raise DurableJobError("cron 排程必須包含 5 個欄位")
    minute, minute_wildcard = _cron_field(fields[0], 0, 59)
    hour, hour_wildcard = _cron_field(fields[1], 0, 23)
    month_day, month_day_wildcard = _cron_field(fields[2], 1, 31)
    month, month_wildcard = _cron_field(fields[3], 1, 12)
    weekday, weekday_wildcard = _cron_field(fields[4], 0, 7, weekday=True)
    del minute_wildcard, hour_wildcard, month_wildcard
    return _CronExpression(
        minute,
        hour,
        month_day,
        month,
        weekday,
        month_day_wildcard,
        weekday_wildcard,
    )


def _cron_field(
    text: str,
    minimum: int,
    maximum: int,
    *,
    weekday: bool = False,
) -> tuple[frozenset[int], bool]:
    values: set[int] = set()
    wildcard = text.startswith("*")
    for item in text.split(","):
        base, separator, step_text = item.partition("/")
        if separator:
            try:
                step = int(step_text)
            except ValueError as error:
                raise DurableJobError("cron step 必須是正整數") from error
            if step <= 0:
                raise DurableJobError("cron step 必須是正整數")
        else:
            step = 1
        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            start_text, end_text = base.split("-", maxsplit=1)
            try:
                start, end = int(start_text), int(end_text)
            except ValueError as error:
                raise DurableJobError("cron range 無效") from error
        else:
            try:
                start = end = int(base)
            except ValueError as error:
                raise DurableJobError("cron 欄位必須是數字、range 或 wildcard") from error
        if start < minimum or end > maximum or start > end:
            raise DurableJobError("cron 欄位超出允許範圍")
        values.update(range(start, end + 1, step))
    if weekday and 7 in values:
        values.remove(7)
        values.add(0)
    if not values:
        raise DurableJobError("cron 欄位不能為空")
    return frozenset(values), wildcard


def _schedule(row: tuple[object, ...]) -> DurableSchedule:
    return DurableSchedule(
        id=str(row[0]),
        owner_user_id=cast(int, row[1]),
        chat_id=cast(int, row[2]),
        session_key=str(row[3]),
        source_session_id=str(row[4]),
        task=str(row[5]),
        model=str(row[6]),
        fallback_model=None if row[7] is None else str(row[7]),
        reasoning_effort=cast(ReasoningEffort, str(row[8])),
        show_thinking=bool(row[9]),
        kind=cast(ScheduleKind, str(row[10])),
        expression=str(row[11]),
        timezone=str(row[12]),
        enabled=bool(row[13]),
        next_run_at=None if row[14] is None else cast(float, row[14]),
        last_scheduled_for=None if row[15] is None else cast(float, row[15]),
        created_at=cast(float, row[16]),
        updated_at=cast(float, row[17]),
    )
