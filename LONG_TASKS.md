# Long tasks: liveness, checkpoints and delivery acceptance

Active execution has no default wall-clock ceiling. Resource budgets remain
configurable with `/limits`; disabling them does **not** disable sandboxing,
repeated-failure protection or silent-stream detection.

## Separate timeouts

| Layer | Setting | Default | Meaning |
| --- | --- | --- | --- |
| XDSH model gateway | `XDSH_PROVIDER_STREAM_IDLE_SECONDS` | 120 s | HTTP/SDK event wait, not cumulative task duration |
| DSH runner | `XDSH_DSH_STREAM_IDLE_TIMEOUT_SECONDS` | 120 s | No structured model event received |
| Outer guard | `XDSH_GUARD_STALL_SECONDS` | 600 s | No normalized activity (active tools have a separate allowance) |

The first two settings accept finite values from 30 to 3600 seconds. For a
provider with observed transient gaps, an operator can try gateway **300 s**
and DSH **330 s**, retaining the outer guard. Set the gateway variable in the
gateway service environment and the DSH variable in the Bot environment;
restart only when no task is active. On direct gateways owned by someone else,
the XDSH gateway setting has no effect.

This is a tolerance tradeoff, not a cure for a stalled provider: a truly dead
request takes longer to detect. Do not increase timeouts merely to hide an
oversized context, broken route, or repeated provider failure. Each streaming
wait is independent; a healthy multi-hour stream is not limited to 300 seconds.
The gateway records only event timing/count metadata, never prompts or keys.

Repeated background-output polls do not prove that a task is dead. Identical
poll results trigger stopping only when the configured repeat count is reached
**and** there has been no changed poll output or other successful substantive
tool/artifact progress for `XDSH_GUARD_ACTIVE_TOOL_STALL_SECONDS` (default1800s).
Text, token activity and Todo updates alone do not reset this grace period.
This is output-based fault detection, not a proof that a process has crashed;
increase the active-tool grace explicitly for legitimately quiet computations.
Real tool errors and total silence retain their separate guards.

The official RC2 model loop first attempts bounded **current-step recovery**
for transient timeouts/disconnects. Telegram/CLI allow two transient retries
per model request by default, independent of task length; generated tools from
a failed model attempt have not executed. See [provider recovery](PROVIDERS.md#transient-model-step-failures).
Gateway prefix retries default off to avoid nested retry multiplication.
For an interactive provider, gateway120s/DSH150s can detect dead streams sooner;
do not shorten the wait for a genuinely slow provider without measurement.

After recovery is exhausted, `MODEL_STREAM_IDLE` distinguishes recognized
model-stream timeouts from generic execution timeouts (an outer retry guard
may instead report a protective stop). Neither replays the whole task. Existing
partial output and session state remain available. Send **continue** in the
same session to resume the interrupted goal; inspect existing outputs and job
state first. This is goal/checkpoint continuation, not restoration of an exact
native process instruction. Do not start a second copy of a job that still runs.

## Acceptance is not a progress bar

Numbered requirements and multi-bullet tasks receive bounded acceptance
guidance. Reproduction tasks additionally require explicit separation of exact
results, substitutes, sensitivity analysis, static review and unexecuted work.
Blocked requirements remain in the denominator. Completed scripts or a report
do not prove that all requested analyses ran.

The UI labels a fully checked plan as model-reported progress, not verified
delivery. A conservative text diagnostic can flag an explicit all-done claim
alongside unresolved items; it does not rewrite answers or change execution
ledger outcomes. It is not a semantic or scientific verifier and can miss
subtle conflicts. Explicit file contracts and independent result checks remain
the stronger acceptance path.

For long research jobs, persist milestone commands, inputs, versions,
parameters, result paths, remaining criteria and the next safe step. Ask before
material scope/method changes **unless the user already delegated that choice**;
still disclose deviations. Parameter sweeps that happen to match a paper do
not establish misconduct. Match subsets, reference groups and definitions
before describing reported numbers as contradictory.

## Native background follow-up reports

The pinned pair uses Python SDK `0.1.1rc1` with Node runtime `0.1.1-rc.2`.
The SDK's foreground `run` returns on the first idle notification; that is not
proof that every background computation has finished. When the SDK exposes its
public session subscription, XDSH Telegram keeps a separate read-only listener
for later official `tool-jobs` follow-up turns. It adds no prompt or tool retry.

A completed follow-up report is redacted, persisted, and delivered as an
additional message, not substituted for the original answer. Original task
outcomes and acceptance baselines remain unchanged. Native follow-up error
codes describe that model turn, not an independently verified process exit.
Titles follow the task's language. Sequence/generation deduplication and the
normal outbox rules apply: an ambiguous Telegram receipt is not auto-retried.

When only one product turn can own the work, the report is appended to its
ordered outbox. After multiple product turns share a runtime, RC2 cannot prove
which earlier task owns a Job; the report is explicitly session-scoped with an
unconfirmed parent, never silently assigned to the newest task.

`/cancel` can also close the current session runtime/listener after foreground
execution ends. Saved reports remain. Normal shutdown attempts to persist
already-received complete reports for later delivery, but does not wait for
unfinished computations. It cannot guarantee termination of external detached
processes or recovery of notifications lost before persistence.

This is **not full native Job supervision**. RC2 does not expose a structured
job registry/exit-code query through this SDK. Quiet notices, exhausted native
wakeup budgets, or notices with no completed follow-up turn are not covered.
Autonomous native wakeups are not thereby brought under the foreground guard's
entire accounting/lifecycle. Required experiment completion still needs explicit
artifact and independent result checks. A future SDK without the optional
subscription keeps the ordinary foreground path, not an invented replacement
for background observation.

## Telegram startup while offline

For typed connection failures, HTTP 429 and HTTP 5xx, startup now waits with
bounded backoff instead of exiting after five attempts. Stop requests remain
effective. Authentication/configuration HTTP 4xx errors (except 429) fail
immediately; unclassified failures retain a bounded attempt count.

While waiting, the Bot is **not ready**: no polling success is advertised and
no scheduled job or saved task starts. This is not OS login startup, crash
supervision, or a network repair. Configure a working route; an optional
credential-free local HTTP proxy can be set with `XDSH_TELEGRAM_PROXY_URL`.
XDSH does not silently inherit shell proxy settings or change the system VPN.
