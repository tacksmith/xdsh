from __future__ import annotations

import asyncio
from typing import Any

import pytest

from xdsh.core.external_gateway import RestrictedMcpGateway
from xdsh.core.external_runtime import ExternalClients
from xdsh.core.model_availability import ModelAvailabilityCheck
from xdsh.core.models import (
    ModelAvailability,
    ModelCatalogError,
    ModelDirectory,
    ModelProvider,
    ModelRouteHealth,
)


class Catalog:
    async def snapshot(self) -> ModelDirectory:
        return ModelDirectory((ModelProvider("test", ("exact-route",), "Test"),),
                              route_names=("exact-route", "*"))


class Probe:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def probe(self, model: str) -> ModelRouteHealth:
        self.calls.append(model)
        return ModelRouteHealth(model, ModelAvailability.AVAILABLE, "execution_ok")


@pytest.mark.asyncio
async def test_availability_is_metadata_only_by_default_and_rejects_unknown_route() -> None:
    probe = Probe()
    check = ModelAvailabilityCheck(Catalog(), probe)
    assert (await check.check("exact-route"))["execution"] == "not_tested"
    assert (await check.check("guessed-route", probe=True))["execution"] == "unconfirmed"
    assert probe.calls == []


@pytest.mark.asyncio
async def test_unavailable_catalog_is_unknown_not_false_configuration() -> None:
    class Missing:
        async def snapshot(self) -> ModelDirectory:
            raise ModelCatalogError("not available")

    result = await ModelAvailabilityCheck(Missing(), Probe()).check("exact-route", probe=True)
    assert result["configured_exact_route"] is None
    assert result["reason"] == "catalog_unavailable"


@pytest.mark.asyncio
async def test_gateway_probe_is_exact_bounded_cached_and_does_not_claim_identity() -> None:
    probe = Probe()
    gateway = RestrictedMcpGateway(ExternalClients(),
                                   model_availability=ModelAvailabilityCheck(Catalog(), probe))
    for _ in range(2):
        response: Any = await gateway.dispatch({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "model_availability",
                       "arguments": {"model": "exact-route", "probe": True}},
        })
        result = response["result"]["structuredContent"]
        assert result["execution"] == "available"
        assert result["upstream_identity"] == "unverified"
        assert result["subscription"] == "unconfirmed"
    assert probe.calls == ["exact-route"]


@pytest.mark.asyncio
async def test_cancelled_probe_is_not_replayed_and_concurrent_call_is_bounded() -> None:
    started = asyncio.Event()

    class Slow(Probe):
        async def probe(self, model: str) -> ModelRouteHealth:
            self.calls.append(model)
            started.set()
            await asyncio.sleep(60)
            raise AssertionError("probe should be cancelled")

    probe = Slow()
    check = ModelAvailabilityCheck(Catalog(), probe)
    first = asyncio.create_task(check.check("exact-route", probe=True))
    await asyncio.wait_for(started.wait(), 1)
    assert (await check.check("exact-route", probe=True))["reason"] == "check_busy"
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    result = await check.check("exact-route", probe=True)
    assert result["cached"] and result["execution"] == "unconfirmed"
    assert probe.calls == ["exact-route"]


@pytest.mark.asyncio
async def test_wildcard_and_catalog_only_names_are_not_exact_routes() -> None:
    class Wildcard:
        async def snapshot(self) -> ModelDirectory:
            return ModelDirectory((ModelProvider("test", ("exact-route",), "Test"),),
                                  route_names=("*",))

    probe = Probe()
    result = await ModelAvailabilityCheck(Wildcard(), probe).check("exact-route", probe=True)
    assert result["configured_exact_route"] is False
    assert probe.calls == []


@pytest.mark.asyncio
async def test_probe_never_discloses_provider_detail_and_budget_is_shared() -> None:
    class Many:
        async def snapshot(self) -> ModelDirectory:
            routes = tuple(f"route-{i}" for i in range(5))
            return ModelDirectory((ModelProvider("test", routes, "Test"),), route_names=routes)

    class Untrusted(Probe):
        async def probe(self, model: str) -> ModelRouteHealth:
            self.calls.append(model)
            return ModelRouteHealth(model, ModelAvailability.UNKNOWN, "private-provider-detail")

    probe = Untrusted()
    check = ModelAvailabilityCheck(Many(), probe)
    for i in range(5):
        result = await check.check(f"route-{i}", probe=True)
        assert "private-provider-detail" not in str(result)
    assert result["reason"] == "probe_budget_exhausted"
    assert len(probe.calls) == 4


@pytest.mark.asyncio
async def test_gateway_rejects_endpoint_prompt_and_credential_overrides() -> None:
    probe = Probe()
    gateway = RestrictedMcpGateway(ExternalClients(),
                                   model_availability=ModelAvailabilityCheck(Catalog(), probe))
    for name in ("url", "prompt", "api_key", "credential_env"):
        response: Any = await gateway.dispatch({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "model_availability",
                       "arguments": {"model": "exact-route", "probe": True, name: "unused"}},
        })
        assert response["error"]["code"] == -32602
    assert probe.calls == []
