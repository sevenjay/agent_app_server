from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import RootModel

from projects import (
    InvalidProjectNameError,
    Project,
    ProjectAlreadyExistsError,
    ProjectRegistry,
    ProjectRegistryError,
    ProjectRootNotConfiguredError,
    UnknownProjectError,
)


def test_registry_normalizes_paths_and_has_public_view(tmp_path: Path) -> None:
    settings = SimpleNamespace(
        current_env="development",
        codex_projects=[
            {"key": "demo", "name": "Demo", "path": str(tmp_path / ".")}
        ],
    )
    registry = ProjectRegistry.from_settings(settings)

    assert registry.get("demo").path == tmp_path.resolve()
    assert registry.project_for_path(tmp_path) == registry.get("demo")
    assert registry.project_for_path(RootModel[str](tmp_path.as_posix())) == registry.get(
        "demo"
    )
    assert registry.project_for_path(tmp_path / "child") is None
    assert registry.public_view() == [
        {"key": "demo", "name": "Demo", "path": str(tmp_path.resolve())}
    ]


def test_registry_discovers_every_visible_directory_from_root(tmp_path: Path) -> None:
    root = tmp_path / "codex-root"
    root.mkdir()
    (root / "project1").mkdir()
    (root / "Project With Spaces").mkdir()
    (root / ".hidden").mkdir()
    (root / "not-a-project.txt").write_text("file", encoding="utf-8")
    (root / "project-alias").symlink_to(root / "project1", target_is_directory=True)

    registry = ProjectRegistry.from_settings(
        SimpleNamespace(
            current_env="development",
            codex_projects_root=str(root),
        )
    )

    projects = registry.public_view()
    assert {project["name"] for project in projects} == {
        ".hidden",
        "project1",
        "Project With Spaces",
    }
    assert registry.get("project1").path == (root / "project1").resolve()
    generated = next(
        project for project in projects if project["name"] == "Project With Spaces"
    )
    assert generated["key"].startswith("project-")
    assert generated["path"] == str((root / "Project With Spaces").resolve())

    (root / "project2").mkdir()
    assert registry.get("project2").path == (root / "project2").resolve()


def test_hidden_projects_are_excluded_only_from_public_view(tmp_path: Path) -> None:
    root = tmp_path / "codex-root"
    root.mkdir()
    hidden_by_key = root / "private"
    hidden_by_name = root / "Project With Spaces"
    visible = root / "public"
    for project_path in (hidden_by_key, hidden_by_name, visible):
        project_path.mkdir()

    registry = ProjectRegistry.from_settings(
        SimpleNamespace(
            current_env="development",
            codex_projects_root=root,
            codex_hidden_projects=["private", "Project With Spaces"],
        )
    )

    assert registry.public_view() == [
        {
            "key": "public",
            "name": "public",
            "path": str(visible.resolve()),
        }
    ]
    assert registry.get("private").path == hidden_by_key.resolve()
    assert registry.project_for_path(hidden_by_name) is not None


def test_registry_creates_projects_only_inside_configured_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "codex-root"
    root.mkdir()
    registry = ProjectRegistry.from_settings(
        SimpleNamespace(
            current_env="development",
            codex_projects_root=root,
        )
    )

    created = registry.create("new project")

    assert created.name == "new project"
    assert created.path == (root / "new project").resolve()
    assert created.path.is_dir()
    assert registry.get(created.key) == created
    with pytest.raises(ProjectAlreadyExistsError):
        registry.create("new project")
    hidden = registry.create(".hidden")
    assert hidden.path == (root / ".hidden").resolve()
    for invalid_name in ("../escape", "nested/project", "line\nbreak", " "):
        with pytest.raises(InvalidProjectNameError):
            registry.create(invalid_name)

    with pytest.raises(ProjectRootNotConfiguredError):
        ProjectRegistry([]).create("unavailable")


def test_registry_rejects_duplicates_invalid_and_missing_paths(tmp_path: Path) -> None:
    with pytest.raises(ProjectRegistryError):
        ProjectRegistry(
            [
                Project("demo", "One", tmp_path),
                Project("demo", "Two", tmp_path / "two"),
            ]
        )
    with pytest.raises(ProjectRegistryError):
        ProjectRegistry([Project("INVALID KEY", "Bad", tmp_path)])
    with pytest.raises(ProjectRegistryError):
        ProjectRegistry.from_settings(
            SimpleNamespace(
                current_env="development",
                codex_projects=[
                    {"key": "missing", "name": "Missing", "path": tmp_path / "none"}
                ],
            )
        )


def test_production_rejects_empty_registry_and_unknown_keys() -> None:
    registry = ProjectRegistry([])
    with pytest.raises(UnknownProjectError):
        registry.get("missing")
    with pytest.raises(ProjectRegistryError):
        ProjectRegistry.from_settings(
            SimpleNamespace(current_env="production", codex_projects=[])
        )


def test_production_allows_an_empty_configured_root(tmp_path: Path) -> None:
    registry = ProjectRegistry.from_settings(
        SimpleNamespace(
            current_env="production",
            codex_projects_root=tmp_path,
        )
    )

    assert registry.public_view() == []
