"""Manual smoke test for harness/generic_agent.py -- run with:
    uv run python -m harness.generic_agent_smoke_test

Requires the local stack up (Ollama serving qwen2.5:3b, LiteLLM Gateway on
:4000) -- this scenario makes a real model call, unlike most other
*_smoke_test.py files in this repo.

The one scenario, `new_agent_from_spec`, is the actual acceptance test for
docs/generic-agent-runtime-plan.md P6's promise: a StepDef that never
existed in any workflows/definitions/*.yaml before this test ran, built with
nothing but a model name, a prompt, and an `output: {..., from: model}`
declaration, executes correctly through run_generic_step() alone -- no
llm/xxx_agent.py, no agents/runtime.py route, no mcp_servers/policy.yaml
edit (an ad-hoc Policy plays that role here, same convention
persistence/memory_smoke_test.py's _BROWSE_POLICY already uses, so this
doesn't need to touch the real policy.yaml just to exercise a test fixture).
The StepDef is built directly in Python rather than round-tripped through a
temp YAML file + load_workflow_def() -- same convention
orchestrator/smoke_test.py's scenario_mapping_* scenarios already use;
load_workflow_def()'s own YAML-parsing/validation is already covered by
orchestrator/smoke_test.py and workflows/parity_check.py.
"""

from __future__ import annotations

import asyncio
import json
import types
from unittest.mock import AsyncMock, patch

from harness.generic_agent import run_generic_step
from harness.schema_validation import validate_against_schema
from mcp_servers.gateway import MCPGateway
from mcp_servers.policy import Grant, Policy, ServerSpec
from orchestrator.workflow_def import PromptDef, StepDef

_PRINCIPAL = "smoke_new_agent"
_POLICY = Policy(
    servers={"lookup": ServerSpec(module="mcp_servers.lookup.server")},
    principals={_PRINCIPAL: Grant(allow=frozenset({"lookup__query_company_profile"}))},
)
"""Only the one server this scenario's tool call needs -- MCPGateway spawns
one stdio subprocess per declared server, so a full mcp_servers/policy.yaml
would spawn four more this test never uses."""

_STEP = StepDef(
    name=_PRINCIPAL,
    command_type="smoke.new_agent.run",
    completion_type="smoke.new_agent.completed",
    input_schema={
        "type": "object",
        "required": ["company"],
        "properties": {"company": {"type": "string"}},
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "required": ["official_name"],
        "properties": {"official_name": {"type": "string"}},
        "additionalProperties": False,
    },
    model="local-qwen",
    prompt=PromptDef(
        system=(
            "你可以呼叫 lookup__query_company_profile 查詢公司的正式名稱。"
            '查完之後只回覆 JSON，格式固定為 {"official_name": "..."}，'
            "填入查到的正式名稱；查不到就照原樣填入使用者提供的名字。"
        ),
        user="{{ input.company }}",
    ),
    output={"official_name": {"from": "model"}},
)


async def scenario_new_agent_from_spec() -> None:
    async with MCPGateway(_POLICY, principal=_PRINCIPAL) as gateway:
        result = await run_generic_step(_STEP, gateway, {"company": "台積電"}, workflow_name="smoke_new_agent")
    validate_against_schema(_PRINCIPAL, "output", result, _STEP.output_schema)
    assert result["official_name"], result
    assert "台積電" in result["official_name"] or "台灣積體電路" in result["official_name"], result
    print(f"[new_agent_from_spec] OK -- brand-new spec-only agent produced {result!r} with zero llm/*.py or agents/runtime.py code")


def _message(*, content: str | None = None, tool_calls=None) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=content, tool_calls=tool_calls))]
    )


def _tool_call(call_id: str, name: str, arguments: dict) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        type="function", id=call_id, function=types.SimpleNamespace(name=name, arguments=json.dumps(arguments))
    )


_NUDGE_STEP = StepDef(
    name="smoke_nudge_retry",
    command_type="smoke.nudge_retry.run",
    completion_type="smoke.nudge_retry.completed",
    input_schema={"type": "object", "required": [], "properties": {}, "additionalProperties": False},
    output_schema={"type": "object", "required": ["profile"], "properties": {"profile": {"type": "string"}}, "additionalProperties": False},
    model="local-qwen",
    prompt=PromptDef(system="system", user="user"),
    output={"profile": {"from": "tool:query_company_profile"}},
)


async def scenario_nudge_retry_recovers_stopped_model() -> None:
    """A model that stops without calling the required tool on its first turn
    (no `from: "tool:X"` capture yet) gets one generic nudge -- covers the
    regression harness/generic_agent.py's module docstring calls out: this
    used to raise AgentLoopIncomplete immediately with no retry."""
    gateway = AsyncMock()
    gateway.list_openai_tools = AsyncMock(return_value=[{"function": {"name": "lookup__query_company_profile"}}])

    async def fake_call_tool(name, arguments, call_id):
        assert name == "lookup__query_company_profile"
        return "台積電", False

    gateway.call_tool = fake_call_tool

    responses = [
        # First turn: stops with plain text, no tool call -- the exact
        # failure shape that used to raise immediately.
        _message(content="好的，我準備好了。"),
        _message(tool_calls=[_tool_call("c1", "lookup__query_company_profile", {})]),
        _message(content='{"profile": "ok"}'),
    ]

    def fake_chat_with_tools(model, messages, tools, response_format=None):
        return responses.pop(0)

    with patch("harness.agent_loop.chat_with_tools", fake_chat_with_tools):
        result = await run_generic_step(_NUDGE_STEP, gateway, {}, workflow_name="smoke_nudge_retry")

    assert result == {"profile": "台積電"}, result
    assert not responses, "not all mocked turns were consumed"
    print("[nudge_retry_recovers] OK -- generic runner nudged the model instead of failing on the first stopped turn")


async def main() -> None:
    await scenario_new_agent_from_spec()
    await scenario_nudge_retry_recovers_stopped_model()
    print("\nAll generic_agent smoke tests passed.")


asyncio.run(main())
