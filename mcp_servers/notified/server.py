"""MCP server exposing notification tools (Slack, Gmail).

Thin wrapper around services/notified/client.py -- the actual send logic
(currently placeholder/fake) lives there so a future pure-logic node can
call it directly without duplicating it, the same way mcp_servers/stt wraps
services/stt. More channels (Teams, SMS, ...) get added to
services/notified first, then exposed here as new @mcp.tool()s.
"""

from __future__ import annotations

import sys

from mcp.server.fastmcp import FastMCP

from mcp_servers.tool_errors import guarded_tool
from services.notified.client import send_gmail_message as service_send_gmail_message
from services.notified.client import send_slack_message as service_send_slack_message

mcp = FastMCP("notified", log_level="WARNING")


def _log(line: str) -> None:
    # stdio transport reserves stdout for JSON-RPC -- app logs must go to stderr
    print(line, file=sys.stderr, flush=True)


@mcp.tool()
@guarded_tool(_log, "send_slack_message")
def send_slack_message(channel: str, message: str) -> str:
    """Send a message to a Slack channel."""
    _log(f"[notified-mcp] routing to services.notified: slack -> {channel}")
    return service_send_slack_message(channel, message)


@mcp.tool()
@guarded_tool(_log, "send_gmail_message")
def send_gmail_message(to: str, subject: str, body: str) -> str:
    """Send an email via Gmail."""
    _log(f"[notified-mcp] routing to services.notified: gmail -> {to}")
    return service_send_gmail_message(to, subject, body)


if __name__ == "__main__":
    _log("[notified-mcp] server starting")
    mcp.run()
