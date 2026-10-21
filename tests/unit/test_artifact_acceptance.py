from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from xdsh.core.artifact_acceptance import AcceptanceError, capture_acceptance

pytestmark = pytest.mark.skipif(os.name != "posix", reason="safe artifact I/O requires POSIX")


def contract(source: str = "source.txt", report: str = "report.json") -> str:
    return "XDSH-VERIFY: " + json.dumps({"unchanged": [source], "json": [{
        "path": report, "equals": {"/ids": ["A"], "/allowed": False},
        "source_sha256": {"/before": source, "/after": source},
    }]}) + "\nDo the task."


def setup_files(root: Path) -> None:
    (root / "source.txt").write_text("original")
    digest = hashlib.sha256(b"original").hexdigest()
    (root / "report.json").write_text(json.dumps({
        "ids": ["A"], "allowed": False, "before": digest, "after": digest,
    }))


def test_verified_values_and_original_before_after_are_independent(tmp_path: Path) -> None:
    setup_files(tmp_path)
    snapshot = capture_acceptance(contract(), tmp_path)
    assert snapshot is not None and snapshot.verify() == ()
    (tmp_path / "source.txt").write_text("changed")
    assert snapshot.verify() == ("source_changed:source.txt",)
    (tmp_path / "report.json").write_text('{"ids":["B"],"allowed":0}')
    issues = snapshot.verify()
    assert "field_mismatch:report.json:/ids" in issues
    assert "field_mismatch:report.json:/allowed" in issues  # bool is not integer
    assert all("original" not in issue and "changed\"" not in issue for issue in issues)


@pytest.mark.parametrize("path", ["../outside", "/tmp/outside", "a/../outside", "a//b",
                                  "./source", "a\\b", "a\x00b"])
def test_contract_rejects_traversal_before_any_execution(tmp_path: Path, path: str) -> None:
    with pytest.raises(AcceptanceError):
        capture_acceptance(contract(path), tmp_path)


