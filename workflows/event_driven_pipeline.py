"""Event-driven counterpart to workflows/simple_pipeline.py's stt -> check ->
notified sequence. Same business logic, different orchestration: this mode
runs each step as its own worker process, coordinated over event_bus/
instead of a single in-process LangGraph graph.invoke() call. See
docs/event-driven-multi-agent-coordination-plan.md for the full design.

build_step_handlers() is the only scenario-specific code in this path -- it
unpacks a command payload into the shape each agent's HTTP client expects
and repacks the result into a completion payload. The agents themselves
(llm.stt_agent.transcribe, ...) are unchanged and still shared with the
synchronous pipeline -- only reached here through agents/<name>/client.py's
HTTP call to the standalone agent servers (docs/agent-api-contract.md)
instead of an in-process function call, so worker processes no longer need
their own MCPGateway.

Two demo scenarios now share this file (docs/exclusion-scenario-plan.md
P5) -- which one a running process serves is a startup-time choice
(orchestrator.workflow_def.resolve_workflow_def_path()'s WORKFLOW_DEF_PATH
env var, default stt_check_notify.yaml -- the same resolver agents/lifespan.py
calls, so the agent-server layer and this orchestrator layer never disagree
about which workflow is live), not a per-request one.
"""

from __future__ import annotations

import argparse
import asyncio
import uuid
from pathlib import Path

from agents.check.client import judge_exclusion as check_agent_judge_exclusion
from agents.check.client import mentions_tsmc as check_agent_mentions_tsmc
from agents.notified.client import decide_and_notify as notified_agent_decide_and_notify
from agents.stt.client import transcribe as stt_agent_transcribe
from event_bus.factory import get_event_bus
from mcp_servers.notified.agent import TSMC_NOTIFICATION_SUBJECT
from orchestrator import run_state
from orchestrator.master_agent import run_deadline_sweeper, run_master
from orchestrator.memory_writer import run_memory_writer
from orchestrator.worker import Handler, run_worker
from orchestrator.workflow_def import WorkflowDef, load_workflow_def, resolve_workflow_def_path
from persistence.call_log import ensure_schema as ensure_call_log_schema
from persistence.checkpointer import get_checkpointer
from persistence.memory_lifespan import open_agent_memory
from persistence.pool import get_shared_pool

_REPO_ROOT = Path(__file__).resolve().parents[1]
_POLICY_PATH = _REPO_ROOT / "mcp_servers" / "policy.yaml"
_EXCLUSION_WORKFLOW_NAME = "stt_exclusion_notify"


