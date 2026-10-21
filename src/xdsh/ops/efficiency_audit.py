"""Compare XDSH and Hermes Telegram latency without emitting message content.

The audit reads prompt text only long enough to normalize and hash it.  Its
public data model and JSON report contain hashes, lengths, timestamps, and
aggregate execution metadata; prompts, answers, tool results, chat IDs, and
session IDs never cross the output seam.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import statistics
import time
import unicodedata
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

_XDSH_TERMINAL_STATES = frozenset({"completed", "failed", "cancelled"})
_HERMES_TERMINAL_REASONS = frozenset({"stop", "length", "content_filter", "error"})


@dataclass(frozen=True, slots=True)
class XdshTurn:
    prompt_hash: str
    prompt_chars: int
    started_at: float
    duration_ms: int
    status: str


@dataclass(frozen=True, slots=True)
class HermesTurn:
    prompt_hash: str
    prompt_chars: int
    started_at: float
    duration_ms: int
    api_calls: int
    tool_calls: int
    finish_reason: str


class _PairReport(TypedDict):
    prompt_hash: str
    prompt_chars: int
    start_delta_ms: int
    xdsh_duration_ms: int
    xdsh_status: str
    hermes_duration_ms: int
    hermes_api_calls: int
    hermes_tool_calls: int
    hermes_finish_reason: str
    xdsh_over_hermes_ratio: float | None


def normalize_prompt(value: str) -> str:
    """Return the minimal stable form used solely for same-prompt matching."""

    return " ".join(unicodedata.normalize("NFKC", value).split())


def prompt_digest(value: str) -> str:
    normalized = normalize_prompt(value)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _timestamp(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _readonly_connection(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve()
    if ".xclaude" in resolved.parts:
        raise ValueError("efficiency audit must not read reserved legacy state")
    if not resolved.is_file():
        raise ValueError(f"SQLite database is unavailable: {resolved}")
    return sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)


def load_xdsh_turns(
    path: Path,
    *,
    since: float,
    min_prompt_chars: int = 20,
) -> tuple[XdshTurn, ...]:
    """Load terminal XDSH turn metadata; discard prompt text after hashing."""

    if min_prompt_chars < 1:
        raise ValueError("min_prompt_chars must be positive")
    with _readonly_connection(path) as connection:
        rows = connection.execute(
            """
            select prompt, status, started_at, updated_at
            from turns
            where started_at >= ? and prompt is not null
            order by started_at
            """,
            (since,),
        ).fetchall()

    turns: list[XdshTurn] = []
    for raw_prompt, raw_status, raw_started, raw_updated in rows:
        if not isinstance(raw_prompt, str) or not isinstance(raw_status, str):
            continue
        if raw_status not in _XDSH_TERMINAL_STATES:
            continue
        normalized = normalize_prompt(raw_prompt)
        if len(normalized) < min_prompt_chars:
            continue
        started_at = _timestamp(raw_started)
        updated_at = _timestamp(raw_updated)
        if started_at is None or updated_at is None:
            continue
        turns.append(
            XdshTurn(
                prompt_hash=prompt_digest(normalized),
                prompt_chars=len(normalized),
                started_at=started_at,
                duration_ms=max(0, round((updated_at - started_at) * 1000)),
                status=raw_status,
            )
        )
    return tuple(turns)


def load_hermes_turns(
    path: Path,
    *,
    since: float,
    min_prompt_chars: int = 20,
) -> tuple[HermesTurn, ...]:
    """Derive completed Hermes Telegram turns from its append-only messages."""

    if min_prompt_chars < 1:
        raise ValueError("min_prompt_chars must be positive")
    with _readonly_connection(path) as connection:
        rows = connection.execute(
            """
            select m.session_id, m.role, m.content, m.timestamp,
                   m.finish_reason, m.display_kind
            from messages as m
            join sessions as s on s.id = m.session_id
            where s.source = 'telegram'
              and m.timestamp >= ?
              and coalesce(m.active, 1) = 1
            order by m.session_id, m.timestamp, m.id
            """,
            (since,),
        ).fetchall()

    by_session: dict[str, list[tuple[object, ...]]] = defaultdict(list)
    for row in rows:
        by_session[str(row[0])].append(tuple(row[1:]))

    turns: list[HermesTurn] = []
    for messages in by_session.values():
        for index, message in enumerate(messages):
            role, content, timestamp, _finish_reason, display_kind = message
            if role != "user" or display_kind == "internal_notification":
                continue
            if not isinstance(content, str):
                continue
            normalized = normalize_prompt(content)
            if len(normalized) < min_prompt_chars:
                continue

            following: list[tuple[object, ...]] = []
            for candidate in messages[index + 1 :]:
                if candidate[0] == "user":
                    break
                following.append(candidate)
            terminal = next(
                (
                    candidate
                    for candidate in reversed(following)
                    if candidate[0] == "assistant"
                    and candidate[3] in _HERMES_TERMINAL_REASONS
                ),
                None,
            )
            if terminal is None:
                continue
            started_at = _timestamp(timestamp)
            ended_at = _timestamp(terminal[2])
            if started_at is None or ended_at is None:
                continue
            turns.append(
                HermesTurn(
                    prompt_hash=prompt_digest(normalized),
                    prompt_chars=len(normalized),
                    started_at=started_at,
                    duration_ms=max(0, round((ended_at - started_at) * 1000)),
                    api_calls=sum(
                        1
                        for candidate in following
                        if candidate[0] == "assistant" and candidate[3] is not None
                    ),
                    tool_calls=sum(1 for candidate in following if candidate[0] == "tool"),
                    finish_reason=str(terminal[3]),
                )
            )
    return tuple(sorted(turns, key=lambda turn: turn.started_at))


def _distribution(values: Sequence[float | int]) -> dict[str, float | int | None]:
    if not values:
        return {"median": None, "p95": None, "max": None}
    ordered = sorted(values)
    rank_95 = min(len(ordered) - 1, int((len(ordered) - 1) * 0.95))
    median = statistics.median(ordered)
    return {
        "median": round(median, 3) if isinstance(median, float) else median,
        "p95": ordered[rank_95],
        "max": ordered[-1],
    }


def build_report(
    *,
    xdsh_turns: Sequence[XdshTurn],
    hermes_turns: Sequence[HermesTurn],
    max_start_delta_seconds: float = 21_600.0,
) -> dict[str, object]:
    """Pair same-prompt observations by nearest start time, once per side."""

    if max_start_delta_seconds <= 0:
        raise ValueError("max_start_delta_seconds must be positive")
    hermes_by_hash: dict[str, list[tuple[int, HermesTurn]]] = defaultdict(list)
    for index, turn in enumerate(hermes_turns):
        hermes_by_hash[turn.prompt_hash].append((index, turn))

    used_hermes: set[int] = set()
    pairs: list[_PairReport] = []
    for xdsh in sorted(xdsh_turns, key=lambda turn: turn.started_at):
        candidates = [
            (index, hermes)
            for index, hermes in hermes_by_hash.get(xdsh.prompt_hash, ())
            if index not in used_hermes
            and abs(hermes.started_at - xdsh.started_at) <= max_start_delta_seconds
        ]
        if not candidates:
            continue
        hermes_index, hermes = min(
            candidates,
            key=lambda item: abs(item[1].started_at - xdsh.started_at),
        )
        used_hermes.add(hermes_index)
        ratio = xdsh.duration_ms / hermes.duration_ms if hermes.duration_ms else None
        pairs.append(
            {
                "prompt_hash": xdsh.prompt_hash,
                "prompt_chars": xdsh.prompt_chars,
                "start_delta_ms": round(abs(hermes.started_at - xdsh.started_at) * 1000),
                "xdsh_duration_ms": xdsh.duration_ms,
                "xdsh_status": xdsh.status,
                "hermes_duration_ms": hermes.duration_ms,
                "hermes_api_calls": hermes.api_calls,
                "hermes_tool_calls": hermes.tool_calls,
                "hermes_finish_reason": hermes.finish_reason,
                "xdsh_over_hermes_ratio": round(ratio, 3) if ratio is not None else None,
            }
        )

    ratios = [
        float(pair["xdsh_over_hermes_ratio"])
        for pair in pairs
        if pair["xdsh_over_hermes_ratio"] is not None
    ]
    return {
        "schema_version": 1,
        "content_policy": "hashes_and_aggregate_metadata_only",
        "xdsh_terminal_turns": len(xdsh_turns),
        "hermes_terminal_turns": len(hermes_turns),
        "matched_pairs": len(pairs),
        "xdsh_duration_ms": _distribution(
            [int(pair["xdsh_duration_ms"]) for pair in pairs]
        ),
        "hermes_duration_ms": _distribution(
            [int(pair["hermes_duration_ms"]) for pair in pairs]
        ),
        "xdsh_over_hermes_ratio": _distribution(ratios),
        "pairs": pairs,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xdsh-ledger", type=Path, required=True)
    parser.add_argument("--hermes-state", type=Path, required=True)
    parser.add_argument("--days", type=float, default=30.0)
    parser.add_argument("--min-prompt-chars", type=int, default=20)
    parser.add_argument("--max-start-delta-seconds", type=float, default=21_600.0)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    if args.days <= 0:
        raise SystemExit("days must be positive")
    since = time.time() - args.days * 86_400
    try:
        report = build_report(
            xdsh_turns=load_xdsh_turns(
                args.xdsh_ledger,
                since=since,
                min_prompt_chars=args.min_prompt_chars,
            ),
            hermes_turns=load_hermes_turns(
                args.hermes_state,
                since=since,
                min_prompt_chars=args.min_prompt_chars,
            ),
            max_start_delta_seconds=args.max_start_delta_seconds,
        )
    except (OSError, sqlite3.Error, ValueError) as error:
        raise SystemExit(type(error).__name__) from None
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
