# 新示範場景：保單除外責任檢核（含記憶的漸進式揭露）

> [!NOTE]
> 這是除外責任場景的上游設計與落地紀錄。該 workflow 目前仍宣告 `gemini-cheap` / `claude-haiku`，尚未在無雲端 API key 的 Windows 環境完整執行；目前狀態見 [current-windows-status.md](current-windows-status.md)。

## 0. 這份文件在回答什麼

把示範 workflow 從「語音 → 是否提到台積電 → 通知」換成「客戶與業務討論保險權利的對話 → 是否牽涉除外責任 → 通知」。

**真正的目的不是換場景**，是要用這個場景壓測一件現在測不到的事：

> check agent 能不能在**不把整份保單條款塞進 context** 的前提下，
> 自己一層一層往下鑽記憶目錄，最後只撈出它真正需要的那幾條條文？

所以改動重心是兩塊，其他都是配角：

1. **讀取端**：`recall()` 是「你給我完整位置、我給你內容」。新增 `browse()`——
   「你給我一段前綴、我告訴你**下一層**有哪些分支」，由 agent 自己決定要不要繼續往下鑽。
2. **儲存**：保單條款要拆成有階層的 semantic 記憶，上面那個鑽取動作才有東西可鑽。

寫入端**這次不做設計**——一次性 seed script 灌進去就好（P3）。

順帶處理一個盤點時發現的架構缺陷：`notified` agent 現在收 `mentions_tsmc: bool`，
還把「提到台積電就用 Gmail」寫死在 prompt 裡——場景邏輯滲進了通用元件（違反 [AGENTS.md](../AGENTS.md)）。
不修的話這次只是把同一個錯誤換一個場景重犯，所以列為 P0。

---

## 1. 現況盤點

| 層 | 檔案 | 現在是什麼 | 這次 |
|---|---|---|---|
| 記憶 API | [persistence/memory.py](../persistence/memory.py) | `recall()` / `remember()`，namespace = `(tenant, kind, *scope)` | **新增 `browse()`** |
| 記憶治理 | [persistence/memory_policy.py](../persistence/memory_policy.py) | fnmatch 比對**完整** namespace 字串 | **修**：prefix 也要能過 |
| 記憶 MCP | [mcp_servers/memory/server.py](../mcp_servers/memory/server.py) | 一個 tool `recall_semantic_memory` | **新增 `browse_semantic_memory`** |
| 治理設定 | [mcp_servers/policy.yaml](../mcp_servers/policy.yaml) | `check` 只有 `reader` role | 修 |
| check 邏輯 | [llm/tsmc_judge.py](../llm/tsmc_judge.py) | 台積電判斷 + 別名 backstop | **不動**（同步凍結路徑仍在用），另開新檔 |
| notified 邏輯 | [mcp_servers/notified/agent.py](../mcp_servers/notified/agent.py) | 收 `mentions_tsmc: bool`，prompt 寫死台積電 | **重構 + 搬到 `llm/`** |
| workflow 定義 | [workflows/definitions/stt_check_notify.yaml](../workflows/definitions/stt_check_notify.yaml) | 三步 + check 的 episodic 蒸餾規則 | **新增一份**，舊的不動 |
| 同步模式 | [workflows/simple_pipeline.py](../workflows/simple_pipeline.py) | 刻意凍結（`parity_check.py` 會擋） | 不動 |
| 音檔 | `samples/gen_tsmc_*.wav` | 假音檔 | 新增 `gen_policy_*.wav` |

新場景**只走 event-driven 主線**。同步模式維持跑舊場景，兩邊都不會壞，舊場景留著當回歸對照。

---

## 2. 漸進式揭露是什麼，agent 怎麼決定往哪走

### 2.1 問題：整份文件塞不進去

保單條款 16 頁、40 條 + 3 個附表。全塞進 prompt 有三個問題：context 成本、
模型在長文裡找細節的準確度會掉、而且這份還只是**一個**商品的條款——平台上遲早有幾百份。

反過來說，「一次撈 top-5 相似片段」（純向量檢索）也不夠：客戶說的話（「我那時候喝了酒騎車出事」）
跟條文用字（「吐氣或血液所含酒精成份超過道路交通法令規定標準者」）字面差很遠，
單跳語意檢索很容易撈錯。人類理專的做法是**先翻目錄**：「這是除外責任的問題 →
翻到除外責任那幾條 → 逐條看哪條對得上」。漸進式揭露就是把這個動作給 agent。

### 2.2 樹、DFS、BFS——三個名詞的白話版

**樹（tree）**：一個有分支、沒有循環的結構。namespace 天生就是樹：

