# XDSH

筆電部署可選[服務防自動休眠](KEEP_AWAKE.md)：使用 `xdsh-setup --keep-awake on`，
Bot 執行期間申請保護，退出自動釋放；可能增加耗電。容器及 WSL 需宿主機配合。

**面向 DeepSeek Harness（DSH）的安全、與執行時解耦的 Agent 產品層。**

語言： [English](README.md) · [简体中文](README.zh-CN.md) ·
[繁體中文](README.zh-TW.md)

相容性：[XDSH / DSH 版本矩陣](COMPATIBILITY.zh-TW.md)

## 它是什麼、能做什麼

XDSH 讓你從 Telegram、QQ 官方機器人或終端使用 DeepSeek Harness，並管理任務、工作階段、
進度與記憶。它不是新模型、DSH 分叉或自訂 DSH 插件。
你可以委派程式開發與工作區任務、用已設定的檢索工具收集資料，或在手機上
接續工作。實際能力取決於模型、啟用的工具與授予的權限。

## 亮點與使用價值

| 你的需求 | XDSH 提供的體驗 |
| --- | --- |
| 離開電腦也能委派 | Telegram／CLI 工作階段與模型選擇器，QQ 官方機器人文字互動 |
| 看懂正在做什麼 | 進度與答案分開、工具聚合、已回報的 Todo 狀態 |
| 控制長期知識 | 本機持久記憶、範圍化搜尋、記憶與 Skill 變更審批 |
| 減少失控消耗 | 上下文保護、重試／Token／停滯守衛與部分結果保留 |
| 使用自己的模型 | TUI／Telegram 接入，多種 API 經自有閘道轉接 |

這些是組合價值，不宣稱每項功能獨家或所有任務都更快。自動縮寫是保守的
盡力處理；辨識到條件或不確定性時保留原文，不保證任意字數限制或答案正確性。

## 部署順序與前置條件

**安裝固定版本 → 啟動模型閘道 → 設定 XDSH → CLI 驗證 → 接上 Telegram
→ 設定常駐服務**。最初建議三個程式放在同一台已授權主機。

| 元件 | 你需要準備 | 驗證重點 |
| --- | --- | --- |
| 主機 | Python 3.10–3.13、本機 Node 22.19.0+、npm | macOS 用 Mac 工具；Linux／Ubuntu WSL 用 Linux 工具，不是 node.exe |
| XDSH／DSH | 本版本原始碼、DSH 0.1.1-rc.2 | Python 套件為 0.1.1rc2；不可安裝 latest |
| 模型閘道 | loopback Messages 路由或 XDSH 自有閘道 | 精確模型 ID、憑據；已儲存不等於能執行 |
| Telegram | 獨立 Bot Token、數字使用者白名單 | doctor、/start 和一個真實小任務 |

使用線上 API 不需要本機 GPU；本機推論需另外配置模型服務。Docker 可選，
Bot Token 本身不能執行任務。下載速度影響安裝時間，不承諾五分鐘完成。

本輪發布驗收涵蓋 Apple Silicon macOS 和 Linux x86_64 容器。Intel Mac 未通過
全棧驗收：`gateway`／`channels` 使用的安全更新版 `cryptography` 已不再支援該
平台，建議改用受支援的 Linux 主機，不要為安裝而降級安全依賴。Windows 指引
針對 Ubuntu WSL，並非原生 Windows；詳見[驗收紀錄和剩餘邊界](RELEASE-NOTES-0.1.1-rc.2.md)。

## 為什麼開發 XDSH

我們希望保留成熟 Agent 產品的外層體驗，同時把內部執行核心替換為官方
DeepSeek Harness。DSH 負責 Agent 的實際執行，XDSH 負責工作階段、CLI/Telegram
互動、長任務控制、結構化進度、Skill、上下文保護與運維邊界。如此一來 DSH
可以持續迭代，而使用者體驗與安全規則不會綁定到某個私有實作。

XDSH 遵循「先短暫規劃，再直接執行」：選擇最短的安全路徑，限制重試與資源
消耗；任務中斷時保留已取得的部分結果，並在憑據、網路、工作區與資源邊界
預設拒絕。

## 單獨使用 DSH，還是配合 XDSH？

