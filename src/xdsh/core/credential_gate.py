"""One-shot, in-memory approval for credential-bearing foreground tasks.

The product normally refuses to put credential-shaped prompts in durable task
state.  This module provides the narrow exception needed for an operator who
explicitly confirms one task: the raw prompt stays in memory, the approval is
bound to one chat and user, and consuming or expiring the ticket removes it.
No prompt, token, or decision payload is persisted by this module.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Literal

from xdsh.core.product_control import redact_sensitive_text

_MAX_PROMPT_CHARS = 12_000
_DEFAULT_TTL_SECONDS = 300.0
_DEFAULT_MAX_PENDING = 64
_TOKEN_BYTES = 16

ApprovalState = Literal["pending", "approved"]


class CredentialApprovalError(ValueError):
    """A credential approval request is malformed or outside its bounds."""


@dataclass(frozen=True, slots=True)
class PendingCredentialTask:
    """A volatile task awaiting (or holding) one explicit approval.

    ``text`` is deliberately excluded from ``repr`` so accidental diagnostics
    cannot print the credential-bearing prompt.
    """

    token: str
    chat_id: int
    user_id: int
    session_key: str
    created_at: float
    text: str = field(repr=False)
    state: ApprovalState = "pending"


class CredentialApprovalGate:
    """Bounded volatile approval state for one-shot credential tasks."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] | None = None,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        max_pending: int = _DEFAULT_MAX_PENDING,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(ttl_seconds, int | float) or isinstance(ttl_seconds, bool):
            raise ValueError("credential approval TTL must be numeric")
        if not 30.0 <= float(ttl_seconds) <= 900.0:
            raise ValueError("credential approval TTL must be between 30 and 900 seconds")
        if not 1 <= max_pending <= 1_024:
            raise ValueError("credential approval capacity must be between 1 and 1024")
        self._clock = clock or time.monotonic
        self.ttl_seconds = float(ttl_seconds)
        self.max_pending = max_pending
        self._token_factory = token_factory or (
            lambda: secrets.token_urlsafe(_TOKEN_BYTES).replace("-", "_").replace("/", "_")
        )
        self._pending: dict[int, PendingCredentialTask] = {}

    @staticmethod
    def requires_approval(text: str) -> bool:
        """Return whether the text changes under the shared credential redactor."""

        return redact_sensitive_text(text) != text

    def stage(
        self,
        *,
        chat_id: int,
        user_id: int,
        session_key: str,
        text: str,
    ) -> PendingCredentialTask:
        """Replace the chat's prior ticket with one fresh pending ticket."""

        normalized = _validate_text(text)
        if not self.requires_approval(normalized):
            raise CredentialApprovalError("task does not contain a credential-shaped value")
        if not isinstance(chat_id, int) or isinstance(chat_id, bool) or chat_id == 0:
            raise CredentialApprovalError("chat ID is invalid")
        if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id == 0:
            raise CredentialApprovalError("user ID is invalid")
        if not session_key.strip():
            raise CredentialApprovalError("session key is invalid")
        self._purge()
        if chat_id not in self._pending and len(self._pending) >= self.max_pending:
            self._evict_oldest()
        token = self._new_token()
        created_at = self._now()
        pending = PendingCredentialTask(
            token=token,
            chat_id=chat_id,
            user_id=user_id,
            session_key=session_key.strip(),
            created_at=created_at,
            text=normalized,
        )
        self._pending[chat_id] = pending
        return pending

    def approve(
        self,
        *,
        chat_id: int,
        user_id: int,
        token: str,
    ) -> PendingCredentialTask | None:
        """Atomically mark a matching pending ticket approved.

        A second click, a mismatched user, and an expired ticket all return
        ``None`` and leave no executable approval behind.
        """

        pending = self._matching(chat_id=chat_id, user_id=user_id, token=token)
        if pending is None or pending.state != "pending":
            return None
        approved = replace(pending, state="approved")
        self._pending[chat_id] = approved
        return approved

    def reject(self, *, chat_id: int, user_id: int, token: str) -> bool:
        """Remove a matching pending or approved ticket without executing it."""

        pending = self._matching(chat_id=chat_id, user_id=user_id, token=token)
        if pending is None:
            return False
        self._pending.pop(chat_id, None)
        return True

    def take_approved(self, *, chat_id: int, user_id: int) -> PendingCredentialTask | None:
        """Consume one approved ticket; consumption is the one-shot boundary."""

        self._purge()
        pending = self._pending.get(chat_id)
        if pending is None or pending.state != "approved" or pending.user_id != user_id:
            return None
        self._pending.pop(chat_id, None)
        return pending

    def approved_user_id(self, chat_id: int) -> int | None:
        """Return only the owner ID of an approved ticket, never its prompt."""

        self._purge()
        pending = self._pending.get(chat_id)
        if pending is None or pending.state != "approved":
            return None
        return pending.user_id

    def retain_approved(self, pending: PendingCredentialTask) -> None:
        """Put a not-yet-started approved ticket back after a transient busy race."""

        if pending.state != "approved":
            raise CredentialApprovalError("only approved tickets can be retained")
        self._purge()
        if self._now() - pending.created_at >= self.ttl_seconds:
            return
        current = self._pending.get(pending.chat_id)
        if current is not None and current.token != pending.token:
            # A newer task is already awaiting its own decision; never let an
            # older in-flight approval overwrite it.
            return
        self._pending[pending.chat_id] = pending

    def clear(self, chat_id: int | None = None) -> None:
        """Forget volatile prompts, normally during shutdown or explicit cleanup."""

        if chat_id is None:
            self._pending.clear()
        else:
            self._pending.pop(chat_id, None)

    def pending_count(self) -> int:
        """Return a content-free count for health/tests."""

        self._purge()
        return len(self._pending)

    def has_pending(self, chat_id: int) -> bool:
        """Return whether one non-expired ticket exists for a chat."""

        self._purge()
        return chat_id in self._pending

    def _matching(self, *, chat_id: int, user_id: int, token: str) -> PendingCredentialTask | None:
        self._purge()
        if not isinstance(token, str):
            return None
        pending = self._pending.get(chat_id)
        if pending is None:
            return None
        if pending.user_id != user_id or not secrets.compare_digest(pending.token, token):
            return None
        return pending

    def _purge(self) -> None:
        now = self._now()
        expired = [
            chat_id
            for chat_id, pending in self._pending.items()
            if now - pending.created_at >= self.ttl_seconds
        ]
        for chat_id in expired:
            self._pending.pop(chat_id, None)

    def _evict_oldest(self) -> None:
        oldest = min(self._pending.values(), key=lambda item: item.created_at)
        self._pending.pop(oldest.chat_id, None)

    def _new_token(self) -> str:
        for _ in range(8):
            token = self._token_factory()
            if isinstance(token, str) and token and token not in {
                item.token for item in self._pending.values()
            }:
                return token
        raise CredentialApprovalError("unable to allocate a unique approval token")

    def _now(self) -> float:
        value = self._clock()
        if not isinstance(value, int | float) or isinstance(value, bool) or value < 0:
            raise CredentialApprovalError("credential approval clock is invalid")
        return float(value)


def _validate_text(value: str) -> str:
    if not isinstance(value, str):
        raise CredentialApprovalError("task text must be a string")
    normalized = value.strip()
    if not 1 <= len(normalized) <= _MAX_PROMPT_CHARS:
        raise CredentialApprovalError("task text must contain 1-12000 characters")
    if any(
        (ord(character) < 32 and character not in "\n\t") or ord(character) == 127
        for character in normalized
    ):
        raise CredentialApprovalError("task text contains unsafe control characters")
    return normalized
