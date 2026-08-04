import asyncio
from pathlib import Path

import pytest
from openai_codex import ApprovalMode, Sandbox
from openai_codex.errors import (
    InvalidParamsError,
    InvalidRequestError,
    TransportClosedError,
)

import codex_service
from codex_service import (
    CodexService,
    ConsoleBadRequest,
    ConsoleConflict,
    ConsoleNotFound,
    ConsoleTimeout,
    ConsoleUnavailable,
)
from event_hub import EventHub
from projects import Project, ProjectRegistry
from tests.fakes import FakeCodex, FakeGoalHandle, FakeThread
from turn_manager import TurnManager, TurnNotActiveError


def make_service(fake: FakeCodex, project_path: Path) -> CodexService:
    return CodexService(
        fake,
        registry=ProjectRegistry(
            [Project("agent_app_server", "Agent App Server", project_path.resolve())]
        ),
        event_hub=EventHub(history_limit=20, subscriber_queue_limit=10),
        turn_manager=TurnManager(),
        approval_mode=ApprovalMode.auto_review,
        sandbox=Sandbox.workspace_write,
        operation_timeout=1,
    )


@pytest.mark.asyncio
async def test_new_empty_thread_is_readable_before_first_message(
    tmp_path: Path,
) -> None:
    class UnmaterializedThread(FakeThread):
        async def read(self, *, include_turns: bool = False):
            if include_turns and self.id in self.codex.unmaterialized_threads:
                raise InvalidRequestError(
                    -32600,
                    f"thread {self.id} is not materialized yet; "
                    "includeTurns is unavailable before first user message",
                )
            return await super().read(include_turns=include_turns)

    class UnmaterializedThreadFake(FakeCodex):
        def __init__(self, project_path: Path) -> None:
            super().__init__(project_path)
            self.unmaterialized_threads: set[str] = set()

        async def thread_start(self, *, cwd: str, **kwargs):
            thread = await super().thread_start(cwd=cwd, **kwargs)
            self.unmaterialized_threads.add(thread.id)
            return UnmaterializedThread(self, thread.id)

    service = make_service(UnmaterializedThreadFake(tmp_path), tmp_path)

    created = await service.create_thread(
        project_key="agent_app_server",
        name=None,
        model=None,
    )
    read = await service.read_thread(created["id"], include_turns=True)

    assert created["id"].startswith("thr_created_")
    assert created["turns"] == []
    assert read["id"] == created["id"]
    assert read["turns"] == []


