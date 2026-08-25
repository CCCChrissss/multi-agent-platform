"""Smoke test for demo/api.py's memory-review endpoints (S8 of
docs/distill-ui-plan.md) -- run with:
    uv run python -m demo.distill_api_smoke_test

Calls the route handler functions directly -- @app.get()/@app.post() just
register a function into the router, they don't wrap or replace it, so
demo.api.get_pending()/approve_candidate() are the exact functions GET
/memory/pending and POST /memory/candidate/{key}/approve run. Only
app.state.store is set (opened the same way scripts/*.py's CLIs do) --
skips demo.api's real lifespan, which also spawns one MCP subprocess per
mcp_servers/policy.yaml server just to build the tool catalog, irrelevant to
what this test checks. No LLM, no evaluation (S1's CLI-output-unchanged
acceptance check already covers compare()'s correctness) -- just the
pending-queue read + approve write path §1.4 of that doc says needs no new
storage logic.

Everything this test writes is prefixed `smoke-distill-` under a scope no
real workflow uses, and deleted in a `finally`.

Requires the local stack's Postgres (`uv run honcho start`, or just Postgres
on its own -- this test never touches the LLM gateway or any MCP server).
"""

from __future__ import annotations

import asyncio
import uuid

from demo import api
from persistence.call_log import current_node_name, current_thread_id
from persistence.memory import MemoryKind, forget, parse_scope, recall, remember
from persistence.memory_lifespan import open_agent_memory

_SCOPE_ARG = "smoke_distill_workflow/smoke_distill_step"
_SCOPE = parse_scope(_SCOPE_ARG)
_KEY = "pending-smoke-distill-test"
_PRINCIPAL = "memory_writer"  # same principal scripts/review_memory.py's CLI uses -- read "*" + write */procedural/*


async def _cleanup(store, policy) -> None:
    current_node_name.set(_PRINCIPAL)
    await forget(store, policy, MemoryKind.PROCEDURAL, tenant="default", scope=_SCOPE, key=_KEY)


async def main() -> None:
    async with open_agent_memory("mcp_servers/policy.yaml") as (store, policy):
        api.app.state.store = store
        current_thread_id.set(f"smoke-distill-{uuid.uuid4()}")
        current_node_name.set(_PRINCIPAL)
        await _cleanup(store, policy)  # in case a previous failed run left it dangling
        try:
            await remember(
                store, policy, MemoryKind.PROCEDURAL, tenant="default", scope=_SCOPE,
                key=_KEY, content={"rule": "smoke test 假規則"}, status="pending",
                extra={"evidence": [], "rationale": "smoke test"},
            )
            print("[distill_api] OK -- seeded a fake pending candidate")

            pending = await api.get_pending(scope=_SCOPE_ARG, kind="procedural")
            assert any(item["key"] == _KEY for item in pending), pending
            print("[distill_api] OK -- GET /memory/pending sees the seeded candidate")

            result = await api.approve_candidate(_KEY, api.CandidateApprove(scope=_SCOPE_ARG, rule=None))
            assert result["status"] == "approved", result
            print("[distill_api] OK -- POST /memory/candidate/{key}/approve flips it")

            active = await recall(store, policy, MemoryKind.PROCEDURAL, tenant="default", scope=_SCOPE, limit=10)
            assert any(item.key == _KEY for item in active), active
            print("[distill_api] OK -- recall() reads it back as active -- status really flipped, not just the response")

            still_pending = await api.get_pending(scope=_SCOPE_ARG, kind="procedural")
            assert not any(item["key"] == _KEY for item in still_pending), still_pending
            print("[distill_api] OK -- it's gone from the pending queue")
        finally:
            await _cleanup(store, policy)

    print("\nAll distill_api smoke tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
