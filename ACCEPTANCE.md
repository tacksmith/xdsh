# Artifact acceptance (RC2)

Model completion and a successful tool call do not prove that a deliverable is
correct. For machine-checkable file tasks, XDSH can independently check an
declared contract. Ordinary tasks use opt-in explicit contracts; recognized
research-execution requests also have the scoped automatic entry below. Checks
are provider-neutral: the checker does not call a model, execute a validation
script, install plugins, or repair files. A separate bounded research-delivery
repair loop can ask the same runner/session to correct a failed delivery.

## Automatic research entry

Managed Telegram and CLI sessions recognize conservative Chinese/English
requests to **execute** a paper reproduction. XDSH generates a unique
`.xdsh-research-<id>.json` checklist path and a first-line deferred contract.
The agent is asked to write that checklist and method plan in an initial
separate tool call. The host captures the **first valid declaration observed
after a tool result**, freezes its criteria and source hashes for this task,
and checks declared outputs before accepting completion. The checker does not
scan the workspace. The recovery loop below may add a limited repair segment.

The checklist uses the ordinary schema below and must include method checks
(`calls` or `source_json`) plus numerical output checks (arrays, numbers,
ratios, maximum differences or score-derived rates). Missing, empty, recursively deferred or
unsafe checklists cannot pass. Source JSON pointers must resolve against the
captured source bytes; a `calls` reference must select a valid parameter object.
A draft with a missing/invalid source reference stays a draft, so correcting its
wiring does not accidentally change an already-frozen baseline. This does not
require future output artifacts to exist. After capture, altering the checklist or its
declared sources fails. If the first valid checklist only appears at finish,
its baseline is captured then; this is **not proof of an experiment-start
state**. Criteria and scientific targets chosen by a model are not independent
ground truth or proof of complete requirement coverage.

### Source-aware research policy (new tasks)

New automatic research tasks require **`research_revision: 2`**. Their concrete
checklist must retain this field and the `feasibility` declaration from
`xdsh-verify --research-example`. The generated command includes
`--research-revision 2`; a managed `--baseline` also pins this minimum policy.
Omitting the flag cannot downgrade the bound policy. CLI preflight is read-only,
and restarting the same binding preserves its revision, sources and comparisons.

Existing explicit contracts and revision-1 baselines keep their original
behavior, including numeric bounds that fail. They are **not** retrospectively
converted to advisory checks. Only a new task can adopt new criteria; it cannot
rewrite the result of an earlier run.

Revision 2 keeps `numbers: {"/value": {}}` for finite measurements. Scalar numeric
`equals` and nonempty numeric bounds must instead be explicitly classified under
a JSON rule's `expectations` map:

| Declared `kind` | Comparison mismatch | Required provenance |
| --- | --- | --- |
| `integrity` | Fails delivery | Frozen JSON record describing the structural invariant |
| `user` | Fails delivery | Frozen JSON record locating the actual user requirement |
| `literature` | Retained advisory difference | Frozen JSON record with citation and paper/code location |
| `exploratory` | Retained advisory difference | No reference required; it is explicitly a model hypothesis |

Every expectation requires either numeric `equals` or nonempty inclusive
`bounds` (`minimum`/`maximum`). It still requires a real, finite measurement:
missing files, absent fields, null, booleans and invalid numbers fail even for
an advisory comparison. Method mismatch, invalid source references and changed
frozen files also remain failures. Advisory differences never trigger a delivery
repair segment merely to make a scientific value match a guessed target.

Example JSON rule fragment (adapt it; these are synthetic targets):

```json
{
  "path": "results.json",
  "numbers": {"/estimate": {}},
  "expectations": {
    "/estimate": {
      "kind": "literature",
      "bounds": {"maximum": 0.1},
      "reference": {"path": "method-plan.json", "pointer": "/comparison"}
    }
  }
}
```

The referenced file must be listed in `unchanged`. Its `/comparison` object
must have the **same** `kind` and `bounds`/`equals`, a nonempty `citation` and
`locator`, for example a publication identifier and a methods/table location.
For `user`, record the actual request and its location; do not relabel a model
guess as a user instruction. Both preflight and final evaluation validate this
reference. This is **declared provenance**, not citation authentication, an
approval service, or verification that the paper actually contains the number.
The checker does not fetch papers. A fabricated but consistent record can still
mislead it; inspect scientific sources independently.

`--details` reports classified comparisons and measured scalar values, while
`advisory_differences` is included even without details. Invalid/missing values
are failures, not advisory differences. Reference bodies and citations are not
echoed. The final Telegram progress card shows localized, content-free research
notes; these are separate from the answer, so a JSON-only answer remains JSON.
Delivery success never certifies full scientific reproduction.

Other declared checks remain structural: array shapes/ranges, ratio arithmetic,
source-parameter equality and structured JSON equality. Do not hide scientific
thresholds in these rules to bypass classification. The host cannot infer the
scientific meaning of arbitrary data or authenticate the author's chosen `kind`.

### Feasibility before costly work

Revision 2 requires a bounded `feasibility` object:

