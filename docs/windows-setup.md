# Windows / PowerShell 安裝與 workflow 操作

本文件是這個 repository 的 Windows 主操作手冊。開始前先看 [current-windows-status.md](current-windows-status.md)：它記錄哪些項目已在本機驗證、哪些只是保留的上游功能。

> [!IMPORTANT]
> 2026-08-27 的狀態是：PostgreSQL、pgvector、Ollama、`local-qwen`、`local-embed` 與五個常駐 service 都曾個別驗證；Breeze-ASR-25 也已完成 CUDA / FP16 直接載入與範例音檔轉錄。目前 service 已停止，8001、LiteLLM alias 與完整 event-driven workflow 尚未重驗，所以仍不宣稱端到端跑通。

## 1. 目前使用的本機路徑

| 用途 | 路徑 |
|---|---|
| Repository | `D:\Projects\multi-agent平台架設\multi-agent-platform` |
| uv | `C:\Users\User\.local\bin\uv.exe` |
| Python 虛擬環境 | `D:\Projects\multi-agent平台架設\multi-agent-platform\.venv` |
| uv cache | `D:\Projects\multi-agent平台架設\.uv-cache` |
| Hugging Face cache | `D:\Projects\multi-agent平台架設\.hf-cache` |
| Ollama | `C:\Users\User\AppData\Local\Programs\Ollama\ollama.exe` |
| Ollama models | `D:\Projects\multi-agent平台架設\.ollama\models` |
| PostgreSQL 測試資料庫 | `agent_architecture_test` |

路徑含中文與連字號，PowerShell 指令應以單引號包住完整路徑。

## 2. 前置工具

專案基準是 [.python-version](../.python-version) 指定的 Python 3.11。需要：

- Git for Windows
- uv
- Python 3.11（可由 uv 管理）
- PostgreSQL + pgvector
- Ollama

在 repository 根目錄檢查：

```powershell
Set-Location 'D:\Projects\multi-agent平台架設\multi-agent-platform'
git --version
& 'C:\Users\User\.local\bin\uv.exe' --version
.\.venv\Scripts\python.exe --version
```

最後一行應顯示 Python 3.11.x。

### 尚未建立 `.venv` 時

```powershell
Set-Location 'D:\Projects\multi-agent平台架設\multi-agent-platform'
$env:UV_CACHE_DIR = 'D:\Projects\multi-agent平台架設\.uv-cache'
$env:HF_HUB_CACHE = 'D:\Projects\multi-agent平台架設\.hf-cache'
& 'C:\Users\User\.local\bin\uv.exe' python install 3.11
& 'C:\Users\User\.local\bin\uv.exe' sync
```

`uv sync` 依 [pyproject.toml](../pyproject.toml) 與 [uv.lock](../uv.lock) 建立環境。Windows 會從 PyTorch 官方 `cu132` index 安裝 `torch 2.13.0+cu132`；這台電腦已完成。除非 lockfile 或依賴改變，不必每次啟動服務都重跑。

驗證 CUDA：

```powershell
.\.venv\Scripts\python.exe -c "import torch; print(torch.__version__); print(torch.version.cuda); print(torch.cuda.is_available()); print(torch.cuda.get_device_name(0))"
```

本機預期為 `2.13.0+cu132`、`13.2`、`True`與 `NVIDIA GeForce RTX 4050 Laptop GPU`。

## 3. PostgreSQL 與 pgvector

本機已驗證 PostgreSQL 18.6、資料庫 `agent_architecture_test`、pgvector 0.8.6。資料庫密碼是安裝 PostgreSQL 時由使用者自行設定，不是 repository 產生的密碼。

最容易確認的方式是用 pgAdmin 4：

1. 展開 `Servers > PostgreSQL 18 > Databases > agent_architecture_test`。
2. 右鍵資料庫，選 `Query Tool`。
3. 執行：

```sql
CREATE EXTENSION IF NOT EXISTS vector;

SELECT extversion
FROM pg_extension
WHERE extname = 'vector';
```

查詢結果應有一列；本機已驗證版本為 `0.8.6`。

也可以在 PostgreSQL `bin` 已加入 PATH 時使用 PowerShell：

```powershell
psql -d agent_architecture_test -c "SELECT extversion FROM pg_extension WHERE extname = 'vector';"
```

`PERSISTENCE_DATABASE_URL` 必須使用你自己的帳號、密碼與資料庫名稱。不要把真實密碼貼進 README、`.env.example`、commit、issue 或聊天截圖。

