# Add a real model connection

XDSH separates **presentation settings** from **executable connections**.
Editing a model label does not create an API route. The optional XDSH-owned
gateway uses LiteLLM to adapt providers while DSH continues to speak Anthropic
Messages. Unknown model names are rejected; no wildcard silently substitutes
another model.

For provider-independent, explicit file/JSON correctness checks, see
[artifact acceptance](ACCEPTANCE.md). Tool success alone is not acceptance.

New installations require an explicit model ID: replace
`XDSH_LITELLM_MODEL=replace-with-exact-model-id` with a route in **your** gateway.
No Volcengine account, maintainer host, or maintainer model is required. Model
names in example catalogs and the historical RC2 validation baseline are examples,
not executable connections or claims about your subscription. Existing configured
models are not changed by setup. Run `xdsh-doctor` after configuring your endpoint,
credential reference, model and runtime; do not start from unedited placeholders.

## Long-task resource limits

### Upstream rate limits

Structured HTTP 429 retries are counted per **consecutive failure episode**,
not across all steps of a long task. A complete tool call or completed text
message resets that episode; heartbeats, usage counters and partial chunks do
not. `XDSH_RATE_LIMIT_MAX_RETRIES` permits that
many retries (default 5), including the last budgeted attempt; further retry
requests stop with partial output retained. RC2 still owns its actual request
retry loop. XDSH does not automatically replay an entire task or completed tools.

Default waits are approximately **5, 10, 20, 40, 80 seconds**, with 10% jitter
(139.5–170.5 seconds total if all five waits occur). Unlike the former 7-second
total, this can cross a minute-based quota reset. This is a bounded opportunity
to recover, not a guarantee: persistent quotas still stop and retain partial
output. A user cancellation interrupts the native wait. Only explicit
`RATE_LIMIT` is eligible; ordinary TIMEOUT, AUTH, QUOTA_EXCEEDED, server errors
and ambiguous transport failures do not enable blind replay. A prior 429 does
not turn a later typed TIMEOUT into evidence of throttling.

The dedicated 429 guard owns that episode's budget; the generic retry guard
still bounds other retry storms and standalone unprotected runners. Silence,
explicit resource limits, and provider limits remain in force. Progress uses
the runtime's scheduled delay when available, not a guessed countdown or a
provider hint that may not have been consumed.

The optional gateway preserves a sanitized numeric `Retry-After` hint on HTTP
429 and structured midstream errors, never the full headers or exception body.
Malformed hints are omitted. This does not add gateway **429** retries. The official
RC2 retry executor honors typed `providerRetryAfterMs` up to its maxDelayMs;
an over-cap hint ends normal recovery instead of retrying too early. However,
the RC2 **pi-ai terminal error-event adapter drops that field**, so the gateway
hint is not guaranteed end-to-end. The fallback window above works without it.
XDSH's route cooldown remains bounded by its configured policy. A persistent account quota cannot be fixed
by looping faster, silently changing models, or raising the task Token budget.

Existing explicit `XDSH_RATE_LIMIT_*` values are **not overwritten** by an
upgrade. To adopt these defaults, remove the old overrides or set MAX_RETRIES=5,
BASE_BACKOFF_SECONDS=5, MAX_BACKOFF_SECONDS=120 and MAX_RETRY_AFTER_SECONDS=120
(each with the `XDSH_RATE_LIMIT_` prefix), then restart at an approved idle
cutover. `/doctor` shows the captured policy; an already running task keeps its
current policy. Do not silently replace a user's shorter retry budget.

### Transient model-step failures

The RC2 composition now retries `TIMEOUT`, `TRANSPORT`, `SERVER` and
`EMPTY_RESPONSE`, in addition to `RATE_LIMIT`, using the official request-retry
executor. This is **within the current model step**, not restarting the user
task. RC2 discards the failed attempt before committing its assistant message
or executing its newly generated tools. Earlier completed tools remain in
history. This boundary was tested with official RC2 and synthetic timeout/EOF
streams, including fully generated tool arguments that must never execute.

Telegram and CLI permit two transient retries per request by default
(`XDSH_GUARD_MAX_TRANSIENT_RETRIES=2`); partial tokens do not reset this bound.
The native total retry ceiling still applies, and may lower that allowance.
The default first two waits are about 5 and 10 seconds with jitter. Limits
reset for a new model step, not for every token; `/limits off` does not disable
fault protection. Authentication, invalid-parameter and cancellation errors
are not automatically retried. The final failure retains partial output.
Failed attempts may consume additional tokens. This is not an exactly-once
guarantee for arbitrary custom runtimes or provider-side tools.

