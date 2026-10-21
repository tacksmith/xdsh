"""Environment-only channel configuration and adapter construction."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from xdsh.channels.base import (
    ChannelAdapter,
    ChannelConfigurationError,
    ChannelTransport,
    SecretRef,
)
from xdsh.channels.dingtalk import DingTalkConfig, DingTalkRobotAdapter
from xdsh.channels.discord import DiscordConfig, DiscordInteractionsAdapter
from xdsh.channels.feishu import FeishuConfig, FeishuEventAdapter
from xdsh.channels.qq import QQGatewayConfig
from xdsh.channels.router import CHANNEL_CATALOG, ChannelDescriptor
from xdsh.channels.wechat import WeChatConfig, WeChatOfficialAccountAdapter
from xdsh.channels.whatsapp import WhatsAppCloudAdapter, WhatsAppConfig


def channel_environment_status(
    environment: Mapping[str, str] | None = None,
) -> tuple[dict[str, object], ...]:
    """Return names-only readiness facts; never return secret values."""

    source = os.environ if environment is None else environment
    statuses: list[dict[str, object]] = []
    for descriptor in CHANNEL_CATALOG:
        missing = tuple(
            name for name in descriptor.required_environment if not source.get(name, "").strip()
        )
        present_optional = tuple(
            name for name in descriptor.optional_environment if source.get(name, "").strip()
        )
        statuses.append(
            {
                "name": descriptor.name,
                "label": descriptor.label,
                "ready": not missing,
                "missing_required": missing,
                "present_optional": present_optional,
                "inbound": descriptor.inbound,
                "outbound": descriptor.outbound,
                "transport": descriptor.transport,
                "requires_public_https": descriptor.requires_public_https,
            }
        )
    return tuple(statuses)


def build_channel_adapter(
    name: str,
    environment: Mapping[str, str] | None = None,
    *,
    transport: ChannelTransport | None = None,
) -> ChannelAdapter:
    """Build one adapter from environment references and non-secret IDs."""

    source = os.environ if environment is None else environment
    normalized = name.casefold()
    if normalized == "discord":
        bot_token = (
            SecretRef("XDSH_DISCORD_BOT_TOKEN")
            if source.get("XDSH_DISCORD_BOT_TOKEN", "").strip()
            else None
        )
        return DiscordInteractionsAdapter(
            DiscordConfig(
                application_id=_required(source, "XDSH_DISCORD_APPLICATION_ID"),
                public_key_hex=_required(source, "XDSH_DISCORD_PUBLIC_KEY_HEX"),
                bot_token=bot_token,
            ),
            transport=transport,
            environment=source,
        )
    if normalized == "whatsapp":
        return WhatsAppCloudAdapter(
            WhatsAppConfig(
                phone_number_id=_required(source, "XDSH_WHATSAPP_PHONE_NUMBER_ID"),
                verify_token=SecretRef("XDSH_WHATSAPP_VERIFY_TOKEN"),
                app_secret=SecretRef("XDSH_WHATSAPP_APP_SECRET"),
                access_token=SecretRef("XDSH_WHATSAPP_ACCESS_TOKEN"),
                api_version=source.get("XDSH_WHATSAPP_API_VERSION", "v21.0").strip(),
            ),
            transport=transport,
            environment=source,
        )
    if normalized == "dingtalk":
        return DingTalkRobotAdapter(
            DingTalkConfig(signing_secret=SecretRef("XDSH_DINGTALK_SIGNING_SECRET")),
            transport=transport,
            environment=source,
        )
    if normalized == "feishu":
        encrypt = (
            SecretRef("XDSH_FEISHU_ENCRYPT_KEY")
            if source.get("XDSH_FEISHU_ENCRYPT_KEY", "").strip()
            else None
        )
        _required(source, "XDSH_FEISHU_VERIFICATION_TOKEN")
        return FeishuEventAdapter(
            FeishuConfig(
                app_id=_required(source, "XDSH_FEISHU_APP_ID"),
                app_secret=SecretRef("XDSH_FEISHU_APP_SECRET"),
                verification_token=SecretRef(
                    "XDSH_FEISHU_VERIFICATION_TOKEN"
                ),
                encrypt_key=encrypt,
            ),
            transport=transport,
            environment=source,
        )
    if normalized == "wechat":
        app_id = source.get("XDSH_WECHAT_APP_ID", "").strip() or None
        app_secret_value = source.get("XDSH_WECHAT_APP_SECRET", "").strip()
        app_secret = SecretRef("XDSH_WECHAT_APP_SECRET") if app_secret_value else None
        return WeChatOfficialAccountAdapter(
            WeChatConfig(
                verification_token=SecretRef("XDSH_WECHAT_VERIFICATION_TOKEN"),
                app_id=app_id,
                app_secret=app_secret,
            ),
            transport=transport,
            environment=source,
        )
    if normalized == "qq":
        raise ChannelConfigurationError(
            "QQ uses the outbound WebSocket gateway; use build_qq_gateway_config"
        )
    raise ChannelConfigurationError(f"unknown channel {name!r}")


def build_qq_gateway_config(
    environment: Mapping[str, str] | None = None,
    *,
    state_root: Path,
) -> QQGatewayConfig:
    """Build fail-closed QQ Gateway settings without retaining secret values."""

    source = os.environ if environment is None else environment
    _reject_qq_endpoint_overrides(source)
    _required(source, "XDSH_QQ_APP_SECRET")
    # Validate the credential shape during ``--check`` while retaining only
    # its environment-variable name in the resulting configuration object.
    SecretRef("XDSH_QQ_APP_SECRET").resolve(source)
    return QQGatewayConfig(
        app_id=_required(source, "XDSH_QQ_APP_ID"),
        app_secret_env="XDSH_QQ_APP_SECRET",
        state_root=Path(state_root),
        allowed_sender_ids=_identifier_set(source, "XDSH_QQ_ALLOWED_USERS", required=True),
        allowed_group_ids=_identifier_set(
            source,
            "XDSH_QQ_ALLOWED_GROUPS",
            required=False,
        ),
        markdown=_boolean(source.get("XDSH_QQ_MARKDOWN", "off"), "XDSH_QQ_MARKDOWN"),
        max_inflight=_integer(
            source.get("XDSH_QQ_MAX_INFLIGHT", "4"),
            "XDSH_QQ_MAX_INFLIGHT",
        ),
        max_pending=_integer(
            source.get("XDSH_QQ_MAX_PENDING", "32"),
            "XDSH_QQ_MAX_PENDING",
        ),
    )


def descriptor(name: str) -> ChannelDescriptor:
    normalized = name.casefold()
    for item in CHANNEL_CATALOG:
        if item.name == normalized:
            return item
    raise ChannelConfigurationError(f"unknown channel {name!r}")


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
    if not value:
        raise ChannelConfigurationError(f"missing required channel setting {name}")
    return value


def _identifier_set(
    environment: Mapping[str, str],
    name: str,
    *,
    required: bool,
) -> frozenset[str]:
    raw = environment.get(name, "")
    values = frozenset(part.strip() for part in raw.split(",") if part.strip())
    if required and not values:
        raise ChannelConfigurationError(f"missing required channel setting {name}")
    if len(values) > 256:
        raise ChannelConfigurationError(f"channel setting {name} has too many identifiers")
    return values


def _boolean(raw: str, name: str) -> bool:
    normalized = raw.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ChannelConfigurationError(f"channel setting {name} must be on or off")


def _integer(raw: str, name: str) -> int:
    try:
        return int(raw.strip())
    except ValueError as error:
        raise ChannelConfigurationError(f"channel setting {name} must be an integer") from error


def _reject_qq_endpoint_overrides(environment: Mapping[str, str]) -> None:
    official = {
        "QQ_API_BASE": "https://api.sgroup.qq.com",
        "QQ_TOKEN_URL": "https://bots.qq.com/app/getAppAccessToken",
        "QQ_PORTAL_HOST": "q.qq.com",
    }
    for name, expected in official.items():
        configured = environment.get(name, "").strip()
        if configured and configured != expected:
            raise ChannelConfigurationError("QQ must use official endpoints")
