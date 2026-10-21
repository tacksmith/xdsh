"""Conservative, local answer-format repair; never rewrite or truncate claims."""
# ruff: noqa: RUF001

from __future__ import annotations

import json
import re
from dataclasses import dataclass

_JSON_ONLY = re.compile(
    r"(?:只|仅|僅)(?:输出|輸出|返回|回傳)\s*(?:一个|一個)?\s*(?:合法)?\s*JSON|"
    r"(?:return|output)\s+(?:only\s+)?(?:a\s+)?(?:valid\s+)?JSON\s+only|"
    r"(?:return|output)\s+only\s+(?:a\s+)?(?:valid\s+)?JSON|JSON\s+only", re.IGNORECASE,
)
_FENCE = re.compile(r"\A```(?:json)?[ \t]*\r?\n(.*?)\r?\n```\Z", re.DOTALL | re.IGNORECASE)


@dataclass(frozen=True)
class AnswerLengthCheck:
    """Deterministic raw Unicode codepoint count, including layout/Markdown.

    Not a token, word, grapheme or Telegram rendered-glyph measurement. Keep
    this diagnostic separate from the answer; never delete facts to satisfy it.
    """

    count: int
    limit: int

    @property
    def exceeded(self) -> bool:
        return self.count > self.limit


def check_answer_length(request: str, answer: str) -> AnswerLengthCheck | None:
    """Check a single explicit whole-response character cap conservatively."""
    if len(request) > 20_000 or any(mark in request for mark in ('"', '“', '「', '`')):
        return None
    # Section-specific and multiple limits need a richer contract; guessing a
    # whole-answer cap from those would produce misleading failure notices.
    if re.search(r"标题|標題|正文|每[段行条條]|title|each", request, re.I):
        return None
    matches = re.findall(
        r"(?<!\d)([1-9]\d{0,3})\s*(?:字|个字符|個字元)(?:以)?[内內]|"
        r"(?:不超过|不超過|最多)\s*([1-9]\d{0,3})\s*(?:字|个字符|個字元)|"
        r"(?:at most|within)\s+([1-9]\d{0,3})\s+characters\b",
        request, re.I,
    )
    if len(matches) != 1:
        return None
    limit = int(next(value for value in matches[0] if value))
    return AnswerLengthCheck(count=len(answer), limit=limit)


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("ambiguous JSON")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("non-JSON constant")


def requests_json_only(request: str) -> bool:
    return _JSON_ONLY.search(request) is not None


def format_answer(request: str, answer: str) -> str:
    """Repair presentation, preserving every JSON token (including precision)."""
    if len(answer) > 30_000:
        return answer
    if _JSON_ONLY.search(request):
        fence = _FENCE.fullmatch(answer.strip())
        value = fence.group(1) if fence else answer.strip()
        try:
            json.loads(value, object_pairs_hook=_unique, parse_constant=_reject_constant,
                       parse_float=str, parse_int=str)
        except (ValueError, RecursionError):
            return answer
        if re.search(r"单行|單行|原样|原樣|single.line|verbatim|minif", request, re.IGNORECASE):
            return value
        return _pretty_json_tokens(value)
    answer = _prose_sections(request, answer)
    match = re.search(r"(?<!\d)([1-9]\d{0,3})\s*字(?:以)?内|"
                      r"(?<!\d)([1-9]\d{0,3})\s*字(?:以)?內", request)
    if not match or len(answer) <= int(match.group(1) or match.group(2)):
        return answer
    # Code, literal JSON, tables and explicitly requested list structure are not
    # prose layout. Do not touch them even when they exceed a requested cap.
    if any(mark in answer for mark in ("`", "~~~", "|", "{", "[")) or re.search(
        r"编号|編號|列表|逐条|逐條|numbered|list", request, re.IGNORECASE,
    ):
        return answer
    compact = re.sub(r"(?m)^\d+[.)] +", "", answer)
    # Only whitespace beside CJK punctuation/text is removable without joining
    # English words. All dates, identifiers, negations and clauses stay intact.
    compact = re.sub(r"(?<=[。；！？\u4e00-\u9fff])\s+|\s+(?=[\u4e00-\u9fff])", "", compact)
    limit = int(match.group(1) or match.group(2))
    return compact if len(compact) <= limit else answer


def _pretty_json_tokens(value: str) -> str:
    # Validate first, then only alter whitespace OUTSIDE string literals. Using
    # json.dumps(json.loads(...)) would round precise decimals and change escapes.
    tokens = re.findall(r'"(?:[^"\\]|\\.)*"|[^\s"{}\[\],:]+|[{}\[\],:]', value)
    output: list[str] = []
    depth = 0
    for index, token in enumerate(tokens):
        before = tokens[index - 1] if index else ""
        after = tokens[index + 1] if index + 1 < len(tokens) else ""
        if token in ("{", "["):
            output.append(token)
            depth += 1
            if after not in ("}", "]"):
                output.append("\n" + "  " * depth)
        elif token in ("}", "]"):
            depth -= 1
            if before not in ("{", "["):
                output.append("\n" + "  " * depth)
            output.append(token)
        elif token == ",":
            output.append(",\n" + "  " * depth)
        elif token == ":":
            output.append(": ")
        else:
            output.append(token)
        if sum(map(len, output[-2:])) > 30_000 or depth > 64:
            return value
    result = "".join(output)
    return result if len(result) <= 30_000 else value


def _prose_sections(request: str, answer: str) -> str:
    """Space existing sections, never invent structure inside code or exact output."""
    if re.search(r"原样|原樣|逐字|单行|單行|\d+\s*行|verbatim|exact|CSV", request, re.I):
        return answer
    labels = {"已完成", "待办", "待辦", "未验证", "未驗證", "部署前提", "工作进展",
              "工作進展", "发布阻塞项", "發布阻塞項", "下一步", "结论", "結論",
              "关键依据", "關鍵依據", "官方来源", "官方來源", "仍生效约束", "仍生效約束",
              "未确认项", "未確認項", "可否部署", "待明确", "待明確", "验证结果",
              "驗證結果", "completed", "next step", "next steps", "constraints", "sources",
              "summary", "verification", "pending", "limitations"}
    output: list[str] = []
    fence: str | None = None
    for line in answer.split("\n"):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if marker:
            run = marker.group(1)
            if fence is None:
                fence = run
            elif run[0] == fence[0] and len(run) >= len(fence):
                fence = None
            output.append(line)
            continue
        if fence is not None or line.startswith(("    ", "\t")) or "`" in line:
            output.append(line)
            continue
        heading = re.fullmatch(r"(?:#{1,6} +(.{1,40})|\*\*([^*\n]{1,40})\*\*)", line.strip())
        label = line.strip().rstrip(":：")
        if heading or label.casefold() in labels:
            title = (heading.group(1) or heading.group(2)) if heading else label
            if output and output[-1].strip():
                output.append("")
            output.extend([f"**{title}**", ""])
        elif not line.strip() and output and not output[-1].strip():
            continue
        else:
            # Separate top-level inline section labels and the beginning of a
            # list from preceding prose; do not space every list item or guess
            # boundaries inside indented/fenced code or exact-output contracts.
            inline_heading = re.match(r"^\*\*[^*\n]{1,40}\*\*\s*\S", line)
            list_start = re.match(r"^(?:[-+*] |\d+[.)] )", line)
            previous_list = bool(output and re.match(r"^(?:[-+*] |\d+[.)] )", output[-1]))
            if output and output[-1].strip() and (
                inline_heading or (list_start and not previous_list)
            ):
                output.append("")
            output.append(line)
    return "\n".join(output).strip()
