"""Regressions from different-cwd and nested-checklist research deliveries."""
from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest

from xdsh.core.artifact_acceptance import PREFIX, capture_acceptance
from xdsh.core.product_control import ProductSessionControl
from xdsh.ops.verify import main


def declaration(root: Path) -> str:
    (root / "method.json").write_text('{"parameters":{"center":false}}')
    contract = {"unchanged": ["method.json"], "json": [{
        "path": "results.json", "source_json": {"/parameters": {
            "path": "method.json", "pointer": "/parameters"}},
        "numbers": {"/estimate": {}},
    }]}
    (root / "checks.json").write_text(json.dumps(contract))
    return PREFIX + json.dumps({"unchanged": [], "json": [], "deferred": {
        "path": "checks.json", "profile": "research",
    }})


def test_prepared_research_pins_host_root_and_exact_checklist(tmp_path: Path) -> None:
    root = tmp_path / "space and 'quote"
    root.mkdir()
    prepared = ProductSessionControl(tmp_path / "state").prepare_task(
        "s", "Please reproduce the paper analysis.", workspace=root,
    )
    path = json.loads(prepared.task.splitlines()[0][len(PREFIX):])["deferred"]["path"]
    command = (f"xdsh-verify --workspace {shlex.quote(str(root))} --contract {path} "
               "--profile research --research-revision 2")
    assert command + " --preflight" in prepared.task
    assert command + " --details" in prepared.task
    assert "Do not copy the XDSH-VERIFY header" in prepared.task
    assert "subdirectory" in prepared.task


