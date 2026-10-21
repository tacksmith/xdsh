"""Run one answer-free, isolated XDSH built-in Skill A/B task.

The command is an operations-only measurement seam.  It launches the same
official SDK/JSON-RPC runtime and Cordis composition for both conditions.  The
``on`` condition projects the verified package bundle into a fresh DSH home;
the ``off`` condition leaves that fresh home without package Skills.  Results
contain only hashes, counters, safe tool names, and allowlisted built-in Skill
names.  Prompt text, model text, tool arguments, and tool results are never
written to the result artifact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from xdsh.core.errors import RunnerConfigurationError
from xdsh.core.events import (
    ErrorEvent,
    ReasoningEvent,
    RetryEvent,
    RunnerEventT,
    TextEvent,
    ToolResultEvent,
    ToolStartEvent,
    UsageEvent,
)
from xdsh.core.execution_policy import (
    with_direct_execution_policy,
    with_optional_skill_routing_policy,
)
from xdsh.core.presentation import safe_error_code
from xdsh.core.runner import REASONING_EFFORTS
from xdsh.core.skills import (
    BUILTIN_SKILLS,
    builtin_skill_bundle_version,
    ensure_builtin_skills,
)
from xdsh.ops.benchmark import (
    _runtime_command,
    _safe_error,
    _workspace_fingerprint,
    _write_result,
)
from xdsh.runners.dsh_events import DshEventNormalizer
from xdsh.runners.sdk import Harness, HarnessFactory, HarnessLaunch, create_official_harness

_TASK_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,79}$")
_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}$")
_TOOL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
_CONDITIONS = ("off", "on")
_SUCCESS_REASONS = frozenset({"completed", "complete", "success", "succeeded"})
_MAX_CATALOG_ENTRIES = 256
_DEFAULT_CORDIS = Path(__file__).parents[1] / "runners" / "anthropic_litellm.cordis.yml"
_SYSTEM_PROMPT = (
    "You are an XDSH software engineering agent in an isolated benchmark workspace. "
    "Complete the user task directly, preserve evidence, and verify the result."
)


@dataclass(frozen=True, slots=True)
class _PreparedHome:
    session_root: Path
    projected_skills: int


class _EventAggregate:
    """Content-free projection of normalized runner events."""

    def __init__(self) -> None:
        self.event_kinds: Counter[str] = Counter()
        self.tool_names: Counter[str] = Counter()
        self.selected_builtin_skills: list[str] = []
        self.skill_tool_calls = 0
        self.unknown_skill_calls = 0
        self.tool_errors = 0
        self.retries = 0
        self.error_codes: set[str] = set()
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read_tokens = 0
        self.cache_write_tokens = 0
        self.reasoning_tokens = 0
        self.text_characters = 0
        self.reasoning_characters = 0
        self.catalog_events = 0
        self.catalog_entry_count = 0
        self.catalog_builtin_skills: list[str] = []
        self.catalog_unknown_entries = 0
        self.catalog_truncated = False

    def feed_notification(self, notification: object, *, session_id: str) -> None:
        """Project only safe names from the official durable Skill catalog event."""

        method = getattr(notification, "method", None)
        payload = getattr(notification, "payload", None)
        if method != "session.event" or not isinstance(payload, dict):
            return
        if payload.get("sessionId") != session_id:
            return
        event = payload.get("event")
        if not isinstance(event, dict) or event.get("type") != "user/message":
            return
        data = event.get("data")
        source = data.get("source") if isinstance(data, dict) else None
        if not isinstance(source, dict) or source.get("kind") != "skill-catalog":
            return
        entries = source.get("entries")
        if not isinstance(entries, list):
            return
        self.catalog_events += 1
        self.catalog_entry_count = min(len(entries), _MAX_CATALOG_ENTRIES)
        self.catalog_truncated = len(entries) > _MAX_CATALOG_ENTRIES
        names: list[str] = []
        unknown = 0
        for entry in entries[:_MAX_CATALOG_ENTRIES]:
            name = entry.get("name") if isinstance(entry, dict) else None
            if isinstance(name, str) and name in BUILTIN_SKILLS:
                if name not in names:
                    names.append(name)
            else:
                unknown += 1
        self.catalog_builtin_skills = sorted(names)
        self.catalog_unknown_entries = unknown

    def feed(self, event: RunnerEventT) -> None:
        self.event_kinds[event.kind.value] += 1
        if isinstance(event, ToolStartEvent):
            name = _safe_tool_name(event.name)
            self.tool_names[name] += 1
            if event.name == "skill":
                self.skill_tool_calls += 1
                skill = _selected_builtin_skill(event.arguments)
                if skill is None:
                    self.unknown_skill_calls += 1
                elif skill not in self.selected_builtin_skills:
                    self.selected_builtin_skills.append(skill)
        elif isinstance(event, ToolResultEvent):
            self.tool_errors += int(event.is_error)
        elif isinstance(event, UsageEvent):
            self.input_tokens += event.input_tokens
            self.output_tokens += event.output_tokens
            self.cache_read_tokens += event.cache_read_tokens
            self.cache_write_tokens += event.cache_write_tokens
            self.reasoning_tokens += event.reasoning_tokens
        elif isinstance(event, TextEvent):
            self.text_characters += len(event.text)
        elif isinstance(event, ReasoningEvent):
            self.reasoning_characters += len(event.text)
        elif isinstance(event, RetryEvent):
            self.retries += 1
        elif isinstance(event, ErrorEvent):
            self.error_codes.add(_safe_category(event.code))

    def as_dict(self) -> dict[str, object]:
        return {
            "event_kinds": dict(sorted(self.event_kinds.items())),
            "tool_names": dict(sorted(self.tool_names.items())),
            "selected_builtin_skills": list(self.selected_builtin_skills),
            "skill_tool_calls": self.skill_tool_calls,
            "unknown_skill_calls": self.unknown_skill_calls,
            "tool_errors": self.tool_errors,
            "retries": self.retries,
            "errors": sorted(self.error_codes),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "text_characters": self.text_characters,
            "reasoning_characters": self.reasoning_characters,
            "catalog_events": self.catalog_events,
            "catalog_entry_count": self.catalog_entry_count,
            "catalog_builtin_skills": list(self.catalog_builtin_skills),
            "catalog_unknown_entries": self.catalog_unknown_entries,
            "catalog_truncated": self.catalog_truncated,
        }


def _safe_tool_name(name: str) -> str:
    return name if _TOOL_NAME.fullmatch(name) is not None else "<invalid>"


def _safe_category(value: str) -> str:
    return safe_error_code(value)


def _selected_builtin_skill(arguments: object) -> str | None:
    if not isinstance(arguments, dict):
        return None
    for key in ("name", "skill", "skillName"):
        value = arguments.get(key)
        if isinstance(value, str) and value in BUILTIN_SKILLS:
            return value
    return None


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _is_within(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def _resolve_non_symlink(path: Path, *, label: str) -> Path:
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise ValueError(f"{label} must not be a symlink")
    return expanded.resolve()


def _validate_paths(
    *,
    prompt_file: Path,
    workspace: Path,
    dsh_home: Path,
    output: Path,
) -> None:
    for path in (workspace, dsh_home, output):
        if ".xclaude" in path.parts or ".dsh" in path.parts:
            raise ValueError("Skill benchmark paths must avoid reserved runtime roots")
    if workspace == Path(workspace.anchor) or dsh_home == Path(dsh_home.anchor):
        raise ValueError("Skill benchmark roots must be dedicated subdirectories")
    if workspace.is_symlink() or dsh_home.is_symlink() or output.is_symlink():
        raise ValueError("Skill benchmark paths must not be symlinks")
    if not prompt_file.is_file() or prompt_file.is_symlink():
        raise ValueError("prompt file must be a regular non-symlink file")
    if not workspace.is_dir():
        raise ValueError("workspace must be an existing directory")
    if _is_within(dsh_home, workspace) or _is_within(workspace, dsh_home):
        raise ValueError("workspace and DSH home must be isolated from each other")
    if _is_within(output, workspace) or _is_within(output, dsh_home):
        raise ValueError("result artifact must remain outside the agent workspace and DSH home")
    if _is_within(prompt_file, workspace) or _is_within(prompt_file, dsh_home):
        raise ValueError("prompt staging must remain outside the agent workspace and DSH home")


def _prepare_home(dsh_home: Path, condition: str) -> _PreparedHome:
    if condition not in _CONDITIONS:
        raise ValueError("condition must be off or on")
    if dsh_home.exists():
        if not dsh_home.is_dir() or any(dsh_home.iterdir()):
            raise ValueError("DSH home must be fresh and empty for every A/B run")
    else:
        dsh_home.mkdir(mode=0o700, parents=True)
    dsh_home.chmod(0o700)
    session_root = dsh_home / "sessions"
    session_root.mkdir(mode=0o700)
    projected = 0
    if condition == "on":
        report = ensure_builtin_skills(dsh_home)
        if not report.ready:
            raise RunnerConfigurationError("built-in Skill benchmark projection was not ready")
        projected = len(report.installed) + len(report.updated) + len(report.verified)
        if projected != len(BUILTIN_SKILLS):
            raise RunnerConfigurationError("built-in Skill benchmark projection was incomplete")
    return _PreparedHome(session_root=session_root, projected_skills=projected)


def _benchmark_system_prompt(condition: str) -> str:
    """Compose the product policy belonging to one measured condition."""

    prompt = with_direct_execution_policy(_SYSTEM_PROMPT)
    if condition == "on":
        return with_optional_skill_routing_policy(prompt)
    if condition == "off":
        return prompt
    raise ValueError("condition must be off or on")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--condition", choices=_CONDITIONS, required=True)
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--dsh-home", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--model", default=os.environ.get("XDSH_LITELLM_MODEL", ""))
    parser.add_argument("--provider", default="litellm")
    parser.add_argument("--cordis", type=Path, default=_DEFAULT_CORDIS)
    parser.add_argument("--runtime-command", default=os.environ.get("XDSH_DSH_RUNTIME_COMMAND"))
    parser.add_argument("--task-timeout", type=float, default=300.0)
    parser.add_argument("--reasoning-effort", choices=REASONING_EFFORTS, default="off")
    parser.add_argument("--max-tokens", type=int, default=8_192)
    parser.add_argument("--context-window", type=int, default=262_144)
    parser.add_argument("--model-max-tokens", type=int, default=32_768)
    return parser


def _run(
    args: argparse.Namespace,
    *,
    harness_factory: HarnessFactory = create_official_harness,
) -> dict[str, object]:
    task_id = str(args.task_id)
    session_id = str(args.session_id)
    if _TASK_ID.fullmatch(task_id) is None:
        raise ValueError("task ID must use safe lowercase benchmark characters")
    if _SESSION_ID.fullmatch(session_id) is None:
        raise ValueError("session ID contains unsupported characters")
    if not str(args.model).strip():
        raise ValueError("--model or XDSH_LITELLM_MODEL is required")
    if not 0 < args.task_timeout <= 3_600:
        raise ValueError("task timeout must be between 0 and 3600 seconds")
    if args.max_tokens <= 0 or args.context_window <= 0 or args.model_max_tokens <= 0:
        raise ValueError("token capacities must be positive")

    prompt_file = _resolve_non_symlink(args.prompt_file, label="prompt file")
    workspace = _resolve_non_symlink(args.workspace, label="workspace")
    dsh_home = _resolve_non_symlink(args.dsh_home, label="DSH home")
    output = _resolve_non_symlink(args.output, label="result artifact")
    cordis = _resolve_non_symlink(args.cordis, label="Cordis config")
    _validate_paths(
        prompt_file=prompt_file,
        workspace=workspace,
        dsh_home=dsh_home,
        output=output,
    )
    if not cordis.is_file() or cordis.is_symlink():
        raise ValueError("Cordis config must be a regular non-symlink file")
    prompt_bytes = prompt_file.read_bytes()
    if not prompt_bytes.strip() or len(prompt_bytes) > 200_000:
        raise ValueError("prompt must be non-empty and at most 200000 bytes")
    prompt = prompt_bytes.decode("utf-8")
    prepared = _prepare_home(dsh_home, str(args.condition))
    initial_files, initial_bytes, initial_sha256 = _workspace_fingerprint(workspace)
    launch = HarnessLaunch(
        cwd=workspace,
        dsh_home=dsh_home,
        session_root=prepared.session_root,
        cordis=cordis,
        provider=str(args.provider),
        model=str(args.model),
        reasoning_effort=args.reasoning_effort,
        system_prompt=_benchmark_system_prompt(str(args.condition)),
        context_window=args.context_window,
        model_max_tokens=args.model_max_tokens,
        max_tokens=args.max_tokens,
        runtime_command=_runtime_command(args.runtime_command),
        request_timeout_seconds=args.task_timeout,
        shutdown_timeout_seconds=3.0,
    )

    aggregate = _EventAggregate()
    normalizer = DshEventNormalizer(session_id=session_id, dsh_session_id=session_id)
    started = time.monotonic()
    first_event_ms: int | None = None
    finish_reason = "exception"
    final_text_characters = 0
    exception_category: str | None = None
    harness: Harness | None = None

    def collect(notification: object) -> None:
        nonlocal first_event_ms
        aggregate.feed_notification(notification, session_id=session_id)
        for event in normalizer.feed(notification):
            if first_event_ms is None:
                first_event_ms = round((time.monotonic() - started) * 1000)
            aggregate.feed(event)

    try:
        harness = harness_factory(launch)
        result = harness.run(prompt, session_id=session_id, on_notification=collect)
        finish_reason = result.finish_reason or normalizer.finish_reason or "unknown"
        final_text_characters = len(result.final_response)
    except Exception as error:
        exception_category = type(error).__name__
        aggregate.error_codes.add(_safe_category(exception_category))
    finally:
        if harness is not None:
            try:
                harness.close()
            except Exception as error:
                if exception_category is None:
                    exception_category = type(error).__name__
                    aggregate.error_codes.add(_safe_category(exception_category))

    final_files, final_bytes, final_sha256 = _workspace_fingerprint(workspace)
    elapsed_ms = round((time.monotonic() - started) * 1000)
    result_payload: dict[str, object] = {
        "schema_version": 1,
        "benchmark_result": "aggregate_only",
        "benchmark": "xdsh_builtin_skill_ab",
        "task_id": task_id,
        "condition": args.condition,
        "bundle_version": builtin_skill_bundle_version(),
        "available_builtin_skills": prepared.projected_skills,
        "model": args.model,
        "provider": args.provider,
        "session_id": session_id,
        "prompt_sha256": _sha256_bytes(prompt_bytes),
        "succeeded": exception_category is None and finish_reason in _SUCCESS_REASONS,
        "finish_reason": safe_error_code(finish_reason),
        "elapsed_ms": elapsed_ms,
        "first_event_ms": first_event_ms,
        "final_text_characters": final_text_characters,
        "initial_workspace_file_count": initial_files,
        "initial_workspace_bytes": initial_bytes,
        "initial_workspace_sha256": initial_sha256,
        "final_workspace_file_count": final_files,
        "final_workspace_bytes": final_bytes,
        "final_workspace_sha256": final_sha256,
        **aggregate.as_dict(),
    }
    if exception_category is not None:
        result_payload["exception"] = exception_category
    return result_payload


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    try:
        result = _run(args)
        _write_result(args.output.expanduser().resolve(), result)
    except (OSError, UnicodeDecodeError, ValueError, RunnerConfigurationError) as error:
        raise SystemExit(_safe_error(error)) from None
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
