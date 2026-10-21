from __future__ import annotations

from dataclasses import dataclass

import pytest

from xdsh.core.events import (
    ActivityEvent,
    ActivityPhase,
    ArtifactEvent,
    CompactionAction,
    CompactionEvent,
    ErrorEvent,
    EventKind,
    ReasoningEvent,
    RetryEvent,
    SubagentEvent,
    TextEvent,
    TodoEvent,
    ToolResultEvent,
    ToolStartEvent,
    UsageEvent,
)
from xdsh.runners.dsh_events import DshEventNormalizer


@dataclass(frozen=True, slots=True)
class Notification:
    method: str
    payload: dict[str, object]


def _notification(
    event_type: str,
    data: dict[str, object],
    *,
    seq: int = 7,
) -> Notification:
    return Notification(
        method="session.event",
        payload={
            "sessionId": "internal-session",
            "event": {"type": event_type, "seq": seq, "time": 1234, "data": data},
        },
    )


def test_gateway_incomplete_marker_is_normalized_only_from_structured_error() -> None:
    normalizer = DshEventNormalizer(session_id="product", dsh_session_id="internal-session")
    events = normalizer.feed(_notification("turn/end", {"reason": {
        "kind": "error", "error": {"code": "UNKNOWN", "message":
        "[XDSH_STREAM_INCOMPLETE] Provider stream ended before completion"}}}))
    assert len(events) == 1
    assert isinstance(events[0], ErrorEvent)
    assert events[0].code == "UPSTREAM_STREAM_INCOMPLETE"
    assert not events[0].retryable


def test_observed_model_stream_idle_is_distinct_and_not_task_replayable() -> None:
    normalizer = DshEventNormalizer(session_id="product", dsh_session_id="internal-session")
    events = normalizer.feed(_notification("turn/end", {"reason": {
        "kind": "error", "error": {"code": "TIMEOUT", "message":
        "pi-ai stream idle timeout after 120000ms"}}}))
    assert isinstance(events[0], ErrorEvent)
    assert events[0].code == "MODEL_STREAM_IDLE"
    assert not events[0].retryable


def test_tool_timeout_text_does_not_become_model_stream_idle() -> None:
    normalizer = DshEventNormalizer(session_id="product", dsh_session_id="internal-session")
    events = normalizer.feed(_notification("turn/end", {"reason": {
        "kind": "error", "error": {"code": "TIMEOUT", "message": "tool deadline"}}}))
    assert isinstance(events[0], ErrorEvent)
    assert events[0].code == "TIMEOUT"
    assert not events[0].retryable


def test_gateway_stream_idle_marker_preserves_specific_recovery_without_replay() -> None:
    normalizer = DshEventNormalizer(session_id="product", dsh_session_id="internal-session")
    events = normalizer.feed(_notification("turn/end", {"reason": {
        "kind": "error", "error": {"code": "UNKNOWN", "message":
        "[XDSH_PROVIDER_STREAM_IDLE] Provider event wait timed out"}}}))
    assert isinstance(events[0], ErrorEvent)
    assert events[0].code == "MODEL_STREAM_IDLE"
    assert not events[0].retryable


