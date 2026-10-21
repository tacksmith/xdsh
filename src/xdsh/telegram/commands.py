"""Merged slash-command catalog for the Telegram product surface."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from xdsh.core.i18n import command_description, translate
from xdsh.telegram.api import TelegramBotCommand

_COMMAND_NAME = re.compile(r"^[a-z0-9_]{1,32}$")


class CommandOrigin(str, Enum):
    """Authoritative owner of a merged command definition."""

    DSH = "dsh"
    XDSH = "xdsh"
    LEGACY_SHELL = "legacy_shell"


class CommandAction(str, Enum):
    """Small execution seam consumed by TelegramService."""

    WELCOME = "welcome"
    HELP = "help"
    STATUS = "status"
    LANGUAGE = "language"
    ICON = "icon"
    LIMITS = "limits"
    PROCESS = "process"
    QUEUE = "queue"
    BACKGROUND = "background"
    JOBS = "jobs"
    CRON = "cron"
    CANCEL = "cancel"
    NEW_SESSION = "new_session"
    CLEAR_SESSION = "clear_session"
    SESSION = "session"
    SEARCH = "search"
    MODEL = "model"
    REASONING = "reasoning"
    THINKING = "thinking"
    LEARN = "learn"
    MEMORY = "memory"
    DOCTOR = "doctor"
    COMPACT = "compact"
    FEEDBACK = "feedback"
    GOAL = "goal"
    PLAN = "plan"
    PERMISSION = "permission"
    EXPORT = "export"
    PROFILE = "profile"
    FALLBACK = "fallback"
    SKILLS = "skills"
    COST = "cost"
    CONTEXT = "context"
    INIT = "init"
    REVIEW = "review"
    WEB = "web"
    MCP = "mcp"
    BROWSER = "browser"
    MEDIA = "media"
    PLUGINS = "plugins"


@dataclass(frozen=True, slots=True, kw_only=True)
class CommandSpec:
    """One normalized command after source precedence is applied."""

    name: str
    description: str
    origin: CommandOrigin
    action: CommandAction
    compatibility: bool = False

    def __post_init__(self) -> None:
        if _COMMAND_NAME.fullmatch(self.name) is None:
            raise ValueError("command name must use Telegram's lowercase command grammar")
        if not self.description.strip():
            raise ValueError("command description must be non-empty")

    @property
    def executable(self) -> bool:
        return True

    def telegram_command(self, language: str = "zh-TW") -> TelegramBotCommand:
        description = command_description(language, self.name)
        if self.compatibility:
            marker = translate(language, "command.compatibility")
            description = f"↔️ {description} ({marker})"
        return TelegramBotCommand(command=self.name, description=description)


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    name: str
    arguments: str


class CommandCatalog:
    """Deep module that owns source precedence, ordering, and Telegram projection."""

    def __init__(
        self,
        *sources: tuple[CommandSpec, ...],
        order: tuple[str, ...],
    ) -> None:
        merged: dict[str, CommandSpec] = {}
        for source in sources:
            for command in source:
                current = merged.get(command.name)
                if current is None or _origin_priority(command.origin) > _origin_priority(
                    current.origin
                ):
                    merged[command.name] = command
        if set(order) != set(merged):
            missing = set(merged).difference(order)
            extra = set(order).difference(merged)
            raise ValueError(f"command order mismatch: missing={missing}, extra={extra}")
        self._commands = tuple(merged[name] for name in order)
        self._by_name = {command.name: command for command in self._commands}

    @property
    def commands(self) -> tuple[CommandSpec, ...]:
        return self._commands

    def resolve(self, name: str) -> CommandSpec | None:
        return self._by_name.get(name)

    def telegram_commands(self, language: str = "zh-TW") -> tuple[TelegramBotCommand, ...]:
        return tuple(command.telegram_command(language) for command in self._commands)


def parse_command(text: str) -> ParsedCommand | None:
    """Parse Telegram's leading slash token, including an optional bot suffix."""

    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    token, separator, remainder = stripped.partition(" ")
    name = token[1:].split("@", maxsplit=1)[0].lower()
    if _COMMAND_NAME.fullmatch(name) is None:
        return None
    return ParsedCommand(name=name, arguments=remainder.strip() if separator else "")


def _origin_priority(origin: CommandOrigin) -> int:
    return {
        CommandOrigin.LEGACY_SHELL: 1,
        CommandOrigin.XDSH: 2,
        CommandOrigin.DSH: 3,
    }[origin]


