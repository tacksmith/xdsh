from __future__ import annotations

import logging

import httpx
import pytest

from xdsh.telegram.api import TelegramApiError, TelegramBotApi, TelegramRequestRejected


async def test_polling_does_not_use_exhausted_outbound_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exhausted(request: httpx.Request) -> httpx.Response:
        raise httpx.PoolTimeout('saturated')

    api = TelegramBotApi('synthetic-token', pooled=True)
    await api.aclose()
    api._http_client = httpx.AsyncClient(transport=httpx.MockTransport(exhausted))
    fresh = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={'ok': True, 'result': []})))
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: fresh)
    try:
        assert await api.get_updates(offset=None, timeout_seconds=1) == ()
        assert fresh.is_closed
    finally:
        await api.aclose()


async def test_unsent_pool_timeout_can_use_one_fresh_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exhausted(request: httpx.Request) -> httpx.Response:
        raise httpx.PoolTimeout('saturated')

    api = TelegramBotApi('synthetic-token', pooled=True)
    await api.aclose()
    api._http_client = httpx.AsyncClient(transport=httpx.MockTransport(exhausted))
    sent = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={'ok': True, 'result': {'message_id': 77}})

    fresh = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: fresh)
    try:
        assert await api.send_message(123, 'one reply') == 77
        assert len(sent) == 1
        assert fresh.is_closed
    finally:
        await api.aclose()


async def test_pooled_calls_reuse_client_and_hide_token(caplog: pytest.LogCaptureFixture) -> None:
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"ok": True, "result": {"id": 1}})

    api = TelegramBotApi("private-test-token", pooled=True)
    await api.aclose()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handle), trust_env=False)
    api._http_client = client
    try:
        with caplog.at_level(logging.INFO):
            await api.get_me()
            await api.get_me()
        assert api._http_client is client
        assert len(calls) == 2
        assert "private-test-token" not in caplog.text
    finally:
        await api.aclose()
    assert client.is_closed


@pytest.mark.parametrize("status", [302, 400, 429, 500])
async def test_pooled_errors_never_redirect_retry_or_expose_secrets(status: int) -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, headers={"Retry-After": "3", "Location": "https://evil"},
                              text="private-test-token")

    api = TelegramBotApi("private-test-token", pooled=True)
    await api.aclose()
    api._http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handle), follow_redirects=False, trust_env=False,
    )
    try:
        with pytest.raises(TelegramApiError) as caught:
            await api.send_message(42, "one attempt")
        assert "private-test-token" not in str(caught.value)
        assert caught.value.status_code == status
        assert caught.value.retry_after_seconds == 3
        assert isinstance(caught.value, TelegramRequestRejected) == (status == 400)
        assert calls == 1
    finally:
        await api.aclose()
