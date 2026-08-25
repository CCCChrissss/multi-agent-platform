"""Isolated smoke test for the memory MCP server -- run with:
    uv run python -m mcp_servers.memory.smoke_test

Talks to mcp_servers/memory/server.py directly over stdio via
mcp_servers/base_client.py's MCPClient -- no MCPGateway, no LLM, no other
MCP server. Unlike the other four servers' smoke tests, this one needs
Postgres (the server opens the real long-term memory store on first
tool call) -- that's the one dependency this tier doesn't try to avoid,
per docs/testing.md. No Ollama/LiteLLM needed: every call here omits
`query`, so recall()/browse() never reach the embedding path.

`MCP_CALLING_PRINCIPAL` can only be set at subprocess spawn time (see
mcp_servers/gateway.py's connect()), so each principal under test gets its
own client/subprocess rather than one shared connection.

Fail-closed coverage (mcp_servers/memory/server.py's module docstring):
recall/browse must return empty -- not raise, not silently succeed -- for
a principal with no `memory:` grant in policy.yaml, whether that's because
MCP_CALLING_PRINCIPAL was never set (a call made outside any node's
context) or because it names a real but unauthorized principal. Since an
empty result and a denied-but-genuinely-no-data result look identical from
the response alone, denial is verified through persistence/call_log.py's
`denied` column instead of the response body.
"""

from __future__ import annotations

import asyncio
import json
import uuid

from mcp import StdioServerParameters

from mcp_servers.base_client import MCPClient
from persistence.call_log import ensure_schema as ensure_call_log_schema, fetch_calls

_MODULE = "mcp_servers.memory.server"


def _params(principal: str | None) -> StdioServerParameters:
    env = {"MCP_CALLING_PRINCIPAL": principal} if principal else None
    return StdioServerParameters(command="uv", args=["run", "python", "-m", _MODULE], env=env)


async def _assert_denied(principal: str | None, label: str) -> None:
    recall_thread = f"smoke-memory-{label}-recall-{uuid.uuid4().hex[:8]}"
    browse_thread = f"smoke-memory-{label}-browse-{uuid.uuid4().hex[:8]}"
    async with MCPClient(_params(principal)) as client:
        text, is_error = await client.call_tool(
            "recall_semantic_memory", {"scope": ["recipient", "smoke-test"], "thread_id": recall_thread}
        )
        assert not is_error, text
        assert json.loads(text)["results"] == [], text

        text, is_error = await client.call_tool(
            "browse_semantic_memory", {"scope": ["company"], "thread_id": browse_thread}
        )
        assert not is_error, text
        assert json.loads(text) == {}, text

    recall_rows = await fetch_calls(recall_thread)
    browse_rows = await fetch_calls(browse_thread)
    assert recall_rows and recall_rows[0]["denied"] is True, recall_rows
    assert browse_rows and browse_rows[0]["denied"] is True, browse_rows
    print(f"[memory] OK -- principal={principal!r} ({label}) fails closed: empty result, denied=True in call_log")


async def _assert_allowed(principal: str, tool_name: str, arguments: dict) -> None:
    thread_id = f"smoke-memory-allowed-{principal}-{uuid.uuid4().hex[:8]}"
    async with MCPClient(_params(principal)) as client:
        text, is_error = await client.call_tool(tool_name, {**arguments, "thread_id": thread_id})
    assert not is_error, text

    rows = await fetch_calls(thread_id)
    assert rows and rows[0]["denied"] is False, rows
    print(f"[memory] OK -- principal={principal!r} is granted {tool_name}, denied=False in call_log")


async def main() -> None:
    ensure_call_log_schema()

    async with MCPClient(_params(None)) as client:
        tools = await client.list_openai_tools()
        names = {t["function"]["name"] for t in tools}
        assert names == {"recall_semantic_memory", "browse_semantic_memory"}, names
        by_name = {t["function"]["name"]: t["function"]["parameters"] for t in tools}
        assert by_name["recall_semantic_memory"]["required"] == ["scope"], by_name
        print("[memory] OK -- tool list matches expected names/schemas")

        text, is_error = await client.call_tool("recall_semantic_memory", {"scope": []})
        assert is_error, text
        assert "scope must be a non-empty list" in text, text
        assert "Traceback" not in text, text
        print("[memory] OK -- empty scope raises ToolInputError, not a raw traceback")

    await _assert_denied(None, "unset")
    await _assert_denied("totally-unauthorized-principal", "unknown")

    # policy.yaml grants: notified -> default/semantic/recipient/*, check -> _global/semantic/company/* (browsable)
    await _assert_allowed("notified", "recall_semantic_memory", {"scope": ["recipient", "smoke-test-recipient"]})
    await _assert_allowed("check", "browse_semantic_memory", {"scope": ["company"]})

    print("\nAll memory smoke tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
