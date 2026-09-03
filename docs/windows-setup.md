# Windows / PowerShell 安裝與 workflow 操作

本文件是這個 repository 的 Windows 主操作手冊。開始前先看 [current-windows-status.md](current-windows-status.md)：它記錄哪些項目已在本機驗證、哪些只是保留的上游功能。

> [!IMPORTANT]
> 2026-08-31 的歷史狀態是：PostgreSQL、pgvector、Ollama、`local-qwen`、`local-embed` 與五個 application service 都曾驗證；兩份 workflow 也曾以當時的 `gemini-cheap` 各完成一次 event-driven 執行。2026-09-03 工作樹已把兩份 workflow 改為 `local-qwen3`；該 alias 的文字回覆與 tool call 已驗證，但完整 event-driven workflow 必須用新的 `thread_id` 重新實測。日期化歷史見 [current-windows-status.md](current-windows-status.md)。

## 1. 先設定自己的本機路徑

本手冊的所有指令都從 repository 根目錄執行。以下表格是目前已驗證電腦的路徑範例，不是其他使用者必須照抄的固定值：

| 用途 | 路徑 |
|---|---|
| Repository | `D:\Projects\multi-agent平台架設\multi-agent-platform` |
| uv | `C:\Users\User\.local\bin\uv.exe` |
| Python 虛擬環境 | `D:\Projects\multi-agent平台架設\multi-agent-platform\.venv` |
| uv cache | `D:\Projects\multi-agent平台架設\.uv-cache` |
| Hugging Face cache | `D:\Projects\multi-agent平台架設\.hf-cache` |
| Ollama | `C:\Users\User\AppData\Local\Programs\Ollama\ollama.exe` |
| Ollama models | `D:\Projects\multi-agent平台架設\.ollama\models` |
| PostgreSQL 測試資料庫 | `agent_architecture_test` |

其他使用者只需要把每個「新 terminal」指令區塊第一行的 `$RepoRoot` 改成自己的 repository 絕對路徑。路徑含中文、空白或連字號時，使用 `-LiteralPath`，不要依賴未初始化的 `$RepoRoot`。全新電腦尚未 clone repository 時，先完成第 2 節，再使用下面的共用區塊。

每個新的 VS Code PowerShell terminal 都是獨立 session，不會繼承其他 terminal 的變數。以下是共用的安全前置區塊；後續 Terminal A、B、C 會各自完整重複必要內容：

```powershell
$RepoRoot = 'D:\Projects\multi-agent平台架設\multi-agent-platform'
if (-not (Test-Path -LiteralPath $RepoRoot -PathType Container)) {
    throw "找不到 repository：$RepoRoot"
}

$WorkspaceRoot = Split-Path -Parent $RepoRoot
Set-Location -LiteralPath $RepoRoot
```

## 2. 從全新 Windows 安裝開發工具與專案

專案基準是 [.python-version](../.python-version) 指定的 Python 3.11。建議使用 Windows 10/11 64-bit、VS Code 與 PowerShell。以下命令中的 `winget` 是 Windows Package Manager；若電腦沒有 `winget`，使用同一列的官方下載頁安裝。

