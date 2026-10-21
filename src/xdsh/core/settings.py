"""Credential-free user settings shared by every XDSH interface.

LiteLLM remains the authority for executable routes.  Settings can decorate
live routes and, when explicitly opted in by a product surface, advertise
operator-declared routes that a gateway omits from ``/model/info``.  Such
routes are deliberately unverified and must be execution-probed before they
are presented as healthy.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypeVar, cast

from xdsh.core.models import (
    ModelCatalog,
    ModelCatalogError,
    ModelDirectory,
    ModelProfile,
    ModelProvider,
)
from xdsh.core.product_control import redact_sensitive_text

_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}$")
_PROVIDER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_MAX_SETTINGS_BYTES = 64 * 1024
_MAX_PROVIDERS = 64
_MAX_MODELS = 1_024
_DOCUMENT_KEYS = frozenset({"schema_version", "default_model", "providers", "models"})
_PROVIDER_KEYS = frozenset({"id", "label", "icon"})
_MODEL_KEYS = frozenset(
    {
        "id",
        "provider",
        "reasoning",
        "context_window_tokens",
        "max_output_tokens",
        "thinking",
        "tools",
        "vision",
        "audio",
        "structured_output",
    }
)


class SettingsError(ValueError):
    """A settings document is unsafe, malformed, or outside its schema."""


@dataclass(frozen=True, slots=True)
class ProviderSettings:
    """Optional presentation for one live provider group."""

    id: str
    label: str | None = None
    icon: str | None = None

    def __post_init__(self) -> None:
        _require_credential_free(self.id, "provider id")
        if self.label is not None:
            _require_credential_free(self.label, "provider label")
        if self.icon is not None:
            _require_credential_free(self.icon, "provider icon")
        # Reuse the public model value object as the authoritative validator.
        ModelProvider(self.id, ("validation-model",), self.label, self.icon)


@dataclass(frozen=True, slots=True)
class ModelSettings:
    """Operator-declared metadata for a model returned by LiteLLM."""

    id: str
    provider: str | None = None
    reasoning: bool | None = None
    context_window_tokens: int | None = None
    max_output_tokens: int | None = None
    thinking: bool | None = None
    tools: bool | None = None
    vision: bool | None = None
    audio: bool | None = None
    structured_output: bool | None = None

    def __post_init__(self) -> None:
        _require_credential_free(self.id, "model id")
        if self.provider is not None and _PROVIDER_ID.fullmatch(self.provider) is None:
            raise ValueError("model provider override is invalid")
        if self.provider is not None:
            _require_credential_free(self.provider, "model provider")
        ModelProfile(
            id=self.id,
            reasoning=self.reasoning,
            context_window_tokens=self.context_window_tokens,
            max_output_tokens=self.max_output_tokens,
            thinking=self.thinking,
            tools=self.tools,
            vision=self.vision,
            audio=self.audio,
            structured_output=self.structured_output,
        )


@dataclass(frozen=True, slots=True)
class XdshSettings:
    """Small versioned settings snapshot; credentials are not part of the schema."""

    default_model: str | None = None
    providers: tuple[ProviderSettings, ...] = ()
    models: tuple[ModelSettings, ...] = ()

    def __post_init__(self) -> None:
        if self.default_model is not None and _MODEL_ID.fullmatch(self.default_model) is None:
            raise ValueError("default model is invalid")
        if self.default_model is not None:
            _require_credential_free(self.default_model, "default model")
        if len(self.providers) > _MAX_PROVIDERS:
            raise ValueError("too many provider settings")
        if len(self.models) > _MAX_MODELS:
            raise ValueError("too many model settings")
        provider_ids = [item.id for item in self.providers]
        model_ids = [item.id for item in self.models]
        if len(provider_ids) != len(set(provider_ids)):
            raise ValueError("provider settings must use unique IDs")
        if len(model_ids) != len(set(model_ids)):
            raise ValueError("model settings must use unique IDs")


@dataclass(frozen=True, slots=True)
class SettingsSnapshot:
    """One immutable settings view with an optimistic-concurrency revision."""

    settings: XdshSettings
    revision: str

    @property
    def document(self) -> dict[str, object]:
        return settings_to_document(self.settings)


@dataclass(frozen=True, slots=True)
class ReplaceSettings:
    settings: XdshSettings


@dataclass(frozen=True, slots=True)
class SetDefaultModel:
    model: str | None

    def __post_init__(self) -> None:
        if self.model is not None:
            XdshSettings(default_model=self.model)


@dataclass(frozen=True, slots=True)
class UpsertProvider:
    provider: ProviderSettings


@dataclass(frozen=True, slots=True)
class RemoveProvider:
    provider_id: str

    def __post_init__(self) -> None:
        ProviderSettings(self.provider_id)


@dataclass(frozen=True, slots=True)
class UpsertModel:
    model: ModelSettings


@dataclass(frozen=True, slots=True)
class RemoveModel:
    model_id: str

    def __post_init__(self) -> None:
        ModelSettings(self.model_id)


SettingsAction = (
    ReplaceSettings
    | SetDefaultModel
    | UpsertProvider
    | RemoveProvider
    | UpsertModel
    | RemoveModel
)
_SettingsItem = TypeVar("_SettingsItem", ProviderSettings, ModelSettings)


class SettingsReader(Protocol):
    """Read-only seam consumed by catalogs and user interfaces."""

    def load(self) -> XdshSettings: ...


class JsonSettingsStore:
    """Atomic owner-only persistence for the credential-free settings schema."""

    def __init__(self, path: Path) -> None:
        self.path = validate_settings_path(path)

    def load(self) -> XdshSettings:
        if self.path.is_symlink():
            raise SettingsError("settings path must not be a symlink")
        if not self.path.exists():
            return XdshSettings()
        if not self.path.is_file():
            raise SettingsError("settings path must be a regular file")
        try:
            with self.path.open("rb") as handle:
                raw = handle.read(_MAX_SETTINGS_BYTES + 1)
        except OSError as error:
            raise SettingsError("settings file cannot be read") from error
        if len(raw) > _MAX_SETTINGS_BYTES:
            raise SettingsError("settings file is too large")
        try:
            document = cast(
                object,
                json.loads(
                    raw,
                    parse_constant=_reject_json_constant,
                    object_pairs_hook=_reject_duplicate_json_pairs,
                ),
            )
        except (UnicodeDecodeError, ValueError) as error:
            raise SettingsError("settings file is not valid UTF-8 JSON") from error
        return _decode_settings(document)

    def save(self, settings: XdshSettings) -> None:
        document = _encode_settings(settings)
        serialized = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True)
        payload = (serialized + "\n").encode("utf-8")
        if len(payload) > _MAX_SETTINGS_BYTES:
            raise SettingsError("settings document is too large")
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise SettingsError("settings path must not use a symlink")
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.path.parent.chmod(stat.S_IMODE(self.path.parent.stat().st_mode) & 0o700)
        except OSError as error:
            raise SettingsError("settings directory cannot be prepared") from error
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            self.path.chmod(0o600)
        except OSError as error:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise SettingsError("settings file cannot be saved") from error


class SettingsController:
    """Own safe mutations for every local settings interface.

    Callers submit a typed action and the revision they observed.  The
    controller reloads immediately before applying it, so two Web/CLI editors
    cannot silently overwrite each other.
    """

    def __init__(self, store: JsonSettingsStore) -> None:
        self.store = store

    def snapshot(self) -> SettingsSnapshot:
        settings = self.store.load()
        return SettingsSnapshot(settings, settings_revision(settings))

    def apply(self, action: SettingsAction, *, expected_revision: str) -> SettingsSnapshot:
        if not re.fullmatch(r"[0-9a-f]{64}", expected_revision):
            raise SettingsError("settings revision is invalid")
        current = self.snapshot()
        if current.revision != expected_revision:
            raise SettingsError("settings changed; reload before saving")
        updated = _apply_settings_action(current.settings, action)
        self.store.save(updated)
        return SettingsSnapshot(updated, settings_revision(updated))


class ConfiguredModelCatalog:
    """Decorate a live catalog with operator-owned names and capabilities.

    ``include_unlisted_models`` is opt-in so library callers retain the safe
    historical behavior.  Product selectors enable it to support providers
    whose aliases are executable but intentionally hidden by LiteLLM's model
    metadata endpoint.  The resulting routes carry no positive health claim;
    a probing wrapper (when present) is responsible for verification.
    """

    def __init__(
        self,
        catalog: ModelCatalog,
        settings: SettingsReader,
        *,
        include_unlisted_models: bool = False,
    ) -> None:
        self._catalog = catalog
        self._settings = settings
        if not isinstance(include_unlisted_models, bool):
            raise TypeError("include_unlisted_models must be boolean")
        self._include_unlisted_models = include_unlisted_models

    async def snapshot(self) -> ModelDirectory:
        try:
            directory, settings = await asyncio.gather(
                self._catalog.snapshot(),
                asyncio.to_thread(self._settings.load),
            )
            return _apply_settings(
                directory,
                settings,
                include_unlisted_models=self._include_unlisted_models,
            )
        except ModelCatalogError:
            raise
        except (SettingsError, ValueError) as error:
            # Keep malformed operator metadata on the same read-only catalog
            # error seam as a gateway outage.  Telegram/CLI can then present a
            # bounded recovery message instead of crashing a command handler.
            raise ModelCatalogError("XDSH model settings are invalid") from error


def _apply_settings(
    directory: ModelDirectory,
    settings: XdshSettings,
    *,
    include_unlisted_models: bool = False,
) -> ModelDirectory:
    live_models = set(directory.models)
    provider_by_model = {
        model: provider.id for provider in directory.providers for model in provider.models
    }
    configured_models = {
        item.id: item
        for item in settings.models
        if include_unlisted_models or item.id in live_models
    }
    if include_unlisted_models and settings.default_model is not None:
        configured_models.setdefault(
            settings.default_model,
            ModelSettings(id=settings.default_model),
        )
    if include_unlisted_models:
        for model, model_settings in configured_models.items():
            if model not in live_models:
                provider_by_model[model] = model_settings.provider or "configured"
    for model, model_settings in configured_models.items():
        if model_settings.provider is not None:
            provider_by_model[model] = model_settings.provider

    presentation = {item.id: item for item in settings.providers}
    original = {item.id: item for item in directory.providers}
    grouped: dict[str, list[str]] = {}
    models = tuple(sorted(provider_by_model))
    for model in models:
        grouped.setdefault(provider_by_model[model], []).append(model)
    providers: list[ModelProvider] = []
    for provider_id, provider_models in sorted(grouped.items()):
        selected = presentation.get(provider_id)
        inherited = original.get(provider_id)
        providers.append(
            ModelProvider(
                provider_id,
                tuple(sorted(provider_models)),
                label=(selected.label if selected is not None else None)
                or (inherited.label if inherited is not None else None),
                icon=(selected.icon if selected is not None else None)
                or (inherited.icon if inherited is not None else None),
            )
        )

    original_profiles = {item.id: item for item in directory.profiles}
    profiles: list[ModelProfile] = []
    for model in models:
        base = original_profiles.get(model, ModelProfile(model))
        configured = configured_models.get(model)
        if configured is None:
            profile = base
        else:
            profile = ModelProfile(
                id=model,
                reasoning=(
                    configured.reasoning
                    if configured.reasoning is not None
                    else base.reasoning
                ),
                context_window_tokens=(
                    configured.context_window_tokens
                    if configured.context_window_tokens is not None
                    else base.context_window_tokens
                ),
                max_output_tokens=(
                    configured.max_output_tokens
                    if configured.max_output_tokens is not None
                    else base.max_output_tokens
                ),
                thinking=(
                    configured.thinking
                    if configured.thinking is not None
                    else base.thinking
                ),
                tools=(
                    configured.tools if configured.tools is not None else base.tools
                ),
                vision=(
                    configured.vision if configured.vision is not None else base.vision
                ),
                audio=(
                    configured.audio if configured.audio is not None else base.audio
                ),
                structured_output=(
                    configured.structured_output
                    if configured.structured_output is not None
                    else base.structured_output
                ),
            )
        if any(
            value is not None
            for value in (
                profile.reasoning,
                profile.context_window_tokens,
                profile.max_output_tokens,
                profile.thinking,
                profile.tools,
                profile.vision,
                profile.audio,
                profile.structured_output,
            )
        ):
            profiles.append(profile)
    # Existing health entries are retained for live routes.  Explicitly
    # configured routes intentionally start without a health claim and will be
    # marked UNKNOWN by ``ProbedModelCatalog`` until a bounded probe runs.
    return ModelDirectory(
        tuple(providers), directory.health, tuple(profiles), directory.route_names,
    )


def _encode_settings(settings: XdshSettings) -> dict[str, object]:
    document: dict[str, object] = {
        "schema_version": 1,
        "providers": [
            _without_none({"id": item.id, "label": item.label, "icon": item.icon})
            for item in settings.providers
        ],
        "models": [
            _without_none(
                {
                    "id": item.id,
                    "provider": item.provider,
                    "reasoning": item.reasoning,
                    "context_window_tokens": item.context_window_tokens,
                    "max_output_tokens": item.max_output_tokens,
                    "thinking": item.thinking,
                    "tools": item.tools,
                    "vision": item.vision,
                    "audio": item.audio,
                    "structured_output": item.structured_output,
                }
            )
            for item in settings.models
        ],
    }
    if settings.default_model is not None:
        document["default_model"] = settings.default_model
    return document


def settings_to_document(settings: XdshSettings) -> dict[str, object]:
    """Return the stable credential-free JSON representation."""

    return _encode_settings(settings)


def settings_from_document(value: object) -> XdshSettings:
    """Validate one untrusted decoded JSON settings document."""

    return _decode_settings(value)


def settings_revision(settings: XdshSettings) -> str:
    """Hash canonical settings bytes for optimistic concurrency."""

    payload = json.dumps(
        _encode_settings(settings),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _apply_settings_action(settings: XdshSettings, action: SettingsAction) -> XdshSettings:
    if isinstance(action, ReplaceSettings):
        return action.settings
    if isinstance(action, SetDefaultModel):
        return XdshSettings(action.model, settings.providers, settings.models)
    if isinstance(action, UpsertProvider):
        providers = _upsert_by_id(settings.providers, action.provider)
        return XdshSettings(settings.default_model, providers, settings.models)
    if isinstance(action, RemoveProvider):
        if any(model.provider == action.provider_id for model in settings.models):
            raise SettingsError("provider is still referenced by a model")
        providers = tuple(item for item in settings.providers if item.id != action.provider_id)
        if len(providers) == len(settings.providers):
            raise SettingsError("provider does not exist")
        return XdshSettings(settings.default_model, providers, settings.models)
    if isinstance(action, UpsertModel):
        models = _upsert_by_id(settings.models, action.model)
        return XdshSettings(settings.default_model, settings.providers, models)
    if isinstance(action, RemoveModel):
        if settings.default_model == action.model_id:
            raise SettingsError("default model must be changed before removal")
        models = tuple(item for item in settings.models if item.id != action.model_id)
        if len(models) == len(settings.models):
            raise SettingsError("model does not exist")
        return XdshSettings(settings.default_model, settings.providers, models)
    raise TypeError("unsupported settings action")


def _upsert_by_id(
    items: tuple[_SettingsItem, ...],
    replacement: _SettingsItem,
) -> tuple[_SettingsItem, ...]:
    output = [item for item in items if item.id != replacement.id]
    output.append(replacement)
    output.sort(key=lambda item: item.id)
    return tuple(output)


def _decode_settings(value: object) -> XdshSettings:
    document = _mapping(value, "settings document")
    unknown = set(document).difference(_DOCUMENT_KEYS)
    if unknown:
        names = ", ".join(sorted(unknown))
        raise SettingsError(f"settings document contains unknown fields: {names}")
    if document.get("schema_version") != 1:
        raise SettingsError("settings schema_version must be 1")
    providers = _object_list(document.get("providers", []), "providers", _MAX_PROVIDERS)
    models = _object_list(document.get("models", []), "models", _MAX_MODELS)
    try:
        return XdshSettings(
            default_model=_optional_string(document.get("default_model"), "default_model"),
            providers=tuple(_decode_provider(item) for item in providers),
            models=tuple(_decode_model(item) for item in models),
        )
    except ValueError as error:
        raise SettingsError(str(error)) from error


def _decode_provider(value: dict[str, object]) -> ProviderSettings:
    _reject_unknown(value, _PROVIDER_KEYS, "provider")
    return ProviderSettings(
        id=_required_string(value.get("id"), "provider id"),
        label=_optional_string(value.get("label"), "provider label"),
        icon=_optional_string(value.get("icon"), "provider icon"),
    )


def _decode_model(value: dict[str, object]) -> ModelSettings:
    _reject_unknown(value, _MODEL_KEYS, "model")
    reasoning = value.get("reasoning")
    if reasoning is not None and not isinstance(reasoning, bool):
        raise SettingsError("model reasoning must be boolean")
    capabilities: dict[str, bool | None] = {}
    for name in ("thinking", "tools", "vision", "audio", "structured_output"):
        capability = value.get(name)
        if capability is not None and not isinstance(capability, bool):
            raise SettingsError(f"model {name} must be boolean")
        capabilities[name] = capability
    return ModelSettings(
        id=_required_string(value.get("id"), "model id"),
        provider=_optional_string(value.get("provider"), "model provider"),
        reasoning=reasoning,
        context_window_tokens=_optional_integer(
            value.get("context_window_tokens"), "model context_window_tokens"
        ),
        max_output_tokens=_optional_integer(
            value.get("max_output_tokens"), "model max_output_tokens"
        ),
        **capabilities,
    )


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise SettingsError(f"{label} must be an object")
    return cast(dict[str, object], value)


def _object_list(value: object, label: str, maximum: int) -> list[dict[str, object]]:
    if not isinstance(value, list) or len(value) > maximum:
        raise SettingsError(f"{label} must be a bounded list")
    return [_mapping(item, f"{label} item") for item in cast(list[object], value)]


def _reject_unknown(value: dict[str, object], allowed: frozenset[str], label: str) -> None:
    unknown = set(value).difference(allowed)
    if unknown:
        raise SettingsError(f"{label} contains unknown fields: {', '.join(sorted(unknown))}")


def _required_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise SettingsError(f"{label} must be a non-empty string")
    return value


def _optional_string(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _required_string(value, label)


def _optional_integer(value: object, label: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise SettingsError(f"{label} must be an integer")
    return value


def _without_none(value: dict[str, object]) -> dict[str, object]:
    return {key: item for key, item in value.items() if item is not None}


def _require_credential_free(value: str, label: str) -> None:
    if redact_sensitive_text(value) != value:
        raise ValueError(f"{label} must not contain credential-shaped text")


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON constant: {value}")


def _reject_duplicate_json_pairs(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


__all__ = [
    "ConfiguredModelCatalog",
    "JsonSettingsStore",
    "ModelSettings",
    "ProviderSettings",
    "RemoveModel",
    "RemoveProvider",
    "ReplaceSettings",
    "SetDefaultModel",
    "SettingsAction",
    "SettingsController",
    "SettingsError",
    "SettingsReader",
    "SettingsSnapshot",
    "UpsertModel",
    "UpsertProvider",
    "XdshSettings",
    "settings_from_document",
    "settings_revision",
    "settings_to_document",
    "validate_settings_path",
]


def validate_settings_path(path: Path) -> Path:
    """Resolve a settings path while excluding reserved roots and symlinks."""

    expanded = path.expanduser()
    if expanded.is_symlink():
        raise SettingsError("settings path must not be a symlink")
    try:
        resolved = expanded.resolve()
    except OSError as error:
        raise SettingsError("settings path cannot be resolved") from error
    if (
        resolved == Path(resolved.anchor)
        or resolved == Path.home().expanduser().resolve()
        or ".xclaude" in resolved.parts
        or ".dsh" in resolved.parts
    ):
        raise SettingsError("settings path must be a dedicated XDSH file")
    if not resolved.name or len(resolved.name) > 128:
        raise SettingsError("settings filename is invalid")
    return resolved
