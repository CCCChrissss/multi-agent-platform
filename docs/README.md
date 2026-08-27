# docs/ 文件索引

每份文件在做什麼、什麼時候該看。文件目標是同時支援原作者的 macOS / Bash / Claude Code 流程，以及目前實機的 Windows / PowerShell / Codex 流程。Windows 使用者可先讀 [current-windows-status.md](current-windows-status.md)，再依需求進入安裝、測試或架構文件。

文件中的 `donydony228/agent-architecture` issue 連結是移植前的歷史決策來源；新的待辦與
修改只追蹤在 [本 repository issues](https://github.com/CCCChrissss/multi-agent-platform/issues)。

## 上手

| 文件 | 在講什麼 |
|---|---|
| [current-windows-status.md](current-windows-status.md) | **目前唯一實機狀態基準**：已安裝、曾驗證、目前停止、尚未驗證、模型缺口與 CI 已知失敗 |
| [onboarding.md](onboarding.md) | 接手新人看的程式追蹤路徑；完整語音執行尚未在目前 Windows 環境重驗 |
| [windows-setup.md](windows-setup.md) | Windows / PowerShell 從零安裝、五個服務、workflow 選擇、workers、trigger、thread_id、log 與排查 |
| [setup.md](setup.md) | 雙平台疑難排解：Windows 的 CP950、PostgreSQL、D 槽模型與 Breeze，以及 macOS 原有的 Homebrew / pgvector / Ollama 排錯 |
| [testing.md](testing.md) | Windows / PowerShell 與 macOS / Bash 的 smoke test 分層、前置條件、本機驗證狀態與上游測試流程 |
| [observability.md](observability.md) | 怎麼查一次執行的稽核歷史/執行狀態，Postgres 各張表存什麼、`store` 跟 checkpoint 的差別 |

## 文件狀態規則

- 操作指令必須標明 Windows / PowerShell 或 macOS / Bash，不能用其中一種覆寫另一種。
- Windows 指令以目前 repository 與本機驗證結果為準；標示「原作者流程」或「尚未重驗」的 macOS 內容繼續保留，但不宣稱由目前維護者實測通過。
- `AGENTS.md` 是現行 Codex 協作規範；`CLAUDE.md` 保留原作者的平台目標與 Claude Code 專案脈絡。
- 每完成一個可驗證階段，再更新狀態文件與相應操作手冊，不預先把未做事項寫成已完成。

## 平台核心設計

| 文件 | 在講什麼 |
|---|---|
| [agent-api-contract.md](agent-api-contract.md) | agent 的請求/回應 envelope 長什麼樣，以及為什麼刻意跟傳輸層（function call / event / HTTP）脫鉤 |
| [event-driven-multi-agent-coordination-plan.md](event-driven-multi-agent-coordination-plan.md) | 事件驅動模式的完整設計：Master Agent + Worker + Event Bus 怎麼協作，取代 `simple_pipeline.py` 單一 process 同步執行 |
| [generic-agent-runtime-plan.md](generic-agent-runtime-plan.md) | 讓「新增一個 agent」從寫程式碼變成寫設定：agent 的身分（prompt/model/tools）怎麼變成可被 UI 組裝的宣告式資料 |
| [ui-backend-integration-plan.md](ui-backend-integration-plan.md) | Demo UI 如何透過正式 workflow/policy loader 寫入設定、熱載入 agent，及前後端的狀態契約 |
| [harness-engineering-principles.md](harness-engineering-principles.md) | 新增/調整 agent、tool 前要對照的檢查清單：agent 需要回饋迴路（工具回傳值、執行中注入、單輪驗收、外層 loop）而非完美提示 |

## 長期記憶與知識蒸餾

| 文件 | 在講什麼 |
|---|---|
| [long-term-memory-plan.md](long-term-memory-plan.md) | 長期記憶（跨執行的知識）導入計畫：跟 checkpointer 的差別、要怎麼做成同步/事件驅動兩條路徑都能共用的獨立元件 |
| [knowledge-distillation-plan.md](knowledge-distillation-plan.md) | episodic 經驗怎麼蒸餾成 procedural 規則、人審核的品質關卡（M5）怎麼設計 |
| [distill-ui-plan.md](distill-ui-plan.md) | 記憶蒸餾審核台的 pending queue、前後比較、人工核准/拒絕與 smoke test 邊界 |

## 除外責任示範場景

| 文件 | 在講什麼 |
|---|---|
| [exclusion-scenario-plan.md](exclusion-scenario-plan.md) | 場景設計本身：為什麼要換場景（壓測 agent 能不能不把整份保單塞進 context、自己一層層鑽找答案）、`browse()` 漸進式揭露怎麼做 |
| [exclusion-actor-distinction-demo.md](exclusion-actor-distinction-demo.md) | 一組誤判案例，示範知識蒸餾閉環完整跑一輪（誤判 → 人審 episodic → 蒸餾 → 人審 procedural → 連沒見過的案例也判對）的設計與跑法 |
| [exclusion-episodic-cases.md](exclusion-episodic-cases.md) | `scripts/seed_exclusion_episodic_examples.py` 寫入的 6 筆種子案例清單——蒸餾器唯一的輸入來源，要加案例或查蒸餾出的規則從哪來先看這裡 |
