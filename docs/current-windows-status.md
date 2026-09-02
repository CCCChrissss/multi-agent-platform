# Windows 本機實機狀態

本文件是特定日期的 Windows / PowerShell 實機驗證快照，用來區分「曾成功」、「當時停止」與「尚未驗證」。它不是安裝或啟動指令的來源；操作方式以 [windows-setup.md](windows-setup.md) 為準，模型、workflow 與權限則分別以實際 YAML／policy 檔案為準。

- 檢查日期：2026-08-31
- Credential 更新：2026-09-01 已移除所有雲端 API key；2026-09-02 新增本機 `qwen3:4b-instruct-2507-q4_K_M`
- 分支：`codex/windows-local-stack-setup`
- 目前 HEAD：`e42fc04`
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
| PostgreSQL | service 執行中、目前可連線 | PostgreSQL 18.6；本機測試資料庫為 `agent_architecture_test` |
| pgvector | 已安裝、曾驗證成功 | `vector` extension 版本 0.8.6 |
| Ollama | 目前停止、曾驗證成功 | port 11434 未監聽；曾指向 `D:\Projects\multi-agent平台架設\.ollama\models` 並成功呼叫 |
| `qwen2.5:3b` | 已下載、目前服務停止 | Ollama 啟動時 `/api/tags` 可見，`local-qwen` 曾實際回覆 `OK` |
| `qwen3:4b-instruct-2507-q4_K_M` | 已下載、目前服務停止 | 2026-09-02 以 Ollama 直接驗證繁體中文與 tool call；`num_ctx=8192` 時完整載入 GPU，實測 `size_vram=3873366343` bytes |
| `bge-m3` | 已下載、目前服務停止 | Ollama 啟動時 `/api/tags` 可見，`local-embed` 曾實測維度 1024 |
| LiteLLM Gateway | 目前停止、曾驗證成功 | port 4000 未監聽；`local-qwen`、`local-qwen3`、`local-embed` 與 `gemini-cheap` 曾實際呼叫成功 |
| STT service | 目前停止、曾驗證成功 | port 8001 未監聽；Breeze 直接 GPU 推論與 workflow 內轉錄已通過 |
| notified service | 目前停止、曾驗證成功 | port 8002 未監聽；workflow 已呼叫 placeholder Gmail tool，不會真的對外寄送 |
| Agent Runtime | 目前停止、曾驗證成功 | port 8003 未監聽；修正 MCP 子行程環境後 lifespan、`/openapi.json` 與 workflow agent request 已成功 |

2026-08-27 event-driven 首次觸發成功建立 `thread_id=9310b3c2-fd0e-4730-a4c6-afedf6bbbe55`，但 `stt` 在任何 LLM／MCP call log 寫入前以 `OSError(22, 'Invalid argument')` 進入 `needs_review`，workers terminal 同時出現 `error connecting in 'pool-2'`。實機檢查發現多組 Honcho 孫程序殘留：至少三個 Master、兩個 `worker-all`、兩個 `memory-writer`、多組 LiteLLM 與 33 條 idle PostgreSQL 連線。清理孤兒程序並依 README 乾淨重啟後，新 run `thread_id=e138228b-317b-4cc3-bc75-8496b26e14f2` 已於 2026-08-27 14:18（Asia/Taipei）完成，`orchestrator_runs.status=completed`。2026-08-31 整理文件時，五個 application port 均已停止；PostgreSQL Windows service 執行中，並以專案 `.env` 實際連線成功。

## 模型與 workflow 現況

LiteLLM 的 alias 仍保留在 [gateway/config.yaml](../gateway/config.yaml)：

| Alias | Provider / 模型 | 本機是否具備使用條件 |
|---|---|---|
| `local-qwen` | Ollama `qwen2.5:3b` | 目前服務停止；啟動時曾透過 LiteLLM 實際呼叫成功 |
| `local-qwen3` | Ollama `qwen3:4b-instruct-2507-q4_K_M` | 2026-09-02 透過 LiteLLM 實測文字回覆與 `lookup_company` tool call 成功 |
| `local-embed` | Ollama `bge-m3` | 目前服務停止；啟動時曾透過 LiteLLM 實測得到 1024 維 embedding |
| `breeze-asr` | 本機 STT service / Breeze-ASR-25 | 目前服務停止；權重、CUDA、直接推論與 workflow 內轉錄均已驗證 |
| `claude-haiku` | Anthropic | alias 保留；目前沒有 `ANTHROPIC_API_KEY`，不可呼叫 |
| `gemini-cheap` | Gemini | alias 保留；2026-09-01 已移除 `GEMINI_API_KEY`，目前不可呼叫；先前具備 key 時三個 agent 曾實際呼叫成功 |
| `gemini-strong` | Gemini | alias 保留；2026-09-01 已移除 `GEMINI_API_KEY`，目前不可呼叫；先前本階段沒有實際呼叫此 alias |

兩份 workflow 的實際 model 宣告如下：

