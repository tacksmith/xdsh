# XDSH

Laptop deployments: [optional keep-awake](KEEP_AWAKE.md) requests idle-sleep
protection while the Bot runs; setup option `xdsh-setup --keep-awake on`.
Containers/WSL need host-side configuration. Increased battery usage is possible.

**Use DeepSeek Harness from Telegram, QQ Official Bot or your terminal—with managed sessions,
visible progress, approval-gated memory and bounded task execution.**

Language: [English](README.md) · [简体中文](README.zh-CN.md) ·
[繁體中文](README.zh-TW.md)

Compatibility: [XDSH / DSH version matrix](COMPATIBILITY.md)

## What it is and what you can do

XDSH (**eX**ternal DeepSeek Harness) is an external product layer, not a new model, a DSH fork or a custom plugin.
DSH executes tasks; XDSH manages interaction and operational boundaries.
Delegate coding/workspace tasks, research with configured retrieval tools, and
continue named work from a phone or terminal. Results depend on your model,
enabled tools and permissions.

## Highlights

| Need | Practical benefit |
| --- | --- |
| Work away from your desk | Telegram/CLI sessions and model selectors; QQ Official Bot text interaction |
| See what is happening | Separate progress/answers, grouped tools and reported Todo state |
| Govern knowledge | Local memory, scoped search, approval-gated memory/Skill changes |
| Limit wasted work | Context protection, retry/token/stall guards and partial-result retention |
| Bring your models | TUI/Telegram onboarding; multiple API families through the managed gateway |

These are benefits of a combination, not exclusive inventions or a universal
speed advantage. Model reasoning is not automatically improved. Recognized
uncertain/conditional answers are excluded from semantic shortening; arbitrary
character limits and factual correctness are not guaranteed.

## Deployment map

```text
Telegram / QQ / CLI → XDSH → DshRunner → official DSH RC2
                                      ↓ Anthropic Messages
                              loopback model gateway
                                      ↓ provider API
                               local or online model
```

For a first installation, put XDSH, DSH and the gateway on one authorized host.
A hosted API needs no local GPU; local inference needs a separate model server.
Docker is optional for the first CLI test.

| Component | You provide | Check |
| --- | --- | --- |
| Host | Python 3.10–3.13, native Node 22.19.0+ and npm | macOS uses Mac tools; Linux/Ubuntu WSL uses Linux tools, not Windows executables |
| XDSH + DSH | This source release; exact DSH 0.1.1-rc.2 packages | XDSH 0.1.1rc2 / DSH 0.1.1-rc.2; never floating latest |
| Model gateway | Existing loopback Messages route, or managed gateway | Exact model ID and credentials; saved does not mean verified |
| Telegram | Independent Bot token and numeric user allowlist | Doctor, /start and a tiny task |

Order: **install pair → start/configure gateway → configure XDSH → verify CLI
→ enable Telegram → configure persistent service**. A package install or Bot
token alone is not a working deployment. Preserve existing services and keep a
rollback. Downloads and network conditions determine installation time.

## Why XDSH exists

XDSH was created to keep the useful outer experience of an agent product while
replacing the inner runtime with the official DeepSeek Harness. The goal is a
small, auditable boundary: DSH remains responsible for agent execution, while
XDSH provides sessions, Telegram/CLI interaction, durable long-task controls,
structured progress, skills, context protection, and safe operational edges.
That separation lets the runtime evolve without making the user-facing product
or its safety rules depend on private implementation details.

XDSH is intentionally direct and bounded. It plans briefly, chooses the
shortest safe execution path, preserves partial results when a run stops, and
fails closed at credential, network, workspace, and resource boundaries.

## DSH alone or DSH with XDSH?

DSH remains independently usable through its CLI, Web UI, and plugin manager.
XDSH does not fork or hide those surfaces; it adds named channel sessions,
durable queue/job state, bounded retry/context protection, approval-gated
memory and Skill changes, content-free audit, and consistent remote progress.
Use DSH alone for direct local work, or put the same installation behind XDSH
when you need managed Telegram/CLI workflows and operational safeguards.

## What you get

- CLI and Telegram interaction with named sessions and multi-turn context;
- structured task/tool/progress events, cancellation, queue/insert choices, and
  bounded retry/token protection;
