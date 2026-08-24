"""MCP server exposing a fake company-profile lookup tool.

Placeholder/fake data for now -- lets an agent (e.g. check_node's TSMC
judge) decide whether to look up a company's aliases and watchlist status
before answering, instead of relying purely on its own knowledge.
"""

from __future__ import annotations

import json
import sys

from mcp.server.fastmcp import FastMCP

from mcp_servers.tool_errors import guarded_tool

mcp = FastMCP("lookup", log_level="WARNING")

_FAKE_PROFILES = [
    {
        "official_name": "台灣積體電路製造股份有限公司",
        "aliases": ["台積電", "TSMC", "台灣積體電路製造"],
        "industry": "半導體晶圓代工",
        "watchlist": True,
        "note": "公司內部列為高敏感度監控對象，提及時須立即通報",
    },
]

_UNKNOWN_PROFILE = {
    "official_name": None,
    "aliases": [],
    "industry": None,
    "watchlist": False,
    "note": "查無資料，非監控清單公司",
}


def _log(line: str) -> None:
    # stdio transport reserves stdout for JSON-RPC -- app logs must go to stderr
    print(line, file=sys.stderr, flush=True)


def _find_profile(company: str) -> dict:
    needle = company.strip().lower()
    if not needle:
        return _UNKNOWN_PROFILE
    for profile in _FAKE_PROFILES:
        names = [profile["official_name"], *profile["aliases"]]
        if any(name and (needle in name.lower() or name.lower() in needle) for name in names):
            return profile
    return _UNKNOWN_PROFILE


@mcp.tool()
@guarded_tool(_log, "query_company_profile")
def query_company_profile(company: str) -> str:
    """Look up a company's official name, aliases, industry, and whether it's on the watchlist."""
    profile = _find_profile(company)
    _log(f"[lookup-mcp] queried '{company}' -> watchlist={profile['watchlist']}")
    return json.dumps(profile, ensure_ascii=False)


if __name__ == "__main__":
    _log("[lookup-mcp] server starting")
    mcp.run()
