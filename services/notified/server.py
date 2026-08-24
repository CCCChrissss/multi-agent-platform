"""FastAPI service hosting the actual notification-sending logic.

Placeholder/fake for now -- swap the bodies for real Slack/Gmail API calls
once credentials are wired up. Runs as a persistent process so both the MCP
tool wrapper (mcp_servers/notified/server.py) and any future pure-logic
node can call the same implementation without duplicating it, the same way
services/stt backs both stt_node and mcp_servers/stt.
"""

from __future__ import annotations

import sys

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()


def _log(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


class SlackMessage(BaseModel):
    channel: str
    message: str


class GmailMessage(BaseModel):
    to: str
    subject: str
    body: str


@app.post("/slack")
def send_slack_message(payload: SlackMessage) -> dict:
    _log(f"[notified-service][slack] -> {payload.channel}: {payload.message}")
    return {"result": f"slack message sent to {payload.channel}"}


@app.post("/gmail")
def send_gmail_message(payload: GmailMessage) -> dict:
    _log(f"[notified-service][gmail] -> {payload.to} | {payload.subject}: {payload.body}")
    return {"result": f"email sent to {payload.to}"}
