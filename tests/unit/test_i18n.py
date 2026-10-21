from __future__ import annotations

import pytest

from xdsh.core.events import (
    ErrorEvent,
    FinishEvent,
    TodoEvent,
    TodoItem,
    ToolStartEvent,
    UsageEvent,
)
from xdsh.core.i18n import (
    SUPPORTED_LANGUAGES,
    canonical_language,
    command_description,
    language_codes,
    language_instruction,
    plan_snapshot_text,
    translate,
    validate_catalogs,
)
from xdsh.telegram.commands import COMMAND_ORDER, DEFAULT_COMMAND_CATALOG
from xdsh.telegram.render import ProgressRenderer, TurnRenderer


@pytest.mark.parametrize("locale", language_codes())
def test_stale_plan_status_is_localized_without_live_progress_claims(locale: str) -> None:
    label = plan_snapshot_text(locale, done=1, total=3, age="70")
    assert "1/3" in label and "70" in label
    assert "Plan sync pending" not in label and "last reported" not in label
    renderer = ProgressRenderer(language=locale)
    renderer.accept(TodoEvent(session_id="s", items=(
        TodoItem(content="A", status="completed"), TodoItem(content="B", status="in_progress"),
        TodoItem(content="C", status="pending"),
    )), elapsed_seconds=0)
    card = renderer.render(final=False, elapsed_seconds=70) or ""
    assert "1/3" in card and "33%" not in card
    assert "🎯" not in card and "⏭️" not in card


@pytest.mark.parametrize("locale", language_codes())
def test_guard_measurements_and_recovery_are_localized(locale: str) -> None:
    from xdsh.core.events import GuardAction, GuardEvent, GuardReason
    from xdsh.core.guard_i18n import guard_text, validate_guard_catalog

    validate_guard_catalog()
    for reason in GuardReason:
        event = GuardEvent(session_id="s", action=GuardAction.STOP, reason=reason,
                           observed=7, limit=6)
        progress = ProgressRenderer(language=locale)
        progress.accept(event)
        card = progress.render(final=True, elapsed_seconds=2) or ""
        assert guard_text(locale, reason.value) in card
        assert guard_text(locale, "stopped") in card
        turn = TurnRenderer(max_answer_chars=1000, language=locale)
        turn.accept(event)
        turn.accept(FinishEvent(session_id="s", reason="guarded"))
        assert guard_text(locale, "retained") in (turn.terminal_notice() or "")


def test_thirteen_language_catalogs_are_complete_and_safe() -> None:
    validate_catalogs()
    from xdsh.core.settings_i18n import effort_label, settings_text, validate_settings_catalog

    validate_settings_catalog()
    from xdsh.core.queue_i18n import queue_text, validate_queue_catalog

    validate_queue_catalog()
    from xdsh.core.approval_i18n import approval_text, validate_approval_catalog

    validate_approval_catalog()
    from xdsh.core.model_i18n import validate_model_catalog

    validate_model_catalog()
    from xdsh.core.session_i18n import validate_session_catalog

    validate_session_catalog()
    from xdsh.core.error_i18n import validate_error_catalog

    validate_error_catalog()
    for locale in language_codes():
        assert approval_text(locale, "approve")
        assert queue_text(locale, "merge")
        assert settings_text(locale, "thinking")
        from xdsh.core.settings_i18n import static_ui_label

        assert settings_text(locale, "diagnostics")
        assert static_ui_label(locale, "Name in a quoted task") == "Name in a quoted task"
        if locale != "en":
            assert static_ui_label(locale, "🩺 XDSH diagnostics") == (
                "🩺 XDSH " + settings_text(locale, "diagnostics")
            )
        assert effort_label(locale, "high")
        assert "model-id" in settings_text(
            locale, "unsupported", model="model-id", command="/thinking",
        )

    assert language_codes() == tuple(item.code for item in SUPPORTED_LANGUAGES)
    assert len(SUPPORTED_LANGUAGES) == 13
    assert {item.code for item in SUPPORTED_LANGUAGES} == {
        "zh-TW",
        "zh-CN",
        "en",
        "ja",
        "ko",
        "es",
        "fr",
        "de",
        "pt-BR",
        "ru",
        "ar",
        "hi",
        "id",
    }
    for language in SUPPORTED_LANGUAGES:
        menu = DEFAULT_COMMAND_CATALOG.telegram_commands(language.code)
        assert tuple(item.command for item in menu) == COMMAND_ORDER
        assert len(menu) == 40
        assert all(1 <= len(item.description) <= 256 for item in menu)
        assert command_description(language.code, "language") in menu[3].description
        assert language_instruction(language.code).strip()
        assert language.native_name in translate(
            language.code,
            "language.saved",
            language=language.native_name,
        )


def test_language_aliases_are_canonical_and_unknown_values_fail_closed() -> None:
    assert canonical_language("zh_hant") == "zh-TW"
    assert canonical_language("zh-CN") == "zh-CN"
    assert canonical_language("EN_us") == "en"
    assert canonical_language("pt") == "pt-BR"
    assert canonical_language("日本語") == "ja"
    assert canonical_language("Bahasa Indonesia") == "id"

    with pytest.raises(ValueError, match="unsupported"):
        canonical_language("not-a-language")
    with pytest.raises(KeyError):
        translate("en", "missing.key")


def test_first_use_navigation_is_native_for_every_supported_language() -> None:
    assert translate("es", "help.title") == "📚 Guía de comandos de XDSH"
    assert translate("ar", "welcome.direct").startswith("أرسل")
    assert translate("hi", "setup.warning", warning="x").startswith("चेतावनी")


