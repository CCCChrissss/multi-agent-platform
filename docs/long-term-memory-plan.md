# 長期記憶（Long-Term Memory）導入計畫

> [!NOTE]
> 這是長期記憶的歷史設計與落地紀錄，包含原作者 macOS 環境的驗證內容。PostgreSQL / pgvector / `local-embed` 的目前 Windows 實機狀態見 [current-windows-status.md](current-windows-status.md)。

## 0. 這份文件在回答什麼

1. LangChain/LangGraph 講的「長期記憶」具體會存哪些東西、用什麼資料結構存。
2. 它跟我們現在 [persistence/checkpointer.py](../persistence/checkpointer.py) 存的東西差在哪——這兩件事很容易混淆，因為兩者都「寫進 Postgres、都跨 process 存活」。
3. 要導入到這個平台，具體要動哪些檔案、分幾個階段。

**先講結論**：長期記憶（`BaseStore`）跟 checkpointer 不是同一層東西，也不能互相取代。checkpointer 回答「**這一次執行跑到哪、狀態長什麼樣**」，長期記憶回答「**跨越所有執行，這個平台學到了什麼**」。而且——這是本專案特有的重點——**我們的事件驅動路徑根本沒有用 checkpointer**（`orchestrator_runs` 取代了它），所以長期記憶**不能**照官方教學那樣掛在 `graph.compile(store=...)` 上，必須做成一個跟 `persistence/checkpointer.py` 平行的獨立元件，讓同步路徑、事件驅動路徑、以及 [agents/runtime.py](../agents/runtime.py) 的 HTTP agent runtime 都能用同一份記憶。

---

## 1. LangChain/LangGraph 的長期記憶是什麼

### 1.1 資料模型：`BaseStore`

長期記憶在 LangGraph 裡就是一個叫 `BaseStore` 的抽象介面（`langgraph.store.base.BaseStore`），本質是**帶命名空間的 JSON document store**：

| 概念 | 型別 | 說明 |
|---|---|---|
| `namespace` | `tuple[str, ...]` | 階層式命名空間，像資料夾路徑。官方慣例是把 user id / org id 之類的隔離維度放進去，例如 `("acme", "memories")` |
| `key` | `str` | 該 namespace 內的唯一識別碼，通常是 uuid 或有意義的自然鍵 |
| `value` | `dict`（JSON） | 任意結構的 JSON document，**schema 完全由應用自己定義**，LangGraph 不管內容 |

讀出來是一個 `Item`（`namespace` / `key` / `value` / `created_at` / `updated_at`）；走語意檢索時是 `SearchItem`，多一個相似度 `score`。

### 1.2 API（本專案會用的 async 版）

```python
await store.aput(namespace, key, value, index=None, ttl=None)      # 寫入 / 覆寫
await store.aget(namespace, key)                                    # 精確取一筆 -> Item | None
await store.asearch(namespace_prefix, *, query=None, filter=None, limit=10, offset=0)
                                                                    # query -> 向量相似度；filter -> value 欄位等值比對
await store.adelete(namespace, key)
await store.alist_namespaces(prefix=..., max_depth=...)             # 列出有哪些 namespace
```

`asearch` 的第一個參數是 **namespace prefix**，不是完整 namespace——所以 `("acme", "episodic")` 會撈到 `("acme", "episodic", "stt_check_notify", "check")` 底下的東西。這個特性決定了 namespace 的層級順序要怎麼設計（見 §3.2）。

### 1.3 落到 Postgres 實際長什麼樣

我們已經裝了 `langgraph-checkpoint-postgres 3.1.0`（`pyproject.toml` 裡本來就有，為了 checkpointer），`AsyncPostgresStore` 就在同一個套件裡，**不需要新增依賴**。它 `setup()` 時建的表（節錄自 `langgraph/store/postgres/base.py` 的 `MIGRATIONS`）：

```sql
CREATE TABLE IF NOT EXISTS store (
    prefix text NOT NULL,          -- namespace tuple 用 "." join 後的字串
    key text NOT NULL,
    value jsonb NOT NULL,          -- 記憶內容本身
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP WITH TIME ZONE,   -- TTL 用
    ttl_minutes INT,
    PRIMARY KEY (prefix, key)
);
CREATE INDEX store_prefix_idx ON store USING btree (prefix text_pattern_ops);
CREATE INDEX idx_store_expires_at ON store (expires_at) WHERE expires_at IS NOT NULL;
```

**只有**在開啟語意檢索（`index=` 設定）時，才會額外跑 `VECTOR_MIGRATIONS` 建：

```sql
CREATE EXTENSION vector;   -- pgvector

CREATE TABLE IF NOT EXISTS store_vectors (
    prefix text NOT NULL,
    key text NOT NULL,
    field_name text NOT NULL,      -- value 裡被拿去做 embedding 的那個欄位
    embedding vector(<dims>),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (prefix, key, field_name),
    FOREIGN KEY (prefix, key) REFERENCES store(prefix, key) ON DELETE CASCADE
);
CREATE INDEX store_vectors_embedding_idx ON store_vectors USING hnsw (embedding vector_cosine_ops);
```

> ⚠️ **本機實測（2026-07-30 更新）**：本機實際在跑的是 `postgresql@14`（`brew services list` 確認），而 `brew install pgvector` 預設只 build 給當時系統偏好的 postgres 版本（build 出來的是 `postgresql@17`/`postgresql@18` 用的 extension），`postgresql@14` 的 extension 目錄裡原本沒有 `vector.control`，所以第一次查 `SELECT * FROM pg_available_extensions WHERE name='vector'` 是 0 rows。已改用 pgvector 原始碼（tag `v0.8.6`）搭配 `make PG_CONFIG=/opt/homebrew/opt/postgresql@14/bin/pg_config install` 針對 `postgresql@14` 重新編譯安裝，並在 `agent_architecture` 資料庫執行 `CREATE EXTENSION vector;`——現在 `pg_extension` 已經有 `vector 0.8.6`。M4 的環境前置條件已滿足，不用再等。

另外 `AsyncPostgresStore` 支援 TTL（`ttl_minutes` / `expires_at`），但**清除是靠背景 sweeper thread**（`start_ttl_sweeper()`），不呼叫就只是欄位有值、資料不會真的消失。

### 1.4 官方把長期記憶分成三類

這是「存哪些東西」最實際的答案：

| 類型 | 存什麼 | 官方描述 |
|---|---|---|
| **語意記憶（Semantic）** | 事實、知識、實體屬性 | 兩種做法：**profile**（一份持續覆寫的 JSON 大文件，好查但改起來容易出錯）vs **collection**（很多份窄範圍小文件，下游 recall 較高、但更新/刪除要另外管）。官方偏好 collection |
| **情節記憶（Episodic）** | 過去發生過的具體事件、「上次這種輸入我怎麼做才對」 | 實務上就是 **few-shot examples**——把過去成功/被人工修正過的案例挑幾筆塞進 prompt |
| **程序記憶（Procedural）** | 該怎麼做事的規則、指令本身 | 靠 reflection / meta-prompting，讓 agent 根據回饋**改寫自己的 system prompt / 規則清單** |

以及寫入時機兩種：
- **Hot path（執行中即時寫）**：即時可用、使用者看得到，但增加延遲、agent 要一心二用。
- **Background（非同步寫）**：不影響延遲、關注點分離，但要自己決定「什麼時候觸發蒸餾」。

---

## 2. 跟 checkpointer 的差別

### 2.1 我們的 checkpointer 現在實際存了什麼

`AsyncPostgresSaver.setup()` 建了 4 張表（實際 DDL 在 `langgraph/checkpoint/postgres/base.py`），本機 `agent_architecture` 資料庫裡都在：

```sql
checkpoints       (thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id,
                   type, checkpoint JSONB, metadata JSONB,
                   PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id))
checkpoint_blobs  (thread_id, checkpoint_ns, channel, version, type, blob BYTEA, ...)
checkpoint_writes (thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type, blob, ...)
checkpoint_migrations (v)
```

