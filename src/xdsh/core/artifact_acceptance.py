"""Read-only artifact acceptance with explicit or deferred baseline capture.

No shell, glob, network or model call. Only a first-line XDSH-VERIFY JSON
contract enables checks (explicit or compiled by the scoped research workflow).
POSIX dirfd/no-follow reads keep
every bounded file access in the caller's workspace. Unsupported hosts fail closed.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from xdsh.core.evidence_files import MAX_FILE_BYTES as MAX_FILE_BYTES
from xdsh.core.evidence_files import EvidenceFileError, WorkspaceEvidence
from xdsh.core.large_artifacts import (
    hdf_references,
    inspect_large_file,
    ordered_ids_fingerprint,
    validate_large_files,
)
from xdsh.core.numeric_artifacts import numeric_array_shape, numeric_threshold_count
from xdsh.core.research_feasibility import parse_feasibility

PREFIX = "XDSH-VERIFY: "


class AcceptanceError(ValueError):
    """Content-free invalid contract or inaccessible evidence."""


RESEARCH_REMEDIES = {
    "large_dependency_unavailable": "HDF structural checks need the optional xdsh[science] "
    "extra in the verifier's environment. Ask the deployment operator to install it; "
    "do not silently downgrade the frozen profile to raw or retry the experiment.",
    "large_file_schema_invalid": "Use --large-example for explicit large_files profiles. "
    "raw checks bytes only; npy checks primitive arrays; hdf5 checks declared dense/CSR/CSC "
    "objects and ordered IDs (requires the optional science extra). Keep JSON/receipts/code "
    "small. Bound stages must declare the same large_files paths. Do not rewrite a frozen "
    "baseline, raise task quotas or silently replace originals with summaries.",
    "checklist_unavailable": "Write a regular checklist at the exact declared host-workspace "
    "path, not in a different cwd; symlinks and oversized files are not accepted.",
    "checklist_invalid": "Use --research-example for concrete JSON schema; do not copy "
    "the XDSH-VERIFY header. Keep the declaration within 8192 bytes.",
    "checklist_recursive": "Replace the deferred wrapper with concrete checks at the "
    "declared path; do not redirect to another checklist.",
    "checklist_inputs_unavailable": "Create the declared unchanged method/source files "
    "inside the host workspace before preflight; keep their bytes unchanged afterward.",
    "source_reference_invalid": "Correct the draft's source JSON pointers before freezing. "
    "They must resolve in the existing method plan (arrays use numeric indexes); calls "
    "must refer to a parameter object. Do not change frozen sources or rules to pass.",
    "expectation_reference_invalid": "A non-exploratory expectation must reference an "
    "unchanged JSON record with the same kind and bounds or equals, plus a nonempty "
    "citation and locator. This checks declared provenance, not the source's truth. "
    "Unsupported model guesses belong to exploratory, not user or literature.",
    "expectation_schema_invalid": "expectations maps JSON pointers to kind (integrity, "
    "user, literature, exploratory) and either bounds or numeric equals. Except for "
    "exploratory, include reference with path in unchanged and a JSON pointer.",
    "feasibility_invalid": "Use the research example's feasibility fields. Declare full, "
    "subset or unknown scope, requested/planned units, peak/available bytes or null, "
    "strategy and estimate_basis. Full requires equal known counts; subset requires "
    "fewer planned units. This is advisory: never raise quotas or fabricate measurements.",
    "research_revision_required": "This task requires research_revision: 2 in the concrete "
    "checklist. Use --research-example and retain feasibility. Do not downgrade the "
    "host policy or rewrite a frozen checklist.",
    "unclassified_target": "Revision 2 numbers checks use {} for finite measurements. "
    "Move scalar numeric equals and minimum/maximum targets into expectations with an "
    "explicit kind and source. Exploratory/literature differences are advisory, not "
    "grounds for tuning results to pass. Existing explicit/legacy contracts stay binding.",
    "method_checks_required": "Declare calls or source_json checks against the method plan.",
    "output_checks_required": "Declare appropriate arrays, numbers, ratios or maximum "
    "difference checks; do not add unrelated experiments.",
    "checklist_fields_required": "The checklist must be an object with unchanged and json "
    "arrays (empty arrays are allowed individually); other keys are arrays, calls, "
    "research_revision and feasibility. "
    "Do not remove mandatory keys when repairing a rule.",
    "array_schema_invalid": "arrays checks accept numeric .npy files only, with path and "
    "shape; json_pointer is not supported. Check JSON lists through equals instead, or "
    "omit an inapplicable array rule while retaining method and numeric checks.",
    "method_schema_invalid": "Each calls entry needs a unique receipt path and source "
    "with path and pointer; source.path must be listed in unchanged. Save one receipt "
    "per distinct dataset or stage.",
    "stage_schema_invalid": "Use --stage-example and --stage-call-example. calls.artifacts "
    "needs inputs, outputs, code and packages. Code and root inputs must be in unchanged; "
    "later inputs may reference earlier declared stage outputs. Use distinct output and "
    "receipt paths. No links, absolute paths, in-place overwrite or arbitrary validators. "
    "Do not rewrite a frozen checklist or recertify old outputs as newly computed.",
    "identity_schema_invalid": "artifact_json maps a result pointer to a path/pointer in "
    "another current artifact. id_selections entries have value, source and exactly one "
    "of mask/indices (each a path/pointer). arrays.axes maps axis strings to ID references. "
    "Use unique string IDs, strict boolean masks or zero-based distinct integer indices.",
    "json_schema_invalid": "A json rule needs path and only supported assertions: equals, "
    "source_sha256, source_json, numbers, expectations, ratios, max_abs_differences, "
    "threshold_rates, artifact_json and id_selections. "
    "Use --example "
    "for exact field shapes; JSON pointers start with slash.",
    "numeric_schema_invalid": 'numbers maps JSON pointers to bounds: {"/value":{}} checks '
    'a finite number; {"/value":{"minimum":0,"maximum":1}} also checks an inclusive range. '
    "Bounds must be finite numbers (not booleans), with minimum <= maximum; no other keys "
    'are supported. For a legacy exact value use sibling "equals":{"/value":1}, not eq or '
    "finite inside numbers. Correct the same checklist, preserve the intended checks, "
    "and rerun preflight; revision 2 uses {} here and classified expectations for targets. "
    "Do not probe schema permutations or inspect framework source.",
}

_RESEARCH_SCHEMA_REASONS = {
    "HDF science dependency unavailable": "large_dependency_unavailable",
    "invalid large file declaration": "large_file_schema_invalid",
    "invalid research feasibility": "feasibility_invalid",
    "invalid research revision": "research_revision_required",
    "unclassified scientific target": "unclassified_target",
    "invalid scientific expectations": "expectation_schema_invalid",
    "scientific expectation needs a declared source": "expectation_schema_invalid",
    "contract requires unchanged and json arrays": "checklist_fields_required",
    "invalid array assertion": "array_schema_invalid",
    "invalid array shape or finite flag": "array_schema_invalid",
    "invalid method call assertion": "method_schema_invalid",
    "method call needs declared source parameters": "method_schema_invalid",
    "each call needs a unique method receipt": "method_schema_invalid",
    "invalid JSON rule": "json_schema_invalid",
    "invalid JSON pointers": "json_schema_invalid",
    "invalid numeric field assertions": "numeric_schema_invalid",
    "invalid ratio assertions": "json_schema_invalid",
    "invalid ratio assertion": "json_schema_invalid",
    "invalid maximum difference assertions": "json_schema_invalid",
    "invalid maximum difference assertion": "json_schema_invalid",
    "invalid required metric labels": "json_schema_invalid",
    "invalid threshold rate assertions": "json_schema_invalid",
    "invalid artifact binding": "stage_schema_invalid",
    "artifact stages need frozen roots and distinct outputs": "stage_schema_invalid",
    "invalid artifact reference": "identity_schema_invalid",
    "invalid ID selection": "identity_schema_invalid",
    "invalid array axes": "identity_schema_invalid",
}


class ResearchDeclarationError(AcceptanceError):
    """Allowlisted, content-free diagnosis shared by CLI preflight and the host."""

    def __init__(self, reason: str) -> None:
        self.reason = reason if reason in RESEARCH_REMEDIES else "checklist_invalid"
        super().__init__(self.reason)


def issue_categories(issues: tuple[str, ...]) -> tuple[str, ...]:
    """Fixed public categories; untrusted file paths and field names stay private."""
    mapping = {"research_contract_unavailable": "checklist", "method_call_mismatch": "parameters",
               "field_mismatch": "fields", "ratio_mismatch": "numbers",
               "number_mismatch": "numbers", "max_difference_mismatch": "numbers",
               "array_invalid": "arrays", "array_shape_mismatch": "arrays",
               "source_changed": "inputs_changed", "source_unavailable": "missing_files",
               "json_unavailable": "missing_files"}
    mapping["threshold_rates_mismatch"] = "numbers"
    mapping["expectation_mismatch"] = "numbers"
    mapping["expectation_reference_invalid"] = "checklist"
    mapping["artifact_binding_mismatch"] = "parameters"
    mapping["id_selection_mismatch"] = "fields"
    mapping["array_identity_mismatch"] = "arrays"
    mapping["large_file_invalid"] = "arrays"
    return tuple(dict.fromkeys(
        mapping.get(issue.split(":", 1)[0], "checklist") for issue in issues
    ))


def contract_header(task: str) -> str | None:
    line = task.split("\n", 1)[0]
    return line if line.startswith(PREFIX) else None


def _pairs(rows: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in rows:
        if key in result:
            raise AcceptanceError("duplicate JSON key")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise AcceptanceError("non-finite JSON value")


def _float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise AcceptanceError("non-finite JSON value")
    return parsed


def _json(text: str | bytes) -> Any:
    try:
        return json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant,
                          parse_float=_float)
    except (ValueError, RecursionError, UnicodeError) as error:
        raise AcceptanceError("invalid JSON evidence") from error


def _path(value: object) -> str:
    if (not isinstance(value, str) or not 1 <= len(value) <= 240
            or "\\" in value or any(ord(c) < 32 for c in value)):
        raise AcceptanceError("invalid evidence path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in {"", ".", ".."} for p in value.split("/")):
        raise AcceptanceError("evidence must stay inside workspace")
    return value


def _pointer(document: Any, pointer: str) -> Any:
    value = document
    for part in pointer[1:].split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict):
            value = value[key]
        elif isinstance(value, list) and key.isascii() and key.isdigit() and str(int(key)) == key:
            value = value[int(key)]
        else:
            raise KeyError("missing pointer")
    return value


def _finite_number(value: object) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)  # type: ignore[arg-type]
    except OverflowError:
        return False


def _valid_pointer(value: object) -> bool:
    return isinstance(value, str) and value.startswith("/") and len(value) <= 200


def _valid_bounds(bounds: object) -> bool:
    return (isinstance(bounds, dict) and not set(bounds) - {"minimum", "maximum"}
            and all(_finite_number(v) for v in bounds.values())
            and ("minimum" not in bounds or "maximum" not in bounds
                 or bounds["minimum"] <= bounds["maximum"]))


def _expectation_reference_matches(record: Any, expectation: dict[str, Any]) -> bool:
    if not isinstance(record, dict) or record.get("kind") != expectation["kind"]:
        return False
    for field in ("citation", "locator"):
        text = record.get(field)
        if (not isinstance(text, str) or not text.strip() or len(text) > 1000
                or any(ord(c) < 32 for c in text)):
            return False
    target = "bounds" if "bounds" in expectation else "equals"
    other = "equals" if target == "bounds" else "bounds"
    return (other not in record and target in record
            and json.dumps(record[target], sort_keys=True)
            == json.dumps(expectation[target], sort_keys=True))


def _ratio_matches(document: Any, rule: dict[str, Any]) -> bool:
    try:
        value, numerator, denominator = (
            _pointer(document, rule[key]) for key in ("value", "numerator", "denominator")
        )
        if not all(_finite_number(v) for v in (value, numerator, denominator)) or denominator == 0:
            return False
        expected = numerator / denominator * rule.get("scale", 1)
        return math.isfinite(expected) and math.isclose(
            value, expected, rel_tol=0, abs_tol=rule.get("atol", 0),
        )
    except (KeyError, IndexError, TypeError, ValueError, OverflowError):
        return False


def _valid_ids(value: Any) -> bool:
    return (isinstance(value, list) and len(value) <= 200_000
            and all(isinstance(item, str) and 1 <= len(item) <= 256
                    and not any(ord(c) < 32 for c in item) for item in value)
            and len(set(value)) == len(value))


def _id_selection_matches(document: Any, rule: dict[str, Any],
                          read: Callable[[str], bytes]) -> bool:
    try:
        source = rule["source"]
        ids = _pointer(_json(read(source["path"])), source["pointer"])
        actual = _pointer(document, rule["value"])
        if not _valid_ids(ids) or not _valid_ids(actual):
            return False
        selector = rule["mask"] if "mask" in rule else rule["indices"]
        selected = _pointer(_json(read(selector["path"])), selector["pointer"])
        if not isinstance(selected, list):
            return False
        if "mask" in rule:
            if len(selected) != len(ids) or any(type(value) is not bool for value in selected):
                return False
            expected = [item for item, keep in zip(ids, selected, strict=True) if keep]
        else:
            if (len(selected) > len(ids)
                    or any(type(value) is not int or not 0 <= value < len(ids)
                           for value in selected)
                    or len(set(selected)) != len(selected)):
                return False
            expected = [ids[index] for index in selected]
        return bool(actual == expected)
    except (ValueError, KeyError, IndexError, TypeError):
        return False


_THRESHOLD_POINTERS = ("threshold", "count", "total", "detected", "detectable", "overall")


def _threshold_rates_match(document: Any, rule: dict[str, Any],
                           read: Callable[[str], bytes]) -> bool:
    try:
        threshold = _pointer(document, rule["threshold"])
        if not _finite_number(threshold):
            return False
        count, total = numeric_threshold_count(read(rule["observed"]), threshold,
                                              inclusive=rule.get("comparison", "gt") == "ge")
        sim_count, sim_total = numeric_threshold_count(
            read(rule["simulated"]), threshold, inclusive=rule.get("comparison", "gt") == "ge")
        if not sim_count:
            return False  # Overall estimate is undefined, not zero or null-as-success.
        for key, actual in (("count", count), ("total", total)):
            claimed = _pointer(document, rule[key])
            if type(claimed) is not int or claimed != actual:
                return False
        detected, detectable = count / total, sim_count / sim_total
        for key, actual_rate in (("detected", detected), ("detectable", detectable),
                                 ("overall", detected / detectable)):
            claimed = _pointer(document, rule[key])
            if not _finite_number(claimed) or not math.isclose(
                claimed, actual_rate * rule.get("scale", 1), rel_tol=0,
                abs_tol=rule.get("atol", 0),
            ):
                return False
        return True
    except (ValueError, KeyError, IndexError, TypeError, OverflowError):
        return False


@dataclass(frozen=True)
class EvidenceRead:
    """Identity of bytes read, not a claim that they passed scientific review."""

    path: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class DifferenceCheck:
    path: str
    pointer: str
    expected_max: float | None
    largest_metric: str | None
    compared_metrics: int
    matched: bool


@dataclass(frozen=True)
class ScientificComparison:
    """A declared comparison, not independently authenticated scientific truth."""

    path: str
    pointer: str
    kind: str
    matched: bool
    blocking: bool
    actual: int | float | None


@dataclass(frozen=True)
class StageCheck:
    """Current check of a persisted stage, not proof of checkpoint durability."""

    path: str
    outcome: str
    matched: bool


@dataclass(frozen=True)
class LargeArtifactCheck:
    path: str
    format: str
    matched: bool
    coverage: str
    reason: str


@dataclass(frozen=True)
class AcceptanceResult:
    issues: tuple[str, ...]
    evidence: tuple[EvidenceRead, ...]
    numeric_checks: tuple[DifferenceCheck, ...]
    expectations: tuple[ScientificComparison, ...] = ()
    stages: tuple[StageCheck, ...] = ()
    large_checks: tuple[LargeArtifactCheck, ...] = ()


def _max_difference(document: Any, rule: dict[str, Any], path: str) -> DifferenceCheck:
    """Compare label-aligned finite values; never infer their scientific units."""

    invalid = DifferenceCheck(path, rule["value"], None, None, 0, False)
    try:
        actual, reference = (_pointer(document, rule[k]) for k in ("actual", "reference"))
        if (not isinstance(actual, dict) or not isinstance(reference, dict)
                or not 1 <= len(actual) <= 128 or actual.keys() != reference.keys()
                or ("required_metrics" in rule
                    and set(actual) != set(rule["required_metrics"]))
                or any(not isinstance(k, str) or not 1 <= len(k) <= 120
                       or any(ord(c) < 32 for c in k) for k in actual)):
            return invalid
        maximum, largest = -1.0, None
        for label in sorted(actual):
            observed, expected = actual[label], reference[label]
            if not _finite_number(observed) or not _finite_number(expected):
                return invalid
            difference = abs(observed - expected) * rule.get("scale", 1)
            if not math.isfinite(difference):
                return invalid
            if difference > maximum:
                maximum, largest = float(difference), label
        try:
            reported = _pointer(document, rule["value"])
        except (KeyError, IndexError, TypeError):
            reported = None
        matched = _finite_number(reported) and math.isclose(
            reported, maximum, rel_tol=0, abs_tol=rule.get("atol", 0),
        )
        return DifferenceCheck(path, rule["value"], maximum, largest, len(actual), matched)
    except (KeyError, IndexError, TypeError, ValueError, OverflowError):
        return invalid


@dataclass(frozen=True)
class ArtifactAcceptance:
    root: Path
    identity: tuple[int, int]
    baselines: tuple[tuple[str, str], ...]
    # Immutable serialized rules: edits to a model-owned file cannot alter them.
    rules: str
    arrays: str = "[]"
    deferred: str | None = None
    calls: str = "[]"
    research_revision: int = 1
    feasibility: str = "null"
    large_files: str = "{}"

    def feasibility_summary(self) -> dict[str, object] | None:
        value = _json(self.feasibility)
        return parse_feasibility(value).summary() if value is not None else None

    def feasibility_notices(self) -> tuple[str, ...]:
        value = _json(self.feasibility)
        return parse_feasibility(value).notices if value is not None else ()

    def _read(self, path: str) -> bytes:
        try:
            return WorkspaceEvidence(self.root, self.identity).read(path)
        except EvidenceFileError as error:
            raise AcceptanceError(str(error)) from None

    def verify(self) -> tuple[str, ...]:
        """Return bounded issue identifiers, never file bodies or digests."""
        return self.evaluate().issues

    def verify_sources(self) -> tuple[str, ...]:
        """Check frozen bytes only, without requiring not-yet-created outputs."""
        issues: list[str] = []
        for path, before in self.baselines:
            try:
                if self._fingerprint(path)["sha256"] != before:
                    issues.append("source_changed:" + path)
            except AcceptanceError:
                issues.append("source_unavailable:" + path)
        return tuple(issues)

    def _fingerprint(self, path: str) -> dict[str, object]:
        try:
            large = _json(self.large_files)
            if path in large:
                return inspect_large_file(WorkspaceEvidence(self.root, self.identity),
                                          path, large[path])
            data = self._read(path)
            return {"sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}
        except ValueError as error:
            raise AcceptanceError(str(error)) from None

    def validate_sources(self) -> None:
        """Validate existing reference targets without requiring future outputs.

        A structurally valid draft is not ready to freeze if its source pointer
        does not resolve. Check the same bytes against the captured source hash
        so a concurrent replacement cannot validate a different method plan.
        """
        documents: dict[str, Any] = {}
        baselines = dict(self.baselines)
        references = [
            (reference, False) for rule in _json(self.rules)
            for reference in rule.get("source_json", {}).values()
        ]
        references.extend((rule["source"], True) for rule in _json(self.calls))
        # Roots already present can be checked before freezing. Future stage
        # outputs must NOT be required here or preflight would trigger execution.
        artifact_references = [reference for rule in _json(self.rules)
                               for reference in rule.get("artifact_json", {}).values()]
        artifact_references.extend(
            selection[name] for rule in _json(self.rules)
            for selection in rule.get("id_selections", [])
            for name in ("source", "mask" if "mask" in selection else "indices")
        )
        artifact_references.extend(reference for rule in _json(self.arrays)
                                   for reference in rule.get("axes", {}).values())
        artifact_references.extend(reference for rule in _json(self.large_files).values()
                                   for reference in hdf_references(rule))
        references.extend((reference, False) for reference in artifact_references
                          if reference["path"] in baselines)
        expectations = [item for rule in _json(self.rules)
                        for item in rule.get("expectations", {}).values() if "reference" in item]
        references.extend((item["reference"], False) for item in expectations)
        try:
            for reference, parameters in references:
                path = reference["path"]
                if path not in documents:
                    content = self._read(path)
                    if hashlib.sha256(content).hexdigest() != baselines[path]:
                        raise AcceptanceError("source changed during preflight")
                    documents[path] = _json(content)
                value = _pointer(documents[path], reference["pointer"])
                if parameters:
                    from xdsh.core.method_evidence import parameter_json

                    if not isinstance(value, dict):
                        raise AcceptanceError("method parameters must be an object")
                    parameter_json(value)
        except (ValueError, KeyError, IndexError, TypeError, RecursionError):
            raise ResearchDeclarationError("source_reference_invalid") from None
        for expectation in expectations:
            reference = expectation["reference"]
            record = _pointer(documents[reference["path"]], reference["pointer"])
            if not _expectation_reference_matches(record, expectation):
                raise ResearchDeclarationError("expectation_reference_invalid")

    def evaluate(self) -> AcceptanceResult:
        """Check one byte snapshot per path and return a bounded audit receipt.

        This is not an atomic snapshot across files, or a promise about later
        writes. Callers must not reread files to construct this check's receipt.
        """

        if self.deferred is not None:
            return self._evaluate_deferred()
        issues: list[str] = []
        numeric_checks: list[DifferenceCheck] = []
        expectations: list[ScientificComparison] = []
        stages: list[StageCheck] = []
        contents: dict[str, bytes | None] = {}
        large_results: dict[str, dict[str, object]] = {}
        large_rules = _json(self.large_files)
        large_errors: set[str] = set()
        large_reasons: dict[str, str] = {}

        def large_once(path: str) -> dict[str, object]:
            if path not in large_rules or path in large_errors:
                raise AcceptanceError("large evidence unavailable")
            if path not in large_results:
                try:
                    result = self._fingerprint(path)
                    rule = large_rules[path]
                    for dataset_path, dataset in rule.get("datasets", {}).items():
                        for axis, item in dataset.get("axes", {}).items():
                            reference = item["reference"]
                            ids = _pointer(_json(read_once(reference["path"])),
                                           reference["pointer"])
                            metadata = result["datasets"]
                            if not isinstance(metadata, dict):
                                raise ValueError("invalid HDF result")
                            if metadata[dataset_path]["axes"][axis] != ordered_ids_fingerprint(ids):
                                raise AcceptanceError("large artifact ID order mismatch")
                    large_results[path] = result
                except (ValueError, KeyError, IndexError, TypeError) as error:
                    large_errors.add(path)
                    large_reasons[path] = (
                        "dependency_unavailable"
                        if str(error) == "HDF science dependency unavailable"
                        else "invalid_or_unavailable")
                    raise AcceptanceError("large artifact unavailable or invalid") from None
            return large_results[path]

        def read_once(path: str) -> bytes:
            if path not in contents:
                try:
                    contents[path] = self._read(path)
                except AcceptanceError:
                    contents[path] = None
            value = contents[path]
            if value is None:
                raise AcceptanceError("evidence inaccessible")
            return value

        baselines = dict(self.baselines)
        source_documents: dict[str, Any] = {}
        invalid_sources: set[str] = set()
        rules = _json(self.rules)
        calls = _json(self.calls)
        referenced_sources = {
            reference["path"] for rule in rules
            for reference in rule.get("source_json", {}).values()
        }
        referenced_sources.update(rule["source"]["path"] for rule in calls)
        referenced_sources.update(
            item["reference"]["path"] for rule in rules
            for item in rule.get("expectations", {}).values() if "reference" in item
        )
        for path, before in self.baselines:
            try:
                if path in large_rules:
                    large_once(path)
                    if large_results[path]["sha256"] != before:
                        issues.append("source_changed:" + path)
                        invalid_sources.add(path)
                    # Small JSON references cannot opt into the raw large path.
                    continue
                content = read_once(path)
                if hashlib.sha256(content).hexdigest() != before:
                    issues.append("source_changed:" + path)
                    invalid_sources.add(path)
                if path in referenced_sources:
                    # Hash and compare the SAME bytes, never reread a mutable
                    # source after checking its captured before-state hash.
                    source_documents[path] = _json(content)
            except AcceptanceError:
                issues.append("source_unavailable:" + path)
                invalid_sources.add(path)
        for rule in rules:
            path = rule["path"]
            try:
                document = _json(read_once(path))
                expected = dict(rule.get("equals", {}))
                expected.update({p: baselines[source]
                                 for p, source in rule.get("source_sha256", {}).items()})
                for pointer, target in expected.items():
                    try:
                        actual = _pointer(document, pointer)
                        matched = json.dumps(actual, sort_keys=True) == json.dumps(target,
                                                                                  sort_keys=True)
                    except (KeyError, IndexError, ValueError, RecursionError):
                        matched = False
                    if not matched:
                        issues.append("field_mismatch:" + path + ":" + pointer)
                for pointer, reference in rule.get("source_json", {}).items():
                    try:
                        source = reference["path"]
                        target = _pointer(source_documents[source], reference["pointer"])
                        actual = _pointer(document, pointer)
                        matched = json.dumps(actual, sort_keys=True) == json.dumps(
                            target, sort_keys=True,
                        )
                    except (AcceptanceError, KeyError, IndexError, ValueError, RecursionError):
                        matched = False
                    if not matched:
                        issues.append("field_mismatch:" + path + ":" + pointer)
                for pointer, reference in rule.get("artifact_json", {}).items():
                    try:
                        target = _pointer(_json(read_once(reference["path"])), reference["pointer"])
                        actual = _pointer(document, pointer)
                        matched = json.dumps(actual, sort_keys=True) == json.dumps(
                            target, sort_keys=True,
                        )
                    except (AcceptanceError, KeyError, IndexError, ValueError, RecursionError):
                        matched = False
                    if not matched:
                        issues.append("field_mismatch:" + path + ":" + pointer)
                for selection in rule.get("id_selections", []):
                    if not _id_selection_matches(document, selection, read_once):
                        issues.append("id_selection_mismatch:" + path + ":" + selection["value"])
                for ratio in rule.get("ratios", []):
                    if not _ratio_matches(document, ratio):
                        issues.append("ratio_mismatch:" + path + ":" + ratio["value"])
                for rate in rule.get("threshold_rates", []):
                    if not _threshold_rates_match(document, rate, read_once):
                        issues.append("threshold_rates_mismatch:" + path + ":" + rate["threshold"])
                for pointer, bounds in rule.get("numbers", {}).items():
                    try:
                        number = _pointer(document, pointer)
                        matched = (_finite_number(number)
                                   and ("minimum" not in bounds or number >= bounds["minimum"])
                                   and ("maximum" not in bounds or number <= bounds["maximum"]))
                    except (KeyError, IndexError, TypeError):
                        matched = False
                    if not matched:
                        issues.append("number_mismatch:" + path + ":" + pointer)
                for pointer, expectation in rule.get("expectations", {}).items():
                    reference = expectation.get("reference")
                    if reference is not None:
                        try:
                            record = _pointer(source_documents[reference["path"]],
                                              reference["pointer"])
                            reference_valid = _expectation_reference_matches(record, expectation)
                        except (KeyError, IndexError, TypeError, ValueError, RecursionError):
                            reference_valid = False
                        if not reference_valid:
                            issues.append("expectation_reference_invalid:" + path + ":" + pointer)
                    try:
                        number = _pointer(document, pointer)
                    except (KeyError, IndexError, TypeError):
                        number = None
                    valid = _finite_number(number)
                    bounds = expectation.get("bounds", {})
                    matched = (valid
                               and ("equals" not in expectation or number == expectation["equals"])
                               and ("minimum" not in bounds or number >= bounds["minimum"])
                               and ("maximum" not in bounds or number <= bounds["maximum"]))
                    blocking = expectation["kind"] in {"integrity", "user"}
                    expectations.append(ScientificComparison(
                        path, pointer, expectation["kind"], matched, blocking,
                        number if valid else None,
                    ))
                    # Advisory thresholds never excuse missing/invalid evidence.
                    if not valid:
                        issue = "number_mismatch:" + path + ":" + pointer
                        if issue not in issues:
                            issues.append(issue)
                    elif blocking and not matched:
                        issues.append("expectation_mismatch:" + path + ":" + pointer)
                for comparison in rule.get("max_abs_differences", []):
                    checked = _max_difference(document, comparison, path)
                    numeric_checks.append(checked)
                    if not checked.matched:
                        issues.append("max_difference_mismatch:" + path + ":" + checked.pointer)
            except AcceptanceError:
                issues.append("json_unavailable:" + path)
        for rule in calls:
            path = rule["path"]
            stage_outcome, stage_matches = "unavailable", False
            try:
                from xdsh.core.method_evidence import parameter_json

                document = _json(read_once(path))
                source = rule["source"]
                expected = _pointer(source_documents[source["path"]], source["pointer"])
                if not isinstance(document, dict) or not isinstance(expected, dict):
                    raise AcceptanceError("invalid method call evidence")
                stage_outcome = document.get("outcome", "invalid")
                if stage_outcome not in ("pending", "returned", "blocked", "failed"):
                    stage_outcome = "invalid"
                actual = document.get("effective_parameters")
                if not isinstance(actual, dict):
                    raise AcceptanceError("missing effective parameters")
                canonical = parameter_json(actual)
                if (type(document.get("schema_version")) is not int
                        or document["schema_version"] != 1
                        or document.get("kind") != "xdsh.method_call"
                        or document.get("outcome") != "returned"
                        or canonical != parameter_json(expected)
                        or document.get("parameters_sha256")
                        != hashlib.sha256(canonical.encode()).hexdigest()):
                    raise AcceptanceError("method call evidence mismatch")
                if "artifacts" in rule:
                    from xdsh.core.stage_evidence import artifact_binding_matches

                    stage_matches = artifact_binding_matches(document.get("artifacts"),
                                                             rule["artifacts"], read_once,
                                                             fingerprint=large_once)
                    stage_matches = stage_matches and not invalid_sources.intersection(
                        [source["path"], *rule["artifacts"]["inputs"], *rule["artifacts"]["code"]],
                    )
                    if not stage_matches:
                        issues.append("artifact_binding_mismatch:" + path)
            except (ValueError, KeyError, IndexError, TypeError):
                issues.append("method_call_mismatch:" + path)
            if "artifacts" in rule:
                stages.append(StageCheck(path, stage_outcome, stage_matches))
        for rule in _json(self.arrays):
            path = rule["path"]
            try:
                shape = numeric_array_shape(
                    read_once(path), finite=rule.get("finite", True),
                    nonconstant=rule.get("nonconstant", False), minimum=rule.get("minimum"),
                    maximum=rule.get("maximum"),
                )
                expected_shape = rule["shape"]
                if len(shape) != len(expected_shape) or any(
                    target is not None and actual != target
                    for actual, target in zip(shape, expected_shape, strict=False)
                ):
                    issues.append("array_shape_mismatch:" + path)
                for axis, reference in rule.get("axes", {}).items():
                    try:
                        ids = _pointer(_json(read_once(reference["path"])), reference["pointer"])
                        aligned = (_valid_ids(ids) and int(axis) < len(shape)
                                   and len(ids) == shape[int(axis)])
                    except (ValueError, KeyError, IndexError, TypeError):
                        aligned = False
                    if not aligned:
                        issues.append("array_identity_mismatch:" + path + ":" + axis)
            except (AcceptanceError, ValueError):
                issues.append("array_invalid:" + path)
        for path in large_rules:
            if path not in large_results and path not in invalid_sources:
                try:
                    large_once(path)
                except AcceptanceError:
                    issues.append("large_file_invalid:" + path)
        evidence = tuple(
            EvidenceRead(path, hashlib.sha256(content).hexdigest(), len(content))
            for path, content in sorted(contents.items()) if content is not None
        ) + tuple(EvidenceRead(path, str(result["sha256"]), int(str(result["size_bytes"])))
                  for path, result in sorted(large_results.items()))
        coverage = {"raw": "sha256_only", "npy": "npy_header_and_declared_numeric_checks",
                    "hdf5": "declared_hdf_structure_and_requested_ids"}
        large_checks = tuple(LargeArtifactCheck(
            path, rule["format"], path in large_results and path not in invalid_sources,
            coverage[rule["format"]], large_reasons.get(path, "source_changed" if
                path in invalid_sources else "verified"),
        ) for path, rule in large_rules.items())
        return AcceptanceResult(tuple(issues), evidence, tuple(numeric_checks), tuple(expectations),
                                tuple(stages), large_checks)

    def _evaluate_deferred(self) -> AcceptanceResult:
        """Post-hoc checks only; never claim an experiment-start baseline."""
        assert self.deferred is not None
        try:
            return self.capture_deferred().evaluate()
        except AcceptanceError:
            return AcceptanceResult(("research_contract_unavailable:" + self.deferred,), (), ())

    def capture_deferred(self) -> ArtifactAcceptance:
        """Freeze the first valid declared checklist, not a claimed experiment start."""
        from dataclasses import replace

        if self.deferred is None:
            return self
        path = self.deferred
        try:
            data = self._read(path)
        except AcceptanceError:
            raise ResearchDeclarationError("checklist_unavailable") from None
        if len(data) > 8192:
            raise ResearchDeclarationError("checklist_invalid")
        try:
            spec = _json(data)
        except AcceptanceError:
            raise ResearchDeclarationError("checklist_invalid") from None
        if isinstance(spec, dict) and "deferred" in spec:
            raise ResearchDeclarationError("checklist_recursive")
        if self.research_revision == 2 and (
            not isinstance(spec, dict) or type(spec.get("research_revision")) is not int
            or spec["research_revision"] != 2
        ):
            raise ResearchDeclarationError("research_revision_required")
        try:
            checked = capture_acceptance(PREFIX + json.dumps(spec), self.root)
        except AcceptanceError as error:
            # These internal errors have fixed strings, never evidence contents.
            reason = ("checklist_inputs_unavailable" if str(error) in {
                "evidence inaccessible", "evidence file is not a bounded regular file",
                "evidence size limit exceeded",
            } else _RESEARCH_SCHEMA_REASONS.get(str(error), "checklist_invalid"))
            raise ResearchDeclarationError(reason) from None
        if checked is None or checked.identity != self.identity:
            raise ResearchDeclarationError("checklist_unavailable")
        rules = _json(checked.rules)
        if not (_json(checked.calls) or any(r.get("source_json") for r in rules)):
            raise ResearchDeclarationError("method_checks_required")
        if not (_json(checked.arrays) or any(
            rule["format"] != "raw" for rule in _json(checked.large_files).values()
        ) or any(
            r.get("numbers") or r.get("ratios") or r.get("max_abs_differences")
            or r.get("threshold_rates") or r.get("expectations") for r in rules
        )):
            raise ResearchDeclarationError("output_checks_required")
        if path in dict(checked.baselines):
            raise ResearchDeclarationError("checklist_recursive")
        checked.validate_sources()
        return replace(checked, baselines=(*checked.baselines,
                                          (path, hashlib.sha256(data).hexdigest())))


def capture_acceptance(task: str, workspace: Path) -> ArtifactAcceptance | None:
    return _capture_acceptance(task, workspace)


def restore_acceptance(
    workspace: Path, declaration: str, value: object,
) -> ArtifactAcceptance:
    """Restore a host-owned snapshot without capturing current source hashes.

    The caller must authenticate the storage boundary. This validates the full
    rule schema again; it is not a way to trust a model-written workspace file.
    """
    from dataclasses import replace

    try:
        required = {"identity", "baselines", "rules", "arrays", "calls"}
        if (not isinstance(value, dict) or not required <= set(value)
                or set(value) - required - {"research_revision", "feasibility", "large_files"}):
            raise AcceptanceError("invalid saved acceptance")
        validate_large_files(value.get("large_files", {}))
        identity = value["identity"]
        if (not isinstance(identity, list) or len(identity) != 2
                or any(type(n) is not int or n < 0 for n in identity)):
            raise AcceptanceError("invalid saved workspace identity")
        rows = value["baselines"]
        if (not isinstance(rows, list) or not 1 <= len(rows) <= 9
                or any(not isinstance(row, list) or len(row) != 2
                       or not isinstance(row[1], str) or len(row[1]) != 64
                       or any(c not in "0123456789abcdef" for c in row[1]) for row in rows)):
            raise AcceptanceError("invalid saved source hashes")
        baselines = tuple((_path(row[0]), row[1]) for row in rows)
        if len(dict(baselines)) != len(baselines) or _path(declaration) not in dict(baselines):
            raise AcceptanceError("invalid saved declaration hash")
        source_hashes = tuple(row for row in baselines if row[0] != declaration)
        spec = {"unchanged": [path for path, _ in source_hashes], "json": value["rules"]}
        # Empty optional defaults were not necessarily in the original header.
        # Adding them on restore would reject otherwise valid near-limit drafts.
        for optional in ("arrays", "calls"):
            if value[optional] != []:
                spec[optional] = value[optional]
        if (type(value.get("research_revision", 1)) is not int
                or value.get("research_revision", 1) not in (1, 2)):
            raise AcceptanceError("invalid saved research revision")
        if value.get("research_revision", 1) != 1:
            spec["research_revision"] = value["research_revision"]
        if value.get("feasibility") is not None:
            spec["feasibility"] = value["feasibility"]
        if value.get("large_files"):
            spec["large_files"] = value["large_files"]
        snapshot = _capture_acceptance(
            PREFIX + json.dumps(spec, allow_nan=False), workspace,
            frozen=((identity[0], identity[1]), source_hashes),
        )
        if snapshot is None:
            raise AcceptanceError("invalid saved acceptance")
        return replace(snapshot, baselines=baselines)
    except (TypeError, ValueError, RecursionError):
        raise AcceptanceError("invalid saved acceptance") from None


def _capture_acceptance(
    task: str, workspace: Path, *,
    frozen: tuple[tuple[int, int], tuple[tuple[str, str], ...]] | None = None,
) -> ArtifactAcceptance | None:
    header = contract_header(task)
    if header is None:
        return None
    if len(header) > 8192:
        raise AcceptanceError("acceptance contract too large")
    spec = _json(header[len(PREFIX):])
    if (not isinstance(spec, dict) or not {"unchanged", "json"} <= set(spec)
            or set(spec) - {"unchanged", "json", "arrays", "deferred", "calls",
                            "research_revision", "feasibility", "large_files"}):
        raise AcceptanceError("contract requires unchanged and json arrays")
    sources, rules = spec["unchanged"], spec["json"]
    arrays = spec.get("arrays", [])
    calls = spec.get("calls", [])
    deferred = spec.get("deferred")
    revision = spec.get("research_revision", 1)
    feasibility = spec.get("feasibility")
    large_files = spec.get("large_files", {})
    try:
        validate_large_files(large_files)
    except ValueError:
        raise AcceptanceError("invalid large file declaration") from None
    if type(revision) is not int or revision not in (1, 2):
        raise AcceptanceError("invalid research revision")
    if feasibility is not None or (revision == 2 and deferred is None):
        try:
            parse_feasibility(feasibility)
        except ValueError:
            raise AcceptanceError("invalid research feasibility") from None
    if deferred is not None:
        if (not isinstance(deferred, dict) or set(deferred) != {"path", "profile"}
                or deferred["profile"] != "research" or sources or rules or arrays or calls
                or feasibility is not None or large_files):
            raise AcceptanceError("invalid deferred research contract")
        _path(deferred["path"])
    if (not isinstance(sources, list) or not isinstance(rules, list)
            or not isinstance(arrays, list) or len(arrays) > 8
            or not isinstance(calls, list) or len(calls) > 8
            or len(sources) > 8 or len(rules) > 8
            or not (sources or rules or arrays or deferred or calls or large_files)):
        raise AcceptanceError("acceptance contract limit exceeded")
    sources = [_path(path) for path in sources]
    if len(set(sources)) != len(sources):
        raise AcceptanceError("duplicate source path")
    for call in calls:
        if (not isinstance(call, dict) or not {"path", "source"} <= set(call)
                or set(call) - {"path", "source", "artifacts"}):
            raise AcceptanceError("invalid method call assertion")
        _path(call["path"])
        source = call["source"]
        if (not isinstance(source, dict) or set(source) != {"path", "pointer"}
                or not isinstance(source["path"], str) or source["path"] not in sources
                or not _valid_pointer(source["pointer"])):
            raise AcceptanceError("method call needs declared source parameters")
        if "artifacts" in call:
            from xdsh.core.stage_evidence import validate_artifact_declaration

            try:
                validate_artifact_declaration(call["artifacts"], call["path"])
            except ValueError:
                raise AcceptanceError("invalid artifact binding") from None
    if len({call["path"] for call in calls}) != len(calls):
        raise AcceptanceError("each call needs a unique method receipt")
    available_inputs = set(sources)
    receipt_paths = {call["path"] for call in calls}
    for call in calls:
        if "artifacts" not in call:
            continue
        binding = call["artifacts"]
        if (not set(binding["code"]) <= set(sources)
                or not set(binding["inputs"]) <= available_inputs
                or set(binding["outputs"]) & (available_inputs | receipt_paths)):
            raise AcceptanceError("artifact stages need frozen roots and distinct outputs")
        expected_large = set(binding["inputs"] + binding["outputs"]) & set(large_files)
        if (set(binding.get("large_files", [])) != expected_large
                or set(binding["code"]) & set(large_files)):
            raise AcceptanceError("invalid large file declaration")
        available_inputs.update(binding["outputs"])
    for rule in rules:
        if (not isinstance(rule, dict) or "path" not in rule
                or set(rule) - {"path", "equals", "source_sha256", "source_json", "ratios",
                                "max_abs_differences", "numbers", "threshold_rates",
                                "expectations", "artifact_json", "id_selections"}):
            raise AcceptanceError("invalid JSON rule")
        _path(rule["path"])
        for name in ("equals", "source_sha256", "source_json", "artifact_json"):
            fields = rule.get(name, {})
            if (not isinstance(fields, dict) or len(fields) > 16
                    or any(not _valid_pointer(p) for p in fields)):
                raise AcceptanceError("invalid JSON pointers")
        if set(rule.get("equals", {})) & set(rule.get("source_sha256", {})):
            raise AcceptanceError("conflicting JSON assertions")
        if set(rule.get("source_json", {})) & (
            set(rule.get("equals", {})) | set(rule.get("source_sha256", {}))
        ):
            raise AcceptanceError("conflicting JSON assertions")
        if any(source not in sources for source in rule.get("source_sha256", {}).values()):
            raise AcceptanceError("hash assertion needs a declared unchanged source")
        for reference in rule.get("source_json", {}).values():
            if (not isinstance(reference, dict) or set(reference) != {"path", "pointer"}
                    or not _valid_pointer(reference["pointer"])
                    or not isinstance(reference["path"], str)
                    or reference["path"] not in sources):
                raise AcceptanceError("JSON field reference needs a declared unchanged source")
            _path(reference["path"])
        for reference in rule.get("artifact_json", {}).values():
            if (not isinstance(reference, dict) or set(reference) != {"path", "pointer"}
                    or not _valid_pointer(reference["pointer"])):
                raise AcceptanceError("invalid artifact reference")
            _path(reference["path"])
        selections = rule.get("id_selections", [])
        if not isinstance(selections, list) or len(selections) > 8:
            raise AcceptanceError("invalid ID selection")
        for selection in selections:
            if (not isinstance(selection, dict) or not {"value", "source"} <= set(selection)
                    or set(selection) - {"value", "source", "mask", "indices"}
                    or ("mask" in selection) == ("indices" in selection)
                    or not _valid_pointer(selection["value"])):
                raise AcceptanceError("invalid ID selection")
            for name in ("source", "mask" if "mask" in selection else "indices"):
                reference = selection[name]
                if (not isinstance(reference, dict) or set(reference) != {"path", "pointer"}
                        or not _valid_pointer(reference["pointer"])):
                    raise AcceptanceError("invalid ID selection")
                _path(reference["path"])
        numbers = rule.get("numbers", {})
        if (not isinstance(numbers, dict) or len(numbers) > 16
                or any(not _valid_pointer(p) or not _valid_bounds(b) for p, b in numbers.items())):
            raise AcceptanceError("invalid numeric field assertions")
        if revision == 2 and (any(numbers.values()) or any(
            _finite_number(value) for value in rule.get("equals", {}).values()
        )):
            raise AcceptanceError("unclassified scientific target")
        expectations = rule.get("expectations", {})
        if not isinstance(expectations, dict) or len(expectations) > 16:
            raise AcceptanceError("invalid scientific expectations")
        for pointer, expectation in expectations.items():
            if (not _valid_pointer(pointer) or not isinstance(expectation, dict)
                    or set(expectation) - {"kind", "bounds", "equals", "reference"}
                    or expectation.get("kind") not in ("integrity", "user", "literature",
                                                        "exploratory")
                    or ("bounds" in expectation) == ("equals" in expectation)
                    or ("equals" in expectation and not _finite_number(expectation["equals"]))
                    or ("bounds" in expectation and (not expectation["bounds"]
                                                     or not _valid_bounds(expectation["bounds"])))):
                raise AcceptanceError("invalid scientific expectations")
            reference = expectation.get("reference")
            if expectation["kind"] != "exploratory" or reference is not None:
                if (not isinstance(reference, dict) or set(reference) != {"path", "pointer"}
                        or not isinstance(reference["path"], str)
                        or reference["path"] not in sources
                        or not _valid_pointer(reference["pointer"])):
                    raise AcceptanceError("scientific expectation needs a declared source")
        ratios = rule.get("ratios", [])
        if not isinstance(ratios, list) or len(ratios) > 16:
            raise AcceptanceError("invalid ratio assertions")
        for ratio in ratios:
            if (not isinstance(ratio, dict)
                    or not {"value", "numerator", "denominator"} <= set(ratio)
                    or set(ratio) - {"value", "numerator", "denominator", "scale", "atol"}
                    or any(not _valid_pointer(ratio[key])
                           for key in ("value", "numerator", "denominator"))
                    or not _finite_number(ratio.get("scale", 1))
                    or ratio.get("scale", 1) == 0
                    or not _finite_number(ratio.get("atol", 0))
                    or ratio.get("atol", 0) < 0):
                raise AcceptanceError("invalid ratio assertion")
        comparisons = rule.get("max_abs_differences", [])
        if not isinstance(comparisons, list) or len(comparisons) > 16:
            raise AcceptanceError("invalid maximum difference assertions")
        for comparison in comparisons:
            if (not isinstance(comparison, dict)
                    or not {"value", "actual", "reference"} <= set(comparison)
                    or set(comparison) - {"value", "actual", "reference", "scale", "atol",
                                          "required_metrics"}
                    or any(not _valid_pointer(comparison[k])
                           for k in ("value", "actual", "reference"))
                    or not _finite_number(comparison.get("scale", 1))
                    or comparison.get("scale", 1) <= 0
                    or not _finite_number(comparison.get("atol", 0))
                    or comparison.get("atol", 0) < 0):
                raise AcceptanceError("invalid maximum difference assertion")
            if "required_metrics" in comparison:
                metrics = comparison["required_metrics"]
                if (not isinstance(metrics, list) or not 1 <= len(metrics) <= 128
                        or any(not isinstance(k, str) or not 1 <= len(k) <= 120
                               or any(ord(c) < 32 for c in k) for k in metrics)
                        or len(set(metrics)) != len(metrics)):
                    raise AcceptanceError("invalid required metric labels")
        rates = rule.get("threshold_rates", [])
        if not isinstance(rates, list) or len(rates) > 8:
            raise AcceptanceError("invalid threshold rate assertions")
        for rate in rates:
            required = {*_THRESHOLD_POINTERS, "observed", "simulated"}
            if (not isinstance(rate, dict) or not required <= set(rate)
                    or set(rate) - required - {"scale", "atol", "comparison"}
                    or any(not _valid_pointer(rate[k]) for k in _THRESHOLD_POINTERS)
                    or rate.get("comparison", "gt") not in ("gt", "ge")
                    or not _finite_number(rate.get("scale", 1)) or rate.get("scale", 1) <= 0
                    or not _finite_number(rate.get("atol", 0)) or rate.get("atol", 0) < 0):
                raise AcceptanceError("invalid threshold rate assertions")
            _path(rate["observed"])
            _path(rate["simulated"])
    for rule in arrays:
        if (not isinstance(rule, dict) or not {"path", "shape"} <= set(rule)
                or set(rule) - {"path", "shape", "finite", "nonconstant", "minimum", "maximum",
                                "axes"}):
            raise AcceptanceError("invalid array assertion")
        _path(rule["path"])
        shape = rule["shape"]
        if (not isinstance(shape, list) or len(shape) > 8
                or any(n is not None and (type(n) is not int or not 0 <= n <= 1_000_000_000)
                       for n in shape)
                or type(rule.get("finite", True)) is not bool
                or type(rule.get("nonconstant", False)) is not bool
                or any(not _finite_number(rule[k]) for k in ("minimum", "maximum") if k in rule)
                or ("minimum" in rule and "maximum" in rule
                    and rule["minimum"] > rule["maximum"])):
            raise AcceptanceError("invalid array shape or finite flag")
        axes = rule.get("axes", {})
        if not isinstance(axes, dict) or len(axes) > 8:
            raise AcceptanceError("invalid array axes")
        for axis, reference in axes.items():
            if (axis not in tuple(str(i) for i in range(len(shape)))
                    or not isinstance(reference, dict) or set(reference) != {"path", "pointer"}
                    or not _valid_pointer(reference["pointer"])):
                raise AcceptanceError("invalid array axes")
            _path(reference["path"])
    # A streaming declaration must not relax a small JSON/receipt/code parser.
    small_paths = {rule["path"] for rule in rules} | {rule["path"] for rule in arrays}
    small_paths.update(call["path"] for call in calls)
    small_paths.update(call["source"]["path"] for call in calls)
    small_paths.update(p for call in calls for p in call.get("artifacts", {}).get("code", []))
    for rule in rules:
        for name in ("source_json", "artifact_json"):
            small_paths.update(ref["path"] for ref in rule.get(name, {}).values())
        small_paths.update(item["reference"]["path"] for item in
                           rule.get("expectations", {}).values() if "reference" in item)
        for selection in rule.get("id_selections", []):
            small_paths.update(selection[name]["path"] for name in
                               ("source", "mask" if "mask" in selection else "indices"))
        for rate in rule.get("threshold_rates", []):
            small_paths.update((rate["observed"], rate["simulated"]))
    small_paths.update(ref["path"] for rule in arrays for ref in rule.get("axes", {}).values())
    for rule in large_files.values():
        for reference in hdf_references(rule):
            if reference["path"] not in sources:
                raise AcceptanceError("invalid large file declaration")
            small_paths.add(reference["path"])
    if small_paths & set(large_files):
        raise AcceptanceError("invalid large file declaration")
    try:
        root = workspace.resolve(strict=True)
        info = root.stat()
        initial = ArtifactAcceptance(root, (info.st_dev, info.st_ino), (),
                                     json.dumps(rules), json.dumps(arrays),
                                     deferred["path"] if deferred is not None else None,
                                     json.dumps(calls), revision, json.dumps(feasibility),
                                     json.dumps(large_files))
        if frozen is None:
            baselines = tuple((path, str(initial._fingerprint(path)["sha256"]))
                              for path in sources)
        else:
            identity, baselines = frozen
            if identity != initial.identity or [path for path, _ in baselines] != sources:
                raise AcceptanceError("saved workspace or source identity mismatch")
        return ArtifactAcceptance(root, initial.identity, baselines, initial.rules,
                                  initial.arrays, initial.deferred, initial.calls,
                                  initial.research_revision, initial.feasibility,
                                  initial.large_files)
    except OSError as error:
        raise AcceptanceError("workspace inaccessible") from error


def read_declared_contract(workspace: Path, path: str) -> str:
    """Read a bounded workspace contract; does not assert who authored it or when."""
    try:
        root = workspace.resolve(strict=True)
        info = root.stat()
        reader = ArtifactAcceptance(root, (info.st_dev, info.st_ino), (), "[]")
        data = reader._read(_path(path))
        if len(data) > 8192:
            raise AcceptanceError("contract file too large")
        spec = _json(data)
        return PREFIX + json.dumps(spec, ensure_ascii=True)
    except OSError as error:
        raise AcceptanceError("workspace inaccessible") from error
