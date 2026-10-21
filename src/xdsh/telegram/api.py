"""Small async client for the official Telegram Bot API."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, cast, runtime_checkable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import ProxyHandler, Request, build_opener

import httpx

from xdsh.core.product_control import safe_outbound_text
from xdsh.core.rate_limit import parse_retry_after


class _BotRequestLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # HTTP client request logs include token-bearing URLs at INFO level.
        return "/bot" not in record.getMessage()


_BOT_REQUEST_LOG_FILTER = _BotRequestLogFilter()
logging.getLogger("httpx").addFilter(_BOT_REQUEST_LOG_FILTER)


class TelegramApiError(RuntimeError):
    """A Telegram request failed without exposing its token-bearing URL."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = (
            status_code
            if isinstance(status_code, int)
            and not isinstance(status_code, bool)
            and 100 <= status_code <= 599
            else None
        )
        self.retry_after_seconds = (
            retry_after_seconds
            if isinstance(retry_after_seconds, int | float)
            and not isinstance(retry_after_seconds, bool)
            and math.isfinite(float(retry_after_seconds))
            and float(retry_after_seconds) >= 0
            else None
        )


class TelegramRequestRejected(TelegramApiError):
    """Telegram rejected a valid request before creating a message."""


class TelegramNotSent(TelegramApiError):
    """No message was submitted, or Telegram explicitly rate-limited it."""


_COMMAND_NAME = re.compile(r"^[a-z0-9_]{1,32}$")
_TELEGRAM_SEND_LIMIT = 3_800


@dataclass(frozen=True, slots=True)
class TelegramBotCommand:
    """One validated entry in Telegram's native slash-command menu."""

    command: str
    description: str

    def __post_init__(self) -> None:
        if _COMMAND_NAME.fullmatch(self.command) is None:
            raise ValueError(
                "Telegram command names must use 1-32 lowercase letters, digits, or underscores"
            )
        if not 1 <= len(self.description) <= 256:
            raise ValueError("Telegram command descriptions must use 1-256 characters")


@dataclass(frozen=True, slots=True)
class InlineKeyboardButton:
    """Validated callback-only inline keyboard button."""

    text: str
    callback_data: str

    def __post_init__(self) -> None:
        if not 1 <= len(self.text) <= 64:
            raise ValueError("Telegram inline button text must use 1-64 characters")
        if not 1 <= len(self.callback_data.encode("utf-8")) <= 64:
            raise ValueError("Telegram callback data must use 1-64 UTF-8 bytes")


@dataclass(frozen=True, slots=True)
class InlineKeyboardMarkup:
    """Small immutable Telegram inline keyboard."""

    rows: tuple[tuple[InlineKeyboardButton, ...], ...]

    def __post_init__(self) -> None:
        if not self.rows or any(not row for row in self.rows):
            raise ValueError("Telegram inline keyboard rows must be non-empty")
        if sum(len(row) for row in self.rows) > 100:
            raise ValueError("Telegram inline keyboard accepts at most 100 buttons")

    def as_payload(self) -> dict[str, object]:
        return {
            "inline_keyboard": [
                [
                    {"text": button.text, "callback_data": button.callback_data}
                    for button in row
                ]
                for row in self.rows
            ]
        }