def test_normalizes_structured_dsh_notifications() -> None:
    normalizer = DshEventNormalizer(
        session_id="product-session", dsh_session_id="internal-session"
    )

    events = [
        *normalizer.feed(
            _notification("llm/retry", {"provider": "litellm-anthropic"}, seq=1)
        ),
        *normalizer.feed(
            _notification(
                "assistant/chunk",
                {"turn": 1, "step": 1, "chunk": {"type": "text-delta", "text": "hello"}},
                seq=2,
            )
        ),
        *normalizer.feed(
            _notification(
                "assistant/chunk",
                {
                    "turn": 1,
                    "step": 1,
                    "chunk": {"type": "reasoning-delta", "text": "thinking"},
                },
                seq=3,
            )
        ),
        *normalizer.feed(
            _notification(
                "assistant/chunk",
                {
                    "turn": 1,
                    "step": 1,
                    "chunk": {
                        "type": "usage",
                        "usage": {
                            "inputTokens": 11,
                            "outputTokens": 5,
                            "cacheReadTokens": 2,
                        },
                    },
                },
                seq=4,
            )
        ),
        *normalizer.feed(
            _notification(
                "tool/call",
                {
                    "callId": "call-1",
                    "name": "write",
                    "arguments": '{"path":"result.txt","content":"done"}',
                },
                seq=5,
            )
        ),
        *normalizer.feed(
            _notification(
                "tool/result",
                {
                    "message": {
                        "source": {"callId": "call-1"},
                        "content": [
                            {
                                "type": "tool-result",
                                "toolCallId": "call-1",
                                "content": [{"type": "text", "text": "wrote result.txt"}],
                            }
                        ],
                    }
                },
                seq=6,
            )
        ),
        *normalizer.feed(
            _notification(
                "todo/write",
                {"todos": [{"content": "verify", "status": "in_progress"}]},
                seq=7,
            )
        ),
    ]

    assert [event.kind for event in events] == [
        EventKind.RETRY,
        EventKind.TEXT,
        EventKind.REASONING,
        EventKind.USAGE,
        EventKind.TOOL_START,
        EventKind.TOOL_RESULT,
        EventKind.ARTIFACT,
        EventKind.TODO,
    ]
    assert isinstance(events[0], RetryEvent)
    assert events[0].provider == "litellm-anthropic"
    assert isinstance(events[1], TextEvent) and events[1].text == "hello"
    assert isinstance(events[2], ReasoningEvent) and events[2].text == "thinking"
    assert isinstance(events[3], UsageEvent) and events[3].total_tokens == 18
    assert isinstance(events[4], ToolStartEvent) and events[4].arguments == {
        "path": "result.txt",
        "content": "done",
    }
    assert isinstance(events[5], ToolResultEvent) and events[5].content == "wrote result.txt"
    assert isinstance(events[6], ArtifactEvent) and events[6].path == "result.txt"
    assert isinstance(events[7], TodoEvent) and events[7].items[0].content == "verify"
    assert all(event.session_id == "product-session" for event in events)
    assert [event.sequence for event in events] == [1, 2, 3, 4, 5, 6, 6, 7]
    assert all(event.timestamp_ms == 1234 for event in events)


def test_tool_call_delta_emits_content_free_activity() -> None:
    normalizer = DshEventNormalizer(
        session_id="product-session", dsh_session_id="internal-session"
    )
    secret_fragment = '{"content":"must-not-cross-the-event-boundary"}'

    events = normalizer.feed(
        _notification(
            "assistant/chunk",
            {
                "turn": 1,
                "step": 2,
                "chunk": {
                    "type": "tool-call-delta",
                    "index": 0,
                    "id": "call-1",
                    "name": "write",
                    "argumentsDelta": secret_fragment,
                },
            },
        )
    )

    assert len(events) == 1
    assert isinstance(events[0], ActivityEvent)
    assert secret_fragment not in repr(events[0])
    assert events[0].session_id == "product-session"
    assert events[0].sequence == 7
    assert events[0].timestamp_ms == 1234
    assert events[0].phase is ActivityPhase.TOOL_ARGUMENTS
    assert events[0].output_characters == len(secret_fragment)
    assert events[0].has_output


def test_empty_argument_frame_is_observable_but_not_output() -> None:
    normalizer = DshEventNormalizer(session_id="product", dsh_session_id="internal-session")
    notification = _notification("assistant/chunk", {"chunk": {
        "type": "tool-call-delta", "index": 0, "id": "call-1", "argumentsDelta": "",
    }})
    events = normalizer.feed(notification)

    assert len(events) == 1
    assert isinstance(events[0], ActivityEvent)
    assert events[0].phase is ActivityPhase.TOOL_ARGUMENTS
    assert events[0].output_characters == 0
    assert not events[0].has_output
    assert normalizer.feed(notification) == ()


@pytest.mark.parametrize("count", [-1, True, 1.5, 2**63])
def test_activity_metadata_rejects_invalid_character_counts(count: object) -> None:
    from typing import cast

    with pytest.raises(ValueError, match="character count"):
        ActivityEvent(session_id="test", output_characters=cast(int, count))


