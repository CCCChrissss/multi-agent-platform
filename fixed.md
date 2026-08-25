# Fixed（還沒做的事寫在 [TODO.md](TODO.md)）

之前記錄在 [TODO.md](TODO.md) 裡、後來已經修掉的問題，搬來這裡保留紀錄，避免和還沒解決的項目混在一起。分類與 [TODO.md](TODO.md) 一致，方便對照。

## 目錄

- [資料庫／事件驅動](#db-event)
  - [Master Agent 沒有「worker 存活但沒回應」的逾時偵測](#master-timeout)
  - [Event-driven 模式：新的 consumer_group 加進既有 topic 時，會把該 topic 的整段歷史重放一遍](#consumer-replay)
  - [NOTIFY 頻道全平台共用，任何 workflow 發布事件會吵醒所有 worker](#notify-channel)
  - [一個 workflow 的所有 step 共用同一條 commands topic，worker 越多浪費越大](#commands-topic)
  - [PostgresEventBus 完全沒有連線池，每次操作都開新連線](#connection-pool)
  - [Master Agent 轉發下一步命令時沒有過濾欄位，跟 input_schema 的 additionalProperties: false 互相矛盾](#unfiltered-forward)
  - [事件驅動路徑補寫的 checkpoint 是事後鏡射，不是真正可續跑的狀態](#event-checkpoint-shadow)
- [長期記憶與 Agent API 合約](#memory-contract)
  - [長期記憶：官方的 `compile(store=...)` 接法在事件驅動路徑上不存在，記憶要放在 agent 層](#ltm-store)
  - [Agent API 合約缺「跨執行的身分」欄位，`thread_id` 在定義上就不能當長期記憶的鍵](#envelope-context)
  - [`browse()` 的多輪鑽取沒有維護一份確定性的「已探索路徑」狀態，回溯完全靠模型自己重讀 messages 拼湊](#browse-traversal-state)
  - [`recall()`/`remember()` 沒有稽核 log，跟 `call_log` 現有的 LLM/tool 呼叫比起來是個缺口](#memory-access-audit-log)
  - [`review_memory.py` 的 approve/reject 繞過剛做完的 memory 稽核日誌，pending→active 這個最關鍵的寫入完全沒留痕](#review-approve-reject-audit-gap)
  - [`stage_candidate_for_eval.py` 只把候選規則放進 `eval` tenant，不含其他已 approve 的 active 規則——候選評測是「取代」不是「疊加」](#stage-eval-single-rule-not-cumulative)
- [服務治理與維運](#ops)
  - [用 process manager 取代手動開 terminal](#process-manager)

每項記錄三件事：**原問題**（當初為什麼是問題）、**修法**（實際怎麼改的、動到哪些檔案）、以及必要時的**驗證**（怎麼確認真的修好）。保留原問題描述是刻意的——只寫「改成 X」會讓後人看不出當初為什麼不能是 Y。

<a id="db-event"></a>
## 資料庫／事件驅動

<a id="master-timeout"></a>
### Master Agent 沒有「worker 存活但沒回應」的逾時偵測

**原問題**：[orchestrator/master_agent.py](orchestrator/master_agent.py) 每次派工都會設定 `step_deadline_at`，但沒有背景機制去掃描「已經過期還沒完成」的執行、把它們標記成 `needs_review`。worker process 真的當機的情況已經靠 event_bus 本身的 lease 過期重派解決（[event_bus/postgres.py](event_bus/postgres.py) 的 `visible_at` 機制，見 M5 韌性測試）；但如果 worker 存活卻卡住不回應（沒 crash、也沒 ack/nack），該次執行會永遠停在 `running`，不會有任何東西發現並升級它。

**修法**：新增一個跟完成事件監聽迴圈完全獨立的定期輪詢迴圈，而不是想辦法把它塞進事件驅動的路徑裡——worker 卡住不回應的定義就是「不會有任何完成事件抵達」，所以升級機制不能靠事件觸發，只能是時間驅動的輪詢。

- [orchestrator/run_state.py](orchestrator/run_state.py) 新增 `sweep_expired_runs()`：單一 `UPDATE ... WHERE status = 'running' AND step_deadline_at < now() RETURNING ...`，把符合條件的 run 標成 `needs_review`（`state_payload` 併入一段 `review_reason` 訊息、`step_deadline_at` 清空——用 `review_reason` 而不是 `error`，是為了跟 [orchestrator/worker.py](orchestrator/worker.py) 的 `AgentLoopIncomplete` 路徑（同樣是 `needs_review`）共用同一個欄位名，見下方「code review 追加修正」）。用一句 SQL 完成整個判斷＋更新是刻意的——多個 process 同時呼叫也不會重複升級同一筆 run，因為第二個呼叫的 `WHERE` 已經不會再選中被第一個呼叫改過（`status` 不再是 `running`）的那一列，不需要額外的鎖或旗標。
- [orchestrator/master_agent.py](orchestrator/master_agent.py) 新增 `run_deadline_sweeper()`：無限迴圈，每 `DEFAULT_SWEEP_INTERVAL_SECONDS`（30 秒）呼叫一次 `sweep_expired_runs()` 並印出被升級的 run。
- [workflows/event_driven_pipeline.py](workflows/event_driven_pipeline.py) 的 `--role master` 改成用 `asyncio.gather()` 同時跑 `run_master()`（原本的完成事件迴圈）和 `run_deadline_sweeper()`（新的逾時輪詢迴圈）——兩者是同一個 process 內兩條獨立的 asyncio 迴圈，不是另開一個 process/role，因為兩者都只需要輕量的 await，沒有理由為此多開一個常駐服務。

**驗證**：[orchestrator/smoke_test.py](orchestrator/smoke_test.py) 新增 `scenario_deadline_sweep`——直接用 `run_state.create_run()` 建一筆 `step_deadline_at` 設在過去的 run（模擬「worker 從來沒回應過」，不需要真的啟動一個會卡住的 worker），呼叫 `sweep_expired_runs()` 後確認該 run 被標成 `needs_review`、`step_deadline_at` 清空、`state_payload.review_reason` 有寫入逾時說明。已對照真實 Postgres 執行過一次，通過。

**code review 追加修正**：`/code-review` 對這兩個 commit 抓到三個問題，已全部修掉：

1. **`advance()`/`mark_terminal()` 沒有防止被 sweeper 搶先的保護，會把逾時升級悄悄蓋掉**——這兩個函式原本的 `UPDATE` 只用 `WHERE thread_id = %s`，沒檢查目前 status。在 `run_deadline_sweeper()` 出現之前這樣寫沒事，因為除了 `_handle_completion()` 自己不會有別人動 `orchestrator_runs.status`；但現在多了 sweeper 這個獨立的並行寫入者，會出現這種競態：worker 剛好在逾時邊緣才完成，`_handle_completion()` 讀到 run 還是 `running`，正要寫回去之前，sweeper 搶先一步把同一筆 run 標成 `needs_review`；`_handle_completion()` 手上還是舊資料，繼續呼叫 `advance()`/`mark_terminal()`，因為沒有 status 檢查，直接把狀態蓋回 `running`/`completed`/`failed`，把 sweeper 剛標好的 `needs_review` 悄悄抹掉。修法：兩個函式的 `UPDATE` 都加上 `AND status = 'running'`，並回傳 `bool`（是否真的改到列）；`orchestrator/master_agent.py` 的 `_handle_completion()` 在每個呼叫點檢查回傳值，如果是 `False`（代表被 sweeper 搶先），印一行 log 說明並跳過，而不是假裝寫入成功。
2. **逾時跟 `AgentLoopIncomplete` 的失敗，用了不同的欄位名記錄「為什麼要人工複查」**——`sweep_expired_runs()` 原本寫進 `state_payload.error`，但 [orchestrator/worker.py](orchestrator/worker.py) 的 `AgentLoopIncomplete` 路徑寫的是 `state_payload.review_reason`。同樣是 `needs_review` 的原因說明卻用兩個不同 key，未來任何要顯示「為什麼」的地方都得同時查兩個欄位才不會漏。修法：`sweep_expired_runs()` 改成寫 `review_reason`，跟既有慣例（`needs_review` 配 `review_reason`、`failed` 配 `error`）對齊。
3. **master process 重啟後，第一次掃描要等滿一個輪詢週期才開始**——`run_deadline_sweeper()` 原本迴圈一開始就先 `sleep(30)` 才第一次呼叫 `sweep_expired_runs()`，如果 process 是在某個 run 早就已經逾期之後才重啟，這筆早該被抓到的 run 要再多等最多 30 秒。修法：改成先掃一次、再進入「sleep → 掃」的迴圈，讓重啟後的第一次掃描立刻執行。

<a id="consumer-replay"></a>
### Event-driven 模式：新的 consumer_group 加進既有 topic 時，會把該 topic 的整段歷史重放一遍

**原問題**：[event_bus/postgres.py](event_bus/postgres.py) 的 `_FAN_OUT` 是用 `NOT EXISTS` 逐列比對，不是水位線：每次 `_claim_one()` 都會掃該 topic 的每一列 `event_log`，只要這個 consumer_group 還沒有對應的 dispatch 列就補一列。新 consumer_group 的 dispatch 列數是 0，所以第一個 poll tick 就會替該 topic 的整段歷史各建一列，然後全部被 claim、全部被處理——例如未來長期記憶的背景蒸餾器第一次啟動，就會把過去所有 run（包含失敗的、`needs_review` 的）一次性重播進記憶。這不是 bug，是刻意的取捨（不能用 `MAX(id)` 當水位線，理由見 `_FAN_OUT` 上方註解），但原本只能靠人工在新 consumer_group 上線前手動跑一段 SQL 把既有事件標成 `done`，漏掉不會報錯、只會靜默重放。

**修法**：把這段人工 SQL 變成 `EventBus.subscribe()` 的正式參數，讓 event_bus 自己完成標記。

- [event_bus/base.py](event_bus/base.py)：`EventBus.subscribe()` 新增 `start_from: Literal["beginning", "now"] = "beginning"` 參數（新增 `StartFrom` type alias）。預設 `"beginning"` 維持現有行為（重放全部歷史）不變；`"now"` 則是新語意：這個 consumer_group 第一次訂閱這個 topic 時，把當下已存在的事件全部標記成對它而言的 `done`（不會被投遞），之後只處理訂閱之後才發布的新事件——語意對應 Kafka 的 `auto.offset.reset`。
- [event_bus/postgres.py](event_bus/postgres.py)：新增 `_SEED_DONE_IF_NEW_GROUP` SQL，`subscribe()` 在 `start_from="now"` 時、開 LISTEN 連線之前先執行它。這句 SQL 本身就是「只在這個 (topic, group) 組合第一次出現時才做事」的完整判斷——`NOT EXISTS` 子句 join 回 `event_log` 用 `topic` + `consumer_group` 一起限定範圍（沒有依賴 `_FAN_OUT`/`_CLAIM` 既有的「一個 group 只會對應一個 topic」命名慣例），單一陳述句原子完成「檢查是不是第一次」+「補標記」，即使兩個 replica 同時對同一個全新 group 搶著訂閱也會透過 `ON CONFLICT DO NOTHING` 收斂成同一個結果，不需要額外的鎖。一旦這個 group 在這個 topic 上有過任一筆 dispatch 列（不管是這句話種下去的，还是真的處理過事件留下的），之後這句話永遠是 no-op——所以呼叫端可以每次啟動都放心傳 `start_from="now"`，不需要自己記錄「這是不是第一次跑」。跟 `MAX(id)` 水位線那個已知的坑一樣，「now」這個起點本身有一個無法避免的模糊地帶（seeding 那一刻前後發布的事件，能不能被抓到要看時間點），這是刻意接受的代價，不是 bug。
- 目前平台上唯二的正式 consumer_group（[orchestrator/master_agent.py](orchestrator/master_agent.py) 的 master 迴圈、[orchestrator/worker.py](orchestrator/worker.py) 的 step worker）都必須處理 topic 上的每一筆事件，所以都維持用預設值 `"beginning"`，呼叫端沒有任何改動——這個參數目前是「已備好但還沒有實際呼叫端用到」的平台能力，等長期記憶蒸餾器這類「事後才訂閱既有 topic」的 consumer 出現時才會真正用上。

**驗證**：[event_bus/smoke_test.py](event_bus/smoke_test.py) 新增 `scenario_start_from_now_skips_history`：對同一個 topic 先發布兩筆「舊」事件，接著用一個全新 group 以預設 `start_from`（`"beginning"`）訂閱，確認兩筆舊事件都會被重放（維持既有行為不變）；再用另一個全新 group 以 `start_from="now"` 訂閱、之後才發布第三筆「新」事件，確認只收到新事件，兩筆舊事件不會被投遞。已對照真實 Postgres 連續跑 5 次全數通過。

過程中踩到一個測試撰寫上的教訓：這個情境最初用寫死的 consumer_group 字面字串（`"smoke-now"` 等），而 `_CLAIM`（[event_bus/postgres.py](event_bus/postgres.py)）本來就只用 `consumer_group` 篩選、不分 topic——先前手動反覆重跑腳本時，斷言失敗導致的訊息沒有 `ack()`、以 `claimed` 狀態留在 `event_dispatch` 裡，租約到期後**跨 topic**被同一個字面 group 名的下一次測試重新搶到，出現偶發性的誤判。修法是讓 `scenario_start_from_now_skips_history` 比照既有 `_topic()` 的做法，改用每次執行都隨機化的 `_group()` 產生 group 名稱，避免和之前手動測試留下的殘留狀態衝突——這不是 `start_from` 這個功能本身的 bug，純粹是這次除錯過程中重跑腳本累積的測試汙染。

<a id="notify-channel"></a>
### NOTIFY 頻道全平台共用，任何 workflow 發布事件會吵醒所有 worker

**原問題**：`event_bus/postgres.py` 的 `_NOTIFY_CHANNEL` 是寫死的單一頻道（`"event_bus"`），不分 workflow、不分 topic，導致平台上任何一個事件發布都會吵醒**全平台**所有正在 LISTEN 的 worker。

**修法**：[event_bus/postgres.py](event_bus/postgres.py) 新增 `_channel_for_topic(topic)`，用 `sha256(topic)[:16]` 算出固定長度（`eb_` + 16 hex）的頻道名，`publish()`/`subscribe()` 都改成用這個 per-topic 頻道，取代原本寫死的 `_NOTIFY_CHANNEL`。理論上 hash 有極小機率撞名，但撞了也只是多醒來查一次、查到「不是我的」繼續睡，不影響正確性。

<a id="commands-topic"></a>
### 一個 workflow 的所有 step 共用同一條 commands topic，worker 越多浪費越大

**原問題**：`commands_topic(workflow_name)`（見 [event_bus/base.py](event_bus/base.py)）是每個 workflow 一條，不是每個 step 各自一條，導致 `_FAN_OUT` 把每筆命令都 fan-out 給訂閱這條 topic 的每一個 consumer_group，3 步驟工作流實測 12 筆 `event_dispatch` 裡有 6 筆是浪費（50%）。

**修法**：`commands_topic()` 改成多帶一個 `step_name` 參數，回傳 `f"{workflow_name}.{step_name}.commands"`。跟著更新了三個呼叫端：

- [orchestrator/master_agent.py](orchestrator/master_agent.py) 的 `start_run()`（發第一步命令）和 `_handle_completion()`（發下一步命令）
- [orchestrator/worker.py](orchestrator/worker.py) 的 `run_worker()`（訂閱時帶 `step_name`）
- [orchestrator/smoke_test.py](orchestrator/smoke_test.py) 裡手動組 topic 的測試情境

現在 `stt` worker 訂閱 `stt_check_notify.stt.commands`、`check` worker 訂閱 `stt_check_notify.check.commands`，每條 topic 從頭到尾只有一個 consumer_group 訂閱，`_FAN_OUT` 天生不會再幫不相關的 worker 建 dispatch 列。`worker.py` 裡的 `event_type` 比對保留當防呆，但已經不是擋「正常運作下的雜訊」了。

跟上一項（NOTIFY 頻道用 topic 算）一起做之後，喚醒範圍精準到「同一個 step」，不只是「同一個 workflow」。

**驗證**：改完後跑過 `event_bus/smoke_test.py`（M0）、`orchestrator/smoke_test.py`（M2-M5）、`workflows/parity_check.py`（M6），全數通過。

<a id="connection-pool"></a>
### PostgresEventBus 完全沒有連線池，每次操作都開新連線

**原問題**：[event_bus/postgres.py](event_bus/postgres.py) 的 `_claim_one()`、`PostgresDelivery.ack()`、`PostgresDelivery.nack()`、`publish()` 每次呼叫都各自 `await psycopg.AsyncConnection.connect(...)` 開一條全新連線，用完即關，開銷隨 worker 數量線性增加。

**修法**：在 commit `3975cb0`（`fix: close event-driven orchestrator correctness gaps from code review`）就已經處理掉了——`PostgresEventBus` 改成持有一個 `AsyncConnectionPool`（`self._pool`），`ensure_schema()`/`publish()`/`ack()`/`nack()`/`_claim_one()` 全部改用 `async with self._pool.connection() as conn`，不再逐次 `connect()`。只有 `subscribe()` 裡的 `LISTEN` 連線維持獨立的長期存活連線（這是設計上刻意如此，不是遺漏）。這項在 [TODO.md](TODO.md) 的紀錄（commit `bf4f49b`）其實寫在連線池已經修好之後，是文件沒跟著同步更新，不是程式碼真的有這個問題。

<a id="unfiltered-forward"></a>
### Master Agent 轉發下一步命令時沒有過濾欄位，跟 input_schema 的 additionalProperties: false 互相矛盾

**原問題**：[orchestrator/master_agent.py](orchestrator/master_agent.py)`_handle_completion()` 派給下一步的命令 payload 是 `merged_payload = {**run["state_payload"], **business_payload}`——刻意轉發 run 累積的**全部**欄位（docstring 講得很清楚：`check_handler` 只回傳 `{"mentions_tsmc": ...}`，不會重新轉發 `transcript`，所以只轉發這一步的 delta 會漏掉更早的步驟貢獻的欄位）。但 commit `0241748`（`feat: upgrade agent step I/O contract to real JSON Schema validation`）把 `input_schema` 也換成完整 JSON Schema、每個 step 都設了 `additionalProperties: false` 之後，沒有人同步處理這兩個設計的衝突：`stt_check_notify.yaml` 裡 `check` 的 input_schema 只允許 `transcript`，但 master 轉發過去的 payload 還帶著 `audio_ref`（`stt` 步驟的輸入,一路留在 `state_payload` 裡），於是任何三步以上的真實鏈路，第二步一定會被自己的 schema 檔死，變成 `status: "failed"`。這正是 [docs/agent-api-contract.md](docs/agent-api-contract.md)「三個現有 agent 的合約規格」那節原本講的 `input_fields`/`output_fields`（過濾轉發欄位）想解決的問題——但 `0241748` 把 `input_fields` 換成 `input_schema` 時，只搬了驗證邏輯，沒有把對應的過濾邏輯也搬過去，所以這個 bug 從那個 commit 就存在，一直沒被抓到（`orchestrator/smoke_test.py` 的 `scenario_full_chain_happy_path` 顯然沒有在那之後對著真實服務重跑過，否則會馬上炸出來)——這是在 [docs/long-term-memory-plan.md](docs/long-term-memory-plan.md) M3 驗證時、真的跑一次三步真實鏈路才發現的。

**修法**：`_handle_completion()` 在算出 `merged_payload`（維持不變，`state_payload`/checkpoint 的稽核紀錄還是要保留全部累積欄位）之後，另外算一個 `next_step_payload = {k: v for k, v in merged_payload.items() if k in next_step.input_schema.get("properties", {})}`，只有這個過濾後的版本才拿去 `bus.publish()`。`run_state.advance()` 那一行維持傳 `business_payload`（這一步自己的 delta),不受影響——過濾只發生在「要派給下一步的命令」這個單一位置，`orchestrator_runs.state_payload` 的累積語意完全不變。

**驗證**：`orchestrator/smoke_test.py` 的 `scenario_full_chain_happy_path`（既有情境，修之前用真實服務跑會炸）修完後對著真實服務（`uv run honcho start`）重新跑過，連同新增的 `scenario_memory_writer_distills_episodic`/`scenario_memory_writer_skips_needs_review` 一起全數通過。

<a id="event-checkpoint-shadow"></a>
### 事件驅動路徑補寫的 checkpoint 是事後鏡射，不是真正可續跑的狀態

**原問題**：在導入長期記憶前，事件驅動路徑的執行狀態一直只有 `orchestrator_runs`（[orchestrator/run_state.py](orchestrator/run_state.py)），跟同步路徑用 LangGraph checkpointer 記錄的 `checkpoints`/`checkpoint_blobs` 是兩套不對稱的資料——`persistence/history.py` 要對兩條編排路徑讀出一致的稽核視圖（`source_thread_id` 稽核鏈：call_log/event_log/checkpoints）時，事件驅動這邊缺一塊。

**修法**：[orchestrator/master_agent.py](orchestrator/master_agent.py) 在每個 `run_state.advance()`/`mark_terminal()` 贏得 compare-and-swap 之後，呼叫新增的 [persistence/event_checkpoints.py](persistence/event_checkpoints.py)`::record_step()`，用 LangGraph checkpointer 的原始資料層 API（`aput()`/`aget_tuple()`，不經過 `compile(checkpointer=...)`）把同一次轉換寫進 `checkpoints`/`checkpoint_blobs`。`orchestrator_runs` 仍是事件驅動路徑唯一的執行控制真相來源，這批 checkpoint 純粹是它的事後稽核鏡射，不是真正可續跑的狀態。

已知限制，都是刻意簡化不是遺漏，記在 `persistence/event_checkpoints.py` 的檔頭 docstring 裡：
- `checkpoint_writes` 對這些 run 永遠是空的——事件驅動路徑的「執行中容錯」是 `event_bus` 的 lease/redelivery（[event_bus/postgres.py](event_bus/postgres.py)），不是 checkpointer 的 pending-writes 語意，兩者不是同一件事。
- `versions_seen` 永遠是空 dict——這個欄位是 Pregel loop 內部拿來決定「哪個 node 該不該重跑」的路由資訊，事件驅動路徑沒有這層路由。
- **不要**拿事件驅動路徑寫的這批 checkpoint 去做真正的 LangGraph resume（例如接一個 `StateGraph.compile(checkpointer=...)` 然後對這個 thread_id 呼叫 `ainvoke(None, config)`）——它們背後沒有真的 compiled graph 或 pending tasks，這麼做的行為未定義。

**驗證**：`record_step()` 只在呼叫端已經贏得 compare-and-swap 之後才會被呼叫（見函式 docstring），跳過輸家路徑天生避免重複/錯序 checkpoint；[orchestrator/master_agent.py](orchestrator/master_agent.py) 的兩個呼叫點（`start_run()`/`_handle_completion()`）都已接上，`persistence/history.py` 對兩條路徑讀出一致視圖。

<a id="memory-contract"></a>
## 長期記憶與 Agent API 合約

<a id="ltm-store"></a>
### 長期記憶：官方的 `compile(store=...)` 接法在事件驅動路徑上不存在，記憶要放在 agent 層

**原問題**：LangGraph 官方教長期記憶只有一條路：`graph.compile(store=store)`、node 裡讀 `runtime.store`——這預設你有一張 compiled StateGraph。但平台上兩套編排模式只有一套有圖：同步路徑（[workflows/simple_pipeline.py](workflows/simple_pipeline.py)）有，事件驅動路徑（[workflows/event_driven_pipeline.py](workflows/event_driven_pipeline.py) + [orchestrator/](orchestrator/)）完全沒有——它的執行狀態走 `orchestrator_runs`（[orchestrator/run_state.py](orchestrator/run_state.py)），該檔案的 docstring 已經說明是刻意不用 checkpointer 的。照官方教學做，記憶只會落在 `simple_pipeline.py` 上，而那正是 [workflows/parity_check.py](workflows/parity_check.py) 用 `_assert_simple_pipeline_untouched()` 明文凍結、也不是主要在開發的那條路。

更根本的問題是「記憶要放哪一層」，三個候選：**編排層**（[workflows/event_driven_pipeline.py](workflows/event_driven_pipeline.py) 的 `STEP_HANDLERS`，換編排模式記憶就消失、同步路徑也拿不到）、**LangGraph 圖上**（`compile(store=...)`，只有同步路徑有）、**Agent 層**（[agents/](agents/) 各自的 `server.py` + [llm/](llm/) 的 agent function，不管誰呼叫、用哪種傳輸層都跟著走）。

**修法**：選 Agent 層，判準跟 [docs/agent-api-contract.md](docs/agent-api-contract.md) 當初讓 envelope 跟傳輸層脫鉤是同一個——記憶要跟編排模式脫鉤，做成 [persistence/](persistence/) 底下跟 [persistence/checkpointer.py](persistence/checkpointer.py) 平行的獨立元件。釐清 `store` **不是** `orchestrator_runs` 的替代品，是第三件事：`checkpoints` 是同步路徑的執行狀態、`orchestrator_runs` 是事件驅動路徑的執行狀態、`store` 是跨越所有執行的知識。

已落地：
- **M1（基礎設施）**：[persistence/memory_store.py](persistence/memory_store.py)、[persistence/memory.py](persistence/memory.py)、[persistence/memory_policy.py](persistence/memory_policy.py)。
- **M2（讀取端）**：`check`/`notified` 接上 `recall()`/`remember()`；[agents/envelope.py](agents/envelope.py) 的 `AgentRequest` 加上 `context: dict` 欄位承載 `tenant_id`/`user_id` 這類跨 run 身分維度——`thread_id`（這一次）對 `context`（跨越很多次）的關係，對應 checkpointer 對 store 的關係往上搬一層。
- **M2.1（共用 helper）+ M2.2（procedural/episodic 讀取端通用化）**：[persistence/memory_lifespan.py](persistence/memory_lifespan.py)/[persistence/memory_prompt.py](persistence/memory_prompt.py) 成為 `check`/`notified`/`stt` 三個 agent 共用的平台函式（`stt`/`notified` 的 procedural/episodic 因為沒有 `policy.yaml` grant，實際跑起來是 no-op）。
- **M3（背景蒸餾寫入）**：[orchestrator/memory_writer.py](orchestrator/memory_writer.py) 訂閱完成事件，把 `check` 每次成功判斷蒸餾成 episodic 記憶寫回同一個 namespace，寫入端跟讀取端第一次真正接起來。

設計與檔案級異動見 [docs/long-term-memory-plan.md](docs/long-term-memory-plan.md) M2/M2.1/M2.2/M3、[docs/agent-api-contract.md](docs/agent-api-contract.md)「後續:記憶讀取端平台化」。

**驗證**：[persistence/memory_smoke_test.py](persistence/memory_smoke_test.py)；[orchestrator/smoke_test.py](orchestrator/smoke_test.py) 的 `scenario_memory_writer_distills_episodic`/`scenario_memory_writer_skips_needs_review`；[workflows/parity_check.py](workflows/parity_check.py) 確認同步路徑不受影響。全數通過。

**尚未完成、留在 [TODO.md](TODO.md) 的後續項**：這次落地的只有「記憶要放哪一層」這個核心架構決策；再往上的精修項還沒做，各自獨立追蹤——語意混合檢索排序見 [TODO.md](TODO.md#memory-hybrid-retrieval)，`needs_review` 品質關卡的裁決入口見 [TODO.md](TODO.md#needs-review-decision-entry)，procedural/semantic 的 LLM 判斷式蒸餾見 [TODO.md](TODO.md#memory-writer-llm-judgment)。M3 寫進去的記憶目前沒有分級，`recall()` 讀到就能用，是刻意接受的現況，不是遺漏。

<a id="envelope-context"></a>
### Agent API 合約缺「跨執行的身分」欄位，`thread_id` 在定義上就不能當長期記憶的鍵

**原問題**：[agents/envelope.py](agents/envelope.py) 的 `AgentRequest` 原本只有 `thread_id` + `input`。但 `thread_id` 每次執行都是新的 uuid（[orchestrator/trigger.py](orchestrator/trigger.py)、[workflows/simple_pipeline.py](workflows/simple_pipeline.py)），**保證永不重複**；而長期記憶的定義就是跨執行。也就是說這份 request 裡沒有任何欄位能當記憶的 namespace 鍵——不是欄位取錯，是這個維度根本不存在。

直覺的修法（把 `tenant_id`/`user_id` 塞進 `input`）會壞在三個地方：會被 [orchestrator/workflow_def.py](orchestrator/workflow_def.py) 的 `validate_output` 拒絕未宣告的多餘欄位；會污染整條 pipeline——[orchestrator/master_agent.py](orchestrator/master_agent.py) 把每步產出合併進 `state_payload` 整包往下傳，身分欄位一旦進了 payload 就得在 YAML 每個 step 的 `input_fields` 重新宣告，還會被當業務資料寫進 `orchestrator_runs.state_payload`；這正是合約文件已經否決過的錯誤——當初把 `output` 巢狀化（而不是攤平）就是為了讓治理欄位不跟業務欄位共用命名空間，塞進 `input` 是同一個錯誤換個位置犯。也不能靠 contextvar 隱式帶入，因為 contextvar 只在同一 process 內有效，而這個欄位的重點就是**由呼叫端明確宣告「這次是替誰做事」**。

**修法**：在 envelope 加第三個 top-level 欄位 `context: dict`（預設 `{}`），承載 `tenant_id`/`user_id` 這類跨 run 的身分維度，跟 `input`（本次業務資料）分開。`thread_id`（這一次）對 `context`（跨越很多次）的關係，對應 checkpointer 對 store 的關係往上搬一層。

**驗證**：[agents/envelope.py](agents/envelope.py) 的 `AgentRequest` 已有 `context: dict` 欄位；合併後的 [agents/runtime.py](agents/runtime.py) 各 route 都讀 `context.get("tenant_id", "default")` 決定記憶 namespace 的 tenant。動到的檔案見 [docs/long-term-memory-plan.md](docs/long-term-memory-plan.md) 的 M2。

<a id="browse-traversal-state"></a>
### `browse()` 的多輪鑽取沒有維護一份確定性的「已探索路徑」狀態，回溯完全靠模型自己重讀 messages 拼湊

**原問題**：[llm/exclusion_judge.py](llm/exclusion_judge.py) 的 `judge_exclusion()` 迴圈裡，`browse_semantic_memory` 每一輪的呼叫結果（[persistence/memory.py](persistence/memory.py)`::browse()` 回傳的 `children`/`items`/`parent`/`siblings`）只是原樣塞進 `messages` 累積下去，「模型記不記得自己探索到哪、要怎麼回到上一層」完全靠它每次重新讀一遍完整對話歷史自己拼湊——沒有任何一份由 Python 端確定性維護、能保證正確的「已探索路徑」結構。當時判斷這是刻意的設計選擇（[docs/exclusion-scenario-plan.md](docs/exclusion-scenario-plan.md) §2.3 要驗證的就是「agent 能不能靠自己在沒有平台預先規劃路徑的情況下完成漸進式揭露」），先記在 TODO 待議，不是遺漏。

**修法**：跟 `seen_articles`（引用驗證用）已經在用的直覺一致——不相信模型自己講得出它探索過哪裡，用 Python 端結構確定性追蹤，但刻意只做到「記住已探索到什麼」，不做「建議下一步去哪」，避免平台替模型做掉導航決策本身。

- [persistence/memory_prompt.py](persistence/memory_prompt.py) 新增兩個平台層函式，跟既有的 `inject_procedural()`/`recall_episodic_few_shot()` 並列、同樣是「純機械式、不知道呼叫端是誰」的模式：`track_browse_result(explored, browse_result_json)` 把一次 `browse()` 的原始 JSON 結果，以其 `scope` 為 key 寫進呼叫端自己持有的 `explored: dict[tuple[str, ...], dict]`；`render_explored_map(explored)` 把累積的 `explored` 渲染成一份精簡地圖文字，`{}`（還沒探索過任何東西）渲染成 `""`。
- [llm/exclusion_judge.py](llm/exclusion_judge.py) 的 `judge_exclusion()` 迴圈裡，每輪 `browse_semantic_memory` 呼叫後即時 `track_browse_result()` 累積進 `explored`，並把 `render_explored_map(explored)` 的結果餵給模型；原本獨立維護的 `seen_articles` 集合拿掉，改成 `_seen_articles(explored)` 直接從同一份 `explored` 衍生——不再是兩份平行追蹤同一件事的狀態。

**驗證**：`track_browse_result()`/`render_explored_map()` 的單元層級檢查（縮排、已讀標記、denied/malformed 結果會被安靜跳過）；確認渲染出的地圖真的進了 message history、不只是不會噴例外；完整跑過 [persistence/memory_smoke_test.py](persistence/memory_smoke_test.py)、`gather_concurrency_smoke_test.py`；透過 P5 已接好的 check-agent/notified-agent HTTP service 做過正、負兩種案例的真實端到端往返（正案例的通知有透過 `memory__recall_semantic_memory` 正確撈到預先寫入的收件人偏好）；[workflows/parity_check.py](workflows/parity_check.py) 重跑過，確認舊的 TSMC 場景不受影響。

<a id="memory-access-audit-log"></a>
### `recall()`/`remember()` 沒有稽核 log，跟 `call_log` 現有的 LLM/tool 呼叫比起來是個缺口

**原問題**：[persistence/memory.py](persistence/memory.py) 的 `recall()`/`remember()`/`browse()` 直接呼叫 `store.asearch()`/`store.aput()`/`store.alist_namespaces()`，完全沒有經過 [persistence/call_log.py](persistence/call_log.py) 的 `log_call()`——那個函式只在 [gateway/client.py](gateway/client.py)（LLM 呼叫）跟 [mcp_servers/gateway.py](mcp_servers/gateway.py)（tool 呼叫）裡被呼叫過，`call_log` 表的 `kind` 欄位也只接受 `'llm'`/`'tool'`。讀取撈到幾筆、撈到哪些 key 完全空白；policy denied 只印到 stderr，行程重啟就消失，不是可查詢的稽核資料。

**修法**：比照 `call_log` 現有 `'llm'`/`'tool'` 兩種 kind，加第三種 `'memory'`。

- [persistence/call_log.py](persistence/call_log.py)：CHECK constraint 加入 `'memory'`（新的 `_ADD_MEMORY_KIND` 遷移）。順帶修掉一個既有的遷移排序 bug——舊的 `_MERGE_STT_INTO_LLM_KIND` 每次 `ensure_schema()` 都會把 constraint **重建**回只剩 `('llm', 'tool')`，這件事本來沒事，因為在它之後沒有人再放寬過；但現在 `_ADD_MEMORY_KIND` 要在它之後把 constraint 放寬到包含 `'memory'`，於是只要 DB 裡已經有一筆 `kind='memory'` 的資料，下一次 `ensure_schema()` 呼叫就會在跑到 `_MERGE_STT_INTO_LLM_KIND` 那一步時被自己重建的舊 constraint 卡到報 `CheckViolation`（本地測試時實際炸到這個問題）。改法：`_MERGE_STT_INTO_LLM_KIND` 只保留那句 `UPDATE`（把歷史遺留的 `kind='stt'` 併回 `'llm'`，這句本身天生冪等），constraint 的定義權整個交給跑在它之後、也是目前最後一個 kind 遷移的 `_ADD_MEMORY_KIND` 統一維護。
- [persistence/memory.py](persistence/memory.py) 新增 `_log_memory_call()`，`recall()`/`browse()`/`remember()` 三個函式的成功路徑跟 `_log_denied()` 的拒絕路徑都接上：`name` 記動作名稱（`recall`/`browse`/`remember`），`request` 記 namespace/query/limit/key 這類中繼資料，`response` 只記筆數與 key 清單（`recall`）或 children/items 計數（`browse`），**不記原始 content**——跟 [gateway/client.py](gateway/client.py) `embed_texts()` 只記 `dims` 不記向量本身同一個原則，避免 `call_log` 變成收件人偏好這類個資的第二份副本。`latency_ms` 用 `time.monotonic()` 量測真正的 store 呼叫耗時。
- **已知限制，記在 `_log_memory_call()` 的 docstring 裡**：`mcp_servers/memory/server.py` 這個 stdio subprocess 只在啟動時把 `MCP_CALLING_PRINCIPAL` 這個固定不變的值讀進 `current_node_name`（[mcp_servers/gateway.py](mcp_servers/gateway.py) 的 `connect()` 只在 spawn 時傳一次），但 `current_thread_id` 是每個請求都不同、只在呼叫端（`agents/envelope.py`）的 process 內每次請求設定一次的 ContextVar，沒有辦法用同一招（單次 env var）跨 stdio 邊界傳過去——所以透過 `recall_semantic_memory`/`browse_semantic_memory` 這兩個 MCP tool 觸發的 `kind='memory'` 稽核行，`node` 欄位是對的，但 `thread_id` 永遠是 `None`。跨 request 傳遞需要在 MCP 協定層另外設計（例如把 thread_id 當成每次呼叫的隱藏參數），這次刻意不做，只記下這個限制。

**驗證**：[persistence/memory_smoke_test.py](persistence/memory_smoke_test.py) 新增 `scenario_audit_log`——用跟 `scenario_browse_tree` 一樣的手法（一份 throwaway `MemoryPolicy`，不碰 `mcp_servers/policy.yaml` 那個明文凍結、待主管拍板的 `memory:` 區塊），對一個固定 `thread_id` 依序做一次成功 `remember()`、一次成功 `recall()`、一次被拒絕的 `recall()`，再用 `fetch_calls(thread_id)` 撈出 `kind='memory'` 的三筆稽核行，斷言 `denied`/`request`/`response` 的形狀都符合預期（尤其 `response` 沒有原始 content、只有計數）。對照真實 Postgres（另外起 Ollama + LiteLLM Gateway 供 `remember()` 觸發的 embedding 呼叫用）連續跑過，連同其餘既有情境全數通過；[workflows/parity_check.py](workflows/parity_check.py) 重跑到 `ensure_call_log_schema()` 這一步確認排序修正沒有讓既有情境炸掉（後段因為 `services/stt` 沒啟動而失敗，是環境缺口，跟這次改動無關）。

<a id="review-approve-reject-audit-gap"></a>
### `review_memory.py` 的 approve/reject 繞過剛做完的 memory 稽核日誌，pending→active 這個最關鍵的寫入完全沒留痕

**原問題**：[persistence/memory.py](persistence/memory.py) 的 `remember()` 每次寫入都會透過 `_log_memory_call()` 留下 `kind='memory'` 的 `call_log` 稽核行（見上一則 [memory-access-audit-log](#memory-access-audit-log)）。但 [scripts/review_memory.py](scripts/review_memory.py) 的 `_approve()`/`_reject()`（P3 人工審核 CLI，把 `pending` 候選規則升級成 `active`，或直接刪除）是直接呼叫 `store.aput()`/`store.adelete()`，完全繞過 `remember()`，因此也繞過了 `_log_memory_call()`——整條蒸餾管線裡最需要留痕的一步（procedural 規則從「機器產生、還不能生效」變成「真的會被注入 production prompt」）反而在稽核日誌裡完全空白。

**修法**：`persistence/memory.py` 新增兩個共用函式，讓「編輯既有記憶」跟「刪除既有記憶」都走跟 `remember()`/`recall()` 一樣的 `can_write()` 檢查 + `_log_memory_call()` 稽核路徑，而不是讓呼叫端直接碰 `store`：
- `edit(store, policy, kind, *, tenant, scope, key, value)`：原地覆寫一筆既有記憶的完整 `value`。跟 `remember()` 不同，不會自動蓋掉 `created_by`/`source_thread_id` 等稽核欄位——呼叫端要自己組出完整的 `value`（保留原始欄位，只改要改的部分），因為 approve 這個動作語意上就是「編輯」不是「新建」。
- `forget(store, policy, kind, *, tenant, scope, key)`：刪除一筆既有記憶，同樣經過 `can_write()`/`_log_memory_call()`。

[scripts/review_memory.py](scripts/review_memory.py) 的 `_approve()` 改呼叫 `edit()`（原本組 `updated` dict 的邏輯不變，只是把最後一步從 `store.aput()` 換成 `edit()`），`_reject()` 改呼叫 `forget()`；兩者都要多接一個 `memory_policy` 參數（`main()` 裡 `open_agent_memory()` 已經有這個物件，原本只是沒傳進去）。P4 的 edit-in-place（重打 `rule` 文字）流程本來就已經呼叫 `remember()`，稽核路徑早就是通的，不受影響。

**驗證**：[persistence/memory_smoke_test.py](persistence/memory_smoke_test.py) 的 `scenario_audit_log` 擴充——在既有的 `remember()`/`recall()`（含被拒）斷言之後，多加一組 `edit()`/`forget()` 對同一個 `audit_test/probe` 的操作，斷言 `fetch_calls()` 撈得到 `("edit", "audit_writer")`/`("forget", "audit_writer")` 兩筆稽核行且 `denied is False`、`request["key"] == "probe"`。對照真實 Postgres 跑過 `uv run python -m persistence.memory_smoke_test`，連同其餘既有情境全數通過。

<a id="stage-eval-single-rule-not-cumulative"></a>
### `stage_candidate_for_eval.py` 只把候選規則放進 `eval` tenant，不含其他已 approve 的 active 規則——候選評測是「取代」不是「疊加」

**原問題**：`evals/run_eval.py --tenant eval` 原本被期待測的是「候選規則 + 目前所有已核准規則」疊加起來的效果，實際上不是。[scripts/stage_candidate_for_eval.py](scripts/stage_candidate_for_eval.py) 的 `stage()` 對 procedural 這步是「先 `_wipe()` 清空 `eval/procedural/<scope>`，再只 `remember()` 這一條候選」——`eval` tenant 的 procedural namespace 裡永遠只有正在測的這一條，不會把 `default` tenant 現有的其他 active 規則一起帶進去。實際踩到的場景：`default/procedural/stt_exclusion_notify/check` 已經 approve 過 1 條規則（`pending-e9b8205f...`）之後，再測第二條候選（`pending-c5f7e6a6...`），`eval` tenant 裡就只剩新候選、approve 過的那條完全不在裡面——baseline（`default`，讀到全部 active 規則）跟 candidate（`eval`，只有新候選）比的其實是「新規則單獨存在 vs 已核准規則單獨存在」，不是正式環境真實會發生的「已核准規則 + 新規則一起疊加」（`inject_procedural()` 一次讀最多 `llm/exclusion_judge.py::_PROCEDURAL_LIMIT=10` 條 active 規則，全部疊在一起）。規則越多，這個落差越會讓評測數字失去意義，而且不會有任何錯誤訊息提醒。

**修法**：`stage()` 對 procedural 那步從「wipe + 只寫入這一條候選」改成「wipe → 先鏡射 `source_tenant`（`default`）目前所有 active 規則 → 再疊加這一條候選」，邏輯跟原本 episodic 那步的鏡射一致（同樣用 `index=False`，因為內容是已經在 `source_tenant` 內嵌入過的逐字複製）。新增 `_PROCEDURAL_MIRROR_LIMIT = 200`，理由跟 `scripts/distill_procedural.py::_RULE_LIMIT` 一樣：procedural 是人審核過才進來的，量體不會大到需要真的分頁。

**驗證**：對真實服務跑 `uv run python -m scripts.stage_candidate_for_eval --key pending-c5f7e6a6-e863-43d9-ab7b-88436150426e --scope stt_exclusion_notify/check`，輸出多印出一行 `mirrored 1 active procedural rule(s) from default into eval/procedural/...`；`psql` 查 `eval.procedural.stt_exclusion_notify.check` 確認同時存在已核准的 `pending-e9b8205f...`（status=active）跟新候選 `pending-c5f7e6a6...`（status=active）兩筆，不再是只有一筆。

<a id="ops"></a>
## 服務治理與維運

<a id="process-manager"></a>
### 用 process manager 取代手動開 terminal

**原問題**：跑一次完整的 workflow 要手動開一堆 terminal，各自 `uv run ...` 啟動 Ollama、LiteLLM Gateway、STT service、通知 service，關閉時還要一個一個關，容易留下殘留 process。事件驅動模式加進來之後更誇張——再加上 Master Agent 跟三個 worker，總共要開 6 個 terminal。

**修法**：分成兩份 [Procfile](Procfile) + [honcho](https://github.com/nickstenning/honcho)，各自一個指令啟動、`Ctrl+C` 一次全部連帶關閉，不留殘留 process：

- [Procfile](Procfile)（`uv run honcho start`）：兩種執行模式共用的 7 個常駐服務——Ollama、LiteLLM Gateway、STT service、通知 service，以及 `stt`/`check`/`notified` 三個 agent 的 HTTP service（port 8003~8005）。
- [Procfile.workers](Procfile.workers)（`uv run honcho -f Procfile.workers start`）：事件驅動模式的編排層——Master Agent + 每個 step 一個 worker。

**為什麼刻意分成兩份，而不是併成一份**：[orchestrator/smoke_test.py](orchestrator/smoke_test.py) 與 [workflows/parity_check.py](workflows/parity_check.py) 會在自己的 process 內起 master/worker，consumer group 名稱（`{workflow}.{step}`）跟 `Procfile.workers` 這批完全同名。因為 event_bus 是 competing consumers 設計（`FOR UPDATE SKIP LOCKED`），這批 process 只要在背景跑著，就會跟測試搶同一批命令——測試裡刻意用假 handler 的情境（`needs_review_short_circuit` 的 `guard_check_handler`、`worker_crash_recovery` 的 `failing_stt_handler`）會被真 handler 接走，測試變成靜默地測了錯的東西。分開放，跑 smoke test 前只要關掉 `Procfile.workers` 那批即可。這個理由寫在 [Procfile.workers](Procfile.workers) 的註解與 [README.md](README.md) 兩處。

**驗證**：`honcho check` 對兩份 Procfile 都通過；實跑 `honcho -f Procfile.workers start`，4 個 process 正常啟動，`Ctrl+C` 後四個都收乾淨（rc=143），`pgrep -f event_driven_pipeline` 確認無殘留。

**後續**：之後如果要接近正式部署，改用 `docker-compose`——需要先幫每個 service 補 Dockerfile，並解決 [TODO.md](TODO.md) 裡「[服務位址目前是寫死的 localhost port](TODO.md#service-registry)」那一項。
