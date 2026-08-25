"""Smoke test for agents/live_spec.py -- run with:
    uv run python -m agents.live_spec_smoke_test

No Postgres, no LLM, no MCP subprocess: this is pure file-watching and
YAML-loading logic, so it runs in well under a second.

Fixtures go in a tmpdir rather than the real workflows/definitions/, because
they are named agent_*.yaml -- exactly what a running agents/runtime.py would
discover and start serving.

The cases that matter are the failure ones. A UI that writes specs will
eventually write a broken one, and the whole point of the snapshot design is
that the runtime keeps serving the last good spec instead of falling over.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from agents.live_spec import LiveSpec

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MAIN_WORKFLOW = _REPO_ROOT / "workflows" / "definitions" / "stt_exclusion_notify.yaml"
_POLICY = _REPO_ROOT / "mcp_servers" / "policy.yaml"

_CALC_AGENT = """\
name: agent_calc_probe
steps:
  - name: calc_probe
    command_type: calc_probe.run
    completion_type: calc_probe.completed
    model: {model}
    input_schema:
      type: object
      required: [expression]
      properties:
        expression: {{type: string}}
      additionalProperties: false
    output_schema:
      type: object
      required: [result]
      properties:
        result: {{type: string}}
      additionalProperties: false
    prompt:
      system: 你可以呼叫 calc__evaluate 計算算式。
      user: "{{{{ input.expression }}}}"
    output:
      result: {{from: model}}
"""


def _write(path: Path, text: str) -> None:
    path.write_text(text)


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="live_spec_smoke_"))
    try:
        live = LiveSpec(workflow_path=str(_MAIN_WORKFLOW), policy_path=_POLICY, definitions_dir=tmp)

        assert live.refresh() is True
        assert live.step_names() == ["check", "notified", "stt"], live.step_names()
        assert live.get("check").workflow_name == "stt_exclusion_notify"
        print("[live_spec] OK -- first load exposes the main workflow's steps")

        assert live.refresh() is False, "unchanged sources must not rebuild the snapshot"
        print("[live_spec] OK -- no-op refresh when nothing changed")

        # A brand-new agent appears as its own single-step file.
        agent_file = tmp / "agent_calc_probe.yaml"
        _write(agent_file, _CALC_AGENT.format(model="claude-haiku"))
        assert live.refresh() is True
        assert "calc_probe" in live.step_names(), live.step_names()
        spec = live.get("calc_probe")
        assert spec.step.model == "claude-haiku", spec.step.model
        assert spec.workflow_name == "agent_calc_probe", spec.workflow_name
        print("[live_spec] OK -- a new agent_*.yaml is discovered without a restart")

        # Editing it takes effect on the next refresh.
        _write(agent_file, _CALC_AGENT.format(model="local-qwen"))
        assert live.refresh() is True
        assert live.get("calc_probe").step.model == "local-qwen", live.get("calc_probe").step.model
        print("[live_spec] OK -- an edited spec is picked up on the next refresh")

        # A broken write must not take the runtime down, and must not lose the
        # steps that were already being served.
        _write(agent_file, "name: agent_calc_probe\nsteps: [{name: calc_probe}]\n")
        assert live.refresh() is False, "a failed reload must not report success"
        assert live.last_error is not None and "ValueError" in live.last_error, live.last_error
        assert live.get("calc_probe").step.model == "local-qwen", "previous good snapshot must survive"
        assert live.step_names() == ["calc_probe", "check", "notified", "stt"], live.step_names()
        print("[live_spec] OK -- invalid spec keeps the previous snapshot and records the error")

        # ...and it stops retrying the same broken file every request.
        live.last_error = "sentinel"
        assert live.refresh() is False
        assert live.last_error == "sentinel", "an unchanged failure should not be re-attempted"
        print("[live_spec] OK -- an unchanged broken file is not re-parsed on every refresh")

        # Fixing it recovers without a restart.
        _write(agent_file, _CALC_AGENT.format(model="claude-haiku"))
        assert live.refresh() is True
        assert live.last_error is None
        assert live.get("calc_probe").step.model == "claude-haiku"
        print("[live_spec] OK -- fixing the file recovers on the next refresh")

        # Two files claiming one step name is two agents claiming one identity
        # (route path and RBAC principal both key off it), so the whole reload
        # is refused rather than letting either win.
        _write(tmp / "agent_dupe.yaml", _CALC_AGENT.format(model="claude-haiku"))
        assert live.refresh() is False
        assert live.last_error is not None and "duplicate step name" in live.last_error, live.last_error
        print("[live_spec] OK -- duplicate step names across files refuse the reload")

        (tmp / "agent_dupe.yaml").unlink()
        agent_file.unlink()
        assert live.refresh() is True
        assert live.step_names() == ["check", "notified", "stt"], live.step_names()
        print("[live_spec] OK -- deleting an agent file removes its step")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\nAll live_spec smoke tests passed.")


if __name__ == "__main__":
    main()
