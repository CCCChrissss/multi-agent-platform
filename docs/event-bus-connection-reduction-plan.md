# Event Bus 連線收斂實作計劃

## Context

事件驅動模式目前常駐 **25 條** Postgres 連線（實測，量測方法與完整拆解見 [event-bus-db-connection-plan.md](event-bus-db-connection-plan.md)）。這些數字全部來自函式庫預設值，沒有一處是刻意調過的，而且會隨 step 數與 replica 數線性成長——平台化之後（多 workflow、多 replica）一定會撞到 `max_connections`。

這份計劃把常駐連線從 **25 降到 5**，分四個階段，每階段可獨立上線、獨立回退。

**核心原則：不動訂閱模型。** 收斂的對象是「連線」，不是「訂閱」。三個 step 各自擁有獨立的 consumer_group 與處理迴圈這件事必須保留——那是背壓、按 step 獨立擴展、以及「生產者不需要知道消費者」的來源。討論過但**否決**的兩個方向：

- **改成中央 dispatcher 用 HTTP 推給各 agent**：會失去持久投遞、背壓、`SKIP LOCKED` 的零協調水平擴展，而且每加一個消費者就要改 dispatcher。省的成本用下面的方法都拿得到。
- **三個 step 合併成單一 topic / consumer_group**：只省 2 條連線，卻換來 head-of-line blocking（慢的 STT 擋住快的 check）、無法只擴充瓶頸那一步、以及失去 `event_type` 不符時的 nack 保護。

---

## 目標

| 階段 | 改動 | worker 小計 | master | 總計 |
|---|---|---|---|---|
| 0 | 現況 | 15 | 10 | **25** |
| 1 | pool 顯式設 `min_size=1, max_size=4` | 6 | 4 | **10** |
| 2 | master 的 event_bus / run_state 共用 pool | 6 | 3 | **9** |
| 3 | 一個 process 跑全部 step 的 worker | 6（實測，見階段 3「實測結果與勘誤」） | 3 | **9** |
| 4 | 共用 listener 連線（一條 LISTEN 聽多個 channel） | 4（實測：1 listener + 3 pool，見階段 4「實測結果」） | 3 | **7** |

> **勘誤（實測後更新）**：階段 3／4 原先推導 worker 小計會分別降到 4、2，實測都卡在同一個根因——三個 step 迴圈共用 pool 時偶爾同時借用，讓 `psycopg_pool` 的收縮條件永遠等不到一個完全無重疊的視窗，pool 端穩定停在 3 條，不會降到 1。階段 4 確實把 **LISTEN 連線**從 3 條收斂成 1 條（這階段真正改到的地方），但 pool 沒變，所以 worker 小計是 4 不是 2。細節見各階段小節末的「實測結果」。

理論上共用 listener 之後，worker 的 LISTEN 連線數不再隨 step 數成長（一條連線想聽幾個 channel 都行）——這部分已實測確認成立。但 pool 連線數是否也能不隨 step 數成長，取決於能不能解決上述的共用 pool 收縮問題（目前沒解，留在階段 3 小節的「若之後真的要...」給出可行方向）。

尖峰時 pool 可長到 `max_size`，上界為 worker 5（1 listener + 4 pool）+ master 6（1 listener + 4 pool + 1 checkpointer）= **11**——閒置 5、最忙 11，對照現況的「不分忙閒都佔 25」。

---

## 前置驗證（已完成）

這三件事會決定計劃可不可行，動工前已經查過：

**① 沒有任何路徑握著一條連線又去要第二條** — `min_size=1` 才不會死鎖。追過全部路徑：`_claim_one` 的三條查詢在同一個 `async with pool.connection()` 內，而且是先 `return` 才輪到 `_deliveries` 去 `yield`（`postgres.py:288-292` 的 `async with` 在 return 前已離開）；`_handle_completion` 是 `get_run → publish → advance → record_step`，每步各自借了就還。**無巢狀取得。**