| 工具 | 用途 | 建議安裝指令 | 官方下載／說明 |
|---|---|---|---|
| Git for Windows | clone、branch、commit、push | `winget install --id Git.Git -e --source winget` | [Git for Windows](https://git-scm.com/download/win) |
| Visual Studio Code | 編輯程式與開啟 PowerShell terminal | `winget install --id Microsoft.VisualStudioCode -e --source winget` | [VS Code for Windows](https://code.visualstudio.com/download) |
| GitHub CLI（建議） | 建立／檢查／合併 Pull Request | `winget install --id GitHub.cli -e --source winget` | [GitHub CLI](https://cli.github.com/) |
| uv | 安裝 Python、建立 `.venv`、依 lockfile 安裝套件 | `winget install --id astral-sh.uv -e --source winget` | [uv Windows 安裝](https://docs.astral.sh/uv/getting-started/installation/) |
| PostgreSQL 18 | event bus、checkpoint、call log、長期記憶 | 使用官方圖形安裝程式 | [PostgreSQL Windows installer](https://www.postgresql.org/download/windows/) |
| Visual Studio Build Tools（僅 pgvector 編譯需要） | 提供 MSVC、C++ header 與 `nmake` | 使用官方圖形安裝程式並勾選 **Desktop development with C++** | [Build Tools for Visual Studio](https://visualstudio.microsoft.com/visual-cpp-build-tools/) |
| Ollama | 執行本機聊天與 embedding 模型 | `irm https://ollama.com/install.ps1 | iex` | [Ollama for Windows](https://ollama.com/download/windows) |

安裝 Git、VS Code、GitHub CLI、uv 或 Ollama 後，關閉並重開 VS Code／PowerShell，讓新的 `PATH` 生效。Ollama 也可以只使用官方頁面的 Windows installer，不需要同時執行兩種安裝方式。

### 2.1 驗證開發工具

開啟新的 PowerShell：

```powershell
git --version
code --version
gh --version
uv --version
ollama --version
```

每一行都應顯示版本而不是「無法辨識」；若暫時不使用 GitHub CLI，只有 `gh --version` 可以略過。要用 GitHub 多人協作時，再登入並確認目前帳號：

```powershell
gh auth login --web --git-protocol https
gh auth status
```

不要執行或分享 `gh auth status --show-token`，也不要把 token 貼進 `.env` 或文件。

### 2.2 Clone repository

以下是全新安裝範例。若 repository 已存在，跳過 `git clone`，直接把 `$RepoRoot` 設為實際路徑：

```powershell
$RepoParent = 'D:\Projects\multi-agent平台架設'
$RepoRoot = Join-Path $RepoParent 'multi-agent-platform'

New-Item -ItemType Directory -Force -Path $RepoParent | Out-Null
Set-Location -LiteralPath $RepoParent
git clone https://github.com/CCCChrissss/multi-agent-platform.git
Set-Location -LiteralPath $RepoRoot
git status
```

成功時，`git status` 會顯示目前分支，且不會出現 `not a git repository`。

### 2.3 安裝 Python 3.11 與全部 Python 套件

不需要先到 python.org 安裝 Python，也不要逐項執行 `pip install`；uv 會安裝 Python 3.11，並依 [pyproject.toml](../pyproject.toml) 與 [uv.lock](../uv.lock) 建立一致的 `.venv`。

```powershell
$RepoRoot = 'D:\Projects\multi-agent平台架設\multi-agent-platform'
if (-not (Test-Path -LiteralPath $RepoRoot -PathType Container)) {
    throw "找不到 repository：$RepoRoot"
}

$WorkspaceRoot = Split-Path -Parent $RepoRoot
$uvExe = (Get-Command uv -ErrorAction Stop).Source
Set-Location -LiteralPath $RepoRoot
$env:UV_CACHE_DIR = Join-Path $WorkspaceRoot '.uv-cache'
$env:HF_HUB_CACHE = Join-Path $WorkspaceRoot '.hf-cache'

& $uvExe python install 3.11
& $uvExe sync
```

`uv sync` 會安裝 LiteLLM、Honcho、FastAPI、MCP SDK、PostgreSQL driver、PyTorch、Transformers、librosa 等本 repository 鎖定的依賴；除非 `pyproject.toml` 或 `uv.lock` 改變，不必每次啟動服務都重跑。Windows 目前會從 [PyTorch CUDA wheel index](https://download.pytorch.org/whl/cu132) 安裝 lockfile 指定版本。

驗證 Python、主要套件與 CUDA：

```powershell
.\.venv\Scripts\python.exe --version
.\.venv\Scripts\python.exe -c "import fastapi, litellm, mcp, torch, transformers; print('project imports: OK')"
.\.venv\Scripts\python.exe -c "import torch; print('torch=', torch.__version__); print('cuda=', torch.version.cuda); print('available=', torch.cuda.is_available()); print('device=', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU only')"
```

第一行必須是 Python 3.11.x，第二行必須印出 `project imports: OK`。目前實機的 CUDA 檢查為 `available=True` 與 `NVIDIA GeForce RTX 4050 Laptop GPU`；沒有相容 NVIDIA GPU 時可以啟動 CPU 路徑，但 Breeze-ASR 推論會明顯較慢。

## 3. PostgreSQL 與 pgvector

本機已驗證 PostgreSQL 18.6、資料庫 `agent_architecture_test`、pgvector 0.8.6。先從 [PostgreSQL 官方 Windows 頁面](https://www.postgresql.org/download/windows/) 進入 EDB installer，安裝時至少保留：

- PostgreSQL Server
- pgAdmin 4
- Command Line Tools

安裝程式會要求建立 `postgres` 使用者的密碼；這是使用者自行設定的密碼，repository 不會產生、保存或知道它。預設 port 使用 `5432`。安裝後可在 PowerShell 確認 service：

```powershell
Get-Service -Name 'postgresql*'
```

`Status` 應為 `Running`。

### 3.1 建立資料庫

在 pgAdmin 4：

1. 展開 `Servers > PostgreSQL 18`。
2. 右鍵 `Databases`，選 `Create > Database...`。
3. Database 填入 `agent_architecture_test`，Owner 使用 `postgres`。
4. 儲存後，對 `agent_architecture_test` 開啟 `Query Tool`。

### 3.2 安裝並啟用 pgvector

先在 `agent_architecture_test` 的 Query Tool 檢查 server 是否已具備 extension：

```sql
SELECT name, default_version
FROM pg_available_extensions
WHERE name = 'vector';
```

如果查到一列，直接執行本節後面的 `CREATE EXTENSION`。如果完全沒有資料，依 [pgvector 官方 Windows 安裝說明](https://github.com/pgvector/pgvector#installation-notes---windows) 編譯安裝：

1. 安裝 [Visual Studio Build Tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/)，勾選 **Desktop development with C++**。
2. 從開始功能表，以系統管理員身分開啟 **x64 Native Tools Command Prompt for VS 2022**。
3. 下列區塊是 `cmd.exe` 語法，不是在一般 PowerShell 執行：

```bat
set "PGROOT=C:\Program Files\PostgreSQL\18"
cd /d "%TEMP%"
git clone --branch v0.8.6 --depth 1 https://github.com/pgvector/pgvector.git pgvector-0.8.6
cd pgvector-0.8.6
nmake /F Makefile.win
nmake /F Makefile.win install
```

如果 PostgreSQL 安裝在其他版本或路徑，必須先調整 `PGROOT`。`nmake ... install` 會寫入 PostgreSQL 安裝目錄，因此需要系統管理員權限；如果 `pgvector-0.8.6` 目錄已存在，請換一個新的空目錄名稱，不要在不知道內容時直接刪除。

完成後回到 pgAdmin 的 `agent_architecture_test` Query Tool：

```sql
CREATE EXTENSION IF NOT EXISTS vector;

SELECT extversion
FROM pg_extension
WHERE extname = 'vector';
```

查詢結果應有一列；本機已驗證版本為 `0.8.6`。

也可以直接指定 PostgreSQL 18 的 `psql.exe`，不依賴 PATH：

```powershell
$psql = 'C:\Program Files\PostgreSQL\18\bin\psql.exe'
& $psql -U postgres -d agent_architecture_test -c "SELECT extversion FROM pg_extension WHERE extname = 'vector';"
```

`PERSISTENCE_DATABASE_URL` 必須使用你自己的帳號、密碼與資料庫名稱。不要把真實密碼貼進 README、`.env.example`、commit、issue 或聊天截圖。

## 4. 建立 `.env`

只在 `.env` 不存在時複製範本：

```powershell
$RepoRoot = 'D:\Projects\multi-agent平台架設\multi-agent-platform'
if (-not (Test-Path -LiteralPath $RepoRoot -PathType Container)) {
    throw "找不到 repository：$RepoRoot"
}
Set-Location -LiteralPath $RepoRoot
if (-not (Test-Path -LiteralPath '.env')) {
    Copy-Item -LiteralPath '.env.example' -Destination '.env'
}
```

打開 `.env`，將 `PERSISTENCE_DATABASE_URL` 改成實際連線資訊，例如：

```text
PERSISTENCE_DATABASE_URL=postgresql://postgres:YOUR_PASSWORD@localhost:5432/agent_architecture_test
```

`YOUR_PASSWORD` 只是明確的佔位字串，必須換成安裝 PostgreSQL 時設定的密碼。

目前金鑰規則：

- 目前工作樹中 `stt_check_notify` 與 `stt_exclusion_notify` 的三個 agent step 都使用 `local-qwen3`，不需要 Anthropic 或 Gemini API key。
- `claude-haiku`、`gemini-cheap`、`gemini-strong` alias 仍保留在 `gateway/config.yaml`；只有 workflow 改用它們時才需要相應的 API key。
- 不要為本機測試填假 key，也不要把真實 key 寫進 `.env.example`。

2026-09-01 已移除 `ANTHROPIC_API_KEY` 與 `GEMINI_API_KEY`。這不會阻擋目前的本機 workflow，但 `local-qwen3` 的完整 event-driven 結果仍應以新的 `thread_id` 實測，不能沿用先前 Gemini run 當作證明。

建議讓 `.env` 裡的 `WORKFLOW_DEF_PATH` 保持註解或不存在，並由 Terminal A、B 明確設定。Honcho 的 `-e .env` 會讀取該檔；如果 `.env` 固定寫了另一份 workflow，可能覆蓋 terminal 的選擇，造成 Runtime、workers 與 trigger 不一致。可先檢查：

```powershell
Select-String -LiteralPath '.env' -Pattern '^WORKFLOW_DEF_PATH='
```

正常建議是沒有輸出；若有輸出，先確認它與本次要執行的 `$WorkflowDef` 完全相同，或將該行改回註解。

## 5. Ollama 與本機模型

若第 2 節尚未安裝 Ollama，使用 [Ollama 官方 Windows 下載頁](https://ollama.com/download/windows)，或在 PowerShell 執行官方安裝命令：

```powershell
irm https://ollama.com/install.ps1 | iex
```

兩種方式擇一即可。安裝完成後，關閉並重開 PowerShell，再執行 `ollama --version`。

### 5.1 將模型下載到 workspace 的 D 槽目錄

每次開新的 PowerShell，先用使用者目錄與 workspace 組出路徑：

```powershell
$RepoRoot = 'D:\Projects\multi-agent平台架設\multi-agent-platform'
$WorkspaceRoot = Split-Path -Parent $RepoRoot
$ollamaBin = Join-Path $env:LOCALAPPDATA 'Programs\Ollama'
$env:PATH = "$ollamaBin;" + $env:PATH
$env:OLLAMA_MODELS = Join-Path $WorkspaceRoot '.ollama\models'
New-Item -ItemType Directory -Force -Path $env:OLLAMA_MODELS | Out-Null
```

重要：`OLLAMA_MODELS` 是 Ollama **server 啟動時**讀取的。如果 Ollama Desktop 已在背景佔用 11434，之後只在 VS Code terminal 設定這個變數，不會改變既有 server 的 model 目錄。建議本專案統一由 Honcho / Procfile 啟動 Ollama，啟動前先從 Windows 系統列退出 Ollama Desktop。

第一次下載模型時：

1. 先從 Windows 系統列完全退出 Ollama Desktop，避免既有 server 使用 C 槽預設目錄。
2. 在第一個 PowerShell 設定上述變數後啟動 server，並保持 terminal 開啟：

```powershell
$ollamaExe = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
if (-not (Test-Path -LiteralPath $ollamaExe -PathType Leaf)) {
    throw "找不到 Ollama：$ollamaExe"
}
& $ollamaExe serve
```

3. 另開第二個 PowerShell，重新設定路徑並下載三個模型：

```powershell
$RepoRoot = 'D:\Projects\multi-agent平台架設\multi-agent-platform'
$WorkspaceRoot = Split-Path -Parent $RepoRoot
$ollamaExe = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
$env:OLLAMA_MODELS = Join-Path $WorkspaceRoot '.ollama\models'

& $ollamaExe pull qwen2.5:3b
& $ollamaExe pull qwen3:4b-instruct-2507-q4_K_M
& $ollamaExe pull bge-m3
& $ollamaExe list
```

模型用途與官方頁面：

| Ollama 模型 | 專案 alias／用途 | 官方模型頁 |
|---|---|---|
| `qwen2.5:3b` | `local-qwen`，保留的舊本機 agent 模型 | [Ollama qwen2.5:3b](https://ollama.com/library/qwen2.5:3b) |
| `qwen3:4b-instruct-2507-q4_K_M` | `local-qwen3`，目前 workflow 使用的本機 agent 模型 | [Ollama qwen3:4b-instruct-2507-q4_K_M](https://ollama.com/library/qwen3:4b-instruct-2507-q4_K_M) |
| `bge-m3` | `local-embed`，長期記憶與知識蒸餾 embedding | [Ollama bge-m3](https://ollama.com/library/bge-m3) |

`pull` 完成後，先在第二個 PowerShell 確認：

```powershell
ollama list
```

預期至少包含：

- `qwen2.5:3b`
- `qwen3:4b-instruct-2507-q4_K_M`
- `bge-m3`

確認完成後，再到第一個 PowerShell 對 `ollama serve` 按 `Ctrl+C`。後續改由第 7 節的 Honcho 啟動；server 已停止時不要單獨執行 `ollama list`，因為 client 沒有可連線的 Ollama server。

如果只想單獨啟動 Ollama，可在設定上述變數後執行 `ollama serve`。最簡單的完整服務路徑則是讓 [Procfile](../Procfile) 啟動它；啟動前先確認 11434 沒有被 Windows 背景版 Ollama 占用。

```powershell
Test-NetConnection -ComputerName 127.0.0.1 -Port 11434 -InformationLevel Quiet
```

回傳 `False` 表示 port 目前空閒；可交給 Honcho 啟動。回傳 `True` 時，先找出 listener：

```powershell
$listener = Get-NetTCPConnection -State Listen -LocalPort 11434
Get-Process -Id $listener.OwningProcess
```

若是 Ollama Desktop，先從系統列完全退出，重新確認 port 為 `False`。不要在未確認 PID 身分前直接強制終止 process。

### 5.2 Breeze-ASR-25 語音辨識模型

STT 使用 [MediaTek Research 的 Breeze-ASR-25](https://huggingface.co/MediaTek-Research/Breeze-ASR-25)，不透過 `ollama pull`。第一次真正轉錄時，[services/stt/breeze_asr.py](../services/stt/breeze_asr.py) 會由 Transformers 自動下載並快取模型；第一次因此需要網路且會比後續慢。

只想讓第一次 workflow 不必邊跑邊下載時，可以在 repository 根目錄預先下載：

```powershell
$RepoRoot = 'D:\Projects\multi-agent平台架設\multi-agent-platform'
if (-not (Test-Path -LiteralPath $RepoRoot -PathType Container)) {
    throw "找不到 repository：$RepoRoot"
}
$WorkspaceRoot = Split-Path -Parent $RepoRoot
Set-Location -LiteralPath $RepoRoot
$env:HF_HUB_CACHE = Join-Path $WorkspaceRoot '.hf-cache'
.\.venv\Scripts\python.exe -c "from huggingface_hub import snapshot_download; print(snapshot_download(repo_id='MediaTek-Research/Breeze-ASR-25'))"
```

成功時會印出 snapshot 的本機 cache 路徑。這個步驟只下載權重，不會啟動 STT service；若跳過，第一次轉錄仍會自動下載。

## 6. 選擇 workflow

Agent Runtime 與 event-driven workers 都在 **process 啟動時**讀取 `WORKFLOW_DEF_PATH`。兩批 process 必須使用相同檔案。

最簡單、目前優先驗證的 workflow：

```powershell
$env:WORKFLOW_DEF_PATH = 'workflows/definitions/stt_check_notify.yaml'
```

若完全不設定，程式預設也是這一份。

除外責任 workflow：

```powershell
$env:WORKFLOW_DEF_PATH = 'workflows/definitions/stt_exclusion_notify.yaml'
```

這份目前使用 `local-qwen3`，不需要雲端 API key，但必須先完成保單記憶 seed。

`orchestrator.trigger --workflow-def` 只決定這次要觸發哪份定義，不能替已啟動的 Runtime / workers 切換設定。切換 workflow 時，兩批 Honcho 都要停止、設定同一個 `$env:WORKFLOW_DEF_PATH`，再重新啟動。

## 7. 啟動五個常駐服務

第一個 PowerShell（Terminal A：Services）：

這個 terminal 建議直接使用 VS Code 的整合式 PowerShell。執行前再次確認 11434 是空的；否則 Honcho 裡的 `ollama` 會啟動失敗，且背景 Ollama 可能讀取另一個 model 目錄。

### 7.1 台積電場景（Services）

```powershell
$RepoRoot = 'D:\Projects\multi-agent平台架設\multi-agent-platform'
if (-not (Test-Path -LiteralPath $RepoRoot -PathType Container)) {
    throw "找不到 repository：$RepoRoot"
}
$WorkspaceRoot = Split-Path -Parent $RepoRoot
$uvExe = (Get-Command uv -ErrorAction Stop).Source
$uvBin = Split-Path -Parent $uvExe
$ollamaBin = Join-Path $env:LOCALAPPDATA 'Programs\Ollama'
Set-Location -LiteralPath $RepoRoot
$env:PYTHONUTF8 = '1'
$env:PATH = "$uvBin;$ollamaBin;" + $env:PATH
$env:UV_CACHE_DIR = Join-Path $WorkspaceRoot '.uv-cache'
$env:HF_HUB_CACHE = Join-Path $WorkspaceRoot '.hf-cache'
$env:OLLAMA_MODELS = Join-Path $WorkspaceRoot '.ollama\models'
$WorkflowDef = 'workflows/definitions/stt_check_notify.yaml'
if (-not (Test-Path -LiteralPath $WorkflowDef -PathType Leaf)) {
    throw "找不到 workflow：$WorkflowDef"
}
$env:WORKFLOW_DEF_PATH = $WorkflowDef
.\.venv\Scripts\honcho.exe start -f Procfile -e .env
```

### 7.2 除外責任場景（Services）

```powershell
$RepoRoot = 'D:\Projects\multi-agent平台架設\multi-agent-platform'
if (-not (Test-Path -LiteralPath $RepoRoot -PathType Container)) {
    throw "找不到 repository：$RepoRoot"
}
$WorkspaceRoot = Split-Path -Parent $RepoRoot
$uvExe = (Get-Command uv -ErrorAction Stop).Source
$uvBin = Split-Path -Parent $uvExe
$ollamaBin = Join-Path $env:LOCALAPPDATA 'Programs\Ollama'
Set-Location -LiteralPath $RepoRoot
$env:PYTHONUTF8 = '1'
$env:PATH = "$uvBin;$ollamaBin;" + $env:PATH
$env:UV_CACHE_DIR = Join-Path $WorkspaceRoot '.uv-cache'
$env:HF_HUB_CACHE = Join-Path $WorkspaceRoot '.hf-cache'
$env:OLLAMA_MODELS = Join-Path $WorkspaceRoot '.ollama\models'
$WorkflowDef = 'workflows/definitions/stt_exclusion_notify.yaml'
if (-not (Test-Path -LiteralPath $WorkflowDef -PathType Leaf)) {
    throw "找不到 workflow：$WorkflowDef"
}
$env:WORKFLOW_DEF_PATH = $WorkflowDef
.\.venv\Scripts\honcho.exe start -f Procfile -e .env
```

這會嘗試啟動：

| Honcho process | Port | 功能 |
|---|---:|---|
| `ollama` | 11434 | 本機模型 runtime |
| `litellm` | 4000 | 統一模型 gateway |
| `stt` | 8001 | Breeze-ASR STT service |
| `notified` | 8002 | 本機通知 placeholder |
| `agents` | 8003 | 三個 agent route 的共用 runtime |

`PYTHONUTF8=1` 是已實際遇過的 Windows 修正。Honcho 2.0.0 若用 CP950 解碼 UTF-8 `.env`，會出現：

```text
UnicodeDecodeError: 'cp950' codec can't decode byte ...
```

設定後必須在同一個 PowerShell 啟動 Honcho。

## 8. 確認 11434 / 4000 / 8001 / 8002 / 8003

第二個 PowerShell：

```powershell
11434, 4000, 8001, 8002, 8003 | ForEach-Object {
    [pscustomobject]@{
        Port = $_
        Listening = Test-NetConnection -ComputerName 127.0.0.1 -Port $_ -InformationLevel Quiet
    }
}
```

全部顯示 `True` 才表示五個 process 都有 listener。這些 service 沒有一致的 `/health` endpoint，因此 port 檢查只是第一層；還要檢查 LiteLLM alias 與實際模型呼叫。

先檢查**目前 Ollama server 實際看到的模型**：

```powershell
$ollamaModels = (Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags').models.name
$ollamaModels

if ($ollamaModels -notcontains 'qwen2.5:3b' -or
    $ollamaModels -notcontains 'qwen3:4b-instruct-2507-q4_K_M' -or
    $ollamaModels -notcontains 'bge-m3:latest') {
    throw 'Ollama 沒有讀到專案 model 目錄；請檢查 OLLAMA_MODELS 與啟動順序。'
}
```

只看 `ollama list` 或 LiteLLM `/v1/models` 都不足以判斷：`/v1/models` 會列出 config 裡的 alias，即使底層 `qwen2.5:3b` 不存在也可能出現 `local-qwen`。

```powershell
(Invoke-RestMethod -Uri 'http://127.0.0.1:4000/v1/models').data.id
```

預期看到：

- `local-qwen`
- `local-qwen3`
- `local-embed`
- `breeze-asr`
- `claude-haiku`
- `gemini-cheap`
- `gemini-strong`

注意：alias 出現在 `/v1/models` 不代表背後 provider 一定可用。目前 Anthropic 與 Gemini key 都未設定，所以三個雲端 alias 即使出現在清單也不可呼叫。Gemini 是先前曾成功呼叫的歷史紀錄，不代表現在仍可用。

## 9. 確認 `local-qwen`、`local-qwen3` 與 `local-embed`

### `local-qwen`

```powershell
$chatBody = @{
    model = 'local-qwen'
    messages = @(@{ role = 'user'; content = '只回覆 OK' })
} | ConvertTo-Json -Depth 5

$chatResponse = Invoke-RestMethod `
    -Uri 'http://127.0.0.1:4000/v1/chat/completions' `
    -Method Post `
    -ContentType 'application/json' `
    -Body $chatBody

$chatResponse.choices[0].message.content
```

成功時會看到模型文字回覆。

### `local-qwen3`

新模型使用相同 API，只需更換 request 的 alias：

```powershell
$chatBody = @{
    model = 'local-qwen3'
    messages = @(@{ role = 'user'; content = '只回覆 OK' })
} | ConvertTo-Json -Depth 5

$chatResponse = Invoke-RestMethod `
    -Uri 'http://127.0.0.1:4000/v1/chat/completions' `
    -Method Post `
    -ContentType 'application/json' `
    -Body $chatBody

$chatResponse.choices[0].message.content
```

### 切換 workflow 使用的 agent 模型

模型有兩層設定，不能混為一談：

1. [gateway/config.yaml](../gateway/config.yaml) 定義 alias 對應的 provider；例如 `local-qwen3` 對應 Ollama 的 `qwen3:4b-instruct-2507-q4_K_M`。
2. [workflows/definitions/](../workflows/definitions/) 內每個 step 的 `model:` 才決定 workflow 實際使用哪個 alias。

只想測試模型時，不必修改 workflow，直接在上面的 LiteLLM request 將 `model` 設成 `local-qwen3` 即可。要讓某份 workflow 改用新模型時：

1. 停止 Terminal A（Services）與 Terminal B（Workers）。
2. 在選定的 workflow YAML 內，只將要切換的 agent step（例如 `stt`、`check`、`notified`）之 `model: gemini-cheap` 改為 `model: local-qwen3`。
3. 不要修改 `breeze-asr` 或 `local-embed`；前者是語音辨識，後者是 embedding，不是 agent 對話模型。
4. 依第 6 至 7 節，讓 Terminal A、B 使用同一個 `WORKFLOW_DEF_PATH` 重新啟動。
5. 先執行本節的 LiteLLM request，再執行 Agent Runtime 基本 request，最後才觸發完整 workflow。

若要退回舊本機模型，將相同 `model:` 改為 `local-qwen`；要恢復雲端模型，改回對應 alias，並先確認所需 API key 已設定。修改 workflow YAML 後必須重啟 Runtime 與 workers，既有 process 不會熱載入變更。

### `local-embed`

```powershell
$embedBody = @{
    model = 'local-embed'
    input = @('embedding smoke test')
} | ConvertTo-Json -Depth 5

$embedResponse = Invoke-RestMethod `
    -Uri 'http://127.0.0.1:4000/v1/embeddings' `
    -Method Post `
    -ContentType 'application/json' `
    -Body $embedBody

$embedResponse.data[0].embedding.Count
```

`bge-m3` 在這個專案的 embedding 維度應為 `1024`。

### 除外責任場景的一次性保單 seed

只有 `stt_exclusion_notify` 需要這一步。先在 pgAdmin Query Tool 查詢：

```sql
SELECT count(*)
FROM store
WHERE prefix LIKE '_global.semantic.insurance_product.%';
```

目前 Windows 實機是 `59`，與 [kgi_ltc.yaml](../data/insurance_product/kgi_ltc.yaml) 的理論筆數一致，表示 seed 已完成，可以跳過。新的空資料庫或筆數不符時，先確認本節的 `local-embed` 呼叫回傳 1024 維，再執行：

```powershell
$RepoRoot = 'D:\Projects\multi-agent平台架設\multi-agent-platform'
if (-not (Test-Path -LiteralPath $RepoRoot -PathType Container)) {
    throw "找不到 repository：$RepoRoot"
}
$WorkspaceRoot = Split-Path -Parent $RepoRoot
Set-Location -LiteralPath $RepoRoot
$env:PYTHONUTF8 = '1'
$env:UV_CACHE_DIR = Join-Path $WorkspaceRoot '.uv-cache'
.\.venv\Scripts\python.exe -m scripts.seed_insurance_memory
```

成功時最後會印出 `Seeded 59 item(s) total.`。腳本使用固定 key upsert，可安全重跑；但已經是 59 筆時沒有重跑的必要。Windows 不使用 `uv run python -m scripts.seed_insurance_memory`，避免 `uv` 回到 C 槽預設 cache。若 traceback 最後是 `KeyboardInterrupt`，代表執行被手動中止，不是 OpenAI SDK 或 seed 資料主動拋出的錯誤。

## 10. Agent Runtime 基本 request

這個 request 不經過 STT，可獨立確認 port 8003 的 `check` route 與 Terminal A 啟動時載入的 workflow model。目前兩份 workflow YAML 都宣告 `local-qwen3`，所以回應與 `call_log` 的 model 應對應 `local-qwen3`：

```powershell
$agentBody = @{
    thread_id = "manual-check-$([guid]::NewGuid())"
    input = @{ transcript = '今天討論台積電的技術發展。' }
    context = @{ tenant_id = 'default' }
} | ConvertTo-Json -Depth 5

Invoke-RestMethod `
    -Uri 'http://127.0.0.1:8003/check/run' `
    -Method Post `
    -ContentType 'application/json' `
    -Body $agentBody
```

成功時預期 `status` 是 `ok`，`output.mentions_tsmc` 是 `True`。這只驗證 check agent，不等於 STT 或完整 workflow 成功。

## 11. 啟動 event-driven workers

第二個長駐 PowerShell（Terminal B：Workers）：

### 11.1 台積電場景（Workers）

```powershell
$RepoRoot = 'D:\Projects\multi-agent平台架設\multi-agent-platform'
if (-not (Test-Path -LiteralPath $RepoRoot -PathType Container)) {
    throw "找不到 repository：$RepoRoot"
}
$WorkspaceRoot = Split-Path -Parent $RepoRoot
Set-Location -LiteralPath $RepoRoot
$env:PYTHONUTF8 = '1'
$env:UV_CACHE_DIR = Join-Path $WorkspaceRoot '.uv-cache'
$WorkflowDef = 'workflows/definitions/stt_check_notify.yaml'
if (-not (Test-Path -LiteralPath $WorkflowDef -PathType Leaf)) {
    throw "找不到 workflow：$WorkflowDef"
}
$env:WORKFLOW_DEF_PATH = $WorkflowDef
.\.venv\Scripts\honcho.exe start -f Procfile.workers -e .env
```

### 11.2 除外責任場景（Workers）

```powershell
$RepoRoot = 'D:\Projects\multi-agent平台架設\multi-agent-platform'
if (-not (Test-Path -LiteralPath $RepoRoot -PathType Container)) {
    throw "找不到 repository：$RepoRoot"
}
$WorkspaceRoot = Split-Path -Parent $RepoRoot
Set-Location -LiteralPath $RepoRoot
$env:PYTHONUTF8 = '1'
$env:UV_CACHE_DIR = Join-Path $WorkspaceRoot '.uv-cache'
$WorkflowDef = 'workflows/definitions/stt_exclusion_notify.yaml'
if (-not (Test-Path -LiteralPath $WorkflowDef -PathType Leaf)) {
    throw "找不到 workflow：$WorkflowDef"
}
$env:WORKFLOW_DEF_PATH = $WorkflowDef
.\.venv\Scripts\honcho.exe start -f Procfile.workers -e .env
```

這批 process 包含 Master Agent、`worker-all` 與 `memory-writer`。跑 smoke test 前必須先關閉這批 workers，否則相同 consumer group 可能搶走測試事件。

## 12. 觸發 workflow

第三個 PowerShell（Terminal C：Client）：

### 12.1 觸發台積電場景

```powershell
$RepoRoot = 'D:\Projects\multi-agent平台架設\multi-agent-platform'
if (-not (Test-Path -LiteralPath $RepoRoot -PathType Container)) {
    throw "找不到 repository：$RepoRoot"
}
Set-Location -LiteralPath $RepoRoot
$WorkflowDef = 'workflows/definitions/stt_check_notify.yaml'
.\.venv\Scripts\python.exe -m orchestrator.trigger `
    --workflow-def $WorkflowDef `
    --payload '{\"audio_ref\":\"samples/gen_tsmc_01.wav\"}'
```

### 12.2 觸發除外責任場景

```powershell
$RepoRoot = 'D:\Projects\multi-agent平台架設\multi-agent-platform'
if (-not (Test-Path -LiteralPath $RepoRoot -PathType Container)) {
    throw "找不到 repository：$RepoRoot"
}
Set-Location -LiteralPath $RepoRoot
$WorkflowDef = 'workflows/definitions/stt_exclusion_notify.yaml'
.\.venv\Scripts\python.exe -m orchestrator.trigger `
    --workflow-def $WorkflowDef `
    --payload '{\"audio_ref\":\"samples/gen_policy_01.wav\"}'
```

這是目前 VS Code 實際使用的 **Windows PowerShell 5.1** 寫法。JSON 內的 `\"` 不能省略，否則 PowerShell 5.1 呼叫 Python `.exe` 時會移除雙引號，並在 `json.loads()` 出現 `JSONDecodeError: Expecting property name enclosed in double quotes`。若終端機是 PowerShell 7，改用未跳脫形式 `--payload '{"audio_ref":"samples/gen_tsmc_01.wav"}'`。

命令成功送出後會印出 `thread_id`。請先複製保存；後續查詢都以它為索引。

> [!NOTE]
> `stt_check_notify` 與 `stt_exclusion_notify` 都曾依這條 event-driven 路徑完成一次實機執行。這是單次成功紀錄，不代表長時間常駐或異常自動恢復已驗證。

## 13. 查看結果、thread_id 與 Agent / MCP log

### 用 repository 內建工具

```powershell
$ThreadId = 'REPLACE_WITH_THREAD_ID'
.\.venv\Scripts\python.exe -m persistence.history $ThreadId
```

輸出包含 checkpoint 與 `call_log`。詳細欄位見 [observability.md](observability.md)。

### 用 pgAdmin Query Tool

查最近五次事件驅動 run：

```sql
SELECT thread_id, workflow_name, current_step, status, updated_at
FROM orchestrator_runs
ORDER BY updated_at DESC
LIMIT 5;
```

查某次 run 的 LLM、MCP tool 與 memory 稽核紀錄：

```sql
SELECT created_at, node, kind, name, is_error, latency_ms
FROM call_log
WHERE thread_id = '<thread_id>'
ORDER BY created_at;
```

五個 service 的即時 log 在第一個 Honcho terminal；Master / worker / memory-writer 的 log 在第二個 Honcho terminal。每一行前面的 process 名稱就是排查入口。

## 14. 正常成功時應看到什麼

完整事件驅動 workflow 成功時，應同時符合：

1. trigger 印出一個新的 `thread_id`。
2. workers log 依序處理 `stt`、`check`、`notified`。
3. `orchestrator_runs.status` 最後是 `completed`。
4. `persistence.history` 顯示各 step 的 checkpoint / state。
5. `call_log` 可看到對應 agent 的 LLM、tool 或 memory 紀錄。
6. 目前兩份 workflow 的 agent LLM model name 應是 `local-qwen3`；embedding 仍應是 `local-embed`，語音辨識仍由 `breeze-asr` 負責。
7. `should_notify=false` 時 `notified_log=[]` 是正常結果，而且不應產生通知 LLM / tool call。

2026-08-27 的 `thread_id=e138228b-317b-4cc3-bc75-8496b26e14f2` 已同時符合上述條件，資料庫最終狀態為 `completed`。這只證明單次 event-driven 執行成功；仍不能把它等同於 Windows 登入後自動啟動、背景常駐或異常後自動恢復已完成。

## 15. 失敗時依 process / port / log 排查

| 現象 | 先查 port / process | 主要 log | 常見原因 |
|---|---|---|---|
| Ollama 無法啟動 | 11434 / `ollama` | 第一個 Honcho terminal 的 `ollama` | Windows 背景版 Ollama 已占用 port；`OLLAMA_MODELS` 沒在 server 啟動前設到 D 槽 |
| Alias 存在但 `model not found` | 11434 / `/api/tags` | `ollama` 與 `litellm` | 目前 Ollama server 使用另一個 model 目錄；退出背景 Ollama，在同一 terminal 先設 `OLLAMA_MODELS` 再由 Honcho 啟動 |
| LiteLLM 無法連線 | 4000 / `litellm` | `litellm` | config 載入錯誤、Ollama 不可達；雲端 alias 真正呼叫時另需 key |
| STT 失敗或卡住 | 8001 / `stt` | `stt` | `HF_HUB_CACHE` 未指向 D 槽已下載權重、GPU 被其他程式佔滿、CUDA OOM、音檔損壞或編碼不支援 |
| 通知失敗 | 8002 / `notified` | `notified` | service 未啟動；目前只是假實作，不會真的寄信或送 Slack |
| Agent HTTP 失敗 | 8003 / `agents` | `agents` | workflow YAML load 失敗、LiteLLM 不可達、input schema 不符；舊版程式若見 `McpError: Connection closed`，檢查 MCP 子行程是否繼承 `UV_CACHE_DIR` |
| trigger 已印出 `thread_id`，接著出現 `pool-2`／`OSError(22)` | 5432、五個 application port、Master/worker 數量 | workers 與 `agents` | Windows Honcho 已退出但 `uv`／Python 孫程序殘留；多組 workers 與 connection pools 同時運行 |
| trigger 後沒推進 | `master` / `worker-all` | workers terminal | `Procfile.workers` 未啟動、兩批 process 的 `WORKFLOW_DEF_PATH` 不一致、PostgreSQL / event bus 問題 |
| 查不到 run | PostgreSQL | trigger 與 `master` | 觸發未成功、查錯資料庫、`PERSISTENCE_DATABASE_URL` 不一致 |
| `UnicodeDecodeError: cp950` | `honcho` | 啟動 terminal | 啟動前沒有在同一個 PowerShell 設定 `$env:PYTHONUTF8='1'` |

## 16. 關閉

先在兩個 Honcho terminal 分別按 `Ctrl+C`。確認五個 application port 都是 `False`：

```powershell
11434, 4000, 8001, 8002, 8003 | ForEach-Object {
    $listener = Get-NetTCPConnection -State Listen -LocalPort $_ -ErrorAction SilentlyContinue
    [pscustomobject]@{
        Port = $_
        Listening = $null -ne $listener
        PID = if ($listener) { $listener.OwningProcess } else { $null }
    }
}
```

若任何一項仍是 `True`，或 Honcho terminal 已關閉但背景仍有 Master/worker，使用 repository 內的範圍限定腳本。先以 `-WhatIf` 預覽，再實際清理：

```powershell
$RepoRoot = 'D:\Projects\multi-agent平台架設\multi-agent-platform'
if (-not (Test-Path -LiteralPath $RepoRoot -PathType Container)) {
    throw "找不到 repository：$RepoRoot"
}
Set-Location -LiteralPath $RepoRoot
.\scripts\stop_windows_stack.ps1 -WhatIf
.\scripts\stop_windows_stack.ps1
```

腳本會追蹤本 repository 的 Honcho／uv／Python 程序樹與 Agent Runtime 的 MCP 子程序，也會停止實際占用 11434 的 `ollama serve`；它不會停止 PostgreSQL 或 VS Code。清理完成時預期 `5432=True`，`11434/4000/8001/8002/8003=False`。PowerShell 若因 execution policy 阻擋本機腳本，可執行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\stop_windows_stack.ps1
```

不要用未限制 CommandLine／repository 範圍的批次 `Stop-Process -Force` 取代這個腳本。

## 17. 安裝前或服務停止時可做的靜態檢查

```powershell
.\.venv\Scripts\python.exe scripts/static_compat_check.py
.\.venv\Scripts\python.exe -m services.stt.temp_audio_smoke_test
```

第一項檢查 Python 語法、編碼、設定引用與 Markdown 連結；第二項驗證 Windows 暫存音檔行為。它們不會驗證 PostgreSQL、模型、LLM API 或完整 workflow。

其他 smoke tests、前置條件與目前 CI 狀態見 [testing.md](testing.md)。
