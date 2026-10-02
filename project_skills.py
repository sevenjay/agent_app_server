"""Project-local Agent Skills validation, editing, and staged directory imports."""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import os
import re
import shutil
import stat
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from threading import RLock
from typing import Any

import yaml

from project_files import (
    MAX_PREVIEW_BYTES,
    InvalidFilePathError,
    ProjectFileError,
    ProjectFilePermissionError,
    ProjectFileTypeError,
    ProjectFileManager,
    _relative_parts,
    _validate_name,
)
from projects import Project

SKILLS_PATH = ".agents/skills"
NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", re.DOTALL)


class SkillError(ProjectFileError):
    def __init__(self, message: str, *, code: str = "invalid_skill", status_code: int = 400):
        super().__init__(message)
        self.safe_message = message
        self.code = code
        self.status_code = status_code


def _operation(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        try:
            with self.files.lock:
                return method(self, *args, **kwargs)
        except PermissionError as exc:
            raise ProjectFilePermissionError from exc
        except OSError as exc:
            raise ProjectFileError from exc
    return guarded


class _SkillLoader(yaml.SafeLoader):
    """Reject duplicate keys and recursive/aliased metadata before construction."""

    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise yaml.YAMLError("YAML aliases are not supported in skill metadata.")
        if len(self.states) > 32:
            raise yaml.YAMLError("Skill metadata is nested too deeply.")
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):
        keys = [self.construct_object(key, deep=deep) for key, _value in node.value]
        if any(not isinstance(key, str) for key in keys) or len(set(keys)) != len(keys):
            raise yaml.YAMLError("Metadata keys must be unique strings.")
        return super().construct_mapping(node, deep=deep)


def valid_name(name: str) -> bool:
    return isinstance(name, str) and 1 <= len(name) <= 64 and NAME_PATTERN.fullmatch(name) is not None


def validate_skill(source: str, directory: str) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    metadata: dict[str, Any] = {}
    match = FRONTMATTER.match(source)
    if not match:
        errors.append("SKILL.md must start with YAML frontmatter delimited by ---.")
    elif len(match[1].encode("utf-8")) > 64 * 1024:
        errors.append("YAML frontmatter must be at most 64 KiB.")
    else:
        try:
            parsed = yaml.load(match[1], Loader=_SkillLoader)
            if not isinstance(parsed, dict):
                errors.append("YAML frontmatter must be a mapping.")
            else:
                metadata = parsed
        except (yaml.YAMLError, ValueError, RecursionError):
            errors.append("Invalid YAML frontmatter: check syntax, duplicate keys, and aliases.")
    name = metadata.get("name")
    if not valid_name(name):
        errors.append("name must be 1–64 lowercase letters, digits, or single hyphens between words.")
    elif name != directory:
        errors.append("name must match the skill directory name. Use Rename skill to change both.")
    description = metadata.get("description")
    if not isinstance(description, str) or not description.strip() or len(description) > 1024:
        errors.append("description must be a non-empty string of at most 1024 characters.")
    for key in ("license", "allowed-tools"):
        if key in metadata and not isinstance(metadata[key], str):
            errors.append(f"{key} must be a string.")
    if "compatibility" in metadata:
        value = metadata["compatibility"]
        if not isinstance(value, str) or not value.strip() or len(value) > 500:
            errors.append("compatibility must be a non-empty string of at most 500 characters.")
    if "metadata" in metadata:
        value = metadata["metadata"]
        if not isinstance(value, dict) or any(not isinstance(v, str) for v in value.values()):
            errors.append("metadata must map string keys to string values.")
    if len(source.splitlines()) > 500:
        warnings.append("Keep SKILL.md under 500 lines; move detailed material into references/.")
    return {
        "valid": not errors, "errors": errors, "warnings": warnings,
        "name": name if isinstance(name, str) else directory,
        "description": description if isinstance(description, str) else "",
    }


@dataclass(frozen=True)
class SkillLimits:
    max_bytes: int = 25 * 1024 * 1024
    max_files: int = 1000
    import_ttl_seconds: int = 1800

    @property
    def max_request_bytes(self):
        # Directory manifests use base64; bound their JSON overhead as well.
        return self.max_bytes * 2 + self.max_files * 8192


