# Windows / VS Code 從零執行

此手冊不需要 Codex 或其他 AI 編輯器。安裝系統工具可能需要管理員權限；專案命令在 VS Code 終端機執行。
這是一套可操作的流程，完整的新機安裝與兩份 workflow 端到端驗收仍需實機完成；歷史結果見 [歷史快照](current-windows-status.md)。

## 1. 支援基準與工具

| 項目 | 本版基準／限制 |
|---|---|
| OS / Shell | Windows x64、Windows PowerShell 5.1 或 PowerShell 7 |
| Python | 3.11，由 `.python-version` 指定；不要把 `>=3.11` 解讀為所有新版都已實測 |
| 套件管理 | uv，依 repository 的 `uv.lock` 安裝 |
| PostgreSQL / pgvector | 歷史驗證為 PostgreSQL 18.6 / pgvector 0.8.6；其他組合需自行驗證 |
| GPU / CPU | Windows lock 使用 CUDA 13.2 PyTorch index；ASR 有 CPU 分支，但本版未驗證獨立 CPU 安裝或效能 |
| 模型 | Qwen3、bge-m3、Breeze-ASR-25；首次準備需網路與足夠磁碟／記憶體 |
| macOS / Linux | 不是本 Windows launcher 的支援範圍；[macOS 歷史參考](legacy-macos.md) 未重新驗證 |

