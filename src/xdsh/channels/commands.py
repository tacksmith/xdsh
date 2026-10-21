"""Runner-neutral slash-command routing shared by non-Telegram channels.

Transport adapters only authenticate and normalize messages.  This module
provides a tiny, explicit command seam so Discord, WhatsApp, DingTalk, Feishu,
and WeChat can register the same product handlers without importing Telegram
or making a channel-specific copy of command parsing.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from xdsh.channels.base import ChannelConfigurationError, ChannelMessage

_COMMAND_NAME = re.compile(r"^[a-z0-9_]{1,32}$")
_MAX_ARGUMENT_CHARS = 8_000


@dataclass(frozen=True, slots=True)
class ChannelCommand:
    """A normalized command received from any supported message transport."""

    name: str
    arguments: str = ""

    def __post_init__(self) -> None:
        if _COMMAND_NAME.fullmatch(self.name) is None:
            raise ChannelConfigurationError("channel command name is invalid")
        if len(self.arguments) > _MAX_ARGUMENT_CHARS or "\x00" in self.arguments:
            raise ChannelConfigurationError("channel command arguments exceed the bound")


CommandHandler = Callable[[ChannelMessage, str], Awaitable[str | None]]


class ChannelCommandRouter:
    """Dispatch explicitly registered commands with one shared contract."""

    def __init__(self) -> None:
        self._handlers: dict[str, CommandHandler] = {}

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._handlers))

    def handles(self, name: str) -> bool:
        """Return whether a command is explicitly registered."""

        return _normalize_name(name) in self._handlers

    def register(self, name: str, handler: CommandHandler) -> None:
        normalized = _normalize_name(name)
        if normalized in self._handlers:
            raise ChannelConfigurationError(f"channel command {normalized} is registered twice")
        self._handlers[normalized] = handler

    async def dispatch(self, message: ChannelMessage) -> str | None:
        command = parse_channel_command(message.text)
        if command is None:
            return None
        handler = self._handlers.get(command.name)
        if handler is None:
            return None
        result = await handler(message, command.arguments)
        if result is not None and not isinstance(result, str):
            raise ChannelConfigurationError("channel command handler returned non-text output")
        if isinstance(result, str) and len(result) > 60_000:
            raise ChannelConfigurationError("channel command output exceeds the bound")
        return result


def parse_channel_command(text: str) -> ChannelCommand | None:
    """Parse a leading slash token, including an optional bot suffix."""

    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    token, separator, remainder = stripped.partition(" ")
    name = token[1:].split("@", maxsplit=1)[0].casefold()
    if _COMMAND_NAME.fullmatch(name) is None:
        return None
    arguments = remainder.strip() if separator else ""
    return ChannelCommand(name=name, arguments=arguments)


def _normalize_name(value: str) -> str:
    if not isinstance(value, str):
        raise ChannelConfigurationError("channel command name is invalid")
    normalized = value.strip().casefold()
    if _COMMAND_NAME.fullmatch(normalized) is None:
        raise ChannelConfigurationError("channel command name is invalid")
    return normalized


__all__ = ["ChannelCommand", "ChannelCommandRouter", "CommandHandler", "parse_channel_command"]