- package-owned Skills, context compaction, model/provider selection, jobs, and
  an authenticated loopback settings UI plus safe Web/MCP/browser/media seams;
- scoped redacted session search with bounded exact/partial/typo retrieval and
  content-free `/memory index` capacity diagnostics;
- isolated `~/.xdsh` state and an explicit DSH runtime command; no custom DSH
  plugin and no credentials in the package.

## Install the pinned pair

Download and extract this release's source archive from GitHub, then open a
terminal in its project directory. These instructions install that local
release; they do not require an XDSH package to exist on PyPI.

The declared Python range is 3.10 through 3.13; the public CI workflow is
configured for all four versions. A configured workflow is not proof that it
has run: see the dated release evidence for actually verified combinations.
DSH itself still follows the Node/runtime requirements in the compatibility guide.

The release checks cover Apple Silicon macOS and Linux x86_64 containers. Intel
macOS is not a verified full-stack target: the security-updated `cryptography`
dependency used by `gateway`/`channels` no longer supports it. Prefer a supported
Linux host for that configuration; do not downgrade security dependencies to
force installation. Windows instructions target Ubuntu WSL, not native Windows;
see [dated verification and remaining limits](RELEASE-NOTES-0.1.1-rc.2.md).

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install '.[channels]'
```

The setup command below asks for a language. You can also select it explicitly with
`--language en`; non-interactive setup defaults to English. Supported choices
are Traditional Chinese, Simplified Chinese, English, Japanese, Korean,
Spanish, French, German, Brazilian Portuguese, Russian, Arabic, Hindi, and
Indonesian.

On macOS, install native Mac Node `22.19.0` or newer; on Linux/Ubuntu WSL,
install Linux Node at that minimum (required by the DSH `pi-ai` dependency).
Use the same platform/architecture for Python, Node and their native packages.
Then install the official DSH release in an XDSH-owned npm prefix and verify
it. If a prior WSL XDSH run already has the validated Linux
Node under `~/.xdsh/node-download`, the WSL installer reuses it automatically.
Otherwise set `XDSH_NODE_BIN` or prepend its Linux `bin` directory; a Windows
`node.exe`/npm mix is rejected explicitly.
This keeps a system or legacy DSH installation untouched:

```bash
DSH_NPM_ROOT="$HOME/.xdsh/dsh-npm"
NODE_BIN="${XDSH_NODE_BIN:-$(command -v node)}"
mkdir -m 700 -p "$DSH_NPM_ROOT"
npm install --prefix "$DSH_NPM_ROOT" --no-fund --no-audit \
  @deepseek-ai/dsh@0.1.1-rc.2 \
  @deepseek-ai/dsh-sdk-jsonrpc-demo@0.1.1-rc.2 \
  @deepseek-ai/dsh-sdk-jsonrpc-server@0.1.1-rc.2 \
  @deepseek-ai/dsh-agent-spine-demo@0.1.1-rc.2