DSH 仍可透過自己的 CLI、Web UI 與插件管理器獨立使用；XDSH 不分叉、隱藏或
取代這些入口。XDSH 增加的是面向遠端工作的產品控制層：命名工作階段、持久
佇列與任務狀態、有界重試與上下文保護、需要審批的記憶/Skill 變更、無內容
稽核與一致的進度顯示。直接在本機工作可以只使用 DSH；需要 Telegram/CLI
託管流程與運維保護時，可以讓同一套 DSH 安裝由 XDSH 接入。

## 功能概覽

- CLI 與 Telegram 互動，支援命名工作階段與多輪上下文；
- 結構化任務、工具與進度事件，支援取消、排隊、插隊/續接與有限重試；
- 內置 Skill、自動上下文壓縮、模型/Provider 選擇與背景任務；
- 受限且需驗證的 Web/MCP/browser/media 介面；
- 限定 chat/工作階段範圍、已脫敏的歷史檢索，以及不顯示內容的
  `/memory index` 容量診斷；
- 所有狀態位於獨立的 `~/.xdsh`，DSH 透過顯式執行命令接入；不開發自訂
  DSH 插件，憑據不進入程式碼或發布包。

## 架構

```text
Telegram / QQ / CLI
      ↓
XDSH 產品層（與執行時無關）
      ↓
DshRunner（官方 SDK / JSON-RPC）
      ↓
LiteLLM Anthropic Messages-compatible 閘道
      ↓
本地或遠端模型
```

DSH 與閘道之間仍使用 Anthropic Messages；自有閘道可轉接 OpenAI Chat
Completions、Gemini、Azure 等 API。這不是 DSH 原生 OpenAI transport，也不代表
每個模型都已驗證。詳見[模型接入](PROVIDERS.md)。

## 安裝固定版本

先從 GitHub 下載並解壓本版本的原始碼歸檔，在專案目錄中開啟終端。
以下指令安裝下載的本地版本，不依賴 PyPI 上已發布 XDSH。

宣告的 Python 範圍為 3.10 至 3.13，公開 CI 設定了這四個版本；有工作流程不代表
已執行通過，實際驗證組合以附日期的發布證據為準。DSH 本身仍遵循相容性說明中的
Node/runtime 要求。

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install '.[channels]'
```

互動式安裝會提示選擇語言，也可明確使用 `--language zh-TW`；非互動安裝預設
使用 English。可選語言為繁體中文、簡體中文、English、日本語、한국어、
Español、Français、Deutsch、Português、Русский、العربية、हिन्दी 與
Bahasa Indonesia。

macOS 準備原生 Mac Node 22.19.0 或更高版本；Linux／Ubuntu WSL 準備同樣最低
版本的 Linux Node。Python、Node 及原生依賴應使用同一平台和架構。官方 DSH
`0.1.1-rc.2` 安裝在 XDSH 自己的 npm 前綴，不覆蓋系統中的 DSH。

完成上方 Python 套件安裝後，在 macOS／Linux／Ubuntu WSL 的同一終端執行：
先確認 `node --version` 至少為 22.19.0，使用
`node -p 'process.platform + "/" + process.arch'` 確認其為本機平台，WSL 不使用 Windows Node。
若已有 XDSH 專用 DSH 前綴，先確認版本，不要盲目覆寫。首次安裝：

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
xdsh-setup --mode both --runtime-command "$NODE_BIN $DSH_JSONRPC_BIN" --language zh-TW --write-env
```

WSL 也可改走 [WSL 安裝器](WSL.md)，二選一，不必重複執行。設定工具只建立
目錄與範本，不會啟動模型、建立 Bot 或替你填好憑據。

只使用 Telegram／CLI 可安裝 `pip install .`；QQ 單獨使用 `.[qq]`，自有模型閘道
使用 `.[gateway]`。`channels` 包含 QQ SDK 及可選渠道的密碼學依賴。QQ 的掃碼接入、
白名單和能力邊界見 [CHANNELS.md](CHANNELS.md)，不承諾與 Telegram 選單完全等價。
Mac 不執行 WSL 安裝腳本；防休眠參數屬於 `xdsh-setup`，不是該腳本的參數。

## 設定模型閘道

二選一：沿用已存在的 loopback Messages 閘道；或安裝 `.[gateway]`、執行
`xdsh-providers tui`，依 [PROVIDERS.md](PROVIDERS.md) 啟動自有閘道。
Provider API Key 與本機閘道存取 Key 是不同用途，前者必須可由閘道程序讀取。
先啟動閘道，再啟動 XDSH；不要把範例模型名當成你的可用路由。

在 `~/.xdsh/config/cli.env` 或 `telegram.env` 中設定環境變數；只引用憑據，
不要把密鑰寫入命令列、程式碼或 Git：

