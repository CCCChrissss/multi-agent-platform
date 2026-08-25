"""Single-process runtime serving every agent (docs/generic-agent-runtime-plan.md
P3/P6) -- replaces the three standalone agents/{stt,check,notified}/server.py
processes. `agents/lifespan.py` builds one MCPGateway per agent (a gateway's
principal is fixed at construction, so one gateway can't serve
stt/check/notified's three different identities) plus one shared
long-term-memory store, all under one FastAPI app.

Route paths carry the agent identity (`/stt/run`, `/check/run`,
`/notified/run`) rather than the request body -- consistent with
docs/agent-api-contract.md's existing design ("node/principal 身分不需要放進
request envelope...身分是由「呼叫的是哪個 agent」決定"), which already assumed
identity comes from which endpoint you call, not payload content.

P6: a step is served generically via harness/generic_agent.py::run_generic_step()
unless it's listed in `_CUSTOM_HANDLERS` below -- that dict is how a step
*opts out* of the zero-code path (because it needs a §8-style verifier:
`check`'s citation-check/alias-backstop retry loops, `notified`'s
should_notify-consistency post-check), not how one opts in. A brand new thin
agent (model + tools + prompt + output.from, no custom verification) needs no
entry here.

docs/ui-backend-integration-plan.md P1: there is now exactly one route,
`POST /{step_name}/run`, instead of a route registered per step at import
time. Same URL shape, so agents/*/client.py's BASE_URLs are unchanged -- but
which step names resolve is a question answered per request against
agents/live_spec.py rather than frozen when this module was imported. That's
what lets an agent created through the UI (P2) be callable without restarting
this process.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import FastAPI, HTTPException

from agents.envelope import AgentRequest, AgentResponse, run_handler
from agents.lifespan import AgentContext, get_agent, make_runtime_lifespan
from harness.generic_agent import run_generic_step
from llm.exclusion_judge import judge_exclusion
from llm.notify_agent import decide_and_notify
from llm.tsmc_judge import mentions_tsmc
from orchestrator.workflow_def import render_prompt

_EXCLUSION_WORKFLOW_NAME = "stt_exclusion_notify"

Handler = Callable[[FastAPI, AgentContext, dict, dict], Awaitable[dict]]

app = FastAPI(lifespan=make_runtime_lifespan())


async def _check_handler(app: FastAPI, ctx: AgentContext, input: dict, context: dict) -> dict:
    transcript = (input.get("transcript") or "").strip()
    system_prompt, user_prompt = render_prompt(ctx.step, input)
    common = dict(
        model=ctx.step.model,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        store=app.state.store,
        memory_policy=app.state.live_spec.memory_policy,
        tenant=context.get("tenant_id", "default"),
    )
    if ctx.workflow_name == _EXCLUSION_WORKFLOW_NAME:
        return await judge_exclusion(ctx.gateway, transcript, **common)
    mentions = await mentions_tsmc(ctx.gateway, transcript, **common)
    return {"mentions_tsmc": mentions}


async def _notified_handler(app: FastAPI, ctx: AgentContext, input: dict, context: dict) -> dict:
    system_prompt, user_prompt = render_prompt(ctx.step, input)
    log = await decide_and_notify(
        ctx.gateway,
        should_notify=bool(input.get("should_notify")),
        subject=input.get("subject") or "",
        body=input.get("body") or "",
        model=ctx.step.model,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        store=app.state.store,
        memory_policy=app.state.live_spec.memory_policy,
        tenant=context.get("tenant_id", "default"),
        # No dedicated recipient identity exists in this demo's input
        # schema yet -- context.user_id (the run's cross-run identity
        # dimension) stands in for "whose notification preference this
        # is" until a real recipient concept exists. Demo-scenario
        # simplification, not a platform decision.
        recipient_id=context.get("user_id", "default"),
        workflow_name=ctx.workflow_name,
    )
    return {"notified_log": log}


async def _generic_handler(app: FastAPI, ctx: AgentContext, input: dict, context: dict) -> dict:
    return await run_generic_step(
        ctx.step,
        ctx.gateway,
        input,
        workflow_name=ctx.workflow_name,
        store=app.state.store,
        memory_policy=app.state.live_spec.memory_policy,
        tenant=context.get("tenant_id", "default"),
    )


_CUSTOM_HANDLERS: dict[str, Handler] = {
    "check": _check_handler,
    "notified": _notified_handler,
}


@app.post("/{step_name}/run", response_model=AgentResponse)
async def run_step(step_name: str, request: AgentRequest) -> AgentResponse:
    try:
        ctx = await get_agent(app, step_name)
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail=(
                f"no agent named {step_name!r}; known agents: {app.state.live_spec.step_names()}"
                + (f" (last spec reload failed: {app.state.live_spec.last_error})" if app.state.live_spec.last_error else "")
            ),
        ) from None

    handler = _CUSTOM_HANDLERS.get(step_name, _generic_handler)

    async def _bound(input: dict, context: dict) -> dict:
        return await handler(app, ctx, input, context)

    return await run_handler(
        step_name, _bound, request, input_schema=ctx.step.input_schema, output_schema=ctx.step.output_schema
    )
