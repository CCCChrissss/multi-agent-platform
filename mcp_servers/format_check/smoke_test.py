"""Isolated smoke test for the format_check MCP server -- run with:
    uv run python -m mcp_servers.format_check.smoke_test

Talks to mcp_servers/format_check/server.py directly over stdio via
mcp_servers/base_client.py's MCPClient -- no MCPGateway, no LLM, no other
MCP server, no other service (this server has no backing service at all).
Like mcp_servers/lookup, a missing/unsupported file is this tool's own
designed answer (`{"valid": false, "reason": ...}`, not a raised error --
see server.py's docstring), so "bad input" here is a wrong-typed argument,
caught by FastMCP's schema validation before the tool body runs. Should
complete in well under a second.
"""

from __future__ import annotations

import asyncio
import json

from mcp import StdioServerParameters

from mcp_servers.base_client import MCPClient

_PARAMS = StdioServerParameters(command="uv", args=["run", "python", "-m", "mcp_servers.format_check.server"])


async def main() -> None:
    async with MCPClient(_PARAMS) as client:
        tools = await client.list_openai_tools()
        names = {t["function"]["name"] for t in tools}
        assert names == {"check_audio_format"}, names
        schema = tools[0]["function"]["parameters"]
        assert schema["required"] == ["audio_path"], schema
        assert schema["properties"]["audio_path"]["type"] == "string", schema
        print("[format_check] OK -- tool list matches expected name/schema")

        text, is_error = await client.call_tool("check_audio_format", {"audio_path": "samples/test_zh_tw.wav"})
        assert not is_error, text
        assert json.loads(text) == {"valid": True, "reason": None}, text
        print("[format_check] OK -- existing .wav file reports valid=True")

        text, is_error = await client.call_tool("check_audio_format", {"audio_path": "/no/such/file.wav"})
        assert not is_error, text
        result = json.loads(text)
        assert result["valid"] is False and "not found" in result["reason"], result
        print("[format_check] OK -- missing file reports valid=False, not a crash")

        text, is_error = await client.call_tool("check_audio_format", {"audio_path": "README.md"})
        assert not is_error, text
        result = json.loads(text)
        assert result["valid"] is False and "unsupported format" in result["reason"], result
        print("[format_check] OK -- unsupported extension reports valid=False, not a crash")

        text, is_error = await client.call_tool("check_audio_format", {"audio_path": 123})
        assert is_error, text
        assert "Traceback" not in text, text
        print("[format_check] OK -- wrong-typed argument rejected cleanly, no raw traceback")

    print("\nAll format_check smoke tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