```
policy/kgi_ltc/                     ← 根（root）
├── definitions/                    ← 節點（node）
├── exclusions/                     ← 節點
│   ├── article_27                  ← 葉節點（leaf，沒有子節點了，就是實際內容）
│   ├── article_28
│   └── article_29
└── benefits/
    └── ...
```

**走訪（traversal）**：從根出發把節點一個一個看過。走訪順序有兩種經典策略：

**DFS（深度優先，Depth-First Search）**——一條路走到底，撞牆了才退回來換下一條。

```
根 → exclusions → article_27（看一下，不是我要的）
                → article_28（不是）
                → article_29（是！停）
```
撞牆時「退回來」這個動作就叫**回溯（backtracking）**。

**BFS（廣度優先，Breadth-First Search）**——先把同一層全部掃過，再一起往下一層。

```
第 1 層：definitions、exclusions、benefits、claims  ← 先全部看過標題
第 2 層：挑出 exclusions 之後，再看它底下所有條文
```

差別在哪：DFS 適合「我大概知道答案在哪個方向」，猜對的話很快，猜錯要付回溯成本；
BFS 適合「不確定方向，先攤開比較」，不會白鑽深，但每層要多讀一些東西。

### 2.3 Agent 實際上怎麼走：它自己選，而且是混合的

**關鍵是：我們不實作任何搜尋演算法。** DFS/BFS 是描述 agent 行為的詞彙，不是要寫進程式的東西。
如果平台幫它決定走法，那就變成「平台替 agent 思考」，回到了寫死流程的老路。

實際發生的事長這樣（每一步都是模型自己發出的一次 tool call）：

```
turn 1  模型：browse(scope=[])
        平台：這裡有 definitions / exclusions / benefits / claims / contract / appendix
              （各附一句話 summary）
turn 2  模型（讀了 summary，判斷「客戶問的是保不保，且情況涉及酒駕」→ 選 exclusions）
        browse(scope=["exclusions"])
        平台：article_27 除外責任（一）、article_28 除外責任（二）、
              article_29 除外責任（三）、article_30 不保事項  ← 葉層，直接給條文
turn 3  模型：條文對上第 29 條第三款「飲酒後駕車…」，輸出判定
```

**這是 DFS，不是 BFS——雖然乍看很像。** 分界點在於「什麼算走訪一個節點」：
是 `browse` 這個呼叫，不是「眼睛看到名字」。`browse(root)` 回傳的那份 children summary，
是父節點**順手附贈的 metadata**，不是走訪——就像 `ls -l` 一次列出目錄裡所有檔名，
你看到了六個名字，但一個都還沒 `cd` 進去。

真正的 BFS 是這樣（六次呼叫才進第二層）：

```
browse(root)
browse(definitions) browse(benefits) browse(exclusions)
browse(claims)      browse(contract) browse(appendix)   ← 第一層全部展開完
browse(exclusions/...)                                   ← 才進第二層
```

而 agent 做的是「一路只沿著一條路徑往下」= DFS。

那「先比較同層所有選項再挑」這件事在演算法裡叫什麼？叫**啟發式（heuristic）**——
決定 DFS 下一步該挑哪個分支的評分依據。所以精確的名字是
**greedy best-first search（貪婪最佳優先搜尋）**：DFS 的骨架（只走一條路、撞牆才回溯），
但每一步不是隨便挑或按順序挑，而是挑當下看起來最有希望的那支。跟純 DFS 的差別只在挑分支的規則。

這也是為什麼 §5 風險 1 說 `_index` 的 summary 品質是整套機制的上限——
**它就是那個啟發式函數本身**。summary 寫爛 = 啟發式失準 = 退化成瞎鑽 + 大量回溯。

**為什麼不讓它跑真 BFS**：BFS 要把第一層全部展開，等於把整份保單目錄讀完，
就回到「整份文件進 context」的老問題了。**漸進式揭露只有在 DFS 形狀下才省得到 token。**
而這個形狀不需要教——`browse` 一次回傳整層的格式自然就誘導出來。

### 2.4 那回溯呢？——不用實作，對話歷史就是那個 stack

演算法課上 DFS 需要一個 stack 記住「我從哪裡來、還有哪些沒走」。這裡不用寫，
因為 **agent 的 `messages` 就是那個 stack**：每一次 `browse` 的結果都留在 context 裡，
所以「鑽下去發現不對，退回上一層」對它來說 = **再發一次 `browse`，帶上一層的 scope**。零平台成本。

要做的不是實作回溯，是讓回溯**便宜且不迷路**，三個小地方就夠：

