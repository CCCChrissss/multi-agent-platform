# persistence/

平台共用的持久層,任何 workflow/node 都可以複用,不用各自兜一套:

- [checkpointer.py](checkpointer.py):LangGraph checkpointer(狀態外部化),`graph.compile(checkpointer=...)` 時用。
- [call_log.py](call_log.py):`gateway/client.py`(所有 LLM 呼叫)、`mcp_servers/gateway.py`(所有 tool 呼叫)共用的呼叫紀錄——node 內部實際打了幾次 LLM、呼叫了哪個 tool、輸入輸出是什麼,不用每個 node 自己加 log。
- [history.py](history.py):稽核 CLI,把上面兩者串在一起查。

## 連線資訊

- Backend:Postgres。checkpointer 走 `langgraph-checkpoint-postgres`(`AsyncPostgresSaver`);call_log 走 `psycopg` 直接下 SQL。
- 連線字串環境變數:`PERSISTENCE_DATABASE_URL`(在 `.env`)。
  - **刻意不叫 `DATABASE_URL`**——LiteLLM Gateway([gateway/](../gateway/))的 proxy server 會把這個名字保留給它自己的 Prisma admin DB,設了會導致 gateway 啟動時崩潰(`ModuleNotFoundError: No module named 'prisma'`)。
- 本機開發:
  ```bash
  brew services start postgresql@14
  psql -d agent_architecture   # 進去手動查
  ```
- Table 是 `checkpointer.setup()` / `call_log.ensure_schema()` 自動建的(`CREATE TABLE IF NOT EXISTS`,冪等),不用手動 migrate。[workflows/simple_pipeline.py](../workflows/simple_pipeline.py) 的 `main()` 每次啟動都會各呼叫一次。
- call_log 寫入失敗(例如 Postgres 斷線)只會印警告,不會讓 LLM/tool 呼叫跟著失敗——這是旁路的稽核紀錄,不該影響主流程。

## Schema

### `call_log`(`call_log.py` 建的)

`gateway/client.py`、`mcp_servers/gateway.py` 每次呼叫都會寫一筆。

| 欄位 | 型別 | 說明 |
|---|---|---|
| `id` | bigserial PK | |
| `thread_id` | text | 對應哪次 workflow 執行(透過 `current_thread_id` 這個 contextvar 自動關聯,可為 null) |
| `node` | text | 對應是哪個 node 觸發的呼叫(如 `stt`/`check`/`notified`,透過 `current_node_name` 這個 contextvar 自動關聯,每個 node 函式一進去就會 set,可為 null) |
| `kind` | text | `'llm'` 或 `'tool'` |
| `name` | text | model 名字(如 `local-qwen`)或 tool 全名(如 `notified__send_gmail_message`) |
| `request` | jsonb | LLM 的 messages,或 tool 的 arguments |
| `response` | jsonb | LLM 回覆內容,或 tool 回傳文字;失敗時是 `{"error": "..."}` |
| `is_error` | boolean | |
| `latency_ms` | integer | |
| `created_at` | timestamptz | |

### checkpointer 的 4 張表(`checkpointer.setup()` 建的,都在 `public` schema)

### `checkpoints`

每個 super-step 跑完後的完整 state snapshot,是查歷史時最常用的表。

| 欄位 | 型別 | 說明 |
|---|---|---|
| `thread_id` | text | 對應一次 workflow 執行(見 `main()` 裡的 `thread_id`) |
| `checkpoint_ns` | text | namespace,子圖用的,目前 pipeline 沒有子圖,固定是空字串 |
| `checkpoint_id` | text | 這個 checkpoint 的 ID(UUID,依時間排序) |
| `parent_checkpoint_id` | text | 上一個 checkpoint 的 ID,可以串成 chain |
| `checkpoint` | jsonb | 實際內容,`checkpoint->'channel_values'` 就是那個當下的完整 state |
| `metadata` | jsonb | `metadata->>'step'`(super-step 編號,`-1` 是初始輸入)、`metadata->>'source'`(`input`/`loop`) |

PK:`(thread_id, checkpoint_ns, checkpoint_id)`。

### `checkpoint_writes`

比 `checkpoints` 更細——記錄每個 node(task)實際寫回哪些 channel,想知道「這個欄位是哪個 node 寫的」查這張表。

| 欄位 | 型別 | 說明 |
|---|---|---|
| `thread_id` / `checkpoint_ns` / `checkpoint_id` | text | 對應到哪次 checkpoint |
| `task_id` | text | 這次 super-step 裡是哪個 task(node 執行實例) |
| `idx` | integer | 同一個 task 裡第幾筆 write |
| `channel` | text | 寫的是哪個 state 欄位(例如 `transcript`、`status`) |
| `blob` | bytea | 序列化後的值 |

### `checkpoint_blobs`

大型 channel 值的獨立儲存(避免 `checkpoints.checkpoint` 太肥),一般查詢用不到,`checkpointer` 內部自己管理。

### `checkpoint_migrations`

單一欄位 `v`(schema 版本號),`setup()` 用來判斷要不要跑新的 migration,不用手動碰。

## 常用指令

```bash
# 看有哪些 thread_id 執行過
psql -d agent_architecture -c "select distinct thread_id from checkpoints;"

# 看某個 thread 每一步的 step / source
psql -d agent_architecture -c "
  select checkpoint_id, metadata->>'step' as step, metadata->>'source' as source
  from checkpoints
  where thread_id = '<thread_id>'
  order by (metadata->>'step')::int;
"

# 看某個 thread 最新一筆的完整 state
psql -d agent_architecture -c "
  select checkpoint->'channel_values' as state
  from checkpoints
  where thread_id = '<thread_id>'
  order by (metadata->>'step')::int desc
  limit 1;
"

# 看某個 thread 底下所有 LLM/tool 呼叫(含是哪個 node 觸發的)
psql -d agent_architecture -c "
  select created_at, node, kind, name, is_error, latency_ms
  from call_log
  where thread_id = '<thread_id>'
  order by created_at;
"

# 用專案內建工具查(state snapshot + call log 一起印,底層就是包上面這些查詢,不用寫 SQL)
uv run python -m persistence.history <thread_id>

# 清空所有紀錄(開發用,小心使用)
psql -d agent_architecture -c "
  truncate checkpoints, checkpoint_writes, checkpoint_blobs, call_log;
"
```

GUI 工具(TablePlus、Postico 等)連 `postgresql://localhost:5432/agent_architecture`,`checkpoint`/`metadata` 是 jsonb,GUI 通常會自動展開成樹狀結構,比 psql 純文字好讀。
