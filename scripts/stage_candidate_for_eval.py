"""P3 §4.3 approach 3: stages one `pending` procedural candidate (written by
scripts/distill_procedural.py) as `active` under the disposable `eval`
tenant, so evals/run_eval.py --tenant eval exercises the real
judge_exclusion() -> inject_procedural() path against it -- no `recall()`
backdoor, no duplicated judging logic (docs/knowledge-distillation-plan.md
§4.3).

Also mirrors `default`'s other currently-active *procedural* rules into
`eval` (TODO.md's stage-eval-single-rule-not-cumulative) -- without this,
staging only the candidate would compare "candidate alone" against
"default's other active rules alone" instead of "default's active rules +
candidate" against "default's active rules alone", not what a real
production rollout looks like once more than one rule is active. Mirroring
keeps everything but the candidate identical between the two runs.

No episodic mirror: docs/knowledge-distillation-plan.md P5 removed episodic
few-shot injection from judge_exclusion() entirely (it's raw distiller
material now, never part of an agent's prompt -- persistence/memory_prompt.py
no longer has a recall_episodic_few_shot() to drive), so `tenant` only ever
affects inject_procedural() here -- there used to be a mirror step for this
exact reason (a real false-positive it caught is preserved in that doc's
§4.3), but there's nothing left for it to keep in sync.

Wipes whatever's already under eval/procedural/<scope> first: this tenant
only ever holds a fresh mirror + the one candidate on top, never an
accumulating pile from past runs.

`stage()` is importable -- scripts/review_memory.py (P3's human review CLI)
calls it directly instead of shelling out, so one process can stage +
compare + review without a subprocess round trip.

Run standalone from the repository root:
    Windows PowerShell:
        .\.venv\Scripts\python.exe -m scripts.stage_candidate_for_eval --key pending-<uuid> --scope stt_exclusion_notify/check
    macOS / Bash:
        uv run python -m scripts.stage_candidate_for_eval --key pending-<uuid> --scope stt_exclusion_notify/check

Requires PostgreSQL, LiteLLM on port 4000, and `local-embed` (LiteLLM ->
Ollama/bge-m3) for the staged candidate write. It does not require STT,
notified, Agent Runtime, or event-driven workers. This command clears and
rewrites only the disposable `eval` tenant for the selected scope.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import uuid
from typing import Any

import psycopg

from persistence.call_log import current_node_name, current_thread_id
from persistence.memory import MemoryKind, build_namespace, parse_scope, recall, remember
from persistence.memory_lifespan import open_agent_memory
from persistence.memory_policy import MemoryPolicy

_POLICY_PATH = "mcp_servers/policy.yaml"
_PRINCIPAL = "memory_writer"  # already holds read "*" + */procedural|episodic/* write (mcp_servers/policy.yaml) -- covers eval's tenant segment
_EVAL_TENANT = "eval"
_WIPE_LIMIT = 200  # generous cap for "everything currently staged", not a real pagination need
_PROCEDURAL_MIRROR_LIMIT = 200  # same "effectively all of them" reasoning as scripts/distill_procedural.py's
"""_RULE_LIMIT: procedural is curated by a human reviewer (unlike episodic,
which accumulates automatically), so this is generous enough at any scale
this platform runs at today."""


async def _wipe(store: Any, kind: MemoryKind, scope: tuple[str, ...]) -> None:
    namespace = build_namespace(kind, _EVAL_TENANT, scope)
    existing = await store.asearch(namespace, limit=_WIPE_LIMIT)
    for item in existing:
        await store.adelete(namespace, item.key)
    if existing:
        print(f"[stage] cleared {len(existing)} stale {kind.value} item(s) under eval/{kind.value}/{'/'.join(scope)}")


async def stage(store: Any, policy: MemoryPolicy, key: str, scope: tuple[str, ...], source_tenant: str) -> dict[str, Any]:
    """Returns the staged candidate's full `value` dict (content/evidence/
    rationale/status/...) so a caller like scripts/review_memory.py can
    display it without a second store round trip."""
    # store.aget() bypasses recall()'s status="active" gate -- same
    # deliberate bypass persistence/memory_smoke_test.py's
    # scenario_remember_extra_fields uses to read back a pending item.
    source_namespace = build_namespace(MemoryKind.PROCEDURAL, source_tenant, scope)
    candidate = await store.aget(source_namespace, key)
    if candidate is None:
        raise ValueError(f"no item {key!r} under {source_tenant}/procedural/{'/'.join(scope)}")
    if candidate.value.get("status") != "pending":
        raise ValueError(f"{key!r} is status={candidate.value.get('status')!r}, not pending -- refusing to stage")

    # `eval` is one shared scratch namespace per scope, wiped and rewritten
    # by every stage() call -- a Postgres advisory lock (keyed by scope, so
    # unrelated scopes never block each other) serializes concurrent
    # stage()s on the same scope across processes/reviewers, so one wipe can
    # never delete another's in-flight comparison mid-run.
    lock_key = f"stage-eval:{'/'.join(scope)}"
    async with await psycopg.AsyncConnection.connect(os.environ["PERSISTENCE_DATABASE_URL"]) as lock_conn:
        await lock_conn.execute("SELECT pg_advisory_lock(hashtext(%s))", (lock_key,))
        try:
            await _wipe(store, MemoryKind.PROCEDURAL, scope)
            # Mirror source_tenant's currently-active rules first, so a
            # candidate is evaluated *stacked on top of* whatever's already
            # promoted -- the shape a real judge_exclusion(tenant="default")
            # call actually sees (inject_procedural() injects every active
            # rule up to its own limit, not just the newest one). Without
            # this, staging only the candidate itself would silently drop
            # every already-approved rule from the candidate run while
            # baseline keeps them, comparing "candidate alone" against
            # "existing rules alone" instead of "existing rules + candidate"
            # against "existing rules alone" (TODO.md's
            # stage-eval-single-rule-not-cumulative, now fixed here).
            active_rules = await recall(
                store, policy, MemoryKind.PROCEDURAL, tenant=source_tenant, scope=scope, limit=_PROCEDURAL_MIRROR_LIMIT
            )
            # recall() returns updated_at DESC (newest first); remember()
            # always stamps the write with "now", so writing in that same
            # order would make the *first*-written (source's newest) item
            # the *oldest* by updated_at in eval -- inverting relative
            # recency end to end. Writing oldest-source-item-first instead
            # means the source's newest item is written last and lands with
            # the newest eval timestamp, preserving the same relative order
            # a fresh recall(tenant=eval, ...) will see.
            for item in reversed(active_rules):
                await remember(
                    store, policy, MemoryKind.PROCEDURAL, tenant=_EVAL_TENANT, scope=scope,
                    key=item.key, content=item.value["content"],
                    # verbatim copy of content already embedded under source_tenant.
                    index=False,
                )
            if active_rules:
                print(f"[stage] mirrored {len(active_rules)} active procedural rule(s) from {source_tenant} into eval/procedural/{'/'.join(scope)}")

            await remember(
                store, policy, MemoryKind.PROCEDURAL, tenant=_EVAL_TENANT, scope=scope,
                key=key, content=candidate.value["content"],
            )
            print(f"[stage] staged {key!r} as active under eval/procedural/{'/'.join(scope)}: {candidate.value['content']!r}")
        finally:
            await lock_conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (lock_key,))

    return candidate.value


async def main(key: str, scope_arg: str, source_tenant: str) -> None:
    scope = parse_scope(scope_arg)

    current_thread_id.set(f"stage-eval-{uuid.uuid4()}")
    current_node_name.set(_PRINCIPAL)

    async with open_agent_memory(_POLICY_PATH) as (store, policy):
        await stage(store, policy, key, scope, source_tenant)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--key", required=True, help="pending candidate's key, e.g. pending-<uuid>")
    parser.add_argument("--scope", required=True, help="<workflow_name>/<step_name>, e.g. stt_exclusion_notify/check")
    parser.add_argument("--source-tenant", default="default")
    args = parser.parse_args()
    try:
        asyncio.run(main(args.key, args.scope, args.source_tenant))
    except ValueError as exc:
        raise SystemExit(f"[stage] {exc}")