@pytest.mark.asyncio
async def test_new_thread_stays_usable_before_it_appears_in_thread_list(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class UnlistedNewThreadFake(FakeCodex):
        async def thread_list(self, **kwargs):
            response = await super().thread_list(**kwargs)
            response.data = [
                thread
                for thread in response.data
                if not thread["id"].startswith("thr_created_")
            ]
            return response

    messages: list[str] = []

    def record_debug(message: str, *_args, **_kwargs) -> None:
        messages.append(message)

    monkeypatch.setattr(codex_service, "LOGD", record_debug)
    service = make_service(UnlistedNewThreadFake(tmp_path), tmp_path)

    created = await service.create_thread(
        project_key="agent_app_server",
        name=None,
        model=None,
    )
    page = await service.list_threads(
        project_key="agent_app_server",
        archived=False,
        limit=30,
    )
    read = await service.read_thread(created["id"], include_turns=False)
    started = await service.start_turn(
        created["id"],
        prompt="Persist this new thread",
        model=None,
    )
    await service.interrupt_turn(created["id"])
    for _ in range(20):
        if not await service.turn_manager.is_active(created["id"]):
            break
        await asyncio.sleep(0)

    assert created["id"] in {thread["id"] for thread in page["data"]}
    assert read["id"] == created["id"]
    assert started["accepted"] is True
    assert any(
        f"codex_thread_create_complete project_key=agent_app_server "
        f"thread_id={created['id']}" in message
        for message in messages
    )
    assert any(
        f"codex_thread_authorize_fresh thread_id={created['id']}" in message
        for message in messages
    )
    assert not any(
        f"codex_thread_lookup_miss thread_id={created['id']}" in message
        for message in messages
    )


@pytest.mark.asyncio
async def test_thread_operations_are_scoped_to_registry(tmp_path: Path) -> None:
    fake = FakeCodex(tmp_path)
    service = make_service(fake, tmp_path)

    page = await service.list_threads(
        project_key="agent_app_server",
        archived=False,
        limit=30,
    )
    assert {thread["id"] for thread in page["data"]} == {"thr_one", "thr_two"}
    assert all("cwd" not in thread and "path" not in thread for thread in page["data"])

    thread = await service.read_thread("thr_one")
    assert thread["name"] == "Existing thread"
    renamed = await service.rename_thread("thr_one", "Renamed")
    assert renamed["name"] == "Renamed"

    with pytest.raises(ConsoleNotFound):
        await service.read_thread("thr_outside")
    with pytest.raises(ConsoleNotFound):
        await service.read_thread("does_not_exist")


@pytest.mark.asyncio
async def test_thread_is_revalidated_after_resume(tmp_path: Path) -> None:
    class MovingThreadFake(FakeCodex):
        async def thread_resume(self, thread_id: str, **kwargs):
            thread = await super().thread_resume(thread_id, **kwargs)
            self.threads[thread_id]["cwd"] = "/tmp/moved-outside-registry"
            return thread

    service = make_service(MovingThreadFake(tmp_path), tmp_path)
    with pytest.raises(ConsoleNotFound):
        await service.read_thread("thr_one")


@pytest.mark.asyncio
async def test_thread_list_has_cursor_pagination(tmp_path: Path) -> None:
    fake = FakeCodex(tmp_path)
    for index in range(35):
        thread_id = f"thr_bulk_{index}"
        fake.threads[thread_id] = fake._thread(thread_id, name=f"Bulk {index}")
    service = make_service(fake, tmp_path)

    first = await service.list_threads(
        project_key="agent_app_server",
        archived=False,
        limit=30,
    )
    second = await service.list_threads(
        project_key="agent_app_server",
        archived=False,
        cursor=first["next_cursor"],
        limit=30,
    )
    assert len(first["data"]) == 30
    assert len(second["data"]) == 7
    assert first["next_cursor"] is not None
    assert second["next_cursor"] is None


@pytest.mark.asyncio
async def test_thread_list_only_maps_typed_invalid_parameters_to_400(
    tmp_path: Path,
) -> None:
    fake = FakeCodex(tmp_path)
    service = make_service(fake, tmp_path)

    async def invalid_cursor(**_kwargs):
        raise InvalidParamsError(-32602, "invalid cursor")

    fake.thread_list = invalid_cursor
    with pytest.raises(ConsoleBadRequest):
        await service.list_threads(
            project_key="agent_app_server",
            cursor="invalid",
        )

    async def closed_transport(**_kwargs):
        raise TransportClosedError("closed")

    fake.thread_list = closed_transport
    with pytest.raises(ConsoleUnavailable):
        await service.list_threads(
            project_key="agent_app_server",
            cursor="opaque",
        )


@pytest.mark.asyncio
async def test_create_fork_archive_delete_and_unarchive(tmp_path: Path) -> None:
    fake = FakeCodex(tmp_path)
    service = make_service(fake, tmp_path)

    created = await service.create_thread(
        project_key="agent_app_server",
        name="Created",
        model="gpt-test",
    )
    assert created["name"] == "Created"
    assert created["project_key"] == "agent_app_server"

    forked = await service.fork_thread("thr_one")
    assert forked["id"] == "thr_one_fork"
    assert (await service.archive_thread("thr_one"))["archived"] is True
    assert (await service.unarchive_thread("thr_one"))["id"] == "thr_one"
    assert await service.delete_thread("thr_two") == {
        "thread_id": "thr_two",
        "project_key": "agent_app_server",
        "deleted": True,
    }
    assert fake.thread_delete_requests == ["thr_two"]
    assert "thr_two" not in fake.threads


@pytest.mark.asyncio
async def test_delete_thread_uses_generated_rpc_when_flat_sdk_method_is_missing(
    tmp_path: Path,
) -> None:
    class RawDeleteClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, str], type]] = []

        async def request(self, method, params, *, response_model):
            self.calls.append((method, params, response_model))
            return response_model.model_validate({})

    class LegacyDeleteFake(FakeCodex):
        thread_delete = None

    fake = LegacyDeleteFake(tmp_path)
    raw_client = RawDeleteClient()
    fake._client = raw_client
    service = make_service(fake, tmp_path)

    deleted = await service.delete_thread("thr_two")

    assert deleted["deleted"] is True
    assert len(raw_client.calls) == 1
    method, params, response_model = raw_client.calls[0]
    assert method == "thread/delete"
    assert params == {"threadId": "thr_two"}
    assert response_model.__name__ == "ThreadDeleteResponse"


