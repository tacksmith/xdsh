"""Run one answer-free benchmark task through the runner-neutral XDSH seam.

The command deliberately emits only aggregate metrics.  Task prompts are read
from a staging file at runtime and are never copied into the result artifact;
gold answers, reference patches, graders, and raw model text are outside the
runner's workspace by construction.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import shlex
import time
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from xdsh.core.events import (
    ErrorEvent,
    FinishEvent,
    ReasoningEvent,
    RuntimeEvent,
    TextEvent,
    UsageEvent,
)
from xdsh.core.presentation import redact_progress_text, safe_error_code
from xdsh.core.product_control import safe_exception_text
from xdsh.core.rate_limit import RateLimitedRunner, RateLimitPolicy
from xdsh.core.run_guard import GuardedRunner, RunGuardPolicy
from xdsh.runners.dsh import DshRunner, DshRunnerConfig

_DEFAULT_RUNTIME = (
    "/usr/local/bin/node",
    "/opt/xdsh/runtime/node_modules/@deepseek-ai/dsh-sdk-jsonrpc-demo/lib/packaged-bin.js",
)
_SUCCESS_REASONS = frozenset({"completed", "complete", "success", "succeeded"})
def _validate_task_timeout(value: float) -> float | None:
    """Normalize an optional wall-clock deadline.

    Zero deliberately means unlimited. Long-running local models can exceed an
    estimate without being cancelled merely because wall-clock time elapsed;
    the activity-aware guard remains responsible for stopping stalled runs.
    """

    timeout = float(value)
    if timeout == 0:
        return None
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError("task timeout must be zero or a finite positive number")
    return timeout


def _progress_payload(
    *,
    benchmark_id: str,
    session_id: str,
    model: str,
    provider: str,
    state: str,
    elapsed_ms: int,
    last_activity_ms: int,
    event_count: int,
    event_kinds: dict[str, int],
    tool_calls: int,
    retries: int,
    guards: int,
    input_tokens: int,
    output_tokens: int,
) -> dict[str, object]:
    """Build an allowlisted progress record without task or response content."""

    return {
        "schema_version": 1,
        "benchmark_result": "aggregate_progress_only",
        "benchmark_id": benchmark_id,
        "session_id": session_id,
        "model": model,
        "provider": provider,
        "state": state,
        "elapsed_ms": max(0, elapsed_ms),
        "last_activity_ms": max(0, last_activity_ms),
        "event_count": max(0, event_count),
        "event_kinds": dict(sorted(event_kinds.items())),
        "tool_calls": max(0, tool_calls),
        "retries": max(0, retries),
        "guards": max(0, guards),
        "input_tokens": max(0, input_tokens),
        "output_tokens": max(0, output_tokens),
    }


async def _wait_for_status_interval(stop: asyncio.Event, *, timeout: float) -> bool:
    """Return whether the heartbeat stop signal arrived before the interval.

    Python 3.10 raises ``asyncio.TimeoutError`` here; it only became an alias
    of the built-in ``TimeoutError`` in Python 3.11.  The benchmark image keeps
    a Python 3.10 sidecar for the RC2 SDK, so catch the asyncio spelling
    explicitly instead of letting the status loop terminate the task.
    """

    try:
        await asyncio.wait_for(stop.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        return False
    return True


def _safe_error(error: BaseException) -> str:
    """Return a bounded error category without credential-bearing details."""

    message = redact_progress_text(safe_exception_text(error, max_chars=400))[:400]
    return message.replace("\n", " ").replace("\r", " ")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _workspace_fingerprint(workspace: Path) -> tuple[int, int, str]:
    """Summarize files without retaining names, contents, or model output."""

    entries: list[str] = []
    total_bytes = 0
    for path in sorted(workspace.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(workspace).as_posix()
        size = path.stat().st_size
        if size > 20 * 1024 * 1024:
            entries.append(f"{relative}:oversize")
            continue
        total_bytes += size
        entries.append(f"{relative}:{size}:{_sha256_file(path)}")
        if len(entries) >= 512:
            entries.append("<file-limit>")
            break
    digest = hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()
    return len(entries), total_bytes, digest


def _runtime_command(raw: str | None) -> tuple[str, ...]:
    if raw is None or not raw.strip():
        return _DEFAULT_RUNTIME
    command = tuple(shlex.split(raw))
    if not command or any(not item for item in command):
        raise ValueError("runtime command must contain non-empty argv entries")
    return command


def _runner_succeeded(finish: FinishEvent | None) -> bool:
    """Classify only an unambiguously complete runner terminal event.

    The official benchmark grader remains authoritative for task correctness;
    this flag only prevents bounded stops such as ``max-tokens`` from being
    mistaken for a successful run in an aggregate artifact.
    """

    return finish is not None and not finish.cancelled and finish.reason in _SUCCESS_REASONS


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--dsh-home", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--status-file", type=Path)
    parser.add_argument("--benchmark-id", default="unspecified")
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--model", default=os.environ.get("XDSH_LITELLM_MODEL", ""))
    parser.add_argument("--provider", default="litellm")
    parser.add_argument("--runtime-command", default=os.environ.get("XDSH_DSH_RUNTIME_COMMAND"))
    parser.add_argument(
        "--task-timeout",
        type=float,
        default=0.0,
        help="optional wall-clock deadline in seconds; 0 disables the deadline",
    )
    parser.add_argument("--status-interval", type=float, default=30.0)
    parser.add_argument("--reasoning-effort", default="off")
    parser.add_argument(
        "--stream-idle-timeout",
        type=float,
        default=300.0,
        help="maximum idle gap within one provider stream before DSH retries",
    )
    parser.add_argument("--max-tokens", type=int, default=None)
    parser.add_argument("--model-max-tokens", type=int, default=32_768)
    parser.add_argument("--guard-stall-timeout", type=float, default=3_600.0)
    parser.add_argument("--guard-max-model-steps", type=int, default=500)
    parser.add_argument("--guard-max-tool-calls", type=int, default=2_000)
    parser.add_argument("--guard-max-same-tool-calls", type=int, default=64)
    parser.add_argument("--guard-max-tool-errors", type=int, default=32)
    parser.add_argument("--guard-max-retries", type=int, default=12)
    parser.add_argument("--guard-max-total-tokens", type=int, default=25_000_000)
    return parser


async def _run(args: argparse.Namespace) -> dict[str, object]:
    prompt_file = args.prompt_file.expanduser().resolve()
    workspace = args.workspace.expanduser().resolve()
    dsh_home = args.dsh_home.expanduser().resolve()
    if ".xclaude" in workspace.parts or ".xclaude" in dsh_home.parts:
        raise ValueError("benchmark paths must not be inside a reserved legacy root")
    if not prompt_file.is_file():
        raise ValueError(f"prompt file is not a regular file: {prompt_file}")
    if not workspace.is_dir():
        raise ValueError(f"workspace is not a directory: {workspace}")
    if not args.model.strip():
        raise ValueError("--model or XDSH_LITELLM_MODEL is required")
    prompt = prompt_file.read_text(encoding="utf-8")
    if not prompt.strip() or len(prompt) > 200_000:
        raise ValueError("prompt must be non-empty and at most 200000 characters")
    task_timeout = _validate_task_timeout(args.task_timeout)
    if not args.benchmark_id.strip() or len(args.benchmark_id) > 200:
        raise ValueError("benchmark id must be between 1 and 200 characters")
    if not 5 <= args.status_interval <= 300:
        raise ValueError("status interval must be between 5 and 300 seconds")
    if args.max_tokens is not None and args.max_tokens <= 0:
        raise ValueError("--max-tokens must be positive when set")
    if args.model_max_tokens <= 0:
        raise ValueError("--model-max-tokens must be positive")

    rate_limit_policy = RateLimitPolicy()
    rate_limited_runner = RateLimitedRunner(
        DshRunner(
            DshRunnerConfig(
                dsh_home=dsh_home,
                model=args.model,
                provider=args.provider,
                runtime_command=_runtime_command(args.runtime_command),
                request_timeout_seconds=task_timeout,
                stream_idle_timeout_seconds=args.stream_idle_timeout,
                max_tokens=args.max_tokens,
                model_max_tokens=args.model_max_tokens,
                rate_limit_policy=rate_limit_policy,
            )
        ),
        provider=args.provider,
        policy=rate_limit_policy,
    )
    runner = GuardedRunner(
        rate_limited_runner,
        policy=RunGuardPolicy(
            max_model_steps=args.guard_max_model_steps,
            max_tool_calls=args.guard_max_tool_calls,
            max_same_tool_calls=args.guard_max_same_tool_calls,
            max_consecutive_tool_errors=args.guard_max_tool_errors,
            max_retries_without_progress=args.guard_max_retries,
            max_total_tokens=args.guard_max_total_tokens,
            stall_timeout_seconds=args.guard_stall_timeout,
            max_runtime_seconds=None,
        ),
    )
    started = time.monotonic()
    last_activity = started
    first_event_ms: int | None = None
    finish: FinishEvent | None = None
    error_codes: list[str] = []
    usage_input = 0
    usage_output = 0
    text_chars = 0
    final_text_chars = 0
    reasoning_chars = 0
    counts: Counter[str] = Counter()
    event_count = 0
    exception_category: str | None = None
    status_path = args.status_file.expanduser().resolve() if args.status_file else None
    output_path = args.output.expanduser().resolve()
    if status_path == output_path:
        raise ValueError("status file and output file must be different")
    heartbeat_stop = asyncio.Event()

    def progress(state: str) -> dict[str, object]:
        now = time.monotonic()
        return _progress_payload(
            benchmark_id=args.benchmark_id,
            session_id=args.session_id,
            model=args.model,
            provider=args.provider,
            state=state,
            elapsed_ms=round((now - started) * 1000),
            last_activity_ms=round((now - last_activity) * 1000),
            event_count=event_count,
            event_kinds=dict(counts),
            tool_calls=counts["tool_start"],
            retries=counts["retry"],
            guards=counts["guard"],
            input_tokens=usage_input,
            output_tokens=usage_output,
        )

    async def heartbeat() -> None:
        if status_path is None:
            await heartbeat_stop.wait()
            return
        while not heartbeat_stop.is_set():
            await asyncio.to_thread(_write_result, status_path, progress("running"))
            await _wait_for_status_interval(
                heartbeat_stop,
                timeout=args.status_interval,
            )

    async def consume() -> None:
        nonlocal first_event_ms, finish, usage_input, usage_output
        nonlocal text_chars, final_text_chars, reasoning_chars, event_count, last_activity
        stream = runner.run(
            prompt,
            session_id=args.session_id,
            cwd=workspace,
            reasoning_effort=args.reasoning_effort,
        )
        async for event in stream:
            last_activity = time.monotonic()
            event_count += 1
            counts[event.kind.value] += 1
            if first_event_ms is None and not isinstance(event, RuntimeEvent):
                first_event_ms = round((time.monotonic() - started) * 1000)
            if isinstance(event, UsageEvent):
                usage_input += event.input_tokens
                usage_output += event.output_tokens
            elif isinstance(event, TextEvent):
                text_chars += len(event.text)
            elif isinstance(event, ReasoningEvent):
                reasoning_chars += len(event.text)
            elif isinstance(event, ErrorEvent):
                error_codes.append(safe_error_code(event.code))
            elif isinstance(event, FinishEvent):
                finish = event
                # Some DSH releases may deliver the completed response on the terminal
                # event rather than as text deltas.  Keep only its length;
                # the benchmark artifact must never contain model output.
                final_text_chars = len(event.final_text)

    async def supervise() -> None:
        consume_task = asyncio.create_task(consume(), name="benchmark-consume")
        heartbeat_task = asyncio.create_task(heartbeat(), name="benchmark-heartbeat")
        try:
            done, _ = await asyncio.wait(
                (consume_task, heartbeat_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat_task in done and not consume_task.done():
                consume_task.cancel()
                await asyncio.gather(consume_task, return_exceptions=True)
                heartbeat_task.result()
                raise RuntimeError("benchmark heartbeat stopped unexpectedly")
            heartbeat_stop.set()
            await heartbeat_task
            await consume_task
        finally:
            heartbeat_stop.set()
            for task in (consume_task, heartbeat_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(consume_task, heartbeat_task, return_exceptions=True)

    try:
        if task_timeout is None:
            await supervise()
        else:
            await asyncio.wait_for(supervise(), timeout=task_timeout)
    except BaseException as error:
        exception_category = type(error).__name__
        error_codes.append(safe_error_code(exception_category))
    finally:
        heartbeat_stop.set()
        await runner.close()

    file_count, output_bytes, workspace_sha256 = _workspace_fingerprint(workspace)
    elapsed_ms = round((time.monotonic() - started) * 1000)
    cancelled = bool(finish and finish.cancelled)
    reason = safe_error_code(finish.reason if finish is not None else "exception")
    # A terminal event is not synonymous with a successful task: max-tokens,
    # guarded, and other bounded-stop reasons require the official grader.
    succeeded = _runner_succeeded(finish)
    result: dict[str, object] = {
        "schema_version": 1,
        "benchmark_result": "aggregate_only",
        "benchmark_id": args.benchmark_id,
        "model": args.model,
        "provider": args.provider,
        "session_id": args.session_id,
        "succeeded": succeeded,
        "finish_reason": reason,
        "cancelled": cancelled,
        "elapsed_ms": elapsed_ms,
        "first_event_ms": first_event_ms,
        "event_count": event_count,
        "event_kinds": dict(sorted(counts.items())),
        "tool_calls": counts["tool_start"],
        "retries": counts["retry"],
        "guards": counts["guard"],
        "errors": sorted(set(error_codes)),
        "input_tokens": usage_input,
        "output_tokens": usage_output,
        "text_chars": text_chars,
        "final_text_chars": final_text_chars,
        "reasoning_chars": reasoning_chars,
        "workspace_file_count": file_count,
        "workspace_bytes": output_bytes,
        "workspace_sha256": workspace_sha256,
    }
    if exception_category is not None:
        result["exception"] = exception_category
    if status_path is not None:
        final_state = "cancelled" if cancelled else "succeeded" if succeeded else "failed"
        _write_result(status_path, progress(final_state))
    return result


def _write_result(path: Path, result: dict[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    payload = json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n"
    temporary.write_text(payload, encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    try:
        result = asyncio.run(_run(args))
        _write_result(args.output.expanduser().resolve(), result)
    except (OSError, ValueError) as error:
        raise SystemExit(_safe_error(error)) from None
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
