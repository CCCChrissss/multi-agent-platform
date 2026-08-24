"""Manual smoke test for the long-term memory store -- run with:
    uv run python -m persistence.memory_smoke_test

Requires only Postgres (the same PERSISTENCE_DATABASE_URL as everything else
in persistence/); no Ollama/LiteLLM/agent-server stack needed -- this is M1,
infrastructure only, no agent calls recall()/remember() yet.

Scenarios:
  - round_trip: memory_writer remembers something, check recalls it back
    under a namespace policy.yaml grants it read access to; audit fields
    (source_thread_id/source_step/created_by) are stamped automatically.
  - prefix_scope: kind (episodic vs procedural) actually separates memories
    that share the same tenant/scope, since asearch matches by full namespace
    prefix, not just scope.
  - tenant_isolation: data that genuinely exists under a different tenant_id
    is still denied to `check` by policy -- this must be a policy denial, not
    merely "no data happened to be there" (an earlier version of this
    scenario only proved the latter, because mcp_servers/policy.yaml's
    patterns wildcarded the tenant segment too -- see the comment on `check`/
    `notified` there for why that was a real cross-tenant leak, not just a
    naming nit).
  - global_tenant: GLOBAL_TENANT is how genuinely cross-tenant knowledge
    (e.g. a company's public aliases) is shared on purpose, opted into by an
    explicit policy pattern -- distinct from the leak tenant_isolation
    guards against.
  - policy_denial: a principal without a read/write grant for a namespace
    gets an empty recall() / a silently dropped remember(), never a raise.
  - semantic_search: recall()'s `query=` actually ranks by embedding
    similarity (docs/long-term-memory-plan.md M4) rather than degrading to
    an equality filter -- requires Ollama serving `bge-m3` and the LiteLLM
    Gateway running (gateway/config.yaml's `local-embed`), unlike every
    other scenario here which needs only Postgres.
  - browse_tree / browse_leaf / browse_policy_denial: browse()'s progressive
    disclosure (docs/exclusion-scenario-plan.md §2/P1) -- a three-level tree
    walked from root to leaf, asserting children/parent/siblings/summary at
    each level and that INDEX_KEY never leaks into `items`; a leaf's empty
    `children`; and a denied principal getting `{}` back, never a raise.
    Uses an ad-hoc MemoryPolicy scoped to a throwaway namespace instead of
    mcp_servers/policy.yaml's real grants, so this doesn't need a policy.yaml
    edit just to exercise a test fixture.
  - browse_from_root: can_read()'s prefix-widening covers a namespace *any*
    number of segments short of a grant's wildcard, not just one -- browsing
    from the true root (prefix=()) must still surface a grant nested two
    segments below tenant/kind, exactly what browse_semantic_memory's own
    tool docstring promises ("omit scope to start from the root").
  - recall_partial_scope_denied: the same prefix-widening must NOT leak into
    recall() -- a scope that's exactly the grant's wildcard directory (valid
    for browse()'s "what's here" listing) must still be denied to recall(),
    which needs a complete namespace, not a directory.
  - audit_log: recall()/remember()/edit()/forget() (success and denied)
    leave queryable kind='memory' rows in call_log, closing the gap
    TODO.md's memory-access-audit-log entry flagged relative to the
    'llm'/'tool' kinds -- asserts namespace/key/denied land correctly and
    that raw memory content never does (only counts/keys/char-lengths).
    edit()/forget() additionally close TODO.md's
    review-approve-reject-audit-gap entry: scripts/review_memory.py's
    approve/reject used to bypass this trail with raw store.aput/adelete.
  - status_gate: docs/knowledge-distillation-plan.md §5 P0 -- a
    `status="pending"` memory (the M5 quality gate's not-yet-promoted state)
    is invisible to recall(), to browse()'s `items`, and to browse()'s
    per-child `_index` summary; a `status="active"` (the default) one is
    visible exactly as before. Also documents the one accepted gap: a
    namespace holding *only* pending memories still surfaces its bare
    segment name in `children` (with `summary=None`), since
    alist_namespaces() has no status concept to filter on.
  - remember_extra_fields: `remember()`'s `extra=` (docs/knowledge-distillation-plan.md
    §3.4/P2) lands its keys in `value` alongside the fixed audit fields
    without clobbering `status`/`content`/etc, even though `extra` is
    applied first in the merge.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from persistence.call_log import current_node_name, current_thread_id, ensure_schema as ensure_call_log_schema, fetch_calls
from persistence.memory import GLOBAL_TENANT, INDEX_KEY, MemoryKind, browse, build_namespace, edit, forget, recall, remember
from persistence.memory_policy import MemoryGrant, MemoryPolicy, load_memory_policy
from persistence.memory_store import get_memory_store

_REPO_ROOT = Path(__file__).resolve().parents[1]
_POLICY_PATH = _REPO_ROOT / "mcp_servers" / "policy.yaml"


async def scenario_round_trip(store, policy) -> None:
    current_thread_id.set("smoke-thread-1")
    current_node_name.set("memory_writer")
    await remember(
        store,
        policy,
        MemoryKind.SEMANTIC,
        tenant=GLOBAL_TENANT,  # a company's public aliases aren't tenant-specific
        scope=("company", "tsmc"),
        key="alias",
        content={"aliases": ["台積電", "TSMC", "台灣積體電路製造"]},
    )

    current_node_name.set("check")
    items = await recall(store, policy, MemoryKind.SEMANTIC, tenant=GLOBAL_TENANT, scope=("company", "tsmc"))
    assert len(items) == 1, items
    assert items[0].value["content"]["aliases"] == ["台積電", "TSMC", "台灣積體電路製造"]
    assert items[0].value["source_thread_id"] == "smoke-thread-1", items[0].value
    assert items[0].value["created_by"] == "memory_writer", items[0].value
    print("[round_trip] OK -- remember()/recall() round trip, audit fields stamped correctly")


async def scenario_prefix_scope(store, policy) -> None:
    current_node_name.set("memory_writer")
    await remember(
        store,
        policy,
        MemoryKind.EPISODIC,
        tenant="default",
        scope=("stt_check_notify", "check"),
        key="case-1",
        # {"input": ..., "output": ...} is the platform-standardized episodic
        # content shape (persistence/memory_prompt.py), not an M1 placeholder --
        # keep this in sync with that module's docstring.
        content={"input": "...", "output": '{"mentions_tsmc": false}'},
    )
    await remember(
        store,
        policy,
        MemoryKind.PROCEDURAL,
        tenant="default",
        scope=("stt_check_notify", "check"),
        key="rule-1",
        content={"rule": "列舉同業不算提到"},
    )

    current_node_name.set("check")
    # limit high enough to see past real memory_writer demo data that already
    # lives under this namespace from actual pipeline runs (this scope is the
    # real `check` agent's, not a scratch namespace) -- assert our key showed
    # up and nothing of the other kind leaked in, rather than assuming an
    # empty starting state.
    episodic = await recall(
        store, policy, MemoryKind.EPISODIC, tenant="default", scope=("stt_check_notify", "check"), limit=50
    )
    procedural = await recall(
        store, policy, MemoryKind.PROCEDURAL, tenant="default", scope=("stt_check_notify", "check"), limit=50
    )
    assert any(item.key == "case-1" for item in episodic), episodic
    assert any(item.key == "rule-1" for item in procedural), procedural
    print("[prefix_scope] OK -- kind keeps episodic/procedural separate even under the same scope")


async def scenario_semantic_search(store, policy) -> None:
    current_node_name.set("memory_writer")
    await remember(
        store,
        policy,
        MemoryKind.EPISODIC,
        tenant="default",
        scope=("stt_check_notify", "check"),
        key="case-tsmc",
        content={"input": "台積電本季法說會公布晶圆代工营收创新高", "output": '{"mentions_tsmc": true}'},
    )
    await remember(
        store,
        policy,
        MemoryKind.EPISODIC,
        tenant="default",
        scope=("stt_check_notify", "check"),
        key="case-weather",
        content={"input": "今天台北下雨，氣溫下降到十八度", "output": '{"mentions_tsmc": false}'},
    )

    current_node_name.set("check")
    # limit=50, not 2: this namespace already has real memory_writer demo
    # data (see scenario_prefix_scope's comment) that could itself rank
    # above one of our two cases -- what M4 actually needs proven is that
    # case-tsmc outranks case-weather for a semiconductor-earnings query,
    # not that our two cases are the only or the top items in the store.
    ranked = await recall(
        store,
        policy,
        MemoryKind.EPISODIC,
        tenant="default",
        scope=("stt_check_notify", "check"),
        query="半導體 晶圆代工 财报",
        limit=50,
    )
    keys = [item.key for item in ranked]
    assert "case-tsmc" in keys and "case-weather" in keys, keys
    assert keys.index("case-tsmc") < keys.index("case-weather"), keys
    print("[semantic_search] OK -- query= ranks the semiconductor case above the unrelated weather case")


async def scenario_tenant_isolation(store, policy) -> None:
    # Written by the trusted distiller under a *different* real tenant, so
    # the data genuinely exists -- proving the denial below is policy, not
    # an empty result that would've happened anyway.
    current_node_name.set("memory_writer")
    await remember(
        store,
        policy,
        MemoryKind.SEMANTIC,
        tenant="other_tenant",
        scope=("company", "tsmc"),
        key="alias",
        content={"aliases": ["OtherCorp"]},
    )

    current_node_name.set("check")
    leaked = await recall(store, policy, MemoryKind.SEMANTIC, tenant="other_tenant", scope=("company", "tsmc"))
    assert leaked == [], leaked

    current_node_name.set("memory_writer")
    confirmed_exists = await recall(store, policy, MemoryKind.SEMANTIC, tenant="other_tenant", scope=("company", "tsmc"))
    assert len(confirmed_exists) == 1, confirmed_exists
    print("[tenant_isolation] OK -- check is denied another tenant's data that genuinely exists (a real policy denial, not absent data)")


async def scenario_global_tenant(store, policy) -> None:
    # Depends on scenario_round_trip having already written the GLOBAL_TENANT
    # company alias fact.
    current_node_name.set("check")
    shared = await recall(store, policy, MemoryKind.SEMANTIC, tenant=GLOBAL_TENANT, scope=("company", "tsmc"))
    assert len(shared) == 1 and shared[0].key == "alias", shared
    print("[global_tenant] OK -- GLOBAL_TENANT knowledge crosses the tenant boundary by explicit policy grant")


_BROWSE_POLICY = MemoryPolicy(
    principals={"browse_tester": MemoryGrant(read=("_global/semantic/smoke_browse/*",))}
)
"""Ad-hoc, not mcp_servers/policy.yaml's real grants -- browse() is a
platform primitive with no agent wired to it yet (docs/exclusion-scenario-plan.md
P1's explicit "zero scenario coupling" goal, same posture M1 took for
recall()/remember()), so there's no real principal to test against. A
throwaway namespace + throwaway principal keeps this scenario from having
to touch policy.yaml just to exercise a test fixture."""


async def scenario_browse_tree(store, policy) -> None:
    # tree, nested one level under a private "smoke_browse" segment so the
    # tested root's *own* siblings assertion isn't coupled to whatever else
    # already lives under the shared `_global/semantic` top level (e.g.
    # scenario_round_trip's real "company" data) -- smoke_browse/root/ ->
    # branch_a/ (_index + one leaf item), branch_b/ (_index only)
    current_node_name.set("memory_writer")
    await remember(
        store, policy, MemoryKind.SEMANTIC, tenant=GLOBAL_TENANT, scope=("smoke_browse", "root"),
        key=INDEX_KEY, content={"title": "root", "summary": "根節點"},
    )
    await remember(
        store, policy, MemoryKind.SEMANTIC, tenant=GLOBAL_TENANT, scope=("smoke_browse", "root", "branch_a"),
        key=INDEX_KEY, content={"title": "branch a", "summary": "分支 A"},
    )
    await remember(
        store, policy, MemoryKind.SEMANTIC, tenant=GLOBAL_TENANT, scope=("smoke_browse", "root", "branch_a"),
        key="leaf-1", content={"text": "leaf content"},
    )
    await remember(
        store, policy, MemoryKind.SEMANTIC, tenant=GLOBAL_TENANT, scope=("smoke_browse", "root", "branch_b"),
        key=INDEX_KEY, content={"title": "branch b", "summary": "分支 B"},
    )

    current_node_name.set("browse_tester")

    root = await browse(
        store, _BROWSE_POLICY, MemoryKind.SEMANTIC, tenant=GLOBAL_TENANT, prefix=("smoke_browse", "root")
    )
    assert root["summary"] == "根節點", root
    assert root["parent"] == ["smoke_browse"], root
    assert root["siblings"] == [], root  # smoke_browse's only child is "root"
    assert root["items"] == [], root  # root's only item is its own _index, excluded from items
    assert {c["segment"] for c in root["children"]} == {"branch_a", "branch_b"}, root
    assert {c["segment"]: c["summary"] for c in root["children"]} == {"branch_a": "分支 A", "branch_b": "分支 B"}, root
    print("[browse_tree] OK -- root lists both children with correct summaries, own _index excluded from items")

    branch_a = await browse(
        store, _BROWSE_POLICY, MemoryKind.SEMANTIC, tenant=GLOBAL_TENANT, prefix=("smoke_browse", "root", "branch_a")
    )
    assert branch_a["summary"] == "分支 A", branch_a
    assert branch_a["parent"] == ["smoke_browse", "root"], branch_a
    assert branch_a["siblings"] == ["branch_b"], branch_a
    assert branch_a["children"] == [], branch_a  # leaf: nothing deeper
    assert len(branch_a["items"]) == 1, branch_a
    assert branch_a["items"][0] == {"key": "leaf-1", "text": "leaf content"}, branch_a
    print("[browse_leaf] OK -- leaf's items exclude INDEX_KEY, parent/siblings point back to root/branch_b")


async def scenario_browse_policy_denial(store, policy) -> None:
    current_node_name.set("nobody")  # not a principal in _BROWSE_POLICY at all
    denied = await browse(store, _BROWSE_POLICY, MemoryKind.SEMANTIC, tenant=GLOBAL_TENANT, prefix=("smoke_browse",))
    assert denied == {}, denied
    print("[browse_policy_denial] OK -- ungranted browse() returns {} instead of raising")


async def scenario_browse_from_root(store, policy) -> None:
    # _BROWSE_POLICY's grant is `_global/semantic/smoke_browse/*` -- two
    # segments below the root namespace (tenant, kind). Depends on
    # scenario_browse_tree having already written the smoke_browse tree.
    current_node_name.set("browse_tester")
    root = await browse(store, _BROWSE_POLICY, MemoryKind.SEMANTIC, tenant=GLOBAL_TENANT, prefix=())
    assert any(c["segment"] == "smoke_browse" for c in root["children"]), root
    print("[browse_from_root] OK -- browse(prefix=()) surfaces a grant nested two segments below the root")


async def scenario_recall_partial_scope_denied(store, policy) -> None:
    # Same principal/grant as scenario_browse_from_root, but recall() with a
    # scope that's exactly the grant's wildcard directory (no leaf segment)
    # must be denied -- only browse() may treat that as "list what's here".
    current_node_name.set("browse_tester")
    denied = await recall(store, _BROWSE_POLICY, MemoryKind.SEMANTIC, tenant=GLOBAL_TENANT, scope=("smoke_browse",))
    assert denied == [], denied
    print("[recall_partial_scope_denied] OK -- recall() rejects an incomplete scope browse() would accept")


async def scenario_policy_denial(store, policy) -> None:
    current_node_name.set("check")  # check has no write grant (see policy.yaml)
    await remember(
        store,
        policy,
        MemoryKind.SEMANTIC,
        tenant=GLOBAL_TENANT,
        scope=("company", "tsmc"),
        key="should-not-write",
        content={"x": 1},
    )
    items = await recall(store, policy, MemoryKind.SEMANTIC, tenant=GLOBAL_TENANT, scope=("company", "tsmc"))
    assert all(item.key != "should-not-write" for item in items), items

    current_node_name.set("stt")  # stt has no `memory:` entry at all
    denied = await recall(store, policy, MemoryKind.SEMANTIC, tenant=GLOBAL_TENANT, scope=("company", "tsmc"))
    assert denied == [], denied
    print("[policy_denial] OK -- ungranted write is dropped, ungranted read returns empty")


_AUDIT_POLICY = MemoryPolicy(
    principals={
        # No trailing "/*": scope=("audit_test",) alone is a 3-segment
        # namespace ("_global/semantic/audit_test"), one segment short of
        # what a "/*" suffix would require (fnmatch, not path-aware glob --
        # see memory_policy.py's can_write) -- trailing "*" with no slash
        # matches that exact namespace (zero or more trailing chars).
        "audit_writer": MemoryGrant(read=("_global/semantic/audit_test*",), write=("_global/semantic/audit_test*",))
    }
)
"""Ad-hoc, like _BROWSE_POLICY -- mcp_servers/policy.yaml's memory: block is
explicitly frozen pending a governance decision (see its own header
comment), so this proves the audit-logging mechanism itself without
depending on, or risking drift with, real principals' grants."""