def test_no_contract_or_quoted_example_never_reads_workspace(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    assert capture_acceptance("hello", missing) is None
    assert capture_acceptance("Explain this:\n" + contract(), missing) is None


def test_source_and_output_symlinks_are_refused(tmp_path: Path) -> None:
    setup_files(tmp_path)
    snapshot = capture_acceptance(contract(), tmp_path)
    assert snapshot is not None
    (tmp_path / "report.json").unlink()
    (tmp_path / "report.json").symlink_to(tmp_path / "source.txt")
    assert snapshot.verify() == ("json_unavailable:report.json",)
    (tmp_path / "link").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(AcceptanceError):
        capture_acceptance(contract("link/source.txt"), tmp_path)


@pytest.mark.parametrize("raw", [
    '{"unchanged":[],"unchanged":[],"json":[]}',
    '{"unchanged":[],"json":[],"shell":"touch bad"}',
    '{"unchanged":[],"json":[{"path":"r.json","equals":{"/x":NaN}}]}',
    '{"unchanged":[],"json":[{"path":"r.json","source_sha256":{"/x":"unknown"}}]}',
    '{"unchanged":[],"json":[]}',
])
def test_invalid_or_ambiguous_schema_fails_closed(tmp_path: Path, raw: str) -> None:
    with pytest.raises(AcceptanceError):
        capture_acceptance("XDSH-VERIFY: " + raw, tmp_path)


def test_non_regular_and_oversized_inputs_are_not_read(tmp_path: Path) -> None:
    from xdsh.core.artifact_acceptance import MAX_FILE_BYTES

    (tmp_path / "source.txt").write_bytes(b"a" * (MAX_FILE_BYTES + 1))
    with pytest.raises(AcceptanceError):
        capture_acceptance(contract(), tmp_path)
    (tmp_path / "source.txt").unlink()
    os.mkfifo(tmp_path / "source.txt")
    with pytest.raises(AcceptanceError):
        capture_acceptance(contract(), tmp_path)


def test_product_context_preserves_only_explicit_first_line_contract(tmp_path: Path) -> None:
    from xdsh.core.product_control import ProductSessionControl

    request = contract() + "\n1. First\n2. Second\n3. Third"
    prepared = ProductSessionControl(tmp_path / "state").prepare_task(
        "test", request, workspace=tmp_path,
    )
    assert prepared.task.splitlines()[0] == request.splitlines()[0]


def test_missing_safe_descriptor_support_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup_files(tmp_path)
    monkeypatch.setattr(os, "supports_dir_fd", set())
    with pytest.raises(AcceptanceError, match="unavailable"):
        capture_acceptance(contract(), tmp_path)


def parameter_contract() -> str:
    return "XDSH-VERIFY: " + json.dumps({
        "unchanged": ["method.json"],
        "json": [{"path": "run.json", "source_json": {
            "/parameters": {"path": "method.json", "pointer": "/parameters"},
        }}],
    })


def test_parameter_defaults_cannot_silently_replace_frozen_method(tmp_path: Path) -> None:
    # Real R2 bug pattern: finite arrays can pass while a default changes the method.
    wanted = {"mean_center": False, "min_counts": 3}
    (tmp_path / "method.json").write_text(json.dumps({"parameters": wanted}))
    snapshot = capture_acceptance(parameter_contract(), tmp_path)
    assert snapshot is not None
    (tmp_path / "run.json").write_text(json.dumps({"parameters": {
        "mean_center": True, "min_counts": 3}}))
    assert snapshot.verify() == ("field_mismatch:run.json:/parameters",)
    (tmp_path / "run.json").write_text(json.dumps({"parameters": wanted}))
    assert snapshot.verify() == ()
    # Altering both claims after execution still cannot bypass the before-state.
    (tmp_path / "method.json").write_text('{"parameters": {"mean_center": true}}')
    (tmp_path / "run.json").write_text('{"parameters": {"mean_center": true}}')
    assert "source_changed:method.json" in snapshot.verify()


@pytest.mark.parametrize("reference", [
    {"path": "../outside", "pointer": "/params"},
    {"path": "undeclared.json", "pointer": "/params"},
    {"path": "method.json", "pointer": "params"},
    {"path": "method.json", "pointer": "/params", "exec": "forbidden"},
    "method.json",
])
def test_source_json_contract_rejects_unbounded_references(
    tmp_path: Path, reference: object,
) -> None:
    (tmp_path / "method.json").write_text('{"parameters": {}}')
    spec = json.loads(parameter_contract().split(": ", 1)[1])
    spec["json"][0]["source_json"]["/parameters"] = reference
    with pytest.raises(AcceptanceError):
        capture_acceptance("XDSH-VERIFY: " + json.dumps(spec), tmp_path)


@pytest.mark.parametrize("source_body", ['{"wrong":1}', '{"parameters":NaN}', 'not-json'])
def test_missing_source_field_never_passes_an_unchecked_parameter_comparison(
    tmp_path: Path, source_body: str,
) -> None:
    (tmp_path / "method.json").write_text(source_body)
    (tmp_path / "run.json").write_text('{"parameters":null}')
    snapshot = capture_acceptance(parameter_contract(), tmp_path)
    assert snapshot is not None and snapshot.verify()


def test_source_json_refs_are_read_nofollow_at_verification(tmp_path: Path) -> None:
    (tmp_path / "method.json").write_text('{"parameters":false}')
    (tmp_path / "run.json").write_text('{"parameters":false}')
    snapshot = capture_acceptance(parameter_contract(), tmp_path)
    assert snapshot is not None
    (tmp_path / "method.json").unlink()
    (tmp_path / "method.json").symlink_to(tmp_path / "run.json")
    issues = snapshot.verify()
    assert "source_unavailable:method.json" in issues
    assert "field_mismatch:run.json:/parameters" in issues


def test_source_hash_and_parameter_comparison_use_the_same_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from xdsh.core.artifact_acceptance import ArtifactAcceptance

    before = b'{"parameters":false}'
    after = b'{"parameters":true}'
    (tmp_path / "method.json").write_bytes(before)
    (tmp_path / "run.json").write_bytes(after)
    snapshot = capture_acceptance(parameter_contract(), tmp_path)
    assert snapshot is not None
    original = ArtifactAcceptance._read
    reads = 0

    def changing_read(self: ArtifactAcceptance, path: str) -> bytes:
        nonlocal reads
        if path == "method.json":
            reads += 1
            return before if reads == 1 else after
        return original(self, path)

    monkeypatch.setattr(ArtifactAcceptance, "_read", changing_read)
    assert snapshot.verify() == ("field_mismatch:run.json:/parameters",)
    assert reads == 1