放進去的內容，就是 [workflows/simple_pipeline.py](../workflows/simple_pipeline.py) 那個 `PipelineState` 的每個欄位在**每一個 super-step 之後**的完整快照：`audio_ref` / `transcript` / `mentions_tsmc` / `status` / `needs_review` / `review_reason`。`persistence/history.py` 印的 `ckpt.checkpoint["channel_values"]` 就是這個。

它的三個關鍵性質：
- **LangGraph 自動寫**，node 完全不用知道它存在。
- **主鍵是 `thread_id`**，也就是綁死在單一次執行上。
- **用途是重放這一次執行**：崩潰接續（`simple_pipeline.py` 的 `aget_state()` + `ainvoke(None, config)`）、time travel、human-in-the-loop 中斷續跑。

### 2.2 對照表

| | **Checkpointer**（現有） | **Store / 長期記憶**（要導入） |
|---|---|---|
| 存什麼 | graph state 的完整快照（`channel_values`）+ pending writes | 應用自己定義 schema 的 JSON 記憶文件 |
| 誰決定內容 | **框架**（就是你的 State TypedDict） | **應用**（你想記什麼就記什麼） |
| 誰觸發寫入 | 框架自動，每個 super-step 後 | **應用主動呼叫 `aput()`**，不寫就沒有 |
| 索引鍵 | `(thread_id, checkpoint_ns, checkpoint_id)` | `(namespace_prefix, key)`，可加向量相似度 |
| 檢索方式 | 只能「拿某個 thread 的第 N 個快照」 | prefix 掃描、value 欄位 filter、語意相似度 top-k |
| 作用範圍 | **單一 thread（單次執行）** | **跨 thread、跨執行、跨 workflow** |
| 生命週期 | 跟著 run 走，run 結束就是歷史紀錄 | 獨立於 run，可以有 TTL、可以被覆寫/遺忘 |
| 用途 | 崩潰接續、稽核回放、HITL | 個人化、少樣本學習、規則累積、跨 run 知識 |

**最容易搞混的一點**：checkpoint 資料其實也永久留在 DB 裡，那為什麼不能直接拿它當長期記憶？因為**索引方式與語意不對**。checkpoint 是「以 run 為單位的原始逐格錄影」，你沒辦法問它「過去所有 run 裡，跟這段逐字稿類似的案例，人工最後判成什麼？」——那需要跨 thread 的語意檢索，而 `checkpoints` 表的主鍵和索引（`checkpoints_thread_id_idx`）根本不是為這種查詢設計的。而且原始快照沒有經過蒸餾，直接塞進 prompt 只是雜訊。**長期記憶 = 從軌跡蒸餾出來、可跨 run 檢索的結論**，這件事 checkpointer 天生不做。

### 2.3 本專案特有的重點：事件驅動路徑沒有 checkpointer

現在整個平台的持久化資料盤點（`\dt` 實測 8 張表）：

| 表 | 誰寫 | 範圍 | 存什麼 | 哪條路徑在用 |
|---|---|---|---|---|
| `checkpoints` / `checkpoint_blobs` | LangGraph 自動（同步路徑）+ [persistence/event_checkpoints.py](../persistence/event_checkpoints.py) 補寫（事件驅動路徑） | thread | graph state 快照 | **兩條路徑都用**——事件驅動路徑那份是 `orchestrator/master_agent.py` 每次 `run_state` 轉換成功後的事後稽核鏡射，不是真正可續跑的 LangGraph 狀態，細節見下方說明 |
| `checkpoint_writes` | LangGraph 自動 | thread | 一個 super-step 內、下個節點還沒讀到的中繼寫入 | **只有**同步路徑——事件驅動路徑的等價機制是 `event_bus` 的 lease/redelivery（[event_bus/postgres.py](../event_bus/postgres.py)），這張表對事件驅動的 run 永遠是空的 |
| `orchestrator_runs` | [orchestrator/run_state.py](../orchestrator/run_state.py) | thread | `current_step` / `status` / 累積的 `state_payload` | **只有**事件驅動路徑，且是它唯一的執行控制真相來源 |
| `event_log` / `event_dispatch` | [event_bus/postgres.py](../event_bus/postgres.py) | thread（事件） | 命令/完成事件 + 每個 consumer_group 的派送狀態 | 事件驅動路徑 |
| `call_log` | [persistence/call_log.py](../persistence/call_log.py) | thread + node | 每一次 LLM/tool 呼叫的 request/response | **兩條路徑都用** |
| **`store` / `store_vectors`** | **（新增）應用主動寫** | **跨 thread** | **長期記憶** | **應該兩條路徑都能用** |

> **checkpoints 表的事件驅動寫入是事後鏡射，不是真正的 checkpointer 用法**：`orchestrator/master_agent.py` 在每個 `run_state.advance()`/`mark_terminal()` 贏得 compare-and-swap 之後，才呼叫 `record_step()` 把同一次轉換寫進 `checkpoints`/`checkpoint_blobs`（用 LangGraph checkpointer 的原始 `aput()`/`aget_tuple()` API，不經過 `compile(checkpointer=...)`，所以下面這句「沒有 compiled graph」依然成立）。這麼做純粹是為了讓 `persistence/history.py` 對兩條路徑都能讀出一致的稽核視圖，也讓長期記憶的 `source_thread_id` 反查鏈（call_log/event_log/checkpoints）在兩條路徑上對稱；`orchestrator_runs` 才是事件驅動路徑唯一的執行控制真相來源，寫入順序永遠是它先、checkpoint 鏡射後，鏡射失敗也不可以讓 run 失敗。已知限制：`checkpoint_writes` 對這些 run 恆空、`versions_seen` 恆為空 dict，這是刻意簡化，見 [TODO.md](../TODO.md)。

`orchestrator/run_state.py` 的 docstring 已經寫得很清楚：它刻意不是 LangGraph checkpointer。這代表——

> 官方文件教的 `builder.compile(store=store)` + node 裡 `runtime.store` 這條路，**在我們的事件驅動路徑上完全不存在**，因為那條路沒有 compiled graph。

所以長期記憶必須做成**跟傳輸/編排模式無關的獨立元件**（跟 `agents/envelope.py` 對 transport 脫鉤是同一種設計精神），由三種呼叫端各自取用：

- 同步路徑：LangGraph node 直接呼叫（也可以順便掛 `compile(store=...)`，但那只是方便，不是必要）。
- 事件驅動路徑：worker 的 handler 呼叫。
- **[agents/runtime.py](../agents/runtime.py) 的 HTTP agent runtime：這才是真正的主戰場**——記憶要在 agent 內部被讀寫，而不是在編排層，否則換掉編排模式記憶就跟著消失。這裡原先是三個獨立 server，後續已合併為單一 process 的三條 route。

---

## 3. 導入設計

### 3.1 定位：這是平台能力，不是場景邏輯

對照 [AGENTS.md](../AGENTS.md) 與 [docs/harness-engineering-principles.md](harness-engineering-principles.md) 檢查清單第 9 條：

- **平台層**（放 `persistence/`、`gateway/`）：store backend、namespace 規約、讀寫 API、embedding 管道、存取治理、背景蒸餾器骨架。
- **場景層**（放 `llm/`、`mcp_servers/*/agent.py`、`workflows/definitions/*.yaml`）：要記什麼（「台積電的別名」「這個收件人偏好 Slack」）、記憶怎麼進 prompt、蒸餾的判準。

具體切法：新增 `persistence/memory_store.py`（backend，跟 `checkpointer.py` 完全平行，換後端只動這一個檔）+ `persistence/memory.py`（namespace 規約 + `recall()`/`remember()` 兩個平台級 API）。

### 3.2 namespace 規約

因為 `asearch` 用 prefix 比對，層級順序 = 未來能做哪些範圍查詢。提案：

```
(tenant_id, kind, *scope)
```

