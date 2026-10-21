"""Bounded public process excerpts and conservative progress-card rotation.

Only text before a following tool call is process output. The last text block
belongs to the final-answer path. No reasoning or raw tool results enter here.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from xdsh.core.events import FinishEvent, RunnerEventT, TextEvent, TodoEvent, ToolStartEvent
from xdsh.core.product_control import safe_outbound_text
from xdsh.telegram.api import ProgressMessageApi, TelegramApiError

_KEY = re.compile(
    r"发现|發現|确认|確認|完成|结论|結論|阻塞|需要你|需要您|失败|失敗|"
    r"found|confirmed|completed|conclusion|blocked|need your|failed|[?\uff1f]|"
    r"確認|完了|発見|확인|완료|발견|confirmado|concluido|encontrado|"
    r"confirmé|terminé|trouvé|bestätigt|abgeschlossen|gefunden|"
    r"подтвержден|завершен|найден|تأكيد|اكتمل|نتيجة|पुष्टि|पूर्ण|selesai|ditemukan",
    re.IGNORECASE,
)
_PROCESS_EXCERPT_MAX_CHARS = 300


@dataclass(slots=True)
class ProcessUpdates:
    mode: str = "key"
    buffer: str = ""
    pending: list[str] = field(default_factory=list)
    seen: set[str] = field(default_factory=set)
    last_publish: float = float("-inf")
    card_created: float | None = None
    owned_card_id: int | None = None
    disabled: bool = False
    rotate_pending: bool = False
    retry_at: float = 0.0
    ended: bool = False
    card_send_uncertain: bool = False
    completed_todos: set[str] = field(default_factory=set)

    def claim_card(self, message_id: int) -> None:
        """Record the progress-card ID created by this task only."""
        if message_id <= 0:
            raise ValueError("progress card ID must be positive")
        self.owned_card_id = message_id

    def enqueue(self, text: str) -> None:
        text = safe_outbound_text(text, max_chars=_PROCESS_EXCERPT_MAX_CHARS)
        digest = hashlib.sha256(text.encode()).hexdigest()
        if text and digest not in self.seen:
            if len(self.seen) >= 2048 or len(self.pending) >= 16:
                # Fall back to the inline card instead of silently dropping
                # an unbounded stream or allowing messages to flood the chat.
                self.disabled = True
                return
            self.seen.add(digest)
            self.pending.append(text)

    def accept(self, event: RunnerEventT) -> None:
        if self.mode == "off" or self.disabled or self.ended:
            return
        if isinstance(event, TextEvent):
            self.buffer = (self.buffer + event.text)[:12000]
        elif isinstance(event, ToolStartEvent):
            text = safe_outbound_text(
                self.buffer.strip(), max_chars=_PROCESS_EXCERPT_MAX_CHARS
            )
            self.buffer = ""
            if not text or (self.mode == "key" and not _KEY.search(text)):
                return
            self.enqueue(text)
        elif isinstance(event, TodoEvent):
            for item in event.items:
                if item.status.casefold() in {"completed", "done"}:
                    digest = hashlib.sha256(item.content.encode()).hexdigest()
                    if digest not in self.completed_todos:
                        self.completed_todos.add(digest)
                        self.enqueue(item.content)
        elif isinstance(event, FinishEvent):
            self.buffer = ""  # Final text is delivered exactly by the answer path.
            self.ended = True
            self.pending.clear()

    async def publish(
        self, api: ProgressMessageApi, chat_id: int, card: str,
        card_id: int | None, *, now: float, final: bool = False,
    ) -> int | None:
        if self.mode == "off" or self.disabled or self.ended or now < self.retry_at:
            return card_id
        interval = 30.0 if self.mode == "key" else 10.0
        if not final and now - self.last_publish < interval:
            return card_id
        renewal = (card_id is not None and self.card_created is not None
                   and now - self.card_created >= 24 * 3600)
        if self.pending:
            # One compact batch, never one message per streaming token.
            batch: list[str] = []
            while self.pending and sum(map(len, batch)) + len(self.pending[0]) < 3500:
                batch.append(self.pending.pop(0))
            try:
                await api.send_silent_message(chat_id, "\n\n".join(batch))
            except TelegramApiError as error:
                if error.status_code == 429:
                    self.pending = batch + self.pending
                    self.retry_at = now + max(interval, error.retry_after_seconds or 0)
                else:
                    # An uncertain send may already exist. Never resend blindly.
                    self.disabled = True
                return card_id
            self.last_publish = now
            self.rotate_pending = True
        if self.ended or not (self.rotate_pending or renewal):
            return card_id
        try:
            new_id = await api.send_silent_message(chat_id, card)
        except TelegramApiError as error:
            if error.status_code == 429:
                self.retry_at = now + max(interval, error.retry_after_seconds or 0)
            else:
                self.disabled = True
                self.card_send_uncertain = True
            return card_id  # Keep the old card until a new ID is known.
        self.card_created = now
        self.rotate_pending = False
        self.last_publish = now
        owns_old_card = card_id is not None and card_id == self.owned_card_id
        self.owned_card_id = new_id
        if owns_old_card and card_id is not None:
            try:
                await api.delete_message(chat_id, card_id)
            except TelegramApiError:
                # At most one retired card remains; don't accumulate more.
                self.disabled = True
        return new_id
