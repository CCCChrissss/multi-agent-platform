"""Shared FastAPI lifespan factory for agents/runtime.py
(docs/generic-agent-runtime-plan.md P3) -- the single process serving every
agent needs the same things at startup: one long-lived MCPGateway per agent
(connecting one spawns a stdio subprocess per configured MCP server, so it
must happen once, never per-request -- see mcp_servers/gateway.py), each
agent's own StepDef (for input/output schema validation in run_handler()),
and the long-term memory store (persistence/memory_lifespan.py). Building
this once here instead of copy-pasting it into every route means a fix to
the teardown ordering (every gateway.close() after the memory context
exits) only has to happen in one place.

Which workflow's YAML to load is resolved by
orchestrator.workflow_def.resolve_workflow_def_path() (the WORKFLOW_DEF_PATH
env var, default stt_check_notify.yaml, so every existing caller is
unaffected). docs/exclusion-scenario-plan.md P5: two demo scenarios now
share the same runtime process, so switching which one a running stack
serves is a process-startup choice (set the env var before starting
honcho), not a per-request one -- workflows/event_driven_pipeline.py calls
the same resolver, so the agent-runtime layer and the orchestrator layer
never disagree about which workflow is live.

docs/ui-backend-integration-plan.md P1 loosened two of those "once at
startup" facts, because the UI now edits the same files mid-session:

  - specs and policy come from agents/live_spec.py, which re-reads them when
    their mtime changes, so a model/prompt/grant edit takes effect on the
    next request instead of the next restart;
  - gateways are created on demand, so an agent that didn't exist when this
    process started still gets one. The main workflow's steps are still
    connected eagerly at startup so the first real run doesn't pay for it.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI

from agents.live_spec import LiveSpec
from mcp_servers.gateway import MCPGateway
from orchestrator.workflow_def import StepDef
from persistence.memory_lifespan import open_agent_memory

_REPO_ROOT = Path(__file__).resolve().parents[1]
_POLICY_PATH = _REPO_ROOT / "mcp_servers" / "policy.yaml"


@dataclass(frozen=True)
class AgentContext:
    """One agent's slice of the runtime's shared state: its own MCPGateway
    (principal fixed at construction -- see MCPGateway.__init__'s docstring,
    a gateway instance can't serve more than one identity for its memory MCP
    subprocess), its current StepDef, and the workflow that step was declared
    in. Built per request from the live spec rather than held for the process
    lifetime, so an edited spec is picked up without a restart -- only the
    gateway inside it is long-lived and cached."""

    gateway: MCPGateway
    step: StepDef
    workflow_name: str


async def get_agent(app: FastAPI, step_name: str) -> AgentContext:
    """This step's current context, reloading the spec first if any source
    file changed. Raises KeyError for an unknown step (agents/runtime.py
    turns that into a 404).

    A gateway is created on first use and kept: connect() spawns one stdio
    subprocess per declared MCP server, so it must not happen per request.
    The lock is only around creation -- two concurrent first-requests for the
    same new agent would otherwise each spawn a full set of subprocesses and
    one of them would leak.
    """
    live: LiveSpec = app.state.live_spec
    if live.refresh():
        for gateway in app.state.gateways.values():
            gateway.set_policy(live.policy)
    spec = live.get(step_name)

    gateway = app.state.gateways.get(step_name)
    if gateway is None:
        async with app.state.gateway_lock:
            gateway = app.state.gateways.get(step_name)
            if gateway is None:
                print(f"[lifespan] connecting gateway for new agent {step_name!r}", file=sys.stderr, flush=True)
                gateway = MCPGateway(live.policy, principal=step_name)
                await gateway.connect()
                app.state.gateways[step_name] = gateway

    return AgentContext(gateway=gateway, step=spec.step, workflow_name=spec.workflow_name)


def make_runtime_lifespan() -> Callable[[FastAPI], AsyncIterator[None]]:
    """One MCPGateway per agent -- docs/generic-agent-runtime-plan.md P3
    weighed this against threading `principal` through every memory MCP call
    instead (one shared gateway); chose this because it's a smaller,
    lower-risk diff that doesn't touch the memory access-control boundary.

    Worth knowing when reading get_agent(): tool permissions are resolved from
    `current_node_name` per call, not from `gateway._principal`, so the split
    is load-bearing only for the memory MCP subprocess, which learns who is
    calling from an env var fixed at connect() time. An agent with no memory
    grants would work through any gateway -- it still gets its own, so that
    adding a memory grant later doesn't silently start attributing its reads
    to a different principal.

    The memory store, unlike the gateway, isn't principal-scoped
    (persistence/memory.py's recall()/browse() read the calling principal from
    current_node_name per call, not from the store object) -- opened once
    here, shared by every agent."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        live = LiveSpec(policy_path=_POLICY_PATH)
        live.refresh()  # first load is fatal on failure: there's no previous good spec to fall back to
        app.state.live_spec = live
        app.state.gateways = {}
        app.state.gateway_lock = asyncio.Lock()

        # Eager for the steps this process was started to serve, lazy for
        # everything else (see get_agent). MCP's stdio transport owns an
        # AnyIO cancel scope that must be closed by the same asyncio task that
        # opened it, so keep eager connect()/close() in this lifespan task.
        eager = [name for name, spec in live.snapshot.steps.items() if spec.source == live.main_workflow_path]
        gateways = {name: MCPGateway(live.policy, principal=name) for name in eager}
        try:
            for name, gateway in gateways.items():
                try:
                    await gateway.connect()
                except BaseException:
                    await gateway.close()
                    raise
                app.state.gateways[name] = gateway

            # open_agent_memory() also returns a MemoryPolicy, ignored here:
            # it would be a second, frozen-at-startup copy of what LiveSpec
            # already reloads. Handlers read
            # app.state.live_spec.memory_policy instead. The store itself is
            # what this context manager is needed for (setup() + the one-time
            # status backfill).
            async with open_agent_memory(str(_POLICY_PATH)) as (store, _startup_memory_policy):
                app.state.store = store
                yield
        finally:
            for gateway in reversed(tuple(app.state.gateways.values())):
                await gateway.close()

    return lifespan
