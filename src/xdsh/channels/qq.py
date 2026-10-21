"""Official QQ Bot WebSocket gateway for runner-neutral XDSH messages.

The Tencent SDK owns protocol details (OAuth, heartbeat, reconnect and Resume).
This module owns only the product boundary: explicit sender/group allowlists,
bounded normalization, durable non-secret Resume state, task concurrency and
safe outbound chunking.  The optional SDK is imported only when the gateway is
started, so installations that do not enable QQ keep no QQ runtime dependency.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import importlib.metadata
import importlib.util
import json
import logging
import math
import os
import re
import stat
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

import httpx

from xdsh.channels.base import (
    ChannelConfigurationError,
    ChannelMessage,
    ChannelPayloadError,
    ChannelResourceLimitError,
    SecretRef,
    split_text,
)
from xdsh.channels.router import MessageHandler
from xdsh.core.product_control import safe_exception_text

LOGGER = logging.getLogger(__name__)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
_SESSION_ID = re.compile(r"^[^\s\x00-\x1f\x7f]{1,512}$")
_MAX_STATE_BYTES = 16_384
_SESSION_MAX_AGE_SECONDS = 3_600.0
_QQ_MESSAGE_CHARS = 4_000
_QQ_SDK_VERSION = "1.2.2"


@dataclass(frozen=True, slots=True)
class QQInboundEvent:
    """SDK-independent QQ message fields consumed by XDSH."""

    event_type: str
    chat_scope: str
    chat_id: str
    user_id: str
    content: str
    message_id: str
    has_attachments: bool = False

    def __post_init__(self) -> None:
        for name, value in (
            ("event_type", self.event_type),
            ("chat_id", self.chat_id),
            ("user_id", self.user_id),
            ("message_id", self.message_id),
        ):
            if not isinstance(value, str) or not value or len(value) > 512 or "\x00" in value:
                raise ChannelPayloadError(f"QQ {name} is invalid")
        if self.chat_scope not in {"c2c", "group", "guild", "dm"}:
            raise ChannelPayloadError("QQ chat scope is invalid")
        if not isinstance(self.content, str) or len(self.content) > 32_000:
            raise ChannelResourceLimitError("QQ message content exceeds its bound")


@dataclass(frozen=True, slots=True)
class QQGatewayConfig:
    """Non-secret QQ product settings; credentials remain environment refs."""

    app_id: str
    app_secret_env: str
    state_root: Path
    allowed_sender_ids: frozenset[str]
    allowed_group_ids: frozenset[str] = frozenset()
    markdown: bool = False
    max_inflight: int = 4
    max_pending: int = 32
    send_retries: int = 3

    def __post_init__(self) -> None:
        if not self.app_id.isdigit() or not 1 <= len(self.app_id) <= 64:
            raise ChannelConfigurationError("QQ application ID is invalid")
        SecretRef(self.app_secret_env)
        root = self.state_root.expanduser().resolve()
        if (
            self.state_root.is_symlink()
            or root == Path(root.anchor)
            or ".xclaude" in root.parts
            or ".dsh" in root.parts
        ):
            raise ChannelConfigurationError("QQ state root must be a dedicated XDSH root")
        if not self.allowed_sender_ids:
            raise ChannelConfigurationError("QQ requires at least one allowed sender")
        for value in self.allowed_sender_ids | self.allowed_group_ids:
            if _IDENTIFIER.fullmatch(value) is None:
                raise ChannelConfigurationError("QQ allowlist contains an invalid identifier")
        if not 1 <= self.max_inflight <= 16:
            raise ChannelConfigurationError("QQ max inflight must be between 1 and 16")
        if not self.max_inflight <= self.max_pending <= 256:
            raise ChannelConfigurationError(
                "QQ max pending must be between max inflight and 256"
            )
        if not 1 <= self.send_retries <= 5:
            raise ChannelConfigurationError("QQ send retries must be between 1 and 5")


class QQAPIClient(Protocol):
    def setup(self, http_client: object) -> None: ...

    async def ensure_token(self) -> str: ...

    async def get_gateway_url(self) -> str: ...

    def ensure_token_sync(self) -> str: ...

    def get_gateway_url_sync(self) -> str: ...

    def clear_token(self) -> None: ...

    async def send_text(
        self,
        chat_type: str,
        chat_id: str,
        content: str,
        *,
        reply_to: str | None = None,
        markdown: bool = True,
        max_length: int = _QQ_MESSAGE_CHARS,
        retries: int = 3,
    ) -> Mapping[str, object]: ...

    async def send_typing(self, chat_id: str, msg_id: str) -> None: ...


class QQEventParser(Protocol):
    def parse(
        self,
        event_type: str,
        raw: Mapping[str, object],
    ) -> QQInboundEvent | None: ...


class QQWebSocket(Protocol):
    def start(self, gateway_url: str, main_loop: asyncio.AbstractEventLoop) -> None: ...

    async def async_stop(self) -> None: ...


@dataclass(frozen=True, slots=True)
class QQGatewayCallbacks:
    """Dependency-injection contract used by the official SDK wrapper."""

    on_message_event: Callable[[str, Mapping[str, object]], Awaitable[None]]
    on_connected: Callable[[], None]
    on_disconnected: Callable[[], None]
    on_fatal_error: Callable[[str, str], None]
    get_session: Callable[[], tuple[str | None, int | None]]
    set_session: Callable[[str | None, int | None], None]
    set_heartbeat_interval: Callable[[float], None]
    fail_pending: Callable[[str], None]
    on_heartbeat_ack: Callable[[], None]


@dataclass(frozen=True, slots=True)
class QQSDKRuntime:
    api: QQAPIClient
    parser: QQEventParser
    websocket: QQWebSocket
    close_http: Callable[[], Awaitable[None]]


class QQSDKFactory(Protocol):
    def create(
        self,
        *,
        app_id: str,
        app_secret: str,
        callbacks: QQGatewayCallbacks,
    ) -> QQSDKRuntime: ...


class _RawQQEventParser(Protocol):
    def parse(self, event_type: str, raw: dict[str, object]) -> object | None: ...


def require_qq_sdk() -> str:
    """Return the exact supported SDK version without making a network call."""

    if importlib.util.find_spec("qqbot_agent_sdk") is None:
        raise ChannelConfigurationError(
            "QQ support requires the optional 'qqbot-agent-sdk==1.2.2' package"
        )
    try:
        version = importlib.metadata.version("qqbot-agent-sdk")
    except importlib.metadata.PackageNotFoundError as error:
        raise ChannelConfigurationError("QQ SDK package metadata is unavailable") from error
    if version != _QQ_SDK_VERSION:
        raise ChannelConfigurationError(
            f"QQ support requires qqbot-agent-sdk=={_QQ_SDK_VERSION}"
        )
    return version


class QQGatewayAdapter:
    """Connect QQ's official outbound Gateway to a runner-neutral handler."""

    name = "qq"

    def __init__(
        self,
        config: QQGatewayConfig,
        *,
        environment: Mapping[str, str] | None = None,
        sdk_factory: QQSDKFactory | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self._environment = os.environ if environment is None else environment
        self._factory = sdk_factory or _OfficialQQSDKFactory()
        self._clock = clock
        self._state = _QQSessionState(config.state_root, config.app_id, clock=clock)
        self._runtime: QQSDKRuntime | None = None
        self._handler: MessageHandler | None = None
        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._fatal_event: asyncio.Event | None = None
        self._fatal_code: str | None = None
        self._connected = False
        self._status_lock = threading.Lock()
        self._semaphore = asyncio.Semaphore(config.max_inflight)
        self._pending_lock = asyncio.Lock()
        self._pending = 0
        self._seen: dict[str, float] = {}

    @property
    def connected(self) -> bool:
        with self._status_lock:
            return self._connected

    async def start(self, message_handler: MessageHandler) -> None:
        """Authenticate, bootstrap the official endpoint and start the Gateway."""

        if self._runtime is not None:
            raise ChannelConfigurationError("QQ gateway is already started")
        self._handler = message_handler
        self._main_loop = asyncio.get_running_loop()
        self._fatal_event = asyncio.Event()
        callbacks = QQGatewayCallbacks(
            on_message_event=self._on_message_event,
            on_connected=self._on_connected,
            on_disconnected=self._on_disconnected,
            on_fatal_error=self._on_fatal_error,
            get_session=self._state.get,
            set_session=self._state.set,
            set_heartbeat_interval=self._set_heartbeat_interval,
            fail_pending=self._fail_pending,
            on_heartbeat_ack=self._state.flush,
        )
        secret = SecretRef(self.config.app_secret_env).resolve(self._environment)
        runtime = self._factory.create(
            app_id=self.config.app_id,
            app_secret=secret,
            callbacks=callbacks,
        )
        self._runtime = runtime
        try:
            await runtime.api.ensure_token()
            gateway_url = await runtime.api.get_gateway_url()
            _validate_gateway_url(gateway_url)
            runtime.websocket.start(gateway_url, self._main_loop)
        except BaseException:
            await self.close()
            raise

    async def serve(
        self,
        stop_event: asyncio.Event,
        message_handler: MessageHandler,
    ) -> None:
        """Serve until an operator stop or a non-retryable SDK failure."""

        await self.start(message_handler)
        assert self._fatal_event is not None
        stop_wait = asyncio.create_task(stop_event.wait())
        fatal_wait = asyncio.create_task(self._fatal_event.wait())
        try:
            done, pending = await asyncio.wait(
                (stop_wait, fatal_wait),
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if fatal_wait in done and not stop_event.is_set():
                raise ChannelConfigurationError(
                    f"QQ gateway stopped with {self._fatal_code or 'fatal_error'}"
                )
        finally:
            await self.close()

    async def close(self) -> None:
        runtime, self._runtime = self._runtime, None
        self._handler = None
        if runtime is None:
            return
        try:
            await runtime.websocket.async_stop()
        finally:
            self._state.flush()
            await runtime.close_http()
            with self._status_lock:
                self._connected = False

    async def _on_message_event(
        self,
        event_type: str,
        raw: Mapping[str, object],
    ) -> None:
        runtime = self._runtime
        if runtime is None or self._handler is None:
            return
        try:
            event = runtime.parser.parse(event_type, raw)
        except Exception as error:
            LOGGER.warning(
                "QQ event parser rejected an event (%s)",
                safe_exception_text(error, max_chars=120),
            )
            return
        if event is None or not self._authorized(event) or self._is_replay(event.message_id):
            return
        admitted = await self._admit()
        if not admitted:
            await self._send_event_text(event, "系统繁忙, 请稍后重试。")
            return
        try:
            async with self._semaphore:
                await self._handle_event(event)
        except asyncio.CancelledError:
            self._seen.pop(event.message_id, None)
            raise
        except Exception as error:
            self._seen.pop(event.message_id, None)
            LOGGER.warning(
                "QQ message handling failed (%s)",
                safe_exception_text(error, max_chars=120),
            )
            try:
                await self._send_event_text(event, "任务未完成, 请稍后重试。")
            except Exception:
                LOGGER.warning("QQ failure notice could not be delivered")
        finally:
            await self._release()

    async def _handle_event(self, event: QQInboundEvent) -> None:
        assert self._runtime is not None
        assert self._handler is not None
        text = event.content.strip()
        if not text:
            if event.has_attachments:
                await self._send_event_text(event, "当前 QQ 接入需要附带文字说明。")
            return
        if event.chat_scope == "c2c":
            await self._runtime.api.send_typing(event.chat_id, event.message_id)
        handle = f"{event.chat_scope}:{event.chat_id}"
        message = ChannelMessage(
            channel="qq",
            sender_id=event.user_id,
            conversation_id=handle,
            text=text,
            message_id=event.message_id,
            reply_handle=handle,
            raw_event_type=event.event_type,
        )
        answer = await self._handler(message)
        if answer is not None and answer.strip():
            await self.send_text(message, answer)

    async def send_text(self, message: ChannelMessage, text: str) -> None:
        runtime = self._runtime
        if runtime is None:
            raise ChannelConfigurationError("QQ gateway is not started")
        if message.channel != "qq":
            raise ChannelPayloadError("QQ adapter received another channel message")
        scope, separator, chat_id = message.reply_handle.partition(":")
        if not separator or scope not in {"c2c", "group"} or not chat_id:
            raise ChannelPayloadError("QQ reply handle is invalid")
        chunks = split_text(text, maximum_chars=_QQ_MESSAGE_CHARS, maximum_chunks=32)
        for index, chunk in enumerate(chunks):
            await runtime.api.send_text(
                scope,
                chat_id,
                chunk,
                reply_to=message.message_id if index == 0 else None,
                markdown=self.config.markdown,
                max_length=_QQ_MESSAGE_CHARS,
                retries=self.config.send_retries,
            )

    def _authorized(self, event: QQInboundEvent) -> bool:
        if event.user_id not in self.config.allowed_sender_ids:
            return False
        if event.chat_scope == "c2c":
            return event.chat_id == event.user_id
        if event.chat_scope == "group":
            return event.chat_id in self.config.allowed_group_ids
        return False

    def _is_replay(self, message_id: str) -> bool:
        now = self._clock()
        cutoff = now - 900.0
        self._seen = {key: seen for key, seen in self._seen.items() if seen > cutoff}
        if message_id in self._seen:
            return True
        self._seen[message_id] = now
        if len(self._seen) > 2_048:
            newest = sorted(self._seen.items(), key=lambda item: item[1], reverse=True)[:1_024]
            self._seen = dict(newest)
        return False

    async def _admit(self) -> bool:
        async with self._pending_lock:
            if self._pending >= self.config.max_pending:
                return False
            self._pending += 1
            return True

    async def _release(self) -> None:
        async with self._pending_lock:
            self._pending = max(0, self._pending - 1)

    async def _send_event_text(self, event: QQInboundEvent, text: str) -> None:
        runtime = self._runtime
        if runtime is None or event.chat_scope not in {"c2c", "group"}:
            return
        await runtime.api.send_text(
            event.chat_scope,
            event.chat_id,
            text,
            reply_to=event.message_id,
            markdown=False,
            max_length=_QQ_MESSAGE_CHARS,
            retries=self.config.send_retries,
        )

    def _on_connected(self) -> None:
        with self._status_lock:
            self._connected = True
        self._state.flush()

    def _on_disconnected(self) -> None:
        with self._status_lock:
            self._connected = False

    def _on_fatal_error(self, code: str, message: str) -> None:
        del message
        safe_code = re.sub(r"[^A-Za-z0-9_.-]", "_", code)[:80] or "fatal_error"
        self._fatal_code = safe_code
        loop = self._main_loop
        event = self._fatal_event
        if loop is not None and event is not None:
            loop.call_soon_threadsafe(event.set)

    def _set_heartbeat_interval(self, seconds: float) -> None:
        if not math.isfinite(seconds) or seconds <= 0:
            self._on_fatal_error("invalid_heartbeat", "")

    def _fail_pending(self, reason: str) -> None:
        del reason
        # Runner tasks are not replayed or cancelled merely because the
        # Gateway transport reconnects; their REST reply can succeed later.


class _OfficialQQEventParser:
    def __init__(self, delegate: object) -> None:
        self._delegate = cast(_RawQQEventParser, delegate)

    def parse(
        self,
        event_type: str,
        raw: Mapping[str, object],
    ) -> QQInboundEvent | None:
        event = self._delegate.parse(event_type, dict(raw))
        if event is None:
            return None
        attachments = getattr(event, "attachments", ())
        return QQInboundEvent(
            event_type=str(getattr(event, "event_type", event_type)),
            chat_scope=str(getattr(event, "chat_scope", "")),
            chat_id=str(getattr(event, "chat_id", "")),
            user_id=str(getattr(event, "user_id", "")),
            content=str(getattr(event, "content", "")),
            message_id=str(getattr(event, "message_id", "")),
            has_attachments=bool(attachments),
        )


class _OfficialQQSDKFactory:
    def create(
        self,
        *,
        app_id: str,
        app_secret: str,
        callbacks: QQGatewayCallbacks,
    ) -> QQSDKRuntime:
        require_qq_sdk()
        try:
            sdk = importlib.import_module("qqbot_agent_sdk")
            constants = importlib.import_module("qqbot_agent_sdk.constants")
        except ImportError as error:
            raise ChannelConfigurationError(
                "QQ support requires the optional 'qqbot-agent-sdk==1.2.2' package"
            ) from error
        if (
            getattr(constants, "API_BASE", None) != "https://api.sgroup.qq.com"
            or getattr(constants, "TOKEN_URL", None)
            != "https://bots.qq.com/app/getAppAccessToken"
        ):
            raise ChannelConfigurationError("QQ SDK is not using official endpoints")

        api_constructor = cast(Callable[..., QQAPIClient], vars(sdk)["QQApiClient"])
        callbacks_constructor = cast(Callable[..., object], vars(sdk)["WSCallbacks"])
        websocket_constructor = cast(
            Callable[..., QQWebSocket],
            vars(sdk)["QQWebSocket"],
        )
        parser_constructor = cast(Callable[[], object], vars(sdk)["EventParser"])

        api = api_constructor(app_id=app_id, client_secret=app_secret, log_tag="XDSH-QQ")
        http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            follow_redirects=False,
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
            trust_env=True,
        )
        api.setup(http_client)

        async def on_message(event_type: str, raw: dict[str, Any]) -> None:
            await callbacks.on_message_event(event_type, cast(Mapping[str, object], raw))

        sdk_callbacks = callbacks_constructor(
            on_message_event=on_message,
            on_connected=callbacks.on_connected,
            on_disconnected=callbacks.on_disconnected,
            on_fatal_error=callbacks.on_fatal_error,
            get_token=api.ensure_token_sync,
            get_session=callbacks.get_session,
            set_session=callbacks.set_session,
            set_heartbeat_interval=callbacks.set_heartbeat_interval,
            clear_token=api.clear_token,
            fail_pending=callbacks.fail_pending,
            get_gateway_url=api.get_gateway_url_sync,
            on_heartbeat_ack=callbacks.on_heartbeat_ack,
        )
        websocket = websocket_constructor(callbacks=sdk_callbacks, log_tag="XDSH-QQ")
        parser = _OfficialQQEventParser(parser_constructor())
        return QQSDKRuntime(
            api=api,
            parser=parser,
            websocket=websocket,
            close_http=http_client.aclose,
        )


class _QQSessionState:
    def __init__(
        self,
        root: Path,
        app_id: str,
        *,
        clock: Callable[[], float],
    ) -> None:
        digest = hashlib.sha256(app_id.encode("utf-8")).hexdigest()[:16]
        self._root = root.expanduser().resolve()
        self._path = self._root / f"qq-gateway-{digest}.json"
        self._clock = clock
        self._lock = threading.Lock()
        self._session_id: str | None = None
        self._sequence: int | None = None
        self._dirty = False
        self._load()

    def get(self) -> tuple[str | None, int | None]:
        with self._lock:
            return self._session_id, self._sequence

    def set(self, session_id: str | None, sequence: int | None) -> None:
        if session_id is not None and _SESSION_ID.fullmatch(session_id) is None:
            raise ChannelPayloadError("QQ Gateway session ID is invalid")
        if sequence is not None and (
            not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0
        ):
            raise ChannelPayloadError("QQ Gateway sequence is invalid")
        with self._lock:
            self._session_id = session_id
            self._sequence = sequence
            self._dirty = True

    def flush(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            payload: dict[str, object] = {
                "schema_version": 1,
                "session_id": self._session_id,
                "sequence": self._sequence,
                "updated_at": self._clock(),
            }
            _write_json_atomic(self._path, payload)
            self._dirty = False

    def _load(self) -> None:
        if not self._path.exists():
            return
        if self._path.is_symlink() or not self._path.is_file():
            raise ChannelConfigurationError("QQ Gateway state must be a regular file")
        if self._path.stat().st_size > _MAX_STATE_BYTES:
            raise ChannelConfigurationError("QQ Gateway state exceeds its size bound")
        try:
            value = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ChannelConfigurationError("QQ Gateway state is invalid") from error
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise ChannelConfigurationError("QQ Gateway state schema is invalid")
        session_id = value.get("session_id")
        sequence = value.get("sequence")
        updated_at = value.get("updated_at")
        if session_id is not None and (
            not isinstance(session_id, str) or _SESSION_ID.fullmatch(session_id) is None
        ):
            raise ChannelConfigurationError("QQ Gateway stored session is invalid")
        if sequence is not None and (
            not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0
        ):
            raise ChannelConfigurationError("QQ Gateway stored sequence is invalid")
        if not isinstance(updated_at, int | float) or isinstance(updated_at, bool):
            raise ChannelConfigurationError("QQ Gateway stored timestamp is invalid")
        age = self._clock() - float(updated_at)
        if not math.isfinite(age) or age < 0 or age > _SESSION_MAX_AGE_SECONDS:
            return
        self._session_id = session_id
        self._sequence = sequence


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    if len(encoded) > _MAX_STATE_BYTES:
        raise ChannelResourceLimitError("QQ Gateway state exceeds its size bound")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise ChannelConfigurationError("QQ Gateway state root cannot be a symlink")
    path.parent.chmod(0o700)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        if stat.S_ISLNK(os.lstat(temporary).st_mode):
            raise ChannelConfigurationError("QQ Gateway temporary state is a symlink")
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _validate_gateway_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise ChannelConfigurationError("QQ returned an invalid Gateway URL") from error
    if (
        parsed.scheme != "wss"
        or parsed.hostname != "api.sgroup.qq.com"
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path != "/websocket"
    ):
        raise ChannelConfigurationError("QQ Gateway URL is outside the official QQ gateway")
