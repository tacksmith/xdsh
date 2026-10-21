"""Runner-neutral protection against upstream HTTP 429 storms.

The DSH runtime owns its provider request loop and may emit several retry
notifications before returning a terminal error.  XDSH cannot safely replay a
turn after a tool may have changed the workspace, so this module deliberately
does two smaller, safer things:

* gate new turns with a process-wide concurrency cap and per-provider/model
  pacing/cooldown;
* stop a retry storm after a small, configurable number of *structured* 429
  notifications while retaining the events already produced by the runner.

No provider response body or stdout is parsed as an event.  The classifier is
used only for typed error metadata and the short error fields already exposed
by the runner-neutral event model.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import random
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import cast

from xdsh.core.decision_router import DecisionOption, DecisionRequest
from xdsh.core.events import (
    ErrorEvent,
    FinishEvent,
    RetryEvent,
    RunnerEventT,
    TextEvent,
    ToolStartEvent,
)
from xdsh.core.runner import (
    AgentRunner,
    CapabilityAwareRunner,
    ReasoningEffort,
    RunnerCapabilities,
    close_event_stream,
)

Clock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]

_ROUTE_PART = re.compile(r"[^A-Za-z0-9._:/+-]+")
_RATE_LIMIT_CODE = re.compile(r"(?:429|rate[ _-]?limit|too[ _-]+many[ _-]+requests|throttl)", re.I)
_RETRY_AFTER_KEYS = frozenset(
    {
        "retry-after",
        "Retry-After",
        "retry_after",
        "retryAfter",
        "retry_after_seconds",
        "retryAfterSeconds",
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class RateLimitPolicy:
    """Bounded route pacing and in-run 429 handling.

    ``max_rate_limit_retries`` permits that many structured 429 retries per
    consecutive failure episode, not per entire long task. Completed model
    output resets the episode; activity/partial chunks do not. No task replay.
    """

    # Two slots keep one foreground turn responsive while a bounded background
    # job is active; operators can set this to one for especially strict routes.
    max_concurrent_requests: int = 2
    min_interval_seconds: float = 0.25
    # 5+10+20+40+80 = 155s before jitter: give minute-scale quotas time to
    # recover without switching to unbounded retries or replaying the task.
    max_rate_limit_retries: int = 5
    base_backoff_seconds: float = 5.0
    max_backoff_seconds: float = 120.0
    max_retry_after_seconds: float = 120.0
    acquire_timeout_seconds: float = 120.0
    jitter_ratio: float = 0.10
    # A 429 usually applies to the whole provider account/route.  Switching
    # models immediately can therefore amplify the storm; fallback is opt-in
    # and remains available for deployments with genuinely independent routes.
    allow_fallback_on_rate_limit: bool = False

    def __post_init__(self) -> None:
        if not 1 <= self.max_concurrent_requests <= 64:
            raise ValueError("max_concurrent_requests must be between 1 and 64")
        if not 0.0 <= self.min_interval_seconds <= 60.0 or not math.isfinite(
            self.min_interval_seconds
        ):
            raise ValueError("min_interval_seconds must be finite and between 0 and 60")
        if not 1 <= self.max_rate_limit_retries <= 10:
            raise ValueError("max_rate_limit_retries must be between 1 and 10")
        for name, value in (
            ("base_backoff_seconds", self.base_backoff_seconds),
            ("max_backoff_seconds", self.max_backoff_seconds),
            ("max_retry_after_seconds", self.max_retry_after_seconds),
            ("acquire_timeout_seconds", self.acquire_timeout_seconds),
        ):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if self.base_backoff_seconds > self.max_backoff_seconds:
            raise ValueError("base_backoff_seconds cannot exceed max_backoff_seconds")
        if self.max_backoff_seconds > self.max_retry_after_seconds:
            raise ValueError("max_backoff_seconds cannot exceed max_retry_after_seconds")
        if not 0.0 <= self.jitter_ratio <= 1.0 or not math.isfinite(self.jitter_ratio):
            raise ValueError("jitter_ratio must be between 0 and 1")
        if not isinstance(self.allow_fallback_on_rate_limit, bool):
            raise ValueError("allow_fallback_on_rate_limit must be a boolean")

    @property
    def summary(self) -> str:
        """Return a content-free operator summary for status/doctor views."""

        fallback = "+fallback" if self.allow_fallback_on_rate_limit else ""
        return (
            f"{self.max_concurrent_requests}inflight/"
            f"{_format_milliseconds(self.min_interval_seconds)}ms/"
            f"{self.max_rate_limit_retries}retries/"
            f"{_format_seconds(self.base_backoff_seconds)}-"
            f"{_format_seconds(self.max_backoff_seconds)}s-backoff/"
            f"cap{_format_seconds(self.max_retry_after_seconds)}s/"
            f"{_format_seconds(self.acquire_timeout_seconds)}s-queue{fallback}"
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class RateLimitDetails:
    """Bounded metadata extracted from a typed upstream failure."""

    status_code: int = 429
    retry_after_seconds: float | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.status_code, bool)
            or not 100 <= self.status_code <= 599
        ):
            raise ValueError("status_code must be between 100 and 599")
        if self.retry_after_seconds is not None and (
            self.retry_after_seconds < 0
            or not math.isfinite(self.retry_after_seconds)
        ):
            raise ValueError("retry_after_seconds must be finite and non-negative")


class RateLimitQueueTimeout(TimeoutError):
    """A task waited too long for the route gate and was not started."""


class RateLimitGate:
    """Async route gate shared by all turns in one XDSH process."""

    def __init__(
        self,
        policy: RateLimitPolicy | None = None,
        *,
        clock: Clock | None = None,
        sleep: Sleeper | None = None,
        random_value: Callable[[], float] | None = None,
    ) -> None:
        self.policy = policy or RateLimitPolicy()
        self._clock: Clock = clock or time.monotonic
        self._sleep: Sleeper = sleep or asyncio.sleep
        self._random: Callable[[], float] = random_value or random.random
        self._semaphore: asyncio.Semaphore | None = None
        self._state_lock: asyncio.Lock | None = None
        self._next_allowed: dict[str, float] = {}
        self._cooldown_until: dict[str, float] = {}
        self._failures: dict[str, int] = {}

    async def acquire(self, route: str) -> RateLimitLease:
        """Wait for an available route slot, or fail within a hard deadline."""

        normalized = normalize_route(route)
        semaphore, state_lock = self._loop_state()
        deadline = self._clock() + self.policy.acquire_timeout_seconds
        try:
            remaining = max(0.0, deadline - self._clock())
            if remaining <= 0.0:
                raise asyncio.TimeoutError
            # wait_for in Python 3.10 can swallow external cancellation when
            # acquisition finishes in the same loop turn (CPython #86296).
            # Own the acquisition task so cancellation always wins and a
            # concurrently granted slot is returned before we propagate it.
            acquisition = asyncio.create_task(semaphore.acquire())
            try:
                done, _ = await asyncio.wait({acquisition}, timeout=remaining)
                if not done:
                    raise asyncio.TimeoutError
                acquisition.result()
            except BaseException:
                acquisition.cancel()
                try:
                    await asyncio.gather(acquisition, return_exceptions=True)
                finally:
                    if (
                        acquisition.done()
                        and not acquisition.cancelled()
                        and acquisition.exception() is None
                        and acquisition.result()
                    ):
                        semaphore.release()
                raise
        except asyncio.TimeoutError as error:
            raise RateLimitQueueTimeout(
                "upstream route is busy or cooling down; task was not started"
            ) from error

        try:
            while True:
                async with state_lock:
                    now = self._clock()
                    blocked_until = max(
                        self._next_allowed.get(normalized, 0.0),
                        self._cooldown_until.get(normalized, 0.0),
                    )
                    wait_seconds = max(0.0, blocked_until - now)
                    if wait_seconds <= 0.0:
                        self._next_allowed[normalized] = now + self.policy.min_interval_seconds
                        return RateLimitLease(self, normalized)
                try:
                    remaining = max(0.0, deadline - self._clock())
                    if remaining <= 0.0:
                        raise asyncio.TimeoutError
                    # Do not wrap a sleep in a timeout with the exact same
                    # duration.  The event loop can resume the timeout task
                    # one tick before the sleeper (especially for the
                    # default 250ms pacing), turning a healthy second turn
                    # into a false queue timeout.  If the cooldown itself
                    # would cross the hard deadline, sleep only until that
                    # deadline and report a real timeout; otherwise let the
                    # pacing sleep complete and re-evaluate the deadline.
                    if wait_seconds >= remaining:
                        await self._sleep(remaining)
                        raise asyncio.TimeoutError
                    await self._sleep(wait_seconds)
                except asyncio.TimeoutError as error:
                    raise RateLimitQueueTimeout(
                        "upstream route cooldown exceeded the queue deadline"
                    ) from error
        except BaseException:
            semaphore.release()
            raise

    def note_rate_limit(
        self,
        route: str,
        retry_after_seconds: float | None = None,
    ) -> float:
        """Record a 429 and return the bounded cooldown selected for the route."""

        normalized = normalize_route(route)
        failures = self._failures.get(normalized, 0) + 1
        self._failures[normalized] = failures
        bounded_retry_after = parse_retry_after(retry_after_seconds)
        if bounded_retry_after is None:
            exponential = min(
                self.policy.max_backoff_seconds,
                self.policy.base_backoff_seconds * (2 ** min(failures - 1, 10)),
            )
            jitter = exponential * self.policy.jitter_ratio * self._random()
            delay: float = float(
                min(self.policy.max_retry_after_seconds, exponential + jitter)
            )
        else:
            delay = float(min(
                self.policy.max_retry_after_seconds,
                max(0.0, bounded_retry_after),
            ))
        now = self._clock()
        self._cooldown_until[normalized] = max(
            self._cooldown_until.get(normalized, 0.0),
            now + delay,
        )
        return delay

    def note_success(self, route: str) -> None:
        """Clear a route's failure streak after a successful turn."""

        normalized = normalize_route(route)
        self._failures.pop(normalized, None)
        self._cooldown_until.pop(normalized, None)

    def snapshot(self, route: str | None = None) -> dict[str, float | int]:
        """Return content-free route state suitable for a health card."""

        normalized = normalize_route(route) if route is not None else None
        if normalized is None:
            return {
                "routes": len(set(self._failures) | set(self._cooldown_until)),
                "cooling_routes": sum(
                    value > self._clock() for value in self._cooldown_until.values()
                ),
                "failure_events": sum(self._failures.values()),
            }
        return {
            "failures": self._failures.get(normalized, 0),
            "cooldown_remaining_seconds": max(
                0.0,
                self._cooldown_until.get(normalized, 0.0) - self._clock(),
            ),
        }

    def _loop_state(self) -> tuple[asyncio.Semaphore, asyncio.Lock]:
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.policy.max_concurrent_requests)
            self._state_lock = asyncio.Lock()
        assert self._state_lock is not None
        return self._semaphore, self._state_lock

    def _release(self) -> None:
        if self._semaphore is not None:
            self._semaphore.release()


