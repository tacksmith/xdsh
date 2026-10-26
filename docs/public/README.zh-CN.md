# XDSH

笔记本部署可选[服务防自动休眠](KEEP_AWAKE.md)：使用 `xdsh-setup --keep-awake on`，
Bot 运行期间申请保护，退出自动释放；可能增加耗电。容器及 WSL 需宿主机配合。

**面向 DeepSeek Harness（DSH）的安全、与运行时解耦的 Agent 产品层。**

语言： [English](README.md) · [简体中文](README.zh-CN.md) ·
[繁體中文](README.zh-TW.md)

兼容性：[XDSH / DSH 版本矩阵](COMPATIBILITY.zh-CN.md)

## 它是什么、能做什么

XDSH 让你从 Telegram、QQ 官方机器人或终端使用 DeepSeek Harness，并管理任务、会话、
进度和记忆。它不是新模型、DSH 分叉或自定义 DSH 插件。
你可以委派编程与工作区任务、用已配置的检索工具收集资料，或在手机上继续
工作。实际能力取决于模型、启用的工具和授予的权限。

## 亮点与使用价值

| 你的需求 | XDSH 提供的体验 |
| --- | --- |
| 离开电脑也能委派 | Telegram／CLI 会话与模型选择器，QQ 官方机器人文字交互 |
| 看懂正在做什么 | 进度与答案分开、工具聚合、已报告的 Todo 状态 |
| 控制长期知识 | 本地持久记忆、范围化搜索、记忆与 Skill 变更审批 |
| 减少失控消耗 | 上下文保护、重试／Token／停滞守卫与部分结果保留 |
| 使用自己的模型 | TUI／Telegram 接入，多种 API 经自有网关转接 |

这些是组合价值，不宣称每项功能独家或所有任务都更快。自动缩写是保守的
尽力处理；识别到条件或不确定性时保留原文，不保证任意字数限制或答案正确性。

## 部署顺序与前置条件

**安装固定版本 → 启动模型网关 → 配置 XDSH → CLI 验证 → 接上 Telegram
→ 配置常驻服务**。最初建议三个程序放在同一台已授权主机。

| 组件 | 你需要准备 | 验证重点 |
| --- | --- | --- |
| 主机 | Python 3.10–3.13、本机 Node 22.19.0+、npm | macOS 用 Mac 工具；Linux／Ubuntu WSL 用 Linux 工具，不是 node.exe |
| XDSH／DSH | 本版本源码、DSH 0.1.1-rc.2 | Python 包为 0.1.1rc2；不可安装 latest |
| 模型网关 | loopback Messages 路由或 XDSH 自有网关 | 精确模型 ID、凭据；已保存不等于能执行 |
| Telegram | 独立 Bot Token、数字用户白名单 | doctor、/start 和一个真实小任务 |

使用在线 API 不需要本地 GPU；本地推理需另外配置模型服务。Docker 可选，
Bot Token 本身不能执行任务。下载速度影响安装时间，不承诺五分钟完成。

本轮发布验收覆盖 Apple Silicon macOS 和 Linux x86_64 容器。Intel Mac 未通过
全栈验收：`gateway`／`channels` 使用的安全更新版 `cryptography` 已不再支持该
平台，建议改用受支持的 Linux 主机，不要为安装而降级安全依赖。Windows 指引
针对 Ubuntu WSL，并非原生 Windows；详见[验收记录和剩余边界](RELEASE-NOTES-0.1.1-rc.2.md)。

## 为什么开发 XDSH

我们希望保留成熟 Agent 产品的外层体验，同时把内部执行内核替换为官方
DeepSeek Harness。DSH 负责 Agent 的实际运行，XDSH 负责会话、CLI/Telegram
交互、长任务控制、结构化进度、Skill、上下文保护和运维边界。这样既能让 DSH
继续迭代，也不会让用户体验和安全规则绑定到某个私有实现。

XDSH 遵循“先短暂规划，再直接执行”的原则：选择最短的安全路径，限制重试和
资源消耗，任务中断时保留已经得到的部分结果，并在凭据、网络、工作区和资源
边界处默认拒绝。

## 单独使用 DSH，还是配合 XDSH？

DSH 仍可通过自己的 CLI、Web UI 和插件管理器独立使用；XDSH 不分叉、隐藏或
替代这些入口。XDSH 增加的是面向远程工作的产品控制层：命名会话、持久队列和
任务状态、有界重试与上下文保护、需要审批的记忆/Skill 变更、无内容审计和一致
的进度显示。直接在本机工作可以只用 DSH；需要 Telegram/CLI 托管流程与运维
保护时，可以让同一个 DSH 安装由 XDSH 接入。

