"""Thin HTTP client for agents/runtime.py's /check/run route
(docs/agent-api-contract.md). See agents/stt/client.py for the
envelope-translation rationale.
"""

from __future__ import annotations

from agents.envelope import run_request
from harness.agent_loop import AgentLoopIncomplete

BASE_URL = "http://localhost:8003/check"


async def mentions_tsmc(text: str, context: dict | None = None) -> bool:
    envelope = await run_request(BASE_URL, "check", "agents.runtime:app --port 8003", {"transcript": text}, context)
    if envelope.status == "ok":
        return envelope.output["mentions_tsmc"]
    if envelope.status == "needs_review":
        raise AgentLoopIncomplete(node="check", reason=envelope.review_reason or "")
    raise RuntimeError(envelope.error or "check agent returned an unspecified error")


async def judge_exclusion(text: str, context: dict | None = None) -> dict:
    """Counterpart to mentions_tsmc() for the stt_exclusion_notify workflow
    (docs/exclusion-scenario-plan.md P5) -- agents/runtime.py's own
    `app.state.workflow_name` decides which judgment function it runs, so
    the client-side split mirrors that rather than trying to unify two
    differently-shaped results behind one function."""
    envelope = await run_request(BASE_URL, "check", "agents.runtime:app --port 8003", {"transcript": text}, context)
    if envelope.status == "ok":
        return envelope.output
    if envelope.status == "needs_review":
        raise AgentLoopIncomplete(node="check", reason=envelope.review_reason or "")
    raise RuntimeError(envelope.error or "check agent returned an unspecified error")
