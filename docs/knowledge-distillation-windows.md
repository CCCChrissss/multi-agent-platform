# Windows 知識蒸餾與長期記憶操作指南

> [!IMPORTANT]
> 本文件以目前 repository 程式碼與 Windows 實機操作為準。核心 P0–P5 機制已存在；actor-distinction demo 已實際進行到 procedural review／candidate staging，但尚未確認 production procedural 最終核准與完整回歸結果，因此不標示為完整閉環成功。原作者的設計與 macOS 驗證紀錄保留在 [knowledge-distillation-plan.md](knowledge-distillation-plan.md) 與 [long-term-memory-plan.md](long-term-memory-plan.md)。

> [!WARNING]
> 2026-09-01 已移除所有雲端 API key。目前仍可進行 DB 唯讀查詢，以及不需模型的 episodic 人工檢視；`distill_procedural`、Gemini `run_eval` 與 `review_memory` 的模型評測部分暫時不能執行。不要填假 credential，也不要刪除 gateway 中保留的 provider alias。

## 1. 先理解三種記憶

| 類型 | 本專案用途 | 目前主要來源 | 是否會影響 agent |
|---|---|---|---|
| Semantic | 保單條款、公司別名等可查證知識 | seed script 或人工寫入 | `active` 時可透過固定注入或 memory MCP 查詢 |
| Episodic | 某次 workflow 的輸入與輸出案例 | `orchestrator/memory_writer.py` | 不直接進 agent prompt；只有 `active` 案例可成為蒸餾原料 |
| Procedural | 可直接加入 system prompt 的判斷規則 | `scripts/distill_procedural.py` | 只有 `active` 規則會由 `inject_procedural()` 注入 agent |

Checkpoint、`orchestrator_runs` 與長期記憶不是同一件事：

- checkpoint／`orchestrator_runs`：記錄某一次 workflow 跑到哪裡。
- long-term memory：記錄跨多次 workflow 可重複使用的知識、案例與規則。

## 2. 安全關卡與資料流

```text
workflow step 完成
        |
        v
memory_writer 寫入 pending episodic
        |
        v
人工 review_episodic：確認／修正案例
        |
        v
active episodic
        |
        v
distill_procedural 用 gemini-cheap 歸納
        |
        v
pending procedural
        |
        +--> baseline 與 eval tenant candidate 比較
        |
        v
人工 review_memory：approve／reject／edit
        |
        v
active procedural --> inject_procedural() 注入 agent
```

目前的狀態規則：

- `pending`：等待人工審核；`recall()` 與 `browse()` 都看不到。
- `active`：正式可讀。active episodic 可被蒸餾，active procedural 可進入 agent prompt。
- procedural reject：`review_memory.py` 會透過正式 `forget()` 刪除候選並留下 audit log。
- episodic reject：目前不刪除，會維持 `pending`，下次審核仍會出現。這是現行程式行為，不等同於已記錄負面訊號。

`open_agent_memory()` 每次開 store 都會執行冪等的 `backfill_missing_status()`；舊資料若沒有 `status`，會補成 `active`。不要直接用 SQL 改 status，否則會繞過 memory policy 與 `call_log` 稽核。

## 3. 相關程式碼地圖

| 檔案 | 責任 |
|---|---|
| [../persistence/memory_store.py](../persistence/memory_store.py) | 建立 PostgreSQL `AsyncPostgresStore`，embedding 固定走 LiteLLM 的 `local-embed` |
| [../persistence/memory.py](../persistence/memory.py) | namespace、`status` gate、remember/recall/browse/edit/forget 與 audit |
| [../persistence/memory_lifespan.py](../persistence/memory_lifespan.py) | 共用 store lifespan、schema setup 與 status backfill |
| [../persistence/memory_prompt.py](../persistence/memory_prompt.py) | 把 active procedural 規則注入 agent prompt |
| [../orchestrator/memory_writer.py](../orchestrator/memory_writer.py) | 依 workflow 的 `memory_write` 宣告，把成功 step 寫成 pending episodic |
| [../scripts/review_episodic.py](../scripts/review_episodic.py) | 人工核准或修正 pending episodic |
| [../scripts/distill_procedural.py](../scripts/distill_procedural.py) | 讀 active episodic，使用 `gemini-cheap` 產生 pending procedural |
| [../scripts/stage_candidate_for_eval.py](../scripts/stage_candidate_for_eval.py) | 將候選與既有 active 規則放入一次性 `eval` tenant |
| [../evals/run_eval.py](../evals/run_eval.py) | 用 `evals/check_cases.yaml` 執行 baseline 或 candidate 評測 |
| [../scripts/review_memory.py](../scripts/review_memory.py) | 整合 stage、前後評測、evidence 診斷與人工 procedural 決策 |
| [../mcp_servers/policy.yaml](../mcp_servers/policy.yaml) | 定義 `check`、`memory_writer`、`distiller` 的 memory namespace 權限 |

