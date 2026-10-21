"""Bounded no-follow reads shared by method capture and artifact validation."""
from __future__ import annotations

import contextlib
import hashlib
import os
import stat
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_STREAM_BYTES = 64 * 1024**3
STREAM_SECONDS = 180
STREAM_BLOCK = 1024 * 1024


class EvidenceFileError(ValueError):
    """No evidence contents or private paths in diagnostics."""


def evidence_path(value: object) -> str:
    if (not isinstance(value, str) or not 1 <= len(value) <= 240
            or "\\" in value or any(ord(c) < 32 for c in value)
            or PurePosixPath(value).is_absolute()
            or any(part in {"", ".", ".."} for part in value.split("/"))):
        raise EvidenceFileError("invalid workspace evidence path")
    return value


@dataclass(frozen=True)
class WorkspaceEvidence:
    root: Path
    identity: tuple[int, int]

    @classmethod
    def at(cls, workspace: Path) -> WorkspaceEvidence:
        try:
            root = workspace.resolve(strict=True)
            info = root.stat()
            return cls(root, (info.st_dev, info.st_ino))
        except OSError:
            raise EvidenceFileError("workspace inaccessible") from None

    def read(self, path: str) -> bytes:
        """Pin the workspace identity, reject links/devices and bound memory/IO."""
        with self.open_regular(path, max_bytes=MAX_FILE_BYTES) as stream:
            data = stream.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES:
                raise EvidenceFileError("evidence size limit exceeded")
            return data

    def fingerprint(self, path: str) -> dict[str, object]:
        """Explicit large-file mode: constant read buffer, no content decoding."""
        deadline = time.monotonic() + STREAM_SECONDS
        with self.open_regular(path, max_bytes=MAX_STREAM_BYTES) as stream:
            digest = hashlib.sha256()
            total = 0
            while block := stream.read(STREAM_BLOCK):
                total += len(block)
                if total > MAX_STREAM_BYTES or time.monotonic() > deadline:
                    raise EvidenceFileError("stream verification budget exceeded")
                digest.update(block)
            return {"sha256": digest.hexdigest(), "size_bytes": total}

    @contextlib.contextmanager
    def open_regular(self, path: str, *, max_bytes: int) -> Iterator[BinaryIO]:
        """Pin a regular file for a bounded reader and detect replacement/writes.

        No symlink traversal or special files. The caller must bound reads and
        interpretation. The descriptor never grants more than caller privileges.
        """
        if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
            raise EvidenceFileError("safe evidence reads unavailable on this host")
        parts = evidence_path(path).split("/")
        handles: list[int] = []
        try:
            directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            handles.append(directory)
            info = os.fstat(directory)
            if (info.st_dev, info.st_ino) != self.identity:
                raise EvidenceFileError("workspace identity changed")
            for part in parts[:-1]:
                directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                    dir_fd=directory)
                handles.append(directory)
            descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=directory)
            handles.append(descriptor)
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes
                    or (max_bytes > MAX_FILE_BYTES and info.st_nlink != 1)):
                raise EvidenceFileError("evidence file is not a bounded regular file")
            with os.fdopen(os.dup(descriptor), "rb") as stream:
                yield stream
            after = os.fstat(descriptor)
            named = os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
            if ((info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
                    after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                    or (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino)):
                raise EvidenceFileError("evidence changed while reading")
            root_now = self.root.lstat()
            if (root_now.st_dev, root_now.st_ino) != self.identity:
                raise EvidenceFileError("workspace identity changed")
            for index, part in enumerate(parts[:-1]):
                actual = os.stat(part, dir_fd=handles[index], follow_symlinks=False)
                pinned = os.fstat(handles[index + 1])
                if (actual.st_dev, actual.st_ino) != (pinned.st_dev, pinned.st_ino):
                    raise EvidenceFileError("evidence directory changed while reading")
        except OSError:
            raise EvidenceFileError("evidence inaccessible") from None
        finally:
            for handle in reversed(handles):
                os.close(handle)
