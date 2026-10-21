"""Credential-free XDSH settings CLI and authenticated loopback Web UI."""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import signal
import sys
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

from xdsh.core.product_control import safe_exception_text
from xdsh.core.settings import (
    JsonSettingsStore,
    ModelSettings,
    ProviderSettings,
    RemoveModel,
    RemoveProvider,
    ReplaceSettings,
    SetDefaultModel,
    SettingsAction,
    SettingsController,
    SettingsError,
    SettingsSnapshot,
    UpsertModel,
    UpsertProvider,
    settings_from_document,
)

_DEFAULT_PATH = Path("~/.xdsh/config/settings.json")
_TOKEN_ENV = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_MAX_BODY_BYTES = 64 * 1024
_BOOL_CHOICES = ("auto", "on", "off")


class ConfigUiError(ValueError):
    """The local settings interface is unsafe or malformed."""


@dataclass(frozen=True, slots=True)
class ConfigUiRequestError(RuntimeError):
    status: int
    message: str
    headers: Mapping[str, str] | None = None

    def __str__(self) -> str:
        return self.message


class ConfigUiPolicy:
    """Pure loopback/auth/request policy shared by the HTTP adapter and tests."""

    def __init__(
        self,
        *,
        port: int,
        token_env: str,
        environment: Mapping[str, str] | None = None,
        max_body_bytes: int = _MAX_BODY_BYTES,
    ) -> None:
        if not 1 <= port <= 65_535:
            raise ConfigUiError("settings UI port is invalid")
        if _TOKEN_ENV.fullmatch(token_env) is None:
            raise ConfigUiError("settings UI token environment name is invalid")
        if not 1_024 <= max_body_bytes <= _MAX_BODY_BYTES:
            raise ConfigUiError("settings UI request limit is invalid")
        self.port = port
        self.token_env = token_env
        self.environment = os.environ if environment is None else environment
        self.max_body_bytes = max_body_bytes
        self._token()

    def authorize(
        self,
        *,
        host: str,
        origin: str | None,
        headers: Mapping[str, str],
    ) -> None:
        normalized_host = host.strip().casefold().rstrip(".")
        if normalized_host not in {
            f"127.0.0.1:{self.port}",
            f"localhost:{self.port}",
        }:
            raise ConfigUiRequestError(403, "request rejected")
        if origin is not None:
            try:
                parsed = urlsplit(origin)
                origin_port = parsed.port or (80 if parsed.scheme == "http" else 443)
            except ValueError as error:
                raise ConfigUiRequestError(403, "request rejected") from error
            if not (
                parsed.scheme == "http"
                and (parsed.hostname or "").casefold().rstrip(".")
                in {"127.0.0.1", "localhost"}
                and origin_port == self.port
                and parsed.username is None
                and parsed.password is None
                and parsed.path in {"", "/"}
                and not parsed.query
                and not parsed.fragment
            ):
                raise ConfigUiRequestError(403, "request rejected")
        authorization = _header(headers, "Authorization")
        expected = f"Bearer {self._token()}"
        if not hmac.compare_digest(authorization.encode(), expected.encode()):
            raise ConfigUiRequestError(
                401,
                "authentication required",
                {"WWW-Authenticate": "Bearer"},
            )

    def content_length(self, headers: Mapping[str, str]) -> int:
        if _header(headers, "Transfer-Encoding"):
            raise ConfigUiRequestError(400, "streaming request bodies are not allowed")
        content_type = _header(headers, "Content-Type").split(";", 1)[0].strip()
        if content_type != "application/json":
            raise ConfigUiRequestError(415, "application/json required")
        try:
            length = int(_header(headers, "Content-Length"))
        except ValueError as error:
            raise ConfigUiRequestError(411, "bounded Content-Length required") from error
        if not 1 <= length <= self.max_body_bytes:
            raise ConfigUiRequestError(413, "request exceeds limit")
        return length

    def _token(self) -> str:
        token = self.environment.get(self.token_env, "")
        if (
            not 24 <= len(token) <= 512
            or token.strip() != token
            or any(ord(character) < 0x21 or ord(character) > 0x7E for character in token)
        ):
            raise ConfigUiError(
                "settings UI token must be 24-512 printable ASCII characters"
            )
        return token


