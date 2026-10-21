# Service-scoped keep-awake / 服务防自动休眠

Native Telegram and channel webhook services can request OS idle-sleep
inhibition for their entire lifetime, including time waiting for new tasks.
This is runner-neutral; it does not change DSH or permanent power settings.

## Enable or disable

New installations: `xdsh-setup --keep-awake on --write-env` records the choice
in the Telegram environment template/file. Interactive terminal installation
asks for consent (more battery usage). Non-interactive installs default to off.
Existing configuration is never overwritten by initialization.
This is an option of `xdsh-setup`, not of the WSL shell installer.

Existing installations: set `XDSH_KEEP_AWAKE=on` or `off` in the service's
owner-only environment file, then restart **when tasks are idle**. For other
channel services, use the same variable in their `--env-file` configuration.
There is no remote Telegram toggle: this is an operator-level power decision.

| Host | Mechanism | Boundary |
| --- | --- | --- |
| macOS | `caffeinate -i -w <service PID>` | Idle sleep only; no display assertion |
| Linux with logind | `systemd-inhibit --what=idle --mode=block` | Requires permission and systemd utility; desktop policy can vary |
| Windows | `SetThreadExecutionState(ES_CONTINUOUS \| ES_SYSTEM_REQUIRED)` | Same service thread restores the previous state on exit |
| Container / WSL | Warning, no guest-side claim of host protection | Configure inhibition on the physical host |

Shutdown releases the request; Unix helpers are tied to parent PID or pipe
EOF so they also release after abrupt service death. Unexpected helper exits
produce a warning. Unsupported systems, missing utilities and permission
denials report `Keep-awake unavailable`, but do not prevent Bot startup.
`Keep-awake requested` is a request, not a promise that hardware cannot sleep.
No shutdown, lid-switch or display-inhibition flags are requested.

This is not autostart, network repair or crash recovery. It cannot guarantee
operation after lid closure, manual suspend, critical battery, shutdown or
power loss. Close/stop the Bot or set the option off when prolonged battery
use is unwanted. Do not grant privileged host power-management access to a
container merely to enable this feature.

For a container, the host administrator can wrap its foreground supervisor
with the corresponding native inhibitor. The lifetime must follow the real
service, not a short-lived `docker start` command. XDSH does not install a
host-wide daemon or modify logind/power profiles automatically.

中文：启用后，Bot 等待消息时也申请防自动空闲休眠；耗电可能增加。退出即释放，
不修改永久电源配置。容器、WSL 需在宿主机配置；合盖、手动休眠及断电不受保证。

References: macOS local `caffeinate(8)`;
[systemd-inhibit](https://www.freedesktop.org/software/systemd/man/250/systemd-inhibit.html);
[Windows SetThreadExecutionState](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-setthreadexecutionstate).
