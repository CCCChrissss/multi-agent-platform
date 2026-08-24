"""Manual smoke test for the event-driven orchestrator -- run with:
    uv run python -m orchestrator.smoke_test

Requires the local stack from `uv run honcho start` (Ollama, LiteLLM
Gateway, STT service, and the stt/check/notified agent servers --
docs/agent-api-contract.md) to already be running.

Scenarios:
  - stt_worker_only (M2): a synthetic stt.run command, handled by the real
    stt worker (llm.stt_agent.transcribe, same code the synchronous
    pipeline uses), produces a real transcript and a matching call_log
    shape.
  - master_agent_single_step (M3): an external trigger (master_agent.start_run)
    drives a run through the stt step via the Master Agent, ending in a
    terminal orchestrator_runs row -- no raw event_bus calls, the way a
    real trigger (file watcher, webhook, ...) would use this module.
  - full_chain_happy_path / needs_review_short_circuit (M4): the full
    stt -> check -> notified chain end-to-end, and confirmation that a
    needs_review completion stops the chain instead of advancing it.
  - checkpoint_parity: the completed full_chain_happy_path run left a
    checkpoints/checkpoint_blobs trail matching orchestrator_runs.state_payload,
    the same tables/shape the synchronous path's checkpointer produces (see
    persistence/event_checkpoints.py).
  - worker_crash_recovery / duplicate_publish_no_double_send (M5):
    resilience -- a worker killed mid-processing gets its message reclaimed
    and completed by a second worker instance, and publishing the identical
    Event twice (a retry) never causes double processing.
  - memory_writer_distills_episodic / memory_writer_skips_needs_review
    (docs/long-term-memory-plan.md M3 -- unrelated to this file's own M-number
    scheme above, which tracks this event-driven build's own milestones):
    orchestrator/memory_writer.py, running alongside master/worker, writes an
    episodic memory for a successful check completion and does *not* write
    one for a needs_review completion (no confirmed-correct output to
    distill without a human decision -- see TODO.md's
    needs-review-decision-entry).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import time
import uuid
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from event_bus.base import Event, commands_topic, deterministic_event_id, events_topic
from event_bus.factory import get_event_bus
from event_bus.postgres import PostgresEventBus
from harness.agent_loop import AgentLoopIncomplete
from orchestrator import master_agent, run_state
from orchestrator.master_agent import run_master
from orchestrator.memory_writer import run_memory_writer
from orchestrator.worker import run_worker
from orchestrator.workflow_def import StepDef, WorkflowDef, load_workflow_def
from persistence.call_log import ensure_schema as ensure_call_log_schema, fetch_calls
from persistence.checkpointer import get_checkpointer
from persistence.memory import MemoryKind, build_namespace
from persistence.memory_lifespan import open_agent_memory
from workflows.event_driven_pipeline import build_step_handlers

_REPO_ROOT = Path(__file__).resolve().parents[1]
_WORKFLOW_DEF_PATH = _REPO_ROOT / "workflows" / "definitions" / "stt_check_notify.yaml"
_POLICY_PATH = _REPO_ROOT / "mcp_servers" / "policy.yaml"
_COMPLETION_TIMEOUT_SECONDS = 180


async def scenario_stt_worker_only(bus) -> None:
    workflow_def = load_workflow_def(str(_WORKFLOW_DEF_PATH))
    handlers = build_step_handlers(workflow_def)
    thread_id = str(uuid.uuid4())
    print(f"[stt_worker_only] thread_id={thread_id}", flush=True)

    worker_task = asyncio.create_task(run_worker(bus, workflow_def, "stt", handlers["stt"], worker_id="verify-worker"))

    commands = commands_topic(workflow_def.name, "stt")
    events = events_topic(workflow_def.name)
    await bus.publish(
        Event(
            event_id=str(uuid.uuid4()),
            thread_id=thread_id,
            topic=commands,
            event_type="stt.run",
            payload={"audio_ref": "samples/test_zh_tw.wav"},
        )
    )
    print("[stt_worker_only] published stt.run, waiting for completion...", flush=True)

    try:
        completion = await _wait_for_event(bus, events, thread_id, "verify-listener")
    finally:
        worker_task.cancel()
        await _swallow_cancelled(worker_task)

    assert completion.event_type == "stt.completed", completion.event_type
    assert completion.payload["status"] == "ok", completion.payload
    assert completion.payload["output"].get("transcript"), "expected a non-empty transcript"
    print(f"[stt_worker_only] completion OK: status={completion.payload['status']!r}")
    print(f"[stt_worker_only] transcript: {completion.payload['output']['transcript']!r}")

    rows = await fetch_calls(thread_id)
    assert rows, "expected at least one call_log row for this thread_id"
    for row in rows:
        assert row["node"] == "stt", f"expected node='stt', got {row['node']!r}"
    print(f"[stt_worker_only] call_log OK: {len(rows)} row(s), all node='stt'")


async def scenario_master_agent_single_step(bus, checkpointer) -> None:
    # In-memory one-step workflow: the run must reach a real terminal state
    # in this scenario, and the "check"/"notified" handlers don't exist
    # until M4 -- using the real 3-step definition would leave the run
    # stuck in 'running' forever waiting for a worker that isn't there.
    workflow_def = WorkflowDef(
        name="stt_only_verify",
        steps=(
            StepDef(
                name="stt",
                command_type="stt.run",
                completion_type="stt.completed",
                input_schema={
                    "type": "object",
                    "required": ["audio_ref"],
                    "properties": {"audio_ref": {"type": "string"}},
                    "additionalProperties": False,
                },
                output_schema={
                    "type": "object",
                    "required": ["transcript"],
                    "properties": {"transcript": {"type": "string"}},
                    "additionalProperties": False,
                },
            ),
        ),
    )
    handlers = build_step_handlers(workflow_def)

    worker_task = asyncio.create_task(run_worker(bus, workflow_def, "stt", handlers["stt"], worker_id="m3-worker"))
    master_task = asyncio.create_task(run_master(bus, workflow_def, worker_id="m3-master", checkpointer=checkpointer))

    thread_id = str(uuid.uuid4())
    try:
        await master_agent.start_run(bus, workflow_def, thread_id, {"audio_ref": "samples/test_zh_tw.wav"})
        print(f"[master_agent_single_step] started run thread_id={thread_id}", flush=True)
        run = await _wait_for_terminal_run(thread_id, timeout=_COMPLETION_TIMEOUT_SECONDS)
    finally:
        worker_task.cancel()
        master_task.cancel()
        await _swallow_cancelled(worker_task)
        await _swallow_cancelled(master_task)

    assert run["status"] == "completed", run
    assert run["state_payload"].get("transcript"), run
    print(f"[master_agent_single_step] run reached terminal state: status={run['status']!r}")
    print(f"[master_agent_single_step] final state_payload: {run['state_payload']}")


async def scenario_full_chain_happy_path(bus, checkpointer) -> str:
    # gen_tsmc_01.wav (see samples/ and README.md) is a synthetic sample that
    # mentions TSMC, so this also exercises notified's notify-tool branch,
    # not just the "no notification needed" path stt_only_verify's sample
    # (test_zh_tw.wav) takes in the other scenarios.
    workflow_def = load_workflow_def(str(_WORKFLOW_DEF_PATH))
    handlers = build_step_handlers(workflow_def)

    workers = [
        asyncio.create_task(run_worker(bus, workflow_def, step.name, handlers[step.name], worker_id=f"m4-{step.name}"))
        for step in workflow_def.steps
    ]
    master_task = asyncio.create_task(run_master(bus, workflow_def, worker_id="m4-master", checkpointer=checkpointer))

    thread_id = str(uuid.uuid4())
    try:
        await master_agent.start_run(bus, workflow_def, thread_id, {"audio_ref": "samples/gen_tsmc_01.wav"})
        print(f"[full_chain_happy_path] started run thread_id={thread_id}", flush=True)
        run = await _wait_for_terminal_run(thread_id, timeout=_COMPLETION_TIMEOUT_SECONDS)
    finally:
        for t in (*workers, master_task):
            t.cancel()
        for t in (*workers, master_task):
            await _swallow_cancelled(t)

    assert run["status"] == "completed", run
    payload = run["state_payload"]
    assert payload.get("transcript"), run
    assert "mentions_tsmc" in payload, run
    assert "notified_log" in payload, run
    print(f"[full_chain_happy_path] run completed: mentions_tsmc={payload['mentions_tsmc']!r} notified_log={payload['notified_log']}")

    rows = await fetch_calls(thread_id)
    nodes_seen = {row["node"] for row in rows}
    assert nodes_seen == {"stt", "check", "notified"}, f"expected all three nodes in call_log, got {nodes_seen}"
    print(f"[full_chain_happy_path] call_log OK: {len(rows)} row(s) across nodes {sorted(nodes_seen)}")
    return thread_id


async def scenario_checkpoint_parity(thread_id: str, checkpointer) -> None:
    """Every run_state transition along a completed run must leave a matching
    trail in the same checkpoints/checkpoint_blobs tables the synchronous path
    uses (persistence/checkpointer.py, persistence/event_checkpoints.py) --
    reuses the run scenario_full_chain_happy_path already drove to completion,
    so persistence/history.py's reader can be trusted to work unmodified for
    both orchestration modes."""
    run = await run_state.get_run(thread_id)
    assert run is not None and run["status"] == "completed", run

    config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    checkpoints = [c async for c in checkpointer.alist(config)]
    assert checkpoints, f"expected at least one checkpoint for thread_id={thread_id!r}, found none"

    steps_seen = [c.metadata.get("step") for c in reversed(checkpoints)]
    assert steps_seen == list(range(len(checkpoints))), f"expected step numbers 0..N in order, got {steps_seen}"
    # stt -> check -> notified is 3 completions the master processed (each its
    # own advance()/mark_terminal() call), so 3 mirrored checkpoints -- same
    # "one row per super-step" shape the sync path's graph.compile(checkpointer=...)
    # produces for the same chain.
    assert len(checkpoints) == 3, f"expected 3 checkpoints (stt, check, notified), got {len(checkpoints)}: {checkpoints}"

    latest = checkpoints[0]  # alist() orders newest checkpoint_id first
    assert latest.checkpoint["channel_values"] == run["state_payload"], (
        latest.checkpoint["channel_values"],
        run["state_payload"],
    )
    print(
        f"[checkpoint_parity] OK -- {len(checkpoints)} checkpoint(s) for thread_id={thread_id!r}, "
        "final channel_values matches orchestrator_runs.state_payload"
    )


async def scenario_needs_review_short_circuit(bus, checkpointer) -> None:
    """A step that raises AgentLoopIncomplete must stop the chain: the run
    ends 'needs_review' at that step, and no later step's worker is ever
    dispatched a command -- mirrors workflows/simple_pipeline.py's `_route`
    early-exit on needs_review."""
    workflow_def = load_workflow_def(str(_WORKFLOW_DEF_PATH))

    async def failing_stt_handler(payload: dict) -> dict:
        raise AgentLoopIncomplete(node="stt", reason="synthetic failure injected by scenario_needs_review_short_circuit")

    check_invoked = False

    async def guard_check_handler(payload: dict) -> dict:
        nonlocal check_invoked
        check_invoked = True
        return {"mentions_tsmc": False}

    worker_stt = asyncio.create_task(run_worker(bus, workflow_def, "stt", failing_stt_handler, worker_id="m4-stt-fail"))
    worker_check = asyncio.create_task(run_worker(bus, workflow_def, "check", guard_check_handler, worker_id="m4-check-guard"))
    master_task = asyncio.create_task(run_master(bus, workflow_def, worker_id="m4-master-guard", checkpointer=checkpointer))

    thread_id = str(uuid.uuid4())
    try:
        await master_agent.start_run(bus, workflow_def, thread_id, {"audio_ref": "samples/gen_tsmc_01.wav"})
        run = await _wait_for_terminal_run(thread_id, timeout=30)
        # The run reaching a terminal state only proves stt's own completion
        # was handled; give a wrongly-dispatched "check" command a moment to
        # arrive before checking the guard flag.
        await asyncio.sleep(2)
    finally:
        for t in (worker_stt, worker_check, master_task):
            t.cancel()
        for t in (worker_stt, worker_check, master_task):
            await _swallow_cancelled(t)

    assert run["status"] == "needs_review", run
    assert run["current_step"] == "stt", run
    assert not check_invoked, "check handler was invoked after an stt needs_review -- short-circuit failed"
    print(f"[needs_review_short_circuit] OK -- run stopped at step={run['current_step']!r} status={run['status']!r}, check never invoked")


async def scenario_deadline_sweep() -> None:
    """A run whose step_deadline_at has already passed, with no worker ever
    completing it, must get escalated to 'needs_review' by the periodic
    sweeper (orchestrator.master_agent.run_deadline_sweeper /
    run_state.sweep_expired_runs) -- the "worker alive but stuck" gap that
    event_bus's own per-message lease doesn't cover (no crash, no exception,
    no redelivery -- so nothing else would ever notice)."""
    thread_id = str(uuid.uuid4())
    past_deadline = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
    await run_state.create_run(
        thread_id, "sweep_verify", "stt", step_deadline_at=past_deadline, initial_state={"audio_ref": "samples/test_zh_tw.wav"}
    )
    print(f"[deadline_sweep] created run thread_id={thread_id} with an already-past deadline", flush=True)

    swept = await run_state.sweep_expired_runs()
    swept_ids = {row["thread_id"] for row in swept}
    assert thread_id in swept_ids, f"expected {thread_id} to be swept, got {swept_ids}"

    run = await run_state.get_run(thread_id)
    assert run["status"] == "needs_review", run
    assert run["step_deadline_at"] is None, run
    # Same key AgentLoopIncomplete's needs_review path uses (orchestrator/worker.py),
    # so both routes to needs_review agree on where to find "why".
    assert "exceeded its deadline" in run["state_payload"].get("review_reason", ""), run
    print(f"[deadline_sweep] OK -- run escalated to needs_review: {run['state_payload']['review_reason']!r}")


async def scenario_worker_crash_recovery() -> None:
    """Kill an stt worker mid-transcription (the asyncio-task equivalent of
    `kill -9` -- the claim was already committed to the DB before the
    handler started, so cancelling mid-handler leaves the dispatch row
    'claimed' exactly like a real crash) and confirm a second worker
    instance reclaims the stuck message once its lease expires and
    completes it for real -- the multi-process equivalent of
    workflows/simple_pipeline.py resuming from its checkpointer."""
    bus = PostgresEventBus(os.environ["PERSISTENCE_DATABASE_URL"], lease_seconds=8)
    await bus.ensure_schema()
    workflow_def = load_workflow_def(str(_WORKFLOW_DEF_PATH))
    handlers = build_step_handlers(workflow_def)
    thread_id = str(uuid.uuid4())
    print(f"[worker_crash_recovery] thread_id={thread_id}", flush=True)

    await bus.publish(
        Event(
            event_id=str(uuid.uuid4()),
            thread_id=thread_id,
            topic=commands_topic(workflow_def.name, "stt"),
            event_type="stt.run",
            payload={"audio_ref": "samples/test_zh_tw.wav"},
        )
    )

    crasher = asyncio.create_task(run_worker(bus, workflow_def, "stt", handlers["stt"], worker_id="m5-crasher"))
    await asyncio.sleep(6)  # long enough to have claimed the message and be inside the slow transcribe call
    crasher.cancel()
    await _swallow_cancelled(crasher)
    print("[worker_crash_recovery] killed the first stt worker mid-transcription", flush=True)

    dispatch = await _fetch_dispatch_status(thread_id, f"{workflow_def.name}.stt")
    assert dispatch is not None and dispatch["status"] == "claimed", f"expected the message to still be 'claimed' after the crash, got {dispatch}"
    print(f"[worker_crash_recovery] confirmed message still 'claimed' (attempts={dispatch['attempts']}) -- exactly like a real crash")

    await asyncio.sleep(9)  # past the 8s lease

    rescuer = asyncio.create_task(run_worker(bus, workflow_def, "stt", handlers["stt"], worker_id="m5-rescuer"))
    try:
        completion = await _wait_for_event(bus, events_topic(workflow_def.name), thread_id, "m5-listener")
    finally:
        rescuer.cancel()
        await _swallow_cancelled(rescuer)

    assert completion.payload["status"] == "ok", completion.payload
    assert completion.payload["output"].get("transcript"), completion.payload
    dispatch_after = await _fetch_dispatch_status(thread_id, f"{workflow_def.name}.stt")
    assert dispatch_after["attempts"] == 2, f"expected the rescuer's claim to be the 2nd attempt, got {dispatch_after}"
    print(f"[worker_crash_recovery] OK -- second worker reclaimed and completed it (attempts={dispatch_after['attempts']})")


async def scenario_duplicate_publish_no_double_send(bus) -> None:
    """Publishing the identical Event twice (same event_id -- e.g. a
    publisher retrying after a network blip it isn't sure succeeded) must
    not create two logical messages: event_log's UNIQUE(event_id) +
    ON CONFLICT DO NOTHING makes the second publish() a no-op, so notified
    only ever runs once and sends at most one notification.

    This proves the publish-side dedup guarantee specifically -- it is
    *not* a claim that two genuinely distinct messages could never both
    reach notified (that consumer-side risk is still open, documented as
    an accepted phase-1 gap in
    docs/event-driven-multi-agent-coordination-plan.md)."""
    workflow_def = load_workflow_def(str(_WORKFLOW_DEF_PATH))
    handlers = build_step_handlers(workflow_def)
    thread_id = str(uuid.uuid4())
    print(f"[duplicate_publish_no_double_send] thread_id={thread_id}", flush=True)

    command = Event(
        event_id=str(uuid.uuid4()),
        thread_id=thread_id,
        topic=commands_topic(workflow_def.name, "notified"),
        event_type="notified.run",
        # should_notify/subject/body, not mentions_tsmc -- docs/exclusion-scenario-plan.md
        # P0 made notified's input contract scenario-agnostic; this fixture
        # publishes a hand-crafted notified.run command directly (bypassing
        # check), so it has to keep up with that contract by hand.
        payload={"should_notify": True, "subject": "偵測到台積電相關內容", "body": "台積電今天股價創新高"},
    )
    await bus.publish(command)
    await bus.publish(command)  # simulate a publisher retrying the exact same event

    dup_count = await _count_event_log_rows(command.event_id)
    assert dup_count == 1, f"expected exactly 1 event_log row after a duplicate publish, got {dup_count}"
    print(f"[duplicate_publish_no_double_send] confirmed duplicate publish() collapsed to {dup_count} event_log row")

    worker_task = asyncio.create_task(run_worker(bus, workflow_def, "notified", handlers["notified"], worker_id="m5-notified"))
    try:
        completion = await _wait_for_event(bus, events_topic(workflow_def.name), thread_id, "m5-notified-listener")
    finally:
        worker_task.cancel()
        await _swallow_cancelled(worker_task)

    assert completion.payload["status"] == "ok", completion.payload
    notified_log = completion.payload["output"]["notified_log"]
    send_count = sum(1 for entry in notified_log if "send_gmail_message" in entry and "[ERROR]" not in entry)
    assert send_count == 1, f"expected exactly one successful send, got {send_count}: {notified_log}"
    print("[duplicate_publish_no_double_send] OK -- exactly one Gmail send happened despite publish() being called twice")


async def scenario_memory_writer_distills_episodic(bus, checkpointer, store, memory_policy) -> None:
    """A successful check completion, distilled by orchestrator/memory_writer.py
    running alongside master/worker, must produce an episodic memory under
    the exact scope llm/tsmc_judge.py's _MEMORY_SCOPE already reads
    (docs/long-term-memory-plan.md M3) -- the first time write and read sides
    are actually connected end-to-end."""
    workflow_def = load_workflow_def(str(_WORKFLOW_DEF_PATH))
    handlers = build_step_handlers(workflow_def)

    workers = [
        asyncio.create_task(run_worker(bus, workflow_def, step.name, handlers[step.name], worker_id=f"m3mem-{step.name}"))
        for step in workflow_def.steps
    ]
    master_task = asyncio.create_task(run_master(bus, workflow_def, worker_id="m3mem-master", checkpointer=checkpointer))
    memory_writer_task = asyncio.create_task(
        run_memory_writer(bus, workflow_def, store, memory_policy, worker_id="m3mem-writer")
    )

    thread_id = str(uuid.uuid4())
    scope = (workflow_def.name, "check")
    key = deterministic_event_id(thread_id, events_topic(workflow_def.name), "check.completed")
    try:
        await master_agent.start_run(bus, workflow_def, thread_id, {"audio_ref": "samples/gen_tsmc_01.wav"})
        print(f"[memory_writer_distills_episodic] started run thread_id={thread_id}", flush=True)
        run = await _wait_for_terminal_run(thread_id, timeout=_COMPLETION_TIMEOUT_SECONDS)
        assert run["status"] == "completed", run

        # memory_writer is a separate consumer_group consuming the same
        # completion independently of master_agent -- the run reaching
        # 'completed' (which happens strictly after check, since notified
        # only runs once check's completion advanced the run) means check's
        # completion was published well before this point, but distillation
        # itself is still a separate async hop, so poll briefly instead of
        # assuming it already landed.
        item = await _poll_for_memory_item(store, MemoryKind.EPISODIC, "default", scope, key)
        assert item is not None, f"expected an episodic memory at key={key!r} under scope={scope!r}, found none"
        assert item.value["content"]["output"] == '{"mentions_tsmc": true}', item.value
        assert item.value["content"]["input"], item.value
        assert item.value["source_thread_id"] == thread_id, item.value
        # status="pending" (docs/knowledge-distillation-plan.md P5) -- not
        # yet promoted by scripts/review_episodic.py, so invisible to
        # recall()/browse() until a human reviews it.
        assert item.value["status"] == "pending", item.value
        print(f"[memory_writer_distills_episodic] OK -- episodic memory written (pending review): {item.value['content']}")
    finally:
        for t in (*workers, master_task, memory_writer_task):
            t.cancel()
        for t in (*workers, master_task, memory_writer_task):
            await _swallow_cancelled(t)
        await store.adelete(build_namespace(MemoryKind.EPISODIC, "default", scope), key)


async def scenario_memory_writer_skips_needs_review(bus, checkpointer, store, memory_policy) -> None:
    """A needs_review completion must not produce a memory -- there's no
    confirmed-correct output to distill without a human decision (see
    orchestrator/memory_writer.py's module docstring, TODO.md's
    needs-review-decision-entry)."""
    workflow_def = load_workflow_def(str(_WORKFLOW_DEF_PATH))
    handlers = build_step_handlers(workflow_def)

    async def failing_check_handler(payload: dict) -> dict:
        raise AgentLoopIncomplete(node="check", reason="synthetic failure injected by scenario_memory_writer_skips_needs_review")

    worker_stt = asyncio.create_task(run_worker(bus, workflow_def, "stt", handlers["stt"], worker_id="m3mem-stt-nr"))
    worker_check = asyncio.create_task(run_worker(bus, workflow_def, "check", failing_check_handler, worker_id="m3mem-check-nr"))
    master_task = asyncio.create_task(run_master(bus, workflow_def, worker_id="m3mem-master-nr", checkpointer=checkpointer))
    memory_writer_task = asyncio.create_task(
        run_memory_writer(bus, workflow_def, store, memory_policy, worker_id="m3mem-writer-nr")
    )

    thread_id = str(uuid.uuid4())
    scope = (workflow_def.name, "check")
    key = deterministic_event_id(thread_id, events_topic(workflow_def.name), "check.completed")
    try:
        await master_agent.start_run(bus, workflow_def, thread_id, {"audio_ref": "samples/gen_tsmc_01.wav"})
        print(f"[memory_writer_skips_needs_review] started run thread_id={thread_id}", flush=True)
        run = await _wait_for_terminal_run(thread_id, timeout=_COMPLETION_TIMEOUT_SECONDS)
        assert run["status"] == "needs_review", run
        assert run["current_step"] == "check", run

        # Give memory_writer's independent consumer_group the same chance to
        # (wrongly) act on this completion that the happy-path scenario gets
        # to act correctly -- absence after a wait is a real assertion, not
        # a race won by checking too early.
        await asyncio.sleep(3)
        item = await store.aget(build_namespace(MemoryKind.EPISODIC, "default", scope), key)
        assert item is None, f"expected no episodic memory for a needs_review completion, found {item!r}"
        print("[memory_writer_skips_needs_review] OK -- no episodic memory written for a needs_review completion")
    finally:
        for t in (worker_stt, worker_check, master_task, memory_writer_task):
            t.cancel()
        for t in (worker_stt, worker_check, master_task, memory_writer_task):
            await _swallow_cancelled(t)


async def _poll_for_memory_item(store, kind: MemoryKind, tenant: str, scope: tuple[str, ...], key: str, *, timeout: float = 15.0, poll_interval: float = 0.5):
    # store.aget() bypasses recall()'s status="active" gate on purpose --
    # docs/knowledge-distillation-plan.md P5 has memory_writer write episodic
    # as status="pending" (awaiting scripts/review_episodic.py), which
    # recall() would never surface; this poll needs to see the row the
    # moment it lands, not the moment a human promotes it.
    namespace = build_namespace(kind, tenant, scope)
    deadline = time.monotonic() + timeout
    while True:
        match = await store.aget(namespace, key)
        if match is not None or time.monotonic() > deadline:
            return match
        await asyncio.sleep(poll_interval)


async def _fetch_dispatch_status(thread_id: str, consumer_group: str) -> dict | None:
    async with await psycopg.AsyncConnection.connect(os.environ["PERSISTENCE_DATABASE_URL"]) as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT ed.status, ed.attempts, ed.claimed_by
                FROM event_dispatch ed JOIN event_log el ON el.id = ed.event_log_id
                WHERE el.thread_id = %s AND ed.consumer_group = %s
                ORDER BY ed.id DESC LIMIT 1
                """,
                (thread_id, consumer_group),
            )
            return await cur.fetchone()


