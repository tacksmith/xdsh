"""Safe, idempotent initialization of an XDSH installation.

The public installer deliberately does not install or discover a DSH process.
DSH is an independently released runtime and must be supplied through an
explicit JSON-RPC command.  This module only creates XDSH-owned directories
and credential-free environment templates.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import stat
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from xdsh.core.i18n import (
    DEFAULT_LANGUAGE,
    SUPPORTED_LANGUAGES,
    canonical_language,
    language_spec,
    translate,
)
from xdsh.core.product_control import safe_exception_text

DEFAULT_ROOT = Path("~/.xdsh")
_ROOT_PART_RE = re.compile(r"^[A-Za-z0-9._+@:/=-]+$")
MAX_ENV_FILE_BYTES = 64 * 1024
MAX_ENV_LINES = 512
MAX_ENV_ENTRIES = 128
MAX_ENV_LINE_CHARS = 32 * 1024
MAX_ENV_VALUE_CHARS = 16 * 1024

_CONFIG_GITIGNORE = "*.env\n*.secret\n"
_SETTINGS_TEMPLATE = """{
  "schema_version": 1,
  "providers": [],
  "models": []
}
"""

_TELEGRAM_ENV_TEMPLATE = """# XDSH public-install template. Do not commit telegram.env.
# Copy this file to telegram.env, replace every placeholder, and chmod 600.
XDSH_TELEGRAM_BOT_TOKEN=replace-with-independent-bot-token
XDSH_TELEGRAM_ALLOWED_USERS=replace-with-numeric-user-id
XDSH_LITELLM_BASE_URL=http://127.0.0.1:4000
XDSH_LITELLM_API_KEY=replace-with-loopback-gateway-key
# Use the exact model ID configured in YOUR gateway, not a display alias.
XDSH_LITELLM_MODEL=replace-with-exact-model-id
XDSH_DEFAULT_LANGUAGE=replace-with-xdsh-language
# Service-lifetime idle-sleep protection; display sleep remains enabled.
# Containers/WSL need host-side setup. Increased battery consumption when on.
XDSH_KEEP_AWAKE=replace-with-keep-awake
# Character limit for the displayed final answer (not a task/token budget).
XDSH_TELEGRAM_MAX_ANSWER_CHARS=200000
XDSH_SETTINGS_PATH=~/.xdsh/config/settings.json
# Optional provider grouping; settings.json can declare aliases omitted by
# LiteLLM /model/info. This line contains no endpoint or credential.
# XDSH_MODEL_PROVIDER_ALIASES=volc-coding=ark-code-latest;local-llama=qwen-local
# Supply the official DSH JSON-RPC argv explicitly; do not use shell syntax.
# Legacy demo and alpha.2 profile examples are documented in WSL.md.
XDSH_DSH_RUNTIME_COMMAND=replace-with-official-dsh-jsonrpc-command
# Optional: official DSH CLI argv for /plugins install/remove. For alpha.2,
# `dsh --profile sdk` is derived automatically; set this only when needed.
# XDSH_DSH_PLUGIN_COMMAND=dsh
XDSH_STATE_ROOT=~/.xdsh/telegram/state
XDSH_WORKSPACE_ROOT=~/.xdsh/telegram/workspaces
XDSH_DSH_HOME=~/.xdsh/telegram/state/dsh-home
XDSH_DSH_PROVIDER=litellm
# Up to four runtime-declared concurrency-safe reads per model step. Writes,
# installs, and state changes remain serial.
XDSH_MAX_PARALLEL_TOOL_CALLS=4
XDSH_AUTO_COMPACT_ENABLED=1
XDSH_NATIVE_COMPACTION_ENABLED=1
# Warn at 20%, compact at 50% or 64K tokens (whichever comes first), and
# retain only the newest 8% verbatim. The 64K cap protects large-window routes
# whose provider/tokenizer differs from DSH rc.2's conservative estimator.
XDSH_AUTO_COMPACT_WARNING_RATIO=0.20
XDSH_AUTO_COMPACT_THRESHOLD_RATIO=0.50
XDSH_AUTO_COMPACT_THRESHOLD_TOKENS=64000
XDSH_AUTO_COMPACT_RETAIN_RATIO=0.08
XDSH_AUTO_COMPACT_TARGET_RATIO=0.20
# A live stream may be slow, but one DSH TIMEOUT is terminal and is not replayed.
XDSH_DSH_STREAM_IDLE_TIMEOUT_SECONDS=120
# Optional hybrid semantic memory. All four required values must be supplied
# together; the endpoint is HTTP loopback only and input is redacted/bounded.
# XDSH_EMBEDDING_ENDPOINT=http://127.0.0.1:8081/v1/embeddings
# XDSH_EMBEDDING_MODEL=local-embedding-model
# XDSH_EMBEDDING_DIMENSIONS=1024
# XDSH_EMBEDDING_CREDENTIAL_ENV=XDSH_EMBEDDING_TOKEN
# XDSH_EMBEDDING_TOKEN=replace-with-independent-loopback-token
# XDSH_EMBEDDING_TIMEOUT_SECONDS=10
# Long-running tasks have no wall-clock ceiling while structured activity
# continues. Silence still warns/stops at 80%/100% of the 600-second default.
# XDSH_GUARD_STALL_SECONDS=600
# XDSH_GUARD_MAX_RUNTIME_SECONDS=unlimited
# Identical failures have a separate per-task budget, unaffected by Todo success.
# XDSH_GUARD_MAX_REPEATED_TOOL_ERRORS=3
# Bounded upstream 429 protection (native DSH retryPolicy + route gate).
# XDSH_RATE_LIMIT_MAX_CONCURRENT_REQUESTS=2
# XDSH_RATE_LIMIT_MIN_INTERVAL_SECONDS=0.25
# XDSH_RATE_LIMIT_MAX_RETRIES=5
# XDSH_RATE_LIMIT_BASE_BACKOFF_SECONDS=5
# XDSH_RATE_LIMIT_MAX_BACKOFF_SECONDS=120
# XDSH_RATE_LIMIT_MAX_RETRY_AFTER_SECONDS=120
# XDSH_RATE_LIMIT_QUEUE_TIMEOUT_SECONDS=120
# XDSH_RATE_LIMIT_JITTER_RATIO=0.10
# XDSH_RATE_LIMIT_ALLOW_FALLBACK=0
"""

_CLI_ENV_TEMPLATE = """# XDSH public-install CLI template. Values are references only.
XDSH_LITELLM_BASE_URL=http://127.0.0.1:4000
XDSH_LITELLM_API_KEY=replace-with-loopback-gateway-key
# Use the exact model ID configured in YOUR gateway, not a display alias.
XDSH_LITELLM_MODEL=replace-with-exact-model-id
XDSH_DEFAULT_LANGUAGE=replace-with-xdsh-language
XDSH_SETTINGS_PATH=~/.xdsh/config/settings.json
# Optional provider grouping; settings.json can declare aliases omitted by
# LiteLLM /model/info. This line contains no endpoint or credential.
# XDSH_MODEL_PROVIDER_ALIASES=volc-coding=ark-code-latest;local-llama=qwen-local
XDSH_DSH_RUNTIME_COMMAND=replace-with-official-dsh-jsonrpc-command
# Optional official DSH CLI argv for approval-gated plugin management.
# XDSH_DSH_PLUGIN_COMMAND=dsh
XDSH_CLI_ROOT=~/.xdsh/cli
XDSH_CLI_STATE_ROOT=~/.xdsh/cli/state
XDSH_CLI_WORKSPACE_ROOT=~/.xdsh/cli/workspaces
XDSH_CLI_DSH_HOME=~/.xdsh/cli/dsh-home
XDSH_DSH_PROVIDER=litellm
# Up to four runtime-declared concurrency-safe reads per model step. Writes,
# installs, and state changes remain serial.
XDSH_MAX_PARALLEL_TOOL_CALLS=4
XDSH_AUTO_COMPACT_ENABLED=1
XDSH_NATIVE_COMPACTION_ENABLED=1
XDSH_AUTO_COMPACT_WARNING_RATIO=0.20
XDSH_AUTO_COMPACT_THRESHOLD_RATIO=0.50
XDSH_AUTO_COMPACT_THRESHOLD_TOKENS=64000
XDSH_AUTO_COMPACT_RETAIN_RATIO=0.08
XDSH_AUTO_COMPACT_TARGET_RATIO=0.20
# Bounded upstream 429 protection (native DSH retryPolicy + route gate).
# XDSH_RATE_LIMIT_MAX_CONCURRENT_REQUESTS=2
# XDSH_RATE_LIMIT_MIN_INTERVAL_SECONDS=0.25
# XDSH_RATE_LIMIT_MAX_RETRIES=5
# XDSH_RATE_LIMIT_BASE_BACKOFF_SECONDS=5
# XDSH_RATE_LIMIT_MAX_BACKOFF_SECONDS=120
# XDSH_RATE_LIMIT_MAX_RETRY_AFTER_SECONDS=120
# XDSH_RATE_LIMIT_QUEUE_TIMEOUT_SECONDS=120
# XDSH_RATE_LIMIT_JITTER_RATIO=0.10
# XDSH_RATE_LIMIT_ALLOW_FALLBACK=0
"""

_CHANNELS_ENV_TEMPLATE = """# XDSH optional channel template. Do not commit channels.env.
# Keep this file mode 0600 and replace only the channels you explicitly enable.
# Discord (public key is not secret, but keep the file owner-only for consistency)
XDSH_DISCORD_APPLICATION_ID=replace-with-discord-application-id
XDSH_DISCORD_PUBLIC_KEY_HEX=replace-with-64-hex-public-key
# Optional: use the bot channel API for long answers.
XDSH_DISCORD_BOT_TOKEN=replace-with-discord-bot-token
# WhatsApp Cloud API
XDSH_WHATSAPP_PHONE_NUMBER_ID=replace-with-phone-number-id
XDSH_WHATSAPP_VERIFY_TOKEN=replace-with-webhook-verify-token
XDSH_WHATSAPP_APP_SECRET=replace-with-meta-app-secret
XDSH_WHATSAPP_ACCESS_TOKEN=replace-with-meta-access-token
XDSH_WHATSAPP_API_VERSION=v21.0
# DingTalk signed robot callback
XDSH_DINGTALK_SIGNING_SECRET=replace-with-dingtalk-signing-secret
# Feishu/Lark event subscription
XDSH_FEISHU_APP_ID=replace-with-feishu-app-id
XDSH_FEISHU_APP_SECRET=replace-with-feishu-app-secret
# Required for URL verification and unsigned event callbacks.
XDSH_FEISHU_VERIFICATION_TOKEN=replace-with-feishu-verification-token
# Optional: enables signed/encrypted event callbacks.
XDSH_FEISHU_ENCRYPT_KEY=replace-with-feishu-encrypt-key
# WeChat Official Account
XDSH_WECHAT_VERIFICATION_TOKEN=replace-with-wechat-server-token
XDSH_WECHAT_APP_ID=replace-with-wechat-app-id
XDSH_WECHAT_APP_SECRET=replace-with-wechat-app-secret
# QQ Official Bot (outbound WebSocket; no public HTTPS endpoint)
XDSH_QQ_APP_ID=replace-with-qq-app-id
XDSH_QQ_APP_SECRET=replace-with-qq-app-secret
# Required fail-closed owner allowlist (QQ user OpenIDs, comma-separated).
XDSH_QQ_ALLOWED_USERS=replace-with-owner-openid
# Optional: group turns require both an allowed user and group OpenID.
XDSH_QQ_ALLOWED_GROUPS=
XDSH_QQ_MARKDOWN=off
XDSH_QQ_MAX_INFLIGHT=4
XDSH_QQ_MAX_PENDING=32
"""


class BootstrapError(ValueError):
    """The requested XDSH initialization target is unsafe or invalid."""


@dataclass(frozen=True, slots=True)
class BootstrapReport:
    """Content-free result of one idempotent initialization pass."""

    root: Path
    mode: str
    language: str
    created: tuple[str, ...]
    existing: tuple[str, ...]
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "root": _display_root(self.root),
            "mode": self.mode,
            "language": self.language,
            "created": list(self.created),
            "existing": list(self.existing),
            "warnings": list(self.warnings),
        }


def initialize_xdsh(
    root: Path = DEFAULT_ROOT,
    *,
    mode: str = "both",
    runtime_command: str | None = None,
    write_env: bool = False,
    language: str = DEFAULT_LANGUAGE,
    keep_awake: str = "off",
) -> BootstrapReport:
    """Create an isolated XDSH layout without overwriting existing files.

    ``mode`` controls which transport roots are created.  The function never
    writes a real credential and never invokes a runtime.  Existing files are
    retained byte-for-byte; a caller can replace a template explicitly after
    inspecting it.
    """

    if mode not in {"cli", "telegram", "both"}:
        raise BootstrapError("mode must be cli, telegram, or both")
    if keep_awake not in {"on", "off"}:
        raise BootstrapError("keep_awake must be on or off")
    try:
        selected_language = canonical_language(language)
    except ValueError as error:
        raise BootstrapError("language is unsupported") from error
    safe_root = validate_xdsh_root(root)
    created: list[str] = []
    existing: list[str] = []

    _ensure_directory(safe_root, safe_root, created, existing)
    _ensure_directory(safe_root / "config", safe_root, created, existing)
    _write_if_absent(
        safe_root / "config" / ".gitignore",
        _CONFIG_GITIGNORE,
        safe_root,
        created,
        existing,
    )
    _write_if_absent(
        safe_root / "config" / "settings.json",
        _SETTINGS_TEMPLATE,
        safe_root,
        created,
        existing,
    )
    # Keep the optional channel setup discoverable in every installation mode;
    # it is a placeholder-only file and does not activate a listener.
    _write_if_absent(
        safe_root / "config" / "channels.env.example",
        _CHANNELS_ENV_TEMPLATE,
        safe_root,
        created,
        existing,
    )
    if write_env:
        _write_if_absent(
            safe_root / "config" / "channels.env",
            _CHANNELS_ENV_TEMPLATE,
            safe_root,
            created,
            existing,
        )

    if mode in {"cli", "both"}:
        for relative in (
            "cli",
            "cli/state",
            "cli/workspaces",
            "cli/dsh-home",
        ):
            _ensure_directory(safe_root / relative, safe_root, created, existing)
        _write_if_absent(
            safe_root / "config" / "cli.env.example",
            _render_template(_CLI_ENV_TEMPLATE, runtime_command, selected_language),
            safe_root,
            created,
            existing,
        )
        if write_env:
            _write_if_absent(
                safe_root / "config" / "cli.env",
                _render_template(_CLI_ENV_TEMPLATE, runtime_command, selected_language),
                safe_root,
                created,
                existing,
            )

    if mode in {"telegram", "both"}:
        for relative in (
            "telegram",
            "telegram/state",
            "telegram/state/dsh-home",
            "telegram/workspaces",
        ):
            _ensure_directory(safe_root / relative, safe_root, created, existing)
        _write_if_absent(
            safe_root / "config" / "telegram.env.example",
            _render_template(_TELEGRAM_ENV_TEMPLATE, runtime_command, selected_language,
                             keep_awake),
            safe_root,
            created,
            existing,
        )
        if write_env:
            _write_if_absent(
                safe_root / "config" / "telegram.env",
                _render_template(_TELEGRAM_ENV_TEMPLATE, runtime_command, selected_language,
                                 keep_awake),
                safe_root,
                created,
                existing,
            )

    warnings: list[str] = []
    if write_env:
        warnings.append(
            "generated environment files contain placeholders only; replace them and keep mode 0600"
        )
    if runtime_command is None:
        warnings.append(
            "DSH runtime command remains a placeholder; set it to the official JSON-RPC argv"
        )
    return BootstrapReport(
        root=safe_root,
        mode=mode,
        language=selected_language,
        created=tuple(created),
        existing=tuple(existing),
        warnings=tuple(warnings),
    )


def validate_xdsh_root(root: Path) -> Path:
    """Resolve and validate a dedicated XDSH root before any write."""

    expanded = root.expanduser()
    if expanded.is_symlink():
        raise BootstrapError("XDSH root must not be a symlink")
    try:
        resolved = expanded.resolve()
    except OSError as error:
        raise BootstrapError("cannot resolve XDSH root") from error
    if resolved == Path(resolved.anchor) or resolved == Path.home().expanduser().resolve():
        raise BootstrapError("XDSH root must be a dedicated child directory")
    if ".xclaude" in resolved.parts or ".dsh" in resolved.parts:
        raise BootstrapError("XDSH root must not overlap reserved agent roots")
    if not _ROOT_PART_RE.fullmatch(resolved.name):
        raise BootstrapError("XDSH root contains an unsupported path component")
    return resolved


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a small dotenv subset without shell evaluation or value logging."""

    expanded = path.expanduser()
    if expanded.is_symlink() or not expanded.is_file():
        raise BootstrapError("environment file must be a regular, non-symlink file")
    try:
        with expanded.open("rb") as handle:
            raw = handle.read(MAX_ENV_FILE_BYTES + 1)
    except OSError as error:
        raise BootstrapError("cannot read environment file") from error
    if len(raw) > MAX_ENV_FILE_BYTES:
        raise BootstrapError(f"environment file exceeds {MAX_ENV_FILE_BYTES} bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise BootstrapError("environment file must be UTF-8") from error
    lines = text.splitlines()
    if len(lines) > MAX_ENV_LINES:
        raise BootstrapError(f"environment file exceeds {MAX_ENV_LINES} lines")
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(lines, 1):
        if len(raw_line) > MAX_ENV_LINE_CHARS:
            raise BootstrapError(
                f"environment file line {line_number} exceeds {MAX_ENV_LINE_CHARS} characters"
            )
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise BootstrapError(f"environment file line {line_number} is not KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise BootstrapError(f"environment file line {line_number} has an invalid key")
        if key in values:
            raise BootstrapError(f"environment file repeats {key}")
        value = value.strip()
        if "\x00" in value or len(value) > MAX_ENV_VALUE_CHARS:
            raise BootstrapError(f"environment file line {line_number} has an unsafe value")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            try:
                parsed = shlex.split(value, comments=False, posix=True)
            except ValueError as error:
                raise BootstrapError(
                    f"environment file line {line_number} has invalid quoting"
                ) from error
            value = parsed[0] if parsed else ""
        if len(values) >= MAX_ENV_ENTRIES:
            raise BootstrapError(f"environment file exceeds {MAX_ENV_ENTRIES} entries")
        values[key] = value
    return values


def _render_template(
    template: str,
    runtime_command: str | None,
    language: str,
    keep_awake: str = "off",
) -> str:
    rendered_template = template.replace("replace-with-xdsh-language", language)
    rendered_template = rendered_template.replace("replace-with-keep-awake", keep_awake)
    if runtime_command is None:
        return rendered_template
    command = runtime_command.strip()
    if not command:
        raise BootstrapError("runtime command cannot be empty")
    try:
        argv = tuple(shlex.split(command))
    except ValueError as error:
        raise BootstrapError("runtime command has invalid quoting") from error
    if not argv or any(not item for item in argv):
        raise BootstrapError("runtime command must contain argv entries")
    # Keep the generated value shell-free and reversible.  The XDSH loaders
    # parse argv with shlex; quoting here preserves spaces in executable paths.
    rendered = shlex.join(argv)
    return rendered_template.replace(
        "replace-with-official-dsh-jsonrpc-command",
        rendered,
    )


def _ensure_directory(
    path: Path,
    root: Path,
    created: list[str],
    existing: list[str],
) -> None:
    _assert_below(path, root)
    if path.is_symlink():
        raise BootstrapError(f"refusing symlink path: {_relative(path, root)}")
    if path.exists() and not path.is_dir():
        raise BootstrapError(f"path is not a directory: {_relative(path, root)}")
    if not path.exists():
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        created.append(_relative(path, root))
    else:
        existing.append(_relative(path, root))
    try:
        current = stat.S_IMODE(path.stat().st_mode)
        if current & 0o077:
            path.chmod(current & 0o700)
    except OSError as error:
        raise BootstrapError(f"cannot harden directory: {_relative(path, root)}") from error


def _write_if_absent(
    path: Path,
    content: str,
    root: Path,
    created: list[str],
    existing: list[str],
) -> None:
    _assert_below(path, root)
    if path.is_symlink():
        raise BootstrapError(f"refusing symlink file: {_relative(path, root)}")
    if path.exists():
        if not path.is_file():
            raise BootstrapError(f"path is not a regular file: {_relative(path, root)}")
        path.chmod(stat.S_IMODE(path.stat().st_mode) & 0o600)
        existing.append(_relative(path, root))
        return
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(content)
        path.chmod(0o600)
    except FileExistsError:
        existing.append(_relative(path, root))
    except OSError as error:
        raise BootstrapError(f"cannot write {_relative(path, root)}") from error
    else:
        created.append(_relative(path, root))


def _assert_below(path: Path, root: Path) -> None:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as error:
        raise BootstrapError("initialization path escaped the XDSH root") from error


def _relative(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix() or "."


def _display_root(path: Path) -> str:
    try:
        return "~/" + path.relative_to(Path.home().expanduser().resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--mode", choices=("cli", "telegram", "both"), default="both")
    parser.add_argument(
        "--runtime-command",
        help="explicit DSH JSON-RPC argv to place in the generated templates",
    )
    parser.add_argument(
        "--write-env",
        action="store_true",
        help="also create selected transport env files with placeholders (never overwrites)",
    )
    parser.add_argument(
        "--language",
        help="default interface/answer language code (interactive when omitted on a TTY)",
    )
    parser.add_argument("--json", action="store_true", help="emit a machine-readable report")
    parser.add_argument("--keep-awake", choices=("on", "off"),
                        help="prevent automatic idle sleep while the Bot runs (uses more power)")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    keep_awake = args.keep_awake
    if keep_awake is None:
        keep_awake = "off"
        if (args.mode != "cli" and not args.json
                and sys.stdin.isatty() and sys.stdout.isatty()):
            answer = input("防自动休眠 / Keep Bot awake (more battery use)? [y/N]: ")
            keep_awake = "on" if answer.strip().lower() in {"y", "yes"} else "off"
    language = args.language
    if language is None:
        language = (
            choose_install_language()
            if not args.json and sys.stdin.isatty() and sys.stdout.isatty()
            else DEFAULT_LANGUAGE
        )
    try:
        report = initialize_xdsh(
            args.root,
            mode=args.mode,
            runtime_command=args.runtime_command,
            write_env=args.write_env,
            language=language,
            keep_awake=keep_awake,
        )
    except (BootstrapError, OSError) as error:
        raise SystemExit(safe_exception_text(error, max_chars=600)) from None
    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, sort_keys=True))
        return
    display_language = language_spec(report.language).native_name
    print(
        translate(
            report.language,
            "setup.ready",
            root=_display_root(report.root),
            mode=report.mode,
            language=display_language,
        )
    )
    print(
        translate(
            report.language,
            "setup.counts",
            created=len(report.created),
            existing=len(report.existing),
        )
    )
    for warning in report.warnings:
        print(translate(report.language, "setup.warning", warning=warning))


def choose_install_language(
    *,
    read: Callable[[str], str] = input,
    write: Callable[[str], None] = print,
) -> str:
    """Prompt for a language using only bounded, non-secret input."""

    write("Language / 語言 / 语言")
    for index, item in enumerate(SUPPORTED_LANGUAGES, start=1):
        write(f"  {index:>2}. {item.native_name} ({item.code})")
    for _attempt in range(3):
        raw = read("Select number or code: ")
        value = raw.strip()
        if value.isascii() and value.isdigit():
            index = int(value)
            if 1 <= index <= len(SUPPORTED_LANGUAGES):
                return SUPPORTED_LANGUAGES[index - 1].code
        try:
            return canonical_language(value)
        except ValueError:
            write("Unsupported language; try again.")
    raise BootstrapError("language selection failed after three attempts")


if __name__ == "__main__":
    main()
