"""Postgres-backed EventBus -- the first, default EventBus implementation.

Delivery mechanism: polling is the correctness backstop, LISTEN/NOTIFY is a
latency optimization layered on top of it, not the other way around.
Postgres's NOTIFY is not durable -- a notification fired while nobody is
LISTENing is lost forever, which is incompatible with at-least-once
delivery. So the base loop always polls `event_dispatch` on a fixed
interval; `publish()` additionally fires a NOTIFY so an idle subscriber
usually wakes up almost instantly, and a missed NOTIFY just means the event
surfaces on the next poll tick instead of being lost for good.

Each topic gets its own NOTIFY channel, derived via `_channel_for_topic()`:
a workflow's topic name is user-controlled (see event_bus/base.py's
commands_topic/events_topic) and Postgres channel names are capped at 63
bytes, so the channel name is a fixed-length hash of the topic rather than
the topic string itself -- this keeps the topic-naming convention free of
a length limit to worry about while still scoping wakeups to the topic.
Two distinct topics hashing to the same channel is possible but harmless:
a subscriber woken by a false-positive NOTIFY just re-checks its own
topic/group (a cheap indexed query) and finds nothing to claim -- same
outcome as a missed NOTIFY falling back to the next poll tick, just
triggered by a spurious wakeup instead. A collision only costs a wasted
query for the unrelated subscribers sharing that hash, never a correctness
problem.

Competing consumers within one consumer_group share work via
`SELECT ... FOR UPDATE SKIP LOCKED` -- the standard "Postgres as a queue"
recipe -- so multiple worker replicas for the same step can run side by
side without extra coordination.

All connections come from one pooled `AsyncConnectionPool` per bus instance
instead of opening/closing a raw connection per call -- a worker's claim
loop calls `_claim_one` continuously (every poll_interval, forever), so
per-call connect/auth overhead would otherwise be a permanent, avoidable
cost for every long-running worker/master process.

Every `subscribe()` on the same bus instance also shares one LISTEN
connection (`_SharedListener` below) instead of opening its own -- Postgres
lets a single connection LISTEN on multiple channels at once, and
`Notify.channel` tells a shared listener which subscription's `wake` to
set, so "one dedicated connection per subscription" was never a mechanism
requirement, just the simplest way to write it originally.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from event_bus.base import Event, StartFrom


def _channel_for_topic(topic: str) -> str:
    digest = hashlib.sha256(topic.encode()).hexdigest()[:16]
    return f"eb_{digest}"


_CREATE_EVENT_LOG = """
CREATE TABLE IF NOT EXISTS event_log (
    id BIGSERIAL PRIMARY KEY,
    event_id UUID NOT NULL UNIQUE,
    thread_id TEXT NOT NULL,
    topic TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS event_log_topic_id_idx ON event_log (topic, id);
"""

_CREATE_EVENT_DISPATCH = """
CREATE TABLE IF NOT EXISTS event_dispatch (
    id BIGSERIAL PRIMARY KEY,
    event_log_id BIGINT NOT NULL REFERENCES event_log(id),
    consumer_group TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'claimed', 'done', 'failed')),
    claimed_by TEXT,
    visible_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    attempts INT NOT NULL DEFAULT 0,
    last_error TEXT,
    done_at TIMESTAMPTZ,
    UNIQUE (event_log_id, consumer_group)
);
CREATE INDEX IF NOT EXISTS event_dispatch_claim_idx ON event_dispatch (consumer_group, status, visible_at);
"""

_INSERT_EVENT = """
INSERT INTO event_log (event_id, thread_id, topic, event_type, payload)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (event_id) DO NOTHING;
"""

# Fan-out: the first time any subscriber in `group` sees an event_log row on
# its topic, it gets its own event_dispatch row to claim/ack independently
# of every other group. Deliberately NOT `id > MAX(dispatched for this
# group)`: event_log.id is a BIGSERIAL, and Postgres assigns sequence values
# before commit, so two concurrent publish() transactions can commit out of
# id order (a lower id can become visible *after* a higher one already was).
# A MAX()-based watermark would then permanently skip the lower id once the
# watermark has moved past it -- silent, unrecoverable event loss. NOT
# EXISTS is immune to that: it re-checks every row on the topic against
# event_dispatch's `UNIQUE(event_log_id, consumer_group)` index rather than
# trusting a monotonic cutoff, at the cost of scanning the topic's rows
# every poll tick instead of only the ones past a cutoff.
_FAN_OUT = """
INSERT INTO event_dispatch (event_log_id, consumer_group)
SELECT el.id, %s
FROM event_log el
WHERE el.topic = %s
  AND NOT EXISTS (
    SELECT 1 FROM event_dispatch ed WHERE ed.event_log_id = el.id AND ed.consumer_group = %s
  )
