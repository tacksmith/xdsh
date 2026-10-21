"""Final artifacts must remain tied to the stage that actually produced them."""
from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from xdsh.core.artifact_acceptance import PREFIX, capture_acceptance
from xdsh.core.method_evidence import ArtifactBinding, verified_call


def test_changed_output_cannot_reuse_an_earlier_method_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "plan.json").write_text('{"parameters":{"scale":2}}')
    (tmp_path / "analysis.py").write_text("# synthetic analysis implementation\n")
    artifacts = {"inputs": ["plan.json"], "outputs": ["result.json"],
                 "code": ["analysis.py"], "packages": {}}
    snapshot = capture_acceptance(PREFIX + json.dumps({
        "unchanged": ["plan.json", "analysis.py"],
        "json": [{"path": "result.json", "numbers": {"/count": {}}}],
        "calls": [{"path": "call.json", "source": {
            "path": "plan.json", "pointer": "/parameters"}, "artifacts": artifacts}],
    }), tmp_path)
    assert snapshot is not None

    def analyze(*, scale: int) -> None:
        Path("result.json").write_text(json.dumps({"count": 2, "values": [scale, 2 * scale]}))

    binding = ArtifactBinding("call.json", inputs=("plan.json",), outputs=("result.json",),
                              code=("analysis.py",))
    verified_call(analyze, {"scale": 2}, binding, scale=2)
    assert snapshot.verify() == ()
    first = {item.path: item.sha256 for item in snapshot.evaluate().evidence}
    # Same shape/count and same method receipt, but the final values were replaced.
    (tmp_path / "result.json").write_text('{"count":2,"values":[4,2]}')
    second = {item.path: item.sha256 for item in snapshot.evaluate().evidence}
    assert first["result.json"] != second["result.json"]
    assert first["call.json"] == second["call.json"]
    assert snapshot.verify() == ("artifact_binding_mismatch:call.json",)


def test_completed_checkpoint_remains_inspectable_when_the_next_stage_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "plan.json").write_text('{"parameters":{"scale":2}}')
    (tmp_path / "analysis.py").write_text("# synthetic checkpoint writer\n")
    calls = [{"path": f"{name}.receipt.json", "source": {
        "path": "plan.json", "pointer": "/parameters"}, "artifacts": {
            "inputs": ["plan.json"], "outputs": [f"{name}.json"],
            "code": ["analysis.py"], "packages": {},
        }} for name in ("first", "second")]
    snapshot = capture_acceptance(PREFIX + json.dumps({
        "unchanged": ["plan.json", "analysis.py"], "json": [], "calls": calls,
    }), tmp_path)
    assert snapshot is not None

    def first(*, scale: int) -> None:
        Path("first.json").write_text(json.dumps({"value": scale}))

    def second(*, scale: int) -> None:
        raise RuntimeError("SYNTHETIC_FAILURE")

    for name, function in (("first", first), ("second", second)):
        binding = ArtifactBinding(f"{name}.receipt.json", inputs=("plan.json",),
                                  outputs=(f"{name}.json",), code=("analysis.py",))
        if name == "first":
            verified_call(function, {"scale": 2}, binding, scale=2)
        else:
            with pytest.raises(RuntimeError, match="SYNTHETIC_FAILURE"):
                verified_call(function, {"scale": 2}, binding, scale=2)
    result = snapshot.evaluate()
    assert result.issues == ("method_call_mismatch:second.receipt.json",)
    assert [(stage.path, stage.outcome, stage.matched) for stage in result.stages] == [
        ("first.receipt.json", "returned", True), ("second.receipt.json", "failed", False),
    ]
    assert (tmp_path / "first.json").read_text() == '{"value": 2}'


