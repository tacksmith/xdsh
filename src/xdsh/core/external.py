"""Runner-neutral, authenticated external access with fail-closed limits.

The public interface in this module is deliberately small: callers validate a
URL through :class:`UrlPolicy`, fetch bounded untrusted Web text, or invoke an
explicitly allow-listed MCP/browser gateway.  The implementations keep
credentials and provider payloads out of the audit record and never follow a
redirect without validating the next destination.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import math
import os
import re
import socket
import time
import uuid
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from html.parser import HTMLParser
from http.client import HTTPMessage
from pathlib import Path
from typing import Literal, Protocol, TypeAlias, cast
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from xdsh.core.learning import contains_sensitive_text
from xdsh.core.product_control import redact_sensitive_text

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | dict[str, "JsonValue"] | list["JsonValue"]


class ExternalError(RuntimeError):
    """Base class for external access failures."""


class ExternalPolicyError(ExternalError):
    """A request, response, schema, or resource violates the safety policy."""


class ExternalProtocolError(ExternalError):
    """An authenticated provider returned an invalid or unsuccessful response."""


class ExternalAuthError(ExternalError):
    """A credential reference is missing or unsafe."""


class ExternalResourceLimitError(ExternalPolicyError):
    """A response, schema, or media-like payload exceeded a configured bound."""


class ExternalHttpError(ExternalProtocolError):
    """HTTP status without provider body, headers or credential-bearing URL."""

    def __init__(self, status: int) -> None:
        super().__init__(f"Web provider returned HTTP {status}")
        self.status = status


class ExternalBusyError(ExternalResourceLimitError):
    """A bounded local admission queue is full or its wait budget expired."""


class ExternalTimeoutError(ExternalResourceLimitError):
    """A bounded operation timed out, distinct from an oversized response."""


_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_HEADER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")
_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_SENSITIVE_QUERY_KEY = re.compile(
    r"(?i)(?:^|[_-])(api[_-]?key|token|secret|password|passwd|credential|signature|sig)(?:$|[_-])"
)
_SENSITIVE_KEY = re.compile(
    r"(?i)(?:api[_-]?key|token|secret|password|passwd|credential|authorization|cookie|private[_-]?key)"
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True, slots=True, kw_only=True)
class ExternalAuditEvent:
    """Content-free audit evidence for one external operation."""

    request_id: str
    operation: str
    outcome: Literal["allowed", "blocked", "completed", "failed"]
    host: str
    url_path: str
    status: int | None = None
    bytes_read: int = 0
    credential_ref: str | None = None
    reason: str | None = None
    at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}", self.request_id):
            raise ValueError("audit request ID is invalid")
        if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", self.operation):
            raise ValueError("audit operation is invalid")
        if not self.host or len(self.host) > 253 or _CONTROL.search(self.host):
            raise ValueError("audit host is invalid")
        if not self.url_path.startswith("/") or len(self.url_path) > 2048:
            raise ValueError("audit URL path is invalid")
        if self.status is not None and (
            not isinstance(self.status, int)
            or isinstance(self.status, bool)
            or not 100 <= self.status <= 599
        ):
            raise ValueError("audit status is invalid")
        if (
            not isinstance(self.bytes_read, int)
            or isinstance(self.bytes_read, bool)
            or self.bytes_read < 0
            or not math.isfinite(self.at)
            or self.at < 0
        ):
            raise ValueError("audit counters cannot be negative")
        for value in (self.credential_ref, self.reason):
            if value is not None and (len(value) > 240 or _CONTROL.search(value)):
                raise ValueError("audit detail is invalid")


class AuditSink(Protocol):
    """Small seam for durable or test audit implementations."""

    def record(self, event: ExternalAuditEvent) -> None: ...


class InMemoryAuditSink:
    """Test adapter that retains content-free external events."""

    def __init__(self) -> None:
        self.events: list[ExternalAuditEvent] = []

    def record(self, event: ExternalAuditEvent) -> None:
        self.events.append(event)


class SQLiteAuditLedger:
    """Mode-0600 append-only external audit ledger."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()

    def record(self, event: ExternalAuditEvent) -> None:
        import sqlite3
        from contextlib import closing

        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        with closing(sqlite3.connect(self.path, timeout=5.0)) as connection:
            with connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS external_audit (
                        id INTEGER PRIMARY KEY,
                        request_id TEXT NOT NULL,
                        operation TEXT NOT NULL,
                        outcome TEXT NOT NULL,
                        host TEXT NOT NULL,
                        url_path TEXT NOT NULL,
                        status INTEGER,
                        bytes_read INTEGER NOT NULL,
                        credential_ref TEXT,
                        reason TEXT,
                        at REAL NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO external_audit (
                        request_id, operation, outcome, host, url_path, status,
                        bytes_read, credential_ref, reason, at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.request_id,
                        event.operation,
                        event.outcome,
                        event.host,
                        event.url_path,
                        event.status,
                        event.bytes_read,
                        event.credential_ref,
                        event.reason,
                        event.at,
                    ),
                )
        os.chmod(self.path, 0o600)

    def recent(self, *, limit: int = 100) -> tuple[ExternalAuditEvent, ...]:
        import sqlite3
        from contextlib import closing

        if not 1 <= limit <= 1000:
            raise ValueError("audit limit must be between 1 and 1000")
        if not self.path.is_file():
            return ()
        with closing(sqlite3.connect(self.path, timeout=5.0)) as connection:
            rows = connection.execute(
                """
                SELECT request_id, operation, outcome, host, url_path, status,
                       bytes_read, credential_ref, reason, at
                FROM external_audit ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return tuple(
            ExternalAuditEvent(
                request_id=str(row[0]),
                operation=cast(str, row[1]),
                outcome=cast(Literal["allowed", "blocked", "completed", "failed"], str(row[2])),
                host=str(row[3]),
                url_path=str(row[4]),
                status=int(row[5]) if row[5] is not None else None,
                bytes_read=int(row[6]),
                credential_ref=str(row[7]) if row[7] is not None else None,
                reason=str(row[8]) if row[8] is not None else None,
                at=float(row[9]),
            )
            for row in rows
        )


class HostResolver(Protocol):
    """Resolver seam used to test DNS rebinding and private-address handling."""

    def resolve(self, hostname: str, port: int) -> tuple[str, ...]: ...


class SystemHostResolver:
    """Resolve all address families, failing closed on resolver errors."""

    def resolve(self, hostname: str, port: int) -> tuple[str, ...]:
        try:
            infos = socket.getaddrinfo(
                hostname,
                port,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
        except OSError as error:
            raise ExternalPolicyError("主機名稱無法安全解析") from error
        addresses = tuple(dict.fromkeys(str(info[4][0]) for info in infos))
        if not addresses:
            raise ExternalPolicyError("主機名稱沒有可用地址")
        return addresses


class StaticHostResolver:
    """Deterministic resolver adapter for isolated tests and probes."""

    def __init__(self, records: Mapping[str, Sequence[str]]) -> None:
        self._records = {
            _normalize_hostname(host): tuple(str(address) for address in addresses)
            for host, addresses in records.items()
        }

    def resolve(self, hostname: str, port: int) -> tuple[str, ...]:
        del port
        try:
            addresses = self._records[_normalize_hostname(hostname)]
        except KeyError as error:
            raise ExternalPolicyError("測試解析器沒有此主機") from error
        if not addresses:
            raise ExternalPolicyError("主機名稱沒有可用地址")
        return addresses


@dataclass(frozen=True, slots=True)
class NetworkPolicy:
    """Allowlist and resource limits for one external provider seam."""

    allowed_hosts: tuple[str, ...] = ()
    allowed_ports: tuple[int, ...] = (443,)
    allow_loopback: bool = False
    allow_cross_host_redirects: bool = False
    max_redirects: int = 3
    max_response_bytes: int = 2_000_000
    max_decompressed_bytes: int = 4_000_000
    max_url_chars: int = 2_048
    max_header_bytes: int = 32_768
    timeout_seconds: float = 15.0

    def __post_init__(self) -> None:
        hosts = tuple(_normalize_allowlist_host(host) for host in self.allowed_hosts)
        if len(hosts) != len(set(hosts)) or not hosts:
            raise ValueError("an explicit non-empty host allowlist is required")
        if len(hosts) > 128:
            raise ValueError("host allowlist exceeds the hard safety ceiling")
        object.__setattr__(self, "allowed_hosts", hosts)
        if (
            not self.allowed_ports
            or any(
                not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535
                for port in self.allowed_ports
            )
            or len(set(self.allowed_ports)) != len(self.allowed_ports)
        ):
            raise ValueError("network ports must be unique values from 1 to 65535")
        for name, value in (
            ("max_redirects", self.max_redirects),
            ("max_response_bytes", self.max_response_bytes),
            ("max_decompressed_bytes", self.max_decompressed_bytes),
            ("max_url_chars", self.max_url_chars),
            ("max_header_bytes", self.max_header_bytes),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.max_redirects > 10:
            raise ValueError("max_redirects must not exceed 10")
        hard_limits = {
            "max_response_bytes": 20_000_000,
            "max_decompressed_bytes": 40_000_000,
            "max_url_chars": 8_192,
            "max_header_bytes": 1_000_000,
        }
        for name, upper in hard_limits.items():
            if getattr(self, name) > upper:
                raise ValueError(f"{name} exceeds the hard safety ceiling")
        if self.max_decompressed_bytes < self.max_response_bytes:
            raise ValueError("decompressed limit must not be below raw response limit")
        if not 0.1 <= self.timeout_seconds <= 120.0:
            raise ValueError("network timeout must be between 0.1 and 120 seconds")


@dataclass(frozen=True, slots=True)
class ValidatedUrl:
    """A URL whose authority and all resolved addresses passed policy checks."""

    raw: str
    normalized: str
    scheme: Literal["http", "https"]
    hostname: str
    port: int
    addresses: tuple[str, ...]


class UrlPolicy:
    """Validate schemes, authorities, DNS results, and redirect destinations."""

    def __init__(self, policy: NetworkPolicy, *, resolver: HostResolver | None = None) -> None:
        self.policy = policy
        self.resolver = resolver or SystemHostResolver()

    def validate(self, url: str) -> ValidatedUrl:
        if not isinstance(url, str) or not url or len(url) > self.policy.max_url_chars:
            raise ExternalPolicyError("URL 超過安全限制")
        if _CONTROL.search(url) or any(character.isspace() for character in url):
            raise ExternalPolicyError("URL 含有控制字元或空白")
        try:
            parsed = urlsplit(url)
            hostname_raw = parsed.hostname
            port = parsed.port
        except ValueError as error:
            raise ExternalPolicyError("URL authority 無效") from error
        if parsed.scheme not in {"http", "https"} or hostname_raw is None:
            raise ExternalPolicyError("只允許 http/https URL")
        if parsed.username is not None or parsed.password is not None:
            raise ExternalPolicyError("URL 不得包含帳密")
        if parsed.fragment:
            raise ExternalPolicyError("URL 不得包含 fragment")
        if any(
            _SENSITIVE_QUERY_KEY.search(key)
            for key, _value in parse_qsl(parsed.query, keep_blank_values=True)
        ):
            raise ExternalPolicyError("URL query 不得攜帶憑據")
        hostname = _normalize_hostname(hostname_raw)
        if hostname.startswith(".") or hostname.endswith("."):
            raise ExternalPolicyError("URL 主機名稱無效")
        default_port = 443 if parsed.scheme == "https" else 80
        actual_port = default_port if port is None else port
        if actual_port not in self.policy.allowed_ports:
            raise ExternalPolicyError("URL port 不在 allowlist")
        if not _host_allowed(hostname, self.policy.allowed_hosts):
            raise ExternalPolicyError("URL 主機不在 allowlist")

        literal = _parse_literal_ip(hostname)
        if parsed.scheme == "http" and not (
            self.policy.allow_loopback and _is_loopback_name(hostname) and literal is not None
        ):
            raise ExternalPolicyError("外部存取只允許 HTTPS")
        if parsed.scheme == "http" and literal is None and hostname != "localhost":
            raise ExternalPolicyError("HTTP 只允許明確 loopback gateway")

        addresses = (
            (str(literal),) if literal is not None else self.resolver.resolve(hostname, actual_port)
        )
        normalized_addresses = tuple(
            dict.fromkeys(_normalize_address(address) for address in addresses)
        )
        if not normalized_addresses:
            raise ExternalPolicyError("URL 沒有可用解析地址")
        loopback_target = _is_loopback_name(hostname)
        for address in normalized_addresses:
            parsed_address = ipaddress.ip_address(address)
            if _blocked_address(parsed_address):
                if not (
                    self.policy.allow_loopback and loopback_target and parsed_address.is_loopback
                ):
                    raise ExternalPolicyError("URL 解析到私有、保留或特殊地址")
            elif loopback_target:
                raise ExternalPolicyError("loopback hostname 必須解析到 loopback address")

        normalized = urlunsplit(
            (
                parsed.scheme.lower(),
                _format_netloc(hostname, actual_port, parsed.scheme),
                parsed.path or "/",
                parsed.query,
                "",
            )
        )
        return ValidatedUrl(
            raw=url,
            normalized=normalized,
            scheme=cast(Literal["http", "https"], parsed.scheme.lower()),
            hostname=hostname,
            port=actual_port,
            addresses=normalized_addresses,
        )


@dataclass(frozen=True, slots=True)
class CredentialRef:
    """Reference to a credential in the process environment, never its value."""

    env_name: str
    header_name: str = "Authorization"
    scheme: Literal["bearer", "api_key"] = "bearer"

    def __post_init__(self) -> None:
        if _ENV_NAME.fullmatch(self.env_name) is None:
            raise ValueError("credential environment name is invalid")
        if _HEADER_NAME.fullmatch(self.header_name) is None:
            raise ValueError("credential header name is invalid")
        if self.header_name.casefold() in {
            "host",
            "content-length",
            "connection",
            "transfer-encoding",
        }:
            raise ValueError("credential header name is reserved")
        if self.scheme not in {"bearer", "api_key"}:
            raise ValueError("credential scheme is invalid")

    def header(self, environment: Mapping[str, str] | None = None) -> tuple[str, str]:
        source = os.environ if environment is None else environment
        value = source.get(self.env_name, "")
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 4096
            or _CONTROL.search(value)
            or any(character.isspace() for character in value)
        ):
            raise ExternalAuthError("外部 provider credential 未設定或不安全")
        header_name = self.header_name
        if self.scheme == "bearer":
            return header_name, f"Bearer {value}"
        return header_name, value


@dataclass(frozen=True, slots=True, kw_only=True)
class HttpRequest:
    method: str
    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes | None = None
    max_response_bytes: int = 2_000_000

    def __post_init__(self) -> None:
        if self.method.upper() not in {"GET", "POST"}:
            raise ValueError("external HTTP method is not allowed")
        if not self.url or len(self.url) > 4096:
            raise ValueError("external HTTP URL is invalid")
        if self.body is not None and len(self.body) > 1_000_000:
            raise ValueError("external HTTP request body is too large")
        header_size = sum(len(str(key)) + len(str(value)) for key, value in self.headers.items())
        if header_size > 32_768:
            raise ValueError("external HTTP headers are too large")
        if any(
            _CONTROL.search(str(key)) or _CONTROL.search(str(value))
            for key, value in self.headers.items()
        ):
            raise ValueError("external HTTP headers contain control characters")
        if (
            not isinstance(self.max_response_bytes, int)
            or isinstance(self.max_response_bytes, bool)
            or not 1 <= self.max_response_bytes <= 20_000_000
        ):
            raise ValueError("external HTTP response limit must be positive")


@dataclass(frozen=True, slots=True, kw_only=True)
class HttpResponse:
    status: int
    url: str
    headers: Mapping[str, str]
    body: bytes

    def __post_init__(self) -> None:
        if not 100 <= self.status <= 599:
            raise ValueError("external HTTP status is invalid")
        if not isinstance(self.body, bytes):
            raise ValueError("external HTTP body must be bytes")


class HttpTransport(Protocol):
    """Synchronous transport seam; production calls it in a worker thread."""

    def request(self, request: HttpRequest, *, timeout_seconds: float) -> HttpResponse: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self,
        request: Request,
        fp: object,
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> Request | None:
        del request, fp, code, msg, headers, newurl
        return None


class UrllibHttpTransport:
    """Bounded stdlib HTTP adapter with redirects disabled at the transport."""

    def __init__(self) -> None:
        # External requests must not inherit ambient HTTP(S)_PROXY variables.
        # An untrusted proxy could otherwise bypass the DNS/authority policy or
        # receive an authenticated request that was intended for an allowlisted
        # provider.  A deployment that needs a proxy must put it behind an
        # explicitly reviewed provider/gateway instead.
        self._opener = build_opener(_NoRedirect, ProxyHandler({}))

    def request(self, request: HttpRequest, *, timeout_seconds: float) -> HttpResponse:
        outbound = Request(
            request.url,
            data=request.body,
            headers=dict(request.headers),
            method=request.method.upper(),
        )
        try:
            response = self._opener.open(outbound, timeout=timeout_seconds)
        except HTTPError as error:
            body = error.read(request.max_response_bytes + 1)
            return HttpResponse(
                status=error.code,
                url=error.geturl() or request.url,
                headers={str(key): str(value) for key, value in error.headers.items()},
                body=body,
            )
        except (URLError, OSError) as error:
            raise ExternalProtocolError("外部 provider 網路請求失敗") from error
        with response:
            body = response.read(request.max_response_bytes + 1)
            headers = {str(key): str(value) for key, value in response.headers.items()}
            return HttpResponse(
                status=int(response.status),
                url=response.geturl() or request.url,
                headers=headers,
                body=body,
            )


@dataclass(frozen=True, slots=True)
class WebDocument:
    """Bounded, provenance-carrying untrusted Web text."""

    source_url: str
    final_url: str
    status: int
    content_type: str
    text: str
    sha256: str
    truncated: bool = False


class SafeWebFetcher:
    """Fetch allow-listed Web text with DNS, redirect, auth, and size guards."""

    def __init__(
        self,
        policy: UrlPolicy,
        *,
        transport: HttpTransport | None = None,
        audit: AuditSink | None = None,
        environment: Mapping[str, str] | None = None,
        max_text_chars: int = 100_000,
        preserve_html: bool = False,
    ) -> None:
        if not 1_000 <= max_text_chars <= 1_000_000:
            raise ValueError("Web text limit must be between 1000 and 1000000")
        self.policy = policy
        self.transport = transport or UrllibHttpTransport()
        self.audit = audit
        self.environment = environment
        self.max_text_chars = max_text_chars
        self.preserve_html = preserve_html

    async def fetch(
        self,
        url: str,
        *,
        credential: CredentialRef | None = None,
        max_bytes: int | None = None,
    ) -> WebDocument:
        request_id = _request_id()
        limit = self.policy.policy.max_response_bytes if max_bytes is None else max_bytes
        if not 1 <= limit <= self.policy.policy.max_response_bytes:
            raise ExternalResourceLimitError("Web response limit exceeds configured maximum")
        try:
            initial = self.policy.validate(url)
        except ExternalError as error:
            self._audit(
                ExternalAuditEvent(
                    request_id=request_id,
                    operation="web.fetch",
                    outcome="blocked" if isinstance(error, ExternalPolicyError) else "failed",
                    host=_audit_host(url),
                    url_path=_audit_path(url),
                    credential_ref=credential.env_name if credential else None,
                    reason=_safe_reason(str(error)),
                )
            )
            raise
        current = initial
        headers: dict[str, str] = {
            "Accept": "text/html, text/plain, application/json, application/xml;q=0.9",
            "Accept-Encoding": "gzip, deflate",
            "User-Agent": "XDSH-safe-web/1",
        }
        credential_header_name: str | None = None
        try:
            if credential is not None:
                name, value = credential.header(self.environment)
                headers[name] = value
                credential_header_name = name
            for redirect_count in range(self.policy.policy.max_redirects + 1):
                # Re-resolve on every hop/request to close DNS-rebinding windows.
                current = self.policy.validate(current.normalized)
                request = HttpRequest(
                    method="GET",
                    url=current.normalized,
                    headers=headers,
                    max_response_bytes=limit,
                )
                try:
                    response = await asyncio.wait_for(
                        asyncio.to_thread(
                            self.transport.request,
                            request,
                            timeout_seconds=self.policy.policy.timeout_seconds,
                        ),
                        timeout=self.policy.policy.timeout_seconds,
                    )
                except asyncio.TimeoutError as error:
                    raise ExternalTimeoutError("Web request timed out") from error
                if response.url:
                    response_url = self.policy.validate(response.url)
                else:
                    response_url = current
                if len(response.body) > limit:
                    raise ExternalResourceLimitError("Web response exceeds byte limit")
                if (
                    sum(len(str(key)) + len(str(value)) for key, value in response.headers.items())
                    > self.policy.policy.max_header_bytes
                ):
                    raise ExternalResourceLimitError("Web response headers exceed limit")
                if (
                    response_url.scheme != current.scheme
                    or response_url.hostname != current.hostname
                    or response_url.port != current.port
                ):
                    raise ExternalPolicyError("Web transport changed authority unexpectedly")
                if 300 <= response.status < 400:
                    location = _header(response.headers, "location")
                    if not location:
                        raise ExternalProtocolError("Web redirect 缺少 Location")
                    if redirect_count >= self.policy.policy.max_redirects:
                        raise ExternalResourceLimitError("Web redirect 次數超過上限")
                    target = self.policy.validate(urljoin(current.normalized, location))
                    if (
                        (target.hostname, target.port) != (initial.hostname, initial.port)
                        and not self.policy.policy.allow_cross_host_redirects
                    ):
                        raise ExternalPolicyError("Web redirect 不得跨越原始主機")
                    if (
                        target.scheme != current.scheme
                        or target.hostname != current.hostname
                        or target.port != current.port
                    ):
                        # Never carry auth across a scheme, host, or port change.
                        if credential_header_name is not None:
                            headers.pop(credential_header_name, None)
                    current = target
                    continue
                if not 200 <= response.status < 300:
                    raise ExternalHttpError(response.status)
                content_type = _content_type(response.headers)
                if content_type not in {
                    "text/html",
                    "text/plain",
                    "application/json",
                    "application/xml",
                    "text/xml",
                }:
                    raise ExternalPolicyError("Web content type is not in the safe text allowlist")
                body = _decode_body(
                    response.body,
                    response.headers,
                    self.policy.policy.max_decompressed_bytes,
                )
                if len(body) > self.policy.policy.max_decompressed_bytes:
                    raise ExternalResourceLimitError("decompressed Web body exceeds limit")
                text = (body.decode('utf-8', errors='replace')[:self.max_text_chars]
                        if self.preserve_html and content_type == 'text/html' else
                        _extract_web_text(body, content_type, self.max_text_chars))
                document = WebDocument(
                    source_url=initial.normalized,
                    final_url=current.normalized,
                    status=response.status,
                    content_type=content_type,
                    text=text,
                    sha256=hashlib.sha256(body).hexdigest(),
                    truncated=len(text) >= self.max_text_chars,
                )
                self._audit(
                    ExternalAuditEvent(
                        request_id=request_id,
                        operation="web.fetch",
                        outcome="completed",
                        host=initial.hostname,
                        url_path=urlsplit(initial.normalized).path or "/",
                        status=response.status,
                        bytes_read=len(body),
                        credential_ref=credential.env_name if credential else None,
                    )
                )
                return document
            raise ExternalResourceLimitError("Web redirect loop exceeded limit")
        except ExternalError as error:
            self._audit(
                ExternalAuditEvent(
                    request_id=request_id,
                    operation="web.fetch",
                    outcome="blocked" if isinstance(error, ExternalPolicyError) else "failed",
                    host=initial.hostname,
                    url_path=urlsplit(initial.normalized).path or "/",
                    credential_ref=credential.env_name if credential else None,
                    reason=_safe_reason(str(error)),
                )
            )
            raise
        except Exception as error:
            self._audit(
                ExternalAuditEvent(
                    request_id=request_id,
                    operation="web.fetch",
                    outcome="failed",
                    host=initial.hostname,
                    url_path=urlsplit(initial.normalized).path or "/",
                    credential_ref=credential.env_name if credential else None,
                    reason=type(error).__name__,
                )
            )
            raise ExternalProtocolError("Web provider request failed") from error

    def _audit(self, event: ExternalAuditEvent) -> None:
        if self.audit is not None:
            self.audit.record(event)


@dataclass(frozen=True, slots=True)
class SchemaLimits:
    """Limits for validating provider schemas and JSON arguments/results."""

    max_depth: int = 8
    max_properties: int = 64
    max_array_items: int = 64
    max_string_chars: int = 8_000
    max_json_bytes: int = 100_000

    def __post_init__(self) -> None:
        for name, value in (
            ("max_depth", self.max_depth),
            ("max_properties", self.max_properties),
            ("max_array_items", self.max_array_items),
            ("max_string_chars", self.max_string_chars),
            ("max_json_bytes", self.max_json_bytes),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be positive")
        bounds = {
            "max_depth": 32,
            "max_properties": 256,
            "max_array_items": 256,
            "max_string_chars": 1_000_000,
            "max_json_bytes": 2_000_000,
        }
        for name, upper in bounds.items():
            if getattr(self, name) > upper:
                raise ValueError(f"{name} exceeds the hard safety ceiling")


class JsonSchemaError(ExternalPolicyError):
    """An MCP/browser JSON schema or value is unsafe or invalid."""


def validate_json_value(
    value: JsonValue,
    schema: Mapping[str, object],
    *,
    limits: SchemaLimits | None = None,
) -> None:
    """Validate a bounded JSON value against a deliberately small schema subset."""

    effective_limits = limits or SchemaLimits()
    if not isinstance(schema, Mapping):
        raise JsonSchemaError("JSON schema must be an object")
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise JsonSchemaError("JSON value is not finite JSON") from error
    if len(encoded) > effective_limits.max_json_bytes:
        raise JsonSchemaError("JSON value exceeds the configured byte limit")
    _validate_schema_definition(schema, effective_limits, depth=0)
    _validate_schema(value, schema, limits=effective_limits, depth=0)


def _validate_schema(
    value: object,
    schema: Mapping[str, object],
    *,
    limits: SchemaLimits,
    depth: int,
) -> None:
    if depth > limits.max_depth:
        raise JsonSchemaError("JSON schema nesting exceeds the configured depth")
    schema_type = schema.get("type")
    if schema_type is not None:
        valid_types = {"object", "array", "string", "number", "integer", "boolean", "null"}
        types = (
            {schema_type}
            if isinstance(schema_type, str)
            else set(schema_type)
            if isinstance(schema_type, list)
            else set()
        )
        if not types or not types.issubset(valid_types):
            raise JsonSchemaError("JSON schema type is unsupported")
        if not any(_json_type_matches(value, item) for item in types):
            raise JsonSchemaError("JSON value does not match schema type")
    enum = schema.get("enum")
    if enum is not None:
        if not isinstance(enum, list) or value not in enum:
            raise JsonSchemaError("JSON value is not in schema enum")
    if "const" in schema and value != schema["const"]:
        raise JsonSchemaError("JSON value does not match schema const")
    if isinstance(value, str):
        if len(value) > limits.max_string_chars:
            raise JsonSchemaError("JSON string exceeds the configured limit")
        min_length = _integer_bound(
            schema.get("minLength"), 0, limits.max_string_chars, "minLength"
        )
        if min_length is not None and len(value) < min_length:
            raise JsonSchemaError("JSON string is shorter than schema minLength")
        max_length = _integer_bound(
            schema.get("maxLength"), 0, limits.max_string_chars, "maxLength"
        )
        if max_length is not None and len(value) > max_length:
            raise JsonSchemaError("JSON string exceeds schema maxLength")
        pattern = schema.get("pattern")
        if pattern is not None:
            raise JsonSchemaError("JSON schema regular expressions are not allowed")
    if isinstance(value, int | float) and not isinstance(value, bool):
        if isinstance(value, float) and not math.isfinite(value):
            raise JsonSchemaError("JSON number must be finite")
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if minimum is not None and (not isinstance(minimum, int | float) or value < minimum):
            raise JsonSchemaError("JSON number is below schema minimum")
        if maximum is not None and (not isinstance(maximum, int | float) or value > maximum):
            raise JsonSchemaError("JSON number exceeds schema maximum")
    if isinstance(value, list):
        if len(value) > limits.max_array_items:
            raise JsonSchemaError("JSON array exceeds the configured item limit")
        min_items = _integer_bound(schema.get("minItems"), 0, limits.max_array_items, "minItems")
        max_items = _integer_bound(
            schema.get("maxItems"), 0, limits.max_array_items, "maxItems"
        )
        if min_items is not None and len(value) < min_items:
            raise JsonSchemaError("JSON array is shorter than schema minItems")
        if max_items is not None and len(value) > max_items:
            raise JsonSchemaError("JSON array exceeds schema maxItems")
        items = schema.get("items")
        if items is not None:
            if not isinstance(items, Mapping):
                raise JsonSchemaError("JSON schema items must be an object")
            for item in value:
                _validate_schema(
                    item,
                    cast(Mapping[str, object], items),
                    limits=limits,
                    depth=depth + 1,
                )
    if isinstance(value, dict):
        if len(value) > limits.max_properties:
            raise JsonSchemaError("JSON object exceeds the configured property limit")
        properties_raw = schema.get("properties", {})
        if not isinstance(properties_raw, Mapping) or len(properties_raw) > limits.max_properties:
            raise JsonSchemaError("JSON schema properties are invalid or too numerous")
        properties = cast(Mapping[str, object], properties_raw)
        required_raw = schema.get("required", [])
        if not isinstance(required_raw, list) or not all(
            isinstance(item, str) for item in required_raw
        ):
            raise JsonSchemaError("JSON schema required list is invalid")
        if any(item not in value for item in required_raw):
            raise JsonSchemaError("JSON object is missing a required property")
        additional = schema.get("additionalProperties", False)
        if additional is not False:
            raise JsonSchemaError("additionalProperties must be false")
        for key, child in value.items():
            if not isinstance(key, str) or len(key) > 128:
                raise JsonSchemaError("JSON object key is invalid")
            if _SENSITIVE_KEY.search(key):
                raise JsonSchemaError("credential-like JSON fields are not allowed")
            child_schema = properties.get(key)
            if child_schema is None or not isinstance(child_schema, Mapping):
                raise JsonSchemaError("JSON object contains an unknown property")
            _validate_schema(
                child,
                cast(Mapping[str, object], child_schema),
                limits=limits,
                depth=depth + 1,
            )
    if isinstance(value, str) and contains_sensitive_text(value):
        raise JsonSchemaError("credential-like JSON value is not allowed")


def _integer_bound(value: object, lower: int, upper: int, name: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or not lower <= value <= upper:
        raise JsonSchemaError(f"JSON schema {name} is invalid")
    return value


def _json_type_matches(value: object, schema_type: object) -> bool:
    return (
        (schema_type == "object" and isinstance(value, dict))
        or (schema_type == "array" and isinstance(value, list))
        or (schema_type == "string" and isinstance(value, str))
        or (
            schema_type == "number"
            and isinstance(value, int | float)
            and not isinstance(value, bool)
            and (not isinstance(value, float) or math.isfinite(value))
        )
        or (schema_type == "integer" and isinstance(value, int) and not isinstance(value, bool))
        or (schema_type == "boolean" and isinstance(value, bool))
        or (schema_type == "null" and value is None)
    )


@dataclass(frozen=True, slots=True)
class McpToolSpec:
    name: str
    input_schema: Mapping[str, object]
    description: str = ""

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", self.name):
            raise JsonSchemaError("MCP tool name is invalid")
        if not isinstance(self.input_schema, Mapping):
            raise JsonSchemaError("MCP tool schema must be an object")
        if _CONTROL.search(self.description):
            raise JsonSchemaError("MCP tool description contains control characters")
        if len(self.description) > 2_000:
            raise JsonSchemaError("MCP tool description is too long")
        object.__setattr__(
            self,
            "description",
            redact_sensitive_text(" ".join(self.description.split()))[:2_000],
        )


@dataclass(frozen=True, slots=True)
class McpCallResult:
    tool_name: str
    result: JsonValue
    is_error: bool = False
    request_id: str = ""


class AuthenticatedMcpClient:
    """Call an explicitly authenticated, HTTP JSON-RPC MCP gateway."""

    def __init__(
        self,
        endpoint: str,
        *,
        policy: UrlPolicy,
        transport: HttpTransport | None = None,
        credential: CredentialRef | None = None,
        allowed_tools: Sequence[str] = (),
        audit: AuditSink | None = None,
        environment: Mapping[str, str] | None = None,
        schema_limits: SchemaLimits | None = None,
        max_tools: int = 64,
        max_result_bytes: int = 200_000,
    ) -> None:
        if credential is None:
            raise ExternalAuthError("MCP gateway 必須提供 environment credential reference")
        if not 1 <= max_tools <= 256 or not 1_000 <= max_result_bytes <= 2_000_000:
            raise ValueError("MCP resource limits are outside the safe range")
        self.endpoint = endpoint
        self.policy = policy
        self.transport = transport or UrllibHttpTransport()
        self.credential = credential
        self.allowed_tools = frozenset(allowed_tools)
        if any(
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", name)
            for name in self.allowed_tools
        ):
            raise ValueError("MCP tool allowlist contains an invalid name")
        self.audit = audit
        self.environment = environment
        self.schema_limits = schema_limits or SchemaLimits()
        self.max_tools = max_tools
        self.max_result_bytes = max_result_bytes
        self._tools: dict[str, McpToolSpec] = {}
        self._counter = 0

    async def list_tools(self) -> tuple[McpToolSpec, ...]:
        request_id = self._next_id()
        try:
            payload = await self._rpc(request_id, "tools/list", {})
            result = payload.get("result")
            if not isinstance(result, Mapping):
                raise ExternalProtocolError("MCP tools/list result is invalid")
            raw_tools = result.get("tools")
            if not isinstance(raw_tools, list) or len(raw_tools) > self.max_tools:
                raise ExternalResourceLimitError("MCP tool list exceeds limit")
            parsed: list[McpToolSpec] = []
            seen_names: set[str] = set()
            for raw in raw_tools:
                if not isinstance(raw, Mapping):
                    raise JsonSchemaError("MCP tool descriptor is invalid")
                name = raw.get("name")
                schema = raw.get("inputSchema")
                description = raw.get("description", "")
                if (
                    not isinstance(name, str)
                    or not isinstance(schema, Mapping)
                    or not isinstance(description, str)
                ):
                    raise JsonSchemaError("MCP tool descriptor is invalid")
                description = redact_sensitive_text(" ".join(description.split()))
                tool = McpToolSpec(
                    name=name,
                    input_schema=cast(Mapping[str, object], schema),
                    description=description,
                )
                _validate_schema_definition(tool.input_schema, self.schema_limits, depth=0)
                if tool.name in seen_names:
                    raise JsonSchemaError("MCP tool list contains duplicate names")
                seen_names.add(tool.name)
                if tool.name in self.allowed_tools:
                    parsed.append(tool)
            self._tools = {tool.name: tool for tool in parsed}
            self._audit(
                "mcp.list", "completed", request_id, status=200, bytes_read=len(json.dumps(payload))
            )
            return tuple(parsed)
        except ExternalError as error:
            self._audit(
                "mcp.list",
                "blocked" if isinstance(error, ExternalPolicyError) else "failed",
                request_id,
                reason=_safe_reason(str(error)),
            )
            raise

    async def call_tool(self, name: str, arguments: JsonValue) -> McpCallResult:
        if name not in self.allowed_tools:
            raise ExternalPolicyError("MCP tool 不在 explicit allowlist")
        if name not in self._tools:
            await self.list_tools()
        tool = self._tools.get(name)
        if tool is None:
            raise ExternalPolicyError("MCP tool 不存在或未通過 allowlist")
        if not isinstance(arguments, dict):
            raise JsonSchemaError("MCP tool arguments must be a JSON object")
        validate_json_value(arguments, tool.input_schema, limits=self.schema_limits)
        request_id = self._next_id()
        try:
            payload = await self._rpc(
                request_id,
                "tools/call",
                {"name": name, "arguments": arguments},
            )
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
            if len(encoded) > self.max_result_bytes:
                raise ExternalResourceLimitError("MCP result exceeds byte limit")
            result = payload.get("result")
            if (
                not isinstance(result, dict | list | str | int | float | bool)
                and result is not None
            ):
                raise ExternalProtocolError("MCP result is not JSON")
            safe_result = _redact_json(cast(JsonValue, result), self.schema_limits)
            is_error = bool(result.get("isError", False)) if isinstance(result, dict) else False
            self._audit("mcp.call", "completed", request_id, status=200, bytes_read=len(encoded))
            return McpCallResult(
                tool_name=name, result=safe_result, is_error=is_error, request_id=request_id
            )
        except ExternalError as error:
            self._audit(
                "mcp.call",
                "blocked" if isinstance(error, ExternalPolicyError) else "failed",
                request_id,
                reason=_safe_reason(str(error)),
            )
            raise

    async def _rpc(self, request_id: str, method: str, params: JsonValue) -> Mapping[str, object]:
        validated = self.policy.validate(self.endpoint)
        name, value = self.credential.header(self.environment)
        body = json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    self.transport.request,
                    HttpRequest(
                        method="POST",
                        url=validated.normalized,
                        headers={
                            "Content-Type": "application/json",
                            "Accept": "application/json",
                            name: value,
                        },
                        body=body,
                        max_response_bytes=self.max_result_bytes,
                    ),
                    timeout_seconds=self.policy.policy.timeout_seconds,
                ),
                timeout=self.policy.policy.timeout_seconds,
            )
        except asyncio.TimeoutError as error:
            raise ExternalResourceLimitError("MCP request timed out") from error
        if response.status != 200:
            raise ExternalProtocolError(f"MCP gateway returned HTTP {response.status}")
        if len(response.body) > self.max_result_bytes:
            raise ExternalResourceLimitError("MCP response exceeds byte limit")
        try:
            payload = json.loads(
                response.body.decode("utf-8"), parse_constant=_reject_json_constant
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise ExternalProtocolError("MCP gateway returned invalid JSON") from error
        if (
            not isinstance(payload, Mapping)
            or payload.get("jsonrpc") != "2.0"
            or payload.get("id") != request_id
        ):
            raise ExternalProtocolError("MCP JSON-RPC envelope is invalid")
        if "error" in payload:
            raise ExternalProtocolError("MCP gateway returned a JSON-RPC error")
        return cast(Mapping[str, object], payload)

    def _next_id(self) -> str:
        self._counter += 1
        return f"mcp-{self._counter}"

    def _audit(
        self,
        operation: Literal["mcp.list", "mcp.call"],
        outcome: Literal["allowed", "blocked", "completed", "failed"],
        request_id: str,
        *,
        status: int | None = None,
        bytes_read: int = 0,
        reason: str | None = None,
    ) -> None:
        if self.audit is None:
            return
        parsed = urlsplit(self.endpoint)
        self.audit.record(
            ExternalAuditEvent(
                request_id=request_id,
                operation=operation,
                outcome=outcome,
                host=parsed.hostname or "unknown",
                url_path=parsed.path or "/",
                status=status,
                bytes_read=bytes_read,
                credential_ref=self.credential.env_name,
                reason=reason,
            )
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class BrowserAction:
    """One safe browser-gateway action; arbitrary JavaScript is not supported."""

    action: str
    url: str | None = None
    selector: str | None = None
    text: str | None = None

    def __post_init__(self) -> None:
        if self.action not in {"open", "click", "type", "snapshot", "back", "reload"}:
            raise ExternalPolicyError("browser action is not allow-listed")
        if self.url is not None and (len(self.url) > 2048 or _CONTROL.search(self.url)):
            raise ExternalPolicyError("browser target URL is invalid")
        if self.selector is not None and (
            not self.selector or len(self.selector) > 512 or _CONTROL.search(self.selector)
        ):
            raise ExternalPolicyError("browser selector is invalid")
        if self.text is not None and (len(self.text) > 4_000 or _CONTROL.search(self.text)):
            raise ExternalResourceLimitError("browser input text exceeds limit")
        if self.action == "open" and not self.url:
            raise ExternalPolicyError("browser open action requires a URL")
        if self.action == "click" and not self.selector:
            raise ExternalPolicyError("browser click action requires a selector")
        if self.action == "type" and (not self.selector or self.text is None):
            raise ExternalPolicyError("browser type action requires selector and text")


@dataclass(frozen=True, slots=True)
class BrowserResult:
    ok: bool
    url: str | None
    text: str
    events: tuple[str, ...] = ()


class AuthenticatedBrowserClient:
    """Use a narrow authenticated browser gateway, never a local shell/browser."""

    def __init__(
        self,
        endpoint: str,
        *,
        endpoint_policy: UrlPolicy,
        target_policy: UrlPolicy,
        transport: HttpTransport | None = None,
        credential: CredentialRef | None = None,
        audit: AuditSink | None = None,
        environment: Mapping[str, str] | None = None,
        max_actions: int = 20,
        max_result_chars: int = 100_000,
    ) -> None:
        if credential is None:
            raise ExternalAuthError("browser gateway 必須提供 environment credential reference")
        if not 1 <= max_actions <= 50 or not 1_000 <= max_result_chars <= 1_000_000:
            raise ValueError("browser resource limits are outside the safe range")
        self.endpoint = endpoint
        self.endpoint_policy = endpoint_policy
        self.target_policy = target_policy
        self.transport = transport or UrllibHttpTransport()
        self.credential = credential
        self.audit = audit
        self.environment = environment
        self.max_actions = max_actions
        self.max_result_chars = max_result_chars
        self._counter = 0

    async def run(self, actions: Sequence[BrowserAction]) -> BrowserResult:
        if not actions or len(actions) > self.max_actions:
            raise ExternalResourceLimitError("browser action count exceeds limit")
        normalized_actions: list[dict[str, str]] = []
        target_authority: tuple[str, str, int] | None = None
        for action in actions:
            if action.url is not None:
                validated_target = self.target_policy.validate(action.url)
                authority = (
                    validated_target.scheme,
                    validated_target.hostname,
                    validated_target.port,
                )
                if target_authority is None:
                    target_authority = authority
                elif authority != target_authority:
                    raise ExternalPolicyError("browser actions cannot switch target authority")
                normalized_actions.append(
                    {"action": action.action, "url": validated_target.normalized}
                )
            else:
                item = {"action": action.action}
                if action.selector is not None:
                    item["selector"] = action.selector
                if action.text is not None:
                    item["text"] = action.text
                normalized_actions.append(item)
        request_id = self._next_id()
        try:
            endpoint = self.endpoint_policy.validate(self.endpoint)
            name, value = self.credential.header(self.environment)
            body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "browser/run",
                    "params": {"actions": normalized_actions},
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            try:
                response = await asyncio.wait_for(
                    asyncio.to_thread(
                        self.transport.request,
                        HttpRequest(
                            method="POST",
                            url=endpoint.normalized,
                            headers={
                                "Content-Type": "application/json",
                                "Accept": "application/json",
                                name: value,
                            },
                            body=body,
                            max_response_bytes=self.max_result_chars * 4,
                        ),
                        timeout_seconds=self.endpoint_policy.policy.timeout_seconds,
                    ),
                    timeout=self.endpoint_policy.policy.timeout_seconds,
                )
            except asyncio.TimeoutError as error:
                raise ExternalResourceLimitError("browser request timed out") from error
            if response.status != 200:
                raise ExternalProtocolError(f"browser gateway returned HTTP {response.status}")
            if len(response.body) > self.max_result_chars * 4:
                raise ExternalResourceLimitError("browser response exceeds byte limit")
            payload = _parse_json_rpc(response.body, request_id)
            result = payload.get("result")
            if not isinstance(result, Mapping):
                raise ExternalProtocolError("browser result is invalid")
            ok = result.get("ok")
            text = result.get("text", "")
            raw_url = result.get("url")
            raw_events = result.get("events", [])
            if (
                not isinstance(ok, bool)
                or not isinstance(text, str)
                or not isinstance(raw_events, list)
            ):
                raise ExternalProtocolError("browser result schema is invalid")
            if len(text) > self.max_result_chars or len(raw_events) > self.max_actions * 4:
                raise ExternalResourceLimitError("browser result exceeds resource limit")
            events = tuple(item for item in raw_events if isinstance(item, str))
            if len(events) != len(raw_events) or any(len(item) > 1_000 for item in events):
                raise ExternalPolicyError("browser event schema is invalid")
            result_url = None
            if raw_url is not None:
                if not isinstance(raw_url, str):
                    raise ExternalProtocolError("browser result URL is invalid")
                validated_result = self.target_policy.validate(raw_url)
                result_url = validated_result.normalized
                if target_authority is not None and (
                    validated_result.scheme,
                    validated_result.hostname,
                    validated_result.port,
                ) != target_authority:
                    raise ExternalPolicyError("browser result changed target authority")
            self._audit(
                request_id,
                "completed",
                host=urlsplit(self.endpoint).hostname or "unknown",
                path=urlsplit(self.endpoint).path or "/",
                status=response.status,
                bytes_read=len(response.body),
            )
            return BrowserResult(
                ok=ok,
                url=result_url,
                text=redact_sensitive_text(text),
                events=tuple(redact_sensitive_text(item) for item in events),
            )
        except ExternalError as error:
            self._audit(
                request_id,
                "blocked" if isinstance(error, ExternalPolicyError) else "failed",
                host=urlsplit(self.endpoint).hostname or "unknown",
                path=urlsplit(self.endpoint).path or "/",
                reason=_safe_reason(str(error)),
            )
            raise

    def _next_id(self) -> str:
        self._counter += 1
        return f"browser-{self._counter}"

    def _audit(
        self,
        request_id: str,
        outcome: Literal["allowed", "blocked", "completed", "failed"],
        *,
        host: str,
        path: str,
        status: int | None = None,
        bytes_read: int = 0,
        reason: str | None = None,
    ) -> None:
        if self.audit is not None:
            self.audit.record(
                ExternalAuditEvent(
                    request_id=request_id,
                    operation="browser.run",
                    outcome=outcome,
                    host=host,
                    url_path=path,
                    status=status,
                    bytes_read=bytes_read,
                    credential_ref=self.credential.env_name,
                    reason=reason,
                )
            )


class _VisibleTextParser(HTMLParser):
    def __init__(self, max_chars: int) -> None:
        super().__init__(convert_charrefs=True)
        self.max_chars = max_chars
        self.parts: list[str] = []
        self._hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag.lower() in {"script", "style", "noscript", "template", "svg"}:
            self._hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript", "template", "svg"} and self._hidden:
            self._hidden -= 1

    def handle_data(self, data: str) -> None:
        if self._hidden or not data.strip():
            return
        remaining = self.max_chars - sum(len(item) for item in self.parts)
        if remaining > 0:
            self.parts.append(data[:remaining])


def _extract_web_text(body: bytes, content_type: str, max_chars: int) -> str:
    try:
        decoded = body.decode("utf-8", errors="replace")
    except Exception as error:  # pragma: no cover - bytes.decode is total with replace
        raise ExternalProtocolError("Web body cannot be decoded") from error
    if content_type == "text/html":
        parser = _VisibleTextParser(max_chars)
        try:
            parser.feed(decoded)
            parser.close()
        except Exception as error:
            raise ExternalProtocolError("Web HTML cannot be parsed safely") from error
        decoded = " ".join(" ".join(parser.parts).split())
    elif content_type == "application/json":
        try:
            parsed = json.loads(
                decoded,
                parse_constant=_reject_json_constant,
                object_pairs_hook=_reject_duplicate_json_pairs,
            )
            decoded = json.dumps(parsed, ensure_ascii=False, indent=2, allow_nan=False)
        except (json.JSONDecodeError, ValueError):
            decoded = decoded
    return decoded[:max_chars]


def _decode_body(body: bytes, headers: Mapping[str, str], max_bytes: int) -> bytes:
    if len(body) > max_bytes:
        raise ExternalResourceLimitError("compressed response exceeds limit")
    encoding = _header(headers, "content-encoding").strip().lower()
    if not encoding or encoding == "identity":
        return body
    if encoding == "gzip":
        return _decode_gzip_bounded(body, max_bytes)
    if encoding == "deflate":
        try:
            decompressor = zlib.decompressobj()
            output = decompressor.decompress(body, max_bytes + 1)
            if len(output) > max_bytes or decompressor.unconsumed_tail:
                raise ExternalResourceLimitError("deflate response exceeds limit")
            output += decompressor.flush(max_bytes + 1 - len(output))
            if len(output) > max_bytes:
                raise ExternalResourceLimitError("deflate response exceeds limit")
            if decompressor.unused_data or not decompressor.eof:
                raise ExternalProtocolError("deflate response is truncated or has trailing data")
            return output
        except zlib.error as error:
            raise ExternalProtocolError("deflate response is invalid") from error
    raise ExternalPolicyError("unsupported Content-Encoding")


def _decode_gzip_bounded(body: bytes, max_bytes: int) -> bytes:
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    output = bytearray()
    try:
        for offset in range(0, len(body), 32_768):
            chunk = body[offset : offset + 32_768]
            part = decompressor.decompress(chunk, max_bytes - len(output) + 1)
            output.extend(part)
            if len(output) > max_bytes or decompressor.unconsumed_tail:
                raise ExternalResourceLimitError("decompressed response exceeds limit")
        if decompressor.unused_data:
            raise ExternalProtocolError("gzip response has trailing data")
        tail = decompressor.flush(max_bytes - len(output) + 1)
        output.extend(tail)
    except zlib.error as error:
        raise ExternalProtocolError("gzip response is invalid") from error
    if len(output) > max_bytes:
        raise ExternalResourceLimitError("decompressed response exceeds limit")
    if not decompressor.eof:
        raise ExternalProtocolError("gzip response is truncated")
    return bytes(output)


_SCHEMA_ALLOWED_KEYS = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "const",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "minimum",
        "maximum",
        "description",
        "title",
        "default",
        "examples",
        "format",
        "$schema",
    }
)
_SCHEMA_FORBIDDEN_KEYS = frozenset(
    {"$ref", "oneOf", "anyOf", "allOf", "not", "patternProperties", "pattern"}
)
_JSON_SCHEMA_TYPES = frozenset(
    {"object", "array", "string", "number", "integer", "boolean", "null"}
)


def _validate_schema_definition(
    schema: Mapping[str, object], limits: SchemaLimits, *, depth: int
) -> None:
    """Validate the schema itself before using it to validate an argument.

    MCP servers are untrusted too: a giant or recursive schema can be a denial
    of service even when the eventual tool arguments are small.  We therefore
    accept only a closed, non-compositional subset and bound every metadata
    value.  The validator deliberately defaults object schemas to closed
    (`additionalProperties: false`) in `_validate_schema`.
    """

    if depth > limits.max_depth:
        raise JsonSchemaError("JSON schema nesting exceeds limit")
    if not isinstance(schema, Mapping):
        raise JsonSchemaError("JSON schema must be an object")
    if any(not isinstance(key, str) or len(key) > 128 for key in schema):
        raise JsonSchemaError("JSON schema key is invalid")
    if _SCHEMA_FORBIDDEN_KEYS.intersection(schema):
        raise JsonSchemaError("JSON schema composition or regular expressions are not allowed")
    if set(schema).difference(_SCHEMA_ALLOWED_KEYS):
        raise JsonSchemaError("JSON schema contains an unsupported keyword")

    schema_type = schema.get("type")
    if schema_type is not None:
        if isinstance(schema_type, str):
            types = {schema_type}
        elif isinstance(schema_type, list) and all(isinstance(item, str) for item in schema_type):
            types = set(schema_type)
            if len(types) != len(schema_type):
                raise JsonSchemaError("JSON schema type list contains duplicates")
        else:
            raise JsonSchemaError("JSON schema type is invalid")
        if not types or not types.issubset(_JSON_SCHEMA_TYPES):
            raise JsonSchemaError("JSON schema type is unsupported")

    additional = schema.get("additionalProperties")
    if additional is not None and additional is not False:
        raise JsonSchemaError("JSON schema additionalProperties must be false")

    properties = schema.get("properties")
    if properties is not None:
        if not isinstance(properties, Mapping) or len(properties) > limits.max_properties:
            raise JsonSchemaError("JSON schema properties exceed limit")
        for key, child in properties.items():
            if (
                not isinstance(key, str)
                or len(key) > 128
                or not isinstance(child, Mapping)
                or _SENSITIVE_KEY.search(key) is not None
            ):
                raise JsonSchemaError("JSON schema property is invalid")
            _validate_schema_definition(cast(Mapping[str, object], child), limits, depth=depth + 1)

    required = schema.get("required")
    if required is not None:
        if (
            not isinstance(required, list)
            or len(required) > limits.max_properties
            or any(not isinstance(item, str) or len(item) > 128 for item in required)
            or len(set(required)) != len(required)
        ):
            raise JsonSchemaError("JSON schema required list is invalid")
        if isinstance(properties, Mapping) and any(item not in properties for item in required):
            raise JsonSchemaError("JSON schema required property is undefined")

    items = schema.get("items")
    if items is not None:
        if not isinstance(items, Mapping):
            raise JsonSchemaError("JSON schema items are invalid")
        _validate_schema_definition(cast(Mapping[str, object], items), limits, depth=depth + 1)

    for name, lower, upper in (
        ("minLength", 0, limits.max_string_chars),
        ("maxLength", 0, limits.max_string_chars),
        ("minItems", 0, limits.max_array_items),
        ("maxItems", 0, limits.max_array_items),
    ):
        _integer_bound(schema.get(name), lower, upper, name)
    min_length = cast(int | None, schema.get("minLength"))
    max_length = cast(int | None, schema.get("maxLength"))
    min_items = cast(int | None, schema.get("minItems"))
    max_items = cast(int | None, schema.get("maxItems"))
    if min_length is not None and max_length is not None and min_length > max_length:
        raise JsonSchemaError("JSON schema string bounds are inverted")
    if min_items is not None and max_items is not None and min_items > max_items:
        raise JsonSchemaError("JSON schema array bounds are inverted")

    minimum = cast(int | float | None, schema.get("minimum"))
    maximum = cast(int | float | None, schema.get("maximum"))
    for name, bound in (("minimum", minimum), ("maximum", maximum)):
        if bound is not None and (
            not isinstance(bound, int | float)
            or isinstance(bound, bool)
            or (isinstance(bound, float) and not math.isfinite(bound))
        ):
            raise JsonSchemaError(f"JSON schema {name} is invalid")
    if minimum is not None and maximum is not None and minimum > maximum:
        raise JsonSchemaError("JSON schema number bounds are inverted")

    enum = schema.get("enum")
    if enum is not None:
        if not isinstance(enum, list) or len(enum) > limits.max_array_items:
            raise JsonSchemaError("JSON schema enum exceeds limit")
        for item in enum:
            _validate_untyped_json(item, limits, depth=depth + 1)
    if "const" in schema:
        _validate_untyped_json(schema["const"], limits, depth=depth + 1)
    for name in ("default", "examples"):
        if name in schema:
            value = schema[name]
            if name == "examples" and (
                not isinstance(value, list) or len(value) > limits.max_array_items
            ):
                raise JsonSchemaError("JSON schema examples are invalid")
            _validate_untyped_json(value, limits, depth=depth + 1)
    for name in ("description", "title", "format", "$schema"):
        value = schema.get(name)
        if value is not None and (
            not isinstance(value, str) or len(value) > limits.max_string_chars
        ):
            raise JsonSchemaError(f"JSON schema {name} is invalid")
        if isinstance(value, str) and contains_sensitive_text(value):
            raise JsonSchemaError("JSON schema metadata contains credential-like text")

    try:
        encoded = json.dumps(schema, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise JsonSchemaError("JSON schema is not finite JSON") from error
    if len(encoded.encode("utf-8")) > limits.max_json_bytes:
        raise JsonSchemaError("JSON schema exceeds the configured byte limit")


def _validate_untyped_json(value: object, limits: SchemaLimits, *, depth: int) -> None:
    """Bound JSON metadata such as enum/default/examples without a schema."""

    if depth > limits.max_depth:
        raise JsonSchemaError("JSON schema metadata nesting exceeds limit")
    if isinstance(value, str):
        if len(value) > limits.max_string_chars:
            raise JsonSchemaError("JSON schema metadata string exceeds limit")
        if contains_sensitive_text(value):
            raise JsonSchemaError("JSON schema metadata contains credential-like text")
        return
    if isinstance(value, float) and not math.isfinite(value):
        raise JsonSchemaError("JSON schema metadata number must be finite")
    if isinstance(value, list):
        if len(value) > limits.max_array_items:
            raise JsonSchemaError("JSON schema metadata array exceeds limit")
        for item in value:
            _validate_untyped_json(item, limits, depth=depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > limits.max_properties:
            raise JsonSchemaError("JSON schema metadata object exceeds limit")
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 128 or _SENSITIVE_KEY.search(key):
                raise JsonSchemaError("JSON schema metadata key is invalid")
            _validate_untyped_json(item, limits, depth=depth + 1)
        return
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return
    raise JsonSchemaError("JSON schema metadata contains a non-JSON value")


def _redact_json(value: JsonValue, limits: SchemaLimits, *, depth: int = 0) -> JsonValue:
    if depth > limits.max_depth:
        raise ExternalResourceLimitError("MCP result nesting exceeds limit")
    if isinstance(value, str):
        if len(value) > limits.max_string_chars:
            raise ExternalResourceLimitError("MCP result string exceeds limit")
        return redact_sensitive_text(value)
    if isinstance(value, list):
        if len(value) > limits.max_array_items:
            raise ExternalResourceLimitError("MCP result array exceeds limit")
        return [_redact_json(item, limits, depth=depth + 1) for item in value]
    if isinstance(value, dict):
        if len(value) > limits.max_properties:
            raise ExternalResourceLimitError("MCP result object exceeds limit")
        output: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 128:
                raise ExternalResourceLimitError("MCP result object key is invalid")
            if _SENSITIVE_KEY.search(key):
                output[key] = "[REDACTED]"
            else:
                output[key] = _redact_json(item, limits, depth=depth + 1)
        return output
    if isinstance(value, float) and not math.isfinite(value):
        raise ExternalResourceLimitError("MCP result number must be finite")
    return value


def _parse_json_rpc(body: bytes, request_id: str) -> Mapping[str, object]:
    try:
        payload = json.loads(
            body.decode("utf-8"),
            parse_constant=_reject_json_constant,
            object_pairs_hook=_reject_duplicate_json_pairs,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ExternalProtocolError("JSON-RPC response is invalid JSON") from error
    if (
        not isinstance(payload, Mapping)
        or payload.get("jsonrpc") != "2.0"
        or payload.get("id") != request_id
    ):
        raise ExternalProtocolError("JSON-RPC response envelope is invalid")
    if "error" in payload:
        raise ExternalProtocolError("JSON-RPC provider returned an error")
    return cast(Mapping[str, object], payload)


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON constant: {value}")


def _reject_duplicate_json_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _request_id() -> str:
    return f"ext-{uuid.uuid4().hex[:16]}"


def _safe_reason(value: str) -> str:
    return redact_sensitive_text(value.replace("\n", " ").replace("\r", " "))[:240]


def _audit_host(url: str) -> str:
    try:
        host = urlsplit(url).hostname or "unknown"
    except ValueError:
        host = "unknown"
    host = _CONTROL.sub("?", host)
    return host[:253] or "unknown"


def _audit_path(url: str) -> str:
    try:
        path = urlsplit(url).path or "/"
    except ValueError:
        path = "/"
    path = _CONTROL.sub("?", path)
    if not path.startswith("/"):
        path = "/" + path
    return path[:2048]


def _header(headers: Mapping[str, str], name: str) -> str:
    wanted = name.casefold()
    for key, value in headers.items():
        if str(key).casefold() == wanted:
            return str(value)
    return ""


def _content_type(headers: Mapping[str, str]) -> str:
    return _header(headers, "content-type").split(";", 1)[0].strip().lower()


def _normalize_allowlist_host(host: str) -> str:
    normalized = host.strip().casefold()
    if normalized.startswith("*."):
        suffix = _normalize_hostname(normalized[2:])
        return f"*.{suffix}"
    return _normalize_hostname(normalized)


def _normalize_hostname(host: str) -> str:
    value = host.strip().casefold().rstrip(".")
    if not value or _CONTROL.search(value):
        raise ExternalPolicyError("主機名稱無效")
    try:
        if ":" in value:
            return ipaddress.ip_address(value).compressed
        ascii_name = value.encode("idna").decode("ascii")
    except (UnicodeError, ValueError) as error:
        raise ExternalPolicyError("主機名稱無效") from error
    if len(ascii_name) > 253:
        raise ExternalPolicyError("主機名稱過長")
    if "." not in ascii_name and ascii_name != "localhost" and not _parse_literal_ip(ascii_name):
        if not _HOST_LABEL.fullmatch(ascii_name):
            raise ExternalPolicyError("主機名稱無效")
    elif ascii_name != "localhost" and not _parse_literal_ip(ascii_name):
        if any(not _HOST_LABEL.fullmatch(label) for label in ascii_name.split(".")):
            raise ExternalPolicyError("主機名稱無效")
    return ascii_name


def _host_allowed(hostname: str, allowlist: Sequence[str]) -> bool:
    for allowed in allowlist:
        if allowed.startswith("*."):
            suffix = allowed[2:]
            if hostname.endswith("." + suffix) and hostname != suffix:
                return True
        elif hostname == allowed:
            return True
    return False


def _is_loopback_name(hostname: str) -> bool:
    if hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _parse_literal_ip(hostname: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(hostname)
    except ValueError:
        pass
    if not hostname or any(character not in "0123456789abcdefABCDEFxX." for character in hostname):
        return None
    # Common integer/hex/octal IPv4 spellings are rejected rather than passed
    # to a resolver where they could disguise loopback or metadata addresses.
    if "." not in hostname:
        try:
            base = (
                16 if hostname.lower().startswith("0x") else 8 if hostname.startswith("0") else 10
            )
            value = int(hostname, base)
            return ipaddress.IPv4Address(value) if 0 <= value <= 0xFFFFFFFF else None
        except ValueError:
            return None
    parts = hostname.split(".")
    if not 1 <= len(parts) <= 4:
        return None
    try:
        values = [
            int(
                part,
                16
                if part.lower().startswith("0x")
                else 8
                if part.startswith("0") and part != "0"
                else 10,
            )
            for part in parts
        ]
    except ValueError:
        return None
    if any(value < 0 for value in values):
        return None
    if len(values) == 4 and all(value <= 255 for value in values):
        return ipaddress.IPv4Address(".".join(str(value) for value in values))
    return None


def _normalize_address(address: str) -> str:
    try:
        return ipaddress.ip_address(address).compressed
    except ValueError as error:
        raise ExternalPolicyError("DNS 回覆含有無效地址") from error


def _blocked_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def _format_netloc(hostname: str, port: int, scheme: str) -> str:
    display_host = f"[{hostname}]" if ":" in hostname else hostname
    default = 443 if scheme == "https" else 80
    return display_host if port == default else f"{display_host}:{port}"
