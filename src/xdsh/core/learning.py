"""Approval-gated durable learning for runner-neutral product surfaces."""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Literal, Protocol, cast

LearningStatus = Literal["pending", "approved", "rejected", "forgotten"]
MemoryLayer = Literal["user", "memory"]

_VERSION = 2
_LEGACY_VERSION = 1
_ENTRY_ID = re.compile(r"^[a-f0-9]{8}$")
_ORIGIN = re.compile(r"^[a-z_]{1,32}$")
_MAX_TEXT_CHARS = 1_000
_MAX_ENTRIES = 200
_MAX_FUZZY_MEMORY_QUERY_TOKENS = 64
_MAX_FUZZY_MEMORY_COMPARISONS = 64
_MAX_MEMORY_TERM_CHARS = 64
_FUZZY_MEMORY_THRESHOLD = 0.78
_SECRET_VALUE = re.compile(
    r"(?i)\b(?:bearer\s+|gh[opshu]_|github_pat_|sk-(?:ant-)?|AKIA|xoxb-|glpat-)"
    r"[A-Za-z0-9._~+/=-]{6,}"
)
_NAMED_SECRET = re.compile(
    r"(?i)\b[A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD)\s*[=:]\s*[^\s,;]+"
)
_TELEGRAM_TOKEN = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b")
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_INSTRUCTION_INJECTION = re.compile(
    r"(?i)(?:ignore|disregard|override).{0,40}(?:instruction|prompt|policy)|"
    r"(?:system|developer)\s+(?:prompt|message)|<\s*/?\s*(?:system|developer)\b|"
    r"(?:忽略|無視|无视|覆蓋|覆盖).{0,20}(?:指令|提示|規則|规则)|"
    r"(?:系統|系统|開發者|开发者)(?:提示|訊息|消息)"
)
_EXPLICIT_MEMORY_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE | re.DOTALL)
    for pattern in (
        r"^(?:請|请)?(?:記住|记住)(?:這一點|这一点)?\s*[\uFF1A:,\uFF0C]?\s*(.+)$",
        r"^(?:從現在起|从现在起|以後|以后)\s*[\uFF1A:,\uFF0C]?\s*(.+)$",
        r"^(?:please\s+)?remember(?:\s+that)?\s*[\uFF1A:,]?\s*(.+)$",
        r"^from\s+now\s+on\s*[\uFF1A:,]?\s*(.+)$",
    )
)


class LearningError(RuntimeError):
    """A learning request or its durable state is invalid."""


@dataclass(frozen=True, slots=True)
class LearningEntry:
    """One auditable memory proposal and its current approval state."""

    id: str
    subsystem: Literal["memory"]
    action: Literal["add"]
    summary: str
    origin: str
    created_at: str
    layer: MemoryLayer = "user"
    status: LearningStatus = "pending"
    decided_at: str | None = None


@dataclass(frozen=True, slots=True)
class LearningSnapshot:
    """Current learning policy and audit ledger for one chat."""

    auto_capture: bool
    entries: tuple[LearningEntry, ...]
    successful_turns: int = 0
    last_reviewed_turn: int = 0
    successful_tool_iterations: int = 0
    last_skill_reviewed_iteration: int = 0

    @property
    def pending(self) -> tuple[LearningEntry, ...]:
        return tuple(entry for entry in self.entries if entry.status == "pending")

    @property
    def approved(self) -> tuple[LearningEntry, ...]:
        return tuple(entry for entry in self.entries if entry.status == "approved")


@dataclass(frozen=True, slots=True)
class LearningProposal:
    """One bounded proposal returned by an unprivileged background reviewer."""

    summary: str
    layer: MemoryLayer = "user"

    def __post_init__(self) -> None:
        if normalize_memory_text(self.summary) != self.summary:
            raise ValueError("learning proposal must contain normalized memory text")
        if self.layer not in {"user", "memory"}:
            raise ValueError("learning proposal layer is invalid")
        if contains_sensitive_text(self.summary) or contains_instruction_injection(
            self.summary
        ):
            raise ValueError("learning proposal did not pass the safety scan")


