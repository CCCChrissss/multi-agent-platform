"""Manual smoke test for llm/exclusion_judge.py's citation-conflict retry --
run with:
    uv run python -m llm.exclusion_judge_smoke_test

No live services needed -- gateway and chat_with_tools are mocked. Regression
test for the bug fixed alongside harness/agent_loop.py's run_tool_calling_loop
extraction: when the retry prompt tells the model to re-browse before
re-citing and the model actually does that, the tool call must run and
update `explored`, or the retry can never succeed. The old code checked
`if not retry_message.tool_calls` before re-parsing the verdict, so a model
that complied with the retry instruction always lost -- its tool call was
silently dropped and the stale, already-rejected verdict was reused.
"""

from __future__ import annotations

import asyncio
import json
import types
from unittest.mock import AsyncMock, patch


def _message(*, content: str | None = None, tool_calls=None) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=content, tool_calls=tool_calls))]
    )


def _tool_call(call_id: str, name: str, arguments: dict) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        type="function", id=call_id, function=types.SimpleNamespace(name=name, arguments=json.dumps(arguments))
    )


def _browse_result(scope: list[str], items: list[dict]) -> str:
    return json.dumps(
        {"scope": scope, "summary": None, "parent": scope[:-1], "siblings": [], "children": [], "items": items, "truncated": False}
    )


async def _const(value):
    return value


async def scenario_citation_retry_recovers_via_rebrowse() -> None:
    import llm.exclusion_judge as m

    gateway = AsyncMock()
    gateway.list_openai_tools = AsyncMock(return_value=[{"function": {"name": m._BROWSE_TOOL}}])

    async def fake_call_tool(name, arguments, call_id):
        assert name == m._BROWSE_TOOL
        if arguments["scope"] == ["insurance_product", "kgi_ltc"]:
            items = [{"key": "art1", "article": "第一條", "text": "..."}]  # 第二條 not seen yet
        else:
            items = [{"key": "art2", "article": "第二條", "text": "..."}]  # only the retry's re-browse reveals it
        return _browse_result(arguments["scope"], items), False

    gateway.call_tool = fake_call_tool

    responses = [
        # Turn 1: model browses the product root.
        _message(tool_calls=[_tool_call("c1", m._BROWSE_TOOL, {"scope": ["insurance_product", "kgi_ltc"]})]),
        # Turn 2: verdict cites 第二條, which the loop never actually browsed -> triggers the retry.
        _message(content='{"involves_exclusion": true, "matched_articles": ["第二條"], "reason": "ok"}'),
        # Retry turn 1: model does exactly what _CITATION_CONFLICT_PROMPT asks -- re-browses before re-citing.
        _message(tool_calls=[_tool_call("c2", m._BROWSE_TOOL, {"scope": ["insurance_product", "kgi_ltc", "exclusions"]})]),
        # Retry turn 2: cites the same article, now actually seen -- must succeed.
        _message(content='{"involves_exclusion": true, "matched_articles": ["第二條"], "reason": "confirmed"}'),
    ]

    def fake_chat_with_tools(model, messages, tools, response_format=None):
        assert response_format == m._VERDICT_SCHEMA, response_format
        return responses.pop(0)

    with (
        patch.object(m, "inject_procedural", lambda *a, **k: _const("system prompt")),
        patch("harness.agent_loop.chat_with_tools", fake_chat_with_tools),
    ):
        result = await m.judge_exclusion(gateway, "客戶描述的情況...", store=None, memory_policy=None, tenant="default")

    assert result["involves_exclusion"] is True, result
    assert result["matched_articles"] == ["第二條"], result
    assert not responses, "not all mocked turns were consumed"
    print("[citation_retry_recovers] OK -- retry that actually re-browses before re-citing now succeeds")


async def scenario_parse_verdict_handles_brace_in_reason() -> None:
    """docs/generic-agent-runtime-plan.md P2: _VERDICT_SCHEMA (passed as
    response_format) guarantees no prose prefix/suffix any more -- this
    scenario now only needs to check that a literal `{` inside the
    free-text `reason` value (still legal JSON) parses fine under the
    simplified plain json.loads()."""
    import llm.exclusion_judge as m

    content = '{"involves_exclusion": false, "matched_articles": [], "reason": "門檻是 {第二至三級}，未達成"}'
    verdict = m._parse_verdict(content)
    assert verdict["involves_exclusion"] is False, verdict
    assert verdict["reason"] == "門檻是 {第二至三級}，未達成", verdict
    print("[parse_verdict_brace_in_reason] OK -- a literal `{` inside `reason` still parses fine")


async def scenario_parse_verdict_handles_markdown_fence() -> None:
    """_parse_verdict() must tolerate a ```json fence around otherwise valid
    content -- harness.agent_loop.parse_structured_json()'s docstring has
    the full story (surfaced under concurrent load, but confounded by a
    since-fixed gateway/client.py bug; kept as a cheap defensive strip
    regardless of root cause)."""
    import llm.exclusion_judge as m

    content = '```json\n{"involves_exclusion": false, "matched_articles": ["第二十九條"], "reason": "門檻未達成"}\n```'
    verdict = m._parse_verdict(content)
    assert verdict["involves_exclusion"] is False, verdict
    assert verdict["matched_articles"] == ["第二十九條"], verdict
    print("[parse_verdict_markdown_fence] OK -- a ```json fence around otherwise-valid content no longer breaks parsing")


async def main() -> None:
    await scenario_citation_retry_recovers_via_rebrowse()
    await scenario_parse_verdict_handles_brace_in_reason()
    await scenario_parse_verdict_handles_markdown_fence()
    print("\nAll exclusion_judge smoke tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
