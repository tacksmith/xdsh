"""Content-free, bounded health trend and alert persistence.

The trend store is deliberately separate from task/result ledgers.  It keeps
only aggregate component state, route availability, and transition alerts;
prompts, answers, session IDs, tool arguments, and credentials never enter the
schema.  Observation is read-only.  A caller must explicitly invoke
``record`` when it wants to persist a snapshot.
"""

from __future__ import annotations

import math
import os
import re
import sqlite3
from collections import defaultdict
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

TrendStatus = Literal["healthy", "degraded", "unknown", "not_observed"]
RouteStatus = Literal["available", "unavailable", "unknown", "not_observed"]

_TREND_STATUSES = frozenset({"healthy", "degraded", "unknown", "not_observed"})
_ROUTE_STATUSES = frozenset({"available", "unavailable", "unknown", "not_observed"})
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_COUNT_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ROUTE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+@-]{0,199}$")
_MAX_COMPONENTS = 32
_MAX_COUNTS = 32
_MAX_ROUTES = 128
_MAX_REASON = 96
_RETENTION_SECONDS = 31 * 86_400
_STALE_REASONS = frozenset(
    {"heartbeat_stale", "heartbeat_missing", "heartbeat_invalid", "clock_ahead"}
)

ObservationRow = tuple[int, float, str]
ComponentRow = tuple[int, str, str, str]
RouteRow = tuple[int, str, str]
AlertRow = tuple[str, str, str | None, int, float]


class HealthTrendError(RuntimeError):
    """A trend snapshot is invalid, unsafe, or cannot be read/written."""


@dataclass(frozen=True, slots=True, kw_only=True)
class HealthTrendComponent:
    """One content-free component observation."""

    name: str
    status: TrendStatus
    reason: str
    age_seconds: float | None = None
    counts: tuple[tuple[str, int], ...] = ()
    routes: tuple[tuple[str, RouteStatus], ...] = ()

    def __post_init__(self) -> None:
        if _NAME.fullmatch(self.name) is None:
            raise HealthTrendError("health component name is invalid")
        if self.status not in _TREND_STATUSES:
            raise HealthTrendError("health component status is invalid")
        if not self.reason or len(self.reason) > _MAX_REASON or "\n" in self.reason:
            raise HealthTrendError("health component reason is invalid")
        if self.age_seconds is not None and (
            not math.isfinite(float(self.age_seconds))
            or self.age_seconds < -86_400
        ):
            raise HealthTrendError("health component age is invalid")
        if len(self.counts) > _MAX_COUNTS:
            raise HealthTrendError("health component has too many counters")
        count_keys = [key for key, _value in self.counts]
        if len(count_keys) != len(set(count_keys)) or any(
            _COUNT_KEY.fullmatch(key) is None
            or not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
            or value > 2_000_000_000
            for key, value in self.counts
        ):
            raise HealthTrendError("health component counters are invalid")
        if len(self.routes) > _MAX_ROUTES:
            raise HealthTrendError("health component has too many routes")
        route_names = [route for route, _status in self.routes]
        if len(route_names) != len(set(route_names)) or any(
            _ROUTE.fullmatch(route) is None or status not in _ROUTE_STATUSES
            for route, status in self.routes
        ):
            raise HealthTrendError("health component routes are invalid")


@dataclass(frozen=True, slots=True, kw_only=True)
class HealthTrendObservation:
    """A complete aggregate snapshot ready for explicit persistence."""

    checked_at: float
    overall: TrendStatus
    components: tuple[HealthTrendComponent, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.checked_at, int | float)
            or isinstance(self.checked_at, bool)
            or not math.isfinite(float(self.checked_at))
            or self.checked_at < 0
        ):
            raise HealthTrendError("health observation timestamp is invalid")
        if self.overall not in _TREND_STATUSES:
            raise HealthTrendError("health observation status is invalid")
        if not self.components or len(self.components) > _MAX_COMPONENTS:
            raise HealthTrendError("health observation component count is invalid")
        names = [component.name for component in self.components]
        if len(names) != len(set(names)):
            raise HealthTrendError("health observation components must be unique")