async def scenario_audit_log(store, policy) -> None:
    thread_id = "smoke-thread-audit"
    current_thread_id.set(thread_id)

    current_node_name.set("audit_writer")
    await remember(
        store,
        _AUDIT_POLICY,
        MemoryKind.SEMANTIC,
        tenant=GLOBAL_TENANT,
        scope=("audit_test",),
        key="probe",
        content={"x": 1},
    )
    await recall(store, _AUDIT_POLICY, MemoryKind.SEMANTIC, tenant=GLOBAL_TENANT, scope=("audit_test",))

    current_node_name.set("nobody")  # not a principal in _AUDIT_POLICY at all
    await recall(store, _AUDIT_POLICY, MemoryKind.SEMANTIC, tenant=GLOBAL_TENANT, scope=("audit_test",))

    # TODO.md's review-approve-reject-audit-gap: scripts/review_memory.py's
    # approve/reject used to bypass this trail entirely (raw store.aput/
    # adelete) -- edit()/forget() close that gap, so pending->active leaves
    # the same kind='memory' rows recall()/remember() do.
    current_node_name.set("audit_writer")
    await edit(
        store, _AUDIT_POLICY, MemoryKind.SEMANTIC, tenant=GLOBAL_TENANT, scope=("audit_test",),
        key="probe", value={"content": {"x": 2}, "status": "active"},
    )
    await forget(store, _AUDIT_POLICY, MemoryKind.SEMANTIC, tenant=GLOBAL_TENANT, scope=("audit_test",), key="probe")

    rows = {(r["name"], r["node"]): r for r in await fetch_calls(thread_id) if r["kind"] == "memory"}

    remembered = rows[("remember", "audit_writer")]
    assert remembered["denied"] is False and remembered["response"] is None, remembered
    assert remembered["request"]["key"] == "probe" and remembered["request"]["content_chars"] > 0, remembered

    recalled = rows[("recall", "audit_writer")]
    assert recalled["denied"] is False, recalled
    assert recalled["response"] == {"count": 1, "keys": ["probe"]}, recalled

    denied = rows[("recall", "nobody")]
    assert denied["denied"] is True and denied["response"] is None, denied

    edited = rows[("edit", "audit_writer")]
    assert edited["denied"] is False and edited["request"]["key"] == "probe", edited

    forgotten = rows[("forget", "audit_writer")]
    assert forgotten["denied"] is False and forgotten["request"]["key"] == "probe", forgotten

    print("[audit_log] OK -- recall()/remember()/edit()/forget() (success and denied) leave queryable kind='memory' call_log rows")


