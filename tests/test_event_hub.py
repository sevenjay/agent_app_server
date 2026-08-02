import pytest

from event_hub import EventHub


@pytest.mark.asyncio
async def test_event_sequence_replay_and_last_event_id() -> None:
    hub = EventHub(history_limit=3, subscriber_queue_limit=2)
    first = await hub.publish(
        "thr_1",
        event_type="codex.notification",
        method="turn/started",
    )
    second = await hub.publish(
        "thr_1",
        event_type="codex.notification",
        method="item/started",
    )
    subscription = await hub.subscribe("thr_1", after_sequence=first.sequence)

    assert [event.sequence for event in subscription.initial_events] == [second.sequence]
    assert subscription.resync_required is False
    assert await hub.subscriber_count() == 1
    await hub.close(subscription)
    assert await hub.subscriber_count() == 0


@pytest.mark.asyncio
async def test_history_gap_and_slow_subscriber_require_resync() -> None:
    hub = EventHub(history_limit=2, subscriber_queue_limit=1)
    for index in range(3):
        await hub.publish(
            "thr_1",
            event_type="codex.notification",
            method=f"event/{index}",
        )
    stale = await hub.subscribe("thr_1", after_sequence=0)
    assert stale.resync_required is True
    await hub.close(stale)

    slow = await hub.subscribe("thr_1")
    await hub.publish("thr_1", event_type="test", method="one")
    await hub.publish("thr_1", event_type="test", method="two")
    assert await hub.next_event(slow) is None
    assert hub.dropped_subscriber_count == 1


@pytest.mark.asyncio
async def test_backend_restart_last_event_id_requires_resync() -> None:
    restarted_hub = EventHub(history_limit=2, subscriber_queue_limit=1)
    subscription = await restarted_hub.subscribe("thr_1", after_sequence=1042)
    assert subscription.resync_required is True
