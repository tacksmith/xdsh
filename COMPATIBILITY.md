# XDSH / DSH compatibility

Language: [English](COMPATIBILITY.md) · [简体中文](COMPATIBILITY.zh-CN.md) ·
[繁體中文](COMPATIBILITY.zh-TW.md)

XDSH and DSH are released as a **tested pair**. The first public release has
one supported pair only. Do not combine it with the newest DSH package and
assume compatibility.

## Validated pairings

| XDSH release | Python package | DSH runtime | Python SDK | Launcher | Status |
| --- | --- | --- | --- | --- | --- |
| `xdsh-0.1.1-rc.2` | `0.1.1rc2` | `0.1.1-rc.2` | `0.1.1rc1` | `dsh-sdk-jsonrpc-demo/lib/packaged-bin.js` | **Active aligned stable publication** |
| `xdsh-0.1.0a0-rc2` | `0.1.0a0` | `0.1.1-rc.2` | `0.1.1rc1` | `dsh-sdk-jsonrpc-demo/lib/packaged-bin.js` | **Immutable historical archive** |

This pair uses the first-release LiteLLM Anthropic Messages-compatible
loopback transport. Its historical baseline used `ark-code-latest`; fresh installs
require your own explicit model ID, not a subscription to that provider. The DSH runtime is not
bundled in the Python package; the installer puts the exact pinned runtime
under the XDSH-owned `~/.xdsh` prefix and rejects every other version.

The stable pair also supports read-only discovery of DSH profile plugins through
`/plugins`: XDSH reads `dependencies` and `dsh.profile.bundles` without changing
the profile. Approval-gated install/remove/sync is available only when an
explicit official `XDSH_DSH_PLUGIN_COMMAND` is configured; the RC2 stdio
JSON-RPC surface has no direct Host-plugin invocation method. Plugin mutations
made independently through DSH Web/CLI are detected on the next XDSH turn and
cause a fresh runtime to load the changed profile.
An optional direct gateway must advertise the versioned
`xdsh-plugin-invoke-v1` contract and the exact `plugin:<package>` capability;
otherwise invocation fails closed. Inventory diagnostics use stable route
codes and cap/redact direct results, so “installed for the agent” is not
mistaken for “directly callable by the shell.”

Project Skill evolution is an XDSH-side compatibility layer, not a DSH Host
extension. It accepts only bounded UTF-8 support resources (16 files maximum,
64 KiB each, 256 KiB total), binds them into before/proposed/applied tree
hashes, and requires isolated evaluation plus explicit approval. Omitted files
never mean deletion; stale, symlinked, hidden, binary, credential-bearing, or
instruction-injection resources fail closed. Curator/archive suggestions are
advisory, and approved changes have a hash-verified rollback path. These
controls work with the listed RC2 runtime but do not imply native Host-only
commands that the protocol does not expose.

The active aligned artifacts use the `xdsh-0.1.1-rc.2` release tag and the
Python package filename spelling `0.1.1rc2`. Maintainers keep the historical
`0.1.0a0-rc2` evidence outside this public source bundle and never mutate it.
It contains the original five busy-chat choices (interrupt/checkpoint-resume,
same-session follow-up, FIFO, replace, and drop). Its historical hashes remain
unchanged; the Apache-2.0 publication candidate is rebuilt separately and gets
its own checksums before upload.

## Protocol contract

The adapter contract is `dsh-sdk-jsonrpc-session-events-v1`. XDSH consumes the
official SDK/JSON-RPC surface and normalizes notifications into runner-neutral
events. Additive optional fields are tolerated; missing required operations fail
closed with `DSH_SDK_INCOMPATIBLE` before a task is submitted. XDSH never infers
typed events by scraping stdout.

## Runtime-shape rules

- This release uses the official RC2 JSON-RPC demo closure and its explicit
  Cordis command.
- The runtime manifest, installer, image label, and doctor probe must all
  report `0.1.1-rc.2`.
- Do not point this release at a profile launcher or floating `latest` tag.
- One live XDSH runner owns an isolated `DSH_HOME` at a time through an
  operating-system advisory lock. Run separate instances with separate
  `DSH_HOME` roots; the lock is released by the OS after a process crash. This
  is host resource fencing, not native DSH generation, resume, or cancellation.
- XDSH checkpoints may record an opaque host-process marker plus the internal
  session generation and runtime-launch nonce. Normalized runtime events carry
  the same marker; the adapter rejects missing or mismatched notification
  provenance before applying sequence checks. Within one normalized run,
  only strictly increasing DSH event sequence numbers are accepted, so replayed
  or same-sequence conflicting notifications cannot be fanned out twice. This
  in-memory watermark is not a durable cursor across turns or processes.
  A replacement runtime receives
  a new nonce even when its DSH session ID is unchanged. This is provenance
  and local event fencing for rollback/recovery diagnostics;
  it is optional, does not alter the DSH wire protocol, and does not turn RC2
  into a native cross-process resume/cancel protocol. Older checkpoints remain
  valid because the field is omitted when unavailable.
- A later DSH line requires a separately versioned XDSH release, isolated
  `DSH_HOME`, sessions, workspaces, image, and compatibility matrix.

## Upgrade procedure

1. Record the current XDSH and DSH versions and back up only the XDSH-owned
   state under `~/.xdsh`.
2. Install the candidate pair in a separate virtual environment and a separate
   `~/.xdsh` runtime prefix.
3. Run `xdsh-doctor --mode both --strict --probe-runtime` and the hermetic test
   suite.
4. Run the four isolated smoke cases: one task, multi-turn continuation,
   structured tool call, and process-scoped cancellation. For a Telegram
   candidate, use a separate Bot, independent state and a manual command/task
   smoke; the internal `xdsh-canary` tool is not shipped in this public profile.
5. Switch only after the candidate matrix and rollback check pass. On failure,
   restore the previous pair; do not replay an ambiguous task automatically.

## What “compatible” means

Compatibility means that the listed DSH launcher and SDK satisfy the XDSH
adapter contract and the tested event/session/cancellation behavior. It does
not mean that every DSH Host-only command, native cross-process resume method,
or future provider feature is available. Such capabilities are listed
explicitly in the feature-parity table and must not be inferred from a version
number alone.

The package metadata declares the XDSH license as Apache-2.0. DSH, its runtime
packages, and provider services remain separately licensed and versioned.
# Gateway adapter name

The default adapter is named `litellm`, independent of the upstream model vendor.
The old `litellm-anthropic` identifier remains a compatibility alias; Telegram
normalizes that old setting to `litellm`. This naming change does not change the
internal Anthropic Messages-compatible wire protocol or upstream model support.
