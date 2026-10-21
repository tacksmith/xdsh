"""Run one optional channel webhook on a loopback listener."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shlex
import signal
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.parse import urlsplit

from xdsh.channels import ChannelAgentBridge, ChannelAgentConfig, ChannelRouter
from xdsh.channels.config import build_channel_adapter, channel_environment_status
from xdsh.channels.http import ChannelHttpServer
from xdsh.channels.router import CHANNEL_CATALOG
from xdsh.core.execution_policy import (
    DEFAULT_MAX_PARALLEL_TOOL_CALLS,
    validate_parallel_tool_limit,
)
from xdsh.core.product_control import safe_exception_text
from xdsh.core.rate_limit import RateLimitedRunner, rate_limit_policy_from_env
from xdsh.core.runner import AgentRunner
from xdsh.ops.bootstrap import BootstrapError, parse_env_file, validate_xdsh_root
from xdsh.runners.dsh import _DEFAULT_CORDIS, DshRunner, DshRunnerConfig

LOGGER = logging.getLogger(__name__)
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        environment = _load_environment(args.env_file)
        _check_channel_ready(args.channel, environment)
        if args.check:
            _check_configuration(args, environment)
            print(
                json.dumps(
                    {
                        "schema_version": 1,
                        "channel": args.channel,
                        "bind": "127.0.0.1",
                        "port": args.port,
                        "ready": True,
                        "note": "configuration-only check; no network, DSH, or provider call",
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
        from xdsh.ops.keep_awake import KeepAwake

        with KeepAwake(environment.get("XDSH_KEEP_AWAKE", "off")):
            asyncio.run(_serve(args, environment))
    except (BootstrapError, ValueError, OSError, RuntimeError) as error:
        print(
            f"xdsh-channel-serve: configuration failed ({type(error).__name__})",
            file=sys.stderr,
        )
        LOGGER.debug(
            "channel server configuration detail: %s",
            safe_exception_text(error, max_chars=400),
        )
        return 2
    return 0


async def _serve(args: argparse.Namespace, environment: Mapping[str, str]) -> None:
    # The explicit environment mapping is copied into the process only for
    # child DSH inheritance; values are never printed or persisted.
    os.environ.update(environment)
    runner = _build_runner(args, environment)
    adapter = build_channel_adapter(args.channel, environment)
    router = ChannelRouter((adapter,))
    bridge = ChannelAgentBridge(
        runner,
        ChannelAgentConfig(
            state_root=_path(environment, "XDSH_CHANNEL_STATE_ROOT", "~/.xdsh/channel-state"),
            workspace_root=_path(
                environment,
                "XDSH_CHANNEL_WORKSPACE_ROOT",
                "~/.xdsh/channel-workspaces",
            ),
            default_model=environment.get("XDSH_LITELLM_MODEL", "ark-code-latest"),
        ),
    )
    server = ChannelHttpServer(
        router,
        channel=args.channel,
        message_handler=bridge.handle,
        bind="127.0.0.1",
        port=args.port,
        path=args.path,
        max_inflight=args.max_inflight,
    )
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass
    LOGGER.info("XDSH channel listener ready on 127.0.0.1:%s", args.port)
    try:
        await server.serve(stop_event)
    finally:
        await runner.close()


def _build_runner(args: argparse.Namespace, environment: Mapping[str, str]) -> AgentRunner:
    runtime_text = args.runtime_command or environment.get("XDSH_DSH_RUNTIME_COMMAND", "")
    if not runtime_text.strip():
        raise ValueError("XDSH_DSH_RUNTIME_COMMAND is required")
    try:
        runtime_command = tuple(shlex.split(runtime_text))
    except ValueError as error:
        raise ValueError("DSH runtime command has invalid quoting") from error
    if not runtime_command:
        raise ValueError("DSH runtime command is empty")
    model = args.model or environment.get("XDSH_LITELLM_MODEL", "ark-code-latest")
    provider = args.provider or environment.get("XDSH_DSH_PROVIDER", "litellm")
    dsh_home = _path(environment, "XDSH_CHANNEL_DSH_HOME", "~/.xdsh/channel-dsh-home")
    rate_limit_policy = rate_limit_policy_from_env(environment)
    try:
        max_parallel_tool_calls = validate_parallel_tool_limit(
            int(
                environment.get(
                    "XDSH_MAX_PARALLEL_TOOL_CALLS",
                    str(DEFAULT_MAX_PARALLEL_TOOL_CALLS),
                )
            )
        )
    except ValueError as error:
        raise ValueError(f"invalid XDSH_MAX_PARALLEL_TOOL_CALLS: {error}") from error
    return RateLimitedRunner(
        DshRunner(
            DshRunnerConfig(
                dsh_home=dsh_home,
                model=model,
                provider=provider,
                cordis=_DEFAULT_CORDIS,
                runtime_command=runtime_command,
                max_parallel_tool_calls=max_parallel_tool_calls,
                rate_limit_policy=rate_limit_policy,
            )
        ),
        provider=provider,
        policy=rate_limit_policy,
    )


def _check_configuration(args: argparse.Namespace, environment: Mapping[str, str]) -> None:
    _build_runner(args, environment)
    base_url = environment.get("XDSH_LITELLM_BASE_URL", "")
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in _LOOPBACK:
        raise ValueError("XDSH_LITELLM_BASE_URL must be loopback-only")
    if not environment.get("XDSH_LITELLM_API_KEY", "").strip():
        raise ValueError("XDSH_LITELLM_API_KEY is required")


def _check_channel_ready(name: str, environment: Mapping[str, str]) -> None:
    statuses = {item["name"]: item for item in channel_environment_status(environment)}
    selected = statuses.get(name.casefold())
    if not isinstance(selected, dict):
        raise ValueError(f"unknown channel: {name}")
    missing = selected.get("missing_required")
    if isinstance(missing, tuple) and missing:
        raise ValueError(f"missing channel settings: {', '.join(str(item) for item in missing)}")


def _load_environment(env_file: str | None) -> dict[str, str]:
    values = dict(os.environ)
    if env_file:
        path = Path(env_file).expanduser()
        values.update(parse_env_file(path))
    return values


def _path(environment: Mapping[str, str], name: str, default: str) -> Path:
    value = Path(environment.get(name, default)).expanduser()
    return validate_xdsh_root(value)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="xdsh-channel-serve")
    parser.add_argument(
        "--channel",
        required=True,
        choices=tuple(
            item.name for item in CHANNEL_CATALOG if item.transport == "webhook"
        ),
    )
    parser.add_argument("--env-file")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--path")
    parser.add_argument("--model")
    parser.add_argument("--provider")
    parser.add_argument("--runtime-command")
    parser.add_argument("--max-inflight", type=int, default=32)
    parser.add_argument("--check", action="store_true")
    return parser


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
