# 知識蒸餾與品質關卡（M5）實作評估

## 0. 這份文件在回答什麼

[docs/long-term-memory-plan.md](long-term-memory-plan.md) M3 落地之後，寫入端只做到「每次成功執行 → 逐筆存一則 episodic」，而且是純機械式欄位擷取（[orchestrator/memory_writer.py](../orchestrator/memory_writer.py) 的 `_apply_rule()`）。真正想要的閉環是：

> 累積的 episodic 經驗 → LLM 歸納出原則性的規則 → **人審核** → 成為 procedural 記憶 → 強制注入 agent

這段目前**完全不存在**。本文評估要做這件事需要動哪些東西、分幾步、以及有哪些問題必須先決定。

同時把 [long-term-memory-plan.md](long-term-memory-plan.md) M5「品質關卡」那節（原文只有四個 bullet）展開成可執行的步驟——這兩件事寫在同一份文件，是因為它們**共用同一個 `status` 機制、而且順序不能顛倒**（見 §2）：蒸餾負責「生出候選規則」，關卡負責「候選規則能不能生效」，缺任何一半，另一半都不該上線。

> **這份文件是評估，不是已定案的實作範圍。** 底下每一節的「待確定」都是真的還沒決定，不是留白待補。

---

## 1. 現況盤點：三個事實決定了改動範圍

### 1.1 procedural 沒有任何自動來源

