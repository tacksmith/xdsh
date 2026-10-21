"""Transport-neutral terminal presentation for normalized runner events.

The renderer intentionally keeps progress and the answer on separate streams:
progress is bounded, credential-redacted, and safe to overwrite, while the
assistant answer remains ordinary Markdown on stdout.  No transport payload or
raw tool output is interpreted here.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

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
from xdsh.core.product_control import safe_outbound_text

CONSOLE_MAX_PROGRESS_CHARS = 2_000
CONSOLE_MAX_OUTPUT_CHARS = 240
CONSOLE_MAX_OUTPUT_ITEMS = 3
CONSOLE_MAX_TOOL_TYPES = 8
CONSOLE_BAR_CELLS = 14

_SUCCESS_REASONS = frozenset({"completed", "complete", "success", "succeeded"})
_ERROR_CODE_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")

_TOOL_LABELS = {
    "bash": "💻 終端",
    "shell": "💻 終端",
    "read": "📖 讀取",
    "read_file": "📖 讀取",
    "edit": "✏️ 編輯",
    "apply_patch": "✏️ 編輯",
    "write": "📝 寫入",
    "write_file": "📝 寫入",
    "glob": "📂 檔案搜尋",
    "find_files": "📂 檔案搜尋",
    "grep": "🔎 內容搜尋",
    "search": "🔎 搜尋",
    "todo": "✅ 待辦",
    "todo_write": "✅ 待辦",
    "web_search": "🌐 網路搜尋",
    "web_fetch": "🔗 讀取網頁",
    "browser": "🌐 瀏覽器",
    "task": "🤖 子代理",
    "subagent": "🤖 子代理",
    "memory": "🧠 記憶",
    "skill": "🧩 技能",
}
_TOOL_PREFIX_LABELS = (
    ("mcp__", "🔌 MCP"),
    ("mcp.", "🔌 MCP"),
    ("browser_", "🌐 瀏覽器"),
    ("web_", "🌐 網路"),
    ("task_", "🤖 子代理"),
)
_GUARD_LABELS = {
    GuardReason.RETRY_BUDGET: "重試",
    GuardReason.MODEL_STEP_BUDGET: "模型步驟",
    GuardReason.TOOL_BUDGET: "工具",
    GuardReason.REPEATED_TOOL: "重複工具",
    GuardReason.TOOL_ERROR_BUDGET: "工具錯誤",
    GuardReason.TOKEN_BUDGET: "Token",
    GuardReason.SUBAGENT_BUDGET: "子代理",
    GuardReason.STALLED: "停滯",
    GuardReason.TIME_BUDGET: "時間",
}


@dataclass(slots=True)
class ConsoleTurnRenderer:
    """Bounded answer/thinking accumulator independent of a transport API."""

    max_answer_chars: int = 60_000
    show_reasoning: bool = False
    max_reasoning_chars: int = 20_000
    text_parts: list[str] = field(default_factory=list)
    text_chars: int = 0
    truncated: bool = False
    reasoning_parts: list[str] = field(default_factory=list)
    reasoning_chars: int = 0
    reasoning_truncated: bool = False
    tool_count: int = 0
    finish: FinishEvent | None = None
    error: ErrorEvent | None = None
    guard: GuardEvent | None = None

    def __post_init__(self) -> None:
        if not 1_000 <= self.max_answer_chars <= 200_000:
            raise ValueError("max_answer_chars must be between 1000 and 200000")
        if not 1_000 <= self.max_reasoning_chars <= 100_000:
            raise ValueError("max_reasoning_chars must be between 1000 and 100000")

    def accept(self, event: RunnerEventT) -> str | None:
        """Consume one event and return an optional short progress notice."""

        if isinstance(event, ActivityEvent):
            return None
        if isinstance(event, VerificationEvent):
            from xdsh.core.verification_i18n import verification_repair_notice

            return verification_repair_notice("zh-TW", event)
        if isinstance(event, TextEvent):
            self._append_text(event.text)
        elif isinstance(event, ReasoningEvent):
            if self.show_reasoning:
                self._append_reasoning(event.text)
        elif isinstance(event, RetryEvent):
            return "🔁 上游模型重試中"
        elif isinstance(event, RuntimeEvent):
            return f"🚀 執行環境就緒 ({event.startup_ms / 1000:.1f}s)"
        elif isinstance(event, SubagentEvent):
            if event.action is SubagentAction.STARTED:
                return "🤖 子代理開始"
            return f"🤖 子代理結束 ({event.status or 'unknown'})"
        elif isinstance(event, ToolStartEvent):
            self.tool_count += 1
        elif isinstance(event, ToolResultEvent) and event.is_error:
            return f"⚠️ {_clip(event.name or '工具', 80)} 返回錯誤"
        elif isinstance(event, CompactionEvent):
            if event.action is CompactionAction.START:
                return "🗜️ DSH 原生壓縮開始"
            if event.action is CompactionAction.SUMMARY:
                return (
                    "🗜️ DSH 原生壓縮: "
                    f"摘要 {event.shadowed_items} 項 · ~{event.shadowed_tokens} tokens"
                )
            if event.action is CompactionAction.PRUNE and event.pruned_characters:
                return f"🧹 工具輸出裁剪: {event.pruned_characters} 字元"
            if event.action is CompactionAction.END and event.error:
                return "🗜️ DSH 原生壓縮重試中 (本回合繼續)"
        elif isinstance(event, GuardEvent):
            self.guard = event
            return (
                f"🛡️ {'停止' if event.action is GuardAction.STOP else '預警'}: "
                f"{_guard_measurement(event)}"
            )
        elif isinstance(event, ErrorEvent):
            self.error = event
            return f"❌ 執行錯誤 ({safe_error_code(event.code)})"
        elif isinstance(event, FinishEvent):
            self.finish = event
            if event.final_text:
                self.text_parts = []
                self.text_chars = 0
                self.truncated = False
                self._append_text(event.final_text)
        return None

    @property
    def answer(self) -> str:
        value = "".join(self.text_parts).strip()
        if self.truncated:
            value += f"\n\n[答案已在 {self.max_answer_chars} 字符處截斷]"
        return value

    @property
    def thinking(self) -> str:
        if not self.show_reasoning:
            return ""
        value = redact_progress_text("".join(self.reasoning_parts).strip())
        if self.reasoning_truncated:
            value += f"\n\n[Thinking 已在 {self.max_reasoning_chars} 字符處截斷]"
        return value

    @property
    def succeeded(self) -> bool:
        finish = self.finish
        return bool(
            finish
            and not finish.cancelled
            and self.error is None
            and finish.reason.casefold() in _SUCCESS_REASONS
            and self.answer.strip()
        )

    def terminal_notice(self) -> str | None:
        """Return only a useful non-success notice; successful turns stay quiet."""

        finish = self.finish
        if finish is None:
            return "DSH 連線中斷: 未收到結束事件。"
        if self.guard is not None and self.guard.action is GuardAction.STOP:
            retained = "部分答案已保留。" if self.answer else "目前沒有可保留的輸出。"
            return f"🛡️ 任務保護已停止: {_guard_measurement(self.guard)}。{retained}"
        if finish.cancelled:
            retained = "部分答案已保留。" if self.answer else "目前沒有可保留的輸出。"
            return f"任務已取消; {retained}"
        if self.error is not None:
            return terminal_error_notice(
                self.error,
                has_answer=bool("".join(self.text_parts).strip()),
            )
        if finish.reason.casefold() not in _SUCCESS_REASONS:
            return f"DSH 結束狀態: {safe_error_code(finish.reason)}。"
        if not self.answer:
            return "DSH 未返回可顯示的內容。"
        return None

    def answer_chunks(self, *, limit: int = 30_000) -> tuple[str, ...]:
        return split_text(self.answer, limit=limit)

    def thinking_chunks(self, *, limit: int = 30_000) -> tuple[str, ...]:
        thinking = self.thinking
        if not thinking:
            return ()
        body_limit = limit - 24
        if body_limit <= 0:
            raise ValueError("thinking chunk limit is too small")
        chunks = split_text(thinking, limit=body_limit)
        return tuple(
            f"### 🧠 Thinking{' (續)' if index else ''}\n\n{chunk}"
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


@dataclass(slots=True)
class ConsoleProgressRenderer:
    """Produce a bounded one-line or card-like progress summary."""

    tool_counts: Counter[str] = field(default_factory=Counter)
    tool_labels: dict[str, str] = field(default_factory=dict)
    recent_output: list[str] = field(default_factory=list)
    todo_items: tuple[TodoItem, ...] = ()
    input_tokens: int = 0
    output_tokens: int = 0
    cache_tokens: int = 0
    native_compaction_count: int = 0
    compaction_active: bool = False
    compaction_incomplete: bool = False
    compaction_error_count: int = 0
    last_compaction_items: int = 0
    last_compaction_tokens: int = 0
    last_pruned_characters: int = 0
    last_pruned_tokens: int = 0
    native_compaction_enabled: bool = False
    retry_count: int = 0
    rate_limit_retry_count: int = 0
    last_retry_after_seconds: float | None = None
    rate_limit_seen: bool = False
    active_tools: int = 0
    guards: dict[GuardReason, GuardEvent] = field(default_factory=dict)
    phase: str = "分析中"
    error: str | None = None
    finished: bool = False
    success: bool = False

    def accept(self, event: RunnerEventT) -> None:
        if isinstance(event, VerificationEvent):
            self.phase = f"修復交付檢查 {event.attempt}/{event.max_attempts}"
        elif isinstance(event, ActivityEvent):
            self.phase = "準備工具輸入"
        elif isinstance(event, TextEvent):
            self._remember(event.text)
            self.phase = "整理輸出"
        elif isinstance(event, ReasoningEvent):
            self.phase = "推理中"
        elif isinstance(event, RetryEvent):
            self.retry_count += 1
            if event.rate_limited or event.status_code == 429:
                self.rate_limit_seen = True
                self.rate_limit_retry_count += 1
                self.last_retry_after_seconds = event.retry_after_seconds
                self.phase = "模型限流"
            else:
                self.phase = "模型重試"
        elif isinstance(event, RuntimeEvent):
            self.phase = "執行環境就緒"
        elif isinstance(event, ToolStartEvent):
            key = _tool_key(event.name)
            self.tool_counts[key] += 1
            self.tool_labels.setdefault(key, _tool_label(event.name))
            self.active_tools += 1
            self.phase = "執行工具"
        elif isinstance(event, ToolResultEvent):
            self.active_tools = max(0, self.active_tools - 1)
            self.phase = (
                "工具失敗"
                if event.is_error
                else ("執行工具" if self.active_tools else "分析結果")
            )
            if event.content and event.is_error:
                self._remember(event.content)
        elif isinstance(event, TodoEvent):
            self.todo_items = event.items
            self.phase = "更新計畫"
        elif isinstance(event, ArtifactEvent):
            self._remember(f"{event.operation} {event.path}")
        elif isinstance(event, CompactionEvent):
            if event.action is CompactionAction.START:
                self.compaction_active = True
                self.phase = "上下文壓縮"
            elif event.action is CompactionAction.SUMMARY:
                self.native_compaction_count += 1
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
                self.compaction_active = False
                # Automatic DSH compaction can reject one summary and continue
                # (for example when it is not smaller). This is a recoverable
                # retry, not a terminal runner error.
                self.phase = "壓縮後整理" if not event.error else "壓縮重試"
        elif isinstance(event, GuardEvent):
            self.guards[event.reason] = event
            self.phase = "保護性停止" if event.action is GuardAction.STOP else "接近保護上限"
        elif isinstance(event, UsageEvent):
            self.input_tokens += max(0, event.input_tokens)
            self.output_tokens += max(0, event.output_tokens)
            self.cache_tokens += max(0, event.cache_read_tokens + event.cache_write_tokens)
        elif isinstance(event, ErrorEvent):
            self.error = safe_error_code(event.code)
            if event.status_code == 429 or event.code.casefold().startswith("rate_limit"):
                self.rate_limit_seen = True
            self.phase = "發生錯誤"
        elif isinstance(event, FinishEvent):
            # A cancelled/failed runtime can end before compaction/end.
            # Preserve a bounded diagnostic and clear the transient state.
            if self.compaction_active:
                self.compaction_active = False
                self.compaction_incomplete = True
            self.active_tools = 0
            self.finished = True
            self.success = (
                not event.cancelled
                and self.error is None
                and event.reason.casefold() in _SUCCESS_REASONS
            )
            self.phase = "完成" if self.success else ("已取消" if event.cancelled else "已結束")

    def render(self, *, elapsed_seconds: int, final: bool = False, one_line: bool = False) -> str:
        elapsed = format_elapsed(elapsed_seconds)
        title = f"{'✅' if self.success else '⏳'} {self.phase} · {elapsed}"
        tool_text = self._tool_text()
        todo_text = self._todo_text(force_complete=final and self.success)
        usage = f"入 {_format_tokens(self.input_tokens)} 出 {_format_tokens(self.output_tokens)}"
        fragments = [title]
        if tool_text:
            fragments.append(f"🧰 {tool_text}")
        if todo_text:
            fragments.append(todo_text)
        if self.active_tools and not final:
            fragments.append(f"🔄 執行中 {self.active_tools}")
        if self.input_tokens or self.output_tokens or self.cache_tokens:
            fragments.append(f"📊 {usage}")
        if self.retry_count or self.rate_limit_seen:
            if self.rate_limit_seen:
                cooldown = (
                    f" · 冷卻 {self.last_retry_after_seconds:.1f}s"
                    if self.last_retry_after_seconds is not None
                    else ""
                )
                fragments.append(
                    f"🚦 限流(429) 重試\N{MULTIPLICATION SIGN}"
                    f"{self.rate_limit_retry_count}{cooldown}"
                )
            else:
                fragments.append(f"🔁 重試\N{MULTIPLICATION SIGN}{self.retry_count}")
        if (
            self.native_compaction_count
            or self.compaction_active
            or self.compaction_incomplete
            or self.compaction_error_count
        ):
            state = (
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
            detail = (
                f"摘要 {self.last_compaction_items} 項 · ~{self.last_compaction_tokens} tokens"
                if self.native_compaction_count
                else "等待摘要"
            )
            if self.compaction_error_count:
                detail += f" · 重試\N{MULTIPLICATION SIGN}{self.compaction_error_count}"
            fragments.append(f"🗜️ DSH 原生壓縮 {state} · {detail}")
        if self.last_pruned_characters or self.last_pruned_tokens:
            prune_details: list[str] = []
            if self.last_pruned_tokens:
                prune_details.append(f"~{self.last_pruned_tokens} tokens")
            if self.last_pruned_characters:
                prune_details.append(f"{self.last_pruned_characters} 字元")
            fragments.append(f"🧹 裁剪 {' · '.join(prune_details)}")
        if self.guards:
            stops = [
                _guard_measurement(item)
                for item in self.guards.values()
                if item.action is GuardAction.STOP
            ]
            if stops:
                fragments.append("🛑 " + " · ".join(stops))
        if self.recent_output:
            fragments.append("💬 " + " / ".join(self.recent_output[-2:]))
        if self.error:
            fragments.append(f"❌ {self.error}")
        rendered = " · ".join(fragments)
        if one_line:
            return _clip(rendered, 1_000)
        return _clip(rendered, CONSOLE_MAX_PROGRESS_CHARS)

    def _tool_text(self) -> str:
        entries = []
        for key, count in self.tool_counts.most_common(CONSOLE_MAX_TOOL_TYPES):
            label = self.tool_labels.get(key, _tool_label(key))
            entries.append(
                f"{label}\N{MULTIPLICATION SIGN}{count}" if count > 1 else label
            )
        if len(self.tool_counts) > CONSOLE_MAX_TOOL_TYPES:
            entries.append(f"另 {len(self.tool_counts) - CONSOLE_MAX_TOOL_TYPES} 種")
        return " · ".join(entries)

    def _todo_text(self, *, force_complete: bool) -> str:
        if not self.todo_items:
            return ""
        total = len(self.todo_items)
        completed = total if force_complete else sum(
            item.status.casefold() in {"completed", "done"} for item in self.todo_items
        )
        percent = int(100 * completed / total) if total else 0
        fill = round(CONSOLE_BAR_CELLS * completed / total) if total else 0
        bar = "▰" * fill + "▱" * (CONSOLE_BAR_CELLS - fill)
        return f"📋 計畫 {completed}/{total} {bar} {percent}%"

    def _remember(self, text: str) -> None:
        excerpt = _clip(redact_progress_text(" ".join(text.split())), CONSOLE_MAX_OUTPUT_CHARS)
        if not excerpt or (self.recent_output and self.recent_output[-1] == excerpt):
            return
        self.recent_output.append(excerpt)
        del self.recent_output[:-CONSOLE_MAX_OUTPUT_ITEMS]


def split_text(text: str, *, limit: int = 30_000) -> tuple[str, ...]:
    """Split bounded Markdown without dropping newline boundaries."""

    if limit <= 0:
        raise ValueError("message chunk limit must be positive")
    remaining = text.strip()
    chunks: list[str] = []
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        boundary = remaining.rfind("\n", 0, limit + 1)
        if boundary < limit // 2:
            boundary = limit
        chunks.append(remaining[:boundary].rstrip())
        remaining = remaining[boundary:].lstrip()
    return tuple(chunk for chunk in chunks if chunk)


def format_elapsed(seconds: int) -> str:
    bounded = max(0, seconds)
    minutes, remaining = divmod(bounded, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {remaining}s"
    return f"{remaining}s"


def redact_progress_text(text: str) -> str:
    """Remove common credential shapes from non-final progress output."""

    redacted = safe_outbound_text(text)
    return redacted.replace("[REDACTED TELEGRAM TOKEN]", "***").replace(
        "[REDACTED]", "***"
    )


def safe_error_code(value: str) -> str:
    """Keep provider error labels bounded and free of raw secret-shaped values."""

    normalized = redact_progress_text(" ".join(value.split()))
    # Keep the redaction marker visible after the ASCII label filter removes
    # punctuation from the progress-safe ``***`` marker.
    normalized = normalized.replace("***", "REDACTED")
    normalized = _ERROR_CODE_UNSAFE.sub("_", normalized).strip("_")
    return normalized[:80] or "UNKNOWN"


def terminal_error_notice(error: ErrorEvent, *, has_answer: bool) -> str:
    """Describe a terminal error without copying an upstream error body."""

    retained = "已保留部分輸出。" if has_answer else "目前沒有可保留的輸出。"
    if error.status_code == 429 or error.code.casefold().startswith("rate_limit"):
        retry = ""
        if error.retry_after_seconds is not None:
            retry_seconds = min(120.0, max(0.0, error.retry_after_seconds))
            retry = f"退避 {retry_seconds:.1f}s。"
        return f"執行錯誤 (HTTP 429 限流); {retained}{retry}"
    return f"執行錯誤 ({safe_error_code(error.code)}); {retained}"


def _tool_key(name: str) -> str:
    return " ".join(name.split()).casefold() or "tool"


def _tool_label(name: str) -> str:
    key = _tool_key(name)
    if key in _TOOL_LABELS:
        return _TOOL_LABELS[key]
    for prefix, label in _TOOL_PREFIX_LABELS:
        if key.startswith(prefix):
            return label
    return f"🧰 {_clip(name, 64)}"


def _guard_measurement(event: GuardEvent) -> str:
    label = _GUARD_LABELS.get(event.reason, event.reason.value)
    return f"{label} {event.observed}/{event.limit}"


def _format_tokens(tokens: int) -> str:
    bounded = max(0, tokens)
    if bounded >= 1_000_000:
        return f"{bounded / 1_000_000:.1f}m"
    if bounded >= 1_000:
        return f"{bounded / 1_000:.1f}k"
    return str(bounded)


def _clip(text: str, limit: int) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: max(1, limit - 1)]}…"
