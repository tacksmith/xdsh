from __future__ import annotations

from xdsh.core.queue_policy import analyze_busy_prompt


def test_busy_prompt_recommends_followup_merge_for_contextual_addition() -> None:
    result = analyze_busy_prompt(
        "整理部署日志并给出根因",
        "补充: 把刚才的根因整理成修复清单",
    )

    assert result.intent == "supplement"
    assert result.recommendation == "merge"
    assert result.confidence in {"medium", "high"}
    assert result.signals


def test_busy_prompt_recommends_interrupt_for_explicit_urgency() -> None:
    result = analyze_busy_prompt("扫描仓库", "紧急: 先检查服务是否存活")

    assert result.intent == "urgent"
    assert result.recommendation == "interrupt"
    assert "紧急" in result.signals


def test_busy_prompt_recommends_replace_for_explicit_correction() -> None:
    result = analyze_busy_prompt("生成完整报告", "取消刚才的任务, 改成只检查状态")

    assert result.intent == "replacement"
    assert result.recommendation == "replace"


def test_busy_prompt_defaults_to_fifo_for_ambiguous_input() -> None:
    result = analyze_busy_prompt("整理部署日志", "列出当前目录")

    assert result.intent == "independent"
    assert result.recommendation == "queue"


def test_busy_prompt_does_not_treat_firsthand_as_an_urgent_command() -> None:
    result = analyze_busy_prompt("scan the repository", "write a firsthand report")

    assert result.recommendation == "queue"
