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
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jinja2
import yaml
from jsonschema import Draft202012Validator

from harness.schema_validation import validate_against_schema

_DEFINITIONS_DIR = Path(__file__).resolve().parents[1] / "workflows" / "definitions"
_DEFAULT_WORKFLOW_DEF_PATH = _DEFINITIONS_DIR / "stt_check_notify.yaml"
_GATEWAY_CONFIG_PATH = Path(__file__).resolve().parents[1] / "gateway" / "config.yaml"

# Reserved state_payload key (docs/generic-agent-runtime-plan.md P0): holds
# {step_name: step_output} for every step that has completed 'ok' so far in
# a run, alongside (never replacing) the flat merged namespace
# run_state.merge_state() already produces. The flat namespace can't
# address "which step's copy of this field" when two steps produce the same
# field name (later one silently wins); this map is what input_mapping's
# `from: "steps.<name>.<field>"` resolves against instead.
STEPS_KEY = "_steps"


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
class PromptDef:
    """A step's system/user prompt (docs/generic-agent-runtime-plan.md P1) --
    the two are declared separately (rather than one blob) so a future UI
    can't blur "static instruction" and "this run's input" into one field a
    non-engineer would misread as free-form text. `user` (and, in principle,
    `system`) may reference `{{ input.<field> }}`; load_workflow_def()
    validates those references against this step's input_schema at load
    time -- see render_prompt() for the runtime half."""

    system: str
    user: str


@dataclass(frozen=True)
class StepDef:
    name: str
    command_type: str
    completion_type: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    # docs/generic-agent-runtime-plan.md P5: the LiteLLM model_name (gateway/config.yaml)
    # this step calls -- was each llm/*.py's own MODEL_NAME module constant.
    # Required (load_workflow_def() checks it's a real model_list entry), not
    # Optional -- every agent step needs a model to run at all.
    model: str
    memory_write: tuple[MemoryWriteRule, ...] = ()
    # {field_name: {"from": "steps.<step>.<field>"} | {"const": <value>} | {"expr": "<jinja2 template>"}}.
    # A field with no entry here falls back to the pre-existing same-name
    # match against the run's flat merged state -- see resolve_step_input().
    input_mapping: dict[str, dict[str, Any]] = field(default_factory=dict)
    prompt: PromptDef | None = None
    # docs/generic-agent-runtime-plan.md P6: {field_name: {"from": "model" |
    # "tool_log" | "tool:<tool_name>"}} -- how this step's output_schema
    # fields get their values. Optional and per-field, same backward-compat
    # posture as input_mapping: a step can declare none, some, or all of its
    # output_schema properties here. harness/generic_agent.py::run_generic_step()
    # is the one caller that actually executes off this declaration; a step
    # with a §8-style verifier (llm/tsmc_judge.py, llm/exclusion_judge.py)
    # can still declare it as documentation without being driven by it.
    output: dict[str, dict[str, Any]] = field(default_factory=dict)

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

    def uses_input_mapping(self) -> bool:
        """Whether any step declares input_mapping -- gates STEPS_KEY
        bookkeeping (orchestrator/master_agent.py, run_state.record_step_output())
        so a workflow that never uses input_mapping sees byte-identical
        state_payload/checkpoint behavior to before P0
        (docs/generic-agent-runtime-plan.md P0's own completion criterion:
        stt_check_notify.yaml's existing checkpoint_parity smoke scenario is
        the unmodified guard for this)."""
        return any(s.input_mapping for s in self.steps)


class MappingResolutionError(ValueError):
    """A step's input_mapping couldn't be resolved for this particular run --
    e.g. a `from:`/`expr:` source step produced output, but not the specific
    field referenced (that field is declared optional in the source step's
    output_schema and this run's handler didn't return it). Distinct from
    the ValueError load_workflow_def() raises for a malformed *definition*:
    this one is a per-run data problem that orchestrator/master_agent.py
    catches and turns into a 'failed' run rather than dispatching a command
    doomed to fail the next step's own input validation."""