1. **每次回傳帶 `parent` 和 `siblings`**。回溯從「回想剛才那層叫什麼」變成「照抄 `parent` 欄位」，
   少一次犯錯機會；`siblings` 則直接告訴它「這層還有哪些分支沒看」——
   等於把 DFS 的 stack 內容**印在回傳值裡**，模型不必自己記帳。
   這是[harness-engineering-principles.md](harness-engineering-principles.md) 講的「回饋迴路在工具回傳值裡」的具體應用。
2. **prompt 明講「這層沒有就退回上一層看別的分支，不要硬掰」**。
   不寫的話小模型傾向在死路上編一個答案出來——這是這個機制最主要的失敗模式。
3. **`StallGuard` 已經有了**：同一個 scope 連續 browse 兩次 → 判定卡住，`AgentLoopIncomplete`。

### 2.5 `recall()` 和 `browse()` 的差別

| | `recall()` | `browse()` |
|---|---|---|
| 呼叫端已知 | 東西**在哪**（完整 scope） | 只有大概方向，或什麼都不知道 |
| 回傳 | 該 namespace 底下的 **items 內容** | **下一層有哪些分支** + 每個一句話 summary |
| 底層 | `asearch(ns, query=...)` | `alist_namespaces(prefix, max_depth)` |
| 類比 | `cat path/to/file` | `ls path/` |
| 現有使用者 | `notified` 查收件人偏好（已知 scope） | 這次新增 |

`browse` 走到葉節點時本來就會順手把 items 一起帶回（`ls` 到底了等於 `cat`），
理論上可以吃掉 `recall`。**先不合併**——`recall` 已經在跑、有自己的 `query=` 語意排序，
合併是重構不是需求。

---

## 3. 設計決定

### 3.1 目錄說明放哪：每層一個 `_index` item

`AsyncPostgresStore.alist_namespaces(prefix=..., max_depth=...)` 已經存在（已實測確認），
所以**目錄樹不用自己維護**——namespace 本身就是樹，免費。

缺的只有「這層是什麼」的說明：`alist_namespaces` 只回 namespace tuple，
agent 看到 `["exclusions", "benefits", "appendix"]` 光靠名字判斷不了該往哪鑽。

**解法（最省的那個）**：每層 namespace 底下放一個 key 固定為 `_index` 的 item，
`content = {"title": ..., "summary": ...}`。browse 時把子層的 `_index` 一起撈出來當標籤。

`_index` 的 summary 品質是**整個機制的成敗關鍵**——它是 agent 唯一的導航資訊，
比條文本身更該花時間寫。判準：一句話要能讓人回答「我的問題該不該進這扇門」。

回傳格式：

```jsonc
{
  "scope": ["insurance_product", "kgi_ltc"],
  "summary": "凱基人壽享安心長期照顧終身保險 保單條款",
  "parent": ["insurance_product"],                        // 回溯用
  "siblings": ["other_product_a"],                        // 同層還有什麼
  "children": [
    {"segment": "definitions", "summary": "名詞定義：長期照顧狀態、免責期間、ADLs、CDR 判定標準"},
    {"segment": "exclusions",  "summary": "除外責任與不保事項：哪些原因造成的失能／長照狀態不理賠"},
    {"segment": "benefits",    "summary": "各項保險金的給付條件與金額計算"}
  ],
  "items": [],                                            // 葉節點才有內容
  "truncated": false
}
```

### 3.2 namespace 分層通則

現有慣例其實已經是這樣，只是沒被寫成規則：

```
<tenant> / <kind> / <subject_type> / <subject_id> / [結構分段...]
    ↑        ↑            ↑               ↑               ↑
 隔離邊界  記憶種類    主體是什麼類     哪一個主體    這個主體內部的結構
 不可 wildcard      （治理 grant 通常切在這裡）
```

對照現有的：`_global/semantic/company/tsmc`（subject_type = `company`）、
`default/semantic/recipient/u123`（subject_type = `recipient`）。
所以保單是一個新的 subject_type。

> **命名**：原本想叫 `policy`，但這個字在本 repo 已經被 `policy.yaml`（權限治理）佔走，
> 會撞名造成誤讀。改用 **`insurance_product`**。

切分段的準則，照重要性排：

1. **權限邊界優先**——會需要「A 能讀、B 不能讀」的地方**必須**是獨立分段。
   `memory_policy.py` 是對 `/` 分隔字串做 fnmatch，切在分段上才 grant 得動。
2. **跟 agent 的提問方式對齊，不是跟文件目錄對齊**。保單條款按「第一章第二章」分沒用
   （agent 不會問「第三章有什麼」），按「除外責任／給付條件／申領文件」分才有用——它的問題長那樣。
3. **每層 3~7 個子節點**。少於 3 這層沒有資訊量（白鑽一跳）；多於 10 等於把整份目錄倒給模型，
   漸進式揭露的意義就消失了。
