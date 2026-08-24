"""Standalone HTTP wrapper for llm/stt_agent.py's transcribe() -- the stt
agent's half of docs/agent-api-contract.md. See agents/lifespan.py for the
shared lifespan/gateway-ownership rationale.
"""

from __future__ import annotations

from fastapi import FastAPI

from agents.envelope import AgentRequest, AgentResponse, run_handler
from agents.lifespan import make_lifespan
from llm.stt_agent import transcribe

# No `memory:` grant exists for `stt` in policy.yaml yet (no known
# kind/scope to read -- docs/long-term-memory-plan.md M2.1/M2.2) --
# store/policy are wired, and llm/stt_agent.py does call recall() for
# procedural/episodic (M2.2's generic component), but with no grant it
# always resolves to "nothing found". A future memory need is a
# policy.yaml grant away, not a redeploy-the-wiring change.
app = FastAPI(lifespan=make_lifespan("stt"))


@app.post("/run")
async def run(request: AgentRequest) -> AgentResponse:
    async def _handler(input: dict, context: dict) -> dict:
        transcript = await transcribe(
            app.state.gateway,
            input["audio_ref"],
            store=app.state.store,
            memory_policy=app.state.memory_policy,
            tenant=context.get("tenant_id", "default"),
        )
        return {"transcript": transcript}

    step = app.state.step
    return await run_handler("stt", _handler, request, input_schema=step.input_schema, output_schema=step.output_schema)
