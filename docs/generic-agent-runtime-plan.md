# 泛用 Agent Runtime：讓 agent 的身分變成設定，而不是程式碼

> [!NOTE]
> 這是跨階段的架構設計與落地紀錄。內容中的歷史執行結果予以保留，但不等於目前 Windows 服務正在運行；現行狀態見 [current-windows-status.md](current-windows-status.md)。

## 1. 想解決的問題

現在 `stt`、`check`、`notified` 是三個**寫死在程式碼裡的 agent**。要在平台上新增第四個 agent，得做這些事：

1. 寫一個 `llm/xxx_agent.py`（prompt 常數、tool-calling 迴圈、輸出組裝）
2. 寫一個 `agents/xxx/server.py` + `client.py`
3. 在 [Procfile](../Procfile) 開一個新 process、佔一個新 port
4. 在 [mcp_servers/policy.yaml](../mcp_servers/policy.yaml) 加一個 principal
5. 在 [workflows/event_driven_pipeline.py](../workflows/event_driven_pipeline.py) 的 `build_step_handlers()` 加一段膠水
6. 在 workflow YAML 加一個 step

這六件事裡，只有第 6 件是「宣告」，其他五件都是「開發」。**這就是「三支寫死的服務」跟「平台」的分界線**——[AGENTS.md](../AGENTS.md) 講的 no-code/low-code 搭建，在這個結構下不可能達成。

理想的樣子是：**agent 本身沒有身分**，它是一個泛用的 tool-calling runtime，掛上「模型 + MCP 工具 + 記憶 + prompt + 輸入輸出契約」之後才「變成」stt 或 notified。改掛的東西，stt 就變成 notified。

## 2. 現況：資料是怎麼在 step 之間流動的

先講清楚，因為這件事**比直覺想的更有能力，也因此限制更微妙**。

[orchestrator/master_agent.py](../orchestrator/master_agent.py) 的 `_handle_completion()`：

```python
business_payload = completion.payload["output"]          # 這一步的產出
merged_payload = run_state.merge_state(run, business_payload)   # 併進整個 run 的累積狀態
next_step_payload = {k: v for k, v in merged_payload.items()
                     if k in next_step.input_schema.get("properties", {})}   # 按欄位名挑
```

也就是說：**下一步的輸入 = 從整個 run 累積的扁平狀態裡，挑出下一步 input_schema 有宣告的同名欄位。**

三個推論：

- 來源**不限上一格**。只要更早的步驟產出過同名欄位、而且沒被後來的步驟覆蓋，就撈得到。
- 對接靠的是**欄位名巧合**，不是 schema 相等。
- `state_payload` 是**扁平合併**（`run_state.merge_state()` 就是 `{**舊, **新}`，資料庫端是 jsonb `||`）。

### 真正的限制

1. **只能同名對接**——沒有改名、沒有常數、沒有字串模板、沒有推導欄位。
2. **扁平命名空間**——兩個步驟產出同名欄位會**互相覆蓋**，後者贏且無聲。也因此**無法定址「哪一步的哪個欄位」**，`steps.stt.transcript` 這種寫法在現有結構下根本無處可取。
3. **必須有某一步真的吐出那個名字**——`notified` 的 input_schema 要 `should_notify`/`subject`/`body`，而唯一可能的來源是先前步驟的輸出，所以 `check` 被迫產出這三個跟「判斷」無關的欄位。這就是場景膠水（[workflows/event_driven_pipeline.py](../workflows/event_driven_pipeline.py) 的 `check_handler`、[llm/exclusion_judge.py](../llm/exclusion_judge.py) 的 `_build_result()`）存在的原因。

## 3. 現況盤點：地基比想像中近

**已經是設定、不用動的：**

