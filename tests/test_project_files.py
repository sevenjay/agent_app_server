import io
import os
import re
import subprocess
import zipfile
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from main import create_app
from project_files import MAX_PREVIEW_BYTES, ProjectFileManager
from projects import Project, ProjectRegistry
from tests.fakes import FakeCodex
from tests.http_client import application_client


@pytest.mark.asyncio
async def test_journal_is_reserved_even_with_hidden_files_enabled(tmp_path):
    application = file_application(tmp_path)
    journal = tmp_path / ".stream_journal"
    journal.mkdir()
    (journal / "private.log").write_text("private")
    (tmp_path / "normal.txt").write_text("normal")
    base = "/api/projects/files_project/files"
    async with application_client(application) as client:
        listing = await client.get(base, params={"show_hidden": "true"})
        assert ".stream_journal" not in {entry["name"] for entry in listing.json()["data"]}
        requests = [
            ("GET", base, {"params": {"path": ".stream_journal", "show_hidden": "true"}}),
            ("GET", base + "/download", {"params": {"path": ".stream_journal/private.log"}}),
            ("GET", base + "/preview", {"params": {"path": ".stream_journal/private.log"}}),
            ("GET", base + "/preview/content", {"params": {"path": ".stream_journal/private.log"}}),
            ("POST", base + "/upload", {"params": {"path": ".stream_journal", "name": "private.log", "overwrite": "true"}, "content": b"overwrite"}),
            ("POST", base + "/upload", {"params": {"name": ".stream_journal"}, "content": b"overwrite"}),
            ("POST", base + "/directories", {"json": {"name": ".stream_journal"}}),
            ("PATCH", base, {"json": {"path": "normal.txt", "name": ".stream_journal"}}),
            ("PATCH", base, {"json": {"path": ".stream_journal", "name": "exposed"}}),
            ("DELETE", base, {"params": {"path": ".stream_journal"}}),
        ]
        for method, url, kwargs in requests:
            assert (await client.request(method, url, **kwargs)).status_code == 400
        assert (journal / "private.log").read_text() == "private"


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
    (project / ".git").mkdir()
    (project / ".env").write_text("SECRET=test", encoding="utf-8")
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
        root_with_hidden = await client.get(
            "/api/projects/files_project/files",
            params={"show_hidden": "true"},
        )

    assert root.status_code == 200
    assert [(item["type"], item["name"]) for item in root.json()["data"]] == [
        ("directory", "Alpha"),
        ("directory", "zeta"),
        ("file", "aardvark.txt"),
        ("file", "beta.txt"),
    ]
    assert root.json()["path"] == ""
    assert root.json()["git_available"] is False
    assert all(item["git_status"] is None for item in root.json()["data"])
    assert "nested.txt" not in {item["name"] for item in root.json()["data"]}
    assert nested.json()["path"] == "Alpha"
    assert nested.json()["data"][0]["path"] == "Alpha/nested.txt"
    assert [item["name"] for item in root_with_hidden.json()["data"]] == [
        ".git",
        "Alpha",
        "zeta",
        ".env",
        "aardvark.txt",
        "beta.txt",
    ]


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
    assert folder_download.status_code == 200
    assert 'filename="documents.zip"' in folder_download.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(folder_download.content)) as archive:
        assert archive.read("documents/hello.txt") == b"second"
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