**② `asyncio.gather` 的 contextvar 是隔離的** — 階段 3 把三個 `run_worker` 放進同一個 process 的前提。`gather` 會把每個 coroutine 包成獨立 Task，各自拿到 `copy_context()` 的副本，所以 stt 迴圈設的 `current_thread_id` 不會漏到 check。而且 `worker.py` 是在 `_handle_one` **內部**才 set，不是建立 task 之前。

**③ `conn.notifies()` 執行期間不能對同一條連線下 `LISTEN`** — 階段 4 的關鍵限制。psycopg 的 `notifies()` 用 `async with self.lock:` 包住整個 `while True`，只有 generator 結束才釋放；此時從別的 task 呼叫 `conn.execute("LISTEN ...")` 會永久卡在鎖上。**所以階段 4 不能「動態加 channel」，必須採取「停掉 listener → 加 channel → 重啟 listener」的做法**（詳見下方）。

---

## 階段 1 · Pool 大小顯式化

**改什麼**

```python
# event_bus/postgres.py:222
self._pool = AsyncConnectionPool(database_url, open=False, min_size=min_size, max_size=max_size)

# orchestrator/run_state.py:46
_pool = AsyncConnectionPool(_database_url(), open=False, min_size=1, max_size=4)
```

`PostgresEventBus.__init__` 增加 `min_size: int = 1, max_size: int = 4` 兩個參數。

**為什麼不是只寫 `min_size=1`**

psycopg_pool 在 `max_size=None` 時會把它設成等於 `min_size`（`psycopg_pool/base.py:119-120`），所以只寫 `min_size=1` 等於把硬上限也壓成 1。master 是真的有並行的——`event_driven_pipeline.py:82-85` 用 `asyncio.gather` 同時跑 `run_master` 和 `run_deadline_sweeper`，兩者會搶同一條連線。不會壞（psycopg_pool 會排隊，預設 `timeout=30.0` 秒），但 `_FAN_OUT` 是會隨 topic 歷史變慢的那條，等它慢下來 sweeper 就會被擋住。

`min_size=1, max_size=4` 的效果是閒置只佔 1 條、忙起來能長到 4 條、`max_idle`（預設 10 分鐘）後自己縮回 1。成本為零，嚴格優於寫死 1。

**風險** 低。純參數，語意不變。前置驗證 ① 保證不死鎖。

**驗收** 啟動 `Procfile.workers`，`SELECT count(*) FROM pg_stat_activity WHERE datname='agent_architecture' AND application_name=''` 應為 10（現況 25）。跑一次 `orchestrator/smoke_test.py` 全綠。

---

## 階段 2 · Master 的兩個 pool 合併

**現況** master 對**同一個** `PERSISTENCE_DATABASE_URL` 開了兩個 pool，只因為分屬兩個模組：`event_bus/postgres.py` 的 `self._pool` 和 `orchestrator/run_state.py` 的 module-level `_pool`。

**改什麼**

新增 `persistence/pool.py`，提供這個 process 對 persistence DB 的共用 pool：

```python
_shared: AsyncConnectionPool | None = None

async def get_shared_pool() -> AsyncConnectionPool:
    """Process 內共用的 persistence DB 連線池。event_bus 與 run_state 都走它，
    避免同一個 DB 開兩個池子各佔 min_size 條。"""
```

- `run_state._get_pool()` 改成回傳 `get_shared_pool()`
- `PostgresEventBus.__init__` 增加 `pool: AsyncConnectionPool | None = None`；傳入就用傳入的，沒傳就照舊自己建（保留「指向另一個 DB 的 bus」這個可能性，不綁死）
- `event_driven_pipeline.py` 的 master 分支把共用 pool 傳給 `get_event_bus()`

**交易語意不變** 兩邊本來就都用 `async with pool.connection()` 的隱式 commit，合併後完全一樣。

**checkpointer 不併進來** langgraph 的 `AsyncPostgresSaver` 雖然接受 `AsyncConnectionPool`（`Conn = AsyncConnection | AsyncConnectionPool`），但 `from_conn_string` 建連線時帶了 `autocommit=True`，而 event_bus / run_state 依賴隱式交易把 `_claim_one` 的三條查詢包成一個交易。共用會改變交易語意，需要實測才能確認，不放進這次範圍。省下的 1 條連線不值得這個風險。