| kind | scope | 完整範例 | 意義 |
|---|---|---|---|
| `semantic` | `(subject_type, subject_id)` | `("default", "semantic", "recipient", "王小明")` | 關於某個實體的事實，租戶專屬 |
| `episodic` | `(workflow_name, step_name)` | `("default", "episodic", "stt_check_notify", "check")` | 這個 step 過去的案例 |
| `procedural` | `(workflow_name, step_name)` | `("default", "procedural", "stt_check_notify", "check")` | 這個 step 累積的判斷規則 |

`tenant_id` 放第一層是為了多租戶隔離（內部平台遲早要分部門/專案），`kind` 放第二層是因為讀取端幾乎總是「我要這一類記憶」而不是「我要這個 workflow 的全部記憶」。

**`tenant_id` 有一個保留值 `_global`**（`persistence/memory.py::GLOBAL_TENANT`），代表「不屬於任何特定租戶的知識」——例如公司別名（「台積電＝TSMC＝台灣積體電路製造」）是世界知識，不該每個租戶各存一份、也不該漲共用時退化成隨便哪個 tenant 都能讀。是否要共用，是**場景層的決定**：`recall()`/`remember()` 本身不會自動合併「我的租戶 + `_global`」的結果，呼叫端想要兩者都查就自己分兩次呼叫再合併；`policy.yaml` 也要為 `_global` 開明確的 pattern，不能靠把 tenant 那一格寫成萬用字元「順便」達成共用——那樣做會連真正該租戶隔離的資料（例如收件人的通知偏好）一起洩漏出去，這正是 §5 風險 3 的教訓（見下方 `policy.yaml` 範例的註解）。

**`value` 一律強制帶稽核欄位**（平台層在 `remember()` 裡自動補，呼叫端不用管）：

```json
{
  "content": { },                        // 場景自定的記憶內容
  "source_thread_id": "…",               // 這則記憶是從哪次 run 蒸餾出來的
  "source_step": "check",
  "created_by": "memory_writer",         // 或 "human_review"
  "confidence": 0.0
}
```

`source_thread_id` 讓任何一則記憶都能反查回 `call_log` / `event_log` / `checkpoints`——沒有這個欄位，記憶就是一團無法稽核的黑箱，出事時無法回答「這條規則哪來的」。

**`content` 本身不是每個場景各自愛怎麼定就怎麼定——procedural/episodic 是平台標準 schema，只有 semantic 才是場景自訂**（M2.2 才確立，見 `persistence/memory_prompt.py`）：

```json
// procedural：任何 agent 都一樣，一句該接進 system prompt 的規則文字
{"rule": "文中只是列舉半導體同業時不算提到"}

// episodic：任何 agent 都一樣，"模型當初看到什麼" -> "它當初該回什麼"
// output 是完整字串，序列化成什麼格式由寫入端（remember() 呼叫者）決定，
// 讀取端不需要知道那是哪個 agent 的輸出 schema
{"input": "這次法說會晶圆代工龙头交出漂亮成績單", "output": "{\"mentions_tsmc\": true}"}

// semantic：沒有共通形狀，不同 subject_type 的屬性完全不同，維持場景自訂
{"aliases": ["台積電", "TSMC", "台灣積體電路製造"]}   // company
{"note": "只用 email，不要用 Slack"}                    // recipient
```

會這樣切，是因為 procedural/episodic 存在的目的本來就是通用的（「一條規則」「一個範例」），跟哪個 agent 無關；semantic 描述的是任意實體的任意屬性，天生沒有共通欄位。這個判準也直接決定了讀取端能不能共用程式碼——`persistence/memory_prompt.py` 的 `inject_procedural()`/`recall_episodic_few_shot()` 只對 procedural/episodic 成立，semantic 的渲染邏輯目前仍留在各 agent 自己的程式碼裡（`llm/tsmc_judge.py` 的別名合併、`mcp_servers/notified/agent.py` 的收件人偏好）。

### 3.3 合約要補一個東西：subject identity

這是導入長期記憶**必然**會撞到的合約問題，先講清楚：

[docs/agent-api-contract.md](agent-api-contract.md) 現在的 request envelope 只有 `thread_id` + `input`。但長期記憶的定義就是**跨 thread**，所以 `thread_id` 對它毫無用處——agent 需要知道的是「這次是**替誰**做事」（哪個租戶、哪個使用者），才知道要讀寫哪個 namespace。

提案：`AgentRequest` 新增一個 optional 的 `context: dict` 欄位（預設 `{}`），承載 `tenant_id` / `user_id` 這類**跨 run 的身分維度**，跟 `input`（本次的業務資料）分開。理由跟合約原文把 `output` 巢狀化的理由一樣：業務欄位跟 envelope 的治理欄位不能混在一起。

`thread_id`（這一次）與 `context`（跨越很多次）的區分，剛好就是 checkpointer 與 store 的區分在 API 層的鏡像。

### 3.4 存取治理：比照 policy.yaml

> ⚠️ **這一節目前是探索階段的暫定設計,還沒定案**：使用者要跟主管討論過權限模型才能拍板，在那之前不要在這個基礎上繼續擴大範圍（例如把 `recall()`/`remember()` 接進真實 agent，或進一步調整 `policy.yaml` 的規則）——見 [TODO.md](../TODO.md) 的「記憶的存取治理還沒定案」。下面內容只是記錄目前探索到的機制跟遇到的坑（包括一個真的被抓到的跨租戶洩漏），不是定案的規範。

[mcp_servers/policy.yaml](../mcp_servers/policy.yaml) 已經建立了「principal → 能用哪些 tool」的 RBAC。記憶是比 tool 更敏感的東西（會跨租戶洩漏資料），不應該讓任何 agent 想讀就讀。提案在**同一份 `policy.yaml`** 加一個 `memory:` 區塊（單一治理面，未來 no-code UI 只需要編輯一個檔）：

```yaml
memory: # principal -> 可讀/可寫哪些 namespace pattern（`*` 為萬用字元）
  check:
    # episodic/procedural 的 scope 是 (workflow_name, step_name)，workflow
    # 那一格故意寫萬用字元：principal 是 agent/step 身分（current_node_name），
    # 同一個 step 名字被不同 workflow 重用時，語意上是同一種判斷任務，記憶
    # 應該跟著角色走，不是被綁死在第一個用到它的 workflow 上。
    #
    # tenant 那一格（第一個 /）不寫萬用字元——tenant 是隔離邊界，pattern
    # 要明確指名哪個租戶。`_global` 是唯一的例外：公司別名這類世界知識，
    # 明確開放給所有租戶共用。
    read:
      - "default/episodic/*/check"
      - "default/procedural/*/check"
      - "_global/semantic/company/*"
    write: []                       # agent 自己不在 hot path 寫記憶
  notified:
    read:  ["default/semantic/recipient/*"]   # 收件人偏好是租戶專屬個資，不共用
    write: []
  memory_writer:                    # 唯一有寫入權的 principal，本來就該跨租戶
    read:  ["*"]
    write: ["*/episodic/*", "*/procedural/*", "*/semantic/*"]
```

> **多 workflow 情境**：workflow 那一格的 wildcard 只在「同一個 step/agent 名字在不同 workflow 裡做的是同一種任務」時才合理（例如 `stt` agent 被多個 workflow 重用來做轉錄）。如果未來某個 workflow 用同樣的 step 名字做完全不相關的事，應該給它一個不同的 principal 名字，而不是依賴這個 wildcard——見 [mcp_servers/policy.yaml](../mcp_servers/policy.yaml) 的註解。
>
> **這裡曾經有一個真的漏洞，不是假設性風險**：早期版本把 tenant 那一格也寫成萬用字元（`*/semantic/company/*`），本意只是想讓 workflow 名字那格能萬用比對，但 `fnmatch` 的 `*` 連 `/` 都會吃掉，結果 `check`/`notified` 意外變成可以跨租戶讀任何租戶的資料——`notified` 甚至因此能讀到別租戶收件人的通知偏好。這是靠 `persistence/memory_smoke_test.py` 補了一個「資料真的存在、但被 policy 擋下」的情境（`scenario_tenant_isolation`）才抓到的，因為原本的測試只驗證了「查無資料回傳空清單」，沒有驗證「policy 真的擋下」，兩者表面結果一樣但意義完全不同。

