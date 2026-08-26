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
from harness.output_capture import ToolCallLog
from mcp_servers.gateway import MCPGateway
from orchestrator.workflow_def import resolve_model, resolve_prompt
from persistence.memory_policy import MemoryPolicy
from persistence.memory_prompt import inject_procedural

MAX_TURNS = 20
_NOTIFY_TOOL_SUFFIXES = ("send_gmail_message", "send_slack_message")

# Default workflow segment for the (workflow_name, step_name) memory scope
# below -- only used when a caller doesn't pass workflow_name (the frozen
# mcp_servers/notified/agent.py compat shim, which only ever serves
# stt_check_notify). Unlike llm/tsmc_judge.py/llm/exclusion_judge.py, this
# module is genuinely shared across workflows (agents/runtime.py's
# _notified_handler calls it for whichever workflow app.state.workflow_name
# says is live), so
# the scope can't be a module-level constant the way those two are -- doing
# that here would mix stt_check_notify's and stt_exclusion_notify's
# episodic/procedural memory the moment a grant exists for `notified` on
# those kinds (see llm/exclusion_judge.py's _MEMORY_SCOPE comment for why
# that mixing matters, even though no such grant exists yet).
_DEFAULT_WORKFLOW_NAME = "stt_check_notify"
_PROCEDURAL_LIMIT = 10


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
    store: Any | None, memory_policy: MemoryPolicy | None, tenant: str, workflow_name: str, base_system_prompt: str
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
    `recipient_id` is surfaced in decide_and_notify()'s user message (outside
    this step's declared prompt -- it's request context, not part of
    input_schema) so the model has something to build the tool call's
    `scope` from."""
    scope = (workflow_name, "notified")
    return await inject_procedural(store, memory_policy, tenant=tenant, scope=scope, base_prompt=base_system_prompt, limit=_PROCEDURAL_LIMIT)


@wrap_agent_exception("notified")
async def decide_and_notify(
    gateway: MCPGateway,
    *,
    should_notify: bool,
    subject: str,
    body: str,
    recipient_id: str = "default",
    model: str | None = None,
    system_prompt: str | None = None,
    user_prompt: str | None = None,
    store: Any | None = None,
    memory_policy: MemoryPolicy | None = None,
    tenant: str = "default",
    workflow_name: str = _DEFAULT_WORKFLOW_NAME,
) -> list[str]:
    # `should_notify` is an upstream business decision, not a suggestion for
    # the model to reconsider. Enforce the negative branch before exposing
    # any side-effecting tools: small/local models may ignore a prompt-only
    # instruction and send anyway.
    if not should_notify:
        return []

    # system_prompt/user_prompt/model come from the caller's workflow spec
    # (agents/runtime.py renders workflows/definitions/*.yaml's `notified`
    # step prompt/model via orchestrator.workflow_def.render_prompt()/
    # resolve_model()). Callers with no spec in hand (mcp_servers/notified/agent.py's
    # compat shim for the frozen workflows/simple_pipeline.py, see
    # workflows/parity_check.py) fall back to `workflow_name`'s own declared
    # prompt/model, so every path runs under the same content
    # (docs/generic-agent-runtime-plan.md P1/P5).
    system_prompt, user_prompt = resolve_prompt(
        workflow_name,
        "notified",
        {"should_notify": should_notify, "subject": subject, "body": body},
        system_prompt=system_prompt,
        user_prompt=user_prompt,
    )
    model = resolve_model(workflow_name, "notified", model=model)

    rendered_system_prompt, tools = await asyncio.gather(
        _recall_prompt(store, memory_policy, tenant, workflow_name, system_prompt),
        gateway.list_openai_tools(),
    )
    messages: list[dict] = [
        {"role": "system", "content": rendered_system_prompt},
        {"role": "user", "content": f"收件人 id: {recipient_id}\n{user_prompt}"},
    ]
    # docs/generic-agent-runtime-plan.md P2's `from: tool_log` declaration.
    log = ToolCallLog()

    # A "no tool call" turn always finishes the loop below, so the only way
    # this loop can get stuck is retrying the same failed send -- exactly
    # the pattern the system prompt already asks the model not to do; this
    # enforces it structurally instead of trusting the instruction alone.
    stall_guard = StallGuard(consecutive_limit=2)
    assistant_message = await run_tool_calling_loop(
        model,
        messages,
        tools,
        gateway,
        node="notified",
        max_turns=MAX_TURNS,
        stall_guard=stall_guard,
        on_tool_result=log.observe,
        raise_on_max_turns=False,
    )
    if assistant_message is None:
        log.entries.append("(stopped: reached max tool-calling turns)")
    else:
        log.entries.append(assistant_message.content or "(model decided: no notification needed)")
    return _finish(should_notify, log.any_succeeded(*_NOTIFY_TOOL_SUFFIXES), log.entries)
