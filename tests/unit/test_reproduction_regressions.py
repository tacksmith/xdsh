"""Minimized regressions from the Sep 27 three-system observation (no private data)."""
# ruff: noqa: RUF001
import pytest

from xdsh.core.completion_check import conflicting_completion_claim
from xdsh.core.events import (
    ActivityEvent,
    FinishEvent,
    TodoEvent,
    TodoItem,
    ToolResultEvent,
    ToolStartEvent,
    UsageEvent,
)
from xdsh.core.task_contract import acceptance_context, reproduction_context
from xdsh.telegram.render import ProgressRenderer, TurnRenderer


def test_two_bullet_reproduction_request_gets_acceptance_guidance() -> None:
    task = (
        "- 请严格复现2026年论文所有分析和结果，再做绘图。\n"
        "- 找出未复现项目，评估参数是否可靠合规。"
    )
    contract = acceptance_context(task)
    assert contract is not None
    assert "FULL original request" in contract
    assert "reproduction" in contract.lower()


def test_scientific_guidance_does_not_bloat_simple_software_bug_reproduction() -> None:
    assert reproduction_context("Reproduce this UI bug") is None
    assert reproduction_context("复现这个按钮点击问题") is None
    assert reproduction_context("Reproduce the paper's analysis") is not None


def test_successful_turn_is_not_verified_reproduction() -> None:
    renderer = TurnRenderer(max_answer_chars=10000, language="zh-CN",
                            original_request="严格复现论文全部分析")
    renderer.accept(FinishEvent(session_id="s", reason="completed", final_text=(
        "复现与审计已全部完成。\n\n未确认：Fig9 不可复现，Fig12 未执行。"
    )))
    renderer.apply_output_contract()
    assert "交付核验" in (renderer.terminal_notice() or "")
    # Host must not silently erase or reinterpret the model's original assertions.
    assert "Fig9 不可复现" in "".join(renderer.answer_chunks())


def test_fully_checked_todo_is_labelled_as_a_plan_not_verified_delivery() -> None:
    progress = ProgressRenderer(language="en")
    progress.accept(TodoEvent(session_id="s", items=(TodoItem(content="Draft", status="done"),)),
                    elapsed_seconds=1)
    progress.accept(FinishEvent(session_id="s", reason="completed"), elapsed_seconds=2)
    value = progress.render(elapsed_seconds=2, final=True)
    assert value is not None and "not delivery verification" in value


def test_completion_warning_does_not_break_json_only_contract() -> None:
    renderer = TurnRenderer(max_answer_chars=10000, language="en",
                            original_request="Return JSON only")
    renderer.accept(FinishEvent(session_id="s", reason="completed", final_text='{"ok":true}'))
    renderer.apply_output_contract()
    assert renderer.terminal_notice() is None


def test_json_claims_are_data_not_prose_delivery_assertions() -> None:
    renderer = TurnRenderer(max_answer_chars=10000, language="en",
                            original_request="Return JSON only")
    renderer.accept(FinishEvent(session_id="s", reason="completed", final_text=(
        '{"before":"All tasks completed", "after":"not executed"}'
    )))
    renderer.apply_output_contract()
    assert renderer.terminal_notice() is None


@pytest.mark.parametrize("answer", [
    "并非已全部完成；Fig9未复现。",
    "尚未全部完成，Fig9未复现。",
    "已全部完成，没有未完成项目。",
    "已全部完成。未复现项目：0项。",
    "旧记录称‘已全部完成’，实际Fig9未复现。",
    "```text\n已全部完成\n未复现\n```\n这是错误示例。",
    "> 已全部完成\n> 未复现\n这是错误示例。",
    "All tasks completed. No unverified results remain.",
])
def test_completion_diagnostic_avoids_negated_or_quoted_claims(answer: str) -> None:
    assert not conflicting_completion_claim(answer)


def test_long_plan_gap_reports_activity_since_snapshot_not_all_previous_work() -> None:
    progress = ProgressRenderer(language="en")
    for call, when in (("before", 1), ("after", 1100)):
        progress.accept(ToolStartEvent(session_id="s", call_id=call, name="read"),
                        elapsed_seconds=when)
        progress.accept(ToolResultEvent(session_id="s", call_id=call),
                        elapsed_seconds=when + 1)
        progress.accept(UsageEvent(session_id="s", input_tokens=100, output_tokens=10),
                        elapsed_seconds=when + 2)
        if call == "before":
            progress.accept(TodoEvent(session_id="s", items=(
                TodoItem(content="Analysis", status="in_progress"),
                TodoItem(content="Report", status="pending"),
            )), elapsed_seconds=10)

    # A duplicate result must not count twice or advance task completion.
    progress.accept(ToolResultEvent(session_id="s", call_id="after"), elapsed_seconds=1103)
    rendered = progress.render(final=False, elapsed_seconds=1110) or ""
    assert "Since plan update" in rendered
    assert "1 tool results" in rendered and "1 model rounds" in rendered
    assert "2 tool results" not in rendered
    assert "last event 7s ago" in rendered
    assert "Plan snapshot 0/2" in rendered
    assert "0%" not in rendered and "ETA" not in rendered

    # Identical native Todo is a new confirmation; a wall-clock tick is not.
    progress.accept(TodoEvent(session_id="s", items=progress.todo_items), elapsed_seconds=1111)
    assert "Since plan update" not in (progress.render(final=False, elapsed_seconds=1112) or "")
    progress.accept(ActivityEvent(session_id="s"),
                    elapsed_seconds=2400)
    later = progress.render(final=False, elapsed_seconds=2401) or ""
    assert "Since plan update" in later
    assert "0 tool results" in later and "0 model rounds" in later
    assert "last event 1s ago" in later  # Streaming is live, not a completed step.
    assert "Plan snapshot 0/2" in later
    assert "Since plan update" not in (progress.render(final=True, elapsed_seconds=2402) or "")


@pytest.mark.parametrize("language,marker", [
    ("zh-CN", "自计划更新后"), ("zh-TW", "自計畫更新後"),
    ("en", "Since plan update"), ("ja", "計画更新後"), ("ko", "계획 갱신 후"),
    ("es", "Desde la actualización del plan"), ("fr", "Depuis la mise à jour du plan"),
    ("de", "Seit Planaktualisierung"), ("pt-BR", "Desde a atualização do plano"),
    ("ru", "После обновления плана"), ("ar", "منذ تحديث الخطة"),
    ("hi", "योजना अपडेट के बाद"), ("id", "Sejak pembaruan rencana"),
])
def test_stale_plan_activity_uses_selected_language(language: str, marker: str) -> None:
    progress = ProgressRenderer(language=language)
    progress.accept(TodoEvent(session_id="s", items=(
        TodoItem(content="Analysis", status="in_progress"),
    )), elapsed_seconds=0)
    progress.accept(ActivityEvent(session_id="s"),
                    elapsed_seconds=90)
    rendered = progress.render(final=False, elapsed_seconds=91) or ""
    assert marker in rendered
    if language != "en":
        assert "Since plan update" not in rendered