The gateway's `XDSH_PROVIDER_STREAM_IDLE_SECONDS` and runtime's
`XDSH_DSH_STREAM_IDLE_TIMEOUT_SECONDS` are **silence** deadlines, not task
deadlines. Choose values appropriate for the provider; for a gateway using
120 seconds, a runtime window of 150 seconds leaves time to receive the
gateway's explicit failure. Slow local models may require longer settings.
Do not remove silence detection or set infinite retries to mask an outage.

### Optional early stream disconnect recovery

The optional XDSH gateway can recover a narrowly defined early EOF: a valid
Messages stream starts with a short text prefix, then closes without a terminal
event, **before any model output has been sent downstream**. It retains at most
8KiB/64 text characters for at most250ms from the first upstream chunk. Reaching
any boundary commits the original stream; normal output is not buffered to the
end of a long answer.

At most two additional requests use the identical payload and model, after
0.5s and1s. No completed tool or full task is replayed. Explicit error frames
(including429), request exceptions, malformed/unknown events, tool/thinking
blocks, and already committed output do not enter this recovery path. Declared
server-side tools disable it because the provider may already have executed
an action. Exhaustion retains the final partial prefix and reports the original
incomplete-stream condition, rather than inventing successful completion.

`XDSH_PROVIDER_STREAM_START_RETRIES` accepts0,1,2 (**default0**);0 disables this
gateway recovery. Leave it off with RC2 native recovery to avoid multiplying
two retry loops. This independent bound is not raised by `/limits off` and
does not promise recovery from arbitrary late disconnects, lost credentials,
network outages, or provider quotas. Additional model attempts may be billed.
Use the metadata-only gateway recovery log to distinguish recovery from a
first-attempt success; it never logs model text, tools, prompts or secrets.

### Cumulative budgets

Telegram `/limits` opens a per-chat selector. `/limits off` removes cumulative
Token, model-step, tool-count, parallel-subagent and total-runtime ceilings for
new tasks in that chat, including background jobs. `/limits on` restores finite
deployment values, falling back to 120 steps, 160 calls, 2,000,000 tokens and
3 parallel subagents where the deployment uses unlimited values. Total runtime
retains its deployment setting (unlimited by default). Running tasks retain the
policy captured at their start. The preference persists across service restarts.

Operators can set `XDSH_GUARD_MAX_MODEL_STEPS`, `XDSH_GUARD_MAX_TOOL_CALLS`,
`XDSH_GUARD_MAX_TOTAL_TOKENS` and `XDSH_GUARD_MAX_PARALLEL_SUBAGENTS` to
`unlimited` in either CLI or Telegram environments. Positive explicit values
still enforce a budget. Clear `XDSH_TASK_TIMEOUT_SECONDS` and set
`XDSH_GUARD_MAX_RUNTIME_SECONDS=unlimited` to remove legacy total-task deadlines.

This does not remove silence/retry/error protection, sandbox restrictions,
provider quotas or model context/output limits. A single foreground RC2 command
still has a timeout; use the runtime's background-process tools for long
computations. No-resource-budget does not promise unlimited hardware or a task
that survives process termination or Mac sleep. Charges/plan usage may grow
without a cumulative ceiling; choose this mode deliberately.

Operational settings are also local choices. For example,
`XDSH_GUARD_MAX_REPEATED_TOOL_ERRORS` defaults to `3`: three failures of the same
tool and exact arguments trigger protective stopping even if unrelated tools
succeed between them. Counters belong to one task, not all users; a successful
identical call or a structured artifact event resets the relevant failure epoch.
Partial answers are retained. This is an event-level safety bound, not a
pre-execution network firewall or a semantic proof that a tool cannot recover.

## Terminal setup

From the extracted release's source directory, install the optional gateway
extra, then use the interactive terminal wizard (no PyPI publication required):

```bash
python -m pip install '.[gateway]'
xdsh-providers tui
# Equivalent entry from the settings tool:
xdsh-config tui
```

The wizard asks for provider ID, protocol, base URL, exact model ID and an
environment reference (`env:MY_MODEL_API_KEY`) or a masked API key. Confirm the
summary before saving. No credential value is printed. Credentials entered in
the wizard are saved in a separate owner-only environment file; connection
JSON contains only references. Answer `n` or press Ctrl-C to cancel.

Supported connection families:

| Protocol | Intended endpoint family | Important limit |
| --- | --- | --- |
| `anthropic` | Anthropic Messages, including compatible DeepSeek and ARK endpoints | Endpoint must implement Messages |
| `openai` | Chat Completions, custom gateways, vLLM, Ollama `/v1` | Not Responses-only endpoints |
| `gemini` | Gemini through LiteLLM | Requires the provider's API key/base URL |
| `azure:API_VERSION` | Azure OpenAI deployment | Explicit deployment ID and API version required |

