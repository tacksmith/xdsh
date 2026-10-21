"""Check inherited macOS confinement before accepting interactive tasks.

This is not a cross-platform execution certificate. Linux/Windows runtime
readiness belongs to their deployment probes. Never fall back to unconfined
execution when the requested backend cannot initialize.
"""

from __future__ import annotations

import platform
import subprocess


def require_local_sandbox() -> None:
    """Reject a Mac service parent that cannot start a read-only Seatbelt child."""
    if platform.system() != "Darwin":
        return
    try:
        result = subprocess.run(
            [
                "/usr/bin/sandbox-exec", "-p",
                "(version 1)(allow default)(deny file-write*)",
                "/usr/bin/true",
            ],
            env={"PATH": "/usr/bin:/bin"},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
        ready = result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ready = False
    if not ready:
        raise ValueError(
            "macOS sandbox cannot initialize from this service parent. "
            "Start XDSH from a normal host terminal or an authorized host service; "
            "do not launch it inside another agent sandbox. "
            "Keep DSH workspace-write confinement enabled."
        )