def test_every_structured_todo_write_preserves_the_latest_step_state() -> None:
    normalizer = DshEventNormalizer(
        session_id="product-session",
        dsh_session_id="internal-session",
    )

    first = normalizer.feed(
        _notification(
            "todo/write",
            {
                "todos": [
                    {"content": "inspect", "status": "in_progress"},
                    {"content": "change", "status": "pending"},
                ],
            },
            seq=7,
        )
    )
    second = normalizer.feed(
        _notification(
            "todo/write",
            {
                "todos": [
                    {"content": "inspect", "status": "completed"},
                    {"content": "change", "status": "in_progress"},
                ],
            },
            seq=8,
        )
    )

    assert isinstance(first[0], TodoEvent)
    assert isinstance(second[0], TodoEvent)
    assert tuple(item.status for item in first[0].items) == ("in_progress", "pending")
    assert tuple(item.status for item in second[0].items) == ("completed", "in_progress")


def test_malformed_chunk_does_not_advance_sequence_watermark() -> None:
    normalizer = DshEventNormalizer(session_id="product", dsh_session_id="internal-session")
    malformed = _notification("assistant/chunk", {"chunk": {
        "type": "tool-call-delta", "index": "invalid", "id": "call-1",
        "argumentsDelta": "ignored",
    }}, seq=1000)
    valid = _notification("assistant/chunk", {"chunk": {
        "type": "text-delta", "text": "accepted",
    }}, seq=2)

    assert normalizer.feed(malformed) == ()
    events = normalizer.feed(valid)
    assert len(events) == 1
    assert isinstance(events[0], TextEvent)
    assert events[0].text == "accepted"
    assert events[0].sequence == 2
    assert normalizer.feed(valid) == ()


def test_malformed_tool_call_delta_cannot_keep_a_run_alive() -> None:
    normalizer = DshEventNormalizer(
        session_id="product-session", dsh_session_id="internal-session"
    )

    assert normalizer.feed(
        _notification(
            "assistant/chunk",
            {
                "chunk": {
                    "type": "tool-call-delta",
                    "index": 0,
                    "argumentsDelta": "missing-call-id",
                }
            },
        )
    ) == ()


def test_out_of_order_notification_cannot_overwrite_newer_sequence() -> None:
    normalizer = DshEventNormalizer(
        session_id="product-session", dsh_session_id="internal-session"
    )

    current = normalizer.feed(
        _notification(
            "assistant/chunk",
            {"chunk": {"type": "text-delta", "text": "current"}},
        )
    )
    late = normalizer.feed(
        Notification(
            method="session.event",
            payload={
                "sessionId": "internal-session",
                "event": {
                    "type": "assistant/chunk",
                    "seq": 6,
                    "data": {"chunk": {"type": "text-delta", "text": "late"}},
                },
            },
        )
    )

    assert len(current) == 1
    assert isinstance(current[0], TextEvent)
    assert current[0].text == "current"
    assert late == ()


def test_duplicate_or_conflicting_sequence_is_ignored() -> None:
    normalizer = DshEventNormalizer(
        session_id="product-session", dsh_session_id="internal-session"
    )

    def notification(text: str, sequence: int) -> Notification:
        return Notification(
            method="session.event",
            payload={
                "sessionId": "internal-session",
                "event": {
                    "type": "assistant/chunk",
                    "seq": sequence,
                    "data": {"chunk": {"type": "text-delta", "text": text}},
                },
            },
        )

    first = normalizer.feed(notification("first", 12))
    duplicate = normalizer.feed(notification("first", 12))
    conflicting = normalizer.feed(notification("different", 12))
    later = normalizer.feed(notification("later", 13))

    assert [event.text for event in first if isinstance(event, TextEvent)] == ["first"]
    assert duplicate == ()
    assert conflicting == ()
    assert [event.text for event in later if isinstance(event, TextEvent)] == ["later"]


