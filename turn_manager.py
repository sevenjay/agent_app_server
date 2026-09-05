"""Atomic one-active-turn-per-thread state management."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Literal


class TurnConflictError(RuntimeError):
    pass


class TurnNotActiveError(RuntimeError):
    pass


class TurnsUnavailableError(RuntimeError):
    pass


@dataclass(slots=True)
class ActiveTurn:
    thread_id: str
    owner_id: str | None = None
    kind: Literal["turn", "goal"] = "turn"
    model: str | None = None
    reasoning_effort: str | None = None
    status: str = "starting"
    turn_id: str | None = None
    handle: Any = None
    task: asyncio.Task[Any] | None = None


class TurnManager:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._active: dict[tuple[str | None, str], ActiveTurn] = {}
        self._mutating: set[tuple[str | None, str]] = set()
        self._accepting_turns = True

    async def reserve(
        self,
        thread_id: str,
        *,
        kind: Literal["turn", "goal"] = "turn",
        model: str | None = None,
        reasoning_effort: str | None = None,
        owner_id: str | None = None,
    ) -> ActiveTurn:
        async with self._lock:
            if not self._accepting_turns:
                raise TurnsUnavailableError("New turns are disabled during shutdown")
            key = (owner_id, thread_id)
            if key in self._active or key in self._mutating:
                raise TurnConflictError("Thread already has an active turn")
            active = ActiveTurn(
                thread_id=thread_id,
                owner_id=owner_id,
                kind=kind,
                model=model,
                reasoning_effort=reasoning_effort,
            )
            self._active[key] = active
            return active

    async def reserve_mutation(
        self,
        thread_id: str,
        *,
        owner_id: str | None = None,
    ) -> None:
        """Exclude Turn start and conflicting thread mutations atomically."""
        async with self._lock:
            key = (owner_id, thread_id)
            if key in self._active or key in self._mutating:
                raise TurnConflictError("Thread is active or already being changed")
            self._mutating.add(key)

    async def finish_mutation(
        self,
        thread_id: str,
        *,
        owner_id: str | None = None,
    ) -> None:
        async with self._lock:
            self._mutating.discard((owner_id, thread_id))

    async def mark_running(
        self,
        thread_id: str,
        *,
        turn_id: str,
        handle: Any,
        owner_id: str | None = None,
    ) -> None:
        async with self._lock:
            active = self._active.get((owner_id, thread_id))
            if active is None:
                raise TurnNotActiveError("Turn reservation no longer exists")
            active.status = "running"
            active.turn_id = turn_id
            active.handle = handle

    async def attach_task(
        self,
        thread_id: str,
        task: asyncio.Task[Any],
        *,
        owner_id: str | None = None,
    ) -> None:
        async with self._lock:
            active = self._active.get((owner_id, thread_id))
            if active is None:
                task.cancel()
                raise TurnNotActiveError("Turn reservation no longer exists")
            active.task = task

    async def finish(
        self,
        thread_id: str,
        *,
        turn_id: str | None = None,
        owner_id: str | None = None,
    ) -> None:
        async with self._lock:
            key = (owner_id, thread_id)
            active = self._active.get(key)
            if active is None:
                return
            if turn_id is not None and active.turn_id not in (None, turn_id):
                return
            self._active.pop(key, None)

    async def is_active(self, thread_id: str, *, owner_id: str | None = None) -> bool:
        async with self._lock:
            return (owner_id, thread_id) in self._active

    async def active(
        self,
        thread_id: str,
        *,
        owner_id: str | None = None,
    ) -> ActiveTurn:
        async with self._lock:
            active = self._active.get((owner_id, thread_id))
            if active is None or active.handle is None:
                raise TurnNotActiveError("Thread has no controllable active turn")
            return active

    async def current(
        self,
        thread_id: str,
        *,
        owner_id: str | None = None,
    ) -> ActiveTurn | None:
        """Return the current operation snapshot, including a starting reservation."""
        async with self._lock:
            return self._active.get((owner_id, thread_id))

    async def steer(
        self,
        thread_id: str,
        prompt: str,
        *,
        owner_id: str | None = None,
    ) -> Any:
        active = await self.active(thread_id, owner_id=owner_id)
        return await active.handle.steer(prompt)

    async def interrupt(
        self,
        thread_id: str,
        *,
        owner_id: str | None = None,
    ) -> Any:
        async with self._lock:
            active = self._active.get((owner_id, thread_id))
            if active is None or active.handle is None:
                raise TurnNotActiveError("Thread has no controllable active turn")
            active.status = "stopping"
            handle = active.handle
        return await handle.interrupt()

    async def status(self, *, owner_id: str | None = None) -> dict[str, Any]:
        async with self._lock:
            active_turns = [
                active
                for active in self._active.values()
                if owner_id is None or active.owner_id == owner_id
            ]
            return {
                "accepting_turns": self._accepting_turns,
                "active_turn_count": len(active_turns),
                "active_threads": {
                    active.thread_id: {
                        "status": active.status,
                        "kind": active.kind,
                        "turn_id": active.turn_id,
                        "model": active.model,
                        "reasoning_effort": active.reasoning_effort,
                    }
                    for active in active_turns
                },
            }

    async def shutdown(self, *, timeout: float) -> None:
        async with self._lock:
            self._accepting_turns = False
            active_turns = list(self._active.values())

        interrupts = [
            active.handle.interrupt()
            for active in active_turns
            if active.handle is not None
        ]
        if interrupts:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*interrupts, return_exceptions=True),
                    timeout=timeout,
                )
            except TimeoutError:
                pass
        tasks = [active.task for active in active_turns if active.task is not None]
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=timeout)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                if not task.cancelled():
                    task.exception()

        async with self._lock:
            self._active.clear()
            self._mutating.clear()

    async def enable(self) -> None:
        async with self._lock:
            self._accepting_turns = True
