"""Scoped automatic research delivery checks; no inference of scientific truth."""
from __future__ import annotations

import json
import re
import shlex
import uuid
from pathlib import Path

from xdsh.core.artifact_acceptance import PREFIX
from xdsh.core.task_contract import reproduction_context


def automatic_research_contract(task: str, *, workspace: Path) -> tuple[str, str] | None:
    """Compile execution requests, not discussion or no-tool tasks (Chinese/English).

    The unique manifest prevents accidentally checking an earlier turn. Its
    contents are declared checks, not independently trusted ground truth.
    """
    if len(task) > 20_000 or reproduction_context(task) is None:
        return None
    if re.search(
        r"不要(?:运行|運行|执行|執行|复现|復現|调用|調用)|只(?:解释|解釋|讨论|討論|阅读|閱讀|总结|總結|提供方案)|"
        r"\b(?:do not|don't|without)\s+(?:run|execute|reproduce|tools)|"
        r"\b(?:no tools|no execution|plan only)\b", task, re.I,
    ):
        return None
    # Discussion intent is scoped to the lead clause: 'execute, then explain
    # deviations' must not quietly bypass the gate because of its report wording.
    lead = re.split(r"[\n。\uff01\uff1f.!?\uff1b;\uff0c,]", task.lstrip(), maxsplit=1)[0]
    if re.search(
        r"解释|解釋|讨论|討論|如何|怎么|怎麼|能否|是否|"
        r"\b(?:explain|discuss|summarize|how to|can we|can you)\b", lead, re.I,
    ):
        return None
    if not re.search(
        r"(?:请|請|实际|實際|开始|開始|执行|執行|继续|繼續|严格|嚴格|精准|精準).{0,24}(?:复现|復現)|"
        r"(?:^|[\n.!?])\s*(?:please\s+)?(?:strictly\s+)?reproduce\b", task, re.I,
    ):
        return None
    path = f".xdsh-research-{uuid.uuid4().hex}.json"
    header = PREFIX + json.dumps({"research_revision": 2, "unchanged": [], "json": [], "deferred": {
        "path": path, "profile": "research",
    }})
    root = workspace.resolve()
    command = (f"xdsh-verify --workspace {shlex.quote(str(root))} --contract {path} "
               "--profile research --research-revision 2")
    context = (
        "Research delivery gate (one checklist, not a separate investigation): "
        f"write concrete checks at {root / path}. Evidence paths are relative to "
        f"{root}, NOT a shell subdirectory; prefix analysis-subdirectory paths. "
        "Do not copy the XDSH-VERIFY header or its deferred wrapper into the file; "
        "do not point it to another checklist. "
        "Adapt xdsh-verify --research-example once: calls or source_json method checks "
        "AND numeric/array output checks. Keep mandatory unchanged and json keys. "
        "Optional arrays read numeric .npy, NOT JSON lists; use equals for JSON lists. "
        "Do not create arrays or install NumPy for a JSON-only task. "
        'JSON numeric syntax: "numbers":{"/value":{}} checks finite. Revision 2 forbids '
        'unclassified scalar equals or minimum/maximum targets. Use expectations instead: '
        '"expectations":{"/value":{"kind":"exploratory","bounds":{"maximum":1}}}. '
        'Or use numeric equals instead of bounds inside that expectation. '
        "literature and exploratory comparisons report differences, not hard failures; "
        "integrity and user are hard checks. Do not label a model guess as a user requirement. "
        "For every non-exploratory expectation, reference an unchanged JSON record with "
        "the identical kind and bounds/equals, plus citation and locator (paper location, "
        "actual user request, or structural invariant). Prioritize official methods and code. "
        "The host validates these declarations, not the authenticity of their scientific claims. "
        "Do not put scientific targets in raw arrays/structured equality to bypass classification. "
        "Keep research_revision: 2 and feasibility from --research-example. Before costly work, "
        "estimate dataset units and peak versus available memory using metadata or a small "
        "permitted pilot; use null for unknowns, never invented measurements. Prefer streaming "
        "or staged execution if needed; never silently label a subset as full, raise budgets, "
        "install new resources or alter user scope without authorization. These estimates "
        "are advisory, not a new time/token/resource limit. "
        "Write the checklist and source plan in an initial separate tool call. "
        f"Run {command} --preflight before costly work; apply its specific remedy on failure, "
        "not speculative schema permutations or framework-source audits. Preflight checks "
        "declaration/source availability and declared feasibility, not future outputs. "
        "The host freezes the "
        "first valid declaration: never rewrite, reformat or reserialize that checklist "
        "or unchanged sources. A fresh CLI check cannot reset those frozen hashes. "
        "For simple scripts, record effective-argument metadata via source_json, not "
        "a fabricated call receipt. For critical Python library calls, "
        "xdsh-verify --method-example shows verified_call from xdsh.core.method_evidence: "
        "bind actual parameters BEFORE execution and save distinct receipts. Neither "
        "mode certifies an unobserved implementation or scientific truth. "
        "For critical staged results, --stage-example and --stage-call-example show "
        "ArtifactBinding: freeze the ready code/input files, bind actual method parameters, "
        "installed package versions and saved output hashes; numeric .npy shapes are read "
        "from bytes. Save distinct per-stage checkpoints (ordered IDs, memberships/ranks, "
        "scores or pseudotime as relevant); later transforms take earlier saved outputs as "
        "inputs, never overwrite them or reuse their receipts. Link summaries to current "
        "outputs with artifact_json; id_selections validates mask/index mappings and arrays "
        "axes checks ID counts. These are explicit consistency rules, not new scientific "
        "thresholds. Do not claim unchecked large originals were verified. "
        "Declare finite/nonconstant/range rules only where appropriate; never hide a "
        "failed threshold behind null. JSON/receipt/code reads are bounded to 2 MiB/file. "
        "For larger originals use --large-example: explicit large_files streaming profiles; "
        "raw is hash-only, npy checks numeric data, hdf5 checks declared structure/ordered IDs "
        "and requires the verifier's optional science extra. In bound stages also declare "
        "the identical large_files paths in calls.artifacts and ArtifactBinding. Failed "
        "large-file checks preserve partial results and cannot trigger experiment replay. "
        "Keep large "
        "originals intact, check summaries and disclose what was not checked. "
        f"Finally run {command} --details (same root/file/profile even after cd). "
        "The host independently checks declared artifacts; missing/invalid evidence fails. "
        "Report literature/exploratory deviations even when checks pass; never tune the "
        "experiment merely to match a reference value. A delivery pass is not full reproduction. "
        "A model-written checklist does not prove complete scientific coverage. "
        "Do not fabricate receipts, rewrite the first-line contract or replay completed "
        "operations just to pass. Preserve partial work and report limitations."
    )
    return header, context