@pytest.mark.asyncio
async def test_preview_text_folders_media_and_binary_files(tmp_path: Path) -> None:
    (tmp_path / "notes & 資料").mkdir()
    name = 'notes & 資料/example #1.html'
    (tmp_path / name).write_text('<script>alert("unsafe")</script>\n你好', encoding="utf-8")
    (tmp_path / "notes & 資料" / ".hidden").write_text("hidden")
    (tmp_path / "binary.dat").write_bytes(b"\x00\xff\x01")
    (tmp_path / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (tmp_path / "large.txt").write_bytes(b"a" * (MAX_PREVIEW_BYTES + 10))
    base = "/api/projects/files_project/files"
    async with application_client(file_application(tmp_path)) as client:
        text = await client.get(base + "/preview", params={"path": name})
        folder = await client.get(base + "/preview", params={"path": "notes & 資料"})
        hidden = await client.get(base + "/preview", params={"path": "notes & 資料", "show_hidden": "true"})
        binary = await client.get(base + "/preview", params={"path": "binary.dat"})
        large = await client.get(base + "/preview", params={"path": "large.txt"})
        image = await client.get(base + "/preview", params={"path": "image.png"})
        content = await client.get(base + "/preview/content", params={"path": "image.png"})
        html_content = await client.get(base + "/preview/content", params={"path": name})
        missing = await client.get(base + "/preview", params={"path": "missing.txt"})
        unknown = await client.get("/api/projects/unknown/files/preview", params={"path": name})

    assert text.status_code == 200
    assert "&lt;script&gt;" in text.text and "<script>" not in text.text
    assert "你好" in text.text and "Parent folder" not in text.text
    assert 'aria-label="File path"' in text.text
    assert 'aria-label="Copy raw"' in text.text
    assert text.text.index('id="file-edit"') < text.text.index('id="file-copy-raw"')
    assert 'id="file-editor"' in text.text
    assert "default-src 'none'" in text.headers["content-security-policy"]
    assert text.headers["cache-control"] == "no-store"
    assert "example #1.html" in folder.text and ".hidden" not in folder.text
    assert "%231.html" in folder.text and "Download ZIP" in folder.text
    assert ".hidden" in hidden.text and "show_hidden=true" in hidden.text
    assert "Preview is not available" in binary.text
    for page in (folder, binary, image):
        assert 'id="file-edit"' not in page.text
    assert "Showing the first 1 MiB" in large.text
    assert len(large.content) < MAX_PREVIEW_BYTES + 8192
    assert '<img src="' in image.text
    assert content.status_code == 200 and content.content.startswith(b"\x89PNG")
    assert content.headers["content-type"] == "image/png"
    assert content.headers["content-disposition"].startswith("inline;")
    assert "sandbox" in content.headers["content-security-policy"]
    assert html_content.status_code == 400
    assert missing.status_code == 404 and unknown.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", ["example #1.py", " example #1.py "])
async def test_preview_save_url_overwrites_the_selected_file_and_preserves_permissions(tmp_path: Path, filename: str) -> None:
    folder = tmp_path / "docs & 資料"
    folder.mkdir()
    target = folder / filename
    target.write_bytes(b"print('before')\r\n")
    target.chmod(0o755)
    base = "/api/projects/files_project/files"
    content = "\ufeffprint('修改')\r\n".encode("utf-8")
    async with application_client(file_application(tmp_path)) as client:
        page = await client.get(base + "/preview", params={"path": f"docs & 資料/{filename}"})
        save_url = unescape(re.search(r'data-save-url="([^"]+)"', page.text).group(1))
        assert parse_qs(urlsplit(save_url).query) == {
            "path": ["docs & 資料"], "name": [filename], "overwrite": ["true"],
        }
        saved = await client.post(save_url, content=content, headers={"Content-Type": "application/octet-stream"})
        refreshed = await client.get(base + "/preview", params={"path": f"docs & 資料/{filename}"})
    assert saved.status_code == 201
    assert target.read_bytes() == content
    assert target.stat().st_mode & 0o777 == 0o755
    assert "修改" in refreshed.text and "before" not in refreshed.text
    assert sorted(path.name for path in folder.iterdir()) == [target.name]


@pytest.mark.asyncio
async def test_preview_breadcrumbs_link_each_directory_and_preserve_hidden_setting(tmp_path: Path) -> None:
    project = tmp_path / "agent_app_server"
    folder = project / "docs & 資料" / "api #1"
    folder.mkdir(parents=True)
    (folder / "api.md").write_text("# API\n\n<script>alert('unsafe')</script>", encoding="utf-8")
    base = "/api/projects/files_project/files/preview"
    async with application_client(file_application(project)) as client:
        page = await client.get(base, params={"path": "docs & 資料/api #1/api.md", "show_hidden": "true"})
        root = await client.get(base)

    assert page.status_code == 200
    assert f'href="{base}?path=&amp;show_hidden=true">agent_app_server</a>' in page.text
    assert f'href="{base}?path=docs+%26+%E8%B3%87%E6%96%99&amp;show_hidden=true"' in page.text
    assert f'href="{base}?path=docs+%26+%E8%B3%87%E6%96%99%2Fapi+%231&amp;show_hidden=true"' in page.text
    assert '<span aria-current="page">api.md</span>' in page.text
    assert 'data-file-path="docs &amp; 資料/api #1/api.md"' in page.text
    assert f'data-preview-url="{base}?path=&amp;show_hidden=true"' in page.text
    assert "Parent folder" not in page.text and "<h1>api.md</h1>" not in page.text
    assert "&lt;script&gt;" in page.text and "<script>alert" not in page.text
    assert '<span aria-current="page">agent_app_server</span>' in root.text
    assert 'id="file-download"' not in root.text
    csp = page.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "connect-src 'self'" in csp
    script_policy = next(part for part in csp.split(";") if part.strip().startswith("script-src "))
    assert "unsafe-inline" not in script_policy and "base-uri 'none'" in csp
    assert script_policy.strip() == "script-src 'self'" and "frame-src 'self' data:" in csp
    assert "unsafe-inline" not in root.headers["content-security-policy"]


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", ["api.md", "README.MD", "readme.markdown", "app.py", "app.js", "config.yaml", "pyproject.toml", "notes.txt"])
async def test_preview_only_markdown_has_two_views(tmp_path: Path, filename: str) -> None:
    (tmp_path / filename).write_text("# title\n\nhello", encoding="utf-8")
    async with application_client(file_application(tmp_path)) as client:
        page = await client.get("/api/projects/files_project/files/preview", params={"path": filename})

    assert page.status_code == 200
    markdown = filename.lower().endswith((".md", ".markdown"))
    assert ('id="file-preview-tab"' in page.text) is markdown
    assert ('id="file-plain-tab"' in page.text) is markdown
    assert ("/static/vendor/marked-15.0.12.min.js" in page.text) is markdown
    assert ("/static/vendor/dompurify-3.2.6.min.js" in page.text) is markdown
    assert "https://unpkg.com" not in page.text
    assert "mermaid-11.12.0.min.js" not in page.text  # Loaded on demand after Markdown is ready.
    assert 'src="/static/js/file-preview.js?v=' in page.text
    assert 'aria-label="Copy raw"' in page.text
    assert page.text.count('aria-label="Download"') == 1


@pytest.mark.asyncio
async def test_preview_script_url_changes_with_content_even_when_metadata_is_unchanged(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "notes.md").write_text("# Preview")
    static = tmp_path / "static"
    (static / "js").mkdir(parents=True)
    script = static / "js" / "file-preview.js"
    script.write_text("window.previewRevision = 1;")
    metadata = script.stat()
    monkeypatch.setattr("main.STATIC_DIR", static)

    def script_url(html):
        return re.search(r'src="(/static/js/file-preview\.js\?v=[a-f0-9]+)"', html).group(1)

    async with application_client(file_application(project)) as client:
        url = "/api/projects/files_project/files/preview?path=notes.md"
        first = script_url((await client.get(url)).text)
        assert script_url((await client.get(url)).text) == first
        assert (await client.get(first)).text == "window.previewRevision = 1;"
        script.write_text("window.previewRevision = 2;")
        os.utime(script, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
        updated = script_url((await client.get(url)).text)
        assert updated != first
        assert (await client.get(updated)).text == "window.previewRevision = 2;"


@pytest.mark.asyncio
async def test_preview_and_archives_respect_project_boundaries(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    folder = project / "folder"
    folder.mkdir()
    (folder / "empty").mkdir()
    (folder / "safe.txt").write_text("safe")
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    (folder / "link.txt").symlink_to(outside)
    (project / "linked").symlink_to(tmp_path, target_is_directory=True)
    temporary = tmp_path / "archives"
    temporary.mkdir()
    monkeypatch.setattr("project_files.tempfile.tempdir", str(temporary))
    base = "/api/projects/files_project/files"
    async with application_client(file_application(project)) as client:
        for endpoint in ("/preview", "/preview/content", "/download"):
            for path in ("../secret.txt", str(outside), "linked/secret.txt", "folder/link.txt"):
                response = await client.get(base + endpoint, params={"path": path})
                assert response.status_code == 400
        response = await client.get(base + "/download", params={"path": "folder"})
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert set(archive.namelist()) == {"folder/", "folder/empty/", "folder/safe.txt"}
        assert archive.read("folder/safe.txt") == b"safe"
    assert not list(temporary.glob("codex-download-*"))


def git(project: Path, *arguments: str, check: bool = True):
    return subprocess.run(
        ["git", "-C", str(project), "-c", "user.name=File tests", "-c", "user.email=files@example.test", *arguments],
        capture_output=True, check=check,
    )


@pytest.mark.asyncio
async def test_git_status_includes_staged_unstaged_renames_ignored_and_nested_changes(tmp_path: Path) -> None:
    git(tmp_path, "init", "-b", "main")
    (tmp_path / "src").mkdir()
    (tmp_path / "src2").mkdir()
    for name in ("clean.txt", "modified.txt", "staged.txt", "rename me.txt", "src/deleted.txt", "src/clean.txt", "src2/clean.txt"):
        (tmp_path / name).write_text(name)
    (tmp_path / ".gitignore").write_text("ignored/\n*.log\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "Initial")
    (tmp_path / "modified.txt").write_text("modified")
    (tmp_path / "staged.txt").write_text("staged")
    (tmp_path / "added.txt").write_text("added")
    git(tmp_path, "add", "staged.txt", "added.txt")
    git(tmp_path, "mv", "rename me.txt", "重新 命名.txt")
    (tmp_path / "src/deleted.txt").unlink()
    (tmp_path / "new 資料.txt").write_text("untracked")
    (tmp_path / "ignored").mkdir()
    (tmp_path / "ignored/debug.log").write_text("ignored")
    (tmp_path / "new directory").mkdir()
    (tmp_path / "new directory/file.txt").write_text("new")
    base = "/api/projects/files_project/files"
    async with application_client(file_application(tmp_path)) as client:
        root = (await client.get(base)).json()
        nested = (await client.get(base, params={"path": "src"})).json()
        ignored = (await client.get(base, params={"path": "ignored"})).json()
    assert root["git_available"] is True
    entries = {entry["name"]: entry for entry in root["data"]}
    assert {name: entry["git_status"] for name, entry in entries.items()} == {
        "src": "deleted", "src2": None, "clean.txt": None, "modified.txt": "modified", "staged.txt": "modified",
        "added.txt": "added", "重新 命名.txt": "renamed", "new 資料.txt": "untracked", "ignored": "ignored", "new directory": "untracked",
    }
    assert entries["staged.txt"]["git_status_code"] == "M "
    assert entries["modified.txt"]["git_status_code"] == " M"
    assert entries["重新 命名.txt"]["git_status_code"] == "R "
    assert nested["git_available"] is True and nested["data"][0]["git_status"] is None
    assert ignored["data"][0]["git_status"] == "ignored"


@pytest.mark.asyncio
@pytest.mark.parametrize("root_repository", [False, True])
async def test_git_status_aggregates_direct_child_repositories(tmp_path: Path, root_repository: bool) -> None:
    if root_repository:
        git(tmp_path, "init", "-b", "main")
        (tmp_path / "root.txt").write_text("before")
        (tmp_path / ".gitignore").write_text("clean/\n")
        git(tmp_path, "add", ".")
        git(tmp_path, "commit", "-m", "Initial root")
        (tmp_path / "root.txt").write_text("after")

    for name in ("clean", "modified repo", "新增 repo"):
        repository = tmp_path / name
        (repository / "src[1]/nested").mkdir(parents=True)
        (repository / "src[1]/nested/file.txt").write_text("before")
        (repository / ".gitignore").write_text("*.log\n")
        git(repository, "init", "-b", "main")
        git(repository, "add", ".")
        git(repository, "commit", "-m", "Initial child")
    (tmp_path / "clean/debug.log").write_text("ignored")
    (tmp_path / "modified repo/src[1]/nested/file.txt").write_text("after")
    (tmp_path / "新增 repo/added.txt").write_text("added")
    git(tmp_path / "新增 repo", "add", "added.txt")
    (tmp_path / "notes.txt").write_text("outside repositories")

    base = "/api/projects/files_project/files"
    async with application_client(file_application(tmp_path)) as client:
        root = (await client.get(base)).json()
        child = (await client.get(base, params={"path": "modified repo"})).json()
        nested = (await client.get(base, params={"path": "modified repo/src[1]/nested"})).json()
        clean = (await client.get(base, params={"path": "clean"})).json()

    assert root["git_available"] is True
    statuses = {entry["name"]: entry["git_status"] for entry in root["data"]}
    assert statuses == {
        "clean": None, "modified repo": "modified", "新增 repo": "added",
        "notes.txt": "untracked" if root_repository else None,
        **({"root.txt": "modified"} if root_repository else {}),
    }
    assert child["git_available"] is True
    assert child["data"][0]["git_status"] == "modified"
    assert nested["git_available"] is True
    assert nested["data"][0]["git_status"] == "modified"
    assert nested["data"][0]["git_status_code"] == " M"
    assert {entry["name"]: entry["git_status"] for entry in clean["data"]} == {"src[1]": None, "debug.log": "ignored"}


def test_git_repository_discovery_stops_after_direct_children(tmp_path: Path, monkeypatch) -> None:
    # Neither an ancestor repository nor a repository two levels down is eligible.
    git(tmp_path, "init", "-b", "main")
    project = tmp_path / "project"
    nested = project / "group/repo"
    nested.mkdir(parents=True)
    git(nested, "init", "-b", "main")
    (nested / "file.txt").write_text("untracked")
    (project / "linked_repo").symlink_to(nested, target_is_directory=True)
    journal = project / ".stream_journal"
    journal.mkdir()
    git(journal, "init", "-b", "main")
    manager = ProjectFileManager(Project("project", "Project", project))

    def unexpected_git(*args, **kwargs):
        pytest.fail("Git must not query repositories outside the allowed discovery depth")

    monkeypatch.setattr("project_files.subprocess.run", unexpected_git)
    for path in ("", "group", "group/repo"):
        listing = manager.list_directory(path, show_hidden=True)
        assert listing["git_available"] is False
        assert all(entry["git_status"] is None for entry in listing["data"])
    assert {entry["name"] for entry in manager.list_directory(show_hidden=True)["data"]} == {"group"}


def test_git_child_worktrees_and_failed_repositories_are_independent(tmp_path: Path, monkeypatch) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    git(repository, "init", "-b", "main")
    (repository / "file.txt").write_text("initial")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "Initial")
    worktree = tmp_path / "worktree"
    git(repository, "worktree", "add", "-b", "worktree", str(worktree), "HEAD")
    (worktree / "file.txt").write_text("modified")
    (tmp_path / "broken/.git").mkdir(parents=True)
    (tmp_path / "unreadable").mkdir()
    exists = Path.exists

    def marker_exists(path):
        if path == tmp_path / "unreadable/.git":
            raise PermissionError
        return exists(path)

    monkeypatch.setattr(Path, "exists", marker_exists)
    manager = ProjectFileManager(Project("project", "Project", tmp_path))

    listing = manager.list_directory()
    assert listing["git_available"] is True
    assert {entry["name"]: entry["git_status"] for entry in listing["data"]} == {
        "repo": None, "worktree": "modified", "broken": None, "unreadable": None,
    }
    assert manager.list_directory("worktree")["data"][0]["git_status"] == "modified"
    assert manager.list_directory("broken")["git_available"] is False


def test_git_ignored_status_stays_on_ignored_entries_without_marking_ancestors(tmp_path: Path) -> None:
    git(tmp_path, "init", "-b", "main")
    (tmp_path / "src/nested").mkdir(parents=True)
    (tmp_path / "src/clean.txt").write_text("clean")
    (tmp_path / "src/nested/clean.txt").write_text("clean")
    (tmp_path / ".gitignore").write_text("*.log\ncache/\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "Initial")
    (tmp_path / "root.log").write_text("ignored file")
    (tmp_path / "src/debug.log").write_text("ignored file")
    (tmp_path / "src/nested/cache/subdir").mkdir(parents=True)
    (tmp_path / "src/nested/cache/subdir/artifact.txt").write_text("ignored contents")
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs/only.log").write_text("ignored file in an otherwise untracked folder")
    manager = ProjectFileManager(Project("repo", "Repo", tmp_path))

    def statuses(path: str = ""):
        return {entry["name"]: entry["git_status"] for entry in manager.list_directory(path)["data"]}

    assert statuses() == {"src": None, "logs": None, "root.log": "ignored"}
    assert statuses("src") == {"nested": None, "clean.txt": None, "debug.log": "ignored"}
    assert statuses("src/nested") == {"cache": "ignored", "clean.txt": None}
    assert statuses("src/nested/cache") == {"subdir": "ignored"}
    assert statuses("src/nested/cache/subdir") == {"artifact.txt": "ignored"}
    assert statuses("logs") == {"only.log": "ignored"}

    # Real changes still propagate alongside ignored descendants.
    (tmp_path / "src/nested/clean.txt").write_text("modified")
    assert statuses()["src"] == "modified"
    assert statuses("src")["nested"] == "modified"
    (tmp_path / "logs/new.txt").write_text("untracked")
    assert statuses()["logs"] == "untracked"


def test_git_conflicts_worktrees_and_unavailable_git(tmp_path: Path, monkeypatch) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    git(repository, "init", "-b", "main")
    (repository / "conflict.txt").write_text("initial\n")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "Initial")
    git(repository, "checkout", "-b", "other")
    (repository / "conflict.txt").write_text("other\n")
    git(repository, "commit", "-am", "Other")
    git(repository, "checkout", "main")
    (repository / "conflict.txt").write_text("main\n")
    git(repository, "commit", "-am", "Main")
    assert git(repository, "merge", "other", check=False).returncode == 1
    manager = ProjectFileManager(Project("repo", "Repo", repository))
    assert manager.list_directory()["data"][0]["git_status"] == "conflicted"
    worktree = tmp_path / "worktree"
    git(repository, "worktree", "add", "-b", "worktree", str(worktree), "HEAD")
    (worktree / "conflict.txt").write_text("worktree change")
    assert (worktree / ".git").is_file()
    manager = ProjectFileManager(Project("worktree", "Worktree", worktree))
    assert manager.list_directory()["data"][0]["git_status"] == "modified"

    def unavailable(*args, **kwargs):
        raise subprocess.TimeoutExpired("git", 3)

    monkeypatch.setattr("project_files.subprocess.run", unavailable)
    result = manager.list_directory()
    assert result["git_available"] is False
    assert result["data"][0]["git_status"] is None