def test_runtime_generation_rejects_missing_or_retired_marker_before_sequence() -> None:
    normalizer = DshEventNormalizer(
        session_id="product-session",
        dsh_session_id="internal-session",
        runtime_generation="current-host:1",
    )
    notification = _notification(
        "assistant/chunk", {"chunk": {"type": "text-delta", "text": "current"}}
    )
    retired = Notification(
        method="session.event",
        payload={
            "sessionId": "internal-session",
            "event": {
                "type": "assistant/chunk",
                "seq": 999,
                "data": {"chunk": {"type": "text-delta", "text": "retired"}},
            },
        },
    )
    assert normalizer.feed(notification) == ()
    assert normalizer.feed(retired, runtime_generation="retired-host:1") == ()
    events = normalizer.feed(notification, runtime_generation="current-host:1")
    assert len(events) == 1
    assert isinstance(events[0], TextEvent)
    assert events[0].text == "current"
    assert events[0].sequence == 7
    assert events[0].runtime_generation == "current-host:1"
    # Matching provenance must not bypass session isolation.
    other_session = Notification(
        method="session.event",
        payload={**notification.payload, "sessionId": "other-session"},
    )
    assert normalizer.feed(other_session, runtime_generation="current-host:1") == ()


def test_runtime_generation_fences_subagent_notifications() -> None:
    normalizer = DshEventNormalizer(
        session_id="product-session",
        dsh_session_id="internal-session",
        runtime_generation="current-host:1",
    )
    for method in ("subagent.started", "subagent.finished"):
        notification = Notification(
            method=method,
            payload={
                "parentSessionId": "internal-session",
                "childSessionId": "child",
                "status": "completed",
            },
        )
        assert normalizer.feed(notification, runtime_generation="retired-host:1") == ()
        events = normalizer.feed(notification, runtime_generation="current-host:1")
        assert len(events) == 1
        assert isinstance(events[0], SubagentEvent)
        assert events[0].runtime_generation == "current-host:1"


def test_accepts_future_json_rpc_mapping_and_field_aliases() -> None:
    normalizer = DshEventNormalizer(session_id="product", dsh_session_id="inner")

    events = normalizer.feed(
        {
            "method": "session/event",
            "params": {
                "session_id": "inner",
                "event": {
                    "eventType": "assistant.chunk",
                    "sequence": 9,
                    "timestamp_ms": 5678,
                    "payload": {
                        "chunk": {"type": "text-delta", "text": "future"}
                    },
                },
            },
        }
    )

    assert len(events) == 1
    assert isinstance(events[0], TextEvent)
    assert events[0].text == "future"
    assert events[0].sequence == 9
    assert events[0].timestamp_ms == 5678


def test_accepts_future_notification_name_data_and_event_kind_aliases() -> None:
    normalizer = DshEventNormalizer(session_id="product", dsh_session_id="inner")

    events = normalizer.feed(
        {
            "name": "session.event",
            "data": {
                "session": "inner",
                "notification": {
                    "kind": "assistant.chunk",
                    "sequence": 10,
                    "data": {
                        "chunk": {"type": "text-delta", "text": "additive"}
                    },
                },
            },
        }
    )

    assert len(events) == 1
    assert isinstance(events[0], TextEvent)
    assert events[0].text == "additive"
    assert events[0].sequence == 10


def test_accepts_future_compaction_action_names() -> None:
    normalizer = DshEventNormalizer(session_id="product", dsh_session_id="inner")

    events = [
        *normalizer.feed(
            {
                "method": "session.event",
                "payload": {
                    "sessionId": "inner",
                    "event": {"type": "compaction/begin", "data": {}},
                },
            }
        ),
        *normalizer.feed(
            {
                "method": "session.event",
                "payload": {
                    "sessionId": "inner",
                    "event": {"type": "compaction/complete", "data": {}},
                },
            }
        ),
    ]

    assert [event.action for event in events if isinstance(event, CompactionEvent)] == [
        CompactionAction.START,
        CompactionAction.END,
    ]


def test_deduplicates_message_fallback_after_stream_chunks() -> None:
    normalizer = DshEventNormalizer(session_id="product", dsh_session_id="internal-session")
    normalizer.feed(
        _notification(
            "assistant/chunk",
            {"turn": 2, "step": 3, "chunk": {"type": "text-delta", "text": "streamed"}},
            seq=7,
        )
    )
    events = normalizer.feed(
        _notification(
            "assistant/message",
            {
                "turn": 2,
                "step": 3,
                "message": {"content": [{"type": "text", "text": "complete"}]},
                "usage": {"inputTokens": 3, "outputTokens": 1},
            },
            seq=8,
        )
    )

    assert len(events) == 1
    assert isinstance(events[0], UsageEvent)


