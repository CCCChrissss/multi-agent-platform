"""Backend selection for EventBus.

Mirrors persistence/checkpointer.py's get_checkpointer(): one factory
function hides which backend is in use, so adding a new backend (e.g.
Kafka) only ever means a new module under event_bus/ plus one more branch
here -- nothing that calls get_event_bus() needs to change.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from dotenv import load_dotenv

from event_bus.base import EventBus

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

load_dotenv()


def get_event_bus(*, pool: AsyncConnectionPool | None = None) -> EventBus:
    backend = os.environ.get("EVENT_BUS_BACKEND", "postgres")
    if backend == "postgres":
        from event_bus.postgres import PostgresEventBus

        return PostgresEventBus(os.environ["PERSISTENCE_DATABASE_URL"], pool=pool)
    raise ValueError(f"unknown EVENT_BUS_BACKEND: {backend!r}")
