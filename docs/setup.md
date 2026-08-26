# 安裝與執行疑難排解

本文件以目前的 Windows / PowerShell 環境為主。正常安裝與完整操作步驟見 [windows-setup.md](windows-setup.md)，目前實機狀態見 [current-windows-status.md](current-windows-status.md)。

## 先判斷是哪一層失敗

| 層級 | Port / 元件 | 最先檢查 |
|---|---|---|
| 本機模型 | Ollama / 11434 | `ollama list`、`OLLAMA_MODELS`、是否重複啟動 |
| 模型 Gateway | LiteLLM / 4000 | `/v1/models`、`litellm` log、實際 alias 呼叫 |
| 語音辨識 | STT / 8001 | Breeze 權重、PyTorch/CUDA、`stt` log |
| 通知 | notified / 8002 | `notified` log；目前只是假實作 |
| Agent | Runtime / 8003 | `agents` log、workflow load、input schema |
| 編排 | master / workers | `WORKFLOW_DEF_PATH`、PostgreSQL、event bus log |
| 持久層 | PostgreSQL | `.env` 連線字串、資料庫名稱、pgvector |

## Windows：Honcho 讀 `.env` 出現 CP950 錯誤

### 錯誤

```text
UnicodeDecodeError: 'cp950' codec can't decode byte ...
```

### 原因

Honcho 2.0.0 在繁體中文 Windows 上可能以 CP950 解碼 `.env`，但檔案是 UTF-8。

### 修正

在同一個 PowerShell 設定 UTF-8 mode，再直接呼叫虛擬環境內的 Honcho：

```powershell
Set-Location 'D:\Projects\multi-agent平台架設\multi-agent-platform'
$env:PYTHONUTF8 = '1'
.\.venv\Scripts\honcho.exe start -f Procfile -e .env
```

### 驗證

不再出現 `UnicodeDecodeError`，且 Honcho 開始輸出 `ollama`、`litellm`、`stt`、`notified`、`agents` 的 process log。

## PostgreSQL / pgvector

### `password authentication failed`

`.env` 中的帳號或密碼與 PostgreSQL 安裝時設定的不一致。密碼不是由 repository 產生，必須使用你安裝 PostgreSQL 時設定的值。

```text
PERSISTENCE_DATABASE_URL=postgresql://postgres:YOUR_PASSWORD@localhost:5432/agent_architecture_test
```

`YOUR_PASSWORD` 必須換成真實密碼，但不能提交到 Git。

### `KeyError: 'PERSISTENCE_DATABASE_URL'`

確認 `.env` 位於 repository 根目錄，而且含有該變數：

```powershell
Set-Location 'D:\Projects\multi-agent平台架設\multi-agent-platform'
Test-Path -LiteralPath '.env'
Select-String -LiteralPath '.env' -Pattern '^PERSISTENCE_DATABASE_URL='
```

第二個指令會顯示整行連線字串，可能包含密碼；只在自己的 terminal 查看，不要貼到 issue、文件或截圖。

### `extension "vector" is not available`

代表 PostgreSQL 已安裝，但目前 server 找不到 pgvector extension。先在 pgAdmin Query Tool 查：

```sql
SELECT version();
SHOW config_file;
SELECT * FROM pg_available_extensions WHERE name = 'vector';
```

本機已使用 PostgreSQL 18.6 與 pgvector 0.8.6 驗證成功。若查不到 `vector`，要確認 pgvector 安裝檔放入的是同一套 PostgreSQL 18，而不是另一個版本的目錄。

## Ollama

### `ollama` 指令找不到

```powershell
$env:PATH = 'C:\Users\User\AppData\Local\Programs\Ollama;' + $env:PATH
ollama --version
```

### `ollama pull` 或 LiteLLM 無法連到 11434

`ollama pull` 是 client 指令，必須先有 Ollama server：

```powershell
$env:OLLAMA_MODELS = 'D:\Projects\multi-agent平台架設\.ollama\models'
ollama serve
```

如果改由 Honcho 啟動，就不要另外執行 `ollama serve`。

### Honcho 的 `ollama` process 一啟動就失敗

通常是 Windows 背景版 Ollama 已占用 11434：

```powershell
Get-NetTCPConnection -State Listen -LocalPort 11434 -ErrorAction SilentlyContinue |
    Select-Object LocalAddress, LocalPort, OwningProcess
```

用 `Get-Process -Id <PID>` 確認後，二選一：關閉背景 Ollama，讓 Procfile 管理；或另行規劃不重複啟動 Ollama 的 Procfile。不要直接啟動兩份。

### `ollama list` 看不到已下載模型

