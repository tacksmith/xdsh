"""Shared bounded envelopes for content-free operational snapshots.

Telegram, the reconciler, and the operator CLI all observe the same state but
must not grow subtly different serialization or disclosure rules.  This module
owns the small, runner-neutral seam: stable schema headers, finite JSON values,
size limits, and rejection of task/result/credential fields in content-free
views.  It never reads or writes a state store.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections.abc import Mapping, Sequence
from typing import Final, Literal

from xdsh.core.jobs import JobBoardProjection, JobCard, JobEventAudit

SNAPSHOT_SCHEMA_VERSION: Final = 1
DEFAULT_SNAPSHOT_MAX_BYTES: Final = 32_000
SnapshotKind = Literal[
    "jobs_board",
    "jobs_stats",
    "jobs_snapshot",
    "jobs_contract_check",
    "jobs_contract_validate",
    "jobs_inspect",
    "jobs_diff",
    "jobs_validate",
    "jobs_bundle",
    "jobs_verify",
    "jobs_events",
    "jobs_events_validate",
    "health_report",
]

_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_FINGERPRINT = re.compile(r"^sha256:[0-9a-f]{64}$")
_FORBIDDEN_CONTENT_KEYS = frozenset(
    {
        "answer",
        "api_key",
        "credential",
        "error_text",
        "error",
        "lease_token",
        "model",
        "password",
        "prompt",
        "result",
        "result_text",
        "runner_session_id",
        "secret",
        "session_id",
        "task",
        "text",
        "token",
        "tool_args",
        "tool_arguments",
    }
)
_FORBIDDEN_ALWAYS_KEYS = frozenset(
    {"api_key", "credential", "lease_token", "password", "secret", "token"}
)
_CONTRACT_REQUIRED: dict[str, frozenset[str]] = {
    "jobs_board": frozenset(
        {
            "schema_version",
            "kind",
            "scope",
            "state_present",
            "audit_integrity",
            "total",
            "truncated",
            "counts",
            "cards",
        }
    ),
    "jobs_stats": frozenset(
        {
            "schema_version",
            "kind",
            "scope",
            "state_present",
            "total",
            "counts",
            "pending_interactions",
            "oldest_pending_interaction_age_seconds",
            "audit_integrity",
        }
    ),
    "jobs_snapshot": frozenset(
        {
            "schema_version",
            "kind",
            "scope",
            "owner_user_id",
            "chat_id",
            "state_present",
            "audit_integrity",
            "total",
            "truncated",
            "counts",
            "cards",
        }
    ),
    "jobs_contract_check": frozenset(
        {
            "schema_version",
            "kind",
            "scope",
            "state_present",
            "audit_integrity",
            "stale_policy",
            "checks",
            "registry",
        }
    ),
    "jobs_contract_validate": frozenset(
        {
            "schema_version",
            "kind",
            "scope",
            "valid",
            "status",
            "registry_version",
            "adapter_count",
            "adapters",
            "checked_kinds",
            "drift_kinds",
        }
    ),
    "jobs_inspect": frozenset(
        {
            "schema_version",
            "kind",
            "scope",
            "owner_user_id",
            "chat_id",
            "state_present",
            "job_id",
            "card",
            "health",
            "audit_integrity",
        }
    ),
    "jobs_diff": frozenset(
        {
            "schema_version",
            "kind",
            "scope",
            "owner_user_id",
            "chat_id",
            "job_id",
            "changed",
            "changes",
            "current_integrity",
            "previous_integrity",
        }
    ),
    "jobs_validate": frozenset(
        {
            "schema_version",
            "kind",
            "scope",
            "owner_user_id",
            "chat_id",
            "job_id",
            "valid",
            "integrity",
        }
    ),
    "jobs_bundle": frozenset(
        {
            "schema_version",
            "kind",
            "scope",
            "owner_user_id",
            "chat_id",
            "job_id",
            "snapshot",
            "manifest",
        }
    ),
    "jobs_verify": frozenset(
        {
            "schema_version",
            "kind",
            "scope",
            "owner_user_id",
            "chat_id",
            "job_id",
            "valid",
            "outcome",
            "bundle_fingerprint",
            "expected_fingerprint",
            "integrity",
        }
    ),
    "jobs_events": frozenset(
        {
            "schema_version",
            "kind",
            "scope",
            "owner_user_id",
            "chat_id",
            "job_id",
            "generated_at",
            "total",
            "omitted",
            "truncated",
            "integrity",
            "events",
        }
    ),
    "jobs_events_validate": frozenset(
        {
            "schema_version",
            "kind",
            "scope",
            "owner_user_id",
            "chat_id",
            "job_id",
            "valid",
            "integrity",
            "total",
            "truncated",
        }
    ),
    "health_report": frozenset(
        {"schema_version", "checked_at", "overall", "components", "actions", "trends"}
    ),
}
_CONTRACT_OPTIONAL: dict[str, frozenset[str]] = {
    "jobs_board": frozenset({"owner_user_id", "chat_id", "job_event_integrity"}),
    "jobs_stats": frozenset({"owner_user_id", "chat_id", "job_event_integrity"}),
    "jobs_snapshot": frozenset(
        {"sample", "observed_at", "changed", "job_event_integrity"}
    ),
    "jobs_contract_check": frozenset({"job_event_integrity"}),
    "jobs_contract_validate": frozenset(),
    "jobs_inspect": frozenset({"observed_at", "fingerprint", "job_event_integrity"}),
    "jobs_diff": frozenset(
        {
            "current_fingerprint",
            "previous_fingerprint",
            "current_job_event_integrity",
            "previous_job_event_integrity",
        }
    ),
    "jobs_validate": frozenset({"fingerprint", "job_event_integrity"}),
    "jobs_bundle": frozenset({"fingerprint"}),
    "jobs_verify": frozenset(
        {"recorded", "audit_event_sequence", "job_event_integrity"}
    ),
    "jobs_events": frozenset({"fingerprint"}),
    "jobs_events_validate": frozenset(
        {"expected_integrity", "max_age_seconds", "fingerprint"}
    ),
    "health_report": frozenset({"kind", "scope"}),
}
SNAPSHOT_CONTRACT_KINDS: tuple[str, ...] = tuple(_CONTRACT_REQUIRED)
_JOB_ID = re.compile(r"^[a-f0-9]{8}$")
_INSPECT_CARD_FIELDS = frozenset(
    {
        "id",
        "kind",
        "status",
        "attempts",
        "created_at",
        "updated_at",
        "scheduled_for",
        "lease_remaining_seconds",
        "cancel_requested",
        "dependency_count",
        "pending_interaction_count",
        "partial_characters",
        "result_characters",
        "age_seconds",
    }
)
_INSPECT_HEALTH_FIELDS = frozenset(
    {
        "counts",
        "running_expired",
        "oldest_running_age_seconds",
        "pending_interactions",
        "oldest_pending_interaction_age_seconds",
    }
)
_INTEGRITY_FIELDS = frozenset({"status", "checked_events", "first_bad_sequence"})
_JOB_EVENT_AUDIT_EVENT_FIELDS = frozenset(
    {"sequence", "job_id", "action", "status", "actor", "created_at"}
)
_JOB_EVENT_AUDIT_ACTIONS = frozenset(
    {
        "submitted",
        "leased",
        "heartbeat",
        "completed",
        "failed",
        "cancelled",
        "cancel_requested",
        "retry_requested",
        "recovered_unknown",
        "dependencies_completed",
        "dependency_manual_review",
        "expired",
        "resolved_completed",
        "resolved_failed",
        "bundle_verify:accepted",
        "bundle_verify:rejected",
        "bundle_verify:unknown",
        "other",
    }
)
_JOB_EVENT_AUDIT_ACTORS = frozenset(
    {"owner", "worker", "system", "schedule", "job", "other"}
)
_JOB_EVENT_AUDIT_STATUSES = frozenset(
    {
        "blocked",
        "queued",
        "running",
        "completed",
        "failed",
        "cancelled",
        "unknown",
        "manual_review",
    }
)
_BUNDLE_MANIFEST_FIELDS = frozenset(
    {"owner_user_id", "chat_id", "job_id", "snapshot_bytes", "fingerprint", "observed_at"}
)
_DIFF_FIELDS = (
    "state_present",
    "card_present",
    "status",
    "attempts",
    "lease_state",
    "cancel_requested",
    "dependency_count",
    "pending_interaction_count",
    "partial_characters",
    "result_characters",
    "health_counts",
    "running_expired",
    "pending_interactions",
    "audit_integrity",
    "job_event_integrity",
)
_CONTRACT_CHECK_ALIASES = {"board": "jobs_board", "stats": "jobs_stats"}
_CONTRACT_CHECK_STATUSES = frozenset({"pass", "drift"})
_CONTRACT_ADAPTER_FIELD_SUFFIX = "_fields"


class SnapshotError(ValueError):
    """A snapshot is invalid, unsafe, or exceeds its explicit bound."""


def snapshot_payload(
    kind: str,
    scope: str,
    fields: Mapping[str, object],
    *,
    max_bytes: int = DEFAULT_SNAPSHOT_MAX_BYTES,
    include_header: bool = True,
    content_free: bool = True,
) -> dict[str, object]:
    """Validate and return one deterministic bounded JSON-compatible payload.

    ``include_header=False`` is used by existing reports whose top-level keys
    are already a public compatibility contract.  The same validation still
    applies, so those reports share the exact field and size policy without an
    accidental key rewrite.
    """

    if _NAME.fullmatch(kind) is None or _NAME.fullmatch(scope) is None:
        raise SnapshotError("snapshot kind/scope is invalid")
    if not 1_024 <= max_bytes <= 128_000:
        raise SnapshotError("snapshot size must be between 1024 and 128000 bytes")
    payload = dict(fields)
    if include_header:
        if any(key in payload for key in ("schema_version", "kind", "scope")):
            raise SnapshotError("snapshot fields cannot override envelope headers")
        payload = {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "kind": kind,
            "scope": scope,
            **payload,
        }
    elif payload.get("schema_version", SNAPSHOT_SCHEMA_VERSION) != SNAPSHOT_SCHEMA_VERSION:
        raise SnapshotError("snapshot schema version is unsupported")
    _validate_value(payload, content_free=content_free, path="$")
    if kind in _CONTRACT_REQUIRED:
        validate_snapshot_contract(payload, kind, include_header=include_header)
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise SnapshotError("snapshot contains a non-JSON value") from error
    if len(encoded.encode("utf-8")) > max_bytes:
        raise SnapshotError("snapshot exceeds its byte limit")
    return payload


def snapshot_fingerprint(payload: Mapping[str, object]) -> str:
    """Return a deterministic content-free identifier for one envelope.

    The value is an identifier, not a signature, authorization token, or
    proof of origin.  It deliberately excludes an existing ``fingerprint``
    field so callers can verify or regenerate the same envelope.
    """

    if not isinstance(payload, Mapping):
        raise SnapshotError("snapshot fingerprint input must be an object")
    kind = payload.get("kind")
    scope = payload.get("scope")
    if not isinstance(kind, str) or not isinstance(scope, str):
        raise SnapshotError("snapshot fingerprint requires kind and scope")
    base = {
        key: value
        for key, value in payload.items()
        if key != "fingerprint"
    }
    fields = {
        key: value
        for key, value in base.items()
        if key not in {"schema_version", "kind", "scope"}
    }
    validated = snapshot_payload(
        kind,
        scope,
        fields,
        max_bytes=128_000,
    )
    if validated != base:
        raise SnapshotError("snapshot fingerprint input is malformed")
    encoded = _canonical_json(validated)
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def fingerprint_matches(expected: str, actual: str) -> bool:
    """Validate two canonical fingerprints and compare them exactly."""

    for label, value in (("expected", expected), ("actual", actual)):
        if not isinstance(value, str) or _FINGERPRINT.fullmatch(value) is None:
            raise SnapshotError(f"{label} fingerprint is malformed")
    return expected == actual


def fingerprinted_snapshot_payload(
    kind: str,
    scope: str,
    fields: Mapping[str, object],
    *,
    max_bytes: int = DEFAULT_SNAPSHOT_MAX_BYTES,
) -> dict[str, object]:
    """Build one bounded envelope and attach its canonical identifier."""

    base = snapshot_payload(kind, scope, fields, max_bytes=max_bytes)
    fingerprint = snapshot_fingerprint(base)
    return snapshot_payload(
        kind,
        scope,
        {**fields, "fingerprint": fingerprint},
        max_bytes=max_bytes,
    )


def _canonical_json(payload: Mapping[str, object]) -> bytes:
    try:
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise SnapshotError("snapshot contains a non-JSON value") from error


def snapshot_contract(
    kind: str,
    *,
    include_header: bool = True,
) -> tuple[frozenset[str], frozenset[str]]:
    """Return required and optional top-level fields for a known snapshot."""

    required = _CONTRACT_REQUIRED.get(kind)
    optional = _CONTRACT_OPTIONAL.get(kind)
    if required is None or optional is None:
        raise SnapshotError("snapshot kind has no registered contract")
    if include_header:
        if kind == "health_report":
            required = required | frozenset({"kind", "scope"})
        return required, optional
    return (
        frozenset(field for field in required if field not in {"kind", "scope"}),
        frozenset(field for field in optional if field not in {"kind", "scope"}),
    )


def validate_snapshot_contract(
    payload: Mapping[str, object],
    kind: str,
    *,
    include_header: bool = True,
) -> None:
    """Fail closed when a known consumer emits missing or unexpected fields."""

    required, optional = snapshot_contract(kind, include_header=include_header)
    actual = frozenset(payload)
    missing = required - actual
    unexpected = actual - required - optional
    if missing or unexpected:
        missing_text = ",".join(sorted(missing)) or "-"
        unexpected_text = ",".join(sorted(unexpected)) or "-"
        raise SnapshotError(
            f"snapshot contract drift for {kind}: missing={missing_text}; "
            f"unexpected={unexpected_text}"
        )
    if payload.get("schema_version", SNAPSHOT_SCHEMA_VERSION) != SNAPSHOT_SCHEMA_VERSION:
        raise SnapshotError("snapshot schema version is unsupported")
    if include_header and payload.get("kind") != kind:
        raise SnapshotError("snapshot kind header does not match contract")


def snapshot_contract_registry() -> dict[str, object]:
    """Return the versioned, machine-readable envelope contract registry.

    The registry is deliberately metadata-only.  It is safe to expose through
    an operator self-check because it contains field names and invariants, not
    task text, model identifiers, results, or credentials.
    """

    kinds: dict[str, object] = {}
    for kind in SNAPSHOT_CONTRACT_KINDS:
        required, optional = snapshot_contract(kind)
        kinds[kind] = {
            "required": sorted(required),
            "optional": sorted(optional),
            "top_level_fingerprint": (
                "optional"
                if kind in {"jobs_inspect", "jobs_bundle", "jobs_events"}
                else "reference"
                if kind == "jobs_validate"
                else "absent"
            ),
        }
    return {
        "version": 1,
        "kinds": kinds,
        "invariants": {
            "fingerprint": {
                "algorithm": "sha256",
                "prefix": "sha256:",
                "hex_length": 64,
                "encoding": "lowercase-hex",
                "self_field_excluded": True,
                "identity_only": True,
            },
            "bundle": {
                "nested_kind": "jobs_inspect",
                "manifest_fields": sorted(_BUNDLE_MANIFEST_FIELDS),
                "identity_fields": ["owner_user_id", "chat_id", "job_id"],
                "snapshot_bytes": "canonical_utf8_json",
                "nested_fingerprint_match": True,
            },
            "job_events": {
                "algorithm": "sha256",
                "sequence_included": True,
                "legacy_status": "legacy_unverified",
                "tamper_status": "tampered",
            },
        },
    }


def snapshot_contract_probe_set(
    *,
    owner_user_id: int = 7,
    chat_id: int = 42,
    job_id: str = "aaaa1111",
    observed_at: float = 1_000.0,
    max_bytes: int = DEFAULT_SNAPSHOT_MAX_BYTES,
) -> dict[str, dict[str, object]]:
    """Build deterministic, content-free examples for every registered kind."""

    normalized_id = job_id.casefold()
    integrity = {"status": "empty", "checked_events": 0, "first_bad_sequence": None}
    job_event_integrity = {
        "status": "empty",
        "checked_events": 0,
        "first_bad_sequence": None,
    }
    health = {
        "counts": {},
        "running_expired": 0,
        "oldest_running_age_seconds": None,
        "pending_interactions": 0,
        "oldest_pending_interaction_age_seconds": None,
    }
    inspect = fingerprinted_snapshot_payload(
        "jobs_inspect",
        "owner-chat",
        {
            "owner_user_id": owner_user_id,
            "chat_id": chat_id,
            "state_present": False,
            "observed_at": observed_at,
            "job_id": normalized_id,
            "card": None,
            "health": health,
            "audit_integrity": integrity,
            "job_event_integrity": job_event_integrity,
        },
        max_bytes=max_bytes,
    )
    derivatives = snapshot_contract_derivatives(inspect, max_bytes=max_bytes)
    board = snapshot_payload(
        "jobs_board",
        "owner-chat",
        {
            "owner_user_id": owner_user_id,
            "chat_id": chat_id,
            "state_present": False,
            "audit_integrity": integrity,
            "job_event_integrity": job_event_integrity,
            "total": 0,
            "truncated": False,
            "counts": {},
            "cards": [],
        },
        max_bytes=max_bytes,
    )
    stats = snapshot_payload(
        "jobs_stats",
        "owner-chat",
        {
            "owner_user_id": owner_user_id,
            "chat_id": chat_id,
            "state_present": False,
            "total": 0,
            "counts": {},
            "pending_interactions": 0,
            "oldest_pending_interaction_age_seconds": None,
            "audit_integrity": integrity,
            "job_event_integrity": job_event_integrity,
        },
        max_bytes=max_bytes,
    )
    jobs_snapshot = snapshot_payload(
        "jobs_snapshot",
        "owner-chat",
        {
            "owner_user_id": owner_user_id,
            "chat_id": chat_id,
            "state_present": False,
            "audit_integrity": integrity,
            "job_event_integrity": job_event_integrity,
            "total": 0,
            "truncated": False,
            "counts": {},
            "cards": [],
            "sample": 0,
            "observed_at": observed_at,
            "changed": False,
        },
        max_bytes=max_bytes,
    )
    contract_check = snapshot_payload(
        "jobs_contract_check",
        "owner-chat",
        {
            "state_present": False,
            "audit_integrity": integrity,
            "stale_policy": {
                "status": "normal",
                "running_jobs": 0,
                "pending_jobs": 0,
            },
            "checks": {},
            "registry": snapshot_contract_registry(),
        },
        max_bytes=max_bytes,
    )
    contract_validate = snapshot_payload(
        "jobs_contract_validate",
        "owner-chat",
        {
            "valid": True,
            "status": "pass",
            "registry_version": 1,
            "adapter_count": 2,
            "adapters": ["cli", "telegram"],
            "checked_kinds": len(SNAPSHOT_CONTRACT_KINDS),
            "drift_kinds": [],
        },
        max_bytes=max_bytes,
    )
    health_report = snapshot_payload(
        "health_report",
        "health",
        {
            "checked_at": observed_at,
            "overall": "healthy",
            "components": {},
            "actions": [],
            "trends": {},
        },
        max_bytes=max_bytes,
    )
    events = fingerprinted_snapshot_payload(
        "jobs_events",
        "owner-chat",
        {
            "owner_user_id": owner_user_id,
            "chat_id": chat_id,
            "job_id": normalized_id,
            "generated_at": observed_at,
            "total": 1,
            "omitted": 0,
            "truncated": False,
            "integrity": job_event_integrity,
            "events": [
                {
                    "sequence": 1,
                    "job_id": normalized_id,
                    "action": "submitted",
                    "status": "queued",
                    "actor": "owner",
                    "created_at": observed_at,
                }
            ],
        },
        max_bytes=max_bytes,
    )
    events_validate = job_event_validation_payload(
        events,
        owner_user_id=owner_user_id,
        chat_id=chat_id,
        job_id=normalized_id,
        valid=True,
    )
    return {
        "jobs_board": board,
        "jobs_stats": stats,
        "jobs_snapshot": jobs_snapshot,
        "jobs_contract_check": contract_check,
        "jobs_contract_validate": contract_validate,
        "jobs_inspect": derivatives["jobs_inspect"],
        "jobs_diff": derivatives["jobs_diff"],
        "jobs_validate": derivatives["jobs_validate"],
        "jobs_bundle": derivatives["jobs_bundle"],
        "jobs_verify": derivatives["jobs_verify"],
        "jobs_events": events,
        "jobs_events_validate": events_validate,
        "health_report": health_report,
    }


def snapshot_contract_derivatives(
    inspect_payload: Mapping[str, object],
    *,
    max_bytes: int = DEFAULT_SNAPSHOT_MAX_BYTES,
) -> dict[str, dict[str, object]]:
    """Derive diff/validate/bundle examples from one validated inspect envelope."""

    owner = inspect_payload.get("owner_user_id")
    chat = inspect_payload.get("chat_id")
    job_id = inspect_payload.get("job_id")
    if type(owner) is not int or type(chat) is not int or not isinstance(job_id, str):
        raise SnapshotError("inspect snapshot identity is malformed")
    validated = validate_inspect_payload(
        inspect_payload,
        owner_user_id=owner,
        chat_id=chat,
        job_id=job_id,
        max_bytes=max_bytes,
    )
    integrity = validated["audit_integrity"]
    if not isinstance(integrity, Mapping):
        raise SnapshotError("inspect snapshot integrity is missing")
    job_event_integrity = validated.get(
        "job_event_integrity",
        {"status": "empty", "checked_events": 0, "first_bad_sequence": None},
    )
    if not isinstance(job_event_integrity, Mapping):
        raise SnapshotError("inspect snapshot job-event integrity is malformed")
    fingerprint = validated.get("fingerprint")
    bundle = snapshot_bundle_payload(validated, max_bytes=max_bytes)
    bundle_fingerprint = bundle.get("fingerprint")
    return {
        "jobs_inspect": validated,
        "jobs_diff": snapshot_payload(
            "jobs_diff",
            "owner-chat",
            {
                "owner_user_id": owner,
                "chat_id": chat,
                "job_id": job_id,
                "changed": False,
                "changes": {},
                "current_integrity": dict(integrity),
                "previous_integrity": dict(integrity),
                "current_fingerprint": fingerprint,
                "previous_fingerprint": fingerprint,
                "current_job_event_integrity": job_event_integrity,
                "previous_job_event_integrity": job_event_integrity,
            },
            max_bytes=max_bytes,
        ),
        "jobs_validate": snapshot_payload(
            "jobs_validate",
            "owner-chat",
            {
                "owner_user_id": owner,
                "chat_id": chat,
                "job_id": job_id,
                "valid": True,
                "integrity": dict(integrity),
                "fingerprint": fingerprint,
                "job_event_integrity": job_event_integrity,
            },
            max_bytes=max_bytes,
        ),
        "jobs_bundle": bundle,
        "jobs_verify": snapshot_payload(
            "jobs_verify",
            "owner-chat",
            {
                "owner_user_id": owner,
                "chat_id": chat,
                "job_id": job_id,
                "valid": True,
                "outcome": "accepted",
                "bundle_fingerprint": bundle_fingerprint,
                "expected_fingerprint": bundle_fingerprint,
                "integrity": dict(integrity),
                "job_event_integrity": job_event_integrity,
            },
            max_bytes=max_bytes,
        ),
    }


def verify_snapshot_bundle(
    payload: Mapping[str, object],
    *,
    expected_fingerprint: str,
    owner_user_id: int,
    chat_id: int,
    job_id: str,
    max_bytes: int = DEFAULT_SNAPSHOT_MAX_BYTES,
    strict_integrity: bool = False,
    max_age_seconds: float | None = None,
    now: float | None = None,
) -> dict[str, object]:
    """Verify a complete bundle against an explicit operator-supplied ID."""

    validated = validate_snapshot_bundle(
        payload,
        owner_user_id=owner_user_id,
        chat_id=chat_id,
        job_id=job_id,
        max_bytes=max_bytes,
        strict_integrity=strict_integrity,
        max_age_seconds=max_age_seconds,
        now=now,
    )
    actual = payload.get("fingerprint")
    if not isinstance(actual, str) or not fingerprint_matches(expected_fingerprint, actual):
        raise SnapshotError("bundle fingerprint does not match expected value")
    return validated


def snapshot_contract_report(
    adapter_payloads: Mapping[str, Mapping[str, Mapping[str, object]]],
) -> dict[str, object]:
    """Compare adapter field sets and verify registered fingerprint invariants."""

    registry = snapshot_contract_registry()
    checks: dict[str, object] = {}
    adapter_names = tuple(sorted(adapter_payloads))
    if not adapter_names:
        raise SnapshotError("snapshot contract report has no adapters")
    overall = "pass"
    for kind in SNAPSHOT_CONTRACT_KINDS:
        required, optional = snapshot_contract(kind)
        expected = required | optional
        per_adapter: dict[str, object] = {}
        all_fields: list[frozenset[str]] = []
        issues_by_adapter: dict[str, list[str]] = {}
        for adapter in adapter_names:
            payload = adapter_payloads[adapter].get(kind)
            if payload is None:
                per_adapter[f"{adapter}_fields"] = []
                issues_by_adapter[adapter] = ["missing_payload"]
                continue
            fields = frozenset(payload)
            all_fields.append(fields)
            per_adapter[f"{adapter}_fields"] = sorted(fields)
            issues: list[str] = []
            if required - fields:
                issues.append("missing_required")
            if fields - expected:
                issues.append("unexpected_fields")
            try:
                validate_snapshot_contract(payload, kind)
                if kind == "jobs_events":
                    payload_owner = payload.get("owner_user_id")
                    payload_chat = payload.get("chat_id")
                    payload_job = payload.get("job_id")
                    if type(payload_owner) is not int or type(payload_chat) is not int:
                        raise SnapshotError("job-event audit identity is malformed")
                    if payload_job is not None and not isinstance(payload_job, str):
                        raise SnapshotError("job-event audit job ID is malformed")
                    validate_job_event_audit_payload(
                        payload,
                        owner_user_id=payload_owner,
                        chat_id=payload_chat,
                        job_id=payload_job,
                        allow_tampered=True,
                    )
                if kind == "jobs_events_validate":
                    result_owner = payload.get("owner_user_id")
                    result_chat = payload.get("chat_id")
                    result_job = payload.get("job_id")
                    result_integrity = payload.get("integrity")
                    if type(result_owner) is not int or type(result_chat) is not int:
                        raise SnapshotError("job-event validation identity is malformed")
                    if result_job is not None and not isinstance(result_job, str):
                        raise SnapshotError("job-event validation job ID is malformed")
                    if type(payload.get("valid")) is not bool:
                        raise SnapshotError("job-event validation flag is malformed")
                    _validate_integrity_shape(
                        result_integrity,
                        "job-event validation integrity",
                    )
                    result_total = payload.get("total")
                    if type(result_total) is not int or result_total < 0:
                        raise SnapshotError("job-event validation total is malformed")
                    if type(payload.get("truncated")) is not bool:
                        raise SnapshotError("job-event validation truncation is malformed")
            except SnapshotError:
                issues.append("contract_invalid")
            fingerprint = payload.get("fingerprint")
            if kind in {"jobs_inspect", "jobs_bundle"}:
                if fingerprint is not None:
                    if not isinstance(fingerprint, str) or (
                        _FINGERPRINT.fullmatch(fingerprint) is None
                    ):
                        issues.append("fingerprint_malformed")
                    else:
                        try:
                            if snapshot_fingerprint(payload) != fingerprint:
                                issues.append("fingerprint_mismatch")
                        except SnapshotError:
                            issues.append("fingerprint_invalid")
                else:
                    issues.append("fingerprint_missing")
            elif fingerprint is not None and (
                not isinstance(fingerprint, str)
                or _FINGERPRINT.fullmatch(fingerprint) is None
            ):
                issues.append("fingerprint_reference_malformed")
            elif kind == "jobs_events":
                try:
                    if snapshot_fingerprint(payload) != fingerprint:
                        issues.append("fingerprint_mismatch")
                except SnapshotError:
                    issues.append("fingerprint_invalid")
            if kind == "jobs_bundle" and not _bundle_manifest_is_consistent(payload):
                issues.append("manifest_invalid")
            issues_by_adapter[adapter] = sorted(set(issues))
        field_mismatch = (
            sorted(set().union(*(set(fields) for fields in all_fields)))
            if all_fields
            else []
        )
        if len(all_fields) > 1:
            common = all_fields[0]
            for fields in all_fields[1:]:
                common &= fields
            field_mismatch = sorted(set(field_mismatch) - set(common))
        mismatch = sorted(
            {
                issue
                for issues in issues_by_adapter.values()
                for issue in issues
            }
        )
        if field_mismatch:
            mismatch.append("adapter_field_drift")
        status = "pass" if not mismatch and len(all_fields) == len(adapter_names) else "drift"
        if status != "pass":
            overall = "drift"
        check = {
            "status": status,
            "mismatch": sorted(set(mismatch)),
            **per_adapter,
            "issues": issues_by_adapter,
        }
        checks[kind] = check
    return {"status": overall, "registry": registry, "checks": checks}


def validate_contract_check_payload(
    payload: Mapping[str, object],
    *,
    max_bytes: int = DEFAULT_SNAPSHOT_MAX_BYTES,
    strict_integrity: bool = False,
) -> dict[str, object]:
    """Validate a saved cross-adapter ``jobs_contract_check`` document.

    The function is intentionally state-free: it compares the recorded
    registry, adapter field sets, issue lists, and aliases against the current
    contract without opening SQLite, invoking a runner, or making a network
    request.  A structurally valid but drifted document returns ``valid=false``
    so callers can render a bounded report; malformed input raises
    :class:`SnapshotError`.
    """

    parsed = dict(payload)
    fields = {
        key: value
        for key, value in parsed.items()
        if key not in {"schema_version", "kind", "scope"}
    }
    validated = snapshot_payload(
        "jobs_contract_check",
        "owner-chat",
        fields,
        max_bytes=max_bytes,
    )
    if parsed != validated:
        raise SnapshotError("jobs contract-check envelope is malformed")
    if type(parsed.get("state_present")) is not bool:
        raise SnapshotError("jobs contract-check state flag is malformed")
    integrity = parsed.get("audit_integrity")
    _validate_integrity_shape(integrity, "jobs contract-check integrity")
    assert isinstance(integrity, Mapping)
    integrity_status = integrity.get("status")
    if strict_integrity and integrity_status == "legacy_unverified":
        raise SnapshotError("jobs contract-check audit is legacy-unverified")
    stale_policy = parsed.get("stale_policy")
    _validate_contract_stale_policy(stale_policy)
    registry = parsed.get("registry")
    registry_version = _validate_contract_registry(registry)
    registry_matches = _contract_registry_matches(registry)
    checks = parsed.get("checks")
    adapters, drift_kinds = _validate_contract_checks(checks)
    if integrity_status in {"tampered", "legacy_unverified"}:
        drift_kinds.append("integrity")
    if not registry_matches:
        drift_kinds.append("registry")
    normalized_drift = sorted(set(drift_kinds))
    valid = not normalized_drift
    return snapshot_payload(
        "jobs_contract_validate",
        "owner-chat",
        {
            "valid": valid,
            "status": "pass" if valid else "drift",
            "registry_version": registry_version,
            "adapter_count": len(adapters),
            "adapters": adapters,
            "checked_kinds": len(SNAPSHOT_CONTRACT_KINDS),
            "drift_kinds": normalized_drift,
        },
        max_bytes=max_bytes,
    )


def _validate_contract_stale_policy(value: object) -> None:
    if not isinstance(value, Mapping):
        raise SnapshotError("jobs contract-check stale policy is malformed")
    required = {
        "status",
        "running_jobs",
        "pending_jobs",
    }
    if not required.issubset(value):
        raise SnapshotError("jobs contract-check stale policy is malformed")
    if value.get("status") not in {"normal", "attention"}:
        raise SnapshotError("jobs contract-check stale policy status is malformed")
    for field in ("running_jobs", "pending_jobs"):
        item = value.get(field)
        if type(item) is not int or item < 0:
            raise SnapshotError("jobs contract-check stale policy counts are malformed")


def _validate_contract_registry(value: object) -> int:
    if not isinstance(value, Mapping):
        raise SnapshotError("jobs contract-check registry is malformed")
    version = value.get("version")
    if type(version) is not int or version < 1:
        raise SnapshotError("jobs contract-check registry version is malformed")
    kinds = value.get("kinds")
    if not isinstance(kinds, Mapping):
        raise SnapshotError("jobs contract-check registry kinds are malformed")
    for kind, entry in kinds.items():
        if not isinstance(kind, str) or not isinstance(entry, Mapping):
            raise SnapshotError("jobs contract-check registry kind entry is malformed")
        for field in ("required", "optional"):
            values = entry.get(field)
            if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
                raise SnapshotError("jobs contract-check registry field list is malformed")
            if len(values) != len(set(values)):
                raise SnapshotError("jobs contract-check registry field list repeats a name")
    return version


def _contract_registry_matches(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    expected = snapshot_contract_registry()
    if value.get("version") != expected.get("version"):
        return False
    actual_kinds = value.get("kinds")
    expected_kinds = expected.get("kinds")
    if not isinstance(actual_kinds, Mapping) or not isinstance(expected_kinds, Mapping):
        return False
    if set(actual_kinds) != set(expected_kinds):
        return False
    for kind in SNAPSHOT_CONTRACT_KINDS:
        actual = actual_kinds.get(kind)
        expected_entry = expected_kinds.get(kind)
        if not isinstance(actual, Mapping) or not isinstance(expected_entry, Mapping):
            return False
        if actual.get("required") != expected_entry.get("required"):
            return False
        if actual.get("optional") != expected_entry.get("optional"):
            return False
    return True


def _validate_contract_checks(value: object) -> tuple[list[str], list[str]]:
    if not isinstance(value, Mapping):
        raise SnapshotError("jobs contract-check checks are malformed")
    expected_kinds = set(SNAPSHOT_CONTRACT_KINDS)
    allowed = expected_kinds | set(_CONTRACT_CHECK_ALIASES)
    if not set(value).issubset(allowed):
        raise SnapshotError("jobs contract-check contains an unknown check kind")
    adapters: set[str] = set()
    drift_kinds: list[str] = []
    adapter_sets: dict[str, frozenset[str]] = {}
    for kind in SNAPSHOT_CONTRACT_KINDS:
        check = value.get(kind)
        if not isinstance(check, Mapping):
            drift_kinds.append(kind)
            continue
        status = check.get("status")
        mismatch = check.get("mismatch")
        issues = check.get("issues")
        if status not in _CONTRACT_CHECK_STATUSES:
            raise SnapshotError(f"jobs contract-check status is malformed for {kind}")
        if not isinstance(mismatch, list) or any(not isinstance(item, str) for item in mismatch):
            raise SnapshotError(f"jobs contract-check mismatch is malformed for {kind}")
        if not isinstance(issues, Mapping):
            raise SnapshotError(f"jobs contract-check issues are malformed for {kind}")
        field_sets: dict[str, frozenset[str]] = {}
        for key, fields in check.items():
            if not isinstance(key, str) or not key.endswith(_CONTRACT_ADAPTER_FIELD_SUFFIX):
                continue
            adapter = key[: -len(_CONTRACT_ADAPTER_FIELD_SUFFIX)]
            if not adapter:
                raise SnapshotError(f"jobs contract-check adapter name is malformed for {kind}")
            if not isinstance(fields, list) or any(not isinstance(item, str) for item in fields):
                raise SnapshotError(f"jobs contract-check fields are malformed for {kind}")
            if len(fields) != len(set(fields)) or fields != sorted(fields):
                raise SnapshotError(f"jobs contract-check fields are not canonical for {kind}")
            field_sets[adapter] = frozenset(fields)
            adapters.add(adapter)
        issue_adapters = set(issues)
        if any(not isinstance(name, str) for name in issue_adapters):
            raise SnapshotError(f"jobs contract-check issue adapter is malformed for {kind}")
        adapters.update(issue_adapters)
        if not field_sets or set(field_sets) != issue_adapters:
            drift_kinds.append(kind)
            continue
        adapter_sets[kind] = frozenset(field_sets)
        required, optional = snapshot_contract(kind)
        expected_fields = required | optional
        per_adapter_issues: list[str] = []
        first_fields: frozenset[str] | None = None
        for adapter, fields in sorted(field_sets.items()):
            if required - fields or fields - expected_fields:
                per_adapter_issues.append(adapter)
            if first_fields is None:
                first_fields = fields
            elif fields != first_fields:
                per_adapter_issues.append(adapter)
            adapter_issues = issues.get(adapter)
            if not isinstance(adapter_issues, list) or any(
                not isinstance(item, str) for item in adapter_issues
            ):
                raise SnapshotError(f"jobs contract-check adapter issues are malformed for {kind}")
            if adapter_issues:
                per_adapter_issues.append(adapter)
        has_drift = bool(per_adapter_issues or mismatch)
        if (status == "pass") != (not has_drift):
            has_drift = True
        if has_drift:
            drift_kinds.append(kind)
    missing = expected_kinds - set(value)
    drift_kinds.extend(sorted(missing))
    complete_sets = tuple(adapter_sets.items())
    if complete_sets:
        expected_adapters = complete_sets[0][1]
        if len(expected_adapters) < 2:
            drift_kinds.append("adapter_set")
        for kind, kind_adapters in complete_sets[1:]:
            if kind_adapters != expected_adapters:
                drift_kinds.append(kind)
                drift_kinds.append("adapter_set")
    for alias, canonical in _CONTRACT_CHECK_ALIASES.items():
        if alias in value and value.get(alias) != value.get(canonical):
            drift_kinds.append(canonical)
    return sorted(adapters), sorted(set(drift_kinds))


def job_card_payload(card: JobCard) -> dict[str, object]:
    """Serialize only the content-free fields allowed in a job card."""

    return {
        "id": card.id,
        "kind": card.kind,
        "status": card.status,
        "attempts": card.attempts,
        "created_at": card.created_at,
        "updated_at": card.updated_at,
        "scheduled_for": card.scheduled_for,
        "lease_remaining_seconds": card.lease_remaining_seconds,
        "cancel_requested": card.cancel_requested,
        "dependency_count": card.dependency_count,
        "pending_interaction_count": card.pending_interaction_count,
        "partial_characters": card.partial_characters,
        "result_characters": card.result_characters,
        "age_seconds": card.age_seconds,
    }


def job_projection_fields(projection: JobBoardProjection) -> dict[str, object]:
    """Serialize a projection for reuse by every operational consumer."""

    return {
        "total": projection.total,
        "truncated": projection.truncated,
        "counts": {key: value for key, value in projection.counts},
        "cards": [job_card_payload(card) for card in projection.cards],
    }


def job_event_audit_payload(
    audit: JobEventAudit,
    *,
    owner_user_id: int,
    chat_id: int,
    job_id: str | None = None,
    generated_at: float | None = None,
    max_bytes: int = DEFAULT_SNAPSHOT_MAX_BYTES,
) -> dict[str, object]:
    """Serialize a redacted job-event projection under the shared contract."""

    timestamp = time.time() if generated_at is None else float(generated_at)
    if not math.isfinite(timestamp) or timestamp < 0:
        raise SnapshotError("job-event audit generation time is malformed")
    normalized_job_id = None if job_id is None else job_id.casefold()
    payload = fingerprinted_snapshot_payload(
        "jobs_events",
        "owner-chat",
        {
            "owner_user_id": owner_user_id,
            "chat_id": chat_id,
            "job_id": normalized_job_id,
            "generated_at": timestamp,
            "total": audit.total,
            "omitted": audit.omitted,
            "truncated": audit.truncated,
            "integrity": {
                "status": audit.integrity.status,
                "checked_events": audit.integrity.checked_events,
                "first_bad_sequence": audit.integrity.first_bad_sequence,
            },
            "events": [
                {
                    "sequence": event.sequence,
                    "job_id": event.job_id,
                    "action": event.action,
                    "status": event.status,
                    "actor": event.actor,
                    "created_at": event.created_at,
                }
                for event in audit.events
            ],
        },
        max_bytes=max_bytes,
    )
    return validate_job_event_audit_payload(
        payload,
        owner_user_id=owner_user_id,
        chat_id=chat_id,
        job_id=normalized_job_id,
        max_bytes=max_bytes,
        allow_tampered=True,
    )


def job_event_validation_payload(
    validated_audit: Mapping[str, object],
    *,
    owner_user_id: int,
    chat_id: int,
    job_id: str | None,
    valid: bool,
    expected_integrity: str | None = None,
    max_age_seconds: float | None = None,
    max_bytes: int = DEFAULT_SNAPSHOT_MAX_BYTES,
) -> dict[str, object]:
    """Build a compact result for a state-free event-audit validation."""

    if type(owner_user_id) is not int or type(chat_id) is not int:
        raise SnapshotError("job-event validation identity is malformed")
    if type(valid) is not bool:
        raise SnapshotError("job-event validation result is malformed")
    if job_id is not None and (
        not isinstance(job_id, str) or _JOB_ID.fullmatch(job_id.casefold()) is None
    ):
        raise SnapshotError("job-event validation job ID is malformed")
    integrity = validated_audit.get("integrity")
    if not isinstance(integrity, Mapping):
        raise SnapshotError("job-event validation integrity is missing")
    _validate_integrity_shape(integrity, "job-event validation integrity")
    total = validated_audit.get("total")
    truncated = validated_audit.get("truncated")
    if type(total) is not int or total < 0 or type(truncated) is not bool:
        raise SnapshotError("job-event validation summary is malformed")
    allowed_integrity = {
        "empty",
        "verified",
        "legacy_unverified",
        "tampered",
    }
    if expected_integrity is not None and (
        not isinstance(expected_integrity, str) or expected_integrity not in allowed_integrity
    ):
        raise SnapshotError("expected job-event integrity is invalid")
    if max_age_seconds is not None:
        _validate_snapshot_age_value(0.0, max_age_seconds=max_age_seconds, now=0.0)
    source_fingerprint = validated_audit.get("fingerprint")
    if source_fingerprint is not None:
        if not isinstance(source_fingerprint, str) or (
            _FINGERPRINT.fullmatch(source_fingerprint) is None
        ):
            raise SnapshotError("job-event audit fingerprint is malformed")
        fields_fingerprint: object = source_fingerprint
    else:
        fields_fingerprint = None
    fields: dict[str, object] = {
        "owner_user_id": owner_user_id,
        "chat_id": chat_id,
        "job_id": None if job_id is None else job_id.casefold(),
        "valid": valid,
        "integrity": dict(integrity),
        "total": total,
        "truncated": truncated,
    }
    if fields_fingerprint is not None:
        fields["fingerprint"] = fields_fingerprint
    if expected_integrity is not None:
        fields["expected_integrity"] = expected_integrity
    if max_age_seconds is not None:
        fields["max_age_seconds"] = max_age_seconds
    return snapshot_payload(
        "jobs_events_validate",
        "owner-chat",
        fields,
        max_bytes=max_bytes,
    )


def validate_job_event_audit_payload(
    payload: Mapping[str, object],
    *,
    owner_user_id: int,
    chat_id: int,
    job_id: str | None = None,
    max_bytes: int = DEFAULT_SNAPSHOT_MAX_BYTES,
    strict_integrity: bool = False,
    allow_tampered: bool = False,
    max_age_seconds: float | None = None,
    now: float | None = None,
) -> dict[str, object]:
    """Validate a redacted ``jobs_events`` envelope without opening state."""

    parsed = dict(payload)
    fields = {
        key: value
        for key, value in parsed.items()
        if key not in {"schema_version", "kind", "scope"}
    }
    validated = snapshot_payload(
        "jobs_events",
        "owner-chat",
        fields,
        max_bytes=max_bytes,
    )
    if parsed != validated:
        raise SnapshotError("job-event audit envelope is malformed")
    if (
        type(parsed.get("owner_user_id")) is not int
        or parsed.get("owner_user_id") != owner_user_id
        or type(parsed.get("chat_id")) is not int
        or parsed.get("chat_id") != chat_id
    ):
        raise SnapshotError("job-event audit does not belong to current owner")
    if job_id is not None and not isinstance(job_id, str):
        raise SnapshotError("job-event audit job ID is malformed")
    normalized_id = None if job_id is None else job_id.casefold()
    if normalized_id is not None and _JOB_ID.fullmatch(normalized_id) is None:
        raise SnapshotError("job-event audit job ID is malformed")
    payload_job_id = parsed.get("job_id")
    if payload_job_id != normalized_id:
        raise SnapshotError("job-event audit job does not match requested ID")
    generated_at = parsed.get("generated_at")
    _require_finite_number(parsed, "generated_at")
    assert isinstance(generated_at, int | float) and not isinstance(generated_at, bool)
    if float(generated_at) < 0:
        raise SnapshotError("job-event audit generation time is malformed")
    _validate_snapshot_age_value(
        float(generated_at),
        max_age_seconds=max_age_seconds,
        now=now,
    )
    total = parsed.get("total")
    omitted = parsed.get("omitted")
    truncated = parsed.get("truncated")
    if type(total) is not int or total < 0 or total > 100_000:
        raise SnapshotError("job-event audit total is malformed")
    if type(omitted) is not int or omitted < 0 or omitted > total:
        raise SnapshotError("job-event audit omitted count is malformed")
    if type(truncated) is not bool:
        raise SnapshotError("job-event audit truncation flag is malformed")
    events = parsed.get("events")
    if not isinstance(events, list) or len(events) > 100:
        raise SnapshotError("job-event audit events are malformed")
    if omitted != total - len(events) or truncated != (omitted > 0):
        raise SnapshotError("job-event audit counts are inconsistent")
    integrity = parsed.get("integrity")
    _validate_integrity_shape(integrity, "job-event audit integrity")
    assert isinstance(integrity, Mapping)
    if integrity["status"] == "tampered" and not allow_tampered:
        raise SnapshotError("job-event audit chain is tampered")
    if strict_integrity and integrity["status"] == "legacy_unverified":
        raise SnapshotError("job-event audit chain is legacy-unverified")
    fingerprint = parsed.get("fingerprint")
    if fingerprint is not None:
        if not isinstance(fingerprint, str) or _FINGERPRINT.fullmatch(fingerprint) is None:
            raise SnapshotError("job-event audit fingerprint is malformed")
        if snapshot_fingerprint(parsed) != fingerprint:
            raise SnapshotError("job-event audit fingerprint does not match content")
    previous_sequence = 0
    for event in events:
        if not isinstance(event, Mapping) or frozenset(event) != _JOB_EVENT_AUDIT_EVENT_FIELDS:
            raise SnapshotError("job-event audit event shape is malformed")
        sequence = event.get("sequence")
        if type(sequence) is not int or sequence <= previous_sequence:
            raise SnapshotError("job-event audit sequence is malformed")
        previous_sequence = sequence
        event_id = event.get("job_id")
        if not isinstance(event_id, str) or _JOB_ID.fullmatch(event_id) is None:
            raise SnapshotError("job-event audit event ID is malformed")
        if normalized_id is not None and event_id != normalized_id:
            raise SnapshotError("job-event audit event scope is malformed")
        if event.get("action") not in _JOB_EVENT_AUDIT_ACTIONS:
            raise SnapshotError("job-event audit action is malformed")
        if event.get("status") not in _JOB_EVENT_AUDIT_STATUSES:
            raise SnapshotError("job-event audit status is malformed")
        if event.get("actor") not in _JOB_EVENT_AUDIT_ACTORS:
            raise SnapshotError("job-event audit actor is malformed")
        _require_finite_number(event, "created_at")
        created_at = event["created_at"]
        assert isinstance(created_at, int | float) and not isinstance(created_at, bool)
        if float(created_at) < 0:
            raise SnapshotError("job-event audit event time is malformed")
    return parsed


def _validate_snapshot_age_value(
    observed: float,
    *,
    max_age_seconds: float | None,
    now: float | None,
) -> None:
    if max_age_seconds is None:
        return
    if (
        isinstance(max_age_seconds, bool)
        or not isinstance(max_age_seconds, int | float)
        or not math.isfinite(max_age_seconds)
        or not 0.0 <= float(max_age_seconds) <= 604_800.0
    ):
        raise SnapshotError("snapshot max age must be between 0 and 604800 seconds")
    reference = time.time() if now is None else float(now)
    if not math.isfinite(reference) or reference < 0:
        raise SnapshotError("snapshot reference time is malformed")
    if observed > reference + 30.0:
        raise SnapshotError("snapshot observation time is in the future")
    if max(0.0, reference - observed) > float(max_age_seconds):
        raise SnapshotError("job-event audit is stale")


def validate_inspect_payload(
    payload: Mapping[str, object],
    *,
    owner_user_id: int,
    chat_id: int,
    job_id: str,
    max_bytes: int = DEFAULT_SNAPSHOT_MAX_BYTES,
    strict_integrity: bool = False,
    max_age_seconds: float | None = None,
    now: float | None = None,
) -> dict[str, object]:
    """Validate one owner-scoped, content-free ``jobs_inspect`` document.

    This is intentionally pure: callers may use it for a file, stdin, or an
    in-memory Telegram download.  It verifies the complete nested shape and
    identity before exposing any value to a diff or renderer.
    """

    normalized_id = job_id.casefold()
    if _JOB_ID.fullmatch(normalized_id) is None:
        raise SnapshotError("inspect snapshot job ID is malformed")
    parsed = dict(payload)
    fields = {
        key: value
        for key, value in parsed.items()
        if key not in {"schema_version", "kind", "scope"}
    }
    validated = snapshot_payload(
        "jobs_inspect",
        "owner-chat",
        fields,
        max_bytes=max_bytes,
    )
    if parsed != validated:
        raise SnapshotError("inspect snapshot envelope is malformed")
    if (
        type(parsed.get("owner_user_id")) is not int
        or parsed.get("owner_user_id") != owner_user_id
        or type(parsed.get("chat_id")) is not int
        or parsed.get("chat_id") != chat_id
    ):
        raise SnapshotError("inspect snapshot does not belong to current owner")
    if parsed.get("job_id") != normalized_id:
        raise SnapshotError("inspect snapshot job does not match requested ID")
    _validate_inspect_shape(parsed, expected_job_id=normalized_id)
    _validate_snapshot_age(parsed, max_age_seconds=max_age_seconds, now=now)
    fingerprint = parsed.get("fingerprint")
    if fingerprint is not None:
        if not isinstance(fingerprint, str) or _FINGERPRINT.fullmatch(fingerprint) is None:
            raise SnapshotError("inspect snapshot fingerprint is malformed")
        if snapshot_fingerprint(parsed) != fingerprint:
            raise SnapshotError("inspect snapshot fingerprint does not match content")
    integrity = parsed["audit_integrity"]
    assert isinstance(integrity, Mapping)
    status = integrity["status"]
    if status == "tampered":
        raise SnapshotError("inspect snapshot interaction audit is tampered")
    if strict_integrity and status == "legacy_unverified":
        raise SnapshotError("inspect snapshot interaction audit is legacy-unverified")
    return parsed


def snapshot_bundle_payload(
    inspect_payload: Mapping[str, object],
    *,
    max_bytes: int = DEFAULT_SNAPSHOT_MAX_BYTES,
) -> dict[str, object]:
    """Wrap one validated inspect envelope with a small hand-off manifest."""

    owner = inspect_payload.get("owner_user_id")
    chat = inspect_payload.get("chat_id")
    job_id = inspect_payload.get("job_id")
    if type(owner) is not int or type(chat) is not int or not isinstance(job_id, str):
        raise SnapshotError("inspect snapshot identity is malformed")
    validated = validate_inspect_payload(
        inspect_payload,
        owner_user_id=owner,
        chat_id=chat,
        job_id=job_id,
        max_bytes=max_bytes,
    )
    encoded = _canonical_json(validated)
    manifest = {
        "owner_user_id": owner,
        "chat_id": chat,
        "job_id": job_id,
        "snapshot_bytes": len(encoded),
        "fingerprint": validated.get("fingerprint"),
        "observed_at": validated.get("observed_at"),
    }
    return fingerprinted_snapshot_payload(
        "jobs_bundle",
        "owner-chat",
        {
            "owner_user_id": owner,
            "chat_id": chat,
            "job_id": job_id,
            "snapshot": validated,
            "manifest": manifest,
        },
        max_bytes=max_bytes,
    )


def validate_snapshot_bundle(
    payload: Mapping[str, object],
    *,
    owner_user_id: int,
    chat_id: int,
    job_id: str,
    max_bytes: int = DEFAULT_SNAPSHOT_MAX_BYTES,
    strict_integrity: bool = False,
    max_age_seconds: float | None = None,
    now: float | None = None,
) -> dict[str, object]:
    """Validate and extract the inspect member of a content-free bundle."""

    parsed = dict(payload)
    fields = {
        key: value
        for key, value in parsed.items()
        if key not in {"schema_version", "kind", "scope"}
    }
    validated_bundle = snapshot_payload(
        "jobs_bundle",
        "owner-chat",
        fields,
        max_bytes=max_bytes,
    )
    if parsed != validated_bundle:
        raise SnapshotError("snapshot bundle envelope is malformed")
    normalized_id = job_id.casefold()
    if (
        type(parsed.get("owner_user_id")) is not int
        or parsed.get("owner_user_id") != owner_user_id
        or type(parsed.get("chat_id")) is not int
        or parsed.get("chat_id") != chat_id
        or parsed.get("job_id") != normalized_id
    ):
        raise SnapshotError("snapshot bundle does not belong to current owner")
    bundle_fingerprint = parsed.get("fingerprint")
    if bundle_fingerprint is not None:
        if not isinstance(bundle_fingerprint, str) or _FINGERPRINT.fullmatch(
            bundle_fingerprint
        ) is None:
            raise SnapshotError("snapshot bundle fingerprint is malformed")
        if snapshot_fingerprint(parsed) != bundle_fingerprint:
            raise SnapshotError("snapshot bundle fingerprint does not match content")
    snapshot = parsed.get("snapshot")
    if not isinstance(snapshot, Mapping):
        raise SnapshotError("snapshot bundle member is missing")
    validated_snapshot = validate_inspect_payload(
        snapshot,
        owner_user_id=owner_user_id,
        chat_id=chat_id,
        job_id=normalized_id,
        max_bytes=max_bytes,
        strict_integrity=strict_integrity,
        max_age_seconds=max_age_seconds,
        now=now,
    )
    manifest = parsed.get("manifest")
    if not isinstance(manifest, Mapping) or frozenset(manifest) != _BUNDLE_MANIFEST_FIELDS:
        raise SnapshotError("snapshot bundle manifest is malformed")
    if (
        manifest.get("owner_user_id") != owner_user_id
        or manifest.get("chat_id") != chat_id
        or manifest.get("job_id") != normalized_id
        or manifest.get("fingerprint") != validated_snapshot.get("fingerprint")
        or manifest.get("observed_at") != validated_snapshot.get("observed_at")
    ):
        raise SnapshotError("snapshot bundle manifest does not match content")
    if manifest.get("snapshot_bytes") != len(_canonical_json(validated_snapshot)):
        raise SnapshotError("snapshot bundle byte count does not match content")
    return validated_snapshot


def _bundle_manifest_is_consistent(payload: Mapping[str, object]) -> bool:
    """Check bundle relationships without disclosing any nested values."""

    snapshot = payload.get("snapshot")
    manifest = payload.get("manifest")
    if not isinstance(snapshot, Mapping) or not isinstance(manifest, Mapping):
        return False
    if frozenset(manifest) != _BUNDLE_MANIFEST_FIELDS:
        return False
    if (
        manifest.get("owner_user_id") != payload.get("owner_user_id")
        or manifest.get("chat_id") != payload.get("chat_id")
        or manifest.get("job_id") != payload.get("job_id")
        or manifest.get("fingerprint") != snapshot.get("fingerprint")
        or manifest.get("observed_at") != snapshot.get("observed_at")
    ):
        return False
    try:
        if manifest.get("snapshot_bytes") != len(_canonical_json(snapshot)):
            return False
        nested_fingerprint = snapshot.get("fingerprint")
        if nested_fingerprint is not None and (
            not isinstance(nested_fingerprint, str)
            or _FINGERPRINT.fullmatch(nested_fingerprint) is None
            or snapshot_fingerprint(snapshot) != nested_fingerprint
        ):
            return False
    except SnapshotError:
        return False
    return True


def inspect_metadata(payload: Mapping[str, object]) -> dict[str, object]:
    """Project an already-validated inspect payload to stable comparable fields."""

    job_id = payload.get("job_id")
    if not isinstance(job_id, str):
        raise SnapshotError("inspect snapshot job ID is missing")
    _validate_inspect_shape(payload, expected_job_id=job_id)
    card_value = payload["card"]
    card = card_value if isinstance(card_value, Mapping) else None
    health = payload["health"]
    integrity = payload["audit_integrity"]
    assert isinstance(health, Mapping)
    assert isinstance(integrity, Mapping)
    job_event_integrity = payload.get(
        "job_event_integrity",
        {"status": "empty", "checked_events": 0, "first_bad_sequence": None},
    )
    if not isinstance(job_event_integrity, Mapping):
        raise SnapshotError("inspect snapshot job-event integrity is malformed")
    lease_state = "none"
    if card is not None:
        remaining = card["lease_remaining_seconds"]
        if remaining is not None:
            assert isinstance(remaining, int | float) and not isinstance(remaining, bool)
            lease_state = "active" if remaining > 0 else "expired"
    return {
        "state_present": payload["state_present"],
        "card_present": card is not None,
        "status": None if card is None else card["status"],
        "attempts": None if card is None else card["attempts"],
        "lease_state": lease_state,
        "cancel_requested": None if card is None else card["cancel_requested"],
        "dependency_count": None if card is None else card["dependency_count"],
        "pending_interaction_count": (
            None if card is None else card["pending_interaction_count"]
        ),
        "partial_characters": None if card is None else card["partial_characters"],
        "result_characters": None if card is None else card["result_characters"],
        "health_counts": dict(health["counts"]),
        "running_expired": health["running_expired"],
        "pending_interactions": health["pending_interactions"],
        "audit_integrity": dict(integrity),
        "job_event_integrity": dict(job_event_integrity),
    }


def metadata_diff(
    previous: Mapping[str, object],
    current: Mapping[str, object],
) -> dict[str, object]:
    """Return stable metadata changes; age and lease countdown are ignored."""

    before = inspect_metadata(previous)
    after = inspect_metadata(current)
    return {
        field: {"before": before[field], "after": after[field]}
        for field in _DIFF_FIELDS
        if before[field] != after[field]
    }


def snapshot_age_seconds(
    payload: Mapping[str, object],
    *,
    now: float | None = None,
) -> float | None:
    """Return an inspect observation age, using legacy card time as fallback."""

    observed = payload.get("observed_at")
    if observed is None:
        card = payload.get("card")
        if not isinstance(card, Mapping):
            return None
        observed = card.get("updated_at")
    if not isinstance(observed, int | float) or isinstance(observed, bool):
        raise SnapshotError("inspect snapshot observation time is malformed")
    timestamp = float(observed)
    if not math.isfinite(timestamp) or timestamp < 0:
        raise SnapshotError("inspect snapshot observation time is malformed")
    reference = time.time() if now is None else float(now)
    if not math.isfinite(reference) or reference < 0:
        raise SnapshotError("snapshot reference time is malformed")
    return max(0.0, reference - timestamp)


def _validate_inspect_shape(
    payload: Mapping[str, object],
    *,
    expected_job_id: str,
) -> None:
    if payload.get("job_id") != expected_job_id:
        raise SnapshotError("inspect snapshot job ID is malformed")
    if type(payload.get("state_present")) is not bool:
        raise SnapshotError("inspect snapshot state flag is malformed")
    if "observed_at" in payload:
        _require_finite_number(payload, "observed_at")
        observed_at = payload["observed_at"]
        if not isinstance(observed_at, int | float) or isinstance(observed_at, bool):
            raise SnapshotError("inspect snapshot observation time is malformed")
        if float(observed_at) < 0:
            raise SnapshotError("inspect snapshot observation time is malformed")
    card = payload.get("card")
    if card is not None:
        if not isinstance(card, Mapping) or frozenset(card) != _INSPECT_CARD_FIELDS:
            raise SnapshotError("inspect snapshot card shape is malformed")
        if card.get("id") != expected_job_id:
            raise SnapshotError("inspect snapshot card ID does not match")
        if not isinstance(card.get("kind"), str) or not isinstance(card.get("status"), str):
            raise SnapshotError("inspect snapshot card status is malformed")
        _require_nonnegative_int(card, "attempts")
        _require_nonnegative_int(card, "dependency_count")
        _require_nonnegative_int(card, "pending_interaction_count")
        _require_nonnegative_int(card, "partial_characters")
        _require_nonnegative_int(card, "result_characters")
        for field in ("created_at", "updated_at", "scheduled_for", "age_seconds"):
            _require_finite_number(card, field)
        lease = card.get("lease_remaining_seconds")
        if lease is not None:
            _require_finite_number(card, "lease_remaining_seconds")
        if type(card.get("cancel_requested")) is not bool:
            raise SnapshotError("inspect snapshot cancellation flag is malformed")
    elif payload["state_present"]:
        raise SnapshotError("inspect snapshot state/card combination is malformed")
    health = payload.get("health")
    if not isinstance(health, Mapping) or frozenset(health) != _INSPECT_HEALTH_FIELDS:
        raise SnapshotError("inspect snapshot health shape is malformed")
    counts = health.get("counts")
    if not isinstance(counts, Mapping):
        raise SnapshotError("inspect snapshot health counts are malformed")
    for key, value in counts.items():
        if not isinstance(key, str) or type(value) is not int or value < 0:
            raise SnapshotError("inspect snapshot health counts are malformed")
    _require_nonnegative_int(health, "running_expired")
    _require_nonnegative_int(health, "pending_interactions")
    for field in (
        "oldest_running_age_seconds",
        "oldest_pending_interaction_age_seconds",
    ):
        value = health.get(field)
        if value is not None:
            _require_finite_number(health, field)
    integrity = payload.get("audit_integrity")
    _validate_integrity_shape(integrity, "inspect snapshot integrity")
    if "job_event_integrity" in payload:
        _validate_integrity_shape(
            payload.get("job_event_integrity"),
            "inspect snapshot job-event integrity",
        )


def _validate_integrity_shape(value: object, label: str) -> None:
    if not isinstance(value, Mapping) or frozenset(value) != _INTEGRITY_FIELDS:
        raise SnapshotError(f"{label} shape is malformed")
    if value.get("status") not in {
        "empty",
        "verified",
        "legacy_unverified",
        "tampered",
    }:
        raise SnapshotError(f"{label} status is malformed")
    _require_nonnegative_int(value, "checked_events")
    first_bad = value.get("first_bad_sequence")
    if first_bad is not None and (type(first_bad) is not int or first_bad < 1):
        raise SnapshotError(f"{label} sequence is malformed")


def _require_nonnegative_int(value: Mapping[str, object], field: str) -> None:
    item = value.get(field)
    if type(item) is not int or item < 0:
        raise SnapshotError(f"inspect snapshot {field} is malformed")


def _require_finite_number(value: Mapping[str, object], field: str) -> None:
    item = value.get(field)
    if not isinstance(item, int | float) or isinstance(item, bool) or not math.isfinite(item):
        raise SnapshotError(f"inspect snapshot {field} is malformed")


def _validate_snapshot_age(
    payload: Mapping[str, object],
    *,
    max_age_seconds: float | None,
    now: float | None,
) -> None:
    if max_age_seconds is None:
        return
    if (
        not isinstance(max_age_seconds, int | float)
        or isinstance(max_age_seconds, bool)
        or not math.isfinite(max_age_seconds)
        or not 0.0 <= float(max_age_seconds) <= 604_800.0
    ):
        raise SnapshotError("snapshot max age must be between 0 and 604800 seconds")
    age = snapshot_age_seconds(payload, now=now)
    if age is None:
        raise SnapshotError("inspect snapshot has no observation time")
    if age > float(max_age_seconds):
        raise SnapshotError("inspect snapshot is stale")
    observed = payload.get("observed_at")
    if observed is not None:
        if not isinstance(observed, int | float) or isinstance(observed, bool):
            raise SnapshotError("inspect snapshot observation time is malformed")
        reference = time.time() if now is None else float(now)
        if float(observed) > reference + 30.0:
            raise SnapshotError("inspect snapshot observation time is in the future")


def _validate_value(value: object, *, content_free: bool, path: str) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str) or not key:
                raise SnapshotError(f"snapshot key is invalid at {path}")
            normalized = key.casefold()
            if _is_always_forbidden_key(normalized):
                raise SnapshotError(f"credential field is not allowed at {path}.{key}")
            if content_free and _is_forbidden_key(normalized):
                raise SnapshotError(f"content field is not allowed at {path}.{key}")
            _validate_value(child, content_free=content_free, path=f"{path}.{key}")
        return
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        for index, child in enumerate(value):
            _validate_value(child, content_free=content_free, path=f"{path}[{index}]")
        return
    if value is None or isinstance(value, str | int | bool):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SnapshotError(f"snapshot number is not finite at {path}")
        return
    raise SnapshotError(f"snapshot value is not JSON-compatible at {path}")


def _is_forbidden_key(key: str) -> bool:
    return key in _FORBIDDEN_CONTENT_KEYS or key.endswith(
        ("_token", "_secret", "_password")
    )


def _is_always_forbidden_key(key: str) -> bool:
    return key in _FORBIDDEN_ALWAYS_KEYS or key.endswith(
        ("_token", "_secret", "_password")
    )
