"""Shared JSON Schema (draft 2020-12) validation + error formatting.

Used by both orchestrator/workflow_def.py's StepDef (event-driven/in-process
transport, checked around orchestrator/worker.py's handler call) and
agents/envelope.py's run_handler() (standalone HTTP transport, checked right
before the API response goes out) -- two different boundaries enforcing the
same per-step contract should raise the same actionable message rather than
each rolling its own, per docs/harness-engineering-principles.md's "write
failures for the agent, not the machine".
"""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator


def validate_against_schema(step_name: str, kind: str, payload: dict, schema: dict[str, Any]) -> None:
    """Raises ValueError with an actionable message on the first schema violation."""
    validator = Draft202012Validator(schema)
    error = next(iter(validator.iter_errors(payload)), None)
    if error is None:
        return
    # error.json_path is e.g. "$.mentions_tsmc" -- precise enough to hand
    # straight back as tool feedback, not just to a human reading logs.
    raise ValueError(
        f"step {step_name!r}: {kind} at {error.json_path} failed schema validation: {error.message}; "
        f"got {kind} keys {sorted(payload)}"
    )
