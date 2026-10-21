"""Telegram transport and multimodal handlers."""

from typing import TYPE_CHECKING

from xdsh.telegram.config import TelegramConfig, TelegramConfigurationError

if TYPE_CHECKING:
    from xdsh.telegram.service import TelegramService

__all__ = ["TelegramConfig", "TelegramConfigurationError", "TelegramService"]


def __getattr__(name: str) -> object:
    """Keep the service export without preloading its executable module."""

    if name == "TelegramService":
        from xdsh.telegram.service import TelegramService

        return TelegramService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
