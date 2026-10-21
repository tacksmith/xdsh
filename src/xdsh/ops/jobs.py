"""Read-only JSON consumer for the durable XDSH jobs and interaction board."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

from xdsh.core.jobs import (
    DurableJobBoard,
    DurableJobError,
    JobBoardProjection,
    JobCard,
    JobEventAudit,
    JobEventIntegrity,
    JobHealthSummary,
    JobInteractionHealthSummary,
    JobInteractionIntegrity,
    JobInteractionIntegrityStatus,
    JobKind,
    JobStatus,
)
from xdsh.core.product_control import safe_exception_text
from xdsh.core.snapshots import (
    SnapshotError,
    fingerprint_matches,
    fingerprinted_snapshot_payload,
    job_card_payload,
    job_event_audit_payload,
    job_event_validation_payload,
    job_projection_fields,
    metadata_diff,
    snapshot_bundle_payload,
    snapshot_contract_probe_set,
    snapshot_contract_report,
    snapshot_payload,
    validate_contract_check_payload,
    validate_inspect_payload,
    validate_job_event_audit_payload,
    validate_snapshot_bundle,
)
from xdsh.ops.reconciler import (
    DEFAULT_PENDING_INTERACTION_WARNING_SECONDS,
    DEFAULT_RUNNING_WARNING_SECONDS,
)

_JOB_STATUSES = (
    "blocked",
    "queued",
    "running",
    "completed",
    "failed",
    "cancelled",
    "unknown",
    "manual_review",
)
_JOB_KINDS = ("background", "scheduled")
_FORMATS = ("json", "text")
_MAX_FOLLOW_RUNTIME_SECONDS = 3_600.0
EXIT_OK = 0
EXIT_DATA_ERROR = 2
EXIT_EMPTY_STATE = 3


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state-root",
        default=os.environ.get("XDSH_STATE_ROOT", "/state"),
        help="XDSH-owned state root; never defaults to a legacy runtime",
    )
    parser.add_argument(
        "--owner-user-id",
        type=int,
        required=True,
        help="explicit Telegram/owner identity (required for every query)",
    )
    parser.add_argument(
        "--chat-id",
        type=int,
        required=True,
        help="explicit chat identity (required for every query)",
    )
    parser.add_argument(
        "--require-state",
        action="store_true",
        help="return exit code 3 when the XDSH jobs database is absent",
    )
    parser.add_argument(
        "--strict-integrity",
        action="store_true",
        help="treat legacy-unverified interaction and job-event chains as errors",
    )
    subparsers = parser.add_subparsers(dest="action", required=True)
    for name in ("board", "stats"):
        command = subparsers.add_parser(name, help=f"read a bounded {name} projection")
        command.add_argument("--status", choices=_JOB_STATUSES)
        command.add_argument("--kind", choices=_JOB_KINDS)
        command.add_argument("--limit", type=int, default=20)
        command.add_argument("--format", choices=_FORMATS, default="json")
    check = subparsers.add_parser(
        "self-check",
        help="compare CLI/Telegram board snapshot contracts without I/O",
    )
    check.add_argument("--format", choices=_FORMATS, default="json")
    inspect = subparsers.add_parser(
        "inspect",
        help="read one owner-authorized content-free job card",
    )
    inspect.add_argument("job_id")
    inspect.add_argument("--format", choices=_FORMATS, default="json")
    bundle = subparsers.add_parser(
        "bundle",
        help="emit one bounded inspect snapshot plus a hand-off manifest",
    )
    bundle.add_argument("job_id")
    bundle.add_argument("--format", choices=_FORMATS, default="json")
    bundle.add_argument("--max-bytes", type=int, default=32_000)
    diff = subparsers.add_parser(
        "diff",
        help="compare one metadata card with a previous inspect JSON",
    )
    diff.add_argument("job_id")
    diff.add_argument("--previous", required=True)
    diff.add_argument("--format", choices=_FORMATS, default="json")
    diff.add_argument("--max-bytes", type=int, default=32_000)
    diff.add_argument(
        "--max-age-seconds",
        type=float,
        help="reject an inspect snapshot older than this explicit bound",
    )
    validate = subparsers.add_parser(
        "validate",
        help="validate a saved inspect JSON without opening the jobs database",
    )
    validate.add_argument("job_id")
    validate.add_argument("--snapshot", required=True)
    validate.add_argument("--format", choices=_FORMATS, default="json")
    validate.add_argument("--max-bytes", type=int, default=32_000)
    validate.add_argument(
        "--max-age-seconds",
        type=float,
        help="reject an inspect snapshot older than this explicit bound",
    )
    validate_events = subparsers.add_parser(
        "validate-events",
        aliases=["events-validate"],
        help="validate a saved redacted job-event audit without opening state",
    )
    validate_events.add_argument("job_id", nargs="?")
    validate_events.add_argument("--snapshot", required=True)
    validate_events.add_argument("--format", choices=_FORMATS, default="json")
    validate_events.add_argument("--max-bytes", type=int, default=32_000)
    validate_events.add_argument(
        "--max-age-seconds",
        type=float,
        help="reject an event audit older than this explicit bound",
    )
    validate_events.add_argument(
        "--expected-integrity",
        choices=("empty", "verified", "legacy_unverified", "tampered"),
        help="require this reported event-chain status",
    )
    validate_contract = subparsers.add_parser(
        "validate-contract",
        aliases=["contract-validate"],
        help="validate a saved cross-adapter jobs contract without opening state",
    )
    validate_contract.add_argument("--snapshot", required=True)
    validate_contract.add_argument("--format", choices=_FORMATS, default="json")
    validate_contract.add_argument("--max-bytes", type=int, default=32_000)
    verify = subparsers.add_parser(
        "verify",
        help="verify a complete hand-off bundle against an expected fingerprint",
    )
    verify.add_argument("job_id")
    verify.add_argument("--bundle", required=True)
    verify.add_argument("--expected-fingerprint", required=True)
    verify.add_argument("--format", choices=_FORMATS, default="json")
    verify.add_argument("--max-bytes", type=int, default=32_000)
    verify.add_argument(
        "--record",
        action="store_true",
        help="explicitly append an idempotent metadata-only outcome to job history",
    )
    verify.add_argument(
        "--max-age-seconds",
        type=float,
        help="reject a bundle whose nested snapshot is older than this bound",
    )
    audit = subparsers.add_parser("audit", help="export explicit interaction audit JSON")
    audit.add_argument("job_id", nargs="?")
    audit.add_argument("--limit", type=int, default=20)
    audit.add_argument("--max-bytes", type=int, default=32_000)
    audit.add_argument("--format", choices=("json",), default="json")
    events = subparsers.add_parser(
        "events",
        help="export a bounded, redacted job-event audit",
    )
    events.add_argument("job_id", nargs="?")
    events.add_argument("--limit", type=int, default=20)
    events.add_argument("--max-bytes", type=int, default=32_000)
    events.add_argument("--format", choices=_FORMATS, default="json")
    follow = subparsers.add_parser(
        "follow",
        help="bounded JSON-lines observation of board/integrity changes",
    )
    follow.add_argument("--status", choices=_JOB_STATUSES)
    follow.add_argument("--kind", choices=_JOB_KINDS)
    follow.add_argument("--limit", type=int, default=20)
    follow.add_argument("--interval", type=float, default=2.0)
    follow.add_argument("--max-bytes", type=int, default=32_000)
    follow.add_argument("--emit-unchanged", action="store_true")
    stop = follow.add_mutually_exclusive_group(required=True)
    stop.add_argument("--samples", type=int)
    stop.add_argument("--duration", type=float)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        owner = int(args.owner_user_id)
        chat = int(args.chat_id)
        if args.action == "validate":
            return _validate_snapshot(
                owner,
                chat,
                args.job_id,
                args.snapshot,
                output_format=args.format,
                max_bytes=args.max_bytes,
                max_age_seconds=args.max_age_seconds,
                strict_integrity=args.strict_integrity,
            )
        if args.action in {"validate-events", "events-validate"}:
            return _validate_events(
                owner,
                chat,
                args.job_id,
                args.snapshot,
                output_format=args.format,
                max_bytes=args.max_bytes,
                max_age_seconds=args.max_age_seconds,
                expected_integrity=args.expected_integrity,
                strict_integrity=args.strict_integrity,
            )
        if args.action in {"validate-contract", "contract-validate"}:
            return _validate_contract(
                owner,
                chat,
                args.snapshot,
                output_format=args.format,
                max_bytes=args.max_bytes,
                strict_integrity=args.strict_integrity,
            )
        if args.action == "verify":
            return _verify_bundle(
                owner,
                chat,
                args.job_id,
                args.bundle,
                args.expected_fingerprint,
                state_root=Path(args.state_root),
                output_format=args.format,
                max_bytes=args.max_bytes,
                max_age_seconds=args.max_age_seconds,
                strict_integrity=args.strict_integrity,
                record=args.record,
            )
        board = DurableJobBoard(Path(args.state_root))
        integrity = board.verify_interaction_integrity()
        event_integrity = board.verify_job_event_integrity()
        integrity_payload = _integrity_dict(
            integrity.status,
            integrity.checked_events,
            integrity.first_bad_sequence,
        )
        event_integrity_payload = _integrity_dict(
            event_integrity.status,
            event_integrity.checked_events,
            event_integrity.first_bad_sequence,
        )
        state_present = board.path.is_file()
        if args.action == "bundle":
            return _bundle(
                board,
                owner,
                chat,
                args.job_id,
                output_format=args.format,
                max_bytes=args.max_bytes,
                require_state=args.require_state,
                strict_integrity=args.strict_integrity,
            )
        if args.action == "inspect":
            return _inspect(
                board,
                owner,
                chat,
                args.job_id,
                output_format=args.format,
                require_state=args.require_state,
                strict_integrity=args.strict_integrity,
            )
        if args.action == "diff":
            return _diff(
                board,
                owner,
                chat,
                args.job_id,
                args.previous,
                output_format=args.format,
                max_bytes=args.max_bytes,
                max_age_seconds=args.max_age_seconds,
                require_state=args.require_state,
                strict_integrity=args.strict_integrity,
            )
        if args.action == "self-check":
            return _self_check(
                board,
                owner,
                chat,
                output_format=args.format,
                require_state=args.require_state,
                strict_integrity=args.strict_integrity,
            )
        if args.action == "follow":
            return _follow(
                board,
                owner,
                chat,
                status=None if args.status is None else cast(JobStatus, args.status),
                kind=None if args.kind is None else cast(JobKind, args.kind),
                limit=args.limit,
                interval=args.interval,
                samples=args.samples,
                duration=args.duration,
                max_bytes=args.max_bytes,
                emit_unchanged=args.emit_unchanged,
                require_state=args.require_state,
                strict_integrity=args.strict_integrity,
            )
        if args.action == "board":
            projection = board.projection(
                owner,
                chat,
                status=None if args.status is None else cast(JobStatus, args.status),
                kind=None if args.kind is None else cast(JobKind, args.kind),
                limit=args.limit,
            )
            payload = snapshot_payload(
                "jobs_board",
                "owner-chat",
                {
                    "owner_user_id": owner,
                    "chat_id": chat,
                    "state_present": state_present,
                    "audit_integrity": integrity_payload,
                    "job_event_integrity": event_integrity_payload,
                    **job_projection_fields(projection),
                },
            )
            if args.format == "text":
                print(_board_text(projection, integrity, owner, chat))
            else:
                _print_json(payload)
            return _exit_code(
                state_present,
                integrity,
                event_integrity=event_integrity,
                require_state=args.require_state,
                strict_integrity=args.strict_integrity,
            )
        if args.action == "stats":
            projection = board.projection(
                owner,
                chat,
                status=None if args.status is None else cast(JobStatus, args.status),
                kind=None if args.kind is None else cast(JobKind, args.kind),
                limit=args.limit,
            )
            interactions = board.interaction_health(owner, chat)
            payload = snapshot_payload(
                "jobs_stats",
                "owner-chat",
                {
                    "owner_user_id": owner,
                    "chat_id": chat,
                    "state_present": state_present,
                    "total": projection.total,
                    "counts": {status: count for status, count in projection.counts},
                    "pending_interactions": interactions.pending,
                    "oldest_pending_interaction_age_seconds": interactions.oldest_age_seconds,
                    "audit_integrity": integrity_payload,
                    "job_event_integrity": event_integrity_payload,
                },
            )
            if args.format == "text":
                print(_stats_text(projection, interactions, integrity, owner, chat))
            else:
                _print_json(payload)
            return _exit_code(
                state_present,
                integrity,
                event_integrity=event_integrity,
                require_state=args.require_state,
                strict_integrity=args.strict_integrity,
            )
        if args.action == "audit":
            if args.format != "json":
                raise ValueError("audit only supports JSON output")
            content = board.export_interactions(
                owner,
                chat,
                args.job_id,
                limit=args.limit,
                max_bytes=args.max_bytes,
            )
            print(content)
            return _exit_code(
                state_present,
                integrity,
                event_integrity=event_integrity,
                require_state=args.require_state,
                strict_integrity=args.strict_integrity,
            )
        if args.action == "events":
            if not 1 <= args.limit <= 100:
                raise ValueError("job event audit limit must be between 1 and 100")
            content = board.export_job_events(
                owner,
                chat,
                args.job_id,
                limit=args.limit,
                max_bytes=args.max_bytes,
            )
            decoded_events = json.loads(content)
            if not isinstance(decoded_events, Mapping):
                raise SnapshotError("job event audit output is malformed")
            validated_events = validate_job_event_audit_payload(
                decoded_events,
                owner_user_id=owner,
                chat_id=chat,
                job_id=args.job_id,
                max_bytes=args.max_bytes,
                allow_tampered=True,
            )
            event_audit = board.job_event_audit(
                owner,
                chat,
                args.job_id,
                limit=args.limit,
            )
            if args.format == "text":
                print(_job_events_text(owner, chat, args.job_id, event_audit))
            else:
                _print_json(validated_events)
            return _exit_code(
                state_present,
                integrity,
                event_integrity=event_audit.integrity,
                require_state=args.require_state,
                strict_integrity=args.strict_integrity,
            )
        raise AssertionError(f"unhandled action: {args.action}")
    except (DurableJobError, OSError, SnapshotError, ValueError) as error:
        print(f"xdsh-jobs: {safe_exception_text(error, max_chars=600)}", file=sys.stderr)
        return EXIT_DATA_ERROR


def _integrity_dict(
    status: JobInteractionIntegrityStatus,
    checked_events: int,
    first_bad_sequence: int | None,
) -> dict[str, object]:
    return {
        "status": status,
        "checked_events": checked_events,
        "first_bad_sequence": first_bad_sequence,
    }


def _print_json(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _inspect(
    board: DurableJobBoard,
    owner: int,
    chat: int,
    job_id: str,
    *,
    output_format: str,
    require_state: bool,
    strict_integrity: bool,
) -> int:
    now = time.time()
    payload, card, health, integrity, state_present = _read_inspect_snapshot(
        board,
        owner,
        chat,
        job_id,
        now=now,
    )
    event_integrity = board.verify_job_event_integrity()
    if output_format == "text":
        lines = [
            f"jobs inspect scope=owner:{owner}/chat:{chat} id={job_id.casefold()} "
            f"state={str(state_present).lower()} audit={integrity.status}"
        ]
        if card is None:
            lines.append("(empty)")
        else:
            lines.append(_card_text(card))
            lines.append(
                f"health running_expired={health.running_expired} "
                f"pending_interactions={health.pending_interactions}"
            )
        print("\n".join(lines))
    else:
        _print_json(payload)
    return _exit_code(
        state_present,
        integrity,
        event_integrity=event_integrity,
        require_state=require_state,
        strict_integrity=strict_integrity,
    )


def _read_inspect_snapshot(
    board: DurableJobBoard,
    owner: int,
    chat: int,
    job_id: str,
    *,
    now: float,
) -> tuple[dict[str, object], JobCard | None, JobHealthSummary, JobInteractionIntegrity, bool]:
    state_present = board.path.is_file()
    integrity = board.verify_interaction_integrity()
    event_integrity = board.verify_job_event_integrity()
    health = board.health_summary(
        now=now,
        owner_user_id=owner,
        chat_id=chat,
    )
    card = board.card(owner, chat, job_id, now=now) if state_present else None
    payload = fingerprinted_snapshot_payload(
        "jobs_inspect",
        "owner-chat",
        {
            "owner_user_id": owner,
            "chat_id": chat,
            "state_present": state_present,
            "observed_at": now,
            "job_id": job_id.casefold(),
            "card": job_card_payload(card) if card is not None else None,
            "health": {
                "counts": {key: value for key, value in health.counts},
                "running_expired": health.running_expired,
                "oldest_running_age_seconds": health.oldest_running_age_seconds,
                "pending_interactions": health.pending_interactions,
                "oldest_pending_interaction_age_seconds": (
                    health.oldest_pending_interaction_age_seconds
                ),
            },
            "audit_integrity": _integrity_dict(
                integrity.status,
                integrity.checked_events,
                integrity.first_bad_sequence,
            ),
            "job_event_integrity": _integrity_dict(
                event_integrity.status,
                event_integrity.checked_events,
                event_integrity.first_bad_sequence,
            ),
        },
    )
    return payload, card, health, integrity, state_present


def _bundle(
    board: DurableJobBoard,
    owner: int,
    chat: int,
    job_id: str,
    *,
    output_format: str,
    max_bytes: int,
    require_state: bool,
    strict_integrity: bool,
) -> int:
    if not 4_000 <= max_bytes <= 32_000:
        raise ValueError("bundle output size must be between 4000 and 32000 bytes")
    now = time.time()
    inspect, _card, _health, integrity, state_present = _read_inspect_snapshot(
        board,
        owner,
        chat,
        job_id,
        now=now,
    )
    event_integrity = board.verify_job_event_integrity()
    inspect = validate_inspect_payload(
        inspect,
        owner_user_id=owner,
        chat_id=chat,
        job_id=job_id,
        max_bytes=max_bytes,
        strict_integrity=strict_integrity,
        now=now,
    )
    bundle = snapshot_bundle_payload(inspect, max_bytes=max_bytes)
    if output_format == "text":
        manifest = cast(Mapping[str, object], bundle["manifest"])
        print(
            f"jobs bundle scope=owner:{owner}/chat:{chat} id={job_id.casefold()} "
            f"bytes={manifest['snapshot_bytes']} fingerprint={manifest['fingerprint']} "
            f"audit={integrity.status}"
        )
    else:
        _print_json(bundle)
    return _exit_code(
        state_present,
        integrity,
        event_integrity=event_integrity,
        require_state=require_state,
        strict_integrity=strict_integrity,
    )


def _diff(
    board: DurableJobBoard,
    owner: int,
    chat: int,
    job_id: str,
    previous_source: str,
    *,
    output_format: str,
    max_bytes: int,
    max_age_seconds: float | None,
    require_state: bool,
    strict_integrity: bool,
) -> int:
    """Compare one current card with a caller-supplied safe inspect snapshot.

    The previous snapshot is deliberately an input, rather than a second
    durable store.  This keeps the operation read-only and makes the boundary
    auditable: malformed, cross-scope, or tampered input is rejected before a
    current card is rendered.
    """

    if not 4_000 <= max_bytes <= 32_000:
        raise ValueError("diff input/output size must be between 4000 and 32000 bytes")
    normalized_id = job_id.casefold()
    previous = _load_previous_inspect(
        previous_source,
        owner=owner,
        chat=chat,
        job_id=normalized_id,
        max_bytes=max_bytes,
        strict_integrity=strict_integrity,
        max_age_seconds=max_age_seconds,
        now=time.time(),
    )
    now = time.time()
    current, _card, _health, integrity, state_present = _read_inspect_snapshot(
        board,
        owner,
        chat,
        normalized_id,
        now=now,
    )
    event_integrity = board.verify_job_event_integrity()
    current = validate_inspect_payload(
        current,
        owner_user_id=owner,
        chat_id=chat,
        job_id=normalized_id,
        max_bytes=max_bytes,
        strict_integrity=strict_integrity,
        max_age_seconds=max_age_seconds,
        now=now,
    )
    current_integrity = _integrity_from_payload(current)
    previous_integrity = _integrity_from_payload(previous)
    changes = metadata_diff(previous, current)
    payload = snapshot_payload(
        "jobs_diff",
        "owner-chat",
        {
            "owner_user_id": owner,
            "chat_id": chat,
            "job_id": normalized_id,
            "changed": bool(changes),
            "changes": changes,
            "current_integrity": current_integrity,
            "previous_integrity": previous_integrity,
            "current_fingerprint": current.get("fingerprint"),
            "previous_fingerprint": previous.get("fingerprint"),
            "current_job_event_integrity": current.get("job_event_integrity"),
            "previous_job_event_integrity": previous.get("job_event_integrity"),
        },
        max_bytes=max_bytes,
    )
    if output_format == "text":
        print(
            _diff_text(
                owner,
                chat,
                normalized_id,
                changes,
                integrity.status,
                str(previous_integrity["status"]),
            )
        )
    else:
        _print_json(payload)
    return _exit_code(
        state_present,
        integrity,
        event_integrity=event_integrity,
        require_state=require_state,
        strict_integrity=strict_integrity,
    )


def _validate_snapshot(
    owner: int,
    chat: int,
    job_id: str,
    source: str,
    *,
    output_format: str,
    max_bytes: int,
    strict_integrity: bool,
    max_age_seconds: float | None,
) -> int:
    if not 4_000 <= max_bytes <= 32_000:
        raise ValueError("snapshot input/output size must be between 4000 and 32000 bytes")
    raw = _read_bounded_previous_source(source, max_bytes=max_bytes)
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as error:
        raise SnapshotError("inspect snapshot is not valid JSON") from error
    if not isinstance(decoded, Mapping):
        raise SnapshotError("inspect snapshot must be a JSON object")
    validated = _decode_inspect_document(
        decoded,
        owner=owner,
        chat=chat,
        job_id=job_id,
        max_bytes=max_bytes,
        strict_integrity=strict_integrity,
        max_age_seconds=max_age_seconds,
        now=time.time(),
    )
    integrity = _integrity_from_payload(validated)
    payload = snapshot_payload(
        "jobs_validate",
        "owner-chat",
        {
            "owner_user_id": owner,
            "chat_id": chat,
            "job_id": job_id.casefold(),
            "valid": True,
            "integrity": integrity,
            "fingerprint": validated.get("fingerprint"),
            **(
                {"job_event_integrity": validated["job_event_integrity"]}
                if "job_event_integrity" in validated
                else {}
            ),
        },
        max_bytes=max_bytes,
    )
    if output_format == "text":
        print(
            f"jobs validate scope=owner:{owner}/chat:{chat} "
            f"id={job_id.casefold()} valid=true audit={integrity['status']}"
        )
    else:
        _print_json(payload)
    return EXIT_OK


def _validate_events(
    owner: int,
    chat: int,
    job_id: str | None,
    source: str,
    *,
    output_format: str,
    max_bytes: int,
    max_age_seconds: float | None,
    expected_integrity: str | None,
    strict_integrity: bool,
) -> int:
    """Validate a saved job-event envelope without touching durable state."""

    if not 4_000 <= max_bytes <= 32_000:
        raise ValueError("event audit input/output size must be between 4000 and 32000 bytes")
    raw = _read_bounded_previous_source(source, max_bytes=max_bytes)
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as error:
        raise SnapshotError("job-event audit is not valid JSON") from error
    if not isinstance(decoded, Mapping) or decoded.get("kind") != "jobs_events":
        raise SnapshotError("validate-events requires a jobs_events envelope")
    validated = validate_job_event_audit_payload(
        decoded,
        owner_user_id=owner,
        chat_id=chat,
        job_id=job_id,
        max_bytes=max_bytes,
        strict_integrity=strict_integrity,
        max_age_seconds=max_age_seconds,
        now=time.time(),
    )
    integrity = validated.get("integrity")
    if not isinstance(integrity, Mapping):
        raise SnapshotError("job-event audit integrity is missing")
    actual_status = integrity.get("status")
    if not isinstance(actual_status, str):
        raise SnapshotError("job-event audit integrity status is malformed")
    valid = expected_integrity is None or actual_status == expected_integrity
    result = job_event_validation_payload(
        validated,
        owner_user_id=owner,
        chat_id=chat,
        job_id=job_id,
        valid=valid,
        expected_integrity=expected_integrity,
        max_age_seconds=max_age_seconds,
        max_bytes=max_bytes,
    )
    if output_format == "text":
        print(
            f"jobs validate-events scope=owner:{owner}/chat:{chat} "
            f"job={'all' if job_id is None else job_id.casefold()} "
            f"valid={str(valid).lower()} integrity={actual_status}"
        )
    else:
        _print_json(result)
    return EXIT_OK if valid else EXIT_DATA_ERROR


def _validate_contract(
    owner: int,
    chat: int,
    source: str,
    *,
    output_format: str,
    max_bytes: int,
    strict_integrity: bool,
) -> int:
    """Validate a saved cross-adapter contract without touching durable state."""

    if not 4_000 <= max_bytes <= 32_000:
        raise ValueError("contract input/output size must be between 4000 and 32000 bytes")
    raw = _read_bounded_previous_source(source, max_bytes=max_bytes)
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as error:
        raise SnapshotError("jobs contract-check is not valid JSON") from error
    if not isinstance(decoded, Mapping) or decoded.get("kind") != "jobs_contract_check":
        raise SnapshotError("validate-contract requires a jobs_contract_check envelope")
    if decoded.get("scope") != "owner-chat":
        raise SnapshotError("jobs contract-check scope is malformed")
    result = validate_contract_check_payload(
        decoded,
        max_bytes=max_bytes,
        strict_integrity=strict_integrity,
    )
    valid = result.get("valid")
    status = result.get("status")
    if type(valid) is not bool or not isinstance(status, str):
        raise SnapshotError("jobs contract validation result is malformed")
    if output_format == "text":
        adapters = ",".join(str(item) for item in cast(list[object], result["adapters"]))
        drift = ",".join(
            str(item) for item in cast(list[object], result["drift_kinds"])
        ) or "-"
        print(
            f"jobs validate-contract scope=owner:{owner}/chat:{chat} "
            f"valid={str(valid).lower()} status={status} adapters={adapters} drift={drift}"
        )
    else:
        _print_json(result)
    return EXIT_OK if valid else EXIT_DATA_ERROR


def _verify_bundle(
    owner: int,
    chat: int,
    job_id: str,
    source: str,
    expected_fingerprint: str,
    *,
    state_root: Path,
    output_format: str,
    max_bytes: int,
    max_age_seconds: float | None,
    strict_integrity: bool,
    record: bool,
) -> int:
    """Verify a bundle; append history only when ``record`` is explicit."""

    if not 4_000 <= max_bytes <= 32_000:
        raise ValueError("verify input/output size must be between 4000 and 32000 bytes")
    raw = _read_bounded_previous_source(source, max_bytes=max_bytes)
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as error:
        raise SnapshotError("bundle is not valid JSON") from error
    if not isinstance(decoded, Mapping) or decoded.get("kind") != "jobs_bundle":
        raise SnapshotError("verify requires a jobs_bundle envelope")
    # Validate the complete envelope first.  This is state-free and ensures a
    # malformed or cross-scope upload can never create an audit record.
    validated = validate_snapshot_bundle(
        decoded,
        owner_user_id=owner,
        chat_id=chat,
        job_id=job_id,
        max_bytes=max_bytes,
        strict_integrity=strict_integrity,
        max_age_seconds=max_age_seconds,
        now=time.time(),
    )
    integrity = _integrity_from_payload(validated)
    bundle_fingerprint = decoded.get("fingerprint")
    if not isinstance(bundle_fingerprint, str):
        raise SnapshotError("verified bundle fingerprint is missing")
    accepted = fingerprint_matches(expected_fingerprint, bundle_fingerprint)
    recorded = False
    audit_event_sequence: int | None = None
    recorded_event_integrity: dict[str, object] | None = None
    if record:
        board = DurableJobBoard(state_root)
        event = board.record_verification(
            owner,
            chat,
            job_id,
            expected_fingerprint=expected_fingerprint,
            actual_fingerprint=bundle_fingerprint,
            outcome="accepted" if accepted else "rejected",
        )
        recorded = True
        audit_event_sequence = event.sequence
        event_integrity = board.verify_job_event_integrity()
        recorded_event_integrity = _integrity_dict(
            event_integrity.status,
            event_integrity.checked_events,
            event_integrity.first_bad_sequence,
        )
    elif not accepted:
        raise SnapshotError("bundle fingerprint does not match expected value")
    payload = snapshot_payload(
        "jobs_verify",
        "owner-chat",
        {
            "owner_user_id": owner,
            "chat_id": chat,
            "job_id": job_id.casefold(),
            "valid": accepted,
            "outcome": "accepted" if accepted else "rejected",
            "bundle_fingerprint": bundle_fingerprint,
            "expected_fingerprint": expected_fingerprint,
            "integrity": integrity,
            **({"recorded": recorded} if record else {}),
            **(
                {"audit_event_sequence": audit_event_sequence}
                if audit_event_sequence is not None
                else {}
            ),
            **(
                {"job_event_integrity": recorded_event_integrity}
                if recorded_event_integrity is not None
                else {}
            ),
        },
        max_bytes=max_bytes,
    )
    if output_format == "text":
        print(
            f"jobs verify scope=owner:{owner}/chat:{chat} id={job_id.casefold()} "
            f"valid={str(accepted).lower()} outcome={'accepted' if accepted else 'rejected'} "
            f"fingerprint={bundle_fingerprint} audit={integrity['status']}"
            + (f" recorded={str(recorded).lower()}" if record else "")
        )
    else:
        _print_json(payload)
    return EXIT_OK if accepted else EXIT_DATA_ERROR


def _load_previous_inspect(
    source: str,
    *,
    owner: int,
    chat: int,
    job_id: str,
    max_bytes: int,
    strict_integrity: bool,
    max_age_seconds: float | None,
    now: float,
) -> dict[str, object]:
    raw = _read_bounded_previous_source(source, max_bytes=max_bytes)
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as error:
        raise SnapshotError("previous inspect snapshot is not valid JSON") from error
    if not isinstance(decoded, Mapping):
        raise SnapshotError("previous inspect snapshot must be a JSON object")
    return _decode_inspect_document(
        decoded,
        owner=owner,
        chat=chat,
        job_id=job_id,
        max_bytes=max_bytes,
        strict_integrity=strict_integrity,
        max_age_seconds=max_age_seconds,
        now=now,
    )


def _decode_inspect_document(
    decoded: Mapping[str, object],
    *,
    owner: int,
    chat: int,
    job_id: str,
    max_bytes: int,
    strict_integrity: bool,
    max_age_seconds: float | None,
    now: float,
) -> dict[str, object]:
    if decoded.get("kind") == "jobs_bundle":
        return validate_snapshot_bundle(
            decoded,
            owner_user_id=owner,
            chat_id=chat,
            job_id=job_id,
            max_bytes=max_bytes,
            strict_integrity=strict_integrity,
            max_age_seconds=max_age_seconds,
            now=now,
        )
    return validate_inspect_payload(
        decoded,
        owner_user_id=owner,
        chat_id=chat,
        job_id=job_id,
        max_bytes=max_bytes,
        strict_integrity=strict_integrity,
        max_age_seconds=max_age_seconds,
        now=now,
    )


def _read_bounded_previous_source(source: str, *, max_bytes: int) -> str:
    if source == "-":
        text = sys.stdin.read(max_bytes + 1)
        raw = text.encode("utf-8")
    else:
        path = Path(source).expanduser()
        if path.is_symlink() or not path.is_file():
            raise SnapshotError("previous inspect snapshot file is unavailable")
        if any(part.casefold() in {".xclaude", ".dsh"} for part in path.parts):
            raise SnapshotError("previous inspect snapshot path is outside XDSH scope")
        try:
            if path.stat().st_size > max_bytes:
                raise SnapshotError("previous inspect snapshot exceeds its byte limit")
            raw = path.read_bytes()
        except OSError as error:
            raise SnapshotError("previous inspect snapshot file is unreadable") from error
    if len(raw) > max_bytes:
        raise SnapshotError("previous inspect snapshot exceeds its byte limit")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SnapshotError("previous inspect snapshot must be UTF-8") from error


def _integrity_from_payload(payload: Mapping[str, object]) -> dict[str, object]:
    integrity = payload.get("audit_integrity")
    if not isinstance(integrity, Mapping):
        raise SnapshotError("inspect snapshot integrity is missing")
    return dict(integrity)


def _diff_text(
    owner: int,
    chat: int,
    job_id: str,
    changes: Mapping[str, object],
    current_status: str,
    previous_status: str,
) -> str:
    fields = ",".join(sorted(changes)) or "none"
    return (
        f"jobs diff scope=owner:{owner}/chat:{chat} id={job_id} "
        f"changed={str(bool(changes)).lower()} fields={fields} "
        f"audit={current_status}/{previous_status}"
    )


def _self_check(
    board: DurableJobBoard,
    owner: int,
    chat: int,
    *,
    output_format: str,
    require_state: bool,
    strict_integrity: bool,
) -> int:
    state_present = board.path.is_file()
    projection = board.projection(owner, chat, limit=12)
    integrity = board.verify_interaction_integrity()
    event_integrity = board.verify_job_event_integrity()
    health = board.health_summary(
        now=time.time(),
        owner_user_id=owner,
        chat_id=chat,
    )
    integrity_payload = _integrity_dict(
        integrity.status,
        integrity.checked_events,
        integrity.first_bad_sequence,
    )
    event_integrity_payload = _integrity_dict(
        event_integrity.status,
        event_integrity.checked_events,
        event_integrity.first_bad_sequence,
    )
    shared = {
        "owner_user_id": owner,
        "chat_id": chat,
        "state_present": state_present,
        "audit_integrity": integrity_payload,
        "job_event_integrity": event_integrity_payload,
    }
    cli_board = snapshot_payload(
        "jobs_board",
        "owner-chat",
        {**shared, **job_projection_fields(projection)},
    )
    telegram_board = snapshot_payload(
        "jobs_board",
        "owner-chat",
        {**shared, **job_projection_fields(projection)},
    )
    interactions = board.interaction_health(owner, chat)
    running_stale = (
        health.oldest_running_age_seconds is not None
        and health.oldest_running_age_seconds > DEFAULT_RUNNING_WARNING_SECONDS
    )
    pending_stale = (
        health.oldest_pending_interaction_age_seconds is not None
        and health.oldest_pending_interaction_age_seconds
        > DEFAULT_PENDING_INTERACTION_WARNING_SECONDS
    )
    stale_status = "attention" if running_stale or pending_stale else "normal"
    health_counts = dict(health.counts)
    stale_policy = {
        "status": stale_status,
        "running_jobs": health_counts.get("running", 0),
        "pending_jobs": health_counts.get("queued", 0) + health_counts.get("blocked", 0),
        "running_warning_seconds": DEFAULT_RUNNING_WARNING_SECONDS,
        "pending_interaction_warning_seconds": DEFAULT_PENDING_INTERACTION_WARNING_SECONDS,
        "oldest_running_age_seconds": health.oldest_running_age_seconds,
        "oldest_pending_interaction_age_seconds": health.oldest_pending_interaction_age_seconds,
        "running_stale": running_stale,
        "pending_interaction_stale": pending_stale,
    }
    stats_fields = {
        **shared,
        "total": projection.total,
        "counts": {key: value for key, value in projection.counts},
        "pending_interactions": interactions.pending,
        "oldest_pending_interaction_age_seconds": interactions.oldest_age_seconds,
    }
    cli_stats = snapshot_payload("jobs_stats", "owner-chat", stats_fields)
    telegram_stats = snapshot_payload("jobs_stats", "owner-chat", stats_fields)
    probes = snapshot_contract_probe_set(
        owner_user_id=owner,
        chat_id=chat,
        observed_at=time.time(),
    )
    events_payload = job_event_audit_payload(
        board.job_event_audit(owner, chat, limit=12),
        owner_user_id=owner,
        chat_id=chat,
        generated_at=time.time(),
    )
    cli_payloads = dict(probes)
    telegram_payloads = dict(probes)
    cli_payloads["jobs_board"] = cli_board
    cli_payloads["jobs_stats"] = cli_stats
    telegram_payloads["jobs_board"] = telegram_board
    telegram_payloads["jobs_stats"] = telegram_stats
    cli_payloads["jobs_events"] = events_payload
    cli_payloads["jobs_events_validate"] = job_event_validation_payload(
        events_payload,
        owner_user_id=owner,
        chat_id=chat,
        job_id=None,
        valid=True,
    )
    telegram_payloads["jobs_events"] = events_payload
    telegram_payloads["jobs_events_validate"] = job_event_validation_payload(
        events_payload,
        owner_user_id=owner,
        chat_id=chat,
        job_id=None,
        valid=True,
    )
    contract_report = snapshot_contract_report(
        {"cli": cli_payloads, "telegram": telegram_payloads}
    )
    checks = contract_report["checks"]
    if not isinstance(checks, Mapping):
        raise SnapshotError("snapshot contract report checks are malformed")
    checks = dict(checks)
    # Preserve the established short keys for shell consumers while exposing
    # the full registered kind names alongside them.
    if "jobs_board" in checks:
        checks["board"] = checks["jobs_board"]
    if "jobs_stats" in checks:
        checks["stats"] = checks["jobs_stats"]
    registry = contract_report["registry"]
    if not isinstance(registry, Mapping):
        raise SnapshotError("snapshot contract report registry is malformed")
    contract_status = contract_report.get("status")
    if not isinstance(contract_status, str):
        raise SnapshotError("snapshot contract report status is malformed")
    board_check = checks.get("jobs_board")
    stats_check = checks.get("jobs_stats")
    if not isinstance(board_check, Mapping) or not isinstance(stats_check, Mapping):
        raise SnapshotError("snapshot contract report is missing board/stats checks")
    board_status = str(board_check.get("status"))
    stats_status = str(stats_check.get("status"))
    payload = snapshot_payload(
        "jobs_contract_check",
        "owner-chat",
        {
            "state_present": state_present,
            "audit_integrity": integrity_payload,
            "stale_policy": stale_policy,
            "checks": checks,
            "registry": registry,
            "job_event_integrity": event_integrity_payload,
        },
    )
    if output_format == "text":
        print(
            f"jobs self-check scope=owner:{owner}/chat:{chat} "
            f"state={str(state_present).lower()} audit={integrity.status} "
            f"board={board_status} stats={stats_status} "
            f"snapshots={contract_status} stale={stale_status}"
        )
    else:
        _print_json(payload)
    if contract_status != "pass":
        return EXIT_DATA_ERROR
    return _exit_code(
        state_present,
        integrity,
        event_integrity=event_integrity,
        require_state=require_state,
        strict_integrity=strict_integrity,
    )


def _follow(
    board: DurableJobBoard,
    owner: int,
    chat: int,
    *,
    status: JobStatus | None,
    kind: JobKind | None,
    limit: int,
    interval: float,
    samples: int | None,
    duration: float | None,
    max_bytes: int,
    emit_unchanged: bool,
    require_state: bool,
    strict_integrity: bool,
) -> int:
    if not 0.05 <= interval <= 60.0:
        raise ValueError("follow interval must be between 0.05 and 60 seconds")
    if samples is None and duration is None:
        raise ValueError("follow requires samples or duration")
    if samples is not None and not 1 <= samples <= 100:
        raise ValueError("follow samples must be between 1 and 100")
    if samples is not None and (samples - 1) * interval > _MAX_FOLLOW_RUNTIME_SECONDS:
        raise ValueError("follow sample window must be at most 3600 seconds")
    if duration is not None and not 0.05 <= duration <= 3_600.0:
        raise ValueError("follow duration must be between 0.05 and 3600 seconds")
    if not 4_000 <= max_bytes <= 32_000:
        raise ValueError("follow output size must be between 4000 and 32000 bytes")

    started = time.monotonic()
    previous_fingerprint: str | None = None
    bytes_written = 0
    observations = 0
    sample_number = 0
    latest_integrity = JobInteractionIntegrity(status="empty")
    latest_event_integrity = JobEventIntegrity(status="empty")
    latest_state_present = board.path.is_file()
    while True:
        observations += 1
        latest_state_present = board.path.is_file()
        projection = board.projection(
            owner,
            chat,
            status=status,
            kind=kind,
            limit=limit,
        )
        latest_integrity = board.verify_interaction_integrity()
        latest_event_integrity = board.verify_job_event_integrity()
        base = snapshot_payload(
            "jobs_snapshot",
            "owner-chat",
            {
                "owner_user_id": owner,
                "chat_id": chat,
                "state_present": latest_state_present,
                "audit_integrity": _integrity_dict(
                    latest_integrity.status,
                    latest_integrity.checked_events,
                    latest_integrity.first_bad_sequence,
                ),
                "job_event_integrity": _integrity_dict(
                    latest_event_integrity.status,
                    latest_event_integrity.checked_events,
                    latest_event_integrity.first_bad_sequence,
                ),
                **job_projection_fields(projection),
            },
            max_bytes=128_000,
        )
        fingerprint = json.dumps(
            _snapshot_fingerprint(base),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        changed = fingerprint != previous_fingerprint
        previous_fingerprint = fingerprint
        if changed or emit_unchanged:
            payload = dict(base)
            sample_number += 1
            payload["sample"] = sample_number
            payload["observed_at"] = time.time()
            payload["changed"] = changed
            line = _bounded_snapshot_line(payload, max_bytes - bytes_written)
            if line is None:
                break
            print(line)
            bytes_written += len(line.encode("utf-8")) + 1
        if samples is not None and observations >= samples:
            break
        if duration is not None and time.monotonic() - started >= duration:
            break
        sleep_seconds = interval
        if duration is not None:
            sleep_seconds = min(
                interval,
                max(0.0, duration - (time.monotonic() - started)),
            )
        if sleep_seconds <= 0:
            break
        try:
            time.sleep(sleep_seconds)
        except KeyboardInterrupt:
            return 130
        if duration is not None and time.monotonic() - started >= duration:
            break
        if samples is not None and observations >= samples:
            break
    return _exit_code(
        latest_state_present,
        latest_integrity,
        event_integrity=latest_event_integrity,
        require_state=require_state,
        strict_integrity=strict_integrity,
    )


def _bounded_snapshot_line(
    payload: dict[str, object],
    remaining_bytes: int,
) -> str | None:
    cards = list(cast(list[dict[str, object]], payload["cards"]))
    base_truncated = bool(payload["truncated"])
    while True:
        candidate = dict(payload)
        candidate["cards"] = cards
        candidate["truncated"] = base_truncated or len(cards) < len(
            cast(list[dict[str, object]], payload["cards"])
        )
        rendered = json.dumps(
            candidate,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(rendered.encode("utf-8")) + 1 <= remaining_bytes:
            return rendered
        if not cards:
            return None
        cards.pop()


def _snapshot_fingerprint(payload: dict[str, object]) -> dict[str, object]:
    """Remove clock-only fields so an unchanged board does not spam output."""

    fingerprint = dict(payload)
    raw_cards = cast(list[dict[str, object]], payload["cards"])
    fingerprint["cards"] = [
        {
            key: value
            for key, value in card.items()
            if key not in {"age_seconds", "lease_remaining_seconds"}
        }
        for card in raw_cards
    ]
    return fingerprint


def _exit_code(
    state_present: bool,
    integrity: JobInteractionIntegrity,
    *,
    event_integrity: JobEventIntegrity | None = None,
    require_state: bool,
    strict_integrity: bool,
) -> int:
    if not state_present and require_state:
        return EXIT_EMPTY_STATE
    if integrity.status == "tampered":
        return EXIT_DATA_ERROR
    if strict_integrity and integrity.status == "legacy_unverified":
        return EXIT_DATA_ERROR
    if event_integrity is not None and event_integrity.status == "tampered":
        return EXIT_DATA_ERROR
    if (
        event_integrity is not None
        and strict_integrity
        and event_integrity.status == "legacy_unverified"
    ):
        return EXIT_DATA_ERROR
    return EXIT_OK


def _board_text(
    projection: JobBoardProjection,
    integrity: JobInteractionIntegrity,
    owner: int,
    chat: int,
) -> str:
    lines = [
        (
            f"jobs board scope=owner:{owner}/chat:{chat} total={projection.total} "
            f"shown={len(projection.cards)} truncated={str(projection.truncated).lower()} "
            f"audit={integrity.status}"
        )
    ]
    if not projection.cards:
        lines.append("(empty)")
        return "\n".join(lines)
    for card in projection.cards:
        lines.append(_card_text(card))
    return "\n".join(lines)


def _card_text(card: JobCard) -> str:
    lease = "-" if card.lease_remaining_seconds is None else f"{card.lease_remaining_seconds:.1f}s"
    return (
        f"{card.id} kind={card.kind} status={card.status} attempts={card.attempts} "
        f"age={card.age_seconds:.1f}s lease={lease} deps={card.dependency_count} "
        f"pending={card.pending_interaction_count} cancel={str(card.cancel_requested).lower()} "
        f"partial_chars={card.partial_characters} result_chars={card.result_characters}"
    )


def _stats_text(
    projection: JobBoardProjection,
    interactions: JobInteractionHealthSummary,
    integrity: JobInteractionIntegrity,
    owner: int,
    chat: int,
) -> str:
    counts = ",".join(f"{status}={count}" for status, count in projection.counts) or "none"
    oldest = (
        "-"
        if interactions.oldest_age_seconds is None
        else f"{interactions.oldest_age_seconds:.1f}s"
    )
    return (
        f"jobs stats scope=owner:{owner}/chat:{chat} total={projection.total} "
        f"counts={counts} pending_interactions={interactions.pending} "
        f"oldest_pending_age={oldest} audit={integrity.status}"
    )


def _job_events_text(
    owner: int,
    chat: int,
    job_id: str | None,
    audit: JobEventAudit,
) -> str:
    scope = "all" if job_id is None else job_id.casefold()
    lines = [
        f"jobs events scope=owner:{owner}/chat:{chat} job={scope} "
        f"total={audit.total} shown={len(audit.events)} "
        f"truncated={str(audit.truncated).lower()} "
        f"integrity={audit.integrity.status}"
    ]
    for event in audit.events:
        lines.append(
            f"{event.sequence} job={event.job_id} action={event.action} "
            f"status={event.status} actor={event.actor} created_at={event.created_at:.3f}"
        )
    if not audit.events:
        lines.append("(empty)")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
