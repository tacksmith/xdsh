from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from xdsh.core.health_trends import (
    HealthTrendComponent,
    HealthTrendError,
    HealthTrendObservation,
    HealthTrendStore,
)


def _observation(
    checked_at: float,
    *,
    overall: str = "healthy",
    telegram_status: str = "healthy",
    telegram_reason: str = "heartbeat_fresh",
    route_status: str = "available",
) -> HealthTrendObservation:
    return HealthTrendObservation(
        checked_at=checked_at,
        overall=overall,  # type: ignore[arg-type]
        components=(
            HealthTrendComponent(
                name="telegram",
                status=telegram_status,  # type: ignore[arg-type]
                reason=telegram_reason,
            ),
            HealthTrendComponent(
                name="model_route",
                status="healthy",
                reason="catalog_available",
                routes=(("ark-code-latest", route_status),),  # type: ignore[arg-type]
            ),
        ),
    )


def test_trends_are_bounded_and_persist_transition_alerts(tmp_path: Path) -> None:
    store = HealthTrendStore(tmp_path / "state")

    assert store.record(_observation(100.0), min_interval_seconds=60.0)
    # An unchanged high-frequency watch sample is coalesced.
    assert not store.record(_observation(101.0), min_interval_seconds=60.0)
    # A changed health state is retained immediately, even inside the interval.
    assert store.record(
        _observation(
            102.0,
            overall="degraded",
            telegram_status="degraded",
            telegram_reason="heartbeat_stale",
            route_status="unavailable",
        ),
        min_interval_seconds=60.0,
    )

    window = store.summary(days=7, now=102.0)
    assert window.samples == 2
    assert window.degraded_samples == 1
    telegram = next(item for item in window.components if item.name == "telegram")
    assert telegram.stale_transitions == 1
    route = next(item for item in window.routes if item.route == "ark-code-latest")
    assert route.unavailable_transitions == 1
    assert {item.code for item in window.alerts} == {
        "heartbeat_stale",
        "route_unavailable",
    }
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.root.stat().st_mode & 0o777 == 0o700


def test_trend_retention_removes_old_samples_and_keeps_windows(tmp_path: Path) -> None:
    store = HealthTrendStore(tmp_path / "state", retention_days=7)
    assert store.record(_observation(0.0), force=True)
    assert store.record(_observation(8 * 86_400.0), force=True)
    window = store.summary(days=7, now=8 * 86_400.0)
    assert window.samples == 1
    assert window.last_checked_at == 8 * 86_400.0


def test_summary_is_read_only_and_does_not_create_state(tmp_path: Path) -> None:
    store = HealthTrendStore(tmp_path / "state")
    assert store.summary(days=7, now=10.0).samples == 0
    assert not store.path.exists()

    store.record(_observation(10.0))
    before = store.path.read_bytes()
    assert store.summary(days=7, now=10.0).samples == 1
    assert store.path.read_bytes() == before

    connection = sqlite3.connect(store.path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM observations").fetchone() == (1,)
    finally:
        connection.close()


def test_trend_rejects_backward_clock_and_unsafe_paths(tmp_path: Path) -> None:
    store = HealthTrendStore(tmp_path / "state")
    store.record(_observation(1_000.0))
    with pytest.raises(HealthTrendError, match="backwards"):
        store.record(_observation(500.0), force=True)

    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(HealthTrendError):
        HealthTrendStore(link)


def test_trend_payload_never_contains_task_text(tmp_path: Path) -> None:
    store = HealthTrendStore(tmp_path / "state")
    observation = _observation(10.0)
    store.record(observation)
    payload = str(store.summary(days=30, now=10.0).as_dict())
    assert "prompt" not in payload
    assert "answer" not in payload
