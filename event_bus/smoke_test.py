"""Manual smoke test for PostgresEventBus -- run with:
    uv run python -m event_bus.smoke_test

No pytest in this repo yet (persistence/, mcp_servers/ etc. are all
verified by hand-run scripts too, e.g. transcribe.py at the repo root).
This exercises the three properties M0 promises: basic pub/sub, redelivery
of a crashed consumer's message, and NOTIFY beating pure polling on latency.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
import uuid

from dotenv import load_dotenv

from event_bus.base import Event
from event_bus.postgres import PostgresEventBus

load_dotenv()


def _topic() -> str:
    return f"smoke.{uuid.uuid4().hex[:8]}"


def _group(name: str) -> str:
    # _CLAIM (event_bus/postgres.py) scopes claims by consumer_group alone,
    # not topic -- a stale 'claimed' row left behind by an earlier manual
    # run of this scenario against the same dev database (e.g. a previous
    # assertion failure that skipped ack()) would otherwise still be
    # reclaimable under a fixed literal group name once its lease expires,
    # racing against this run's own events. Randomizing the group the same
    # way _topic() already does keeps each run's dispatch rows isolated.
    return f"smoke-{name}.{uuid.uuid4().hex[:8]}"


async def scenario_basic_pub_sub(bus: PostgresEventBus) -> None:
    topic = _topic()
    thread_id = str(uuid.uuid4())
    published = [f"payload-{i}" for i in range(5)]

    received: list[str] = []
    async with bus.subscribe(topic, "smoke-group", worker_id="w1") as deliveries:
        for text in published:
            await bus.publish(
                Event(event_id=str(uuid.uuid4()), thread_id=thread_id, topic=topic, event_type="smoke.msg", payload={"text": text})
            )

        async for delivery in deliveries:
            received.append(delivery.event.payload["text"])
            await delivery.ack()
            if len(received) == len(published):
                break

    assert received == published, f"expected {published}, got {received}"
    print(f"[basic_pub_sub] OK -- received {len(received)} events in order")


async def scenario_redelivery(bus: PostgresEventBus) -> None:
    topic = _topic()
    thread_id = str(uuid.uuid4())
    await bus.publish(
        Event(event_id=str(uuid.uuid4()), thread_id=thread_id, topic=topic, event_type="smoke.msg", payload={"text": "only-once"})
    )

    # First subscriber claims the message but "crashes" -- never acks, so
    # the dispatch row stays 'claimed' until its lease expires.
    async with bus.subscribe(topic, "smoke-redelivery", worker_id="crasher") as deliveries:
        first = await anext(deliveries)
        assert first.event.payload["text"] == "only-once"
        assert first.attempts == 1

    await asyncio.sleep(bus._lease_seconds + 1)  # wait past the lease

    async with bus.subscribe(topic, "smoke-redelivery", worker_id="rescuer") as deliveries:
        second = await anext(deliveries)
        assert second.event.payload["text"] == "only-once"
        assert second.attempts == 2, f"expected redelivery to bump attempts to 2, got {second.attempts}"
        await second.ack()

    print("[redelivery] OK -- crashed worker's message was reclaimed after lease expiry (attempts=2)")


async def scenario_start_from_now_skips_history(bus: PostgresEventBus) -> None:
    """A brand-new consumer_group subscribing with start_from="now" must
    never see events already on the topic before it first subscribed, but
    must still see whatever gets published afterwards -- the fix for
    TODO.md's "new consumer_group replays the whole topic" gap. Contrasted
    with the default ("beginning"), which replays everything, matching
    existing/pre-this-parameter behavior."""
    topic = _topic()
    thread_id = str(uuid.uuid4())

    async def _publish(text: str) -> None:
        await bus.publish(
            Event(event_id=str(uuid.uuid4()), thread_id=thread_id, topic=topic, event_type="smoke.msg", payload={"text": text})
        )

    await _publish("old-1")
    await _publish("old-2")

    # A brand-new group with the default start_from ("beginning") replays
    # the pre-existing history, same as before this parameter existed.
    # _FAN_OUT's bulk backlog insert has no ORDER BY, so the two pre-existing
    # events aren't guaranteed to arrive in publish order -- only that both
    # eventually do.
    seen: set[str] = set()
    async with bus.subscribe(topic, _group("beginning"), worker_id="w-beginning") as deliveries:
        for _ in range(2):
            delivery = await anext(deliveries)
            seen.add(delivery.event.payload["text"])
            await delivery.ack()
    assert seen == {"old-1", "old-2"}, seen
    print("[start_from_now_skips_history] confirmed start_from='beginning' (default) still replays history")

    # A brand-new group with start_from="now" must skip both pre-existing
    # events and only see what's published from here on.
    async with bus.subscribe(topic, _group("now"), worker_id="w-now", start_from="now") as deliveries:
        await _publish("new-1")
        delivered = await anext(deliveries)
        assert delivered.event.payload["text"] == "new-1", (
            f"expected start_from='now' to skip pre-existing history and deliver only 'new-1', got {delivered.event.payload}"
        )
        await delivered.ack()

    print("[start_from_now_skips_history] OK -- start_from='now' skipped both pre-existing events, only delivered the one published after subscribing")


async def scenario_notify_latency(bus: PostgresEventBus, poll_interval: float) -> None:
    topic = _topic()
    thread_id = str(uuid.uuid4())

    async with bus.subscribe(topic, "smoke-latency", worker_id="w1") as deliveries:
        consume_task = asyncio.create_task(anext(deliveries))
        await asyncio.sleep(0.2)  # let the subscriber settle into its LISTEN wait

        start = time.monotonic()
        await bus.publish(
            Event(event_id=str(uuid.uuid4()), thread_id=thread_id, topic=topic, event_type="smoke.msg", payload={"text": "fast"})
        )
        delivery = await consume_task
        elapsed = time.monotonic() - start
        await delivery.ack()

    print(f"[notify_latency] OK -- delivered in {elapsed:.2f}s (poll_interval={poll_interval:.0f}s)")
    assert elapsed < poll_interval / 2, f"expected NOTIFY to beat the poll interval, took {elapsed:.2f}s"


async def scenario_multi_channel_isolation(bus: PostgresEventBus, poll_interval: float) -> None:
    """Stage 4: every subscribe() on one bus now shares a single LISTEN
    connection (_SharedListener), which fans a Notify out to the right
    waiter(s) by `Notify.channel`. Three subscriptions on three different
    topics, published to one at a time -- each publish must wake only its
    own topic's subscriber, never the other two, or the channel-based
    filtering is broken and every subscriber would just see every NOTIFY."""
    topics = [_topic() for _ in range(3)]
    names = ["a", "b", "c"]
    thread_id = str(uuid.uuid4())

    async with (
        bus.subscribe(topics[0], _group("iso-a"), worker_id="w-a") as deliveries_a,
        bus.subscribe(topics[1], _group("iso-b"), worker_id="w-b") as deliveries_b,
        bus.subscribe(topics[2], _group("iso-c"), worker_id="w-c") as deliveries_c,
    ):
        deliveries = [deliveries_a, deliveries_b, deliveries_c]
        # Created once, outside the loop: cancelling a task wrapping
        # anext(gen) while it's suspended inside the generator propagates
        # the CancelledError through it and closes it, so an untriggered
        # subscriber's task must stay alive (not be cancelled/recreated)
        # across rounds -- only the round's target gets a fresh anext().
        tasks = [asyncio.create_task(anext(d)) for d in deliveries]
        await asyncio.sleep(0.2)  # let all three settle into their LISTEN wait

        elapsed = 0.0
        for target in range(3):
            start = time.monotonic()
            await bus.publish(
                Event(
                    event_id=str(uuid.uuid4()),
                    thread_id=thread_id,
                    topic=topics[target],
                    event_type="smoke.msg",
                    payload={"text": f"only-{names[target]}"},
                )
            )
            delivery = await tasks[target]
            elapsed = time.monotonic() - start
            assert delivery.event.payload["text"] == f"only-{names[target]}"
            await delivery.ack()
            assert elapsed < poll_interval / 2, f"expected NOTIFY on channel {names[target]!r} to beat the poll interval, took {elapsed:.2f}s"

            for i in (i for i in range(3) if i != target):
                assert not tasks[i].done(), f"channel {names[i]!r}'s subscriber was woken by a NOTIFY meant for channel {names[target]!r}"

            tasks[target] = asyncio.create_task(anext(deliveries[target]))

        for task in tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    print(f"[multi_channel_isolation] OK -- each of 3 channels' NOTIFY woke only its own subscriber in {elapsed:.2f}s, sharing one LISTEN connection")


async def scenario_late_subscribe_does_not_drop_earlier(bus: PostgresEventBus, poll_interval: float) -> None:
    """_SharedListener.register() tears down and rebuilds the shared
    connection's LISTEN state (cancel notifies() task -> UNLISTEN * ->
    LISTEN every channel -> restart) every time a new subscription joins --
    see event_bus/postgres.py's _relisten(). A pre-existing subscription
    must keep getting its NOTIFYs promptly after that rebuild, not silently
    degrade to pure polling because the rebuild only re-LISTENed the new
    channel and forgot the old one."""
    topic_first = _topic()
    thread_id = str(uuid.uuid4())

    async with bus.subscribe(topic_first, _group("late-first"), worker_id="w-first") as deliveries_first:
        async with bus.subscribe(_topic(), _group("late-second"), worker_id="w-second"):
            # Just entering this second subscribe() already exercised
            # _relisten() once while the first subscription was live.
            task_first = asyncio.create_task(anext(deliveries_first))
            await asyncio.sleep(0.2)

            start = time.monotonic()
            await bus.publish(
                Event(event_id=str(uuid.uuid4()), thread_id=thread_id, topic=topic_first, event_type="smoke.msg", payload={"text": "still-here"})
            )
            delivery = await task_first
            elapsed = time.monotonic() - start
            assert delivery.event.payload["text"] == "still-here"
            await delivery.ack()

    print(f"[late_subscribe_does_not_drop_earlier] OK -- first subscription still got its NOTIFY in {elapsed:.2f}s after a second subscription joined")
    assert elapsed < poll_interval / 2, f"expected NOTIFY to beat the poll interval, took {elapsed:.2f}s"


async def main() -> None:
    database_url = os.environ["PERSISTENCE_DATABASE_URL"]
    bus = PostgresEventBus(database_url)
    await bus.ensure_schema()

    await scenario_basic_pub_sub(bus)
    await scenario_redelivery(PostgresEventBus(database_url, lease_seconds=2))
    await scenario_start_from_now_skips_history(bus)
    await scenario_notify_latency(PostgresEventBus(database_url, poll_interval=5.0), poll_interval=5.0)
    await scenario_multi_channel_isolation(PostgresEventBus(database_url, poll_interval=5.0), poll_interval=5.0)
    await scenario_late_subscribe_does_not_drop_earlier(PostgresEventBus(database_url, poll_interval=5.0), poll_interval=5.0)

    print("\nAll M0 smoke tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
