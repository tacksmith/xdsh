"""Large originals are checked at the public acceptance/call/CLI boundaries."""
from __future__ import annotations

import json
import os
import struct
import tracemalloc
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from xdsh.core.artifact_acceptance import PREFIX, capture_acceptance
from xdsh.core.method_evidence import ArtifactBinding, verified_call


def test_large_original_is_frozen_and_same_size_replacement_is_detected(tmp_path: Path) -> None:
    original = tmp_path / "original.h5ad"
    # Raw fingerprint mode makes no claim about this synthetic file's structure.
    with original.open("wb") as stream:
        for _ in range(48):
            stream.write(b"a" * 65536)
    snapshot = capture_acceptance(PREFIX + json.dumps({
        "unchanged": ["original.h5ad"], "json": [],
        "large_files": {"original.h5ad": {"format": "raw"}},
    }), tmp_path)
    assert snapshot is not None
    result = snapshot.evaluate()
    assert result.issues == ()
    assert [(entry.path, entry.size_bytes) for entry in result.evidence] == [
        ("original.h5ad", 3145728),
    ]
    with original.open("r+b") as stream:
        stream.seek(2800000)
        stream.write(b"b")
    assert snapshot.verify() == ("source_changed:original.h5ad",)


def test_large_npy_checks_payload_not_only_shape(tmp_path: Path) -> None:
    header = b"{'descr': '<f8', 'fortran_order': False, 'shape': (400000,)}\n"
    target = tmp_path / "scores.npy"
    with target.open("wb") as stream:
        stream.write(b"\x93NUMPY\x01\x00" + struct.pack("<H", len(header)) + header)
        for _ in range(400):
            stream.write(struct.pack("<1000d", *range(1000)))
    snapshot = capture_acceptance(PREFIX + json.dumps({
        "unchanged": [], "json": [], "large_files": {"scores.npy": {
            "format": "npy", "shape": [400000], "finite": True, "nonconstant": True,
            "minimum": 0, "maximum": 999,
        }},
    }), tmp_path)
    assert snapshot is not None and snapshot.verify() == ()
    # A non-finite value at the very end must not be missed by sampled checks.
    with target.open("r+b") as stream:
        stream.seek(-8, 2)
        stream.write(struct.pack("<d", float("nan")))
    assert snapshot.verify() == ("large_file_invalid:scores.npy",)


@pytest.mark.parametrize("shape", [[True], [-1], [1.2], [10**15], "4"])
def test_large_shape_declarations_are_strict(tmp_path: Path, shape: object) -> None:
    from xdsh.core.artifact_acceptance import AcceptanceError

    with pytest.raises(AcceptanceError, match="invalid large file declaration"):
        capture_acceptance(PREFIX + json.dumps({"unchanged": [], "json": [], "large_files": {
            "scores.npy": {"format": "npy", "shape": shape},
        }}), tmp_path)


def test_large_stage_receipt_survives_host_restore_but_not_output_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from xdsh.core.acceptance_store import AcceptanceStore

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    Path("plan.json").write_text('{"parameters":{"n":3}}')
    Path("analysis.py").write_text("# synthetic large-stage fixture\n")
    Path("input.bin").write_bytes(b"a" * (3 * 1024 * 1024))
    spec = {
        "unchanged": ["plan.json", "analysis.py", "input.bin"],
        "json": [{"path": "summary.json", "numbers": {"/count": {}}}],
        "large_files": {name: {"format": "raw"} for name in ("input.bin", "output.bin")},
        "calls": [{"path": "call.json", "source": {"path": "plan.json", "pointer": "/parameters"},
                   "artifacts": {"inputs": ["input.bin"], "code": ["analysis.py"],
                                 "outputs": ["output.bin"], "packages": {},
                                 "large_files": ["input.bin", "output.bin"]}}],
    }
    Path("contract.json").write_text(json.dumps(spec))
    pending = capture_acceptance(PREFIX + json.dumps({"unchanged": [], "json": [],
        "deferred": {"path": "contract.json", "profile": "research"}}), workspace)
    assert pending is not None
    store = AcceptanceStore(tmp_path / "host-state")
    binding = store.begin(pending, "large-session")
    frozen = store.freeze(binding, pending.capture_deferred(), capture_point="tool_boundary")
    assert frozen.snapshot is not None

    def analyze(*, n: int) -> None:
        Path("output.bin").write_bytes(b"b" * (n * 1024 * 1024))
        Path("summary.json").write_text(json.dumps({"count": n}))

    verified_call(analyze, {"n": 3}, ArtifactBinding(
        "call.json", inputs=("input.bin",), outputs=("output.bin",), code=("analysis.py",),
        large_files=("input.bin", "output.bin")), n=3)
    restored = store.load(workspace, "contract.json").snapshot
    assert restored is not None and restored.verify() == ()
    with Path("output.bin").open("r+b") as stream:
        stream.seek(-1, 2)
        stream.write(b"c")
    assert restored.verify() == ("artifact_binding_mismatch:call.json",)


