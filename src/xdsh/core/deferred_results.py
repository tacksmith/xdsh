"""Task-scoped, runner-neutral destination for reports after a foreground turn.

Context-local binding crosses transparent runner wrappers without changing every
run signature. Adapters opt in; a report is evidence of output, not proof that a
background computation succeeded. Receivers must persist before returning.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DeferredResult:
    session_id: str
    runtime_generation: str
    sequence: int
    text: str
    reason: str
    # None means attribution is not verifiable; never attach to the latest turn.
    origin_id: str | None


@dataclass(frozen=True, slots=True)
class DeferredResultContext:
    origin_id: str
    receive: Callable[[DeferredResult], None]


_CURRENT: ContextVar[DeferredResultContext | None] = ContextVar(
    "xdsh_deferred_result_context",
    default=None,
)


def current_deferred_context() -> DeferredResultContext | None:
    return _CURRENT.get()


@contextmanager
def receive_deferred_results(
    origin_id: str,
    receive: Callable[[DeferredResult], None],
) -> Iterator[None]:
    """Bind one product turn without exposing transport types to a runner."""

    token = _CURRENT.set(DeferredResultContext(origin_id, receive))
    try:
        yield
    finally:
        _CURRENT.reset(token)
