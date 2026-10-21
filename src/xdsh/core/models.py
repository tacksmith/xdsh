"""Runner-neutral provider/model discovery through the loopback LiteLLM gateway."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import time
from dataclasses import dataclass
from enum import Enum
from http.client import HTTPMessage
from typing import IO, Literal, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}$")
_PROVIDER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_VOLCENGINE_HOST_SUFFIX = ".volces.com"


class _NoCredentialRedirect(HTTPRedirectHandler):
    """A loopback endpoint must never redirect its Authorization to another host."""

    def redirect_request(
        self, req: Request, fp: IO[bytes], code: int, msg: str,
        headers: HTTPMessage, newurl: str,
    ) -> None:
        return None

# Capability names are deliberately small and transport-neutral.  ``None``
# means that the provider did not make a claim; callers must keep the feature
# available in that case for backwards compatibility, while an explicit
# ``False`` is safe grounds for disabling a selector.  In particular, model
# names are never used as a capability oracle.
MODEL_CAPABILITIES = frozenset(
    {"reasoning", "thinking", "tools", "vision", "audio", "structured_output"}
)
ModelCapabilityState = Literal["supported", "unsupported", "unknown"]
_CAPABILITY_ALIASES: dict[str, tuple[str, ...]] = {
    "reasoning": ("supports_reasoning", "reasoning"),
    "thinking": (
        "supports_thinking",
        "supports_extended_thinking",
        "thinking",
    ),
    "tools": (
        "supports_function_calling",
        "supports_tool_choice",
        "supports_tools",
        "tools",
    ),
    "vision": (
        "supports_vision",
        "supports_image_input",
        "vision",
    ),
    "audio": (
        "supports_audio_input",
        "supports_audio",
        "audio",
    ),
    "structured_output": (
        "supports_structured_output",
        "supports_response_schema",
        "supports_json_schema",
        "structured_output",
    ),
}

_PROVIDER_PRESENTATION: dict[str, tuple[str, str]] = {
    "volc-coding": ("🔥", "火山引擎 ARK"),
    "local-llama": ("🖥️", "本地 LLM"),
    "anthropic": ("☁️", "Anthropic-compatible"),
    "openai": ("☁️", "OpenAI-compatible"),
}


class ModelCatalogError(RuntimeError):
    """Model discovery failed without exposing credentials."""


class ModelAvailability(str, Enum):
    """Execution health is distinct from metadata catalog presence."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ModelRouteHealth:
    """Credential-safe result of one minimal route execution probe."""

    model: str
    availability: ModelAvailability
    detail: str = ""

    def __post_init__(self) -> None:
        if _MODEL_ID.fullmatch(self.model) is None:
            raise ValueError("model health contains an invalid model ID")
        if len(self.detail) > 128 or "\n" in self.detail or "\r" in self.detail:
            raise ValueError("model health detail must be a short single-line code")


@dataclass(frozen=True, slots=True)
class ModelProfile:
    """Optional capabilities for one live model route.

    Unknown values remain ``None``.  A catalog must never infer support merely
    from a model name, so transports can distinguish verified configuration
    from an absent metadata field.
    """

    id: str
    reasoning: bool | None = None
    context_window_tokens: int | None = None
    max_output_tokens: int | None = None
    thinking: bool | None = None
    tools: bool | None = None
    vision: bool | None = None
    audio: bool | None = None
    structured_output: bool | None = None

    def __post_init__(self) -> None:
        if _MODEL_ID.fullmatch(self.id) is None:
            raise ValueError("model profile contains an invalid model ID")
        if self.reasoning is not None and not isinstance(self.reasoning, bool):
            raise ValueError("model reasoning capability must be boolean when set")
        if self.context_window_tokens is not None and not (
            1_024 <= self.context_window_tokens <= 10_000_000
        ):
            raise ValueError("model context window is out of bounds")
        if self.max_output_tokens is not None and not (
            1 <= self.max_output_tokens <= 2_000_000
        ):
            raise ValueError("model output limit is out of bounds")
        for capability in MODEL_CAPABILITIES:
            value = getattr(self, capability)
            if value is not None and not isinstance(value, bool):
                raise ValueError(f"model {capability} capability must be boolean when set")

    def capability(self, name: str) -> bool | None:
        """Return one declared capability, preserving an unknown state."""

        if name not in MODEL_CAPABILITIES:
            raise ValueError(f"unknown model capability: {name}")
        return cast(bool | None, getattr(self, name))


