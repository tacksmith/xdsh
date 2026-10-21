"""Native-safe, bounded media ingestion.

Telegram and other transports hand untrusted bytes to this module before a
runner can see them.  The pipeline only sniffs and previews data; it never
executes a file and never extracts an archive to the workspace.  Every limit
is checked before the bytes are persisted, and persisted files are created
with a private directory and mode ``0600``.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import tarfile
import uuid
import zipfile
import zlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from xml.parsers import expat

from xdsh.core.external import AuditSink, ExternalAuditEvent
from xdsh.core.learning import contains_sensitive_text
from xdsh.core.product_control import redact_sensitive_text


class MediaSecurityError(RuntimeError):
    """Media bytes, metadata, or storage destination failed closed."""


MediaKind = Literal[
    "image",
    "document",
    "archive",
    "audio",
    "video",
    "text",
    "binary",
]

_OOXML_FORMAT_BY_MIME = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
}
_OOXML_REQUIRED_PATH = {
    "docx": "word/document.xml",
    "pptx": "ppt/presentation.xml",
    "xlsx": "xl/workbook.xml",
}
_XML_UNSAFE_DECLARATION = re.compile(br"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_MAX_XML_ELEMENTS = 200_000
_MAX_XML_DEPTH = 256
_MAX_XML_ATTRIBUTES = 128


@dataclass(frozen=True, slots=True)
class MediaPolicy:
    """Hard limits applied to every ingested media object."""

    max_bytes: int = 20_000_000
    max_uncompressed_bytes: int = 40_000_000
    max_archive_entries: int = 100
    max_archive_depth: int = 2
    max_image_pixels: int = 40_000_000
    max_text_chars: int = 100_000
    max_compression_ratio: int = 100
    max_pdf_pages: int = 100
    max_filename_chars: int = 120

    def __post_init__(self) -> None:
        positive = (
            ("max_bytes", self.max_bytes),
            ("max_uncompressed_bytes", self.max_uncompressed_bytes),
            ("max_archive_entries", self.max_archive_entries),
            ("max_image_pixels", self.max_image_pixels),
            ("max_text_chars", self.max_text_chars),
            ("max_compression_ratio", self.max_compression_ratio),
            ("max_pdf_pages", self.max_pdf_pages),
            ("max_filename_chars", self.max_filename_chars),
        )
        for name, value in positive:
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if not isinstance(self.max_archive_depth, int) or isinstance(self.max_archive_depth, bool):
            raise ValueError("max_archive_depth must be an integer")
        if not 0 <= self.max_archive_depth <= 8:
            raise ValueError("max_archive_depth must be between 0 and 8")
        if self.max_uncompressed_bytes < self.max_bytes:
            raise ValueError("max_uncompressed_bytes must not be below max_bytes")
        if self.max_bytes > 100_000_000 or self.max_uncompressed_bytes > 200_000_000:
            raise ValueError("media byte limits exceed the hard safety ceiling")
        if self.max_filename_chars > 240:
            raise ValueError("max_filename_chars exceeds the Telegram-safe ceiling")


@dataclass(frozen=True, slots=True)
class MediaArtifact:
    """Safe metadata and optional bounded preview returned to a caller."""

    filename: str
    declared_mime: str | None
    detected_mime: str
    kind: MediaKind
    size: int
    sha256: str
    path: Path | None = None
    text: str = ""
    warnings: tuple[str, ...] = ()
    metadata: Mapping[str, int | str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _Sniffed:
    mime: str
    kind: MediaKind
    metadata: Mapping[str, int | str] = field(default_factory=dict)


class NativeSafeMediaPipeline:
    """Inspect untrusted bytes with bounded, non-executing native parsers."""

    def __init__(
        self, policy: MediaPolicy | None = None, *, audit: AuditSink | None = None
    ) -> None:
        self.policy = policy or MediaPolicy()
        self.audit = audit

    def ingest(
        self,
        content: bytes,
        *,
        filename: str,
        declared_mime: str | None = None,
        destination: Path | None = None,
        stored_filename: str | None = None,
    ) -> MediaArtifact:
        """Validate, inspect, and optionally persist one byte payload.

        ``destination`` is a directory, never a caller-controlled filename.
        Archives are inspected in memory and are deliberately not extracted.
        """

        request_id = f"media-{uuid.uuid4().hex[:16]}"
        safe_name = _safe_filename(filename, self.policy.max_filename_chars)
        try:
            if not isinstance(content, bytes) or not content:
                raise MediaSecurityError("media payload is empty")
            if len(content) > self.policy.max_bytes:
                raise MediaSecurityError("media payload exceeds byte limit")
            declared = _normalize_mime(declared_mime)
            sniffed = _sniff(content)
            if sniffed.mime == "application/zip":
                office = _sniff_ooxml(content, self.policy)
                if office is not None:
                    sniffed = office
            _validate_declared_mime(declared, sniffed.mime)
            if sniffed.mime in {"application/x-dosexec", "application/x-executable"}:
                raise MediaSecurityError("executable payloads are not accepted")
            metadata = dict(sniffed.metadata)
            warnings: list[str] = []
            preview = ""
            if sniffed.kind == "image":
                _validate_image_limits(metadata, self.policy)
            elif sniffed.mime == "application/gzip":
                unpacked = _bounded_gzip(content, self.policy)
                metadata["uncompressed_bytes"] = len(unpacked)
                if _looks_like_tar(unpacked):
                    archive_metadata = self._inspect_archive(
                        unpacked,
                        "application/x-tar",
                    )
                    archive_preview = archive_metadata.pop("preview", "")
                    metadata.update(archive_metadata)
                    if isinstance(archive_preview, str):
                        preview = archive_preview
                elif _looks_like_text(unpacked):
                    preview = _bounded_text(unpacked, self.policy.max_text_chars)
            elif sniffed.kind == "archive":
                archive_metadata = self._inspect_archive(content, sniffed.mime)
                archive_preview = archive_metadata.pop("preview", "")
                metadata.update(archive_metadata)
                if isinstance(archive_preview, str):
                    preview = archive_preview
            elif sniffed.mime in _OOXML_FORMAT_BY_MIME:
                document_metadata = _inspect_ooxml(content, sniffed.mime, self.policy)
                document_preview = document_metadata.pop("preview", "")
                metadata.update(document_metadata)
                if isinstance(document_preview, str):
                    preview = document_preview
            elif sniffed.mime == "application/pdf":
                pages = _pdf_page_count(content)
                metadata["pages"] = pages
                if pages > self.policy.max_pdf_pages:
                    raise MediaSecurityError("PDF page count exceeds limit")
            elif sniffed.kind == "text":
                preview = _bounded_text(content, self.policy.max_text_chars)
            if contains_sensitive_text(preview):
                warnings.append("preview_redacted_credentials")
                preview = redact_sensitive_text(preview)
            if preview:
                metadata["extracted_text_chars"] = len(preview)
            storage_name = (
                _safe_filename(stored_filename, self.policy.max_filename_chars)
                if stored_filename is not None
                else safe_name
            )
            stored_path = self._store(content, storage_name, destination) if destination else None
            artifact = MediaArtifact(
                filename=safe_name,
                declared_mime=declared,
                detected_mime=sniffed.mime,
                kind=sniffed.kind,
                size=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
                path=stored_path,
                text=preview,
                warnings=tuple(warnings),
                metadata=metadata,
            )
            self._audit(
                request_id,
                outcome="completed",
                filename=safe_name,
                bytes_read=len(content),
            )
            return artifact
        except MediaSecurityError as error:
            self._audit(
                request_id,
                outcome="blocked",
                filename=safe_name,
                reason=str(error),
            )
            raise
        except (
            OSError,
            tarfile.TarError,
            zipfile.BadZipFile,
            zipfile.LargeZipFile,
            UnicodeError,
            RecursionError,
            RuntimeError,
            OverflowError,
        ) as error:
            self._audit(
                request_id,
                outcome="failed",
                filename=safe_name,
                reason=type(error).__name__,
            )
            raise MediaSecurityError("media inspection or storage failed") from error

    def _inspect_archive(self, content: bytes, mime: str) -> dict[str, int | str]:
        if mime == "application/zip":
            return _inspect_zip(content, self.policy)
        if mime in {"application/x-tar", "application/gzip+tar"}:
            return _inspect_tar(content, self.policy)
        # gzip is classified separately and handled by _bounded_gzip.
        raise MediaSecurityError("unsupported archive format")

    def _store(self, content: bytes, filename: str, destination: Path) -> Path:
        directory = _safe_destination(destination)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
        if directory.is_symlink() or not directory.is_dir():
            raise MediaSecurityError("media destination is not a private directory")
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            directory_fd = os.open(directory, flags)
        except OSError as error:
            raise MediaSecurityError("media destination cannot be opened safely") from error
        try:
            stem = _safe_filename(Path(filename).stem, self.policy.max_filename_chars)
            suffix = Path(filename).suffix.lower()[:16]
            for _index in range(100):
                if _index == 0:
                    candidate = filename
                else:
                    candidate = f"{stem}-{_index}{suffix}"
                file_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                try:
                    descriptor = os.open(candidate, file_flags, 0o600, dir_fd=directory_fd)
                except FileExistsError:
                    continue
                try:
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(content)
                        stream.flush()
                        os.fsync(stream.fileno())
                except BaseException:
                    # The descriptor is closed by fdopen when its context exits.
                    try:
                        os.unlink(candidate, dir_fd=directory_fd)
                    except OSError:
                        pass
                    raise
                return directory / candidate
        finally:
            os.close(directory_fd)
        raise MediaSecurityError("could not allocate a safe media filename")

    def _audit(
        self,
        request_id: str,
        *,
        outcome: Literal["completed", "blocked", "failed"],
        filename: str,
        bytes_read: int = 0,
        reason: str | None = None,
    ) -> None:
        if self.audit is None:
            return
        self.audit.record(
            ExternalAuditEvent(
                request_id=request_id,
                operation="media.ingest",
                outcome=outcome,
                host="local",
                url_path=f"/media/{redact_sensitive_text(filename)[:200]}",
                bytes_read=bytes_read,
                reason=redact_sensitive_text(reason)[:240] if reason else None,
            )
        )


def _safe_filename(value: str, limit: int) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise MediaSecurityError("media filename is invalid")
    # Treat both separators as path delimiters and reject traversal semantics.
    normalized_path = value.replace("\\", "/")
    if normalized_path.startswith("/") or re.match(r"^[A-Za-z]:", normalized_path):
        raise MediaSecurityError("media filename must be relative")
    if any(part == ".." for part in normalized_path.split("/")):
        raise MediaSecurityError("media filename traversal is not allowed")
    basename = normalized_path.rsplit("/", 1)[-1]
    if basename in {"", ".", ".."}:
        raise MediaSecurityError("media filename traversal is not allowed")
    normalized = re.sub(r"[^A-Za-z0-9._() -]+", "_", basename).strip(". ")
    normalized = normalized[:limit].rstrip(". ")
    if not normalized:
        raise MediaSecurityError("media filename is empty after normalization")
    return normalized


def _safe_destination(destination: Path) -> Path:
    if not isinstance(destination, Path):
        destination = Path(destination)
    # Never permit a media helper to write into reserved legacy/runtime roots.
    raw_parts = {part.casefold() for part in destination.parts}
    if ".xclaude" in raw_parts or ".dsh" in raw_parts:
        raise MediaSecurityError("media destination is reserved")
    expanded = destination.expanduser()
    # Check dangling links too.  ``Path.exists()`` is false for a dangling
    # symlink, but resolving it before mkdir would otherwise let an attacker
    # redirect the first write outside the intended workspace.
    probe = expanded
    while True:
        if probe.is_symlink() and not _is_trusted_system_alias(probe):
            raise MediaSecurityError("media destination symlink is not allowed")
        parent = probe.parent
        if parent == probe:
            break
        probe = parent
    resolved = expanded.resolve(strict=False)
    if any(part.casefold() in {".xclaude", ".dsh"} for part in resolved.parts):
        raise MediaSecurityError("media destination is reserved")
    return resolved


def _is_trusted_system_alias(path: Path) -> bool:
    """Allow macOS's fixed /var and /tmp aliases without allowing user links."""

    aliases = {
        Path("/var"): Path("/private/var"),
        Path("/tmp"): Path("/private/tmp"),
    }
    target = aliases.get(path)
    if target is None:
        return False
    try:
        return path.resolve(strict=True) == target
    except OSError:
        return False


