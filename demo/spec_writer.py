"""Spec-writing layer for the UI (docs/ui-backend-integration-plan.md P2, D1).

The UI edits exactly two kinds of file -- a workflow YAML
(workflows/definitions/*.yaml) and mcp_servers/policy.yaml. There is no
third "overrides" file: model/prompt/output already live in workflow YAML
(P1/P5/P6 of docs/generic-agent-runtime-plan.md), and tool/memory grants
already live in policy.yaml. agents/live_spec.py (P1) is the read side that
notices these files changed; this module is the write side.

Every write here follows the same shape: load the file with ruamel.yaml (a
round-trip loader -- a plain load/dump through PyYAML would silently delete
policy.yaml's governance comments), mutate the in-memory document, dump to
`<path>.tmp`, validate the tmp file with the same loader
agents/live_spec.py itself uses, and only then os.replace() it over the
original. Any failure at any step leaves the original file untouched -- a
running runtime hot-reloads these files (P1), so a bad write can't be
allowed to land even transiently.

Deliberately does NOT let the UI edit input_schema/output_schema on a step
that already belongs to the main workflow (only model/prompt, via
update_step()) -- docs/ui-backend-integration-plan.md §6: master and every
worker hold their own process-lifetime WorkflowDef snapshot, so a schema
edit there would desync from what's actually enforced until a restart. A
standalone agent (update_agent_schema()) has no such restriction because a
UI-built agent isn't dispatched by master/worker at all (see issue #41) --
every request re-reads its agent_<name>.yaml fresh, so there's no snapshot
to desync from.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import LiteralScalarString

from mcp_servers.policy import load_policy
from orchestrator.workflow_def import load_workflow_def, resolve_workflow_def_path
from persistence.memory_policy import load_memory_policy

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFINITIONS_DIR = _REPO_ROOT / "workflows" / "definitions"
_POLICY_PATH = _REPO_ROOT / "mcp_servers" / "policy.yaml"

_AGENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
# Sibling key under memory.<principal> that set_memory(enabled=False) parks
# the original `read` patterns in. load_memory_policy() only reads
# read/write off each grant (persistence/memory_policy.py's dict.get calls),
# so an extra key here is silently ignored by every reader -- no parser
# change needed to make this round-trip safely.
_DISABLED_READ_KEY = "_disabled_read"

_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.width = 4096  # don't let long prompt lines get folded/rewrapped on dump
# Matches every hand-written file's block-sequence style ("- item" indented
# 2 past its key, not flush with it) -- ruamel's own default reindents every
# multi-line list the first time it touches a file, which would turn one
# intended one-line diff into a noisy whole-file one.
_yaml.indent(mapping=2, sequence=4, offset=2)
# ponytail: manual column-alignment spaces after "key:" (e.g. "stt:      {...}")
# collapse to a single space on the first write to a given block -- ruamel
# has no concept of "developer padded this for alignment" to preserve.
# One-time cosmetic diff per block, not a parsing or data change; not worth
# hand-rolling alignment-preservation for a local demo.


class SpecWriteError(ValueError):
    """A write was rejected before touching the real file -- either the new
    content doesn't parse as a valid spec, or it collides with an existing
    agent's identity. The original file is guaranteed untouched."""


def _agent_path(name: str) -> Path:
    return _DEFINITIONS_DIR / f"agent_{name}.yaml"


def _scalar(text: str) -> str | LiteralScalarString:
    """A multi-line prompt as a `|-` literal block (exact newlines
    preserved, matches every hand-written prompt in this repo already);
    single-line text stays a plain scalar. Assigning a raw multi-line str
    would dump as a folded plain scalar, which YAML re-reads with internal
    newlines collapsed to spaces -- silently changing the prompt's meaning."""
    return LiteralScalarString(text) if "\n" in text else text


def _load(path: Path) -> Any:
    with open(path, encoding="utf-8") as f:
        return _yaml.load(f)