```json
{
  "scope": "subset",
  "unit": "samples",
  "requested_units": 1000,
  "planned_units": 100,
  "estimated_peak_bytes": 8000000000,
  "available_bytes": 4000000000,
  "strategy": "staged",
  "estimate_basis": "Document the metadata calculation or permitted pilot here"
}
```

These are illustrative numbers, not defaults. Estimate dataset size and peak
memory using known metadata or a small permitted pilot. Use JSON `null` for
unknown counts/bytes, `scope: unknown` when coverage is unconfirmed, and
`strategy: undecided` when no approach has been selected. Other strategies are
`in_memory`, `streaming` and `staged`. The editable example deliberately leaves
estimates unknown rather than inventing measurements.
Counts, when known, are positive integers; byte estimates are nonnegative integers.
An available-byte estimate of zero means zero available bytes, **not unlimited**;
it remains a resource-risk advisory, not a new runtime stopping condition.

`full` requires equal, known requested/planned counts; `subset` requires fewer
planned units. This detects inconsistent **declarations**, not actual processed
row counts or all paper panels. Actual output-to-input identity/coverage needs
separate evidence checks. Choosing a subset is not permission to reduce a user's
required scope; missing mandatory outputs still fail. Explain remaining scope.

Preflight reports unknown coverage, partial scope, incomplete resource estimates,
estimated memory risk and undecided strategy as **advisories**, not runtime stops.
It never changes Token/time/memory quotas, starts a pilot, installs resources or
queries the machine. Estimates are frozen with the checklist, not live resource
telemetry; output says `declared_estimates_not_live_measurement`. Prefer streaming
or staged execution where appropriate without silently expanding authority.

`xdsh-verify --research-example` provides a minimal schema with `source_json`
effective-argument metadata and numeric fields; it does not require call receipts,
arrays or NumPy. This is declaration consistency, not direct call attestation.
The fuller `--example` also shows `calls` and numeric `.npy` checks; use
`--method-example` to generate real selected-argument receipts for critical Python
library calls. Plain parameter JSON is not a `calls` receipt. JSON lists can use
`equals`, not an array `json_pointer`.
Rename example fields/paths and omit unrelated rules;
do not add experiments merely to fill an example. `--method-example` shows the
Python helper's calling interface without inspecting framework source.

The generated instructions pin the **absolute host workspace**, unique contract
file, research profile and **host-owned `--baseline` path**, including shell
quoting for spaces. After creating
the concrete checklist and its sources, run the exact generated command with
`--profile research --research-revision 2 --baseline <host-path> --preflight` for
new tasks (use the exact command, not a revision upgrade on an old binding). This checks schema,
source availability and source references
using the **same parser as the host**, before expensive work. Its successful
response is `declaration_valid: true, checks_passed: null`: outputs have **not**
been checked. At delivery run that command with `--details` instead of
`--preflight`. Both responses identify the workspace, contract and profile.
Use the generated command verbatim, rather than guessing a state path or omitting
`--baseline`; the same command is supplied to automatic delivery-repair segments.
Do not generate fake data just to make preflight pass.

### One authoritative baseline for the host and CLI

Managed Telegram and CLI runners register deferred research contracts under
`<instance-state-root>/acceptance`, **outside the runner workspace**. The binding
contains the session, canonical workspace identity and exact checklist path.
The host freezes it once; a separate `xdsh-verify` process reads the same saved
rules and original hashes. Restarting a host with the **same task binding** or
repairing its delivery does not recapture changed method files. A fresh user
task/continuation normally has a new generated checklist and therefore a new
binding; this does not repair or rewrite the earlier task's audit outcome.

| State/result | Meaning |
| --- | --- |
| `baseline_scope: host_draft` | Preflight may validate the draft; `checks_passed: null`. The CLI cannot freeze it or claim final success. |
| `baseline_scope: host_frozen` | CLI checks the persisted host snapshot; `baseline_id` identifies that snapshot. |
| `capture_point: tool_boundary` / `completion` | First valid capture happened after a tool result / at completion; neither proves pre-experiment registration. |
| `host_baseline_not_frozen` | Final check attempted before the host captured a valid draft; use preflight in a separate tool call and then the final command. |
| `host_baseline_unavailable` | The bound state is missing, damaged or unsafe; do not retry without `--baseline` or create a replacement. |

Preflight on a frozen binding checks the original checklist/source hashes, not
the presence of outputs still being computed. Final checks evaluate output
artifacts against the same criteria. Output files can of course change between
two checks; inspect `--details` receipts rather than expecting different-time
checks to be identical regardless of file changes.

The store uses owner-only directories/files, bounded JSON, checksums, a separate
registration claim, serialized writes and atomic record replacement. Losing
either a claim or its corresponding snapshot fails closed. The CLI is read-only;
it does not create a missing store or repair corrupted state. The deployment
sandbox must keep this directory **readable but not writable** by agent tools;
do not fix access errors by granting agent state-write permissions. These hashes
are not signatures and do not protect against an actor with host-user write
access. Deleting the entire host registry destroys its history; keep it with
the instance's protected state/backup, not in workspace-cleanup targets.

Custom integrations can opt into persistence with
`GuardedRunner(..., acceptance_root=private_state / "acceptance")`; leaving the
option unset retains the legacy in-memory behavior. Explicit non-deferred
contracts are unchanged. Existing historical runs without saved bindings cannot
be given a trustworthy original snapshot retroactively. No model or network
call is added by this storage/checking path.

