"""Manual smoke test for llm/tsmc_judge.py's `from: model` structured output
(docs/generic-agent-runtime-plan.md P2) -- run with:
    uv run python -m llm.tsmc_judge_smoke_test

No live services needed -- gateway and chat_with_tools are mocked.
"""

from __future__ import annotations

import asyncio
import types
from unittest.mock import AsyncMock, patch


def _message(*, content: str | None = None, tool_calls=None) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=content, tool_calls=tool_calls))]
    )


async def _const(value):
    return value


async def scenario_response_format_threaded_and_parsed() -> None:
    """chat_with_tools() must receive _VERDICT_SCHEMA on the main loop call,
    and _parse_verdict() must read straight off the schema-conforming content
    with no fallback parsing needed."""
    import llm.tsmc_judge as m

    gateway = AsyncMock()
    gateway.list_openai_tools = AsyncMock(return_value=[{"function": {"name": m._LOOKUP_TOOL}}])
    gateway.call_tool = AsyncMock(return_value=('{"official_name": "台積電", "aliases": ["TSMC"]}', False))

    seen_response_formats: list[dict | None] = []

    def fake_chat_with_tools(model, messages, tools, response_format=None):
        seen_response_formats.append(response_format)
        return _message(content='{"mentions_tsmc": true}')

    with (
        patch.object(m, "inject_procedural", lambda *a, **k: _const("system prompt")),
        patch("harness.agent_loop.chat_with_tools", fake_chat_with_tools),
    ):
        result = await m.mentions_tsmc(gateway, "台積電今天股價上漲", store=None, memory_policy=None, tenant="default")

    assert result is True, result
    assert seen_response_formats == [m._VERDICT_SCHEMA], seen_response_formats
    print("[response_format_threaded] OK -- main loop call carried _VERDICT_SCHEMA and content parsed cleanly")


async def scenario_alias_conflict_retry_also_carries_schema() -> None:
    """Backstop finds a known alias but the model says false -- the retry
    turn must carry _VERDICT_SCHEMA too, not just the first call."""
    import llm.tsmc_judge as m

    gateway = AsyncMock()
    gateway.list_openai_tools = AsyncMock(return_value=[{"function": {"name": m._LOOKUP_TOOL}}])
    gateway.call_tool = AsyncMock(return_value=('{"official_name": "台積電", "aliases": ["TSMC"]}', False))

    responses = [
        _message(content='{"mentions_tsmc": false}'),
        _message(content='{"mentions_tsmc": true}'),
    ]
    seen_response_formats: list[dict | None] = []

    def fake_chat_with_tools(model, messages, tools, response_format=None):
        seen_response_formats.append(response_format)
        return responses.pop(0)

    with (
        patch.object(m, "inject_procedural", lambda *a, **k: _const("system prompt")),
        patch("harness.agent_loop.chat_with_tools", fake_chat_with_tools),
    ):
        result = await m.mentions_tsmc(gateway, "TSMC 今天股價上漲", store=None, memory_policy=None, tenant="default")

    assert result is True, result
    assert not responses, "not all mocked turns were consumed"
    assert seen_response_formats == [m._VERDICT_SCHEMA, m._VERDICT_SCHEMA], seen_response_formats
    print("[alias_conflict_retry_schema] OK -- both the first call and the conflict-retry call carried _VERDICT_SCHEMA")


async def scenario_parse_verdict_handles_markdown_fence() -> None:
    """See llm/exclusion_judge_smoke_test.py's equivalent scenario and
    harness.agent_loop.parse_structured_json()'s docstring for why this
    defensive strip is kept."""
    import llm.tsmc_judge as m

    content = '```json\n{"mentions_tsmc": true}\n```'
    assert m._parse_verdict(content) is True
    print("[parse_verdict_markdown_fence] OK -- a ```json fence around otherwise-valid content no longer breaks parsing")


async def main() -> None:
    await scenario_response_format_threaded_and_parsed()
    await scenario_alias_conflict_retry_also_carries_schema()
    await scenario_parse_verdict_handles_markdown_fence()
    print("\nAll tsmc_judge smoke tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
