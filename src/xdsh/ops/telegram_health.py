"""Container health check for the XDSH Telegram polling heartbeat."""

from __future__ import annotations

import argparse
import math
import os
from collections.abc import Sequence
from pathlib import Path

from xdsh.core.product_control import safe_exception_text
from xdsh.telegram.state import TelegramState, TelegramStateError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state-root",
        default=os.environ.get("XDSH_STATE_ROOT", "/state"),
    )
    parser.add_argument("--max-age", type=float, default=90.0)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    if args.max_age <= 0:
        raise SystemExit("max heartbeat age must be positive")
    try:
        state = TelegramState(Path(args.state_root))
        age = state.heartbeat_age()
        poll_age = state.poll_heartbeat_age()
    except TelegramStateError as error:
        raise SystemExit(safe_exception_text(error, max_chars=400)) from None
    if not math.isfinite(age) or age < -5.0 or age > args.max_age:
        raise SystemExit(f"Telegram heartbeat age is outside the healthy range: {age:.1f}s")
    if poll_age is not None and (
        not math.isfinite(poll_age) or poll_age < -5 or poll_age > args.max_age
    ):
        raise SystemExit(
            f"Telegram polling is stale: {poll_age:.1f}s; process heartbeat is insufficient"
        )
    print(f"Telegram heartbeat healthy: {age:.1f}s")


if __name__ == "__main__":
    main()
