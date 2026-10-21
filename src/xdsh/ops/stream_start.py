"""Bounded recovery before a Messages stream commits any model output.

Only a valid, short text prefix ending in EOF is eligible. Tool/server events,
explicit errors, large output, or a 250ms deadline commit the original stream.
Committed output is never replayed. This is request recovery, not task replay.
"""

from __future__ import annotations

import contextlib
import json
import queue
import threading
import time
from collections.abc import Callable, Generator, Iterator, Mapping
from typing import cast

_PREFIX_BYTES = 8192
_PREFIX_CHARS = 64
_PREFIX_SECONDS = 0.25


def client_tools_only(payload: Mapping[str, object]) -> bool:
    """Server-side tools may have effects even before their events arrive."""
    tools = payload.get("tools", [])
    return isinstance(tools, list) and all(
        isinstance(tool, dict) and "type" not in tool
        and isinstance(tool.get("name"), str) and isinstance(tool.get("input_schema"), dict)
        for tool in tools
    )


class _Prefix:
    def __init__(self) -> None:
        self.pending = b""
        self.size = 0
        self.text_chars = 0
        self.started = False
        self.safe = True

    def feed(self, chunk: bytes) -> None:
        self.size += len(chunk)
        if self.size > _PREFIX_BYTES:
            self.safe = False
            return
        self.pending = (self.pending + chunk).replace(b"\r\n", b"\n")
        frames = self.pending.split(b"\n\n")
        self.pending = frames.pop()
        for frame in frames:
            data = b"\n".join(line[5:].lstrip(b" ") for line in frame.split(b"\n")
                              if line.startswith(b"data:"))
            if not data:
                continue
            try:
                event = json.loads(data)
            except (ValueError, UnicodeError):
                self.safe = False
                return
            if not isinstance(event, dict):
                self.safe = False
                return
            kind = event.get("type")
            if kind == "ping":
                continue
            if kind == "message_start" and not self.started:
                message = event.get("message", {})
                self.started = bool(isinstance(message, dict)
                                    and message.get("role") == "assistant"
                                    and message.get("content") == [])
                self.safe &= self.started
            elif kind == "content_block_start" and self.started:
                block = event.get("content_block", {})
                self.safe &= (isinstance(block, dict) and block.get("type") == "text"
                              and block.get("text", "") == "")
            elif kind == "content_block_delta" and self.started:
                delta = event.get("delta", {})
                if (not isinstance(delta, dict) or delta.get("type") != "text_delta"
                        or not isinstance(delta.get("text"), str)):
                    self.safe = False
                    return
                self.text_chars += len(delta["text"])
                self.safe &= self.text_chars <= _PREFIX_CHARS
            else:
                # Includes tool/thinking blocks, message_stop/error and all
                # unknown extensions: expose them immediately, never replay.
                self.safe = False

    @property
    def retryable(self) -> bool:
        return self.safe and self.started and self.text_chars > 0 and not self.pending.strip()


class _Pump:
    """One-item lookahead so a quiet provider cannot hide the prefix indefinitely."""

    def __init__(self, source: Iterator[object]) -> None:
        self.source = source
        self.items: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=1)
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._run, name="xdsh-stream-start", daemon=True)
        self.thread.start()

    def _put(self, item: tuple[str, object]) -> None:
        while not self.stopped.is_set():
            try:
                self.items.put(item, timeout=0.05)
                return
            except queue.Full:
                continue

    def _run(self) -> None:
        try:
            for item in self.source:
                if self.stopped.is_set():
                    break
                self._put(("item", item))
        except BaseException as error:
            self._put(("error", error))
        else:
            self._put(("eof", None))
        finally:
            self._close_source()

    def _close_source(self) -> None:
        close = getattr(self.source, "close", None)
        if callable(close):
            with contextlib.suppress(Exception):
                close()

    def close(self) -> None:
        self.stopped.set()
        self._close_source()
        self.thread.join(0.25)


def stream_with_start_recovery(
    source: Iterator[object],
    *,
    reopen: Callable[[], Iterator[object]],
    encode: Callable[[object], bytes],
    retries: int,
    on_retry: Callable[[int], None],
) -> Generator[bytes, None, None]:
    """Retry at most ``retries`` short, uncommitted EOFs on the same request.

    Comments may be sent during recovery, never failed model text. An explicit
    upstream error or exception is propagated unchanged, not nested-retried.
    The caller must forbid recovery for server-side tools. Always close this
    generator when the downstream disconnects or reaches a terminal event.
    """
    for attempt in range(retries + 1):
        prefix = _Prefix()
        buffered: list[bytes] = []
        deadline: float | None = None
        committed = retries == 0
        pump = _Pump(source)
        recover = False
        try:
            while True:
                timeout = None if committed or deadline is None else max(
                    0.0, deadline - time.monotonic())
                try:
                    kind, item = pump.items.get(timeout=timeout)
                except queue.Empty:
                    committed = True
                    yield from buffered
                    buffered.clear()
                    continue
                if not committed and deadline is not None and time.monotonic() >= deadline:
                    committed = True
                    yield from buffered
                    buffered.clear()
                if kind == "eof":
                    recover = not committed and prefix.retryable and attempt < retries
                    if not recover:
                        yield from buffered
                    break
                if kind == "error":
                    yield from buffered
                    raise cast(BaseException, item)
                chunk = encode(item)
                if committed:
                    yield chunk
                    continue
                if deadline is None:
                    deadline = time.monotonic() + _PREFIX_SECONDS
                prefix.feed(chunk)
                buffered.append(chunk)
                if not prefix.safe or time.monotonic() >= deadline:
                    committed = True
                    yield from buffered
                    buffered.clear()
        finally:
            pump.close()
        if not recover:
            return
        on_retry(attempt + 1)
        # Flush a protocol comment first: a disconnected/cancelled consumer
        # stops iteration here, before any additional model request is opened.
        yield b": xdsh recovering uncommitted stream prefix\n\n"
        time.sleep(0.5 * (2 ** attempt))
        source = reopen()
