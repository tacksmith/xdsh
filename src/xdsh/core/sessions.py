"""Runner-neutral durable named sessions for local and message transports.

The registry stores product-session identity and presentation settings only.  It
never reads a runner log or a credential, and it deliberately keeps the runtime
session ID opaque to transports.  Telegram keeps its historical chat-scoped
registry for backwards-compatible state files; new transports can use this
module without importing a transport package.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

from xdsh.core.runner import REASONING_EFFORTS, ReasoningEffort

_SESSION_KEY = re.compile(r"^(?:main|[a-z0-9]{8,16})$")
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}$")
_NAMESPACE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_VERSION = 1


class SessionRegistryError(RuntimeError):
    """Session input or durable state is invalid."""


@dataclass(frozen=True, slots=True)
class NamedSession:
    """One user-visible session and its runner-neutral settings."""

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
    """A deletion result that identifies the newly active session."""

    deleted: NamedSession
    current: NamedSession


@dataclass(frozen=True, slots=True)
class _SessionDocument:
    active: str
    sessions: tuple[NamedSession, ...]


class SessionRegistry:
    """Persist named sessions below one explicitly scoped XDSH state root.

    ``namespace`` lets multiple transports share a state root without sharing
    session identity.  Writes are mode-0600 and atomic; malformed state fails
    closed instead of silently creating a replacement session.
    """

    def __init__(
        self,
        state_root: Path,
        *,
        default_model: str,
        namespace: str = "cli",
        max_sessions: int = 20,
        key_factory: Callable[[], str] | None = None,
    ) -> None:
        root = _safe_root(state_root)
        if _NAMESPACE.fullmatch(namespace) is None:
            raise SessionRegistryError("session namespace is invalid")
        if _MODEL_ID.fullmatch(default_model) is None:
            raise SessionRegistryError("default model ID is invalid")
        if not 1 <= max_sessions <= 500:
            raise SessionRegistryError("session limit must be between 1 and 500")
        self.root = root
        self.namespace = namespace
        self.sessions_root = root / "sessions"
        self.default_model = default_model
        self.max_sessions = max_sessions
        self._key_factory = key_factory or (lambda: uuid.uuid4().hex[:8])

    def current(self) -> NamedSession:
        document = self._load()
        return self._by_key(document, document.active)

    def list_sessions(self) -> tuple[NamedSession, ...]:
        return self._load().sessions

    def resolve(self, selector: str) -> NamedSession:
        return self._resolve(self._load(), selector)

    def create(self, name: str | None = None) -> NamedSession:
        document = self._load()
        if len(document.sessions) >= self.max_sessions:
            raise SessionRegistryError(f"at most {self.max_sessions} sessions are allowed")
        normalized_name = self._default_name(document) if name is None else _normalize_name(name)
        self._ensure_unique_name(document, normalized_name)
        key = self._new_key(document)
        source = self._by_key(document, document.active)
        session = NamedSession(
            key=key,
            name=normalized_name,
            runner_session_id=self._runner_session_id(key),
            model=source.model,
            fallback_model=source.fallback_model,
            reasoning_effort=source.reasoning_effort,
            show_thinking=source.show_thinking,
        )
        updated = _SessionDocument(active=key, sessions=(*document.sessions, session))
        self._save(updated)
        return session

    def branch(self, name: str | None = None) -> NamedSession:
        """Create a session with inherited settings and a lazy context source."""

        document = self._load()
        if len(document.sessions) >= self.max_sessions:
            raise SessionRegistryError(f"at most {self.max_sessions} sessions are allowed")
        source = self._by_key(document, document.active)
        normalized_name = (
            self._branch_name(document, source) if name is None else _normalize_name(name)
        )
        self._ensure_unique_name(document, normalized_name)
        key = self._new_key(document)
        session = replace(
            source,
            key=key,
            name=normalized_name,
            runner_session_id=self._runner_session_id(key),
            branch_source_session_id=source.runner_session_id,
        )
        updated = _SessionDocument(active=key, sessions=(*document.sessions, session))
        self._save(updated)
        return session

    def switch(self, selector: str) -> NamedSession:
        document = self._load()
        session = self._resolve(document, selector)
        if session.key != document.active:
            self._save(_SessionDocument(session.key, document.sessions))
        return session

    def rename(self, selector: str, name: str) -> NamedSession:
        document = self._load()
        target = self._resolve(document, selector)
        normalized_name = _normalize_name(name)
        self._ensure_unique_name(document, normalized_name, except_key=target.key)
        renamed = replace(target, name=normalized_name)
        self._save(
            _SessionDocument(
                document.active,
                tuple(renamed if item.key == target.key else item for item in document.sessions),
            )
        )
        return renamed

    def delete(self, selector: str) -> SessionDeletion:
        document = self._load()
        if len(document.sessions) == 1:
            raise SessionRegistryError("cannot delete the last session")
        deleted = self._resolve(document, selector)
        remaining = tuple(item for item in document.sessions if item.key != deleted.key)
        active = remaining[0].key if document.active == deleted.key else document.active
        updated = _SessionDocument(active, remaining)
        self._save(updated)
        return SessionDeletion(deleted=deleted, current=self._by_key(updated, active))

    def select_model(self, model: str) -> NamedSession:
        _validate_model(model)
        return self._update_current(model=model.strip())

    def select_fallback(self, model: str | None) -> NamedSession:
        normalized = model.strip() if model is not None else None
        if normalized is not None:
            _validate_model(normalized)
        return self._update_current(fallback_model=normalized)

    def select_reasoning(self, reasoning_effort: ReasoningEffort) -> NamedSession:
        if reasoning_effort not in REASONING_EFFORTS:
            raise SessionRegistryError("reasoning effort is invalid")
        return self._update_current(reasoning_effort=reasoning_effort)

    def set_show_thinking(self, show: bool) -> NamedSession:
        if not isinstance(show, bool):
            raise SessionRegistryError("thinking setting must be boolean")
        return self._update_current(show_thinking=show)

    def _update_current(self, **changes: object) -> NamedSession:
        document = self._load()
        current = self._by_key(document, document.active)
        selected = NamedSession(
            key=current.key,
            name=current.name,
            runner_session_id=current.runner_session_id,
            model=cast(str, changes.get("model", current.model)),
            fallback_model=cast(
                str | None,
                changes.get("fallback_model", current.fallback_model),
            ),
            reasoning_effort=cast(
                ReasoningEffort,
                changes.get("reasoning_effort", current.reasoning_effort),
            ),
            show_thinking=cast(bool, changes.get("show_thinking", current.show_thinking)),
            branch_source_session_id=current.branch_source_session_id,
        )
        self._save(
            _SessionDocument(
                document.active,
                tuple(selected if item.key == current.key else item for item in document.sessions),
            )
        )
        return selected

    def _load(self) -> _SessionDocument:
        path = self._path()
        if not path.exists():
            session = NamedSession(
                key="main",
                name="Main",
                runner_session_id=self._runner_session_id("main"),
                model=self.default_model,
            )
            document = _SessionDocument(active="main", sessions=(session,))
            self._save(document)
            return document
        if path.is_symlink():
            raise SessionRegistryError("session state cannot be a symlink")
        try:
            payload = cast(object, json.loads(path.read_text(encoding="ascii")))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SessionRegistryError("session state is corrupt") from error
        if not isinstance(payload, dict):
            raise SessionRegistryError("session state must be a JSON object")
        raw = cast(dict[object, object], payload)
        if raw.get("version") != _VERSION:
            raise SessionRegistryError("unsupported session state version")
        active = raw.get("active")
        raw_sessions = raw.get("sessions")
        if not isinstance(active, str) or not isinstance(raw_sessions, list):
            raise SessionRegistryError("session state is missing active or sessions")
        sessions = tuple(self._decode(item) for item in raw_sessions)
        if not sessions:
            raise SessionRegistryError("session state cannot be empty")
        keys = [item.key for item in sessions]
        names = [item.name.casefold() for item in sessions]
        if len(keys) != len(set(keys)) or len(names) != len(set(names)):
            raise SessionRegistryError("session state contains duplicate entries")
        document = _SessionDocument(active=active, sessions=sessions)
        self._by_key(document, active)
        return document

    def _decode(self, value: object) -> NamedSession:
        if not isinstance(value, dict):
            raise SessionRegistryError("session entry format is invalid")
        raw = cast(dict[object, object], value)
        key = raw.get("key")
        name = raw.get("name")
        runner_session_id = raw.get("runner_session_id")
        model = raw.get("model")
        fallback_model = raw.get("fallback_model")
        reasoning_effort = raw.get("reasoning_effort", "off")
        show_thinking = raw.get("show_thinking", False)
        branch_source = raw.get("branch_source_session_id")
        if not all(isinstance(item, str) for item in (key, name, runner_session_id, model)):
            raise SessionRegistryError("session entry fields are invalid")
        key = cast(str, key)
        name = cast(str, name)
        runner_session_id = cast(str, runner_session_id)
        model = cast(str, model)
        if _SESSION_KEY.fullmatch(key) is None or name != _normalize_name(name):
            raise SessionRegistryError("session key or name is invalid")
        if runner_session_id != self._runner_session_id(key):
            raise SessionRegistryError("runner session ID is outside this namespace")
        _validate_model(model)
        if fallback_model is not None:
            if not isinstance(fallback_model, str):
                raise SessionRegistryError("fallback model is invalid")
            _validate_model(fallback_model)
        if reasoning_effort not in REASONING_EFFORTS:
            raise SessionRegistryError("reasoning effort is invalid")
        if not isinstance(show_thinking, bool):
            raise SessionRegistryError("thinking setting is invalid")
        if branch_source is not None and (
            not isinstance(branch_source, str)
            or not (
                branch_source == f"{self.namespace}-session"
                or _valid_child_runner_id(self.namespace, branch_source)
            )
            or branch_source == runner_session_id
        ):
            raise SessionRegistryError("branch source is invalid")
        return NamedSession(
            key=key,
            name=name,
            runner_session_id=runner_session_id,
            model=model,
            fallback_model=fallback_model,
            reasoning_effort=cast(ReasoningEffort, reasoning_effort),
            show_thinking=show_thinking,
            branch_source_session_id=branch_source,
        )

    def _save(self, document: _SessionDocument) -> None:
        payload = {
            "version": _VERSION,
            "active": document.active,
            "sessions": [
                {
                    "key": item.key,
                    "name": item.name,
                    "runner_session_id": item.runner_session_id,
                    "model": item.model,
                    "fallback_model": item.fallback_model,
                    "reasoning_effort": item.reasoning_effort,
                    "show_thinking": item.show_thinking,
                    "branch_source_session_id": item.branch_source_session_id,
                }
                for item in document.sessions
            ],
        }
        content = json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n"
        self._prepare()
        destination = self._path()
        if destination.is_symlink():
            raise SessionRegistryError("session state cannot be a symlink")
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="ascii") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            os.chmod(destination, 0o600)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _prepare(self) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.sessions_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.sessions_root.chmod(0o700)

    def _path(self) -> Path:
        return self.sessions_root / f"{self.namespace}.json"

    def _runner_session_id(self, key: str) -> str:
        return f"{self.namespace}-session" if key == "main" else f"{self.namespace}-session-{key}"

    def _resolve(self, document: _SessionDocument, selector: str) -> NamedSession:
        normalized = selector.strip()
        if not normalized:
            raise SessionRegistryError("session ID or name is required")
        matches = [
            item
            for item in document.sessions
            if item.key == normalized or item.name.casefold() == normalized.casefold()
        ]
        if len(matches) != 1:
            raise SessionRegistryError(f"session does not exist: {normalized}")
        return matches[0]

    def _by_key(self, document: _SessionDocument, key: str) -> NamedSession:
        selected = next((item for item in document.sessions if item.key == key), None)
        if selected is None:
            raise SessionRegistryError("active session does not exist")
        return selected

    def _new_key(self, document: _SessionDocument) -> str:
        existing = {item.key for item in document.sessions}
        for _ in range(20):
            try:
                candidate = self._key_factory()
            except (RuntimeError, StopIteration) as error:
                raise SessionRegistryError("cannot create a session ID") from error
            if _SESSION_KEY.fullmatch(candidate) is not None and candidate not in existing:
                return candidate
        raise SessionRegistryError("cannot create a unique session ID")

    def _default_name(self, document: _SessionDocument) -> str:
        existing = {item.name.casefold() for item in document.sessions}
        number = len(document.sessions) + 1
        while f"Session {number}".casefold() in existing:
            number += 1
        return f"Session {number}"

    def _branch_name(self, document: _SessionDocument, source: NamedSession) -> str:
        existing = {item.name.casefold() for item in document.sessions}
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
            item.key != except_key and item.name.casefold() == name.casefold()
            for item in document.sessions
        ):
            raise SessionRegistryError(f"session name already exists: {name}")


def _safe_root(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if resolved in {Path(resolved.anchor), Path.home().resolve()}:
        raise SessionRegistryError("session state root must be a dedicated directory")
    if ".xclaude" in resolved.parts or ".dsh" in resolved.parts:
        raise SessionRegistryError("session state root is reserved or unsafe")
    return resolved


def _normalize_name(name: str) -> str:
    normalized = " ".join(name.split())
    if (
        not 1 <= len(normalized) <= 48
        or normalized != name.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in name)
    ):
        raise SessionRegistryError("session name must contain 1-48 visible characters")
    return normalized


def _validate_model(model: str) -> None:
    if _MODEL_ID.fullmatch(model) is None:
        raise SessionRegistryError("model ID is invalid")


def _valid_child_runner_id(namespace: str, value: str) -> bool:
    prefix = f"{namespace}-session-"
    return bool(
        value.startswith(prefix)
        and _SESSION_KEY.fullmatch(value.removeprefix(prefix)) is not None
    )
