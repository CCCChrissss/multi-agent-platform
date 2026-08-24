"""Standalone HTTP wrapper for the check agent's judgment logic -- the
check agent's half of docs/agent-api-contract.md. See agents/lifespan.py
for the shared lifespan/gateway-ownership rationale.

Two scenarios now share this one server (docs/exclusion-scenario-plan.md
P5): which judgment function runs is decided once at startup from
`app.state.workflow_name` (itself derived from the WORKFLOW_DEF_PATH env
var agents/lifespan.py reads), not per-request -- a running process serves
exactly one workflow at a time, same as every other piece of
process-level config (ports, PERSISTENCE_DATABASE_URL, ...).
"""

from __future__ import annotations

from fastapi import FastAPI

from agents.envelope import AgentRequest, AgentResponse, run_handler
from agents.lifespan import make_lifespan
from llm.exclusion_judge import judge_exclusion
from llm.tsmc_judge import mentions_tsmc

_EXCLUSION_WORKFLOW_NAME = "stt_exclusion_notify"

# principal="check" (docs/exclusion-scenario-plan.md P2): mcp_servers/policy.yaml
# grants `check` the memory__browse_semantic_memory tool -- without this,
# MCPGateway.connect() has no MCP_CALLING_PRINCIPAL to pass to
# mcp_servers/memory/server.py's stdio subprocess, so its current_node_name
# would stay None and every browse()/recall() call from that subprocess
# would silently fail closed (see mcp_servers/memory/server.py's module
# docstring). llm/tsmc_judge.py doesn't call any memory MCP tool itself, so
# this was inert for it until llm/exclusion_judge.py (P4) started using it.
app = FastAPI(lifespan=make_lifespan("check", principal="check"))


@app.post("/run")
async def run(request: AgentRequest) -> AgentResponse:
    async def _handler(input: dict, context: dict) -> dict:
        transcript = (input.get("transcript") or "").strip()
        if app.state.workflow_name == _EXCLUSION_WORKFLOW_NAME:
            return await judge_exclusion(
                app.state.gateway,
                transcript,
                store=app.state.store,
                memory_policy=app.state.memory_policy,
                tenant=context.get("tenant_id", "default"),
            )
        mentions = await mentions_tsmc(
            app.state.gateway,
            transcript,
            store=app.state.store,
            memory_policy=app.state.memory_policy,
            tenant=context.get("tenant_id", "default"),
        )
        return {"mentions_tsmc": mentions}

    step = app.state.step
    return await run_handler("check", _handler, request, input_schema=step.input_schema, output_schema=step.output_schema)
