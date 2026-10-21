# Optional messaging channels

XDSH keeps the Telegram transport as the first-class, polling-based product
surface. The same runner-neutral message contract is available for six
additional official products: five authenticated HTTP webhook adapters plus
the QQ Official Bot outbound WebSocket Gateway. Each transport authenticates
and normalizes provider events before invoking the same `ChannelAgentBridge`.

### Telegram formatting path

Telegram replies use the official `sendMessage` Bot API endpoint directly.
Formatted replies are sent as bounded HTML, with plain-text fallback when a
provider rejects a fragment. Newlines are never replaced with zero-width or
full-width whitespace, and message chunking does not trim either side of a
chunk. A custom relay must expose the standard Bot API methods; XDSH does not
depend on a proprietary `sendRichMessage` endpoint.

| Channel | Inbound | Outbound | Credentials (environment names only) | Live prerequisite |
| --- | --- | --- | --- | --- |
| Discord | signed Interactions POST (Ed25519) | interaction follow-up webhook (or bot-token channel API) | `XDSH_DISCORD_APPLICATION_ID`, `XDSH_DISCORD_PUBLIC_KEY_HEX` | Discord application with an Interactions Endpoint URL |
| WhatsApp | Meta webhook (`hub.challenge`, HMAC-SHA256) | Graph Cloud API text message | `XDSH_WHATSAPP_PHONE_NUMBER_ID`, `XDSH_WHATSAPP_VERIFY_TOKEN`, `XDSH_WHATSAPP_APP_SECRET`, `XDSH_WHATSAPP_ACCESS_TOKEN` | Meta developer app, WABA/phone approval, public HTTPS webhook |
| DingTalk | signed robot callback with `sessionWebhook` | official `oapi.dingtalk.com` session webhook | `XDSH_DINGTALK_SIGNING_SECRET` | DingTalk robot/event product that emits the documented callback shape |
| Feishu/Lark | event subscription (verification token or Encrypt Key signature; optional AES-CBC decryption) | tenant access token + IM API | `XDSH_FEISHU_APP_ID`, `XDSH_FEISHU_APP_SECRET`, `XDSH_FEISHU_VERIFICATION_TOKEN`, optional `XDSH_FEISHU_ENCRYPT_KEY` | published Feishu app, event permission, public HTTPS request URL |
| WeChat Official Account | signed XML callback | customer-service API text message | `XDSH_WECHAT_VERIFICATION_TOKEN`, optional `XDSH_WECHAT_APP_ID`, `XDSH_WECHAT_APP_SECRET` | Official Account server URL and customer-service permission |
| QQ Official Bot | official outbound WebSocket Gateway (heartbeat, reconnect, Resume) | OpenAPI v2 C2C/group text | `XDSH_QQ_APP_ID`, `XDSH_QQ_APP_SECRET`, `XDSH_QQ_ALLOWED_USERS`; optional `XDSH_QQ_ALLOWED_GROUPS` | QQ Open Platform bot with C2C/group event permissions; no public endpoint |

The implementation does not automate WhatsApp Web, WeChat Web, personal QQ,
Discord Gateway sessions, or undocumented DingTalk/Feishu bridges. QQ support
uses the official bot platform, not a logged-in personal account. Enterprise WeChat
(WeCom) is a different protocol and is not silently treated as Official
Account.  These boundaries keep credentials and user data on the explicit
official API paths.

## Check the installation

The check is local and names-only; it does not contact a provider and never
prints a secret value:

```bash
xdsh-channels list
xdsh-channels check --json
xdsh-channels check --channel whatsapp
```

Install the optional cryptography extra before enabling Discord or encrypted
Feishu events. Feishu's verification token is always required for the URL
handshake; add the Encrypt Key when the signed/encrypted callback mode is
enabled:

```bash
python -m pip install '.[channels]'  # from the extracted release source directory
# Or install only the QQ transport dependency:
python -m pip install '.[qq]'
```

The adapters are importable for a web framework or a small HTTP service:

```python
from xdsh.channels import ChannelRouter, build_channel_adapter

adapter = build_channel_adapter("whatsapp")
router = ChannelRouter((adapter,))

async def handle(message):
    # Connect this callback to the same runner-neutral service used by XDSH.
    return await agent_bridge.handle(message)

# A framework endpoint passes method/query/headers/raw_body to:
dispatch = await router.handle(
    "whatsapp", "POST", query=query, headers=headers, body=raw_body,
    message_handler=handle,
)
```

For a single optional channel, the packaged loopback host wires the adapter to
the bounded `ChannelAgentBridge` and acknowledges the webhook before doing
slow model work:

```bash
xdsh-channel-serve --channel discord --port 8787
# Expose only this loopback port through an operator-approved HTTPS tunnel.
```

It requires the same loopback LiteLLM and explicit DSH JSON-RPC environment
used by the CLI, plus that channel's settings. `--check` validates names and
paths without starting DSH or making a network request. The host accepts only
`/webhook/<channel>`, caps request bodies, request headers, and concurrent
turns, and rejects chunked or duplicate-header framing. It binds `127.0.0.1`
unconditionally. For Discord, the deferred acknowledgement is
returned immediately so the platform's short response deadline is respected.
Provider message limits are handled by bounded chunking. Discord can use the
short-lived interaction token by default; setting the optional
`XDSH_DISCORD_BOT_TOKEN` lets long answers use the channel API instead.

## QQ Official Bot (no public tunnel)

