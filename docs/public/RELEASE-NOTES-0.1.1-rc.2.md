# XDSH `0.1.1-rc.2` release notes

This is the aligned RC2 release of XDSH for the fixed DeepSeek Harness
`0.1.1-rc.2` runtime.

## Version identity

| Surface | Value |
| --- | --- |
| GitHub release/tag | `xdsh-0.1.1-rc.2` |
| Python distribution | `xdsh==0.1.1rc2` |
| DSH runtime | `0.1.1-rc.2` only |
| Python SDK | `deepseek-harness-sdk==0.1.1rc1` |
| License | Apache-2.0 |

Python package metadata uses the PEP 440 spelling `0.1.1rc2`; the hyphenated
`0.1.1-rc.2` is the equivalent npm/DSH spelling. The product and runtime
therefore share the same major, minor, patch, and release-candidate line.
The protocol client remains independently versioned because it is published
as a separate Python distribution.

From the extracted release source directory, install XDSH:

```bash
python -m pip install '.[channels]'
```

The DSH runtime is installed separately under the XDSH-owned `~/.xdsh` prefix;
no runtime or credential is embedded in the Python artifacts.

## Included

- Python 3.10 cancellation and timeout compatibility: simultaneous cancellation
  and rate-gate admission cannot silently start cancelled work or leak a slot;
  provider timeout types map to HTTP 504 consistently; Telegram delivery-ack
  cancellation has bounded cleanup. Later Python versions retain the same
  runner-neutral contract.
- Updated optional gateway/channel dependencies with security floors in wheel
  metadata as well as the lockfile. The tested gateway uses LiteLLM 1.88.6;
  DSH and its protocol SDK remain pinned to the fixed RC2 pairing. Native
  Messages streaming keeps its required official proxy extra, without starting
  the upstream proxy application. See [security boundaries](SECURITY.md).
- Research baseline consistency: managed Telegram/CLI tasks persist one private
  host snapshot outside the workspace and supply its `--baseline` command to
  the agent. Source references validate before a draft freezes; the verification
  CLI, delivery repairs and same-binding restarts use the original rules/hashes.
  Missing or corrupt bound state fails closed. Standalone checks are explicitly
  labeled, and no check is claimed to prove pre-experiment registration or
  scientific validity. Existing historical tasks are not retroactively certified.
- Bounded same-task research delivery repair: after confirmed native completion,
  correct failed report/checklist issues in the same session before giving up;
  keep input hashes, criteria, time and usage, stop unchanged or unsafe attempts.
  Telegram shows localized repair progress. Native stream cleanup now propagates
  through fallback/rate-limit wrappers before continuation. A fallback model may
  not replay the original task after tool dispatch. This is not automatic recovery
  of unconfirmed side effects or a promise of zero failures.
- Optional `threshold_rates` checks recompute positive counts and derived rates
  from actual observed/simulated score files, rejecting substituted target rates
  even when a report is internally consistent. `xdsh-verify --score-example`
  supplies the schema; checks do not certify the scientific method.

- Scoped research-delivery acceptance: recognized Chinese/English execution
  requests require a bounded checklist, captured at its first valid declaration.
  Selected Python defaults can be checked before calling; numeric nulls, array
  degeneracy and ranges have explicit checks. Failed checks preserve partial
  output and continuation diagnostics. Model-owned criteria/receipts do not
  establish scientific truth or full requirement coverage. See
  [artifact acceptance](ACCEPTANCE.md).
- Research preflight pins the host workspace, checklist and research profile.
  CLI and host share concrete-declaration validation with actionable safe
  diagnostics; a preflight pass is not a completed-result pass. Optional frozen
  metric labels detect coverage shrinkage; distinct calls need distinct receipts.