## 功能概览

- CLI 与 Telegram 交互，支持命名会话和多轮上下文；
- 结构化任务、工具和进度事件，支持取消、队列、插队/续接和有限重试；
- 内置 Skill、自动上下文压缩、模型/Provider 选择、后台任务；
- 受限且需认证的 Web/MCP/browser/media 接口；
- 限定 chat/会话范围、已脱敏的历史检索，以及不显示内容的 `/memory index`
  容量诊断；
- 所有状态位于独立的 `~/.xdsh`，DSH 通过显式运行命令接入；不开发自定义
  DSH 插件，凭据不进入代码或发布包。

## 架构

```text
Telegram / QQ / CLI
      ↓
XDSH 产品层（运行时无关）
      ↓
DshRunner（官方 SDK / JSON-RPC）
      ↓
LiteLLM Anthropic Messages-compatible 网关
      ↓
本地或远程模型
```

DSH 与网关之间仍使用 Anthropic Messages；自有网关可转接 OpenAI Chat
Completions、Gemini、Azure 等 API。这不是 DSH 原生 OpenAI transport，也不代表
每个模型都已验证。详见[模型接入](PROVIDERS.md)。

## 安装固定版本

### 交给 AI agent 部署

把下方提示词贴给能访问本项目和目标主机的 agent：

```text
请在我授权的主机部署此 XDSH 源码版本。先读 README、COMPATIBILITY、
PROVIDERS、SECURITY（开发仓库位于 docs/public），按环境读 WSL 或 CONTAINER。
简要规划后直接执行。固定 XDSH／DSH 0.1.1-rc.2，Python 包0.1.1rc2，
不要安装 DSH latest。检查 Python／Linux Node、现有服务和端口。
只询问缺少的主机、Provider、精确模型 ID、语言与数字用户白名单。
隔离安装到 XDSH 专用目录，保留其他服务；仅绑定 loopback。
凭据使用本地遮罩输入或环境引用，不打印、记录、提交或借用其他产品凭据。
先启动模型网关，再配置精确 DSH runtime 命令及独立 Telegram Bot。
执行 doctor、小型 CLI 任务、同会话续话、隔离文件写入读回；
请我发送 /start 和小任务完成 Telegram 验收。替换服务前保留回滚。
报告版本、路径、命令、真实测试结果和阻塞。不要把模板生成称为部署完成，
不要捏造下载地址、暗换模型、关闭安全保护或擅自发布。
```

agent 仍需要你的授权及安全提供的凭据；这不是管理其他主机的许可。

### 手动安装

先从 GitHub 下载并解压本版本的源码归档，在项目目录中打开终端。
以下命令安装下载的本地版本，不依赖 PyPI 上已发布 XDSH。

声明的 Python 范围为 3.10 至 3.13，公开 CI 配置了这四个版本；有工作流不代表
已执行通过，实际验证组合以带日期的发布证据为准。DSH 本身仍遵循兼容性说明中的
Node/runtime 要求。

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install '.[channels]'
```

交互式安装会提示选择语言，也可显式使用 `--language zh-CN`；非交互安装默认
使用 English。可选语言为繁体中文、简体中文、English、日本語、한국어、
Español、Français、Deutsch、Português、Русский、العربية、हिन्दी 和
Bahasa Indonesia。

macOS 准备原生 Mac Node 22.19.0 或更高版本；Linux／Ubuntu WSL 准备同样最低
版本的 Linux Node。Python、Node 及原生依赖应使用同一平台和架构。官方 DSH
`0.1.1-rc.2` 安装在 XDSH 自己的 npm 前缀，不覆盖系统中的 DSH。

完成上方 Python 包安装后，在 macOS／Linux／Ubuntu WSL 的同一终端执行：
先确认 `node --version` 至少为 22.19.0，使用
`node -p 'process.platform + "/" + process.arch'` 确认其为本机平台，WSL 不使用 Windows Node。
若已有 XDSH 专用 DSH 前缀，先确认版本，不要盲目覆盖。首次安装：

```bash
NODE_BIN="$(command -v node)"
DSH_NPM_ROOT="$HOME/.xdsh/dsh-npm"
mkdir -m 700 -p "$DSH_NPM_ROOT"
npm install --prefix "$DSH_NPM_ROOT" --no-fund --no-audit \
  @deepseek-ai/dsh@0.1.1-rc.2 \
  @deepseek-ai/dsh-sdk-jsonrpc-demo@0.1.1-rc.2 \
  @deepseek-ai/dsh-sdk-jsonrpc-server@0.1.1-rc.2 \
  @deepseek-ai/dsh-agent-spine-demo@0.1.1-rc.2
