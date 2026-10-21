"""WeChat Official Account server callback adapter.

This is the official public-account XML callback/API flow, not an unofficial
WeChat-Web automation bridge.  Enterprise WeChat (WeCom) has a different
signature and media protocol and is intentionally a separate future adapter.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import quote

from xdsh.channels.base import (
    ChannelAuthError,
    ChannelConfigurationError,
    ChannelError,
    ChannelMessage,
    ChannelName,
    ChannelPayloadError,
    ChannelResourceLimitError,
    ChannelTransport,
    JsonChannelClient,
    SecretRef,
    WebhookDispatch,
    bounded_body,
    bounded_query_value,
    require_text,
    split_text,
    text_response,
)

_SAFE_APP_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


@dataclass(frozen=True, slots=True)
class WeChatConfig:
    """Secret-free Official Account settings."""

    verification_token: SecretRef
    app_id: str | None = None
    app_secret: SecretRef | None = None
    api_base_url: str = "https://api.weixin.qq.com"
    max_response_bytes: int = 2_000_000

    def __post_init__(self) -> None:
        if self.app_id is not None and _SAFE_APP_ID.fullmatch(self.app_id) is None:
            raise ChannelConfigurationError("WeChat app ID is invalid")
        if (self.app_id is None) != (self.app_secret is None):
            raise ChannelConfigurationError(
                "WeChat app ID and app secret must be supplied together"
            )
        if self.api_base_url != "https://api.weixin.qq.com":
            raise ChannelConfigurationError("WeChat API base URL must be official HTTPS")
        if not 1 <= self.max_response_bytes <= 20_000_000:
            raise ChannelConfigurationError("WeChat response bound is invalid")


class WeChatOfficialAccountAdapter:
    """Authenticate XML callbacks and send customer-service text messages."""

    def __init__(
        self,
        config: WeChatConfig,
        *,
        transport: ChannelTransport | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.config = config
        self._environment = environment
        self._client = JsonChannelClient(
            ("api.weixin.qq.com",),
            transport=transport,
            max_response_bytes=config.max_response_bytes,
        )

    @property
    def name(self) -> ChannelName:
        return "wechat"

    def handle_webhook(
        self,
        method: str,
        *,
        query: Mapping[str, str],
        headers: Mapping[str, str],
        body: bytes,
    ) -> WebhookDispatch:
        del headers
        normalized_method = method.upper()
        if normalized_method not in {"GET", "POST"}:
            raise ChannelPayloadError("WeChat callbacks require GET or POST")
        self._verify_signature(query)
        if normalized_method == "GET":
            echo = bounded_query_value(query, "echostr", maximum=4096)
            if not echo:
                raise ChannelPayloadError("WeChat echo string is missing")
            return WebhookDispatch(text_response(echo))
        return self._decode_xml(bounded_body(body))

    async def send_text(self, message: ChannelMessage, text: str) -> None:
        if message.channel != "wechat":
            raise ChannelPayloadError("WeChat adapter received another channel message")
        if self.config.app_id is None or self.config.app_secret is None:
            raise ChannelConfigurationError(
                "WeChat outbound messaging requires app_id and app_secret"
            )
        chunks = split_text(text, maximum_chars=1_800, maximum_chunks=40)
        token_result = await self._client.get_json(
            f"{self.config.api_base_url}/cgi-bin/token"
            f"?grant_type=client_credential&appid={quote(self.config.app_id, safe='')}"
            f"&secret={quote(self.config.app_secret.resolve(self._environment), safe='')}"
        )
        token_code = token_result.get("errcode")
        if token_code is not None and token_code != 0 and token_code != "0":
            raise ChannelAuthError("WeChat access-token request failed")
        access_token = token_result.get("access_token")
        if not isinstance(access_token, str) or not access_token.strip():
            raise ChannelAuthError("WeChat response did not contain an access token")
        for chunk in chunks:
            result = await self._client.post_json(
                f"{self.config.api_base_url}/cgi-bin/message/custom/send"
                f"?access_token={quote(access_token, safe='')}",
                {
                    "touser": require_text(message.reply_handle, "open_id", maximum=128),
                    "msgtype": "text",
                    "text": {"content": chunk},
                },
                expected_statuses=(200,),
            )
            result_code = result.get("errcode")
            if result_code is not None and result_code != 0 and result_code != "0":
                raise ChannelError("WeChat message API rejected the response")

    def _verify_signature(self, query: Mapping[str, str]) -> None:
        signature = bounded_query_value(query, "signature", maximum=128)
        timestamp = bounded_query_value(query, "timestamp", maximum=64)
        nonce = bounded_query_value(query, "nonce", maximum=128)
        if not signature or not timestamp or not nonce:
            raise ChannelAuthError("WeChat signature parameters are missing")
        token = self.config.verification_token.resolve(self._environment)
        expected = hashlib.sha1("".join(sorted((token, timestamp, nonce))).encode()).hexdigest()
        if not hmac.compare_digest(signature.casefold(), expected):
            raise ChannelAuthError("WeChat callback signature is invalid")

    def _decode_xml(self, body: bytes) -> WebhookDispatch:
        upper_body = body.upper()
        if b"<!DOCTYPE" in upper_body or b"<!ENTITY" in upper_body:
            raise ChannelPayloadError("WeChat XML declarations and external entities are blocked")
        if body.count(b"<") > 2_048:
            raise ChannelResourceLimitError("WeChat XML contains too many elements")
        try:
            root = ET.fromstring(body)
        except (ET.ParseError, ValueError) as error:
            raise ChannelPayloadError("WeChat callback is not valid XML") from error
        if root.tag != "xml":
            raise ChannelPayloadError("WeChat callback root element is invalid")
        values: dict[str, str] = {}
        for child in root:
            if child.tag not in {"ToUserName", "FromUserName", "MsgType", "Content", "MsgId"}:
                continue
            text = child.text or ""
            values[child.tag] = require_text(text, child.tag, maximum=32_000)
        if values.get("MsgType") != "text":
            return WebhookDispatch(text_response("success"))
        sender = values.get("FromUserName")
        content = values.get("Content")
        if not sender or not content:
            raise ChannelPayloadError("WeChat text callback is missing sender or content")
        message_id = values.get("MsgId", f"wechat-{sender}")
        return WebhookDispatch(
            text_response("success"),
            ChannelMessage(
                channel="wechat",
                sender_id=sender,
                conversation_id=sender,
                text=content,
                message_id=message_id,
                reply_handle=sender,
                raw_event_type="official_account.text",
            ),
        )
