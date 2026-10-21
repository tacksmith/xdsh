# XDSH / DSH 相容性說明

語言：[English](COMPATIBILITY.md) · [简体中文](COMPATIBILITY.zh-CN.md) ·
[繁體中文](COMPATIBILITY.zh-TW.md)

XDSH 與 DSH 必須按照**已驗證的版本組合**使用。第一次公開發布只有一個受支援
組合，不能把它與最新 DSH 隨意混裝。

## 已驗證組合

| XDSH 發布識別 | Python 套件 | DSH runtime | Python SDK | 啟動器 | 狀態 |
| --- | --- | --- | --- | --- | --- |
| `xdsh-0.1.1-rc.2` | `0.1.1rc2` | `0.1.1-rc.2` | `0.1.1rc1` | `dsh-sdk-jsonrpc-demo/lib/packaged-bin.js` | **目前對齊的穩定公開版** |
| `xdsh-0.1.0a0-rc2` | `0.1.0a0` | `0.1.1-rc.2` | `0.1.1rc1` | `dsh-sdk-jsonrpc-demo/lib/packaged-bin.js` | **不可變歷史歸檔** |

該組合使用首版 LiteLLM Anthropic Messages-compatible loopback 鏈路。歷史驗證模型
為 `ark-code-latest`；新安裝須明確填寫自己的模型 ID，不要求火山引擎帳戶。
DSH runtime 不會打包進 Python 套件；安裝器會把精確固定
的 runtime 安裝到 XDSH 自己的 `~/.xdsh` 前綴，並拒絕其他版本。

穩定組合也支援透過 `/plugins` 唯讀發現 DSH profile 插件：XDSH 讀取官方
`dependencies` 與 `dsh.profile.bundles`，不會改寫 profile。只有明確設定官方
CLI argv（`XDSH_DSH_PLUGIN_COMMAND`）後，安裝、移除與同步才會進入審批流程；
RC2 stdio JSON-RPC 沒有直接 Host 插件調用方法。透過 DSH Web/CLI 獨立修改的
插件會在下一次 XDSH 任務前被偵測，並由新的 runtime 載入。
可選的直接調用閘道必須宣告版本化的 `xdsh-plugin-invoke-v1` 契約和準確的
`plugin:<package>` 能力，否則調用會安全拒絕。Inventory 使用穩定路由代碼，
並對直接結果脫敏和限長，因此「已供 agent 安裝」不會被誤報為「外層可直接調用」。

專案 Skill 演化是 XDSH 外層的相容層，不是 DSH Host 擴充。它只接受有界的 UTF-8
支援檔案（最多 16 個、單個 64 KiB、總計 256 KiB），並將其綁定到
before/proposed/applied 樹雜湊；必須先隔離評估，再由操作員明確批准。缺少檔案不
代表刪除；過期、符號連結、隱藏、二進位、含憑據或指令注入的資源都會安全拒絕。
Curator/歸檔建議只供人工審核，已批准的變更支援經過雜湊校驗的回復。這些控制與
列出的 RC2 runtime 相容，但不會虛構協議未提供的原生 Host-only 命令。

目前對齊版本的發布識別為 `xdsh-0.1.1-rc.2`，Python 套件依 PEP 440 寫作
`0.1.1rc2`；維護者在公開原始碼包之外保留不可變的 `0.1.0a0-rc2` 證據歸檔，包含原始五種忙碌工作階段
選擇：插隊並保存續接、同工作階段補充、正常 FIFO、取代、移除。歷史雜湊不會
被覆蓋；Apache-2.0 正式候選會單獨重建，並在上傳前產生自己的校驗值。

## 執行時規則

- 本版只使用官方 RC2 JSON-RPC demo 閉包與明確的 Cordis 命令。
- runtime manifest、安裝器、映像標籤與 doctor 探測都必須報告
  `0.1.1-rc.2`。
- 不要把本版指向 profile 啟動器或浮動的 `latest` 標籤。
- 每個隔離的 `DSH_HOME` 同時只允許一個存活的 XDSH runner，透過作業系統
  advisory lock 做資源互斥。不同實例必須使用不同的 `DSH_HOME`；進程崩潰後
  鎖由作業系統釋放。這是宿主資源隔離，不是 DSH 原生 generation、續接或取消。
- XDSH checkpoint 可選記錄不透明的宿主進程標記與內部 session generation，
  以及每次 runtime 啟動的隨機標記。正規化事件攜帶同一標記；適配器先拒絕來源
  標記缺失或不匹配的通知，再檢查序號。即使 DSH 工作階段 ID 未變，更換 runtime
  也會產生新標記。這用於本地事件隔離與回滾/恢復來源診斷；它不改變 DSH wire，
  也不會把 RC2 變成原生跨進程
  續接/取消協議。舊 checkpoint 在沒有此欄位時仍然有效。
- 後續 DSH 版本必須配套新的 XDSH 版本、隔離狀態與獨立相容性矩陣。

## 升級步驟

1. 記錄目前 XDSH/DSH 版本，只備份 `~/.xdsh` 下的 XDSH 狀態。
2. 在獨立虛擬環境和獨立 `~/.xdsh` runtime 前綴安裝候選。
3. 執行 `xdsh-doctor --mode both --strict --probe-runtime` 與完整 hermetic 測試。
4. 驗證單任務、多輪續話、結構化工具呼叫與進程級取消；Telegram 候選使用
   獨立 Bot、獨立狀態，並手動驗證命令與任務；內部 `xdsh-canary` 工具不包含在公開套件中。
5. 只有矩陣與回滾檢查都通過才切換；失敗時恢復原組合，不自動重放不明任務。

相容表示啟動器與 SDK 滿足 `dsh-sdk-jsonrpc-session-events-v1` 契約，並通過
事件、工作階段與取消測試；不表示所有 DSH Host-only 命令或未來 Provider 功能
都自動可用。XDSH 採用 Apache-2.0，DSH runtime 與第三方服務仍依各自條款提供。