QQ is a separate outbound service and is not served by
`xdsh-channel-serve`. Create an official QQ Bot application, grant the required
C2C/group message events, generate the dedicated owner-only template, and set
the three required QQ values plus the runtime/model settings:

```bash
xdsh-qq --init --env-file "$HOME/.xdsh/config/qq.env"
# Edit qq.env locally; reuse the exact runtime/model route of this XDSH install.
xdsh-qq --env-file "$HOME/.xdsh/config/qq.env" --check
xdsh-qq --env-file "$HOME/.xdsh/config/qq.env"
```

Initialization is idempotent, never overwrites an existing file, stores no
real credential, and creates a mode-0600 template. If `--env-file` is omitted
from `--init`, the same `~/.xdsh/config/qq.env` path is used by default.

To avoid manually finding AppID, AppSecret and your OpenID, the packaged
onboarding command uses the official SDK's scan-to-configure flow. It displays
an official binding link, waits up to five minutes, and writes the validated
credentials directly to the private env file. It never prints them. The QQ
account that confirms the scan becomes the explicitly allowed user:

```bash
xdsh-qq --onboard --env-file "$HOME/.xdsh/config/qq.env"
```

Existing QQ credentials are never replaced by onboarding; use a separate file
to bind a different Bot. A failure, expired scan, or concurrent file edit leaves
the credential fields unchanged. No Bot is started by `--init` or `--onboard`.

Reuse the existing instance's model route with explicit `--from-env` arguments.
Only runtime/model fields are imported. Source files must be owner-only. When
these arguments are explicitly supplied, those route fields are updated even
in an existing QQ template; its QQ credentials, allowlists and comments remain
intact. QQ's state, workspaces and DSH_HOME live in a separate `qq/` directory
beside `config/`. For a standard installation (adjust these paths if you chose
a different XDSH root):

```bash
xdsh-qq --init --env-file "$HOME/.xdsh/config/qq.env" \
  --from-env "$HOME/.xdsh/config/cli.env"
xdsh-qq --onboard --env-file "$HOME/.xdsh/config/qq.env"
xdsh-qq --env-file "$HOME/.xdsh/config/qq.env" --check
```

Use the already configured `cli.env` or `telegram.env`; a placeholder template
does not supply a working model route. If your gateway access key lives in a
separate owner-only env file, pass it as a later `--from-env`; later files take
precedence. No Telegram Bot token, user ID, sessions or source state paths are
imported. The model gateway must already be running before the QQ service starts.

`XDSH_QQ_ALLOWED_USERS` is required and an empty list fails closed. A group
message runs only when both its sender is in `XDSH_QQ_ALLOWED_USERS` and its
group is in `XDSH_QQ_ALLOWED_GROUPS`. Plain text is the reliable default;
`XDSH_QQ_MARKDOWN=on` is opt-in for applications that have the corresponding
QQ permission. Answers are redacted and split into bounded 4,000-character
messages. Duplicate deliveries are suppressed, concurrent work is bounded,
and non-secret Gateway Resume state is stored mode 0600 under the configured
XDSH channel state root. The App Secret is resolved only from the environment.
QQ tasks have no XDSH wall-clock timeout by default; an operator can add a
finite `XDSH_CHANNEL_TASK_TIMEOUT_SECONDS` when deployment policy requires it.

The packaged transport pins Tencent's framework-neutral
`qqbot-agent-sdk==1.2.2`; that SDK owns OAuth, heartbeat, reconnect and Resume.
XDSH validates the returned `wss://api.sgroup.qq.com/websocket` endpoint and
rejects endpoint overrides. A live acceptance test remains opt-in because it
requires a separately created QQ sandbox application and credentials.

`ChannelRouter` de-duplicates provider retries by `(channel, message_id)` in a
bounded, expiring in-memory window.  A failed handler removes the entry so a
provider can retry.  The adapter and router do not own DSH sessions or
credentials; `ChannelAgentBridge` creates one hashed, mode-0700 scope per
conversation below the XDSH state/workspace roots.

## Public endpoint and loopback boundary

The five HTTP provider webhooks require a public HTTPS URL. XDSH itself should still bind
only to loopback; expose the endpoint through an operator-approved reverse
proxy or Cloudflare tunnel that forwards to a loopback listener.  Do not bind
the Python process to `0.0.0.0`, mount a Docker socket, or put a provider token
in a user-supplied URL, command line, image, or log.  A provider protocol may
require a short-lived token in its official query URL (for example, WeChat's
API); XDSH constructs that URL only in memory, never logs it, and still applies
an exact HTTPS host allowlist plus bounded request/response sizes.

The current repository includes protocol adapters and hermetic tests.  Live
provider tests are intentionally opt-in: they require a separate test app and
credentials for each platform.  A Telegram token alone cannot activate the
other platforms, and no provider credential is bundled by the public release.

Official protocol references:

- [Discord Interactions](https://docs.discord.com/developers/interactions/receiving-and-responding)
- [WhatsApp webhook verification](https://whatsapp.github.io/WhatsApp-Nodejs-SDK/api-reference/webhooks/start/)
- [DingTalk custom robot](https://open.dingtalk.com/document/orgapp/custom-robot-access)
- [Feishu event subscription](https://open.feishu.cn/document/server-docs/event-subscription-guide/event-subscription-configure-/request-url-configuration)
- [WeChat Official Account server configuration](https://developers.weixin.qq.com/doc/offiaccount/Basic_Information/Access_Overview.html)
- [Tencent QQ Bot Agent SDK](https://github.com/tencent-connect/qqbot-agent-sdk)
- [QQ Bot Open Platform](https://q.qq.com/)
