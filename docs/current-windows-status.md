# Windows 本機實機狀態

本文件是目前 Windows / PowerShell 開發環境的操作基準。其他文件若與這裡衝突，先以本文件為準，再回頭確認程式碼與 Git 歷史。

- 檢查日期：2026-08-26
- 分支：`codex/windows-local-stack-setup`
- 基準 commit：`39d6449`
- 開發工具：Codex Desktop + VS Code + PowerShell
- 專案目錄：`D:\Projects\multi-agent平台架設\multi-agent-platform`

## 狀態用語

- **已安裝**：檔案或套件已存在，不代表服務現在正在執行。
- **曾驗證成功**：先前已在這台電腦實際執行成功，但不代表目前仍在執行。
- **目前停止**：本次檢查時沒有 process 監聽該 port。
- **尚未驗證**：尚未在這台電腦完成實際端到端執行，不宣稱可用。

## 已確認項目

| 項目 | 目前狀態 | 實機結果 |
|---|---|---|
| Python | 已安裝 | 專案 `.venv` 使用 Python 3.11 |
| uv | 已安裝 | 執行檔位於 `C:\Users\User\.local\bin\uv.exe` |
| PostgreSQL | 已安裝、曾驗證成功 | PostgreSQL 18.6；本機測試資料庫為 `agent_architecture_test` |
| pgvector | 已安裝、曾驗證成功 | `vector` extension 版本 0.8.6 |
| Ollama | 已安裝、目前停止 | 執行檔位於 `C:\Users\User\AppData\Local\Programs\Ollama\ollama.exe` |
| `qwen2.5:3b` | 已下載 | 模型保存在 D 槽的 Ollama model 目錄 |
| `bge-m3` | 已下載 | 提供 `local-embed` 使用 |
| LiteLLM Gateway | 曾驗證成功、目前停止 | port 4000；保留本機與雲端 provider alias |
| STT service | 曾啟動、目前停止 | port 8001；完整模型推論尚未驗證 |
| notified service | 曾驗證成功、目前停止 | port 8002；目前是本機 placeholder，不會真的寄 Gmail 或 Slack |
| Agent Runtime | 曾驗證成功、目前停止 | port 8003；`stt`、`check`、`notified` 共用同一個 process |

本次檢查時，`11434`、`4000`、`8001`、`8002`、`8003` 都沒有 listener。這是「服務已停止」，不是安裝失敗。

## 模型與 workflow 現況

LiteLLM 的 alias 仍保留在 [gateway/config.yaml](../gateway/config.yaml)：

| Alias | Provider / 模型 | 本機是否具備使用條件 |
|---|---|---|
| `local-qwen` | Ollama `qwen2.5:3b` | 已具備，先前已透過 LiteLLM 實際呼叫成功 |
| `local-embed` | Ollama `bge-m3` | 已具備，先前已實際呼叫成功 |
| `breeze-asr` | 本機 STT service / Breeze-ASR-25 | **尚未具備完整條件** |
| `claude-haiku` | Anthropic | alias 保留；目前沒有 `ANTHROPIC_API_KEY`，不可呼叫 |
| `gemini-cheap` | Gemini | alias 保留；目前沒有 `GEMINI_API_KEY`，不可呼叫 |
| `gemini-strong` | Gemini | alias 保留；目前沒有 `GEMINI_API_KEY`，不可呼叫 |

兩份 workflow 的實際 model 宣告如下：

| Workflow | `stt` | `check` | `notified` | 目前判定 |
|---|---|---|---|---|
| [stt_check_notify.yaml](../workflows/definitions/stt_check_notify.yaml) | `local-qwen` | `local-qwen` | `local-qwen` | agent 決策模型已本機化；完整語音鏈路仍受 Breeze-ASR-25 缺口阻擋 |
| [stt_exclusion_notify.yaml](../workflows/definitions/stt_exclusion_notify.yaml) | `gemini-cheap` | `claude-haiku` | `claude-haiku` | 目前缺少雲端 API key，不能完整執行；尚未進行本機 alias 修改 |

`local-embed` 仍只負責 embedding，`breeze-asr` 仍只負責語音辨識；兩者不應改成 `local-qwen`。

## 目前阻擋完整 workflow 的項目

1. `Breeze-ASR-25` 權重尚未下載完成。
2. 專案 `.venv` 目前是 CPU 版 PyTorch（`torch 2.13.0+cpu`，`cuda_available=False`）。
3. 本機 GPU 是 NVIDIA GeForce RTX 4050 Laptop GPU 6 GB；適合的 CUDA / PyTorch / Breeze 組合尚未安裝與驗證。
4. 五個常駐服務目前都已停止。
5. `stt_exclusion_notify` 仍依賴目前沒有金鑰的 Anthropic / Gemini alias。

因此，目前只能說本機 LLM、embedding、資料庫與個別 service 曾經驗證成功；**不能宣稱完整語音 workflow 已成功**。

## 已完成的局部驗證

- `stt_check_notify` 三個 step 的 model alias 都已是 `local-qwen`。
- LiteLLM 曾成功把 `local-qwen` 路由到 Ollama 的 `qwen2.5:3b`。
- `local-embed` 曾成功使用 `bge-m3`。
- Agent Runtime 的 `check` 基本 request 曾成功使用 `local-qwen`。
- `should_notify=false` 時，[llm/notify_agent.py](../llm/notify_agent.py) 會在 LLM / tool call 前直接回傳空陣列 `[]`；這是目前程式行為。
- PostgreSQL 的 `orchestrator_runs`、checkpoint 與 `call_log` 查詢流程曾驗證可用。
- Windows 靜態相容性 GitHub Actions job 在 commit `39d6449` 通過。

## 已知測試問題

commit `39d6449` 的最新 GitHub Actions 結果為失敗，但不是三個 job 全部失敗：

- `windows-static-compatibility`：成功
- `mcp-server-smoke-tests`：成功
- `gather-concurrency-smoke-test`：失敗

失敗原因已在本機重現：`gather_concurrency_smoke_test.py` 仍預期不通知時回傳 `["no notification needed"]`，目前實作實際回傳 `[]`。這是測試預期值落後於已採用的安全短路行為；本次文件階段不修改測試或程式。

GitHub Actions 執行紀錄：[run 32950383731](https://github.com/CCCChrissss/multi-agent-platform/actions/runs/32950383731)。

## Windows 啟動注意事項

Honcho 2.0.0 在這台繁體中文 Windows 上會用 CP950 讀 `.env`；如果 `.env` 含 CP950 無法解碼的 UTF-8 字元，會出現 `UnicodeDecodeError`。啟動前必須在同一個 PowerShell 設定：

```powershell
$env:PYTHONUTF8 = '1'
```

完整的 Windows 啟動、port 檢查、workflow 選擇、workers、trigger 與查詢方式，統一維護在 [windows-setup.md](windows-setup.md)。在 Breeze-ASR-25 與 PyTorch 環境完成前，其中的完整 workflow 指令只作為下一階段操作程序，不代表已在本機跑通。

## 文件維護原則

- Windows / PowerShell / Codex 是本 repository 現行操作基準。
- 原作者的 macOS / Bash / Claude Code 內容若尚未在本機重驗，保留為歷史或上游參考，不直接宣稱適用。
- 每完成一個可獨立驗證的階段，再同步更新本文件、測試結果與操作步驟。
- 不把 API key、密碼或其他 secret 寫進文件、`.env.example`、commit 或 GitHub。