**風險** 低到中。要確認 master 的兩個並行迴圈在 pool 只有 1 條時不會互相餓死——`max_size=4` 已經留了餘裕。

**驗收** master 單獨啟動時連線數應為 3（現況 10）。`smoke_test.py` 的 `full_chain_happy_path` 與 `worker_crash_recovery` 全綠。

---

## 階段 3 · 一個 process 跑全部 step

**改什麼** `workflows/event_driven_pipeline.py` 增加一個角色，例如 `--role worker --step all`：

```python
bus = get_event_bus()          # 只建一次 → 只有一個 pool
await asyncio.gather(*[
    run_worker(bus, workflow_def, step, handlers[step], worker_id=f"{step}-{suffix}")
    for step in handlers
])
```

topic、consumer_group、扇出、搶單全部不變——只是三個處理迴圈搬進同一個 process、共用一個 pool。

**必須保留單 step 模式** 現有的 `--step stt` 不能拿掉，理由有二：

1. **之後要擴瓶頸那一步時要用**。STT 變慢時直接多起一個只跑 stt 的 process，它會跟合併 process 裡的 stt 迴圈一起搶同一條隊伍（`SKIP LOCKED` 自動分工），check / notified 完全不受影響。
2. `orchestrator/smoke_test.py` 依賴單 step 啟動來注入假 handler。

**三個迴圈不會互相 block** 它們是並行的 asyncio task，stt 在 `await` HTTP 期間 check 照跑。handler 都是 I/O bound（HTTP 打 agent server），不會卡住 event loop。pool 4 條也夠——每個迴圈同時最多借 1 條。

**風險** blast radius：一個 process 掛掉三個 step 同時停。對目前這條線性 pipeline 影響有限（少任何一步都會停擺），但平台上有多個 workflow 之後要重新評估。`Procfile.workers` 保留兩種寫法，切換成本接近零。

**驗收** 合併 process 單獨啟動時連線數應為 4（3 LISTEN + 1 pool）。`full_chain_happy_path` 全綠。額外測一個 case：在合併 process 之外再起一個 `--step stt`，確認兩者會分工而不是重複處理同一則命令。

**實測結果與勘誤**（2026-08-03）：

- ✅ `--role worker --step all` 已實作，`orchestrator/smoke_test.py` 全 8 個情境全綠。
- ✅ 分工 case 通過：合併 process 之外另起一個獨立 `--step stt`，觸發一次真實 run 後查 `event_dispatch`，`stt.run` 只被合併 process 內的迴圈認領一次（`attempts=1`），獨立 worker 完全沒碰到——`SKIP LOCKED` 分工如預期。
- ❌ **連線數沒有降到 4，穩定停在 6**（3 LISTEN + 3 pool），且不是暫態——啟動後量測、等滿一個 `max_idle`（10 分鐘）視窗後再量，數字不變。

  **根因**：三個 step 迴圈的 `_claim_one` 輪詢彼此是獨立的 asyncio task，用取樣 `pg_stat_activity.query_start` 追蹤發現三者的輪詢時間點確實是錯開的（不是同一個 tick 觸發），但只要任兩個迴圈的查詢窗口有一絲重疊，pool 當下就需要 2～3 條連線同時借出。`psycopg_pool` 的收縮規則是「整個 `max_idle` 視窗內都至少有 1 條閒置」才會縮 1 條；以 3 個迴圈、`poll_interval=2s` 的頻率，10 分鐘視窗內幾乎必然撞見至少一次重疊，導致收縮條件永遠不成立。這不是暖機噪音（階段 1／2 那種，等一下就會自己降下來），是這個輪詢頻率下的穩定態。

  **影響**：階段 3 在「process 數量」上如期把 4 個 process 收斂成 2 個，但在「連線數」這個計劃真正要收斂的指標上，worker 小計沒有比階段 1／2 更省（仍是 6，不是預期的 4）。上面「目標」表格與下面「驗收總表」已經照實測數字更新。
  - 若之後真的要把 worker 小計壓到 4，可行方向是在 `event_bus/postgres.py` 內用一把 process-scoped `asyncio.Lock` 包住 `pool.connection()` 的借用，強制同一個 process 內的多個 step 迴圈序列化存取共用 pool（單次查詢是毫秒級，序列化的延遲成本可忽略）。這次先不做，留給要用到時再評估——目前 6 條相對現況 25 條已經是很大的改善，值得先驗證階段 4 再回頭看要不要投這個成本。
  - 階段 4「共用 listener」的驗收目標（worker 小計 2）目前**還沒實測**，這個 pool 收縮問題會不會在 listener 共用上重演，要留到階段 4 驗收時才知道。

