# Harness Engineering 核心原則

> 整理自 ihower《給 Agent 開發者的駕馭工程》系列第 2～8 篇。每次要新增或調整 agent、tool 時，回來對照這份清單，確認有沒有漏掉該有的回饋迴路。
>
> 系列全文：https://blog.aihao.tw/2026/06/26/harness-engineering-2-what-is-harness-engineering/ 起，依序到
> https://blog.aihao.tw/2026/06/26/harness-engineering-8-model-harness-fit/
> 配套投影片：https://ihower.tw/presentation/harness.html

---

## (2) 什麼是 Harness Engineering：回饋迴路

原文：https://blog.aihao.tw/2026/06/26/harness-engineering-2-what-is-harness-engineering/

**核心主張**：Agent 需要的是回饋迴路，不是完美提示。Harness 工程不是功能清單，而是讓 agent 在行動過程中被約束、檢查、修正的系統設計。

- **三層疊加模型**，做 agent 時要分清楚自己在哪一層動手：
  - Prompt Engineering：單次呼叫的表達品質
  - Context Engineering：選擇合適的資訊放進 context
  - Harness Engineering：行動迴圈中的約束與修正
- **棘輪心法（The Ratchet）**：每當發現 agent 出錯一次，就用工程手段確保「不會再犯同一個錯」——用約束取代單次提醒，讓系統只會越來越可靠，不會退步。
- **基本策略：先生成再驗證（Generate then Verify）**——不奢望一次做對，重點是提高一次做對的機率，並讓 agent 在出錯時能自我修正。
- **二維座標框架**（Böckeler）：設計任何 harness 元件時可以定位在
  - 方向軸：前饋引導（Guides，事前告訴 agent 該怎麼做）vs 回饋感測（Sensors，事後告訴 agent 做得如何）
  - 執行軸：確定性程式 vs 推論式 LLM

---

## (3) 回饋時機一：工具回傳值

原文：https://blog.aihao.tw/2026/06/26/harness-engineering-3-tool-execution-feedback/

**核心主張**：工具回傳值不只是資料傳輸，本質上是寫給 agent 看的提示工程，設計品質直接決定 agent 能不能自我修正。

- **三段式介入架構**：
  1. 執行前驗證輸入（用確定性檢查擋掉危險/無效呼叫，不要指望提示引導）
  2. 執行後檢查結果（品質不夠就地修復或重試）
  3. 回傳值夾帶指引（不只給資料，也給「接下來該怎麼做」的線索）
- **失敗訊息要寫給 agent 看，不是寫給機器看**：例如不要只回 `column 'x' does not exist`，要附上「可用欄位有哪些、建議改用哪個」。每個失敗都是免費的引導機會。
- **成功也要設計回傳值**：不要只回 `{"success": true}`，要帶規模資訊（影響幾筆、動了哪些欄位/區塊），讓 agent 判斷得出動作的影響範圍。
- **大結果要截斷 + 提示**：超過預算就頭尾截斷或寫檔案，並註明「共 N 筆已截斷」，避免把 context 塞爆又讓 agent 誤以為看到全貌。
- **多層 Grader 分工**：能確定性驗證的用程式（快、穩、可重現），無法寫成 assert 的細膩判斷才用 LLM Judge（貴，但能給語意層級的理由）。
- **RAG／搜尋類工具要帶 facets**：只回 top-k 會讓 agent 對資料全貌產生盲點，回傳結果時附上整體分佈統計與下一步搜尋建議。
- **需要獨立審查時，換一顆不同訓練的模型**：agent 自評不是真正獨立的檢查，因為和自己共享同一套訓練與先驗；有需要時才呼叫（使用者觸發 / 規則觸發 / agent 自判），這是工具層最貴的回饋形式，要控制觸發頻率。
- **工具層四原則**：能確定就不用 LLM；語意判斷才用 Judge；回饋必須可行動；驗證邏輯不能拖垮延遲預算。
- **結構性限制**：單步驗證再多次，也不等於整輪任務完成——這件事要留給時機三處理。

