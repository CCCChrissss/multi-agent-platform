"""Shared FastAPI lifespan factory for agents/<name>/server.py -- each one
needs the same three things at startup: a long-lived MCPGateway (connecting
one spawns a stdio subprocess per configured MCP server, so it must happen
once, never per-request -- see mcp_servers/gateway.py), this step's
WorkflowDef (for input/output schema validation in run_handler()), and the
long-term memory store (persistence/memory_lifespan.py). Building this once
here instead of copy-pasting it into every server.py means a fix to the
teardown ordering (gateway.close() after the memory context exits) only
has to happen in one place.

Which workflow's YAML to load is resolved by
orchestrator.workflow_def.resolve_workflow_def_path() (the WORKFLOW_DEF_PATH
env var, default stt_check_notify.yaml, so every existing caller is
unaffected). docs/exclusion-scenario-plan.md P5: two demo scenarios now
share the same three agent servers/ports, so switching which one a running
stack serves is a process-startup choice (set the env var before starting
honcho), not a per-request one -- workflows/event_driven_pipeline.py calls
the same resolver, so the agent-server layer and the orchestrator layer
never disagree about which workflow is live.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from mcp_servers.gateway import MCPGateway
from mcp_servers.policy import load_policy
from orchestrator.workflow_def import load_workflow_def, resolve_workflow_def_path
from persistence.memory_lifespan import open_agent_memory

_REPO_ROOT = Path(__file__).resolve().parents[1]
_POLICY_PATH = _REPO_ROOT / "mcp_servers" / "policy.yaml"


def make_lifespan(step_name: str, *, principal: str | None = None) -> Callable[[FastAPI], AsyncIterator[None]]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        gateway = MCPGateway(load_policy(str(_POLICY_PATH)), principal=principal)
        await gateway.connect()
        app.state.gateway = gateway
        workflow_def = load_workflow_def(resolve_workflow_def_path())
        app.state.step = workflow_def.step(step_name)
        app.state.workflow_name = workflow_def.name
        async with open_agent_memory(str(_POLICY_PATH)) as (store, memory_policy):
            app.state.store = store
            app.state.memory_policy = memory_policy
            try:
                yield
            finally:
                await gateway.close()

    return lifespan
