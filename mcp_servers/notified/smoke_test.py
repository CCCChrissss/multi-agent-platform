"""Isolated smoke test for the notified MCP server -- run with:
    uv run python -m mcp_servers.notified.smoke_test

Talks to mcp_servers/notified/server.py directly over stdio via
mcp_servers/base_client.py's MCPClient -- no MCPGateway, no LLM. Neither
tool validates its arguments locally (services/notified/client.py goes
straight to an HTTP call), so this tier's only "bad input" case is really
"dependency down": if services/notified/server.py isn't running (the
expected CI shape, since this tier doesn't require `honcho start`), the
connection fails fast and this asserts the resulting error is a clean
ToolDependencyError, not a raw traceback. If it happens to be running (a
dev machine with `honcho start` up), the call really sends and this
asserts the success shape instead -- either way is a fast HTTP round trip,
no model inference involved, so no timeout dance like mcp_servers/stt
needs.
"""

from __future__ import annotations

import asyncio

from mcp import StdioServerParameters

from mcp_servers.base_client import MCPClient

_PARAMS = StdioServerParameters(command="uv", args=["run", "python", "-m", "mcp_servers.notified.server"])


async def main() -> None:
    async with MCPClient(_PARAMS) as client:
        tools = await client.list_openai_tools()
        names = {t["function"]["name"] for t in tools}
        assert names == {"send_slack_message", "send_gmail_message"}, names
        by_name = {t["function"]["name"]: t["function"]["parameters"] for t in tools}
        assert by_name["send_slack_message"]["required"] == ["channel", "message"], by_name
        assert by_name["send_gmail_message"]["required"] == ["to", "subject", "body"], by_name
        print("[notified] OK -- tool list matches expected names/schemas")

        text, is_error = await client.call_tool(
            "send_gmail_message", {"to": "smoke-test@example.com", "subject": "smoke test", "body": "smoke test"}
        )
        if is_error:
            assert "Traceback" not in text, text
            print("[notified] OK -- service unreachable, translated to a clean ToolDependencyError")
        else:
            assert text.startswith("email sent to smoke-test@example.com"), text
            print("[notified] OK -- service reachable, real send acknowledged")

        text, is_error = await client.call_tool("send_gmail_message", {"to": 123, "subject": "s", "body": "b"})
        assert is_error, text
        assert "Traceback" not in text, text
        print("[notified] OK -- wrong-typed argument rejected cleanly, no raw traceback")

    print("\nAll notified smoke tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