def test_maps_turn_error_and_ignores_other_sessions() -> None:
    normalizer = DshEventNormalizer(session_id="product", dsh_session_id="internal-session")
    other = Notification(
        method="session.event",
        payload={
            "sessionId": "subagent-or-other",
            "event": {"type": "assistant/chunk", "data": {}},
        },
    )
    assert normalizer.feed(other) == ()

    events = normalizer.feed(
        _notification(
            "turn/end",
            {
                "reason": {
                    "kind": "error",
                    "error": {"code": "RATE_LIMIT", "message": "try later"},
                }
            },
        )
    )
    assert len(events) == 1
    assert isinstance(events[0], ErrorEvent)
    assert events[0].retryable is True
    assert normalizer.finish_reason == "error"


def test_maps_nested_rate_limit_metadata_and_retry_after() -> None:
    normalizer = DshEventNormalizer(
        session_id="product",
        dsh_session_id="internal-session",
    )
    event = normalizer.feed(
        _notification(
            "llm/retry",
            {
                "provider": "litellm-anthropic",
                "error": {
                    "code": "RATE_LIMIT",
                    "status": 429,
                    "headers": {"Retry-After": "4"},
                },
                "attempt": 2,
                "maxAttempts": 3,
            },
        )
    )[0]

    assert isinstance(event, RetryEvent)
    assert event.rate_limited is True
    assert event.status_code == 429
    assert event.retry_after_seconds == 4.0
    assert event.attempt == 2
    assert event.max_attempts == 3


def test_maps_observed_rc2_retry_failure_without_double_counting_started() -> None:
    normalizer = DshEventNormalizer(session_id="product", dsh_session_id="internal-session")
    payload = {
        "provider": "litellm", "retryId": "episode-1", "turn": 1, "step": 79,
        "retry": 2, "maxRetries": 3, "delayMs": 1866.0,
        "failure": {"code": "RATE_LIMIT", "message": "Provider rate limit reached"},
    }
    event = normalizer.feed(_notification("llm/retry", payload, seq=1))[0]
    assert isinstance(event, RetryEvent)
    assert event.rate_limited and event.status_code == 429
    assert event.attempt == 2 and event.max_attempts == 3
    # The runtime's scheduled backoff is not a provider Retry-After header.
    assert event.retry_after_seconds is None
    assert event.retry_delay_seconds == 1.866
    assert normalizer.feed(_notification("llm/retry-started", payload, seq=2)) == ()


@pytest.mark.parametrize("code", ["TIMEOUT", "TRANSPORT", "SERVER", "EMPTY_RESPONSE"])
def test_native_transient_retry_exposes_only_stable_failure_code(code: str) -> None:
    normalizer = DshEventNormalizer(session_id="product", dsh_session_id="internal-session")
    event = normalizer.feed(_notification("llm/retry", {
        "retry": 2, "maxRetries": 5, "delayMs": 10000,
        "failure": {"code": code, "message": "api_key=private-value"},
    }))[0]
    assert isinstance(event, RetryEvent) and event.failure_code == code
    assert event.attempt == 2 and not event.rate_limited
    assert "private-value" not in repr(event)


def test_retry_resets_message_dedup_but_preserves_each_attempt_usage() -> None:
    normalizer = DshEventNormalizer(session_id="product", dsh_session_id="internal-session")
    common: dict[str, object] = {"turn": 1, "step": 2}
    first = normalizer.feed(_notification("assistant/chunk", {
        **common, "chunk": {"type": "usage", "usage": {"inputTokens": 20, "outputTokens": 2}},
    }, seq=1))
    normalizer.feed(_notification("llm/retry", {**common, "retry": 1,
                    "failure": {"code": "TIMEOUT"}}, seq=2))
    second = normalizer.feed(_notification("assistant/message", {
        **common, "message": {"content": [{"type": "text", "text": "done"}]},
        "usage": {"inputTokens": 25, "outputTokens": 3},
    }, seq=3))
    usages = [e for e in (*first, *second) if isinstance(e, UsageEvent)]
    assert [e.input_tokens for e in usages] == [20, 25]