def _normalize_mime(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise MediaSecurityError("declared MIME type is invalid")
    mime = value.split(";", 1)[0].strip().casefold()
    if re.fullmatch(r"[a-z0-9.+-]+/[a-z0-9.+-]+", mime) is None:
        raise MediaSecurityError("declared MIME type is invalid")
    return mime


def _validate_declared_mime(declared: str | None, detected: str) -> None:
    if declared is None or declared == "application/octet-stream":
        return
    # Generic text declarations are compatible with JSON/XML/CSV previews.
    compatible = {
        "text/plain": {"text/plain", "application/json", "application/xml", "text/xml"},
        "text/csv": {"text/plain", "text/csv"},
        "application/json": {"application/json"},
        "application/xml": {"application/xml", "text/xml"},
        "text/xml": {"application/xml", "text/xml"},
        "image/jpeg": {"image/jpeg"},
        "image/png": {"image/png"},
        "image/gif": {"image/gif"},
        "image/webp": {"image/webp"},
        "application/pdf": {"application/pdf"},
        "application/zip": {"application/zip", *_OOXML_FORMAT_BY_MIME},
        "application/x-tar": {"application/x-tar"},
        "application/gzip": {"application/gzip"},
    }
    for office_mime in _OOXML_FORMAT_BY_MIME:
        compatible[office_mime] = {office_mime}
    if detected not in compatible.get(declared, {declared}):
        raise MediaSecurityError("declared MIME does not match detected content")


def _sniff(content: bytes) -> _Sniffed:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        metadata: dict[str, int | str] = {}
        if len(content) >= 24 and content[12:16] == b"IHDR":
            metadata["width"] = int.from_bytes(content[16:20], "big")
            metadata["height"] = int.from_bytes(content[20:24], "big")
        return _Sniffed("image/png", "image", metadata)
    if content.startswith(b"\xff\xd8\xff"):
        width, height = _jpeg_dimensions(content)
        metadata = {"width": width, "height": height} if width and height else {}
        return _Sniffed("image/jpeg", "image", metadata)
    if content.startswith((b"GIF87a", b"GIF89a")):
        metadata = {}
        if len(content) >= 10:
            metadata = {
                "width": int.from_bytes(content[6:8], "little"),
                "height": int.from_bytes(content[8:10], "little"),
            }
        return _Sniffed("image/gif", "image", metadata)
    if content.startswith(b"RIFF") and len(content) >= 12 and content[8:12] == b"WEBP":
        webp_metadata = _webp_dimensions(content)
        return _Sniffed("image/webp", "image", webp_metadata)
    if content.startswith(b"%PDF-"):
        return _Sniffed("application/pdf", "document")
    if content.startswith(b"MZ"):
        return _Sniffed("application/x-dosexec", "binary")
    if content.startswith(b"\x7fELF") or content.startswith(b"\xfe\xed\xfa\xce"):
        return _Sniffed("application/x-executable", "binary")
    if content.startswith(b"PK\x03\x04") or content.startswith(b"PK\x05\x06"):
        return _Sniffed("application/zip", "archive")
    if content.startswith(b"\x1f\x8b"):
        # Do not classify a gzip stream as tar until the bounded decoder says so.
        return _Sniffed("application/gzip", "archive")
    if content.startswith(b"OggS"):
        return _Sniffed("audio/ogg", "audio")
    if content.startswith(b"RIFF") and len(content) >= 12 and content[8:12] == b"WAVE":
        return _Sniffed("audio/wav", "audio")
    if content.startswith(b"ID3") or _looks_like_mp3_frame(content):
        return _Sniffed("audio/mpeg", "audio")
    if _looks_like_tar(content):
        return _Sniffed("application/x-tar", "archive")
    if _looks_like_text(content):
        stripped = content.lstrip()
        if stripped.startswith((b"{", b"[")):
            try:
                json.loads(stripped.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
            else:
                return _Sniffed("application/json", "text")
        return _Sniffed("text/plain", "text")
    return _Sniffed("application/octet-stream", "binary")


def _looks_like_text(content: bytes) -> bool:
    if b"\x00" in content:
        return False
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _bounded_text(content: bytes, max_chars: int) -> str:
    if len(content) > max_chars * 8:
        # Decode only a bounded prefix; the byte check prevents a pathological
        # multibyte payload from consuming unbounded memory.
        content = content[: max_chars * 8]
    return content.decode("utf-8", errors="replace")[:max_chars]


def _validate_image_limits(metadata: Mapping[str, int | str], policy: MediaPolicy) -> None:
    width = metadata.get("width")
    height = metadata.get("height")
    if not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0:
        raise MediaSecurityError("image dimensions are missing or invalid")
    if width > 100_000 or height > 100_000 or width * height > policy.max_image_pixels:
        raise MediaSecurityError("image dimensions exceed pixel limit")


def _jpeg_dimensions(content: bytes) -> tuple[int | None, int | None]:
    index = 2
    length = len(content)
    while index + 4 <= length:
        if content[index] != 0xFF:
            index += 1
            continue
        while index < length and content[index] == 0xFF:
            index += 1
        if index >= length:
            break
        marker = content[index]
        index += 1
        if marker in {0xD8, 0xD9}:
            continue
        if marker == 0xDA:
            break
        if index + 2 > length:
            break
        segment_length = int.from_bytes(content[index : index + 2], "big")
        if segment_length < 2 or index + segment_length > length:
            break
        if marker in set(range(0xC0, 0xC4)) | set(range(0xC5, 0xC8)) | set(range(0xC9, 0xCC)) | set(
            range(0xCD, 0xD0)
        ):
            if segment_length >= 7:
                height = int.from_bytes(content[index + 3 : index + 5], "big")
                width = int.from_bytes(content[index + 5 : index + 7], "big")
                return width, height
        index += segment_length
    return None, None


def _webp_dimensions(content: bytes) -> dict[str, int]:
    if len(content) < 30:
        return {}
    chunk = content[12:16]
    if chunk == b"VP8X" and len(content) >= 30:
        width = 1 + int.from_bytes(content[24:27], "little")
        height = 1 + int.from_bytes(content[27:30], "little")
        return {"width": width, "height": height}
    return {}


def _looks_like_mp3_frame(content: bytes) -> bool:
    if len(content) < 2 or content[0] != 0xFF or content[1] & 0xE0 != 0xE0:
        return False
    return True


def _looks_like_tar(content: bytes) -> bool:
    return len(content) >= 265 and content[257:262] == b"ustar"


def _bounded_gzip(content: bytes, policy: MediaPolicy) -> bytes:
    if len(content) > policy.max_bytes:
        raise MediaSecurityError("compressed payload exceeds byte limit")
    output = bytearray()
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        for offset in range(0, len(content), 32_768):
            chunk = content[offset : offset + 32_768]
            part = decompressor.decompress(chunk, policy.max_uncompressed_bytes - len(output) + 1)
            output.extend(part)
            if len(output) > policy.max_uncompressed_bytes:
                raise MediaSecurityError("decompressed payload exceeds limit")
            if len(output) > max(len(content), 1) * policy.max_compression_ratio:
                raise MediaSecurityError("compression ratio exceeds limit")
        if decompressor.unused_data:
            raise MediaSecurityError("concatenated or trailing gzip data is not allowed")
        tail = decompressor.flush(policy.max_uncompressed_bytes - len(output) + 1)
        output.extend(tail)
    except zlib.error as error:
        raise MediaSecurityError("gzip payload is invalid") from error
    if len(output) > policy.max_uncompressed_bytes:
        raise MediaSecurityError("decompressed payload exceeds limit")
    if not decompressor.eof:
        raise MediaSecurityError("gzip payload is truncated")
    if len(output) > max(len(content), 1) * policy.max_compression_ratio:
        raise MediaSecurityError("compression ratio exceeds limit")
    return bytes(output)


def _inspect_zip(content: bytes, policy: MediaPolicy) -> dict[str, int | str]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as error:
        raise MediaSecurityError("ZIP archive is invalid") from error
    total = 0
    preview_parts: list[str] = []
    with archive:
        infos, total = _validated_zip_inventory(archive, policy)
        for info in infos:
            if _is_text_filename(info.filename) and info.file_size <= policy.max_text_chars * 8:
                with archive.open(info, "r") as stream:
                    data = _read_stream_bounded(
                        stream, min(info.file_size, policy.max_text_chars * 8), policy
                    )
                if _looks_like_text(data) and sum(map(len, preview_parts)) < policy.max_text_chars:
                    preview_parts.append(
                        _bounded_text(data, policy.max_text_chars - sum(map(len, preview_parts)))
                    )
    result: dict[str, int | str] = {
        "entries": len(infos),
        "uncompressed_bytes": total,
    }
    if preview_parts:
        result["preview"] = "\n".join(preview_parts)[: policy.max_text_chars]
    return result


def _sniff_ooxml(content: bytes, policy: MediaPolicy) -> _Sniffed | None:
    """Recognize an OOXML document only from its bounded ZIP manifest and layout."""

    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as error:
        raise MediaSecurityError("ZIP archive is invalid") from error
    with archive:
        infos, total = _validated_zip_inventory(archive, policy)
        by_name = {_normalized_archive_name(info.filename): info for info in infos}
        manifest = by_name.get("[Content_Types].xml")
        if manifest is None:
            return None
        manifest_limit = min(policy.max_uncompressed_bytes, 1_000_000)
        if manifest.file_size > manifest_limit:
            raise MediaSecurityError("Office content manifest exceeds limit")
        with archive.open(manifest, "r") as stream:
            payload = _read_stream_bounded(stream, manifest.file_size, policy)
        if _XML_UNSAFE_DECLARATION.search(payload):
            raise MediaSecurityError("Office XML declarations are not allowed")
        matches = [
            (mime, document_format)
            for mime, document_format in _OOXML_FORMAT_BY_MIME.items()
            if _xml_attribute_mentions(payload, "ContentType", mime)
            and _OOXML_REQUIRED_PATH[document_format] in by_name
        ]
        if not matches:
            return None
        if len(matches) != 1:
            raise MediaSecurityError("Office document type is ambiguous")
        mime, document_format = matches[0]
        return _Sniffed(
            mime,
            "document",
            {
                "format": document_format,
                "entries": len(infos),
                "uncompressed_bytes": total,
            },
        )


def _inspect_ooxml(
    content: bytes,
    mime: str,
    policy: MediaPolicy,
) -> dict[str, int | str]:
    document_format = _OOXML_FORMAT_BY_MIME[mime]
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as error:
        raise MediaSecurityError("Office document is not a valid ZIP archive") from error
    preview_parts: list[str] = []
    with archive:
        infos, total = _validated_zip_inventory(archive, policy)
        selected = sorted(
            (info for info in infos if _is_ooxml_text_entry(info.filename, document_format)),
            key=lambda info: info.filename,
        )
        for info in selected:
            remaining = policy.max_text_chars - sum(len(part) for part in preview_parts)
            if remaining <= 0:
                break
            with archive.open(info, "r") as stream:
                payload = _read_stream_bounded(stream, info.file_size, policy)
            text = _bounded_ooxml_text(payload, remaining, document_format)
            if text:
                preview_parts.append(text)
    result: dict[str, int | str] = {
        "format": document_format,
        "entries": len(infos),
        "uncompressed_bytes": total,
        "text_parts": len(preview_parts),
    }
    if preview_parts:
        result["preview"] = "\n\n".join(preview_parts)[: policy.max_text_chars]
    return result


def _validated_zip_inventory(
    archive: zipfile.ZipFile,
    policy: MediaPolicy,
) -> tuple[list[zipfile.ZipInfo], int]:
    infos = archive.infolist()
    if len(infos) > policy.max_archive_entries:
        raise MediaSecurityError("archive entry count exceeds limit")
    total = 0
    seen: set[str] = set()
    for info in infos:
        _validate_archive_name(info.filename)
        normalized = _normalized_archive_name(info.filename).casefold()
        if normalized in seen:
            raise MediaSecurityError("archive contains duplicate entry names")
        seen.add(normalized)
        mode = (info.external_attr >> 16) & 0o170000
        if mode in {
            stat.S_IFLNK,
            stat.S_IFCHR,
            stat.S_IFBLK,
            stat.S_IFIFO,
            getattr(stat, "S_IFSOCK", 0),
        }:
            raise MediaSecurityError("archive links and device entries are not allowed")
        if info.file_size < 0 or info.file_size > policy.max_uncompressed_bytes:
            raise MediaSecurityError("archive entry exceeds uncompressed limit")
        total += info.file_size
        if total > policy.max_uncompressed_bytes:
            raise MediaSecurityError("archive uncompressed total exceeds limit")
        compressed = max(info.compress_size, 1)
        if info.file_size > compressed * policy.max_compression_ratio:
            raise MediaSecurityError("archive compression ratio exceeds limit")
        if (
            _archive_name(info.filename)
            and _archive_depth(info.filename) > policy.max_archive_depth
        ):
            raise MediaSecurityError("nested archive depth exceeds limit")
    return infos, total


def _normalized_archive_name(name: str) -> str:
    return "/".join(part for part in name.replace("\\", "/").split("/") if part not in {"", "."})


def _xml_attribute_mentions(payload: bytes, attribute: str, value: str) -> bool:
    pattern = re.compile(
        rb"\b"
        + re.escape(attribute.encode("ascii"))
        + rb"\s*=\s*(['\"])"
        + re.escape(value.encode("ascii"))
        + rb"\1",
        re.IGNORECASE,
    )
    return pattern.search(payload) is not None


def _is_ooxml_text_entry(name: str, document_format: str) -> bool:
    normalized = _normalized_archive_name(name)
    if document_format == "docx":
        return re.fullmatch(
            r"word/(?:document|footnotes|endnotes|comments|header\d+|footer\d+)\.xml",
            normalized,
        ) is not None
    if document_format == "pptx":
        return re.fullmatch(
            r"ppt/(?:slides/slide\d+|notesSlides/notesSlide\d+)\.xml",
            normalized,
        ) is not None
    if document_format == "xlsx":
        return normalized == "xl/sharedStrings.xml" or re.fullmatch(
            r"xl/worksheets/sheet\d+\.xml",
            normalized,
        ) is not None
    return False


class _XmlPreviewComplete(Exception):
    """Internal early-stop once the caller's text bound is full."""


def _bounded_ooxml_text(payload: bytes, max_chars: int, document_format: str) -> str:
    if _XML_UNSAFE_DECLARATION.search(payload):
        raise MediaSecurityError("Office XML declarations are not allowed")
    parts: list[str] = []
    characters = 0
    depth = 0
    elements = 0
    capture_depth = 0

    def append(value: str) -> None:
        nonlocal characters
        if not value:
            return
        remaining = max_chars - characters
        if remaining <= 0:
            raise _XmlPreviewComplete
        selected = value[:remaining]
        parts.append(selected)
        characters += len(selected)
        if len(value) > remaining or characters >= max_chars:
            raise _XmlPreviewComplete

    def start(name: str, attributes: Mapping[str, str]) -> None:
        nonlocal capture_depth, depth, elements
        depth += 1
        elements += 1
        if depth > _MAX_XML_DEPTH or elements > _MAX_XML_ELEMENTS:
            raise MediaSecurityError("Office XML complexity exceeds limit")
        if len(attributes) > _MAX_XML_ATTRIBUTES or len(name) > 512:
            raise MediaSecurityError("Office XML element exceeds limit")
        local = _xml_local_name(name)
        if local in {"t", "v"}:
            capture_depth += 1
        elif local == "tab":
            append("\t")
        elif local == "br":
            append("\n")

    def end(name: str) -> None:
        nonlocal capture_depth, depth
        local = _xml_local_name(name)
        if local in {"t", "v"} and capture_depth:
            capture_depth -= 1
        if local in {"p", "row", "si"}:
            append("\n")
        elif document_format == "xlsx" and local == "c":
            append("\t")
        depth = max(0, depth - 1)

    def data(value: str) -> None:
        if capture_depth:
            append(value)

    def unsafe_declaration(*_args: object) -> None:
        raise MediaSecurityError("Office XML declarations are not allowed")

    def unsafe_external_entity(
        _context: str,
        _base: str | None,
        _system_id: str | None,
        _public_id: str | None,
    ) -> int:
        raise MediaSecurityError("Office XML declarations are not allowed")

    parser = expat.ParserCreate(namespace_separator="}")
    parser.buffer_text = True
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = data
    parser.StartDoctypeDeclHandler = unsafe_declaration
    parser.EntityDeclHandler = unsafe_declaration
    parser.ExternalEntityRefHandler = unsafe_external_entity
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    try:
        parser.Parse(payload, True)
    except _XmlPreviewComplete:
        pass
    except expat.ExpatError as error:
        raise MediaSecurityError("Office XML is malformed") from error
    text = "".join(parts)
    text = re.sub(r"[\t\r\f\v ]+", " ", text)
    text = re.sub(r" *\n+ *", "\n", text)
    return text.strip()[:max_chars]


def _xml_local_name(name: str) -> str:
    return name.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


def _inspect_tar(content: bytes, policy: MediaPolicy) -> dict[str, int | str]:
    total = 0
    entries = 0
    preview_parts: list[str] = []
    try:
        archive = tarfile.open(fileobj=io.BytesIO(content), mode="r:*")
    except tarfile.TarError as error:
        raise MediaSecurityError("TAR archive is invalid") from error
    with archive:
        for member in archive:
            entries += 1
            if entries > policy.max_archive_entries:
                raise MediaSecurityError("archive entry count exceeds limit")
            _validate_archive_name(member.name)
            if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                raise MediaSecurityError("archive links and device entries are not allowed")
            if member.size < 0 or member.size > policy.max_uncompressed_bytes:
                raise MediaSecurityError("archive entry exceeds uncompressed limit")
            total += member.size
            if total > policy.max_uncompressed_bytes:
                raise MediaSecurityError("archive uncompressed total exceeds limit")
            if (
                _archive_name(member.name)
                and _archive_depth(member.name) > policy.max_archive_depth
            ):
                raise MediaSecurityError("nested archive depth exceeds limit")
            if (
                member.isfile()
                and _is_text_filename(member.name)
                and member.size <= policy.max_text_chars * 8
            ):
                stream = archive.extractfile(member)
                if stream is not None:
                    with stream:
                        data = _read_stream_bounded(
                            stream, min(member.size, policy.max_text_chars * 8), policy
                        )
                    if (
                        _looks_like_text(data)
                        and sum(map(len, preview_parts)) < policy.max_text_chars
                    ):
                        preview_parts.append(
                            _bounded_text(
                                data, policy.max_text_chars - sum(map(len, preview_parts))
                            )
                        )
    result: dict[str, int | str] = {
        "entries": entries,
        "uncompressed_bytes": total,
    }
    if preview_parts:
        result["preview"] = "\n".join(preview_parts)[: policy.max_text_chars]
    return result


def _read_stream_bounded(stream: object, max_bytes: int, policy: MediaPolicy) -> bytes:
    read = getattr(stream, "read", None)
    if not callable(read):
        raise MediaSecurityError("archive stream is invalid")
    data = read(max_bytes + 1)
    if not isinstance(data, bytes) or len(data) > max_bytes:
        raise MediaSecurityError("archive preview exceeds limit")
    if len(data) > policy.max_uncompressed_bytes:
        raise MediaSecurityError("archive stream exceeds limit")
    return data


def _validate_archive_name(name: str) -> None:
    if not name or "\x00" in name or any(ord(character) < 0x20 for character in name):
        raise MediaSecurityError("archive entry name is invalid")
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise MediaSecurityError("archive absolute path is not allowed")
    parts = [part for part in normalized.split("/") if part not in {"", "."}]
    if any(part == ".." for part in parts):
        raise MediaSecurityError("archive path traversal is not allowed")
    if any(len(part) > 255 for part in parts) or len(normalized) > 1024:
        raise MediaSecurityError("archive entry name is too long")


def _archive_name(name: str) -> bool:
    return Path(name.replace("\\", "/")).suffix.casefold() in {
        ".zip",
        ".tar",
        ".tgz",
        ".gz",
        ".bz2",
        ".xz",
        ".7z",
        ".rar",
    }


def _archive_depth(name: str) -> int:
    # One archive member is depth 1; repeated archive-like suffixes model a
    # conservative nesting estimate without unpacking untrusted children.
    suffixes = Path(name.replace("\\", "/")).name.casefold().split(".")[1:]
    return sum(
        1 for suffix in suffixes if suffix in {"zip", "tar", "tgz", "gz", "bz2", "xz", "7z", "rar"}
    )


def _is_text_filename(name: str) -> bool:
    return Path(name).suffix.casefold() in {
        ".txt",
        ".md",
        ".csv",
        ".json",
        ".xml",
        ".yaml",
        ".yml",
        ".log",
    }


def _pdf_page_count(content: bytes) -> int:
    # This is intentionally a conservative upper-bound heuristic.  It never
    # interprets PDF actions or streams; an over-count fails closed.
    return max(1, len(re.findall(rb"/Type\s*/Page(?:\s|/|>)", content[:10_000_000])))
