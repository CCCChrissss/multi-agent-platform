"""Access policy for the shared MCP gateway: parsing and permission resolution.

The gateway is shared across every agent, so "which tools may this caller
use" can no longer be expressed by which clients got wired into a per-agent
gateway. Instead a `principal` (an agent/node identity) is resolved against
this policy -- a hybrid RBAC model:

  - `roles` are reusable tool bundles (grant tools to a role once, reuse it).
  - a principal reuses roles, and/or grants tools directly with `allow`,
    and/or carves tools out with `deny`.

Resolution: expand the principal's roles -> union every `allow` -> subtract
every `deny`. deny always wins. An unknown principal resolves to no tools
(fail-closed), never to "everything".

Tool names are namespaced `<server>__<tool>`; a pattern `<server>__*`
matches every tool on that server, `<server>__<tool>` matches exactly one.
Everything here is pure (no I/O beyond load_policy reading the file), so the
permission logic is unit-testable on its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import yaml

_WILDCARD_SUFFIX = "__*"


@dataclass(frozen=True)
class ServerSpec:
    module: str


@dataclass(frozen=True)
class Grant:
    allow: frozenset[str] = frozenset()
    deny: frozenset[str] = frozenset()
    roles: tuple[str, ...] = ()


@dataclass(frozen=True)
class Policy:
    servers: dict[str, ServerSpec] = field(default_factory=dict)
    roles: dict[str, Grant] = field(default_factory=dict)
    principals: dict[str, Grant] = field(default_factory=dict)


@dataclass(frozen=True)
class Permissions:
    """A principal's resolved allow/deny pattern sets. deny is kept separate
    (not pre-subtracted) so a specific `deny` can override an `allow` wildcard
    at match time -- set subtraction can't, since `notified__*` and
    `notified__send_slack_message` are different strings."""

    allow: frozenset[str] = frozenset()
    deny: frozenset[str] = frozenset()


def _grant(raw: dict | None) -> Grant:
    raw = raw or {}
    return Grant(
        allow=frozenset(raw.get("allow", [])),
        deny=frozenset(raw.get("deny", [])),
        roles=tuple(raw.get("roles", [])),
    )


def load_policy(path: str) -> Policy:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return Policy(
        servers={name: ServerSpec(**spec) for name, spec in (raw.get("servers") or {}).items()},
        roles={name: _grant(g) for name, g in (raw.get("roles") or {}).items()},
        principals={name: _grant(g) for name, g in (raw.get("principals") or {}).items()},
    )


def resolve_allowed(policy: Policy, principal: str | None) -> Permissions:
    """The allow/deny patterns `principal` may use. Empty for unknown (including
    None, e.g. a call made outside any node's context) principals."""
    grant = policy.principals.get(principal)
    if grant is None:
        return Permissions()  # fail-closed: unknown principal gets nothing

    allow: set[str] = set(grant.allow)
    deny: set[str] = set(grant.deny)
    for role_name in grant.roles:
        role = policy.roles.get(role_name)
        if role is not None:
            allow |= role.allow
            deny |= role.deny
    return Permissions(allow=frozenset(allow), deny=frozenset(deny))


def _matches(patterns: frozenset[str], tool_name: str) -> bool:
    if tool_name in patterns:
        return True
    server, _, _ = tool_name.partition("__")
    return f"{server}{_WILDCARD_SUFFIX}" in patterns


def is_allowed(perms: Permissions, tool_name: str) -> bool:
    """Whether `tool_name` (namespaced `<server>__<tool>`) is permitted: it must
    match an allow pattern and no deny pattern. deny wins (a specific deny beats
    an allow wildcard, and a deny wildcard beats a specific allow)."""
    if _matches(perms.deny, tool_name):
        return False
    return _matches(perms.allow, tool_name)
