"""Process-wide shared connection pool for the persistence DB.

event_bus/postgres.py and orchestrator/run_state.py both talk to the same
PERSISTENCE_DATABASE_URL but used to each open their own AsyncConnectionPool
purely because they're separate modules -- doubling the idle connection
floor for no reason. Routing both through get_shared_pool() collapses that
to one pool per process.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from psycopg_pool import AsyncConnectionPool

from persistence.asyncio_compat import configure_asyncio_for_psycopg

configure_asyncio_for_psycopg()
load_dotenv()

_shared: AsyncConnectionPool | None = None


def _database_url() -> str:
    return os.environ["PERSISTENCE_DATABASE_URL"]


async def get_shared_pool() -> AsyncConnectionPool:
    """Process 內共用的 persistence DB 連線池。event_bus 與 run_state 都走它，
    避免同一個 DB 開兩個池子各佔 min_size 條。"""
    global _shared
    if _shared is None:
        _shared = AsyncConnectionPool(_database_url(), open=False, min_size=1, max_size=4)
    if _shared.closed:
        await _shared.open()
    return _shared
