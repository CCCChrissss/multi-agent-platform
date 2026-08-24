"""Persistent stdio MCP client for the format_check server.

See mcp_servers/base_client.py for the actual connect/session/call-tool
logic -- this module only wires up the server-specific launch command.
"""

from __future__ import annotations

from mcp import StdioServerParameters

from mcp_servers.base_client import MCPClient

SERVER_PARAMS = StdioServerParameters(
    command="uv",
    args=["run", "python", "-m", "mcp_servers.format_check.server"],
)


class FormatCheckMCPClient(MCPClient):
    def __init__(self) -> None:
        super().__init__(SERVER_PARAMS)
