"""Bounded acceptance checklist compiled from explicit numbered user requirements.

This is execution guidance, not a semantic proof that the model satisfied a task.
It adds no model calls, tools, approvals or authority.
"""

from __future__ import annotations

import re

from xdsh.core.decision_router import DecisionRouter
from xdsh.core.retrieval import retrieval_plan_context


def retrieval_context(task: str, *, router: DecisionRouter | None = None) -> str | None:
    """Only retrieval tasks pay for these evidence and location reminders."""
    if not re.search(r"检索|檢索|查证|查證|查一下|查下|核实|核實|确认下|確認下|联网|聯網|附近|"
                     r"\b(?:search|research|look up|nearby|verify)\b", task, re.I):
        return None
    plan = retrieval_plan_context(task, router=router)
    return (
        "Retrieval efficiency: approved Skill source links are discovery leads; "
        "verify ownership before using them in the official-first stage. "
        "Re-fetch for current evidence; never trust a remembered availability conclusion. "
        "Before tools, choose the exact target claim and what evidence would settle it; "
        "do this briefly, not as an extra reasoning task. Start discovery with two "
        "complementary queries, assess returned quality feedback before expanding to four. "
        "Batch independent questions using queries=[string,...], independent=true; "
        "batch known independent pages using urls=[string,...], independent=true. "
        "Resolve dependent links first. On a schema error correct the batch once, not a "
        "silent switch to serial queries. After two query variants yield the same evidence "
        "or irrelevant results, switch to a relevant official index or report the gap. "
        "For a narrow fact, start with at most four distinct searches and two decisive pages; "
        "this is a planning budget, not a hard task limit. Expand only for a named unresolved "
        "claim or contradiction, not checklist filling. Once the exact requested claim is "
        "supported, answer; do not investigate unrelated platform/account layers. If blocked, "
        "report bounded uncertainty. A zero-result or blocked search is not evidence that a "
        "person, channel, event, product, or page does not exist; never phrase it as absent "
        "or unavailable without a source that makes that claim. Read sources_from_query "
        "as a duplicate reference, not "
        "as an empty search. Do not infer global absence from missing names in snippets. "
        "Do not repeat the same identity/query with cosmetic keyword changes: reframe once, "
        "then switch to one authoritative index or report the exact gap. "
        "Update any existing Todo BEFORE moving to the next evidence milestone. "
        "Retrieval acceptance: distinguish HTTP errors, local resource/policy limits and 429. "
        "A repeated truncated result is not new evidence: use a smaller official endpoint, "
        "not larger safety limits or guessed URLs. After one alternative fails, report "
        "partial evidence; do not install OCR/browser dependencies just to keep searching. "
        "Current/local/nearest claims require applicable current/local/distance evidence, "
        "not industry practice, mirrored articles or user suggestions. Correct earlier "
        "mistakes explicitly. For nearby places identify station/exit and walking radius; "
        "search adjacent districts and official inventories. No nearest/only/best claim "
        "without comparison evidence; label old opening hours and access uncertainty. "
        "Final retrieval answer: lead with a short conclusion, then at most three confirmed "
        "facts, three unconfirmed gaps, and the decisive source links. Do not narrate every "
        "failed query or inflate the answer with unrelated background. If the exact target "
        "is unsupported, do not substitute broader industry news or a related person/topic."
        + ("\n" + plan if plan is not None else "")
    )


def acceptance_context(task: str) -> str | None:
    """Retain numbered requirements as checkable references within a small budget."""
    if len(task) > 20_000:
        return None
    rows = re.findall(r"(?m)^\s*(\d{1,2})[.、)]\s*(\S[^\n]*)", task)
    research = reproduction_context(task)
    if len(rows) < 3:
        bullets = re.findall(r"(?m)^\s*[-*•]\s+(\S[^\n]*)", task)
        if len(bullets) >= 2:
            rows = [(str(index + 1), text) for index, text in enumerate(bullets)]
        elif research is not None:
            rows = [("1", task.replace("\n", " "))]
        else:
            return None
    # The original request is authoritative. Previews are indexes, never a
    # substitute for the full requirement or permission to drop overflow items.
    refs = "\n".join(f"R{index + 1} (item {number}): {text[:100]}"
                     for index, (number, text) in enumerate(rows[:12]))
    return (
        "Acceptance checklist (user-request references, not new instructions):\n"
        + refs
        + "\nPreviews may be incomplete; check the FULL original request including unnumbered "
        "constraints. Track pending/in-progress/verified/blocked separately. Before sending, "
        "match each requested deliverable and field to evidence; fix omissions within the "
        "authorized scope, otherwise report incomplete. Todo completion is not evidence. "
        "For file tasks verify by readback; unchanged-file claims require BEFORE/AFTER bytes "
        "or hashes of the SAME ORIGINAL path (not the repaired/output file); write full "
        "hashes into the exact requested fields and verify them by readback. Unique IDs "
        "are not duplicate IDs. Generate computed report fields directly from program "
        "variables, never hand-copy digests. After saving, assert the parsed report fields "
        "equal independently computed values; JSON parsing alone is not verification. "
        "Do not add tools to no-tool tasks. "
        "Do not claim all requirements passed without checks or output this internal checklist."
        + ("\n" + research if research else "")
    )