@pytest.mark.parametrize("unsafe", ["symlink", "parent_symlink", "hardlink", "fifo", "directory",
                                    "missing", "over_budget"])
def test_large_files_never_relax_workspace_boundaries(tmp_path: Path, unsafe: str) -> None:
    from xdsh.core.artifact_acceptance import AcceptanceError
    from xdsh.core.evidence_files import MAX_STREAM_BYTES

    workspace = tmp_path / "work"
    workspace.mkdir()
    (tmp_path / "outside").write_text("not evidence")
    target = workspace / "data.bin"
    relative = "data.bin"
    if unsafe == "symlink":
        target.symlink_to(tmp_path / "outside")
    elif unsafe == "parent_symlink":
        (workspace / "parent").symlink_to(tmp_path, target_is_directory=True)
        relative = "parent/outside"
    elif unsafe == "hardlink":
        os.link(tmp_path / "outside", target)
    elif unsafe == "fifo":
        os.mkfifo(target)
    elif unsafe == "directory":
        target.mkdir()
    elif unsafe == "over_budget":
        with target.open("wb") as stream:
            stream.truncate(MAX_STREAM_BYTES + 1)
    with pytest.raises(AcceptanceError):
        capture_acceptance(PREFIX + json.dumps({"unchanged": [relative], "json": [],
            "large_files": {relative: {"format": "raw"}}}), workspace)


def test_large_stream_does_not_retain_whole_original_in_memory(tmp_path: Path) -> None:
    with (tmp_path / "data.bin").open("wb") as stream:
        stream.truncate(32 * 1024**2)
    tracemalloc.start()
    try:
        snapshot = capture_acceptance(PREFIX + json.dumps({"unchanged": ["data.bin"], "json": [],
            "large_files": {"data.bin": {"format": "raw"}}}), tmp_path)
        assert snapshot is not None and snapshot.verify() == ()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 5 * 1024**2


@pytest.mark.parametrize("mutation", ["replace", "in_place", "parent", "workspace"])
def test_mutation_during_stream_never_certifies_stale_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str,
) -> None:
    from xdsh.core.artifact_acceptance import AcceptanceError

    workspace = tmp_path / "work"
    parent = workspace / "data"
    parent.mkdir(parents=True)
    target = parent / "original.bin"
    target.write_bytes(b"a" * (3 * 1024**2))
    real_fdopen = os.fdopen
    changed = False

    class MutatingStream:
        def __init__(self, stream: Any) -> None:
            self.stream = stream

        def read(self, count: int) -> bytes:
            nonlocal changed
            data: bytes = self.stream.read(count)
            if not changed:
                changed = True
                if mutation == "replace":
                    target.unlink()
                    target.write_bytes(b"b" * (3 * 1024**2))
                elif mutation == "in_place":
                    with target.open("r+b") as output:
                        output.seek(-1, 2)
                        output.write(b"b")
                elif mutation == "parent":
                    parent.rename(workspace / "old-data")
                    parent.mkdir()
                else:
                    workspace.rename(tmp_path / "old-work")
                    workspace.mkdir()
            return data

    @contextmanager
    def mutated_open(fd: int, mode: str) -> Iterator[MutatingStream]:
        with real_fdopen(fd, mode) as stream:
            yield MutatingStream(stream)

    monkeypatch.setattr(os, "fdopen", mutated_open)
    with pytest.raises(AcceptanceError):
        capture_acceptance(PREFIX + json.dumps({"unchanged": ["data/original.bin"], "json": [],
            "large_files": {"data/original.bin": {"format": "raw"}}}), workspace)


def test_large_timeout_does_not_become_a_partial_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time

    from xdsh.core.artifact_acceptance import AcceptanceError

    target = tmp_path / "data.bin"
    target.write_bytes(b"a" * (3 * 1024**2))
    clock = iter([0, 181])
    monkeypatch.setattr(time, "monotonic", lambda: next(clock))
    with pytest.raises(AcceptanceError, match="budget exceeded"):
        capture_acceptance(PREFIX + json.dumps({"unchanged": ["data.bin"], "json": [],
            "large_files": {"data.bin": {"format": "raw"}}}), tmp_path)