class LearningReviewer(Protocol):
    """No-tools reviewer seam used after a successful foreground turn."""

    async def review(
        self,
        *,
        user_text: str,
        final_text: str,
        tool_names: tuple[str, ...],
    ) -> LearningProposal | None: ...


class LearningLedger:
    """Persist memory candidates atomically and apply only approved entries."""

    def __init__(
        self,
        state_root: Path,
        *,
        id_factory: Callable[[], str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        root = state_root.expanduser().resolve()
        if root == Path(root.anchor) or ".xclaude" in root.parts or ".dsh" in root.parts:
            raise LearningError("學習狀態根目錄不安全")
        self.root = root / "learning"
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex[:8])
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def snapshot(self, chat_id: int) -> LearningSnapshot:
        return self._load(chat_id)

    def set_auto_capture(self, chat_id: int, enabled: bool) -> LearningSnapshot:
        current = self._load(chat_id)
        updated = replace(current, auto_capture=enabled)
        self._save(chat_id, updated)
        return updated

    def record_success(self, chat_id: int, *, review_interval: int) -> int | None:
        """Advance the durable cadence and return a due turn number."""

        if not 1 <= review_interval <= 1_000:
            raise ValueError("learning review interval must be between 1 and 1000")
        current = self._load(chat_id)
        updated = replace(current, successful_turns=current.successful_turns + 1)
        self._save(chat_id, updated)
        if (
            updated.auto_capture
            and updated.successful_turns - updated.last_reviewed_turn >= review_interval
        ):
            return updated.successful_turns
        return None

    def mark_reviewed(self, chat_id: int, turn_number: int) -> LearningSnapshot:
        current = self._load(chat_id)
        if not 1 <= turn_number <= current.successful_turns:
            raise LearningError("背景審查回合編號無效")
        updated = replace(
            current,
            last_reviewed_turn=max(current.last_reviewed_turn, turn_number),
        )
        self._save(chat_id, updated)
        return updated

    def record_tool_iterations(
        self,
        chat_id: int,
        *,
        tool_calls: int,
        review_interval: int,
    ) -> int | None:
        """Advance procedural-learning cadence and return a due iteration."""

        if tool_calls < 0:
            raise ValueError("tool call count cannot be negative")
        if not 1 <= review_interval <= 10_000:
            raise ValueError("skill review interval must be between 1 and 10000")
        current = self._load(chat_id)
        updated = replace(
            current,
            successful_tool_iterations=current.successful_tool_iterations + tool_calls,
        )
        self._save(chat_id, updated)
        if (
            updated.auto_capture
            and tool_calls > 0
            and updated.successful_tool_iterations
            - updated.last_skill_reviewed_iteration
            >= review_interval
        ):
            return updated.successful_tool_iterations
        return None

    def mark_skill_reviewed(
        self,
        chat_id: int,
        iteration_number: int,
    ) -> LearningSnapshot:
        current = self._load(chat_id)
        if not 1 <= iteration_number <= current.successful_tool_iterations:
            raise LearningError("Skill 背景審查 iteration 編號無效")
        updated = replace(
            current,
            last_skill_reviewed_iteration=max(
                current.last_skill_reviewed_iteration,
                iteration_number,
            ),
        )
        self._save(chat_id, updated)
        return updated

    def stage(
        self,
        chat_id: int,
        text: str,
        *,
        origin: str,
        layer: MemoryLayer = "user",
    ) -> LearningEntry:
        summary = normalize_memory_text(text)
        if contains_sensitive_text(summary):
            raise LearningError("疑似包含憑據或私密金鑰, 未建立學習候選")
        if contains_instruction_injection(summary):
            raise LearningError("疑似包含指令注入, 未建立學習候選")
        if _ORIGIN.fullmatch(origin) is None:
            raise LearningError("學習來源無效")
        if layer not in {"user", "memory"}:
            raise LearningError("記憶分層無效")
        current = self._load(chat_id)
        duplicate = next(
            (
                entry
                for entry in current.entries
                if entry.status in {"pending", "approved"}
                and entry.summary.casefold() == summary.casefold()
            ),
            None,
        )
        if duplicate is not None:
            raise LearningError(f"相同內容已存在 ({duplicate.id})")
        if len(current.entries) >= _MAX_ENTRIES:
            raise LearningError("學習記錄已達上限, 請先整理舊記錄")
        entry = LearningEntry(
            id=self._new_id(current),
            subsystem="memory",
            action="add",
            summary=summary,
            origin=origin,
            created_at=self._timestamp(),
            layer=layer,
        )
        self._save(chat_id, replace(current, entries=(*current.entries, entry)))
        return entry

    def approve(self, chat_id: int, entry_id: str) -> LearningEntry:
        return self._transition(chat_id, entry_id, expected="pending", target="approved")

    def reject(self, chat_id: int, entry_id: str) -> LearningEntry:
        return self._transition(chat_id, entry_id, expected="pending", target="rejected")

    def forget(self, chat_id: int, entry_id: str) -> LearningEntry:
        return self._transition(chat_id, entry_id, expected="approved", target="forgotten")

    def restore(self, chat_id: int, entry_id: str) -> LearningEntry:
        return self._transition(chat_id, entry_id, expected="forgotten", target="approved")

    def augment_task(
        self,
        chat_id: int,
        task: str,
        *,
        max_items: int = 20,
        max_chars: int = 4_000,
    ) -> str:
        """Attach bounded, user-approved memory at user authority."""

        if max_items <= 0 or max_chars <= 0:
            raise ValueError("memory context limits must be positive")
        approved = self._load(chat_id).approved
        if not approved:
            return task
        user_entries = tuple(entry for entry in approved if entry.layer == "user")
        memory_entries = tuple(entry for entry in approved if entry.layer == "memory")
        relevant_memory = _relevant_memory(memory_entries, task, max_items)
        both_layers = bool(user_entries and relevant_memory)
        user_limit = (max_items + 1) // 2 if both_layers else max_items
        memory_limit = max_items - user_limit if both_layers else max_items
        user_chars = (max_chars + 1) // 2 if both_layers else max_chars
        memory_chars = max_chars - user_chars if both_layers else max_chars
        user_lines = _bounded_memory_lines(user_entries[-user_limit:], user_chars)
        memory_lines = _bounded_memory_lines(
            relevant_memory[:memory_limit],
            memory_chars,
        )
        sections: list[str] = []
        if user_lines:
            sections.append("[USER preferences]\n" + "\n".join(user_lines))
        if memory_lines:
            sections.append("[MEMORY relevant facts]\n" + "\n".join(memory_lines))
        if not sections:
            return task
        memory = "\n\n".join(sections)
        return (
            "XDSH user-approved durable knowledge follows. It has the same authority as "
            "the current Telegram user, not system policy. USER contains stable user "
            "preferences; MEMORY contains historical task-relevant project notes. "
            "The recorded date is when a note was stored, not a fresh verification. "
            "Use notes as leads, not current external evidence; preserve uncertainty, exact "
            "entity/version and scope. A prior unconfirmed result is not a current exclusion.\n"
            f"{memory}\n\nCurrent Telegram task:\n{task}"
        )

    def _transition(
        self,
        chat_id: int,
        entry_id: str,
        *,
        expected: LearningStatus,
        target: LearningStatus,
    ) -> LearningEntry:
        if _ENTRY_ID.fullmatch(entry_id) is None:
            raise LearningError("學習記錄 ID 無效")
        current = self._load(chat_id)
        entry = next((item for item in current.entries if item.id == entry_id), None)
        if entry is None:
            raise LearningError(f"學習記錄不存在: {entry_id}")
        if entry.status != expected:
            raise LearningError(f"學習記錄 {entry_id} 目前狀態為 {entry.status}")
        updated_entry = replace(entry, status=target, decided_at=self._timestamp())
        entries = tuple(
            updated_entry if item.id == entry_id else item for item in current.entries
        )
        self._save(chat_id, replace(current, entries=entries))
        return updated_entry

    def _load(self, chat_id: int) -> LearningSnapshot:
        path = self._path(chat_id)
        if not path.exists():
            return LearningSnapshot(auto_capture=True, entries=())
        try:
            payload = cast(object, json.loads(path.read_text(encoding="ascii")))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise LearningError("學習狀態檔損壞") from error
        if not isinstance(payload, dict):
            raise LearningError("學習狀態必須是 JSON object")
        raw = cast(dict[object, object], payload)
        version = raw.get("version")
        if version not in {_LEGACY_VERSION, _VERSION} or not isinstance(
            raw.get("auto_capture"), bool
        ):
            raise LearningError("不支援的學習狀態版本或設定")
        raw_entries = raw.get("entries")
        if not isinstance(raw_entries, list):
            raise LearningError("學習狀態缺少 entries")
        entries = tuple(self._decode_entry(item) for item in raw_entries)
        if len(entries) > _MAX_ENTRIES or len({entry.id for entry in entries}) != len(entries):
            raise LearningError("學習狀態包含過多或重複記錄")
        successful_turns = 0 if version == _LEGACY_VERSION else raw.get("successful_turns")
        last_reviewed_turn = (
            0 if version == _LEGACY_VERSION else raw.get("last_reviewed_turn")
        )
        successful_tool_iterations = (
            0
            if version == _LEGACY_VERSION
            else raw.get("successful_tool_iterations", 0)
        )
        last_skill_reviewed_iteration = (
            0
            if version == _LEGACY_VERSION
            else raw.get("last_skill_reviewed_iteration", 0)
        )
        if (
            not isinstance(successful_turns, int)
            or isinstance(successful_turns, bool)
            or not isinstance(last_reviewed_turn, int)
            or isinstance(last_reviewed_turn, bool)
            or successful_turns < 0
            or not 0 <= last_reviewed_turn <= successful_turns
            or not isinstance(successful_tool_iterations, int)
            or isinstance(successful_tool_iterations, bool)
            or not isinstance(last_skill_reviewed_iteration, int)
            or isinstance(last_skill_reviewed_iteration, bool)
            or successful_tool_iterations < 0
            or not 0
            <= last_skill_reviewed_iteration
            <= successful_tool_iterations
        ):
            raise LearningError("學習審查計數無效")
        snapshot = LearningSnapshot(
            cast(bool, raw["auto_capture"]),
            entries,
            successful_turns,
            last_reviewed_turn,
            successful_tool_iterations,
            last_skill_reviewed_iteration,
        )
        if version == _LEGACY_VERSION or (
            "successful_tool_iterations" not in raw
            or "last_skill_reviewed_iteration" not in raw
            or any(
                isinstance(item, dict) and "layer" not in item
                for item in raw_entries
            )
        ):
            self._save(chat_id, snapshot)
        return snapshot

    def _decode_entry(self, value: object) -> LearningEntry:
        if not isinstance(value, dict):
            raise LearningError("學習記錄格式無效")
        raw = cast(dict[object, object], value)
        entry_id = raw.get("id")
        subsystem = raw.get("subsystem")
        action = raw.get("action")
        summary = raw.get("summary")
        origin = raw.get("origin")
        created_at = raw.get("created_at")
        layer = raw.get("layer", "user")
        status = raw.get("status")
        decided_at = raw.get("decided_at")
        if not all(
            isinstance(item, str)
            for item in (
                entry_id,
                subsystem,
                action,
                summary,
                origin,
                created_at,
                status,
                layer,
            )
        ):
            raise LearningError("學習記錄欄位無效")
        if decided_at is not None and not isinstance(decided_at, str):
            raise LearningError("學習記錄決策時間無效")
        if (
            _ENTRY_ID.fullmatch(cast(str, entry_id)) is None
            or subsystem != "memory"
            or action != "add"
            or _ORIGIN.fullmatch(cast(str, origin)) is None
            or status not in {"pending", "approved", "rejected", "forgotten"}
            or layer not in {"user", "memory"}
            or normalize_memory_text(cast(str, summary)) != summary
            or contains_sensitive_text(cast(str, summary))
            or contains_instruction_injection(cast(str, summary))
        ):
            raise LearningError("學習記錄內容無效")
        return LearningEntry(
            id=cast(str, entry_id),
            subsystem="memory",
            action="add",
            summary=cast(str, summary),
            origin=cast(str, origin),
            created_at=cast(str, created_at),
            layer=cast(MemoryLayer, layer),
            status=cast(LearningStatus, status),
            decided_at=decided_at,
        )

    def _new_id(self, snapshot: LearningSnapshot) -> str:
        existing = {entry.id for entry in snapshot.entries}
        for _ in range(20):
            try:
                candidate = self._id_factory()
            except (StopIteration, RuntimeError) as error:
                raise LearningError("無法產生學習記錄 ID") from error
            if _ENTRY_ID.fullmatch(candidate) is not None and candidate not in existing:
                return candidate
        raise LearningError("無法產生唯一的學習記錄 ID")

    def _timestamp(self) -> str:
        value = self._clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _save(self, chat_id: int, snapshot: LearningSnapshot) -> None:
        payload = {
            "version": _VERSION,
            "auto_capture": snapshot.auto_capture,
            "successful_turns": snapshot.successful_turns,
            "last_reviewed_turn": snapshot.last_reviewed_turn,
            "successful_tool_iterations": snapshot.successful_tool_iterations,
            "last_skill_reviewed_iteration": snapshot.last_skill_reviewed_iteration,
            "entries": [
                {
                    "id": entry.id,
                    "subsystem": entry.subsystem,
                    "action": entry.action,
                    "summary": entry.summary,
                    "origin": entry.origin,
                    "created_at": entry.created_at,
                    "layer": entry.layer,
                    "status": entry.status,
                    "decided_at": entry.decided_at,
                }
                for entry in snapshot.entries
            ],
        }
        content = json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n"
        self._prepare()
        destination = self._path(chat_id)
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="ascii") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _prepare(self) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)

    def _path(self, chat_id: int) -> Path:
        if chat_id == 0:
            raise LearningError("Telegram chat ID 不能為零")
        return self.root / f"{chat_id}.json"