Changing the shell directory does not change the host workspace. Keep the
checklist at the declared root path and prefix output paths with the analysis
subdirectory. The file must contain concrete checks, **not** the first-line
`XDSH-VERIFY` header or another `deferred` wrapper. A check of another manifest
does not satisfy this task. Preflight errors return an allowlisted reason and
remedy for missing/inaccessible, malformed, recursive or insufficient checklists
and missing/invalid source references; evidence bodies are never echoed. These checks do not
discover alternate files, widen sandbox access, reset host-frozen hashes or
certify whether the method plan accurately reflects the paper.

Discussion, explanation, explicit no-execution requests and plan mode do not
enable this entry. A user-supplied first-line contract takes precedence. The
intent matcher is deliberately conservative, not a universal multilingual
research classifier; unmatched requests can use an explicit contract. Failed
checks retain partial answers and files, show localized failure categories,
and persist bounded diagnostics for an explicit continuation. No automatic
whole-experiment replay occurs. A new task binding is not a durable attestation
of an earlier run; only resuming the same saved binding restores its baseline.

### Automatic delivery repair, not experiment replay

After a **confirmed successful native segment** with actual tool/artifact events,
an automatic research contract can trigger up to **two** same-session repair
segments before returning a terminal mismatch. Their prompt asks the agent to
inspect existing evidence and fix only failed report calculations or checklist
wiring. It does not resubmit the original experiment. Model settings, frozen
criteria/input hashes, task clock, usage and guards are retained. Repeated
unchanged failures stop early. An explicit non-deferred contract keeps its
existing one-pass behavior; integrators may set `research_repair_attempts=0`
on `GuardedRunner` to disable this recovery.

A changed/unreadable frozen input, unavailable host baseline, cancellation, permanent rejection, or
unconfirmed native execution does **not** authorize automatic delivery repair.
Nor does a repair authorize installing dependencies, new experiments or external
side effects. These behavioral boundaries are conveyed to the model, not a new
read-only sandbox: the existing tool permissions still apply. A successful check
is still only a check of declared artifacts. Failed/exhausted repairs retain the
partial report and files instead of producing a false success. Cleanup propagates
through wrappers before continuation, so a native session lock or rate-limit lease
cannot be intentionally left for garbage collection to release.

After the repair notice is yielded, the guard rechecks the saved baseline identity
and original source hashes **before dispatching the next segment**. A source change,
missing authority or cancellation at that handoff prevents the new dispatch. This
is a second check at an asynchronous boundary, not a filesystem transaction or a
guarantee against changes after dispatch. It does not make repair tools read-only.

The guard retains observed answer deltas/snapshots for a terminal fallback if a
stream ends without a final answer. A provided final answer still takes precedence.
Marked events from an independently known old runtime or a regressed sequence are
not used as answer evidence, errors, activity or completion. Unmarked legacy events
remain compatible; an initial marker without an independent current marker is not
authenticated. Rejected-frame traffic does not disable the existing silence clock.
A normalized turn-level error cannot later be relabelled as successful completion.

The baseline is durable, but repair counters and failure signatures are scoped to
the active `GuardedRunner.run` invocation. They are **not** a durable crash-recovery
journal; reconstructing a verifier is not resuming an interrupted executing task.
Unconfirmed execution still requires inspection/explicit continuation, not replay.

Telegram shows a localized repair notice in the **existing** progress card and
keeps one task timer and one terminal event. The checker never creates additional
Telegram tasks. Neither this loop nor model-step recovery guarantees completion
during a provider outage, loss of power, missing authorization or invalid science.

Native retries of an uncommitted model step remain the safe path for temporary
429/timeouts. The fallback-model wrapper does not replay an entire failed task
once a tool, artifact or subagent has been dispatched, since side effects may
already have occurred. Earlier valid output remains available for informed
continuation rather than an automatic duplicate execution.

## Explicit contract

Put `XDSH-VERIFY: ` followed by one JSON object on the **first line** of the
task. The following lines contain your ordinary instructions. For example:

```text
XDSH-VERIFY: {"unchanged":["source.json"],"json":[{"path":"report.json","equals":{"/duplicate_ids":["A"]},"source_sha256":{"/source_before_sha256":"source.json","/source_after_sha256":"source.json"}}]}
Fix the JSON syntax in a separate output file, without modifying source.json.
Write report.json with duplicate IDs and the original source file's before/after hashes.
```

Use your own paths and expected values, not the example values blindly.

- `unchanged`: existing relative files whose SHA-256 is captured before this
  invocation and rechecked before accepting successful completion.
- `json`: JSON deliverables; a rule with just `path` requires valid, unambiguous
  JSON. `equals` maps JSON pointers to exact expected values. Booleans and
  numbers are distinct; arrays are ordered.
- `source_sha256`: maps JSON pointers to a source declared in `unchanged`.
  Both before/after report fields must equal that source's captured hash, not
  the hash of a repaired copy. A changed original independently fails the check.
