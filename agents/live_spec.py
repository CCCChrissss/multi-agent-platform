"""Hot-reloadable spec registry for agents/runtime.py
(docs/ui-backend-integration-plan.md P1).

Before this, every agent's spec was read exactly once -- `load_workflow_def()`
at module import, `load_policy()` in the lifespan -- so changing a step's
model or a principal's tool grants meant restarting the process. That was
fine while both files were only ever edited by hand between runs; it isn't
once the UI writes them mid-session (P2). This module keeps one immutable
snapshot of {steps, policy, memory_policy} and swaps in a fresh one when any
source file's mtime changes.

Two sources of steps, deliberately:
  - the process's main workflow (`resolve_workflow_def_path()`), unchanged;
  - every `workflows/definitions/agent_*.yaml`, one single-step file per
    agent built from scratch through the UI.

Why UI-built agents get their own file rather than being appended to the
main workflow: `load_workflow_def()` requires every non-first step's required
input fields to be resolvable (an `input_mapping` entry, or a same-named
field in some earlier step's output_schema). A brand-new agent nobody has
wired up yet necessarily fails that check -- and because the check runs over
the whole file, it would take the main workflow down with it. A single-step
file is exempt (a first step's inputs come from the external trigger), so it
can be saved in any half-configured state without endangering anything else.
The main workflow also stays the exact set of steps master/worker will
dispatch, which a UI-built agent is not (see issue #41).

Failure posture: a reload that raises leaves the previous good snapshot in
place and records the error rather than taking the runtime down -- the UI can
write a broken spec, and "the file you just saved is invalid" has to be a
message, not a dead process. Only the very first load is fatal.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from mcp_servers.policy import Policy, load_policy
from orchestrator.workflow_def import StepDef, load_workflow_def, resolve_workflow_def_path
from persistence.memory_policy import MemoryPolicy, load_memory_policy

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFINITIONS_DIR = _REPO_ROOT / "workflows" / "definitions"
_POLICY_PATH = _REPO_ROOT / "mcp_servers" / "policy.yaml"

AGENT_FILE_GLOB = "agent_*.yaml"
"""Naming convention for a UI-built single-step agent's own workflow file --
`demo/spec_writer.py` (P2) writes these, this module discovers them."""


@dataclass(frozen=True)
class StepSpec:
    """One step plus which workflow file it came from. `workflow_name` is what
    scope-keyed features (procedural-memory injection, memory_write rules) use
    to namespace this step's memory, so it has to travel with the step now that
    one process serves steps from more than one workflow."""

    workflow_name: str
    step: StepDef
    source: Path


@dataclass(frozen=True)
class SpecSnapshot:
    steps: dict[str, StepSpec]
    policy: Policy
    memory_policy: MemoryPolicy
    signature: tuple[tuple[str, int], ...]


class LiveSpec:
    def __init__(
        self,
        *,
        workflow_path: str | None = None,
        policy_path: Path = _POLICY_PATH,
        definitions_dir: Path = _DEFINITIONS_DIR,
    ) -> None:
        self._workflow_path = Path(workflow_path or resolve_workflow_def_path())
        self._policy_path = policy_path
        # Overridable so agents/live_spec_smoke_test.py can point at a tmpdir:
        # its fixtures are named agent_*.yaml, which is exactly what a running
        # runtime would pick up and try to serve if they were written here.
        self._definitions_dir = definitions_dir
        self._snapshot: SpecSnapshot | None = None
        self._failed_signature: tuple[tuple[str, int], ...] | None = None
        self.last_error: str | None = None

    @property
    def main_workflow_path(self) -> Path:
        """The process's WORKFLOW_DEF_PATH workflow, as opposed to the
        UI-built single-step agent files -- agents/lifespan.py connects
        gateways for this one's steps eagerly at startup."""
        return self._workflow_path

    @property
    def snapshot(self) -> SpecSnapshot:
        if self._snapshot is None:
            raise RuntimeError("LiveSpec.refresh() has never succeeded -- call it once at startup")
        return self._snapshot

    @property
    def policy(self) -> Policy:
        return self.snapshot.policy

    @property
    def memory_policy(self) -> MemoryPolicy:
        return self.snapshot.memory_policy

    def step_names(self) -> list[str]:
        return sorted(self.snapshot.steps)

    def get(self, step_name: str) -> StepSpec:
        """Raises KeyError for an unknown step -- agents/runtime.py turns that
        into a 404."""
        return self.snapshot.steps[step_name]

    def refresh(self) -> bool:
        """True if a new snapshot was installed. Fully synchronous on purpose:
        an async caller can't be interleaved mid-swap by another request, so
        the "build a whole new snapshot, then assign it" sequence below is
        atomic with respect to the event loop without needing a lock.

        # ponytail: blocking stat()/open() on the request path -- microseconds
        # against a multi-second LLM call, revisit if this ever fronts
        # something latency-sensitive.
        """
        signature = self._signature()
        if self._snapshot is not None and signature == self._snapshot.signature:
            return False
        # A file that fails to parse stays broken until someone edits it
        # again, so don't re-attempt (and re-log) the same failure on every
        # single request.
        if signature == self._failed_signature:
            return False

        try:
            snapshot = self._load(signature)
        except Exception as exc:
            if self._snapshot is None:
                raise
            self._failed_signature = signature
            self.last_error = f"{type(exc).__name__}: {exc}"
            print(f"[live_spec] keeping previous spec, reload failed -- {self.last_error}", file=sys.stderr, flush=True)
            return False

        self._snapshot = snapshot
        self._failed_signature = None
        self.last_error = None
        return True

    def _sources(self) -> list[Path]:
        agent_files = sorted(self._definitions_dir.glob(AGENT_FILE_GLOB))
        return [self._policy_path, self._workflow_path, *agent_files]

    def _signature(self) -> tuple[tuple[str, int], ...]:
        """Identity of the current on-disk state. Includes the file list
        itself, not just mtimes, so adding or deleting an agent_*.yaml counts
        as a change. Readers never see a half-written file: `demo/spec_writer.py`
        writes to a temp file and os.replace()s it, which is atomic."""
        return tuple((str(p), p.stat().st_mtime_ns) for p in self._sources() if p.exists())

    def _load(self, signature: tuple[tuple[str, int], ...]) -> SpecSnapshot:
        policy = load_policy(str(self._policy_path))
        memory_policy = load_memory_policy(str(self._policy_path))

        steps: dict[str, StepSpec] = {}
        for path in [self._workflow_path, *sorted(self._definitions_dir.glob(AGENT_FILE_GLOB))]:
            if not path.exists():
                continue
            workflow_def = load_workflow_def(str(path))
            for step in workflow_def.steps:
                if step.name in steps:
                    # Step name is the route path and the RBAC principal, so a
                    # collision isn't a merge conflict to resolve -- it's two
                    # different agents claiming one identity. Refuse the whole
                    # reload rather than let first-or-last-wins decide.
                    raise ValueError(
                        f"duplicate step name {step.name!r}: declared in both "
                        f"{steps[step.name].source} and {path}"
                    )
                steps[step.name] = StepSpec(workflow_name=workflow_def.name, step=step, source=path)

        return SpecSnapshot(steps=steps, policy=policy, memory_policy=memory_policy, signature=signature)
