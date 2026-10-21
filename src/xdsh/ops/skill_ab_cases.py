"""Stage and grade answer-free built-in Skill A/B cases.

The grader is never mounted into the agent workspace during execution.  It
checks immutable input hashes and bounded structural outcomes after the task;
there is no reference patch or reference answer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from xdsh.core.skills import BUILTIN_SKILLS

_MAX_ARTIFACT_BYTES = 20_000


@dataclass(frozen=True, slots=True)
class SkillAbCase:
    id: str
    target_skills: tuple[str, ...]
    prompt: str
    files: Mapping[str, str]
    artifact: str
    min_on_skill_calls: int = 0
    max_on_skill_calls: int = 1
    assessed_conditions: tuple[str, ...] = ("off", "on")


_CASES = (
    SkillAbCase(
        id="repair-and-verify",
        target_skills=("xdsh-testing-verification", "xdsh-direct-execution"),
        prompt=(
            "Fix the implementation in this workspace so the existing tests pass. "
            "Do not modify or replace the tests. Keep the code change minimal, run the "
            "exact relevant test command, and verify the final state before reporting."
        ),
        files={
            "calc.py": (
                "from __future__ import annotations\n\n\n"
                "def moving_average(values: list[float], window: int) -> list[float]:\n"
                "    if window <= 0:\n"
                "        raise ValueError(\"window must be positive\")\n"
                "    if window > len(values):\n"
                "        return []\n"
                "    return [\n"
                "        sum(values[index : index + window]) / (window + 1)\n"
                "        for index in range(len(values) - window + 1)\n"
                "    ]\n"
            ),
            "test_calc.py": (
                "from __future__ import annotations\n\n"
                "import unittest\n\n"
                "from calc import moving_average\n\n\n"
                "class MovingAverageTests(unittest.TestCase):\n"
                "    def test_pairs(self) -> None:\n"
                "        self.assertEqual(moving_average([2.0, 4.0, 8.0], 2), [3.0, 6.0])\n\n"
                "    def test_window_one(self) -> None:\n"
                "        self.assertEqual(moving_average([1.5, 2.5], 1), [1.5, 2.5])\n\n"
                "    def test_full_window(self) -> None:\n"
                "        self.assertEqual(moving_average([1.0, 2.0, 6.0], 3), [3.0])\n\n"
                "    def test_oversized_window(self) -> None:\n"
                "        self.assertEqual(moving_average([1.0], 2), [])\n\n"
                "    def test_invalid_window(self) -> None:\n"
                "        with self.assertRaises(ValueError):\n"
                "            moving_average([1.0], 0)\n"
            ),
        },
        artifact="calc.py",
    ),
    SkillAbCase(
        id="investigate-with-evidence",
        target_skills=("xdsh-bounded-investigation", "xdsh-evidence-reporting"),
        prompt=(
            "Investigate why request-42 retried before succeeding. Do not change the "
            "evidence files. Write finding.md with separate Observed facts, Inference, "
            "Conflicting evidence, and Unresolved questions sections. Cite exact workspace "
            "file names and keep conclusions narrower than the evidence."
        ),
        files={
            "service.log": (
                "06:00:01 request-42 attempt=1 result=timeout elapsed_ms=1200\n"
                "06:00:03 request-42 attempt=2 result=timeout elapsed_ms=1180\n"
                "06:00:05 request-42 attempt=3 result=success elapsed_ms=240\n"
                "06:01:00 request-43 attempt=1 result=success elapsed_ms=210\n"
            ),
            "config.toml": (
                "request_timeout_ms = 1000\n"
                "retry_limit = 2\n"
                "retry_backoff_ms = 500\n"
            ),
            "OPERATIONS.md": (
                "# Retry policy\n\n"
                "The operations handbook says a request receives at most one retry.\n"
                "Configuration is authoritative until this handbook is reconciled.\n"
            ),
        },
        artifact="finding.md",
    ),
    SkillAbCase(
        id="explicit-single-skill",
        target_skills=("xdsh-bounded-investigation",),
        prompt=(
            "Use xdsh-bounded-investigation as the single optional procedural Skill for "
            "this task; do not load another Skill. Investigate why request-42 retried "
            "before succeeding without changing the evidence files. Write finding.md "
            "with separate Observed facts, Inference, Conflicting evidence, and "
            "Unresolved questions sections. Cite exact workspace file names and keep "
            "conclusions narrower than the evidence."
        ),
        files={
            "service.log": (
                "06:00:01 request-42 attempt=1 result=timeout elapsed_ms=1200\n"
                "06:00:03 request-42 attempt=2 result=timeout elapsed_ms=1180\n"
                "06:00:05 request-42 attempt=3 result=success elapsed_ms=240\n"
                "06:01:00 request-43 attempt=1 result=success elapsed_ms=210\n"
            ),
            "config.toml": (
                "request_timeout_ms = 1000\n"
                "retry_limit = 2\n"
                "retry_backoff_ms = 500\n"
            ),
            "OPERATIONS.md": (
                "# Retry policy\n\n"
                "The operations handbook says a request receives at most one retry.\n"
                "Configuration is authoritative until this handbook is reconciled.\n"
            ),
        },
        artifact="finding.md",
        min_on_skill_calls=1,
        assessed_conditions=("on",),
    ),
    SkillAbCase(
        id="synthesize-conflicting-sources",
        target_skills=(
            "xdsh-deep-research",
            "xdsh-evidence-reporting",
            "xdsh-artifact-quality",
        ),
        prompt=(
            "Create brief.md recommending a safe concurrency setting from the supplied "
            "sources. Separate sourced facts from inference, cite every source file, "
            "explicitly address conflicting limits, and include one unresolved question. "
            "Do not modify the source files."
        ),
        files={
            "sources/vendor.md": (
                "# Vendor guidance\n\n"
                "The service supports up to 500 concurrent requests under ideal conditions.\n"
            ),
            "sources/operations.md": (
                "# Operations constraint\n\n"
                "The current connection pool is capped at 350 concurrent requests.\n"
            ),
            "sources/measurements.csv": (
                "minute,concurrency,error_rate\n"
                "1,300,0.002\n"
                "2,350,0.006\n"
                "3,410,0.081\n"
            ),
        },
        artifact="brief.md",
    ),
)


def skill_ab_cases() -> tuple[SkillAbCase, ...]:
    return _CASES


def _case(case_id: str) -> SkillAbCase:
    try:
        return next(case for case in _CASES if case.id == case_id)
    except StopIteration as error:
        raise ValueError(f"unknown Skill A/B case: {case_id}") from error


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _fixture_digest(case: SkillAbCase) -> str:
    rows = [
        f"{name}:{_digest(content.encode('utf-8'))}"
        for name, content in sorted(case.files.items())
    ]
    return _digest("\n".join(rows).encode("ascii"))


def case_manifest() -> dict[str, object]:
    if any(
        skill not in BUILTIN_SKILLS
        for case in _CASES
        for skill in case.target_skills
    ):
        raise RuntimeError("Skill A/B manifest references an unknown package Skill")
    if any(
        case.min_on_skill_calls < 0
        or case.max_on_skill_calls < case.min_on_skill_calls
        or case.max_on_skill_calls > 2
        for case in _CASES
    ):
        raise RuntimeError("Skill A/B manifest has invalid routing bounds")
    if any(
        not case.assessed_conditions
        or len(case.assessed_conditions) != len(set(case.assessed_conditions))
        or any(condition not in {"off", "on"} for condition in case.assessed_conditions)
        for case in _CASES
    ):
        raise RuntimeError("Skill A/B manifest has invalid assessed conditions")
    return {
        "schema_version": 1,
        "suite": "xdsh_builtin_skill_ab",
        "answer_present": False,
        "tasks": [
            {
                "id": case.id,
                "target_skills": list(case.target_skills),
                "prompt_sha256": _digest((case.prompt + "\n").encode("utf-8")),
                "fixture_sha256": _fixture_digest(case),
                "answer_present": False,
                "assessed_conditions": list(case.assessed_conditions),
                "routing_expectation": {
                    condition: (
                        {
                            "min_calls": case.min_on_skill_calls,
                            "max_calls": case.max_on_skill_calls,
                        }
                        if condition == "on"
                        else {"min_calls": 0, "max_calls": 0}
                    )
                    for condition in case.assessed_conditions
                },
            }
            for case in _CASES
        ],
    }


def _safe_root(raw: Path, *, create: bool) -> Path:
    expanded = raw.expanduser()
    if expanded.is_symlink():
        raise ValueError("case root must not be a symlink")
    root = expanded.resolve()
    if root == Path(root.anchor) or root == Path.home().resolve():
        raise ValueError("case root must be a dedicated subdirectory")
    if ".xclaude" in root.parts or ".dsh" in root.parts:
        raise ValueError("case root must avoid reserved runtime roots")
    if create:
        if root.exists() and (not root.is_dir() or any(root.iterdir())):
            raise ValueError("case root must be fresh and empty")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        root.chmod(0o700)
    elif not root.is_dir():
        raise ValueError("case root does not exist")
    return root


def _write_exclusive(path: Path, content: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)


def stage_case(case_id: str, root: Path) -> dict[str, object]:
    case = _case(case_id)
    resolved = _safe_root(root, create=True)
    workspace = resolved / "workspace"
    workspace.mkdir(mode=0o700)
    for relative, content in case.files.items():
        _write_exclusive(workspace / relative, content)
    prompt = resolved / "prompt.md"
    _write_exclusive(prompt, case.prompt + "\n")
    return {
        "task_id": case.id,
        "prompt_sha256": _digest((case.prompt + "\n").encode("utf-8")),
        "fixture_sha256": _fixture_digest(case),
        "input_files": len(case.files),
    }


def _inputs_unchanged(case: SkillAbCase, workspace: Path) -> bool:
    for relative, expected in case.files.items():
        path = workspace / relative
        if not path.is_file() or path.is_symlink():
            return False
        if _digest(path.read_bytes()) != _digest(expected.encode("utf-8")):
            return False
    return True


def _bounded_artifact(workspace: Path, relative: str) -> str | None:
    artifact = workspace / relative
    if not artifact.is_file() or artifact.is_symlink():
        return None
    if not 0 < artifact.stat().st_size <= _MAX_ARTIFACT_BYTES:
        return None
    try:
        return artifact.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None


def _read_result(path: Path) -> dict[str, object]:
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise ValueError("benchmark result must not be a symlink")
    resolved = expanded.resolve()
    if ".xclaude" in resolved.parts or ".dsh" in resolved.parts:
        raise ValueError("benchmark result must avoid reserved runtime roots")
    if not resolved.is_file() or resolved.stat().st_size > 128_000:
        raise ValueError("benchmark result is unavailable or too large")
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("benchmark result is invalid") from error
    if not isinstance(payload, dict):
        raise ValueError("benchmark result must be an object")
    return payload


def assess_routing(case_id: str, result_path: Path) -> dict[str, object]:
    """Assess only content-free routing telemetry for one staged case."""

    case = _case(case_id)
    result = _read_result(result_path)
    condition = result.get("condition")
    calls = result.get("skill_tool_calls")
    selected = result.get("selected_builtin_skills")
    if condition not in {"off", "on"}:
        raise ValueError("benchmark result condition is invalid")
    if condition not in case.assessed_conditions:
        raise ValueError("benchmark result condition is not assessed for this case")
    if not isinstance(calls, int) or isinstance(calls, bool) or calls < 0:
        raise ValueError("benchmark result Skill call count is invalid")
    if not isinstance(selected, list) or not all(isinstance(item, str) for item in selected):
        raise ValueError("benchmark result Skill selection is invalid")
    minimum = case.min_on_skill_calls if condition == "on" else 0
    maximum = case.max_on_skill_calls if condition == "on" else 0
    expected_available = len(BUILTIN_SKILLS) if condition == "on" else 0
    expected_catalog = list(BUILTIN_SKILLS) if condition == "on" else []
    prompt_sha256 = _digest((case.prompt + "\n").encode("utf-8"))
    catalog_events = result.get("catalog_events")
    checks = {
        "result_schema_matches": result.get("schema_version") == 1
        and result.get("benchmark") == "xdsh_builtin_skill_ab",
        "task_matches": result.get("task_id") == case.id,
        "prompt_matches": result.get("prompt_sha256") == prompt_sha256,
        "run_succeeded": result.get("succeeded") is True,
        "available_bundle_matches": result.get("available_builtin_skills")
        == expected_available,
        "native_catalog_matches": result.get("catalog_builtin_skills")
        == expected_catalog,
        "native_catalog_count_matches": result.get("catalog_entry_count")
        == expected_available,
        "native_catalog_safe": result.get("catalog_unknown_entries") == 0
        and result.get("catalog_truncated") is False,
        "native_catalog_observed": (
            isinstance(catalog_events, int)
            and catalog_events >= (1 if condition == "on" else 0)
        ),
        "skill_call_bounds": minimum <= calls <= maximum,
        "selected_skills_allowed": all(item in case.target_skills for item in selected),
        "selected_skills_unique": len(selected) == len(set(selected)),
        "selected_call_count_matches": len(selected) == calls,
        "unknown_skill_calls_absent": result.get("unknown_skill_calls") == 0,
        "tool_errors_absent": result.get("tool_errors") == 0,
        "runner_errors_absent": result.get("errors") == [],
    }
    return {
        "task_id": case.id,
        "condition": condition,
        "passed": all(checks.values()),
        "checks": checks,
    }


def _contains_all(text: str, terms: Sequence[str]) -> bool:
    lowered = text.casefold()
    return all(term.casefold() in lowered for term in terms)


def _grade_repair(case: SkillAbCase, workspace: Path) -> dict[str, bool]:
    test_path = workspace / "test_calc.py"
    implementation_path = workspace / "calc.py"
    test_unchanged = (
        test_path.is_file()
        and not test_path.is_symlink()
        and _digest(test_path.read_bytes())
        == _digest(case.files["test_calc.py"].encode("utf-8"))
    )
    implementation_changed = (
        implementation_path.is_file()
        and not implementation_path.is_symlink()
        and _digest(implementation_path.read_bytes())
        != _digest(case.files["calc.py"].encode("utf-8"))
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-q"],
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
            check=False,
            env={"PYTHONDONTWRITEBYTECODE": "1"},
        )
        tests_pass = completed.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        tests_pass = False
    return {
        "test_file_unchanged": test_unchanged,
        "implementation_changed": implementation_changed,
        "tests_pass": tests_pass,
    }


def _grade_investigation(case: SkillAbCase, workspace: Path) -> dict[str, bool]:
    artifact = _bounded_artifact(workspace, case.artifact)
    text = artifact or ""
    return {
        "inputs_unchanged": _inputs_unchanged(case, workspace),
        "artifact_bounded": artifact is not None,
        "sections_present": _contains_all(
            text,
            ("observed", "inference", "conflicting", "unresolved"),
        ),
        "sources_cited": _contains_all(
            text,
            ("service.log", "config.toml", "operations.md"),
        ),
        "material_facts_present": _contains_all(
            text,
            ("request-42", "timeout", "retry_limit"),
        ),
    }


def _grade_synthesis(case: SkillAbCase, workspace: Path) -> dict[str, bool]:
    artifact = _bounded_artifact(workspace, case.artifact)
    text = artifact or ""
    return {
        "inputs_unchanged": _inputs_unchanged(case, workspace),
        "artifact_bounded": artifact is not None,
        "sources_cited": _contains_all(
            text,
            ("vendor.md", "operations.md", "measurements.csv"),
        ),
        "conflict_addressed": _contains_all(text, ("500", "350", "410", "conflict")),
        "fact_inference_separated": _contains_all(text, ("fact", "inference")),
        "unresolved_present": "unresolved" in text.casefold(),
    }


def grade_case(case_id: str, root: Path) -> dict[str, object]:
    case = _case(case_id)
    resolved = _safe_root(root, create=False)
    workspace = resolved / "workspace"
    if not workspace.is_dir() or workspace.is_symlink():
        raise ValueError("case workspace is unavailable")
    if any(
        path.is_symlink() or (path.is_file() and path.stat().st_size > 1_000_000)
        for path in workspace.rglob("*")
    ):
        checks = {"workspace_safe": False}
    elif case.id == "repair-and-verify":
        checks = {"workspace_safe": True, **_grade_repair(case, workspace)}
    elif case.id in {"investigate-with-evidence", "explicit-single-skill"}:
        checks = {"workspace_safe": True, **_grade_investigation(case, workspace)}
    else:
        checks = {"workspace_safe": True, **_grade_synthesis(case, workspace)}
    return {
        "task_id": case.id,
        "passed": all(checks.values()),
        "checks": checks,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    stage = subparsers.add_parser("stage")
    stage.add_argument("--task", choices=tuple(case.id for case in _CASES), required=True)
    stage.add_argument("--root", type=Path, required=True)
    grade = subparsers.add_parser("grade")
    grade.add_argument("--task", choices=tuple(case.id for case in _CASES), required=True)
    grade.add_argument("--root", type=Path, required=True)
    assess = subparsers.add_parser("assess")
    assess.add_argument("--task", choices=tuple(case.id for case in _CASES), required=True)
    assess.add_argument("--result", type=Path, required=True)
    subparsers.add_parser("manifest")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    try:
        if args.action == "stage":
            result = stage_case(args.task, args.root)
        elif args.action == "grade":
            result = grade_case(args.task, args.root)
        elif args.action == "assess":
            result = assess_routing(args.task, args.result)
        else:
            result = case_manifest()
    except (OSError, ValueError) as error:
        raise SystemExit(type(error).__name__) from None
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if args.action in {"grade", "assess"} and not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