@dataclass(frozen=True, slots=True)
class ModelProvider:
    """One selectable LiteLLM provider and its public model aliases."""

    id: str
    models: tuple[str, ...]
    label: str | None = None
    icon: str | None = None

    def __post_init__(self) -> None:
        if _PROVIDER_ID.fullmatch(self.id) is None:
            raise ValueError("provider ID is invalid")
        if not self.models:
            raise ValueError("provider must contain at least one model")
        if len(self.models) != len(set(self.models)):
            raise ValueError("provider model IDs must be unique")
        if any(_MODEL_ID.fullmatch(model) is None for model in self.models):
            raise ValueError("provider contains an invalid model ID")
        if self.label is not None and (
            not self.label.strip()
            or len(self.label) > 64
            or any(character in self.label for character in "\r\n")
        ):
            raise ValueError("provider label must be a short single line")
        if self.icon is not None and (
            not self.icon.strip()
            or len(self.icon) > 16
            or any(character in self.icon for character in "\r\n")
        ):
            raise ValueError("provider icon must be short and non-empty")

    @property
    def token(self) -> str:
        """Stable callback-safe token independent of provider ID length."""

        return _selection_token(self.id)

    @property
    def display_label(self) -> str:
        """Human label with stable ID retained for unambiguous selection."""

        return provider_display_label(self.id, label=self.label, icon=self.icon)


