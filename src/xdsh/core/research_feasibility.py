"""Bounded, advisory resource/scope estimates; never a new runtime quota."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Feasibility:
    scope: str
    unit: str
    requested_units: int | None
    planned_units: int | None
    estimated_peak_bytes: int | None
    available_bytes: int | None
    strategy: str
    estimate_basis: str

    @property
    def notices(self) -> tuple[str, ...]:
        notices = []
        if self.scope == "unknown":
            notices.append("scope_unknown")
        elif self.scope == "subset":
            notices.append("partial_scope")
        if self.estimated_peak_bytes is None or self.available_bytes is None:
            notices.append("resource_estimate_unknown")
        elif self.estimated_peak_bytes > self.available_bytes:
            notices.append("memory_risk")
        if self.strategy == "undecided":
            notices.append("strategy_undecided")
        return tuple(notices)

    def summary(self) -> dict[str, object]:
        """No free-text estimate basis or filesystem/environment data is echoed."""
        return {"scope": self.scope, "requested_units": self.requested_units,
                "planned_units": self.planned_units,
                "estimated_peak_bytes": self.estimated_peak_bytes,
                "available_bytes": self.available_bytes, "strategy": self.strategy,
                "notices": self.notices, "assurance": "declared_estimates_not_live_measurement"}


def parse_feasibility(value: object) -> Feasibility:
    """Require explicit unknowns instead of inventing available memory or coverage."""
    required = {"scope", "unit", "requested_units", "planned_units", "estimated_peak_bytes",
                "available_bytes", "strategy", "estimate_basis"}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("invalid research feasibility")
    for key, limit in (("unit", 60), ("estimate_basis", 500)):
        text = value[key]
        if (not isinstance(text, str) or not text.strip() or len(text) > limit
                or any(ord(c) < 32 for c in text)):
            raise ValueError("invalid research feasibility")
    for key in ("requested_units", "planned_units", "estimated_peak_bytes", "available_bytes"):
        number = value[key]
        minimum = 1 if key.endswith("units") else 0
        if number is not None and (type(number) is not int or not minimum <= number <= 2**63 - 1):
            raise ValueError("invalid research feasibility")
    if (value["scope"] not in ("full", "subset", "unknown")
            or value["strategy"] not in ("in_memory", "streaming", "staged", "undecided")):
        raise ValueError("invalid research feasibility")
    requested, planned = value["requested_units"], value["planned_units"]
    known = requested is not None and planned is not None
    if ((value["scope"] == "full" and (not known or requested != planned))
            or (value["scope"] == "subset" and (not known or planned >= requested))
            or (known and planned > requested)):
        raise ValueError("invalid research feasibility")
    return Feasibility(**value)


def unknown_feasibility() -> dict[str, object]:
    """An editable example, not a fabricated measurement or permission to reduce scope."""
    return {"scope": "unknown", "unit": "samples", "requested_units": None,
            "planned_units": None, "estimated_peak_bytes": None, "available_bytes": None,
            "strategy": "undecided", "estimate_basis": "Not measured; assess before costly work"}