def test_preflight_is_not_a_false_success_for_missing_outputs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    header = declaration(tmp_path)
    child = tmp_path / "analysis"
    child.mkdir()
    monkeypatch.chdir(child)
    args = ["--workspace", str(tmp_path), "--contract", "checks.json", "--profile", "research"]
    assert main([*args, "--preflight"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["declaration_valid"] is True
    assert report["checks_passed"] is None  # No claim about uncreated outputs.
    assert report["workspace"] == str(tmp_path)
    assert report["contract"] == "checks.json"
    assert report["scientific_validity"] == "not_assessed"
    host = capture_acceptance(header, tmp_path)
    assert host is not None
    frozen = host.capture_deferred()
    assert main(args) == 1
    assert json.loads(capsys.readouterr().out)["issues"] == list(frozen.verify())
    (tmp_path / "results.json").write_text('{"parameters":{"center":false},"estimate":0.3}')
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["issues"] == list(frozen.verify()) == []


@pytest.mark.parametrize(("case", "code"), [
    ("missing", "checklist_unavailable"),
    ("subdirectory", "checklist_unavailable"),
    ("recursive", "checklist_recursive"),
    ("malformed", "checklist_invalid"),
    ("no_method", "method_checks_required"),
    ("no_outputs", "output_checks_required"),
    ("missing_source", "checklist_inputs_unavailable"),
    ("symlink", "checklist_unavailable"),
])
def test_host_and_cli_reject_same_bad_declaration_with_actionable_diagnostics(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], case: str, code: str,
) -> None:
    header = declaration(tmp_path)
    path = tmp_path / "checks.json"
    if case == "missing":
        path.unlink()
    elif case == "subdirectory":
        (tmp_path / "analysis").mkdir()
        path.rename(tmp_path / "analysis/checks.json")
    elif case == "recursive":
        path.write_text(header[len(PREFIX):])
    elif case == "malformed":
        path.write_text('{"private":"NEVER_ECHO_THIS",broken')
    elif case == "no_method":
        path.write_text('{"unchanged":[],"json":[{"path":"r","numbers":{"/x":{}}}]}')
    elif case == "no_outputs":
        spec = json.loads(path.read_text())
        del spec["json"][0]["numbers"]
        path.write_text(json.dumps(spec))
    elif case == "missing_source":
        (tmp_path / "method.json").unlink()
    elif case == "symlink":
        path.rename(tmp_path / "real.json")
        path.symlink_to(tmp_path / "real.json")
    assert main(["--workspace", str(tmp_path), "--contract", "checks.json",
                 "--profile", "research", "--preflight"]) == 2
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["reason"] == code
    assert report["remedy"] and "NEVER_ECHO_THIS" not in output
    host = capture_acceptance(header, tmp_path)
    assert host is not None and host.verify()
    with pytest.raises(ValueError) as caught:
        host.capture_deferred()
    assert getattr(caught.value, "reason", None) == code


def test_preflight_does_not_reset_host_frozen_hashes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    host = capture_acceptance(declaration(tmp_path), tmp_path)
    assert host is not None
    frozen = host.capture_deferred()
    (tmp_path / "method.json").write_text('{"parameters":{"center":true}}')
    assert main(["--workspace", str(tmp_path), "--contract", "checks.json",
                 "--profile", "research", "--preflight"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["before_state"] == "captured_at_this_check_not_at_experiment_start"
    assert "source_changed:method.json" in frozen.verify()


def test_max_difference_requires_declared_complete_metric_set(tmp_path: Path) -> None:
    spec = {"unchanged": [], "json": [{"path": "r.json", "max_abs_differences": [{
        "value": "/maximum", "actual": "/actual", "reference": "/reference",
        "required_metrics": ["detected", "detectable", "overall"],
    }]}]}
    host = capture_acceptance(PREFIX + json.dumps(spec), tmp_path)
    assert host is not None
    body = {"actual": {"detected": 1, "overall": 2},
            "reference": {"detected": 1, "overall": 2}, "maximum": 0}
    (tmp_path / "r.json").write_text(json.dumps(body))
    assert host.verify() == ("max_difference_mismatch:r.json:/maximum",)
    body["actual"]["detectable"] = 3  # type: ignore[index]
    body["reference"]["detectable"] = 3  # type: ignore[index]
    (tmp_path / "r.json").write_text(json.dumps(body))
    assert host.verify() == ()


def test_multiple_dataset_calls_cannot_share_one_overwritten_receipt(tmp_path: Path) -> None:
    (tmp_path / "plan.json").write_text('{"a":{"center":false},"b":{"center":true}}')
    spec = {"unchanged": ["plan.json"], "json": [], "calls": [
        {"path": "one-receipt.json", "source": {"path": "plan.json", "pointer": "/a"}},
        {"path": "one-receipt.json", "source": {"path": "plan.json", "pointer": "/b"}},
    ]}
    with pytest.raises(ValueError, match="unique method receipt"):
        capture_acceptance(PREFIX + json.dumps(spec), tmp_path)


@pytest.mark.parametrize("labels", [[], ["a", "a"], [True], [1], [None],
                                   {"a": 1}, "a", ["a\n"], ["x" * 121],
                                   [str(i) for i in range(129)]])
def test_invalid_coverage_labels_fail_closed(tmp_path: Path, labels: object) -> None:
    spec = {"unchanged": [], "json": [{"path": "r.json", "max_abs_differences": [{
        "value": "/max", "actual": "/actual", "reference": "/reference",
        "required_metrics": labels,
    }]}]}
    with pytest.raises(ValueError, match="required metric labels"):
        capture_acceptance(PREFIX + json.dumps(spec), tmp_path)


def test_continuation_has_specific_checklist_repair_without_execution_replay(
    tmp_path: Path,
) -> None:
    from xdsh.core.product_control import TurnUsage

    control = ProductSessionControl(tmp_path / "state")
    control.record_turn("s", user_text="Please reproduce the paper analysis.",
                        assistant_text="Results saved.", model="test", usage=TurnUsage(),
                        succeeded=False, verification_issues=("checklist",))
    prepared = control.prepare_task("s", "continue", workspace=tmp_path)
    assert "exact host workspace" in prepared.task
    assert "--profile research --preflight" in prepared.task
    assert "Do not replay completed operations" in prepared.task


@pytest.mark.parametrize(("body", "reason", "remedy_fragment"), [
    ({"json": []}, "checklist_fields_required", "unchanged and json"),
    ({"unchanged": [], "json": [], "arrays": [
        {"path": "r.json", "json_pointer": "/ids", "shape": [3]},
    ]}, "array_schema_invalid", "numeric .npy"),
    ({"unchanged": [], "json": [], "calls": [
        {"path": "call.json", "source": {"path": "missing.json", "pointer": "/parameters"}},
    ]}, "method_schema_invalid", "listed in unchanged"),
    ({"unchanged": [], "json": [{"path": "r.json", "unknown": "NEVER_ECHO_THIS"}]},
     "json_schema_invalid", "supported assertions"),
])
def test_schema_diagnostics_prevent_trial_and_error_without_echoing_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], body: object,
    reason: str, remedy_fragment: str,
) -> None:
    (tmp_path / "check.json").write_text(json.dumps(body))
    assert main(["--workspace", str(tmp_path), "--contract", "check.json",
                 "--profile", "research", "--preflight"]) == 2
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["reason"] == reason
    assert remedy_fragment in result["remedy"]
    assert "NEVER_ECHO_THIS" not in output


@pytest.mark.parametrize("bounds", [
    {"eq": 1}, {"finite": True}, {"min": 0}, {"maximum": True},
    {"minimum": "NEVER_ECHO_THIS"}, {"minimum": 2, "maximum": 1},
])
def test_numeric_preflight_gives_exact_fix_instead_of_schema_guessing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], bounds: object,
) -> None:
    declaration(tmp_path)
    path = tmp_path / "checks.json"
    spec = json.loads(path.read_text())
    spec["json"][0]["numbers"] = {"/estimate": bounds}
    path.write_text(json.dumps(spec))
    args = ["--workspace", str(tmp_path), "--contract", "checks.json",
            "--profile", "research", "--preflight"]
    assert main(args) == 2
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["reason"] == "numeric_schema_invalid"
    for fragment in ("{}", "minimum", "maximum", "finite", "equals", "same checklist"):
        assert fragment in result["remedy"]
    assert "NEVER_ECHO_THIS" not in output
    # Use the documented syntax, preserving the original requirement (finite
    # estimate + exact value), rather than dropping the failing check.
    spec["json"][0]["numbers"] = {"/estimate": {}}
    spec["json"][0]["equals"] = {"/estimate": 1}
    path.write_text(json.dumps(spec))
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["declaration_valid"] is True


def test_research_numeric_guidance_is_available_before_the_first_attempt(tmp_path: Path) -> None:
    task = ProductSessionControl(tmp_path / "state").prepare_task(
        "s", "Please reproduce the paper analysis.", workspace=tmp_path,
    ).task
    assert '"numbers":{"/value":{}}' in task
    assert "numeric equals instead of bounds inside that expectation" in task
    assert "minimum/maximum" in task