@dataclass(slots=True)
class RateLimitLease:
    gate: RateLimitGate
    route: str
    _released: bool = False

    def release(self) -> None:
        if not self._released:
            self._released = True
            self.gate._release()


class RateLimitedRunner:
    """Apply the route gate and stop structured 429 retry storms."""

    def __init__(
        self,
        runner: AgentRunner,
        *,
        provider: str = "unknown",
        policy: RateLimitPolicy | None = None,
        gate: RateLimitGate | None = None,
    ) -> None:
        self._runner = runner
        self.provider = _bounded_route_part(provider, fallback="unknown")
        self.policy = policy or RateLimitPolicy()
        self.gate = gate or RateLimitGate(self.policy)

    @property
    def rate_limit_protected(self) -> bool:
        """Marker used by outer transports to avoid duplicate gates."""

        return True

    @property
    def allow_fallback_on_rate_limit(self) -> bool:
        """Whether the fallback layer may immediately switch after a 429."""

        return self.policy.allow_fallback_on_rate_limit

    def snapshot(self, model: str | None = None) -> dict[str, float | int]:
        """Expose bounded route state for health surfaces without task content."""

        route = normalize_route(f"{self.provider}/{model or 'default'}")
        return self.gate.snapshot(route)

    @property
    def capabilities(self) -> RunnerCapabilities:
        if isinstance(self._runner, CapabilityAwareRunner):
            return self._runner.capabilities
        return RunnerCapabilities(
            protocol="unknown",
            runtime_package="unknown",
            runtime_identity="unknown",
            native=frozenset(),
            unsupported_native=frozenset(),
            source="unknown_adapter",
        )

    async def run(
        self,
        task: str,
        *,
        session_id: str,
        cwd: Path,
        model: str | None = None,
        reasoning_effort: ReasoningEffort = "off",
        fallback: str | None = None,
        system_prompt: str | None = None,
        resume_context: str | None = None,
    ) -> AsyncIterator[RunnerEventT]:
        route = normalize_route(f"{self.provider}/{model or 'default'}")
        try:
            lease = await self.gate.acquire(route)
        except RateLimitQueueTimeout:
            yield ErrorEvent(
                session_id=session_id,
                message="上游模型忙碌或处于限流冷却, 任务未启动; 请稍后重试。",
                code="RATE_LIMIT_QUEUE_TIMEOUT",
                retryable=True,
                status_code=429,
            )
            yield FinishEvent(session_id=session_id, reason="rate_limited")
            return

        rate_events = 0
        rate_limit_seen = False
        saw_finish = False
        finished_successfully = False
        stream: AsyncIterator[RunnerEventT] | None = None
        try:
            stream = self._runner.run(
                task, session_id=session_id, cwd=cwd, model=model,
                reasoning_effort=reasoning_effort, fallback=fallback,
                system_prompt=system_prompt, resume_context=resume_context,
            )
            try:
                async for event in stream:
                    if isinstance(event, RetryEvent) and is_rate_limit_retry(event):
                        rate_events += 1
                        rate_limit_seen = True
                        self.gate.note_rate_limit(route, event.retry_after_seconds)
                        if rate_events > self.policy.max_rate_limit_retries:
                            await self._stop_underlying(session_id)
                            yield ErrorEvent(
                                session_id=session_id,
                                message=(
                                    "上游返回 HTTP 429; 已达到 XDSH 有界限流重试上限, "
                                    "保留当前部分输出且不自动重放任务。"
                                ),
                                code="RATE_LIMIT_EXHAUSTED",
                                retryable=True,
                                status_code=429,
                                retry_after_seconds=event.retry_after_seconds,
                            )
                            yield FinishEvent(session_id=session_id, reason="rate_limited")
                            return
                    elif (
                        isinstance(event, ToolStartEvent)
                        or (isinstance(event, TextEvent) and not event.delta and bool(event.text))
                    ):
                        # A complete tool call/message proves the model advanced.
                        # Heartbeats, reasoning, usage and partial text cannot
                        # forgive a retry storm (usage can arrive midstream);
                        # unrelated tool completions cannot either.
                        if rate_limit_seen:
                            self.gate.note_success(route)
                        rate_events = 0
                        rate_limit_seen = False
                    elif isinstance(event, ErrorEvent):
                        details = classify_rate_limit(
                            code=event.code,
                            message=event.message,
                            status_code=event.status_code,
                            retry_after=event.retry_after_seconds,
                        )
                        if details is not None:
                            rate_limit_seen = True
                            self.gate.note_rate_limit(route, details.retry_after_seconds)
                            if (
                                event.status_code != details.status_code
                                or event.retry_after_seconds != details.retry_after_seconds
                                or not event.retryable
                            ):
                                event = ErrorEvent(
                                    session_id=event.session_id,
                                    sequence=event.sequence,
                                    timestamp_ms=event.timestamp_ms,
                                    message=event.message,
                                    code=(
                                        "RATE_LIMIT"
                                        if event.code.casefold() in {"unknown", "dsh_turn_error"}
                                        else event.code
                                    ),
                                    retryable=True,
                                    status_code=details.status_code,
                                    retry_after_seconds=details.retry_after_seconds,
                                )
                        elif event.code.casefold() not in {"unknown", "dsh_turn_error"}:
                            # A later typed TIMEOUT/AUTH/etc. is not evidence
                            # of another 429. Keep route cooldown, but do not
                            # relabel its terminal reason using an older retry.
                            rate_limit_seen = False
                    elif isinstance(event, FinishEvent):
                        # A few DSH versions report the provider failure in a
                        # preceding structured event but leave the terminal
                        # reason as generic ``error``. Normalize that one
                        # terminal edge so fallback, metrics, and transports
                        # all agree that the turn is rate-limited.
                        if (
                            rate_limit_seen
                            and not event.cancelled
                            and event.reason.casefold() in {"error", "failed", "unknown"}
                        ):
                            event = FinishEvent(
                                session_id=event.session_id,
                                sequence=event.sequence,
                                timestamp_ms=event.timestamp_ms,
                                reason="rate_limited",
                                final_text=event.final_text,
                                cancelled=event.cancelled,
                            )
                        saw_finish = True
                        finished_successfully = _finish_succeeded(event)
                    yield event
                if rate_limit_seen and not saw_finish:
                    # A runtime can close its stream immediately after a
                    # retry notification (some preview SDKs do this on
                    # transport abort). Do not let an absent finish turn into
                    # an implicit fallback replay; close the turn with the
                    # same bounded, partial-preserving outcome.
                    yield ErrorEvent(
                        session_id=session_id,
                        message=(
                            "上游返回 HTTP 429; 重试流在达到完成事件前结束, "
                            "保留当前部分输出且不自动重放任务。"
                        ),
                        code="RATE_LIMIT_EXHAUSTED",
                        retryable=True,
                        status_code=429,
                    )
                    yield FinishEvent(session_id=session_id, reason="rate_limited")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                details = classify_exception(error)
                if details is None:
                    if rate_limit_seen:
                        yield ErrorEvent(
                            session_id=session_id,
                            message=(
                                "上游返回 HTTP 429 后运行时异常结束; 保留当前部分输出, "
                                "且不自动重放任务。"
                            ),
                            code="RATE_LIMIT_EXHAUSTED",
                            retryable=True,
                            status_code=429,
                        )
                        yield FinishEvent(session_id=session_id, reason="rate_limited")
                        return
                    raise
                self.gate.note_rate_limit(route, details.retry_after_seconds)
                yield ErrorEvent(
                    session_id=session_id,
                    message="上游模型返回 HTTP 429 Too Many Requests。",
                    code="RATE_LIMIT",
                    retryable=True,
                    status_code=details.status_code,
                    retry_after_seconds=details.retry_after_seconds,
                )
                yield FinishEvent(session_id=session_id, reason="rate_limited")
                return
        finally:
            try:
                if stream is not None:
                    await close_event_stream(stream)
            finally:
                if finished_successfully:
                    self.gate.note_success(route)
                lease.release()

    async def cancel(self, session_id: str) -> bool:
        return await self._runner.cancel(session_id)

    async def runtime_generation(self, session_id: str) -> str | None:
        """Forward optional XDSH host-generation provenance."""

        method = getattr(self._runner, "runtime_generation", None)
        if method is None:
            return None
        return cast(str | None, await method(session_id))

    async def reset(self, session_id: str) -> None:
        await self._runner.reset(session_id)

    async def close(self) -> None:
        await self._runner.close()

    async def _stop_underlying(self, session_id: str) -> None:
        try:
            await self._runner.cancel(session_id)
        except Exception:
            # The terminal rate-limit event is still authoritative; cancellation
            # is best effort because a runtime may already have exited.
            return


