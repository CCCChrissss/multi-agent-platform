"""Background distiller (docs/long-term-memory-plan.md M3): subscribes to a
workflow's completion events and writes long-term memory per each finished
step's declared `memory_write` rules (orchestrator/workflow_def.py's
StepDef.memory_write) -- same "pure interpreter" split as
orchestrator/master_agent.py: no step name or content shape is hardcoded
here, only read from the WorkflowDef passed in.

Runs as its own consumer_group (`{workflow_name}.memory_writer`) on the same
events_topic() master_agent listens to, so it sees every completion
independently of what master_agent does with it -- adding this distiller
never touches master_agent.py or orchestrator/worker.py.

Only a `status: "ok"` completion can be distilled: `needs_review`/`error`
completions have no confirmed-correct output, and this platform has no
human-decision entry point yet to supply one (see TODO.md's
needs-review-decision-entry) -- so those are skipped, not guessed at.

Every episodic write lands `status="pending"` (docs/knowledge-distillation-
plan.md P5): recall()/browse() only ever surface `status="active"` memory, so
a freshly-distilled case is invisible to every agent-facing prompt (no more
few-shot leak of an unreviewed judgment) and to scripts/distill_procedural.py
(it also reads through recall(), so it only generalizes from episodic a human
has already reviewed via scripts/review_episodic.py) until a human promotes
it.

`subscribe(..., start_from="now")`: this consumer_group is adopted on a topic
that may already have history (every completion event ever published for
this workflow). Without "now" it would replay that entire history the first
time it's ever started -- event_bus/base.py's `subscribe` docstring names
this exact distiller as the motivating case for that parameter.
"""

from __future__ import annotations

import asyncio
import json

from event_bus.base import Event, EventBus, events_topic
from orchestrator import run_state
from orchestrator.workflow_def import MemoryWriteRule, StepDef, WorkflowDef
from persistence.call_log import current_node_name, current_thread_id
from persistence.memory import MemoryKind, build_namespace, remember
from persistence.memory_policy import MemoryPolicy

_PRINCIPAL = "memory_writer"


def _memory_writer_group(workflow_def: WorkflowDef) -> str:
    return f"{workflow_def.name}.memory_writer"


async def run_memory_writer(bus: EventBus, workflow_def: WorkflowDef, store, memory_policy: MemoryPolicy, *, worker_id: str) -> None:
    """Runs forever: distills one completion event at a time."""
    events = events_topic(workflow_def.name)
    group = _memory_writer_group(workflow_def)
    async with bus.subscribe(events, group, worker_id=worker_id, start_from="now") as deliveries:
        async for delivery in deliveries:
            await _distill(workflow_def, store, memory_policy, delivery.event)
            await delivery.ack()


async def _distill(workflow_def: WorkflowDef, store, memory_policy: MemoryPolicy, completion: Event) -> None:
    finished_step = next((s for s in workflow_def.steps if s.completion_type == completion.event_type), None)
    if finished_step is None or not finished_step.memory_write:
        return
    if completion.payload.get("status") != "ok":
        return  # no confirmed-correct output to distill -- see module docstring

    run = await run_state.get_run(completion.thread_id)
    if run is None:
        return

    business_output = completion.payload["output"]
    merged_state = run_state.merge_state(run, business_output)

    current_thread_id.set(completion.thread_id)
    current_node_name.set(_PRINCIPAL)
    # Rules are independent writes (each its own memory kind/content) -- no
    # ordering dependency between them, so apply them concurrently instead of
    # paying N sequential DB round trips per completion event.
    await asyncio.gather(
        *(
            _apply_rule(store, memory_policy, workflow_def, finished_step, rule, merged_state, business_output, completion.event_id)
            for rule in finished_step.memory_write
        )
    )


async def _apply_rule(
    store,
    memory_policy: MemoryPolicy,
    workflow_def: WorkflowDef,
    step: StepDef,
    rule: MemoryWriteRule,
    merged_state: dict,
    business_output: dict,
    event_id: str,
) -> None:
    content = {
        "input": str(merged_state.get(rule.input_field, "")),
        "output": json.dumps({f: business_output.get(f) for f in rule.output_fields}, ensure_ascii=False),
    }
    # event_id is deterministic, so a redelivered completion recomputes the
    # same key -- but if a human already reviewed that key via
    # scripts/review_episodic.py (status flipped away from "pending",
    # possibly with edited content), the redelivery must not silently
    # overwrite that decision back to a fresh pending row.
    namespace = build_namespace(MemoryKind.EPISODIC, rule.tenant, (workflow_def.name, step.name))
    existing = await store.aget(namespace, event_id)
    if existing is not None and existing.value.get("status") != "pending":
        return
    await remember(
        store,
        memory_policy,
        MemoryKind.EPISODIC,  # the only kind MemoryWriteRule supports -- see its docstring
        tenant=rule.tenant,
        scope=(workflow_def.name, step.name),
        # completion.event_id is deterministic per (thread_id, topic,
        # event_type) -- a redelivered completion recomputes the same key,
        # so aput()'s upsert makes this idempotent for free instead of
        # needing separate dedup bookkeeping.
        key=event_id,
        content=content,
        status="pending",  # see module docstring -- awaits scripts/review_episodic.py
    )