@dataclass(frozen=True, slots=True, kw_only=True)
class TrendAlertSummary:
    """Persisted transition alert count for one window."""

    code: str
    component: str
    occurrences: int
    last_seen_at: float
    route: str | None = None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "code": self.code,
            "component": self.component,
            "occurrences": self.occurrences,
            "last_seen_at": round(self.last_seen_at, 3),
        }
        if self.route is not None:
            result["route"] = self.route
        return result


@dataclass(frozen=True, slots=True, kw_only=True)
class ComponentTrendSummary:
    """Aggregate status history for one component."""

    name: str
    samples: int = 0
    healthy: int = 0
    degraded: int = 0
    unknown: int = 0
    not_observed: int = 0
    stale_transitions: int = 0
    unknown_transitions: int = 0
    last_status: TrendStatus | None = None
    last_reason: str | None = None
    last_seen_at: float | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "samples": self.samples,
            "status_counts": {
                "healthy": self.healthy,
                "degraded": self.degraded,
                "unknown": self.unknown,
                "not_observed": self.not_observed,
            },
            "stale_transitions": self.stale_transitions,
            "unknown_transitions": self.unknown_transitions,
            "last_status": self.last_status,
            "last_reason": self.last_reason,
            "last_seen_at": (
                round(self.last_seen_at, 3) if self.last_seen_at is not None else None
            ),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class RouteTrendSummary:
    """Aggregate availability history for one model/provider route."""

    route: str
    samples: int = 0
    available: int = 0
    unavailable: int = 0
    unknown: int = 0
    not_observed: int = 0
    unavailable_transitions: int = 0
    unknown_transitions: int = 0
    last_status: RouteStatus | None = None
    last_seen_at: float | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "route": self.route,
            "samples": self.samples,
            "status_counts": {
                "available": self.available,
                "unavailable": self.unavailable,
                "unknown": self.unknown,
                "not_observed": self.not_observed,
            },
            "unavailable_transitions": self.unavailable_transitions,
            "unknown_transitions": self.unknown_transitions,
            "last_status": self.last_status,
            "last_seen_at": (
                round(self.last_seen_at, 3) if self.last_seen_at is not None else None
            ),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class TrendWindowSummary:
    """Bounded aggregate over one retention window."""

    days: int
    samples: int = 0
    healthy_samples: int = 0
    degraded_samples: int = 0
    unknown_samples: int = 0
    not_observed_samples: int = 0
    unknown_ratio: float = 0.0
    degraded_ratio: float = 0.0
    last_overall: TrendStatus | None = None
    last_checked_at: float | None = None
    components: tuple[ComponentTrendSummary, ...] = ()
    routes: tuple[RouteTrendSummary, ...] = ()
    alerts: tuple[TrendAlertSummary, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "days": self.days,
            "samples": self.samples,
            "status_counts": {
                "healthy": self.healthy_samples,
                "degraded": self.degraded_samples,
                "unknown": self.unknown_samples,
                "not_observed": self.not_observed_samples,
            },
            "unknown_ratio": round(self.unknown_ratio, 6),
            "degraded_ratio": round(self.degraded_ratio, 6),
            "last_overall": self.last_overall,
            "last_checked_at": (
                round(self.last_checked_at, 3)
                if self.last_checked_at is not None
                else None
            ),
            "components": [item.as_dict() for item in self.components],
            "routes": [item.as_dict() for item in self.routes],
            "alerts": [item.as_dict() for item in self.alerts],
        }


