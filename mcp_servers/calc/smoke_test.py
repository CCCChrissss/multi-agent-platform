"""Isolated smoke test for the calc MCP server -- run with:
    uv run python -m mcp_servers.calc.smoke_test

Talks to mcp_servers/calc/server.py directly over stdio via
mcp_servers/base_client.py's MCPClient -- no MCPGateway, no LLM, no other
MCP server. Should complete in well under a second.

The interesting half is the rejection cases: `expression` is a model-written
string on a trust boundary, so the point isn't only "does 3 + 5 * 2 come back
as 13" but "does anything that isn't plain arithmetic get refused before it
evaluates". `9 ** 9 ** 9` is the one that would otherwise hang this test
rather than fail it.
"""

from __future__ import annotations

import asyncio
import json

from mcp import StdioServerParameters

from mcp_servers.base_client import MCPClient

_PARAMS = StdioServerParameters(command="uv", args=["run", "python", "-m", "mcp_servers.calc.server"])

_OK_CASES = [
    ("3 + 5 * 2", 13),
    ("(3 + 5) * 2", 16),
    ("10 / 4", 2.5),
    ("10 // 4", 2),
    ("10 % 4", 2),
    ("2 ** 10", 1024),
    ("-3 + 1", -2),
    ("1.5 * 4", 6.0),
]

_REJECTED_CASES = [
    ("__import__('os').system('echo pwned')", "call/name is refused, not executed"),
    ("os.getcwd()", "attribute access is refused"),
    ("x + 1", "bare names are refused"),
    ("9 ** 9 ** 9", "oversized exponent is refused before it evaluates"),
    ("1 / 0", "division by zero is reported, not raised as a traceback"),
    ("3 +", "syntax error is reported cleanly"),
    ("", "empty expression is refused"),
]


async def main() -> None:
    async with MCPClient(_PARAMS) as client:
        tools = await client.list_openai_tools()
        names = {t["function"]["name"] for t in tools}
        assert names == {"evaluate"}, names
        schema = tools[0]["function"]["parameters"]
        assert schema["required"] == ["expression"], schema
        assert schema["properties"]["expression"]["type"] == "string", schema
        print("[calc] OK -- tool list matches expected name/schema")

        for expression, expected in _OK_CASES:
            text, is_error = await client.call_tool("evaluate", {"expression": expression})
            assert not is_error, (expression, text)
            payload = json.loads(text)
            assert payload["result"] == expected, (expression, payload, expected)
        print(f"[calc] OK -- {len(_OK_CASES)} arithmetic cases evaluate correctly")

        for expression, what in _REJECTED_CASES:
            text, is_error = await client.call_tool("evaluate", {"expression": expression})
            assert is_error, (expression, text)
            assert "Traceback" not in text, (expression, text)
            print(f"[calc] OK -- {what}")

        text, is_error = await client.call_tool("evaluate", {"expression": 123})
        assert is_error, text
        assert "Traceback" not in text, text
        print("[calc] OK -- wrong-typed argument rejected cleanly, no raw traceback")

    print("\nAll calc smoke tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
