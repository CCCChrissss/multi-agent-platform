"""MCP server exposing semantic long-term memory as tools the model can call
on its own, instead of the platform always injecting it into the prompt --
docs/long-term-memory-plan.md §3.9/M4.5's judgment call: semantic memory the
agent already knows it needs (a recipient's channel preference) doesn't need
to be force-fed; the agent can ask for it, and a miss just degrades to the
default channel, unlike procedural/episodic "blind spot" memory which stays
a forced injection (persistence/memory_prompt.py).

Two tools, two access patterns: `recall_semantic_memory` ("cat") for when
the caller already knows the exact scope; `browse_semantic_memory` ("ls",
docs/exclusion-scenario-plan.md P2) for progressively discovering a scope
it doesn't know yet, one level at a time, in a knowledge tree too large to
hand over in one recall() call.

Runs as its own stdio subprocess (mcp_servers/gateway.py spawns one per
calling agent process), so it can't read the calling agent's
current_node_name ContextVar directly -- that's process-local. The gateway
instead passes the calling agent's fixed identity down once via the
MCP_CALLING_PRINCIPAL env var at spawn time (see MCPGateway.connect()),
which this module reads once and stamps onto its own current_node_name so
persistence/memory.py's recall() -> memory_policy.py's can_read() sees the
real principal instead of None (which would fail closed to "nothing found"
for every caller, silently).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from mcp_servers.tool_errors import ToolInputError, guarded_tool
from persistence.call_log import current_node_name
from persistence.memory import GLOBAL_TENANT, MemoryKind, browse, recall
from persistence.memory_lifespan import open_agent_memory

mcp = FastMCP("memory", log_level="WARNING")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_POLICY_PATH = _REPO_ROOT / "mcp_servers" / "policy.yaml"
_RESULT_LIMIT = 5
# Every current caller/scenario is tenant "default" -- real multi-tenancy
# isn't implemented anywhere yet (docs/long-term-memory-plan.md §5 risk 7),
# so there's no per-request tenant to thread through here even if the MCP
# transport made that easy. Revisit once a real tenant concept exists.
_TENANT = "default"

current_node_name.set(os.environ.get("MCP_CALLING_PRINCIPAL"))


def _log(line: str) -> None:
    # stdio transport reserves stdout for JSON-RPC -- app logs must go to stderr
    print(line, file=sys.stderr, flush=True)


# ponytail: process-lifetime singleton, never explicitly closed -- this
# subprocess lives exactly as long as the gateway that spawned it, and exits
# (dropping the connection) when the gateway does. Upgrade to an explicit
# shutdown hook if this server ever needs to outlive its gateway.
_memory_cm = open_agent_memory(str(_POLICY_PATH))
_store = None
_memory_policy = None
_init_lock = asyncio.Lock()


async def _ensure_memory():
    global _store, _memory_policy
    async with _init_lock:
        if _store is None:
            _store, _memory_policy = await _memory_cm.__aenter__()
    return _store, _memory_policy


@mcp.tool()
@guarded_tool(_log, "recall_semantic_memory")
async def recall_semantic_memory(scope: list[str], query: str | None = None) -> str:
    """Recall semantic memory (facts about an entity) you don't already know.

    `scope` is a path under the memory namespace, e.g. ["recipient", "<id>"]
    for a recipient's notification-channel preference, or
    ["company", "<name>"] for a company's known aliases. `query` is optional
    free text to rank results by relevance when a scope alone returns more
    than a few matches. Returns nothing if there's no memory for that scope,
    or if you aren't permitted to read it -- that's not an error, it just
    means no memory is available and you should fall back to your default
    behavior.
    """
    if not scope:
        raise ToolInputError("scope must be a non-empty list, e.g. [\"recipient\", \"<id>\"]")
    store, memory_policy = await _ensure_memory()
    items = await recall(
        store,
        memory_policy,
        MemoryKind.SEMANTIC,
        tenant=_TENANT,
        scope=scope,
        query=query,
        limit=_RESULT_LIMIT + 1,
    )
    truncated = len(items) > _RESULT_LIMIT
    items = items[:_RESULT_LIMIT]
    _log(f"[memory-mcp] recall scope={scope!r} query={query!r} -> {len(items)} item(s), truncated={truncated}")
    return json.dumps(
        {
            "results": [{"key": item.key, "content": item.value["content"], "score": item.score} for item in items],
            "returned": len(items),
            "truncated": truncated,
            "hint": (
                "more results exist than shown -- narrow `scope` (add a segment) or pass `query` to rank by relevance"
                if truncated
                else None
            ),
        },
        ensure_ascii=False,
    )


@mcp.tool()
@guarded_tool(_log, "browse_semantic_memory")
async def browse_semantic_memory(scope: list[str] | None = None) -> str:
    """Browse the semantic memory tree -- this is "ls", not "cat"
    (recall_semantic_memory is "cat": use it once you already know the exact
    scope you want). Use browse when you only know a rough direction -- for
    example, exploring what an insurance product's policy document covers
    before you know which article actually answers your question.

    `scope` is a path under the memory namespace, same convention as
    recall_semantic_memory's `scope`; omit it (or pass []) to start from the
    root and see what top-level subjects exist at all.

    The response has three parts:
      - `children`: branch names one level below `scope`, each with a
        one-line `summary` -- read these before deciding whether to descend.
        To go one level deeper, call this tool again with `scope` plus one
        child's `segment` appended.
      - `items`: this scope's own content, if this is a leaf (`children`
        will be empty in that case) -- this is the actual material, already
        returned in full; no further recall() needed.
      - `parent`/`siblings`: if this branch turns out to be the wrong one,
        call this tool again with `scope` set to `parent` to see what else
        was at the level above. Do this instead of guessing -- if a branch
        doesn't have what you're looking for, backtrack and check a sibling
        rather than fabricating an answer from the current branch's content.

    Returns an empty result if you aren't permitted to read this scope --
    that's not an error, it means nothing is available here.
    """
    store, memory_policy = await _ensure_memory()
    # GLOBAL_TENANT, not _TENANT ("default"): what browse() is for right now
    # is walking a shared knowledge tree (docs/exclusion-scenario-plan.md's
    # motivating case: an insurance product's policy document) -- the same
    # "not tenant-specific" category company aliases already live under
    # (persistence/memory.py's GLOBAL_TENANT docstring). Revisit if a future
    # tenant-scoped tree ever needs browsing too.
    result = await browse(store, memory_policy, MemoryKind.SEMANTIC, tenant=GLOBAL_TENANT, prefix=scope or [])
    _log(
        f"[memory-mcp] browse scope={scope!r} -> "
        f"{len(result.get('children', []))} children, {len(result.get('items', []))} items"
    )
    return json.dumps(result, ensure_ascii=False)


if __name__ == "__main__":
    _log("[memory-mcp] server starting")
    mcp.run()
