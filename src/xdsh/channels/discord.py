"""Discord Interactions webhook adapter.

This adapter deliberately targets Discord's signed Interactions endpoint rather
than the Gateway.  Gateway/WebSocket lifecycle is an application concern; the
adapter can therefore remain dependency-free and loopback-friendly while still
supporting slash-command style interaction requests.
"""

from __future__ import annotations

import binascii
import importlib
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import quote, urlsplit

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
    header,
    json_response,
    parse_json_body,
    require_text,
    split_text,
)

SignatureVerifier = Callable[[bytes, bytes, bytes], bool]


@dataclass(frozen=True, slots=True)
class DiscordConfig:
    """Secret-free Discord application settings."""

    application_id: str
    public_key_hex: str
    bot_token: SecretRef | None = None
    api_base_url: str = "https://discord.com/api/v10"
    max_clock_skew_seconds: float = 300.0
    max_response_bytes: int = 2_000_000

    def __post_init__(self) -> None:
        if not self.application_id.isdigit() or not 1 <= len(self.application_id) <= 32:
            raise ChannelConfigurationError("Discord application ID is invalid")
        if len(self.public_key_hex) != 64:
            raise ChannelConfigurationError("Discord public key must be 32-byte hex")
        try:
            bytes.fromhex(self.public_key_hex)
        except ValueError as error:
            raise ChannelConfigurationError("Discord public key must be hex") from error
        if not 1.0 <= self.max_clock_skew_seconds <= 900.0:
            raise ChannelConfigurationError("Discord signature clock skew is out of bounds")
        parsed = urlsplit(self.api_base_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "discord.com"
            or parsed.port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or not parsed.path.startswith("/api/")
        ):
            raise ChannelConfigurationError("Discord API base URL must use discord.com HTTPS")


