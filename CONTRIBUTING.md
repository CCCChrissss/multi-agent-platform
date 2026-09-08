# 參與開發

本專案可直接使用 Git、VS Code 與終端機開發。Codex、Claude Code 等 AI 工具都是選用工具，不是安裝、執行或測試的前置條件。

## 開始前

1. 閱讀 [README](README.md) 與 [Windows 操作手冊](docs/windows-setup.md)。
2. 從自己的 clone 建立環境，使用 Python 3.11 與 `uv sync --locked`。不要複製別人的 `.venv`。
3. 複製 `.env.example` 為 `.env`，填寫自己的資料庫連線。不要提交憑證或本機快取。
4. 每位開發者使用自己的資料庫。即使是同一位開發者，不同測試 checkout 也不要同時消費同一套測試事件。

## 開發與 Pull Request

從最新的 `main` 建立用途明確的分支，完成小範圍修改後提出 PR。沒有寫入權限時使用 fork；不要推送到歷史上游。
PR 說明需包含問題、修改後行為、實際執行的驗證，以及尚未驗證的條件。由維護者 review 後合併。

沿用現有 Python 模組與 smoke tests；目前沒有全專案統一的 formatter/linter，不要順手格式化無關檔案。
修改相依套件時說明用途與平台影響，同步更新 `pyproject.toml`、`uv.lock`，並重新驗證乾淨安裝。
不要為了配合編輯器更换 workflow 的 runtime 模型。

平台能力放在 gateway、harness、orchestrator 等共用層；場景規則放在 workflow 或對應場景模組。
修改 agent/tool loop 前閱讀 [Harness 原則](docs/harness-engineering-principles.md)。
保留 MCP 與 memory 的拒絕預設，UI 編輯 YAML 應經過 `demo/spec_writer.py`。

## 提交前驗證

在根目錄執行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\dev.ps1 check
git diff --check
git status --short
```

再依 [測試手冊](docs/testing.md) 執行與變更相關的 smoke tests。
DB、LLM、模型下載與資料寫入測試需要自己的測試環境；不要對共用或正式資料庫直接跑測試。
工具使用規範另見 [AGENTS.md](AGENTS.md)，不需要安裝該文件提及的 AI 工具。

目前服務位址仍以固定 localhost ports 為基準；一台電腦同時只能在這些 ports 上執行一套 stack。
停止命令只管理本 checkout 由 `dev.ps1` 啟動的程序。

## 來源與授權狀態

來源與原作者同意紀錄見 [UPSTREAM.md](UPSTREAM.md)。目前沒有新增 LICENSE，請勿自行假設或附加特定開源授權。
