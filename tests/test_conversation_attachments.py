import asyncio
import os
import struct
import time
import zlib
from pathlib import Path

import pytest

from conversation_attachments import AttachmentError, AttachmentNotFound, ConversationAttachmentStore
from projects import Project


def png_bytes() -> bytes:
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\0\xff\0\0"))
        + chunk(b"IEND", b"")
    )


async def chunks(content: bytes):
    for offset in range(0, len(content), 3):
        yield content[offset : offset + 3]


def store_at(path: Path) -> ConversationAttachmentStore:
    path.mkdir(exist_ok=True)
    return ConversationAttachmentStore(Project("project", "Project", path))


@pytest.mark.asyncio
async def test_same_name_roundtrip_atomic_binding_and_project_scope(tmp_path):
    store = store_at(tmp_path / "one")
    first = await store.upload("記錄.log", chunks("第一份".encode()))
    second = await store.upload("記錄.log", chunks(b"second"))
    ids = [first.metadata["id"], second.metadata["id"]]
    assert len(set(ids)) == 2
    with pytest.raises(AttachmentNotFound):
        store_at(tmp_path / "two").validate_pending(ids)
    with pytest.raises(AttachmentNotFound):
        store.bind("thread", [ids[0], "a" * 32])
    assert first.path.exists()
    bound = store.bind("thread", ids)
    assert [item.path.read_text() for item in bound] == ["第一份", "second"]
    with pytest.raises(AttachmentNotFound):
        store.bind("thread", ids)
    with pytest.raises(AttachmentNotFound):
        store.delete_pending(ids[0])
    assert store_at(tmp_path / "one").submitted("thread", ids[0]).path == bound[0].path


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,content",
    [("x.pdf", b"%PDF"), ("x.png", b"fake png"), ("x.jpg", b"fake jpeg"), ("x.log", b"\xff"), ("x.txt", b"a\0b"), ("../x.txt", b"a")],
)
async def test_invalid_uploads_leave_no_payloads(tmp_path, name, content):
    store = store_at(tmp_path)
    with pytest.raises(Exception) as exc:
        await store.upload(name, chunks(content))
    assert getattr(exc.value, "status_code", None) == 400
    assert not list(tmp_path.rglob("payload.*"))


@pytest.mark.asyncio
async def test_actual_byte_limits_and_count_and_total(tmp_path, monkeypatch):
    import conversation_attachments as module

    store = store_at(tmp_path)
    monkeypatch.setattr(module, "MAX_FILE_BYTES", 10)
    monkeypatch.setattr(module, "MAX_TOTAL_BYTES", 25)
    with pytest.raises(AttachmentError):
        await store.upload("x.log", chunks(b"x" * 11))
    items = [await store.upload("x.log", chunks(b"x" * 10)) for _ in range(3)]
    ids = [item.metadata["id"] for item in items]
    with pytest.raises(AttachmentError):
        store.validate_pending(ids)
    with pytest.raises(AttachmentError):
        store.validate_pending([ids[0]] * 2)
    with pytest.raises(AttachmentError):
        store.validate_pending([str(i) * 32 for i in range(6)])


@pytest.mark.asyncio
async def test_symlink_special_file_and_changed_payload_rejected(tmp_path):
    store = store_at(tmp_path)
    item = await store.upload("x.png", chunks(png_bytes()))
    item.path.unlink()
    item.path.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(AttachmentError):
        store.bind("thread", [item.metadata["id"]])
    item.path.unlink()
    os.mkfifo(item.path)
    with pytest.raises(AttachmentError):
        store.validate_pending([item.metadata["id"]])
    item.path.unlink()
    item.path.write_bytes(png_bytes())
    target = store.root / "thread"
    target.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(AttachmentError):
        store.bind("thread", [item.metadata["id"]])
    target.unlink()
    text = await store.upload("x.log", chunks(b"original"))
    text.path.write_bytes(b"modified")
    with pytest.raises(AttachmentError):
        store.validate_pending([text.metadata["id"]])


@pytest.mark.asyncio
async def test_pending_cleanup_skips_active_upload_and_journal_retention(tmp_path):
    from stream_journal import StreamJournal

    store = store_at(tmp_path)
    item = await store.upload("x.log", chunks(b"old"))
    old = time.time() - 2 * 86400
    started = asyncio.Event()
    finish = asyncio.Event()

    async def slow_chunks():
        started.set()
        await finish.wait()
        yield b"new"

    task = asyncio.create_task(store.upload("new.log", slow_chunks()))
    await started.wait()
    os.utime(item.path.parent, (old, old))
    staging = next((store.root / ".attachment-pending").glob(".upload-*"))
    os.utime(staging, (old, old))
    assert await asyncio.to_thread(store.prune_pending) == 1
    assert staging.exists()
    os.utime(store.root / ".attachment-pending", (0, 0))
    assert StreamJournal().prune_retention([tmp_path], days=1) == 0
    finish.set()
    assert (await task).path.read_bytes() == b"new"
