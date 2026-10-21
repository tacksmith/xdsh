import sqlite3
from pathlib import Path

import pytest

from xdsh.core.delivery import (
    DeliveryLedgerError,
    DurableDeliveryLedger,
    DurableTurn,
    HandoffKind,
    QueuedTask,
)


def _pending(
    root: Path, *, limit: int = 10
) -> tuple[DurableDeliveryLedger, DurableTurn, QueuedTask]:
    ledger = DurableDeliveryLedger(root, max_queue_items=limit)
    active = ledger.begin_direct(
        chat_id=42,
        session_key="main",
        session_id="test-session",
        prompt="A: original goal",
    )
    queued = ledger.enqueue(
        chat_id=42,
        session_key="main",
        prompt="B: amended requirement",
        parent_turn_id=active.id,
    )
    return ledger, active, queued


@pytest.mark.parametrize("kind", ["merge", "interrupt", "replace"])
def test_handoff_reservation_is_not_runnable_until_commit(
    tmp_path: Path, kind: HandoffKind
) -> None:
    ledger, active, queued = _pending(tmp_path)
    ledger.prepare_handoff(
        42,
        queued.id,
        parent_turn_id=active.id,
        kind=kind,
        resume_context="bounded partial progress",
    )
    assert ledger.queued(42) == ()
    with pytest.raises(DeliveryLedgerError):
        ledger.begin_queued(queue_id=queued.id, session_id="test-session")
    ledger.finish(active.id, outcome="cancelled", partial_text="partial", messages=())
    ledger.commit_handoff(42, queued.id, resume_context="final checkpoint")
    items = ledger.queued(42)
    assert items[0].id == queued.id
    assert len(items) == (2 if kind == "interrupt" else 1)
    if kind == "merge":
        assert active.prompt in items[0].prompt and queued.prompt in items[0].prompt
        assert items[0].resume_context == "final checkpoint"
    if kind == "interrupt":
        assert items[1].prompt == active.prompt
        assert items[1].resume_context == "final checkpoint"
    assert DurableDeliveryLedger(tmp_path).queue_choice_consumed(42, queued.id)


def test_handoff_checks_parent_capacity_and_length_before_mutating(tmp_path: Path) -> None:
    ledger, active, queued = _pending(tmp_path, limit=1)
    with pytest.raises(DeliveryLedgerError):
        ledger.prepare_handoff(
            42, queued.id, parent_turn_id="aaaaaaaa", kind="merge", resume_context="partial"
        )
    with pytest.raises(DeliveryLedgerError):
        ledger.prepare_handoff(
            42, queued.id, parent_turn_id=active.id, kind="interrupt", resume_context="partial"
        )
    assert ledger.queued(42) == (queued,)
    ledger.prepare_handoff(
        42, queued.id, parent_turn_id=active.id, kind="merge", resume_context="partial"
    )
    ledger.abort_handoff(42, queued.id)
    assert ledger.queued(42) == (queued,)
    assert not ledger.queue_choice_consumed(42, queued.id)


def test_handoff_crash_is_visible_and_never_replayed(tmp_path: Path) -> None:
    ledger, active, queued = _pending(tmp_path)
    ledger.prepare_handoff(
        42, queued.id, parent_turn_id=active.id, kind="interrupt", resume_context="partial"
    )
    reloaded = DurableDeliveryLedger(tmp_path)
    reloaded.reconcile()
    assert reloaded.queued(42) == ()
    assert reloaded.held_handoffs(42)[0].state == "unknown"
    assert any(queued.id in item.text for item in reloaded.pending_outbox(42))
    reloaded.drop_queued(42, queued.id)
    assert reloaded.held_handoffs(42) == ()


def test_handoff_does_not_truncate_instructions(tmp_path: Path) -> None:
    ledger = DurableDeliveryLedger(tmp_path)
    active = ledger.begin_direct(chat_id=42, session_key="main", session_id="s", prompt="A" * 8000)
    queued = ledger.enqueue(
        chat_id=42, session_key="main", prompt="B" * 8000, parent_turn_id=active.id
    )
    with pytest.raises(DeliveryLedgerError):
        ledger.prepare_handoff(
            42, queued.id, parent_turn_id=active.id, kind="merge", resume_context="partial"
        )
    assert ledger.queued(42) == (queued,)


def test_handoff_commit_failure_keeps_both_items_held(tmp_path: Path) -> None:
    ledger, active, queued = _pending(tmp_path)
    ledger.prepare_handoff(
        42, queued.id, parent_turn_id=active.id, kind="interrupt", resume_context="partial",
    )
    ledger.finish(active.id, outcome="cancelled", partial_text="partial", messages=())
    with sqlite3.connect(ledger.path) as connection:
        connection.execute(
            "CREATE TRIGGER reject_commit BEFORE UPDATE ON queue_items "
            "WHEN NEW.status='queued' BEGIN SELECT RAISE(ABORT, 'injected storage failure'); END"
        )
    with pytest.raises(sqlite3.Error):
        ledger.commit_handoff(42, queued.id, resume_context="final checkpoint")
    assert ledger.queued(42) == ()
    ledger.reconcile()
    assert ledger.held_handoffs(42)[0].state == "unknown"
    notices = len(ledger.pending_outbox(42))
    ledger.reconcile()
    assert len(ledger.pending_outbox(42)) == notices


def test_handoff_reservation_counts_toward_capacity(tmp_path: Path) -> None:
    ledger, active, queued = _pending(tmp_path, limit=2)
    ledger.prepare_handoff(
        42, queued.id, parent_turn_id=active.id, kind="interrupt", resume_context="partial",
    )
    with pytest.raises(DeliveryLedgerError):
        ledger.enqueue(chat_id=42, session_key="main", prompt="third item")


def test_handoff_cannot_commit_while_parent_is_running(tmp_path: Path) -> None:
    ledger, active, queued = _pending(tmp_path)
    ledger.prepare_handoff(
        42, queued.id, parent_turn_id=active.id, kind="merge", resume_context="partial",
    )
    with pytest.raises(DeliveryLedgerError):
        ledger.commit_handoff(42, queued.id, resume_context="partial")
    assert ledger.queued(42) == ()
