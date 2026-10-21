"""Private-chat provider onboarding; raw credentials never reach task routing."""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from xdsh.core.approval_i18n import approval_text
from xdsh.core.provider_presets import PROVIDER_PRESETS
from xdsh.core.provider_ui_i18n import provider_ui
from xdsh.core.providers import ProviderError, ProviderRegistry, ProviderWizard
from xdsh.telegram.api import InlineKeyboardButton, InlineKeyboardMarkup


@dataclass(frozen=True, slots=True)
class SetupReply:
    text: str
    markup: InlineKeyboardMarkup | None = None


@dataclass(slots=True)
class _Pending:
    user_id: int
    token: str
    created_at: float
    wizard: ProviderWizard = field(default_factory=ProviderWizard, repr=False)


class TelegramProviderSetup:
    """Bounded volatile wizard, with chat/user/token-bound one-shot approval."""

    def __init__(
        self,
        registry: ProviderRegistry | None,
        *,
        clock: Callable[[], float] = time.monotonic,
        language: Callable[[int], str] = lambda chat: "en",
    ) -> None:
        self.registry = registry
        self._clock = clock
        self._language = language
        self._pending: dict[int, _Pending] = {}

    def _text(self, chat_id: int, key: str) -> str:
        return provider_ui(self._language(chat_id), key)

    def catalog(self, page: int = 0, *, chat_id: int = 0) -> SetupReply:
        size = 8
        pages = (len(PROVIDER_PRESETS) + size - 1) // size
        page = min(max(page, 0), pages - 1)
        buttons: list[tuple[InlineKeyboardButton, ...]] = [
            (
                InlineKeyboardButton(
                    ("⚙️ " if preset.engine else "🔒 ") + preset.id,
                    "model.preset." + preset.id,
                ),
            )
            for preset in PROVIDER_PRESETS[page * size : (page + 1) * size]
        ]
        buttons.append(
            (
                InlineKeyboardButton("←", f"model.catalog.{max(0, page - 1)}"),
                InlineKeyboardButton("→", f"model.catalog.{min(pages - 1, page + 1)}"),
            )
        )
        buttons.append((InlineKeyboardButton(
            "✏️ " + self._text(chat_id, "custom"), "model.custom",
        ),))
        return SetupReply(
            f"🌐 {page + 1}/{pages}\n" + self._text(chat_id, "catalog"),
            InlineKeyboardMarkup(tuple(buttons)),
        )

    def start(self, chat_id: int, user_id: int, preset_id: str | None = None) -> SetupReply:
        if self.registry is None:
            return SetupReply(self._text(chat_id, "unavailable"))
        if chat_id != user_id:
            return SetupReply("🔒 " + self._text(chat_id, "private"))
        self._pending.pop(chat_id, None)
        self._purge_other(chat_id)
        if len(self._pending) >= 32 and chat_id not in self._pending:
            return SetupReply(self._text(chat_id, "busy"))
        pending = _Pending(user_id, secrets.token_hex(12), self._clock())
        notice = ""
        if preset_id is not None:
            preset = next((item for item in PROVIDER_PRESETS if item.id == preset_id), None)
            if preset is None:
                return SetupReply(self._text(chat_id, "invalid"))
            if not preset.engine:
                return SetupReply(
                    f"🔒 {preset.id}\n" + self._text(chat_id, "unsupported")
                )
            for value in (preset.id, preset.engine):
                pending.wizard.advance(value)
            if preset.url:
                pending.wizard.advance(preset.url)
            notice = f"provider={preset.id}\nprotocol={preset.engine}\n"
            if preset.url:
                notice += f"URL: {preset.url}\n"
            notice += self._text(chat_id, "catalog") + "\n"
        self._pending[chat_id] = pending
        return SetupReply(
            "⚙️ " + self._text(chat_id, "intro") + "\n"
            + notice
            + self._text(chat_id, f"step{pending.wizard.step}")
        )

    def consume(self, chat_id: int, user_id: int, text: str) -> SetupReply | None:
        pending = self._pending.get(chat_id)
        if pending is None or pending.user_id != user_id:
            return None
        if self._clock() - pending.created_at >= 600:
            self._pending.pop(chat_id, None)
            return SetupReply(self._text(chat_id, "expired"))
        control_command = text.split()[0].split("@")[0] if text.split() else ""
        if control_command in {"/cancel", "/model", "/help", "/start"}:
            self._pending.pop(chat_id, None)
            if control_command == "/cancel":
                return SetupReply(self._text(chat_id, "cancelled"))
            return None
        if pending.wizard.step == 5:
            return SetupReply(self._text(chat_id, "confirm"))
        try:
            route = pending.wizard.advance(text)
        except ProviderError:
            return SetupReply("⚠️ " + self._text(chat_id, "invalid") + "\n"
                              + self._text(chat_id, f"step{pending.wizard.step}"))
        if route is None:
            return SetupReply(self._text(chat_id, f"step{pending.wizard.step}"))
        return SetupReply(
            "🔐 " + self._text(chat_id, "confirm") + "\n\n"
            + f"provider={route.provider}\nprotocol={route.engine}\nURL={route.base_url}\n"
            + f"model={route.model}\ncredential=env:{route.credential_env}",
            InlineKeyboardMarkup(
                (
                    (
                        InlineKeyboardButton(
                            "✅ " + approval_text(self._language(chat_id), "approve"),
                            f"model.setup.approve.{pending.token}"
                        ),
                        InlineKeyboardButton(
                            "✖ " + approval_text(self._language(chat_id), "reject"),
                            f"model.setup.reject.{pending.token}"
                        ),
                    ),
                )
            ),
        )

    def decide(self, chat_id: int, user_id: int, data: str) -> SetupReply | None:
        pending = self._pending.get(chat_id)
        if pending is None or pending.user_id != user_id:
            return None
        action, _, token = data.removeprefix("model.setup.").partition(".")
        if action not in {"approve", "reject"} or not secrets.compare_digest(token, pending.token):
            return None
        self._pending.pop(chat_id, None)
        if self._clock() - pending.created_at >= 600:
            return SetupReply(self._text(chat_id, "expired"))
        if action == "reject":
            return SetupReply(self._text(chat_id, "cancelled"))
        try:
            if self.registry is None:
                raise ProviderError("Managed provider registry unavailable")
            route = pending.wizard.connection()
            self.registry.add(route, approved=True, credential=pending.wizard.secret)
        except (ProviderError, OSError):
            return SetupReply("⚠️ " + self._text(chat_id, "failed"))
        return SetupReply(
            "✅ " + self._text(chat_id, "saved") + f"\nmodel={route.model}"
        )

    def _purge_other(self, current_chat: int) -> None:
        for chat in tuple(self._pending):
            if chat != current_chat and self._clock() - self._pending[chat].created_at >= 600:
                self._pending.pop(chat, None)

    def clear(self) -> None:
        self._pending.clear()
