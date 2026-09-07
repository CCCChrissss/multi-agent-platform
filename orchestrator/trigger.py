"""Generic external-trigger CLI for starting a workflow run.

This is the "external trigger" master_agent.start_run() is written to expect
-- a file watcher, a webhook, a cron, or (for now) a human running this
script by hand are all the same thing from start_run()'s point of view: it
never inspects the payload, so this CLI doesn't need to know anything about
any particular workflow's payload shape either (see
orchestrator/master_agent.py's docstring). It only accepts a workflow
definition path and an already-built JSON payload, and passes both straight
through.

Usage:
    uv run python -m orchestrator.trigger \
        --workflow-def workflows/definitions/stt_check_notify.yaml \
        --payload '{"audio_ref": "samples/gen_tsmc_01.wav"}'
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from pathlib import Path

from persistence.asyncio_compat import configure_asyncio_for_psycopg


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse and validate input before importing database/model dependencies."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workflow-def", required=True, help="path to a workflow definition YAML")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--payload", help="JSON payload for the first step's command")
    source.add_argument("--payload-file", type=Path, help="UTF-8 JSON object file (relative to current directory)")
    parser.add_argument("--thread-id", default=None, help="defaults to a random UUID")
    args = parser.parse_args(argv)
    try:
        text = args.payload_file.read_text(encoding="utf-8-sig") if args.payload_file else args.payload
        args.payload = json.loads(text)
        if not isinstance(args.payload, dict):
            parser.error("payload must be a JSON object")
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        parser.error(f"cannot read payload as a valid JSON object: {exc}")
    return args


async def main() -> None:
    args = parse_args()
    from event_bus.factory import get_event_bus
    from orchestrator import master_agent, run_state
    from orchestrator.workflow_def import load_workflow_def

    workflow_def = load_workflow_def(args.workflow_def)
    payload = args.payload
    thread_id = args.thread_id or str(uuid.uuid4())

    run_state.ensure_schema()
    bus = get_event_bus()
    await bus.ensure_schema()

    await master_agent.start_run(bus, workflow_def, thread_id, payload)
    print(f"started run thread_id={thread_id} workflow={workflow_def.name}", flush=True)


def run() -> None:
    """Configure the process before ``asyncio.run()`` creates its event loop."""
    configure_asyncio_for_psycopg()
    asyncio.run(main())


if __name__ == "__main__":
    run()
