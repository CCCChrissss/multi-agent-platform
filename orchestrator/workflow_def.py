"""Declarative workflow definitions for event-driven orchestration.

A workflow definition is the single source of truth for what steps run, in
what order, and what command/completion event types each step speaks --
the Master Agent (orchestrator/master_agent.py) is a pure interpreter over
this structure, so no step name or event type is ever hardcoded in
orchestrator code. Scenario-specific sequences (e.g. STT -> Check ->
Notified, see workflows/definitions/stt_check_notify.yaml) live only here,
as data -- the platform-capability-vs-scenario-logic split from
docs/harness-engineering-principles.md's checklist item 9.

`input_schema`/`output_schema` are a step's half of the agent API contract
(docs/agent-api-contract.md): JSON Schema (draft 2020-12) objects the
command payload and the handler's result must each validate against,
respectively -- not just field presence, but the declared type/shape of
every field. `validate_input`/`validate_output` enforce them at the same
two boundaries every step already crosses (orchestrator/worker.py's
claim-execute-publish loop, orchestrator/master_agent.py's start_run)
regardless of whether the step ends up called in-process, over event_bus,
or (later) over a real API -- the contract doesn't know or care which.

Pure (no I/O beyond load_workflow_def reading the file), so this is
unit-testable on its own, the same shape as mcp_servers/policy.py.

`memory_write` (optional, per step) declares what orchestrator/memory_writer.py
(docs/long-term-memory-plan.md M3) should distill into long-term memory when
this step completes successfully -- same interpreter split as everything
else here: the background distiller stays workflow-agnostic, the "what to
remember" decision lives in the YAML.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from harness.schema_validation import validate_against_schema

_DEFAULT_WORKFLOW_DEF_PATH = Path(__file__).resolve().parents[1] / "workflows" / "definitions" / "stt_check_notify.yaml"


@dataclass(frozen=True)
class MemoryWriteRule:
    """One declared "distill this step's success into a memory" rule
    (orchestrator/memory_writer.py, docs/long-term-memory-plan.md M3).

    Always writes MemoryKind.EPISODIC -- `input_field`/`output_fields` only
    make sense for episodic's platform-standard `{"input": str, "output":
    str}` content shape (persistence/memory_prompt.py); procedural
    (`{"rule": str}`) and semantic (scenario-defined, no common shape) would
    need a different rule shape entirely, and nothing consumes one yet. Add a
    `kind` field back if a second kind is ever actually implemented."""

    tenant: str
    input_field: str
    output_fields: tuple[str, ...]


@dataclass(frozen=True)
class StepDef:
    name: str
    command_type: str
    completion_type: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    memory_write: tuple[MemoryWriteRule, ...] = ()

    def validate_input(self, payload: dict) -> None:
        validate_against_schema(self.name, "input", payload, self.input_schema)

    def validate_output(self, output: dict) -> None:
        validate_against_schema(self.name, "output", output, self.output_schema)


@dataclass(frozen=True)
class WorkflowDef:
    name: str
    steps: tuple[StepDef, ...] = ()

    def first_step(self) -> StepDef:
        if not self.steps:
            raise ValueError(f"workflow {self.name!r} has no steps")
        return self.steps[0]

    def step(self, name: str) -> StepDef:
        for s in self.steps:
            if s.name == name:
                return s
        raise KeyError(f"workflow {self.name!r} has no step named {name!r}; known steps: {[s.name for s in self.steps]}")

    def next_step(self, current: str) -> StepDef | None:
        """The step after `current`, or None if `current` is the last step."""
        names = [s.name for s in self.steps]
        if current not in names:
            raise KeyError(f"workflow {self.name!r} has no step named {current!r}; known steps: {names}")
        idx = names.index(current)
        return self.steps[idx + 1] if idx + 1 < len(self.steps) else None


_REQUIRED_STEP_FIELDS = ("name", "command_type", "completion_type", "input_schema", "output_schema")


def resolve_workflow_def_path() -> str:
    """Which workflow YAML a running process serves -- the WORKFLOW_DEF_PATH
    env var, defaulting to stt_check_notify.yaml so every caller predating
    docs/exclusion-scenario-plan.md P5 is unaffected. agents/lifespan.py and
    workflows/event_driven_pipeline.py both read this at process startup
    (not per-request) so the agent-server layer and the orchestrator layer
    never disagree about which workflow is live -- previously each defined
    its own copy of this default-path/env-read logic; this is the one place
    now, so the two can't drift."""
    return os.environ.get("WORKFLOW_DEF_PATH", str(_DEFAULT_WORKFLOW_DEF_PATH))


def load_workflow_def(path: str) -> WorkflowDef:
    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    name = raw.get("name")
    if not name:
        raise ValueError(f"{path}: workflow definition missing required 'name'")

    raw_steps = raw.get("steps") or []
    if not raw_steps:
        raise ValueError(f"{path}: workflow {name!r} has no steps")

    steps: list[StepDef] = []
    seen_names: set[str] = set()
    # master_agent._handle_completion routes a completion to a step purely by
    # matching completion_type (first match wins), so a duplicate
    # completion_type would silently misroute with no error -- reject it at
    # load time instead, same as the duplicate-name check. command_type gets
    # the same treatment for symmetry, even though nothing currently routes
    # on it the same way.
    seen_command_types: set[str] = set()
    seen_completion_types: set[str] = set()
    for i, raw_step in enumerate(raw_steps):
        for field_name in _REQUIRED_STEP_FIELDS:
            if not raw_step.get(field_name):
                raise ValueError(f"{path}: step {i} in workflow {name!r} missing required {field_name!r}")
        step_name = raw_step["name"]
        command_type = raw_step["command_type"]
        completion_type = raw_step["completion_type"]
        if step_name in seen_names:
            raise ValueError(f"{path}: workflow {name!r} has duplicate step name {step_name!r}")
        if command_type in seen_command_types:
            raise ValueError(f"{path}: workflow {name!r} has duplicate command_type {command_type!r}")
        if completion_type in seen_completion_types:
            raise ValueError(f"{path}: workflow {name!r} has duplicate completion_type {completion_type!r}")
        seen_names.add(step_name)
        seen_command_types.add(command_type)
        seen_completion_types.add(completion_type)
        input_schema = raw_step["input_schema"]
        output_schema = raw_step["output_schema"]
        for schema_name, schema in (("input_schema", input_schema), ("output_schema", output_schema)):
            try:
                Draft202012Validator.check_schema(schema)
            except Exception as exc:
                raise ValueError(
                    f"{path}: step {step_name!r} has an invalid {schema_name}: {exc}"
                ) from exc
        raw_memory_write = (raw_step.get("memory") or {}).get("write") or []
        memory_write: list[MemoryWriteRule] = []
        for j, raw_rule in enumerate(raw_memory_write):
            input_field = raw_rule.get("input_field")
            output_fields = raw_rule.get("output_fields") or []
            if not input_field or not output_fields:
                raise ValueError(
                    f"{path}: step {step_name!r} memory.write[{j}] requires non-empty 'input_field' and 'output_fields'"
                )
            memory_write.append(
                MemoryWriteRule(
                    tenant=raw_rule.get("tenant", "default"),
                    input_field=input_field,
                    output_fields=tuple(output_fields),
                )
            )

        steps.append(
            StepDef(
                name=step_name,
                command_type=command_type,
                completion_type=completion_type,
                input_schema=input_schema,
                output_schema=output_schema,
                memory_write=tuple(memory_write),
            )
        )

    return WorkflowDef(name=name, steps=tuple(steps))