DSH_COMMANDS = (
    CommandSpec(
        name="compact",
        description="壓縮對話歷史; 達上下文門檻時也會自動執行",
        origin=CommandOrigin.DSH,
        action=CommandAction.COMPACT,
        compatibility=True,
    ),
    CommandSpec(
        name="feedback",
        description="記錄本次 DSH 會話的意見",
        origin=CommandOrigin.DSH,
        action=CommandAction.FEEDBACK,
        compatibility=True,
    ),
    CommandSpec(
        name="goal",
        description="設定或查看長任務目標",
        origin=CommandOrigin.DSH,
        action=CommandAction.GOAL,
        compatibility=True,
    ),
    CommandSpec(
        name="plan",
        description="進入或離開計畫模式",
        origin=CommandOrigin.DSH,
        action=CommandAction.PLAN,
        compatibility=True,
    ),
    CommandSpec(
        name="permission",
        description="查看有效沙箱與核准策略",
        origin=CommandOrigin.DSH,
        action=CommandAction.PERMISSION,
        compatibility=True,
    ),
    CommandSpec(
        name="export",
        description="匯出目前會話 (原生接口為 Session ZIP)",
        origin=CommandOrigin.DSH,
        action=CommandAction.EXPORT,
        compatibility=True,
    ),
    CommandSpec(
        name="model",
        description="按 Provider 選擇本會話模型",
        origin=CommandOrigin.DSH,
        action=CommandAction.MODEL,
    ),
)


XDSH_COMMANDS = (
    CommandSpec(
        name="process",
        description="選擇獨立過程訊息與進度卡輪換模式",
        origin=CommandOrigin.XDSH,
        action=CommandAction.PROCESS,
    ),
    CommandSpec(
        name="limits",
        description="開關任務資源上限並保留安全與停滯保護",
        origin=CommandOrigin.XDSH,
        action=CommandAction.LIMITS,
    ),
    CommandSpec(
        name="language",
        description="選擇介面與回答語言",
        origin=CommandOrigin.XDSH,
        action=CommandAction.LANGUAGE,
    ),
    CommandSpec(
        name="icon",
        description="設定輸出語義圖標密度與模型自由注入",
        origin=CommandOrigin.XDSH,
        action=CommandAction.ICON,
    ),
    CommandSpec(
        name="queue",
        description="管理排隊、插隊續接、補充合併與可靠回覆",
        origin=CommandOrigin.XDSH,
        action=CommandAction.QUEUE,
    ),
    CommandSpec(
        name="background",
        description="建立不阻塞聊天的持久背景任務",
        origin=CommandOrigin.XDSH,
        action=CommandAction.BACKGROUND,
    ),
    CommandSpec(
        name="jobs",
        description="查看與控制持久任務看板",
        origin=CommandOrigin.XDSH,
        action=CommandAction.JOBS,
    ),
    CommandSpec(
        name="cron",
        description="管理一次、間隔與 Cron 排程",
        origin=CommandOrigin.XDSH,
        action=CommandAction.CRON,
    ),
    CommandSpec(
        name="session",
        description="管理與切換多個會話",
        origin=CommandOrigin.XDSH,
        action=CommandAction.SESSION,
    ),
    CommandSpec(
        name="search",
        description="搜尋目前或全部會話歷史",
        origin=CommandOrigin.XDSH,
        action=CommandAction.SEARCH,
    ),
    CommandSpec(
        name="reasoning",
        description="調整本會話的 LLM 推理強度",
        origin=CommandOrigin.XDSH,
        action=CommandAction.REASONING,
    ),
    CommandSpec(
        name="thinking",
        description="顯示或隱藏結構化 thinking",
        origin=CommandOrigin.XDSH,
        action=CommandAction.THINKING,
    ),
    CommandSpec(
        name="learn",
        description="管理需批准的自主學習",
        origin=CommandOrigin.XDSH,
        action=CommandAction.LEARN,
    ),
    CommandSpec(
        name="web",
        description="使用受限 allowlist 讀取 Web 文字",
        origin=CommandOrigin.XDSH,
        action=CommandAction.WEB,
    ),
    CommandSpec(
        name="mcp",
        description="列出或呼叫已核准的 authenticated MCP 工具",
        origin=CommandOrigin.XDSH,
        action=CommandAction.MCP,
    ),
    CommandSpec(
        name="browser",
        description="透過 authenticated browser gateway 執行受限操作",
        origin=CommandOrigin.XDSH,
        action=CommandAction.BROWSER,
    ),
    CommandSpec(
        name="media",
        description="查看 native-safe 媒體管線限制",
        origin=CommandOrigin.XDSH,
        action=CommandAction.MEDIA,
    ),
    CommandSpec(
        name="plugins",
        description="發現、審批管理與檢視 DSH 原生插件",
        origin=CommandOrigin.XDSH,
        action=CommandAction.PLUGINS,
    ),
)


