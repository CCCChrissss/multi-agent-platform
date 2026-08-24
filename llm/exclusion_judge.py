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
3. MODEL_NAME is gemini-strong, not local-qwen -- this loop needs several
   consecutive tool-calling turns plus mid-task direction changes
   (backtracking to a sibling branch), which is a heavier ask than
   llm/tsmc_judge.py's single-shot classification. Worth revisiting once
   this scenario is stable, per the module's own reason for existing:
   probing how much multi-hop tool use a small local model can sustain.

   Was gemini-cheap until 2026-08-11 (TODO.md's exclusion-judge-model-choice):
   repeated sampling on evals/check_cases.yaml's drunk_driving_bike (the
   deliberately-tricky multi-hop case) measured 0/5 on gemini-cheap vs
   15/15 on gemini-strong, and gemini-cheap separately showed degraded
   format/reasoning discipline the moment *any* episodic few-shot content
   entered the prompt (TODO.md's exclusion-judge-episodic-degradation-risk)
   -- switching to gemini-strong is the user-approved interim fix for both
   findings at once, accepting gemini-strong's materially higher latency
   as the cost. Still an open item: this is an accuracy-over-cost tradeoff
   made under time pressure, not proof the two are the same root cause --
   revisit for a cheaper model once one clears the same bar.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from harness.agent_loop import AgentLoopIncomplete, StallGuard, run_tool_calling_loop, wrap_agent_exception
from mcp_servers.gateway import MCPGateway
from persistence.memory_policy import MemoryPolicy
from persistence.memory_prompt import inject_procedural, render_explored_map, track_browse_result

MODEL_NAME = "gemini-strong"
MAX_TURNS = 20
_RETRY_MAX_TURNS = 5
"""Bound for the citation-conflict retry loop below -- smaller than
MAX_TURNS since this is the model re-checking one already-explored
citation, not exploring the tree from scratch."""
_BROWSE_TOOL = "memory__browse_semantic_memory"

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

# Hardcoded to this one product rather than a browsable "which product"
# step -- this scenario only has one seeded product (data/insurance_product/
# kgi_ltc.yaml, docs/exclusion-scenario-plan.md P3). Routing across multiple
# products is a real future need but not this scenario's, so it's not built
# ahead of a second product actually existing.
_POLICY_ROOT_SCOPE = ["insurance_product", "kgi_ltc"]

_SYSTEM_PROMPT = (
    "你是保單除外責任判斷助手。你會收到一段客戶與業務討論保險權利的逐字稿，"
    "判斷客戶描述的情況是否涉及本保單的「除外責任」"
    "（保險公司依條款不負給付責任、或暫停豁免保險費的情況）。\n\n"
    "你不知道這份保單條款實際寫什麼，也不可以用你自己對保險的一般常識回答——"
    "你必須透過 browse_semantic_memory 這個工具實際查閱條款內容，再根據查到的條文做判斷。\n\n"
    f"用法：\n"
    f'- 從 scope={json.dumps(_POLICY_ROOT_SCOPE, ensure_ascii=False)} 開始查（這是本次要判斷的保單）。\n'
    "- 每次呼叫會回傳這一層底下有哪些分支（children，各附一句話說明）跟這一層本身的條文內容"
    "（items，如果這層是葉節點的話）。\n"
    "- 看到想深入的分支，把它的 segment 名字加進 scope 陣列裡再呼叫一次，往下鑽。\n"
    "- 如果鑽進去的分支沒有你要的答案，不要用猜的——用回傳值裡的 parent 退回上一層，"
    "看 siblings 裡還有哪些分支沒探索過，換一個方向繼續找。\n\n"
    "下結論前，以下兩件事都要查證過，不能只查其中一項就下結論：\n"
    "1. 有沒有除外條款的原因跟客戶描述的情況對得上？\n"
    "2. 那條除外條款排除的是哪一項保險金給付？那項給付本身的門檻（例如失能要達到第幾級、"
    "是不是符合長期照顧狀態的定義），客戶描述的情況實際上有沒有達到？"
    "如果連給付本身的門檻都沒達到，這項給付本來就不會理賠，跟除外條款無關——這種情況不算「涉及"
    "除外責任」，是給付條件本身沒成立。要查清楚失能等級，通常要另外去查失能程度表（附表），"
    "不會只看除外責任那個分支就有答案。\n"
    "第 1、2 點通常記在不同分支底下，只查完第 1 點就下結論是不夠的。\n"
    "如果你查到了具體的失能等級（例如「第六級」），reason 裡要直接寫出那個等級，並明確跟除外"
    "條款排除的門檻（例如「第二至三級」）做比較、講出結論——不要說「建議客戶再確認」或「若符合"
    "…則…」這種保留字句，你已經查到的資料就是答案，不需要保留、不需要交給客戶或專科醫師再確認。\n\n"
    "- 只有你透過這個工具實際讀到全文的條文，才可以在最終答案裡引用；沒讀過的條文，"
    "即使你覺得應該相關，也不可以引用。\n\n"
    "查完之後，直接用純文字回答，格式固定為：\n"
    '{"involves_exclusion": true 或 false, "matched_articles": ["第XX條", ...], "reason": "..."}\n'
    "- matched_articles 只能放你已經透過工具讀到全文的條號/附表名稱"
    "（跟工具回傳的 \"article\" 欄位完全一致的字串），沒有相關條文就給空陣列。\n"
    "- reason 用一兩句話說明判斷依據，特別是如果查到的除外條款跟客戶的失能情況不完全對應"
    "（例如除外條款只適用某幾種給付，不是全部給付），要在 reason 裡講清楚。\n"
    "- 不要有格式以外的文字。"
)

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
    # The system prompt asks for JSON only, but gemini-cheap occasionally
    # prefaces it with explanatory prose anyway (observed for genuinely
    # informational questions, where the model wants to be helpful beyond
    # the terse verdict format) -- parsing the whole string as JSON then
    # breaks even though a valid JSON object is right there in it. Don't
    # rely on the model perfectly obeying a formatting instruction: decode
    # starting from the first `{` rather than slicing to the last one --
    # `reason` is free-text and may itself contain a literal `{`, which
    # would make a last-brace slice cut the object in the wrong place;
    # raw_decode finds the matching close brace properly instead.
    data, _ = json.JSONDecoder().raw_decode(content, content.index("{"))
    return {
        "involves_exclusion": bool(data["involves_exclusion"]),
        "matched_articles": [str(a) for a in (data.get("matched_articles") or [])],
        "reason": str(data.get("reason") or ""),
    }


def _build_result(transcript: str, verdict: dict) -> dict:
    """should_notify/subject/body are derived here, deterministically, not
    asked of the model -- docs/exclusion-scenario-plan.md §3.5/P0's
    should_notify/subject/body contract is the scenario step's
    responsibility to fill in, and the judgment fields above (involves_
    exclusion/matched_articles/reason) are already the model's whole job;
    letting code assemble the rest keeps that job narrow and the output
    deterministic instead of trusting the model to also format a subject
    line correctly."""
    involves = verdict["involves_exclusion"]
    matched = verdict["matched_articles"]
    reason = verdict["reason"]
    return {
        "involves_exclusion": involves,
        "matched_articles": matched,
        "reason": reason,
        "should_notify": involves,
        "subject": "客戶對話涉及保單除外責任，需要人工覆核" if involves else "",
        "body": (
            f"逐字稿：\n{transcript}\n\n"
            f"判斷：{'涉及除外責任' if involves else '不涉及除外責任'}\n"
            f"引用條文：{'、'.join(matched) if matched else '無'}\n"
            f"理由：{reason}"
        ),
    }


@wrap_agent_exception("check")
async def judge_exclusion(
    gateway: MCPGateway,
    transcript: str,
    *,
    store: Any | None = None,
    memory_policy: MemoryPolicy | None = None,
    tenant: str = "default",
) -> dict:
    system_prompt, all_tools = await asyncio.gather(
        inject_procedural(store, memory_policy, tenant=tenant, scope=_MEMORY_SCOPE, base_prompt=_SYSTEM_PROMPT, limit=_PROCEDURAL_LIMIT),
        gateway.list_openai_tools(),
    )
    # Only offer the browse tool -- `check` also has lookup__* (TSMC
    # scenario leftover, irrelevant here) via the `reader` role; narrowing
    # the tool list avoids tempting the model into an unrelated call, same
    # reasoning llm/tsmc_judge.py uses to remove the lookup tool once its
    # deterministic backstop already covers it.
    tools = [t for t in all_tools if t["function"]["name"] == _BROWSE_TOOL]
    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": transcript},
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
        MODEL_NAME,
        messages,
        tools,
        gateway,
        node="check",
        max_turns=MAX_TURNS,
        stall_guard=stall_guard,
        on_tool_result=_on_tool_result,
        on_turn_end=_on_turn_end,
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
            MODEL_NAME,
            messages,
            tools,
            gateway,
            node="check",
            max_turns=_RETRY_MAX_TURNS,
            on_tool_result=_on_tool_result,
            on_turn_end=_on_turn_end,
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

    return _build_result(transcript, verdict)
