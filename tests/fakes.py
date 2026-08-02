from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any


@dataclass
class FakeNotification:
    method: str
    payload: dict[str, Any]


class FakeTurnHandle:
    def __init__(self, turn_id: str) -> None:
        self.id = turn_id
        self.release = asyncio.Event()
        self.interrupted = False
        self.steers: list[str] = []
        self.raise_in_stream = False

    async def stream(self):
        yield FakeNotification("turn/started", {"turn_id": self.id})
        if self.raise_in_stream:
            raise RuntimeError("fake stream failure")
        await self.release.wait()
        yield FakeNotification(
            "item/agentMessage/delta",
            {"item_id": "agent-1", "delta": "done"},
        )
        yield FakeNotification(
            "turn/completed",
            {"turn_id": self.id, "status": "completed"},
        )

    async def steer(self, prompt: str):
        self.steers.append(prompt)
        return {"accepted": True}

    async def interrupt(self):
        self.interrupted = True
        self.release.set()
        return {"accepted": True}


class FakeGoalHandle:
    def __init__(self, codex: FakeCodex, thread_id: str, turn_id: str) -> None:
        self.codex = codex
        self.thread_id = thread_id
        self.id = turn_id
        self.release = asyncio.Event()
        self.interrupted = False
        self.steers: list[str] = []

    async def stream(self):
        yield FakeNotification("turn/started", {"turn_id": self.id})
        yield FakeNotification(
            "thread/goal/updated",
            {
                "thread_id": self.thread_id,
                "turn_id": self.id,
                "goal": deepcopy(self.codex.goals[self.thread_id]),
            },
        )
        await self.release.wait()
        goal = self.codex.goals.get(self.thread_id)
        if goal is not None:
            yield FakeNotification(
                "thread/goal/updated",
                {
                    "thread_id": self.thread_id,
                    "turn_id": self.id,
                    "goal": deepcopy(goal),
                },
            )
        yield FakeNotification(
            "turn/completed",
            {
                "turn_id": self.id,
                "status": "interrupted" if self.interrupted else "completed",
            },
        )

    async def steer(self, prompt: str):
        self.steers.append(prompt)
        return {"accepted": True}

    async def interrupt(self):
        self.interrupted = True
        goal = self.codex.goals.get(self.thread_id)
        if goal is not None:
            goal["status"] = "paused"
            goal["updated_at"] += 1
        self.release.set()
        return {"accepted": True}

    def complete(self, status: str = "complete") -> None:
        goal = self.codex.goals[self.thread_id]
        goal["status"] = status
        goal["updated_at"] += 1
        self.release.set()


class FakeThread:
    def __init__(self, codex: FakeCodex, thread_id: str) -> None:
        self.codex = codex
        self.id = thread_id

    async def read(self, *, include_turns: bool = False):
        thread = deepcopy(self.codex.threads[self.id])
        if not include_turns:
            thread["turns"] = []
        return SimpleNamespace(thread=thread)

    async def set_name(self, name: str):
        self.codex.threads[self.id]["name"] = name
        return {"name": name}

    async def turn(self, prompt: str, **kwargs):
        sequence = len(self.codex.handles) + 1
        handle = FakeTurnHandle(f"turn_{sequence}")
        handle.raise_in_stream = self.id in self.codex.stream_error_threads
        self.codex.handles[self.id] = handle
        self.codex.prompts.append((self.id, prompt))
        self.codex.turn_requests.append((self.id, prompt, kwargs))
        return handle