- `ratios` (inside a JSON rule): checks `value = numerator / denominator * scale`
  using pointers into that same document. `scale` defaults to 1; `atol` is an
  explicit absolute tolerance in the **reported value's units**, default 0.
  A zero denominator, boolean, missing field or non-finite value fails.
  The numerator/denominator must be measured results, not published target
  values. A ratio check alone cannot certify where those numbers came from.
- `arrays` (optional): checks actual uncompressed numeric `.npy` content, exact
  dimension count and declared sizes. `null` means an unspecified dimension;
  `[null, 2]` cannot accidentally accept 30-dimensional coordinates. Use an
  independently known sample count instead of `null` when checking row count.
  `finite` defaults to `true`; disabling it is an explicit contract choice.
  `nonconstant: true` requires at least two unequal values. Optional inclusive
  `minimum`/`maximum` bounds also require finite values. Use these rules only
  when the method requires variation/ranges; legitimate constant arrays are
  not rejected by default.
- `numbers` (inside a JSON rule) maps pointers to optional inclusive bounds,
  for example `"/threshold": {"minimum": 0, "maximum": 1}`. An empty bounds
  object still requires a finite number: `null`, booleans and numeric strings
  cannot stand in for a valid threshold. A scientifically justified absent
  threshold should be reported as a limitation, not forced into the interval.

Malformed contracts fail before starting the runner. Missing/invalid outputs,
wrong fields, changed originals or unsafe paths produce `ACCEPTANCE_FAILED`,
not successful completion. Partial model output is retained but is **unverified**.
Review that diagnostic before using a file. Checks do not roll back mutations
or automatically repeat side effects.

## Numerical artifact example

This generic rule checks a saved percentage and two-dimensional embedding:

```json
{
  "unchanged": [],
  "json": [{
    "path": "results.json",
    "equals": {"/target_guided": false},
    "ratios": [{
      "value": "/percent", "numerator": "/count", "denominator": "/total",
      "scale": 100, "atol": 0.001
    }]
  }],
  "arrays": [{"path": "embedding.npy", "shape": [null, 2], "finite": true}]
}
```

Use the object on a single first line after `XDSH-VERIFY: ` for host-enforced
task acceptance. Both `unchanged` and `json` must be present; either may be empty
when another check exists. Expected values must be selected before execution
from independent evidence, not copied from the output to make it pass.

An explicit `target_guided: false` field checks the **saved declaration** only.
It cannot prove that code avoided target-fitting or actually used the reported
parameters. A matching percentage cannot establish methodological fidelity;
2D coordinates do not establish that the generating algorithm was UMAP.
Data provenance, methods, sample ordering, timing, statistical inference and
scientific interpretation require separate evidence and review.

## Read-only self-check CLI

### Recompute rates from actual saved scores

`xdsh-verify --score-example` prints an optional schema for a threshold-based
analysis with observed and simulated score vectors. Its `threshold_rates` rule
reads bounded numeric `.npy` files and checks **all** of: observed positive count,
observed total, detected fraction, simulated detectable fraction, and overall
estimate (`detected / detectable`). Reports cannot substitute a paper target
for the measured numerator or pass merely because invented counts are internally
consistent. All reads share the same per-check bytes/hash cache.

The fields `observed` and `simulated` are relative NPY paths. `threshold`, `count`,
`total`, `detected`, `detectable`, and `overall` are required pointers into the
report JSON. `comparison` is `gt` (default, strictly greater) or `ge`; select the
actual method, not whichever yields a closer answer. `scale` defaults to 1
(fractions), or use 100 for percentages. `atol` defaults to 0 in reported units.
Counts must be exact JSON integers. Vectors must be nonempty, one-dimensional
and finite. No simulated positives makes the overall estimate undefined and
fails the check; null/zero does not stand in for scientific validity. Up to eight
rate rules per JSON file use the same 2 MiB/file limit and no-symlink reader.

This rule does not prove a threshold was chosen independently, scores used the
correct algorithm, labels match sample identities, or the paper method is right.
Use frozen source checks for independently specified thresholds/reference data
where appropriate and review those scientific assumptions separately. Do not
convert a JSON-only task to arrays just to use this optional check.

### Check the largest reported difference, not just selected metrics

Inside a JSON rule, `max_abs_differences` verifies a report's maximum absolute
difference from **all entries in two label-aligned maps**. For example:

```json
{
  "unchanged": [],
  "json": [{
    "path": "results.json",
    "max_abs_differences": [{
      "value": "/max_error_pp", "actual": "/observed", "reference": "/reference",
      "scale": 100, "atol": 0.001,
      "required_metrics": ["metric_a", "metric_b"]
    }]
  }]
}
```

If `observed` is `{"metric_a": 0.102, "metric_b": 0.647}` and `reference` is
`{"metric_a": 0.10, "metric_b": 0.62}`, the maximum is **2.7 percentage points**,
not 0.2. Here both inputs are fractions, hence `scale: 100`. Already-percentage
inputs need `scale: 1`; relative-percent error is a different quantity. Select
the units and tolerance from the intended protocol, not to force a pass.

