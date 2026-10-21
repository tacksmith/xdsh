"""Small, content-free heartbeat files for XDSH-owned processes."""

from __future__ import annotations

import math
import os
import time
import uuid
from pathlib import Path


class HeartbeatError(RuntimeError):
    """A heartbeat is missing, malformed, or stored at an unsafe path."""


class HeartbeatStore:
    """Atomically maintain one mode-0600 timestamp below a dedicated root."""

    def __init__(self, state_root: Path, name: str) -> None:
        root = state_root.expanduser().resolve()
        if (
            root in {Path(root.anchor), Path.home().resolve()}
            or ".xclaude" in root.parts
            or ".dsh" in root.parts
            or not name
            or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for character in name)
        ):
            raise HeartbeatError("heartbeat path is unsafe")
        self.root = root / "heartbeats"
        self.path = self.root / f"{name}.heartbeat"

    def touch(self, *, now: float | None = None) -> None:
        timestamp = time.time() if now is None else now
        if (
            isinstance(timestamp, bool)
            or not isinstance(timestamp, int | float)
            or not math.isfinite(float(timestamp))
            or timestamp < 0
        ):
            raise HeartbeatError("heartbeat timestamp is invalid")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        if self.path.is_symlink() or (self.path.exists() and not self.path.is_file()):
            raise HeartbeatError("heartbeat path is unsafe")
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="ascii") as handle:
                handle.write(f"{float(timestamp):.6f}\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        finally:
            if temporary.exists():
                temporary.unlink()

    def age(self, *, now: float | None = None) -> float:
        if self.path.is_symlink() or not self.path.is_file():
            raise HeartbeatError("heartbeat is missing or unsafe")
        try:
            timestamp = float(self.path.read_text(encoding="ascii").strip())
        except (OSError, UnicodeDecodeError, ValueError) as error:
            raise HeartbeatError("heartbeat is invalid") from error
        current = time.time() if now is None else now
        if (
            not math.isfinite(timestamp)
            or not isinstance(current, int | float)
            or isinstance(current, bool)
            or not math.isfinite(float(current))
        ):
            raise HeartbeatError("heartbeat timestamp is invalid")
        return float(current) - timestamp
