"""Loopback-only authenticated HTTP adapter for RestrictedMcpGateway."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import threading
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Literal
from urllib.parse import urlsplit

from xdsh.core.admission import BoundedAdmission
from xdsh.core.external import (
    AuditSink,
    CredentialRef,
    ExternalAuditEvent,
    ExternalAuthError,
)
from xdsh.core.external_gateway import RestrictedMcpGateway
from xdsh.core.product_control import safe_exception_text


class InternalGatewayConfigurationError(ValueError):
    """The optional DSH-facing MCP gateway is partially or unsafely configured."""


@dataclass(frozen=True, slots=True)
class InternalGatewayConfig:
    """Secret-free loopback gateway settings."""

    enabled: bool = False
    port: int = 8765
    credential: CredentialRef | None = None
    max_request_bytes: int = 100_000
    max_concurrent_requests: int = 1
    request_timeout_seconds: float = 30.0


def internal_gateway_config_from_env(
    environment: Mapping[str, str] | None = None,
) -> InternalGatewayConfig:
    """Validate the internal gateway without retaining any credential value."""

    env = os.environ if environment is None else environment
    raw_enabled = env.get("XDSH_INTERNAL_MCP_ENABLED", "0").strip()
    if raw_enabled not in {"0", "1"}:
        raise InternalGatewayConfigurationError("XDSH_INTERNAL_MCP_ENABLED must be 0 or 1")
    enabled = raw_enabled == "1"
    try:
        port = int(env.get("XDSH_INTERNAL_MCP_PORT", "8765"))
        max_request_bytes = int(env.get("XDSH_INTERNAL_MCP_MAX_REQUEST_BYTES", "100000"))
        max_concurrent = int(env.get("XDSH_INTERNAL_MCP_MAX_CONCURRENT", "1"))
        request_timeout = float(
            env.get("XDSH_INTERNAL_MCP_REQUEST_TIMEOUT_SECONDS", "30")
        )
    except ValueError as error:
        raise InternalGatewayConfigurationError(
            "internal MCP numeric setting is invalid"
        ) from error
    if not 1_024 <= port <= 65_535:
        raise InternalGatewayConfigurationError("internal MCP port must use 1024-65535")
    if not 1_000 <= max_request_bytes <= 1_000_000:
        raise InternalGatewayConfigurationError("internal MCP request limit is unsafe")
    if not 1 <= max_concurrent <= 16:
        raise InternalGatewayConfigurationError("internal MCP concurrency limit is unsafe")
    if not 0.5 <= request_timeout <= 120.0:
        raise InternalGatewayConfigurationError("internal MCP request timeout is unsafe")
    env_name = env.get("XDSH_INTERNAL_MCP_CREDENTIAL_ENV", "").strip()
    credential: CredentialRef | None = None
    if env_name:
        try:
            credential = CredentialRef(env_name)
        except ValueError as error:
            raise InternalGatewayConfigurationError(
                "internal MCP credential reference is invalid"
            ) from error
    if enabled and credential is None:
        raise InternalGatewayConfigurationError(
            "enabled internal MCP gateway requires a credential env reference"
        )
    if enabled and credential is not None:
        try:
            credential.header(env)
        except ExternalAuthError as error:
            raise InternalGatewayConfigurationError(
                "internal MCP credential environment value is unavailable"
            ) from error
    return InternalGatewayConfig(
        enabled=enabled,
        port=port,
        credential=credential,
        max_request_bytes=max_request_bytes,
        max_concurrent_requests=max_concurrent,
        request_timeout_seconds=request_timeout,
    )


class GatewayRequestError(RuntimeError):
    """A loopback HTTP request failed before JSON-RPC dispatch."""

    def __init__(
        self,
        status: int,
        message: str,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.headers = dict(headers or {})


class LoopbackGatewayPolicy:
    """Pure request guard shared by hermetic tests and the socket adapter."""

    def __init__(
        self,
        *,
        port: int,
        credential: CredentialRef,
        environment: Mapping[str, str] | None = None,
        max_request_bytes: int = 100_000,
    ) -> None:
        if not 1 <= port <= 65_535:
            raise ValueError("MCP gateway policy port is invalid")
        if not 1_000 <= max_request_bytes <= 1_000_000:
            raise ValueError("MCP gateway request limit is unsafe")
        self.port = port
        self.credential = credential
        self.environment = environment
        self.max_request_bytes = max_request_bytes

    def authorize(
        self,
        *,
        host: str,
        origin: str | None,
        headers: Mapping[str, str],
    ) -> None:
        normalized_host = host.strip().casefold().rstrip(".")
        if normalized_host not in {
            f"127.0.0.1:{self.port}",
            f"localhost:{self.port}",
        }:
            raise GatewayRequestError(403, "request rejected")
        if origin is not None:
            try:
                parsed = urlsplit(origin)
                origin_port = parsed.port
            except ValueError as error:
                raise GatewayRequestError(403, "request rejected") from error
            if origin_port is None:
                origin_port = 80 if parsed.scheme == "http" else 443
            if not (
                parsed.scheme in {"http", "https"}
                and (parsed.hostname or "").casefold().rstrip(".")
                in {"127.0.0.1", "localhost"}
                and origin_port == self.port
                and parsed.username is None
                and parsed.password is None
                and parsed.path in {"", "/"}
                and not parsed.query
                and not parsed.fragment
            ):
                raise GatewayRequestError(403, "request rejected")
        try:
            header_name, expected = self.credential.header(self.environment)
        except ExternalAuthError as error:
            raise GatewayRequestError(503, "gateway unavailable") from error
        supplied = _header(headers, header_name)
        if not hmac.compare_digest(supplied.encode(), expected.encode()):
            raise GatewayRequestError(
                401,
                "authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )

    def content_length(self, headers: Mapping[str, str]) -> int:
        if _header(headers, "Transfer-Encoding"):
            raise GatewayRequestError(400, "streaming request bodies are not allowed")
        content_type = _header(headers, "Content-Type").split(";", 1)[0].strip()
        if content_type != "application/json":
            raise GatewayRequestError(415, "application/json required")
        try:
            length = int(_header(headers, "Content-Length"))
        except ValueError as error:
            raise GatewayRequestError(411, "bounded Content-Length required") from error
        if not 1 <= length <= self.max_request_bytes:
            raise GatewayRequestError(413, "request exceeds limit")
        return length


class LoopbackMcpHttpServer:
    """Expose one stateless gateway without publishing a non-loopback socket."""

    def __init__(
        self,
        gateway: RestrictedMcpGateway,
        *,
        bind_host: str,
        port: int,
        credential: CredentialRef,
        environment: Mapping[str, str] | None = None,
        audit: AuditSink | None = None,
        max_request_bytes: int = 100_000,
        max_concurrent_requests: int = 1,
        request_timeout_seconds: float = 30.0,
    ) -> None:
        if bind_host != "127.0.0.1":
            raise ValueError("MCP gateway must bind to explicit IPv4 loopback")
        if not 0 <= port <= 65_535:
            raise ValueError("MCP gateway port is invalid")
        if not 1_000 <= max_request_bytes <= 1_000_000:
            raise ValueError("MCP gateway request limit is unsafe")
        if not 1 <= max_concurrent_requests <= 16:
            raise ValueError("MCP gateway concurrency limit is unsafe")
        if not 0.5 <= request_timeout_seconds <= 120.0:
            raise ValueError("MCP gateway request timeout is unsafe")
        self.gateway = gateway
        self.bind_host = bind_host
        self.credential = credential
        self.audit = audit
        self.max_request_bytes = max_request_bytes
        self.request_timeout_seconds = request_timeout_seconds
        self._capacity = BoundedAdmission(
            workers=max_concurrent_requests, pending=4,
            wait_seconds=min(5.0, request_timeout_seconds / 4),
        )
        handler = self._handler_type()
        self._server = ThreadingHTTPServer((bind_host, port), handler)
        self._server.daemon_threads = True
        self._policy = LoopbackGatewayPolicy(
            port=int(self._server.server_address[1]),
            credential=credential,
            environment=environment,
            max_request_bytes=max_request_bytes,
        )
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("MCP gateway server is already started")
        thread = threading.Thread(
            target=self._server.serve_forever,
            name="xdsh-restricted-mcp",
            daemon=True,
        )
        self._thread = thread
        thread.start()

    def close(self) -> None:
        thread = self._thread
        if thread is not None:
            self._server.shutdown()
            thread.join(timeout=5.0)
            self._thread = None
        self._server.server_close()

    def _handler_type(self) -> type[BaseHTTPRequestHandler]:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "XDSH-MCP/1"
            sys_version = ""

            def setup(self) -> None:
                super().setup()
                self.connection.settimeout(owner.request_timeout_seconds)

            def do_POST(self) -> None:
                owner._handle(self)

            def do_GET(self) -> None:
                owner._reply(self, 405, {"error": "method not allowed"})

            def log_message(self, format: str, *args: object) -> None:
                del format, args

        return Handler

    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        request_id = f"gateway-{uuid.uuid4().hex[:16]}"
        try:
            target = urlsplit(handler.path)
        except ValueError:
            self._audit(request_id, "blocked", "request target is invalid")
            self._reply(handler, 400, {"error": "request target is invalid"})
            return
        if (
            target.path != "/mcp"
            or target.query
            or target.fragment
            or target.scheme
            or target.netloc
        ):
            self._reply(handler, 404, {"error": "not found"})
            return
        security_headers = (
            "Host",
            "Authorization",
            "Content-Type",
            "Content-Length",
            "Origin",
            "Transfer-Encoding",
        )
        duplicate_names = {
            name.casefold()
            for name in security_headers
            if len(handler.headers.get_all(name) or []) > 1
        }
        if duplicate_names:
            self._audit(request_id, "blocked", "duplicate security header")
            self._reply(handler, 400, {"error": "duplicate security header"})
            return
        content_lengths = handler.headers.get_all("Content-Length") or []
        if len(content_lengths) != 1:
            self._audit(request_id, "blocked", "exactly one content length is required")
            self._reply(handler, 411, {"error": "bounded Content-Length required"})
            return
        try:
            headers = {str(key): str(value) for key, value in handler.headers.items()}
            self._policy.authorize(
                host=handler.headers.get("Host", ""),
                origin=handler.headers.get("Origin"),
                headers=headers,
            )
            length = self._policy.content_length(headers)
        except GatewayRequestError as error:
            self._audit(
                request_id,
                "failed" if error.status == 503 else "blocked",
                safe_exception_text(error, max_chars=400),
            )
            self._reply(
                handler,
                error.status,
                {"error": safe_exception_text(error, max_chars=400)},
                extra_headers=error.headers,
            )
            return
        if not self._capacity.acquire():
            self._audit(request_id, "blocked", "gateway concurrency limit reached")
            self._reply(handler, 429, {"error": "gateway concurrency limit reached"},
                        extra_headers={"Retry-After": "2"})
            return
        try:
            try:
                body = handler.rfile.read(length)
            except (TimeoutError, OSError):
                self._audit(request_id, "failed", "request read timeout")
                self._reply(handler, 408, {"error": "request timeout"})
                return
            if len(body) != length:
                self._reply(handler, 400, {"error": "request body is invalid"})
                return
            try:
                payload = json.loads(
                    body.decode("utf-8"),
                    parse_constant=_reject_json_constant,
                    object_pairs_hook=_reject_duplicate_json_keys,
                )
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                self._audit(request_id, "blocked", "request JSON is invalid")
                self._reply(handler, 400, {"error": "request JSON is invalid"})
                return
            method = payload.get("method") if isinstance(payload, Mapping) else None
            try:
                response = asyncio.run(
                    asyncio.wait_for(
                        self.gateway.dispatch(payload),
                        timeout=self.request_timeout_seconds,
                    )
                )
            except asyncio.TimeoutError:
                self._audit(request_id, "failed", "gateway dispatch timeout")
                self._reply(handler, 504, {"error": "gateway timeout"})
                return
            except Exception:
                self._audit(request_id, "failed", "gateway dispatch failed")
                self._reply(handler, 502, {"error": "gateway unavailable"})
                return
            if response is None:
                self._reply_empty(handler, 202)
            else:
                self._reply(handler, 200, response)
            self._audit(
                request_id,
                "completed",
                f"method:{method}" if isinstance(method, str) else "method:invalid",
            )
        finally:
            self._capacity.release()

    def _reply(
        self,
        handler: BaseHTTPRequestHandler,
        status: int,
        payload: Mapping[str, object],
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        body = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (extra_headers or {}).items():
            handler.send_header(name, value)
        handler.end_headers()
        handler.wfile.write(body)

    def _reply_empty(self, handler: BaseHTTPRequestHandler, status: int) -> None:
        handler.send_response(status)
        handler.send_header("Content-Length", "0")
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()

    def _audit(
        self,
        request_id: str,
        outcome: Literal["allowed", "blocked", "completed", "failed"],
        reason: str | None = None,
    ) -> None:
        if self.audit is None:
            return
        self.audit.record(
            ExternalAuditEvent(
                request_id=request_id,
                operation="gateway.mcp",
                outcome=outcome,
                host="127.0.0.1",
                url_path="/mcp",
                credential_ref=self.credential.env_name,
                reason=reason,
            )
        )


def _header(headers: Mapping[str, str], name: str) -> str:
    wanted = name.casefold()
    for key, value in headers.items():
        if str(key).casefold() == wanted:
            return str(value)
    return ""


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON constant: {value}")


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result
