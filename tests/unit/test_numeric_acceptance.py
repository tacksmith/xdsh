"""Artifact regressions from real research runs, without live data or NumPy."""
from __future__ import annotations

import hashlib
import json
import os
import struct
from pathlib import Path

import pytest

from xdsh.core.artifact_acceptance import AcceptanceError, capture_acceptance

pytestmark = pytest.mark.skipif(os.name != "posix", reason="bounded dirfd reads require POSIX")

def npy(shape: tuple[int, ...], values: list[float], *, dtype: str = "<f8") -> bytes:
    header = repr({"descr": dtype, "fortran_order": False, "shape": shape}).encode()
    header += b" " * ((64 - (10 + len(header) + 1) % 64) % 64) + b"\n"
    return b"\x93NUMPY\x01\x00" + struct.pack("<H", len(header)) + header + struct.pack(
        "<" + "d" * len(values), *values,
    )


def array_contract() -> str:
    return "XDSH-VERIFY: " + json.dumps({"unchanged": [], "json": [], "arrays": [
        {"path": "embedding.npy", "shape": [None, 2], "finite": True},
    ]})


def test_nonconstant_score_rule_rejects_the_observed_degenerate_result(tmp_path: Path) -> None:
    task = "XDSH-VERIFY: " + json.dumps({"unchanged": [], "json": [], "arrays": [{
        "path": "scores.npy", "shape": [None], "nonconstant": True,
        "minimum": 0, "maximum": 1,
    }]})
    snapshot = capture_acceptance(task, tmp_path)
    assert snapshot is not None
    (tmp_path / "scores.npy").write_bytes(npy((3,), [0.00002] * 3))
    assert snapshot.verify() == ("array_invalid:scores.npy",)
    (tmp_path / "scores.npy").write_bytes(npy((3,), [0.1, 0.2, 0.3]))
    assert snapshot.verify() == ()


def test_missing_threshold_cannot_pass_finite_numeric_requirements(tmp_path: Path) -> None:
    task = "XDSH-VERIFY: " + json.dumps({"unchanged": [], "json": [{
        "path": "summary.json", "numbers": {"/threshold": {"minimum": 0, "maximum": 1}},
    }]})
    snapshot = capture_acceptance(task, tmp_path)
    assert snapshot is not None
    for value in (None, True, "0.3", -0.1, 1.1):
        (tmp_path / "summary.json").write_text(json.dumps({"threshold": value}))
        assert snapshot.verify() == ("number_mismatch:summary.json:/threshold",)
    (tmp_path / "summary.json").write_text('{"threshold":0.3}')
    assert snapshot.verify() == ()


def test_declared_2d_embedding_rejects_a_real_30d_pca_array(tmp_path: Path) -> None:
    acceptance = capture_acceptance(array_contract(), tmp_path)
    assert acceptance is not None
    (tmp_path / "embedding.npy").write_bytes(npy((3, 30), [0.0] * 90))
    assert "array_shape_mismatch:embedding.npy" in acceptance.verify()
    (tmp_path / "embedding.npy").write_bytes(npy((3, 2), [0.0] * 6))
    assert acceptance.verify() == ()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_coordinates_fail_even_with_the_right_shape(
    tmp_path: Path, value: float,
) -> None:
    acceptance = capture_acceptance(array_contract(), tmp_path)
    assert acceptance is not None
    (tmp_path / "embedding.npy").write_bytes(npy((1, 2), [0.0, value]))
    assert "array_invalid:embedding.npy" in acceptance.verify()


@pytest.mark.parametrize("payload", [
    b"not an array", npy((1, 2), [1.0]), npy((1, 2), [1.0, 2.0], dtype="|O"),
    npy((1, 2), [1.0, 2.0]) + b"trailing data",
])
def test_malformed_truncated_object_or_extended_arrays_fail_closed(
    tmp_path: Path, payload: bytes,
) -> None:
    acceptance = capture_acceptance(array_contract(), tmp_path)
    assert acceptance is not None
    (tmp_path / "embedding.npy").write_bytes(payload)
    assert "array_invalid:embedding.npy" in acceptance.verify()