def extract_durable_memory_request(text: str) -> str | None:
    """Recognize an unambiguous durable write request before any model call."""
    match = re.fullmatch(
        r"(?:请|請)?(?:永久记住|永久記住|持久保存|持久化保存)\s*[\uff1a:]\s*(.+)"
        r"|(?:please\s+)?remember\s+permanently\s*:\s*(.+)",
        text.strip(), re.IGNORECASE | re.DOTALL,
    )
    if match is None:
        return None
    return normalize_memory_text(match.group(1) or match.group(2))


def extract_explicit_memory(text: str) -> str | None:
    """Extract only an explicit user request to remember durable information."""

    stripped = text.strip()
    for pattern in _EXPLICIT_MEMORY_PATTERNS:
        match = pattern.fullmatch(stripped)
        if match is None:
            continue
        try:
            return normalize_memory_text(match.group(1))
        except LearningError:
            return None
    return None


def _bounded_memory_lines(
    entries: tuple[LearningEntry, ...],
    max_chars: int,
) -> list[str]:
    if max_chars <= 0:
        return []
    lines: list[str] = []
    used = 0
    for entry in entries:
        stamp = f" recorded={entry.created_at[:10]}" if entry.layer == "memory" else ""
        line = f"- [{entry.id}{stamp}] {entry.summary}"
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
    return lines


