# Confined container deployment

Container execution is optional; a direct virtual-environment install is the
shortest path for local use. When a task runner is exposed to untrusted input,
run the XDSH service and the official DSH JSON-RPC runtime in an XDSH-owned
container with an explicit resource and filesystem boundary.

## Required boundary

- Build the image from the public XDSH wheel and the separately installed,
  pinned official DSH release. Do not copy a host's shell profile, private
  runtime directory, or another agent's image layers into the build context.
- Pass `XDSH_DSH_RUNTIME_COMMAND` as the exact DSH JSON-RPC argv. Do not
  emulate unsupported host commands through prompt text and do not silently
  fall back to a bundled preview runtime.
- Mount only two XDSH-owned directories: a mode-0700 state directory at
  `/state` and per-session workspaces at `/workspace`. Never mount `/`, the
  host home directory, a Docker socket, SSH keys, or a legacy agent root.
- Keep the image root read-only, drop all Linux capabilities, enable
  `no-new-privileges`, run as a non-root numeric UID/GID, and set finite
  memory, CPU, PID, temporary-storage, and task-guard limits.
- Publish no container port. The first release accepts only a loopback
  LiteLLM URL; if the gateway is outside the container, use a dedicated
  authenticated bridge or narrowly firewalled host gateway. Host networking
  makes every host-loopback service reachable and is not a general sandbox.
- Give Telegram and provider credentials only through a mode-0600 environment
  file or process environment. Keep the file outside the image and never put
  secret values in labels, command lines, logs, manifests, or progress cards.

## Minimal Docker flags

The following is a safety baseline, not a complete image build. Replace the
image and paths with XDSH-owned values and add only the gateway route you have
approved:

```bash
docker run --detach --name xdsh-telegram \
  --user "$(id -u):$(id -g)" \
  --read-only --cap-drop ALL \
  --security-opt no-new-privileges=true \
  --pids-limit 512 --memory 8g --cpus 8 \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=512m,mode=1777 \
  --mount "type=bind,src=$HOME/.xdsh/container-state,dst=/state" \
  --mount "type=bind,src=$HOME/.xdsh/workspaces,dst=/workspace" \
  --env-file "$HOME/.xdsh/config/telegram.env" \
  xdsh-telegram:local
```

Before starting, run `xdsh-doctor --mode telegram --strict --probe-runtime`
against the same environment and run one bounded smoke task in a fresh state,
workspace, session, and `DSH_HOME`. The image must bind no public listener and
must not contain a real `.env` file.

The writable `/tmp` tmpfs is required with `--read-only`, including upgrades
from older writable-root containers that had no tmpfs. Official DSH subprocess
tools create temporary directories there before launching Bash or ripgrep.
Without it, native file tools and heartbeat can work while every subprocess
fails with `EROFS`. Test actual Bash/Python and search-tool execution as well
as heartbeat after cutover; roll back if either fails. Do not solve this by
granting root, disabling confinement, or enabling unrestricted approvals.

## Upgrade and recovery

Keep the previous image and wheel until the candidate passes one task, one
multi-turn continuation, one structured tool call, one cancellation, and the
native-compaction probe. If the adapter contract fails, stop the candidate and
restore the previous package/runtime pair; ambiguous work is never replayed
automatically. The public package does not replace an existing container or
touch any state outside `~/.xdsh`.

The image label reports the aligned XDSH/DSH release line `0.1.1-rc.2`, and the
runtime probe must report DSH `0.1.1-rc.2`. A
different runtime belongs to a separately versioned release and must not be
substituted into this stable image. Rollback removes only the label-verified
candidate container; it never stops, replaces, or edits another service.

The source repository's `deploy/docker/` directory contains host-specific
launchers and is intentionally excluded from the public export because they
expect host-provided artifacts. Adapt their checks only after reviewing the
boundary above; do not copy their credentials or host paths.
# Workspace authority

The default `XDSH_WORKSPACE_SCOPE=session` gives each Telegram session its own
writable directory. An `rw` Docker mount does not override DSH's narrower file
policy: RC2 uses the immutable session cwd as its one writable root.

For a dedicated single-user Bot, an operator may set
`XDSH_WORKSPACE_SCOPE=owner`. Private chats from the sole allowed user then run
with `XDSH_WORKSPACE_ROOT` itself as cwd. All that user's sessions share write
authority over this workspace; old per-session folders remain in place. Relative
paths now start at the shared root. Group chats keep per-session cwd. Multiple
allowed users, broad system roots, and workspaces overlapping service state are
rejected. Keep secrets/state outside the shared workspace; do not mount unrelated
host files there. This is not a multi-tenant isolation mechanism.