---

## (4) 回饋時機二：兩次 model request 之間的訊息注入

原文：https://blog.aihao.tw/2026/06/26/harness-engineering-4-mid-run-injection/

**核心主張**：Agent 一輪執行是「model request → tool call → model request → …」的迴圈，訊息注入只能發生在「工具結果到齊」與「下一次 model request」之間。

- **API 硬性規則**：每個 `tool_use`/`tool_call` 都必須先收到對應的 `tool_result`，中間不能插別的訊息，否則下一次請求會 400。設計任何注入機制前先確保這件事合規。
- **人工介入分兩種**：
  - Steering（方向引導）：新訊息先排隊，等當前工具呼叫全部有結果才注入，已建立的上下文保留。
  - Interrupt（中斷）：主動中止執行中的工具，harness 要幫未完成的 tool_use 補上固定字串（如 `aborted`）維持 API 合法性，再停下等新指令。
  - 人工介入應是補救而非常態；如果經常需要 steering，代表前饋指令（system prompt / 初始任務描述）不夠清楚，應該先補強前饋，而不是依賴事後修正。
- **程式自動注入三類訊息**：背景工具結果（長任務完成時回送）、外部事件（webhook、告警、聊天訊息）、補充脈絡（執行中發現該追加的資訊）。
- **背景工具的標準模式**：工具被呼叫當下先回一個「正在背景執行」的 `tool_result`，讓呼叫立即合法配對、agent 可以先做別的事；背景任務真正完成時再以新訊息注入結果。
- **框架選型**：如果業務需要動態注入（背景任務、外部事件），要挑有支援這個機制的 agent 框架，不要自己刻底層合規邏輯。

---

## (5) 回饋時機三：單輪結束的驗收（Goal）

原文：https://blog.aihao.tw/2026/06/26/harness-engineering-5-goal-and-outcomes/

**核心主張**：agent 覺得「做完了」不代表真的做完，必須對著明確的停止條件驗證，沒過就繼續做——不能只靠模型自我判定。

- **Goal 是持久目標（durable objective）**，好的 Goal 要講清楚三件事：
  - 終態：做完是什麼樣子
  - 證據：用什麼驗證
  - 限制：過程中不能弄壞什麼
  - 模板：`/goal <期望終態> verified by <證據> while preserving <限制>`，並補充可用工具邊界、每輪之間怎麼選下一步、卡住時要回報什麼。
- **實戰技巧**：意圖模糊就先跟 agent 討論收斂成 goal；長任務用進度儀表板（如 `goal.html`）；把「做完」拆成可勾選的 checklist；goal 寫成版本控制的檔案方便重用。完成條件真的模糊，或屬於灰色地帶的研究型工作，就不要硬套二元判斷，先定義可信度分級。
- **三種驗收實作，依獨立性、資訊量、成本各有取捨**：
  1. **自我審計**（如 Codex）：主模型自己宣告完成，靠 prompt 層的 completion audit 強制逐項自查。成本最低、獨立性最低，適合互動式開發、環境回饋密集、開發者能即時盯的場景。
  2. **Transcript 裁判**（如 Claude Code `/goal`）：獨立小模型只讀刪減版 transcript 判定 Y/N + 理由，沒印出來的證據對裁判等於不存在。成本與可靠性的折衷起點，適合無人值守、完成條件能機械化證明的中程任務。
  3. **獨立 Grader / Outcomes**：全新 context 的 grader 像真實使用者一樣操作 artifact（點 UI、打 API、查資料庫），逐條 rubric 判定，抓得出「自評通過但其實是假完成」的狀況。成本最高（數十倍 token、數百倍延遲），適合長期、交付物導向、假性完成代價高的任務。局限是抓不到「產出物完美但過程違規」。
