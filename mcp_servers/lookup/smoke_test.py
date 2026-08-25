"""Isolated smoke test for the lookup MCP server -- run with:
    uv run python -m mcp_servers.lookup.smoke_test

Talks to mcp_servers/lookup/server.py directly over stdio via
mcp_servers/base_client.py's MCPClient -- no MCPGateway, no LLM, no other
MCP server. This server has no downstream dependency and no way for a
well-typed string argument to raise inside query_company_profile(), so
"bad input" here means an unmatched company name (the tool's own designed
failure shape: an explicit `_UNKNOWN_PROFILE`, not a crash -- see
mcp_servers/lookup/server.py's docstring) and a wrong-typed argument
(caught by FastMCP's schema validation before the tool body ever runs).
Should complete in well under a second.
"""

from __future__ import annotations

import asyncio
import json

from mcp import StdioServerParameters

from mcp_servers.base_client import MCPClient

_PARAMS = StdioServerParameters(command="uv", args=["run", "python", "-m", "mcp_servers.lookup.server"])


async def main() -> None:
    async with MCPClient(_PARAMS) as client:
        tools = await client.list_openai_tools()
        names = {t["function"]["name"] for t in tools}
        assert names == {"query_company_profile"}, names
        schema = tools[0]["function"]["parameters"]
        assert schema["required"] == ["company"], schema
        assert schema["properties"]["company"]["type"] == "string", schema
        print("[lookup] OK -- tool list matches expected name/schema")

        text, is_error = await client.call_tool("query_company_profile", {"company": "台積電"})
        assert not is_error, text
        profile = json.loads(text)
        assert profile["watchlist"] is True, profile
        assert "台積電" in profile["aliases"], profile
        print("[lookup] OK -- known company returns watchlist=True with aliases")

        text, is_error = await client.call_tool("query_company_profile", {"company": "不存在的公司名稱"})
        assert not is_error, text
        profile = json.loads(text)
        assert profile == {
            "official_name": None,
            "aliases": [],
            "industry": None,
            "watchlist": False,
            "note": "查無資料，非監控清單公司",
        }, profile
        print("[lookup] OK -- unmatched company returns explicit-empty profile, not a crash")

        text, is_error = await client.call_tool("query_company_profile", {"company": 123})
        assert is_error, text
        assert "Traceback" not in text, text
        print("[lookup] OK -- wrong-typed argument rejected cleanly, no raw traceback")

    print("\nAll lookup smoke tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
