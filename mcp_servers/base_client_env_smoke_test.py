"""Dependency-free checks for the MCP stdio child environment boundary."""

from __future__ import annotations

from unittest.mock import patch

from mcp import StdioServerParameters

from mcp_servers.base_client import _with_safe_project_env


def main() -> None:
    parent_env = {
        "UV_CACHE_DIR": r"D:\Projects\multi-agent平台架設\.uv-cache",
        "PYTHONUTF8": "1",
        "ANTHROPIC_API_KEY": "must-not-leak",
        "GEMINI_API_KEY": "must-not-leak",
        "PERSISTENCE_DATABASE_URL": "must-not-leak",
    }
    with patch.dict("os.environ", parent_env, clear=True):
        params = StdioServerParameters(
            command="uv",
            args=["run", "python", "-m", "mcp_servers.stt.server"],
            env={"MCP_CALLING_PRINCIPAL": "stt"},
        )
        result = _with_safe_project_env(params)

    assert result.env == {
        "UV_CACHE_DIR": parent_env["UV_CACHE_DIR"],
        "PYTHONUTF8": "1",
        "MCP_CALLING_PRINCIPAL": "stt",
    }
    for secret_name in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "PERSISTENCE_DATABASE_URL"):
        assert secret_name not in result.env

    with patch.dict("os.environ", {"UV_CACHE_DIR": "parent-cache"}, clear=True):
        explicit = _with_safe_project_env(
            StdioServerParameters(command="uv", env={"UV_CACHE_DIR": "explicit-cache"})
        )
    assert explicit.env == {"UV_CACHE_DIR": "explicit-cache"}

    print("base client env smoke: OK")


if __name__ == "__main__":
    main()
