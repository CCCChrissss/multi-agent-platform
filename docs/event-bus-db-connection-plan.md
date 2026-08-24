# Event Bus DB 連線與查詢量實測、與連線收斂計劃

## Context

事件驅動模式（`workflows/event_driven_pipeline.py`）啟動後，Master Agent 與每個 step 的 Worker 都是常駐 process，各自持有對 Postgres 的常駐連線。這份文件記錄一次實測的結果：**這些 process 到底佔了幾條 DB 連線、一個 thread 跑完會下幾條 SQL、以及哪些變數在推動這兩個數字**，並提出把連線數收斂下來的計劃。

動機不是現在會不會出事（現在不會），而是這些數字全部來自函式庫預設值，沒有一處是刻意調過的，而它們會隨著 worker replica 數線性成長——這是平台化之後一定會撞到的東西，現在改很便宜。

量測環境：本機 Postgres 14，`max_connections = 100`（其中 3 條保留給 superuser，實際可用 97），示範場景 `stt_check_notify`（3 個 step），每個 step 1 個 worker。

---

## 一、名詞

**連線（connection）**：一條實際的 TCP 連線，加上 Postgres 那端為它 fork 出來的一個獨立作業系統 process，各自佔數 MB 記憶體。`pg_stat_activity` 一列就是一條。這是有實體成本的資源，不是「開了不用就沒差」。

**連線池（connection pool）**：預先建好數條連線放著，要用時「借」、用完「還」，連線本身不關閉。存在的理由是建立連線很貴（TCP 握手 + 認證 + Postgres fork process，通常數十毫秒），而 worker 的 claim 迴圈是永遠在跑的，每查一次就 connect/close 一次的話，這個成本就變成 process 生命週期內付不完的固定開銷。`event_bus/postgres.py` 的 module docstring 第 31-35 行寫的就是這個理由。

**`min_size`**：池子裡最少要有幾條連線。pool 啟動就先建好這麼多條放著，**即使完全沒人在用也不會關掉**——這就是「常駐佔用」的底線。psycopg_pool 的預設值是 **4**（`psycopg_pool/pool_async.py:55`）。

**`max_size`**：池子最多能長到幾條。借光了會再開新的直到這個上限，再滿就讓呼叫端排隊等（預設 `timeout=30.0` 秒後拋 `PoolTimeout`）。psycopg_pool 在 `max_size=None` 時**會把它設成等於 `min_size`**（`psycopg_pool/base.py:119-120`）——所以不傳 `max_size` 的 pool 是固定大小、不長不縮的。

**`max_idle`**：連線閒置多久後、超出 `min_size` 的部分會被回收。預設 10 分鐘。

專案裡兩處建 pool 的地方都沒傳任何大小參數，所以兩個都是「固定 4 條」：

```python
self._pool = AsyncConnectionPool(database_url, open=False)   # event_bus/postgres.py:222
_pool = AsyncConnectionPool(_database_url(), open=False)      # orchestrator/run_state.py:46
```

（`open=False` 只是「先別急著連、第一次用到再連」，跟大小無關。）

---

## 二、現況：25 條常駐連線

實測方式：啟動 `Procfile.workers` 的 4 個 process，查 `pg_stat_activity`。單獨啟動一個 worker 量到 5 條、單獨啟動 master 量到 10 條，4 個全開量到 25 條，彼此吻合。

```
worker  = 1 (LISTEN) + 4 (event bus pool)                                        = 5
master  = 1 (LISTEN) + 4 (event bus pool) + 4 (run_state pool) + 1 (checkpointer) = 10

3 worker + 1 master = 25   ← 佔可用連線預算 (97) 的 26%
```

### 各項組成

**1. LISTEN 專用連線（每個 subscription 1 條）**

`subscribe()` 會另外開一條連線專門掛 `LISTEN`（`event_bus/postgres.py:253`）：

```python
listen_conn = await psycopg.AsyncConnection.connect(self._database_url, autocommit=True)
await listen_conn.execute(f"LISTEN {_channel_for_topic(topic)};")
listener_task = asyncio.create_task(self._listen_loop(listen_conn, wake))
```

而 `_listen_loop` 是 `async for _ in conn.notifies(): wake.set()`——一個**永不結束的迴圈**，把這條連線整個佔住掛在那裡等推播。**這條連線不能拿來下查詢**，也不在 pool 的管轄範圍內（是直接 `connect()` 出來的、且是 `autocommit=True`）。這是「為什麼 worker 不是只有 1 條」的答案。

**2. event bus pool（每個 process 1 個，4 條）**

