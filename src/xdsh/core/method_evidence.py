"""Opt-in Python call evidence, executed with the caller's existing permissions.

This checks selected effective arguments BEFORE calling a function and records
its return/exception state. It neither certifies the scientific method nor
authenticates model-writable receipts. No model calls or third-party patches.
"""
from __future__ import annotations

import contextlib
import hashlib
import inspect
import json
import os
import platform
import re
import uuid
from collections.abc import Callable, Iterator, Mapping
from importlib import metadata
from pathlib import Path
from typing import Any, TypeVar

from xdsh.core.evidence_files import WorkspaceEvidence
from xdsh.core.stage_evidence import ArtifactBinding as ArtifactBinding
from xdsh.core.stage_evidence import (
    file_fingerprints,
    valid_method_name,
    validate_artifact_declaration,
)

T = TypeVar("T")


class MethodEvidenceError(ValueError):
    """Bounded message without values, source bodies or arbitrary exception text."""


def parameter_json(parameters: Mapping[str, object]) -> str:
    """Canonical selected parameters, excluding secrets and opaque Python objects."""
    from xdsh.core.product_control import redact_sensitive_text

    if (not 1 <= len(parameters) <= 16
            or any(not isinstance(k, str) or not k.isidentifier() or len(k) > 100
                   for k in parameters)):
        raise MethodEvidenceError("invalid method parameters")
    try:
        text = json.dumps(dict(parameters), sort_keys=True, allow_nan=False,
                          ensure_ascii=True, separators=(",", ":"))
    except (ValueError, TypeError, RecursionError):
        raise MethodEvidenceError("unsupported method parameters") from None
    if (len(text) > 8192 or redact_sensitive_text(text) != text
            or re.search(r'"(?:api_key|password|token|secret|authorization|credential)"',
                         text, re.I)):
        raise MethodEvidenceError("unsafe or oversized method parameters")
    return text


@contextlib.contextmanager
def _receipt_writer(path: str) -> Iterator[Callable[[dict[str, object]], None]]:
    if (not isinstance(path, str) or not 1 <= len(path) <= 240 or path.startswith("/")
            or "\\" in path or any(ord(c) < 32 for c in path)
            or any(p in {"", ".", ".."} for p in path.split("/"))
            or not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd):
        raise MethodEvidenceError("receipt must be a safe workspace-relative path")
    handles: list[int] = []
    try:
        directory = os.open(Path.cwd(), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        handles.append(directory)
        parts = path.split("/")
        for part in parts[:-1]:
            directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=directory)
            handles.append(directory)

        def write(document: dict[str, object]) -> None:
            data = json.dumps(document, ensure_ascii=True, allow_nan=False).encode()
            temporary = ".xdsh-receipt-" + uuid.uuid4().hex
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=directory)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, parts[-1], src_dir_fd=directory, dst_dir_fd=directory)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=directory)

        yield write
    except OSError:
        raise MethodEvidenceError("method receipt is inaccessible") from None
    finally:
        for handle in reversed(handles):
            os.close(handle)