principal 一樣取自 `current_node_name`（[persistence/call_log.py](../persistence/call_log.py) 的 contextvar），跟 MCP gateway 完全同一套身分來源，不新創概念。

### 3.5 embedding 走 LiteLLM Gateway，不要直接呼叫 provider

`IndexConfig.embed` 接受 `Embeddings` 物件、model 字串、或**一個 callable**（`EmbeddingsFunc = Callable[[Sequence[str]], list[list[float]]]`，async 版是 `AEmbeddingsFunc`）。我們要用 callable，把它接到 [gateway/client.py](../gateway/client.py)，理由就是 [AGENTS.md](../AGENTS.md) 說的「AI 基礎建設要素件化、可替換」：

- embedding 模型換 provider 時只改 `gateway/config.yaml`，不動記憶層。
- 每一次 embedding 呼叫自動進 `call_log`（成本/延遲可觀測），跟 chat/STT 一致。
- 未來 LLM Gateway 的存取控制（見 [TODO.md](../TODO.md)）可以一併涵蓋 embedding。

> 注意：`call_log` **不要記原始向量**（1024 維 × 每則記憶會把 `request`/`response` JSONB 撐爆），只記 `input` 筆數、字元數、dims。

### 3.6 三類記憶在這個示範場景分別對應什麼

平台能力要有場景來驗證，但實作時要清楚哪些是可拋棄的示範：

| 類型 | 這個場景的具體內容 | 對應的既有痛點 |
|---|---|---|
| **Semantic** | 公司別名（台積電 / TSMC / 台灣積體電路製造）、收件人的通知管道偏好 | 現在別名是 `mcp_servers/lookup` 裡的**靜態**資料；人工修正過的新別名沒地方沉澱回去 |
| **Episodic** | ~~過去被判 `needs_review` 而後人工裁決過的逐字稿片段 + 正確答案，當 few-shot 塞進 `llm/tsmc_judge.py` 的 messages~~ **已過期，見 §3.10**：episodic 不再塞進任何 agent 的 prompt，只作為 `scripts/distill_procedural.py` 的蒸餾原料 | [llm/tsmc_judge.py](../llm/tsmc_judge.py) 的 `_CONFLICT_PROMPT` 目前只能在**單次執行內**做二次確認；同一種誤判下一個 run 會原封不動重犯（這個痛點現在改由 procedural 蒸餾鏈接手，見 knowledge-distillation-plan.md） |
| **Procedural** | 累積的判斷規則清單（「文中只是列舉半導體同業時不算提到」），append 進 `_SYSTEM_PROMPT` | 規則現在硬編在 Python 常數字串裡，要改就要改 code + 重新部署 |

Episodic 這條正是 harness 原則第 7 篇「**生產軌跡 → 新判斷規則**」的閉環：`orchestrator_runs.status = 'needs_review'` 就是現成的「生產失敗追蹤」，只差把它蒸餾成記憶再送回 prompt。

### 3.7 寫入路徑：背景蒸餾，不放 hot path

採官方的 **background** 寫法，而不是讓 agent 在回答問題的同時決定要記什麼。理由：

1. `check`/`notified` 兩個 agent 已經在跑多輪 tool-calling loop 了（`MAX_TURNS = 20`、`StallGuard`），再讓它們一心二用去管記憶，只會讓 loop 更容易 stall。
2. 我們的事件驅動架構天生就適合：`{workflow}.events` topic 上每一個完成事件都是現成的觸發點，**新增一個 consumer_group 就好，不用改 master 或任何 worker**。

新增 `orchestrator/memory_writer.py`，以 consumer_group `{workflow_name}.memory_writer` 訂閱 `events_topic(workflow_name)`，收到完成事件後依 workflow 宣告的規則蒸餾成記憶寫入 store。

> ⚠️ **實作陷阱（一定要處理）**：[event_bus/postgres.py](../event_bus/postgres.py) 的 `_FAN_OUT` 是用 `NOT EXISTS` 逐列比對，**不是**用水位線。這代表 memory_writer 第一次啟動時，會把 `event_log` 裡該 topic 的**全部歷史事件**一次 fan-out 給自己重放一遍。第一次上線前必須先把既有事件對這個 consumer_group 補成 `done`：
> ```sql
> INSERT INTO event_dispatch (event_log_id, consumer_group, status, done_at)
> SELECT id, 'stt_check_notify.memory_writer', 'done', now()
> FROM event_log WHERE topic = 'stt_check_notify.events'
> ON CONFLICT (event_log_id, consumer_group) DO NOTHING;
> ```
> 這是 workaround；根本修法（讓 `EventBus.subscribe()` 支援 `start_from="now"`，由 event_bus 自己處理，不再靠人記住這段 SQL）已記進 [TODO.md](../TODO.md)。

### 3.8 讀取端的取捨：`recall()` 要寫死在場景程式碼裡，還是讓 agent 自己決定查什麼

M2（§4）目前的設計是把 `recall()` 硬寫在 `llm/tsmc_judge.py` 組 prompt 的那一步——查哪個 `kind`/`scope`、`limit` 多少，全部是呼叫端寫死的常數。這樣做的好處是可預測、不多耗一輪 model request，但代價是路徑寫死：agent 沒辦法在「猜不準需要更精準案例」時主動多查一次，也沒辦法查到當初寫程式的人沒預料到的 scope。

**建議方向**：把 `recall()` 額外包成一個 MCP tool、掛進 `MCPGateway`，讓 `check`/`notified` 這類本來就在跑 tool-calling loop 的 agent 自己決定何時查、查什麼 scope，而不是只能被動吃呼叫端塞進 prompt 的固定內容。這比較貼近 [harness-engineering-principles.md](harness-engineering-principles.md) 的核心主張——agent 需要的是回饋迴路，不是寫死的劇本——也符合該文件檢查清單第 9 條「這是平台通用能力還是場景邏輯」：`recall()` 本身是平台層 API，要不要開放給 agent 呼叫，只是 `MCPGateway` 要不要多註冊一個 tool 的路由選擇，不需要在 `persistence/memory.py` 裡加新邏輯。

安全性上不會因此變薄：`memory_policy.py` 是 fail-closed 的，agent 不管透過 prompt 注入還是自己選 namespace 呼叫 tool，`can_read()` 都會用同一份 `policy.yaml` 擋，agent 頂多是「查不到」而不會「查到不該查的」——開放成 tool 只是換了誰決定查詢參數，不是換了誰能通過權限檢查。

**主要取捨**：`check`/`notified` 已經在跑 `MAX_TURNS = 20`、帶 `StallGuard` 的 tool-calling 迴圈（見 §3.7 的第 1 點），多開一個 tool 就多一輪 model request、多一次卡住的風險，這正是 §3.7 把「寫入」排除在 hot path 外的理由；同一個風險同樣適用在「讀取」上，只是目前規劃選擇把讀取留在 hot path（M2 的固定注入），還沒開放給 agent 自主呼叫。

**折衷方案**：讀取端可以雙軌並存——保留一個自動注入的 baseline recall（M2 現有設計：組 prompt 前固定撈一次 procedural + 少量 episodic，確保 agent 不會「忘記查」），另外「加開」一個 `recall` MCP tool，讓 agent 在案例不夠明確、需要更精準比對時才主動多查一次。若採用這個折衷，工具的回傳值要照 harness 原則「RAG／搜尋類工具要帶 facets」設計：不能只回 top-k 結果，要附上「這個 scope 底下總共還有幾筆」「可以怎麼再縮小範圍（換 `kind`／加 `filter`）」這類 meta 資訊，避免 agent 拿到 5 筆結果就誤以為那是全部。