def _relevant_memory(
    entries: tuple[LearningEntry, ...],
    task: str,
    limit: int,
) -> tuple[LearningEntry, ...]:
    if limit <= 0:
        return ()
    task_tokens = _memory_tokens(task)
    ranked: list[tuple[int, int, float, int, LearningEntry]] = []
    for index, entry in enumerate(entries):
        entry_tokens = _memory_tokens(entry.summary)
        exact_score = len(task_tokens.intersection(entry_tokens))
        fuzzy_score = _fuzzy_memory_score(task_tokens, entry_tokens)
        if exact_score or fuzzy_score:
            ranked.append((exact_score, int(fuzzy_score > 0), fuzzy_score, index, entry))
    ranked.sort(key=lambda item: (-item[0], -item[1], -item[2], -item[3]))
    selected = sorted(ranked[:limit], key=lambda item: item[3])
    return tuple(item[4] for item in selected)


def _memory_tokens(text: str) -> set[str]:
    latin = [
        value[:_MAX_MEMORY_TERM_CHARS]
        for value in re.findall(r"[a-z0-9_]{2,}", text.casefold())
    ]
    cjk = re.findall(r"[\u3400-\u9fff]{2,}", text)
    pairs = [
        span[index : index + 2]
        for span in cjk
        for index in range(len(span) - 1)
    ]
    return set((*latin, *pairs))


