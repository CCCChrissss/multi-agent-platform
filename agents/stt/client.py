"""Thin HTTP client for agents/runtime.py's /stt/run route
(docs/agent-api-contract.md). Mirrors services/notified/client.py's httpx +
ToolDependencyError conventions, translating the response envelope back into
the calling convention llm/stt_agent.py's transcribe() offered directly
(return the transcript, or raise) so workflows/event_driven_pipeline.py's
handler only needed a one-line swap.
"""

from __future__ import annotations

from agents.envelope import run_request
from harness.agent_loop import AgentLoopIncomplete

BASE_URL = "http://localhost:8003/stt"


async def transcribe(audio_path: str, context: dict | None = None) -> str:
    envelope = await run_request(BASE_URL, "stt", "agents.runtime:app --port 8003", {"audio_ref": audio_path}, context)
    if envelope.status == "ok":
        return envelope.output["transcript"]
    if envelope.status == "needs_review":
        raise AgentLoopIncomplete(node="stt", reason=envelope.review_reason or "")
    raise RuntimeError(envelope.error or "stt agent returned an unspecified error")