`PostgresEventBus.__init__` 建的 `self._pool`。負責 event bus 全部真正的 SQL：`publish()` 的 INSERT + `pg_notify`、`_claim_one()` 的三條查詢、`ack()`/`nack()` 的 UPDATE。master 和 worker 都各有一個（兩邊都呼叫 `get_event_bus()`）。

它跟 LISTEN 連線是互補而非二選一：pool 負責主動輪詢（正確性的保底），LISTEN 只是降延遲的最佳化——Postgres 的 NOTIFY 不持久化，沒有輪詢會漏事件。

**注意**：worker 一次只處理一則訊息（`async for delivery in deliveries` 嚴格循序），所以任何時刻最多只借 1 條，那 4 條裡有 3 條從頭到尾沒被用過。

**3. run_state pool（只有 master，4 條）**

`orchestrator/run_state.py:46` 的 module-level `_pool`，服務 `orchestrator_runs` 表（run 跑到哪一步、狀態是什麼）。只有 master 會呼叫 `create_run`/`get_run`/`advance`/`mark_terminal`/`sweep_expired_runs`；worker 完全不碰 `orchestrator_runs`。

（`event_driven_pipeline.py` 啟動時兩種角色都會呼叫 `run_state.ensure_schema()`，但那是同步的 `with psycopg.connect(...)`，離開 `with` 就關閉，不留常駐連線——實測 worker 是 5 而非 9 也印證了這點。）

**這 4 條是目前最明顯的浪費**：它跟 event bus pool 連的是同一個資料庫、同一個 `PERSISTENCE_DATABASE_URL`，只因為分屬兩個模組、各自 `AsyncConnectionPool(...)` 就變成兩個池子。

**4. checkpointer（只有 master，1 條）**

`persistence/checkpointer.py` 的 `AsyncPostgresSaver`，寫 `checkpoints`/`checkpoint_blobs`/`checkpoint_writes`。在事件驅動模式下它是**審計鏡像**（見 `orchestrator/master_agent.py` docstring）：master 每次 `advance()`/`mark_terminal()` 成功後透過 `persistence/event_checkpoints.py` 的 `record_step()` 把同一筆轉換也寫進去，讓 `persistence/history.py` 對兩種模式都能讀。`orchestrator_runs` 仍是執行控制的唯一真實來源。

它是 1 條而非 4 條，因為它不是 pool——`from_conn_string` 內部是單純一條 `await AsyncConnection.connect(...)`，一個 saver 實例綁死一條連線。master 用 `async with get_checkpointer() as checkpointer:` 包住整個 process 生命週期，所以常駐 1 條。

---

## 三、收斂計劃

已獨立成一份實作計劃：**[event-bus-connection-reduction-plan.md](event-bus-connection-reduction-plan.md)**。

四個階段把 25 條降到 5 條，其中最關鍵的認知是：**LISTEN 連線的全部工作只是「收到通知、舉個旗子」**，而 Postgres 允許一條連線同時 LISTEN 多個 channel。所以「一個訂閱一條 LISTEN 連線」是現在的實作寫法，不是機制上的必然——真正需要各自獨立的是處理迴圈（決定誰擋誰），而那只是個 Python 迴圈，不用連線。

把這兩件事分開之後，就能同時拿到「連線最少」和「隊伍最多」，不必在兩者間取捨。收斂後 worker 端的連線數不再隨 step 數成長。

那份計劃也記錄了三項動工前的前置驗證（無巢狀取得連線、`asyncio.gather` 的 contextvar 隔離、`conn.notifies()` 執行期間無法對同一條連線下 `LISTEN`），以及被否決的兩個方向（改成中央 dispatcher 推播、三個 step 合併成單一 topic）及其理由。

## 四、一併量到的：查詢量

用 monkeypatch `psycopg.AsyncCursor.execute` 計數，跑一個完整 thread（假 handler，避開 agent server 依賴），新建 workflow 名稱以排除歷史資料干擾。

**一個 thread 跑完 = 59 條 SQL statement**（S=3、R=1）：

| 角色 | 條數 | 組成 |
|---|---|---|
| trigger (`start_run`) | 4 | create_run, publish (INSERT + pg_notify), get_run |
| master | 31 | `_FAN_OUT`×4, `_CLAIM`×4, `_FETCH_EVENT`×3, `_ACK`×3, publish×2×2, run_state×6, checkpoint×7 |
| worker ×3 | 8 each = 24 | `_FAN_OUT`×2, `_CLAIM`×2, `_FETCH_EVENT`×1, publish×2, `_ACK`×1 |

**成功 claim 的次數確實是 6 次**（master 3 個 completion + 每個 worker 各 1 個 command），但那是「訊息遞送次數」不是查詢次數——一次 claim 迴圈固定跑 `_FAN_OUT` + `_CLAIM`，命中才多一條 `_FETCH_EVENT`，所以命中 3 條、空手 2 條。另外每次命中後一定會多一次空轉 claim，因為 `_deliveries` 是 yield 完就 `continue` 直接再 claim（`postgres.py:273-280`）。

