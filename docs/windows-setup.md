# Windows / PowerShell 開發環境

根目錄 [README.md](../README.md) 的主要安裝範例以 macOS / Bash 為主。本文件提供
Windows PowerShell 對應方式；專案的基準版本是 [.python-version](../.python-version)
指定的 Python 3.11，與 GitHub Actions 一致。

## 1. 前置工具

需要以下工具，安裝後請重新開啟 PowerShell，確認指令可用：

```powershell
git --version
uv --version
python --version
```

- Git：[Git for Windows](https://git-scm.com/download/win)
- uv：[官方安裝文件](https://docs.astral.sh/uv/getting-started/installation/)
- Python：可讓 uv 依 `.python-version` 管理 3.11，或使用
  [Python 官方 Windows 下載](https://www.python.org/downloads/windows/)
- PostgreSQL：[官方 Windows installer](https://www.postgresql.org/download/windows/)
- pgvector：[官方 Windows build 說明](https://github.com/pgvector/pgvector#windows)
- Ollama：[官方 Windows 下載](https://ollama.com/download/windows)

本文件不會自動安裝上述工具。安裝前應先確認公司電腦權限、磁碟空間與既有
PostgreSQL/Ollama 服務，避免撞版本或 port。

## 2. 取得專案與建立 Python 環境

在要放專案的父資料夾執行：

```powershell
git clone https://github.com/CCCChrissss/multi-agent-platform.git
Set-Location .\multi-agent-platform
uv python install 3.11
uv sync
```

`uv sync` 會依 [pyproject.toml](../pyproject.toml) 與 [uv.lock](../uv.lock)
建立 `.venv` 並安裝相依套件。成功後驗證：

```powershell
uv run python --version
```

輸出應為 Python 3.11.x。若電腦只能找到 3.14，不要直接把它當成相容性結論；
目前 CI 的已知基準仍是 3.11。

## 3. PostgreSQL 與 pgvector

先建立資料庫，再啟用 `vector` extension：

```powershell
createdb agent_architecture
psql agent_architecture -c "CREATE EXTENSION vector;"
psql agent_architecture -c "\dx"
```

最後一個指令必須列出 `vector`。如果 PowerShell 找不到 `createdb` 或 `psql`，
把 PostgreSQL 的 `bin` 目錄加入 PATH，或用安裝目錄下的完整執行檔路徑。
如果 `CREATE EXTENSION` 回報找不到 extension，代表只裝了 PostgreSQL、尚未安裝
pgvector，不能略過這一步後宣稱長期記憶可用。

## 4. 環境變數

```powershell
Copy-Item .env.example .env
```

編輯 `.env`，至少填入目前 workflow 需要的 API key。兩個示範 workflow 都需要
`ANTHROPIC_API_KEY`；`stt_exclusion_notify` 的 `stt` 使用 `gemini-cheap`，因此也需要
`GEMINI_API_KEY`。`.env` 已被 Git 忽略，不要把真實 key 加進 `.env.example`。

切換到除外責任 workflow 可在 `.env` 設定：

```text
WORKFLOW_DEF_PATH=workflows/definitions/stt_exclusion_notify.yaml
```

也可以只在目前 PowerShell 工作階段設定 `$env:WORKFLOW_DEF_PATH`，但重開終端後會
消失。修改 workflow 選擇後要重啟 agent runtime 與 workers。

## 5. Ollama 模型

確認 Ollama 背景服務正在執行，再下載專案目前宣告的本機模型：

```powershell
ollama pull qwen2.5:3b
ollama pull bge-m3
ollama list
```

如果已由 Windows 背景服務啟動 Ollama，請勿再讓 [Procfile](../Procfile) 的
`ollama` process 重複占用 11434。啟動前可先檢查：

```powershell
Test-NetConnection -ComputerName localhost -Port 11434
```

## 6. 啟動與檢查

在 repository 根目錄執行：

```powershell
uv run honcho start
```

另開 PowerShell 檢查服務 port：

```powershell
11434, 4000, 8001, 8002, 8003 | ForEach-Object {
    Test-NetConnection -ComputerName localhost -Port $_ -InformationLevel Quiet
}
```

查 LiteLLM 已載入的 alias：

```powershell
(Invoke-RestMethod http://localhost:4000/v1/models).data.id
```

預期包含 `local-qwen`、`claude-haiku`、`gemini-cheap`、`gemini-strong`、
`breeze-asr`、`local-embed`。服務啟動失敗時，依 [setup.md](setup.md) 的錯誤分類
查看 honcho 對應 process 的 log。

## 7. 安裝前也能執行的相容性檢查

以下兩項只使用 Python 標準函式庫，不需要先 `uv sync`：

```powershell
python scripts/static_compat_check.py
python -m services.stt.temp_audio_smoke_test
```

第一項檢查語法、編碼、設定引用與文件連結；第二項驗證 Windows 上暫存音檔可以在
原始 handle 關閉後重新開啟，並會在使用後刪除。它們不會驗證 PostgreSQL、模型、
LLM API 或完整 workflow；完整測試層級見 [testing.md](testing.md)。

## 8. 常見 Windows 差異

| macOS / Bash | PowerShell |
|---|---|
| `cp .env.example .env` | `Copy-Item .env.example .env` |
| `export NAME=value` | `$env:NAME = "value"` |
| `lsof -ti :4000` | `Get-NetTCPConnection -LocalPort 4000` |
| `grep pattern` | `Select-String pattern` |
| `python3` | `python` 或 `uv run python` |

不要直接照 README 執行 `brew`、`brew services`、`say`、`afconvert` 或 `/opt/homebrew`
路徑；這些都是 macOS 工具或路徑。
