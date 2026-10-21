"""Host-owned exact-route checks; no model-visible credentials or arbitrary requests."""

from __future__ import annotations

import asyncio
import re
import threading
import time

from xdsh.core.external import JsonValue
from xdsh.core.models import ModelCatalog, ModelCatalogError, ModelExecutionProbe


class ModelAvailabilityCheck:
    """Metadata by default; explicit probes have a shared ten-minute token budget.

    This reports a configured route's execution health, not subscription terms,
    quota or proof of upstream model identity. The caller cannot supply an endpoint,
    prompt, credential reference or key. Instances may serve multiple HTTP loops.
    """

    def __init__(self, catalog: ModelCatalog, execution_probe: ModelExecutionProbe) -> None:
        self._catalog = catalog
        self._probe = execution_probe
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, dict[str, JsonValue]]] = {}
        self._attempts: list[float] = []

    async def check(self, model: str, *, probe: bool = False) -> dict[str, JsonValue]:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}", model) is None:
            raise ValueError("invalid model route")
        result: dict[str, JsonValue] = {
            "model": model, "configured_exact_route": None, "execution": "unconfirmed",
            "scope": "This XDSH gateway only, not the upstream account's model inventory.",
            "upstream_identity": "unverified", "subscription": "unconfirmed",
            "quota": "unconfirmed", "cached": False,
            "next_action": "If unconfirmed, stop. Do not search for credentials or other routes.",
        }
        if not self._lock.acquire(blocking=False):
            return {**result, "reason": "check_busy"}
        try:
            now = time.monotonic()
            self._cache = {k: v for k, v in self._cache.items() if now - v[0] < 600}
            self._attempts = [t for t in self._attempts if now - t < 600]
            try:
                directory = await asyncio.wait_for(self._catalog.snapshot(), timeout=5)
            except (ModelCatalogError, OSError, ValueError, asyncio.TimeoutError):
                return {**result, "reason": "catalog_unavailable"}
            if directory.route_names is None:
                return {**result, "reason": "exact_route_not_verified"}
            if (model not in directory.route_names or model not in directory.models
                    or directory.is_shadowed_route(model)):
                result["configured_exact_route"] = False
                return {**result, "reason": "exact_route_not_verified"}
            result["configured_exact_route"] = True
            cached = self._cache.get(model)
            if cached is not None:
                return {**cached[1], "cached": True}
            if not probe:
                return {**result, "execution": "not_tested", "reason": "metadata_only"}
            if len(self._attempts) >= 4:
                return {**result, "reason": "probe_budget_exhausted"}
            # Claim before awaiting so timeout/cancellation cannot cause an implicit replay.
            self._attempts.append(now)
            result["reason"] = "probe_interrupted_or_unavailable"
            self._cache[model] = (now, dict(result))
            try:
                health = await asyncio.wait_for(self._probe.probe(model), timeout=20)
            except (ModelCatalogError, OSError, ValueError, asyncio.TimeoutError):
                return result
            if health.model != model:
                return result
            result["execution"] = health.availability.value
            # Never pass an arbitrary provider detail string through this seam.
            safe_codes = {"execution_ok", "credential_unavailable", "transport_error",
                          "invalid_response", "invalid_payload", "response_too_large"}
            result["reason"] = (
                health.detail if health.detail in safe_codes
                or re.fullmatch(r"http_[1-5][0-9]{2}", health.detail) else "probe_completed"
            )
            result["observed_at"] = time.time()
            self._cache[model] = (now, dict(result))
            return result
        finally:
            self._lock.release()