LEGACY_SHELL_COMMANDS = (
    CommandSpec(
        name="start",
        description="啟動 XDSH 並顯示使用方法",
        origin=CommandOrigin.LEGACY_SHELL,
        action=CommandAction.WELCOME,
    ),
    CommandSpec(
        name="new",
        description="建立並切換到新會話",
        origin=CommandOrigin.LEGACY_SHELL,
        action=CommandAction.NEW_SESSION,
    ),
    CommandSpec(
        name="cancel",
        description="中斷目前執行中的任務",
        origin=CommandOrigin.LEGACY_SHELL,
        action=CommandAction.CANCEL,
    ),
    CommandSpec(
        name="help",
        description="顯示命令與任務說明",
        origin=CommandOrigin.LEGACY_SHELL,
        action=CommandAction.HELP,
    ),
    CommandSpec(
        name="profile",
        description="切換或建立工作 profile",
        origin=CommandOrigin.LEGACY_SHELL,
        action=CommandAction.PROFILE,
    ),
    CommandSpec(
        name="memory",
        description="查看記憶、偏好與有界檢索狀態",
        origin=CommandOrigin.LEGACY_SHELL,
        action=CommandAction.MEMORY,
    ),
    CommandSpec(
        name="model",
        description="查看或切換後端模型",
        origin=CommandOrigin.LEGACY_SHELL,
        action=CommandAction.MODEL,
    ),
    CommandSpec(
        name="fallback",
        description="查看或設定備用模型",
        origin=CommandOrigin.LEGACY_SHELL,
        action=CommandAction.FALLBACK,
    ),
    CommandSpec(
        name="skills",
        description="查看內置與專案 Skills",
        origin=CommandOrigin.LEGACY_SHELL,
        action=CommandAction.SKILLS,
    ),
    CommandSpec(
        name="compact",
        description="壓縮目前會話上下文",
        origin=CommandOrigin.LEGACY_SHELL,
        action=CommandAction.COMPACT,
    ),
    CommandSpec(
        name="clear",
        description="清空目前會話的 DSH 上下文",
        origin=CommandOrigin.LEGACY_SHELL,
        action=CommandAction.CLEAR_SESSION,
    ),
    CommandSpec(
        name="cost",
        description="查看本會話用量與花費",
        origin=CommandOrigin.LEGACY_SHELL,
        action=CommandAction.COST,
    ),
    CommandSpec(
        name="context",
        description="查看上下文占用情況",
        origin=CommandOrigin.LEGACY_SHELL,
        action=CommandAction.CONTEXT,
    ),
    CommandSpec(
        name="status",
        description="查看目前任務狀態",
        origin=CommandOrigin.LEGACY_SHELL,
        action=CommandAction.STATUS,
    ),
    CommandSpec(
        name="doctor",
        description="執行 XDSH 環境自檢",
        origin=CommandOrigin.LEGACY_SHELL,
        action=CommandAction.DOCTOR,
    ),
    CommandSpec(
        name="init",
        description="產生專案代理說明檔",
        origin=CommandOrigin.LEGACY_SHELL,
        action=CommandAction.INIT,
    ),
    CommandSpec(
        name="review",
        description="執行程式碼審查",
        origin=CommandOrigin.LEGACY_SHELL,
        action=CommandAction.REVIEW,
    ),
    CommandSpec(
        name="export",
        description="匯出目前會話",
        origin=CommandOrigin.LEGACY_SHELL,
        action=CommandAction.EXPORT,
    ),
)


COMMAND_ORDER = (
    "start",
    "help",
    "status",
    "language",
    "icon",
    "limits",
    "process",
    "queue",
    "background",
    "jobs",
    "cron",
    "cancel",
    "new",
    "session",
    "search",
    "clear",
    "model",
    "reasoning",
    "thinking",
    "learn",
    "web",
    "mcp",
    "browser",
    "media",
    "plugins",
    "compact",
    "feedback",
    "goal",
    "plan",
    "permission",
    "export",
    "profile",
    "memory",
    "fallback",
    "skills",
    "cost",
    "context",
    "doctor",
    "init",
    "review",
)

DEFAULT_COMMAND_CATALOG = CommandCatalog(
    LEGACY_SHELL_COMMANDS,
    XDSH_COMMANDS,
    DSH_COMMANDS,
    order=COMMAND_ORDER,
)
