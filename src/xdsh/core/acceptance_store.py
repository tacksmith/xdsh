"""Host-owned research baselines shared with a read-only verification CLI.

The state directory must be outside the runner workspace and protected by the
deployment sandbox. Hashes detect corruption, not an attacker with host-user
write access. No workspace-controlled path is accepted as an authority.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xdsh.core.artifact_acceptance import (
    AcceptanceError,
    ArtifactAcceptance,
    restore_acceptance,
)

MAX_RECORD_BYTES = 64 * 1024


def _encoded(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode()


@dataclass(frozen=True)
class StoredAcceptance:
    path: Path
    session_id: str
    contract: str
    snapshot: ArtifactAcceptance | None
    baseline_id: str | None
    capture_point: str | None
    required_revision: int = 1


class AcceptanceStore:
    """Create a binding once; freeze once; never repair a missing/corrupt record."""

    def __init__(self, root: Path) -> None:
        expanded = root.expanduser().absolute()
        if expanded.is_symlink():
            raise AcceptanceError("baseline root must not be a symlink")
        self.root = expanded.parent.resolve() / expanded.name

    def path_for(self, workspace: Path, contract: str) -> Path:
        key = hashlib.sha256(_encoded([str(workspace.resolve()), contract])).hexdigest()
        return self.root / (key + ".json")

    def begin(self, pending: ArtifactAcceptance, session_id: str) -> StoredAcceptance:
        if pending.deferred is None:
            raise AcceptanceError("only deferred research uses a baseline binding")
        self._outside(pending.root)
        path = self.path_for(pending.root, pending.deferred)
        with self._directory(create=True) as directory, self._lock(directory):
            claim = self._read(directory, path.stem + ".claim", missing=True)
            record = self._read(directory, path.name, missing=True)
            if claim is None and record is None:
                claim = {"workspace": str(pending.root), "identity": list(pending.identity),
                         "contract": pending.deferred, "session_id": session_id,
                         "registration_id": uuid.uuid4().hex, "registered_at": time.time()}
                if pending.research_revision != 1:
                    claim["research_revision"] = pending.research_revision
                record = {"binding": claim, "snapshot": None, "capture_point": None}
                # The immutable claim makes an interrupted write or a later lost
                # snapshot distinguishable from a genuinely new task.
                self._write(directory, path.stem + ".claim", claim)
                self._write(directory, path.name, record)
            stored = self._decode(path, claim, record, pending.root, pending.deferred)
            if stored.session_id != session_id:
                raise AcceptanceError("baseline belongs to a different session")
            if stored.required_revision != pending.research_revision:
                raise AcceptanceError("baseline research policy mismatch")
            return stored

    def load(self, workspace: Path, contract: str) -> StoredAcceptance:
        """Read only. In particular, absence never registers a fresh baseline."""
        self._outside(workspace)
        path = self.path_for(workspace, contract)
        with self._directory() as directory:
            return self._decode(
                path, self._read(directory, path.stem + ".claim"),
                self._read(directory, path.name), workspace, contract,
            )

    def freeze(
        self, binding: StoredAcceptance, snapshot: ArtifactAcceptance, *, capture_point: str,
    ) -> StoredAcceptance:
        self._outside(snapshot.root)
        path = self.path_for(snapshot.root, binding.contract)
        if path != binding.path or capture_point not in {"tool_boundary", "completion"}:
            raise AcceptanceError("invalid baseline binding")
        with self._directory() as directory, self._lock(directory):
            claim = self._read(directory, path.stem + ".claim")
            record = self._read(directory, path.name)
            current = self._decode(path, claim, record, snapshot.root, binding.contract)
            if current.session_id != binding.session_id:
                raise AcceptanceError("baseline belongs to a different session")
            if current.snapshot is not None:
                return current  # Never replace it with later source bytes/rules.
            if snapshot.deferred is not None or binding.contract not in dict(snapshot.baselines):
                raise AcceptanceError("baseline is not ready to freeze")
            if snapshot.research_revision < current.required_revision:
                raise AcceptanceError("baseline research policy downgrade")
            saved = {"identity": list(snapshot.identity), "baselines": list(snapshot.baselines),
                     "rules": json.loads(snapshot.rules), "arrays": json.loads(snapshot.arrays),
                     "calls": json.loads(snapshot.calls)}
            if snapshot.research_revision != 1:
                saved["research_revision"] = snapshot.research_revision
            if snapshot.feasibility != "null":
                saved["feasibility"] = json.loads(snapshot.feasibility)
            if snapshot.large_files != "{}":
                saved["large_files"] = json.loads(snapshot.large_files)
            record = {"binding": claim, "snapshot": saved, "capture_point": capture_point,
                      "captured_at": time.time()}
            self._write(directory, path.name, record)
            # Read back through the same validation as a different CLI process.
            return self._decode(path, claim, self._read(directory, path.name),
                                snapshot.root, binding.contract)

    def _outside(self, workspace: Path) -> None:
        root = workspace.resolve()
        if self.root == root or root in self.root.parents:
            raise AcceptanceError("host baselines must be outside the workspace")

    @contextmanager
    def _directory(self, *, create: bool = False) -> Iterator[int]:
        if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
            raise AcceptanceError("safe baseline storage unavailable")
        descriptor: int | None = None
        try:
            if create:
                self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
            descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            info = os.fstat(descriptor)
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise AcceptanceError("baseline root must be owner-only")
            yield descriptor
        except OSError:
            raise AcceptanceError("host baseline storage unavailable") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @contextmanager
    def _lock(self, directory: int) -> Iterator[None]:
        import fcntl

        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            descriptor = os.open(".lock", flags | os.O_CREAT | os.O_EXCL,
                                 0o600, dir_fd=directory)
        except FileExistsError:
            # Separate exclusive creation from opening an existing lock. On
            # macOS concurrent O_CREAT|O_NOFOLLOW opens can spuriously return
            # ENOENT while the other opener is creating this same inode.
            descriptor = os.open(".lock", flags, dir_fd=directory)
        try:
            self._regular(descriptor)
            deadline = time.monotonic() + 2
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise AcceptanceError("host baseline storage busy") from None
                    time.sleep(0.01)
            yield
        finally:
            os.close(descriptor)

    @staticmethod
    def _regular(descriptor: int) -> None:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1
                or info.st_size > MAX_RECORD_BYTES):
            raise AcceptanceError("unsafe or oversized baseline record")

    @classmethod
    def _read(cls, directory: int, name: str, *, missing: bool = False) -> Any:
        try:
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=directory)
        except FileNotFoundError:
            if missing:
                return None
            raise AcceptanceError("host baseline record missing") from None
        with os.fdopen(descriptor, "rb") as stream:
            cls._regular(stream.fileno())
            raw = stream.read(MAX_RECORD_BYTES + 1)
        try:
            wrapper = json.loads(raw)
            if (len(raw) > MAX_RECORD_BYTES or not isinstance(wrapper, dict)
                    or set(wrapper) != {"payload", "sha256"}
                    or wrapper["sha256"] != hashlib.sha256(
                        _encoded(wrapper["payload"])).hexdigest()):
                raise ValueError
            return wrapper["payload"]
        except (ValueError, TypeError, RecursionError, UnicodeError):
            raise AcceptanceError("host baseline record corrupt") from None

    @staticmethod
    def _write(directory: int, name: str, payload: object) -> None:
        digest = hashlib.sha256(_encoded(payload)).hexdigest()
        raw = _encoded({"payload": payload, "sha256": digest})
        if len(raw) > MAX_RECORD_BYTES:
            raise AcceptanceError("host baseline record too large")
        temporary = "." + uuid.uuid4().hex + ".tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass

    @staticmethod
    def _decode(
        path: Path, claim: object, record: object, workspace: Path, contract: str,
    ) -> StoredAcceptance:
        try:
            root = workspace.resolve(strict=True)
            info = root.stat()
            if (not isinstance(claim, dict) or not isinstance(record, dict)
                    or claim != record.get("binding") or claim.get("workspace") != str(root)
                    or claim.get("identity") != [info.st_dev, info.st_ino]
                    or claim.get("contract") != contract
                    or not isinstance(claim.get("session_id"), str)):
                raise ValueError
            snapshot = (restore_acceptance(root, contract, record["snapshot"])
                        if record["snapshot"] is not None else None)
            revision = claim.get("research_revision", 1)
            if (type(revision) is not int or revision not in (1, 2)
                    or (snapshot is not None and snapshot.research_revision < revision)):
                raise ValueError
            point = record.get("capture_point")
            if ((snapshot is not None and point not in {"tool_boundary", "completion"})
                    or (snapshot is None and point is not None)):
                raise ValueError
            return StoredAcceptance(
                path, claim["session_id"], contract, snapshot,
                hashlib.sha256(_encoded(record)).hexdigest() if snapshot else None, point,
                revision,
            )
        except (ValueError, KeyError, TypeError, RecursionError, OSError):
            raise AcceptanceError("host baseline binding invalid") from None


def load_host_baseline(path: Path, *, workspace: Path, contract: str) -> StoredAcceptance:
    """CLI entry point: known workspace/contract binding, never a fresh capture."""
    store = AcceptanceStore(path.parent)
    if path.name != store.path_for(workspace, contract).name:
        raise AcceptanceError("host baseline binding mismatch")
    return store.load(workspace, contract)