```text
XDSH_LITELLM_BASE_URL=http://127.0.0.1:4000
XDSH_LITELLM_API_KEY=your-independent-loopback-key
XDSH_LITELLM_MODEL=your-exact-configured-model-id
XDSH_DSH_RUNTIME_COMMAND=/absolute/path/to/the/official/dsh-jsonrpc-command
```

閘道必須綁定 loopback。使用 `xdsh-doctor --mode both --strict` 做唯讀檢查。

模型選擇器不會把閘道的 `/model/info` 視為唯一來源。設定
`XDSH_SETTINGS_PATH` 後，可以在不寫入憑據的 `settings.json` 宣告閘道
未列出的企業或本地路由；Provider 別名也可透過
`XDSH_MODEL_PROVIDER_ALIASES` 設定。明確宣告的路由在有界執行探測或實際
任務驗證前會顯示為未知，探測明確不可用時不允許選擇。該檔案只保存展示與
能力元資料，不會建立 LiteLLM 路由或保存密鑰；重複 key、非有限數字、未知
欄位和疑似憑據的值都會被安全拒絕。例如：

```json
{"schema_version":1,"default_model":"my-company-route",
 "providers":[{"id":"company","label":"Company gateway","icon":"🏢"}],
 "models":[{"id":"my-company-route","provider":"company","tools":true}]}
```
倉庫中的 `examples/settings.json.example` 也可直接作為起點，複製到
`XDSH_SETTINGS_PATH` 指定的位置後只修改路由元資料。

### 不編輯 JSON 也能設定模型

新增真實 API 位址、憑據引用與模型 ID，可使用 `xdsh-providers tui`
（或 `xdsh-config tui`），也可在 Telegram 私聊中使用 `/model add`。
兩種入口共用確認式接入流程；新增路由由 XDSH 自有閘道自動載入，不會暗中
換成其他模型。安裝方法、支援的協定與限制見[模型接入](PROVIDERS.md)。

`xdsh-config` 是 CLI 與 Web 共用的無憑據設定控制面。腳本或終端可直接使用：

```bash
xdsh-config show
xdsh-config provider-set company --label '企業閘道' --icon '🏢'
xdsh-config model-set my-company-route --provider company --tools on
xdsh-config set-default --model my-company-route
xdsh-config validate
```

需要本機設定頁時，先把一個臨時存取值放入環境變數，再執行
`xdsh-config serve --port 8877`。服務固定綁定 `127.0.0.1`，要求讀取
`XDSH_CONFIG_UI_TOKEN` 的 bearer 值，並拒絕跨來源、過期版本與超限請求；它不
儲存憑據，也不會啟動 DSH 或發起模型請求。遠端主機只能經 SSH 本機連接埠轉送
存取，不要公開暴露該連接埠。`xdsh-setup` 會建立權限為 0600 的空
`settings.json`，`xdsh-doctor` 會檢查其 schema。

### 低 Token 安全並發

搜尋與研究任務預設產生最多四個互不重複的查詢，並優先使用一次批次搜尋呼叫；
儲存庫檢索在不改變語義時，會把相關 pattern 合併成一個有界原生查詢。其他
情況下，只有 DSH runtime 或工具定義明確宣告 concurrency-safe 的獨立唯讀
呼叫，才會在同一個模型步驟內並發，最多四個。獨立檔案／圖片讀取、多來源 Web
fetch、唯讀 metadata／health check 適合並發；寫入、編輯、安裝、狀態變更、
相依步驟和未宣告安全的工具一律串行。

此策略只使用一個模型回合和一份共享的證據／輸出預算，會去重結果，也不會為了
加速而建立 subagent。可用 `XDSH_MAX_PARALLEL_TOOL_CALLS=1..8` 調整上限；安全
預設值為 `4`，`/doctor` 會顯示實際值。

### 429 限流保護