---

## 階段 4 · 共用 listener 連線

這是唯一需要動 `event_bus/postgres.py` 核心邏輯的階段，也是收益最大的一階（4 → 2，且脫離 step 數）。

**動機** LISTEN 連線的全部工作只有兩行——

```python
async def _listen_loop(self, conn, wake):
    async for _ in conn.notifies():
        wake.set()
```

它不排隊、不處理、不持有任何狀態，收到通知就舉個旗子。而 Postgres 允許**一條連線同時 LISTEN 多個 channel**，psycopg 的 `Notify` 物件也帶著 `.channel` 欄位（`psycopg/_connection_base.py:63`）可以分辨來源。所以「一個訂閱一條 LISTEN 連線」是現在的實作寫法，不是機制上的必然。

真正需要各自獨立的是**處理迴圈**（決定誰擋誰），而那只是個 Python 迴圈，不用連線。

**設計** 在 `PostgresEventBus` 內加一個 lazily-created 的 `_SharedListener`：

```python
class _SharedListener:
    """一條連線服務這個 bus 的所有訂閱。收到通知後依 Notify.channel
    只叫醒該 channel 的等待者。"""
    _conn: psycopg.AsyncConnection            # autocommit
    _waiters: dict[str, set[asyncio.Event]]   # channel -> 等待此 channel 的 wake events
    _task: asyncio.Task | None                # 跑 notifies() 的迴圈

    async def register(self, channel: str, wake: asyncio.Event) -> None: ...
    async def unregister(self, channel: str, wake: asyncio.Event) -> None: ...
```

`subscribe()` 改成向 listener 註冊自己的 `wake`，離開時註銷；channel 的 `LISTEN` / `UNLISTEN` 以參照計數決定（同一個 channel 被多個訂閱者共用時只 LISTEN 一次）。

**關鍵限制與對策** 前置驗證 ③ 說明 `notifies()` 執行期間無法對同一條連線下 `LISTEN`。所以 `register()` 不能直接執行 `LISTEN`，必須：

```
1. 取消 notifies() 的 task（generator 結束，釋放連線鎖）
2. 執行 LISTEN <new channel>
3. 重新啟動 notifies() task
```

**為什麼這是安全的**：重啟過程中可能漏掉幾毫秒內的通知，而漏掉 NOTIFY 的後果就是「該訊息延到下一次輪詢才被撿到」——這正是 `event_bus/postgres.py` module docstring 開宗明義的設計前提（「輪詢是正確性的保底，LISTEN/NOTIFY 是疊在上面的延遲最佳化」）。不是新增風險，是既有保底機制的正常運作路徑。

實務上訂閱都在 process 啟動時建立、之後永不變動，所以這個重啟一輩子只發生 3 次。

**風險** 中——這是唯一改到核心投遞路徑的一階。需要留意：

- 重啟 task 時的競態（用一把 `asyncio.Lock` 保護 register/unregister）
- listener 連線斷線後的重連（現在是每個訂閱各自一條，斷一條只影響一個訂閱；共用之後斷線影響全部訂閱。要加重連，重連後重新 LISTEN 全部 channel）
- 參照計數錯誤會導致 channel 被提前 UNLISTEN（症狀是該訂閱退化成純輪詢，不會出錯但會慢）