> ⚠️ 這一節目前只是設計討論，**不是** M2 已定案的實作範圍——是否要開放 `recall` 成 MCP tool，等 M2 的固定注入版本先跑通、且 §3.4 存取治理的權限模型定案之後再評估，避免在治理模型還沒拍板前就擴大 agent 能接觸的 namespace 面。

### 3.9 決定基準（暫定，episodic 已在 §3.10 被移出這個框架）：依「補漏 vs agent 已知道自己需要什麼」決定強制或 MCP 化，不是依 `kind`

M2 上線後接著討論出來的結論：**不是「procedural 永遠強制、semantic 永遠 MCP 化」，是按每則記憶存在的目的分**——

- **補漏型**（記憶存在是為了修正 agent 自己發現不了的盲點）→ **強制注入**，不管它是哪個 `kind`。`check` 的 procedural 規則是這一類；`check` 的 semantic 別名記憶形狀上是 semantic，但功能上一樣是補漏（模型可能沒認出某個別名，屬於§0 提到的「危險的、低估的方向」），所以**也**強制——現在 `_lookup_tsmc_aliases()` 把它併進確定性 alias backstop，就是這個判準的落地。
- **agent 已知道自己需要什麼**（不查不會出安全問題，只是少個人化資訊）→ **可以 MCP 化，讓 agent 自己決定要不要查**。`notified` 的收件人偏好是目前唯一符合這個形狀的案例：agent 本來就在明確決定「要不要發、往哪發」，漏查頂多退化成用預設管道。

**這個判準的兩邊證據強度不對等，要誠實面對**：「semantic 有時候必須強制」有實測支撐——塞別名記憶、拿掉別名記憶，`check` 的判斷真的跟著反轉（§4 M2 的驗證記錄）。「procedural 必須強制、不能讓 agent 自己決定要不要查」目前**只是推論**，論據是 LLM（尤其 `local-qwen` 這種小模型）對自己「有沒有把握」的校準能力普遍不好,不是這個平台實測出來的結果。

**對照現有實作**:`notified` 的收件人偏好記憶目前是強制注入(`mcp_servers/notified/agent.py::_recall_system_prompt()` 無條件呼叫),按這個判準應該遷移成 MCP tool——這就是下面 M4.5 要做的事。`check` 的兩類記憶(procedural/semantic 別名)都已經符合"補漏型強制"，不用動。episodic 原本也放在「補漏型強制」這一格，後來被拿掉了——見 §3.10。

### 3.10 episodic 不再是「agent 可見的記憶」（docs/knowledge-distillation-plan.md P5）

§3.9 原本把 episodic 跟 procedural 一起歸類成「補漏型，強制注入」——這個判準**已經被推翻**，不是先前段落標的「還沒驗證的推論」慢慢補上實測，是直接改變設計:episodic few-shot 不再進任何 agent 的 prompt，`persistence/memory_prompt.py` 已經沒有 `recall_episodic_few_shot()` 這個函式。

推翻的理由不是「episodic 補漏效果不好」，是兩個更根本的問題：

1. **示範比規則更容易教壞模型**：`docs/knowledge-distillation-plan.md` P1 那次調查已經實測到——把過去案例塞成假的 `[user, assistant]` 對話輪次，`llm/exclusion_judge.py` 這種「答案要先查證才能下」的 loop 會被教會跳過查證（模型會模仿"直接吐答案"這個示範動作，比文字規則的約束力更強）。改成單則 reference-text 訊息之後問題消失，但這只解決了「格式崩潰」，沒解決下一條。
2. **episodic 內容從來沒被驗證過是對的**：`orchestrator/memory_writer.py` 只要 workflow 判斷成功完成（`status == "ok"`）就寫一筆episodic，這代表一個**錯誤但沒觸發 needs_review 的判斷**，會被原封不動存成「正確答案」，下一次遇到類似輸入時直接當範例教給模型——不用等蒸餾、不用等人審，錯就直接複製給下一次。

現在的設計：episodic 繼續照常寫入（`_apply_rule()` 不變），但 `status="pending"`（跟procedural的M5關卡共用同一個機制，見 knowledge-distillation-plan.md §5 P0）。`recall()`/`browse()` 只讀 `status="active"`，所以：

- 沒有任何 agent 的 prompt 會看到未經審核的 episodic。
- `scripts/distill_procedural.py` 讀 episodic 用的也是 `recall()`——蒸餾器一樣只讀得到已經審核過的 episodic，不會被未審核的錯誤案例污染。
- `scripts/review_episodic.py`（P5）是唯一能把 episodic 從 pending 轉成 active 的路徑：approve/edit 才轉 active，reject/skip 都維持 pending（不刪除——跟 `scripts/review_memory.py` 的 procedural reject 行為不同，見該腳本docstring）。

episodic 現在唯一的用途是「蒸餾器的原料」，不是「agent 的記憶」——這也代表 §3.2 的 namespace 規約、§3.6 的三類記憶對照表裡「episodic 直接影響 agent 判斷」的描述已經過期，實際影響 agent 判斷的只剩 procedural（經蒸餾+人審後）跟 semantic。

---

## 4. 分階段實作計畫（含檔案級異動）

### M1 — 記憶基礎設施（不含向量、不含任何場景邏輯）

| 檔案 | 異動 | 內容 |
|---|---|---|
| `persistence/memory_store.py` | **新增** | 完全比照 [persistence/checkpointer.py](../persistence/checkpointer.py) 的形狀：`get_memory_store()` 回傳 `AsyncPostgresStore.from_conn_string(os.environ["PERSISTENCE_DATABASE_URL"])` 的 async context manager，加一個 `ensure_schema()` 呼叫 `store.setup()`。**換後端只改這個檔**。共用同一個 `PERSISTENCE_DATABASE_URL` |
| `persistence/memory.py` | **新增** | 平台級 API：`MemoryKind` enum（`semantic`/`episodic`/`procedural`）、`build_namespace(kind, tenant, scope)`、`async recall(store, kind, *, tenant, scope, query=None, filter=None, limit=5)`、`async remember(store, kind, *, tenant, scope, key, content, ttl_minutes=None)`。`remember()` 負責自動補 §3.2 的稽核欄位（`source_thread_id` 從 `current_thread_id` contextvar 讀、`source_step` 從 `current_node_name` 讀——跟 `call_log` 用同一組 contextvar，呼叫端不用傳） |
| `persistence/memory_policy.py` | **新增** | 讀 `policy.yaml` 的 `memory:` 區塊，`can_read(principal, namespace)` / `can_write(...)`。純函式、無 I/O（除了載入），比照 [mcp_servers/policy.py](../mcp_servers/policy.py) 的形狀，可單元測試。`recall`/`remember` 內部強制檢查，fail closed |
| [mcp_servers/policy.yaml](../mcp_servers/policy.yaml) | 修改 | 加 §3.4 的 `memory:` 區塊 |
| `persistence/memory_smoke_test.py` | **新增** | 比照 [orchestrator/smoke_test.py](../orchestrator/smoke_test.py)：put/get/search/delete 往返、prefix 檢索範圍正確、跨 tenant 隔離、policy 擋下越權讀寫 |
| [README.md](../README.md) | 修改 | 「狀態外部化」段落加一小節說明 `store` 表跟 checkpointer 的差別（就是本文 §2.2 的濃縮版），避免下一個人重蹈混淆 |

**M1 完成的定義**：`store` 表建起來、能存能取、policy 能擋，**但沒有任何 agent 用它**。這一步刻意零場景耦合。

### M2 — 讀取端：把記憶接進 check agent（hot path 只讀不寫）

