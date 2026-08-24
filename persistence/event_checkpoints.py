"""Mirrors orchestrator_runs transitions into the same checkpoint tables
LangGraph's checkpointer uses for the synchronous path (persistence/checkpointer.py),
so persistence/history.py's existing reader works unmodified for both orchestration
modes -- see docs/event-driven-multi-agent-coordination-plan.md's audit/observability
section for why this exists instead of a separate event-log-based view.

This is NOT a resumable LangGraph checkpoint: there's no compiled graph behind it.
orchestrator/run_state.py's `orchestrator_runs` stays the sole authority for
execution control on the event-driven path (compare-and-swap via
`WHERE status = 'running'`); record_step() below is only called *after* a
run_state.advance()/mark_terminal() call has already won that compare-and-swap, and
just mirrors the transition it already committed into checkpoints/checkpoint_blobs
for audit symmetry with the sync path.

checkpoint_writes is deliberately never populated here -- that table holds a
compiled graph's in-flight pending writes between super-steps, which the
event-driven path has no equivalent of; its crash recovery is event_bus's
lease/redelivery (event_bus/postgres.py), a different mechanism entirely. Leaving
it empty for these rows is honest, not an omission.

A write failure here must never fail the run it's mirroring -- same swallow-your-
own-errors contract as persistence/call_log.py's log_call().
"""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.base import Checkpoint, CheckpointMetadata, empty_checkpoint


async def record_step(
    checkpointer: Any,
    *,
    thread_id: str,
    step_name: str,
    state_payload: dict[str, Any],
) -> None:
    """Append one checkpoint row for a transition orchestrator_runs already committed.

    Callers must only invoke this after the corresponding run_state.advance()/
    mark_terminal() call returned True (i.e. this process actually won the
    compare-and-swap) -- skipping it on a lost race is what keeps a redelivered
    completion from creating a duplicate or out-of-order checkpoint.
    """
    try:
        config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
        parent = await checkpointer.aget_tuple(config)
        step_index = (parent.metadata.get("step", -1) + 1) if parent is not None else 0
        if parent is not None:
            config["configurable"]["checkpoint_id"] = parent.checkpoint["id"]

        checkpoint: Checkpoint = empty_checkpoint()
        checkpoint["channel_values"] = dict(state_payload)
        # One version tag per channel, all set to this checkpoint's own id: real
        # LangGraph version strings track per-channel history for routing, which
        # doesn't exist here -- we only need blob rows written by different
        # checkpoints to never collide on (thread_id, checkpoint_ns, channel,
        # version), and checkpoint ids (uuid6, monotonic) already guarantee that.
        checkpoint["channel_versions"] = {k: checkpoint["id"] for k in state_payload}

        metadata: CheckpointMetadata = {"source": "loop", "step": step_index}
        await checkpointer.aput(config, checkpoint, metadata, checkpoint["channel_versions"])
    except Exception as exc:  # noqa: BLE001 -- audit mirror must never break the run it's mirroring
        print(
            f"[event_checkpoints] failed to record checkpoint for thread_id={thread_id!r} step={step_name!r}: {exc}",
            flush=True,
        )
