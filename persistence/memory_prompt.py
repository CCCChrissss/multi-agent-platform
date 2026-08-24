"""Platform-level rendering for procedural memory into prompt text.

Episodic memory (`{"input": str, "output": str}`, docs/knowledge-
distillation-plan.md P5) is deliberately NOT rendered here anymore -- it's
recorded by orchestrator/memory_writer.py purely as raw material for
scripts/distill_procedural.py to generalize into procedural rules, reviewed
by scripts/review_episodic.py, and never injected into any agent's prompt.
Splicing a live judgment (right or wrong, never human-checked) straight into
the next similar case's few-shot taught the model by demonstration, which
turned out to be a bigger risk than a stated rule -- see this module's git
history for the old recall_episodic_few_shot() and the investigation that
motivated removing it.

procedural memory has a platform-standardized `content` shape regardless of
which agent reads or writes it: `{"rule": str}`. Reading is therefore purely
mechanical (this module never needs to know which agent is calling).

track_browse_result()/render_explored_map() (docs/exclusion-scenario-plan.md
§2's browse(), added when llm/exclusion_judge.py needed it) are the same
kind of platform-mechanical helper for a different memory kind: turning
scattered browse_semantic_memory tool results into one reliable "what have I
already seen" view, so any future tool-calling agent that browses a
namespace tree gets this for free instead of hand-rolling its own tracking.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from persistence.memory import MemoryKind, recall
from persistence.memory_policy import MemoryPolicy

_PROCEDURAL_HEADER = "過去累積的判斷規則："
_EXPLORED_MAP_HEADER = "已探索地圖（只記錄看過的分支/內容，不代表接下來該去哪）："


async def inject_procedural(
    store: Any | None,
    memory_policy: MemoryPolicy | None,
    *,
    tenant: str,
    scope: Sequence[str],
    base_prompt: str,
    limit: int = 10,
) -> str:
    """Appends recalled procedural rules to `base_prompt` as a bulleted
    list. None store/policy, or nothing found (no memory yet, or the
    principal has no read grant -- recall() fails closed), returns
    `base_prompt` unchanged -- the same no-op contract every caller
    (workflows/simple_pipeline.py included) already relies on."""
    if store is None or memory_policy is None:
        return base_prompt
    items = await recall(store, memory_policy, MemoryKind.PROCEDURAL, tenant=tenant, scope=scope, limit=limit)
    if not items:
        return base_prompt
    rules = "\n".join(f"- {item.value['content']['rule']}" for item in items)
    return f"{base_prompt}\n\n{_PROCEDURAL_HEADER}\n{rules}"


def track_browse_result(explored: dict[tuple[str, ...], dict], browse_result_json: str) -> None:
    """Records one `memory__browse_semantic_memory` tool result into
    `explored`, keyed by its own scope -- the caller's loop owns `explored`
    (starts it as `{}`, same convention as a plain accumulator variable like
    a `seen` set) and calls this once per browse tool result. Purely
    mechanical, like the rest of this module: doesn't know or care what the
    tree contains, doesn't decide anything -- an agent still has to browse()
    to see something before this can record it, and where to browse next is
    still entirely the agent's own call (docs/exclusion-scenario-plan.md
    §2.3's "the platform doesn't pick the traversal strategy" holds; this
    only makes what's already been seen reliable to look back on, instead of
    the agent re-deriving it from a pile of scattered raw tool results
    itself).

    A denied browse() returns `{}` (persistence/memory.py's fail-closed
    contract) -- silently skipped, nothing to record."""
    try:
        result = json.loads(browse_result_json)
    except (TypeError, ValueError):
        return
    if not result:
        return
    explored[tuple(result.get("scope", []))] = result


def render_explored_map(explored: dict[tuple[str, ...], dict]) -> str:
    """Renders everything accumulated via track_browse_result() into a
    compact indented outline, ready to splice into a chat `messages` list as
    its own message. `{}` (nothing explored yet) renders to `""` -- callers
    should skip appending an empty map rather than send a header with no
    body.

    Sorting by scope tuple is what gives parent-before-child ordering for
    free: a tuple that's a prefix of another always sorts before it in
    Python (`("a",) < ("a", "b")`), so no separate tree-building pass is
    needed -- indentation is just `len(scope)`."""
    if not explored:
        return ""
    lines = [_EXPLORED_MAP_HEADER]
    for scope in sorted(explored):
        node = explored[scope]
        label = scope[-1] if scope else "(根)"
        indent = "  " * len(scope)
        marker = ""
        items = node.get("items") or []
        if items:
            keys = "、".join(item["key"] for item in items if item.get("key"))
            marker = f"　✓ 已讀全文：{keys}"
        lines.append(f"{indent}{label}{marker}")
    return "\n".join(lines)