- **收斂成本才是關鍵**：真正決定總成本的是「要跑幾輪才能達到同等可靠度」，不是單次評估的價格；驗證單價越低越能負擔逐輪把關，驗證越貴就越只能驗整個成品。
- **停止條件寫得越具體、越能被機械化證明，就越不需要昂貴的驗證**——這是設計 goal/驗收邏輯時最值得投資的地方。
- 這一層的做法不綁定在「單輪結束」——同一套 pattern（自評／裁判／獨立驗證）可以套用在工具層或外層 loop，粒度由任務決定。

---

## (6) 回饋時機四：外層 Loop（Ralph、Symphony、Cron）

原文：https://blog.aihao.tw/2026/06/26/harness-engineering-6-outer-loop/

**核心主張**：單一 context window 撐不住的任務（跨天、數百步、需要外部事件觸發）需要外層迴圈，把「進度」從 context 移到持久化存儲——agent 會忘，但 repo/資料庫不會。

- **為什麼需要外層迴圈**：context 容量瓶頸（塞滿後表現劣化、失敗推理污染後續判斷）、多任務要各自乾淨的 context、外部事件要靠常駐機制自動觸發而非等人介入。
- **三種實現模式**（依需求選擇，不互斥）：
  - **Ralph**：Bash 無窮迴圈，每圈用同一份 prompt 起全新 agent，靠約定字串判斷是否跳出，配 `max-iterations` 上限。跨圈記憶靠三層持久化：git history（已完成工作）、`progress.txt`（累積的決策/陷阱）、`prd.json`（任務清單與完成狀態）。批評：純蠻力迭代、若無內層驗證機制容易變成「slop in a loop」——外層迴圈是疊加在內層 harness 之上，不能取代。
  - **Symphony**：以看板（有限狀態機）作為 agent 的控制平面，Todo → In Progress → Review → Done，支援 DAG 編排、自開 ticket、交付物是「工作成果證明」而非單純寫完代碼。適合多並行任務、需要人工審批環節的場景。
  - **Cron / Heartbeat**：觸發方式分定時、動態間隔、事件驅動；context 沿用策略分「獨立 context」（每次全新，進度靠外部狀態，如 `/schedule`）與「heartbeat 沿用」（同一 session 定時喚醒，如 `/loop`，記憶連續但 context 會越滾越大）。
- **Goal 與 Loop 職責分離，不要混淆**：Loop 管「何時運行、多久一次」（排程觸發），Goal 管「什麼程度才算完」（終止條件）。只有 Loop 沒 Goal 會無限迭代到圈數上限；只有 Goal 沒 Loop 會被限死在單一 session。兩者可以疊加（loop 控制外圈啟動，goal 控制內圈停止）或串接（loop 找工作、派工後結束，另一個 goal 驅動的任務去完成）。
- **自建任何外層 loop 前要回答三個問題**：觸發機制是什麼？喚醒時要讀哪些 context 外的狀態才能知道進度和新工作？結果要流向哪裡（開 PR、寄簡報、進 triage 收件匣、無異常就歸檔）？
- **實施順序建議**：先確保內層 harness 完善（時機①②③：工具回饋、中段注入、驗收條件），再設計外層 loop；進度務必外化到檔案/資料庫/看板，不要依賴模型記憶；一定要設 max iterations、成本上限、timeout；人仍要在架構決策與安全邊界上把關，外層自動化不等於完全無人。

---

## (7) 進階：自我改進 Harness

原文：https://blog.aihao.tw/2026/06/26/harness-engineering-7-self-improving/

**核心主張**：`agent = fit(model, harness, evals)`。自我改進不是讓 agent 想改什麼就改什麼，必須在嚴格的評測、版本控制、退步防護下進行——評測品質決定 agent 是真的變強還是學會了作弊（Goodhart 陷阱）。