@pytest.mark.parametrize("mode", ["nonconstant", "truncated", "oversized_header", "pickle"])
def test_streamed_npy_rejects_invalid_numeric_evidence(tmp_path: Path, mode: str) -> None:
    descriptor = "|O" if mode == "pickle" else "<f8"
    header = ("{'descr': '" + descriptor + "', 'fortran_order': False, 'shape': (2,)}\n").encode()
    length = 6000 if mode == "oversized_header" else len(header)
    payload = struct.pack("<2d", 1, float("nan") if mode == "nonconstant" else 2)
    if mode == "truncated":
        payload = payload[:-1]
    (tmp_path / "data.npy").write_bytes(b"\x93NUMPY\x01\x00" + struct.pack("<H", length)
                                       + header + payload)
    snapshot = capture_acceptance(PREFIX + json.dumps({"unchanged": [], "json": [],
        "large_files": {"data.npy": {"format": "npy", "finite": False, "nonconstant": True}},
    }), tmp_path)
    assert snapshot is not None and snapshot.verify() == ("large_file_invalid:data.npy",)


def test_large_invalidity_never_automatically_replays_research(tmp_path: Path) -> None:
    from xdsh.core.research_recovery import ResearchRepair

    (tmp_path / "data.npy").write_bytes(b"incomplete output")
    snapshot = capture_acceptance(PREFIX + json.dumps({"unchanged": [], "json": [],
        "large_files": {"data.npy": {"format": "npy", "shape": [2, 3]}}}), tmp_path)
    assert snapshot is not None
    assert ResearchRepair("contract.json").next_prompt(snapshot, snapshot.evaluate(),
                                                      saw_work=True) is None


def test_cli_large_example_and_coverage_are_explicit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    from xdsh.ops.verify import main

    assert main(["--large-example"]) == 0
    example = json.loads(capsys.readouterr().out)
    assert example["large_files"]["input.h5ad"]["format"] == "hdf5"
    (tmp_path / "data.bin").write_bytes(b"a" * (3 * 1024**2))
    (tmp_path / "checks.json").write_text(json.dumps({"unchanged": [], "json": [],
        "large_files": {"data.bin": {"format": "raw"}}}))
    assert main(["--workspace", str(tmp_path), "--contract", "checks.json", "--details"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["scientific_validity"] == "not_assessed"
    assert result["large_artifact_checks"] == {"checked": 1, "verified": 1}
    assert result["large_artifacts"] == [{
        "path": "data.bin", "format": "raw", "matched": True,
        "coverage": "sha256_only", "reason": "verified",
    }]


@pytest.mark.parametrize("corrupt", [None, [], False])
def test_restoring_corrupt_large_profiles_cannot_silently_drop_them(
    tmp_path: Path, corrupt: object,
) -> None:
    from xdsh.core.artifact_acceptance import AcceptanceError, restore_acceptance

    info = tmp_path.stat()
    record = {"identity": [info.st_dev, info.st_ino],
              "baselines": [["input.bin", "0" * 64], ["contract.json", "1" * 64]],
              "rules": [], "arrays": [], "calls": [], "large_files": corrupt}
    with pytest.raises(AcceptanceError, match="invalid saved acceptance"):
        restore_acceptance(tmp_path, "contract.json", record)


@pytest.mark.parametrize("role", ["json", "arrays", "reference"])
def test_streaming_mode_cannot_bypass_small_structured_parser_limits(
    tmp_path: Path, role: str,
) -> None:
    from xdsh.core.artifact_acceptance import AcceptanceError

    spec: dict[str, Any] = {"unchanged": [], "json": [],
                            "large_files": {"input.bin": {"format": "raw"}}}
    if role == "json":
        spec["json"] = [{"path": "input.bin", "numbers": {"/x": {}}}]
    elif role == "arrays":
        spec["arrays"] = [{"path": "input.bin", "shape": [2]}]
    else:
        spec["unchanged"] = ["input.bin"]
        spec["json"] = [{"path": "output.json", "source_json": {
            "/value": {"path": "input.bin", "pointer": "/value"}}}]
    with pytest.raises(AcceptanceError, match="invalid large file declaration"):
        capture_acceptance(PREFIX + json.dumps(spec), tmp_path)