def test_marker_summary_must_match_the_final_table_not_an_earlier_cluster_number(
    tmp_path: Path,
) -> None:
    (tmp_path / "markers.json").write_text('{"by_marker":{"MS4A1":{"cluster":"9","rank":11}}}')
    (tmp_path / "summary.json").write_text('{"marker":{"cluster":"9","rank":11}}')
    snapshot = capture_acceptance(PREFIX + json.dumps({
        "unchanged": [], "json": [{"path": "summary.json", "artifact_json": {
            "/marker": {"path": "markers.json", "pointer": "/by_marker/MS4A1"},
        }}],
    }), tmp_path)
    assert snapshot is not None and snapshot.verify() == ()
    (tmp_path / "markers.json").write_text('{"by_marker":{"MS4A1":{"cluster":"3","rank":12}}}')
    assert snapshot.verify() == ("field_mismatch:summary.json:/marker",)


def test_gene_ranks_are_mapped_through_the_filtered_id_order(tmp_path: Path) -> None:
    (tmp_path / "genes.json").write_text('{"ids":["A","B","C","D"]}')
    (tmp_path / "mask.json").write_text('{"keep":[false,true,false,true]}')
    (tmp_path / "selected.json").write_text('{"ids":["B","D"]}')
    (tmp_path / "ranks.json").write_text('{"order":[1,0],"top":["B","A"]}')
    snapshot = capture_acceptance(PREFIX + json.dumps({
        "unchanged": ["genes.json"], "json": [
            {"path": "selected.json", "id_selections": [{
                "value": "/ids", "source": {"path": "genes.json", "pointer": "/ids"},
                "mask": {"path": "mask.json", "pointer": "/keep"},
            }]},
            {"path": "ranks.json", "id_selections": [{
                "value": "/top", "source": {"path": "selected.json", "pointer": "/ids"},
                "indices": {"path": "ranks.json", "pointer": "/order"},
            }]},
        ],
    }), tmp_path)
    assert snapshot is not None
    assert snapshot.verify() == ("id_selection_mismatch:ranks.json:/top",)
    (tmp_path / "ranks.json").write_text('{"order":[1,0],"top":["D","B"]}')
    assert snapshot.verify() == ()


def test_numeric_axis_length_is_checked_against_the_declared_ordered_ids(tmp_path: Path) -> None:
    header = b"{'descr': '<f8', 'fortran_order': False, 'shape': (2, 3)}\n"
    (tmp_path / "values.npy").write_bytes(
        b"\x93NUMPY\x01\x00" + struct.pack("<H", len(header)) + header
        + struct.pack("<6d", 1, 2, 3, 4, 5, 6))
    (tmp_path / "ids.json").write_text('{"samples":["a","b"],"genes":["A","B","C","D"]}')
    snapshot = capture_acceptance(PREFIX + json.dumps({
        "unchanged": [], "json": [], "arrays": [{"path": "values.npy", "shape": [None, None],
            "axes": {"0": {"path": "ids.json", "pointer": "/samples"},
                     "1": {"path": "ids.json", "pointer": "/genes"}}}],
    }), tmp_path)
    assert snapshot is not None
    assert snapshot.verify() == ("array_identity_mismatch:values.npy:1",)
    (tmp_path / "ids.json").write_text('{"samples":["a","b"],"genes":["B","C","D"]}')
    assert snapshot.verify() == ()


