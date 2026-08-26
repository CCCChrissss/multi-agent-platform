"""Call-level logging for every LLM and tool call.

gateway/client.py (all LLM calls) and mcp_servers/gateway.py (all tool
calls) both funnel through this module, so any current or future
workflow/node gets fine-grained audit logs for free without adding
instrumentation to the node itself.

Correlates to a workflow run via `current_thread_id`, and to the node that
triggered the call via `current_node_name` -- both contextvars set once
(per run / per node) in the workflow (see workflows/simple_pipeline.py).
They propagate through the async call stack automatically, so callers never
need to pass thread_id/node_name down explicitly.

A logging failure (e.g. Postgres is down) must never break the actual
LLM/tool call, so both log functions swallow their own errors.
"""

from __future__ import annotations

import os
from contextvars import ContextVar
from typing import Any

import psycopg
from dotenv import load_dotenv
from psycopg.types.json import Jsonb

from persistence.asyncio_compat import configure_asyncio_for_psycopg

configure_asyncio_for_psycopg()
load_dotenv()

current_thread_id: ContextVar[str | None] = ContextVar("current_thread_id", default=None)
current_node_name: ContextVar[str | None] = ContextVar("current_node_name", default=None)

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS call_log (
    id BIGSERIAL PRIMARY KEY,
    thread_id TEXT,
    node TEXT,
    kind TEXT NOT NULL CHECK (kind IN ('llm', 'tool')),
    name TEXT NOT NULL,
    request JSONB NOT NULL,
    response JSONB,
    is_error BOOLEAN NOT NULL DEFAULT FALSE,
    denied BOOLEAN NOT NULL DEFAULT FALSE,
    tool_call_id TEXT,
    response_model TEXT,
    latency_ms INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""
# call_log predates the `node` column; add it for DBs created before this change.
_ADD_NODE_COLUMN = "ALTER TABLE call_log ADD COLUMN IF NOT EXISTS node TEXT;"
# call_log briefly had a separate 'stt' kind for calls through gateway/client.py's
# transcribe(); folded back into 'llm' since both go through the same ungated
# LiteLLM Gateway call path (gateway/client.py), unlike 'tool', which is
# policy/RBAC-gated (see mcp_servers/gateway.py). `name` (e.g. 'breeze-asr' vs
# 'local-qwen') already tells audio calls apart from chat calls. Just the
# UPDATE -- naturally idempotent (no-op once no 'stt' rows are left) -- the
# CHECK constraint itself is owned solely by _ADD_MEMORY_KIND below (the
# migration that runs last), not re-asserted here too: this statement used
# to also rebuild the constraint down to ('llm', 'tool'), which broke
# ensure_schema() on every subsequent run once any 'memory' row existed --
# it kept narrowing the constraint right before _ADD_MEMORY_KIND widened it
# back, a CheckViolation against the rows already sitting between the two.
_MERGE_STT_INTO_LLM_KIND = "UPDATE call_log SET kind = 'llm' WHERE kind = 'stt';"
# call_log predates the `denied` column; add it for DBs created before this change.
# Kept separate from is_error: a denied call never reached the tool/model, while
# is_error also covers calls that reached it and failed there -- audits need to
# tell "blocked by policy" apart from "attempted and errored".
_ADD_DENIED_COLUMN = "ALTER TABLE call_log ADD COLUMN IF NOT EXISTS denied BOOLEAN NOT NULL DEFAULT FALSE;"
# call_log predates these two columns; add them for DBs created before this change.
# tool_call_id links a tool-kind row back to the exact tool_calls[] entry (and
# therefore the exact LLM row) that spawned it -- a tool_call_id is unique per
# conversation, so this is enough to reconstruct decision -> effect causality
# without a separate turn counter.
# response_model records the model that actually answered (from the LiteLLM
# response), which can differ from the requested `name` when a fallback
# (see gateway/config.yaml's `fallbacks`) kicks in.
_ADD_TOOL_CALL_ID_COLUMN = "ALTER TABLE call_log ADD COLUMN IF NOT EXISTS tool_call_id TEXT;"
_ADD_RESPONSE_MODEL_COLUMN = "ALTER TABLE call_log ADD COLUMN IF NOT EXISTS response_model TEXT;"
# call_log predates the 'memory' kind -- persistence/memory.py's recall()/
# browse()/remember() now log through here too, closing the audit gap
# relative to 'llm'/'tool' (see TODO.md's memory-access-audit-log entry).
_ADD_MEMORY_KIND = """
ALTER TABLE call_log DROP CONSTRAINT IF EXISTS call_log_kind_check;
ALTER TABLE call_log ADD CONSTRAINT call_log_kind_check CHECK (kind IN ('llm', 'tool', 'memory'));
"""

_INSERT = """
INSERT INTO call_log (
    thread_id, node, kind, name, request, response, is_error, denied, tool_call_id, response_model, latency_ms
)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
"""


def _database_url() -> str:
    return os.environ["PERSISTENCE_DATABASE_URL"]


def ensure_schema() -> None:
    with psycopg.connect(_database_url()) as conn:
        conn.execute(_CREATE_TABLE)
        conn.execute(_ADD_NODE_COLUMN)
        conn.execute(_MERGE_STT_INTO_LLM_KIND)
        conn.execute(_ADD_DENIED_COLUMN)
        conn.execute(_ADD_TOOL_CALL_ID_COLUMN)
        conn.execute(_ADD_RESPONSE_MODEL_COLUMN)
        conn.execute(_ADD_MEMORY_KIND)


def log_call_sync(
    kind: str,
    name: str,
    request: Any,
    response: Any,
    is_error: bool,
    latency_ms: int,
    denied: bool = False,
    tool_call_id: str | None = None,
    response_model: str | None = None,
) -> None:
    try:
        with psycopg.connect(_database_url()) as conn:
            conn.execute(
                _INSERT,
                (
                    current_thread_id.get(),
                    current_node_name.get(),
                    kind,
                    name,
                    Jsonb(request),
                    Jsonb(response),
                    is_error,
                    denied,
                    tool_call_id,
                    response_model,
                    latency_ms,
                ),
            )
    except Exception as exc:
        print(f"[call_log] failed to record {kind} call '{name}': {exc}", flush=True)


async def log_call(
    kind: str,
    name: str,
    request: Any,
    response: Any,
    is_error: bool,
    latency_ms: int,
    denied: bool = False,
    tool_call_id: str | None = None,
    response_model: str | None = None,
    thread_id: str | None = None,
) -> None:
    """`thread_id` overrides current_thread_id.get() when given -- the escape
    hatch persistence/memory.py's recall()/browse() need
    (docs/generic-agent-runtime-plan.md P7): when they run inside
    mcp_servers/memory/server.py's stdio subprocess, current_thread_id was
    never set there (that ContextVar is per-process, can't cross the stdio
    boundary), so the caller passes the value explicitly instead. Every other
    caller (gateway/client.py, mcp_servers/gateway.py's non-memory tool
    calls) leaves this None and keeps reading the ContextVar exactly as
    before."""
    try:
        async with await psycopg.AsyncConnection.connect(_database_url()) as conn:
            await conn.execute(
                _INSERT,
                (
                    thread_id if thread_id is not None else current_thread_id.get(),
                    current_node_name.get(),
                    kind,
                    name,
                    Jsonb(request),
                    Jsonb(response),
                    is_error,
                    denied,
                    tool_call_id,
                    response_model,
                    latency_ms,
                ),
            )
    except Exception as exc:
        print(f"[call_log] failed to record {kind} call '{name}': {exc}", flush=True)


async def fetch_calls(thread_id: str) -> list[dict]:
    """All logged LLM/tool calls for a thread_id, oldest first."""
    async with await psycopg.AsyncConnection.connect(_database_url()) as conn:
        async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            await cur.execute(
                "SELECT node, kind, name, request, response, is_error, denied, tool_call_id, response_model, "
                "latency_ms, created_at "
                "FROM call_log WHERE thread_id = %s ORDER BY created_at",
                (thread_id,),
            )
            return await cur.fetchall()