Each map needs the same 1–128 nonempty labels and finite numeric values. Extra
or missing labels, booleans, non-finite values, overflow and missing reported
maxima fail. `scale` must be positive; `atol` must be nonnegative. Both default
to 1 and 0 respectively. This checks the **reported maximum**, not whether
that deviation is scientifically acceptable. The optional `required_metrics`
list pins the complete nonempty, unique label set: both maps must match it
exactly. This prevents a missing metric in **both** maps from silently shrinking
the check. Declare labels from the requested protocol before seeing results.
The host cannot infer an important metric omitted from the declaration itself.
Use independently specified `equals`/`source_json` assertions to fix the reference
values when needed; a model's chosen reference is not ground truth.

### Prevent silent parameter drift

`source_json` maps an output JSON pointer to `{ "path": "method-plan.json",
"pointer": "/parameters" }`. The referenced path must be declared in
`unchanged`. This compares complete, type-sensitive parameter objects without
duplicating a handwritten expected value in every downstream check:

```json
{
  "unchanged": ["method-plan.json"],
  "json": [
    {"path": "run.json", "source_json": {
      "/effective_parameters": {"path": "method-plan.json", "pointer": "/parameters"}
    }},
    {"path": "figure.json", "source_json": {
      "/analysis_parameters": {"path": "method-plan.json", "pointer": "/parameters"}
    }}
  ]
}
```

Write the plan from applicable methods **before** running; write effective
parameters from the actual program arguments, not copied report prose. Reuse
saved analysis intermediates for figures, or pass that same parameter object
explicitly. A missing field, changed parameter, unsafe reference or modified
source fails. Changing both the plan and the result does not erase a captured
before-state mismatch. Sources and references use the same bounded no-symlink
reader; no Python source or arbitrary validator is executed.

This detects inconsistent declarations, not whether the scientific plan was
correct or whether uninstrumented code secretly ignored a declared parameter.
The CLI still captures its baseline only when invoked. A user-specified first-line
contract captures existing sources before the run; the automatic deferred entry
captures them at its first valid declaration. An agent must not rewrite the
host's first-line contract or imply either baseline existed earlier than it did.

### Check actual Python argument binding

`xdsh-verify --method-example` prints an adaptable example of
`xdsh.core.method_evidence.verified_call(function, expected, receipt, /, *args, **kwargs)`.
The helper runs **in the agent's existing Python process with its existing
permissions**. It binds actual arguments, expands defaults and compares the
selected JSON-safe parameters with the method plan **before invoking the
function once**. An omitted argument that changes the planned behavior is
blocked. A failed invocation invalidates a previous successful receipt.
Asynchronous/generator calls are not supported. Data objects and exception
bodies are not recorded. This does not create a privileged host executor.

Declare a receipt in the contract, with its reviewed source plan frozen:

```json
{
  "unchanged": ["method-plan.json"],
  "calls": [{"path": "method-call.json", "source": {
    "path": "method-plan.json", "pointer": "/parameters"
  }}],
  "json": [{"path": "results.json", "numbers": {
    "/threshold": {"minimum": 0, "maximum": 1}
  }}],
  "arrays": [{"path": "scores.npy", "shape": [null],
              "nonconstant": true, "minimum": 0, "maximum": 1}]
}
```

The host checks the returned status, selected effective parameters and their
canonical hash against the source plan. For a separate analysis virtualenv,
the helper uses the standard library; its package root is printed by
`xdsh-verify --module-root` and can be added to that process's `PYTHONPATH`.
Receipts are workspace-relative, owner-only files; their parents must exist
and cannot be symlinks. Never put credentials in the selected parameters.
Give every dataset/stage a distinct receipt and declare each separately; duplicate
`calls` receipt paths are rejected. One declared receipt still cannot prove that
all requested datasets were checked. Instrument the actual library call rather
than a wrapper that hides critical defaults; the helper sees only selected bound
arguments and cannot inspect the wrapper's internals.

These receipts are **model-writable, unsigned evidence**, not authenticated
proof of execution. The helper cannot certify the function's implementation,
unselected internal parameters, input-data identity or scientific validity.
Do not fabricate or retroactively manufacture a receipt for an earlier run.
For R/other languages, `source_json` can check effective-argument metadata,
with the weaker declaration-consistency assurance described above.

### Bind calls to stage artifacts

Parameter-only receipts do **not** bind later saved files. Opt into that check
with `xdsh-verify --stage-example` (JSON) and `--stage-call-example` (Python).
Pass an `ArtifactBinding` instead of the third `receipt` string to `verified_call`:

```python
binding = ArtifactBinding(
    "stage1-call.json", inputs=("input-ids.json", "method-plan.json"),
    outputs=("stage1-scores.npy", "stage1-ids.json"), code=("analysis.py",),
    packages=("scanpy",), save_result=save_stage1,
)
verified_call(actual_library_function, planned_parameters, binding, **actual_arguments)
```

This is schematic: adapt the actual function, selected parameters, files and
serializer. The matching `calls` entry retains `path` and `source`, and adds:

```json
{
  "artifacts": {
    "inputs": ["input-ids.json", "method-plan.json"],
    "outputs": ["stage1-scores.npy", "stage1-ids.json"],
    "code": ["analysis.py"],
    "packages": {"scanpy": "1.11.0"}
  }
}
```

