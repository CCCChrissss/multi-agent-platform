"""Manual smoke test for llm/notify_agent.py's `from: tool_log` accumulation
(docs/generic-agent-runtime-plan.md P2) -- run with:
    uv run python -m llm.notify_agent_smoke_test

No live services needed -- gateway and chat_with_tools are mocked.
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


async def _const(value):
    return value


async def scenario_log_accumulates_every_call_and_gates_on_success() -> None:
    """A failed send followed by a successful one on a different channel --
    notified_log must contain both entries (in order, [ERROR] marked on the
    failed one), and the should_notify=True guard must pass because *some*
    send eventually succeeded."""
    import llm.notify_agent as m

    gateway = AsyncMock()
    gateway.list_openai_tools = AsyncMock(
        return_value=[{"function": {"name": "notified__send_gmail_message"}}, {"function": {"name": "notified__send_slack_message"}}]
    )

    async def fake_call_tool(name, arguments, call_id):
        if name == "notified__send_gmail_message":
            return "smtp timeout", True
        assert name == "notified__send_slack_message"
        return "sent", False

    gateway.call_tool = fake_call_tool

    responses = [
        _message(tool_calls=[_tool_call("c1", "notified__send_gmail_message", {"to": "a"})]),
        _message(tool_calls=[_tool_call("c2", "notified__send_slack_message", {"to": "a"})]),
        _message(content="已透過 Slack 送出通知"),
    ]

    def fake_chat_with_tools(model, messages, tools, response_format=None):
        return responses.pop(0)

    with (
        patch.object(m, "_recall_prompt", lambda *a, **k: _const("system prompt")),
        patch("harness.agent_loop.chat_with_tools", fake_chat_with_tools),
    ):
        log = await m.decide_and_notify(
            gateway,
            should_notify=True,
            subject="主旨",
            body="內容",
            system_prompt="system",
            user_prompt="user",
            store=None,
            memory_policy=None,
            tenant="default",
        )

    assert not responses, "not all mocked turns were consumed"
    assert any("send_gmail_message" in line and "[ERROR]" in line for line in log), log
    assert any("send_slack_message" in line and "[ERROR]" not in line for line in log), log
    print("[log_accumulates_and_gates] OK -- notified_log has both calls; should_notify=True guard passed on the later success")


async def scenario_should_notify_true_with_no_send_raises() -> None:
    import llm.notify_agent as m

    gateway = AsyncMock()
    gateway.list_openai_tools = AsyncMock(return_value=[])
    gateway.call_tool = AsyncMock()

    responses = [_message(content="判斷後決定不用發送")]

    def fake_chat_with_tools(model, messages, tools, response_format=None):
        return responses.pop(0)

    with (
        patch.object(m, "_recall_prompt", lambda *a, **k: _const("system prompt")),
        patch("harness.agent_loop.chat_with_tools", fake_chat_with_tools),
    ):
        try:
            await m.decide_and_notify(
                gateway,
                should_notify=True,
                subject="主旨",
                body="內容",
                system_prompt="system",
                user_prompt="user",
                store=None,
                memory_policy=None,
                tenant="default",
            )
        except m.AgentLoopIncomplete as exc:
            print(f"[should_notify_true_no_send_raises] OK -- {exc}")
            return
    raise AssertionError("expected AgentLoopIncomplete")


async def main() -> None:
    await scenario_log_accumulates_every_call_and_gates_on_success()
    await scenario_should_notify_true_with_no_send_raises()
    print("\nAll notify_agent smoke tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
