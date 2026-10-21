"""Read-only, bounded artifact checks. No model, network, shell or unpickling."""
from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from xdsh.core.acceptance_store import load_host_baseline
from xdsh.core.artifact_acceptance import (
    PREFIX,
    RESEARCH_REMEDIES,
    AcceptanceError,
    ResearchDeclarationError,
    capture_acceptance,
    read_declared_contract,
)
from xdsh.core.research_feasibility import unknown_feasibility

EXAMPLE = {
    "unchanged": ["method-plan.json"],
    "calls": [{"path": "method-call.json", "source": {
        "path": "method-plan.json", "pointer": "/parameters",
    }}],
    "json": [{"path": "results.json", "equals": {"/target_guided": False},
              "numbers": {"/estimate": {}, "/percent": {"minimum": 0, "maximum": 100}},
              "ratios": [{"value": "/percent", "numerator": "/count",
                          "denominator": "/total", "scale": 100, "atol": 0.001}],
              "max_abs_differences": [{"value": "/max_error_pp", "actual": "/observed",
                                       "reference": "/reference", "scale": 100,
                                       "atol": 0.001}]}],
    "arrays": [{"path": "embedding.npy", "shape": [None, 2], "finite": True},
               {"path": "scores.npy", "shape": [None], "nonconstant": True,
                "minimum": 0, "maximum": 1}],
}

RESEARCH_EXAMPLE = {
    "research_revision": 2,
    "feasibility": unknown_feasibility(),
    "unchanged": ["method-plan.json"],
    "json": [{"path": "results.json", "source_json": {
        "/effective_parameters": {"path": "method-plan.json", "pointer": "/parameters"},
    }, "numbers": {"/estimate": {}}}],
}

SCORE_EXAMPLE = {
    "research_revision": 2,
    "feasibility": unknown_feasibility(),
    "unchanged": ["method-plan.json"],
    "json": [{"path": "results.json", "source_json": {
        "/effective_parameters": {"path": "method-plan.json", "pointer": "/parameters"},
    }, "threshold_rates": [{
        "observed": "observed-scores.npy", "simulated": "simulated-scores.npy",
        "threshold": "/threshold", "comparison": "gt", "count": "/positive",
        "total": "/total", "detected": "/detected_percent", "detectable": "/detectable_percent",
        "overall": "/overall_percent", "scale": 100, "atol": 0.000001,
    }]}],
}

STAGE_EXAMPLE = {
    "research_revision": 2,
    "feasibility": unknown_feasibility(),
    "unchanged": ["method-plan.json", "input.json", "analysis.py"],
    "calls": [{"path": "method-call.json", "source": {
        "path": "method-plan.json", "pointer": "/parameters"}, "artifacts": {
            "inputs": ["method-plan.json", "input.json"], "outputs": ["results.json"],
            "code": ["analysis.py"], "packages": {},
        }}],
    "json": [{"path": "results.json", "numbers": {"/estimate": {}}, "artifact_json": {
        "/sample_ids": {"path": "input.json", "pointer": "/ids"},
    }}],
}

LARGE_EXAMPLE = {
    "unchanged": ["input.h5ad", "ids.json"], "json": [],
    "large_files": {
        "input.h5ad": {"format": "hdf5", "datasets": {
            "X": {"encoding": "csr", "shape": [None, None], "axes": {
                "0": {"dataset": "obs/_index", "reference": {
                    "path": "ids.json", "pointer": "/cells"}},
                "1": {"dataset": "var/_index", "reference": {
                    "path": "ids.json", "pointer": "/genes"}},
            }},
        }},
        "embedding.npy": {"format": "npy", "shape": [None, 2], "finite": True},
    },
}

STAGE_CALL_EXAMPLE = '''# Save as analysis.py BEFORE preflight. Adapt, do not run dummy science.
import json
from pathlib import Path
from xdsh.core.method_evidence import ArtifactBinding, verified_call

def analyze(values, *, scale=1.0):
    return sum(values) * scale

root = Path.cwd()  # host-workspace root, including after any later chdir
plan = json.loads((root / "method-plan.json").read_text())["parameters"]
data = json.loads((root / "input.json").read_text())
# input.json: {"ids": ["a", "b"], "values": [2.0, 4.0]}
# method-plan.json: {"parameters": {"scale": 2.0}}
def save(result):
    (root / "results.json").write_text(json.dumps({
        "estimate": result, "sample_ids": data["ids"]}))

binding = ArtifactBinding(
    "method-call.json", inputs=("method-plan.json", "input.json"),
    outputs=("results.json",), code=("analysis.py",), packages=(), save_result=save)
verified_call(analyze, plan, binding, data["values"], scale=plan["scale"])
# For a mutating library call, save_result can serialize its in-memory object.
# Add actual distribution names (e.g. scanpy) to packages and matching version
# pins to the contract. The helper reads installed metadata; it never installs.
# Each next stage uses a DISTINCT receipt/output, taking the previous saved
# output as input. Do not overwrite an earlier checkpoint or handcraft receipts.
# This records file consistency, not proof that an input was really consumed,
# or that save_result performed serialization only. Keep original large data;
# For large originals use --large-example and matching large_files tuples in
# ArtifactBinding and the calls.artifacts declaration. JSON/code stay <= 2 MiB.
# Large receipts bind bytes/size; profiles separately verify shape/selected IDs.
'''

