"""Private, project-scoped composer attachments; no browser-supplied paths."""

from __future__ import annotations

import codecs
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from starlette.concurrency import run_in_threadpool

from project_files import ProjectFileError, _validate_name
from projects import Project

MAX_ATTACHMENTS = 5
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 25 * 1024 * 1024
PENDING_TTL_SECONDS = 24 * 60 * 60
ATTACHMENT_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")
THREAD_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
REFERENCE_MARKER = "\n\n[Console attachment file references]\n"
MIMES = {".png": "image/png", ".jpg": "image/jpeg", ".txt": "text/plain", ".md": "text/plain", ".log": "text/plain"}


class AttachmentError(ProjectFileError):
    status_code = 400
    code = "invalid_attachment"
    safe_message = "Use PNG, JPEG, or UTF-8 .txt, .md, .log files (5 files, 10 MiB each, 25 MiB total)."


class AttachmentNotFound(AttachmentError):
    status_code = 404
    code = "attachment_unavailable"
    safe_message = "An attachment is unavailable or already submitted. Remove it and select the file again."


@dataclass(frozen=True)
class Attachment:
    metadata: dict[str, Any]
    path: Path

    def public(self) -> dict[str, Any]:
        return {key: self.metadata[key] for key in ("id", "name", "mime", "size")}

    def message(self) -> dict[str, Any]:
        return {**self.public(), "delivery": "vision" if self.metadata["mime"].startswith("image/") else "file_reference"}


def safe_attachment_metadata(value: Any) -> list[dict[str, Any]]:
    """Journal/UI allowlist; never retain content, blob URLs, or filesystem paths."""
    result = []
    for item in value[:MAX_ATTACHMENTS] if isinstance(value, list) else ():
        if not isinstance(item, dict) or not ATTACHMENT_ID_PATTERN.fullmatch(str(item.get("id", ""))):
            continue
        if item.get("mime") not in MIMES.values() or item.get("delivery") not in {"vision", "file_reference"}:
            continue
        try:
            name = _validate_name(item.get("name", ""))
        except (ProjectFileError, TypeError, AttributeError):
            continue
        size = item.get("size")
        if isinstance(size, int) and not isinstance(size, bool) and 0 <= size <= MAX_FILE_BYTES:
            result.append({"id": item["id"], "name": name, "mime": item["mime"], "size": size, "delivery": item["delivery"]})
    return result


def message_attachment_fields(item: dict[str, Any]) -> dict[str, Any]:
    attachments = safe_attachment_metadata(item.get("attachments"))
    content = item.get("content") or []
    unknown = any(
        isinstance(part, dict) and (part.get("root", part)).get("type") in {"image", "localImage", "local_image"}
        for part in content
        if isinstance(content, list)
    )
    unknown = unknown or any(
        isinstance(part, dict) and REFERENCE_MARKER in str(part.get("root", part).get("text", ""))
        for part in content
        if isinstance(content, list)
    )
    return {
        "attachments": attachments,
        "attachments_unavailable": bool(item.get("attachments_unavailable") or (unknown and not attachments)),
    }


@contextmanager
def _directory(parent: int, name: str, *, create: bool = False) -> Iterator[int]:
    if create:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent)
        except FileExistsError:
            pass
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        yield fd
    finally:
        os.close(fd)


def _read_regular(directory: int, name: str):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise AttachmentError
    return os.fdopen(fd, "rb")


def _write_metadata(directory: int, metadata: dict[str, Any]) -> None:
    name = f".metadata-{uuid.uuid4().hex}"
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        json.dump(metadata, output, ensure_ascii=False)
        output.flush()
        os.fsync(output.fileno())
    os.replace(name, "metadata.json", src_dir_fd=directory, dst_dir_fd=directory)


