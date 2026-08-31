# Windows 本機實機狀態

本文件是目前 Windows / PowerShell 開發環境的操作基準。其他文件若與這裡衝突，先以本文件為準，再回頭確認程式碼與 Git 歷史。

- 檢查日期：2026-08-27
- 分支：`codex/windows-local-stack-setup`
- 本次修正前基準 commit：`f6f5089`
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
| Ollama | 目前執行中、已驗證成功 | port 11434 正在監聽；曾指向 `D:\Projects\multi-agent平台架設\.ollama\models` 並成功呼叫 |
| `qwen2.5:3b` | 已下載、目前可用 | `/api/tags` 可見，`local-qwen` 實際回覆 `OK` |
| `bge-m3` | 已下載、目前可用 | `/api/tags` 可見，`local-embed` 實測維度 1024 |
| LiteLLM Gateway | 目前執行中、已驗證成功 | port 4000 正在監聽；`local-qwen`、`local-embed` 與 `gemini-cheap` 已實際呼叫成功 |
| STT service | 目前執行中、已驗證成功 | port 8001 正在監聽；Breeze 直接 GPU 推論與 workflow 內轉錄已通過 |
| notified service | 目前執行中、已驗證成功 | port 8002 正在監聽；workflow 已呼叫 placeholder Gmail tool，不會真的對外寄送 |
| Agent Runtime | 目前執行中、已驗證成功 | port 8003 正在監聽；修正 MCP 子行程環境後 lifespan、`/openapi.json` 與 workflow agent request 已成功 |

2026-08-27 event-driven 首次觸發成功建立 `thread_id=9310b3c2-fd0e-4730-a4c6-afedf6bbbe55`，但 `stt` 在任何 LLM／MCP call log 寫入前以 `OSError(22, 'Invalid argument')` 進入 `needs_review`，workers terminal 同時出現 `error connecting in 'pool-2'`。實機檢查發現多組 Honcho 孫程序殘留：至少三個 Master、兩個 `worker-all`、兩個 `memory-writer`、多組 LiteLLM 與 33 條 idle PostgreSQL 連線。清理孤兒程序並依 README 乾淨重啟後，新 run `thread_id=e138228b-317b-4cc3-bc75-8496b26e14f2` 已於 2026-08-27 14:18（Asia/Taipei）完成，`orchestrator_runs.status=completed`。commit 前再次檢查時，`11434`、`4000`、`8001`、`8002`、`8003` 與 PostgreSQL `5432` 均在監聽。

## 模型與 workflow 現況

LiteLLM 的 alias 仍保留在 [gateway/config.yaml](../gateway/config.yaml)：

| Alias | Provider / 模型 | 本機是否具備使用條件 |
|---|---|---|
| `local-qwen` | Ollama `qwen2.5:3b` | 目前可用；已透過 LiteLLM 實際呼叫成功 |
| `local-embed` | Ollama `bge-m3` | 目前可用；已透過 LiteLLM 實測得到 1024 維 embedding |
| `breeze-asr` | 本機 STT service / Breeze-ASR-25 | 權重、CUDA、直接推論與 workflow 內轉錄均已驗證 |
| `claude-haiku` | Anthropic | alias 保留；目前沒有 `ANTHROPIC_API_KEY`，不可呼叫 |
| `gemini-cheap` | Gemini | `.env` 已設定 `GEMINI_API_KEY`；乾淨重啟後已由三個 agent 實際呼叫成功 |
| `gemini-strong` | Gemini | `.env` 已設定 `GEMINI_API_KEY`；alias 保留，但本階段沒有實際呼叫 |

兩份 workflow 的實際 model 宣告如下：

| Workflow | `stt` | `check` | `notified` | 目前判定 |
|---|---|---|---|---|
| [stt_check_notify.yaml](../workflows/definitions/stt_check_notify.yaml) | `gemini-cheap` | `gemini-cheap` | `gemini-cheap` | 已在乾淨重啟後完成一次完整 event-driven 執行 |
| [stt_exclusion_notify.yaml](../workflows/definitions/stt_exclusion_notify.yaml) | `gemini-cheap` | `claude-haiku` | `claude-haiku` | 目前缺少雲端 API key，不能完整執行；尚未進行本機 alias 修改 |

`local-embed` 仍只負責 embedding，`breeze-asr` 仍只負責語音辨識；兩者不應改成 `local-qwen`。

## 已完成的單次 workflow 里程碑與剩餘範圍

1. `stt_check_notify` 已使用新的 `thread_id` 取得完整 `stt -> check -> notified` 結果與 call log。
2. `gemini-cheap` 的 `stt`、`check`、`notified` 呼叫都成功，實際 `response_model` 也是 `gemini-cheap`。
3. STT、company lookup、placeholder Gmail notification 與 memory writer 都留下成功的 audit log。
4. 尚未完成的是 Windows 常駐方式的選型、啟動／停止程序、異常重啟與後續驗證；單次 run 成功不能取代這些驗證。
5. `stt_exclusion_notify` 的 `check`／`notified` 仍使用 `claude-haiku`；沒有 Anthropic key 時不能完整執行。

因此，目前可以宣稱 **`stt_check_notify` 的單次 event-driven 語音 workflow 已成功**；不能延伸宣稱 `stt_exclusion_notify` 或 Windows 常駐服務模式已完成。

