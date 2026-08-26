"""Asyncio compatibility required by Psycopg on Windows.

Psycopg's async implementation needs a selector-based event loop. Python
3.11 defaults to ``ProactorEventLoop`` on Windows, so executable modules
that import an async Postgres backend must select the compatible policy
before they create their event loop.
"""

from __future__ import annotations

import asyncio
import sys

_configured = False


def configure_asyncio_for_psycopg() -> None:
    """Use Windows' selector policy; remain a no-op on other platforms."""
    global _configured
    if _configured or sys.platform != "win32":
        return

    selector_policy_type = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
    if selector_policy_type is None:
        return

    if not isinstance(asyncio.get_event_loop_policy(), selector_policy_type):
        asyncio.set_event_loop_policy(selector_policy_type())
    _configured = True