class DiscordInteractionsAdapter:
    """Verify Discord interactions and send deferred follow-up messages."""

    def __init__(
        self,
        config: DiscordConfig,
        *,
        transport: ChannelTransport | None = None,
        signature_verifier: SignatureVerifier | None = None,
        clock: Callable[[], float] = time.time,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.config = config
        self._environment = environment
        self._clock = clock
        self._verify_signature = signature_verifier or _verify_ed25519
        self._client = JsonChannelClient(
            ("discord.com",),
            transport=transport,
            max_response_bytes=config.max_response_bytes,
        )

    @property
    def name(self) -> ChannelName:
        return "discord"

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
            raise ChannelPayloadError("Discord interactions require POST")
        bounded = bounded_body(body)
        signature = header(headers, "X-Signature-Ed25519")
        timestamp = header(headers, "X-Signature-Timestamp")
        if not signature or not timestamp:
            raise ChannelAuthError("Discord interaction signature headers are missing")
        try:
            timestamp_seconds = float(timestamp)
        except ValueError as error:
            raise ChannelAuthError("Discord interaction timestamp is invalid") from error
        if not math.isfinite(timestamp_seconds) or abs(self._clock() - timestamp_seconds) > (
            self.config.max_clock_skew_seconds
        ):
            raise ChannelAuthError("Discord interaction timestamp is stale")
        try:
            signature_bytes = bytes.fromhex(signature)
        except ValueError as error:
            raise ChannelAuthError("Discord interaction signature is not hex") from error
        if len(signature_bytes) != 64:
            raise ChannelAuthError("Discord interaction signature length is invalid")
        try:
            public_key = bytes.fromhex(self.config.public_key_hex)
        except ValueError as error:  # validated by config, retain a fail-closed boundary
            raise ChannelConfigurationError("Discord public key is invalid") from error
        try:
            verified = self._verify_signature(
                public_key,
                timestamp.encode("ascii") + bounded,
                signature_bytes,
            )
        except Exception as error:
            if isinstance(error, ChannelAuthError):
                raise
            raise ChannelAuthError("Discord interaction signature verification failed") from error
        if not verified:
            raise ChannelAuthError("Discord interaction signature is invalid")
        payload = parse_json_body(bounded)
        interaction_type = payload.get("type")
        if interaction_type == 1:
            return WebhookDispatch(json_response({"type": 1}))
        if interaction_type != 2:
            return WebhookDispatch(json_response({"type": 5}))
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise ChannelPayloadError("Discord interaction data is invalid")
        command_name = require_text(data.get("name"), "command", maximum=100)
        user = _discord_user(payload)
        channel_id = require_text(payload.get("channel_id"), "channel_id", maximum=128)
        interaction_id = require_text(payload.get("id"), "interaction_id", maximum=128)
        token = require_text(payload.get("token"), "interaction_token", maximum=4096)
        text = _command_text(command_name, data.get("options"))
        message = ChannelMessage(
            channel="discord",
            sender_id=user,
            conversation_id=channel_id,
            text=text,
            message_id=interaction_id,
            reply_handle=token,
            raw_event_type="interaction.command",
        )
        # Defer immediately; the outer channel service sends the eventual
        # answer through the short-lived interaction webhook token.
        return WebhookDispatch(json_response({"type": 5}), message)

    async def send_text(self, message: ChannelMessage, text: str) -> None:
        if message.channel != "discord":
            raise ChannelPayloadError("Discord adapter received another channel message")
        chunks = split_text(text, maximum_chars=2_000, maximum_chunks=32)
        if self.config.bot_token is not None:
            channel_id = require_text(message.conversation_id, "channel_id", maximum=128)
            if not channel_id.isdigit():
                raise ChannelPayloadError("Discord channel ID is invalid")
            url = (
                f"{self.config.api_base_url.rstrip('/')}/channels/"
                f"{quote(channel_id, safe='')}/messages"
            )
            headers = {
                "Authorization": f"Bot {self.config.bot_token.resolve(self._environment)}"
            }
        else:
            url = (
                f"{self.config.api_base_url.rstrip('/')}/webhooks/"
                f"{quote(self.config.application_id, safe='')}/"
                f"{quote(message.reply_handle, safe='')}"
            )
            headers = None
        for chunk in chunks:
            # Discord may acknowledge a webhook follow-up with either a JSON
            # 200 (when a message object is returned) or an empty 204 response.
            await self._client.post_json(
                url,
                {"content": chunk},
                headers=headers,
                expected_statuses=(200, 204),
            )


def _verify_ed25519(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """Verify a Discord signature through the optional cryptography package."""

    try:
        exceptions_module = importlib.import_module("cryptography.exceptions")
        ed25519_module = importlib.import_module(
            "cryptography.hazmat.primitives.asymmetric.ed25519"
        )
        invalid_signature = cast(
            type[BaseException], vars(exceptions_module)["InvalidSignature"]
        )
        public_key_type = cast(Any, vars(ed25519_module)["Ed25519PublicKey"])
    except (ImportError, KeyError) as error:  # pragma: no cover - optional extra
        raise ChannelConfigurationError(
            "Discord verification requires the optional 'cryptography' dependency"
        ) from error
    try:
        public_key_type.from_public_bytes(public_key).verify(signature, message)
    except invalid_signature:
        return False
    except (ValueError, TypeError, binascii.Error) as error:
        raise ChannelAuthError("Discord public key or signature is invalid") from error
    return True


def _discord_user(payload: Mapping[str, object]) -> str:
    member = payload.get("member")
    if isinstance(member, Mapping):
        user = member.get("user")
        if isinstance(user, Mapping) and user.get("id") is not None:
            return require_text(user.get("id"), "sender_id", maximum=128)
    user = payload.get("user")
    if isinstance(user, Mapping) and user.get("id") is not None:
        return require_text(user.get("id"), "sender_id", maximum=128)
    raise ChannelPayloadError("Discord interaction has no sender")


def _command_text(name: str, options: object) -> str:
    if not isinstance(options, list):
        return f"/{name}"
    values = _flatten_options(options)
    suffix = " ".join(values)
    return f"/{name} {suffix}" if suffix else f"/{name}"


def _flatten_options(options: list[object]) -> list[str]:
    values: list[str] = []
    for option in options:
        if not isinstance(option, Mapping):
            continue
        option_name = option.get("name")
        option_value = option.get("value")
        option_type = option.get("type")
        if option_type in {1, 2} and isinstance(option_name, str) and option_name.strip():
            values.append(option_name.strip())
        if option_type not in {1, 2} and isinstance(
            option_value, str | int | float | bool
        ):
            value = str(option_value).strip()
            if value:
                values.append(value)
        elif option_type not in {1, 2} and isinstance(option_name, str) and option_value is None:
            values.append(option_name.strip())
        nested = option.get("options")
        if isinstance(nested, list):
            values.extend(_flatten_options(cast(list[object], nested)))
    return values
