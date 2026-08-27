# Multi-Agent Platform

公司內部多 agent 平台的原型（平台目標與開發規範見 [AGENTS.md](AGENTS.md)）。平台上目前有**兩個示範 workflow**，用來驗證基礎建設堪不堪用，都是同一個形狀：語音轉文字 → 檢核 → 通知。

> [!IMPORTANT]
> 本 repository 現在以 **Windows / PowerShell / Codex** 為主要操作環境。2026-08-27 的實機安裝狀態、已驗證範圍與目前阻擋項目，請先看 [docs/current-windows-status.md](docs/current-windows-status.md)。Breeze-ASR-25 已完成 CUDA / FP16 直接載入與範例音檔轉錄；五個常駐服務目前均已停止，所以尚未宣稱完整語音 workflow 已在本機跑通。

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

- **stt**：透過 `MCPGateway` 連上 [mcp_servers/stt](mcp_servers/stt/)（轉錄）與 [mcp_servers/format_check](mcp_servers/format_check/)（格式檢查）兩個 MCP server，透過 LiteLLM Gateway 呼叫 workflow 宣告的 LLM 自行決定要不要先檢查音檔格式、再進行轉錄（[llm/stt_agent.py](llm/stt_agent.py)）。目前 `stt_check_notify` 使用 `local-qwen`，`stt_exclusion_notify` 仍使用 `gemini-cheap`；兩者共用相同 agent 邏輯，但 model alias 由各自 YAML 決定。實際轉錄仍由 [Breeze-ASR-25](https://huggingface.co/MediaTek-Research/Breeze-ASR-25)（[services/stt/breeze_asr.py](services/stt/breeze_asr.py)）負責，不會改由 `local-qwen` 取代。
- **check**：兩個場景各自一套判斷邏輯，[agents/runtime.py](agents/runtime.py) 的 `/check/run` 路由依啟動時選的 workflow 決定呼叫哪一套（見下方「切換示範 workflow」）：
  - `stt_check_notify`：透過 LiteLLM Gateway 呼叫 LLM（`local-qwen`）判斷逐字稿是否提到台積電，並用確定性的別名比對當 backstop（[llm/tsmc_judge.py](llm/tsmc_judge.py)）。
  - `stt_exclusion_notify`：透過 LiteLLM Gateway 呼叫 LLM（`claude-haiku`）判斷客戶描述的情況是否涉及保單除外責任——不會把保單條款塞進 prompt，而是透過 [`browse_semantic_memory`](mcp_servers/memory/server.py) 這個 MCP tool 自己決定要往下鑽哪個分支，只把讀到過的條文拿來引用（[llm/exclusion_judge.py](llm/exclusion_judge.py)，詳見 [docs/exclusion-scenario-plan.md](docs/exclusion-scenario-plan.md)）。
- **notified**：兩個場景共用同一顆 agent，不知道場景邏輯——只收「要不要發、主旨、內容」，透過 `MCPGateway`（[mcp_servers/gateway.py](mcp_servers/gateway.py)）連上 [mcp_servers/notified](mcp_servers/notified/)（Slack / Gmail 兩個 tool，背後打 [services/notified/](services/notified/)）。`stt_check_notify` 目前宣告 `local-qwen`，`stt_exclusion_notify` 仍宣告 `claude-haiku`。`should_notify=false` 時會在呼叫 LLM / tool 前直接回傳 `[]`；需要通知時才由模型決定管道。目前 notified service 是本機 placeholder，不會真的對外寄送。

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

## Windows / PowerShell 從零開始

完整步驟與每一步的成功判斷統一維護在 [docs/windows-setup.md](docs/windows-setup.md)，錯誤分類見 [docs/setup.md](docs/setup.md)。以下只列目前這台電腦實際使用的路徑與最短入口。

```powershell
Set-Location 'D:\Projects\multi-agent平台架設\multi-agent-platform'
$env:PATH = 'C:\Users\User\.local\bin;C:\Users\User\AppData\Local\Programs\Ollama;' + $env:PATH
$env:UV_CACHE_DIR = 'D:\Projects\multi-agent平台架設\.uv-cache'
$env:HF_HUB_CACHE = 'D:\Projects\multi-agent平台架設\.hf-cache'
$env:OLLAMA_MODELS = 'D:\Projects\multi-agent平台架設\.ollama\models'
uv sync
Copy-Item .env.example .env  # 只有 .env 不存在時才執行；既有 .env 不要覆寫
```

本機已安裝 PostgreSQL 18.6、pgvector 0.8.6、`qwen2.5:3b` 與 `bge-m3`。資料庫密碼是安裝 PostgreSQL 時由使用者自行設定，不來自 repository，也不應寫進文件。

目前預設 workflow `stt_check_notify` 的三個 agent 決策模型都是 `local-qwen`，不需要 Anthropic / Gemini key；Breeze-ASR-25 已在這台電腦完成直接轉錄驗證。`stt_exclusion_notify` 尚未本機化，仍需要有效的 Anthropic / Gemini key。

### 原作者 macOS / Bash 安裝參考（尚未於本機重驗）

以下內容保留原作者的 macOS 操作脈絡，供上游差異比對；它不是目前 Windows 環境的直接操作指令。

### 1. 專案本身

需要 Python 3.11+ 與 [uv](https://docs.astral.sh/uv/)。

```bash
git clone https://github.com/CCCChrissss/multi-agent-platform.git
cd multi-agent-platform
uv sync                      # 建 .venv 並裝好所有依賴（含 honcho、litellm）
```

### 2. Postgres + pgvector

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

### 3. 環境變數

```bash
cp .env.example .env
```

打開 `.env` 填：

- `ANTHROPIC_API_KEY`——目前只有仍宣告 `claude-haiku` 的 workflow / 工具才需要。預設的 `stt_check_notify` 不需要；`stt_exclusion_notify` 的 `check`／`notified` 仍需要。
- `GEMINI_API_KEY`——`stt_exclusion_notify` 的 `stt` 仍宣告 `gemini-cheap`，因此執行該 workflow 時需要；[scripts/distill_procedural.py](scripts/distill_procedural.py) 的知識蒸餾與 [evals/run_eval.py](evals/run_eval.py) 的 Gemini 對照診斷也可能需要。只跑 `stt_check_notify` 且不執行這些工具時可以留空。

`PERSISTENCE_DATABASE_URL` 預設值對應上一步建的 DB，本機 Postgres 有設帳密才要改。

每個模組都會自己 `load_dotenv()`，所以不用手動 export。

### 4. Ollama 與本機模型

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

### 5. Breeze-ASR-25（Windows 本機已完成直接推論）

本機已安裝 `torch 2.13.0+cu132`，`torch.cuda.is_available()` 為 `True`，Breeze-ASR-25 快取放在 `D:\Projects\multi-agent平台架設\.hf-cache`。使用 RTX 4050 Laptop GPU 6 GB 對 `samples/gen_tsmc_01.wav` 直接推論成功，輸出「台積電今天股價創新高投資人非常關注」；峰值 CUDA allocated 約 4.09 GiB、reserved 約 4.51 GiB。

`services/stt/breeze_asr.py` 使用已安裝的 `librosa` / `soundfile` 先轉成單聲道 16 kHz，所以這條 Windows 路徑不需要另裝 FFmpeg。此處只代表「直接模型推論」通過；8001、LiteLLM `breeze-asr` alias 與完整 workflow 仍要另行驗證。

---

## Windows / PowerShell 執行（目前主線）

目前五個常駐服務均已停止。Breeze-ASR-25 與 CUDA PyTorch 已就緒；要開始下一階段的服務層驗證時，在 repository 根目錄開啟第一個 PowerShell：

```powershell
Set-Location 'D:\Projects\multi-agent平台架設\multi-agent-platform'
$env:PYTHONUTF8 = '1'
$env:PATH = 'C:\Users\User\.local\bin;C:\Users\User\AppData\Local\Programs\Ollama;' + $env:PATH
$env:UV_CACHE_DIR = 'D:\Projects\multi-agent平台架設\.uv-cache'
$env:HF_HUB_CACHE = 'D:\Projects\multi-agent平台架設\.hf-cache'
$env:OLLAMA_MODELS = 'D:\Projects\multi-agent平台架設\.ollama\models'
Remove-Item Env:WORKFLOW_DEF_PATH -ErrorAction SilentlyContinue  # 使用預設 stt_check_notify
.\.venv\Scripts\honcho.exe start -f Procfile -e .env
```

`PYTHONUTF8=1` 是這台繁體中文 Windows 的必要設定；否則 Honcho 2.0.0 可能用 CP950 讀 UTF-8 `.env`，產生 `UnicodeDecodeError`。如果 Windows 版 Ollama 已在背景占用 11434，先關閉該背景 process，再讓 Procfile 啟動 Ollama；同一個 port 不能同時啟動兩份服務。

另開第二個 PowerShell 檢查五個 port 與 LiteLLM alias：

```powershell
11434, 4000, 8001, 8002, 8003 | ForEach-Object {
    [pscustomobject]@{
        Port = $_
        Listening = Test-NetConnection -ComputerName 127.0.0.1 -Port $_ -InformationLevel Quiet
    }
}

(Invoke-RestMethod -Uri 'http://127.0.0.1:4000/v1/models').data.id
```

預期五個 port 都是 `True`，alias 清單包含 `local-qwen`、`local-embed`、`breeze-asr`、`claude-haiku`、`gemini-cheap`、`gemini-strong`。Alias 出現在清單只代表 LiteLLM 已載入設定；沒有 API key 的雲端 alias 仍不能實際呼叫。

確認本機模型的實際呼叫：

```powershell
$chatBody = @{
    model = 'local-qwen'
    messages = @(@{ role = 'user'; content = '只回覆 OK' })
} | ConvertTo-Json -Depth 5
Invoke-RestMethod -Uri 'http://127.0.0.1:4000/v1/chat/completions' `
    -Method Post -ContentType 'application/json' -Body $chatBody

$embedBody = @{ model = 'local-embed'; input = @('embedding smoke test') } |
    ConvertTo-Json -Depth 5
$embedResponse = Invoke-RestMethod -Uri 'http://127.0.0.1:4000/v1/embeddings' `
    -Method Post -ContentType 'application/json' -Body $embedBody
$embedResponse.data[0].embedding.Count
```

第二個指令預期維度為 `1024`。這兩項只驗證本機 LLM 與 embedding，不等於 Breeze-ASR 或完整 workflow 已通過。

事件驅動模式需要第三個 PowerShell，並且 `WORKFLOW_DEF_PATH` 必須與 Agent Runtime 使用同一份 YAML：

```powershell
Set-Location 'D:\Projects\multi-agent平台架設\multi-agent-platform'
$env:PYTHONUTF8 = '1'
$env:WORKFLOW_DEF_PATH = 'workflows/definitions/stt_check_notify.yaml'
.\.venv\Scripts\honcho.exe start -f Procfile.workers -e .env
```

觸發一次 workflow：

```powershell
.\.venv\Scripts\python.exe -m orchestrator.trigger `
    --workflow-def workflows/definitions/stt_check_notify.yaml `
    --payload '{"audio_ref":"samples/gen_tsmc_01.wav"}'
```

trigger 會印出 `thread_id`。用該值查執行結果與每次 LLM / MCP tool call：

```powershell
.\.venv\Scripts\python.exe -m persistence.history <thread_id>
```

完整的三個 terminal 配置、workflow 切換、SQL 查詢、預期輸出與錯誤排查見 [docs/windows-setup.md](docs/windows-setup.md) 與 [docs/observability.md](docs/observability.md)。Breeze 直接推論已就緒，但以上完整觸發流程仍是下一階段的操作程序，尚未在這台電腦完成端到端驗證。

## 原作者 macOS / Bash 執行參考（尚未於本機重驗）

以下段落保留原作者的執行方式與功能說明，作為上游設計參考。Windows 使用者不要直接執行其中的 `brew`、`export`、`lsof` 或 `pkill` 指令。

### 步驟 0：啟動常駐服務（兩種模式共用）

常駐服務用 [Procfile](Procfile) + [honcho](https://github.com/nickstenning/honcho) 一個指令全部啟動：

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

## Demo UI（保留功能；Windows 尚未實機驗證）

不寫指令、用瀏覽器操作的替代介面：組裝/測試 agent、瀏覽 workflow 設定、觸發執行、審核 `memory-writer` 寫入的 `pending` 記憶（approve/reject）。

```powershell
.\.venv\Scripts\python.exe -m uvicorn demo.api:app --port 8010
```

啟動後直接用瀏覽器打開 [demo/index.html](demo/index.html)（本機檔案，不用另外起 static server——它是純前端，透過 CORS 打 `http://localhost:8010`）。需要上面 Postgres + `honcho start` 這批常駐服務已經在跑（catalog 讀 `gateway/config.yaml`、跑 workflow 打 8003 的 agent runtime）；要審核記憶則另外需要 `memory-writer`（`honcho -f Procfile.workers start`）先寫入過 `pending` 候選。

---

## 觀察執行結果

```powershell
.\.venv\Scripts\python.exe -m persistence.history <thread_id>
```

印每一步的 checkpoint 快照 + 每個 agent 內部的 LLM/tool 呼叫紀錄，兩種模式都可用。事件驅動模式怎麼直接查執行狀態、Postgres 各張表存什麼、`store` 跟 checkpoint 的差別，見 [docs/observability.md](docs/observability.md)。

---

## 驗證

專案主要使用可直接執行的 smoke test，而不是 pytest。PowerShell 指令如下：

```powershell
.\.venv\Scripts\python.exe -m event_bus.smoke_test
.\.venv\Scripts\python.exe -m orchestrator.smoke_test
.\.venv\Scripts\python.exe -m workflows.parity_check
.\.venv\Scripts\python.exe -m persistence.memory_smoke_test
```

⚠️ 跑前先關掉 `honcho -f Procfile.workers start`（consumer group 撞名，會搶走測試的命令）。各支的前置條件、記憶蒸餾 pipeline（P0-P5）手動試跑步驟，見 [docs/testing.md](docs/testing.md)。

commit `39d6449` 的 CI 曾因過時的 notified gather scenario 失敗。本機已把該 scenario 改成真正進入 `should_notify=true` 的並行路徑，並通過 gather、notify-agent、五個 dependency-free MCP smoke tests 與靜態檢查；新的遠端 GitHub Actions 結果仍要等本次修正 push 後確認。詳見 [docs/current-windows-status.md](docs/current-windows-status.md)。

---

## 關閉

兩個 Honcho terminal 各按一次 `Ctrl+C`，各自的 process 會連帶關閉；trigger 是一次性指令，demo UI 則在自己的 terminal 按 `Ctrl+C`。關閉後用 PowerShell 確認 port：

```powershell
11434, 4000, 8001, 8002, 8003 | ForEach-Object {
    Get-NetTCPConnection -State Listen -LocalPort $_ -ErrorAction SilentlyContinue
}
```

沒有輸出代表這五個 port 已無 listener。若仍有輸出，先用回傳的 `OwningProcess` 搭配 `Get-Process -Id <PID>` 確認 process 身分；不要在未確認 PID 前批次強制終止。

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
evals/                   check 的評測案例（check_cases.yaml）+ runner（run_eval.py），M5 品質關卡用
samples/                 測試音檔（gen_tsmc_*/gen_other_* 是台積電場景，gen_policy_* 是除外責任場景）
demo/                    瀏覽器 UI（api.py 是 port 8010 的 FastAPI 後端、index.html 是純前端）：組裝/測試 agent、觸發 workflow、審核 memory-writer 寫入的 pending 記憶
transcribe.py            獨立的 ASR 測試腳本
docs/                    設計文件（見下）
.github/workflows/       CI（Windows dependency-free 相容檢查，以及 Ubuntu 上不需 Postgres/LLM 的 gather/MCP smoke tests）
```

## 進一步閱讀

- [AGENTS.md](AGENTS.md) — 平台目標、Codex/貢獻規範，以及「什麼算平台能力、什麼算場景邏輯」的判準
- [docs/current-windows-status.md](docs/current-windows-status.md) — 目前 Windows 實機狀態、已驗證範圍、阻擋項目與已知 CI 失敗
- [docs/README.md](docs/README.md) — 每份設計文件在講什麼、什麼時候該看，一份索引
- [TODO.md](TODO.md) — 已知缺口與尚未做的決策；[fixed.md](fixed.md) — 已經解決的
