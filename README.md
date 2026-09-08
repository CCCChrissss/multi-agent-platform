# Multi-Agent Platform

可重複使用的內部多 Agent 平台原型，包含事件驅動編排、MCP 工具權限、LiteLLM 模型入口、長期記憶與 Demo UI。
目標是讓開發者自行 clone、設定、執行與測試；Git、VS Code 和終端機即可操作，AI 編輯器為選用工具。

## 開始使用

1. [Windows / VS Code 完整手冊](docs/windows-setup.md)：工具、clone、Python、DB、模型、服務、UI、觸發與停止。
2. [測試手冊](docs/testing.md)：無服務、套件、DB 與模型測試的邊界。
3. [參與開發](CONTRIBUTING.md)：分支、PR、設定與驗證規範。
4. [本次驗證狀態](docs/portability-validation.md)：已測項目與仍待新機驗收的部分。

命令都從 repository 根目錄執行；沒有固定的磁碟、使用者名稱或父目錄。
完成 Python/uv 安裝後：

```powershell
uv python install 3.11
uv sync --locked
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\dev.ps1 check
```

接著建立自己的 `.env`，啟動 PostgreSQL 與 Ollama，再用 repository-owned 指令檢查／初始化資料庫；主要流程不要求 `psql` 在 PATH：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\dev.ps1 doctor
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\dev.ps1 db-check  # 唯讀，未初始化時預期回報缺項
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\dev.ps1 db-init   # 建立 DB、啟用 vector、初始化專案 schema；可重複執行
```

本手冊的 `dev.ps1` 指令一律以 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File`
啟動，只對該次子程序生效，不需要修改系統或使用者的永久 Execution Policy。
有了這些前置條件，使用不同 VS Code terminals：

```powershell
# Terminal A（已有 Ollama server；沒有時先依主手冊啟動）
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\dev.ps1 services -Workflow stt_check_notify
# Terminal B，等 services ready
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\dev.ps1 workers -Workflow stt_check_notify
# Terminal C
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\dev.ps1 trigger -Workflow stt_check_notify
# 另開 terminal 提供 UI backend
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\dev.ps1 ui -Workflow stt_check_notify
```