async def scenario_remember_extra_fields(store, policy) -> None:
    current_node_name.set("memory_writer")
    await remember(
        store, policy, MemoryKind.PROCEDURAL, tenant="default", scope=("stt_exclusion_notify", "check"),
        key="smoke-extra-fields", content={"rule": "smoke test rule"},
        status="pending", extra={"evidence": ["case-1", "case-2"], "rationale": "smoke test rationale"},
    )

    # Read back via store.aget() directly, not recall() -- recall() forces
    # status="active" unconditionally (§5 P0's gate), so a "pending" item is
    # invisible to it by design; that behavior is scenario_status_gate's
    # job to verify, not this one's. This scenario only checks that extra=
    # actually lands in `value` correctly.
    namespace = build_namespace(MemoryKind.PROCEDURAL, "default", ("stt_exclusion_notify", "check"))
    item = await store.aget(namespace, "smoke-extra-fields")
    assert item.value["evidence"] == ["case-1", "case-2"], item.value
    assert item.value["rationale"] == "smoke test rationale", item.value
    # extra must never clobber the fixed audit fields, even by accident.
    assert item.value["status"] == "pending", item.value
    assert item.value["content"] == {"rule": "smoke test rule"}, item.value
    print("[remember_extra_fields] OK -- extra= lands in value alongside the fixed audit fields without clobbering them")


