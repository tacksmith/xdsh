"""The provider timeout must be configurable independently of total task duration."""
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import cast

import pytest

from xdsh.core.providers import ProviderConnection
from xdsh.ops.model_gateway import LiteLlmMessagesBackend, _AsyncMessagesStream


def test_provider_idle_configuration_is_used_and_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDSH_PROVIDER_STREAM_IDLE_SECONDS", "300")
    monkeypatch.setattr("xdsh.ops.model_gateway.importlib.import_module",
                        lambda _: SimpleNamespace())
    backend = LiteLlmMessagesBackend()
    try:
        assert backend.stream_idle_seconds == 300
    finally:
        backend.close()


def test_sdk_import_cannot_reenable_noisy_cost_map_tracebacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import logging

    def importing(_: str) -> SimpleNamespace:
        logging.getLogger("LiteLLM Proxy").setLevel(logging.ERROR)
        return SimpleNamespace()

    monkeypatch.setattr("xdsh.ops.model_gateway.importlib.import_module", importing)
    backend = LiteLlmMessagesBackend()
    try:
        assert logging.getLogger("LiteLLM Proxy").level == logging.CRITICAL
    finally:
        backend.close()


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "3601", "not-a-number"])
def test_bad_provider_idle_config_is_rejected_before_sdk_start(
    value: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDSH_PROVIDER_STREAM_IDLE_SECONDS", value)
    with pytest.raises(ValueError):
        LiteLlmMessagesBackend()


def test_configured_budget_reaches_http_adapter_and_resets_per_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Fast in-process protocol seam: no provider, credentials, or 300s sleeps.
    seen: list[float] = []
    calls = 0

    async def create(**kwargs: object) -> AsyncIterator[object]:
        nonlocal calls
        calls += 1
        assert kwargs["timeout"] == 300
        assert kwargs["num_retries"] == 0
        handler = kwargs["client"]
        assert handler.client.timeout.read == 300  # type: ignore[attr-defined]

        async def events() -> AsyncIterator[object]:
            yield {"type": "ping"}
            yield {"type": "message_stop"}

        return events()

    class Handler:
        def __init__(self, *, timeout: float) -> None:
            import httpx
            assert timeout == 300
            self.client = httpx.AsyncClient()

    sdk = SimpleNamespace(anthropic=SimpleNamespace(messages=SimpleNamespace(acreate=create)))
    import importlib
    original_import = importlib.import_module
    monkeypatch.setattr("xdsh.ops.model_gateway.importlib.import_module", lambda name: (
        sdk if name == "litellm" else SimpleNamespace(AsyncHTTPHandler=Handler)
        if name == "litellm.llms.custom_httpx.http_handler" else original_import(name)
    ))
    monkeypatch.setattr("xdsh.ops.model_gateway.check_endpoint_dns", lambda _: None)
    monkeypatch.setenv("XDSH_PROVIDER_STREAM_IDLE_SECONDS", "300")
    from concurrent.futures import Future
    original_result = Future.result

    def result(self: Future[object], timeout: float | None = None) -> object:
        if timeout is not None:
            seen.append(timeout)
        return original_result(self, timeout)

    monkeypatch.setattr(Future, "result", result)
    backend = LiteLlmMessagesBackend()
    try:
        response = backend.create(ProviderConnection(
            "test", "test", "anthropic", "https://example.com", "TEST_KEY"
        ), {"stream": True}, "synthetic-key")
        assert list(cast(_AsyncMessagesStream, response)) == [
            {"type": "ping"}, {"type": "message_stop"}
        ]
        assert calls == 1
        assert seen.count(310) == 4  # Open, two events, and EOF; per-wait, not total runtime.
    finally:
        backend.close()
