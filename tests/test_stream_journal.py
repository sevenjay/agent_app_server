import json
import os
import stat
import time
from pathlib import Path

import pytest

from stream_journal import (
    OUTPUT_LIMIT_BYTES,
    InvalidThreadId,
    StreamJournal,
    UnsafeJournalPath,
    materialize_timeline,
    normalize_event,
)


def event(
    thread_id: str,
    event_type: str,
    method: str,
    data: dict,
    *,
    turn_id: str | None = None,
):
    return normalize_event(
        thread_id=thread_id,
        event_type=event_type,
        method=method,
        data=data,
        turn_id=turn_id,
    )


@pytest.mark.asyncio
async def test_writer_assigns_durable_sequences_permissions_and_deduplicates(
    tmp_path: Path,
) -> None:
    journal = StreamJournal()
    opened = await journal.ensure_opened(tmp_path, "thr_1")
    started = await journal.append(
        tmp_path,
        "thr_1",
        event(
            "thr_1",
            "codex.notification",
            "turn/started",
            {"turn_id": "turn_1"},
        ),
    )
    duplicate = await journal.append(
        tmp_path,
        "thr_1",
        event(
            "thr_1",
            "codex.notification",
            "turn/started",
            {"turn_id": "turn_1"},
        ),
    )

    assert opened.record["seq"] == 1
    assert started.record["seq"] == 2
    assert duplicate.duplicate is True
    assert duplicate.record["seq"] == 2
    snapshot = await StreamJournal().read(tmp_path, "thr_1")
    assert [item["seq"] for item in snapshot.events] == [1, 2]
    assert snapshot.coverage == "partial"
    assert stat.S_IMODE((tmp_path / ".stream_journal").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / ".stream_journal" / "thr_1").stat().st_mode) == 0o700
    assert stat.S_IMODE(StreamJournal.events_path(tmp_path, "thr_1").stat().st_mode) == 0o600

    with pytest.raises(InvalidThreadId):
        await journal.ensure_opened(tmp_path, "../../escape")


@pytest.mark.asyncio
async def test_writer_refuses_symlinks_in_private_journal_path(tmp_path: Path) -> None:
    root = tmp_path / ".stream_journal"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "thr_link").symlink_to(outside, target_is_directory=True)

    with pytest.raises(UnsafeJournalPath):
        await StreamJournal().ensure_opened(tmp_path, "thr_link")
    assert not (outside / "events.jsonl").exists()


@pytest.mark.asyncio
async def test_partial_history_merge_uses_aliases_and_preserves_unique_command(
    tmp_path: Path,
) -> None:
    journal = StreamJournal()
    thread_id = "thr_merge"
    turn_id = "turn_same"
    await journal.ensure_opened(tmp_path, thread_id)
    for values in (
        event(
            thread_id,
            "codex.notification",
            "turn/started",
            {"turn_id": turn_id},
            turn_id=turn_id,
        ),
        event(
            thread_id,
            "codex.notification",
            "item/completed",
            {
                "item": {
                    "id": "live-user-id",
                    "type": "userMessage",
                    "content": [{"type": "text", "text": "Question"}],
                }
            },
            turn_id=turn_id,
        ),
        event(
            thread_id,
            "codex.notification",
            "item/agentMessage/delta",
            {"item_id": "msg_live", "delta": "Final answer"},
            turn_id=turn_id,
        ),
        event(
            thread_id,
            "codex.notification",
            "item/completed",
            {
                "item": {
                    "id": "exec-live",
                    "type": "commandExecution",
                    "command": "pytest -q",
                    "status": "completed",
                    "exit_code": 0,
                    "aggregated_output": "12 passed",
                }
            },
            turn_id=turn_id,
        ),
    ):
        await journal.append(tmp_path, thread_id, values)

    await journal.append_history(
        tmp_path,
        thread_id,
        {
            "updated_at": 2,
            "turns": [
                {
                    "id": turn_id,
                    "status": "completed",
                    "items": [
                        {
                            "id": "item-1",
                            "type": "userMessage",
                            "content": [{"type": "text", "text": "Question"}],
                        },
                        {
                            "id": "item-2",
                            "type": "agentMessage",
                            "text": "Final answer",
                        },
                    ],
                }
            ],
        },
    )
    snapshot = await journal.read(tmp_path, thread_id)
    turns, aliases = materialize_timeline(thread_id, snapshot)
    assert snapshot.coverage == "partial"
    assert [item["type"] for item in turns[0]["items"]] == [
        "userMessage",
        "agentMessage",
        "commandExecution",
    ]
    assert turns[0]["items"][1]["text"] == "Final answer"
    assert len(aliases) == 2

    await journal.attach_aliases(tmp_path, thread_id, aliases)
    rebuilt, repeated_aliases = materialize_timeline(
        thread_id,
        await journal.read(tmp_path, thread_id),
    )
    assert repeated_aliases == []
    assert rebuilt[0]["status"] == "completed"
    assert rebuilt[0]["items"][1]["source_ids"] == {
        "codex_stream": "msg_live",
        "codex_history": "item-2",
    }


