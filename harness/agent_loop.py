"""Shared signal for tool-calling agent loops that couldn't reach a
verified, confident conclusion within their turn/verification budget.

Different semantic layer from services/errors.py's ToolInputError/
ToolDependencyError, which classify why a single tool call failed. A loop
can raise this even when every individual tool call inside it succeeded
(e.g. llm/notify_agent.py's _finish(): the model claims done but a post-hoc
check finds the claimed outcome wasn't actually achieved).

Callers are LangGraph node functions (workflows/simple_pipeline.py): catch
this, record needs_review=True + the reason in PipelineState, and let the
graph run to completion instead of crashing the whole workflow run.
"""

from __future__ import annotations

import functools
import json
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from gateway.client import chat_with_tools


class AgentLoopIncomplete(RuntimeError):
    def __init__(self, node: str, reason: str) -> None:
        self.node = node
        self.reason = reason
        super().__init__(f"[{node}] needs review: {reason}")


class StallGuard:
    """Detects a tool-calling loop that keeps repeating the same
    non-productive step instead of advancing, independent of the raw
    turn-count ceiling (MAX_TURNS's job is only to cap total cost/latency,
    not to tell "still working" apart from "stuck").

    Callers feed it one hashable "signature" per turn -- whatever that
    particular loop considers "didn't move": e.g. a constant like
    "no_tool_call" for a loop where only tool calls advance the task, or
    the exact (tool_name, arguments) pairs called for a loop where calling
    the same thing again is the failure mode to catch. A different
    signature than last turn resets the streak (counts as progress), even
    if the model's wording changed but the underlying action didn't.
    """

    def __init__(self, consecutive_limit: int = 2) -> None:
        self._consecutive_limit = consecutive_limit
        self._last_signature: object = None
        self._streak = 0

    def observe(self, signature: object) -> bool:
        """Record this turn's signature. Returns True once the same
        signature has repeated `consecutive_limit` times in a row."""
        if signature == self._last_signature:
            self._streak += 1
        else:
            self._last_signature = signature
            self._streak = 1
        return self._streak >= self._consecutive_limit

    @property
    def streak(self) -> int:
        return self._streak


async def run_tool_calling_loop(
    model: str,
    messages: list[dict],
    tools: list[dict],
    gateway: Any,
    *,
    node: str,
    max_turns: int,
    stall_guard: StallGuard | None = None,
    on_tool_result: Callable[[Any, dict, str, bool], None] | None = None,
    on_turn_end: Callable[[], None] | None = None,
    raise_on_max_turns: bool = True,
    response_format: dict | None = None,
) -> Any | None:
    """The mechanical shell every llm/*.py tool-calling loop repeated
    verbatim: call the model, stop once it replies with no tool calls,
    otherwise run each call through `gateway` and feed the results back --
    up to `max_turns`, bailing out via StallGuard if the same call repeats
    `consecutive_limit` times in a row.

    Appends to `messages` in place, so a caller that wants a follow-up turn
    (e.g. a citation-conflict retry) can call this again against the same
    list. `on_tool_result(call, arguments, result_text, is_error)` and
    `on_turn_end()` are the two extension points callers actually needed
    (tracking a browse map, logging sends, injecting a per-turn summary) --
    everything else about the shape was identical everywhere it was copied.

    Returns the model's final no-tool-call message, or `None` if
    `raise_on_max_turns=False` and the turn budget ran out first (the
    caller decides what "gave up" means for it); otherwise raises
    AgentLoopIncomplete on stall or max-turns exhaustion.

    `response_format` (docs/generic-agent-runtime-plan.md P2's `from: model`)
    is forwarded to every turn's chat_with_tools() call, tool-calling turns
    included -- verified (scripts/check_structured_output_support.py) that a
    model still calls a tool when it wants to even with a response_format
    set; the schema only constrains the turn where it replies with no tool
    call, which is the only turn whose content a caller ever parses."""
    guard = stall_guard or StallGuard(consecutive_limit=2)
    for _ in range(max_turns):
        response = chat_with_tools(model, messages, tools, response_format=response_format)
        assistant_message = response.choices[0].message
        # we only ever register type="function" tools, so ignore any other tool-call kind
        tool_calls = [c for c in (assistant_message.tool_calls or []) if c.type == "function"]

        if not tool_calls:
            return assistant_message

        signature = tuple((c.function.name, c.function.arguments) for c in tool_calls)
        if guard.observe(signature):
            raise AgentLoopIncomplete(
                node=node,
                reason=(
                    f"model repeated the same tool call {guard.streak} turns in a row "
                    f"without reaching a final answer: {signature!r}"
                ),
            )

        messages.append(
            {
                "role": "assistant",
                "content": assistant_message.content,
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.function.name, "arguments": call.function.arguments},
                    }
                    for call in tool_calls
                ],
            }
        )
        for call in tool_calls:
            arguments = json.loads(call.function.arguments)
            result_text, is_error = await gateway.call_tool(call.function.name, arguments, call.id)
            if on_tool_result:
                on_tool_result(call, arguments, result_text, is_error)
            content = f"[ERROR] {result_text}" if is_error else result_text
            messages.append({"role": "tool", "tool_call_id": call.id, "content": content})

        if on_turn_end:
            on_turn_end()

    if raise_on_max_turns:
        raise AgentLoopIncomplete(node=node, reason=f"{node}: reached max tool-calling turns without a final answer")
    return None


