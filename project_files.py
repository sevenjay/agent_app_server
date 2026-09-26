"""Safe, project-scoped filesystem operations for the Files console tab."""

from __future__ import annotations

import errno
import mimetypes
import os
import re
import shutil
import stat
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from projects import Project


MAX_RELATIVE_PATH_BYTES = 4096
MAX_NAME_BYTES = 255
MAX_PREVIEW_BYTES = 1024 * 1024

PREVIEW_MEDIA_TYPES = {
    "image/png", "image/jpeg", "image/gif", "image/webp", "image/avif", "image/bmp", "image/x-icon",
    "audio/mpeg", "audio/ogg", "audio/wav", "audio/x-wav", "audio/mp4", "audio/webm",
    "video/mp4", "video/webm", "video/ogg", "application/pdf",
}


def _git_status(codes: set[str]) -> str | None:
    if codes & {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}:
        return "conflicted"
    for marker, label in (("D", "deleted"), ("M", "modified"), ("T", "modified"), ("R", "renamed"), ("A", "added"), ("C", "added")):
        if any(marker in code for code in codes):
            return label
    if "??" in codes:
        return "untracked"
    if "!!" in codes:
        return "ignored"
    return None


class ProjectFileError(RuntimeError):
    """An expected filesystem error that is safe to expose at the HTTP boundary."""

    status_code = 500
    code = "file_operation_failed"
    safe_message = "The file operation could not be completed."


class InvalidFilePathError(ProjectFileError):
    status_code = 400
    code = "invalid_file_path"
    safe_message = "The path must stay inside the selected project and must not use symbolic links."


class InvalidFileNameError(ProjectFileError):
    status_code = 400
    code = "invalid_file_name"
    safe_message = "Enter one non-empty file or folder name without slashes or control characters."


class ProjectFileNotFoundError(ProjectFileError):
    status_code = 404
    code = "file_not_found"
    safe_message = "The requested file or folder was not found. Refresh the file list and try again."


class ProjectFileConflictError(ProjectFileError):
    status_code = 409
    code = "file_exists"
    safe_message = "An item with that name already exists in this folder."


class ProjectNotFoundError(ProjectFileError):
    status_code = 404
    code = "project_not_found"
    safe_message = "The selected project was not found."


class ProjectFileTypeError(ProjectFileError):
    status_code = 400
    code = "invalid_file_type"
    safe_message = "The requested item is not the expected file or folder type."


class ProjectFilePermissionError(ProjectFileError):
    status_code = 403
    code = "file_permission_denied"
    safe_message = "The server does not have permission to complete this file operation."


def _validate_name(value: str) -> str:
    name = value.strip()
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or "\x00" in name
        or any(ord(character) < 32 or ord(character) == 127 for character in name)
        or len(name.encode("utf-8")) > MAX_NAME_BYTES
    ):
        raise InvalidFileNameError
    return name


def _relative_parts(value: str, *, allow_root: bool) -> tuple[str, ...]:
    if not isinstance(value, str):
        raise InvalidFilePathError
    if not value:
        if allow_root:
            return ()
        raise InvalidFilePathError
    if (
        value.startswith("/")
        or re.match(r"^[A-Za-z]:", value) is not None
        or "\\" in value
        or "\x00" in value
        or len(value.encode("utf-8")) > MAX_RELATIVE_PATH_BYTES
    ):
        raise InvalidFilePathError
    parts = tuple(value.split("/"))
    if parts[0] == ".stream_journal":
        raise InvalidFilePathError
    if any(
        not part
        or part in {".", ".."}
        or len(part.encode("utf-8")) > MAX_NAME_BYTES
        or any(ord(character) < 32 or ord(character) == 127 for character in part)
        for part in parts
    ):
        raise InvalidFilePathError
    return parts


def _relative_string(path: Path, root: Path) -> str:
    relative = path.relative_to(root)
    return "" if relative == Path(".") else relative.as_posix()