"$DSH_NPM_ROOT/node_modules/.bin/dsh" --version
DSH_JSONRPC_BIN="$DSH_NPM_ROOT/node_modules/@deepseek-ai/dsh-sdk-jsonrpc-demo/lib/packaged-bin.js"
xdsh-setup --mode both --runtime-command "$NODE_BIN $DSH_JSONRPC_BIN" --language zh-CN --write-env
```

WSL 也可改走 [WSL 安装器](WSL.md)，二选一，不必重复执行。配置工具只创建
目录与模板，不会启动模型、创建 Bot 或替你填好凭据。

只使用 Telegram／CLI 可安装 `pip install .`；QQ 单独使用 `.[qq]`，自有模型网关
使用 `.[gateway]`。`channels` 包含 QQ SDK 及可选渠道的密码学依赖。QQ 的扫码接入、
白名单和能力边界见 [CHANNELS.md](CHANNELS.md)，不承诺与 Telegram 菜单完全等价。
Mac 不运行 WSL 安装脚本；防休眠参数属于 `xdsh-setup`，不是该脚本的参数。

## 配置模型网关

二选一：沿用已存在的 loopback Messages 网关；或安装 `.[gateway]`、运行
`xdsh-providers tui`，按 [PROVIDERS.md](PROVIDERS.md) 启动自有网关。
Provider API Key 与本地网关访问 Key 用途不同，前者必须可由网关进程读取。
先启动网关，再启动 XDSH；不要把示例模型名当成你的可用路由。

在 `~/.xdsh/config/cli.env` 或 `telegram.env` 中设置环境变量；只引用凭据，
不要把密钥写进命令行、代码或 Git：

```text
XDSH_LITELLM_BASE_URL=http://127.0.0.1:4000
XDSH_LITELLM_API_KEY=your-independent-loopback-key
XDSH_LITELLM_MODEL=your-exact-configured-model-id
XDSH_DSH_RUNTIME_COMMAND=/absolute/path/to/the/official/dsh-jsonrpc-command
```

网关必须绑定 loopback。使用 `xdsh-doctor --mode both --strict` 做只读检查。

模型选择器不会把网关的 `/model/info` 当成唯一来源。设置
`XDSH_SETTINGS_PATH` 后，可以在不写入凭据的 `settings.json` 中声明网关
没有列出的企业或本地路由；Provider 别名也可以通过
`XDSH_MODEL_PROVIDER_ALIASES` 设置。显式声明的路由在有界执行探测或真实
任务验证前会显示为未知，探测明确不可用时不会允许选择。该文件只保存
展示和能力元数据，不会创建 LiteLLM 路由或保存密钥；重复 key、非有限数字、
未知字段和疑似凭据的值都会被安全拒绝。例如：

```json
{"schema_version":1,"default_model":"my-company-route",
 "providers":[{"id":"company","label":"Company gateway","icon":"🏢"}],
 "models":[{"id":"my-company-route","provider":"company","tools":true}]}