- RC2 recovery retries an uncommitted model step, not the whole user task or
  previously completed tools, with separate bounded transient-error and 429
  budgets. Gateway prefix retries default to off to avoid multiplied retries;
  when explicitly enabled, the250ms/8KiB/64-character prefix gate stays bounded. See
  [early stream disconnects](PROVIDERS.md#early-stream-disconnects).
- Configurable provider/DSH silent-stream windows, specific no-replay recovery
  advice, multi-bullet acceptance guidance, and model-reported plan labels.
  See [long-task operation and acceptance](LONG_TASKS.md); this is not a claim
  of fully automated scientific validation.
- Runner-neutral CLI, Telegram, named sessions, queue/insert workflows,
  structured progress, cancellation, context protection, Skills, jobs, and
  safety guards.
- Retrieval guidance consults relevant official sources before other sources,
  with bounded concurrency within each source stage. Saved Skill links remain
  discovery leads; source ownership, freshness and conflicts must be checked.
  This is model-facing guidance, not a guarantee of model compliance.
- Long-running tasks have no default wall-clock ceiling while structured
  activity continues. Exact RC2 `tool-call-delta` chunks refresh liveness
  without exposing or persisting tool arguments; truly silent streams still
  warn and stop at the configured stall deadline. Retry, repeated-tool, step,
  token, and concurrency budgets remain active.
- Slow-but-healthy RC2 startup gives only runtime construction and eager
  JSON-RPC initialization a 90-second deadline with one clean retry. Model
  time-to-first-event is deliberately separate, so a healthy runtime never
  replays a slow provider request under a misleading cold-start error.
- Interactive context is protected before latency runs away: warn at 20%,
  compact at 50% or a 64K input-token ceiling (whichever comes first), retain
  8% verbatim, and target 20% after a defensive generation reset. This lower
  absolute ceiling compensates for rc.2's documented CJK/JSON underestimation.
  A static capability flag is not treated as proof of completion; XDSH must
  observe the structured native compaction lifecycle or use its bounded outer
  fallback at the next safe boundary.
- Interactive provider stream idle defaults to 120 seconds; the DSH silent-stream
  window leaves additional time to receive a typed gateway error. RC2 permits
  bounded TIMEOUT/TRANSPORT/SERVER/EMPTY_RESPONSE model-step recovery, while
  AUTH/INVALID_REQUEST are not blindly retried. Independent liveness protection
  still preserves partial output when a run cannot recover.
- Cold-session outer hydration is bounded to 4,000 characters. Telegram uses
  a single asynchronous, coalescing progress publisher: slow updates cannot
  backpressure model events or completed answers. Edit spacing starts after
  request completion, respects typed `Retry-After`, and never replays a queue
  of stale progress snapshots. Pending progress cannot block cancellation or
  error delivery; both retain the partial answer.
- Telegram queue choices are persisted to suppress confirmations after callback
  replay or restart. Merge checkpoints and stops A, then immediately continues
  A+B in the same session; interrupt runs B before resuming A. This is bounded
  stop-and-resume, not native RC2 steering. Unconfirmed stops stay held for
  inspection; completed tool side effects cannot be undone.
- The Messages gateway retains its SDK client owner through the complete
  stream. OpenAI/Azure routes no longer construct a redundant HTTP handler
  whose finalizer could close their active connection.
- Every changed structured Todo snapshot bypasses generic stream throttling,
  so percentage, current step, and next step update before task completion;
  repeated identical snapshots remain throttled to avoid edit storms.
  After 60 seconds without a new confirmation, unfinished plans show a
  localized pending-sync state and last-reported count, not an obsolete live
  percentage, current/next step or ETA. Fresh confirmations restore the bar;
  model/tool activity alone is never treated as completed work.
- Completed answers are delivered before optional thinking output. After a
  Telegram transport ambiguity, the original payload remains manual-only, but
  the next successful poll immediately flushes its never-attempted recovery
  notice; recovery-notice failures cannot create an unbounded notice chain.
- Checkpoints optionally record an opaque XDSH host-process marker and internal
  session generation. Normalized runner events carry the same host-owned marker
  plus a launch nonce, and the adapter rejects missing or retired notification
  sources before sequence processing. Within one normalized run, the
  adapter accepts only strictly increasing event sequence numbers, dropping
  replayed and same-sequence conflicting notifications with constant memory.
  This makes rollback provenance and local event fencing auditable while
  preserving compatibility with older checkpoint hashes; it does not claim
  native DSH cross-process resume, event replay, or per-session cancellation.
- `/language` persists a per-chat selection and refreshes that chat's native
  37-command menu. Thirteen major languages have native setup, first-use help,
  command descriptions, and answer-language guidance; existing installs keep
  Traditional Chinese unless configured, while new non-interactive installs
  default to English.
- External `DshRunner` over the official SDK/JSON-RPC seam.
- LiteLLM Anthropic Messages-compatible loopback routing with
  an operator-selected model (no vendor/model default for new users), plus credential-free
  provider/model metadata with explicit supported/unsupported/unknown
  capabilities and strict duplicate/non-finite/credential-shape rejection.
- Coherent DSH plugin inventory and approval-gated official CLI management;
  optional direct gateways require the `xdsh-plugin-invoke-v1` handshake and
  exact package capability, while RC2 agent-tool routes remain clearly labeled.
- Bounded redacted session search with strict/partial/typo-tolerant local
  ranking, an opt-in authenticated loopback embedding adapter for hybrid
  semantic retrieval, and content-free `/memory index` capacity diagnostics.
- Exact RC2 WSL installer, runtime manifest, doctor/version-alignment checks,
  Apache-2.0 `LICENSE`/`NOTICE`, and English/Simplified Chinese/Traditional
  Chinese documentation.
- A clean public-source contributor surface: locked dependencies, an
  auditable credential-free core contract profile, independent CI,
  contribution/support guidance, and a code of conduct.
- Optional QQ Official Bot support through Tencent's pinned
  `qqbot-agent-sdk==1.2.2`: official outbound WebSocket Gateway, OAuth,
  heartbeat/reconnect/Resume, C2C and group text, mandatory user OpenID
  authorization, optional dual user/group authorization, bounded concurrency,
  replay suppression, 4,000-character chunking, and mode-0600 non-secret
  Resume state. It neither logs into a personal QQ account nor requires a
  public callback listener.

## Skill lifecycle boundary

The package-owned Skill bundle is immutable from the product layer. Project
Skill evolution and archival are available as explicit, approval-only
operations. A candidate can carry at most 16 UTF-8 text support files, limited
to 64 KiB per file and 256 KiB in total. Safe relative paths, secret and
instruction-injection scans, before/proposed/applied tree hashes, isolated
evaluation, stale detection, and mode-0600 atomic state are enforced. Omitted
files never imply deletion; symlinked, hidden, binary, malformed, or oversized
resources fail closed. Approved changes and archive moves have a verifiable
hash-checked rollback path. Curator output is advisory and never auto-applies
or consolidates Skills.

The controls are exposed consistently by the CLI and Telegram (`/skills
pending|diff|evaluate|approve|reject|rollback`, `/skills curate`, and
`/skills archive ...`). RC2 still has no direct native Host method for
arbitrary plugin invocation; an authenticated external gateway is a separate,
versioned capability and this release does not claim a native method.

## Compatibility boundary

The package release profile, runtime manifest, installer, image label, and
doctor probe all enforce the aligned DSH `0.1.1-rc.2` line. A different DSH
line requires a separately versioned XDSH release and compatibility matrix.
The historical `0.1.0a0-rc2` artifacts remain immutable for rollback evidence
but are not the active install target.

## Publication rule

The aligned candidate must pass the package, hermetic, fresh-install, and
isolated RC2 smoke gates before registry upload. Publishing is an explicit
operator action; this repository does not upload artifacts automatically.

## Maintainer verification record

Rechecked on **2026-10-09**. This supersedes the incomplete September install
record; older archives remain immutable and are not the publication input.

| Evidence | Result / scope |
| --- | --- |
| Full maintainer suite | 2,452 passed, zero skips on Apple Silicon / Python 3.12.12 with all extras; 81.78 seconds |
| Public core profile | 47 test files; 1,019 passed on Apple Silicon / Python 3.12.12 |
| Linux core profile | Python 3.10.19, 3.11.14, 3.12.15 and 3.13.12: each 1,018 passed / one Darwin-only RSS-monitor skip; all extras installed |
| Static/package gates | Ruff, strict mypy, compileall, shell syntax, lock consistency, bounded 429 probe, clean export and archive checks passed |
| Independent dependency scan | 114 third-party packages in the locked Mac all-extra/development environment: no known advisories found; unpublished XDSH is not in the advisory database and is reviewed through source/tests |
| Real pinned runtime | Fresh official DSH 0.1.1-rc.2 installation; installed candidate wheel and isolated LiteLLM 1.88.6 gateway; single task, multi-turn, structured tool and cancellation all passed |

The four real smoke cases took 1.828s, 4.081s, 5.441s and 2.752s respectively
on the operator's `ark-code-latest` Coding Plan route. These are four small
correctness checks, not a latency benchmark, proof of long-task reliability,
or evidence for every provider. The existing gateway and Bots were not changed.

The frozen release's `RELEASE.json` and checksums identify the exact source,
wheel and sdist and their fresh-install gates. Archive installation checks use
new virtual environments and full dependency resolution, not `--no-deps` or an
inherited dependency path. Their doctor version stub is not a real runtime;
the independently installed DSH smoke above covers that separate boundary.

Eight optional LiteLLM logging-coroutine cleanup warnings remain in the
maintainer test run; they do not change its passing result and are not hidden.
The security scan is dated, not a guarantee against unknown vulnerabilities.
The Linux runs use x86_64 containers on an ARM64 local engine (emulated), not
a native x86_64 host. Native Windows, an actual Ubuntu WSL installation,
Linux ARM64 and Intel macOS are not newly certified by this run. In particular,
cryptography 49+ no longer supports Intel macOS; do not downgrade the security
floor to claim a supported gateway/channels installation there.

Telegram/QQ and other channel contract tests are credential-free; this release
validation does not log into a real messaging application or send user messages.
Real channel round trips and host supervision remain deployment-specific gates.
No existing Bot deployment or public upload is implied by package acceptance.
