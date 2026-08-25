"""Thin HTTP client for agents/runtime.py's /notified/run route
(docs/agent-api-contract.md). See agents/stt/client.py for the
envelope-translation rationale.
"""

from __future__ import annotations

from agents.envelope import run_request
from harness.agent_loop import AgentLoopIncomplete

BASE_URL = "http://localhost:8003/notified"


async def decide_and_notify(should_notify: bool, subject: str, body: str, context: dict | None = None) -> list[str]:
    envelope = await run_request(
        BASE_URL,
        "notified",
        "agents.runtime:app --port 8003",
        {"should_notify": should_notify, "subject": subject, "body": body},
        context,
    )
    if envelope.status == "ok":
        return envelope.output["notified_log"]
    if envelope.status == "needs_review":
        raise AgentLoopIncomplete(node="notified", reason=envelope.review_reason or "")
    raise RuntimeError(envelope.error or "notified agent returned an unspecified error")
