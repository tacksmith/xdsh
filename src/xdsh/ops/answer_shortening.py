"""No-tools shortening transport over the configured loopback Messages gateway."""

from __future__ import annotations

import asyncio
import os
from urllib.parse import urlsplit

import httpx

from xdsh.core.answer_shortening import ModelReply


class LoopbackShorteningBackend:
    def __init__(self, base_url: str, *, api_key_env: str = "XDSH_LITELLM_API_KEY") -> None:
        parsed = urlsplit(base_url)
        if (parsed.scheme not in {"http", "https"}
                or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in {"", "/"}):
            raise ValueError("shortening requires loopback gateway")
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self._capacity = asyncio.Semaphore(1)

    async def complete(self, *, model: str, system: str, text: str) -> ModelReply:
        key = os.environ.get(self.api_key_env, "")
        if not key:
            raise ValueError("shortening credential unavailable")
        async with self._capacity, httpx.AsyncClient(
            trust_env=False, follow_redirects=False, timeout=10,
        ) as client:
            async with client.stream(
                "POST", self.base_url + "/v1/messages",
                headers={"Authorization": "Bearer " + key, "anthropic-version": "2023-06-01"},
                json={"model": model, "max_tokens": 1536, "stream": False,
                      "thinking": {"type": "enabled", "budget_tokens": 1024},
                      "system": system, "messages": [{"role": "user", "content": text}]},
            ) as response:
                response.raise_for_status()
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 65_536:
                        raise ValueError("shortening response too large")
        import json

        payload = json.loads(data)
        if payload.get("stop_reason") != "end_turn":
            raise ValueError("shortening incomplete")
        content = payload["content"]
        if not isinstance(content, list) or any(
            not isinstance(block, dict)
            or block.get("type") not in {"text", "thinking", "redacted_thinking"}
            for block in content
        ):
            raise ValueError("unexpected shortening content")
        usage = payload["usage"]
        return ModelReply("".join(block["text"] for block in content if block["type"] == "text"),
                          usage["input_tokens"], usage["output_tokens"])
