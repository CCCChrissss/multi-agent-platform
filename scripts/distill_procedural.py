"""P2 distiller (docs/knowledge-distillation-plan.md §3): reads a scope's
episodic memories -- plus its existing *active* procedural rules, so the
model doesn't propose duplicates -- and asks an LLM to generalize them into
candidate procedural rules. Every candidate is written with
`status="pending"`, invisible to recall()/browse() (persistence/memory.py's
§5 P0 gate) until a human reviewer promotes it -- the M5 quality gate (P3,
not built yet). Writing here can never affect a live agent, which is
exactly why P0 (the status gate) had to land before this script could be
written at all.

`evidence`/`rationale` are stored in `value` at the top level via
remember()'s `extra=` (persistence/memory.py), not inside `content` --
`content` only ever holds `{"rule": str}`, the same platform-standard shape
inject_procedural() (persistence/memory_prompt.py) reads for *active*
procedural rules. evidence/rationale are for the human reviewer, not for
injection into any agent's prompt.

Run with:
    uv run python -m scripts.distill_procedural --scope stt_exclusion_notify/check [--limit 20]

Requires the local stack (`uv run honcho start`).
"""

from __future__ import annotations

import argparse
import asyncio
import uuid

from gateway.client import chat_json
from langgraph.store.base import SearchItem
from persistence.call_log import current_node_name, current_thread_id
from persistence.memory import MemoryKind, parse_scope, recall, remember
from persistence.memory_lifespan import open_agent_memory

_POLICY_PATH = "mcp_servers/policy.yaml"
_PRINCIPAL = "distiller"
# Summarizing already-collected cases into a rule is a much lighter ask than
# llm/exclusion_judge.py's multi-hop browsing loop -- gemini-cheap is a
# reasonable starting point here, not the same "worth revisiting" capability
# concern docs/knowledge-distillation-plan.md's P2-kickoff findings raised
# for that loop. Revisit if candidate quality turns out to be the bottleneck
# once a human is actually reviewing these.
_MODEL = "gemini-cheap"
_READ_LIMIT = 20
_RULE_LIMIT = 200
"""Separate, generous cap for the "already have these rules, don't
duplicate" procedural recall -- decoupled from --limit (which only bounds
the episodic evidence pool) so a scope with more than _READ_LIMIT active
rules doesn't drop its oldest, most-stable ones from the dedup check and
get them regenerated as fresh pending duplicates. Rules are curated by a
human reviewer (unlike episodic, which accumulates automatically), so 200
is "effectively all of them" at any scale this platform runs at today."""

_SYSTEM_PROMPT = (
    "你是一個規則歸納助手。你會看到一個判斷任務過去的案例（客戶描述 -> 查證後的結論），"
    "以及這個任務目前已經有的規則清單。\n\n"
    "你的工作：找出案例裡有沒有可以歸納成「規則」的模式——一條規則應該是一句話，"
    "能直接加進另一個 AI 做判斷時參考的 system prompt 裡，用來修正那個 AI 自己可能想不到的盲點"
    "（例如常見的誤判方向、容易忽略的查證步驟）。\n\n"
    "不要提出跟現有規則語意重複的規則。如果案例裡看不出值得歸納的新模式，就回傳空的 candidates 陣列，"
    "不要為了有產出而硬湊。\n\n"
    "輸出格式固定為 JSON：\n"
    '{"candidates": [{"rule": "...", "evidence": ["案例key1", ...], "rationale": "..."}]}\n'
    "- rule：規則文字本身，會直接被注入另一個 AI 的 system prompt，不要包含任何解釋性文字。\n"
    "- evidence：這條規則是從哪幾個案例的 key 歸納出來的，只能填下面實際列出的案例 key，不能自己編。\n"
    "- rationale：給人審核用的說明，解釋為什麼從這些案例可以歸納出這條規則——這段不會被注入 prompt。"
)


def _render_cases(items: list[SearchItem]) -> str:
    lines = ["過去案例："]
    for item in items:
        content = item.value["content"]
        lines.append(f"- key={item.key!r} 客戶描述：「{content['input']}」→ 查證後結論：{content['output']}")
    return "\n".join(lines)


def _render_rules(items: list[SearchItem]) -> str:
    if not items:
        return "目前已有規則：（無）"
    lines = ["目前已有規則："]
    lines.extend(f"- {item.value['content']['rule']}" for item in items)
    return "\n".join(lines)


async def main(scope_arg: str, limit: int) -> list[str]:
    """Returns the keys actually written (empty if nothing to distill or the
    model proposed nothing) -- demo/api.py's POST /memory/distill job result
    for the UI; `print()` stays for the CLI path, unchanged."""
    scope = parse_scope(scope_arg)

    current_thread_id.set(f"distill-{uuid.uuid4()}")
    current_node_name.set(_PRINCIPAL)

    async with open_agent_memory(_POLICY_PATH) as (store, policy):
        # recall() only ever returns status="active" (§5 P0) -- existing
        # *pending* candidates from a previous distill run deliberately
        # don't count as "already have this rule", since they haven't been
        # reviewed yet.
        episodic = await recall(store, policy, MemoryKind.EPISODIC, tenant="default", scope=scope, limit=limit)
        procedural = await recall(
            store, policy, MemoryKind.PROCEDURAL, tenant="default", scope=scope, limit=_RULE_LIMIT
        )

        if not episodic:
            print(f"[distill] no episodic memories under default/episodic/{'/'.join(scope)} -- nothing to distill")
            return []

        user_content = f"{_render_cases(episodic)}\n\n{_render_rules(procedural)}"
        response = chat_json(_MODEL, _SYSTEM_PROMPT, user_content)
        candidates = response.get("candidates") or []

        if not candidates:
            print(f"[distill] model proposed no new candidates for default/procedural/{'/'.join(scope)}")
            return []

        # A candidate's evidence must point at cases the model actually saw
        # -- same "don't trust the model's own claim" posture llm/
        # exclusion_judge.py's citation check uses, just checked against the
        # prompt input here instead of tool-call results. Hallucinated keys
        # are dropped, not the whole candidate -- a partially-grounded
        # candidate still has something real for a human to review.
        valid_keys = {item.key for item in episodic}
        written_keys = []
        for candidate in candidates:
            rule = candidate.get("rule")
            evidence = [e for e in (candidate.get("evidence") or []) if e in valid_keys]
            rationale = candidate.get("rationale", "")
            if not rule or not evidence:
                print(f"[distill] skipping malformed candidate (missing rule or no valid evidence): {candidate!r}")
                continue
            key = f"pending-{uuid.uuid4()}"
            await remember(
                store, policy, MemoryKind.PROCEDURAL, tenant="default", scope=scope,
                key=key, content={"rule": rule}, status="pending",
                extra={"evidence": evidence, "rationale": rationale},
            )
            written_keys.append(key)
            print(f"[distill] wrote pending candidate {key}: {rule!r} (evidence={evidence})")

        print(f"[distill] wrote {len(written_keys)} pending candidate(s) under default/procedural/{'/'.join(scope)}")
        return written_keys


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--scope", required=True, help="<workflow_name>/<step_name>, e.g. stt_exclusion_notify/check")
    parser.add_argument("--limit", type=int, default=_READ_LIMIT)
    args = parser.parse_args()
    asyncio.run(main(args.scope, args.limit))
