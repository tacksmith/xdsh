"""Validated construction of optional authenticated external clients.

The process starts with all external providers disabled.  Enabling one requires
an explicit host allowlist and, for gateway-backed MCP/browser access, an
environment credential reference.  This keeps deployment configuration
declarative while ensuring secret values are read only at request time.
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast

from xdsh.core.external import (
    AuditSink,
    AuthenticatedBrowserClient,
    AuthenticatedMcpClient,
    CredentialRef,
    NetworkPolicy,
    SafeWebFetcher,
    UrlPolicy,
)
from xdsh.core.public_web import PublicWebClient


class ExternalConfigurationError(ValueError):
    """An optional external capability was only partially configured."""


@dataclass(frozen=True, slots=True)
class ExternalRuntimeConfig:
    """Validated, secret-free external client settings."""

    web_hosts: tuple[str, ...] = ()
    public_web_enabled: bool = False
    public_web_proxy: str | None = None
    web_ports: tuple[int, ...] = (443,)
    web_credential: CredentialRef | None = None
    web_max_bytes: int = 2_000_000
    web_max_decompressed_bytes: int = 4_000_000
    web_timeout_seconds: float = 15.0
    mcp_endpoint: str | None = None
    mcp_hosts: tuple[str, ...] = ()
    mcp_ports: tuple[int, ...] = (443,)
    mcp_credential: CredentialRef | None = None
    mcp_allowed_tools: tuple[str, ...] = ()
    browser_endpoint: str | None = None
    browser_endpoint_hosts: tuple[str, ...] = ()
    browser_endpoint_ports: tuple[int, ...] = (443,)
    browser_target_hosts: tuple[str, ...] = ()
    browser_target_ports: tuple[int, ...] = (443,)
    browser_credential: CredentialRef | None = None


@dataclass(slots=True)
class ExternalClients:
    """Optional product-level clients; ``None`` means capability is disabled."""

    web: SafeWebFetcher | None = None
    public_web: PublicWebClient | None = None
    web_credential: CredentialRef | None = None
    mcp: AuthenticatedMcpClient | None = None
    browser: AuthenticatedBrowserClient | None = None


def external_config_from_env(
    environment: Mapping[str, str] | None = None,
) -> ExternalRuntimeConfig:
    """Parse optional external settings without ever materializing secrets."""

    env = os.environ if environment is None else environment
    public_reader = env.get("XDSH_PUBLIC_WEB_ENABLED", "0").strip()
    if public_reader not in {"0", "1"}:
        raise ExternalConfigurationError("XDSH_PUBLIC_WEB_ENABLED must be 0 or 1")
    public_mode = env.get("XDSH_WEB_PUBLIC", "0").strip()
    if public_mode not in {"0", "1"}:
        raise ExternalConfigurationError("XDSH_WEB_PUBLIC must be 0 or 1")
    try:
        web_hosts = _csv(env.get("XDSH_WEB_ALLOWED_HOSTS", ""))
        web_ports = _ports(env.get("XDSH_WEB_ALLOWED_PORTS", "443"))
        web_credential = _credential(env, "XDSH_WEB_CREDENTIAL_ENV")
        mcp_endpoint = _optional(env, "XDSH_MCP_ENDPOINT")
        mcp_hosts = _csv(env.get("XDSH_MCP_ALLOWED_HOSTS", ""))
        mcp_ports = _ports(env.get("XDSH_MCP_ALLOWED_PORTS", "443"))
        mcp_credential = _credential(env, "XDSH_MCP_CREDENTIAL_ENV")
        mcp_tools = _csv(env.get("XDSH_MCP_ALLOWED_TOOLS", ""))
        browser_endpoint = _optional(env, "XDSH_BROWSER_ENDPOINT")
        browser_endpoint_hosts = _csv(env.get("XDSH_BROWSER_ENDPOINT_ALLOWED_HOSTS", ""))
        browser_endpoint_ports = _ports(env.get("XDSH_BROWSER_ENDPOINT_ALLOWED_PORTS", "443"))
        browser_target_hosts = _csv(env.get("XDSH_BROWSER_ALLOWED_HOSTS", ""))
        browser_target_ports = _ports(env.get("XDSH_BROWSER_ALLOWED_PORTS", "443"))
        browser_credential = _credential(env, "XDSH_BROWSER_CREDENTIAL_ENV")
        max_bytes = _bounded_int(env, "XDSH_WEB_MAX_BYTES", 2_000_000, 1, 20_000_000)
        max_decompressed = _bounded_int(
            env,
            "XDSH_WEB_MAX_DECOMPRESSED_BYTES",
            4_000_000,
            max_bytes,
            40_000_000,
        )
        timeout = _bounded_float(env, "XDSH_WEB_TIMEOUT_SECONDS", 15.0, 0.1, 120.0)
    except (ValueError, ExternalConfigurationError) as error:
        if isinstance(error, ExternalConfigurationError):
            raise
        raise ExternalConfigurationError(str(error)) from error

    web_configured = bool(
        public_mode == "1" or web_hosts
        or web_credential is not None
        or _any_present(
            env,
            "XDSH_WEB_ALLOWED_PORTS",
            "XDSH_WEB_CREDENTIAL_HEADER",
            "XDSH_WEB_CREDENTIAL_SCHEME",
            "XDSH_WEB_MAX_BYTES",
            "XDSH_WEB_MAX_DECOMPRESSED_BYTES",
            "XDSH_WEB_TIMEOUT_SECONDS",
        )
    )
    if public_mode == "1" and (web_credential is not None or _any_present(
        env, "XDSH_WEB_CREDENTIAL_HEADER", "XDSH_WEB_CREDENTIAL_SCHEME",
    )):
        raise ExternalConfigurationError("Public Web must not include credential configuration")
    if web_configured and (not web_hosts or (web_credential is None and public_mode != "1")):
        raise ExternalConfigurationError(
            "Web requires an explicit host allowlist and credentials or explicit public mode"
        )
    mcp_configured = bool(
        mcp_endpoint or mcp_hosts or mcp_tools or mcp_credential is not None
    ) or _any_present(
        env,
        "XDSH_MCP_CREDENTIAL_HEADER",
        "XDSH_MCP_CREDENTIAL_SCHEME",
        "XDSH_MCP_ALLOWED_PORTS",
    )
    if mcp_configured and (
        not mcp_endpoint or not mcp_hosts or not mcp_tools or mcp_credential is None
    ):
        raise ExternalConfigurationError(
            "MCP requires endpoint, explicit host/tool allowlists, and credential env reference"
        )
    browser_configured = bool(
        browser_endpoint
        or browser_endpoint_hosts
        or browser_target_hosts
        or browser_credential is not None
    ) or _any_present(
        env,
        "XDSH_BROWSER_CREDENTIAL_HEADER",
        "XDSH_BROWSER_CREDENTIAL_SCHEME",
        "XDSH_BROWSER_ENDPOINT_ALLOWED_PORTS",
        "XDSH_BROWSER_ALLOWED_PORTS",
    )
    if browser_configured and (
        not browser_endpoint
        or not browser_endpoint_hosts
        or not browser_target_hosts
        or browser_credential is None
    ):
        raise ExternalConfigurationError(
            "browser requires endpoint/target host allowlists and credential env reference"
        )
    return ExternalRuntimeConfig(
        public_web_enabled=public_reader == "1",
        public_web_proxy=env.get("XDSH_PUBLIC_WEB_PROXY_URL") or None,
        web_hosts=web_hosts,
        web_ports=web_ports,
        web_credential=web_credential,
        web_max_bytes=max_bytes,
        web_max_decompressed_bytes=max_decompressed,
        web_timeout_seconds=timeout,
        mcp_endpoint=mcp_endpoint,
        mcp_hosts=mcp_hosts,
        mcp_ports=mcp_ports,
        mcp_credential=mcp_credential,
        mcp_allowed_tools=mcp_tools,
        browser_endpoint=browser_endpoint,
        browser_endpoint_hosts=browser_endpoint_hosts,
        browser_endpoint_ports=browser_endpoint_ports,
        browser_target_hosts=browser_target_hosts,
        browser_target_ports=browser_target_ports,
        browser_credential=browser_credential,
    )


def build_external_clients(
    config: ExternalRuntimeConfig,
    *,
    audit: AuditSink | None = None,
    environment: Mapping[str, str] | None = None,
) -> ExternalClients:
    """Build only explicitly configured clients; disabled seams stay inert."""

    clients = ExternalClients()
    if config.public_web_enabled:
        clients.public_web = PublicWebClient(proxy=config.public_web_proxy, audit=audit)
    if config.web_hosts:
        web_network_policy = NetworkPolicy(
            allowed_hosts=config.web_hosts,
            allowed_ports=config.web_ports,
            max_response_bytes=config.web_max_bytes,
            max_decompressed_bytes=config.web_max_decompressed_bytes,
            timeout_seconds=config.web_timeout_seconds,
        )
        clients.web = SafeWebFetcher(
            UrlPolicy(web_network_policy),
            audit=audit,
            environment=environment,
        )
        clients.web_credential = config.web_credential
    if config.mcp_endpoint is not None:
        mcp_url_policy = UrlPolicy(
            NetworkPolicy(
                allowed_hosts=config.mcp_hosts,
                allowed_ports=config.mcp_ports,
                allow_loopback=_allows_loopback(config.mcp_hosts),
            )
        )
        clients.mcp = AuthenticatedMcpClient(
            config.mcp_endpoint,
            policy=mcp_url_policy,
            credential=config.mcp_credential,
            allowed_tools=config.mcp_allowed_tools,
            audit=audit,
            environment=environment,
        )
    if config.browser_endpoint is not None:
        endpoint_policy = UrlPolicy(
            NetworkPolicy(
                allowed_hosts=config.browser_endpoint_hosts,
                allowed_ports=config.browser_endpoint_ports,
                allow_loopback=_allows_loopback(config.browser_endpoint_hosts),
            )
        )
        target_policy = UrlPolicy(
            NetworkPolicy(
                allowed_hosts=config.browser_target_hosts,
                allowed_ports=config.browser_target_ports,
            )
        )
        clients.browser = AuthenticatedBrowserClient(
            config.browser_endpoint,
            endpoint_policy=endpoint_policy,
            target_policy=target_policy,
            credential=config.browser_credential,
            audit=audit,
            environment=environment,
        )
    return clients


def _optional(environment: Mapping[str, str], name: str) -> str | None:
    value = environment.get(name, "").strip()
    return value or None


def _csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _ports(value: str) -> tuple[int, ...]:
    values = tuple(int(item.strip()) for item in value.split(",") if item.strip())
    if not values or any(not 1 <= item <= 65_535 for item in values):
        raise ExternalConfigurationError("external port allowlist is invalid")
    if len(values) != len(set(values)):
        raise ExternalConfigurationError("external port allowlist contains duplicates")
    return values


def _credential(environment: Mapping[str, str], name: str) -> CredentialRef | None:
    env_name = _optional(environment, name)
    if env_name is None:
        return None
    header = environment.get(name.replace("_ENV", "_HEADER"), "Authorization").strip()
    scheme_value = environment.get(name.replace("_ENV", "_SCHEME"), "bearer").strip().casefold()
    if scheme_value not in {"bearer", "api_key"}:
        raise ExternalConfigurationError(f"{name} credential scheme is invalid")
    scheme = cast(Literal["bearer", "api_key"], scheme_value)
    try:
        return CredentialRef(env_name=env_name, header_name=header, scheme=scheme)
    except ValueError as error:
        raise ExternalConfigurationError(f"{name} credential reference is invalid") from error


def _bounded_int(
    environment: Mapping[str, str],
    name: str,
    default: int,
    lower: int,
    upper: int,
) -> int:
    raw = environment.get(name)
    value = default if raw is None or not raw.strip() else int(raw)
    if not lower <= value <= upper:
        raise ExternalConfigurationError(f"{name} is outside the safe range")
    return value


def _any_present(environment: Mapping[str, str], *names: str) -> bool:
    return any(bool(environment.get(name, "").strip()) for name in names)


def _allows_loopback(hosts: tuple[str, ...]) -> bool:
    for host in hosts:
        value = host.strip().casefold().strip("[]")
        if value == "localhost":
            return True
        try:
            if ipaddress.ip_address(value).is_loopback:
                return True
        except ValueError:
            continue
    return False


def _bounded_float(
    environment: Mapping[str, str],
    name: str,
    default: float,
    lower: float,
    upper: float,
) -> float:
    raw = environment.get(name)
    value = default if raw is None or not raw.strip() else float(raw)
    if not lower <= value <= upper:
        raise ExternalConfigurationError(f"{name} is outside the safe range")
    return value
