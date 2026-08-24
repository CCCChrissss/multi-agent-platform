# Agent API 合約

## Context

主管提到過去的 agent 專案實務上都會把 agent 做成 API,雖然目前這個平台沒有指定要解決哪個具體痛點,但背後的通用理由(團隊邊界清楚、語言/框架無關、獨立部署擴展、治理與可觀測性集中化)跟 CLAUDE.md 的平台目標(通用、可替換、為 no-code 組裝鋪路)是一致的。

這份文件定義「agent 的 API 合約長什麼樣子」,範圍是現有全部 3 個 agent(`stt`/`check`/`notified`),不分先後全部套用同一份規格。**這不是決定要把它們拆成獨立 HTTP service**——合約本身刻意跟傳輸層(Python function call / event_bus Event / 未來的 HTTP)脫鉤,不管以後用哪種方式呼叫,agent 收/還的資料形狀都是同一份,不用因為換傳輸層重寫一次規格。

已落地到 event-driven 路徑(`orchestrator/`、`workflows/definitions/stt_check_notify.yaml`),同步路徑([workflows/simple_pipeline.py](../workflows/simple_pipeline.py))刻意維持不動,原因見文末。

## 合約總覽(Envelope)

Request envelope:

```json
{
  "thread_id": "...",
  "input": { },
  "context": { }
}
```

Response envelope:

```json
{
  "status": "ok | needs_review | error",
  "output": { },
  "review_reason": "...",
  "error": "..."
}
```

- `thread_id`:這次 workflow run 的識別碼,呼叫端必須**明確傳入**,不能再靠隱含的方式帶入(理由見下方「Identity 與可觀測性」)。
- `input`/`output`:每個 agent 各自定義的業務欄位,見下方「三個現有 agent 的合約規格」。
- `context`:選填,預設 `{}`。承載 `tenant_id`/`user_id` 這類**跨 run 的身分維度**,跟 `input`(本次的業務資料)分開放。理由見下方「為什麼 `thread_id` 不足以支撐長期記憶」。
- `status` 三個值的定義,沿用現有 [harness/agent_loop.py](../harness/agent_loop.py) 的 `AgentLoopIncomplete` 語意,不新創一套:
  - `ok`:業務邏輯正常跑完,`output` 是最終結果。
  - `needs_review`:agent 自己判斷沒辦法達到有信心/已驗證的結論(對應現在丟出 `AgentLoopIncomplete`),需要人工介入,不是系統錯誤。
  - `error`:執行中出現非預期例外,狀態未知,當作失敗處理(對應現在 worker 的 broad except 分支)。
- `output`/`review_reason`/`error` 三者互斥,由 `status` 決定要看哪一個。這樣設計是為了讓合約在資料結構上就杜絕「業務欄位剛好叫 `status` 或 `error`,把 envelope 自己的欄位蓋掉」——現在 [orchestrator/worker.py](../orchestrator/worker.py) 是靠 `{**result, "status": "ok"}` 這種 dict 展開順序的技巧來防這件事,合約改用巢狀結構後,這個風險在結構上就不存在,不用再靠「欄位放的順序」這種容易忘記的約定。

## Identity 與可觀測性

`thread_id` 一定是 request envelope 的明確欄位,不是隱含帶入。現在 [persistence/call_log.py](../persistence/call_log.py) 是靠 `current_thread_id`/`current_node_name` 兩個 contextvar,在同一個 Python process 內「隱形地」把每一筆 LLM/tool 呼叫關聯回正確的 run——這招只有同一個 process 才有效。合約要求 `thread_id` 明確傳入,agent 不管是被同一個 process 呼叫、還是被獨立 process/service 呼叫,都能自己決定要把 log 記到哪個 run 底下。

`node`/principal 身分不需要放進 request envelope。因為現在每個 agent(worker/consumer_group)本來就只服務一個 step,身分是由「呼叫的是哪個 agent」決定,不是呼叫端逐次宣告——這跟現有 [mcp_servers/gateway.py](../mcp_servers/gateway.py) 用 `current_node_name` 做 RBAC 的精神一致,只是身分認定的來源從 contextvar 換成「這是哪個 agent 的 endpoint/topic」。

