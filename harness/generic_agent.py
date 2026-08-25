"""Generic tool-calling agent runtime, driven entirely by a step's spec
(docs/generic-agent-runtime-plan.md P6) -- the mechanical shell every "thin"
agent needs (prompt in, tools out, output extracted per `output.from`) with
no per-agent llm/xxx_agent.py at all.

Only fits a step with no §8-style verifier: a step that needs a citation
check, a deterministic backstop, or any other post-hoc verification/retry
loop still needs its own llm/xxx_agent.py (llm/tsmc_judge.py,
llm/exclusion_judge.py keep theirs) -- this module replaces the mechanical
shell those two still hand-roll on top of, not the verification logic
itself. agents/runtime.py decides per step whether it needs one of those
escape hatches or can run through here.
"""

from __future__ import annotations

import asyncio
from typing import Any

from harness.agent_loop import AgentLoopIncomplete, StallGuard, parse_structured_json, run_tool_calling_loop
from harness.output_capture import ToolCallLog, ToolResultCapture
from mcp_servers.gateway import MCPGateway
from orchestrator.workflow_def import StepDef, render_prompt
from persistence.memory_policy import MemoryPolicy
from persistence.memory_prompt import inject_procedural

MAX_TURNS = 20
_PROCEDURAL_LIMIT = 10


def build_response_format(step_name: str, output_schema: dict[str, Any], output: dict[str, dict[str, Any]]) -> dict | None:
    """The response_format json_schema (P2's `from: model`) covering every
    field this step declares `from: model` -- generated straight from
    output_schema instead of a hand-rolled _VERDICT_SCHEMA constant like
    llm/tsmc_judge.py/llm/exclusion_judge.py each still carry, closing the
    gap P2's own landed-results note flagged. None if no field uses
    `from: model`."""
    model_fields = [f for f, entry in output.items() if entry.get("from") == "model"]
    if not model_fields:
        return None
    properties = {f: output_schema["properties"][f] for f in model_fields}
    return {
        "type": "json_schema",
        "json_schema": {
            "name": f"{step_name}_output",
            "schema": {
                "type": "object",
                "properties": properties,
                "required": model_fields,
                "additionalProperties": False,
            },
            "strict": True,
        },
    }


async def run_generic_step(
    step: StepDef,
    gateway: MCPGateway,
    input: dict[str, Any],
    *,
    workflow_name: str,
    store: Any | None = None,
    memory_policy: MemoryPolicy | None = None,
    tenant: str = "default",
) -> dict[str, Any]:
    """Runs `step` purely off its own spec: prompt (P1), model (P5), tools
    (whatever MCPGateway's policy already grants this step's principal),
    output (P6). `scope=(workflow_name, step.name)` for procedural-memory
    injection matches every existing llm/*.py agent's convention -- a brand
    new step with no mcp_servers/policy.yaml `memory:` grant yet just gets
    the no-op fallback inject_procedural() already gives every caller.

    A `from: "tool:X"` field left uncaptured when the model stops calling
    tools gets one generic, field-name-only nudge retry (no scenario-specific
    wording the way llm/stt_agent.py's old hand-rolled loop could afford --
    a generic runner only knows the field name, not what it means) before
    raising AgentLoopIncomplete. needs_review is still the fallback once that
    retry is spent.
    """
    try:
        return await _run_generic_step(
            step, gateway, input, workflow_name=workflow_name, store=store, memory_policy=memory_policy, tenant=tenant
        )
    except AgentLoopIncomplete:
        raise
    except Exception as exc:
        raise AgentLoopIncomplete(node=step.name, reason=f"unexpected error: {exc!r}") from exc


async def _run_generic_step(
    step: StepDef,
    gateway: MCPGateway,
    input: dict[str, Any],
    *,
    workflow_name: str,
    store: Any | None,
    memory_policy: MemoryPolicy | None,
    tenant: str,
) -> dict[str, Any]:
    system_prompt, user_prompt = render_prompt(step, input)
    scope = (workflow_name, step.name)
    rendered_system_prompt, tools = await asyncio.gather(
        inject_procedural(store, memory_policy, tenant=tenant, scope=scope, base_prompt=system_prompt, limit=_PROCEDURAL_LIMIT),
        gateway.list_openai_tools(),
    )
    messages: list[dict] = [
        {"role": "system", "content": rendered_system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    captures = {
        field_name: ToolResultCapture(entry["from"].removeprefix("tool:"))
        for field_name, entry in step.output.items()
        if entry["from"].startswith("tool:")
    }
    log = ToolCallLog() if any(entry["from"] == "tool_log" for entry in step.output.values()) else None

    def _on_tool_result(call: Any, arguments: dict, result_text: str, is_error: bool) -> None:
        for capture in captures.values():
            capture.observe(call, arguments, result_text, is_error)
        if log is not None:
            log.observe(call, arguments, result_text, is_error)

    response_format = build_response_format(step.name, step.output_schema, step.output)
    stall_guard = StallGuard(consecutive_limit=2)
    on_tool_result = _on_tool_result if (captures or log is not None) else None

    # First pass, plus one generic nudge retry if a `tool:X` field is still
    # uncaptured when the model stops calling tools -- see the module
    # docstring above.
    for attempt in range(2):
        assistant_message = await run_tool_calling_loop(
            step.model,
            messages,
            tools,
            gateway,
            node=step.name,
            max_turns=MAX_TURNS,
            stall_guard=stall_guard,
            on_tool_result=on_tool_result,
            response_format=response_format,
        )
        missing = [field_name for field_name, capture in captures.items() if capture.value is None]
        if not missing or attempt == 1:
            break
        messages.append(
            {
                "role": "user",
                "content": f"尚未取得欄位 {', '.join(missing)}，請呼叫對應工具完成後再回覆。",
            }
        )

    model_fields = parse_structured_json(assistant_message.content) if response_format else {}
    result: dict[str, Any] = {}
    for field_name, entry in step.output.items():
        src = entry["from"]
        if src == "model":
            result[field_name] = model_fields[field_name]
        elif src == "tool_log":
            result[field_name] = log.entries if log else []
        else:  # "tool:<suffix>"
            if captures[field_name].value is None:
                raise AgentLoopIncomplete(
                    node=step.name,
                    reason=f"model stopped calling tools before field {field_name!r} (from: {src!r}) was captured",
                )
            result[field_name] = captures[field_name].value
    return result
