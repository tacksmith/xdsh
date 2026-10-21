from __future__ import annotations

import stat
from pathlib import Path

import pytest

from xdsh.core.models import (
    FixedModelCatalog,
    ModelAvailability,
    ModelCatalogError,
    ModelDirectory,
    ModelProvider,
)
from xdsh.core.settings import (
    ConfiguredModelCatalog,
    JsonSettingsStore,
    ModelSettings,
    ProviderSettings,
    SettingsError,
    XdshSettings,
)


def test_settings_store_round_trips_non_secret_model_preferences(tmp_path: Path) -> None:
    path = tmp_path / "config" / "settings.json"
    store = JsonSettingsStore(path)
    settings = XdshSettings(
        default_model="team-code-model",
        providers=(
            ProviderSettings(id="team-cloud", label="Team Cloud", icon="🛰️"),
        ),
        models=(
            ModelSettings(
                id="team-code-model",
                provider="team-cloud",
                reasoning=True,
                context_window_tokens=200_000,
                max_output_tokens=16_384,
                thinking=True,
                tools=True,
                vision=True,
                audio=False,
                structured_output=True,
            ),
        ),
    )

    store.save(settings)

    assert store.load() == settings
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "API_KEY" not in path.read_text(encoding="utf-8")
    profile = settings.models[0]
    assert profile.thinking is True
    assert profile.tools is True
    assert profile.vision is True
    assert profile.audio is False
    assert profile.structured_output is True


async def test_operator_labels_cannot_override_gateway_route_identity(tmp_path: Path) -> None:
    store = JsonSettingsStore(tmp_path / "settings.json")
    store.save(XdshSettings(models=(ModelSettings(id="remote-alias", provider="cloud"),)))
    catalog = ConfiguredModelCatalog(
        FixedModelCatalog(ModelDirectory(
            (ModelProvider("local", ("local-model",)),), route_names=("local-model", "*"),
        )),
        store, include_unlisted_models=True,
    )
    directory = await catalog.snapshot()
    assert directory.health_for("remote-alias").availability is ModelAvailability.UNAVAILABLE


def test_settings_store_rejects_unknown_or_symlink_documents(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text('{"schema_version":1,"api_key":"must-not-be-here"}', encoding="utf-8")
    with pytest.raises(SettingsError, match="unknown"):
        JsonSettingsStore(path).load()

    target = tmp_path / "target.json"
    target.write_text('{"schema_version":1}', encoding="utf-8")
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(SettingsError, match="symlink"):
        JsonSettingsStore(path).load()


@pytest.mark.parametrize(
    "payload",
    (
        '{"schema_version":1,"schema_version":1}',
        '{"schema_version":1,"models":[{"id":"a","id":"b"}]}',
        '{"schema_version":1,"models":[{"id":"a","context_window_tokens":NaN}]}',
    ),
)
def test_settings_store_rejects_ambiguous_or_non_finite_json(
    tmp_path: Path,
    payload: str,
) -> None:
    path = tmp_path / "settings.json"
    path.write_text(payload, encoding="utf-8")

    with pytest.raises(SettingsError, match="valid UTF-8 JSON"):
        JsonSettingsStore(path).load()


def test_settings_values_reject_credential_shaped_text() -> None:
    with pytest.raises(ValueError, match="credential-shaped"):
        ProviderSettings(
            id="provider",
            label="TOKEN=super-secret-value",
        )
    with pytest.raises(ValueError, match="credential-shaped"):
        ModelSettings(id="123456789:" + "abcdefghijklmnopqrstuvwxyzABCDE")


@pytest.mark.asyncio
async def test_configured_catalog_only_decorates_live_litellm_models(tmp_path: Path) -> None:
    live = FixedModelCatalog(
        ModelDirectory(
            (
                ModelProvider("anthropic", ("team-code-model", "plain-model")),
            )
        )
    )
    store = JsonSettingsStore(tmp_path / "settings.json")
    store.save(
        XdshSettings(
            providers=(ProviderSettings("team-cloud", "Team Cloud", "🛰️"),),
            models=(
                ModelSettings(
                    "team-code-model",
                    provider="team-cloud",
                    reasoning=True,
                    context_window_tokens=200_000,
                ),
                ModelSettings("not-live", provider="team-cloud", reasoning=False),
            ),
        )
    )

    directory = await ConfiguredModelCatalog(live, store).snapshot()

    assert directory.models == ("plain-model", "team-code-model")
    assert [(item.id, item.models) for item in directory.providers] == [
        ("anthropic", ("plain-model",)),
        ("team-cloud", ("team-code-model",)),
    ]
    team = directory.provider_for("team-code-model")
    assert team is not None
    assert team.display_label == "🛰️ Team Cloud · team-cloud"
    profile = directory.profile_for("team-code-model")
    assert profile.reasoning is True
    assert profile.context_window_tokens == 200_000


@pytest.mark.asyncio
async def test_configured_catalog_can_expose_explicit_unlisted_routes_when_opted_in(
    tmp_path: Path,
) -> None:
    """Configured aliases remain selectable when LiteLLM omits them from metadata."""

    live = FixedModelCatalog(
        ModelDirectory((ModelProvider("live", ("live-model",)),))
    )
    store = JsonSettingsStore(tmp_path / "settings.json")
    store.save(
        XdshSettings(
            default_model="ark-code-latest",
            providers=(ProviderSettings("volc-coding", "火山引擎 ARK", "🔥"),),
            models=(
                ModelSettings(
                    "ark-code-latest",
                    provider="volc-coding",
                    reasoning=True,
                    tools=True,
                ),
                ModelSettings("team-route", reasoning=False),
            ),
        )
    )

    directory = await ConfiguredModelCatalog(
        live,
        store,
        include_unlisted_models=True,
    ).snapshot()

    assert directory.models == ("ark-code-latest", "live-model", "team-route")
    assert [(item.id, item.models) for item in directory.providers] == [
        ("configured", ("team-route",)),
        ("live", ("live-model",)),
        ("volc-coding", ("ark-code-latest",)),
    ]
    assert directory.health_for("ark-code-latest").availability.value == "unknown"
    assert directory.profile_for("ark-code-latest").reasoning is True
    assert directory.profile_for("team-route").reasoning is False


@pytest.mark.asyncio
async def test_configured_catalog_keeps_unlisted_routes_hidden_by_default(
    tmp_path: Path,
) -> None:
    live = FixedModelCatalog.from_models("live-model")
    store = JsonSettingsStore(tmp_path / "settings.json")
    store.save(XdshSettings(default_model="configured-default"))

    directory = await ConfiguredModelCatalog(live, store).snapshot()

    assert directory.models == ("live-model",)


@pytest.mark.asyncio
async def test_invalid_settings_are_normalized_to_catalog_error_for_transports(
    tmp_path: Path,
) -> None:
    path = tmp_path / "settings.json"
    path.write_text('{"schema_version":1,"unknown":true}', encoding="utf-8")

    with pytest.raises(ModelCatalogError, match="settings are invalid"):
        await ConfiguredModelCatalog(
            FixedModelCatalog.from_models("live-model"),
            JsonSettingsStore(path),
        ).snapshot()
