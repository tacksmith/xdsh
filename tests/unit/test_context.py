import math
from pathlib import Path

import pytest

from xdsh.core.context import ContextCompactionPolicy
from xdsh.core.events import CompactionAction, CompactionEvent, FinishEvent, UsageEvent
from xdsh.core.product_control import ProductSessionControl, TurnUsage
from xdsh.telegram.render import ProgressRenderer


def test_context_policy_triggers_at_fifty_percent_and_reports_target() -> None:
    policy = ContextCompactionPolicy()

    decision = policy.evaluate(
        latest_input_tokens=5_000,
        context_window_tokens=10_000,
        retained_turns=1,
        turns_since_compaction=1,
    )

    assert decision.should_compact is True
    assert decision.reason == "ready"
    assert decision.threshold_tokens == 5_000
    assert decision.target_tokens == 2_000
    assert decision.percent == 50.0


def test_absolute_context_threshold_can_lower_ratio_threshold() -> None:
    decision = ContextCompactionPolicy(threshold_tokens=4_000).evaluate(
        latest_input_tokens=4_000,
        context_window_tokens=10_000,
        retained_turns=1,
        turns_since_compaction=1,
    )

    assert decision.should_compact is True
    assert decision.threshold_tokens == 4_000
    assert decision.target_tokens == 2_000


def test_context_threshold_matches_dsh_floor_boundary() -> None:
    decision = ContextCompactionPolicy(threshold_ratio=0.70).evaluate(
        latest_input_tokens=4,
        context_window_tokens=7,
        retained_turns=1,
        turns_since_compaction=1,
    )

    assert decision.threshold_tokens == 4  # floor(7 * .70), not ceil(...)
    assert decision.should_compact is True


def test_context_policy_rejects_non_finite_ratios() -> None:
    with pytest.raises(ValueError, match="must be finite"):
        ContextCompactionPolicy(warning_ratio=math.nan)
    with pytest.raises(ValueError, match="must be finite"):
        ContextCompactionPolicy(threshold_ratio=math.inf)
    with pytest.raises(ValueError, match="must be finite"):
        ContextCompactionPolicy(target_ratio=-math.inf)


def test_context_pressure_includes_cached_prompt_tokens() -> None:
    renderer = ProgressRenderer()
    renderer.accept(
        UsageEvent(
            session_id="session",
            input_tokens=100,
            output_tokens=1,
            cache_read_tokens=6_900,
        )
    )

    assert renderer.last_input_tokens == 100
    assert renderer.last_context_tokens == 7_000


def test_progress_renderer_marks_context_warning_and_trigger() -> None:
    renderer = ProgressRenderer(
        context_warning_tokens=6_000,
        context_threshold_tokens=7_000,
    )
    renderer.accept(
        UsageEvent(session_id="session", input_tokens=7_000, output_tokens=1)
    )

    rendered = renderer.render(final=False, elapsed_seconds=1)

    assert rendered is not None
    assert "自動壓縮: 已達門檻" in rendered


@pytest.mark.parametrize(
    ("latest", "turns", "since", "reason"),
    [
        (1_999, 1, 1, "below_threshold"),
        (2_000, 1, 1, "warning"),
        (5_000, 0, 1, "insufficient_history"),
        (5_000, 1, 0, "cooldown"),
    ],
)
def test_context_policy_is_fail_closed_for_non_ready_states(
    latest: int,
    turns: int,
    since: int,
    reason: str,
) -> None:
    decision = ContextCompactionPolicy().evaluate(
        latest_input_tokens=latest,
        context_window_tokens=10_000,
        retained_turns=turns,
        turns_since_compaction=since,
    )

    assert decision.should_compact is False
    assert decision.reason == reason


def test_auto_compaction_persists_marker_and_is_cooldown_bounded(tmp_path: Path) -> None:
    control = ProductSessionControl(tmp_path / "state")
    policy = ContextCompactionPolicy(cooldown_turns=1)
    control.record_turn(
        "session",
        user_text="first",
        assistant_text="answer",
        model="test-model",
        usage=TurnUsage(latest_input_tokens=8_000),
        succeeded=True,
    )

    applied = control.compact_if_needed(
        "session",
        policy=policy,
        context_window_tokens=10_000,
    )
    snapshot = control.snapshot("session")
    assert applied.compaction is not None
    assert applied.compaction.automatic is True
    assert snapshot.last_compaction_turn == 1

    blocked = control.compact_if_needed(
        "session",
        policy=policy,
        context_window_tokens=10_000,
    )
    assert blocked.compaction is None
    assert blocked.decision.reason == "cooldown"


