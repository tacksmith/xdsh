"""Durable per-chat product sessions above ephemeral DSH runtime sessions."""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from xdsh.core.runner import REASONING_EFFORTS, ReasoningEffort

_SESSION_KEY = re.compile(r"^(?:main|[a-z0-9]{8,16})$")
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}$")
_VERSION = 3
_REASONING_VERSION = 2
_LEGACY_VERSION = 1


class SessionRegistryError(RuntimeError):
    """Session input or durable state is invalid."""

    def __init__(self, message: str, *, code: str = "state", limit: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.limit = limit


@dataclass(frozen=True, slots=True)
class TelegramSession:
    """One named product session with its own DSH identity and selected model."""

    key: str
    name: str
    runner_session_id: str
    model: str
    fallback_model: str | None = None
    reasoning_effort: ReasoningEffort = "off"
    show_thinking: bool = False
    branch_source_session_id: str | None = None


@dataclass(frozen=True, slots=True)
class SessionDeletion:
    """Deletion result; DSH logs remain outside the registry."""

    deleted: TelegramSession
    current: TelegramSession


@dataclass(frozen=True, slots=True)
class _SessionDocument:
    active: str
    sessions: tuple[TelegramSession, ...]


class TelegramSessionRegistry:
    """Own session identity, selection, model state, migration, and persistence."""

    def __init__(
        self,
        telegram_state_root: Path,
        *,
        default_model: str,
        max_sessions: int = 500,
        key_factory: Callable[[], str] | None = None,
    ) -> None:
        root = telegram_state_root.expanduser().resolve()
        if root == Path(root.anchor) or ".xclaude" in root.parts or ".dsh" in root.parts:
            raise SessionRegistryError("會話狀態根目錄不安全")
        if _MODEL_ID.fullmatch(default_model) is None:
            raise SessionRegistryError("預設模型 ID 無效")
        if type(max_sessions) is not int or not 1 <= max_sessions <= 500:
            raise SessionRegistryError("會話數上限必須為 1 至 500 的整數")
        self.root = root
        self.sessions_root = root / "chat-sessions"
        self.legacy_models_root = root / "chat-models"
        self.default_model = default_model
        self.max_sessions = max_sessions
        self._key_factory = key_factory or (lambda: uuid.uuid4().hex[:8])

    def current(self, chat_id: int) -> TelegramSession:
        document = self._load(chat_id)
        return self._by_key(document, document.active)

    def list_sessions(self, chat_id: int) -> tuple[TelegramSession, ...]:
        return self._load(chat_id).sessions

    def resolve(self, chat_id: int, selector: str) -> TelegramSession:
        return self._resolve(self._load(chat_id), selector)

    def create(self, chat_id: int, name: str | None = None) -> TelegramSession:
        document = self._load(chat_id)
        if len(document.sessions) >= self.max_sessions:
            raise SessionRegistryError(
                f"每個聊天最多保留 {self.max_sessions} 個會話",
                code="limit", limit=self.max_sessions,
            )
        normalized_name = (
            self._default_name(document) if name is None else _normalize_name(name)
        )
        self._ensure_unique_name(document, normalized_name)
        key = self._new_key(document)
        session = TelegramSession(
            key=key,
            name=normalized_name,
            runner_session_id=_runner_session_id(chat_id, key),
            model=self._by_key(document, document.active).model,
            fallback_model=self._by_key(document, document.active).fallback_model,
            reasoning_effort=self._by_key(document, document.active).reasoning_effort,
            show_thinking=self._by_key(document, document.active).show_thinking,
        )
        updated = _SessionDocument(
            active=key,
            sessions=(*document.sessions, session),
        )
        self._save(chat_id, updated)
        return session

    def branch(self, chat_id: int, name: str | None = None) -> TelegramSession:
        """Create and select a session whose bounded context is cloned lazily."""

        document = self._load(chat_id)
        if len(document.sessions) >= self.max_sessions:
            raise SessionRegistryError(
                f"每個聊天最多保留 {self.max_sessions} 個會話",
                code="limit", limit=self.max_sessions,
            )
        source = self._by_key(document, document.active)
        normalized_name = (
            self._branch_name(document, source)
            if name is None
            else _normalize_name(name)
        )
        self._ensure_unique_name(document, normalized_name)
        key = self._new_key(document)
        session = TelegramSession(
            key=key,
            name=normalized_name,
            runner_session_id=_runner_session_id(chat_id, key),
            model=source.model,
            fallback_model=source.fallback_model,
            reasoning_effort=source.reasoning_effort,
            show_thinking=source.show_thinking,
            branch_source_session_id=source.runner_session_id,
        )
        self._save(
            chat_id,
            _SessionDocument(
                active=key,
                sessions=(*document.sessions, session),
            ),
        )
        return session

    def switch(self, chat_id: int, selector: str) -> TelegramSession:
        document = self._load(chat_id)
        session = self._resolve(document, selector)
        if session.key != document.active:
            self._save(chat_id, _SessionDocument(session.key, document.sessions))
        return session

    def rename(self, chat_id: int, selector: str, name: str) -> TelegramSession:
        document = self._load(chat_id)
        target = self._resolve(document, selector)
        normalized_name = _normalize_name(name)
        self._ensure_unique_name(document, normalized_name, except_key=target.key)
        renamed = TelegramSession(
            key=target.key,
            name=normalized_name,
            runner_session_id=target.runner_session_id,
            model=target.model,
            fallback_model=target.fallback_model,
            reasoning_effort=target.reasoning_effort,
            show_thinking=target.show_thinking,
            branch_source_session_id=target.branch_source_session_id,
        )
        sessions = tuple(renamed if item.key == target.key else item for item in document.sessions)
        self._save(chat_id, _SessionDocument(document.active, sessions))
        return renamed

    def delete(self, chat_id: int, selector: str) -> SessionDeletion:
        document = self._load(chat_id)
        if len(document.sessions) == 1:
            raise SessionRegistryError("不能刪除最後一個會話", code="last")
        deleted = self._resolve(document, selector)
        remaining = tuple(session for session in document.sessions if session.key != deleted.key)
        active = remaining[0].key if document.active == deleted.key else document.active
        updated = _SessionDocument(active, remaining)
        self._save(chat_id, updated)
        return SessionDeletion(deleted=deleted, current=self._by_key(updated, active))

    def clear_all(self, chat_id: int) -> TelegramSession:
        """Atomically retire this chat's list; preserve files and start fresh context."""
        document = self._load(chat_id)
        current = self._by_key(document, document.active)
        key = self._new_key(document)
        fresh = TelegramSession(
            key=key,
            name="Session 1",
            runner_session_id=_runner_session_id(chat_id, key),
            model=current.model,
            fallback_model=current.fallback_model,
            reasoning_effort=current.reasoning_effort,
            show_thinking=current.show_thinking,
        )
        self._save(chat_id, _SessionDocument(key, (fresh,)))
        return fresh

    def select_model(self, chat_id: int, model: str) -> TelegramSession:
        if _MODEL_ID.fullmatch(model) is None:
            raise SessionRegistryError("模型 ID 無效")
        document = self._load(chat_id)
        current = self._by_key(document, document.active)
        selected = TelegramSession(
            key=current.key,
            name=current.name,
            runner_session_id=current.runner_session_id,
            model=model,
            fallback_model=current.fallback_model,
            reasoning_effort=current.reasoning_effort,
            show_thinking=current.show_thinking,
            branch_source_session_id=current.branch_source_session_id,
        )
        sessions = tuple(
            selected if session.key == current.key else session for session in document.sessions
        )
        self._save(chat_id, _SessionDocument(document.active, sessions))
        return selected

    def select_fallback(self, chat_id: int, model: str | None) -> TelegramSession:
        normalized = model.strip() if model is not None else None
        if normalized is not None and _MODEL_ID.fullmatch(normalized) is None:
            raise SessionRegistryError("備用模型 ID 無效")
        document = self._load(chat_id)
        current = self._by_key(document, document.active)
        selected = TelegramSession(
            key=current.key,
            name=current.name,
            runner_session_id=current.runner_session_id,
            model=current.model,
            fallback_model=normalized,
            reasoning_effort=current.reasoning_effort,
            show_thinking=current.show_thinking,
            branch_source_session_id=current.branch_source_session_id,
        )
        sessions = tuple(
            selected if session.key == current.key else session for session in document.sessions
        )
        self._save(chat_id, _SessionDocument(document.active, sessions))
        return selected

    def select_reasoning(
        self,
        chat_id: int,
        reasoning_effort: ReasoningEffort,
    ) -> TelegramSession:
        if reasoning_effort not in REASONING_EFFORTS:
            raise SessionRegistryError("推理強度無效")
        document = self._load(chat_id)
        current = self._by_key(document, document.active)
        selected = TelegramSession(
            key=current.key,
            name=current.name,
            runner_session_id=current.runner_session_id,
            model=current.model,
            fallback_model=current.fallback_model,
            reasoning_effort=reasoning_effort,
            show_thinking=current.show_thinking,
            branch_source_session_id=current.branch_source_session_id,
        )
        sessions = tuple(
            selected if session.key == current.key else session for session in document.sessions
        )
        self._save(chat_id, _SessionDocument(document.active, sessions))
        return selected

    def set_show_thinking(self, chat_id: int, show: bool) -> TelegramSession:
        document = self._load(chat_id)
        current = self._by_key(document, document.active)
        selected = TelegramSession(
            key=current.key,
            name=current.name,
            runner_session_id=current.runner_session_id,
            model=current.model,
            fallback_model=current.fallback_model,
            reasoning_effort=current.reasoning_effort,
            show_thinking=show,
            branch_source_session_id=current.branch_source_session_id,
        )
        sessions = tuple(
            selected if session.key == current.key else session for session in document.sessions
        )
        self._save(chat_id, _SessionDocument(document.active, sessions))
        return selected

    def _load(self, chat_id: int) -> _SessionDocument:
        path = self._path(chat_id)
        if not path.exists():
            session = TelegramSession(
                key="main",
                name="Main",
                runner_session_id=_runner_session_id(chat_id, "main"),
                model=self._legacy_model(chat_id) or self.default_model,
            )
            document = _SessionDocument(active="main", sessions=(session,))
            self._save(chat_id, document)
            return document
        try:
            payload = cast(object, json.loads(path.read_text(encoding="ascii")))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SessionRegistryError("會話狀態檔損壞") from error
        if not isinstance(payload, dict):
            raise SessionRegistryError("會話狀態必須是 JSON object")
        raw = cast(dict[object, object], payload)
        version = raw.get("version")
        if version not in {_LEGACY_VERSION, _REASONING_VERSION, _VERSION}:
            raise SessionRegistryError("不支援的會話狀態版本")
        active = raw.get("active")
        raw_sessions = raw.get("sessions")
        if not isinstance(active, str) or not isinstance(raw_sessions, list):
            raise SessionRegistryError("會話狀態缺少 active 或 sessions")
        sessions = tuple(
            self._decode_session(
                chat_id,
                item,
                version=cast(int, version),
            )
            for item in raw_sessions
        )
        if not sessions:
            raise SessionRegistryError("會話狀態不能為空")
        keys = [session.key for session in sessions]
        names = [session.name.casefold() for session in sessions]
        if len(keys) != len(set(keys)) or len(names) != len(set(names)):
            raise SessionRegistryError("會話狀態包含重複項目")
        document = _SessionDocument(active=active, sessions=sessions)
        self._by_key(document, active)
        if version != _VERSION:
            self._save(chat_id, document)
        return document

    def _decode_session(
        self,
        chat_id: int,
        value: object,
        *,
        version: int,
    ) -> TelegramSession:
        if not isinstance(value, dict):
            raise SessionRegistryError("會話項目格式無效")
        raw = cast(dict[object, object], value)
        key = raw.get("key")
        name = raw.get("name")
        runner_session_id = raw.get("runner_session_id")
        model = raw.get("model")
        fallback_model = None if version < _VERSION else raw.get("fallback_model")
        reasoning_effort = (
            "off" if version == _LEGACY_VERSION else raw.get("reasoning_effort")
        )
        show_thinking = False if version == _LEGACY_VERSION else raw.get("show_thinking")
        branch_source_session_id = raw.get("branch_source_session_id")
        if not all(isinstance(item, str) for item in (key, name, runner_session_id, model)):
            raise SessionRegistryError("會話項目欄位無效")
        key = cast(str, key)
        name = cast(str, name)
        runner_session_id = cast(str, runner_session_id)
        model = cast(str, model)
        if _SESSION_KEY.fullmatch(key) is None or name != _normalize_name(name):
            raise SessionRegistryError("會話 key 或名稱無效")
        if runner_session_id != _runner_session_id(chat_id, key):
            raise SessionRegistryError("會話 runner ID 不符合聊天範圍")
        if _MODEL_ID.fullmatch(model) is None:
            raise SessionRegistryError("會話模型 ID 無效")
        if fallback_model is not None and (
            not isinstance(fallback_model, str)
            or _MODEL_ID.fullmatch(fallback_model) is None
        ):
            raise SessionRegistryError("會話備用模型 ID 無效")
        if reasoning_effort not in REASONING_EFFORTS:
            raise SessionRegistryError("會話推理強度無效")
        if not isinstance(show_thinking, bool):
            raise SessionRegistryError("會話 thinking 顯示設定無效")
        if branch_source_session_id is not None and (
            not isinstance(branch_source_session_id, str)
            or not _is_runner_session_id(chat_id, branch_source_session_id)
            or branch_source_session_id == runner_session_id
        ):
            raise SessionRegistryError("會話分支來源無效")
        return TelegramSession(
            key=key,
            name=name,
            runner_session_id=runner_session_id,
            model=model,
            fallback_model=fallback_model,
            reasoning_effort=cast(ReasoningEffort, reasoning_effort),
            show_thinking=show_thinking,
            branch_source_session_id=branch_source_session_id,
        )

    def _save(self, chat_id: int, document: _SessionDocument) -> None:
        payload = {
            "version": _VERSION,
            "active": document.active,
            "sessions": [
                {
                    "key": session.key,
                    "name": session.name,
                    "runner_session_id": session.runner_session_id,
                    "model": session.model,
                    "fallback_model": session.fallback_model,
                    "reasoning_effort": session.reasoning_effort,
                    "show_thinking": session.show_thinking,
                    "branch_source_session_id": session.branch_source_session_id,
                }
                for session in document.sessions
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

    def _legacy_model(self, chat_id: int) -> str | None:
        path = self.legacy_models_root / f"{chat_id}.model"
        if not path.exists():
            return None
        try:
            model = path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeDecodeError) as error:
            raise SessionRegistryError("舊模型狀態無法讀取") from error
        if _MODEL_ID.fullmatch(model) is None:
            raise SessionRegistryError("舊模型狀態無效")
        return model

    def _resolve(self, document: _SessionDocument, selector: str) -> TelegramSession:
        normalized = selector.strip()
        if not normalized:
            raise SessionRegistryError("請提供會話 ID 或名稱", code="missing")
        matches = [
            session
            for session in document.sessions
            if session.key == normalized or session.name.casefold() == normalized.casefold()
        ]
        if len(matches) != 1:
            raise SessionRegistryError(f"會話不存在: {normalized}", code="missing")
        return matches[0]

    def _by_key(self, document: _SessionDocument, key: str) -> TelegramSession:
        session = next((item for item in document.sessions if item.key == key), None)
        if session is None:
            raise SessionRegistryError("目前會話不存在")
        return session

    def _new_key(self, document: _SessionDocument) -> str:
        existing = {session.key for session in document.sessions}
        for _ in range(20):
            try:
                candidate = self._key_factory()
            except (StopIteration, RuntimeError) as error:
                raise SessionRegistryError("無法產生新的會話 ID") from error
            if _SESSION_KEY.fullmatch(candidate) is not None and candidate not in existing:
                return candidate
        raise SessionRegistryError("無法產生唯一的會話 ID")

    def _default_name(self, document: _SessionDocument) -> str:
        existing = {session.name.casefold() for session in document.sessions}
        number = len(document.sessions) + 1
        while f"Session {number}".casefold() in existing:
            number += 1
        return f"Session {number}"

    def _branch_name(
        self,
        document: _SessionDocument,
        source: TelegramSession,
    ) -> str:
        existing = {session.name.casefold() for session in document.sessions}
        number = 1
        while True:
            suffix = " branch" if number == 1 else f" branch {number}"
            stem = source.name[: 48 - len(suffix)].rstrip() or "Session"
            candidate = _normalize_name(f"{stem}{suffix}")
            if candidate.casefold() not in existing:
                return candidate
            number += 1

    def _ensure_unique_name(
        self,
        document: _SessionDocument,
        name: str,
        *,
        except_key: str | None = None,
    ) -> None:
        if any(
            session.key != except_key and session.name.casefold() == name.casefold()
            for session in document.sessions
        ):
            raise SessionRegistryError(f"會話名稱已存在: {name}", code="duplicate")

    def _prepare(self) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.sessions_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.sessions_root.chmod(0o700)

    def _path(self, chat_id: int) -> Path:
        if chat_id == 0:
            raise SessionRegistryError("Telegram chat ID 不能為零")
        return self.sessions_root / f"{chat_id}.json"


def _normalize_name(name: str) -> str:
    normalized = " ".join(name.split())
    if (
        not 1 <= len(normalized) <= 48
        or normalized != name.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in name)
    ):
        raise SessionRegistryError("會話名稱必須是 1-48 個可見字元", code="name")
    return normalized


def _runner_session_id(chat_id: int, key: str) -> str:
    base = f"telegram-chat-{chat_id}"
    return base if key == "main" else f"{base}-s-{key}"


def _is_runner_session_id(chat_id: int, value: str) -> bool:
    base = f"telegram-chat-{chat_id}"
    if value == base:
        return True
    prefix = f"{base}-s-"
    if not value.startswith(prefix):
        return False
    key = value.removeprefix(prefix)
    return key != "main" and _SESSION_KEY.fullmatch(key) is not None
