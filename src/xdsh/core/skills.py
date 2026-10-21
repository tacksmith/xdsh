"""XDSH-owned Skill catalog and safe projection into an isolated DSH_HOME.

The source of truth for product Skills is the XDSH wheel under
``xdsh/skills``. DSH still expects a filesystem tree, so the runner creates a
small, isolated *projection* below its own ``DSH_HOME/skills`` at first use.
That projection is never treated as the source of truth and is never allowed
to overwrite a user-owned Skill.

The manifest and per-file hashes make the bundle reproducible and let a later
XDSH upgrade refresh only files that XDSH itself seeded previously. A user
edit, a symlink, malformed package data, or a path escape fails closed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import cast

from xdsh.core.errors import RunnerConfigurationError

# Keep this explicit rather than discovering arbitrary directories from the
# installed wheel. A release review therefore has a finite, auditable list of
# product-owned Skills, while future additions require a manifest update.
BUILTIN_SKILLS = (
    "evidence-verification-unified",
    "session-workspace-cleanup",
    "xdsh-approval-first-evolution",
    "xdsh-artifact-quality",
    "xdsh-bounded-delegation",
    "xdsh-bounded-investigation",
    "xdsh-context-budget",
    "xdsh-deep-research",
    "xdsh-direct-execution",
    "xdsh-evidence-reporting",
    "xdsh-failure-recovery",
    "xdsh-long-task-control",
    "xdsh-memory-hygiene",
    "xdsh-safe-external-access",
    "xdsh-skill-routing",
    "xdsh-testing-verification",
)

_MANIFEST_NAME = "manifest.json"
_STATE_NAME = ".xdsh-builtin-state.json"
_SCHEMA_VERSION = 1
_SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,31}$")
_BUNDLE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_PROVENANCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_RELATIVE_FILE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}(?:/[A-Za-z0-9][A-Za-z0-9._-]{0,127})*$"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_MANIFEST_BYTES = 128_000
_MAX_SKILLS = 64
_MAX_FILES = 512
_MAX_FILE_BYTES = 512_000
_MAX_BUNDLE_BYTES = 8_000_000
_MAX_STATE_BYTES = 512_000


@dataclass(frozen=True, slots=True, kw_only=True)
class BuiltinSkill:
    """One immutable Skill entry shipped by the XDSH package."""

    name: str
    description: str
    version: str
    files: tuple[tuple[str, str], ...]
    default_enabled: bool = True
    provenance: str = "xdsh"

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or _SKILL_NAME.fullmatch(self.name) is None:
            raise ValueError("built-in Skill name is invalid")
        if not isinstance(self.description, str) or not 1 <= len(self.description) <= 500 or any(
            ord(char) < 32 and char not in "\n\t" for char in self.description
        ):
            raise ValueError("built-in Skill description is invalid")
        if not isinstance(self.version, str) or _VERSION.fullmatch(self.version) is None:
            raise ValueError("built-in Skill version is invalid")
        if not isinstance(self.files, tuple) or not self.files or len(self.files) > _MAX_FILES:
            raise ValueError("built-in Skill file list is invalid")
        pairs = [
            item
            for item in self.files
            if isinstance(item, tuple) and len(item) == 2
        ]
        if len(pairs) != len(self.files):
            raise ValueError("built-in Skill file list is invalid")
        paths = [path for path, _digest in pairs]
        if len(paths) != len(set(paths)) or "SKILL.md" not in paths:
            raise ValueError("built-in Skill must contain one SKILL.md")
        for path, digest in pairs:
            if (
                not isinstance(path, str)
                or _RELATIVE_FILE.fullmatch(path) is None
                or path.startswith(".")
            ):
                raise ValueError("built-in Skill resource path is invalid")
            if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
                raise ValueError("built-in Skill resource hash is invalid")
        if not isinstance(self.default_enabled, bool):
            raise ValueError("built-in Skill enabled flag is invalid")
        if (
            not isinstance(self.provenance, str)
            or _PROVENANCE.fullmatch(self.provenance) is None
        ):
            raise ValueError("built-in Skill provenance is invalid")

    @property
    def file_names(self) -> tuple[str, ...]:
        return tuple(path for path, _digest in self.files)


@dataclass(frozen=True, slots=True, kw_only=True)
class BuiltinSkillInstallReport:
    """Content-free result of projecting the XDSH bundle into DSH_HOME."""

    bundle_version: str
    installed: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    verified: tuple[str, ...] = ()
    preserved_user_files: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        """Whether every bundled Skill has a usable, verified projection."""

        return not self.conflicts


def builtin_skill_catalog() -> tuple[BuiltinSkill, ...]:
    """Read and verify the finite Skill catalog embedded in the XDSH wheel."""

    manifest, _version = _load_manifest()
    entries: list[BuiltinSkill] = []
    seen: set[str] = set()
    raw_skills = manifest.get("skills")
    if not isinstance(raw_skills, list) or len(raw_skills) > _MAX_SKILLS:
        raise RunnerConfigurationError("XDSH built-in Skill manifest has an invalid list")
    for raw_item in raw_skills:
        if not isinstance(raw_item, dict):
            raise RunnerConfigurationError("XDSH built-in Skill manifest entry is invalid")
        raw = cast(dict[object, object], raw_item)
        try:
            name = cast(str, raw["name"])
            description = cast(str, raw["description"])
            version = cast(str, raw["version"])
            raw_files = cast(dict[object, object], raw["files"])
            default_enabled = raw.get("default_enabled", True)
            provenance = cast(str, raw.get("provenance", "xdsh"))
        except (KeyError, TypeError) as error:
            raise RunnerConfigurationError(
                "XDSH built-in Skill manifest entry is invalid"
            ) from error
        if not isinstance(raw_files, dict) or not isinstance(default_enabled, bool):
            raise RunnerConfigurationError("XDSH built-in Skill manifest entry is invalid")
        try:
            files = tuple(
                sorted(
                    (cast(str, path), cast(str, digest))
                    for path, digest in raw_files.items()
                )
            )
            entry = BuiltinSkill(
                name=name,
                description=description,
                version=version,
                files=files,
                default_enabled=default_enabled,
                provenance=provenance,
            )
        except (TypeError, ValueError) as error:
            raise RunnerConfigurationError(
                "XDSH built-in Skill manifest entry is invalid"
            ) from error
        if entry.name in seen:
            raise RunnerConfigurationError("XDSH built-in Skill manifest contains duplicates")
        seen.add(entry.name)
        entries.append(entry)
    if tuple(sorted(seen)) != tuple(sorted(BUILTIN_SKILLS)):
        raise RunnerConfigurationError(
            "XDSH built-in Skill manifest does not match the packaged allowlist"
        )
    if sum(len(entry.files) for entry in entries) > _MAX_FILES:
        raise RunnerConfigurationError("XDSH built-in Skill bundle has too many files")
    return tuple(sorted(entries, key=lambda item: item.name))


def builtin_skill(name: str) -> BuiltinSkill:
    """Return one catalog entry by its normalized name."""

    if not isinstance(name, str):
        raise RunnerConfigurationError("XDSH built-in Skill name is invalid")
    normalized = name.strip().casefold()
    for entry in builtin_skill_catalog():
        if entry.name == normalized:
            return entry
    raise RunnerConfigurationError(f"XDSH built-in Skill does not exist: {normalized}")


def read_builtin_skill(name: str, relative_path: str = "SKILL.md") -> str:
    """Read a verified text resource for read-only product presentation."""

    if (
        not isinstance(relative_path, str)
        or _RELATIVE_FILE.fullmatch(relative_path) is None
        or relative_path.startswith(".")
    ):
        raise RunnerConfigurationError("XDSH built-in Skill resource path is unsafe")
    entry = builtin_skill(name)
    resource_map = dict(_validated_resources(entry))
    try:
        content = resource_map[relative_path]
    except KeyError as error:
        raise RunnerConfigurationError("XDSH built-in Skill resource does not exist") from error
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RunnerConfigurationError("XDSH built-in Skill resource is not UTF-8") from error


def builtin_skill_bundle_version() -> str:
    """Return the version of the XDSH-owned Skill bundle in this package."""

    return _load_manifest()[1]


def ensure_builtin_skills(dsh_home: Path) -> BuiltinSkillInstallReport:
    """Project packaged Skills into an isolated DSH_HOME without clobbering users.

    A file is refreshed on package upgrade only when the previous state file
    proves that XDSH seeded it and the current bytes still equal that previous
    digest. Any user-owned edit becomes a conflict and is left untouched.
    """

    expanded_home = dsh_home.expanduser()
    if expanded_home.is_symlink():
        raise RunnerConfigurationError("DSH built-in Skill home must not be a symlink")
    try:
        resolved_home = expanded_home.resolve()
    except OSError as error:
        raise RunnerConfigurationError("DSH built-in Skill home cannot be resolved") from error
    if resolved_home == Path(resolved_home.anchor) or ".xclaude" in resolved_home.parts:
        raise RunnerConfigurationError("DSH built-in Skill home is not isolated")
    skills_root = resolved_home / "skills"
    resolved_home.mkdir(mode=0o700, parents=True, exist_ok=True)
    _require_real_directory(resolved_home, "DSH home")
    skills_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    _require_real_directory(skills_root, "DSH skill root")

    entries = builtin_skill_catalog()
    validated: dict[str, dict[str, bytes]] = {}
    total_bytes = 0
    for entry in entries:
        files = dict(_validated_resources(entry))
        total_bytes += sum(len(content) for content in files.values())
        if total_bytes > _MAX_BUNDLE_BYTES:
            raise RunnerConfigurationError("XDSH built-in Skill bundle is too large")
        validated[entry.name] = files

    state_path = resolved_home / _STATE_NAME
    previous = _read_state(state_path, allowed_skills={entry.name for entry in entries})
    next_state: dict[str, str] = {}
    installed: list[str] = []
    updated: list[str] = []
    verified: list[str] = []
    preserved: list[str] = []
    conflicts: list[str] = []

    for entry in entries:
        target_root = skills_root / entry.name
        if target_root.exists() or target_root.is_symlink():
            _require_real_directory(target_root, "DSH built-in Skill path")
        else:
            target_root.mkdir(mode=0o700)
            _require_real_directory(target_root, "DSH built-in Skill path")

        files = validated[entry.name]
        # Preflight all existing files first. If one has become user-owned,
        # do not create a partial mixed-version Skill tree.
        updates: dict[str, tuple[bytes, str | None]] = {}
        user_conflict = False
        for relative_path, content in files.items():
            destination = _safe_destination(
                target_root,
                relative_path,
                create_parents=False,
            )
            key = f"{entry.name}/{relative_path}"
            expected_digest = _sha256(content)
            old_digest = previous.get(key)
            if destination.exists() or destination.is_symlink():
                if destination.is_symlink() or not destination.is_file():
                    raise RunnerConfigurationError("DSH built-in Skill file is unsafe")
                current_digest = _sha256_file(destination)
                if current_digest == expected_digest:
                    next_state[key] = expected_digest
                    verified.append(key)
                elif old_digest is not None and current_digest == old_digest:
                    updates[relative_path] = (content, old_digest)
                else:
                    user_conflict = True
                    preserved.append(key)
            else:
                updates[relative_path] = (content, None)
        if user_conflict:
            conflicts.append(entry.name)
            # Keep state for bytes that are already verified; do not add
            # missing resources to a user-modified tree.
            continue
        for relative_path, (content, previous_digest) in updates.items():
            destination = _safe_destination(target_root, relative_path)
            if destination.exists() or destination.is_symlink():
                if destination.is_symlink() or not destination.is_file():
                    raise RunnerConfigurationError("DSH built-in Skill file is unsafe")
                current_digest = _sha256_file(destination)
                expected_digest = _sha256(content)
                if current_digest == expected_digest:
                    verified.append(f"{entry.name}/{relative_path}")
                elif (
                    previous_digest is not None
                    and current_digest == previous_digest
                    and _replace_if_unchanged(
                        destination,
                        content,
                        previous_digest=previous_digest,
                    )
                ):
                    updated.append(f"{entry.name}/{relative_path}")
                else:
                    # A user or concurrent writer changed the old XDSH-owned
                    # bytes. Preserve them and fail the runner closed.
                    conflicts.append(entry.name)
                    preserved.append(f"{entry.name}/{relative_path}")
                    continue
            else:
                if previous_digest is not None:
                    # The old XDSH-owned file disappeared after preflight.
                    # Do not recreate it across a concurrent/user mutation.
                    conflicts.append(entry.name)
                    preserved.append(f"{entry.name}/{relative_path}")
                    continue
                _write_exclusive(destination, content)
                installed.append(f"{entry.name}/{relative_path}")
            next_state[f"{entry.name}/{relative_path}"] = _sha256(content)

    _write_state(state_path, next_state, entries)
    return BuiltinSkillInstallReport(
        bundle_version=_load_manifest()[1],
        installed=tuple(sorted(set(installed))),
        updated=tuple(sorted(set(updated))),
        verified=tuple(sorted(set(verified))),
        preserved_user_files=tuple(sorted(set(preserved))),
        conflicts=tuple(sorted(set(conflicts))),
    )


def _load_manifest() -> tuple[dict[object, object], str]:
    root = resources.files("xdsh").joinpath("skills")
    manifest_resource = root.joinpath(_MANIFEST_NAME)
    try:
        raw_bytes = manifest_resource.read_bytes()
    except (OSError, FileNotFoundError) as error:
        raise RunnerConfigurationError("XDSH built-in Skill manifest is missing") from error
    if len(raw_bytes) > _MAX_MANIFEST_BYTES:
        raise RunnerConfigurationError("XDSH built-in Skill manifest is too large")
    try:
        raw_object = cast(object, json.loads(raw_bytes.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RunnerConfigurationError("XDSH built-in Skill manifest is invalid") from error
    if not isinstance(raw_object, dict):
        raise RunnerConfigurationError("XDSH built-in Skill manifest must be an object")
    manifest = cast(dict[object, object], raw_object)
    if manifest.get("schema") != _SCHEMA_VERSION:
        raise RunnerConfigurationError("XDSH built-in Skill manifest schema is unsupported")
    bundle = manifest.get("bundle")
    if not isinstance(bundle, str) or _BUNDLE_NAME.fullmatch(bundle) is None:
        raise RunnerConfigurationError("XDSH built-in Skill bundle name is invalid")
    bundle_version = manifest.get("version")
    if not isinstance(bundle_version, str) or _VERSION.fullmatch(bundle_version) is None:
        raise RunnerConfigurationError("XDSH built-in Skill bundle version is invalid")
    return manifest, bundle_version


def _validated_resources(entry: BuiltinSkill) -> tuple[tuple[str, bytes], ...]:
    root = resources.files("xdsh").joinpath("skills")
    result: list[tuple[str, bytes]] = []
    total = 0
    for relative_path, expected_digest in entry.files:
        resource = root.joinpath(entry.name)
        for component in relative_path.split("/"):
            resource = resource.joinpath(component)
        try:
            if not resource.is_file():
                raise RunnerConfigurationError(
                    f"XDSH built-in Skill resource is missing: {entry.name}/{relative_path}"
                )
            content = resource.read_bytes()
        except (OSError, FileNotFoundError) as error:
            raise RunnerConfigurationError(
                f"XDSH built-in Skill resource cannot be read: {entry.name}/{relative_path}"
            ) from error
        if len(content) > _MAX_FILE_BYTES:
            raise RunnerConfigurationError("XDSH built-in Skill resource is too large")
        total += len(content)
        if total > _MAX_BUNDLE_BYTES:
            raise RunnerConfigurationError("XDSH built-in Skill resources are too large")
        if relative_path == "SKILL.md":
            _validate_skill_document(entry, content)
        if _sha256(content) != expected_digest:
            raise RunnerConfigurationError(
                f"XDSH built-in Skill hash mismatch: {entry.name}/{relative_path}"
            )
        result.append((relative_path, content))
    return tuple(result)


def _read_state(path: Path, *, allowed_skills: set[str]) -> dict[str, str]:
    if path.is_symlink():
        raise RunnerConfigurationError("XDSH built-in Skill state file is unsafe")
    if not path.exists():
        return {}
    if not path.is_file():
        raise RunnerConfigurationError("XDSH built-in Skill state file is unsafe")
    try:
        if path.stat().st_size > _MAX_STATE_BYTES:
            raise RunnerConfigurationError("XDSH built-in Skill state file is too large")
        raw_object = cast(object, json.loads(path.read_text(encoding="ascii")))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RunnerConfigurationError("XDSH built-in Skill state file is invalid") from error
    if not isinstance(raw_object, dict):
        raise RunnerConfigurationError("XDSH built-in Skill state file is invalid")
    if raw_object.get("schema") != _SCHEMA_VERSION:
        raise RunnerConfigurationError("XDSH built-in Skill state schema is unsupported")
    raw_files = raw_object.get("files", {})
    if not isinstance(raw_files, dict):
        raise RunnerConfigurationError("XDSH built-in Skill state file is invalid")
    state: dict[str, str] = {}
    for key, digest in raw_files.items():
        if (
            not isinstance(key, str)
            or not _RELATIVE_FILE.fullmatch(key)
            or "/" not in key
            or key.split("/", maxsplit=1)[0] not in allowed_skills
            or not isinstance(digest, str)
            or not _SHA256.fullmatch(digest)
        ):
            raise RunnerConfigurationError("XDSH built-in Skill state file is invalid")
        state[key] = digest
    return state


def _write_state(path: Path, state: dict[str, str], entries: tuple[BuiltinSkill, ...]) -> None:
    payload = {
        "schema": _SCHEMA_VERSION,
        "bundle": _load_manifest()[1],
        "skills": [entry.name for entry in entries],
        "files": dict(sorted(state.items())),
    }
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            descriptor = -1
            json.dump(payload, handle, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()


def _write_exclusive(path: Path, content: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _require_real_directory(path.parent, "DSH built-in Skill parent")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as error:
        raise RunnerConfigurationError("DSH built-in Skill changed during installation") from error
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(path, 0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _replace_if_unchanged(
    path: Path,
    content: bytes,
    *,
    previous_digest: str,
) -> bool:
    """Refresh one previously seeded file after a final content check."""

    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if path.is_symlink() or not path.is_file():
            return False
        if _sha256_file(path) != previous_digest:
            return False
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        return True
    except OSError as error:
        raise RunnerConfigurationError(
            "DSH built-in Skill file cannot be refreshed"
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()


def _safe_destination(
    root: Path,
    relative_path: str,
    *,
    create_parents: bool = True,
) -> Path:
    if _RELATIVE_FILE.fullmatch(relative_path) is None or relative_path.startswith("."):
        raise RunnerConfigurationError("DSH built-in Skill resource path is unsafe")
    current = root
    components = relative_path.split("/")
    for component in components[:-1]:
        current = current / component
        if current.exists() or current.is_symlink():
            _require_real_directory(current, "DSH built-in Skill resource directory")
        elif create_parents:
            current.mkdir(mode=0o700)
            _require_real_directory(current, "DSH built-in Skill resource directory")
    return current / components[-1]


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sha256_file(path: Path) -> str:
    try:
        return _sha256(path.read_bytes())
    except OSError as error:
        raise RunnerConfigurationError("DSH built-in Skill file cannot be read") from error


def _require_real_directory(path: Path, label: str) -> None:
    try:
        if path.is_symlink() or not path.is_dir():
            raise RunnerConfigurationError(f"{label} must be a real directory")
        path.chmod(0o700)
    except OSError as error:
        raise RunnerConfigurationError(f"{label} cannot be secured") from error


def _validate_skill_document(entry: BuiltinSkill, content: bytes) -> None:
    """Check the package document metadata before it reaches a DSH home."""

    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RunnerConfigurationError(
            f"XDSH built-in Skill resource is not UTF-8: {entry.name}/SKILL.md"
        ) from error
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.splitlines()
    if not lines or lines[0] != "---":
        raise RunnerConfigurationError(
            f"XDSH built-in Skill frontmatter is missing: {entry.name}"
        )
    try:
        closing = lines[1:40].index("---") + 1
    except ValueError as error:
        raise RunnerConfigurationError(
            f"XDSH built-in Skill frontmatter is invalid: {entry.name}"
        ) from error
    fields: dict[str, str] = {}
    for line in lines[1:closing]:
        key, separator, value = line.partition(":")
        if separator:
            fields[key.strip().casefold()] = value.strip().strip("'\"")
    if (
        fields.get("name", "").casefold() != entry.name
        or fields.get("description") != entry.description
    ):
        raise RunnerConfigurationError(
            f"XDSH built-in Skill frontmatter does not match manifest: {entry.name}"
        )
    if not any(line.strip() for line in lines[closing + 1 :]):
        raise RunnerConfigurationError(
            f"XDSH built-in Skill body is empty: {entry.name}"
        )


__all__ = [
    "BUILTIN_SKILLS",
    "BuiltinSkill",
    "BuiltinSkillInstallReport",
    "builtin_skill",
    "builtin_skill_bundle_version",
    "builtin_skill_catalog",
    "ensure_builtin_skills",
    "read_builtin_skill",
]