def test_reported_rate_is_checked_against_saved_numerator_and_denominator(tmp_path: Path) -> None:
    task = "XDSH-VERIFY: " + json.dumps({"unchanged": [], "json": [{
        "path": "result.json", "ratios": [{"value": "/rate", "numerator": "/n",
        "denominator": "/total", "scale": 100, "atol": 0.001}],
    }]})
    acceptance = capture_acceptance(task, tmp_path)
    assert acceptance is not None
    (tmp_path / "result.json").write_text('{"rate": 20, "n": 2, "total": 10}')
    assert acceptance.verify() == ()
    for body in ('{"rate": 2, "n": 2, "total": 10}',
                 '{"rate": 0, "n": 0, "total": 0}',
                 '{"rate": 10, "n": true, "total": 10}'):
        (tmp_path / "result.json").write_text(body)
        assert "ratio_mismatch:result.json:/rate" in acceptance.verify()


def threshold_contract() -> str:
    return "XDSH-VERIFY: " + json.dumps({"unchanged": [], "json": [{
        "path": "result.json", "threshold_rates": [{
            "observed": "observed.npy", "simulated": "simulated.npy",
            "threshold": "/threshold", "count": "/count", "total": "/total",
            "detected": "/detected", "detectable": "/detectable", "overall": "/overall",
            "scale": 100, "atol": 0.0001,
        }],
    }]})


def test_rates_use_saved_scores_not_reference_target_or_claimed_counts(tmp_path: Path) -> None:
    (tmp_path / "observed.npy").write_bytes(npy((4,), [0.1, 0.5, 0.8, 0.9]))
    (tmp_path / "simulated.npy").write_bytes(npy((4,), [0.2, 0.6, 0.7, 0.8]))
    checked = capture_acceptance(threshold_contract(), tmp_path)
    assert checked is not None
    result = {"threshold": 0.5, "count": 2, "total": 4, "detected": 50,
              "detectable": 75, "overall": 100 * 0.5 / 0.75}
    (tmp_path / "result.json").write_text(json.dumps(result))
    assert checked.verify() == ()
    for key, wrong in (("count", 3), ("total", 10), ("detected", 75),
                       ("detectable", 80), ("overall", 80), ("threshold", None)):
        (tmp_path / "result.json").write_text(json.dumps({**result, key: wrong}))
        assert checked.verify() == ("threshold_rates_mismatch:result.json:/threshold",)
    # Even self-consistent claims fail when counts do not match saved observations.
    (tmp_path / "result.json").write_text(json.dumps(
        {**result, "count": 3, "detected": 75, "overall": 100}))
    assert checked.verify()


@pytest.mark.parametrize("payload", [npy((0,), []), npy((1, 2), [0.8, 0.9]),
                                   npy((1,), [float("nan")]), b"invalid",
                                   npy((2,), [0.1, 0.2])])
def test_invalid_or_zero_detectable_vectors_fail_closed(tmp_path: Path, payload: bytes) -> None:
    (tmp_path / "observed.npy").write_bytes(npy((2,), [0.1, 0.8]))
    (tmp_path / "simulated.npy").write_bytes(payload)
    (tmp_path / "result.json").write_text(json.dumps({
        "threshold": 0.5, "count": 1, "total": 2,
        "detected": 50, "detectable": 100, "overall": 50,
    }))
    checked = capture_acceptance(threshold_contract(), tmp_path)
    assert checked is not None and checked.verify()


def test_score_comparison_strictness_and_same_read_evidence(tmp_path: Path) -> None:
    for name in ("observed.npy", "simulated.npy"):
        (tmp_path / name).write_bytes(npy((2,), [0.5, 0.9]))
    (tmp_path / "result.json").write_text(json.dumps({"threshold": 0.5, "count": 2, "total": 2,
                                                   "detected": 100, "detectable": 100,
                                                   "overall": 100}))
    spec = json.loads(threshold_contract().removeprefix("XDSH-VERIFY: "))
    spec["json"][0]["threshold_rates"][0]["comparison"] = "ge"
    checked = capture_acceptance("XDSH-VERIFY: " + json.dumps(spec), tmp_path)
    assert checked is not None
    result = checked.evaluate()
    assert not result.issues
    assert len(result.evidence) == 3
    (tmp_path / "observed.npy").unlink()
    (tmp_path / "observed.npy").symlink_to(tmp_path / "simulated.npy")
    assert checked.verify()


@pytest.mark.parametrize(("key", "value"), [("observed", "../outside"),
    ("simulated", "/tmp/outside"), ("count", "not-pointer"), ("scale", True),
    ("scale", 0), ("atol", -1), ("comparison", "guess"), ("shell", "do-not-execute")])