### 為什麼 `thread_id` 不足以支撐長期記憶

`thread_id` 每次執行都是新的 uuid([orchestrator/trigger.py](../orchestrator/trigger.py)、[workflows/simple_pipeline.py](../workflows/simple_pipeline.py)),**保證永不重複**;而長期記憶([docs/long-term-memory-plan.md](long-term-memory-plan.md))的定義就是跨執行——用 `thread_id` 當記憶的 namespace 鍵,等於每次都開一個新資料夾,記憶永遠查不到上一次的東西。也就是說,這不是欄位取錯,而是「跨越很多次執行的身分」這個維度在原本的合約裡根本不存在。

直覺的修法(把 `tenant_id`/`user_id` 塞進 `input`)會壞在三個地方:會被 [orchestrator/workflow_def.py](../orchestrator/workflow_def.py) 的 `validate_input`/`validate_output` 當成未宣告的多餘欄位擋掉;把「這次要做什麼」跟「這次是替誰做」這兩種不同壽命的資料混進同一個欄位,污染每個 agent 各自的業務 schema;而且每個 workflow YAML 的 `input_schema` 都要重複宣告這兩個欄位。

所以合約在 `input` 之外加第三個 top-level 欄位 `context: dict`(預設 `{}`),跟 `input` 分開。`thread_id`(這一次)對 `context`(跨越很多次)的關係,剛好是 checkpointer([persistence/checkpointer.py](../persistence/checkpointer.py))對長期記憶(store)的關係,往上搬到 API 層的鏡像——完整的取捨過程見 [docs/long-term-memory-plan.md](long-term-memory-plan.md) §3.3。

目前只有 `check`/`notified` 兩個 agent 真的讀 `context`(見下方合約規格),讀出 `tenant_id`/`user_id` 後轉呼叫 `persistence/memory.py` 的 `recall()`;`stt` 不需要記憶,接受這個欄位純粹是配合共用的 `agents/envelope.py::Handler` 簽章,直接忽略。呼叫端(`agents/*/client.py`)目前一律預設 `{"tenant_id": "default"}`——多租戶身分還沒有真正的來源,這個欄位是為了將來接上真實租戶時不用再改一次合約而先留的位置。

## 傳輸層對應(Transport Mapping)

同一份 envelope,不同傳輸層各自怎麼扛,合約本身不預設是哪一種:

| 傳輸層 | Request envelope | Response envelope |
|---|---|---|
| 同步 in-process([workflows/simple_pipeline.py](../workflows/simple_pipeline.py)) | 直接呼叫 Python function,參數是 envelope 攤平後的欄位 | function 回傳值攤平成 envelope |
| 事件驅動([orchestrator/worker.py](../orchestrator/worker.py)) | `Event.payload` = envelope 的 `input`(`thread_id` 已經是 `Event.thread_id`,不用重複塞進 payload) | completion event 的 `payload` = envelope攤平後的欄位 |
| 獨立 HTTP service([agents/](../agents/)) | HTTP request body = envelope(JSON,見 `agents/envelope.py` 的 `AgentRequest`) | HTTP response body = envelope(JSON,見 `AgentResponse`) |

三個 agent 後來真的被包成獨立 HTTP service 了(見下方「對現有程式碼的影響」)——envelope 本身完全沒有跟著改,只有 `workflows/event_driven_pipeline.py` 的 handler 內部從「直接呼叫 Python function」換成「打 HTTP call」,證明這份合約設計如預期地 transport-agnostic。

## 三個現有 agent 的合約規格

依照現有程式碼([workflows/event_driven_pipeline.py](../workflows/event_driven_pipeline.py) 的 `STEP_HANDLERS`、各 agent 實作)整理出的 input/output schema:

### stt