def verified_call(
    function: Callable[..., T], expected: Mapping[str, object], receipt: str | ArtifactBinding, /,
    *args: Any, **kwargs: Any,
) -> T:
    """Bind defaults, require selected parameters, then call once and save a receipt.

    A string ``receipt`` records parameters only; ``ArtifactBinding`` also records
    declared file fingerprints and runtime identity. Paths are relative to the
    workspace at invocation and receipt parents must exist. A prior success is
    invalidated before validation/execution. Dataset bodies and arbitrary
    exception messages are never recorded.
    Call this in the agent's analysis process, not as a host-side executor.
    """
    binding = receipt if isinstance(receipt, ArtifactBinding) else None
    receipt_path = receipt.receipt if isinstance(receipt, ArtifactBinding) else receipt
    with _receipt_writer(receipt_path) as write:
        record: dict[str, object] = {"schema_version": 1, "kind": "xdsh.method_call",
                                     "outcome": "pending"}
        write(record)
        reader: WorkspaceEvidence | None = None
        artifacts: dict[str, Any] = {}
        if binding is not None:
            try:
                if binding.save_result is not None and (
                    not callable(binding.save_result)
                    or inspect.iscoroutinefunction(binding.save_result)
                    or inspect.isgeneratorfunction(binding.save_result)
                    or inspect.isasyncgenfunction(binding.save_result)
                ):
                    raise ValueError("serializer must execute synchronously")
                if (not isinstance(binding.packages, tuple) or len(binding.packages) > 8
                        or any(not isinstance(name, str) for name in binding.packages)
                        or len(set(binding.packages)) != len(binding.packages)):
                    raise ValueError("invalid package names")
                declaration = {"inputs": list(binding.inputs), "outputs": list(binding.outputs),
                               "code": list(binding.code),
                               "large_files": list(binding.large_files),
                               "packages": {name: "unknown" for name in binding.packages}}
                validate_artifact_declaration(declaration, binding.receipt)
                packages = {name: metadata.version(name) for name in binding.packages}
                declaration["packages"] = packages
                validate_artifact_declaration(declaration, binding.receipt)
                method = f"{function.__module__}.{function.__qualname__}"
                if not valid_method_name(method):
                    raise ValueError("invalid method identity")
                reader = WorkspaceEvidence.at(Path.cwd())

                def fingerprint(path: str) -> dict[str, object]:
                    assert reader is not None
                    return reader.fingerprint(path)

                def fingerprints(paths: tuple[str, ...]) -> dict[str, dict[str, object]]:
                    assert reader is not None and binding is not None
                    return file_fingerprints(paths, reader.read, large_files=binding.large_files,
                                             fingerprint=fingerprint)

                artifacts = {"schema_version": 1, "method": method,
                             "python": platform.python_version(), "packages": packages,
                             "inputs": fingerprints(binding.inputs),
                             "code": fingerprints(binding.code)}
            except (ValueError, TypeError, AttributeError, metadata.PackageNotFoundError):
                record["outcome"] = "blocked"
                write(record)
                raise MethodEvidenceError("artifact inputs or declaration unavailable") from None
        wanted = parameter_json(expected)
        try:
            if (inspect.iscoroutinefunction(function) or inspect.isgeneratorfunction(function)
                    or inspect.isasyncgenfunction(function)):
                raise MethodEvidenceError("only synchronous eager calls are supported")
            signature = inspect.signature(function)
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            effective = dict(bound.arguments)
            for name, parameter in signature.parameters.items():
                if parameter.kind is inspect.Parameter.VAR_KEYWORD:
                    effective.update(bound.arguments[name])
            actual = parameter_json({k: effective[k] for k in expected})
        except (TypeError, ValueError, KeyError):
            record["outcome"] = "blocked"
            write(record)
            raise MethodEvidenceError(
                "effective method parameters unavailable or invalid",
            ) from None
        if actual != wanted:
            record["outcome"] = "blocked"
            write(record)
            raise MethodEvidenceError("effective method parameters differ from plan")
        record.update(effective_parameters=json.loads(actual),
                      parameters_sha256=hashlib.sha256(actual.encode()).hexdigest())
        try:
            result = function(*args, **kwargs)
            if (inspect.isawaitable(result) or inspect.isgenerator(result)
                    or inspect.isasyncgen(result)):
                close = getattr(result, "close", None)
                if callable(close):
                    close()
                raise MethodEvidenceError("deferred execution is not a completed method call")
            if binding is not None:
                assert reader is not None
                if binding.save_result is not None:
                    saved = binding.save_result(result)
                    if (inspect.isawaitable(saved) or inspect.isgenerator(saved)
                            or inspect.isasyncgen(saved)):
                        close = getattr(saved, "close", None)
                        if callable(close):
                            close()
                        raise MethodEvidenceError("serialization was deferred, not completed")
                try:
                    if any(artifacts[role] != fingerprints(getattr(binding, role))
                           for role in ("inputs", "code")):
                        raise ValueError("stage inputs changed")
                    artifacts["outputs"] = fingerprints(binding.outputs)
                except (ValueError, TypeError):
                    raise MethodEvidenceError("stage artifacts changed or unavailable") from None
                record["artifacts"] = artifacts
        except BaseException:
            record["outcome"] = "failed"
            write(record)
            raise
        record["outcome"] = "returned"
        write(record)
        return result
