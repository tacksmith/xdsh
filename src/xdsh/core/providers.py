"""Executable provider connections, separate from presentation-only settings.

Only an operator's confirmed connection can create a route. Public records
contain environment references, never keys; secrets live in a separate 0600
environment file. Neither registration nor the wizard invokes an agent.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import ipaddress
import json
import os
import re
import socket
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from xdsh.core.settings import validate_settings_path

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}$")
_ENV = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_PROVIDER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MAX_BYTES = 256 * 1024
ENGINES = ("anthropic", "openai", "gemini", "azure")


class ProviderError(ValueError):
    """A safe, content-free error suitable for operator interfaces."""


class ProviderDnsError(ProviderError):
    """Local egress policy/configuration failure, not an upstream HTTP 400."""


def validate_endpoint(value: str) -> str:
    """Accept credential-free HTTPS or explicitly selected IP loopback only."""
    if not isinstance(value, str) or not 8 <= len(value) <= 512:
        raise ProviderError("Invalid provider URL")
    if any(ord(char) < 33 or ord(char) > 126 for char in value) or "\\" in value:
        raise ProviderError("Provider URL contains invalid characters")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ProviderError("Invalid provider URL") from error
    host = parsed.hostname or ""
    if (
        not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "%" in value
        or port == 0
    ):
        raise ProviderError("URL credentials, queries and fragments are not allowed")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        if not address.is_global and not address.is_loopback:
            raise ProviderError("Private and special-use provider addresses are not allowed")
    elif host.lower() == "localhost" or host.lower().endswith((".local", ".internal")):
        raise ProviderError("Use an explicit loopback IP for a local provider")
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and address is not None and address.is_loopback
    ):
        raise ProviderError("Remote providers require HTTPS")
    return value.rstrip("/")


def check_endpoint_dns(value: str) -> None:
    """Reject public names resolving to non-public addresses before key use.

    These are trusted operator-created endpoints, not model-controlled URLs.
    The HTTP client must separately refuse redirects.
    """
    parsed = urlsplit(validate_endpoint(value))
    host = parsed.hostname or ""
    try:
        ipaddress.ip_address(host)
        return
    except ValueError:
        pass
    try:
        answers = socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
    except OSError as error:
        raise ProviderDnsError("Provider DNS lookup failed") from error
    # Opt-in for an operator-controlled TUN proxy. Never accept arbitrary
    # private IPs, wildcards, model-provided flags, or a direct fake-IP URL.
    # HTTPS certificate verification and redirect refusal remain mandatory.
    fake_ip_hosts = {
        name.strip().lower() for name in
        os.environ.get("XDSH_PROVIDER_FAKE_IP_HOSTS", "").split(",") if name.strip()
    }
    allow_fake_ip = host.lower() in fake_ip_hosts
    fake_ip_range = ipaddress.ip_network("198.18.0.0/15")
    addresses = [ipaddress.ip_address(answer[4][0]) for answer in answers]
    if not addresses or any(
        not address.is_global and not (allow_fake_ip and address in fake_ip_range)
        for address in addresses
    ):
        raise ProviderDnsError("Provider DNS must resolve only to public addresses")


@dataclass(frozen=True, slots=True)
class ProviderConnection:
    model: str
    provider: str
    engine: str
    base_url: str
    credential_env: str
    api_version: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or _ID.fullmatch(self.model) is None:
            raise ProviderError("Invalid exact model ID; wildcards are not allowed")
        if not isinstance(self.provider, str) or _PROVIDER.fullmatch(self.provider) is None:
            raise ProviderError("Invalid provider ID")
        if self.engine not in ENGINES:
            raise ProviderError("Unsupported protocol; use anthropic, openai, gemini or azure")
        validate_endpoint(self.base_url)
        if not isinstance(self.credential_env, str) or _ENV.fullmatch(self.credential_env) is None:
            raise ProviderError("Use an environment variable name, not a key")
        if any(part in self.credential_env for part in ("TELEGRAM", "BOT_TOKEN", "ADMIN")):
            raise ProviderError("Bot and administrative credentials are not provider keys")
        if self.api_version is not None and (
            not isinstance(self.api_version, str)
            or re.fullmatch(r"[A-Za-z0-9.-]{1,64}", self.api_version) is None
        ):
            raise ProviderError("Invalid API version")
        if self.engine == "azure" and self.api_version is None:
            raise ProviderError("Azure requires an explicit API version")

    @property
    def route_id(self) -> str:
        encoded = json.dumps(asdict(self), sort_keys=True).encode()
        return hashlib.sha256(encoded).hexdigest()[:32]

    def summary(self) -> str:
        return (
            f"Provider: {self.provider}\nProtocol: {self.engine}\nURL: {self.base_url}\n"
            f"Model: {self.model}\nCredential: env:{self.credential_env}"
        )


class ProviderRegistry:
    """Cross-process, atomic, bounded exact-route registry for all interfaces."""

    def __init__(self, root: Path) -> None:
        if root.expanduser().is_symlink():
            raise ProviderError("Provider directory must not be a symlink")
        self.root = validate_settings_path(root / "connections.json").parent
        self.path = self.root / "connections.json"
        self.secrets_path = self.root / "provider-secrets.env"

    def connections(self) -> tuple[ProviderConnection, ...]:
        raw = self._read(self.path)
        if not raw:
            return ()
        try:
            document = json.loads(raw)
            if not isinstance(document, dict) or set(document) != {"schema_version", "routes"}:
                raise ValueError
            routes = document["routes"]
            if document["schema_version"] != 1 or not isinstance(routes, list) or len(routes) > 128:
                raise ValueError
            result = tuple(ProviderConnection(**route) for route in routes)
            if len({route.model for route in result}) != len(result):
                raise ValueError
            return result
        except (TypeError, ValueError) as error:
            raise ProviderError("Provider registry has an invalid schema") from error

    def credential(
        self,
        route: ProviderConnection,
        environment: Mapping[str, str] | None = None,
    ) -> str:
        environment = os.environ if environment is None else environment
        value = environment.get(route.credential_env, "")
        if not value:
            value = self._secrets().get(route.credential_env, "")
        if not value:
            raise ProviderError("Provider credential reference is not configured")
        _validate_secret(value)
        return value

    def add(
        self,
        route: ProviderConnection,
        *,
        approved: bool = False,
        credential: str | None = None,
    ) -> None:
        if not approved:
            raise ProviderError("Explicit confirmation is required before provider registration")
        if credential is not None:
            _validate_secret(credential)
        with self._lock():
            routes = self.connections()
            if any(item.model == route.model for item in routes):
                raise ProviderError(
                    "This model ID is already registered; no existing route changed"
                )
            if len(routes) >= 128:
                raise ProviderError("Provider route limit reached")
            if credential is not None:
                values = self._secrets()
                if route.credential_env in values and values[route.credential_env] != credential:
                    raise ProviderError("Credential reference already exists; use a new reference")
                values[route.credential_env] = credential
                self._write(
                    self.secrets_path,
                    "".join(f"{key}={value}\n" for key, value in sorted(values.items())).encode(),
                )
            self._write(
                self.path,
                (
                    json.dumps(
                        {
                            "schema_version": 1,
                            "routes": [asdict(item) for item in (*routes, route)],
                        },
                        indent=2,
                    )
                    + "\n"
                ).encode(),
            )

    def _secrets(self) -> dict[str, str]:
        try:
            lines = self._read(self.secrets_path).decode("utf-8").splitlines()
        except UnicodeError as error:
            raise ProviderError("Invalid provider credential file") from error
        result: dict[str, str] = {}
        for line in lines:
            if not line or line.startswith("#"):
                continue
            key, separator, value = line.partition("=")
            if not separator or _ENV.fullmatch(key) is None or key in result:
                raise ProviderError("Invalid provider credential file")
            _validate_secret(value)
            result[key] = value
        return result

    @contextlib.contextmanager
    def _lock(self) -> Iterator[None]:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.is_symlink():
            raise ProviderError("Provider directory must not be a symlink")
        descriptor = os.open(
            self.root / ".connections.lock",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def _read(self, path: Path) -> bytes:
        if self.root.is_symlink() or path.is_symlink():
            raise ProviderError("Provider files must not be symlinks")
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return b""
        with os.fdopen(descriptor, "rb") as handle:
            if os.fstat(handle.fileno()).st_mode & 0o077:
                raise ProviderError("Provider files must have owner-only permissions")
            raw = handle.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            raise ProviderError("Provider file exceeds its size limit")
        return raw

    def _write(self, path: Path, payload: bytes) -> None:
        if len(payload) > _MAX_BYTES:
            raise ProviderError("Provider file exceeds its size limit")
        temporary = self.root / f".{uuid.uuid4().hex}.tmp"
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def _validate_secret(value: str) -> None:
    if not 1 <= len(value) <= 4096 or any(ord(char) < 33 or ord(char) > 126 for char in value):
        raise ProviderError("Credential must be a single printable value (maximum 4096 characters)")


@dataclass(slots=True)
class ProviderWizard:
    """Transport-independent, volatile setup; callers own expiry/authorization."""

    step: int = 0
    answers: list[str] = field(default_factory=list, repr=False)
    secret: str | None = field(default=None, repr=False)

    @property
    def prompt(self) -> str:
        return (
            "Provider ID (e.g. volcengine, deepseek, local):",
            "Protocol: anthropic / openai / gemini (Azure: azure:API_VERSION)",
            "Base URL (HTTPS, or an explicit loopback IP for local models):",
            "Exact upstream model ID (no wildcard):",
            "Credential: env:VARIABLE_NAME, or enter an API key. It will not be echoed.",
            "Confirm saving this connection and sending requests to this endpoint?",
        )[self.step]

    def advance(self, value: str) -> ProviderConnection | None:
        value = value.strip()
        if self.step == 0 and _PROVIDER.fullmatch(value) is None:
            raise ProviderError("Invalid provider ID")
        if self.step == 1:
            engine, _, version = value.partition(":")
            if engine not in ENGINES or (engine == "azure" and not version):
                raise ProviderError("Use anthropic, openai, gemini or azure:API_VERSION")
        if self.step == 2:
            value = validate_endpoint(value)
        if self.step == 3 and _ID.fullmatch(value) is None:
            raise ProviderError("Invalid exact model ID")
        if self.step == 4:
            if value.startswith("env:"):
                self.secret = None
                value = value.removeprefix("env:")
                if _ENV.fullmatch(value) is None:
                    raise ProviderError("Invalid credential environment reference")
            else:
                _validate_secret(value)
                self.secret = value
                value = f"XDSH_PROVIDER_{uuid.uuid4().hex[:16].upper()}_KEY"
        if self.step >= 5:
            raise ProviderError("Use the explicit confirmation action")
        if self.step == 4:
            provider, protocol, base_url, model = self.answers
            engine, _, version = protocol.partition(":")
            ProviderConnection(model, provider, engine, base_url, value, version or None)
        self.answers.append(value)
        self.step += 1
        return self.connection() if self.step == 5 else None

    def connection(self) -> ProviderConnection:
        if self.step != 5:
            raise ProviderError("Provider setup is incomplete")
        provider, protocol, base_url, model, credential_env = self.answers
        engine, _, version = protocol.partition(":")
        return ProviderConnection(
            model,
            provider,
            engine,
            base_url,
            credential_env,
            version or None,
        )
