"""Credential-free, runner-neutral execution metrics."""

from __future__ import annotations

import math
import os
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

_SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
_MAX_SELECTED_SKILLS = 2


@dataclass(frozen=True, slots=True, kw_only=True)
class RunMetric:
    """One terminal task measurement; prompts and answers are intentionally absent."""

    finished_at: float
    model: str
    succeeded: bool
    finish_reason: str
    duration_ms: int
    first_event_ms: int | None
    cold_start_ms: int | None
    cold_start_retries: int
    tool_calls: int
    total_tokens: int
    guard_reason: str | None = None
    error_code: str | None = None
    first_answer_ms: int | None = None
    selected_skills: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.finished_at < 0:
            raise ValueError("metric timestamp cannot be negative")
        if not self.model or len(self.model) > 200:
            raise ValueError("metric model must use 1-200 characters")
        if not self.finish_reason or len(self.finish_reason) > 80:
            raise ValueError("metric finish reason must use 1-80 characters")
        counters = (
            self.duration_ms,
            self.cold_start_retries,
            self.tool_calls,
            self.total_tokens,
        )
        if any(value < 0 for value in counters):
            raise ValueError("metric counters cannot be negative")
        if self.first_event_ms is not None and self.first_event_ms < 0:
            raise ValueError("first-event duration cannot be negative")
        if self.first_answer_ms is not None and self.first_answer_ms < 0:
            raise ValueError("first-answer duration cannot be negative")
        if self.cold_start_ms is not None and self.cold_start_ms < 0:
            raise ValueError("cold-start duration cannot be negative")
        for value in (self.guard_reason, self.error_code):
            if value is not None and (len(value) > 80 or "\n" in value or "\r" in value):
                raise ValueError("metric category must be a short single-line value")
        if (
            len(self.selected_skills) > _MAX_SELECTED_SKILLS
            or len(set(self.selected_skills)) != len(self.selected_skills)
            or any(_SKILL_NAME.fullmatch(skill) is None for skill in self.selected_skills)
        ):
            raise ValueError("metric Skills must be unique safe names within the selection limit")


@dataclass(frozen=True, slots=True)
class SkillOutcomeSummary:
    """Observed terminal outcomes for runs where one Skill was selected."""

    skill: str
    runs: int
    succeeded: int
    success_rate: float
    last_used_at: float


@dataclass(frozen=True, slots=True)
class RunMetricsSummary:
    total: int = 0
    succeeded: int = 0
    success_rate: float = 0.0
    duration_p50_ms: int | None = None
    duration_p95_ms: int | None = None
    first_event_p95_ms: int | None = None
    first_answer_p95_ms: int | None = None
    cold_start_p95_ms: int | None = None
    cold_start_retries: int = 0
    guard_reasons: tuple[tuple[str, int], ...] = ()
    model_routes: tuple[tuple[str, int, int], ...] = ()
    skill_outcomes: tuple[SkillOutcomeSummary, ...] = ()


