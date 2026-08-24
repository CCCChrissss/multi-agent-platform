"""Shared error-handling for @mcp.tool() functions.

Every mcp_servers/*/server.py tool should wrap its body with @guarded_tool
so an unclassified exception never leaks a raw traceback to the agent as
FastMCP's default "Error executing tool X: {e}" text. Re-exports the
services/ exception vocabulary so server.py modules only need one import.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Awaitable, Callable
from typing import TypeVar

from services.errors import ToolDependencyError, ToolInputError

__all__ = ["ToolInputError", "ToolDependencyError", "guarded_tool"]

_F = TypeVar("_F", bound=Callable[..., str] | Callable[..., Awaitable[str]])


def guarded_tool(log: Callable[[str], None], tool_name: str) -> Callable[[_F], _F]:
    """Only touches the except path -- the success path returns untouched.

    ToolInputError/ToolDependencyError are re-raised as-is: their message is
    already written for the agent (see services/errors.py). Anything else is
    logged to stderr for debugging and re-raised as a ToolDependencyError
    with a generic but honest message instead of exposing implementation
    details (stack traces, library-internal error text) to the agent.

    Wraps both sync and async tool functions -- mcp_servers/memory/server.py
    is the first tool that needs to `await recall()`, and a sync wrapper
    around an async func would call it without awaiting, silently returning
    an un-awaited coroutine instead of running the try/except around it.
    """

    def decorator(func: _F) -> _F:
        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: object, **kwargs: object) -> str:
                try:
                    return await func(*args, **kwargs)
                except (ToolInputError, ToolDependencyError):
                    raise
                except Exception as exc:
                    log(f"[{tool_name}] unhandled {type(exc).__name__}: {exc}")
                    raise ToolDependencyError(
                        f"{tool_name} failed unexpectedly ({type(exc).__name__}). "
                        "This isn't a recognized input or dependency error -- check server logs; "
                        "retrying the same call is unlikely to help without a code fix."
                    ) from exc

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def wrapper(*args: object, **kwargs: object) -> str:
            try:
                return func(*args, **kwargs)
            except (ToolInputError, ToolDependencyError):
                raise
            except Exception as exc:
                log(f"[{tool_name}] unhandled {type(exc).__name__}: {exc}")
                raise ToolDependencyError(
                    f"{tool_name} failed unexpectedly ({type(exc).__name__}). "
                    "This isn't a recognized input or dependency error -- check server logs; "
                    "retrying the same call is unlikely to help without a code fix."
                ) from exc

        return wrapper  # type: ignore[return-value]

    return decorator