`MemoryWriteRule`（[orchestrator/workflow_def.py:47-61](../orchestrator/workflow_def.py#L47-L61)）刻意連 `kind` 欄位都沒有——它永遠寫 `EPISODIC`，因為 `input_field`/`output_fields` 這組形狀只對 episodic 的 `{"input", "output"}` 標準 schema 成立。procedural（`{"rule": str}`）需要的是「歸納」，不是「複製某個已知欄位的值」，所以現在 procedural 記憶唯一的產生方式是**人手動呼叫 `remember()`**（實測 DB 裡目前就只有 1 筆，見 §1.3）。這一點 [TODO.md](../TODO.md#memory-writer-llm-judgment) 已經記錄為已知缺口。

### 1.2 讀取端已經接好了，procedural 一寫進去就立刻生效

`inject_procedural()`（[persistence/memory_prompt.py:42-62](../persistence/memory_prompt.py#L42-L62)）已經被 `llm/tsmc_judge.py`、`llm/stt_agent.py`、`mcp_servers/notified/agent.py` 三個 agent 呼叫，撈到什麼就直接接在 system prompt 後面。`recall()`（[persistence/memory.py:98-125](../persistence/memory.py#L98-L125)）**沒有任何 status 過濾**。

這是整件事最危險的一點，必須先講：**現在的系統，任何寫進 procedural namespace 的東西都會立刻改變三個 agent 的 system prompt，沒有任何關卡。** 所以「先做蒸餾、關卡之後再補」等於讓 LLM 自動改寫自己的 system prompt 且無人把關——這正是 [long-term-memory-plan.md](long-term-memory-plan.md) §5 風險 1（記憶污染 / Goodhart）跟 harness 原則第 7 篇「沒有測試把關的 skill 只是把幻覺寫進記憶」講的東西。

### 1.3 既有記憶完全沒有 `status` 欄位——加 filter 會讓它們全部消失

`remember()` 寫進 `value` 的固定欄位是 `content` / `source_thread_id` / `source_step` / `created_by` / `confidence`（[persistence/memory.py:273-279](../persistence/memory.py#L273-L279)），沒有 `status`。本機 DB 目前的存量：

```
 kind       | count
------------+-------
 semantic   |    60      -- 多數是 scripts/seed_insurance_memory.py 種進去的保單知識樹
 episodic   |     9      -- memory_writer 蒸餾的 check 案例
 procedural |     1      -- 手動塞的測試規則
```

實際翻過 `langgraph.store.postgres.base` 的 `_get_filter_condition()`（第三方套件，`.venv` 內）確認：`filter=` 產生的 SQL 是 `value->'status' = '"active"'::jsonb`，只比對 **`value` 的頂層欄位**（不是 `content` 裡面），而且 `$ne` 也是 `value->'status' != ...`。對一個**根本沒有 `status` 這個 key** 的 row，`value->'status'` 是 SQL NULL，`NULL = x` 和 `NULL != x` 都是 NULL → 兩種寫法都會把它濾掉。

**結論**：`recall()` 一旦預設帶 status filter，這 70 筆既有記憶全部瞬間讀不到（agent 不會報錯，只會安靜地退化成沒有記憶）。**backfill 腳本是這件事的硬前置，不是 nice-to-have**。

---

## 2. 蒸餾與關卡是兩件事，順序不能顛倒

| | **知識蒸餾** | **品質關卡（M5）** |
|---|---|---|
| 在做什麼 | 生成：N 則 episodic → M 條 procedural 規則 | 篩選：一則已存在的記憶能不能被任何讀取端（`recall()` / `browse()`）看到 |
| 輸入 | 一批既有記憶 | 一則候選記憶 + 評測集 |
| 輸出 | 新的記憶內容（不存在過的東西） | 一個布林決定（`pending` → `active` 或打回） |
| 要不要 LLM | 要（歸納本身就是模型的工作） | 不一定（結構化輸出用精確比對就夠，見 §4.4） |
| 沒有它會怎樣 | `pending` 佇列永遠是空的，關卡無事可審 | LLM 歸納出的錯規則直接改三個 agent 的 prompt |

**做的順序建議：關卡的地基（§4 的 A/B/C）先於蒸餾（§3）。** 理由不是穩健性潔癖，是很實際的：蒸餾產出的候選規則需要一個地方讓人判斷「這條規則到底有沒有用」，那個地方就是評測集 + 執行器。沒有它，人審核只能靠讀規則文字憑感覺點頭，那不是審核，是簽字。

---

## 3. 蒸餾器：具體設計與改動範圍

### 3.1 為什麼不能塞進 `memory_writer`

[orchestrator/memory_writer.py](../orchestrator/memory_writer.py) 是 per-event 的事件驅動 consumer，每收到一個完成事件跑一次。蒸餾的形狀相反：**要看一整批 episodic 才能歸納**，而且要呼叫 LLM。塞進去等於每次成功執行都跑一次昂貴的歸納（而且輸入只多了一筆，結論八成跟上次一樣）。

→ 蒸餾器是**獨立的批次工作**，不是事件驅動 consumer。

### 3.2 觸發方式（建議先做最笨的）

| 方式 | 評估 |
|---|---|
| **手動 CLI（建議）** | `uv run python -m scripts.distill_procedural --scope stt_exclusion_notify/check`（demo 主線改保單除外責任場景後的 scope，見 §4.1）。產出必須等人審核才會生效，所以自動觸發**一點都沒省到事**——人還是得回來看。先做這個 |
| 計數觸發（每 N 筆新 episodic 跑一次） | 等 CLI 版本跑過幾輪、確認歸納品質穩定再說 |
| 定期排程 | 同上，而且平台目前沒有排程基礎設施，會是額外的新東西 |

### 3.3 流程步驟

1. **讀既有 episodic**：`recall(EPISODIC, tenant, scope, limit=N)`。注意目前排序是 `updated_at DESC`（[TODO.md](../TODO.md#memory-procedural-episodic-vector-search)），所以 N 不夠大時拿到的是「最近 N 筆」不是「最有代表性 N 筆」。
2. **也要讀既有 procedural**：這步很容易漏掉但很關鍵——不把現有規則餵給模型，它會反覆生成語意重複、甚至互相矛盾的規則，procedural 清單會迅速膨脹成垃圾。
3. **一次 LLM 呼叫**：走 [gateway/client.py](../gateway/client.py) 的 `chat_json()`（已經有 `response_format=json_object` 跟 `call_log` 記錄，不用新寫）。輸出約定成一個候選規則清單，每條至少要有：
   - `rule`：規則文字本身（要直接能接進 system prompt，跟 `{"rule": str}` 的 schema 一致）
   - `evidence`：這條規則是從哪幾則 episodic 歸納出來的（存那些 item 的 `key`）
   - `rationale`：給人審核時看的理由，**不進 `content`**（不該被注入 prompt，只是審核用的元資料）
4. **寫入 procedural，`status="pending"`**：走既有的 `remember()`，`created_by` 會自動記成 principal（見 §3.4 對 `remember()` 的改動）。
5. **人審核 CLI**：列出所有 `pending` → 顯示規則 + rationale + evidence（以及 §4 評測跑出來的數字）→ approve 就把 `status` 改成 `active`。

### 3.4 檔案級改動範圍

| 檔案 | 異動 | 內容 |
|---|---|---|
| [persistence/memory.py](../persistence/memory.py) | 修改 | `remember()` 新增 `status: str = "active"` 參數，寫進 `value` 頂層（**不是 `content` 裡**——見 §1.3，filter 只比對頂層）。預設 `"active"` 讓既有所有呼叫端（`memory_writer`、`seed_insurance_memory.py`、smoke test）行為不變 |
| [persistence/memory.py](../persistence/memory.py) | 修改 | `recall()` **和 `browse()`** 都預設只讀 `active`（理由見 §5 P0 的說明——`browse()` 的 `items` 回的是完整 `content`，不是摘要）。**這是 breaking change**，要配 backfill（下一列） |
| `scripts/backfill_memory_status.py` | **新增** | 把既有 70 筆記憶補上 `value.status = "active"`。做法：`alist_namespaces()` + `asearch()` 掃出來，逐筆 `aput()` 回去。**必須在 `recall()` 的 filter 上線前跑** |
| `scripts/distill_procedural.py` | **新增** | §3.3 的 1~4 步。比照 [scripts/seed_insurance_memory.py](../scripts/seed_insurance_memory.py) 的形狀（自己開 store/policy、設 `current_node_name`、`asyncio.run`） |
| `scripts/review_memory.py` | **新增** | §3.3 第 5 步的人審核 CLI：列 pending、approve/reject。reject 的處理方式未定（§6） |
| [mcp_servers/policy.yaml](../mcp_servers/policy.yaml) | 修改 | 新增 principal（`distiller` 讀 episodic+procedural／寫 procedural；審核者寫 procedural）。⚠️ **這會動到那個「還沒跟主管定案」的 `memory:` 區塊**，見 [TODO.md](../TODO.md#memory-policy-pending)——不要順手改，要先確認 |
| [gateway/config.yaml](../gateway/config.yaml) | 可能修改 | 歸納用哪個 model。建議不要用 `local-qwen`——歸納是這整條鏈裡最需要推理品質的一步，省在這裡等於讓後面的人審核去承擔 |
| [persistence/memory_smoke_test.py](../persistence/memory_smoke_test.py) | 修改 | 加一個情境：`pending` 的記憶 `recall()` 讀不到、`active` 的讀得到 |

**不需要改的**：`persistence/memory_prompt.py`（`inject_procedural()` 只是呼叫 `recall()`，過濾在 `recall()` 那層做完就好）、`orchestrator/memory_writer.py`（episodic 寫入不變）、三個 agent 的程式碼（完全無感）。

---

## 4. M5 品質關卡：展開

原文四個 bullet（[long-term-memory-plan.md](long-term-memory-plan.md#L442-L451)）拆成四塊具體的東西。

### 4.1 A — 標註評測集（沒有這個，後面全部做不了）

**目標場景改成保單除外責任判斷（`llm/exclusion_judge.py` / `judge_exclusion()`），不是台積電判斷。** 理由：demo 會以 `stt_exclusion_notify` 這條 workflow 為主；而且 `judge_exclusion()` 的輸出比 `mentions_tsmc()` 更適合當評測集的起點——除了 `involves_exclusion: bool`（可判定），還有 `matched_articles: list[str]`（可做集合比對的結構化欄位），正好是 §6 #2「非結構化輸出怎麼定過關」問題的部分解——但**不是完整解**，見下方「已驗證發現」。

**建議：評測集用逐字稿文字，不要用音檔。** 理由——要驗證的是 `check` 這個判斷 step 加了記憶會不會壞掉，跑音檔等於每次評測都額外跑一次 STT，慢、貴、而且 STT 本身的不確定性會污染判斷結果的訊號。

`samples/gen_policy_*.wav` 只有 2 個、且都沒有標註檔。實際用真實服務（`open_agent_memory()` + 已 seed 的 `data/insurance_product/kgi_ltc.yaml` + `judge_exclusion()`）跑過一次確認逐字稿與正確答案：

```yaml
# evals/check_cases.yaml
- id: drunk_driving_bike        # 對應 samples/gen_policy_01.wav 現在的實際內容
  transcript: "我上個月喝了點酒騎腳踏車回家撞了現在右手肩膀手肘手腕都動不了了醫生說好不了我這張長照顯賠不賠"
  expected: {involves_exclusion: false}
  split: holdout   # docs/exclusion-scenario-plan.md P4 的主驗收案例：直覺答案「酒駕→除外」是錯的。目前沒有規則指著它修，還不夠格叫 regression（見 §5 P3）
- id: unrelated_ltc_criteria     # 對應 samples/gen_policy_02.wav
  transcript: "長期照顧狀態到底怎麼認定要準備什麼文件"
  expected: {involves_exclusion: false, matched_articles: []}
  split: holdout
- id: self_harm                  # P4 的「明確除外」案例，原音檔已被 cf6b141 覆蓋，只剩逐字稿
  transcript: "我那時候是想不開自己弄的，現在人躺著要人照顧"
  expected: {involves_exclusion: true}
  split: holdout
```

**已驗證發現（跑真實 `judge_exclusion()` 得到的，不是猜測）**：

- 三個案例的 `involves_exclusion` 都跟預期一致、且穩定——這是主要判準。
- **`matched_articles` 不穩定，不能當嚴格相等比對**：`drunk_driving_bike` 案例，`reason` 裡正確講出了第16、17、29條的關係，但模型最終判定「不適用除外」時，把 `matched_articles` 留成空陣列 `[]`——不是文件原先預期的「含第27、29條」。判斷本身（`involves_exclusion`）穩，引用清單的精確內容會因為模型怎麼詮釋「相關」而浮動。**評測執行器要把 `matched_articles` 當輔助訊號（例如子集合檢查、或只在案例明確標注時才比對），不能當跟 `involves_exclusion` 同等地位的硬指標**——這條印證了 §6 #2 的疑慮：就算有結構化欄位，也不代表能拿來做嚴格相等比對。
- README.md 對 `gen_policy_01.wav` 的說明原本是舊版「自傷」音檔內容，已順手修正（見該檔案）——這次順帶抓到的既有文件缺口，不是本次改動造成的。

要做的事：`self_harm` 案例目前沒有對應音檔（P4 原本的音檔在 `cf6b141` 被改成 `drunk_driving_bike` 的內容）——`evals/check_cases.yaml` 用文字就夠，不必補音檔，除非之後想在 demo 裡展示一個真的會觸發通知的音檔案例（那是獨立的 nice-to-have，不擋 P1）。

### 4.2 B — 評測執行器

**`workflows/parity_check.py` 不是這個東西**，別誤用：它比對的是「同步路徑 vs 事件驅動路徑跑出來一不一樣」，是一致性檢查，不對任何正確答案。

要新增的是 `evals/run_eval.py`：讀 `check_cases.yaml`，對每個 case 呼叫判斷函式，跟 `expected` 比對，回傳通過率 + 逐案結果。關鍵是它要能**用同一組案例跑兩種設定**（下一節）。

### 4.3 C — 晉級判準

一條候選規則的評測是**兩次跑、比差異**，不是單次跑看及格：

| 跑法 | 設定 | 看什麼 |
|---|---|---|
| baseline | 不含這條候選規則（`status=pending` 本來就讀不到，所以這是預設狀態） | 現在的準確率 |
| candidate | 含這條候選規則 | 加了之後的準確率 |

判準：**目標案例修好（原本錯的那幾筆現在對了）且 holdout 不退步**。這也是為什麼候選規則要帶 `evidence`（§3.3）——「原本錯的那幾筆」就是從 evidence 指到的 episodic 案例來的。以 `involves_exclusion` 為硬指標；`matched_articles` 只當輔助訊號（見 §4.1「已驗證發現」——它不穩定，不能跟 `involves_exclusion` 同等看待）。

技術上怎麼讓 candidate 那一跑讀得到 pending 記憶，有三種做法：

1. `recall()` 開一個 `include_pending` 參數——最直接，但等於為了測試在生產 API 上留一個繞過關卡的後門。
2. 評測執行器不走 `recall()`，自己組 prompt——沒有後門，但也就沒有跑到真實路徑（`judge_exclusion()` 內部是自己呼叫 `inject_procedural()` 組 prompt 的，見 [llm/exclusion_judge.py:188-192](../llm/exclusion_judge.py#L188-L192)，外部塞不進去，等於要複製一份判斷邏輯——複製品跟本尊漂掉的時候，評測會過但線上是壞的）。
3. **借用既有的 tenant 維度**：候選規則同時以 `active` 寫進一個拋棄式租戶（例如 `eval`），評測跑 `judge_exclusion(..., tenant="eval")`。走的是完全一樣的真實路徑、不用改任何生產 API，代價是 [mcp_servers/policy.yaml](../mcp_servers/policy.yaml) 要為 `check` 多開一條 `eval/procedural/*/check` 讀權限（會碰到那個凍結中的 `memory:` 區塊，見 §6 #1），而且要確保評測用的租戶不會被誤當成真租戶留在 store 裡。

**2026-08-10 定案：選做法 3。** `evals/run_eval.py` 的 `--tenant` 參數已經在 P1 階段就先接好（見該檔案），`judge_exclusion(..., tenant=tenant)` 這條路徑本來就通；P3 還缺的兩塊已補上：（a）`policy.yaml` 幫 `check` 開 `eval/procedural/*/check` 讀權限、（b）`scripts/stage_candidate_for_eval.py`（新增）把某個 `pending` 候選規則讀出來、以 `active` 寫進 `eval` tenant（寫入前先清空該 scope 下 eval tenant 既有的東西，保證每次只測一條規則，不會累積前幾輪的殘留）。

**已跑過（2026-08-10，真實服務，第一版，only procedural staged）**：把 P2 產出的候選規則（見上）用 `stage_candidate_for_eval.py` 寫進 `eval` tenant，`uv run python -m evals.run_eval --tenant eval --repeats 3`：`drunk_driving_bike` 從 baseline 的 0/5（0%）變成 3/3（100%），另兩筆 holdout 案例維持 3/3。

**發現這個結果不可信，根因是混淆變數**：`judge_exclusion()` 的 `tenant` 參數同時控制 procedural 注入*和* episodic few-shot（[llm/exclusion_judge.py:186-190](../llm/exclusion_judge.py#L186-L190)），但當時 `policy.yaml` 只幫 `check` 開了 `eval/procedural/*/check`，沒開 `eval/episodic/*/check`——`eval` tenant 下沒有任何 episodic 記憶，這條 recall 會被拒（fail-closed，不會噴錯，只是回空清單）。結果是那一跑**同時**少了 baseline（`tenant=default`，能讀到 P2 種子的 3 筆 episodic few-shot）有的參考案例——3/3 的「進步」有兩個可能解釋：候選規則真的有效，或只是少了 few-shot 剛好幫了忙，兩者糾纏在一起分不開。

**修法：把 episodic 也鏡射進 eval tenant，讓兩次跑除了候選規則以外完全一樣**——`policy.yaml` 再開 `eval/episodic/*/check` 讀權限；`stage_candidate_for_eval.py` 擴充成先清空 `eval/{procedural,episodic}`、寫入候選規則、再把 `default` tenant 目前 active 的 episodic 記憶原封不動複製一份進 `eval` tenant。

**重跑後（真實服務，episodic 已鏡射）**：`uv run python -m evals.run_eval --tenant eval --repeats 3` → **`drunk_driving_bike` 打回 0/3（0%）**，跟 production baseline 一致；另兩筆 holdout 仍 3/3。**結論反轉**：候選規則對 `drunk_driving_bike` 沒有實際效果，先前的 3/3 完全是拿掉 few-shot 造成的假象。這條候選規則目前看起來**不該被人審核通過**（至少對這個 regression 案例沒有修好任何東西）——留給人審核時看到這個對照即可，不用現在就手動改 `status`。

這次調查本身驗證了 §4.3 approach 3 這套機制的價值：如果沒有堅持把混淆變數控制乾淨，會把一個假陽性結果當成候選規則有效的證據，讓一條沒用的規則被放進人審核甚至上線。

### 4.4 D — 人工審查與回滾

- **procedural 強制人審**：不是「評測過了就自動 active」。理由是評測集永遠涵蓋不完全，而 procedural 影響的是**所有**未來輸入。
- **回滾**：`store` 有 `updated_at`，再加 `value.version` 就能退回上一版。目前 `remember()` 是直接 `aput()` 覆寫，沒有版本概念——要做回滾就得決定舊版本存哪（同 key 加版號？另一個 namespace？），這是未定項。

---

## 5. 實作步驟

分成四個階段。**每一階段結束時系統都是可以停在那裡的**——不會出現「做到一半上線就壞掉」的中間狀態。階段順序不是偏好，是依賴：P0 沒做完就做 P2，蒸餾出來的規則會直接生效（§1.2）。

### P0 — `status` 欄位與 backfill（線上行為零改變）

這階段的產出是「記憶有了 pending/active 的概念」，但因為所有既有記憶都會被補成 `active`，跑完之後**三個 agent 的行為跟現在完全一樣**。

| # | 動作 | 檔案 |
|---|---|---|
| 1 | `remember()` 加 `status: str = "active"` 參數，寫進 `value` 頂層（跟 `confidence` 並列，**不要放進 `content`**——§1.3 驗證過 filter 只比對頂層） | [persistence/memory.py:243-283](../persistence/memory.py#L243-L283) |
| 2 | 寫 backfill 腳本：掃出 `store` 表所有記憶，逐筆補 `value.status = "active"` 後 `aput()` 回去。用 `alist_namespaces()` 拿到所有 namespace、對每個 namespace `asearch()`。**要 idempotent**（已經有 `status` 的就跳過），比照 [scripts/seed_insurance_memory.py](../scripts/seed_insurance_memory.py) 的形狀 | `scripts/backfill_memory_status.py`（新增） |
| 3 | **先跑 backfill，再改 `recall()`**——順序顛倒的話，中間那段時間三個 agent 會安靜地讀不到任何記憶 | — |
| 4 | `recall()` 預設帶 `filter={"status": "active"}`。注意跟呼叫端傳進來的 `filter=` 要合併（現在是直接轉傳給 `asearch()`），不能覆蓋掉 | [persistence/memory.py:98-125](../persistence/memory.py#L98-L125) |
| 5 | **`browse()` 一起擋**（理由見下方）。三個地方各自處理：①`asearch()` 那次加同一個 `filter`（擋 `items`）；②`aget(ns, INDEX_KEY)` 拿到的 `_index` 要在 Python 端檢查 `value["status"]`（`aget` 沒有 `filter` 參數）——`own_index` 跟 `child_indexes` 兩處都要；③`alist_namespaces()` 沒辦法過濾，見下方殘留缺口 | [persistence/memory.py:133-240](../persistence/memory.py#L133-L240) |
| 6 | smoke test 加情境：同一個 scope 底下寫一則 `pending`、一則 `active`，`recall()` 只回得到 `active` 那則；`browse()` 的 `items` 同樣只看得到 `active` 那則、pending 的 `_index` 摘要不出現 | [persistence/memory_smoke_test.py](../persistence/memory_smoke_test.py) |

**驗證**：
```bash
uv run python -m scripts.backfill_memory_status     # 應報告補了 70 筆左右
psql agent_architecture -c "select count(*) from store where value->>'status' is null;"   # 應為 0
uv run python -m persistence.memory_smoke_test
uv run python -m workflows.parity_check             # 需要 uv run honcho start 先跑起來 -- 見下方 caveat
```

> ⚠️ **`parity_check.py` 現在會在最後一個斷言（`call_log` shape 比對）失敗，跟這階段的改動無關**——已用 `git stash` 驗證過：stash 掉本階段所有改動、重跑，同樣的 `AssertionError` 照樣出現。原因是既有缺口（[TODO.md](../TODO.md#parity-check-memory-shape-mismatch)）：同步路徑刻意不接記憶（`store=None`），event-driven 路徑接了，兩邊的 `call_log` 天生就會有不同筆數的 `kind='memory'` row，但這支測試的 shape 比對還停在「兩邊完全相等」，沒跟上 M2 的改動。**這階段真正要看的是 `[parity] transcript matches` 跟 `[parity] mentions_tsmc matches` 這兩行有沒有印出來**——那兩個斷言在那行 shape 比對之前，通過了就代表三個 agent 的判斷結果沒被這階段的改動影響；shape 比對的 `AssertionError` 是預期中的雜訊，不是這階段的回歸。

**完成的定義**：`store` 表沒有任何一筆缺 `status`；`parity_check.py` 印出 transcript/mentions_tsmc 兩個 match（證明三個 agent 讀記憶的行為沒變，shape 比對的失敗是既有缺口，見上方 caveat）；smoke test 證明 `pending` 對 `recall()` 跟 `browse()` 都讀不到。

#### 為什麼 `browse()` 一定要一起擋

一開始評估時以為 `browse()` 只回 namespace 結構跟 `_index` 摘要、語意上跟 `recall()` 不同，可以先不動——**那是錯的**。看 [persistence/memory.py:221](../persistence/memory.py#L221)：

```python
items = [{"key": item.key, **item.value["content"]} for item in own_items]
```

`items` 是把該層每一則記憶的 `content` **整包展開**回去，不是摘要。`mcp_servers/memory/server.py` 又把 `browse()` 包成 agent 可以自己呼叫的 MCP 工具（M4.5），所以一則 pending 的 semantic 記憶，只要 agent browse 到它所在的那一層，全文就直接進 LLM 的 context——跟 `recall()` 讀到它的效果沒有任何差別。

真正該守的不變式是**「`pending` 不會被任何讀取端看到」**，一條規則、零例外。按 API 逐個決定要不要擋，只會養出「這個 API 擋、那個不擋」的例外表，之後每加一個讀取端就要重問一次同樣的問題——而漏掉的那一次就是關卡失效的那一次。

**殘留缺口（要誠實記著）**：`alist_namespaces()` 沒有 `filter` 參數，所以一個底下**只有** pending 記憶的 namespace，它的 segment 名字仍然會出現在 `children`/`siblings` 裡（摘要會是 `None`，因為 `_index` 被擋掉了）。要完全隱藏得對每個子樹各掃一次，成本跟 `browse()` 「只展開一層、不做全樹走訪」的設計原則直接衝突（[docs/exclusion-scenario-plan.md](exclusion-scenario-plan.md) §2.3）。判斷：洩漏的是一個 segment 名字、沒有任何內容，可以接受——但如果之後有人把敏感資訊編進 namespace 名字裡，這個判斷要重新評估。

### P1 — 評測集與執行器（不碰記憶，純新增）

| # | 動作 | 檔案 |
|---|---|---|
| 1 | §4.1 那三筆已驗證案例（逐字稿 + 真實服務跑出的正確答案）先落地成 yaml。**答案一律人工／真實服務驗證，不要從既有 episodic 自動生**（§6 #4：`status == "ok"` 只代表執行成功，不代表判斷正確） | `evals/check_cases.yaml`（新增） |
| 2 | 邊界案例已經有了——`drunk_driving_bike` 本身就是 P4 設計的邊界案例（直覺答案是錯的）。之後累積更多邊界案例時比照同樣格式擴充 | 同上 |
| 3 | 標 `split: holdout` / `regression`。holdout 是「不准退步」的守門案例；regression 專屬於「某條已核准規則的 `evidence` 指到的案例」，**P2 還沒做出任何規則前，全部案例都只能是 holdout**（§4.1 三筆現在都是 holdout，見 §5 P3 項目 1） | 同上 |
| 4 | 執行器：讀 yaml、對每個 case 呼叫 `judge_exclusion(gateway, transcript, store=..., memory_policy=..., tenant=...)`（[llm/exclusion_judge.py:180](../llm/exclusion_judge.py#L180)，簽章已經吃純文字，不用碰音檔），比對 `involves_exclusion`（硬指標）+ `matched_articles`（輔助訊號，見 §4.1），印出通過率 + 逐案結果。**`gateway` 要用 `MCPGateway(policy, principal="check")` 建**——`judge_exclusion()` 靠 `memory__browse_semantic_memory` 這個 MCP subprocess 工具查條款，沒有 `principal` 會在 subprocess 裡 fail-closed 查不到任何東西（[docs/exclusion-scenario-plan.md](exclusion-scenario-plan.md) P2 的架構缺口，`llm/tsmc_judge.py` 不用管這個是因為它的確定性 backstop 不靠 MCP subprocess）。要能接受 `--tenant` 以支援 §4.3 的做法 3 | `evals/run_eval.py`（新增） |

**驗證**：
```bash
uv run python -m evals.run_eval          # 需要 honcho stack + 已 seed 的保單條款
```
拿現況跑一次，記下 baseline 通過率——**這個數字是後面所有晉級判斷的基準線，沒有它就沒有「有沒有退步」可言**。

> **2026-08-10 更新，重大修正**：最初記錄的「baseline = 3/3（100%）」只跑了一次，後來證實不可信——見下方「P2 開工前的重複取樣調查」。真正站得住的數字改記在那一節。

**P2 開工前的重複取樣調查（2026-08-10）**：準備 P2 種子資料時，`evals/run_eval.py` 開始間歇性崩潰（模型輸出非 JSON），逼出一次完整調查，過程和結論：

1. **格式崩潰的根因確認並修好**：`persistence/memory_prompt.py::recall_episodic_few_shot()` 原本把過去案例塞成假的 `[user, assistant(最終JSON)]` 對話輪次——這段歷史對模型來說是一段完整示範，示範的是「不用查證、直接吐 JSON」，剛好跟系統提示要求的「一定要先 browse 查證」互相矛盾，而且 LLM 通常對示範過的行為比文字規則更買帳。改成不偽裝成對話輪次，改成一段參考文字（單則 user message），三個呼叫端（`tsmc_judge`/`stt_agent`/`exclusion_judge`）都重跑過 smoke test + 既有 mock test，沒有壞掉。修好之後，格式崩潰完全消失（`unrelated_ltc_criteria`/`self_harm` 各連續 6 次全過，零崩潰）。
2. **`evals/run_eval.py` 加 `--repeats N`**：單次 pass/fail 從來就不是可信的 baseline，尤其對這種難案例。改成每個案例跑 N 次記通過率。
3. **`drunk_driving_bike` 的問題修完 #1 之後仍然存在，且跟 few-shot 無關**：`--repeats 5` 用 `gemini-cheap`（`gemini-3.1-flash-lite`）測，**`drunk_driving_bike` 是 0/5（0%），另外兩案例都是 5/5**。回頭算這個 session 對這一題總共取樣近 20 次，只對過 2 次——最初記錄的「100%」是異常值，不是真實水準。
4. **拿 `gemini-3.1-pro-preview`（[gateway/config.yaml](../gateway/config.yaml) 新增的 `gemini-strong`，純診斷用，沒有動 `llm/exclusion_judge.py` 的 production `MODEL_NAME`）對照，`--repeats 5` 全部 15/15（100%）**，`drunk_driving_bike` 也 5/5。這確認了 #3 是模型能力天花板，不是 prompt/harness 的 bug——`gemini-cheap` 在這個多跳推理陷阱案例上就是靠不住，換強一點的模型完全解決，但代價是明顯更慢（N=5 跑超過 300 秒）、更貴。

**目前真正站得住的 baseline（`gemini-cheap`，N=5）**：

```
drunk_driving_bike       0/5   (0%)
unrelated_ltc_criteria   5/5   (100%)
self_harm                5/5   (100%)
overall                  10/15 (67%)
```

要不要把 `llm/exclusion_judge.py::MODEL_NAME` 從 `gemini-cheap` 換成更強的模型，是成本/延遲 vs 準確率的產品決策，還沒定案——見對話紀錄，這裡先如實記錄兩邊都測過的數字。

> **2026-08-11 更新**：使用者已拍板，`MODEL_NAME` 換成 `gemini-strong`（[TODO.md](../TODO.md) 的 `exclusion-judge-model-choice`）——這是接受延遲/成本代價換準確率的**暫時決定**，不是最終定案：使用者明確表示之後還是要找便宜一點的方式解決，該 TODO 項目調降優先級後保留，沒有刪除。

**完成的定義**：有一份人工標註過的案例集（含邊界案例）、一支能跑出通過率的執行器、一個記錄下來的 baseline 數字——**已達成**。這階段完全沒碰記憶讀寫，不可能弄壞線上。

### P2 — 蒸餾器（產出只會是 `pending`，線上讀不到）

**狀態：已落地。**

因為 P0 已經讓 `recall()` 只讀 `active`，這階段的產出**寫進生產 store 也不會影響任何 agent**——這正是把 P0 排在前面的理由。

| # | 動作 | 檔案 |
|---|---|---|
| 1 | 加 principal：`distiller` 讀 `default/episodic/*/*` + `default/procedural/*/*`、寫 `default/procedural/*/*`。⚠️ **動手前先確認**——這會碰到還沒定案的 `memory:` 區塊（[TODO.md](../TODO.md#memory-policy-pending)） | [mcp_servers/policy.yaml](../mcp_servers/policy.yaml) |
| 2 | 蒸餾 CLI：`--scope <workflow>/<step>`、`--limit N`。步驟照 §3.3——撈 episodic、**同時撈既有 procedural**（不撈會反覆生成重複／矛盾的規則）、一次 `chat_json()`、寫入 `status="pending"` | `scripts/distill_procedural.py`（新增） |
| 3 | 決定歸納用哪個 model。**不建議 `local-qwen`**——歸納是整條鏈裡最吃推理品質的一步，省在這裡等於把成本轉嫁給人審核。**選定 `gemini-cheap`**：歸納（把已收集案例摘要成規則）比 `judge_exclusion()` 的多跳 browsing 迴圈簡單很多，不是 P2 kickoff 那次調查抓到準確率天花板的同一種任務，先用便宜的跑，真的發現候選規則品質差再回頭考慮升級 | [gateway/config.yaml](../gateway/config.yaml) |
| 4 | 候選規則的 `content` 只放 `{"rule": str}`（要跟 `inject_procedural()` 的 schema 一致，[persistence/memory_prompt.py:61](../persistence/memory_prompt.py#L61)）；`evidence` / `rationale` 放 `value` 頂層，**不進 `content`**——它們是給人審核看的，不該被注入 prompt | 同 #2 |

**驗證**：
```bash
uv run python -m scripts.distill_procedural --scope stt_exclusion_notify/check
psql agent_architecture -c "select key, value->'content'->>'rule' from store where prefix like '%procedural%' and value->>'status'='pending';"
uv run python -m evals.run_eval --repeats 3    # 通過率應與 baseline（10/15、67%）同分佈 -- 證明 pending 真的沒生效
```

**已跑過（2026-08-10，真實服務、`gemini-cheap`）**：`scripts/distill_procedural.py --scope stt_exclusion_notify/check` 讀了 3 筆種子 episodic（`seed-drug_driving_amputation`/`seed-criminal_robbery`/`seed-claims_payout_timing`），產出 1 條候選規則（「若客戶詢問的情況涉及違法行為（如吸毒、犯罪），請務必優先檢查條款中的除外責任章節...」），`evidence` 正確指到 `seed-drug_driving_amputation`/`seed-criminal_robbery`（兩則涉及違法行為的案例，沒有把不相關的 `seed-claims_payout_timing` 也拉進來），DB 確認 `status="pending"`。寫完之後 `--repeats 3` 重跑 `evals/run_eval.py`：`6/9（67%）`，逐案分佈（`drunk_driving_bike` 0/3、其餘 3/3）跟寫入前的 baseline 完全一致（比例相同）——`pending` 候選規則沒有以任何形式影響 production 判斷。

**完成的定義**：跑得出候選規則、每條都帶 evidence、而且**評測通過率跟 baseline 完全一樣**（證明 pending 沒洩漏到線上）——**已達成**。

### P3 — 評測晉級與人審核（閉環）

**狀態：核心閉環已落地。**

| # | 動作 | 檔案 |
|---|---|---|
| 1 | **`evidence` → regression case 的轉換**：`evidence`（§3.3）存的是 episodic item 的 `key`，不是 `evals/check_cases.yaml` 的一筆 eval case——兩者是不同東西，中間沒有自動轉換，也不該自動轉換：episodic 的 `output` 欄位沒人驗證過是對的（`status=="ok"` 只代表執行沒出錯，見 §6 #4），直接拿來當標準答案，等於自己測自己。**做法：`review_memory.py` 在 approve 之後，對每個 evidence key 印出一筆現成的 `split: regression` YAML entry**（含 transcript、模型當時的輸出當參考），審核者人工確認 `expected` 後自己貼進 `check_cases.yaml`——CLI 只做格式化，不直接寫檔，保持該檔「人工維護」的定位 | `scripts/review_memory.py`（新增） |
| 2 | **§4.3 三選一，已定案並落地做法 3（拋棄式 tenant）**：`policy.yaml` 開了 `eval/procedural/*/check` + `eval/episodic/*/check` 讀權限；`scripts/stage_candidate_for_eval.py` 把候選規則以 `active` 寫進 `eval` tenant，並把 `default` 目前 active 的 episodic 記憶鏡射進去（見 §4.3「已知落差」的修法），確保 baseline/candidate 兩次跑除了候選規則本身以外完全一樣 | `mcp_servers/policy.yaml`、`scripts/stage_candidate_for_eval.py` |
| 3 | **晉級判準的對照輸出**：`review_memory.py` 對每個 pending 候選規則自動跑一次 stage + baseline(`default`) + candidate(`eval`) 對照，逐案印出 `IMPROVED`/`REGRESSED`/`-`。目前是「印出來給人看」，**還沒有自動化的通過/擋下判準**（例如 regression 沒修好就擋 approve）——`split: regression` 案例現在都還是 0 筆（見上，因為還沒有審核者真的貼過任何 entry 進 check_cases.yaml），所以「regression 修好 + holdout 不退步」這條判準目前實質上只有 holdout 半邊在跑，這是接下來累積 regression case 後才會顯出價值的部分，不是這輪的 bug | `scripts/review_memory.py` |
| 4 | **人審核 CLI**：列出一個 scope 下所有 `pending` → 顯示 rule/rationale/evidence → 自動跑第 3 項的對照 → `input()` 問 approve/reject/skip。approve 用 `store.aput()` 直接把 `status` 從 `pending` 翻成 `active`，保留原本的 `evidence`/`rationale`/`created_by`（不是重新 `remember()`，避免覆寫掉原始出處），另外加 `reviewed_by`/`reviewed_at`。reject 直接刪除該筆（§6 #6 仍未定案：要不要留 `status="rejected"` 當負面訊號回饋給下次蒸餾——先用最簡單的刪除，之後真的觀察到同一條爛規則反覆被蒸餾出來再加） | `scripts/review_memory.py` |
| 5 | **procedural 一律強制人審**：approve/reject 都要走 CLI 的 `input()`，沒有任何「評測過就自動 active」的路徑 | 同上 |

**已跑過（2026-08-11，真實服務）**：
- 對 P2 產出的候選規則跑 `review_memory.py`：對照表顯示 `drunk_driving_bike` baseline/candidate 都是 0%（無變化）——**證實這條規則沒用**，選 reject，`store` 裡對應的 `default` tenant pending 項目被刪除，之後同一個 scope 下 `review_memory.py` 正確回報「no pending candidates」。
- 用一筆假造的 pending 候選規則測 approve 路徑：approve 後 `status` 正確變成 `active`（`store.aput` 直接改的，`content`/`evidence`/`rationale` 都保留），CLI 印出的 regression-case 建議格式正確（transcript + 模型原始輸出 + `split: regression`）。approve 這條路徑先前完全沒測過，這次補上，確認沒有 bug 後才把測試資料清掉。
- `persistence.memory_smoke_test` 全數通過（`policy.yaml` 這輪多開了兩條讀權限，跑一次全量 smoke test 確認沒有意外影響其他情境）。

**跟原計畫的落差（誠實記錄，不是還沒做完）**：P2 目前唯一產出過的候選規則被證實無效、已 reject，所以「approve 一條真的有用的規則 → 通過率真的提升 → `parity_check.py` 仍通過」這個最終端到端場景，**目前手上沒有一條「有效」的候選規則可以拿來示範**——approve 這條程式碼路徑本身已經用假資料驗證過沒有 bug（見上），缺的是一個真實案例走完整條鏈給人看，不是機制沒做。之後 P2 蒸餾器產出新的候選規則、真的修好某個 regression 案例時，這個場景自然就會發生。

**完成的定義**：「episodic 經驗 → 規則 → 人審（含真實評測對照）→ 強制注入」這條鏈的每一段都已落地且個別驗證過——**機制上已達成**；「一條真的有效的規則被 approve 並提升準確率」這個里程碑式的示範還沒發生，取決於 P2 未來蒸餾出更好的規則，不是這階段要補的工。

---

### P4 — 審核時就地改規則文字再重測

**動機**：P2 蒸餾器目前是一次性、無回饋的生成器（§6 #6 未定案），一條規則「方向對但用字不夠精準」時，原本唯一的路是 reject 再等下次蒸餾重猜，沒有中間地帶。

**做法**：`review_memory.py` 的 approve/reject/skip 提示多一個 `e`（edit）選項——讓審核者直接重打 `rule` 文字，`remember()` 把新文字覆寫進 `eval` tenant 同一個 key（預設 `status="active"`，見 [persistence/memory.py:274](../persistence/memory.py#L274)），然後重新跑一次對照表，回到同一個提示。baseline 只在每個候選規則的迴圈外算一次——candidate 端的文字才會變，重算 baseline 只是白燒 eval 呼叫。

approve 時如果這個候選被編輯過，存進 `default` tenant 的值會多一個 `edited_by_reviewer: true` 欄位——因為存進去的 `rule` 文字已經不是 `created_by`（distiller）原本產出的東西，`created_by` 單獨看會誤導成「這是蒸餾器寫的」。

檔案：`scripts/review_memory.py`。

---

### P5 — episodic 寫入也套用 pending/active 關卡，且不再對 agent 可見（堵住 few-shot 洩漏這條唯一沒被把關的路徑）

**狀態：已落地，且比原計畫更進一步——episodic 不再只是「先過一關才 active」，而是整條 few-shot 注入路徑直接拔掉。**

**動機**：P0 讓 `recall()`/`browse()` 只讀 `status="active"`，procedural 因此有了「蒸餾出來的規則要先經人審核才生效」的關卡（P2/P3）。但這個關卡原本只蓋到 procedural——`orchestrator/memory_writer.py::_apply_rule()` 寫 episodic 記憶時完全沒有傳 `status`，直接吃 `remember()` 的預設值 `"active"`，等於**每一次 production 執行完，不管 agent 判斷對不對，那筆記憶立刻對外可見**（唯一的門檻是 `completion.payload["status"] == "ok"`，只代表這個工作流程步驟有正常跑完，不代表判斷結果正確，見 §6 #4）。

這條路徑原本是整條鏈裡唯一沒有任何把關的地方：一個錯誤判斷寫進 episodic 後，`recall_episodic_few_shot()`（`persistence/memory_prompt.py`）會把它原封不動地當「過去案例參考」塞進下一個相似問題的 prompt——不用等蒸餾、不用等人審核，錯誤答案就能直接教壞下一次判斷；同一批被污染的 episodic 也會被 `distill_procedural.py` 讀進去當歸納原料，有系統性放大成錯誤 procedural 規則的風險。`docs/exclusion-episodic-cases.md`「為什麼要加後三筆」記錄過一次因為語料缺乏對照組而歸納錯誤的案例，根因跟這裡相通：episodic 內容的正確性從來沒被驗證過。

**跟原計畫的落差**：原本設計是「先過 pending/active 關卡，過關後一樣進 agent 的 few-shot」。動手前重新討論後決定範圍更大——**episodic 直接從 agent 可見的記憶裡整個拿掉，只留給蒸餾器當原料**（`persistence/memory_prompt.py` 已經沒有 `recall_episodic_few_shot()` 這個函式，不是留著但一直讀不到）。理由：episodic 的品質天花板本來就卡在「沒有人工裁決入口」（TODO.md's needs-review-decision-entry），與其把一個「大部分時候讀不到、少數時候讀到未必可靠」的東西留在 agent 的 prompt 組裝路徑上，不如讓 procedural（已經有完整的蒸餾 + 人審 + 評測對照鏈）當唯一真正影響 agent 判斷的記憶，episodic 退回「原始生產軌跡」的定位——這也解掉了 §3.9 那條「episodic 必須強制注入」判準的推論成分：不用驗證要不要強制了，因為它已經不注入。

**實際做法**：

| # | 動作 | 檔案 |
|---|---|---|
| 1 | `_apply_rule()` 的 `remember()` 呼叫加 `status="pending"` | [orchestrator/memory_writer.py](../orchestrator/memory_writer.py) |
| 2 | 移除 `recall_episodic_few_shot()` 及所有呼叫端（`llm/tsmc_judge.py`/`llm/exclusion_judge.py`/`llm/stt_agent.py`/`llm/notify_agent.py`）——不是留著但無效，是整個函式跟四處呼叫都拔掉 | `persistence/memory_prompt.py` + 上述四個檔案 |
| 3 | `scripts/distill_procedural.py` 完全不用改：它讀 episodic 用的也是 `recall()`，P0 的 status 過濾本來就不分 kind，蒸餾器自動變成只讀已審核（`active`）的 episodic | （不用改） |
| 4 | 人審 CLI：列出一個 scope 下所有 pending episodic，秀出 `input`/`output`。**四個選項 a/r/e/s**：`a`（approve）/`e`（edit，重打 `output` 再 approve）→ `status="active"`；`r`（reject）/`s`（skip）→ **不刪除，維持 `pending`**（跟 `review_memory.py` procedural 的 reject 會刪除不同——episodic 沒有 evidence/rationale 這類需要清理的候選中繼資料，被拒的案例留著也不影響任何讀取路徑，之後審核者可以重新看一次） | [scripts/review_episodic.py](../scripts/review_episodic.py)（新增） |
| 5 | `scripts/stage_candidate_for_eval.py` 移除 episodic 鏡射：`judge_exclusion()` 的 `tenant` 參數本來要驅動 `inject_procedural()` **和** `recall_episodic_few_shot()` 兩者，鏡射就是為了讓 baseline/candidate 兩次跑除了候選規則外完全一樣；拔掉 few-shot 之後，`tenant` 只剩 `inject_procedural()`，鏡射變成死代碼，一併移除；`policy.yaml` 的 `eval/episodic/*/check` 讀權限同步收回 | 同上、[mcp_servers/policy.yaml](../mcp_servers/policy.yaml) |
| 6 | `orchestrator/smoke_test.py` 的 `scenario_memory_writer_distills_episodic` 改用 `store.aget()` 直接讀（原本用 `recall()`，會被 P0 的 status 過濾擋住），並新增斷言 `status == "pending"` | [orchestrator/smoke_test.py](../orchestrator/smoke_test.py) |

**沒做的部分**：原計畫 #3「審核節奏要不要接自動化 proxy」——目前只有純人工逐筆審核，量級問題還沒真的出現（demo 規模），先不做。

**驗證**：`gather_concurrency_smoke_test.py`、`llm/exclusion_judge_smoke_test.py`（不需活服務）與 `persistence/memory_smoke_test.py`、`orchestrator/smoke_test.py` 的兩個 memory_writer 情境（需活服務）全數通過；手動跑過 `scripts/review_episodic.py` 的 a/e/r/s 四條路徑，確認 reject/skip 留在 `pending`、approve/edit 轉 `active` 且 `edited_by_reviewer` 正確標記。

**觸發這個規劃的討論**：`docs/exclusion-actor-distinction-demo.md` 那組「要保人 vs 被保險人」demo 案例（2026-08-11/12）動手驗證時發現的——demo 用的兩筆 episodic 案例是人工寫入的正確答案（`scripts/seed_exclusion_episodic_examples.py` 本身就是 bootstrap 腳本，不是真實 production 的寫入路徑），如果換成真實 `_apply_rule()` 路徑，同樣一次誤判會被原封不動存成「正確答案」，few-shot 洩漏機制會讓後續相似案例照抄這個錯誤而不是被糾正，蒸餾器也會拿到被污染的原料。詳細的發現過程（含實測 few-shot 洩漏、`stage_candidate_for_eval.py` 排序 bug 等連帶發現）留在那次對話紀錄裡，沒有另外整理成文件。

---

## 6. 待確定的問題彙總

2026-08-11：這裡原本各自展開的說明已經搬進 [TODO.md](../TODO.md) 長期追蹤（避免同一件事兩邊維護、內容分岔）——這份文件裡散落的 `§6 #N` 引用維持不變，指到下面同一個編號，只是內容改成指向 TODO.md 對應項目。

| # | 問題 | 追蹤 |
|---|---|---|
| 1 | 動 `policy.yaml` 的 `memory:` 區塊要新增 principal | 已在 TODO：[memory-policy-pending](../TODO.md#memory-policy-pending) |
| 2 | 非結構化輸出的 agent 怎麼定「過關」 | [TODO.md#memory-gate-unstructured-output-criteria](../TODO.md#memory-gate-unstructured-output-criteria) |
| 3 | regression set 三種 kind 都納入嗎 | [TODO.md#memory-gate-regression-set-kinds](../TODO.md#memory-gate-regression-set-kinds) |
| 4 | regression 案例的「正確答案」哪來 | 跟 episodic 品質天花板同一個根因（缺人工裁決入口），記在 [TODO.md#needs-review-decision-entry](../TODO.md#needs-review-decision-entry) 底下的 2026-08-11 更新，沒有另開一項 |
| 5 | ~~評測時怎麼讓候選規則生效~~ | 已排除：定案做法 3（拋棄式 `eval` tenant），見 §4.3，不需要追蹤 |
| 6 | reject 掉的候選規則怎麼處理 | [TODO.md#distill-reject-negative-signal](../TODO.md#distill-reject-negative-signal) |
| 7 | 回滾的舊版本存哪 | [TODO.md#memory-procedural-rollback-versioning](../TODO.md#memory-procedural-rollback-versioning) |
| 8 | 「人」在哪裡審核 | [TODO.md#memory-gate-review-ui](../TODO.md#memory-gate-review-ui) |

---

## 7. 範圍邊界：明確不做的事

- **不做**「蒸餾出來直接 active」——那等於讓系統自我改版無人把關（§1.2）。
- **不做** semantic 的自動蒸餾。本文只處理 episodic → procedural；semantic 是「判斷某個屬性算不算值得記的事實」，是另一個形狀的問題（[TODO.md](../TODO.md#memory-writer-llm-judgment)）。
- **不做** 寫入端的 episodic 篩選。那是另一個獨立問題（[TODO.md](../TODO.md#memory-writer-write-filter)）——關卡管「寫進去之後能不能被讀到」，篩選管「要不要一開始就寫」，兩者可以各自獨立決定要不要做。
- **不做** UI。CLI 夠用到證明這條鏈有價值為止。

---

## 8. 一句話總結

蒸餾負責把「發生過什麼」變成「該怎麼做」，關卡負責確保這個轉換沒有把系統弄壞——而在這個專案裡，關卡的地基（標註評測集）必須先蓋，否則蒸餾產出的候選規則只能靠人憑感覺點頭，那不是審核。
