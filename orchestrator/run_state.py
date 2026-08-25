"""Master Agent's run-state tracking: the orchestrator_runs table.

One row per workflow execution (thread_id -- the same value as
call_log.thread_id and event_log/event_dispatch's thread_id, see
event_bus/base.py's Event docstring).

Deliberately not the LangGraph checkpointer: that models suspend/resume
*within one compiled graph's in-process execution*, whereas this only needs
"which step is this run on, and is it done" -- forcing that into a
StateGraph would tie the event-driven path back to the in-process execution
model it exists to move away from. See
docs/event-driven-multi-agent-coordination-plan.md.

This table remains the sole execution-control source of truth for the
event-driven path (every write below is a compare-and-swap guarded by
`WHERE status = 'running'`). That does not mean the checkpoints/
checkpoint_blobs tables stay sync-path-only, though: orchestrator/
master_agent.py mirrors every transition committed here into those same
tables via persistence/event_checkpoints.py's `record_step()`, using
LangGraph's checkpointer storage API directly (not `compile(checkpointer=)`,
still no StateGraph involved) so persistence/history.py's reader works for
both orchestration modes. That mirror is a derived, after-the-fact audit
projection of what this module already decided -- never the other way
around.
"""

from __future__ import annotations

import datetime as dt
import os
from typing import Any

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from orchestrator.workflow_def import STEPS_KEY
from persistence.pool import get_shared_pool

load_dotenv()

# Every async function below is a one-shot call from a long-running
# worker/master process; they all share persistence/pool.py's process-wide
# pool with event_bus/postgres.py instead of each opening their own against
# the same PERSISTENCE_DATABASE_URL.
_get_pool = get_shared_pool


_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS orchestrator_runs (
    thread_id TEXT PRIMARY KEY,
    workflow_name TEXT NOT NULL,
    current_step TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'needs_review', 'failed', 'completed')),
    state_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    step_deadline_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

_TERMINAL_STATUSES = ("needs_review", "failed", "completed")


def _database_url() -> str:
    return os.environ["PERSISTENCE_DATABASE_URL"]


def ensure_schema() -> None:
    with psycopg.connect(_database_url()) as conn:
        conn.execute(_CREATE_TABLE)


async def create_run(
    thread_id: str, workflow_name: str, first_step: str, *, step_deadline_at: dt.datetime, initial_state: dict | None = None
) -> None:
    # ON CONFLICT DO NOTHING makes this safe to retry: if a caller retries
    # start_run() after an earlier attempt's publish() failed (thread_id's
    # row already exists from that attempt), this becomes a no-op instead of
    # raising on the thread_id primary key, and the retry's publish() (using
    # the same deterministic event_id) can still get through.
    pool = await _get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO orchestrator_runs (thread_id, workflow_name, current_step, status, state_payload, step_deadline_at)
            VALUES (%s, %s, %s, 'running', %s, %s)
            ON CONFLICT (thread_id) DO NOTHING
            """,
            (thread_id, workflow_name, first_step, Jsonb(initial_state or {}), step_deadline_at),
        )


async def advance(thread_id: str, next_step: str, state_updates: dict, *, step_deadline_at: dt.datetime) -> bool:
    """Returns False (without writing anything) if the run is no longer
    'running' -- e.g. run_deadline_sweeper() (orchestrator/master_agent.py)
    already escalated it to 'needs_review' out from under a completion that
    was already in flight. Without this guard, a stale read-then-write from
    the completion path would silently clobber the sweeper's escalation
    (or vice versa) since both write the same row; the WHERE clause makes
    the two writers compare-and-swap against each other instead of one
    blindly overwriting whatever the other just did."""
    pool = await _get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            UPDATE orchestrator_runs
            SET current_step = %s, status = 'running',
                state_payload = state_payload || %s::jsonb,
                step_deadline_at = %s, updated_at = now()
            WHERE thread_id = %s AND status = 'running'
            """,
            (next_step, Jsonb(state_updates), step_deadline_at, thread_id),
        )
        return cur.rowcount > 0


async def mark_terminal(thread_id: str, status: str, state_updates: dict | None = None) -> bool:
    """Returns False (without writing anything) if the run is no longer
    'running' -- same compare-and-swap reasoning as advance() above."""
    if status not in _TERMINAL_STATUSES:
        raise ValueError(f"status must be one of {_TERMINAL_STATUSES}, got {status!r}")
    pool = await _get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            UPDATE orchestrator_runs
            SET status = %s, state_payload = state_payload || %s::jsonb, step_deadline_at = NULL, updated_at = now()
            WHERE thread_id = %s AND status = 'running'
            """,
            (status, Jsonb(state_updates or {}), thread_id),
        )
        return cur.rowcount > 0


async def sweep_expired_runs() -> list[dict[str, Any]]:
    """Escalate every 'running' run whose step_deadline_at has passed to
    'needs_review'. Catches a worker that's alive but silently stuck (no
    crash, no exception, no redelivery) -- event_bus's own per-message lease
    only re-dispatches after a worker process actually dies, so a hung-but-
    alive worker would otherwise leave the run in 'running' forever.

    A single UPDATE ... RETURNING, so it's safe to call repeatedly/from
    multiple processes: the WHERE clause only ever matches rows still
    'running' past their deadline, and Postgres's row locking means two
    concurrent sweeps can't both escalate the same row."""
    pool = await _get_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                UPDATE orchestrator_runs
                SET status = 'needs_review',
                    state_payload = state_payload || jsonb_build_object(
                        'review_reason',
                        'step ' || current_step || ' exceeded its deadline (' || step_deadline_at::text || ') without completing'
                    ),
                    step_deadline_at = NULL,
                    updated_at = now()
                WHERE status = 'running' AND step_deadline_at < now()
                RETURNING thread_id, workflow_name, current_step, state_payload
                """
            )
            return await cur.fetchall()


async def get_run(thread_id: str) -> dict[str, Any] | None:
    pool = await _get_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM orchestrator_runs WHERE thread_id = %s", (thread_id,))
            return await cur.fetchone()


def merge_state(run: dict[str, Any], step_output: dict) -> dict:
    """A step only returns the fields *it* produced (e.g. check_handler
    returns {"mentions_tsmc": ...} without re-forwarding "transcript"), so
    the full run state is always `run`'s persisted state unioned with what
    the step just produced -- equivalent to the jsonb `||` advance() does
    when it persists the same merge. orchestrator/master_agent.py and
    orchestrator/memory_writer.py both need this for the same `run` row on
    the same completion event."""
    return {**run["state_payload"], **step_output}


def record_step_output(run: dict[str, Any], step_name: str, output: dict) -> dict:
    """The full state-update dict for one step's successful ('ok') completion:
    its own output fields (flat merge, unchanged behavior) plus this run's
    STEPS_KEY map with `step_name`'s output attached -- the addressable
    per-step record workflow_def.py's resolve_step_input() resolves
    `steps.<name>.<field>` input_mapping entries against, kept separate from
    the flat merge so two steps producing a same-named field don't clobber
    each other under this key the way they already do (silently) in the flat
    namespace. Callers pass the result to both run_state.advance()/
    mark_terminal() and the checkpoint mirror, so both stay in sync."""
    steps_map = dict(run["state_payload"].get(STEPS_KEY) or {})
    steps_map[step_name] = output
    return {**output, STEPS_KEY: steps_map}
