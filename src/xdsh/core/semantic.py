"""Optional, authenticated loopback embeddings for hybrid memory retrieval.

The default knowledge index stays dependency-free and lexical.  Operators may
explicitly attach one embeddings provider through this small interface.  The
production adapter accepts only a loopback HTTP endpoint, keeps credentials in
an environment variable, bounds every request/response/vector, and records
content-free audit metadata.
"""

from __future__ import annotations

import json
import math
import os
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol
from urllib.parse import urlsplit

from xdsh.core.external import (
    AuditSink,
    CredentialRef,
    ExternalAuditEvent,
    ExternalError,
    HttpRequest,
    HttpTransport,
    NetworkPolicy,
    UrllibHttpTransport,
    UrlPolicy,
)
from xdsh.core.product_control import redact_sensitive_text

_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}$")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_CONFIG_NAMES = frozenset(
    {
        "XDSH_EMBEDDING_ENDPOINT",
        "XDSH_EMBEDDING_MODEL",
        "XDSH_EMBEDDING_DIMENSIONS",
        "XDSH_EMBEDDING_CREDENTIAL_ENV",
        "XDSH_EMBEDDING_TIMEOUT_SECONDS",
    }
)
_MAX_INPUTS = 16
_MAX_INPUT_CHARS = 12_000
_MAX_TOTAL_INPUT_CHARS = 64_000
_MAX_RESPONSE_BYTES = 2_000_000
_MAX_DIMENSIONS = 8_192


class SemanticEmbeddingError(RuntimeError):
    """An optional embedding provider failed its bounded contract."""


class SemanticEmbeddingConfigurationError(SemanticEmbeddingError, ValueError):
    """The opt-in embedding environment is absent, incomplete, or unsafe."""


class EmbeddingProvider(Protocol):
    """Small synchronous seam consumed by the knowledge index."""

    @property
    def identity(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]: ...


@dataclass(frozen=True, slots=True)
class LoopbackEmbeddingConfig:
    endpoint: str
    model: str
    dimensions: int
    credential: CredentialRef
    timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        if _IDENTITY.fullmatch(self.model) is None:
            raise SemanticEmbeddingConfigurationError("embedding model ID is invalid")
        if (
            not isinstance(self.dimensions, int)
            or isinstance(self.dimensions, bool)
            or not 1 <= self.dimensions <= _MAX_DIMENSIONS
        ):
            raise SemanticEmbeddingConfigurationError("embedding dimensions are invalid")
        if not 0.5 <= self.timeout_seconds <= 30.0:
            raise SemanticEmbeddingConfigurationError("embedding timeout is invalid")