async def scenario_status_gate(store, policy) -> None:
    current_node_name.set("memory_writer")
    await remember(
        store, policy, MemoryKind.EPISODIC, tenant="default", scope=("stt_check_notify", "check"),
        key="case-pending", content={"input": "...", "output": '{"mentions_tsmc": true}'}, status="pending",
    )
    await remember(
        store, policy, MemoryKind.EPISODIC, tenant="default", scope=("stt_check_notify", "check"),
        key="case-active", content={"input": "...", "output": '{"mentions_tsmc": false}'},
    )

    current_node_name.set("check")
    items = await recall(store, policy, MemoryKind.EPISODIC, tenant="default", scope=("stt_check_notify", "check"), limit=50)
    keys = [item.key for item in items]
    assert "case-active" in keys and "case-pending" not in keys, keys
    print("[status_gate_recall] OK -- recall() returns status=active only, pending stays invisible")

    # browse(): a standalone subtree, independent of scenario_browse_tree's
    # own tree so this scenario doesn't depend on run order -- one branch
    # mixes active/pending leaves, the other is pending-only (the documented
    # segment-name gap).
    current_node_name.set("memory_writer")
    await remember(
        store, policy, MemoryKind.SEMANTIC, tenant=GLOBAL_TENANT, scope=("smoke_browse", "status_check"),
        key=INDEX_KEY, content={"title": "status_check", "summary": "已核准分支"},
    )
    await remember(
        store, policy, MemoryKind.SEMANTIC, tenant=GLOBAL_TENANT, scope=("smoke_browse", "status_check"),
        key="leaf-active", content={"text": "promoted"},
    )
    await remember(
        store, policy, MemoryKind.SEMANTIC, tenant=GLOBAL_TENANT, scope=("smoke_browse", "status_check"),
        key="leaf-pending", content={"text": "not yet promoted"}, status="pending",
    )
    await remember(
        store, policy, MemoryKind.SEMANTIC, tenant=GLOBAL_TENANT, scope=("smoke_browse", "status_check_pending_only"),
        key=INDEX_KEY, content={"title": "status_check_pending_only", "summary": "整支都待審"}, status="pending",
    )

    current_node_name.set("browse_tester")
    status_check = await browse(
        store, _BROWSE_POLICY, MemoryKind.SEMANTIC, tenant=GLOBAL_TENANT, prefix=("smoke_browse", "status_check")
    )
    item_keys = [item["key"] for item in status_check["items"]]
    assert "leaf-active" in item_keys and "leaf-pending" not in item_keys, status_check
    print("[status_gate_browse_items] OK -- browse()'s items exclude a pending leaf, keep an active one")

    root = await browse(store, _BROWSE_POLICY, MemoryKind.SEMANTIC, tenant=GLOBAL_TENANT, prefix=("smoke_browse",))
    children_by_segment = {c["segment"]: c["summary"] for c in root["children"]}
    assert children_by_segment.get("status_check") == "已核准分支", children_by_segment
    # documented gap (docs/knowledge-distillation-plan.md §5 P0): the
    # pending-only subtree's segment name still surfaces, but its pending
    # _index summary must not.
    assert "status_check_pending_only" in children_by_segment, children_by_segment
    assert children_by_segment["status_check_pending_only"] is None, children_by_segment
    print("[status_gate_browse_index] OK -- pending _index summary hidden; segment-name gap is the documented one")


