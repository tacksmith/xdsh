# Voice messages

Send a Telegram voice message. XDSH asks once whether to use a configured local
STT service or an online STT service. Before approval it retains only a temporary
Telegram file reference, not downloaded audio. Choices expire after ten minutes.
Use `/media` → **Speech settings** to change or disable transcription.

After approval, XDSH validates/downloads at most 10 MB, transcribes in memory,
and sends the text through the normal task/queue/credential-approval path.
The selected LLM may be online even when STT is local. Transcribed text has the
same conversation retention as typed instructions; audio is not saved to the
agent workspace. Transcription failure never falls back to a cloud service.
`/cancel` cancels the client request; a local server may finish its bounded
inference later. Maximum declared duration: ten minutes; request deadline: 260s.

## Existing local service

Configure an authenticated service reachable on **literal loopback**, e.g.:

```dotenv
XDSH_STT_LOCAL_URL=http://127.0.0.1:18081/transcribe
XDSH_STT_LOCAL_PROTOCOL=raw
XDSH_STT_LOCAL_MODEL=small
XDSH_STT_LOCAL_KEY_ENV=MY_LOCAL_STT_KEY
```

Keep `MY_LOCAL_STT_KEY` in the service's private environment file. `raw` sends the
audio bytes with Bearer authentication and expects `{"text":"..."}` JSON.
For an OpenAI-compatible local server, set protocol `openai` and use its complete
`/v1/audio/transcriptions` URL. Containers need an operator-managed loopback
forward to their private speech service; XDSH does not scan private networks.

## Deploy an optional local model

The core package does not download model weights or install GPU dependencies.
Explicitly install the optional runtime in the same environment:

```bash
python -m pip install faster-whisper==1.2.1
xdsh-speech download --model-dir "$HOME/.xdsh/speech/small" --approve-download
xdsh-speech serve --model-dir "$HOME/.xdsh/speech/small"
```

Before serving, set a separate random `XDSH_STT_LOCAL_KEY` in a private environment
file and load it into both service and Bot; do not paste it in shell arguments.
The first command installs packages; the second downloads the `small` model only
after explicit consent. The server defaults to CPU/int8; `--device cuda` requires
an independently working CUDA installation. It binds only 127.0.0.1, admits one
inference at a time, runs inference in a child with a 240s deadline, and removes
its temporary audio file afterward. Run it under your service/container manager
with suitable memory/CPU limits. The Bot is never given Docker socket access.

Clicking **Deploy local model** on an unconfigured Bot shows these prerequisites;
it does not yet install packages or manage host services from Telegram.

## Online STT

Set `XDSH_STT_ONLINE_URL` to the full trusted HTTPS transcription endpoint,
`XDSH_STT_ONLINE_MODEL` to the exact STT model, and
`XDSH_STT_ONLINE_KEY_ENV` to a separate environment variable holding its API key.
The default `openai` protocol uses multipart `file`, `model`, `response_format=json`.
Users must explicitly choose online before any audio upload. Public DNS checks,
no redirects, bounded JSON responses and no automatic retries are enforced.
Changing endpoint/model/key reference invalidates prior consent.

Online protocol fixtures pass, but not every online vendor is live-verified.
The existing 1080 local Faster-Whisper service is separate from this optional
standalone server; XDSH connects to it without modifying or restarting it.
