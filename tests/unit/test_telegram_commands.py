from xdsh.telegram.commands import (
    COMMAND_ORDER,
    DEFAULT_COMMAND_CATALOG,
    CommandAction,
    CommandOrigin,
    parse_command,
)


def test_catalog_contains_complete_static_union_with_dsh_precedence() -> None:
    commands = DEFAULT_COMMAND_CATALOG.commands

    assert len(commands) == 40
    assert tuple(command.name for command in commands) == COMMAND_ORDER
    assert len({command.name for command in commands}) == len(commands)
    for name in ("compact", "export", "model"):
        command = DEFAULT_COMMAND_CATALOG.resolve(name)
        assert command is not None
        assert command.origin is CommandOrigin.DSH
    start = DEFAULT_COMMAND_CATALOG.resolve("start")
    assert start is not None
    assert start.origin is CommandOrigin.LEGACY_SHELL


def test_menu_marks_host_compatibility_without_hiding_commands() -> None:
    menu = {
        command.command: command.description
        for command in DEFAULT_COMMAND_CATALOG.telegram_commands()
    }

    assert len(menu) == 40
    for name in ("compact", "feedback", "goal", "plan", "permission", "export"):
        assert "XDSH 相容" in menu[name]
    for name in (
        "model",
        "profile",
        "reasoning",
        "thinking",
        "learn",
        "memory",
        "search",
        "queue",
        "background",
        "jobs",
        "cron",
        "clear",
    ):
        assert "XDSH 相容" not in menu[name]


def test_all_command_actions_are_executable_with_explicit_host_compatibility() -> None:
    model = DEFAULT_COMMAND_CATALOG.resolve("model")
    session = DEFAULT_COMMAND_CATALOG.resolve("session")
    search = DEFAULT_COMMAND_CATALOG.resolve("search")
    reasoning = DEFAULT_COMMAND_CATALOG.resolve("reasoning")
    thinking = DEFAULT_COMMAND_CATALOG.resolve("thinking")
    learn = DEFAULT_COMMAND_CATALOG.resolve("learn")
    memory = DEFAULT_COMMAND_CATALOG.resolve("memory")
    compact = DEFAULT_COMMAND_CATALOG.resolve("compact")
    profile = DEFAULT_COMMAND_CATALOG.resolve("profile")
    background = DEFAULT_COMMAND_CATALOG.resolve("background")
    jobs = DEFAULT_COMMAND_CATALOG.resolve("jobs")
    cron = DEFAULT_COMMAND_CATALOG.resolve("cron")
    web = DEFAULT_COMMAND_CATALOG.resolve("web")
    mcp = DEFAULT_COMMAND_CATALOG.resolve("mcp")
    browser = DEFAULT_COMMAND_CATALOG.resolve("browser")
    media = DEFAULT_COMMAND_CATALOG.resolve("media")
    plugins = DEFAULT_COMMAND_CATALOG.resolve("plugins")
    language = DEFAULT_COMMAND_CATALOG.resolve("language")

    assert model is not None and model.action is CommandAction.MODEL and model.executable
    assert session is not None and session.action is CommandAction.SESSION and session.executable
    assert search is not None and search.action is CommandAction.SEARCH and search.executable
    assert (
        reasoning is not None
        and reasoning.action is CommandAction.REASONING
        and reasoning.executable
    )
    assert (
        thinking is not None and thinking.action is CommandAction.THINKING and thinking.executable
    )
    assert learn is not None and learn.action is CommandAction.LEARN and learn.executable
    assert memory is not None and memory.action is CommandAction.MEMORY and memory.executable
    assert compact is not None and compact.action is CommandAction.COMPACT
    assert compact.compatibility and compact.executable
    assert profile is not None and profile.action is CommandAction.PROFILE
    assert profile.executable and not profile.compatibility
    assert background is not None and background.action is CommandAction.BACKGROUND
    assert jobs is not None and jobs.action is CommandAction.JOBS
    assert cron is not None and cron.action is CommandAction.CRON
    assert web is not None and web.action is CommandAction.WEB
    assert mcp is not None and mcp.action is CommandAction.MCP
    assert browser is not None and browser.action is CommandAction.BROWSER
    assert media is not None and media.action is CommandAction.MEDIA
    assert plugins is not None and plugins.action is CommandAction.PLUGINS
    assert language is not None and language.action is CommandAction.LANGUAGE
    assert all(command.executable for command in DEFAULT_COMMAND_CATALOG.commands)
    assert {command.action for command in DEFAULT_COMMAND_CATALOG.commands} == set(CommandAction)


def test_parse_command_supports_bot_suffix_and_arguments() -> None:
    parsed = parse_command(" /goal@xdsh_3090_bot edit durable objective ")

    assert parsed is not None
    assert parsed.name == "goal"
    assert parsed.arguments == "edit durable objective"
    assert parse_command("normal task") is None
    assert parse_command("/invalid-name") is None
