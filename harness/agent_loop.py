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
    AgentLoopIncomplete on stall or max-turns exhaustion."""
    guard = stall_guard or StallGuard(consecutive_limit=2)
    for _ in range(max_turns):
        response = chat_with_tools(model, messages, tools)
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
