from collections import deque

import pytest
from openai_codex._goal import _GoalOperationState
from openai_codex.generated.v2_all import (
    ThreadGoal,
    ThreadGoalStatus,
    ThreadGoalUpdatedNotification,
    Turn,
    TurnCompletedNotification,
    TurnStartedNotification,
    TurnStatus,
)
from openai_codex.models import Notification, UnknownNotification

from codex_goal_adapter import CodexGoalHandle


def _turn(turn_id: str, status: TurnStatus) -> Turn:
    return Turn(id=turn_id, items=[], status=status, started_at=1)


def _goal(status: ThreadGoalStatus) -> ThreadGoal:
    return ThreadGoal(
        created_at=1,
        objective="Finish the release",
        status=status,
        thread_id="thr_one",
        time_used_seconds=12,
        token_budget=1000,
        tokens_used=120,
        updated_at=2,
    )


@pytest.mark.asyncio
async def test_logical_goal_handle_coalesces_physical_turns_and_keeps_goal_updates() -> None:
    notifications = deque(
        [
            Notification(
                "turn/started",
                TurnStartedNotification(
                    thread_id="thr_one",
                    turn=_turn("physical_one", TurnStatus.in_progress),
                ),
            ),
            Notification(
                "thread/goal/updated",
                ThreadGoalUpdatedNotification(
                    thread_id="thr_one",
                    turn_id="physical_one",
                    goal=_goal(ThreadGoalStatus.active),
                ),
            ),
            Notification(
                "item/agentMessage/delta",
                UnknownNotification(
                    {"threadId": "thr_one", "turnId": "physical_one", "delta": "one"}
                ),
            ),
            Notification(
                "turn/completed",
                TurnCompletedNotification(
                    thread_id="thr_one",
                    turn=_turn("physical_one", TurnStatus.completed),
                ),
            ),
            Notification(
                "turn/started",
                TurnStartedNotification(
                    thread_id="thr_one",
                    turn=_turn("physical_two", TurnStatus.in_progress),
                ),
            ),
            Notification(
                "item/agentMessage/delta",
                UnknownNotification(
                    {"threadId": "thr_one", "turnId": "physical_two", "delta": "two"}
                ),
            ),
            Notification(
                "turn/completed",
                TurnCompletedNotification(
                    thread_id="thr_one",
                    turn=_turn("physical_two", TurnStatus.completed),
                ),
            ),
            Notification(
                "thread/goal/updated",
                ThreadGoalUpdatedNotification(
                    thread_id="thr_one",
                    turn_id="physical_two",
                    goal=_goal(ThreadGoalStatus.complete),
                ),
            ),
        ]
    )

    class FakeAsyncClient:
        def __init__(self) -> None:
            self.unregistered = False

        async def next_goal_notification(self, _state):
            return notifications.popleft()

        def unregister_goal_operation(self, _state) -> None:
            self.unregistered = True

        async def cancel_goal_operation(self, _state) -> None:
            raise AssertionError("completed goal must not be cancelled")

    state = _GoalOperationState(
        thread_id="thr_one",
        logical_turn_id="logical_goal_turn",
    )
    client = FakeAsyncClient()
    handle = CodexGoalHandle(client, state, "thr_one", "logical_goal_turn")

    events = [event async for event in handle.stream()]

    assert [event.method for event in events] == [
        "turn/started",
        "thread/goal/updated",
        "item/agentMessage/delta",
        "item/agentMessage/delta",
        "thread/goal/updated",
        "turn/completed",
    ]
    deltas = [event.payload for event in events if event.method.endswith("delta")]
    assert [payload.params["turnId"] for payload in deltas] == [
        "logical_goal_turn",
        "logical_goal_turn",
    ]
    completed = events[-1].payload
    assert completed.turn.id == "logical_goal_turn"
    assert client.unregistered is True