class TelegramApi(Protocol):
    """Transport seam used by the bot service and isolated tests."""

    async def get_me(self) -> None: ...

    async def set_commands(
        self,
        commands: Sequence[TelegramBotCommand],
        *,
        chat_id: int | None = None,
    ) -> None: ...

    async def get_updates(
        self, *, offset: int | None, timeout_seconds: int
    ) -> tuple[Mapping[str, object], ...]: ...

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> int: ...

    async def edit_message(
        self, chat_id: int, message_id: int, text: str, *,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None: ...

    async def send_markdown(self, chat_id: int, text: str) -> None: ...

    async def send_document(
        self,
        chat_id: int,
        filename: str,
        content: bytes,
    ) -> None: ...

    async def download_file(self, file_id: str, *, max_bytes: int) -> bytes: ...

    async def send_chat_action(self, chat_id: int, action: str) -> None: ...

    async def answer_callback_query(self, callback_query_id: str) -> None: ...


@runtime_checkable
class ProgressMessageApi(Protocol):
    """Optional transport capability for silent, replaceable progress cards."""

    async def send_silent_message(self, chat_id: int, text: str) -> int: ...

    async def delete_message(self, chat_id: int, message_id: int) -> None: ...


class TelegramBotApi:
    """JSON-over-HTTPS Bot API client with token-safe failures."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str = "https://api.telegram.org",
        proxy_url: str | None = None,
        pooled: bool = False,
    ) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._proxy_url = proxy_url
        self._http_client = (
            httpx.AsyncClient(
                proxy=proxy_url, trust_env=False, follow_redirects=False,
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=8,
                                    keepalive_expiry=60),
            ) if pooled else None
        )

    async def aclose(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()

    async def get_me(self) -> None:
        await self._call("getMe", {}, network_timeout_seconds=15.0)

    async def set_commands(
        self,
        commands: Sequence[TelegramBotCommand],
        *,
        chat_id: int | None = None,
    ) -> None:
        if len(commands) > 100:
            raise ValueError("Telegram accepts at most 100 bot commands")
        names = [command.command for command in commands]
        if len(names) != len(set(names)):
            raise ValueError("Telegram bot command names must be unique")
        payload: dict[str, object] = {
            "commands": [
                {
                    "command": command.command,
                    "description": command.description,
                }
                for command in commands
            ]
        }
        if chat_id is not None:
            if chat_id == 0:
                raise ValueError("Telegram chat ID must be non-zero")
            payload["scope"] = {"type": "chat", "chat_id": chat_id}
        await self._call("setMyCommands", payload, network_timeout_seconds=15.0)

    async def get_updates(
        self, *, offset: int | None, timeout_seconds: int
    ) -> tuple[Mapping[str, object], ...]:
        request: dict[str, object] = {
            "timeout": timeout_seconds,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            request["offset"] = offset
        result = await self._call(
            "getUpdates",
            request,
            network_timeout_seconds=float(timeout_seconds + 15),
        )
        if not isinstance(result, list):
            raise TelegramApiError("Telegram getUpdates returned an invalid result")
        updates: list[Mapping[str, object]] = []
        for item in cast(list[object], result):
            mapping = _string_mapping(item)
            if mapping is None:
                raise TelegramApiError("Telegram getUpdates returned an invalid update")
            updates.append(mapping)
        return tuple(updates)

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: InlineKeyboardMarkup | None = None,
        disable_notification: bool = False,
        network_timeout_seconds: float = 30.0,
    ) -> int:
        safe_text = safe_outbound_text(text, max_chars=30_000)
        payload: dict[str, object] = {
            "chat_id": chat_id,
            "text": safe_text,
            "link_preview_options": {"is_disabled": True},
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup.as_payload()
        if disable_notification:
            payload["disable_notification"] = True
        result = await self._call(
            "sendMessage",
            payload,
            network_timeout_seconds=network_timeout_seconds,
        )
        message = _string_mapping(result)
        message_id = message.get("message_id") if message is not None else None
        if not isinstance(message_id, int) or isinstance(message_id, bool):
            raise TelegramApiError("Telegram sendMessage returned no message ID")
        return message_id

    async def send_silent_message(self, chat_id: int, text: str) -> int:
        safe_text = safe_outbound_text(text, max_chars=4000)
        payload: dict[str, object] = {
            "chat_id": chat_id, "text": _markdown_to_telegram_html(safe_text),
            "parse_mode": "HTML", "disable_notification": True,
            "link_preview_options": {"is_disabled": True},
        }
        try:
            result = await self._call("sendMessage", payload, network_timeout_seconds=5.0)
        except TelegramRequestRejected as error:
            if error.status_code != 400:
                raise
            return await self.send_message(chat_id, safe_text, disable_notification=True,
                                           network_timeout_seconds=5.0)
        message = _string_mapping(result)
        message_id = message.get("message_id") if message is not None else None
        if not isinstance(message_id, int) or isinstance(message_id, bool) or message_id <= 0:
            raise TelegramApiError("Telegram silent send returned no message ID")
        return message_id

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        if message_id <= 0:
            raise ValueError("Telegram message ID must be positive")
        result = await self._call("deleteMessage", {"chat_id": chat_id, "message_id": message_id},
                                  network_timeout_seconds=5.0)
        if result is not True:
            raise TelegramApiError("Telegram deleteMessage did not confirm deletion")

    async def edit_message(
        self, chat_id: int, message_id: int, text: str, *,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        if message_id <= 0:
            raise ValueError("Telegram message ID must be positive")
        safe_text = safe_outbound_text(text, max_chars=30_000)
        payload: dict[str, object] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": safe_text,
            "link_preview_options": {"is_disabled": True},
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup.as_payload()
        await self._call(
            "editMessageText", payload,
            network_timeout_seconds=10.0,
        )

    async def send_markdown(self, chat_id: int, text: str) -> None:
        """Send formatted text through the official Bot API.

        XDSH used to call a non-standard ``sendRichMessage`` endpoint here.
        That endpoint is not part of Telegram's Bot API and some relays
        normalize whitespace before forwarding it, which makes intentional
        blank lines disappear.  Use the official ``sendMessage`` endpoint so
        Telegram receives the exact newline sequence.  A plain-text fallback
        keeps delivery reliable when a provider rejects the bounded HTML.
        """

        safe_text = safe_outbound_text(text, max_chars=30_000)
        for chunk in _split_telegram_text(safe_text, limit=_TELEGRAM_SEND_LIMIT):
            payload: dict[str, object] = {
                "chat_id": chat_id,
                "text": _markdown_to_telegram_html(chunk),
                "parse_mode": "HTML",
                "link_preview_options": {"is_disabled": True},
            }
            try:
                await self._call(
                    "sendMessage",
                    payload,
                    network_timeout_seconds=30.0,
                )
            except TelegramRequestRejected:
                await self.send_message(chat_id, chunk)

    async def send_document(
        self,
        chat_id: int,
        filename: str,
        content: bytes,
    ) -> None:
        """Upload one bounded in-memory document without writing a temp file."""

        if (
            not filename
            or len(filename) > 120
            or any(character in filename for character in '\r\n/\\"')
        ):
            raise ValueError("Telegram document filename is invalid")
        if not 1 <= len(content) <= 10_000_000:
            raise ValueError("Telegram document must use 1-10000000 bytes")
        await asyncio.to_thread(self._send_document_sync, chat_id, filename, content)

    async def download_file(self, file_id: str, *, max_bytes: int) -> bytes:
        """Resolve and download one bounded Telegram attachment."""

        if not file_id or len(file_id) > 512 or any(character.isspace() for character in file_id):
            raise ValueError("Telegram file ID is invalid")
        if not 1 <= max_bytes <= 20_000_000:
            raise ValueError("Telegram download limit must use 1-20000000 bytes")
        result = await self._call(
            "getFile",
            {"file_id": file_id},
            network_timeout_seconds=10.0,
        )
        file = _string_mapping(result)
        path = file.get("file_path") if file is not None else None
        size = file.get("file_size") if file is not None else None
        if not isinstance(path, str) or not _safe_remote_file_path(path):
            raise TelegramApiError("Telegram getFile returned an invalid path")
        if isinstance(size, int) and not isinstance(size, bool) and size > max_bytes:
            raise TelegramRequestRejected("Telegram attachment exceeds the configured limit")
        return await asyncio.to_thread(self._download_file_sync, path, max_bytes)

    async def send_chat_action(self, chat_id: int, action: str) -> None:
        await self._call(
            "sendChatAction",
            {"chat_id": chat_id, "action": action},
            network_timeout_seconds=15.0,
        )

    async def answer_callback_query(self, callback_query_id: str) -> None:
        if not callback_query_id:
            raise ValueError("Telegram callback query ID must be non-empty")
        await self._call(
            "answerCallbackQuery",
            {"callback_query_id": callback_query_id},
            network_timeout_seconds=2.0,
        )

    async def _call(
        self,
        method: str,
        payload: Mapping[str, object],
        *,
        network_timeout_seconds: float,
    ) -> object:
        if self._http_client is not None:
            if method == "getUpdates":
                # Receiving commands must not depend on progress/outbound pool
                # capacity. A fresh bounded client per long poll also discards
                # stale proxy connections instead of retrying a poisoned pool.
                async with httpx.AsyncClient(
                    proxy=self._proxy_url, trust_env=False, follow_redirects=False,
                    limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
                ) as polling_client:
                    try:
                        return await asyncio.wait_for(
                            self._pooled_call(
                                method, payload, network_timeout_seconds, client=polling_client,
                            ), timeout=network_timeout_seconds + 5,
                        )
                    except asyncio.TimeoutError:
                        raise TelegramApiError("Telegram polling deadline exceeded") from None
            attempts = 3 if method == "sendMessage" else 1
            for attempt in range(attempts):
                try:
                    return await self._pooled_call(method, payload, network_timeout_seconds)
                except TelegramNotSent as error:
                    delay = error.retry_after_seconds
                    delay = max(0.25, delay if delay is not None else 0.5 * 2 ** attempt)
                    if attempt + 1 == attempts or delay > 5:
                        raise
                    await asyncio.sleep(delay)
            raise AssertionError("unreachable retry state")
        return await asyncio.to_thread(
            self._call_sync,
            method,
            payload,
            network_timeout_seconds,
        )

    async def _pooled_call(
        self, method: str, payload: Mapping[str, object], timeout: float,
        *, client: httpx.AsyncClient | None = None,
    ) -> object:
        assert self._http_client is not None
        try:
            response = await (client or self._http_client).post(
                f"{self._base_url}/bot{self._token}/{method}", json=dict(payload),
                timeout=httpx.Timeout(timeout, connect=min(timeout, 15.0), pool=2.0),
            )
        except (httpx.ConnectTimeout, httpx.ConnectError, httpx.PoolTimeout) as error:
            if isinstance(error, httpx.PoolTimeout) and client is None:
                # PoolTimeout proves no request was submitted. A one-shot
                # independent connection may safely bypass exhausted slots;
                # never do this for read/write/response uncertainty.
                async with httpx.AsyncClient(
                    proxy=self._proxy_url, trust_env=False, follow_redirects=False,
                    limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
                ) as recovery_client:
                    return await self._pooled_call(
                        method, payload, timeout, client=recovery_client,
                    )
            logging.getLogger(__name__).warning(
                "Telegram transport not-submitted: method=%s class=%s",
                method, type(error).__name__,
            )
            raise TelegramNotSent(
                f"Telegram {method} not submitted ({type(error).__name__})",
            ) from None
        except (httpx.HTTPError, OSError) as error:
            logging.getLogger(__name__).warning(
                "Telegram transport uncertain: method=%s class=%s",
                method, type(error).__name__,
            )
            raise TelegramApiError(
                f"Telegram {method} request failed ({type(error).__name__})",
            ) from None
        status = response.status_code
        # A proxy HTTP 429 alone is not proof that Telegram rejected the request.
        # Require Telegram's structured negative acknowledgement before retrying.
        try:
            envelope = _string_mapping(response.json())
        except (ValueError, UnicodeDecodeError):
            envelope = None
        if (envelope is not None and envelope.get("ok") is False
                and _api_status_code(envelope) == 429):
            raise TelegramNotSent(
                f"Telegram {method} rate limited", status_code=429,
                retry_after_seconds=(
                    _api_retry_after(envelope)
                    if _api_retry_after(envelope) is not None else
                    parse_retry_after(response.headers.get("Retry-After"))
                ),
            )
        if not 200 <= status < 300:
            error_type = (
                TelegramRequestRejected if 400 <= status < 500 and status != 429
                else TelegramApiError
            )
            raise error_type(
                f"Telegram {method} failed with HTTP status {status}", status_code=status,
                retry_after_seconds=parse_retry_after(response.headers.get("Retry-After")),
            )
        try:
            decoded = response.json()
        except (ValueError, UnicodeDecodeError):
            raise TelegramApiError(f"Telegram {method} returned invalid JSON") from None
        mapping = _string_mapping(decoded)
        if mapping is None or mapping.get("ok") is not True:
            raise TelegramRequestRejected(
                f"Telegram {method} returned an API error",
                status_code=_api_status_code(mapping),
                retry_after_seconds=_api_retry_after(mapping),
            )
        return mapping.get("result")

    def _call_sync(
        self,
        method: str,
        payload: Mapping[str, object],
        network_timeout_seconds: float,
    ) -> object:
        endpoint = f"{self._base_url}/bot{self._token}/{method}"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            endpoint,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        proxies = (
            {}
            if self._proxy_url is None
            else {"http": self._proxy_url, "https": self._proxy_url}
        )
        opener = build_opener(ProxyHandler(proxies))
        try:
            with opener.open(request, timeout=network_timeout_seconds) as response:
                raw_response = response.read()
        except HTTPError as error:
            error_type = (
                TelegramRequestRejected
                if 400 <= error.code < 500 and error.code != 429
                else TelegramApiError
            )
            raise error_type(
                f"Telegram {method} failed with HTTP status {error.code}",
                status_code=error.code,
                retry_after_seconds=parse_retry_after(
                    error.headers.get("Retry-After") if error.headers is not None else None
                ),
            ) from None
        except (URLError, TimeoutError, OSError):
            raise TelegramApiError(f"Telegram {method} request failed") from None
        try:
            decoded = cast(object, json.loads(raw_response))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise TelegramApiError(f"Telegram {method} returned invalid JSON") from None
        response_mapping = _string_mapping(decoded)
        if response_mapping is None or response_mapping.get("ok") is not True:
            raise TelegramRequestRejected(
                f"Telegram {method} returned an API error",
                status_code=_api_status_code(response_mapping),
                retry_after_seconds=_api_retry_after(response_mapping),
            )
        return response_mapping.get("result")

    def _send_document_sync(self, chat_id: int, filename: str, content: bytes) -> None:
        method = "sendDocument"
        boundary = f"XDSH-{uuid.uuid4().hex}"
        body = b"".join(
            (
                f"--{boundary}\r\n".encode("ascii"),
                b'Content-Disposition: form-data; name="chat_id"\r\n\r\n',
                str(chat_id).encode("ascii"),
                b"\r\n",
                f"--{boundary}\r\n".encode("ascii"),
                (
                    'Content-Disposition: form-data; name="document"; '
                    f'filename="{filename}"\r\n'
                ).encode(),
                b"Content-Type: text/markdown; charset=utf-8\r\n\r\n",
                content,
                b"\r\n",
                f"--{boundary}--\r\n".encode("ascii"),
            )
        )
        endpoint = f"{self._base_url}/bot{self._token}/{method}"
        request = Request(
            endpoint,
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        proxies = (
            {}
            if self._proxy_url is None
            else {"http": self._proxy_url, "https": self._proxy_url}
        )
        opener = build_opener(ProxyHandler(proxies))
        try:
            with opener.open(request, timeout=60.0) as response:
                raw_response = response.read()
        except HTTPError as error:
            error_type = (
                TelegramRequestRejected
                if 400 <= error.code < 500 and error.code != 429
                else TelegramApiError
            )
            raise error_type(
                f"Telegram {method} failed with HTTP status {error.code}",
                status_code=error.code,
                retry_after_seconds=parse_retry_after(
                    error.headers.get("Retry-After") if error.headers is not None else None
                ),
            ) from None
        except (URLError, TimeoutError, OSError):
            raise TelegramApiError(f"Telegram {method} request failed") from None
        try:
            decoded = cast(object, json.loads(raw_response))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise TelegramApiError(f"Telegram {method} returned invalid JSON") from None
        response_mapping = _string_mapping(decoded)
        if response_mapping is None or response_mapping.get("ok") is not True:
            raise TelegramRequestRejected(
                f"Telegram {method} returned an API error",
                status_code=_api_status_code(response_mapping),
                retry_after_seconds=_api_retry_after(response_mapping),
            )

    def _download_file_sync(self, path: str, max_bytes: int) -> bytes:
        endpoint = f"{self._base_url}/file/bot{self._token}/{quote(path, safe='/')}"
        request = Request(endpoint, headers={"Accept": "application/octet-stream"}, method="GET")
        proxies = (
            {}
            if self._proxy_url is None
            else {"http": self._proxy_url, "https": self._proxy_url}
        )
        opener = build_opener(ProxyHandler(proxies))
        try:
            with opener.open(request, timeout=60.0) as response:
                content_length = response.headers.get("Content-Length")
                if content_length is not None:
                    try:
                        declared_size = int(content_length)
                    except ValueError:
                        declared_size = 0
                    if declared_size > max_bytes:
                        raise TelegramRequestRejected(
                            "Telegram attachment exceeds the configured limit"
                        )
                content = cast(bytes, response.read(max_bytes + 1))
        except TelegramRequestRejected:
            raise
        except HTTPError as error:
            error_type = (
                TelegramRequestRejected
                if 400 <= error.code < 500 and error.code != 429
                else TelegramApiError
            )
            raise error_type(
                f"Telegram file download failed with HTTP status {error.code}",
                status_code=error.code,
                retry_after_seconds=parse_retry_after(
                    error.headers.get("Retry-After") if error.headers is not None else None
                ),
            ) from None
        except (URLError, TimeoutError, OSError):
            raise TelegramApiError("Telegram file download failed") from None
        if len(content) > max_bytes:
            raise TelegramRequestRejected("Telegram attachment exceeds the configured limit")
        return content


def _string_mapping(value: object) -> Mapping[str, object] | None:
    if not isinstance(value, dict):
        return None
    raw = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in raw):
        return None
    return cast(Mapping[str, object], raw)


def _api_status_code(value: Mapping[str, object] | None) -> int | None:
    """Read Telegram's structured error code without trusting free-form text."""

    if value is None:
        return None
    raw = value.get("error_code")
    if isinstance(raw, int) and not isinstance(raw, bool) and 100 <= raw <= 599:
        return raw
    return None


def _api_retry_after(value: Mapping[str, object] | None) -> float | None:
    """Read the documented ``parameters.retry_after`` hint, if present."""

    if value is None:
        return None
    parameters = _string_mapping(value.get("parameters"))
    if parameters is None:
        return None
    return parse_retry_after(parameters.get("retry_after"))


def _safe_remote_file_path(path: str) -> bool:
    parts = path.split("/")
    return (
        bool(path)
        and not path.startswith("/")
        and all(part not in {"", ".", ".."} for part in parts)
        and "\\" not in path
        and "?" not in path
        and "#" not in path
    )


def _safe_rich_markdown(text: str) -> str:
    """Neutralize embedded Rich HTML while preserving Markdown structure."""

    text = _preserve_prose_breaks(text)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _markdown_to_telegram_html(text: str) -> str:
    """Convert the small Markdown subset used by XDSH to Telegram HTML.

    This deliberately preserves every newline, including empty lines.  It is
    not a general CommonMark parser; unsupported syntax remains readable as
    escaped text and the caller can fall back to plain text if Telegram
    rejects a malformed model fragment.
    """

    lines = _normalize_heading_spacing(text).split("\n")
    output: list[str] = []
    in_fence = False
    fence_lines: list[str] = []
    for line in lines:
        fence = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence:
            if in_fence:
                output.append(
                    "<pre><code>"
                    + _escape_html("\n".join(fence_lines))
                    + "</code></pre>"
                )
                fence_lines = []
                in_fence = False
            else:
                in_fence = True
            continue
        if in_fence:
            fence_lines.append(line)
            continue
        heading = re.match(r"^ {0,3}#{1,6}\s+(.*)$", line)
        if heading:
            output.append("<b>" + _markdown_inline_to_html(heading.group(1)) + "</b>")
        else:
            output.append(_markdown_inline_to_html(line))
    if in_fence:
        output.append("<pre><code>" + _escape_html("\n".join(fence_lines)) + "</code></pre>")
    return "\n".join(output)


def _normalize_heading_spacing(text: str) -> str:
    """Keep Markdown headings visually attached to the content below.

    XDSH's answer contract uses headings as section labels, not paragraph
    bodies.  A heading therefore gets one separating blank line before it
    (except at the beginning) and none after it.  Fenced code stays literal.
    """

    lines = text.split("\n")
    output: list[str] = []
    fence: str | None = None
    index = 0
    while index < len(lines):
        line = lines[index]
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if marker:
            run = marker.group(1)
            if fence is None:
                fence = run
            elif run[0] == fence[0] and len(run) >= len(fence):
                fence = None
            output.append(line)
            index += 1
            continue
        if fence is None and _is_markdown_heading(line):
            while output and not output[-1].strip():
                output.pop()
            if output:
                output.append("")
            output.append(line)
            index += 1
            while index < len(lines) and not lines[index].strip():
                index += 1
            continue
        output.append(line)
        index += 1
    return "\n".join(output)


def _is_markdown_heading(line: str) -> bool:
    """Recognize both ATX and the bold-label headings emitted by XDSH."""

    return bool(
        re.match(r"^ {0,3}#{1,6}\s+", line)
        or re.match(r"^\s*\*\*[^*\n]{1,80}\*\*\s*$", line)
    )


def _escape_html(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _split_telegram_text(text: str, *, limit: int) -> tuple[str, ...]:
    """Split text at safe newline boundaries without trimming whitespace."""

    if not text:
        return ()
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        boundary = remaining.rfind("\n", 0, limit)
        if boundary < limit // 2:
            boundary = limit
        else:
            boundary += 1
        chunks.append(remaining[:boundary])
        remaining = remaining[boundary:]
    chunks.append(remaining)
    return tuple(chunks)


def _markdown_inline_to_html(value: str) -> str:
    """Escape inline text and translate safe emphasis/code/link constructs."""

    tokens: list[str] = []

    def stash(fragment: str) -> str:
        token = f"\x00{len(tokens)}\x00"
        tokens.append(fragment)
        return token

    protected = re.sub(
        r"`([^`\n]+)`",
        lambda match: stash("<code>" + _escape_html(match.group(1)) + "</code>"),
        value,
    )
    protected = re.sub(
        r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)",
        lambda match: stash(
            '<a href="' + _escape_html(match.group(2)) + '">' +
            _escape_html(match.group(1)) + "</a>"
        ),
        protected,
    )
    escaped = _escape_html(protected)
    escaped = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", escaped)
    escaped = re.sub(r"__([^_\n]+)__", r"<b>\1</b>", escaped)
    escaped = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", escaped)
    escaped = re.sub(r"(?<!_)_([^_\n]+)_(?!_)", r"<i>\1</i>", escaped)
    for index, token in enumerate(tokens):
        escaped = escaped.replace(f"\x00{index}\x00", token)
    return escaped


def _preserve_prose_breaks(text: str) -> str:
    """Make ordinary prose newlines visible without decoding literal escapes.

    Conservative around code/structural Markdown: ambiguous lines are retained
    verbatim. Two trailing spaces express a GFM hard break, not a new paragraph.
    """
    lines = text.splitlines(keepends=True)
    fence: str | None = None
    inline_ticks = 0
    output: list[str] = []
    for index, raw in enumerate(lines):
        line = raw.rstrip("\r\n")
        ending = raw[len(line):]
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        protected = fence is not None or inline_ticks != 0
        if marker:
            run = marker.group(1)
            if fence is None:
                fence = run
            elif run[0] == fence[0] and len(run) >= len(fence) and not line[marker.end():].strip():
                fence = None
            protected = True
        if fence is None and not marker:
            for ticks in re.finditer(r"(?<!\\)`+", line):
                size = len(ticks.group())
                if inline_ticks == 0:
                    inline_ticks = size
                elif size == inline_ticks:
                    inline_ticks = 0
            protected |= inline_ticks != 0
        next_line = lines[index + 1].rstrip("\r\n") if index + 1 < len(lines) else ""
        structural = any(
            not value.strip() or value.startswith(("    ", "\t"))
            or re.match(r"^ {0,3}(?:#{1,6}\s|[>|]|`{3,}|~{3,}|[-=*_]{3,}\s*$)", value)
            or "|" in value
            for value in (line, next_line)
        )
        if ending and not protected and not structural and not line.endswith(("  ", "\\")):
            line += "  "
        output.append(line + ending)
    return "".join(output)
