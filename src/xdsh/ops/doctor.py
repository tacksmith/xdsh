"""Read-only installation diagnostics for the public XDSH distribution."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from xdsh import __version__
from xdsh.core.product_control import safe_exception_text
from xdsh.core.settings import JsonSettingsStore, SettingsError
from xdsh.ops.bootstrap import BootstrapError, parse_env_file, validate_xdsh_root
from xdsh.runners.compat import DshSdkSurface, inspect_sdk_surface, installed_sdk_version
from xdsh.runners.dsh import DSH_PYTHON_SDK_VERSION
from xdsh.runners.runtime import (
    VALIDATED_DSH_NPM_VERSIONS,
    RuntimeMode,
    classify_runtime_command,
)
from xdsh.version import DSH_COMPATIBLE_VERSION, XDSH_VERSION_DISPLAY, versions_align

DoctorStatus = Literal["ok", "warn", "error", "skip"]

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_PLACEHOLDER_MARKERS = (
    "replace-with-",
    "your-",
    "change-me",
    "<",
    "example",
)
_VERSION_RE = re.compile(
    r"(?<!\d)(?:0\.1\.1-rc\.2|0\.1\.2-alpha\.2)(?![0-9])",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    """One bounded, credential-free diagnostic observation."""

    name: str
    status: DoctorStatus
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class DoctorReport:
    """Aggregate result suitable for a terminal or CI check."""

    root: Path
    overall: DoctorStatus
    checks: tuple[DoctorCheck, ...]
    sdk_version: str | None = None
    sdk_surface: DshSdkSurface | None = None

    @property
    def exit_code(self) -> int:
        return 1 if self.overall == "error" else 0

    def as_dict(self) -> dict[str, object]:
        surface = self.sdk_surface
        return {
            "schema_version": 1,
            "xdsh_version": __version__,
            "root": _display_root(self.root),
            "overall": self.overall,
            "checks": [check.as_dict() for check in self.checks],
            "sdk": {
                "version": self.sdk_version,
                "contract": surface.contract if surface is not None else None,
                "ready": surface.ready if surface is not None else False,
                "config_available": surface.config_available if surface is not None else False,
                "harness_available": surface.harness_available if surface is not None else False,
                "run_method": surface.run_method if surface is not None else None,
                "close_method": surface.close_method if surface is not None else None,
                "operations": sorted(surface.operations) if surface is not None else [],
            },
        }


def inspect_installation(
    root: Path,
    *,
    mode: str = "both",
    environment: Mapping[str, str] | None = None,
    env_file: Path | None = None,
    runtime_command: str | None = None,
    probe_runtime: bool = False,
    sdk_module: object | None = None,
) -> DoctorReport:
    """Inspect an installation without starting DSH or contacting a gateway."""

    checks: list[DoctorCheck] = []
    try:
        safe_root = validate_xdsh_root(root)
        checks.append(DoctorCheck("root", "ok", "dedicated XDSH root"))
    except BootstrapError as error:
        # Keep a deterministic path for the rest of the report while making
        # the unsafe root the only hard error.
        safe_root = root.expanduser().resolve()
        checks.append(DoctorCheck("root", "error", safe_exception_text(error, max_chars=600)))

    checks.append(_directory_check(safe_root))
    checks.append(_permission_check(safe_root))

    module = sdk_module
    if module is None:
        try:
            import deepseek_harness as module  # type: ignore[import-untyped,no-redef]
        except ImportError:
            module = None
    if module is None:
        sdk_version = installed_sdk_version()
        surface = None
        checks.append(
            DoctorCheck(
                "dsh-sdk",
                "error",
                "official Python SDK is not installed",
            )
        )
    else:
        sdk_version = installed_sdk_version()
        surface = inspect_sdk_surface(module)
        if not surface.ready or not surface.config_available:
            checks.append(
                DoctorCheck(
                    "dsh-sdk",
                    "error",
                    "SDK surface does not satisfy the XDSH adapter contract",
                )
            )
        elif sdk_version and sdk_version != DSH_PYTHON_SDK_VERSION:
            checks.append(
                DoctorCheck(
                    "dsh-sdk",
                    "warn",
                    f"SDK {sdk_version} differs from validated "
                    f"{DSH_PYTHON_SDK_VERSION}; run the compatibility gate",
                )
            )
        else:
            checks.append(
                DoctorCheck(
                    "dsh-sdk",
                    "ok",
                    f"SDK {sdk_version or 'unknown'} exposes the versioned adapter surface",
                )
            )

    env_values = dict(environment or {})
    selected_env_files = _selected_env_files(safe_root, mode, env_file)
    if not env_values:
        for selected_env_file in selected_env_files:
            if selected_env_file.is_file():
                try:
                    parsed_values = parse_env_file(selected_env_file)
                    conflicts = sorted(set(env_values).intersection(parsed_values))
                    conflicting = [
                        name for name in conflicts if env_values[name] != parsed_values[name]
                    ]
                    if conflicting:
                        checks.append(
                            DoctorCheck(
                                "env-file",
                                "error",
                                "conflicting values across environment files: "
                                f"{', '.join(conflicting)}",
                            )
                        )
                    env_values.update(parsed_values)
                    checks.append(
                        DoctorCheck(
                            "env-file",
                            "ok",
                            f"parsed {selected_env_file.name} without evaluating shell code",
                        )
                    )
                    checks.append(_env_permissions_check(selected_env_file))
                except (BootstrapError, OSError, UnicodeError) as error:
                    checks.append(
                        DoctorCheck(
                            "env-file",
                            "error",
                            safe_exception_text(error, max_chars=600),
                        )
                    )
            else:
                checks.append(
                    DoctorCheck(
                        "env-file",
                        "warn",
                        f"{selected_env_file.name} is absent; selected transport is not configured",
                    )
                )
    else:
        checks.append(DoctorCheck("env-file", "skip", "explicit environment mapping supplied"))

    checks.extend(_configuration_checks(env_values, mode=mode))
    checks.append(_settings_check(safe_root, env_values))
    selected_runtime_command = runtime_command or env_values.get("XDSH_DSH_RUNTIME_COMMAND", "")
    checks.append(
        _runtime_check(
            selected_runtime_command,
            root=safe_root,
            probe=probe_runtime,
        )
    )
    checks.append(_version_alignment_check(selected_runtime_command))
    checks.append(_layout_check(safe_root, mode))

    overall: DoctorStatus = "ok"
    if any(check.status == "error" for check in checks):
        overall = "error"
    elif any(check.status == "warn" for check in checks):
        overall = "warn"
    return DoctorReport(
        root=safe_root,
        overall=overall,
        checks=tuple(checks),
        sdk_version=sdk_version,
        sdk_surface=surface,
    )


def _selected_env_files(root: Path, mode: str, env_file: Path | None) -> tuple[Path, ...]:
    if env_file is not None:
        return (env_file.expanduser(),)
    if mode == "cli":
        return (root / "config" / "cli.env",)
    if mode == "telegram":
        return (root / "config" / "telegram.env",)
    if mode == "both":
        return (root / "config" / "telegram.env", root / "config" / "cli.env")
    return (root / "config" / "telegram.env",)


def _configuration_checks(environment: Mapping[str, str], *, mode: str) -> list[DoctorCheck]:
    if mode not in {"cli", "telegram", "both"}:
        return [DoctorCheck("mode", "error", "mode must be cli, telegram, or both")]
    checks: list[DoctorCheck] = [DoctorCheck("mode", "ok", mode)]
    required = {
        "XDSH_LITELLM_BASE_URL",
        "XDSH_LITELLM_API_KEY",
        "XDSH_LITELLM_MODEL",
        "XDSH_DSH_RUNTIME_COMMAND",
    }
    if mode in {"telegram", "both"}:
        required.update({"XDSH_TELEGRAM_BOT_TOKEN", "XDSH_TELEGRAM_ALLOWED_USERS"})
    missing = sorted(name for name in required if not environment.get(name, "").strip())
    if missing:
        checks.append(
            DoctorCheck(
                "configuration",
                "warn",
                f"missing {len(missing)} required value(s); names only: {', '.join(missing)}",
            )
        )
    else:
        checks.append(DoctorCheck("configuration", "ok", "required names are present"))

    base_url = environment.get("XDSH_LITELLM_BASE_URL", "").strip()
    if base_url:
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in _LOOPBACK_HOSTS:
            checks.append(
                DoctorCheck("gateway", "error", "LiteLLM base URL must be loopback-only")
            )
        else:
            checks.append(DoctorCheck("gateway", "ok", "LiteLLM URL is loopback-only"))
    else:
        checks.append(DoctorCheck("gateway", "warn", "LiteLLM URL is not configured"))

    placeholder_names = sorted(
        name
        for name, value in environment.items()
        if name.endswith(("_TOKEN", "_API_KEY"))
        and _is_placeholder(value)
    )
    if placeholder_names:
        checks.append(
            DoctorCheck(
                "credentials",
                "warn",
                f"{len(placeholder_names)} credential reference(s) still use placeholders",
            )
        )
    else:
        checks.append(DoctorCheck("credentials", "ok", "credential values were not printed"))
    return checks


def _settings_check(root: Path, environment: Mapping[str, str]) -> DoctorCheck:
    raw_path = environment.get("XDSH_SETTINGS_PATH", "").strip()
    if not raw_path or raw_path == "~/.xdsh/config/settings.json":
        selected = root / "config" / "settings.json"
    else:
        selected = Path(raw_path)
    try:
        store = JsonSettingsStore(selected)
        if not store.path.exists():
            return DoctorCheck(
                "settings",
                "warn",
                "credential-free settings file is absent; built-in defaults are active",
            )
        settings = store.load()
    except (SettingsError, OSError, UnicodeError) as error:
        return DoctorCheck(
            "settings",
            "error",
            f"credential-free settings file is invalid ({type(error).__name__})",
        )
    return DoctorCheck(
        "settings",
        "ok",
        f"schema 1 valid; {len(settings.providers)} provider(s), "
        f"{len(settings.models)} model(s)",
    )


def _runtime_check(command_text: str, *, root: Path, probe: bool) -> DoctorCheck:
    raw = command_text.strip()
    if not raw or _is_placeholder(raw):
        return DoctorCheck(
            "dsh-runtime",
            "error",
            "explicit DSH runtime command is not configured",
        )
    try:
        argv = tuple(shlex.split(raw))
    except ValueError:
        return DoctorCheck("dsh-runtime", "error", "runtime command has invalid quoting")
    if not argv:
        return DoctorCheck("dsh-runtime", "error", "runtime command is empty")
    executable = Path(argv[0]).expanduser()
    resolved = executable if executable.is_absolute() else shutil.which(argv[0])
    if resolved is None:
        return DoctorCheck("dsh-runtime", "error", "runtime executable is not on PATH")
    path = Path(resolved)
    if not path.is_file() or not os.access(path, os.X_OK):
        return DoctorCheck("dsh-runtime", "error", "runtime executable is not executable")
    if not probe:
        return DoctorCheck(
            "dsh-runtime",
            "ok",
            "explicit executable is available (version probe skipped)",
        )
    try:
        completed = subprocess.run(
            [*argv, "--version"],
            cwd=str(root),
            env=_safe_probe_environment(),
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return DoctorCheck(
            "dsh-runtime",
            "warn",
            f"version probe unavailable: {type(error).__name__}",
        )
    output = f"{completed.stdout}\n{completed.stderr}"
    if completed.returncode != 0:
        launcher_check = _jsonrpc_launcher_check(argv, output, root=root)
        if launcher_check is not None:
            return launcher_check
        return DoctorCheck(
            "dsh-runtime",
            "warn",
            "executable exists but --version returned non-zero",
        )
    if _VERSION_RE.search(output):
        return DoctorCheck(
            "dsh-runtime",
            "ok",
            f"runtime reports validated DSH version ({_validated_versions_text()})",
        )
    return DoctorCheck(
        "dsh-runtime",
        "warn",
        "runtime responded, but its version is outside the validated DSH markers",
    )


def _version_alignment_check(command_text: str) -> DoctorCheck:
    """Report whether the detected DSH line matches the active XDSH line."""

    raw = command_text.strip()
    if not raw or _is_placeholder(raw):
        return DoctorCheck("version-alignment", "skip", "runtime version is not configured")
    try:
        argv = tuple(shlex.split(raw))
        runtime = classify_runtime_command(argv)
    except (ValueError, OSError):
        return DoctorCheck("version-alignment", "skip", "runtime shape is not inspectable")
    if runtime.version is None:
        return DoctorCheck("version-alignment", "skip", "runtime version metadata is unavailable")
    if versions_align(runtime.version, DSH_COMPATIBLE_VERSION):
        return DoctorCheck(
            "version-alignment",
            "ok",
            f"XDSH {XDSH_VERSION_DISPLAY} aligns with DSH {runtime.version}",
        )
    return DoctorCheck(
        "version-alignment",
        "warn",
        f"XDSH {XDSH_VERSION_DISPLAY} differs from detected DSH {runtime.version}",
    )


def _jsonrpc_launcher_check(
    argv: Sequence[str],
    output: str,
    *,
    root: Path,
) -> DoctorCheck | None:
    """Recognize the official JSON-RPC launcher whose version flag is usage-only.

    ``dsh-sdk-jsonrpc-demo`` intentionally exits non-zero for ``--version`` and
    prints its invocation form instead.  The adjacent package manifest is the
    stable, credential-free version source; arbitrary non-zero commands still
    follow the normal warning path.
    """

    try:
        runtime_spec = classify_runtime_command(argv)
    except ValueError:
        runtime_spec = None
    if runtime_spec is not None and runtime_spec.mode is RuntimeMode.PROFILE:
        if runtime_spec.version in VALIDATED_DSH_NPM_VERSIONS:
            return DoctorCheck(
                "dsh-runtime",
                "ok",
                f"official DSH profile launcher reports validated DSH {runtime_spec.version}",
            )
        return DoctorCheck(
            "dsh-runtime",
            "warn",
            "official DSH profile launcher found, but its package version is "
            "unreadable or outside the validated markers",
        )

    if "usage: dsh-jsonrpc-agent" not in output or "DSH_CORDIS_CONFIG" not in output:
        return None
    for raw_argument in argv:
        candidate = Path(raw_argument).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        if candidate.name != "packaged-bin.js":
            continue
        package_json = candidate.parent.parent / "package.json"
        try:
            payload = json.loads(package_json.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return DoctorCheck(
                "dsh-runtime",
                "warn",
                "official JSON-RPC launcher found, but its package version is unreadable",
            )
        version = payload.get("version") if isinstance(payload, dict) else None
        if isinstance(version, str) and _VERSION_RE.fullmatch(version):
            return DoctorCheck(
                "dsh-runtime",
                "ok",
                f"runtime package reports validated DSH {version}",
            )
        return DoctorCheck(
            "dsh-runtime",
            "warn",
            "official JSON-RPC launcher found, but its package version is "
            "outside the validated markers",
        )
    return DoctorCheck(
        "dsh-runtime",
        "warn",
        "official JSON-RPC usage response found, but packaged-bin.js was not identified",
    )


def _validated_versions_text() -> str:
    """Render the finite runtime allow-list deterministically for diagnostics."""

    return ", ".join(sorted(VALIDATED_DSH_NPM_VERSIONS))


def _safe_probe_environment() -> dict[str, str]:
    """Keep version probes from receiving credential-like environment values."""

    result: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if any(marker in upper for marker in ("TOKEN", "API_KEY", "PASSWORD", "SECRET")):
            continue
        result[key] = value
    return result


def _directory_check(root: Path) -> DoctorCheck:
    if not root.exists():
        return DoctorCheck("root-layout", "warn", "root is absent; run xdsh-setup")
    if not root.is_dir():
        return DoctorCheck("root-layout", "error", "root is not a directory")
    return DoctorCheck("root-layout", "ok", "root directory exists")


def _permission_check(root: Path) -> DoctorCheck:
    if not root.exists() or not root.is_dir():
        return DoctorCheck("permissions", "skip", "root permissions cannot be checked")
    try:
        mode = stat.S_IMODE(root.stat().st_mode)
    except OSError as error:
        return DoctorCheck(
            "permissions",
            "error",
            f"cannot stat root: {type(error).__name__}",
        )
    if mode & 0o077:
        return DoctorCheck(
            "permissions",
            "warn",
            "root is accessible by group/other; run xdsh-setup to harden it",
        )
    return DoctorCheck("permissions", "ok", "root is owner-only")


def _env_permissions_check(path: Path) -> DoctorCheck:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as error:
        return DoctorCheck(
            "env-permissions",
            "error",
            f"cannot stat env file: {type(error).__name__}",
        )
    if mode & 0o077:
        return DoctorCheck(
            "env-permissions",
            "error",
            "environment file must be mode 0600 or stricter",
        )
    return DoctorCheck("env-permissions", "ok", "environment file is owner-only")


def _layout_check(root: Path, mode: str) -> DoctorCheck:
    required: list[Path] = []
    if mode in {"cli", "both"}:
        required.extend(
            (
                root / "cli" / "state",
                root / "cli" / "workspaces",
                root / "cli" / "dsh-home",
            )
        )
    if mode in {"telegram", "both"}:
        required.extend(
            (
                root / "telegram" / "state",
                root / "telegram" / "state" / "dsh-home",
                root / "telegram" / "workspaces",
            )
        )
    missing = sum(not path.is_dir() for path in required)
    if missing:
        return DoctorCheck(
            "transport-layout",
            "warn",
            f"{missing} transport directorie(s) missing; run xdsh-setup",
        )
    return DoctorCheck("transport-layout", "ok", "requested transport directories exist")


def _is_placeholder(value: str) -> bool:
    normalized = value.strip().casefold()
    return any(marker in normalized for marker in _PLACEHOLDER_MARKERS)


def _display_root(path: Path) -> str:
    try:
        return "~/" + path.relative_to(Path.home().expanduser().resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("~/.xdsh"))
    parser.add_argument("--mode", choices=("cli", "telegram", "both"), default="both")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--runtime-command")
    parser.add_argument(
        "--probe-runtime",
        action="store_true",
        help="run the explicit command with --version using a credential-scrubbed environment",
    )
    parser.add_argument("--strict", action="store_true", help="treat warnings as failures")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    report = inspect_installation(
        args.root,
        mode=args.mode,
        env_file=args.env_file,
        runtime_command=args.runtime_command,
        probe_runtime=args.probe_runtime,
    )
    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, sort_keys=True))
    else:
        print(f"XDSH doctor: {report.overall} · root={_display_root(report.root)}")
        for check in report.checks:
            print(f"[{check.status}] {check.name}: {check.detail}")
    if report.overall == "error" or (args.strict and report.overall == "warn"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
