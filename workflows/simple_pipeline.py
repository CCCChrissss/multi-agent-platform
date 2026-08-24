"""Simplest possible LangGraph pipeline, no Kafka: stt -> check -> notified.

stt, check, and notified are all MCP tool-calling agents sharing one
MCPGateway. Each node's `principal` identity is its own node name -- the
gateway reads it off `current_node_name` (set below) rather than having it
passed in, so it can never drift from the node that's actually running. The
gateway's policy (mcp_servers/policy.yaml) decides which tools that
principal may use -- stt gets the format-check + transcribe tools (the
latter backed by the real Breeze-ASR-25 model, see services/stt/server.py)
and decides for itself whether to check the format before transcribing.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path
from typing import TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph

from harness.agent_loop import AgentLoopIncomplete
from llm.stt_agent import transcribe as stt_agent_transcribe
from llm.tsmc_judge import mentions_tsmc as llm_mentions_tsmc
from mcp_servers.gateway import MCPGateway
from mcp_servers.notified.agent import decide_and_notify
from mcp_servers.policy import load_policy
from persistence.call_log import current_node_name, current_thread_id, ensure_schema as ensure_call_log_schema
from persistence.checkpointer import get_checkpointer


class PipelineState(TypedDict):
    audio_ref: str
    transcript: str | None
    mentions_tsmc: bool | None
    status: str
    needs_review: bool
    review_reason: str | None


def _route(state: PipelineState) -> str:
    return "review" if state.get("needs_review") else "continue"


def build_graph(gateway: MCPGateway, checkpointer):
    # Setting current_node_name is what makes this node's identity; the
    # gateway's policy (mcp_servers/policy.yaml) maps that same name (as
    # `principal`) to its allowed tools -- see mcp_servers/gateway.py.
    async def stt_node(state: PipelineState) -> dict:
        current_node_name.set("stt")
        try:
            transcript = await stt_agent_transcribe(gateway, state["audio_ref"])
        except AgentLoopIncomplete as exc:
            print(f"[stt] needs review: {exc.reason}", flush=True)
            return {"needs_review": True, "review_reason": str(exc), "status": "needs_review"}
        print(f"[stt] transcribed '{state['audio_ref']}'", flush=True)
        print(transcript)
        return {"transcript": transcript, "status": "transcribed"}

    # ponytail: check_node/notified_node never open persistence.memory_lifespan
    # or pass store/memory_policy/tenant/recipient_id -- this synchronous demo
    # pipeline intentionally skips memory/tenant scoping, add when this path
    # needs it too.
    async def check_node(state: PipelineState) -> dict:
        current_node_name.set("check")
        transcript = (state["transcript"] or "").strip()
        try:
            mentions_tsmc = await llm_mentions_tsmc(gateway, transcript)
        except AgentLoopIncomplete as exc:
            print(f"[check] needs review: {exc.reason}", flush=True)
            return {"needs_review": True, "review_reason": str(exc), "status": "needs_review"}
        print(f"[check] mentions_tsmc={mentions_tsmc}", flush=True)
        return {"mentions_tsmc": mentions_tsmc, "status": "checked"}

    async def notified_node(state: PipelineState) -> dict:
        current_node_name.set("notified")
        try:
            results = await decide_and_notify(gateway, state["transcript"] or "", bool(state["mentions_tsmc"]))
        except AgentLoopIncomplete as exc:
            print(f"[notified] needs review: {exc.reason}", flush=True)
            return {"needs_review": True, "review_reason": str(exc), "status": "needs_review"}
        for result in results:
            print(f"[notified] {result}", flush=True)
        return {"status": "sent"}

    graph = StateGraph(PipelineState)
    graph.add_node("stt", stt_node)
    graph.add_node("check", check_node)
    graph.add_node("notified", notified_node)

    graph.set_entry_point("stt")
    graph.add_conditional_edges("stt", _route, {"continue": "check", "review": END})
    graph.add_conditional_edges("check", _route, {"continue": "notified", "review": END})
    graph.add_edge("notified", END)

    return graph.compile(checkpointer=checkpointer)


async def main() -> None:
    thread_id = sys.argv[1] if len(sys.argv) > 1 else str(uuid.uuid4())
    print(f"[main] thread_id={thread_id}", flush=True)
    current_thread_id.set(thread_id)
    ensure_call_log_schema()

    gateway = MCPGateway(load_policy(str(Path(__file__).resolve().parents[1] / "mcp_servers" / "policy.yaml")))
    async with gateway, get_checkpointer() as checkpointer:
        await checkpointer.setup()
        graph = build_graph(gateway, checkpointer)
        config: RunnableConfig = {"configurable": {"thread_id": thread_id}}

        existing_state = await graph.aget_state(config)
        if existing_state.values and existing_state.next:
            print(f"[main] resuming from {existing_state.next}", flush=True)
            result = await graph.ainvoke(None, config=config)
        else:
            initial_state: PipelineState = {
                "audio_ref": "samples/gen_tsmc_01.wav",
                "transcript": None,
                "mentions_tsmc": None,
                "status": "pending",
                "needs_review": False,
                "review_reason": None,
            }
            result = await graph.ainvoke(initial_state, config=config)

        print("=== result ===", flush=True)
        print(result, flush=True)
        if result.get("needs_review"):
            print(f"[main] NEEDS REVIEW: {result.get('review_reason')}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