This is protocol coverage, not a claim that every model or cloud-specific IAM
flow has passed live tests. Tool, vision and reasoning support still depend on
the model and subscription. HTTPS is required for remote endpoints. Explicit
loopback IPs permit local models; private/LAN and special-use addresses are
rejected. Use a loopback SSH forward for a separately authorized remote local
model. Redirects cannot carry a configured credential to another endpoint.

The four adapters have real-SDK loopback fixture coverage for their request
paths and redirect refusal. Native Messages tool-argument streaming is also
covered. The September 7 live acceptance used ARK's actual Flash deployment;
it is not evidence that every cloud provider or custom endpoint has been
verified. DNS is checked before requests, but the current client does not pin
the resolved IP: only trusted operators should register endpoints. Custom
headers, cloud IAM and Responses-only APIs are not covered by this wizard.

If a host proxy uses fake-IP DNS (for example `198.18.0.0/15`), the gateway
will reject that non-public DNS answer before sending credentials. Configure
real-IP resolution for the trusted provider domain in your deployment; do not
disable SSRF protection. A temporary container host mapping may help isolate
the issue, but it is not durable DNS configuration and can become stale.

## Run the managed gateway

Provide a separate gateway access value in `XDSH_LITELLM_API_KEY` (at least 24
characters), and keep the same reference in the XDSH process environment. Never
put a real key in a command argument. Start:

```bash
export XDSH_PROVIDERS_ROOT="$HOME/.xdsh/config/providers"
xdsh-model-gateway --port 4001
```

The listener binds only `127.0.0.1`. In the CLI/Telegram environment configure
`XDSH_LITELLM_BASE_URL=http://127.0.0.1:4001` and the same
`XDSH_PROVIDERS_ROOT`. For containers, mount one shared provider directory: the
gateway needs read-only access and the trusted settings host needs write
access. Provider environment references must exist in the **gateway** process,
not just in the terminal used to edit settings. Neither service needs another
product's credentials or configuration.

New confirmed connections appear automatically; the gateway does not need a
restart. An existing exact model ID is not overwritten by another registration.

## Telegram setup

In an authorized **private chat**, open `/model` → **Add model connection** or
send `/model add`. Follow the same five fields and choose **Confirm** or
**Cancel**. Setup expires after ten minutes. The confirmation is bound to the
chat/user and is consumed once. Input is handled as configuration, never an
agent task or a durable queued prompt. A typed key may remain in Telegram's
own message history; delete that original message if desired.

After saving, `/model` probes connectivity and lets you select the route.
Saving is not a successful live probe, and a short text probe does not prove
tool support or model identity. Confirm the actual upstream deployment before
comparing model performance. Existing interfaces still work with an externally
managed LiteLLM gateway; `/model add` reports that onboarding is unavailable
unless `XDSH_PROVIDERS_ROOT` is configured.
# Telegram native-provider catalog (RC2)

Use `/model` → **Add model connection**, or `/model add`, to browse a paginated
catalog of the 37 providers exposed by the installed DSH RC2 pi-ai adapter,
plus Volcengine and a local/custom connection path. Configured routes and setup
presets are separate: catalog presence is not an executable model claim.

Choose a preset, enter the **exact upstream model ID**, then an API key or
`env:VARIABLE_NAME`, and explicitly confirm. The common presets prefill provider,
protocol and endpoint. Cloudflare account-specific endpoints and local URLs must
be supplied explicitly. Custom retains protocol/endpoint control, including Azure
Chat Completions with an explicit API version.

31 native-provider entries offer compatible API setup paths. Six are visible but
not bridged: Bedrock (SigV4), Azure Responses, GitHub Copilot, Google Vertex,
OpenAI Codex (OAuth), and Mistral Conversations. OpenAI's preset uses Chat
Completions, **not** native Responses; Responses-only models are not supported by
that preset. Multi-protocol providers may require Custom for the chosen model.
No subscription OAuth credentials are read or imported from DSH.

Telegram onboarding requires `XDSH_PROVIDERS_ROOT` pointing at the same persistent
registry used by the XDSH managed gateway. Merely setting that variable against a
different gateway does not activate new routes. Confirmed additions are loaded
without restarting the gateway. Listing providers makes no LLM requests.

Provider endpoint facts were checked against the installed
`@deepseek-ai/dsh-llm-pi-ai@0.1.1-rc.2` public built-in catalog, not user settings.
Real authentication/model capability remains to be verified with each user's
credentials; only configured/tested routes should be described as verified.
