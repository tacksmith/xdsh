"""Opt-in, bounded STT; provider credentials and audio never enter agent prompts."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import re
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from xdsh.core.providers import check_endpoint_dns, validate_endpoint


class SpeechError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SpeechEndpoint:
    url: str
    model: str
    credential_env: str
    protocol: str = "openai"
    local: bool = False

    def __post_init__(self) -> None:
        validate_endpoint(self.url)
        if self.protocol not in {"openai", "raw"}:
            raise ValueError("Unsupported STT protocol")
        if not re.fullmatch(r"[A-Za-z0-9_./:-]{1,128}", self.model):
            raise ValueError("Invalid STT model")
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", self.credential_env):
            raise ValueError("STT credentials must be environment references")
        if any(word in self.credential_env for word in ("TELEGRAM", "BOT_TOKEN", "ADMIN")):
            raise ValueError("Administrative credentials are not STT keys")
        host = urlsplit(self.url).hostname or ""
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
        if self.local != loopback:
            raise ValueError("Local STT requires literal loopback; online STT requires HTTPS")

    @property
    def consent_id(self) -> str:
        return hashlib.sha256(repr(self).encode()).hexdigest()[:24]


def speech_endpoints_from_env() -> dict[str, SpeechEndpoint]:
    result: dict[str, SpeechEndpoint] = {}
    for mode in ("local", "online"):
        prefix = f"XDSH_STT_{mode.upper()}_"
        url = os.environ.get(prefix + "URL")
        if url:
            result[mode] = SpeechEndpoint(
                url, os.environ.get(prefix + "MODEL", "whisper-1"),
                os.environ.get(prefix + "KEY_ENV", prefix + "KEY"),
                os.environ.get(prefix + "PROTOCOL", "openai"), local=mode == "local",
            )
    return result


class Transcriber(Protocol):
    async def transcribe(self, audio: bytes, endpoint: SpeechEndpoint) -> str: ...


class HttpTranscriber:
    """One HTTP attempt only: busy/timeout never falls back to another provider."""

    async def transcribe(self, audio: bytes, endpoint: SpeechEndpoint) -> str:
        if not 0 < len(audio) <= 10_000_000:
            raise SpeechError("Audio exceeds the 10 MB limit")
        await asyncio.to_thread(check_endpoint_dns, endpoint.url)
        key = os.environ.get(endpoint.credential_env, "")
        if not key or len(key) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in key):
            raise SpeechError("STT credential reference is not configured")
        headers = {"Authorization": f"Bearer {key}", "Accept-Encoding": "identity"}
        try:
            async with httpx.AsyncClient(
                trust_env=False, follow_redirects=False,
                timeout=httpx.Timeout(250, connect=10, pool=2),
                limits=httpx.Limits(max_connections=1),
            ) as client:
                request = (
                    client.build_request("POST", endpoint.url, headers=headers, content=audio)
                    if endpoint.protocol == "raw" else
                    client.build_request("POST", endpoint.url, headers=headers,
                                         data={"model": endpoint.model, "response_format": "json"},
                                         files={"file": ("voice.ogg", audio, "audio/ogg")})
                )
                response = await client.send(request, stream=True)
                try:
                    if response.status_code != 200:
                        raise SpeechError(f"STT HTTP {response.status_code}; no automatic retry")
                    if response.headers.get("content-encoding", "identity") != "identity":
                        raise SpeechError("Compressed STT responses are not accepted")
                    if response.headers.get("content-type", "").split(";")[0] != "application/json":
                        raise SpeechError("STT response must be JSON")
                    raw = bytearray()
                    async for chunk in response.aiter_raw(chunk_size=16_384):
                        raw.extend(chunk)
                        if len(raw) > 262_144:
                            raise SpeechError("STT response exceeds limit")
                    result = json.loads(raw)
                finally:
                    await response.aclose()
        except (httpx.HTTPError, ValueError, OSError):
            raise SpeechError("STT unavailable or invalid response; no cloud fallback") from None
        text = result.get("text") if isinstance(result, dict) else None
        if not isinstance(text, str) or not text.strip() or len(text) > 20_000:
            raise SpeechError("STT returned empty or oversized text")
        return text.strip()
