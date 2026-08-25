"""Manual smoke test for llm/stt_agent.py's `from: tool` capture
(docs/generic-agent-runtime-plan.md P2) -- run with:
    uv run python -m llm.stt_agent_smoke_test

No live services needed -- gateway and chat_with_tools are mocked. Covers
the module docstring's original design intent: the model's own final reply
is a "done" status message and is otherwise ignored, so even if it
paraphrases/truncates the transcript there, the returned value must still be
exactly what transcribe_audio returned.
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


async def scenario_transcript_survives_model_paraphrase() -> None:
    import llm.stt_agent as m

    real_transcript = "客戶說要投保長照險，並詢問除外責任範圍。"

    gateway = AsyncMock()
    gateway.list_openai_tools = AsyncMock(
        return_value=[{"function": {"name": "format__check_audio_format"}}, {"function": {"name": "stt__transcribe_audio"}}]
    )

    async def fake_call_tool(name, arguments, call_id):
        if name == "format__check_audio_format":
            return "ok", False
        assert name == "stt__transcribe_audio"
        return real_transcript, False

    gateway.call_tool = fake_call_tool

    responses = [
        _message(tool_calls=[_tool_call("c1", "format__check_audio_format", {"path": "a.wav"})]),
        _message(tool_calls=[_tool_call("c2", "stt__transcribe_audio", {"path": "a.wav"})]),
        # Final turn: no tool call, and the model paraphrases a *different*
        # string here -- must be ignored entirely.
        _message(content="轉錄完成，客戶似乎想投保。"),
    ]

    def fake_chat_with_tools(model, messages, tools):
        return responses.pop(0)

    with patch.object(m, "chat_with_tools", fake_chat_with_tools):
        result = await m.transcribe(
            gateway,
            "a.wav",
            system_prompt="system",
            user_prompt="user",
            store=None,
            memory_policy=None,
            tenant="default",
        )

    assert result == real_transcript, result
    assert not responses, "not all mocked turns were consumed"
    print("[transcript_survives_paraphrase] OK -- returned value came from the tool result, not the model's final reply")


async def main() -> None:
    await scenario_transcript_survives_model_paraphrase()
    print("\nAll stt_agent smoke tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