以瀏覽器開啟本機 `demo/index.html`。也可用「終端機 → 執行工作」選 `platform:` Tasks。
完成後以 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\dev.ps1 stop`
停止本 checkout 管理的 groups；將最後的 action 改為 `status` 可查 supervisor 狀態。
已由其他程式管理的 Ollama / PostgreSQL 不由專案 stop 結束。

## 支援與功能需求

Windows x64 / PowerShell 5.1 或 7 / Python 3.11 為本版操作基準。
目前 Windows 套件來源仍含 CUDA 13.2 PyTorch；CPU 路徑存在，但獨立 CPU 安裝與效能未完成驗證。
macOS / Linux 的完整流程未由本版重新驗證；macOS 原流程保存在 [歷史參考](docs/legacy-macos.md)。

| 功能 | 模型／服務需求 |
|---|---|
| 台積電、保單除外責任 workflow | local-qwen3、local-embed、Breeze-ASR-25、PostgreSQL/vector、services/workers |
| semantic / procedural 記憶 | bge-m3 embedding 與 PostgreSQL/vector |
| CLI 知識蒸餾 | 預設 local-qwen3，不需要雲端 API key；可顯式指定其他已配置 chat alias |
| UI 蒸餾 | 使用同一個 local-qwen3 預設 |
| notified | 本機 placeholder，沒有真的寄信或發 Slack |
| 無服務測試 | Python 3.11，不需雲端 key／模型／DB |

兩個示範都是 `stt → check → notified`，用來驗證通用平台，不是平台功能的界限。
台積電另有刻意凍結的同步 LangGraph 路徑；除外責任僅有事件驅動路徑。
主工作流在 services/workers 啟動時選定；切換 UI 選單或 trigger 參數不會切換已執行的服務。

此版本支援每位開發者各自執行本機 stack；固定 localhost ports 仍不支援同機多套同時啟動。
它不是多人線上部署方案；登入、共用資料治理及遠端服務設定不在本次範圍。

## 示範 workflow 的三個 agent

```
stt -> check -> notified
```

- **stt**：透過 `MCPGateway` 連上 [mcp_servers/stt](mcp_servers/stt/)（轉錄）與 [mcp_servers/format_check](mcp_servers/format_check/)（格式檢查）兩個 MCP server，透過 LiteLLM Gateway 呼叫 workflow 宣告的 LLM 自行決定要不要先檢查音檔格式、再進行轉錄（[llm/stt_agent.py](llm/stt_agent.py)）。目前 `stt_check_notify` 與 `stt_exclusion_notify` 都宣告 `local-qwen3`；兩者共用相同 agent 邏輯，但 model alias 由各自 YAML 決定。實際轉錄仍由 [Breeze-ASR-25](https://huggingface.co/MediaTek-Research/Breeze-ASR-25)（[services/stt/breeze_asr.py](services/stt/breeze_asr.py)）負責，不會因 agent 決策模型切換而被取代。
- **check**：兩個場景各自一套判斷邏輯，[agents/runtime.py](agents/runtime.py) 的 `/check/run` 路由依啟動時選的 workflow 決定呼叫哪一套（見下方「切換示範 workflow」）：
  - `stt_check_notify`：透過 LiteLLM Gateway 呼叫 LLM（目前宣告 `local-qwen3`）判斷逐字稿是否提到台積電，並用確定性的別名比對當 backstop（[llm/tsmc_judge.py](llm/tsmc_judge.py)）。
  - `stt_exclusion_notify`：透過 LiteLLM Gateway 呼叫 LLM（目前宣告 `local-qwen3`）判斷客戶描述的情況是否涉及保單除外責任。為了讓本機 4B 模型可穩定重現，程式會先在指定保單根目錄做兩路、有界的 semantic recall（除外原因、給付／失能門檻），並實際 browse 根節點；模型仍可用 [`browse_semantic_memory`](mcp_servers/memory/server.py) 補查，只允許引用 recall 或 browse 真正讀到的條文（[llm/exclusion_judge.py](llm/exclusion_judge.py)，詳見 [docs/exclusion-scenario-plan.md](docs/exclusion-scenario-plan.md)）。
- **notified**：兩個場景共用同一顆 agent，不知道場景邏輯——只收「要不要發、主旨、內容」，透過 `MCPGateway`（[mcp_servers/gateway.py](mcp_servers/gateway.py)）連上 [mcp_servers/notified](mcp_servers/notified/)（Slack / Gmail 兩個 tool，背後打 [services/notified/](services/notified/)）。目前兩份 workflow 都宣告 `local-qwen3`。`should_notify=false` 時會在呼叫 LLM / tool 前直接回傳 `[]`；需要通知時才由模型決定管道。目前 notified service 是本機 placeholder，不會真的對外寄送。

### 單一 runtime process

三個 agent 由同一個 FastAPI process（[agents/runtime.py](agents/runtime.py)）服務，各自一條路由（`/stt/run`、`/check/run`、`/notified/run`）——路由本身就是身分，跟 [docs/agent-api-contract.md](docs/agent-api-contract.md) 的既有設計一致（呼叫哪個 endpoint 決定是哪個 agent，不用在 request body 裡宣告身分）。三個 agent 各自一份 `MCPGateway`（[agents/lifespan.py](agents/lifespan.py) 的 `make_runtime_lifespan()`）：一個 gateway 實例的 principal 在建構時就固定，塞進它啟動的 `memory` MCP 子行程當環境變數，沒辦法一個 gateway 服務三種不同身分，所以是三份 gateway、不是三個 process——MCP 子行程總數（18 個：3 agent × 6 個 server，`connect()` 對 `policy.servers` 全開）跟改動前一樣，只是現在都在同一個 process 底下。長期記憶的 store/policy 不是 principal-scoped 的（呼叫時才從 `current_node_name` 讀 principal），三個 agent 共用同一份。

## 分層架構

```
觸發        orchestrator/trigger.py                       workflows/simple_pipeline.py
                     |  (事件驅動)                                 |  (同步)
                     v                                             v
編排層      Master Agent --event_bus--> Worker x3           LangGraph StateGraph
            master_agent.py             worker.py
                     |                                             |
                     |  HTTP（docs/agent-api-contract.md）         |  in-process call
                     v                                             |
Agent 層    agents/runtime.py（stt/check/notified 三個路由）             |
                     |                                             |
                     +-------> llm/stt_agent.py <-----------------+
                               llm/tsmc_judge.py / llm/exclusion_judge.py
                               llm/notify_agent.py
                                     |                    |
                                     v                    v
基礎建設層           MCPGateway（RBAC）          LiteLLM Gateway
                     mcp_servers/gateway.py      gateway/
                                     |                    |
MCP server 層  mcp_servers/{stt,format_check,lookup,notified,memory,calc}/  |
                                     |                             |
Service 層     services/{stt,notified}/  <------------------------+
               （Breeze-ASR-25、通知 placeholder）
```

兩種模式共用同一份 agent 邏輯（`llm/`、`mcp_servers/*/agent.py`）——差別只在誰去呼叫它們。

---

## 文件與來源

- [文件索引](docs/README.md)、[程式接手路線](docs/onboarding.md)、[觀測](docs/observability.md)
- [知識蒸餾](docs/knowledge-distillation-windows.md)、[Harness 原則](docs/harness-engineering-principles.md)
- [歷史主機快照](docs/current-windows-status.md)：只記錄過去結果，不代表新使用者環境。
- [AGENTS.md](AGENTS.md) / [CLAUDE.md](CLAUDE.md)：選用 AI 開發工具的規範與歷史脈絡。
- [UPSTREAM.md](UPSTREAM.md)：來源與原作者同意紀錄，目前未新增 LICENSE。
- [TODO.md](TODO.md)：新待辦的 issue 入口；[fixed.md](fixed.md) 保存過去修正脈絡。
