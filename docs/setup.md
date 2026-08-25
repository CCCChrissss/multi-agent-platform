# 安裝疑難排解

根目錄 README 的主要安裝指令以 macOS / Bash 為例；Windows / PowerShell 請先看
[windows-setup.md](windows-setup.md)，再回到本文件依錯誤類型排查。

[README 的「從零開始安裝」](../README.md#從零開始安裝)是happy path，這份收的是實際會卡住的地方。

## pgvector

### `CREATE EXTENSION vector` 說 extension "vector" is not available

本機 Postgres 沒有 pgvector。Homebrew 的 `postgresql@14` 沒有附官方 build，要自己編：

```bash
git clone https://github.com/pgvector/pgvector.git
cd pgvector
# PG_CONFIG 要指到你實際在跑的那個 Postgres，不是 which psql 找到的那個
make PG_CONFIG=/opt/homebrew/opt/postgresql@14/bin/pg_config
make install PG_CONFIG=/opt/homebrew/opt/postgresql@14/bin/pg_config
```

裝完回到專案再跑一次 `psql agent_architecture -c "CREATE EXTENSION vector;"`。

編譯需要 Xcode command line tools（`xcode-select --install`）。背景與當初踩到的細節見 [long-term-memory-plan.md](long-term-memory-plan.md) §1.3。

### 裝了但還是找不到

多半是機器上有多個 Postgres（Homebrew 一個、Postgres.app 一個、conda 一個）。確認 `psql` 連到的跟你編譯時 `PG_CONFIG` 指的是同一個：

```bash
psql agent_architecture -c "SHOW server_version; SHOW config_file;"
```

## Postgres 連線

### `psql: could not connect to server`

Postgres 沒在跑：`brew services start postgresql@14`。

### 程式跑起來噴 `KeyError: 'PERSISTENCE_DATABASE_URL'`

`.env` 沒建或沒有這一行。`cp .env.example .env` 之後確認檔案在**專案根目錄**（各模組的 `load_dotenv()` 是從 cwd 往上找）。

### 連得上但 `password authentication failed`

`.env.example` 的預設值假設本機 Postgres 免密碼。有設帳密就改成完整格式：

```
PERSISTENCE_DATABASE_URL=postgresql://使用者:密碼@localhost:5432/agent_architecture
```

## Ollama

### `ollama pull` 說 could not connect

daemon 沒起來。`ollama pull` 是打去 `localhost:11434` 的 client 指令，不會自己啟動 server：

```bash
brew services start ollama    # 或另開 terminal 跑 ollama serve
```

### `honcho start` 時 ollama 那行立刻掛掉

port 11434 已經被 `brew services` 起的 Ollama 佔用了。二選一：把 [Procfile](../Procfile) 的 `ollama:` 那行註解掉（推薦，daemon 讓 brew 管），或 `brew services stop ollama` 讓 honcho 自己起。

## LiteLLM Gateway

### `curl localhost:4000/v1/models` 連不上

litellm 沒起來或啟動失敗。看 honcho 那個 terminal 裡 `litellm` 前綴的 log——最常見是 `gateway/config.yaml` 有 YAML 語法錯，或 `GEMINI_API_KEY` 沒設（config 裡 `os.environ/GEMINI_API_KEY` 解不出來）。

### 呼叫 gemini 系列模型噴 401 / API key not valid

`.env` 的 `GEMINI_API_KEY` 是空的或無效。改完要**重啟 honcho**——litellm 是啟動時讀環境變數，不會熱更新。

### `gemini-3.1-pro-preview` 404

preview tag 有可能被下架。`gateway/config.yaml` 的註解裡列了替代方案（`gemini-2.5-pro` 是非 preview 的穩定版）。相關取捨見 [GitHub Issue #21](https://github.com/donydony228/agent-architecture/issues/21)。

## 跑 workflow

### 除外責任場景 check 查不到任何條文、`matched_articles` 永遠是空的

保單條款沒灌進長期記憶。這是這個場景的必要前置，跑之前要先做一次：

```bash
uv run python -m scripts.seed_insurance_memory
```

### 事件驅動模式觸發後沒有任何反應

`honcho -f Procfile.workers start` 那批沒起來——master/worker 不在 [Procfile](../Procfile) 裡，是另一份 [Procfile.workers](../Procfile.workers)，要另開 terminal 跑。

### 換了 `WORKFLOW_DEF_PATH` 但行為沒變

那是**啟動時**讀的，不是每次請求。兩批 honcho（常駐服務 + workers）都要帶著同一個值重啟才會生效。

### smoke test 有些情境莫名其妙失敗

`honcho -f Procfile.workers start` 還開著。那批 process 的 consumer group 跟測試同名，會搶走測試的命令，讓測試裡用假 handler 的情境被真 handler 接走。跑 smoke test 前先關掉它。

## 殘留 process

`Ctrl+C` 沒清乾淨、下次啟動撞 port 的話，清理指令見 [README 的「關閉」](../README.md#關閉)。
