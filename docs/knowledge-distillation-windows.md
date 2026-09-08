# 知識蒸餾與人工審核（Windows / VS Code）

先依 [Windows 手冊](windows-setup.md) 完成自己的 Python、DB、pgvector、Ollama 與 LiteLLM。
不需要 Codex。歷史實機資料見 [歷史快照](current-windows-status.md)，不能當成自己資料庫已有的內容。

## 1. 三種記憶與關卡

| 類型 | 來源 | 作用 |
|---|---|---|
| semantic | 保單 seed 等參考資料 | 提供工具查證內容 |
| episodic | memory-writer 記錄成功 step | 先 pending，人工確認／修正後 active，才可作為蒸餾素材 |
| procedural | distiller 歸納候選規則 | 先 pending，經比較、人工核准後 active，才注入 prompt |

Checkpoint 和 orchestrator_runs 是單次執行狀態，不是長期記憶。
流程為：完成 workflow → pending episodic → 人工審核 → active episodic →
蒸餾 → pending procedural → baseline/candidate/evidence 比較 → 人工決定。

`open_agent_memory()` 會建立必要 schema，並為缺少 status 的舊資料補上 status；
因此「只是啟動 CLI」也可能有初始化寫入。請使用自己的開發 DB。
`review_memory` 的 reject 會刪除候選並留下 audit；episodic 未核准時目前仍留在 pending。
不要直接改 SQL status 繞過授權與稽核。

## 2. 功能需要哪些服務與模型

| 操作 | 前置條件 | 寫入 |
|---|---|---|
| psql 查詢 | PostgreSQL | 以下 SELECT 不寫入 |
| review_episodic | PostgreSQL；編輯內容重建 embedding 時需 local-embed | 核准／修正，可能初始化與 backfill |
| distill_procedural | PostgreSQL、LiteLLM、bge-m3，以及選定 chat alias 的 provider | pending procedural / call log |
| run_eval / review_memory | PostgreSQL、LiteLLM、bge-m3，以及選定 check 模型 | call log、eval staging；人工核准／拒絕修改記憶 |
| workflow 累積案例 | 完整 services / workers | run、event、checkpoint、call log、pending episodic |

蒸餾 CLI 預設 `local-qwen3`，透過 Ollama 在地端執行，不需要雲端 API key。
可用 `--model` 指定其他已配置的 chat alias；若顯式選擇雲端 alias，才需要對應 key。
使用本機模型不代表候選品質已驗證，仍必須經過評測與人工審核。

`run_eval` / `review_memory` 未指定 `--model` 時，依實際 workflow check 模型；
目前除外責任 YAML 是 `local-qwen3`。不要把蒸餾模型與被改善的 check 模型混為一談。
UI 的蒸餾 API 呼叫同一個 distiller 預設，因此也使用 `local-qwen3`。

## 3. 準備案例與 baseline

在 repository 根目錄的 VS Code PowerShell：

```powershell
$env:PYTHONUTF8 = '1'
.\.venv\Scripts\python.exe -m scripts.review_episodic --scope stt_exclusion_notify/check
.\.venv\Scripts\python.exe -m evals.run_eval --tenant default --repeats 3 --model local-qwen3
```

前置條件：已有除外責任 semantic seed，以及完整 workflow 產生的 pending episodic。
空資料庫沒有案例是正常情況；先依主手冊完成場景。
逐筆核對輸入、判斷與證據，修正後才核准。保留 baseline 輸出供比較；不要提交含私人輸入的紀錄。

## 4. 蒸餾候選

使用預設地端 Qwen：

```powershell
.\.venv\Scripts\python.exe -m scripts.distill_procedural --scope stt_exclusion_notify/check --limit 20
```

也可顯式寫出相同模型：

```powershell
.\.venv\Scripts\python.exe -m scripts.distill_procedural --scope stt_exclusion_notify/check --limit 20 --model local-qwen3
```

兩條效果相同，不必連續執行。預期列出新 pending key，或明確表示沒有 active 案例／沒有候選。
有候選不等於改善成功；不要直接把它改成 active。
蒸餾不需要 STT、notified、Agent Runtime 或 workers；若使用 dev services，會一起啟動這些服務，亦可依 Procfile 手動單獨啟動 LiteLLM。

## 5. 評測與人工決策

```powershell
.\.venv\Scripts\python.exe -m scripts.review_memory --scope stt_exclusion_notify/check --repeats 3 --model local-qwen3
```

可加 `--key` 限定一筆。baseline/candidate 使用同一個 check 模型，避免把模型切換誤認成記憶改善。
此流程會在 `eval` tenant staging，不能與另一位開發者共用同一 DB 同時評測。
核對 eval cases、evidence 診斷、規則內容與回歸結果，再選 approve / reject / edit。
保留未見過的案例驗證泛化；單次正確不算穩定改善。

## 6. 查詢自己的現況

用 `psql` 連到 `.env` 指定的同一資料庫；tables 尚未建立時先完成初始化。

```sql
SELECT prefix, value->>'status' AS status, count(*)
FROM store
GROUP BY prefix, value->>'status'
ORDER BY prefix, status;
```

`default` 的 production candidate 與 `eval` 的 staged candidate 是不同用途。
筆數要以本次查詢為準，不能套用舊主機的 59／3 筆紀錄。

## 7. 程式與深入文件

- [記憶 API](../persistence/memory.py)、[權限政策](../mcp_servers/policy.yaml)
- [蒸餾](../scripts/distill_procedural.py)、[人工 procedural 審核](../scripts/review_memory.py)
- [人工 episodic 審核](../scripts/review_episodic.py)、[評測](../evals/run_eval.py)
- [歷史設計與決策](knowledge-distillation-plan.md)、[行為人區分 demo 設計](exclusion-actor-distinction-demo.md)
- [UI 契約](distill-ui-plan.md)、[測試手冊](testing.md)

歷史設計文件可能記錄不同模型的結果；當前模型與權限始終以實際 YAML、CLI 選項及 policy 為準。
