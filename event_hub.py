"""Non-blocking, process-local Codex event fan-out and replay."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    sequence: int
    thread_id: str
    turn_id: str | None
    type: str
    method: str
    data: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "type": self.type,
            "method": self.method,
            "data": self.data,
        }


_RESYNC = object()


@dataclass(eq=False, slots=True)
class Subscription:
    thread_id: str
    queue: asyncio.Queue[EventEnvelope | object]
    initial_events: list[EventEnvelope]
    resync_required: bool
    closed: bool = False


@dataclass(slots=True)
class _ThreadEvents:
    history: deque[EventEnvelope]
    sequence: int = 0
    subscribers: set[Subscription] = field(default_factory=set)


class EventHub:
    def __init__(self, *, history_limit: int = 2000, subscriber_queue_limit: int = 1000) -> None:
        if history_limit < 1 or subscriber_queue_limit < 1:
            raise ValueError("EventHub limits must be positive")
        self._history_limit = history_limit
        self._subscriber_queue_limit = subscriber_queue_limit
        self._threads: dict[str, _ThreadEvents] = {}
        self._lock = asyncio.Lock()
        self._dropped_subscribers = 0

    def _state(self, thread_id: str) -> _ThreadEvents:
        state = self._threads.get(thread_id)
        if state is None:
            state = _ThreadEvents(history=deque(maxlen=self._history_limit))
            self._threads[thread_id] = state
        return state

    async def publish(
        self,
        thread_id: str,
        *,
        event_type: str,
        method: str,
        data: dict[str, Any] | None = None,
        turn_id: str | None = None,
    ) -> EventEnvelope:
        async with self._lock:
            state = self._state(thread_id)
            state.sequence += 1
            envelope = EventEnvelope(
                sequence=state.sequence,
                thread_id=thread_id,
                turn_id=turn_id,
                type=event_type,
                method=method,
                data=data or {},
            )
            state.history.append(envelope)
            for subscriber in tuple(state.subscribers):
                try:
                    subscriber.queue.put_nowait(envelope)
                except asyncio.QueueFull:
                    state.subscribers.discard(subscriber)
                    subscriber.closed = True
                    self._dropped_subscribers += 1
                    while not subscriber.queue.empty():
                        subscriber.queue.get_nowait()
                    subscriber.queue.put_nowait(_RESYNC)
            return envelope

    async def subscribe(
        self,
        thread_id: str,
        *,
        after_sequence: int | None = None,
    ) -> Subscription:
        async with self._lock:
            state = self._state(thread_id)
            history = list(state.history)
            resync_required = False
            initial: list[EventEnvelope] = []
            if after_sequence is not None:
                if after_sequence > state.sequence:
                    resync_required = True
                elif history and after_sequence < history[0].sequence - 1:
                    resync_required = True
                else:
                    initial = [event for event in history if event.sequence > after_sequence]
            subscription = Subscription(
                thread_id=thread_id,
                queue=asyncio.Queue(maxsize=self._subscriber_queue_limit),
                initial_events=initial,
                resync_required=resync_required,
            )
            state.subscribers.add(subscription)
            return subscription

    async def close(self, subscription: Subscription) -> None:
        async with self._lock:
            state = self._threads.get(subscription.thread_id)
            if state is not None:
                state.subscribers.discard(subscription)
            subscription.closed = True

    async def next_event(self, subscription: Subscription) -> EventEnvelope | None:
        item = await subscription.queue.get()
        if item is _RESYNC:
            return None
        assert isinstance(item, EventEnvelope)
        return item

    async def current_sequence(self, thread_id: str) -> int:
        async with self._lock:
            return self._state(thread_id).sequence

    async def subscriber_count(self) -> int:
        async with self._lock:
            return sum(len(state.subscribers) for state in self._threads.values())

    @property
    def dropped_subscriber_count(self) -> int:
        return self._dropped_subscribers
