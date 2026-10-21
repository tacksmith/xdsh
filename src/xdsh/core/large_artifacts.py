"""Explicit large-original checks, separate from bounded JSON/receipt parsing."""
from __future__ import annotations

import hashlib
import json
import math
import os
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from xdsh.core.evidence_files import (
    MAX_STREAM_BYTES,
    STREAM_BLOCK,
    STREAM_SECONDS,
    EvidenceFileError,
    WorkspaceEvidence,
    evidence_path,
)
from xdsh.core.numeric_artifacts import numeric_array_layout


def _valid_shape(shape: Any) -> bool:
    return (isinstance(shape, list) and len(shape) <= 8
            and all(n is None or (type(n) is int and 0 <= n <= 1_000_000_000) for n in shape))


def _finite(value: Any) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def validate_large_files(value: Any) -> None:
    if not isinstance(value, dict) or len(value) > 8:
        raise ValueError("invalid large file declaration")
    for path, rule in value.items():
        evidence_path(path)
        if not isinstance(rule, dict):
            raise ValueError("invalid large file declaration")
        if rule == {"format": "raw"}:
            continue
        if rule.get("format") == "hdf5":
            _validate_hdf(rule)
            continue
        if (rule.get("format") != "npy"
                or set(rule) - {"format", "shape", "finite", "nonconstant", "minimum", "maximum"}
                or ("shape" in rule and not _valid_shape(rule["shape"]))
                or any(type(rule[k]) is not bool for k in ("finite", "nonconstant") if k in rule)
                or any(not _finite(rule[k])
                       for k in ("minimum", "maximum") if k in rule)
                or ("minimum" in rule and "maximum" in rule and rule["minimum"] > rule["maximum"])):
            raise ValueError("invalid large file declaration")


def _validate_hdf(rule: dict[str, Any]) -> None:
    datasets = rule.get("datasets")
    if (set(rule) != {"format", "datasets"} or not isinstance(datasets, dict)
            or not 1 <= len(datasets) <= 8):
        raise ValueError("invalid large file declaration")
    for path, dataset in datasets.items():
        evidence_path(path)  # HDF object paths are also relative, without . or ..
        if (not isinstance(dataset, dict) or not {"shape", "encoding"} <= set(dataset)
                or set(dataset) - {"shape", "encoding", "axes"}
                or dataset["encoding"] not in ("dense", "csr", "csc")
                or not _valid_shape(dataset["shape"])
                or (dataset["encoding"] != "dense" and len(dataset["shape"]) != 2)):
            raise ValueError("invalid large file declaration")
        axes = dataset.get("axes", {})
        if not isinstance(axes, dict) or len(axes) > 8:
            raise ValueError("invalid large file declaration")
        for axis, item in axes.items():
            if (axis not in tuple(str(n) for n in range(len(dataset["shape"])))
                    or not isinstance(item, dict) or set(item) != {"dataset", "reference"}):
                raise ValueError("invalid large file declaration")
            evidence_path(item["dataset"])
            reference = item["reference"]
            if (not isinstance(reference, dict) or set(reference) != {"path", "pointer"}
                    or not isinstance(reference["pointer"], str)
                    or not reference["pointer"].startswith("/") or len(reference["pointer"]) > 200):
                raise ValueError("invalid large file declaration")
            evidence_path(reference["path"])


def hdf_references(rule: dict[str, Any]) -> list[dict[str, str]]:
    return [item["reference"] for dataset in rule.get("datasets", {}).values()
            for item in dataset.get("axes", {}).values()]


def ordered_ids_fingerprint(ids: Any) -> dict[str, object]:
    """Length-framed exact UTF-8 order, never a sorted/set-only identity."""
    if not isinstance(ids, list) or len(ids) > 200_000:
        raise ValueError("invalid ordered IDs")
    digest = hashlib.sha256()
    seen: set[str] = set()
    for item in ids:
        if (not isinstance(item, str) or not 1 <= len(item) <= 256
                or any(ord(c) < 32 for c in item) or item in seen):
            raise ValueError("invalid ordered IDs")
        seen.add(item)
        data = item.encode("utf-8")
        digest.update(len(data).to_bytes(4, "big"))
        digest.update(data)
    return {"count": len(ids), "sha256": digest.hexdigest()}


