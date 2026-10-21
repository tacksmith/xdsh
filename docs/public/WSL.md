# WSL installation and field test

The Mac is a build host; the runtime can be installed entirely inside a WSL
distribution.  Run the script from a clean XDSH checkout or public export:

```bash
bash scripts/wsl_install.sh --source "$PWD"
```

On an interactive terminal, setup asks for one of thirteen interface languages.
For unattended installation, pass an explicit code such as `--language en`
(`zh-TW`, `zh-CN`, `en`, `ja`, `ko`, `es`, `fr`, `de`, `pt-BR`, `ru`, `ar`,
`hi`, and `id` are supported); otherwise the non-interactive default is English.

The script performs these bounded actions (including the optional `channels`
extra so Discord signatures and encrypted Feishu callbacks work after a clean
install):

1. Confirms it is running in WSL and validates a dedicated `~/.xdsh` root.
2. Checks `python3`, Linux `node` **22.19.0 or newer**, `npm`, and the existing
   `dsh --version` when present.  A Windows interop `node.exe` is rejected so
   the official JSON-RPC process cannot accidentally run with the wrong
   platform runtime.  An existing system DSH is never overwritten.  The requested pair
   is installed under the XDSH-owned `~/.xdsh/dsh-npm` prefix; refreshing that
   prefix requires the explicit `--upgrade-dsh` flag.  Peer dependencies stay
   enabled, and the one-shot Node heap is sized from available WSL memory so
   the selected DSH dependency graph does not fail at the default 2 GiB limit.
3. Installs the exact aligned XDSH `0.1.1-rc.2` / DSH `0.1.1-rc.2` pairing under the XDSH-owned prefix,
   including the JSON-RPC demo, server, and agent-spine packages required by
   the RC2 composition. The installer rejects floating and different DSH
   versions. If an earlier XDSH install has only the headline package, a rerun
   adds the required official closure without touching an existing system DSH.
4. Creates `~/.xdsh/venv`, installs XDSH `0.1.1rc2` (including the channel crypto extra), writes only placeholder environment
   templates, selects the interface language, and runs the normal setup entry point.

The installer also creates the owner-only convenience wrapper
`~/.xdsh/bin/dsh`. It pins the validated Linux Node path and the isolated DSH
binary, so `~/.xdsh/bin/dsh --version` works even when the WSL shell inherited
Windows `node.exe`; it does not modify system Node or shell startup files. Add
`export PATH="$HOME/.xdsh/bin:$PATH"` to the current shell only if you want to
type `dsh` directly.

No Telegram bot is created automatically.  After the script completes, create
an independent bot with BotFather, copy and edit
`~/.xdsh/config/telegram.env`, set mode `0600`, and run:

```bash
~/.xdsh/venv/bin/xdsh-doctor --mode telegram \
  --env-file ~/.xdsh/config/telegram.env
~/.xdsh/venv/bin/xdsh-telegram --env-file ~/.xdsh/config/telegram.env
```

For the official `packaged-bin.js` runtime, `xdsh-doctor --probe-runtime`
understands the launcher’s usage-only `--version` response and verifies the
adjacent DSH package manifest. A strict doctor run is therefore safe after the
environment file is populated.

The setup also creates the placeholder-only
`~/.xdsh/config/channels.env.example`. Copy it to `channels.env` only when
enabling a separate official Discord, WhatsApp, DingTalk, Feishu, WeChat, or
QQ Bot application. The first five require webhook registration; QQ uses an
outbound WebSocket and therefore needs no public callback URL.

The file must provide the Telegram token and allowlisted numeric user IDs,
the loopback LiteLLM URL/key/model, and the exact official DSH JSON-RPC argv.
The installer writes the detected absolute Linux Node path into the generated
example, so a non-login service does not depend on a user-shell `PATH`.  The
minimum is Node `22.19.0`, required by the official DSH `pi-ai` dependency.  If an
earlier XDSH run already placed the validated binary under
`~/.xdsh/node-download/node-v22.19.0-linux-x64`, the installer reuses it even
when WSL currently resolves `node.exe`.  Otherwise install a Linux Node release
and prepend its `bin` directory to `PATH` (or set `XDSH_NODE_BIN`) before
running the installer; the script fails closed instead of mixing Windows npm
with Linux packages.  The
DSH runtime and LiteLLM/model route are required in addition to the bot token;
a bot token by itself cannot execute an agent task.  Credentials are never
command-line arguments and are not read by the installer.

After the Telegram smoke test, an optional platform can use the loopback
webhook host, for example `~/.xdsh/venv/bin/xdsh-channel-serve --channel
discord --port 8787`. Configure that platform's official application and
forward only `127.0.0.1:8787` through an approved HTTPS tunnel; Telegram and
the optional channels use separate credentials and webhook registrations.
For QQ, first run `~/.xdsh/venv/bin/xdsh-qq --init --env-file
~/.xdsh/config/qq.env`, fill that owner-only template locally, then use the
same path with `xdsh-qq --check` and `xdsh-qq`; do not put QQ through the
webhook host or an HTTPS tunnel.

Use `--dry-run` to inspect the bounded command sequence without writing files.
Use `--root /some/dedicated/path` only when that path is XDSH-owned.  The
script rejects reserved legacy/DSH roots, the home directory itself, and `/`.

## Container option

WSL can run the same XDSH process in a confined Docker container if Docker
Desktop/Engine is deliberately enabled.  Follow [`CONTAINER.md`](CONTAINER.md):
mount only XDSH-owned state and workspaces, run as a non-root UID, drop
capabilities, keep the image read-only, and publish no port.  A container is
not required for a first WSL smoke test; it is a separate security decision.
