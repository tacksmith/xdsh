"""Small, stable semantic icon catalog for user-facing XDSH surfaces.

Icons are presentation metadata, not model-generated content.  Callers use a
semantic key and receive a bounded, deterministic symbol; unknown keys fall
back to a neutral marker instead of leaking arbitrary emoji into output.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, Literal

IconIntensity = Literal["off", "standard", "aggressive", "llm"]
ICON_INTENSITIES: Final[tuple[IconIntensity, ...]] = (
    "off",
    "standard",
    "aggressive",
    "llm",
)


def canonical_icon_intensity(value: str) -> IconIntensity:
    """Normalize the bounded Telegram icon policy, including friendly aliases."""

    aliases = {
        "none": "off",
        "minimal": "standard",
        "normal": "standard",
        "high": "aggressive",
        "auto": "llm",
        "model": "llm",
        "自由": "llm",
        "free": "llm",
        "llm-free": "llm",
        "自由注入": "llm",
        "关闭": "off",
        "关": "off",
        "激進": "aggressive",
    }
    normalized = value.strip().casefold()
    normalized = aliases.get(normalized, normalized)
    if normalized not in ICON_INTENSITIES:
        raise ValueError("unsupported icon intensity")
    return normalized  # type: ignore[return-value]

_ICONS: Final[Mapping[str, str]] = MappingProxyType({
    "phase.analysis": "🔍",
    "phase.reasoning": "🧠",
    "phase.retry": "🔁",
    "phase.rate_limit": "🚦",
    "phase.output": "✍️",
    "phase.tool": "⚙️",
    "phase.compaction": "🗜️",
    "phase.cleanup": "🧹",
    "phase.subagent": "🤖",
    "phase.ready": "🚀",
    "phase.warning": "⚠️",
    "phase.error": "❌",
    "phase.plan": "📋",
    "phase.guard": "🛡️",
    "phase.stop": "🛑",
    "phase.finished": "⏹️",
    "phase.success": "✅",
    "tool.terminal": "💻",
    "tool.read": "📖",
    "tool.edit": "✏️",
    "tool.write": "📝",
    "tool.files": "📂",
    "tool.search": "🔎",
    "tool.todo": "✅",
    "tool.web": "🌐",
    "tool.fetch": "🔗",
    "tool.browser": "🌐",
    "tool.subagent": "🤖",
    "tool.image": "🖼️",
    "tool.memory": "🧠",
    "tool.skill": "🧩",
    "tool.mcp": "🔌",
    "artifact.create": "🆕",
    "artifact.update": "✏️",
    "artifact.delete": "🗑️",
    "status.info": "💡",
    "status.question": "❓",
})


def semantic_icon(key: str, *, fallback: str = "🔹") -> str:
    """Return the catalogued icon for a semantic key."""

    return _ICONS.get(key, fallback)


def icon_catalog() -> Mapping[str, str]:
    """Expose the read-only catalog for tests and alternate transports."""

    return _ICONS