ON CONFLICT (event_log_id, consumer_group) DO NOTHING;
"""

# subscribe(start_from="now")'s seeding step: marks every event already on
# the topic as 'done' for `group` -- without this ever being delivered --
# but only the first time this group is ever seen *on this topic* (the
# `NOT EXISTS` guard is scoped to (topic, group) via the join, not just
# `group`, unlike _FAN_OUT/_CLAIM's usual group-only scoping elsewhere in
# this file -- those rely on the "one group is always paired with exactly
# one topic, forever" naming convention documented in orchestrator/worker.py
# and orchestrator/master_agent.py, but this guard's whole job is deciding
# "have I ever seen this group here before", so it can't lean on that
# convention holding). Once the group has a single dispatch row of any
# status on this topic, this becomes a no-op forever after, including on
# every later restart passing start_from="now" again. Deliberately one
# statement rather than a separate check-then-insert: two replicas of the
# same brand-new group racing this concurrently both see "no dispatch rows
# yet" and both attempt to seed the same rows, which converges safely via
# ON CONFLICT DO NOTHING -- no lock or leader election needed. Same caveat as
# Kafka's auto.offset.reset=latest: an event published in the narrow window
# around this statement's execution may or may not be caught by it, which is
# an inherent ambiguity of "now" as a starting point, not a bug -- see
# fixed.md's writeup on this for why a precise cutoff isn't attempted.
_SEED_DONE_IF_NEW_GROUP = """
INSERT INTO event_dispatch (event_log_id, consumer_group, status, done_at)
SELECT el.id, %s, 'done', now()
FROM event_log el
WHERE el.topic = %s
  AND NOT EXISTS (
    SELECT 1 FROM event_dispatch ed
    JOIN event_log el2 ON el2.id = ed.event_log_id
    WHERE el2.topic = %s AND ed.consumer_group = %s
  )
ON CONFLICT (event_log_id, consumer_group) DO NOTHING;
"""

# Claims exactly one dispatch row for `group`: either freshly pending, or
# claimed-but-expired (its holder never ack'd/nack'd within the lease --
# treated as a crashed worker and redelivered). SKIP LOCKED lets N workers
# in the same group run this concurrently and never double-claim a row.
_CLAIM = """
UPDATE event_dispatch
SET status = 'claimed', claimed_by = %s, visible_at = now() + (%s || ' seconds')::interval,
    attempts = attempts + 1
