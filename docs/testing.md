# 測試與重現驗證

在 repository 根目錄執行。測試使用既有 smoke modules 與標準函式庫 unittest，不新增 pytest。
以下「執行方法」不是「已驗證結果」；當前實際結果見 [本次驗證紀錄](portability-validation.md)，
歷史 CI 與主機紀錄見 [歷史快照](current-windows-status.md)。

## 1. 不需要平台服務

Python 3.11 已可用時，不需要安裝專案相依套件：

```powershell
python -B scripts/static_compat_check.py
python -B -m services.stt.temp_audio_smoke_test
python -B -m scripts.portability_smoke_test
```

已有 .venv 可使用 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\dev.ps1 check`。
靜態檢查包含 UTF-8、Python AST、TOML/JSON、文件連結、workflow model alias 與音檔標頭。
portability 測試包含 JSON 檔案、中文／空白／BOM、錯誤輸入、停止預覽與 PID 重用防護。
Windows 額外用暫存目錄和短命 Python 測試程序驗證子孫程序清理，不會啟動 DB、Ollama 或實際服務；
其他 OS 跳過 Windows 程序測試。

## 2. 已安裝套件，不呼叫真實模型

先完成 `uv sync --locked`。需要啟動 MCP 子程序的測試仍需 uv 在 PATH：

```powershell
.\.venv\Scripts\python.exe -m gateway.client_smoke_test
.\.venv\Scripts\python.exe -m agents.live_spec_smoke_test
.\.venv\Scripts\python.exe -m llm.notify_agent_smoke_test
.\.venv\Scripts\python.exe -m llm.stt_agent_smoke_test
.\.venv\Scripts\python.exe -m llm.tsmc_judge_smoke_test
.\.venv\Scripts\python.exe -m llm.exclusion_judge_smoke_test
.\.venv\Scripts\python.exe -m services.stt.breeze_asr_smoke_test
.\.venv\Scripts\python.exe -m mcp_servers.base_client_env_smoke_test
.\.venv\Scripts\python.exe -m scripts.database_setup_smoke_test
.\.venv\Scripts\python.exe gather_concurrency_smoke_test.py
```

單一 MCP server 的 stdio tests：

```powershell
$env:PYTHONUTF8 = '1'
$env:UV_NO_SYNC = 'true'
.\.venv\Scripts\python.exe -m mcp_servers.stt.smoke_test
.\.venv\Scripts\python.exe -m mcp_servers.format_check.smoke_test
.\.venv\Scripts\python.exe -m mcp_servers.lookup.smoke_test
.\.venv\Scripts\python.exe -m mcp_servers.notified.smoke_test
.\.venv\Scripts\python.exe -m mcp_servers.calc.smoke_test
```

不用為了通過測試填假雲端 key。若某測試載入額外本機狀態或需要寫檔，先閱讀該 smoke module。
`demo.spec_writer_smoke_test` 會修改／還原實際設定與產生測試 YAML，應在乾淨的測試 checkout、沒有 runtime 時執行。
[子程序環境測試](../mcp_servers/base_client_env_smoke_test.py) 內的路徑是字串 fixture，不是必須存在的資料夾。

## 3. DB 準備與檢查

主要流程不依賴 `psql` 是否在 PATH；先啟動 PostgreSQL、建立 `.env`，再執行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\dev.ps1 db-check
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\dev.ps1 db-init
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\dev.ps1 db-check
```

`db-check` 唯讀；database、vector extension 或必要 table 缺少時以 exit code 1 結束。
`db-init` 會建立 `.env` 指定的 database、啟用 pgvector，並呼叫各模組既有的冪等 schema setup；
它不會刪除或重建既有 database。若 PostgreSQL server 尚未安裝 pgvector binary，會明確停止，依
[Windows 主手冊](windows-setup.md) 安裝後重跑。兩個指令只顯示 host、port、database，不輸出帳密。

## 4. DB / embedding 整合測試

使用專屬測試資料庫並確認 schema、pgvector、LiteLLM 與 bge-m3 可用。
先停止 event-driven workers，否則相同 consumer group 可能搶走測試事件。
這些測試可能寫入或清除测试資料，不對共用／正式 DB 執行。

```powershell
.\.venv\Scripts\python.exe -m event_bus.smoke_test
.\.venv\Scripts\python.exe -m persistence.memory_smoke_test
.\.venv\Scripts\python.exe -m mcp_servers.memory.smoke_test
.\.venv\Scripts\python.exe -m demo.distill_api_smoke_test
```

先讀各 module 的前置條件與清理範圍。這些是服務整合測試，不屬於 doctor/check 的範圍。

## 5. 模型與端到端

`harness.generic_agent_smoke_test` 的第一個情境會啟動 lookup MCP 子程序，並透過 LiteLLM 呼叫實際模型；需先啟動本機 stack：

```powershell
.\.venv\Scripts\python.exe -m harness.generic_agent_smoke_test
.\.venv\Scripts\python.exe -m orchestrator.smoke_test
```

`orchestrator.smoke_test` 不只是 DB 測試：happy path 會實際呼叫 8001/8002/8003 與本機模型；執行前必須先啟動對應 workflow 的 `services`，並停止正式 `workers`，避免相同 consumer group 搶走測試事件。

[Windows 主手冊](windows-setup.md) 提供兩份 workflow 的啟動、模型探測、trigger、run/history 查詢與停止。
[知識蒸餾手冊](knowledge-distillation-windows.md) 提供人工審核、候選與 eval 流程。
切換模型時需重新評估 structured output、tool calling、場景品質與耗時，不能沿用其他模型的成功紀錄。

`workflows.parity_check` 需要真實服務，且以 `main` 的同步程式為保留基準；
它不是本次可移植性改動的必要無服務測試，不要順手改寫凍結的同步流程。

## 6. GitHub Actions 的邊界

CI 在 Windows 執行無服務檢查與 ownership 測試，在 Ubuntu 保留 MCP／gather smoke tests。
套件安裝使用 `uv sync --locked`；CI 不代表 Windows 新機已完成 GPU、DB 或全流程驗證。

PR 回報需列出實際命令與結果，未跑的項目需寫明前置條件。發現錯誤就保留錯誤與 exit code，不宣稱已通過。