def _fuzzy_memory_score(task_tokens: set[str], entry_tokens: set[str]) -> float:
    """Return bounded typo-tolerant coverage for approved memory entries.

    This is deliberately lexical rather than semantic: it handles small
    spelling/inflection differences without adding a model, network call, or
    unbounded candidate scan.  Short CJK bigrams are exact-only to avoid noisy
    one-character matches.
    """

    if not task_tokens or not entry_tokens:
        return 0.0
    score = 0.0
    query_tokens = sorted(task_tokens, key=lambda value: (-len(value), value))[
        :_MAX_FUZZY_MEMORY_QUERY_TOKENS
    ]
    for task_token in query_tokens:
        if task_token in entry_tokens or len(task_token) < 3:
            continue
        candidates = sorted(
            (
                entry_token
                for entry_token in entry_tokens
                if len(entry_token) >= 3
                and entry_token[0] == task_token[0]
                and abs(len(entry_token) - len(task_token))
                <= max(3, min(len(entry_token), len(task_token)) // 3)
            ),
            key=lambda value: (abs(len(task_token) - len(value)), value),
        )[:_MAX_FUZZY_MEMORY_COMPARISONS]
        best = max(
            (
                SequenceMatcher(None, task_token, entry_token, autojunk=False).ratio()
                for entry_token in candidates
            ),
            default=0.0,
        )
        if best >= _FUZZY_MEMORY_THRESHOLD:
            score += best
    # A single approximate token is useful for a short task, but multi-token
    # tasks need two independent matches before a memory fact is injected.
    minimum = 1 if len(task_tokens) <= 2 else 2
    return score if score >= minimum * _FUZZY_MEMORY_THRESHOLD else 0.0


def normalize_memory_text(text: str) -> str:
    normalized = " ".join(text.split())
    if not 1 <= len(normalized) <= _MAX_TEXT_CHARS:
        raise LearningError(f"記憶內容必須是 1-{_MAX_TEXT_CHARS} 個字元")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise LearningError("記憶內容含有控制字元")
    return normalized


def contains_sensitive_text(text: str) -> bool:
    """Reject common credential shapes instead of persisting or echoing them."""

    return any(
        pattern.search(text) is not None
        for pattern in (_SECRET_VALUE, _NAMED_SECRET, _TELEGRAM_TOKEN, _PRIVATE_KEY)
    )


def contains_instruction_injection(text: str) -> bool:
    """Fail closed on common attempts to turn memory into higher-authority policy."""

    return _INSTRUCTION_INJECTION.search(text) is not None