| 檔案 | 異動 |
|---|---|
| [agents/envelope.py](../agents/envelope.py) | `AgentRequest` 新增 `context: dict = {}`（§3.3）；`run_handler()` 把它連同 `node_name` 一起傳給 handler |
| [docs/agent-api-contract.md](agent-api-contract.md) | 修改：request envelope 加 `context` 欄位定義，補一段「為什麼 `thread_id` 不足以支撐長期記憶」 |
| [agents/lifespan.py](../agents/lifespan.py) | runtime lifespan 除了 `MCPGateway` 之外，再建一個長期存活的 store（跟 gateway 同理，不可 per-request 建立），掛在 `app.state.store`；原先獨立的 `agents/check/server.py` 已合併 |
| [agents/check/client.py](../agents/check/client.py)、`agents/{stt,notified}/client.py` | 帶上 `context`（先由呼叫端傳入，預設 `{"tenant_id": "default"}`） |
| [llm/tsmc_judge.py](../llm/tsmc_judge.py) | 新增 optional `store` 參數（`None` 時行為完全不變，同步路徑不受影響）。在組 `messages` 前 `recall()`：procedural 記憶 append 到 `_SYSTEM_PROMPT` 後面、episodic 記憶轉成 few-shot 的 user/assistant 訊息對插在 `text` 之前;**別漏了 semantic**——公司別名（§3.6）也是這個 step 的記憶,`_lookup_tsmc_aliases()` 要把 `recall(MemoryKind.SEMANTIC, tenant=GLOBAL_TENANT, scope=("company","tsmc"))` 的結果併進 `mcp_servers/lookup` 那份靜態別名清單,不然 `remember()` 進去的別名修正永遠進不了那個決定性 backstop |
| [agents/runtime.py](../agents/runtime.py)、[mcp_servers/notified/agent.py](../mcp_servers/notified/agent.py) | 同樣模式，`recall` semantic 記憶（收件人偏好管道）注入 prompt |

**M2 完成的定義**：手動 `remember()` 塞一則規則進去，`check` agent 的行為確實改變，且 `parity_check.py` 仍通過。

### M2.1 — 記憶讀取端補完：共用 helper + `stt` 接上基礎設施（尚無 recall 呼叫）

M2 落地後盤點發現兩個缺口：當時 `agents/check/server.py`/`agents/notified/server.py` 的 `lifespan` 幾乎一字不差地各自手刻了一次「開 store、載 policy」，沒有共用；當時的 `agents/stt/server.py` 完全沒接記憶基礎設施。這三個 server 後來已合併為 [agents/runtime.py](../agents/runtime.py) 與 [agents/lifespan.py](../agents/lifespan.py)。

`stt` 目前確實沒有已知的記憶需求（轉錄不是判斷/決策類任務），但這不代表它永遠不會有——例如未來可能需要 procedural 記憶累積「某些專有名詞/公司內部代號要怎麼轉寫」這類修正規則。等真的有這個需求才回頭接基礎設施，會比現在就把「一個 agent 該怎麼接上記憶」變成平台共用的標準做法更貴。所以這一步只補基礎設施，不預先猜 `stt` 該記什麼：

| 檔案 | 異動 |
|---|---|
| `persistence/memory_lifespan.py` | **新增**。`open_agent_memory(policy_path: str)`：async context manager，內部做 M2 每個 agent server 手刻的那幾行（`get_memory_store()` + `store.setup()` + `load_memory_policy()`），yield `(store, memory_policy)`。任何 `agents/<name>/server.py` 的 `lifespan` 只要一行就能接上——這是這份計畫第一次把「怎麼讓一個新 agent 用上 `recall()`/`remember()`」本身平台化，而不是每個 agent 各自複製貼上 |
| [agents/lifespan.py](../agents/lifespan.py) | runtime 統一改用 `open_agent_memory()`，取代原本各 server 手刻的 `async with get_memory_store() as store: ...` + `load_memory_policy(...)` |
| [agents/runtime.py](../agents/runtime.py) | `stt` route 同樣取得共用 `app.state.store`/memory policy；當時先讓基礎設施就位，尚未呼叫 `recall()` |
| [mcp_servers/policy.yaml](../mcp_servers/policy.yaml) | **不**幫 `stt` 加 `memory:` entry——還沒有具體要讀哪個 `kind`/`scope`，先讓它維持 fail-closed（`recall()` 之後真的被呼叫也只會拿到空清單，不會報錯，見 `persistence/memory_smoke_test.py` 的 `policy_denial` 案例）。等哪天真的定義出 `stt` 該記什麼，才需要開 grant |

**M2.1 完成的定義**：三個 agent server 的 `lifespan` 都用同一個 helper 開 store/policy，`check`/`notified` 行為不變（`parity_check.py` 仍通過）；`stt` 的 `app.state.store` 存在但沒有任何呼叫端使用它。

### M2.2 — procedural/episodic 讀取端通用化，三個 agent 全部強制注入

M2.1 補的是「怎麼開 store」，這一步補「開了 store 之後怎麼撈記憶、怎麼塞進 prompt」——依 §3.9 的判準（補漏型記憶不管哪個 kind 都該強制），procedural/episodic 該對每個 agent 一視同仁；而這件事能做成平台層元件，前提是 §3.2 那條新規則：procedural/episodic 的 `content` 是平台標準 schema，不是場景自訂。

| 檔案 | 異動 |
|---|---|
| `persistence/memory_prompt.py` | **新增**。`inject_procedural(store, memory_policy, *, tenant, scope, base_prompt, limit)` 撈 procedural 規則、接成條列附加在 `base_prompt` 後面；`recall_episodic_few_shot(store, memory_policy, *, tenant, scope, limit)` 撈 episodic 案例、轉成 `[{"role": "user", ...}, {"role": "assistant", ...}]` 訊息對。兩者 `store`/`memory_policy` 是 `None` 或撈不到東西都回傳原樣/`[]`，不做任何事——跟 M2 的 no-op 契約一致 |
| [llm/tsmc_judge.py](../llm/tsmc_judge.py) | 拿掉 `_recall_system_prompt_and_few_shot`，改呼叫上面兩個平台函式；episodic 的 `content` schema 從 `{"transcript", "mentions_tsmc"}`（check 專屬命名）改成標準的 `{"input", "output"}`（`output` 存 `json.dumps({"mentions_tsmc": ...})` 這個完整字串，序列化決定留在寫入端） |
| [mcp_servers/notified/agent.py](../mcp_servers/notified/agent.py) | `_recall_system_prompt` 改名 `_recall_prompt_and_few_shot`，一樣呼叫 `inject_procedural`/`recall_episodic_few_shot`（`notified` 目前沒有這兩個 kind 的 `memory:` grant，呼叫了也是 no-op，但腿接上了）；收件人偏好（semantic）維持自己的邏輯不變，因為 §3.2 講的標準化不適用 semantic |
| [llm/stt_agent.py](../llm/stt_agent.py) | 同樣模式接上兩個平台函式，`transcribe()` 新增 optional `store`/`memory_policy`/`tenant` 參數。**已知形狀落差要記下來**：`recall_episodic_few_shot` 假設「一組 user/assistant 文字」就是完整的正確答案，但 `stt` 這個迴圈的正確答案其實是一串 tool call（見模組 docstring：逐字稿是從工具結果撈的，不是模型自己的回覆）——現在因為沒有 grant、呼叫了也是空清單，這個形狀落差不會造成問題，但真的要給 `stt` 開 episodic 記憶時，這個假設要重新想過，不是加資料就好 |
| [agents/runtime.py](../agents/runtime.py) | `stt` handler 不再丟棄 `context`，改把共用 store/memory policy/`context.tenant_id` 傳進 `transcribe()` |
| `persistence/memory_smoke_test.py` | `scenario_prefix_scope` 的 episodic 範例內容跟著改成 `{"input", "output"}`，避免文件跟程式碼對不上 |