class RunMetricsStore:
    """Small SQLite ledger suitable for cross-run health summaries."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()

    def record(self, metric: RunMetric) -> None:
        with closing(self._connect(create=True)) as connection:
            with connection:
                cursor = connection.execute(
                    """
                    INSERT INTO runs (
                        finished_at, model, succeeded, finish_reason, duration_ms,
                        first_event_ms, first_answer_ms, cold_start_ms, cold_start_retries,
                        tool_calls, total_tokens, guard_reason, error_code
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        metric.finished_at,
                        metric.model,
                        int(metric.succeeded),
                        metric.finish_reason,
                        metric.duration_ms,
                        metric.first_event_ms,
                        metric.first_answer_ms,
                        metric.cold_start_ms,
                        metric.cold_start_retries,
                        metric.tool_calls,
                        metric.total_tokens,
                        metric.guard_reason or None,
                        metric.error_code or None,
                    ),
                )
                run_id = cursor.lastrowid
                if not isinstance(run_id, int):
                    raise sqlite3.IntegrityError("run metric did not receive an id")
                connection.executemany(
                    """
                    INSERT INTO skill_runs (run_id, skill, succeeded, finished_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        (run_id, skill, int(metric.succeeded), metric.finished_at)
                        for skill in metric.selected_skills
                    ),
                )
        os.chmod(self.path, 0o600)

    def summary(self, *, since: float) -> RunMetricsSummary:
        if since < 0:
            raise ValueError("metrics summary timestamp cannot be negative")
        if not self.path.is_file():
            return RunMetricsSummary()
        with closing(self._connect(create=False)) as connection:
            rows = connection.execute(
                """
                SELECT model, succeeded, duration_ms, first_event_ms, first_answer_ms,
                       cold_start_ms, cold_start_retries, guard_reason
                FROM runs WHERE finished_at >= ? ORDER BY id
                """,
                (since,),
            ).fetchall()
            skill_rows = connection.execute(
                """
                SELECT skill, COUNT(*), SUM(succeeded), MAX(finished_at)
                FROM skill_runs
                WHERE finished_at >= ?
                GROUP BY skill
                ORDER BY skill
                """,
                (since,),
            ).fetchall()
        if not rows:
            return RunMetricsSummary()
        durations = [int(row[2]) for row in rows]
        first_events = [int(row[3]) for row in rows if row[3] is not None]
        first_answers = [int(row[4]) for row in rows if row[4] is not None]
        cold_starts = [int(row[5]) for row in rows if row[5] is not None]
        route_counts: dict[str, list[int]] = {}
        guards: dict[str, int] = {}
        succeeded = 0
        retries = 0
        for model, success, _duration, _first, _first_answer, _cold, retry_count, guard in rows:
            succeeded += int(success)
            retries += int(retry_count)
            counts = route_counts.setdefault(str(model), [0, 0])
            counts[0] += int(success)
            counts[1] += 1
            if guard:
                guards[str(guard)] = guards.get(str(guard), 0) + 1
        total = len(rows)
        return RunMetricsSummary(
            total=total,
            succeeded=succeeded,
            success_rate=succeeded / total,
            duration_p50_ms=_percentile(durations, 0.50),
            duration_p95_ms=_percentile(durations, 0.95),
            first_event_p95_ms=_percentile(first_events, 0.95),
            first_answer_p95_ms=_percentile(first_answers, 0.95),
            cold_start_p95_ms=_percentile(cold_starts, 0.95),
            cold_start_retries=retries,
            guard_reasons=tuple(sorted(guards.items())),
            model_routes=tuple(
                (model, counts[0], counts[1])
                for model, counts in sorted(route_counts.items())
            ),
            skill_outcomes=tuple(
                SkillOutcomeSummary(
                    skill=str(skill),
                    runs=int(run_count),
                    succeeded=int(success_count),
                    success_rate=int(success_count) / int(run_count),
                    last_used_at=float(last_used_at),
                )
                for skill, run_count, success_count, last_used_at in skill_rows
            ),
        )

    def _connect(self, *, create: bool) -> sqlite3.Connection:
        if create:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.path.parent.chmod(0o700)
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY,
                finished_at REAL NOT NULL,
                model TEXT NOT NULL,
                succeeded INTEGER NOT NULL,
                finish_reason TEXT NOT NULL,
                duration_ms INTEGER NOT NULL,
                first_event_ms INTEGER,
                cold_start_ms INTEGER,
                cold_start_retries INTEGER NOT NULL,
                tool_calls INTEGER NOT NULL,
                total_tokens INTEGER NOT NULL,
                guard_reason TEXT,
                error_code TEXT
            )
            """
        )
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(runs)")
        }
        if "first_answer_ms" not in columns:
            connection.execute("ALTER TABLE runs ADD COLUMN first_answer_ms INTEGER")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS skill_runs (
                run_id INTEGER NOT NULL,
                skill TEXT NOT NULL,
                succeeded INTEGER NOT NULL,
                finished_at REAL NOT NULL,
                PRIMARY KEY (run_id, skill),
                FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS skill_runs_finished_at
            ON skill_runs(finished_at)
            """
        )
        return connection


def _percentile(values: list[int], percentile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * percentile) - 1)
    return ordered[index]
