"""Repository-owned PostgreSQL readiness check and idempotent bootstrap.

This module deliberately uses psycopg from the project environment instead of
requiring ``psql`` on PATH.  It never prints credentials and never drops or
recreates an existing database.
"""
from __future__ import annotations

import argparse
import asyncio
import os
from dataclasses import dataclass
from typing import Callable

import psycopg
from dotenv import load_dotenv
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from persistence.asyncio_compat import configure_asyncio_for_psycopg


REQUIRED_TABLES = (
    "call_log",
    "checkpoint_blobs",
    "checkpoint_writes",
    "checkpoints",
    "event_dispatch",
    "event_log",
    "orchestrator_runs",
    "store",
    "store_vectors",
)


@dataclass(frozen=True)
class DatabaseTarget:
    conninfo: str
    admin_conninfo: str
    host: str
    port: str
    database: str


def parse_target(database_url: str) -> DatabaseTarget:
    """Parse a PostgreSQL URL/conninfo without exposing its password."""
    if not database_url:
        raise ValueError("PERSISTENCE_DATABASE_URL is missing")
    params = conninfo_to_dict(database_url)
    database = params.get("dbname")
    if not database:
        raise ValueError("PERSISTENCE_DATABASE_URL must include a database name")
    admin_params = dict(params)
    admin_params["dbname"] = "postgres"
    return DatabaseTarget(
        conninfo=make_conninfo(**params),
        admin_conninfo=make_conninfo(**admin_params),
        host=params.get("host", "localhost"),
        port=params.get("port", "5432"),
        database=database,
    )


def _database_exists(conn: psycopg.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone() is not None


def ensure_database(
    target: DatabaseTarget,
    *,
    initialize: bool,
    connect: Callable[..., psycopg.Connection] = psycopg.connect,
) -> bool:
    """Return whether the target DB exists after the optional creation."""
    with connect(target.admin_conninfo, autocommit=True, connect_timeout=5) as conn:
        if _database_exists(conn, target.database):
            print(f"[OK] Database already exists: {target.database}")
            return True
        if not initialize:
            print(f"[MISSING] Database does not exist: {target.database}")
            return False
        try:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target.database)))
        except psycopg.errors.DuplicateDatabase:
            # Another bootstrap may have won the race after the SELECT.
            pass
        print(f"[CREATED] Database: {target.database}")
        return True


def ensure_vector(
    target: DatabaseTarget,
    *,
    initialize: bool,
    connect: Callable[..., psycopg.Connection] = psycopg.connect,
) -> bool:
    """Check pgvector availability and optionally enable it in the target DB."""
    with connect(target.conninfo, autocommit=True, connect_timeout=5) as conn:
        available = conn.execute(
            "SELECT default_version FROM pg_available_extensions WHERE name = 'vector'"
        ).fetchone()
        if available is None:
            print("[MISSING] pgvector is not installed for this PostgreSQL server")
            print("[HINT] Install pgvector for the running PostgreSQL version; see docs/windows-setup.md.")
            return False
        installed = conn.execute(
            "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
        ).fetchone()
        if installed is not None:
            print(f"[OK] Extension enabled: vector {installed[0]}")
            return True
        if not initialize:
            print(f"[MISSING] Extension is available but not enabled: vector {available[0]}")
            return False
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        installed = conn.execute(
            "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
        ).fetchone()
        if installed is None:
            print("[ERROR] CREATE EXTENSION completed but vector is still absent")
            return False
        print(f"[CREATED] Extension: vector {installed[0]}")
        return True


async def initialize_project_schema(database_url: str) -> None:
    """Run the existing idempotent schema owners; do not duplicate their SQL."""
    from psycopg_pool import AsyncConnectionPool

    from event_bus.factory import get_event_bus
    from orchestrator import run_state
    from persistence.call_log import ensure_schema as ensure_call_log_schema
    from persistence.checkpointer import get_checkpointer
    from persistence.memory_store import get_memory_store

    ensure_call_log_schema()
    run_state.ensure_schema()

    pool = AsyncConnectionPool(database_url, open=False, min_size=1, max_size=2)
    await pool.open()
    try:
        await get_event_bus(pool=pool).ensure_schema()
    finally:
        await pool.close()

    async with get_checkpointer() as checkpointer:
        await checkpointer.setup()
    async with get_memory_store() as store:
        await store.setup()


def check_tables(
    target: DatabaseTarget,
    *,
    connect: Callable[..., psycopg.Connection] = psycopg.connect,
) -> bool:
    with connect(target.conninfo, connect_timeout=5) as conn:
        rows = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        ).fetchall()
    existing = {row[0] for row in rows}
    missing = sorted(set(REQUIRED_TABLES) - existing)
    if missing:
        print("[MISSING] Project tables: " + ", ".join(missing))
        return False
    print(f"[OK] Project schema contains {len(REQUIRED_TABLES)} required tables")
    return True


def run(action: str) -> int:
    load_dotenv()
    try:
        target = parse_target(os.environ.get("PERSISTENCE_DATABASE_URL", ""))
        print(f"[INFO] Target: host={target.host} port={target.port} database={target.database}")
        initialize = action == "init"
        if not ensure_database(target, initialize=initialize):
            return 1
        if not ensure_vector(target, initialize=initialize):
            return 1
        if initialize:
            asyncio.run(initialize_project_schema(target.conninfo))
            print("[OK] Project schemas initialized")
        if not check_tables(target):
            return 1
        print(f"[OK] Database {action} completed")
        return 0
    except psycopg.errors.InsufficientPrivilege as exc:
        print(f"[ERROR] PostgreSQL permission denied: {exc}")
        print("[HINT] Use a database role with CREATEDB permission for db-init.")
        return 1
    except psycopg.OperationalError as exc:
        message = str(exc).replace(os.environ.get("PERSISTENCE_DATABASE_URL", ""), "<database URL hidden>")
        print(f"[ERROR] PostgreSQL connection failed: {message}")
        print("[HINT] Start PostgreSQL, then verify the host, port, user and password in .env.")
        return 1
    except (KeyError, ValueError) as exc:
        print(f"[ERROR] Invalid database configuration: {exc}")
        return 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "init"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_asyncio_for_psycopg()
    return run(args.action)


if __name__ == "__main__":
    raise SystemExit(main())