## 4. Windows 實機資料與查詢原則

2026-08-31 開始 actor-distinction demo 前的唯讀盤點結果：

| Namespace／狀態 | 數量 |
|---|---:|
| `_global.semantic.*`／`active` | 59 |
| `default.episodic.*`／`pending` | 3 |
| `default.episodic.*`／`active` | 0 |
| `default.procedural.*` | 0 |

當時三筆 pending episodic 分布在：

- `default.episodic.stt_check_notify.check`：2 筆。
- `default.episodic.stt_exclusion_notify.check`：1 筆。

這張表只保留為 demo 起點。後續 procedural review 已看到：

- `eval.procedural.stt_exclusion_notify.check`：`active`，供 candidate 對照測試使用。
- `default.procedural.stt_exclusion_notify.check`：`pending`，尚未成為 production active 規則。

兩筆是不同 tenant 的預期隔離機制，不是重複 production rule。這次文件整併沒有重新查詢 DB 或服務，因此下方唯讀 SQL 才是每次操作前判斷現況的依據。

## 5. 每個階段需要哪些服務

| 操作 | PostgreSQL | Ollama 11434 | LiteLLM 4000 | 8001／8002／8003 | Gemini key | 是否改資料 |
|---|---:|---:|---:|---:|---:|---|
| 查 DB | 必須 | 不需要 | 不需要 | 不需要 | 不需要 | 否 |
| `review_episodic` | 必須 | 通常不需要 | 通常不需要 | 不需要 | 不需要 | approve/edit 會修改 episodic |
| `distill_procedural` | 必須 | 必須，供 `local-embed` | 必須 | 不需要 | 必須，供 `gemini-cheap` | 寫入 pending procedural |
| `run_eval` | 必須 | 視記憶操作而定，建議啟動 | 必須 | 不需要 | 預設模型需要 | 會寫 call log；開 store 時可能 backfill status |
| `review_memory` | 必須 | 必須，供 candidate embedding | 必須 | 不需要 | 預設模型需要 | 會改 eval tenant；approve/reject 會改正式 procedural |
| 完整 workflow 產生新 episodic | 必須 | 必須 | 必須 | 必須 | 依 workflow model | 會寫 run、event、call log 與 pending episodic |

知識蒸餾 CLI 不需要啟動 event-driven workers、STT、notified 或 Agent Runtime。需要重新執行完整 workflow 時，啟動順序以 [windows-setup.md](windows-setup.md) 為唯一詳細來源。

## 6. Windows PowerShell 操作順序

以下指令都要在新的 PowerShell session 先設定 repository。其他使用者必須把 `$RepoRoot` 改成自己的絕對路徑。

```powershell
$RepoRoot = 'D:\Projects\multi-agent平台架設\multi-agent-platform'
if (-not (Test-Path -LiteralPath $RepoRoot -PathType Container)) {
    throw "找不到 repository：$RepoRoot"
}
Set-Location -LiteralPath $RepoRoot

$env:PYTHONUTF8 = '1'
$env:UV_CACHE_DIR = 'D:\Projects\multi-agent平台架設\.uv-cache'

if (-not (Test-Path -LiteralPath '.\.venv\Scripts\python.exe' -PathType Leaf)) {
    throw '找不到專案 Python，請先依 README 完成 uv sync。'
}
if (-not (Test-Path -LiteralPath '.env' -PathType Leaf)) {
    throw '找不到 .env，請先依 README 建立環境設定。'
}
```

