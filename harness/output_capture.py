"""Reusable `on_tool_result` observers for the two non-model `output.from`
kinds in docs/generic-agent-runtime-plan.md's §6/P2 target shape:

- `from: tool` -- take a field's value straight off a specific tool's
  result, never off what the model says about it (llm/stt_agent.py's
  transcript capture: a long transcript must not be re-typed, paraphrased,
  or truncated by the model relaying it).
- `from: tool_log` -- accumulate a line per tool call this loop made
  (llm/notify_agent.py's `notified_log`).

Both match run_tool_calling_loop()'s `on_tool_result(call, arguments,
result_text, is_error)` callback signature so they plug in directly, but
neither depends on it -- llm/stt_agent.py runs its own hand-rolled loop
(docs/generic-agent-runtime-plan.md §3's noted, deliberately-untouched
inconsistency) and calls `.observe()` inline per tool call instead.
"""

from __future__ import annotations

from typing import Any


class ToolResultCapture:
    """`from: tool` -- the last successful result from a tool whose name
    ends with `tool_name_suffix`. "Last" not "first": a loop may retry the
    same tool after an earlier error, and the retry's result is the one
    that should win."""

    def __init__(self, tool_name_suffix: str) -> None:
        self._suffix = tool_name_suffix
        self.value: str | None = None

    def observe(self, call: Any, arguments: dict, result_text: str, is_error: bool) -> None:
        if not is_error and call.function.name.endswith(self._suffix):
            self.value = result_text


class ToolCallLog:
    """`from: tool_log` -- one formatted line per tool call, in call order.

    `any_succeeded(*name_suffixes)` is the small derived check callers
    building an "did the thing I care about actually happen" gate need
    (llm/notify_agent.py's `should_notify=True but nothing sent` guard) --
    kept here rather than duplicated per caller since it reads the same
    (name, succeeded) pairs `observe()` already records."""

    def __init__(self) -> None:
        self.entries: list[str] = []
        self._calls: list[tuple[str, bool]] = []

    def observe(self, call: Any, arguments: dict, result_text: str, is_error: bool) -> None:
        self.entries.append(f"{call.function.name}({arguments}) -> {result_text}{' [ERROR]' if is_error else ''}")
        self._calls.append((call.function.name, not is_error))

    def any_succeeded(self, *name_suffixes: str) -> bool:
        return any(succeeded and name.endswith(name_suffixes) for name, succeeded in self._calls)