**驗收**
- 合併 process 單獨啟動時連線數應為 2，且 `pg_stat_activity` 只有一列 `LISTEN`
- 新增一個 smoke test：驗證三個 channel 的通知都能正確叫醒對應的迴圈、且不會誤叫醒別人（發布 stt 命令時 check 迴圈不應被喚醒——可用 claim 次數觀察）
- 既有的 `event_bus/smoke_test.py` 的 latency case（測 NOTIFY 有沒有生效）必須維持通過
- 額外測：訂閱建立後再新增一個訂閱，確認先前的訂閱沒有掉通知

**實測結果**（2026-08-03）：

- ✅ `_SharedListener` 已實作在 `event_bus/postgres.py`：一個 bus 的所有訂閱共用一條 autocommit 連線，`register()`/`unregister()` 用參照計數決定 channel 集合，每次異動走「取消 notifies() task → `UNLISTEN *` → 重新 `LISTEN` 目前全部 channel → 重啟 task」，並在 `_run()` 加了斷線重連（重連後重新 LISTEN 全部 channel）。
- ✅ `event_bus/smoke_test.py` 全部情境（含既有的 `notify_latency`）全綠，新增的兩個情境都通過：
  - `multi_channel_isolation`：3 個 topic 各自訂閱，輪流發布，每次都只有對應 channel 的訂閱者被喚醒，另外兩個仍在等待（未被誤喚醒），且延遲都在 `poll_interval/2` 內。
  - `late_subscribe_does_not_drop_earlier`：先訂閱一個 topic，再訂閱第二個（觸發一次 `_relisten()` 重建），確認第一個訂閱之後仍然正常收到 NOTIFY，沒有因為重建而掉訂閱。
- ✅ 完整 stack 下 `orchestrator/smoke_test.py` 全 8 情境全綠，無 regression。
- ✅ **合併 process 單獨啟動時 `pg_stat_activity` 確實只有一列 `LISTEN`**（3 條 LISTEN 連線成功收斂成 1 條，這階段真正的設計目標達成）。
- ⚠️ 但**連線總數是 4，不是預期的 2**：實測為 1 LISTEN + 3 pool。pool 沒降到 1，是階段 3「實測結果與勘誤」記錄的同一個根因——三個 step 迴圈共用 pool 時偶爾同時借用，讓 `psycopg_pool` 的收縮條件永遠等不到一個完全無重疊的視窗，這個階段完全沒改到 `_claim_one`/pool 邏輯，問題原封不動延續下來。
  - master 單獨啟動維持階段 2 的 3 條，不受影響（master 本來就只有一個訂閱，`_SharedListener` 對它來說退化成原本「一個訂閱一條連線」的行為，沒有變化）。
  - 目標表格與驗收總表已依實測更新：階段 4 worker 小計是 4（1 LISTEN + 3 pool），不是 2；總計 7，不是 5。

---

## 不在這次範圍

已記進 [TODO.md](../TODO.md)，供之後排優先級：

