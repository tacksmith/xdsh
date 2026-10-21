"""Runner-neutral messaging channel adapters.

Channel adapters only translate authenticated platform webhooks and outbound
API calls into the small :mod:`xdsh.channels.base` contract.  They do not own
sessions, prompts, or DSH state; an outer service supplies the message handler.
"""

from xdsh.channels.agent import ChannelAgentBridge, ChannelAgentConfig
from xdsh.channels.base import (
    ChannelAdapter,
    ChannelAuthError,
    ChannelConfigurationError,
    ChannelError,
    ChannelHttpResponse,
    ChannelMessage,
    ChannelPayloadError,
    ChannelResourceLimitError,
    ChannelTransport,
    JsonChannelClient,
    SecretRef,
    WebhookDispatch,
    split_text,
)
from xdsh.channels.commands import ChannelCommand, ChannelCommandRouter, parse_channel_command
from xdsh.channels.config import build_channel_adapter, channel_environment_status
from xdsh.channels.dingtalk import DingTalkConfig, DingTalkRobotAdapter
from xdsh.channels.discord import DiscordConfig, DiscordInteractionsAdapter
from xdsh.channels.feishu import FeishuConfig, FeishuEventAdapter
from xdsh.channels.http import ChannelHttpServer
from xdsh.channels.qq import (
    QQGatewayAdapter,
    QQGatewayCallbacks,
    QQGatewayConfig,
    QQInboundEvent,
    QQSDKRuntime,
    require_qq_sdk,
)
from xdsh.channels.router import CHANNEL_CATALOG, ChannelDescriptor, ChannelRouter
from xdsh.channels.wechat import WeChatConfig, WeChatOfficialAccountAdapter
from xdsh.channels.whatsapp import WhatsAppCloudAdapter, WhatsAppConfig

__all__ = [
    "CHANNEL_CATALOG",
    "ChannelAdapter",
    "ChannelAgentBridge",
    "ChannelAgentConfig",
    "ChannelAuthError",
    "ChannelCommand",
    "ChannelCommandRouter",
    "ChannelConfigurationError",
    "ChannelDescriptor",
    "ChannelError",
    "ChannelHttpResponse",
    "ChannelHttpServer",
    "ChannelMessage",
    "ChannelPayloadError",
    "ChannelResourceLimitError",
    "ChannelRouter",
    "ChannelTransport",
    "DingTalkConfig",
    "DingTalkRobotAdapter",
    "DiscordConfig",
    "DiscordInteractionsAdapter",
    "FeishuConfig",
    "FeishuEventAdapter",
    "JsonChannelClient",
    "QQGatewayAdapter",
    "QQGatewayCallbacks",
    "QQGatewayConfig",
    "QQInboundEvent",
    "QQSDKRuntime",
    "SecretRef",
    "WeChatConfig",
    "WeChatOfficialAccountAdapter",
    "WebhookDispatch",
    "WhatsAppCloudAdapter",
    "WhatsAppConfig",
    "build_channel_adapter",
    "channel_environment_status",
    "parse_channel_command",
    "require_qq_sdk",
    "split_text",
]