class HealthTrendStore:
    """Persist and query aggregate health observations below an XDSH root."""

    def __init__(self, state_root: Path, *, retention_days: int = 30) -> None:
        root = state_root.expanduser()
        if root.is_symlink():
            raise HealthTrendError("health trend state root is unsafe")
        resolved = root.resolve()
        if (
            resolved in {Path(resolved.anchor), Path.home().resolve()}
            or ".xclaude" in resolved.parts
            or ".dsh" in resolved.parts
        ):
            raise HealthTrendError("health trend state root is unsafe")
        if not 7 <= retention_days <= 90:
            raise ValueError("health trend retention must be between 7 and 90 days")
        self.state_root = resolved
        self.root = resolved / "health"
        self.path = self.root / "trends.sqlite3"
        self.retention_days = retention_days

    def record(
        self,
        observation: HealthTrendObservation,
        *,
        min_interval_seconds: float = 60.0,
        force: bool = False,
    ) -> bool:
        """Record a snapshot, coalescing unchanged high-frequency samples.

        A changed status/reason/route is recorded immediately even inside the
        interval.  Unchanged watch-loop samples are bounded to one row per
        interval, preventing a five-second watcher from filling the state root.
        """

        if (
            not isinstance(min_interval_seconds, int | float)
            or isinstance(min_interval_seconds, bool)
            or not math.isfinite(float(min_interval_seconds))
            or min_interval_seconds < 0
            or min_interval_seconds > 86_400
        ):
            raise HealthTrendError("health trend interval is invalid")
        with closing(self._connect(create=True)) as connection:
            latest = connection.execute(
                "SELECT id, checked_at, overall FROM observations "
                "ORDER BY checked_at DESC, id DESC LIMIT 1"
            ).fetchone()
            if latest is not None:
                latest_id = int(cast(int, latest[0]))
                latest_at = float(cast(float, latest[1]))
                if observation.checked_at < latest_at - 300.0:
                    raise HealthTrendError("health observation clock moved backwards")
                changed = self._signature(connection, latest_id) != _signature(observation)
                if (
                    not force
                    and not changed
                    and observation.checked_at - latest_at < min_interval_seconds
                ):
                    return False
            with connection:
                cursor = connection.execute(
                    "INSERT INTO observations (checked_at, overall) VALUES (?, ?)",
                    (float(observation.checked_at), observation.overall),
                )
                observation_id = cursor.lastrowid
                if not isinstance(observation_id, int):
                    raise sqlite3.IntegrityError("health observation did not receive an id")
                self._insert_components(connection, observation_id, observation.components)
                if latest is not None:
                    self._insert_transition_alerts(
                        connection,
                        previous_id=int(latest[0]),
                        observation_id=observation_id,
                        checked_at=float(observation.checked_at),
                        components=observation.components,
                    )
                cutoff = float(observation.checked_at) - self.retention_days * 86_400
                connection.execute("DELETE FROM observations WHERE checked_at < ?", (cutoff,))
        self._secure()
        return True

    def summary(self, *, days: int, now: float | None = None) -> TrendWindowSummary:
        """Return one aggregate window using a read-only SQLite connection."""

        if not 1 <= days <= self.retention_days:
            raise ValueError("health trend window is outside retention")
        timestamp = self._now() if now is None else float(now)
        if not math.isfinite(timestamp) or timestamp < 0:
            raise HealthTrendError("health trend query timestamp is invalid")
        if not self.path.is_file():
            return TrendWindowSummary(days=days)
        if self.path.is_symlink():
            raise HealthTrendError("health trend database path is unsafe")
        since = max(0.0, timestamp - days * 86_400)
        try:
            with closing(self._connect_read_only()) as connection:
                observations = connection.execute(
                    "SELECT id, checked_at, overall FROM observations "
                    "WHERE checked_at >= ? AND checked_at <= ? "
                    "ORDER BY checked_at, id",
                    (since, timestamp),
                ).fetchall()
                components = connection.execute(
                    """
                    SELECT c.observation_id, c.name, c.status, c.reason
                    FROM components AS c
                    JOIN observations AS o ON o.id = c.observation_id
                    WHERE o.checked_at >= ? AND o.checked_at <= ?
                    ORDER BY o.checked_at, o.id, c.name
                    """,
                    (since, timestamp),
                ).fetchall()
                routes = connection.execute(
                    """
                    SELECT r.observation_id, r.route, r.status
                    FROM routes AS r
                    JOIN observations AS o ON o.id = r.observation_id
                    WHERE o.checked_at >= ? AND o.checked_at <= ?
                    ORDER BY o.checked_at, o.id, r.route
                    """,
                    (since, timestamp),
                ).fetchall()
                alerts = connection.execute(
                    """
                    SELECT code, component, route, COUNT(*), MAX(checked_at)
                    FROM alerts
                    WHERE checked_at >= ? AND checked_at <= ?
                    GROUP BY code, component, route
                    ORDER BY component, code, route
                    """,
                    (since, timestamp),
                ).fetchall()
        except sqlite3.Error as error:
            raise HealthTrendError("health trend database is unreadable") from error
        return _build_window(days, observations, components, routes, alerts)

    def summaries(
        self,
        *,
        now: float | None = None,
        windows: Iterable[int] = (7, 30),
    ) -> tuple[TrendWindowSummary, ...]:
        """Return sorted, bounded windows; each query remains read-only."""

        selected = tuple(dict.fromkeys(int(days) for days in windows))
        if not selected:
            return ()
        if any(days < 1 or days > self.retention_days for days in selected):
            raise ValueError("health trend window is outside retention")
        timestamp = self._now() if now is None else float(now)
        return tuple(self.summary(days=days, now=timestamp) for days in sorted(selected))

    def _insert_components(
        self,
        connection: sqlite3.Connection,
        observation_id: int,
        components: tuple[HealthTrendComponent, ...],
    ) -> None:
        for component in components:
            connection.execute(
                "INSERT INTO components "
                "(observation_id, name, status, reason, age_seconds) VALUES (?, ?, ?, ?, ?)",
                (
                    observation_id,
                    component.name,
                    component.status,
                    component.reason,
                    component.age_seconds,
                ),
            )
            connection.executemany(
                "INSERT INTO component_counts "
                "(observation_id, component, count_key, count_value) VALUES (?, ?, ?, ?)",
                (
                    (observation_id, component.name, key, value)
                    for key, value in component.counts
                ),
            )
            connection.executemany(
                "INSERT INTO routes (observation_id, route, status) VALUES (?, ?, ?)",
                (
                    (observation_id, route, status)
                    for route, status in component.routes
                ),
            )

    def _insert_transition_alerts(
        self,
        connection: sqlite3.Connection,
        *,
        previous_id: int,
        observation_id: int,
        checked_at: float,
        components: tuple[HealthTrendComponent, ...],
    ) -> None:
        previous_components = {
            str(row[0]): (str(row[1]), str(row[2]))
            for row in connection.execute(
                "SELECT name, status, reason FROM components WHERE observation_id = ?",
                (previous_id,),
            ).fetchall()
        }
        previous_routes = {
            str(row[0]): str(row[1])
            for row in connection.execute(
                "SELECT route, status FROM routes WHERE observation_id = ?",
                (previous_id,),
            ).fetchall()
        }
        for component in components:
            old_status, old_reason = previous_components.get(component.name, ("", ""))
            if component.reason in _STALE_REASONS and (
                old_reason not in _STALE_REASONS or old_reason != component.reason
            ):
                self._alert(
                    connection,
                    observation_id,
                    checked_at,
                    code="heartbeat_stale",
                    component=component.name,
                    route=None,
                )
            if component.status == "unknown" and old_status != "unknown":
                self._alert(
                    connection,
                    observation_id,
                    checked_at,
                    code="component_unknown",
                    component=component.name,
                    route=None,
                )
            for route, status in component.routes:
                old_route = previous_routes.get(route, "")
                if status == "unavailable" and old_route != "unavailable":
                    self._alert(
                        connection,
                        observation_id,
                        checked_at,
                        code="route_unavailable",
                        component=component.name,
                        route=route,
                    )
                if status == "unknown" and old_route != "unknown":
                    self._alert(
                        connection,
                        observation_id,
                        checked_at,
                        code="route_unknown",
                        component=component.name,
                        route=route,
                    )

    @staticmethod
    def _alert(
        connection: sqlite3.Connection,
        observation_id: int,
        checked_at: float,
        *,
        code: str,
        component: str,
        route: str | None,
    ) -> None:
        connection.execute(
            "INSERT OR IGNORE INTO alerts "
            "(observation_id, checked_at, code, component, route) VALUES (?, ?, ?, ?, ?)",
            (observation_id, checked_at, code, component, route),
        )

    def _signature(
        self, connection: sqlite3.Connection, observation_id: int
    ) -> tuple[object, ...]:
        overall_row = connection.execute(
            "SELECT overall FROM observations WHERE id = ?", (observation_id,)
        ).fetchone()
        overall = str(overall_row[0]) if overall_row is not None else ""
        components = tuple(
            (str(row[0]), str(row[1]), str(row[2]))
            for row in connection.execute(
                "SELECT name, status, reason FROM components WHERE observation_id = ? "
                "ORDER BY name",
                (observation_id,),
            ).fetchall()
        )
        component_counts = tuple(
            (str(row[0]), str(row[1]), int(row[2]))
            for row in connection.execute(
                "SELECT component, count_key, count_value FROM component_counts "
                "WHERE observation_id = ? ORDER BY component, count_key",
                (observation_id,),
            ).fetchall()
        )
        routes = tuple(
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                "SELECT route, status FROM routes WHERE observation_id = ? ORDER BY route",
                (observation_id,),
            ).fetchall()
        )
        return (overall, components, component_counts, routes)

    def _connect(self, *, create: bool) -> sqlite3.Connection:
        if self.path.is_symlink() or (self.path.exists() and not self.path.is_file()):
            raise HealthTrendError("health trend database path is unsafe")
        if create:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self.root.is_symlink() or not self.root.is_dir():
                raise HealthTrendError("health trend state root is unsafe")
            self.root.chmod(0o700)
        try:
            connection = sqlite3.connect(self.path, timeout=5.0)
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA foreign_keys = ON")
            if create:
                connection.execute("PRAGMA journal_mode = DELETE")
                connection.execute("PRAGMA synchronous = FULL")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS observations (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        checked_at REAL NOT NULL,
                        overall TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS observations_checked_at
                        ON observations(checked_at, id);
                    CREATE TABLE IF NOT EXISTS components (
                        observation_id INTEGER NOT NULL REFERENCES observations(id)
                            ON DELETE CASCADE,
                        name TEXT NOT NULL,
                        status TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        age_seconds REAL,
                        PRIMARY KEY(observation_id, name)
                    );
                    CREATE TABLE IF NOT EXISTS component_counts (
                        observation_id INTEGER NOT NULL REFERENCES observations(id)
                            ON DELETE CASCADE,
                        component TEXT NOT NULL,
                        count_key TEXT NOT NULL,
                        count_value INTEGER NOT NULL,
                        PRIMARY KEY(observation_id, component, count_key)
                    );
                    CREATE TABLE IF NOT EXISTS routes (
                        observation_id INTEGER NOT NULL REFERENCES observations(id)
                            ON DELETE CASCADE,
                        route TEXT NOT NULL,
                        status TEXT NOT NULL,
                        PRIMARY KEY(observation_id, route)
                    );
                    CREATE INDEX IF NOT EXISTS routes_route
                        ON routes(route, observation_id);
                    CREATE TABLE IF NOT EXISTS alerts (
                        observation_id INTEGER NOT NULL REFERENCES observations(id)
                            ON DELETE CASCADE,
                        checked_at REAL NOT NULL,
                        code TEXT NOT NULL,
                        component TEXT NOT NULL,
                        route TEXT,
                        PRIMARY KEY(observation_id, code, component, route)
                    );
                    CREATE INDEX IF NOT EXISTS alerts_checked_at
                        ON alerts(checked_at, component, code);
                    """
                )
        except sqlite3.Error as error:
            raise HealthTrendError("SQLite health trend state is unavailable") from error
        if create:
            self._secure()
        return connection

    def _connect_read_only(self) -> sqlite3.Connection:
        if self.path.is_symlink() or not self.path.is_file():
            raise HealthTrendError("health trend database path is unsafe")
        try:
            connection = sqlite3.connect(
                f"{self.path.as_uri()}?mode=ro",
                uri=True,
                timeout=5.0,
            )
            connection.execute("PRAGMA query_only = ON")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            return connection
        except sqlite3.Error as error:
            raise HealthTrendError("SQLite health trend state is unreadable") from error

    def _secure(self) -> None:
        self.root.chmod(0o700)
        os.chmod(self.path, 0o600)

    @staticmethod
    def _now() -> float:
        import time

        return float(time.time())


def _signature(observation: HealthTrendObservation) -> tuple[object, ...]:
    components = tuple(
        (
            component.name,
            component.status,
            component.reason,
        )
        for component in sorted(observation.components, key=lambda item: item.name)
    )
    routes = tuple(
        (route, status)
        for component in sorted(observation.components, key=lambda item: item.name)
        for route, status in sorted(component.routes)
    )
    component_counts = tuple(
        (component.name, key, value)
        for component in sorted(observation.components, key=lambda item: item.name)
        for key, value in sorted(component.counts)
    )
    return (observation.overall, components, component_counts, routes)


def _build_window(
    days: int,
    observations: list[ObservationRow],
    component_rows: list[ComponentRow],
    route_rows: list[RouteRow],
    alert_rows: list[AlertRow],
) -> TrendWindowSummary:
    statuses: defaultdict[str, int] = defaultdict(int)
    for _id, _checked_at, overall in observations:
        statuses[str(overall)] += 1
    samples = len(observations)
    components_by_name: dict[str, list[tuple[float, str, str]]] = defaultdict(list)
    observation_times: dict[int, float] = {row[0]: row[1] for row in observations}
    for observation_id, name, status, reason in component_rows:
        components_by_name[str(name)].append(
            (observation_times[observation_id], status, reason)
        )
    component_summaries: list[ComponentTrendSummary] = []
    for name, component_history in sorted(components_by_name.items()):
        component_history.sort()
        component_counts: defaultdict[str, int] = defaultdict(int)
        stale_transitions = unknown_transitions = 0
        previous_status: str | None = None
        previous_reason: str | None = None
        for _at, status, reason in component_history:
            component_counts[status] += 1
            if reason in _STALE_REASONS and (
                previous_reason not in _STALE_REASONS or previous_reason != reason
            ):
                stale_transitions += 1
            if status == "unknown" and previous_status != "unknown":
                unknown_transitions += 1
            previous_status, previous_reason = status, reason
        last_at, last_status, last_reason = component_history[-1]
        component_summaries.append(
            ComponentTrendSummary(
                name=name,
                samples=len(component_history),
                healthy=component_counts["healthy"],
                degraded=component_counts["degraded"],
                unknown=component_counts["unknown"],
                not_observed=component_counts["not_observed"],
                stale_transitions=stale_transitions,
                unknown_transitions=unknown_transitions,
                last_status=cast(TrendStatus, last_status),
                last_reason=last_reason,
                last_seen_at=last_at,
            )
        )
    routes_by_name: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for observation_id, route, status in route_rows:
        routes_by_name[route].append((observation_times[observation_id], status))
    route_summaries: list[RouteTrendSummary] = []
    for route, route_history in sorted(routes_by_name.items()):
        route_history.sort()
        route_counts: defaultdict[str, int] = defaultdict(int)
        unavailable_transitions = unknown_transitions = 0
        route_previous_status: str | None = None
        for _at, status in route_history:
            route_counts[status] += 1
            if status == "unavailable" and route_previous_status != "unavailable":
                unavailable_transitions += 1
            if status == "unknown" and route_previous_status != "unknown":
                unknown_transitions += 1
            route_previous_status = status
        last_at, last_status = route_history[-1]
        route_summaries.append(
            RouteTrendSummary(
                route=route,
                samples=len(route_history),
                available=route_counts["available"],
                unavailable=route_counts["unavailable"],
                unknown=route_counts["unknown"],
                not_observed=route_counts["not_observed"],
                unavailable_transitions=unavailable_transitions,
                unknown_transitions=unknown_transitions,
                last_status=cast(RouteStatus, last_status),
                last_seen_at=last_at,
            )
        )
    alert_summaries = tuple(
        TrendAlertSummary(
            code=str(code),
            component=str(component),
            route=str(route) if route is not None else None,
            occurrences=occurrences,
            last_seen_at=last_seen,
        )
        for code, component, route, occurrences, last_seen in alert_rows
    )
    return TrendWindowSummary(
        days=days,
        samples=samples,
        healthy_samples=statuses["healthy"],
        degraded_samples=statuses["degraded"],
        unknown_samples=statuses["unknown"],
        not_observed_samples=statuses["not_observed"],
        unknown_ratio=(statuses["unknown"] / samples if samples else 0.0),
        degraded_ratio=(statuses["degraded"] / samples if samples else 0.0),
        last_overall=(cast(TrendStatus, str(observations[-1][2])) if observations else None),
        last_checked_at=(float(observations[-1][1]) if observations else None),
        components=tuple(component_summaries),
        routes=tuple(route_summaries),
        alerts=alert_summaries,
    )
