# 接手指南：跟著一次呼叫追下去

這份文件是**閱讀程式碼的追蹤路線**：沿著 `stt_check_notify` 從 YAML 追到 MCP 呼叫。它原本以一次真實執行為敘事方式。Windows 的 Breeze-ASR-25 直接推論已驗證，但完整語音 workflow 尚未端到端跑通；Windows 請先讀 [current-windows-status.md](current-windows-status.md) 與 [windows-setup.md](windows-setup.md)，macOS 則保留 [README](../README.md) 的原作者 Bash 流程。

1. **workflow 定義（資料）** — [workflows/definitions/stt_check_notify.yaml](../workflows/definitions/stt_check_notify.yaml)
   每個 step 的 `command_type`/`completion_type`（事件名）、`input_schema`/`output_schema`、`prompt`。

2. **YAML → 物件** — [orchestrator/workflow_def.py](../orchestrator/workflow_def.py)`::load_workflow_def()`
   `WorkflowDef`/`StepDef`。Master Agent 是這份結構的純解釋器，不硬編任何 step 名稱。

3. **啟動一個 run** — [orchestrator/trigger.py](../orchestrator/trigger.py)`::main()` → [orchestrator/master_agent.py:76](../orchestrator/master_agent.py)`::start_run()`
   發布第一個 step 的 command 事件。

4. **推進到下一步** — [orchestrator/master_agent.py:180](../orchestrator/master_agent.py)`::_handle_completion()`
   收到 completion 事件 → 查 `WorkflowDef` 找下一 step → 組 input → 發下一個 command。

5. **Worker 認領並執行** — [orchestrator/worker.py:76](../orchestrator/worker.py)`::_handle_one()`
   validate_input → `handler()` → validate_output → 發布 completion。claim-execute-publish 迴圈。

6. **變成一次 HTTP 呼叫** — [agents/envelope.py:96](../agents/envelope.py) POST `{base_url}/run` → [agents/runtime.py:111](../agents/runtime.py) `POST /{step_name}/run`
   路由本身就是 agent 身分（`stt`/`check`/`notified`）。

7. **Agent 判斷邏輯，這裡碰到 MCP** — [llm/stt_agent.py](../llm/stt_agent.py)
   `gateway.list_openai_tools()` → `chat_with_tools()`（走 [gateway/client.py](../gateway/client.py) / LiteLLM）→ `gateway.call_tool(...)`。

8. **MCP 路由 + RBAC** — [mcp_servers/gateway.py](../mcp_servers/gateway.py)`::MCPGateway.call_tool()`，對照 [mcp_servers/policy.yaml](../mcp_servers/policy.yaml)
   落到實際的 [mcp_servers/stt/server.py](../mcp_servers/stt/) → [services/stt/breeze_asr.py](../services/stt/breeze_asr.py)。

這八步覆蓋 README「分層架構」圖的全部六層。可以先用靜態閱讀走完；實際觸發時再依對應作業系統的指令執行，並用 `thread_id` 對照。之後再讀 [CLAUDE.md](../CLAUDE.md)（原作者的平台目標）、[AGENTS.md](../AGENTS.md)（Codex 協作規範）與 [harness-engineering-principles.md](harness-engineering-principles.md)（Agent 工程原則），補「為什麼這樣設計」。
