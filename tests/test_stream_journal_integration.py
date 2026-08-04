import asyncio
from pathlib import Path

import pytest
from openai_codex import ApprovalMode, Sandbox

from codex_service import CodexService
from event_hub import EventHub
from projects import Project, ProjectRegistry
from stream_journal import StreamJournal
from tests.fakes import FakeCodex
from turn_manager import TurnManager


def service_for(
    fake: FakeCodex,
    project_path: Path,
    journal: StreamJournal | None = None,
) -> CodexService:
    return CodexService(
        fake,
        registry=ProjectRegistry([Project("agent_app_server", "Agent App Server", project_path.resolve())]),
        event_hub=EventHub(history_limit=2, subscriber_queue_limit=20),
        turn_manager=TurnManager(),
        approval_mode=ApprovalMode.auto_review,
        sandbox=Sandbox.workspace_write,
        stream_journal=journal or StreamJournal(),
        operation_timeout=1,
    )


async def wait_until_idle(service: CodexService, thread_id: str) -> None:
    for _ in range(50):
        if not await service.turn_manager.is_active(thread_id):
            return
        await asyncio.sleep(0)
    raise AssertionError("fake turn did not finish")


@pytest.mark.asyncio
async def test_disconnected_stream_survives_backend_restart_and_replays_from_disk(
    tmp_path: Path,
) -> None:
    fake = FakeCodex(tmp_path)
    first = service_for(fake, tmp_path)

    started = await first.start_turn(
        "thr_one",
        prompt="Persist me without a browser",
        model=None,
    )
    command = await first.publish_notification(
        "thr_one",
        method="item/completed",
        turn_id=started["turn_id"],
        data={
            "item": {
                "id": "exec-live",
                "type": "commandExecution",
                "command": "pytest -q",
                "status": "completed",
                "exit_code": 0,
                "aggregated_output": "journal-only command output",
            }
        },
    )
    assert command is not None
    await first.publish_notification(
        "thr_one",
        method="turn/diff/updated",
        turn_id=started["turn_id"],
        data={"diff": "diff --git a/main.py b/main.py\n+durable\n"},
    )
    await first.publish_notification(
        "thr_one",
        method="turn/tokenUsage/updated",
        turn_id=started["turn_id"],
        data={
            "token_usage": {
                "total": {"total_tokens": 42},
                "last": {"total_tokens": 12},
                "model_context_window": 100_000,
            }
        },
    )
    fake.handles["thr_one"].release.set()
    await wait_until_idle(first, "thr_one")

    restarted = service_for(fake, tmp_path)
    snapshot = await restarted.snapshot_thread("thr_one")
    assert [turn["id"] for turn in snapshot["turns"]] == [
        "turn_history",
        started["turn_id"],
    ]
    live_turn = next(turn for turn in snapshot["turns"] if turn["id"] == started["turn_id"])
    assert snapshot["journal_coverage"] == "complete"
    assert [item["type"] for item in live_turn["items"]] == [
        "userMessage",
        "commandExecution",
        "agentMessage",
    ]
    assert live_turn["items"][0]["content"][0]["text"] == ("Persist me without a browser")
    assert live_turn["items"][1]["aggregated_output"] == ("journal-only command output")
    assert live_turn["items"][2]["text"] == "done"
    assert snapshot["journal_diff"].endswith("+durable\n")
    assert snapshot["journal_usage"]["token_usage"]["total"]["total_tokens"] == 42

    subscription, replay, cursor, resync = await restarted.subscribe_events(
        "thr_one",
        after_sequence=command.sequence - 1,
    )
    try:
        assert resync is False
        assert cursor == snapshot["journal_cursor"]
        assert any(item.sequence == command.sequence and item.method == "item/completed" for item in replay)
    finally:
        await restarted.event_hub.close(subscription)


