"""Small loopback-only, exact-route host for the public LiteLLM Messages SDK.

This is an optional XDSH-owned gateway, not a DSH plugin. Configuration can be
added through the same registry used by TUI and Telegram, without restarting
the gateway. There is no wildcard, fallback, request-driven endpoint or admin
API. All protocol adaptation belongs to LiteLLM.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import hmac
import importlib
import json
import logging
import math
import os
import threading
import time
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from concurrent.futures import TimeoutError as FutureTimeoutError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Protocol, cast

import httpx

from xdsh.core.providers import (
    ProviderConnection,
    ProviderDnsError,
    ProviderError,
    ProviderRegistry,
    check_endpoint_dns,
)
from xdsh.core.rate_limit import parse_retry_after
from xdsh.ops.stream_start import client_tools_only, stream_with_start_recovery

_BODY_LIMIT = 2 * 1024 * 1024
_OUTPUT_LIMIT = 16 * 1024 * 1024
_LOG = logging.getLogger(__name__)
_FIELDS = frozenset(
    {
        "model",
        "messages",
        "max_tokens",
        "stream",
        "system",
        "temperature",
        "top_p",
        "top_k",
        "stop_sequences",
        "tools",
        "tool_choice",
        "thinking",
        "output_config",
        "metadata",
    }
)


class MessagesBackend(Protocol):
    def create(
        self,
        route: ProviderConnection,
        payload: Mapping[str, object],
        key: str,
    ) -> object: ...


class ProviderRequestError(RuntimeError):
    """No upstream request/credential body may cross the adapter exception seam."""

    def __init__(self, status: object, *, retry_after_seconds: object = None) -> None:
        self.status_code = status if isinstance(status, int) and 400 <= status <= 599 else 502
        self.retry_after_seconds = _safe_retry_after(retry_after_seconds)
        super().__init__(f"Provider request rejected (HTTP {self.status_code})")


class _IncompleteStream(ProviderRequestError):
    def __init__(self) -> None:
        super().__init__(502)


def _provider_status(error: BaseException) -> int:
    # asyncio and concurrent.futures use distinct timeout classes on Python 3.10.
    if isinstance(
        error, TimeoutError | asyncio.TimeoutError | FutureTimeoutError | httpx.TimeoutException,
    ):
        return 504
    status = getattr(error, "status_code", 502)
    if status == 408:
        return 504
    return status if isinstance(status, int) and 400 <= status <= 599 else 502


def _safe_retry_after(value: object) -> float | None:
    # Forward only a bounded numeric hint, never arbitrary header text. Long
    # quotas are not a reason to keep a model request/thread asleep for days.
    if isinstance(value, str) and (len(value) > 128 or "\r" in value or "\n" in value):
        return None
    try:
        delay = parse_retry_after(value)
    except (OverflowError, ValueError):
        return None
    return delay if delay is not None and delay <= 604_800 else None


def _provider_retry_after(error: BaseException) -> float | None:
    delay = _safe_retry_after(getattr(error, "retry_after_seconds", None))
    if delay is not None:
        return delay
    for holder in (error, getattr(error, "response", None)):
        headers = getattr(holder, "headers", None)
        if isinstance(headers, Mapping):
            for key in ("Retry-After", "retry-after"):
                delay = _safe_retry_after(headers.get(key))
                if delay is not None:
                    return delay
    return None


class LiteLlmMessagesBackend:
    stream_idle_seconds: float = 120.0

    def __init__(self) -> None:
        self.stream_idle_seconds = float(os.environ.get("XDSH_PROVIDER_STREAM_IDLE_SECONDS", "120"))
        if (not math.isfinite(self.stream_idle_seconds)
                or not 30 <= self.stream_idle_seconds <= 3600):
            raise ValueError("XDSH_PROVIDER_STREAM_IDLE_SECONDS must be finite and within 30..3600")
        os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
        self._sdk = importlib.import_module("litellm")
        # Importing the SDK installs/reconfigures its own loggers. Apply the
        # policy afterwards; XDSH's sanitized request-outcome logger stays on.
        for name in ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy", "httpx", "httpcore"):
            logging.getLogger(name).setLevel(logging.CRITICAL)
        self._sdk.__dict__.update(
            telemetry=False,
            set_verbose=False,
            suppress_debug_info=True,
            use_chat_completions_url_for_anthropic_messages=True,
        )
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever,
            name="xdsh-provider-sdk",
            daemon=True,
        )
        self._thread.start()

    def create(
        self,
        route: ProviderConnection,
        payload: Mapping[str, object],
        key: str,
    ) -> object:
        check_endpoint_dns(route.base_url)
        future = asyncio.run_coroutine_threadsafe(self._create(route, payload, key), self._loop)
        try:
            response, client, owner = future.result(timeout=self.stream_idle_seconds + 10)
        except FutureTimeoutError:
            future.cancel()
            raise ProviderRequestError(504) from None
        if payload.get("stream"):
            return _AsyncMessagesStream(self._loop, response, client, owner,
                                        idle_seconds=self.stream_idle_seconds)
        return response

    async def _create(
        self,
        route: ProviderConnection,
        payload: Mapping[str, object],
        key: str,
    ) -> tuple[object, object, object]:
        # One service loop prevents SDK background workers crossing per-request
        # loops. Each request still owns its client and incremental iterator.
        httpx = importlib.import_module("httpx")
        arguments = dict(payload)
        # Messages thinking on an OpenAI-compatible route otherwise triggers
        # LiteLLM's automatic Responses API switch. These routes explicitly
        # select Chat Completions; translate before that dispatch decision.
        if route.engine == "openai" and isinstance(arguments.get("thinking"), dict):
            thinking = cast(dict[str, object], arguments["thinking"])
            if thinking.get("type") == "enabled":
                budget = thinking.get("budget_tokens")
                if isinstance(budget, bool) or not isinstance(budget, int) or budget <= 0:
                    raise ProviderError("Enabled thinking requires a positive token budget")
                # RC2 sends display="summarized" by default. Chat Completions
                # has no equivalent display control; return only the upstream's
                # actual structured reasoning, never invent a summary. Explicit
                # non-default display/summary requests remain unsupported.
                if (set(thinking) - {"type", "budget_tokens", "display"}
                        or thinking.get("display", "summarized") != "summarized"):
                    raise ProviderError("Thinking options cannot be represented on this route")
                arguments.pop("thinking")
                arguments["reasoning_effort"] = (
                    "high" if budget >= 10_000 else "medium" if budget >= 5_000
                    else "low" if budget >= 2_000 else "minimal"
                )
                # An unknown model ID is not evidence that the upstream rejects
                # this user-selected field. Preserve it; do not drop or retry it.
                arguments["allowed_openai_params"] = ["reasoning_effort"]
        client = httpx.AsyncClient(follow_redirects=False, timeout=self.stream_idle_seconds,
                                   trust_env=False)
        arguments.update(
            model=f"{route.engine}/{route.model}",
            api_base=route.base_url,
            api_key=key,
            num_retries=0,
            timeout=self.stream_idle_seconds,
        )
        if route.api_version:
            arguments["api_version"] = route.api_version
        if route.engine in {"openai", "azure"}:
            # The Chat Completions adapter takes an OpenAI SDK client, unlike
            # the native Messages adapter's AsyncHTTPHandler.
            openai = importlib.import_module("openai")
            if route.engine == "azure":
                arguments["client"] = openai.AsyncAzureOpenAI(
                    api_key=key,
                    azure_endpoint=route.base_url,
                    api_version=route.api_version,
                    http_client=client,
                    max_retries=0,
                )
            else:
                arguments["client"] = openai.AsyncOpenAI(
                    api_key=key,
                    base_url=route.base_url,
                    http_client=client,
                    max_retries=0,
                )
        else:
            http_module = importlib.import_module("litellm.llms.custom_httpx.http_handler")
            handler = http_module.AsyncHTTPHandler(timeout=self.stream_idle_seconds)
            await handler.client.aclose()
            handler.client = client
            arguments["client"] = handler
        try:
            response = await self._sdk.anthropic.messages.acreate(**arguments)
            if not payload.get("stream"):
                await client.aclose()
            # AsyncHTTPHandler.__del__ schedules client.close(). Retain the
            # actual SDK owner until streaming ends; never construct a spare
            # handler for OpenAI/Azure that can close their shared client.
            return response, client, arguments["client"]
        except BaseException as error:
            await client.aclose()
            if isinstance(error, Exception):
                status = _provider_status(error)
                if isinstance(error, getattr(self._sdk, "Timeout", ())):
                    status = 504
                raise ProviderRequestError(
                    status,
                    retry_after_seconds=_provider_retry_after(error) if status == 429 else None,
                ) from None
            raise

    def close(self) -> None:
        if self._loop.is_closed():
            return

        async def drain() -> None:
            tasks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self._loop.shutdown_asyncgens()

        asyncio.run_coroutine_threadsafe(drain(), self._loop).result(timeout=5)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(5)
        self._loop.close()


class _AsyncMessagesStream(Iterator[object]):
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        response: object,
        client: object,
        owner: object | None = None,
        *,
        idle_seconds: float = 120.0,
    ) -> None:
        self._loop = loop
        self._response = cast(AsyncIterator[object], response).__aiter__()
        self._client = client
        self._owner = owner
        self._closed = False
        self._idle_seconds = idle_seconds

    def __next__(self) -> object:
        if self._closed or self._loop.is_closed():
            raise StopIteration

        async def take() -> object:
            return await self._response.__anext__()

        future = asyncio.run_coroutine_threadsafe(take(), self._loop)
        try:
            return future.result(timeout=self._idle_seconds + 10)
        except StopAsyncIteration:
            self.close()
            raise StopIteration from None
        except BaseException:
            future.cancel()
            self.close()
            raise

    def close(self) -> None:
        if self._closed or self._loop.is_closed():
            return
        self._closed = True

        async def finish() -> None:
            with contextlib.suppress(Exception):
                close = getattr(self._response, "aclose", None)
                if callable(close):
                    await close()
            close_client = getattr(self._client, "aclose", None)
            if callable(close_client):
                await close_client()

        asyncio.run_coroutine_threadsafe(finish(), self._loop).result(timeout=5)
        self._owner = None


def validate_message(payload: object, routes: Sequence[ProviderConnection]) -> ProviderConnection:
    if not isinstance(payload, dict) or set(payload) - _FIELDS:
        raise ProviderError("Unknown Messages fields; endpoint and credential overrides forbidden")
    model = payload.get("model")
    route = next((item for item in routes if item.model == model), None)
    if route is None:
        raise ProviderError("No explicit route for this exact model; no fallback was attempted")
    tokens = payload.get("max_tokens")
    if isinstance(tokens, bool) or not isinstance(tokens, int) or not 1 <= tokens <= 131_072:
        raise ProviderError("max_tokens is outside the supported range")
    messages = payload.get("messages")
    if not isinstance(messages, list) or not 1 <= len(messages) <= 2048:
        raise ProviderError("Messages must be a bounded nonempty array")
    if not all(
        isinstance(item, dict) and item.get("role") in {"user", "assistant"} for item in messages
    ):
        raise ProviderError("Invalid message role")
    if "stream" in payload and not isinstance(payload["stream"], bool):
        raise ProviderError("stream must be boolean")
    return route


def _document(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return cast(dict[str, object], value)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        result = dump()
        if isinstance(result, dict):
            return cast(dict[str, object], result)
    raise ProviderError("Unexpected LiteLLM response type")


def _chunk(value: object) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    document = _document(value)
    event = document.get("type")
    if not isinstance(event, str) or not event.replace("_", "").isalnum():
        raise ProviderError("Invalid structured stream event")
    return f"event: {event}\ndata: {json.dumps(document)}\n\n".encode()


class _StreamTermination:
    """Recognize protocol termination, including fragmented raw SDK SSE frames."""

    def __init__(self) -> None:
        self.pending = b""
        self.terminal: str | None = None

    def feed(self, chunk: bytes) -> None:
        self.pending = (self.pending + chunk).replace(b"\r\n", b"\n")
        frames = self.pending.split(b"\n\n")
        self.pending = frames.pop()
        for frame in frames:
            data = b"\n".join(line[5:].lstrip(b" ") for line in frame.split(b"\n")
                              if line.startswith(b"data:"))
            try:
                document = json.loads(data)
            except (ValueError, UnicodeError):
                continue
            if isinstance(document, dict) and document.get("type") in {"message_stop", "error"}:
                self.terminal = document["type"]
                return


class ModelGatewayServer:
    def __init__(
        self,
        registry: ProviderRegistry,
        *,
        token: str,
        port: int = 4001,
        backend: MessagesBackend | None = None,
        max_inflight: int = 4,
    ) -> None:
        if not 24 <= len(token) <= 512 or not token.isascii() or any(c.isspace() for c in token):
            raise ProviderError("Gateway credential must contain 24-512 nonspace ASCII characters")
        if not 1 <= max_inflight <= 16 or not 0 <= port <= 65535:
            raise ProviderError("Invalid gateway resource limits")
        self.registry = registry
        self._token_hash = hashlib.sha256(token.encode()).digest()
        # Official DSH owns bounded model-step retries. Do not multiply its
        # attempts by a second default gateway loop; prefix recovery is opt-in
        # for clients without that native facility.
        self._stream_start_retries = int(os.environ.get("XDSH_PROVIDER_STREAM_START_RETRIES", "0"))
        if not 0 <= self._stream_start_retries <= 2:
            raise ValueError("XDSH_PROVIDER_STREAM_START_RETRIES must be within 0..2")
        self.backend = backend or LiteLlmMessagesBackend()
        self._capacity = threading.BoundedSemaphore(max_inflight)
        self._server = ThreadingHTTPServer(("127.0.0.1", port), self._handler_type())
        self._server.daemon_threads = True

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def serve_forever(self) -> None:
        self._server.serve_forever()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        close_backend = getattr(self.backend, "close", None)
        if callable(close_backend):
            close_backend()

    def _handler_type(self) -> type[BaseHTTPRequestHandler]:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self) -> None:
                super().setup()
                self.connection.settimeout(10.0)

            def log_message(self, format: str, *args: object) -> None:
                pass  # Never emit paths, header values, prompts or provider exceptions.

            def authorize(self) -> bool:
                for name in ("Authorization", "x-api-key", "Host", "Content-Length"):
                    if len(self.headers.get_all(name) or []) > 1:
                        self.reply(400, {"error": "Duplicate security header"})
                        return False
                if (
                    self.headers.get("Host")
                    not in {
                        f"127.0.0.1:{owner.port}",
                        f"localhost:{owner.port}",
                    }
                    or self.headers.get("Origin")
                    or self.headers.get("Transfer-Encoding")
                ):
                    self.reply(403, {"error": "Request rejected"})
                    return False
                value = self.headers.get("Authorization", "")
                token = (
                    value.removeprefix("Bearer ") if value else self.headers.get("x-api-key", "")
                )
                candidate_hash = hashlib.sha256(token.encode()).digest()
                if not hmac.compare_digest(candidate_hash, owner._token_hash):
                    self.reply(401, {"error": "Authentication required"})
                    return False
                return True

            def reply(self, status: int, document: object,
                      *, retry_after: float | None = None) -> None:
                raw = json.dumps(document).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                if status == 429 and retry_after is not None:
                    self.send_header("Retry-After", str(math.ceil(retry_after)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:
                if not self.authorize():
                    return
                try:
                    routes = owner.registry.connections()
                    if self.path == "/model/info":
                        self.reply(
                            200,
                            {
                                "data": [
                                    {
                                        "model_name": route.model,
                                        "litellm_params": {
                                            "model": f"{route.engine}/{route.model}",
                                            "api_base": route.base_url,
                                        },
                                        "model_info": {
                                            "id": route.route_id,
                                            "xdsh_provider": route.provider,
                                        },
                                    }
                                    for route in routes
                                ]
                            },
                        )
                    elif self.path in {"/v1/models", "/models"}:
                        self.reply(200, {"data": [{"id": route.model} for route in routes]})
                    elif self.path == "/health":
                        self.reply(200, {"status": "ready", "routes": len(routes)})
                    else:
                        self.reply(404, {"error": "Not found"})
                except (ProviderError, OSError):
                    self.reply(503, {"error": "Provider registry unavailable"})

            def do_POST(self) -> None:
                if not self.authorize():
                    return
                if self.path != "/v1/messages":
                    self.reply(404, {"error": "Not found"})
                    return
                if not owner._capacity.acquire(blocking=False):
                    self.reply(429, {"error": "Gateway concurrency limit; retry later"})
                    return
                response: object = None
                stream: Iterator[bytes] | None = None
                streaming_started = False
                started = time.monotonic()
                phase = "request_validation"
                outcome = "rejected"
                event_count = 0
                first_event_seconds: float | None = None
                last_event_seconds: float | None = None
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= _BODY_LIMIT:
                        self.reply(413, {"error": "Bounded Content-Length required"})
                        return
                    if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                        self.reply(415, {"error": "JSON required"})
                        return
                    payload = json.loads(self.rfile.read(length))
                    route = validate_message(payload, owner.registry.connections())
                    key = owner.registry.credential(route)
                    phase = "upstream_open"
                    response = owner.backend.create(route, payload, key)
                    if payload.get("stream", False):
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.send_header("Cache-Control", "no-store")
                        self.send_header("Connection", "close")
                        self.send_header("x-litellm-model-id", route.route_id)
                        self.end_headers()
                        streaming_started = True
                        phase = "upstream_stream"
                        total = 0
                        termination = _StreamTermination()

                        def reopen() -> Iterator[object]:
                            nonlocal response
                            response = owner.backend.create(route, payload, key)
                            return cast(Iterator[object], response)

                        stream = stream_with_start_recovery(
                            cast(Iterator[object], response), reopen=reopen, encode=_chunk,
                            retries=(owner._stream_start_retries
                                     if client_tools_only(payload) else 0),
                            on_retry=lambda attempt: _LOG.warning(
                                "Gateway short stream EOF recovery attempt=%d; "
                                "no model output committed, no task/tool replay", attempt),
                        )
                        for chunk in stream:
                            event_count += 1
                            last_event_seconds = round(time.monotonic() - started, 3)
                            if first_event_seconds is None:
                                first_event_seconds = last_event_seconds
                            total += len(chunk)
                            if total > _OUTPUT_LIMIT:
                                raise ProviderError("Stream output limit reached")
                            termination.feed(chunk)
                            self.wfile.write(chunk)
                            self.wfile.flush()
                            if termination.terminal is not None:
                                break
                        if termination.terminal is None:
                            raise _IncompleteStream()
                        self.close_connection = True
                    else:
                        self.reply(200, _document(response))
                    outcome = (
                        "provider_error" if streaming_started and termination.terminal == "error"
                        else "completed"
                    )
                except (BrokenPipeError, ConnectionResetError):
                    outcome = "downstream_disconnected"
                    self.close_connection = True
                except Exception as error:
                    # Provider exceptions can contain headers and whole requests.
                    # Do not log, persist or return their string representations.
                    status = _provider_status(error)
                    if isinstance(error, ProviderError | ValueError):
                        status = 400
                    if isinstance(error, ProviderDnsError):
                        status = 503
                    error_type, message = {
                        400: ("invalid_request_error", "Provider rejected the request parameters"),
                        401: ("authentication_error", "Provider authentication failed"),
                        403: ("permission_error", "Provider denied access to this route"),
                        429: ("rate_limit_error",
                              "Provider rate limit reached; no replay attempted"),
                        504: ("timeout_error", "Provider response timed out; no replay attempted"),
                    }.get(status, ("api_error", "Provider request failed; no replay attempted"))
                    if isinstance(error, ProviderDnsError):
                        message = (
                            "[XDSH_PROVIDER_DNS] Local provider DNS/egress policy rejected "
                            "the endpoint; check operator proxy settings, not model credentials"
                        )
                    if isinstance(error, _IncompleteStream):
                        message = (
                            "[XDSH_STREAM_INCOMPLETE] Provider stream ended before completion; "
                            "partial output retained, no replay attempted"
                        )
                    elif status == 504 and streaming_started:
                        message = (
                            "[XDSH_PROVIDER_STREAM_IDLE] Provider event wait timed out; "
                            "partial output retained, no replay attempted"
                        )
                    outcome = error_type
                    retry_after = _provider_retry_after(error) if status == 429 else None
                    detail: dict[str, object] = {"type": error_type, "message": message}
                    if retry_after is not None:
                        detail["retry_after_seconds"] = retry_after
                    document = {
                        "type": "error",
                        "error": detail,
                    }
                    if streaming_started:
                        with contextlib.suppress(OSError):
                            self.wfile.write(_chunk(document))
                            self.wfile.flush()
                    else:
                        with contextlib.suppress(OSError):
                            self.reply(status, document, retry_after=retry_after)
                    self.close_connection = True
                finally:
                    close_stream = getattr(stream, "close", None)
                    if callable(close_stream):
                        close_stream()
                    # One bounded metadata-only record per request: no prompts,
                    # responses, model names, URLs, headers or credentials.
                    _LOG.log(
                        logging.INFO if outcome == "completed" else logging.WARNING,
                        "Gateway request outcome=%s phase=%s elapsed_seconds=%.3f "
                        "first_event_seconds=%s last_event_seconds=%s "
                        "silence_seconds=%.3f events=%d",
                        outcome, phase, time.monotonic() - started,
                        first_event_seconds, last_event_seconds,
                        max(0.0, time.monotonic() - started - (last_event_seconds or 0.0)),
                        event_count,
                    )
                    close = getattr(response, "close", None)
                    if callable(close):
                        with contextlib.suppress(Exception):
                            close()
                    owner._capacity.release()

        return Handler


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="xdsh-model-gateway")
    parser.add_argument(
        "--root",
        default=os.environ.get(
            "XDSH_PROVIDERS_ROOT",
            "~/.xdsh/config/providers",
        ),
    )
    parser.add_argument("--port", type=int, default=4001)
    parser.add_argument("--key-env", default="XDSH_LITELLM_API_KEY")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if args.check:
        from urllib.request import ProxyHandler, Request, build_opener

        request = Request(
            f"http://127.0.0.1:{args.port}/health",
            headers={"Authorization": f"Bearer {os.environ.get(args.key_env, '')}"},
        )
        try:
            with build_opener(ProxyHandler({})).open(request, timeout=3) as response:
                return 0 if response.status == 200 else 1
        except OSError:
            return 1
    server = ModelGatewayServer(
        ProviderRegistry(Path(args.root).expanduser()),
        token=os.environ.get(args.key_env, ""),
        port=args.port,
    )
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