"$DSH_NPM_ROOT/node_modules/.bin/dsh" --version
DSH_JSONRPC_BIN="$DSH_NPM_ROOT/node_modules/@deepseek-ai/dsh-sdk-jsonrpc-demo/lib/packaged-bin.js"
xdsh-setup --mode both --runtime-command "$NODE_BIN ${DSH_JSONRPC_BIN}" --write-env
```

First check `node --version` and `node -p 'process.platform + "/" + process.arch'`.
`NODE_BIN` must resolve to the host's native Node `22.19.0+` executable. On WSL,
the WSL installer is preferred because it rejects Windows executables and can
reuse its own managed Linux Node automatically; do not run that installer on Mac.

After a WSL install, check the isolated DSH without relying on the shell's
Node PATH with `~/.xdsh/bin/dsh --version`. The owner-only wrapper stays below
`~/.xdsh` and does not replace a system DSH; add `export
PATH="$HOME/.xdsh/bin:$PATH"` only for the current shell if you want to type
`dsh` directly.

For a Windows WSL installation, use the bounded
[`scripts/wsl_install.sh`](scripts/wsl_install.sh) instead; it performs
the same prefix setup, keeps official peer dependencies enabled, and accepts
only the pinned DSH `0.1.1-rc.2` JSON-RPC runtime. Do not use a global npm
install for XDSH or overwrite an existing DSH runtime.

The `channels` extra includes the official QQ SDK and cryptographic verification
for optional channel adapters. For Telegram/CLI only, `pip install .` is enough;
add `.[qq]` for QQ alone or `.[gateway]` for the managed multi-provider gateway.
QQ has its own onboarding, allowlist and feature limits in [CHANNELS.md](CHANNELS.md);
it does not imply Telegram command/menu parity.

XDSH requires an explicit DSH JSON-RPC runtime command. The command is
deliberately not guessed from a shell or silently replaced with the Python
SDK's bundled preview runtime. Put the exact command from the installed DSH
release into the generated `~/.xdsh/config/cli.env.example` or
`telegram.env.example`, then copy and edit the file without committing it.
If a live environment file already exists, edit it in place; do not overwrite
credentials or settings with a fresh template.

The stable RC2 publication uses one deliberately aligned release line:
XDSH `0.1.1-rc.2` and DSH `0.1.1-rc.2`, with
`deepseek-harness-sdk==0.1.1rc1` as the separately versioned protocol client.
The Python distribution spells the same prerelease as `0.1.1rc2` to comply
with PEP 440, so `xdsh[channels]==0.1.1rc2` and DSH `0.1.1-rc.2` are the same
compatibility pair. The installer rejects every other DSH line. See the exact
mapping in [`RELEASE-NOTES-0.1.1-rc.2.md`](RELEASE-NOTES-0.1.1-rc.2.md).

The aligned RC2 pairing is the active stable publication and rollback baseline.
See [`RC2-BASELINE.md`](RC2-BASELINE.md) and
[`rc2-baseline.json`](rc2-baseline.json) for the exact lock. The former
`0.1.0a0-rc2` artifacts are retained as maintainer-only immutable historical
evidence outside this public source bundle and are not used by the current
installer or image.

## Configure a loopback LiteLLM route

Choose one path: reuse an existing loopback Messages gateway, or install
`.[gateway]` and follow [managed gateway setup](PROVIDERS.md). Provider keys
belong in the gateway process, separate from the gateway access key used by
XDSH. Configure the XDSH environment file or process environment:

```text
XDSH_LITELLM_BASE_URL=http://127.0.0.1:4000
XDSH_LITELLM_API_KEY=your-independent-loopback-key
XDSH_LITELLM_MODEL=your-exact-configured-model-id
XDSH_DSH_RUNTIME_COMMAND=/absolute/path/to/the/official/dsh-jsonrpc-command
```

The gateway URL must resolve to loopback. DSH still uses Messages; the optional
gateway translates OpenAI Chat Completions, Gemini and Azure as documented in
PROVIDERS.md. This is not native OpenAI transport in DSH or universal model support.
Start the gateway in a separate process before the CLI or Telegram service.

The model selector is inclusive without pretending that metadata is proof of
execution. `XDSH_MODEL_PROVIDER_ALIASES` groups live routes and, with
`XDSH_SETTINGS_PATH` enabled, a credential-free `settings.json` may also
declare a route omitted by LiteLLM `/model/info`:

```json
{"schema_version":1,"default_model":"my-company-route",
 "providers":[{"id":"company","label":"Company gateway","icon":"🏢"}],
 "models":[{"id":"my-company-route","provider":"company","tools":true}]}
