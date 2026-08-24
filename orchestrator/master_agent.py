"""Master Agent: a pure interpreter over a WorkflowDef.

Turns an external trigger into the first command dispatch (`start_run`),
and turns each completion event into either the next step's command or a
terminal run state (`run_master`). Step names, command/completion event
types, and step order all come from the WorkflowDef passed in -- nothing
here is scenario-specific (docs/harness-engineering-principles.md checklist
item 9). Whatever calls `start_run` (a file watcher, a webhook, a cron --
all future work) owns the scenario-specific initial payload; this module
never inspects it.

Consumer group naming: `run_master`/`run_worker` (orchestrator/worker.py)
namespace their consumer_group by `workflow_def.name` rather than using a
bare "master"/step-name literal, because event_bus's `_CLAIM` query (see
event_bus/postgres.py) only scopes by consumer_group, not by topic --
without the workflow prefix, two different workflows sharing a step name
(or every workflow's master loop sharing the literal "master") would
silently claim and process each other's dispatch rows once a second
workflow exists on this platform.

Idempotency: every event this module publishes uses
`event_bus.base.deterministic_event_id(thread_id, topic, event_type)`
rather than a random id, so a redelivered/retried publish collapses into
the original `event_log` row via `UNIQUE(event_id)` instead of creating a
second, undeduped one -- see docs/event-driven-multi-agent-coordination-plan.md.
`_handle_completion` also refuses to reprocess a completion for a step the
run isn't currently on (or a run that's already terminal): combined with
publishing the next command *before* persisting the advance, this makes
redelivery of the same completion (e.g. this loop crashing, or `ack()`
itself failing, after a first pass already went through) a safe retry
instead of double-dispatching or double-terminating the run.

Step timeout: each dispatched command gets a fixed
DEFAULT_STEP_TIMEOUT_SECONDS deadline recorded on the run, well above
event_bus's own per-message lease (which only protects against a literally
crashed worker process, not "this step is legitimately still running").
`run_deadline_sweeper()` runs alongside `run_master()` (both started
together in workflows/event_driven_pipeline.py's `--role master`) and
periodically escalates any run whose deadline has passed into
`needs_review` via `run_state.sweep_expired_runs()` -- this is what catches
a worker that goes quiet without crashing (no exception, no redelivery),
which would otherwise leave a run stuck in 'running' forever.

Checkpoint mirroring: every `run_state.advance()`/`mark_terminal()` call in
`_handle_completion` that actually wins its compare-and-swap is immediately
followed by `persistence/event_checkpoints.py`'s `record_step()`, which
mirrors the same transition into the checkpoints/checkpoint_blobs tables
LangGraph's checkpointer uses for the synchronous path -- see that module's
docstring for why this is an audit mirror, not a resumable checkpoint.
`orchestrator_runs` remains the sole execution-control source of truth;
mirroring only ever happens after it, never before or instead of it.
"""

from __future__ import annotations

import asyncio
import datetime as dt

from event_bus.base import Event, EventBus, commands_topic, deterministic_event_id, events_topic
from orchestrator import run_state
from orchestrator.workflow_def import WorkflowDef
from persistence.event_checkpoints import record_step

DEFAULT_STEP_TIMEOUT_SECONDS = 300
DEFAULT_SWEEP_INTERVAL_SECONDS = 30


def _deadline() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=DEFAULT_STEP_TIMEOUT_SECONDS)


def _master_group(workflow_def: WorkflowDef) -> str:
    return f"{workflow_def.name}.master"


async def start_run(bus: EventBus, workflow_def: WorkflowDef, thread_id: str, initial_payload: dict) -> None:
    """External trigger -> run recorded as 'running', first command dispatched.

    Safe to retry for the same thread_id (e.g. a caller unsure whether an
    earlier call actually got through): run_state.create_run() is an
    ON CONFLICT DO NOTHING no-op on an existing row, and the command publish
    below uses a deterministic event_id, so a retry after a partial failure
    re-attempts only whichever half didn't complete."""
    step = workflow_def.first_step()
    step.validate_input(initial_payload)
    await run_state.create_run(
        thread_id, workflow_def.name, step.name, step_deadline_at=_deadline(), initial_state=initial_payload
    )
    topic = commands_topic(workflow_def.name, step.name)
    await bus.publish(
        Event(
            event_id=deterministic_event_id(thread_id, topic, step.command_type),
            thread_id=thread_id,
            topic=topic,
            event_type=step.command_type,
            payload=initial_payload,
        )
    )