**M2.2 完成的定義**：`check`/`notified`/`stt` 三個 agent 的 `_run_*_loop` 都呼叫同一組 `persistence/memory_prompt.py` 函式；`check` 用真實服務重新驗證過一次（同一句「晶圆代工龙头」測試句，塞記憶／拿掉記憶行為仍然反轉，`call_log` 也確認新 schema 的 few-shot 真的送進了模型）；`stt`/`notified` 的 procedural/episodic 呼叫因為沒有 `policy.yaml` grant，實際跑起來是 no-op，但程式碼路徑已經跟 `check` 一致。

### M3 — 寫入端：背景蒸餾器

**狀態：已落地。**

| 檔案 | 異動 |
|---|---|
| `orchestrator/memory_writer.py` | **新增**。`run_memory_writer(bus, workflow_def, store, memory_policy, *, worker_id)`：訂閱 `events_topic`、consumer_group `{name}.memory_writer`，逐事件蒸餾後 `remember()`，最後 `ack()`。**蒸餾規則本身不寫在這裡**，只依 `StepDef.memory_write` 宣告執行——引擎保持 workflow-agnostic，跟 `master_agent.py` 是純 interpreter 的設計一致。只處理 `status: "ok"` 的完成事件；`needs_review`/`error` 沒有「確定正確」的 output,直接跳過（見下方 HITL 缺口） |
| [orchestrator/workflow_def.py](../orchestrator/workflow_def.py) | `StepDef` 新增 optional `memory_write: tuple[MemoryWriteRule, ...]`（YAML 底下寫成 `memory: {write: [...]}`）。**跟原計畫的差別**：只做了 `write` 這一半——`read` 那一半在 M2.2 已經用另一條路徑（`persistence/memory_prompt.py` 直接被 `llm/tsmc_judge.py` 呼叫）解決掉了，不需要在這裡重複宣告一次。`kind` 目前只接受 `"episodic"`（`input_field`/`output_fields` 這組欄位是 episodic `{"input","output"}` 標準 schema 專用的形狀,procedural/semantic 沒有消費者、沒有定案的規則形狀前不硬做) |
| [workflows/definitions/stt_check_notify.yaml](../workflows/definitions/stt_check_notify.yaml) | 只在 `check` step 加 `memory: {write: [{kind: episodic, input_field: transcript, output_fields: [mentions_tsmc]}]}`——這就是未來 no-code UI 要生成的東西，等於提前驗證了介面。`stt`/`notified` 這次不加（跟 M2.2 對這兩個 agent 保持 no-op 是同一個態度） |
| [workflows/event_driven_pipeline.py](../workflows/event_driven_pipeline.py) | 新增 `--role memory-writer`，用跟 [agents/lifespan.py](../agents/lifespan.py) 相同的 `persistence/memory_lifespan.py::open_agent_memory()` 開長駐 store |
| [Procfile.workers](../Procfile.workers) | 加一行 `memory-writer:`（跟 master/worker 同一批長駐 process，不是 `Procfile` 那批常駐服務） |
| [orchestrator/smoke_test.py](../orchestrator/smoke_test.py) | 新增 `scenario_memory_writer_distills_episodic`（真實 stt→check→notified 全鏈路 + memory_writer 一起跑，斷言 `check` 的成功判斷確實蒸餾成 episodic 記憶,且跟 `llm/tsmc_judge.py` 讀取端用的是同一個 scope）與 `scenario_memory_writer_skips_needs_review`（`check` 回 `needs_review` 時確認沒有記憶被寫入) |

**實作陷阱那段 SQL 已經不需要了**：原計畫這裡曾經寫一段「上線前手動跑 SQL 把 `event_dispatch` 補 `done`，避免 `memory_writer` 第一次啟動時重放全部歷史事件」的 workaround。這件事後來已經有根本修法落地——`event_bus/base.py`/`event_bus/postgres.py` 實作並測試過 `subscribe(topic, group, worker_id=..., start_from="now")`（見 `fixed.md`），`run_memory_writer()` 直接傳這個參數即可，不需要任何手動步驟；`event_bus/base.py` 的 `subscribe` docstring 甚至直接點名「a future long-term-memory distiller」是這個參數的動機案例。

**HITL 缺口（要明講,M3 仍然沒有補上）**：episodic 記憶**最高品質的來源是人工裁決**，但這個平台目前**沒有人工裁決的入口**——`orchestrator_runs` 停在 `needs_review` 之後就沒有下一步了（已記錄進 [TODO.md](../TODO.md#needs-review-decision-entry)）。所以 M3 只蒸餾了自動可得的訊號（成功 run 的輸入/輸出對）,**沒有**蒸餾 `needs_review` 的原因字串（那沒有確定答案,寧可不寫,見上方 memory_writer 的行為)，**品質天花板有限**。這是刻意排除在 M3 範圍外的獨立工作項——牽涉新 API/UI、範疇跟「依宣告蒸餾寫入」的機制本身不同——等 M3 上線、實測品質天花板造成多少誤判之後再評估是否要提前做,不是 M3 的阻塞依賴。

**M3 完成的定義**：`check` 每次成功判斷,`memory_writer` 都把「逐字稿 → 判斷結果」蒸餾成一則 episodic 記憶,寫進 `llm/tsmc_judge.py` 讀取端已經在用的同一個 namespace（`default/episodic/stt_check_notify/check`）——寫入端跟讀取端第一次真正接起來，形成閉環的前半段。`needs_review` 的完成事件確認不會產生任何記憶。`orchestrator/smoke_test.py` 兩個新情境（見上表）搭配真實服務全部通過。**沒做的部分**：`needs_review` 的人工裁決蒸餾（見上方 HITL 缺口）、procedural/semantic 的自動蒸餾（沒有消費者前不猜)、寫入品質關卡（M5 才做,現在寫進去的記憶沒有 `pending`/`active` 分級,`recall()` 讀到就能用——如實記錄、不假裝已經有關卡)。

### M4 — 語意檢索（pgvector）

| 檔案 | 異動 |
|---|---|
| 環境 | pgvector 已裝好且 `agent_architecture` 已 `CREATE EXTENSION vector;`（見 §1.3），這裡不用再做 |
| [gateway/config.yaml](../gateway/config.yaml) | 新增 embedding model，例如 `local-embed` → `ollama/bge-m3`（1024 維）；README 前置需求補 `ollama pull bge-m3` |
| [gateway/client.py](../gateway/client.py) | 新增 `embed(model, texts) -> list[list[float]]` 與 async 版，走 `_client.embeddings.create()`，比照現有三個函式的 `log_call_sync` 模式。**log 只記筆數/字元數/dims，不記向量本身** |
| `persistence/memory_store.py` | `from_conn_string(..., index={"embed": <gateway callable>, "dims": 1024, "fields": ["content.text"]})`；同時決定要不要 `ttl=` + `start_ttl_sweeper()` |
| `persistence/memory.py` | `recall()` 的 `query=` 參數真正生效（M1~M3 期間可先退化成 filter 查詢） |

**遷移注意**：`store_vectors` 只會對「開了 index 之後才寫入」的記憶建 embedding，M1~M3 期間寫入的舊記憶需要一支一次性 backfill 腳本重新 `aput()`。

### M4.5 — semantic 記憶的 MCP 化讀取端（讓 agent 自己決定要不要查）

**狀態：已落地。**

依 §3.9 的判準，`notified` 的收件人偏好記憶要從 M2 的強制注入遷移成 MCP tool。排在 M4 之後，是因為只查固定 `scope` key 的工具跟現在的強制注入沒有實質差別——agent 真正需要的彈性是自己下 query 文字查，這要等 M4 的向量檢索才有意義。

