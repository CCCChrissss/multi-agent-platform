# Event-Driven Multi-Agent Coordination 實作計劃

## Context

目前 `workflows/simple_pipeline.py` 是**單一 process 內的 LangGraph 同步執行**：三個 node（stt/check/notified）透過 `graph.ainvoke()` 依序在同一個 Python process 裡跑完，程式檔案開頭甚至直接寫「no Kafka」——這是刻意的初始設計，不是遺漏。

依照 Figma 架構圖（Outer Structure → Multi-agent Coordination）的規劃，平台要新增一種**事件驅動的多 agent 協作模式**：Master Agent 透過 Event Bus（圖上標註「ex. Apache Kafka, DB」）發布任務命令，Worker Nodes 訂閱命令、執行、再發布完成事件回去，Master Agent 訂閱事件追蹤整體進度。

這不是要取代現有的示範 pipeline，而是要新增一條**平行的執行路徑**——因為專案目標是通用的 no-code/low-code 多 agent 平台（見 [AGENTS.md](../AGENTS.md)），Event Bus、Master Agent、Worker 協調機制都要做成平台通用能力（放基礎建設層），STT/Check/Notified 這條序列本身則要做成「設定」而非寫死在協調邏輯裡，這樣未來才可能給非工程背景使用者透過 UI 組裝別的 workflow。

已確認的兩個方向決策：
1. **Event Bus 第一階段用 DB-backed（Postgres）**，但先定義好通用的 `EventBus` 介面，之後才能無痛加 Kafka backend（比照 `persistence/checkpointer.py`「一個 factory function 藏住 backend」的既有模式）。
2. **`workflows/simple_pipeline.py` 保持原樣不動**，事件驅動模式是新增的第二條執行路徑，兩者共用同一份業務邏輯（`llm/stt_agent.py`、`llm/tsmc_judge.py`、`mcp_servers/notified/agent.py`），只有「怎麼被觸發、怎麼串起來」不同。

## 設計總覽

### 1. EventBus 抽象層 — 新增 `event_bus/`

平台基礎設施層的新目錄，跟 `gateway/`、`mcp_servers/`、`persistence/`、`harness/` 同一層級。

- `event_bus/base.py`：`Event`（`event_id`/`thread_id`/`topic`/`event_type`/`payload`）、`EventBus` protocol（`publish` / `subscribe`）、`Delivery` protocol（`ack` / `nack`），以及 topic 命名規則：
  ```python
  commands_topic(workflow_name) -> f"{workflow_name}.commands"
  events_topic(workflow_name)   -> f"{workflow_name}.events"
  ```
  這是唯一定義 topic 命名規則的地方，其他程式碼一律呼叫這兩個 helper，不直接寫字串（不會有任何 `audit-*` 這種寫死在協調邏輯裡的命名）。
- `event_bus/factory.py`：`get_event_bus()`，讀 `EVENT_BUS_BACKEND` 環境變數（預設 `postgres`）選 backend，之後要加 Kafka backend 只需新增 `event_bus/kafka.py` 並在這裡多一個分支，不動其他程式碼。這個模式直接複製 `persistence/checkpointer.py` 的 `get_checkpointer()` 寫法。
- `event_bus/postgres.py`：`PostgresEventBus` 實作。

### 2. Postgres-backed 實作細節

新增兩張表（跟 LangGraph 自己的 `checkpoints*` 與既有的 `call_log` 完全獨立，不動它們的 schema）：

```sql
CREATE TABLE event_log (
    id BIGSERIAL PRIMARY KEY,
    event_id UUID NOT NULL UNIQUE,      -- producer 端指定，這一筆訊息的冪等鍵，不代表整個 run
    thread_id TEXT NOT NULL,             -- 跟 call_log.thread_id 同一個值，不用轉名字直接 join
    topic TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE event_dispatch (
    id BIGSERIAL PRIMARY KEY,
    event_log_id BIGINT NOT NULL REFERENCES event_log(id),
    consumer_group TEXT NOT NULL,        -- "{workflow_name}.{step_name}" (or ".master") -- namespaced
                                          -- so two workflows can't claim each other's rows, since the
                                          -- claim query scopes only by consumer_group, not topic. The
                                          -- RBAC principal (current_node_name) stays the bare step name.
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','claimed','done','failed')),
    claimed_by TEXT,
    visible_at TIMESTAMPTZ NOT NULL DEFAULT now(),  -- lease 到期時間，用來 reclaim
    attempts INT NOT NULL DEFAULT 0,
    last_error TEXT,
    done_at TIMESTAMPTZ,
    UNIQUE (event_log_id, consumer_group)
);
```