async def run_master(bus: EventBus, workflow_def: WorkflowDef, *, worker_id: str, checkpointer) -> None:
    """Runs forever: advances or terminates a run for each completion event
    on the workflow's events topic.

    `checkpointer` is the same long-lived AsyncPostgresSaver the synchronous
    path uses (persistence/checkpointer.py) -- passed in rather than opened
    here so the caller (workflows/event_driven_pipeline.py) owns its
    lifetime, same reasoning as agents/*/server.py building its MCPGateway
    once in `lifespan` instead of per-request. See
    persistence/event_checkpoints.py for what gets written and why."""
    events = events_topic(workflow_def.name)
    async with bus.subscribe(events, _master_group(workflow_def), worker_id=worker_id) as deliveries:
        async for delivery in deliveries:
            await _handle_completion(bus, workflow_def, delivery.event, checkpointer)
            await delivery.ack()


async def run_deadline_sweeper(*, checkpointer, poll_interval_seconds: float = DEFAULT_SWEEP_INTERVAL_SECONDS) -> None:
    """Runs forever: periodically escalates any run whose step_deadline_at
    has passed into 'needs_review'. Meant to run concurrently with
    run_master() (see workflows/event_driven_pipeline.py) -- it's a
    completely separate polling loop, not tied to the completion-event
    stream, because a stuck-but-alive worker is exactly the case where no
    completion event ever arrives to trigger anything.

    Sweeps once immediately before the first sleep -- a run that was already
    past its deadline before this process (re)started must not wait out a
    full poll_interval_seconds just because the loop happened to sleep
    first.

    This is the one run_state transition that happens outside
    _handle_completion, so it mirrors its own checkpoint here rather than
    sharing that function's `_checkpoint` closure -- same
    record_step()/checkpointer contract, just a second call site."""
    while True:
        swept = await run_state.sweep_expired_runs()
        for row in swept:
            print(
                f"[master_agent] swept thread_id={row['thread_id']!r} workflow={row['workflow_name']!r} "
                f"step={row['current_step']!r}: step deadline exceeded, marked needs_review",
                flush=True,
            )
            await record_step(
                checkpointer,
                thread_id=row["thread_id"],
                step_name=row["current_step"],
                state_payload=row["state_payload"],
            )
        await asyncio.sleep(poll_interval_seconds)


def _log_lost_race(thread_id: str, attempted: str) -> None:
    # mark_terminal()/advance() returning False means the row was no longer
    # 'running' by the time the UPDATE ran -- run_deadline_sweeper() escalated
    # it to 'needs_review' out from under this completion, which was already
    # in flight (past get_run() at the top of _handle_completion) when the
    # sweep happened. Nothing to undo: the sweeper's write already won, this
    # is purely for visibility into how often the race actually fires.
    print(
        f"[master_agent] lost the race {attempted} for thread_id={thread_id!r}: "
        f"run_deadline_sweeper already moved this run out of 'running'",
        flush=True,
    )


