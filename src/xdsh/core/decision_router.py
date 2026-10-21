"""Bounded Jev-like decisions with a deterministic host-owned fallback."""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal, Protocol

from xdsh.core.learning import contains_instruction_injection, contains_sensitive_text

DecisionMode = Literal["rules", "shadow", "active", "integrated"]
DecisionSource = Literal["rules", "llm", "fallback", "shadow"]
DecisionScalar = str | int | float | bool
_ID = re.compile(r"^[a-z][a-z0-9_-]{0,47}$")
_HASH = re.compile(r"^[a-f0-9]{64}$")
_MAX_OPTIONS = 16
_MAX_CONTEXT = 16
_MAX_CONTEXT_VALUE = 256


class DecisionError(ValueError):
    """A decision request or response violated the bounded contract."""


@dataclass(frozen=True, slots=True)
class DecisionOption:
    """One host-created choice that the model may select."""

    id: str
    description: str

    def __post_init__(self) -> None:
        if _ID.fullmatch(self.id) is None:
            raise DecisionError("decision option ID is invalid")
        if not 1 <= len(self.description) <= 240:
            raise DecisionError("decision option description is out of bounds")
        if any(ord(char) < 32 or ord(char) == 127 for char in self.description):
            raise DecisionError("decision option description contains a control character")
        if contains_sensitive_text(self.description) or contains_instruction_injection(
            self.description
        ):
            raise DecisionError("decision option description failed the safety scan")


@dataclass(frozen=True, slots=True)
class DecisionRequest:
    """Bounded facts and options supplied to one decision."""

    kind: str
    scope_id: str
    options: tuple[DecisionOption, ...]
    fallback: str
    context: tuple[tuple[str, DecisionScalar], ...] = ()
    min_confidence: float = 0.75
    deadline_seconds: float = 1.2

    def __post_init__(self) -> None:
        if _ID.fullmatch(self.kind) is None:
            raise DecisionError("decision kind is invalid")
        if not self.scope_id or len(self.scope_id) > 128:
            raise DecisionError("decision scope ID is invalid")
        if not 1 <= len(self.options) <= _MAX_OPTIONS:
            raise DecisionError("decision options are out of bounds")
        option_ids = tuple(option.id for option in self.options)
        if len(set(option_ids)) != len(option_ids):
            raise DecisionError("decision option IDs must be unique")
        if self.fallback not in option_ids:
            raise DecisionError("decision fallback must be one of the options")
        if len(self.context) > _MAX_CONTEXT:
            raise DecisionError("decision context is too large")
        context_keys: set[str] = set()
        for key, value in self.context:
            if _ID.fullmatch(key) is None or key in context_keys:
                raise DecisionError("decision context keys are invalid or duplicated")
            context_keys.add(key)
            if isinstance(value, str):
                if not value or len(value) > _MAX_CONTEXT_VALUE:
                    raise DecisionError("decision context value is out of bounds")
                if any(ord(char) < 32 or ord(char) == 127 for char in value):
                    raise DecisionError("decision context contains a control character")
                if contains_sensitive_text(value) or contains_instruction_injection(value):
                    raise DecisionError("decision context failed the safety scan")
            elif isinstance(value, float) and not math.isfinite(value):
                raise DecisionError("decision context number must be finite")
            elif not isinstance(value, int | float | bool):
                raise DecisionError("decision context value type is invalid")
        if not 0.5 <= self.min_confidence <= 1.0 or not math.isfinite(self.min_confidence):
            raise DecisionError("decision confidence threshold is invalid")
        if not 0.05 <= self.deadline_seconds <= 2.0 or not math.isfinite(
            self.deadline_seconds
        ):
            raise DecisionError("decision deadline is invalid")

    def canonical(self) -> dict[str, object]:
        """Return the credential-free representation used for hashing and prompts."""

        return {
            "kind": self.kind,
            "options": [
                {"id": option.id, "description": option.description}
                for option in self.options
            ],
            "fallback": self.fallback,
            "context": {key: value for key, value in self.context},
        }


