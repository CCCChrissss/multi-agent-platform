"""Long-term memory store backend.

Parallel to persistence/checkpointer.py, but for LangGraph's BaseStore --
cross-run, cross-thread memory instead of a single execution's state
snapshots (see docs/long-term-memory-plan.md for the distinction). Swapping
the backend only touches this file.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from langgraph.store.postgres.aio import AsyncPostgresStore

from gateway.client import aembed
from persistence.asyncio_compat import configure_asyncio_for_psycopg

configure_asyncio_for_psycopg()
load_dotenv()

EMBED_MODEL = "local-embed"
EMBED_DIMS = 1024


async def _embed(texts: list[str]) -> list[list[float]]:
    return await aembed(EMBED_MODEL, texts)


def get_memory_store():
    """Async context manager yielding a ready-to-use long-term memory store.

    Usage: `async with get_memory_store() as store: await store.setup(); ...`

    `index` turns on semantic recall (docs/long-term-memory-plan.md M4):
    `store.asearch(..., query=...)` now ranks by embedding similarity on the
    `content` field instead of degrading to an equality filter. `fields=
    ["content"]` works uniformly across all three MemoryKinds even though
    their content shapes differ (procedural's {"rule"}, episodic's
    {"input","output"}, semantic's per-subject-type shape) -- langgraph's
    get_text_at_path() json.dumps()s a dict field wholesale when the path
    doesn't drill into a scalar, so there's no per-kind branching here.
    Embedding goes through gateway/client.py's aembed() -- the same LiteLLM
    Gateway every other model call uses -- so swapping the embedding
    provider only touches gateway/config.yaml (AGENTS.md's "AI 基礎建設要
    素件化、可替換").
    """
    database_url = os.environ["PERSISTENCE_DATABASE_URL"]
    index = {"dims": EMBED_DIMS, "embed": _embed, "fields": ["content"]}
    # Without pool_config this falls back to a single non-pooled connection
    # for the store's whole lifetime, serializing every concurrent
    # recall()/remember() in the process onto it. Same min/max_size as
    # persistence/pool.py's shared pool for the other persistence traffic.
    pool_config = {"min_size": 1, "max_size": 4}
    return AsyncPostgresStore.from_conn_string(database_url, index=index, pool_config=pool_config)