def normalize_route(route: str) -> str:
    """Return a short route key that cannot contain credentials or newlines."""

    if not isinstance(route, str):
        return "unknown/default"
    pieces = [_bounded_route_part(part, fallback="unknown") for part in route.split("/", 1)]
    normalized = "/".join(pieces)
    return normalized[:240] or "unknown/default"


def classify_rate_limit(
    *,
    code: str | None = None,
    message: str | None = None,
    status_code: int | None = None,
    retry_after: object | None = None,
    now_wall: datetime | None = None,
) -> RateLimitDetails | None:
    """Classify a typed error without depending on provider-specific classes."""

    normalized_code = code.casefold() if isinstance(code, str) else ""
    normalized_message = message.casefold() if isinstance(message, str) else ""
    if status_code == 429 or _RATE_LIMIT_CODE.search(normalized_code) or _RATE_LIMIT_CODE.search(
        normalized_message
    ):
        parsed = parse_retry_after(retry_after, now_wall=now_wall)
        effective_status = (
            status_code
            if isinstance(status_code, int)
            and not isinstance(status_code, bool)
            and 100 <= status_code <= 599
            else 429
        )
        return RateLimitDetails(
            status_code=effective_status,
            retry_after_seconds=parsed,
        )
    return None


def classify_exception(error: BaseException) -> RateLimitDetails | None:
    """Classify an SDK/provider exception using typed status/header fields."""

    return classify_rate_limit(
        code=type(error).__name__,
        message=str(error),
        status_code=_status_from_exception(error),
        retry_after=_retry_after_from_exception(error),
    )