The example version is **not a recommended or installed version**. Declare the
actual intended installed distribution version. No packages are installed by
this helper. Code and root input files must already be in `unchanged`; later
stages can use outputs of earlier declared stages as inputs. The call list is
ordered by dependency. Receipt paths and output paths cannot collide, outputs
cannot overwrite frozen inputs or another stage's output, and each stage uses
distinct snapshots. Prepare the driver and inputs before preflight; a frozen
driver is not editable in the same binding. Legitimate method/code revisions
need a new explicit task/baseline, not a hash rewrite on the old one.

The helper captures input/code SHA-256 before execution, binds effective
parameters, invokes once, and optionally invokes a synchronous `save_result`
immediately after return. This supports libraries that mutate an in-memory
object: the serializer closes over that object and saves its current state.
It then rechecks input/code bytes and fingerprints saved outputs. Numeric NPY
shapes come from the actual header/payload, not a claimed report field. It also
records Python version, the callable's module-qualified name, and requested
installed distribution versions. An optional `artifacts.method` in the contract
pins that exact qualified name; no import or function lookup is performed by
the checker. Without it, the name is recorded but not compared with a planned name.

Missing/unsafe evidence, changed inputs/code, parameter mismatch or failed/deferred
serialization cannot produce a returned bound receipt. Receipts are atomically
replaced using owner-only files; failures invalidate this attempt's prior success
but do not delete partial outputs or other stages' checkpoints. This is not a
filesystem transaction or a power-loss durability guarantee. Resume/replay is
not automatic: inspect the saved outputs and failure state first.

Final checking re-reads each declared artifact through the same bounded reader
and compares bytes/size/shape, method pins and package pins. Replacing output with
different values fails even when dimensions/counts are unchanged. An earlier
unbound receipt cannot satisfy a bound contract. `stage_checkpoints` reports
checked/verified counts; `--details` adds each declared stage's receipt path,
recorded outcome and current match result. It can report an intact first stage
and a failed second stage without pretending the whole task succeeded. Invalid
bound stages suppress automatic delivery-repair segments: repairing provenance
by rerunning an experiment or merely changing a hash is not authorized. Ordinary
derived-report corrections with intact bindings remain eligible for bounded repair.

These are **caller-writable consistency records, not authenticated provenance**.
The helper cannot prove a function consumed the declared file, that an existing
in-memory object came from it, that a serializer did no extra computation, or that
the named code files fully describe the implementation. Package coverage is only
the declared distributions, not a full environment lock. Old successful files
can still be misrepresented by dishonest code; the checker is not an adversarial
attestation service. Use fresh stage snapshots and independent scientific review.
Late fingerprints cannot retroactively prove an earlier experiment's history.

### Keep summaries and ordered IDs consistent

`artifact_json` has the same `{path, pointer}` reference shape as `source_json`,
but compares against **current saved artifacts**, including outputs not yet
present at preflight. For example, a report's `/marker` can reference
`markers.json` at `/by_marker/MS4A1`. A stale cluster/rank fails after the table
changes; strict JSON types and list order are preserved. Referencing a frozen
input is allowed and its pointer is validated before freeze. New relationships
still do not authenticate the table's biological interpretation.

Use `id_selections` for an explicit mask/index mapping, rather than only a count:

```json
{
  "path": "ranks.json",
  "id_selections": [{
    "value": "/top_ids",
    "source": {"path": "filtered-genes.json", "pointer": "/ids"},
    "indices": {"path": "ranks.json", "pointer": "/rank_order"}
  }]
}
```

Exactly one selector is required: `indices` (distinct, zero-based integers) or
`mask` (strict booleans, same length as the source IDs). Source/result IDs must
be unique, nonempty strings; selection order matters. Chain a mask mapping to
filtered IDs and then a rank mapping to catch using filtered positions against
the unfiltered gene list. Empty selections may be legitimate and are not alone
evidence of a successful analysis.

An array rule can add `axes: {"0": {"path": "ids.json", "pointer": "/samples"}}`
to require a unique ID for each row; other axes work similarly. This checks
axis length, **not the semantic identity of each numeric row**. Use bindings and
ordered equality/selection together; independently review how matrices were
constructed. For a summary of a saved NPY shape, `artifact_json` can reference
the bound receipt's `/artifacts/outputs/embedding.npy/shape`; the binding check
independently verifies that shape against the actual file. Do not use unbound
self-reported shape metadata as equivalent evidence.

All this remains opt-in through declared checks. Non-Python tools keep their
weaker metadata path; do not forge Python receipts to simulate instrumentation.
The compact-evidence path still reads **at most 2 MiB per file**. For larger
originals, opt into the separate streaming profiles below. A compact summary
alone never proves that a large original was checked.

### Check large originals explicitly

`xdsh-verify --large-example` prints a contract for a saved HDF5/AnnData matrix
and an NPY embedding. `large_files` is a map from exact workspace-relative file
paths to profiles; no directory crawling, path inference, downloads or unpickling.

```json
{
  "unchanged": ["original.h5ad", "ids.json"],
  "json": [],
  "large_files": {
    "original.h5ad": {
      "format": "hdf5",
      "datasets": {
        "X": {
          "encoding": "csr",
          "shape": [null, null],
          "axes": {
            "0": {"dataset": "obs/_index", "reference": {"path": "ids.json", "pointer": "/cells"}},
            "1": {"dataset": "var/_index", "reference": {"path": "ids.json", "pointer": "/genes"}}
          }
        }
      }
    },
    "embedding.npy": {"format": "npy", "shape": [null, 2], "finite": true}
  }
}
```