4. **深度 2~4 層**。再深純粹燒 turn 數與失敗機率。
5. **分段名用穩定 id**（`exclusions`），可讀標題放 `_index.summary`——標題會改，key 不該改。

套到保單條款：

```
_global/semantic/insurance_product/kgi_ltc/               _index: 這是什麼商品
                                          /definitions    第2條：長照狀態、免責期間、ADLs、CDR
                                          /benefits       第12~21條：各項給付
                                          /exclusions     第27~31條：除外責任、不保事項  ★ 本場景目標
                                          /claims         第22~26條：申領文件
                                          /contract       第1、3~11、32~40條：契約行政
                                          /appendix       附表
                                                 /icd10          附表一：失智症 ICD 碼
                                                 /total_disability  附表二：完全失能程度表
                                                 /disability_1_6    附表三：一至六級失能程度表
```

`appendix` 底下再切一層，除了因為附表三真的很長，也刻意讓樹深度到 3
——不然只有兩層，「漸進式揭露」根本測不到（一跳就到底跟直接給沒差別）。

葉層 item：`key = "article_29"`，content：

```json
{
  "article": "第二十九條",
  "title": "除外責任（三）",
  "text": "被保險人因下列原因致成附表三所列第二至三級失能程度之一時，本公司不負給付…（全文）",
  "applies_to": ["第十六條", "第十七條"]
}
```

一條 = 一個 item。最長的條文也就幾百字，葉節點一次回 3~5 條不會炸 context。

### 3.3 `memory_policy.py` 有個會踩到的坑

現在 `can_read` 是 `fnmatch(ns, pattern)`。browse 的 prefix 是**不完整的 namespace**——
prefix `_global/semantic/insurance_product` 對不上 pattern `_global/semantic/insurance_product/*`，
會被誤判成無權限，而且因為 fail-closed 是靜默回空，**症狀是「永遠查不到東西」而不是報錯**，
很難 debug（M4.5 已經踩過一次同型的坑，見該節「架構缺口」）。

最小修法：多比一次補了 `/*` 的字串。

```python
return any(fnmatch(ns, p) or fnmatch(f"{ns}/*", p) for p in grant.read)
```

語意是「這個 prefix 底下**存在**我讀得到的東西，就允許 browse 這一層」。
browse 只揭露分段名與 summary、不揭露內容，這個放寬可接受。
若之後要更嚴，才需要在回傳前逐個 child 再驗一次 `can_read`——先不做。

### 3.4 為什麼不直接用 `query=` 語意檢索一步到位

可以，但那就測不到這次要測的東西（而且 §2.1 講了單跳檢索在這個場景的準確度風險）。
兩者也不衝突：`browse` 回傳的 items 之後仍可加 `query` 在該層內排序。
**先不做**，等目錄鑽取跑通再說。

### 3.5 `notified` 的契約要去場景化

現況（[mcp_servers/notified/agent.py](../mcp_servers/notified/agent.py)）：

- L38 system prompt 寫死「內容提到台積電這間公司的名字時，使用 Gmail 發送通知」
- L47 `_finish(mentions_tsmc, ...)` 拿這個 bool 當「該發卻沒發」的驗收條件
- 函式簽名收 `mentions_tsmc: bool`

所以它不是通知 agent，是「台積電通知 agent」。正確的切法是**上游把通知意圖算完再交棒**，
notified 只負責「怎麼送、送成功了沒」：

```python
decide_and_notify(gateway, *, should_notify: bool, subject: str, body: str, recipient_id: str)
```

- `should_notify` 由 **check** 決定（它才知道場景規則）。
- `_finish` 的邏輯**一行都不用改**，只是驗收條件從場景旗標換成場景無關的 `should_notify`
  ——「說要發就必須真的發成功」。
- prompt 裡「提到台積電 → Gmail」拿掉，改成「依收件人偏好選管道，查不到用預設」
  ——這本來就是它該做的事，M4.5 已經把偏好查詢做成 MCP tool 了。

### 3.6 `llm/` 的定位

`llm/` 底下是場景層的 agent loop（`stt_agent.py`、`tsmc_judge.py`），但**同類東西有一個放錯地方**：
`mcp_servers/notified/agent.py`。`mcp_servers/` 應該只放 MCP 殼（server + client），
agent loop 不該在那裡。三個 agent、兩個在 `llm/`、一個埋在 `mcp_servers/`，純粹是歷史。

既然 P0 本來就要改 notified，順手搬到 `llm/notify_agent.py`，三個 agent 歸位同一層。
import 改幾行而已。

---

## 4. 分階段實作計畫（含檔案級異動）

