"""Standalone HTTP wrapper for llm/notify_agent.py's decide_and_notify() --
the notified agent's half of docs/agent-api-contract.md. See
agents/lifespan.py for the shared lifespan/gateway-ownership rationale.
"""

from __future__ import annotations

from fastapi import FastAPI

from agents.envelope import AgentRequest, AgentResponse, run_handler
from agents.lifespan import make_lifespan
from llm.notify_agent import decide_and_notify

app = FastAPI(lifespan=make_lifespan("notified", principal="notified"))


@app.post("/run")
async def run(request: AgentRequest) -> AgentResponse:
    async def _handler(input: dict, context: dict) -> dict:
        log = await decide_and_notify(
            app.state.gateway,
            should_notify=bool(input.get("should_notify")),
            subject=input.get("subject") or "",
            body=input.get("body") or "",
            store=app.state.store,
            memory_policy=app.state.memory_policy,
            tenant=context.get("tenant_id", "default"),
            # No dedicated recipient identity exists in this demo's input
            # schema yet -- context.user_id (the run's cross-run identity
            # dimension) stands in for "whose notification preference this
            # is" until a real recipient concept exists. Demo-scenario
            # simplification, not a platform decision.
            recipient_id=context.get("user_id", "default"),
            workflow_name=app.state.workflow_name,
        )
        return {"notified_log": log}

    step = app.state.step
    return await run_handler(
        "notified", _handler, request, input_schema=step.input_schema, output_schema=step.output_schema
    )
