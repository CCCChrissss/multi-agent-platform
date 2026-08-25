"""P3 §5 #4 -- the human review CLI. docs/knowledge-distillation-plan.md §4.4:
procedural is *never* auto-promoted, no matter how clean an eval comparison
looks -- this is the only path a `pending` candidate can become `active`.

For each pending candidate under a scope: shows the rule/rationale/evidence,
stages it into the disposable `eval` tenant (scripts/stage_candidate_for_eval
.py's stage()) and prints a real baseline-vs-candidate pass-rate comparison
over evals/check_cases.yaml -- exact mechanism §4.3 caught a real false
positive this way (a candidate that looked like it fixed a case only because
staging had silently dropped episodic few-shot context; see that doc --
since resolved by P5 removing episodic few-shot injection outright, so
there's no longer any few-shot context for staging to drop). Then asks
approve/reject/skip.

Also prints a second, separate "evidence diagnostic" table: each episodic
case in `evidence` gets its own transcript re-judged fresh under baseline
and candidate, right before the approve/reject prompt -- otherwise nothing
in this CLI ever actually re-checks whether the candidate changes the
judgment on the very case it claims to fix; the holdout table above only
proves nothing else broke. Deliberately never scored as pass/fail against
the episodic `output` -- that value was never verified as correct (§6 #4,
same reason _print_regression_suggestions() below makes the reviewer
confirm `expected` by hand instead of trusting it), so treating it as
ground truth here would be testing the rule against its own possibly-wrong
premise. Printed as its own labeled section so it's never mistaken for the
scored holdout comparison.

- approve: flips status pending -> active in place (persistence/memory.py's
  edit(), not remember() -- this is an edit to an existing item, not a new
  write, and must preserve the original evidence/rationale/created_by
  instead of re-stamping audit fields as if a new memory were created).
  Also prints a ready-to-paste `evals/check_cases.yaml` entry per evidence
  case (split: regression) for the reviewer to add by hand -- that file is
  explicitly human-maintained, not auto-generated (§5 P3 #1), because the
  episodic `output` was never verified as correct (§6 #4) and the reviewer
  must confirm `expected` themselves.
- reject: deletes the pending item (persistence/memory.py's forget()).
  ponytail: no `status="rejected"` negative-signal loop back into the
  distiller (§6 #6 still open, deferred) -- add if the same rejected rule
  keeps getting regenerated in practice.

P4: at the approve/reject/skip prompt, `e` lets the reviewer retype the
candidate's `rule` text in place, re-stages it into `eval` (remember()
defaults status="active" -- see persistence/memory.py:274 -- so this is a
plain overwrite of the same eval-tenant key, no need to redo stage()'s
episodic mirror since that's untouched by an edit) and reruns the
comparison before asking again. Baseline is computed once per candidate,
outside the edit loop -- it can't change from editing the candidate side,
re-running it per edit would just burn eval calls for the same answer. If
an edited version gets approved, the stored value gets `edited_by_reviewer:
true` -- content diverges from what the distiller actually produced, and
`created_by` alone would misattribute the final text to it.

Run with:
    uv run python -m scripts.review_memory --scope stt_exclusion_notify/check [--repeats 3] [--model gemini-cheap] [--key pending-...]

Requires the local stack (`uv run honcho start`).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from evals.run_eval import _load_cases, _run_case
from mcp_servers.gateway import MCPGateway
from mcp_servers.policy import load_policy
from persistence.call_log import current_node_name, current_thread_id
from persistence.memory import MemoryKind, build_namespace, edit, forget, list_pending, parse_scope, remember
from persistence.memory_lifespan import open_agent_memory
from scripts.stage_candidate_for_eval import _EVAL_TENANT, stage

_POLICY_PATH = "mcp_servers/policy.yaml"
_PRINCIPAL = "memory_writer"  # same principal stage_candidate_for_eval.py uses -- this CLI both reads pending candidates and flips their status


async def _run_side(
    gateway: MCPGateway, store: Any, memory_policy: Any, tenant: str, repeats: int, model: str | None = None
) -> list[dict]:
    cases = _load_cases()
    # Independent runs against a store pool already sized for concurrency
    # (persistence/memory_store.py's max_size=4) -- no reason to stack them
    # sequentially on the human reviewer's wall-clock time.
    return list(
        await asyncio.gather(*(_run_case(gateway, store, memory_policy, tenant, case, repeats, model) for case in cases))
    )


async def _run_evidence_side(
    gateway: MCPGateway, store: Any, memory_policy: Any, tenant: str, cases: list[dict], repeats: int, model: str | None = None
) -> list[dict]:
    if not cases:
        return []
    return list(
        await asyncio.gather(*(_run_case(gateway, store, memory_policy, tenant, c, repeats, model) for c in cases))
    )


def _print_comparison(baseline: list[dict], candidate: list[dict]) -> None:
    print(f"\n{'id':<24} {'split':<10} {'baseline':<10} {'candidate':<10} change")
    for b, c in zip(baseline, candidate):
        change = "-"
        if c["pass_rate"] > b["pass_rate"]:
            change = "IMPROVED"
        elif c["pass_rate"] < b["pass_rate"]:
            change = "REGRESSED"
        print(f"{b['id']:<24} {b['split']:<10} {b['pass_rate']:<10.0%} {c['pass_rate']:<10.0%} {change}")


async def _load_evidence_cases(store: Any, scope: tuple[str, ...], evidence: list[str]) -> list[dict]:
    """Turns each `evidence` episodic key into a synthetic eval case so
    main() can re-judge its transcript fresh under baseline/candidate right
    before the approve/reject prompt -- see module docstring on why the
    holdout table alone was never enough (it can only show nothing else
    broke, never that the case the rule claims to fix actually changed).
    `expected` here is the episodic record's own `output` -- not verified
    ground truth, only usable as a *reference* for _print_evidence_
    diagnostic() to show a match/diverge signal, never as a real
    pass/fail gate (§6 #4 -- same reason _print_regression_suggestions()
    makes the reviewer confirm `expected` by hand instead of trusting it)."""
    namespace = build_namespace(MemoryKind.EPISODIC, "default", scope)
    cases = []
    for evidence_key in evidence:
        item = await store.aget(namespace, evidence_key)
        if item is None:
            continue
        content = item.value["content"]
        try:
            expected = json.loads(content["output"])
        except json.JSONDecodeError:
            print(f"[review] skipping evidence case {evidence_key!r} -- stored output isn't valid JSON: {content['output']!r}")
            continue
        cases.append({
            "id": evidence_key,
            "transcript": content["input"],
            "expected": expected,
            "split": "evidence",
        })
    return cases


def _print_evidence_diagnostic(baseline: list[dict], candidate: list[dict]) -> None:
    print("\n--- evidence diagnostic (參考用，非正式判準 -- episodic 記錄的 output 未經人工驗證，見 §6 #4) ---")
    print(f"{'id':<28} {'episodic 記錄':<14} {'baseline runs':<20} {'candidate runs':<20} changed?")
    for b, c in zip(baseline, candidate):
        recorded = str(b["expected_involves_exclusion"]).lower()
        b_runs = [run["involves_exclusion"] for run in b["runs"]]
        c_runs = [run["involves_exclusion"] for run in c["runs"]]
        changed = "yes" if b_runs != c_runs else "-"
        print(f"{b['id']:<28} {recorded:<14} {str(b_runs):<20} {str(c_runs):<20} {changed}")


async def compare(
    gateway: MCPGateway, store: Any, memory_policy: Any, item: Any, scope: tuple[str, ...], repeats: int, model: str | None = None
) -> dict[str, Any]:
    """stage + baseline + candidate + evidence 四段，回傳結構化結果 -- the
    calculation half of main()'s loop, split out so a caller (e.g. the demo
    API's background job) can get the comparison data without CLI's
    print/input() loop. Re-staging happens from `item`'s original pending
    content every call -- a caller re-running this after the reviewer edits
    the rule text must restage separately (see main()'s edit branch), since
    stage() always pulls from the pending item, not the eval tenant's
    current (possibly edited) content."""
    current_node_name.set(_PRINCIPAL)
    await stage(store, memory_policy, item.key, scope, "default")

    evidence = item.value.get("evidence", [])
    evidence_cases = await _load_evidence_cases(store, scope, evidence)

    baseline, baseline_evidence = await asyncio.gather(
        _run_side(gateway, store, memory_policy, "default", repeats, model),
        _run_evidence_side(gateway, store, memory_policy, "default", evidence_cases, repeats, model),
    )
    candidate, candidate_evidence = await asyncio.gather(
        _run_side(gateway, store, memory_policy, _EVAL_TENANT, repeats, model),
        _run_evidence_side(gateway, store, memory_policy, _EVAL_TENANT, evidence_cases, repeats, model),
    )
    return {
        "baseline": baseline,
        "candidate": candidate,
        "evidence_baseline": baseline_evidence,
        "evidence_candidate": candidate_evidence,
        "evidence_cases": evidence_cases,
    }


async def approve(
    store: Any, memory_policy: Any, scope: tuple[str, ...], key: str, value: dict[str, Any], *, edited: bool = False
) -> None:
    """Flips a pending candidate to active -- shared by main()'s CLI loop and
    demo/api.py's POST /memory/candidate/{key}/approve, so this write's
    exact shape (status/reviewed_by/reviewed_at/edited_by_reviewer) lives in
    one place instead of two copies that could drift."""
    updated = {**value, "status": "active", "reviewed_by": _PRINCIPAL, "reviewed_at": datetime.now(UTC).isoformat()}
    if edited:
        # content no longer matches what created_by (the distiller) actually
        # produced -- flag the divergence instead of silently misattributing it.
        updated["edited_by_reviewer"] = True
    await edit(store, memory_policy, MemoryKind.PROCEDURAL, tenant="default", scope=scope, key=key, value=updated)
    print(f"[review] approved {key!r} -- now active under default/procedural/{'/'.join(scope)}")


async def reject(store: Any, memory_policy: Any, scope: tuple[str, ...], key: str) -> None:
    """Deletes a pending candidate -- shared the same way approve() is."""
    await forget(store, memory_policy, MemoryKind.PROCEDURAL, tenant="default", scope=scope, key=key)
    print(f"[review] rejected {key!r} -- deleted")


async def regression_suggestions(store: Any, scope: tuple[str, ...], evidence: list[str]) -> list[dict[str, Any]]:
    """Ready-to-paste `evals/check_cases.yaml` entries (`split: regression`)
    for each evidence case -- computed once so main()'s CLI print and
    demo/api.py's approve response render the same data instead of two
    independently-written copies. `expected` is deliberately left for the
    reviewer to fill in by hand (§6 #4 -- the episodic `output` was never
    verified as correct); `model_output` is only what the model answered at
    the time, not a substitute for it."""
    namespace = build_namespace(MemoryKind.EPISODIC, "default", scope)
    suggestions = []
    for evidence_key in evidence:
        item = await store.aget(namespace, evidence_key)
        if item is None:
            continue
        content = item.value["content"]
        suggestions.append({
            "id": evidence_key,
            "transcript": content["input"],
            "model_output": content["output"],
            "split": "regression",
        })
    return suggestions


def _print_regression_suggestions(suggestions: list[dict[str, Any]]) -> None:
    for s in suggestions:
        print(f"\n# 建議加進 evals/check_cases.yaml -- 先人工確認 expected 是否正確再貼上：")
        print(f"- id: {s['id']}")
        print(f"  transcript: {s['transcript']!r}")
        print(f"  expected: {{involves_exclusion: <人工確認>}}  # 模型當時輸出：{s['model_output']!r}")
        print(f"  split: regression")


async def main(scope_arg: str, repeats: int, model: str | None, key: str | None = None) -> None:
    if model:
        # Diagnostic-only override, same technique evals/run_eval.py's
        # --model uses -- gemini-strong's ceiling effect (docs/
        # knowledge-distillation-plan.md) makes baseline/candidate look
        # identical on most eval cases, hiding exactly the signal a
        # reviewer needs to see. Threaded through _run_side/_run_evidence_side
        # -> _run_case -> judge_exclusion(model=...) below, not a module
        # attribute patch (docs/generic-agent-runtime-plan.md P5 removed
        # llm/exclusion_judge.py's MODEL_NAME constant).
        print(f"[review] overriding model: -> {model!r} (this run only)")
    scope = parse_scope(scope_arg)

    current_thread_id.set(f"review-{uuid.uuid4()}")
    current_node_name.set(_PRINCIPAL)
    gateway_policy = load_policy(_POLICY_PATH)

    # principal="check" for the gateway: judge_exclusion() reads the policy
    # tree through the memory__browse_semantic_memory MCP subprocess tool,
    # same reasoning as evals/run_eval.py::main().
    async with (
        open_agent_memory(_POLICY_PATH) as (store, memory_policy),
        MCPGateway(gateway_policy, principal="check") as gateway,
    ):
        pending = await list_pending(store, MemoryKind.PROCEDURAL, scope, key)
        if not pending:
            reason = f"key={key!r} not found or not pending" if key else "no pending candidates"
            print(f"[review] {reason} under default/procedural/{'/'.join(scope)}")
            return

        for item in pending:
            print(f"\n{'=' * 72}\ncandidate {item.key}")
            print(f"rationale: {item.value.get('rationale', '(none)')}")
            evidence = item.value.get("evidence", [])
            print(f"evidence: {evidence}")

            content = item.value["content"]
            edited = False
            # _run_side() -> _run_case() -> evals.run_eval._run_once() sets
            # current_node_name="check" for judge_exclusion() and never
            # restores it (evals/run_eval.py:67) -- this codebase's
            # convention is that every principal-sensitive call re-sets the
            # contextvar itself right before running (see
            # persistence/memory_smoke_test.py), so stage()'s remember()
            # calls need a fresh set here every iteration, not just once in
            # main() before the loop. compare() does this set itself.
            result = await compare(gateway, store, memory_policy, item, scope, repeats, model)
            baseline, baseline_evidence = result["baseline"], result["evidence_baseline"]
            candidate, candidate_evidence = result["candidate"], result["evidence_candidate"]
            evidence_cases = result["evidence_cases"]

            while True:
                print(f"rule: {content['rule']}")
                _print_comparison(baseline, candidate)

                if evidence_cases:
                    _print_evidence_diagnostic(baseline_evidence, candidate_evidence)
                else:
                    print("\n--- evidence diagnostic: skipped (no evidence episodic case(s) found in store) ---")

                decision = input("\napprove / reject / edit / skip this candidate? [a/r/e/s]: ").strip().lower()
                if decision == "e":
                    new_rule = input("new rule text (blank to cancel):\n> ").strip()
                    if new_rule:
                        content = {**content, "rule": new_rule}
                        edited = True
                        current_node_name.set(_PRINCIPAL)  # same leak as above -- the _run_side call just above left it as "check"
                        await remember(
                            store, memory_policy, MemoryKind.PROCEDURAL, tenant=_EVAL_TENANT, scope=scope,
                            key=item.key, content=content,
                        )
                        # candidate/candidate_evidence only -- baseline never
                        # changes from editing the candidate side, and
                        # restaging via compare() would overwrite this edit
                        # with the pending item's original content.
                        candidate, candidate_evidence = await asyncio.gather(
                            _run_side(gateway, store, memory_policy, _EVAL_TENANT, repeats, model),
                            _run_evidence_side(gateway, store, memory_policy, _EVAL_TENANT, evidence_cases, repeats, model),
                        )
                    else:
                        print("[review] cancelled edit -- unchanged")
                    continue
                if decision == "a":
                    await approve(store, memory_policy, scope, item.key, {**item.value, "content": content}, edited=edited)
                    _print_regression_suggestions(await regression_suggestions(store, scope, evidence))
                elif decision == "r":
                    await reject(store, memory_policy, scope, item.key)
                else:
                    print("[review] skipped -- still pending")
                break


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--scope", required=True, help="<workflow_name>/<step_name>, e.g. stt_exclusion_notify/check")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--model", default=None, help="Override the check step's model for this run (see gateway/config.yaml)")
    parser.add_argument("--key", default=None, help="Only review this one pending candidate's key, ignore other pending ones in scope")
    args = parser.parse_args()
    asyncio.run(main(args.scope, args.repeats, args.model, args.key))
