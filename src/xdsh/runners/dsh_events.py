"""Translate official DSH session notifications into core events."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TypedDict, cast

from xdsh.core.events import (
    TRANSIENT_REQUEST_FAILURES,
    ActivityEvent,
    ActivityPhase,
    ArtifactEvent,
    CompactionAction,
    CompactionEvent,
    ErrorEvent,
    JsonValue,
    ReasoningEvent,
    RetryEvent,
    RunnerEventT,
    SubagentAction,
    SubagentEvent,
    TextEvent,
    TodoEvent,
    TodoItem,
    ToolResultEvent,
    ToolStartEvent,
    UsageEvent,
)
from xdsh.core.product_control import safe_outbound_text
from xdsh.core.rate_limit import classify_rate_limit, parse_retry_after

_RETRYABLE_ERROR_CODES = {"OVERLOADED", "RATE_LIMIT", "UNAVAILABLE"}
_MAX_COMPACTION_ITEMS = 100_000
_MAX_COMPACTION_MEASUREMENT = 2_000_000_000
_MAX_ERROR_MESSAGE_CHARS = 4_000
_MAX_ERROR_CODE_CHARS = 120
_MUTATING_TOOLS = {
    "apply_patch",
    "edit",
    "notebook",
    "str_replace_editor",
    "write",
}
_MUTATING_EDITOR_COMMANDS = {"create", "insert", "str_replace", "write"}


@dataclass(frozen=True, slots=True)
class _ToolCall:
    name: str
    arguments: JsonValue | None


class _EventContext(TypedDict):
    session_id: str
    sequence: int | None
    timestamp_ms: int | None
    runtime_generation: str | None


class DshEventNormalizer:
    """Stateful normalizer for one root DSH session run."""

    def __init__(
        self,
        *,
        session_id: str,
        dsh_session_id: str,
        runtime_generation: str | None = None,
    ) -> None:
        self.session_id = session_id
        self.dsh_session_id = dsh_session_id
        self.runtime_generation = runtime_generation
        self.finish_reason: str | None = None
        self._calls: dict[str, _ToolCall] = {}
        self._text_steps: set[tuple[int, int]] = set()
        self._reasoning_steps: set[tuple[int, int]] = set()
        self._usage_steps: set[tuple[int, int]] = set()
        self._latest_sequence: int | None = None

    def feed(
        self,
        notification: object,
        *,
        runtime_generation: str | None = None,
    ) -> tuple[RunnerEventT, ...]:
        """Normalize one SDK notification; unrelated notifications are ignored."""

        expected_generation = self.runtime_generation
        if expected_generation is not None and runtime_generation != expected_generation:
            # RC2 does not carry process identity on the wire. The adapter
            # supplies it at this seam and fails closed for retired or
            # unverified runtime notifications.
            return ()
        event_generation = expected_generation or runtime_generation

        method, payload = _notification_parts(notification)
        method = _canonical_notification_method(method)
        if isinstance(method, str) and method in {
            "subagent.started",
            "subagent.finished",
        } and isinstance(
            payload, dict
        ):
            return self._subagent_events(method, payload, runtime_generation=event_generation)
        if method != "session.event" or not isinstance(payload, dict):
            return ()
        payload_session_id = _mapping_value(payload, "sessionId", "session_id", "session")
        if payload_session_id != self.dsh_session_id:
            return ()
        raw_event = _mapping_value(payload, "event", "notification", "data")
        # A few preview clients wrap the event one level deeper under a
        # ``payload`` field.  Keep the traversal explicit and bounded; never
        # inspect arbitrary JSON looking for text that resembles an event.
        if isinstance(raw_event, dict) and "event" in raw_event:
            nested = raw_event.get("event")
            if isinstance(nested, dict):
                raw_event = nested
        if not isinstance(raw_event, dict):
            return ()
        return self._feed_session_event(raw_event, runtime_generation=event_generation)

    def _subagent_events(
        self,
        method: str,
        payload: dict[object, object],
        *,
        runtime_generation: str | None,
    ) -> tuple[RunnerEventT, ...]:
        if payload.get("parentSessionId") != self.dsh_session_id:
            return ()
        child_session_id = payload.get("childSessionId")
        if not isinstance(child_session_id, str):
            return ()
        if method == "subagent.started":
            return (
                SubagentEvent(
                    session_id=self.session_id,
                    child_session_id=child_session_id,
                    action=SubagentAction.STARTED,
                    runtime_generation=runtime_generation,
                ),
            )
        status = payload.get("status")
        last_message = payload.get("lastAssistantMessage")
        final_text = (
            _render_tool_content(last_message.get("content"))
            if isinstance(last_message, dict)
            else ""
        )
        return (
            SubagentEvent(
                session_id=self.session_id,
                child_session_id=child_session_id,
                action=SubagentAction.FINISHED,
                status=status if isinstance(status, str) else None,
                final_text=final_text,
                runtime_generation=runtime_generation,
            ),
        )

    def _feed_session_event(
        self,
        event: dict[object, object],
        *,
        runtime_generation: str | None,
    ) -> tuple[RunnerEventT, ...]:
        event_type = _mapping_value(
            event,
            "type",
            "eventType",
            "event_type",
            "kind",
            "name",
        )
        data = _mapping_value(event, "data", "payload", "details")
        if isinstance(event_type, str):
            event_type = _canonical_event_type(event_type)
        if not isinstance(event_type, str) or not isinstance(data, dict):
            return ()
        sequence = _optional_int(_mapping_value(event, "seq", "sequence"))
        timestamp_ms = _optional_int(_mapping_value(event, "time", "timestamp", "timestamp_ms"))
        if sequence is not None:
            if sequence < 0 or (
                self._latest_sequence is not None and sequence <= self._latest_sequence
            ):
                # A notification from a retired generation, a replayed frame,
                # or a reordered transport frame must not overwrite newer
                # product state.  Keep only the high-water mark: storing event
                # bodies or hashes would make long runs grow without bound.
                return ()
        common: _EventContext = {
            "session_id": self.session_id,
            "sequence": sequence,
            "timestamp_ms": timestamp_ms,
            "runtime_generation": runtime_generation,
        }
        events = self._session_events(event_type, data, common)
        if events and sequence is not None:
            # Commit order only after this notification produced a validated
            # normalized event. An unknown or malformed high-sequence frame
            # must not suppress subsequent valid product updates.
            self._latest_sequence = sequence
        return events

    def _session_events(
        self,
        event_type: str,
        data: dict[object, object],
        common: _EventContext,
    ) -> tuple[RunnerEventT, ...]:
        if event_type == "assistant/chunk":
            return self._chunk_events(data, common)
        if event_type == "assistant/message":
            return self._assistant_message_events(data, common)
        if event_type == "llm/retry":
            provider = data.get("provider")
            rate_limit = _rate_limit_fields(data)
            failure = data.get("failure")
            raw_code = failure.get("code") if isinstance(failure, dict) else None
            code = raw_code if isinstance(raw_code, str) and raw_code in (
                TRANSIENT_REQUEST_FAILURES | {"RATE_LIMIT"}
            ) else None
            # A retry begins another attempt of the SAME step. Do not let
            # dedup from its failed attempt suppress the successful message or
            # its billable usage. Already emitted partials remain diagnostics.
            step = _step_key(data)
            if step is not None:
                self._text_steps.discard(step)
                self._reasoning_steps.discard(step)
                self._usage_steps.discard(step)
            return (
                RetryEvent(
                    failure_code=code,
                    provider=provider if isinstance(provider, str) else None,
                    status_code=rate_limit[0],
                    retry_after_seconds=rate_limit[1],
                    retry_delay_seconds=_milliseconds_to_seconds(data.get("delayMs")),
                    attempt=_bounded_positive_int(
                        _first_nested(data, "attempt", "retryAttempt", "retry_attempt", "retry")
                    ),
                    max_attempts=_bounded_positive_int(
                        _first_nested(
                            data, "maxAttempts", "max_attempts", "retryLimit", "maxRetries",
                        )
                    ),
                    rate_limited=rate_limit[2],
                    **common,
                ),
            )
        if event_type == "tool/call":
            return self._tool_start_events(data, common)
        if event_type == "tool/result":
            return self._tool_result_events(data, common)
        if event_type == "todo/write":
            return self._todo_events(data, common)
        if event_type.startswith("compaction/"):
            return self._compaction_events(event_type, data, common)
        if event_type == "turn/end":
            return self._turn_end_events(data, common)
        return ()

    def _compaction_events(
        self,
        event_type: str,
        data: dict[object, object],
        common: _EventContext,
    ) -> tuple[RunnerEventT, ...]:
        """Normalize official DSH compaction notifications without text parsing."""

        raw_action = event_type.removeprefix("compaction/")
        try:
            action = CompactionAction(raw_action)
        except ValueError:
            return ()
        shadowed = data.get("shadowedSeqs")
        shadowed_items = len(shadowed) if isinstance(shadowed, list) else 0
        # Keep untrusted notification measurements bounded before they reach
        # renderers or metrics.  The runtime itself emits ordinary safe ints,
        # but a malformed JSON-RPC peer must not create huge counters.
        shadowed_items = min(shadowed_items, _MAX_COMPACTION_ITEMS)
        shadowed_tokens = _bounded_nonnegative_int(data.get("shadowedTokenCount"))
        pruned_characters = _bounded_nonnegative_int(data.get("charsRemoved"))
        raw_error = data.get("error")
        error = isinstance(raw_error, dict | str) or raw_error is True
        compaction_id = _bounded_text(data.get("compactionId"), 200)
        provider = _bounded_text(data.get("provider"), 120)
        model = _bounded_text(data.get("model"), 200)
        return (
            CompactionEvent(
                action=action,
                compaction_id=compaction_id,
                turn=_bounded_optional_int(data.get("turn")),
                shadowed_items=shadowed_items,
                shadowed_tokens=shadowed_tokens,
                pruned_characters=pruned_characters,
                provider=provider,
                model=model,
                error=error,
                **common,
            ),
        )

    def _chunk_events(
        self,
        data: dict[object, object],
        common: _EventContext,
    ) -> tuple[RunnerEventT, ...]:
        chunk = data.get("chunk")
        if not isinstance(chunk, dict):
            return ()
        chunk_type = chunk.get("type")
        step = _step_key(data)
        if chunk_type == "text-delta" and isinstance(chunk.get("text"), str):
            if step is not None:
                self._text_steps.add(step)
            return (TextEvent(text=cast(str, chunk["text"]), delta=True, **common),)
        if chunk_type == "reasoning-delta" and isinstance(chunk.get("text"), str):
            if step is not None:
                self._reasoning_steps.add(step)
            return (ReasoningEvent(text=cast(str, chunk["text"]), delta=True, **common),)
        if chunk_type == "usage" and isinstance(chunk.get("usage"), dict):
            if step is not None:
                self._usage_steps.add(step)
            return (_usage_event(cast(dict[object, object], chunk["usage"]), common),)
        if _valid_tool_call_delta(chunk):
            # The raw arguments fragment may contain source, credentials, or
            # many megabytes of generated file content.  Preserve only the
            # observed phase and length, not the payload. Empty prelude or
            # keep-alive frames are valid observations but cannot prove new
            # output or extend the guard's content-activity deadline.
            return (ActivityEvent(
                phase=ActivityPhase.TOOL_ARGUMENTS,
                output_characters=len(cast(str, chunk["argumentsDelta"])),
                **common,
            ),)
        return ()

    def _assistant_message_events(
        self,
        data: dict[object, object],
        common: _EventContext,
    ) -> tuple[RunnerEventT, ...]:
        events: list[RunnerEventT] = []
        step = _step_key(data)
        message = data.get("message")
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    block_type = block.get("type")
                    text = block.get("text")
                    if block_type == "text" and isinstance(text, str):
                        if step is None or step not in self._text_steps:
                            events.append(TextEvent(text=text, delta=False, **common))
                    elif block_type == "reasoning" and isinstance(text, str):
                        if step is None or step not in self._reasoning_steps:
                            events.append(ReasoningEvent(text=text, delta=False, **common))
        usage = data.get("usage")
        if isinstance(usage, dict) and (step is None or step not in self._usage_steps):
            events.append(_usage_event(usage, common))
            if step is not None:
                self._usage_steps.add(step)
        return tuple(events)

    def _tool_start_events(
        self,
        data: dict[object, object],
        common: _EventContext,
    ) -> tuple[RunnerEventT, ...]:
        call_id = data.get("callId")
        name = data.get("name")
        if not isinstance(call_id, str) or not isinstance(name, str):
            return ()
        arguments = _parse_arguments(data.get("arguments"))
        self._calls[call_id] = _ToolCall(name=name, arguments=arguments)
        return (
            ToolStartEvent(call_id=call_id, name=name, arguments=arguments, **common),
        )

    def _tool_result_events(
        self,
        data: dict[object, object],
        common: _EventContext,
    ) -> tuple[RunnerEventT, ...]:
        message = data.get("message")
        if not isinstance(message, dict):
            return ()
        source = message.get("source")
        call_id = source.get("callId") if isinstance(source, dict) else None
        content = message.get("content")
        block: dict[object, object] | None = None
        if isinstance(content, list):
            block = next(
                (
                    item
                    for item in content
                    if isinstance(item, dict) and item.get("type") == "tool-result"
                ),
                None,
            )
        if not isinstance(call_id, str) and block is not None:
            block_call_id = block.get("toolCallId")
            call_id = block_call_id if isinstance(block_call_id, str) else None
        if not isinstance(call_id, str):
            return ()

        call = self._calls.get(call_id)
        is_error = bool(block.get("isError")) if block is not None else False
        if isinstance(data.get("error"), dict):
            is_error = True
        # Consume optional structured metadata when a future/alternate official
        # composition supplies it. The current SDK runtime may omit the raw
        # typed Bash value, so never infer status from human-readable output.
        if call is not None and call.name == "bash" and _bash_meta_failed(data.get("meta")):
            is_error = True
        rendered = _render_tool_content(block.get("content") if block is not None else None)
        events: list[RunnerEventT] = [
            ToolResultEvent(
                call_id=call_id,
                name=call.name if call is not None else None,
                content=rendered,
                is_error=is_error,
                **common,
            )
        ]
        artifact = _artifact_event(call, is_error, common)
        if artifact is not None:
            events.append(artifact)
        return tuple(events)

    def _todo_events(
        self,
        data: dict[object, object],
        common: _EventContext,
    ) -> tuple[RunnerEventT, ...]:
        raw_items = data.get("todos")
        if not isinstance(raw_items, list):
            return ()
        items: list[TodoItem] = []
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                continue
            content = raw_item.get("content")
            status = raw_item.get("status")
            if isinstance(content, str) and isinstance(status, str):
                items.append(TodoItem(content=content, status=status))
        return (TodoEvent(items=tuple(items), **common),)

    def _turn_end_events(
        self,
        data: dict[object, object],
        common: _EventContext,
    ) -> tuple[RunnerEventT, ...]:
        reason = data.get("reason")
        if not isinstance(reason, dict) or not isinstance(reason.get("kind"), str):
            return ()
        self.finish_reason = cast(str, reason["kind"])
        if self.finish_reason != "error" or not isinstance(reason.get("error"), dict):
            return ()
        failure = cast(dict[object, object], reason["error"])
        message = failure.get("message")
        code = failure.get("code")
        raw_message = message if isinstance(message, str) else "DSH turn failed"
        if "[XDSH_STREAM_INCOMPLETE]" in raw_message:
            raw_code = "UPSTREAM_STREAM_INCOMPLETE"
        elif "[XDSH_PROVIDER_STREAM_IDLE]" in raw_message:
            raw_code = "MODEL_STREAM_IDLE"
        elif code == "TIMEOUT" and re.fullmatch(
            r"pi-ai stream idle timeout after [0-9]{1,8}ms", raw_message.strip(),
        ):
            # Only the structured runtime failure, not tool output or stdout.
            # Replaying the whole task could duplicate earlier tool operations.
            raw_code = "MODEL_STREAM_IDLE"
        elif code == "UNKNOWN" and "id collision" in raw_message.casefold():
            raw_code = "DSH_SESSION_ID_COLLISION"
        elif not isinstance(code, str) or code == "UNKNOWN":
            raw_code = "DSH_TURN_ERROR"
        else:
            raw_code = code
        rate_limit = _rate_limit_fields(failure)
        if rate_limit[2]:
            if raw_code.casefold() in {"unknown", "dsh_turn_error"}:
                raw_code = "RATE_LIMIT"
        # The JSON-RPC peer is an untrusted boundary.  Keep the structured
        # error useful for diagnostics, but do not let provider bodies,
        # control characters, or an unbounded string enter the core event
        # stream where a later consumer might persist it.
        normalized_message = _safe_event_text(
            raw_message,
            fallback="DSH turn failed",
            maximum=_MAX_ERROR_MESSAGE_CHARS,
        )
        normalized_code = _safe_event_text(
            raw_code,
            fallback="DSH_TURN_ERROR",
            maximum=_MAX_ERROR_CODE_CHARS,
        )
        normalized_code = " ".join(normalized_code.split()) or "DSH_TURN_ERROR"
        return (
            ErrorEvent(
                message=normalized_message,
                code=normalized_code,
                retryable=raw_code in _RETRYABLE_ERROR_CODES or rate_limit[2],
                status_code=rate_limit[0],
                retry_after_seconds=rate_limit[1],
                **common,
            ),
        )


def _notification_parts(notification: object) -> tuple[object, object]:
    """Read current and future SDK notification containers without text parsing."""

    if isinstance(notification, Mapping):
        method = notification.get(
            "method",
            notification.get("name", notification.get("type")),
        )
        payload = notification.get(
            "payload",
            notification.get("params", notification.get("data")),
        )
        return method, payload
    method = getattr(notification, "method", None)
    if method is None:
        method = getattr(notification, "name", None)
    if method is None:
        method = getattr(notification, "type", None)
    payload = getattr(notification, "payload", None)
    if payload is None:
        payload = getattr(notification, "params", None)
    if payload is None:
        payload = getattr(notification, "data", None)
    return method, payload


def _mapping_value(mapping: Mapping[object, object], *names: str) -> object:
    for name in names:
        if name in mapping:
            return mapping[name]
    return None


def _canonical_notification_method(value: object) -> object:
    if not isinstance(value, str):
        return value
    return {
        "session/event": "session.event",
        "session_event": "session.event",
        "session.event": "session.event",
        "event/session": "session.event",
        "subagent/started": "subagent.started",
        "subagent/finished": "subagent.finished",
    }.get(value, value)


def _canonical_event_type(value: str) -> str:
    """Accept separator/name changes while preserving unknown events as no-ops."""

    normalized = value.strip().replace(".", "/")
    if not normalized.startswith("compaction/"):
        return normalized
    prefix, _, action = normalized.partition("/")
    aliases = {
        "begin": "start",
        "started": "start",
        "summarize": "summary",
        "summarized": "summary",
        "pruned": "prune",
        "complete": "end",
        "completed": "end",
        "finish": "end",
        "finished": "end",
    }
    return f"{prefix}/{aliases.get(action, action)}"


def _usage_event(
    usage: dict[object, object],
    common: _EventContext,
) -> UsageEvent:
    return UsageEvent(
        input_tokens=_int(usage.get("inputTokens")),
        output_tokens=_int(usage.get("outputTokens")),
        cache_read_tokens=_int(usage.get("cacheReadTokens")),
        cache_write_tokens=_int(usage.get("cacheWriteTokens")),
        reasoning_tokens=_int(usage.get("reasoningTokens")),
        **common,
    )


def _bash_meta_failed(raw_meta: object) -> bool:
    """Return whether official typed Bash metadata describes a failed run."""

    if not isinstance(raw_meta, dict):
        return False
    exit_code = raw_meta.get("exitCode")
    if isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code != 0:
        return True
    if raw_meta.get("timedOut") is True or raw_meta.get("aborted") is True:
        return True
    if isinstance(raw_meta.get("signal"), str) and raw_meta["signal"]:
        return True
    sandbox = raw_meta.get("sandbox")
    return isinstance(sandbox, dict) and sandbox.get("denied") is True


def _artifact_event(
    call: _ToolCall | None,
    is_error: bool,
    common: _EventContext,
) -> ArtifactEvent | None:
    if call is None or is_error or call.name not in _MUTATING_TOOLS:
        return None
    arguments = call.arguments
    if not isinstance(arguments, dict):
        return None
    command = arguments.get("command")
    if call.name == "str_replace_editor" and command not in _MUTATING_EDITOR_COMMANDS:
        return None
    path = next(
        (
            arguments[key]
            for key in ("path", "file_path", "notebook_path")
            if isinstance(arguments.get(key), str)
        ),
        None,
    )
    if not isinstance(path, str):
        return None
    operation = command if isinstance(command, str) else call.name
    return ArtifactEvent(path=path, operation=operation, **common)


def _parse_arguments(value: object) -> JsonValue | None:
    if not isinstance(value, str):
        return None
    try:
        return cast(JsonValue, json.loads(value))
    except json.JSONDecodeError:
        return value


def _render_tool_content(value: object) -> str:
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if item.get("type") in {"text", "reasoning"} and isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def _step_key(data: dict[object, object]) -> tuple[int, int] | None:
    turn = _optional_int(data.get("turn"))
    step = _optional_int(data.get("step"))
    return (turn, step) if turn is not None and step is not None else None


def _valid_tool_call_delta(chunk: Mapping[object, object]) -> bool:
    """Recognize the exact RC2 stream shape without retaining its payload."""

    if chunk.get("type") != "tool-call-delta":
        return False
    index = chunk.get("index")
    call_id = chunk.get("id")
    arguments_delta = chunk.get("argumentsDelta")
    name = chunk.get("name")
    return (
        isinstance(index, int)
        and not isinstance(index, bool)
        and 0 <= index <= 1_000_000
        and isinstance(call_id, str)
        and 1 <= len(call_id) <= 512
        and isinstance(arguments_delta, str)
        and (name is None or (isinstance(name, str) and len(name) <= 512))
    )


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _bounded_nonnegative_int(
    value: object,
    *,
    maximum: int = _MAX_COMPACTION_MEASUREMENT,
) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        return 0
    return min(max(0, value), maximum)


def _bounded_text(value: object, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:maximum] or None


def _safe_event_text(value: str, *, fallback: str, maximum: int) -> str:
    """Bound and redact text crossing from an untrusted DSH notification."""

    safe = safe_outbound_text(value, max_chars=maximum)
    return safe or fallback


def _bounded_optional_int(value: object) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return None
    return min(value, _MAX_COMPACTION_MEASUREMENT)


def _rate_limit_fields(data: Mapping[object, object]) -> tuple[int | None, float | None, bool]:
    """Return status, bounded Retry-After, and evidence of a 429 response."""

    status = _bounded_status(
        _first_nested(
            data,
            "statusCode",
            "status_code",
            "httpStatus",
            "http_status",
            "status",
        )
    )
    code = _first_nested(data, "code", "errorCode", "error_code", "type")
    message = _first_nested(data, "message", "errorMessage", "error_message")
    retry_after = parse_retry_after(
        _first_nested(
            data,
            "retry-after",
            "Retry-After",
            "retry_after",
            "retryAfter",
            "retryAfterSeconds",
            "retry_after_seconds",
        )
    )
    if retry_after is None:
        retry_after = _milliseconds_to_seconds(_first_nested(data, "providerRetryAfterMs"))
    details = classify_rate_limit(
        code=code if isinstance(code, str) else None,
        message=message if isinstance(message, str) else None,
        status_code=status,
        retry_after=retry_after,
    )
    if details is None:
        return status, retry_after, False
    return details.status_code, details.retry_after_seconds, True


def _milliseconds_to_seconds(value: object) -> float | None:
    """Accept only finite numeric timing metadata, at most one day."""

    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not 0 <= value <= 86_400_000:
        return None
    return value / 1000.0


def _first_nested(mapping: Mapping[object, object], *names: str) -> object | None:
    """Find a small metadata field through bounded response wrappers.

    Provider SDKs have used both ``error.headers`` and
    ``response.details.headers`` over time.  Search at most two wrapper levels
    and never walk arbitrary payloads, keeping malformed notifications bounded.
    """

    wanted = {name.casefold() for name in names}
    seen: set[int] = set()

    def search(current: Mapping[object, object], depth: int) -> object | None:
        marker = id(current)
        if marker in seen:
            return None
        seen.add(marker)
        for key, value in current.items():
            if key in names or (isinstance(key, str) and key.casefold() in wanted):
                return value
        if depth >= 2:
            return None
        for key in ("error", "failure", "response", "details", "data", "headers"):
            child = current.get(key)
            if isinstance(child, Mapping):
                found = search(child, depth + 1)
                if found is not None:
                    return found
        return None

    return search(mapping, 0)


def _bounded_status(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599:
        return value
    if isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
        return parsed if 100 <= parsed <= 599 else None
    return None


def _bounded_positive_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return min(value, _MAX_COMPACTION_MEASUREMENT)
    if isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
        return min(parsed, _MAX_COMPACTION_MEASUREMENT) if parsed > 0 else None
    return None