- input:`audio_ref: str`——音檔路徑/URI
- output(`ok`):`transcript: str`
- `needs_review` 常見原因(見 [llm/stt_agent.py](../llm/stt_agent.py)):模型連續多輪沒呼叫工具、或在 `MAX_TURNS` 內沒能產出逐字稿

### check

- input:`transcript: str`
- output(`ok`):`mentions_tsmc: bool`
- `needs_review` 常見原因(見 [llm/tsmc_judge.py](../llm/tsmc_judge.py)):模型在 turns 內沒給出最終判斷、或確定性別名比對命中但模型仍判 false,經二次提示仍未改判
- `context.tenant_id`(選填,預設 `"default"`):組 prompt 前用來 `recall()` 這個 tenant 底下累積的 procedural/episodic 記憶([docs/long-term-memory-plan.md](long-term-memory-plan.md) M2)

### notified

場景無關的通知代理(docs/exclusion-scenario-plan.md §3.5/P0):只決定「要不要送、送到哪個管道」,不判斷「這段內容值不值得通知」——那是呼叫端(例如 `check` 這種場景判斷步驟)的責任,寫進 `should_notify`/`subject`/`body` 交過來。

- input:`should_notify: bool`、`subject: str`、`body: str`
- output(`ok`):`notified_log: list[str]`——每一步工具呼叫的紀錄字串
- `needs_review` 常見原因(見 [llm/notify_agent.py](../llm/notify_agent.py)):`should_notify=True` 但沒有觀察到成功的 `send_gmail_message`/`send_slack_message` 呼叫
- `context.tenant_id`/`context.user_id`(選填,預設皆為 `"default"`):`recall()` 這位收件人(`user_id` 暫代真正的 recipient 身分)的通知管道偏好
- `workflows/simple_pipeline.py`(凍結)仍呼叫舊的 `decide_and_notify(gateway, transcript, mentions_tsmc)` 合約——這個舊合約(含台積電專屬的通知規則)保留在 [mcp_servers/notified/agent.py](../mcp_servers/notified/agent.py) 當一層相容 shim,只服務這一個凍結呼叫端,不是平台合約的一部分

## 對現有程式碼的影響

這份合約已經落地到事件驅動路徑的三個 agent:

| 檔案 | 異動 |
|---|---|
| [orchestrator/workflow_def.py](../orchestrator/workflow_def.py) | `StepDef` 新增必填的 `input_schema`/`output_schema`(JSON Schema draft 2020-12,不只檢查欄位有沒有出現,連型別/形狀都驗),以及 `validate_input()`/`validate_output()` 兩個合約檢查方法(底層用 `jsonschema` 套件) |
| [workflows/definitions/stt_check_notify.yaml](../workflows/definitions/stt_check_notify.yaml) | 三個 step 都補上 `input_fields`/`output_fields` 宣告 |
| [orchestrator/worker.py](../orchestrator/worker.py) | `_handle_one()` 執行 handler 前後呼叫 `validate_input`/`validate_output`,並改成組出巢狀 envelope(`{"status": "ok", "output": result}`)取代原本的攤平合併(`{**result, "status": "ok"}`) |
| [orchestrator/master_agent.py](../orchestrator/master_agent.py) | `start_run()` 呼叫 `step.validate_input()` 檢查外部觸發帶的初始 payload;`_handle_completion()` 改讀巢狀 envelope 的 `completion.payload["output"]`,不再需要手動濾掉 `"status"` 欄位 |
| [orchestrator/smoke_test.py](../orchestrator/smoke_test.py) | 手動組的 `StepDef`、以及直接讀 `completion.payload["transcript"]` 這類攤平欄位的斷言,改成讀 `payload["output"]["transcript"]` |

`orchestrator_runs.state_payload`(`run["state_payload"]`)本身維持攤平的累積狀態不變——`output` 是在 `master_agent.py` 裡被拆出來、合併進 `state_payload` 的,所以 [workflows/parity_check.py](../workflows/parity_check.py) 讀的是 `state_payload`,不需要跟著改。