@pytest.mark.asyncio
async def test_live_event_merges_into_an_existing_history_baseline(tmp_path: Path) -> None:
    journal = StreamJournal()
    thread_id = "thr_history_first"
    turn_id = "turn_same"
    await journal.append_history(
        tmp_path,
        thread_id,
        {
            "turns": [
                {
                    "id": turn_id,
                    "status": "completed",
                    "items": [{"id": "history-agent", "type": "agentMessage", "text": "Same answer"}],
                }
            ]
        },
    )
    await journal.append(
        tmp_path,
        thread_id,
        event(
            thread_id,
            "codex.notification",
            "item/completed",
            {"item": {"id": "live-agent", "type": "agentMessage", "text": "Same answer"}},
            turn_id=turn_id,
        ),
    )

    turns, aliases = materialize_timeline(thread_id, await journal.read(tmp_path, thread_id))

    assert len(turns[0]["items"]) == 1
    assert turns[0]["items"][0]["source_ids"] == {
        "codex_history": "history-agent",
        "codex_stream": "live-agent",
    }
    assert len(aliases) == 1
    assert aliases[0]["turn_id"] == turn_id
    assert aliases[0]["target_event_id"]
    assert aliases[0]["source"] == "codex_stream"
    assert aliases[0]["source_id"] == "live-agent"


@pytest.mark.asyncio
async def test_message_alias_requires_unique_exact_text(tmp_path: Path) -> None:
    journal = StreamJournal()
    thread_id = "thr_message_mismatch"
    turn_id = "turn_same"
    await journal.append(
        tmp_path,
        thread_id,
        event(
            thread_id,
            "codex.notification",
            "item/completed",
            {"item": {"id": "live-agent", "type": "agentMessage", "text": "Live answer"}},
            turn_id=turn_id,
        ),
    )
    await journal.append_history(
        tmp_path,
        thread_id,
        {
            "turns": [
                {
                    "id": turn_id,
                    "status": "completed",
                    "items": [
                        {
                            "id": "history-agent",
                            "type": "agentMessage",
                            "text": "Different history answer",
                        }
                    ],
                }
            ]
        },
    )

    turns, aliases = materialize_timeline(
        thread_id,
        await journal.read(tmp_path, thread_id),
    )

    assert aliases == []
    assert turns[0]["status"] == "completed"
    assert [item["text"] for item in turns[0]["items"]] == [
        "Live answer",
        "Different history answer",
    ]
    assert all(item["unresolved"] is True for item in turns[0]["items"])
    assert turns[0]["items"][0]["source_ids"] == {"codex_stream": "live-agent"}
    assert turns[0]["items"][1]["source_ids"] == {"codex_history": "history-agent"}


@pytest.mark.asyncio
async def test_message_alias_rejects_ambiguous_exact_text(tmp_path: Path) -> None:
    journal = StreamJournal()
    thread_id = "thr_message_ambiguous"
    turn_id = "turn_same"
    for item_id in ("live-agent-1", "live-agent-2"):
        await journal.append(
            tmp_path,
            thread_id,
            event(
                thread_id,
                "codex.notification",
                "item/completed",
                {"item": {"id": item_id, "type": "agentMessage", "text": "Repeated answer"}},
                turn_id=turn_id,
            ),
        )
    await journal.append_history(
        tmp_path,
        thread_id,
        {
            "turns": [
                {
                    "id": turn_id,
                    "status": "completed",
                    "items": [
                        {
                            "id": "history-agent",
                            "type": "agentMessage",
                            "text": "Repeated answer",
                        }
                    ],
                }
            ]
        },
    )

    turns, aliases = materialize_timeline(
        thread_id,
        await journal.read(tmp_path, thread_id),
    )

    assert aliases == []
    assert len(turns[0]["items"]) == 3
    assert all(item["unresolved"] is True for item in turns[0]["items"])