def _validate_payload(file, extension: str) -> tuple[int, str]:
    decoder = codecs.getincrementaldecoder("utf-8")() if extension in {".txt", ".md", ".log"} else None
    digest = hashlib.sha256()
    size = 0
    prefix = b""
    tail = b""
    try:
        while chunk := file.read(64 * 1024):
            size += len(chunk)
            if size > MAX_FILE_BYTES:
                raise AttachmentError
            prefix = (prefix + chunk)[:24]
            tail = (tail + chunk)[-2:]
            digest.update(chunk)
            if decoder:
                if b"\x00" in chunk:
                    raise AttachmentError
                decoder.decode(chunk)
        if decoder:
            decoder.decode(b"", final=True)
        elif extension == ".png":
            if not (prefix.startswith(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR") and len(prefix) == 24 and size >= 45):
                raise AttachmentError
        elif extension == ".jpg":
            if not prefix.startswith(b"\xff\xd8\xff") or tail != b"\xff\xd9":
                raise AttachmentError
        else:
            raise AttachmentError
    except UnicodeDecodeError as exc:
        raise AttachmentError from exc
    return size, digest.hexdigest()


class ConversationAttachmentStore:
    """Directory descriptors and no-follow opens protect every managed path component.

    Short root locks serialize bind/delete/cleanup even across server workers.
    Uploads hold a separate staging-directory lock so cleanup cannot remove them.
    """

    def __init__(self, project: Project) -> None:
        self.project = project
        self.root = project.path.resolve() / ".stream_journal"

    @contextmanager
    def _root(self) -> Iterator[int]:
        try:
            with (
                _directory(-1, str(self.project.path.resolve())) as project_fd,
                _directory(project_fd, ".stream_journal", create=True) as root,
            ):
                fcntl.flock(root, fcntl.LOCK_EX)
                try:
                    yield root
                finally:
                    fcntl.flock(root, fcntl.LOCK_UN)
        except FileNotFoundError as exc:
            raise AttachmentNotFound from exc
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise AttachmentError from exc

    def _begin(self, name: str) -> tuple[str, str, int, int]:
        name = _validate_name(name)
        extension = Path(name).suffix.lower()
        if extension == ".jpeg":
            extension = ".jpg"
        if extension not in MIMES:
            raise AttachmentError
        attachment_id = uuid.uuid4().hex
        with self._root() as root, _directory(root, ".attachment-pending", create=True) as pending:
            os.mkdir(f".upload-{attachment_id}", mode=0o700, dir_fd=pending)
            staging = os.open(f".upload-{attachment_id}", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=pending)
            fcntl.flock(staging, fcntl.LOCK_EX)
            try:
                fd = os.open(f"payload{extension}", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=staging)
            except BaseException:
                os.close(staging)
                shutil.rmtree(f".upload-{attachment_id}", dir_fd=pending)
                raise
        return attachment_id, extension, staging, fd

    def _finish_upload(self, attachment_id: str, name: str, extension: str, staging: int, fd: int) -> Attachment:
        os.fsync(fd)
        with _read_regular(staging, f"payload{extension}") as source:
            size, digest = _validate_payload(source, extension)
        metadata = {
            "id": attachment_id,
            "name": _validate_name(name),
            "mime": MIMES[extension],
            "size": size,
            "extension": extension,
            "sha256": digest,
            "created_at": time.time(),
            "state": "pending",
            "project": str(self.project.path.resolve()),
        }
        _write_metadata(staging, metadata)
        with self._root() as root, _directory(root, ".attachment-pending") as pending:
            os.rename(f".upload-{attachment_id}", attachment_id, src_dir_fd=pending, dst_dir_fd=pending)
        return Attachment(metadata, self.root / ".attachment-pending" / attachment_id / f"payload{extension}")

    def _close_upload(self, attachment_id: str, staging: int, fd: int) -> None:
        os.close(fd)
        os.close(staging)
        with self._root() as root, _directory(root, ".attachment-pending") as pending:
            try:
                shutil.rmtree(f".upload-{attachment_id}", dir_fd=pending)
            except FileNotFoundError:
                pass

    async def upload(self, name: str, chunks: AsyncIterator[bytes]) -> Attachment:
        await run_in_threadpool(self.prune_pending)
        attachment_id, extension, staging, fd = await run_in_threadpool(self._begin, name)
        try:
            size = 0
            async for chunk in chunks:
                size += len(chunk)
                if size > MAX_FILE_BYTES:
                    raise AttachmentError
                await run_in_threadpool(self._write_chunk, fd, chunk)
            return await run_in_threadpool(self._finish_upload, attachment_id, name, extension, staging, fd)
        finally:
            await run_in_threadpool(self._close_upload, attachment_id, staging, fd)

    @staticmethod
    def _write_chunk(fd: int, chunk: bytes) -> None:
        remaining = memoryview(chunk)
        while remaining:
            remaining = remaining[os.write(fd, remaining) :]

    def _load(self, parent: int, attachment_id: str, *, thread_id: str | None = None) -> Attachment:
        if not ATTACHMENT_ID_PATTERN.fullmatch(attachment_id):
            raise AttachmentError
        with _directory(parent, attachment_id) as directory:
            with _read_regular(directory, "metadata.json") as source:
                metadata = json.loads(source.read(8192))
            if (
                metadata.get("id") != attachment_id
                or metadata.get("project") != str(self.project.path.resolve())
                or metadata.get("state") != ("submitted" if thread_id else "pending")
                or metadata.get("thread_id") != thread_id
                or (not thread_id and metadata.get("created_at", 0) < time.time() - PENDING_TTL_SECONDS)
            ):
                raise AttachmentNotFound
            extension = metadata.get("extension")
            if extension not in MIMES or metadata.get("mime") != MIMES[extension]:
                raise AttachmentError
            _validate_name(metadata["name"])
            with _read_regular(directory, f"payload{extension}") as source:
                size, digest = _validate_payload(source, extension)
            if size != metadata.get("size") or digest != metadata.get("sha256"):
                raise AttachmentError
        parent_path = self.root / thread_id / "attachments" if thread_id else self.root / ".attachment-pending"
        return Attachment(metadata, parent_path / attachment_id / f"payload{extension}")

    def _validate(self, pending: int, ids: list[str]) -> list[Attachment]:
        if len(ids) > MAX_ATTACHMENTS or len(set(ids)) != len(ids):
            raise AttachmentError
        attachments = [self._load(pending, attachment_id) for attachment_id in ids]
        if sum(item.metadata["size"] for item in attachments) > MAX_TOTAL_BYTES:
            raise AttachmentError
        return attachments

    def validate_pending(self, ids: list[str]) -> list[Attachment]:
        if not ids:
            return []
        with self._root() as root, _directory(root, ".attachment-pending") as pending:
            return self._validate(pending, ids)

    def bind(self, thread_id: str, ids: list[str]) -> list[Attachment]:
        if not ids:
            return []
        if not THREAD_ID_PATTERN.fullmatch(thread_id):
            raise AttachmentError
        with self._root() as root, _directory(root, ".attachment-pending") as pending:
            attachments = self._validate(pending, ids)
            with _directory(root, thread_id, create=True) as thread, _directory(thread, "attachments", create=True) as target:
                moved: list[Attachment] = []
                try:
                    for item in attachments:
                        attachment_id = item.metadata["id"]
                        # Never overwrite a destination, including an empty directory.
                        if attachment_id in os.listdir(target):
                            raise AttachmentError
                        os.rename(attachment_id, attachment_id, src_dir_fd=pending, dst_dir_fd=target)
                        moved.append(item)
                        with _directory(target, attachment_id) as directory:
                            _write_metadata(directory, {**item.metadata, "state": "submitted", "thread_id": thread_id})
                    return [self._load(target, attachment_id, thread_id=thread_id) for attachment_id in ids]
                except BaseException:
                    for item in moved:
                        attachment_id = item.metadata["id"]
                        with _directory(target, attachment_id) as directory:
                            _write_metadata(directory, item.metadata)
                        os.rename(attachment_id, attachment_id, src_dir_fd=target, dst_dir_fd=pending)
                    raise

    def restore_pending(self, thread_id: str, ids: list[str]) -> None:
        if not THREAD_ID_PATTERN.fullmatch(thread_id):
            raise AttachmentError
        with (
            self._root() as root,
            _directory(root, thread_id) as thread,
            _directory(thread, "attachments") as target,
            _directory(root, ".attachment-pending", create=True) as pending,
        ):
            for attachment_id in ids:
                item = self._load(target, attachment_id, thread_id=thread_id)
                metadata = {**item.metadata, "state": "pending"}
                metadata.pop("thread_id", None)
                with _directory(target, attachment_id) as directory:
                    _write_metadata(directory, metadata)
                os.rename(attachment_id, attachment_id, src_dir_fd=target, dst_dir_fd=pending)

    def delete_pending(self, attachment_id: str) -> None:
        with self._root() as root, _directory(root, ".attachment-pending") as pending:
            self._load(pending, attachment_id)
            shutil.rmtree(attachment_id, dir_fd=pending)

    def submitted(self, thread_id: str, attachment_id: str) -> Attachment:
        if not THREAD_ID_PATTERN.fullmatch(thread_id):
            raise AttachmentError
        with self._root() as root, _directory(root, thread_id) as thread, _directory(thread, "attachments") as attachments:
            return self._load(attachments, attachment_id, thread_id=thread_id)

    def open_submitted(self, thread_id: str, attachment_id: str):
        if not THREAD_ID_PATTERN.fullmatch(thread_id):
            raise AttachmentError
        with self._root() as root, _directory(root, thread_id) as thread, _directory(thread, "attachments") as attachments:
            item = self._load(attachments, attachment_id, thread_id=thread_id)
            with _directory(attachments, attachment_id) as directory:
                source = _read_regular(directory, item.path.name)
            return item, source

    def decorate_timeline(self, thread_id: str, turns: list[dict[str, Any]]) -> None:
        for turn in turns:
            for item in turn.get("items", []):
                for attachment in item.get("attachments", []):
                    try:
                        stored = self.submitted(thread_id, attachment["id"])
                        attachment["available"] = stored.message() == {key: attachment[key] for key in stored.message()}
                    except ProjectFileError:
                        attachment["available"] = False

    def prune_pending(self) -> int:
        removed = 0
        if not self.root.exists():
            return removed
        with self._root() as root, _directory(root, ".attachment-pending", create=True) as pending:
            for name in os.listdir(pending):
                if not ATTACHMENT_ID_PATTERN.fullmatch(name.removeprefix(".upload-")):
                    continue
                try:
                    with _directory(pending, name) as directory:
                        fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        if os.fstat(directory).st_mtime >= time.time() - PENDING_TTL_SECONDS:
                            continue
                        shutil.rmtree(name, dir_fd=pending)
                        removed += 1
                except OSError:
                    continue
        return removed
