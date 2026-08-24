"""Audit helper: dump every checkpoint recorded for a given thread_id.

Works against raw checkpoint tuples (not a compiled graph), so it's
reusable across any workflow that shares the checkpointer in
persistence/checkpointer.py.

Usage: uv run python -m persistence.history <thread_id>
"""

from __future__ import annotations

import asyncio
import sys

from langchain_core.runnables import RunnableConfig

from persistence.call_log import fetch_calls
from persistence.checkpointer import get_checkpointer


async def print_history(thread_id: str) -> None:
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    async with get_checkpointer() as checkpointer:
        checkpoints = [c async for c in checkpointer.alist(config)]
        print("=== checkpoints (state after each step) ===")
        for ckpt in reversed(checkpoints):
            step = ckpt.metadata.get("step")
            source = ckpt.metadata.get("source")
            print(f"--- step={step} source={source} ---")
            print(ckpt.checkpoint["channel_values"])

    print("\n=== call log (LLM/tool calls inside each node) ===")
    try:
        calls = await fetch_calls(thread_id)
    except Exception as exc:
        print(f"(couldn't read call_log: {exc})")
        return
    if not calls:
        print("(no calls recorded)")
    for call in calls:
        print(
            f"--- {call['created_at']} node={call['node']} kind={call['kind']} name={call['name']} "
            f"error={call['is_error']} denied={call['denied']} tool_call_id={call['tool_call_id']} "
            f"response_model={call['response_model']} latency_ms={call['latency_ms']} ---"
        )
        print(f"request: {call['request']}")
        print(f"response: {call['response']}")


if __name__ == "__main__":
    asyncio.run(print_history(sys.argv[1]))
