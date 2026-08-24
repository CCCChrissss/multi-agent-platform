"""Shared MCP gateway: one instance for the whole platform, policy-gated.

Every agent talks to the *same* gateway rather than getting its own wired
with just the tools it needs. The gateway launches each MCP server declared
in the policy once (one stdio subprocess, one session), namespaces tool
names per server (`<server>__<tool>`), and gates access by `principal` against
the policy -- see mcp_servers/policy.py. A principal is a LangGraph node's
identity: every node is (or will be) its own agent, so "which node is
running" and "which permission set applies" are the same question. Rather
than have every caller thread `principal` down manually (and risk it drifting
from the node name), it's read straight off `current_node_name` -- the same
contextvar the workflow already sets once per node for call_log correlation
(see workflows/simple_pipeline.py) and persistence/call_log.py's log_call
already reads for the `node` column. That column doubling as the audit
record of principal is intentional, not a gap: they're defined to be the
same thing.

Access control is enforced twice, fail-closed:
  - list_openai_tools() hides tools the principal may not use (UX),
  - call_tool(...) re-checks before dispatching (security). A shared gateway
    loses the old "not wired in, so uncallable" guarantee, so the call-time
    check is the real boundary; the list-time filter is only there to keep
    the model from trying tools it can't use.
"""

from __future__ import annotations

import time
from contextlib import AsyncExitStack

from mcp import StdioServerParameters

from mcp_servers.base_client import MCPClient
from mcp_servers.policy import Policy, is_allowed, resolve_allowed
from persistence.call_log import current_node_name, log_call

_SEPARATOR = "__"


class MCPGateway:
    def __init__(self, policy: Policy, *, principal: str | None = None) -> None:
        self._policy = policy
        self._clients: dict[str, MCPClient] = {}
        self._stack = AsyncExitStack()
        # A server subprocess can't read current_node_name -- that's a
        # ContextVar, scoped to this process, not the child it spawns over
        # stdio. But the identity of "which agent owns this gateway" is a
        # fixed fact for the whole process lifetime (agents/notified/server.py
        # always calls run_handler("notified", ...), never any other node
        # name), so it's passed down once here rather than needing to cross
        # the process boundary per-call. Only a server whose tools need to
        # know the calling principal (mcp_servers/memory/server.py, so it can
        # enforce persistence/memory_policy.py's can_read()) actually reads
        # this env var -- every other server ignores it.
        self._principal = principal

    async def connect(self) -> None:
        env = {"MCP_CALLING_PRINCIPAL": self._principal} if self._principal else None
        for namespace, spec in self._policy.servers.items():
            client = MCPClient(
                StdioServerParameters(command="uv", args=["run", "python", "-m", spec.module], env=env)
            )
            await self._stack.enter_async_context(client)
            self._clients[namespace] = client

    async def close(self) -> None:
        await self._stack.aclose()

    async def __aenter__(self) -> MCPGateway:
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def list_openai_tools(self) -> list[dict]:
        allowed = resolve_allowed(self._policy, current_node_name.get())
        tools: list[dict] = []
        for namespace, client in self._clients.items():
            for tool in await client.list_openai_tools():
                name = f"{namespace}{_SEPARATOR}{tool['function']['name']}"
                if not is_allowed(allowed, name):
                    continue
                tool["function"]["name"] = name
                tools.append(tool)
        return tools

    async def call_tool(
        self, tool_name: str, arguments: dict, tool_call_id: str | None = None
    ) -> tuple[str, bool]:
        principal = current_node_name.get()
        # Security boundary: re-check at call time, never trust that the model
        # only picked from the filtered list. Fail closed on anything not allowed.
        if not is_allowed(resolve_allowed(self._policy, principal), tool_name):
            result_text = f"error: '{principal}' is not permitted to call '{tool_name}'"
            await log_call("tool", tool_name, arguments, result_text, True, 0, denied=True, tool_call_id=tool_call_id)
            return result_text, True

        namespace, _, real_name = tool_name.partition(_SEPARATOR)
        client = self._clients.get(namespace)
        if client is None:
            result_text = f"error: unknown tool namespace in '{tool_name}'"
            await log_call("tool", tool_name, arguments, result_text, True, 0, tool_call_id=tool_call_id)
            return result_text, True

        start = time.monotonic()
        result_text, is_error = await client.call_tool(real_name, arguments)
        latency_ms = int((time.monotonic() - start) * 1000)
        await log_call("tool", tool_name, arguments, result_text, is_error, latency_ms, tool_call_id=tool_call_id)
        return result_text, is_error