@pytest.mark.asyncio
async def test_complete_live_turn_is_not_reimported_when_history_changes(
    tmp_path: Path,
) -> None:
    fake = FakeCodex(tmp_path)
    service = service_for(fake, tmp_path)
    thread_id = "thr_two"
    turn_id = "turn_live"

    initial = await service.snapshot_thread(thread_id)
    assert initial["turns"] == []

    for method, data in (
        ("turn/started", {"turn_id": turn_id}),
        (
            "item/completed",
            {
                "item": {
                    "id": "live-user",
                    "type": "userMessage",
                    "content": [{"type": "text", "text": "Check tomorrow's weather"}],
                }
            },
        ),
        (
            "item/completed",
            {
                "item": {
                    "id": "live-search",
                    "type": "webSearch",
                    "action": {"type": "search", "query": "tomorrow weather"},
                }
            },
        ),
        (
            "item/completed",
            {"item": {"id": "live-agent", "type": "agentMessage", "text": "Sunny."}},
        ),
        ("turn/completed", {"turn_id": turn_id, "status": "completed"}),
    ):
        await service.publish_notification(
            thread_id,
            method=method,
            turn_id=turn_id,
            data=data,
        )

    fake.threads[thread_id]["updated_at"] = 3
    fake.threads[thread_id]["turns"] = [
        {
            "id": turn_id,
            "status": "completed",
            "items": [
                {
                    "id": "item-1",
                    "type": "userMessage",
                    "content": [{"type": "text", "text": "Check tomorrow's weather"}],
                },
                {
                    "id": "live-search",
                    "type": "webSearch",
                    "action": {"type": "search", "query": "tomorrow weather"},
                },
                {"id": "item-2", "type": "agentMessage", "text": "Sunny."},
            ],
        }
    ]
    before = await service.stream_journal.read(tmp_path, thread_id)

    snapshot = await service.snapshot_thread(thread_id)
    after = await service.stream_journal.read(tmp_path, thread_id)
    appended = [event for event in after.events if event["seq"] > before.cursor]

    assert [event["type"] for event in appended] == ["history.baseline_imported"]
    assert not [event for event in after.events if event.get("source") == "codex_history" and event.get("turn_id") == turn_id]
    assert snapshot["journal_coverage"] == "complete"
    assert len(snapshot["turns"]) == 1
    assert [item["type"] for item in snapshot["turns"][0]["items"]] == [
        "userMessage",
        "webSearch",
        "agentMessage",
    ]
    assert all(set(item["source_ids"]) == {"codex_stream"} for item in snapshot["turns"][0]["items"])


@pytest.mark.asyncio
async def test_history_reconciliation_imports_only_turns_missing_from_live_journal(
    tmp_path: Path,
) -> None:
    fake = FakeCodex(tmp_path)
    service = service_for(fake, tmp_path)
    thread_id = "thr_two"
    live_turn_id = "turn_live"
    history_turn_id = "turn_elsewhere"

    await service.snapshot_thread(thread_id)
    for method, data in (
        ("turn/started", {"turn_id": live_turn_id}),
        (
            "item/completed",
            {"item": {"id": "live-agent", "type": "agentMessage", "text": "Observed live."}},
        ),
        ("turn/completed", {"turn_id": live_turn_id, "status": "completed"}),
    ):
        await service.publish_notification(
            thread_id,
            method=method,
            turn_id=live_turn_id,
            data=data,
        )

    fake.threads[thread_id]["updated_at"] = 3
    fake.threads[thread_id]["turns"] = [
        {
            "id": live_turn_id,
            "status": "completed",
            "items": [{"id": "item-1", "type": "agentMessage", "text": "Observed live."}],
        },
        {
            "id": history_turn_id,
            "status": "completed",
            "items": [{"id": "item-2", "type": "agentMessage", "text": "Created elsewhere."}],
        },
    ]

    snapshot = await service.snapshot_thread(thread_id)
    journal = await service.stream_journal.read(tmp_path, thread_id)
    history_turn_ids = {event["turn_id"] for event in journal.events if event.get("source") == "codex_history" and event.get("turn_id")}

    assert history_turn_ids == {history_turn_id}
    assert [turn["id"] for turn in snapshot["turns"]] == [
        history_turn_id,
        live_turn_id,
    ]
    assert next(turn for turn in snapshot["turns"] if turn["id"] == live_turn_id)["items"][0]["source_ids"] == {
        "codex_stream": "live-agent"
    }


