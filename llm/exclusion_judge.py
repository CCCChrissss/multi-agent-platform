"""Agentic insurance-exclusion judge (docs/exclusion-scenario-plan.md P4).

Same tool-calling loop shape as llm/tsmc_judge.py (StallGuard,
AgentLoopIncomplete, @wrap_agent_exception), but three deliberate
differences -- see docs/exclusion-scenario-plan.md P4 for the full
rationale, summarized here:

1. The whole policy is never injected into the prompt. The platform performs
   two bounded semantic recalls under the selected product root (exclusion
   cause and benefit/disability threshold) plus one root browse; the model
   can still use `browse_semantic_memory` for missing branches. This keeps
   evidence small enough for the local model while proving that answers come
   from the memory store rather than bundled policy text.
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
   module constant. It is `local-qwen3`; gateway/config.yaml fixes its
   context/output bounds for the Windows-local workflow. The deterministic,
   bounded recall above exists because this 4B model was not reliable at
   planning several consecutive browse hops on its own.

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
import json
import uuid
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
from persistence.memory import GLOBAL_TENANT, MemoryKind, recall
from persistence.memory_policy import MemoryPolicy
from persistence.memory_prompt import inject_procedural, render_explored_map, track_browse_result

MAX_TURNS = 20
_RETRY_MAX_TURNS = 5
"""Bound for the citation-conflict retry loop below -- smaller than
MAX_TURNS since this is the model re-checking one already-explored
citation, not exploring the tree from scratch."""
_BROWSE_TOOL = "memory__browse_semantic_memory"
_POLICY_ROOT_SCOPE = ["insurance_product", "kgi_ltc"]
_EVIDENCE_LIMIT = 6

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


async def _recall_policy_evidence(
    store: Any | None,
    memory_policy: MemoryPolicy | None,
    transcript: str,
) -> list[dict[str, Any]]:
    """Retrieve a small, evidence-focused slice across the policy tree.

    The local 4B model is good at judging compact evidence but is not reliable
    at planning several browse hops. Two bounded semantic searches cover the
    two independent questions the workflow requires: exclusion cause and
    benefit/disability threshold. Policy checks and audit logging still run
    inside persistence.memory.recall().
    """
    if store is None or memory_policy is None:
        return []

    queries = (
        f"{transcript}\n保單除外責任：飲酒、酒駕、犯罪、故意、毒品、戰爭、核污染或競賽是否適用",
        f"{transcript}\n保險金給付條件、失能等級、受影響身體部位與門檻",
    )
    batches = await asyncio.gather(
        *(
            recall(
                store,
                memory_policy,
                MemoryKind.SEMANTIC,
                tenant=GLOBAL_TENANT,
                scope=tuple(_POLICY_ROOT_SCOPE),
                query=query,
                limit=_EVIDENCE_LIMIT,
            )
            for query in queries
        )
    )

    evidence: list[dict[str, Any]] = []
    seen: set[tuple[tuple[str, ...], str]] = set()
    for item in (item for batch in batches for item in batch):
        content = item.value.get("content") or {}
        article = content.get("article")
        identity = (tuple(item.namespace), item.key)
        if not article or identity in seen:
            continue
        seen.add(identity)
        evidence.append(
            {
                "scope": list(item.namespace[2:]),
                "key": item.key,
                "article": article,
                "title": content.get("title"),
                "text": content.get("text"),
                "applies_to": content.get("applies_to") or [],
            }
        )
    return evidence


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

    rendered_system_prompt, all_tools, recalled_evidence = await asyncio.gather(
        inject_procedural(store, memory_policy, tenant=tenant, scope=_MEMORY_SCOPE, base_prompt=system_prompt, limit=_PROCEDURAL_LIMIT),
        gateway.list_openai_tools(),
        _recall_policy_evidence(store, memory_policy, transcript),
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
    if recalled_evidence:
        messages.append(
            {
                "role": "user",
                "content": (
                    "系統已用逐字稿在指定保單根目錄做兩路 semantic recall。以下每筆都是記憶模組實際命中的"
                    "條文全文，可直接作為引用證據；如仍缺少必要資訊，再使用 browse_semantic_memory 補查。\n"
                    + json.dumps(recalled_evidence, ensure_ascii=False)
                ),
            }
        )
    recalled_articles = {item["article"] for item in recalled_evidence}

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

    # The policy root is a required input to this judgment, not an optional
    # convenience tool.  Small local models can occasionally answer directly
    # despite the prompt telling them to browse first.  Execute the first
    # browse deterministically through the same gateway used by model tool
    # calls. Present it as explicitly labelled platform context: fabricating
    # an assistant-authored tool call here confuses Ollama/Qwen's tool-call
    # template because the model did not actually produce that message.
    initial_call_id = f"required-browse-{uuid.uuid4().hex}"
    initial_arguments = {"scope": _POLICY_ROOT_SCOPE, "thread_id": None}
    initial_result, initial_is_error = await gateway.call_tool(
        _BROWSE_TOOL, initial_arguments, initial_call_id
    )
    if initial_is_error:
        raise AgentLoopIncomplete(
            node="check",
            reason=f"required policy-root browse failed: {initial_result}",
        )
    track_browse_result(explored, initial_result)
    messages.append(
        {
            "role": "user",
            "content": (
                "系統已先透過 browse_semantic_memory 讀取指定保單根節點；以下是未修改的工具結果。"
                "請依 children 選擇相關分支繼續呼叫工具，不要只根據分支摘要引用條文。\n"
                f"{initial_result}"
            ),
        }
    )
    _on_turn_end()

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

    verified_articles = _seen_articles(explored) | recalled_articles
    unverified = [a for a in verdict["matched_articles"] if a not in verified_articles]
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
        verified_articles = _seen_articles(explored) | recalled_articles
        unverified = [a for a in verdict["matched_articles"] if a not in verified_articles]
        if unverified:
            raise AgentLoopIncomplete(
                node="check",
                reason=(
                    f"model cited article(s) it never actually browsed via {_BROWSE_TOOL}: "
                    f"{unverified!r} (text={transcript!r})"
                ),
            )

    return verdict
