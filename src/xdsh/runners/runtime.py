"""Version and launch-shape compatibility for official DSH runtimes.

The public DSH launcher changed between the validated rc.2 SDK demo and the
0.1.2 alpha profile launcher.  This module keeps that difference at the
runner boundary: product code can identify the launch shape, generate a small
profile overlay, and otherwise continue to speak the same JSON-RPC protocol.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from xdsh.core.execution_policy import validate_parallel_tool_limit
from xdsh.core.rate_limit import RateLimitPolicy
from xdsh.core.runner import REASONING_EFFORTS, ReasoningEffort
from xdsh.version import DSH_COMPATIBLE_VERSION, XDSH_VERSION, versions_align

# Keep the historical constant name for callers while sourcing the active
# release from the single XDSH/DSH version contract.
LEGACY_DSH_NPM_VERSION = DSH_COMPATIBLE_VERSION
ALPHA2_DSH_NPM_VERSION = "0.1.2-alpha.2"
VALIDATED_DSH_NPM_VERSIONS = frozenset(
    {LEGACY_DSH_NPM_VERSION, ALPHA2_DSH_NPM_VERSION}
)

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PACKAGE_NAME = "@deepseek-ai/dsh"
_LEGACY_PACKAGE_NAME = "@deepseek-ai/dsh-sdk-jsonrpc-demo"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_PROFILE_SETTINGS_NAME = "xdsh-profile-settings.yaml"
_PROFILE_PATCH_NAME = "xdsh-profile.patch.yml"
_MANAGED_HEADER = "# XDSH-managed DSH profile compatibility files; do not edit.\n"
_DEFAULT_RELEASE_PROFILE = Path(__file__).resolve().parents[1] / "release_profile.json"
_MAX_RELEASE_PROFILE_BYTES = 16 * 1024
_DSH_RETRYABLE_CODES = ("RATE_LIMIT", "SERVER", "TRANSPORT", "EMPTY_RESPONSE")


class RuntimeMode(str, Enum):
    """Supported official DSH launch families."""

    LEGACY = "legacy-jsonrpc"
    PROFILE = "profile"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class DshRuntimeSpec:
    """A content-free classification of one explicit runtime argv."""

    command: tuple[str, ...]
    mode: RuntimeMode
    profile: str | None = None
    package_name: str | None = None
    version: str | None = None

    @property
    def is_profile(self) -> bool:
        return self.mode is RuntimeMode.PROFILE

    @property
    def is_legacy(self) -> bool:
        return self.mode is RuntimeMode.LEGACY


RuntimeCompatibilityStatus = Literal["compatible", "unsupported", "unknown"]


@dataclass(frozen=True, slots=True)
class RuntimeCompatibilityReport:
    """Stable, content-free explanation of a runtime compatibility decision."""

    runtime: DshRuntimeSpec
    supported_versions: tuple[str, ...]
    status: RuntimeCompatibilityStatus
    reason: str

    @property
    def ready(self) -> bool:
        return self.status == "compatible"


def runtime_compatibility_report(
    command: Sequence[str],
    *,
    supported_versions: Sequence[str] = tuple(sorted(VALIDATED_DSH_NPM_VERSIONS)),
) -> RuntimeCompatibilityReport:
    """Explain compatibility without starting DSH or contacting a provider.

    Development tooling can show a useful downgrade reason for a future DSH
    line, while the published RC2 validator remains fail-closed.
    """

    runtime = classify_runtime_command(command)
    versions = tuple(sorted({value for value in supported_versions if value}))
    if runtime.mode is RuntimeMode.UNKNOWN:
        return RuntimeCompatibilityReport(runtime, versions, "unknown", "unknown_launcher")
    if runtime.version is None:
        return RuntimeCompatibilityReport(runtime, versions, "unknown", "version_unavailable")
    if runtime.version not in versions:
        return RuntimeCompatibilityReport(
            runtime, versions, "unsupported", "version_not_allowlisted"
        )
    if not versions_align(runtime.version, DSH_COMPATIBLE_VERSION):
        return RuntimeCompatibilityReport(
            runtime, versions, "unsupported", "version_not_aligned"
        )
    return RuntimeCompatibilityReport(runtime, versions, "compatible", "aligned")


@dataclass(frozen=True, slots=True)
class ProfileFiles:
    """Generated alpha-profile files owned by one isolated DSH home."""

    settings_path: Path
    patch_path: Path


def classify_runtime_command(command: Sequence[str]) -> DshRuntimeSpec:
    """Classify an explicit DSH command without starting it.

    ``dsh-sdk-jsonrpc-demo/lib/packaged-bin.js`` is the rc.2 launcher.  Alpha
    profiles use the official ``dsh/lib/bin.js --profile sdk`` entry point (or
    an equivalent ``dsh --profile sdk`` wrapper).  Unknown commands remain
    usable for hermetic test doubles, but carry no optimistic version claim.
    """

    argv = tuple(command)
    if any(not item for item in argv):
        raise ValueError("runtime command arguments must be non-empty")

    profile = _profile_argument(argv)
    normalized = tuple(item.replace("\\", "/") for item in argv)
    legacy_launcher = any(
        item.endswith("/packaged-bin.js")
        or Path(item).name == "packaged-bin.js"
        or "/@deepseek-ai/dsh-sdk-jsonrpc-demo/" in f"/{item.lstrip('/')}"
        for item in normalized
    )
    profile_launcher = profile is not None or any(
        "/@deepseek-ai/dsh/lib/bin.js" in f"/{item.lstrip('/')}"
        for item in normalized
    )

    if profile_launcher:
        package_name, version = _package_metadata(argv, expected=_PACKAGE_NAME)
        return DshRuntimeSpec(
            command=argv,
            mode=RuntimeMode.PROFILE,
            profile=profile or "sdk",
            package_name=package_name or _PACKAGE_NAME,
            version=version,
        )
    if legacy_launcher:
        package_name, version = _package_metadata(argv, expected=_LEGACY_PACKAGE_NAME)
        return DshRuntimeSpec(
            command=argv,
            mode=RuntimeMode.LEGACY,
            package_name=package_name or _LEGACY_PACKAGE_NAME,
            version=version or LEGACY_DSH_NPM_VERSION,
        )
    return DshRuntimeSpec(command=argv, mode=RuntimeMode.UNKNOWN)


def validate_packaged_release_runtime(
    command: Sequence[str],
    *,
    release_profile: Path | None = None,
) -> None:
    """Fail closed when a packaged stable release is given another DSH line.

    Development checkouts intentionally have no ``release_profile.json`` and
    can exercise future compatibility adapters. The clean public exporter
    writes the immutable profile into the package, making the published wheel
    accept only its declared runtime pair.
    """

    explicit_profile = release_profile is not None
    profile_path = (release_profile or _DEFAULT_RELEASE_PROFILE).expanduser()
    if not profile_path.exists() and not explicit_profile:
        return
    if profile_path.is_symlink() or not profile_path.is_file():
        raise ValueError("packaged release profile is missing or unsafe")
    try:
        if profile_path.stat().st_size > _MAX_RELEASE_PROFILE_BYTES:
            raise ValueError("packaged release profile exceeds its size limit")
        payload = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("packaged release profile is invalid") from error
    if not isinstance(payload, dict) or payload.get("release_channel") != "stable":
        raise ValueError("packaged release profile is invalid")
    packaged_xdsh_version = payload.get("xdsh_version")
    if packaged_xdsh_version is not None and (
        not isinstance(packaged_xdsh_version, str)
        or not versions_align(packaged_xdsh_version, XDSH_VERSION)
    ):
        raise ValueError("packaged release profile does not match the XDSH version")
    raw_versions = payload.get("supported_dsh_runtime_versions")
    if (
        not isinstance(raw_versions, list)
        or not raw_versions
        or len(raw_versions) > 8
        or any(not isinstance(version, str) or not version for version in raw_versions)
    ):
        raise ValueError("packaged release profile has no valid DSH runtime allowlist")
    runtime = classify_runtime_command(command)
    if runtime.mode is RuntimeMode.UNKNOWN:
        raise ValueError("stable XDSH requires an official runtime launcher")
    if runtime.version not in frozenset(raw_versions):
        reported = runtime.version or "unknown"
        raise ValueError(f"stable XDSH does not support DSH runtime {reported}")
    if runtime.version is None or not versions_align(runtime.version, DSH_COMPATIBLE_VERSION):
        raise ValueError("stable XDSH requires an XDSH-aligned DSH runtime")


def append_profile_patch(command: Sequence[str], patch_path: Path) -> tuple[str, ...]:
    """Append one deterministic alpha profile overlay exactly once."""

    argv = tuple(command)
    rendered = str(patch_path.expanduser().resolve())
    for index, item in enumerate(argv[:-1]):
        if item == "--patch" and argv[index + 1] == rendered:
            return argv
    return (*argv, "--patch", rendered)


def render_profile_settings(
    *,
    provider: str,
    model: str,
    base_url: str,
    api_key_env: str,
    context_window: int,
    model_max_tokens: int,
    reasoning_effort: ReasoningEffort,
    max_parallel_tool_calls: int,
    rate_limit_policy: RateLimitPolicy | None = None,
    stream_idle_timeout_seconds: float = 120.0,
) -> str:
    """Render an alpha profile settings document without embedding secrets.

    The alpha profile's settings-file provider intentionally wins over
    composition-time ``llm-pi-ai`` config.  The generated document therefore
    carries only a credential *environment name*; the value is inherited by
    the official child process at launch time.
    """

    _validate_route_inputs(
        provider=provider,
        model=model,
        base_url=base_url,
        api_key_env=api_key_env,
        context_window=context_window,
        model_max_tokens=model_max_tokens,
        reasoning_effort=reasoning_effort,
    )
    parallel_limit = validate_parallel_tool_limit(max_parallel_tool_calls)
    if not 30.0 <= stream_idle_timeout_seconds <= 3_600.0:
        raise ValueError(
            "stream_idle_timeout_seconds must be between 30 and 3600"
        )
    policy = rate_limit_policy or RateLimitPolicy()
    lines = [
        "llm-pi-ai:",
        "  providers:",
        f"    {_yaml_quote(provider)}:",
        "      displayName: \"XDSH LiteLLM Anthropic gateway\"",
        f"      apiKeyEnv: {_yaml_quote(api_key_env)}",
        "      api: \"anthropic-messages\"",
        f"      baseURL: {_yaml_quote(base_url)}",
        f"      streamIdleTimeoutMs: {_milliseconds(stream_idle_timeout_seconds)}",
        "      retryPolicy:",
        "        mode: normal",
        f"        maxRetries: {policy.max_rate_limit_retries}",
        "        retryableCodes:",
        *(f"          - {code}" for code in _DSH_RETRYABLE_CODES),
        "        backoff:",
        f"          initialDelayMs: {_milliseconds(policy.base_backoff_seconds)}",
        f"          maxDelayMs: {_milliseconds(policy.max_backoff_seconds)}",
        f"          jitterRatio: {policy.jitter_ratio:.6f}",
        f"      reasoning: {_yaml_quote(reasoning_effort)}",
        "      thinkingBudgets:",
        "        minimal: 1024",
        "        low: 2048",
        "        medium: 8192",
        "        high: 16384",
        "      models:",
        f"        - id: {_yaml_quote(model)}",
        f"          contextWindow: {context_window}",
        f"          maxTokens: {model_max_tokens}",
        "          reasoningEfforts:",
    ]
    for effort in REASONING_EFFORTS:
        if effort == "off":
            lines.append("            off:")
        else:
            lines.append(f"            {effort}: {effort}")
    lines.extend(
        (
            "agent-loop:",
            f"  maxParallelToolCalls: {parallel_limit}",
        )
    )
    return "\n".join(lines) + "\n"


def render_profile_patch(
    settings_path: Path,
    *,
    system_prompt: str,
    workspace_root: Path,
    auto_compaction_enabled: bool,
    native_compaction_enabled: bool,
    threshold_ratio: float,
    retain_ratio: float,
) -> str:
    """Render a static, bounded overlay for the alpha ``sdk`` profile."""

    if not system_prompt.strip():
        raise ValueError("system_prompt must be non-empty")
    if not 0.0 < threshold_ratio < 1.0 or not 0.0 < retain_ratio < threshold_ratio:
        raise ValueError("invalid compaction ratios")
    resolved_settings = settings_path.expanduser().resolve()
    resolved_workspace = workspace_root.expanduser().resolve()
    lines = [
        "- id: settings",
        "  config:",
        f"    path: {_yaml_quote(str(resolved_settings))}",
        "- id: system-prompt",
        "  config:",
        f"    persona: {_yaml_quote(system_prompt)}",
        "- id: sandbox-policy",
        "  config:",
        "    mode: \"workspace-write\"",
        f"    workspaceRoot: {_yaml_quote(str(resolved_workspace))}",
        "- id: bash-sandbox",
        "  config:",
        "    timeoutMs: 300000",
        "- id: compaction-basic",
        "  config:",
        f"    thresholdRatio: {threshold_ratio:.6f}",
        f"    retainRatio: {retain_ratio:.6f}",
        "    maxTokens: 8192",
        "    compactionRetries: 1",
        "    maxOverflowRetries: 1",
        f"    auto: {'true' if auto_compaction_enabled and native_compaction_enabled else 'false'}",
    ]
    return "\n".join(lines) + "\n"


def materialize_profile_files(
    *,
    dsh_home: Path,
    provider: str,
    model: str,
    base_url: str,
    api_key_env: str,
    context_window: int,
    model_max_tokens: int,
    reasoning_effort: ReasoningEffort,
    max_parallel_tool_calls: int,
    system_prompt: str,
    workspace_root: Path,
    auto_compaction_enabled: bool,
    native_compaction_enabled: bool,
    threshold_ratio: float,
    retain_ratio: float,
    rate_limit_policy: RateLimitPolicy | None = None,
    stream_idle_timeout_seconds: float = 120.0,
) -> ProfileFiles:
    """Atomically write the generated settings and patch below ``dsh_home``.

    Existing files are overwritten only when they carry XDSH's ownership
    marker.  This prevents an operator's unrelated file from being clobbered
    while allowing a later turn to refresh model or workspace settings.
    """

    home = dsh_home.expanduser().resolve()
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    settings_path = home / _PROFILE_SETTINGS_NAME
    patch_path = home / _PROFILE_PATCH_NAME
    settings = _MANAGED_HEADER + render_profile_settings(
        provider=provider,
        model=model,
        base_url=base_url,
        api_key_env=api_key_env,
        context_window=context_window,
        model_max_tokens=model_max_tokens,
        reasoning_effort=reasoning_effort,
        max_parallel_tool_calls=max_parallel_tool_calls,
        rate_limit_policy=rate_limit_policy,
        stream_idle_timeout_seconds=stream_idle_timeout_seconds,
    )
    patch = _MANAGED_HEADER + render_profile_patch(
        settings_path,
        system_prompt=system_prompt,
        workspace_root=workspace_root,
        auto_compaction_enabled=auto_compaction_enabled,
        native_compaction_enabled=native_compaction_enabled,
        threshold_ratio=threshold_ratio,
        retain_ratio=retain_ratio,
    )
    _write_managed_file(settings_path, settings)
    _write_managed_file(patch_path, patch)
    return ProfileFiles(settings_path=settings_path, patch_path=patch_path)


def _validate_route_inputs(
    *,
    provider: str,
    model: str,
    base_url: str,
    api_key_env: str,
    context_window: int,
    model_max_tokens: int,
    reasoning_effort: ReasoningEffort,
) -> None:
    if not provider.strip() or not model.strip():
        raise ValueError("provider and model must be non-empty")
    if _ENV_NAME.fullmatch(api_key_env) is None:
        raise ValueError("api_key_env must be a valid environment variable name")
    if context_window <= 0 or model_max_tokens <= 0 or model_max_tokens >= context_window:
        raise ValueError("model token capacities are invalid")
    if reasoning_effort not in REASONING_EFFORTS:
        raise ValueError("reasoning_effort is unsupported")
    parsed = urlsplit(base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in _LOOPBACK_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("base_url must be a credential-free loopback root URL")
    try:
        _ = parsed.port
    except ValueError as error:
        raise ValueError("base_url has an invalid port") from error


def _yaml_quote(value: str) -> str:
    """Use JSON strings, a strict YAML 1.2 scalar subset."""

    return json.dumps(value, ensure_ascii=False)


def _milliseconds(seconds: float) -> int:
    """Render a validated policy duration in the integer unit DSH expects."""

    return max(1, round(seconds * 1000))


def _write_managed_file(path: Path, content: str) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f"refusing to replace non-regular profile file: {path}")
    if path.exists():
        try:
            first_line = path.read_text(encoding="utf-8", errors="strict").splitlines(
                keepends=True
            )[:1]
        except (OSError, UnicodeError) as error:
            raise ValueError(f"cannot inspect profile file: {path}") from error
        if first_line != [_MANAGED_HEADER]:
            raise ValueError(f"refusing to replace an unmanaged profile file: {path}")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def _profile_argument(argv: Sequence[str]) -> str | None:
    for index, item in enumerate(argv):
        if item == "--profile" and index + 1 < len(argv):
            value = argv[index + 1].strip()
            return value or None
        if item.startswith("--profile="):
            value = item.partition("=")[2].strip()
            return value or None
    return None


def _package_metadata(
    argv: Sequence[str],
    *,
    expected: str,
) -> tuple[str | None, str | None]:
    for raw in argv:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            continue
        for parent in (candidate.parent, *candidate.parents):
            if "@deepseek-ai" not in parent.parts:
                continue
            package_json = parent / "package.json"
            try:
                payload = json.loads(package_json.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            name = payload.get("name")
            if name != expected:
                continue
            version = payload.get("version")
            return name, version if isinstance(version, str) else None
    return None, None


__all__ = [
    "ALPHA2_DSH_NPM_VERSION",
    "LEGACY_DSH_NPM_VERSION",
    "VALIDATED_DSH_NPM_VERSIONS",
    "DshRuntimeSpec",
    "ProfileFiles",
    "RuntimeCompatibilityReport",
    "RuntimeCompatibilityStatus",
    "RuntimeMode",
    "append_profile_patch",
    "classify_runtime_command",
    "materialize_profile_files",
    "render_profile_patch",
    "render_profile_settings",
    "runtime_compatibility_report",
    "validate_packaged_release_runtime",
]
