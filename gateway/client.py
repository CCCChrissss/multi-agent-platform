"""Thin client for the LiteLLM gateway (see gateway/config.yaml).

Callers use OpenAI-style model names/messages and never touch the underlying
provider (Ollama today, swappable later by editing the gateway config only).
"""

from __future__ import annotations

import json
import time

from openai import AsyncOpenAI, OpenAI

from persistence.call_log import log_call, log_call_sync

BASE_URL = "http://localhost:4000"
API_KEY = "sk-local"  # gateway doesn't enforce auth by default; placeholder to satisfy the client

_client = OpenAI(base_url=BASE_URL, api_key=API_KEY)
_aclient = AsyncOpenAI(base_url=BASE_URL, api_key=API_KEY)


def chat_json(model: str, system_prompt: str, user_content: str) -> dict:
    request = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
    }
    start = time.monotonic()
    try:
        response = _client.chat.completions.create(
            model=model,
            messages=request["messages"],
            response_format={"type": "json_object"},
        )
        result = json.loads(response.choices[0].message.content)
    except Exception as exc:
        log_call_sync("llm", model, request, {"error": str(exc)}, True, int((time.monotonic() - start) * 1000))
        raise
    log_call_sync(
        "llm",
        model,
        request,
        result,
        False,
        int((time.monotonic() - start) * 1000),
        response_model=response.model,
    )
    return result


async def aembed(model: str, texts: list[str]) -> list[list[float]]:
    """Embedding call for persistence/memory_store.py's IndexConfig
    (langgraph.store.base.AEmbeddingsFunc) -- AsyncPostgresStore's
    store.asearch()/aput() need the async path to avoid blocking the event
    loop. Logs input count/char count/dims only -- never the vectors
    themselves, which would blow up call_log's JSONB
    (docs/long-term-memory-plan.md §3.5)."""
    request = {"count": len(texts), "chars": sum(len(t) for t in texts)}
    start = time.monotonic()
    try:
        response = await _aclient.embeddings.create(model=model, input=texts, encoding_format="float")
        result = [item.embedding for item in response.data]
    except Exception as exc:
        await log_call("llm", model, request, {"error": str(exc)}, True, int((time.monotonic() - start) * 1000))
        raise
    await log_call(
        "llm", model, request, {"dims": len(result[0]) if result else 0}, False, int((time.monotonic() - start) * 1000)
    )
    return result


def transcribe(model: str, audio_path: str) -> str:
    request = {"audio_path": audio_path}
    start = time.monotonic()
    try:
        with open(audio_path, "rb") as f:
            response = _client.audio.transcriptions.create(model=model, file=f)
        result = response.text
    except Exception as exc:
        log_call_sync("llm", model, request, {"error": str(exc)}, True, int((time.monotonic() - start) * 1000))
        raise
    log_call_sync("llm", model, request, {"text": result}, False, int((time.monotonic() - start) * 1000))
    return result


def chat_with_tools(model: str, messages: list[dict], tools: list[dict]):
    """Takes/returns a raw messages list so callers can run a multi-turn tool-calling loop."""
    """Expects response: {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "...",
                    "tool_calls": [
                        {
                            "id": "...",
                            "type": "function",
                            "function": {
                                "name": "...",
                                "arguments": {...}
                            }
                        }"""
    start = time.monotonic()
    try:
        response = _client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
        )
    except Exception as exc:
        log_call_sync(
            "llm", model, {"messages": messages}, {"error": str(exc)}, True, int((time.monotonic() - start) * 1000)
        )
        raise
    log_call_sync(
        "llm",
        model,
        {"messages": messages},
        response.choices[0].message.model_dump(),
        False,
        int((time.monotonic() - start) * 1000),
        response_model=response.model,
    )
    return response