## 4. 建立 `.env`

只在 `.env` 不存在時複製範本：

```powershell
Set-Location 'D:\Projects\multi-agent平台架設\multi-agent-platform'
if (-not (Test-Path -LiteralPath '.env')) {
    Copy-Item -LiteralPath '.env.example' -Destination '.env'
}
```

打開 `.env`，將 `PERSISTENCE_DATABASE_URL` 改成實際連線資訊，例如：

```text
PERSISTENCE_DATABASE_URL=postgresql://postgres:YOUR_PASSWORD@localhost:5432/agent_architecture_test
```

`YOUR_PASSWORD` 只是明確的佔位字串，必須換成安裝 PostgreSQL 時設定的密碼。

目前金鑰規則：

- 預設 `stt_check_notify` 的三個 agent step 都使用 `local-qwen`，不需要 `ANTHROPIC_API_KEY` 或 `GEMINI_API_KEY`。
- `stt_exclusion_notify` 仍使用 `gemini-cheap` 與 `claude-haiku`，沒有有效金鑰時不能執行。
- 不要為本機測試填假 key，也不要把真實 key 寫進 `.env.example`。

## 5. Ollama 與本機模型

每次開新的 PowerShell，先設定這台電腦使用的路徑：

```powershell
$env:PATH = 'C:\Users\User\AppData\Local\Programs\Ollama;' + $env:PATH
$env:OLLAMA_MODELS = 'D:\Projects\multi-agent平台架設\.ollama\models'
```

確認已下載模型：

```powershell
ollama list
```

預期至少包含：

- `qwen2.5:3b`
- `bge-m3`

如果只想單獨啟動 Ollama，可執行 `ollama serve`。最簡單的完整服務路徑則是讓 [Procfile](../Procfile) 啟動它；啟動前先確認 11434 沒有被 Windows 背景版 Ollama 占用。

```powershell
Test-NetConnection -ComputerName 127.0.0.1 -Port 11434 -InformationLevel Quiet
```

回傳 `False` 表示 port 目前空閒；可交給 Honcho 啟動。回傳 `True` 表示已經有 listener，不能再啟動第二份 `ollama serve`。

## 6. 選擇 workflow

Agent Runtime 與 event-driven workers 都在 **process 啟動時**讀取 `WORKFLOW_DEF_PATH`。兩批 process 必須使用相同檔案。

最簡單、目前優先驗證的 workflow：

```powershell
$env:WORKFLOW_DEF_PATH = 'workflows/definitions/stt_check_notify.yaml'
```

若完全不設定，程式預設也是這一份。

除外責任 workflow：

```powershell
$env:WORKFLOW_DEF_PATH = 'workflows/definitions/stt_exclusion_notify.yaml'
```

但這份目前仍需要 Anthropic / Gemini key，尚未納入無雲端金鑰的本機路徑。只在之後具備 provider 條件時使用。

`orchestrator.trigger --workflow-def` 只決定這次要觸發哪份定義，不能替已啟動的 Runtime / workers 切換設定。切換 workflow 時，兩批 Honcho 都要停止、設定同一個 `$env:WORKFLOW_DEF_PATH`，再重新啟動。

## 7. 啟動五個常駐服務

第一個 PowerShell：

```powershell
Set-Location 'D:\Projects\multi-agent平台架設\multi-agent-platform'
$env:PYTHONUTF8 = '1'
$env:PATH = 'C:\Users\User\.local\bin;C:\Users\User\AppData\Local\Programs\Ollama;' + $env:PATH
$env:UV_CACHE_DIR = 'D:\Projects\multi-agent平台架設\.uv-cache'
$env:HF_HUB_CACHE = 'D:\Projects\multi-agent平台架設\.hf-cache'
$env:OLLAMA_MODELS = 'D:\Projects\multi-agent平台架設\.ollama\models'
$env:WORKFLOW_DEF_PATH = 'workflows/definitions/stt_check_notify.yaml'
.\.venv\Scripts\honcho.exe start -f Procfile -e .env
```

這會嘗試啟動：

| Honcho process | Port | 功能 |
|---|---:|---|
| `ollama` | 11434 | 本機模型 runtime |
| `litellm` | 4000 | 統一模型 gateway |
| `stt` | 8001 | Breeze-ASR STT service |
| `notified` | 8002 | 本機通知 placeholder |
| `agents` | 8003 | 三個 agent route 的共用 runtime |

