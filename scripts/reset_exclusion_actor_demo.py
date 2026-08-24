"""Resets the DB state written by docs/exclusion-actor-distinction-demo.md's
demo -- the "misjudge -> distill -> add candidate back -> improve" loop for
gemini-cheap on the article-29 要保人/被保險人 actor-distinction blind spot.
Lets the demo be run repeatedly from a clean slate instead of accumulating
whatever the last run left behind (an approved rule, a staged eval
candidate, a rejected one still sitting pending, ...).

Deletes:
  - the two episodic seed cases (docs §2) from default/episodic and, if
    scripts/stage_candidate_for_eval.py was run, their mirror under
    eval/episodic
  - any procedural memory under default/procedural for this scope whose
    `evidence` references either seed case's key, regardless of `status`
    (pending, rejected, or approved -- store.asearch() bypasses recall()'s
    active-only filter, same technique stage_candidate_for_eval.py's
    _wipe() already uses) -- catches the distiller's candidate no matter
    what key it got assigned or how far review got
  - the entire eval/procedural and eval/episodic scope for this step,
    reusing stage_candidate_for_eval.py's own _wipe() -- that tenant is
    already documented as disposable, one-shot-overwrite scratch space, so
    a full wipe is simpler and just as correct as trying to match keys

Deliberately does NOT touch evals/check_cases.yaml -- if a demo run adds a
case there (docs' step 6), that's a git-tracked file edit, not DB state;
revert it manually with `git checkout -- evals/check_cases.yaml` if you
don't want to keep it.

Run with:
    uv run python -m scripts.reset_exclusion_actor_demo [--dry-run]

Requires the local stack (`uv run honcho start`).
"""

from __future__ import annotations

import argparse
import asyncio

from persistence.memory import MemoryKind, build_namespace
from persistence.memory_lifespan import open_agent_memory
from scripts.stage_candidate_for_eval import _wipe

_SCOPE = ("stt_exclusion_notify", "check")
_EPISODIC_KEYS = ["seed-policyholder_assault_disability23", "seed-policyholder_family_assault_disability3"]
_SCAN_LIMIT = 200  # same "effectively all of them" reasoning as distill_procedural.py's _RULE_LIMIT


async def main(dry_run: bool) -> None:
    async with open_agent_memory("mcp_servers/policy.yaml") as (store, _policy):
        for tenant in ("default", "eval"):
            namespace = build_namespace(MemoryKind.EPISODIC, tenant, _SCOPE)
            for key in _EPISODIC_KEYS:
                if await store.aget(namespace, key) is None:
                    continue
                print(f"[reset] {'would delete' if dry_run else 'deleting'} {tenant}/episodic/{'/'.join(_SCOPE)}/{key}")
                if not dry_run:
                    await store.adelete(namespace, key)

        procedural_ns = build_namespace(MemoryKind.PROCEDURAL, "default", _SCOPE)
        for item in await store.asearch(procedural_ns, limit=_SCAN_LIMIT):
            evidence = item.value.get("evidence") or []
            if not set(evidence) & set(_EPISODIC_KEYS):
                continue
            print(f"[reset] {'would delete' if dry_run else 'deleting'} default/procedural/{'/'.join(_SCOPE)}/{item.key} (status={item.value.get('status')}, evidence={evidence})")
            if not dry_run:
                await store.adelete(procedural_ns, item.key)

        for kind in (MemoryKind.PROCEDURAL, MemoryKind.EPISODIC):
            eval_ns = build_namespace(kind, "eval", _SCOPE)
            staged = await store.asearch(eval_ns, limit=_SCAN_LIMIT)
            if not staged:
                continue
            print(f"[reset] {'would wipe' if dry_run else 'wiping'} {len(staged)} item(s) under eval/{kind.value}/{'/'.join(_SCOPE)}")
            if not dry_run:
                await _wipe(store, kind, _SCOPE)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="list what would be deleted without deleting")
    args = parser.parse_args()
    asyncio.run(main(args.dry_run))