- **投遞機制：polling 當正確性後盾，LISTEN/NOTIFY 當延遲優化**。理由：Postgres 的 `NOTIFY` 不是持久化的，worker 斷線期間發生的 NOTIFY 會直接遺失，這跟「至少一次送達」的要求牴觸；所以底層一定要有定時 poll `event_dispatch`（`SELECT ... FOR UPDATE SKIP LOCKED`，經典的「Postgres 當 competing-consumer queue」寫法）當作最後防線，`publish()` 額外在同一個 transaction 內發 `NOTIFY <topic>` 讓正常情況幾乎即時喚醒，最差情況也只會延遲一個 poll interval，不會真的漏掉。`psycopg` 的 async connection 本來就支援 LISTEN，不用加新依賴。
- **At-least-once + 冪等**：`claimed` 但超過 `visible_at` 的列會被下一個 poller 重新 claim（等同故障 worker 的訊息會被重派）。發布端用「決定性 `event_id`」（如 `uuid5(NAMESPACE, f"{thread_id}:{topic}:{event_type}")`）讓重複發布同一個完成事件時被 `UNIQUE(event_id)` 擋掉。消費端真正的風險是「side effect 做完但 crash 在 ack 之前」（例如 Gmail 已經寄出但沒標記 done）——這是所有 at-least-once 系統共通的殘餘風險，第一階段先接受並記錄下來，不強求完美（真正的修法是把冪等鍵傳進 Gmail/Slack tool 本身，列為後續工作）。
- **跟既有表共存**：worker process 一樣在處理訊息時 `current_thread_id.set(thread_id)` / `current_node_name.set(step_name)`，`call_log` 完全不用改，兩張表的 `thread_id` 欄位本來就是同一個值，直接 join 起來做完整稽核，不用做名字對照。

### 3. Master Agent — 新增 `orchestrator/`

新的平台基礎設施目錄（通用排程邏輯，不是 STT 場景邏輯）。

- `workflows/definitions/stt_check_notify.yaml`：**宣告式 workflow 定義**，把「STT → Check → Notified」這個序列表達成設定檔，呼應平台的 no-code 目標：
  ```yaml
  name: stt_check_notify
  steps:
    - name: stt
      command_type: stt.run
      completion_type: stt.completed
    - name: check
      command_type: check.run
      completion_type: check.completed
    - name: notified
      command_type: notified.run
      completion_type: notified.completed
  ```
- `orchestrator/workflow_def.py`：載入/驗證上述 YAML。
- `orchestrator/master_agent.py`：純粹是這份宣告式定義的直譯器——引擎程式碼裡不會出現任何 step 名稱或 type 字串，全部來自 YAML，對應 harness-engineering 檢查清單第 9 條（平台通用能力 vs 場景邏輯要分層）。收到外部觸發後寫入 `orchestrator_runs`、發布第一步命令；訂閱 `events_topic`，依每個完成事件 payload 的 `status`（`ok`/`needs_review`/`error`，對應現有 `PipelineState.status`/`needs_review`）決定推進下一步或把 run 標成終態——邏輯上等同今天 `_route()` 那個 conditional edge，只是換成用訊息內容驅動；另外用一個定期掃描 `step_deadline_at` 的機制抓「worker 完全沒回應」的卡住的 run，升級成 `needs_review`。
- `orchestrator/run_state.py`：新表 `orchestrator_runs`（`thread_id`/`workflow_name`/`current_step`/`status`/`state_payload`/`step_deadline_at`）追蹤進度。**刻意不重用 LangGraph checkpointer**：checkpointer 模型的是「同一個已編譯 graph 的中斷/續跑」，這裡要的只是「這個 run 現在跑到哪一步、完了沒」，硬塞進 `StateGraph` 反而會把事件驅動路徑綁回它原本要脫離的 in-process 執行模型，一張獨立小表更單純、也符合現有「一個能力一張表/一個 factory」的慣例。

### 4. Worker Nodes

