"""Small runner-neutral bridge from channel messages to an AgentRunner.

The bridge is intentionally transport agnostic: channel adapters authenticate
and normalize a message, while this module owns only bounded session/workspace
selection and event-to-text collection.  Telegram keeps its richer historical
service; other channels can use this bridge or supply an equivalent handler.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from xdsh.channels.base import ChannelMessage, ChannelPayloadError
from xdsh.channels.commands import ChannelCommandRouter, parse_channel_command
from xdsh.core.events import ErrorEvent, FinishEvent, TextEvent
from xdsh.core.presentation import terminal_error_notice
from xdsh.core.product_control import redact_sensitive_text, safe_outbound_text
from xdsh.core.runner import REASONING_EFFORTS, AgentRunner, ReasoningEffort
from xdsh.core.sessions import SessionRegistry, SessionRegistryError

_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}$")


@dataclass(frozen=True, slots=True)
class ChannelAgentConfig:
    """Bounded state and presentation settings for non-Telegram channels."""

    state_root: Path
    workspace_root: Path
    default_model: str
    system_prompt: str = "You are a software engineering agent working through XDSH."
    max_answer_chars: int = 60_000
    task_timeout_seconds: float | None = 1_800.0
    max_sessions: int = 500

    def __post_init__(self) -> None:
        for name, root in (
            ("state_root", self.state_root),
            ("workspace_root", self.workspace_root),
        ):
            resolved = root.expanduser().resolve()
            if (
                root.is_symlink()
                or resolved == Path(resolved.anchor)
                or ".xclaude" in resolved.parts
                or ".dsh" in resolved.parts
            ):
                raise ValueError(f"{name} must be a dedicated XDSH root")
        if self.state_root.expanduser().resolve() == self.workspace_root.expanduser().resolve():
            raise ValueError("channel state and workspace roots must be separate")
        if _MODEL_ID.fullmatch(self.default_model) is None:
            raise ValueError("channel default model is invalid")
        if not self.system_prompt.strip() or len(self.system_prompt) > 32_000:
            raise ValueError("channel system prompt is invalid")
        if not 1_000 <= self.max_answer_chars <= 200_000:
            raise ValueError("channel answer bound is invalid")
        if self.task_timeout_seconds is not None and (
            not math.isfinite(self.task_timeout_seconds)
            or not 1.0 <= self.task_timeout_seconds <= 86_400
        ):
            raise ValueError("channel task timeout is out of bounds")
        if not 1 <= self.max_sessions <= 500:
            raise ValueError("channel session bound is invalid")


class ChannelAgentBridge:
    """Execute one normalized message and return a bounded final answer.

    The bridge also owns a deliberately small set of product controls.  They
    are registered in the same command router for every channel, so a new
    transport cannot accidentally turn a control message into a model task.
    A caller may provide additional handlers; built-ins win only when the
    caller has not registered the same name.
    """

    def __init__(
        self,
        runner: AgentRunner,
        config: ChannelAgentConfig,
        *,
        command_router: ChannelCommandRouter | None = None,
    ) -> None:
        self._runner = runner
        self.config = config
        self._command_router = command_router or ChannelCommandRouter()
        self._locks: dict[str, asyncio.Lock] = {}
        self._active_sessions: dict[str, str] = {}
        self._register_builtin_commands(command_router is not None)

    @property
    def command_router(self) -> ChannelCommandRouter:
        """Return the shared command registry used by this bridge."""

        return self._command_router

    def _register_builtin_commands(self, custom_router: bool) -> None:
        """Register safe controls without replacing caller-owned handlers."""

        handlers = {
            "help": self._command_help,
            "status": self._command_status,
            "session": self._command_session,
            "model": self._command_model,
            "reasoning": self._command_reasoning,
            "thinking": self._command_thinking,
            "cancel": self._command_cancel,
            "reset": self._command_reset,
        }
        for name, handler in handlers.items():
            if custom_router and self._command_router.handles(name):
                continue
            self._command_router.register(name, handler)

    async def handle(self, message: ChannelMessage) -> str:
        """Run a channel message, preserving same-scope multi-turn context."""

        if not message.text.strip():
            raise ChannelPayloadError("channel message text is empty")
        command = parse_channel_command(message.text)
        if command is not None and self._command_router.handles(command.name):
            try:
                return (await self._command_router.dispatch(message)) or ""
            except Exception as error:
                safe_error = safe_outbound_text(
                    f"{type(error).__name__}: {error}",
                    max_chars=600,
                )
                return f"⚠️ 命令未完成: {safe_error}"
        scope = _scope_key(message)
        lock = self._locks.setdefault(scope, asyncio.Lock())
        async with lock:
            return await self._run_locked(message, scope)

    async def _run_locked(self, message: ChannelMessage, scope: str) -> str:
        state_root = (
            self.config.state_root.expanduser().resolve()
            / "channels"
            / message.channel
            / scope
        )
        workspace_root = (
            self.config.workspace_root.expanduser().resolve() / message.channel / scope
        )
        state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        workspace_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if state_root.is_symlink() or workspace_root.is_symlink():
            raise ChannelPayloadError("channel state or workspace path is a symlink")
        try:
            registry = SessionRegistry(
                state_root,
                default_model=self.config.default_model,
                namespace="main",
                max_sessions=self.config.max_sessions,
            )
            session = registry.current()
        except SessionRegistryError as error:
            raise ChannelPayloadError("channel session state is invalid") from error
        events: list[object] = []
        self._active_sessions[scope] = session.runner_session_id
        try:
            stream = self._runner.run(
                message.text,
                session_id=session.runner_session_id,
                cwd=workspace_root,
                model=session.model,
                reasoning_effort=session.reasoning_effort,
                fallback=session.fallback_model,
                system_prompt=self.config.system_prompt,
            )
            if self.config.task_timeout_seconds is None:
                await self._consume(stream, events)
            else:
                await asyncio.wait_for(
                    self._consume(stream, events),
                    timeout=self.config.task_timeout_seconds,
                )
        except asyncio.CancelledError:
            await self._runner.cancel(session.runner_session_id)
            raise
        except asyncio.TimeoutError:
            await self._runner.cancel(session.runner_session_id)
            return _partial_answer(
                events,
                self.config.max_answer_chars,
                "任務逾時, 已保留部分輸出.",
            )
        except Exception as error:
            # Never return provider exception text verbatim: it may contain a
            # URL credential or an API response body.
            partial = _partial_answer(events, self.config.max_answer_chars, "")
            safe_error = safe_outbound_text(
                f"{type(error).__name__}: {error}",
                max_chars=600,
            )
            if partial:
                return f"{partial}\n\n⚠️ 任務未完成: {safe_error}"
            return f"⚠️ 任務未完成: {safe_error}"
        finally:
            self._active_sessions.pop(scope, None)
        answer = _partial_answer(events, self.config.max_answer_chars, "")
        error_event = next(
            (event for event in reversed(events) if isinstance(event, ErrorEvent)),
            None,
        )
        finish = next((event for event in reversed(events) if isinstance(event, FinishEvent)), None)
        if finish is None:
            if error_event is not None:
                notice = terminal_error_notice(error_event, has_answer=bool(answer))
                return f"{answer}\n\n⚠️ {notice}" if answer else f"⚠️ {notice}"
            return answer or "⚠️ Runner 未回傳完成事件。"
        if error_event is not None:
            notice = terminal_error_notice(error_event, has_answer=bool(answer))
            return f"{answer}\n\n⚠️ {notice}" if answer else f"⚠️ {notice}"
        if finish.reason.casefold() not in {"completed", "complete", "success", "succeeded"}:
            suffix = "任務已取消。" if finish.cancelled else "任務未完成。"
            return f"{answer}\n\n⚠️ {suffix}" if answer else f"⚠️ {suffix}"
        return answer or "✅ 任務完成 (沒有文字輸出)."

    async def _consume(self, stream: AsyncIterator[object], events: list[object]) -> None:
        async for event in stream:
            events.append(event)

    def _scope_paths(self, message: ChannelMessage) -> tuple[str, Path, Path]:
        """Resolve one channel scope without exposing provider identifiers."""

        scope = _scope_key(message)
        state_root = (
            self.config.state_root.expanduser().resolve()
            / "channels"
            / message.channel
            / scope
        )
        workspace_root = (
            self.config.workspace_root.expanduser().resolve() / message.channel / scope
        )
        return scope, state_root, workspace_root

    def _registry_for(self, message: ChannelMessage) -> SessionRegistry:
        _scope, state_root, _workspace_root = self._scope_paths(message)
        state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if state_root.is_symlink():
            raise ChannelPayloadError("channel session state path is a symlink")
        try:
            return SessionRegistry(
                state_root,
                default_model=self.config.default_model,
                namespace="main",
                max_sessions=self.config.max_sessions,
            )
        except SessionRegistryError as error:
            raise ChannelPayloadError("channel session state is invalid") from error

    async def _command_help(
        self,
        message: ChannelMessage,
        arguments: str,
    ) -> str:
        del message, arguments
        return (
            "XDSH channel controls\n"
            "/status · current task/session health\n"
            "/session [list|new NAME|use SELECT|rename SELECT NAME|delete SELECT]\n"
            "/model [MODEL] · /reasoning [off|minimal|low|medium|high|xhigh|max]\n"
            "/thinking [on|off] · /cancel · /reset\n"
            "Any other message is sent to the DSH runner."
        )

    async def _command_status(
        self,
        message: ChannelMessage,
        arguments: str,
    ) -> str:
        if arguments.strip():
            return "用法: /status"
        scope, _state_root, _workspace_root = self._scope_paths(message)
        registry = self._registry_for(message)
        session = registry.current()
        running = self._active_sessions.get(scope) == session.runner_session_id
        return (
            f"🟢 {message.channel} · {'執行中' if running else '待命'}\n"
            f"session: {session.name} ({session.key})\n"
            f"model: {session.model}\n"
            f"reasoning: {session.reasoning_effort} · thinking: "
            f"{'on' if session.show_thinking else 'off'}"
        )

    async def _command_session(
        self,
        message: ChannelMessage,
        arguments: str,
    ) -> str:
        scope, _state_root, _workspace_root = self._scope_paths(message)
        registry = self._registry_for(message)
        action, separator, remainder = arguments.strip().partition(" ")
        normalized = action.casefold()
        if not normalized or normalized in {"list", "ls"}:
            current = registry.current()
            lines = ["🗂️ sessions"]
            for item in registry.list_sessions():
                marker = "▶" if item.key == current.key else "·"
                lines.append(f"{marker} {item.name} ({item.key}) · {item.model}")
            return "\n".join(lines)
        if self._active_sessions.get(scope) is not None:
            return "⚠️ 目前任務執行中; 請先 /cancel 再切換會話。"
        if normalized == "new":
            created = registry.create(remainder.strip() or None)
            return f"✅ 已建立並切換會話 {created.name} ({created.key})。"
        if normalized in {"use", "switch"}:
            if not separator or not remainder.strip():
                return "用法: /session use <name|key>"
            selected = registry.switch(remainder.strip())
            return f"✅ 已切換至 {selected.name} ({selected.key})。"
        if normalized == "rename":
            selector, name_separator, name = remainder.strip().partition(" ")
            if not name_separator or not selector or not name.strip():
                return "用法: /session rename <name|key> <new-name>"
            renamed = registry.rename(selector, name.strip())
            return f"✅ 已重新命名為 {renamed.name} ({renamed.key})。"
        if normalized == "delete":
            if not separator or not remainder.strip():
                return "用法: /session delete <name|key>"
            deleted = registry.delete(remainder.strip())
            return f"✅ 已刪除 {deleted.deleted.name}; 目前會話是 {deleted.current.name}。"
        return "用法: /session [list|new NAME|use SELECT|rename SELECT NAME|delete SELECT]"

    async def _command_model(
        self,
        message: ChannelMessage,
        arguments: str,
    ) -> str:
        registry = self._registry_for(message)
        value = arguments.strip()
        if not value:
            return f"🤖 目前模型: {registry.current().model}\n用法: /model <LiteLLM model ID>"
        try:
            selected = registry.select_model(value)
        except SessionRegistryError:
            return "⚠️ 模型 ID 無效; 請使用 LiteLLM /model/info 返回的路由 ID。"
        return f"✅ 本會話模型已切換為 {selected.model}。"

    async def _command_reasoning(
        self,
        message: ChannelMessage,
        arguments: str,
    ) -> str:
        registry = self._registry_for(message)
        value = arguments.strip().casefold()
        if not value:
            return f"🧠 reasoning: {registry.current().reasoning_effort}"
        if value not in REASONING_EFFORTS:
            return "用法: /reasoning [off|minimal|low|medium|high|xhigh|max]"
        selected = registry.select_reasoning(cast(ReasoningEffort, value))
        return f"✅ reasoning 已設為 {selected.reasoning_effort}。"

    async def _command_thinking(
        self,
        message: ChannelMessage,
        arguments: str,
    ) -> str:
        registry = self._registry_for(message)
        value = arguments.strip().casefold()
        if not value:
            return f"💭 thinking: {'on' if registry.current().show_thinking else 'off'}"
        if value not in {"on", "off"}:
            return "用法: /thinking [on|off]"
        selected = registry.set_show_thinking(value == "on")
        return f"✅ thinking 已{'開啟' if selected.show_thinking else '關閉'}。"

    async def _command_cancel(
        self,
        message: ChannelMessage,
        arguments: str,
    ) -> str:
        if arguments.strip():
            return "用法: /cancel"
        scope, _state_root, _workspace_root = self._scope_paths(message)
        session = self._registry_for(message).current()
        if self._active_sessions.get(scope) != session.runner_session_id:
            return "目前沒有執行中的任務。"
        cancelled = await self._runner.cancel(session.runner_session_id)
        return "🛑 已發出取消" if cancelled else "目前任務已結束或正在收尾。"

    async def _command_reset(
        self,
        message: ChannelMessage,
        arguments: str,
    ) -> str:
        if arguments.strip():
            return "用法: /reset"
        scope, _state_root, _workspace_root = self._scope_paths(message)
        session = self._registry_for(message).current()
        if self._active_sessions.get(scope) is not None:
            return "⚠️ 目前任務執行中; 請先 /cancel 再 reset。"
        await self._runner.reset(session.runner_session_id)
        return "↻ 已切換到新的 DSH generation; 產品會話設定保留。"


def _scope_key(message: ChannelMessage) -> str:
    identity = f"{message.channel}\x00{message.conversation_id}".encode()
    return hashlib.sha256(identity).hexdigest()[:32]


def _partial_answer(events: list[object], maximum: int, suffix: str) -> str:
    chunks = [
        redact_sensitive_text(event.text)
        for event in events
        if isinstance(event, TextEvent) and event.text
    ]
    answer = "".join(chunks)
    if not answer:
        final = next(
            (
                event.final_text
                for event in reversed(events)
                if isinstance(event, FinishEvent) and event.final_text
            ),
            "",
        )
        answer = redact_sensitive_text(final)
    if len(answer) > maximum:
        answer = answer[:maximum] + "\n\n[…輸出已截斷]"
    if suffix:
        answer = f"{answer}\n\n⚠️ {suffix}" if answer else f"⚠️ {suffix}"
    return answer
