"""Thin HTTP client for the notified service (see services/notified/server.py).

Plays the same role for notifications that services/stt/client.py plays for
STT: callers get a result back without knowing how or where the send
actually happens.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from services.errors import ToolDependencyError

BASE_URL = "http://localhost:8002"


def _post(path: str, payload: dict) -> str:
    try:
        response = httpx.post(f"{BASE_URL}{path}", json=payload, timeout=30)
        response.raise_for_status()
    except httpx.ConnectError as exc:
        raise ToolDependencyError(
            f"無法連線到通知服務 ({BASE_URL})，服務可能沒啟動——"
            "確認 `uv run uvicorn services.notified.server:app --port 8002` 是否在跑"
        ) from exc
    except httpx.TimeoutException as exc:
        raise ToolDependencyError(f"通知服務逾時（>30s）：{BASE_URL}{path}") from exc
    except httpx.HTTPStatusError as exc:
        raise ToolDependencyError(
            f"通知服務回傳 {exc.response.status_code}：{exc.response.text}"
        ) from exc
    return response.json()["result"]


def send_slack_message(channel: str, message: str) -> str:
    result = _post("/slack", {"channel": channel, "message": message})
    return f"{result} at {datetime.now(UTC).isoformat()}"


def send_gmail_message(to: str, subject: str, body: str) -> str:
    result = _post("/gmail", {"to": to, "subject": subject, "body": body})
    return f"{result} at {datetime.now(UTC).isoformat()}"
