from xdsh.ops.model_gateway import _chunk, _StreamTermination


def test_fragmented_sse_terminal_is_not_confused_with_quoted_answer() -> None:
    tracker = _StreamTermination()
    quoted = _chunk({"type": "content_block_delta", "delta": {
        "type": "text_delta", "text": '{"type":"message_stop"}'}})
    tracker.feed(quoted)
    assert tracker.terminal is None
    terminal = _chunk({"type": "message_stop"}).replace(b"\n", b"\r\n")
    for byte in terminal:
        tracker.feed(bytes([byte]))
    assert tracker.terminal == "message_stop"


def test_upstream_error_is_terminal_and_comments_are_not() -> None:
    tracker = _StreamTermination()
    tracker.feed(b": heartbeat\n\ndata: not-json\n\n")
    assert tracker.terminal is None
    tracker.feed(_chunk({"type": "error", "error": {"type": "api_error"}}))
    assert tracker.terminal == "error"