@pytest.mark.asyncio
async def test_delete_thread_accepts_rpc_failure_when_list_confirms_removal(
    tmp_path: Path,
) -> None:
    class PartiallyFailedDeleteFake(FakeCodex):
        async def thread_delete(self, thread_id: str):
            await super().thread_delete(thread_id)
            raise RuntimeError("state cleanup failed after rollout deletion")

    fake = PartiallyFailedDeleteFake(tmp_path)
    service = make_service(fake, tmp_path)

    deleted = await service.delete_thread("thr_two")

    assert deleted["deleted"] is True
    assert "thr_two" not in fake.threads


@pytest.mark.asyncio
async def test_delete_pending_thread_accepts_partial_rpc_failure_when_handle_is_gone(
    tmp_path: Path,
) -> None:
    class PendingThread(FakeThread):
        async def read(self, *, include_turns: bool = False):
            if self.id not in self.codex.threads:
                raise InvalidRequestError(
                    -32600,
                    f"thread not loaded: {self.id}",
                )
            return await super().read(include_turns=include_turns)

    class PartiallyFailedPendingDeleteFake(FakeCodex):
        async def thread_start(self, *, cwd: str, **kwargs):
            thread = await super().thread_start(cwd=cwd, **kwargs)
            return PendingThread(self, thread.id)

        async def thread_delete(self, thread_id: str):
            await super().thread_delete(thread_id)
            raise RuntimeError("no such table: agent_jobs")

    fake = PartiallyFailedPendingDeleteFake(tmp_path)
    service = make_service(fake, tmp_path)
    created = await service.create_thread(
        project_key="agent_app_server",
        name=None,
        model=None,
    )

    deleted = await service.delete_thread(created["id"])

    assert deleted["deleted"] is True
    assert created["id"] not in fake.threads
    listed_ids = {
        thread["id"]
        for thread in (await service.list_threads(project_key="agent_app_server"))[
            "data"
        ]
    }
    assert created["id"] not in listed_ids


@pytest.mark.asyncio
async def test_delete_pending_thread_preserves_rpc_failure_when_handle_remains(
    tmp_path: Path,
) -> None:
    class FailedPendingDeleteFake(FakeCodex):
        async def thread_delete(self, thread_id: str):
            self.thread_delete_requests.append(thread_id)
            raise RuntimeError("delete failed before removing thread")

    fake = FailedPendingDeleteFake(tmp_path)
    service = make_service(fake, tmp_path)
    created = await service.create_thread(
        project_key="agent_app_server",
        name=None,
        model=None,
    )

    with pytest.raises(ConsoleUnavailable):
        await service.delete_thread(created["id"])

    assert created["id"] in fake.threads
    listed_ids = {
        thread["id"]
        for thread in (await service.list_threads(project_key="agent_app_server"))[
            "data"
        ]
    }
    assert created["id"] in listed_ids


@pytest.mark.asyncio
async def test_delete_thread_preserves_rpc_failure_when_session_still_exists(
    tmp_path: Path,
) -> None:
    class FailedDeleteFake(FakeCodex):
        async def thread_delete(self, thread_id: str):
            self.thread_delete_requests.append(thread_id)
            raise RuntimeError("delete failed before removing rollout")

    fake = FailedDeleteFake(tmp_path)
    service = make_service(fake, tmp_path)

    with pytest.raises(ConsoleUnavailable):
        await service.delete_thread("thr_two")

    assert "thr_two" in fake.threads


