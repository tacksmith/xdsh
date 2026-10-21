"""Thread-safe, bounded admission shared across short-lived gateway event loops."""

from __future__ import annotations

import math
import threading


class BoundedAdmission:
    """Admit at most workers+pending requests; queue waits never run indefinitely.

    The executing worker owns its slot until work really ends, even when an
    async caller has stopped waiting for a blocking network operation.
    """

    def __init__(self, *, workers: int, pending: int, wait_seconds: float) -> None:
        if (isinstance(workers, bool) or not isinstance(workers, int)
                or not 1 <= workers <= 16 or isinstance(pending, bool)
                or not isinstance(pending, int) or not 0 <= pending <= 32
                or not math.isfinite(wait_seconds) or not 0 < wait_seconds <= 20):
            raise ValueError("invalid bounded admission limits")
        self._admitted = threading.BoundedSemaphore(workers + pending)
        self._workers = threading.BoundedSemaphore(workers)
        self._wait_seconds = wait_seconds

    def acquire(self) -> bool:
        if not self._admitted.acquire(blocking=False):
            return False
        try:
            if self._workers.acquire(timeout=self._wait_seconds):
                return True
        except BaseException:
            self._admitted.release()
            raise
        self._admitted.release()
        return False

    def release(self) -> None:
        self._workers.release()
        self._admitted.release()
