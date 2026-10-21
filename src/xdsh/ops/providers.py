"""Interactive terminal provider setup with masked credential entry."""

from __future__ import annotations

import argparse
import getpass
import os
from collections.abc import Callable, Sequence
from pathlib import Path

from xdsh.core.providers import ProviderError, ProviderRegistry, ProviderWizard


def run_wizard(
    registry: ProviderRegistry,
    *,
    read: Callable[[str], str] = input,
    read_secret: Callable[[str], str] = getpass.getpass,
    write: Callable[[str], None] = print,
) -> bool:
    """One bounded terminal transaction; escape with Ctrl-C or answer no."""
    wizard = ProviderWizard()
    write(
        "XDSH · Add model connection / 新增模型接入\n"
        "OpenAI-compatible: vLLM, Ollama (/v1), OpenRouter and custom gateways.\n"
        "Anthropic-compatible: Claude, DeepSeek, ARK Coding Plan and custom gateways."
    )
    while wizard.step < 5:
        try:
            value = (read_secret if wizard.step == 4 else read)(wizard.prompt + "\n> ")
            wizard.advance(value)
        except ProviderError as error:
            write(str(error))
    route = wizard.connection()
    write(route.summary())
    if read("Save this connection and authorize use of its credential? [y/N] ").lower() != "y":
        write("Cancelled; nothing saved.")
        return False
    registry.add(route, approved=True, credential=wizard.secret)
    write(
        "Saved. The gateway reloads routes automatically. Use /model to select it.\n"
        "Connection and tool support still need a live probe; saved does not mean verified."
    )
    return True


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="xdsh-providers")
    parser.add_argument(
        "--root",
        default=os.environ.get(
            "XDSH_PROVIDERS_ROOT",
            "~/.xdsh/config/providers",
        ),
    )
    parser.add_argument("command", choices=("tui", "list"), nargs="?", default="tui")
    args = parser.parse_args(argv)
    try:
        registry = ProviderRegistry(Path(args.root).expanduser())
        if args.command == "list":
            for route in registry.connections():
                print(route.summary() + "\n")
        else:
            run_wizard(registry)
        return 0
    except (EOFError, KeyboardInterrupt):
        print("Cancelled; no new connection activated.")
        return 130
    except (ProviderError, OSError):
        print("Provider setup failed; check the connection and owner-only configuration files.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