class ProjectFileManager:
    """Manage files beneath one server-authorized Project root."""

    def __init__(self, project: Project) -> None:
        try:
            root = project.path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ProjectFileNotFoundError from exc
        if not root.is_dir():
            raise ProjectFileNotFoundError
        self.root = root

    def _existing_path(self, relative_path: str, *, allow_root: bool = True) -> Path:
        parts = _relative_parts(relative_path, allow_root=allow_root)
        current = self.root
        for part in parts:
            current = current / part
            try:
                metadata = current.lstat()
            except FileNotFoundError as exc:
                raise ProjectFileNotFoundError from exc
            except PermissionError as exc:
                raise ProjectFilePermissionError from exc
            except OSError as exc:
                raise ProjectFileError from exc
            if stat.S_ISLNK(metadata.st_mode):
                raise InvalidFilePathError

        try:
            current.resolve(strict=False).relative_to(self.root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise InvalidFilePathError from exc
        return current

    def _directory(self, relative_path: str) -> Path:
        directory = self._existing_path(relative_path)
        try:
            is_directory = stat.S_ISDIR(directory.lstat().st_mode)
        except PermissionError as exc:
            raise ProjectFilePermissionError from exc
        except OSError as exc:
            raise ProjectFileError from exc
        if not is_directory:
            raise ProjectFileTypeError
        return directory

    def _entry(self, path: Path) -> dict[str, Any]:
        try:
            metadata = path.lstat()
        except FileNotFoundError as exc:
            raise ProjectFileNotFoundError from exc
        except PermissionError as exc:
            raise ProjectFilePermissionError from exc
        except OSError as exc:
            raise ProjectFileError from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise InvalidFilePathError
        if stat.S_ISDIR(metadata.st_mode):
            item_type = "directory"
            size = None
        elif stat.S_ISREG(metadata.st_mode):
            item_type = "file"
            size = metadata.st_size
        else:
            raise ProjectFileTypeError
        return {
            "name": path.name,
            "path": _relative_string(path, self.root),
            "type": item_type,
            "size": size,
            "modified_at": metadata.st_mtime_ns // 1_000_000,
        }

    def list_directory(
        self,
        relative_path: str = "",
        *,
        show_hidden: bool = False,
    ) -> dict[str, Any]:
        directory = self._directory(relative_path)
        entries: list[dict[str, Any]] = []
        try:
            with os.scandir(directory) as iterator:
                for directory_entry in iterator:
                    if directory == self.root and directory_entry.name == ".stream_journal":
                        continue
                    if not show_hidden and directory_entry.name.startswith("."):
                        continue
                    # Symbolic links and special files are deliberately not exposed in the
                    # browser, so they cannot become a path into another filesystem tree.
                    if directory_entry.is_symlink():
                        continue
                    if not (
                        directory_entry.is_dir(follow_symlinks=False)
                        or directory_entry.is_file(follow_symlinks=False)
                    ):
                        continue
                    try:
                        _validate_name(directory_entry.name)
                        entries.append(self._entry(Path(directory_entry.path)))
                    except (InvalidFileNameError, ProjectFileTypeError):
                        continue
        except FileNotFoundError as exc:
            raise ProjectFileNotFoundError from exc
        except PermissionError as exc:
            raise ProjectFilePermissionError from exc
        except OSError as exc:
            raise ProjectFileError from exc

        entries.sort(
            key=lambda item: (
                item["type"] != "directory",
                str(item["name"]).casefold(),
                str(item["name"]),
            )
        )
        git_available, git_codes = self._directory_git_status(directory)
        for entry in entries:
            codes = git_codes.get(entry["name"], git_codes.get("", set()))
            entry["git_status"] = _git_status(codes)
            entry["git_status_code"] = next(iter(codes)) if len(codes) == 1 and entry["type"] == "file" else None
        return {
            "path": _relative_string(directory, self.root),
            "data": entries,
            "git_available": git_available,
        }

    def _directory_git_status(self, directory: Path) -> tuple[bool, dict[str, set[str]]]:
        # Porcelain -z preserves spaces, Unicode and rename source/destination paths.
        # Disable optional index writes and filesystem monitor hooks for this read.
        command = ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", str(directory)]
        environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        try:
            top = subprocess.run(
                [*command, "rev-parse", "--show-toplevel"], capture_output=True, timeout=3, check=True, env=environment,
            )
            repository = Path(os.fsdecode(top.stdout.rstrip(b"\n")))
            result = subprocess.run(
                [*command, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored=matching", "--", "."],
                capture_output=True, timeout=3, check=True, env=environment,
            )
        except (OSError, subprocess.SubprocessError):
            # Git is optional; an unavailable repository must not break file browsing.
            return False, {}

        codes: dict[str, set[str]] = {}

        def record(path: bytes, code: str) -> None:
            target = repository / os.fsdecode(path)
            try:
                relative = target.relative_to(directory)
            except ValueError:
                if code == "!!" and directory.is_relative_to(target):
                    codes.setdefault("", set()).add(code)
                return
            if not relative.parts:
                codes.setdefault("", set()).add(code)
            elif relative.parts[0] != ".stream_journal":
                codes.setdefault(relative.parts[0], set()).add(code)

        records = iter(result.stdout.split(b"\0"))
        for item in records:
            if len(item) < 4:
                continue
            code = item[:2].decode("ascii", errors="replace")
            record(item[3:], code)
            if "R" in code or "C" in code:
                original = next(records, b"")
                if "R" in code:
                    record(original, " D")
        return True, codes

    def preview(self, relative_path: str, *, show_hidden: bool = False) -> dict[str, Any]:
        target = self._existing_path(relative_path)
        entry = self._entry(target)
        if entry["type"] == "directory":
            return {"entry": entry, "kind": "directory", **self.list_directory(relative_path, show_hidden=show_hidden)}
        media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if media_type in PREVIEW_MEDIA_TYPES:
            kind = "pdf" if media_type == "application/pdf" else media_type.split("/")[0]
            return {"entry": entry, "kind": kind, "media_type": media_type}
        try:
            with target.open("rb") as source:
                content = source.read(MAX_PREVIEW_BYTES + 1)
        except PermissionError as exc:
            raise ProjectFilePermissionError from exc
        except FileNotFoundError as exc:
            raise ProjectFileNotFoundError from exc
        except OSError as exc:
            raise ProjectFileError from exc
        truncated = len(content) > MAX_PREVIEW_BYTES
        content = content[:MAX_PREVIEW_BYTES]
        try:
            # A truncated UTF-8 character at the boundary is harmless in a preview.
            text = content.decode("utf-8-sig", errors="replace" if truncated else "strict")
            if any(ord(character) < 32 and character not in "\n\r\t\f" for character in text):
                raise UnicodeError
        except UnicodeError:
            return {"entry": entry, "kind": "binary"}
        return {"entry": entry, "kind": "text", "text": text, "truncated": truncated}

    def preview_content(self, relative_path: str) -> tuple[Path, str]:
        target = self.download_file(relative_path)
        media_type = mimetypes.guess_type(target.name)[0]
        if media_type not in PREVIEW_MEDIA_TYPES:
            raise ProjectFileTypeError
        return target, media_type

    def prepare_download(self, relative_path: str) -> tuple[Path, str, bool]:
        target = self._existing_path(relative_path, allow_root=False)
        entry = self._entry(target)
        if entry["type"] == "file":
            return target, target.name, False
        descriptor, name = tempfile.mkstemp(prefix="codex-download-", suffix=".zip")
        os.close(descriptor)
        archive_path = Path(name)
        try:
            with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                def walk_error(error: OSError) -> None:
                    raise error

                for folder, directories, filenames in os.walk(target, followlinks=False, onerror=walk_error):
                    current = Path(folder)
                    self._directory(_relative_string(current, self.root))
                    directories[:] = [name for name in directories if not (current / name).is_symlink()]
                    archive.write(current, current.relative_to(target.parent).as_posix() + "/")
                    for filename in filenames:
                        candidate = current / filename
                        if not stat.S_ISREG(candidate.lstat().st_mode):
                            continue
                        source = self.download_file(_relative_string(candidate, self.root))
                        archive.write(source, source.relative_to(target.parent).as_posix())
        except Exception as exc:
            archive_path.unlink(missing_ok=True)
            if isinstance(exc, ProjectFileError):
                raise
            if isinstance(exc, PermissionError):
                raise ProjectFilePermissionError from exc
            raise ProjectFileError from exc
        return archive_path, target.name + ".zip", True

    def download_file(self, relative_path: str) -> Path:
        target = self._existing_path(relative_path, allow_root=False)
        try:
            metadata = target.lstat()
        except FileNotFoundError as exc:
            raise ProjectFileNotFoundError from exc
        except PermissionError as exc:
            raise ProjectFilePermissionError from exc
        except OSError as exc:
            raise ProjectFileError from exc
        if not stat.S_ISREG(metadata.st_mode):
            raise ProjectFileTypeError
        return target

    def create_directory(self, parent_path: str, name: str) -> dict[str, Any]:
        parent = self._directory(parent_path)
        target = parent / _validate_name(name)
        if parent == self.root and target.name == ".stream_journal":
            raise InvalidFilePathError
        try:
            target.mkdir(mode=0o755)
        except FileExistsError as exc:
            raise ProjectFileConflictError from exc
        except FileNotFoundError as exc:
            raise ProjectFileNotFoundError from exc
        except PermissionError as exc:
            raise ProjectFilePermissionError from exc
        except OSError as exc:
            raise ProjectFileError from exc
        return self._entry(target)

    def upload_file(
        self,
        parent_path: str,
        name: str,
        content: bytes,
        *,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        parent = self._directory(parent_path)
        target = parent / _validate_name(name)
        if parent == self.root and target.name == ".stream_journal":
            raise InvalidFilePathError
        try:
            existing = target.lstat()
        except FileNotFoundError:
            existing = None
        except PermissionError as exc:
            raise ProjectFilePermissionError from exc
        except OSError as exc:
            raise ProjectFileError from exc
        if existing is not None:
            if stat.S_ISLNK(existing.st_mode):
                raise InvalidFilePathError
            if not stat.S_ISREG(existing.st_mode):
                raise ProjectFileConflictError
            if not overwrite:
                raise ProjectFileConflictError

        descriptor = -1
        temporary_name = ""
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".codex-upload-",
                dir=parent,
            )
            with os.fdopen(descriptor, "wb") as destination:
                descriptor = -1
                destination.write(content)
                destination.flush()
                os.fsync(destination.fileno())
            if overwrite:
                os.replace(temporary_name, target)
                temporary_name = ""
            else:
                try:
                    os.link(temporary_name, target, follow_symlinks=False)
                except FileExistsError as exc:
                    raise ProjectFileConflictError from exc
                os.unlink(temporary_name)
                temporary_name = ""
        except ProjectFileError:
            raise
        except PermissionError as exc:
            raise ProjectFilePermissionError from exc
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                raise ProjectFileConflictError from exc
            raise ProjectFileError from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary_name:
                try:
                    os.unlink(temporary_name)
                except OSError:
                    pass
        return self._entry(target)

    def rename(self, relative_path: str, new_name: str) -> dict[str, Any]:
        source = self._existing_path(relative_path, allow_root=False)
        target = source.parent / _validate_name(new_name)
        if source.parent == self.root and target.name == ".stream_journal":
            raise InvalidFilePathError
        if target == source:
            return self._entry(source)
        try:
            target.lstat()
        except FileNotFoundError:
            pass
        except PermissionError as exc:
            raise ProjectFilePermissionError from exc
        except OSError as exc:
            raise ProjectFileError from exc
        else:
            raise ProjectFileConflictError
        try:
            source.rename(target)
        except FileNotFoundError as exc:
            raise ProjectFileNotFoundError from exc
        except FileExistsError as exc:
            raise ProjectFileConflictError from exc
        except PermissionError as exc:
            raise ProjectFilePermissionError from exc
        except OSError as exc:
            raise ProjectFileError from exc
        return self._entry(target)

    def delete(self, relative_path: str) -> None:
        target = self._existing_path(relative_path, allow_root=False)
        try:
            metadata = target.lstat()
            if stat.S_ISDIR(metadata.st_mode):
                shutil.rmtree(target)
            elif stat.S_ISREG(metadata.st_mode):
                target.unlink()
            else:
                raise ProjectFileTypeError
        except ProjectFileError:
            raise
        except FileNotFoundError as exc:
            raise ProjectFileNotFoundError from exc
        except PermissionError as exc:
            raise ProjectFilePermissionError from exc
        except OSError as exc:
            raise ProjectFileError from exc
