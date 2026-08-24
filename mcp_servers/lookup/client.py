"""Persistent stdio MCP client for the lookup server.

See mcp_servers/base_client.py for the actual connect/session/call-tool
logic -- this module only wires up the server-specific launch command.
"""

from __future__ import annotations

from mcp import StdioServerParameters

from mcp_servers.base_client import MCPClient

SERVER_PARAMS = StdioServerParameters(
    command="uv",
    args=["run", "python", "-m", "mcp_servers.lookup.server"],
)


class LookupMCPClient(MCPClient):
    def __init__(self) -> None:
        super().__init__(SERVER_PARAMS)
