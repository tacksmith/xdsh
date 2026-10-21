from __future__ import annotations

import asyncio

from xdsh.ops.rate_limit_probe import run_probe


def test_synthetic_rate_limit_probe_is_bounded_and_content_free() -> None:
    result = asyncio.run(run_probe())

    assert result["schema_version"] == 1
    assert result["ok"] is True
    cases = result["cases"]
    assert isinstance(cases, dict)
    bounded = cases["bounded_storm"]
    assert isinstance(bounded, dict)
    assert bounded["ok"] is True
    assert bounded["delegate_calls"] == 1
    assert bounded["cancel_calls"] == 1
    assert bounded["partial_text_chars"] == 7
    assert bounded["terminal"] == "rate_limited"
    assert bounded["retry_exhausted"] is True
    assert 0 <= bounded["cooldown_cap_seconds"] <= 0.05
    queue = cases["queue_timeout"]
    assert isinstance(queue, dict)
    assert queue == {
        "ok": True,
        "delegate_calls": 0,
        "terminal": "rate_limited",
        "queue_error": "RATE_LIMIT_QUEUE_TIMEOUT",
    }
