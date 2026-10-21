from __future__ import annotations

from pathlib import Path

import pytest

from xdsh.core.metrics import RunMetric, RunMetricsStore


def test_metrics_store_reports_success_latency_routes_guards_and_cold_tail(
    tmp_path: Path,
) -> None:
    store = RunMetricsStore(tmp_path / "metrics.sqlite3")
    store.record(
        RunMetric(
            finished_at=1_000.0,
            model="model-a",
            succeeded=True,
            finish_reason="completed",
            duration_ms=1_000,
            first_event_ms=100,
            first_answer_ms=180,
            cold_start_ms=80,
            cold_start_retries=0,
            tool_calls=2,
            total_tokens=120,
            selected_skills=("skill-a", "skill-b"),
        )
    )
    store.record(
        RunMetric(
            finished_at=1_010.0,
            model="model-a",
            succeeded=False,
            finish_reason="guarded",
            duration_ms=9_000,
            first_event_ms=800,
            first_answer_ms=1_200,
            cold_start_ms=700,
            cold_start_retries=1,
            tool_calls=8,
            total_tokens=2_000,
            guard_reason="tool_error_budget",
            error_code="",
            selected_skills=("skill-a",),
        )
    )
    store.record(
        RunMetric(
            finished_at=1_020.0,
            model="model-b",
            succeeded=True,
            finish_reason="completed",
            duration_ms=5_000,
            first_event_ms=500,
            first_answer_ms=None,
            cold_start_ms=None,
            cold_start_retries=0,
            tool_calls=0,
            total_tokens=50,
            selected_skills=("skill-b",),
        )
    )

    summary = store.summary(since=900.0)

    assert summary.total == 3
    assert summary.succeeded == 2
    assert summary.success_rate == 2 / 3
    assert summary.duration_p50_ms == 5_000
    assert summary.duration_p95_ms == 9_000
    assert summary.cold_start_p95_ms == 700
    assert summary.first_answer_p95_ms == 1_200
    assert summary.cold_start_retries == 1
    assert summary.guard_reasons == (("tool_error_budget", 1),)
    assert summary.model_routes == (("model-a", 1, 2), ("model-b", 1, 1))
    assert tuple(
        (item.skill, item.runs, item.succeeded, item.success_rate, item.last_used_at)
        for item in summary.skill_outcomes
    ) == (
        ("skill-a", 2, 1, 0.5, 1_010.0),
        ("skill-b", 2, 2, 1.0, 1_020.0),
    )
    assert (tmp_path / "metrics.sqlite3").stat().st_mode & 0o777 == 0o600


def test_metrics_rejects_invalid_or_duplicate_skill_names() -> None:
    with pytest.raises(ValueError, match="metric Skills"):
        RunMetric(
            finished_at=1.0,
            model="model",
            succeeded=True,
            finish_reason="completed",
            duration_ms=1,
            first_event_ms=None,
            cold_start_ms=None,
            cold_start_retries=0,
            tool_calls=0,
            total_tokens=0,
            selected_skills=("same", "same"),
        )