async def _count_event_log_rows(event_id: str) -> int:
    async with await psycopg.AsyncConnection.connect(os.environ["PERSISTENCE_DATABASE_URL"]) as conn:
        cur = await conn.execute("SELECT count(*) FROM event_log WHERE event_id = %s", (event_id,))
        (count,) = await cur.fetchone()
        return count


async def _swallow_cancelled(task: asyncio.Task) -> None:
    try:
        await task
    except asyncio.CancelledError:
        pass


async def _wait_for_event(bus, topic: str, thread_id: str, group: str) -> Event:
    async def _listen() -> Event:
        async with bus.subscribe(topic, group, worker_id=group) as deliveries:
            async for delivery in deliveries:
                await delivery.ack()
                if delivery.event.thread_id == thread_id:
                    return delivery.event
        raise AssertionError("unreachable: subscribe() never returns without yielding")

    return await asyncio.wait_for(_listen(), timeout=_COMPLETION_TIMEOUT_SECONDS)


async def _wait_for_terminal_run(thread_id: str, timeout: float, poll_interval: float = 1.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        run = await run_state.get_run(thread_id)
        if run and run["status"] != "running":
            return run
        if time.monotonic() > deadline:
            raise TimeoutError(f"run {thread_id} did not reach a terminal state within {timeout}s (last seen: {run})")
        await asyncio.sleep(poll_interval)


async def main() -> None:
    ensure_call_log_schema()
    run_state.ensure_schema()
    bus = get_event_bus()
    await bus.ensure_schema()

    async with get_checkpointer() as checkpointer, open_agent_memory(str(_POLICY_PATH)) as (store, memory_policy):
        await checkpointer.setup()

        await scenario_stt_worker_only(bus)
        await scenario_master_agent_single_step(bus, checkpointer)
        thread_id = await scenario_full_chain_happy_path(bus, checkpointer)
        await scenario_checkpoint_parity(thread_id, checkpointer)
        await scenario_needs_review_short_circuit(bus, checkpointer)
        await scenario_deadline_sweep()
        await scenario_worker_crash_recovery()
        await scenario_duplicate_publish_no_double_send(bus)
        await scenario_memory_writer_distills_episodic(bus, checkpointer, store, memory_policy)
        await scenario_memory_writer_skips_needs_review(bus, checkpointer, store, memory_policy)

    print("\nAll orchestrator smoke tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
