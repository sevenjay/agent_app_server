"""Validated, server-owned Codex Project Registry."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from threading import RLock
from typing import Any


PROJECT_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
PROJECT_NAME_MAX_BYTES = 255


class ProjectRegistryError(ValueError):
    """Raised when configured project data is unsafe or malformed."""


class ProjectRootNotConfiguredError(ProjectRegistryError):
    """Raised when project creation is requested for a static registry."""


class ProjectAlreadyExistsError(ProjectRegistryError):
    """Raised when a project directory already exists."""


class InvalidProjectNameError(ProjectRegistryError):
    """Raised when a project name is not one safe directory component."""


class ProjectCreationError(ProjectRegistryError):
    """Raised when the configured root cannot create a project directory."""


class UnknownProjectError(KeyError):
    """Raised when a request references a project outside the registry."""


@dataclass(frozen=True, slots=True)
class Project:
    key: str
    name: str
    path: Path

    def public_view(self) -> dict[str, str]:
        return {
            "key": self.key,
            "name": self.name,
            "path": str(self.path),
        }


def _validate_project_name(value: str) -> str:
    name = value.strip()
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or "\x00" in name
        or any(ord(character) < 32 or ord(character) == 127 for character in name)
        or len(name.encode("utf-8")) > PROJECT_NAME_MAX_BYTES
    ):
        raise InvalidProjectNameError
    return name


def _generated_project_key(name: str, reserved: set[str]) -> str:
    """Return the directory name when safe, otherwise a stable opaque key."""
    if PROJECT_KEY_PATTERN.fullmatch(name) and name not in reserved:
        return name

    digest = sha256(name.encode("utf-8")).hexdigest()
    for length in (16, 24, 32, 48):
        candidate = f"project-{digest[:length]}"
        if candidate not in reserved:
            return candidate
    candidate = f"p-{digest[:62]}"
    if candidate not in reserved:
        return candidate
    raise ProjectRegistryError(f"Could not generate a unique key for project {name!r}")


class ProjectRegistry:
    def __init__(
        self,
        projects: Iterable[Project],
        *,
        root: Path | None = None,
        hidden_projects: Iterable[str] = (),
    ) -> None:
        self._lock = RLock()
        self._root = root
        self._hidden_projects = frozenset(
            identifier
            for value in hidden_projects
            if (identifier := str(value).strip())
        )
        self._by_key: dict[str, Project] = {}
        self._by_path: dict[Path, Project] = {}
        self._replace(projects)

    @property
    def root(self) -> Path | None:
        return self._root

    def _replace(self, projects: Iterable[Project]) -> None:
        by_key: dict[str, Project] = {}
        by_path: dict[Path, Project] = {}
        for project in projects:
            if not PROJECT_KEY_PATTERN.fullmatch(project.key):
                raise ProjectRegistryError(f"Invalid project key: {project.key!r}")
            if project.key in by_key:
                raise ProjectRegistryError(f"Duplicate project key: {project.key!r}")
            if project.path in by_path:
                raise ProjectRegistryError(
                    f"Duplicate project path for keys {by_path[project.path].key!r} "
                    f"and {project.key!r}"
                )
            by_key[project.key] = project
            by_path[project.path] = project
        self._by_key = by_key
        self._by_path = by_path

    @classmethod
    def from_settings(
        cls,
        settings_obj: Any,
        *,
        require_non_empty: bool | None = None,
    ) -> ProjectRegistry:
        raw_hidden_projects = getattr(
            settings_obj,
            "codex_hidden_projects",
            (),
        ) or ()
        if isinstance(raw_hidden_projects, str):
            raw_hidden_projects = (raw_hidden_projects,)

        configured_root = str(
            getattr(settings_obj, "codex_projects_root", "") or ""
        ).strip()
        if configured_root:
            root_candidate = Path(configured_root).expanduser()
            try:
                root = root_candidate.resolve(strict=True)
            except OSError as exc:
                raise ProjectRegistryError(
                    "Configured codex_projects_root does not exist"
                ) from exc
            if not root.is_dir():
                raise ProjectRegistryError(
                    "Configured codex_projects_root is not a directory"
                )
            registry = cls(
                (),
                root=root,
                hidden_projects=raw_hidden_projects,
            )
            registry.refresh()
            return registry

        raw_projects = list(getattr(settings_obj, "codex_projects", ()) or ())
        projects: list[Project] = []
        for raw in raw_projects:
            try:
                key = str(raw["key"]).strip()
                name = str(raw["name"]).strip()
                configured_path = Path(str(raw["path"])).expanduser()
            except (KeyError, TypeError) as exc:
                raise ProjectRegistryError(
                    "Every codex_projects entry requires key, name, and path"
                ) from exc
            if not name:
                raise ProjectRegistryError(f"Project {key!r} has an empty name")
            try:
                path = configured_path.resolve(strict=True)
            except OSError as exc:
                raise ProjectRegistryError(
                    f"Configured path for project {key!r} does not exist"
                ) from exc
            if not path.is_dir():
                raise ProjectRegistryError(
                    f"Configured path for project {key!r} is not a directory"
                )
            projects.append(Project(key=key, name=name, path=path))

        registry = cls(
            projects,
            hidden_projects=raw_hidden_projects,
        )
        if require_non_empty is None:
            require_non_empty = (
                str(getattr(settings_obj, "current_env", "")).lower() == "production"
            )
        if require_non_empty and not registry:
            raise ProjectRegistryError("Production requires at least one Codex project")
        return registry

    def _discover(self) -> list[Project]:
        if self._root is None:
            return list(self._by_key.values())
        try:
            entries = sorted(
                (
                    entry
                    for entry in self._root.iterdir()
                    if entry.is_dir() and not entry.is_symlink()
                ),
                key=lambda entry: (entry.name.casefold(), entry.name),
            )
        except OSError as exc:
            raise ProjectRegistryError(
                "Configured codex_projects_root cannot be read"
            ) from exc

        safe_entries = [
            entry for entry in entries if PROJECT_KEY_PATTERN.fullmatch(entry.name)
        ]
        other_entries = [
            entry for entry in entries if not PROJECT_KEY_PATTERN.fullmatch(entry.name)
        ]
        reserved = {entry.name for entry in safe_entries}
        projects = [
            Project(key=entry.name, name=entry.name, path=entry.resolve())
            for entry in safe_entries
        ]
        for entry in other_entries:
            key = _generated_project_key(entry.name, reserved)
            reserved.add(key)
            projects.append(
                Project(key=key, name=entry.name, path=entry.resolve())
            )
        return sorted(
            projects,
            key=lambda project: (project.name.casefold(), project.name),
        )

    def refresh(self) -> None:
        if self._root is None:
            return
        with self._lock:
            self._replace(self._discover())

    def create(self, name: str) -> Project:
        project_name = _validate_project_name(name)
        if self._root is None:
            raise ProjectRootNotConfiguredError
        target = self._root / project_name
        try:
            target.mkdir(mode=0o755)
        except FileExistsError as exc:
            raise ProjectAlreadyExistsError(project_name) from exc
        except OSError as exc:
            raise ProjectCreationError(project_name) from exc

        with self._lock:
            self._replace(self._discover())
            project = self._by_path.get(target.resolve())
        if project is None:
            raise ProjectCreationError(project_name)
        return project

    def __bool__(self) -> bool:
        self.refresh()
        return bool(self._by_key)

    def __iter__(self):
        self.refresh()
        return iter(tuple(self._by_key.values()))

    def __len__(self) -> int:
        self.refresh()
        return len(self._by_key)

    def get(self, key: str) -> Project:
        self.refresh()
        try:
            return self._by_key[key]
        except KeyError as exc:
            raise UnknownProjectError(key) from exc

    def project_for_path(self, path: Any) -> Project | None:
        path_value = path if isinstance(path, (str, Path)) else getattr(path, "root", path)
        try:
            normalized = Path(path_value).expanduser().resolve(strict=False)
        except (OSError, RuntimeError, TypeError):
            return None
        return self._by_path.get(normalized)

    def public_view(self) -> list[dict[str, str]]:
        self.refresh()
        return [
            project.public_view()
            for project in self._by_key.values()
            if (
                project.key not in self._hidden_projects
                and project.name not in self._hidden_projects
            )
        ]