[workflows/simple_pipeline.py](../workflows/simple_pipeline.py) **刻意維持不動**:同步模式的 `PipelineState.status` 存的是「跑到哪個階段」(`transcribed`/`checked`/`sent`),語意跟合約的 `status`(結果分類:`ok`/`needs_review`/`error`)不同,套用需要另外設計,而且 `workflows/parity_check.py` 的 `_assert_simple_pipeline_untouched()` 本來就會擋下這個檔案的異動——留待之後單獨排。

**驗證**:改完後跑過 `event_bus/smoke_test.py`(M0)、`orchestrator/smoke_test.py`(M2-M5)、`workflows/parity_check.py`(M6),全數通過。

### 後續:把三個 agent 真的包成獨立 HTTP service

上面落地的是 event-driven 路徑的 envelope 邏輯;之後進一步把 `stt`/`check`/`notified` 三個 agent 本身也包成獨立 HTTP service(不再是同一個 worker process 直接 import 呼叫),異動如下:

| 檔案 | 異動 |
|---|---|
| `agents/envelope.py` | 新增,`AgentRequest`/`AgentResponse` pydantic model + `run_handler()`——跟 `orchestrator/worker.py` 的 `_handle_one()` 平行實作同一套三段式分類,故意不共用 import(`agents/` 要能獨立部署,不該拉進 `orchestrator/` 的依賴鏈) |
| `agents/{stt,check,notified}/server.py` | 新增,各自一個 FastAPI app,`lifespan` 建立長期存活的 `MCPGateway`,`POST /run` 收 `AgentRequest`、呼叫原本的 agent function(`llm/stt_agent.py` 等,原地不動)、回傳 `AgentResponse` |
| `agents/{stt,check,notified}/client.py` | 新增,比照 `services/notified/client.py` 的 `httpx` + `ToolDependencyError` 慣例,把 response envelope 轉回舊有呼叫慣例(成功回傳業務欄位、`needs_review` 轉成 `raise AgentLoopIncomplete`、`error` 轉成 `raise RuntimeError`),讓上層呼叫端不用感知傳輸層換了 |
| [workflows/event_driven_pipeline.py](../workflows/event_driven_pipeline.py) | 三個 handler 改呼叫 `agents/*/client.py`;`build_step_handlers()` 拿掉 `gateway` 參數;worker process 不再需要自己建 `MCPGateway` |
| [orchestrator/smoke_test.py](../orchestrator/smoke_test.py)、[workflows/parity_check.py](../workflows/parity_check.py) | 事件驅動路徑的呼叫點跟著拿掉 `gateway` 參數;`parity_check.py` 的同步路徑仍保留 `MCPGateway`,因為 `workflows/simple_pipeline.py` 還是需要 in-process gateway |
| [Procfile](../Procfile) | 新增 `stt-agent`/`check-agent`/`notified-agent` 三行(port 8003/8004/8005) |

`orchestrator/worker.py`、`orchestrator/master_agent.py` 完全沒有變動——這正是 envelope 設計成 transport-agnostic 的目的:換傳輸層只動 handler 內部的呼叫方式，不動 envelope 邏輯本身。

### 後續:接上長期記憶讀取端([docs/long-term-memory-plan.md](long-term-memory-plan.md) M2)

