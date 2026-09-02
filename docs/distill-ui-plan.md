# 記憶蒸餾審核 UI：現況與驗收契約

> [!NOTE]
> 本文件保留功能設計與既有驗收契約；Demo UI 尚未在目前 Windows / PowerShell 環境重新執行。CLI 操作主線見 [knowledge-distillation-windows.md](knowledge-distillation-windows.md)，現行實機狀態見 [current-windows-status.md](current-windows-status.md)。

本文件描述 [demo/index.html](../demo/index.html) 與 [demo/api.py](../demo/api.py) 已落地的
memory review UI。核心記憶語意仍以 [knowledge-distillation-plan.md](knowledge-distillation-plan.md)
和 [long-term-memory-plan.md](long-term-memory-plan.md) 為準；UI 只把既有 CLI 流程搬到
可檢視、可審核的介面，不另建儲存邏輯。

## 1. 範圍與安全邊界

審核台處理兩種 `pending` 項目：workflow 產生的 episodic 經驗，以及 distiller 產生的
procedural 候選規則。`pending` 不得被 production recall 使用；只有人員 approve 後才
轉為 active。Reject、edit、approve 都必須沿用正式 memory policy 與 audit context。

## 2. UI 資訊架構

### 2.1 待審數量 badge

Agent 卡片顯示待審數量，資料來自後端 pending queue。Badge 是提示，不是授權；使用者
仍需進入審核面板查看 scope、evidence 與評測結果。

### 2.2 Episodic / Procedural 分頁

- Episodic 分頁讓使用者核准、編輯或拒絕單次案例。
- Procedural 分頁讓使用者先從已核准 episodic 蒸餾候選規則，再比較規則啟用前後的
  評測結果，最後核准或拒絕。

兩種記憶不可混在同一個不標示 kind 的清單，避免把案例誤當 production 規則核准。

### 2.3 Pending queue

`GET /memory/pending` 依 `scope` 與 `kind` 讀取正式 store。UI 必須顯示 key、內容、
evidence、狀態與可編輯欄位；重新整理後的結果要以後端為準。

### 2.4 蒸餾工作

`POST /memory/distill` 啟動既有 distillation 流程，`GET /memory/job/{job_id}` 查進度。
模型或外部服務錯誤必須以失敗狀態回傳，不能在 UI 建立假的候選規則。

### 2.5 人工決策

Episodic 與 procedural 各自使用對應 approve/reject endpoint。若核准時修改文字，後端
必須以 edited 狀態記錄，保留「模型原始提案」與「人員最終決策」的可稽核差異。

### 2.6 規則前後比較

`POST /memory/candidate/{key}/evaluate` 呼叫既有 compare 邏輯，回傳 baseline 與候選規則
加入後的結果。[demo/api.py](../demo/api.py) 只增加 UI 顯示需要的 evidence case 摘要，
不改變 [scripts/review_memory.py](../scripts/review_memory.py) 的評測判斷。模型 override
是診斷用途，使用者仍要理解不同模型可能有 ceiling effect。

## 3. API 對應

| 用途 | Endpoint |
|---|---|
| 待審清單 | `GET /memory/pending` |
| 評測案例 | `GET /memory/eval-cases` |
| 啟動蒸餾 | `POST /memory/distill` |
| 查工作狀態 | `GET /memory/job/{job_id}` |
| 比較候選規則 | `POST /memory/candidate/{key}/evaluate` |
| 核准/拒絕 procedural | `POST /memory/candidate/{key}/approve|reject` |
| 核准 episodic | `POST /memory/episodic/{key}/approve` |

實際 request/response schema 以 [demo/api.py](../demo/api.py) 的 Pydantic models 與 route
handler 為準。

## 4. 驗證

Windows / PowerShell 需要先安裝專案相依套件並確認 PostgreSQL 可連線。在 repository 根目錄執行：

```powershell
$RepoRoot = 'D:\Projects\multi-agent平台架設\multi-agent-platform'
if (-not (Test-Path -LiteralPath $RepoRoot -PathType Container)) {
    throw "找不到 repository：$RepoRoot"
}
Set-Location -LiteralPath $RepoRoot

$env:PYTHONUTF8 = '1'
.\.venv\Scripts\python.exe -m demo.distill_api_smoke_test
```

這支 smoke test 會在隔離 scope 寫入假 pending procedural、經 API handler 核准、確認
可被 active recall 讀回，再於 `finally` 清除測試資料。它不呼叫 LLM，也不代表完整
compare/eval 流程已通過；完整流程仍需 API key、LiteLLM 與代表性 eval cases。