所有互動入口都在底層使用同一個限流邊界。官方 DSH 的重試策略預設最多重試
5 次（每段連續失敗獨立計數），約等待 5、10、20、40、80 秒，附加 10% 抖動，
累計名義等待 155 秒，單次上限 120 秒。XDSH 另外限制並發與路由冷卻。
RC2 pi-ai 適配器可能遺失 `Retry-After` 提示，參見
[模型限流說明](PROVIDERS.md#upstream-rate-limits)。遇到 429 時不會自動重放整項任務，也不會立即
切換到共享配額的 fallback；達到上限會保留部分答案與進度，並以結構化限流
狀態結束。只有確認備用路由使用獨立配額時，才設定
`XDSH_RATE_LIMIT_ALLOW_FALLBACK=1`。其餘 `XDSH_RATE_LIMIT_*` 參數可依閘道
配額調整，`/status` 與 `/doctor` 會顯示生效邊界。

發布前可執行 `python -m xdsh.ops.rate_limit_probe`；這是無憑據、無網路的
合成探針，會驗證 429 有界處理、部分答案保留以及佇列准入保護。

## 先驗證 CLI

```bash
# 先在本機編輯 cli.env，替換所有佔位值；不要貼入公開終端紀錄。
set -a; . "$HOME/.xdsh/config/cli.env"; set +a
xdsh-doctor --mode cli --strict
xdsh --check
xdsh --prompt 'Reply with exactly READY.'
```

接著進入互動式 `xdsh`，測試同一工作階段記住／回憶一個測試代碼，再於
拋棄式工作區寫入並讀回小檔案。doctor 通過不代表模型或工具已通過實測。

## 使用 Telegram

1. 建立獨立的 Telegram Bot，並只把自己的數字使用者 ID 加入 allowlist。
2. 複製 `~/.xdsh/config/telegram.env.example` 為 `telegram.env`，填入環境
   變數並執行 `chmod 600`。
3. 執行下方檢查與啟動命令，再於 Telegram 發送 `/start` 和一個小任務。
4. 確認 `/model`、`/session`、`/language` 與多輪對話正常後，再設定常駐服務。

```bash
xdsh-doctor --mode telegram --env-file "$HOME/.xdsh/config/telegram.env" --strict
xdsh-telegram --env-file "$HOME/.xdsh/config/telegram.env"
```

已有正式 env 檔時直接編輯，不要用範本覆蓋。其他平台需要獨立官方應用與憑據，
不能共用 Telegram Bot Token；詳見 [CHANNELS.md](CHANNELS.md)。

QQ 使用官方機器人平台的出站 WebSocket，不需要公網回呼位址，也不會登入個人 QQ。
安裝 `.[qq]`（或完整的 `.[channels]`），執行
`xdsh-qq --init --env-file ~/.xdsh/config/qq.env` 產生不會覆寫既有檔案的
owner-only 範本；在其中設定 `XDSH_QQ_APP_ID`、`XDSH_QQ_APP_SECRET`、必填的
`XDSH_QQ_ALLOWED_USERS` 與既有 XDSH 的 runtime/model 設定，再以同一路徑執行
`xdsh-qq --check` 和啟動服務。群組訊息同時要求傳送者和群組 OpenID 均在白名單中。
也可以執行 `xdsh-qq --onboard --env-file ~/.xdsh/config/qq.env`，使用 QQ
開啟終端顯示的官方綁定連結；AppID、AppSecret 與 OpenID 自動儲存且不會列印。
安裝流程與明確重用模型設定的 `--from-env` 見 [CHANNELS.md](CHANNELS.md)。

隨時使用 `/language` 切換目前聊天的語言。選擇會持久保存，並重新整理該聊天的
37 項 Telegram 命令選單；除非任務明確要求其他語言，模型也會用所選語言
回答。13 種語言都有原生安裝選擇、首次導覽、說明頁與命令描述；尚未翻譯的
少用診斷資訊穩定回退到 English，不會回退到中文。

忙碌工作階段收到新訊息時，XDSH 會先持久化訊息，再提供五種明確選擇：插隊並
   在新任務後續接、合併補充後立即續跑、保持 FIFO、取代目前任務、移除訊息。
   提示只是建議，最終操作由使用者確認；憑據任務不會自動重放。

### 清空會話列表

每個聊天最多支援 **500 個會話**。`/session` 每頁顯示 10 個，預設開啟目前
會話所在頁，支援上一頁／下一頁。翻頁不切換會話，升級不刪除已有會話。

`/session` 的「清空全部會話」按鈕或 `/session clear-all` 會先要求二次確認。
確認後移出本聊天全部舊會話，建立空白會話並保留目前模型設定，釋放會話名額。
工作區檔案、記憶與歷史日誌保留，不是磁碟資料擦除。有執行中或排隊任務時
不能清理；確認兩分鐘後過期，會話狀態變更後也需重新確認。

## DSH 插件管理

DSH 仍可透過自己的 CLI 或 Web UI 獨立執行。XDSH 在開啟 `/plugins` 時重新
讀取官方 profile 的 `dependencies` 與 `dsh.profile.bundles`，因此透過 DSH
安裝的插件不需要第二套 registry 就會被發現，並在下一回合由新 runtime 載入：

```text
/plugins list
/plugins profiles
/plugins install @scope/package@1.2.3
/plugins remove @scope/package
/plugins sync source-profile [target-profile]
```

安裝、移除與同步都會先產生綁定使用者的 Telegram 審批卡，再把精確操作委託給
官方 `dsh plugin` CLI。設定 `XDSH_DSH_PLUGIN_COMMAND`（官方 CLI argv）才會
啟用變更；未設定時僅提供唯讀 inventory。`/plugins invoke` 只有在已驗證閘道
明確宣告 `plugin:<package>` 並通過版本化的 `xdsh-plugin-invoke-v1` 握手時才
開放（相容舊測試適配器仍接受 capability-only 形式）。RC2 的 stdio JSON-RPC
沒有直接 Host 調用方法，普通 tool 插件仍由 DSH agent 在任務中選擇與調用。
`/plugins` 返回一次性一致快照，並顯示穩定路由代碼
(`direct_gateway`、`agent_tool_only`、`direct_unavailable`、`gateway_invalid`)；
直接結果在離開核心邊界前會脫敏並限制大小。XDSH 不編輯 DSH manifest、不執行
shell，也不安裝自訂 DSH 插件。
互動式 `xdsh` CLI 提供相同的 inventory，並要求在本地安裝、移除或同步命令末尾
明確加入 `--approve`。

## Skill 與審批

研究 Skill 指導來源與方法對照、環境預檢和如實報告；
[產物驗收](ACCEPTANCE.md)檢查宣告的 JSON 值、比例、輸入雜湊、陣列維度/範圍/非退化，
以及指定 Python 參數的實際綁定值。辨識到中英文執行型論文復現請求時自動要求檢查清單；
普通任務仍可選擇明確合同。失敗保留部分結果並說明未滿足的類別，供後續修復。
`xdsh-verify` 提供唯讀自檢，不等於獨立科學驗證或全部需求已涵蓋，也不把 Todo 完成當成結果正確。
大型原件可明確啟用串流 SHA-256、NPY 校驗；安裝可選 `science` 依賴後，還可檢查指定的
HDF5／AnnData 結構與 ID 順序。用 `xdsh-verify --large-example` 查看範例，
[安裝與邊界說明](ACCEPTANCE.md#check-large-originals-explicitly)明確區分結構一致性與科學正確性。

XDSH 隨包提供有限的基礎 Skill，也可以載入目前工作區的專案 Skill，以及按
Telegram 聊天隔離、跨該 Bot 會話與工作區共用的個人 Skill。審閱者可以
提出一個 `SKILL.md` 以及最多 16 個 UTF-8 文字支援檔案（單個 64 KiB、總計
256 KiB）；路徑必須保持在 Skill 目錄內。隱藏檔、符號連結、二進位檔、含有
憑據或指令注入的內容都會被拒絕。支援檔案會納入有界的 before/proposed/applied
樹雜湊與 diff，但僅展示 Skill 時不會把檔案內容注入任務提示。

所有演化或歸檔候選都先保持 pending，只有操作員明確選擇專案或個人庫範圍並批准後
才會套用。Telegram 會提供兩個範圍按鈕；個人 Skill 存放在 XDSH 管理的狀態目錄，
不會寫入原始碼，並在重用前校驗雜湊。評估在隔離環境中唯讀執行；雜湊過期、缺少檔案
（不代表刪除）以及校驗失敗都會安全拒絕。已批准的變更可在再次校驗雜湊後回復。
專案級命令由 CLI 與 Telegram 共用；個人庫目前透過 Telegram 管理：

```text
/skills pending | diff ID | evaluate ID | approve ID | reject ID | rollback ID
/skills approve-personal ID | personal
/skills curate
/skills archive NAME
/skills archive diff ID | approve ID | reject ID | rollback ID
```

Curator 只根據觀測指標給出人工 review/archive 建議，不會自動合併或自我修改。

## 記憶與工作階段檢索

成功回合經脫敏後，依每個工作階段和每個 chat 的固定上限保留。`/search` 使用
限定範圍的 SQLite FTS5：先匹配嚴格 AND，再只從有限候選補充部分匹配與拼寫
近似，並採用確定性排序。已批准的 `MEMORY` 事實使用相同的有界詞法容錯，穩定
的 `USER` 偏好則保持載入。`/memory index` 只顯示回合數、保留上限與檢索模式，
不會顯示儲存內容。預設模式不需要外部 embedding 服務，也不會暗中發起網路
請求。維運者可以明確設定一個經過驗證、僅限 HTTP loopback、相容 OpenAI 形狀
的 embedding 端點；混合檢索仍會先脫敏、嚴格限制於目前 chat/工作階段並設定
資源上限，只儲存標準化 float32 向量，端點故障時自動退回詞法檢索。

## 版本與邊界

RC2 固定配對是 XDSH／DSH `0.1.1-rc.2`，協議 SDK 為 `0.1.1rc1`；
模型不綁定特定供應商，請配置自己的精確路由。舊的 `0.1.0a0-rc2` 回滾證據由維護者在公開原始碼包之外
以不可變方式保留；後續候選版本使用獨立的狀態目錄、映像與執行時，不覆蓋
該證據。當前發布候選還包含排隊續接優先級穩定化與
`/queue insert` 別名。
目前穩定 RC2 使用與 DSH 對齊的版本線：XDSH 與 DSH 都是
`0.1.1-rc.2`。Python 套件依 PEP 440 寫作 `0.1.1rc2`，兩種寫法代表同一
個預發布版本；協議客戶端 `deepseek-harness-sdk==0.1.1rc1` 仍獨立維護。
舊的 `0.1.0a0-rc2` 僅作為公開原始碼包之外的不可變歷史證據保留，不再是目前安裝版本。
詳見 [RC2 發布說明](RELEASE-NOTES-0.1.1-rc.2.md)。

XDSH 不打包 DSH runtime、Provider 憑據或生產狀態。所有部署狀態預設位於
`~/.xdsh`，服務只綁定 loopback。執行前請閱讀 [安全說明](SECURITY.md) 與
[容器說明](CONTAINER.md)。

## 交給 AI agent 部署

將下方提示詞貼給能存取本專案與目標主機的 agent：

```text
請在我授權的主機部署此 XDSH 原始碼版本。先讀 README、COMPATIBILITY、
PROVIDERS、SECURITY（開發倉庫位於 docs/public），依環境讀 WSL 或 CONTAINER。
簡要規劃後直接執行。固定 XDSH／DSH 0.1.1-rc.2，Python 套件0.1.1rc2，
不要安裝 DSH latest。檢查 Python／Linux Node、既有服務與連接埠。
只詢問缺少的主機、Provider、精確模型 ID、語言與數字使用者白名單。
隔離安裝到 XDSH 專用目錄，保留其他服務；僅綁定 loopback。
憑據使用本機遮罩輸入或環境引用，不列印、記錄、提交或借用其他產品憑據。
先啟動模型閘道，再設定精確 DSH runtime 命令及獨立 Telegram Bot。
執行 doctor、小型 CLI 任務、同會話續話、隔離檔案寫入讀回；
請我發送 /start 和小任務完成 Telegram 驗收。替換服務前保留回滾。
回報版本、路徑、命令、真實測試結果與阻塞。不要把範本生成稱為部署完成，
不要捏造下載位址、暗換模型、關閉安全保護或擅自發布。
```

agent 仍需要你的授權及安全提供的憑據；這不是管理其他主機的許可。

## 開發與驗證

```bash
uv sync --all-groups
python -m pytest -q
ruff check .
mypy src tests
xdsh-release export --destination /tmp/xdsh-public
xdsh-release audit /tmp/xdsh-public --strict
xdsh-release verify /tmp/xdsh-public
```

發布包會包含 Apache-2.0 許可證與 `NOTICE`，但不會包含憑據、執行時狀態或
特定主機設定。乾淨原始碼包也包含鎖定依賴、無憑據核心契約測試（數量以當前執行結果為準）與獨立 GitHub Actions 工作流程；部署/對抗夾具與真實服務
測試仍由維護者發布門執行，不會被誤稱為公開 CI 覆蓋。

參與開發前請讀 [CONTRIBUTING.md](CONTRIBUTING.md)，提交問題前請讀
[SUPPORT.md](SUPPORT.md)，參與社群時請遵守
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。完整發布矩陣見
[RELEASE-MATRIX.md](RELEASE-MATRIX.md)。

## 許可證

XDSH 採用 [Apache License 2.0](LICENSE)。DSH runtime、LiteLLM 與其他
第三方依賴仍依各自許可證與條款提供。