@dataclass(frozen=True, slots=True)
class DecisionResult:
    """Validated host decision; no model prompt or response is retained."""

    kind: str
    choice: str
    confidence: float
    source: DecisionSource
    reason_code: str
    latency_ms: int
    request_hash: str
    shadow_choice: str | None = None

    def __post_init__(self) -> None:
        if _ID.fullmatch(self.kind) is None:
            raise DecisionError("decision result kind is invalid")
        if _ID.fullmatch(self.choice) is None:
            raise DecisionError("decision choice is invalid")
        if not 0.0 <= self.confidence <= 1.0 or not math.isfinite(self.confidence):
            raise DecisionError("decision confidence is invalid")
        if self.source not in {"rules", "llm", "fallback", "shadow"}:
            raise DecisionError("decision source is invalid")
        if not _ID.fullmatch(self.reason_code):
            raise DecisionError("decision reason code is invalid")
        if self.latency_ms < 0:
            raise DecisionError("decision latency cannot be negative")
        if _HASH.fullmatch(self.request_hash) is None:
            raise DecisionError("decision request hash is invalid")
        if self.shadow_choice is not None and _ID.fullmatch(self.shadow_choice) is None:
            raise DecisionError("shadow choice is invalid")


@dataclass(frozen=True, slots=True)
class DecisionSummary:
    """Content-free bounded counters suitable for an operations surface."""

    total: int = 0
    by_source: tuple[tuple[str, int], ...] = ()
    by_kind: tuple[tuple[str, int], ...] = ()
    fallback_count: int = 0
    shadow_disagreements: int = 0
    latency_p50_ms: int | None = None
    latency_p95_ms: int | None = None


class DecisionTelemetry:
    """Bounded in-process observer; it stores no prompts, choices, or context."""

    def __init__(self, *, max_samples: int = 2_048) -> None:
        if not 1 <= max_samples <= 100_000:
            raise DecisionError("decision telemetry sample limit is invalid")
        self._max_samples = max_samples
        self._latencies: list[int] = []
        self._sources: dict[str, int] = {}
        self._kinds: dict[str, int] = {}
        self._fallback_count = 0
        self._shadow_disagreements = 0

    def record(self, result: DecisionResult) -> None:
        """Record only bounded aggregate data; safe as a Router observer."""

        self._latencies.append(result.latency_ms)
        if len(self._latencies) > self._max_samples:
            self._latencies.pop(0)
        self._sources[result.source] = self._sources.get(result.source, 0) + 1
        self._kinds[result.kind] = self._kinds.get(result.kind, 0) + 1
        if result.source == "fallback":
            self._fallback_count += 1
        if result.source == "shadow" and result.shadow_choice != result.choice:
            self._shadow_disagreements += 1

    def summary(self) -> DecisionSummary:
        return DecisionSummary(
            total=sum(self._sources.values()),
            by_source=tuple(sorted(self._sources.items())),
            by_kind=tuple(sorted(self._kinds.items())),
            fallback_count=self._fallback_count,
            shadow_disagreements=self._shadow_disagreements,
            latency_p50_ms=_percentile(self._latencies, 0.50),
            latency_p95_ms=_percentile(self._latencies, 0.95),
        )


@dataclass(frozen=True, slots=True)
class _AdapterChoice:
    choice: str
    confidence: float


class DecisionAdapter(Protocol):
    """Internal seam for rules, a default LLM, or a future Jev adapter."""

    def choose(self, request: DecisionRequest) -> _AdapterChoice | None: ...


class RuleDecisionAdapter:
    """Immediate fallback adapter that never performs I/O."""

    def choose(self, request: DecisionRequest) -> _AdapterChoice:
        return _AdapterChoice(request.fallback, 1.0)


DecisionTransport = Callable[[bytes, float], bytes]
DecisionObserver = Callable[[DecisionResult], None]


