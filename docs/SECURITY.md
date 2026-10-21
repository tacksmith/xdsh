# XDSH security boundary

## Credentials

- Tokens and API keys are read only from environment variables.
- The setup command writes placeholders, never real credentials.
- Environment files must be regular owner-only files (`0600` or stricter).
- The dotenv reader caps each file at 64 KiB, 512 lines, and 128 entries;
  oversized, non-UTF-8, NUL-containing, or overlong values fail closed.
- Diagnostic and release commands redact values and do not include them in
  reports.
- Telegram foreground tasks that contain a recognized credential shape pause
  for one inline confirmation. The ticket is short-lived, bound to the
  originating chat and user, one-use, and held only in process memory. After
  approval the runner receives the original task, while the XDSH durable
  ledger, session history, knowledge index, progress outbox, and reviewer
  seams receive only a redacted copy. Reject, expiry, shutdown, or restart
  drops the original. Durable background and scheduled jobs still reject
  credential-shaped task bodies; use environment references for those flows.

## Filesystem and runtime isolation

- Default state is below `~/.xdsh`; reserved legacy/DSH roots, the home
  directory, and the filesystem root are rejected as XDSH roots.
- DSH session state and XDSH product state use separate directories.
- The official DSH JSON-RPC command is explicit and is never emulated by
  sending unsupported Host commands as prompt text.
- Public release export excludes handoff notes, host artifacts, caches, and
  credential-bearing environment files.
- The optional container profile is documented in `CONTAINER.md`; it uses a
  read-only image, dropped capabilities, finite resources, and only XDSH-owned
  state/workspace mounts.

## Network

The first release accepts only a loopback LiteLLM base URL. Telegram traffic may
use an explicitly configured proxy. The optional settings UI hard-binds IPv4
loopback, requires a 24-character-or-longer bearer value from an environment
variable, validates Host and Origin, bounds body/concurrency/time, and uses
optimistic revisions to reject stale writes. It stores only the credential-free
model schema and must be reached remotely through an operator-controlled local
forward, never a public bind. Other authenticated Web/MCP/browser seams remain
disabled until their endpoint and allowlists are configured; optional internal
MCP also binds to loopback only.
Optional semantic retrieval follows a narrower rule: it accepts only an HTTP
loopback embeddings endpoint, requires the credential by environment-variable
reference, redacts and bounds every input, rejects redirects/MIME or schema
drift/non-finite vectors, caps dimensions and response bytes, writes only
normalized float32 vectors, and falls back to lexical retrieval on outage.

## Tasks and recovery

Runner-neutral guards bound model steps, tool calls, repeated failures, true
event silence, optional operator-selected runtime duration, and token usage.
Structured tool-argument deltas refresh liveness through a content-free event;
the argument fragments are never rendered or persisted. Healthy tasks have no
default wall-clock ceiling, while silent tasks still warn and stop on the
configured inactivity deadline. Cancellation, retry, rollback, and replay of
potentially side-effecting work are explicit operations. Partial output is
preserved, but ambiguous side effects are never replayed automatically.

## Dependency maintenance

The fixed DSH RC2 compatibility line does not freeze vulnerable Python
dependencies. The managed gateway pins a tested patched LiteLLM release and
declares security floors for affected dependencies, including independent
`gateway`, `channels`, and `qq` installations. `uv.lock` reproduces the tested
development graph; wheel metadata carries the floors for pip consumers.
Upgrade pip before installation and re-audit dependencies periodically. A clean
dated advisory scan is not a guarantee against undisclosed vulnerabilities.

Cryptography 49+ dropped Intel macOS support. Do not bypass the security floor
to install the complete gateway/channels stack on that platform; use a supported
host. See the [upstream changelog](https://cryptography.io/en/latest/changelog/#v49-0-0)
and the platform limits in the release notes.

## Reporting a problem

For a suspected vulnerability, use **Security → Advisories → Report a
vulnerability** in the official GitHub repository when private reporting is
enabled. Do not put credentials or exploit details in a public issue. If no
private channel is listed, open only a minimal contact request without sensitive
details. Before publishing, maintainers must enable private vulnerability
reporting or publish an alternative private contact in this file.

Include the XDSH version, DSH version, adapter contract, command exit code, and
the relevant `xdsh-doctor --json` checks. Remove prompts, answers, paths,
tokens, and model payloads before sharing a report.