@dataclass(frozen=True, slots=True)
class ModelDirectory:
    """Immutable provider/model snapshot used by selectors and validation."""

    providers: tuple[ModelProvider, ...]
    health: tuple[ModelRouteHealth, ...] = ()
    profiles: tuple[ModelProfile, ...] = ()
    # Routing facts are not presentation aliases. Preserve them through
    # decorators so a successful catch-all cannot certify an online model.
    route_names: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if not self.providers:
            raise ValueError("model directory must contain at least one provider")
        provider_ids = [provider.id for provider in self.providers]
        if len(provider_ids) != len(set(provider_ids)):
            raise ValueError("model directory provider IDs must be unique")
        models = [model for provider in self.providers for model in provider.models]
        if len(models) != len(set(models)):
            raise ValueError("a model ID cannot belong to multiple providers")
        health_models = [item.model for item in self.health]
        if len(health_models) != len(set(health_models)):
            raise ValueError("model health entries must be unique")
        if any(model not in models for model in health_models):
            raise ValueError("model health must refer to a catalog model")
        profile_models = [item.id for item in self.profiles]
        if len(profile_models) != len(set(profile_models)):
            raise ValueError("model profiles must be unique")
        if any(model not in models for model in profile_models):
            raise ValueError("model profiles must refer to a catalog model")

    @property
    def models(self) -> tuple[str, ...]:
        return tuple(sorted(model for provider in self.providers for model in provider.models))

    def resolve_provider(self, token: str) -> ModelProvider | None:
        matches = [provider for provider in self.providers if provider.token == token]
        return matches[0] if len(matches) == 1 else None

    def resolve_model(self, token: str) -> str | None:
        matches = [model for model in self.models if _selection_token(model) == token]
        return matches[0] if len(matches) == 1 else None

    def provider_for(self, model: str) -> ModelProvider | None:
        return next((provider for provider in self.providers if model in provider.models), None)

    def model_token(self, model: str) -> str:
        if model not in self.models:
            raise ValueError("model is not present in this directory")
        return _selection_token(model)

    def health_for(self, model: str) -> ModelRouteHealth:
        if model not in self.models:
            raise ValueError("model is not present in this directory")
        if self.is_shadowed_route(model):
            return ModelRouteHealth(
                model, ModelAvailability.UNAVAILABLE, "unconfigured_wildcard_route",
            )
        return next(
            (item for item in self.health if item.model == model),
            ModelRouteHealth(model=model, availability=ModelAvailability.UNKNOWN),
        )

    def is_shadowed_route(self, model: str) -> bool:
        """A concrete requested alias must never silently hit a catch-all."""
        return (
            self.route_names is not None
            and "*" in self.route_names
            and model not in self.route_names
        )

    def profile_for(self, model: str) -> ModelProfile:
        if model not in self.models:
            raise ValueError("model is not present in this directory")
        return next(
            (item for item in self.profiles if item.id == model),
            ModelProfile(id=model),
        )

    def capability_for(self, model: str, capability: str) -> bool | None:
        """Return a model capability without treating missing metadata as false."""

        return self.profile_for(model).capability(capability)

    def capability_state(self, model: str, capability: str) -> ModelCapabilityState:
        """Return a stable three-state capability value for transport policy.

        A missing declaration is deliberately ``unknown`` rather than false.
        This keeps older LiteLLM/DSH metadata usable while allowing an explicit
        negative declaration to disable a feature safely.
        """

        value = self.capability_for(model, capability)
        if value is True:
            return "supported"
        if value is False:
            return "unsupported"
        return "unknown"

    def capability_allowed(
        self,
        model: str,
        capability: str,
        *,
        allow_unknown: bool = True,
    ) -> bool:
        """Decide whether a requested capability may cross the route boundary."""

        state = self.capability_state(model, capability)
        return state == "supported" or (state == "unknown" and allow_unknown)

    def models_for_capability(
        self,
        capability: str,
        *,
        include_unknown: bool = False,
    ) -> tuple[str, ...]:
        """List models explicitly supporting a capability.

        Selectors can request ``include_unknown`` when presenting a broad
        provider list.  Execution code should normally leave it false and
        only rely on explicit positive metadata.
        """

        if capability not in MODEL_CAPABILITIES:
            raise ValueError(f"unknown model capability: {capability}")
        models = []
        for model in self.models:
            value = self.capability_for(model, capability)
            if value is True or (include_unknown and value is None):
                models.append(model)
        return tuple(models)


class ModelCatalog(Protocol):
    """Remote catalog seam; callers consume one coherent snapshot per interaction."""

    async def snapshot(self) -> ModelDirectory: ...


class ModelExecutionProbe(Protocol):
    """Minimal real request used to distinguish configured from executable routes."""

    async def probe(self, model: str) -> ModelRouteHealth: ...


class ModelRouteVerifier(Protocol):
    """Metadata-only admission check; never spend tokens to validate a name."""

    async def check(self, model: str) -> ModelRouteHealth | None: ...


