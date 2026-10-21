"""Runner-neutral channel routing and bounded webhook de-duplication."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Literal, cast

from xdsh.channels.base import (
    ChannelAdapter,
    ChannelConfigurationError,
    ChannelMessage,
    ChannelName,
    ChannelPayloadError,
    WebhookDispatch,
    WebhookMethod,
)

MessageHandler = Callable[[ChannelMessage], Awaitable[str | None]]


@dataclass(frozen=True, slots=True)
class ChannelDescriptor:
    """Public, credential-free capability metadata for one channel."""

    name: ChannelName
    label: str
    inbound: str
    outbound: str
    required_environment: tuple[str, ...]
    optional_environment: tuple[str, ...] = ()
    transport: Literal["webhook", "websocket"] = "webhook"
    requires_public_https: bool = True


CHANNEL_CATALOG: tuple[ChannelDescriptor, ...] = (
    ChannelDescriptor(
        "discord",
        "Discord",
        "signed Interactions HTTP",
        "interaction follow-up webhook",
        ("XDSH_DISCORD_APPLICATION_ID", "XDSH_DISCORD_PUBLIC_KEY_HEX"),
        ("XDSH_DISCORD_BOT_TOKEN",),
    ),
    ChannelDescriptor(
        "whatsapp",
        "WhatsApp Cloud API",
        "Meta webhook",
        "Graph API text message",
        (
            "XDSH_WHATSAPP_PHONE_NUMBER_ID",
            "XDSH_WHATSAPP_VERIFY_TOKEN",
            "XDSH_WHATSAPP_APP_SECRET",
            "XDSH_WHATSAPP_ACCESS_TOKEN",
        ),
    ),
    ChannelDescriptor(
        "dingtalk",
        "DingTalk",
        "signed robot callback",
        "sessionWebhook text message",
        ("XDSH_DINGTALK_SIGNING_SECRET",),
    ),
    ChannelDescriptor(
        "feishu",
        "Feishu / Lark",
        "event subscription HTTP",
        "tenant token + IM API",
        (
            "XDSH_FEISHU_APP_ID",
            "XDSH_FEISHU_APP_SECRET",
            "XDSH_FEISHU_VERIFICATION_TOKEN",
        ),
        ("XDSH_FEISHU_ENCRYPT_KEY",),
    ),
    ChannelDescriptor(
        "wechat",
        "WeChat Official Account",
        "signed XML callback",
        "customer-service API text message",
        ("XDSH_WECHAT_VERIFICATION_TOKEN",),
        ("XDSH_WECHAT_APP_ID", "XDSH_WECHAT_APP_SECRET"),
    ),
    ChannelDescriptor(
        "qq",
        "QQ Official Bot",
        "official WebSocket Gateway",
        "OpenAPI v2 text message",
        (
            "XDSH_QQ_APP_ID",
            "XDSH_QQ_APP_SECRET",
            "XDSH_QQ_ALLOWED_USERS",
        ),
        (
            "XDSH_QQ_ALLOWED_GROUPS",
            "XDSH_QQ_MARKDOWN",
            "XDSH_QQ_MAX_INFLIGHT",
            "XDSH_QQ_MAX_PENDING",
        ),
        transport="websocket",
        requires_public_https=False,
    ),
)


class ChannelRouter:
    """Route authenticated webhooks and invoke a caller-owned message handler.

    The router deliberately owns no sessions, prompts, DSH state, or
    credentials.  A service can pass the same callback used by the CLI or
    Telegram transport.  Message IDs are de-duplicated in a small in-memory
    window so provider retries do not launch duplicate agent turns.
    """

    def __init__(
        self,
        adapters: tuple[ChannelAdapter, ...] = (),
        *,
        clock: Callable[[], float] = time.monotonic,
        replay_ttl_seconds: float = 900.0,
        max_replay_entries: int = 2_048,
    ) -> None:
        if not 1.0 <= replay_ttl_seconds <= 86_400.0:
            raise ChannelConfigurationError("channel replay TTL is out of bounds")
        if not 16 <= max_replay_entries <= 100_000:
            raise ChannelConfigurationError("channel replay cache size is out of bounds")
        self._clock = clock
        self._replay_ttl = replay_ttl_seconds
        self._max_replay_entries = max_replay_entries
        self._adapters: dict[ChannelName, ChannelAdapter] = {}
        self._seen: dict[tuple[ChannelName, str], float] = {}
        for adapter in adapters:
            self.register(adapter)

    def register(self, adapter: ChannelAdapter) -> None:
        name = adapter.name
        if name in self._adapters:
            raise ChannelConfigurationError(f"channel adapter {name} is registered twice")
        if name not in {descriptor.name for descriptor in CHANNEL_CATALOG}:
            raise ChannelConfigurationError(f"channel adapter {name} is not in the catalog")
        self._adapters[name] = adapter

    def adapter(self, name: str) -> ChannelAdapter:
        try:
            normalized = name.casefold()
            return self._adapters[normalized]  # type: ignore[index]
        except (AttributeError, KeyError) as error:
            raise ChannelConfigurationError(
                f"channel adapter {name!r} is not configured"
            ) from error

    @property
    def names(self) -> tuple[ChannelName, ...]:
        return tuple(sorted(self._adapters))

    def dispatch(
        self,
        name: str,
        method: WebhookMethod | str,
        *,
        query: Mapping[str, str],
        headers: Mapping[str, str],
        body: bytes,
    ) -> WebhookDispatch:
        adapter = self.adapter(name)
        normalized_method = method.upper()
        if normalized_method not in {"GET", "POST"}:
            raise ChannelPayloadError("channel webhook method must be GET or POST")
        return adapter.handle_webhook(
            cast(WebhookMethod, normalized_method), query=query, headers=headers, body=body
        )

    async def handle(
        self,
        name: str,
        method: WebhookMethod | str,
        *,
        query: Mapping[str, str],
        headers: Mapping[str, str],
        body: bytes,
        message_handler: MessageHandler,
    ) -> WebhookDispatch:
        dispatch = self.dispatch(
            name,
            method,
            query=query,
            headers=headers,
            body=body,
        )
        await self.process(name, dispatch, message_handler=message_handler)
        return dispatch

    async def process(
        self,
        name: str,
        dispatch: WebhookDispatch,
        *,
        message_handler: MessageHandler,
    ) -> bool:
        """Process a previously dispatched message.

        HTTP servers can return ``dispatch.response`` immediately (important
        for Discord's three-second interaction deadline) and schedule this
        method in the background.  The boolean reports whether a new message
        was actually handled; provider retries return ``False``.
        """

        message = dispatch.message
        if message is None or self._is_replay(message):
            return False
        try:
            answer = await message_handler(message)
            if answer is not None and answer.strip():
                await self.adapter(name).send_text(message, answer)
        except BaseException:
            self._seen.pop((message.channel, message.message_id), None)
            raise
        return True

    def _is_replay(self, message: ChannelMessage) -> bool:
        now = self._clock()
        expired = [key for key, seen_at in self._seen.items() if now - seen_at > self._replay_ttl]
        for key in expired:
            self._seen.pop(key, None)
        key = (message.channel, message.message_id)
        if key in self._seen:
            return True
        self._seen[key] = now
        while len(self._seen) > self._max_replay_entries:
            oldest = min(self._seen, key=self._seen.__getitem__)
            self._seen.pop(oldest, None)
        return False


def catalog_by_name(name: str) -> ChannelDescriptor:
    """Return catalog metadata without reading any credential values."""

    normalized = name.casefold()
    for descriptor in CHANNEL_CATALOG:
        if descriptor.name == normalized:
            return descriptor
    raise ChannelConfigurationError(f"unknown channel {name!r}")
