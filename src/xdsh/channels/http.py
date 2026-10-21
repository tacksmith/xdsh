"""Loopback-only HTTP webhook host for runner-neutral channel adapters."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Mapping
from concurrent.futures import Future
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit

from xdsh.channels.base import (
    MAX_REQUEST_HEADER_BYTES,
    MAX_REQUEST_HEADERS,
    MAX_WEBHOOK_BYTES,
    ChannelAuthError,
    ChannelConfigurationError,
    ChannelError,
    ChannelHttpResponse,
    ChannelPayloadError,
    ChannelResourceLimitError,
    WebhookDispatch,
)
from xdsh.channels.router import ChannelRouter, MessageHandler

LOGGER = logging.getLogger(__name__)


class ChannelHttpServer:
    """Serve one configured channel on a loopback address.

    The server returns the provider handshake/deferred response immediately
    and schedules the agent turn on the asyncio loop.  It therefore satisfies
    Discord's short interaction acknowledgement window while retaining the
    same router/bridge for all other channels.
    """

    def __init__(
        self,
        router: ChannelRouter,
        *,
        channel: str,
        message_handler: MessageHandler,
        bind: str = "127.0.0.1",
        port: int = 8787,
        path: str | None = None,
        max_inflight: int = 32,
    ) -> None:
        if bind not in {"127.0.0.1", "localhost", "::1"}:
            raise ChannelConfigurationError("channel HTTP server must bind loopback")
        if not 0 <= port <= 65_535:
            raise ChannelConfigurationError("channel HTTP port is out of bounds")
        if not 1 <= max_inflight <= 256:
            raise ChannelConfigurationError("channel HTTP concurrency is out of bounds")
        normalized_channel = channel.casefold()
        if normalized_channel not in router.names:
            raise ChannelConfigurationError("channel adapter is not registered in the router")
        normalized_path = path or f"/webhook/{normalized_channel}"
        if (
            not normalized_path.startswith("/")
            or "?" in normalized_path
            or "#" in normalized_path
            or len(normalized_path) > 256
        ):
            raise ChannelConfigurationError("channel webhook path is invalid")
        self.router = router
        self.channel = normalized_channel
        self.message_handler = message_handler
        self.bind = bind
        self.port = port
        self.path = normalized_path
        self.max_inflight = max_inflight
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._inflight = 0
        self._inflight_lock = threading.Lock()

    @property
    def bound_port(self) -> int | None:
        server = self._httpd
        if server is None:
            return None
        return int(server.server_address[1])

    async def serve(self, stop_event: asyncio.Event) -> None:
        """Run until ``stop_event`` is set, then close the listener."""

        loop = asyncio.get_running_loop()
        self._loop = loop
        handler_type = self._handler_type()
        try:
            self._httpd = ThreadingHTTPServer((self.bind, self.port), handler_type)
        except OSError as error:
            raise ChannelConfigurationError("channel loopback listener could not start") from error
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            kwargs={"poll_interval": 0.2},
            name=f"xdsh-channel-{self.channel}",
            daemon=True,
        )
        self._thread.start()
        try:
            await stop_event.wait()
        finally:
            server = self._httpd
            if server is not None:
                await asyncio.to_thread(server.shutdown)
                server.server_close()
            thread = self._thread
            if thread is not None:
                await asyncio.to_thread(thread.join, 2.0)
            self._httpd = None
            self._thread = None
            self._loop = None

    def _handler_type(self) -> type[BaseHTTPRequestHandler]:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "XDSH-channel/1"
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                owner._handle_http(self, "GET")

            def do_POST(self) -> None:
                owner._handle_http(self, "POST")

            def log_message(self, format: str, *args: object) -> None:
                # Do not log request URLs: provider verification/query strings
                # can contain short-lived tokens.
                LOGGER.info("channel HTTP request: %s", format.split(" ", 1)[0])

        return Handler

    def _handle_http(self, request: BaseHTTPRequestHandler, method: str) -> None:
        try:
            parsed = urlsplit(request.path)
            if parsed.path != self.path:
                self._write_error(request, HTTPStatus.NOT_FOUND, "not found")
                return
            query = _query_mapping(parsed.query)
            headers = _read_headers(request)
            body = _read_body(request) if method == "POST" else b""
            dispatch = self.router.dispatch(
                self.channel,
                method,
                query=query,
                headers=headers,
                body=body,
            )
            if dispatch.message is not None and not self._reserve_slot():
                self._write_error(request, HTTPStatus.TOO_MANY_REQUESTS, "busy")
                return
            self._write_response(request, dispatch.response)
            if dispatch.message is not None:
                loop = self._loop
                if loop is None:
                    self._release_slot()
                    return
                future = asyncio.run_coroutine_threadsafe(
                    self._process(dispatch),
                    loop,
                )
                future.add_done_callback(self._processing_done)
        except (ChannelAuthError, ChannelPayloadError, ChannelResourceLimitError) as error:
            status = (
                HTTPStatus.UNAUTHORIZED
                if isinstance(error, ChannelAuthError)
                else HTTPStatus.BAD_REQUEST
            )
            self._write_error(request, status, "webhook rejected")
        except ChannelConfigurationError:
            self._write_error(request, HTTPStatus.SERVICE_UNAVAILABLE, "channel unavailable")
        except (ChannelError, OSError, ValueError):
            self._write_error(request, HTTPStatus.BAD_REQUEST, "webhook rejected")
        except Exception:
            LOGGER.exception("channel webhook dispatch failed")
            self._write_error(request, HTTPStatus.INTERNAL_SERVER_ERROR, "internal error")

    async def _process(self, dispatch: WebhookDispatch) -> None:
        try:
            await self.router.process(
                self.channel,
                dispatch,
                message_handler=self.message_handler,
            )
        finally:
            self._release_slot()

    def _processing_done(self, future: Future[None]) -> None:
        try:
            error = future.exception()
        except Exception:
            error = None
        if error is not None:
            LOGGER.warning("channel message processing failed: %s", type(error).__name__)

    def _reserve_slot(self) -> bool:
        with self._inflight_lock:
            if self._inflight >= self.max_inflight:
                return False
            self._inflight += 1
            return True

    def _release_slot(self) -> None:
        with self._inflight_lock:
            self._inflight = max(0, self._inflight - 1)

    @staticmethod
    def _write_response(request: BaseHTTPRequestHandler, response: ChannelHttpResponse) -> None:
        request.send_response(response.status)
        for key, value in response.headers.items():
            request.send_header(key, value)
        request.send_header("Content-Length", str(len(response.body)))
        request.send_header("Connection", "close")
        request.end_headers()
        if response.body:
            request.wfile.write(response.body)

    @staticmethod
    def _write_error(
        request: BaseHTTPRequestHandler,
        status: HTTPStatus,
        text: str,
    ) -> None:
        body = text.encode("utf-8")
        request.send_response(int(status))
        request.send_header("Content-Type", "text/plain; charset=utf-8")
        request.send_header("Content-Length", str(len(body)))
        request.send_header("Connection", "close")
        request.end_headers()
        request.wfile.write(body)


def _query_mapping(query: str) -> Mapping[str, str]:
    if len(query) > 16_384:
        raise ChannelResourceLimitError("webhook query exceeds its bound")
    pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=False, max_num_fields=64)
    values: dict[str, str] = {}
    for key, value in pairs:
        if key in values:
            raise ChannelPayloadError("webhook query repeats a field")
        values[key] = value
    return values


def _read_body(request: BaseHTTPRequestHandler) -> bytes:
    transfer_encoding = request.headers.get("Transfer-Encoding", "").strip()
    if transfer_encoding:
        # The bounded host deliberately accepts only a fixed-length body.  In
        # particular, do not let a chunked/framing variation bypass the byte
        # limit or make signature verification observe a different byte stream.
        raise ChannelPayloadError("chunked webhook bodies are not supported")
    raw_length = request.headers.get("Content-Length")
    if raw_length is None:
        raise ChannelPayloadError("webhook Content-Length is required")
    try:
        length = int(raw_length)
    except ValueError as error:
        raise ChannelPayloadError("webhook Content-Length is invalid") from error
    if length < 0 or length > MAX_WEBHOOK_BYTES:
        raise ChannelResourceLimitError("webhook body exceeds its bound")
    body = request.rfile.read(length)
    if len(body) != length:
        raise ChannelPayloadError("webhook body is truncated")
    return body


def _read_headers(request: BaseHTTPRequestHandler) -> dict[str, str]:
    """Copy a bounded, duplicate-free request-header mapping.

    ``EmailMessage.items()`` can combine duplicate fields, which is unsafe for
    signature-bearing headers.  ``raw_items`` preserves the wire fields so a
    repeated header is rejected instead of being silently selected/merged.
    """

    raw_items = tuple(request.headers.raw_items())
    if len(raw_items) > MAX_REQUEST_HEADERS:
        raise ChannelResourceLimitError("webhook contains too many headers")
    result: dict[str, str] = {}
    total_bytes = 0
    for raw_key, raw_value in raw_items:
        key = str(raw_key)
        value = str(raw_value)
        total_bytes += len(key.encode("utf-8")) + len(value.encode("utf-8"))
        if total_bytes > MAX_REQUEST_HEADER_BYTES:
            raise ChannelResourceLimitError("webhook headers exceed their byte bound")
        normalized = key.casefold()
        if normalized in {existing.casefold() for existing in result}:
            raise ChannelPayloadError("webhook repeats a header")
        result[key] = value
    return result