class LoopbackEmbeddingProvider:
    """Call one authenticated, OpenAI-shaped embeddings endpoint on loopback."""

    def __init__(
        self,
        config: LoopbackEmbeddingConfig,
        *,
        transport: HttpTransport | None = None,
        environment: Mapping[str, str] | None = None,
        audit: AuditSink | None = None,
    ) -> None:
        self.config = config
        self.transport = transport or UrllibHttpTransport()
        self.environment = os.environ if environment is None else environment
        self.audit = audit
        parsed = urlsplit(config.endpoint)
        hostname = (parsed.hostname or "").casefold().rstrip(".")
        if (
            parsed.scheme != "http"
            or hostname not in _LOOPBACK_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise SemanticEmbeddingConfigurationError(
                "embedding endpoint must be an HTTP loopback URL"
            )
        try:
            port = parsed.port or 80
        except ValueError as error:
            raise SemanticEmbeddingConfigurationError(
                "embedding endpoint port is invalid"
            ) from error
        self._url = UrlPolicy(
            NetworkPolicy(
                allowed_hosts=(hostname,),
                allowed_ports=(port,),
                allow_loopback=True,
                max_redirects=1,
                max_response_bytes=_MAX_RESPONSE_BYTES,
                max_decompressed_bytes=_MAX_RESPONSE_BYTES,
                timeout_seconds=config.timeout_seconds,
            )
        ).validate(config.endpoint)
        # Fail startup rather than discovering a missing reference after a
        # successful turn.  The value itself is never retained or displayed.
        config.credential.header(self.environment)

    @property
    def identity(self) -> str:
        return f"loopback:{self.config.model}"

    @property
    def dimensions(self) -> int:
        return self.config.dimensions

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        bounded = _validate_inputs(texts)
        request_id = f"embed-{uuid.uuid4().hex[:16]}"
        credential_name, credential_value = self.config.credential.header(self.environment)
        body = json.dumps(
            {"model": self.config.model, "input": list(bounded)},
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        request = HttpRequest(
            method="POST",
            url=self._url.normalized,
            headers={
                credential_name: credential_value,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            body=body,
            max_response_bytes=_MAX_RESPONSE_BYTES,
        )
        self._audit(request_id, "allowed", bytes_read=0)
        try:
            response = self.transport.request(
                request,
                timeout_seconds=self.config.timeout_seconds,
            )
            final = UrlPolicy(
                NetworkPolicy(
                    allowed_hosts=(self._url.hostname,),
                    allowed_ports=(self._url.port,),
                    allow_loopback=True,
                    max_redirects=1,
                    max_response_bytes=_MAX_RESPONSE_BYTES,
                    max_decompressed_bytes=_MAX_RESPONSE_BYTES,
                    timeout_seconds=self.config.timeout_seconds,
                )
            ).validate(response.url)
            if final.normalized != self._url.normalized:
                raise SemanticEmbeddingError("embedding endpoint changed the response URL")
            if response.status != 200:
                raise SemanticEmbeddingError("embedding endpoint returned a non-success status")
            if len(response.body) > _MAX_RESPONSE_BYTES:
                raise SemanticEmbeddingError("embedding response exceeds byte limit")
            content_type = _header(response.headers, "Content-Type").split(";", 1)[0].strip()
            if content_type != "application/json":
                raise SemanticEmbeddingError("embedding response must be application/json")
            vectors = _decode_response(
                response.body,
                expected=len(bounded),
                dimensions=self.dimensions,
            )
        except (
            ExternalError,
            OSError,
            SemanticEmbeddingError,
            ValueError,
            UnicodeError,
        ) as error:
            self._audit(request_id, "failed", reason=type(error).__name__)
            if isinstance(error, SemanticEmbeddingError):
                raise
            raise SemanticEmbeddingError("embedding request failed") from error
        self._audit(
            request_id,
            "completed",
            status=200,
            bytes_read=len(response.body),
        )
        return vectors

    def _audit(
        self,
        request_id: str,
        outcome: Literal["allowed", "blocked", "completed", "failed"],
        *,
        status: int | None = None,
        bytes_read: int = 0,
        reason: str | None = None,
    ) -> None:
        if self.audit is None:
            return
        self.audit.record(
            ExternalAuditEvent(
                request_id=request_id,
                operation="memory.embed",
                outcome=outcome,
                host=self._url.hostname,
                url_path=urlsplit(self._url.normalized).path or "/",
                status=status,
                bytes_read=bytes_read,
                credential_ref=self.config.credential.env_name,
                reason=reason,
            )
        )


def semantic_provider_from_env(
    environment: Mapping[str, str] | None = None,
    *,
    transport: HttpTransport | None = None,
    audit: AuditSink | None = None,
) -> EmbeddingProvider | None:
    """Build the explicit opt-in provider, or return ``None`` when fully absent."""

    source = os.environ if environment is None else environment
    configured = {name for name in _CONFIG_NAMES if source.get(name, "").strip()}
    if not configured:
        return None
    required = {
        "XDSH_EMBEDDING_ENDPOINT",
        "XDSH_EMBEDDING_MODEL",
        "XDSH_EMBEDDING_DIMENSIONS",
        "XDSH_EMBEDDING_CREDENTIAL_ENV",
    }
    missing = sorted(required.difference(configured))
    if missing:
        raise SemanticEmbeddingConfigurationError(
            "embedding configuration is incomplete: " + ", ".join(missing)
        )
    try:
        dimensions = int(source["XDSH_EMBEDDING_DIMENSIONS"])
        timeout = float(source.get("XDSH_EMBEDDING_TIMEOUT_SECONDS", "10"))
        credential = CredentialRef(source["XDSH_EMBEDDING_CREDENTIAL_ENV"])
        config = LoopbackEmbeddingConfig(
            endpoint=source["XDSH_EMBEDDING_ENDPOINT"],
            model=source["XDSH_EMBEDDING_MODEL"],
            dimensions=dimensions,
            credential=credential,
            timeout_seconds=timeout,
        )
    except (KeyError, ValueError) as error:
        if isinstance(error, SemanticEmbeddingConfigurationError):
            raise
        raise SemanticEmbeddingConfigurationError("embedding configuration is invalid") from error
    return LoopbackEmbeddingProvider(
        config,
        transport=transport,
        environment=source,
        audit=audit,
    )


def normalize_embedding_vector(
    value: Sequence[object],
    *,
    dimensions: int,
) -> tuple[float, ...]:
    """Validate and unit-normalize one provider-owned vector."""

    if len(value) != dimensions:
        raise SemanticEmbeddingError("embedding vector dimensions do not match")
    numbers: list[float] = []
    for item in value:
        if not isinstance(item, int | float) or isinstance(item, bool):
            raise SemanticEmbeddingError("embedding vector contains a non-number")
        number = float(item)
        if not math.isfinite(number) or abs(number) > 1_000_000:
            raise SemanticEmbeddingError("embedding vector contains an unsafe number")
        numbers.append(number)
    norm = math.sqrt(sum(number * number for number in numbers))
    if not math.isfinite(norm) or norm <= 1e-12:
        raise SemanticEmbeddingError("embedding vector has no usable magnitude")
    return tuple(number / norm for number in numbers)


def _validate_inputs(texts: Sequence[str]) -> tuple[str, ...]:
    if not 1 <= len(texts) <= _MAX_INPUTS:
        raise SemanticEmbeddingError("embedding input count exceeds limit")
    values = tuple(texts)
    if any(
        not isinstance(value, str)
        or not value.strip()
        or len(value) > _MAX_INPUT_CHARS
        or "\x00" in value
        for value in values
    ):
        raise SemanticEmbeddingError("embedding input is invalid or too large")
    if sum(len(value) for value in values) > _MAX_TOTAL_INPUT_CHARS:
        raise SemanticEmbeddingError("embedding inputs exceed total limit")
    return tuple(redact_sensitive_text(value) for value in values)


def _decode_response(
    payload: bytes,
    *,
    expected: int,
    dimensions: int,
) -> tuple[tuple[float, ...], ...]:
    try:
        decoded = json.loads(
            payload,
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_pairs,
        )
    except (RecursionError, UnicodeDecodeError, ValueError) as error:
        raise SemanticEmbeddingError("embedding response is invalid JSON") from error
    if not isinstance(decoded, dict):
        raise SemanticEmbeddingError("embedding response must be an object")
    data = decoded.get("data")
    if not isinstance(data, list) or len(data) != expected or len(data) > _MAX_INPUTS:
        raise SemanticEmbeddingError("embedding response item count does not match")
    ordered: list[tuple[float, ...] | None] = [None] * expected
    for item in data:
        if not isinstance(item, dict):
            raise SemanticEmbeddingError("embedding response item is invalid")
        index = item.get("index")
        vector = item.get("embedding")
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or not 0 <= index < expected
            or ordered[index] is not None
            or not isinstance(vector, list)
        ):
            raise SemanticEmbeddingError("embedding response index is invalid")
        ordered[index] = normalize_embedding_vector(vector, dimensions=dimensions)
    if any(vector is None for vector in ordered):
        raise SemanticEmbeddingError("embedding response is incomplete")
    return tuple(vector for vector in ordered if vector is not None)


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON value: {value}")


def _header(headers: Mapping[str, str], name: str) -> str:
    folded = name.casefold()
    return next((value for key, value in headers.items() if key.casefold() == folded), "")


__all__ = [
    "EmbeddingProvider",
    "LoopbackEmbeddingConfig",
    "LoopbackEmbeddingProvider",
    "SemanticEmbeddingConfigurationError",
    "SemanticEmbeddingError",
    "normalize_embedding_vector",
    "semantic_provider_from_env",
]
