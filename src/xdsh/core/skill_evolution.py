"""Approval-gated, auditable evolution of project-owned Skills."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Protocol, cast

from xdsh.core.learning import contains_instruction_injection, contains_sensitive_text
from xdsh.core.product_control import ProductControlError, ProductSessionControl
from xdsh.core.skills import BUILTIN_SKILLS

SkillEvolutionStatus = Literal[
    "pending",
    "approved",
    "rejected",
    "stale",
    "rolled_back",
]
SkillEvolutionAction = Literal["create", "patch"]
SkillInstallScope = Literal["project", "personal"]

_VERSION = 1
_ENTRY_ID = re.compile(r"^[a-f0-9]{8}$")
_SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
_ORIGIN = re.compile(r"^[a-z_]{1,32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RELATIVE_FILE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}(?:/[A-Za-z0-9][A-Za-z0-9._-]{0,127})*$"
)
_MAX_SUMMARY_CHARS = 300
_MAX_SKILL_CHARS = 16_000
_MAX_SUPPORT_FILES = 16
_MAX_SUPPORT_FILE_BYTES = 64_000
_MAX_SUPPORT_TOTAL_BYTES = 256_000
_MAX_CANDIDATES = 100
_MAX_DIFF_CHARS = 20_000
_MAX_STATE_BYTES = 1_000_000
_OWNER_FIELD = "x-xdsh-owner"
_OWNER_VALUE = "agent-approved"
_EVALUATION_CHECKS = frozenset(
    {
        "active_target_unchanged",
        "complete_bounded_diff",
        "isolated_loader_discovery",
        "isolated_selection",
        "isolated_write_mode",
    }
)

SupportFiles = tuple[tuple[str, str], ...]
SupportStates = tuple[tuple[str, str | None], ...]


class SkillEvolutionError(RuntimeError):
    """A proposed Skill mutation is unsafe, stale, or otherwise invalid."""


@dataclass(frozen=True, slots=True)
class SkillProposal:
    """Strict no-tools reviewer output before it becomes durable state."""

    name: str
    summary: str
    content: str
    # Optional text resources used by the Skill (templates, schemas, examples,
    # and similar).  They are additive: omission never authorizes deletion of
    # an existing project file.  Applying them remains approval-gated.
    support_files: SupportFiles = ()

    def __post_init__(self) -> None:
        normalized_name = _normalize_name(self.name)
        normalized_summary = _normalize_summary(self.summary)
        normalized_content = _normalize_skill_content(self.content, normalized_name)
        if (
            normalized_name != self.name
            or normalized_summary != self.summary
            or normalized_content != self.content
        ):
            raise ValueError("skill proposal must already be normalized")
        object.__setattr__(self, "support_files", _canonical_support_files(self.support_files))


@dataclass(frozen=True, slots=True)
class ManagedSkillSource:
    """One bounded agent-owned Skill that a reviewer may propose patching."""

    name: str
    content: str
    support_files: SupportFiles = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "support_files", _canonical_support_files(self.support_files))


class SkillLearningReviewer(Protocol):
    """No-tools reviewer seam for procedural-learning proposals."""

    async def review_skill(
        self,
        *,
        user_text: str,
        final_text: str,
        tool_names: tuple[str, ...],
        existing_skills: tuple[ManagedSkillSource, ...],
    ) -> SkillProposal | None: ...


@dataclass(frozen=True, slots=True)
class SkillCandidate:
    """One durable candidate and its transactional state."""

    id: str
    name: str
    action: SkillEvolutionAction
    summary: str
    content: str
    base_content: str | None
    base_sha256: str | None
    proposed_sha256: str
    workspace: str
    origin: str
    created_at: str
    status: SkillEvolutionStatus = "pending"
    decided_at: str | None = None
    applied_sha256: str | None = None
    evaluation_sha256: str | None = None
    evaluated_at: str | None = None
    evaluation_passed: bool | None = None
    support_files: SupportFiles = ()
    base_support_files: SupportStates = ()
    base_tree_sha256: str | None = None
    proposed_tree_sha256: str | None = None
    applied_tree_sha256: str | None = None
    install_scope: SkillInstallScope = "project"
    install_base_sha256: str | None = None
    install_base_tree_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class SkillEvolutionSnapshot:
    """Current candidates for one Telegram chat."""

    entries: tuple[SkillCandidate, ...]

    @property
    def pending(self) -> tuple[SkillCandidate, ...]:
        return tuple(entry for entry in self.entries if entry.status == "pending")

    @property
    def applied(self) -> tuple[SkillCandidate, ...]:
        return tuple(entry for entry in self.entries if entry.status == "approved")


@dataclass(frozen=True, slots=True)
class SkillMutationResult:
    """Observable result of an approved apply or rollback."""

    candidate: SkillCandidate
    target: Path


@dataclass(frozen=True, slots=True)
class PersonalSkillEntry:
    """A verified Skill installed in one Telegram chat's reusable library."""

    name: str
    sha256: str
    candidate_id: str
    content: str
    support_files: SupportFiles = ()
    description: str = ""


@dataclass(frozen=True, slots=True)
class _SkillTreeState:
    """The bounded subset of a Skill that an approved candidate owns."""

    content: str | None
    support_files: SupportStates = ()


@dataclass(frozen=True, slots=True)
class SkillEvaluationReport:
    """Content-free result from loading a candidate in an isolated workspace."""

    candidate_id: str
    proposed_sha256: str
    evaluated_at: str
    passed_checks: tuple[str, ...]
    failed_checks: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.failed_checks and set(self.passed_checks) == _EVALUATION_CHECKS


