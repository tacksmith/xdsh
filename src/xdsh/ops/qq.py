"""Run the official QQ Bot outbound WebSocket gateway."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from xdsh.channels import ChannelAgentBridge, ChannelAgentConfig, ChannelError
from xdsh.channels.config import build_qq_gateway_config
from xdsh.channels.qq import QQGatewayAdapter, QQGatewayConfig, require_qq_sdk
from xdsh.core.product_control import safe_exception_text
from xdsh.ops.bootstrap import BootstrapError
from xdsh.ops.channel_server import (
    _build_runner,
    _check_configuration,
    _load_environment,
    _path,
)
from xdsh.ops.qq_setup import (
    create_qq_environment,
    onboard_qq_environment,
    read_private_environment,
)

LOGGER = logging.getLogger(__name__)
_DEFAULT_ENV_FILE = Path("~/.xdsh/config/qq.env")
_QQ_ENV_TEMPLATE = """# XDSH QQ Official Bot service. Keep this file mode 0600.
# Fill the three required QQ values from a dedicated QQ Open Platform Bot.
XDSH_QQ_APP_ID=
XDSH_QQ_APP_SECRET=
XDSH_QQ_ALLOWED_USERS=
# Optional comma-separated group OpenIDs. Group turns require both allowlists.
XDSH_QQ_ALLOWED_GROUPS=
XDSH_QQ_MARKDOWN=off
XDSH_QQ_MAX_INFLIGHT=4
XDSH_QQ_MAX_PENDING=32