@pytest.mark.asyncio
async def test_same_thread_conflicts_and_different_threads_run_concurrently(
    tmp_path: Path,
) -> None:
    fake = FakeCodex(tmp_path)
    service = make_service(fake, tmp_path)

    first = await service.start_turn(
        "thr_one",
        prompt="one",
        model="gpt-test",
        reasoning_effort="xhigh",
    )
    assert first["accepted"] is True
    with pytest.raises(ConsoleConflict):
        await service.start_turn("thr_one", prompt="duplicate", model=None)

    second = await service.start_turn("thr_two", prompt="two", model=None)
    assert second["accepted"] is True
    status = await service.turn_manager.status()
    assert status["active_turn_count"] == 2
    assert status["active_threads"]["thr_one"]["model"] == "gpt-test"
    assert status["active_threads"]["thr_one"]["reasoning_effort"] == "xhigh"
    assert fake.turn_requests[0][2]["effort"] == "xhigh"

    steered = await service.steer_turn("thr_one", "new direction")
    assert steered["accepted"] is True
    assert fake.handles["thr_one"].steers == ["new direction"]

    with pytest.raises(ConsoleConflict):
        await service.archive_thread("thr_one")
    with pytest.raises(ConsoleConflict):
        await service.delete_thread("thr_one")

    await service.interrupt_turn("thr_one")
    await service.interrupt_turn("thr_two")
    for _ in range(20):
        if (await service.turn_manager.status())["active_turn_count"] == 0:
            break
        await asyncio.sleep(0)
    assert (await service.turn_manager.status())["active_turn_count"] == 0


@pytest.mark.asyncio
async def test_stream_exception_releases_active_state_and_emits_error(
    tmp_path: Path,
) -> None:
    fake = FakeCodex(tmp_path)
    fake.stream_error_threads.add("thr_one")
    service = make_service(fake, tmp_path)
    subscription = await service.event_hub.subscribe("thr_one")

    await service.start_turn("thr_one", prompt="fail later", model=None)
    for _ in range(30):
        if not await service.turn_manager.is_active("thr_one"):
            break
        await asyncio.sleep(0)

    assert await service.turn_manager.is_active("thr_one") is False
    methods: list[str] = []
    while not subscription.queue.empty():
        event = await service.event_hub.next_event(subscription)
        if event is not None:
            methods.append(event.method)
    assert "console.turn.error" in methods
    assert "console.turn.idle" in methods
    await service.event_hub.close(subscription)


