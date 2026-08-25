"""Smoke test for demo/spec_writer.py -- run with:
    uv run python -m demo.spec_writer_smoke_test

Pure file I/O against the real workflows/definitions/ and mcp_servers/policy.yaml
(the same files agents/live_spec.py hot-reloads), no Postgres, no LLM, no MCP
subprocess. Everything this test creates is prefixed `_spec_writer_smoke_`
and torn down in a `finally`, including a policy.yaml restore, so it's safe
to run against the same policy.yaml a live runtime has open -- worst case is
one harmless reload while this test's own edits are in place.
"""

from __future__ import annotations

import shutil

from mcp_servers.policy import load_policy, resolve_allowed
from orchestrator.workflow_def import load_workflow_def

from demo import spec_writer as sw
from demo.spec_writer import SpecWriteError

_NAME = "smoke_spec_writer_probe"
_INPUT_SCHEMA = {
    "type": "object",
    "required": ["expression"],
    "properties": {"expression": {"type": "string"}},
    "additionalProperties": False,
}
_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["result"],
    "properties": {"result": {"type": "string"}},
    "additionalProperties": False,
}


def _cleanup() -> None:
    path = sw._agent_path(_NAME)
    if path.exists():
        path.unlink()
    doc = sw._load(sw._POLICY_PATH)
    changed = False
    for section in ("principals", "memory"):
        bucket = doc.get(section) or {}
        if _NAME in bucket:
            del bucket[_NAME]
            changed = True
    if changed:
        sw._atomic_write(sw._POLICY_PATH, doc, validate=sw._validate_policy)
    # In case a failure test left a stray .tmp behind.
    tmp = path.with_suffix(path.suffix + ".tmp")
    if tmp.exists():
        tmp.unlink()