# ponytail: handlers never pass `context`, so every run's memory ops resolve
# to tenant_id="default" -- multi-tenant isolation deferred, wire context
# through Handler/orchestrator_runs when a second tenant actually shows up.
def build_step_handlers(workflow_def: WorkflowDef) -> dict[str, Handler]:
    async def stt_handler(payload: dict) -> dict:
        transcript = await stt_agent_transcribe(payload["audio_ref"])
        return {"transcript": transcript}

    async def notified_handler(payload: dict) -> dict:
        log = await notified_agent_decide_and_notify(
            bool(payload.get("should_notify")), payload.get("subject") or "", payload.get("body") or ""
        )
        return {"notified_log": log}

    if workflow_def.name == _EXCLUSION_WORKFLOW_NAME:
        # llm/exclusion_judge.py's output is just the judgment fields
        # (involves_exclusion/matched_articles/reason) -- notified's
        # should_notify/subject/body are derived by `notified`'s own
        # input_mapping (workflows/definitions/stt_exclusion_notify.yaml,
        # docs/generic-agent-runtime-plan.md P0), not scenario glue here, so
        # there's nothing left to do below beyond forwarding the judgment.
        async def check_handler(payload: dict) -> dict:
            transcript = (payload.get("transcript") or "").strip()
            return await check_agent_judge_exclusion(transcript)

        return {"stt": stt_handler, "check": check_handler, "notified": notified_handler}

    # ponytail: "should_notify = mentions_tsmc, subject = a fixed TSMC string"
    # is the one bit of TSMC-specific scenario logic left in this path --
    # it can't live in agents/check/server.py (that HTTP contract stays the
    # generic "does this mention TSMC" judgment, unchanged) or in
    # llm/notify_agent.py (deliberately scenario-agnostic, see its module
    # docstring), so it lives here, in the file whose own docstring already
    # says it's "the only scenario-specific code in this path"
    # (docs/exclusion-scenario-plan.md §3.5/P0).
    async def check_handler(payload: dict) -> dict:
        transcript = (payload.get("transcript") or "").strip()
        mentions_tsmc = await check_agent_mentions_tsmc(transcript)
        return {
            "mentions_tsmc": mentions_tsmc,
            "should_notify": mentions_tsmc,
            "subject": TSMC_NOTIFICATION_SUBJECT if mentions_tsmc else "",
            "body": transcript,
        }

    return {"stt": stt_handler, "check": check_handler, "notified": notified_handler}


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=["master", "worker", "memory-writer"], required=True)
    parser.add_argument("--step", required=False, help="required for --role worker; a step name, or 'all' to run every step's loop in this one process")
    parser.add_argument("--worker-id", default=None)
    args = parser.parse_args()
    if args.role == "worker" and not args.step:
        raise SystemExit("--step is required for --role worker")

    ensure_call_log_schema()
    run_state.ensure_schema()
    workflow_def = load_workflow_def(resolve_workflow_def_path())

    if args.role == "master":
        # Master runs run_master (event_bus) and run_deadline_sweeper
        # (run_state) concurrently against the same PERSISTENCE_DATABASE_URL
        # -- share one pool between them instead of each opening its own.
        pool = await get_shared_pool()
        bus = get_event_bus(pool=pool)
        await bus.ensure_schema()
        worker_id = args.worker_id or f"master-{uuid.uuid4().hex[:8]}"
        print(f"[master] starting, worker_id={worker_id}", flush=True)
        # Long-lived for the whole process, same reasoning as
        # workflows/simple_pipeline.py's main() opening one for the graph's
        # lifetime -- setup() also creates/migrates the checkpoints/
        # checkpoint_blobs/checkpoint_writes/checkpoint_migrations tables
        # persistence/event_checkpoints.py's record_step() writes into (see
        # orchestrator/master_agent.py's module docstring).
        async with get_checkpointer() as checkpointer:
            await checkpointer.setup()
            await asyncio.gather(
                run_master(bus, workflow_def, worker_id=worker_id, checkpointer=checkpointer),
                run_deadline_sweeper(checkpointer=checkpointer),
            )
        return

    if args.role == "memory-writer":
        # docs/long-term-memory-plan.md M3. Own consumer_group, own long-lived
        # store -- same open_agent_memory() helper agents/*/server.py's
        # lifespan uses, same reasoning: AsyncPostgresStore must not be
        # opened per-event.
        pool = await get_shared_pool()
        bus = get_event_bus(pool=pool)
        await bus.ensure_schema()
        worker_id = args.worker_id or f"memory-writer-{uuid.uuid4().hex[:8]}"
        print(f"[memory-writer] starting, worker_id={worker_id}", flush=True)
        async with open_agent_memory(str(_POLICY_PATH)) as (store, memory_policy):
            await run_memory_writer(bus, workflow_def, store, memory_policy, worker_id=worker_id)
        return

    bus = get_event_bus()  # one process, one bus -> one pool shared by every step below
    await bus.ensure_schema()
    handlers = build_step_handlers(workflow_def)

    if args.step == "all":
        # topic / consumer_group / claim 邏輯完全不變，只是三個處理迴圈搬進同一個
        # process、共用一個 pool。SKIP LOCKED 保證跟外面獨立起的單 step worker
        # （例如擴瓶頸那一步）自動分工，不會重複處理同一則命令。
        suffix = args.worker_id or uuid.uuid4().hex[:8]
        print(f"[worker:all] starting, steps={sorted(handlers)}, suffix={suffix}", flush=True)
        await asyncio.gather(
            *(run_worker(bus, workflow_def, step, handler, worker_id=f"{step}-{suffix}") for step, handler in handlers.items())
        )
        return

    handler = handlers.get(args.step)
    if handler is None:
        raise SystemExit(f"no handler registered for step {args.step!r}; available: {sorted(handlers)} or 'all'")

    worker_id = args.worker_id or f"{args.step}-{uuid.uuid4().hex[:8]}"
    print(f"[worker:{args.step}] starting, worker_id={worker_id}", flush=True)
    await run_worker(bus, workflow_def, args.step, handler, worker_id=worker_id)


if __name__ == "__main__":
    asyncio.run(main())
