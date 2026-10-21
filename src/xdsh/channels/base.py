"""Shared contracts and bounded helpers for optional messaging channels."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol, TypeAlias, cast
from urllib.parse import urlsplit

from xdsh.core.external import HttpRequest, HttpResponse, UrllibHttpTransport
from xdsh.core.product_control import safe_outbound_text

ChannelName: TypeAlias = Literal[
    "discord",
    "whatsapp",
    "dingtalk",
    "feishu",
    "wechat",
    "qq",
]
WebhookMethod: TypeAlias = Literal["GET", "POST"]
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_CONTROL = re.compile(r"[\x00\x01-\x08\x0b\x0c\x0e-\x1f\x7f]")
MAX_WEBHOOK_BYTES = 1_000_000
MAX_MESSAGE_CHARS = 32_000
MAX_REQUEST_HEADER_BYTES = 32_768
MAX_REQUEST_HEADERS = 128
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 20_000
_CHANNEL_NAMES = frozenset({"discord", "whatsapp", "dingtalk", "feishu", "wechat", "qq"})


class ChannelError(RuntimeError):
    """Base class for channel adapter failures."""


class ChannelConfigurationError(ChannelError):
    """A channel is missing a required non-secret configuration value."""


class ChannelAuthError(ChannelError):
    """A webhook or outbound credential failed validation."""


class ChannelPayloadError(ChannelError):
    """A channel payload is malformed or exceeds a schema bound."""


class ChannelResourceLimitError(ChannelPayloadError):
    """A channel request or response exceeds a configured resource limit."""


@dataclass(frozen=True, slots=True)
class ChannelMessage:
    """A normalized inbound user message.

    ``reply_handle`` is intentionally excluded from ``repr`` because some
    platforms put a short-lived interaction token in it.  It is never suitable
    for durable storage or an audit record.
    """

    channel: ChannelName
    sender_id: str
    conversation_id: str
    text: str
    message_id: str
    reply_handle: str = field(repr=False)
    raw_event_type: str = "message"

    def __post_init__(self) -> None:
        if not isinstance(self.channel, str) or self.channel not in _CHANNEL_NAMES:
            raise ChannelPayloadError("channel name is invalid")
        for name, value, maximum in (
            ("sender_id", self.sender_id, 512),
            ("conversation_id", self.conversation_id, 512),
            ("message_id", self.message_id, 512),
            ("reply_handle", self.reply_handle, 4096),
            ("raw_event_type", self.raw_event_type, 128),
        ):
            if (
                not isinstance(value, str)
                or not value
                or len(value) > maximum
                or _CONTROL.search(value)
            ):
                raise ChannelPayloadError(f"{name} is invalid")
        if not isinstance(self.text, str) or not self.text.strip():
            raise ChannelPayloadError("message text must be non-empty")
        if len(self.text) > MAX_MESSAGE_CHARS or "\x00" in self.text:
            raise ChannelResourceLimitError("message text exceeds channel bound")


@dataclass(frozen=True, slots=True)
class ChannelHttpResponse:
    """A bounded response for a webhook server."""

    status: int
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.status, int)
            or isinstance(self.status, bool)
            or not 100 <= self.status <= 599
        ):
            raise ValueError("channel HTTP status is invalid")
        if not isinstance(self.body, bytes) or len(self.body) > MAX_WEBHOOK_BYTES:
            raise ChannelResourceLimitError("channel response exceeds byte bound")
        for key, value in self.headers.items():
            if (
                not isinstance(key, str)
                or not isinstance(value, str)
                or _CONTROL.search(key)
                or _CONTROL.search(value)
            ):
                raise ChannelPayloadError("channel response header is invalid")


@dataclass(frozen=True, slots=True)
class WebhookDispatch:
    """Result of authenticating and decoding one webhook request."""

    response: ChannelHttpResponse
    message: ChannelMessage | None = None


class ChannelTransport(Protocol):
    """Synchronous HTTP seam used by channel clients in worker threads."""

    def request(self, request: HttpRequest, *, timeout_seconds: float) -> HttpResponse: ...


class ChannelAdapter(Protocol):
    """Common adapter contract consumed by a future channel service."""

    @property
    def name(self) -> ChannelName: ...

    def handle_webhook(
        self,
        method: WebhookMethod,
        *,
        query: Mapping[str, str],
        headers: Mapping[str, str],
        body: bytes,
    ) -> WebhookDispatch: ...

    async def send_text(self, message: ChannelMessage, text: str) -> None: ...


@dataclass(frozen=True, slots=True)
class SecretRef:
    """A reference to an environment secret; the value is never retained."""

    env_name: str
    maximum_chars: int = 4096

    def __post_init__(self) -> None:
        if _ENV_NAME.fullmatch(self.env_name) is None:
            raise ChannelConfigurationError("credential environment name is invalid")
        if not 1 <= self.maximum_chars <= 16_384:
            raise ChannelConfigurationError("credential size bound is invalid")

    def resolve(self, environment: Mapping[str, str] | None = None) -> str:
        source = os.environ if environment is None else environment
        value = source.get(self.env_name, "")
        if (
            not isinstance(value, str)
            or not value
            or len(value) > self.maximum_chars
            or _CONTROL.search(value)
            or any(character.isspace() for character in value)
        ):
            raise ChannelAuthError("channel credential is unset or unsafe")
        return value


class JsonChannelClient:
    """Bounded HTTPS JSON client with an exact host allowlist."""

    def __init__(
        self,
        allowed_hosts: Sequence[str],
        *,
        transport: ChannelTransport | None = None,
        timeout_seconds: float = 15.0,
        max_response_bytes: int = 2_000_000,
    ) -> None:
        hosts = tuple(host.strip().casefold() for host in allowed_hosts)
        if not hosts or any(not host or "." not in host for host in hosts):
            raise ChannelConfigurationError("channel API host allowlist is invalid")
        if len(hosts) != len(set(hosts)):
            raise ChannelConfigurationError("channel API host allowlist repeats a host")
        if not 0.1 <= timeout_seconds <= 120.0:
            raise ChannelConfigurationError("channel API timeout is out of bounds")
        if not 1 <= max_response_bytes <= 20_000_000:
            raise ChannelConfigurationError("channel API response bound is invalid")
        self._allowed_hosts = frozenset(hosts)
        self._transport = transport or UrllibHttpTransport()
        self._timeout_seconds = timeout_seconds
        self._max_response_bytes = max_response_bytes

    async def post_json(
        self,
        url: str,
        payload: Mapping[str, object],
        *,
        headers: Mapping[str, str] | None = None,
        expected_statuses: Sequence[int] = (200, 201, 202),
    ) -> Mapping[str, object]:
        _validate_api_url(url, self._allowed_hosts)
        try:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise ChannelPayloadError("channel API payload is not JSON serializable") from error
        if len(body) > MAX_WEBHOOK_BYTES:
            raise ChannelResourceLimitError("channel API request exceeds byte bound")
        request_headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "XDSH-channel/1",
        }
        request_headers.update(_safe_headers(headers))
        response = await asyncio.wait_for(
            asyncio.to_thread(
                self._transport.request,
                HttpRequest(
                    method="POST",
                    url=url,
                    headers=request_headers,
                    body=body,
                    max_response_bytes=self._max_response_bytes,
                ),
                timeout_seconds=self._timeout_seconds,
            ),
            timeout=self._timeout_seconds,
        )
        if response.status not in expected_statuses:
            raise ChannelError(f"channel API returned HTTP {response.status}")
        if len(response.body) > self._max_response_bytes:
            raise ChannelResourceLimitError("channel API response exceeds byte bound")
        if not response.body:
            return {}
        try:
            decoded = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ChannelPayloadError("channel API returned invalid JSON") from error
        if not isinstance(decoded, Mapping):
            raise ChannelPayloadError("channel API returned a non-object JSON value")
        return cast(Mapping[str, object], decoded)

    async def get_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        expected_statuses: Sequence[int] = (200,),
    ) -> Mapping[str, object]:
        """Perform a bounded GET for provider token/bootstrap endpoints."""

        _validate_api_url(url, self._allowed_hosts)
        request_headers = {
            "Accept": "application/json",
            "User-Agent": "XDSH-channel/1",
        }
        request_headers.update(_safe_headers(headers))
        response = await asyncio.wait_for(
            asyncio.to_thread(
                self._transport.request,
                HttpRequest(
                    method="GET",
                    url=url,
                    headers=request_headers,
                    body=None,
                    max_response_bytes=self._max_response_bytes,
                ),
                timeout_seconds=self._timeout_seconds,
            ),
            timeout=self._timeout_seconds,
        )
        if response.status not in expected_statuses:
            raise ChannelError(f"channel API returned HTTP {response.status}")
        if len(response.body) > self._max_response_bytes:
            raise ChannelResourceLimitError("channel API response exceeds byte bound")
        if not response.body:
            return {}
        try:
            decoded = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ChannelPayloadError("channel API returned invalid JSON") from error
        if not isinstance(decoded, Mapping):
            raise ChannelPayloadError("channel API returned a non-object JSON value")
        return cast(Mapping[str, object], decoded)


def _validate_api_url(url: str, allowed_hosts: frozenset[str]) -> None:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise ChannelConfigurationError("channel API URL authority is invalid") from error
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.hostname.casefold() not in allowed_hosts
    ):
        raise ChannelConfigurationError(
            "channel API URL is outside its fixed HTTPS allowlist"
        )
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port not in (None, 443)
    ):
        raise ChannelConfigurationError(
            "channel API URL must not contain credentials, fragments, or non-HTTPS ports"
        )


def _safe_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    if headers is None:
        return {}
    result: dict[str, str] = {}
    for key, value in headers.items():
        normalized = str(key).casefold()
        if normalized in {"host", "content-length", "connection", "transfer-encoding"}:
            raise ChannelPayloadError("channel API headers cannot override transport framing")
        if (
            not isinstance(key, str)
            or not isinstance(value, str)
            or _CONTROL.search(key)
            or _CONTROL.search(value)
        ):
            raise ChannelPayloadError("channel API header is invalid")
        result[key] = value
    return result


def bounded_body(body: bytes, *, maximum: int = MAX_WEBHOOK_BYTES) -> bytes:
    """Reject an inbound body before parsing it."""

    if not isinstance(body, bytes) or not 1 <= maximum <= MAX_WEBHOOK_BYTES:
        raise ChannelResourceLimitError("webhook body bound is invalid")
    if len(body) > maximum:
        raise ChannelResourceLimitError("webhook body exceeds byte bound")
    return body


def parse_json_body(body: bytes, *, maximum: int = MAX_WEBHOOK_BYTES) -> Mapping[str, object]:
    """Decode one bounded JSON object without accepting arbitrary arrays."""

    bounded = bounded_body(body, maximum=maximum)
    try:
        value = json.loads(bounded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ChannelPayloadError("webhook body must be UTF-8 JSON") from error
    if not isinstance(value, Mapping):
        raise ChannelPayloadError("webhook body must be a JSON object")
    _check_json_bounds(value)
    return cast(Mapping[str, object], value)


def _check_json_bounds(value: object) -> None:
    """Reject deeply nested or node-heavy JSON before provider fields are used."""

    nodes = 0

    def visit(item: object, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_JSON_NODES:
            raise ChannelResourceLimitError("webhook JSON contains too many nodes")
        if depth > MAX_JSON_DEPTH:
            raise ChannelResourceLimitError("webhook JSON is nested too deeply")
        if isinstance(item, Mapping):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ChannelPayloadError("webhook JSON object key is invalid")
                visit(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                visit(child, depth + 1)

    visit(value, 0)


def header(headers: Mapping[str, str], name: str) -> str:
    """Read a case-insensitive HTTP header without exposing all headers."""

    wanted = name.casefold()
    for key, value in headers.items():
        if str(key).casefold() == wanted:
            return str(value)
    return ""


def require_text(value: object, field_name: str, *, maximum: int = 512) -> str:
    """Validate a bounded provider identifier or text field."""

    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or _CONTROL.search(value)
    ):
        raise ChannelPayloadError(f"channel field {field_name} is invalid")
    return value.strip()


def split_text(
    value: str,
    *,
    maximum_chars: int,
    maximum_chunks: int = 64,
) -> tuple[str, ...]:
    """Split one bounded answer for a provider message-size limit.

    Provider APIs enforce different per-message limits.  Chunking is done only
    after the runner has produced a bounded answer, and a hard chunk count
    keeps a pathological response from turning into an unbounded send loop.
    """

    if (
        not isinstance(value, str)
        or not value.strip()
        or not 1 <= maximum_chars <= MAX_MESSAGE_CHARS
        or not 1 <= maximum_chunks <= 256
    ):
        raise ChannelPayloadError("channel response chunk settings are invalid")
    normalized = safe_outbound_text(value).strip()
    if not normalized:
        raise ChannelPayloadError("channel response is empty after safety filtering")
    chunks = tuple(
        normalized[offset : offset + maximum_chars]
        for offset in range(0, len(normalized), maximum_chars)
    )
    if len(chunks) > maximum_chunks:
        raise ChannelResourceLimitError("channel response has too many provider chunks")
    return chunks


def optional_text(value: object, *, maximum: int = 512) -> str | None:
    if value is None:
        return None
    return require_text(value, "optional", maximum=maximum)


def query_value(query: Mapping[str, str], name: str) -> str:
    value = query.get(name, "")
    return value if isinstance(value, str) else ""


def bounded_query_value(
    query: Mapping[str, str],
    name: str,
    *,
    maximum: int = 4096,
) -> str:
    """Read a query value while applying the same bounds as webhook bodies."""

    value = query_value(query, name)
    if len(value) > maximum or _CONTROL.search(value):
        raise ChannelResourceLimitError(f"query value {name} exceeds its bound")
    return value


def verify_digest(
    expected: bytes,
    supplied: str,
    *,
    encoding: Literal["hex", "base64"] = "hex",
) -> None:
    """Compare a provider signature in constant time."""

    raw = supplied.strip()
    try:
        candidate = (
            bytes.fromhex(raw)
            if encoding == "hex"
            else base64.b64decode(raw, validate=True)
        )
    except (ValueError, binascii.Error) as error:
        raise ChannelAuthError("channel signature encoding is invalid") from error
    if not hmac.compare_digest(expected, candidate):
        raise ChannelAuthError("channel signature is invalid")


def hmac_digest(
    secret: str,
    message: bytes,
    *,
    algorithm: Literal["sha1", "sha256"] = "sha256",
) -> bytes:
    digest = hashlib.sha1 if algorithm == "sha1" else hashlib.sha256
    return hmac.new(secret.encode("utf-8"), message, digest).digest()


def hmac_hex(
    secret: str,
    message: bytes,
    *,
    algorithm: Literal["sha1", "sha256"] = "sha256",
) -> str:
    return hmac_digest(secret, message, algorithm=algorithm).hex()


def hmac_base64(
    secret: str,
    message: bytes,
    *,
    algorithm: Literal["sha1", "sha256"] = "sha256",
) -> str:
    return base64.b64encode(hmac_digest(secret, message, algorithm=algorithm)).decode("ascii")


def json_response(payload: Mapping[str, object], *, status: int = 200) -> ChannelHttpResponse:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return ChannelHttpResponse(status, body, {"Content-Type": "application/json; charset=utf-8"})


def text_response(text: str, *, status: int = 200) -> ChannelHttpResponse:
    value = require_text(text, "response", maximum=MAX_WEBHOOK_BYTES)
    return ChannelHttpResponse(
        status,
        value.encode("utf-8"),
        {"Content-Type": "text/plain; charset=utf-8"},
    )


def request_id() -> str:
    return uuid.uuid4().hex
