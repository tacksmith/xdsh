"""Small, content-free failure contract for bounded retrieval decisions."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, replace
from typing import Literal, cast

from xdsh.core.decision_router import (
    DecisionOption,
    DecisionRequest,
    DecisionResult,
    DecisionRouter,
)
from xdsh.core.execution_policy import OFFICIAL_SOURCE_ORDER
from xdsh.core.external import (
    ExternalAuthError,
    ExternalBusyError,
    ExternalHttpError,
    ExternalPolicyError,
    ExternalResourceLimitError,
    ExternalTimeoutError,
    JsonValue,
)

_SOURCE_STAGE_POLICY = (
    OFFICIAL_SOURCE_ORDER + " Verify ownership and freshness; official does not mean infallible. "
    "Search engines and saved links are discovery leads; inspect relevant official pages first. "
    "Use other sources afterward as needed for gaps or cross-checks. "
    "Batch only within each source stage; a blocked official source is a gap, not proof of absence."
)


@dataclass(frozen=True, slots=True)
class RetrievalPlan:
    """Deterministic retrieval gate shared by model prompts and adapters."""

    mode: Literal["dependent", "independent", "mixed"]
    max_queries: int = 4
    max_parallel: int = 2
    source_priority: tuple[str, ...] = (
        "official primary source",
        "authoritative registry or standards body",
        "reputable secondary source",
        "search snippet (discovery only)",
    )


def retrieval_decision_request(task: str, plan: RetrievalPlan) -> DecisionRequest:
    """Build a content-free decision request for optional shadow evaluation."""

    fallback = {
        "dependent": "serial",
        "independent": "parallel",
        "mixed": "staged",
    }[plan.mode]
    return DecisionRequest(
        kind="retrieval_plan",
        scope_id=hashlib.sha256(task.encode("utf-8")).hexdigest()[:16],
        options=(
            DecisionOption("serial", "Resolve prerequisite sources in order."),
            DecisionOption("parallel", "Read independent sources concurrently."),
            DecisionOption("staged", "Resolve prerequisites, then batch independent reads."),
        ),
        fallback=fallback,
        context=(
            ("mode", plan.mode),
            ("max_queries", plan.max_queries),
            ("max_parallel", plan.max_parallel),
        ),
    )


def evaluate_retrieval_plan(
    task: str,
    *,
    router: DecisionRouter,
) -> DecisionResult | None:
    """Evaluate one plan without changing retrieval behavior.

    This is intentionally an observation seam.  The caller decides whether to
    persist metrics; no model decision is allowed to alter the current plan.
    """

    plan = plan_retrieval(task)
    if plan is None:
        return None
    return router.decide(retrieval_decision_request(task, plan))


def plan_retrieval(task: str) -> RetrievalPlan | None:
    """Classify retrieval dependencies before the model starts searching.

    This is intentionally lexical and bounded: it does not claim semantic
    certainty or perform a search.  The model still decides the exact queries,
    while the plan prevents dependent pages from being fanned out prematurely.
    """

    if not isinstance(task, str) or not task.strip():
        return None
    if not re.search(
        r"检索|檢索|查证|查證|查一下|查下|查|核实|核實|确认下|確認下|联网|聯網|"
        r"\b(?:search|research|look up|verify|sources?)\b",
        task,
        re.I,
    ):
        return None
    independent = re.search(
        r"分别|分別|各自|多个|多個|多源|并行|並行|对比|對比|compare|multiple|parallel|sources",
        task,
        re.I,
    ) is not None
    dependent = re.search(
        r"然后|然後|之后|之後|根据|根據|链接|連結|详情|詳情|具体|具體|follow|then|after",
        task,
        re.I,
    ) is not None
    mode: Literal["dependent", "independent", "mixed"] = (
        "mixed" if independent and dependent else "independent" if independent else "dependent"
    )
    return RetrievalPlan(mode=mode)


def retrieval_plan_context(task: str, *, router: DecisionRouter | None = None) -> str | None:
    """Render a compact, model-facing plan without adding another reasoning task."""

    plan = plan_retrieval(task)
    if plan is None:
        return None
    if router is not None and router.mode == "integrated":
        # The main agent already has the full task and structured tool results.
        # Keep this an execution constraint, not a second planning task: long
        # planning prompts can trigger unnecessary reasoning and malformed
        # structured output on providers with hidden reasoning streams.
        if plan.mode == "independent":
            return (
                "Execution constraints: reads are independent; run at most two "
                "concurrently and four distinct queries. " + _SOURCE_STAGE_POLICY
                + " Do not add a planning report; return supported claims "
                "or the exact evidence gap."
            )
        if plan.mode == "dependent":
            return retrieval_plan_context(task, router=None)
        # Mixed tasks already have a deterministic staged plan.  Keep the
        # host-owned prerequisite ordering and avoid a second LLM plan that can
        # expand reasoning without improving the tool schedule.
        return retrieval_plan_context(task, router=None)
    if router is not None:
        result = router.decide(retrieval_decision_request(task, plan))
        if router.mode == "active" and result.source == "llm":
            # Never turn host-detected prerequisite work into a parallel batch.
            # Only independent work may opt into staged/serial execution.
            if result.choice == "serial":
                plan = replace(plan, mode="dependent")
            elif plan.mode == "independent" and result.choice == "staged":
                plan = replace(plan, mode="mixed")
    if plan.mode == "dependent":
        flow = "Resolve the prerequisite source first; fetch dependent pages serially."
    elif plan.mode == "independent":
        flow = "Batch up to four independent queries/pages; use at most two concurrent reads."
    else:
        flow = "Resolve the prerequisite first, then batch only the independent follow-up reads."
    priority = " > ".join(plan.source_priority)
    return (
        "Retrieval plan (pre-action gate): " + _SOURCE_STAGE_POLICY + "\n" + flow + "\n"
        f"Source priority: {priority}.\n"
        "Stop after the requested claim is supported; retain partial evidence on failure."
    )


def assess_search_batch(rows: list[dict[str, object]]) -> dict[str, object]:
    """Compact byte-equivalent evidence within one batch, never across users.

    Identifier matching is a diagnostic, not a semantic relevance verdict.
    Aliases and incomplete snippets cannot establish model absence.
    """
    seen: dict[str, str] = {}
    duplicates = unmatched = 0
    for row in rows:
        sources = cast(list[dict[str, str]], row.get('sources', []))
        if not sources:
            continue
        query = str(row['query'])
        identifiers = list(dict.fromkeys(
            token for token in re.findall(r'\b[a-z][a-z0-9]*(?:[-.][a-z0-9]+)+\b', query.lower())
            if any(char.isdigit() for char in token)
        ))[:8]
        evidence = json.dumps(sources, ensure_ascii=False).casefold()
        missing = [token for token in identifiers if token not in evidence]
        row['quality'] = {'unmatched_identifiers': missing,
                          'identifier_match_is_not_availability_proof': True}
        unmatched += bool(missing)
        # Include titles/snippets, not just URLs: different extracts may carry
        # new evidence even when the same document appears in multiple queries.
        fingerprint = json.dumps(sources, ensure_ascii=False, sort_keys=True)
        if fingerprint in seen:
            row['sources'] = []
            row['sources_from_query'] = seen[fingerprint]
            duplicates += 1
        else:
            seen[fingerprint] = query
    return {
        'duplicate_source_lists': duplicates,
        'queries_with_unmatched_identifiers': unmatched,
        'next_action': (
            'Low-yield evidence: consult sources_from_query for compacted duplicates. '
            'Missing identifiers are not evidence of absence (aliases/snippets may differ). '
            'Do not issue more cosmetic query variants; use a different source, preferably '
            'a known official plan/index link. If unavailable, report the concrete gap.'
            if duplicates or unmatched else
            'Inspect applicable primary evidence; answer when the requested claim is supported. '
            'Search success alone does not establish completeness or availability.'
        ),
    }


def retrieval_failure(error: Exception) -> dict[str, JsonValue]:
    """Use types/status, never provider error prose, to recommend a safe next step."""

    status: int | None = None
    retryable = False
    action = "Use one different authorized source, or report unconfirmed. Do not bypass safety."
    if isinstance(error, ExternalBusyError):
        kind, retryable = "busy", True
        action = "Wait briefly; retry at most once. Do not create parallel retries."
    elif isinstance(error, ExternalHttpError):
        status = error.status
        kind = "http_error"
        if status in {401, 403}:
            kind = "access_denied"
            action = "No credential hunting. Use an authorized source or report unconfirmed."
        elif status == 429 or 500 <= status <= 599:
            retryable = True
            action = "Wait briefly; retry at most once, then retain partial evidence."
        elif status in {404, 410}:
            kind = "not_found"
            action = (
                "Do not guess more URL variants or repeat this request. "
                "Find a published link from an official index, or report unconfirmed."
            )
        elif status in {406, 415}:
            kind = "request_incompatible"
            action = (
                "Check adapter request headers and the endpoint's supported media types; "
                "this status is not rate limiting. Do not repeat the unchanged request. "
                "Use one compatible authorized source or report the integration problem."
            )
    elif isinstance(error, ExternalAuthError):
        kind = "authentication_unavailable"
        action = "Report unconfirmed. Do not inspect secrets or discover alternate credentials."
    elif isinstance(error, ExternalTimeoutError | TimeoutError | asyncio.TimeoutError):
        kind, retryable = "timeout", True
    elif isinstance(error, ExternalResourceLimitError):
        kind = "resource_limit"
        action = "Use a smaller official page or report incomplete; do not increase safety limits."
    elif isinstance(error, ExternalPolicyError):
        kind = "policy_denied"
    else:
        kind = "unavailable"
    return {"kind": kind, "retryable": retryable, "http_status": status,
            "max_retries": 1 if retryable else 0, "next_action": action}