def parse_structured_json(content: str) -> Any:
    """json.loads() for a `response_format=json_schema` reply
    (docs/generic-agent-runtime-plan.md P2's `from: model`), tolerant of a
    ```/```json markdown fence around otherwise schema-conforming content.

    Written after hitting exactly this under concurrent load against
    gemini-strong (six concurrent llm/exclusion_judge.py::judge_exclusion()
    calls) -- though that run turned out to be while gateway/client.py's
    chat_with_tools() had a since-fixed bug silently dropping
    response_format entirely, so it isn't confirmed evidence this still
    happens under genuine schema enforcement. Kept anyway: a stray fence
    around otherwise-valid JSON is cheap to tolerate regardless of cause,
    and a bare json.loads() raises the exact same JSONDecodeError on it as
    on truly malformed content, for turns that were otherwise perfectly
    parseable.

    2026-08-17: claude-haiku (docs/generic-agent-runtime-plan.md's Gemini ->
    Anthropic model swap) confirmed a second failure shape a fence-strip
    alone doesn't cover -- scripts/check_structured_output_support.py's
    tools=yes case came back as free-text reasoning *followed by* the JSON
    object (`"我理解您提供的信息是...\n\n{\"mentions_tsmc\": true}"`), not
    wrapped in a fence at all. Anthropic has no native equivalent of OpenAI's
    json_schema response_format, so `strict: True` evidently isn't a hard
    constraint on the whole reply the way it is for Gemini/local-qwen (which
    reliably returned bare JSON in the same check). Falls back to
    `raw_decode()` from the first `{` when a plain parse fails -- the same
    technique llm/exclusion_judge.py's pre-P2 _parse_verdict() used before
    structured output existed, revived because Anthropic turned out to need
    it after all."""
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = stripped.removeprefix("```json").removeprefix("```").strip()
        stripped = stripped.removesuffix("```").strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        # The model's real answer is always the trailing content (see the
        # claude-haiku example above), so scan '{' positions right-to-left and
        # take the first one that decodes cleanly through to the exact end of
        # the string -- taking the *first* '{' in the string instead would grab
        # an earlier brace from the prose itself (e.g. the model quoting the
        # schema before answering) and silently return the wrong object.
        starts = [i for i, ch in enumerate(stripped) if ch == "{"]
        for start in reversed(starts):
            try:
                obj, end = json.JSONDecoder().raw_decode(stripped[start:])
            except json.JSONDecodeError:
                continue
            if start + end == len(stripped):
                return obj
        raise


T = TypeVar("T")


def wrap_agent_exception(node: str) -> Callable:
    """Decorator for agent loop entry points: catch unexpected exceptions and
    convert them to AgentLoopIncomplete so callers don't need to distinguish
    between "loop decided it couldn't reach a confident answer" vs "something
    broke internally". Re-raises AgentLoopIncomplete as-is.

    Usage:
        @wrap_agent_exception("stt")
        async def transcribe(gateway, audio_path):
            ...
    """

    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(func)
        async def wrapper(*args: object, **kwargs: object) -> T:
            try:
                return await func(*args, **kwargs)
            except AgentLoopIncomplete:
                raise
            except Exception as exc:
                raise AgentLoopIncomplete(node=node, reason=f"unexpected error: {exc!r}") from exc

        return wrapper

    return decorator
