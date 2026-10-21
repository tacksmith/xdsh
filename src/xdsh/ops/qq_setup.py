"""Private QQ onboarding using the pinned official SDK and isolated state."""

from __future__ import annotations

import asyncio
import importlib
import os
import shlex
import stat
import tempfile
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol, cast
from urllib.parse import urlsplit

from xdsh.channels.config import build_qq_gateway_config
from xdsh.channels.qq import require_qq_sdk
from xdsh.ops.bootstrap import MAX_ENV_FILE_BYTES, BootstrapError, parse_env_file

# Explicitly exclude Bot tokens, user identities and all source state paths.
_REUSABLE_SETTINGS = frozenset({
    "XDSH_DSH_RUNTIME_COMMAND", "XDSH_DSH_PROVIDER", "XDSH_LITELLM_BASE_URL",
    "XDSH_LITELLM_API_KEY", "XDSH_LITELLM_MODEL", "XDSH_KEEP_AWAKE",
    "XDSH_MAX_PARALLEL_TOOL_CALLS", "XDSH_DSH_STREAM_IDLE_TIMEOUT_SECONDS",
})
_CREDENTIAL_SETTINGS = (
    "XDSH_QQ_APP_ID", "XDSH_QQ_APP_SECRET", "XDSH_QQ_ALLOWED_USERS",
)


class OnboardResult(Protocol):
    app_id: str
    client_secret: str
    user_openid: str


def read_private_environment(path: Path) -> dict[str, str]:
    """Read a current-user, owner-only env file without evaluating shell code."""
    _validate_path(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise BootstrapError("QQ setup requires owner-only regular environment files (0600)")
    if info.st_uid != os.getuid() or info.st_nlink != 1:
        raise BootstrapError("QQ setup requires current-user owned files with one link")
    return parse_env_file(path)


def create_qq_environment(
    target: Path, template: str, source_files: Sequence[Path] = (),
) -> bool:
    """Create a private template or explicitly import only named route settings."""
    _validate_path(target)
    before: bytes | None = None
    if target.exists():
        read_private_environment(target)
        if not source_files:
            return False
        before = target.read_bytes()
    settings: dict[str, str] = {}
    for source in source_files:
        values = read_private_environment(source.expanduser())
        settings.update({key: value for key, value in values.items()
                         if key in _REUSABLE_SETTINGS and value})
    root = target.parent.parent / "qq"
    settings.update({
        "XDSH_CHANNEL_STATE_ROOT": str(root / "state"),
        "XDSH_CHANNEL_WORKSPACE_ROOT": str(root / "workspaces"),
        "XDSH_CHANNEL_DSH_HOME": str(root / "dsh-home"),
    })
    content = _update_environment_text(
        template if before is None else before.decode("utf-8"), settings,
    )
    if before is not None:
        _replace_if_unchanged(target, content, before)
        return False
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags, 0o600)
    except FileExistsError:
        read_private_environment(target)
        return False
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    return True


async def onboard_qq_environment(
    target: Path,
    *,
    on_url: Callable[[str], None],
    onboard: Callable[..., Awaitable[OnboardResult]] | None = None,
) -> tuple[str, ...]:
    """Scan, validate, then atomically fill an unbound QQ configuration."""
    current = read_private_environment(target)
    if any(current.get(key, "").strip() for key in _CREDENTIAL_SETTINGS):
        raise BootstrapError("QQ credentials already configured; use a separate env file")
    before = target.read_bytes()
    if onboard is None:
        require_qq_sdk()
        constants = importlib.import_module("qqbot_agent_sdk.constants")
        if getattr(constants, "PORTAL_HOST", None) != "q.qq.com":
            raise BootstrapError("QQ onboarding must use the official q.qq.com portal")
        sdk = importlib.import_module("qqbot_agent_sdk")
        onboard = cast(Callable[..., Awaitable[OnboardResult]], sdk.start_onboard)

    rejected_url = False

    def show_url(url: str) -> None:
        nonlocal rejected_url
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname != "q.qq.com"
                or parsed.username or parsed.password or parsed.port not in (None, 443)):
            rejected_url = True
            raise BootstrapError("QQ onboarding returned a non-official URL")
        on_url(url)

    try:
        result = await asyncio.wait_for(
            onboard(on_qr_ready=show_url, poll_timeout=300.0), timeout=330.0,
        )
    except asyncio.CancelledError:
        raise
    except Exception as error:
        # SDK exceptions may embed URLs or secrets; expose only the failure type.
        raise BootstrapError(
            f"QQ scan binding failed ({type(error).__name__}); retry --onboard",
        ) from None
    if rejected_url:
        raise BootstrapError("QQ onboarding returned a non-official URL; credentials not saved")
    updates = dict(zip(_CREDENTIAL_SETTINGS, (
        result.app_id, result.client_secret, result.user_openid,
    ), strict=True))
    build_qq_gateway_config({**current, **updates}, state_root=target.parent.parent / "qq/state")
    content = _update_environment_text(before.decode("utf-8"), updates)
    _replace_if_unchanged(target, content, before)
    return _CREDENTIAL_SETTINGS


def _update_environment_text(content: str, updates: Mapping[str, str]) -> str:
    for value in updates.values():
        if any(char in value for char in ("\n", "\r", "\x00")):
            raise BootstrapError("QQ configuration values must stay on one line")
    lines: list[str] = []
    remaining = dict(updates)
    for line in content.splitlines():
        stripped = line.strip().removeprefix("export ")
        key = stripped.partition("=")[0].strip()
        if "=" in stripped and key in remaining:
            lines.append(f"{key}={shlex.quote(remaining.pop(key))}")
        else:
            lines.append(line)
    lines.extend(f"{key}={shlex.quote(value)}" for key, value in remaining.items())
    rendered = "\n".join(lines) + "\n"
    if len(rendered.encode("utf-8")) > MAX_ENV_FILE_BYTES:
        raise BootstrapError("QQ environment exceeds its size bound")
    return rendered


def _validate_path(path: Path) -> None:
    for item in (path, *path.parents):
        if item.is_symlink():
            raise BootstrapError("QQ setup paths must not traverse symlinks")
    resolved = path.resolve()
    if ".xclaude" in resolved.parts or ".dsh" in resolved.parts:
        raise BootstrapError("QQ setup path overlaps a reserved agent root")


def _replace_if_unchanged(target: Path, content: str, before: bytes) -> None:
    read_private_environment(target)
    if target.read_bytes() != before:
        raise BootstrapError("QQ env changed during scan; nothing overwritten, retry --onboard")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".qq-onboard-", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if target.read_bytes() != before or target.is_symlink():
            raise BootstrapError("QQ env changed during scan; nothing overwritten")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
