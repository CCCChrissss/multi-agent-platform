"""Manual smoke test for the asyncio.gather() concurrency fixes in
llm/tsmc_judge.py, llm/notify_agent.py, and
orchestrator/memory_writer.py -- run with:
    uv run python gather_concurrency_smoke_test.py

No live services needed (no Ollama/LiteLLM/Postgres): every independent
call each function gathers is replaced with an asyncio.sleep() stub, so
this only proves two things per site -- (a) the calls actually run
concurrently (elapsed time ~= one delay, not the sum of all of them) and
(b) each stub's result still lands in the right place after the gather,
i.e. the refactor from sequential awaits didn't reorder anything.
"""

from __future__ import annotations

import asyncio
import json
import time
import types
from unittest.mock import AsyncMock, patch

DELAY = 0.2


async def _slow(value):
    await asyncio.sleep(DELAY)
    return value


def _message(*, content: str | None = None, tool_calls=None) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=content, tool_calls=tool_calls))]
    )


def _tool_call(call_id: str, name: str, arguments: dict) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        type="function", id=call_id, function=types.SimpleNamespace(name=name, arguments=json.dumps(arguments))
    )


async def scenario_tsmc_judge_gather() -> None:
    import llm.tsmc_judge as m

    async def _tools():
        return await _slow([{"function": {"name": m._LOOKUP_TOOL}}, {"function": {"name": "other_tool"}}])

    gateway = AsyncMock()
    gateway.list_openai_tools = _tools

    with (
        patch.object(m, "_lookup_tsmc_aliases", lambda *a, **k: _slow(["台積電"])),
        patch.object(m, "inject_procedural", lambda *a, **k: _slow("system prompt")),
        # The tool-calling loop itself now lives in harness/agent_loop.py
        # (run_tool_calling_loop), so that's where chat_with_tools is
        # actually called from -- not llm.tsmc_judge anymore.
        patch("harness.agent_loop.chat_with_tools", lambda *a, **k: _message(content='{"mentions_tsmc": true}')),
    ):
        start = time.monotonic()
        result = await m.mentions_tsmc(
            gateway, "台積電本季營收創新高", store=None, memory_policy=None, tenant="default"
        )
        elapsed = time.monotonic() - start

    assert result is True, result
    assert elapsed < DELAY * 2, f"expected ~{DELAY}s (concurrent), took {elapsed:.2f}s -- looks sequential"
    print(f"[tsmc_judge_gather] OK -- 3 independent calls ran concurrently ({elapsed:.2f}s), verdict={result}")


async def scenario_notified_agent_gather() -> None:
    import llm.notify_agent as m

    async def _tools():
        return await _slow([{"function": {"name": "send_gmail_message"}}])

    gateway = AsyncMock()
    gateway.list_openai_tools = _tools
    gateway.call_tool = AsyncMock(return_value=("sent", False))

    responses = [
        _message(
            tool_calls=[
                _tool_call(
                    "notify-1",
                    "send_gmail_message",
                    {"subject": "台積電通知", "body": "台積電本季營收創新高"},
                )
            ]
        ),
        _message(content="notification sent"),
    ]

    def fake_chat_with_tools(*args, **kwargs):
        return responses.pop(0)

    with (
        patch.object(m, "inject_procedural", lambda *a, **k: _slow("system prompt")),
        # See scenario_tsmc_judge_gather()'s comment -- same reason.
        patch("harness.agent_loop.chat_with_tools", fake_chat_with_tools),
    ):
        start = time.monotonic()
        log = await m.decide_and_notify(
            gateway,
            should_notify=True,
            subject="台積電通知",
            body="台積電本季營收創新高",
            recipient_id="default",
            model="local-qwen",
            system_prompt="system prompt",
            user_prompt="user prompt",
            store=None,
            memory_policy=None,
            tenant="default",
        )
        elapsed = time.monotonic() - start

    assert not responses, "not all mocked turns were consumed"
    assert len(log) == 2, log
    assert "send_gmail_message" in log[0] and "[ERROR]" not in log[0], log
    assert log[1] == "notification sent", log
    gateway.call_tool.assert_awaited_once()
    assert elapsed < DELAY * 2, f"expected ~{DELAY}s (concurrent), took {elapsed:.2f}s -- looks sequential"
    print(f"[notified_agent_gather] OK -- prompt recall + tool list ran concurrently ({elapsed:.2f}s)")


async def scenario_memory_writer_gather() -> None:
    from orchestrator.memory_writer import _distill
    from orchestrator.workflow_def import MemoryWriteRule, StepDef, WorkflowDef

    step = StepDef(
        name="check",
        command_type="check.run",
        completion_type="check.completed",
        input_schema={},
        output_schema={},
        model="local-qwen",
        memory_write=(
            MemoryWriteRule(tenant="default", input_field="transcript", output_fields=("mentions_tsmc",)),
            MemoryWriteRule(tenant="default", input_field="transcript", output_fields=("mentions_tsmc",)),
        ),
    )
    workflow_def = WorkflowDef(name="smoke_wf", steps=(step,))

    calls: list[tuple] = []

    async def fake_remember(store, policy, kind, *, tenant, scope, key, content, **kw):
        await asyncio.sleep(DELAY)
        calls.append((tenant, scope, key, content))

    # _apply_rule() dedup-checks via store.aget() before writing (redelivery
    # guard) -- stub it to "no existing record" so this stays a pure-mock
    # scenario with no real store.
    store = AsyncMock()
    store.aget.return_value = None

    completion = types.SimpleNamespace(
        event_type="check.completed",
        payload={"status": "ok", "output": {"mentions_tsmc": True}},
        thread_id="smoke-thread",
        event_id="smoke-event-1",
    )

    with (
        patch("orchestrator.memory_writer.remember", fake_remember),
        patch(
            "orchestrator.memory_writer.run_state.get_run",
            AsyncMock(return_value={"state_payload": {"transcript": "台積電..."}}),
        ),
    ):
        start = time.monotonic()
        await _distill(workflow_def, store=store, memory_policy=None, completion=completion)
        elapsed = time.monotonic() - start

    assert len(calls) == 2, calls
    assert elapsed < DELAY * 2, f"expected ~{DELAY}s (concurrent), took {elapsed:.2f}s -- looks sequential"
    print(f"[memory_writer_gather] OK -- {len(calls)} memory_write rules applied concurrently ({elapsed:.2f}s)")


async def main() -> None:
    await scenario_tsmc_judge_gather()
    await scenario_notified_agent_gather()
    await scenario_memory_writer_gather()
    print("\nAll gather-concurrency smoke tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
