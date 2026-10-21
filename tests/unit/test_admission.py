from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from xdsh.core.admission import BoundedAdmission


def test_busy_worker_queues_one_request_then_releases_without_exceeding_capacity() -> None:
    gate = BoundedAdmission(workers=1, pending=1, wait_seconds=0.5)
    assert gate.acquire()
    entered = threading.Event()

    def waiter() -> bool:
        entered.set()
        acquired = gate.acquire()
        if acquired:
            gate.release()
        return acquired

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(waiter)
        assert entered.wait(0.5)
        gate.release()
        assert future.result(1)
    assert gate.acquire()
    gate.release()


def test_no_queue_rejects_excess_without_leaking_capacity() -> None:
    gate = BoundedAdmission(workers=1, pending=0, wait_seconds=0.01)
    assert gate.acquire()
    assert not gate.acquire()
    gate.release()
    assert gate.acquire()
    gate.release()


def test_pending_wait_expires_without_leaking_a_slot() -> None:
    gate = BoundedAdmission(workers=1, pending=1, wait_seconds=0.01)
    assert gate.acquire()
    assert not gate.acquire()
    gate.release()
    assert gate.acquire()
    gate.release()