class FakeCodex:
    def __init__(
        self,
        project_path: str | Path,
        *,
        fail_start: bool = False,
        unauthenticated: bool = False,
    ) -> None:
        self.project_path = str(Path(project_path).resolve())
        self.fail_start = fail_start
        self.unauthenticated = unauthenticated
        self.global_notifications: asyncio.Queue[FakeNotification] = asyncio.Queue()
        self.entered = 0
        self.exited = 0
        self.handles: dict[str, FakeTurnHandle | FakeGoalHandle] = {}
        self.prompts: list[tuple[str, str]] = []
        self.turn_requests: list[tuple[str, str, dict[str, Any]]] = []
        self.goal_requests: list[tuple[str, str, int | None]] = []
        self.thread_resume_requests: list[tuple[str, dict[str, Any]]] = []
        self.thread_delete_requests: list[str] = []
        self.rate_limit_requests = 0
        self.rate_limits_response: dict[str, Any] = {
            "rate_limits": {
                "primary": {
                    "used_percent": 18,
                    "window_duration_mins": 10_080,
                    "resets_at": 1_785_624_480,
                },
                "secondary": None,
                "individual_limit": {
                    "limit": "1000",
                    "used": "250",
                    "remaining_percent": 75,
                    "resets_at": 1_785_883_680,
                },
            }
        }
        self.goals: dict[str, dict[str, Any]] = {}
        self.stream_error_threads: set[str] = set()
        self.threads: dict[str, dict[str, Any]] = {
            "thr_one": self._thread(
                "thr_one",
                name="Existing thread",
                turns=[
                    {
                        "id": "turn_history",
                        "status": "completed",
                        "items": [
                            {
                                "id": "user-1",
                                "type": "userMessage",
                                "content": [{"type": "text", "text": "Inspect tests"}],
                            },
                            {
                                "id": "agent-1",
                                "type": "agentMessage",
                                "text": "The tests are ready.",
                            },
                            {
                                "id": "plan-1",
                                "type": "plan",
                                "text": "1. Inspect\n2. Verify",
                            },
                            {
                                "id": "command-1",
                                "type": "commandExecution",
                                "command": "pytest -q",
                                "status": "completed",
                                "exit_code": 0,
                                "aggregated_output": "12 passed",
                            },
                            {
                                "id": "files-1",
                                "type": "fileChange",
                                "status": "completed",
                                "changes": [{"path": "main.py", "kind": "update"}],
                            },
                        ],
                    }
                ],
            ),
            "thr_two": self._thread("thr_two", name="Second thread"),
            "thr_archived": self._thread(
                "thr_archived",
                name="Archived thread",
                archived=True,
            ),
            "thr_outside": {
                **self._thread("thr_outside", name="Outside"),
                "cwd": "/tmp/not-registered",
            },
        }

    def _thread(
        self,
        thread_id: str,
        *,
        name: str,
        archived: bool = False,
        turns: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return {
            "id": thread_id,
            "cwd": self.project_path,
            "path": f"/private/thread-store/{thread_id}",
            "name": name,
            "preview": name,
            "status": "idle",
            "created_at": 1,
            "updated_at": 2,
            "archived": archived,
            "turns": turns or [],
        }

    async def __aenter__(self):
        self.entered += 1
        if self.fail_start:
            raise RuntimeError("fake startup failed")
        return self

    async def __aexit__(self, *_args):
        self.exited += 1

    async def account(self):
        return SimpleNamespace(
            account=(
                None
                if self.unauthenticated
                else {
                    "type": "chatgpt",
                    "email": "developer@example.com",
                    "plan_type": "business",
                }
            ),
            requires_openai_auth=self.unauthenticated,
        )

    async def rate_limits(self):
        self.rate_limit_requests += 1
        return deepcopy(self.rate_limits_response)

    async def next_notification(self) -> FakeNotification:
        return await self.global_notifications.get()

    async def models(self, *, include_hidden: bool = False):
        models = [
            {
                "id": "gpt-test",
                "model": "gpt-test",
                "display_name": "GPT Test",
                "hidden": False,
                "is_default": True,
                "default_reasoning_effort": "medium",
                "supported_reasoning_efforts": [
                    {
                        "reasoning_effort": "low",
                        "description": "Faster responses with less reasoning.",
                    },
                    {
                        "reasoning_effort": "medium",
                        "description": "Balances speed and reasoning depth.",
                    },
                    {
                        "reasoning_effort": "high",
                        "description": "Deeper reasoning for complex tasks.",
                    },
                    {
                        "reasoning_effort": "xhigh",
                        "description": "Extra high reasoning depth.",
                    },
                    {
                        "reasoning_effort": "max",
                        "description": "Consumes usage limits faster.",
                    },
                    {
                        "reasoning_effort": "ultra",
                        "description": "Consumes usage limits faster.",
                    },
                ],
            },
            {
                "id": "gpt-hidden",
                "model": "gpt-hidden",
                "display_name": "GPT Hidden",
                "hidden": True,
                "is_default": False,
                "default_reasoning_effort": "low",
                "supported_reasoning_efforts": [],
            },
        ]
        if not include_hidden:
            models = [model for model in models if not model["hidden"]]
        return SimpleNamespace(data=models, next_cursor=None)

    async def thread_list(
        self,
        *,
        archived: bool | None = None,
        cursor: str | None = None,
        cwd: str | None = None,
        limit: int | None = None,
        **_kwargs,
    ):
        data = [
            deepcopy(thread)
            for thread in self.threads.values()
            if (cwd is None or thread["cwd"] == cwd)
            and (archived is None or thread["archived"] is archived)
        ]
        offset = int(cursor or 0)
        page_size = limit or len(data)
        page = data[offset : offset + page_size]
        next_cursor = (
            str(offset + page_size) if offset + page_size < len(data) else None
        )
        return SimpleNamespace(
            data=page,
            next_cursor=next_cursor,
            backwards_cursor=None,
        )

    async def thread_start(self, *, cwd: str, **_kwargs):
        thread_id = f"thr_created_{len(self.threads)}"
        self.threads[thread_id] = self._thread(thread_id, name="Untitled thread")
        self.threads[thread_id]["cwd"] = cwd
        return FakeThread(self, thread_id)

    async def thread_resume(self, thread_id: str, **kwargs):
        if thread_id not in self.threads:
            raise RuntimeError("not found")
        self.thread_resume_requests.append((thread_id, kwargs))
        return FakeThread(self, thread_id)

    async def thread_fork(self, thread_id: str, **_kwargs):
        fork_id = f"{thread_id}_fork"
        self.threads[fork_id] = deepcopy(self.threads[thread_id])
        self.threads[fork_id]["id"] = fork_id
        self.threads[fork_id]["name"] = f"{self.threads[thread_id]['name']} (fork)"
        self.threads[fork_id]["archived"] = False
        return FakeThread(self, fork_id)

    async def thread_archive(self, thread_id: str):
        self.threads[thread_id]["archived"] = True
        return {"thread_id": thread_id}

    async def thread_delete(self, thread_id: str):
        self.thread_delete_requests.append(thread_id)
        self.goals.pop(thread_id, None)
        self.handles.pop(thread_id, None)
        self.threads.pop(thread_id)
        return {}

    async def thread_unarchive(self, thread_id: str):
        self.threads[thread_id]["archived"] = False
        return FakeThread(self, thread_id)

    async def goal_get(self, thread_id: str):
        goal = self.goals.get(thread_id)
        return deepcopy(goal) if goal is not None else None

    async def goal_start(
        self,
        thread_id: str,
        *,
        objective: str,
        token_budget: int | None,
    ):
        goal = {
            "thread_id": thread_id,
            "objective": objective,
            "status": "active",
            "token_budget": token_budget,
            "tokens_used": 0,
            "time_used_seconds": 0,
            "created_at": 1,
            "updated_at": 1,
        }
        self.goals[thread_id] = goal
        self.goal_requests.append((thread_id, objective, token_budget))
        handle = FakeGoalHandle(
            self,
            thread_id,
            f"goal_turn_{len(self.goal_requests)}",
        )
        self.handles[thread_id] = handle
        return handle, deepcopy(goal)

    async def goal_resume(self, thread_id: str):
        goal = self.goals[thread_id]
        goal["status"] = "active"
        goal["updated_at"] += 1
        handle = FakeGoalHandle(
            self,
            thread_id,
            f"goal_turn_{len(self.goal_requests) + 1}",
        )
        self.handles[thread_id] = handle
        return handle, deepcopy(goal)

    async def goal_pause(self, thread_id: str):
        goal = self.goals[thread_id]
        goal["status"] = "paused"
        goal["updated_at"] += 1
        return deepcopy(goal)

    async def goal_clear(self, thread_id: str):
        return self.goals.pop(thread_id, None) is not None
