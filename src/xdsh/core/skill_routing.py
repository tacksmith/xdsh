"""Bounded metadata-only Skill ranking, without a model or network request.

Concept aliases cover common workflows, not arbitrary multilingual semantics.
Bodies never contribute to relevance: imperative text is not routing evidence.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

_STOP = frozenset(
    "a an and are as at be by can do for from in is it of on or the to use with "
    "please task skill skills workflow procedure help using current".split()
)
_CJK_STOP = frozenset({"请帮", "帮我", "请你", "一下", "任务", "使用", "进行", "需要", "幫我"})
_CONCEPTS = {
    "paper": ("paper", "papers", "论文", "論文"),
    "reproduction": ("reproduce", "reproduction", "replicate", "replication", "复现", "復現"),
    "audit": ("audit", "auditing", "审计", "審計", "核验", "核驗"),
    "testing": ("test", "tests", "testing", "pytest", "测试", "測試", "回归", "回歸"),
    "research": ("research", "retrieval", "search", "检索", "檢索", "搜索", "搜尋", "调研", "調研"),
    "model": ("model", "models", "llm", "模型"),
    "availability": ("availability", "available", "上线", "上線", "可用"),
    "plan": ("plan", "subscription", "套餐", "订阅", "訂閱"),
    "memory": ("memory", "memories", "记忆", "記憶"),
    "deployment": ("deploy", "deployment", "部署"),
    "speech": ("speech", "transcription", "transcribe", "语音", "語音", "转写", "轉寫"),
    "debugging": ("debug", "debugging", "bug", "bugs", "故障", "排错", "排錯", "调试", "調試"),
    "cleanup": ("cleanup", "clean", "清理"),
    "security": ("security", "vulnerability", "安全", "漏洞"),
}


@dataclass(frozen=True)
class SkillMatch:
    name: str
    score: float
    explicit: bool
    evidence: frozenset[str]


def _terms(text: str) -> set[str]:
    text = text.casefold()
    terms = set(re.findall(r"[a-z0-9]+", text)) - _STOP
    for span in re.findall(r"[\u3400-\u9fff]+", text):
        terms.update(
            span[i : i + 2] for i in range(len(span) - 1) if span[i : i + 2] not in _CJK_STOP
        )
    for concept, aliases in _CONCEPTS.items():
        if any(
            re.search(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])", text)
            if alias.isascii()
            else alias in text
            for alias in aliases
        ):
            terms.add("concept:" + concept)
    return terms


def _evidence(terms: set[str]) -> set[str]:
    result = set(terms)
    for concept, aliases in _CONCEPTS.items():
        if "concept:" + concept in result:
            result.difference_update(aliases)
    return result


def rank_skills(
    task: str, metadata: Sequence[tuple[str, str]], *, limit: int = 2
) -> tuple[SkillMatch, ...]:
    """Prefer explicit enabled candidates, then specific relevant workflows.

    Callers enforce discovery/approval/disabled boundaries. Explicit exclusion
    wins within this turn. A second automatic match must add routing evidence.
    """
    if limit <= 0:
        return ()
    # Quoted fenced examples are not direct Skill invocation instructions.
    intent = re.sub(r"```.*?```", " ", task[:32_000], flags=re.DOTALL)
    task_terms = _terms(intent)
    candidates = [
        (name, _terms(name), _terms(name + " " + description[:2000]))
        for name, description in metadata
    ]
    frequencies = Counter(term for _, _, terms in candidates for term in terms)
    weights = {
        term: 1 + math.log((len(candidates) + 1) / (count + 1))
        for term, count in frequencies.items()
    }
    matches: list[SkillMatch] = []
    for name, name_terms, terms in candidates:
        target = r"[`\"]?" + re.escape(name) + r"(?![a-z0-9_-])"
        if re.search(
            r"(?:do not|don't|never|without|不要|禁止|别|別|不使用|不调用|不調用)"
            r"\s*(?:(?:use|apply|load|使用|调用|調用|加载|載入)\s*)?" + target,
            intent,
            re.IGNORECASE,
        ):
            continue
        explicit = bool(
            re.search(
                r"(?:^|[.!?;\n。\uFF1B])\s*(?:please\s+|请|請)?(?:优先|優先)?"
                r"(?:use\s+|apply\s+|使用\s*|调用\s*|調用\s*)" + target,
                intent,
                re.IGNORECASE,
            )
        )
        overlap = task_terms & terms
        name_overlap = task_terms & name_terms
        # An alias and its original word are one piece of evidence, not two.
        evidence = _evidence(overlap)
        named = bool(
            re.search(
                r"(?<![a-z0-9_-])" + re.escape(name) + r"(?![a-z0-9_-])", intent, re.IGNORECASE
            )
        )
        if not explicit and not (
            len(evidence) >= 2 or named or (name_overlap and len(_evidence(name_terms)) == 1)
        ):
            continue
        if not explicit and not evidence:
            continue
        score = sum(weights[t] * (1.5 if t in name_terms else 1) for t in overlap)
        score /= math.sqrt(max(1, sum(weights[t] for t in terms)))
        matches.append(SkillMatch(name, score, explicit, frozenset(evidence)))
    matches.sort(key=lambda item: (-item.explicit, -item.score, item.name))
    selected: list[SkillMatch] = []
    covered: set[str] = set()
    for match in matches:
        if selected and not match.explicit and not (match.evidence - covered):
            continue
        selected.append(match)
        covered.update(match.evidence)
        if len(selected) == limit:
            break
    return tuple(selected)