| 面向 | 在哪裡 |
|---|---|
| 模型 | 一個字串，走 LiteLLM Gateway（[gateway/config.yaml](../gateway/config.yaml)） |
| MCP 工具 | [mcp_servers/policy.yaml](../mcp_servers/policy.yaml) 的 `principals` |
| 記憶 namespace | 同一份 policy.yaml 的 `memory:` |
| step 順序、input/output schema、記憶蒸餾規則 | [workflows/definitions/*.yaml](../workflows/definitions/) |
| HTTP 合約 | [agents/envelope.py](../agents/envelope.py)：`AgentRequest{input, context} → AgentResponse{status, output}`，本來就不知道自己在跑哪個場景 |
| tool-calling 迴圈 | [harness/agent_loop.py](../harness/agent_loop.py) 的 `run_tool_calling_loop()`（`llm/stt_agent.py` 例外，自己寫了一份，是既有不一致） |
| 編排層 | `master_agent` / `worker` / `memory_writer` 全都是 WorkflowDef 的純解釋器，沒有硬編碼任何 step 名 |

**還寫死在程式碼裡的：**

| 面向 | 在哪裡 | 難度 |
|---|---|---|
| **輸出組裝邏輯** | `llm/*.py` 各自一套（見 §4） | 高 |
| **欄位對應** | 只有同名自動撈（見 §2） | 中 |
| prompt | `llm/*.py` 的 `_SYSTEM_PROMPT` 模組常數 | 低 |
| process / port / principal | [Procfile](../Procfile) 三個固定 port；`make_lifespan("check", principal="check")` | 中 |
| 場景膠水 | `build_step_handlers()`、`_build_result()` | 修完對應層自然消失 |

## 4. 硬骨頭：輸出是怎麼組裝出來的

[harness/schema_validation.py](../harness/schema_validation.py) 只保證**最後那個 dict 長什麼樣**，不保證**怎麼來的**。目前五種寫法：

| agent | 做法 | 位置 |
|---|---|---|
| `check`（台積電） | 模型吐 JSON → `json.loads` | [llm/tsmc_judge.py](../llm/tsmc_judge.py) `_parse_verdict()` |
| `check`（除外責任） | 同上，但模型常在 JSON 前加廢話，改用 `raw_decode` 從第一個 `{` 找起 | [llm/exclusion_judge.py](../llm/exclusion_judge.py) `_parse_verdict()` |
| `stt` | **不理模型講什麼**，直接截工具回傳值（避免長逐字稿被改寫／截斷） | [llm/stt_agent.py](../llm/stt_agent.py) |
| `notified` | 從每次工具呼叫的回呼累積 log | [llm/notify_agent.py](../llm/notify_agent.py) `_on_tool_result` |
| `check` 的三個欄位 | `should_notify`/`subject`/`body` **沒問模型**，程式碼推導 | `_build_result()` |

要讓 agent 變成設定，這五種必須收斂成有限的宣告選項。

## 5. 界線：「固定輸入輸出在 MCP」只對一半

**對的部分**：MCP tool 有 JSON Schema，是很好的**建議來源**。掛上 `stt__transcribe(path: string) -> string`，UI 可以提示「要不要把 `audio_ref: string` 加進輸入、`transcript: string` 加進輸出」。

**不對的部分**：**agent 的 input/output 不是它工具的 input/output 的聯集。**

```
check 掛的工具：browse_semantic_memory(prefix: string)
check 的輸入：transcript            ← 從來沒進過任何工具
check 的輸出：involves_exclusion / matched_articles / reason
                                    ← 沒有任何工具產出這個
```

逐字稿是**給模型讀的**；判斷是**模型產的**，工具只是蒐證手段。工具 schema 描述「模型可以做什麼動作」，agent schema 描述「這個任務收什麼、交什麼」。

- **薄 agent**（`stt`）：本質是工具包裝，推論成立。
- **厚 agent**（`check`）：產出是判斷，推論不成立。

**結論：MCP schema 當建議來源，不當唯一真相。**

## 6. 目標形狀

```yaml
- name: check
  desc: 判斷是否涉及除外責任
  model: gemini-strong
  tools: [memory__browse_semantic_memory]
  memory: ["_global/semantic/insurance_product/*"]

  input:
    transcript: {type: string, from: "steps.stt.transcript"}

  prompt:
    system: "你是保單條款判讀員，先用 browse_semantic_memory 找到相關條款…"
    user: "{{ input.transcript }}"

  output:
    involves_exclusion: {type: boolean,  from: model}
    matched_articles:   {type: string[], from: model}
    reason:             {type: string,   from: model}

  verifiers: [cited_articles_were_browsed]
```

`from:` 把 §4 的五種組裝收斂成宣告：

| `from` | 語意 | 取代 |
|---|---|---|
| `model` | 模型直接產出（用 structured output 強制格式） | 兩個 `_parse_verdict()` |
| `tool: <name>` | 直接取工具回傳值 | `stt` 的逐字稿捕捉 |
| `tool_log` | 蒐集工具呼叫紀錄 | `notified_log` |
| `expr` | 從其他欄位／模板推導 | `_build_result()` |

---

# 分階段與完成標準

專案沒有 pytest，驗證方式一律是 smoke test（見 [docs/testing.md](testing.md)）。下面每階段的完成標準都寫成可以直接跑的東西。

**每一階段共通的迴歸底線**（列一次，之後不重複）：

```bash
uv run python -m event_bus.smoke_test        # 不需要 LLM
uv run python -m orchestrator.smoke_test     # 需要 honcho start
uv run python -m workflows.parity_check      # 需要 honcho start
```

三個都必須通過，且 `parity_check` 的 `_assert_simple_pipeline_untouched()` 意味著 **[workflows/simple_pipeline.py](../workflows/simple_pipeline.py) 全程不得修改**（它會 `git diff main` 比對）。

---

## P0 — 欄位對應層（= [issue #32](https://github.com/donydony228/agent-architecture/issues/32)）

所有事情的前提。

### 範圍

在 workflow YAML 的 step 上新增 `input_mapping`，三種來源：

```yaml
- name: notified
  input_mapping:
    should_notify: {from: "steps.check.involves_exclusion"}
    subject:       {const: "保單理賠初判：可能涉及除外責任"}
    body:          {expr: "逐字稿：{{ steps.stt.transcript }}\n引用條文：{{ steps.check.matched_articles }}\n理由：{{ steps.check.reason }}"}
```

**不做條件式**（`if involves else ""`）。理由：`should_notify=false` 時 [llm/notify_agent.py](../llm/notify_agent.py) 本來就不會送，`subject`/`body` 的內容無關緊要——`_build_result()` 裡那兩個三元運算是防禦性的冗餘，可以直接刪掉，不需要在 DSL 裡複製它。加條件式是 P0 最容易失控的地方，明確排除。

### 要改的檔案

| 檔案 | 改什麼 |
|---|---|
| [orchestrator/workflow_def.py](../orchestrator/workflow_def.py) | `StepDef` 加 `input_mapping`；loader 解析並**在載入時**驗證（見下方完成標準） |
| [orchestrator/run_state.py](../orchestrator/run_state.py) | **新增每步輸出的獨立儲存**：`state_payload["_steps"][step_name] = output`。扁平合併**保留不動**（`memory_writer` 的 `merged_state.get(rule.input_field)`、`parity_check` 的 `ed_payload["transcript"]`、checkpoint 鏡像全都讀扁平結構，動它會連鎖破三個地方） |
| [orchestrator/master_agent.py](../orchestrator/master_agent.py) | `_handle_completion()` 有 `input_mapping` 就用它求值，沒有就走現在的同名過濾（向後相容） |
| [workflows/definitions/stt_exclusion_notify.yaml](../workflows/definitions/stt_exclusion_notify.yaml) | `check` 的 output_schema 拿掉 `should_notify`/`subject`/`body`；`notified` 加 `input_mapping` |
| [llm/exclusion_judge.py](../llm/exclusion_judge.py) | `_build_result()` 縮成只回三個判斷欄位（等於刪掉整個函式，直接回 `_parse_verdict()` 的結果） |
| [workflows/event_driven_pipeline.py](../workflows/event_driven_pipeline.py) | 除外責任場景的 `check_handler` 變成純轉發 |

### 完成標準

1. **載入時就擋掉錯誤，不是執行時才炸**：`load_workflow_def()` 對以下情況丟 `ValueError`，訊息要指名是哪個 step 的哪個欄位
   - `from:` 指向不存在的 step 名
   - `from:` 指向的 step 排在自己**後面**（前向參照）
   - `from:` 指向的欄位不在該 step 的 `output_schema.properties` 裡
   - `input_schema` 的 `required` 欄位既沒有 `input_mapping` 條目、也不可能靠同名撈到
2. **求值失敗是明確的終止狀態**：執行時某個必填欄位解不出值（例如來源 step 因 `needs_review` 沒產出），run 標成 `failed`，`state_payload` 帶訊息指名欄位與來源，**且不發出下一個 command**。不可以發出一個註定 schema 驗證失敗的命令。
3. **兩個現有 workflow 的行為不變**：`stt_check_notify.yaml` 完全不加 `input_mapping` 也照跑（走同名 fallback），`parity_check` 通過。
4. **`_steps` 不進 checkpoint 鏡像**（已定案，見下方「資料庫層面的影響」）：`record_step()` 收到的 `state_payload` 必須已經濾掉 `_steps`，`checkpoints.channel_values` 的 key 集合與改動前完全一致，`parity_check._checkpoint_shape()` **一行都不用改**。
5. **新增 smoke test 情境**到 [orchestrator/smoke_test.py](../orchestrator/smoke_test.py)：
   - `mapping_renames_field`：上游輸出 `a`，下游 input_schema 要 `b`，靠 `input_mapping` 接起來，run 走到 `completed`
   - `mapping_const_and_expr`：常數與字串模板都正確帶入下一個 command 的 payload
   - `mapping_missing_source_fails_run`：來源缺值 → run 是 `failed`、有可讀訊息、下游 command **沒有**被發出（查 `event_log` 確認）
   - `mapping_addresses_specific_step`：兩個步驟產出同名欄位時，`steps.<name>.<field>` 取到的是指定那一步的值（現況做不到的事）
   - 既有的 `checkpoint_parity` 情境必須照舊通過，且**不得修改**——它就是「`_steps` 沒有滲進鏡像」的守門員
6. **`stt_exclusion_notify` 場景端到端跑通**，且 `check` 的 completion payload 只剩三個判斷欄位：
   ```bash
   WORKFLOW_DEF_PATH=workflows/definitions/stt_exclusion_notify.yaml uv run honcho start
   WORKFLOW_DEF_PATH=... uv run honcho -f Procfile.workers start
   uv run python -m orchestrator.trigger --workflow-def workflows/definitions/stt_exclusion_notify.yaml --payload '{"audio_ref": "samples/gen_policy_02.wav"}'
   ```
   `orchestrator_runs` 走到 `completed`、`notified_log` 有實際送出紀錄。
6. **記憶蒸餾沒被打壞**：`memory_writer_distills_episodic` 情境通過（`memory.write` 的 `input_field`/`output_fields` 仍讀得到值）。
7. **評測沒被打壞**：`uv run python -m evals.run_eval --repeats 1` 能跑完（它直接呼叫 `judge_exclusion()`，只讀 `involves_exclusion`，理論上不受影響——但 `_build_result()` 被改，要實際確認）。

### 已定案：`_steps` 不進 checkpoint 鏡像

`_steps` 塞進 `state_payload` 之後，會順著 [orchestrator/master_agent.py](../orchestrator/master_agent.py) 的 `_checkpoint()` 一路進到 [persistence/event_checkpoints.py](../persistence/event_checkpoints.py) 的 `channel_values`。**在傳給 `record_step()` 之前濾掉它。**

不是為了讓 `parity_check` 好過（`_checkpoint_shape()` 比的是 `channel_values` 的 key 集合，多一個 key 就會讓 sync 與 event-driven 不相等），而是因為**它對稽核沒有新增任何資訊**：

- `record_step()` 本來就是每個 transition 寫一列、`metadata.step` 遞增，「哪一步產出什麼」用相鄰 checkpoint 的差集就能還原
- 扁平合併裡已經有全部的值，`_steps` 只是多了「可定址」這個求值方便性
- `event_checkpoints.py` 自己的 docstring 把這張表定位成「派生的、事後的稽核投影」，塞編排層的記帳進去會弄髒那個定位

守門員是既有的 `checkpoint_parity` 情境與 `parity_check`，兩者都**不得修改**——要是為了讓它們通過而去改比較方式，就是把這個決定悄悄推翻了。

---

## P1 — prompt 進 spec

### 範圍

`llm/*.py` 的 `_SYSTEM_PROMPT` 常數搬進 workflow 定義，**system 與 user 兩則訊息分開宣告**（現在 UI 把兩者混成一段，會讓人誤以為 agent 只吃一段自由文字；實際上 system 是靜態指示、user 帶當次輸入）。

記憶注入（`inject_procedural()`）維持自動疊加在 system 之後，不進 YAML。

### 完成標準

1. 三個 agent 的 prompt 常數從 `llm/*.py` 消失，改由 spec 傳入；`llm/*.py` 收到的是已組好的 messages 或 prompt 參數。
2. `{{ input.<field> }}` 佔位符只能引用該 step `input_schema` 宣告過的欄位，**載入時**驗證，引用未宣告欄位丟 `ValueError`。
3. `parity_check` 通過——同一份 prompt 內容下，call_log 形狀與 `mentions_tsmc` 結果不變。
4. 改 YAML 裡的 prompt 文字、重啟 agent server，行為跟著變（手動確認一次，不需要自動化）。

---
# P2 - 4 都跟 issue 33 有關，要 commit 記得連上去
## P2 — `output.from` 宣告化

### 範圍

依序做，每個獨立可驗收：

1. **`from: tool`**（最單純）：`stt` 的逐字稿捕捉抽象成宣告。
2. **`from: tool_log`**：`notified_log` 同上。
3. **`from: model`**（最大條）：改用 LiteLLM `response_format` 的 json_schema，消滅兩個 `_parse_verdict()`。

### 完成標準

1. **先驗證再改**：寫一個一次性腳本確認 `gemini-strong`、`gemini-cheap`、`local-qwen`（ollama_chat）三個模型各自對 `response_format={"type": "json_schema", ...}` 的支援情況，把結果寫進 PR 描述。**任何一個不支援，就必須保留現在的文字解析當 fallback**，不能假設。
2. `from: model` 只取代**解析**，不取代重試迴圈——`exclusion_judge` 的引用驗證重問、`tsmc_judge` 的別名衝突重問都必須原封不動還在。
3. 每一種 `from` 至少一個 smoke test 情境；`stt` 的情境要涵蓋「模型在最後一輪複述了不一樣的逐字稿，輸出仍取自工具回傳值」這個原本的設計意圖。
4. `evals/run_eval.py` 通過率不低於改動前（同樣 `--repeats 3` 的基準）——這是唯一有數字的迴歸防線，structured output 換掉解析路徑後必須確認判斷品質沒退。

### 落地結果與還沒做的部分

三種 `from` 都做完了，但只到**可重用的 code 元件**這一層，不是 §6 畫的 `output: {field: {type, from: ...}}` 那種 YAML 宣告語法：

- `from: tool` → [harness/output_capture.py](../harness/output_capture.py) 的 `ToolResultCapture`
- `from: tool_log` → 同檔案的 `ToolCallLog`
- `from: model` → [gateway/client.py](../gateway/client.py)/[harness/agent_loop.py](../harness/agent_loop.py) 的 `response_format` 參數 + `parse_structured_json()`

也就是說，**哪個 step 用哪種 `from`，現在仍然是寫在 `llm/*.py` 裡的 Python 呼叫**（例如 `llm/stt_agent.py` 建一個 `ToolResultCapture`、`llm/tsmc_judge.py` 傳一個 `_VERDICT_SCHEMA` 當 `response_format`），workflow YAML 裡完全看不到這件事、也沒辦法透過改 YAML 換掉。要把這件事也變成宣告，還要多做兩層：（a）YAML 裡怎麼指名要抓哪個工具的回傳值（`from: "tool:stt__transcribe_audio"` 這種寫法）；（b）`from: model` 的 JSON schema 現在是每個 `llm/*.py` 手刻一份 `_VERDICT_SCHEMA`，理論上可以直接從這個 step 自己的 `output_schema` 自動生成，但沒有人做這個轉換。這是 §6 目標形狀跟現況最大的落差，留給以後。

驗證過程中另外找到並修掉一個不在原計畫裡的真實 bug：`gateway/client.py::chat_with_tools()` 一開始雖然收了 `response_format` 參數，但忘了真的傳給底層的 `chat.completions.create()`，等於 P2 的 structured output 保證整個是空的——被 `/code-review` 抓到，修掉後補了 [gateway/client_smoke_test.py](../gateway/client_smoke_test.py) 守住這個回歸點。

---

## P3 — 單一 runtime process

### 範圍

現在三個 FastAPI process、三個固定 port、principal 寫死在 `make_lifespan("check", principal="check")`。目標是一個 runtime 依 spec 服務多個 agent。

### 完成標準

1. **先釐清 principal 的兩條路徑**（我原本的計畫把這件事講錯了，這裡寫清楚）：
   - **工具 RBAC** 讀的是 `current_node_name` 這個 ContextVar，[agents/envelope.py](../agents/envelope.py) 的 `run_handler()` **每個請求都會重設**——這條路徑本來就支援一個 process 服務多個 agent，不用改。
   - **真正的阻礙**是 `MCPGateway.__init__(principal=...)`：它把 principal 當成 `MCP_CALLING_PRINCIPAL` 環境變數傳給 **stdio 子行程**（只有 [mcp_servers/memory/server.py](../mcp_servers/memory/server.py) 會讀，用來執行記憶的 `can_read()`）。ContextVar 跨不過 process 邊界，所以一個 gateway 實例的記憶 principal 是**整個生命週期固定**的。
   - 因此二選一，PR 要明講選哪個：**(a)** 每個 agent 各自一份 MCPGateway 實例（簡單，但 MCP 子行程數量 × agent 數）；**(b)** 記憶 MCP 改成每次呼叫帶 principal 參數（省資源，但動到權限邊界的傳遞方式，需要更謹慎的驗收）。
2. 記憶權限的負面測試必須通過：某個 agent 讀取它 policy 沒授權的 namespace，仍然 fail-closed 回空並留下 `denied` 的 call_log（[persistence/memory_smoke_test.py](../persistence/memory_smoke_test.py) 已有涵蓋，跑它）。
3. 三個 agent 由同一份 spec 起在同一個 process，`orchestrator.smoke_test` 全數通過。
4. `Procfile` 的 agent 行數從三行變一行。

### 落地結果：選了 (a)

三個 agent 各自一份 `MCPGateway`（[agents/lifespan.py](../agents/lifespan.py) 的 `make_runtime_lifespan()`），principal 在建構時各自固定成 step 名。選這個不是因為比較好，是因為比較不冒險：**(b)** 要動記憶 MCP 工具的呼叫簽章跟 `MCPGateway.call_tool()` 的注入邏輯，等於改權限邊界的傳遞方式；**(a)** 只動 `agents/lifespan.py` 的組裝方式，完全不碰 `mcp_servers/memory/server.py`。代價記在下面「§7 P3 順帶可以補上的稽核缺口」——選 (a) 就是那個缺口沒有被順便補上，繼續留著。

MCP 子行程總數沒有變少（一樣 15 個：3 個 agent × 5 個 server，因為每份 gateway 都連所有 policy.yaml 裡宣告的 server，不是只連自己會用到的），P3 縮的是 process/port 數，不是子行程數。

---

## P4 — UI 依掛載的 MCP 工具建議 schema

### 完成標準

1. 掛上一個 MCP server 後，UI 能列出它每個工具的 input/output schema，並提供「加進這個 agent 的輸入／輸出」的一鍵動作。
2. **建議 ≠ 決定**：使用者可以刪掉建議的欄位、也可以新增工具 schema 裡沒有的欄位（`check` 的 `transcript`/`involves_exclusion` 就是這種欄位，必須能手動宣告，見 §5）。
3. 對應層與 schema 建議在 UI 上是同一個畫面：改了輸入欄位就要能立刻指定它的來源。

### 落地結果：只做到互動原型，還沒接後端

> **後續狀態更新**：以下記錄的是當時的歷史階段。現在 repository 內已有
> [demo/index.html](../demo/index.html) 與 [demo/api.py](../demo/api.py) 的正式可追蹤實作；
> 目前整合邊界見 [ui-backend-integration-plan.md](ui-backend-integration-plan.md)。外部 Claude
> artifact 只保留為設計來源，不再是現行實作或唯一可用介面。

三條完成標準都在一個 Claude artifact——[Agent 平台 Demo 介面原型](https://claude.ai/code/artifact/8049a95b-09da-4fa9-b6f4-c42a1f8e5e29?org=d32aa613-84a7-4f98-b2da-e857e8831578)（純前端 HTML/CSS/JS，掛在使用者自己的 claude.ai/code 帳號底下，不在這個 repo 裡，畫面右上角自己標了「原型 · 資料全為假」）——驗證過設計可不可行：每個 agent 卡片右側的「輸入/輸出」分頁最上面有「建議欄位（依掛載工具）」區塊，只列目前開啟的工具，每個建議欄位都有「+ 加進輸入／輸出」按鈕；每個既有欄位旁邊有來源標籤（工具建議 vs 手動宣告）跟移除鈕；建議區塊跟輸入來源對應在同一個畫面，加了欄位馬上能選來源。刻意留了幾個「建議了但沒被採用」的例子（例如 `browse_semantic_memory` 的 `scope` 沒被加進 `check` 的輸入），示範「建議 ≠ 決定」這件事。

**這是設計驗證，不是可以上線的功能**，跟真正落地還差這幾層：

- 工具 schema 是手刻的假資料（`TOOL_SCHEMAS` 常數），不是真的呼叫 [mcp_servers/gateway.py](../mcp_servers/gateway.py) 的 `MCPGateway.list_openai_tools()` 拿回來的。
- 沒有接任何後端 API——不會讀取、也不會寫回真正的 [workflows/definitions/*.yaml](../workflows/definitions/)，改動只存在瀏覽器的記憶體裡，重新整理就消失。
- 要接上真的東西，至少需要：一個列出目前掛載了哪些 MCP server／工具的 endpoint（`mcp_servers/policy.yaml` 已經有這份資料，缺一個 HTTP 出口）、一個讀寫 `WorkflowDef` 的 endpoint（`orchestrator/workflow_def.py` 已經有 `load_workflow_def()`，缺寫回 YAML 檔案這一半，且要重用 P0/P1 已經做的「載入時驗證」邏輯，不能繞過去）。

---

## 7. 資料庫層面的影響

**全部階段做完都不需要 DDL**——不新增表、不加欄位、不改 CHECK constraint。所有變化都在既有 jsonb 欄位的內容裡。

| 表 | 影響 | 內容 |
|---|---|---|
| `orchestrator_runs` | 內容 | `state_payload` 多一個 `_steps` key（P0）。扁平合併原封不動保留，兩者並存 |
| `checkpoints` / `checkpoint_blobs` | **無** | `_steps` 在寫入前濾掉，`channel_values` 的 key 集合與現在完全一致 |
| `call_log` | 內容 | `request`/`response` 的內容會變（prompt 改由 spec 提供、structured output 多 `response_format`），欄位與 kind 不變 |
| `event_log` / `event_dispatch` | 內容 | command payload 改由 mapping 產生；topic 與 consumer_group 命名（`{workflow}.{step}`）不變 |
| `store`（長期記憶） | **無** | `memory_write` 規則仍讀扁平 state，namespace 結構不動 |

`_steps` 與扁平合併會有值的重複（同一份 transcript 存兩次）。這是為了向後相容刻意付的代價——`memory_writer` 的 `input_field`、`parity_check` 的 `ed_payload["transcript"]`、checkpoint 鏡像三處都讀扁平結構，改動它們的連鎖成本遠高於這點儲存空間。

### P3 順帶可以補上的稽核缺口

現在**從 MCP 子行程發出的 memory call_log 全部是 `thread_id = NULL`**（[persistence/memory.py](../persistence/memory.py) 的 `_log_memory_call()` docstring 有記：`current_thread_id` 是 ContextVar，跨不過 stdio 邊界；`node` 欄位還在，只有 thread_id 的關聯斷了）。也就是說**無法從一次 run 追出它讀過哪些記憶**。

P3 若選 (b)「記憶 MCP 改成每次呼叫帶 principal」，同一條路上順便把 thread_id 帶過去就能補上。**P3 實際選了 (a)（見上面 P3 的「落地結果」），這個缺口沒有被順便補上，現在仍然存在**——從 MCP 子行程發出的 memory call_log 還是查不出屬於哪次 run。要補的話得回頭做 (b) 那條路，不是 P3 剩下的工作，是一個獨立的、目前沒有排期的技術債。

### 未來唯一可能的 DDL

[issue #34](https://github.com/donydony228/agent-architecture/issues/34) 的熱載入若真的要做，需要把 spec 版本綁進 `orchestrator_runs`（新欄位），才能決定「跑到一半的 run 用哪個版本的定義」。現在不做，也**不預留欄位**——沒有實際需求前先開欄位，只會變成沒人知道該不該填的東西。

## 8. 刻意不配置化的部分

以下不進 YAML，也不讓使用者自己寫：

- `exclusion_judge` 的引用驗證迴圈：模型引用了它沒真的 browse 過的條號 → 把證據丟回去要它重答 → 再失敗就 `needs_review`
- `tsmc_judge` 的確定性別名 backstop 與衝突重問
- `StallGuard` 的卡住偵測

**這些不是還沒重構完的遺跡，是這個專案真正的工程價值**，也正是 [docs/harness-engineering-principles.md](harness-engineering-principles.md) 在講的：agent 需要的是回饋迴路，不是更完美的提示。硬塞進 DSL 只會得到沒人看得懂、也沒人寫得對的設定語言。

做法是留掛載點：spec 宣告 `verifiers: [名字]`，工程師實作、使用者只勾選，並在 UI 上看得到「這個 agent 掛了驗證器」。

## 9. 判斷：該做嗎

**該做，但驅動力不是優雅，是第二個場景的成本。**

現在加一個新場景要開 process、寫膠水、改 policy、加一個 `llm/xxx.py`。變成 spec 之後應該是「填一份 YAML」。這個差別才是平台跟三支服務的分界。

但**不要追求 100% 配置化**。目標是把薄的那層（模型／工具／記憶／prompt／欄位對應／輸出組裝）完全宣告化，剩下的驗證器與 backstop 留在程式碼裡當可掛載元件。全部塞進設定檔，是把可讀的 Python 換成不可讀的 DSL，負向重構。

## 10. P0–P4 做完之後，跟 §6 目標形狀還差多少

P0–P4 全部做完、都已經 commit。對照 §6 那份目標 YAML，逐欄位盤點現在真的到哪裡：

| §6 的欄位 | 現況 | 在哪裡 |
|---|---|---|
| `input: {field: {type, from}}` | **做完**，語法是 `input_mapping`（跟目標形狀不同名，但語意等價：`from`/`const`/`expr` 三種來源都有） | workflow YAML 的 `input_mapping`（P0） |
| `prompt: {system, user}` | **做完** | workflow YAML 的 `prompt`（P1） |
| `output: {field: {type, from}}` | **YAML 語法做完**（`StepDef.output`，載入時驗證），兩個 workflow 的每個 step 都已宣告。**執行面只有 `stt` 真的走這條路**（`harness/generic_agent.py::run_generic_step()`）——`check`/`notified` 有 §8 等級的驗證迴圈，宣告只當文件用，不驅動執行，見 P6「落地結果」 | workflow YAML 的 `output`、`harness/generic_agent.py`（P6） |
| `model:` | **做完** | workflow YAML 的 `model`（P5） |
| `tools:` | **沒動，定案不搬**（見 §11 P5 的「tools/memory 決定」） | `mcp_servers/policy.yaml` 的 `principals` |
| `memory:` | **沒動，定案不搬**，同上 | `mcp_servers/policy.yaml` 的 `memory:` |
| `verifiers:` | **刻意不做**（§8 已說明原因），連掛載點都還沒留 | 無 |
| （不在 §6 裡，但同一條路上）三個 process → 一個 runtime | **做完**，但選 (a) 方案留了一個已知稽核缺口（見 §7、P3「落地結果」） | `agents/runtime.py`（P3） |
| （不在 §6 裡）UI 建議 schema | **只做到互動原型**，沒有接後端、沒有讀寫真正的 workflow YAML | 見 P4「落地結果」 |

**還沒動的三格（`model`／`tools`／`memory`）不是漏做，是 P0–P4 本來就沒把它們排進來**——§3 那張「已經是設定、不用動的」表格把它們算作已經定案，理由是「本來就是一個字串／一份 policy 檔」；但嚴格對照 §6 的目標形狀，它們仍然不在 workflow 的 step YAML 裡。這個決定現在補上了（2026-08-17 定案，見 §11）：`model` 搬進 workflow YAML（P5）；`tools`／`memory` **不搬**，繼續留在 `mcp_servers/policy.yaml`，理由見 §11 P5。

---

## 11. 下一階段規劃（P5–P7）

P0–P4 做完之後又發現兩件事：(1) `output.from` 只做到 code 元件、還沒變成 YAML 語法，而且就算補了語法，`agents/runtime.py`（三個手寫路由各自 import 一個 `llm/*.py` 函式）和 `workflows/event_driven_pipeline.py::build_step_handlers()`（同樣手寫每個 step 的 handler）**依然是寫死的**——單做語法沒有兌現「新增 agent 不用寫 Python」這件事；(2) P3 選 (a) 留下的 memory call_log 稽核缺口，其實不需要重做 P3 的決定，是一個獨立的小修法（見 P7）。

### 目標：demo 時能不能零程式碼建立一個新 agent

盤點過 `agents/runtime.py`、`agents/envelope.py`、`workflows/event_driven_pipeline.py` 之後的結論：**現在不行，P0–P4 做完仍然不行**。`run_handler()`（`agents/envelope.py`）本身是通用的，`make_runtime_lifespan()`（`agents/lifespan.py`）也已經是依 `step_names` 迴圈建 `MCPGateway`，但這兩個通用元件外面包的東西不是：

- `agents/runtime.py` 一個 agent 一個手寫的 `@app.post("/xxx/run")` route，各自 `import` 一個具名的 `llm/xxx_agent.py` 函式並手刻怎麼呼叫它
- `workflows/event_driven_pipeline.py::build_step_handlers()` 一個 agent 一個手寫的 handler 閉包，轉呼叫 `agents/xxx/client.py` 的具名函式（`transcribe(audio_ref)` 這種帶簽名的呼叫，不是通用的 `run(input) -> output`）

所以想像中的「建一個算數 agent，只要有 calculator MCP 工具就好」，現在還是要：寫一個 `llm/calc_agent.py`、在 `agents/runtime.py` 加一個 route、在 `build_step_handlers()` 加一個分支、`policy.yaml` 加一個 principal、workflow YAML 加一個 step——六件事只少了「不用開新 process」這一件（P3 的成果），其他五件基本沒變。**P6 存在的理由就是把這件事的答案改成「可以」，至少對薄 agent（沒有 §8 那種自訂驗證迴圈）成立。**

### P5 — `model:` 進 workflow YAML

#### 範圍

- `StepDef` 加 `model: str`。
- `load_workflow_def()` 載入時驗證：`model` 必須是 [gateway/config.yaml](../gateway/config.yaml) `model_list` 裡真的存在的 `model_name`——跟 `input_mapping` 同一個「載入時擋錯，不要執行時才炸」精神。
- 四個 `llm/*.py`（[stt_agent.py](../llm/stt_agent.py)、[tsmc_judge.py](../llm/tsmc_judge.py)、[exclusion_judge.py](../llm/exclusion_judge.py)、[notify_agent.py](../llm/notify_agent.py)）的 `MODEL_NAME` 模組常數刪掉，改吃呼叫方傳入的參數；[agents/runtime.py](../agents/runtime.py) 的三個 `_handler` 從 `ctx.step.model` 讀出後傳進去。

#### tools/memory 決定（定案，不搬）

`tools`／`memory` **不**搬進 workflow step YAML，繼續留在 [mcp_servers/policy.yaml](../mcp_servers/policy.yaml)。理由：

1. `policy.yaml` 的 `principal` 是刻意設計成**跨 workflow 複用的身分**（見該檔第 52–59 行的註解：同名的 step 在不同 workflow 裡是同一個判斷任務，episodic/procedural 記憶掛在 principal 上、不掛在 workflow 上）。把工具/記憶授權寫進 workflow step YAML 會拆掉這個假設——要嘛每個 workflow 重複宣告同一份授權（兩份事實來源，會漂移），要嘛授權變成 per-workflow，這是動到身分模型的決定，不是搬個欄位而已。
2. `policy.yaml` 檔頭本來就寫著「Future no-code UI edits this file」——UI 直接編輯的目標本來就打算落在這個檔案上，缺的只是 P4 落地結果點名的「一個讀寫 endpoint」，不是缺欄位。

UI 上要「同一張 agent 卡片看到 model/tools/memory」不代表資料要住同一個檔案：可以分頁顯示，一個分頁讀寫 workflow YAML 的 `model`，另一個分頁讀寫 `policy.yaml` 的 `tools`/`memory`，兩條 load path 各自獨立。

#### 完成標準

1. 兩個現有 workflow YAML 補上各 step 的 `model:`（照各 `llm/*.py` 目前的既有值填），行為不變。
2. `model:` 打錯字（`gateway/config.yaml` 沒有的 model_name）在 `load_workflow_def()` 時就丟 `ValueError`，訊息指名是哪個 step。
3. `orchestrator.smoke_test`、`workflows.parity_check` 全過。
4. 改 YAML 的 `model:`、重啟 `agents.runtime`，行為跟著換模型（手動確認一次）。

---

### P6 — `output.from` 宣告化 + 通用 runtime dispatch

這兩件事綁在一起做，理由見上面「目標」小節：只做 YAML 語法不做通用 dispatch，新增 agent 還是要碰 `agents/runtime.py` 和 `build_step_handlers()`，宣告化就沒有兌現價值。

#### 範圍

1. **`output: {field: {from}}` YAML 語法**，三種 `from`（`tool:<suffix>`、`tool_log`、`model`）對應 P2 已經做出來的三個底層元件（[harness/output_capture.py](../harness/output_capture.py) 的 `ToolResultCapture`/`ToolCallLog`、`gateway/client.py`/`harness/agent_loop.py` 的 `response_format` + `parse_structured_json()`）。`StepDef` 加 `output` 欄位，`load_workflow_def()` 載入時驗證：`output_schema` 每個欄位都要有一種 `from`；`from: "tool:X"` 語法要對。
2. **一個通用的「依 spec 跑一個 agent」函式**（例如 `harness/generic_agent.py::run_generic_step()`）：用 P1 的 `render_prompt()` 組 prompt、用 P5 的 `step.model` 跑 `run_tool_calling_loop()`、用新的 `step.output` 組裝結果。
3. **`agents/runtime.py` 收斂**：三個手寫路由改成對 `step_names` 迴圈註冊 `POST /{name}/run`；`_handler` 預設呼叫 `run_generic_step()`。§8 那些刻意留在程式碼裡的驗證迴圈（`exclusion_judge` 的引用重問、`tsmc_judge` 的 alias backstop、`StallGuard`）**不會被這次重構吃掉**——哪個 step 需要專屬驗證迴圈，`runtime.py` 才 import 對應的 `llm/xxx_agent.py`，當成通用路徑的逃生口，不是預設路徑。也就是說：**薄 agent 全走通用路徑，厚 agent 才需要寫 Python。**
4. **`workflows/event_driven_pipeline.py::build_step_handlers()` 同步收斂**：`agents/<name>/client.py` 的具名函式（`transcribe(audio_ref)` 這種）改成通用的 `run(input: dict) -> dict`，通用 handler 直接轉發；TSMC 場景那段「`should_notify = mentions_tsmc`」的推導邏輯繼續當明確例外留著，不強行塞進 DSL。

#### 完成標準

1. `stt`（`from: tool`）先改走通用路徑，`orchestrator.smoke_test`/`parity_check` 全過、行為不變。
2. `notified`（`tool_log`）比照通過。
3. `check` 的 TSMC 判斷分支（純 `from: model`，沒有驗證迴圈）改走通用路徑；`exclusion_judge`（有引用驗證重問）保留專屬 `llm/exclusion_judge.py`，作為「厚 agent 逃生口」的實例。
4. **新增一個 smoke test，直接拿使用者想要的 demo 場景當驗收**：定義一個全新的算術 agent（一個 calculator MCP 工具 + `from: model` 輸出），只寫 workflow YAML 一個 step + `policy.yaml` 加一行 principal，**不寫任何 `llm/*.py`／不碰 `agents/runtime.py`／不碰 `build_step_handlers()`**，run 要能跑到 `completed`。這是「零程式碼新增 agent」唯一有意義的驗收方式。
5. `evals/run_eval.py --repeats 1` 通過率不低於改動前。

#### 落地結果：實際改動範圍比原計畫小，原因是重讀 code 後發現原計畫的角色分類錯了

實作前重讀了 `llm/tsmc_judge.py`/`llm/notify_agent.py` 才發現，上面「範圍」寫的角色分類不準——**現有三個真實 agent 裡，只有 `stt` 是真的薄**：

- **`llm/tsmc_judge.py`（`check`/TSMC 分支）不是「純 `from: model`，沒有驗證迴圈」**——它有確定性別名查找（`_lookup_tsmc_aliases`）跟一個條件重問迴圈（backstop 命中但模型判否時，補一輪証據重問），跟 `exclusion_judge` 只差在沒有「引用檢查」，本質上是同一種厚 agent。原計畫寫「check 的 TSMC 判斷分支改走通用路徑」是誤判，實際沒有改。
- **`llm/notify_agent.py`（`notified`）也不是無條件可通用化**——`decide_and_notify()` 的 `_finish()` 會做一個「`should_notify=true` 但沒有任何工具呼叫真的成功送出」的事後檢查，這本身就是 §8 講的驗證器（post-hoc verification），只是沒被 §8 點名。硬塞進通用路徑等於把這個檢查拿掉，是真正的品質退步，不是重構。

**因此只有 `stt` 真的改走通用路徑**（`agents/runtime.py` 的 `/stt/run` 現在呼叫 `harness/generic_agent.py::run_generic_step()`，不再呼叫 `llm/stt_agent.py::transcribe()`）；`check`（兩個分支）跟 `notified` 都留在 `agents/runtime.py` 的 `_CUSTOM_HANDLER_FACTORIES` 裡，繼續呼叫各自的 `llm/*.py`，行為完全不變。`llm/stt_agent.py::transcribe()` 本身沒刪也沒改——`workflows/simple_pipeline.py`（凍結檔）還在直接呼叫它，事件驅動路徑（`agents/runtime.py`）跟同步路徑（`simple_pipeline.py`）現在各自獨立跑，這是刻意的，不是遺漏。

**`workflows/event_driven_pipeline.py::build_step_handlers()` 完全沒改，也不需要改**——原計畫第 4 點高估了它跟 `agents/runtime.py` 的耦合。`build_step_handlers()` 只是轉呼叫 `agents/<name>/client.py` 的 HTTP client，client 跟 `/stt/run` 之間隔著一個完全不透明的 HTTP 合約（`agents/stt/client.py` 的 docstring就寫著它只是把 envelope 轉譯回舊呼叫慣例）——`/stt/run` 內部從呼叫 `transcribe()` 換成呼叫 `run_generic_step()`，對呼叫端零可見，所以 `build_step_handlers()` 一行都不用動。

**`agents/runtime.py` 的收斂做法跟原計畫一致，但更進一步**：三個手寫 route 收斂成對 `step_names` 迴圈用 `app.add_api_route()` 動態註冊；`step_names` 本身也不再是寫死的 Python list，改成從 `load_workflow_def(resolve_workflow_def_path()).steps` 讀出來。`_CUSTOM_HANDLER_FACTORIES: dict[str, ...]` 是「哪些 step 需要逃生口」的唯一登記處——`check`/`notified` 在裡面，其他任何 step 名字（包括未來新增的）自動走 `run_generic_step()`，不用改這個檔案。

**零程式碼新增 agent 的驗收**：[harness/generic_agent_smoke_test.py](../harness/generic_agent_smoke_test.py) 的 `scenario_new_agent_from_spec`——一個從沒出現在任何 `workflows/definitions/*.yaml` 裡的全新 `StepDef`（直接在 Python 建構，跟 `orchestrator/smoke_test.py` 的 `scenario_mapping_*` 系列同一個慣例，理由是 `load_workflow_def()` 自己的 YAML 解析已經被 P0–P6 的其他測試覆蓋過，不用再繞一次臨時檔案），搭配一個 ad-hoc `Policy`（不動真正的 `mcp_servers/policy.yaml`，同 `persistence/memory_smoke_test.py` 的 `_BROWSE_POLICY` 慣例）、一個既有的 `lookup__query_company_profile` 工具（沒有另外建算術 MCP server，理由見下）、`output: {official_name: {from: model}}`。跑到 `chat_with_tools()` 呼叫 LiteLLM Gateway 那一步之前的每一段都已用真實元件驗證過（`MCPGateway` 真的連上 `lookup` 子行程、`run_tool_calling_loop` 真的組出訊息並帶著工具清單）——卡在 `litellm`/`ollama` 沒啟動（跟 P5/P7 一樣的環境限制），還沒真的跑出一個模型回覆。

沒有另外建一個 calculator MCP server：使用者原本說的例子（算術 agent）需要的只是「至少一個真的 MCP 工具」這件事本身，跟工具語意（算術 vs 查公司資料）無關——`run_generic_step()` 不會因為工具是算術還是查詢而有任何不同行為。建一個新 MCP server 是額外、不相關的工程量，跟這次要驗收的東西（通用 dispatch 機制本身）沒有因果關係，所以借用了已經存在的 `lookup` 工具。

**已知、刻意的行為差異（未經真實環境驗證）**：`llm/stt_agent.py` 原本手刻的迴圈，在「模型還沒呼叫轉錄工具就先回一段純文字」時會補一句提示（「尚未取得逐字稿，請呼叫 transcribe_audio 完成轉錄」）再繼續等，`run_generic_step()` 沒有這個行為——`from: "tool:X"` 欄位如果在模型停止呼叫工具時仍未捕捉到值，直接 `raise AgentLoopIncomplete`（進 `needs_review`），不會多補一輪提示。理由寫在 `harness/generic_agent.py` 的 `# ponytail:` 註解裡：一個真正通用的 runner 沒有「這個場景該講什麼提示語」的知識，`needs_review` 是平台既有設計好的兜底路徑，不是新增的失敗模式。但這確實是跟 `stt` 舊行為的一個真實差異，**還沒有用真實 stack 跑過 `orchestrator.smoke_test` 驗證會不會影響 happy path 的成功率**——是這次 P6 改動裡風險相對最高的一塊，優先順序上應該排在正式合併前第一個要用真實 stack 驗證的項目。

---

### P7 — 稽核缺口：memory call_log 的 `thread_id` 補齊

不是重做 P3 的決定（不用把 P3 從方案 (a) 改成方案 (b)），是一個獨立、小很多的修法——查過 code 確認 `recall()`/`browse()`（[persistence/memory.py](../persistence/memory.py)）目前沒有 `thread_id` 參數，只在內部讀 `current_thread_id` 這個 ContextVar；`MCPGateway.call_tool()`（[mcp_servers/gateway.py](../mcp_servers/gateway.py)）每次呼叫本來就會讀 `current_node_name.get()`，同樣的地方可以順手讀 `current_thread_id.get()`。

#### 範圍

1. `MCPGateway.call_tool()`：呼叫 `memory__*` 工具時，把 `current_thread_id.get()` 塞進送給 stdio 子行程的 `arguments` dict 裡（保留鍵名，例如 `thread_id`）。
2. [mcp_servers/memory/server.py](../mcp_servers/memory/server.py) 的 `recall_semantic_memory`/`browse_semantic_memory` 加一個 `thread_id: str | None = None` 參數，往下傳給 `persistence/memory.py` 的 `recall()`/`browse()`（兩者各加一個 `thread_id` 參數，預設 `None` 時退回讀 ContextVar，維持 in-process 呼叫者零改動）。
3. **`MCPGateway.list_openai_tools()` 加一條過濾**：把這個保留參數名從回給模型的 JSON schema 裡拿掉——FastMCP 的 schema 是逐字從函式簽名生成（`mcp_servers/base_client.py` 沒有欄位過濾這層），不擋的話模型會看到一個它不該填、填了也會被蓋掉的欄位。

#### 完成標準

1. [persistence/memory_smoke_test.py](../persistence/memory_smoke_test.py) 新增情境：透過 `agents.runtime` 呼叫一次 `check`/`notified` 的記憶工具，查 `call_log` 該筆記錄的 `thread_id` **不是 NULL**。
2. 既有的 fail-closed 負面測試不受影響。
3. 新增一個 assert：`list_openai_tools()` 回給模型的 `memory__recall_semantic_memory`/`memory__browse_semantic_memory` schema 裡**不含**這個保留參數名。

---

### 執行順序與 P4 的關係

建議 **P5 → P7 → P6**：P5 小且獨立、P6 依賴它（通用 dispatch 要讀 `step.model`）；P7 完全獨立、風險低，可以插在中間先做掉。**三者都已照這個順序完成並 commit**（P5/P7/P6 各自的「落地結果」小節記錄了實際範圍與跟原計畫的差異）——**但都還沒用真實 stack（ollama + litellm + honcho）跑過 `orchestrator.smoke_test`/`workflows.parity_check`/`persistence.memory_smoke_test`**，目前只做到靜態驗證（載入時驗證、單元層級檢查、`harness/generic_agent_smoke_test.py` 跑到打真實 LLM 之前的每一步）。正式視為完成前，這是下一步要做的事，`stt` 遷移到通用路徑那個已知行為差異（見 P6「落地結果」）優先權最高。

P4 當時先以 [Agent 平台 Demo 介面原型](https://claude.ai/code/artifact/8049a95b-09da-4fa9-b6f4-c42a1f8e5e29?org=d32aa613-84a7-4f98-b2da-e857e8831578) 驗證三條完成標準；該連結現在只作歷史來源。後續後端串接已落在 repository 的 [demo/index.html](../demo/index.html)、[demo/api.py](../demo/api.py)、[demo/spec_writer.py](../demo/spec_writer.py) 與 [agents/live_spec.py](../agents/live_spec.py)，現行契約由 [ui-backend-integration-plan.md](ui-backend-integration-plan.md) 接手。
