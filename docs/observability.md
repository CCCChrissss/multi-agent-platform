# 觀察 workflow 執行結果

Workflow 狀態與 Agent 內部的 LLM、MCP tool、memory 操作會寫入 PostgreSQL。開始前先確認 `.env` 的 `PERSISTENCE_DATABASE_URL` 指向你實際使用的資料庫；目前 Windows 實機資料庫是 `agent_architecture_test`，原作者 macOS 範例使用 `agent_architecture`。

> [!NOTE]
> 2026-08-27 已用本頁查詢方式確認 `thread_id=e138228b-317b-4cc3-bc75-8496b26e14f2` 的 `stt_check_notify` 為 `completed`，且三個 agent、MCP tool 與 memory writer 都有 audit log。查到歷史成功紀錄仍不代表服務現在持續運作，應另外檢查 port 與即時 terminal log。

## thread_id 從哪裡來

事件驅動 trigger 成功送出時會直接印出 `thread_id`：

Windows / PowerShell：

```powershell
.\.venv\Scripts\python.exe -m orchestrator.trigger `
    --workflow-def workflows/definitions/stt_check_notify.yaml `
    --payload '{\"audio_ref\":\"samples/gen_tsmc_01.wav\"}'
```

以上是目前 VS Code 使用的 Windows PowerShell 5.1 實機寫法；`\"` 用來避免原生 `.exe` 參數處理移除 JSON 雙引號。PowerShell 7 則使用未跳脫形式 `--payload '{"audio_ref":"samples/gen_tsmc_01.wav"}'`。

macOS / Bash：

```bash
uv run python -m orchestrator.trigger \
  --workflow-def workflows/definitions/stt_check_notify.yaml \
  --payload '{"audio_ref":"samples/gen_tsmc_01.wav"}'
```

如果當下沒有複製，Windows 可在 pgAdmin 4 的 `agent_architecture_test` 資料庫開啟 Query Tool；macOS 可用 `psql agent_architecture` 或原本的 TablePlus / Postico 查詢同一段 SQL：

```sql
SELECT thread_id, workflow_name, current_step, status, updated_at
FROM orchestrator_runs
ORDER BY updated_at DESC
LIMIT 5;
```

## 用內建 CLI 查完整稽核歷史

在 repository 根目錄執行：

Windows / PowerShell：

```powershell
$ThreadId = 'REPLACE_WITH_THREAD_ID'
.\.venv\Scripts\python.exe -m persistence.history $ThreadId
```

macOS / Bash：

```bash
uv run python -m persistence.history <thread_id>
```

輸出包含：

1. checkpoint：每一步的 state 快照。
2. call log：每個 Agent 內部呼叫的 LLM、tool 或 memory 操作、輸入輸出、延遲與錯誤狀態。

事件驅動模式的正式執行狀態保存在 `orchestrator_runs`。Master Agent 另外把狀態鏡射進 checkpoint 表，讓這個 CLI 能用同一種格式顯示同步與事件驅動模式；鏡射限制見 [long-term-memory-plan.md](long-term-memory-plan.md)。

## 用資料庫工具查 workflow 狀態

### 最近執行

```sql
SELECT thread_id, workflow_name, current_step, status, updated_at
FROM orchestrator_runs
ORDER BY updated_at DESC
LIMIT 20;
```

`status` 可能是：

- `running`
- `completed`
- `needs_review`
- `failed`

### 某次執行的累積 payload

```sql
SELECT thread_id, workflow_name, current_step, status, state_payload, updated_at
FROM orchestrator_runs
WHERE thread_id = '<thread_id>';
```

## 查每個 Agent / MCP tool call

```sql
SELECT created_at, node, kind, name, is_error, latency_ms, request, response
FROM call_log
WHERE thread_id = '<thread_id>'
ORDER BY created_at;
```

主要欄位：

| 欄位 | 意義 |
|---|---|
| `node` | 觸發呼叫的 Agent，例如 `stt`、`check`、`notified` |
| `kind` | `llm`、`tool` 或 `memory` |
| `name` | model alias、MCP tool 名稱或 memory 動作 |
| `is_error` | 該次呼叫是否失敗 |
| `latency_ms` | 呼叫耗時 |
| `request` / `response` | 稽核內容；memory log 刻意不複製完整記憶內容 |

確認預設 workflow 使用本機模型：

```sql
SELECT created_at, node, name, is_error
FROM call_log
WHERE thread_id = '<thread_id>'
  AND kind = 'llm'
ORDER BY created_at;
```

`stt_check_notify` 的 Agent LLM 呼叫目前預期使用 `gemini-cheap`。embedding 仍使用 `local-embed`；兩者角色不同。

## 看即時 process log

| Terminal | log prefix | 用途 |
|---|---|---|
| `Procfile` | `ollama` | 11434、本機模型與模型目錄 |
| `Procfile` | `litellm` | 4000、alias 路由與 provider 錯誤 |
| `Procfile` | `stt` | 8001、Breeze 載入與轉錄錯誤 |
| `Procfile` | `notified` | 8002、通知 placeholder 呼叫 |
| `Procfile` | `agents` | 8003、workflow load、schema、Agent LLM / MCP 行為 |
| `Procfile.workers` | `master` | run 狀態與下一步派送 |
| `Procfile.workers` | `worker-all` | `stt` / `check` / `notified` command 執行 |
| `Procfile.workers` | `memory-writer` | episodic memory 寫入 |

`should_notify=false` 時，notified agent 目前會直接回傳 `notified_log=[]`，不呼叫通知 LLM 或 tool。此時 `call_log` 沒有 notified LLM / tool 行是正常的，不是 log 遺失。

## 資料表角色

| 表 | 存放內容 |
|---|---|
| `checkpoints` / `checkpoint_blobs` / `checkpoint_writes` | 同步模式 state，以及事件驅動模式的狀態鏡射 |
| `orchestrator_runs` | 事件驅動 workflow 的正式 run 狀態與累積 payload |
| `event_log` / `event_dispatch` | command / completion 事件與 consumer group 派送狀態 |
| `call_log` | LLM、MCP tool、memory 操作的稽核紀錄 |
| `store` / `store_vectors` | 跨 run 的長期記憶，不是某一次執行的 checkpoint |

checkpoint 以 `thread_id` 描述一次執行；long-term memory 以 `(namespace, key)` 保存跨多次執行的知識。兩者不能互相取代。

## 快速排查順序

1. trigger 有沒有印出 `thread_id`。
2. `orchestrator_runs` 有沒有該筆資料，`status` 與 `current_step` 是什麼。
3. workers terminal 是否收到相同步驟的事件。
4. 對應 port 是否有 listener。
5. 第一個 Honcho terminal 中該 process 的 log。
6. `call_log.is_error=true` 的最後一筆 request / response。
7. 若卡在 `stt`，先區分是 Gemini 決策呼叫還是 Breeze 工具／權重；若卡在 `check`，優先檢查 4000 / `gemini-cheap` 與 `GEMINI_API_KEY`；若卡在 memory，優先檢查 PostgreSQL / `local-embed`。