def test_cli_exposes_current_checkpoint_status_without_claiming_scientific_truth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from xdsh.ops.verify import main

    monkeypatch.chdir(tmp_path)
    (tmp_path / "plan.json").write_text('{"parameters":{"n":1}}')
    (tmp_path / "analysis.py").write_text("# synthetic script\n")
    (tmp_path / "contract.json").write_text(json.dumps({
        "unchanged": ["plan.json", "analysis.py"], "json": [], "calls": [{
            "path": "call.json", "source": {"path": "plan.json", "pointer": "/parameters"},
            "artifacts": {"inputs": ["plan.json"], "outputs": ["values.json"],
                          "code": ["analysis.py"], "packages": {}},
        }],
    }))

    def analyze(*, n: int) -> None:
        Path("values.json").write_text(json.dumps({"value": n}))

    verified_call(analyze, {"n": 1}, ArtifactBinding(
        "call.json", inputs=("plan.json",), outputs=("values.json",), code=("analysis.py",)), n=1)
    assert main(["--workspace", str(tmp_path), "--contract", "contract.json", "--details"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["stage_checkpoints"] == {"checked": 1, "verified": 1}
    assert report["stages"] == [{"path": "call.json", "outcome": "returned", "matched": True}]
    assert report["scientific_validity"] == "not_assessed"


def test_stage_schema_requires_frozen_code_and_prevents_overwriting_other_checkpoints(
    tmp_path: Path,
) -> None:
    from xdsh.core.artifact_acceptance import AcceptanceError

    (tmp_path / "plan.json").write_text('{"parameters":{"n":1}}')
    (tmp_path / "analysis.py").write_text("# source\n")
    artifacts = {"inputs": ["plan.json"], "outputs": ["result.json"],
                 "code": ["analysis.py"], "packages": {}}
    call = {"path": "call.json", "source": {"path": "plan.json", "pointer": "/parameters"},
            "artifacts": artifacts}
    with pytest.raises(AcceptanceError, match="artifact"):
        capture_acceptance(PREFIX + json.dumps({
            "unchanged": ["plan.json"], "json": [], "calls": [call],
        }), tmp_path)
    with pytest.raises(AcceptanceError, match="artifact"):
        capture_acceptance(PREFIX + json.dumps({
            "unchanged": ["plan.json", "analysis.py"], "json": [],
            "calls": [call, {**call, "path": "second.json"}],
        }), tmp_path)


def test_recorded_shape_uses_numeric_file_header_and_cannot_be_forged_with_booleans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "plan.json").write_text('{"parameters":{"n":1}}')
    (tmp_path / "analysis.py").write_text("# synthetic numeric writer\n")
    artifacts = {"inputs": ["plan.json"], "outputs": ["values.npy"],
                 "code": ["analysis.py"], "packages": {}}
    snapshot = capture_acceptance(PREFIX + json.dumps({
        "unchanged": ["plan.json", "analysis.py"], "json": [], "calls": [{
            "path": "call.json", "source": {"path": "plan.json", "pointer": "/parameters"},
            "artifacts": artifacts,
        }],
    }), tmp_path)
    assert snapshot is not None

    def analyze(*, n: int) -> None:
        header = b"{'descr': '<f8', 'fortran_order': False, 'shape': (1,)}\n"
        Path("values.npy").write_bytes(b"\x93NUMPY\x01\x00" + struct.pack("<H", len(header))
                                      + header + struct.pack("<d", n))

    verified_call(analyze, {"n": 1}, ArtifactBinding(
        "call.json", inputs=("plan.json",), outputs=("values.npy",), code=("analysis.py",)), n=1)
    assert snapshot.verify() == ()
    receipt = json.loads(Path("call.json").read_text())
    assert receipt["artifacts"]["outputs"]["values.npy"]["shape"] == [1]
    receipt["artifacts"]["outputs"]["values.npy"]["shape"] = [True]
    Path("call.json").write_text(json.dumps(receipt))
    assert snapshot.verify() == ("artifact_binding_mismatch:call.json",)


def test_async_serializer_cannot_relabel_an_old_file_as_a_completed_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from xdsh.core.method_evidence import MethodEvidenceError

    monkeypatch.chdir(tmp_path)
    (tmp_path / "input.json").write_text("{}")
    (tmp_path / "analysis.py").write_text("# source\n")
    (tmp_path / "old.json").write_text("{}")
    called: list[bool] = []

    def analyze(*, n: int) -> int:
        called.append(True)
        return n

    async def save(result: object) -> None:
        Path("old.json").write_text(json.dumps(result))

    binding = ArtifactBinding("call.json", inputs=("input.json",), outputs=("old.json",),
                              code=("analysis.py",), save_result=save)
    with pytest.raises(MethodEvidenceError):
        verified_call(analyze, {"n": 1}, binding, n=1)
    assert not called
    assert json.loads(Path("call.json").read_text())["outcome"] == "blocked"


@pytest.mark.parametrize("changed", ["input.json", "analysis.py"])
def test_call_cannot_succeed_after_modifying_its_declared_inputs_or_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str,
) -> None:
    from xdsh.core.method_evidence import MethodEvidenceError

    monkeypatch.chdir(tmp_path)
    Path("input.json").write_text("{}")
    Path("analysis.py").write_text("# original\n")

    def analyze(*, n: int) -> None:
        Path(changed).write_text("CHANGED")
        Path("result.json").write_text(json.dumps(n))

    with pytest.raises(MethodEvidenceError, match="artifacts"):
        verified_call(analyze, {"n": 1}, ArtifactBinding(
            "call.json", inputs=("input.json",), outputs=("result.json",),
            code=("analysis.py",)), n=1)
    assert json.loads(Path("call.json").read_text())["outcome"] == "failed"
    assert Path("result.json").exists()  # Never destroy partial results on failure.


