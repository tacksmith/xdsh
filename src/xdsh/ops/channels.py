"""Credential-free diagnostics for optional XDSH messaging channels."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Sequence

from xdsh.channels.config import channel_environment_status
from xdsh.channels.router import CHANNEL_CATALOG


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="xdsh-channels")
    parser.add_argument("command", nargs="?", choices=("list", "check"), default="list")
    parser.add_argument("--channel", action="append", dest="channels")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    statuses = list(channel_environment_status(os.environ))
    selected = set(args.channels or ())
    if selected:
        known = {descriptor.name for descriptor in CHANNEL_CATALOG}
        unknown = sorted(selected - known)
        if unknown:
            parser.error(f"unknown channel(s): {', '.join(unknown)}")
        statuses = [item for item in statuses if item["name"] in selected]
    if args.command == "check":
        payload: object = {
            "schema_version": 1,
            "command": "check",
            "channels": statuses,
            "note": "readiness checks names only; credentials and network calls are not performed",
        }
    else:
        payload = {
            "schema_version": 1,
            "command": "list",
            "channels": [
                {
                    "name": descriptor.name,
                    "label": descriptor.label,
                    "inbound": descriptor.inbound,
                    "outbound": descriptor.outbound,
                    "required_environment": descriptor.required_environment,
                    "optional_environment": descriptor.optional_environment,
                    "transport": descriptor.transport,
                    "requires_public_https": descriptor.requires_public_https,
                }
                for descriptor in CHANNEL_CATALOG
                if not selected or descriptor.name in selected
            ],
        }
    if args.as_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        _print_text(payload)
    return 0


def _print_text(payload: object) -> None:
    if not isinstance(payload, dict):
        return
    channels = payload.get("channels")
    if not isinstance(channels, list):
        return
    for item in channels:
        if not isinstance(item, dict):
            continue
        name = item.get("name", "?")
        label = item.get("label", "")
        if payload.get("command") == "check":
            ready = "ready" if item.get("ready") else "needs-credentials"
            missing = ",".join(str(value) for value in item.get("missing_required", ())) or "-"
            print(f"{name}: {ready} · {label} · missing={missing}")
        else:
            print(f"{name}: {label} · {item.get('inbound')} → {item.get('outbound')}")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
