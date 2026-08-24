# 觀察執行結果

Workflow 每一步的狀態、以及每個 agent 內部打的每一次 LLM/tool 呼叫，都會寫進 Postgres（見 [persistence/](../persistence/)）。

## 查稽核歷史（兩種模式都可用）

```bash
uv run python -m persistence.history <thread_id>
```

印兩段：checkpoint（每一步的 state 快照）與 call log（每個 agent 內部呼叫了哪些 LLM/tool、輸入輸出、延遲、有沒有被 policy 擋下）。

> checkpoint 那段兩種模式現在都有：事件驅動模式的執行狀態仍然記在 `orchestrator_runs`（不是 LangGraph checkpointer），但 `orchestrator/master_agent.py` 會在每次狀態轉換後把同一份資料鏡射進 checkpoint 系列表（`persistence/event_checkpoints.py`），單純是為了讓這支稽核工具對兩種模式輸出一致——細節與已知限制見 [long-term-memory-plan.md](long-term-memory-plan.md) §2.3 與 [../TODO.md](../TODO.md)。

## 查事件驅動模式的執行狀態（目前還沒有 CLI，直接下 SQL）

```bash
psql agent_architecture -c "SELECT thread_id, workflow_name, current_step, status FROM orchestrator_runs ORDER BY updated_at DESC LIMIT 5;"
```

`status` 有四種：`running` / `completed` / `needs_review`（agent 判斷自己沒把握，需要人工介入）/ `failed`。

## 資料表一覽

| 表 | 誰寫 | 存什麼 |
|---|---|---|
| `checkpoints` / `checkpoint_blobs` / `checkpoint_writes` | LangGraph 自動 | 同步模式每一步的 state 快照 |
| `orchestrator_runs` | [../orchestrator/run_state.py](../orchestrator/run_state.py) | 事件驅動模式每次執行跑到哪一步、狀態、累積的 payload |
| `event_log` / `event_dispatch` | [../event_bus/postgres.py](../event_bus/postgres.py) | 命令/完成事件本身，以及每個 consumer group 的派送狀態 |
| `call_log` | [../persistence/call_log.py](../persistence/call_log.py) | 每一次 LLM/tool 呼叫的 request/response/延遲/是否被 policy 擋下 |
| `store` / `store_vectors` | [../persistence/memory.py](../persistence/memory.py) 的 `remember()`（應用主動呼叫，框架不會自動寫） | **長期記憶**：跨 run、跨 thread 的知識，不是某一次執行的狀態快照 |

`store` 跟 checkpoint 系列表看起來都「永久留在 Postgres」，但索引方式完全不同：checkpoint 是以 `thread_id` 為主鍵的單次執行錄影，`store` 是以 `(namespace, key)` 為主鍵、可跨 thread 查詢的知識庫。細節見 [long-term-memory-plan.md](long-term-memory-plan.md) §2。