@pytest.mark.parametrize("role", ["inputs", "code", "outputs"])
@pytest.mark.parametrize("unsafe", ["symlink", "fifo", "oversized", "missing"])
def test_stage_file_boundaries_fail_closed_without_reading_special_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str, unsafe: str,
) -> None:
    import os

    from xdsh.core.evidence_files import MAX_FILE_BYTES
    from xdsh.core.method_evidence import MethodEvidenceError

    monkeypatch.chdir(tmp_path)
    Path("input.json").write_text("{}")
    Path("analysis.py").write_text("# source\n")
    Path("result.json").write_text("{}")
    Path("private.txt").write_text("DO_NOT_DISCLOSE_FILE_BODY")
    target = Path({"inputs": "input.json", "code": "analysis.py", "outputs": "result.json"}[role])
    target.unlink()
    if unsafe == "symlink":
        target.symlink_to("private.txt")
    elif unsafe == "fifo":
        os.mkfifo(target)
    elif unsafe == "oversized":
        with target.open("wb") as stream:
            stream.truncate(MAX_FILE_BYTES + 1)
    called: list[bool] = []

    def analyze(*, n: int) -> None:
        called.append(True)

    with pytest.raises(MethodEvidenceError) as caught:
        verified_call(analyze, {"n": 1}, ArtifactBinding(
            "call.json", inputs=("input.json",), outputs=("result.json",),
            code=("analysis.py",)), n=1)
    assert "DO_NOT_DISCLOSE" not in str(caught.value)
    assert bool(called) == (role == "outputs")
    assert json.loads(Path("call.json").read_text())["outcome"] != "returned"


