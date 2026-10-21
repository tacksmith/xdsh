"""Opt-in stage bindings, not authenticated provenance or scientific truth."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from xdsh.core.evidence_files import evidence_path
from xdsh.core.numeric_artifacts import numeric_array_shape


@dataclass(frozen=True)
class ArtifactBinding:
    """Declare files explicitly; never infer paths from arbitrary call arguments.

    Outputs must be saved before the call returns, or by ``save_result`` directly
    after it returns (useful for libraries that mutate an in-memory object).
    All reads/writes retain the caller's permissions. Use a distinct receipt and
    output snapshot per stage; this is not an execution or recovery scheduler.
    """

    receipt: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    code: tuple[str, ...]
    packages: tuple[str, ...] = ()
    save_result: Callable[[Any], Any] | None = field(default=None, repr=False, compare=False)
    large_files: tuple[str, ...] = ()


def validate_artifact_declaration(value: Any, receipt: str) -> None:
    """Validate frozen file roles and expected distribution versions."""
    if (not isinstance(value, dict)
            or not {"inputs", "outputs", "code", "packages"} <= set(value)
            or set(value) - {"inputs", "outputs", "code", "packages", "method", "large_files"}):
        raise ValueError("invalid artifact binding")
    paths: list[str] = [evidence_path(receipt)]
    for role in ("inputs", "outputs", "code"):
        rows = value[role]
        if not isinstance(rows, list) or not 1 <= len(rows) <= (4 if role == "code" else 8):
            raise ValueError("invalid artifact binding")
        paths.extend(evidence_path(path) for path in rows)
    if len(set(paths)) != len(paths):
        raise ValueError("artifact paths must be distinct")
    large = value.get("large_files", [])
    if (not isinstance(large, list) or len(large) > 8
            or any(not isinstance(p, str) for p in large)
            or len(set(large)) != len(large)
            or not set(large) <= set(value["inputs"] + value["outputs"])):
        raise ValueError("invalid artifact binding")
    packages = value["packages"]
    if (not isinstance(packages, dict) or len(packages) > 8
            or any(not isinstance(name, str)
                   or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", name)
                   or not isinstance(version, str)
                   or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+!-]{0,99}", version)
                   for name, version in packages.items())):
        raise ValueError("invalid artifact packages")
    if "method" in value and not valid_method_name(value["method"]):
        raise ValueError("invalid artifact method")


def valid_method_name(value: Any) -> bool:
    return (isinstance(value, str) and 1 <= len(value) <= 240
            and re.fullmatch(r"[A-Za-z0-9_.<>]+", value) is not None)


def file_fingerprints(
    paths: list[str] | tuple[str, ...], read: Callable[[str], bytes], *,
    large_files: list[str] | tuple[str, ...] = (),
    fingerprint: Callable[[str], dict[str, object]] | None = None,
) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for path in paths:
        if path in large_files:
            if fingerprint is None:
                raise ValueError("large evidence reader unavailable")
            result[path] = {k: v for k, v in fingerprint(path).items()
                            if k in {"sha256", "size_bytes"}}
            continue
        data = read(path)
        record: dict[str, object] = {"sha256": hashlib.sha256(data).hexdigest(),
                                     "size_bytes": len(data)}
        if path.endswith(".npy"):
            record["shape"] = list(numeric_array_shape(data, finite=False))
        result[path] = record
    return result


def artifact_binding_matches(document: Any, declaration: dict[str, Any],
                             read: Callable[[str], bytes], *,
                             fingerprint: Callable[[str], dict[str, object]] | None = None) -> bool:
    """Recheck final bytes, not just a claimed shape or a copied digest."""
    try:
        if (not isinstance(document, dict)
                or set(document) != {"schema_version", "inputs", "outputs", "code",
                                     "packages", "method", "python"}
                or type(document["schema_version"]) is not int or document["schema_version"] != 1
                or not valid_method_name(document["method"])
                or not isinstance(document["python"], str)
                or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", document["python"])
                or document["packages"] != declaration["packages"]
                or ("method" in declaration and document["method"] != declaration["method"])):
            return False
        return all(json.dumps(document[role], sort_keys=True, allow_nan=False)
                   == json.dumps(file_fingerprints(declaration[role], read,
                        large_files=declaration.get("large_files", []), fingerprint=fingerprint),
                        sort_keys=True)
                   for role in ("inputs", "code", "outputs"))
    except (ValueError, TypeError, KeyError):
        return False
