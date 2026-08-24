"""Shared lifespan helper for agents/<name>/server.py: opens the long-term
memory store + policy the same way every agent server needs to, so adding
memory to a new agent is one line instead of hand-copying the open/setup
sequence check/notified each wrote separately in M2
(docs/long-term-memory-plan.md M2.1).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.store.postgres.aio import AsyncPostgresStore

from persistence.memory import backfill_missing_status
from persistence.memory_policy import MemoryPolicy, load_memory_policy
from persistence.memory_store import get_memory_store


@asynccontextmanager
async def open_agent_memory(policy_path: str) -> AsyncIterator[tuple[AsyncPostgresStore, MemoryPolicy]]:
    """Long-lived for the caller's process, same reasoning as MCPGateway --
    AsyncPostgresStore must not be opened per-request. `policy_path` is
    mcp_servers/policy.yaml in every current caller, but this module doesn't
    hardcode that path -- same convention as mcp_servers.policy.load_policy.

    Runs backfill_missing_status() once per open -- idempotent and cheap at
    demo scale, so every caller gets a store where recall()/browse()'s
    status="active" filter can never silently hide a pre-existing row that
    predates the status field, without needing to remember to run
    scripts/backfill_memory_status.py by hand."""
    memory_policy = load_memory_policy(policy_path)
    async with get_memory_store() as store:
        await store.setup()
        await backfill_missing_status(store)
        yield store, memory_policy