- `orchestrator/worker.py`：通用的 consume-execute-publish 迴圈，`run_worker(step_name, handler)`：claim 一筆 dispatch → 設定 `current_node_name`/`current_thread_id` → 呼叫 `handler(payload)` → 捕捉 `AgentLoopIncomplete` 轉成 `{"status": "needs_review", ...}` → 發布完成事件 → `ack()`。
- **重用而非重寫業務邏輯**：`llm.stt_agent.transcribe`、`llm.tsmc_judge.mentions_tsmc`、`mcp_servers.notified.agent.decide_and_notify` 原封不動被呼叫。唯一新增的場景相關程式碼是 `workflows/event_driven_pipeline.py` 裡的 `STEP_HANDLERS` registry，負責把命令 payload 拆成這些函式原本的參數形狀、再把回傳值包成完成事件 payload——這段因為知道 `audio_ref`/`transcript`/`mentions_tsmc` 這些場景細節，所以照第 9 條原則放在 `workflows/`，不放進 `orchestrator/`。
- **RBAC/principal 身分完全不用改**：因為每個 worker process 專職一個 step（consumer group == step name），`mcp_servers/gateway.py` 和 `mcp_servers/policy.yaml` 零修改——gateway 一樣讀 `current_node_name.get()` 去查 `policy.yaml` 裡的 `stt`/`check`/`notified`。
- **失敗處理**：`AgentLoopIncomplete` → 完成事件帶 `status: needs_review`（dispatch 正常 ack，因為「事件送達」跟「業務結果」是兩個層次，不會造成重複派工迴圈）；真的 crash（process 被殺）則 dispatch 列卡在 `claimed`，等 lease 過期被重新派送——這是多 process 版本的「checkpointer 續跑」。

### 5. 稽核/可觀測性

`call_log` 不用改 schema：三張表的 `thread_id` 都是同一個值，直接 join `event_log`/`event_dispatch` 即可，這部分維持原案。

> **更新（見 [docs/long-term-memory-plan.md](long-term-memory-plan.md)）**：本節原本規劃「不動 checkpoint 表，改成延伸 `persistence/history.py` 去 join `event_log`/`event_dispatch`」。後來在長期記憶導入前，確認事件驅動路徑也要跟同步路徑共用同一批 `checkpoints`/`checkpoint_blobs`/`checkpoint_writes`/`checkpoint_migrations` 表（理由：長期記憶每則記憶都要能反查 `source_thread_id` 回 `call_log`/`event_log`/`checkpoints` 三處，若事件驅動的 run 沒有 checkpoints 資料，稽核鏈就不對稱），因此改採：`orchestrator/master_agent.py` 在每個 `run_state.advance()`/`mark_terminal()` 成功轉換之後，呼叫 `persistence/event_checkpoints.py` 的 `record_step()`，直接用 LangGraph checkpointer 的原始資料層 API（`aput()`/`aget_tuple()`，不經過 `compile()`/`StateGraph`）把同一次轉換寫進同一批表。放棄「延伸 history.py」這個做法的原因：那樣會產生兩套讀取邏輯（checkpoint 元組 vs event_log 列），而且沒有解決「兩條路徑真的共用同一批表」這個訴求本身。
>
> 這批事件驅動路徑寫入的 checkpoint 是**事後補寫的稽核鏡射**，不是真正可續跑的 LangGraph 狀態——`orchestrator_runs` 才是唯一的執行控制真相來源；`checkpoint_writes` 對這些 run 永遠是空的（事件驅動路徑的容錯機制是 `event_bus` 的 lease/redelivery，不是 checkpointer 的 pending-writes），`versions_seen` 也永遠是空 dict。詳見 `persistence/event_checkpoints.py` 與 `orchestrator/run_state.py` 的 docstring，以及 [TODO.md](../TODO.md) 記錄的已知限制。

## 檔案異動清單

**新增**
| 檔案 | 用途 |
|---|---|
| `event_bus/__init__.py` | re-export `get_event_bus`/`Event`/`EventBus` |
| `event_bus/base.py` | `Event`/`Delivery`/`EventBus` protocol + topic 命名 helper |
| `event_bus/postgres.py` | `PostgresEventBus`：publish/subscribe、LISTEN/NOTIFY + polling、`ensure_schema()` |
| `event_bus/factory.py` | `get_event_bus()`，依 `EVENT_BUS_BACKEND` 選 backend |
| `orchestrator/__init__.py` | — |
| `orchestrator/workflow_def.py` | 載入/驗證宣告式 workflow YAML |
| `orchestrator/run_state.py` | `orchestrator_runs` 表 + `ensure_schema()` |
| `orchestrator/master_agent.py` | 通用排程引擎 |
| `orchestrator/worker.py` | 通用 consume-execute-publish 迴圈 |
| `workflows/definitions/stt_check_notify.yaml` | STT→Check→Notified 序列設定 |
| `workflows/event_driven_pipeline.py` | `STEP_HANDLERS` registry + `--role master\|worker --step <name>` CLI 進入點 |

