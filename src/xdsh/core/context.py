"""Runner-neutral context-pressure and compatibility-compaction policy.

The official DSH rc.2 composition can compact automatically inside its agent
loop, but its SDK/stdio JSON-RPC surface does not expose a manual compact
request.  This pure policy remains the fallback for runners without a verified
native auto-compaction capability: the caller applies a bounded product-history
checkpoint and advances that runner's generation.  Telegram, the CLI, and
future transports can therefore share one conservative compatibility path.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Literal, TypeAlias

from xdsh.core.decision_router import DecisionOption, DecisionRequest

CompactionDecisionReason: TypeAlias = Literal[
    "disabled",
    "warning",
    "below_threshold",
    "insufficient_history",
    "cooldown",
    "ready",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextCompactionPolicy:
    """Bounded automatic-compaction settings.

    ``threshold_ratio`` is deliberately below the hard provider limit.  The
    default ratio of 50% is also passed to the official DSH backend.  A 64K
    absolute ceiling protects large-window routes because the rc.2 token meter
    deliberately estimates unanchored CJK/JSON surfaces at four characters
    per token and can otherwise trigger too late.
    ``target_ratio`` is informational for the deterministic outer fallback:
    its bounded resume payload is expected to land below this target after the
    next generation starts.
    """

    enabled: bool = True
    warning_ratio: float = 0.20
    threshold_ratio: float = 0.50
    target_ratio: float = 0.20
    threshold_tokens: int | None = 64_000
    min_turns: int = 1
    cooldown_turns: int = 1

    def __post_init__(self) -> None:
        for name, value in (
            ("warning_ratio", self.warning_ratio),
            ("threshold_ratio", self.threshold_ratio),
            ("target_ratio", self.target_ratio),
        ):
            if not math.isfinite(value):
                raise ValueError(f"context compaction {name} must be finite")
        if not 0.05 <= self.warning_ratio < self.threshold_ratio:
            raise ValueError(
                "context compaction warning must be below the trigger threshold"
            )
        if not 0.10 <= self.threshold_ratio < 0.95:
            raise ValueError("context compaction threshold must be between 0.10 and 0.95")
        if not 0.05 <= self.target_ratio < self.threshold_ratio:
            raise ValueError(
                "context compaction target must be at least 0.05 and below the threshold"
            )
        if self.threshold_tokens is not None and self.threshold_tokens < 1:
            raise ValueError("context compaction absolute threshold must be positive")
        if self.min_turns < 1:
            raise ValueError("context compaction minimum turns must be positive")
        if self.cooldown_turns < 0:
            raise ValueError("context compaction cooldown turns cannot be negative")

    def evaluate(
        self,
        *,
        latest_input_tokens: int,
        context_window_tokens: int,
        retained_turns: int,
        turns_since_compaction: int,
    ) -> ContextCompactionDecision:
        """Return a stable decision from one structured usage observation."""

        if context_window_tokens <= 0:
            raise ValueError("context window must be positive")
        if latest_input_tokens < 0 or retained_turns < 0 or turns_since_compaction < 0:
            raise ValueError("context pressure values cannot be negative")
        # Match the official rc.2 compaction-basic calculation exactly:
        # ``floor(routedContextWindow * thresholdRatio)``.  Keeping the
        # boundary identical prevents native and compatibility paths from
        # disagreeing by one token on non-integral products.
        threshold_tokens = max(1, math.floor(context_window_tokens * self.threshold_ratio))
        if self.threshold_tokens is not None:
            threshold_tokens = min(threshold_tokens, self.threshold_tokens)
        threshold_tokens = min(threshold_tokens, context_window_tokens)
        target_tokens = max(1, math.ceil(context_window_tokens * self.target_ratio))
        # An absolute early threshold may be below the ratio-derived target;
        # keep the reported target strictly below the effective trigger.
        target_tokens = min(target_tokens, max(1, threshold_tokens - 1))
        warning_tokens = max(1, math.ceil(context_window_tokens * self.warning_ratio))
        warning_tokens = min(warning_tokens, max(1, threshold_tokens - 1))
        reason: CompactionDecisionReason
        if not self.enabled:
            reason = "disabled"
        elif latest_input_tokens < warning_tokens:
            reason = "below_threshold"
        elif latest_input_tokens < threshold_tokens:
            reason = "warning"
        elif retained_turns < self.min_turns:
            reason = "insufficient_history"
        elif turns_since_compaction < self.cooldown_turns:
            reason = "cooldown"
        else:
            reason = "ready"
        return ContextCompactionDecision(
            should_compact=reason == "ready",
            reason=reason,
            latest_input_tokens=latest_input_tokens,
            context_window_tokens=context_window_tokens,
            warning_tokens=warning_tokens,
            threshold_tokens=threshold_tokens,
            target_tokens=target_tokens,
            retained_turns=retained_turns,
            turns_since_compaction=turns_since_compaction,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextCompactionDecision:
    """Auditable result of evaluating one context-pressure sample."""

    should_compact: bool
    reason: CompactionDecisionReason
    latest_input_tokens: int
    context_window_tokens: int
    warning_tokens: int
    threshold_tokens: int
    target_tokens: int
    retained_turns: int
    turns_since_compaction: int

    @property
    def percent(self) -> float:
        """Latest input pressure as a percentage of the configured window."""

        return min(100.0, self.latest_input_tokens * 100 / self.context_window_tokens)

    @property
    def threshold_percent(self) -> float:
        return self.threshold_tokens * 100 / self.context_window_tokens

    @property
    def warning_percent(self) -> float:
        return self.warning_tokens * 100 / self.context_window_tokens


def compaction_decision_request(
    decision: ContextCompactionDecision,
    *,
    scope_id: str = "context",
) -> DecisionRequest:
    """Project pressure into a bounded advisory decision request."""

    fallback = "compact" if decision.should_compact else "continue"
    return DecisionRequest(
        kind="compaction_action",
        scope_id=hashlib.sha256(scope_id.encode("utf-8")).hexdigest()[:16],
        options=(
            DecisionOption("compact", "Create a bounded checkpoint now."),
            DecisionOption("continue", "Keep the current context within policy."),
            DecisionOption("checkpoint", "Preserve a checkpoint before continuing."),
        ),
        fallback=fallback,
        context=(
            ("pressure_percent", round(decision.percent, 1)),
            ("threshold_percent", round(decision.threshold_percent, 1)),
            ("retained_turns", decision.retained_turns),
            ("turns_since_compaction", decision.turns_since_compaction),
        ),
    )

__all__ = [
    "CompactionDecisionReason",
    "ContextCompactionDecision",
    "ContextCompactionPolicy",
    "compaction_decision_request",
]