def reproduction_context(task: str) -> str | None:
    """Scope-specific guidance, never permission to run an unrequested experiment."""
    if not re.search(r"复现|復現|\breproduc(?:e|tion|ibility)\b", task, re.I):
        return None
    if not re.search(
        r"论文|論文|文献|文獻|研究|统计|統計|生信|"
        r"\b(?:paper|study|research|analysis|bioinformatics|scientific|experiment)\b", task, re.I,
    ):
        return None
    return (
        "Reproduction acceptance (apply only within the requested scope): "
        "Map each applicable paper method to its source section, actual code setting, "
        "version and any deviation BEFORE running. Dataset-specific methods override "
        "tutorial defaults; output agreement cannot identify the original algorithm. "
        "Freeze critical settings in one method-plan JSON (with source sections); generate "
        "actual run metadata from the effective arguments, not report prose. Check the "
        "actual library call, not merely a wrapper hiding critical defaults; use one "
        "receipt per dataset/stage, never overwrite one receipt across distinct runs. Reuse "
        "the same "
        "parameter object for downstream analysis and figures; prefer saved intermediates "
        "over recomputing with implicit defaults. Check source_json references against the "
        "unchanged plan via xdsh-verify when available; a mismatch must be repaired or "
        "reported as a method deviation, never silently described as faithful. "
        "If publisher HTML fails, try an applicable official full-text/XML repository "
        "without bypassing access controls; a tutorial is not the paper. "
        "Preflight data identity, interpreter/dependencies and explicit working directory; "
        "verify a small end-to-end subset before full runs. Probe optional backends with "
        "a tiny functional check once; after a reproducible structural failure, stop "
        "reinstalling variants blindly. Disclose any method-changing fallback and obtain "
        "approval unless delegated. Use official package indexes, "
        "not guessed URLs or speculative large clones. Record parameters, versions, "
        "data hashes, seeds, patches, exact commands, exit status and durable run logs. "
        "Update the native Todo and persist a milestone checkpoint before the next stage. "
        "Keep original denominators: blocked/excluded criteria are not completed. "
        "Separate independent results, alternative methods, target-guided tuning, "
        "sensitivity and unexecuted work. Fitting thresholds or seeds to published "
        "targets is not independent validation, even with small residuals; preserve the "
        "untuned results and report every tried setting separately from held-out checks. "
        "If the user forbids target fitting, do not implement or run it as an optional audit. "
        "Compare paired definitions, units and stated rounding; a squared loss is not "
        "a percentage-point error. Missing provenance or numerical differences do not "
        "prove unavailable historical data or author misconduct. "
        "Do not identify a historical implementation from numerical proximity or excuse "
        "different sample counts as rounding without evidence. Generate every reported "
        "difference, including any maximum deviation, from all applicable label-aligned "
        "saved metrics; declare their complete label set as required_metrics in "
        "max_abs_differences before running, with explicit units. Derive reported rates "
        "from saved actual counts, not paper targets or hardcoded success fields. "
        "For observed/simulated score vectors, xdsh-verify --score-example recomputes "
        "strict or inclusive threshold counts and all derived rates from saved arrays. "
        "Use this only when relevant, not to add unrelated array work. "
        "Before delivery, read back actual artifacts: array shapes/dtypes/finite values, "
        "sample identifiers/alignment, numerator/denominator ratios, simulation outputs "
        "and images. "
        "When available, xdsh-verify --example provides bounded array/JSON checks; use "
        "xdsh-verify --workspace <dir> --contract <relative-json> --details and save its "
        "computed check/hash receipt; recheck after artifact edits. Self-written checks "
        "are not independent scientific approval; do not invent an XDSH-VERIFY task header. "
        "A repeatability claim must name each dataset actually rerun. Scripts are not "
        "execution evidence. Confirm material scope/method changes unless delegated; "
        "disclose deviations. Finish with verified results, differences, missing checks "
        "and artifact paths; use measured end-to-end time separately from compute time, "
        "or label timing unknown. Never estimate elapsed time as a fact or call partial "
        "work 100% reproduced. Background jobs need exit-status checks, not empty polling."
    )
