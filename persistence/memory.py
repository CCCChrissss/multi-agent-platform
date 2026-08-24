"""Platform-level API for long-term memory: the namespace convention plus
recall()/remember()/browse(), the entry points scenario code (agents/*,
workflow nodes) should use -- see docs/long-term-memory-plan.md §3 for
recall()/remember(), docs/exclusion-scenario-plan.md §2 for browse() (the
progressive-disclosure "ls" counterpart to recall()'s "cat", added when a
knowledge base got too large to hand an agent in one recall() call).

Kept independent of orchestration mode, the same way agents/envelope.py
decouples agent logic from transport: a synchronous LangGraph node, an
event-driven worker, and a standalone agents/*/server.py handler all call
these same two functions against the same store.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections.abc import Sequence
from enum import Enum
from typing import Any

from langgraph.store.base import SearchItem

from persistence.call_log import current_node_name, current_thread_id, log_call
from persistence.memory_policy import MemoryPolicy, can_read, can_write


def _log_denied(action: str, principal: str | None, namespace: tuple[str, ...]) -> None:
    # stderr, not stdout: mcp_servers/memory/server.py runs this module
    # inside a stdio-transport MCP subprocess that reserves stdout for
    # JSON-RPC -- a stray stdout print() here corrupts that stream and the
    # caller's JSON-RPC parser chokes on it (seen for real during
    # docs/exclusion-scenario-plan.md P0's parity_check.py run, logged in
    # TODO.md before this fix).
    print(f"[memory] {action} denied: principal={principal!r} namespace={namespace!r}", file=sys.stderr, flush=True)


async def _log_memory_call(
    name: str, request: dict[str, Any], response: dict[str, Any] | None, *, denied: bool, latency_ms: int
) -> None:
    """kind='memory' call_log rows, closing the audit gap recall()/browse()/
    remember() had relative to the 'llm'/'tool' kinds (TODO.md's
    memory-access-audit-log entry). Never records raw memory content --
    counts/keys/char-lengths only, the same principle gateway/client.py's
    embed_texts() applies to embedding vectors (logs `dims`, not the vector)
    -- call_log must not become a second copy of whatever's stored in memory
    (e.g. a recipient's notification preference).

    Note: when recall()/browse() run inside mcp_servers/memory/server.py's
    stdio subprocess, current_thread_id is always None here -- that
    ContextVar is set per-request in the calling agent's own process
    (agents/envelope.py) and can't cross the stdio boundary the way the
    subprocess's fixed-for-its-lifetime principal does (see that module's
    docstring). The `node` column still identifies the principal; only the
    thread_id correlation is missing for MCP-originated memory calls.
    """
    await log_call("memory", name, request, response, False, latency_ms, denied=denied)


class MemoryKind(str, Enum):
    SEMANTIC = "semantic"
    EPISODIC = "episodic"
    PROCEDURAL = "procedural"


INDEX_KEY = "_index"
"""Reserved item key for a namespace's own self-description
(`{"title": str, "summary": str}`) -- browse()'s only way to tell an agent
what a branch *is* before it decides whether to descend into it
(docs/exclusion-scenario-plan.md §3.1). Shared between browse() and whatever
seed script writes a knowledge tree (e.g. a future scripts/seed_*.py) so
the two never drift on what the marker key is called. Excluded from
browse()'s own `items` -- it's metadata about the namespace, not a memory
in it."""


GLOBAL_TENANT = "_global"
"""Reserved tenant_id for knowledge that isn't tenant-specific -- a company's
public aliases, unlike a per-tenant recipient's notification preference (see
docs/long-term-memory-plan.md §3.2). Whether a given piece of knowledge
belongs under a real tenant or under GLOBAL_TENANT is a scenario decision,
not something recall()/remember() decide automatically -- a caller that wants
"my tenant's memory plus shared memory" makes two calls and merges them
itself, mirroring how mcp_servers/policy.yaml grants each namespace pattern
explicitly rather than folding tenant into a wildcard."""


def build_namespace(kind: MemoryKind, tenant: str, scope: Sequence[str]) -> tuple[str, ...]:
    """`(tenant_id, kind, *scope)` -- this order is what makes asearch's
    namespace-prefix matching useful: callers almost always want "all memory
    of this kind for this step", not "all memory for this tenant"
    (docs/long-term-memory-plan.md §3.2)."""
    return (tenant, kind.value, *scope)


def parse_scope(scope_arg: str) -> tuple[str, str]:
    """`"<workflow_name>/<step_name>"` -> `(workflow_name, step_name)` -- the
    CLI-facing counterpart to build_namespace()'s scope tuple, shared by
    every scripts/*.py entry point that takes a `--scope` argument so the
    parsing rule lives in one place."""
    workflow_name, step_name = scope_arg.split("/", 1)
    return (workflow_name, step_name)


def _is_active(item: SearchItem | None) -> bool:
    return item is not None and item.value.get("status") == "active"


async def backfill_missing_status(store: Any) -> int:
    """One-time-per-store-open repair for rows written before the `status`
    field existed (docs/knowledge-distillation-plan.md §5 P0): such a row's
    status is SQL NULL, which never matches recall()/browse()'s
    `filter={"status": "active"}` -- it would otherwise silently vanish from
    every read path. Idempotent (rows that already have a status, including
    every new write, are left untouched), so calling this on every
    open_agent_memory() open is a safe no-op once a store is fully migrated.

    Bypasses persistence/memory_policy.py on purpose -- this is an
    infrastructure migration over the whole store, not a scenario read/write
    through some principal's grant."""
    scan_limit = 10_000  # demo-scale store (order of 100 rows total as of writing); raise if it ever grows past this
    namespaces = await store.alist_namespaces(limit=scan_limit)
    patched = 0
    for namespace in namespaces:
        items = await store.asearch(namespace, limit=scan_limit)
        for item in items:
            if "status" in item.value:
                continue
            # index=False: `content` isn't changing, so this must not trigger
            # a fresh embedding call.
            await store.aput(item.namespace, item.key, {**item.value, "status": "active"}, index=False)
            patched += 1
    return patched


_PENDING_LIST_LIMIT = 200


async def list_pending(store: Any, kind: MemoryKind, scope: Sequence[str], key: str | None = None) -> list[SearchItem]:
    """The `status="pending"` review queue under `("default", kind, *scope)` --
    shared by scripts/review_memory.py (procedural) and scripts/
    review_episodic.py (episodic) so the pending/active review gate's query
    logic lives in one place instead of two drifting copies. filter= is
    pushed into the query itself (not a Python-side post-filter) so `limit`
    caps the pending population, not the combined pending+active one.
    `key` narrows to one item -- e.g. right after a distill run, review just
    the new candidate/case without also being asked about unrelated
    pre-existing pending ones. Bypasses recall()'s can_read() check the same
    way backfill_missing_status() does: this is a reviewer CLI operating
    directly against the store, not a scenario-code read through a
    principal's policy grant."""
    namespace = build_namespace(kind, "default", scope)
    items = await store.asearch(namespace, filter={"status": "pending"}, limit=_PENDING_LIST_LIMIT)
    if key is not None:
        items = [item for item in items if item.key == key]
    return items


async def recall(
    store: Any,
    policy: MemoryPolicy,
    kind: MemoryKind,
    *,
    tenant: str,
    scope: Sequence[str],
    query: str | None = None,
    filter: dict[str, Any] | None = None,
    limit: int = 5,
) -> list[SearchItem]:
    """Fetch memories under `(tenant, kind, *scope)`. A denied read returns an
    empty list (fail-closed) instead of raising, so a policy miss degrades to
    "no memory available" rather than breaking the caller's hot path.

    Only `status="active"` memories are ever returned (docs/knowledge-distillation-plan.md
    §5 P0) -- a caller-supplied `filter` is merged with, not able to override,
    this constraint, so a `pending` memory (awaiting the M5 quality gate) can
    never leak into a prompt through this path."""
    principal = current_node_name.get()
    namespace = build_namespace(kind, tenant, scope)
    effective_filter = {**(filter or {}), "status": "active"}
    request = {"namespace": list(namespace), "query": query, "limit": limit}
    if not can_read(policy, principal, namespace):
        _log_denied("read", principal, namespace)
        await _log_memory_call("recall", request, None, denied=True, latency_ms=0)
        return []
    start = time.monotonic()
    items = await store.asearch(namespace, query=query, filter=effective_filter, limit=limit)
    latency_ms = int((time.monotonic() - start) * 1000)
    await _log_memory_call(
        "recall", request, {"count": len(items), "keys": [item.key for item in items]}, denied=False, latency_ms=latency_ms
    )
    return items


_BROWSE_CANDIDATE_CAP = 200
"""Overfetch size for browse()'s own-namespace item search -- see the
`asearch()` call inside browse() for why this can't just be `limit`."""


async def browse(
    store: Any,
    policy: MemoryPolicy,
    kind: MemoryKind,
    *,
    tenant: str,
    prefix: Sequence[str],
    limit: int = 10,
) -> dict[str, Any]:
    """List what's one level below `(tenant, kind, *prefix)`, without
    fetching full content -- the "ls" counterpart to recall()'s "cat"
    (docs/exclusion-scenario-plan.md §2.5). For a caller that only knows a
    rough direction (not yet the exact scope recall() needs), this lets it
    see the next layer's branch names plus a one-line summary of each (from
    that child's INDEX_KEY item, if any) and the current layer's own items
    (if it's a leaf), then decide for itself whether to descend further.

    Deliberately does NOT recurse or return the whole subtree -- only one
    caller-chosen branch is ever expanded per call, which is what makes this
    DFS-shaped rather than BFS-shaped (docs/exclusion-scenario-plan.md §2.3):
    the caller pays for exactly the branches it asks about, never a full
    tree walk. `parent`/`siblings` in the return value exist so backtracking
    ("this branch was wrong, what else was at the level above?") costs one
    more browse() call with a namespace already visible in the caller's own
    conversation history, not a walk back down from the root
    (docs/exclusion-scenario-plan.md §2.4).

    A denied read returns `{}` (fail-closed, same policy as recall()) instead
    of raising.

    Like recall(), only `status="active"` memories are visible -- both
    `items` (which inlines full `content`, not a summary) and each child's
    `_index` summary are filtered (docs/knowledge-distillation-plan.md §5
    P0). `alist_namespaces()` itself has no filter/status concept, so a
    namespace containing *only* pending memories still surfaces its bare
    segment name in `children`/`siblings` with `summary=None` -- a known,
    accepted gap (same doc, "殘留缺口"), not a bug."""
    principal = current_node_name.get()
    namespace = build_namespace(kind, tenant, prefix)
    request = {"namespace": list(namespace), "limit": limit}
    if not can_read(policy, principal, namespace, for_browse=True):
        _log_denied("browse", principal, namespace)
        await _log_memory_call("browse", request, None, denied=True, latency_ms=0)
        return {}

    start = time.monotonic()

    # Siblings come from the parent's own children -- one extra
    # alist_namespaces call, not a second can_read check (this only ever
    # exposes segment names, the same exposure level as `children` below;
    # see persistence/memory_policy.py::can_read's docstring for the
    # deliberate prefix-widening this and `children` both rely on).
    parent = tuple(prefix[:-1])
    parent_namespace = build_namespace(kind, tenant, parent) if prefix else None

    async def _list_siblings() -> list[str]:
        if parent_namespace is None:
            return []
        return sorted(
            ns[-1]
            for ns in await store.alist_namespaces(prefix=parent_namespace, max_depth=len(parent_namespace) + 1)
            if ns not in (namespace, parent_namespace)
        )

    # None of these four depend on each other's result -- run them
    # concurrently instead of stacking four DB round trips on the request
    # hot path (only child_indexes below genuinely depends on one of these,
    # so it stays a second stage).
    raw_child_namespaces, own_index, searched, siblings = await asyncio.gather(
        store.alist_namespaces(prefix=namespace, max_depth=len(namespace) + 1),
        store.aget(namespace, INDEX_KEY),
        # asearch() matches by namespace *prefix*, not exact namespace (see
        # build_namespace()'s docstring) -- it would otherwise also return
        # items from every namespace below this one (e.g. a leaf several
        # branches down), which browse() must not leak into this layer's
        # own `items`. Overfetch a flat, generous candidate cap and filter
        # to an exact namespace match before slicing to `limit` --
        # ponytail: this can still under-count `own_items` if a single
        # namespace's descendants outnumber _BROWSE_CANDIDATE_CAP; raise the
        # cap (or push exact-namespace filtering into the query itself, if
        # AsyncPostgresStore ever exposes that) if §3.2's "3~7 children,
        # depth 2~4" guidance stops holding in practice.
        store.asearch(namespace, filter={"status": "active"}, limit=_BROWSE_CANDIDATE_CAP),
        _list_siblings(),
    )
    # aget() has no filter= param, unlike asearch() above -- status has to be
    # checked in Python for the two INDEX_KEY lookups (own_index here,
    # child_indexes below).
    if not _is_active(own_index):
        own_index = None

    child_namespaces = [ns for ns in raw_child_namespaces if ns != namespace]
    raw_child_indexes = await asyncio.gather(*(store.aget(ns, INDEX_KEY) for ns in child_namespaces))
    child_indexes = [item if _is_active(item) else None for item in raw_child_indexes]
    children = [
        {"segment": ns[-1], "summary": (item.value["content"].get("summary") if item else None)}
        for ns, item in zip(child_namespaces, child_indexes)
    ]

    own_matches = [item for item in searched if item.namespace == namespace and item.key != INDEX_KEY]
    truncated = len(own_matches) > limit
    own_items = own_matches[:limit]
    items = [{"key": item.key, **item.value["content"]} for item in own_items]

    result = {
        "scope": list(prefix),
        "summary": (own_index.value["content"].get("summary") if own_index else None),
        "parent": list(parent),
        "siblings": siblings,
        "children": children,
        "items": items,
        "truncated": truncated,
    }
    latency_ms = int((time.monotonic() - start) * 1000)
    await _log_memory_call(
        "browse",
        request,
        {"children": len(children), "items": len(items), "truncated": truncated},
        denied=False,
        latency_ms=latency_ms,
    )
    return result


async def remember(
    store: Any,
    policy: MemoryPolicy,
    kind: MemoryKind,
    *,
    tenant: str,
    scope: Sequence[str],
    key: str,
    content: dict[str, Any],
    confidence: float = 1.0,
    ttl_minutes: float | None = None,
    status: str = "active",
    extra: dict[str, Any] | None = None,
    index: bool = True,
) -> None:
    """Write one memory under `(tenant, kind, *scope)`. Auto-stamps the audit
    fields from docs/long-term-memory-plan.md §3.2 -- source_thread_id/
    source_step come from the same contextvars persistence/call_log.py uses,
    so callers never pass them -- so any memory can be traced back to the run
    that produced it. A denied write is silently dropped (fail-closed),
    mirroring recall().

    `status` defaults to `"active"` (immediately visible to recall()/
    browse()) so every existing caller is unaffected. A future distiller
    (docs/knowledge-distillation-plan.md §3) writes `status="pending"`
    instead -- invisible to both read paths until the M5 quality gate
    promotes it, stored at the `value` top level (not inside `content`,
    which the gate's filter never inspects -- see that doc's §1.3).

    `extra` merges additional top-level `value` fields for callers that need
    metadata alongside `content` without it being part of what gets injected
    into a prompt -- e.g. a distiller's `evidence`/`rationale` for a
    candidate procedural rule (docs/knowledge-distillation-plan.md §3.3),
    or a future rollback's `version` (docs/long-term-memory-plan.md §5 M5).
    Applied *before* the fixed audit fields below, so `extra` can never
    silently clobber `status`/`content`/etc even if a caller passes a
    colliding key by mistake.

    `index=False` skips the embedding call for a write whose `content` is a
    verbatim copy of something already embedded elsewhere in the store (e.g.
    mirroring an existing episodic memory into a throwaway eval tenant) --
    same reasoning as backfill_missing_status()'s status-only patch."""
    principal = current_node_name.get()
    namespace = build_namespace(kind, tenant, scope)
    request = {
        "namespace": list(namespace),
        "key": key,
        "confidence": confidence,
        "content_chars": len(json.dumps(content, ensure_ascii=False)),
    }
    if not can_write(policy, principal, namespace):
        _log_denied("write", principal, namespace)
        await _log_memory_call("remember", request, None, denied=True, latency_ms=0)
        return
    value = {
        **(extra or {}),
        "content": content,
        "source_thread_id": current_thread_id.get(),
        "source_step": principal,
        "created_by": principal or "unknown",
        "confidence": confidence,
        "status": status,
    }
    start = time.monotonic()
    # store.aput()'s own `index` param is `Literal[False] | list[str] | None`,
    # not a plain bool -- `None` means "use the store's configured default
    # fields" (persistence/memory_store.py's index={"fields": ["content"]}),
    # `False` disables it. remember()'s `index` stays a plain bool for
    # callers; translate here rather than leaking that type into every caller.
    await store.aput(namespace, key, value, index=None if index else False, ttl=ttl_minutes)
    latency_ms = int((time.monotonic() - start) * 1000)
    await _log_memory_call("remember", request, None, denied=False, latency_ms=latency_ms)


async def edit(
    store: Any, policy: MemoryPolicy, kind: MemoryKind, *, tenant: str, scope: Sequence[str], key: str, value: dict[str, Any]
) -> None:
    """In-place overwrite of an *existing* memory's full value -- e.g.
    scripts/review_memory.py's _approve() flipping a pending procedural
    candidate to status="active". Unlike remember(), does not stamp
    created_by/source_thread_id/etc, so a caller must pass the complete
    value (original audit fields preserved, only the fields it means to
    change updated) rather than just `content`.

    Exists so this write goes through the same can_write() check and
    _log_memory_call() audit trail as remember(), instead of callers
    reaching for store.aput() directly and leaving pending->active --
    procedural review's most consequential write -- with no call_log trace
    (TODO.md's review-approve-reject-audit-gap entry)."""
    principal = current_node_name.get()
    namespace = build_namespace(kind, tenant, scope)
    request = {"namespace": list(namespace), "key": key}
    if not can_write(policy, principal, namespace):
        _log_denied("write", principal, namespace)
        await _log_memory_call("edit", request, None, denied=True, latency_ms=0)
        return
    start = time.monotonic()
    await store.aput(namespace, key, value)
    latency_ms = int((time.monotonic() - start) * 1000)
    await _log_memory_call("edit", request, None, denied=False, latency_ms=latency_ms)


async def forget(store: Any, policy: MemoryPolicy, kind: MemoryKind, *, tenant: str, scope: Sequence[str], key: str) -> None:
    """Delete an existing memory -- e.g. scripts/review_memory.py's
    _reject() discarding a pending candidate. Same reasoning as edit():
    routes through can_write()/_log_memory_call() instead of a caller
    reaching for store.adelete() directly, so a reject is as traceable in
    call_log as an approve."""
    principal = current_node_name.get()
    namespace = build_namespace(kind, tenant, scope)
    request = {"namespace": list(namespace), "key": key}
    if not can_write(policy, principal, namespace):
        _log_denied("write", principal, namespace)
        await _log_memory_call("forget", request, None, denied=True, latency_ms=0)
        return
    start = time.monotonic()
    await store.adelete(namespace, key)
    latency_ms = int((time.monotonic() - start) * 1000)
    await _log_memory_call("forget", request, None, denied=False, latency_ms=latency_ms)