def main() -> None:
    policy_backup = sw._POLICY_PATH.read_bytes()
    _cleanup()
    try:
        created = sw.create_agent(
            _NAME,
            model="claude-haiku",
            prompt={"system": "你可以呼叫 calc__evaluate 計算算式。\n只回覆 JSON。", "user": "{{ input.expression }}"},
            input_schema=_INPUT_SCHEMA,
            output_schema=_OUTPUT_SCHEMA,
            output={"result": {"from": "model"}},
            tools=["calc__evaluate"],
        )
        assert created == _NAME
        path = sw._agent_path(_NAME)
        assert path.exists(), path
        print("[spec_writer] OK -- create_agent() writes a new single-step file")

        workflow_def = load_workflow_def(str(path))
        assert workflow_def.name == f"agent_{_NAME}"
        step = workflow_def.step(_NAME)
        assert step.model == "claude-haiku", step.model
        assert step.output == {"result": {"from": "model"}}, step.output
        print("[spec_writer] OK -- load_workflow_def() reads back the created spec correctly")

        policy = load_policy(str(sw._POLICY_PATH))
        allowed = resolve_allowed(policy, _NAME)
        assert "calc__evaluate" in allowed.allow, allowed
        assert len(allowed.allow) == 1, allowed  # exactly the requested set, nothing extra
        print("[spec_writer] OK -- create_agent() grants exactly the requested tools")

        # A name already claimed by an existing step must be rejected before
        # any file is written.
        try:
            sw.create_agent(
                "check", model="claude-haiku", prompt={"system": "x", "user": "y"},
                input_schema=_INPUT_SCHEMA, output_schema=_OUTPUT_SCHEMA, output={},
            )
            raise AssertionError("expected SpecWriteError for a colliding step name")
        except SpecWriteError:
            pass
        print("[spec_writer] OK -- a step name collision is rejected")

        # update_step() only touches model/prompt.
        sw.update_step(str(path), _NAME, model="local-qwen")
        assert load_workflow_def(str(path)).step(_NAME).model == "local-qwen"
        sw.update_step(str(path), _NAME, prompt={"user": "{{ input.expression }} 請算出來"})
        updated = load_workflow_def(str(path)).step(_NAME)
        assert "system" in updated.prompt.system or updated.prompt.system  # untouched half survives
        assert updated.prompt.user == "{{ input.expression }} 請算出來"
        print("[spec_writer] OK -- update_step() edits model/prompt independently and preserves the rest")

        # update_agent_schema() replaces input/output schema + output decl.
        # Keeps "expression" as a property -- prompt.user (set just above)
        # still references {{ input.expression }}, and load_workflow_def()'s
        # own reference check correctly rejects a schema that would orphan
        # that reference, same guard a real UI edit would hit.
        new_input = {
            "type": "object", "required": ["expression"],
            "properties": {"expression": {"type": "string"}, "note": {"type": "string"}},
            "additionalProperties": False,
        }
        sw.update_agent_schema(_NAME, input_schema=new_input, output={"result": {"from": "tool_log"}})
        reloaded = load_workflow_def(str(path)).step(_NAME)
        assert reloaded.input_schema == new_input, reloaded.input_schema
        assert reloaded.output_schema == _OUTPUT_SCHEMA, "omitted param must leave output_schema untouched"
        assert reloaded.output == {"result": {"from": "tool_log"}}, reloaded.output
        print("[spec_writer] OK -- update_agent_schema() replaces only the given parts")

        try:
            sw.update_agent_schema("check", input_schema=new_input)
            raise AssertionError("expected SpecWriteError -- 'check' has no agent_check.yaml to edit")
        except SpecWriteError:
            pass
        print("[spec_writer] OK -- update_agent_schema() rejects a name with no standalone agent file")

        # set_tools() replaces the whole grant.
        sw.set_tools(_NAME, ["calc__evaluate", "lookup__query_company_profile"])
        allowed = resolve_allowed(load_policy(str(sw._POLICY_PATH)), _NAME)
        assert allowed.allow == frozenset({"calc__evaluate", "lookup__query_company_profile"}), allowed
        print("[spec_writer] OK -- set_tools() replaces the grant with exactly the given set")

        # A broken write (invalid model name) must not touch the file at all.
        before = path.read_text()
        try:
            sw.update_step(str(path), _NAME, model="not-a-real-model")
            raise AssertionError("expected SpecWriteError for an unknown model")
        except SpecWriteError:
            pass
        assert path.read_text() == before, "a failed write must leave the original file byte-for-byte unchanged"
        assert not path.with_suffix(path.suffix + ".tmp").exists(), "a failed write must not leave a .tmp file behind"
        print("[spec_writer] OK -- an invalid write is rejected and the original file is untouched")

        # set_memory(): nothing to disable/enable for an agent with no memory
        # grant at all.
        sw.set_memory(_NAME, enabled=False)  # no-op, must not raise
        try:
            sw.set_memory(_NAME, enabled=True)
            raise AssertionError("expected SpecWriteError enabling memory with no grant declared")
        except SpecWriteError:
            pass
        print("[spec_writer] OK -- set_memory() on a grant-less principal no-ops off, rejects on")

        # set_memory() round-trip against a principal that already has one
        # (reuse `check`'s real grant, restore it in `finally`).
        original_check_read = _read_grant(sw._POLICY_PATH, "check")
        assert original_check_read, "expected the real check principal to already have memory.read patterns"
        sw.set_memory("check", enabled=False)
        assert _read_grant(sw._POLICY_PATH, "check") == [], "disabled principal must read as empty"
        sw.set_memory("check", enabled=True)
        assert _read_grant(sw._POLICY_PATH, "check") == original_check_read, "re-enabling must restore the exact original patterns"
        print("[spec_writer] OK -- set_memory() off/on round-trips check's real grant losslessly")

        sw.delete_agent(_NAME)
        assert not path.exists()
        policy = load_policy(str(sw._POLICY_PATH))
        assert resolve_allowed(policy, _NAME).allow == frozenset()
        print("[spec_writer] OK -- delete_agent() removes the file and its policy grant")

        # Regression: a freshly-created principal is always the new *last*
        # key in `principals:` (plain dict/CommentedMap assignment appends).
        # ruamel attaches the real policy.yaml's trailing "Examples of the
        # other two shapes" comment (the block between the last principal and
        # the next top-level `memory:` key) to whichever key is last -- a
        # naive `principals[name] = ...` followed by `del principals[name]`
        # deletes that comment along with the key, even though the key
        # itself never had anything to do with it. Reproduced once while
        # building set_tools(); this guards against it coming back.
        assert "Examples of the other two shapes" in sw._POLICY_PATH.read_text(), (
            "create_agent()+delete_agent() must not delete an unrelated trailing comment in policy.yaml"
        )
        print("[spec_writer] OK -- a create+delete cycle doesn't strand-delete policy.yaml's trailing comment")

    finally:
        _cleanup()
        # Belt-and-suspenders: restore policy.yaml byte-for-byte in case any
        # assertion above failed mid-sequence and left it in an intermediate
        # (still valid, but not identical) state.
        if sw._POLICY_PATH.read_bytes() != policy_backup:
            sw._POLICY_PATH.write_bytes(policy_backup)

    print("\nAll spec_writer smoke tests passed.")


def _read_grant(policy_path, principal: str) -> list[str] | None:
    doc = sw._load(policy_path)
    memory = doc.get("memory") or {}
    grant = memory.get(principal)
    if grant is None:
        return None
    return list(grant.get("read") or [])


if __name__ == "__main__":
    main()