| 檔案 | 異動 |
|---|---|
| `agents/envelope.py` | `AgentRequest` 新增 `context: dict`(預設 `{}`);`Handler` 型別從 `Callable[[dict], Awaitable[dict]]` 改成 `Callable[[dict, dict], Awaitable[dict]]`;`run_handler()` 把 `request.context` 一併傳給 handler |
| `agents/{stt,check,notified}/server.py` | 三個 `_handler` 簽章補上 `context` 參數;`check`/`notified` 的 `lifespan` 各自多開一個長期存活的 `AsyncPostgresStore`(`persistence/memory_store.py`)掛在 `app.state.store`,並載入 `persistence/memory_policy.py` 的 `memory_policy` 掛在 `app.state.memory_policy`;`stt` 只是配合共用簽章接受 `context`,直接丟棄不用 |
| [llm/tsmc_judge.py](../llm/tsmc_judge.py) | `mentions_tsmc()` 新增 optional `store`/`memory_policy`/`tenant` 參數(全部不傳時行為與改動前逐位元組相同,`workflows/simple_pipeline.py` 因此不受影響);組 messages 前呼叫 `persistence/memory.py` 的 `recall()`,procedural 記憶接在 system prompt 後面,episodic 記憶轉成 few-shot 訊息對插在使用者輸入之前;`_lookup_tsmc_aliases()` 額外 `recall()` semantic 記憶(`GLOBAL_TENANT`/`("company","tsmc")`),跟 `mcp_servers/lookup` 的靜態別名清單合併餵給確定性 alias backstop |
| [mcp_servers/notified/agent.py](../mcp_servers/notified/agent.py) | `decide_and_notify()` 同樣模式新增 optional 參數,外加 `recipient_id`;`recall()` 該收件人的 `semantic` 通知管道偏好注入 system prompt |
| `agents/{stt,check,notified}/client.py` | 公開函式與 `_run()` 都新增 optional `context: dict \| None = None`;未傳入時預設 `{"tenant_id": "default"}` |
| [docs/agent-api-contract.md](agent-api-contract.md)(本檔) | envelope 加 `context` 欄位、補「為什麼 `thread_id` 不足以支撐長期記憶」一節 |

**已知簡化,寫在這裡避免下一個人誤以為是設計定案**:`notified` 目前沒有獨立於 `context.user_id` 之外的「收件人」概念——`stt_check_notify` 這個示範 workflow 的 input schema 裡沒有 recipient 欄位,所以暫時借用 `context.user_id` 當 recipient 的 namespace key。這是場景層的示範簡化,不是平台合約決定收件人身分一定長這樣;等平台有真正的多收件人場景時要重新設計。

**這一步刻意跳過寫入**:`recall()` 只在 hot path 讀,`remember()` 完全沒有被接進任何 agent——寫入是背景蒸餾器(M3)的範圍,原因見 [docs/long-term-memory-plan.md](long-term-memory-plan.md) §3.7(避免已經在跑多輪 tool-calling loop 的 agent 一心二用)。

**驗證**:`persistence/memory_smoke_test.py` 的 policy/namespace 機制不受影響(這次沒有改 `mcp_servers/policy.yaml`);手動對 `check` agent 的 namespace `default/procedural/stt_check_notify/check` 塞一則規則,`check` agent 的判斷確實會反映在它的 system prompt 裡。

### 後續:記憶讀取端平台化([docs/long-term-memory-plan.md](long-term-memory-plan.md) M2.1/M2.2)

M2 是把記憶接進 `check`/`notified`,但接法(開 store、撈記憶、塞進 prompt)是三個 agent 各自手刻的。M2.1/M2.2 把這兩件事都收成平台層元件:`persistence/memory_lifespan.py::open_agent_memory()` 統一開 store/policy;`persistence/memory_prompt.py::inject_procedural()`/`recall_episodic_few_shot()` 統一把 procedural/episodic 記憶轉成 prompt 文字/few-shot 訊息。**三個 agent(含 `stt`)現在都呼叫同一組函式**,差別只在 `stt`/`notified` 目前沒有 `policy.yaml` 的 `memory:` grant,呼叫了也是 no-op——程式碼路徑統一了,不代表三個 agent 現在都真的有記憶在用。

這一步也改了 `check` 既有的 episodic `content` schema:從 `{"transcript", "mentions_tsmc"}` 這種 check 專屬命名,改成平台標準的 `{"input", "output"}`(`output` 是完整字串,序列化留給寫入端決定)——因為讀取端要通用,`content` 就不能再是逐 agent 自訂,細節見 [docs/long-term-memory-plan.md](long-term-memory-plan.md) §3.2。semantic 記憶(公司別名、收件人偏好)不受影響,維持場景自訂,因為不同 subject 的屬性形狀天生不同,套不上同一個標準。