`PYTHONUTF8=1` 是已實際遇過的 Windows 修正。Honcho 2.0.0 若用 CP950 解碼 UTF-8 `.env`，會出現：

```text
UnicodeDecodeError: 'cp950' codec can't decode byte ...
```

設定後必須在同一個 PowerShell 啟動 Honcho。

## 8. 確認 11434 / 4000 / 8001 / 8002 / 8003

第二個 PowerShell：

```powershell
11434, 4000, 8001, 8002, 8003 | ForEach-Object {
    [pscustomobject]@{
        Port = $_
        Listening = Test-NetConnection -ComputerName 127.0.0.1 -Port $_ -InformationLevel Quiet
    }
}
```

全部顯示 `True` 才表示五個 process 都有 listener。這些 service 沒有一致的 `/health` endpoint，因此 port 檢查只是第一層；還要檢查 LiteLLM alias 與實際模型呼叫。

```powershell
(Invoke-RestMethod -Uri 'http://127.0.0.1:4000/v1/models').data.id
```

預期看到：

- `local-qwen`
- `local-embed`
- `breeze-asr`
- `claude-haiku`
- `gemini-cheap`
- `gemini-strong`

注意：alias 出現在 `/v1/models` 不代表背後 provider 一定可用。沒有 key 的 Anthropic / Gemini alias 仍會在實際呼叫時失敗。

## 9. 確認 `local-qwen` 與 `local-embed`

### `local-qwen`

```powershell
$chatBody = @{
    model = 'local-qwen'
    messages = @(@{ role = 'user'; content = '只回覆 OK' })
} | ConvertTo-Json -Depth 5

$chatResponse = Invoke-RestMethod `
    -Uri 'http://127.0.0.1:4000/v1/chat/completions' `
    -Method Post `
    -ContentType 'application/json' `
    -Body $chatBody

$chatResponse.choices[0].message.content
```

成功時會看到模型文字回覆。

### `local-embed`

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

`bge-m3` 在這個專案的 embedding 維度應為 `1024`。

## 10. Agent Runtime 基本 request

這個 request 不經過 STT，可獨立確認 port 8003 的 `check` route 與 `local-qwen`：

```powershell
$agentBody = @{
    thread_id = "manual-check-$([guid]::NewGuid())"
    input = @{ transcript = '今天討論台積電的技術發展。' }
    context = @{ tenant_id = 'default' }
} | ConvertTo-Json -Depth 5

Invoke-RestMethod `
    -Uri 'http://127.0.0.1:8003/check/run' `
    -Method Post `
    -ContentType 'application/json' `
    -Body $agentBody
```

成功時預期 `status` 是 `ok`，`output.mentions_tsmc` 是 `True`。這只驗證 check agent，不等於 STT 或完整 workflow 成功。

## 11. 啟動 event-driven workers

第三個 PowerShell：

```powershell
Set-Location 'D:\Projects\multi-agent平台架設\multi-agent-platform'
$env:PYTHONUTF8 = '1'
$env:WORKFLOW_DEF_PATH = 'workflows/definitions/stt_check_notify.yaml'
.\.venv\Scripts\honcho.exe start -f Procfile.workers -e .env
```

這批 process 包含 Master Agent、`worker-all` 與 `memory-writer`。跑 smoke test 前必須先關閉這批 workers，否則相同 consumer group 可能搶走測試事件。

## 12. 觸發 workflow

第四個 PowerShell或 workers 以外的任一個 PowerShell：

```powershell
Set-Location 'D:\Projects\multi-agent平台架設\multi-agent-platform'
.\.venv\Scripts\python.exe -m orchestrator.trigger `
    --workflow-def workflows/definitions/stt_check_notify.yaml `
    --payload '{"audio_ref":"samples/gen_tsmc_01.wav"}'
```

命令成功送出後會印出 `thread_id`。請先複製保存；後續查詢都以它為索引。

> [!WARNING]
> Breeze-ASR-25 直接推論已就緒，但這條完整觸發路徑仍是下一階段程序，不是 2026-08-27 已完成的端到端驗證結果。

## 13. 查看結果、thread_id 與 Agent / MCP log

### 用 repository 內建工具

```powershell
.\.venv\Scripts\python.exe -m persistence.history <thread_id>
```