### 6.1 確認依賴狀態

```powershell
Get-Service -Name 'postgresql*' -ErrorAction SilentlyContinue |
    Select-Object Name, Status, DisplayName

11434, 4000 | ForEach-Object {
    [pscustomobject]@{
        Port = $_
        Listening = Test-NetConnection -ComputerName 127.0.0.1 -Port $_ -InformationLevel Quiet
    }
}
```

需要蒸餾或評測時，`11434` 與 `4000` 應為 `True`。本文件整理階段沒有啟動它們；啟動方式使用 README 的 Terminal A 完整區塊，或在後續另行驗證最小服務啟動方式。

確認 LiteLLM alias：

```powershell
(Invoke-RestMethod -Uri 'http://127.0.0.1:4000/v1/models').data.id
```

至少應包含 `gemini-cheap` 與 `local-embed`。alias 出現在清單只代表設定已載入；仍要具備有效 `GEMINI_API_KEY`、Ollama 的 `bge-m3` 模型及可連線 provider。

目前沒有 `GEMINI_API_KEY`，所以這項檢查只能確認 alias 已載入；不能把 `gemini-cheap` 出現在清單解讀為可執行蒸餾。

### 6.2 先人工審核 episodic

除外責任 workflow：

```powershell
.\.venv\Scripts\python.exe -m scripts.review_episodic `
    --scope stt_exclusion_notify/check
```

台積電 workflow：

```powershell
.\.venv\Scripts\python.exe -m scripts.review_episodic `
    --scope stt_check_notify/check
```

互動選項：

- `a`：核准，改成 `active`。
- `e`：先輸入有效 JSON output，再核准。
- `r`：現行行為是保留 `pending`，不是刪除。
- `s`：跳過並保留 `pending`。

可以加 `--key <pending-key>`，一次只審核一筆。核准前必須人工確認 output 的業務判斷正確，不能因為 workflow status 是 `ok` 就直接核准。

### 6.3 建立正式 baseline

```powershell
.\.venv\Scripts\python.exe -m evals.run_eval `
    --tenant default `
    --repeats 3
```

這會讀 [../evals/check_cases.yaml](../evals/check_cases.yaml)，呼叫真實 exclusion judge 與模型。`involves_exclusion` 是硬性 pass/fail；`matched_articles` 目前只作為觀察資訊。

### 6.4 從 active episodic 蒸餾候選規則

```powershell
.\.venv\Scripts\python.exe -m scripts.distill_procedural `
    --scope stt_exclusion_notify/check `
    --limit 20
```

成功時會印出 `pending-<uuid>`。候選只會寫成 `pending`，不會立刻影響 production agent。若出現 `no episodic memories`，先回到 6.2 核准正確案例。

### 6.5 評測並人工審核 procedural

最簡單且較不容易漏步驟的方式：

```powershell
.\.venv\Scripts\python.exe -m scripts.review_memory `
    --scope stt_exclusion_notify/check `
    --repeats 3
```

只審一筆：

```powershell
$CandidateKey = 'pending-REPLACE_WITH_REAL_UUID'
.\.venv\Scripts\python.exe -m scripts.review_memory `
    --scope stt_exclusion_notify/check `
    --repeats 3 `
    --key $CandidateKey
```

這個 CLI 會依序：

1. 將 default tenant 的既有 active procedural 規則與候選規則 stage 到 `eval` tenant。
2. 跑 baseline 與 candidate 的 holdout/regression 比較。
3. 另外重跑候選 evidence 指向的 episodic 案例，作為診斷資訊。
4. 等待人工選擇 approve、reject、edit 或 skip。

`evidence diagnostic` 不能直接當 pass/fail，因為 episodic 原始 output 不必然是人工標註的 ground truth。至少要確認正式 holdout/regression 不退步，並由人工判斷規則內容與 evidence 是否合理。

### 6.6 需要拆開檢查時

```powershell
$CandidateKey = 'pending-REPLACE_WITH_REAL_UUID'