def _atomic_write(path: Path, data: Any, *, validate: Callable[[Path], None]) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        _yaml.dump(data, f)
    try:
        validate(tmp_path)
    except Exception as exc:
        tmp_path.unlink(missing_ok=True)
        raise SpecWriteError(f"{path}: {exc}") from exc
    os.replace(tmp_path, path)


def _validate_workflow(path: Path) -> None:
    load_workflow_def(str(path))


def _validate_policy(path: Path) -> None:
    load_policy(str(path))
    load_memory_policy(str(path))


def _other_step_names(exclude: Path) -> set[str]:
    """Every step name declared anywhere except `exclude` -- checked before
    create_agent() lands a new file, so a name collision is reported as a
    normal SpecWriteError instead of surfacing later as agents/live_spec.py
    refusing its *entire* next reload (which would also take down every
    already-working agent, not just the new one)."""
    names: set[str] = set()
    candidates = [Path(resolve_workflow_def_path()), *sorted(_DEFINITIONS_DIR.glob("agent_*.yaml"))]
    for path in candidates:
        if path.resolve() == exclude.resolve() or not path.exists():
            continue
        names.update(s.name for s in load_workflow_def(str(path)).steps)
    return names


def update_step(workflow_path: str, step_name: str, *, model: str | None = None, prompt: dict[str, str] | None = None) -> None:
    """Edit an existing step's model and/or prompt in place. `prompt` may
    give just one of "system"/"user" to change only that half. No
    schema/output param on purpose -- see module docstring."""
    path = Path(workflow_path)
    doc = _load(path)
    step = next((s for s in doc.get("steps") or [] if s.get("name") == step_name), None)
    if step is None:
        raise SpecWriteError(f"{path}: no step named {step_name!r}")

    if model is not None:
        step["model"] = model
    if prompt is not None:
        current = step.setdefault("prompt", {})
        if "system" in prompt:
            current["system"] = _scalar(prompt["system"])
        if "user" in prompt:
            current["user"] = _scalar(prompt["user"])

    _atomic_write(path, doc, validate=_validate_workflow)


def update_agent_schema(
    name: str,
    *,
    input_schema: dict[str, Any] | None = None,
    output_schema: dict[str, Any] | None = None,
    output: dict[str, dict[str, str]] | None = None,
) -> None:
    """Replace a standalone agent's input/output schema and/or output
    declarations wholesale. Only ever targets agent_<name>.yaml -- see
    module docstring for why a step inside the shared workflow doesn't get
    this. Caller (demo/api.py) is responsible for refusing the request
    before it gets here if `name` isn't actually a standalone agent."""
    path = _agent_path(name)
    if not path.exists():
        raise SpecWriteError(f"no such agent {name!r} ({path} does not exist)")
    doc = _load(path)
    step = doc["steps"][0]  # create_agent() only ever writes a single-step file

    if input_schema is not None:
        step["input_schema"] = input_schema
    if output_schema is not None:
        step["output_schema"] = output_schema
    if output is not None:
        step["output"] = output

    _atomic_write(path, doc, validate=_validate_workflow)


def set_tools(principal: str, tools: list[str]) -> None:
    """Replace `principal`'s entire grant with a flat `allow: [tools]`,
    dropping any `roles:`/`deny:` it had. Simplest contract for a UI that
    shows a checkbox per catalog tool and PUTs the full checked set back --
    "what's checked is what it can call", not a diff against role bundles."""
    doc = _load(_POLICY_PATH)
    principals = doc.setdefault("principals", {})
    grant = {"allow": sorted(set(tools))}
    if principal in principals:
        principals[principal] = grant  # in-place value swap, key position (and any comment tied to it) is untouched
    else:
        # NOT `principals[principal] = grant` -- plain assignment of a new
        # key appends at the end, which silently steals whatever
        # dangling/trailing comment ruamel attached to "after the last
        # principal, before the next top-level key" (this repo's real
        # policy.yaml has exactly such a block: the "Examples of the other
        # two shapes" comment before `memory:`). delete_agent() removing
        # that same newly-last key later would then delete the comment right
        # along with it -- reproduced and confirmed while building this.
        # Inserting at a fixed position (0) instead means a new principal is
        # never adjacent to that trailing comment in the first place, so
        # neither creating nor later deleting it can touch that comment.
        principals.insert(0, principal, grant)
    _atomic_write(_POLICY_PATH, doc, validate=_validate_policy)


