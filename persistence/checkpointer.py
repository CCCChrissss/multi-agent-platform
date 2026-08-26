"""Checkpointer backend for LangGraph workflows.

Any workflow that wants crash recovery / audit history compiles its graph
with the checkpointer this module provides. Backend is Postgres today;
swapping it (different instance, different store entirely) only touches
this file.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from persistence.asyncio_compat import configure_asyncio_for_psycopg

configure_asyncio_for_psycopg()
load_dotenv()


def get_checkpointer():
    """Async context manager yielding a ready-to-use checkpointer.

    Usage: `async with get_checkpointer() as checkpointer: ...`
    """
    # Not named DATABASE_URL: litellm's proxy server (gateway/) reserves that
    # name for its own Prisma-backed admin DB and crashes on startup if it's
    # set without the `prisma` package installed.
    database_url = os.environ["PERSISTENCE_DATABASE_URL"]
    return AsyncPostgresSaver.from_conn_string(database_url)