Adapt `X`, the actual index dataset names, encoding (`dense`, `csr`, `csc`),
dimensions and IDs. AnnData's index key need not be `_index`. The reference
JSON must be in `unchanged`; it supplies independently reviewed, unique ordered
string IDs, not just a count. Only declared datasets are inspected: this is
**not validation of every AnnData field or numerical value**.

- `raw`: stream SHA-256/byte count only. Supports opaque originals without a native
  dependency; does **not** certify format, compression integrity or scientific values.
- `npy`: stream the complete primitive array, checking header/payload length,
  optional shape (`null` for an unconstrained axis), `finite` (default true),
  `nonconstant` and optional `minimum`/`maximum`. No sample-only shortcut. A
  nonconstant/range constraint also rejects nonfinite numbers. Object/pickled,
  compound or compressed NPZ files remain unsupported.
- `hdf5`: hash the entire original, then check selected actual dense shapes or
  CSR/CSC shape, vector lengths, monotonic indptr, terminal count and index bounds.
  Check each requested axis's unique ID count and exact UTF-8 order against its
  frozen reference. The matrix is **never densified**; data values and unselected
  datasets are not scanned for finiteness. This does not certify analysis accuracy.

HDF structural checks require `h5py==3.16.0` in the **verifier's** Python environment.
Install the optional extra during deployment, for example
`pip install 'xdsh[science]==0.1.1rc2'` after this build is published, or
`uv sync --locked --extra science` in a source checkout. Installing h5py in an
agent's separate analysis venv does not install it in the host verifier. Missing
dependencies fail the declared check; the checker never auto-installs, silently
downgrades to raw mode, or weakens a frozen contract. Core/raw/NPY checks do not
require NumPy or h5py. The public CI includes the science extra to exercise HDF fixtures.

For bound stages add `large_files` to **both** the contract's `calls.artifacts`
and the helper, with the same input/output paths:

```python
binding = ArtifactBinding(
    "call.json", inputs=("original.h5ad",), outputs=("processed.h5ad",),
    code=("analysis.py",), large_files=("original.h5ad", "processed.h5ad"),
)
```

Also give both files their top-level `large_files` profiles. Large method receipts
bind **all bytes and size**, while profiles independently perform the declared
structure/numeric/identity checks; unlike compact NPY receipts, do not expect a
`shape` field in their fingerprint. Existing small-file contracts are unchanged.
JSON, receipt, code and reference paths cannot be promoted to the streaming path
to bypass their parsing limits. The host store persists profiles with the original
baseline, and restore does not rehash/recapture current inputs as new originals.

The result adds `large_artifact_checks` counts. `--details` includes per-file
format, coverage, match and a content-free reason. Raw coverage is labelled
`sha256_only`; HDF coverage is `declared_hdf_structure_and_requested_ids`.
`scientific_validity` remains `not_assessed`. Invalid large evidence retains
partial answers/files and **does not automatically rerun the experiment**.

#### Large-reader resource and trust boundaries

At most 8 streamed files, each at most 64 GiB; the input buffer is 1 MiB.
Hash/NPY loops have a 180-second per-file deadline checked between reads. This
does not interrupt an OS filesystem read already blocked in the kernel. These
are **validator budgets, not task runtime/token quotas**: exceeding them leaves
the evidence unverified and does not kill the analysis process or delete outputs.

HDF parsing runs trusted bundled code in a separate process, using the same pinned
read-only file descriptor as the hash. It receives no provider credentials,
workspace Python path or plugin environment. Dynamic HDF plugins are disabled.
Each selected object path must traverse only HDF hard links; external/soft links,
external storage, virtual datasets, arbitrary filters and non-primitive numeric
dtypes are refused. Supported compression filters are gzip, shuffle, Fletcher32
and bundled LZF; an uncompressed chunk is at most 8 MiB. Up to 8 objects/file,
8 axes/object, 200,000 IDs/axis, 256 characters/ID and 256 MiB of sparse-index/ID
scan work. Logical dataset size is bounded to 256 GiB; this is not allocated.

The native worker has a 30-second wall deadline and 20-second CPU limit. On
Linux it additionally has 1 GiB address-space and 512 MiB data limits; on macOS,
where lowering those limits is unsupported in the tested runtime, the parent
checks child RSS every 50 ms and kills it above 512 MiB or if monitoring fails.
macOS monitoring can overshoot between samples and **is not a hard memory cap**.
Failures/timeouts reap only that child. Linux limits need actual target-runtime
acceptance before deployment; local macOS synthetic tests do not establish Linux
readiness. This is process/resource isolation, **not an OS security sandbox** or
a guarantee against all native-parser vulnerabilities.

