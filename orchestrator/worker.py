"""Generic event-driven worker loop: claim a command, run the step's
handler, publish the completion event, ack.

One `run_worker()` call is dedicated to exactly one workflow step -- it
subscribes under a `{workflow_name}.{step_name}` consumer_group (namespaced
so two workflows sharing a step name can't claim each other's dispatch
rows, since event_bus's `_CLAIM` query scopes only by consumer_group, not
topic -- see event_bus/postgres.py), and sets `current_node_name` to the
bare `step_name` while running the handler. That equivalence is what lets
mcp_servers/gateway.py's RBAC keep working completely unmodified once
execution moves out of the single LangGraph process: the gateway reads
`current_node_name` as the calling principal regardless of which process set
it (see mcp_servers/gateway.py's docstring).

`handler` is the one piece of scenario-specific code a caller supplies (see
workflows/event_driven_pipeline.py's STEP_HANDLERS) -- it receives the
command payload's business fields and returns the step's business output
fields. Everything else here is workflow-agnostic.

Completion payload shape (docs/agent-api-contract.md's response envelope):
  {"status": "ok", "output": {...}}                    -- handler succeeded
  {"status": "needs_review", "review_reason": "..."}    -- see below
  {"status": "error", "error": "..."}                   -- see below
`output` is nested under its own key rather than merged flat alongside
"status" so a handler-returned field can never collide with (and silently
clobber) this loop's own bookkeeping key, regardless of what the handler
happens to name its fields.

Failure mapping:
  - AgentLoopIncomplete (the step ran but couldn't reach a confident,
    verified conclusion) -> a "needs_review" completion event, acked. This
    is a legitimate business outcome, not a delivery failure -- the message
    doesn't need redelivery, the run just needs a human.
  - A handler result that doesn't match the step's declared output_schema
    (StepDef.validate_output) is a contract violation, not a business
    outcome -> falls through to the same "error" handling as any other
    unexpected exception below.
  - Any other exception -> an "error" completion event, acked, same
    reasoning: the process is still alive and already knows this attempt
    failed, so it reports that rather than staying silent.
  - An actual process crash (kill -9, OOM) never reaches either branch --
    the dispatch row stays 'claimed' and is reclaimed after its lease
    expires, exactly like a crashed sync-mode run resuming from its
    checkpoint. Retry-then-give-up policy for transient errors is left to
    a later milestone (M5) rather than guessed at here.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from event_bus.base import Event, EventBus, commands_topic, deterministic_event_id, events_topic
from harness.agent_loop import AgentLoopIncomplete
from orchestrator.workflow_def import StepDef, WorkflowDef
from persistence.call_log import current_node_name, current_thread_id

Handler = Callable[[dict], Awaitable[dict]]


async def run_worker(bus: EventBus, workflow_def: WorkflowDef, step_name: str, handler: Handler, *, worker_id: str) -> None:
    """Runs forever, processing one command at a time for `step_name`."""
    step = workflow_def.step(step_name)
    commands = commands_topic(workflow_def.name, step_name)
    events = events_topic(workflow_def.name)
    group = f"{workflow_def.name}.{step_name}"

    async with bus.subscribe(commands, group, worker_id=worker_id) as deliveries:
        async for delivery in deliveries:
            if delivery.event.event_type != step.command_type:
                await delivery.nack(f"expected event_type {step.command_type!r} on {step_name}'s command stream, got {delivery.event.event_type!r}")
                continue
            await _handle_one(bus, events, step, handler, delivery.event)
            await delivery.ack()


async def _handle_one(bus: EventBus, events_topic_name: str, step: StepDef, handler: Handler, command: Event) -> None:
    current_thread_id.set(command.thread_id)
    current_node_name.set(step.name)
    try:
        step.validate_input(command.payload)
        result = await handler(command.payload)
        step.validate_output(result)
        completion_payload = {"status": "ok", "output": result}
    except AgentLoopIncomplete as exc:
        completion_payload = {"status": "needs_review", "review_reason": str(exc)}
    except Exception as exc:  # noqa: BLE001 -- deliberately broad, see module docstring
        completion_payload = {"status": "error", "error": repr(exc)}

    await bus.publish(
        Event(
            event_id=deterministic_event_id(command.thread_id, events_topic_name, step.completion_type),
            thread_id=command.thread_id,
            topic=events_topic_name,
            event_type=step.completion_type,
            payload=completion_payload,
        )
    )
