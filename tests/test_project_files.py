from pathlib import Path

import pytest

from main import create_app
from projects import Project, ProjectRegistry
from tests.fakes import FakeCodex
from tests.http_client import application_client


def file_application(project_path: Path):
    fake = FakeCodex(project_path)
    registry = ProjectRegistry(
        [Project("files_project", "Files Project", project_path.resolve())]
    )
    return create_app(
        codex_client_factory=lambda: fake,
        codex_enabled=True,
        registry=registry,
    )


@pytest.mark.asyncio
async def test_project_files_list_directories_before_sorted_files(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "zeta").mkdir()
    (project / "Alpha").mkdir()
    (project / "beta.txt").write_text("beta", encoding="utf-8")
    (project / "aardvark.txt").write_text("aardvark", encoding="utf-8")
    (project / "Alpha" / "nested.txt").write_text("nested", encoding="utf-8")
    application = file_application(project)

    async with application_client(application) as client:
        root = await client.get("/api/projects/files_project/files")
        nested = await client.get(
            "/api/projects/files_project/files",
            params={"path": "Alpha"},
        )

    assert root.status_code == 200
    assert [(item["type"], item["name"]) for item in root.json()["data"]] == [
        ("directory", "Alpha"),
        ("directory", "zeta"),
        ("file", "aardvark.txt"),
        ("file", "beta.txt"),
    ]
    assert root.json()["path"] == ""
    assert "nested.txt" not in {item["name"] for item in root.json()["data"]}
    assert nested.json()["path"] == "Alpha"
    assert nested.json()["data"][0]["path"] == "Alpha/nested.txt"


@pytest.mark.asyncio
async def test_project_file_crud_and_upload_overwrite_confirmation(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    application = file_application(project)

    async with application_client(application) as client:
        created = await client.post(
            "/api/projects/files_project/files/directories",
            json={"path": "", "name": "notes"},
        )
        uploaded = await client.post(
            "/api/projects/files_project/files/upload",
            params={"path": "notes", "name": "hello.txt"},
            content=b"first",
            headers={"Content-Type": "text/plain"},
        )
        duplicate = await client.post(
            "/api/projects/files_project/files/upload",
            params={"path": "notes", "name": "hello.txt"},
            content=b"second",
        )
        content_after_conflict = (project / "notes" / "hello.txt").read_bytes()
        overwritten = await client.post(
            "/api/projects/files_project/files/upload",
            params={"path": "notes", "name": "hello.txt", "overwrite": "true"},
            content=b"second",
        )
        renamed = await client.patch(
            "/api/projects/files_project/files",
            json={"path": "notes", "name": "documents"},
        )
        after_rename = await client.get(
            "/api/projects/files_project/files",
            params={"path": "documents"},
        )
        downloaded = await client.get(
            "/api/projects/files_project/files/download",
            params={"path": "documents/hello.txt"},
        )
        folder_download = await client.get(
            "/api/projects/files_project/files/download",
            params={"path": "documents"},
        )
        renamed_content = (project / "documents" / "hello.txt").read_bytes()
        deleted = await client.delete(
            "/api/projects/files_project/files",
            params={"path": "documents"},
        )

    assert created.status_code == 201
    assert created.json()["type"] == "directory"
    assert uploaded.status_code == 201
    assert duplicate.status_code == 409
    assert duplicate.json()["error"] == {
        "code": "file_exists",
        "message": "An item with that name already exists in this folder.",
    }
    assert content_after_conflict == b"first"
    assert overwritten.status_code == 201
    assert renamed.status_code == 200
    assert renamed.json()["path"] == "documents"
    assert after_rename.json()["data"][0]["path"] == "documents/hello.txt"
    assert downloaded.status_code == 200
    assert downloaded.content == b"second"
    assert 'filename="hello.txt"' in downloaded.headers["content-disposition"]
    assert folder_download.status_code == 400
    assert folder_download.json()["error"]["code"] == "invalid_file_type"
    assert renamed_content == b"second"
    assert deleted.status_code == 204
    assert not (project / "documents").exists()


@pytest.mark.asyncio
async def test_project_files_reject_invalid_names_conflicts_and_root_delete(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "existing").mkdir()
    (project / "first.txt").write_text("first", encoding="utf-8")
    (project / "second.txt").write_text("second", encoding="utf-8")
    application = file_application(project)

    async with application_client(application) as client:
        invalid_name = await client.post(
            "/api/projects/files_project/files/directories",
            json={"path": "", "name": "../escape"},
        )
        duplicate_folder = await client.post(
            "/api/projects/files_project/files/directories",
            json={"path": "", "name": "existing"},
        )
        rename_conflict = await client.patch(
            "/api/projects/files_project/files",
            json={"path": "first.txt", "name": "second.txt"},
        )
        root_delete = await client.delete(
            "/api/projects/files_project/files",
            params={"path": ""},
        )
        unknown_project = await client.get("/api/projects/unknown/files")

    assert invalid_name.status_code == 400
    assert invalid_name.json()["error"]["code"] == "invalid_file_name"
    assert duplicate_folder.status_code == 409
    assert rename_conflict.status_code == 409
    # The HTTP validator rejects an empty destructive target before the filesystem layer.
    assert root_delete.status_code == 422
    assert project.is_dir()
    assert unknown_project.status_code == 404
    assert unknown_project.json()["error"]["code"] == "project_not_found"


@pytest.mark.asyncio
async def test_project_files_cannot_escape_or_follow_symbolic_links(tmp_path: Path) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    (project / "outside-link").symlink_to(outside, target_is_directory=True)
    application = file_application(project)

    async with application_client(application) as client:
        root = await client.get("/api/projects/files_project/files")
        traversals = [
            await client.get(
                "/api/projects/files_project/files",
                params={"path": value},
            )
            for value in (
                "../outside",
                "/etc",
                "C:/Windows",
                "outside-link",
                "outside-link/secret.txt",
            )
        ]
        upload_escape = await client.post(
            "/api/projects/files_project/files/upload",
            params={"path": "../outside", "name": "written.txt"},
            content=b"unsafe",
        )
        rename_link = await client.patch(
            "/api/projects/files_project/files",
            json={"path": "outside-link", "name": "renamed-link"},
        )
        delete_link = await client.delete(
            "/api/projects/files_project/files",
            params={"path": "outside-link"},
        )
        download_link = await client.get(
            "/api/projects/files_project/files/download",
            params={"path": "outside-link/secret.txt"},
        )

    assert "outside-link" not in {item["name"] for item in root.json()["data"]}
    assert all(response.status_code == 400 for response in traversals)
    assert upload_escape.status_code == 400
    assert rename_link.status_code == 400
    assert delete_link.status_code == 400
    assert download_link.status_code == 400
    assert (outside / "secret.txt").read_text(encoding="utf-8") == "secret"
    assert not (outside / "written.txt").exists()
    assert (project / "outside-link").is_symlink()
