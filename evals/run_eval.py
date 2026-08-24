"""Evaluation runner for docs/knowledge-distillation-plan.md P1: replays
evals/check_cases.yaml against the real llm/exclusion_judge.py::judge_exclusion()
pipeline (real gateway, real seeded policy tree, real memory reads) and
prints a per-case pass *rate* -- the baseline every future memory-promotion
decision (P2/P3's candidate-vs-baseline comparison) is measured against.

`--repeats N` (default 3), not a single run: a single pass/fail was proven
unreliable in practice (see docs/knowledge-distillation-plan.md's P2-kickoff
findings) -- `drunk_driving_bike` (the P4 case deliberately designed so the
naive answer is wrong) passed twice in a row early on, then failed roughly
5 times out of every 6 repeats once sampled properly. A single run's result
was never a trustworthy "baseline"; the real signal is the rate.

Only `involves_exclusion` is a hard pass/fail signal. `matched_articles` is
printed for visibility but never fails a case -- verified unstable in
practice (see check_cases.yaml's header and the plan doc's §4.1 "已驗證發現"):
the same correct involves_exclusion=false judgment sometimes leaves
matched_articles empty even when the model's own reasoning discussed
specific articles, depending on how it interprets "relevant".

`--tenant` exists for §4.3's candidate-rule evaluation (a pending procedural
rule written active under a throwaway tenant, not yet implemented here --
P3's job).

`--model` overrides llm/exclusion_judge.py's MODEL_NAME for this run only
(module-attribute patch, not a judge_exclusion() parameter -- deliberately
not widening that function's signature for what's a diagnostic-only
comparison, see gateway/config.yaml's gemini-strong entry). Lets this tool
answer "is drunk_driving_bike's 0% pass rate a model-capability ceiling, or
something fixable in the prompt/harness" without touching the production
MODEL_NAME.

Run with:
    uv run python -m evals.run_eval [--repeats 3] [--tenant default] [--model gemini-strong]

Requires the local stack (`uv run honcho start`) and a seeded policy tree
(`uv run python -m scripts.seed_insurance_memory`).
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import yaml

import llm.exclusion_judge as exclusion_judge_module
from llm.exclusion_judge import judge_exclusion
from mcp_servers.gateway import MCPGateway
from mcp_servers.policy import load_policy
from persistence.call_log import current_node_name, current_thread_id
from persistence.memory_lifespan import open_agent_memory

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CASES_PATH = _REPO_ROOT / "evals" / "check_cases.yaml"
_POLICY_PATH = _REPO_ROOT / "mcp_servers" / "policy.yaml"


def _load_cases() -> list[dict]:
    with open(_CASES_PATH) as f:
        return yaml.safe_load(f)


async def _run_once(gateway: MCPGateway, store, memory_policy, tenant: str, case: dict, run_idx: int) -> dict:
    current_thread_id.set(f"eval-{case['id']}-{run_idx}")
    current_node_name.set("check")
    expected = case["expected"]
    try:
        result = await judge_exclusion(gateway, case["transcript"], store=store, memory_policy=memory_policy, tenant=tenant)
        return {
            "passed": result["involves_exclusion"] == expected["involves_exclusion"],
            "involves_exclusion": result["involves_exclusion"],
            "matched_articles": result["matched_articles"],
            "error": None,
        }
    except Exception as exc:
        # A run that raises (e.g. AgentLoopIncomplete on unparseable model
        # output) counts as a failed run, not a crashed eval -- one bad
        # sample must not take down the whole repeats loop.
        return {"passed": False, "involves_exclusion": None, "matched_articles": None, "error": repr(exc)}


async def _run_case(gateway: MCPGateway, store, memory_policy, tenant: str, case: dict, repeats: int) -> dict:
    # Independent repeats against a store pool already sized for concurrency
    # (persistence/memory_store.py's max_size=4) -- no reason to run them
    # one at a time.
    runs = list(
        await asyncio.gather(*(_run_once(gateway, store, memory_policy, tenant, case, i) for i in range(repeats)))
    )
    passed = sum(r["passed"] for r in runs)
    return {
        "id": case["id"],
        "split": case.get("split", "regression"),
        "expected_involves_exclusion": case["expected"]["involves_exclusion"],
        "expected_matched_articles": case["expected"].get("matched_articles"),
        "repeats": repeats,
        "passed": passed,
        "pass_rate": passed / repeats,
        "runs": runs,
    }


async def main(tenant: str, repeats: int, model: str | None) -> None:
    if model:
        print(f"[run_eval] overriding MODEL_NAME: {exclusion_judge_module.MODEL_NAME!r} -> {model!r} (this run only)")
        exclusion_judge_module.MODEL_NAME = model
    cases = _load_cases()
    policy = load_policy(str(_POLICY_PATH))
    # principal="check": judge_exclusion() reads the policy tree entirely
    # through the memory__browse_semantic_memory MCP subprocess tool -- no
    # principal means that subprocess's current_node_name is None and every
    # recall/browse fails closed (docs/exclusion-scenario-plan.md P2's
    # architecture gap). llm/tsmc_judge.py doesn't need this because its
    # deterministic alias backstop never goes through that subprocess.
    async with (
        open_agent_memory(str(_POLICY_PATH)) as (store, memory_policy),
        MCPGateway(policy, principal="check") as gateway,
    ):
        # Independent cases against the same pool -- see _run_case()'s
        # comment on why sequential execution here was never load-bearing.
        results = list(
            await asyncio.gather(*(_run_case(gateway, store, memory_policy, tenant, case, repeats) for case in cases))
        )

    print(f"\n{'id':<24} {'split':<10} {'pass rate':<10} expected involves_exclusion={{}}")
    for r in results:
        print(f"{r['id']:<24} {r['split']:<10} {r['passed']}/{r['repeats']:<8} {r['expected_involves_exclusion']!r}")
        actuals = [run["involves_exclusion"] for run in r["runs"]]
        print(f"{'':<24} {'':<10} {'':<10} actual across runs: {actuals}")
        errors = [run["error"] for run in r["runs"] if run["error"]]
        if errors:
            print(f"{'':<24} {'':<10} {'':<10} errors: {errors}")
        if r["expected_matched_articles"] is not None:
            actual_matched = [run["matched_articles"] for run in r["runs"]]
            print(f"{'':<24} {'':<10} {'':<10} matched_articles (informational, not scored): {actual_matched}")

    total_passed = sum(r["passed"] for r in results)
    total_runs = sum(r["repeats"] for r in results)
    print(f"\noverall: {total_passed}/{total_runs} passed ({total_passed / total_runs:.0%})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", default="default")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--model", default=None, help="Override MODEL_NAME for this run (see gateway/config.yaml)")
    args = parser.parse_args()
    asyncio.run(main(args.tenant, args.repeats, args.model))