def test_score_contract_invalid_references_and_schema_fail_closed(
    tmp_path: Path, key: str, value: object,
) -> None:
    spec = json.loads(threshold_contract().removeprefix("XDSH-VERIFY: "))
    spec["json"][0]["threshold_rates"][0][key] = value
    with pytest.raises(AcceptanceError):
        capture_acceptance("XDSH-VERIFY: " + json.dumps(spec), tmp_path)


def difference_contract() -> str:
    return "XDSH-VERIFY: " + json.dumps({"unchanged": [], "json": [{
        "path": "result.json", "max_abs_differences": [{
            "value": "/max_error_pp", "actual": "/observed", "reference": "/reference",
            "scale": 100, "atol": 0.00001,
        }],
    }]})


def test_report_cannot_overlook_the_metric_with_the_largest_difference(tmp_path: Path) -> None:
    # Generic version of the observed report bug; no paper-specific answer.
    body = {"observed": {"small": 0.102, "large": 0.647},
            "reference": {"small": 0.10, "large": 0.62}, "max_error_pp": 0.3}
    (tmp_path / "result.json").write_text(json.dumps(body))
    snapshot = capture_acceptance(difference_contract(), tmp_path)
    assert snapshot is not None
    assert snapshot.verify() == ("max_difference_mismatch:result.json:/max_error_pp",)
    body["max_error_pp"] = 2.7
    (tmp_path / "result.json").write_text(json.dumps(body))
    assert snapshot.verify() == ()


def test_one_verification_does_not_combine_two_different_versions_of_an_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from xdsh.core.artifact_acceptance import ArtifactAcceptance

    task = "XDSH-VERIFY: " + json.dumps({"unchanged": [], "json": [
        {"path": "result.json", "equals": {"/x": 1}},
        {"path": "result.json", "equals": {"/x": 2}},
    ]})
    snapshot = capture_acceptance(task, tmp_path)
    assert snapshot is not None
    reads = 0

    def changing_read(self: ArtifactAcceptance, path: str) -> bytes:
        nonlocal reads
        reads += 1
        return b'{"x":1}' if reads == 1 else b'{"x":2}'

    monkeypatch.setattr(ArtifactAcceptance, "_read", changing_read)
    assert snapshot.verify() == ("field_mismatch:result.json:/x",)
    assert reads == 1


@pytest.mark.parametrize(("observed", "reference"), [
    ({"a": 1}, {"b": 1}), ({"a": 1, "b": 2}, {"a": 1}),
    ({"a": True}, {"a": 1}), ({"a": "1"}, {"a": 1}), ({}, {}),
    ([1], [1]), ({"a": float("nan")}, {"a": 1}),
    ({"a": 1e308}, {"a": -1e308}),
    ({str(i): 1 for i in range(129)}, {str(i): 1 for i in range(129)}),
])
def test_difference_check_requires_complete_labels_and_finite_numbers(
    tmp_path: Path, observed: object, reference: object,
) -> None:
    (tmp_path / "result.json").write_text(json.dumps({
        "observed": observed, "reference": reference, "max_error_pp": 0,
    }))
    snapshot = capture_acceptance(difference_contract(), tmp_path)
    assert snapshot is not None
    assert snapshot.verify()


@pytest.mark.parametrize(("key", "value"), [
    ("actual", "not-a-pointer"), ("scale", 0), ("scale", -1),
    ("scale", True), ("scale", float("inf")), ("atol", -1),
    ("atol", False), ("exec", "never execute"),
])
def test_invalid_difference_rules_fail_before_execution(
    tmp_path: Path, key: str, value: object,
) -> None:
    spec = json.loads(difference_contract().removeprefix("XDSH-VERIFY: "))
    spec["json"][0]["max_abs_differences"][0][key] = value
    with pytest.raises(AcceptanceError):
        capture_acceptance("XDSH-VERIFY: " + json.dumps(spec), tmp_path)


def test_audit_receipt_identifies_the_bytes_checked_not_a_later_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from xdsh.core.artifact_acceptance import ArtifactAcceptance

    content = json.dumps({"observed": {"one": 0.4}, "reference": {"one": 0.5},
                          "max_error_pp": 10}).encode()
    path = tmp_path / "result.json"
    path.write_bytes(content)
    snapshot = capture_acceptance(difference_contract(), tmp_path)
    assert snapshot is not None
    original_read = ArtifactAcceptance._read

    def changing_read(self: ArtifactAcceptance, relative: str) -> bytes:
        result = original_read(self, relative)
        path.write_text('{"private_note":"modified after the read"}')
        return result

    monkeypatch.setattr(ArtifactAcceptance, "_read", changing_read)
    result = snapshot.evaluate()
    assert result.issues == ()
    assert result.evidence[0].sha256 == hashlib.sha256(content).hexdigest()
    assert result.evidence[0].size_bytes == len(content)
    assert result.numeric_checks[0].expected_max == pytest.approx(10)
    assert snapshot.verify()  # The next invocation sees the changed file.