class LiteLlmRouteVerifier:
    """Reject known wildcard substitutions before starting a runner.

    Missing metadata remains unknown for Messages-compatible gateways that do
    not implement LiteLLM's catalog. Unknown is not evidence of identity.
    Only routing names are cached, never a payload, credential or response.
    """

    def __init__(
        self, *, base_url_env: str = "XDSH_LITELLM_BASE_URL",
        api_key_env: str = "XDSH_LITELLM_API_KEY", ttl_seconds: float = 30.0,
    ) -> None:
        if not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
            raise ValueError("route verification TTL must be finite and positive")
        self._base_url_env = base_url_env
        self._api_key_env = api_key_env
        self._ttl_seconds = ttl_seconds
        self._base_url: str | None = None
        self._route_names: tuple[str, ...] | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def check(self, model: str) -> ModelRouteHealth | None:
        base_url = os.environ.get(self._base_url_env, "")
        async with self._lock:
            if base_url != self._base_url or time.monotonic() >= self._expires_at:
                if base_url != self._base_url:
                    self._route_names = None
                catalog = LiteLlmModelCatalog(
                    base_url, api_key_env=self._api_key_env, timeout_seconds=3.0,
                    configured_model=model, include_configured_aliases=True,
                )
                try:
                    self._route_names = (await catalog.snapshot()).route_names
                except ModelCatalogError:
                    # An outage must not erase an already observed catch-all
                    # and allow a previously rejected alias through it.
                    pass
                self._base_url = base_url
                self._expires_at = time.monotonic() + self._ttl_seconds
            if (
                self._route_names is not None
                and "*" in self._route_names
                and model not in self._route_names
            ):
                return ModelRouteHealth(
                    model, ModelAvailability.UNAVAILABLE, "unconfigured_wildcard_route",
                )
        return None


@dataclass(frozen=True, slots=True)
class FixedModelCatalog:
    """Deterministic catalog adapter for tests and minimal configurations."""

    directory: ModelDirectory

    @classmethod
    def from_models(
        cls,
        *models: str,
        provider: str = "default",
    ) -> FixedModelCatalog:
        return cls(ModelDirectory((ModelProvider(provider, tuple(sorted(models))),)))

    async def snapshot(self) -> ModelDirectory:
        return self.directory