def test_task_progress_localizes_heading_without_rewriting_model_text() -> None:
    renderer = ProgressRenderer(language="es")
    renderer.accept(ToolStartEvent(session_id="s", call_id="c", name="bash"))
    renderer.accept(
        TodoEvent(
            session_id="s",
            items=(TodoItem(content="保留原文", status="in_progress"),),
        )
    )
    renderer.accept(UsageEvent(session_id="s", input_tokens=12, output_tokens=3))

    text = renderer.render(final=False, elapsed_seconds=5)

    assert text is not None
    assert "Actualización del progreso" in text
    assert "🧰 Herramientas: 💻 Terminal" in text
    assert "📋 Plan:" in text
    assert "🎯 Actual: 保留原文" in text
    assert "📊 Uso: entrada 12 · salida 3" in text

    turn = TurnRenderer(max_answer_chars=1_000, language="ja")
    turn.accept(FinishEvent(session_id="s", reason="cancelled", cancelled=True))
    assert turn.terminal_notice() == "タスクをキャンセルしました。"


@pytest.mark.parametrize("locale", language_codes())
def test_progress_headings_never_use_another_locales_default(locale: str) -> None:
    from xdsh.core.i18n import progress_heading

    live = progress_heading(locale, final=False)
    summary = progress_heading(locale, final=True)
    assert live and summary and live != summary
    if locale != "en":
        assert live != progress_heading("en", final=False)
        assert summary != progress_heading("en", final=True)
    renderer = ProgressRenderer(language=locale)
    renderer.accept(UsageEvent(session_id="s", input_tokens=12, output_tokens=3))
    assert live in (renderer.render(final=False, elapsed_seconds=5) or "")
    assert summary in (renderer.render(final=True, elapsed_seconds=5) or "")


def test_language_policy_is_explicit_and_does_not_leak_between_calls() -> None:
    english = language_instruction("en")
    japanese = language_instruction("ja")
    assert "Selected response language: en (English)" in english
    assert "Selected response language: ja (日本語)" in japanese
    assert "headings, explanations, checklists" in english
    assert "JSON keys" in english
    assert "source language is not a language-switch request" in english
    assert language_instruction("en") == english
    assert "Selected response language: ja" not in english


@pytest.mark.parametrize("locale", language_codes())
def test_live_card_uses_selected_language_for_its_phase(locale: str) -> None:
    from xdsh.core.i18n import progress_phase

    renderer = ProgressRenderer(language=locale)
    renderer.accept(ToolStartEvent(session_id="s", call_id="c", name="bash"))
    text = renderer.render(final=False, elapsed_seconds=1) or ""
    assert progress_phase(locale, "執行工具") in text
    assert progress_phase(locale, "custom.phase") == "custom.phase"
    if locale not in {"en", "zh-TW", "zh-CN"}:
        assert "Running tools" not in text
        assert "Running:" not in text


@pytest.mark.parametrize("locale", language_codes())
def test_terminal_and_tool_categories_follow_selected_language(locale: str) -> None:
    from xdsh.core.i18n import terminal_text, tool_term

    progress = ProgressRenderer(language=locale)
    progress.accept(ToolStartEvent(session_id="s", call_id="c", name="read"))
    assert tool_term(locale, "Read") in (progress.render(final=False, elapsed_seconds=1) or "")
    turn = TurnRenderer(max_answer_chars=1000, language=locale)
    turn.accept(FinishEvent(session_id="s", reason="cancelled", cancelled=True))
    assert turn.terminal_notice() == terminal_text(locale, "cancelled")
    assert tool_term(locale, "custom_tool_identifier") == "custom_tool_identifier"


@pytest.mark.parametrize("locale", language_codes())
def test_error_message_translates_labels_not_error_codes(locale: str) -> None:
    from xdsh.core.i18n import terminal_text

    turn = TurnRenderer(max_answer_chars=1000, language=locale)
    turn.accept(ErrorEvent(session_id="s", code="CUSTOM_ERROR", message="private upstream body"))
    turn.accept(FinishEvent(session_id="s", reason="error"))
    notice = turn.terminal_notice() or ""
    assert "CUSTOM_ERROR" in notice
    assert "private upstream body" not in notice
    assert terminal_text(locale, "no_partial") in notice
    if locale not in {"en", "zh-TW", "zh-CN"}:
        assert "Execution error" not in notice


@pytest.mark.parametrize("locale", language_codes())
@pytest.mark.parametrize("code", (
    "TIMEOUT", "UPSTREAM_STREAM_INCOMPLETE", "INVALID_REQUEST",
    "ACCEPTANCE_INVALID", "ACCEPTANCE_FAILED",
))
def test_error_recovery_advice_follows_locale(locale: str, code: str) -> None:
    from xdsh.core.error_i18n import recovery_advice

    turn = TurnRenderer(max_answer_chars=1000, language=locale)
    turn.accept(ErrorEvent(session_id="s", code=code, message="upstream private detail"))
    turn.accept(FinishEvent(session_id="s", reason="error"))
    notice = turn.terminal_notice() or ""
    if code == "ACCEPTANCE_FAILED":
        from xdsh.core.verification_i18n import verification_notice

        assert verification_notice(locale, ()) == notice
        categorized = verification_notice(locale, ("parameters", "arrays", "arrays"))
        assert categorized != notice
        assert categorized == verification_notice(locale, ("parameters", "arrays"))
    else:
        assert recovery_advice(locale, code) in notice
        assert code in notice
    assert "upstream private detail" not in notice
    assert recovery_advice(locale, "UNMAPPED_ERROR") == ""
