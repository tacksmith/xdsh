"""Real synthetic HDF5 fixtures; run with the optional science extra installed."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from xdsh.core.artifact_acceptance import PREFIX, capture_acceptance

h5py = pytest.importorskip("h5py", reason="optional science extra required")
np = pytest.importorskip("numpy")


def _spec() -> dict[str, Any]:
    return {"unchanged": ["ids.json"], "json": [], "large_files": {"data.h5ad": {
        "format": "hdf5", "datasets": {"X": {"encoding": "dense", "shape": [4000, 100],
            "axes": {"0": {"dataset": "obs/_index", "reference": {
                "path": "ids.json", "pointer": "/cells"}}}}},
    }}}


def _dense(workspace: Path) -> None:
    ids = [f"cell-{n}" for n in range(4000)]
    (workspace / "ids.json").write_text(json.dumps({"cells": ids}))
    with h5py.File(workspace / "data.h5ad", "w") as file:
        file.create_dataset("X", data=np.ones((4000, 100)), chunks=(100, 100))
        file.create_dataset("obs/_index", data=ids, dtype=h5py.string_dtype("utf-8"))


def test_hdf_dense_actual_shape_and_id_order_are_checked(tmp_path: Path) -> None:
    _dense(tmp_path)
    assert (tmp_path / "data.h5ad").stat().st_size > 2 * 1024**2
    snapshot = capture_acceptance(PREFIX + json.dumps(_spec()), tmp_path)
    assert snapshot is not None and snapshot.verify() == ()
    with h5py.File(tmp_path / "data.h5ad", "r+") as file:
        file["obs/_index"][0:2] = ["cell-1", "cell-0"]
    assert snapshot.verify() == ("large_file_invalid:data.h5ad",)


@pytest.mark.parametrize("kind", ["soft_link", "external_link", "external_storage", "virtual",
                                  "object_reference", "oversized_chunk", "duplicate_ids",
                                  "wrong_shape", "truncated", "unknown_filter"])
def test_unsafe_or_misaligned_hdf_evidence_is_rejected(tmp_path: Path, kind: str) -> None:
    _dense(tmp_path)
    with h5py.File(tmp_path / "data.h5ad", "r+") as file:
        if kind == "duplicate_ids":
            file["obs/_index"][0] = "cell-1"
        elif kind != "truncated":
            del file["X"]
            if kind == "soft_link":
                file["X"] = h5py.SoftLink("/obs/_index")
            elif kind == "external_link":
                file["X"] = h5py.ExternalLink("/outside-must-not-open.h5", "/private")
            elif kind == "external_storage":
                file.create_dataset("X", (4000, 100), dtype="f8",
                                    external=[("/outside-must-not-open.bin", 0,
                                               h5py.h5f.UNLIMITED)])
            elif kind == "virtual":
                layout = h5py.VirtualLayout((4000, 100), dtype="f8")
                layout[:] = h5py.VirtualSource("/outside-must-not-open.h5", "data", (4000, 100))
                file.create_virtual_dataset("X", layout)
            elif kind == "object_reference":
                file.create_dataset("X", (4000, 100), dtype=h5py.ref_dtype)
            elif kind == "oversized_chunk":
                file.create_dataset("X", (4000, 1000), dtype="f8", chunks=(4000, 1000),
                                    compression="gzip")
            elif kind == "unknown_filter":
                file.create_dataset("X", (4000, 100), dtype="f8", chunks=(100, 100),
                                    compression=32001, allow_unknown_filter=True)
            else:
                file.create_dataset("X", (3999, 100), dtype="f8")
    if kind == "truncated":
        with (tmp_path / "data.h5ad").open("r+b") as stream:
            stream.truncate(70)
    spec = _spec()
    if kind == "oversized_chunk":
        spec["large_files"]["data.h5ad"]["datasets"]["X"]["shape"] = [4000, 1000]
    snapshot = capture_acceptance(PREFIX + json.dumps(spec), tmp_path)
    assert snapshot is not None
    assert snapshot.verify() == ("large_file_invalid:data.h5ad",)


@pytest.mark.parametrize("encoding", ["csr", "csc"])
def test_sparse_shape_structure_and_identities(tmp_path: Path, encoding: str) -> None:
    with h5py.File(tmp_path / "sparse.h5ad", "w") as file:
        group = file.create_group("X")
        group.attrs["shape"] = [2, 3]
        group.attrs["encoding-type"] = encoding + "_matrix"
        group.create_dataset("data", data=[1.0, 2.0, 3.0])
        group.create_dataset("indices", data=[0, 2, 1] if encoding == "csr" else [0, 1, 0])
        group.create_dataset("indptr", data=[0, 2, 3] if encoding == "csr" else [0, 1, 2, 3])
        file.create_dataset("obs/_index", data=["cell-a", "cell-b"])
        file.create_dataset("var/_index", data=["g0", "g1", "g2"])
    (tmp_path / "ids.json").write_text('{"cells":["cell-a","cell-b"],"genes":["g0","g1","g2"]}')
    spec = {"unchanged": ["ids.json"], "json": [], "large_files": {"sparse.h5ad": {
        "format": "hdf5", "datasets": {"X": {"encoding": encoding, "shape": [2, 3], "axes": {
            "0": {"dataset": "obs/_index", "reference": {"path": "ids.json", "pointer": "/cells"}},
            "1": {"dataset": "var/_index", "reference": {"path": "ids.json", "pointer": "/genes"}},
        }}},
    }}}
    snapshot = capture_acceptance(PREFIX + json.dumps(spec), tmp_path)
    assert snapshot is not None and snapshot.verify() == ()
    with h5py.File(tmp_path / "sparse.h5ad", "r+") as file:
        file["X/indices"][-1] = 99
    assert snapshot.verify() == ("large_file_invalid:sparse.h5ad",)


def test_plugin_environment_and_workspace_python_are_not_loaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _dense(tmp_path)
    poison = tmp_path / "python"
    poison.mkdir()
    (poison / "sitecustomize.py").write_text("raise RuntimeError('workspace code loaded')")
    monkeypatch.setenv("PYTHONPATH", str(poison))
    monkeypatch.setenv("HDF5_PLUGIN_PATH", str(poison))
    monkeypatch.setenv("HDF5_VOL_CONNECTOR", "nonexistent_plugin")
    monkeypatch.chdir(tmp_path)
    snapshot = capture_acceptance(PREFIX + json.dumps(_spec()), tmp_path)
    assert snapshot is not None and snapshot.verify() == ()


@pytest.mark.parametrize("failure", ["deadline", "exit", "memory"])
def test_hdf_worker_failure_is_reaped_and_does_not_certify_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    if failure == "memory" and sys.platform != "darwin":
        pytest.skip("Darwin RSS monitor; Linux uses RLIMIT_AS/DATA")
    _dense(tmp_path)
    original_popen = subprocess.Popen
    children: list[subprocess.Popen[bytes]] = []

    def process_boundary(args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        # A controlled broken native worker at the OS-process boundary, never a
        # model-controlled script or real research process.
        program = "raise SystemExit(1)" if failure == "exit" else "import time; time.sleep(5)"
        child = original_popen([sys.executable, "-I", "-c", program], **kwargs)
        children.append(child)
        return child

    real_clock = time.monotonic
    if failure == "deadline":
        monkeypatch.setattr(time, "monotonic", lambda: real_clock() * 1000)
    elif failure == "memory":
        import ctypes

        class OverBudget:
            def proc_pidinfo(self, pid: int, flavor: int, arg: int, info: Any, size: int) -> int:
                ctypes.c_uint64.from_buffer(info, 8).value = 600 * 1024**2
                return 96

        monkeypatch.setattr(ctypes, "CDLL", lambda *_: OverBudget())
    monkeypatch.setattr(subprocess, "Popen", process_boundary)
    snapshot = capture_acceptance(PREFIX + json.dumps(_spec()), tmp_path)
    assert snapshot is not None
    started = real_clock()
    assert snapshot.verify() == ("large_file_invalid:data.h5ad",)
    assert real_clock() - started < 2
    assert children and all(child.poll() is not None for child in children)


def test_hdf_large_metadata_cannot_request_unbounded_id_allocation(tmp_path: Path) -> None:
    _dense(tmp_path)
    with h5py.File(tmp_path / "data.h5ad", "r+") as file:
        del file["obs/_index"]
        # Logical ID storage is enormous; no data is actually allocated/written.
        file.create_dataset("obs/_index", (4000,), dtype="S16777216")
    snapshot = capture_acceptance(PREFIX + json.dumps(_spec()), tmp_path)
    assert snapshot is not None and snapshot.verify() == ("large_file_invalid:data.h5ad",)


def test_hdf_compressed_input_is_supported_without_densifying_matrix(tmp_path: Path) -> None:
    with h5py.File(tmp_path / "data.h5ad", "w") as file:
        file.create_dataset("X", (100_000, 100_000), dtype="f4", chunks=(100, 100),
                            compression="gzip")
    snapshot = capture_acceptance(PREFIX + json.dumps({"unchanged": [], "json": [],
        "large_files": {"data.h5ad": {"format": "hdf5", "datasets": {
            "X": {"encoding": "dense", "shape": [100_000, 100_000]},
        }}},
    }), tmp_path)
    assert snapshot is not None and snapshot.verify() == ()
    assert (snapshot.evaluate().large_checks[0].coverage
            == "declared_hdf_structure_and_requested_ids")