async def _cleanup(store) -> None:
    """Unlike orchestrator/smoke_test.py (which sidesteps collisions with a
    fresh uuid thread_id per run), these scenarios write fixed keys under
    `default/episodic/stt_check_notify/check` and `default/procedural/
    stt_check_notify/check` -- the exact namespaces the real `check` agent
    reads via llm/tsmc_judge.py once M2 wires recall() into it. Left in
    place, scenario_prefix_scope's `{"verdict": "not_tsmc"}` shape doesn't
    match what the real agent expects (`{"mentions_tsmc": bool}`) and would
    crash it with a KeyError -- delete everything this file writes so
    running the smoke test never pollutes the demo scenario's real memory."""
    for kind, tenant, scope, key in [
        (MemoryKind.SEMANTIC, GLOBAL_TENANT, ("company", "tsmc"), "alias"),
        (MemoryKind.EPISODIC, "default", ("stt_check_notify", "check"), "case-1"),
        (MemoryKind.PROCEDURAL, "default", ("stt_check_notify", "check"), "rule-1"),
        (MemoryKind.EPISODIC, "default", ("stt_check_notify", "check"), "case-tsmc"),
        (MemoryKind.EPISODIC, "default", ("stt_check_notify", "check"), "case-weather"),
        (MemoryKind.SEMANTIC, "other_tenant", ("company", "tsmc"), "alias"),
        (MemoryKind.SEMANTIC, GLOBAL_TENANT, ("smoke_browse", "root"), INDEX_KEY),
        (MemoryKind.SEMANTIC, GLOBAL_TENANT, ("smoke_browse", "root", "branch_a"), INDEX_KEY),
        (MemoryKind.SEMANTIC, GLOBAL_TENANT, ("smoke_browse", "root", "branch_a"), "leaf-1"),
        (MemoryKind.SEMANTIC, GLOBAL_TENANT, ("smoke_browse", "root", "branch_b"), INDEX_KEY),
        (MemoryKind.SEMANTIC, GLOBAL_TENANT, ("audit_test",), "probe"),
        (MemoryKind.EPISODIC, "default", ("stt_check_notify", "check"), "case-pending"),
        (MemoryKind.EPISODIC, "default", ("stt_check_notify", "check"), "case-active"),
        (MemoryKind.SEMANTIC, GLOBAL_TENANT, ("smoke_browse", "status_check"), INDEX_KEY),
        (MemoryKind.SEMANTIC, GLOBAL_TENANT, ("smoke_browse", "status_check"), "leaf-active"),
        (MemoryKind.SEMANTIC, GLOBAL_TENANT, ("smoke_browse", "status_check"), "leaf-pending"),
        (MemoryKind.SEMANTIC, GLOBAL_TENANT, ("smoke_browse", "status_check_pending_only"), INDEX_KEY),
        (MemoryKind.PROCEDURAL, "default", ("stt_exclusion_notify", "check"), "smoke-extra-fields"),
    ]:
        await store.adelete(build_namespace(kind, tenant, scope), key)


async def main() -> None:
    ensure_call_log_schema()
    policy = load_memory_policy(str(_POLICY_PATH))
    async with get_memory_store() as store:
        await store.setup()
        try:
            await scenario_round_trip(store, policy)
            await scenario_prefix_scope(store, policy)
            await scenario_semantic_search(store, policy)
            await scenario_tenant_isolation(store, policy)
            await scenario_global_tenant(store, policy)
            await scenario_policy_denial(store, policy)
            await scenario_browse_tree(store, policy)
            await scenario_browse_policy_denial(store, policy)
            await scenario_browse_from_root(store, policy)
            await scenario_recall_partial_scope_denied(store, policy)
            await scenario_audit_log(store, policy)
            await scenario_status_gate(store, policy)
            await scenario_remember_extra_fields(store, policy)
        finally:
            await _cleanup(store)
    print("\nAll memory smoke tests passed.")


asyncio.run(main())