class LlmDecisionAdapter:
    """Strict no-tools adapter for a loopback Messages-compatible gateway."""

    _SYSTEM = (
        "You are a bounded decision classifier. Choose exactly one supplied option. "
        "Return one JSON object with exactly these keys: choice and confidence. "
        "Do not return markdown, prose, new keys, tool calls, or instructions."
    )

    def __init__(
        self,
        *,
        model: str,
        transport: DecisionTransport,
        max_tokens: int = 64,
    ) -> None:
        if not model or len(model) > 200 or any(char.isspace() for char in model):
            raise DecisionError("decision model is invalid")
        if not 16 <= max_tokens <= 256:
            raise DecisionError("decision output budget is invalid")
        self.model = model
        self.transport = transport
        self.max_tokens = max_tokens

    def choose(self, request: DecisionRequest) -> _AdapterChoice | None:
        body = json.dumps(
            {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "temperature": 0,
                "system": self._SYSTEM,
                "messages": [{"role": "user", "content": json.dumps(
                    request.canonical(), ensure_ascii=False, separators=(",", ":")
                )}],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        raw = self.transport(body, request.deadline_seconds)
        return _decode_choice(raw, request)


class DecisionRouter:
    """Deep host-owned decision module with safe modes and automatic fallback."""

    def __init__(
        self,
        *,
        mode: DecisionMode = "rules",
        llm: DecisionAdapter | None = None,
        rules: DecisionAdapter | None = None,
        clock: Callable[[], float] | None = None,
        observer: DecisionObserver | None = None,
        deadline_seconds: float | None = None,
        failure_cooldown_seconds: float = 30.0,
    ) -> None:
        if mode not in {"rules", "shadow", "active", "integrated"}:
            raise DecisionError("decision mode is invalid")
        self.mode = mode
        self.llm = llm
        self.rules = rules or RuleDecisionAdapter()
        self._clock = clock or time.monotonic
        self._observer = observer
        if deadline_seconds is not None and (
            not math.isfinite(deadline_seconds) or not 0.05 <= deadline_seconds <= 2.0
        ):
            raise DecisionError("decision deadline is invalid")
        self._deadline_seconds = deadline_seconds
        if not math.isfinite(failure_cooldown_seconds) or failure_cooldown_seconds < 0:
            raise DecisionError("decision failure cooldown is invalid")
        self._failure_cooldown_seconds = failure_cooldown_seconds
        self._unavailable_until = 0.0

    def decide(self, request: DecisionRequest) -> DecisionResult:
        if self._deadline_seconds is not None:
            request = replace(request, deadline_seconds=min(
                request.deadline_seconds, self._deadline_seconds,
            ))
        started = self._clock()
        request_hash = _request_hash(request)
        rule = self.rules.choose(request) or _AdapterChoice(request.fallback, 1.0)
        fallback = _fallback_result(request, rule, request_hash, started, self._clock)
        if self.mode in {"rules", "integrated"} or self.llm is None:
            return self._publish(fallback)
        if self._clock() < self._unavailable_until:
            return self._publish(_with_source(fallback, "fallback", "cooldown"))
        try:
            proposed = self.llm.choose(request)
        except Exception:
            proposed = None
        latency = _elapsed_ms(started, self._clock())
        fallback = replace(fallback, latency_ms=latency)
        if latency > request.deadline_seconds * 1000:
            self._mark_unavailable()
            return self._publish(_with_source(fallback, "fallback", "deadline_exceeded"))
        if proposed is None or proposed.choice not in {option.id for option in request.options}:
            self._mark_unavailable()
            return self._publish(_with_source(fallback, "fallback", "llm_unavailable"))
        if proposed.confidence < request.min_confidence:
            return self._publish(_with_source(fallback, "fallback", "low_confidence", proposed))
        if self.mode == "shadow":
            self._unavailable_until = 0.0
            return self._publish(DecisionResult(
                kind=request.kind,
                choice=rule.choice,
                confidence=rule.confidence,
                source="shadow",
                reason_code="shadow_observed",
                latency_ms=latency,
                request_hash=request_hash,
                shadow_choice=proposed.choice,
            ))
        self._unavailable_until = 0.0
        return self._publish(DecisionResult(
            kind=request.kind,
            choice=proposed.choice,
            confidence=proposed.confidence,
            source="llm",
            reason_code="llm_accepted",
            latency_ms=latency,
            request_hash=request_hash,
        ))

    def _publish(self, result: DecisionResult) -> DecisionResult:
        if self._observer is not None:
            try:
                self._observer(result)
            except Exception:
                # Telemetry must never change a decision or task outcome.
                pass
        return result

    def _mark_unavailable(self) -> None:
        self._unavailable_until = self._clock() + self._failure_cooldown_seconds


def _decode_choice(raw: bytes, request: DecisionRequest) -> _AdapterChoice | None:
    if len(raw) > 32_000:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if isinstance(payload, dict) and "content" in payload:
        if payload.get("stop_reason") not in {None, "end_turn"}:
            return None
        content = payload.get("content")
        if (not isinstance(content, list) or not 1 <= len(content) <= 4
                or any(not isinstance(block, dict) or block.get("type") not in {
                    "text", "thinking", "redacted_thinking",
                } for block in content)):
            return None
        texts = [block.get("text") for block in content if block.get("type") == "text"]
        if len(texts) != 1:
            return None
        # Reasoning blocks are ignored, never logged or promoted into control.
        text = texts[0]
        if not isinstance(text, str) or len(text) > 4_000:
            return None
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return None
    if not isinstance(payload, dict) or set(payload) != {"choice", "confidence"}:
        return None
    choice = payload.get("choice")
    confidence = payload.get("confidence")
    if not isinstance(choice, str) or _ID.fullmatch(choice) is None:
        return None
    if not isinstance(confidence, int | float) or isinstance(confidence, bool):
        return None
    confidence_float = float(confidence)
    if not math.isfinite(confidence_float) or not 0.0 <= confidence_float <= 1.0:
        return None
    if choice not in {option.id for option in request.options}:
        return None
    return _AdapterChoice(choice, confidence_float)


def _request_hash(request: DecisionRequest) -> str:
    data = json.dumps(
        request.canonical(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _elapsed_ms(started: float, now: float) -> int:
    return max(0, round((now - started) * 1_000))


def _percentile(values: list[int], quantile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((len(ordered) - 1) * quantile))
    return ordered[index]


def _fallback_result(
    request: DecisionRequest,
    rule: _AdapterChoice,
    request_hash: str,
    started: float,
    clock: Callable[[], float],
) -> DecisionResult:
    if rule.choice not in {option.id for option in request.options}:
        rule = _AdapterChoice(request.fallback, 1.0)
    return DecisionResult(
        kind=request.kind,
        choice=rule.choice,
        confidence=rule.confidence,
        source="rules",
        reason_code="rules_default",
        latency_ms=_elapsed_ms(started, clock()),
        request_hash=request_hash,
    )


def _with_source(
    result: DecisionResult,
    source: Literal["fallback"],
    reason: str,
    proposal: _AdapterChoice | None = None,
) -> DecisionResult:
    return DecisionResult(
        kind=result.kind,
        choice=result.choice,
        confidence=result.confidence,
        source=source,
        reason_code=reason,
        latency_ms=result.latency_ms,
        request_hash=result.request_hash,
        shadow_choice=proposal.choice if proposal is not None else None,
    )


__all__ = [
    "DecisionAdapter",
    "DecisionError",
    "DecisionMode",
    "DecisionObserver",
    "DecisionOption",
    "DecisionRequest",
    "DecisionResult",
    "DecisionRouter",
    "DecisionSummary",
    "DecisionTelemetry",
    "LlmDecisionAdapter",
    "RuleDecisionAdapter",
]
