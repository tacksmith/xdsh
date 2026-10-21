"""Version identity for the active XDSH/DSH release pair.

Python package metadata follows PEP 440 (``0.1.1rc2``), while the official
DSH/npm ecosystem spells the same prerelease ``0.1.1-rc.2``.  Keeping both
spellings here makes the equivalence explicit and prevents an accidental
minor-version drift between the product shell and its runtime.
"""

from __future__ import annotations

import re

# PEP 440 spelling used by ``pyproject.toml`` and distribution metadata.
XDSH_VERSION = "0.1.1rc2"
# Canonical display/tag spelling shared with the pinned DSH release.
XDSH_VERSION_DISPLAY = "0.1.1-rc.2"
XDSH_RELEASE_TAG = "xdsh-0.1.1-rc.2"
DSH_COMPATIBLE_VERSION = "0.1.1-rc.2"
# The protocol client is published independently from the Node runtime.  It
# intentionally remains rc1 even though the XDSH product/runtime pair is rc2.
DSH_PYTHON_SDK_VERSION = "0.1.1rc1"

_VERSION = re.compile(
    r"^(?P<base>\d+\.\d+\.\d+)(?:-?(?P<label>alpha|beta|rc)[.-]?(?P<number>\d+))?$",
    re.IGNORECASE,
)


def normalize_release_version(value: str) -> str:
    """Normalize npm/PEP-style prerelease punctuation for safe comparison."""

    match = _VERSION.fullmatch(value.strip())
    if match is None:
        raise ValueError(f"unsupported release version: {value!r}")
    label = match.group("label")
    if label is None:
        return match.group("base")
    return f"{match.group('base')}{label.lower()}{match.group('number')}"


def versions_align(left: str, right: str) -> bool:
    """Return whether two release spellings identify the same version."""

    try:
        return normalize_release_version(left) == normalize_release_version(right)
    except ValueError:
        return False


if not versions_align(XDSH_VERSION, DSH_COMPATIBLE_VERSION):  # pragma: no cover
    raise RuntimeError("XDSH and DSH release constants must remain aligned")

