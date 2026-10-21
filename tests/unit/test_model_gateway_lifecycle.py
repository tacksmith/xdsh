import asyncio
import gc
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, cast

import pytest

from xdsh.core.providers import ProviderConnection
from xdsh.ops.model_gateway import LiteLlmMessagesBackend, ProviderRequestError, _safe_retry_after


@pytest.mark.parametrize("value", ["NaN", "inf", "-2", "1\r\nX-Private: secret", True,
                                  "x" * 129, 604_801, 10**400])
def test_retry_after_hint_rejects_untrusted_or_unbounded_values(value: object) -> None:
    assert _safe_retry_after(value) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["openai", "azure", "anthropic"])
async def test_adapter_keeps_stream_client_alive_until_consumer_finishes(
    monkeypatch: pytest.MonkeyPatch,
    engine: str,
) -> None:
    class Client:
        closed = False

        def __init__(self, **kwargs: Any) -> None:
            pass

        async def aclose(self) -> None:
            self.closed = True

    class Handler:
        def __init__(self, **kwargs: Any) -> None:
            self.client = kwargs.get("http_client", Client())

        def __del__(self) -> None:
            asyncio.get_running_loop().create_task(self.client.aclose())

    async def create(**kwargs: Any) -> Any:
        client = kwargs["client"].client

        async def stream() -> Any:
            assert not client.closed, "adapter destroyed the owner of an active stream"
            yield {"type": "message_stop"}

        return stream()

    modules = {
        "httpx": SimpleNamespace(AsyncClient=Client),
        "litellm.llms.custom_httpx.http_handler": SimpleNamespace(AsyncHTTPHandler=Handler),
        "openai": SimpleNamespace(AsyncOpenAI=Handler, AsyncAzureOpenAI=Handler),
    }
    monkeypatch.setattr("xdsh.ops.model_gateway.importlib.import_module", modules.__getitem__)
    backend = object.__new__(LiteLlmMessagesBackend)
    monkeypatch.setattr(
        backend,
        "_sdk",
        SimpleNamespace(anthropic=SimpleNamespace(messages=SimpleNamespace(acreate=create))),
        raising=False,
    )
    route = ProviderConnection(
        "test",
        "test",
        engine,
        "https://example.com/v1",
        "TEST_KEY",
        "2025-01-01" if engine == "azure" else None,
    )
    result = await backend._create(route, {"stream": True}, "synthetic-key")
    gc.collect()
    await asyncio.sleep(0)
    assert await anext(cast(AsyncIterator[object], result[0])) == {"type": "message_stop"}
    await cast(Client, result[1]).aclose()


@pytest.mark.asyncio
async def test_sdk_error_sanitization_preserves_retry_hint_without_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    class Handler:
        def __init__(self, **kwargs: Any) -> None:
            import httpx
            self.client = httpx.AsyncClient()

    class Failure(Exception):
        status_code = 429

        def __init__(self, message: str) -> None:
            self.headers = {"Retry-After": "15", "Authorization": "private-credential"}
            super().__init__(message)

    async def create(**kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        assert kwargs["num_retries"] == 0
        raise Failure("private provider body")

    backend = object.__new__(LiteLlmMessagesBackend)
    monkeypatch.setattr(backend, "_sdk", SimpleNamespace(
        anthropic=SimpleNamespace(messages=SimpleNamespace(acreate=create))), raising=False)
    import importlib
    original_import = importlib.import_module
    monkeypatch.setattr("xdsh.ops.model_gateway.importlib.import_module", lambda name: (
        SimpleNamespace(AsyncHTTPHandler=Handler)
        if name == "litellm.llms.custom_httpx.http_handler" else original_import(name)))
    route = ProviderConnection("test", "test", "anthropic", "https://example.com", "TEST_KEY")
    with pytest.raises(ProviderRequestError) as caught:
        await backend._create(route, {"stream": True}, "synthetic-key")
    assert calls == 1
    assert caught.value.retry_after_seconds == 15
    assert caught.value.status_code == 429
    assert "private" not in str(caught.value)