def test_synchronous_result_save_binds_the_actual_method_package_and_function(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from importlib.metadata import version

    monkeypatch.chdir(tmp_path)
    Path("plan.json").write_text('{"parameters":{"n":1}}')
    Path("analysis.py").write_text("# synthetic serializer\n")

    def analyze(*, n: int) -> int:
        return n * 2

    def save(result: int) -> None:
        Path("result.json").write_text(json.dumps({"value": result}))

    binding = ArtifactBinding("call.json", inputs=("plan.json",), outputs=("result.json",),
                              code=("analysis.py",), packages=("pytest",), save_result=save)
    assert verified_call(analyze, {"n": 1}, binding, n=1) == 2
    declaration = {"inputs": ["plan.json"], "outputs": ["result.json"], "code": ["analysis.py"],
                   "packages": {"pytest": version("pytest")},
                   "method": f"{analyze.__module__}.{analyze.__qualname__}"}
    spec = {"unchanged": ["plan.json", "analysis.py"], "json": [], "calls": [{
        "path": "call.json", "source": {"path": "plan.json", "pointer": "/parameters"},
        "artifacts": declaration,
    }]}
    snapshot = capture_acceptance(PREFIX + json.dumps(spec), tmp_path)
    assert snapshot is not None and snapshot.verify() == ()
    for key, invalid in (("method", "a.different_method"), ("packages", {"pytest": "0.0.0"})):
        changed = {**spec, "calls": [{"path": "call.json", "source": {
                                    "path": "plan.json", "pointer": "/parameters"},
                                    "artifacts": {**declaration, key: invalid}}]}
        other = capture_acceptance(PREFIX + json.dumps(changed), tmp_path)
        assert other is not None and other.verify() == ("artifact_binding_mismatch:call.json",)


@pytest.mark.parametrize("selector", ["mask", "indices"])
@pytest.mark.parametrize("bad", [[True, 0], [0, 0], [2, -1], [None], "all", [0, 1, 2]])
def test_ambiguous_or_invalid_id_selection_never_passes(
    tmp_path: Path, selector: str, bad: object,
) -> None:
    (tmp_path / "data.json").write_text(json.dumps({"ids": ["A", "B"], "select": bad,
                                                  "selected": ["A", "B"]}))
    snapshot = capture_acceptance(PREFIX + json.dumps({"unchanged": [], "json": [{
        "path": "data.json", "id_selections": [{"value": "/selected", "source": {
            "path": "data.json", "pointer": "/ids"}, selector: {
            "path": "data.json", "pointer": "/select"}}],
    }]}), tmp_path)
    assert snapshot is not None
    assert snapshot.verify() == ("id_selection_mismatch:data.json:/selected",)


@pytest.mark.parametrize("ids", [["A", "A"], [1, 2], ["", "B"], ["A\n", "B"], None])
def test_id_lists_must_be_unambiguous_even_when_counts_match(tmp_path: Path, ids: object) -> None:
    (tmp_path / "data.json").write_text(json.dumps({"ids": ids, "select": [True, True],
                                                  "selected": ids}))
    snapshot = capture_acceptance(PREFIX + json.dumps({"unchanged": [], "json": [{
        "path": "data.json", "id_selections": [{"value": "/selected", "source": {
            "path": "data.json", "pointer": "/ids"}, "mask": {
            "path": "data.json", "pointer": "/select"}}],
    }]}), tmp_path)
    assert snapshot is not None and snapshot.verify()


@pytest.mark.parametrize("packages", [("not-a-real-distribution-for-xdsh-test",),
                                      ("../private",), ("pytest", "pytest"), ("pytest",) * 9])
def test_missing_or_invalid_packages_block_before_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, packages: tuple[str, ...],
) -> None:
    from xdsh.core.method_evidence import MethodEvidenceError

    monkeypatch.chdir(tmp_path)
    Path("input.json").write_text("{}")
    Path("analysis.py").write_text("# code\n")

    def never_run(*, n: int) -> None:
        pytest.fail("invalid artifact setup must not execute")

    with pytest.raises(MethodEvidenceError):
        verified_call(never_run, {"n": 1}, ArtifactBinding(
            "call.json", inputs=("input.json",), outputs=("result.json",),
            code=("analysis.py",), packages=packages), n=1)
    assert json.loads(Path("call.json").read_text())["outcome"] == "blocked"


