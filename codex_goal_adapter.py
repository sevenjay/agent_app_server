"""Compatibility adapter for Codex long-running goal operations.

The 0.144 Python SDK ships typed goal protocol support on its wrapped async
client, but does not expose it from the high-level ``AsyncCodex`` API yet.
Keeping that version-specific access here prevents the Web service from
depending on SDK private attributes throughout the application.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from openai_codex._goal import _GoalStreamClosed, _GoalStreamCursor
from openai_codex.generated.v2_all import (
    ThreadGoalClearResponse,
    ThreadGoalGetResponse,
    ThreadGoalSetResponse,
    ThreadGoalStatus,
)

_GOAL_START_TIMEOUT_SECONDS = 30.0


def _goal_method(codex: Any, name: str):
    """Return an optional high-level method used by the in-process fake adapter."""
    method = getattr(codex, name, None)
    return method if callable(method) else None


@dataclass(slots=True)
class CodexGoalHandle:
    """Logical handle that coalesces every physical goal Turn into one stream."""

    _client: Any
    _state: Any
    thread_id: str
    id: str

    async def stream(self) -> AsyncIterator[Any]:
        cursor = _GoalStreamCursor(self._state)
        try:
            while True:
                notification = await self._client.next_goal_notification(self._state)
                events, completed = cursor.process(notification)
                if str(getattr(notification, "method", "")).startswith("thread/goal/"):
                    yield notification
                for event in events:
                    yield event
                if completed:
                    return
        except _GoalStreamClosed:
            return
        except asyncio.CancelledError:
            await self._client.cancel_goal_operation(self._state)
            raise
        finally:
            self._state.finish()
            self._state.wake_notification_reader()
            self._client.unregister_goal_operation(self._state)

    async def steer(self, prompt: str) -> Any:
        turn_id = self._state.current_turn()
        if turn_id is None:
            raise RuntimeError("The goal is between continuation turns and cannot be steered yet")
        return await self._client.turn_steer(self.thread_id, turn_id, prompt)

    async def interrupt(self) -> None:
        await self._client.cancel_goal_operation(self._state)


class CodexGoalAdapter:
    """Typed goal RPCs and logical operation construction for one AsyncCodex."""

    def __init__(self, codex: Any) -> None:
        self.codex = codex

    def _client(self) -> Any:
        client = getattr(self.codex, "_client", None)
        if client is None:
            raise RuntimeError("The installed Codex SDK does not expose goal protocol access")
        return client

    async def get(self, thread_id: str) -> Any | None:
        if method := _goal_method(self.codex, "goal_get"):
            return await method(thread_id)
        response = await self._client().request(
            "thread/goal/get",
            {"threadId": thread_id},
            response_model=ThreadGoalGetResponse,
        )
        return response.goal

    async def start(
        self,
        thread_id: str,
        *,
        objective: str,
        token_budget: int | None,
    ) -> tuple[Any, Any]:
        if method := _goal_method(self.codex, "goal_start"):
            return await method(
                thread_id,
                objective=objective,
                token_budget=token_budget,
            )

        client = self._client()
        state, logical_turn_id = await client.start_goal_operation(thread_id, objective)
        handle = CodexGoalHandle(client, state, thread_id, logical_turn_id)
        try:
            if token_budget is not None:
                await client.request(
                    "thread/goal/set",
                    {"threadId": thread_id, "tokenBudget": token_budget},
                    response_model=ThreadGoalSetResponse,
                )
            goal = await self.get(thread_id)
            if goal is None:
                raise RuntimeError("Codex started a goal without returning persisted state")
            return handle, goal
        except BaseException:
            await client.cancel_goal_operation(state)
            state.finish()
            client.unregister_goal_operation(state)
            raise

    async def resume(self, thread_id: str) -> tuple[Any, Any]:
        if method := _goal_method(self.codex, "goal_resume"):
            return await method(thread_id)

        client = self._client()
        state = client.register_goal_operation(thread_id)
        activated = False
        try:
            response = await client.thread_goal_set(
                thread_id,
                status=ThreadGoalStatus.active,
            )
            activated = True
            logical_turn_id = await asyncio.to_thread(
                state.wait_for_start,
                _GOAL_START_TIMEOUT_SECONDS,
            )
            if logical_turn_id is None:
                raise RuntimeError("Timed out waiting for the resumed goal to start")
            return (
                CodexGoalHandle(client, state, thread_id, logical_turn_id),
                response.goal,
            )
        except BaseException:
            if activated:
                await client.cancel_goal_operation(state)
            state.finish()
            client.unregister_goal_operation(state)
            raise

    async def pause(self, thread_id: str) -> Any:
        if method := _goal_method(self.codex, "goal_pause"):
            return await method(thread_id)
        response = await self._client().thread_goal_set(
            thread_id,
            status=ThreadGoalStatus.paused,
        )
        return response.goal

    async def clear(self, thread_id: str) -> bool:
        if method := _goal_method(self.codex, "goal_clear"):
            return bool(await method(thread_id))
        response: ThreadGoalClearResponse = await self._client().thread_goal_clear(thread_id)
        return bool(response.cleared)
