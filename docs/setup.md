# 安裝與執行疑難排解

本文件同時保留 Windows / PowerShell 與 macOS / Bash 的疑難排解。Windows 正常安裝與完整操作步驟見 [windows-setup.md](windows-setup.md)，目前實機狀態見 [current-windows-status.md](current-windows-status.md)；macOS 節落是原作者流程，尚未由目前維護者重驗。

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

如果沒有 Anthropic / Gemini key，對應的雲端 alias 仍會出現在模型清單，但實際呼叫會失敗；不要填假 credential。

2026-09-01 已移除本機所有雲端 API key，因此目前 `claude-haiku`、`gemini-cheap`、`gemini-strong` 都不可實際呼叫。兩份 workflow 的三個 step 都使用 `gemini-cheap`，所以完整 workflow 暫時不可執行；本機 `local-qwen`、`local-embed` 與 `breeze-asr` 不受雲端 key 影響。

## STT / Breeze-ASR-25

### 8001 能啟動，但第一次轉錄失敗或長時間等待

Port listener 只代表 FastAPI process 已啟動，不代表 Breeze 權重與推論 backend 已就緒。這台電腦已實際確認：

- Breeze-ASR-25 權重已快取到 `D:\Projects\multi-agent平台架設\.hf-cache`。
- `.venv` 是 `torch 2.13.0+cu132`。
- `torch.cuda.is_available()` 是 `True`，GPU 是 RTX 4050 Laptop GPU 6 GB。
- `samples/gen_tsmc_01.wav` 直接推論成功，峰值 CUDA reserved 約 4.51 GiB。
- 目前沒有 FFmpeg；[services/stt/breeze_asr.py](../services/stt/breeze_asr.py) 會用既有 `librosa` / `soundfile` 載入及重採樣，不再把音檔路徑直接交給 Transformers。

8001 HTTP route、LiteLLM `breeze-asr` alias、Breeze 直接推論及 workflow 內轉錄都曾在 Windows 實機通過。若目前再次失敗，先確認啟動該 process 的同一個 PowerShell 已設定正確 `HF_HUB_CACHE`；否則 process 可能回到使用者目錄的預設快取，看不到已下載的權重。

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

### trigger 成功後出現 `pool-2` 或 `OSError(22, 'Invalid argument')`