class LoopbackConfigUiServer:
    """Serve one credential-free settings document on explicit IPv4 loopback."""

    def __init__(
        self,
        controller: SettingsController,
        *,
        port: int,
        token_env: str,
        environment: Mapping[str, str] | None = None,
        max_body_bytes: int = _MAX_BODY_BYTES,
        max_concurrent_requests: int = 4,
        request_timeout_seconds: float = 10.0,
    ) -> None:
        if not 0 <= port <= 65_535:
            raise ConfigUiError("settings UI port is invalid")
        if not 1 <= max_concurrent_requests <= 16:
            raise ConfigUiError("settings UI concurrency limit is invalid")
        if not 1.0 <= request_timeout_seconds <= 30.0:
            raise ConfigUiError("settings UI request timeout is invalid")
        self.controller = controller
        self.request_timeout_seconds = request_timeout_seconds
        self._capacity = threading.BoundedSemaphore(max_concurrent_requests)
        self._server = ThreadingHTTPServer(("127.0.0.1", port), self._handler_type())
        self._server.daemon_threads = True
        self._policy = ConfigUiPolicy(
            port=int(self._server.server_address[1]),
            token_env=token_env,
            environment=environment,
            max_body_bytes=max_body_bytes,
        )
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("settings UI is already started")
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="xdsh-config-ui",
            daemon=True,
        )
        self._thread.start()

    def serve_forever(self) -> None:
        self._server.serve_forever()

    def close(self) -> None:
        if self._thread is not None:
            self._server.shutdown()
            self._thread.join(timeout=5.0)
            self._thread = None
        self._server.server_close()

    def shutdown(self) -> None:
        """Stop a foreground ``serve_forever`` call from another thread."""

        self._server.shutdown()

    def _handler_type(self) -> type[BaseHTTPRequestHandler]:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "XDSH-Config/1"
            sys_version = ""

            def setup(self) -> None:
                super().setup()
                self.connection.settimeout(owner.request_timeout_seconds)

            def do_GET(self) -> None:
                owner._get(self)

            def do_PUT(self) -> None:
                owner._put(self)

            def do_POST(self) -> None:
                owner._json(self, 405, {"error": "method not allowed"})

            def do_DELETE(self) -> None:
                owner._json(self, 405, {"error": "method not allowed"})

            def log_message(self, format: str, *args: object) -> None:
                del format, args

        return Handler

    def _get(self, handler: BaseHTTPRequestHandler) -> None:
        target = _target(handler.path)
        if target == "/":
            self._bytes(handler, 200, _INDEX_HTML, "text/html; charset=utf-8")
            return
        if target == "/app.js":
            self._bytes(handler, 200, _APP_JS, "text/javascript; charset=utf-8")
            return
        if target == "/style.css":
            self._bytes(handler, 200, _STYLE_CSS, "text/css; charset=utf-8")
            return
        if target == "/health":
            self._json(handler, 200, {"status": "ok", "bind": "127.0.0.1"})
            return
        if target != "/api/settings":
            self._json(handler, 404, {"error": "not found"})
            return
        if not self._authorize(handler):
            return
        try:
            snapshot = self.controller.snapshot()
        except (SettingsError, OSError):
            self._json(handler, 500, {"error": "settings unavailable"})
            return
        self._json(
            handler,
            200,
            _snapshot_document(snapshot),
            {"X-XDSH-Revision": snapshot.revision},
        )

    def _put(self, handler: BaseHTTPRequestHandler) -> None:
        if _target(handler.path) != "/api/settings":
            self._json(handler, 404, {"error": "not found"})
            return
        if not self._authorize(handler):
            return
        if not self._capacity.acquire(blocking=False):
            self._json(handler, 429, {"error": "settings UI is busy"})
            return
        try:
            headers = _headers(handler)
            try:
                length = self._policy.content_length(headers)
                expected = _header(headers, "X-XDSH-Revision")
                if re.fullmatch(r"[0-9a-f]{64}", expected) is None:
                    raise ConfigUiRequestError(428, "settings revision required")
                body = handler.rfile.read(length)
                if len(body) != length:
                    raise ConfigUiRequestError(400, "request body is invalid")
                document = _decode_json(body)
                settings = settings_from_document(document)
                snapshot = self.controller.apply(
                    ReplaceSettings(settings),
                    expected_revision=expected,
                )
            except ConfigUiRequestError as error:
                self._json(handler, error.status, {"error": error.message}, error.headers)
                return
            except SettingsError as error:
                status = 409 if "reload before saving" in str(error) else 422
                self._json(handler, status, {"error": safe_exception_text(error)})
                return
            except (OSError, UnicodeError, ValueError):
                self._json(handler, 422, {"error": "settings document is invalid"})
                return
            self._json(
                handler,
                200,
                _snapshot_document(snapshot),
                {"X-XDSH-Revision": snapshot.revision},
            )
        finally:
            self._capacity.release()

    def _authorize(self, handler: BaseHTTPRequestHandler) -> bool:
        security_names = (
            "Host",
            "Authorization",
            "Origin",
            "Content-Type",
            "Content-Length",
            "X-XDSH-Revision",
            "Transfer-Encoding",
        )
        if any(len(handler.headers.get_all(name) or []) > 1 for name in security_names):
            self._json(handler, 400, {"error": "duplicate security header"})
            return False
        try:
            self._policy.authorize(
                host=handler.headers.get("Host", ""),
                origin=handler.headers.get("Origin"),
                headers=_headers(handler),
            )
        except ConfigUiRequestError as error:
            self._json(handler, error.status, {"error": error.message}, error.headers)
            return False
        return True

    def _json(
        self,
        handler: BaseHTTPRequestHandler,
        status: int,
        document: Mapping[str, object],
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        payload = json.dumps(document, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self._bytes(handler, status, payload, "application/json; charset=utf-8", extra_headers)

    def _bytes(
        self,
        handler: BaseHTTPRequestHandler,
        status: int,
        payload: bytes,
        content_type: str,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(payload)))
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.send_header("X-Frame-Options", "DENY")
        handler.send_header("Referrer-Policy", "no-referrer")
        handler.send_header("Cross-Origin-Resource-Policy", "same-origin")
        handler.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
        )
        for name, value in (extra_headers or {}).items():
            handler.send_header(name, value)
        handler.end_headers()
        handler.wfile.write(payload)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    path = Path(args.settings).expanduser()
    try:
        controller = SettingsController(JsonSettingsStore(path))
        if args.command == "tui":
            from xdsh.core.providers import ProviderRegistry
            from xdsh.ops.providers import run_wizard

            run_wizard(ProviderRegistry(Path(os.environ.get(
                "XDSH_PROVIDERS_ROOT", str(path.parent / "providers"),
            )).expanduser()))
            return 0
        if args.command == "show":
            _print_snapshot(controller.snapshot())
            return 0
        if args.command == "validate":
            snapshot = controller.snapshot()
            print(f"settings valid · revision {snapshot.revision}")
            return 0
        if args.command == "serve":
            server = LoopbackConfigUiServer(
                controller,
                port=args.port,
                token_env=args.token_env,
            )
            _install_signal_close(server)
            print(f"XDSH settings UI: http://127.0.0.1:{server.port}/")
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.close()
            return 0
        action = _action(args)
        current = controller.snapshot()
        updated = controller.apply(
            action,
            expected_revision=args.expect or current.revision,
        )
        _print_snapshot(updated)
        return 0
    except (ConfigUiError, SettingsError, OSError, ValueError) as error:
        print(
            f"xdsh-config: {safe_exception_text(error, max_chars=300)}",
            file=sys.stderr,
        )
        return 2