@pytest.mark.parametrize("reported", [None, True, 0.3, 10])
def test_cli_details_are_computed_not_model_claims_and_do_not_echo_file_bodies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], reported: object,
) -> None:
    from xdsh.ops.verify import main

    body: dict[str, object] = {
        "observed": {"one": 0.4}, "reference": {"one": 0.5},
        "private_note": "NEVER_ECHO_THIS_BODY",
    }
    if reported is not None:
        body["max_error_pp"] = reported
    (tmp_path / "result.json").write_text(json.dumps(body))
    (tmp_path / "checks.json").write_text(difference_contract().removeprefix("XDSH-VERIFY: "))
    assert main(["--workspace", str(tmp_path), "--contract", "checks.json", "--details"]) == (
        0 if reported == 10 else 1
    )
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["numeric_checks"][0]["expected_max"] == pytest.approx(10)
    assert result["numeric_checks"][0]["compared_metrics"] == 1
    assert result["evidence"][0]["path"] == "result.json"
    assert result["scientific_validity"] == "not_assessed"
    assert result["evidence_scope"] == "bytes_read_during_this_check_not_later_files"
    assert "NEVER_ECHO_THIS_BODY" not in output and "private_note" not in output


def test_declared_methods_are_not_overridden_by_the_report(tmp_path: Path) -> None:
    # Existing immutable equality contract already handles a method mapping;
    # it must not infer scientific authority from a model-written success flag.
    task = "XDSH-VERIFY: " + json.dumps({"unchanged": [], "json": [{
        "path": "result.json", "equals": {"/parameters/mean_center": False,
        "/target_guided": False},
    }]})
    acceptance = capture_acceptance(task, tmp_path)
    assert acceptance is not None
    (tmp_path / "result.json").write_text(json.dumps({
        "parameters": {"mean_center": True}, "target_guided": True, "success": True,
    }))
    assert len(acceptance.verify()) == 2


@pytest.mark.parametrize("shape", [[True, 2], [-1, 2], ["*", 2], [None] * 9])
def test_invalid_contract_shapes_fail_before_task_execution(
    tmp_path: Path, shape: list[object],
) -> None:
    task = "XDSH-VERIFY: " + json.dumps({"unchanged": [], "json": [], "arrays": [
        {"path": "embedding.npy", "shape": shape},
    ]})
    with pytest.raises(AcceptanceError):
        capture_acceptance(task, tmp_path)


def test_array_symlink_cannot_escape_workspace(tmp_path: Path) -> None:
    acceptance = capture_acceptance(array_contract(), tmp_path)
    assert acceptance is not None
    (tmp_path / "embedding.npy").symlink_to(tmp_path / "secret.npy")
    (tmp_path / "secret.npy").write_bytes(npy((1, 2), [1.0, 2.0]))
    assert acceptance.verify() == ("array_invalid:embedding.npy",)


@pytest.mark.parametrize("raw", [
    '{"unchanged":[],"json":[{"path":"r.json","equals":{"/x":1e999}}]}',
    '{"unchanged":[],"json":[],"arrays":[{"path":"../secret","shape":[2]}]}',
    '{"unchanged":[],"json":[],"arrays":[{"path":"x","shape":[2],"finite":1}]}',
    '{"unchanged":[],"json":[{"path":"r","ratios":[{"value":"/v",'
    '"numerator":"/n","denominator":"/d","atol":-1}]}]}',
])
def test_invalid_numeric_rules_are_rejected(tmp_path: Path, raw: str) -> None:
    with pytest.raises(AcceptanceError):
        capture_acceptance("XDSH-VERIFY: " + raw, tmp_path)


@pytest.mark.parametrize("major", [1, 2, 3])
def test_supported_npy_versions_and_big_endian_float32(major: int) -> None:
    from xdsh.core.numeric_artifacts import numeric_array_shape

    header = b"{'descr': '>f4', 'fortran_order': True, 'shape': (2,)}\n"
    size = 2 if major == 1 else 4
    data = (b"\x93NUMPY" + bytes([major, 0]) + len(header).to_bytes(size, "little")
            + header + struct.pack(">ff", 1.0, 2.0))
    assert numeric_array_shape(data) == (2,)