# Reuse the exact runtime/model settings from an existing XDSH instance.
XDSH_DSH_RUNTIME_COMMAND=
XDSH_DSH_PROVIDER=litellm
XDSH_LITELLM_BASE_URL=http://127.0.0.1:4000
XDSH_LITELLM_API_KEY=
XDSH_LITELLM_MODEL=ark-code-latest
XDSH_CHANNEL_STATE_ROOT=~/.xdsh/channel-state
XDSH_CHANNEL_WORKSPACE_ROOT=~/.xdsh/channel-workspaces
XDSH_CHANNEL_DSH_HOME=~/.xdsh/channel-dsh-home
XDSH_CHANNEL_TASK_TIMEOUT_SECONDS=off
XDSH_KEEP_AWAKE=off
"""


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.init or args.onboard:
            target = (
                Path(args.env_file).expanduser() if args.env_file
                else _DEFAULT_ENV_FILE.expanduser()
            ).absolute()
            if args.onboard:
                require_qq_sdk()
            created = create_qq_environment(
                target, _QQ_ENV_TEMPLATE, tuple(Path(path) for path in args.from_env),
            )
            if args.onboard:
                written = asyncio.run(onboard_qq_environment(
                    target,
                    on_url=lambda url: print(
                        f"Use QQ to open/scan this official binding link:\n{url}", flush=True,
                    ),
                ))
                print(json.dumps({
                    "schema_version": 1, "command": "onboard", "path": str(target),
                    "configured": list(written), "credentials_printed": False,
                    "note": "binding saved; next run xdsh-qq --env-file <path> --check",
                }, sort_keys=True))
                return 0
            print(
                json.dumps(
                    {
                        "schema_version": 1,
                        "command": "init",
                        "created": created,
                        "path": str(target),
                        "note": (
                            "private env prepared; --from-env imports only runtime settings; "
                            "add QQ credentials locally or run --onboard, "
                            "then run xdsh-qq --check"
                        ),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
        environment = _load_qq_environment(args.env_file)
        state_root = _path(
            environment,
            "XDSH_CHANNEL_STATE_ROOT",
            "~/.xdsh/channel-state",
        )
        config = build_qq_gateway_config(
            environment,
            state_root=state_root / "qq-gateway",
        )
        sdk_version = require_qq_sdk()
        if args.check:
            _check_configuration(args, environment)
            print(
                json.dumps(
                    {
                        "schema_version": 1,
                        "channel": "qq",
                        "transport": "websocket",
                        "requires_public_https": False,
                        "ready": True,
                        "sdk": f"qqbot-agent-sdk=={sdk_version}",
                        "note": (
                            "configuration-only check; no network, DSH, or provider call"
                        ),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
        from xdsh.ops.keep_awake import KeepAwake

        with KeepAwake(environment.get("XDSH_KEEP_AWAKE", "off")):
            asyncio.run(_serve(args, environment, config))
    except (BootstrapError, ValueError, OSError, RuntimeError) as error:
        detail = _operator_error_detail(error)
        print(
            f"xdsh-qq: configuration failed ({type(error).__name__}): {detail}",
            file=sys.stderr,
        )
        LOGGER.debug(
            "QQ service configuration detail: %s",
            safe_exception_text(error, max_chars=400),
        )
        return 2
    return 0


async def _serve(
    args: argparse.Namespace,
    environment: Mapping[str, str],
    config: QQGatewayConfig,
) -> None:
    os.environ.update(environment)
    runner = _build_runner(args, environment)
    bridge = ChannelAgentBridge(
        runner,
        ChannelAgentConfig(
            state_root=_path(
                environment,
                "XDSH_CHANNEL_STATE_ROOT",
                "~/.xdsh/channel-state",
            ),
            workspace_root=_path(
                environment,
                "XDSH_CHANNEL_WORKSPACE_ROOT",
                "~/.xdsh/channel-workspaces",
            ),
            default_model=environment.get("XDSH_LITELLM_MODEL", "ark-code-latest"),
            task_timeout_seconds=_task_timeout(environment),
            max_sessions=500,
        ),
    )
    adapter = QQGatewayAdapter(config, environment=environment)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass
    LOGGER.info("XDSH QQ outbound WebSocket gateway starting")
    try:
        await adapter.serve(stop_event, bridge.handle)
    finally:
        await runner.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="xdsh-qq")
    parser.add_argument("--env-file")
    setup = parser.add_mutually_exclusive_group()
    setup.add_argument(
        "--init",
        action="store_true",
        help="create a mode-0600 placeholder QQ service env file without overwriting",
    )
    setup.add_argument(
        "--onboard", action="store_true",
        help="bind via official QQ scan and save credentials privately (five-minute timeout)",
    )
    parser.add_argument(
        "--from-env", action="append", default=[], metavar="PATH",
        help="import only runtime/model settings during setup; explicitly updates those fields",
    )
    parser.add_argument("--model")
    parser.add_argument("--provider")
    parser.add_argument("--runtime-command")
    parser.add_argument("--check", action="store_true")
    return parser


def _load_qq_environment(env_file: str | None) -> dict[str, str]:
    if env_file is not None:
        path = Path(env_file).expanduser()
        if not path.exists():
            display = safe_exception_text(ValueError(str(path)), max_chars=400)
            raise BootstrapError(
                "environment file does not exist: "
                f"{display}. Run xdsh-qq --init --env-file {display} first"
            )
        read_private_environment(path)
    return _load_environment(env_file)


def _initialize_env_file(env_file: str | None) -> tuple[Path, bool]:
    path = Path(env_file).expanduser() if env_file else _DEFAULT_ENV_FILE.expanduser()
    created = create_qq_environment(path.absolute(), _QQ_ENV_TEMPLATE)
    return path.resolve(), created


def _operator_error_detail(error: BaseException) -> str:
    # These types originate from XDSH's bounded configuration validators. Do
    # not expose arbitrary SDK/network RuntimeError or OSError text here.
    if isinstance(error, BootstrapError | ChannelError) or (
        isinstance(error, ValueError) and not isinstance(error, OSError | RuntimeError)
    ):
        return safe_exception_text(error, max_chars=600)
    return type(error).__name__


def _task_timeout(environment: Mapping[str, str]) -> float | None:
    raw = environment.get("XDSH_CHANNEL_TASK_TIMEOUT_SECONDS", "").strip().casefold()
    if raw in {"", "0", "off", "none", "unlimited"}:
        return None
    try:
        value = float(raw)
    except ValueError as error:
        raise ValueError("XDSH_CHANNEL_TASK_TIMEOUT_SECONDS must be numeric or off") from error
    if not 1.0 <= value <= 86_400.0:
        raise ValueError("XDSH_CHANNEL_TASK_TIMEOUT_SECONDS is out of bounds")
    return value


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