輸出包含 checkpoint 與 `call_log`。詳細欄位見 [observability.md](observability.md)。

### 用 pgAdmin Query Tool

查最近五次事件驅動 run：

```sql
SELECT thread_id, workflow_name, current_step, status, updated_at
FROM orchestrator_runs
ORDER BY updated_at DESC
LIMIT 5;
```

查某次 run 的 LLM、MCP tool 與 memory 稽核紀錄：

```sql
SELECT created_at, node, kind, name, is_error, latency_ms
FROM call_log
WHERE thread_id = '<thread_id>'
ORDER BY created_at;
```

五個 service 的即時 log 在第一個 Honcho terminal；Master / worker / memory-writer 的 log 在第二個 Honcho terminal。每一行前面的 process 名稱就是排查入口。

## 14. 正常成功時應看到什麼

完整事件驅動 workflow 成功時，應同時符合：

1. trigger 印出一個新的 `thread_id`。
2. workers log 依序處理 `stt`、`check`、`notified`。
3. `orchestrator_runs.status` 最後是 `completed`。
4. `persistence.history` 顯示各 step 的 checkpoint / state。
5. `call_log` 可看到對應 agent 的 LLM、tool 或 memory 紀錄。
6. `stt_check_notify` 的 LLM model name 應是 `local-qwen`；embedding 仍應是 `local-embed`。
7. `should_notify=false` 時 `notified_log=[]` 是正常結果，而且不應產生通知 LLM / tool call。

目前本機尚未同時達成上述完整條件，因此不能把「服務能啟動」等同於「workflow 已成功」。

## 15. 失敗時依 process / port / log 排查

| 現象 | 先查 port / process | 主要 log | 常見原因 |
|---|---|---|---|
| Ollama 無法啟動 | 11434 / `ollama` | 第一個 Honcho terminal 的 `ollama` | Windows 背景版 Ollama 已占用 port；`OLLAMA_MODELS` 沒設到 D 槽 |
| LiteLLM 無法連線 | 4000 / `litellm` | `litellm` | config 載入錯誤、Ollama 不可達；雲端 alias 真正呼叫時另需 key |
| STT 失敗或卡住 | 8001 / `stt` | `stt` | `HF_HUB_CACHE` 未指向 D 槽已下載權重、GPU 被其他程式佔滿、CUDA OOM、音檔損壞或編碼不支援 |
| 通知失敗 | 8002 / `notified` | `notified` | service 未啟動；目前只是假實作，不會真的寄信或送 Slack |
| Agent HTTP 失敗 | 8003 / `agents` | `agents` | workflow YAML load 失敗、LiteLLM 不可達、input schema 不符 |
| trigger 後沒推進 | `master` / `worker-all` | workers terminal | `Procfile.workers` 未啟動、兩批 process 的 `WORKFLOW_DEF_PATH` 不一致、PostgreSQL / event bus 問題 |
| 查不到 run | PostgreSQL | trigger 與 `master` | 觸發未成功、查錯資料庫、`PERSISTENCE_DATABASE_URL` 不一致 |
| `UnicodeDecodeError: cp950` | `honcho` | 啟動 terminal | 啟動前沒有在同一個 PowerShell 設定 `$env:PYTHONUTF8='1'` |

## 16. 關閉

在兩個 Honcho terminal 分別按 `Ctrl+C`。確認沒有 listener：

```powershell
11434, 4000, 8001, 8002, 8003 | ForEach-Object {
    Get-NetTCPConnection -State Listen -LocalPort $_ -ErrorAction SilentlyContinue
}
```

若仍有輸出，先檢查 process，再決定是否終止：

```powershell
Get-NetTCPConnection -State Listen -LocalPort 4000 |
    Select-Object LocalAddress, LocalPort, OwningProcess
Get-Process -Id <PID>
```

不要在未確認 PID 與用途前批次 `Stop-Process -Force`。

## 17. 安裝前或服務停止時可做的靜態檢查

```powershell
.\.venv\Scripts\python.exe scripts/static_compat_check.py
.\.venv\Scripts\python.exe -m services.stt.temp_audio_smoke_test
```

第一項檢查 Python 語法、編碼、設定引用與 Markdown 連結；第二項驗證 Windows 暫存音檔行為。它們不會驗證 PostgreSQL、模型、LLM API 或完整 workflow。

其他 smoke tests、前置條件與目前已知 CI 失敗見 [testing.md](testing.md)。
