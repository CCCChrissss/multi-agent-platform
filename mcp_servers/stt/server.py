"""MCP server exposing an STT tool.

Lets an agent node dynamically decide to transcribe audio, without needing
to know it's just an HTTP call underneath -- the STT service
(services/stt/server.py) still does the actual model inference. This is a
thin wrapper, not a second implementation: it reuses services/stt/client.py.
"""

from __future__ import annotations

import sys

from mcp.server.fastmcp import FastMCP

from mcp_servers.tool_errors import guarded_tool
from services.stt.client import transcribe as stt_transcribe

mcp = FastMCP("stt", log_level="WARNING")


def _log(line: str) -> None:
    # stdio transport reserves stdout for JSON-RPC -- app logs must go to stderr
    print(line, file=sys.stderr, flush=True)


@mcp.tool()
@guarded_tool(_log, "transcribe_audio")
def transcribe_audio(audio_path: str) -> str:
    """Transcribe a local audio file to Traditional Chinese text.

    On success returns the raw transcript text (llm/stt_agent.py captures it
    directly, unparaphrased) -- guarded_tool only touches the except path,
    so this success shape is untouched by the error-handling wrapper.
    """
    _log(f"[stt-mcp] transcribing {audio_path}")
    return stt_transcribe(audio_path)


if __name__ == "__main__":
    _log("[stt-mcp] server starting")
    mcp.run()
