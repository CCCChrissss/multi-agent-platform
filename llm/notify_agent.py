"""Standard MCP agent loop -- scenario-agnostic notification delivery.

list_tools() -> hand schema to an LLM as function-calling tools -> execute
what it picks -> feed each result back as a `tool` message -> let the model
see the outcome and decide whether to call more tools or stop. Same shape as
llm/tsmc_judge.py/llm/stt_agent.py -- lives alongside them (not under
mcp_servers/) because it's scenario-layer agent logic, not an MCP server
shell (docs/exclusion-scenario-plan.md §3.6).

This agent only knows "send or don't send, and if sending, which channel" --
*whether* a given piece of content warrants a notification is a scenario
judgment call this module deliberately does not make (docs/exclusion-scenario-plan.md
§3.5/P0). The caller (e.g. a `check` step) decides `should_notify` and writes
`subject`/`body`; a TSMC-mention rule used to be hardcoded in this module's
system prompt, which coupled the platform's one generic notify agent to one
scenario -- workflows/simple_pipeline.py (frozen, see workflows/parity_check.py)
still needs that old rule, so it now lives in mcp_servers/notified/agent.py
as a compat shim in front of this module instead of in here.
"""

from __future__ import annotations

import asyncio
from typing import Any

from harness.agent_loop import AgentLoopIncomplete, StallGuard, run_tool_calling_loop, wrap_agent_exception
from mcp_servers.gateway import MCPGateway
from persistence.memory_policy import MemoryPolicy
from persistence.memory_prompt import inject_procedural

MODEL_NAME = "gemini-cheap"
MAX_TURNS = 20
_NOTIFY_TOOL_SUFFIXES = ("send_gmail_message", "send_slack_message")

# Default workflow segment for the (workflow_name, step_name) memory scope
# below -- only used when a caller doesn't pass workflow_name (the frozen
# mcp_servers/notified/agent.py compat shim, which only ever serves
# stt_check_notify). Unlike llm/tsmc_judge.py/llm/exclusion_judge.py, this
# module is genuinely shared across workflows (agents/notified/server.py
# calls it for whichever workflow app.state.workflow_name says is live), so
# the scope can't be a module-level constant the way those two are -- doing
# that here would mix stt_check_notify's and stt_exclusion_notify's
# episodic/procedural memory the moment a grant exists for `notified` on
# those kinds (see llm/exclusion_judge.py's _MEMORY_SCOPE comment for why
# that mixing matters, even though no such grant exists yet).
_DEFAULT_WORKFLOW_NAME = "stt_check_notify"
_PROCEDURAL_LIMIT = 10

_SYSTEM_PROMPT = (
    "你是通知決策助手。你會收到一個 should_notify 判斷結果、一則主旨（subject）與內容（body）——"
    "這個判斷是上游已經做完的場景決策，不是你要重新評估的東西。"
    "should_notify 為 true 時，用給定的主旨與內容發送通知，並自行決定要用哪個管道："
    "可以先用 recall_semantic_memory 工具查這位收件人過去表達過的通知管道偏好"
    "（scope=[\"recipient\", 收件人 id]）；查不到就照預設管道（Gmail）發，不是錯誤，不用重試。"
    "should_notify 為 false 時不用發送任何通知，這是正常情況，不是錯誤。"
    "工具執行後你會看到結果；如果失敗，可以決定要不要換個方式重試或放棄，"
    "不要重複重試同一個失敗的呼叫超過一次。"
)


def _finish(should_notify: bool, notified_ok: bool, log: list[str]) -> list[str]:
    if should_notify and not notified_ok:
        raise AgentLoopIncomplete(
            node="notified",
            reason=(
                "should_notify=True but no successful send_gmail_message/send_slack_message "
                f"call was observed; log={log}"
            ),
        )
    return log


async def _recall_prompt(
    store: Any | None, memory_policy: MemoryPolicy | None, tenant: str, recipient_id: str, workflow_name: str
) -> str:
    """None store/policy leaves the prompt untouched -- same no-op contract
    as llm/tsmc_judge.py's equivalent recall wiring. procedural uses the
    platform-generic persistence/memory_prompt.py helper (currently a no-op
    here -- no policy.yaml grant exists for `notified` on this kind yet).

    The recipient's channel preference (semantic memory) is deliberately
    *not* recalled here (docs/long-term-memory-plan.md §3.9/M4.5): unlike
    procedural, which patches blind spots the model can't know it has, the
    model already knows it's about to decide "send or not, which channel" --
    it can ask the `memory__recall_semantic_memory` tool for this recipient's
    preference itself, and a miss just degrades to the default channel.
    `recipient_id` is surfaced in the user message below so the model has
    something to build the tool call's `scope` from."""
    scope = (workflow_name, "notified")
    return await inject_procedural(store, memory_policy, tenant=tenant, scope=scope, base_prompt=_SYSTEM_PROMPT, limit=_PROCEDURAL_LIMIT)


@wrap_agent_exception("notified")
async def decide_and_notify(
    gateway: MCPGateway,
    *,
    should_notify: bool,
    subject: str,
    body: str,
    recipient_id: str = "default",
    store: Any | None = None,
    memory_policy: MemoryPolicy | None = None,
    tenant: str = "default",
    workflow_name: str = _DEFAULT_WORKFLOW_NAME,
) -> list[str]:
    system_prompt, tools = await asyncio.gather(
        _recall_prompt(store, memory_policy, tenant, recipient_id, workflow_name),
        gateway.list_openai_tools(),
    )
    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"should_notify={should_notify}\n收件人 id: {recipient_id}\n主旨: {subject}\n內容: {body}"},
    ]
    log: list[str] = []
    notified_ok = False

    def _on_tool_result(call: Any, arguments: dict, result_text: str, is_error: bool) -> None:
        nonlocal notified_ok
        log.append(f"{call.function.name}({arguments}) -> {result_text}{' [ERROR]' if is_error else ''}")
        if not is_error and call.function.name.endswith(_NOTIFY_TOOL_SUFFIXES):
            notified_ok = True

    # A "no tool call" turn always finishes the loop below, so the only way
    # this loop can get stuck is retrying the same failed send -- exactly
    # the pattern the system prompt already asks the model not to do; this
    # enforces it structurally instead of trusting the instruction alone.
    stall_guard = StallGuard(consecutive_limit=2)
    assistant_message = await run_tool_calling_loop(
        MODEL_NAME,
        messages,
        tools,
        gateway,
        node="notified",
        max_turns=MAX_TURNS,
        stall_guard=stall_guard,
        on_tool_result=_on_tool_result,
        raise_on_max_turns=False,
    )
    if assistant_message is None:
        log.append("(stopped: reached max tool-calling turns)")
    else:
        log.append(assistant_message.content or "(model decided: no notification needed)")
    return _finish(should_notify, notified_ok, log)