def test_native_retry_after_milliseconds_are_not_treated_as_seconds() -> None:
    normalizer = DshEventNormalizer(session_id="product", dsh_session_id="internal-session")
    event = normalizer.feed(_notification("llm/retry", {
        "delayMs": 90_000, "retry": 1, "maxRetries": 5,
        "failure": {"code": "RATE_LIMIT", "providerRetryAfterMs": 90_000},
    }))[0]
    assert isinstance(event, RetryEvent)
    assert event.retry_after_seconds == 90
    assert event.retry_delay_seconds == 90


@pytest.mark.parametrize("value", [True, -1, float("nan"), float("inf"), 1e30, "bad"])
def test_invalid_native_retry_durations_are_ignored(value: object) -> None:
    normalizer = DshEventNormalizer(session_id="product", dsh_session_id="internal-session")
    event = normalizer.feed(_notification("llm/retry", {
        "delayMs": value,
        "failure": {"code": "RATE_LIMIT", "providerRetryAfterMs": value},
    }))[0]
    assert isinstance(event, RetryEvent)
    assert event.retry_delay_seconds is None
    assert event.retry_after_seconds is None


def test_turn_error_without_provider_code_uses_stable_dsh_code() -> None:
    normalizer = DshEventNormalizer(session_id="outer", dsh_session_id="inner")

    events = normalizer.feed(
        Notification(
            method="session.event",
            payload={
                "sessionId": "inner",
                "event": {
                    "type": "turn/end",
                    "data": {
                        "reason": {
                            "kind": "error",
                            "error": {"message": "provider did not return a code"},
                        }
                    },
                },
            },
        )
    )

    assert len(events) == 1
    assert isinstance(events[0], ErrorEvent)
    assert events[0].code == "DSH_TURN_ERROR"


def test_turn_error_notification_redacts_and_bounds_provider_details() -> None:
    normalizer = DshEventNormalizer(session_id="outer", dsh_session_id="inner")
    secret = "notification-secret-value"
    events = normalizer.feed(
        Notification(
            method="session.event",
            payload={
                "sessionId": "inner",
                "event": {
                    "type": "turn/end",
                    "data": {
                        "reason": {
                            "kind": "error",
                            "error": {
                                "code": f"gateway\nTOKEN={secret}",
                                "message": (
                                    f'upstream {{"api_key":"{secret}"}}\x00'
                                    + "x" * 5_000
                                ),
                            },
                        }
                    },
                },
            },
        )
    )

    assert len(events) == 1
    assert isinstance(events[0], ErrorEvent)
    assert len(events[0].message) <= 4_000
    assert len(events[0].code) <= 120
    assert secret not in events[0].message
    assert secret not in events[0].code
    assert "\x00" not in events[0].message
    assert "\n" not in events[0].code


def test_structured_bash_meta_marks_nonzero_and_sandbox_denial_as_errors() -> None:
    normalizer = DshEventNormalizer(session_id="outer", dsh_session_id="internal-session")
    normalizer.feed(
        _notification(
            "tool/call",
            {
                "callId": "bash-1",
                "name": "bash",
                "arguments": '{"command":"touch /outside"}',
            },
            seq=7,
        )
    )

    events = normalizer.feed(
        _notification(
            "tool/result",
            {
                "message": {
                    "source": {"callId": "bash-1"},
                    "content": [
                        {
                            "type": "tool-result",
                            "toolCallId": "bash-1",
                            "isError": False,
                            "content": [{"type": "text", "text": "denied"}],
                        }
                    ],
                },
                "meta": {
                    "card": "terminal",
                    "exitCode": 13,
                    "sandbox": {"denied": True, "mode": "workspace-write"},
                },
            },
            seq=8,
        )
    )

    assert len(events) == 1
    assert isinstance(events[0], ToolResultEvent)
    assert events[0].is_error is True