**修改**
| 檔案 | 異動 |
|---|---|
| `Procfile` | 新增 `master`、`worker-stt`、`worker-check`、`worker-notified` process 行 |
| `persistence/history.py` | 依 `run_id` 額外印出 `event_log`/`event_dispatch` |

**不動**（依決策 2）：`workflows/simple_pipeline.py`、`mcp_servers/gateway.py`、`mcp_servers/policy.yaml`、`persistence/call_log.py`、`persistence/checkpointer.py`、`llm/stt_agent.py`、`llm/tsmc_judge.py`、`mcp_servers/notified/agent.py`。

`pyproject.toml` 這階段不用加新依賴（`psycopg[binary,pool]` 已經涵蓋 async + LISTEN）；Kafka backend 階段才會加 `aiokafka`。

## 分階段里程碑

1. **M0 — Event Bus 骨架**：只做 `event_bus/`（base + postgres + factory），還沒有 orchestrator。驗證：兩個 process 之間 publish/subscribe smoke test；處理到一半殺掉 subscriber，確認 lease 過期後被重派；確認 NOTIFY 喚醒比 poll interval 快。
2. **M1 — Workflow 定義**：`workflow_def.py` + `stt_check_notify.yaml`，純 unit test，還沒接 runtime。
3. **M2 — 單一 worker**：`orchestrator/worker.py` + 接上真正的 `llm.stt_agent.transcribe` 的 `stt` worker。手動發一筆 `stt.run` 命令，確認轉錄結果、完成事件、`call_log` 記錄跟同步模式的 `stt` 記錄形狀一致。
4. **M3 — Master Agent 單步**：`master_agent.py` + `orchestrator_runs`，從外部觸發到 `stt` 這一步跑完、run 進終態的完整端對端流程。
5. **M4 — 全序列**：串起 `stt → check → notified`；驗證 `needs_review` 短路會擋住後續步驟的派送（模擬 `AgentLoopIncomplete`），行為對齊今天的 `_route()`。
6. **M5 — 韌性測試**：處理到一半殺掉 worker，確認重派並最終完成；重複派送同一命令，確認不會寄出兩次 Gmail 通知。
7. **M6 — 對照驗收（收尾里程碑）**：同一份輸入（`samples/gen_tsmc_01.wav`）分別跑 `workflows/simple_pipeline.py` 和事件驅動模式，diff 最終結果（transcript/mentions_tsmc/通知結果）與 `call_log` 記錄序列（node/kind/name 應一致，時間戳與 thread_id 除外），並確認 `workflows/simple_pipeline.py` 全程未被修改。

## 驗證方式

- M0：對真正的本機 Postgres 跑整合測試（`SKIP LOCKED`/`LISTEN-NOTIFY` 的語意不太能 mock）。
- M1：純 unit test，比照 `mcp_servers/policy.py` 的驗證方式。
- M2–M5：手動/腳本化注入合成事件，直接檢查 `event_log`/`event_dispatch`/`call_log`（或延伸後的 `persistence/history.py`）。
- M5：`kill -9` worker 驗證 reclaim；手動重複發布同一命令驗證不會重複寄信。
- M6：寫一支小型 parity script，同輸入跑兩種模式並 diff 輸出與 `call_log` 形狀，作為「事件驅動模式是真正平行路徑、不是分岔重寫」的長期回歸防線。

## 落地補充：worker-all 連線數實測

2026-08-04 將三個獨立 step worker 合併為 [Procfile.workers](../Procfile.workers) 的
`worker-all`。Topic、consumer group 與 `SKIP LOCKED` 認領語意沒有改變；取捨是用較小的
常駐連線數交換較大的故障範圍（單一 worker-all 中止時三個 step 同時暫停）。

在同樣啟動 `master`、三個 step 與 `memory-writer` 的條件下，當時實測常駐連線由 17
降為 12，節省 5 條。需要單獨擴充某個瓶頸 step 時，可以在 `worker-all` 之外再啟動
單 step worker；competing-consumer 與 `SKIP LOCKED` 仍會分工，不應重複處理同一命令。

這是特定版本與啟動組合的歷史量測，不是永久容量保證。修改 process 拓撲或連線池後，
必須重新量測再更新數字。
