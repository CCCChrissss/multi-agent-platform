"""docs/exclusion-actor-distinction-demo.md step 1 (redesigned for P5): gets
the two 要保人/被保險人 actor-distinction cases into episodic memory as a
genuine live *misjudgment*, not hand-authored already-correct content the
way scripts/seed_exclusion_episodic_examples.py's corpus is.

Calls the real judge_exclusion() with gemini-cheap (docs/exclusion-actor-
distinction-demo.md §3 already found production's gemini-strong has a
ceiling effect that makes this blind spot invisible -- MODEL_NAME override,
same technique as scripts/review_memory.py's --model) against
`tenant="default"`, where the only active procedural rule
(`pending-e9b8205f`, the "不同給付項目" cross-article distinction) doesn't
cover this within-article actor distinction -- so the model is expected to
get both cases wrong (§3's baseline: 0/5).

Whatever verdict comes back -- right or wrong -- gets written to episodic
with status="pending", content shaped exactly like orchestrator/
memory_writer.py::_apply_rule() would produce from a real check.completed
event (output_fields=[involves_exclusion, matched_articles, reason], per
workflows/definitions/stt_exclusion_notify.yaml). This is what "a production
misjudgment" looks like on this platform after P5: a wrong verdict lands in
the store as raw material for a human to catch via scripts/review_episodic.py,
not a leak into anyone's prompt.

Run with:
    uv run python -m scripts.seed_actor_distinction_demo

Requires the local stack (`uv run honcho start`).
"""

from __future__ import annotations

import asyncio
import json
import uuid

import llm.exclusion_judge as exclusion_judge_module
from mcp_servers.gateway import MCPGateway
from mcp_servers.policy import load_policy
from persistence.call_log import current_node_name, current_thread_id
from persistence.memory import MemoryKind, remember
from persistence.memory_lifespan import open_agent_memory

_POLICY_PATH = "mcp_servers/policy.yaml"
_SCOPE = ("stt_exclusion_notify", "check")
_MODEL = "gemini-cheap"  # see module docstring -- production gemini-strong has a ceiling effect here

_CASES = [
    (
        "seed-policyholder_assault_disability23",
        "我朋友的保單要保人是他先生，被保險人是他自己，去年他先生跟他吵架失控打傷他的背，"
        "害他脊椎受損被鑑定到第二級失能，這張保單的意外二至三級失能保險金賠不賠",
    ),
    (
        "seed-policyholder_family_assault_disability3",
        "我表姐的保單要保人是她媽媽，之前她們家因為遺產糾紛，她媽媽情緒失控拿東西砸傷她的脊椎，"
        "害她被鑑定到第三級失能，這個意外二至三級失能保險金賠不賠",
    ),
]


async def main() -> None:
    print(f"[seed] overriding MODEL_NAME: {exclusion_judge_module.MODEL_NAME!r} -> {_MODEL!r} (this run only)")
    exclusion_judge_module.MODEL_NAME = _MODEL

    gateway_policy = load_policy(_POLICY_PATH)
    async with (
        open_agent_memory(_POLICY_PATH) as (store, memory_policy),
        MCPGateway(gateway_policy, principal="check") as gateway,
    ):
        for key, transcript in _CASES:
            current_thread_id.set(f"seed-actor-distinction-{uuid.uuid4()}")
            current_node_name.set("check")  # the real principal judge_exclusion() runs as
            verdict = await exclusion_judge_module.judge_exclusion(
                gateway, transcript, store=store, memory_policy=memory_policy, tenant="default"
            )
            output = json.dumps(
                {k: verdict[k] for k in ("involves_exclusion", "matched_articles", "reason")}, ensure_ascii=False
            )
            correctness = "WRONG (expected involves_exclusion=false)" if verdict["involves_exclusion"] else "correct"
            print(f"[seed] {key}: model said {output}\n       -- {correctness}")

            current_node_name.set("memory_writer")  # remember()'s write-grant/created_by principal
            await remember(
                store, memory_policy, MemoryKind.EPISODIC, tenant="default", scope=_SCOPE,
                key=key, content={"input": transcript, "output": output}, status="pending",
            )
        print(f"\n[seed] wrote {len(_CASES)} pending episodic case(s) under default/episodic/{'/'.join(_SCOPE)}")
        print("[seed] next: uv run python -m scripts.review_episodic --scope stt_exclusion_notify/check")


if __name__ == "__main__":
    asyncio.run(main())
