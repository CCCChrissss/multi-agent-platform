# macOS / Bash 歷史操作參考

本頁保存先前操作流程，未依本次 Windows launcher 重新驗證。當前設定以實際 YAML 為準；Windows 請用 [主手冊](windows-setup.md)。

## 原作者 macOS / Bash / Claude Code 操作原文

> [!NOTE]
> 以下內容取自 Windows 遷移前的 README（commit `dd5ec5d`），保留原作者的 macOS / Bash 操作方式供歷史比對。本區塊未由目前 Windows 維護者重新實機驗證，也不會與 Windows 指令混寫。Windows 使用者請回到前面的 Windows 主操作手冊。

## 從零開始安裝

依序照做，每一步都有驗證方式。卡住的話看 [docs/setup.md](../docs/setup.md)（常見錯誤與排除）。

以下主要指令以 macOS / Bash 為例；Windows / PowerShell 請改看 [docs/windows-setup.md](../docs/windows-setup.md)。

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

> `CREATE EXTENSION vector` 失敗代表本機 Postgres 沒有 pgvector。Homebrew 的 `postgresql@14` 沒有官方 build，要從[原始碼編譯安裝](https://github.com/pgvector/pgvector)——步驟見 [docs/setup.md](../docs/setup.md)，背景見 [docs/long-term-memory-plan.md](../docs/long-term-memory-plan.md) §1.3。
>
> 資料表**不用**手動建，各模組啟動時會自己 `CREATE TABLE IF NOT EXISTS`；只有 extension 這一步是手動的。

### 3. 環境變數

```bash
cp .env.example .env
```

打開 `.env` 填：

- `ANTHROPIC_API_KEY`——`claude-haiku` 用；台積電場景的 `stt`／`notified`，以及除外責任場景的 `check`／`notified` 都需要，**目前兩個示範場景都不能留空**。
- `GEMINI_API_KEY`——除外責任場景的 `stt` 宣告為 `gemini-cheap`，因此跑 `stt_exclusion_notify` 時必填；[scripts/distill_procedural.py](../scripts/distill_procedural.py) 的知識蒸餾與 [evals/run_eval.py](../evals/run_eval.py) 的 Gemini 對照診斷也需要。只跑 `stt_check_notify` 且不執行這些工具時可以留空。

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

> 用 `brew services` 讓 Ollama 常駐的話，要把 [Procfile](../Procfile) 裡 `ollama:` 那行註解掉，不然下一步 `honcho start` 會撞 port 失敗。

### 5. Breeze-ASR-25（自動）

第一次跑 `stt` 時會自動從 HuggingFace 下載（需要網路，之後快取在本機），不用預先準備。第一次呼叫會因此慢很多。

---

## 執行

### 步驟 0：啟動常駐服務（兩種模式共用）

常駐服務用 [Procfile](../Procfile) + [honcho](https://github.com/nickstenning/honcho) 一個指令全部啟動：

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
| `agents` | 8003 | agent runtime（[agents/runtime.py](../agents/runtime.py)，`stt`/`check`/`notified` 三個 agent 都在同一個 process 裡，見下方「單一 runtime process」） |

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

第二個指令要印出 `local-qwen`、`claude-haiku`、`gemini-cheap`、`gemini-strong`、`breeze-asr`、`local-embed`。任何一項對不上，見 [docs/setup.md](../docs/setup.md)。

> - 如果你已經用 `brew services start ollama` 讓 Ollama 開機自動啟動，把 [Procfile](../Procfile) 裡的 `ollama:` 那行刪掉或註解掉，不然 honcho 啟動時會撞 port 失敗。
> - `mcp_servers/` 底下的 MCP server **不用**手動啟動——它們是薄薄一層 MCP 殼，`MCPGateway` 會在需要時把對應 server 當子行程開起來，用完自動關閉，實際邏輯還是打去上面的常駐 service。
> - 同步模式其實用不到 8003 這個 agent runtime，但一起起來也無妨。

#### 切換示範 workflow

`check-agent`/`notified-agent`（上面這批常駐服務）以及事件驅動模式的 `master`/`worker`（下面 [Procfile.workers](../Procfile.workers)）在**啟動時**讀 `WORKFLOW_DEF_PATH` 這個環境變數，決定要照哪一份 workflow 定義檔驗證/執行——不填預設是 `workflows/definitions/stt_check_notify.yaml`（台積電場景）。要跑除外責任場景，啟動**這兩批 process 時都要帶同一個值**：

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

跑完會印出這次執行的 `thread_id`。預設讀 `samples/gen_tsmc_01.wav`，要換音檔得改 [workflows/simple_pipeline.py](../workflows/simple_pipeline.py) 裡 `main()` 的 `audio_ref`（`samples/gen_tsmc_*.wav`、`samples/gen_other_*.wav` 是測試用的假音檔，一半提到台積電、一半沒有）。注意這個檔刻意凍結，改了 `parity_check.py` 會擋下來。

**崩潰後接續執行**：帶著同一個 `thread_id` 再跑一次，會自動從上次中斷的節點接續，不會重跑已完成的步驟。

```bash
uv run python -m workflows.simple_pipeline <thread_id>
```

---
### 模式 B：事件驅動（目前的主線）

這個模式下，Master Agent 跟每一個 step 的 worker 都是**各自獨立的長駐 process**。它們放在另一份 [Procfile.workers](../Procfile.workers)，一樣一個指令啟動。

**B-1. 啟動 Master Agent、worker 與 memory-writer**（另開一個 terminal）

```bash
uv run honcho -f Procfile.workers start
```

| process | 是什麼 |
|---|---|
| `master` | Master Agent：收完成事件、決定下一步派給誰、更新 `orchestrator_runs` |
| `worker-all` | 認領 `stt`/`check`/`notified` 三步的命令（同一個 process 內三條迴圈），執行完發出完成事件——單一 process 是取捨後的結果，見 [Procfile.workers](../Procfile.workers) 註解 |
| `memory-writer` | 背景蒸餾：訂閱同一批完成事件，依 workflow 定義把每步結果寫進長期記憶（[docs/long-term-memory-plan.md](../docs/long-term-memory-plan.md) M3、[orchestrator/memory_writer.py](../orchestrator/memory_writer.py)），寫入的都是 `pending` 狀態，要透過 `scripts/review_episodic.py` 或下方 demo UI 人工核准才會生效 |

`Ctrl+C` 一次全部關掉，行為跟 `honcho start` 一致。

> **為什麼不直接併進 [Procfile](../Procfile)？** 因為 [orchestrator/smoke_test.py](../orchestrator/smoke_test.py) 與 [workflows/parity_check.py](../workflows/parity_check.py) 會在自己的 process 內起 master/worker，consumer group 跟這批完全同名。這批 process 如果在背景跑著，會跟測試搶同一批命令，測試裡刻意用假 handler 的情境就會被真 handler 接走。**跑 smoke test 前記得先關掉這個 Procfile**（詳見 [Procfile.workers](../Procfile.workers) 的註解）。

**B-2. 觸發一次執行**（一次性指令，跑完就結束）

```bash
uv run python -m orchestrator.trigger \
    --workflow-def workflows/definitions/stt_check_notify.yaml \
    --payload '{"audio_ref": "samples/gen_tsmc_01.wav"}'
```

會印出這次執行的 `thread_id`，接著在 B-1 那個 terminal 就會看到 `stt -> check -> notified` 依序被推進。

`trigger.py` 完全不認識這個場景——它只收一份 workflow 定義檔和一包 JSON payload 就往下送，所以換一個 workflow 只要換 `--workflow-def` 跟 `--payload`，不用改任何程式碼。步驟順序、每步的事件名稱、輸入輸出欄位全部宣告在 [workflows/definitions/stt_check_notify.yaml](../workflows/definitions/stt_check_notify.yaml) 裡。

### 跑另一個場景：除外責任

三件事都要做，少一件就不會動：

**① 先把保單條款灌進長期記憶**（只需做一次，做過就跳過）

```bash
uv run python -m scripts.seed_insurance_memory
```

這個場景的 `check` 不把條款塞進 prompt，而是去長期記憶裡查——沒灌過就查不到任何條文，`matched_articles` 永遠是空的（[docs/exclusion-scenario-plan.md](../docs/exclusion-scenario-plan.md) P3）。

**② 兩批 honcho 都帶 `WORKFLOW_DEF_PATH` 重啟**（見上面「切換示範 workflow」）

**③ 觸發**

```bash
uv run python -m orchestrator.trigger \
    --workflow-def workflows/definitions/stt_exclusion_notify.yaml \
    --payload '{"audio_ref": "samples/gen_policy_01.wav"}'
```

> **跑完沒收到通知是正常的，不是壞掉。** 目前 `samples/` 底下沒有任何一個音檔會真的觸發通知——`gen_policy_01.wav` 是刻意設計的邊界案例（直覺答案「酒駕 → 除外 → 不賠」是錯的），`gen_policy_02.wav` 單純問長期照顧狀態怎麼認定，兩個都不涉及除外責任。真正會觸發的第三個案例目前只有逐字稿、沒有對應音檔。判斷理由見 [docs/exclusion-scenario-plan.md](../docs/exclusion-scenario-plan.md) P4。

> 總共 3 個 terminal：`honcho start`（常駐服務）、`honcho -f Procfile.workers start`（編排）、trigger（一次性）。

---

## Demo UI（用瀏覽器組 workflow / 審核長期記憶）

不寫指令、用瀏覽器操作的替代介面：組裝/測試 agent、瀏覽 workflow 設定、觸發執行、審核 `memory-writer` 寫入的 `pending` 記憶（approve/reject）。

```bash
uv run uvicorn demo.api:app --port 8010
```

啟動後直接用瀏覽器打開 [demo/index.html](../demo/index.html)（本機檔案，不用另外起 static server——它是純前端，透過 CORS 打 `http://localhost:8010`）。需要上面 Postgres + `honcho start` 這批常駐服務已經在跑（catalog 讀 `gateway/config.yaml`、跑 workflow 打 8003 的 agent runtime）；要審核記憶則另外需要 `memory-writer`（`honcho -f Procfile.workers start`）先寫入過 `pending` 候選。

---

## 觀察執行結果

```bash
uv run python -m persistence.history <thread_id>
```

印每一步的 checkpoint 快照 + 每個 agent 內部的 LLM/tool 呼叫紀錄，兩種模式都可用。事件驅動模式怎麼直接查執行狀態、Postgres 各張表存什麼、`store` 跟 checkpoint 的差別，見 [docs/observability.md](../docs/observability.md)。

---

## 驗證

沒有 pytest，全部是手動跑的 smoke test：

```bash
uv run python -m event_bus.smoke_test           # event bus 本身
uv run python -m orchestrator.smoke_test        # 編排層
uv run python -m workflows.parity_check         # 兩種模式一致性
uv run python -m persistence.memory_smoke_test  # 長期記憶
```

⚠️ 跑前先關掉 `honcho -f Procfile.workers start`（consumer group 撞名，會搶走測試的命令）。各支的前置條件、記憶蒸餾 pipeline（P0-P5）手動試跑步驟，見 [docs/testing.md](../docs/testing.md)。

---

## 關閉

兩個 honcho terminal 各按一次 `Ctrl+C`，各自的 process 都會連帶關掉（trigger 是一次性執行，跑完自動結束；demo UI 是另開的 terminal，`Ctrl+C` 單獨關）。如果不小心留下殘留 process：

```bash
pkill -f "ollama serve"
pkill -f "litellm --config gateway/config.yaml"
pkill -f "uvicorn services."
pkill -f "uvicorn agents."
pkill -f "uvicorn demo.api"
pkill -f "workflows.event_driven_pipeline"
```