@pytest.mark.parametrize("header", [
    "{'descr':'<f8','fortran_order':False,'shape':(__import__('os'),)}",
    "{'descr':'<f8','fortran_order':False,'shape':(True,)}",
    "{'descr':'<f8','fortran_order':False,'shape':(-1,)}",
    "{'descr':'<f8','descr':'<f8','shape':(1,)}",
])
def test_array_header_is_data_never_executable(header: str) -> None:
    from xdsh.core.numeric_artifacts import numeric_array_shape

    raw = header.encode()
    data = b"\x93NUMPY\x01\x00" + struct.pack("<H", len(raw)) + raw
    with pytest.raises(ValueError):
        numeric_array_shape(data)


def test_cli_reports_scope_and_returns_nonzero_on_mismatch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    from xdsh.ops.verify import main

    (tmp_path / "checks.json").write_text(array_contract().removeprefix("XDSH-VERIFY: "))
    (tmp_path / "embedding.npy").write_bytes(npy((1, 30), [0.0] * 30))
    assert main(["--workspace", str(tmp_path), "--contract", "checks.json"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["scientific_validity"] == "not_assessed"
    assert result["assurance"] == "declared_artifact_checks_only"
    assert result["issues"] == ["array_shape_mismatch:embedding.npy"]
    (tmp_path / "embedding.npy").write_bytes(npy((1, 2), [0.0, 0.0]))
    assert main(["--workspace", str(tmp_path), "--contract", "checks.json"]) == 0
    assert json.loads(capsys.readouterr().out)["checks_passed"] is True


def test_cli_invalid_input_never_echoes_untrusted_content(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    from xdsh.ops.verify import main

    (tmp_path / "checks.json").write_text('{"private_marker":"do not echo"}')
    assert main(["--workspace", str(tmp_path), "--contract", "checks.json"]) == 2
    output = capsys.readouterr().out
    assert "private_marker" not in output and "do not echo" not in output
    assert "invalid_or_inaccessible_contract" in output


def test_cli_requires_a_bounded_relative_contract(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    from xdsh.ops.verify import main

    assert main(["--workspace", str(tmp_path), "--contract", "../outside.json"]) == 2
    assert json.loads(capsys.readouterr().out)["checks_passed"] is False


@pytest.mark.parametrize("kind", ["oversized_file", "oversized_header", "fifo", "archive"])
def test_numeric_checker_rejects_unbounded_or_nonregular_inputs(tmp_path: Path, kind: str) -> None:
    from xdsh.core.artifact_acceptance import MAX_FILE_BYTES

    target = tmp_path / "embedding.npy"
    if kind == "oversized_file":
        with target.open("wb") as stream:
            stream.truncate(MAX_FILE_BYTES + 1)
    elif kind == "oversized_header":
        target.write_bytes(b"\x93NUMPY\x02\x00" + struct.pack("<I", 4097) + b" " * 4097)
    elif kind == "fifo":
        os.mkfifo(target)
    else:
        target.write_bytes(b"PK\x03\x04" + b"not an uncompressed NPY")
    acceptance = capture_acceptance(array_contract(), tmp_path)
    assert acceptance is not None
    assert acceptance.verify() == ("array_invalid:embedding.npy",)


def test_array_rules_are_captured_before_model_edits_contract_file(tmp_path: Path) -> None:
    from xdsh.core.artifact_acceptance import read_declared_contract

    contract = tmp_path / "contract.json"
    contract.write_text(array_contract().removeprefix("XDSH-VERIFY: "))
    acceptance = capture_acceptance(read_declared_contract(tmp_path, "contract.json"), tmp_path)
    assert acceptance is not None
    spec = json.loads(contract.read_text())
    spec["arrays"][0]["shape"] = [None, 30]
    contract.write_text(json.dumps(spec))
    (tmp_path / "embedding.npy").write_bytes(npy((1, 30), [0.0] * 30))
    assert acceptance.verify() == ("array_shape_mismatch:embedding.npy",)


def test_posthoc_cli_cannot_claim_an_experiment_start_baseline(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    from xdsh.ops.verify import main

    (tmp_path / "checks.json").write_text('{"unchanged":["source.txt"],"json":[]}')
    (tmp_path / "source.txt").write_text("possibly modified before this check")
    assert main(["--workspace", str(tmp_path), "--contract", "checks.json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["before_state"] == "captured_at_this_check_not_at_experiment_start"