.\.venv\Scripts\python.exe -m scripts.stage_candidate_for_eval `
    --key $CandidateKey `
    --scope stt_exclusion_notify/check

.\.venv\Scripts\python.exe -m evals.run_eval `
    --tenant default `
    --repeats 3

.\.venv\Scripts\python.exe -m evals.run_eval `
    --tenant eval `
    --repeats 3
```

拆解版適合診斷，不建議直接用 SQL approve/reject。正式人工決策仍優先使用 `review_memory.py`，才能保留 policy 檢查與 audit log。

## 7. 在 pgAdmin 查記憶資料

以下 SQL 在 `agent_architecture_test` 的 Query Tool 執行。

依 kind 與 status 統計：

```sql
SELECT
    split_part(prefix, '.', 2) AS kind,
    COALESCE(value ->> 'status', '<missing>') AS status,
    count(*) AS item_count
FROM store
GROUP BY 1, 2
ORDER BY 1, 2;
```

列出待審 episodic：

```sql
SELECT
    prefix,
    key,
    value ->> 'status' AS status,
    value -> 'content' AS content,
    value ->> 'source_thread_id' AS source_thread_id,
    updated_at
FROM store
WHERE split_part(prefix, '.', 2) = 'episodic'
  AND value ->> 'status' = 'pending'
ORDER BY updated_at DESC;
```

列出 procedural 候選與 evidence：

```sql
SELECT
    prefix,
    key,
    value ->> 'status' AS status,
    value -> 'content' ->> 'rule' AS rule,
    value -> 'evidence' AS evidence,
    value ->> 'rationale' AS rationale,
    updated_at
FROM store
WHERE split_part(prefix, '.', 2) = 'procedural'
ORDER BY updated_at DESC;
```

SQL 查詢只用於觀察。status 轉換、核准與拒絕應使用 repository CLI，不要直接 `UPDATE` 或 `DELETE`。

## 8. 稽核與排錯

查蒸餾、embedding、memory edit/forget 的 call log：

```sql
SELECT
    created_at,
    thread_id,
    node_name,
    kind,
    name,
    is_error,
    request,
    response
FROM call_log
WHERE thread_id LIKE 'distill-%'
   OR thread_id LIKE 'review-%'
   OR thread_id LIKE 'stage-eval-%'
   OR kind = 'memory'
ORDER BY created_at DESC
LIMIT 200;
```

常見問題：

| 現象 | 先檢查 |
|---|---|
| `no episodic memories` | 該 scope 是否有 `active` episodic；pending 不會被蒸餾器讀取 |
| 連不上資料庫 | PostgreSQL service、`.env` 的 `PERSISTENCE_DATABASE_URL`、資料庫名稱 |
| `gemini-cheap` 401／403／provider error | `.env` 的 `GEMINI_API_KEY`，以及 LiteLLM 是否由同一份 `.env` 啟動 |
| `local-embed` 失敗 | 11434、Ollama 的 `bge-m3`、4000 與 LiteLLM alias |
| candidate 評測與 baseline 完全相同 | 不代表程式錯誤；規則可能沒有改善案例，需查看 evidence diagnostic |
| pending 規則沒有影響 workflow | 正常；只有 approve 後的 `active` procedural 才能被 recall |
| 評測結果每次不同 | LLM 有抽樣不確定性；至少使用 `--repeats 3`，不要以單次結果晉級 |

## 9. 目前仍未完成的驗證

- actor-distinction demo 已進行到 candidate staging／procedural review；exact episodic 數量與狀態必須重新查 DB。
- 已看到 `default` pending procedural 與 `eval` active staged candidate，但尚未確認 production procedural 已 approve 成 `active`。
- 尚未確認完整 baseline/candidate/review/production approve 與回歸結果全部符合成功條件。
- Demo UI 的 pending queue、distill job 與 review API 尚未在目前 Windows 環境重跑。
- 尚未確認一條真實有效規則經 approve 後能提高回歸測試且不造成 holdout 退步。

後續實機階段應一次只完成一個關卡，記錄輸出與 DB 變化後再更新本文件，不預先宣稱完整流程成功。
