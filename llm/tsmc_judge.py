"""Agentic TSMC-mention judge.

Same shape as llm/notify_agent.py's decide_and_notify: hand the LLM a tool
list, let it call query_company_profile if it wants grounding before
answering, feed results back, and treat its first non-tool-call reply as
the final verdict.

A deterministic alias backstop runs before the model loop (see
_lookup_tsmc_aliases/_alias_hit) so the model no longer needs to decide
whether to call lookup__query_company_profile itself -- the tool is
removed from its tool list. When the backstop finds a known alias in the
text but the model says false (the dangerous, under-reporting direction),
the model gets one more turn with that fact spelled out before this
escalates to AgentLoopIncomplete. The opposite direction (backstop misses,
model says true) is never overridden -- alias matching only proves
presence, never absence.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from harness.agent_loop import AgentLoopIncomplete, StallGuard, run_tool_calling_loop, wrap_agent_exception
from mcp_servers.gateway import MCPGateway
from persistence.memory import GLOBAL_TENANT, MemoryKind, recall
from persistence.memory_policy import MemoryPolicy
from persistence.memory_prompt import inject_procedural

MODEL_NAME = "local-qwen"
MAX_TURNS = 20
_RETRY_MAX_TURNS = 5
_TSMC_QUERY = "台積電"
_LOOKUP_TOOL = "lookup__query_company_profile"

# (workflow_name, step_name) scope for this step's episodic/procedural memory
# (docs/long-term-memory-plan.md §3.2). Hardcoded to this demo workflow's name
# rather than threaded through as a parameter -- mcp_servers/policy.yaml's
# `memory:` grants for `check` wildcard the workflow segment specifically so
# this same scope tuple keeps working if another workflow reuses this step.
_MEMORY_SCOPE = ("stt_check_notify", "check")
_PROCEDURAL_LIMIT = 10

# Company aliases are semantic memory, not tenant-specific (docs/long-term-memory-plan.md
# §3.2/§3.6) -- always read under GLOBAL_TENANT, matching
# mcp_servers/policy.yaml's `_global/semantic/company/*` grant for `check`.
_ALIAS_SCOPE = ("company", "tsmc")
_ALIAS_LIMIT = 5

_SYSTEM_PROMPT = (
    "你是一個文字分類助手。判斷使用者提供的文字內容是否提到台積電"
    "（包含別名，例如 TSMC、台灣積體電路製造）。"
    '直接用純文字回答，格式固定為 {"mentions_tsmc": true} 或 {"mentions_tsmc": false}，不要有其他文字。'
)

_CONFLICT_PROMPT = (
    "系統查核（確定性字串比對，非模型判斷）：文字中包含台積電的已知別名字串，"
    "但你剛才的回答是 false。請重新檢視文字脈絡：這段文字裡的字串是否真的在指台積電"
    "這間公司（而非巧合、範例列舉或其他無關用法）？"
    '只回覆最終判斷，格式固定為 {"mentions_tsmc": true} 或 {"mentions_tsmc": false}。'
)


async def _lookup_tsmc_aliases(
    gateway: MCPGateway, store: Any | None, memory_policy: MemoryPolicy | None
) -> list[str] | None:
    """Deterministic ground-truth fetch, not model-invoked. Merges the static
    mcp_servers/lookup profile with recalled semantic memory -- company
    aliases are this demo's semantic memory type (docs/long-term-memory-plan.md
    §3.6); the static lookup data is what semantic memory is meant to
    eventually augment, since a human-corrected alias otherwise has nowhere
    to persist. None only when *both* sources come up empty -- the backstop
    degrades to "no opinion" for this call rather than blocking or biasing
    the judge."""
    result_text, is_error = await gateway.call_tool(_LOOKUP_TOOL, {"company": _TSMC_QUERY})
    static_aliases: list[str] = []
    if not is_error:
        profile = json.loads(result_text)
        static_aliases = [name for name in [profile.get("official_name"), *profile.get("aliases", [])] if name]

    recalled_aliases: list[str] = []
    if store is not None and memory_policy is not None:
        items = await recall(
            store, memory_policy, MemoryKind.SEMANTIC, tenant=GLOBAL_TENANT, scope=_ALIAS_SCOPE, limit=_ALIAS_LIMIT
        )
        for item in items:
            recalled_aliases.extend(item.value["content"].get("aliases", []))

    if is_error and not recalled_aliases:
        return None
    return list(dict.fromkeys(static_aliases + recalled_aliases))  # dedupe, preserve first-seen order


def _alias_hit(text: str, aliases: list[str]) -> bool:
    haystack = text.lower()
    return any(alias.lower() in haystack for alias in aliases)


def _parse_verdict(content: str) -> bool:
    return bool(json.loads(content)["mentions_tsmc"])


@wrap_agent_exception("check")
async def mentions_tsmc(
    gateway: MCPGateway,
    text: str,
    *,
    store: Any | None = None,
    memory_policy: MemoryPolicy | None = None,
    tenant: str = "default",
) -> bool:
    # None of these three depend on each other's result -- run them concurrently
    # instead of stacking three DB/MCP round trips on the request hot path.
    aliases, system_prompt, all_tools = await asyncio.gather(
        _lookup_tsmc_aliases(gateway, store, memory_policy),
        inject_procedural(store, memory_policy, tenant=tenant, scope=_MEMORY_SCOPE, base_prompt=_SYSTEM_PROMPT, limit=_PROCEDURAL_LIMIT),
        gateway.list_openai_tools(),
    )
    backstop_hit = _alias_hit(text, aliases) if aliases is not None else None
    # Strip lookup__* (covered deterministically by the alias backstop
    # above) and memory__* (docs/exclusion-scenario-plan.md P2/P4 granted it
    # to the `check` step for the *exclusion* scenario's browse loop --
    # mcp_servers/policy.yaml scopes grants per step name, not per workflow,
    # so it also reaches this loop even though this judgment never reads the
    # policy tree). Narrowing the tool list avoids tempting the model into
    # an unrelated call either way.
    tools = [
        t
        for t in all_tools
        if t["function"]["name"] != _LOOKUP_TOOL and not t["function"]["name"].startswith("memory__")
    ]
    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": text},
    ]

    # Only the safety-net branch below can loop without reaching a verdict
    # (repeatedly hallucinating a call to the removed lookup tool); catch
    # that stuck pattern well before MAX_TURNS burns out.
    stall_guard = StallGuard(consecutive_limit=2)
    # Safety net only -- lookup__query_company_profile is no longer offered
    # (the backstop above already queries it deterministically), so a tool
    # call here should be unreachable in practice. run_tool_calling_loop
    # still executes one if the model hallucinates it; gateway.call_tool
    # fails closed on it.
    assistant_message = await run_tool_calling_loop(
        MODEL_NAME, messages, tools, gateway, node="check", max_turns=MAX_TURNS, stall_guard=stall_guard
    )
    model_result = _parse_verdict(assistant_message.content)

    if backstop_hit is True and model_result is False:
        messages.append({"role": "assistant", "content": assistant_message.content})
        messages.append({"role": "user", "content": _CONFLICT_PROMPT})
        retry_message = await run_tool_calling_loop(
            MODEL_NAME, messages, tools, gateway, node="check", max_turns=_RETRY_MAX_TURNS
        )
        model_result = _parse_verdict(retry_message.content)
        if model_result is False:
            raise AgentLoopIncomplete(
                node="check",
                reason=(
                    "deterministic alias match found in text but model still judged "
                    f"mentions_tsmc=false after being shown the conflicting evidence (text={text!r})"
                ),
            )

    return model_result