def test_serializer_failure_keeps_partial_output_but_never_reuses_success_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    Path("input.json").write_text("{}")
    Path("analysis.py").write_text("# source\n")

    def analyze(*, n: int) -> int:
        return n

    def save(result: int) -> None:
        Path("result.json").write_text(json.dumps(result))

    binding = ArtifactBinding("call.json", inputs=("input.json",), outputs=("result.json",),
                              code=("analysis.py",), save_result=save)
    verified_call(analyze, {"n": 1}, binding, n=1)

    def fail(result: int) -> None:
        Path("result.json").write_text("PARTIAL")
        raise RuntimeError("DO_NOT_COPY_EXCEPTION_BODY_INTO_RECEIPT")

    from dataclasses import replace

    with pytest.raises(RuntimeError):
        verified_call(analyze, {"n": 1}, replace(binding, save_result=fail), n=1)
    body = Path("call.json").read_text()
    assert json.loads(body)["outcome"] == "failed"
    assert "DO_NOT_COPY" not in body
    assert Path("result.json").read_text() == "PARTIAL"


@pytest.mark.parametrize("field,path", [("inputs", "../outside"), ("outputs", "/tmp/out"),
                                        ("code", "a//b"), ("outputs", "call.json")])
def test_binding_schema_rejects_unsafe_or_overlapping_paths(
    tmp_path: Path, field: str, path: str,
) -> None:
    from xdsh.core.artifact_acceptance import AcceptanceError

    Path(tmp_path / "plan.json").write_text('{"parameters":{"n":1}}')
    (tmp_path / "analysis.py").write_text("# script\n")
    declaration = {"inputs": ["plan.json"], "outputs": ["result.json"],
                   "code": ["analysis.py"], "packages": {}, field: [path]}
    with pytest.raises(AcceptanceError):
        capture_acceptance(PREFIX + json.dumps({"unchanged": ["plan.json", "analysis.py"],
            "json": [], "calls": [{"path": "call.json", "source": {
                "path": "plan.json", "pointer": "/parameters"}, "artifacts": declaration}],
        }), tmp_path)


def test_later_transformation_uses_its_own_checkpoint_and_links_to_the_previous_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    Path("plan.json").write_text('{"parameters":{"n":1}}')
    Path("analysis.py").write_text("# script\n")
    declarations = []
    for index, input_file in enumerate(("plan.json", "stage0.json")):
        declarations.append({"path": f"call{index}.json", "source": {
            "path": "plan.json", "pointer": "/parameters"}, "artifacts": {
                "inputs": [input_file], "outputs": [f"stage{index}.json"],
                "code": ["analysis.py"], "packages": {},
            }})
    snapshot = capture_acceptance(PREFIX + json.dumps({
        "unchanged": ["plan.json", "analysis.py"], "json": [], "calls": declarations,
    }), tmp_path)
    assert snapshot is not None
    for index, input_file in enumerate(("plan.json", "stage0.json")):
        def analyze(*, n: int, stage: int = index) -> None:
            Path(f"stage{stage}.json").write_text(json.dumps({"value": n + stage}))

        verified_call(analyze, {"n": 1}, ArtifactBinding(
            f"call{index}.json", inputs=(input_file,), outputs=(f"stage{index}.json",),
            code=("analysis.py",)), n=1)
    assert snapshot.verify() == ()
    Path("stage0.json").write_text('{"value":5}')
    assert snapshot.verify() == ("artifact_binding_mismatch:call0.json",
                                 "artifact_binding_mismatch:call1.json")


def test_old_parameter_only_receipt_cannot_satisfy_an_output_bound_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    Path("plan.json").write_text('{"parameters":{"n":1}}')
    Path("analysis.py").write_text("# script\n")
    snapshot = capture_acceptance(PREFIX + json.dumps({
        "unchanged": ["plan.json", "analysis.py"], "json": [], "calls": [{
            "path": "call.json", "source": {"path": "plan.json", "pointer": "/parameters"},
            "artifacts": {"inputs": ["plan.json"], "outputs": ["result.json"],
                          "code": ["analysis.py"], "packages": {}},
        }],
    }), tmp_path)
    assert snapshot is not None

    def analyze(*, n: int) -> None:
        Path("result.json").write_text(json.dumps(n))

    verified_call(analyze, {"n": 1}, "call.json", n=1)
    assert snapshot.verify() == ("artifact_binding_mismatch:call.json",)