前三階段（P0~P2）是**平台能力，跟保單場景無關**，可獨立驗收；P3 之後才是場景。

### P0 — 通知契約去場景化 + agent loop 歸位

先做這步的原因：它跟記憶完全無關、跟保單完全無關，是純粹的既有債務清理。
夾在後面做的話，會跟新場景的改動混在同一批 diff 裡，出事分不清是誰的錯。

| 檔案 | 異動 | 內容 |
|---|---|---|
| `llm/notify_agent.py` | **新增（搬移）** | 從 [mcp_servers/notified/agent.py](../mcp_servers/notified/agent.py) 整檔搬過來。簽名改成 `decide_and_notify(gateway, *, should_notify, subject, body, recipient_id, store=None, memory_policy=None, tenant="default")`；`_finish(should_notify, notified_ok, log)`；`_SYSTEM_PROMPT` 拿掉台積電那句，改成「依收件人偏好選管道（`recall_semantic_memory`，scope=`["recipient", id]`），查不到用預設 Gmail」。procedural/episodic 的強制注入不動 |
| `mcp_servers/notified/agent.py` | **刪除** | |
| [agents/runtime.py](../agents/runtime.py) | 修改 | `notified` route 改呼叫 `llm/notify_agent.py`；handler 從 input 取新欄位（原先的獨立 `agents/notified/server.py` 已合併） |
| [workflows/simple_pipeline.py](../workflows/simple_pipeline.py) | **不動** | 它 import 的是 `mcp_servers.notified.agent`——⚠️ 這個檔被 `parity_check.py` 凍結，**搬家會撞到**。做法：`mcp_servers/notified/agent.py` 保留成一行 re-export（`from llm.notify_agent import decide_and_notify`），凍結檔的 import path 不變。凍結檔的 `notified_node` 仍傳舊參數 → 所以 re-export 那層要留一個相容 shim 處理 `mentions_tsmc` → `should_notify` 的轉換。**這是舊場景的場景邏輯，正確的歸屬地就是那個 shim**，不算沒清乾淨 |
| [workflows/definitions/stt_check_notify.yaml](../workflows/definitions/stt_check_notify.yaml) | 修改 | `notified` 的 input_schema：`mentions_tsmc` → `should_notify`/`subject`/`body`。舊 workflow 的 `check` step 要跟著多輸出這三個欄位（`llm/tsmc_judge.py` 回傳值包一層，不動判斷邏輯） |

**P0 完成的定義**：`grep -ri 台積電 llm/notify_agent.py mcp_servers/` 沒有結果；
舊場景（event-driven + 同步）端到端跑起來行為完全不變；`parity_check.py` 通過。

### P1 — 平台能力：`browse()` 漸進式揭露

| 檔案 | 異動 | 內容 |
|---|---|---|
| [persistence/memory_policy.py](../persistence/memory_policy.py) | 修改 | `can_read` 依 §3.3 補 prefix 比對。**只改 `can_read`，不改 `can_write`**——寫入永遠是完整 namespace，沒有 prefix 的概念，放寬它沒有理由 |
| [persistence/memory.py](../persistence/memory.py) | 修改 | 新增 `INDEX_KEY = "_index"` 常數（seed script 與 browse 共用同一個來源）；新增 `async browse(store, policy, kind, *, tenant, prefix, limit=10) -> dict`。實作：`can_read` 檢查（拒絕回空 dict，fail-closed，跟 `recall()` 一致，**不 raise**）→ `alist_namespaces(prefix=ns, max_depth=len(ns)+1)` 拿子層 → 逐個子層 `aget(child_ns, INDEX_KEY)` 拿 summary（`asyncio.gather` 併發，不要 N 次序列 round trip）→ `asearch(ns, limit=limit+1)` 拿本層 items 並濾掉 `_index` → 組成 §3.1 的 dict（含 `parent`/`siblings`/`truncated`）。回傳平台格式 dict 而非 `SearchItem`，因為它同時混了目錄與內容 |
| [persistence/memory_smoke_test.py](../persistence/memory_smoke_test.py) | 修改 | 新增情境：(a) 灌一棵三層小樹，從根鑽到葉，斷言每層 `children`/`parent`/`siblings` 正確、`_index` 不出現在 `items` 裡；(b) 葉節點的 `children` 為空、`items` 有內容；(c) 無 grant 的 principal browse 任一層都回空且不 raise；(d) prefix 比對修正的單元測試：`can_read(policy, "check", ("_global","semantic","insurance_product"))` 對 pattern `_global/semantic/insurance_product/*` 為 True |

**P1 完成的定義**：smoke test 全過，**沒有任何 agent 用到 `browse()`**。刻意零場景耦合，
跟 M1 當初的做法一致。