Apply this setting only after active tasks finish and restart the Bot. Do not
rewrite existing DSH session headers. XDSH creates its normal fresh runtime
generation on restart. The official `workspace-write` sandbox remains enabled;
this does not grant root, Docker access, host filesystem access, or write access
to `/state`. No custom DSH plugin or `danger-full-access` mode is used.
# Optional public Internet search and reading

Set `XDSH_PUBLIC_WEB_ENABLED=1` to enable public HTTPS search/read without
a search API credential. Enable the authenticated internal MCP bridge using
the existing internal-MCP configuration to expose `web_search` and `web_fetch`
to the agent. This uses the official DSH MCP client, not a custom DSH plugin.
`/search` continues to mean local knowledge search; `/web` can read a public URL.

Privacy: search queries go to Bing's ordinary HTML search endpoint; domain
lookups go to Google DNS-over-HTTPS. Do not include private content in searches.
No cookies or authentication headers are forwarded. An optional
`XDSH_PUBLIC_WEB_PROXY_URL` must be an explicitly configured credential-free
HTTP proxy on literal loopback. Public DNS answers are validated, then TLS is
verified against the original hostname while connecting to the validated IP.
Private, link-local and fake-IP destinations remain blocked, including redirects.

This is a best-effort public HTML backend, not a guaranteed search API. Captchas,
blocked pages and layout changes return an explicit failure; no bypass is used.
For reliable contractual service, configure an authenticated search MCP provider.
The reader bounds response and decompressed bodies to 500 KB, text to 100K
characters and concurrent network workers to two. The caller time budget is
25 seconds; cancelled blocking I/O retains its worker slot until it exits.
Two active readers and at most four pending readers share a five-second queue
wait. The HTTP MCP layer also queues a bounded burst, then reports 429 with
Retry-After instead of starting more work. A batch returns completed evidence
even when another page exceeds the deadline. These are response budgets, not
a claim that an OS-level blocked socket can be forcibly cancelled.

Retrieval policy ranks sources by authority/relevance, then freshness,
completeness, accessibility and cost. These are qualitative priorities, not
calibrated scores. Dependent queries wait for upstream evidence. The batch tool
requires explicit independence, accepts at most four queries and runs at most
two concurrently, retaining partial results. It cannot prove a model's declared
independence: dependency planning and optional next-step suggestions remain
model-followed policy. Repeated/syndicated sources are not independent evidence.

The public `web_fetch` tool accepts `url` or up to four `urls` plus
`independent=true`. `max_bytes` caps the shared returned text budget (default
12000 UTF-8 bytes), not the network body safety limit. Optional `focus` extracts
an exact phrase's vicinity. Provenance, `truncated`, `focus_matched` and failure
classification distinguish missing/partial evidence from evidence of absence.
For JSON, `json_pointer` performs exact-key lookup without changing prefixes or
date semantics. Missing keys return `found=false` plus bounded parent keys;
truncated, duplicate-key or invalid JSON returns `found=null`, never absence.
It cannot be combined with `focus`. This is evidence extraction, not a guarantee
that a model will interpret every result correctly.
Non-retryable fetch failures are cached briefly; changing the output limit is
not permission to repeat a denied request. Transient retries remain bounded
execution guidance, not permission for unlimited automatic retries.

When the Telegram service's authenticated MCP bridge is enabled it also exposes
`model_availability`. It reports only an already configured exact model route.
Metadata is the default. An explicitly authorized `probe=true` spends at most
eight output tokens; a route is cached ten minutes and at most four probes may
start in that window across the host checker. No endpoint, prompt, credential
or default-model override is accepted. A successful probe proves route execution,
not upstream identity, subscription inclusion or quota. User authorization is
an agent policy requirement, not a separate human-approval UI. If no safe
configured route exists, report unconfirmed instead of exploring secret files.

Historical memory is marked as dated context, not fresh evidence. Inline
project Skills are already loaded and are not advertised as native tool names.
Todo cards keep the last reported incomplete state after a successful task;
after 60 seconds without a checklist change they show a stale-checklist note.
No model progress is invented from elapsed time or a final answer alone.
