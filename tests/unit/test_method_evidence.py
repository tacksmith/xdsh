"""Effective arguments, not report prose, establish what a Python call received."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


def test_wrong_implicit_default_is_rejected_before_function_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from xdsh.core.method_evidence import MethodEvidenceError, verified_call

    monkeypatch.chdir(tmp_path)
    ran: list[bool] = []

    def analysis(*, center: bool = True) -> int:
        ran.append(center)
        return 42

    with pytest.raises(MethodEvidenceError, match="parameters"):
        verified_call(analysis, {"center": False}, "call.json")
    assert ran == []
    assert json.loads((tmp_path / "call.json").read_text())["outcome"] != "returned"


def test_success_receipt_is_checked_against_the_declared_method_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from xdsh.core.artifact_acceptance import capture_acceptance
    from xdsh.core.method_evidence import verified_call

    monkeypatch.chdir(tmp_path)
    (tmp_path / "method.json").write_text('{"parameters":{"center":false}}')
    contract = "XDSH-VERIFY: " + json.dumps({"unchanged": ["method.json"], "json": [],
        "calls": [{"path": "call.json", "source": {
            "path": "method.json", "pointer": "/parameters"}}]})
    snapshot = capture_acceptance(contract, tmp_path)
    assert snapshot is not None

    def analysis(*, center: bool = True) -> int:
        return 42

    assert verified_call(analysis, {"center": False}, "call.json", center=False) == 42
    assert snapshot.verify() == ()
    record = json.loads((tmp_path / "call.json").read_text())
    record["effective_parameters"]["center"] = True
    (tmp_path / "call.json").write_text(json.dumps(record))
    assert snapshot.verify() == ("method_call_mismatch:call.json",)


def test_later_failed_attempt_invalidates_a_prior_success_and_excludes_exception_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from xdsh.core.method_evidence import verified_call

    monkeypatch.chdir(tmp_path)

    def analysis(*, scale: int = 2, fail: bool = False) -> int:
        if fail:
            raise RuntimeError("PRIVATE_EXCEPTION_BODY")
        return 4 * scale

    assert verified_call(analysis, {"scale": 2}, "call.json") == 8
    with pytest.raises(RuntimeError):
        verified_call(analysis, {"scale": 2}, "call.json", fail=True)
    body = (tmp_path / "call.json").read_text()
    assert json.loads(body)["outcome"] == "failed"
    assert "PRIVATE_EXCEPTION_BODY" not in body
    assert (tmp_path / "call.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("expected", [
    {"center": 0}, {"missing": False}, {"api_key": "synthetic-private-value"},
    {"center": float("nan")}, {}, {"center": "x" * 9000},
])
def test_unsafe_or_different_parameters_cannot_invoke_function(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, expected: dict[str, object],
) -> None:
    from xdsh.core.method_evidence import MethodEvidenceError, verified_call

    monkeypatch.chdir(tmp_path)
    calls: list[bool] = []

    def analysis(*, center: bool = False) -> None:
        calls.append(True)

    with pytest.raises(MethodEvidenceError):
        verified_call(analysis, expected, "call.json")
    assert not calls
    assert json.loads((tmp_path / "call.json").read_text())["outcome"] != "returned"


@pytest.mark.parametrize("path", ["../outside.json", "/tmp/outside.json", "a//b", "a/../b"])
def test_receipt_paths_do_not_expand_execution_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str,
) -> None:
    from xdsh.core.method_evidence import MethodEvidenceError, verified_call

    monkeypatch.chdir(tmp_path)
    with pytest.raises(MethodEvidenceError):
        verified_call(lambda **kw: pytest.fail("must not execute"), {"x": 2}, path, x=2)


def test_receipt_directory_symlink_is_refused_and_cwd_changes_do_not_redirect_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from xdsh.core.method_evidence import MethodEvidenceError, verified_call

    monkeypatch.chdir(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    (tmp_path / "link").symlink_to(other, target_is_directory=True)
    with pytest.raises(MethodEvidenceError):
        verified_call(lambda **kw: None, {"x": 2}, "link/receipt.json", x=2)

    def analysis(*, x: int) -> int:
        os.chdir(other)
        return x

    assert verified_call(analysis, {"x": 2}, "receipt.json", x=2) == 2
    assert (tmp_path / "receipt.json").is_file() and not (other / "receipt.json").exists()


def test_async_or_lazy_function_is_not_misrepresented_as_executed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from collections.abc import Iterator

    from xdsh.core.method_evidence import MethodEvidenceError, verified_call

    monkeypatch.chdir(tmp_path)

    def lazy(*, n: int = 1) -> Iterator[int]:
        yield n

    async def later(*, n: int = 1) -> int:
        return n

    for function in (lazy, later):
        with pytest.raises(MethodEvidenceError):
            verified_call(function, {"n": 1}, "receipt.json")
        assert json.loads((tmp_path / "receipt.json").read_text())["outcome"] != "returned"
