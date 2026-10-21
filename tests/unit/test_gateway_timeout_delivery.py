from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator, Mapping
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import httpx
import pytest

from xdsh.core.providers import ProviderConnection, ProviderDnsError, ProviderRegistry
from xdsh.ops.model_gateway import ModelGatewayServer


def test_dns_policy_failure_is_not_a_model_parameter_error(tmp_path: Path) -> None:
    class Backend:
        def create(self, route: ProviderConnection, payload: Mapping[str, object],
                   key: str) -> object:
            raise ProviderDnsError("private DNS detail")

    registry = ProviderRegistry(tmp_path / "providers")
    registry.add(ProviderConnection("test", "test", "openai", "https://example.com",
                                    "TEST_KEY"), approved=True, credential="synthetic-key")
    server = ModelGatewayServer(registry, token="g" * 32, port=0, backend=Backend())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = Request(f"http://127.0.0.1:{server.port}/v1/messages",
                          data=json.dumps({"model": "test", "max_tokens": 16,
                                           "messages": [{"role": "user", "content": "test"}]
                                           }).encode(),
                          headers={"Content-Type": "application/json", "x-api-key": "g" * 32})
        with pytest.raises(HTTPError) as caught:
            build_opener(ProxyHandler({})).open(request, timeout=2)
        with caught.value as response:
            body = response.read().decode()
            assert response.status == 503
            assert "XDSH_PROVIDER_DNS" in body
            assert "invalid_request_error" not in body
            assert "private DNS detail" not in body
    finally:
        server.close()
        thread.join(2)


@pytest.mark.parametrize("after_partial", [False, True])
def test_429_preserves_only_safe_retry_after_without_replaying(
    tmp_path: Path, after_partial: bool,
) -> None:
    calls = 0

    class Backend:
        def create(self, route: ProviderConnection, payload: Mapping[str, object],
                   key: str) -> object:
            nonlocal calls
            calls += 1
            error = httpx.HTTPStatusError("private upstream detail", request=httpx.Request(
                "POST", "https://example.com"), response=httpx.Response(
                    429, headers={"Retry-After": "15", "X-Private": "secret-header"}))
            # LiteLLM exposes a typed status; do the same in this HTTP fixture.
            error.status_code = 429  # type: ignore[attr-defined]
            if not after_partial:
                raise error

            def chunks() -> Iterator[object]:
                yield {"type": "content_block_delta", "delta": {
                    "type": "text_delta", "text": "partial answer"}}
                raise error
            return chunks()

    registry = ProviderRegistry(tmp_path / "providers")
    registry.add(ProviderConnection("test", "test", "openai", "https://example.com",
                                    "TEST_KEY"), approved=True, credential="synthetic-key")
    server = ModelGatewayServer(registry, token="g" * 32, port=0, backend=Backend())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = Request(f"http://127.0.0.1:{server.port}/v1/messages",
                          data=json.dumps({"model": "test", "stream": True, "max_tokens": 16,
                                           "messages": [{"role": "user", "content": "test"}]
                                           }).encode(),
                          headers={"Content-Type": "application/json", "x-api-key": "g" * 32})
        try:
            response = build_opener(ProxyHandler({})).open(request, timeout=2)
        except HTTPError as error:
            response = error
        with response:
            body = response.read().decode()
            assert response.status == (200 if after_partial else 429)
            if not after_partial:
                assert response.headers["Retry-After"] == "15"
            assert '"retry_after_seconds": 15' in body
            assert "rate_limit_error" in body
            assert "secret-header" not in body and "private upstream detail" not in body
            assert response.headers.get("X-Private") is None
            assert calls == 1
            if after_partial:
                assert body.index("partial answer") < body.index("rate_limit_error")
    finally:
        server.close()
        thread.join(2)


@pytest.mark.parametrize("stream_started", [False, True])
@pytest.mark.parametrize(
    "failure", [TimeoutError, asyncio.TimeoutError, FutureTimeoutError, httpx.ReadTimeout, None],
)
def test_upstream_timeout_delivers_explicit_error_not_silent_eof(
    tmp_path: Path, stream_started: bool, failure: type[Exception] | None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO", logger="xdsh.ops.model_gateway")
    calls = 0
    closed = False

    class Backend:
        def create(self, route: ProviderConnection, payload: Mapping[str, object],
                   key: str) -> object:
            nonlocal calls
            calls += 1
            if not stream_started:
                if failure is not None:
                    raise failure("private upstream body")
                return iter(())

            def events() -> Iterator[object]:
                nonlocal closed
                try:
                    yield {"type": "content_block_delta", "delta": {
                        "type": "text_delta", "text": "partial answer"}}
                    if failure is not None:
                        raise failure("private upstream body")
                finally:
                    closed = True

            return events()

    registry = ProviderRegistry(tmp_path / "providers")
    route = ProviderConnection("test", "test", "openai", "https://example.com", "TEST_KEY")
    registry.add(route, approved=True, credential="synthetic-key")
    server = ModelGatewayServer(registry, token="g" * 32, port=0, backend=Backend())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = Request(f"http://127.0.0.1:{server.port}/v1/messages",
                          data=json.dumps({"model": "test", "max_tokens": 16, "stream": True,
                                           "messages": [{"role": "user", "content": "test"}]
                                           }).encode(),
                          headers={"Content-Type": "application/json", "x-api-key": "g" * 32})
        try:
            response = build_opener(ProxyHandler({})).open(request, timeout=2)
        except HTTPError as error:
            response = error
        with response:
            wire = response.read().decode()
            assert response.status == (200 if stream_started or failure is None else 504)
        expected = "timeout_error" if failure is not None else "api_error"
        assert expected in wire
        assert "private upstream body" not in wire
        assert "credential" not in wire
        assert calls == 1
        if failure is not None and stream_started:
            assert "[XDSH_PROVIDER_STREAM_IDLE]" in wire
        else:
            assert "[XDSH_PROVIDER_STREAM_IDLE]" not in wire
        if stream_started:
            assert wire.index("partial answer") < wire.index(expected)
            assert closed
    finally:
        server.close()
        thread.join(2)
    assert f"outcome={expected}" in caplog.text
    phase = "upstream_stream" if stream_started or failure is None else "upstream_open"
    assert f"phase={phase}" in caplog.text
    assert "last_event_seconds=" in caplog.text
    assert "silence_seconds=" in caplog.text
    assert "private upstream body" not in caplog.text
    assert "synthetic-key" not in caplog.text