@pytest.mark.asyncio
async def test_partial_snapshot_merges_different_live_and_history_ids_once(
    tmp_path: Path,
) -> None:
    fake = FakeCodex(tmp_path)
    service = service_for(fake, tmp_path)
    turn_id = "turn_history"
    await service.publish_notification(
        "thr_one",
        method="turn/started",
        turn_id=turn_id,
        data={"turn_id": turn_id},
    )
    await service.publish_notification(
        "thr_one",
        method="item/completed",
        turn_id=turn_id,
        data={
            "item": {
                "id": "live-user-id",
                "type": "userMessage",
                "content": [{"type": "text", "text": "Inspect tests"}],
            }
        },
    )
    await service.publish_notification(
        "thr_one",
        method="item/completed",
        turn_id=turn_id,
        data={
            "item": {
                "id": "msg_live_id",
                "type": "agentMessage",
                "text": "The tests are ready.",
            }
        },
    )

    snapshot = await service.snapshot_thread("thr_one")
    turn = next(item for item in snapshot["turns"] if item["id"] == turn_id)
    messages = [item for item in turn["items"] if item["type"] == "agentMessage"]
    users = [item for item in turn["items"] if item["type"] == "userMessage"]

    assert snapshot["journal_coverage"] == "partial"
    assert len(messages) == 1
    assert len(users) == 1
    assert messages[0]["source_ids"] == {
        "codex_stream": "msg_live_id",
        "codex_history": "agent-1",
    }
    assert users[0]["source_ids"] == {
        "codex_stream": "live-user-id",
        "codex_history": "user-1",
    }
    assert any(item["type"] == "commandExecution" for item in turn["items"])


@pytest.mark.asyncio
async def test_scoped_global_duplicate_is_dropped_but_same_channel_repeat_is_kept(
    tmp_path: Path,
) -> None:
    fake = FakeCodex(tmp_path)
    service = service_for(fake, tmp_path)
    payload = {"item_id": "msg-1", "delta": "same token"}

    scoped = await service.publish_notification(
        "thr_one",
        method="item/agentMessage/delta",
        data=payload,
        turn_id="turn-1",
        channel="scoped",
    )
    duplicate_global = await service.publish_notification(
        "thr_one",
        method="item/agentMessage/delta",
        data=payload,
        turn_id="turn-1",
        channel="global",
    )
    repeated_scoped = await service.publish_notification(
        "thr_one",
        method="item/agentMessage/delta",
        data=payload,
        turn_id="turn-1",
        channel="scoped",
    )

    assert scoped is not None
    assert duplicate_global is None
    assert repeated_scoped is not None
    snapshot = await service.stream_journal.read(tmp_path, "thr_one")
    assert [event["type"] for event in snapshot.events].count("agent_message.delta") == 2

    console_user = await service.publish_notification(
        "thr_one",
        method="item/completed",
        data={
            "item": {
                "id": "console-user",
                "type": "userMessage",
                "content": [{"type": "text", "text": "same prompt"}],
            }
        },
        turn_id="turn-1",
        channel="console",
    )
    scoped_user_echo = await service.publish_notification(
        "thr_one",
        method="item/completed",
        data={
            "item": {
                "id": "sdk-user",
                "type": "userMessage",
                "content": [{"type": "text", "text": "same prompt"}],
            }
        },
        turn_id="turn-1",
        channel="scoped",
    )
    assert console_user is not None
    assert scoped_user_echo is None


@pytest.mark.asyncio
async def test_event_arriving_after_durable_read_is_delivered_from_live_queue(
    tmp_path: Path,
) -> None:
    class PausingReadJournal(StreamJournal):
        def __init__(self) -> None:
            super().__init__()
            self.pause_next_read = False
            self.read_captured = asyncio.Event()
            self.resume_read = asyncio.Event()

        async def read(self, project_path: Path, thread_id: str):
            result = await super().read(project_path, thread_id)
            if self.pause_next_read:
                self.pause_next_read = False
                self.read_captured.set()
                await self.resume_read.wait()
            return result

    fake = FakeCodex(tmp_path)
    journal = PausingReadJournal()
    service = service_for(fake, tmp_path, journal)
    baseline = await service.publish_notification(
        "thr_one",
        method="turn/started",
        data={"turn_id": "turn-race"},
        turn_id="turn-race",
    )
    assert baseline is not None

    journal.pause_next_read = True
    subscribe_task = asyncio.create_task(
        service.subscribe_events(
            "thr_one",
            after_sequence=baseline.sequence,
        )
    )
    await journal.read_captured.wait()
    live = await service.publish_notification(
        "thr_one",
        method="item/agentMessage/delta",
        data={"item_id": "msg-race", "delta": "not lost"},
        turn_id="turn-race",
    )
    journal.resume_read.set()
    subscription, replay, cursor, resync = await subscribe_task
    try:
        assert live is not None
        assert replay == []
        assert cursor == baseline.sequence
        assert resync is False
        queued = await asyncio.wait_for(
            service.event_hub.next_event(subscription),
            timeout=0.1,
        )
        assert queued is not None
        assert queued.sequence == live.sequence
        assert queued.sequence > cursor
    finally:
        await service.event_hub.close(subscription)