- **受控改版的七項必要條件**：固定評測集、regression set（修好的失敗案例變永久測試）、生產失敗追蹤、版本化管理（prompt/工具/schema 變更可追蹤比較）、晉級關卡（沒改進或有退步就拒絕）、回滾機制、高風險變更要人工審查。
- **三條實踐路徑**：
  1. **生產軌跡 → 新判斷規則**：system prompt 裡的每一條禁止清單，本質上就是一次事故報告。閉環三步驟：發現生產失敗模式 → 沉澱成標註過的資料集與對齊過的 judge → 部署到既有的回饋時機（工具層/單輪驗收/外層迴圈）。
  2. **以 agent 作優化器，透過評測爬坡**：任何能快速評估的指標都能交給 coding agent 迭代改進。配方：蒐集標記資料 → 切分最佳化集/holdout 集（holdout 最重要，代理真實泛化）→ 先測基準 → 每輪只改一個變因 → 驗證新案例通過且舊案例不退步（退步清單留給下一輪）→ 上線前人工看一遍。退步關卡讓改進疊加式上升、不會走回頭路。
  3. **把學到的沉澱成可重用 skill**：10 步檢查清單（SKILL.md 契約、確定性程式碼、單元測試、整合測試、LLM 評測、resolver 觸發條目與評測、DRY 稽核、端到端煙霧測試、規則歸檔）。沒有測試把關的 skill 只是把幻覺寫進記憶，且 skill 太多會讓 resolver 路由失準、彼此腐爛。
- **十層堆疊架構**（分級看自己做到哪一層）：① 穩定基底（釘住模型/runtime/工具/評測切分）② 執行軌跡完整記錄 ③ 學習外部化到可回滾位置 ④ 依根因分群失敗 ⑤ 提案引擎轉成可驗證候選改動 ⑥ **關卡（held-out 驗證/退步防護，整套堆疊的樞紐，不可省略）** ⑦ 版本與回滾 ⑧ 依任務類別分流不同 harness 變體 ⑨ 量測 worker agent 實際效益（不是量更新器本身）⑩ 選配的模型權重更新（前九層穩定後才考慮）。
- **防範 Goodhart 陷阱**：agent 可能為了刷高單一指標而移除真功能；改了 harness 不代表真的有效益，要實測 worker agent 的表現是否真的變好；所有自動化機制都要預留人工審查關卡。工程師的角色從逐步操作，轉為定義「什麼叫做好」並守住這個定義。

---

## (8) 收尾：會過期的 Harness（Model-Harness-Fit）

原文：https://blog.aihao.tw/2026/06/26/harness-engineering-8-model-harness-fit/

**核心主張**：Harness 會跟模型深度綁定，且會隨模型世代過期——這對「什麼值得現在投資、什麼不值得」有直接影響。

- **Harness 與模型的綁定**：模型在 post-training 階段會針對特定工具格式（例如 `apply_patch` vs `edit_file`）、citation 標籤、skill 契約進行訓練，這些會內化成模型的本能。同一份 harness 換一個模型，效果就會不同，因為綁定發生在很底層的慣例層級。
- **Model-Harness-Task 四層模型**：任務分布（選什麼任務）→ workflow 層（編排與 skill，**自建 agent 的優勢所在**）→ 內層 tool loop（工具描述、解析，原廠優勢）→ 模型權重本身（原廠優勢）。自建 agent 要打贏原廠,靠的是針對特定任務優化 workflow 層,而不是複製一份通用 harness。
- **Harness 會過期的模式**：補強某代模型短處的元件，模型變強後就變成死碼或阻力（例如 `TodoWrite` 在 Claude Code 中逐步被 Tasks 系統取代）；context reset、模型提醒等腳手架，隨新模型版本可以拿掉;廠商也會把某些 harness 做法直接訓練進新模型(如 `apply_patch` 格式內化)。
- **不會過期的要素**：定義「什麼叫做好」與驗證達標的 eval/judge 系統不會過期——這是穿越模型世代的基本功，模型再強也需要人類定義目標。**這也呼應第 5、7 篇的結論：投資在評測與驗收邏輯，比投資在補模型短處的腳手架更划算。**

