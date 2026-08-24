"""MCP server exposing an audio format-check tool.

Lets an agent (e.g. the STT agent) decide whether to validate an audio file
before spending time/money transcribing it. No backing service -- the check
is cheap enough to run inline, same as mcp_servers/lookup/server.py's fake
profile lookup.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from mcp_servers.tool_errors import guarded_tool

mcp = FastMCP("format_check", log_level="WARNING")

_SUPPORTED_EXTENSIONS = {".wav", ".mp3", ".m4a", ".flac"}


def _log(line: str) -> None:
    # stdio transport reserves stdout for JSON-RPC -- app logs must go to stderr
    print(line, file=sys.stderr, flush=True)


def _check(audio_path: str) -> dict:
    path = Path(audio_path)
    if not path.exists():
        return {"valid": False, "reason": f"file not found: {audio_path}"}
    if path.suffix.lower() not in _SUPPORTED_EXTENSIONS:
        return {"valid": False, "reason": f"unsupported format: {path.suffix or '(no extension)'}"}
    return {"valid": True, "reason": None}


@mcp.tool()
@guarded_tool(_log, "check_audio_format")
def check_audio_format(audio_path: str) -> str:
    """Check whether a local audio file exists and is in a supported format (wav/mp3/m4a/flac).

    Returns {"valid": false, "reason": ...} for an unsupported/missing file --
    that's this tool's normal answer, not a failure, so it's a return value
    rather than a raised error (is_error stays False).
    """
    result = _check(audio_path)
    _log(f"[format-check-mcp] checked '{audio_path}' -> valid={result['valid']}")
    return json.dumps(result, ensure_ascii=False)


if __name__ == "__main__":
    _log("[format-check-mcp] server starting")
    mcp.run()