@pytest.mark.asyncio
async def test_goal_lifecycle_is_one_logical_operation_and_conflicts_with_turns(
    tmp_path: Path,
) -> None:
    fake = FakeCodex(tmp_path)
    service = make_service(fake, tmp_path)
    service.event_hub = EventHub(history_limit=100, subscriber_queue_limit=100)
    subscription = await service.event_hub.subscribe("thr_one")

    assert await service.get_goal("thr_one") is None
    started = await service.start_goal(
        "thr_one",
        objective="Finish the release",
        token_budget=50_000,
        model="gpt-test",
        reasoning_effort="xhigh",
    )
    assert started["accepted"] is True
    assert started["model"] == "gpt-test"
    assert started["reasoning_effort"] == "xhigh"
    assert started["goal"]["status"] == "active"
    assert fake.goal_requests == [("thr_one", "Finish the release", 50_000)]
    assert fake.thread_resume_requests[-1][1]["model"] == "gpt-test"
    assert fake.thread_resume_requests[-1][1]["config"] == {
        "model_reasoning_effort": "xhigh"
    }
    active = (await service.turn_manager.status())["active_threads"]["thr_one"]
    assert active["kind"] == "goal"
    assert active["model"] == "gpt-test"
    assert active["reasoning_effort"] == "xhigh"
    with pytest.raises(ConsoleConflict):
        await service.start_turn("thr_one", prompt="ordinary turn", model=None)

    await service.steer_turn("thr_one", "Focus on tests")
    handle = fake.handles["thr_one"]
    assert isinstance(handle, FakeGoalHandle)
    assert handle.steers == ["Focus on tests"]

    paused = await service.pause_goal("thr_one")
    assert paused["goal"]["status"] == "paused"
    for _ in range(20):
        if not await service.turn_manager.is_active("thr_one"):
            break
        await asyncio.sleep(0)
    assert await service.turn_manager.is_active("thr_one") is False

    resumed = await service.resume_goal(
        "thr_one",
        model="gpt-test",
        reasoning_effort="xhigh",
    )
    assert resumed["model"] == "gpt-test"
    assert resumed["reasoning_effort"] == "xhigh"
    assert resumed["goal"]["status"] == "active"
    assert fake.thread_resume_requests[-1][1]["model"] == "gpt-test"
    assert fake.thread_resume_requests[-1][1]["config"] == {
        "model_reasoning_effort": "xhigh"
    }
    resumed_handle = fake.handles["thr_one"]
    assert isinstance(resumed_handle, FakeGoalHandle)
    resumed_handle.complete()
    for _ in range(20):
        if not await service.turn_manager.is_active("thr_one"):
            break
        await asyncio.sleep(0)
    assert (await service.get_goal("thr_one"))["status"] == "complete"

    assert (await service.clear_goal("thr_one"))["cleared"] is True
    assert await service.get_goal("thr_one") is None
    events = []
    while not subscription.queue.empty():
        event = await service.event_hub.next_event(subscription)
        if event is not None:
            events.append(event)
    methods = [event.method for event in events]
    assert "console.goal.starting" in methods
    assert "console.goal.running" in methods
    assert "thread/goal/updated" in methods
    assert "console.goal.idle" in methods
    assert "thread/goal/cleared" in methods
    model_events = [
        event
        for event in events
        if event.method in {"console.goal.starting", "console.goal.running"}
    ]
    assert model_events
    assert all(event.data["model"] == "gpt-test" for event in model_events)
    assert all(event.data["reasoning_effort"] == "xhigh" for event in model_events)
    await service.event_hub.close(subscription)


@pytest.mark.asyncio
async def test_shutdown_pauses_and_drains_an_active_goal(tmp_path: Path) -> None:
    fake = FakeCodex(tmp_path)
    service = make_service(fake, tmp_path)
    await service.start_goal(
        "thr_one",
        objective="Keep working while the browser is disconnected",
        token_budget=None,
        model=None,
        reasoning_effort=None,
    )

    await service.turn_manager.shutdown(timeout=1)

    assert fake.goals["thr_one"]["status"] == "paused"
    assert (await service.turn_manager.status())["active_turn_count"] == 0


@pytest.mark.asyncio
async def test_operation_timeout_has_stable_504_error(tmp_path: Path) -> None:
    fake = FakeCodex(tmp_path)
    service = make_service(fake, tmp_path)
    service.operation_timeout = 0.001

    async def slow_models(*, include_hidden: bool = False):
        await asyncio.sleep(1)

    fake.models = slow_models
    with pytest.raises(ConsoleTimeout) as caught:
        await service.list_models()
    assert caught.value.status_code == 504
    assert caught.value.code == "codex_timeout"


@pytest.mark.asyncio
async def test_interrupt_completion_race_is_reported_as_conflict(
    tmp_path: Path,
) -> None:
    fake = FakeCodex(tmp_path)
    service = make_service(fake, tmp_path)
    await service.turn_manager.reserve("thr_one")
    await service.turn_manager.mark_running(
        "thr_one",
        turn_id="turn_one",
        handle=fake.handles.setdefault("thr_one", object()),
    )

    async def completed_before_interrupt(_thread_id: str) -> None:
        raise TurnNotActiveError

    service.turn_manager.interrupt = completed_before_interrupt  # type: ignore[method-assign]

    with pytest.raises(ConsoleConflict):
        await service.interrupt_turn("thr_one")
