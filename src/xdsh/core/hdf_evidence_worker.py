"""Private bounded HDF5 reader, invoked by large_artifacts with a read-only FD.

Not a user command/execution relay. No workspace path is opened. Optional h5py
is imported only AFTER limits. No traversal of undeclared objects or links.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import math
import os
import resource
import sys
from typing import Any

MAX_CHUNK_BYTES = 8 * 1024**2
MAX_SCAN_BYTES = 256 * 1024**2
MAX_IDS = 200_000


def _limit() -> None:
    limits = [(resource.RLIMIT_CPU, 20), (resource.RLIMIT_CORE, 0), (resource.RLIMIT_FSIZE, 0)]
    if sys.platform == "linux":
        limits.extend(((resource.RLIMIT_AS, 1024**3), (resource.RLIMIT_DATA, 512 * 1024**2)))
    for name, maximum in limits:
        soft, hard = resource.getrlimit(name)
        bound = min(maximum, hard) if hard != resource.RLIM_INFINITY else maximum
        if soft != resource.RLIM_INFINITY:
            bound = min(bound, soft)
        resource.setrlimit(name, (bound, bound))


def inspect(descriptor: int, rule: dict[str, Any]) -> dict[str, Any]:
    h5py = importlib.import_module("h5py")
    np = importlib.import_module("numpy")

    scanned = 0

    def safe_object(root: Any, path: str) -> Any:
        current = root
        for part in path.split("/"):
            if (part in ("", ".", "..") or not isinstance(current, h5py.Group)
                    or not isinstance(current.get(part, getlink=True), h5py.HardLink)):
                raise ValueError("links or missing objects")
            current = current[part]
        return current

    def dataset(root: Any, path: str, *, strings: bool = False) -> Any:
        item = safe_object(root, path)
        if not isinstance(item, h5py.Dataset) or item.is_virtual or item.external:
            raise ValueError("external or non-dataset storage")
        dtype = item.dtype
        if strings:
            string_type = h5py.check_string_dtype(dtype)
            if (string_type is None
                    or (string_type.length is not None and string_type.length > 1024)):
                raise ValueError("non-string IDs")
        elif dtype.kind not in "biuf" or dtype.itemsize not in (1, 2, 4, 8):
            raise ValueError("non-primitive numeric data")
        if (item.shape is None or len(item.shape) > 8
                or any(n > 1_000_000_000 for n in item.shape)
                or math.prod(item.shape) * dtype.itemsize > 256 * 1024**3):
            raise ValueError("unbounded dataset")
        props = item.id.get_create_plist()
        # Disable plugin loading in the parent environment before importing HDF.
        # Only lossless built-in filters; no scale-offset, custom filters or VDS.
        if props.get_nfilters() > 4 or any(
            props.get_filter(n)[0] not in (1, 2, 3, 32000) for n in range(props.get_nfilters())
        ):
            raise ValueError("unsupported filter")
        if item.chunks and math.prod(item.chunks) * dtype.itemsize > MAX_CHUNK_BYTES:
            raise ValueError("oversized decompression chunk")
        return item

    def blocks(item: Any) -> Any:
        nonlocal scanned
        for start in range(0, item.shape[0], 8192):
            stop = min(item.shape[0], start + 8192)
            scanned += (stop - start) * item.dtype.itemsize
            if scanned > MAX_SCAN_BYTES:
                raise ValueError("scan budget exceeded")
            yield item[start:stop]

    def ids_record(item: Any, expected_count: int) -> dict[str, object]:
        nonlocal scanned
        if item.shape != (expected_count,) or expected_count > MAX_IDS:
            raise ValueError("ID shape or count mismatch")
        digest = hashlib.sha256()
        seen: set[str] = set()
        for block in blocks(item):
            for raw in block:
                text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
                if (not 1 <= len(text) <= 256 or any(ord(c) < 32 for c in text) or text in seen):
                    raise ValueError("invalid or duplicate ID")
                data = text.encode("utf-8")
                scanned += len(data)
                if scanned > MAX_SCAN_BYTES:
                    raise ValueError("ID budget exceeded")
                seen.add(text)
                digest.update(len(data).to_bytes(4, "big"))
                digest.update(data)
        return {"count": expected_count, "sha256": digest.hexdigest()}

    def sparse_shape(root: Any, path: str, encoding: str) -> list[int]:
        item = safe_object(root, path)
        if not isinstance(item, h5py.Group):
            raise ValueError("sparse array must be a group")
        # Numeric shape attributes are bounded before decoding.
        attr = item.attrs.get_id("shape")
        if attr.shape != (2,) or attr.dtype.kind not in "iu" or attr.dtype.itemsize > 8:
            raise ValueError("invalid sparse shape")
        shape = [int(n) for n in item.attrs["shape"]]
        if any(not 0 <= n <= 1_000_000_000 for n in shape):
            raise ValueError("invalid sparse shape")
        tag = item.attrs.get_id("encoding-type")
        if tag.shape != () or tag.dtype.itemsize > 256:
            raise ValueError("invalid encoding metadata")
        actual = item.attrs["encoding-type"]
        if isinstance(actual, bytes):
            actual = actual.decode("ascii")
        if actual != encoding + "_matrix":
            raise ValueError("sparse encoding mismatch")
        data = dataset(root, path + "/data")
        indices = dataset(root, path + "/indices")
        ptr = dataset(root, path + "/indptr")
        major, minor = shape if encoding == "csr" else shape[::-1]
        if (len(data.shape) != 1 or indices.shape != data.shape or ptr.shape != (major + 1,)
                or indices.dtype.kind not in "iu" or ptr.dtype.kind not in "iu"):
            raise ValueError("sparse vector mismatch")
        previous = 0
        first = True
        for block in blocks(ptr):
            if (not len(block) or (first and int(block[0]) != 0)
                    or int(block[0]) < previous or np.any(block[1:] < block[:-1])
                    or np.any(block < 0) or np.any(block > data.shape[0])):
                raise ValueError("invalid sparse pointers")
            previous = int(block[-1])
            first = False
        if previous != data.shape[0]:
            raise ValueError("sparse terminal pointer mismatch")
        for block in blocks(indices):
            if np.any(block < 0) or np.any(block >= minor):
                raise ValueError("sparse index out of bounds")
        return shape

    results: dict[str, Any] = {}
    # File-object driver prevents reopening a task-supplied pathname or driver.
    with os.fdopen(os.dup(descriptor), "rb") as stream, h5py.File(stream, "r") as root:
        for path, declared in rule["datasets"].items():
            encoding = declared["encoding"]
            shape = (list(dataset(root, path).shape) if encoding == "dense"
                     else sparse_shape(root, path, encoding))
            if len(shape) != len(declared["shape"]) or any(
                expected is not None and actual != expected
                for actual, expected in zip(shape, declared["shape"], strict=False)
            ):
                raise ValueError("dataset shape mismatch")
            axes = {axis: ids_record(dataset(root, spec["dataset"], strings=True), shape[int(axis)])
                    for axis, spec in declared.get("axes", {}).items()}
            results[path] = {"shape": shape, "axes": axes, "encoding": encoding}
    return {"datasets": results}


def main() -> int:
    try:
        _limit()
        document = sys.stdin.buffer.read(8193)
        if len(document) > 8192:
            raise ValueError("oversized declaration")
        result = inspect(int(sys.argv[1]), json.loads(document))
        print(json.dumps(result, allow_nan=False))
        return 0
    except ModuleNotFoundError:
        print('{"dependency_unavailable":true}')
        return 3
    except Exception:
        # Never emit HDF error text, paths, attributes, IDs or tracebacks.
        print('{"rejected":true}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
