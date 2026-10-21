"""Observe official idle/wakeup reports independently of SDK ``Session.run``.

RC2 does not expose structured job IDs/outcomes through JSON-RPC. This observer
therefore reports native follow-up *output*, never inferred job success. No
prompt, command, stdout parsing, or private runtime file access is involved.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, replace

from xdsh.core.deferred_results import DeferredResult, DeferredResultContext
from xdsh.runners.dsh_events import _notification_parts
from xdsh.runners.sdk import NotificationSubscription

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class _ForegroundWindow:
    covered_sequence: int = -1
    completed: bool = False


@dataclass(frozen=True, slots=True)
class _PendingReport:
    result: DeferredResult
    context: DeferredResultContext
    foreground: _ForegroundWindow
    unbound_sequence: int


class DshDeferredObserver:
    def __init__(
        self,
        subscription: NotificationSubscription,
        *,
        session_id: str,
        native_session_id: str,
        runtime_generation: str,
    ) -> None:
        self._subscription = subscription
        self._session_id = session_id
        self._native_session_id = native_session_id
        self._generation = runtime_generation
        self._context: DeferredResultContext | None = None
        self._first_origin: str | None = None
        self._ambiguous = False
        self._foreground = True
        self._latest_sequence = -1
        self._notice = False
        self._text = ""
        self._pending: deque[_PendingReport] = deque()
        self._window = _ForegroundWindow()
        self._unbound_sequence = -1
        self._closed = False
        self._warned = False
        self._task = asyncio.create_task(self._watch(), name=f"dsh-deferred:{session_id}")

    def begin(self, context: DeferredResultContext | None) -> None:
        # Drain previously received idle/late frames under their original binding.
        self.poll()
        self._window = _ForegroundWindow()
        if context is None or (
            self._first_origin is not None and context.origin_id != self._first_origin
        ):
            # RC2 cannot identify which earlier prompt owns a completion notice.
            # Once more than one product turn can own jobs, stay conservative
            # until this runtime is retired, not merely until the next answer.
            self._ambiguous = True
        if self._first_origin is None and context is not None:
            self._first_origin = context.origin_id
        self._context = context
        self._foreground = True
        self._notice = False
        self._text = ""

    def record_foreground_notification(self, notification: object) -> None:
        """Record frames consumed by SDK run, not guesses based on idle timing."""

        method, payload = _notification_parts(notification)
        if method != "session.event" or not isinstance(payload, dict):
            return
        if payload.get("sessionId") != self._native_session_id:
            return
        event = payload.get("event")
        sequence = event.get("seq") if isinstance(event, dict) else None
        if type(sequence) is int and 0 <= sequence <= 2**53 - 1:
            self._window.covered_sequence = max(self._window.covered_sequence, sequence)

    def foreground_complete(self) -> None:
        self.poll()
        self._window.completed = True
        if self._unbound_sequence > self._window.covered_sequence:
            self._ambiguous = True
        self.poll()

    def poll(self) -> None:
        if self._closed:
            return
        try:
            self._subscription.drain(self._feed)
            self._publish_pending()
            self._warned = False
        except Exception as error:
            # Leave a failed persistence attempt pending; do not resend a prompt.
            # Log only the class, never provider content or report text.
            if not self._warned:
                LOGGER.warning("Deferred notification drain failed: %s", type(error).__name__)
                self._warned = True

    async def _watch(self) -> None:
        while not self._closed:
            self.poll()
            await asyncio.sleep(0.1 if not self._warned else 1.0)

    def close(self, *, persist_received: bool = False) -> None:
        if persist_received:
            self.poll()
        if self._pending:
            LOGGER.warning(
                "Closing deferred observer with %s unpersisted report(s)", len(self._pending)
            )
        self._closed = True
        self._pending.clear()
        self._subscription.close()
        self._task.cancel()

    def _publish_pending(self) -> None:
        while self._pending:
            pending = self._pending[0]
            if not pending.foreground.completed:
                return
            if pending.result.sequence <= pending.foreground.covered_sequence:
                # A startup/initialization idle or native follow-up fully
                # included in Session.run is NOT a separate late report.
                self._pending.popleft()
                continue
            result = pending.result
            if pending.unbound_sequence > pending.foreground.covered_sequence:
                result = replace(result, origin_id=None)
            pending.context.receive(result)
            self._pending.popleft()

    def _feed(self, notification: object) -> None:
        if self._closed:
            return
        method, payload = _notification_parts(notification)
        if not isinstance(payload, dict) or payload.get("sessionId") != self._native_session_id:
            return
        if method == "session.status":
            if payload.get("status") == "idle":
                self._foreground = False
            return
        if method != "session.event":
            return
        event = payload.get("event")
        if not isinstance(event, dict):
            return
        sequence, kind, data = event.get("seq"), event.get("type"), event.get("data")
        if (
            type(sequence) is not int
            or not 0 <= sequence <= 2**53 - 1
            or sequence <= self._latest_sequence
            or not isinstance(data, dict)
        ):
            return
        self._latest_sequence = sequence
        if self._foreground:
            return
        if kind == "agent/inbox/spliced":
            inserted = data.get("inserted")
            if not isinstance(inserted, list):
                return
            for message in inserted:
                source = message.get("source") if isinstance(message, dict) else None
                if isinstance(source, dict) and (
                    source.get("kind") == "plugin"
                    and source.get("plugin") == "tool-jobs"
                    and source.get("form") == "notice"
                ):
                    self._notice = True
                else:
                    self._unbound_sequence = sequence
                    if self._window.completed:
                        self._ambiguous = True
            return
        if kind == "assistant/message" and self._notice:
            message = data.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, list):
                text = "".join(
                    block["text"]
                    for block in content
                    if isinstance(block, dict)
                    and block.get("type") == "text"
                    and isinstance(block.get("text"), str)
                )
                if text:
                    self._text = text
            return
        if kind != "turn/end":
            return
        if self._notice and self._context is not None:
            reason = data.get("reason")
            reason_kind = reason.get("kind") if isinstance(reason, dict) else None
            result = DeferredResult(
                session_id=self._session_id,
                runtime_generation=self._generation,
                sequence=sequence,
                text=self._text,
                reason=reason_kind
                if isinstance(reason_kind, str)
                and reason_kind
                in {
                    "completed",
                    "error",
                    "aborted",
                    "blocked",
                }
                else "unknown",
                origin_id=None if self._ambiguous else self._first_origin,
            )
            self._pending.append(
                _PendingReport(
                    result,
                    self._context,
                    self._window,
                    self._unbound_sequence,
                )
            )
        self._notice = False
        self._text = ""
