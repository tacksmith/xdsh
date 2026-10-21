"""Read-only XDSH health reconciler with conservative ambiguity handling.

The default command only observes XDSH-owned heartbeat and SQLite metadata.  It
never starts a job, retries a request, sends a message, or repairs a task.  An
operator may explicitly request the existing delivery ledger's safe
``sending/running -> unknown`` transition; no blind replay is ever performed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import signal
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from xdsh.core.delivery import DeliveryLedgerError, DurableDeliveryLedger
from xdsh.core.health_trends import (
    HealthTrendComponent,
    HealthTrendError,
    HealthTrendObservation,
    HealthTrendStore,
    RouteStatus,
    TrendWindowSummary,
)
from xdsh.core.heartbeat import HeartbeatError
from xdsh.core.jobs import DurableJobBoard, DurableJobError
from xdsh.core.models import (
    LiteLlmExecutionProbe,
    LiteLlmModelCatalog,
    ModelAvailability,
    ModelCatalogError,
    ModelDirectory,
    ModelRouteHealth,
)
from xdsh.core.product_control import safe_exception_text
from xdsh.core.snapshots import snapshot_payload

LOGGER = logging.getLogger(__name__)

HealthLevel = Literal["healthy", "degraded", "unknown", "not_observed"]
DEFAULT_RUNNING_WARNING_SECONDS = 900.0
DEFAULT_PENDING_INTERACTION_WARNING_SECONDS = 3_600.0


@dataclass(frozen=True, slots=True)
class ReconcilerConfig:
    """Bounded, local-only reconciler settings."""

    state_root: Path
    telegram_max_age_seconds: float = 90.0
    cli_max_age_seconds: float = 180.0
    pending_warning_seconds: float = 300.0
    running_warning_seconds: float = DEFAULT_RUNNING_WARNING_SECONDS
    pending_interaction_warning_seconds: float = DEFAULT_PENDING_INTERACTION_WARNING_SECONDS
    check_models: bool = False
    probe_models: bool = False
    model_base_url: str | None = None
    model_api_key_env: str = "XDSH_LITELLM_API_KEY"
    model_timeout_seconds: float = 5.0
    model_probe_timeout_seconds: float = 20.0
    model_probe_max_routes: int = 12
    reconcile_delivery: bool = False
    record_trends: bool = False
    trend_interval_seconds: float = 60.0
    trend_retention_days: int = 30
    telegram_state_root: Path | None = None
    cli_state_root: Path | None = None
    delivery_state_root: Path | None = None
    jobs_state_root: Path | None = None
    trend_state_root: Path | None = None

    def __post_init__(self) -> None:
        raw_root = self.state_root.expanduser()
        if raw_root.is_symlink():
            raise ValueError("reconciler state root is unsafe")
        root = raw_root.resolve()
        if (
            root in {Path(root.anchor), Path.home().resolve()}
            or ".xclaude" in root.parts
            or ".dsh" in root.parts
        ):
            raise ValueError("reconciler state root is unsafe")
        for name, value in (
            ("telegram_max_age_seconds", self.telegram_max_age_seconds),
            ("cli_max_age_seconds", self.cli_max_age_seconds),
            ("pending_warning_seconds", self.pending_warning_seconds),
            ("running_warning_seconds", self.running_warning_seconds),
            (
                "pending_interaction_warning_seconds",
                self.pending_interaction_warning_seconds,
            ),
            ("model_timeout_seconds", self.model_timeout_seconds),
            ("model_probe_timeout_seconds", self.model_probe_timeout_seconds),
            ("trend_interval_seconds", self.trend_interval_seconds),
        ):
            if value <= 0 or not math.isfinite(float(value)):
                raise ValueError(f"{name} must be positive")
        if not 7 <= self.trend_retention_days <= 90:
            raise ValueError("trend_retention_days must be between 7 and 90")
        if self.check_models and not self.model_base_url:
            raise ValueError("model_base_url is required when model checks are enabled")
        if self.probe_models and not self.check_models:
            raise ValueError("probe_models requires check_models")
        if not 1 <= self.model_probe_max_routes <= 32:
            raise ValueError("model_probe_max_routes must be between 1 and 32")
        if not self.model_api_key_env or any(
            character.isspace() for character in self.model_api_key_env
        ):
            raise ValueError("model_api_key_env is invalid")
        for name, optional_root in (
            ("telegram_state_root", self.telegram_state_root),
            ("cli_state_root", self.cli_state_root),
            ("delivery_state_root", self.delivery_state_root),
            ("jobs_state_root", self.jobs_state_root),
            ("trend_state_root", self.trend_state_root),
        ):
            if optional_root is None:
                continue
            raw_optional = optional_root.expanduser()
            if raw_optional.is_symlink():
                raise ValueError(f"{name} is unsafe")
            resolved_optional = raw_optional.resolve()
            if not resolved_optional.is_relative_to(root):
                raise ValueError(f"{name} must remain below state_root")


@dataclass(frozen=True, slots=True)
class ComponentHealth:
    """One credential/content-free component observation."""

    name: str
    status: HealthLevel
    reason: str
    age_seconds: float | None = None
    counts: tuple[tuple[str, int], ...] = ()
    routes: tuple[tuple[str, str], ...] = ()

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "name": self.name,
            "status": self.status,
            "reason": self.reason,
        }
        if self.age_seconds is not None:
            result["age_seconds"] = round(self.age_seconds, 3)
        if self.counts:
            result["counts"] = {key: value for key, value in self.counts}
        if self.routes:
            result["routes"] = {route: status for route, status in self.routes}
        return result


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    """A bounded health snapshot suitable for logs, CI, or a dashboard."""

    checked_at: float
    overall: HealthLevel
    components: tuple[ComponentHealth, ...]
    actions: tuple[str, ...] = ()
    trends: tuple[TrendWindowSummary, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return snapshot_payload(
            "health_report",
            "local",
            {
                "schema_version": 1,
                "checked_at": self.checked_at,
                "overall": self.overall,
                "components": [component.as_dict() for component in self.components],
                "actions": list(self.actions),
                "trends": [trend.as_dict() for trend in self.trends],
            },
            include_header=False,
            max_bytes=128_000,
        )


class ServiceReconciler:
    """Observe XDSH-owned state and make only explicit conservative transitions."""

    def __init__(
        self,
        config: ReconcilerConfig,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self._clock = clock

    async def run_once(self) -> ReconciliationReport:
        now = float(self._clock())
        if now < 0:
            raise ValueError("reconciler clock must be non-negative")
        components = [
            self._telegram_health(now),
            self._cli_health(now),
            self._delivery_health(now),
            self._jobs_health(now),
        ]
        if self.config.check_models:
            components.append(await self._model_health())
        actions: list[str] = []
        if self.config.reconcile_delivery:
            actions.extend(self._reconcile_delivery())
            # Re-read after an explicit transition so the report states the
            # resulting ambiguity rather than the pre-transition state.
            components[2] = self._delivery_health(now)
        preliminary = ReconciliationReport(
            checked_at=now,
            overall=_overall_status(tuple(components)),
            components=tuple(components),
            actions=tuple(actions),
        )
        trends: tuple[TrendWindowSummary, ...] = ()
        try:
            trend_store = HealthTrendStore(
                self._root(self.config.trend_state_root),
                retention_days=self.config.trend_retention_days,
            )
            observation = _trend_observation(preliminary)
            if self.config.record_trends:
                await asyncio.to_thread(
                    trend_store.record,
                    observation,
                    min_interval_seconds=self.config.trend_interval_seconds,
                )
            if self.config.record_trends or trend_store.path.is_file():
                trends = await asyncio.to_thread(trend_store.summaries, now=now)
        except (HealthTrendError, OSError, ValueError):
            actions.append("health_trends_unavailable")
        return ReconciliationReport(
            checked_at=preliminary.checked_at,
            overall=preliminary.overall,
            components=preliminary.components,
            actions=tuple(actions),
            trends=trends,
        )

    def _telegram_health(self, now: float) -> ComponentHealth:
        path = self._root(self.config.telegram_state_root, "telegram") / "heartbeat"
        if not path.exists():
            return ComponentHealth("telegram", "not_observed", "heartbeat_missing")
        if path.is_symlink():
            return ComponentHealth("telegram", "unknown", "heartbeat_unsafe")
        try:
            age = _heartbeat_age(path, now)
        except (OSError, ValueError):
            return ComponentHealth("telegram", "unknown", "heartbeat_invalid")
        if age < -5.0:
            return ComponentHealth("telegram", "unknown", "clock_ahead", age)
        if age > self.config.telegram_max_age_seconds:
            return ComponentHealth("telegram", "degraded", "heartbeat_stale", age)
        return ComponentHealth("telegram", "healthy", "heartbeat_fresh", age)

    def _cli_health(self, now: float) -> ComponentHealth:
        path = self._root(self.config.cli_state_root) / "heartbeats" / "cli.heartbeat"
        if not path.exists():
            return ComponentHealth("cli", "not_observed", "heartbeat_missing")
        try:
            age = _heartbeat_age(path, now)
        except (HeartbeatError, OSError, ValueError):
            return ComponentHealth("cli", "unknown", "heartbeat_invalid")
        if age < -5.0:
            return ComponentHealth("cli", "unknown", "clock_ahead", age)
        if age > self.config.cli_max_age_seconds:
            return ComponentHealth("cli", "degraded", "heartbeat_stale", age)
        return ComponentHealth("cli", "healthy", "heartbeat_fresh", age)

    def _delivery_health(self, now: float) -> ComponentHealth:
        ledger = DurableDeliveryLedger(self._root(self.config.delivery_state_root))
        try:
            summary = ledger.health_summary(now=now)
        except DeliveryLedgerError:
            return ComponentHealth("delivery", "unknown", "ledger_unreadable")
        counts = (
            ("pending", summary.pending_deliveries),
            ("sending", summary.sending_deliveries),
            ("unknown", summary.unknown_deliveries),
            ("running_turns", summary.running_turns),
            ("unknown_turns", summary.unknown_turns),
        )
        if summary.sending_deliveries or summary.unknown_deliveries or summary.running_turns:
            return ComponentHealth("delivery", "unknown", "ambiguous_state", counts=counts)
        if summary.unknown_turns:
            return ComponentHealth("delivery", "degraded", "turn_requires_review", counts=counts)
        if (
            summary.oldest_pending_age_seconds is not None
            and summary.oldest_pending_age_seconds > self.config.pending_warning_seconds
        ):
            return ComponentHealth(
                "delivery",
                "degraded",
                "pending_delivery_stale",
                counts=counts,
            )
        if any(value for _, value in counts):
            return ComponentHealth("delivery", "healthy", "pending_work", counts=counts)
        return ComponentHealth("delivery", "healthy", "idle", counts=counts)

    def _jobs_health(self, now: float) -> ComponentHealth:
        board = DurableJobBoard(self._root(self.config.jobs_state_root))
        try:
            summary = board.health_summary(now=now)
            interaction_integrity = board.verify_interaction_integrity()
        except DurableJobError:
            return ComponentHealth("jobs", "unknown", "board_unreadable")
        counts = tuple(
            (*summary.counts, ("pending_requests", summary.pending_interactions))
        )
        count_map = dict(counts)
        if interaction_integrity.status == "tampered":
            return ComponentHealth(
                "jobs",
                "unknown",
                "interaction_audit_tampered",
                counts=counts,
            )
        if interaction_integrity.status == "legacy_unverified":
            return ComponentHealth(
                "jobs",
                "degraded",
                "interaction_audit_unverified",
                counts=counts,
            )
        if summary.running_expired:
            return ComponentHealth("jobs", "unknown", "expired_lease", counts=counts)
        if count_map.get("unknown", 0) or count_map.get("manual_review", 0):
            return ComponentHealth("jobs", "degraded", "manual_review_required", counts=counts)
        if (
            summary.oldest_running_age_seconds is not None
            and summary.oldest_running_age_seconds > self.config.running_warning_seconds
        ):
            return ComponentHealth("jobs", "degraded", "running_job_stale", counts=counts)
        if (
            summary.oldest_pending_interaction_age_seconds is not None
            and summary.oldest_pending_interaction_age_seconds
            > self.config.pending_interaction_warning_seconds
        ):
            return ComponentHealth(
                "jobs",
                "degraded",
                "pending_interaction_stale",
                age_seconds=summary.oldest_pending_interaction_age_seconds,
                counts=counts,
            )
        return ComponentHealth("jobs", "healthy", "normal", counts=counts)

    async def _model_health(self) -> ComponentHealth:
        assert self.config.model_base_url is not None
        try:
            catalog = LiteLlmModelCatalog(
                self.config.model_base_url,
                api_key_env=self.config.model_api_key_env,
                timeout_seconds=self.config.model_timeout_seconds,
            )
            directory = await catalog.snapshot()
            if self.config.probe_models:
                directory = await self._probe_routes(directory)
        except (ModelCatalogError, ValueError):
            return ComponentHealth("model_route", "unknown", "catalog_unavailable")
        health_by_model = {item.model: item for item in directory.health}
        observed_health = bool(health_by_model)
        routes = tuple(
            (
                model,
                health_by_model[model].availability.value
                if model in health_by_model
                else "not_observed",
            )
            for model in directory.models[:128]
        )
        route_counts = {
            status: sum(route_status == status for _route, route_status in routes)
            for status in ("available", "unavailable", "unknown", "not_observed")
        }
        status: HealthLevel = "unknown"
        if directory.models:
            status = (
                "unknown"
                if observed_health and route_counts["unknown"]
                else "degraded"
                if observed_health and route_counts["unavailable"]
                else "healthy"
            )
        return ComponentHealth(
            "model_route",
            status,
            "catalog_available" if directory.models else "catalog_empty",
            counts=(
                ("models", len(directory.models)),
                ("providers", len(directory.providers)),
                *tuple((key, value) for key, value in route_counts.items() if value),
            ),
            routes=routes,
        )

    async def _probe_routes(
        self,
        directory: ModelDirectory,
    ) -> ModelDirectory:
        probe = LiteLlmExecutionProbe(
            self.config.model_base_url or "",
            api_key_env=self.config.model_api_key_env,
            timeout_seconds=self.config.model_probe_timeout_seconds,
        )
        selected = directory.models[: self.config.model_probe_max_routes]
        semaphore = asyncio.Semaphore(4)

        async def bounded(model: str) -> ModelRouteHealth:
            async with semaphore:
                try:
                    return await probe.probe(model)
                except Exception:
                    return ModelRouteHealth(
                        model=model,
                        availability=ModelAvailability.UNKNOWN,
                        detail="probe_failed",
                    )

        results = await asyncio.gather(*(bounded(model) for model in selected))
        return ModelDirectory(directory.providers, tuple(results))

    def _reconcile_delivery(self) -> tuple[str, ...]:
        ledger = DurableDeliveryLedger(self._root(self.config.delivery_state_root))
        try:
            result = ledger.reconcile()
        except DeliveryLedgerError:
            return ("delivery_reconcile_failed",)
        actions = []
        if result.unknown_deliveries:
            actions.append(f"delivery_unknown:{len(result.unknown_deliveries)}")
        if result.unknown_turns:
            actions.append(f"turn_unknown:{len(result.unknown_turns)}")
        if result.recovery_notices_created:
            actions.append(f"recovery_notices:{result.recovery_notices_created}")
        return tuple(actions) or ("delivery_no_change",)

    def _root(self, configured: Path | None, suffix: str = "") -> Path:
        if configured is not None:
            return configured.expanduser().resolve()
        return self.config.state_root.expanduser().resolve() / suffix


def _overall_status(components: tuple[ComponentHealth, ...]) -> HealthLevel:
    statuses = {component.status for component in components}
    if "unknown" in statuses:
        return "unknown"
    if "degraded" in statuses:
        return "degraded"
    if statuses and statuses == {"not_observed"}:
        return "not_observed"
    return "healthy"


def _heartbeat_age(path: Path, now: float) -> float:
    if path.is_symlink() or not path.is_file():
        raise HeartbeatError("heartbeat is missing or unsafe")
    try:
        timestamp = float(path.read_text(encoding="ascii").strip())
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise HeartbeatError("heartbeat is invalid") from error
    if not math.isfinite(timestamp) or timestamp < 0:
        raise HeartbeatError("heartbeat timestamp is invalid")
    return now - timestamp


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", type=Path, default=None)
    parser.add_argument("--json", action="store_true", help="emit one JSON report")
    parser.add_argument("--watch", type=float, default=None, help="repeat at this interval")
    parser.add_argument(
        "--reconcile-delivery",
        action="store_true",
        help="explicitly mark ambiguous sending/running delivery state unknown",
    )
    parser.add_argument("--check-models", action="store_true")
    parser.add_argument(
        "--probe-models",
        action="store_true",
        help="opt-in bounded execution probes for catalog routes",
    )
    parser.add_argument("--model-base-url", default=None)
    parser.add_argument("--model-probe-timeout", type=float, default=20.0)
    parser.add_argument("--model-probe-max-routes", type=int, default=12)
    parser.add_argument("--max-telegram-age", type=float, default=90.0)
    parser.add_argument("--max-cli-age", type=float, default=180.0)
    parser.add_argument("--strict", action="store_true", help="exit non-zero for degraded/unknown")
    parser.add_argument(
        "--record",
        action="store_true",
        help="explicitly persist content-free health trends",
    )
    parser.add_argument(
        "--trend-interval",
        type=float,
        default=60.0,
        help="minimum seconds between unchanged trend samples",
    )
    parser.add_argument("--trend-retention-days", type=int, default=30)
    return parser


async def _run(args: argparse.Namespace) -> int:
    state_root = args.state_root or Path(os.environ.get("XDSH_STATE_ROOT", "~/.xdsh"))
    model_url = args.model_base_url or os.environ.get("XDSH_LITELLM_BASE_URL")
    config = ReconcilerConfig(
        state_root=state_root,
        telegram_max_age_seconds=args.max_telegram_age,
        cli_max_age_seconds=args.max_cli_age,
        check_models=args.check_models or args.probe_models,
        probe_models=args.probe_models,
        model_base_url=model_url,
        model_probe_timeout_seconds=args.model_probe_timeout,
        model_probe_max_routes=args.model_probe_max_routes,
        reconcile_delivery=args.reconcile_delivery,
        record_trends=args.record,
        trend_interval_seconds=args.trend_interval,
        trend_retention_days=args.trend_retention_days,
    )
    reconciler = ServiceReconciler(config)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_number in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signal_number, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    interval = args.watch
    if interval is not None and interval < 5.0:
        raise ValueError("watch interval must be at least 5 seconds")
    while True:
        report = await reconciler.run_once()
        payload = json.dumps(report.as_dict(), ensure_ascii=False, sort_keys=True)
        print(payload if args.json else _human_report(report), flush=True)
        if interval is None or stop.is_set():
            break
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
    if args.strict and report.overall in {"degraded", "unknown"}:
        return 1
    return 0


def _human_report(report: ReconciliationReport) -> str:
    lines = [f"XDSH reconciler: {report.overall} ({report.checked_at:.3f})"]
    for component in report.components:
        age = f" age={component.age_seconds:.1f}s" if component.age_seconds is not None else ""
        lines.append(f"- {component.name}: {component.status} · {component.reason}{age}")
    if report.actions:
        lines.append("actions: " + ", ".join(report.actions))
    for trend in report.trends:
        lines.append(
            f"📈 {trend.days}d: samples={trend.samples} · "
            f"unknown={trend.unknown_ratio:.1%} · degraded={trend.degraded_ratio:.1%}"
        )
        for alert in trend.alerts[:8]:
            route = f" route={alert.route}" if alert.route else ""
            lines.append(
                f"⚠️ {alert.code} component={alert.component}{route} "
                f"\u00d7{alert.occurrences}"
            )
    return "\n".join(lines)


def _trend_observation(report: ReconciliationReport) -> HealthTrendObservation:
    return HealthTrendObservation(
        checked_at=report.checked_at,
        overall=report.overall,
        components=tuple(
            HealthTrendComponent(
                name=component.name,
                status=component.status,
                reason=component.reason,
                age_seconds=component.age_seconds,
                counts=component.counts,
                routes=tuple(
                    (route, cast(RouteStatus, status))
                    for route, status in component.routes
                ),
            )
            for component in report.components
        ),
    )


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    try:
        raise SystemExit(asyncio.run(_run(args)))
    except (OSError, ValueError) as error:
        raise SystemExit(safe_exception_text(error, max_chars=600)) from None


if __name__ == "__main__":
    main()