---

## 建立新 agent / tool 時的檢查清單

濃縮自上述七篇，動手前快速自問：

1. **這個元件在解決哪一層的問題**：prompt、context，還是 harness（行動迴圈的約束/修正）？不要用 prompt 硬解決本質是 harness 缺失的問題。
2. **工具的輸入驗證、輸出檢查、回傳值指引，三段都做了嗎**？失敗訊息是不是寫給 agent 看的（附帶可行動的修正建議），而不只是原始錯誤訊息？
3. **成功回傳有沒有帶規模/影響範圍**？大結果有沒有截斷並註明？
4. **需要人工或程式在執行中插話嗎**？是否確認過所有 tool_result 已到齊才注入，符合 API 合規？如果常常需要 steering，是不是該回頭補強前饋指令？
5. **這一輪「做完了沒」誰來判？** 自評、獨立裁判、還是獨立 grader？選擇是否對應到任務的風險與驗證成本？停止條件是否寫成可機械化證明的形式？
6. **這個任務會不會超過單一 context window**？如果會，進度是否已經外化到檔案/資料庫，而不是靠模型記住？
7. **如果要讓這個元件自動改進**：有沒有固定評測集、regression set、晉級關卡、回滾機制？高風險變更是否強制人工審查？
8. **這個 harness 元件是在補模型短處，還是在做「定義好壞標準」的事**？前者要預期會過期，後者才是長期資產——評測與驗收邏輯優先投資。
9. **（對照本專案目標）這是平台通用能力，還是場景邏輯**？回饋迴路、驗收邏輯等 harness 元件若具備通用性，應該放進基礎建設層（如 gateway/、service registry），而不是寫死在單一場景的 workflow 節點裡。

## 新增 MCP server 的檢查清單

MCP server 的持續擴充會是這個平台往後最常做的主線工程工作，新工具的驗收標準不是「會動」，而是回傳值有沒有構成 agent 的回饋迴路（呼應上面第 2 點）：

1. **成功時**，回傳值有沒有讓 agent 知道下一步能做什麼？（例如 `browse_semantic_memory` 回 children/siblings/parent，是為了讓模型自己決定往下鑽或回頭，不是只回一坨資料）
2. **失敗時**，錯誤訊息是寫給 agent 看的，還是寫給人看的？工具本體要用 `@guarded_tool`（見 [../mcp_servers/tool_errors.py](../mcp_servers/tool_errors.py)）包住，讓未分類的例外變成 `ToolDependencyError` 而不是 raw traceback；能區分「retry 換輸入可能有用」（`ToolInputError`）跟「retry 現在沒用」（`ToolDependencyError`）就分開分類。
3. **查無結果時**，回的是空的，還是「明確的空」？（`lookup` 的 `_UNKNOWN_PROFILE` 帶 `note: "查無資料，非監控清單公司"`，而不是回 `{}`——這一行是 agent 能不能從查無資料中恢復的分界）

新增一個 server 固定三步，不含 client.py（那是 [MCPClient](../mcp_servers/base_client.py) 唯一的 client 實作，per-server 不再各自維護一份）：`mcp_servers/<name>/server.py` → `policy.yaml` 的 `servers:` 註冊 → `policy.yaml` 的 role/principal 授權。寫完別急著整條鏈路跑一次——先補一份 `mcp_servers/<name>/smoke_test.py`（見 [testing.md](testing.md)），直接用 `MCPClient` 連上 server 的 stdio 子行程驗上面三件事，不碰 LLM/agent/gateway，秒級回饋；端到端 smoke test 留給「這個工具接進真實鏈路後行為不變」這一層判斷。