```
仓库中的 `examples/settings.json.example` 也可以直接作为起点，复制到
`XDSH_SETTINGS_PATH` 指定的位置后只修改路由元数据。

### 不编辑 JSON 也能配置模型

新增真实 API 地址、凭据引用和模型 ID，可使用 `xdsh-providers tui`
（或 `xdsh-config tui`），也可在 Telegram 私聊中使用 `/model add`。
两种入口共用确认式接入流程；新增路由由 XDSH 自有网关自动载入，不会暗中
换成其他模型。安装方法、支持的协议及限制见[模型接入](PROVIDERS.md)。

`xdsh-config` 是 CLI 与 Web 共用的无凭据设置控制面。脚本或终端可以直接使用：

```bash
xdsh-config show
xdsh-config provider-set company --label '企业网关' --icon '🏢'
xdsh-config model-set my-company-route --provider company --tools on
xdsh-config set-default --model my-company-route
xdsh-config validate
```

需要本地设置页时，先把一个临时访问值放入环境变量，再运行
`xdsh-config serve --port 8877`。服务固定绑定 `127.0.0.1`，要求读取
`XDSH_CONFIG_UI_TOKEN` 的 bearer 值，并拒绝跨域、过期版本和超限请求；它不保存
凭据，也不会启动 DSH 或发起模型请求。远程主机只能通过 SSH 本地端口转发访问，
不要公开暴露该端口。`xdsh-setup` 会创建权限为 0600 的空 `settings.json`，
`xdsh-doctor` 会检查其 schema。

### 低 Token 安全并发

搜索和研究任务默认生成最多四个互不重复的查询，并优先使用一次批量搜索调用；
仓库检索在不改变语义时会把相关 pattern 合并成一个有界原生查询。其他情况下，
只有 DSH runtime 或工具定义明确声明 concurrency-safe 的独立只读调用，才会在
同一个模型步骤内并发，最多四个。独立文件/图片读取、多来源 Web fetch、只读
metadata/health check 适合并发；写入、编辑、安装、状态变更、依赖步骤和未声明
安全的工具始终串行。

该策略只使用一个模型回合和一份共享的证据/输出预算，会去重结果，也不会为了
提速而创建 subagent。可用 `XDSH_MAX_PARALLEL_TOOL_CALLS=1..8` 调整上限；安全
默认值为 `4`，`/doctor` 会显示实际值。

### 429 限流保护

所有交互入口都在底层使用同一个限流边界。官方 DSH 的重试策略默认最多重试
5 次（每段连续失败独立计数），约等待 5、10、20、40、80 秒，附加 10% 抖动，
累计名义等待 155 秒，单次上限 120 秒。XDSH 另外限制并发与路由冷却。
RC2 pi-ai 适配器可能丢失 `Retry-After` 提示，参见
[模型限流说明](PROVIDERS.md#upstream-rate-limits)。遇到 429 时不会自动重放整项任务，也不会立即
切换到共享配额的 fallback；达到上限会保留部分答案和进度，并以结构化限流
状态结束。只有确认备用路由使用独立配额时，才设置
`XDSH_RATE_LIMIT_ALLOW_FALLBACK=1`。其余 `XDSH_RATE_LIMIT_*` 参数可按网关
配额调整，`/status` 与 `/doctor` 会显示生效边界。

发布前可运行 `python -m xdsh.ops.rate_limit_probe`；这是无凭据、无网络的
合成探针，会验证 429 有界处理、部分答案保留以及队列准入保护。

## 先验证 CLI

```bash
# 先在本地编辑 cli.env，替换所有占位值；不要贴入公开终端记录。
set -a; . "$HOME/.xdsh/config/cli.env"; set +a
xdsh-doctor --mode cli --strict
xdsh --check
xdsh --prompt 'Reply with exactly READY.'
```

接着进入交互式 `xdsh`，测试同一会话记住／回忆一个测试代码，再在临时
工作区写入并读回小文件。doctor 通过不代表模型或工具已通过实测。

## 使用 Telegram

1. 创建独立的 Telegram Bot，并只把自己的数字用户 ID 加入 allowlist。
2. 复制 `~/.xdsh/config/telegram.env.example` 为 `telegram.env`，填入环境
   变量并执行 `chmod 600`。
3. 运行下方检查与启动命令，再在 Telegram 发送 `/start` 和一个小任务。
4. 确认 `/model`、`/session`、`/language` 与多轮对话正常后，再配置常驻服务。

```bash
xdsh-doctor --mode telegram --env-file "$HOME/.xdsh/config/telegram.env" --strict
xdsh-telegram --env-file "$HOME/.xdsh/config/telegram.env"
```

已有正式 env 文件时直接编辑，不要用模板覆盖。其他平台需要独立官方应用与凭据，
不能共用 Telegram Bot Token；详见 [CHANNELS.md](CHANNELS.md)。

QQ 使用官方机器人平台的出站 WebSocket，不需要公网回调地址，也不会登录个人 QQ。
安装 `.[qq]`（或完整的 `.[channels]`），运行
`xdsh-qq --init --env-file ~/.xdsh/config/qq.env` 生成不会覆盖已有文件的
owner-only 模板；在其中设置 `XDSH_QQ_APP_ID`、`XDSH_QQ_APP_SECRET`、必填的
`XDSH_QQ_ALLOWED_USERS` 及现有 XDSH 的 runtime/model 配置，再用同一路径运行
`xdsh-qq --check` 和启动服务。群消息同时要求发送者和群 OpenID 均在白名单中。
也可以运行 `xdsh-qq --onboard --env-file ~/.xdsh/config/qq.env`，使用 QQ
打开终端显示的官方绑定链接；AppID、AppSecret 和 OpenID 自动保存且不会打印。
安装流程和显式复用模型配置的 `--from-env` 见 [CHANNELS.md](CHANNELS.md)。

随时使用 `/language` 切换当前聊天的语言。选择会持久保存，并刷新该聊天的
37 项 Telegram 命令菜单；除非任务明确要求其他语言，模型也会用所选语言
回答。13 种语言都有原生安装选择、首次导航、帮助页和命令描述；尚未翻译的
少用诊断信息稳定回退到 English，不会回退到中文。

忙碌会话收到新消息时，XDSH 会先持久化消息，再给出五种明确选择：插队并在
   新任务后续接、合并补充后立即续跑、保持 FIFO、替代当前任务、移除消息。提示
   只是建议，最终操作由用户确认；凭据任务不会被自动重放。

### 清空会话列表

每个聊天最多支持 **500 个会话**。`/session` 每页显示 10 个，默认打开当前
会话所在页，支持上一页／下一页。翻页不切换当前会话，升级不删除已有会话。

`/session` 中的“清空全部会话”按钮，或 `/session clear-all`，会先要求二次确认。
确认后移出当前聊天的全部旧会话，创建一个空白会话并保留当前模型设置，
释放500个会话列表的名额。工作区文件、记忆和历史日志保留，不是磁盘数据擦除。
有运行中或排队任务时不能清理；确认两分钟后过期，会话状态变更后也需重新确认。

## DSH 插件管理

DSH 仍可通过自己的 CLI 或 Web UI 独立运行。XDSH 在打开 `/plugins` 时重新
读取官方 profile 的 `dependencies` 与 `dsh.profile.bundles`，因此通过 DSH
安装的插件无需第二套 registry 就会被发现，并在下一回合由新 runtime 载入：

```text
/plugins list
/plugins profiles
/plugins install @scope/package@1.2.3
/plugins remove @scope/package
/plugins sync source-profile [target-profile]
```

安装、移除和同步都会先生成绑定用户的 Telegram 审批卡，再把精确操作委托给
官方 `dsh plugin` CLI。设置 `XDSH_DSH_PLUGIN_COMMAND`（官方 CLI argv）才能
启用变更；未设置时仅提供只读 inventory。`/plugins invoke` 只有在已认证网关
明确声明 `plugin:<package>` 并通过版本化的 `xdsh-plugin-invoke-v1` 握手时才
开放（兼容旧测试适配器仍接受 capability-only 形式）。RC2 的 stdio JSON-RPC
没有直接 Host 调用方法，普通 tool 插件仍由 DSH agent 在任务中选择和调用。
`/plugins` 返回一次性一致快照，并显示稳定的路由代码
(`direct_gateway`、`agent_tool_only`、`direct_unavailable`、`gateway_invalid`)；
直接结果在离开核心边界前会脱敏并限制大小。XDSH 不编辑 DSH manifest、不执行
shell，也不安装自定义 DSH 插件。
交互式 `xdsh` CLI 提供相同的 inventory，并要求在本地安装、移除或同步命令末尾
显式加入 `--approve`。

## Skill 与审批

研究 Skill 指导来源与方法对照、环境预检和如实报告；
[产物验收](ACCEPTANCE.md)检查声明的 JSON 值、比例、输入哈希、数组维度/范围/非退化，
以及指定 Python 参数的实际绑定值。识别到中英文执行型论文复现请求时自动要求检查清单；
普通任务仍可选择显式合同。失败保留部分结果并说明未满足的类别，供后续修复。
`xdsh-verify` 提供只读自检，不等于独立科学验证或全部需求已覆盖，也不把 Todo 完成当成结果正确。
大型原件可显式启用流式 SHA-256、NPY 校验；安装可选 `science` 依赖后，还可检查指定的
HDF5／AnnData 结构与 ID 顺序。用 `xdsh-verify --large-example` 查看示例，
[安装和边界说明](ACCEPTANCE.md#check-large-originals-explicitly)明确区分结构一致性与科学正确性。

XDSH 随包提供有限的基础 Skill，也可以加载当前工作区的项目 Skill，以及按
Telegram 聊天隔离、跨该 Bot 会话和工作区复用的个人 Skill。审阅者可以
提出一个 `SKILL.md` 以及最多 16 个 UTF-8 文本支持文件（单个 64 KiB、总计
256 KiB）；路径必须保持在 Skill 目录内。隐藏文件、符号链接、二进制文件、含有
凭据或指令注入的内容都会被拒绝。支持文件会纳入有界的 before/proposed/applied
树哈希和 diff，但仅展示 Skill 时不会把文件内容注入任务提示。

所有演化或归档候选都先保持 pending，只有操作员明确选择项目或个人库范围并批准后
才会应用。Telegram 会提供两个范围按钮；个人 Skill 存放在 XDSH 管理的状态目录，
不会写入源码，并在复用前校验哈希。评估在隔离环境中只读执行；哈希过期、缺失文件
（不代表删除）以及校验失败都会安全拒绝。已批准的变更可在再次校验哈希后回滚。
项目级命令由 CLI 与 Telegram 共用；个人库目前通过 Telegram 管理：

```text
/skills pending | diff ID | evaluate ID | approve ID | reject ID | rollback ID
/skills approve-personal ID | personal
/skills curate
/skills archive NAME
/skills archive diff ID | approve ID | reject ID | rollback ID
```

Curator 只根据观测指标给出人工 review/archive 建议，不会自动合并或自我修改。

## 记忆与会话检索

成功回合经过脱敏后，按每个会话和每个 chat 的固定上限保留。`/search` 使用
限定范围的 SQLite FTS5：先匹配严格 AND，再只从有限候选中补充部分匹配和拼写
近似，并采用确定性排序。已批准的 `MEMORY` 事实使用同样有界的词法容错，稳定
的 `USER` 偏好则保持加载。`/memory index` 只显示回合数、保留上限和检索模式，
不会显示存储内容。默认模式不需要外部 embedding 服务，也不会暗中发起网络
请求。运维者可以显式配置一个经过认证、仅限 HTTP loopback、兼容 OpenAI 形状
的 embedding 端点；混合检索仍会先脱敏、严格限制在当前 chat/会话并设置资源
上限，只保存标准化 float32 向量，端点故障时自动退回词法检索。

## 版本与边界

RC2 固定配对是 XDSH／DSH `0.1.1-rc.2`，协议 SDK 为 `0.1.1rc1`；
模型不绑定特定供应商，请配置自己的精确路由。旧的 `0.1.0a0-rc2` 回滚证据由维护者在公开源码包之外
以不可变方式保留；后续候选版本使用独立的状态目录、镜像和运行时，不覆盖
该证据。当前发布候选还包含排队续接优先级稳定化和
`/queue insert` 别名。
当前稳定 RC2 使用与 DSH 对齐的版本线：XDSH 与 DSH 都是
`0.1.1-rc.2`。Python 包按 PEP 440 写作 `0.1.1rc2`，两种写法代表同一
个预发布版本；协议客户端 `deepseek-harness-sdk==0.1.1rc1` 仍单独维护。
旧的 `0.1.0a0-rc2` 只作为公开源码包之外的不可变历史证据保留，不再作为当前安装版本。
详见 [RC2 发布说明](RELEASE-NOTES-0.1.1-rc.2.md)。

XDSH 不打包 DSH runtime、Provider 凭据或生产状态。所有部署状态默认位于
`~/.xdsh`，服务只绑定 loopback。运行前请阅读 [安全说明](SECURITY.md) 和
[容器说明](CONTAINER.md)。

## 开发与验证

```bash
uv sync --all-groups
python -m pytest -q
ruff check .
mypy src tests
xdsh-release export --destination /tmp/xdsh-public
xdsh-release audit /tmp/xdsh-public --strict
xdsh-release verify /tmp/xdsh-public
```

发布包会包含 Apache-2.0 许可证和 `NOTICE`，但不会包含凭据、运行时状态或
特定主机配置。干净源码包还包含锁定依赖、无凭据核心契约测试（数量以当前执行结果为准）和独立 GitHub Actions 工作流；部署/对抗夹具与真实服务
测试仍由维护者发布门执行，不会被误称为公开 CI 覆盖。

参与开发前请读 [CONTRIBUTING.md](CONTRIBUTING.md)，提交问题前请读
[SUPPORT.md](SUPPORT.md)，参与社区时请遵守
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。完整发布矩阵见
[RELEASE-MATRIX.md](RELEASE-MATRIX.md)。

## 许可证

XDSH 采用 [Apache License 2.0](../../LICENSE)。DSH runtime、LiteLLM 和其他
第三方依赖仍按各自许可证和条款提供。