WHERE id = (
    SELECT id FROM event_dispatch
    WHERE consumer_group = %s
      AND (status = 'pending' OR (status = 'claimed' AND visible_at <= now()))
    ORDER BY id
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
RETURNING id, event_log_id, attempts;
"""

_FETCH_EVENT = "SELECT event_id, thread_id, topic, event_type, payload FROM event_log WHERE id = %s;"
_ACK = "UPDATE event_dispatch SET status = 'done', done_at = now() WHERE id = %s;"
_NACK_RETRY = "UPDATE event_dispatch SET status = 'pending', last_error = %s, visible_at = now() WHERE id = %s;"
_NACK_FAIL = "UPDATE event_dispatch SET status = 'failed', last_error = %s WHERE id = %s;"


class _SharedListener:
    """一條連線服務這個 bus 的所有訂閱。收到通知後依 Notify.channel
    只叫醒該 channel 的等待者。Lazily-created -- a bus with zero active
    subscriptions never opens this connection at all.

    channel 的 LISTEN 狀態永遠跟 `_waiters` 的 key 集合同步：只要這個集合的
    *key* 真的變了（新 channel 加入、或最後一個等待者離開讓 channel 整個
    消失），就整批 UNLISTEN * 再重新 LISTEN 目前的 channel，而不是只加/減
    異動的那個 -- 三個 channel 對一個 process 來說整批重來沒有性能顧慮，
    換來的是不必個別追蹤「這個 channel 還有沒有人要」的正確性。同一個
    channel 多個等待者彼此加入/離開（key 集合沒變）則不觸發重建。
    """

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url
        self._conn: psycopg.AsyncConnection | None = None
        self._waiters: dict[str, set[asyncio.Event]] = {}
        self._notify_task: asyncio.Task | None = None
        # register()/unregister() both read-modify-write _waiters and then
        # tear down + rebuild the listener connection's LISTEN state -- must
        # be serialized so two callers racing don't interleave those steps.
        self._lock = asyncio.Lock()
        # Bumped every time _relisten() finishes installing a connection/task
        # (or tearing down to none). Lets a _reconnect_with_retry() call that
        # was sleeping between attempts notice a concurrent register()/
        # unregister() already repaired the listener, so it can bail instead
        # of nulling out and reconnecting over the connection that just got
        # fixed -- see _reconnect_with_retry().
        self._generation = 0

    async def register(self, channel: str, wake: asyncio.Event) -> None:
        async with self._lock:
            is_new_channel = channel not in self._waiters
            self._waiters.setdefault(channel, set()).add(wake)
            # A second waiter joining a channel that's already LISTENed
            # doesn't change the LISTEN set -- only rebuild when it does,
            # so N subscribers sharing one channel don't each pay a
            # cancel/UNLISTEN*/relisten-all/restart cycle.
            if is_new_channel:
                await self._relisten()

    async def unregister(self, channel: str, wake: asyncio.Event) -> None:
        async with self._lock:
            waiters = self._waiters.get(channel)
            if waiters is None:
                return
            waiters.discard(wake)
            if not waiters:
                del self._waiters[channel]
                await self._relisten()

    async def _relisten(self) -> None:
        # 前置驗證 ③：conn.notifies() 執行期間這條連線的鎖被整個 while True
        # 迴圈占住，此時對同一條連線下 LISTEN 會永久卡住。所以每次要改
        # LISTEN 集合，都必須先讓 notifies() 的 task 結束（釋放鎖），改完
        # 再重啟。重啟期間漏掉的 NOTIFY 就退化成該次輪詢晚一輪撿到 --
        # module docstring 開宗明義的設計前提，不是新風險。
        if self._notify_task is not None:
            self._notify_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._notify_task
            self._notify_task = None

        if not self._waiters:
            # Deliberately NOT closing the connection here. The previous
            # close-now/reconnect-in-register() cycle left a window between
            # "old connection gone" and "new connection's LISTEN takes
            # effect" wide enough (TCP connect + auth) for a NOTIFY fired in
            # between to be silently dropped -- Postgres NOTIFY is
            # fire-and-forget to a connection that isn't LISTENing (see
            # module docstring). Keeping the connection open (LISTEN state
            # and all) means the only gap left is two local SQL statements
            # (UNLISTEN */LISTEN) on the next _relisten(), not a network
            # round trip -- a channel with zero local waiters staying
            # LISTENed in the meantime just costs one ignored NOTIFY if it
            # fires, same as the documented false-collision case above.
            # ponytail: idle listener connection stays open indefinitely
            # once any subscription has ever existed on this bus -- add an
            # idle timeout that actually closes it if a bus whose
            # subscriptions permanently end needs the connection back.
            self._generation += 1
            return

        if self._conn is None or self._conn.closed:
            self._conn = await psycopg.AsyncConnection.connect(self._database_url, autocommit=True)
        await self._conn.execute("UNLISTEN *;")
        for channel in self._waiters:
            await self._conn.execute(f"LISTEN {channel};")
        self._notify_task = asyncio.create_task(self._run())
        self._generation += 1

    async def _run(self) -> None:
        assert self._conn is not None
        my_generation = self._generation
        try:
            async for notify in self._conn.notifies():
                for wake in self._waiters.get(notify.channel, ()):
                    wake.set()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Connection dropped -- reconnect and re-LISTEN every channel
            # this bus's subscribers still care about. Shared listener means
            # one dropped connection affects every subscription on this bus
            # at once (unlike the old one-listen-conn-per-subscription
            # design), so this reconnect exists specifically to bound that
            # blast radius to "brief gap", not "permanently degraded to
            # pure polling" -- which is exactly what happens if the
            # reconnect attempt itself fails and nothing retries it, hence
            # _reconnect_with_retry below instead of one bare attempt.
            await self._reconnect_with_retry(my_generation)

    async def _reconnect_with_retry(self, broken_generation: int) -> None:
        delay = 1.0
        while True:
            async with self._lock:
                if self._generation != broken_generation:
                    # A concurrent register()/unregister() already repaired
                    # (or tore down) the listener via its own _relisten()
                    # while we were sleeping below -- bail instead of nulling
                    # out and reconnecting over the connection/task that call
                    # just installed, which would leak both.
                    return
                self._notify_task = None
                self._conn = None
                try:
                    await self._relisten()
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    print(f"[event_bus] shared listener reconnect failed, retrying in {delay:.0f}s: {exc!r}", flush=True)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)


async def _ensure_pool_open(pool: AsyncConnectionPool) -> None:
    # Not every PostgresEventBus instance is guaranteed to have had
    # ensure_schema() called on it first (e.g. a scenario constructing a
    # second bus instance sharing the same already-migrated DB) -- opening
    # lazily on first real use, here and in every entry point below, means
    # the pool works regardless of call order rather than only when
    # ensure_schema() happens to run first.
    if pool.closed:
        await pool.open()


@dataclass(frozen=True)
class PostgresDelivery:
    event: Event
    dispatch_id: int
    attempts: int
    _pool: AsyncConnectionPool
    _max_attempts: int

    async def ack(self) -> None:
        await _ensure_pool_open(self._pool)
        async with self._pool.connection() as conn:
            await conn.execute(_ACK, (self.dispatch_id,))

    async def nack(self, reason: str) -> None:
        # Past max_attempts, stop redelivering -- an endlessly-failing message
        # would otherwise loop forever between claim and nack.
        query = _NACK_FAIL if self.attempts >= self._max_attempts else _NACK_RETRY
        await _ensure_pool_open(self._pool)
        async with self._pool.connection() as conn:
            await conn.execute(query, (reason, self.dispatch_id))


class PostgresEventBus:
    def __init__(
        self,
        database_url: str,
        *,
        poll_interval: float = 60.0,
        lease_seconds: int = 30,
        max_attempts: int = 5,
        pool: AsyncConnectionPool | None = None,
    ) -> None:
        self._database_url = database_url
        self._poll_interval = poll_interval
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        # A caller sharing a pool (e.g. the master process reusing
        # persistence/pool.py's process-wide pool with run_state) passes one
        # in; otherwise this bus opens its own -- keeping open the case of a
        # bus pointed at a different DB than the shared pool's. No caller has
        # ever needed a non-default size, so it's not a constructor param --
        # add one back if a real caller needs a different size.
        self._pool = pool if pool is not None else AsyncConnectionPool(database_url, open=False, min_size=1, max_size=4)
        self._listener: _SharedListener | None = None

    def _get_listener(self) -> _SharedListener:
        if self._listener is None:
            self._listener = _SharedListener(self._database_url)
        return self._listener

    async def ensure_schema(self) -> None:
        await _ensure_pool_open(self._pool)
        async with self._pool.connection() as conn:
            await conn.execute(_CREATE_EVENT_LOG)
            await conn.execute(_CREATE_EVENT_DISPATCH)

    async def publish(self, event: Event) -> None:
        await _ensure_pool_open(self._pool)
        async with self._pool.connection() as conn:
            await conn.execute(
                _INSERT_EVENT,
                (event.event_id, event.thread_id, event.topic, event.event_type, Jsonb(event.payload)),
            )
            await conn.execute("SELECT pg_notify(%s, '')", (_channel_for_topic(event.topic),))

    @asynccontextmanager
    async def subscribe(
        self, topic: str, group: str, *, worker_id: str, start_from: StartFrom = "beginning"
    ) -> AsyncIterator[AsyncIterator[PostgresDelivery]]:
        if start_from == "now":
            # Must run before this group's first _FAN_OUT (inside the
            # deliveries loop below) ever gets a chance to fan today's
            # backlog out to 'pending' -- otherwise there'd be nothing left
            # for this seeding step to mark 'done' instead.
            await _ensure_pool_open(self._pool)
            async with self._pool.connection() as conn:
                await conn.execute(_SEED_DONE_IF_NEW_GROUP, (group, topic, topic, group))

        wake = asyncio.Event()
        channel = _channel_for_topic(topic)
        listener = self._get_listener()
        try:
            # register() inside the try: if it raises after already adding
            # `wake` to the listener's waiters (e.g. _relisten() fails to
            # connect), the finally below must still run to remove it --
            # otherwise that waiter leaks forever (dead Event nobody will
            # ever clear, and its channel entry never goes empty).
            await listener.register(channel, wake)
            yield self._deliveries(topic, group, worker_id, wake)
        finally:
            await listener.unregister(channel, wake)

    async def _deliveries(
        self, topic: str, group: str, worker_id: str, wake: asyncio.Event
    ) -> AsyncIterator[PostgresDelivery]:
        while True:
            claimed = await self._claim_one(topic, group, worker_id)
            if claimed is not None:
                yield claimed
                continue
            wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(wake.wait(), timeout=self._poll_interval)

    async def _claim_one(self, topic: str, group: str, worker_id: str) -> PostgresDelivery | None:
        await _ensure_pool_open(self._pool)
        async with self._pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(_FAN_OUT, (group, topic, group))
                await cur.execute(_CLAIM, (worker_id, self._lease_seconds, group))
                claim_row = await cur.fetchone()
                if claim_row is None:
                    return None
                await cur.execute(_FETCH_EVENT, (claim_row["event_log_id"],))
                event_row = await cur.fetchone()
        assert event_row is not None  # claimed row always has a matching event_log row (FK)
        event = Event(
            event_id=str(event_row["event_id"]),
            thread_id=event_row["thread_id"],
            topic=event_row["topic"],
            event_type=event_row["event_type"],
            payload=event_row["payload"],
        )
        return PostgresDelivery(
            event=event,
            dispatch_id=claim_row["id"],
            attempts=claim_row["attempts"],
            _pool=self._pool,
            _max_attempts=self._max_attempts,
        )