_JINJA_ENV = jinja2.Environment(undefined=jinja2.StrictUndefined)
_INPUT_REF_RE = re.compile(r"\binput\.(\w+)")


def render_prompt(step: StepDef, input: dict[str, Any]) -> tuple[str, str]:
    """This step's declared prompt, with `{{ input.<field> }}` filled in from
    a command payload -- the runtime half of PromptDef; load_workflow_def()
    already checked every reference resolves against input_schema, so a
    missing key here means `input` itself is incomplete, not a bad
    template."""
    if step.prompt is None:
        raise ValueError(f"step {step.name!r} has no prompt declared")
    return (
        _JINJA_ENV.from_string(step.prompt.system).render(input=input),
        _JINJA_ENV.from_string(step.prompt.user).render(input=input),
    )


def resolve_prompt(
    workflow_name: str,
    step_name: str,
    default_input: dict[str, Any],
    *,
    system_prompt: str | None,
    user_prompt: str | None,
) -> tuple[str, str]:
    """Fill in whichever of system_prompt/user_prompt the caller didn't
    already provide, from `workflow_name`'s own declared `step_name` prompt
    (docs/generic-agent-runtime-plan.md P1) -- the fallback every llm/*.py
    entry point needs for callers with no spec in hand (workflows/simple_pipeline.py,
    evals/run_eval.py, the llm/*_smoke_test.py suites). Was duplicated
    near-identically across four llm/*.py modules; collected here since it's
    the one place that already knows how to load a step and render its
    prompt."""
    if system_prompt is not None and user_prompt is not None:
        return system_prompt, user_prompt
    default_step = load_workflow_def_by_name(workflow_name).step(step_name)
    default_system, default_user = render_prompt(default_step, default_input)
    return (
        system_prompt if system_prompt is not None else default_system,
        user_prompt if user_prompt is not None else default_user,
    )


def resolve_model(workflow_name: str, step_name: str, *, model: str | None) -> str:
    """`model` if the caller already has one (e.g. agents/runtime.py already
    holding this step's StepDef off app.state), otherwise `workflow_name`'s
    own declared `step_name` model (docs/generic-agent-runtime-plan.md P5) --
    same fallback shape as resolve_prompt(), for callers with no spec in hand
    (workflows/simple_pipeline.py via llm/*.py's default params, evals/run_eval.py,
    the llm/*_smoke_test.py suites)."""
    if model is not None:
        return model
    return load_workflow_def_by_name(workflow_name).step(step_name).model


def resolve_step_input(step: StepDef, merged_payload: dict[str, Any]) -> dict:
    """The command payload to dispatch for `step`, given the run's flat
    merged state plus its STEPS_KEY map (run_state.merge_state()'s output,
    after folding in the just-completed step's output). Per input_schema
    property: an input_mapping entry wins if present, otherwise same-name
    fallback against the flat namespace -- unchanged behavior for any step
    with no mapping at all, or a mapping that only covers some fields."""
    steps_map = merged_payload.get(STEPS_KEY) or {}
    resolved: dict[str, Any] = {}
    for field_name in step.input_schema.get("properties", {}):
        if field_name in step.input_mapping:
            resolved[field_name] = _resolve_mapping_entry(step.name, field_name, step.input_mapping[field_name], steps_map)
        elif field_name in merged_payload:
            resolved[field_name] = merged_payload[field_name]
    return resolved


def _resolve_mapping_entry(step_name: str, field_name: str, entry: dict[str, Any], steps_map: dict[str, Any]) -> Any:
    if "const" in entry:
        return entry["const"]
    if "expr" in entry:
        # load_workflow_def() already rejected a syntactically invalid
        # expr at load time -- jinja2.TemplateError here (UndefinedError's
        # own base class, so this still covers it) means something about
        # *this run's* data made rendering fail, e.g. a filter applied to a
        # value of the wrong type.
        try:
            return _JINJA_ENV.from_string(entry["expr"]).render(steps=steps_map)
        except jinja2.TemplateError as exc:
            raise MappingResolutionError(
                f"step {step_name!r} field {field_name!r}: expr {entry['expr']!r} failed to render: {exc}"
            ) from exc
    _, source_step, source_field = entry["from"].split(".")
    source_output = steps_map.get(source_step)
    if source_output is None or source_field not in source_output:
        raise MappingResolutionError(
            f"step {step_name!r} field {field_name!r}: source {entry['from']!r} has no value -- "
            f"step {source_step!r} hasn't produced that field yet"
        )
    return source_output[source_field]