**近似式**：`每 run ≈ S × (18 + 2(R-1)) + 4`（S=3,R=1 → 58，實測 59）

**閒置背景**（與有無 thread 在跑無關）：`(S×R + M) × 2 / poll_interval + M / 30秒`。現在是 4 個 subscriber × 2 條 / 60 秒 ≈ 每分鐘 8 條，加 sweeper 每 30 秒 1 條。

### 推動查詢量的變數，按影響力排

| 變數 | 影響 | 現值 |
|---|---|---|
| **workflow 步驟數 S** | 每 run 線性，係數 ~18/步。**主導項** | 3 |
| **併發 thread 數** | 直接倍乘每 run 成本 | — |
| **topic 歷史筆數** | 不改變條數，改變**每條 `_FAN_OUT` 的成本** | events topic 已 63 筆 |
| **`poll_interval`** | 閒置負載的除數 | 60s（`event_bus/factory.py` 未覆寫，吃預設） |
| **handler 執行時間** | 決定 master 在 step 之間多空轉幾次 claim | — |
| **replica 數 R** | 每 run +2/步；閒置輪詢 ×R | 1 |

注意 **S 是乘數、R 只是加數**。加 replica 不增加成功 claim 次數（`FOR UPDATE SKIP LOCKED` 保證只有一個搶到），只多出空手而回的 claim——因為 `pg_notify` 會叫醒該 channel 上**所有**訂閱者。今天看起來像「worker 數量決定一切」，只是因為現在每個 step 剛好配 1 個 worker，S 與 worker 數恰好相等。

---

## 五、順帶發現（未處理）

**1. `_FAN_OUT` 的成本是唯一會「越跑越慢」的項**

它刻意不用 watermark（`postgres.py:95-106` 的註解說明了原因：BIGSERIAL 會亂序 commit，watermark 會永久漏事件），代價是每次 poll 都要把 topic 的**全部**事件拿去跟 `event_dispatch` 做 anti-join。statement 數量不變，但單條成本隨保留的歷史線性成長。上面所有變數都是常數係數，只有這個會累積。之後需要歸檔，或在 `event_log(topic, id)` 上配合 dispatch 做 partial index。

**2. R≥2 會踩到 lease 的坑（由程式碼推導，未實測）**

`lease_seconds` 預設 30 秒，而 worker 在跑 handler 期間不會續約 lease；`_CLAIM` 會把 `status='claimed' AND visible_at <= now()` 的 row 當成 crash 重新發出。R=1 時無害（沒有別人能搶）。但 **R≥2 且 handler 超過 30 秒**（真實 STT 很可能超過），第二個 replica 會把同一則命令領走重跑。orchestration 本身安全——兩個 replica 產生的 completion event 因 `deterministic_event_id` 是同一個 UUID，`UNIQUE(event_id)` 擋掉第二筆，master 不會重複前進——但 **handler 的副作用會執行兩次**（notified step 是真的送通知）。所以加 replica 不是免費的，要嘛把 `lease_seconds` 拉到大於最慢的 handler，要嘛加 lease 續約。

**3. `persistence/call_log.py` 的 `log_call()` 每次呼叫開一條新連線**

```python
async with await psycopg.AsyncConnection.connect(_database_url()) as conn:
```

沒走 pool。事件驅動模式下 worker 是用 HTTP 打 agent server，所以記錄是在 agent server process 寫的（這也是為什麼 worker 剛好是 5、沒有多）。這些連線短命、不列入常駐基線，但它付的正是 pool 存在要避免的那個 connect/認證/fork 成本，每次 LLM 呼叫付一遍。

**4. DB 裡有 26 筆 `failed` dispatch row，是已修問題的殘骸**

交叉比對 `event_dispatch` × `event_log`：

```
stt_check_notify.stt      | stt_check_notify.commands | failed |  7
stt_check_notify.check    | stt_check_notify.commands | failed | 10
stt_check_notify.notified | stt_check_notify.commands | failed |  9
```

`stt_check_notify.commands`（13 筆事件，現無人訂閱）是「一個 workflow 共用一個 command topic」時代的產物。當時三個 step 的 group 都訂同一個 topic，`_FAN_OUT` 就把每筆 command 發給全部三個 group，worker 收到不是自己的 `event_type` 就 nack，重試到上限變 `failed`。這正是 `event_bus/base.py:15-18` 說明為何改成 per-step command topic 的那個問題——**已經修好的問題留下的殘骸，不是進行中的 bug**。要清的話可直接刪那 13 筆與對應 dispatch row。