```

Such a route is displayed with unknown health until a bounded execution probe or
real task verifies it; an explicit unavailable result is not selectable. The
settings file contains presentation/capability metadata only—never endpoint
credentials—and is validated before use. Duplicate keys, non-finite numbers,
unknown fields, and credential-shaped values fail closed.
The repository includes a starter at `examples/settings.json.example`; copy it
to the path named by `XDSH_SETTINGS_PATH` and edit only the route metadata.

### Configure models without editing JSON

For real endpoint/key/model onboarding, use `xdsh-providers tui` (or
`xdsh-config tui`) and Telegram `/model add`. Both write the same approved
connection registry for the optional XDSH-owned gateway. See
[Model connections](PROVIDERS.md) for installation, custom protocols, credential
handling and the distinction between saved and verified routes.

`xdsh-config` is the single credential-free settings control plane used by its
CLI and Web adapters. The CLI is suitable for scripts:

```bash
xdsh-config show
xdsh-config provider-set company --label 'Company gateway' --icon '🏢'
xdsh-config model-set my-company-route --provider company --tools on
xdsh-config set-default --model my-company-route
xdsh-config validate
```

For a local settings page, place a temporary access value in the environment,
then run `xdsh-config serve --port 8877`. The server is hard-bound to
`127.0.0.1`, requires a bearer value from `XDSH_CONFIG_UI_TOKEN`, rejects
cross-origin and stale writes, stores no credential, and never starts DSH or a
model request. If XDSH runs on another machine, reach it only through an SSH
local port forward; do not expose the port publicly. The setup command creates
an owner-only empty `settings.json`, and `xdsh-doctor` validates its schema.

### Low-token safe concurrency

Search and research default to at most four non-overlapping queries and prefer
one batch-capable search call. Repository discovery combines related patterns
into one bounded native search when that preserves meaning. Otherwise, XDSH
allows up to four independent read-only calls in one model step only when the
DSH runtime or tool definition explicitly declares them concurrency-safe.
Independent file/image reads, Web fetches, and read-only metadata/health checks
qualify; writes, edits, installs, state changes, dependencies, and unclassified
tools remain serial.

This uses one model turn and one shared evidence/output budget, deduplicates
results, and never creates subagents solely for speed. Set
`XDSH_MAX_PARALLEL_TOOL_CALLS=1..8` to tune the bound; the safe default is `4`
and `/doctor` shows the effective value.

### 429 protection

Rate limiting is enforced below every transport. The official DSH retry policy
is bounded to five retries per failure episode: approximately 5, 10, 20, 40,
80 seconds with 10% jitter (155 seconds nominal total; 120 seconds per-wait cap).
XDSH limits in-flight turns and paces routes with a capped `Retry-After`
cooldown. RC2's pi-ai adapter can discard the hint; see
[provider limits](PROVIDERS.md#upstream-rate-limits). A 429 never causes an implicit task
replay: after the bound, XDSH emits a structured rate-limit result, keeps the
partial answer/progress, and leaves a later retry to the user. Fallback is
disabled after a 429 by default because provider quotas are often shared; opt
in only when the alternate route has an independent quota:

```text
XDSH_RATE_LIMIT_MAX_CONCURRENT_REQUESTS=2
XDSH_RATE_LIMIT_MIN_INTERVAL_SECONDS=0.25
XDSH_RATE_LIMIT_MAX_RETRIES=5
XDSH_RATE_LIMIT_BASE_BACKOFF_SECONDS=5
XDSH_RATE_LIMIT_MAX_BACKOFF_SECONDS=120
XDSH_RATE_LIMIT_MAX_RETRY_AFTER_SECONDS=120
XDSH_RATE_LIMIT_QUEUE_TIMEOUT_SECONDS=120
XDSH_RATE_LIMIT_JITTER_RATIO=0.10
# XDSH_RATE_LIMIT_ALLOW_FALLBACK=1
```

Leave the defaults unless the gateway operator has measured a different quota.
The policy contains no credentials or provider response bodies; `/status` and
`/doctor` show the effective bounded settings.

Maintainers can run `python -m xdsh.ops.rate_limit_probe` before deployment;
the credential-free probe verifies bounded 429 handling, partial-output
retention, and queue admission without contacting a provider.

## Use the CLI

```bash
# Fill ~/.xdsh/config/cli.env before sourcing it.
set -a; . ~/.xdsh/config/cli.env; set +a
xdsh --check
xdsh --prompt 'Reply with exactly READY.'
# Equivalent module form:
python -m xdsh --check
```

`xdsh-doctor --mode cli --strict` performs a read-only installation check (and
`--mode both` checks both transport environment files). It never starts a DSH
task or calls the gateway, and it never prints credential values. Add
`--probe-runtime` when you want it to run the explicit runtime's bounded
`--version` check. Use
`xdsh --doctor --doctor-json` after tasks to inspect aggregate, content-free
health data.

For a hand-off or incident review, `xdsh-jobs self-check` emits a bounded
cross-adapter contract document. Validate a saved document without opening
SQLite or starting DSH:

```bash
xdsh-jobs validate-contract --snapshot CONTRACT_JSON
```

Telegram accepts the same document with caption `/jobs validate-contract`.
The validator reports adapter/registry drift as data (`status=drift`) and
never turns the upload into a task or writes local state.

Maintainers can verify a staged public bundle before sharing it:

```bash
xdsh-release verify /tmp/xdsh-public
```

This checks the bounded export audit and every SHA-256 entry in
`PUBLIC_RELEASE.json`; it never publishes or contacts a registry.

## Enable Telegram

1. Create an independent Telegram bot and an allowlist containing only the
   intended numeric user IDs.
2. Copy `~/.xdsh/config/telegram.env.example` to
   `~/.xdsh/config/telegram.env`, fill the placeholders, and run
   `chmod 600 ~/.xdsh/config/telegram.env`.
3. Check the configuration:

   ```bash
   xdsh-doctor --mode telegram --env-file ~/.xdsh/config/telegram.env
   ```

4. Start the service in a dedicated process:

   ```bash
   xdsh-telegram --env-file ~/.xdsh/config/telegram.env
   ```

Use `/language` at any time to change the current chat. The selector persists
the choice, refreshes that chat's 37-command Telegram menu, and asks the model
to answer in the selected language unless the task explicitly requests another
one. First-use navigation and command descriptions are native in all thirteen
languages; uncommon fixed diagnostics use English as the safe fallback.

For a confined deployment, follow [`CONTAINER.md`](CONTAINER.md) and mount
only XDSH-owned state. The service binds no public listener; optional internal
gateways are loopback-only and authenticated.

If a foreground Telegram task contains a recognized credential shape, XDSH
shows one inline confirmation (bound to the chat/user, valid for five minutes,
and usable once). Approval sends that task to the live DSH runner while the
outer queue, history, knowledge index, outbox, and reviewers keep only a
redacted copy. Rejecting, expiring, restarting, or shutting down discards the
raw task. Durable background and scheduled tasks still require environment
credential references.

When a chat is busy, each new message is durably queued with a bounded local
relationship hint and five explicit choices: interrupt and checkpoint-resume
the current task, merge a supplement into the active task, keep ordinary FIFO
order, replace the current task, or drop the message. The hint is advisory and
the user action is authoritative. The official DSH JSON-RPC surface has no
in-place pause/steer request, so interruption safely stops the named turn,
keeps a redacted partial checkpoint, runs the selected message first, and
starts a bounded continuation immediately after it. Merge instead checkpoints/stops the active execution segment and continues
immediately with the original task plus the supplement. This is not a native
in-place DSH steer operation. Credential-approved turns are never replayed.

### DSH plugins

DSH remains independently runnable through its own CLI and Web UI. XDSH reads
the official profile manifest (`dependencies` and `dsh.profile.bundles`) each
time `/plugins` is opened, so plugins installed through DSH are visible without
maintaining a second registry. Use:

```text
/plugins list
/plugins profiles
/plugins install @scope/package@1.2.3
/plugins remove @scope/package
/plugins sync source-profile [target-profile]
```

Install/remove/sync create a user-bound Telegram approval card and then delegate
the exact operation to the official `dsh plugin` CLI. Set
`XDSH_DSH_PLUGIN_COMMAND` to an explicit official CLI argv to enable mutations;
without it, inventory remains read-only. `/plugins invoke` is available only
through an authenticated gateway that explicitly declares the package and
passes the versioned `xdsh-plugin-invoke-v1` handshake (the compatibility-era
capability-only adapter is still accepted). On RC2, ordinary tool plugins are
instead selected and called by the DSH agent because the stdio JSON-RPC surface
has no direct Host invocation method. `/plugins` exposes one coherent snapshot
and stable route codes (`direct_gateway`, `agent_tool_only`,
`direct_unavailable`, `gateway_invalid`); direct results are redacted and
bounded before they leave the core boundary. XDSH never edits DSH manifests,
executes a shell, or installs a custom DSH plugin.
The interactive `xdsh` CLI exposes the same inventory and accepts the explicit
`--approve` suffix for local install/remove/sync operations.

### Skills and approval

Research Skills guide source/method mapping, environment preflight and honest
reproduction reports. [Artifact acceptance](ACCEPTANCE.md) checks declared JSON
values, rates, input hashes, array shape/range/non-degeneracy and selected effective
Python arguments. Recognized Chinese/English research-execution requests require
a checklist automatically; ordinary tasks retain opt-in explicit contracts.
Failures retain partial results and explain unmet categories for continuation.
`xdsh-verify` provides read-only self-checks, not independent scientific validation
or proof of complete requirement coverage.
Large originals can opt into streaming SHA-256 and NPY checks, or selected
HDF5/AnnData shape/ordered-ID checks with the optional `science` extra.
Start with `xdsh-verify --large-example`; [limits and setup](ACCEPTANCE.md#check-large-originals-explicitly)
distinguish checked structure from scientific correctness.

XDSH ships a finite set of package-owned foundational Skills and can also load
project-owned Skills from the active workspace, plus a chat-isolated personal
Skill library that is shared across that Bot chat's sessions and workspaces. A reviewer may propose a
`SKILL.md` plus at most 16 UTF-8 text support files (64 KiB per file, 256 KiB
total); paths must stay relative to the Skill and hidden, symlinked, binary,
credential-bearing, or instruction-injection resources are rejected. Support
files are included in the bounded before/proposed/applied tree hash and diff,
but their contents are not copied into a task prompt merely to show them.

Every evolution or archive candidate is pending until an operator explicitly
reviews and approves its scope. Telegram offers separate project and personal
library approval buttons; personal Skills are stored in XDSH-owned state, not
the source tree, and are hash-verified before being reused. Evaluation is
isolated and read-only, stale hashes fail closed, omitted files never mean
deletion, and an approved change can be rolled back after another hash check.
The controls are available from the CLI and Telegram; the personal library is
currently a Telegram-host feature:

```text
/skills pending | diff ID | evaluate ID | approve ID | reject ID | rollback ID
/skills approve-personal ID | personal
/skills curate
/skills archive NAME
/skills archive diff ID | approve ID | reject ID | rollback ID
```

Curator output is an observation-based review/archive suggestion, not an
automatic consolidation or self-modifying loop.

### Memory and session retrieval

Successful turns are redacted and retained under fixed per-session and
per-chat limits. `/search` performs chat/session-scoped SQLite FTS5 retrieval:
exact AND matches come first, followed only by capped partial and
typo-tolerant candidates with deterministic ranking. Approved `MEMORY` facts
use the same bounded lexical tolerance, while `USER` preferences remain
stable. `/memory index` reports counts, retention caps, and retrieval mode but
never stored text. The dependency-free default makes no embedding call. An
operator may explicitly configure an authenticated HTTP-loopback,
OpenAI-shaped embeddings endpoint; hybrid retrieval remains redacted,
bounded, and scoped to the current chat/session, stores only normalized
float32 vectors, reports content-free coverage, and falls back to lexical
search if the endpoint is unavailable.

## Optional messaging channels

Discord, WhatsApp Cloud API, DingTalk, Feishu/Lark, WeChat Official Account,
and QQ Official Bot adapters are available through the runner-neutral channel
contract. The five webhook products require public HTTPS; QQ uses an outbound
official WebSocket and requires no public endpoint. Every platform needs its
own official application and credentials; a Telegram bot token grants none.
Run `xdsh-channels list` or `xdsh-channels check --json` for a names-only
readiness report, then follow [`CHANNELS.md`](CHANNELS.md). Keep webhook hosts
on loopback behind an approved reverse proxy or tunnel. Start QQ separately
with `xdsh-qq --init --env-file ~/.xdsh/config/qq.env`, edit that private
template locally, and run `xdsh-qq --env-file ~/.xdsh/config/qq.env --check`.
Its required user OpenID allowlist fails closed.
`xdsh-qq --onboard --env-file ~/.xdsh/config/qq.env` offers official scan
binding and stores the resulting credentials privately; `--from-env` explicitly
imports only runtime/model settings from an existing installation. See the
channel guide for the dedicated state layout and local gateway prerequisite.

For a field installation inside Windows WSL, run
[`scripts/wsl_install.sh`](scripts/wsl_install.sh) from the checkout and
follow [`WSL.md`](WSL.md). The script installs only the validated DSH
`0.1.1-rc.2` pair and rejects a floating or different runtime version. It never
overwrites an existing DSH without an explicit upgrade flag.

## Deploy with an AI agent

Give an agent access to this checkout and paste:

```text
Deploy this XDSH source release on my authorized host. Read README.md,
COMPATIBILITY.md, PROVIDERS.md and SECURITY.md (under docs/public in a development
checkout); use WSL.md or CONTAINER.md for the chosen path. Plan briefly, then act.
Pin XDSH/DSH to 0.1.1-rc.2 (Python 0.1.1rc2); never use DSH latest.
Check Python/Node, existing services and ports. Ask only for missing host,
provider/exact model ID, interface language and numeric Telegram allowlist choices.
Install in XDSH-owned paths; preserve other runtimes and keep services on loopback.
Use masked/local secret entry or env references; never print, log or commit keys,
or import another product's credentials. Start a working model gateway first,
then configure the exact DSH runtime command and XDSH.
Run doctor, a tiny CLI task, same-session continuation and an isolated write/read
test. Ask me to send /start and a tiny Telegram task for channel acceptance.
Keep a rollback before replacing services. Report versions, paths, commands,
actual results and blockers. Do not call templates a working deployment, invent
download URLs, silently substitute models, disable safeguards or publish.
```

The agent still needs your authorization and securely supplied credentials.
This is a checklist, not permission to administer unrelated machines.

## Clearing the session list

Each chat supports up to **500 sessions**. `/session` shows 10 per page, opens
the active session's page, and offers previous/next buttons. Browsing pages does
not switch the active session. Existing sessions are preserved on upgrade.

Use **Clear all sessions** in `/session`, or `/session clear-all`, then confirm
within two minutes. This retires this chat's session list and creates one fresh
session with the current model settings, freeing slots under the 500-session cap.
Workspace files, memory and old logs remain; this is not disk erasure.
Active/queued tasks block clearing. Changed session state invalidates confirmation,
and replaying the same confirmation cannot clear newly created sessions.

## Upgrade and rollback

Pin XDSH and DSH versions, run `xdsh-doctor --strict`, and execute the bounded
smoke suite in an isolated root before switching a service. Keep the previous
wheel/image and the XDSH state backup until one real Telegram/CLI task and one
multi-turn continuation pass. If the new adapter fails its contract check,
restore the previous package/runtime pair; no task is automatically replayed.

## What is intentionally not bundled

XDSH does not include provider tokens, Telegram tokens, private host state,
another product's state, model weights or a custom DSH plugin. Media depends on
configured processing services; see [speech setup](SPEECH.md). The package
ships the fourteen XDSH-owned foundational Skills and projects them into the
isolated DSH home without overwriting user files.

See [`SECURITY.md`](SECURITY.md) for the boundary model and
[`examples/telegram.env.example`](examples/telegram.env.example) for
the minimal sanitized configuration surface. Maintainers can find optional
operational settings in the source repository's deployment guide.
The candidate gates are listed in [`RELEASE-MATRIX.md`](RELEASE-MATRIX.md).

## Contributing and public CI

The clean source bundle includes a locked dependency graph, credential-free
core contract tests (the current count is reported by pytest), and a self-contained GitHub
Actions workflow. It checks the stable RC2 identity, event/session/model/plugin/
memory/guard contracts, static types, formatting, the synthetic 429 boundary,
clean re-export, manifest verification, and archive build without production
credentials or host state. Deployment/adversarial fixtures and live tests stay
in the maintainer gate and are not silently treated as public CI coverage.

Read [`CONTRIBUTING.md`](CONTRIBUTING.md) before changing code,
[`SUPPORT.md`](SUPPORT.md) before filing a bug, and
[`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) when participating in project spaces.

## Compatibility and license

Use only the version pairs listed in [`COMPATIBILITY.md`](COMPATIBILITY.md).
How version numbers evolve — including fixes on a published line and the move to
a next aligned line — is defined in [`VERSIONING.md`](VERSIONING.md).
The RC2 archive is an immutable rollback baseline; a later XDSH candidate and
its DSH runtime must pass a separate matrix before deployment.

XDSH is released under the [Apache License 2.0](LICENSE). The DSH runtime,
LiteLLM, and other third-party dependencies remain under their own licenses
and terms.
