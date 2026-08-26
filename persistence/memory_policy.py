"""Access policy for long-term memory: parsing and permission resolution.

Same governance surface as mcp_servers/policy.py (one file, mcp_servers/policy.yaml,
edited by a future no-code UI) and the same fail-closed philosophy, but a
different resource: a namespace path in the memory store, glob-matched,
rather than a `<server>__<tool>` name resolved through roles. The verbs are
also just read/write, not allow/deny+roles, so this doesn't reuse
mcp_servers.policy.Policy/Grant -- the shapes don't fit.

An unknown principal (or one with no `memory:` entry) resolves to no access,
never to "everything".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatch

import yaml


@dataclass(frozen=True)
class MemoryGrant:
    read: tuple[str, ...] = ()
    write: tuple[str, ...] = ()


@dataclass(frozen=True)
class MemoryPolicy:
    principals: dict[str, MemoryGrant] = field(default_factory=dict)


def load_memory_policy(path: str) -> MemoryPolicy:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    memory_raw = raw.get("memory") or {}
    return MemoryPolicy(
        principals={
            name: MemoryGrant(read=tuple(grant.get("read", [])), write=tuple(grant.get("write", [])))
            for name, grant in memory_raw.items()
        }
    )


def _namespace_str(namespace: tuple[str, ...]) -> str:
    return "/".join(namespace)


def _is_browsable_ancestor(ns: str, pattern: str) -> bool:
    """True if `ns` sits at or above `pattern`'s first wildcard segment --
    i.e. it's a directory browse() may list into because something further
    down would be granted, even though `ns` itself doesn't match `pattern`
    (e.g. `ns="_global/semantic"` against pattern
    `"_global/semantic/insurance_product/*"`, or even `ns=""`, the root).
    Walks `pattern`'s literal (non-glob) prefix segments and checks `ns` is
    a prefix of those -- general in namespace depth, unlike a single
    string comparison, so it also covers browsing from the root, not just
    one level short of the grant."""
    ns_parts = ns.split("/") if ns else []
    literal_prefix: list[str] = []
    for part in pattern.split("/"):
        if any(ch in part for ch in "*?["):
            break
        literal_prefix.append(part)
    return len(ns_parts) <= len(literal_prefix) and ns_parts == literal_prefix[: len(ns_parts)]


def can_read(
    policy: MemoryPolicy, principal: str | None, namespace: tuple[str, ...], *, for_browse: bool = False
) -> bool:
    """`namespace` is usually a complete key (recall()'s case). persistence/
    memory.py's browse() instead passes a *prefix* -- a namespace that
    doesn't reach all the way down to a leaf yet -- and sets `for_browse`
    so an incomplete-but-genuinely-a-directory prefix (e.g.
    `_global/semantic` when the grant pattern is
    `_global/semantic/insurance_product/*`) is still readable enough to
    list one level down. `for_browse` must stay False for recall(): the
    ancestor check makes an *incomplete* namespace match a grant that
    requires more segments, which is fine for browse()'s "what's here"
    listing (segment names + one-line summaries, no content) but would let
    recall() return every leaf under an under-specified scope -- e.g.
    `notified`'s `default/semantic/recipient/*` grant must require the
    caller name a specific recipient id, not just "recipient"."""
    grant = policy.principals.get(principal) if principal else None
    if grant is None:
        return False
    ns = _namespace_str(namespace)
    if any(fnmatch(ns, pattern) for pattern in grant.read):
        return True
    return for_browse and any(_is_browsable_ancestor(ns, pattern) for pattern in grant.read)


def can_write(policy: MemoryPolicy, principal: str | None, namespace: tuple[str, ...]) -> bool:
    grant = policy.principals.get(principal) if principal else None
    if grant is None:
        return False
    ns = _namespace_str(namespace)
    return any(fnmatch(ns, pattern) for pattern in grant.write)
