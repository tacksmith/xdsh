from __future__ import annotations

import json
from pathlib import Path

import pytest

from xdsh.telegram.sessions import SessionRegistryError, TelegramSessionRegistry


def test_default_registry_holds_500_sessions_and_preserves_them_on_reload(tmp_path: Path) -> None:
    registry = TelegramSessionRegistry(tmp_path, default_model="model")
    first = registry.current(42)
    for index in range(1, 500):
        registry.create(42, f"session-{index}")
    assert len(registry.list_sessions(42)) == 500
    reloaded = TelegramSessionRegistry(tmp_path, default_model="model")
    assert len(reloaded.list_sessions(42)) == 500
    assert reloaded.resolve(42, first.key) == first
    with pytest.raises(SessionRegistryError, match="500"):
        reloaded.create(42, "over-limit")
    assert len(reloaded.list_sessions(43)) == 1


@pytest.mark.parametrize("limit", [0, 501, True, 2.5])
def test_invalid_session_cap_is_rejected(tmp_path: Path, limit: int) -> None:
    with pytest.raises(SessionRegistryError):
        TelegramSessionRegistry(tmp_path, default_model="model", max_sessions=limit)


class KeyFactory:
    def __init__(self, *keys: str) -> None:
        self._keys = iter(keys)

    def __call__(self) -> str:
        return next(self._keys)


def test_registry_preserves_legacy_session_and_model_then_manages_multiple_sessions(
    tmp_path: Path,
) -> None:
    root = tmp_path / "telegram"
    legacy_models = root / "chat-models"
    legacy_models.mkdir(parents=True)
    (legacy_models / "42.model").write_text("legacy-model\n", encoding="ascii")
    registry = TelegramSessionRegistry(
        root,
        default_model="default-model",
        key_factory=KeyFactory("research1"),
    )

    main = registry.current(42)
    research = registry.create(42, "Research")
    registry.select_model(42, "research-model")
    registry.select_fallback(42, "fallback-model")
    registry.select_reasoning(42, "high")
    registry.set_show_thinking(42, True)
    selected_main = registry.switch(42, "main")
    renamed = registry.rename(42, "research1", "Analysis")

    assert main.key == "main"
    assert main.runner_session_id == "telegram-chat-42"
    assert main.model == "legacy-model"
    assert main.reasoning_effort == "off"
    assert main.show_thinking is False
    assert research.runner_session_id == "telegram-chat-42-s-research1"
    assert registry.current(42) == selected_main
    assert renamed.name == "Analysis"
    assert renamed.model == "research-model"
    assert renamed.fallback_model == "fallback-model"
    assert renamed.reasoning_effort == "high"
    assert renamed.show_thinking is True
    assert [session.name for session in registry.list_sessions(42)] == ["Main", "Analysis"]
    assert (root / "chat-sessions" / "42.json").stat().st_mode & 0o777 == 0o600

    reloaded = TelegramSessionRegistry(root, default_model="different-default")
    assert reloaded.current(42).model == "legacy-model"
    reloaded_research = reloaded.switch(42, "Analysis")
    assert reloaded_research.model == "research-model"
    assert reloaded_research.fallback_model == "fallback-model"
    assert reloaded_research.reasoning_effort == "high"
    assert reloaded_research.show_thinking is True


def test_registry_migrates_v1_state_and_new_sessions_inherit_reasoning(
    tmp_path: Path,
) -> None:
    root = tmp_path / "telegram"
    state_path = root / "chat-sessions" / "42.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "active": "main",
                "sessions": [
                    {
                        "key": "main",
                        "name": "Main",
                        "runner_session_id": "telegram-chat-42",
                        "model": "legacy-model",
                    }
                ],
            }
        ),
        encoding="ascii",
    )
    registry = TelegramSessionRegistry(
        root,
        default_model="default-model",
        key_factory=KeyFactory("second01"),
    )

    migrated = registry.current(42)
    registry.select_reasoning(42, "medium")
    registry.set_show_thinking(42, True)
    inherited = registry.create(42, "Second")

    assert migrated.reasoning_effort == "off"
    assert migrated.show_thinking is False
    assert inherited.reasoning_effort == "medium"
    assert inherited.show_thinking is True
    payload = json.loads(state_path.read_text(encoding="ascii"))
    assert payload["version"] == 3
    assert payload["sessions"][0]["reasoning_effort"] == "medium"
    assert payload["sessions"][0]["show_thinking"] is True
    assert payload["sessions"][0]["fallback_model"] is None


def test_registry_delete_preserves_at_least_one_session_and_rejects_bad_state(
    tmp_path: Path,
) -> None:
    root = tmp_path / "telegram"
    registry = TelegramSessionRegistry(
        root,
        default_model="default-model",
        key_factory=KeyFactory("second01"),
    )
    registry.create(42, "Second")

    deletion = registry.delete(42, "Second")

    assert deletion.deleted.name == "Second"
    assert deletion.current.key == "main"
    with pytest.raises(SessionRegistryError, match="最後一個"):
        registry.delete(42, "main")

    state_path = root / "chat-sessions" / "7.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"version": 99}), encoding="ascii")
    with pytest.raises(SessionRegistryError, match="版本"):
        registry.current(7)


def test_registry_rejects_duplicate_names_and_unsafe_selectors(tmp_path: Path) -> None:
    registry = TelegramSessionRegistry(
        tmp_path / "telegram",
        default_model="default-model",
        key_factory=KeyFactory("second01"),
    )
    registry.create(42, "Research")

    with pytest.raises(SessionRegistryError, match="已存在"):
        registry.create(42, "research")
    with pytest.raises(SessionRegistryError, match="不存在"):
        registry.switch(42, "../outside")
    with pytest.raises(SessionRegistryError, match="名稱"):
        registry.rename(42, "Research", "bad\nname")


def test_registry_branch_inherits_configuration_and_persists_source(
    tmp_path: Path,
) -> None:
    root = tmp_path / "telegram"
    registry = TelegramSessionRegistry(
        root,
        default_model="default-model",
        key_factory=KeyFactory("branch01"),
    )
    registry.select_model(42, "research-model")
    registry.select_fallback(42, "fallback-model")
    registry.select_reasoning(42, "high")
    registry.set_show_thinking(42, True)

    branch = registry.branch(42, "Experiment")
    reloaded = TelegramSessionRegistry(root, default_model="different-model")
    persisted = reloaded.current(42)

    assert branch.runner_session_id == "telegram-chat-42-s-branch01"
    assert branch.branch_source_session_id == "telegram-chat-42"
    assert branch.model == "research-model"
    assert branch.fallback_model == "fallback-model"
    assert branch.reasoning_effort == "high"
    assert branch.show_thinking is True
    assert persisted == branch
    payload = json.loads((root / "chat-sessions" / "42.json").read_text("ascii"))
    assert payload["version"] == 3
    assert payload["sessions"][1]["branch_source_session_id"] == "telegram-chat-42"