class SkillEvolutionStore:
    """Own validation, diff, snapshots, atomic mutation, audit, and rollback."""

    def __init__(
        self,
        state_root: Path,
        *,
        id_factory: Callable[[], str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        root = state_root.expanduser().resolve()
        if root == Path(root.anchor) or ".xclaude" in root.parts or ".dsh" in root.parts:
            raise SkillEvolutionError("Skill 演化狀態根目錄不安全")
        self.root = root / "skill-evolution"
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex[:8])
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def snapshot(self, chat_id: int) -> SkillEvolutionSnapshot:
        entries = tuple(
            sorted(
                (
                    self._load_candidate(path)
                    for path in self._candidate_paths(chat_id)
                ),
                key=lambda entry: (entry.created_at, entry.id),
            )
        )
        return SkillEvolutionSnapshot(entries)

    def managed_sources(self, workspace: Path) -> tuple[ManagedSkillSource, ...]:
        """Return only agent-approved Skills that a background reviewer may patch."""

        resolved = _safe_workspace(workspace)
        skills_root = _existing_skills_root(resolved)
        if skills_root is None:
            return ()
        sources: list[ManagedSkillSource] = []
        for directory in sorted(skills_root.iterdir(), key=lambda item: item.name):
            if directory.is_symlink() or not directory.is_dir():
                continue
            name = directory.name.casefold()
            if _SKILL_NAME.fullmatch(name) is None or name in BUILTIN_SKILLS:
                continue
            target = directory / "SKILL.md"
            try:
                content = _read_skill(target, expected_name=name, require_owned=True)
                support_files = _read_support_tree(directory)
            except SkillEvolutionError:
                continue
            if content is not None:
                sources.append(ManagedSkillSource(name, content, support_files))
        return tuple(sources[:20])

    def personal_skills_root(self, chat_id: int, *, create: bool = False) -> Path:
        """Return the isolated reusable Skill root for one Telegram chat."""

        _validate_chat_id(chat_id)
        chat_root = self._chat_root(chat_id)
        library_root = chat_root / "personal-skills"
        skills_root = library_root / "skills"
        if create:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            _require_real_directory(self.root, "Skill 演化根目錄")
            chat_root.mkdir(mode=0o700, exist_ok=True)
            _require_real_directory(chat_root, "Skill 演化 chat 目錄")
            library_root.mkdir(mode=0o700, exist_ok=True)
            _require_real_directory(library_root, "個人 Skill 庫")
            skills_root.mkdir(mode=0o700, exist_ok=True)
            _require_real_directory(skills_root, "個人 Skill 目錄")
        else:
            for directory, label in (
                (chat_root, "Skill 演化 chat 目錄"),
                (library_root, "個人 Skill 庫"),
                (skills_root, "個人 Skill 目錄"),
            ):
                if directory.is_symlink():
                    raise SkillEvolutionError(f"{label}不安全")
                if not directory.exists():
                    break
                _require_real_directory(directory, label)
        return skills_root

    def personal_skills(self, chat_id: int) -> tuple[PersonalSkillEntry, ...]:
        """List only Skills whose current bytes still match an approved record."""

        root = self.personal_skills_root(chat_id)
        if not root.exists():
            return ()
        from xdsh.core.product_control import _skill_description

        entries: list[PersonalSkillEntry] = []
        for candidate in self.snapshot(chat_id).applied:
            if candidate.install_scope != "personal":
                continue
            target = _personal_skill_target(root, candidate.name, create=False)
            try:
                content = _read_skill(target, expected_name=candidate.name, require_owned=True)
                support = _read_support_tree(target.parent)
            except SkillEvolutionError:
                continue
            if content is None or _sha256(content) != candidate.applied_sha256:
                continue
            if _tree_sha256(content, support) != candidate.applied_tree_sha256:
                continue
            entries.append(
                PersonalSkillEntry(
                    name=candidate.name,
                    sha256=candidate.applied_sha256,
                    candidate_id=candidate.id,
                    content=content,
                    support_files=support,
                    description=_skill_description(content),
                )
            )
        return tuple(sorted(entries, key=lambda item: item.name))

    def stage(
        self,
        chat_id: int,
        *,
        workspace: Path,
        proposal: SkillProposal,
        origin: str,
    ) -> SkillCandidate:
        """Persist a candidate without changing the active Skill tree."""

        _validate_chat_id(chat_id)
        if _ORIGIN.fullmatch(origin) is None:
            raise SkillEvolutionError("Skill 演化來源無效")
        resolved_workspace = _safe_workspace(workspace)
        if proposal.name in BUILTIN_SKILLS:
            raise SkillEvolutionError(
                f"Skill {proposal.name} 是 XDSH 內置 Skill; 不允許演化流程覆寫"
            )
        existing = self.snapshot(chat_id)
        if len(existing.entries) >= _MAX_CANDIDATES:
            raise SkillEvolutionError("Skill 演化候選已達上限")
        if any(
            entry.name == proposal.name and entry.status == "pending"
            for entry in existing.entries
        ):
            raise SkillEvolutionError(f"Skill {proposal.name} 已有待批准候選")

        target = _skill_target(resolved_workspace, proposal.name, create=False)
        base_content = _read_skill(
            target,
            expected_name=proposal.name,
            require_owned=True,
        )
        skill_root = target.parent
        if base_content is None and skill_root.exists():
            _require_real_directory(skill_root, "專案 Skill 目錄")
            if any(skill_root.iterdir()):
                raise SkillEvolutionError(
                    "沒有 SKILL.md 的既有 Skill 目錄含有檔案; 為避免覆寫, 拒絕建立"
                )
        base_support_files = _read_support_states(
            skill_root,
            (path for path, _content in proposal.support_files),
        )
        action: SkillEvolutionAction = "patch" if base_content is not None else "create"
        if base_content == proposal.content and all(
            current == proposed
            for (path, current), (_same_path, proposed) in zip(
                base_support_files,
                proposal.support_files,
                strict=True,
            )
        ):
            raise SkillEvolutionError(f"Skill {proposal.name} 沒有可套用的變更")
        base_sha256 = _sha256(base_content) if base_content is not None else None
        base_tree_sha256 = _tree_sha256(base_content, base_support_files)
        proposed_tree_sha256 = _tree_sha256(
            proposal.content,
            tuple((path, content) for path, content in proposal.support_files),
        )
        candidate = SkillCandidate(
            id=self._new_id(existing),
            name=proposal.name,
            action=action,
            summary=proposal.summary,
            content=proposal.content,
            base_content=base_content,
            base_sha256=base_sha256,
            proposed_sha256=_sha256(proposal.content),
            workspace=str(resolved_workspace),
            origin=origin,
            created_at=self._timestamp(),
            support_files=proposal.support_files,
            base_support_files=base_support_files,
            base_tree_sha256=base_tree_sha256,
            proposed_tree_sha256=proposed_tree_sha256,
        )
        self._save_candidate(chat_id, candidate, create=True)
        self._write_audit(chat_id, candidate, event="staged")
        return candidate

    def diff(self, chat_id: int, entry_id: str) -> str:
        candidate = self._get(chat_id, entry_id)
        rendered = _render_diff(candidate)
        if len(rendered) > _MAX_DIFF_CHARS:
            rendered = rendered[: _MAX_DIFF_CHARS - 24] + "\n... diff truncated ...\n"
        return rendered

    def evaluate(
        self,
        chat_id: int,
        entry_id: str,
        *,
        workspace: Path,
    ) -> SkillEvaluationReport:
        """Load a candidate without tools in an isolated workspace before approval."""

        candidate = self._get(chat_id, entry_id)
        if candidate.status != "pending":
            raise SkillEvolutionError(
                f"Skill 候選 {candidate.id} 目前狀態為 {candidate.status}"
            )
        resolved_workspace = _safe_workspace(workspace)
        if str(resolved_workspace) != candidate.workspace:
            raise SkillEvolutionError("Skill 候選不屬於目前工作區")
        active_target = _skill_target(resolved_workspace, candidate.name, create=False)
        active_content = _read_skill(
            active_target,
            expected_name=candidate.name,
            require_owned=True,
        )
        active_hash = _sha256(active_content) if active_content is not None else None
        current_support = _read_support_states(
            active_target.parent,
            (path for path, _content in candidate.support_files),
        )
        current_tree_hash = _tree_sha256(active_content, current_support)
        if (
            active_hash != candidate.base_sha256
            or current_support != candidate.base_support_files
            or (
                candidate.base_tree_sha256 is not None
                and current_tree_hash != candidate.base_tree_sha256
            )
        ):
            stale = replace(candidate, status="stale", decided_at=self._timestamp())
            self._save_candidate(chat_id, stale)
            self._write_audit(chat_id, stale, event="evaluation_stale")
            raise SkillEvolutionError("Skill 已在候選建立後變更; 候選已標記 stale")

        passed: set[str] = set()
        failed: set[str] = set()
        if len(_render_diff(candidate)) <= _MAX_DIFF_CHARS:
            passed.add("complete_bounded_diff")
        else:
            failed.add("complete_bounded_diff")

        evaluation_parent = self._prepare_chat_dir(chat_id, "evaluation-workspaces")
        isolated_root = evaluation_parent / f".{candidate.id}.{uuid.uuid4().hex}.tmp"
        isolated_root.mkdir(mode=0o700)
        try:
            isolated_workspace = isolated_root / "workspace"
            isolated_workspace.mkdir(mode=0o700)
            isolated_target = _skill_target(
                isolated_workspace,
                candidate.name,
                create=True,
            )
            _write_skill(isolated_target, candidate.content)
            for relative_path, content in candidate.support_files:
                _write_support_file(isolated_target.parent, relative_path, content)
            support_paths = tuple(
                _support_path(isolated_target.parent, path)
                for path, _content in candidate.support_files
            )
            isolated_files = (isolated_target, *support_paths)
            if all(path.stat().st_mode & 0o777 == 0o600 for path in isolated_files):
                passed.add("isolated_write_mode")
            else:
                failed.add("isolated_write_mode")
            control = ProductSessionControl(isolated_root / "control-state")
            discovered = control.list_skills(
                "skill-evaluation",
                workspace=isolated_workspace,
            )
            if any(skill.name == candidate.name for skill in discovered):
                passed.add("isolated_loader_discovery")
            else:
                failed.add("isolated_loader_discovery")
            prepared = control.prepare_task(
                "skill-evaluation",
                candidate.summary,
                workspace=isolated_workspace,
            )
            if candidate.name in prepared.selected_skills:
                passed.add("isolated_selection")
            else:
                failed.add("isolated_selection")
        except (OSError, ProductControlError, SkillEvolutionError):
            for check in (
                "isolated_loader_discovery",
                "isolated_selection",
                "isolated_write_mode",
            ):
                if check not in passed:
                    failed.add(check)
        finally:
            shutil.rmtree(isolated_root)

        after_content = _read_skill(
            active_target,
            expected_name=candidate.name,
            require_owned=True,
        )
        after_hash = _sha256(after_content) if after_content is not None else None
        after_support = _read_support_states(
            active_target.parent,
            (path for path, _content in candidate.support_files),
        )
        if (
            after_hash == active_hash == candidate.base_sha256
            and after_support == current_support == candidate.base_support_files
        ):
            passed.add("active_target_unchanged")
        else:
            failed.add("active_target_unchanged")
        failed.difference_update(passed)
        evaluated_at = self._timestamp()
        report = SkillEvaluationReport(
            candidate_id=candidate.id,
            proposed_sha256=candidate.proposed_sha256,
            evaluated_at=evaluated_at,
            passed_checks=tuple(sorted(passed)),
            failed_checks=tuple(sorted(failed)),
        )
        self._write_evaluation_report(chat_id, report)
        evaluated = replace(
            candidate,
            evaluation_sha256=candidate.proposed_sha256,
            evaluated_at=evaluated_at,
            evaluation_passed=report.passed,
        )
        self._save_candidate(chat_id, evaluated)
        self._write_audit(
            chat_id,
            evaluated,
            event="evaluation_passed" if report.passed else "evaluation_failed",
        )
        return report

    def evaluation(self, chat_id: int, entry_id: str) -> SkillEvaluationReport:
        candidate = self._get(chat_id, entry_id)
        return self._load_evaluation_report(chat_id, candidate)

    def approve(
        self,
        chat_id: int,
        entry_id: str,
        *,
        workspace: Path,
        scope: SkillInstallScope = "project",
    ) -> SkillMutationResult:
        candidate = self._get(chat_id, entry_id)
        if candidate.status != "pending":
            raise SkillEvolutionError(
                f"Skill 候選 {candidate.id} 目前狀態為 {candidate.status}"
            )
        if (
            candidate.evaluation_passed is not True
            or candidate.evaluation_sha256 != candidate.proposed_sha256
        ):
            raise SkillEvolutionError("Skill 候選尚未通過隔離評估; 不可批准")
        report = self._load_evaluation_report(chat_id, candidate)
        if not report.passed:
            raise SkillEvolutionError("Skill 候選隔離評估未通過; 不可批准")
        if scope not in {"project", "personal"}:
            raise SkillEvolutionError("Skill 安裝範圍無效")
        resolved_workspace = _safe_workspace(workspace)
        if scope == "project" and str(resolved_workspace) != candidate.workspace:
            raise SkillEvolutionError("Skill 候選不屬於目前工作區")
        if scope == "personal":
            target = _personal_skill_target(
                self.personal_skills_root(chat_id), candidate.name, create=False
            )
            current = _read_skill(target, expected_name=candidate.name, require_owned=True)
            if current is not None:
                raise SkillEvolutionError(
                    "個人 Skill 庫已有同名 Skill; 為避免覆寫, 請先回滾舊候選"
                )
            if target.parent.exists() and any(target.parent.iterdir()):
                raise SkillEvolutionError(
                    "個人 Skill 目錄已有未受管理檔案; 為避免覆寫, 拒絕安裝"
                )
            current_support = _read_support_states(
                target.parent, (path for path, _content in candidate.support_files)
            )
            if any(content is not None for _path, content in current_support):
                raise SkillEvolutionError("個人 Skill 目錄已有同名資源; 拒絕覆寫")
            before_state = _SkillTreeState(None, current_support)
        else:
            target = _skill_target(resolved_workspace, candidate.name, create=False)
            current = _read_skill(
                target,
                expected_name=candidate.name,
                require_owned=True,
            )
            current_support = _read_support_states(
                target.parent,
                (path for path, _content in candidate.support_files),
            )
            current_hash = _sha256(current) if current is not None else None
            if (
                current_hash != candidate.base_sha256
                or current_support != candidate.base_support_files
            ):
                stale = replace(candidate, status="stale", decided_at=self._timestamp())
                self._save_candidate(chat_id, stale)
                self._write_audit(chat_id, stale, event="stale")
                raise SkillEvolutionError("Skill 已在候選建立後變更; 候選已標記 stale")
            before_state = _SkillTreeState(current, current_support)
        scoped_candidate = replace(
            candidate,
            install_scope=scope,
            install_base_sha256=(
                _sha256(before_state.content) if before_state.content is not None else None
            ),
            install_base_tree_sha256=_tree_sha256(
                before_state.content, before_state.support_files
            ),
        )
        self._write_snapshot(chat_id, scoped_candidate, before_state)
        self._write_audit(chat_id, scoped_candidate, event="apply_prepared")
        mutated = False
        try:
            if scope == "personal":
                target = _personal_skill_target(
                    self.personal_skills_root(chat_id, create=True),
                    candidate.name,
                    create=True,
                )
            else:
                target = _skill_target(resolved_workspace, candidate.name, create=True)
            mutated = True
            _write_skill(target, candidate.content)
            for relative_path, content in candidate.support_files:
                _write_support_file(target.parent, relative_path, content)
            applied = replace(
                scoped_candidate,
                status="approved",
                decided_at=self._timestamp(),
                applied_sha256=candidate.proposed_sha256,
                applied_tree_sha256=candidate.proposed_tree_sha256,
            )
            self._save_candidate(chat_id, applied)
            self._write_audit(chat_id, applied, event="applied")
        except (OSError, SkillEvolutionError) as error:
            if mutated:
                try:
                    self._restore_tree(
                        target.parent,
                    before_state,
                    )
                except (OSError, SkillEvolutionError):
                    pass
                try:
                    self._save_candidate(chat_id, candidate)
                    self._write_audit(chat_id, candidate, event="apply_rolled_back")
                except (OSError, SkillEvolutionError):
                    pass
            raise SkillEvolutionError("Skill 套用失敗; 已嘗試恢復 before-state") from error
        return SkillMutationResult(applied, target)

    def reject(self, chat_id: int, entry_id: str) -> SkillCandidate:
        candidate = self._get(chat_id, entry_id)
        if candidate.status != "pending":
            raise SkillEvolutionError(
                f"Skill 候選 {candidate.id} 目前狀態為 {candidate.status}"
            )
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
    ) -> SkillMutationResult:
        candidate = self._get(chat_id, entry_id)
        if candidate.status != "approved" or candidate.applied_sha256 is None:
            raise SkillEvolutionError("只有仍在生效的已批准 Skill 候選可以回滾")
        if candidate.install_scope == "personal":
            target = _personal_skill_target(
                self.personal_skills_root(chat_id), candidate.name, create=False
            )
        else:
            resolved_workspace = _safe_workspace(workspace)
            if str(resolved_workspace) != candidate.workspace:
                raise SkillEvolutionError("Skill 候選不屬於目前工作區")
            target = _skill_target(resolved_workspace, candidate.name, create=False)
        current = _read_skill(
            target,
            expected_name=candidate.name,
            require_owned=True,
        )
        current_support = _read_support_states(
            target.parent,
            (path for path, _content in candidate.support_files),
        )
        current_tree_hash = _tree_sha256(
            current,
            tuple((path, value) for path, value in current_support),
        )
        expected_applied_tree = candidate.applied_tree_sha256 or candidate.applied_sha256
        if (
            current is None
            or _sha256(current) != candidate.applied_sha256
            or (
                candidate.applied_tree_sha256 is not None
                and current_tree_hash != expected_applied_tree
            )
        ):
            raise SkillEvolutionError("Skill 套用後又被修改; 為避免覆寫, 拒絕回滾")
        before = self._load_snapshot(chat_id, candidate)
        self._write_audit(chat_id, candidate, event="rollback_prepared")
        applied_state = _SkillTreeState(current, current_support)
        restored = False
        persisted = False
        try:
            self._restore_tree(target.parent, before)
            restored = True
            rolled_back = replace(
                candidate,
                status="rolled_back",
                decided_at=self._timestamp(),
            )
            self._save_candidate(chat_id, rolled_back)
            persisted = True
            self._write_audit(chat_id, rolled_back, event="rolled_back")
        except (OSError, SkillEvolutionError) as error:
            if restored:
                try:
                    self._restore_tree(target.parent, applied_state)
                except (OSError, SkillEvolutionError):
                    pass
            if persisted:
                try:
                    self._save_candidate(chat_id, candidate)
                    self._write_audit(chat_id, candidate, event="rollback_reverted")
                except (OSError, SkillEvolutionError):
                    pass
            raise SkillEvolutionError("Skill 回滾失敗; 已嘗試恢復 applied-state") from error
        return SkillMutationResult(rolled_back, target)

    def _restore_tree(
        self,
        skill_root: Path,
        before: _SkillTreeState,
    ) -> None:
        """Restore only files owned by a candidate; never delete user files."""

        target = skill_root / "SKILL.md"
        if before.content is None:
            if target.is_symlink() or (target.exists() and not target.is_file()):
                raise SkillEvolutionError("Skill rollback 目標不安全")
            if target.exists():
                target.unlink()
        else:
            _write_skill(target, before.content)
        for relative_path, content in before.support_files:
            support = _support_path(skill_root, relative_path)
            if content is None:
                if support.is_symlink() or (support.exists() and not support.is_file()):
                    raise SkillEvolutionError("Skill support rollback 目標不安全")
                if support.exists():
                    support.unlink()
            else:
                _write_support_file(skill_root, relative_path, content)
        _fsync_directory(skill_root)

    def _get(self, chat_id: int, entry_id: str) -> SkillCandidate:
        _validate_chat_id(chat_id)
        if _ENTRY_ID.fullmatch(entry_id) is None:
            raise SkillEvolutionError("Skill 候選 ID 無效")
        path = self._chat_root(chat_id) / "candidates" / f"{entry_id}.json"
        if not path.is_file() or path.is_symlink():
            raise SkillEvolutionError(f"Skill 候選不存在: {entry_id}")
        return self._load_candidate(path)

    def _candidate_paths(self, chat_id: int) -> tuple[Path, ...]:
        _validate_chat_id(chat_id)
        directory = self._chat_root(chat_id) / "candidates"
        if not directory.exists():
            return ()
        if directory.is_symlink() or not directory.is_dir():
            raise SkillEvolutionError("Skill 候選目錄不安全")
        paths = tuple(sorted(directory.glob("*.json"), key=lambda item: item.name))
        if len(paths) > _MAX_CANDIDATES or any(path.is_symlink() for path in paths):
            raise SkillEvolutionError("Skill 候選目錄內容無效")
        return paths

    def _new_id(self, snapshot: SkillEvolutionSnapshot) -> str:
        existing = {entry.id for entry in snapshot.entries}
        for _ in range(20):
            try:
                candidate = self._id_factory()
            except (StopIteration, RuntimeError) as error:
                raise SkillEvolutionError("無法產生 Skill 候選 ID") from error
            if _ENTRY_ID.fullmatch(candidate) is not None and candidate not in existing:
                return candidate
        raise SkillEvolutionError("無法產生唯一 Skill 候選 ID")

    def _load_candidate(self, path: Path) -> SkillCandidate:
        try:
            if path.stat().st_size > _MAX_STATE_BYTES:
                raise SkillEvolutionError("Skill 候選狀態過大")
            raw_object = cast(
                object,
                json.loads(
                    path.read_text(encoding="ascii"),
                    parse_constant=_reject_json_constant,
                    object_pairs_hook=_reject_duplicate_json_pairs,
                ),
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SkillEvolutionError("Skill 候選狀態損壞") from error
        if not isinstance(raw_object, dict):
            raise SkillEvolutionError("Skill 候選狀態必須是 JSON object")
        raw = cast(dict[object, object], raw_object)
        try:
            if raw["version"] != _VERSION:
                raise ValueError
            candidate = SkillCandidate(
                id=cast(str, raw["id"]),
                name=cast(str, raw["name"]),
                action=cast(SkillEvolutionAction, raw["action"]),
                summary=cast(str, raw["summary"]),
                content=cast(str, raw["content"]),
                base_content=cast(str | None, raw["base_content"]),
                base_sha256=cast(str | None, raw["base_sha256"]),
                proposed_sha256=cast(str, raw["proposed_sha256"]),
                workspace=cast(str, raw["workspace"]),
                origin=cast(str, raw["origin"]),
                created_at=cast(str, raw["created_at"]),
                status=cast(SkillEvolutionStatus, raw["status"]),
                decided_at=cast(str | None, raw["decided_at"]),
                applied_sha256=cast(str | None, raw["applied_sha256"]),
                evaluation_sha256=cast(str | None, raw.get("evaluation_sha256")),
                evaluated_at=cast(str | None, raw.get("evaluated_at")),
                evaluation_passed=cast(bool | None, raw.get("evaluation_passed")),
                support_files=_decode_support_files(raw.get("support_files", [])),
                base_support_files=_decode_support_states(
                    raw.get("base_support_files", [])
                ),
                base_tree_sha256=cast(str | None, raw.get("base_tree_sha256")),
                proposed_tree_sha256=cast(str | None, raw.get("proposed_tree_sha256")),
                applied_tree_sha256=cast(str | None, raw.get("applied_tree_sha256")),
                install_scope=cast(SkillInstallScope, raw.get("install_scope", "project")),
                install_base_sha256=cast(str | None, raw.get("install_base_sha256")),
                install_base_tree_sha256=cast(
                    str | None, raw.get("install_base_tree_sha256")
                ),
            )
            self._validate_candidate(candidate)
        except (AttributeError, KeyError, OverflowError, TypeError, ValueError) as error:
            raise SkillEvolutionError("Skill 候選內容無效") from error
        return candidate

    def _validate_candidate(self, candidate: SkillCandidate) -> None:
        if not all(
            isinstance(value, str)
            for value in (
                candidate.id,
                candidate.name,
                candidate.summary,
                candidate.content,
                candidate.proposed_sha256,
                candidate.workspace,
                candidate.origin,
                candidate.created_at,
            )
        ):
            raise ValueError("invalid candidate text fields")
        if candidate.base_content is not None and not isinstance(candidate.base_content, str):
            raise ValueError("invalid candidate base content")
        for value in (
            candidate.base_sha256,
            candidate.applied_sha256,
            candidate.evaluation_sha256,
            candidate.evaluated_at,
            candidate.decided_at,
            candidate.base_tree_sha256,
            candidate.proposed_tree_sha256,
            candidate.applied_tree_sha256,
            candidate.install_base_sha256,
            candidate.install_base_tree_sha256,
        ):
            if value is not None and not isinstance(value, str):
                raise ValueError("invalid candidate optional field")
        if candidate.evaluation_passed is not None and not isinstance(
            candidate.evaluation_passed, bool
        ):
            raise ValueError("invalid candidate evaluation flag")
        workspace = Path(candidate.workspace)
        if (
            _ENTRY_ID.fullmatch(candidate.id) is None
            or _normalize_name(candidate.name) != candidate.name
            or candidate.name in BUILTIN_SKILLS
            or _normalize_summary(candidate.summary) != candidate.summary
            or _normalize_skill_content(candidate.content, candidate.name)
            != candidate.content
            or candidate.action not in {"create", "patch"}
            or candidate.status
            not in {"pending", "approved", "rejected", "stale", "rolled_back"}
            or candidate.install_scope not in {"project", "personal"}
            or _ORIGIN.fullmatch(candidate.origin) is None
            or _sha256(candidate.content) != candidate.proposed_sha256
            or (candidate.base_content is None) != (candidate.base_sha256 is None)
            or (
                candidate.base_content is not None
                and _sha256(candidate.base_content) != candidate.base_sha256
            )
            or (candidate.action == "create") != (candidate.base_content is None)
            or not workspace.is_absolute()
            or ".." in workspace.parts
            or ".xclaude" in workspace.parts
            or ".dsh" in workspace.parts
            or workspace == Path(workspace.anchor)
            or not _valid_timestamp(candidate.created_at)
            or (
                candidate.decided_at is not None
                and not _valid_timestamp(candidate.decided_at)
            )
        ):
            raise ValueError("invalid candidate")
        if _normalize_support_files(candidate.support_files) != candidate.support_files:
            raise ValueError("invalid candidate support files")
        if _normalize_support_states(candidate.base_support_files) != candidate.base_support_files:
            raise ValueError("invalid candidate base support files")
        if tuple(path for path, _content in candidate.base_support_files) != tuple(
            path for path, _content in candidate.support_files
        ):
            raise ValueError("candidate support file sets do not match")
        if candidate.proposed_tree_sha256 is None:
            # Version-1 records had no support-file tree hash.  They remain
            # readable, but a new candidate must carry the stronger binding.
            if (
                candidate.support_files
                or candidate.base_support_files
                or candidate.base_tree_sha256 is not None
                or candidate.applied_tree_sha256 is not None
            ):
                raise ValueError("candidate support files lack tree hash")
        else:
            if not _SHA256.fullmatch(candidate.proposed_tree_sha256):
                raise ValueError("invalid proposed tree hash")
            expected_proposed_tree = _tree_sha256(
                candidate.content,
                candidate.support_files,
            )
            if candidate.proposed_tree_sha256 != expected_proposed_tree:
                raise ValueError("proposed tree hash mismatch")
            if candidate.base_tree_sha256 is None or not _SHA256.fullmatch(
                candidate.base_tree_sha256
            ):
                raise ValueError("invalid base tree hash")
            if candidate.base_tree_sha256 != _tree_sha256(
                candidate.base_content,
                candidate.base_support_files,
            ):
                raise ValueError("base tree hash mismatch")
        if candidate.applied_tree_sha256 is not None:
            if not _SHA256.fullmatch(candidate.applied_tree_sha256) or (
                candidate.applied_tree_sha256 != candidate.proposed_tree_sha256
            ):
                raise ValueError("invalid applied tree hash")
        if candidate.install_base_sha256 is not None and not _SHA256.fullmatch(
            candidate.install_base_sha256
        ):
            raise ValueError("invalid installed before-state hash")
        if candidate.install_base_tree_sha256 is not None and not _SHA256.fullmatch(
            candidate.install_base_tree_sha256
        ):
            raise ValueError("invalid installed before-state tree hash")
        if candidate.install_base_sha256 is not None and candidate.install_base_tree_sha256 is None:
            raise ValueError("installed before-state hash is missing its tree hash")
        if candidate.applied_sha256 is not None and not _SHA256.fullmatch(
            candidate.applied_sha256
        ):
            raise ValueError("invalid applied hash")
        if candidate.status in {"approved", "rolled_back"} and candidate.applied_sha256 is None:
            raise ValueError("approved candidate lacks applied hash")
        if (
            candidate.status not in {"approved", "rolled_back"}
            and candidate.applied_sha256 is not None
        ):
            raise ValueError("non-approved candidate has applied hash")
        if (
            candidate.status not in {"approved", "rolled_back"}
            and candidate.applied_tree_sha256 is not None
        ):
            raise ValueError("non-approved candidate has applied tree hash")
        if candidate.status == "approved" and candidate.proposed_tree_sha256 is not None:
            if candidate.applied_tree_sha256 is None:
                raise ValueError("approved candidate lacks applied tree hash")
        evaluation_values = (
            candidate.evaluation_sha256,
            candidate.evaluated_at,
            candidate.evaluation_passed,
        )
        if (
            (any(value is None for value in evaluation_values)
            and not all(value is None for value in evaluation_values))
            or (
                candidate.evaluation_sha256 is not None
                and candidate.evaluation_sha256 != candidate.proposed_sha256
            )
        ):
            raise ValueError("invalid candidate evaluation")
        if (
            candidate.base_content is not None
            and _normalize_skill_content(candidate.base_content, candidate.name)
            != candidate.base_content
        ):
            raise ValueError("invalid base content")

    def _save_candidate(
        self,
        chat_id: int,
        candidate: SkillCandidate,
        *,
        create: bool = False,
    ) -> None:
        self._validate_candidate(candidate)
        directory = self._prepare_chat_dir(chat_id, "candidates")
        destination = directory / f"{candidate.id}.json"
        _write_json_atomic(destination, _candidate_payload(candidate), create=create)

    def _write_snapshot(
        self,
        chat_id: int,
        candidate: SkillCandidate,
        before: _SkillTreeState,
    ) -> None:
        directory = self._prepare_chat_dir(chat_id, "snapshots")
        destination = directory / f"{candidate.id}.json"
        _write_json_atomic(
            destination,
            {
                "version": _VERSION,
                "candidate_id": candidate.id,
                "base_sha256": candidate.install_base_sha256,
                "base_tree_sha256": candidate.install_base_tree_sha256,
                "content": before.content,
                "support_files": [
                    {"path": path, "content": content}
                    for path, content in before.support_files
                ],
            },
            create=True,
        )

    def _load_snapshot(self, chat_id: int, candidate: SkillCandidate) -> _SkillTreeState:
        path = self._chat_root(chat_id) / "snapshots" / f"{candidate.id}.json"
        if not path.is_file() or path.is_symlink():
            raise SkillEvolutionError("Skill before-state snapshot 不存在")
        try:
            if path.stat().st_size > _MAX_STATE_BYTES:
                raise SkillEvolutionError("Skill before-state snapshot 過大")
            raw_object = cast(
                object,
                json.loads(
                    path.read_text(encoding="ascii"),
                    parse_constant=_reject_json_constant,
                    object_pairs_hook=_reject_duplicate_json_pairs,
                ),
            )
        except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
            raise SkillEvolutionError("Skill before-state snapshot 損壞") from error
        if not isinstance(raw_object, dict):
            raise SkillEvolutionError("Skill before-state snapshot 無效")
        raw = cast(dict[object, object], raw_object)
        content = raw.get("content")
        raw_support = raw.get("support_files", [])
        try:
            support_files = _decode_support_states(raw_support)
        except (AttributeError, TypeError, ValueError) as error:
            raise SkillEvolutionError("Skill before-state snapshot 內容無效") from error
        if (
            raw.get("version") != _VERSION
            or raw.get("candidate_id") != candidate.id
            or raw.get("base_sha256") != (
                candidate.install_base_sha256
                if candidate.install_base_tree_sha256 is not None
                else candidate.base_sha256
            )
            or raw.get("base_tree_sha256") != (
                candidate.install_base_tree_sha256
                if candidate.install_base_tree_sha256 is not None
                else candidate.base_tree_sha256
            )
            or (content is not None and not isinstance(content, str))
            or tuple(path for path, _value in support_files)
            != tuple(path for path, _value in candidate.support_files)
        ):
            raise SkillEvolutionError("Skill before-state snapshot 不符合候選")
        snapshot_hash = (
            candidate.install_base_sha256
            if candidate.install_base_tree_sha256 is not None
            else candidate.base_sha256
        )
        if isinstance(content, str) and _sha256(content) != snapshot_hash:
            raise SkillEvolutionError("Skill before-state snapshot hash 不符合")
        if content is None and snapshot_hash is not None:
            raise SkillEvolutionError("Skill before-state snapshot hash 不符合")
        snapshot_tree = (
            candidate.install_base_tree_sha256
            if candidate.install_base_tree_sha256 is not None
            else candidate.base_tree_sha256
        )
        if snapshot_tree is not None and _tree_sha256(content, support_files) != snapshot_tree:
            raise SkillEvolutionError("Skill before-state tree hash 不符合")
        return _SkillTreeState(
            content if isinstance(content, str) else None,
            support_files,
        )

    def _write_evaluation_report(
        self,
        chat_id: int,
        report: SkillEvaluationReport,
    ) -> None:
        directory = self._prepare_chat_dir(chat_id, "evaluations")
        destination = directory / f"{report.candidate_id}.json"
        _write_json_atomic(
            destination,
            {
                "version": _VERSION,
                "candidate_id": report.candidate_id,
                "proposed_sha256": report.proposed_sha256,
                "evaluated_at": report.evaluated_at,
                "passed_checks": list(report.passed_checks),
                "failed_checks": list(report.failed_checks),
            },
            create=False,
        )

    def _load_evaluation_report(
        self,
        chat_id: int,
        candidate: SkillCandidate,
    ) -> SkillEvaluationReport:
        path = self._chat_root(chat_id) / "evaluations" / f"{candidate.id}.json"
        if not path.is_file() or path.is_symlink():
            raise SkillEvolutionError("Skill 隔離評估報告不存在")
        try:
            if path.stat().st_size > _MAX_STATE_BYTES:
                raise SkillEvolutionError("Skill 隔離評估報告過大")
            raw_object = cast(
                object,
                json.loads(
                    path.read_text(encoding="ascii"),
                    parse_constant=_reject_json_constant,
                    object_pairs_hook=_reject_duplicate_json_pairs,
                ),
            )
        except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
            raise SkillEvolutionError("Skill 隔離評估報告損壞") from error
        if not isinstance(raw_object, dict):
            raise SkillEvolutionError("Skill 隔離評估報告無效")
        raw = cast(dict[object, object], raw_object)
        passed_checks = raw.get("passed_checks")
        failed_checks = raw.get("failed_checks")
        if (
            raw.get("version") != _VERSION
            or raw.get("candidate_id") != candidate.id
            or raw.get("proposed_sha256") != candidate.proposed_sha256
            or raw.get("evaluated_at") != candidate.evaluated_at
            or not isinstance(passed_checks, list)
            or not isinstance(failed_checks, list)
            or not all(isinstance(value, str) for value in (*passed_checks, *failed_checks))
        ):
            raise SkillEvolutionError("Skill 隔離評估報告不符合候選")
        report = SkillEvaluationReport(
            candidate_id=candidate.id,
            proposed_sha256=candidate.proposed_sha256,
            evaluated_at=cast(str, raw["evaluated_at"]),
            passed_checks=tuple(cast(list[str], passed_checks)),
            failed_checks=tuple(cast(list[str], failed_checks)),
        )
        if (
            set(report.passed_checks).intersection(report.failed_checks)
            or not set((*report.passed_checks, *report.failed_checks)).issubset(
                _EVALUATION_CHECKS
            )
            or report.passed != candidate.evaluation_passed
        ):
            raise SkillEvolutionError("Skill 隔離評估報告內容無效")
        return report

    def _write_audit(
        self,
        chat_id: int,
        candidate: SkillCandidate,
        *,
        event: str,
    ) -> None:
        directory = self._prepare_chat_dir(chat_id, "audit")
        destination = directory / f"{self._timestamp_filename()}-{candidate.id}-{event}.json"
        _write_json_atomic(
            destination,
            {
                "version": _VERSION,
                "candidate_id": candidate.id,
                "skill": candidate.name,
                "action": candidate.action,
                "event": event,
                "status": candidate.status,
                "base_sha256": candidate.base_sha256,
                "install_scope": candidate.install_scope,
                "install_base_sha256": candidate.install_base_sha256,
                "install_base_tree_sha256": candidate.install_base_tree_sha256,
                "proposed_sha256": candidate.proposed_sha256,
                "evaluation_passed": candidate.evaluation_passed,
                "at": self._timestamp(),
            },
            create=True,
        )

    def _prepare_chat_dir(self, chat_id: int, name: str) -> Path:
        _validate_chat_id(chat_id)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        _require_real_directory(self.root, "Skill 演化根目錄")
        chat_root = self._chat_root(chat_id)
        chat_root.mkdir(mode=0o700, exist_ok=True)
        _require_real_directory(chat_root, "Skill 演化 chat 目錄")
        directory = chat_root / name
        directory.mkdir(mode=0o700, exist_ok=True)
        _require_real_directory(directory, "Skill 演化資料目錄")
        return directory

    def _chat_root(self, chat_id: int) -> Path:
        _validate_chat_id(chat_id)
        return self.root / str(chat_id)

    def _timestamp(self) -> str:
        value = self._clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _timestamp_filename(self) -> str:
        return self._timestamp().replace(":", "").replace("-", "") + f"-{uuid.uuid4().hex[:8]}"


def _normalize_name(value: str) -> str:
    normalized = value.strip().casefold()
    if _SKILL_NAME.fullmatch(normalized) is None:
        raise SkillEvolutionError("Skill 名稱必須是安全的小寫 slug")
    return normalized


def _valid_timestamp(value: object) -> bool:
    if not isinstance(value, str) or len(value) > 64:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _render_diff(candidate: SkillCandidate) -> str:
    rendered: list[str] = []
    rendered.extend(
        difflib.unified_diff(
            ()
            if candidate.base_content is None
            else candidate.base_content.splitlines(keepends=True),
            candidate.content.splitlines(keepends=True),
            fromfile=(
                "/dev/null"
                if candidate.base_content is None
                else f"a/{candidate.name}/SKILL.md"
            ),
            tofile=f"b/{candidate.name}/SKILL.md",
        )
    )
    before_support = dict(candidate.base_support_files)
    for path, content in candidate.support_files:
        rendered.extend(
            difflib.unified_diff(
                ()
                if before_support[path] is None
                else cast(str, before_support[path]).splitlines(keepends=True),
                content.splitlines(keepends=True),
                fromfile=(
                    "/dev/null"
                    if before_support[path] is None
                    else f"a/{candidate.name}/{path}"
                ),
                tofile=f"b/{candidate.name}/{path}",
            )
        )
    return "".join(rendered) or "(no changes)\n"


def _normalize_summary(value: str) -> str:
    normalized = " ".join(value.split())
    if not 1 <= len(normalized) <= _MAX_SUMMARY_CHARS:
        raise SkillEvolutionError("Skill 候選摘要長度無效")
    if contains_sensitive_text(normalized) or contains_instruction_injection(normalized):
        raise SkillEvolutionError("Skill 候選摘要未通過安全掃描")
    return normalized


def _normalize_skill_content(value: str, expected_name: str) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip() + "\n"
    if not 80 <= len(normalized) <= _MAX_SKILL_CHARS:
        raise SkillEvolutionError("SKILL.md 必須是 80-16000 個字元")
    if any(ord(character) < 32 and character not in {"\n", "\t"} for character in normalized):
        raise SkillEvolutionError("SKILL.md 含有不允許的控制字元")
    if contains_sensitive_text(normalized) or contains_instruction_injection(normalized):
        raise SkillEvolutionError("SKILL.md 未通過 secret/injection 掃描")
    lines = normalized.splitlines()
    if not lines or lines[0] != "---":
        raise SkillEvolutionError("SKILL.md 缺少 YAML frontmatter")
    try:
        closing = lines[1:40].index("---") + 1
    except ValueError:
        raise SkillEvolutionError("SKILL.md frontmatter 未關閉") from None
    fields: dict[str, str] = {}
    for line in lines[1:closing]:
        key, separator, raw_value = line.partition(":")
        if separator:
            fields[key.strip().casefold()] = raw_value.strip().strip("'\"")
    if fields.get("name", "").casefold() != expected_name:
        raise SkillEvolutionError("SKILL.md frontmatter name 與候選不一致")
    description = fields.get("description", "")
    if not 1 <= len(description) <= 300:
        raise SkillEvolutionError("SKILL.md description 長度無效")
    if fields.get(_OWNER_FIELD) != _OWNER_VALUE:
        raise SkillEvolutionError(
            f"SKILL.md 必須標記 `{_OWNER_FIELD}: {_OWNER_VALUE}`"
        )
    if not any(line.strip() for line in lines[closing + 1 :]):
        raise SkillEvolutionError("SKILL.md 正文不能為空")
    return normalized


def _normalize_support_files(value: object) -> SupportFiles:
    """Validate a canonical, bounded set of text resources."""

    if not isinstance(value, tuple) or len(value) > _MAX_SUPPORT_FILES:
        raise ValueError("support file list is invalid")
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    total_bytes = 0
    for item in value:
        if not isinstance(item, tuple) or len(item) != 2:
            raise ValueError("support file entry is invalid")
        path, content = item
        if not isinstance(path, str) or not _safe_support_relative_path(path):
            raise ValueError("support file path is invalid")
        folded_path = path.casefold()
        if folded_path in seen:
            raise ValueError("support file paths must be unique")
        seen.add(folded_path)
        if not isinstance(content, str):
            raise ValueError("support file content is invalid")
        normalized = _normalize_support_content(content)
        if normalized != content:
            raise ValueError("support file content must already be normalized")
        total_bytes += len(content.encode("utf-8"))
        if total_bytes > _MAX_SUPPORT_TOTAL_BYTES:
            raise ValueError("support files are too large")
        pairs.append((path, content))
    if tuple(sorted(pairs, key=lambda pair: pair[0])) != value:
        raise ValueError("support files must be sorted")
    return value


def _canonical_support_files(value: object) -> SupportFiles:
    """Normalize caller/reviewer input before it enters durable state."""

    if not isinstance(value, tuple | list):
        raise ValueError("support file list is invalid")
    pairs: list[tuple[str, str]] = []
    for item in value:
        if not isinstance(item, tuple | list) or len(item) != 2:
            raise ValueError("support file entry is invalid")
        path, content = item
        if not isinstance(path, str) or not _safe_support_relative_path(path):
            raise ValueError("support file path is invalid")
        if not isinstance(content, str):
            raise ValueError("support file content is invalid")
        pairs.append((path, _normalize_support_content(content)))
    result = tuple(sorted(pairs, key=lambda pair: pair[0]))
    _normalize_support_files(result)
    return result


def _normalize_support_states(value: object) -> SupportStates:
    if not isinstance(value, tuple) or len(value) > _MAX_SUPPORT_FILES:
        raise ValueError("support state list is invalid")
    pairs: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    total_bytes = 0
    for item in value:
        if not isinstance(item, tuple) or len(item) != 2:
            raise ValueError("support state entry is invalid")
        path, content = item
        if not isinstance(path, str) or not _safe_support_relative_path(path):
            raise ValueError("support state path is invalid")
        folded_path = path.casefold()
        if folded_path in seen:
            raise ValueError("support state paths must be unique")
        seen.add(folded_path)
        if content is not None:
            if not isinstance(content, str):
                raise ValueError("support state content is invalid")
            normalized = _normalize_support_content(content)
            if normalized != content:
                raise ValueError("support state content must already be normalized")
            total_bytes += len(content.encode("utf-8"))
        pairs.append((path, content))
    if total_bytes > _MAX_SUPPORT_TOTAL_BYTES:
        raise ValueError("support states are too large")
    if tuple(sorted(pairs, key=lambda pair: pair[0])) != value:
        raise ValueError("support states must be sorted")
    return value


def _decode_support_files(raw: object) -> SupportFiles:
    if not isinstance(raw, list):
        raise ValueError("support files must be a list")
    pairs: list[tuple[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("support file entry is invalid")
        mapping = cast(dict[object, object], item)
        path = mapping.get("path")
        content = mapping.get("content")
        if not isinstance(path, str) or not isinstance(content, str):
            raise ValueError("support file entry is invalid")
        pairs.append((path, _normalize_support_content(content)))
    result = tuple(sorted(pairs, key=lambda pair: pair[0]))
    _normalize_support_files(result)
    return result


def _decode_support_states(raw: object) -> SupportStates:
    if not isinstance(raw, list):
        raise ValueError("support states must be a list")
    pairs: list[tuple[str, str | None]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("support state entry is invalid")
        mapping = cast(dict[object, object], item)
        path = mapping.get("path")
        content = mapping.get("content")
        if not isinstance(path, str) or (content is not None and not isinstance(content, str)):
            raise ValueError("support state entry is invalid")
        pairs.append(
            (
                path,
                None if content is None else _normalize_support_content(content),
            )
        )
    result = tuple(sorted(pairs, key=lambda pair: pair[0]))
    _normalize_support_states(result)
    return result


def _safe_support_relative_path(path: str) -> bool:
    return (
        bool(_RELATIVE_FILE.fullmatch(path))
        and not path.startswith(".")
        and path.casefold() != "skill.md"
        and all(part not in {".", ".."} for part in path.split("/"))
    )


def _normalize_support_content(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("support file content is invalid")
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.strip():
        raise ValueError("support file content cannot be empty")
    if any(ord(character) < 32 and character not in {"\n", "\t"} for character in normalized):
        raise ValueError("support file contains a control character")
    if len(normalized.encode("utf-8")) > _MAX_SUPPORT_FILE_BYTES:
        raise ValueError("support file is too large")
    if contains_sensitive_text(normalized) or contains_instruction_injection(normalized):
        raise ValueError("support file failed the secret/injection scan")
    return normalized if normalized.endswith("\n") else normalized + "\n"


def _tree_sha256(content: str | None, support_files: SupportStates) -> str:
    """Hash canonical paths and bytes, including explicit absent markers."""

    _normalize_support_states(support_files)
    digest = hashlib.sha256()
    values: tuple[tuple[str, str | None], ...] = (("SKILL.md", content), *support_files)
    for path, value in values:
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(b"<absent>" if value is None else _sha256(value).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _support_path(skill_root: Path, relative_path: str) -> Path:
    if not _safe_support_relative_path(relative_path):
        raise SkillEvolutionError("Skill support file path is unsafe")
    target = skill_root / relative_path
    try:
        if not target.absolute().is_relative_to(skill_root.absolute()):
            raise SkillEvolutionError("Skill support file escapes its root")
    except ValueError as error:
        raise SkillEvolutionError("Skill support file escapes its root") from error
    return target


def _read_support_file(skill_root: Path, relative_path: str) -> str | None:
    target = _support_path(skill_root, relative_path)
    if skill_root.is_symlink():
        raise SkillEvolutionError("專案 Skill 目錄不安全")
    if not skill_root.exists():
        return None
    _require_real_directory(skill_root, "專案 Skill 目錄")
    if target.is_symlink():
        raise SkillEvolutionError("Skill support file is unsafe")
    if not target.exists():
        return None
    if not target.is_file():
        raise SkillEvolutionError("Skill support file is unsafe")
    try:
        if target.stat().st_size > _MAX_SUPPORT_FILE_BYTES:
            raise SkillEvolutionError("Skill support file is too large")
        content = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise SkillEvolutionError("Skill support file cannot be read safely") from error
    try:
        return _normalize_support_content(content)
    except (ValueError, SkillEvolutionError) as error:
        raise SkillEvolutionError("Skill support file failed the safety scan") from error


def _read_support_states(skill_root: Path, paths: Iterable[str]) -> SupportStates:
    normalized_paths = tuple(sorted(paths))
    if len(normalized_paths) > _MAX_SUPPORT_FILES:
        raise SkillEvolutionError("Skill support file count exceeds the bound")
    states: list[tuple[str, str | None]] = []
    total_bytes = 0
    for path in normalized_paths:
        content = _read_support_file(skill_root, path)
        if content is not None:
            total_bytes += len(content.encode("utf-8"))
        states.append((path, content))
    if total_bytes > _MAX_SUPPORT_TOTAL_BYTES:
        raise SkillEvolutionError("Skill support files exceed the total bound")
    result = tuple(states)
    _normalize_support_states(result)
    return result


def _read_support_tree(skill_root: Path) -> SupportFiles:
    """Read all safe support resources for reviewer context, bounded."""

    if skill_root.is_symlink():
        raise SkillEvolutionError("Skill support tree contains a symlink")
    if not skill_root.exists():
        return ()
    _require_real_directory(skill_root, "專案 Skill 目錄")
    paths: list[str] = []
    try:
        discovered = tuple(sorted(skill_root.rglob("*")))
    except (OSError, RuntimeError) as error:
        raise SkillEvolutionError("Skill support tree cannot be read") from error
    for path in discovered:
        try:
            relative = path.relative_to(skill_root).as_posix()
        except ValueError as error:
            raise SkillEvolutionError("Skill support tree escapes its root") from error
        if path.is_symlink():
            raise SkillEvolutionError("Skill support tree contains a symlink")
        if path.is_dir():
            if not _safe_support_directory(relative):
                raise SkillEvolutionError("Skill support tree contains an unsafe directory")
            continue
        if relative.casefold() == "skill.md":
            continue
        if not _safe_support_relative_path(relative):
            raise SkillEvolutionError("Skill support tree contains an unsafe file")
        paths.append(relative)
    if len(paths) > _MAX_SUPPORT_FILES:
        raise SkillEvolutionError("Skill support tree has too many files")
    states = _read_support_states(skill_root, paths)
    result = tuple((path, content) for path, content in states if content is not None)
    _normalize_support_files(result)
    return result


def _safe_support_directory(relative: str) -> bool:
    return bool(relative) and all(
        bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", part))
        for part in relative.split("/")
    )


def _write_support_file(skill_root: Path, relative_path: str, content: str) -> None:
    try:
        normalized = _normalize_support_content(content)
    except (ValueError, SkillEvolutionError) as error:
        raise SkillEvolutionError("Skill support file failed validation") from error
    target = _support_path(skill_root, relative_path)
    if skill_root.is_symlink() or not skill_root.is_dir():
        raise SkillEvolutionError("Skill support root is unsafe")
    parent = target.parent
    current = skill_root
    for component in target.relative_to(skill_root).parts[:-1]:
        current = current / component
        if current.exists() or current.is_symlink():
            if current.is_symlink() or not current.is_dir():
                raise SkillEvolutionError("Skill support parent is unsafe")
        else:
            current.mkdir(mode=0o700)
        current.chmod(0o700)
    if parent.is_symlink() or not parent.is_dir():
        raise SkillEvolutionError("Skill support parent is unsafe")
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise SkillEvolutionError("Skill support file target is unsafe")
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(normalized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        target.chmod(0o600)
        _fsync_directory(parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def _safe_workspace(workspace: Path) -> Path:
    if not isinstance(workspace, Path):
        raise SkillEvolutionError("Skill 工作區不安全")
    expanded = workspace.expanduser()
    try:
        resolved = expanded.resolve(strict=True)
    except OSError as error:
        raise SkillEvolutionError("Skill 工作區不存在") from error
    if (
        resolved == Path(resolved.anchor)
        or expanded.is_symlink()
        or not resolved.is_dir()
        or resolved != expanded.absolute()
        or ".xclaude" in resolved.parts
        or ".dsh" in resolved.parts
    ):
        raise SkillEvolutionError("Skill 工作區不安全")
    return resolved


def _existing_skills_root(workspace: Path) -> Path | None:
    xdsh_root = workspace / ".xdsh"
    skills_root = xdsh_root / "skills"
    for directory in (xdsh_root, skills_root):
        if not directory.exists():
            return None
        _require_real_directory(directory, "專案 Skill 目錄")
    return skills_root


def _skill_target(workspace: Path, name: str, *, create: bool) -> Path:
    normalized_name = _normalize_name(name)
    xdsh_root = workspace / ".xdsh"
    skills_root = xdsh_root / "skills"
    skill_root = skills_root / normalized_name
    if create:
        for directory in (xdsh_root, skills_root, skill_root):
            if directory.is_symlink():
                raise SkillEvolutionError("專案 Skill 目錄不安全")
            directory.mkdir(mode=0o700, exist_ok=True)
            _require_real_directory(directory, "專案 Skill 目錄")
    else:
        for directory in (xdsh_root, skills_root, skill_root):
            if directory.is_symlink():
                raise SkillEvolutionError("專案 Skill 目錄不安全")
            if not directory.exists():
                return skill_root / "SKILL.md"
            _require_real_directory(directory, "專案 Skill 目錄")
    target = skill_root / "SKILL.md"
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise SkillEvolutionError("SKILL.md 目標不安全")
    return target


def _personal_skill_target(skills_root: Path, name: str, *, create: bool) -> Path:
    normalized_name = _normalize_name(name)
    skill_root = skills_root / normalized_name
    if skills_root.is_symlink():
        raise SkillEvolutionError("個人 Skill 目錄不安全")
    if skill_root.is_symlink():
        raise SkillEvolutionError("個人 Skill 目錄不安全")
    if create:
        _require_real_directory(skills_root, "個人 Skill 目錄")
        skill_root.mkdir(mode=0o700, exist_ok=True)
        _require_real_directory(skill_root, "個人 Skill 目錄")
    elif skill_root.exists():
        _require_real_directory(skill_root, "個人 Skill 目錄")
    target = skill_root / "SKILL.md"
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise SkillEvolutionError("個人 SKILL.md 目標不安全")
    return target


def _read_skill(
    target: Path,
    *,
    expected_name: str,
    require_owned: bool,
) -> str | None:
    if not target.exists():
        return None
    if target.is_symlink() or not target.is_file():
        raise SkillEvolutionError("SKILL.md 目標不安全")
    try:
        if target.stat().st_size > _MAX_SKILL_CHARS * 4:
            raise SkillEvolutionError("既有 SKILL.md 過大")
        content = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise SkillEvolutionError("無法安全讀取既有 SKILL.md") from error
    if require_owned:
        return _normalize_skill_content(content, expected_name)
    return content.replace("\r\n", "\n").replace("\r", "\n").strip() + "\n"


def _write_skill(target: Path, content: str) -> None:
    normalized = _normalize_skill_content(content, target.parent.name.casefold())
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise SkillEvolutionError("SKILL.md 目標不安全")
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(normalized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        target.chmod(0o600)
        _fsync_directory(target.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def _candidate_payload(candidate: SkillCandidate) -> dict[str, object]:
    return {
        "version": _VERSION,
        "id": candidate.id,
        "name": candidate.name,
        "action": candidate.action,
        "summary": candidate.summary,
        "content": candidate.content,
        "base_content": candidate.base_content,
        "base_sha256": candidate.base_sha256,
        "proposed_sha256": candidate.proposed_sha256,
        "workspace": candidate.workspace,
        "origin": candidate.origin,
        "created_at": candidate.created_at,
        "status": candidate.status,
        "decided_at": candidate.decided_at,
        "applied_sha256": candidate.applied_sha256,
        "evaluation_sha256": candidate.evaluation_sha256,
        "evaluated_at": candidate.evaluated_at,
        "evaluation_passed": candidate.evaluation_passed,
        "support_files": [
            {"path": path, "content": content}
            for path, content in candidate.support_files
        ],
        "base_support_files": [
            {"path": path, "content": content}
            for path, content in candidate.base_support_files
        ],
        "base_tree_sha256": candidate.base_tree_sha256,
        "proposed_tree_sha256": candidate.proposed_tree_sha256,
        "applied_tree_sha256": candidate.applied_tree_sha256,
        "install_scope": candidate.install_scope,
        "install_base_sha256": candidate.install_base_sha256,
        "install_base_tree_sha256": candidate.install_base_tree_sha256,
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


def _write_json_atomic(destination: Path, payload: dict[str, object], *, create: bool) -> None:
    content = json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n"
    if create and destination.exists():
        raise SkillEvolutionError("Skill 演化記錄已存在")
    if destination.is_symlink() or (destination.exists() and not destination.is_file()):
        raise SkillEvolutionError("Skill 演化記錄路徑不安全")
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
            raise SkillEvolutionError("Skill 演化記錄已存在")
        os.replace(temporary, destination)
        destination.chmod(0o600)
        _fsync_directory(destination.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def _require_real_directory(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_dir():
        raise SkillEvolutionError(f"{label}不安全")
    path.chmod(0o700)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validate_chat_id(chat_id: int) -> None:
    if chat_id == 0 or isinstance(chat_id, bool):
        raise SkillEvolutionError("Telegram chat ID 無效")


def _sha256(content: str | None) -> str:
    if content is None:
        raise ValueError("cannot hash absent content")
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