def _action(args: argparse.Namespace) -> SettingsAction:
    if args.command == "set-default":
        return SetDefaultModel(None if args.clear else cast(str, args.model))
    if args.command == "provider-set":
        return UpsertProvider(ProviderSettings(args.id, args.label, args.icon))
    if args.command == "provider-remove":
        return RemoveProvider(args.id)
    if args.command == "model-set":
        return UpsertModel(
            ModelSettings(
                id=args.id,
                provider=args.provider,
                reasoning=_tri_bool(args.reasoning),
                context_window_tokens=args.context_window,
                max_output_tokens=args.max_output,
                thinking=_tri_bool(args.thinking),
                tools=_tri_bool(args.tools),
                vision=_tri_bool(args.vision),
                audio=_tri_bool(args.audio),
                structured_output=_tri_bool(args.structured_output),
            )
        )
    if args.command == "model-remove":
        return RemoveModel(args.id)
    raise ConfigUiError("settings action is unavailable")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="xdsh-config")
    parser.add_argument(
        "--settings",
        default=os.environ.get("XDSH_SETTINGS_PATH", str(_DEFAULT_PATH)),
        help="credential-free settings JSON path",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("show")
    subparsers.add_parser("validate")
    subparsers.add_parser("tui", help="interactive model API/credential setup")

    default = subparsers.add_parser("set-default")
    default_group = default.add_mutually_exclusive_group(required=True)
    default_group.add_argument("--model")
    default_group.add_argument("--clear", action="store_true")
    _expect_argument(default)

    provider_set = subparsers.add_parser("provider-set")
    provider_set.add_argument("id")
    provider_set.add_argument("--label")
    provider_set.add_argument("--icon")
    _expect_argument(provider_set)

    provider_remove = subparsers.add_parser("provider-remove")
    provider_remove.add_argument("id")
    _expect_argument(provider_remove)

    model_set = subparsers.add_parser("model-set")
    model_set.add_argument("id")
    model_set.add_argument("--provider")
    model_set.add_argument("--context-window", type=int)
    model_set.add_argument("--max-output", type=int)
    for name in ("reasoning", "thinking", "tools", "vision", "audio", "structured-output"):
        model_set.add_argument(f"--{name}", choices=_BOOL_CHOICES, default="auto")
    _expect_argument(model_set)

    model_remove = subparsers.add_parser("model-remove")
    model_remove.add_argument("id")
    _expect_argument(model_remove)

    serve = subparsers.add_parser("serve")
    serve.add_argument("--port", type=int, default=8877)
    serve.add_argument("--token-env", default="XDSH_CONFIG_UI_TOKEN")
    return parser


def _expect_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--expect", help="optional expected 64-character revision")


def _tri_bool(value: str) -> bool | None:
    return {"auto": None, "on": True, "off": False}[value]


def _print_snapshot(snapshot: SettingsSnapshot) -> None:
    print(
        json.dumps(
            {
                "schema_version": 1,
                "revision": snapshot.revision,
                "settings": snapshot.document,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


def _snapshot_document(snapshot: SettingsSnapshot) -> dict[str, object]:
    return {
        "schema_version": 1,
        "revision": snapshot.revision,
        "settings": snapshot.document,
    }


def _decode_json(payload: bytes) -> object:
    if len(payload) > _MAX_BODY_BYTES:
        raise ConfigUiRequestError(413, "request exceeds limit")
    try:
        return json.loads(
            payload,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON value: {value}")
            ),
            object_pairs_hook=_unique_pairs,
        )
    except (UnicodeDecodeError, ValueError) as error:
        raise ConfigUiRequestError(422, "settings document is invalid") from error


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError("duplicate JSON key")
        document[key] = value
    return document


def _target(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        return ""
    return parsed.path


def _headers(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    return {str(key): str(value) for key, value in handler.headers.items()}


def _header(headers: Mapping[str, str], name: str) -> str:
    wanted = name.casefold()
    return next((value for key, value in headers.items() if key.casefold() == wanted), "")


def _install_signal_close(server: LoopbackConfigUiServer) -> None:
    def close(_signum: int, _frame: object) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(signum, close)
        except (ValueError, OSError):
            pass


_INDEX_HTML = """<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>XDSH Settings</title>
  <link rel="stylesheet" href="/style.css">
</head>
<body><main>
  <h1>XDSH Settings</h1>
  <p class="muted">只管理不含憑據的模型與 Provider 設定。</p>
  <section id="login">
    <label>Access token
      <input id="token" type="password" autocomplete="off">
    </label>
    <button id="connect">連線</button>
  </section>
  <p id="status" role="status"></p>
  <section id="editor" hidden>
    <label>預設模型
      <input id="defaultModel" placeholder="your-exact-model-id">
    </label>
    <div class="heading"><h2>Providers</h2><button id="addProvider">新增</button></div>
    <div id="providers"></div>
    <div class="heading"><h2>Models</h2><button id="addModel">新增</button></div>
    <div id="models"></div>
    <div class="actions">
      <button id="reload">重新載入</button>
      <button id="save" class="primary">儲存</button>
    </div>
  </section>
</main><script src="/app.js"></script></body>
</html>
""".encode()

_STYLE_CSS = b"""
:root { color-scheme: light dark; font-family: system-ui, sans-serif; }
body { margin: 0; background: #10131a; color: #eef2ff; }
main { max-width: 1100px; margin: 2rem auto; padding: 0 1rem; }
h1, h2 { margin: .4rem 0; }
.muted { color: #aab4c8; }
.heading, .actions {
  display: flex; align-items: center; justify-content: space-between; margin-top: 1.4rem;
}
.card {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: .6rem;
  background: #1b2230; border: 1px solid #33415c; border-radius: 10px;
  padding: .8rem; margin: .6rem 0;
}
label { display: grid; gap: .3rem; }
input, select, button {
  font: inherit; padding: .55rem; border-radius: 7px; border: 1px solid #52617c;
  background: #121824; color: inherit;
}
button { cursor: pointer; }
.primary { background: #3267e3; border-color: #5d87ec; }
.remove { background: #5e2530; }
.actions { justify-content: flex-end; gap: .6rem; }
#status { min-height: 1.4rem; }
.error { color: #ff9aa9; }
.ok { color: #83e6a2; }
@media (max-width: 600px) {
  main { margin: 1rem auto; }
  .card { grid-template-columns: 1fr; }
}
"""

_APP_JS = """
'use strict';
let token = '';
let revision = '';
let settings = {schema_version: 1, providers: [], models: []};
const $ = id => document.getElementById(id);
function status(text, ok = false) {
  $('status').textContent = text;
  $('status').className = ok ? 'ok' : 'error';
}
function input(label, value = '', type = 'text') {
  const wrapper = document.createElement('label');
  wrapper.textContent = label;
  const field = document.createElement('input');
  field.type = type;
  field.value = value ?? '';
  wrapper.append(field);
  return [wrapper, field];
}
function choice(label, value) {
  const wrapper = document.createElement('label');
  wrapper.textContent = label;
  const select = document.createElement('select');
  for (const [optionValue, text] of [['', 'Auto'], ['true', 'On'], ['false', 'Off']]) {
    const option = document.createElement('option');
    option.value = optionValue;
    option.textContent = text;
    if (String(value ?? '') === optionValue) option.selected = true;
    select.append(option);
  }
  wrapper.append(select);
  return [wrapper, select];
}
function removeButton(card) {
  const button = document.createElement('button');
  button.textContent = '移除';
  button.className = 'remove';
  button.onclick = () => card.remove();
  return button;
}
function providerRow(provider = {}) {
  const card = document.createElement('div');
  card.className = 'card';
  const [idLabel, id] = input('ID', provider.id);
  const [iconLabel, icon] = input('圖標', provider.icon);
  const [nameLabel, name] = input('名稱', provider.label);
  card.append(idLabel, iconLabel, nameLabel, removeButton(card));
  card.read = () => ({
    id: id.value.trim(),
    ...(icon.value.trim() ? {icon: icon.value.trim()} : {}),
    ...(name.value.trim() ? {label: name.value.trim()} : {}),
  });
  $('providers').append(card);
}
function modelRow(model = {}) {
  const card = document.createElement('div');
  card.className = 'card';
  const [idLabel, id] = input('Model ID', model.id);
  const [providerLabel, provider] = input('Provider', model.provider);
  const [contextLabel, context] = input('Context', model.context_window_tokens, 'number');
  const [outputLabel, output] = input('Max output', model.max_output_tokens, 'number');
  card.append(idLabel, providerLabel, contextLabel, outputLabel);
  const capabilities = {};
  for (const name of [
    'reasoning', 'thinking', 'tools', 'vision', 'audio', 'structured_output'
  ]) {
    const [label, select] = choice(name, model[name]);
    capabilities[name] = select;
    card.append(label);
  }
  card.append(removeButton(card));
  card.read = () => {
    const result = {id: id.value.trim()};
    if (provider.value.trim()) result.provider = provider.value.trim();
    if (context.value) result.context_window_tokens = Number(context.value);
    if (output.value) result.max_output_tokens = Number(output.value);
    for (const [name, select] of Object.entries(capabilities)) {
      if (select.value) result[name] = select.value === 'true';
    }
    return result;
  };
  $('models').append(card);
}
function render() {
  $('providers').replaceChildren();
  $('models').replaceChildren();
  $('defaultModel').value = settings.default_model ?? '';
  (settings.providers ?? []).forEach(providerRow);
  (settings.models ?? []).forEach(modelRow);
  $('editor').hidden = false;
}
async function load() {
  const response = await fetch('/api/settings', {
    headers: {Authorization: 'Bearer ' + token},
  });
  const document = await response.json();
  if (!response.ok) throw Error(document.error || '載入失敗');
  revision = document.revision;
  settings = document.settings;
  render();
  status('已載入 · ' + revision.slice(0, 12), true);
}
$('connect').onclick = async () => {
  token = $('token').value;
  try { await load(); } catch (error) { status(error.message); }
};
$('reload').onclick = async () => {
  try { await load(); } catch (error) { status(error.message); }
};
$('addProvider').onclick = () => providerRow();
$('addModel').onclick = () => modelRow();
$('save').onclick = async () => {
  const next = {
    schema_version: 1,
    providers: [...$('providers').children].map(row => row.read()),
    models: [...$('models').children].map(row => row.read()),
  };
  const defaultModel = $('defaultModel').value.trim();
  if (defaultModel) next.default_model = defaultModel;
  try {
    const response = await fetch('/api/settings', {
      method: 'PUT',
      headers: {
        Authorization: 'Bearer ' + token,
        'Content-Type': 'application/json',
        'X-XDSH-Revision': revision,
      },
      body: JSON.stringify(next),
    });
    const document = await response.json();
    if (!response.ok) throw Error(document.error || '儲存失敗');
    revision = document.revision;
    settings = document.settings;
    render();
    status('已安全儲存 · ' + revision.slice(0, 12), true);
  } catch (error) {
    status(error.message);
  }
};
""".encode()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
