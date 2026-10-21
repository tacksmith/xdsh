from typing import Any

import httpx
import pytest

from xdsh.telegram.api import TelegramApiError, TelegramBotApi


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['connect', 'pool', 'rate_limit'])
async def test_safe_pre_delivery_failures_recover_without_unknown(
    failure: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            if failure == 'connect':
                raise httpx.ConnectTimeout('synthetic', request=request)
            if failure == 'pool':
                raise httpx.PoolTimeout('synthetic', request=request)
            return httpx.Response(429, json={'ok': False, 'error_code': 429,
                                            'parameters': {'retry_after': 0}})
        return httpx.Response(200, json={'ok': True, 'result': {'message_id': 123}})

    client_type = httpx.AsyncClient
    clients: list[httpx.AsyncClient] = []

    def offline_client(**kwargs: Any) -> httpx.AsyncClient:
        # PoolTimeout opens a fresh recovery client. Mock every client, not
        # just the initial one, so this unit test can never contact Telegram.
        kwargs["transport"] = httpx.MockTransport(handle)
        client = client_type(**kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr("xdsh.telegram.api.httpx.AsyncClient", offline_client)
    api = TelegramBotApi('synthetic-only')
    api._http_client = offline_client()
    try:
        assert await api.send_message(42, 'synthetic reply') == 123
        assert calls == 2
        assert len(clients) == (2 if failure == "pool" else 1)
    finally:
        await api.aclose()
    assert all(client.is_closed for client in clients)


@pytest.mark.asyncio
async def test_read_timeout_is_never_automatically_resent() -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout('synthetic', request=request)

    api = TelegramBotApi('synthetic-only')
    api._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(TelegramApiError):
            await api.send_message(42, 'synthetic reply')
        assert calls == 1
    finally:
        await api.aclose()