def test_normalizes_official_subagent_lifecycle_notifications() -> None:
    normalizer = DshEventNormalizer(session_id="outer", dsh_session_id="root-session")

    started = normalizer.feed(
        Notification(
            method="subagent.started",
            payload={
                "parentSessionId": "root-session",
                "childSessionId": "child-1",
            },
        )
    )
    finished = normalizer.feed(
        Notification(
            method="subagent.finished",
            payload={
                "parentSessionId": "root-session",
                "childSessionId": "child-1",
                "status": "ok",
                "stopReason": "completed",
                "lastAssistantMessage": {
                    "content": [{"type": "text", "text": "child done"}]
                },
            },
        )
    )

    assert len(started) == len(finished) == 1
    assert isinstance(started[0], SubagentEvent)
    assert started[0].action == "started"
    assert isinstance(finished[0], SubagentEvent)
    assert finished[0].action == "finished"
    assert finished[0].status == "ok"
    assert finished[0].final_text == "child done"


def test_classifies_rc2_unknown_session_collision() -> None:
    normalizer = DshEventNormalizer(session_id="outer", dsh_session_id="inner")

    events = normalizer.feed(
        Notification(
            method="session.event",
            payload={
                "sessionId": "inner",
                "event": {
                    "type": "turn/end",
                    "data": {
                        "reason": {
                            "kind": "error",
                            "error": {
                                "code": "UNKNOWN",
                                "message": (
                                    "persisted log does not match live session (id collision)"
                                ),
                            },
                        }
                    },
                },
            },
        )
    )

    assert len(events) == 1
    assert isinstance(events[0], ErrorEvent)
    assert events[0].code == "DSH_SESSION_ID_COLLISION"


def test_normalizes_native_compaction_lifecycle_without_copying_summary_text() -> None:
    normalizer = DshEventNormalizer(
        session_id="outer", dsh_session_id="internal-session"
    )

    start = normalizer.feed(
        _notification(
            "compaction/start",
            {"compactionId": "compact-1", "turn": 4},
            seq=1,
        )
    )
    prune = normalizer.feed(
        _notification(
            "compaction/prune",
            {
                "shadowedSeqs": [10, 11],
                "shadowedTokenCount": 321,
                "charsRemoved": 900,
            },
            seq=2,
        )
    )
    summary = normalizer.feed(
        _notification(
            "compaction/summary",
            {
                "compactionId": "compact-1",
                "turn": 4,
                "shadowedSeqs": [10, 11, 12],
                "shadowedTokenCount": 1_234,
                "summary": "PRIVATE SUMMARY MUST NOT ENTER THE EVENT MODEL",
                "provider": "litellm-anthropic",
                "model": "ark-code-latest",
            },
            seq=3,
        )
    )
    end = normalizer.feed(
        _notification(
            "compaction/end",
            {"compactionId": "compact-1", "turn": 4, "error": {"message": "failed"}},
            seq=4,
        )
    )

    events = [*start, *prune, *summary, *end]
    assert [event.action for event in events if isinstance(event, CompactionEvent)] == [
        CompactionAction.START,
        CompactionAction.PRUNE,
        CompactionAction.SUMMARY,
        CompactionAction.END,
    ]
    assert isinstance(start[0], CompactionEvent)
    assert start[0].compaction_id == "compact-1"
    assert isinstance(prune[0], CompactionEvent)
    assert prune[0].shadowed_items == 2
    assert prune[0].shadowed_tokens == 321
    assert prune[0].pruned_characters == 900
    assert isinstance(summary[0], CompactionEvent)
    assert summary[0].shadowed_items == 3
    assert summary[0].shadowed_tokens == 1_234
    assert not hasattr(summary[0], "summary")
    assert isinstance(end[0], CompactionEvent)
    assert end[0].error is True


def test_bounds_untrusted_native_compaction_measurements() -> None:
    normalizer = DshEventNormalizer(
        session_id="outer", dsh_session_id="internal-session"
    )
    events = normalizer.feed(
        _notification(
            "compaction/summary",
            {
                "shadowedSeqs": [0] * 200_000,
                "shadowedTokenCount": 9_999_999_999_999,
                "charsRemoved": -5,
                "compactionId": "x" * 500,
            },
        )
    )

    assert len(events) == 1
    assert isinstance(events[0], CompactionEvent)
    assert events[0].shadowed_items == 100_000
    assert events[0].shadowed_tokens == 2_000_000_000
    assert events[0].pruned_characters == 0
    assert events[0].compaction_id is not None
    assert len(events[0].compaction_id) == 200
