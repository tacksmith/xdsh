from __future__ import annotations

import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import pytest

from xdsh.core.settings import (
    JsonSettingsStore,
    ModelSettings,
    ProviderSettings,
    RemoveModel,
    RemoveProvider,
    SetDefaultModel,
    SettingsController,
    SettingsError,
    UpsertModel,
    UpsertProvider,
)
from xdsh.ops.config_ui import (
    _APP_JS,
    _INDEX_HTML,
    ConfigUiError,
    ConfigUiPolicy,
    ConfigUiRequestError,
    LoopbackConfigUiServer,
    _decode_json,
    main,
)


def test_settings_controller_applies_typed_actions_with_revision_guard(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config" / "settings.json"
    controller = SettingsController(JsonSettingsStore(path))
    initial = controller.snapshot()

    provider = controller.apply(
        UpsertProvider(ProviderSettings("volc", "Volcengine", "🔥")),
        expected_revision=initial.revision,
    )
    model = controller.apply(
        UpsertModel(
            ModelSettings(
                "ark-code-latest",
                provider="volc",
                reasoning=True,
                tools=True,
            )
        ),
        expected_revision=provider.revision,
    )
    selected = controller.apply(
        SetDefaultModel("ark-code-latest"),
        expected_revision=model.revision,
    )

    assert selected.settings.default_model == "ark-code-latest"
    assert selected.settings.providers[0].label == "Volcengine"
    assert selected.settings.models[0].tools is True
    assert path.stat().st_mode & 0o077 == 0
    with pytest.raises(SettingsError, match="reload"):
        controller.apply(SetDefaultModel(None), expected_revision=initial.revision)


def test_settings_controller_refuses_dangling_removals(tmp_path: Path) -> None:
    controller = SettingsController(JsonSettingsStore(tmp_path / "settings.json"))
    snapshot = controller.snapshot()
    snapshot = controller.apply(
        UpsertProvider(ProviderSettings("local")),
        expected_revision=snapshot.revision,
    )
    snapshot = controller.apply(
        UpsertModel(ModelSettings("local/model", provider="local")),
        expected_revision=snapshot.revision,
    )
    snapshot = controller.apply(
        SetDefaultModel("local/model"),
        expected_revision=snapshot.revision,
    )

    with pytest.raises(SettingsError, match="referenced"):
        controller.apply(RemoveProvider("local"), expected_revision=snapshot.revision)
    with pytest.raises(SettingsError, match="default model"):
        controller.apply(RemoveModel("local/model"), expected_revision=snapshot.revision)


def test_config_ui_policy_requires_loopback_origin_and_strong_bearer() -> None:
    token = "u" * 32
    policy = ConfigUiPolicy(
        port=8877,
        token_env="UI_TOKEN",
        environment={"UI_TOKEN": token},
        max_body_bytes=4_096,
    )
    with pytest.raises(ConfigUiRequestError) as foreign_host:
        policy.authorize(host="example.test", origin=None, headers={})
    assert foreign_host.value.status == 403
    with pytest.raises(ConfigUiRequestError) as unauthorized:
        policy.authorize(host="127.0.0.1:8877", origin=None, headers={})
    assert unauthorized.value.status == 401
    assert token not in str(unauthorized.value)
    with pytest.raises(ConfigUiRequestError) as foreign_origin:
        policy.authorize(
            host="127.0.0.1:8877",
            origin="http://127.0.0.1:8878",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert foreign_origin.value.status == 403

    policy.authorize(
        host="localhost:8877",
        origin="http://localhost:8877/",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert (
        policy.content_length(
            {"Content-Type": "application/json", "Content-Length": "4096"}
        )
        == 4_096
    )
    with pytest.raises(ConfigUiRequestError) as oversized:
        policy.content_length(
            {"Content-Type": "application/json", "Content-Length": "4097"}
        )
    assert oversized.value.status == 413

    with pytest.raises(ConfigUiError, match="24-512"):
        ConfigUiPolicy(port=8877, token_env="UI_TOKEN", environment={"UI_TOKEN": "short"})


@pytest.mark.parametrize(
    "payload",
    [
        b'{"schema_version":1,"schema_version":1}',
        b'{"schema_version":NaN}',
        b"\xff",
    ],
)
def test_config_ui_json_decoder_rejects_ambiguous_documents(payload: bytes) -> None:
    with pytest.raises(ConfigUiRequestError) as rejected:
        _decode_json(payload)
    assert rejected.value.status == 422


def test_config_cli_manages_settings_without_credentials(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "settings.json"
    assert main(["--settings", str(path), "provider-set", "volc", "--label", "ARK"]) == 0
    assert (
        main(
            [
                "--settings",
                str(path),
                "model-set",
                "ark-code-latest",
                "--provider",
                "volc",
                "--reasoning",
                "on",
                "--tools",
                "on",
            ]
        )
        == 0
    )
    assert main(["--settings", str(path), "set-default", "--model", "ark-code-latest"]) == 0
    assert main(["--settings", str(path), "validate"]) == 0

    rendered = capsys.readouterr()
    assert "ark-code-latest" in rendered.out
    assert "settings valid" in rendered.out
    assert rendered.err == ""
    saved = JsonSettingsStore(path).load()
    assert saved.default_model == "ark-code-latest"
    assert saved.models[0].reasoning is True


def test_config_ui_assets_use_external_script_and_safe_dom_writes() -> None:
    html = _INDEX_HTML.decode()
    script = _APP_JS.decode()
    assert '<script src="/app.js"></script>' in html
    assert "innerHTML" not in script
    assert "localStorage" not in script
    assert "sessionStorage" not in script


def test_loopback_config_ui_round_trip(tmp_path: Path) -> None:
    token = "v" * 32
    controller = SettingsController(JsonSettingsStore(tmp_path / "settings.json"))
    try:
        server = LoopbackConfigUiServer(
            controller,
            port=0,
            token_env="UI_TOKEN",
            environment={"UI_TOKEN": token},
        )
    except OSError as error:
        pytest.skip(f"loopback listener unavailable: {error}")
    opener = build_opener(ProxyHandler({}))
    base = f"http://127.0.0.1:{server.port}"
    server.start()
    try:
        root = opener.open(base + "/", timeout=2)
        assert "default-src 'none'" in root.headers["Content-Security-Policy"]
        with pytest.raises(HTTPError) as unauthorized:
            opener.open(base + "/api/settings", timeout=2)
        assert unauthorized.value.code == 401

        response = opener.open(
            Request(
                base + "/api/settings",
                headers={"Authorization": f"Bearer {token}"},
            ),
            timeout=2,
        )
        snapshot = json.loads(response.read())
        revision = snapshot["revision"]
        payload = json.dumps(
            {
                "schema_version": 1,
                "default_model": "ark-code-latest",
                "providers": [],
                "models": [],
            }
        ).encode()
        updated = opener.open(
            Request(
                base + "/api/settings",
                method="PUT",
                data=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Origin": base,
                    "X-XDSH-Revision": revision,
                },
            ),
            timeout=2,
        )
        document = json.loads(updated.read())
        assert document["settings"]["default_model"] == "ark-code-latest"
        assert document["revision"] != revision
    finally:
        server.close()

