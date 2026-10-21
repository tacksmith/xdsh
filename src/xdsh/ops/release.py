"""Build and audit a clean, public XDSH source bundle.

The repository contains operational handoff material and deployment artifacts
that are useful to the maintainers but should not be shipped to users.  The
exporter copies only the package, a curated public contract suite, optional
maintainer tests, public documentation, and sanitized examples into an explicit
staging directory.  It never publishes or contacts a package registry.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from xdsh import __version__
from xdsh.core.product_control import safe_exception_text, safe_outbound_text
from xdsh.version import (
    DSH_COMPATIBLE_VERSION,
    DSH_PYTHON_SDK_VERSION,
    XDSH_RELEASE_TAG,
    XDSH_VERSION_DISPLAY,
    versions_align,
)

MAX_FILES = 2_000
MAX_TOTAL_BYTES = 50 * 1024 * 1024
_MAX_AUDIT_MESSAGES = 64
_MAX_AUDIT_MESSAGE_CHARS = 400
_MAX_AUDIT_TOTAL_CHARS = 12_000
_TEXT_SUFFIXES = frozenset(
    {".lock", ".md", ".py", ".toml", ".json", ".yml", ".yaml", ".sh", ".txt"}
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RC2_RELEASE_CONTRACT: dict[str, object] = {
    "release": XDSH_RELEASE_TAG,
    "release_channel": "stable",
    "publication_order": 2,
    "xdsh_version": __version__,
    "xdsh_version_display": XDSH_VERSION_DISPLAY,
    "version_alignment": "XDSH and DSH use the same 0.1.1-rc.2 release line",
    "dsh_runtime_version": DSH_COMPATIBLE_VERSION,
    "supported_dsh_runtime_versions": [DSH_COMPATIBLE_VERSION],
    "python_sdk_version": DSH_PYTHON_SDK_VERSION,
}
_PUBLIC_TEST_PROFILE = "core-contracts-v1"
_PUBLIC_TEST_FILES = (
    "tests/unit/test_admission.py",
    "tests/unit/test_answer_contract.py",
    "tests/unit/test_artifact_acceptance.py",
    "tests/unit/test_numeric_acceptance.py",
    "tests/unit/test_method_evidence.py",
    "tests/unit/test_stage_evidence.py",
    "tests/unit/test_stage_delivery.py",
    "tests/unit/test_large_artifacts.py",
    "tests/unit/test_hdf_artifacts.py",
    "tests/unit/test_research_acceptance.py",
    "tests/unit/test_research_preflight.py",
    "tests/unit/test_research_recovery.py",
    "tests/unit/test_task_contract.py",
    "tests/unit/test_model_availability.py",
    "tests/unit/test_retrieval_regressions.py",
    "tests/unit/test_model_gateway_lifecycle.py",
    "tests/unit/test_gateway_timeout_delivery.py",
    "tests/unit/test_gateway_stream_recovery.py",
    "tests/unit/test_stream_termination.py",
    "tests/unit/test_provider_idle_policy.py",
    "tests/unit/test_reproduction_regressions.py",
    "tests/unit/test_task_handoff.py",
    "tests/unit/test_config_ui.py",
    "tests/unit/test_context.py",
    "tests/unit/test_dsh_events.py",
    "tests/unit/test_execution_policy.py",
    "tests/unit/test_health_trends.py",
    "tests/unit/test_i18n.py",
    "tests/unit/test_knowledge.py",
    "tests/unit/test_metrics.py",
    "tests/unit/test_models.py",
    "tests/unit/test_provider_presets.py",
    "tests/unit/test_speech.py",
    "tests/unit/test_telegram_delivery_retry.py",
    "tests/unit/test_public_web.py",
    "tests/unit/test_plugins.py",
    "tests/unit/test_queue_policy.py",
    "tests/unit/test_rate_limit_probe.py",
    "tests/unit/test_rate_limit.py",
    "tests/unit/test_run_guard.py",
    "tests/unit/test_guard_terminal_evidence.py",
    "tests/unit/test_sdk_compat.py",
    "tests/unit/test_sessions.py",
    "tests/unit/test_settings.py",
    "tests/unit/test_telegram_commands.py",
    "tests/unit/test_telegram_pooled.py",
    "tests/unit/test_version.py",
)
_FORBIDDEN_PARTS = frozenset(
    {
        ".git",
        ".venv",
        ".uv-cache",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "__pycache__",
        "artifacts",
        "benchmarks",
        "deploy",
        "dist",
        "dsh-home",
        "HANDOFF.md",
        "CONTEXT.md",
        "AGENTS.md",
        "research",
    }
)
_SECRET_PATTERNS = (
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"(?i)(?:^|[=:\s])(?:sk|key|token)-[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(
        r"(?i)(?:api[_-]?key|access[_-]?token|password)\s*[:=]\s*['\"]?"
        r"(?:sk-|key-|token-|bearer\s+|eyJ|AKIA)[A-Za-z0-9._~+/=-]{12,}"
    ),
)
_ABSOLUTE_HOME_PATTERN = re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+")


@dataclass(frozen=True, slots=True)
class ReleaseAudit:
    """Bounded result of auditing a staging tree."""

    root: Path
    passed: bool
    files: tuple[str, ...]
    total_bytes: int
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    manifest_sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "xdsh_version": __version__,
            "root": self.root.name,
            "passed": self.passed,
            "file_count": len(self.files),
            "total_bytes": self.total_bytes,
            "files": list(self.files),
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "manifest_sha256": self.manifest_sha256,
        }


def audit_public_tree(root: Path, *, strict: bool = False) -> ReleaseAudit:
    """Audit a staged tree for private artifacts, credentials, and bounds."""

    raw_root = root.expanduser()
    if raw_root.is_symlink():
        resolved = raw_root.resolve()
        return _audit_result(resolved, (), 0, ["staging root is a symlink"], [])
    resolved = raw_root.resolve()
    errors: list[str] = []
    warnings: list[str] = []
    files: list[str] = []
    total_bytes = 0
    if not resolved.is_dir():
        errors.append("staging root is not a directory")
        return _audit_result(resolved, files, total_bytes, errors, warnings)

    for current, directories, names in os.walk(resolved, followlinks=False):
        current_path = Path(current)
        directories[:] = sorted(directories)
        names = sorted(names)
        for directory in directories:
            directory_path = current_path / directory
            relative = directory_path.relative_to(resolved).as_posix()
            if directory_path.is_symlink():
                errors.append(f"symlink directory: {relative}")
            if directory in _FORBIDDEN_PARTS:
                errors.append(f"private directory included: {relative}")
        for name in names:
            path = current_path / name
            relative = path.relative_to(resolved).as_posix()
            if path.is_symlink():
                errors.append(f"symlink file: {relative}")
                continue
            if any(part in _FORBIDDEN_PARTS for part in path.relative_to(resolved).parts):
                errors.append(f"private path included: {relative}")
                continue
            if name.endswith(".env") and not name.endswith(".env.example"):
                errors.append(f"credential environment file included: {relative}")
            try:
                size = path.stat().st_size
            except OSError as error:
                errors.append(f"cannot stat {relative}: {type(error).__name__}")
                continue
            total_bytes += size
            files.append(relative)
            if len(files) > MAX_FILES:
                errors.append(f"file count exceeds {MAX_FILES}")
                break
            if size > 2 * 1024 * 1024:
                errors.append(f"text file exceeds scan bound: {relative}")
            else:
                try:
                    content = path.read_text(
                        encoding="utf-8",
                        errors="strict" if path.suffix.lower() in _TEXT_SUFFIXES else "replace",
                    )
                except (OSError, UnicodeError) as error:
                    errors.append(f"cannot read {relative}: {type(error).__name__}")
                    continue
                for pattern in _SECRET_PATTERNS:
                    if pattern.search(content):
                        errors.append(f"credential-like value in {relative}")
                        break
                if _ABSOLUTE_HOME_PATTERN.search(content):
                    errors.append(f"machine-specific absolute home path in {relative}")
        if len(files) > MAX_FILES:
            break
    if total_bytes > MAX_TOTAL_BYTES:
        errors.append(f"staging tree exceeds {MAX_TOTAL_BYTES} bytes")

    required = {
        ".github/workflows/ci.yml",
        "CODE_OF_CONDUCT.md",
        "CONTRIBUTING.md",
        "README.md",
        "SUPPORT.md",
        "pyproject.toml",
        "src/xdsh/__init__.py",
        "docs/SECURITY.md",
        "docs/public/GITHUB-CI.yml",
        "examples/telegram.env.example",
        "examples/settings.json.example",
        "docs/public/examples/settings.json.example",
        "uv.lock",
    }
    required.update(_PUBLIC_TEST_FILES)
    missing = sorted(required.difference(files))
    errors.extend(f"required public file missing: {path}" for path in missing)
    if not any(Path(item).name.startswith("LICENSE") for item in files):
        warnings.append("no LICENSE file is present; choose a license before publication")
    if strict and warnings:
        errors.extend(f"strict release warning: {warning}" for warning in warnings)
    return _audit_result(resolved, files, total_bytes, errors, warnings)


def export_public_release(
    source_root: Path,
    destination: Path,
    *,
    include_tests: bool = False,
    replace: bool = False,
    license_path: Path | None = None,
) -> ReleaseAudit:
    """Export a clean staging tree and return its audit result."""

    raw_source = source_root.expanduser()
    raw_target = destination.expanduser()
    if raw_source.is_symlink() or raw_target.is_symlink():
        raise ValueError("source and destination must not be symlinks")
    source = raw_source.resolve()
    target = raw_target.resolve()
    if not source.is_dir():
        raise ValueError("source root is not a directory")
    if target == source or source.is_relative_to(target):
        raise ValueError("destination must not contain or equal the source root")
    if target.exists():
        if not replace and any(target.iterdir()):
            raise ValueError("destination is non-empty; pass --replace explicitly")
        if replace:
            shutil.rmtree(target)
    target.mkdir(mode=0o700, parents=True, exist_ok=True)

    _copy_tree(source / "src" / "xdsh", target / "src" / "xdsh")
    release_contract = _load_rc2_release_contract(source)
    for relative in _PUBLIC_TEST_FILES:
        _copy_file(source / relative, target / relative)
    if include_tests and (source / "tests").is_dir():
        _copy_tree(source / "tests", target / "tests")
    _copy_file(source / "pyproject.toml", target / "pyproject.toml")
    _copy_file(source / "uv.lock", target / "uv.lock")
    _apply_rc2_publication_profile(target, release_contract)
    _copy_file(source / "docs" / "public" / "README.md", target / "README.md")
    # The same public README is also kept under ``docs/public``.  Its source
    # links are correct there, but would escape the bundle when rendered from
    # the root copy; rewrite only that relative path in the root copy.
    root_readme = target / "README.md"
    root_readme.write_text(
        root_readme.read_text(encoding="utf-8").replace(
            "](../../scripts/wsl_install.sh)", "](scripts/wsl_install.sh)"
        ).replace(
            "](../../LICENSE)", "](LICENSE)"
        ),
        encoding="utf-8",
    )
    root_readme.chmod(0o644)
    _copy_file(
        source / "docs" / "public" / "README.md",
        target / "docs" / "public" / "README.md",
    )
    for readme_name in ("README.zh-CN.md", "README.zh-TW.md"):
        localized_readme = source / "docs" / "public" / readme_name
        if localized_readme.is_file():
            _copy_file(localized_readme, target / readme_name)
            _copy_file(localized_readme, target / "docs" / "public" / readme_name)
            root_localized_readme = target / readme_name
            root_localized_readme.write_text(
                root_localized_readme.read_text(encoding="utf-8").replace(
                    "](../../LICENSE)", "](LICENSE)"
                ),
                encoding="utf-8",
            )
    _copy_file(source / "docs" / "public" / "SECURITY.md", target / "docs" / "SECURITY.md")
    _copy_file(source / "docs" / "public" / "SECURITY.md", target / "SECURITY.md")
    _copy_file(
        source / "docs" / "public" / "SECURITY.md",
        target / "docs" / "public" / "SECURITY.md",
    )
    if (source / "docs" / "public" / "UPGRADE.md").is_file():
        _copy_file(
            source / "docs" / "public" / "UPGRADE.md",
            target / "docs" / "public" / "UPGRADE.md",
        )
        _copy_file(source / "docs" / "public" / "UPGRADE.md", target / "UPGRADE.md")
    if (source / "docs" / "public" / "RELEASE-MATRIX.md").is_file():
        _copy_file(
            source / "docs" / "public" / "RELEASE-MATRIX.md",
            target / "docs" / "public" / "RELEASE-MATRIX.md",
        )
        _copy_file(
            source / "docs" / "public" / "RELEASE-MATRIX.md",
            target / "RELEASE-MATRIX.md",
        )
    if (source / "docs" / "public" / "CONTAINER.md").is_file():
        _copy_file(
            source / "docs" / "public" / "CONTAINER.md",
            target / "docs" / "CONTAINER.md",
        )
        _copy_file(
            source / "docs" / "public" / "CONTAINER.md",
            target / "docs" / "public" / "CONTAINER.md",
        )
        _copy_file(source / "docs" / "public" / "CONTAINER.md", target / "CONTAINER.md")
    for public_name in (
        "PROVIDERS.md",
        "PROVIDER_FAKE_IP.md",
        "KEEP_AWAKE.md",
        "LONG_ANSWERS.md",
        "LONG_TASKS.md",
        "ACCEPTANCE.md",
        "SPEECH.md",
        "CHANNELS.md",
        "WSL.md",
        "COMPATIBILITY.md",
        "COMPATIBILITY.zh-CN.md",
        "COMPATIBILITY.zh-TW.md",
        "VERSIONING.md",
        "RC2-BASELINE.md",
        "RELEASE-NOTES-0.1.1-rc.2.md",
        "rc2-baseline.json",
        "CONTRIBUTING.md",
        "CODE_OF_CONDUCT.md",
        "SUPPORT.md",
        "GITHUB-CI.yml",
    ):
        public_doc = source / "docs" / "public" / public_name
        if public_doc.is_file():
            _copy_file(public_doc, target / "docs" / "public" / public_name)
            if public_name == "GITHUB-CI.yml":
                _copy_file(public_doc, target / ".github" / "workflows" / "ci.yml")
            else:
                _copy_file(public_doc, target / public_name)
    _copy_file(
        source / "examples" / "telegram.env.example",
        target / "examples" / "telegram.env.example",
    )
    if (source / "docs" / "public" / "examples" / "telegram.env.example").is_file():
        _copy_file(
            source / "docs" / "public" / "examples" / "telegram.env.example",
            target / "docs" / "public" / "examples" / "telegram.env.example",
        )
    if (source / "examples" / "channels.env.example").is_file():
        _copy_file(
            source / "examples" / "channels.env.example",
            target / "examples" / "channels.env.example",
        )
    _copy_file(
        source / "examples" / "settings.json.example",
        target / "examples" / "settings.json.example",
    )
    if (source / "docs" / "public" / "examples" / "settings.json.example").is_file():
        _copy_file(
            source / "docs" / "public" / "examples" / "settings.json.example",
            target / "docs" / "public" / "examples" / "settings.json.example",
        )
    _copy_file(source / ".gitignore", target / ".gitignore")
    if (source / "scripts" / "wsl_install.sh").is_file():
        installer_target = target / "scripts" / "wsl_install.sh"
        _copy_file(source / "scripts" / "wsl_install.sh", installer_target)
        # Keep the field installer directly executable in a public export;
        # ordinary documentation/files remain non-executable and mode-safe.
        installer_target.chmod(0o755)
    selected_license = license_path.expanduser() if license_path is not None else source / "LICENSE"
    if selected_license.is_file():
        _copy_file(selected_license, target / "LICENSE")
    notice = source / "NOTICE"
    if notice.is_file():
        _copy_file(notice, target / "NOTICE")

    audit = audit_public_tree(target)
    manifest = {
        "schema_version": 1,
        **release_contract,
        "public_test_profile": _PUBLIC_TEST_PROFILE,
        "public_test_file_count": len(_PUBLIC_TEST_FILES),
        "manifest": "PUBLIC_RELEASE.json",
        "manifest_excludes_self": True,
        "file_count": len(audit.files),
        "total_bytes": audit.total_bytes,
        "files": [
            {"path": path, "sha256": _sha256(target / path)}
            for path in audit.files
        ],
        "excluded": [
            "HANDOFF.md",
            "CONTEXT.md",
            "AGENTS.md",
            "artifacts/",
            "deploy/",
            "docs/research/",
            "maintainer-only tests/ (unless --include-tests is explicitly used)",
        ],
    }
    manifest_path = target / "PUBLIC_RELEASE.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    manifest_path.chmod(0o644)
    return audit_public_tree(target)


def verify_public_manifest(root: Path, *, strict: bool = False) -> ReleaseAudit:
    """Verify a generated ``PUBLIC_RELEASE.json`` and every listed file.

    The audit is deliberately performed before reading the manifest so a
    symlink, private path, credential-like value, or size violation remains a
    hard failure even when a manifest is syntactically valid.  The manifest
    itself is excluded from its own file list to avoid a self-referential hash.
    """

    raw_root = root.expanduser()
    audit = audit_public_tree(raw_root, strict=strict)
    errors = list(audit.errors)
    warnings = list(audit.warnings)
    if raw_root.is_symlink():
        errors.append("staging root is a symlink")
    manifest_path = audit.root / "PUBLIC_RELEASE.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        errors.append("PUBLIC_RELEASE.json is missing or unsafe")
        return _audit_result(audit.root, audit.files, audit.total_bytes, errors, warnings)
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        errors.append(f"PUBLIC_RELEASE.json is not valid JSON: {type(error).__name__}")
        return _audit_result(audit.root, audit.files, audit.total_bytes, errors, warnings)
    if not isinstance(payload, dict):
        errors.append("PUBLIC_RELEASE.json must contain an object")
        return _audit_result(audit.root, audit.files, audit.total_bytes, errors, warnings)

    if payload.get("schema_version") != 1:
        errors.append("PUBLIC_RELEASE.json has an unsupported schema version")
    try:
        release_contract = _load_rc2_release_contract(audit.root)
    except ValueError as error:
        errors.append(safe_exception_text(error, max_chars=600))
    else:
        for field, expected_value in release_contract.items():
            if payload.get(field) != expected_value:
                errors.append(f"PUBLIC_RELEASE.json release contract mismatch: {field}")
    public_contract = {
        "public_test_profile": _PUBLIC_TEST_PROFILE,
        "public_test_file_count": len(_PUBLIC_TEST_FILES),
    }
    for field, expected_value in public_contract.items():
        if payload.get(field) != expected_value:
            errors.append(f"PUBLIC_RELEASE.json public test contract mismatch: {field}")
    manifest_name = payload.get("manifest")
    if manifest_name is not None and manifest_name != "PUBLIC_RELEASE.json":
        errors.append("PUBLIC_RELEASE.json names an unexpected manifest file")

    entries = payload.get("files")
    listed: dict[str, str] = {}
    if not isinstance(entries, list):
        errors.append("PUBLIC_RELEASE.json files must be a list")
        entries = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            errors.append(f"PUBLIC_RELEASE.json entry {index} is not an object")
            continue
        path_value = entry.get("path")
        digest_value = entry.get("sha256")
        safe_path = _manifest_path(path_value)
        if safe_path is None:
            errors.append(f"PUBLIC_RELEASE.json entry {index} has an unsafe path")
            continue
        if safe_path in listed:
            errors.append(f"PUBLIC_RELEASE.json repeats {safe_path}")
            continue
        if not isinstance(digest_value, str) or _SHA256_RE.fullmatch(digest_value) is None:
            errors.append(f"PUBLIC_RELEASE.json entry {index} has an invalid SHA-256")
            continue
        if safe_path == "PUBLIC_RELEASE.json":
            errors.append("PUBLIC_RELEASE.json must not hash itself")
            continue
        listed[safe_path] = digest_value

    expected = set(audit.files)
    expected.discard("PUBLIC_RELEASE.json")
    listed_paths = set(listed)
    if listed_paths != expected:
        missing = ", ".join(sorted(expected - listed_paths)[:3]) or "none"
        extra = ", ".join(sorted(listed_paths - expected)[:3]) or "none"
        errors.append(f"PUBLIC_RELEASE.json file set differs (missing={missing}; extra={extra})")

    declared_count = payload.get("file_count")
    if isinstance(declared_count, bool) or not isinstance(declared_count, int):
        errors.append("PUBLIC_RELEASE.json file_count is not an integer")
    elif declared_count != len(listed):
        errors.append("PUBLIC_RELEASE.json file_count does not match entries")

    declared_bytes = payload.get("total_bytes")
    actual_bytes = 0
    if isinstance(declared_bytes, bool) or not isinstance(declared_bytes, int):
        errors.append("PUBLIC_RELEASE.json total_bytes is not an integer")
    for relative, expected_digest in sorted(listed.items()):
        path = audit.root.joinpath(*PurePosixPath(relative).parts)
        if path.is_symlink() or not path.is_file():
            errors.append(f"PUBLIC_RELEASE.json lists a missing or unsafe file: {relative}")
            continue
        try:
            actual_bytes += path.stat().st_size
            actual_digest = _sha256(path)
        except OSError as error:
            errors.append(f"cannot verify {relative}: {type(error).__name__}")
            continue
        if actual_digest != expected_digest:
            errors.append(f"SHA-256 mismatch: {relative}")
    if isinstance(declared_bytes, int) and not isinstance(declared_bytes, bool):
        if declared_bytes != actual_bytes:
            errors.append("PUBLIC_RELEASE.json total_bytes does not match entries")
    return _audit_result(audit.root, audit.files, audit.total_bytes, errors, warnings)


def _manifest_path(value: object) -> str | None:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        return None
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or ".." in candidate.parts or "." in candidate.parts:
        return None
    normalized = candidate.as_posix()
    if normalized != value or not normalized:
        return None
    return normalized


def _load_rc2_release_contract(root: Path) -> dict[str, object]:
    candidates = (
        root / "docs" / "public" / "rc2-baseline.json",
        root / "rc2-baseline.json",
    )
    baseline_path = next(
        (
            candidate
            for candidate in candidates
            if candidate.is_file() and not candidate.is_symlink()
        ),
        None,
    )
    if baseline_path is None:
        raise ValueError("RC2 release contract is missing")
    try:
        payload = json.loads(baseline_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"RC2 release contract is invalid: {type(error).__name__}") from None
    if not isinstance(payload, dict):
        raise ValueError("RC2 release contract must contain an object")
    contract = {field: payload.get(field) for field in _RC2_RELEASE_CONTRACT}
    if contract != _RC2_RELEASE_CONTRACT:
        raise ValueError("RC2 release contract does not match the stable first-publication lock")
    if contract["xdsh_version"] != __version__:
        raise ValueError("RC2 release contract does not match the XDSH package version")
    if not versions_align(
        str(contract["xdsh_version"]),
        str(contract["dsh_runtime_version"]),
    ):
        raise ValueError("RC2 release contract must align XDSH and DSH versions")
    return contract


def _copy_file(source: Path, target: Path) -> None:
    if not source.is_file() or source.is_symlink():
        raise ValueError(f"required source file is missing or unsafe: {source.name}")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    target.chmod(0o644)


def _apply_rc2_publication_profile(
    target: Path,
    release_contract: dict[str, object],
) -> None:
    """Make the clean public tree fail closed outside its one RC2 pairing."""

    profile = {
        "schema_version": 1,
        "release": release_contract["release"],
        "release_channel": release_contract["release_channel"],
        "xdsh_version": release_contract["xdsh_version"],
        "xdsh_version_display": release_contract["xdsh_version_display"],
        "version_alignment": release_contract["version_alignment"],
        "dsh_runtime_version": release_contract["dsh_runtime_version"],
        "supported_dsh_runtime_versions": release_contract[
            "supported_dsh_runtime_versions"
        ],
    }
    profile_path = target / "src" / "xdsh" / "release_profile.json"
    profile_path.write_text(
        json.dumps(profile, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    profile_path.chmod(0o644)

    canary_path = target / "src" / "xdsh" / "ops" / "canary.py"
    if canary_path.is_file() and not canary_path.is_symlink():
        canary_path.unlink()
    pyproject_path = target / "pyproject.toml"
    pyproject = pyproject_path.read_text(encoding="utf-8")
    pyproject_path.write_text(
        "\n".join(
            line for line in pyproject.splitlines() if not line.startswith("xdsh-canary =")
        )
        + "\n",
        encoding="utf-8",
    )
    pyproject_path.chmod(0o644)


def _copy_tree(source: Path, target: Path) -> None:
    if not source.is_dir() or source.is_symlink():
        raise ValueError(f"required source directory is missing or unsafe: {source.name}")
    for current, directories, names in os.walk(source, followlinks=False):
        current_path = Path(current)
        relative = current_path.relative_to(source)
        destination_dir = target / relative
        destination_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
        kept_directories: list[str] = []
        for directory in directories:
            source_directory = current_path / directory
            if source_directory.is_symlink():
                raise ValueError(f"required source tree contains a symlink: {directory}")
            if directory.startswith("__pycache__") or directory in {".git", ".pytest_cache"}:
                continue
            kept_directories.append(directory)
        directories[:] = kept_directories
        for name in names:
            source_path = current_path / name
            if source_path.is_symlink():
                raise ValueError(f"required source tree contains a symlink: {name}")
            if name.endswith(".pyc"):
                continue
            _copy_file(source_path, destination_dir / name)


def _audit_result(
    root: Path,
    files: Iterable[str],
    total_bytes: int,
    errors: list[str],
    warnings: list[str],
) -> ReleaseAudit:
    ordered = tuple(sorted(set(files)))
    canonical = "\n".join(ordered).encode("utf-8")
    bounded_errors = _bounded_messages(errors, label="errors")
    bounded_warnings = _bounded_messages(warnings, label="warnings")
    return ReleaseAudit(
        root=root,
        passed=not errors,
        files=ordered,
        total_bytes=total_bytes,
        errors=bounded_errors,
        warnings=bounded_warnings,
        manifest_sha256=hashlib.sha256(canonical).hexdigest(),
    )


def _bounded_messages(messages: Iterable[str], *, label: str) -> tuple[str, ...]:
    """Keep release diagnostics useful without allowing an error storm."""

    result: list[str] = []
    used = 0
    omitted = 0
    for message in messages:
        safe = safe_outbound_text(str(message), max_chars=_MAX_AUDIT_MESSAGE_CHARS)
        if not safe:
            continue
        if (
            len(result) >= _MAX_AUDIT_MESSAGES
            or used + len(safe) + 1 > _MAX_AUDIT_TOTAL_CHARS
        ):
            omitted += 1
            continue
        result.append(safe)
        used += len(safe) + 1
    if omitted:
        marker = f"[…{omitted} additional {label} omitted]"
        if len(result) >= _MAX_AUDIT_MESSAGES:
            removed = result.pop()
            used -= len(removed) + 1
        while result and used + len(marker) + 1 > _MAX_AUDIT_TOTAL_CHARS:
            removed = result.pop()
            used -= len(removed) + 1
        if len(result) < _MAX_AUDIT_MESSAGES:
            result.append(marker)
    return tuple(result)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    audit = subparsers.add_parser("audit", help="audit an existing public staging tree")
    audit.add_argument("root", type=Path)
    audit.add_argument("--strict", action="store_true")
    audit.add_argument("--json", action="store_true")
    verify = subparsers.add_parser(
        "verify",
        help="verify PUBLIC_RELEASE.json and the content hashes it records",
    )
    verify.add_argument("root", type=Path)
    verify.add_argument("--strict", action="store_true")
    verify.add_argument("--json", action="store_true")
    export = subparsers.add_parser("export", help="create and audit a public staging tree")
    export.add_argument("--source", type=Path, default=Path.cwd())
    export.add_argument("--destination", type=Path, required=True)
    export.add_argument(
        "--include-tests",
        action="store_true",
        help="include repository tests only when their credential audit passes",
    )
    export.add_argument("--replace", action="store_true")
    export.add_argument(
        "--license",
        type=Path,
        help="copy an owner-selected license file into the staging tree",
    )
    export.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    try:
        if args.command == "audit":
            result = audit_public_tree(args.root, strict=args.strict)
        elif args.command == "verify":
            result = verify_public_manifest(args.root, strict=args.strict)
        else:
            result = export_public_release(
                args.source,
                args.destination,
                include_tests=args.include_tests,
                replace=args.replace,
                license_path=args.license,
            )
    except (OSError, ValueError) as error:
        raise SystemExit(safe_exception_text(error, max_chars=600)) from None
    if args.json:
        print(json.dumps(result.as_dict(), ensure_ascii=False, sort_keys=True))
    else:
        print(
            f"public release {'passed' if result.passed else 'failed'}: "
            f"files={len(result.files)} bytes={result.total_bytes}"
        )
        for message in (*result.errors, *result.warnings):
            print(f"- {message}")
    if not result.passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
