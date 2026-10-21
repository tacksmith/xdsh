"""Research requests must cross the same final acceptance seam as explicit checks."""
# ruff: noqa: RUF001
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from xdsh.core.events import ErrorEvent, FinishEvent, RunnerEventT, ToolResultEvent
from xdsh.core.product_control import ProductSessionControl
from xdsh.core.research_feasibility import unknown_feasibility
from xdsh.core.run_guard import GuardedRunner


class ResearchRunner:
    def __init__(self) -> None:
        self.calls = 0

    async def run(self, task: str, *, session_id: str, cwd: Path,
                  **options: object) -> AsyncIterator[RunnerEventT]:
        self.calls += 1
        yield FinishEvent(session_id=session_id, reason="completed",
                          final_text="Reproduction complete; all results are correct.")

    async def cancel(self, session_id: str) -> bool:
        return False

    async def reset(self, session_id: str) -> None:
        pass

    async def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_ordinary_reproduction_cannot_skip_artifact_acceptance(tmp_path: Path) -> None:
    prepared = ProductSessionControl(tmp_path / "state").prepare_task(
        "test", "请实际复现这篇论文的分析并保存结果。", workspace=tmp_path,
    )
    delegate = ResearchRunner()
    events = [e async for e in GuardedRunner(delegate).run(
        prepared.task, session_id="test", cwd=tmp_path,
    )]
    assert isinstance(events[-1], FinishEvent) and events[-1].reason == "error"
    assert events[-1].final_text == "Reproduction complete; all results are correct."
    assert any(isinstance(e, ErrorEvent) and e.code == "ACCEPTANCE_FAILED" for e in events)
    assert delegate.calls == 1  # No whole task replay to manufacture a pass.


@pytest.mark.parametrize("user_task", [
    "你好", "解释如何复现这篇论文", "请复现论文，但不要运行，只提供方案",
    "Explain how to reproduce the paper", "Please reproduce this UI bug",
    "请复现这个按钮问题", "Do not reproduce the paper; summarize it",
])
def test_nonexecuting_and_unrelated_requests_keep_existing_behavior(
    tmp_path: Path, user_task: str,
) -> None:
    prepared = ProductSessionControl(tmp_path / "state").prepare_task(
        "s", user_task, workspace=tmp_path,
    )
    assert not prepared.task.startswith("XDSH-VERIFY: ")


def test_plan_mode_and_explicit_contract_take_precedence(tmp_path: Path) -> None:
    control = ProductSessionControl(tmp_path / "state")
    control.set_plan_mode("s", True)
    task = "Please reproduce the paper analysis."
    assert not control.prepare_task("s", task, workspace=tmp_path).task.startswith("XDSH-VERIFY: ")
    header = 'XDSH-VERIFY: {"unchanged":[],"json":[{"path":"result.json"}]}'
    control.set_plan_mode("s", False)
    prepared = control.prepare_task("s", header + "\n" + task, workspace=tmp_path)
    assert prepared.task.splitlines()[0] == header


def test_each_research_turn_uses_a_fresh_declared_path(tmp_path: Path) -> None:
    control = ProductSessionControl(tmp_path / "state")
    paths = []
    for _ in range(2):
        task = control.prepare_task("s", "Please reproduce the paper analysis.",
                                    workspace=tmp_path).task
        paths.append(json.loads(task.splitlines()[0].removeprefix("XDSH-VERIFY: "))
                     ["deferred"]["path"])
    assert paths[0] != paths[1]


@pytest.mark.parametrize("user_task", [
    "Please reproduce the paper analysis. Explain any genuine blocker.",
    "请实际复现这篇论文，解释最终偏差并讨论局限。",
    "請實際復現這篇論文，說明是否仍有未完成項。",
])
def test_reporting_instructions_do_not_disable_an_execution_request(
    tmp_path: Path, user_task: str,
) -> None:
    task = ProductSessionControl(tmp_path / "state").prepare_task(
        "s", user_task, workspace=tmp_path,
    )
    assert task.task.startswith("XDSH-VERIFY: ")


