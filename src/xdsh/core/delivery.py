"""Durable task queue, execution checkpoints, and conservative reply outbox."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import time
import uuid
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from xdsh.core.product_control import redact_sensitive_text

DeliveryKind = Literal["text", "markdown"]
DeliveryStatus = Literal["pending", "sending", "delivered", "unknown", "dropped"]
QueueMode = Literal["normal", "priority", "followup", "resume"]
TurnOutcome = Literal["completed", "failed", "cancelled"]
HandoffKind = Literal["merge", "interrupt", "replace"]
ExecutionPhase = Literal[
    "initializing", "generating", "tool_arguments", "tool_running", "result", "stopping", "finished"
]
ExecutionState = Literal["running", "stopping", "retired"]

_ID = re.compile(r"^[a-f0-9]{8}$")
_SESSION_KEY = re.compile(r"^(?:main|[a-z0-9]{8,16})$")
_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}$")
_DEDUPE_KEY = re.compile(r"^[a-z0-9][a-z0-9:._-]{0,127}$")
_MAX_PROMPT_CHARS = 12_000
_MAX_PARTIAL_CHARS = 16_000
MAX_DELIVERY_CHARS = 16_000
# 200K plain answers may need >100 transport-safe parts. Keep a finite
# transaction bound that also covers 100K opt-in reasoning and notices.
MAX_DELIVERY_MESSAGES = 256
_MAX_QUEUE_ITEMS = 10
_MAX_QUEUE_CONTEXT_CHARS = 16_000
_QUEUE_MODES = frozenset({"normal", "priority", "followup", "resume"})
_RECOVERY_DEDUPE_PREFIX = "delivery-recovery:"
_EXECUTION_PHASES = frozenset({
    "initializing", "generating", "tool_arguments", "tool_running", "result", "stopping", "finished"
})


class DeliveryLedgerError(RuntimeError):
    """Durable delivery state or a requested transition is invalid."""


class ExecutionFencedError(DeliveryLedgerError):
    """A stale execution must not fall back to unfenced persistence or sending."""


class DeliveryAttemptFencedError(DeliveryLedgerError):
    """A receipt from an older submission cannot settle a newer attempt."""


@dataclass(frozen=True, slots=True)
class ExecutionLease:
    """Per-turn write authority; not a runtime cancellation or tool-side-effect fence."""

    turn_id: str
    owner_id: str
    epoch: int

    def __post_init__(self) -> None:
        if self.turn_id != _entry_id(self.turn_id) or self.owner_id != _session_id(self.owner_id):
            raise DeliveryLedgerError("execution identity is invalid")
        _execution_epoch(self.epoch)


@dataclass(frozen=True, slots=True)
class ExecutionCheckpoint:
    """Latest redacted partial result and its actual producer, read atomically."""

    lease: ExecutionLease
    producer_epoch: int
    runtime_generation: str | None
    sequence: int | None
    phase: ExecutionPhase
    partial_text: str
    partial_hash: str
    revision: int
    state: ExecutionState


@dataclass(frozen=True, slots=True)
class DeliveryMessage:
    """One ordered Telegram payload persisted before network delivery."""

    kind: DeliveryKind
    text: str

    def __post_init__(self) -> None:
        if self.kind not in {"text", "markdown"}:
            raise ValueError("delivery kind is invalid")
        if not 1 <= len(self.text) <= MAX_DELIVERY_CHARS:
            raise ValueError("delivery text must use 1-16000 characters")


@dataclass(frozen=True, slots=True)
class QueuedTask:
    id: str
    chat_id: int
    session_key: str
    prompt: str
    created_at: float
    priority: int
    mode: QueueMode = "normal"
    parent_turn_id: str | None = None
    resume_context: str | None = None


@dataclass(frozen=True, slots=True)
class DurableTurn:
    id: str
    chat_id: int
    session_key: str
    session_id: str
    prompt: str
    status: str
    partial_text: str
    started_at: float
    updated_at: float
    queue_id: str | None


@dataclass(frozen=True, slots=True)
class QueueHandoff:
    queue_id: str
    parent_turn_id: str
    kind: HandoffKind
    state: str
    resume_queue_id: str | None


@dataclass(frozen=True, slots=True)
class OutboxItem:
    id: str
    chat_id: int
    turn_id: str | None
    position: int
    kind: DeliveryKind
    text: str
    status: DeliveryStatus


@dataclass(frozen=True, slots=True)
class DeliveryAttempt:
    """Host attempt token, not a Telegram client idempotency key."""

    item: OutboxItem
    epoch: int

    def __post_init__(self) -> None:
        _execution_epoch(self.epoch)


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    unknown_turns: tuple[DurableTurn, ...]
    unknown_deliveries: tuple[OutboxItem, ...]
    recovery_notices_created: int


@dataclass(frozen=True, slots=True)
class DeliveryHealthSummary:
    """Credential/content-free counts used by an operations reconciler."""

    pending_deliveries: int = 0
    sending_deliveries: int = 0
    unknown_deliveries: int = 0
    running_turns: int = 0
    unknown_turns: int = 0
    oldest_pending_age_seconds: float | None = None


class DurableDeliveryLedger:
    """Own queue/execution/outbox transitions behind one SQLite transaction seam.

    A network call is never replayed automatically after its outbox item enters
    ``sending``. Startup reconciliation changes it to ``unknown`` and requires an
    explicit retry, because Telegram has no client idempotency key.
    """

    def __init__(
        self,
        state_root: Path,
        *,
        id_factory: Callable[[], str] | None = None,
        clock: Callable[[], float] | None = None,
        max_queue_items: int = _MAX_QUEUE_ITEMS,
    ) -> None:
        root = state_root.expanduser()
        if (
            root == Path(root.anchor)
            or root.is_symlink()
            or ".xclaude" in root.parts
            or ".dsh" in root.parts
        ):
            raise DeliveryLedgerError("可靠交付狀態根目錄不安全")
        if not 1 <= max_queue_items <= 100:
            raise ValueError("queue limit must be between 1 and 100")
        self.root = root.resolve() / "delivery"
        self.path = self.root / "ledger.sqlite3"
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex[:8])
        self._clock = clock or time.time
        self.max_queue_items = max_queue_items

    def enqueue(
        self,
        *,
        chat_id: int,
        session_key: str,
        prompt: str,
        priority: bool = False,
        priority_rank: int | None = None,
        mode: QueueMode = "normal",
        parent_turn_id: str | None = None,
        resume_context: str | None = None,
    ) -> QueuedTask:
        chat = _chat_id(chat_id)
        key = _session_key(session_key)
        stored_prompt = _prompt(prompt)
        normalized_mode = _queue_mode(mode)
        normalized_parent = _optional_entry_id(parent_turn_id, label="父任務 ID")
        stored_context = _queue_context(resume_context)
        if priority_rank is not None:
            if (
                isinstance(priority_rank, bool)
                or not isinstance(priority_rank, int)
                or priority_rank < 0
                or priority_rank >= 2**63
            ):
                raise DeliveryLedgerError("排隊優先序狀態無效")
            if priority or normalized_mode == "priority":
                raise DeliveryLedgerError("priority_rank 不能與優先模式同時使用")
            if normalized_mode != "resume":
                raise DeliveryLedgerError("priority_rank 只能用於 resume 續接任務")
        if normalized_mode != "resume" and stored_context is not None:
            raise DeliveryLedgerError("只有 resume 排隊任務可以攜帶續接上下文")
        if normalized_mode == "resume" and normalized_parent is None:
            raise DeliveryLedgerError("resume 排隊任務必須綁定父任務")
        if normalized_mode == "resume" and stored_context is None:
            raise DeliveryLedgerError("resume 排隊任務必須攜帶續接上下文")
        with closing(self._connect(create=True)) as connection:
            with connection:
                count = cast(
                    int,
                    connection.execute(
                        "SELECT COUNT(*) FROM queue_items WHERE chat_id = ? "
                        "AND status IN ('queued', 'handoff')",
                        (chat,),
                    ).fetchone()[0],
                )
                if count >= self.max_queue_items:
                    raise DeliveryLedgerError(
                        f"每個聊天最多保留 {self.max_queue_items} 個排隊任務"
                    )
                task_id = self._new_id(connection)
                created_at = self._timestamp()
                rank = (
                    priority_rank
                    if priority_rank is not None
                    else (
                        self._next_priority(connection, chat)
                        if priority or normalized_mode == "priority"
                        else 0
                    )
                )
                connection.execute(
                    """
                    INSERT INTO queue_items (
                        id, chat_id, session_key, prompt, created_at, priority, status,
                        mode, parent_turn_id, resume_context
                    ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)
                    """,
                    (
                        task_id,
                        chat,
                        key,
                        stored_prompt,
                        created_at,
                        rank,
                        normalized_mode,
                        normalized_parent,
                        stored_context,
                    ),
                )
        self._secure()
        return QueuedTask(
            task_id,
            chat,
            key,
            stored_prompt,
            created_at,
            rank,
            normalized_mode,
            normalized_parent,
            stored_context,
        )

    def queued(self, chat_id: int) -> tuple[QueuedTask, ...]:
        chat = _chat_id(chat_id)
        if not self.path.is_file():
            return ()
        with closing(self._connect(create=False)) as connection:
            rows = connection.execute(
                """
                SELECT id, chat_id, session_key, prompt, created_at, priority,
                       mode, parent_turn_id, resume_context
                FROM queue_items
                WHERE chat_id = ? AND status = 'queued'
                ORDER BY priority DESC,
                         CASE
                             WHEN priority > 0 AND mode = 'resume' THEN -1
                             WHEN priority = 0 AND mode = 'resume' THEN 2
                             WHEN priority = 0 AND mode = 'followup' THEN 1
                             ELSE 0
                         END DESC,
                         created_at, id
                """,
                (chat,),
            ).fetchall()
        return tuple(_queued_task(row) for row in rows)

    def prepare_handoff(
        self,
        chat_id: int,
        queue_id: str,
        *,
        parent_turn_id: str,
        kind: HandoffKind,
        resume_context: str,
    ) -> QueueHandoff:
        """Reserve a validated transition before touching a running process.

        Held queue items cannot be dispatched, even after a process crash.
        An interruption reserves A's continuation before requesting its stop.
        """
        chat, item_id, parent_id = _chat_id(chat_id), _entry_id(queue_id), _entry_id(parent_turn_id)
        if kind not in {"merge", "interrupt", "replace"}:
            raise DeliveryLedgerError("任務切換方式無效")
        context = _queue_context(resume_context)
        with closing(self._connect(create=False)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            parent = _durable_turn(self._turn_row(connection, parent_id))
            row = connection.execute(
                "SELECT id, chat_id, session_key, prompt, created_at, priority, "
                "mode, parent_turn_id, resume_context FROM queue_items "
                "WHERE id = ? AND chat_id = ? AND status = 'queued'",
                (item_id, chat),
            ).fetchone()
            if row is None:
                raise DeliveryLedgerError("排隊項不存在、已開始或正在切換")
            item = _queued_task(row)
            if (
                parent.chat_id != chat
                or parent.status != "running"
                or item.parent_turn_id != parent_id
                or item.session_key != parent.session_key
            ):
                raise DeliveryLedgerError("原任務已變更; 舊選項不能操作目前任務, 請重新選擇")
            if connection.execute(
                "SELECT 1 FROM queue_handoffs WHERE chat_id = ? "
                "AND state IN ('prepared', 'unknown')",
                (chat,),
            ).fetchone():
                raise DeliveryLedgerError("尚有未確認的任務切換; 請先查看 /queue")
            original = _prompt(parent.prompt)
            merged = (
                _prompt(
                    "Continue the unfinished task under the amended user instructions. "
                    "The amendment overrides conflicting earlier requirements. "
                    "Inspect current state "
                    "before repeating side effects; do not restart completed work.\n\n"
                    f"Original task:\n{original}\n\nUser amendment:\n{item.prompt}"
                )
                if kind == "merge"
                else item.prompt
            )
            count = connection.execute(
                "SELECT COUNT(*) FROM queue_items WHERE chat_id = ? "
                "AND status IN ('queued', 'handoff')",
                (chat,),
            ).fetchone()[0]
            if kind == "interrupt" and count >= self.max_queue_items:
                raise DeliveryLedgerError("排隊已滿, 無法預留 A 的續接; 尚未中止 A")
            rank = self._next_priority(connection, chat)
            resume_id = self._new_id(connection) if kind == "interrupt" else None
            if resume_id is not None:
                connection.execute(
                    "INSERT INTO queue_items (id, chat_id, session_key, prompt, "
                    "created_at, priority, "
                    "status, mode, parent_turn_id, resume_context) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'handoff', 'resume', ?, ?)",
                    (
                        resume_id,
                        chat,
                        item.session_key,
                        original,
                        self._timestamp(),
                        rank,
                        parent_id,
                        context,
                    ),
                )
            connection.execute("UPDATE queue_items SET status = 'handoff' WHERE id = ?", (item_id,))
            connection.execute(
                "INSERT INTO queue_handoffs (queue_id, chat_id, parent_turn_id, kind, state, "
                "resume_queue_id, prompt, resume_context, priority) "
                "VALUES (?, ?, ?, ?, 'prepared', ?, ?, ?, ?) "
                "ON CONFLICT(queue_id) DO UPDATE SET state='prepared', kind=excluded.kind, "
                "resume_queue_id=excluded.resume_queue_id, prompt=excluded.prompt, "
                "resume_context=excluded.resume_context, priority=excluded.priority, notified=0",
                (item_id, chat, parent_id, kind, resume_id, merged, context, rank),
            )
        return QueueHandoff(item_id, parent_id, kind, "prepared", resume_id)

    def commit_handoff(self, chat_id: int, queue_id: str, *, resume_context: str) -> None:
        """Release the selected work and reserved continuation atomically."""
        chat, item_id = _chat_id(chat_id), _entry_id(queue_id)
        context = _queue_context(resume_context)
        with closing(self._connect(create=False)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT parent_turn_id, kind, resume_queue_id, prompt, priority "
                "FROM queue_handoffs "
                "WHERE queue_id = ? AND chat_id = ? AND state = 'prepared'",
                (item_id, chat),
            ).fetchone()
            if row is None:
                raise DeliveryLedgerError("任務切換已處理或結果未知")
            parent_id, kind, resume_id, prompt, rank = row
            parent = _durable_turn(self._turn_row(connection, str(parent_id)))
            if parent.status not in {"cancelled", "failed"}:
                raise DeliveryLedgerError("原任務尚未確認停止, 或已自然完成")
            connection.execute(
                "UPDATE queue_items SET status='queued', prompt=?, priority=?, "
                "mode=?, resume_context=? "
                "WHERE id=? AND status='handoff'",
                (
                    prompt,
                    rank,
                    "resume" if kind == "merge" else "priority",
                    context if kind == "merge" else None,
                    item_id,
                ),
            )
            if resume_id is not None:
                connection.execute(
                    "UPDATE queue_items SET status='queued', resume_context=? "
                    "WHERE id=? AND status='handoff'",
                    (context, resume_id),
                )
            connection.execute(
                "UPDATE queue_handoffs SET state='committed' WHERE queue_id=?", (item_id,)
            )
            self._record_queue_choice(connection, chat, item_id)

    def abort_handoff(self, chat_id: int, queue_id: str, *, uncertain: bool = False) -> None:
        """Only a confirmed no-stop restores FIFO; ambiguous stops remain held."""
        chat, item_id = _chat_id(chat_id), _entry_id(queue_id)
        with closing(self._connect(create=False)) as connection, connection:
            row = connection.execute(
                "SELECT resume_queue_id FROM queue_handoffs "
                "WHERE queue_id=? AND chat_id=? AND state='prepared'",
                (item_id, chat),
            ).fetchone()
            if row is None:
                return
            connection.execute(
                "UPDATE queue_handoffs SET state=? WHERE queue_id=?",
                ("unknown" if uncertain else "aborted", item_id),
            )
            if not uncertain:
                connection.execute(
                    "UPDATE queue_items SET status='queued' WHERE id=? AND status='handoff'",
                    (item_id,),
                )
                if row[0] is not None:
                    connection.execute(
                        "UPDATE queue_items SET status='dropped' WHERE id=?", (row[0],)
                    )

    def held_handoffs(self, chat_id: int) -> tuple[QueueHandoff, ...]:
        chat = _chat_id(chat_id)
        if not self.path.is_file():
            return ()
        with closing(self._connect(create=False)) as connection:
            rows = connection.execute(
                "SELECT queue_id, parent_turn_id, kind, state, resume_queue_id FROM queue_handoffs "
                "WHERE chat_id=? AND state IN ('prepared', 'unknown')",
                (chat,),
            ).fetchall()
        return tuple(
            QueueHandoff(
                str(r[0]),
                str(r[1]),
                cast(HandoffKind, r[2]),
                str(r[3]),
                None if r[4] is None else str(r[4]),
            )
            for r in rows
        )

    def queue_choice_consumed(self, chat_id: int, queue_id: str) -> bool:
        chat, item_id = _chat_id(chat_id), _entry_id(queue_id)
        if not self.path.is_file():
            return False
        with closing(self._connect(create=False)) as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM queue_choices WHERE chat_id=? AND queue_id=? "
                    "UNION ALL SELECT 1 FROM queue_handoffs WHERE chat_id=? AND queue_id=? "
                    "AND state IN ('prepared', 'unknown', 'committed')",
                    (chat, item_id, chat, item_id),
                ).fetchone()
                is not None
            )

    @staticmethod
    def _record_queue_choice(connection: sqlite3.Connection, chat_id: int, queue_id: str) -> None:
        connection.execute(
            "INSERT OR IGNORE INTO queue_choices (chat_id, queue_id) VALUES (?, ?)",
            (chat_id, queue_id),
        )

    def _recover_handoffs(self) -> None:
        with closing(self._connect(create=False)) as connection, connection:
            connection.execute("UPDATE queue_handoffs SET state='unknown' WHERE state='prepared'")
            rows = connection.execute(
                "SELECT queue_id, chat_id, parent_turn_id FROM queue_handoffs "
                "WHERE state='unknown' AND notified=0",
            ).fetchall()
            for item_id, chat, parent_id in rows:
                notice = (
                    f"⚠️ 任務切換 {item_id} 的停止結果未知, 未自動重跑。"
                    f"原任務 {parent_id} 的進度已保留。查看 /queue; "
                    f"確認狀態後可用 /queue drop {item_id} 移除切換, 再手動續接原任務。"
                )
                now = self._timestamp()
                connection.execute(
                    "INSERT INTO outbox (id, chat_id, turn_id, position, kind, "
                    "payload, status, created_at, updated_at) "
                    "VALUES (?, ?, NULL, 0, 'text', ?, 'pending', ?, ?)",
                    (self._new_id(connection), chat, notice, now, now),
                )
                connection.execute(
                    "UPDATE queue_handoffs SET notified=1 WHERE queue_id=?", (item_id,)
                )

    def queued_chat_ids(self) -> tuple[int, ...]:
        if not self.path.is_file():
            return ()
        with closing(self._connect(create=False)) as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT chat_id FROM queue_items
                WHERE status = 'queued' ORDER BY chat_id
                """
            ).fetchall()
        return tuple(int(row[0]) for row in rows)

    def promote(self, chat_id: int, queue_id: str) -> QueuedTask:
        chat = _chat_id(chat_id)
        item_id = _entry_id(queue_id)
        with closing(self._connect(create=False)) as connection:
            with connection:
                rank = self._next_priority(connection, chat)
                cursor = connection.execute(
                    """
                    UPDATE queue_items SET priority = ?, mode = 'priority'
                    WHERE id = ? AND chat_id = ? AND status = 'queued'
                    """,
                    (rank, item_id, chat),
                )
                if cursor.rowcount != 1:
                    raise DeliveryLedgerError(f"排隊任務不存在或已開始: {item_id}")
                self._record_queue_choice(connection, chat, item_id)
                row = connection.execute(
                    """
                        SELECT id, chat_id, session_key, prompt, created_at, priority,
                               mode, parent_turn_id, resume_context
                        FROM queue_items WHERE id = ?
                    """,
                    (item_id,),
                ).fetchone()
        if row is None:
            raise DeliveryLedgerError("排隊任務狀態遺失")
        return _queued_task(row)

    def set_queue_mode(self, chat_id: int, queue_id: str, mode: QueueMode) -> QueuedTask:
        """Set an explicit dispatch mode for one still-queued item."""

        chat = _chat_id(chat_id)
        item_id = _entry_id(queue_id)
        normalized_mode = _queue_mode(mode)
        if normalized_mode == "resume":
            raise DeliveryLedgerError("resume 模式只能由 enqueue_resume 建立")
        with closing(self._connect(create=False)) as connection:
            with connection:
                priority = (
                    self._next_priority(connection, chat)
                    if normalized_mode == "priority"
                    else 0
                )
                cursor = connection.execute(
                    """
                    UPDATE queue_items SET priority = ?, mode = ?
                    WHERE id = ? AND chat_id = ? AND status = 'queued'
                    """,
                    (priority, normalized_mode, item_id, chat),
                )
                if cursor.rowcount != 1:
                    raise DeliveryLedgerError(f"排隊任務不存在或已開始: {item_id}")
                self._record_queue_choice(connection, chat, item_id)
                row = connection.execute(
                    """
                    SELECT id, chat_id, session_key, prompt, created_at, priority,
                           mode, parent_turn_id, resume_context
                    FROM queue_items WHERE id = ?
                    """,
                    (item_id,),
                ).fetchone()
        if row is None:
            raise DeliveryLedgerError("排隊任務狀態遺失")
        return _queued_task(row)

    def enqueue_resume(
        self,
        *,
        chat_id: int,
        session_key: str,
        prompt: str,
        parent_turn_id: str,
        resume_context: str,
        after_priority: int | None = None,
    ) -> QueuedTask:
        """Persist a bounded continuation after an explicitly interrupted turn."""

        return self.enqueue(
            chat_id=chat_id,
            session_key=session_key,
            prompt=prompt,
            priority_rank=after_priority,
            mode="resume",
            parent_turn_id=parent_turn_id,
            resume_context=resume_context,
        )

    def drop_queued(self, chat_id: int, queue_id: str) -> None:
        chat, item_id = _chat_id(chat_id), _entry_id(queue_id)
        with closing(self._connect(create=False)) as connection, connection:
            held = connection.execute(
                "SELECT resume_queue_id FROM queue_handoffs "
                "WHERE queue_id=? AND chat_id=? AND state='unknown'",
                (item_id, chat),
            ).fetchone()
            if held is not None:
                connection.execute("UPDATE queue_items SET status='dropped' WHERE id IN (?, ?)",
                                   (item_id, held[0]))
                connection.execute("UPDATE queue_handoffs SET state='discarded' WHERE queue_id=?",
                                   (item_id,))
                self._record_queue_choice(connection, chat, item_id)
                return
        self._transition_queue(chat_id, queue_id, target="dropped")

    def clear_queued(self, chat_id: int) -> int:
        chat = _chat_id(chat_id)
        if not self.path.is_file():
            return 0
        with closing(self._connect(create=False)) as connection:
            with connection:
                cursor = connection.execute(
                    """
                    UPDATE queue_items SET status = 'dropped'
                    WHERE chat_id = ? AND status = 'queued'
                    """,
                    (chat,),
                )
        return max(0, cursor.rowcount)

    def begin_direct(
        self,
        *,
        chat_id: int,
        session_key: str,
        session_id: str,
        prompt: str,
        allow_sensitive_prompt: bool = False,
    ) -> DurableTurn:
        return self._begin_turn(
            chat_id=chat_id,
            session_key=session_key,
            session_id=session_id,
            prompt=prompt,
            queue_id=None,
            allow_sensitive_prompt=allow_sensitive_prompt,
        )

    def begin_queued(self, *, queue_id: str, session_id: str) -> DurableTurn:
        item_id = _entry_id(queue_id)
        normalized_session = _session_id(session_id)
        with closing(self._connect(create=False)) as connection:
            with connection:
                row = connection.execute(
                    """
                    SELECT chat_id, session_key, prompt, mode, parent_turn_id
                    FROM queue_items WHERE id = ? AND status = 'queued'
                    """,
                    (item_id,),
                ).fetchone()
                if row is None:
                    raise DeliveryLedgerError(
                        f"排隊任務不存在或已開始: {item_id}"
                    )
                logical_started_at: float | None = None
                if str(row[3]) == "resume" and row[4] is not None:
                    parent = _durable_turn(self._turn_row(connection, str(row[4])))
                    logical_started_at = parent.started_at
                turn = self._insert_turn(
                    connection,
                    chat_id=int(row[0]),
                    session_key=str(row[1]),
                    session_id=normalized_session,
                    prompt=str(row[2]),
                    queue_id=item_id,
                    started_at=logical_started_at,
                )
                connection.execute(
                    """
                    UPDATE queue_items SET status = 'started', turn_id = ?
                    WHERE id = ?
                    """,
                    (turn.id, item_id),
                )
        self._secure()
        return turn

    def claim_execution(self, turn_id: str, *, owner_id: str) -> ExecutionLease:
        """Claim a running turn once, using a durable per-turn epoch (no time expiry)."""
        item_id, owner = _entry_id(turn_id), _session_id(owner_id)
        with closing(self._connect(create=False)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT owner_id, epoch, state FROM execution_owners WHERE turn_id = ?",
                (item_id,),
            ).fetchone()
            if self._turn_row(connection, item_id)[5] != "running":
                raise ExecutionFencedError("execution turn is not running")
            if row is not None:
                if row[0] != owner or row[2] != "running":
                    raise ExecutionFencedError("execution is already claimed or retired")
                return ExecutionLease(item_id, owner, int(row[1]))
            connection.execute(
                "INSERT INTO execution_owners (turn_id, owner_id, epoch, state) "
                "VALUES (?, ?, 1, 'running')", (item_id, owner),
            )
        return ExecutionLease(item_id, owner, 1)

    def bind_execution(
        self, execution: ExecutionLease, *, runtime_generation: str,
    ) -> ExecutionLease:
        """Bind an adapter-confirmed source; a source change revokes older tokens.

        The caller must confirm the source against its current runner. An event's
        self-reported generation is not authority to rebind the execution.
        """
        generation = _session_id(runtime_generation)
        with closing(self._connect(create=False)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require_execution(connection, execution.turn_id, execution)
            assert row is not None
            epoch = execution.epoch
            if row[3] is not None and row[3] != generation:
                epoch = _execution_epoch(epoch + 1)
                connection.execute(
                    "UPDATE execution_owners SET epoch = ?, sequence = NULL WHERE turn_id = ?",
                    (epoch, execution.turn_id),
                )
            connection.execute(
                "UPDATE execution_owners SET runtime_generation = ? WHERE turn_id = ?",
                (generation, execution.turn_id),
            )
        return ExecutionLease(execution.turn_id, execution.owner_id, epoch)

    def retire_execution(self, execution: ExecutionLease, *, partial_text: str) -> None:
        """Persist a recovery boundary and revoke writes; does not cancel native tools."""
        partial = _partial(partial_text)
        with closing(self._connect(create=False)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require_execution(
                connection, execution.turn_id, execution, allow_stopping=True,
            )
            assert row is not None
            self._write_execution_checkpoint(
                connection, execution, partial, row[3], row[4], "stopping",
            )
            connection.execute(
                "UPDATE execution_owners SET state = 'retired' WHERE turn_id = ?",
                (execution.turn_id,),
            )
            connection.execute(
                "UPDATE turns SET status = 'unknown', partial_text = ?, updated_at = ? "
                "WHERE id = ?", (partial, self._timestamp(), execution.turn_id),
            )

    def resume_execution(
        self, turn_id: str, *, expected_epoch: int, owner_id: str,
    ) -> ExecutionLease:
        """Explicit host-state handover, not permission to replay tools automatically."""
        item_id, owner = _entry_id(turn_id), _session_id(owner_id)
        previous = _execution_epoch(expected_epoch)
        epoch = _execution_epoch(previous + 1)
        with closing(self._connect(create=False)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE execution_owners SET owner_id = ?, epoch = ?, state = 'running', "
                "runtime_generation = NULL, sequence = NULL "
                "WHERE turn_id = ? AND epoch = ? AND state = 'retired' "
                "AND EXISTS (SELECT 1 FROM turns WHERE id = ? AND status = 'unknown')",
                (owner, epoch, item_id, previous, item_id),
            )
            if cursor.rowcount != 1:
                raise ExecutionFencedError("execution handover boundary no longer matches")
            connection.execute(
                "UPDATE turns SET status = 'running', updated_at = ? WHERE id = ?",
                (self._timestamp(), item_id),
            )
        return ExecutionLease(item_id, owner, epoch)

    def execution_checkpoint(self, turn_id: str) -> ExecutionCheckpoint | None:
        item_id = _entry_id(turn_id)
        with closing(self._connect(create=False)) as connection:
            row = connection.execute(
                "SELECT o.owner_id, o.epoch, o.state, c.producer_epoch, "
                "c.runtime_generation, c.sequence, c.phase, c.partial_hash, c.revision, "
                "t.partial_text FROM execution_owners o "
                "JOIN execution_checkpoints c USING (turn_id) JOIN turns t ON t.id = o.turn_id "
                "WHERE o.turn_id = ?", (item_id,),
            ).fetchone()
        if row is None:
            return None
        partial = str(row[9])
        if hashlib.sha256(partial.encode("utf-8")).hexdigest() != row[7]:
            raise DeliveryLedgerError("execution checkpoint integrity mismatch")
        return ExecutionCheckpoint(
            ExecutionLease(item_id, str(row[0]), int(row[1])), int(row[3]),
            None if row[4] is None else str(row[4]),
            None if row[5] is None else int(row[5]), cast(ExecutionPhase, str(row[6])),
            partial, str(row[7]), int(row[8]), cast(ExecutionState, str(row[2])),
        )

    @staticmethod
    def _require_execution(
        connection: sqlite3.Connection, turn_id: str, execution: ExecutionLease | None,
        *, allow_stopping: bool = False,
    ) -> tuple[object, ...] | None:
        row = connection.execute(
            "SELECT owner_id, epoch, state, runtime_generation, sequence "
            "FROM execution_owners WHERE turn_id = ?", (turn_id,),
        ).fetchone()
        if row is None and execution is None:
            return None  # Legacy, unclaimed turn; never bypass a claimed turn's fence.
        if (row is None or execution is None or execution.turn_id != turn_id
                or row[0] != execution.owner_id or row[1] != execution.epoch
                or row[2] not in ({"running", "stopping"} if allow_stopping else {"running"})):
            raise ExecutionFencedError("execution write authority has been revoked")
        return cast(tuple[object, ...], row)

    @staticmethod
    def _write_execution_checkpoint(
        connection: sqlite3.Connection, execution: ExecutionLease, partial: str,
        runtime_generation: object, sequence: object, phase: ExecutionPhase,
    ) -> None:
        connection.execute(
            "INSERT INTO execution_checkpoints "
            "(turn_id, producer_epoch, runtime_generation, sequence, "
            "phase, partial_hash, revision) "
            "VALUES (?, ?, ?, ?, ?, ?, 1) ON CONFLICT(turn_id) DO UPDATE SET "
            "producer_epoch = excluded.producer_epoch, "
            "runtime_generation = excluded.runtime_generation, sequence = excluded.sequence, "
            "phase = excluded.phase, partial_hash = excluded.partial_hash, revision = revision + 1",
            (execution.turn_id, execution.epoch, runtime_generation, sequence, phase,
             hashlib.sha256(partial.encode("utf-8")).hexdigest()),
        )

    def checkpoint(
        self, turn_id: str, partial_text: str, *, execution: ExecutionLease | None = None,
        runtime_generation: str | None = None, sequence: int | None = None,
        phase: ExecutionPhase = "generating",
    ) -> DurableTurn:
        item_id = _entry_id(turn_id)
        partial = _partial(partial_text)
        if phase not in _EXECUTION_PHASES:
            raise DeliveryLedgerError("execution phase is invalid")
        if sequence is not None and (
            isinstance(sequence, bool) or not isinstance(sequence, int) or not 0 <= sequence < 2**63
        ):
            raise DeliveryLedgerError("execution sequence is invalid")
        with closing(self._connect(create=False)) as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                owner = self._require_execution(connection, item_id, execution)
                if owner is not None:
                    if runtime_generation != owner[3]:
                        raise ExecutionFencedError("execution runtime source does not match")
                    if (sequence is not None and owner[4] is not None
                            and sequence < cast(int, owner[4])):
                        raise ExecutionFencedError("execution sequence is obsolete")
                    assert execution is not None
                    self._write_execution_checkpoint(
                        connection, execution, partial, runtime_generation,
                        sequence if sequence is not None else owner[4], phase,
                    )
                    connection.execute(
                        "UPDATE execution_owners SET sequence = COALESCE(?, sequence), state = ? "
                        "WHERE turn_id = ?",
                        (sequence, "stopping" if phase == "stopping" else "running", item_id),
                    )
                cursor = connection.execute(
                    """
                    UPDATE turns SET partial_text = ?, updated_at = ?
                    WHERE id = ? AND status = 'running'
                    """,
                    (partial, self._timestamp(), item_id),
                )
                if cursor.rowcount != 1:
                    raise DeliveryLedgerError(f"執行記錄不可更新: {item_id}")
                row = self._turn_row(connection, item_id)
        return _durable_turn(row)

    def finish(
        self,
        turn_id: str,
        *,
        outcome: TurnOutcome,
        partial_text: str,
        messages: tuple[DeliveryMessage, ...],
        execution: ExecutionLease | None = None,
        runtime_generation: str | None = None,
        sequence: int | None = None,
    ) -> tuple[OutboxItem, ...]:
        item_id = _entry_id(turn_id)
        if outcome not in {"completed", "failed", "cancelled"}:
            raise ValueError("terminal turn outcome is invalid")
        partial = _partial(partial_text)
        if sequence is not None and (
            isinstance(sequence, bool) or not isinstance(sequence, int) or not 0 <= sequence < 2**63
        ):
            raise DeliveryLedgerError("execution sequence is invalid")
        if len(messages) > MAX_DELIVERY_MESSAGES:
            raise DeliveryLedgerError("單一任務的可靠回覆不能超過 256 段")
        now = self._timestamp()
        with closing(self._connect(create=False)) as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                owner = self._require_execution(
                    connection, item_id, execution, allow_stopping=True,
                )
                if owner is not None:
                    if runtime_generation is not None and runtime_generation != owner[3]:
                        raise ExecutionFencedError("execution terminal source does not match")
                    if (sequence is not None and owner[4] is not None
                            and sequence < cast(int, owner[4])):
                        raise ExecutionFencedError("execution terminal sequence is obsolete")
                cursor = connection.execute(
                    """
                    UPDATE turns SET status = ?, partial_text = ?, updated_at = ?
                    WHERE id = ? AND status = 'running'
                    """,
                    (outcome, partial, now, item_id),
                )
                if cursor.rowcount != 1:
                    raise DeliveryLedgerError(f"執行記錄不可完成: {item_id}")
                if owner is not None:
                    assert execution is not None
                    self._write_execution_checkpoint(
                        connection, execution, partial, owner[3],
                        sequence if sequence is not None else owner[4],
                        "stopping" if owner[2] == "stopping" else "finished",
                    )
                    connection.execute(
                        "UPDATE execution_owners SET state = 'retired', "
                        "sequence = COALESCE(?, sequence) WHERE turn_id = ?",
                        (sequence, item_id),
                    )
                turn_row = self._turn_row(connection, item_id)
                chat_id = cast(int, turn_row[1])
                created: list[OutboxItem] = []
                for position, message in enumerate(messages):
                    delivery_id = self._new_id(connection)
                    connection.execute(
                        """
                        INSERT INTO outbox (
                            id, chat_id, turn_id, position, kind, payload,
                            status, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                        """,
                        (
                            delivery_id,
                            chat_id,
                            item_id,
                            position,
                            message.kind,
                            message.text,
                            now,
                            now,
                        ),
                    )
                    created.append(
                        OutboxItem(
                            delivery_id,
                            chat_id,
                            item_id,
                            position,
                            message.kind,
                            message.text,
                            "pending",
                        )
                    )
        self._secure()
        return tuple(created)

    def pending_outbox(self, chat_id: int) -> tuple[OutboxItem, ...]:
        return self._outbox_by_status(chat_id, "pending")

    def health_summary(self, *, now: float | None = None) -> DeliveryHealthSummary:
        """Read delivery/turn counters without loading prompts or payloads."""

        if not self.path.is_file():
            return DeliveryHealthSummary()
        timestamp = self._timestamp() if now is None else float(now)
        if timestamp < 0:
            raise DeliveryLedgerError("可靠狀態時間無效")
        try:
            with closing(self._connect_read_only()) as connection:
                outbox_rows = connection.execute(
                    "SELECT status, COUNT(*) FROM outbox GROUP BY status"
                ).fetchall()
                turn_rows = connection.execute(
                    "SELECT status, COUNT(*) FROM turns GROUP BY status"
                ).fetchall()
                oldest = connection.execute(
                    "SELECT MIN(created_at) FROM outbox WHERE status = 'pending'"
                ).fetchone()
        except sqlite3.Error as error:
            raise DeliveryLedgerError("可靠交付資料庫不可讀") from error
        outbox = {str(status): int(count) for status, count in outbox_rows}
        turns = {str(status): int(count) for status, count in turn_rows}
        oldest_created = oldest[0] if oldest is not None else None
        oldest_age = (
            max(0.0, timestamp - float(oldest_created))
            if oldest_created is not None
            else None
        )
        return DeliveryHealthSummary(
            pending_deliveries=outbox.get("pending", 0),
            sending_deliveries=outbox.get("sending", 0),
            unknown_deliveries=outbox.get("unknown", 0),
            running_turns=turns.get("running", 0),
            unknown_turns=turns.get("unknown", 0),
            oldest_pending_age_seconds=oldest_age,
        )

    def enqueue_notification(
        self,
        *,
        chat_id: int,
        dedupe_key: str,
        messages: tuple[DeliveryMessage, ...],
        turn_id: str | None = None,
    ) -> tuple[OutboxItem, ...]:
        """Persist a deduplicated notification, optionally appended to a settled turn.

        A late report does not change the original outcome or partial answer.
        Cancelled turns suppress late reports. The outbox, not the network, is
        exactly-once: ambiguous transport receipts still require explicit action.
        """

        chat = _chat_id(chat_id)
        key = dedupe_key.strip().casefold()
        if _DEDUPE_KEY.fullmatch(key) is None:
            raise DeliveryLedgerError("可靠通知去重鍵無效")
        if not messages or len(messages) > MAX_DELIVERY_MESSAGES:
            raise DeliveryLedgerError("可靠通知必須包含 1-32 段")
        now = self._timestamp()
        parent = _entry_id(turn_id) if turn_id is not None else None
        with closing(self._connect(create=True)) as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                base_position = 0
                if parent is not None:
                    row = connection.execute(
                        "SELECT chat_id, status FROM turns WHERE id = ?", (parent,),
                    ).fetchone()
                    if row is None or row[0] != chat:
                        raise DeliveryLedgerError("可靠通知的原任務不存在或不屬於此對話")
                    if row[1] == "cancelled":
                        return ()
                    if row[1] not in {"completed", "failed"}:
                        raise DeliveryLedgerError("可靠通知的原任務尚未確認收尾")
                    base_position = connection.execute(
                        "SELECT COALESCE(MAX(position), -1) + 1 FROM outbox WHERE turn_id = ?",
                        (parent,),
                    ).fetchone()[0]
                existing_rows = connection.execute(
                    """
                    SELECT id, chat_id, turn_id, position, kind, payload, status
                    FROM outbox WHERE dedupe_key = ? ORDER BY position
                    """,
                    (key,),
                ).fetchall()
                if existing_rows:
                    existing = tuple(_outbox_item(row) for row in existing_rows)
                    expected = tuple((item.kind, item.text) for item in messages)
                    actual = tuple((item.kind, item.text) for item in existing)
                    if len(existing) != len(messages) or actual != expected or any(
                        item.chat_id != chat or item.turn_id != parent for item in existing
                    ):
                        raise DeliveryLedgerError("可靠通知去重鍵已綁定不同 payload")
                    return existing
                created: list[OutboxItem] = []
                for position, message in enumerate(messages, start=base_position):
                    delivery_id = self._new_id(connection)
                    connection.execute(
                        """
                        INSERT INTO outbox (
                            id, chat_id, turn_id, position, kind, payload,
                            status, created_at, updated_at, dedupe_key
                        ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                        """,
                        (
                            delivery_id,
                            chat,
                            parent,
                            position,
                            message.kind,
                            message.text,
                            now,
                            now,
                            key,
                        ),
                    )
                    created.append(
                        OutboxItem(
                            delivery_id,
                            chat,
                            parent,
                            position,
                            message.kind,
                            message.text,
                            "pending",
                        )
                    )
        self._secure()
        return tuple(created)

    def unknown_outbox(self, chat_id: int) -> tuple[OutboxItem, ...]:
        return self._outbox_by_status(chat_id, "unknown")

    def pending_chat_ids(self) -> tuple[int, ...]:
        if not self.path.is_file():
            return ()
        with closing(self._connect(create=False)) as connection:
            rows = connection.execute(
                "SELECT DISTINCT chat_id FROM outbox WHERE status = 'pending' ORDER BY chat_id"
            ).fetchall()
        return tuple(int(row[0]) for row in rows)

    def claim_delivery(self, chat_id: int, delivery_id: str) -> OutboxItem:
        return self._transition_delivery(
            chat_id,
            delivery_id,
            expected="pending",
            target="sending",
        )

    def claim_delivery_attempt(self, chat_id: int, delivery_id: str) -> DeliveryAttempt:
        """Commit submission intent and epoch before the caller sends anything."""
        chat, item_id = _chat_id(chat_id), _entry_id(delivery_id)
        with closing(self._connect(create=False)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE outbox SET status = 'sending', updated_at = ? "
                "WHERE id = ? AND chat_id = ? AND status = 'pending'",
                (self._timestamp(), item_id, chat),
            )
            if cursor.rowcount != 1:
                raise DeliveryLedgerError("delivery is not pending")
            previous = connection.execute(
                "SELECT epoch FROM delivery_attempts WHERE delivery_id = ?", (item_id,),
            ).fetchone()
            epoch = _execution_epoch(1 if previous is None else int(previous[0]) + 1)
            connection.execute(
                "INSERT INTO delivery_attempts (delivery_id, epoch) VALUES (?, ?) "
                "ON CONFLICT(delivery_id) DO UPDATE SET epoch = excluded.epoch",
                (item_id, epoch),
            )
            row = connection.execute(
                "SELECT id, chat_id, turn_id, position, kind, payload, status "
                "FROM outbox WHERE id = ?", (item_id,),
            ).fetchone()
            assert row is not None
        return DeliveryAttempt(_outbox_item(row), epoch)

    def mark_delivered(
        self,
        chat_id: int,
        delivery_id: str,
        *,
        telegram_message_id: int | None,
        attempt: DeliveryAttempt | None = None,
    ) -> OutboxItem:
        if telegram_message_id is not None and telegram_message_id <= 0:
            raise ValueError("Telegram message ID must be positive")
        return self._transition_delivery(
            chat_id,
            delivery_id,
            expected="sending",
            target="delivered",
            telegram_message_id=telegram_message_id,
            attempt=attempt,
        )

    def mark_delivery_unknown(
        self, chat_id: int, delivery_id: str, *, attempt: DeliveryAttempt | None = None,
    ) -> OutboxItem:
        return self._transition_delivery(
            chat_id,
            delivery_id,
            expected="sending",
            target="unknown",
            attempt=attempt,
        )

    def defer_unsent_delivery(
        self, chat_id: int, delivery_id: str, *, attempt: DeliveryAttempt | None = None,
    ) -> OutboxItem:
        """Only for positively classified pre-submission failures; safe to retry."""
        return self._transition_delivery(
            chat_id, delivery_id, expected="sending", target="pending",
            attempt=attempt,
        )

    def abandon_delivery(
        self, chat_id: int, delivery_id: str, *, attempt: DeliveryAttempt | None = None,
    ) -> OutboxItem:
        """Mark one attempted delivery unknown and queue an explicit recovery notice."""

        chat = _chat_id(chat_id)
        item_id = _entry_id(delivery_id)
        now = self._timestamp()
        with closing(self._connect(create=False)) as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                self._require_delivery_attempt(connection, chat, item_id, attempt)
                cursor = connection.execute(
                    """
                    UPDATE outbox SET status = 'unknown', updated_at = ?
                    WHERE id = ? AND chat_id = ? AND status = 'sending'
                    """,
                    (now, item_id, chat),
                )
                if cursor.rowcount != 1:
                    raise DeliveryLedgerError(
                        f"可靠回覆 {item_id} 目前不可標記為 unknown"
                    )
                row = connection.execute(
                    """
                    SELECT id, chat_id, turn_id, position, kind, payload, status,
                           dedupe_key
                    FROM outbox WHERE id = ?
                    """,
                    (item_id,),
                ).fetchone()
                if row is None:
                    raise DeliveryLedgerError("可靠回覆狀態遺失")
                item = _outbox_item(row[:7])
                dedupe_key = None if row[7] is None else str(row[7])
                if dedupe_key is None or not dedupe_key.startswith(
                    _RECOVERY_DEDUPE_PREFIX
                ):
                    self._insert_delivery_recovery_notice(connection, item, now)
        return item

    def retry_delivery(self, chat_id: int, delivery_id: str) -> OutboxItem:
        return self._transition_delivery(
            chat_id,
            delivery_id,
            expected="unknown",
            target="pending",
        )

    def drop_delivery(self, chat_id: int, delivery_id: str) -> OutboxItem:
        return self._transition_delivery(
            chat_id,
            delivery_id,
            expected="unknown",
            target="dropped",
        )

    def unknown_turns(self, chat_id: int) -> tuple[DurableTurn, ...]:
        chat = _chat_id(chat_id)
        if not self.path.is_file():
            return ()
        with closing(self._connect(create=False)) as connection:
            rows = connection.execute(
                """
                SELECT id, chat_id, session_key, session_id, prompt, status,
                       partial_text, started_at, updated_at, queue_id
                FROM turns WHERE chat_id = ? AND status = 'unknown'
                ORDER BY started_at, id
                """,
                (chat,),
            ).fetchall()
        return tuple(_durable_turn(row) for row in rows)

    def retry_turn(self, chat_id: int, turn_id: str) -> QueuedTask:
        chat = _chat_id(chat_id)
        item_id = _entry_id(turn_id)
        with closing(self._connect(create=False)) as connection:
            with connection:
                row = self._turn_row(connection, item_id)
                turn = _durable_turn(row)
                if turn.chat_id != chat or turn.status != "unknown":
                    raise DeliveryLedgerError(f"未知執行記錄不存在: {item_id}")
                count = cast(
                    int,
                    connection.execute(
                        "SELECT COUNT(*) FROM queue_items WHERE chat_id = ? AND status = 'queued'",
                        (chat,),
                    ).fetchone()[0],
                )
                if count >= self.max_queue_items:
                    raise DeliveryLedgerError(
                        f"每個聊天最多保留 {self.max_queue_items} 個排隊任務"
                )
                queue_id = self._new_id(connection)
                now = self._timestamp()
                priority = self._next_priority(connection, chat)
                connection.execute(
                    """
                    INSERT INTO queue_items (
                        id, chat_id, session_key, prompt, created_at, priority, status,
                        mode, parent_turn_id, resume_context
                    ) VALUES (?, ?, ?, ?, ?, ?, 'queued', 'priority', NULL, NULL)
                    """,
                    (queue_id, chat, turn.session_key, turn.prompt, now, priority),
                )
                connection.execute(
                    "UPDATE turns SET status = 'retried', updated_at = ? WHERE id = ?",
                    (now, item_id),
                )
        return QueuedTask(
            queue_id,
            chat,
            turn.session_key,
            turn.prompt,
            now,
            priority,
            "priority",
            None,
            None,
        )

    def resolve_turn(self, chat_id: int, turn_id: str) -> DurableTurn:
        chat = _chat_id(chat_id)
        item_id = _entry_id(turn_id)
        with closing(self._connect(create=False)) as connection:
            with connection:
                cursor = connection.execute(
                    """
                    UPDATE turns SET status = 'resolved', updated_at = ?
                    WHERE id = ? AND chat_id = ? AND status = 'unknown'
                    """,
                    (self._timestamp(), item_id, chat),
                )
                if cursor.rowcount != 1:
                    raise DeliveryLedgerError(f"未知執行記錄不存在: {item_id}")
                row = self._turn_row(connection, item_id)
        return _durable_turn(row)

    def reconcile(self) -> ReconciliationResult:
        """Mark ambiguous network/task state unknown and create durable notices."""

        if not self.path.is_file():
            return ReconciliationResult((), (), 0)
        self._recover_handoffs()
        now = self._timestamp()
        with closing(self._connect(create=False)) as connection:
            with connection:
                sending_rows = connection.execute(
                    """
                    SELECT id, chat_id, turn_id, position, kind, payload, status,
                           dedupe_key
                    FROM outbox WHERE status = 'sending' ORDER BY created_at, position
                    """
                ).fetchall()
                connection.execute(
                    "UPDATE outbox SET status = 'unknown', updated_at = ? WHERE status = 'sending'",
                    (now,),
                )
                running_rows = connection.execute(
                    """
                    SELECT id, chat_id, session_key, session_id, prompt, status,
                           partial_text, started_at, updated_at, queue_id
                    FROM turns WHERE status = 'running' ORDER BY started_at, id
                    """
                ).fetchall()
                connection.execute(
                    "UPDATE turns SET status = 'unknown', updated_at = ? WHERE status = 'running'",
                    (now,),
                )
                connection.execute(
                    "UPDATE execution_owners SET state = 'retired' "
                    "WHERE turn_id IN (SELECT id FROM turns WHERE status = 'unknown')",
                )
                notices = 0
                for row in sending_rows:
                    item = _outbox_item((*row[:6], "unknown"))
                    dedupe_key = None if row[7] is None else str(row[7])
                    if (
                        dedupe_key is None
                        or not dedupe_key.startswith(_RECOVERY_DEDUPE_PREFIX)
                    ) and self._insert_delivery_recovery_notice(connection, item, now):
                        notices += 1
                for row in running_rows:
                    turn = _durable_turn((*row[:5], "unknown", row[6], row[7], now, row[9]))
                    recovery_notified = connection.execute(
                        "SELECT recovery_notified FROM turns WHERE id = ?",
                        (turn.id,),
                    ).fetchone()
                    if recovery_notified is None or int(recovery_notified[0]):
                        continue
                    delivery_id = self._new_id(connection)
                    partial_note = (
                        f" 已保留 {len(turn.partial_text)} 字部分答案。"
                        if turn.partial_text
                        else " 尚無可保留的部分答案。"
                    )
                    payload = (
                        f"⚠️ 偵測到中斷中的任務 {turn.id}; 是否已產生外部副作用未知, "
                        f"因此不會自動重跑。{partial_note}\n"
                        f"使用 /queue retry {turn.id} 明確重跑, 或 /queue resolve "
                        f"{turn.id} 接受現況。"
                    )
                    connection.execute(
                        """
                        INSERT INTO outbox (
                            id, chat_id, turn_id, position, kind, payload,
                            status, created_at, updated_at
                        ) VALUES (?, ?, NULL, 0, 'text', ?, 'pending', ?, ?)
                        """,
                        (delivery_id, turn.chat_id, payload, now, now),
                    )
                    if turn.partial_text:
                        partial_id = self._new_id(connection)
                        connection.execute(
                            """
                            INSERT INTO outbox (
                                id, chat_id, turn_id, position, kind, payload,
                                status, created_at, updated_at
                            ) VALUES (?, ?, NULL, 1, 'markdown', ?, 'pending', ?, ?)
                            """,
                            (
                                partial_id,
                                turn.chat_id,
                                "### 中斷前保留的部分答案\n\n" + turn.partial_text,
                                now,
                                now,
                            ),
                        )
                    connection.execute(
                        "UPDATE turns SET recovery_notified = 1 WHERE id = ?",
                        (turn.id,),
                    )
                    notices += 1
        return ReconciliationResult(
            unknown_turns=tuple(
                _durable_turn((*row[:5], "unknown", row[6], row[7], now, row[9]))
                for row in running_rows
            ),
            unknown_deliveries=tuple(
                _outbox_item((*row[:6], "unknown")) for row in sending_rows
            ),
            recovery_notices_created=notices,
        )

    def _begin_turn(
        self,
        *,
        chat_id: int,
        session_key: str,
        session_id: str,
        prompt: str,
        queue_id: str | None,
        allow_sensitive_prompt: bool = False,
    ) -> DurableTurn:
        chat = _chat_id(chat_id)
        key = _session_key(session_key)
        normalized_session = _session_id(session_id)
        stored_prompt = _prompt(prompt, allow_sensitive=allow_sensitive_prompt)
        with closing(self._connect(create=True)) as connection:
            with connection:
                turn = self._insert_turn(
                    connection,
                    chat_id=chat,
                    session_key=key,
                    session_id=normalized_session,
                    prompt=stored_prompt,
                    queue_id=queue_id,
                )
        self._secure()
        return turn

    def _insert_turn(
        self,
        connection: sqlite3.Connection,
        *,
        chat_id: int,
        session_key: str,
        session_id: str,
        prompt: str,
        queue_id: str | None,
        started_at: float | None = None,
    ) -> DurableTurn:
        turn_id = self._new_id(connection)
        now = self._timestamp()
        logical_started_at = now if started_at is None else started_at
        connection.execute(
            """
            INSERT INTO turns (
                id, chat_id, session_key, session_id, prompt, status,
                partial_text, started_at, updated_at, queue_id
            ) VALUES (?, ?, ?, ?, ?, 'running', '', ?, ?, ?)
            """,
            (
                turn_id,
                chat_id,
                session_key,
                session_id,
                prompt,
                logical_started_at,
                now,
                queue_id,
            ),
        )
        return DurableTurn(
            turn_id,
            chat_id,
            session_key,
            session_id,
            prompt,
            "running",
            "",
            logical_started_at,
            now,
            queue_id,
        )

    def _transition_queue(self, chat_id: int, queue_id: str, *, target: str) -> None:
        chat = _chat_id(chat_id)
        item_id = _entry_id(queue_id)
        with closing(self._connect(create=False)) as connection:
            with connection:
                cursor = connection.execute(
                    """
                    UPDATE queue_items SET status = ?
                    WHERE id = ? AND chat_id = ? AND status = 'queued'
                    """,
                    (target, item_id, chat),
                )
                if cursor.rowcount != 1:
                    raise DeliveryLedgerError(f"排隊任務不存在或已開始: {item_id}")
                self._record_queue_choice(connection, chat, item_id)

    def _outbox_by_status(
        self,
        chat_id: int,
        status: DeliveryStatus,
    ) -> tuple[OutboxItem, ...]:
        chat = _chat_id(chat_id)
        if not self.path.is_file():
            return ()
        with closing(self._connect(create=False)) as connection:
            if status == "pending":
                rows = connection.execute(
                    """
                    SELECT item.id, item.chat_id, item.turn_id, item.position,
                           item.kind, item.payload, item.status
                    FROM outbox AS item
                    WHERE item.chat_id = ? AND item.status = 'pending'
                      AND NOT EXISTS (
                          SELECT 1 FROM outbox AS blocker
                          WHERE (
                                (blocker.turn_id = item.turn_id
                                 AND item.turn_id IS NOT NULL)
                                OR
                                (blocker.dedupe_key = item.dedupe_key
                                 AND item.dedupe_key IS NOT NULL)
                            )
                            AND blocker.position < item.position
                            AND blocker.status IN ('pending', 'sending', 'unknown')
                      )
                    ORDER BY item.created_at, item.turn_id, item.position, item.id
                    """,
                    (chat,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT id, chat_id, turn_id, position, kind, payload, status
                    FROM outbox WHERE chat_id = ? AND status = ?
                    ORDER BY created_at, turn_id, position, id
                    """,
                    (chat, status),
                ).fetchall()
        return tuple(_outbox_item(row) for row in rows)

    def _insert_delivery_recovery_notice(
        self,
        connection: sqlite3.Connection,
        item: OutboxItem,
        now: float,
    ) -> bool:
        dedupe_key = f"{_RECOVERY_DEDUPE_PREFIX}{item.id}"
        existing = connection.execute(
            "SELECT 1 FROM outbox WHERE dedupe_key = ?",
            (dedupe_key,),
        ).fetchone()
        if existing is not None:
            return False
        notice_id = self._new_id(connection)
        payload = (
            f"⚠️ 回覆 {item.id} 尚未收到送達確認。內容已保留; 為避免重複, 不會自動重送。\n\n"
            f"若未收到: /queue resend {item.id}\n"
            f"若已收到: /queue dismiss {item.id}"
        )
        connection.execute(
            """
            INSERT INTO outbox (
                id, chat_id, turn_id, position, kind, payload,
                status, created_at, updated_at, dedupe_key
            ) VALUES (?, ?, NULL, 0, 'text', ?, 'pending', ?, ?, ?)
            """,
            (notice_id, item.chat_id, payload, now, now, dedupe_key),
        )
        return True

    def _transition_delivery(
        self,
        chat_id: int,
        delivery_id: str,
        *,
        expected: DeliveryStatus,
        target: DeliveryStatus,
        telegram_message_id: int | None = None,
        attempt: DeliveryAttempt | None = None,
    ) -> OutboxItem:
        chat = _chat_id(chat_id)
        item_id = _entry_id(delivery_id)
        with closing(self._connect(create=False)) as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                if expected in {"pending", "sending"}:
                    self._require_delivery_attempt(connection, chat, item_id, attempt)
                cursor = connection.execute(
                    """
                    UPDATE outbox
                    SET status = ?, updated_at = ?, telegram_message_id = ?
                    WHERE id = ? AND chat_id = ? AND status = ?
                    """,
                    (
                        target,
                        self._timestamp(),
                        telegram_message_id,
                        item_id,
                        chat,
                        expected,
                    ),
                )
                if cursor.rowcount != 1:
                    raise DeliveryLedgerError(
                        f"可靠回覆 {item_id} 目前不可由 {expected} 轉為 {target}"
                    )
                row = connection.execute(
                    """
                    SELECT id, chat_id, turn_id, position, kind, payload, status
                    FROM outbox WHERE id = ?
                    """,
                    (item_id,),
                ).fetchone()
        if row is None:
            raise DeliveryLedgerError("可靠回覆狀態遺失")
        return _outbox_item(row)

    @staticmethod
    def _require_delivery_attempt(
        connection: sqlite3.Connection, chat_id: int, delivery_id: str,
        attempt: DeliveryAttempt | None,
    ) -> None:
        row = connection.execute(
            "SELECT epoch FROM delivery_attempts WHERE delivery_id = ?", (delivery_id,),
        ).fetchone()
        if row is None and attempt is None:
            return  # Legacy unclaimed outbox only; not an escape hatch for a claimed attempt.
        if (row is None or attempt is None or attempt.item.id != delivery_id
                or attempt.item.chat_id != chat_id or attempt.epoch != int(row[0])):
            raise DeliveryAttemptFencedError("delivery attempt authority has been revoked")

    def _turn_row(self, connection: sqlite3.Connection, turn_id: str) -> tuple[object, ...]:
        row = connection.execute(
            """
            SELECT id, chat_id, session_key, session_id, prompt, status,
                   partial_text, started_at, updated_at, queue_id
            FROM turns WHERE id = ?
            """,
            (turn_id,),
        ).fetchone()
        if row is None:
            raise DeliveryLedgerError(f"執行記錄不存在: {turn_id}")
        return cast(tuple[object, ...], row)

    def _new_id(self, connection: sqlite3.Connection) -> str:
        for _ in range(40):
            try:
                candidate = self._id_factory()
            except (StopIteration, RuntimeError) as error:
                raise DeliveryLedgerError("無法產生可靠狀態 ID") from error
            if _ID.fullmatch(candidate) is None:
                continue
            exists = any(
                connection.execute(
                    f"SELECT 1 FROM {table} WHERE id = ?",
                    (candidate,),
                ).fetchone()
                is not None
                for table in ("queue_items", "turns", "outbox")
            )
            if not exists:
                return candidate
        raise DeliveryLedgerError("無法產生唯一可靠狀態 ID")

    @staticmethod
    def _next_priority(connection: sqlite3.Connection, chat_id: int) -> int:
        """Return a rank above every queued item in one chat.

        Older ledgers treated ``priority`` as a boolean.  Keeping zero as the
        FIFO value and assigning monotonically increasing positive ranks makes
        an explicit insertion deterministic even when several priority items
        already exist, while remaining compatible with those ledgers.
        """

        row = connection.execute(
            """
            SELECT COALESCE(MAX(priority), 0) + 1
            FROM queue_items
            WHERE chat_id = ? AND status = 'queued'
            """,
            (chat_id,),
        ).fetchone()
        if row is None or not isinstance(row[0], int | float) or isinstance(row[0], bool):
            raise DeliveryLedgerError("排隊優先序狀態無效")
        try:
            rank = int(row[0])
        except (OverflowError, ValueError):
            raise DeliveryLedgerError("排隊優先序狀態無效") from None
        if rank <= 0 or rank >= 2**63:
            raise DeliveryLedgerError("排隊優先序已超出安全範圍")
        return rank

    def _timestamp(self) -> float:
        value = self._clock()
        if not isinstance(value, int | float) or isinstance(value, bool) or value < 0:
            raise DeliveryLedgerError("可靠狀態時間無效")
        return float(value)

    def _connect(self, *, create: bool) -> sqlite3.Connection:
        if self.path.is_symlink() or (self.path.exists() and not self.path.is_file()):
            raise DeliveryLedgerError("可靠交付資料庫路徑不安全")
        if create:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self.root.is_symlink() or not self.root.is_dir():
                raise DeliveryLedgerError("可靠交付狀態根目錄不安全")
            self.root.chmod(0o700)
        try:
            connection = sqlite3.connect(self.path, timeout=5.0)
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS queue_items (
                    id TEXT PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    session_key TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    priority INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    turn_id TEXT,
                    mode TEXT NOT NULL DEFAULT 'normal',
                    parent_turn_id TEXT,
                    resume_context TEXT
                );
                CREATE INDEX IF NOT EXISTS queue_chat_status
                    ON queue_items(chat_id, status, priority, created_at);
                CREATE TABLE IF NOT EXISTS queue_handoffs (
                    queue_id TEXT PRIMARY KEY, chat_id INTEGER NOT NULL,
                    parent_turn_id TEXT NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL,
                    resume_queue_id TEXT, prompt TEXT NOT NULL, resume_context TEXT,
                    priority INTEGER NOT NULL, notified INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS queue_choices (
                    chat_id INTEGER NOT NULL, queue_id TEXT NOT NULL,
                    PRIMARY KEY(chat_id, queue_id)
                );
                CREATE TABLE IF NOT EXISTS turns (
                    id TEXT PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    session_key TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    status TEXT NOT NULL,
                    partial_text TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    queue_id TEXT,
                    recovery_notified INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS turns_chat_status
                    ON turns(chat_id, status, started_at);
                CREATE TABLE IF NOT EXISTS execution_owners (
                    turn_id TEXT PRIMARY KEY REFERENCES turns(id),
                    owner_id TEXT NOT NULL,
                    epoch INTEGER NOT NULL CHECK (epoch > 0),
                    state TEXT NOT NULL CHECK (state IN ('running', 'stopping', 'retired')),
                    runtime_generation TEXT,
                    sequence INTEGER
                );
                CREATE TABLE IF NOT EXISTS execution_checkpoints (
                    turn_id TEXT PRIMARY KEY REFERENCES execution_owners(turn_id),
                    producer_epoch INTEGER NOT NULL,
                    runtime_generation TEXT,
                    sequence INTEGER,
                    phase TEXT NOT NULL,
                    partial_hash TEXT NOT NULL,
                    revision INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS outbox (
                    id TEXT PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    turn_id TEXT,
                    position INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    telegram_message_id INTEGER,
                    dedupe_key TEXT,
                    UNIQUE(turn_id, position)
                );
                CREATE INDEX IF NOT EXISTS outbox_chat_status
                    ON outbox(chat_id, status, created_at, position);
                CREATE TABLE IF NOT EXISTS delivery_attempts (
                    delivery_id TEXT PRIMARY KEY REFERENCES outbox(id),
                    epoch INTEGER NOT NULL CHECK (epoch > 0)
                );
                """
            )
            columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(outbox)")
            }
            if "dedupe_key" not in columns:
                connection.execute("ALTER TABLE outbox ADD COLUMN dedupe_key TEXT")
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS outbox_dedupe_position
                ON outbox(dedupe_key, position)
                WHERE dedupe_key IS NOT NULL
                """
            )
            self._migrate_queue_columns(connection)
            connection.commit()
        except sqlite3.Error as error:
            raise DeliveryLedgerError("SQLite 可靠交付狀態不可用") from error
        if create:
            self._secure()
        return connection

    @staticmethod
    def _migrate_queue_columns(connection: sqlite3.Connection) -> None:
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(queue_items)")
        }
        if "mode" not in columns:
            connection.execute(
                "ALTER TABLE queue_items ADD COLUMN mode TEXT NOT NULL DEFAULT 'normal'"
            )
        if "parent_turn_id" not in columns:
            connection.execute("ALTER TABLE queue_items ADD COLUMN parent_turn_id TEXT")
        if "resume_context" not in columns:
            connection.execute("ALTER TABLE queue_items ADD COLUMN resume_context TEXT")

    def _connect_read_only(self) -> sqlite3.Connection:
        """Open an existing ledger without schema creation or journal writes."""

        if self.path.is_symlink() or not self.path.is_file():
            raise DeliveryLedgerError("可靠交付資料庫路徑不安全")
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
            raise DeliveryLedgerError("SQLite 可靠交付狀態不可讀") from error

    def _secure(self) -> None:
        self.root.chmod(0o700)
        os.chmod(self.path, 0o600)


def _chat_id(value: int) -> int:
    if value == 0 or isinstance(value, bool):
        raise DeliveryLedgerError("Telegram chat ID 無效")
    return value


def _execution_epoch(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < 2**63:
        raise DeliveryLedgerError("execution epoch is invalid")
    return value


def _session_key(value: str) -> str:
    normalized = value.strip()
    if _SESSION_KEY.fullmatch(normalized) is None:
        raise DeliveryLedgerError("可靠任務的會話 key 無效")
    return normalized


def _session_id(value: str) -> str:
    normalized = value.strip()
    if _SESSION_ID.fullmatch(normalized) is None:
        raise DeliveryLedgerError("可靠任務的 runner session ID 無效")
    return normalized


def _entry_id(value: str) -> str:
    normalized = value.strip().casefold()
    if _ID.fullmatch(normalized) is None:
        raise DeliveryLedgerError("可靠狀態 ID 無效")
    return normalized


def _prompt(value: str, *, allow_sensitive: bool = False) -> str:
    normalized = value.strip()
    if not 1 <= len(normalized) <= _MAX_PROMPT_CHARS:
        raise DeliveryLedgerError("持久任務內容必須是 1-12000 個字元")
    if any(
        (ord(character) < 32 and character not in "\n\t") or ord(character) == 127
        for character in normalized
    ):
        raise DeliveryLedgerError("持久任務內容含有不安全控制字元")
    redacted = redact_sensitive_text(normalized)
    if redacted != normalized and not allow_sensitive:
        raise DeliveryLedgerError("疑似包含憑據, 不寫入持久任務佇列")
    return redacted if allow_sensitive else normalized


def _queue_mode(value: str) -> QueueMode:
    normalized = value.strip().casefold()
    if normalized not in _QUEUE_MODES:
        raise DeliveryLedgerError("排隊模式無效")
    return cast(QueueMode, normalized)


def _optional_entry_id(value: str | None, *, label: str) -> str | None:
    if value is None:
        return None
    normalized = value.strip().casefold()
    if _ID.fullmatch(normalized) is None:
        raise DeliveryLedgerError(f"{label}無效")
    return normalized


def _queue_context(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = redact_sensitive_text(value).strip()
    if not normalized:
        raise DeliveryLedgerError("續接上下文不能為空")
    if len(normalized) > _MAX_QUEUE_CONTEXT_CHARS:
        normalized = normalized[: _MAX_QUEUE_CONTEXT_CHARS - 1] + "…"
    if any(
        (ord(character) < 32 and character not in "\n\t") or ord(character) == 127
        for character in normalized
    ):
        raise DeliveryLedgerError("續接上下文含有不安全控制字元")
    return normalized


def _partial(value: str) -> str:
    redacted = redact_sensitive_text(value).strip()
    if len(redacted) <= _MAX_PARTIAL_CHARS:
        return redacted
    return redacted[: _MAX_PARTIAL_CHARS - 1] + "…"


def _queued_task(row: tuple[object, ...]) -> QueuedTask:
    return QueuedTask(
        id=str(row[0]),
        chat_id=cast(int, row[1]),
        session_key=str(row[2]),
        prompt=str(row[3]),
        created_at=cast(float, row[4]),
        priority=cast(int, row[5]),
        mode=cast(QueueMode, str(row[6])) if str(row[6]) in _QUEUE_MODES else "normal",
        parent_turn_id=None if row[7] is None else str(row[7]),
        resume_context=None if row[8] is None else str(row[8]),
    )


def _durable_turn(row: tuple[object, ...]) -> DurableTurn:
    return DurableTurn(
        id=str(row[0]),
        chat_id=cast(int, row[1]),
        session_key=str(row[2]),
        session_id=str(row[3]),
        prompt=str(row[4]),
        status=str(row[5]),
        partial_text=str(row[6]),
        started_at=cast(float, row[7]),
        updated_at=cast(float, row[8]),
        queue_id=None if row[9] is None else str(row[9]),
    )


def _outbox_item(row: tuple[object, ...]) -> OutboxItem:
    return OutboxItem(
        id=str(row[0]),
        chat_id=cast(int, row[1]),
        turn_id=None if row[2] is None else str(row[2]),
        position=cast(int, row[3]),
        kind=cast(DeliveryKind, str(row[4])),
        text=str(row[5]),
        status=cast(DeliveryStatus, str(row[6])),
    )