async def _handle_completion(bus: EventBus, workflow_def: WorkflowDef, completion: Event, checkpointer) -> None:
    # A completion for a thread_id with no orchestrator_runs row (e.g. a
    # command was published directly without going through start_run(), or
    # the run's row genuinely no longer exists) must not crash the master
    # loop -- log and move on rather than subscript a None run later.
    run = await run_state.get_run(completion.thread_id)
    if run is None:
        print(
            f"[master_agent] ignoring completion for thread_id={completion.thread_id!r} "
            f"(event_type={completion.event_type!r}): no orchestrator_runs row",
            flush=True,
        )
        return

    if run["status"] != "running":
        # A redelivery of a completion this loop already processed (e.g. it
        # crashed, or ack() itself failed, after the first pass already
        # advanced/terminated the run). Reprocessing would double-advance or
        # re-terminate -- idempotent no-op instead, the caller still acks it.
        print(
            f"[master_agent] ignoring completion for thread_id={completion.thread_id!r} "
            f"(event_type={completion.event_type!r}): run is already {run['status']!r}",
            flush=True,
        )
        return

    # Records a checkpoint mirroring a mark_terminal()/advance() call *iff* it
    # actually won its compare-and-swap -- lets every terminal branch below
    # share the same "only checkpoint a transition this process really made"
    # guard as _log_lost_race() itself, instead of repeating it five times.
    async def _checkpoint(step_name: str, state_updates: dict) -> None:
        await record_step(
            checkpointer,
            thread_id=completion.thread_id,
            step_name=step_name,
            state_payload=run_state.merge_state(run, state_updates),
        )

    finished_step = next((s for s in workflow_def.steps if s.completion_type == completion.event_type), None)
    if finished_step is None:
        state_updates = {"error": f"unrecognized completion_type {completion.event_type!r}"}
        if not await run_state.mark_terminal(completion.thread_id, "failed", state_updates):
            _log_lost_race(completion.thread_id, "marking 'failed' (unrecognized completion_type)")
            return
        await _checkpoint(completion.event_type, state_updates)
        return

    if finished_step.name != run["current_step"]:
        # Stale/duplicate completion for a step this run already moved past
        # -- same reprocessing risk as the terminal-run case above.
        print(
            f"[master_agent] ignoring completion for thread_id={completion.thread_id!r} "
            f"step={finished_step.name!r}: run is currently on step {run['current_step']!r}",
            flush=True,
        )
        return

    status = completion.payload.get("status")
    if status == "needs_review":
        if not await run_state.mark_terminal(completion.thread_id, "needs_review", completion.payload):
            _log_lost_race(completion.thread_id, "marking 'needs_review'")
            return
        await _checkpoint(finished_step.name, completion.payload)
        return
    if status == "error":
        if not await run_state.mark_terminal(completion.thread_id, "failed", completion.payload):
            _log_lost_race(completion.thread_id, "marking 'failed'")
            return
        await _checkpoint(finished_step.name, completion.payload)
        return
    if status != "ok":
        # An unrecognized status is treated as a failure rather than silently
        # advancing on a completion event this run loop doesn't understand.
        state_updates = {"error": f"unrecognized completion status {status!r}"}
        if not await run_state.mark_terminal(completion.thread_id, "failed", state_updates):
            _log_lost_race(completion.thread_id, "marking 'failed' (unrecognized completion status)")
            return
        await _checkpoint(finished_step.name, state_updates)
        return

    # "output" is the step's business result, nested under its own key
    # (docs/agent-api-contract.md's response envelope) precisely so it can
    # never collide with "status" or any other envelope bookkeeping key --
    # no filtering needed before folding it into persisted state / forwarding
    # as the next command's payload.
    business_payload = completion.payload["output"]

    next_step = workflow_def.next_step(finished_step.name)
    if next_step is None:
        if not await run_state.mark_terminal(completion.thread_id, "completed", business_payload):
            _log_lost_race(completion.thread_id, "marking 'completed'")
            return
        await _checkpoint(finished_step.name, business_payload)
        return

    # Accumulate the run's full state, not just this step's delta -- see
    # run_state.merge_state's docstring.
    merged_payload = run_state.merge_state(run, business_payload)

    # The *dispatched* command, though, must be filtered down to next_step's
    # own declared input_schema properties -- forwarding the full
    # merged_payload verbatim would include fields earlier steps
    # contributed (e.g. "audio_ref" surviving into check's command) that
    # next_step never declared, and every input_schema here sets
    # additionalProperties: false, so an unfiltered forward fails
    # next_step.validate_input() the moment a workflow has more than two
    # steps. This is the input_fields-style filtering
    # docs/agent-api-contract.md always intended (see its "三個現有 agent 的
    # 合約規格" table) -- it just never got implemented when input_fields
    # was replaced by full JSON Schema validation.
    next_step_payload = {k: v for k, v in merged_payload.items() if k in next_step.input_schema.get("properties", {})}

    # Publish the next command *before* persisting the advance, and with a
    # deterministic event_id: if this process crashes (or ack() fails)
    # between the two, the redelivered completion re-enters this function
    # with current_step unchanged, so it retries both steps cleanly --
    # publish() dedupes via UNIQUE(event_id), and advance() is a plain
    # UPDATE safe to (re)apply once the publish side is confirmed.
    topic = commands_topic(workflow_def.name, next_step.name)
    await bus.publish(
        Event(
            event_id=deterministic_event_id(completion.thread_id, topic, next_step.command_type),
            thread_id=completion.thread_id,
            topic=topic,
            event_type=next_step.command_type,
            payload=next_step_payload,
        )
    )
    if not await run_state.advance(completion.thread_id, next_step.name, business_payload, step_deadline_at=_deadline()):
        _log_lost_race(completion.thread_id, f"advancing to step {next_step.name!r}")
        return
    await _checkpoint(finished_step.name, business_payload)