def test_minimal_cli_example_exposes_numeric_schema_without_source_inspection(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from xdsh.ops.verify import main

    assert main(["--research-example"]) == 0
    example = json.loads(capsys.readouterr().out)
    assert example["json"][0]["numbers"] == {"/estimate": {}}
    method = example["json"][0]["source_json"]["/effective_parameters"]
    assert method["path"] in example["unchanged"]
    assert "calls" not in example  # Plain metadata must not masquerade as a call receipt.
    assert "arrays" not in example  # Small JSON tasks must not invent array work/dependencies.


def test_preflight_rejects_missing_method_reference_before_outputs_exist(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    from xdsh.ops.verify import main

    (tmp_path / "method.json").write_text('{"pipeline":[{"parameters":{"scale":2}}]}')
    (tmp_path / "check.json").write_text(json.dumps({
        "unchanged": ["method.json"], "json": [{
            "path": "result.json", "source_json": {"/parameters": {
                "path": "method.json", "pointer": "/pipeline/parameters",
            }}, "numbers": {"/estimate": {}},
        }],
    }))
    result = main(["--workspace", str(tmp_path), "--contract", "check.json",
                   "--profile", "research", "--preflight"])
    response = json.loads(capsys.readouterr().out)
    assert result == 2
    assert response["reason"] == "source_reference_invalid"
    assert response["checks_passed"] is False
    assert not (tmp_path / "result.json").exists()


class EvidenceRunner(ResearchRunner):
    def __init__(self, *, mutate_plan: bool = False, mutate_checks: bool = False,
                 threshold: object = 0.3) -> None:
        super().__init__()
        self.mutate_plan = mutate_plan
        self.mutate_checks = mutate_checks
        self.threshold = threshold

    async def run(self, task: str, *, session_id: str, cwd: Path,
                  **options: object) -> AsyncIterator[RunnerEventT]:
        self.calls += 1
        declared = json.loads(task.splitlines()[0].removeprefix("XDSH-VERIFY: "))
        path = cwd / declared["deferred"]["path"]
        (cwd / "method.json").write_text(json.dumps({"parameters": {"center": False}, "range": {
            "kind": "integrity", "bounds": {"minimum": 0, "maximum": 1},
            "citation": "fixture protocol", "locator": "Unit interval probability",
        }}))
        path.write_text(json.dumps({"research_revision": 2, "feasibility": unknown_feasibility(),
                                   "unchanged": ["method.json"], "json": [{
            "path": "result.json", "source_json": {"/parameters": {
                "path": "method.json", "pointer": "/parameters"}},
            "expectations": {"/threshold": {"kind": "integrity",
                "bounds": {"minimum": 0, "maximum": 1},
                "reference": {"path": "method.json", "pointer": "/range"}}},
        }]}))
        yield ToolResultEvent(session_id=session_id, call_id="declare", name="write")
        # In production these are separate native tools; the host captures the
        # first declaration between them, rather than trusting the final prose.
        if self.mutate_plan:
            (cwd / "method.json").write_text('{"parameters":{"center":true}}')
        if self.mutate_checks:
            path.write_text('{"unchanged": [], "json": []}')
        (cwd / "result.json").write_text(json.dumps({
            "parameters": {"center": self.mutate_plan}, "threshold": self.threshold,
        }))
        yield ToolResultEvent(session_id=session_id, call_id="compute", name="bash")
        yield FinishEvent(session_id=session_id, reason="completed", final_text="Saved analysis.")


class ContractBeforeSourcesRunner(ResearchRunner):
    def __init__(self, *, rewrite_plan: bool = False) -> None:
        super().__init__()
        self.rewrite_plan = rewrite_plan

    async def run(self, task: str, *, session_id: str, cwd: Path,
                  **options: object) -> AsyncIterator[RunnerEventT]:
        declared = json.loads(task.splitlines()[0].removeprefix("XDSH-VERIFY: "))
        (cwd / declared["deferred"]["path"]).write_text(json.dumps({
            "research_revision": 2, "feasibility": unknown_feasibility(),
            "unchanged": ["method.json"], "json": [{
                "path": "result.json", "source_json": {"/parameters": {
                    "path": "method.json", "pointer": "/parameters"}},
                "numbers": {"/estimate": {}},
            }],
        }))
        yield ToolResultEvent(session_id=session_id, call_id="checklist", name="write")
        (cwd / "method.json").write_text('{"parameters":{"center":true}}')
        yield ToolResultEvent(session_id=session_id, call_id="source-plan", name="write")
        if self.rewrite_plan:
            plan = json.loads((cwd / "method.json").read_text())
            (cwd / "method.json").write_text(json.dumps(plan, indent=2))
        (cwd / "result.json").write_text('{"parameters":{"center":true},"estimate":0.5}')
        yield ToolResultEvent(session_id=session_id, call_id="compute", name="bash")
        yield FinishEvent(session_id=session_id, reason="completed", final_text="Verified values.")


@pytest.mark.asyncio
async def test_deferred_declaration_waits_for_its_frozen_sources_to_exist(tmp_path: Path) -> None:
    prepared = ProductSessionControl(tmp_path / "state").prepare_task(
        "test", "Please reproduce the paper analysis.", workspace=tmp_path,
    )
    events = [e async for e in GuardedRunner(ContractBeforeSourcesRunner()).run(
        prepared.task, session_id="test", cwd=tmp_path,
    )]
    assert isinstance(events[-1], FinishEvent) and events[-1].reason == "completed"
    assert not any(isinstance(e, ErrorEvent) for e in events)


@pytest.mark.asyncio
async def test_semantic_only_reformat_cannot_hide_frozen_plan_changes(tmp_path: Path) -> None:
    prepared = ProductSessionControl(tmp_path / "state").prepare_task(
        "test", "Please reproduce the paper analysis.", workspace=tmp_path,
    )
    assert "never rewrite, reformat or reserialize" in prepared.task
    events = [e async for e in GuardedRunner(ContractBeforeSourcesRunner(rewrite_plan=True)).run(
        prepared.task, session_id="test", cwd=tmp_path,
    )]
    assert any(isinstance(e, ErrorEvent) and "source_changed:method.json" in e.message
               for e in events)
    assert isinstance(events[-1], FinishEvent) and events[-1].reason == "error"
    assert events[-1].final_text == "Verified values."


@pytest.mark.asyncio
@pytest.mark.parametrize(("plan", "checks", "threshold", "reason"), [
    (False, False, 0.3, "completed"), (True, False, 0.3, "error"),
    (False, True, 0.3, "error"), (False, False, None, "error"),
])
async def test_actual_product_and_runner_flow_freezes_checks_and_retains_answer(
    tmp_path: Path, plan: bool, checks: bool, threshold: object, reason: str,
) -> None:
    prepared = ProductSessionControl(tmp_path / "state").prepare_task(
        "test", "Please reproduce the paper analysis.", workspace=tmp_path,
    )
    delegate = EvidenceRunner(mutate_plan=plan, mutate_checks=checks, threshold=threshold)
    result = [e async for e in GuardedRunner(delegate, research_repair_attempts=0).run(
        prepared.task, session_id="test", cwd=tmp_path,
    )]
    assert isinstance(result[-1], FinishEvent) and result[-1].reason == reason
    assert result[-1].final_text == "Saved analysis." and delegate.calls == 1
    if reason == "error":
        assert len([e for e in result if isinstance(e, ErrorEvent)]) == 1


@pytest.mark.parametrize("body", [
    '{"unchanged":[],"json":[]}',
    '{"unchanged":[],"json":[{"path":"result.json","equals":{"/ok":true}}]}',
    '{"unchanged":[],"json":[],"deferred":{"path":"again.json","profile":"research"}}',
    '{"unchanged":["../outside.json"],"json":[]}',
    '{"unchanged":[],"json":[],"private":"NEVER_ECHO_THIS"}',
])
def test_research_declarations_cannot_be_empty_recursive_or_unsafe(
    tmp_path: Path, body: str,
) -> None:
    from xdsh.core.artifact_acceptance import capture_acceptance

    (tmp_path / "check.json").write_text(body)
    snapshot = capture_acceptance('XDSH-VERIFY: {"unchanged":[],"json":[],"deferred":'
                                 '{"path":"check.json","profile":"research"}}', tmp_path)
    assert snapshot is not None
    issues = snapshot.verify()
    assert issues == ("research_contract_unavailable:check.json",)
    assert "NEVER_ECHO_THIS" not in str(issues)


def test_failed_delivery_explains_check_category_without_leaking_file_body() -> None:
    from xdsh.telegram.render import TurnRenderer

    renderer = TurnRenderer(max_answer_chars=10000, language="zh-CN")
    renderer.accept(ErrorEvent(session_id="s", code="ACCEPTANCE_FAILED",
                               message="DO_NOT_ECHO_PRIVATE_FILE", verification_issues=("arrays",)))
    renderer.accept(FinishEvent(session_id="s", reason="error", final_text="保留的分析结果。"))
    notice = renderer.terminal_notice() or ""
    assert "数组" in notice and "继续" in notice
    assert "DO_NOT_ECHO_PRIVATE_FILE" not in notice
    assert "保留的分析结果" in "".join(renderer.answer_chunks())


def test_failed_artifact_check_is_not_presented_as_a_runtime_crash() -> None:
    from xdsh.telegram.render import ProgressRenderer

    renderer = ProgressRenderer(language="zh-CN")
    renderer.accept(ErrorEvent(session_id="s", code="ACCEPTANCE_FAILED", message="private"))
    renderer.accept(FinishEvent(session_id="s", reason="error"))
    card = renderer.render(final=True, elapsed_seconds=3) or ""
    assert "验收未通过" in card and "执行错误" not in card and "private" not in card


@pytest.mark.parametrize("continuation", [
    "继续", "繼續", "continue", "続けて", "계속", "continuar", "continuer", "weiter",
    "продолжить", "متابعة", "जारी रखें", "lanjut",
])
def test_failed_checks_survive_restart_and_guide_continue_without_replaying(
    tmp_path: Path, continuation: str,
) -> None:
    from xdsh.core.product_control import TurnUsage

    control = ProductSessionControl(tmp_path / "state")
    control.record_turn("s", user_text="请实际复现论文的分析。", assistant_text="Saved results.",
                        model="test", usage=TurnUsage(), succeeded=False,
                        verification_issues=("parameters", "arrays"))
    restarted = ProductSessionControl(tmp_path / "state")
    task = restarted.prepare_task("s", continuation, workspace=tmp_path)
    assert "Host verification did not pass: parameters, arrays" in task.task
    assert "Inspect existing artifacts first" in task.task
    assert task.resume_context is not None  # Even a failed first turn has a recovery target.
    assert "parameters, arrays" in task.resume_context
    unrelated = restarted.prepare_task("s", "你好", workspace=tmp_path)
    assert "Host verification did not pass" not in unrelated.task
    assert unrelated.resume_context is not None
    assert "A different current request takes precedence" in unrelated.resume_context
    restarted.record_turn("s", user_text=continuation, assistant_text="Fixed.", model="test",
                          usage=TurnUsage(), succeeded=True)
    assert "Host verification did not pass" not in restarted.resolve_task("s", "继续")
