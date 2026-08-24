"""Event Bus abstraction: the platform-generic contract every backend must
satisfy.

Any workflow that wants event-driven coordination (Master Agent <-> Worker
Nodes) talks to `EventBus`/`Event`/`Delivery`, never to a backend directly --
swapping backends (see event_bus/factory.py) only ever means adding one new
file under event_bus/, the same pattern persistence/checkpointer.py already
established for the LangGraph checkpointer.

`commands_topic`/`events_topic` are the single place topic names are derived
from a workflow name (and, for commands, the step name) -- callers must go
through these, never hand-write a topic string, so the naming convention
can't drift or leak scenario-specific names (e.g. "audit-*") into platform
code. Commands are namespaced per step (not just per workflow) so each
step's worker subscribes to a topic no other step's worker ever publishes
to -- see event_bus/postgres.py's `_FAN_OUT`, which would otherwise create
a dispatch row for every consumer_group subscribed to a shared topic
regardless of which step the command was actually for.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Literal, Protocol

StartFrom = Literal["beginning", "now"]

# Fixed namespace for deterministic_event_id() below -- arbitrary, just needs
# to stay constant for this app so the same (thread_id, topic, event_type)
# always hashes to the same UUID across processes/restarts.
_EVENT_ID_NAMESPACE = uuid.UUID("6f5e6f0a-6a5e-4b8b-9d3e-6a2f8e6b7c1d")


def deterministic_event_id(thread_id: str, topic: str, event_type: str) -> str:
    """The idempotency key producers must use for a step's command/completion
    event: `uuid5(NAMESPACE, f"{thread_id}:{topic}:{event_type}")`, exactly as
    described in docs/event-driven-multi-agent-coordination-plan.md. Because a
    given (thread_id, topic, event_type) triple only ever legitimately occurs
    once for a linear workflow run, republishing after a crash/redelivery
    reproduces the same event_id, so `event_log`'s `UNIQUE(event_id)` +
    `ON CONFLICT DO NOTHING` collapses the retry instead of creating a second,
    undeduped row."""
    return str(uuid.uuid5(_EVENT_ID_NAMESPACE, f"{thread_id}:{topic}:{event_type}"))


@dataclass(frozen=True)
class Event:
    event_id: str
    """Idempotency key for *this message* -- producers should build it with
    `deterministic_event_id()`, not a random uuid4, so a retried publish
    dedupes via `UNIQUE(event_id)` instead of creating a second row. Not a
    run/thread identity -- one thread_id has many events (one per step
    transition)."""
    thread_id: str
    """Same value as persistence/call_log.py's thread_id -- no translation
    needed to join event_bus tables against call_log."""
    topic: str
    event_type: str
    payload: dict


class Delivery(Protocol):
    event: Event

    async def ack(self) -> None: ...
    async def nack(self, reason: str) -> None: ...


class EventBus(Protocol):
    async def publish(self, event: Event) -> None: ...

    def subscribe(
        self, topic: str, group: str, *, worker_id: str, start_from: StartFrom = "beginning"
    ) -> AbstractAsyncContextManager[AsyncIterator[Delivery]]:
        """`start_from` only matters the first time `group` is ever
        subscribed to `topic` -- it decides what happens to events already on
        the topic at that moment, mirroring Kafka's `auto.offset.reset`:

        - `"beginning"` (default, and the only behavior before this
          parameter existed): the new group gets a dispatch row for every
          event already on the topic, i.e. it replays the topic's full
          history.
        - `"now"`: every event already on the topic at first-subscribe time
          is marked done for this group without being delivered -- the group
          only ever sees events published from here on.

        Once a group has any dispatch row on a topic (whether from real
        processing or from `"now"`'s own seeding), `start_from` stops
        mattering for that (topic, group) pair -- there's no "already caught
        up, skip history" state to fall out of on a later restart, so callers
        can pass the same `start_from` on every startup without tracking
        whether this is the group's first run.

        A production step/master consumer group must always process every
        event on its topic, so orchestrator/worker.py and
        orchestrator/master_agent.py only ever use the default. `"now"`
        exists for consumers that opt into a topic *after* it already has
        history and must not reprocess it -- e.g. a future long-term-memory
        distiller subscribing to a workflow's completion events -- see
        docs/event-driven-multi-agent-coordination-plan.md and fixed.md's
        writeup on this for why this was previously only a manual,
        easy-to-forget SQL workaround."""
        ...


def commands_topic(workflow_name: str, step_name: str) -> str:
    return f"{workflow_name}.{step_name}.commands"


def events_topic(workflow_name: str) -> str:
    return f"{workflow_name}.events"
