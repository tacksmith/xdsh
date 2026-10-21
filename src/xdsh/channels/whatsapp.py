"""WhatsApp Cloud API webhook and outbound message adapter.

The adapter intentionally targets Meta's official Cloud API.  It does not use
WhatsApp Web automation or an unofficial bridge.  Webhook verification and
message authentication happen before any JSON field is trusted; credentials
are environment references and are never retained in a message or log.
"""

from __future__ import annotations

import hmac
import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import quote

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
    hmac_digest,
    json_response,
    parse_json_body,
    require_text,
    split_text,
    text_response,
)

_VERSION = re.compile(r"^v[0-9]+(?:\.[0-9]+)?$")


@dataclass(frozen=True, slots=True)
class WhatsAppConfig:
    """Secret-free WhatsApp Cloud API settings."""

    phone_number_id: str
    verify_token: SecretRef
    app_secret: SecretRef
    access_token: SecretRef
    api_version: str = "v21.0"
    graph_base_url: str = "https://graph.facebook.com"
    max_response_bytes: int = 2_000_000

    def __post_init__(self) -> None:
        if not self.phone_number_id.isdigit() or not 1 <= len(self.phone_number_id) <= 32:
            raise ChannelConfigurationError("WhatsApp phone number ID is invalid")
        if _VERSION.fullmatch(self.api_version) is None:
            raise ChannelConfigurationError("WhatsApp Graph API version is invalid")
        if self.graph_base_url != "https://graph.facebook.com":
            raise ChannelConfigurationError(
                "WhatsApp Graph API base URL must be the official HTTPS host"
            )
        if not 1 <= self.max_response_bytes <= 20_000_000:
            raise ChannelConfigurationError("WhatsApp response bound is invalid")


class WhatsAppCloudAdapter:
    """Decode authenticated Cloud API webhooks and send text messages."""

    def __init__(
        self,
        config: WhatsAppConfig,
        *,
        transport: ChannelTransport | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.config = config
        self._environment = environment
        self._client = JsonChannelClient(
            ("graph.facebook.com",),
            transport=transport,
            max_response_bytes=config.max_response_bytes,
        )

    @property
    def name(self) -> ChannelName:
        return "whatsapp"

    def handle_webhook(
        self,
        method: str,
        *,
        query: Mapping[str, str],
        headers: Mapping[str, str],
        body: bytes,
    ) -> WebhookDispatch:
        normalized_method = method.upper()
        if normalized_method == "GET":
            return self._verify_subscription(query)
        if normalized_method != "POST":
            raise ChannelPayloadError("WhatsApp webhooks require GET or POST")
        bounded = bounded_body(body)
        supplied = header(headers, "X-Hub-Signature-256").strip()
        if not supplied.casefold().startswith("sha256="):
            raise ChannelAuthError("WhatsApp webhook signature is missing")
        expected = hmac_digest(
            self.config.app_secret.resolve(self._environment),
            bounded,
            algorithm="sha256",
        ).hex()
        candidate = supplied[7:].strip().casefold()
        if len(candidate) != len(expected) or not hmac.compare_digest(candidate, expected):
            raise ChannelAuthError("WhatsApp webhook signature is invalid")
        payload = parse_json_body(bounded)
        object_name = payload.get("object")
        if object_name != "whatsapp_business_account":
            raise ChannelPayloadError("WhatsApp webhook object is unsupported")
        message = _first_message(payload)
        return WebhookDispatch(json_response({"ok": True}), message)

    async def send_text(self, message: ChannelMessage, text: str) -> None:
        if message.channel != "whatsapp":
            raise ChannelPayloadError("WhatsApp adapter received another channel message")
        chunks = split_text(text, maximum_chars=4_096, maximum_chunks=16)
        url = (
            f"{self.config.graph_base_url}/{self.config.api_version}/"
            f"{quote(self.config.phone_number_id, safe='')}/messages"
        )
        for chunk in chunks:
            await self._client.post_json(
                url,
                {
                    "messaging_product": "whatsapp",
                    "recipient_type": "individual",
                    "to": message.reply_handle,
                    "type": "text",
                    "text": {"preview_url": False, "body": chunk},
                },
                headers={
                    "Authorization": f"Bearer {self.config.access_token.resolve(self._environment)}"
                },
            )

    def _verify_subscription(self, query: Mapping[str, str]) -> WebhookDispatch:
        mode = bounded_query_value(query, "hub.mode", maximum=64)
        supplied_token = bounded_query_value(query, "hub.verify_token", maximum=4096)
        challenge = bounded_query_value(query, "hub.challenge", maximum=256)
        expected_token = self.config.verify_token.resolve(self._environment)
        if mode != "subscribe" or not hmac.compare_digest(supplied_token, expected_token):
            raise ChannelAuthError("WhatsApp webhook verification failed")
        if not challenge:
            raise ChannelPayloadError("WhatsApp verification challenge is missing")
        return WebhookDispatch(text_response(challenge))


def _first_message(payload: Mapping[str, object]) -> ChannelMessage | None:
    entries = payload.get("entry")
    if not isinstance(entries, list):
        raise ChannelPayloadError("WhatsApp webhook entry is invalid")
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        changes = entry.get("changes")
        if not isinstance(changes, list):
            continue
        for change in changes:
            if not isinstance(change, Mapping):
                continue
            value = change.get("value")
            if not isinstance(value, Mapping):
                continue
            messages = value.get("messages")
            if not isinstance(messages, list):
                continue
            for message in messages:
                decoded = _decode_message(message)
                if decoded is not None:
                    return decoded
    return None


def _decode_message(value: object) -> ChannelMessage | None:
    if not isinstance(value, Mapping):
        return None
    message_type = value.get("type")
    if message_type != "text":
        return None
    sender = require_text(value.get("from"), "sender_id", maximum=64)
    message_id = require_text(value.get("id"), "message_id", maximum=256)
    text = value.get("text")
    if not isinstance(text, Mapping):
        raise ChannelPayloadError("WhatsApp text message body is invalid")
    content = require_text(text.get("body"), "message_text", maximum=32_000)
    return ChannelMessage(
        channel="whatsapp",
        sender_id=sender,
        conversation_id=sender,
        text=content,
        message_id=message_id,
        reply_handle=sender,
        raw_event_type="messages.text",
    )
