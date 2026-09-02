"""Agentic insurance-exclusion judge (docs/exclusion-scenario-plan.md P4).

Same tool-calling loop shape as llm/tsmc_judge.py (StallGuard,
AgentLoopIncomplete, @wrap_agent_exception), but three deliberate
differences -- see docs/exclusion-scenario-plan.md P4 for the full
rationale, summarized here:

1. No policy text is ever injected into the prompt. The system prompt only
   tells the model the policy document lives in memory and how to
   `browse_semantic_memory` into it -- the whole point of this module is
   proving an agent can answer without the platform ever handing it the
   full document.
2. No deterministic backstop (there's no string to alias-match for "does
   this involve an exclusion"). Instead, a citation check: every article
   the model cites in its final answer must be one it actually read via a
   browse_semantic_memory tool result during this same loop -- derived from
   `explored`, the platform-level accumulated browse map
   (persistence/memory_prompt.py's track_browse_result()/render_explored_map(),
   collected from tool *results*, never from what the model merely claims).
   A citation that doesn't check out gets one retry turn with the mismatch
   spelled out; still wrong -> AgentLoopIncomplete.
3. This step's current declared `model` lives in workflows/definitions/
   stt_exclusion_notify.yaml (docs/generic-agent-runtime-plan.md P5), not a
   module constant. It is `gemini-cheap` as of the 2026-08-31 Windows-local
   workflow update -- this loop needs several consecutive tool-calling
   turns plus mid-task direction changes (backtracking to a sibling branch),
   which is a heavier ask than llm/tsmc_judge.py's single-shot
   classification. Worth revisiting once this scenario is stable, per the
   module's own reason for existing: probing how much multi-hop tool use a
   small/cheap model can sustain.

   History: was gemini-cheap until 2026-08-11 (TODO.md's exclusion-judge-model-choice),
   then gemini-strong, then claude-haiku on 2026-08-17, before the current
   Windows workflow moved the declared step back to gemini-cheap. Repeated
   sampling on evals/check_cases.yaml's
   drunk_driving_bike (the deliberately-tricky multi-hop case) measured 0/5
   on gemini-cheap vs 15/15 on gemini-strong, and gemini-cheap separately
   showed degraded format/reasoning discipline the moment *any* episodic
   few-shot content entered the prompt (that injection path was later
   removed by P5). Run `evals/run_eval.py --repeats 3` before trusting a
   model change for this step; the gemini-strong-vs-gemini-cheap gap is
   evidence this judge is genuinely sensitive to model choice, not a
   generic "any model works" task.
"""

from __future__ import annotations

import asyncio
from typing import Any

from harness.agent_loop import (
    AgentLoopIncomplete,
    StallGuard,
    parse_structured_json,
    run_tool_calling_loop,
    wrap_agent_exception,
)
from mcp_servers.gateway import MCPGateway
from orchestrator.workflow_def import resolve_model, resolve_prompt
from persistence.memory_policy import MemoryPolicy
from persistence.memory_prompt import inject_procedural, render_explored_map, track_browse_result

MAX_TURNS = 20
_RETRY_MAX_TURNS = 5
"""Bound for the citation-conflict retry loop below -- smaller than
MAX_TURNS since this is the model re-checking one already-explored
citation, not exploring the tree from scratch."""
_BROWSE_TOOL = "memory__browse_semantic_memory"

# docs/generic-agent-runtime-plan.md P2's `from: model` declaration -- same
# rationale as llm/tsmc_judge.py's _VERDICT_SCHEMA.
_VERDICT_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "exclusion_verdict",
        "schema": {
            "type": "object",
            "properties": {
                "involves_exclusion": {"type": "boolean"},
                "matched_articles": {"type": "array", "items": {"type": "string"}},
                "reason": {"type": "string"},
            },
            "required": ["involves_exclusion", "matched_articles", "reason"],
            "additionalProperties": False,
        },
        "strict": True,
    },
}

# (workflow_name, step_name) scope for this step's procedural/episodic memory
# (docs/long-term-memory-plan.md §3.2). Deliberately NOT the same
# workflow_name llm/tsmc_judge.py uses ("stt_check_notify") even though both
# are a `check` step -- they're not "the same judgment task reused across
# workflows" (the case mcp_servers/policy.yaml's wildcarded workflow segment
# was written for), they're two different scenarios that happen to share a
# step name. Keeping the workflow segment distinct keeps their episodic
# few-shots and procedural rules from mixing -- the grant pattern still
# covers this scope (it wildcards that segment), but the actual namespace
# string differs, and recall() matches namespaces exactly.
_MEMORY_SCOPE = ("stt_exclusion_notify", "check")
_PROCEDURAL_LIMIT = 10


def _citation_conflict_prompt(unverified: list[str]) -> str:
    # An f-string only for the first line (the one interpolated part);
    # the rest is plain string literals adjacent to it, not `.format()` --
    # the JSON example below contains its own literal `{...}`, which
    # `str.format()` would misparse as a field reference and crash on.
    return (
        f"系統查核：你最終答案引用的條文 {unverified} 並不在你剛才透過 browse_semantic_memory 實際讀到過的"
        "內容裡。請重新確認——如果這條真的相關，請先用工具查到它的全文再引用；如果查不到或想不起來是"
        "哪條，就把它從 matched_articles 移除。只回覆最終判斷，格式固定為 "
        '{"involves_exclusion": true 或 false, "matched_articles": [...], "reason": "..."}。'
    )


