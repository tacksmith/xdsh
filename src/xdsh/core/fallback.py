"""Runner-neutral single-hop fallback policy."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

from xdsh.core.events import (
    ArtifactEvent,
    ErrorEvent,
    FinishEvent,
    RetryEvent,
    RunnerEventT,
    SubagentEvent,
    ToolResultEvent,
    ToolStartEvent,
)
from xdsh.core.execution_policy import with_direct_execution_policy
from xdsh.core.runner import (
    AgentRunner,
    CapabilityAwareRunner,
    ReasoningEffort,
    RunnerCapabilities,
    close_event_stream,
)

_SUCCESS_REASONS = frozenset({"completed", "complete", "success", "succeeded"})


class FallbackRunner:
    """Retry one failed turn on its configured fallback model.

    Primary terminal errors are buffered so a successful fallback cannot leave
    transports in a false failed state. Non-terminal structured progress and
    usage remain visible and auditable.
    """

    def __init__(self, runner: AgentRunner) -> None:
        self._runner = runner

    @property
    def rate_limit_protected(self) -> bool:
        """Expose nested route protection so transports do not double-wrap it."""

        return bool(getattr(self._runner, "rate_limit_protected", False))

    @property
    def allow_fallback_on_rate_limit(self) -> bool:
        """Forward the nested route's explicit 429 fallback decision."""

        return bool(getattr(self._runner, "allow_fallback_on_rate_limit", False))

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
        effective_system_prompt = with_direct_execution_policy(system_prompt)
        selected_fallback = fallback.strip() if fallback is not None else None
        if not selected_fallback or selected_fallback == model:
            stream = self._runner.run(
                task,
                session_id=session_id,
                cwd=cwd,
                model=model,
                reasoning_effort=reasoning_effort,
                system_prompt=effective_system_prompt,
                resume_context=resume_context,
            )
            try:
                async for event in stream:
                    yield event
            finally:
                await close_event_stream(stream)
            return

        terminal: list[ErrorEvent | FinishEvent] = []
        dispatched_work = False
        stream = self._runner.run(
            task, session_id=session_id, cwd=cwd, model=model,
            reasoning_effort=reasoning_effort, system_prompt=effective_system_prompt,
            resume_context=resume_context,
        )
        try:
            async for event in stream:
                if isinstance(event, ToolStartEvent | ToolResultEvent | ArtifactEvent
                              | SubagentEvent):
                    dispatched_work = True
                if isinstance(event, ErrorEvent | FinishEvent):
                    terminal.append(event)
                else:
                    yield event
        except Exception:
            terminal = [FinishEvent(session_id=session_id, reason="error")]
        finally:
            await close_event_stream(stream)

        finish = next(
            (event for event in reversed(terminal) if isinstance(event, FinishEvent)),
            None,
        )
        failed = finish is None or (
            not finish.cancelled and finish.reason.casefold() not in _SUCCESS_REASONS
        )
        if not failed:
            if finish is not None:
                yield finish
            return
        if finish is not None and finish.cancelled:
            for event in terminal:
                yield event
            return

        if dispatched_work:
            # Native current-model-step retry is safe; starting the original
            # task on a new model after tool dispatch is NOT the same operation.
            # No reliable cross-runtime side-effect idempotency contract exists.
            for event in terminal:
                yield event
            return

        if any(
            isinstance(event, ErrorEvent) and event.code == "MODEL_ROUTE_UNVERIFIED"
            for event in terminal
        ):
            # A model-identity/configuration failure is not permission to run
            # the task on another model, even when a fallback is configured.
            for event in terminal:
                yield event
            return

        # A protected route has already applied bounded native/outer retry
        # policy.  Replaying the same user task on a second model immediately
        # after a provider 429 commonly hits the same account quota and can
        # turn one failure into a retry storm.  Keep the partial/terminal
        # events visible and require an explicit later retry unless the
        # deployment opted into fallback for independent routes.
        if (
            getattr(self._runner, "rate_limit_protected", False)
            and not getattr(self._runner, "allow_fallback_on_rate_limit", False)
            and (
                (finish is not None and finish.reason.casefold() == "rate_limited")
                or any(_is_rate_limit_error(event) for event in terminal)
            )
        ):
            for event in terminal:
                yield event
            return

        yield RetryEvent(session_id=session_id, provider="fallback")
        stream = self._runner.run(
            task,
            session_id=session_id,
            cwd=cwd,
            model=selected_fallback,
            reasoning_effort=reasoning_effort,
            system_prompt=effective_system_prompt,
            resume_context=resume_context,
        )
        try:
            async for event in stream:
                yield event
        finally:
            await close_event_stream(stream)

    async def cancel(self, session_id: str) -> bool:
        return await self._runner.cancel(session_id)

    async def runtime_generation(self, session_id: str) -> str | None:
        """Forward optional host-generation provenance through fallback."""

        method = getattr(self._runner, "runtime_generation", None)
        if method is None:
            return None
        return cast(str | None, await method(session_id))

    async def reset(self, session_id: str) -> None:
        await self._runner.reset(session_id)

    async def close(self) -> None:
        await self._runner.close()


def _is_rate_limit_error(event: ErrorEvent | FinishEvent) -> bool:
    if not isinstance(event, ErrorEvent):
        return False
    return event.status_code == 429 or event.code.casefold().startswith("rate_limit")
