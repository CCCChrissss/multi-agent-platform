"""Agentic STT node.

Same tool-calling loop shape as llm/tsmc_judge.py: hand the LLM a tool list
(format check + transcribe) and let it decide which to call. Unlike
tsmc_judge.py, the final answer isn't parsed from the model's own reply --
the transcript is captured directly off the transcribe_audio tool result as
it comes back, so a long transcript can never be paraphrased or truncated
by the model relaying it. The model's own final turn is just a "done"
status message and is otherwise ignored.
"""

from __future__ import annotations

import json
from typing import Any

from gateway.client import chat_with_tools
from harness.agent_loop import AgentLoopIncomplete, StallGuard, wrap_agent_exception
from harness.output_capture import ToolResultCapture
from mcp_servers.gateway import MCPGateway
from orchestrator.workflow_def import resolve_model, resolve_prompt
from persistence.memory_policy import MemoryPolicy
from persistence.memory_prompt import inject_procedural

# Higher than tsmc_judge.py's MAX_TURNS=4: this agent needs at least 3 turns
# for the happy path (format check, transcribe, final reply), plus slack for
# the model occasionally mis-naming the namespaced tool on the first attempt.
MAX_TURNS = 20
_TRANSCRIBE_TOOL_SUFFIX = "transcribe_audio"

# (workflow_name, step_name) scope, same convention as llm/tsmc_judge.py's
# _MEMORY_SCOPE. No policy.yaml `memory:` grant exists for `stt` yet (no
# concrete kind/scope decided -- docs/long-term-memory-plan.md M2.1/M2.2), so
# recall() fails closed to "nothing found"; wiring the calls now costs
# nothing and means a future need is a policy.yaml grant + remember() away.
_MEMORY_SCOPE = ("stt_check_notify", "stt")
_PROCEDURAL_LIMIT = 10


@wrap_agent_exception("stt")
async def transcribe(
    gateway: MCPGateway,
    audio_path: str,
    *,
    model: str | None = None,
    system_prompt: str | None = None,
    user_prompt: str | None = None,
    store: Any | None = None,
    memory_policy: MemoryPolicy | None = None,
    tenant: str = "default",
) -> str:
    # system_prompt/user_prompt/model come from the caller's workflow spec
    # (agents/runtime.py renders workflows/definitions/*.yaml's `stt` step
    # prompt/model via orchestrator.workflow_def.render_prompt()/resolve_model()).
    # Callers with no spec in hand (workflows/simple_pipeline.py, frozen -- see
    # workflows/parity_check.py) fall back to _MEMORY_SCOPE's own workflow's
    # declared prompt/model, so every path runs under the same content
    # (docs/generic-agent-runtime-plan.md P1/P5).
    system_prompt, user_prompt = resolve_prompt(
        *_MEMORY_SCOPE, {"audio_ref": audio_path}, system_prompt=system_prompt, user_prompt=user_prompt
    )
    model = resolve_model(*_MEMORY_SCOPE, model=model)

    rendered_system_prompt = await inject_procedural(
        store, memory_policy, tenant=tenant, scope=_MEMORY_SCOPE, base_prompt=system_prompt, limit=_PROCEDURAL_LIMIT
    )
    tools = await gateway.list_openai_tools()
    messages: list[dict] = [
        {"role": "system", "content": rendered_system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    # docs/generic-agent-runtime-plan.md P2's `from: tool` declaration --
    # the transcript is whatever transcribe_audio last returned, never what
    # the model says about it (see module docstring).
    capture = ToolResultCapture(_TRANSCRIBE_TOOL_SUFFIX)
    # Tool calls are the only way this loop can progress -- catches the model
    # repeating the same refusal/hesitation turn after turn (even reworded
    # each time) well before MAX_TURNS is exhausted, so a genuinely long
    # multi-tool-call transcription isn't punished by the same ceiling.
    stall_guard = StallGuard(consecutive_limit=2)

    for _ in range(MAX_TURNS):
        response = chat_with_tools(model, messages, tools)
        assistant_message = response.choices[0].message
        # we only ever register type="function" tools, so ignore any other tool-call kind
        tool_calls = [c for c in (assistant_message.tool_calls or []) if c.type == "function"]

        if not tool_calls:
            if capture.value is not None:
                return capture.value
            if stall_guard.observe("no_tool_call"):
                raise AgentLoopIncomplete(
                    node="stt",
                    reason=(
                        f"model didn't call a tool for {stall_guard.streak} turns in a row "
                        f"(last reply: {assistant_message.content!r})"
                    ),
                )
            # No tool call yet and no transcript -- the model replied with
            # plain text instead of calling a tool (e.g. just acknowledging
            # the format check). Nudge it forward instead of giving up on
            # the very first miss.
            messages.append({"role": "assistant", "content": assistant_message.content})
            messages.append({"role": "user", "content": "尚未取得逐字稿，請呼叫 transcribe_audio 完成轉錄。"})
            continue

        signature = tuple((c.function.name, c.function.arguments) for c in tool_calls)
        if stall_guard.observe(signature):
            raise AgentLoopIncomplete(
                node="stt",
                reason=(
                    f"model repeated the same tool call {stall_guard.streak} turns in a row "
                    f"without progress: {signature!r}"
                ),
            )

        messages.append(
            {
                "role": "assistant",
                "content": assistant_message.content,
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.function.name, "arguments": call.function.arguments},
                    }
                    for call in tool_calls
                ],
            }
        )

        for call in tool_calls:
            arguments = json.loads(call.function.arguments)
            result_text, is_error = await gateway.call_tool(call.function.name, arguments, call.id)
            capture.observe(call, arguments, result_text, is_error)
            content = f"[ERROR] {result_text}" if is_error else result_text
            messages.append({"role": "tool", "tool_call_id": call.id, "content": content})

    raise AgentLoopIncomplete(
        node="stt", reason=f"failed to produce a transcript for '{audio_path}' within {MAX_TURNS} turns"
    )
