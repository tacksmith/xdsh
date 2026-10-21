"""Runner interface consumed by CLI and Telegram layers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, TypeAlias, runtime_checkable

from xdsh.core.events import RunnerEventT

ReasoningEffort: TypeAlias = Literal[
    "off",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
]
REASONING_EFFORTS: tuple[ReasoningEffort, ...] = (
    "off",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)


@dataclass(frozen=True, slots=True)
class RunnerCapabilities:
    """Fail-closed capability snapshot from one exact runtime adapter."""

    protocol: str
    runtime_package: str
    runtime_identity: str
    native: frozenset[str]
    unsupported_native: frozenset[str]
    source: str = "pinned_adapter"


@runtime_checkable
class CapabilityAwareRunner(Protocol):
    """Optional read-only capability surface for operations and transports."""

    @property
    def capabilities(self) -> RunnerCapabilities: ...


@runtime_checkable
class RuntimeGenerationAwareRunner(Protocol):
    """Optional host-owned runtime identity for durable product controls.

    This is deliberately separate from :class:`AgentRunner`: DSH rc.2 does
    not expose a generation or connection-fencing field.  An adapter may
    provide an opaque XDSH-owned marker so a checkpoint can identify which
    host process/generation produced it without claiming native DSH resume.
    """

    async def runtime_generation(self, session_id: str) -> str | None: ...


class AgentRunner(Protocol):
    """Transport-neutral lifecycle for an agent runtime.

    Each run ends with one authoritative ``FinishEvent``. A turn-level
    ``ErrorEvent`` is emitted only when that finish reason is ``error``.
    """

    def run(
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
    ) -> AsyncIterator[RunnerEventT]: ...

    async def cancel(self, session_id: str) -> bool: ...

    async def reset(self, session_id: str) -> None: ...

    async def close(self) -> None: ...


def has_native_capability(runner: AgentRunner, capability: str) -> bool:
    """Return a verified adapter capability through any transparent wrappers."""

    if not capability or not isinstance(runner, CapabilityAwareRunner):
        return False
    return capability in runner.capabilities.native


async def close_event_stream(stream: AsyncIterator[RunnerEventT]) -> None:
    """Propagate deterministic cleanup through wrappers, including early return.

    AsyncIterator does not require aclose; async generators do. Do not rely on
    GC to release native session locks or request leases before the next turn.
    """
    close = getattr(stream, "aclose", None)
    if close is not None:
        await close()
