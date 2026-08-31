# Multi-Agent Platform

公司內部多 agent 平台的原型（平台目標見 [CLAUDE.md](CLAUDE.md)，Codex 開發規範見 [AGENTS.md](AGENTS.md)）。平台上目前有**兩個示範 workflow**，用來驗證基礎建設堪不堪用，都是同一個形狀：語音轉文字 → 檢核 → 通知。

> [!IMPORTANT]
> 本 repository 同時保留原作者的 **macOS / Bash / Claude Code** 流程，並漸進新增目前實機的 **Windows / PowerShell / Codex** 流程。Windows 於 2026-08-27 的安裝狀態、已驗證範圍與阻擋項目見 [docs/current-windows-status.md](docs/current-windows-status.md)；macOS 內容保留上游操作方式，但尚未由目前維護者重新實機驗證。

| | [stt_check_notify.yaml](workflows/definitions/stt_check_notify.yaml) | [stt_exclusion_notify.yaml](workflows/definitions/stt_exclusion_notify.yaml) |
|---|---|---|
| `check` 判斷什麼 | 逐字稿有沒有提到台積電（[llm/tsmc_judge.py](llm/tsmc_judge.py)） | 客戶描述的情況有沒有涉及保單除外責任（[llm/exclusion_judge.py](llm/exclusion_judge.py)） |
| `check` 怎麼查證據 | 確定性別名比對當 backstop | 把保單條款存進長期記憶（[data/insurance_product/](data/insurance_product/)），透過 `browse_semantic_memory` 這個 MCP tool 自己一層一層鑽,查到什麼答什麼 |
| 定位 | 最早的場景，事件驅動路徑仍在跑 | [docs/exclusion-scenario-plan.md](docs/exclusion-scenario-plan.md) 的示範，用來壓測「agent 能不能不把整份文件塞進 context，自己找到答案」 |
| 假音檔 | `samples/gen_tsmc_*.wav` / `gen_other_*.wav` | `samples/gen_policy_*.wav` |

兩個 workflow 共用同一個 agent runtime process（`stt`/`check`/`notified` 三個 agent 都在裡面，見 [agents/runtime.py](agents/runtime.py)），跑哪一個是**啟動時**的選擇，不是每次請求各自決定——見下方「切換示範 workflow」。

台積電那個場景（`stt_check_notify`）的同一份業務邏輯有**兩種執行模式**：

| | 同步模式 | 事件驅動模式 |
|---|---|---|
| 入口 | [workflows/simple_pipeline.py](workflows/simple_pipeline.py) | [orchestrator/trigger.py](orchestrator/trigger.py) |
| 編排 | 單一 process 內的 LangGraph `StateGraph` | Master Agent + 每步一個 worker process，靠 [event_bus/](event_bus/) 協調 |
| 執行狀態存哪 | `checkpoints` 三張表（LangGraph checkpointer） | `orchestrator_runs`（[orchestrator/run_state.py](orchestrator/run_state.py)） |
| agent 怎麼被呼叫 | in-process function call | HTTP（[agents/runtime.py](agents/runtime.py) 單一 service、三條 route） |
| 定位 | 最早的版本，刻意凍結不動（見 [workflows/parity_check.py](workflows/parity_check.py)） | **目前的主線** |

除外責任那個場景（`stt_exclusion_notify`）**只有事件驅動模式**——`workflows/simple_pipeline.py` 是刻意凍結的舊版本，不會跟著新場景長。

---

## 示範 workflow 的三個 agent

```
stt -> check -> notified
```