## 已完成的局部驗證

- `stt_check_notify` 三個 step 曾以 `local-qwen` 完成個別模型／Agent request 驗證；切換為 `gemini-cheap` 後也已完成一次完整 event-driven run。
- LiteLLM 曾成功把 `local-qwen` 路由到 Ollama 的 `qwen2.5:3b`。
- `local-embed` 曾成功使用 `bge-m3`。
- Agent Runtime 的 `check` 基本 request 曾成功使用 `local-qwen`。
- 2026-08-27 修正 MCP 子行程環境後，Agent Runtime lifespan、8003 與 `/openapi.json` 通過；切回正確 Ollama model 目錄後，`check` request 回 `status=ok`、`mentions_tsmc=true`。
- `.venv` 已是 `torch 2.13.0+cu132`，`torch.cuda.is_available()` 為 `True`，CUDA tensor 實際運算已通過。
- Breeze-ASR-25 權重已放在 `D:\Projects\multi-agent平台架設\.hf-cache`，3,086,761,032 bytes 的 `model.safetensors` SHA-256 已驗證為 `c5d952b3bc03ea277209aff0ef5b5c4c055d74449ff794c02d8f4e315fdef6b6`。
- `samples/gen_tsmc_01.wav` 直接轉錄成功，輸出「台積電今天股價創新高投資人非常關注」；峰值 CUDA allocated 約 4.09 GiB、reserved 約 4.51 GiB。
- `should_notify=false` 時，[llm/notify_agent.py](../llm/notify_agent.py) 會在 LLM / tool call 前直接回傳空陣列 `[]`；這是目前程式行為。
- PostgreSQL 的 `orchestrator_runs`、checkpoint 與 `call_log` 查詢流程曾驗證可用。
- Windows 靜態相容性 GitHub Actions job 在 commit `39d6449` 通過。

## 測試狀態

commit `39d6449` 的最新 GitHub Actions 結果為失敗，但不是三個 job 全部失敗：

- `windows-static-compatibility`：成功
- `mcp-server-smoke-tests`：成功
- `gather-concurrency-smoke-test`：失敗

失敗原因已在本機重現並修正測試：舊 gather scenario 用 `should_notify=false`，因此安全短路後根本不會進入要測的 `asyncio.gather()`。現在改用 `should_notify=true`、模擬一次成功 Gmail tool call，實際驗證 prompt recall 與 tool list 並行；獨立的 `llm.notify_agent_smoke_test` 繼續負責驗證負分支回傳 `[]` 且不碰 LLM / tool。

2026-08-27 本機結果：

- `gather_concurrency_smoke_test.py`：通過（三個 scenario）
- `llm.notify_agent_smoke_test`：通過（三個 scenario）
- 五個 dependency-free MCP smoke tests：通過
- `mcp_servers.base_client_env_smoke_test`：通過（傳遞 `UV_CACHE_DIR` / `PYTHONUTF8`，不傳遞 API key 或資料庫連線）
- `scripts/static_compat_check.py`：通過
- `services.stt.breeze_asr_smoke_test`：通過（8 kHz WAV 在無 FFmpeg 環境轉為單聲道 16 kHz）
- `services.stt.temp_audio_smoke_test`：通過

遠端 GitHub Actions 仍要等本次修正 commit / push 後才能判定，現在不能宣稱新的 CI run 已成功。

Windows MCP stdio 子行程的 `UV_CACHE_DIR` 繼承已在共用 `MCPClient` 修正；Agent Runtime 與五個 dependency-free MCP smoke tests 皆已驗證不再需要一次性 wrapper。

GitHub Actions 執行紀錄：[run 32950383731](https://github.com/CCCChrissss/multi-agent-platform/actions/runs/32950383731)。

## Windows 啟動注意事項

Honcho 2.0.0 在這台繁體中文 Windows 上會用 CP950 讀 `.env`；如果 `.env` 含 CP950 無法解碼的 UTF-8 字元，會出現 `UnicodeDecodeError`。啟動前必須在同一個 PowerShell 設定：

```powershell
$env:PYTHONUTF8 = '1'
```

完整的 Windows 啟動、port 檢查、workflow 選擇、workers、trigger 與查詢方式，統一維護在 [windows-setup.md](windows-setup.md)。Breeze-ASR-25、CUDA PyTorch 與一次完整 `stt_check_notify` event-driven run 已完成實機驗證；常駐服務模式仍是下一階段。

Windows Honcho 的 `Ctrl+C` 可能只終止管理程序而留下 `uv`／Python 孫程序。關閉後必須檢查五個 application port；有殘留時使用 `scripts/stop_windows_stack.ps1 -WhatIf` 預覽，再執行腳本清理。該腳本已用 Windows PowerShell 5.1 驗證，保留 PostgreSQL 與 VS Code。

## 文件維護原則

- Windows / PowerShell / Codex 是本 repository 現行操作基準。
- 原作者的 macOS / Bash / Claude Code 內容若尚未在本機重驗，保留為歷史或上游參考，不直接宣稱適用。
- 每完成一個可獨立驗證的階段，再同步更新本文件、測試結果與操作步驟。
- 不把 API key、密碼或其他 secret 寫進文件、`.env.example`、commit 或 GitHub。
