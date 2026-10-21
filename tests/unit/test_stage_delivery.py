"""Stage checks reach the real CLI, persisted host baseline and run guard."""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from xdsh.core.acceptance_store import AcceptanceStore
from xdsh.core.artifact_acceptance import PREFIX, capture_acceptance
from xdsh.core.events import ErrorEvent, FinishEvent, RunnerEventT, ToolResultEvent
from xdsh.core.method_evidence import ArtifactBinding, verified_call
from xdsh.core.run_guard import GuardedRunner
from xdsh.ops.verify import main


def test_stage_examples_execute_and_a_restarted_cli_keeps_the_original_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "work"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    assert main(["--stage-example"]) == 0
    contract = capsys.readouterr().out
    assert main(["--stage-call-example"]) == 0
    script = capsys.readouterr().out
    Path("contract.json").write_text(contract)
    Path("analysis.py").write_text(script)
    Path("method-plan.json").write_text('{"parameters":{"scale":2.0}}')
    Path("input.json").write_text('{"ids":["a","b"],"values":[2.0,4.0]}')
    pending = capture_acceptance(PREFIX + json.dumps({
        "research_revision": 2, "unchanged": [], "json": [], "deferred": {
            "path": "contract.json", "profile": "research"},
    }), workspace)
    assert pending is not None
    store = AcceptanceStore(tmp_path / "host")
    binding = store.begin(pending, "s")
    args = ["--workspace", str(workspace), "--contract", "contract.json", "--profile", "research",
            "--baseline", str(binding.path)]
    assert main([*args, "--preflight"]) == 0
    assert json.loads(capsys.readouterr().out)["checks_passed"] is None
    store.freeze(binding, pending.capture_deferred(), capture_point="tool_boundary")
    # This is our printed example, not arbitrary validators or live research code.
    exec(compile(script, str(workspace / "analysis.py"), "exec"), {"__name__": "__main__"})
    assert json.loads(Path("results.json").read_text())["estimate"] == 12.0
    assert main([*args, "--details"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["stage_checkpoints"] == {"checked": 1, "verified": 1}
    assert report["baseline_scope"] == "host_frozen"
    restored = AcceptanceStore(store.root).load(workspace, "contract.json")
    assert restored.snapshot is not None and not restored.snapshot.verify()
    result = json.loads(Path("results.json").read_text())
    result["estimate"] = 999
    Path("results.json").write_text(json.dumps(result))
    assert main([*args, "--details"]) == 1
    failed = json.loads(capsys.readouterr().out)
    assert failed["stage_checkpoints"] == {"checked": 1, "verified": 0}
    assert failed["baseline_id"] == report["baseline_id"]
    assert "artifact_binding_mismatch:method-call.json" in failed["issues"]


class ResearchRunner:
    """Only the external model is substituted; guard and evidence code are real."""

    def __init__(self, overwrite: bool) -> None:
        self.overwrite = overwrite
        self.runs = 0

    async def run(self, task: str, *, session_id: str, cwd: Path,
                  **options: object) -> AsyncIterator[RunnerEventT]:
        self.runs += 1

        def analyze(*, n: int) -> None:
            (cwd / "result.json").write_text(json.dumps({"value": n}))

        verified_call(analyze, {"n": 1}, ArtifactBinding(
            "call.json", inputs=("plan.json",), outputs=("result.json",),
            code=("analysis.py",)), n=1)
        yield ToolResultEvent(session_id=session_id, call_id="run", name="bash")
        if self.overwrite:
            (cwd / "result.json").write_text('{"value":2}')
        yield FinishEvent(session_id=session_id, reason="completed", final_text="Partial analysis.")

    async def cancel(self, session_id: str) -> bool:
        return True

    async def reset(self, session_id: str) -> None:
        raise AssertionError("must not reset completed work")

    async def close(self) -> None:
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("overwrite", [True, False])
@pytest.mark.parametrize("deferred", [True, False])
async def test_host_rejects_overwritten_output_while_preserving_the_answer_and_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overwrite: bool, deferred: bool,
) -> None:
    monkeypatch.chdir(tmp_path)
    Path("plan.json").write_text('{"parameters":{"n":1}}')
    Path("analysis.py").write_text("# synthetic writer\n")
    spec = {"unchanged": ["plan.json", "analysis.py"], "json": [
        {"path": "result.json", "numbers": {"/value": {}}}],
        "calls": [{"path": "call.json", "source": {"path": "plan.json", "pointer": "/parameters"},
                   "artifacts": {"inputs": ["plan.json"], "outputs": ["result.json"],
                                 "code": ["analysis.py"], "packages": {}}}]}
    Path("contract.json").write_text(json.dumps(spec))
    task = PREFIX + json.dumps({"unchanged": [], "json": [], "deferred": {
        "path": "contract.json", "profile": "research"}} if deferred else spec)
    delegate = ResearchRunner(overwrite)
    events = [event async for event in GuardedRunner(delegate).run(
        task, session_id="s", cwd=tmp_path)]
    finish = events[-1]
    assert isinstance(finish, FinishEvent)
    assert finish.reason == ("error" if overwrite else "completed")
    assert finish.final_text == "Partial analysis."
    assert delegate.runs == 1
    assert Path("result.json").is_file() and Path("call.json").is_file()
    assert bool([e for e in events if isinstance(e, ErrorEvent)
                 and e.code == "ACCEPTANCE_FAILED"]) is overwrite


def test_preflight_rejects_a_dangling_identity_pointer_in_a_frozen_input(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    from xdsh.core.research_feasibility import unknown_feasibility

    (tmp_path / "plan.json").write_text('{"parameters":{"n":1},"ids":["a"]}')
    (tmp_path / "contract.json").write_text(json.dumps({
        "research_revision": 2, "feasibility": unknown_feasibility(),
        "unchanged": ["plan.json"], "json": [{
            "path": "result.json", "numbers": {"/value": {}}, "source_json": {
                "/parameters": {"path": "plan.json", "pointer": "/parameters"}},
            "artifact_json": {"/ids": {"path": "plan.json", "pointer": "/MISSING"}},
        }],
    }))
    assert main(["--workspace", str(tmp_path), "--contract", "contract.json", "--profile",
                 "research", "--preflight"]) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "source_reference_invalid"
