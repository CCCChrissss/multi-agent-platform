"""Standalone CLI for persistence/memory.py::backfill_missing_status() --
docs/knowledge-distillation-plan.md §5 P0. persistence/memory_lifespan.py's
open_agent_memory() now runs this same backfill automatically on every open,
so this script is only needed to backfill a store out-of-band (e.g. before
any agent process has opened it since a migration) or to check the patched
count directly.

Bypasses persistence/memory_policy.py on purpose -- this is an
infrastructure migration over the whole store, not a scenario read/write
through some principal's grant (mirrors persistence/memory_smoke_test.py's
_cleanup(), which also talks to `store` directly for the same reason).

Run with:
    uv run python -m scripts.backfill_memory_status
"""

from __future__ import annotations

import asyncio

from persistence.memory import backfill_missing_status
from persistence.memory_store import get_memory_store


async def main() -> None:
    async with get_memory_store() as store:
        await store.setup()
        patched = await backfill_missing_status(store)
        print(f"[backfill] patched {patched} memory row(s) missing status")


if __name__ == "__main__":
    asyncio.run(main())