這台電腦把模型放在 D 槽。新的 PowerShell 若沒設定 `OLLAMA_MODELS`，Ollama 可能讀到預設 C 槽目錄：

```powershell
$env:OLLAMA_MODELS = 'D:\Projects\multi-agent平台架設\.ollama\models'
ollama list
```

預期至少看到 `qwen2.5:3b` 與 `bge-m3`。

## LiteLLM Gateway

### 4000 沒有 listener

先看第一個 Honcho terminal 的 `litellm` log。確認 11434 已啟動、[gateway/config.yaml](../gateway/config.yaml) 能載入。

```powershell
Test-NetConnection -ComputerName 127.0.0.1 -Port 4000 -InformationLevel Quiet
```

### `/v1/models` 有 alias，但呼叫仍失敗

`/v1/models` 只證明 alias 已載入，不證明 provider credential 或底層服務可用。

- `local-qwen`：確認 Ollama 與 `qwen2.5:3b`。
- `local-embed`：確認 Ollama 與 `bge-m3`。
- `breeze-asr`：確認 8001 與 Breeze 模型環境。
- `claude-haiku`：需要有效 `ANTHROPIC_API_KEY`。
- `gemini-cheap` / `gemini-strong`：需要有效 `GEMINI_API_KEY`。

目前沒有 Anthropic / Gemini key，因此雲端 alias 保留但不可實際使用；不要填假 credential。

## STT / Breeze-ASR-25

### 8001 能啟動，但第一次轉錄失敗或長時間等待

Port listener 只代表 FastAPI process 已啟動，不代表 Breeze 權重與推論 backend 已就緒。這台電腦目前：

- Breeze-ASR-25 權重尚未下載完成。
- `.venv` 是 `torch 2.13.0+cpu`。
- `torch.cuda.is_available()` 是 `False`。

因此完整 STT 尚未驗證。下一階段需要先確認相容的 CUDA / PyTorch 組合與 6 GB VRAM 下的模型執行策略，再下載模型；本文件不把未執行過的安裝命令當成既定答案。

## Workflow 選擇與 event-driven workers

### 改了 `--workflow-def`，Runtime 行為沒有變

Agent Runtime 與 workers 是在啟動時讀 `WORKFLOW_DEF_PATH`。trigger 的 `--workflow-def` 不會熱切換它們。

```powershell
$env:WORKFLOW_DEF_PATH = 'workflows/definitions/stt_check_notify.yaml'
```

兩批 Honcho 都要在各自的 PowerShell 設定相同值後重啟。

### trigger 印出 thread_id，但 workflow 沒有推進

先確認：

1. `Procfile.workers` 那批 process 已啟動。
2. `master` 與 `worker-all` 沒有錯誤。
3. 兩批 process 的 `WORKFLOW_DEF_PATH` 相同。
4. `PERSISTENCE_DATABASE_URL` 指向同一個可連線資料庫。
5. `orchestrator_runs` 是否停在某個 `current_step`。

### `stt_exclusion_notify` 查不到條文

這是上游場景的必要前置，原作者流程要求先執行：

```powershell
.\.venv\Scripts\python.exe -m scripts.seed_insurance_memory
```

但這個 workflow 目前仍依賴 Anthropic / Gemini key，本機尚未完成無雲端金鑰改造與端到端驗證；保留此步驟作為上游功能參考。

## Smoke test

### `gather_concurrency_smoke_test.py` 失敗

目前已知失敗：測試預期不通知時是 `["no notification needed"]`，但 [llm/notify_agent.py](../llm/notify_agent.py) 的安全短路已回傳 `[]`。這是測試預期落後於程式行為；本文件階段不修改測試。

### Smoke test 偶發收到錯的事件

跑測試前先關閉 `Procfile.workers`，因為常駐 worker 與測試可能使用相同 consumer group，會互相搶事件。

完整測試矩陣與前置條件見 [testing.md](testing.md)。

## 原作者 macOS / Bash 參考（未於目前 Windows 環境重驗）

以下內容只保留上游操作脈絡，不是 Windows 指令：

```bash
brew install postgresql@14
brew services start postgresql@14
createdb agent_architecture
psql agent_architecture -c "CREATE EXTENSION vector;"

brew install ollama
brew services start ollama
ollama pull qwen2.5:3b
ollama pull bge-m3

export WORKFLOW_DEF_PATH=workflows/definitions/stt_exclusion_notify.yaml
uv run honcho start
uv run honcho -f Procfile.workers start
```

pgvector 在 Homebrew PostgreSQL 的原始碼編譯背景與歷史決策仍保留在 [long-term-memory-plan.md](long-term-memory-plan.md)。Windows 使用者不要直接執行 `brew`、`make`、`lsof` 或 `/opt/homebrew` 路徑。