def parse_retry_after(value: object, *, now_wall: datetime | None = None) -> float | None:
    """Parse seconds or an HTTP-date ``Retry-After`` value."""

    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        numeric_value = float(value)
        return (
            numeric_value
            if math.isfinite(numeric_value) and numeric_value >= 0
            else None
        )
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    parsed_number: float | None
    try:
        parsed_number = float(text)
    except ValueError:
        parsed_number = None
    if parsed_number is not None:
        return (
            parsed_number
            if math.isfinite(parsed_number) and parsed_number >= 0
            else None
        )
    try:
        target = parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    origin = now_wall or datetime.now(timezone.utc)
    if origin.tzinfo is None:
        origin = origin.replace(tzinfo=timezone.utc)
    return max(0.0, (target - origin).total_seconds())


def rate_limit_policy_from_env(environment: Mapping[str, str]) -> RateLimitPolicy:
    """Build the shared policy from XDSH environment variables.

    This helper intentionally reads only numeric policy knobs; credentials and
    endpoint values never enter the rate-limit object.
    """

    def integer(name: str, default: int) -> int:
        raw = environment.get(name, str(default))
        try:
            return int(raw)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{name} must be an integer") from error

    def number(name: str, default: float) -> float:
        raw = environment.get(name, str(default))
        try:
            return float(raw)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{name} must be a number") from error

    def boolean(name: str, default: bool) -> bool:
        raw = environment.get(name, "1" if default else "0").strip().casefold()
        if raw in {"1", "true", "yes", "on"}:
            return True
        if raw in {"0", "false", "no", "off", ""}:
            return False
        raise ValueError(f"{name} must be a boolean")

    defaults = RateLimitPolicy()
    return RateLimitPolicy(
        max_concurrent_requests=integer(
            "XDSH_RATE_LIMIT_MAX_CONCURRENT_REQUESTS", defaults.max_concurrent_requests,
        ),
        min_interval_seconds=number(
            "XDSH_RATE_LIMIT_MIN_INTERVAL_SECONDS", defaults.min_interval_seconds,
        ),
        max_rate_limit_retries=integer(
            "XDSH_RATE_LIMIT_MAX_RETRIES", defaults.max_rate_limit_retries,
        ),
        base_backoff_seconds=number(
            "XDSH_RATE_LIMIT_BASE_BACKOFF_SECONDS", defaults.base_backoff_seconds,
        ),
        max_backoff_seconds=number(
            "XDSH_RATE_LIMIT_MAX_BACKOFF_SECONDS", defaults.max_backoff_seconds,
        ),
        max_retry_after_seconds=number(
            "XDSH_RATE_LIMIT_MAX_RETRY_AFTER_SECONDS", defaults.max_retry_after_seconds,
        ),
        acquire_timeout_seconds=number(
            "XDSH_RATE_LIMIT_QUEUE_TIMEOUT_SECONDS", defaults.acquire_timeout_seconds,
        ),
        jitter_ratio=number("XDSH_RATE_LIMIT_JITTER_RATIO", defaults.jitter_ratio),
        allow_fallback_on_rate_limit=boolean(
            "XDSH_RATE_LIMIT_ALLOW_FALLBACK",
            defaults.allow_fallback_on_rate_limit,
        ),
    )