先安裝 [Git](https://git-scm.com/downloads)、[VS Code](https://code.visualstudio.com/download)、
[uv](https://docs.astral.sh/uv/getting-started/installation/)、
[PostgreSQL Windows installer](https://www.postgresql.org/download/windows/) 與
[Ollama](https://ollama.com/download/windows)。
PostgreSQL installer 選擇 server、command line tools；pgAdmin 為可選 GUI。

安裝後重新開啟 VS Code，終端機選 PowerShell，確認：

```powershell
git --version
uv --version
ollama --version
$PSVersionTable.PSVersion
```

若找不到命令，依安裝工具提示把其實際安裝目錄加入 PATH，再重新開啟終端機。不要複製別人的使用者路徑。

## 2. Clone 與建立 Python 環境

在自己選擇的父目錄開啟終端機：

```powershell
git clone https://github.com/CCCChrissss/multi-agent-platform.git
Set-Location multi-agent-platform
code .
```

沒有 `code` 命令時，用 VS Code 的「檔案 → 開啟資料夾」開啟 clone。
接下來所有命令的工作目錄都是含 `pyproject.toml` 的 repository 根目錄，資料夾名稱與磁碟位置可以不同。

```powershell
uv python install 3.11
uv sync --locked
.\.venv\Scripts\python.exe --version
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\dev.ps1 check
```

成功條件：Python 為 3.11.x，安裝成功，check 各測試通過。
`--locked` 要求 lock 不被改寫；若 lock 與專案宣告不一致，先確認 checkout，向維護者回報，不要直接重建 lock 掩蓋問題。
安裝會包含現有 PyTorch、Transformers 等相依套件；本次沒有拆分 CPU/GPU 或精簡套件組。
詳見 [uv 鎖定與同步](https://docs.astral.sh/uv/concepts/projects/sync/)。

本手冊所有 `dev.ps1` 指令都已使用 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File`；
這只對該次子程序生效，不修改全機或使用者的持久 Execution Policy。

## 3. 建立設定並準備自己的資料庫

先在 repository 根目錄建立自己的 `.env`：

```powershell
if (-not (Test-Path -LiteralPath .env)) {
    Copy-Item -LiteralPath .env.example -Destination .env
}
```

用 VS Code 開啟 `.env`，設定 `PERSISTENCE_DATABASE_URL`，並確認 PostgreSQL Windows service 已啟動。
URL 內帳號／密碼如含保留字元，需使用 percent encoding；不要提交或貼出實際值。主要交接流程不要求
`psql` 在 PATH，也不寫死 PostgreSQL 安裝位置；專案使用 `.venv` 內既有的 psycopg：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\dev.ps1 db-check
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\dev.ps1 db-init
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\dev.ps1 db-check
```

- `db-check` 是唯讀診斷；database、vector 或必要 table 缺少時 exit code 為 1。
- `db-init` 會建立 `.env` 指定的 database、執行 `CREATE EXTENSION IF NOT EXISTS vector`，並呼叫
  Event Bus、checkpointer、call log、run state 與 memory store 各自既有的冪等 setup。
- 重跑 `db-init` 不會刪除、清空或重建既有 database。
- 輸出只顯示 host、port 與 database；不顯示密碼。

成功結尾應為：

```text
[OK] Project schema contains 9 required tables
[OK] Database init completed
```

上述 Bypass 形式已適用於 `db-init`，不會修改永久 Execution Policy。
`psql` 保留給進階人工 SQL 診斷，不是首次啟動的必要條件。

### pgvector 尚未安裝

若 `db-init` 顯示：

```text
[MISSING] pgvector is not installed for this PostgreSQL server
```

代表目前正在執行的 PostgreSQL 安裝目錄裡沒有 server extension；這不是 Python 的 `pgvector`
套件，也不能用 `uv` 安裝。先透過 SQL 的 `data_directory`／`config_file` 或目前 service 設定確認
正在使用哪一套 PostgreSQL，避免電腦上多套安裝時把 extension 裝錯位置。

依 [pgvector 官方 Windows 說明](https://github.com/pgvector/pgvector#installation) 安裝 Visual Studio
Build Tools 的 C++ workload，並在「x64 Native Tools Command Prompt」或已載入 x64 Developer
PowerShell 的系統管理員終端機編譯。下列 PowerShell 會動態找出 Build Tools；`PGROOT` 必須改成
**目前 server 的實際安裝根目錄**，且該目錄下要有 `include\server\postgres.h`：

```powershell
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
$vsInstall = & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $vsInstall) { throw '找不到 C++ Build Tools，請先完成安裝。' }
& (Join-Path $vsInstall 'Common7\Tools\Launch-VsDevShell.ps1') -Arch amd64 -HostArch amd64 -SkipAutomaticLocation
$env:PGROOT = Read-Host '輸入目前 PostgreSQL 安裝根目錄（例如 C:\Program Files\PostgreSQL\18）'
if (-not (Test-Path (Join-Path $env:PGROOT 'include\server\postgres.h'))) {
    throw 'PGROOT 不是完整 PostgreSQL server/development 安裝。'
}
$buildPath = Join-Path $env:TEMP ('pgvector-build-' + [guid]::NewGuid().ToString('N'))
git clone --branch v0.8.6 --depth 1 https://github.com/pgvector/pgvector.git $buildPath
if ($LASTEXITCODE -ne 0) { throw 'pgvector clone failed' }
Push-Location -LiteralPath $buildPath
try {
    nmake /F Makefile.win
    if ($LASTEXITCODE -ne 0) { throw 'pgvector build failed' }
    nmake /F Makefile.win install
    if ($LASTEXITCODE -ne 0) { throw 'pgvector install failed' }
} finally {
    Pop-Location
}
```

安裝後回到一般權限的 VS Code，重新執行 `db-init`；它會啟用 extension 並建立專案 schema。
若編譯曾指向另一套 PostgreSQL，先在 pgvector 原始碼目錄執行 `nmake /F Makefile.win clean` 再重建。
工具位置偵測依據：[Microsoft vswhere](https://github.com/microsoft/vswhere)；
PowerShell 編譯環境依據：[Developer PowerShell](https://learn.microsoft.com/en-us/visualstudio/ide/reference/command-prompt-powershell?view=vs-2022)。

## 4. 其他環境設定

資料庫 URL 已在上一節設定。其餘 `.env` 欄位依實際使用功能填寫；不用的雲端 provider key 保持空白，
不要為了通過檢查填入假值。
設定責任：

| 設定 | 用途 |
|---|---|
| PERSISTENCE_DATABASE_URL | 本專案資料庫，不是 LiteLLM 的 DATABASE_URL |
| GEMINI_API_KEY / ANTHROPIC_API_KEY | 僅使用對應雲端模型時需要；不用時留空 |
| HF_HUB_CACHE / UV_CACHE_DIR | 選填，預設使用工具自己的快取；覆寫請用自己可寫入的絕對路徑 |
| OLLAMA_MODELS | 選填，只影響之後啟動的 Ollama server |
| WORKFLOW_DEF_PATH | 建議留為註解，使用下方 -Workflow 選擇；有啟用值時必須與選項相同 |

同一套 managed groups 不能混用不同 workflow。每個啟動命令都要傳同一個 `-Workflow`。
launcher 會拒絕已存在的設定衝突；服務準備流程之外，不要修改正在使用的 `.env`。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\dev.ps1 doctor
```

doctor 不連 DB、不呼叫模型、不安裝套件，只報告本機前置條件。exit code 1 表示有缺項。
它只檢查套件是否存在，不保證版本吻合或 GPU／模型可用；不要把 doctor 全綠當作端到端成功。

## 5. Ollama 與模型準備

先決定 Ollama 的管理方式，兩者擇一：

- 已有 Ollama Desktop／自己管理的 server：保持它執行，不再啟動第二份。專案 stop 不會停止它。
- 沒有 server：在獨立 VS Code terminal 執行 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\dev.ps1 ollama`，保持終端機開啟；這份才由專案管理。

若 11434 被占用，launcher 會拒絕再啟動，不會殺掉現有 listener。
另開終端機下載目前工作流需要的模型：

```powershell
ollama pull qwen3:4b-instruct-2507-q4_K_M
ollama pull bge-m3
ollama list
```

只在使用 `local-qwen` 舊 alias 時才需要另外下載 `qwen2.5:3b`。
Ollama 模型清單必須包含實際使用的兩個模型；自訂 `OLLAMA_MODELS` 應在 server 啟動之前生效。

`gateway/config.yaml` 會為 `local-qwen3` 指定 `num_ctx: 8192` 與 `max_tokens: 512`。第二套 exclusion workflow 會把多層記憶瀏覽結果送回模型，Ollama 在較小顯示記憶體機器上的預設 4096 context 不足以穩定執行。服務啟動並完成一次模型呼叫後，以 `ollama ps` 驗證 `CONTEXT` 為 `8192`，且 `PROCESSOR` 應維持 `100% GPU`；否則應降低同時執行量或改用有足夠記憶體的環境，而不是只提高 HTTP timeout。

Breeze-ASR-25 在首次 STT 呼叫時下載／載入，因此首次請求可能超過 workflow 的時間預算。
先在啟動平台服務前直接預熱：

```powershell
$env:PYTHONUTF8 = '1'
.\.venv\Scripts\python.exe -c "from dotenv import load_dotenv; load_dotenv(); from services.stt.breeze_asr import transcribe; print(transcribe('samples/gen_tsmc_01.wav'))"
```

成功條件是印出非空的中文轉錄結果。這一步會下載模型、進行推論；完成後程序退出，釋放其裝置資源。
Breeze 的實際模型來源見 [模型頁](https://huggingface.co/MediaTek-Research/Breeze-ASR-25)。
CPU 分支或 GPU 可用性需在自己的機器確認；不以歷史 GPU 結果代替當前驗證。

## 6. 啟動 services

Terminal A：台積電

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\dev.ps1 services -Workflow stt_check_notify
```


Terminal A：除外責任

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\dev.ps1 services -Workflow stt_exclusion_notify
```

啟動 LiteLLM 4000、STT 8001、placeholder notified 8002、Agent Runtime 8003。
Ollama 由上一節獨立管理。每組由 Python supervisor 管理，log 顯示在該終端機，`.run/` 只保存非機密 owner metadata。
launcher 會用 python-dotenv 讀取一次 `.env`，再把結果傳給 Honcho；Honcho 不會重新用 POSIX 規則解析 Windows 反斜線路徑。
launcher 也會移除從 Windows／父程序繼承的通用 `DEBUG` 變數，避免 LiteLLM 把其他工具使用的
`DEBUG=release` 誤解成布林 `--debug`。不要在 `.env` 宣告通用 `DEBUG`；需要除錯時使用個別元件
文件所定義的專用設定或直接執行其 CLI flag。
`status` 表示 supervisor 存活，不代表每個服務 ready。

Terminal C 執行以下檢查：

```powershell
11434, 4000, 8001, 8002, 8003 | ForEach-Object {
    [pscustomobject]@{Port=$_; Listening=Test-NetConnection -ComputerName 127.0.0.1 -Port $_ -InformationLevel Quiet}
}
(Invoke-RestMethod http://127.0.0.1:4000/v1/models).data.id
Invoke-RestMethod http://127.0.0.1:8003/openapi.json | Select-Object openapi
```

ports 必須都可連線。alias 列出只代表 gateway 載入設定，不能證明模型推論成功。執行實際模型探測：

```powershell
.\.venv\Scripts\python.exe -c "from gateway.client import chat_json; print(chat_json('local-qwen3', 'Return JSON only.', 'Return an object with ok=true.'))"
.\.venv\Scripts\python.exe -c "import asyncio; from gateway.client import aembed; result=asyncio.run(aembed('local-embed', ['test'])); print(len(result[0]))"
```

預期為含 `ok: true` 的物件，以及 embedding 維度 `1024`。呼叫可能寫入 call log，需要自己的 DB。

## 7. 啟動 workers、觸發與驗收

Terminal B：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\dev.ps1 workers -Workflow stt_check_notify
```

Terminal C：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\dev.ps1 trigger -Workflow stt_check_notify
```

trigger 等價命令如下，可直接操作並換自己的 JSON 檔案：

```powershell
.\.venv\Scripts\python.exe -m orchestrator.trigger --workflow-def workflows/definitions/stt_check_notify.yaml --payload-file samples/payloads/tsmc.json
```

JSON 檔案以 UTF-8 儲存，可有 BOM；必須是物件。音檔相對路徑依執行程序的工作目錄解析，managed commands 統一使用 repository 根目錄。
舊 `--payload` 仍可用，兩種參數不能同時指定。

保存輸出的新 `thread_id`，再查：

```powershell
$ThreadId = Read-Host '輸入剛取得的 thread_id'
.\.venv\Scripts\python.exe -m persistence.history $ThreadId
```

另在 `psql -h localhost -U postgres -d agent_architecture_test` 的互動提示字元執行：

```sql
SELECT thread_id, workflow_name, current_step, status, updated_at
FROM orchestrator_runs ORDER BY updated_at DESC LIMIT 5;
```

成功必須同時有：新的 run 最終為 `completed`、stt/check/notified 輸出與 checkpoint、對應 call log。
`needs_review`、超時、只有 thread_id 或 ports 開啟都不算完成。
通知目前是 placeholder，不會真的寄信／發 Slack。

## 8. 除外責任 workflow

先停止已管理的 groups，再以 `-Workflow stt_exclusion_notify` 重啟 services。
Ollama 與 workflow 選擇無關；只要 server 與所需模型仍可用，不必重新啟動。
services ready 後初始化示範保單：

```powershell
.\.venv\Scripts\python.exe -m scripts.seed_insurance_memory
```

此命令會寫入自己的 DB；目前內建資料預期為 `Seeded 59 item(s) total.`，以當前 seed 資料為準。
固定 key upsert 會更新同 key 內容，所以不要對已有人工修改的共用資料庫直接重跑。
再啟動及觸發：

```powershell
# Terminal B
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\dev.ps1 workers -Workflow stt_exclusion_notify
# Terminal C
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\dev.ps1 trigger -Workflow stt_exclusion_notify
```

沿用上一節的驗收方式。保單 seed 不會建立已審核的 episodic/procedural；
完整蒸餾流程另見 [知識蒸餾手冊](knowledge-distillation-windows.md)。

目前 `samples/gen_policy_01.wav` 是刻意設計的邊界案例，正確主結果是
`involves_exclusion=false`，因此 `notified_log=[]`、沒有對外通知才是正常行為。這一輪仍必須同時驗證：

- run 為 `completed`，且有 3 個 checkpoints；
- call log 中有保單根目錄的 `browse`，以及針對除外原因與給付／失能門檻的 semantic `recall`；
- `memory_writer` 在 `default/episodic/stt_exclusion_notify/check` 寫入本次 thread 的 `pending` 記憶。

只看到 `completed`、但沒有上述記憶讀寫證據，不算記憶模組驗證完成。

## 9. Demo UI 與 VS Code Tasks

services / workers ready 後，另開 terminal：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\dev.ps1 ui -Workflow stt_check_notify
```

除外責任使用相同的 `-Workflow stt_exclusion_notify`。
從檔案總管用瀏覽器開啟本 repository 的 `demo/index.html`（file URL）。
UI 會連線 `http://localhost:8010`。不需要 Live Server 或 Codex。
UI 可編輯 YAML；操作完檢查 Git diff。若選了另一個示範 workflow，仍需停止並重啟後端與 workers，不能只切前端選單。

VS Code 選「終端機 → 執行工作」，選 `platform: ...`。
Tasks 呼叫同一份 `scripts/dev.ps1`，沒有額外隱藏步驟；services/workers/UI 會詢問 workflow，必須選同一份。

## 10. 停止與重新執行

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\dev.ps1 stop -Preview
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\dev.ps1 stop
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\dev.ps1 status
```

也可對每個 managed terminal 按 Ctrl+C。supervisor 提供短暫結束時間後，關閉所擁有的 Windows Job 清理子孫程序。
stop 只透過本 checkout 的 owner record 發送請求，不依全機程序名稱或 port 批次終止。
PID 與建立時間不吻合的舊紀錄不會被用來停止程序。
由其他工具啟動的舊 Honcho 或共用 Ollama，應回到其原終端機／管理工具停止。

切換 workflow 前先確認 managed groups 都是 stopped。關閉後若共用 Ollama 或 PostgreSQL 仍執行，是正常現象。
固定 localhost ports 仍限制同機只能執行一套 stack；本版未新增多實例 port 配置。

Windows Job 行為依據：[Microsoft Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)。
VS Code Tasks 設定依據：[官方 Tasks 文件](https://code.visualstudio.com/docs/debugtest/tasks)。

## 11. 重現紀錄

換一個不同名稱、含空白的乾淨 clone，且不複製別人的 .env / .venv，依第 1–10 節操作。
記錄 commit、OS/Shell、Python、uv、PostgreSQL/vector、GPU、模型、兩份新的 thread_id 與停止結果。
本次靜態與測試程序的通過紀錄不等於上述新機整合驗收已完成；未執行欄位應標示「未驗證」。
