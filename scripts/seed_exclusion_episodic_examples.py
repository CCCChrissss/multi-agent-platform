"""Historical one-time bootstrap for docs/knowledge-distillation-plan.md
P2, written when `default/episodic/stt_exclusion_notify/check` was empty.
That premise is no longer current: production memory_writer can populate
the scope, and the 2026-08-31 Windows DB has one pending episodic there.
This script remains a deterministic known-correct seed corpus for demos;
it is not required when enough human-approved production episodic exists.

Content is three *different* transcripts from evals/check_cases.yaml --
deliberately not reused, on the theory that reusing an eval transcript as
its own few-shot example would contaminate the measurement. That theory
turned out to be **wrong, or at least not the whole story**: an earlier
version of this script did seed the three check_cases.yaml transcripts
verbatim, which broke evals/run_eval.py (`_parse_verdict()` raised
`ValueError: substring not found` on non-JSON model output) -- but
replacing them with these three entirely different transcripts did **not**
fix it. Repeated `evals/run_eval.py` runs with *these* three seeded
(non-overlapping) episodic memories present are flaky across runs: format
breaks on different cases each time, and one run had `drunk_driving_bike`
(the case designed so the naive answer is wrong) answer *wrong*
(involves_exclusion=true) instead of its previously-consistent correct
`false` -- something that never happened across repeated baseline runs with
zero episodic memories in the store. The real cause looks like "gemini-cheap's
format/reasoning discipline on this multi-hop task degrades once *any*
few-shot content is present in the prompt," not anything about *this*
seed data's content -- see the P2-kickoff discussion in the conversation
this script was written in for the full run log. Flagged to the user rather
than silently worked around (e.g. by hand-picking "safer" few-shot content) --
this is a real finding about llm/exclusion_judge.py's robustness, not
something this script should paper over.

Verified via the real judge_exclusion() pipeline (2026-08-10) against the
real seeded policy tree -- matched_articles/reason captured verbatim from
that run, not fabricated. Mirrors orchestrator/memory_writer.py::
_apply_rule()'s exact content shape ({"input": str, "output": <json
string>}) and the workflow's declared memory_write rule (workflows/
definitions/stt_exclusion_notify.yaml's output_fields: [involves_exclusion,
matched_articles, reason]), so what P2 reads here is shaped exactly like
what a real production run would have written -- just front-loaded instead
of waiting for one.

Idempotent: aput() is an upsert on (namespace, key); keys below are fixed.

2026-08-11: added 3 more cases after the P2 distiller's only candidate so
far turned out useless (docs/knowledge-distillation-plan.md §5 P2/P3) --
both original 犯罪/毒品 cases below conclude involves_exclusion=true, so the
only pattern there to generalize was "illegal act -> exclusion applies",
which is the wrong-direction rule that got reject()ed. The real lesson
`evals/check_cases.yaml`'s drunk_driving_bike encodes -- same reason (酒駕)
excludes one benefit (第二十九條 / 意外二至三級失能) but not another (第
二十七條 / 長照) -- had no episodic case to be distilled *from*. The 3 new
cases give the distiller that contrast directly:
  - seed-drunk_ride_ltc: 酒駕 + 長照 claim -> NOT excluded (mirrors
    drunk_driving_bike's insight, deliberately different wording/scenario
    so it isn't the same transcript being both eval label and few-shot
    input -- see the "content is three different transcripts" note above).
  - seed-drunk_driving_grade23: same 酒駕 reason, but claim is 意外二至三級
    失能 (第十六/十七條) -> excluded under 第二十九條. The contrasting pair
    with the case above is the point -- same cause, different benefit,
    different outcome.
  - seed-premium_grace_period: a plain administrative question, no
    exclusion angle at all. Existing case seed-claims_payout_timing already
    covers this shape once; this is a second, distinct example so it's not
    a one-off in the corpus the distiller reads.
All 3 verified against the real judge_exclusion() pipeline (2026-08-11,
gemini-strong, 3 repeats each) -- involves_exclusion was 3/3 stable on all
three; matched_articles text below is captured verbatim from one of those
runs, not fabricated. See docs/exclusion-episodic-cases.md for the full,
human-readable list of every episodic seed case (transcript + verdict) --
the keys/content here alone don't show what's actually in each case.

2026-08-11: added 2 more cases for a different blind spot (要保人 vs 被保險人
actor distinction on article 29), later pulled back out of this file --
docs/exclusion-actor-distinction-demo.md's redesign (P5) needs those two
cases to arrive as a genuine live *misjudgment* (scripts/seed_actor_
distinction_demo.py calls the real judge_exclusion() and captures whatever
it gets wrong), not hand-authored already-correct content the way every
case in this file is -- that's the whole point of the redesigned demo:
showing scripts/review_episodic.py correct a real wrong answer before it's
allowed to become distillation material. This file stays the "known-correct
foundational corpus" for the 酒駕跨條文 blind spot that's already resolved.

Run from the repository root:
    Windows PowerShell:
        .\.venv\Scripts\python.exe -m scripts.seed_exclusion_episodic_examples
    macOS / Bash:
        uv run python -m scripts.seed_exclusion_episodic_examples

Requires PostgreSQL and local-embed through LiteLLM/Ollama because these
writes are indexed. It writes the known-correct corpus directly as active
episodic and therefore intentionally bypasses P5's production pending
review path; do not run it merely to inspect current data.
"""

