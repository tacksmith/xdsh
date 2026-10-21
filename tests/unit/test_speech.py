import json
from collections.abc import AsyncIterator

import httpx
import pytest

from xdsh.core.speech import HttpTranscriber, SpeechEndpoint, SpeechError


@pytest.mark.parametrize("url,local", [
    ("http://example.com/transcribe", False),
    ("http://192.168.1.2/transcribe", True),
    ("https://example.com/transcribe", True),
    ("http://127.0.0.1/transcribe", False),
    ("https://user:password@example.com/transcribe", False),
])
def test_speech_endpoint_rejects_wrong_trust_boundary(url: str, local: bool) -> None:
    with pytest.raises(ValueError):
        SpeechEndpoint(url, "small", "STT_KEY", local=local)


def test_changing_endpoint_model_or_key_reference_invalidates_consent() -> None:
    a = SpeechEndpoint("http://127.0.0.1:18081/transcribe", "small", "STT_KEY", local=True)
    b = SpeechEndpoint("http://127.0.0.1:18081/transcribe", "medium", "STT_KEY", local=True)
    assert a.consent_id != b.consent_id


@pytest.mark.parametrize("protocol", ["raw", "openai"])
@pytest.mark.parametrize("case", ["valid", "redirect", "busy", "empty", "oversize", "compressed"])
async def test_stt_protocols_are_bounded_and_do_not_retry(
    monkeypatch: pytest.MonkeyPatch, protocol: str, case: str,
) -> None:
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield json.dumps({"text": "" if case == "empty" else "hello"}).encode()
            if case == "oversize":
                yield b" " * 262_145

    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        headers = {"Content-Type": "application/json"}
        if case == "compressed":
            headers["Content-Encoding"] = "gzip"
        status = {"redirect": 302, "busy": 429}.get(case, 200)
        return httpx.Response(status, headers=headers, stream=Body())

    original = httpx.AsyncClient

    def factory(**kwargs: object) -> httpx.AsyncClient:
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        return original(transport=httpx.MockTransport(handle), trust_env=False)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    monkeypatch.setenv("STT_KEY", "synthetic-stt-credential")
    endpoint = SpeechEndpoint("http://127.0.0.1:18081/transcribe", "small", "STT_KEY",
                              protocol, local=True)
    if case == "valid":
        assert await HttpTranscriber().transcribe(b"OggSdata", endpoint) == "hello"
    else:
        with pytest.raises(SpeechError) as error:
            await HttpTranscriber().transcribe(b"OggSdata", endpoint)
        assert "synthetic-stt-credential" not in str(error.value)
    assert len(calls) == 1
    if protocol == "openai":
        assert "multipart/form-data" in calls[0].headers["Content-Type"]
        assert b"small" in calls[0].content
    else:
        assert calls[0].content == b"OggSdata"