def _seen_articles(explored: dict[tuple[str, ...], dict]) -> set[str]:
    """Every `article` field across everything accumulated in `explored` --
    the only source citation verification ever draws from (tool results,
    never the model's own claims). Derived on demand from the same map
    render_explored_map() shows the model, rather than kept as a second,
    separately-maintained set -- one accumulator, not two copies of the same
    fact drifting apart."""
    return {
        item["article"]
        for node in explored.values()
        for item in (node.get("items") or [])
        if item.get("article")
    }


def _parse_verdict(content: str) -> dict:
    # _VERDICT_SCHEMA (passed as response_format on every run_tool_calling_loop
    # call below) guarantees `content` is this JSON object, but not always
    # bare -- parse_structured_json() strips an occasional markdown fence
    # around it (harness.agent_loop.parse_structured_json's docstring: seen
    # under concurrent load even with strict:True). The str()/bool()
    # coercions stay as cheap insurance against a provider not perfectly
    # honoring `strict` in other ways.
    data = parse_structured_json(content)
    return {
        "involves_exclusion": bool(data["involves_exclusion"]),
        "matched_articles": [str(a) for a in (data.get("matched_articles") or [])],
        "reason": str(data.get("reason") or ""),
    }


@wrap_agent_exception("check")
async def judge_exclusion(
    gateway: MCPGateway,
    transcript: str,
    *,
    model: str | None = None,
    system_prompt: str | None = None,
    user_prompt: str | None = None,
    store: Any | None = None,
    memory_policy: MemoryPolicy | None = None,
    tenant: str = "default",
) -> dict:
    # system_prompt/user_prompt/model come from the caller's workflow spec
    # (agents/runtime.py renders workflows/definitions/stt_exclusion_notify.yaml's
    # `check` step prompt/model via orchestrator.workflow_def.render_prompt()/
    # resolve_model()). Callers with no spec in hand (evals/run_eval.py's own
    # --model override, llm/exclusion_judge_smoke_test.py) fall back to
    # _MEMORY_SCOPE's own workflow's declared prompt/model, so every path
    # runs under the same content (docs/generic-agent-runtime-plan.md P1/P5).
    system_prompt, user_prompt = resolve_prompt(
        *_MEMORY_SCOPE, {"transcript": transcript}, system_prompt=system_prompt, user_prompt=user_prompt
    )
    model = resolve_model(*_MEMORY_SCOPE, model=model)

    rendered_system_prompt, all_tools = await asyncio.gather(
        inject_procedural(store, memory_policy, tenant=tenant, scope=_MEMORY_SCOPE, base_prompt=system_prompt, limit=_PROCEDURAL_LIMIT),
        gateway.list_openai_tools(),
    )
    # Only offer the browse tool -- `check` also has lookup__* (TSMC
    # scenario leftover, irrelevant here) via the `reader` role; narrowing
    # the tool list avoids tempting the model into an unrelated call, same
    # reasoning llm/tsmc_judge.py uses to remove the lookup tool once its
    # deterministic backstop already covers it.
    tools = [t for t in all_tools if t["function"]["name"] == _BROWSE_TOOL]
    messages: list[dict] = [
        {"role": "system", "content": rendered_system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    explored: dict[tuple[str, ...], dict] = {}
    stall_guard = StallGuard(consecutive_limit=2)

    def _on_tool_result(call: Any, arguments: dict, result_text: str, is_error: bool) -> None:
        if not is_error and call.function.name == _BROWSE_TOOL:
            track_browse_result(explored, result_text)

    def _on_turn_end() -> None:
        # Injected after every tool_call in this turn already has its
        # tool_result appended (docs/harness-engineering-principles.md §4's
        # legal injection point) -- a reliable "what have I already seen"
        # view, so the model doesn't have to reconstruct it by re-reading
        # scattered raw browse() results itself. Doesn't suggest where to go
        # next; see persistence/memory_prompt.py::render_explored_map()'s
        # docstring for why that line matters.
        map_text = render_explored_map(explored)
        if map_text:
            messages.append({"role": "user", "content": map_text})

    assistant_message = await run_tool_calling_loop(
        model,
        messages,
        tools,
        gateway,
        node="check",
        max_turns=MAX_TURNS,
        stall_guard=stall_guard,
        on_tool_result=_on_tool_result,
        on_turn_end=_on_turn_end,
        response_format=_VERDICT_SCHEMA,
    )
    verdict = _parse_verdict(assistant_message.content)

    unverified = [a for a in verdict["matched_articles"] if a not in _seen_articles(explored)]
    if unverified:
        messages.append({"role": "assistant", "content": assistant_message.content})
        messages.append({"role": "user", "content": _citation_conflict_prompt(unverified)})
        # A real loop, not one bare chat call -- if the model does what
        # the retry prompt asks (re-browse before re-citing), that
        # tool call must actually run and update `explored`, or this retry
        # can never succeed.
        retry_message = await run_tool_calling_loop(
            model,
            messages,
            tools,
            gateway,
            node="check",
            max_turns=_RETRY_MAX_TURNS,
            on_tool_result=_on_tool_result,
            on_turn_end=_on_turn_end,
            response_format=_VERDICT_SCHEMA,
        )
        verdict = _parse_verdict(retry_message.content)
        unverified = [a for a in verdict["matched_articles"] if a not in _seen_articles(explored)]
        if unverified:
            raise AgentLoopIncomplete(
                node="check",
                reason=(
                    f"model cited article(s) it never actually browsed via {_BROWSE_TOOL}: "
                    f"{unverified!r} (text={transcript!r})"
                ),
            )

    return verdict