METHOD_EXAMPLE = '''# Adapt the function, plan and paths; do not copy example targets.
import json
from pathlib import Path
from xdsh.core.method_evidence import verified_call

def analyze(values, *, scale=1.0):
    return sum(values) * scale

# Write this plan from the method BEFORE the calculation, in a separate tool call.
plan = json.loads(Path("method-plan.json").read_text())["parameters"]
# Example plan: {"parameters": {"scale": 2.0}}
result = verified_call(analyze, plan, "method-call.json", [2.0, 4.0], scale=plan["scale"])
Path("result.json").write_text(json.dumps({"value": result}))
# The helper records actual bound parameters and returned/failed state, not the dataset.
# For output-bound stages use --stage-example and --stage-call-example instead.
# In another Python environment, use PYTHONPATH="$(xdsh-verify --module-root)".
# For R/other languages use source_json on effective-argument metadata; its assurance
# is only declaration consistency. Do not manufacture a Python method-call receipt.
'''


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog="A contract requires unchanged and json arrays. Optional arrays checks read "
        "numeric .npy files, not JSON lists; use equals for JSON lists. numbers maps a JSON "
        "pointer to {} (finite) or numeric minimum/maximum bounds; exact values use equals. "
        "No arbitrary validators.",
    )
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--contract", help="Workspace-relative JSON contract, not executable code")
    parser.add_argument("--baseline", type=Path,
                        help="Read the host-owned baseline; never capture a replacement")
    parser.add_argument("--profile", choices=("declared", "research"), default="declared",
                        help="Research enforces the same concrete-checklist rules as the host")
    parser.add_argument("--research-revision", type=int, choices=(1, 2),
                        help="Minimum research policy; defaults to the bound host policy or 1")
    parser.add_argument("--preflight", action="store_true",
                        help="Validate schema and source availability, not output correctness")
    parser.add_argument("--example", action="store_true", help="Print a generic editable example")
    parser.add_argument("--research-example", action="store_true",
                        help="Minimal effective metadata + numbers; no receipts/NumPy needed")
    parser.add_argument("--method-example", action="store_true", help="Show checked Python calling")
    parser.add_argument("--score-example", action="store_true",
                        help="Recompute counts and rates from saved numeric score vectors")
    parser.add_argument("--stage-example", action="store_true",
                        help="Editable contract binding calls to saved stage artifacts")
    parser.add_argument("--stage-call-example", action="store_true",
                        help="Python call plus immediate save, paired with --stage-example")
    parser.add_argument("--large-example", action="store_true",
                        help="Streaming NPY/HDF5 profiles; optional science extra for HDF5")
    parser.add_argument("--module-root", action="store_true", help="Module root for another venv")
    parser.add_argument("--details", action="store_true",
                        help="Include same-read file hashes and numeric check summaries")
    args = parser.parse_args(argv)
    if args.example:
        print(json.dumps(EXAMPLE, ensure_ascii=False, indent=2))
        return 0
    if args.research_example:
        print(json.dumps(RESEARCH_EXAMPLE, ensure_ascii=False, indent=2))
        return 0
    if args.method_example:
        print(METHOD_EXAMPLE)
        return 0
    if args.score_example:
        print(json.dumps(SCORE_EXAMPLE, ensure_ascii=False, indent=2))
        return 0
    if args.stage_example:
        print(json.dumps(STAGE_EXAMPLE, ensure_ascii=False, indent=2))
        return 0
    if args.stage_call_example:
        print(STAGE_CALL_EXAMPLE)
        return 0
    if args.large_example:
        print(json.dumps(LARGE_EXAMPLE, ensure_ascii=False, indent=2))
        return 0
    if args.module_root:
        print(Path(__file__).resolve().parents[2])
        return 0
    if not args.contract:
        parser.error("--contract is required unless --example is selected")
    try:
        stored = (load_host_baseline(args.baseline, workspace=args.workspace,
                                     contract=args.contract)
                  if args.baseline is not None else None)
        if stored is not None and args.profile != "research":
            raise AcceptanceError("host baseline requires research profile")
        required_revision = stored.required_revision if stored is not None else 1
        if args.research_revision is not None:
            if args.research_revision < required_revision:
                raise ResearchDeclarationError("research_revision_required")
            required_revision = args.research_revision
        if stored is not None and stored.snapshot is None and not args.preflight:
            print(json.dumps({"checks_passed": False, "error": "host_baseline_not_frozen",
                              "baseline_scope": "host_draft", "scientific_validity": "not_assessed",
                              "remedy": "Validate the draft with --preflight in a separate tool "
                              "call. Wait for the host to freeze it; do not bypass --baseline."}))
            return 2
        if args.profile == "research":
            task = PREFIX + json.dumps({"research_revision": required_revision,
                                       "unchanged": [], "json": [], "deferred": {
                "path": args.contract, "profile": "research",
            }})
        else:
            task = read_declared_contract(args.workspace, args.contract)
        snapshot = (stored.snapshot if stored is not None and stored.snapshot is not None
                    else capture_acceptance(task, args.workspace))
        if snapshot is None:
            raise AcceptanceError("missing contract")
        snapshot = snapshot.capture_deferred()
        if snapshot.research_revision < required_revision:
            raise ResearchDeclarationError("research_revision_required")
        identity: dict[str, object] = {
            "workspace": str(snapshot.root), "contract": args.contract, "profile": args.profile,
            "scientific_validity": "not_assessed",
            "before_state": "captured_at_this_check_not_at_experiment_start",
            "baseline_scope": "standalone_current_files_not_host_acceptance",
            "research_revision": snapshot.research_revision,
            "feasibility": snapshot.feasibility_summary(),
        }
        if stored is not None:
            identity.update({"baseline_scope": "host_frozen" if stored.snapshot else "host_draft",
                             "baseline_id": stored.baseline_id,
                             "capture_point": stored.capture_point,
                             "before_state": "host_frozen_not_proof_of_experiment_start"
                             if stored.snapshot else "draft_not_frozen"})
        if args.preflight:
            if stored is not None and stored.snapshot is not None:
                issues = snapshot.verify_sources()
                print(json.dumps({**identity, "declaration_valid": not issues,
                                  "checks_passed": None, "issues": issues,
                                  "assurance": "frozen_declaration_and_sources_only"}))
                return 1 if issues else 0
            snapshot.validate_sources()
            print(json.dumps({**identity, "declaration_valid": True, "checks_passed": None,
                              "assurance": "declaration_and_source_availability_only"}))
            return 0
        checked = snapshot.evaluate()
        result: dict[str, object] = {**identity, "checks_passed": not checked.issues,
                                   "issues": checked.issues,
                                   "advisory_differences": sum(
                                       not item.matched and not item.blocking
                                       and item.actual is not None
                                       for item in checked.expectations),
                                   "expectation_provenance": "declared_not_independently_verified",
                                   "stage_checkpoints": {"checked": len(checked.stages),
                                                         "verified": sum(s.matched
                                                                         for s in checked.stages)},
                                   "stage_provenance": "caller_writable_not_attested",
                                   "large_artifact_checks": {
                                       "checked": len(checked.large_checks),
                                       "verified": sum(c.matched for c in checked.large_checks),
                                   },
                                   "assurance": "declared_artifact_checks_only"}
        if args.details:
            result.update({"evidence": [asdict(item) for item in checked.evidence],
                           "numeric_checks": [asdict(item) for item in checked.numeric_checks],
                           "expectations": [asdict(item) for item in checked.expectations],
                           "stages": [asdict(item) for item in checked.stages],
                           "large_artifacts": [asdict(item) for item in checked.large_checks],
                           "evidence_scope": "bytes_read_during_this_check_not_later_files"})
        print(json.dumps(result, ensure_ascii=True))
        return 1 if checked.issues else 0
    except AcceptanceError as error:
        # Never echo untrusted JSON bodies, credentials or exception details.
        baseline_unavailable = args.baseline is not None and not isinstance(
            error, ResearchDeclarationError,
        )
        failure: dict[str, object] = {"checks_passed": False,
                                     "error": "host_baseline_unavailable" if baseline_unavailable
                                     else "invalid_or_inaccessible_contract"}
        if isinstance(error, ResearchDeclarationError):
            failure.update(reason=error.reason, remedy=RESEARCH_REMEDIES[error.reason])
        print(json.dumps(failure))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