def is_rate_limit_retry(event: RetryEvent) -> bool:
    """Return true only for a retry carrying 429/rate-limit evidence."""

    return bool(
        event.rate_limited
        or event.status_code == 429
        or (isinstance(event.provider, str) and _RATE_LIMIT_CODE.search(event.provider))
    )


def _finish_succeeded(event: FinishEvent) -> bool:
    return (
        not event.cancelled
        and event.reason.casefold() in {"completed", "complete", "success", "succeeded"}
    )


def _status_from_exception(error: BaseException) -> int | None:
    for name in ("status_code", "status", "http_status", "http_status_code"):
        value = cast(object, getattr(error, name, None))
        if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599:
            return value
    response = getattr(error, "response", None)
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) and 100 <= value <= 599 else None


def _retry_after_from_exception(error: BaseException) -> object | None:
    for name in ("retry_after", "retry_after_seconds"):
        value = cast(object, getattr(error, name, None))
        if value is not None:
            return value
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if isinstance(headers, Mapping):
        for key in _RETRY_AFTER_KEYS:
            if key in headers:
                return cast(object, headers[key])
        for key, value in headers.items():
            if isinstance(key, str) and key.casefold() == "retry-after":
                return cast(object, value)
    return None


def retry_decision_request(
    *,
    retryable: bool,
    attempts: int,
    max_attempts: int,
    route: str,
    scope_id: str = "retry",
) -> DecisionRequest:
    """Project retry pressure for optional observation; ceilings stay local."""

    if attempts < 0 or max_attempts <= 0 or attempts > max_attempts:
        raise ValueError("retry attempt counts are invalid")
    normalized = normalize_route(route)
    fallback = "retry" if retryable and attempts < max_attempts else "report"
    return DecisionRequest(
        kind="retry_action",
        scope_id=hashlib.sha256(scope_id.encode("utf-8")).hexdigest()[:16],
        options=(
            DecisionOption("retry", "Retry once within the host ceiling."),
            DecisionOption("alternate", "Use another already-authorized route."),
            DecisionOption("report", "Preserve the partial result and report the gap."),
        ),
        fallback=fallback,
        context=(
            ("retryable", retryable),
            ("attempts", attempts),
            ("max_attempts", max_attempts),
            ("route", normalized),
        ),
    )


def _bounded_route_part(value: object, *, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    normalized = _ROUTE_PART.sub("_", value.strip())
    return normalized[:120] or fallback


def _format_seconds(value: float) -> str:
    """Keep status summaries compact while retaining useful precision."""

    numeric = float(value)
    if numeric >= 10 or numeric.is_integer():
        return str(int(numeric))
    return f"{numeric:.2f}".rstrip("0").rstrip(".")


def _format_milliseconds(value: float) -> str:
    milliseconds = float(value) * 1000
    if milliseconds.is_integer():
        return str(int(milliseconds))
    return f"{milliseconds:.2f}".rstrip("0").rstrip(".")


__all__ = [
    "RateLimitDetails",
    "RateLimitGate",
    "RateLimitLease",
    "RateLimitPolicy",
    "RateLimitQueueTimeout",
    "RateLimitedRunner",
    "classify_exception",
    "classify_rate_limit",
    "is_rate_limit_retry",
    "normalize_route",
    "parse_retry_after",
    "rate_limit_policy_from_env",
    "retry_decision_request",
]