def test_failed_turn_still_counts_as_history_for_pressure_decision(tmp_path: Path) -> None:
    control = ProductSessionControl(tmp_path / "state")
    policy = ContextCompactionPolicy()
    control.record_turn(
        "failed-session",
        user_text="large task",
        assistant_text="partial answer",
        model="test-model",
        usage=TurnUsage(latest_input_tokens=8_000),
        succeeded=False,
    )

    decision = control.auto_compaction_decision(
        "failed-session",
        policy=policy,
        context_window_tokens=10_000,
    )
    assert decision.retained_turns == 1
    assert decision.should_compact is True


def test_progress_renderer_exposes_structured_native_compaction() -> None:
    renderer = ProgressRenderer(native_compaction_enabled=True)
    renderer.accept(
        CompactionEvent(
            session_id="session",
            action=CompactionAction.START,
            compaction_id="c1",
        )
    )
    renderer.accept(
        CompactionEvent(
            session_id="session",
            action=CompactionAction.SUMMARY,
            compaction_id="c1",
            shadowed_items=4,
            shadowed_tokens=1_200,
        )
    )
    renderer.accept(
        CompactionEvent(
            session_id="session",
            action=CompactionAction.END,
            compaction_id="c1",
        )
    )

    rendered = renderer.render(final=False, elapsed_seconds=2)
    assert rendered is not None
    assert "DSH 原生壓縮: 已完成" in rendered
    assert "摘要 4 項" in rendered


def test_progress_renderer_shows_pruner_token_measurement_when_chars_are_unavailable() -> None:
    renderer = ProgressRenderer(native_compaction_enabled=True)
    renderer.accept(
        CompactionEvent(
            session_id="session",
            action=CompactionAction.PRUNE,
            shadowed_tokens=4_096,
        )
    )

    rendered = renderer.render(final=False, elapsed_seconds=1)

    assert rendered is not None
    assert "工具輸出裁剪" in rendered
    assert "4.1k tokens" in rendered


def test_progress_renderer_marks_interrupted_native_compaction_as_incomplete() -> None:
    renderer = ProgressRenderer(native_compaction_enabled=True)
    renderer.accept(
        CompactionEvent(
            session_id="session",
            action=CompactionAction.START,
            compaction_id="c1",
        )
    )
    renderer.accept(
        FinishEvent(
            session_id="session",
            reason="aborted",
            cancelled=True,
        )
    )

    rendered = renderer.render(final=True, elapsed_seconds=3)

    assert rendered is not None
    assert "DSH 原生壓縮: 未完成" in rendered
    assert renderer.compaction_active is False
    assert renderer.compaction_incomplete is True


def test_progress_renderer_marks_structured_compaction_error() -> None:
    renderer = ProgressRenderer(native_compaction_enabled=True)
    renderer.accept(
        CompactionEvent(
            session_id="session",
            action=CompactionAction.END,
            compaction_id="c1",
            error=True,
        )
    )

    rendered = renderer.render(final=True, elapsed_seconds=3)

    assert rendered is not None
    assert "DSH 原生壓縮: 失敗" in rendered
    assert renderer.compaction_error_count == 1


def test_progress_renderer_marks_recovered_compaction_retry_as_completed() -> None:
    renderer = ProgressRenderer(native_compaction_enabled=True)
    renderer.accept(
        CompactionEvent(
            session_id="session",
            action=CompactionAction.START,
            compaction_id="failed",
        )
    )
    renderer.accept(
        CompactionEvent(
            session_id="session",
            action=CompactionAction.END,
            compaction_id="failed",
            error=True,
        )
    )
    renderer.accept(
        CompactionEvent(
            session_id="session",
            action=CompactionAction.START,
            compaction_id="ok",
        )
    )
    renderer.accept(
        CompactionEvent(
            session_id="session",
            action=CompactionAction.SUMMARY,
            compaction_id="ok",
            shadowed_items=2,
        )
    )
    renderer.accept(
        CompactionEvent(
            session_id="session",
            action=CompactionAction.END,
            compaction_id="ok",
        )
    )

    rendered = renderer.render(final=True, elapsed_seconds=3)

    assert rendered is not None
    assert "DSH 原生壓縮: 已完成·含重試" in rendered
    assert "重試\N{MULTIPLICATION SIGN}1" in rendered