@pytest.mark.asyncio
async def test_message_started_notifications_do_not_create_blank_message_cards(
    tmp_path: Path,
) -> None:
    journal = StreamJournal()
    thread_id = "thr_message_started"
    turn_id = "turn_message_started"

    for sdk_type in ("userMessage", "agentMessage"):
        assert (
            event(
                thread_id,
                "codex.notification",
                "item/started",
                {"item": {"id": f"new-{sdk_type}", "type": sdk_type}},
                turn_id=turn_id,
            )
            is None
        )

    await journal.ensure_opened(tmp_path, thread_id)
    for values in (
        event(
            thread_id,
            "codex.notification",
            "item/completed",
            {
                "item": {
                    "id": "console-user",
                    "type": "userMessage",
                    "content": [{"type": "text", "text": "hi"}],
                }
            },
            turn_id=turn_id,
        ),
        event(
            thread_id,
            "codex.notification",
            "item/completed",
            {
                "item": {
                    "id": "agent-message",
                    "type": "agentMessage",
                    "text": "Hi! How can I help?",
                }
            },
            turn_id=turn_id,
        ),
    ):
        await journal.append(tmp_path, thread_id, values)

    await journal.append_history(
        tmp_path,
        thread_id,
        {
            "turns": [
                {
                    "id": turn_id,
                    "status": "completed",
                    "items": [
                        {
                            "id": "item-1",
                            "type": "userMessage",
                            "content": [{"type": "text", "text": "hi"}],
                        },
                        {
                            "id": "item-2",
                            "type": "agentMessage",
                            "text": "Hi! How can I help?",
                        },
                    ],
                }
            ]
        },
    )

    turns, _aliases = materialize_timeline(
        thread_id,
        await journal.read(tmp_path, thread_id),
    )

    assert [item["type"] for item in turns[0]["items"]] == [
        "userMessage",
        "agentMessage",
    ]
    assert turns[0]["items"][0]["content"] == [{"type": "text", "text": "hi"}]
    assert turns[0]["items"][1]["text"] == "Hi! How can I help?"


@pytest.mark.asyncio
async def test_reader_degrades_for_an_incomplete_tail_and_recovers_on_append(
    tmp_path: Path,
) -> None:
    journal = StreamJournal()
    await journal.ensure_opened(tmp_path, "thr_crash")
    path = journal.events_path(tmp_path, "thr_crash")
    with path.open("ab") as handle:
        handle.write(b'{"v":1,"seq":2')

    damaged = await StreamJournal().read(tmp_path, "thr_crash")
    assert damaged.damaged is True
    assert damaged.coverage == "partial"
    assert damaged.cursor == 1

    restarted = StreamJournal()
    appended = await restarted.append(
        tmp_path,
        "thr_crash",
        event(
            "thr_crash",
            "codex.notification",
            "turn/started",
            {"turn_id": "turn_after_crash"},
        ),
    )
    assert appended.record["seq"] == 2
    recovered = await StreamJournal().read(tmp_path, "thr_crash")
    assert recovered.damaged is False
    assert recovered.cursor == 2


@pytest.mark.asyncio
async def test_reader_marks_middle_corruption_partial_without_losing_later_events(
    tmp_path: Path,
) -> None:
    journal = StreamJournal()
    await journal.ensure_opened(tmp_path, "thr_middle")
    path = journal.events_path(tmp_path, "thr_middle")
    with path.open("ab") as handle:
        handle.write(b"not-json\n")
    await StreamJournal().append(
        tmp_path,
        "thr_middle",
        event(
            "thr_middle",
            "codex.notification",
            "turn/started",
            {"turn_id": "turn_after_damage"},
        ),
    )

    damaged = await StreamJournal().read(tmp_path, "thr_middle")

    assert damaged.damaged is True
    assert damaged.coverage == "partial"
    assert damaged.cursor == 2
    assert [record["seq"] for record in damaged.events] == [1, 2]


