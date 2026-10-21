from pathlib import Path

import pytest

from xdsh.core.provider_presets import NATIVE_PROVIDER_PRESETS, PROVIDER_PRESETS, ProviderPreset
from xdsh.core.provider_ui_i18n import provider_ui
from xdsh.core.providers import ProviderRegistry
from xdsh.telegram.provider_setup import TelegramProviderSetup


def test_all_native_presets_are_paginated_without_registering_routes(tmp_path: Path) -> None:
    registry = ProviderRegistry(tmp_path / "providers")
    setup = TelegramProviderSetup(registry)
    assert len(NATIVE_PROVIDER_PRESETS) == 37
    found = set()
    for page in range(5):
        reply = setup.catalog(page)
        assert reply.markup is not None
        for row in reply.markup.rows:
            for button in row:
                assert len(button.callback_data.encode()) <= 64
                if button.callback_data.startswith("model.preset."):
                    found.add(button.callback_data.removeprefix("model.preset."))
    assert found == {item.id for item in PROVIDER_PRESETS}
    assert registry.connections() == ()


@pytest.mark.parametrize("preset", PROVIDER_PRESETS, ids=lambda p: p.id)
def test_preset_confirmation_is_explicit_and_preserves_exact_model(
    tmp_path: Path,
    preset: ProviderPreset,
) -> None:
    registry = ProviderRegistry(tmp_path / "providers")
    setup = TelegramProviderSetup(registry)
    reply = setup.start(7, 7, preset.id)
    if not preset.engine:
        assert provider_ui("en", "unsupported") in reply.text
        assert registry.connections() == ()
        assert setup.consume(7, 7, "not-an-api-key") is None
        return
    if not preset.url:
        setup.consume(7, 7, "https://example.com/v1")
    setup.consume(7, 7, "exact-model-id")
    confirmation = setup.consume(7, 7, "env:EXAMPLE_API_KEY")
    assert confirmation is not None and confirmation.markup is not None
    assert registry.connections() == ()
    callback = confirmation.markup.rows[0][0].callback_data
    assert setup.decide(7, 8, callback) is None
    assert setup.decide(7, 7, callback) is not None
    assert setup.decide(7, 7, callback) is None
    (route,) = ProviderRegistry(tmp_path / "providers").connections()
    assert route.provider == preset.id
    assert route.model == "exact-model-id"
    assert route.engine == preset.engine
    assert route.credential_env == "EXAMPLE_API_KEY"