class ProjectSkillManager:
    def __init__(self, project: Project, limits: SkillLimits | None = None):
        self.files = ProjectFileManager(project)
        self.root = self.files.root
        self.limits = limits or SkillLimits()

    def _target(self, directory: str, path: str = "", *, missing: bool = False) -> Path:
        directory = _validate_name(directory, strip=False)
        if path:
            _relative_parts(path, allow_root=False)
        relative = f"{SKILLS_PATH}/{directory}" + (f"/{path}" if path else "")
        return self.files._existing_path(relative, allow_root=False, allow_missing=missing)

    def _ensure_root(self) -> Path:
        root = self.files._existing_path(SKILLS_PATH, allow_missing=True)
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _scan(self, target: Path) -> tuple[list[dict], str]:
        entries: list[dict] = []
        fingerprint = hashlib.sha256()
        total = 0
        for parent, directories, names in os.walk(target, followlinks=False):
            directories.sort()
            for name in sorted([*directories, *names]):
                entry = Path(parent) / name
                path = entry.relative_to(target).as_posix()
                _relative_parts(path, allow_root=False)
                info = entry.lstat()
                if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                    raise InvalidFilePathError
                if len(entries) >= self.limits.max_files:
                    raise SkillError("This skill has too many files or directories.")
                kind = "directory" if stat.S_ISDIR(info.st_mode) else "file"
                entries.append({"path": path, "type": kind, "size": info.st_size if kind == "file" else None})
                fingerprint.update(json.dumps([path, info.st_mode, info.st_mtime_ns, info.st_ino]).encode())
                if kind == "file":
                    total += info.st_size
                    if total > self.limits.max_bytes:
                        raise SkillError("This skill exceeds the configured size limit.")
                    file_hash = hashlib.sha256()
                    with entry.open("rb") as source:
                        for chunk in iter(lambda: source.read(64 * 1024), b""):
                            fingerprint.update(chunk)
                            file_hash.update(chunk)
                    entries[-1]["revision"] = file_hash.hexdigest()
        # Include directory identity, including empty skills and recreation at the same path.
        info = target.lstat()
        fingerprint.update(f"{info.st_ino}:{info.st_mtime_ns}".encode())
        entries.sort(key=lambda entry: entry["path"])
        return entries, fingerprint.hexdigest()

    def _summary(self, target: Path, directory: str) -> dict:
        if not target.is_dir():
            raise ProjectFileTypeError
        entries, revision = self._scan(target)
        skill = target / "SKILL.md"
        try:
            if skill.stat().st_size > MAX_PREVIEW_BYTES:
                raise SkillError("SKILL.md exceeds the 1 MiB editor limit.")
            source = skill.read_text(encoding="utf-8")
            validation = validate_skill(source, directory)
        except (FileNotFoundError, IsADirectoryError):
            validation = {"valid": False, "errors": ["SKILL.md is missing."], "warnings": [], "description": ""}
        except UnicodeError:
            validation = {"valid": False, "errors": ["SKILL.md must be UTF-8 text."], "warnings": [], "description": ""}
        except SkillError as exc:
            validation = {"valid": False, "errors": [exc.safe_message], "warnings": [], "description": ""}
        return {
            **validation, "directory": directory, "path": f"{SKILLS_PATH}/{directory}",
            "revision": revision, "files": entries, "supported": True,
            "file_count": sum(entry["type"] == "file" for entry in entries),
            "size": sum(entry["size"] or 0 for entry in entries),
        }

    @_operation
    def list_skills(self):
        base = self.files._existing_path(SKILLS_PATH, allow_missing=True)
        data = []
        if base.exists():
            if not base.is_dir():
                raise ProjectFileTypeError
            for target in sorted(base.iterdir(), key=lambda item: item.name.casefold()):
                if not target.is_dir() and not target.is_symlink():
                    continue
                try:
                    item = self.detail(target.name)
                except ProjectFileError as exc:
                    item = {
                        "directory": target.name, "path": f"{SKILLS_PATH}/{target.name}", "description": "",
                        "valid": False, "errors": [exc.safe_message], "warnings": [], "supported": False,
                        "file_count": None, "size": None,
                    }
                data.append({key: value for key, value in item.items() if key != "files"})
        return {
            "data": data, "path": SKILLS_PATH,
            "limits": {"max_bytes": self.limits.max_bytes, "max_files": self.limits.max_files, "max_text_bytes": MAX_PREVIEW_BYTES},
        }

    @_operation
    def detail(self, directory: str):
        return self._summary(self._target(directory), directory)

    def revision(self, directory: str):
        target = self._target(directory, missing=True)
        return self._summary(target, directory)["revision"] if target.exists() else "missing"

    def _check_revision(self, directory: str, expected: str):
        if self.revision(directory) != expected:
            raise SkillError("This skill changed since it was opened. Refresh it before trying again.", code="skill_changed", status_code=409)

    @_operation
    def read_file(self, directory: str, path: str):
        target = self._target(directory, path)
        if not target.is_file():
            raise ProjectFileTypeError
        if target.stat().st_size > MAX_PREVIEW_BYTES:
            raise SkillError("The editor supports files up to 1 MiB. Download this file to edit it.")
        raw = target.read_bytes()
        if b"\x00" in raw:
            raise SkillError("This file is binary. Preview or download it instead.")
        try:
            content = raw.decode("utf-8")
        except UnicodeError as exc:
            raise SkillError("The editor supports UTF-8 text files. Download this file to edit it.") from exc
        return {"path": path, "content": content, "revision": hashlib.sha256(raw).hexdigest()}

    @_operation
    def save_file(self, directory: str, path: str, content: bytes, expected: str, *, text_only: bool = True):
        _relative_parts(path, allow_root=False)
        limit = MAX_PREVIEW_BYTES if text_only or path == "SKILL.md" else self.limits.max_bytes
        if len(content) > limit:
            raise SkillError("The file exceeds the configured size limit.", status_code=413)
        target = self._target(directory, path, missing=True)
        if not self._target(directory).is_dir():
            raise ProjectFileTypeError
        exists = target.exists()
        if exists and not target.is_file():
            raise ProjectFileTypeError
        if exists and target.stat().st_size > self.limits.max_bytes:
            raise SkillError("This file exceeds the configured size limit.", status_code=413)
        revision = hashlib.sha256(target.read_bytes()).hexdigest() if exists else "missing"
        if revision != expected:
            raise SkillError("This file changed since it was opened. Reload it before saving; your draft is still available.",
                             code="skill_changed", status_code=409)
        if path == "SKILL.md":
            try:
                result = validate_skill(content.decode("utf-8"), directory)
            except UnicodeError as exc:
                raise SkillError("SKILL.md must be UTF-8 text.") from exc
            if not result["valid"]:
                raise SkillError("\n".join(result["errors"]))
        # Scan before mutation so a malformed tree cannot hide symlinks or exceed limits.
        entries, _revision = self._scan(self._target(directory))
        old_size = target.stat().st_size if exists else 0
        if sum(entry["size"] or 0 for entry in entries) - old_size + len(content) > self.limits.max_bytes:
            raise SkillError("This skill exceeds the configured size limit.", status_code=413)
        if len(entries) + (0 if exists else len(target.relative_to(self._target(directory)).parts)) > self.limits.max_files:
            raise SkillError("This skill has too many files or directories.", status_code=413)
        target.parent.mkdir(parents=True, exist_ok=True)
        parent = target.parent.relative_to(self.root).as_posix()
        self.files.upload_file(parent, target.name, content, overwrite=exists)
        return self.detail(directory)

    @_operation
    def delete_file(self, directory: str, path: str, expected: str):
        _relative_parts(path, allow_root=False)
        if path == "SKILL.md":
            raise SkillError("SKILL.md is required. Use Delete skill to remove the entire skill.")
        self._check_revision(directory, expected)
        self.files.delete(f"{SKILLS_PATH}/{directory}/{path}")
        return self.detail(directory)

    @_operation
    def create(self, name: str, description: str):
        if not valid_name(name):
            raise SkillError("Enter a skill name using 1–64 lowercase letters, digits, and single hyphens.")
        source = "---\n" + yaml.safe_dump({"name": name, "description": description}, sort_keys=False, allow_unicode=True) + "---\n\n# Instructions\n\nDescribe the workflow, examples, and edge cases here.\n"
        validation = validate_skill(source, name)
        if not validation["valid"]:
            raise SkillError("\n".join(validation["errors"]))
        if self._target(name, missing=True).exists():
            raise SkillError("A skill with this name already exists.", code="skill_exists", status_code=409)
        root = self._ensure_root()
        stage = Path(tempfile.mkdtemp(prefix=".skill-install-", dir=root.parent))
        try:
            (stage / "SKILL.md").write_text(source, encoding="utf-8")
            os.rename(stage, root / name)
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        return self.detail(name)

    @_operation
    def rename(self, directory: str, name: str, expected: str):
        if not valid_name(name):
            raise SkillError("Enter a skill name using 1–64 lowercase letters, digits, and single hyphens.")
        self._check_revision(directory, expected)
        target = self._target(name, missing=True)
        if target.exists():
            raise SkillError("A skill with this name already exists.", code="skill_exists", status_code=409)
        current = self._target(directory)
        entry = self.read_file(directory, "SKILL.md")
        source = entry["content"]
        match = FRONTMATTER.match(source)
        if not match:
            raise SkillError("Repair SKILL.md frontmatter before renaming the skill.")
        try:
            # Replace only the scalar value: preserve comments and all other fields.
            node = yaml.compose(match[1], Loader=_SkillLoader)
            value_node = next(value for key, value in node.value if key.value == "name")
            start = match.start(1) + value_node.start_mark.index
            end = match.start(1) + value_node.end_mark.index
            suffix = "\n" if source[start:end].endswith("\n") else ""
            updated = source[:start] + json.dumps(name) + suffix + source[end:]
        except (yaml.YAMLError, AttributeError, StopIteration, TypeError) as exc:
            raise SkillError("Repair the name field in SKILL.md before renaming the skill.") from exc
        result = validate_skill(updated, name)
        if not result["valid"]:
            raise SkillError("\n".join(result["errors"]))
        self.files.upload_file(current.relative_to(self.root).as_posix(), "SKILL.md", updated.encode(), overwrite=True)
        try:
            os.rename(current, target)
        except OSError:
            self.files.upload_file(current.relative_to(self.root).as_posix(), "SKILL.md", source.encode(), overwrite=True)
            raise
        return self.detail(name)

    @_operation
    def delete(self, directory: str, expected: str):
        self._check_revision(directory, expected)
        self.files.delete(f"{SKILLS_PATH}/{directory}")

    @_operation
    def install(self, directory: str, source: Path, expected: str, replace: bool):
        self._check_revision(directory, expected)
        if expected != "missing" and not replace:
            raise SkillError("Confirm replacement of the existing skill.", code="skill_exists", status_code=409)
        root = self._ensure_root()
        target = self._target(directory, missing=True)
        stage = Path(tempfile.mkdtemp(prefix=".skill-install-", dir=root.parent))
        backup = root.parent / f".skill-backup-{uuid.uuid4().hex}"
        moved_old = False
        installed = False
        try:
            shutil.copytree(source, stage, dirs_exist_ok=True)
            validation = self._summary(stage, directory)
            if not validation["valid"]:
                raise SkillError("\n".join(validation["errors"]))
            self._check_revision(directory, expected)
            if target.exists():
                os.rename(target, backup)
                moved_old = True
            try:
                os.rename(stage, target)
                installed = True
            except OSError:
                if moved_old:
                    os.rename(backup, target)
                    moved_old = False
                raise
        finally:
            shutil.rmtree(stage, ignore_errors=True)
            if installed and moved_old:
                shutil.rmtree(backup, ignore_errors=True)
        return self.detail(directory)