### P2 — `browse` 的 MCP 化 + 治理設定

| 檔案 | 異動 | 內容 |
|---|---|---|
| [mcp_servers/memory/server.py](../mcp_servers/memory/server.py) | 修改 | 新增 `@mcp.tool() async def browse_semantic_memory(scope: list[str] \| None = None) -> str`。`scope` 空 → 從該 tenant 的 semantic 根開始。**docstring 是這個機制唯一的操作說明，寫壞了整套失效**，必須明講三件事：①這是目錄不是內容，看到想要的子目錄就再 call 一次帶上它；②葉節點會直接給內容；③這層沒有你要的就用 `parent` 退回上一層看 `siblings`，**不要用猜的回答**。既有的 `recall_semantic_memory` 完全不動 |
| [mcp_servers/policy.yaml](../mcp_servers/policy.yaml) | 修改 | `principals.check` → `{roles: [reader], allow: ["memory__browse_semantic_memory"]}`（比照 M4.5 給 `notified` 開 tool 的做法，一個 principal 不值得開新 role）；`memory.check.read` 加一條 `"_global/semantic/insurance_product/*"`，既有三條保留 |
| [agents/lifespan.py](../agents/lifespan.py) | 修改 | 建立 `check` 的 `MCPGateway` 時傳入 `principal="check"`（現行實作以 step name 通用化）。不補的話 M4.5 那個 `MCP_CALLING_PRINCIPAL` 傳不下去，subprocess 裡的 principal 是 `None`，靜默 fail-closed 查無資料 |

**P2 完成的定義**：手動起一個 gateway 以 `check` 身分 call 一次 `memory__browse_semantic_memory`，
回傳長相正確；換一個沒 grant 的 principal call，回空不報錯。

