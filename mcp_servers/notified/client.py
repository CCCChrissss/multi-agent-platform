"""Persistent stdio MCP client for the notified server.

See mcp_servers/base_client.py for the actual connect/session/call-tool
logic -- this module only wires up the server-specific launch command.
"""

from __future__ import annotations

from mcp import StdioServerParameters

from mcp_servers.base_client import MCPClient

# Run as `-m` (not a bare script path) so `services.notified` resolves as a
# top-level import -- a bare script path only puts its own directory on
# sys.path, not the repo root.
SERVER_PARAMS = StdioServerParameters(
    command="uv",
    args=["run", "python", "-m", "mcp_servers.notified.server"],
)


class NotifiedMCPClient(MCPClient):
    def __init__(self) -> None:
        super().__init__(SERVER_PARAMS)