@dataclass(frozen=True, slots=True)
class LiteLlmModelCatalog:
    """Read LiteLLM provider metadata over a credential-free loopback URL."""

    base_url: str
    api_key_env: str = "XDSH_LITELLM_API_KEY"
    timeout_seconds: float = 5.0
    provider_aliases: tuple[ModelProvider, ...] = ()
    # The live /model/info endpoint is not authoritative for every explicitly
    # configured LiteLLM alias.  Production selectors opt into showing these
    # declarations; they remain UNKNOWN until a bounded execution probe (or a
    # real task) proves them usable.
    configured_model: str | None = None
    include_configured_aliases: bool = False

    def __post_init__(self) -> None:
        parsed = urlsplit(self.base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("LiteLLM model catalog must use a credential-free loopback URL")
        if self.timeout_seconds <= 0:
            raise ValueError("model catalog timeout must be positive")
        if self.configured_model is not None and _MODEL_ID.fullmatch(self.configured_model) is None:
            raise ValueError("configured model ID is invalid")
        provider_ids = [provider.id for provider in self.provider_aliases]
        if len(provider_ids) != len(set(provider_ids)):
            raise ValueError("model provider aliases must use unique provider IDs")
        alias_models = [
            model for provider in self.provider_aliases for model in provider.models
        ]
        if len(alias_models) != len(set(alias_models)):
            raise ValueError("a model provider alias cannot belong to multiple providers")

    async def snapshot(self) -> ModelDirectory:
        return await asyncio.to_thread(self._snapshot)

    async def list_models(self) -> tuple[str, ...]:
        """Compatibility helper for non-selector callers."""

        return (await self.snapshot()).models

    def _snapshot(self) -> ModelDirectory:
        payload = self._request("model/info")
        data = payload.get("data")
        if not isinstance(data, list):
            raise ModelCatalogError("LiteLLM model catalog returned an invalid payload")
        aliases = {
            model: provider.id
            for provider in self.provider_aliases
            for model in provider.models
        }
        alias_providers = {provider.id: provider for provider in self.provider_aliases}
        grouped: dict[str, set[str]] = {}
        profiles: dict[str, ModelProfile] = {}
        route_names: set[str] = set()
        for raw_item in cast(list[object], data):
            item = _string_mapping(raw_item)
            if item is None:
                continue
            model = item.get("model_name")
            if model == "*":
                route_names.add("*")
                continue
            if not isinstance(model, str) or _MODEL_ID.fullmatch(model) is None:
                continue
            route_names.add(model)
            provider_id = aliases.get(model) or _provider_id(item) or "unclassified"
            grouped.setdefault(provider_id, set()).add(model)
            profile = _model_profile(model, item)
            if profile is not None:
                previous = profiles.get(model)
                profiles[model] = _merge_profiles(previous, profile)
        if self.include_configured_aliases:
            for alias_provider in self.provider_aliases:
                grouped.setdefault(alias_provider.id, set()).update(alias_provider.models)
            if (self.configured_model is not None and self.configured_model not in aliases
                    and self.configured_model not in route_names):
                grouped.setdefault("configured", set()).add(self.configured_model)
        providers = tuple(
            ModelProvider(
                provider,
                tuple(sorted(models)),
                label=(
                    alias_providers[provider].label
                    if provider in alias_providers
                    else ("Configured route" if provider == "configured" else None)
                ),
                icon=(
                    alias_providers[provider].icon
                    if provider in alias_providers
                    else ("⚙️" if provider == "configured" else None)
                ),
            )
            for provider, models in sorted(grouped.items())
            if models
        )
        if not providers:
            raise ModelCatalogError("LiteLLM model catalog returned no valid models")
        return ModelDirectory(
            providers,
            profiles=tuple(profiles[model] for model in sorted(profiles)),
            route_names=tuple(sorted(route_names)),
        )

    def _request(self, endpoint: str) -> dict[str, object]:
        headers = {"Accept": "application/json"}
        api_key = os.environ.get(self.api_key_env)
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = Request(
            f"{self.base_url.rstrip('/')}/{endpoint}",
            headers=headers,
            method="GET",
        )
        opener = build_opener(ProxyHandler({}), _NoCredentialRedirect())
        try:
            with opener.open(request, timeout=self.timeout_seconds) as response:
                raw_response = response.read(1_000_001)
        except HTTPError as error:
            raise ModelCatalogError(
                f"LiteLLM model catalog returned HTTP {error.code}"
            ) from None
        except (URLError, TimeoutError, OSError):
            raise ModelCatalogError("LiteLLM model catalog request failed") from None
        if len(raw_response) > 1_000_000:
            raise ModelCatalogError("LiteLLM model catalog response exceeds limit")
        try:
            payload = cast(object, json.loads(raw_response))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ModelCatalogError("LiteLLM model catalog returned invalid JSON") from None
        mapping = _string_mapping(payload)
        if mapping is None:
            raise ModelCatalogError("LiteLLM model catalog returned an invalid payload")
        return mapping


class ProbedModelCatalog:
    """Attach cached execution health to a metadata catalog snapshot."""

    def __init__(
        self,
        catalog: ModelCatalog,
        probe: ModelExecutionProbe,
        *,
        ttl_seconds: float = 900.0,
        max_probe_models: int = 64,
        max_concurrent_probes: int = 8,
        probe_on_snapshot: bool = True,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("model health TTL must be positive")
        if (
            isinstance(max_probe_models, bool)
            or not isinstance(max_probe_models, int)
            or not 1 <= max_probe_models <= 256
        ):
            raise ValueError("max_probe_models must be between 1 and 256")
        if (
            isinstance(max_concurrent_probes, bool)
            or not isinstance(max_concurrent_probes, int)
            or not 1 <= max_concurrent_probes <= 32
        ):
            raise ValueError("max_concurrent_probes must be between 1 and 32")
        self._catalog = catalog
        self._probe = probe
        self._ttl_seconds = ttl_seconds
        self._max_probe_models = max_probe_models
        self._max_concurrent_probes = max_concurrent_probes
        self._probe_on_snapshot = probe_on_snapshot
        self._cache: dict[str, tuple[float, ModelRouteHealth]] = {}
        self._lock = asyncio.Lock()

    async def snapshot(self) -> ModelDirectory:
        directory = await self._catalog.snapshot()
        if not self._probe_on_snapshot:
            # Interactive selectors consume declared metadata, not paid model
            # executions. Preserve explicit/shadowed route health as supplied.
            return directory
        now = time.monotonic()
        async with self._lock:
            stale = [
                model
                for model in directory.models
                if not directory.is_shadowed_route(model)
                and (model not in self._cache or self._cache[model][0] <= now)
            ]
            to_probe = stale[: self._max_probe_models]
            deferred = stale[self._max_probe_models :]
            if to_probe:
                semaphore = asyncio.Semaphore(self._max_concurrent_probes)

                async def bounded_probe(model: str) -> ModelRouteHealth | BaseException:
                    async with semaphore:
                        try:
                            return await self._probe.probe(model)
                        except Exception as error:
                            return error

                results = await asyncio.gather(*(bounded_probe(model) for model in to_probe))
                expires_at = time.monotonic() + self._ttl_seconds
                for model, result in zip(to_probe, results, strict=True):
                    route_health = (
                        result
                        if isinstance(result, ModelRouteHealth) and result.model == model
                        else ModelRouteHealth(
                            model=model,
                            availability=ModelAvailability.UNKNOWN,
                            detail="probe_failed",
                        )
                    )
                    self._cache[model] = (expires_at, route_health)
            if deferred:
                expires_at = time.monotonic() + self._ttl_seconds
                for model in deferred:
                    self._cache[model] = (
                        expires_at,
                        ModelRouteHealth(
                            model=model,
                            availability=ModelAvailability.UNKNOWN,
                            detail="probe_deferred",
                        ),
                    )
            known = set(directory.models)
            self._cache = {
                model: cached for model, cached in self._cache.items() if model in known
            }
            health_snapshot = tuple(
                directory.health_for(model)
                if directory.is_shadowed_route(model) else self._cache[model][1]
                for model in directory.models
                if model in self._cache or directory.is_shadowed_route(model)
            )
        return ModelDirectory(
            directory.providers, health_snapshot, directory.profiles, directory.route_names,
        )


@dataclass(frozen=True, slots=True)
class LiteLlmExecutionProbe:
    """Run a minimal no-tool Anthropic Messages request against loopback LiteLLM."""

    base_url: str
    api_key_env: str = "XDSH_LITELLM_API_KEY"
    timeout_seconds: float = 20.0

    def __post_init__(self) -> None:
        _validate_loopback_base_url(self.base_url, purpose="model probe")
        if self.timeout_seconds <= 0:
            raise ValueError("model probe timeout must be positive")

    async def probe(self, model: str) -> ModelRouteHealth:
        if _MODEL_ID.fullmatch(model) is None:
            raise ValueError("model probe received an invalid model ID")
        return await asyncio.to_thread(self._probe, model)

    def _probe(self, model: str) -> ModelRouteHealth:
        api_key = os.environ.get(self.api_key_env, "").strip()
        if not api_key:
            return ModelRouteHealth(
                model=model,
                availability=ModelAvailability.UNKNOWN,
                detail="credential_unavailable",
            )
        body = json.dumps(
            {
                "model": model,
                "max_tokens": 8,
                "messages": [{"role": "user", "content": "Reply only OK."}],
                "stream": False,
            }
        ).encode("utf-8")
        request = Request(
            f"{self.base_url.rstrip('/')}/v1/messages",
            data=body,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        opener = build_opener(ProxyHandler({}), _NoCredentialRedirect())
        try:
            with opener.open(request, timeout=self.timeout_seconds) as response:
                raw_response = response.read(65_537)
        except HTTPError as error:
            permanent_route_failure = error.code in {400, 404, 405, 410, 422}
            return ModelRouteHealth(
                model=model,
                availability=(
                    ModelAvailability.UNAVAILABLE
                    if permanent_route_failure
                    else ModelAvailability.UNKNOWN
                ),
                detail=f"http_{error.code}",
            )
        except (URLError, TimeoutError, OSError):
            return ModelRouteHealth(
                model=model,
                availability=ModelAvailability.UNKNOWN,
                detail="transport_error",
            )
        if len(raw_response) > 65_536:
            return ModelRouteHealth(model, ModelAvailability.UNKNOWN, "response_too_large")
        try:
            payload = cast(object, json.loads(raw_response))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return ModelRouteHealth(
                model=model,
                availability=ModelAvailability.UNKNOWN,
                detail="invalid_response",
            )
        message = _string_mapping(payload)
        executable = (
            message is not None
            and message.get("type") == "message"
            and isinstance(message.get("content"), list)
        )
        return ModelRouteHealth(
            model=model,
            availability=(
                ModelAvailability.AVAILABLE
                if executable
                else ModelAvailability.UNKNOWN
            ),
            detail="execution_ok" if executable else "invalid_payload",
        )


def _provider_id(item: dict[str, object]) -> str | None:
    model_info = _string_mapping(item.get("model_info"))
    operator_provider = model_info.get("xdsh_provider") if model_info is not None else None
    if isinstance(operator_provider, str) and _PROVIDER_ID.fullmatch(operator_provider):
        return operator_provider
    provider = model_info.get("litellm_provider") if model_info is not None else None
    parameters = _string_mapping(item.get("litellm_params"))
    route_provider = _route_provider_from_api_base(
        parameters.get("api_base") if parameters is not None else None
    )
    if route_provider is not None:
        return route_provider
    if not isinstance(provider, str) and parameters is not None:
        provider = parameters.get("custom_llm_provider")
        if not isinstance(provider, str):
            routed_model = parameters.get("model")
            if isinstance(routed_model, str) and "/" in routed_model:
                provider = routed_model.split("/", maxsplit=1)[0]
    if not isinstance(provider, str) or _PROVIDER_ID.fullmatch(provider) is None:
        return None
    return provider


def _model_profile(model: str, item: dict[str, object]) -> ModelProfile | None:
    model_info = _string_mapping(item.get("model_info")) or {}
    parameters = _string_mapping(item.get("litellm_params")) or {}
    capabilities = {
        name: _capability_value(name, model_info, parameters)
        for name in MODEL_CAPABILITIES
    }
    context_window = _bounded_integer(
        model_info.get("max_input_tokens", model_info.get("max_tokens")),
        minimum=1_024,
        maximum=10_000_000,
    )
    max_output = _bounded_integer(
        model_info.get("max_output_tokens"),
        minimum=1,
        maximum=2_000_000,
    )
    if (
        all(value is None for value in capabilities.values())
        and context_window is None
        and max_output is None
    ):
        return None
    return ModelProfile(
        id=model,
        reasoning=capabilities["reasoning"],
        context_window_tokens=context_window,
        max_output_tokens=max_output,
        thinking=capabilities["thinking"],
        tools=capabilities["tools"],
        vision=capabilities["vision"],
        audio=capabilities["audio"],
        structured_output=capabilities["structured_output"],
    )


def _capability_value(
    capability: str,
    model_info: dict[str, object],
    parameters: dict[str, object],
) -> bool | None:
    """Read only explicit boolean capability claims from LiteLLM metadata."""

    aliases = _CAPABILITY_ALIASES[capability]
    for source in (model_info, parameters):
        for key in aliases:
            value = source.get(key)
            if isinstance(value, bool):
                return value
        raw_map = source.get("capabilities")
        if isinstance(raw_map, dict):
            for key in aliases:
                value = raw_map.get(key)
                if isinstance(value, bool):
                    return value
        elif isinstance(raw_map, list):
            normalized = {
                item.casefold().replace("-", "_")
                for item in raw_map
                if isinstance(item, str) and len(item) <= 64
            }
            canonical_aliases = {
                key.casefold().replace("-", "_") for key in aliases
            }
            if normalized.intersection(canonical_aliases):
                return True
    return None


def _merge_profiles(first: ModelProfile | None, second: ModelProfile) -> ModelProfile:
    """Merge duplicate catalog rows without turning an unknown into false."""

    if first is None:
        return second
    def merged(capability: str) -> bool | None:
        current = second.capability(capability)
        return current if current is not None else first.capability(capability)

    return ModelProfile(
        id=second.id,
        reasoning=merged("reasoning"),
        context_window_tokens=(
            second.context_window_tokens
            if second.context_window_tokens is not None
            else first.context_window_tokens
        ),
        max_output_tokens=(
            second.max_output_tokens
            if second.max_output_tokens is not None
            else first.max_output_tokens
        ),
        thinking=merged("thinking"),
        tools=merged("tools"),
        vision=merged("vision"),
        audio=merged("audio"),
        structured_output=merged("structured_output"),
    )


def _bounded_integer(value: object, *, minimum: int, maximum: int) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return value if minimum <= value <= maximum else None


def parse_model_provider_aliases(value: str) -> tuple[ModelProvider, ...]:
    """Parse a bounded Hermes-style ``provider=model1,model2`` list.

    The declaration only changes presentation/grouping for model IDs that the
    live LiteLLM catalog returns.  It contains no endpoint or credential data.
    """

    if not isinstance(value, str) or len(value) > 16_384:
        raise ValueError("model provider aliases are too large")
    if not value.strip():
        return ()
    providers: list[ModelProvider] = []
    seen_providers: set[str] = set()
    seen_models: set[str] = set()
    groups = value.split(";")
    if len(groups) > 32:
        raise ValueError("too many model provider aliases")
    for group in groups:
        provider_text, separator, models_text = group.partition("=")
        if not separator or not provider_text or not models_text:
            raise ValueError("model provider alias must use provider=model1,model2")
        if provider_text in seen_providers:
            raise ValueError("model provider aliases must use unique provider IDs")
        model_values = models_text.split(",")
        if not model_values or any(not model for model in model_values):
            raise ValueError("model provider alias contains an empty model")
        if len(model_values) > 256:
            raise ValueError("too many models in one provider alias")
        if any(model in seen_models for model in model_values):
            raise ValueError("a model provider alias cannot belong to multiple providers")
        provider = ModelProvider(provider_text, tuple(model_values))
        providers.append(provider)
        seen_providers.add(provider.id)
        seen_models.update(provider.models)
    return tuple(providers)


def _route_provider_from_api_base(value: object) -> str | None:
    """Map trusted LiteLLM route hosts to Hermes-compatible provider labels.

    LiteLLM often reports the wire adapter (``anthropic``/``openai``) while
    the route's ``api_base`` contains the actual upstream.  We only recognize
    fixed, non-credential-bearing host shapes here; this never performs DNS or
    network access and falls back to LiteLLM's explicit provider metadata.
    """

    if not isinstance(value, str) or len(value) > 2048:
        return None
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        # Accessing ``port`` validates malformed numeric ports without ever
        # opening a socket or resolving the route.
        _ = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or host is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return None
    normalized_host = host.rstrip(".").casefold()
    if normalized_host in _LOOPBACK_HOSTS:
        return "local-llama"
    if normalized_host.endswith(_VOLCENGINE_HOST_SUFFIX) and normalized_host != "volces.com":
        return "volc-coding"
    return None


def provider_display_label(
    provider_id: str,
    *,
    label: str | None = None,
    icon: str | None = None,
) -> str:
    """Return a credential-free, operator-friendly provider label."""

    default_icon, default_label = _PROVIDER_PRESENTATION.get(
        provider_id, ("🧰", provider_id)
    )
    return f"{icon or default_icon} {label or default_label} · {provider_id}"


def _selection_token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _string_mapping(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    raw = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in raw):
        return None
    return cast(dict[str, object], raw)


def _validate_loopback_base_url(value: str, *, purpose: str) -> None:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError(f"LiteLLM {purpose} must use a credential-free loopback URL")
