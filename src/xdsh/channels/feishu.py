"""Feishu/Lark event-subscription adapter.

Only the official HTTP event and tenant-access-token APIs are used.  The
adapter supports URL verification, signed plain events, and encrypted events
when the optional ``cryptography`` extra is installed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import importlib
import json
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast

from xdsh.channels.base import (
    MAX_WEBHOOK_BYTES,
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
    header,
    json_response,
    parse_json_body,
    require_text,
    split_text,
)


@dataclass(frozen=True, slots=True)
class FeishuConfig:
    """Secret-free Feishu/Lark application settings."""

    app_id: str
    app_secret: SecretRef
    verification_token: SecretRef
    encrypt_key: SecretRef | None = None
    api_base_url: str = "https://open.feishu.cn"
    max_clock_skew_seconds: float = 600.0
    max_response_bytes: int = 2_000_000

    def __post_init__(self) -> None:
        if not self.app_id.strip() or len(self.app_id) > 128 or any(
            character.isspace() for character in self.app_id
        ):
            raise ChannelConfigurationError("Feishu app ID is invalid")
        if self.api_base_url != "https://open.feishu.cn":
            raise ChannelConfigurationError("Feishu API base URL must be official HTTPS")
        if not 1.0 <= self.max_clock_skew_seconds <= 3_600.0:
            raise ChannelConfigurationError("Feishu signature clock skew is out of bounds")
        if not 1 <= self.max_response_bytes <= 20_000_000:
            raise ChannelConfigurationError("Feishu response bound is invalid")


class FeishuEventAdapter:
    """Authenticate Feishu callbacks and send text through the IM API."""

    def __init__(
        self,
        config: FeishuConfig,
        *,
        transport: ChannelTransport | None = None,
        environment: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self._environment = environment
        self._clock = clock
        self._client = JsonChannelClient(
            ("open.feishu.cn",),
            transport=transport,
            max_response_bytes=config.max_response_bytes,
        )

    @property
    def name(self) -> ChannelName:
        return "feishu"

    def handle_webhook(
        self,
        method: str,
        *,
        query: Mapping[str, str],
        headers: Mapping[str, str],
        body: bytes,
    ) -> WebhookDispatch:
        del query
        if method.upper() != "POST":
            raise ChannelPayloadError("Feishu event callbacks require POST")
        bounded = bounded_body(body)
        payload = parse_json_body(bounded)
        if payload.get("type") == "url_verification":
            return self._url_verification(payload)
        self._authenticate_event(payload, headers, bounded)
        decrypted = _decrypt_payload(payload, self.config.encrypt_key, self._environment)
        if decrypted.get("type") == "url_verification":
            return self._url_verification(decrypted)
        payload = decrypted
        return WebhookDispatch(json_response({"code": 0}), _decode_message(payload))

    async def send_text(self, message: ChannelMessage, text: str) -> None:
        if message.channel != "feishu":
            raise ChannelPayloadError("Feishu adapter received another channel message")
        chunks = split_text(text, maximum_chars=28_000, maximum_chunks=16)
        token_result = await self._client.post_json(
            f"{self.config.api_base_url}/open-apis/auth/v3/tenant_access_token/internal",
            {
                "app_id": self.config.app_id,
                "app_secret": self.config.app_secret.resolve(self._environment),
            },
            expected_statuses=(200,),
        )
        code = token_result.get("code")
        if code is not None and code != 0 and code != "0":
            raise ChannelAuthError("Feishu tenant access token request failed")
        access_token = token_result.get("tenant_access_token")
        if not isinstance(access_token, str) or not access_token.strip():
            raise ChannelAuthError("Feishu response did not contain a tenant access token")
        url = (
            f"{self.config.api_base_url}/open-apis/im/v1/messages"
            f"?receive_id_type=chat_id"
        )
        for chunk in chunks:
            await self._client.post_json(
                url,
                {
                    "receive_id": require_text(message.reply_handle, "chat_id", maximum=256),
                    "msg_type": "text",
                    "content": json.dumps({"text": chunk}, ensure_ascii=False),
                },
                headers={"Authorization": f"Bearer {access_token}"},
                expected_statuses=(200,),
            )

    def _url_verification(self, payload: Mapping[str, object]) -> WebhookDispatch:
        supplied = require_text(payload.get("token"), "verification_token", maximum=4096)
        expected = self.config.verification_token.resolve(self._environment)
        if not hmac.compare_digest(supplied, expected):
            raise ChannelAuthError("Feishu URL verification token is invalid")
        challenge = require_text(payload.get("challenge"), "challenge", maximum=4096)
        return WebhookDispatch(json_response({"challenge": challenge}))

    def _authenticate_event(
        self,
        payload: Mapping[str, object],
        headers: Mapping[str, str],
        body: bytes,
    ) -> None:
        timestamp = header(headers, "X-Lark-Request-Timestamp")
        nonce = header(headers, "X-Lark-Request-Nonce")
        signature = header(headers, "X-Lark-Signature")
        if timestamp or nonce or signature:
            if not timestamp or not nonce or not signature or self.config.encrypt_key is None:
                raise ChannelAuthError("Feishu signed event headers are incomplete")
            try:
                timestamp_value = float(timestamp)
            except ValueError as error:
                raise ChannelAuthError("Feishu event timestamp is invalid") from error
            if not math.isfinite(timestamp_value) or abs(self._clock() - timestamp_value) > (
                self.config.max_clock_skew_seconds
            ):
                raise ChannelAuthError("Feishu event timestamp is stale")
            key = self.config.encrypt_key.resolve(self._environment)
            expected = hashlib.sha256(
                f"{timestamp}{nonce}{key}".encode() + body
            ).hexdigest()
            if not hmac.compare_digest(signature.casefold(), expected):
                raise ChannelAuthError("Feishu event signature is invalid")
            return
        supplied = payload.get("token")
        if not isinstance(supplied, str) or not hmac.compare_digest(
            supplied, self.config.verification_token.resolve(self._environment)
        ):
            raise ChannelAuthError("Feishu event verification token is invalid")


def _decrypt_payload(
    payload: Mapping[str, object],
    encrypt_key: SecretRef | None,
    environment: Mapping[str, str] | None,
) -> Mapping[str, object]:
    encrypted = payload.get("encrypt")
    if encrypted is None:
        return payload
    if not isinstance(encrypted, str) or encrypt_key is None:
        raise ChannelPayloadError("Feishu encrypted event is invalid")
    try:
        ciphers_module = importlib.import_module("cryptography.hazmat.primitives.ciphers")
        cipher_type = cast(Any, vars(ciphers_module)["Cipher"])
        algorithms_module = importlib.import_module(
            "cryptography.hazmat.primitives.ciphers.algorithms"
        )
        aes_type = cast(Any, vars(algorithms_module)["AES"])
        modes_module = importlib.import_module("cryptography.hazmat.primitives.ciphers.modes")
        cbc_type = cast(Any, vars(modes_module)["CBC"])
    except (ImportError, KeyError) as error:  # pragma: no cover - optional extra
        raise ChannelConfigurationError(
            "Feishu encrypted events require the optional 'cryptography' dependency"
        ) from error
    try:
        packed = base64.b64decode(encrypted, validate=True)
        key = hashlib.sha256(encrypt_key.resolve(environment).encode("utf-8")).digest()
        # Feishu prepends a random 16-byte IV to the PKCS#7-padded AES-CBC
        # ciphertext.  The plaintext is the JSON body itself (there is no
        # extra length/random-string header in this protocol).
        if len(packed) < 32 or len(packed[16:]) % 16 != 0:
            raise ValueError("encrypted event has an invalid length")
        iv = packed[:16]
        decryptor = cipher_type(aes_type(key), cbc_type(iv)).decryptor()
        padded = decryptor.update(packed[16:]) + decryptor.finalize()
        padding = padded[-1]
        if not 1 <= padding <= 16 or padded[-padding:] != bytes([padding]) * padding:
            raise ValueError("encrypted event padding is invalid")
        clear = padded[:-padding]
        if not clear or len(clear) > MAX_WEBHOOK_BYTES:
            raise ValueError("encrypted event body is out of bounds")
        decoded = json.loads(clear.decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ChannelPayloadError("Feishu encrypted event could not be decoded") from error
    if not isinstance(decoded, Mapping):
        raise ChannelPayloadError("Feishu decrypted event must be an object")
    return cast(Mapping[str, object], decoded)


def _decode_message(payload: Mapping[str, object]) -> ChannelMessage | None:
    header_payload = payload.get("header")
    event = payload.get("event")
    if not isinstance(event, Mapping):
        return None
    event_type = (
        header_payload.get("event_type")
        if isinstance(header_payload, Mapping)
        else event.get("type")
    )
    if event_type != "im.message.receive_v1":
        return None
    message = event.get("message")
    sender = event.get("sender")
    if not isinstance(message, Mapping):
        raise ChannelPayloadError("Feishu message object is invalid")
    if not isinstance(sender, Mapping):
        raise ChannelPayloadError("Feishu sender object is invalid")
    message_type = require_text(message.get("message_type"), "message_type", maximum=64)
    if message_type != "text":
        return None
    content_raw = message.get("content")
    if not isinstance(content_raw, str):
        raise ChannelPayloadError("Feishu message content is invalid")
    try:
        content = json.loads(content_raw)
    except json.JSONDecodeError as error:
        raise ChannelPayloadError("Feishu message content is not JSON") from error
    if not isinstance(content, Mapping):
        raise ChannelPayloadError("Feishu message content object is invalid")
    text = require_text(content.get("text"), "message_text", maximum=32_000)
    sender_id = _sender_id(sender)
    chat_id = require_text(message.get("chat_id"), "chat_id", maximum=256)
    message_id = require_text(message.get("message_id"), "message_id", maximum=256)
    return ChannelMessage(
        channel="feishu",
        sender_id=sender_id,
        conversation_id=chat_id,
        text=text,
        message_id=message_id,
        reply_handle=chat_id,
        raw_event_type="im.message.receive_v1",
    )


def _sender_id(sender: Mapping[str, object]) -> str:
    sender_id = sender.get("sender_id")
    if isinstance(sender_id, Mapping):
        for key in ("open_id", "user_id", "union_id"):
            value = sender_id.get(key)
            if isinstance(value, str) and value.strip():
                return require_text(value, "sender_id", maximum=256)
    raise ChannelPayloadError("Feishu sender ID is missing")
