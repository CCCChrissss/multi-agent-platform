"""Compat shim for workflows/simple_pipeline.py (frozen -- see
workflows/parity_check.py's `_assert_simple_pipeline_untouched()`), which
still calls the old scenario-coupled `decide_and_notify(gateway, transcript,
mentions_tsmc)` contract positionally. New callers should use
llm/notify_agent.py's scenario-agnostic decide_and_notify() directly --
see docs/exclusion-scenario-plan.md P0.

This reproduces the old TSMC-mention notification rule inline: it used to
live in decide_and_notify()'s own system prompt/args, which coupled the
platform's one generic notify agent to one scenario. Moving the rule out of
that module (the whole point of P0) still leaves this one frozen caller
needing it somewhere -- so it lives here now, scoped to exactly the caller
that can't be changed.
"""

from __future__ import annotations

from typing import Any

from llm.notify_agent import decide_and_notify as _decide_and_notify
from persistence.memory_policy import MemoryPolicy

TSMC_NOTIFICATION_SUBJECT = "偵測到台積電相關內容"
"""The one TSMC-scenario subject line, shared with
workflows/event_driven_pipeline.py's check_handler -- both produce the same
should_notify=True path's subject for the same scenario, just from two
different orchestration modes (in-process vs event-driven), so it's one
constant, not two copies that could drift apart."""


async def decide_and_notify(
    gateway,
    transcript: str,
    mentions_tsmc: bool,
    *,
    store: Any | None = None,
    memory_policy: MemoryPolicy | None = None,
    tenant: str = "default",
    recipient_id: str = "default",
) -> list[str]:
    return await _decide_and_notify(
        gateway,
        should_notify=mentions_tsmc,
        subject=TSMC_NOTIFICATION_SUBJECT if mentions_tsmc else "",
        body=transcript,
        recipient_id=recipient_id,
        store=store,
        memory_policy=memory_policy,
        tenant=tenant,
    )
