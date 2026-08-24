"""Shared request/response envelope for standalone agent HTTP servers
(docs/agent-api-contract.md) -- every agents/<name>/server.py builds its
FastAPI route around AgentRequest/AgentResponse and run_handler() instead
of each reimplementing the same status-mapping logic.

Deliberately independent of orchestrator/worker.py's parallel _handle_one()
(same three-way status mapping, same schema-validation boundary via
harness/schema_validation.py, different transport) rather than importing
from it: agents/ is meant to be deployable on its own, without pulling in
orchestrator/'s event_bus/run_state dependency chain. `input_schema`/
`output_schema` are plain JSON Schema dicts rather than an
orchestrator.workflow_def.StepDef, for the same reason -- callers
(agents/<name>/server.py) are free to source them from that module's
load_workflow_def() (the single source of truth, see
workflows/definitions/stt_check_notify.yaml) without this module needing to
know that type exists.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Literal

import httpx
from pydantic import BaseModel, Field

from harness.agent_loop import AgentLoopIncomplete
from harness.schema_validation import validate_against_schema
from persistence.call_log import current_node_name, current_thread_id
from services.errors import ToolDependencyError

Handler = Callable[[dict, dict], Awaitable[dict]]


class AgentRequest(BaseModel):
    thread_id: str
    input: dict
    context: dict = Field(default_factory=dict)
    """Cross-run identity dimensions (tenant_id/user_id, ...), separate from
    `input`'s per-run business data -- see docs/long-term-memory-plan.md §3.3
    for why `thread_id` alone can't key long-term memory. Defaults to `{}`
    rather than being required: agents that don't touch memory (stt) never
    need to look at it."""


class AgentResponse(BaseModel):
    status: Literal["ok", "needs_review", "error"]
    output: dict | None = None
    review_reason: str | None = None
    error: str | None = None


async def run_handler(
    node_name: str,
    handler: Handler,
    request: AgentRequest,
    *,
    input_schema: dict | None = None,
    output_schema: dict | None = None,
) -> AgentResponse:
    current_thread_id.set(request.thread_id)
    current_node_name.set(node_name)
    try:
        if input_schema is not None:
            validate_against_schema(node_name, "input", request.input, input_schema)
        result = await handler(request.input, request.context)
        if output_schema is not None:
            # A handler result that doesn't match the declared contract is a
            # contract violation, not a business outcome the agent itself
            # could have resolved -- falls through to "error" below, same
            # policy as orchestrator/worker.py's _handle_one(), not
            # "needs_review" (that's reserved for AgentLoopIncomplete, the
            # agent's own "couldn't reach a confident answer" signal).
            validate_against_schema(node_name, "output", result, output_schema)
        return AgentResponse(status="ok", output=result)
    except AgentLoopIncomplete as exc:
        return AgentResponse(status="needs_review", review_reason=str(exc))
    except Exception as exc:  # noqa: BLE001 -- mirrors orchestrator/worker.py's failure mapping
        return AgentResponse(status="error", error=repr(exc))


async def run_request(
    base_url: str, service_name: str, uvicorn_target: str, input: dict, context: dict | None = None
) -> AgentResponse:
    """Client-side counterpart to run_handler() -- every agents/<name>/client.py's
    _run() POSTs to its server's /run route this same way, so it lives here
    rather than being copy-pasted three times."""
    thread_id = current_thread_id.get()
    if thread_id is None:
        raise RuntimeError(f"agents.{service_name}.client: no current_thread_id set -- must be called from within a worker's command handler")
    # ponytail: tenant isolation deferred -- `context or {...}` also treats an
    # explicit context={} the same as "not provided"; harmless while every
    # caller always passes None, revisit once a real tenant is wired through.
    request = AgentRequest(thread_id=thread_id, input=input, context=context or {"tenant_id": "default"})
    try:
        async with httpx.AsyncClient(timeout=180) as client:
            response = await client.post(f"{base_url}/run", json=request.model_dump())
            response.raise_for_status()
    except httpx.ConnectError as exc:
        raise ToolDependencyError(
            f"無法連線到 {service_name} agent ({base_url})，服務可能沒啟動——"
            f"確認 `uv run uvicorn {uvicorn_target}` 是否在跑"
        ) from exc
    except httpx.TimeoutException as exc:
        raise ToolDependencyError(f"{service_name} agent 逾時（>180s）：{base_url}/run") from exc
    except httpx.HTTPStatusError as exc:
        raise ToolDependencyError(f"{service_name} agent 回傳 {exc.response.status_code}：{exc.response.text}") from exc
    return AgentResponse.model_validate(response.json())