| 檔案 | 異動 |
|---|---|
| `mcp_servers/memory/server.py` | **新增**。比照 `mcp_servers/lookup/server.py` 的形狀，包一個 `recall_semantic_memory(scope: list[str], query: str \| None)` 工具，內部呼叫 `persistence/memory.py` 的 `recall(MemoryKind.SEMANTIC, tenant="default", ...)`（tenant 先寫死——見 §5 風險 7,這個示範場景本來就沒有真正的多租戶）。回傳值帶 facets（`returned`/`truncated`/`hint`），不是裸的 top-k 列表 |
| [mcp_servers/policy.yaml](../mcp_servers/policy.yaml) | `servers:` 加這個新 server；`notified` 的 `principals:` 加 `allow: ["memory__recall_semantic_memory"]`。**`memory:` 區塊本身沒有動**——`notified` 原本就有 `default/semantic/recipient/*` 的讀權限,剛好夠用,不需要碰那個「還沒跟主管定案」的區塊（見下方 TODO.md 連結） |
| [mcp_servers/notified/agent.py](../mcp_servers/notified/agent.py) | 拿掉 `_recall_prompt_and_few_shot()` 對收件人偏好的無條件 `recall()` 呼叫；`recipient_id` 改成寫進 user message 讓模型看得到，才能自己組出 `scope=["recipient", recipient_id]` 去呼叫新工具；procedural/episodic（`inject_procedural`/`recall_episodic_few_shot`）維持強制注入不動——§3.9 的判準只挪動 semantic 這一格 |
| [mcp_servers/tool_errors.py](../mcp_servers/tool_errors.py) | `guarded_tool` 原本只包同步工具函式；`recall_semantic_memory` 要 `await recall()`,所以擴充成偵測 `inspect.iscoroutinefunction`,同時支援 sync/async 兩種 `@mcp.tool()` |
| 驗證 | 手動跑過一次真實服務全鏈路（`persistence/memory.py::remember()` 塞一則收件人偏好 → 透過真的 `MCPGateway`/`gemini-cheap` 跑 `decide_and_notify()`）：模型主動呼叫了 `memory__recall_semantic_memory`,查到偏好後正確改用 Gmail（不是 Slack）,2 輪內完成,沒有觸發 `StallGuard`——`MAX_TURNS=20` 多開一個工具沒有讓 stall 機率有感上升 |

**沒在原計畫表格裡、但落地時才發現必須解決的架構缺口**：`mcp_servers/memory/server.py` 是 `MCPGateway.connect()` 用 `uv run python -m ...` 起的獨立 subprocess，`persistence/call_log.py` 的 `current_node_name` 是 process-local 的 `ContextVar`，過不了 stdio 這道 process 邊界——不處理的話,`recall()` 在這個 subprocess 裡讀到的 principal 永遠是 `None`,`memory_policy.py::can_read()` 永遠 fail closed,新工具永遠查不到東西（不是安全漏洞,是功能整個失效）。修法：[mcp_servers/gateway.py](../mcp_servers/gateway.py) 的 `MCPGateway.__init__` 新增 optional `principal` 參數,`connect()` 透過 `StdioServerParameters(env=...)` 把它寫進子行程環境變數（現由 [agents/lifespan.py](../agents/lifespan.py) 依 step name 建構 gateway 並固定 principal）,`mcp_servers/memory/server.py` 啟動時讀那個環境變數、`current_node_name.set()` 一次。這個修正跟 `memory:` 區塊怎麼設計無關,純粹是讓既有機制在「recall() 被包成 MCP tool」這個新場景下能正確運作,但任何未來想這麼做的人都要知道這件事。

> ⚠️ **權限模型刻意簡化,不是忘了做**：這個工具同時要過兩層檢查——`mcp_servers/policy.yaml` 的 `principals:`/`roles:`（能不能呼叫這個工具）和 `memory:` 區塊（呼叫之後能不能讀到那個 namespace）。**這一版先不做任何通用化的雙層一致性檢查機制,兩個地方的 grant 都手動寫死在 `policy.yaml` 裡**，因為完整的存取治理模型還沒跟主管定案(見 §3.4 跟 [TODO.md](../TODO.md) 的 `memory-policy-pending`)，值得投資做一個通用機制之前，這個懸而未決的前提要先解決。這是刻意的短期簡化，不是遺漏——詳細追蹤記在 TODO.md。

### M5 — 品質關卡（沒有這個就不要開自動寫入）

> **展開版在 [docs/knowledge-distillation-plan.md](knowledge-distillation-plan.md)**：這節只有下面四個 bullet 的方向，具體要做哪些檔案、分幾步、有哪些還沒決定的問題（包括「非結構化輸出怎麼定過關」「regression set 三種 kind 都納入嗎」「既有 70 筆記憶沒有 `status` 欄位，加 filter 會全部讀不到」）都在那份文件。那份文件同時涵蓋「episodic 歸納成 procedural」的知識蒸餾——它跟這個關卡是同一條鏈的前後兩半（蒸餾生出候選規則、關卡決定候選能不能生效），缺任一半另一半都不該上線。

harness 原則第 7 篇講得很明確：**「沒有測試把關的 skill 只是把幻覺寫進記憶」**，而且第 ⑥ 層「關卡」是整套堆疊的樞紐、不可省略。自動寫入的記憶會直接改變 agent 行為，形同讓系統自我改版——所以：

- 固定評測集：`samples/gen_tsmc_*.wav` / `gen_other_*.wav` 已經是雛形，需要擴充成有標註的正負案例集，並切出 holdout。
- Regression set：每一則被寫入的記憶，都要伴隨一個「當初它想修好的案例」進 regression set。
- 晉級關卡：記憶寫入預設為 `pending` 狀態（`value.status`），要通過評測（新案例過、舊案例不退步）才轉 `active`；`recall()` 預設只讀 `active`。這用 `filter={"status": "active"}` 就能做，不需要額外的表。
- 回滾：`store` 有 `updated_at`，加上 `value.version` 就能回退；高風險類型（procedural——它直接改 system prompt）強制人工審查。

`workflows/parity_check.py` 是現成的 regression harness 雛形，可以擴充成記憶晉級關卡的執行器。

---

## 5. 風險與已知限制

1. **記憶污染 / Goodhart**：蒸餾出錯的規則會讓 agent 穩定地錯下去，而且比 prompt bug 更難發現（它不在 code review 的範圍內）。緩解：M5 的關卡 + 每則記憶的 `source_thread_id` 稽核鏈 + procedural 類強制人工審查。
2. **Prompt 膨脹**：episodic few-shot 塞太多會把 context 撐爆、也會拖慢 `local-qwen`。`recall()` 一律強制 `limit`，並在 `StepDef.memory.read` 裡宣告上限，不讓場景層自由調大。
3. **跨租戶洩漏**：namespace 打錯就是資料外洩。`memory_policy.py` 必須 fail closed，且 smoke test 要有跨 tenant 的負面案例。
4. **M1~M3 階段仍只做等值 filter，語意檢索留給 M4**：pgvector 雖然已裝好（見 §1.3），但 M1~M3 刻意不啟用向量索引，避免場景邏輯提早滲透進基礎設施。要誠實看待這幾個階段的召回率，不要用它的效果去否定整個方案。
5. **同步路徑（`simple_pipeline.py`）刻意不動**：`parity_check.py` 的 `_assert_simple_pipeline_untouched()` 本來就會擋。記憶對它是 optional 參數，預設 `None` 時行為完全不變——跟 agent API 合約當初的處理方式一致。
6. **`store` 表的成長與清理**：TTL sweeper 是背景 thread，`memory-writer` process 掛掉就沒人清。長期要有獨立的清理排程，第一階段先接受。
7. **多租戶目前是假的**：`tenant_id` 現在只會是 `"default"`。這個欄位是**為了不用在有真實租戶時做痛苦的資料遷移**而先預留的，不是現在就要做多租戶。

---

## 6. 一句話總結

Checkpointer 讓一次執行**可以中斷後續跑**；長期記憶讓平台**跨執行變聰明**。前者是框架自動幫你做的錄影，後者是你必須主動決定要記什麼、並且要有評測關卡守住品質的資產——而在這個專案裡，因為事件驅動路徑沒有 compiled graph，它必須做成獨立於編排模式的平台元件，而不是掛在 LangGraph 圖上的一個參數。