from __future__ import annotations

import asyncio
import json

from persistence.call_log import current_node_name, current_thread_id
from persistence.memory import MemoryKind, remember
from persistence.memory_lifespan import open_agent_memory

_POLICY_PATH = "mcp_servers/policy.yaml"
_SCOPE = ("stt_exclusion_notify", "check")

_CASES = [
    (
        "seed-criminal_robbery",
        "他之前因為搶劫銀行被警察開槍打傷腦部，現在完全失去自理能力需要人24小時照顧，這個長照險賠不賠",
        {
            "involves_exclusion": True,
            "matched_articles": ["第二十七條"],
            "reason": (
                "客戶因「搶劫銀行」（犯罪行為）導致腦部受傷並符合長期照顧狀態，依據第二十七條之除外責任條款，"
                "被保險人因犯罪行為所致之長期照顧狀態，保險公司不負給付相關保險金及豁免保險費的責任。"
            ),
        },
    ),
    (
        "seed-claims_payout_timing",
        "申請理賠金核准之後大概要多久時間才能撥款到我的帳戶",
        {
            "involves_exclusion": False,
            "matched_articles": ["第十條"],
            "reason": "此問題與除外責任無關，屬理賠行政程序。根據第十條規定，保險公司應於收齊各項理賠申請文件後十五日內給付保險金。",
        },
    ),
    (
        "seed-drug_driving_amputation",
        "他吸毒後開車撞上護欄，雙腿在意外中完全截肢，這個意外險賠不賠",
        {
            "involves_exclusion": True,
            "matched_articles": ["第二十七條", "第二十九條"],
            "reason": (
                "依條款第二十七條及第二十九條規定，非法施用防制毒品相關法令所稱之毒品為除外責任。本案被保險人因吸毒後駕車"
                "導致雙腿截肢，屬於該除外責任範圍，保險公司不負給付長期照顧保險金及意外二至三級失能保險金之責任。"
            ),
        },
    ),
    (
        "seed-drunk_ride_ltc",
        "我朋友他去年喝酒之後騎機車摔倒，頸椎受傷，現在生活起居都要人照顧，這張保單的長期照顧保險金賠不賠",
        {
            "involves_exclusion": False,
            "matched_articles": ["第二十七條", "第二十九條"],
            "reason": (
                "依據第二十七條規定，長期照顧保險金的除外責任僅包含故意行為、犯罪行為及非法施用毒品，"
                "並未將酒駕明文列入；酒駕僅於第二十九條被明確列為「意外二至三級失能保險金」的除外責任。"
                "因此，若該酒駕行為未達構成犯罪行為之標準，長期照顧保險金並不涉及除外責任。"
            ),
        },
    ),
    (
        "seed-drunk_driving_grade23",
        "他喝酒之後開車撞斷護欄，脊椎受傷評估到第二級失能，這個意外二至三級失能保險金賠不賠",
        {
            "involves_exclusion": True,
            "matched_articles": ["第二十九條"],
            "reason": (
                "客戶因酒駕車禍導致第二級失能，已達到該項給付所設定的「第二至三級」失能門檻，但依據第二十九條規定，"
                "被保險人飲酒後駕車（酒精濃度超標）致成附表三所列第二至三級失能程度時，"
                "為意外二至三級失能保險金之除外責任，故保險公司不負理賠責任。"
            ),
        },
    ),
    (
        "seed-premium_grace_period",
        "如果忘記繳保費有幾天的寬限期可以補繳",
        {
            "involves_exclusion": False,
            "matched_articles": [],
            "reason": "此問題詢問保費補繳之寬限期，與除外責任無關，屬於保險費交付與寬限期間之契約行政程序。",
        },
    ),
]


async def main() -> None:
    current_node_name.set("memory_writer")  # the principal remember()'s created_by/write-grant check expects
    async with open_agent_memory(_POLICY_PATH) as (store, policy):
        for key, transcript, verdict in _CASES:
            current_thread_id.set(key)
            await remember(
                store,
                policy,
                MemoryKind.EPISODIC,
                tenant="default",
                scope=_SCOPE,
                key=key,
                content={"input": transcript, "output": json.dumps(verdict, ensure_ascii=False)},
            )
        print(f"[seed] wrote {len(_CASES)} episodic memories under default/episodic/{'/'.join(_SCOPE)}")


if __name__ == "__main__":
    asyncio.run(main())
