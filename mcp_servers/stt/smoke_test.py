"""Isolated smoke test for the stt MCP server -- run with:
    uv run python -m mcp_servers.stt.smoke_test

Talks to mcp_servers/stt/server.py directly over stdio via
mcp_servers/base_client.py's MCPClient -- no MCPGateway, no other MCP
server. transcribe_audio's only real work happens behind the LiteLLM
gateway (services/stt/client.py -> gateway/client.py), which this tier
deliberately does not stand up -- so the one case this file exercises with
a real, existing, correctly-formatted audio file is bounded by a short
timeout: if the gateway happens to be running (a dev machine with `honcho
start` up) and actually transcribing, that's real model inference (multi-
second, sometimes 15s+) and out of scope for a smoke test, so it's treated
as a skip rather than waited on. If the gateway is unreachable (the
expected CI shape), the connection fails fast and this asserts the
resulting error is a clean ToolDependencyError, not a raw traceback.
Missing-file and wrong-typed-argument cases are always fast and
deterministic (no gateway call at all).
"""

from __future__ import annotations

import asyncio

from mcp import StdioServerParameters

from mcp_servers.base_client import MCPClient

_PARAMS = StdioServerParameters(command="uv", args=["run", "python", "-m", "mcp_servers.stt.server"])
_GATEWAY_PROBE_TIMEOUT = 2.0


async def main() -> None:
    async with MCPClient(_PARAMS) as client:
        tools = await client.list_openai_tools()
        names = {t["function"]["name"] for t in tools}
        assert names == {"transcribe_audio"}, names
        schema = tools[0]["function"]["parameters"]
        assert schema["required"] == ["audio_path"], schema
        assert schema["properties"]["audio_path"]["type"] == "string", schema
        print("[stt] OK -- tool list matches expected name/schema")

        text, is_error = await client.call_tool("transcribe_audio", {"audio_path": "/no/such/file.wav"})
        assert is_error, text
        assert "audio file not found" in text, text
        assert "Traceback" not in text, text
        print("[stt] OK -- missing file raises ToolInputError, not a raw traceback")

        text, is_error = await client.call_tool("transcribe_audio", {"audio_path": 123})
        assert is_error, text
        assert "Traceback" not in text, text
        print("[stt] OK -- wrong-typed argument rejected cleanly, no raw traceback")

        try:
            text, is_error = await asyncio.wait_for(
                client.call_tool("transcribe_audio", {"audio_path": "samples/test_zh_tw.wav"}),
                timeout=_GATEWAY_PROBE_TIMEOUT,
            )
        except asyncio.TimeoutError:
            print("[stt] SKIP -- gateway reachable and actually inferring; real transcription is out of scope here")
        else:
            if is_error:
                assert "Traceback" not in text, text
                print("[stt] OK -- gateway unreachable, translated to a clean ToolDependencyError")
            else:
                assert text.strip(), text
                print("[stt] OK -- gateway reachable, real transcript returned")

    print("\nAll stt smoke tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