@dataclass
class _Import:
    tenant: str
    project_root: Path
    temporary: Path
    source: Path
    directory: str
    revision: str
    expires: float


class SkillImportStore:
    """Private, expiring import previews; tokens are scoped to tenant and project."""

    def __init__(self, limits: SkillLimits | None = None):
        self.limits = limits or SkillLimits()
        self._imports: dict[str, _Import] = {}
        self._lock = RLock()

    def close(self):
        with self._lock:
            for item in self._imports.values():
                shutil.rmtree(item.temporary, ignore_errors=True)
            self._imports.clear()

    def _expire(self):
        for token, item in list(self._imports.items()):
            if item.expires <= time.monotonic():
                shutil.rmtree(item.temporary, ignore_errors=True)
                del self._imports[token]

    def _get(self, token: str, manager: ProjectSkillManager, tenant: str):
        self._expire()
        item = self._imports.get(token)
        if item is None or item.project_root != manager.root or item.tenant != tenant:
            raise SkillError("This import preview expired or was not found. Upload the skill again.", code="skill_import_not_found", status_code=404)
        return item

    def discard(self, token: str, manager: ProjectSkillManager, tenant: str):
        with self._lock:
            item = self._get(token, manager, tenant)
            shutil.rmtree(item.temporary, ignore_errors=True)
            del self._imports[token]

    def commit(self, token: str, manager: ProjectSkillManager, tenant: str, replace: bool):
        with self._lock:
            item = self._get(token, manager, tenant)
            result = manager.install(item.directory, item.source, item.revision, replace)
            self.discard(token, manager, tenant)
            return result

    def prepare(self, manager: ProjectSkillManager, tenant: str, raw: bytes, kind: str):
        with self._lock:
            self._expire()
            if len(self._imports) >= 64 or sum(item.tenant == tenant for item in self._imports.values()) >= 4:
                raise SkillError("Too many pending imports. Cancel an import or wait for it to expire.", status_code=429)
            temporary = Path(tempfile.mkdtemp(prefix="codex-skill-import-"))
            keep = False
            try:
                payload = temporary / "payload"
                payload.mkdir()
                self._extract(payload, raw, kind)
                if (payload / "SKILL.md").is_file():
                    source = payload
                    if (source / "SKILL.md").stat().st_size > MAX_PREVIEW_BYTES:
                        raise SkillError("SKILL.md exceeds the 1 MiB editor limit.")
                    try:
                        match = FRONTMATTER.match((source / "SKILL.md").read_text(encoding="utf-8"))
                        if not match or len(match[1].encode("utf-8")) > 64 * 1024:
                            raise SkillError("SKILL.md needs YAML frontmatter of at most 64 KiB.")
                        metadata = yaml.load(match[1], Loader=_SkillLoader)
                        directory = metadata.get("name")
                    except (yaml.YAMLError, TypeError, AttributeError, UnicodeError) as exc:
                        raise SkillError("SKILL.md needs valid YAML frontmatter with name and description.") from exc
                    if not valid_name(directory):
                        raise SkillError("SKILL.md must declare a valid skill name.")
                else:
                    children = list(payload.iterdir())
                    if len(children) != 1 or not children[0].is_dir() or not (children[0] / "SKILL.md").is_file():
                        raise SkillError("Upload one skill with SKILL.md at its root, or inside one enclosing skill directory.")
                    source = children[0]
                    directory = source.name
                summary = manager._summary(source, directory)
                if not summary["valid"]:
                    return {**summary, "token": None, "exists": False}
                with manager.files.lock:
                    revision = manager.revision(directory)
                token = uuid.uuid4().hex
                self._imports[token] = _Import(tenant, manager.root, temporary, source, directory, revision,
                                              time.monotonic() + self.limits.import_ttl_seconds)
                keep = True
                return {**summary, "token": token, "exists": revision != "missing"}
            finally:
                if not keep:
                    shutil.rmtree(temporary, ignore_errors=True)

    def _extract(self, destination: Path, raw: bytes, kind: str):
        total = 0
        seen: set[str] = set()

        def write(path: str, content: bytes, mode: int = 0o644, directory: bool = False):
            nonlocal total
            parts = _relative_parts(path, allow_root=False)
            if path in seen:
                raise SkillError("The upload contains duplicate paths.")
            seen.add(path)
            if len(seen) > self.limits.max_files:
                raise SkillError("The upload contains too many files or directories.", status_code=413)
            total += len(content)
            if total > self.limits.max_bytes:
                raise SkillError("The unpacked skill exceeds the configured size limit.", status_code=413)
            # Ignore only known OS-generated metadata; validate all paths first.
            if parts[0] == "__MACOSX" or parts[-1] == ".DS_Store":
                return
            target = destination.joinpath(*parts)
            if directory:
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                target.chmod(0o755 if mode & 0o111 else 0o644)

        try:
            if kind == "zip":
                if len(raw) > self.limits.max_bytes:
                    raise SkillError("The ZIP exceeds the configured upload limit.", status_code=413)
                with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                    if len(archive.infolist()) > self.limits.max_files:
                        raise SkillError("The ZIP contains too many entries.", status_code=413)
                    for item in archive.infolist():
                        mode = item.external_attr >> 16
                        file_type = stat.S_IFMT(mode)
                        if file_type not in (0, stat.S_IFREG, stat.S_IFDIR) or item.flag_bits & 1:
                            raise SkillError("ZIP entries must be regular files or directories and must not be encrypted.")
                        if item.file_size + total > self.limits.max_bytes:
                            raise SkillError("The unpacked skill exceeds the configured size limit.", status_code=413)
                        path = item.filename[:-1] if item.is_dir() else item.filename
                        _relative_parts(path, allow_root=False)
                        with archive.open(item) as source:
                            content = source.read(self.limits.max_bytes - total + 1)
                        write(path, content, mode, item.is_dir())
            elif kind == "directory":
                manifest = json.loads(raw)
                files = manifest.get("files") if isinstance(manifest, dict) else None
                if not isinstance(files, list) or not files or len(files) > self.limits.max_files:
                    raise SkillError("Choose one non-empty skill directory within the configured file limit.")
                for item in files:
                    if not isinstance(item, dict) or not isinstance(item.get("content"), str):
                        raise SkillError("Invalid directory upload manifest.")
                    write(item.get("path"), base64.b64decode(item["content"], validate=True))
            else:
                raise SkillError("Choose a skill directory or ZIP file.")
        except ProjectFileError:
            raise
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError, json.JSONDecodeError, binascii.Error, ValueError, OSError) as exc:
            raise SkillError("The upload could not be read. Check the archive and file paths.") from exc
