"""Shared exception vocabulary for services/*.

These carry no MCP-specific knowledge -- they're the business-logic layer's
way of classifying failures so callers (currently mcp_servers/*/server.py)
can tell "retrying with different input might help" from "the dependency is
down, retrying the same input won't help" without parsing exception text.

The message on each raised instance IS the agent-facing feedback -- write it
as an instruction, not a description (see docs/harness-engineering-principles.md,
"回饋時機一:工具回傳值").
"""

from __future__ import annotations


class ToolInputError(ValueError):
    """The call's own input/preconditions don't hold (missing file, bad format).

    Retrying with different input has a real chance of succeeding.
    """


class ToolDependencyError(RuntimeError):
    """A downstream dependency failed (service down, timeout, 5xx).

    Retrying with different input won't help; retrying later or checking the
    dependency might.
    """
