# 安裝與執行疑難排解

Windows 從 [主手冊](windows-setup.md) 開始。以下命令都在 VS Code 的 repository 根目錄執行。
先執行 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\dev.ps1 doctor`
查看缺項，再看所啟動 terminal 的錯誤；不要直接套用歷史主機路徑。

## Python、uv 與 PowerShell

- `uv` / `ollama` 找不到：確認自己的安裝目錄在 PATH，重開 VS Code terminal。
- 缺 `.venv`：`uv python install 3.11` 後執行 `uv sync --locked`。不複製舊機器的 .venv。
- lock 不一致：確認 branch/commit，不直接以重新 lock 掩蓋差異。
- CP950 / UnicodeDecodeError：launcher 已設定 `PYTHONUTF8=1`；手動命令在同一 terminal 先執行 `$env:PYTHONUTF8='1'`，.env 用 UTF-8。
- execution policy 阻擋：使用主手冊的單次 `powershell.exe -ExecutionPolicy Bypass -File ...`，不需改全機 policy。
- trigger JSONDecodeError：使用 `--payload-file samples/payloads/tsmc.json`，不要把 PowerShell 5.1 與 7 的跳脫規則混用。

## PostgreSQL / pgvector

- password authentication failed：確認 .env 是自己的帳號、密碼、host、port、DB；與 psql 實際連線一致。
- `PERSISTENCE_DATABASE_URL` 缺少：從 .env.example 建立 .env，與 LiteLLM 的 DATABASE_URL 分開。
- extension vector 不可用：先查 `pg_available_extensions`；沒有時安裝對應 PostgreSQL 的 pgvector，再 CREATE EXTENSION。
- relation `store` / `orchestrator_runs` 不存在：DB 存在不代表應用 tables 已初始化。先依主手冊啟動相應元件／seed，再查資料。
- 不要用刪除、truncate 或沿用別人的 DB dump 當成預設排錯方法。

## Ollama、模型與快取

- 11434 已占用：可使用原有 Ollama server。只有沒有 server 時才執行 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\dev.ps1 ollama`；不要再開第二份。
- 模型找不到：確認 `ollama list` 包含 YAML alias 指向的模型。`OLLAMA_MODELS` 必須在 server 啟動前設定，client 的設定不會更換已執行 server 的儲存位置。
- exclusion 的 `check agent 逾時（>180s）`：先用 `ollama ps` 確認 `local-qwen3` 實際載入的 context。專案已在 `gateway/config.yaml` 固定 `num_ctx: 8192` 與 `max_tokens: 512`；修改後必須停止並重啟 `services` 才會生效。若 `PROCESSOR` 不再是 `100% GPU`，代表本機顯示記憶體不足，不能只繼續拉長 Agent timeout。
- 自訂快取請用自己的可寫入絕對路徑，不需要 D 槽。不設定時由工具使用預設位置。
- LiteLLM `/v1/models` 列出 alias 不代表 provider 可用；依主手冊執行真正的 chat/embedding probe。
- workflow、CLI 蒸餾與 UI 蒸餾目前都預設使用 `local-qwen3`，不需要雲端 API key；只有顯式以 `--model` 選擇雲端 alias 時才需要對應 key。
- Breeze 第一次呼叫太久：先完成主手冊的直接預熱，排除下載時間與模型載入問題。GPU/CPU 可用性與 OOM 必須依實際機器判斷。

## 啟停、工作流與 MCP

- `status` 顯示 running 但請求失敗：它僅代表 supervisor 活著，查看服務 log、ports、runtime OpenAPI 和模型探測。
- workflow 衝突：services/workers/UI 需選同一個 `-Workflow`；先執行 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\dev.ps1 stop`，確認 stopped，再重啟。`.env` 中啟用的 `WORKFLOW_DEF_PATH` 也必須一致。
- 主工作流改 schema：Master/Workers 保留啟動時定義，請重新啟動；UI 單一 agent 的熱載入另有自己的範圍。
- `uv` MCP 子程序失敗：确认 uv 在 PATH、.venv 已同步，以及自訂 UV_CACHE_DIR 有權限。共用 MCP client 只繼承允許的環境變數，不應為排錯傳遞全部 secrets。
- managed group 已存在：不要啟動第二份；用相同 checkout 的 stop/status 檢查。PID 與建立時間不符的 stale record 不會被用來停止程序，下一次啟動可回收。
- manual/舊 Honcho 留下程序：回到該 terminal 或工具停止。新版 stop 不猜測這些程序的歸屬，也不按全機模組名稱清理。
- `.run` 格式損壞或 permission error：停止該 checkout 的已知 supervisor，保留紀錄排查；不要為了繼續啟動而對不明 PID 強制終止。
- 端到端卡住：保留新的 thread_id，查 [observability](observability.md) 的 run、checkpoint、call log。缺少 workers 或查錯 DB 都可能導致誤判。
- 測試收到錯誤事件：不要讓相同 DB / consumer group 的正式 workers 與 smoke tests 同時執行。

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

若看到 `Invalid value for '--debug': 'release' is not a valid boolean`，代表父程序的通用
`DEBUG=release` 被 LiteLLM CLI 誤認為自己的布林選項。使用 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\dev.ps1 services` 啟動時，
launcher 會隔離父程序的 `DEBUG`；不要在專案 `.env` 重新宣告 `DEBUG`。舊版 checkout 可先在同一個
PowerShell 執行 `Remove-Item Env:DEBUG -ErrorAction SilentlyContinue` 再啟動，這只影響目前終端機。

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
