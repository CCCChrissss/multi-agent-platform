"""P5 (docs/knowledge-distillation-plan.md): human review CLI for episodic
memory. orchestrator/memory_writer.py writes every episodic case with
status="pending" -- invisible to recall()/browse() (so no agent ever sees an
unreviewed judgment as a few-shot example -- persistence/memory_prompt.py
doesn't even offer that path anymore) and to scripts/distill_procedural.py
(it also reads episodic through recall(), so it only generalizes from cases
a human has already confirmed correct here).

For each pending case under a scope: shows the input/output pair, asks
approve/reject/edit/skip.

- approve (a): flips status pending -> active in place (persistence/memory.py's
  edit()), preserving the original audit fields (source_thread_id/source_step/
  created_by) instead of re-stamping them as if a new memory were created.
- edit (e): lets the reviewer retype the `output` field (fixing a wrong
  automatic judgment) before approving -- the stored value gets
  `edited_by_reviewer: true` since the content no longer matches what
  memory_writer actually produced.
- reject (r) / skip (s): no write. A case a reviewer looked at and rejected
  is, on disk, indistinguishable from one nobody's looked at yet -- both
  just stay `pending` and reappear next run. ponytail: no negative-signal
  tracking (same open question as scripts/review_memory.py's reject --
  TODO.md's distill-reject-negative-signal); add a `status="rejected"` third
  state if a rejected case turning up again in review actually becomes a
  problem in practice.

Run with:
    uv run python -m scripts.review_episodic --scope stt_exclusion_notify/check [--key pending-...]

Requires the local stack (`uv run honcho start`).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from persistence.call_log import current_node_name, current_thread_id
from persistence.memory import MemoryKind, edit, list_pending, parse_scope
from persistence.memory_lifespan import open_agent_memory

_POLICY_PATH = "mcp_servers/policy.yaml"
_PRINCIPAL = "memory_writer"  # same principal orchestrator/memory_writer.py writes as -- already holds write on */episodic/* (mcp_servers/policy.yaml)


async def approve(
    store: Any, memory_policy: Any, scope: tuple[str, ...], key: str, value: dict[str, Any], *, edited: bool = False
) -> None:
    """Flips a pending episodic case to active -- shared by main()'s CLI loop
    and demo/api.py's POST /memory/episodic/{key}/approve, same reasoning as
    scripts/review_memory.py's approve()."""
    updated = {**value, "status": "active", "reviewed_by": _PRINCIPAL, "reviewed_at": datetime.now(UTC).isoformat()}
    if edited:
        updated["edited_by_reviewer"] = True
    await edit(store, memory_policy, MemoryKind.EPISODIC, tenant="default", scope=scope, key=key, value=updated)
    print(f"[review] approved {key!r} -- now active under default/episodic/{'/'.join(scope)}")


async def main(scope_arg: str, key: str | None = None) -> None:
    scope = parse_scope(scope_arg)

    current_thread_id.set(f"review-episodic-{uuid.uuid4()}")
    current_node_name.set(_PRINCIPAL)

    async with open_agent_memory(_POLICY_PATH) as (store, memory_policy):
        pending = await list_pending(store, MemoryKind.EPISODIC, scope, key)
        if not pending:
            reason = f"key={key!r} not found or not pending" if key else "no pending cases"
            print(f"[review] {reason} under default/episodic/{'/'.join(scope)}")
            return

        for item in pending:
            content = item.value["content"]
            edited = False
            print(f"\n{'=' * 72}\ncase {item.key}")

            while True:
                print(f"input:  {content['input']}")
                print(f"output: {content['output']}")
                decision = input("\napprove / reject / edit / skip this case? [a/r/e/s]: ").strip().lower()
                if decision == "e":
                    new_output = input(f"new output (JSON, blank to cancel, current: {content['output']!r}):\n> ").strip()
                    if new_output:
                        try:
                            json.loads(new_output)
                        except json.JSONDecodeError as exc:
                            print(f"[review] cancelled edit -- not valid JSON ({exc})")
                            continue
                        content = {**content, "output": new_output}
                        edited = True
                    else:
                        print("[review] cancelled edit -- unchanged")
                    continue
                if decision == "a":
                    await approve(store, memory_policy, scope, item.key, {**item.value, "content": content}, edited=edited)
                elif decision == "r":
                    print(f"[review] rejected {item.key!r} -- left pending (no delete, see module docstring)")
                else:
                    print(f"[review] skipped {item.key!r} -- still pending")
                break


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--scope", required=True, help="<workflow_name>/<step_name>, e.g. stt_exclusion_notify/check")
    parser.add_argument("--key", default=None, help="Only review this one pending case's key, ignore other pending ones in scope")
    args = parser.parse_args()
    asyncio.run(main(args.scope, args.key))