_REQUIRED_STEP_FIELDS = ("name", "command_type", "completion_type", "input_schema", "output_schema", "model")


def _known_model_names(path: str) -> set[str]:
    """The LiteLLM `model_name`s gateway/config.yaml actually serves --
    load_workflow_def() cross-checks every step's `model:` against this set
    at load time (docs/generic-agent-runtime-plan.md P5), same "catch a typo
    at load time, not mid-run" posture as input_mapping's own checks."""
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return {entry["model_name"] for entry in raw.get("model_list") or []}


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


def load_workflow_def_by_name(workflow_name: str) -> WorkflowDef:
    """load_workflow_def() for the workflow whose file is named after it --
    the convention every workflows/definitions/*.yaml already follows.
    Used by llm/*.py's default-prompt fallback (docs/generic-agent-runtime-plan.md
    P1) to resolve a specific scenario's spec regardless of which workflow
    the current process happens to be configured for, unlike
    resolve_workflow_def_path()'s single process-wide WORKFLOW_DEF_PATH."""
    return load_workflow_def(str(_DEFINITIONS_DIR / f"{workflow_name}.yaml"))


def load_workflow_def(path: str) -> WorkflowDef:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    name = raw.get("name")
    if not name:
        raise ValueError(f"{path}: workflow definition missing required 'name'")

    raw_steps = raw.get("steps") or []
    if not raw_steps:
        raise ValueError(f"{path}: workflow {name!r} has no steps")

    all_step_names = {raw_step.get("name") for raw_step in raw_steps}
    known_model_names = _known_model_names(str(_GATEWAY_CONFIG_PATH))

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
        model = raw_step["model"]
        if model not in known_model_names:
            raise ValueError(
                f"{path}: step {step_name!r} model {model!r} is not in gateway/config.yaml's model_list "
                f"(known: {sorted(known_model_names)})"
            )
        for schema_name, schema in (("input_schema", input_schema), ("output_schema", output_schema)):
            try:
                Draft202012Validator.check_schema(schema)
            except Exception as exc:
                raise ValueError(
                    f"{path}: step {step_name!r} has an invalid {schema_name}: {exc}"
                ) from exc
        raw_input_mapping = raw_step.get("input_mapping") or {}
        input_mapping: dict[str, dict[str, Any]] = {}
        for mapped_field, entry in raw_input_mapping.items():
            if not isinstance(entry, dict) or sum(k in entry for k in ("from", "const", "expr")) != 1:
                raise ValueError(
                    f"{path}: step {step_name!r} input_mapping.{mapped_field!r} must have exactly one of "
                    f"'from', 'const', 'expr', got {entry!r}"
                )
            if "from" in entry:
                parts = str(entry["from"]).split(".")
                if len(parts) != 3 or parts[0] != "steps":
                    raise ValueError(
                        f"{path}: step {step_name!r} input_mapping.{mapped_field!r}.from must look like "
                        f"'steps.<step_name>.<field>', got {entry['from']!r}"
                    )
                _, source_step_name, source_field_name = parts
                if source_step_name not in all_step_names:
                    raise ValueError(
                        f"{path}: step {step_name!r} input_mapping.{mapped_field!r}.from references unknown "
                        f"step {source_step_name!r}"
                    )
                source_step = next((s for s in steps if s.name == source_step_name), None)
                if source_step is None:
                    raise ValueError(
                        f"{path}: step {step_name!r} input_mapping.{mapped_field!r}.from references step "
                        f"{source_step_name!r}, which is not before it in the workflow"
                    )
                if source_field_name not in (source_step.output_schema.get("properties") or {}):
                    raise ValueError(
                        f"{path}: step {step_name!r} input_mapping.{mapped_field!r}.from references field "
                        f"{source_field_name!r}, which is not in step {source_step_name!r}'s output_schema"
                    )
            if "expr" in entry:
                # Only a syntax check -- undefined-variable errors depend on
                # the run's actual steps_map and can only surface at
                # resolve_step_input() time (_resolve_mapping_entry() below).
                # Still worth catching a malformed template (unbalanced
                # `{{`, bad filter name, ...) at load time rather than
                # letting it crash orchestrator/master_agent.py mid-run.
                try:
                    _JINJA_ENV.from_string(entry["expr"])
                except jinja2.TemplateSyntaxError as exc:
                    raise ValueError(
                        f"{path}: step {step_name!r} input_mapping.{mapped_field!r}.expr is not valid Jinja2: {exc}"
                    ) from exc
            input_mapping[mapped_field] = entry

        # The first step's required input fields come from the external
        # trigger payload, not from any earlier step -- nothing to check.
        if i > 0:
            prior_output_fields = {
                prop for source_step in steps for prop in (source_step.output_schema.get("properties") or {})
            }
            for required_field in input_schema.get("required", []):
                if required_field in input_mapping or required_field in prior_output_fields:
                    continue
                raise ValueError(
                    f"{path}: step {step_name!r} requires input field {required_field!r}, but no input_mapping "
                    f"entry maps it and no earlier step's output_schema produces a field named {required_field!r}"
                )

        raw_output = raw_step.get("output") or {}
        output_properties = output_schema.get("properties") or {}
        output: dict[str, dict[str, Any]] = {}
        for out_field, entry in raw_output.items():
            if not isinstance(entry, dict) or set(entry) != {"from"}:
                raise ValueError(
                    f"{path}: step {step_name!r} output.{out_field!r} must have exactly one key 'from', got {entry!r}"
                )
            if out_field not in output_properties:
                raise ValueError(
                    f"{path}: step {step_name!r} output.{out_field!r} is not in this step's output_schema.properties"
                )
            src = entry["from"]
            valid_tool_ref = isinstance(src, str) and src.startswith("tool:") and len(src) > len("tool:")
            if src not in ("model", "tool_log") and not valid_tool_ref:
                raise ValueError(
                    f"{path}: step {step_name!r} output.{out_field!r}.from must be 'model', 'tool_log', or "
                    f"'tool:<tool_name>', got {src!r}"
                )
            output[out_field] = entry

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

        raw_prompt = raw_step.get("prompt")
        prompt: PromptDef | None = None
        if raw_prompt is not None:
            if not isinstance(raw_prompt, dict) or "system" not in raw_prompt or "user" not in raw_prompt:
                raise ValueError(f"{path}: step {step_name!r} prompt must have both 'system' and 'user'")
            # Must be `required`, not just declared in `properties` -- render_prompt()
            # renders with StrictUndefined, so a prompt referencing an optional
            # field would only fail the moment some caller actually omits it,
            # long after this step's definition loaded fine.
            required_fields = set(input_schema.get("required", []))
            for label in ("system", "user"):
                for ref in _INPUT_REF_RE.findall(raw_prompt[label]):
                    if ref not in required_fields:
                        raise ValueError(
                            f"{path}: step {step_name!r} prompt.{label} references input.{ref!r}, which must be "
                            f"in this step's input_schema.required (an optional field can't be safely referenced -- "
                            f"rendering would raise if a caller omits it)"
                        )
            prompt = PromptDef(system=raw_prompt["system"], user=raw_prompt["user"])

        steps.append(
            StepDef(
                name=step_name,
                command_type=command_type,
                completion_type=completion_type,
                input_schema=input_schema,
                output_schema=output_schema,
                model=model,
                memory_write=tuple(memory_write),
                input_mapping=input_mapping,
                prompt=prompt,
                output=output,
            )
        )

    return WorkflowDef(name=name, steps=tuple(steps))
