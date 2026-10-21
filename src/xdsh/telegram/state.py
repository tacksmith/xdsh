"""Private, durable Telegram polling state and single-instance locking."""

from __future__ import annotations

import fcntl
import os
import time
import uuid
from pathlib import Path
from types import TracebackType

from xdsh.core.i18n import canonical_language
from xdsh.core.icons import IconIntensity, canonical_icon_intensity


class TelegramStateError(RuntimeError):
    """Telegram state is invalid or already owned by another process."""


class TelegramState:
    """Own offset, heartbeat, and lock files below one mode-0700 directory."""

    def __init__(self, state_root: Path) -> None:
        self.root = state_root.expanduser().resolve() / "telegram"
        self.models_root = self.root / "chat-models"
        self.languages_root = self.root / "chat-languages"
        self.icons_root = self.root / "chat-icons"
        self.offset_path = self.root / "update-offset"
        self.heartbeat_path = self.root / "heartbeat"
        self.poll_heartbeat_path = self.root / "poll-heartbeat"
        self.lock_path = self.root / "bot.lock"

    def prepare(self) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.models_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.models_root.chmod(0o700)
        self.languages_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.languages_root.chmod(0o700)
        self.icons_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.icons_root.chmod(0o700)

    def load_offset(self) -> int | None:
        if not self.offset_path.exists():
            return None
        try:
            value = int(self.offset_path.read_text(encoding="ascii").strip())
        except (OSError, ValueError) as error:
            raise TelegramStateError("invalid Telegram update offset") from error
        if value < 0:
            raise TelegramStateError("Telegram update offset must not be negative")
        return value

    def load_speech_choice(self, chat_id: int) -> str | None:
        path = self.root / f"speech-{chat_id}.choice"
        if not path.exists():
            return None
        if path.is_symlink() or path.stat().st_size > 128:
            raise TelegramStateError("Unsafe speech preference")
        return path.read_text(encoding="ascii").strip()

    def save_speech_choice(self, chat_id: int, choice: str) -> None:
        if len(choice) > 100 or not choice.isascii():
            raise TelegramStateError("Invalid speech preference")
        self._atomic_write(self.root / f"speech-{chat_id}.choice", choice + "\n")

    def save_offset(self, offset: int) -> None:
        if offset < 0:
            raise TelegramStateError("Telegram update offset must not be negative")
        self._atomic_write(self.offset_path, f"{offset}\n")

    def touch_heartbeat(self, *, now: float | None = None) -> None:
        timestamp = time.time() if now is None else now
        self._atomic_write(self.heartbeat_path, f"{timestamp:.6f}\n")

    def heartbeat_age(self, *, now: float | None = None) -> float:
        if self.heartbeat_path.is_symlink() or not self.heartbeat_path.is_file():
            raise TelegramStateError("Telegram heartbeat is missing or unsafe")
        try:
            timestamp = float(self.heartbeat_path.read_text(encoding="ascii").strip())
        except (OSError, ValueError) as error:
            raise TelegramStateError("Telegram heartbeat is missing or invalid") from error
        return (time.time() if now is None else now) - timestamp

    def touch_poll_heartbeat(self, *, now: float | None = None) -> None:
        timestamp = time.time() if now is None else now
        self._atomic_write(self.poll_heartbeat_path, f"{timestamp:.6f}\n")

    def poll_heartbeat_age(self) -> float | None:
        if self.poll_heartbeat_path.is_symlink():
            raise TelegramStateError("Telegram polling heartbeat is unsafe")
        if not self.poll_heartbeat_path.exists():
            return None  # Backward-compatible inspection of older installations.
        try:
            return time.time() - float(self.poll_heartbeat_path.read_text(encoding="ascii"))
        except (OSError, ValueError) as error:
            raise TelegramStateError("Telegram polling heartbeat is invalid") from error

    def load_model(self, chat_id: int) -> str | None:
        path = self._model_path(chat_id)
        if not path.exists():
            return None
        try:
            model = path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeDecodeError) as error:
            raise TelegramStateError("invalid Telegram chat model state") from error
        if not model or any(character.isspace() for character in model):
            raise TelegramStateError("invalid Telegram chat model state")
        return model

    def save_model(self, chat_id: int, model: str) -> None:
        if not model or any(character.isspace() for character in model):
            raise TelegramStateError("invalid Telegram chat model")
        self._atomic_write(self._model_path(chat_id), f"{model}\n")

    def load_language(self, chat_id: int) -> str | None:
        """Return a chat's explicit language without inventing a default."""

        path = self._language_path(chat_id)
        if not path.exists():
            return None
        try:
            value = path.read_text(encoding="ascii").strip()
            return canonical_language(value)
        except (OSError, UnicodeDecodeError, ValueError) as error:
            raise TelegramStateError("invalid Telegram chat language state") from error

    def save_language(self, chat_id: int, language: str) -> str:
        """Validate and atomically persist one canonical language code."""

        try:
            canonical = canonical_language(language)
        except ValueError as error:
            raise TelegramStateError("invalid Telegram chat language") from error
        self._atomic_write(self._language_path(chat_id), f"{canonical}\n")
        return canonical

    def load_icon_intensity(self, chat_id: int) -> IconIntensity | None:
        path = self._icon_path(chat_id)
        if not path.exists():
            return None
        try:
            value = path.read_text(encoding="ascii").strip()
            return canonical_icon_intensity(value)
        except (OSError, UnicodeDecodeError, ValueError) as error:
            raise TelegramStateError("invalid Telegram chat icon state") from error

    def save_icon_intensity(self, chat_id: int, intensity: str) -> IconIntensity:
        try:
            canonical = canonical_icon_intensity(intensity)
        except ValueError as error:
            raise TelegramStateError("invalid Telegram chat icon intensity") from error
        self._atomic_write(self._icon_path(chat_id), f"{canonical}\n")
        return canonical

    def lock(self) -> TelegramInstanceLock:
        return TelegramInstanceLock(self.lock_path)

    def load_resource_limits(self, chat_id: int) -> str | None:
        path = self._resource_limits_path(chat_id)
        if not path.exists():
            return None
        if path.is_symlink():
            raise TelegramStateError("unsafe resource limits state")
        value = path.read_text(encoding="ascii").strip()
        if value not in {"on", "off"}:
            raise TelegramStateError("invalid resource limits state")
        return value

    def load_process_mode(self, chat_id: int) -> str:
        path = self._resource_limits_path(chat_id).with_suffix(".process-mode")
        if not path.exists():
            return "key"
        if path.is_symlink():
            raise TelegramStateError("unsafe process mode state")
        value = path.read_text(encoding="ascii").strip()
        if value not in {"off", "key", "all"}:
            raise TelegramStateError("invalid process mode state")
        return value

    def save_process_mode(self, chat_id: int, mode: str) -> None:
        if mode not in {"off", "key", "all"}:
            raise TelegramStateError("invalid process mode")
        self._atomic_write(self._resource_limits_path(chat_id).with_suffix(".process-mode"),
                           mode + "\n")

    def save_resource_limits(self, chat_id: int, value: str) -> None:
        if value not in {"on", "off"}:
            raise TelegramStateError("invalid resource limits mode")
        self._atomic_write(self._resource_limits_path(chat_id), value + "\n")

    def _resource_limits_path(self, chat_id: int) -> Path:
        if not isinstance(chat_id, int) or isinstance(chat_id, bool) or chat_id == 0:
            raise TelegramStateError("invalid Telegram chat ID")
        return self.root / f"{chat_id}.resource-limits"

    def _model_path(self, chat_id: int) -> Path:
        if chat_id == 0:
            raise TelegramStateError("Telegram chat ID must be non-zero")
        return self.models_root / f"{chat_id}.model"

    def _language_path(self, chat_id: int) -> Path:
        if chat_id == 0:
            raise TelegramStateError("Telegram chat ID must be non-zero")
        return self.languages_root / f"{chat_id}.language"

    def _icon_path(self, chat_id: int) -> Path:
        if chat_id == 0:
            raise TelegramStateError("Telegram chat ID must be non-zero")
        return self.icons_root / f"{chat_id}.intensity"

    def _atomic_write(self, destination: Path, content: str) -> None:
        self.prepare()
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


class TelegramInstanceLock:
    """Advisory process lock retained for the service lifetime."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._descriptor: int | None = None

    def __enter__(self) -> TelegramInstanceLock:
        descriptor = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            raise TelegramStateError("another XDSH Telegram process owns the state root") from None
        self._descriptor = descriptor
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exception_type, exception, traceback
        if self._descriptor is None:
            return
        fcntl.flock(self._descriptor, fcntl.LOCK_UN)
        os.close(self._descriptor)
        self._descriptor = None
