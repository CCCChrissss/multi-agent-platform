"""M6 validation milestone: proves the event-driven mode (event_bus/,
orchestrator/) reproduces the original synchronous pipeline's
(workflows/simple_pipeline.py, never modified by this effort -- checked
below rather than trusted) result and call_log shape for the same input.
Run with:
    uv run python -m workflows.parity_check

Requires the local stack (`uv run honcho start`) to be running.

Caveat: this drives a real LLM, so a turn count can occasionally differ
between two independent runs of identical code (e.g. the model deciding to
take one more/fewer tool-calling turn) -- a call_log shape mismatch here is
worth a rerun before assuming a real regression.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
import uuid
from pathlib import Path

from langchain_core.runnables import RunnableConfig

from event_bus.factory import get_event_bus
from mcp_servers.gateway import MCPGateway
from mcp_servers.policy import load_policy
from orchestrator import master_agent, run_state
from orchestrator.master_agent import run_master
from orchestrator.worker import run_worker
from orchestrator.workflow_def import load_workflow_def
from persistence.call_log import current_thread_id, ensure_schema as ensure_call_log_schema, fetch_calls
from persistence.checkpointer import get_checkpointer
from workflows.event_driven_pipeline import build_step_handlers
from workflows.simple_pipeline import build_graph

_REPO_ROOT = Path(__file__).resolve().parents[1]
_WORKFLOW_DEF_PATH = _REPO_ROOT / "workflows" / "definitions" / "stt_check_notify.yaml"
_POLICY_PATH = _REPO_ROOT / "mcp_servers" / "policy.yaml"
_AUDIO_REF = "samples/gen_tsmc_01.wav"
_TIMEOUT_SECONDS = 180


def _assert_simple_pipeline_untouched() -> None:
    result = subprocess.run(
        ["git", "diff", "--stat", "main", "--", "workflows/simple_pipeline.py"],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    if result.stdout.strip():
        raise AssertionError(f"workflows/simple_pipeline.py differs from main -- it must stay untouched:\n{result.stdout}")
    print("[parity] confirmed workflows/simple_pipeline.py is unchanged from main", flush=True)


async def _run_sync_pipeline(gateway: MCPGateway, thread_id: str, checkpointer) -> dict:
    # workflows/simple_pipeline.py's main() sets this before invoking the
    # graph (its node functions only set current_node_name); calling
    # build_graph()/ainvoke() directly, bypassing main(), means we must set
    # it ourselves or every call_log row lands under thread_id=NULL instead.
    current_thread_id.set(thread_id)
    graph = build_graph(gateway, checkpointer)
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    initial_state = {
        "audio_ref": _AUDIO_REF,
        "transcript": None,
        "mentions_tsmc": None,
        "status": "pending",
        "needs_review": False,
        "review_reason": None,
    }
    return await graph.ainvoke(initial_state, config=config)


async def _run_event_driven_pipeline(bus, thread_id: str, checkpointer) -> dict:
    workflow_def = load_workflow_def(str(_WORKFLOW_DEF_PATH))
    handlers = build_step_handlers(workflow_def)
    workers = [
        asyncio.create_task(run_worker(bus, workflow_def, step.name, handlers[step.name], worker_id=f"parity-{step.name}"))
        for step in workflow_def.steps
    ]
    master_task = asyncio.create_task(run_master(bus, workflow_def, worker_id="parity-master", checkpointer=checkpointer))
    try:
        await master_agent.start_run(bus, workflow_def, thread_id, {"audio_ref": _AUDIO_REF})
        return await _wait_for_terminal_run(thread_id, timeout=_TIMEOUT_SECONDS)
    finally:
        for t in (*workers, master_task):
            t.cancel()
        for t in (*workers, master_task):
            try:
                await t
            except asyncio.CancelledError:
                pass


async def _wait_for_terminal_run(thread_id: str, timeout: float, poll_interval: float = 1.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        run = await run_state.get_run(thread_id)
        if run and run["status"] != "running":
            return run
        if time.monotonic() > deadline:
            raise TimeoutError(f"run {thread_id} did not reach a terminal state within {timeout}s (last seen: {run})")
        await asyncio.sleep(poll_interval)


def _call_shape(rows: list[dict]) -> list[tuple]:
    """(node, kind, name) per call_log row, in order. Deliberately excludes
    thread_id, timestamps, and latency -- everything that's expected to
    differ between two independent runs regardless of orchestration mode."""
    return [(r["node"], r["kind"], r["name"]) for r in rows]


async def _checkpoint_shape(checkpointer, thread_id: str) -> list[frozenset]:
    """Set of channel_values keys per checkpoint, oldest first. Deliberately
    excludes checkpoint ids/timestamps and the exact accumulated values
    (transcript text will legitimately differ between two independent runs)
    -- only the *shape* (how many super-steps, which fields exist by which
    step) is expected to match between the two orchestration modes."""
    config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    checkpoints = [c async for c in checkpointer.alist(config)]
    return [frozenset(c.checkpoint["channel_values"]) for c in reversed(checkpoints)]


async def main() -> None:
    _assert_simple_pipeline_untouched()

    ensure_call_log_schema()
    run_state.ensure_schema()
    bus = get_event_bus()
    await bus.ensure_schema()

    policy = load_policy(str(_POLICY_PATH))
    sync_thread_id = str(uuid.uuid4())
    event_driven_thread_id = str(uuid.uuid4())

    async with MCPGateway(policy) as gateway, get_checkpointer() as checkpointer:
        await checkpointer.setup()

        print(f"[parity] running synchronous pipeline, thread_id={sync_thread_id}", flush=True)
        sync_result = await _run_sync_pipeline(gateway, sync_thread_id, checkpointer)
        sync_calls = await fetch_calls(sync_thread_id)

        print(f"[parity] running event-driven pipeline, thread_id={event_driven_thread_id}", flush=True)
        event_driven_result = await _run_event_driven_pipeline(bus, event_driven_thread_id, checkpointer)
        event_driven_calls = await fetch_calls(event_driven_thread_id)

        sync_checkpoint_shape = await _checkpoint_shape(checkpointer, sync_thread_id)
        ed_checkpoint_shape = await _checkpoint_shape(checkpointer, event_driven_thread_id)

    assert not sync_result.get("needs_review"), f"sync pipeline unexpectedly needed review: {sync_result}"
    assert event_driven_result["status"] == "completed", event_driven_result
    ed_payload = event_driven_result["state_payload"]

    assert sync_result["transcript"] == ed_payload["transcript"], (sync_result["transcript"], ed_payload["transcript"])
    print(f"[parity] transcript matches: {sync_result['transcript']!r}")

    assert sync_result["mentions_tsmc"] == ed_payload["mentions_tsmc"], (sync_result["mentions_tsmc"], ed_payload["mentions_tsmc"])
    print(f"[parity] mentions_tsmc matches: {sync_result['mentions_tsmc']!r}")

    sync_shape = _call_shape(sync_calls)
    ed_shape = _call_shape(event_driven_calls)
    assert sync_shape == ed_shape, f"call_log shape differs:\n  sync:         {sync_shape}\n  event-driven: {ed_shape}"
    print(f"[parity] call_log shape matches across {len(sync_shape)} calls: {sync_shape}")

    # Not asserted equal: PipelineState (workflows/simple_pipeline.py) and the
    # event-driven path's accumulated state_payload are deliberately different
    # schemas (PipelineState always carries status/needs_review/review_reason;
    # state_payload only carries whatever fields workflow_def.yaml's steps
    # declare), so their channel keys were never going to match -- this is
    # informational evidence that both wrote *some* trail into the same
    # checkpoints/checkpoint_blobs tables, not a shape-equality claim.
    print(f"[parity] sync checkpoint shape ({len(sync_checkpoint_shape)} checkpoint(s)): {sync_checkpoint_shape}")
    print(f"[parity] event-driven checkpoint shape ({len(ed_checkpoint_shape)} checkpoint(s)): {ed_checkpoint_shape}")
    assert sync_checkpoint_shape and ed_checkpoint_shape, (
        f"expected both pipelines to leave at least one checkpoint: sync={sync_checkpoint_shape}, event-driven={ed_checkpoint_shape}"
    )

    print("\nM6 parity check passed: event-driven mode reproduces the synchronous pipeline's result and call_log shape, and both leave a checkpoint trail.")


if __name__ == "__main__":
    asyncio.run(main())