先用 `persistence.history <thread_id>` 確認 run 狀態。Windows 實機曾發生 Honcho 已退出，但多組 `uv`／Python 孫程序、Master、worker、LiteLLM 與 MCP 子程序仍存活；它們會保留重複 PostgreSQL connection pools，甚至讓 Agent Runtime 繼續使用已失效的終端輸出 handle。先依 [README 的「關閉」](../README.md#關閉) 執行 `scripts/stop_windows_stack.ps1`，確認 5432 保持運行、其餘五個 port 都釋放，再只啟動一組主 Honcho 與一組 workers。

### `stt_exclusion_notify` 查不到條文

這是上游場景的必要前置。Windows 先查目前資料庫：

```sql
SELECT count(*)
FROM store
WHERE prefix LIKE '_global.semantic.insurance_product.%';
```

目前實機為 59，與來源 YAML 的理論筆數相同，已完成時不要重跑。新的空資料庫或筆數不符時，先確認 5432、11434、4000 與 `local-embed`，再執行：

```powershell
$env:PYTHONUTF8 = '1'
$env:UV_CACHE_DIR = 'D:\Projects\multi-agent平台架設\.uv-cache'
.\.venv\Scripts\python.exe -m scripts.seed_insurance_memory
```

腳本以固定 key upsert，可安全重跑。若停在 `from openai import ...` 且 traceback 最後是 `KeyboardInterrupt`，代表 import 尚未完成時被手動中止；2026-08-27 Windows 實測完整 seed module import 約 2.3 秒。目前這個 workflow 的 `stt`、`check`、`notified` 都宣告 `gemini-cheap`，執行時需要有效的 `GEMINI_API_KEY`，不需要 Anthropic key。

## Smoke test

### `gather_concurrency_smoke_test.py` 在 commit `39d6449` 失敗

原因不是正式輸出應改回舊字串，而是 gather scenario 使用 `should_notify=false` 後會在任何並行工作前直接回傳 `[]`，已經測不到原本要驗證的 concurrency。2026-08-27 本機已把該 scenario 改成 `should_notify=true` 並模擬成功 tool call，三個 gather scenario 全部通過；負分支安全短路則由 `llm.notify_agent_smoke_test` 獨立驗證。後續 commit `e42fc04` 的三個 GitHub Actions job 也已全部成功，連結與歷史失敗紀錄見 [current-windows-status.md](current-windows-status.md#測試狀態)。

### Agent Runtime / MCP smoke test 報 `Connection closed` 或 uv cache 初始化失敗

如果底層 stderr 指向 `C:\Users\User\AppData\Local\uv\cache`，代表 MCP SDK 的 stdio 子行程沒有繼承目前 PowerShell 的 `UV_CACHE_DIR`。Agent Runtime 外層通常只看到 `McpError: Connection closed`，不是 workflow YAML、MCP permission 或 MCP server assertion 失敗。

2026-08-27 已在 [mcp_servers/base_client.py](../mcp_servers/base_client.py) 修正：只將 `UV_CACHE_DIR` 與 `PYTHONUTF8` 傳給 stdio 子行程，不傳遞 secret。啟動 Honcho 或單獨跑 smoke test 前，仍要在同一個 PowerShell 設定：

```powershell
$env:PYTHONUTF8 = '1'
$env:UV_CACHE_DIR = 'D:\Projects\multi-agent平台架設\.uv-cache'
```

驗證方式見 [testing.md](testing.md#windows-d-槽-uv-cache-注意事項)。

### Smoke test 偶發收到錯的事件

跑測試前先關閉 `Procfile.workers`，因為常駐 worker 與測試可能使用相同 consumer group，會互相搶事件。

完整測試矩陣與前置條件見 [testing.md](testing.md)。

## macOS / Bash 疑難排解（原作者流程，尚未由目前維護者重驗）

以下保留原作者的完整排錯內容。Windows 使用者不要直接執行 `brew`、`make`、`pkill` 或 `/opt/homebrew` 路徑。

### pgvector

#### `CREATE EXTENSION vector` 說 extension "vector" is not available

本機 Postgres 沒有 pgvector。Homebrew 的 `postgresql@14` 沒有附官方 build，要自己編譯：

```bash
git clone https://github.com/pgvector/pgvector.git
cd pgvector
# PG_CONFIG 要指到你實際在跑的那個 Postgres，不是 which psql 找到的那個
make PG_CONFIG=/opt/homebrew/opt/postgresql@14/bin/pg_config
make install PG_CONFIG=/opt/homebrew/opt/postgresql@14/bin/pg_config
```

裝完回到專案再跑一次 `psql agent_architecture -c "CREATE EXTENSION vector;"`。

編譯需要 Xcode command line tools（`xcode-select --install`）。背景與當初踩到的細節見 [long-term-memory-plan.md](long-term-memory-plan.md) §1.3。

#### 裝了但還是找不到

多半是機器上有多個 Postgres（Homebrew 一個、Postgres.app 一個、conda 一個）。確認 `psql` 連到的跟你編譯時 `PG_CONFIG` 指的是同一個：

```bash
psql agent_architecture -c "SHOW server_version; SHOW config_file;"
```

### Postgres 連線

#### `psql: could not connect to server`

Postgres 沒在跑：`brew services start postgresql@14`。

#### 程式跑起來噴 `KeyError: 'PERSISTENCE_DATABASE_URL'`

`.env` 沒建或沒有這一行。`cp .env.example .env` 之後確認檔案在**專案根目錄**（各模組的 `load_dotenv()` 是從 cwd 往上找）。

#### 連得上但 `password authentication failed`

`.env.example` 的預設值假設本機 Postgres 免密碼。有設帳密就改成完整格式：

```dotenv
PERSISTENCE_DATABASE_URL=postgresql://使用者:密碼@localhost:5432/agent_architecture
```

### Ollama

#### `ollama pull` 說 could not connect

daemon 沒起來。`ollama pull` 是打去 `localhost:11434` 的 client 指令，不會自己啟動 server：

```bash
brew services start ollama    # 或另開 terminal 跑 ollama serve
```

#### `honcho start` 時 ollama 那行立即掛掉

port 11434 已經被 `brew services` 起的 Ollama 佔用了。二選一：把 [Procfile](../Procfile) 的 `ollama:` 那行註解掉（推薦，daemon 讓 brew 管），或 `brew services stop ollama` 讓 Honcho 自己起。

### LiteLLM Gateway

#### `curl localhost:4000/v1/models` 連不上

LiteLLM 沒起來或啟動失敗。看 Honcho terminal 裡 `litellm` 前綴的 log——最常見是 `gateway/config.yaml` 有 YAML 語法錯，或 provider 需要的 API key 未設定。

#### 呼叫 Gemini 系列模型噴 401 / API key not valid

`.env` 的 `GEMINI_API_KEY` 是空的或無效。改完要**重新啟動 Honcho**——LiteLLM 是啟動時讀環境變數，不會熱更新。

#### `gemini-3.1-pro-preview` 404

Preview tag 可能被下架。`gateway/config.yaml` 的註解裡有替代方案；相關歷史取捨見 [上游 GitHub Issue #21](https://github.com/donydony228/agent-architecture/issues/21)。

### 跑 workflow

#### 除外責任場景 check 查不到任何條文、`matched_articles` 永遠是空的

保單條款沒灌進長期記憶。這是這個場景的必要前置，跑之前要先做一次：

```bash
uv run python -m scripts.seed_insurance_memory
```

#### 事件驅動模式觸發後沒有任何反應

`honcho -f Procfile.workers start` 那批沒起來——master/worker 不在 [Procfile](../Procfile) 裡，是另一份 [Procfile.workers](../Procfile.workers)，要另開 terminal 跑。

#### 換了 `WORKFLOW_DEF_PATH` 但行為沒變

那是**啟動時**讀的，不是每次請求。兩批 Honcho（常駐服務 + workers）都要帶著同一個值重新啟動才會生效。

#### smoke test 有些情境莫名其妙失敗

`honcho -f Procfile.workers start` 還開著。那批 process 的 consumer group 跟測試同名，會搶走測試的命令，讓測試裡用假 handler 的情境被真 handler 接走。跑 smoke test 前先關掉它。

### 殘留 process

`Ctrl+C` 沒清乾淨、下次啟動撞 port 的話，先依 [README 的「關閉」](../README.md#關閉) 用 `pgrep` 確認後再清理。