| Workflow | `stt` | `check` | `notified` | 目前判定 |
|---|---|---|---|---|
| [stt_check_notify.yaml](../workflows/definitions/stt_check_notify.yaml) | `gemini-cheap` | `gemini-cheap` | `gemini-cheap` | 已在乾淨重啟後完成一次完整 event-driven 執行 |
| [stt_exclusion_notify.yaml](../workflows/definitions/stt_exclusion_notify.yaml) | `gemini-cheap` | `gemini-cheap` | `gemini-cheap` | 已完成一次 event-driven 執行，三個 step 與 memory writer 均留下紀錄 |

`local-embed` 仍只負責 embedding，`breeze-asr` 仍只負責語音辨識；兩者不應改成 `local-qwen` 或 `local-qwen3`。目前兩份 workflow 仍宣告 `gemini-cheap`，新增 alias 不會自動切換 workflow。

## 已完成的單次 workflow 里程碑與剩餘範圍

1. `stt_check_notify` 已使用新的 `thread_id` 取得完整 `stt -> check -> notified` 結果與 call log。
2. `gemini-cheap` 的 `stt`、`check`、`notified` 呼叫都成功，實際 `response_model` 也是 `gemini-cheap`。
3. STT、company lookup、placeholder Gmail notification 與 memory writer 都留下成功的 audit log。
4. `stt_exclusion_notify` run `bc894fab-6b30-4ff2-b0e7-db315cbbc4e3` 已完成；`stt`、`check`、`notified` 實際使用 `gemini-cheap`，並寫入一筆 pending episodic。
5. 常駐服務的乾淨啟動／停止程序已整理進 [windows-setup.md](windows-setup.md)；更長時間的異常重啟與穩定性驗證仍屬後續工作。

因此，目前可以宣稱 **兩份示範 workflow 都各自完成過單次 event-driven 執行**；不能延伸宣稱長時間常駐穩定性或知識蒸餾 Windows 全鏈已完成。

上述 workflow 成功紀錄是在 Gemini key 可用時取得。2026-09-01 移除所有雲端 API key 後，兩份 workflow 因三個 step 都宣告 `gemini-cheap`，目前都不能重新完整執行；這不會抹除歷史成功紀錄。

## 長期記憶與知識蒸餾歷史快照

2026-08-31 開始 actor-distinction demo **之前**，曾直接查詢 PostgreSQL `store` 得到：

| Kind | Status | 數量 |
|---|---|---:|
| semantic | active | 59 |
| episodic | pending | 3 |
| episodic | active | 0 |
| procedural | — | 0 |

這張表不是目前 DB 數量。後續已實際執行 actor-distinction demo，並在 procedural review 階段看到 `eval` tenant 的 active staged candidate 與 `default` tenant 的 pending production candidate；兩筆是不同 tenant／用途，不是重複 production 規則。這次文件整併沒有重新查詢或修改 DB，操作前必須使用 [knowledge-distillation-windows.md](knowledge-distillation-windows.md) 的唯讀 SQL 重新確認。

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

commit `39d6449` 的 GitHub Actions 曾失敗，但不是三個 job 全部失敗：

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

後續 commit `e42fc04` 的 [GitHub Actions run 33361205275](https://github.com/CCCChrissss/multi-agent-platform/actions/runs/33361205275) 已確認三個 job 全部成功：`windows-static-compatibility`、`mcp-server-smoke-tests`、`gather-concurrency-smoke-test`。

Windows MCP stdio 子行程的 `UV_CACHE_DIR` 繼承已在共用 `MCPClient` 修正；Agent Runtime 與五個 dependency-free MCP smoke tests 皆已驗證不再需要一次性 wrapper。

歷史失敗紀錄：[run 32950383731](https://github.com/CCCChrissss/multi-agent-platform/actions/runs/32950383731)；目前成功基準：[run 33361205275](https://github.com/CCCChrissss/multi-agent-platform/actions/runs/33361205275)。

## Windows 啟動注意事項

Honcho 2.0.0 在這台繁體中文 Windows 上會用 CP950 讀 `.env`；如果 `.env` 含 CP950 無法解碼的 UTF-8 字元，會出現 `UnicodeDecodeError`。啟動前必須在同一個 PowerShell 設定：

```powershell
$env:PYTHONUTF8 = '1'
```

完整的 Windows 安裝、啟動、port 檢查、workflow 選擇、workers、trigger 與停止方式，唯一詳細來源是 [windows-setup.md](windows-setup.md)。執行結果與 DB 查詢見 [observability.md](observability.md)。Breeze-ASR-25、CUDA PyTorch 與兩份示範 workflow 的單次 event-driven run 已完成實機驗證；知識蒸餾全鏈尚未在目前 Windows 環境重跑。

Windows Honcho 的 `Ctrl+C` 可能只終止管理程序而留下 `uv`／Python 孫程序。關閉後必須檢查五個 application port；有殘留時使用 `scripts/stop_windows_stack.ps1 -WhatIf` 預覽，再執行腳本清理。該腳本已用 Windows PowerShell 5.1 驗證，保留 PostgreSQL 與 VS Code。

## 文件維護原則

- Windows / PowerShell / Codex 是本 repository 現行操作基準。
- 原作者的 macOS / Bash / Claude Code 內容若尚未在本機重驗，保留為歷史或上游參考，不直接宣稱適用。
- 每完成一個可獨立驗證的階段，再同步更新本文件、測試結果與操作步驟。
- 不把 API key、密碼或其他 secret 寫進文件、`.env.example`、commit 或 GitHub。
