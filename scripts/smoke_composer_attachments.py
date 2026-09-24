"""Opt-in real SDK verification: poetry run python -m scripts.smoke_composer_attachments.

Uses two real model turns and a title-generation turn. Creates an isolated project
inside the repository, then removes its Codex session and files on completion.
Only generated test data is sent. Does not install dependencies.
"""

import asyncio
import json
import secrets
import struct
import tempfile
import zlib
from pathlib import Path

from openai_codex import ApprovalMode, AsyncCodex, Sandbox

from codex_service import CodexService
from conversation_attachments import ConversationAttachmentStore
from event_hub import EventHub
from projects import Project, ProjectRegistry
from turn_manager import TurnManager


def image_fixture() -> bytes:
    # Three red squares on white; no answer in the prompt or filename.
    width, height = 320, 100
    rows = []
    for y in range(height):
        row = bytearray(b"\0")
        for x in range(width):
            red = 25 <= y < 75 and any(left <= x < left + 50 for left in (20, 125, 230))
            row.extend(b"\xe0\x10\x10" if red else b"\xff\xff\xff")
        rows.append(row)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"".join(rows)))
        + chunk(b"IEND", b"")
    )


async def chunks(data: bytes):
    yield data


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix=".attachment-smoke-", dir=Path.cwd()) as directory:
        project = Project("attachment_smoke", "Attachment smoke", Path(directory).resolve())
        store = ConversationAttachmentStore(project)
        async with AsyncCodex() as codex:
            service = CodexService(
                codex,
                registry=ProjectRegistry([project]),
                event_hub=EventHub(),
                turn_manager=TurnManager(),
                approval_mode=ApprovalMode.deny_all,
                sandbox=Sandbox.workspace_write,
                operation_timeout=60,
            )
            thread_id = None
            try:
                for iteration in range(2):
                    token = secrets.token_hex(12)
                    image = await store.upload("sample.png", chunks(image_fixture()))
                    log = await store.upload("sample.log", chunks(f"verification_code={token}\n".encode()))
                    ids = [image.metadata["id"], log.metadata["id"]]
                    prompt = (
                        "Count the red squares in the attached image using vision. Do not use tools to inspect the image. "
                        "Read the attached .log file using a tool to find verification_code. "
                        "Return only JSON with keys red_squares (integer) and verification_code (string)."
                    )
                    if thread_id is None:
                        result = await service.create_thread_from_prompt(
                            project_key=project.key, prompt=prompt, model=None, reasoning_effort="low", attachment_ids=ids
                        )
                        thread_id = result["thread_id"]
                    else:
                        result = await service.start_turn(thread_id, prompt=prompt, model=None, reasoning_effort="low", attachment_ids=ids)
                    async with asyncio.timeout(180):
                        while await service.turn_manager.is_active(thread_id):
                            await asyncio.sleep(0.2)
                    view = await service.read_thread(thread_id)
                    turn = next(turn for turn in view["turns"] if turn["id"] == result["turn_id"])
                    users = [item for item in turn["items"] if item["type"] == "userMessage"]
                    answers = [item["text"] for item in turn["items"] if item["type"] == "agentMessage"]
                    commands = [item for item in turn["items"] if item["type"] == "commandExecution"]
                    print(
                        json.dumps(
                            {
                                "case": "new_session" if iteration == 0 else "new_turn",
                                "status": turn["status"],
                                "users": len(users),
                                "cards": len(users[0].get("attachments", [])) if users else 0,
                                "answers": answers,
                                "commands": [item.get("command") for item in commands],
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                    print(
                        json.dumps(
                            {"tools": [item for item in turn["items"] if item["type"] not in {"userMessage", "agentMessage"}]},
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                    assert len(users) == 1 and len(users[0]["attachments"]) == 2
                    assert all(item["available"] for item in users[0]["attachments"])
                    answer = json.loads(answers[-1].strip().removeprefix("```json").removesuffix("```").strip())
                    assert answer == {"red_squares": 3, "verification_code": token}
                    assert any("payload.log" in str(command.get("command")) for command in commands)
                    persisted = await service.stream_journal.read(project.path, thread_id)
                    assert token not in " ".join(
                        str(event.get("data")) for event in persisted.events if event["type"] == "user_message.completed"
                    )
                print("PASS: vision, file reads, both submission paths, persistent cards, and user-message dedup", flush=True)
            finally:
                await service.turn_manager.shutdown(timeout=5)
                if thread_id:
                    await service.delete_thread(thread_id)


if __name__ == "__main__":
    asyncio.run(main())
