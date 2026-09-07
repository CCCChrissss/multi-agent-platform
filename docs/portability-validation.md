# 可移植性改動驗證紀錄

本文件記錄本次改動的實際驗證，不代表全新 Windows 安裝或模型工作流已完成。
本次沒有安裝套件、建立 .env、連線資料庫或啟動真實平台服務。

## 已執行

- `dev.ps1 check` 已在 Windows PowerShell 5.1 與 PowerShell 7.6.5 從 repository 外部目錄執行成功。
- 靜態檢查：175 個 UTF-8 文字檔、123 個 Python AST、2 個 TOML、5 個 JSON、568 個本機 Markdown links、6 個 workflow model references、13 個音檔標頭均通過。
- 暫存音檔 smoke test 通過。
- 7 個 portability tests 通過：JSON 檔案／BOM／中文空白路徑、無效輸入、指定蒸餾模型與 pending gate、managed services 不含共用 Ollama、停止 preview、PID 建立時間核對、正常／意外結束時的子孫程序清理，以及保留無關程序。
- 33 個文件中的 PowerShell code blocks 與 2 個 PowerShell 腳本通過 parser 語法檢查。
- `git diff --check` 通過。
- 初始 fresh checkout 的 doctor 以獨立 Python 3.11 執行，正確將缺少的 `.venv`、Ollama、專案 distributions 與 `.env` 回報為 `MISSING`，且沒有連線或修改外部服務。
- 已安裝環境下的 gateway、live spec、四個 LLM mock、Breeze 音訊前處理、MCP 子程序環境與 gather concurrency smoke tests 通過。
- `harness.generic_agent_smoke_test` 原列於無模型測試，但其第一個情境實際需要 LiteLLM 與模型；在 gateway 未啟動時正確失敗，文件已將它移到模型整合測試，未把此結果列為通過。

驗證期間發現通用 `python` 指令可能是 uv 自動同步代理；舊版啟動器呼叫它時意外建立了 `.venv`。啟動器已改用 `uv python find 3.11` 解析明確的直譯器，使 `doctor`、`check`、`status`、`stop` 在 fresh checkout 保持唯讀。驗證結束後會移除該環境，並再次確認 `check` 不會重建它。沙箱帳號直接使用原本的 `python` trampoline 時收到 permission denied，因此不把該次失敗歸類為專案功能通過；實際使用者權限下的 PowerShell 5.1／7 check 已另行通過。

最終 fresh 狀態再次執行 `dev.ps1 check` 與 `dev.ps1 doctor`：兩者均未建立 `.venv` 或 `.run`；check 全數通過。doctor 偵測到本機已有被 Git 忽略的 `.env`，但在套件尚未安裝時不解析其內容，並正確列出 `.venv` 與 distributions 尚未準備完成。

## 尚未驗證

- 全新 clone 的 uv sync --locked；Windows CPU/GPU 安裝與模型資源需求。
- PostgreSQL / pgvector 全新建立、schema 初始化與保單 seed。
- 本次 launcher 搭配真實 Honcho、MCP、Ollama、LiteLLM、ASR 的啟動／停止。
- 兩份 local-qwen3 workflow 的新 thread_id、UI 操作與完整知識蒸餾閉環。
- VS Code Tasks 的實際 GUI 點選；macOS / Linux 的完整執行流程。

下一階段依 [Windows 主手冊](windows-setup.md) 在乾淨環境逐步記錄版本、成功輸出、thread_id 與停止結果。