> ⚠️ **前置決策**：[mcp_servers/policy.yaml](../mcp_servers/policy.yaml) 的 `memory:` 區塊目前自己標了
> 「exploratory / provisional，未定案，先別繼續往上蓋」（[TODO.md](../TODO.md#memory-policy-pending)）。
> P2 會再往上蓋一層。**開工前要先確認這件事可以往前推**，或明確接受它仍是暫定狀態。

### P3 — 保單條款入庫（一次性，不做寫入端設計）

| 檔案 | 異動 | 內容 |
|---|---|---|
| `data/insurance_product/kgi_ltc.yaml` | **新增** | 條款切分結果。人工／一次性 LLM 切分，**checked in**——這樣它可以 review、可以 diff，出錯時知道是切分的問題還是 agent 的問題。結構直接對應 §3.2 的樹：每個節點有 `summary`（會變成 `_index`）、葉節點有 `articles: [{key, article, title, text, applies_to}]` |
| `scripts/seed_insurance_memory.py` | **新增** | 讀該 yaml → 遞迴走訪 → 每個節點 `remember(key="_index", content={"title","summary"})`、每個條文 `remember(key="article_NN", ...)`。`current_node_name.set("memory_writer")`（policy.yaml 裡唯一有 write 權限的 principal）。**可重複執行**——`aput` 是 upsert 且 key 固定，重跑等於重新同步，不會長出重複資料 |
| [orchestrator/memory_writer.py](../orchestrator/memory_writer.py) | **不動** | 背景蒸餾出保單條款沒有意義，這是靜態知識，不是從執行中學到的東西 |

**P3 完成的定義**：跑完 script 之後，用 P1 的 `browse()` 從根鑽到 `exclusions` 葉層，
拿得到第 27~31 條全文；`_index` 的 summary 拿給一個沒讀過條款的人看，
他能正確回答「酒駕出事該進哪個分支」。

### P4 — check 場景邏輯：`llm/exclusion_judge.py`

| 檔案 | 異動 | 內容 |
|---|---|---|
| `llm/exclusion_judge.py` | **新增** | `async judge_exclusion(gateway, transcript, *, store=None, memory_policy=None, tenant="default") -> dict`，回傳 `{"involves_exclusion": bool, "matched_articles": [str], "reason": str, "should_notify": bool, "subject": str, "body": str}`。跟 [llm/tsmc_judge.py](../llm/tsmc_judge.py) 的形狀相同（tool loop + `StallGuard` + `AgentLoopIncomplete` + `@wrap_agent_exception`），三個差別見下 |
| [mcp_servers/policy.yaml](../mcp_servers/policy.yaml) | 修改 | `memory.check.read` 已在 P2 加好，這階段不用再動 |

三個跟 `tsmc_judge.py` 的差別：

1. **不預先注入任何條款**。system prompt 只說「保單條款在記憶裡，用 `browse_semantic_memory`
   從根目錄往下找」。**整份文件永遠不進 context，這是本次改動的驗收標準**。
2. **沒有確定性 backstop**（除外責任沒有字串比對可用），改成**引用驗收**：
   模型回傳的 `matched_articles` 必須是它在這輪 browse 過程中**實際讀到過**的條號
   （在 loop 裡累積一個 `seen_articles` 集合，從 tool 回傳值收集，不是從模型講的話收集）。
   對不上 → 追加一輪要求它重講，再對不上 → `AgentLoopIncomplete`。
   這是這顆 agent 的單輪驗收機制，取代 alias backstop 的角色。
3. **模型先用 `gemini-cheap`**。`local-qwen`(qwen2.5:3b) 要連續多跳工具呼叫 + 中途判斷方向，
   很可能撐不住。先用大的跑通，再回頭試小的——**這正好是這個場景值得測的第二件事**
   （多跳鑽取對模型能力的門檻在哪）。

procedural/episodic 的強制注入（`inject_procedural`/`recall_episodic_few_shot`）照舊沿用，不變。

#### P4 的主驗收案例：「酒駕致上肢失能」

這個案例刻意選成**正確答案是 `involves_exclusion: false`**，因為它一次測到三件事：
需要回溯、需要跨兩個分支引用、而且直覺答案（「酒駕 → 除外 → 不賠」）是**錯的**——
一顆沒真的讀到條文、靠常識硬掰的 agent 會穩定答錯，測起來訊號很乾淨。

逐字稿：

> 「我上個月喝了點酒騎車回家，撞了，現在右手肩膀、手肘、手腕都動不了了，醫生說好不了。我這張長照險賠不賠？」

正確推論鏈（三步，缺一步就會答錯）：

1. 第29條第三款確實列了酒駕，但 `applies_to` 只有**第16、17條**（意外二至三級失能保險金）
2. 排除**長照給付**的是第27條，列舉只有故意行為／犯罪行為／非法施用毒品——**酒駕不在其中**
3. 「一上肢肩、肘及腕關節均永久喪失機能」在附表三是**第6級**，未達第16條的 2~3 級門檻
   ——不是被除外，是本來就不在給付範圍

第 3 步的資料在 `appendix/disability_1_6`，跟第 1、2 步的 `exclusions` **不同分支**，
所以 agent 必須從 `exclusions` 撞牆後回溯。預期走法（4 次 browse）：

```
turn 1  browse([])                              → 6 個分支的 summary
turn 2  browse(["exclusions"])                  → 第27~31條全文；撞牆（不知道客戶算幾級）
        回傳值的 siblings 裡有 appendix ────┐
turn 3  browse(["appendix"]) ←──────────────┘   → icd10 / total_disability / disability_1_6
turn 4  browse(["appendix","disability_1_6"])   → 上肢機能障害 = 第 6 級
turn 5  輸出 involves_exclusion=false, matched_articles=["第二十七條","第二十九條"]
```

turn 3 直接跳到 `appendix` 而不是先退回根再往下，是因為 turn 1 的 children 清單還留在
`messages` 裡（§2.4：對話歷史就是 stack）。**如果實測發現它真的退回根再走一次，
表示 `siblings` 欄位沒發揮作用，要回頭調 prompt 或回傳格式。**

**已知失敗模式**：若 `appendix` 的 `_index.summary` 寫得含糊（例如只寫「附表」），
turn 2 撞牆後 agent 大概率想不到答案在那裡，會直接輸出「酒駕屬除外責任，不賠」——
**答案錯，但讀起來很合理**。這條失敗路徑就是 §5 風險 1 的具體長相，
也是 P3 那條「summary 拿給沒讀過條款的人看」驗收條件真正在測的東西。

**P4 完成的定義**：不接 stt，直接餵逐字稿字串跑三個案例：

| 案例 | 逐字稿 | 預期 | 測到什麼 |
|---|---|---|---|
| 主案例 | 上面那段酒駕 | `involves_exclusion: false`，`matched_articles` 含第27、29條，reason 說得出「第29條只管第16、17條」 | 回溯、跨分支引用、不被直覺帶偏 |
| 明確除外 | 「我那時候是想不開自己弄的，現在人躺著要人照顧」 | `involves_exclusion: true`，引用第27條第一款（故意行為） | 單分支即可答對的基本盤 |
| 無關 | 「長期照顧狀態到底怎麼認定？要準備什麼文件？」 | `involves_exclusion: false`，`matched_articles` 為空 | **不硬掰條號**（引用驗收擋得住） |

另外兩條硬性條件：`matched_articles` 裡每個條號都必須在 `seen_articles` 集合裡
（引用驗收生效）；從 `call_log` 確認整輪 context 裡從來沒有出現過完整條款
（`contract`/`claims`/`benefits` 三個分支的條文一次都沒被讀進去）。

### P5 — 端到端接線

| 檔案 | 異動 | 內容 |
|---|---|---|
| `workflows/definitions/stt_exclusion_notify.yaml` | **新增** | 三步同名（stt/check/notified），`check` 的 output_schema 是 P4 那六個欄位，`notified` 的 input_schema 是 `should_notify`/`subject`/`body`。`memory: write` 已接上（見下） |
| [agents/runtime.py](../agents/runtime.py) | 修改 | `check` route 依目前 workflow 呼叫 `judge_exclusion`，schema 由 live spec 取得；workflow 選擇仍需指向同一份 YAML |
| [agents/runtime.py](../agents/runtime.py) | 修改 | `notified` route 對應新欄位（P0 已改一半） |
| `samples/gen_policy_01.wav` | **新增** | 客戶自述情況**牽涉**除外責任（酒駕致失能／自傷／犯罪行為）。macOS `say -v Meijia` 產生，跟現有假音檔同做法 |
| `samples/gen_policy_02.wav` | **新增** | **不牽涉**（單純問「長期照顧狀態」怎麼認定、要準備什麼文件） |
| [README.md](../README.md) | 修改 | 示範 workflow 那節補上第二個場景，說明兩個場景並存、各自走哪條路徑 |

`memory: write`（`input_field: transcript` / `output_fields: [involves_exclusion,
matched_articles, reason]`，對齊 `judge_exclusion()` 模型原始輸出的形狀）已比照
`stt_check_notify.yaml` 接上——**接受的取捨**：episodic 蒸餾的品質天花板本來就卡在
[TODO.md](../TODO.md#needs-review-decision-entry) 的人工裁決缺口，而這個場景的「正確答案」
比台積電那個主觀得多（法律判斷）；一個 schema 驗證通過但法律判斷錯誤的案例，一樣會被蒸餾進
episodic、之後又當 few-shot 餵回模型。等裁決入口做出來之後再回頭補上過濾。

**P5 完成的定義**：`gen_policy_01.wav` 進去，Gmail 通知出來且內文帶著條號與理由；
`gen_policy_02.wav` 進去，沒有通知發出。舊場景（`stt_check_notify`）同時仍然跑得通。

---

## 5. 風險與已知限制

1. **`_index` summary 的品質就是這個機制的上限**。summary 寫得含糊，agent 就會鑽錯分支或全部鑽一遍
   （退化成「把整份文件讀完」，等於這次改動白做）。緩解：P3 完成定義裡那個「拿給沒讀過條款的人看」
   的驗收；長期則需要一個「browse 鑽取路徑」的評測集。
2. **小模型可能撐不住多跳**。P4 先用 `gemini-cheap` 是刻意的。如果 `local-qwen` 完全跑不動，
   那是這次實驗的**結論之一**（多跳鑽取有模型能力門檻），不是失敗——但要如實記錄，
   不要靠加 prompt 硬凹。
3. **`memory:` 治理區塊仍未定案**，P2 會再往上蓋一層（見 P2 的前置決策警告）。
4. **prefix 放寬讓 browse 可以看到「分段名稱」本身**。分段名如果本身就是敏感資訊
   （例如 namespace 裡有客戶姓名），這個放寬就不夠。目前的 namespace 設計沒有這種情況，
   但這是往後加新 subject_type 時要記得檢查的事。
5. **沒有 browse 的稽核 log**。跟 [TODO.md](../TODO.md#memory-access-audit-log) 記的
   `recall()`/`remember()` 沒有稽核 log 是同一個缺口，這次一併不補，但多一個呼叫路徑等於缺口變大。
6. **切分品質沒有自動檢查**。`data/insurance_product/kgi_ltc.yaml` 漏抄一條、抄錯一個字，
   現在沒有任何機制會發現。緩解：checked in + code review。真要做，得有一支比對原始 PDF 的驗證腳本。
7. **舊場景的相容 shim**（P0 那個 `mcp_servers/notified/agent.py` re-export）是技術債。
   等 `simple_pipeline.py` 哪天解凍，就該一起刪掉。

---

## 6. 一句話總結

`recall()` 是「我知道東西在哪，給我內容」；`browse()` 是「我只知道方向，告訴我下一層有哪些門」。
前者讓 agent 用上它已經知道自己需要的記憶，後者讓 agent 在一份大到塞不進 context 的知識裡
**自己找路**——而找路的策略（先攤開比較還是一路鑽到底、什麼時候該退回上一層）
刻意留給模型決定，平台只負責把「你在哪、旁邊有什麼、上一層是誰」誠實地放在每次工具回傳值裡。