- **stt**：透過 `MCPGateway` 連上 [mcp_servers/stt](mcp_servers/stt/)（轉錄）與 [mcp_servers/format_check](mcp_servers/format_check/)（格式檢查）兩個 MCP server，透過 LiteLLM Gateway 呼叫 workflow 宣告的 LLM 自行決定要不要先檢查音檔格式、再進行轉錄（[llm/stt_agent.py](llm/stt_agent.py)）。目前 `stt_check_notify` 與 `stt_exclusion_notify` 都宣告 `gemini-cheap`；兩者共用相同 agent 邏輯，但 model alias 由各自 YAML 決定。實際轉錄仍由 [Breeze-ASR-25](https://huggingface.co/MediaTek-Research/Breeze-ASR-25)（[services/stt/breeze_asr.py](services/stt/breeze_asr.py)）負責，不會因 agent 決策模型切換而被取代。
- **check**：兩個場景各自一套判斷邏輯，[agents/runtime.py](agents/runtime.py) 的 `/check/run` 路由依啟動時選的 workflow 決定呼叫哪一套（見下方「切換示範 workflow」）：
  - `stt_check_notify`：透過 LiteLLM Gateway 呼叫 LLM（目前宣告 `gemini-cheap`）判斷逐字稿是否提到台積電，並用確定性的別名比對當 backstop（[llm/tsmc_judge.py](llm/tsmc_judge.py)）。
  - `stt_exclusion_notify`：透過 LiteLLM Gateway 呼叫 LLM（目前工作樹宣告 `gemini-cheap`）判斷客戶描述的情況是否涉及保單除外責任——不會把保單條款塞進 prompt，而是透過 [`browse_semantic_memory`](mcp_servers/memory/server.py) 這個 MCP tool 自己決定要往下鑽哪個分支，只把讀到過的條文拿來引用（[llm/exclusion_judge.py](llm/exclusion_judge.py)，詳見 [docs/exclusion-scenario-plan.md](docs/exclusion-scenario-plan.md)）。
- **notified**：兩個場景共用同一顆 agent，不知道場景邏輯——只收「要不要發、主旨、內容」，透過 `MCPGateway`（[mcp_servers/gateway.py](mcp_servers/gateway.py)）連上 [mcp_servers/notified](mcp_servers/notified/)（Slack / Gmail 兩個 tool，背後打 [services/notified/](services/notified/)）。目前兩份 workflow 都宣告 `gemini-cheap`。`should_notify=false` 時會在呼叫 LLM / tool 前直接回傳 `[]`；需要通知時才由模型決定管道。目前 notified service 是本機 placeholder，不會真的對外寄送。

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

## Windows / PowerShell 執行（目前主線）

本節以 **Windows PowerShell 5.1、VS Code、Python 3.11、uv 與 D 槽 workspace** 為基準，涵蓋安裝、啟動、workflow 切換、觸發、資料庫查詢與關閉。更細的背景與錯誤分類見 [Windows 安裝手冊](docs/windows-setup.md)、[疑難排解](docs/setup.md) 與 [觀測手冊](docs/observability.md)。

> [!IMPORTANT]
> Windows 不要複製後方 macOS / Bash 區塊的 `uv run honcho start`、`export`、`brew` 或 Bash 反斜線續行。繁體中文 Windows 必須在**同一個 PowerShell** 先設定 `$env:PYTHONUTF8 = '1'`，再啟動 Honcho，否則可能以 CP950 解碼 UTF-8 `.env` 而失敗。

### 0. 先理解三個 terminal

整個 event-driven workflow 使用三個 VS Code PowerShell terminal：

| Terminal | 是否保持開啟 | 用途 |
|---|---|---|
| A：Services | 是 | `Procfile`：Ollama、LiteLLM、STT、notified、Agent Runtime |
| B：Workers | 是 | `Procfile.workers`：Master、`worker-all`、`memory-writer` |
| C：Client | 否 | 檢查 port／model、trigger、查詢結果 |

正確順序是：A 啟動 → C 驗證五個 port → B 啟動 → C trigger。切換 workflow 時，A、B 必須一起停止並用相同 YAML 重新啟動。

### 1. Windows 前置工具

需要以下工具：

- [Git for Windows](https://git-scm.com/downloads/win)
- [uv 官方 Windows 安裝方式](https://docs.astral.sh/uv/getting-started/installation/)
- [PostgreSQL Windows installer](https://www.postgresql.org/download/windows/)
- [pgvector](https://github.com/pgvector/pgvector)
- [Ollama for Windows](https://ollama.com/download/windows)
- VS Code（建議使用整合式 PowerShell）

安裝 uv 後，開新的 PowerShell 驗證：

```powershell
$uvExe = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
& $uvExe --version
```

如果安裝程式把 uv 放在其他位置，請以安裝程式顯示的路徑為準。不要在不知道來源的情況下載替代執行檔。

### 2. Clone repository 與建立 Python 3.11 環境

以下路徑是目前實機路徑；其他使用者只需修改 `$RepoRoot`：

```powershell
$RepoRoot = 'D:\Projects\multi-agent平台架設\multi-agent-platform'
$WorkspaceRoot = Split-Path -Parent $RepoRoot
$uvExe = Join-Path $env:USERPROFILE '.local\bin\uv.exe'

$RepoParent = Split-Path -Parent $RepoRoot
New-Item -ItemType Directory -Force -Path $RepoParent | Out-Null
Set-Location -LiteralPath $RepoParent
git clone https://github.com/CCCChrissss/multi-agent-platform.git
Set-Location -LiteralPath $RepoRoot

$env:UV_CACHE_DIR = Join-Path $WorkspaceRoot '.uv-cache'
$env:HF_HUB_CACHE = Join-Path $WorkspaceRoot '.hf-cache'
& $uvExe python install 3.11
& $uvExe venv --python 3.11
& $uvExe sync
```

如果 repository 已存在，不要再次 `git clone`；直接從設定 `$RepoRoot` 開始。`uv sync` 會依 [pyproject.toml](pyproject.toml) 與 [uv.lock](uv.lock) 建立 `.venv`，不必每次啟動服務都重跑。

驗證：

```powershell
.\.venv\Scripts\python.exe --version
.\.venv\Scripts\python.exe -c "import torch; print(torch.__version__); print(torch.cuda.is_available())"
```

第一行必須是 Python 3.11.x。目前 Windows lockfile 使用 PyTorch CUDA 13.2 index；有 NVIDIA GPU 的實機已驗證 `torch.cuda.is_available()` 為 `True`，但沒有相容 GPU 時為 `False` 不代表 Python 環境損壞。

### 3. 安裝 PostgreSQL、建立資料庫與 pgvector

使用 PostgreSQL 官方 Windows installer 安裝 Server、pgAdmin 4 與 Command Line Tools。安裝過程要求設定的 `postgres` 密碼是**使用者自行建立的密碼**，repository 不會產生也不知道這個密碼。

目前實機版本是 PostgreSQL 18.6 與 pgvector 0.8.6。pgvector 必須安裝到同一套 PostgreSQL major version；若 `vector` 不在 available extensions，請依 [pgvector 官方 Windows 說明](https://github.com/pgvector/pgvector#installation-notes---windows) 安裝，不能混用另一套 PostgreSQL 的 extension 檔案。

在 pgAdmin 4 建立資料庫：

1. 展開 `Servers > PostgreSQL 18 > Databases`。
2. 右鍵 `Databases`，建立 `agent_architecture_test`。
3. 對 `agent_architecture_test` 開啟 Query Tool。
4. 執行：

```sql
CREATE EXTENSION IF NOT EXISTS vector;

SELECT extversion
FROM pg_extension
WHERE extname = 'vector';
```

查詢必須回傳版本。應用程式資料表會由各模組建立，不需要手動貼 `CREATE TABLE`。

### 4. 建立 `.env`

只在 `.env` 不存在時複製，避免覆蓋既有密碼與 API key：

```powershell
Set-Location -LiteralPath $RepoRoot
if (-not (Test-Path -LiteralPath '.env')) {
    Copy-Item -LiteralPath '.env.example' -Destination '.env'
}
```

在 `.env` 至少設定：

```text
PERSISTENCE_DATABASE_URL=postgresql://postgres:YOUR_PASSWORD@localhost:5432/agent_architecture_test
GEMINI_API_KEY=YOUR_GEMINI_API_KEY
ANTHROPIC_API_KEY=
```

`YOUR_PASSWORD` 與 `YOUR_GEMINI_API_KEY` 都是佔位字串，不能原樣保留。若資料庫密碼包含 `@`、`:`、`/` 等 URL 保留字元，必須先做 percent-encoding，否則連線字串會被錯誤切割。不要提交 `.env`，也不要把真實 secret 貼進 README、issue、commit 或終端截圖。

目前兩份 workflow YAML 的 `stt`、`check`、`notified` 都宣告 `gemini-cheap`，因此執行時需要有效的 `GEMINI_API_KEY`。`gateway/config.yaml` 仍保留 `claude-haiku` provider 供未來使用；目前 workflow 沒引用時，`ANTHROPIC_API_KEY` 可以留空。

建議讓 `.env` 中的 `WORKFLOW_DEF_PATH` 保持註解，改由每個 terminal 明確設定。Honcho 會用 `.env` 覆蓋同名的 PowerShell 環境變數；如果 `.env` 寫死另一份 workflow，會造成 Runtime／workers 與 trigger 不一致。

安全檢查（只顯示 workflow 設定，不顯示 secret）：

```powershell
Select-String -Path .env -Pattern '^WORKFLOW_DEF_PATH='
```

建議沒有輸出。

### 5. 安裝 Ollama 並把模型放在 D 槽

安裝 Ollama 後，先從 Windows 系統列完全退出 Ollama Desktop，避免背景 server 先占用 11434 並讀取 C 槽預設模型目錄。

在第一個暫時 PowerShell 啟動只供下載模型使用的 server：

```powershell
$RepoRoot = 'D:\Projects\multi-agent平台架設\multi-agent-platform'
$WorkspaceRoot = Split-Path -Parent $RepoRoot
$ollamaExe = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'

$env:OLLAMA_MODELS = Join-Path $WorkspaceRoot '.ollama\models'
& $ollamaExe serve
```

保持該 terminal 開啟，在第二個 PowerShell 下載模型：

```powershell
$ollamaExe = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
& $ollamaExe pull qwen2.5:3b
& $ollamaExe pull bge-m3
& $ollamaExe list
```

預期至少看到 `qwen2.5:3b` 與 `bge-m3`。下載完成後，在暫時 server terminal 按 `Ctrl+C`，並確認 11434 已釋放；後續由 `Procfile` 管理 Ollama。

### 6. 每次啟動前先清除舊程序

先在既有 Honcho terminal 按 `Ctrl+C`。若曾直接關閉 terminal、VS Code 或遇到 `pool-2`／`OSError(22)`，Honcho 的 uv／Python 孫程序可能仍在背景。

在 repository 根目錄先預覽，再清理：

```powershell
Set-Location -LiteralPath $RepoRoot
.\scripts\stop_windows_stack.ps1 -WhatIf
.\scripts\stop_windows_stack.ps1
```

PowerShell execution policy 若阻擋腳本：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\stop_windows_stack.ps1
```

腳本保留 PostgreSQL 與 VS Code。完成後預期 `5432=True`，`11434/4000/8001/8002/8003=False`。

### 7. 選擇 workflow

| Workflow | 用途 | 測試音檔 | 額外前置 |
|---|---|---|---|
| `stt_check_notify` | 判斷逐字稿是否提到台積電 | `samples/gen_tsmc_01.wav` | 無額外 seed |
| `stt_exclusion_notify` | 判斷是否涉及保單除外責任 | `samples/gen_policy_01.wav` | 保單記憶應有 59 筆 |

選擇一份 YAML，之後在 Terminal A、B 與 trigger 都使用相同值：

```powershell
$WorkflowDef = 'workflows/definitions/stt_check_notify.yaml'
```

或：

```powershell
$WorkflowDef = 'workflows/definitions/stt_exclusion_notify.yaml'
```

不設定 `WORKFLOW_DEF_PATH` 時，程式預設 `stt_check_notify.yaml`。為了避免誤會，本指南仍建議明確設定。

### 8. Terminal A：啟動五個 services

開啟新的 VS Code PowerShell，完整執行以下區塊。這個 terminal 必須保持開啟：

```powershell
$RepoRoot = 'D:\Projects\multi-agent平台架設\multi-agent-platform'
$WorkspaceRoot = Split-Path -Parent $RepoRoot
$uvBin = Join-Path $env:USERPROFILE '.local\bin'
$ollamaBin = Join-Path $env:LOCALAPPDATA 'Programs\Ollama'

Set-Location -LiteralPath $RepoRoot
$env:PYTHONUTF8 = '1'
$env:PATH = "$uvBin;$ollamaBin;$env:PATH"
$env:UV_CACHE_DIR = Join-Path $WorkspaceRoot '.uv-cache'
$env:HF_HUB_CACHE = Join-Path $WorkspaceRoot '.hf-cache'
$env:OLLAMA_MODELS = Join-Path $WorkspaceRoot '.ollama\models'
$env:WORKFLOW_DEF_PATH = 'workflows/definitions/stt_check_notify.yaml'

.\.venv\Scripts\honcho.exe start -f Procfile -e .env
```

若要跑除外責任場景，只替換這一行：

```powershell
$env:WORKFLOW_DEF_PATH = 'workflows/definitions/stt_exclusion_notify.yaml'
```

成功時會依序看到 `ollama`、`litellm`、`stt`、`notified`、`agents` 啟動，最後包含：

```text
Uvicorn running on http://127.0.0.1:4000
Uvicorn running on http://127.0.0.1:8001
Uvicorn running on http://127.0.0.1:8002
Uvicorn running on http://127.0.0.1:8003
```

LiteLLM 的 cost map warning 與 Ollama 的舊 AMD driver warning 在目前實機屬非致命訊息；應以 listener、實際模型呼叫與後續錯誤為判斷依據。

### 9. Terminal C：驗證 services、models 與 API key

另開 PowerShell：

```powershell
$ports = 11434, 4000, 8001, 8002, 8003
$ports | ForEach-Object {
    [pscustomobject]@{
        Port = $_
        Listening = Test-NetConnection -ComputerName 127.0.0.1 -Port $_ -InformationLevel Quiet
    }
}
```

五個 port 都必須是 `True`。接著檢查 Ollama server 實際看到的模型：

```powershell
$ollamaModels = (Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags').models.name
$ollamaModels

if ($ollamaModels -notcontains 'qwen2.5:3b' -or
    $ollamaModels -notcontains 'bge-m3:latest') {
    throw 'Ollama 沒有讀到專案模型；請檢查 OLLAMA_MODELS 與啟動順序。'
}
```

檢查 LiteLLM alias：

```powershell
(Invoke-RestMethod -Uri 'http://127.0.0.1:4000/v1/models').data.id
```

預期包含：`local-qwen`、`local-embed`、`breeze-asr`、`claude-haiku`、`gemini-cheap`、`gemini-strong`。Alias 出現只代表 config 已載入，不代表 provider 一定能呼叫。

實際驗證 `gemini-cheap`：

```powershell
$geminiBody = @{
    model = 'gemini-cheap'
    messages = @(@{ role = 'user'; content = '只回覆 OK' })
} | ConvertTo-Json -Depth 5

$geminiResponse = Invoke-RestMethod `
    -Uri 'http://127.0.0.1:4000/v1/chat/completions' `
    -Method Post `
    -ContentType 'application/json' `
    -Body $geminiBody

$geminiResponse.choices[0].message.content
```

驗證 embedding：

```powershell
$embedBody = @{
    model = 'local-embed'
    input = @('embedding smoke test')
} | ConvertTo-Json -Depth 5

$embedResponse = Invoke-RestMethod `
    -Uri 'http://127.0.0.1:4000/v1/embeddings' `
    -Method Post `
    -ContentType 'application/json' `
    -Body $embedBody

$embedResponse.data[0].embedding.Count
```

embedding 維度應為 `1024`。

### 10. 除外責任 workflow：確認保單記憶

只有 `stt_exclusion_notify` 需要這一步。在 pgAdmin Query Tool 執行：

```sql
SELECT count(*) AS insurance_memory_rows
FROM store
WHERE prefix LIKE '_global.semantic.insurance_product.%';
```

目前資料來源 [kgi_ltc.yaml](data/insurance_product/kgi_ltc.yaml) 對應 59 筆。結果是 59 時直接跳過 seed；新的空資料庫或筆數不符時，先確認 5432、11434、4000 與 `local-embed`，再於 Terminal C 執行：

```powershell
$RepoRoot = 'D:\Projects\multi-agent平台架設\multi-agent-platform'
$WorkspaceRoot = Split-Path -Parent $RepoRoot
Set-Location -LiteralPath $RepoRoot
$env:PYTHONUTF8 = '1'
$env:UV_CACHE_DIR = Join-Path $WorkspaceRoot '.uv-cache'
.\.venv\Scripts\python.exe -m scripts.seed_insurance_memory
```

成功時最後顯示 `Seeded 59 item(s) total.`。腳本以固定 namespace／key upsert，可安全重跑；traceback 最後若只有 `KeyboardInterrupt`，代表程序被手動中止，不是 seed 自己回報資料錯誤。

### 11. Terminal B：啟動 event-driven workers

開啟新的 VS Code PowerShell，使用與 Terminal A **完全相同的 workflow YAML**：

```powershell
$RepoRoot = 'D:\Projects\multi-agent平台架設\multi-agent-platform'
$WorkspaceRoot = Split-Path -Parent $RepoRoot
$uvBin = Join-Path $env:USERPROFILE '.local\bin'

Set-Location -LiteralPath $RepoRoot
$env:PYTHONUTF8 = '1'
$env:PATH = "$uvBin;$env:PATH"
$env:UV_CACHE_DIR = Join-Path $WorkspaceRoot '.uv-cache'
$env:WORKFLOW_DEF_PATH = 'workflows/definitions/stt_check_notify.yaml'

.\.venv\Scripts\honcho.exe start -f Procfile.workers -e .env
```

除外責任場景同樣只替換：

```powershell
$env:WORKFLOW_DEF_PATH = 'workflows/definitions/stt_exclusion_notify.yaml'
```

正常輸出應包含：

```text
[master] starting
[worker:all] starting, steps=['check', 'notified', 'stt']
[memory-writer] starting
```

跑 event bus／orchestrator／parity smoke tests 前必須關閉這批 workers，避免相同 consumer group 搶走測試事件。

### 12. Terminal C：trigger workflow

PowerShell 5.1 呼叫原生 `.exe` 時會移除 JSON 內層雙引號，因此這裡使用已在目前 VS Code 環境驗證的 `\"` 寫法。

台積電場景：

```powershell
$RepoRoot = 'D:\Projects\multi-agent平台架設\multi-agent-platform'
Set-Location -LiteralPath $RepoRoot

.\.venv\Scripts\python.exe -m orchestrator.trigger `
    --workflow-def workflows/definitions/stt_check_notify.yaml `
    --payload '{\"audio_ref\":\"samples/gen_tsmc_01.wav\"}'
```

除外責任場景：

```powershell
$RepoRoot = 'D:\Projects\multi-agent平台架設\multi-agent-platform'
Set-Location -LiteralPath $RepoRoot

.\.venv\Scripts\python.exe -m orchestrator.trigger `
    --workflow-def workflows/definitions/stt_exclusion_notify.yaml `
    --payload '{\"audio_ref\":\"samples/gen_policy_01.wav\"}'
```

反引號必須是每行最後一個字元，後面不能有空白。PowerShell 7 可使用未跳脫的一般 JSON；完整指令如下：

```powershell
$RepoRoot = 'D:\Projects\multi-agent平台架設\multi-agent-platform'
Set-Location -LiteralPath $RepoRoot

.\.venv\Scripts\python.exe -m orchestrator.trigger `
    --workflow-def workflows/definitions/stt_check_notify.yaml `
    --payload '{"audio_ref":"samples/gen_tsmc_01.wav"}'
```

trigger 成功只代表命令已寫入 event bus，會印出：

```text
started run thread_id=<新的 UUID> workflow=<workflow 名稱>
```

請保存 `thread_id`。舊 run 已是 `needs_review`／`failed`／`completed` 時不能當成新 run 重用；修正設定後必須重新 trigger。

### 13. 查詢 workflow 狀態與 DB

Repository 內建查詢工具：

```powershell
$ThreadId = 'REPLACE_WITH_THREAD_ID'
.\.venv\Scripts\python.exe -m persistence.history $ThreadId
```

`REPLACE_WITH_THREAD_ID` 是佔位值，必須換成 trigger 印出的 UUID。

在 pgAdmin Query Tool 查最近的 runs：

```sql
SELECT thread_id, workflow_name, current_step, status,
       step_deadline_at, created_at, updated_at
FROM orchestrator_runs
ORDER BY updated_at DESC
LIMIT 10;
```

查指定 run：

```sql
SELECT thread_id, workflow_name, current_step, status,
       state_payload ->> 'review_reason' AS review_reason,
       step_deadline_at, updated_at
FROM orchestrator_runs
WHERE thread_id = '<thread_id>';
```

查每個 Agent、LLM、MCP tool 與 memory audit log：

```sql
SELECT created_at, node, kind, name, response_model,
       is_error, denied, latency_ms
FROM call_log
WHERE thread_id = '<thread_id>'
ORDER BY created_at;
```

查事件是否被正確 workflow 的 consumer group 接走：

```sql
SELECT e.topic, e.event_type, e.created_at,
       d.consumer_group, d.status, d.attempts,
       d.last_error, d.done_at
FROM event_log AS e
LEFT JOIN event_dispatch AS d
       ON d.event_log_id = e.id
WHERE e.thread_id = '<thread_id>'
ORDER BY e.id, d.id;
```

常見狀態：

| `orchestrator_runs.status` | 意義 |
|---|---|
| `running` | 尚在執行；搭配 `current_step` 與 `step_deadline_at` 判斷是否正常 |
| `completed` | 所有 step 已完成 |
| `needs_review` | 已停止自動推進，需要人工查看 `review_reason`／logs |
| `failed` | 已失敗，不會自動變回 running |

若某個 command event 存在但沒有 `event_dispatch`，通常表示 workers 訂閱了另一份 workflow；檢查 Terminal A、B 與 trigger 的 YAML 是否完全一致。

### 14. 在哪裡看即時 log

| 問題 | 先看哪裡 |
|---|---|
| 11434／模型目錄 | Terminal A 的 `ollama` |
| Gemini、alias、provider key | Terminal A 的 `litellm` |
| Breeze 模型／CUDA／轉錄 | Terminal A 的 `stt` |
| 通知 placeholder | Terminal A 的 `notified` |
| Agent／MCP 子程序 | Terminal A 的 `agents` |
| run 推進、deadline | Terminal B 的 `master` |
| step 執行 | Terminal B 的 `worker-all` |
| 記憶寫入 | Terminal B 的 `memory-writer` |
| 歷史結果 | `persistence.history`、`orchestrator_runs`、`call_log` |

成功的單次 event-driven run 應同時符合：trigger 有新 `thread_id`、workers 依序處理 `stt -> check -> notified`、run 最終為 `completed`、`call_log` 有相符的 model/tool/memory 紀錄。

### 15. 切換 workflow 的正確流程

1. 在 Terminal A、B 分別按 `Ctrl+C`。
2. 檢查五個 application ports；有殘留就執行 `stop_windows_stack.ps1`。
3. 確認 `.env` 沒有用 `WORKFLOW_DEF_PATH` 覆蓋 terminal 設定。
4. Terminal A 設定新 YAML後啟動 `Procfile`。
5. 驗證五個 ports 與 models。
6. Terminal B 設定同一份 YAML後啟動 `Procfile.workers`。
7. trigger 的 `--workflow-def` 使用同一份 YAML。
8. 取得新的 `thread_id`。

只改 trigger 不會熱切換已執行的 Runtime／workers。這正是 command event 存在但沒有正確 dispatch、最後在 `stt` deadline 進入 `needs_review` 的常見原因。

### 16. 關閉全部專案程序

先在 Terminal B、A 分別按 `Ctrl+C`，再檢查：

```powershell
11434, 4000, 8001, 8002, 8003 | ForEach-Object {
    $listener = Get-NetTCPConnection -State Listen -LocalPort $_ -ErrorAction SilentlyContinue
    [pscustomobject]@{
        Port = $_
        Listening = $null -ne $listener
        PID = if ($listener) { $listener.OwningProcess } else { $null }
    }
}
```

若仍有 listener：

```powershell
Set-Location -LiteralPath $RepoRoot
.\scripts\stop_windows_stack.ps1 -WhatIf
.\scripts\stop_windows_stack.ps1
```

不要用未限制 repository 範圍的批次 `Stop-Process -Force`。清理腳本不會停止 PostgreSQL 或 VS Code。

### 17. Windows 常見錯誤

| 錯誤／現象 | 原因 | 修正 |
|---|---|---|
| `UnicodeDecodeError: cp950` | 新 terminal 沒有先設定 `PYTHONUTF8` | 在同一 terminal 設 `$env:PYTHONUTF8='1'` 後再啟動 Honcho |
| `JSONDecodeError` | PowerShell 5.1 移除 JSON 內層引號 | 使用本指南的 `'{\"audio_ref\":...}'` 寫法 |
| 11434 已占用 | Ollama Desktop 已在背景執行 | 從系統列退出，確認 port 釋放，再讓 Procfile 啟動 |
| Alias 有但模型找不到 | Ollama server 讀到另一個模型目錄 | 在 server 啟動前設定 `OLLAMA_MODELS`，並查 `/api/tags` |
| `McpError: Connection closed` | MCP stdio 子程序的 uv cache／環境錯誤 | 設定 D 槽 `UV_CACHE_DIR`，確認使用含 Windows 修正的版本 |
| trigger 停在 `stt` 且沒有 call log | Runtime／workers 與 trigger 使用不同 workflow | 關閉兩批 Honcho，以相同 YAML 重啟並建立新 run |
| `pool-2`／`OSError(22)` | Honcho 孫程序、重複 workers 或 connection pools 殘留 | 使用範圍限定的關閉腳本清理後只啟動一組 |
| seed import 最後是 `KeyboardInterrupt` | 使用者在 import 完成前中止 | 先查 DB；已有 59 筆就跳過，空 DB 才重新執行 |

### 18. 靜態檢查

不啟動服務也能執行：

```powershell
.\.venv\Scripts\python.exe scripts\static_compat_check.py
.\.venv\Scripts\python.exe -m services.stt.temp_audio_smoke_test
```

需要 event bus 的 smoke／parity tests 前，務必先關閉 `Procfile.workers`。完整測試分層見 [docs/testing.md](docs/testing.md)。

## 原作者 macOS / Bash 安裝參考（保留原文）

### macOS / Bash / Claude Code（原作者流程，尚未由目前維護者重驗）

以下內容保留原作者的 macOS 操作脈絡，供上游差異比對；它不是目前 Windows 環境的直接操作指令。

#### 1. 專案本身

需要 Python 3.11+ 與 [uv](https://docs.astral.sh/uv/)。

```bash
git clone https://github.com/CCCChrissss/multi-agent-platform.git
cd multi-agent-platform
uv sync                      # 建 .venv 並裝好所有依賴（含 honcho、litellm）
```

#### 2. Postgres + pgvector

checkpointer、呼叫紀錄、event bus、run state、長期記憶全都存這裡。

```bash
brew install postgresql@14
brew services start postgresql@14
createdb agent_architecture
psql agent_architecture -c "CREATE EXTENSION vector;"    # 長期記憶的語意檢索用
```

驗證（要看到 `vector`）：

```bash
psql agent_architecture -c "\dx"
```

> `CREATE EXTENSION vector` 失敗代表本機 Postgres 沒有 pgvector。Homebrew 的 `postgresql@14` 沒有官方 build，要從[原始碼編譯安裝](https://github.com/pgvector/pgvector)——步驟見 [docs/setup.md](docs/setup.md)，背景見 [docs/long-term-memory-plan.md](docs/long-term-memory-plan.md) §1.3。
>
> 資料表**不用**手動建，各模組啟動時會自己 `CREATE TABLE IF NOT EXISTS`；只有 extension 這一步是手動的。

#### 3. 環境變數

```bash
cp .env.example .env
```

打開 `.env` 填：

- `ANTHROPIC_API_KEY`——目前只有仍宣告 `claude-haiku` 的 workflow / 工具才需要。預設的 `stt_check_notify` 不需要；`stt_exclusion_notify` 的 `check`／`notified` 仍需要。
- `GEMINI_API_KEY`——目前 `stt_check_notify` 的三個 step 與 `stt_exclusion_notify` 的 `stt` 都宣告 `gemini-cheap`，執行任一 workflow 都需要；[scripts/distill_procedural.py](scripts/distill_procedural.py) 的知識蒸餾與 [evals/run_eval.py](evals/run_eval.py) 的 Gemini 對照診斷也可能需要。

`PERSISTENCE_DATABASE_URL` 預設值對應上一步建的 DB，本機 Postgres 有設帳密才要改。

每個模組都會自己 `load_dotenv()`，所以不用手動 export。

#### 4. Ollama 與本機模型

```bash
brew install ollama
brew services start ollama   # 或另開 terminal 跑 `ollama serve`
ollama pull qwen2.5:3b       # 台積電場景的 check、以及 stt agent 用
ollama pull bge-m3           # 長期記憶的 embedding 用
```

`ollama pull` 需要 daemon 已經在跑（它是打去 `localhost:11434` 的 client 指令），所以先 `brew services start ollama` 再 pull。

驗證（要看到上面兩個模型）：

```bash
ollama list
```

> 用 `brew services` 讓 Ollama 常駐的話，要把 [Procfile](Procfile) 裡 `ollama:` 那行註解掉，不然下一步 `honcho start` 會撞 port 失敗。

#### 5. Breeze-ASR-25（自動）

第一次跑 `stt` 時會自動從 Hugging Face 下載（需要網路，之後快取在本機），不用預先準備。第一次呼叫會因此慢很多。Windows 的已驗證 CUDA、D 槽快取與免 FFmpeg 設定請另見 [docs/windows-setup.md](docs/windows-setup.md)。

---

## 原作者 macOS / Bash 執行參考（尚未於本機重驗）

以下段落保留原作者的執行方式與功能說明，作為上游設計參考。Windows 使用者不要直接執行其中的 `brew`、`export`、`lsof` 或 `pkill` 指令。

### 步驟 0：啟動常駐服務（僅限原作者 macOS / Bash 流程）

> [!WARNING]
> 以下 `bash` 指令中的「兩種模式」是指原作者的同步／事件驅動模式，不是指 Windows 與 macOS 共用。Windows 使用者請回到前面的 [Windows / PowerShell 執行](#windows--powershell-執行目前主線)，不要在 PowerShell 直接執行 `uv run honcho start`。

原作者在 macOS 上使用 [Procfile](Procfile) + [honcho](https://github.com/nickstenning/honcho) 一個指令啟動常駐服務：
Set-Location 'D:\Projects\multi-agent平台架設\multi-agent-platform'
$env:PYTHONUTF8 = '1'
$env:WORKFLOW_DEF_PATH = 'workflows/definitions/stt_policy_notify.yaml'
.\.venv\Scripts\honcho.exe start -f Procfile.workers -e .env
```bash
uv run honcho start
```

會起 5 個 process：

| process | port | 是什麼 |
|---|---|---|
| `ollama` | 11434 | 本機 LLM runtime |
| `litellm` | 4000 | LiteLLM Gateway，所有 LLM/STT 呼叫的統一入口 |
| `stt` | 8001 | STT service（Breeze-ASR-25 模型） |
| `notified` | 8002 | 通知 service（Slack/Gmail，目前是 placeholder） |
| `agents` | 8003 | agent runtime（[agents/runtime.py](agents/runtime.py)，`stt`/`check`/`notified` 三個 agent 都在同一個 process 裡，見下方「單一 runtime process」） |

這個指令會佔用這個 terminal、把 5 個服務的 log 用不同顏色 prefix 混在一起印出來；`Ctrl+C` 一次就會全部連帶關掉，不會留下殘留 process。

**確認有起來**（另開一個 terminal）：

```bash
# 5 個 port 都要是 LISTENING
for p in 11434 4000 8001 8002 8003; do
  printf "%s: " $p; lsof -ti :$p >/dev/null 2>&1 && echo OK || echo FAILED
done

# LiteLLM Gateway 讀到 gateway/config.yaml 的 6 個模型
curl -s http://localhost:4000/v1/models | python3 -m json.tool | grep '"id"'
```

第二個指令要印出 `local-qwen`、`claude-haiku`、`gemini-cheap`、`gemini-strong`、`breeze-asr`、`local-embed`。任何一項對不上，見 [docs/setup.md](docs/setup.md)。

> - 如果你已經用 `brew services start ollama` 讓 Ollama 開機自動啟動，把 [Procfile](Procfile) 裡的 `ollama:` 那行刪掉或註解掉，不然 honcho 啟動時會撞 port 失敗。
> - `mcp_servers/` 底下的 MCP server **不用**手動啟動——它們是薄薄一層 MCP 殼，`MCPGateway` 會在需要時把對應 server 當子行程開起來，用完自動關閉，實際邏輯還是打去上面的常駐 service。
> - 同步模式其實用不到 8003 這個 agent runtime，但一起起來也無妨。

#### 切換示範 workflow

> [!NOTE]
> 本小節仍是原作者的 macOS / Bash 寫法。Windows 必須使用前方 PowerShell 區段的 `$env:WORKFLOW_DEF_PATH = '...'` 與 `\.venv\Scripts\honcho.exe` 指令。

`check-agent`/`notified-agent`（上面這批常駐服務）以及事件驅動模式的 `master`/`worker`（下面 [Procfile.workers](Procfile.workers)）在**啟動時**讀 `WORKFLOW_DEF_PATH` 這個環境變數，決定要照哪一份 workflow 定義檔驗證/執行——不填預設是 `workflows/definitions/stt_check_notify.yaml`（台積電場景）。要跑除外責任場景，啟動**這兩批 process 時都要帶同一個值**：

```bash
export WORKFLOW_DEF_PATH=workflows/definitions/stt_exclusion_notify.yaml
uv run honcho start                        # 這批常駐服務
uv run honcho -f Procfile.workers start    # 事件驅動模式才需要這批
```

這是 process 啟動時的選擇，不是每次請求各自決定——同一批 process 同一時間只服務一個 workflow。要換回台積電場景，重新啟動兩批 process、不帶這個環境變數即可。

---

### 模式 A：同步（單一 process）

另開一個 terminal：

```bash
uv run python -m workflows.simple_pipeline
```

跑完會印出這次執行的 `thread_id`。預設讀 `samples/gen_tsmc_01.wav`，要換音檔得改 [workflows/simple_pipeline.py](workflows/simple_pipeline.py) 裡 `main()` 的 `audio_ref`（`samples/gen_tsmc_*.wav`、`samples/gen_other_*.wav` 是測試用的假音檔，一半提到台積電、一半沒有）。注意這個檔刻意凍結，改了 `parity_check.py` 會擋下來。

**崩潰後接續執行**：帶著同一個 `thread_id` 再跑一次，會自動從上次中斷的節點接續，不會重跑已完成的步驟。

```bash
uv run python -m workflows.simple_pipeline <thread_id>
```

---

### 模式 B：事件驅動（目前的主線）

這個模式下，Master Agent 跟每一個 step 的 worker 都是**各自獨立的長駐 process**。它們放在另一份 [Procfile.workers](Procfile.workers)，一樣一個指令啟動。

**B-1. 啟動 Master Agent、worker 與 memory-writer**（另開一個 terminal）

```bash
uv run honcho -f Procfile.workers start
```

| process | 是什麼 |
|---|---|
| `master` | Master Agent：收完成事件、決定下一步派給誰、更新 `orchestrator_runs` |
| `worker-all` | 認領 `stt`/`check`/`notified` 三步的命令（同一個 process 內三條迴圈），執行完發出完成事件——單一 process 是取捨後的結果，見 [Procfile.workers](Procfile.workers) 註解 |
| `memory-writer` | 背景蒸餾：訂閱同一批完成事件，依 workflow 定義把每步結果寫進長期記憶（[docs/long-term-memory-plan.md](docs/long-term-memory-plan.md) M3、[orchestrator/memory_writer.py](orchestrator/memory_writer.py)），寫入的都是 `pending` 狀態，要透過 `scripts/review_episodic.py` 或下方 demo UI 人工核准才會生效 |

`Ctrl+C` 一次全部關掉，行為跟 `honcho start` 一致。

> **為什麼不直接併進 [Procfile](Procfile)？** 因為 [orchestrator/smoke_test.py](orchestrator/smoke_test.py) 與 [workflows/parity_check.py](workflows/parity_check.py) 會在自己的 process 內起 master/worker，consumer group 跟這批完全同名。這批 process 如果在背景跑著，會跟測試搶同一批命令，測試裡刻意用假 handler 的情境就會被真 handler 接走。**跑 smoke test 前記得先關掉這個 Procfile**（詳見 [Procfile.workers](Procfile.workers) 的註解）。

**B-2. 觸發一次執行**（一次性指令，跑完就結束）

```bash
uv run python -m orchestrator.trigger \
    --workflow-def workflows/definitions/stt_check_notify.yaml \
    --payload '{"audio_ref": "samples/gen_tsmc_01.wav"}'
```

會印出這次執行的 `thread_id`，接著在 B-1 那個 terminal 就會看到 `stt -> check -> notified` 依序被推進。

`trigger.py` 完全不認識這個場景——它只收一份 workflow 定義檔和一包 JSON payload 就往下送，所以換一個 workflow 只要換 `--workflow-def` 跟 `--payload`，不用改任何程式碼。步驟順序、每步的事件名稱、輸入輸出欄位全部宣告在 [workflows/definitions/stt_check_notify.yaml](workflows/definitions/stt_check_notify.yaml) 裡。

### 跑另一個場景：除外責任

三件事都要做，少一件就不會動：

**① 先把保單條款灌進長期記憶**（只需做一次，做過就跳過）

> [!WARNING]
> 以下仍是 macOS / Bash 指令。Windows 請使用前方「Windows / PowerShell：除外責任場景的保單記憶」段落，並先查資料庫；目前 Windows 實機已經有完整 59 筆，不需要重跑。

```bash
uv run python -m scripts.seed_insurance_memory
```

這個場景的 `check` 不把條款塞進 prompt，而是去長期記憶裡查——沒灌過就查不到任何條文，`matched_articles` 永遠是空的（[docs/exclusion-scenario-plan.md](docs/exclusion-scenario-plan.md) P3）。

**② 兩批 honcho 都帶 `WORKFLOW_DEF_PATH` 重啟**（見上面「切換示範 workflow」）

**③ 觸發**

```bash
uv run python -m orchestrator.trigger \
    --workflow-def workflows/definitions/stt_exclusion_notify.yaml \
    --payload '{"audio_ref": "samples/gen_policy_01.wav"}'
```

> **跑完沒收到通知是正常的，不是壞掉。** 目前 `samples/` 底下沒有任何一個音檔會真的觸發通知——`gen_policy_01.wav` 是刻意設計的邊界案例（直覺答案「酒駕 → 除外 → 不賠」是錯的），`gen_policy_02.wav` 單純問長期照顧狀態怎麼認定，兩個都不涉及除外責任。真正會觸發的第三個案例目前只有逐字稿、沒有對應音檔。判斷理由見 [docs/exclusion-scenario-plan.md](docs/exclusion-scenario-plan.md) P4。

> 總共 3 個 terminal：`honcho start`（常駐服務）、`honcho -f Procfile.workers start`（編排）、trigger（一次性）。

---

## Demo UI

不寫指令、用瀏覽器操作的替代介面：組裝/測試 agent、瀏覽 workflow 設定、觸發執行、審核 `memory-writer` 寫入的 `pending` 記憶（approve/reject）。

Windows / PowerShell（尚未實機驗證）：

```powershell
.\.venv\Scripts\python.exe -m uvicorn demo.api:app --port 8010
```

macOS / Bash（原作者流程）：

```bash
uv run uvicorn demo.api:app --port 8010
```

啟動後直接用瀏覽器打開 [demo/index.html](demo/index.html)（本機檔案，不用另外起 static server——它是純前端，透過 CORS 打 `http://localhost:8010`）。需要上面 Postgres + `honcho start` 這批常駐服務已經在跑（catalog 讀 `gateway/config.yaml`、跑 workflow 打 8003 的 agent runtime）；要審核記憶則另外需要 `memory-writer`（`honcho -f Procfile.workers start`）先寫入過 `pending` 候選。

---

## 觀察執行結果

Windows / PowerShell：

```powershell
$ThreadId = 'REPLACE_WITH_THREAD_ID'
.\.venv\Scripts\python.exe -m persistence.history $ThreadId
```

macOS / Bash：

```bash
uv run python -m persistence.history <thread_id>
```

印每一步的 checkpoint 快照 + 每個 agent 內部的 LLM/tool 呼叫紀錄，兩種模式都可用。事件驅動模式怎麼直接查執行狀態、Postgres 各張表存什麼、`store` 跟 checkpoint 的差別，見 [docs/observability.md](docs/observability.md)。

---

## 驗證

專案主要使用可直接執行的 smoke test，而不是 pytest。

Windows / PowerShell：

```powershell
.\.venv\Scripts\python.exe -m event_bus.smoke_test
.\.venv\Scripts\python.exe -m orchestrator.smoke_test
.\.venv\Scripts\python.exe -m workflows.parity_check
.\.venv\Scripts\python.exe -m persistence.memory_smoke_test
```

macOS / Bash：

```bash
uv run python -m event_bus.smoke_test
uv run python -m orchestrator.smoke_test
uv run python -m workflows.parity_check
uv run python -m persistence.memory_smoke_test
```

⚠️ 跑前先關掉 `honcho -f Procfile.workers start`（consumer group 撞名，會搶走測試的命令）。各支的前置條件、記憶蒸餾 pipeline（P0-P5）手動試跑步驟，見 [docs/testing.md](docs/testing.md)。

commit `39d6449` 的 CI 曾因過時的 notified gather scenario 失敗。本機已把該 scenario 改成真正進入 `should_notify=true` 的並行路徑，並通過 gather、notify-agent、五個 dependency-free MCP smoke tests 與靜態檢查；目前未用 GitHub CLI 重查最新遠端 Actions。詳見 [docs/current-windows-status.md](docs/current-windows-status.md)。

---

## 關閉

兩個 Honcho terminal 各按一次 `Ctrl+C`，各自的 process 會連帶關閉；trigger 是一次性指令，demo UI 則在自己的 terminal 按 `Ctrl+C`。

Windows / PowerShell 關閉後確認 port；正常結果是五個 application port 都顯示 `False`：

```powershell
11434, 4000, 8001, 8002, 8003 | ForEach-Object {
    $listener = Get-NetTCPConnection -State Listen -LocalPort $_ -ErrorAction SilentlyContinue
    [pscustomobject]@{
        Port = $_
        Listening = $null -ne $listener
        PID = if ($listener) { $listener.OwningProcess } else { $null }
    }
}
```

Windows 上的 Honcho 可能已退出，但它啟動的 `uv`、Python、MCP 或 worker 孫程序仍然存活。實機曾因此同時留下多組 Master/worker/LiteLLM、33 條 idle PostgreSQL 連線，下一次 run 在 `stt` 出現 `error connecting in 'pool-2'` 與 `OSError(22, 'Invalid argument')`。若上表仍有 `True`、重啟時撞 port，或 workers terminal 已關閉但程序仍在，於 repository 根目錄先預覽再清理：

```powershell
Set-Location 'D:\Projects\multi-agent平台架設\multi-agent-platform'
.\scripts\stop_windows_stack.ps1 -WhatIf
.\scripts\stop_windows_stack.ps1
```

`-WhatIf` 只列出範圍，不會停止程序；第二個指令才會執行。腳本只比對本 repository 的 Honcho、LiteLLM、STT、notified、Agent Runtime、event-driven workers、其子程序，以及實際占用 11434 的 `ollama serve`；不會停止 PostgreSQL 或 VS Code。完成後預期 `5432=True`，其餘五個 port 都是 `False`。若 PowerShell execution policy 阻擋本機腳本，可只對這一次執行繞過：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\stop_windows_stack.ps1
```

macOS / Bash 如果 `Ctrl+C` 後還有殘留 process，可使用原作者的檢查與清理方式：

```bash
pkill -f "ollama serve"
pkill -f "litellm --config gateway/config.yaml"
pkill -f "uvicorn services."
pkill -f "uvicorn agents."
pkill -f "uvicorn demo.api"
pkill -f "workflows.event_driven_pipeline"
```

`pkill` 會終止符合樣式的 process，執行前應先用 `pgrep -af '<pattern>'` 確認目標。

---

## 專案結構

```
.env.example             環境變數範本，複製成 .env 後填值
Procfile                 honcho 用，一個指令啟動所有常駐服務（LLM gateway、service、agent service）
Procfile.workers         honcho 用，一個指令啟動事件驅動模式的 Master Agent + worker-all + memory-writer
workflows/               兩個入口：simple_pipeline.py（同步）、event_driven_pipeline.py（事件驅動）
workflows/definitions/   宣告式 workflow 定義（步驟順序、事件名稱、輸入輸出欄位）
orchestrator/            事件驅動編排：Master Agent、worker loop、run 狀態、外部觸發 CLI
event_bus/               EventBus 抽象 + Postgres 實作（Kafka backend 尚未實作，見 TODO.md）
agents/                  單一 runtime process（runtime.py）服務三個 agent + 各自的 client + 共用的 request/response envelope
llm/                     agent 的實際判斷邏輯（STT agent、台積電檢核、除外責任檢核、通知決策）
harness/                 agent loop 的共用機制（AgentLoopIncomplete、StallGuard）
gateway/                 LiteLLM Gateway 設定與 client（所有 LLM/STT 呼叫的統一入口）
mcp_servers/             MCPGateway（聚合多個 MCP server + RBAC，見 gateway.py、policy.yaml）+ 各個 MCP server
services/                真正幹活的常駐服務：STT（Breeze-ASR-25）、通知（Slack/Gmail，目前是 placeholder）
persistence/             LangGraph checkpointer + LLM/tool 呼叫紀錄 + 長期記憶（recall/browse/remember）+ 稽核歷史查詢，任何 workflow 共用
data/insurance_product/  除外責任場景的保單條款來源資料（seed 進長期記憶用，見 scripts/）
scripts/                 一次性腳本：seed_insurance_memory.py（保單條款）、記憶蒸餾 pipeline 的其餘幾支（見「驗證」章節）
                          stop_windows_stack.ps1（Windows Honcho 殘留程序安全清理）
evals/                   check 的評測案例（check_cases.yaml）+ runner（run_eval.py），M5 品質關卡用
samples/                 測試音檔（gen_tsmc_*/gen_other_* 是台積電場景，gen_policy_* 是除外責任場景）
demo/                    瀏覽器 UI（api.py 是 port 8010 的 FastAPI 後端、index.html 是純前端）：組裝/測試 agent、觸發 workflow、審核 memory-writer 寫入的 pending 記憶
transcribe.py            獨立的 ASR 測試腳本
docs/                    設計文件（見下）
.github/workflows/       CI（Windows dependency-free 相容檢查，以及 Ubuntu 上不需 Postgres/LLM 的 gather/MCP smoke tests）
```

## 進一步閱讀

- [AGENTS.md](AGENTS.md) — 平台目標、Codex/貢獻規範，以及「什麼算平台能力、什麼算場景邏輯」的判準
- [CLAUDE.md](CLAUDE.md) — 原作者的平台目標與 Claude Code 專案脈絡；保留給 macOS / Claude Code 工作流程
- [docs/current-windows-status.md](docs/current-windows-status.md) — 目前 Windows 實機狀態、已驗證範圍、阻擋項目與已知 CI 失敗
- [docs/README.md](docs/README.md) — 每份設計文件在講什麼、什麼時候該看，一份索引
- [TODO.md](TODO.md) — 已知缺口與尚未做的決策；[fixed.md](fixed.md) — 已經解決的