def _hdf_metadata(descriptor: int, rule: dict[str, Any]) -> dict[str, Any]:
    """Run trusted read-only code, not validators supplied by the task.

    Native parsing is separated from the Bot, with resource limits installed
    before h5py import. This is process/resource isolation, not an OS sandbox.
    No model credentials, Python path, HDF plugins or workspace cwd are inherited.
    """
    worker = Path(__file__).with_name("hdf_evidence_worker.py").resolve()
    environment = {"PATH": os.defpath, "HDF5_PLUGIN_PRELOAD": "::", "HDF5_PLUGIN_PATH": "",
                   "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1",
                   "MKL_NUM_THREADS": "1"}
    try:
        if sys.platform not in ("darwin", "linux"):
            raise ValueError("unsupported HDF resource controls")
        with subprocess.Popen(
            [sys.executable, "-I", "-B", str(worker), str(descriptor)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            pass_fds=(descriptor,), env=environment, cwd=worker.parent,
        ) as process:
            pending: bytes | None = json.dumps(rule).encode()
            deadline = time.monotonic() + 30
            try:
                while True:
                    try:
                        output, _ = process.communicate(pending, timeout=0.05)
                        break
                    except subprocess.TimeoutExpired:
                        pending = None
                        if time.monotonic() > deadline:
                            raise ValueError("HDF verification deadline") from None
                        if sys.platform == "darwin" and process.poll() is None:
                            # Darwin rejects RLIMIT_AS/DATA lowering in supported
                            # Python builds. Monitor this child, not system-wide RSS.
                            import ctypes

                            library = ctypes.CDLL("/usr/lib/libproc.dylib")
                            info = ctypes.create_string_buffer(256)
                            count = library.proc_pidinfo(process.pid, 4, 0, info, len(info))
                            rss = ctypes.c_uint64.from_buffer(info, 8).value
                            if ((count < 16 and process.poll() is None)
                                    or (count >= 16 and rss > 512 * 1024**2)):
                                raise ValueError("HDF memory monitor rejected child") from None
            except BaseException:
                process.kill()
                process.communicate()
                raise
        if process.returncode == 3:
            raise EvidenceFileError("HDF science dependency unavailable")
        if process.returncode != 0 or len(output) > 65536:
            raise ValueError("HDF worker failed")
        result = json.loads(output)
        if not isinstance(result, dict) or set(result) != {"datasets"}:
            raise ValueError("HDF worker did not verify the declaration")
        return result
    except EvidenceFileError:
        raise
    except (OSError, ValueError, subprocess.SubprocessError):
        raise EvidenceFileError(
            "HDF verification unavailable or rejected; science extra required",
        ) from None


def inspect_large_file(reader: WorkspaceEvidence, path: str,
                       rule: dict[str, Any]) -> dict[str, object]:
    """Fingerprint every byte. Raw mode does not validate the file format."""
    if rule["format"] == "raw":
        return reader.fingerprint(path)
    if rule["format"] == "hdf5":
        # Keep the same descriptor pinned across full hashing and metadata reads.
        deadline = time.monotonic() + STREAM_SECONDS
        with reader.open_regular(path, max_bytes=MAX_STREAM_BYTES) as stream:
            digest = hashlib.sha256()
            total = 0
            while block := stream.read(STREAM_BLOCK):
                total += len(block)
                if total > MAX_STREAM_BYTES or time.monotonic() > deadline:
                    raise EvidenceFileError("stream verification budget exceeded")
                digest.update(block)
            stream.seek(0)
            metadata = _hdf_metadata(stream.fileno(), rule)
            return {"sha256": digest.hexdigest(), "size_bytes": total, **metadata}
    deadline = time.monotonic() + STREAM_SECONDS
    with reader.open_regular(path, max_bytes=MAX_STREAM_BYTES) as stream:
        prefix = stream.read(8)
        if len(prefix) != 8 or prefix[:6] != b"\x93NUMPY" or prefix[6:8] not in (
            b"\x01\x00", b"\x02\x00", b"\x03\x00",
        ):
            raise EvidenceFileError("unsupported array format")
        length = stream.read(2 if prefix[6] == 1 else 4)
        size = int.from_bytes(length, "little")
        if not 1 <= size <= 4096:
            raise EvidenceFileError("invalid array header size")
        header = prefix + length + stream.read(size)
        total = os.fstat(stream.fileno()).st_size
        shape, offset, fmt = numeric_array_layout(header, total)
        if "shape" in rule and (len(shape) != len(rule["shape"]) or any(
            wanted is not None and got != wanted
            for got, wanted in zip(shape, rule["shape"], strict=False)
        )):
            raise EvidenceFileError("array shape mismatch")
        digest = hashlib.sha256(header)
        count = offset
        first: float | int | None = None
        varied = False
        scan = (rule.get("finite", True) or rule.get("nonconstant", False)
                or "minimum" in rule or "maximum" in rule)
        while block := stream.read(STREAM_BLOCK):
            count += len(block)
            if count > total or time.monotonic() > deadline:
                raise EvidenceFileError("stream verification budget exceeded")
            digest.update(block)
            if len(block) % struct.calcsize(fmt):
                raise EvidenceFileError("invalid numeric array payload")
            if scan:
                for (value,) in struct.iter_unpack(fmt, block):
                    if (not math.isfinite(value)
                            or ("minimum" in rule and not value >= rule["minimum"])
                            or ("maximum" in rule and not value <= rule["maximum"])):
                        raise EvidenceFileError("invalid numeric array values")
                    if first is None:
                        first = value
                    elif value != first:
                        varied = True
        if count != total or (rule.get("nonconstant", False) and not varied):
            raise EvidenceFileError("invalid numeric array payload")
        return {"sha256": digest.hexdigest(), "size_bytes": total, "shape": list(shape)}