def set_memory(principal: str, enabled: bool) -> None:
    """Toggle `principal`'s existing memory grant off/on. Does NOT declare a
    new grant -- a principal with no `memory:` entry (every freshly
    create_agent()'d agent) is already fail-closed, so disabling it is a
    no-op and enabling it raises (there's no namespace to turn on)."""
    doc = _load(_POLICY_PATH)
    memory = doc.get("memory") or {}
    grant = memory.get(principal)
    if grant is None:
        if enabled:
            raise SpecWriteError(
                f"{principal!r} has no memory grant to enable -- declare one under policy.yaml's memory: section first"
            )
        return

    if enabled:
        disabled = grant.pop(_DISABLED_READ_KEY, None)
        if disabled is None:
            return  # already enabled, nothing parked to restore
        grant["read"] = disabled
    else:
        if grant.get("read"):
            grant[_DISABLED_READ_KEY] = grant["read"]
        grant["read"] = []

    _atomic_write(_POLICY_PATH, doc, validate=_validate_policy)


def create_agent(
    name: str,
    *,
    model: str,
    prompt: dict[str, str],
    input_schema: dict[str, Any],
    output_schema: dict[str, Any],
    output: dict[str, dict[str, str]],
    tools: list[str] = (),
) -> str:
    """A brand-new single-step workflow file, `name`'s own principal/tool
    grant, and nothing else -- no memory grant (see set_memory()'s
    docstring for why that's fine), not wired into any other workflow (see
    module docstring). Returns `name` back for convenience."""
    if not _AGENT_NAME_RE.match(name):
        raise SpecWriteError(
            f"{name!r} is not a valid agent name -- must match {_AGENT_NAME_RE.pattern!r} "
            "(lowercase letters, digits, underscores, starting with a letter)"
        )
    if "system" not in prompt or "user" not in prompt:
        raise SpecWriteError("prompt must have both 'system' and 'user'")

    path = _agent_path(name)
    if path.exists():
        raise SpecWriteError(f"agent {name!r} already exists ({path}) -- use update_step() or delete_agent() first")
    if name in _other_step_names(exclude=path):
        raise SpecWriteError(f"step name {name!r} is already used by another workflow/agent")

    doc = {
        "name": f"agent_{name}",
        "steps": [
            {
                "name": name,
                "command_type": f"{name}.run",
                "completion_type": f"{name}.completed",
                "model": model,
                "input_schema": input_schema,
                "output_schema": output_schema,
                "prompt": {"system": _scalar(prompt["system"]), "user": _scalar(prompt["user"])},
                "output": output,
            }
        ],
    }
    _atomic_write(path, doc, validate=_validate_workflow)

    try:
        set_tools(name, list(tools))
    except Exception:
        # Partial failure here is still safe (no principal grant = fail-closed,
        # not "runs with the wrong permissions"), but an orphaned agent file
        # nobody asked for is confusing, so clean it up rather than leave it.
        path.unlink(missing_ok=True)
        raise

    return name


def delete_agent(name: str) -> None:
    """Removes the agent's file and its policy.yaml principal/memory
    entries. No validate-then-land dance needed for the file removal itself
    -- deleting one single-step file can't invalidate any other file (that
    isolation is the whole reason create_agent() uses one file per agent)."""
    path = _agent_path(name)
    if not path.exists():
        raise SpecWriteError(f"no such agent {name!r} ({path} does not exist)")
    path.unlink()

    doc = _load(_POLICY_PATH)
    principals = doc.get("principals") or {}
    memory = doc.get("memory") or {}
    changed = False
    if name in principals:
        del principals[name]
        changed = True
    if name in memory:
        del memory[name]
        changed = True
    if changed:
        _atomic_write(_POLICY_PATH, doc, validate=_validate_policy)
