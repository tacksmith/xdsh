"""Regression for the R3 model stream ending after its first short text delta."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener

import pytest

from xdsh.core.providers import ProviderConnection, ProviderRegistry
from xdsh.ops.model_gateway import ModelGatewayServer, _chunk
from xdsh.ops.stream_start import client_tools_only, stream_with_start_recovery


@pytest.mark.parametrize("packed", [False, True])
def test_short_uncommitted_stream_eof_recovers_without_replaying_prior_tools(
    tmp_path: Path, packed: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDSH_PROVIDER_STREAM_START_RETRIES", "2")
    attempts: list[dict[str, object]] = []

    class Backend:
        def create(self, route: ProviderConnection, payload: Mapping[str, object],
                   key: str) -> Iterator[object]:
            attempts.append(dict(payload))
            if packed and len(attempts) == 1:
                yield b"".join(_prefix())
                return
            yield {"type": "message_start", "message": {"id": "synthetic-message",
                   "type": "message", "role": "assistant", "content": []}}
            yield {"type": "content_block_start", "index": 0,
                   "content_block": {"type": "text", "text": ""}}
            yield {"type": "content_block_delta", "index": 0,
                   "delta": {"type": "text_delta", "text": "The" if len(attempts) == 1
                             else "Analysis can continue."}}
            if len(attempts) == 1:
                return  # Exact failure shape: short prefix, EOF, no message_stop.
            yield {"type": "content_block_stop", "index": 0}
            yield {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}
            yield {"type": "message_stop"}

    registry = ProviderRegistry(tmp_path / "providers")
    registry.add(ProviderConnection("test", "test", "openai", "https://example.com",
                                    "TEST_KEY"), approved=True, credential="synthetic-key")
    server = ModelGatewayServer(registry, token="g" * 32, port=0, backend=Backend())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        payload = {"model": "test", "max_tokens": 32, "stream": True, "messages": [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "done-call",
             "name": "write", "input": {"path": "done.txt"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "done-call",
             "content": "already written"}]},
        ]}
        request = Request(f"http://127.0.0.1:{server.port}/v1/messages",
                          data=json.dumps(payload).encode(),
                          headers={"Content-Type": "application/json", "x-api-key": "g" * 32})
        with build_opener(ProxyHandler({})).open(request, timeout=5) as response:
            wire = response.read().decode()
        assert "XDSH_STREAM_INCOMPLETE" not in wire
        assert "Analysis can continue." in wire
        assert '"text": "The"' not in wire
        assert len(attempts) == 2
        assert attempts[0] == attempts[1] == payload
    finally:
        server.close()
        thread.join(2)


def _prefix(text: str = "The") -> list[bytes]:
    return [_chunk({"type": "message_start", "message": {
        "id": "synthetic", "role": "assistant", "content": []}}),
        _chunk({"type": "content_block_start", "index": 0,
                "content_block": {"type": "text", "text": ""}}),
        _chunk({"type": "content_block_delta", "index": 0,
                "delta": {"type": "text_delta", "text": text}})]


def test_exhaustion_is_bounded_and_retains_last_partial_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits: list[float] = []
    monkeypatch.setattr("xdsh.ops.stream_start.time.sleep", waits.append)
    reopened: list[int] = []
    retries: list[int] = []

    def reopen() -> Iterator[object]:
        reopened.append(1)
        return iter(_prefix())

    result = b"".join(stream_with_start_recovery(
        iter(_prefix()), reopen=reopen, encode=_chunk, retries=2, on_retry=retries.append))
    assert reopened == [1, 1] and retries == [1, 2] and waits == [0.5, 1.0]
    assert result.count(b'"text": "The"') == 1


@pytest.mark.parametrize("events", [
    _prefix("x" * 65),
    [*_prefix(), _chunk({"type": "error", "error": {"type": "rate_limit_error"}})],
    [*_prefix(), _chunk({"type": "content_block_start", "index": 1,
                        "content_block": {"type": "tool_use", "id": "call", "name": "write"}})],
    [*_prefix(), _chunk({"type": "content_block_start", "index": 1,
                        "content_block": {"type": "server_tool_use", "name": "web_search"}})],
    [*_prefix(), _chunk({"type": "content_block_start", "index": 1,
                        "content_block": {"type": "thinking", "thinking": ""}})],
    [*_prefix(), _chunk({"type": "message_stop"})],
    [*_prefix(), b"data: not-json\n\n"],
    [*_prefix(), b"data: {\"type\":\"incomplete"],
    [b": " + b" " * 8192 + b"\n\n"],
    [],
])
def test_committed_unknown_tool_and_explicit_error_streams_never_replay(
    events: list[bytes],
) -> None:
    def forbidden() -> Iterator[object]:
        pytest.fail("committed, empty, malformed or effectful stream must not replay")

    wire = b"".join(stream_with_start_recovery(
        iter(events), reopen=forbidden, encode=_chunk, retries=2,
        on_retry=lambda attempt: pytest.fail("unexpected retry")))
    assert wire == b"".join(events)


def test_slow_next_event_flushes_prefix_without_waiting_for_provider() -> None:
    release = threading.Event()
    closed = threading.Event()

    def slow() -> Iterator[object]:
        try:
            yield from _prefix()
            release.wait(2)
        finally:
            closed.set()

    def forbidden() -> Iterator[object]:
        pytest.fail("deadline-committed prefix must not replay")

    stream = stream_with_start_recovery(
        slow(), reopen=forbidden, encode=_chunk, retries=2,
        on_retry=lambda attempt: pytest.fail("unexpected retry"))
    start = time.monotonic()
    try:
        first = next(stream)
        elapsed = time.monotonic() - start
        assert b"message_start" in first
        assert 0.20 <= elapsed < 1.5
        release.set()
        assert b"The" in b"".join(stream)
        assert closed.wait(1)
    finally:
        release.set()
        stream.close()


def test_close_during_recovery_never_reopens() -> None:
    closed: list[bool] = []

    def source() -> Iterator[object]:
        try:
            yield from _prefix()
        finally:
            closed.append(True)

    def forbidden() -> Iterator[object]:
        pytest.fail("cancelled consumer must not retry")

    stream = stream_with_start_recovery(source(), reopen=forbidden, encode=_chunk,
                                        retries=2, on_retry=lambda attempt: None)
    assert next(stream).startswith(b": xdsh recovering")
    stream.close()
    assert closed == [True]


def test_fragmented_crlf_prefix_can_recover(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("xdsh.ops.stream_start.time.sleep", lambda delay: None)
    first = b"".join(_prefix()).replace(b"\n", b"\r\n")
    second = [*_prefix("done"), _chunk({"type": "message_stop"})]
    calls: list[int] = []

    def reopen() -> Iterator[object]:
        calls.append(1)
        return iter(second)

    wire = b"".join(stream_with_start_recovery(iter(bytes([b]) for b in first), reopen=reopen,
                                              encode=_chunk, retries=2,
                                              on_retry=lambda attempt: None))
    assert calls == [1] and b'"text": "The"' not in wire and b'done' in wire


def test_server_tools_disable_recovery() -> None:
    assert client_tools_only({})
    assert client_tools_only({"tools": [{"name": "bash", "input_schema": {"type": "object"}}]})
    assert not client_tools_only({"tools": [{"type": "web_search_20250305", "name": "web_search"}]})
    assert not client_tools_only({"tools": "malformed"})


@pytest.mark.parametrize("failure", [TimeoutError, RuntimeError])
def test_exceptions_retain_prefix_and_are_not_nested_retried(failure: type[Exception]) -> None:
    def source() -> Iterator[object]:
        yield from _prefix()
        raise failure("synthetic failure")

    stream = stream_with_start_recovery(source(), reopen=lambda: iter(()), encode=_chunk,
                                        retries=2, on_retry=lambda n: pytest.fail("retry"))
    chunks: list[bytes] = []
    with pytest.raises(failure):
        chunks.extend(stream)
    assert b"The" in b"".join(chunks)
