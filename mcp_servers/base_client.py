"""Generic persistent stdio MCP client, shared by every mcp_servers/*/client.py.

Connects once (`connect()` / async context manager) and reuses the same
session for every call, matching how real MCP hosts keep one long-lived
connection instead of spawning a fresh server process per tool call.
"""

from __future__ import annotations

from contextlib import AsyncExitStack

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class MCPClient:
    def __init__(self, server_params: StdioServerParameters) -> None:
        self._server_params = server_params
        self._stack = AsyncExitStack()
        self.session: ClientSession | None = None

    async def connect(self) -> None:
        read, write = await self._stack.enter_async_context(stdio_client(self._server_params))
        self.session = await self._stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()

    async def close(self) -> None:
        await self._stack.aclose()
        self.session = None

    async def __aenter__(self) -> MCPClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def list_openai_tools(self) -> list[dict]:
        assert self.session is not None, "call connect() first"
        mcp_tools = (await self.session.list_tools()).tools
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.inputSchema,
                },
            }
            for tool in mcp_tools
        ]

    async def call_tool(self, tool_name: str, arguments: dict) -> tuple[str, bool]:
        """Returns (result_text, is_error) -- never raises."""
        assert self.session is not None, "call connect() first"
        try:
            result = await self.session.call_tool(tool_name, arguments)
            text = getattr(result.content[0], "text", "") if result.content else ""
            return text, bool(getattr(result, "isError", False))
        except Exception as exc:  # transport/protocol errors, unknown tool, etc.
            return f"error calling {tool_name}: {exc}", True