def test_command_output_is_redacted_bounded_and_carries_truncation_metadata() -> None:
    raw_output = "A" * (OUTPUT_LIMIT_BYTES + 10_000) + " password=do-not-store"
    normalized = event(
        "thr_safe",
        "codex.notification",
        "item/completed",
        {
            "item": {
                "id": "exec-1",
                "type": "commandExecution",
                "command": "curl -H 'Authorization: Bearer secret-token' example.test",
                "status": "completed",
                "exit_code": 1,
                "aggregated_output": raw_output,
                "cwd": "/private/path",
                "process_id": 123,
            }
        },
        turn_id="turn_1",
    )
    item = normalized["data"]["item"]

    assert "secret-token" not in json.dumps(item)
    assert "do-not-store" not in json.dumps(item)
    assert "cwd" not in item
    assert "process_id" not in item
    assert item["output_byte_count"] == len(raw_output.encode("utf-8"))
    assert item["output_truncated"] is True
    assert len(item["aggregated_output"].encode("utf-8")) <= OUTPUT_LIMIT_BYTES

    structured = event(
        "thr_safe",
        "codex.notification",
        "item/completed",
        {
            "item": {
                "id": "tool-1",
                "type": "mcpToolCall",
                "result": {"content": "B" * (OUTPUT_LIMIT_BYTES + 1)},
            }
        },
        turn_id="turn_1",
    )["data"]["item"]
    assert structured["output_truncated"] is True
    assert structured["output_byte_count"] > OUTPUT_LIMIT_BYTES
    assert len(structured["result"].encode("utf-8")) <= OUTPUT_LIMIT_BYTES


def test_reasoning_and_hook_payloads_are_not_normalized_for_persistence() -> None:
    assert (
        normalize_event(
            thread_id="thr_private",
            event_type="codex.notification",
            method="item/completed",
            data={"item": {"id": "reason-1", "type": "reasoning", "text": "private"}},
            turn_id="turn-1",
        )
        is None
    )
    assert (
        normalize_event(
            thread_id="thr_private",
            event_type="codex.notification",
            method="unknown/privatePayload",
            data={"raw": "must not be persisted"},
            turn_id="turn-1",
        )
        is None
    )


def test_agent_checkpoint_and_goal_events_use_allowlisted_payloads() -> None:
    checkpoint = normalize_event(
        thread_id="thr_safe",
        event_type="codex.notification",
        method="item/agentMessage/checkpoint",
        data={"item_id": "msg-1", "text": "Answer so far"},
        turn_id="turn-1",
    )
    goal = normalize_event(
        thread_id="thr_safe",
        event_type="codex.notification",
        method="thread/goal/updated",
        data={
            "thread_id": "thr_safe",
            "goal": {
                "objective": "Finish safely",
                "status": "active",
                "private_sdk_field": "must not be persisted",
            },
            "raw": "must not be persisted",
        },
        turn_id="turn-1",
    )

    assert checkpoint is not None
    assert checkpoint["type"] == "agent_message.checkpoint"
    assert checkpoint["data"]["text"] == "Answer so far"
    assert goal is not None
    assert goal["data"] == {
        "thread_id": "thr_safe",
        "goal": {"objective": "Finish safely", "status": "active"},
    }
    assert (
        normalize_event(
            thread_id="thr_private",
            event_type="codex.notification",
            method="item/hookPrompt/delta",
            data={"delta": "private hook"},
            turn_id="turn-1",
        )
        is None
    )


@pytest.mark.asyncio
async def test_delete_moves_to_recoverable_trash_and_retention_prunes_old_data(
    tmp_path: Path,
) -> None:
    journal = StreamJournal()
    await journal.ensure_opened(tmp_path, "thr_delete")
    trashed = await journal.trash_thread(tmp_path, "thr_delete")
    assert trashed is not None and trashed.exists()
    assert not journal.thread_directory(tmp_path, "thr_delete").exists()

    old = time.time() - (40 * 24 * 60 * 60)
    os.utime(trashed, (old, old))
    assert journal.prune_retention([tmp_path], days=30) == 1
    assert not trashed.exists()