- **查詢量最佳化** — `_FETCH_EVENT` 併進 `_CLAIM`、扇出搬到 publish 時、`poll_interval` 60→300。這些省的是 SQL 條數與 `_FAN_OUT` 的成長問題，跟連線是正交的兩件事，另開計劃。見 TODO 的[「查詢量最佳化尚未做」](../TODO.md#query-volume-optimization)。
- **checkpointer 共用 pool** — 見階段 2 的說明，需先實測 `autocommit` 對交易語意的影響。見 TODO 的[「checkpointer 還沒併進共用 pool」](../TODO.md#checkpointer-shared-pool)。
- **並行消費（一個訂閱同時處理 N 則）** — 可行且底層安全（`SKIP LOCKED` 就是為此設計），但需要處理租約在排隊時過期的問題（`lease_seconds=30`，搶到就開始倒數），以及並行上限與 task 例外處理。獨立議題。見 TODO 的[「一個訂閱同時處理 N 則命令」](../TODO.md#parallel-consumption)。
- **換 broker** — `EventBus` 是 Protocol、`factory.py` 已預留 Kafka 分支。真的撞到 Postgres 的吞吐上限才做。已在 TODO 的[「Kafka backend 還沒做」](../TODO.md#kafka-backend)。

---

## 驗收總表

每階段結束後，啟動 `Procfile.workers`（階段 3 之後改用合併模式）量測：

```sql
SELECT count(*) FROM pg_stat_activity
WHERE datname = 'agent_architecture' AND application_name = '';
```

| 階段 | 預期連線數 | 必須通過 |
|---|---|---|
| 1 | 10 | `orchestrator/smoke_test.py` 全部 |
| 2 | 9 | 同上 |
| 3 | ~~7~~ → **9**（實測，見階段 3「實測結果與勘誤」） | 同上 ＋ 「合併 process 與獨立 stt worker 分工」新 case ✅ |
| 4 | ~~5~~ → **7**（實測：master 3 + worker 4，見階段 4「實測結果」） | 同上 ＋ `event_bus/smoke_test.py` latency case ✅ ＋ 多 channel 喚醒隔離新 case ✅ |

跑 smoke test 前務必先關掉 `Procfile.workers`——它的 consumer_group 跟測試同名，會搶走測試刻意用假 handler 處理的命令（`Procfile.workers` 開頭的註解有說明）。

---

## 補充：`memory-writer` 加入後的實測（2026-08-04）

[docs/long-term-memory-plan.md](long-term-memory-plan.md) M3 落地後，`Procfile.workers` 多了第五個常駐 process（`memory-writer`，見該檔案）——這份計劃寫的時候它還不存在，上面的驗收總表沒有算到它。用同一條驗收 SQL 重新量了三種組合（Postgres 本機、只起 `Procfile.workers`，不含 `Procfile` 那批 agent service，因為 `memory-writer` 開機不需要打真的 agent），每個組合都量了「剛啟動」跟「等 60 秒後」兩個時間點，60 秒後數字沒有再往下掉：

| 組合 | process 數 | 連線數（穩定值） |
|---|---|---|
| 拆開模式（`worker-stt`/`worker-check`/`worker-notified`/`master`），不含 `memory-writer` | 4 | **13** |
| 拆開模式 + `memory-writer` | 5 | **17** |
| `worker-all` 合併模式 + `memory-writer` | 3 | **12** |

**`memory-writer` 自己的成本**：拆開模式下加了它，連線數 13 → 17，多 4 條——1 條 LISTEN（跟 `master` 的 events topic 剛好同名，但各自獨立連線，見前面「1&2」的說明）＋ process 內 `event_bus`/`run_state` 共用的那個 pool（`persistence/pool.py::get_shared_pool()`，`min_size=1`）＋長期記憶 store 自己的連線（`persistence/memory_store.py` 的 `AsyncPostgresStore`，跟 `checkpointer` 一樣是刻意不併進共用 pool 的獨立連線，見階段 2「checkpointer 不併進來」的同一個理由）。這個 footprint 跟 `master` 自己（1 LISTEN + 共用 pool + `checkpointer` 各自一條)是同一種形狀，不是新問題，是這個 process 本來就該有的最小配置。

**worker-all 合併模式確實比較省，但沒有精準落在文件原本推導的數字上**：`worker-all + memory-writer`（12）比「拆開模式 + memory-writer」（17）省了 5 條，方向與階段 3/4 的結論一致；但也沒有省到「用階段 4 的 7 加上 memory-writer 自己的 4」等於 11 這麼低，量出來是 12，只差 1，在同一個數量級，可能是量測時機或 pool 收縮視窗（10 分鐘)還沒完全跑完的緣故，不是新的異常。

**發現一個跟 `memory-writer`無關、但值得記下來的落差**：拆開模式（不含 `memory-writer`）量到 13 條，比這份文件階段 2 表格寫的理論值「9」高出 4 條。回頭看階段 2 的「驗收」只單獨量了 `master` 自己的連線數（3 條，符合)，**沒有對整組 `Procfile.workers` 做過端到端量測**——階段 3 之後量測方式全部改成「合併模式」，拆開模式的總數从此再也沒被真的量過，「9」這個數字其實只是理論推導、不是實測值。13 這個數字才是拆開模式第一次被真的量出來的總數，跟哪個 stage 都對不上，這是今天（2026-08-04）順著量 `memory-writer` 才發現的，還沒查根因，先如實記在這裡，要不要深入查再由你決定。
