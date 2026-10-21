"""Read-only Skill curation and approval-gated archival.

The curator turns observed Skill outcomes into bounded suggestions.  It never
edits a Skill, removes a file, or treats a low success rate as proof of
causality.  An operator can explicitly stage and approve an archive move; the
move is atomic, retained on disk, and reversible after a hash re-check.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, cast

from xdsh.core.learning import contains_instruction_injection, contains_sensitive_text
from xdsh.core.metrics import SkillOutcomeSummary
from xdsh.core.skills import BUILTIN_SKILLS

SkillCurationAction = Literal["review", "archive"]
SkillArchiveStatus = Literal["pending", "approved", "rejected", "stale", "rolled_back"]

_VERSION = 1
_SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
_ENTRY_ID = re.compile(r"^[a-f0-9]{8}$")
_RELATIVE_FILE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}(?:/[A-Za-z0-9][A-Za-z0-9._-]{0,127})*$"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_ARCHIVE_CANDIDATES = 100
_MAX_ARCHIVE_FILES = 128
_MAX_ARCHIVE_BYTES = 1_000_000
_MAX_REASON_CHARS = 300
_MAX_AUDIT_FILES = 2_000
_MAX_STATE_BYTES = 64_000


class SkillCurationError(RuntimeError):
    """A curation suggestion or archive operation is invalid or unsafe."""


@dataclass(frozen=True, slots=True)
class SkillCurationSuggestion:
    """Content-free recommendation derived from bounded outcome metrics."""

    name: str
    action: SkillCurationAction
    reason: str
    runs: int
    succeeded: int
    success_rate: float | None
    last_used_at: float | None

    def __post_init__(self) -> None:
        if _SKILL_NAME.fullmatch(self.name) is None or self.name in BUILTIN_SKILLS:
            raise ValueError("curation Skill name is invalid")
        if self.action not in {"review", "archive"}:
            raise ValueError("curation action is invalid")
        if not 1 <= len(self.reason) <= _MAX_REASON_CHARS:
            raise ValueError("curation reason is invalid")
        if "\n" in self.reason or "\r" in self.reason:
            raise ValueError("curation reason must be a single line")
        if (
            isinstance(self.runs, bool)
            or isinstance(self.succeeded, bool)
            or not isinstance(self.runs, int)
            or not isinstance(self.succeeded, int)
            or self.runs < 0
            or self.succeeded < 0
            or self.succeeded > self.runs
        ):
            raise ValueError("curation counts are invalid")
        if self.success_rate is not None and not _finite_number(
            self.success_rate,
            minimum=0.0,
            maximum=1.0,
        ):
            raise ValueError("curation success rate is invalid")
        if self.last_used_at is not None and not _finite_number(
            self.last_used_at,
            minimum=0.0,
        ):
            raise ValueError("curation timestamp is invalid")


class SkillCurator:
    """Produce deterministic, non-mutating archive/review recommendations."""

    def __init__(
        self,
        *,
        stale_after_seconds: float = 30 * 86_400,
        low_success_rate: float = 0.40,
        minimum_runs: int = 3,
        max_suggestions: int = 20,
    ) -> None:
        if (
            not isinstance(stale_after_seconds, int | float)
            or isinstance(stale_after_seconds, bool)
            or not math.isfinite(float(stale_after_seconds))
            or not 86_400 <= stale_after_seconds <= 365 * 86_400
        ):
            raise ValueError("Skill stale window is out of bounds")
        if (
            not isinstance(low_success_rate, int | float)
            or isinstance(low_success_rate, bool)
            or not math.isfinite(float(low_success_rate))
            or not 0.0 <= low_success_rate <= 1.0
        ):
            raise ValueError("Skill low-success threshold is out of bounds")
        if (
            isinstance(minimum_runs, bool)
            or not isinstance(minimum_runs, int)
            or not 1 <= minimum_runs <= 100
        ):
            raise ValueError("Skill minimum run count is out of bounds")
        if (
            isinstance(max_suggestions, bool)
            or not isinstance(max_suggestions, int)
            or not 1 <= max_suggestions <= 100
        ):
            raise ValueError("Skill suggestion bound is out of bounds")
        self.stale_after_seconds = stale_after_seconds
        self.low_success_rate = low_success_rate
        self.minimum_runs = minimum_runs
        self.max_suggestions = max_suggestions

    def suggest(
        self,
        skill_names: Iterable[str],
        outcomes: Iterable[SkillOutcomeSummary],
        *,
        now: float,
    ) -> tuple[SkillCurationSuggestion, ...]:
        """Return stable suggestions from metrics; never reads task content."""

        if (
            isinstance(now, bool)
            or not isinstance(now, int | float)
            or not math.isfinite(float(now))
            or now < 0
        ):
            raise ValueError("curation clock cannot be negative")
        names: set[str] = set()
        for raw_name in skill_names:
            if not isinstance(raw_name, str):
                raise ValueError("curation Skill name must be text")
            name = raw_name.strip().casefold()
            if _SKILL_NAME.fullmatch(name) is None:
                raise ValueError("curation Skill name is invalid")
            if name not in BUILTIN_SKILLS:
                names.add(name)
        by_name: dict[str, SkillOutcomeSummary] = {}
        for outcome in outcomes:
            if (
                isinstance(outcome, SkillOutcomeSummary)
                and _SKILL_NAME.fullmatch(outcome.skill) is not None
                and outcome.skill not in BUILTIN_SKILLS
                and outcome.skill in names
                and _valid_outcome(outcome)
            ):
                by_name[outcome.skill] = outcome
        suggestions: list[SkillCurationSuggestion] = []
        for name in sorted(names):
            metric = by_name.get(name)
            if metric is None:
                continue
            age = max(0.0, now - metric.last_used_at)
            if age >= self.stale_after_seconds:
                suggestions.append(
                    SkillCurationSuggestion(
                        name=name,
                        action="archive",
                        reason=(
                            f"未使用約 {int(age // 86_400)} 天; 建議先歸檔保留, "
                            "需要時可回滾。"
                        ),
                        runs=metric.runs,
                        succeeded=metric.succeeded,
                        success_rate=metric.success_rate,
                        last_used_at=metric.last_used_at,
                    )
                )
            elif (
                metric.runs >= self.minimum_runs
                and metric.success_rate <= self.low_success_rate
            ):
                suggestions.append(
                    SkillCurationSuggestion(
                        name=name,
                        action="review",
                        reason=(
                            f"近 {metric.runs} 次成功率 {metric.success_rate * 100:.0f}%; "
                            "僅建議人工檢視, 不代表 Skill 造成失敗。"
                        ),
                        runs=metric.runs,
                        succeeded=metric.succeeded,
                        success_rate=metric.success_rate,
                        last_used_at=metric.last_used_at,
                    )
                )
        return tuple(suggestions[: self.max_suggestions])


@dataclass(frozen=True, slots=True)
class SkillArchiveCandidate:
    """One explicit archive proposal with a before-state tree fingerprint."""

    id: str
    name: str
    workspace: str
    before_sha256: str
    file_count: int
    total_bytes: int
    reason: str
    created_at: str
    status: SkillArchiveStatus = "pending"
    decided_at: str | None = None


@dataclass(frozen=True, slots=True)
class SkillArchiveSnapshot:
    entries: tuple[SkillArchiveCandidate, ...]

    @property
    def pending(self) -> tuple[SkillArchiveCandidate, ...]:
        return tuple(item for item in self.entries if item.status == "pending")

    @property
    def archived(self) -> tuple[SkillArchiveCandidate, ...]:
        return tuple(item for item in self.entries if item.status == "approved")


@dataclass(frozen=True, slots=True)
class SkillArchiveMutationResult:
    candidate: SkillArchiveCandidate
    active_target: Path
    archive_target: Path


@dataclass(frozen=True, slots=True)
class _TreeInfo:
    digest: str
    file_count: int
    total_bytes: int


class SkillArchiveStore:
    """Persist archive proposals and move project Skills only after approval."""

    def __init__(
        self,
        state_root: Path,
        *,
        id_factory: Callable[[], str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        root = state_root.expanduser().resolve()
        if root == Path(root.anchor) or ".xclaude" in root.parts or ".dsh" in root.parts:
            raise SkillCurationError("Skill 歸檔狀態根目錄不安全")
        self.root = root / "skill-archive"
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex[:8])
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def snapshot(self, chat_id: int) -> SkillArchiveSnapshot:
        entries = tuple(
            sorted(
                (self._load_candidate(path) for path in self._candidate_paths(chat_id)),
                key=lambda item: (item.created_at, item.id),
            )
        )
        return SkillArchiveSnapshot(entries)

    def stage(
        self,
        chat_id: int,
        *,
        workspace: Path,
        name: str,
        reason: str = "人工要求保留並移出目前 Skill 清單",
    ) -> SkillArchiveCandidate:
        _validate_chat_id(chat_id)
        normalized_name = _normalize_name(name)
        if normalized_name in BUILTIN_SKILLS:
            raise SkillCurationError("XDSH 內置 Skill 不可歸檔")
        resolved_workspace = _safe_workspace(workspace)
        normalized_reason = _normalize_reason(reason)
        existing = self.snapshot(chat_id)
        if len(existing.entries) >= _MAX_ARCHIVE_CANDIDATES:
            raise SkillCurationError("Skill 歸檔候選已達上限")
        if any(
            item.name == normalized_name and item.status == "pending"
            for item in existing.entries
        ):
            raise SkillCurationError(f"Skill {normalized_name} 已有待批准歸檔候選")
        target = _active_skill_root(resolved_workspace, normalized_name)
        tree = _scan_tree(target)
        candidate = SkillArchiveCandidate(
            id=self._new_id(existing),
            name=normalized_name,
            workspace=str(resolved_workspace),
            before_sha256=tree.digest,
            file_count=tree.file_count,
            total_bytes=tree.total_bytes,
            reason=normalized_reason,
            created_at=self._timestamp(),
        )
        self._save_candidate(chat_id, candidate, create=True)
        self._write_audit(chat_id, candidate, event="staged")
        return candidate

    def diff(self, chat_id: int, entry_id: str) -> str:
        candidate = self._get(chat_id, entry_id)
        archive_name = f"{candidate.name}-{candidate.id}"
        return (
            f"Skill archive candidate {candidate.id}\n"
            f"action: move {candidate.name} to .xdsh/skills-archive/{archive_name}\n"
            f"files: {candidate.file_count} · bytes: {candidate.total_bytes}\n"
            f"before sha256: {candidate.before_sha256}\n"
            f"reason: {candidate.reason}\n"
            "No file is changed until explicit approval."
        )

    def approve(
        self,
        chat_id: int,
        entry_id: str,
        *,
        workspace: Path,
    ) -> SkillArchiveMutationResult:
        candidate = self._get(chat_id, entry_id)
        if candidate.status != "pending":
            raise SkillCurationError(f"Skill 歸檔候選 {candidate.id} 目前狀態為 {candidate.status}")
        resolved_workspace = _safe_workspace(workspace)
        if str(resolved_workspace) != candidate.workspace:
            raise SkillCurationError("Skill 歸檔候選不屬於目前工作區")
        active = _active_skill_root(resolved_workspace, candidate.name)
        current = _scan_tree(active)
        if current.digest != candidate.before_sha256:
            stale = replace(candidate, status="stale", decided_at=self._timestamp())
            self._save_candidate(chat_id, stale)
            self._write_audit(chat_id, stale, event="stale")
            raise SkillCurationError("Skill 在候選建立後變更; 候選已標記 stale")
        archive_root = _archive_root(resolved_workspace)
        destination = archive_root / f"{candidate.name}-{candidate.id}"
        if destination.exists() or destination.is_symlink():
            raise SkillCurationError("Skill 歸檔目標已存在")
        self._write_audit(chat_id, candidate, event="archive_prepared")
        moved = False
        persisted = False
        try:
            os.replace(active, destination)
            moved = True
            approved = replace(candidate, status="approved", decided_at=self._timestamp())
            self._save_candidate(chat_id, approved)
            persisted = True
            self._write_audit(chat_id, approved, event="archived")
        except (OSError, SkillCurationError) as error:
            if moved:
                try:
                    os.replace(destination, active)
                except OSError:
                    pass
            if persisted:
                try:
                    self._save_candidate(chat_id, candidate)
                    self._write_audit(chat_id, candidate, event="archive_rolled_back")
                except (OSError, SkillCurationError):
                    pass
            raise SkillCurationError("Skill 歸檔失敗; 已嘗試恢復 active tree") from error
        return SkillArchiveMutationResult(approved, active, destination)

    def reject(self, chat_id: int, entry_id: str) -> SkillArchiveCandidate:
        candidate = self._get(chat_id, entry_id)
        if candidate.status != "pending":
            raise SkillCurationError(f"Skill 歸檔候選 {candidate.id} 目前狀態為 {candidate.status}")
        rejected = replace(candidate, status="rejected", decided_at=self._timestamp())
        self._save_candidate(chat_id, rejected)
        self._write_audit(chat_id, rejected, event="rejected")
        return rejected

    def rollback(
        self,
        chat_id: int,
        entry_id: str,
        *,
        workspace: Path,
    ) -> SkillArchiveMutationResult:
        candidate = self._get(chat_id, entry_id)
        if candidate.status != "approved":
            raise SkillCurationError("只有已批准的 Skill 歸檔可以回復")
        resolved_workspace = _safe_workspace(workspace)
        if str(resolved_workspace) != candidate.workspace:
            raise SkillCurationError("Skill 歸檔候選不屬於目前工作區")
        active = _active_skill_path(resolved_workspace, candidate.name)
        if active.exists() or active.is_symlink():
            raise SkillCurationError("active Skill 已存在; 為避免覆寫, 拒絕回復")
        archive_target = _archive_root(resolved_workspace) / f"{candidate.name}-{candidate.id}"
        tree = _scan_tree(archive_target)
        if tree.digest != candidate.before_sha256:
            raise SkillCurationError("歸檔內容已變更; 為避免覆寫, 拒絕回復")
        self._write_audit(chat_id, candidate, event="rollback_prepared")
        moved = False
        persisted = False
        try:
            os.replace(archive_target, active)
            moved = True
            rolled_back = replace(candidate, status="rolled_back", decided_at=self._timestamp())
            self._save_candidate(chat_id, rolled_back)
            persisted = True
            self._write_audit(chat_id, rolled_back, event="rolled_back")
        except (OSError, SkillCurationError) as error:
            if moved:
                try:
                    os.replace(active, archive_target)
                except OSError:
                    pass
            if persisted:
                try:
                    self._save_candidate(chat_id, candidate)
                    self._write_audit(chat_id, candidate, event="rollback_reverted")
                except (OSError, SkillCurationError):
                    pass
            raise SkillCurationError("Skill 回復失敗; 已嘗試恢復歸檔 tree") from error
        return SkillArchiveMutationResult(rolled_back, active, archive_target)

    def _get(self, chat_id: int, entry_id: str) -> SkillArchiveCandidate:
        _validate_chat_id(chat_id)
        if _ENTRY_ID.fullmatch(entry_id) is None:
            raise SkillCurationError("Skill 歸檔候選 ID 無效")
        path = self._chat_root(chat_id) / "candidates" / f"{entry_id}.json"
        if not path.is_file() or path.is_symlink():
            raise SkillCurationError(f"Skill 歸檔候選不存在: {entry_id}")
        return self._load_candidate(path)

    def _candidate_paths(self, chat_id: int) -> tuple[Path, ...]:
        _validate_chat_id(chat_id)
        directory = self._chat_root(chat_id) / "candidates"
        if not directory.exists():
            return ()
        _require_directory(directory, "Skill 歸檔候選目錄")
        paths = tuple(sorted(directory.glob("*.json"), key=lambda item: item.name))
        if len(paths) > _MAX_ARCHIVE_CANDIDATES or any(path.is_symlink() for path in paths):
            raise SkillCurationError("Skill 歸檔候選目錄內容無效")
        return paths

    def _new_id(self, snapshot: SkillArchiveSnapshot) -> str:
        existing = {item.id for item in snapshot.entries}
        for _ in range(20):
            try:
                candidate = self._id_factory()
            except (StopIteration, RuntimeError, ValueError) as error:
                raise SkillCurationError("無法產生 Skill 歸檔候選 ID") from error
            if _ENTRY_ID.fullmatch(candidate) is not None and candidate not in existing:
                return candidate
        raise SkillCurationError("無法產生唯一 Skill 歸檔候選 ID")

    def _save_candidate(
        self,
        chat_id: int,
        candidate: SkillArchiveCandidate,
        *,
        create: bool = False,
    ) -> None:
        _validate_candidate(candidate)
        directory = self._prepare_chat_dir(chat_id, "candidates")
        _write_json_atomic(
            directory / f"{candidate.id}.json",
            _candidate_payload(candidate),
            create=create,
        )

    def _load_candidate(self, path: Path) -> SkillArchiveCandidate:
        try:
            if path.stat().st_size > _MAX_STATE_BYTES:
                raise SkillCurationError("Skill 歸檔候選狀態過大")
            raw_object = cast(
                object,
                json.loads(
                    path.read_text(encoding="ascii"),
                    parse_constant=_reject_json_constant,
                    object_pairs_hook=_reject_duplicate_json_pairs,
                ),
            )
        except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
            raise SkillCurationError("Skill 歸檔候選狀態損壞") from error
        if not isinstance(raw_object, dict):
            raise SkillCurationError("Skill 歸檔候選狀態必須是 JSON object")
        raw = cast(dict[object, object], raw_object)
        try:
            candidate = SkillArchiveCandidate(
                id=cast(str, raw["id"]),
                name=cast(str, raw["name"]),
                workspace=cast(str, raw["workspace"]),
                before_sha256=cast(str, raw["before_sha256"]),
                file_count=cast(int, raw["file_count"]),
                total_bytes=cast(int, raw["total_bytes"]),
                reason=cast(str, raw["reason"]),
                created_at=cast(str, raw["created_at"]),
                status=cast(SkillArchiveStatus, raw.get("status", "pending")),
                decided_at=cast(str | None, raw.get("decided_at")),
            )
            _validate_candidate(candidate)
        except (KeyError, TypeError, ValueError) as error:
            raise SkillCurationError("Skill 歸檔候選內容無效") from error
        return candidate

    def _prepare_chat_dir(self, chat_id: int, name: str) -> Path:
        _validate_chat_id(chat_id)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        _require_directory(self.root, "Skill 歸檔根目錄")
        chat_root = self._chat_root(chat_id)
        chat_root.mkdir(mode=0o700, exist_ok=True)
        _require_directory(chat_root, "Skill 歸檔 chat 目錄")
        directory = chat_root / name
        directory.mkdir(mode=0o700, exist_ok=True)
        _require_directory(directory, "Skill 歸檔資料目錄")
        return directory

    def _chat_root(self, chat_id: int) -> Path:
        _validate_chat_id(chat_id)
        return self.root / str(chat_id)

    def _timestamp(self) -> str:
        value = self._clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _write_audit(
        self,
        chat_id: int,
        candidate: SkillArchiveCandidate,
        *,
        event: str,
    ) -> None:
        directory = self._prepare_chat_dir(chat_id, "audit")
        destination = directory / f"{self._timestamp_filename()}-{candidate.id}-{event}.json"
        if len(tuple(directory.glob("*.json"))) >= _MAX_AUDIT_FILES:
            raise SkillCurationError("Skill 歸檔 audit 已達上限")
        _write_json_atomic(
            destination,
            {
                "version": _VERSION,
                "candidate_id": candidate.id,
                "skill": candidate.name,
                "event": event,
                "status": candidate.status,
                "before_sha256": candidate.before_sha256,
                "at": self._timestamp(),
            },
            create=True,
        )

    def _timestamp_filename(self) -> str:
        return self._timestamp().replace(":", "").replace("-", "") + f"-{uuid.uuid4().hex[:8]}"


def _candidate_payload(candidate: SkillArchiveCandidate) -> dict[str, object]:
    return {
        "version": _VERSION,
        "id": candidate.id,
        "name": candidate.name,
        "workspace": candidate.workspace,
        "before_sha256": candidate.before_sha256,
        "file_count": candidate.file_count,
        "total_bytes": candidate.total_bytes,
        "reason": candidate.reason,
        "created_at": candidate.created_at,
        "status": candidate.status,
        "decided_at": candidate.decided_at,
    }


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON constant: {value}")


def _reject_duplicate_json_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _validate_candidate(candidate: SkillArchiveCandidate) -> None:
    if (
        not isinstance(candidate.id, str)
        or not isinstance(candidate.name, str)
        or not isinstance(candidate.workspace, str)
        or not isinstance(candidate.before_sha256, str)
        or not isinstance(candidate.file_count, int)
        or isinstance(candidate.file_count, bool)
        or not isinstance(candidate.total_bytes, int)
        or isinstance(candidate.total_bytes, bool)
        or not isinstance(candidate.reason, str)
        or not isinstance(candidate.created_at, str)
        or _ENTRY_ID.fullmatch(candidate.id) is None
        or _SKILL_NAME.fullmatch(candidate.name) is None
        or candidate.name in BUILTIN_SKILLS
        or not candidate.workspace
        or Path(candidate.workspace) == Path(Path(candidate.workspace).anchor)
        or not _SHA256.fullmatch(candidate.before_sha256)
        or not 1 <= candidate.file_count <= _MAX_ARCHIVE_FILES
        or not 1 <= candidate.total_bytes <= _MAX_ARCHIVE_BYTES
        or not 1 <= len(candidate.reason) <= _MAX_REASON_CHARS
        or "\n" in candidate.reason
        or "\r" in candidate.reason
        or not isinstance(candidate.status, str)
        or candidate.status not in {"pending", "approved", "rejected", "stale", "rolled_back"}
        or (candidate.decided_at is not None and not isinstance(candidate.decided_at, str))
        or not _valid_timestamp(candidate.created_at)
        or (
            candidate.decided_at is not None
            and not _valid_timestamp(candidate.decided_at)
        )
        or (candidate.status == "pending" and candidate.decided_at is not None)
        or (candidate.status != "pending" and candidate.decided_at is None)
    ):
        raise ValueError("invalid Skill archive candidate")


def _normalize_name(value: str) -> str:
    normalized = value.strip().casefold()
    if _SKILL_NAME.fullmatch(normalized) is None:
        raise SkillCurationError("Skill 名稱必須是安全的小寫 slug")
    return normalized


def _normalize_reason(value: str) -> str:
    normalized = " ".join(value.split())
    if not 1 <= len(normalized) <= _MAX_REASON_CHARS:
        raise SkillCurationError("Skill 歸檔原因長度無效")
    if contains_sensitive_text(normalized) or contains_instruction_injection(normalized):
        raise SkillCurationError("Skill 歸檔原因未通過安全掃描")
    return normalized


def _safe_workspace(workspace: Path) -> Path:
    expanded = workspace.expanduser()
    if expanded.is_symlink():
        raise SkillCurationError("Skill 工作區不可為 symlink")
    try:
        resolved = expanded.resolve(strict=True)
    except OSError as error:
        raise SkillCurationError("Skill 工作區不存在") from error
    if (
        resolved == Path(resolved.anchor)
        or not resolved.is_dir()
        or resolved != expanded.absolute()
        or ".xclaude" in resolved.parts
        or ".dsh" in resolved.parts
    ):
        raise SkillCurationError("Skill 工作區不安全")
    return resolved


def _active_skill_path(workspace: Path, name: str) -> Path:
    normalized_name = _normalize_name(name)
    xdsh_root = workspace / ".xdsh"
    skills_root = xdsh_root / "skills"
    if xdsh_root.is_symlink() or (xdsh_root.exists() and not xdsh_root.is_dir()):
        raise SkillCurationError("XDSH project root is unsafe")
    if skills_root.is_symlink() or (skills_root.exists() and not skills_root.is_dir()):
        raise SkillCurationError("active Skill root is unsafe")
    return skills_root / normalized_name


def _active_skill_root(workspace: Path, name: str) -> Path:
    target = _active_skill_path(workspace, name)
    for directory in (workspace / ".xdsh", workspace / ".xdsh" / "skills", target):
        if directory.is_symlink() or not directory.is_dir():
            raise SkillCurationError("active Skill path is unsafe or missing")
    resolved = target.resolve(strict=True)
    if resolved != target.absolute() or not resolved.is_relative_to(workspace):
        raise SkillCurationError("active Skill path escapes workspace")
    _validate_skill_owner(resolved)
    return resolved


def _archive_root(workspace: Path) -> Path:
    xdsh_root = workspace / ".xdsh"
    if xdsh_root.is_symlink() or not xdsh_root.is_dir():
        raise SkillCurationError("XDSH project root is unsafe")
    skills_root = xdsh_root / "skills"
    if skills_root.is_symlink() or not skills_root.is_dir():
        raise SkillCurationError("active Skill root is unsafe")
    target = xdsh_root / "skills-archive"
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_dir():
            raise SkillCurationError("Skill archive root is unsafe")
    else:
        target.mkdir(mode=0o700)
    _require_directory(target, "Skill archive root")
    return target


def _validate_skill_owner(root: Path) -> None:
    target = root / "SKILL.md"
    if target.is_symlink() or not target.is_file():
        raise SkillCurationError("Skill SKILL.md is missing or unsafe")
    try:
        if target.stat().st_size > 64_000:
            raise SkillCurationError("Skill SKILL.md is too large")
        content = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise SkillCurationError("Skill SKILL.md cannot be read") from error
    lines = content.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillCurationError("Skill SKILL.md lacks frontmatter")
    fields: dict[str, str] = {}
    for line in lines[1:40]:
        if line.strip() == "---":
            break
        key, separator, value = line.partition(":")
        if separator:
            fields[key.strip().casefold()] = value.strip().strip("'\"")
    if fields.get("name", "").casefold() != root.name.casefold():
        raise SkillCurationError("Skill name does not match its directory")
    if fields.get("x-xdsh-owner") != "agent-approved":
        raise SkillCurationError("Skill is not XDSH agent-owned")


def _scan_tree(root: Path) -> _TreeInfo:
    """Hash a small text-oriented Skill tree without following symlinks."""

    if root.is_symlink() or not root.is_dir():
        raise SkillCurationError("Skill tree is not a regular directory")
    digest = hashlib.sha256()
    file_count = 0
    total_bytes = 0

    def visit(directory: Path, prefix: str) -> None:
        nonlocal file_count, total_bytes
        try:
            children = tuple(sorted(directory.iterdir(), key=lambda item: item.name))
        except OSError as error:
            raise SkillCurationError("Skill tree cannot be read") from error
        for child in children:
            if child.is_symlink() or child.name.startswith("."):
                raise SkillCurationError("Skill tree contains a hidden or symlinked entry")
            relative = child.name if not prefix else f"{prefix}/{child.name}"
            if _RELATIVE_FILE.fullmatch(relative) is None:
                raise SkillCurationError("Skill tree contains an unsafe path")
            if child.is_dir():
                visit(child, relative)
                continue
            if not child.is_file():
                raise SkillCurationError("Skill tree contains a non-regular entry")
            file_count += 1
            if file_count > _MAX_ARCHIVE_FILES:
                raise SkillCurationError("Skill tree contains too many files")
            try:
                size = child.stat().st_size
                if size > _MAX_ARCHIVE_BYTES or total_bytes + size > _MAX_ARCHIVE_BYTES:
                    raise SkillCurationError("Skill tree exceeds the archive byte bound")
                content = child.read_bytes()
            except OSError as error:
                raise SkillCurationError("Skill tree file cannot be read") from error
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError as error:
                raise SkillCurationError("Skill tree contains a non-text file") from error
            if contains_sensitive_text(text) or contains_instruction_injection(text):
                raise SkillCurationError("Skill tree failed the secret/injection scan")
            total_bytes += len(content)
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(len(content).to_bytes(8, "big"))
            digest.update(content)

    visit(root, "")
    if file_count == 0 or not (root / "SKILL.md").is_file():
        raise SkillCurationError("Skill tree must contain SKILL.md")
    return _TreeInfo(digest.hexdigest(), file_count, total_bytes)


def _require_directory(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_dir():
        raise SkillCurationError(f"{label}不安全")
    path.chmod(0o700)


def _write_json_atomic(
    destination: Path,
    payload: dict[str, object],
    *,
    create: bool,
) -> None:
    content = json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n"
    if create and destination.exists():
        raise SkillCurationError("Skill 歸檔記錄已存在")
    if destination.is_symlink() or (destination.exists() and not destination.is_file()):
        raise SkillCurationError("Skill 歸檔記錄路徑不安全")
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if create and destination.exists():
            raise SkillCurationError("Skill 歸檔記錄已存在")
        os.replace(temporary, destination)
        destination.chmod(0o600)
        _fsync_directory(destination.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validate_chat_id(chat_id: int) -> None:
    if not isinstance(chat_id, int) or isinstance(chat_id, bool) or chat_id == 0:
        raise SkillCurationError("chat ID 無效")


def _valid_timestamp(value: str) -> bool:
    if not isinstance(value, str) or len(value) > 64:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _valid_outcome(outcome: SkillOutcomeSummary) -> bool:
    if (
        not isinstance(outcome.runs, int)
        or isinstance(outcome.runs, bool)
        or not isinstance(outcome.succeeded, int)
        or isinstance(outcome.succeeded, bool)
        or not 0 <= outcome.succeeded <= outcome.runs
    ):
        return False
    if not _finite_number(outcome.success_rate, minimum=0.0, maximum=1.0):
        return False
    return _finite_number(outcome.last_used_at, minimum=0.0)


def _finite_number(
    value: object,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> bool:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    try:
        numeric = float(value)
    except (OverflowError, ValueError):
        return False
    return (
        math.isfinite(numeric)
        and (minimum is None or numeric >= minimum)
        and (maximum is None or numeric <= maximum)
    )


__all__ = [
    "SkillArchiveCandidate",
    "SkillArchiveMutationResult",
    "SkillArchiveSnapshot",
    "SkillArchiveStatus",
    "SkillArchiveStore",
    "SkillCurationAction",
    "SkillCurationError",
    "SkillCurationSuggestion",
    "SkillCurator",
]