Format and parser boundaries follow the primary documentation:
[h5py datasets](https://docs.h5py.org/en/stable/high/dataset.html),
[AnnData on-disk sparse/index layout](https://anndata.readthedocs.io/en/stable/fileformat-prose.html),
[HDF plugin controls](https://support.hdfgroup.org/documentation/hdf5/latest/group___h5_p_l.html),
and [Apple process-memory fields](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/sys/proc_info.h).

Filesystem symlinks, large-file hardlinks, devices/FIFOs, traversal, replacement
and detected concurrent writes are refused. Stable descriptors and before/after
metadata checks cover the read interval, not an atomic multi-file snapshot or
future changes. Signatures/attestation and scientific truth remain out of scope.

### Run a declared check

`xdsh-verify --example` prints an editable example. Save a reviewed contract as
`checks.json` in the intended workspace, then run:

```sh
xdsh-verify --workspace /path/to/isolated-workspace --contract checks.json
```

The equivalent module entry is `python -m xdsh.ops.verify`. No model request,
network, shell command or experiment rerun is performed by this checker. HDF
profiles use the optional native dependency in a bounded subprocess as above;
all other profiles remain dependency-free. It returns JSON and exits **0** for passing declared checks, **1** for
an artifact mismatch, or **2** for an invalid/inaccessible contract.

Without a host `--baseline`, this command is **post-hoc self-checking**, not
retroactive host acceptance.
It always reports `scientific_validity: not_assessed` and
`before_state: captured_at_this_check_not_at_experiment_start`, plus
`baseline_scope: standalone_current_files_not_host_acceptance`. In particular,
`unchanged` here cannot prove that an experiment left a source unchanged before
this command started. Model-authored contracts remain model self-checks; do not
present them as an independently specified validation protocol. With the
generated `--profile research --baseline <host-path>` command, the baseline is
the host snapshot instead; `before_state` is
`host_frozen_not_proof_of_experiment_start` (or `draft_not_frozen` in preflight).
`scientific_validity` remains `not_assessed` in either mode.

Add `--details` to include the SHA-256 and byte length of each successfully read
file, plus computed maximum differences, the largest metric label and comparison
counts. A missing claimed maximum still returns the computed value if the two
maps are valid. No file body is printed, but paths/metric labels can themselves
be sensitive: review a receipt before sharing it.

All assertions for the same path use **the same bytes read once during this
check**, including the receipt hash. This prevents a changing file from passing
contradictory assertions across repeated reads. It is not an atomic snapshot
across files, does not lock future edits and is not a signed attestation. Keep
the receipt alongside the report and rerun after changes. Without `--details`,
evidence bodies and detailed numeric receipts are omitted. Ordinary chat gains no extra model calls
or background file scans.

## Scope and limits

The criteria are declared: XDSH does not infer ground truth from arbitrary prose,
Todo checkboxes or a model's assertions. A managed deferred research contract
is persisted as described above; an explicit non-deferred contract or custom
guard without `acceptance_root` is held in memory for one invocation. Workspace
files cannot rewrite a frozen contract in either case. A new task/checklist
captures a new baseline, not the baseline from an earlier task.
Use independently known expected values when you need historical guarantees.

Paths must be workspace-relative with no `..`, absolute paths or symlink
traversal. Only regular files are read, up to 2 MiB each on the compact path;
explicit `large_files` use the separate limits above. At most 8 originals,
8 JSON rules, 8 array rules, 8 call receipts and 16 entries per field map,
including each classified expectations map,
numeric map, ratio list or maximum difference list. Each bound stage allows
1–8 inputs, 1–8 outputs, 1–4 code files and up to 8 distribution pins. Each JSON
rule permits up to 8 ID selections; ordered lists contain at most 200,000 unique
IDs of 1–256 characters (the file-byte limit may be reached sooner). A deferred profile also
retains its one checklist hash. No glob
or shell syntax is executed. NPY headers are limited to 4096 bytes and shapes to
8 dimensions. NPY 1.0/2.0/3.0 primitive bool, integer and float32/float64 arrays
are supported; object/pickled, structured, complex, string and compressed NPZ
files are deliberately unsupported. Streamed profiles are explicit opt-in,
not permission to use the JSON parser as an unlimited reader.
The reader follows the [official NPY format](https://numpy.org/doc/stable/reference/generated/numpy.lib.format.html).
The header is limited to 8 KiB. POSIX safe descriptor-relative reads are required
(Linux/macOS/WSL); unsupported hosts refuse explicit checks rather than weakening
isolation. Ordinary tasks do not scan any additional files. A pending research
declaration reads only its one known path after tool results until capture.

The checks run in XDSH's runner-neutral guard, including managed Telegram and CLI
flows. Custom integrations that bypass the guard must call the acceptance module
explicitly. DSH used independently does not inherit these XDSH checks.

## Research workflow and progress

Built-in research, artifact-quality and evidence-reporting Skills guide source
selection, method-to-code mapping, preflight checks, reproducible entrypoints,
measured timings and separation of independent validation from target-guided
fits. These are model instructions, **not enforced scientific guarantees**.

Native Todo updates remain the only evidence for plan completion percentages.
When an unfinished plan is stale, the progress card keeps its last snapshot and
shows actual tool returns/model rounds **since that snapshot**, plus event age.
Clock ticks and streamed tool arguments show liveness, never fabricated step
completion. A checked Todo means the model reported completion, not that these
artifact checks or scientific review passed.
