# docs/ 文件索引

每份文件在做什麼、什麼時候該看。安裝步驟本身在專案根目錄的 [README.md](../README.md)，這裡只收設計/機制文件。

## 上手

| 文件 | 在講什麼 |
|---|---|
| [onboarding.md](onboarding.md) | 接手新人看的：跟著 `stt_check_notify` 一次真實執行，從 workflow 定義 YAML 一路追到最底層的 MCP 呼叫，八步走完分層架構圖全部六層 |
| [setup.md](setup.md) | 安裝疑難排解——根目錄 README 的「從零開始安裝」是 happy path，這份收實際會卡住的地方（pgvector 編譯、Postgres 連線、Ollama 撞 port） |
| [testing.md](testing.md) | 怎麼驗證改動：沒有 pytest，全部是手動跑的 smoke test，分「單一 MCP server」跟「完整 pipeline」兩層，含記憶蒸餾 pipeline 的手動試跑步驟 |
| [observability.md](observability.md) | 怎麼查一次執行的稽核歷史/執行狀態，Postgres 各張表存什麼、`store` 跟 checkpoint 的差別 |

## 平台核心設計

| 文件 | 在講什麼 |
|---|---|
| [agent-api-contract.md](agent-api-contract.md) | agent 的請求/回應 envelope 長什麼樣，以及為什麼刻意跟傳輸層（function call / event / HTTP）脫鉤 |
| [event-driven-multi-agent-coordination-plan.md](event-driven-multi-agent-coordination-plan.md) | 事件驅動模式的完整設計：Master Agent + Worker + Event Bus 怎麼協作，取代 `simple_pipeline.py` 單一 process 同步執行 |
| [generic-agent-runtime-plan.md](generic-agent-runtime-plan.md) | 讓「新增一個 agent」從寫程式碼變成寫設定：agent 的身分（prompt/model/tools）怎麼變成可被 UI 組裝的宣告式資料 |
| [harness-engineering-principles.md](harness-engineering-principles.md) | 新增/調整 agent、tool 前要對照的檢查清單：agent 需要回饋迴路（工具回傳值、執行中注入、單輪驗收、外層 loop）而非完美提示 |

## 長期記憶與知識蒸餾

| 文件 | 在講什麼 |
|---|---|
| [long-term-memory-plan.md](long-term-memory-plan.md) | 長期記憶（跨執行的知識）導入計畫：跟 checkpointer 的差別、要怎麼做成同步/事件驅動兩條路徑都能共用的獨立元件 |
| [knowledge-distillation-plan.md](knowledge-distillation-plan.md) | episodic 經驗怎麼蒸餾成 procedural 規則、人審核的品質關卡（M5）怎麼設計 |

## 除外責任示範場景

| 文件 | 在講什麼 |
|---|---|
| [exclusion-scenario-plan.md](exclusion-scenario-plan.md) | 場景設計本身：為什麼要換場景（壓測 agent 能不能不把整份保單塞進 context、自己一層層鑽找答案）、`browse()` 漸進式揭露怎麼做 |
| [exclusion-actor-distinction-demo.md](exclusion-actor-distinction-demo.md) | 一組誤判案例，示範知識蒸餾閉環完整跑一輪（誤判 → 人審 episodic → 蒸餾 → 人審 procedural → 連沒見過的案例也判對）的設計與跑法 |
| [exclusion-episodic-cases.md](exclusion-episodic-cases.md) | `scripts/seed_exclusion_episodic_examples.py` 寫入的 6 筆種子案例清單——蒸餾器唯一的輸入來源，要加案例或查蒸餾出的規則從哪來先看這裡 |
