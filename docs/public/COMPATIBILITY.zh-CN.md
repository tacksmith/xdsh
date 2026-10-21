# XDSH / DSH 兼容性说明

语言：[English](COMPATIBILITY.md) · [简体中文](COMPATIBILITY.zh-CN.md) ·
[繁體中文](COMPATIBILITY.zh-TW.md)

XDSH 与 DSH 必须按**经过验证的版本组合**使用。第一次公开发布只有一个受支持
组合，不能把它与最新 DSH 随意混装。

## 已验证组合

| XDSH 发布标识 | Python 包 | DSH runtime | Python SDK | 启动器 | 状态 |
| --- | --- | --- | --- | --- | --- |
| `xdsh-0.1.1-rc.2` | `0.1.1rc2` | `0.1.1-rc.2` | `0.1.1rc1` | `dsh-sdk-jsonrpc-demo/lib/packaged-bin.js` | **当前对齐的稳定公开版** |
| `xdsh-0.1.0a0-rc2` | `0.1.0a0` | `0.1.1-rc.2` | `0.1.1rc1` | `dsh-sdk-jsonrpc-demo/lib/packaged-bin.js` | **不可变历史归档** |

该组合使用首版 LiteLLM Anthropic Messages-compatible loopback 链路。历史验证模型
为 `ark-code-latest`；新安装须明确填写自己的模型 ID，不要求火山引擎账户。
DSH runtime 不打包进 Python 包；安装器会把精确固定的
runtime 安装到 XDSH 自己的 `~/.xdsh` 前缀，并拒绝其他版本。

稳定组合也支持通过 `/plugins` 只读发现 DSH profile 插件：XDSH 读取官方
`dependencies` 与 `dsh.profile.bundles`，不会改写 profile。只有显式配置官方
CLI argv（`XDSH_DSH_PLUGIN_COMMAND`）后，安装、移除和同步才会进入审批流程；
RC2 stdio JSON-RPC 没有直接 Host 插件调用方法。通过 DSH Web/CLI 独立修改的
插件会在下一次 XDSH 任务前被检测，并由新的 runtime 载入。
可选的直接调用网关必须声明版本化的 `xdsh-plugin-invoke-v1` 契约和准确的
`plugin:<package>` 能力，否则调用会安全拒绝。Inventory 使用稳定路由代码，
并对直接结果脱敏和限长，因此“已供 agent 安装”不会被误报为“外层可直接调用”。

项目 Skill 演化是 XDSH 外层的兼容层，不是 DSH Host 扩展。它只接受有界的 UTF-8
支持文件（最多 16 个、单个 64 KiB、总计 256 KiB），并将其绑定到
before/proposed/applied 树哈希；必须先隔离评估，再由操作员明确批准。缺少文件不
代表删除；过期、符号链接、隐藏、二进制、含凭据或指令注入的资源都会安全拒绝。
Curator/归档建议只供人工审核，已批准的变更支持经过哈希校验的回滚。这些控制与
列出的 RC2 runtime 兼容，但不会虚构协议未提供的原生 Host-only 命令。

当前对齐版本的发布标识为 `xdsh-0.1.1-rc.2`，Python 包按 PEP 440 写作
`0.1.1rc2`；维护者在公开源码包之外保留不可变的 `0.1.0a0-rc2` 证据归档，包含原始五种忙碌会话
选择：插队并保存续接、同会话补充、正常 FIFO、替代、移除。历史哈希不会被
覆盖；Apache-2.0 正式候选会单独重建，并在上传前生成自己的校验值。

## 运行时规则

- 本版只使用官方 RC2 JSON-RPC demo 闭包和显式 Cordis 命令。
- runtime manifest、安装器、镜像标签与 doctor 探测都必须报告
  `0.1.1-rc.2`。
- 不要把本版指向 profile 启动器或浮动的 `latest` 标签。
- 每个隔离的 `DSH_HOME` 同时只允许一个存活的 XDSH runner，通过操作系统
  advisory lock 做资源互斥。不同实例必须使用不同的 `DSH_HOME`；进程崩溃后
  锁由操作系统释放。这是宿主资源隔离，不是 DSH 原生 generation、续接或取消。
- XDSH checkpoint 可选记录不透明的宿主进程标记和内部 session generation，
  以及每次 runtime 启动的随机标记。规范化事件携带同一标记；适配器先拒绝来源
  标记缺失或不匹配的通知，再检查序号。即使 DSH 会话 ID 未变，更换 runtime
  也会产生新标记。这用于本地事件隔离与回滚/恢复来源诊断；它不改变 DSH wire，
  也不把 RC2 变成原生跨进程
  续接/取消协议。旧 checkpoint 在没有该字段时仍然有效。
- 后续 DSH 版本必须配套新的 XDSH 版本、隔离状态及独立兼容性矩阵。

## 升级步骤

1. 记录当前 XDSH/DSH 版本，只备份 `~/.xdsh` 下的 XDSH 状态。
2. 在独立虚拟环境和独立 `~/.xdsh` runtime 前缀中安装候选。
3. 运行 `xdsh-doctor --mode both --strict --probe-runtime` 和完整 hermetic 测试。
4. 验证单任务、多轮续话、结构化工具调用和进程级取消；Telegram 候选使用
   独立 Bot、独立状态，并手动验证命令和任务；内部 `xdsh-canary` 工具不包含在公开包中。
5. 只有矩阵和回滚检查都通过才切换；失败时恢复原组合，不自动重放不明任务。

兼容表示启动器和 SDK 满足 `dsh-sdk-jsonrpc-session-events-v1` 契约，并通过
事件、会话和取消测试；不表示所有 DSH Host-only 命令或未来 Provider 功能都
自动可用。XDSH 采用 Apache-2.0，DSH runtime 与第三方服务仍按各自条款提供。
