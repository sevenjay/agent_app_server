"""Safe, project-scoped filesystem operations for the Files console tab."""

from __future__ import annotations

import errno
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any

from projects import Project


MAX_RELATIVE_PATH_BYTES = 4096
MAX_NAME_BYTES = 255


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

    def list_directory(self, relative_path: str = "") -> dict[str, Any]:
        directory = self._directory(relative_path)
        entries: list[dict[str, Any]] = []
        try:
            with os.scandir(directory) as iterator:
                for directory_entry in iterator:
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
        return {
            "path": _relative_string(directory, self.root),
            "data": entries,
        }

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
