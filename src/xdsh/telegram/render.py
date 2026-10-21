"""Runner-event rendering for Telegram's plain-text message surface."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from xdsh.core.error_i18n import recovery_advice
from xdsh.core.events import (
    ActivityEvent,
    ArtifactEvent,
    CompactionAction,
    CompactionEvent,
    ErrorEvent,
    FinishEvent,
    GuardAction,
    GuardEvent,
    GuardReason,
    ReasoningEvent,
    RetryEvent,
    RunnerEventT,
    RuntimeEvent,
    SubagentAction,
    SubagentEvent,
    TextEvent,
    TodoEvent,
    TodoItem,
    ToolResultEvent,
    ToolStartEvent,
    UsageEvent,
    VerificationEvent,
)
from xdsh.core.guard_i18n import guard_text
from xdsh.core.i18n import (
    canonical_language,
    plan_snapshot_text,
    progress_heading,
    progress_label,
    progress_phase,
    terminal_text,
    tool_term,
)
from xdsh.core.icons import (
    IconIntensity,
    canonical_icon_intensity,
    icon_catalog,
    semantic_icon,
)
from xdsh.core.presentation import (
    safe_error_code,
    terminal_error_notice,
)
from xdsh.core.product_control import safe_outbound_text

TELEGRAM_TEXT_CHUNK = 3_800
TELEGRAM_RICH_TEXT_CHUNK = 30_000
PROGRESS_OUTPUT_ITEMS = 3
PROGRESS_OUTPUT_CHARS = 320
PROGRESS_PENDING_CHARS = 2_000
PROGRESS_TOOL_TYPES = 8
PROGRESS_BAR_CELLS = 14
LONG_CONTEXT_WARNING_TOKENS = 64_000


_TOOL_PRESENTATIONS = {
    "bash": f"{semantic_icon('tool.terminal')} 終端",
    "shell": f"{semantic_icon('tool.terminal')} 終端",
    "read": f"{semantic_icon('tool.read')} 讀取",
    "read_file": f"{semantic_icon('tool.read')} 讀取",
    "str_replace_editor": f"{semantic_icon('tool.edit')} 編輯",
    "str_replace_based_edit_tool": f"{semantic_icon('tool.edit')} 編輯",
    "edit": f"{semantic_icon('tool.edit')} 編輯",
    "apply_patch": f"{semantic_icon('tool.edit')} 編輯",
    "write": f"{semantic_icon('tool.write')} 寫入",
    "write_file": f"{semantic_icon('tool.write')} 寫入",
    "create_file": f"{semantic_icon('tool.write')} 寫入",
    "glob": f"{semantic_icon('tool.files')} 檔案搜尋",
    "find_files": f"{semantic_icon('tool.files')} 檔案搜尋",
    "grep": f"{semantic_icon('tool.search')} 內容搜尋",
    "search": f"{semantic_icon('tool.search')} 搜尋",
    "todo": f"{semantic_icon('tool.todo')} 待辦",
    "todo_write": f"{semantic_icon('tool.todo')} 待辦",
    "web_search": f"{semantic_icon('tool.web')} 網路搜尋",
    "web_fetch": f"{semantic_icon('tool.fetch')} 讀取網頁",
    "fetch": f"{semantic_icon('tool.fetch')} 讀取網頁",
    "browser": f"{semantic_icon('tool.browser')} 瀏覽器",
    "task": f"{semantic_icon('tool.subagent')} 子代理",
    "subagent": f"{semantic_icon('tool.subagent')} 子代理",
    "agent": f"{semantic_icon('tool.subagent')} 子代理",
    "notebook": "📓 Notebook",
    "image": f"{semantic_icon('tool.image')} 圖像",
    "memory": f"{semantic_icon('tool.memory')} 記憶",
    "skill": f"{semantic_icon('tool.skill')} 技能",
    "mcp": f"{semantic_icon('tool.mcp')} MCP",
}
_TOOL_PREFIX_PRESENTATIONS = (
    ("mcp__", "🔌 MCP"),
    ("mcp.", "🔌 MCP"),
    ("browser_", "🌐 瀏覽器"),
    ("web_", "🌐 網路"),
    ("image_", "🖼️ 圖像"),
    ("todo_", "✅ 待辦"),
    ("task_", "🤖 子代理"),
)
_TOOL_CANONICAL_KEYS = {
    "bash": "bash",
    "shell": "bash",
    "read": "read",
    "read_file": "read",
    "str_replace_editor": "edit",
    "str_replace_based_edit_tool": "edit",
    "edit": "edit",
    "apply_patch": "edit",
    "write": "write",
    "write_file": "write",
    "create_file": "write",
    "glob": "glob",
    "find_files": "glob",
    "grep": "grep",
    "search": "search",
    "todo": "todo",
    "todo_write": "todo",
    "web_search": "web_search",
    "web_fetch": "web_fetch",
    "fetch": "fetch",
    "browser": "browser",
    "task": "task",
    "subagent": "task",
    "agent": "task",
    "notebook": "notebook",
    "image": "image",
    "memory": "memory",
    "skill": "skill",
}
_ARTIFACT_PRESENTATIONS = {
    "create": "🆕 建立",
    "new": "🆕 建立",
    "write": "📝 寫入",
    "save": "📝 寫入",
    "edit": "✏️ 更新",
    "update": "✏️ 更新",
    "delete": "🗑️ 刪除",
    "remove": "🗑️ 刪除",
}
_PHASE_ICONS = {
    "分析中": semantic_icon("phase.analysis"),
    "推理中": semantic_icon("phase.reasoning"),
    "模型重試": semantic_icon("phase.retry"),
    "模型限流": semantic_icon("phase.rate_limit"),
    "整理輸出": semantic_icon("phase.output"),
    "執行工具": semantic_icon("phase.tool"),
    "上下文壓縮": semantic_icon("phase.compaction"),
    "壓縮後整理": semantic_icon("phase.cleanup"),
    "壓縮重試": semantic_icon("phase.retry"),
    "子代理執行": semantic_icon("phase.subagent"),
    "執行環境就緒": semantic_icon("phase.ready"),
    "工具失敗": semantic_icon("phase.warning"),
    "分析結果": semantic_icon("phase.reasoning"),
    "更新計畫": semantic_icon("phase.plan"),
    "發生錯誤": semantic_icon("phase.error"),
    "接近保護上限": semantic_icon("phase.guard"),
    "保護性停止": semantic_icon("phase.stop"),
    "已取消": semantic_icon("phase.stop"),
    "已結束": semantic_icon("phase.finished"),
    "完成": semantic_icon("phase.success"),
}
_PHASE_EN = {
    "分析中": "Analyzing",
    "準備工具輸入": "Preparing tool input",
    "推理中": "Reasoning",
    "模型重試": "Model retry",
    "模型限流": "Model rate limit",
    "整理輸出": "Preparing output",
    "執行工具": "Running tools",
    "上下文壓縮": "Compacting context",
    "壓縮後整理": "Finalizing compaction",
    "壓縮重試": "Compaction retry",
    "子代理執行": "Running subagents",
    "執行環境就緒": "Runtime ready",
    "工具失敗": "Tool failure",
    "分析結果": "Analyzing results",
    "更新計畫": "Updating plan",
    "發生錯誤": "Error",
    "接近保護上限": "Near protection limit",
    "保護性停止": "Protection stop",
    "已取消": "Cancelled",
    "已結束": "Finished",
    "完成": "Complete",
}
_PHASE_ZH_CN = {
    "分析中": "分析中",
    "準備工具輸入": "准备工具输入",
    "推理中": "推理中",
    "模型重試": "模型重试",
    "模型限流": "模型限流",
    "整理輸出": "整理输出",
    "執行工具": "执行工具",
    "上下文壓縮": "上下文压缩",
    "壓縮後整理": "压缩后整理",
    "壓縮重試": "压缩重试",
    "子代理執行": "子代理执行",
    "執行環境就緒": "执行环境就绪",
    "工具失敗": "工具失败",
    "分析結果": "分析结果",
    "更新計畫": "更新计划",
    "發生錯誤": "发生错误",
    "接近保護上限": "接近保护上限",
    "保護性停止": "保护性停止",
    "已取消": "已取消",
    "已結束": "已结束",
    "完成": "完成",
}
_PRODUCT_LABEL_EN = {
    "💻 終端": "💻 Terminal",
    "📖 讀取": "📖 Read",
    "✏️ 編輯": "✏️ Edit",
    "📝 寫入": "📝 Write",
    "📂 檔案搜尋": "📂 File search",
    "🔎 內容搜尋": "🔎 Content search",
    "🔎 搜尋": "🔎 Search",
    "✅ 待辦": "✅ Todo",
    "🌐 網路搜尋": "🌐 Web search",
    "🔗 讀取網頁": "🔗 Fetch web page",
    "🌐 瀏覽器": "🌐 Browser",
    "🤖 子代理": "🤖 Subagent",
    "🖼️ 圖像": "🖼️ Image",
    "🧠 記憶": "🧠 Memory",
    "🧩 技能": "🧩 Skill",
    "🆕 建立": "🆕 Create",
    "✏️ 更新": "✏️ Update",
    "🗑️ 刪除": "🗑️ Delete",
}
_PRODUCT_LABEL_ZH_CN = {
    "💻 終端": "💻 终端",
    "📖 讀取": "📖 读取",
    "✏️ 編輯": "✏️ 编辑",
    "📝 寫入": "📝 写入",
    "📂 檔案搜尋": "📂 文件搜索",
    "🔎 內容搜尋": "🔎 内容搜索",
    "🔎 搜尋": "🔎 搜索",
    "✅ 待辦": "✅ 待办",
    "🌐 網路搜尋": "🌐 网络搜索",
    "🔗 讀取網頁": "🔗 读取网页",
    "🌐 瀏覽器": "🌐 浏览器",
    "🤖 子代理": "🤖 子代理",
    "🖼️ 圖像": "🖼️ 图像",
    "🧠 記憶": "🧠 记忆",
    "🧩 技能": "🧩 技能",
    "🆕 建立": "🆕 创建",
    "✏️ 更新": "✏️ 更新",
    "🗑️ 刪除": "🗑️ 删除",
}
_SUCCESS_FINISH_REASONS = frozenset({"completed", "complete", "success", "succeeded"})


def _surface_locale(language: str) -> str:
    canonical = canonical_language(language)
    return canonical if canonical in {"zh-TW", "zh-CN"} else "en"


def _surface_text(
    language: str,
    *,
    zh_tw: str,
    en: str,
    zh_cn: str | None = None,
) -> str:
    locale = _surface_locale(language)
    if locale == "zh-TW":
        return zh_tw
    if locale == "zh-CN":
        return zh_cn if zh_cn is not None else zh_tw
    return en


def _phase_display(phase: str, language: str) -> str:
    return progress_phase(language, phase)


def _product_label(label: str, language: str) -> str:
    locale = _surface_locale(language)
    if locale == "zh-TW":
        return label
    if locale == "zh-CN":
        return _PRODUCT_LABEL_ZH_CN.get(label, label)
    known = _PRODUCT_LABEL_EN.get(label)
    if known is None:
        return label
    icon, _, term = known.partition(" ")
    return f"{icon} {tool_term(language, term)}"


@dataclass(frozen=True, slots=True)
class _ActiveTool:
    label: str
    started_at: int


@dataclass(slots=True)
class ProgressRenderer:
    """Build one bounded live-progress card from structured runner events."""

    language: str = "zh-TW"
    icon_intensity: IconIntensity = "standard"
    show_output_excerpts: bool = True
    tool_counts: dict[str, int] = field(default_factory=dict)
    tool_labels: dict[str, str] = field(default_factory=dict)
    recent_output: list[str] = field(default_factory=list)
    pending_text: str = ""
    todo_items: tuple[TodoItem, ...] = ()
    todo_updated_at: int = 0
    last_activity_at: int | None = None
    completed_tool_calls: int = 0
    model_rounds: int = 0
    _tools_at_todo: int = 0
    _rounds_at_todo: int = 0
    artifacts: list[str] = field(default_factory=list)
    tool_errors: dict[str, int] = field(default_factory=dict)
    active_tools: dict[str, _ActiveTool] = field(default_factory=dict)
    input_tokens: int = 0
    last_input_tokens: int = 0
    last_context_tokens: int = 0
    output_tokens: int = 0
    cache_tokens: int = 0
    reasoning_tokens: int = 0
    context_warning_tokens: int | None = None
    context_threshold_tokens: int | None = None
    native_compaction_enabled: bool = False
    native_compaction_count: int = 0
    native_compaction_completed_count: int = 0
    compaction_active: bool = False
    compaction_incomplete: bool = False
    compaction_error_count: int = 0
    _native_compaction_summary_seen: bool = False
    last_compaction_items: int = 0
    last_compaction_tokens: int = 0
    last_pruned_characters: int = 0
    last_pruned_tokens: int = 0
    reasoning_seen: bool = False
    retry_count: int = 0
    rate_limit_retry_count: int = 0
    last_retry_after_seconds: float | None = None
    last_retry_delay_seconds: float | None = None
    rate_limit_seen: bool = False
    runtime_startup_ms: int | None = None
    runtime_attempt: int = 0
    subagent_total: int = 0
    subagent_finished: int = 0
    subagent_failed: int = 0
    active_subagents: set[str] = field(default_factory=set)
    guards: dict[GuardReason, GuardEvent] = field(default_factory=dict)
    guard_stopped: bool = False
    phase: str = "分析中"
    error: str | None = None
    finished_successfully: bool = False
    finished: bool = False
    finished_cancelled: bool = False
    revision: int = 0
    answer_length_overrun: tuple[int, int] | None = None
    verification_repair: VerificationEvent | None = None
    research_advisories: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        self.language = canonical_language(self.language)
        self.icon_intensity = canonical_icon_intensity(self.icon_intensity)

    def todo_is_stale(self, elapsed_seconds: int) -> bool:
        """Whether an unfinished plan lacks a recent structured confirmation."""

        return (
            bool(self.todo_items)
            and elapsed_seconds - self.todo_updated_at >= 60
            and any(item.status.casefold() not in {"completed", "done"}
                    for item in self.todo_items)
        )

    def accept(self, event: RunnerEventT, *, elapsed_seconds: int | None = None) -> None:
        """Update the card without exposing reasoning or raw tool output."""

        elapsed = max(0, elapsed_seconds or 0)
        if (isinstance(event, ActivityEvent) and not event.has_output) or (
            isinstance(event, TextEvent | ReasoningEvent) and not event.text
        ):
            return
        if isinstance(event, VerificationEvent):
            self.verification_repair = event
            self.revision += 1
            return
        if isinstance(event, ActivityEvent | TextEvent | ReasoningEvent | ToolStartEvent
                      | ToolResultEvent | UsageEvent | SubagentEvent | CompactionEvent):
            self.last_activity_at = max(self.last_activity_at or 0, elapsed)
        if isinstance(event, UsageEvent):
            self.model_rounds += 1
        if isinstance(event, ActivityEvent):
            # Reflect liveness once; never render or retain raw argument data.
            if self.phase != "準備工具輸入":
                self.phase = "準備工具輸入"
                self.revision += 1
            return
        if isinstance(event, TextEvent):
            self.pending_text = (self.pending_text + event.text)[-PROGRESS_PENDING_CHARS:]
            if not event.delta:
                self._commit_pending_text()
            self.phase = "整理輸出"
            self.revision += 1
            return
        if isinstance(event, ReasoningEvent):
            self.reasoning_seen = True
            if not self.active_tools:
                self.phase = "推理中"
            self.revision += 1
            return
        if isinstance(event, RetryEvent):
            self.retry_count += 1
            if event.rate_limited or event.status_code == 429:
                self.rate_limit_seen = True
                self.rate_limit_retry_count += 1
                self.last_retry_after_seconds = event.retry_after_seconds
                self.last_retry_delay_seconds = event.retry_delay_seconds
                if not self.active_tools:
                    self.phase = "模型限流"
            elif not self.active_tools:
                self.phase = "模型重試"
            self.revision += 1
            return
        if isinstance(event, RuntimeEvent):
            self.runtime_startup_ms = event.startup_ms
            self.runtime_attempt = event.attempt
            if not self.active_tools:
                self.phase = "執行環境就緒"
            self.revision += 1
            return
        if isinstance(event, SubagentEvent):
            if event.action is SubagentAction.STARTED:
                if event.child_session_id not in self.active_subagents:
                    self.subagent_total += 1
                self.active_subagents.add(event.child_session_id)
            else:
                self.active_subagents.discard(event.child_session_id)
                self.subagent_finished += 1
                if event.status not in {None, "ok"}:
                    self.subagent_failed += 1
                excerpt = _progress_excerpt(event.final_text)
                if excerpt and (not self.recent_output or self.recent_output[-1] != excerpt):
                    self.recent_output.append(excerpt)
                    self.recent_output = self.recent_output[-PROGRESS_OUTPUT_ITEMS:]
            self.phase = "子代理執行" if self.active_subagents else "分析結果"
            self.revision += 1
            return
        if isinstance(event, ToolStartEvent):
            self._commit_pending_text()
            key = _tool_key(event.name)
            self.tool_labels.setdefault(key, _tool_display(event.name, self.language))
            self.tool_counts[key] = self.tool_counts.get(key, 0) + 1
            self.active_tools[event.call_id] = _ActiveTool(
                label=_tool_activity(event, self.language),
                started_at=elapsed,
            )
            self.phase = "執行工具"
            self.revision += 1
            return
        if isinstance(event, ToolResultEvent):
            if event.call_id in self.active_tools:
                self.completed_tool_calls += 1
            self.active_tools.pop(event.call_id, None)
            if event.is_error:
                key = _tool_key(event.name or "工具")
                self.tool_labels.setdefault(
                    key,
                    _tool_display(event.name or "工具", self.language),
                )
                self.tool_errors[key] = self.tool_errors.get(key, 0) + 1
            self.phase = (
                "執行工具"
                if self.active_tools
                else "分析結果"
            )
            self.revision += 1
            return
        if isinstance(event, TodoEvent):
            # An identical structured snapshot is still a fresh confirmation
            # that the same step remains active. Clock ticks are not evidence.
            self.todo_updated_at = elapsed
            self.todo_items = event.items
            self._tools_at_todo = self.completed_tool_calls
            self._rounds_at_todo = self.model_rounds
            if not self.active_tools:
                self.phase = "更新計畫"
            self.revision += 1
            return
        if isinstance(event, ArtifactEvent):
            artifact = (
                f"{_artifact_operation_display(event.operation, self.language)} "
                f"{clip_text(redact_progress_text(event.path), 180)}"
            )
            if not self.artifacts or self.artifacts[-1] != artifact:
                self.artifacts.append(artifact)
                self.artifacts = self.artifacts[-PROGRESS_OUTPUT_ITEMS:]
            self.revision += 1
            return
        if isinstance(event, CompactionEvent):
            if event.action is CompactionAction.START:
                self.compaction_active = True
                self._native_compaction_summary_seen = False
                self.phase = "上下文壓縮"
            elif event.action is CompactionAction.SUMMARY:
                self.native_compaction_count += 1
                self._native_compaction_summary_seen = True
                self.last_compaction_items = event.shadowed_items
                self.last_compaction_tokens = event.shadowed_tokens
                self.compaction_active = True
                self.phase = "上下文壓縮"
            elif event.action is CompactionAction.PRUNE:
                self.last_pruned_tokens += event.shadowed_tokens
                self.last_pruned_characters += event.pruned_characters
                self.phase = "上下文壓縮"
            else:
                if event.error:
                    self.compaction_error_count += 1
                elif self._native_compaction_summary_seen:
                    self.native_compaction_completed_count += 1
                self._native_compaction_summary_seen = False
                self.compaction_active = False
                # Automatic DSH compaction may reject one summary and continue
                # with a later attempt. Keep that recoverable warning separate
                # from a runner ErrorEvent so a successful task stays healthy.
                self.phase = "壓縮後整理" if not event.error else "壓縮重試"
            self.revision += 1
            return
        if isinstance(event, GuardEvent):
            self.guards[event.reason] = event
            if event.action is GuardAction.STOP:
                self.guard_stopped = True
                self.phase = "保護性停止"
            elif not self.active_tools:
                self.phase = "接近保護上限"
            self.revision += 1
            return
        if isinstance(event, UsageEvent):
            # DSH can emit more than one usage notification in a turn.  Keep
            # the peak context size for the pressure guard; using only the
            # final notification can hide a transient 200K-token request.
            observed_input = max(0, event.input_tokens)
            observed_context = observed_input + max(0, event.cache_read_tokens)
            observed_context += max(0, event.cache_write_tokens)
            self.last_input_tokens = max(self.last_input_tokens, observed_input)
            self.last_context_tokens = max(self.last_context_tokens, observed_context)
            self.input_tokens += observed_input
            self.output_tokens += max(0, event.output_tokens)
            self.cache_tokens += max(0, event.cache_read_tokens)
            self.cache_tokens += max(0, event.cache_write_tokens)
            self.reasoning_tokens += max(0, event.reasoning_tokens)
            self.revision += 1
            return
        if isinstance(event, ErrorEvent):
            self.error = safe_error_code(event.code)
            if event.status_code == 429 or event.code.casefold() in {
                "rate_limit",
                "rate_limit_exhausted",
                "rate_limit_queue_timeout",
            }:
                self.rate_limit_seen = True
                self.last_retry_after_seconds = event.retry_after_seconds
            self.finished_successfully = False
            self.phase = "發生錯誤"
            self.revision += 1
            return
        if isinstance(event, FinishEvent):
            self.research_advisories = event.research_advisories
            # Cancellation or transport failure may interrupt native
            # compaction before its end event. Preserve that fact instead of
            # leaving a final card that falsely says "in progress".
            if self.compaction_active:
                self.compaction_active = False
                self.compaction_incomplete = True
            pending = " ".join(self.pending_text.split())
            final_text = " ".join(event.final_text.split())
            if pending and final_text and (
                final_text.startswith(pending) or final_text.endswith(pending)
            ):
                self.pending_text = ""
            else:
                self._commit_pending_text()
            self.active_tools.clear()
            self.active_subagents.clear()
            reason = event.reason.strip().casefold()
            self.finished = True
            self.finished_cancelled = event.cancelled
            self.finished_successfully = (
                not event.cancelled
                and self.error is None
                and reason in _SUCCESS_FINISH_REASONS
            )
            if self.guard_stopped:
                self.phase = "保護性停止"
            elif event.cancelled:
                self.phase = "已取消"
            elif self.finished_successfully:
                self.phase = "完成"
            elif self.error is not None or reason == "error":
                self.phase = "發生錯誤"
            else:
                self.phase = "已結束"
            self.revision += 1

    @property
    def has_non_text_activity(self) -> bool:
        return bool(
            self.tool_counts
            or self.todo_items
            or self.artifacts
            or self.tool_errors
            or self.answer_length_overrun is not None
            or self.input_tokens
            or self.output_tokens
            or self.cache_tokens
            or self.error
            or self.reasoning_seen
            or self.retry_count
            or self.runtime_startup_ms is not None
            or self.subagent_total
            or self.native_compaction_count
            or self.compaction_active
            or self.compaction_incomplete
            or self.compaction_error_count
            or self.last_pruned_characters
            or self.last_pruned_tokens
            or self.guards
            or self.verification_repair
            or self.research_advisories
        )

    def render(self, *, final: bool, elapsed_seconds: int) -> str | None:
        """Render the current visible state as one Telegram-safe plain message."""

        final = final or self.finished
        outputs = list(self.recent_output)
        pending = _progress_excerpt(self.pending_text)
        if pending and (not outputs or outputs[-1] != pending):
            outputs.append(pending)
        outputs = outputs[-PROGRESS_OUTPUT_ITEMS:]
        if not self.has_non_text_activity and not outputs:
            return None

        elapsed = format_elapsed(elapsed_seconds, language=self.language)
        phase = _phase_display(self.phase, self.language)
        heading = progress_heading(self.language, final=final)
        title = (
            f"📌 {heading} · ⏱️ {elapsed}" if final else
            f"⏳ {heading} · {_PHASE_ICONS.get(self.phase, '🔹')} {phase} · ⏱️ {elapsed}"
        )
        lines = [title]
        if final and self.research_advisories:
            from xdsh.core.verification_i18n import research_advisory_notice

            lines.append("🔎 " + research_advisory_notice(self.language, self.research_advisories))
        if self.verification_repair is not None and not final:
            from xdsh.core.verification_i18n import verification_repair_notice

            lines.append("🔎 " + verification_repair_notice(
                self.language, self.verification_repair))
        if final and self.answer_length_overrun is not None:
            count, limit = self.answer_length_overrun
            lines.append(_surface_text(
                self.language,
                zh_tw=f"📏 文本長度 (含格式): {count}/{limit} · 已保留完整答案",
                zh_cn=f"📏 文本长度 (含格式): {count}/{limit} · 已保留完整答案",
                en=f"📏 Text length (including markup): {count}/{limit} · full answer retained",
            ))
        if self.tool_counts:
            tools = []
            entries = list(self.tool_counts.items())
            for key, count in entries[:PROGRESS_TOOL_TYPES]:
                label = self.tool_labels[key]
                tools.append(
                    f"{label} \N{MULTIPLICATION SIGN}{count}" if count > 1 else label
                )
            if len(entries) > PROGRESS_TOOL_TYPES:
                remaining_types = len(entries) - PROGRESS_TOOL_TYPES
                tools.append(
                    _surface_text(
                        self.language,
                        zh_tw=f"另 {remaining_types} 種",
                        zh_cn=f"另 {remaining_types} 种",
                        en=f"{remaining_types} more",
                    )
                )
            lines.append(f"🧰 {progress_label(self.language, 'tools')}: {' · '.join(tools)}")
        if self.todo_items:
            stale_plan = self.todo_is_stale(elapsed_seconds)
            if not final and self.last_activity_at is not None:
                from xdsh.core.progress_activity_i18n import live_activity_text

                lines.append("📡 " + live_activity_text(
                    self.language,
                    tools=self.completed_tool_calls - (self._tools_at_todo if stale_plan else 0),
                    rounds=self.model_rounds - (self._rounds_at_todo if stale_plan else 0),
                    age=format_elapsed(max(0, elapsed_seconds - self.last_activity_at),
                                       language=self.language),
                    since_plan=stale_plan,
                ))
            completed = sum(item.status.casefold() in {"completed", "done"}
                            for item in self.todo_items)
            unfinished = completed < len(self.todo_items)
            age = max(0, elapsed_seconds - self.todo_updated_at)
            if not final and stale_plan:
                # Without a new snapshot, old Current/Next/ETA are not live
                # facts. Keep the last report, not a frozen apparent 0% bar.
                lines.append("📋 " + plan_snapshot_text(
                    self.language, done=completed, total=len(self.todo_items),
                    age=format_elapsed(age, language=self.language),
                ))
            else:
                lines.extend(
                    _todo_lines(
                        self.todo_items,
                        elapsed_seconds=elapsed_seconds,
                        final=final,
                        force_complete=False,
                        language=self.language,
                    )
                )
            if final and not unfinished:
                from xdsh.core.completion_check import completion_note

                lines.append(completion_note(self.language))
            if unfinished:
                if final and self.finished_successfully:
                    lines.append(_surface_text(
                        self.language, zh_tw="⚠️ 任務已結束。清單尚未核對。保留最後狀態。",
                        zh_cn="⚠️ 任务已结束。清单尚未核对。保留最后状态。",
                        en="⚠️ Task ended; checklist not reconciled. Last reported state retained.",
                    ))
        if self.active_tools and not final:
            last_call_id = next(reversed(self.active_tools))
            active = self.active_tools[last_call_id]
            same_tool_count = sum(
                item.label == active.label for item in self.active_tools.values()
            )
            other_count = len(self.active_tools) - same_tool_count
            active_count_suffix = (
                f" \N{MULTIPLICATION SIGN}{same_tool_count}"
                if same_tool_count > 1
                else ""
            )
            others = f" +{other_count}" if other_count else ""
            active_elapsed = max(0, elapsed_seconds - active.started_at)
            running_label = (
                "Running" if canonical_language(self.language) == "en"
                else progress_phase(self.language, "執行工具")
            )
            lines.append(
                _surface_text(
                    self.language,
                    zh_tw=(
                        f"🔄 執行中: {active.label}{active_count_suffix}{others} · 已 "
                        f"{format_elapsed(active_elapsed, language=self.language)}"
                    ),
                    zh_cn=(
                        f"🔄 执行中: {active.label}{active_count_suffix}{others} · 已 "
                        f"{format_elapsed(active_elapsed, language=self.language)}"
                    ),
                    en=(
                        f"🔄 {running_label}: {active.label}{active_count_suffix}{others} · "
                        f"{format_elapsed(active_elapsed, language=self.language)}"
                    ),
                )
            )
        if self.input_tokens or self.output_tokens or self.cache_tokens:
            usage = [
                f"{progress_label(self.language, key)} {_format_tokens(count)}"
                for key, count in (
                    ("input", self.input_tokens), ("output", self.output_tokens),
                    ("cache", self.cache_tokens), ("reasoning", self.reasoning_tokens),
                ) if count or key in {"input", "output"}
            ]
            lines.append(f"📊 {progress_label(self.language, 'usage')}: {' · '.join(usage)}")
            if self.last_context_tokens >= LONG_CONTEXT_WARNING_TOKENS:
                lines.append(
                    _surface_text(
                        self.language,
                        zh_tw=(
                            "⚠️ 長上下文: 本回合峰值 "
                            f"{_format_tokens(self.last_context_tokens)} · 後續模型步驟可能變慢"
                        ),
                        zh_cn=(
                            "⚠️ 长上下文: 本回合峰值 "
                            f"{_format_tokens(self.last_context_tokens)} · 后续模型步骤可能变慢"
                        ),
                        en=(
                            "⚠️ Long context: turn peak "
                            f"{_format_tokens(self.last_context_tokens)} · "
                            "later model steps may slow"
                        ),
                    )
                )
            if (
                self.context_threshold_tokens is not None
                and self.last_context_tokens >= self.context_threshold_tokens
            ):
                lines.append(
                    _surface_text(
                        self.language,
                        zh_tw=(
                            "🗜️ 自動壓縮: 已達門檻 · "
                            + (
                                "DSH 將在安全步驟邊界原生處理"
                                if self.native_compaction_enabled
                                else "下一個安全回合邊界切換 DSH generation"
                            )
                        ),
                        zh_cn=(
                            "🗜️ 自动压缩: 已达阈值 · "
                            + (
                                "DSH 将在安全步骤边界原生处理"
                                if self.native_compaction_enabled
                                else "下一个安全回合边界切换 DSH generation"
                            )
                        ),
                        en=(
                            "🗜️ Auto compact: threshold reached · "
                            + (
                                "DSH will handle it natively at a safe step boundary"
                                if self.native_compaction_enabled
                                else "switch DSH generation at the next safe turn boundary"
                            )
                        ),
                    )
                )
            elif (
                self.context_warning_tokens is not None
                and self.last_context_tokens >= self.context_warning_tokens
            ):
                lines.append(
                    _surface_text(
                        self.language,
                        zh_tw=(
                            "🗜️ 自動壓縮: 接近門檻 · "
                            + (
                                "DSH 會在步驟邊界處理"
                                if self.native_compaction_enabled
                                else "會在安全回合邊界處理"
                            )
                        ),
                        zh_cn=(
                            "🗜️ 自动压缩: 接近阈值 · "
                            + (
                                "DSH 会在步骤边界处理"
                                if self.native_compaction_enabled
                                else "会在安全回合边界处理"
                            )
                        ),
                        en=(
                            "🗜️ Auto compact: nearing threshold · "
                            + (
                                "DSH will handle it at a step boundary"
                                if self.native_compaction_enabled
                                else "it will run at a safe turn boundary"
                            )
                        ),
                    )
                )
        if (
            self.native_compaction_count
            or self.compaction_active
            or self.compaction_incomplete
            or self.compaction_error_count
        ):
            state_tw = (
                "進行中"
                if self.compaction_active
                else "未完成"
                if self.compaction_incomplete
                else "已完成·含重試"
                if self.compaction_error_count and self.native_compaction_count
                else "失敗"
                if self.compaction_error_count
                else "已完成"
            )
            state = _surface_text(
                self.language,
                zh_tw=state_tw,
                zh_cn={
                    "進行中": "进行中",
                    "未完成": "未完成",
                    "已完成·含重試": "已完成·含重试",
                    "失敗": "失败",
                    "已完成": "已完成",
                }[state_tw],
                en={
                    "進行中": "running",
                    "未完成": "incomplete",
                    "已完成·含重試": "complete with retries",
                    "失敗": "failed",
                    "已完成": "complete",
                }[state_tw],
            )
            detail = _surface_text(
                self.language,
                zh_tw=(
                    f"摘要 {self.last_compaction_items} 項 · "
                    f"~{_format_tokens(self.last_compaction_tokens)} tokens"
                    if self.native_compaction_count
                    else "等待摘要"
                ),
                zh_cn=(
                    f"摘要 {self.last_compaction_items} 项 · "
                    f"~{_format_tokens(self.last_compaction_tokens)} tokens"
                    if self.native_compaction_count
                    else "等待摘要"
                ),
                en=(
                    f"{self.last_compaction_items} summary items · "
                    f"~{_format_tokens(self.last_compaction_tokens)} tokens"
                    if self.native_compaction_count
                    else "waiting for summary"
                ),
            )
            if self.compaction_error_count:
                detail += _surface_text(
                    self.language,
                    zh_tw=(
                        f" · 重試\N{MULTIPLICATION SIGN}{self.compaction_error_count}"
                    ),
                    zh_cn=(
                        f" · 重试\N{MULTIPLICATION SIGN}{self.compaction_error_count}"
                    ),
                    en=(
                        f" · retries\N{MULTIPLICATION SIGN}{self.compaction_error_count}"
                    ),
                )
            lines.append(
                _surface_text(
                    self.language,
                    zh_tw=f"🗜️ DSH 原生壓縮: {state} · {detail}",
                    zh_cn=f"🗜️ DSH 原生压缩: {state} · {detail}",
                    en=f"🗜️ Native DSH compaction: {state} · {detail}",
                )
            )
        if self.last_pruned_characters or self.last_pruned_tokens:
            prune_details: list[str] = []
            if self.last_pruned_tokens:
                prune_details.append(f"~{_format_tokens(self.last_pruned_tokens)} tokens")
            if self.last_pruned_characters:
                prune_details.append(
                    _surface_text(
                        self.language,
                        zh_tw=f"{_format_tokens(self.last_pruned_characters)} 字元",
                        zh_cn=f"{_format_tokens(self.last_pruned_characters)} 字符",
                        en=f"{_format_tokens(self.last_pruned_characters)} chars",
                    )
                )
            lines.append(
                _surface_text(
                    self.language,
                    zh_tw=f"🧹 工具輸出裁剪: {' · '.join(prune_details)}",
                    zh_cn=f"🧹 工具输出裁剪: {' · '.join(prune_details)}",
                    en=f"🧹 Tool-output pruning: {' · '.join(prune_details)}",
                )
            )
        if self.reasoning_seen:
            lines.append(
                _surface_text(
                    self.language,
                    zh_tw="🧠 Thinking: 已接收結構化內容",
                    zh_cn="🧠 Thinking: 已接收结构化内容",
                    en="🧠 Thinking: structured content received",
                )
            )
        if self.retry_count or self.rate_limit_seen:
            if self.rate_limit_seen:
                delay = self.last_retry_delay_seconds
                cooldown = ""
                if delay is not None:
                    cooldown = _surface_text(
                        self.language,
                        zh_tw=f" · 最近退避 {delay:.1f}s",
                        zh_cn=f" · 最近退避 {delay:.1f}s",
                        en=f" · last backoff {delay:.1f}s",
                    )
                elif self.last_retry_after_seconds is not None:
                    cooldown = _surface_text(
                        self.language,
                        zh_tw=f" · 冷卻 {self.last_retry_after_seconds:.1f}s",
                        zh_cn=f" · 冷却 {self.last_retry_after_seconds:.1f}s",
                        en=f" · cooldown {self.last_retry_after_seconds:.1f}s",
                    )
                lines.append(
                    _surface_text(
                        self.language,
                        zh_tw=(
                            f"🚦 上游限流(429): 重試 {self.rate_limit_retry_count} 次"
                            f"{cooldown}"
                        ),
                        zh_cn=(
                            f"🚦 上游限流(429): 重试 {self.rate_limit_retry_count} 次"
                            f"{cooldown}"
                        ),
                        en=(
                            f"🚦 Upstream rate limit (429): "
                            f"{self.rate_limit_retry_count} retries{cooldown}"
                        ),
                    )
                )
            else:
                lines.append(
                    _surface_text(
                        self.language,
                        zh_tw=f"🔁 上游模型重試: {self.retry_count} 次 · 系統已自動續跑",
                        zh_cn=f"🔁 上游模型重试: {self.retry_count} 次 · 系统已自动续跑",
                        en=f"🔁 Upstream model retry: {self.retry_count} · resumed automatically",
                    )
                )
        if self.runtime_startup_ms is not None:
            startup = _surface_text(
                self.language,
                zh_tw=f"{self.runtime_startup_ms / 1000:.1f} 秒",
                zh_cn=f"{self.runtime_startup_ms / 1000:.1f} 秒",
                en=f"{self.runtime_startup_ms / 1000:.1f}s",
            )
            attempt = (
                _surface_text(
                    self.language,
                    zh_tw=f" · 第 {self.runtime_attempt} 次",
                    zh_cn=f" · 第 {self.runtime_attempt} 次",
                    en=f" · attempt {self.runtime_attempt}",
                )
                if self.runtime_attempt > 1
                else ""
            )
            lines.append(
                _surface_text(
                    self.language,
                    zh_tw=f"🚀 冷啟動: {startup}{attempt}",
                    zh_cn=f"🚀 冷启动: {startup}{attempt}",
                    en=f"🚀 Cold start: {startup}{attempt}",
                )
            )
        if self.subagent_total:
            child_parts = [
                _surface_text(
                    self.language,
                    zh_tw=f"完成 {self.subagent_finished}/{self.subagent_total}",
                    zh_cn=f"完成 {self.subagent_finished}/{self.subagent_total}",
                    en=f"complete {self.subagent_finished}/{self.subagent_total}",
                )
            ]
            if self.active_subagents:
                child_parts.append(
                    _surface_text(
                        self.language,
                        zh_tw=f"執行中 {len(self.active_subagents)}",
                        zh_cn=f"执行中 {len(self.active_subagents)}",
                        en=f"running {len(self.active_subagents)}",
                    )
                )
            if self.subagent_failed:
                child_parts.append(
                    _surface_text(
                        self.language,
                        zh_tw=f"失敗 {self.subagent_failed}",
                        zh_cn=f"失败 {self.subagent_failed}",
                        en=f"failed {self.subagent_failed}",
                    )
                )
            lines.append(
                _surface_text(
                    self.language,
                    zh_tw=f"🤖 子代理: {' · '.join(child_parts)}",
                    zh_cn=f"🤖 子代理: {' · '.join(child_parts)}",
                    en=f"🤖 Subagents: {' · '.join(child_parts)}",
                )
            )
        if self.guards:
            warnings = [
                _guard_measurement(event, language=self.language)
                for event in self.guards.values()
                if event.action is GuardAction.WARNING
            ]
            stops = [
                _guard_measurement(event, language=self.language)
                for event in self.guards.values()
                if event.action is GuardAction.STOP
            ]
            if warnings:
                lines.append(
                    f"🛡️ {guard_text(self.language, 'warning')}: {' · '.join(warnings)}"
                )
            if stops:
                lines.append(
                    f"🛑 {guard_text(self.language, 'stopped')}: {' · '.join(stops)}"
                )
        if outputs and self.show_output_excerpts:
            lines.append(
                _surface_text(
                    self.language,
                    zh_tw="💬 最新輸出: (原文摘錄)",
                    zh_cn="💬 最新输出: (原文摘录)",
                    en="💬 Latest output: (original excerpt)",
                )
            )
            lines.extend(
                f"• {_decorate_progress_excerpt(output, self.icon_intensity)}"
                for output in outputs
            )
        # Tool-level diagnostics stay in state/metrics, not in the user-facing
        # progress card. Terminal errors and protective stops remain visible.
        if self.error == "ACCEPTANCE_FAILED":
            from xdsh.core.verification_i18n import verification_notice

            lines.append("❌ " + verification_notice(self.language, (), brief=True))
        elif self.error is not None:
            lines.append(
                _surface_text(
                    self.language,
                    zh_tw=f"❌ 執行錯誤: {self.error}",
                    zh_cn=f"❌ 执行错误: {self.error}",
                    en=f"❌ Execution error: {self.error}",
                )
            )
        # Group card sections without spreading each Todo line or excerpt
        # into a separate paragraph. Icons are stable across locale catalogs.
        spaced: list[str] = []
        for line in lines:
            if spaced and line.startswith(("🧰", "📋", "📊", "💬", "📎", "❌", "🛑")):
                if spaced[-1]:
                    spaced.append("")
            spaced.append(line)
        rendered = "\n".join(spaced)
        if len(rendered) <= TELEGRAM_TEXT_CHUNK:
            return rendered
        return f"{rendered[: TELEGRAM_TEXT_CHUNK - 1]}…"

    def _commit_pending_text(self) -> None:
        excerpt = _progress_excerpt(self.pending_text)
        self.pending_text = ""
        if not excerpt or (self.recent_output and self.recent_output[-1] == excerpt):
            return
        self.recent_output.append(excerpt)
        self.recent_output = self.recent_output[-PROGRESS_OUTPUT_ITEMS:]


def _decorate_progress_excerpt(excerpt: str, intensity: IconIntensity) -> str:
    """Add a bounded semantic marker only in aggressive progress mode."""

    if intensity != "aggressive" or not excerpt:
        return excerpt
    if any(excerpt.startswith(icon) for icon in icon_catalog().values()):
        return excerpt
    return f"💬 {excerpt}"


def decorate_answer_icons(text: str, intensity: IconIntensity) -> str:
    """Guarantee visible, bounded semantic markers without touching structured data."""

    if intensity not in {"aggressive", "llm"} or not text.strip():
        return text
    stripped = text.lstrip()
    if stripped.startswith(("{", "[", "```json", "```JSON")):
        return text
    lines = text.splitlines()
    decorated: list[str] = []
    for index, line in enumerate(lines):
        content = line.strip()
        if not content or content.startswith("```"):
            decorated.append(line)
            continue
        if any(content.startswith(icon) for icon in icon_catalog().values()):
            decorated.append(line)
            continue
        if content.startswith("#") or (content.startswith("**") and content.endswith("**")):
            decorated.append(f"📌 {line}")
        elif content.startswith(("- ", "* ", "+ ")):
            decorated.append(f"🔹 {line}")
        elif index == next((i for i, candidate in enumerate(lines) if candidate.strip()), 0):
            decorated.append(f"💡 {line}")
        else:
            decorated.append(line)
    result = "\n".join(decorated)
    return result if result != text else f"💡 {text}"


_RETRIEVAL_NEGATIVE_CLAIM = re.compile(
    r"(?:不存在|不存在哪|没有任何|沒有任何|不可见|不可見|无此|無此|未出现|未出現|"
    r"not exist|does not exist|none found|unavailable|not visible)",
    re.IGNORECASE,
)
_RETRIEVAL_SAFE_QUALIFIER = re.compile(
    r"(?:无法确认|無法確認|未能确认|未能確認|未能核实|未能核實|不能证明|不能證明|"
    r"不代表不存在|不等于不存在|not confirmed|could not verify|not evidence)",
    re.IGNORECASE,
)


def _retrieval_claim_warning(answer: str, *, language: str) -> str | None:
    """Caveat hard absence claims when retrieval evidence is incomplete."""

    if (
        not _RETRIEVAL_NEGATIVE_CLAIM.search(answer)
        or _RETRIEVAL_SAFE_QUALIFIER.search(answer)
    ):
        return None
    if language == "zh-CN":
        return "⚠️ 检索未能证实该结论。搜索无结果不等于对象不存在或不可用。"
    if language == "en":
        return "⚠️ Retrieval did not establish this conclusion; no result is not proof of absence."
    return "⚠️ 檢索未能證實該結論。搜尋無結果不等於對象不存在或不可用。"


@dataclass(slots=True)
class TurnRenderer:
    """Accumulate an answer while emitting compact, separate progress text."""

    max_answer_chars: int
    show_reasoning: bool = False
    max_reasoning_chars: int = 20_000
    language: str = "zh-TW"
    icon_intensity: IconIntensity = "standard"
    text_parts: list[str] = field(default_factory=list)
    text_chars: int = 0
    truncated: bool = False
    reasoning_parts: list[str] = field(default_factory=list)
    reasoning_chars: int = 0
    reasoning_truncated: bool = False
    tool_count: int = 0
    usage: UsageEvent | None = None
    finish: FinishEvent | None = None
    error: ErrorEvent | None = None
    guard: GuardEvent | None = None
    original_request: str = ""
    retrieval_evidence_incomplete: bool = False
    completion_conflict: bool = False

    def __post_init__(self) -> None:
        self.language = canonical_language(self.language)
        self.icon_intensity = canonical_icon_intensity(self.icon_intensity)

    def apply_output_contract(self) -> None:
        from xdsh.core.answer_contract import format_answer, requests_json_only
        from xdsh.core.completion_check import conflicting_completion_claim

        self.completion_conflict = (
            not requests_json_only(self.original_request)
            and conflicting_completion_claim("".join(self.text_parts))
        )

        if not self.truncated:
            answer = format_answer(self.original_request, "".join(self.text_parts).strip())
            if self.retrieval_evidence_incomplete:
                warning = _retrieval_claim_warning(answer, language=self.language)
                if warning and warning not in answer:
                    answer = f"{answer.rstrip()}\n\n{warning}".strip()
            self.text_parts = [decorate_answer_icons(answer, self.icon_intensity)] if answer else []
            self.text_chars = len(answer)

    def accept(self, event: RunnerEventT) -> str | None:
        if isinstance(event, VerificationEvent):
            from xdsh.core.verification_i18n import verification_repair_notice

            return verification_repair_notice(self.language, event)
        if isinstance(event, ActivityEvent):
            return None
        if isinstance(event, TextEvent):
            self._append_text(event.text)
            return None
        if isinstance(event, ReasoningEvent):
            if self.show_reasoning:
                self._append_reasoning(event.text)
            return None
        if isinstance(event, RetryEvent):
            if event.rate_limited or event.status_code == 429:
                delay = (
                    event.retry_delay_seconds if event.retry_delay_seconds is not None
                    else event.retry_after_seconds
                )
                suffix = (
                    _surface_text(
                        self.language,
                        zh_tw=f" · 退避 {delay:.1f}s",
                        zh_cn=f" · 退避 {delay:.1f}s",
                        en=f" · backoff {delay:.1f}s",
                    )
                    if delay is not None
                    else _surface_text(
                        self.language,
                        zh_tw=" · 按策略退避",
                        zh_cn=" · 按策略退避",
                        en=" · policy backoff",
                    )
                )
                return _surface_text(
                    self.language,
                    zh_tw=f"進度: 上游限流 (429){suffix}",
                    zh_cn=f"进度: 上游限流 (429){suffix}",
                    en=f"Progress: upstream rate limit (429){suffix}",
                )
            return _surface_text(
                self.language,
                zh_tw="進度: 上游模型正在自動重試",
                zh_cn="进度: 上游模型正在自动重试",
                en="Progress: retrying the upstream model automatically",
            )
        if isinstance(event, RuntimeEvent):
            return _surface_text(
                self.language,
                zh_tw=f"進度: 執行環境已就緒 ({event.startup_ms / 1000:.1f} 秒)",
                zh_cn=f"进度: 执行环境已就绪 ({event.startup_ms / 1000:.1f} 秒)",
                en=f"Progress: runtime ready ({event.startup_ms / 1000:.1f}s)",
            )
        if isinstance(event, SubagentEvent):
            return _surface_text(
                self.language,
                zh_tw=(
                    "進度: 子代理已開始"
                    if event.action is SubagentAction.STARTED
                    else f"進度: 子代理已結束 ({event.status or 'unknown'})"
                ),
                zh_cn=(
                    "进度: 子代理已开始"
                    if event.action is SubagentAction.STARTED
                    else f"进度: 子代理已结束 ({event.status or 'unknown'})"
                ),
                en=(
                    "Progress: subagent started"
                    if event.action is SubagentAction.STARTED
                    else f"Progress: subagent finished ({event.status or 'unknown'})"
                ),
            )
        if isinstance(event, ToolStartEvent):
            self.tool_count += 1
            return _surface_text(
                self.language,
                zh_tw=f"進度: 正在執行 {clip_text(event.name, 80)} (工具 {self.tool_count})",
                zh_cn=f"进度: 正在运行 {clip_text(event.name, 80)} (工具 {self.tool_count})",
                en=f"Progress: running {clip_text(event.name, 80)} (tool {self.tool_count})",
            )
        if isinstance(event, ToolResultEvent):
            tool_name = (event.name or "").casefold()
            if tool_name in {"web_search", "web_fetch", "search", "fetch"}:
                content = event.content[:12_000].casefold()
                self.retrieval_evidence_incomplete |= (
                    event.is_error
                    or '"complete": false' in content
                    or "not evidence of absence" in content
                    or "no usable results" in content
                )
        if isinstance(event, ToolResultEvent) and event.is_error:
            name = event.name or "工具"
            return _surface_text(
                self.language,
                zh_tw=f"進度: {clip_text(name, 80)} 返回錯誤",
                zh_cn=f"进度: {clip_text(name, 80)} 返回错误",
                en=f"Progress: {clip_text(name, 80)} returned an error",
            )
        if isinstance(event, TodoEvent):
            total = len(event.items)
            completed = sum(item.status.lower() in {"completed", "done"} for item in event.items)
            active = next(
                (item.content for item in event.items if item.status.lower() == "in_progress"),
                None,
            )
            suffix = f" · {clip_text(active, 100)}" if active else ""
            return f"进度: 计划 {completed}/{total}{suffix}"
        if isinstance(event, ArtifactEvent):
            return f"进度: {clip_text(event.operation, 40)} {clip_text(event.path, 160)}"
        if isinstance(event, CompactionEvent):
            if event.action is CompactionAction.START:
                return "進度: DSH 原生上下文壓縮開始"
            if event.action is CompactionAction.SUMMARY:
                return (
                    "進度: DSH 原生上下文壓縮完成 "
                    f"(摘要 {event.shadowed_items} 項)"
                )
            if event.action is CompactionAction.PRUNE and (
                event.shadowed_tokens or event.pruned_characters
            ):
                details: list[str] = []
                if event.shadowed_tokens:
                    details.append(f"~{event.shadowed_tokens} tokens")
                if event.pruned_characters:
                    details.append(f"{event.pruned_characters} 字元")
                return f"進度: 工具輸出已裁剪 ({' · '.join(details)})"
            if event.action is CompactionAction.END and event.error:
                return "進度: DSH 原生上下文壓縮收尾失敗"
            return None
        if isinstance(event, GuardEvent):
            self.guard = event
            if event.action is GuardAction.STOP:
                measurement = _guard_measurement(event, language=self.language)
                return f"🛑 {guard_text(self.language, 'stopped')} ({measurement})"
            measurement = _guard_measurement(event, language=self.language)
            return f"🛡️ {guard_text(self.language, 'warning')} ({measurement})"
        if isinstance(event, UsageEvent):
            self.usage = event
            return None
        if isinstance(event, ErrorEvent):
            self.error = event
            if event.code == "ACCEPTANCE_FAILED":
                from xdsh.core.verification_i18n import verification_notice

                return verification_notice(self.language, event.verification_issues, brief=True)
            if event.status_code == 429 or event.code.casefold().startswith("rate_limit"):
                return _surface_text(
                    self.language,
                    zh_tw="執行錯誤 (HTTP 429 限流); 已保留部分輸出。",
                    zh_cn="执行错误 (HTTP 429 限流); 已保留部分输出。",
                    en="Execution error (HTTP 429 rate limit); partial output was retained.",
                )
            code = safe_error_code(event.code)
            return _surface_text(
                self.language,
                zh_tw=f"執行錯誤 ({code})。",
                zh_cn=f"执行错误 ({code})。",
                en=f"Execution error ({code}).",
            )
        if isinstance(event, FinishEvent):
            self.finish = event
            if event.final_text:
                self.text_parts = []
                self.text_chars = 0
                self.truncated = False
                self._append_text(event.final_text)
        return None

    def terminal_notice(self) -> str | None:
        """Return only an actionable terminal notice; successful answers need none."""

        finish = self.finish
        if finish is None:
            return terminal_text(self.language, "connection_lost")
        if self.guard is not None and self.guard.action is GuardAction.STOP:
            measurement = _guard_measurement(self.guard, language=self.language)
            return (f"🛡️ {guard_text(self.language, 'stopped')}: {measurement}\n"
                    + guard_text(self.language, "retained"))
        if finish.cancelled:
            return terminal_text(self.language, "cancelled")
        if self.error is not None:
            if self.error.code == "ACCEPTANCE_FAILED":
                from xdsh.core.verification_i18n import verification_notice

                return verification_notice(self.language, self.error.verification_issues)
            has_answer = bool("".join(self.text_parts).strip())
            advice = recovery_advice(self.language, self.error.code)
            hint = "\n" + advice if advice else ""
            if self.language == "zh-TW":
                return terminal_error_notice(self.error, has_answer=has_answer) + hint
            code = safe_error_code(self.error.code)
            retained = terminal_text(self.language, "retained" if has_answer else "no_partial")
            return terminal_text(self.language, "error", code=code, retained=retained) + hint
        if finish.reason == "error":
            return terminal_text(self.language, "missing_error")
        if not "".join(self.text_parts).strip():
            return terminal_text(self.language, "empty")
        if self.completion_conflict:
            from xdsh.core.completion_check import completion_note

            return completion_note(self.language, conflict=True)
        return None

    def answer_chunks(self, *, limit: int = TELEGRAM_TEXT_CHUNK) -> tuple[str, ...]:
        answer = "".join(self.text_parts).strip()
        if self.truncated:
            note = _surface_text(
                self.language,
                zh_tw=f"[答案已在 {self.max_answer_chars} 字元處截斷]",
                zh_cn=f"[答案已在 {self.max_answer_chars} 字符处截断]",
                en=f"[Answer truncated at {self.max_answer_chars} characters]",
            )
            answer = f"{answer}\n\n{note}"
        if not answer:
            return ()
        return split_text(answer, limit=limit)

    def thinking_chunks(self, *, limit: int = TELEGRAM_TEXT_CHUNK) -> tuple[str, ...]:
        """Render opt-in reasoning separately without placing it in progress cards."""

        if not self.show_reasoning:
            return ()
        thinking = redact_progress_text("".join(self.reasoning_parts).strip())
        if self.reasoning_truncated:
            note = _surface_text(
                self.language,
                zh_tw=f"[Thinking 已在 {self.max_reasoning_chars} 字元處截斷]",
                zh_cn=f"[Thinking 已在 {self.max_reasoning_chars} 字符处截断]",
                en=f"[Thinking truncated at {self.max_reasoning_chars} characters]",
            )
            thinking = (
                f"{thinking}\n\n{note}"
            )
        if not thinking:
            return ()
        header = "### 🧠 Thinking"
        continued = _surface_text(
            self.language,
            zh_tw="### 🧠 Thinking (續)",
            zh_cn="### 🧠 Thinking (续)",
            en="### 🧠 Thinking (continued)",
        )
        body_limit = limit - len(continued) - 2
        if body_limit <= 0:
            raise ValueError("thinking chunk limit is too small")
        chunks = split_text(thinking, limit=body_limit)
        return tuple(
            f"{header if index == 0 else continued}\n\n{chunk}"
            for index, chunk in enumerate(chunks)
        )

    def _append_text(self, text: str) -> None:
        remaining = self.max_answer_chars - self.text_chars
        if remaining <= 0:
            self.truncated = True
            return
        fragment = text[:remaining]
        self.text_parts.append(fragment)
        self.text_chars += len(fragment)
        if len(fragment) < len(text):
            self.truncated = True

    def _append_reasoning(self, text: str) -> None:
        remaining = self.max_reasoning_chars - self.reasoning_chars
        if remaining <= 0:
            self.reasoning_truncated = True
            return
        fragment = text[:remaining]
        self.reasoning_parts.append(fragment)
        self.reasoning_chars += len(fragment)
        if len(fragment) < len(text):
            self.reasoning_truncated = True


def split_text(text: str, *, limit: int = TELEGRAM_TEXT_CHUNK) -> tuple[str, ...]:
    """Split without Markdown parsing and prefer newline boundaries.

    Do not strip either side of a chunk: doing so removes intentional blank
    lines when a long answer crosses Telegram's message limit.
    """

    if limit <= 0:
        raise ValueError("message chunk limit must be positive")
    if not text:
        return ()
    remaining = text
    chunks: list[str] = []
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        boundary = remaining.rfind("\n", 0, limit)
        if boundary < limit // 2:
            boundary = limit
        else:
            boundary += 1
        chunks.append(remaining[:boundary])
        remaining = remaining[boundary:]
    return tuple(chunks)


def clip_text(text: str, limit: int) -> str:
    normalized = " ".join(text.split())
    return normalized if len(normalized) <= limit else f"{normalized[: limit - 1]}…"


def _tool_key(name: str) -> str:
    normalized = " ".join(name.split()).casefold() or "工具"
    canonical = _TOOL_CANONICAL_KEYS.get(normalized)
    if canonical is not None:
        return canonical
    for prefix, _label in _TOOL_PREFIX_PRESENTATIONS:
        if normalized.startswith(prefix):
            return {
                "mcp__": "mcp",
                "mcp.": "mcp",
                "browser_": "browser",
                "web_": "web_search",
                "image_": "image",
                "todo_": "todo",
                "task_": "task",
            }[prefix]
    return normalized


def _tool_display(name: str, language: str = "zh-TW") -> str:
    raw = clip_text(name, 80)
    key = _tool_key(name)
    presentation = _TOOL_PRESENTATIONS.get(key)
    if presentation is not None:
        return _product_label(presentation, language)
    for prefix, prefixed_presentation in _TOOL_PREFIX_PRESENTATIONS:
        if key.startswith(prefix):
            return _product_label(prefixed_presentation, language)
    return f"🧰 {raw}"


def _artifact_operation_display(operation: str, language: str = "zh-TW") -> str:
    raw = clip_text(operation, 40)
    presentation = _ARTIFACT_PRESENTATIONS.get(raw.casefold())
    return _product_label(presentation, language) if presentation is not None else f"📄 {raw}"


def _progress_excerpt(text: str) -> str:
    normalized = " ".join(text.split())
    if not normalized:
        return ""
    return clip_text(redact_progress_text(normalized), PROGRESS_OUTPUT_CHARS)


def format_elapsed(seconds: int, *, language: str = "zh-TW") -> str:
    bounded = max(0, seconds)
    minutes, remaining_seconds = divmod(bounded, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return _surface_text(
            language,
            zh_tw=f"{hours} 小時 {minutes} 分 {remaining_seconds} 秒",
            zh_cn=f"{hours} 小时 {minutes} 分 {remaining_seconds} 秒",
            en=f"{hours}h {minutes}m {remaining_seconds}s",
        )
    if minutes:
        return _surface_text(
            language,
            zh_tw=f"{minutes} 分 {remaining_seconds} 秒",
            zh_cn=f"{minutes} 分 {remaining_seconds} 秒",
            en=f"{minutes}m {remaining_seconds}s",
        )
    return _surface_text(
        language,
        zh_tw=f"{remaining_seconds} 秒",
        zh_cn=f"{remaining_seconds} 秒",
        en=f"{remaining_seconds}s",
    )


def redact_progress_text(text: str) -> str:
    """Redact common credential shapes from non-final Telegram progress."""

    redacted = safe_outbound_text(text)
    return redacted.replace("[REDACTED TELEGRAM TOKEN]", "***").replace(
        "[REDACTED]", "***"
    )


def _tool_activity(event: ToolStartEvent, language: str = "zh-TW") -> str:
    label = _tool_display(event.name, language)
    arguments = event.arguments
    if not isinstance(arguments, dict):
        return label
    path = next(
        (
            arguments.get(key)
            for key in ("path", "file_path", "notebook_path")
            if isinstance(arguments.get(key), str)
        ),
        None,
    )
    if not isinstance(path, str):
        return label
    basename = path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    if not basename:
        return label
    return f"{label} · {clip_text(redact_progress_text(basename), 100)}"


def _todo_lines(
    items: tuple[TodoItem, ...],
    *,
    elapsed_seconds: int,
    final: bool,
    force_complete: bool,
    language: str = "zh-TW",
) -> list[str]:
    total = len(items)
    completed = (
        total
        if force_complete
        else sum(item.status.casefold() in {"completed", "done"} for item in items)
    )
    fill = round(PROGRESS_BAR_CELLS * completed / total) if total else 0
    percent = int(100 * completed / total) if total else 0
    bar = "▰" * fill + "▱" * (PROGRESS_BAR_CELLS - fill)
    lines = [f"📋 {progress_label(language, 'plan')}: {completed}/{total} {bar} {percent}%"]
    active = None
    pending = None
    if not force_complete and not final:
        active = next(
            (item.content for item in items if item.status.casefold() == "in_progress"),
            None,
        )
        pending = next(
            (
                item.content
                for item in items
                if item.status.casefold() in {"pending", "todo"}
            ),
            None,
        )
    if active:
        active_text = clip_text(redact_progress_text(active), 160)
        lines.append(f"🎯 {progress_label(language, 'current')}: {active_text}")
    if pending:
        pending_text = clip_text(redact_progress_text(pending), 160)
        lines.append(f"⏭️ {progress_label(language, 'next')}: {pending_text}")
    if not final and 0 < completed < total and elapsed_seconds > 0:
        remaining = int(elapsed_seconds / completed * (total - completed))
        remaining_text = format_elapsed(remaining, language=language)
        lines.append(
            _surface_text(
                language,
                zh_tw=f"⌛ 粗估剩餘: {remaining_text}",
                zh_cn=f"⌛ 粗略剩余: {remaining_text}",
                en=f"⌛ Rough ETA: {remaining_text}",
            )
        )
    return lines


def _format_tokens(tokens: int) -> str:
    bounded = max(0, tokens)
    if bounded >= 1_000_000:
        return f"{bounded / 1_000_000:.1f}m"
    if bounded >= 1_000:
        return f"{bounded / 1_000:.1f}k"
    return str(bounded)


def _guard_measurement(event: GuardEvent, *, language: str = "zh-TW") -> str:
    label = guard_text(language, event.reason.value)
    if event.reason is GuardReason.TOKEN_BUDGET:
        observed = _format_tokens(event.observed)
        limit = _format_tokens(event.limit)
    elif event.reason in {GuardReason.STALLED, GuardReason.TIME_BUDGET}:
        observed = format_elapsed(event.observed, language=language)
        limit = format_elapsed(event.limit, language=language)
    else:
        observed = str(event.observed)
        limit = str(event.limit)
    return f"{label} {observed}/{limit}"
