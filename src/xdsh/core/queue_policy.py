"""Runner-neutral analysis for a message received while a chat is busy.

The decision is deliberately local and bounded.  It does not call an LLM (a
second model request while the first one is running would be surprising and
could itself deadlock the queue), and it never changes queue state.  The
transport presents the recommendation together with explicit actions so the
user remains the authority over interruption and follow-up semantics.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Literal

from xdsh.core.decision_router import DecisionOption, DecisionRequest

QueueIntent = Literal["supplement", "urgent", "replacement", "independent", "unclear"]
QueueRecommendation = Literal["merge", "interrupt", "replace", "queue"]

_MAX_ANALYSIS_CHARS = 4_000
_TOKEN = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)

# These are intentionally small, high-signal groups.  They are a hint for the
# UI, not an authorization mechanism and not a claim about model intent.
_SUPPLEMENT_MARKERS = frozenset(
    {
        "補充",
        "补充",
        "補上",
        "补上",
        "繼續",
        "继续",
        "接著",
        "接着",
        "剛才",
        "刚才",
        "上一個",
        "上一个",
        "之前",
        "上面",
        "另外",
        "追加",
        "followup",
        "follow-up",
        "follow",
        "continue",
        "clarify",
        "補充說明",
        "补充说明",
    }
)
_URGENT_MARKERS = frozenset(
    {
        "緊急",
        "紧急",
        "立即",
        "馬上",
        "马上",
        "優先",
        "优先",
        "插隊",
        "插队",
        "阻塞",
        "阻塞了",
        "urgent",
        "asap",
        "blocker",
        "immediately",
        "first",
    }
)
_REPLACEMENT_MARKERS = frozenset(
    {
        "取消剛才",
        "取消刚才",
        "不要做",
        "不用做",
        "改成",
        "換成",
        "换成",
        "取代",
        "替代",
        "instead",
        "replace",
        "cancel",
        "stop",
    }
)


@dataclass(frozen=True, slots=True)
class QueueAnalysis:
    """A bounded, explainable recommendation for one busy-chat message."""

    intent: QueueIntent
    recommendation: QueueRecommendation
    confidence: Literal["low", "medium", "high"]
    reason: str
    signals: tuple[str, ...] = ()


def queue_decision_request(
    analysis: QueueAnalysis,
    *,
    scope_id: str = "queue",
) -> DecisionRequest:
    """Project a queue recommendation without allowing model-side mutation."""

    return DecisionRequest(
        kind="queue_action",
        scope_id=hashlib.sha256(scope_id.encode("utf-8")).hexdigest()[:16],
        options=(
            DecisionOption("queue", "Keep FIFO order and run after the active task."),
            DecisionOption("merge", "Combine this message with the active task."),
            DecisionOption("interrupt", "Run this first, then resume the active task."),
            DecisionOption("replace", "Replace the active task and discard its continuation."),
        ),
        fallback=analysis.recommendation,
        context=(
            ("intent", analysis.intent),
            ("confidence", analysis.confidence),
            ("signal_count", len(analysis.signals)),
        ),
    )


def analyze_busy_prompt(active_prompt: str, incoming_prompt: str) -> QueueAnalysis:
    """Classify a busy-chat message without invoking the runner.

    The active prompt is used only for a bounded token-overlap signal.  The
    returned text is safe for a UI card and never contains either full prompt.
    Ambiguous input defaults to ordinary FIFO queueing; a heuristic can
    recommend an action but cannot perform it.
    """

    active = active_prompt[:_MAX_ANALYSIS_CHARS]
    incoming = incoming_prompt[:_MAX_ANALYSIS_CHARS]
    incoming_tokens = _tokens(incoming)
    active_tokens = _tokens(active)
    supplement = _marker_hits(incoming, _SUPPLEMENT_MARKERS)
    urgent = _marker_hits(incoming, _URGENT_MARKERS)
    replacement = _marker_hits(incoming, _REPLACEMENT_MARKERS)
    overlap = _overlap_ratio(active_tokens, incoming_tokens)

    if replacement:
        return QueueAnalysis(
            intent="replacement",
            recommendation="replace",
            confidence="high" if len(replacement) >= 2 else "medium",
            reason="检测到改写或取消当前任务的措辞。",
            signals=tuple(replacement[:3]),
        )
    if urgent:
        return QueueAnalysis(
            intent="urgent",
            recommendation="interrupt",
            confidence="high" if len(urgent) >= 2 else "medium",
            reason="检测到需要优先处理的措辞; 可先执行新任务, 再续接当前任务。",
            signals=tuple(urgent[:3]),
        )
    if supplement or overlap >= 0.34:
        signals = list(supplement[:3])
        if overlap >= 0.34:
            signals.append(f"上下文词汇重叠 {overlap:.0%}")
        return QueueAnalysis(
            intent="supplement",
            recommendation="merge",
            confidence="high" if supplement and overlap >= 0.34 else "medium",
            reason="消息看起来像当前任务的补充; 建议保存进度并立即合并续跑, 不等待原任务完成。",
            signals=tuple(signals),
        )
    if incoming_tokens:
        return QueueAnalysis(
            intent="independent",
            recommendation="queue",
            confidence="medium",
            reason="未发现明确的补充、替代或紧急信号; 建议保持 FIFO 排队。",
        )
    return QueueAnalysis(
        intent="unclear",
        recommendation="queue",
        confidence="low",
        reason="消息过短或无法判断关系; 建议保持 FIFO 排队。",
    )


def _tokens(text: str) -> frozenset[str]:
    return frozenset(match.group(0).casefold() for match in _TOKEN.finditer(text))


def _marker_hits(text: str, markers: frozenset[str]) -> tuple[str, ...]:
    folded = text.casefold()
    return tuple(sorted(marker for marker in markers if _contains_marker(folded, marker)))


def _contains_marker(folded_text: str, marker: str) -> bool:
    normalized = marker.casefold()
    # Chinese markers are intentionally substring-based because punctuation
    # and inflection are not word-delimited.  Latin markers use boundaries so
    # an unrelated word such as ``firsthand`` cannot trigger an interruption.
    if normalized.isascii():
        return re.search(rf"(?<!\w){re.escape(normalized)}(?!\w)", folded_text) is not None
    return normalized in folded_text


def _overlap_ratio(active: frozenset[str], incoming: frozenset[str]) -> float:
    if not active or not incoming:
        return 0.0
    return len(active.intersection(incoming)) / max(1, len(incoming))
