"""DingTalk robot callback adapter using the official signed webhook shape.

DingTalk has several bot products (custom robots, enterprise robots, and
stream robots) with different transports.  This module covers the HTTP event
payload that includes a short-lived ``sessionWebhook`` and the documented
timestamp/sign HMAC guard.  Stream/WebSocket lifecycle is intentionally kept
outside this small HTTP adapter.
"""

from __future__ import annotations

import hmac
import json
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.parse import unquote

from xdsh.channels.base import (
    ChannelAuthError,
    ChannelConfigurationError,
    ChannelMessage,
    ChannelName,
    ChannelPayloadError,
    ChannelTransport,
    JsonChannelClient,
    SecretRef,
    WebhookDispatch,
    bounded_body,
    bounded_query_value,
    header,
    hmac_base64,
    json_response,
    parse_json_body,
    require_text,
    split_text,
    text_response,
)


@dataclass(frozen=True, slots=True)
class DingTalkConfig:
    """Secret-free DingTalk robot settings."""

    signing_secret: SecretRef
    max_clock_skew_seconds: float = 300.0
    max_response_bytes: int = 2_000_000

    def __post_init__(self) -> None:
        if not 1.0 <= self.max_clock_skew_seconds <= 3_600.0:
            raise ChannelConfigurationError("DingTalk signature clock skew is out of bounds")
        if not 1 <= self.max_response_bytes <= 20_000_000:
            raise ChannelConfigurationError("DingTalk response bound is invalid")


class DingTalkRobotAdapter:
    """Authenticate DingTalk robot events and reply through session webhooks."""

    def __init__(
        self,
        config: DingTalkConfig,
        *,
        transport: ChannelTransport | None = None,
        environment: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self._environment = environment
        self._clock = clock
        self._client = JsonChannelClient(
            ("oapi.dingtalk.com",),
            transport=transport,
            max_response_bytes=config.max_response_bytes,
        )

    @property
    def name(self) -> ChannelName:
        return "dingtalk"

    def handle_webhook(
        self,
        method: str,
        *,
        query: Mapping[str, str],
        headers: Mapping[str, str],
        body: bytes,
    ) -> WebhookDispatch:
        normalized_method = method.upper()
        if normalized_method not in {"GET", "POST"}:
            raise ChannelPayloadError("DingTalk callbacks require GET or POST")
        timestamp, sign = _signature_parts(query, headers)
        self._verify_signature(timestamp, sign)
        if normalized_method == "GET":
            challenge = bounded_query_value(query, "challenge", maximum=4096)
            if not challenge:
                raise ChannelPayloadError("DingTalk challenge is missing")
            return WebhookDispatch(text_response(challenge))
        payload = parse_json_body(bounded_body(body))
        # Some stream callbacks wrap the event JSON in a string-valued data
        # field.  Decode that wrapper while retaining the same bounds.
        wrapped = payload.get("data")
        if isinstance(wrapped, str) and wrapped.strip().startswith("{"):
            try:
                decoded = json.loads(wrapped)
            except json.JSONDecodeError as error:
                raise ChannelPayloadError("DingTalk data wrapper is invalid JSON") from error
            if isinstance(decoded, Mapping):
                payload = decoded
        return WebhookDispatch(json_response({"ok": True}), _decode_message(payload))

    async def send_text(self, message: ChannelMessage, text: str) -> None:
        if message.channel != "dingtalk":
            raise ChannelPayloadError("DingTalk adapter received another channel message")
        chunks = split_text(text, maximum_chars=16_000, maximum_chunks=16)
        webhook = require_text(message.reply_handle, "session_webhook", maximum=4096)
        # The session webhook is supplied by DingTalk, but it is still treated
        # as untrusted input and constrained to the official HTTPS host.
        if not webhook.startswith("https://oapi.dingtalk.com/"):
            raise ChannelAuthError("DingTalk session webhook host is not allowed")
        for chunk in chunks:
            await self._client.post_json(
                webhook,
                {"msgtype": "text", "text": {"content": chunk}},
                expected_statuses=(200,),
            )

    def _verify_signature(self, timestamp: str, supplied: str) -> None:
        if not timestamp or not supplied:
            raise ChannelAuthError("DingTalk signature parameters are missing")
        try:
            timestamp_value = float(timestamp) / (1000.0 if len(timestamp) >= 12 else 1.0)
        except ValueError as error:
            raise ChannelAuthError("DingTalk signature timestamp is invalid") from error
        if not math.isfinite(timestamp_value) or abs(self._clock() - timestamp_value) > (
            self.config.max_clock_skew_seconds
        ):
            raise ChannelAuthError("DingTalk signature timestamp is stale")
        secret = self.config.signing_secret.resolve(self._environment)
        expected = hmac_base64(secret, f"{timestamp}\n{secret}".encode())
        # Query strings commonly carry URL-encoded signatures.
        candidate = unquote(supplied).replace(" ", "+").strip()
        if not hmac.compare_digest(candidate, expected):
            raise ChannelAuthError("DingTalk signature is invalid")


def _signature_parts(query: Mapping[str, str], headers: Mapping[str, str]) -> tuple[str, str]:
    timestamp = bounded_query_value(query, "timestamp", maximum=64)
    sign = bounded_query_value(query, "sign", maximum=1024)
    if not timestamp:
        timestamp = bounded_query_value(query, "eventTimestamp", maximum=64)
    if not sign:
        timestamp = timestamp or header(headers, "timestamp")
        sign = header(headers, "sign") or header(headers, "X-DingTalk-Signature")
    return timestamp, sign


def _decode_message(payload: Mapping[str, object]) -> ChannelMessage | None:
    message_type = payload.get("msgtype")
    if message_type not in {None, "text"}:
        return None
    text_value = payload.get("text")
    if not isinstance(text_value, Mapping):
        return None
    content = require_text(text_value.get("content"), "message_text", maximum=32_000)
    sender = payload.get("senderId") or payload.get("senderStaffId") or payload.get("sender_id")
    conversation = (
        payload.get("conversationId")
        or payload.get("openConversationId")
        or payload.get("conversation_id")
    )
    message_id = payload.get("msgId") or payload.get("messageId") or payload.get("message_id")
    webhook = payload.get("sessionWebhook") or payload.get("session_webhook")
    return ChannelMessage(
        channel="dingtalk",
        sender_id=require_text(sender, "sender_id", maximum=512),
        conversation_id=require_text(conversation, "conversation_id", maximum=512),
        text=content,
        message_id=require_text(message_id, "message_id", maximum=512),
        reply_handle=require_text(webhook, "session_webhook", maximum=4096),
        raw_event_type="robot.text",
    )
