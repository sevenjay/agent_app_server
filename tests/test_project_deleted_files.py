import shutil
from pathlib import Path

import pytest

from project_files import ProjectFileManager, ProjectFileNotFoundError
from projects import Project
from tests.http_client import application_client
from tests.test_project_files import file_application, git


def manager(root: Path) -> ProjectFileManager:
    return ProjectFileManager(Project("project", "Project", root))


def seed_repository(repository: Path, files: dict[str, str]) -> None:
    repository.mkdir(parents=True, exist_ok=True)
    git(repository, "init", "-b", "main")
    for name, content in files.items():
        path = repository / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    git(repository, "add", ".")
    git(repository, "commit", "-m", "Initial")


@pytest.mark.asyncio
@pytest.mark.parametrize("repo_path", ["", "child repo"])
@pytest.mark.parametrize("staged", [False, True])
async def test_deleted_files_and_directories_can_be_browsed_and_diffed_until_committed(
    tmp_path: Path, repo_path: str, staged: bool,
) -> None:
    repository = tmp_path / repo_path
    seed_repository(repository, {
        "root.txt": "root before deletion\n",
        "src/keep.txt": "unchanged\n",
        "src/deleted.txt": "removed from existing directory\n",
        "old[1]/deep/中文 #?.txt": "<deleted content>\n",
        "old[1]/deep/.hidden": "hidden deletion\n",
        ".hidden-root": "hidden root deletion\n",
    })
    for name in ("root.txt", "src/deleted.txt", ".hidden-root"):
        (repository / name).unlink()
    shutil.rmtree(repository / "old[1]")
    if staged:
        git(repository, "add", "-u")

    def path(name: str) -> str:
        return f"{repo_path}/{name}" if repo_path else name

    base = "/api/projects/files_project/files"
    async with application_client(file_application(tmp_path)) as client:
        if repo_path:
            project = (await client.get(base)).json()
            assert project["data"][0]["exists"] is True
            assert project["data"][0]["git_status"] == "deleted"
            assert project["data"][0]["git_repository"] == repo_path
        listing = (await client.get(base, params={"path": repo_path})).json()
        entries = {entry["name"]: entry for entry in listing["data"]}
        assert list(entries) == ["old[1]", "src", "root.txt"]
        assert entries["src"]["exists"] is True
        assert entries["src"]["git_status"] == "deleted"
        for name in ("old[1]", "root.txt"):
            entry = entries[name]
            assert entry["exists"] is False
            assert entry["git_status"] == "deleted"
            assert entry["git_status_code"] == ("D " if staged else " D")
            assert entry["git_staged"] is staged
            assert entry["git_unstaged"] is not staged
            assert entry["git_repository"] == repo_path
            assert entry["size"] is None and entry["modified_at"] is None
        old = (await client.get(base, params={"path": path("old[1]")})).json()
        assert [(entry["name"], entry["type"], entry["exists"]) for entry in old["data"]] == [("deep", "directory", False)]
        deep = (await client.get(base, params={"path": path("old[1]/deep")})).json()
        assert [entry["name"] for entry in deep["data"]] == ["中文 #?.txt"]
        hidden = (await client.get(base, params={"path": path("old[1]/deep"), "show_hidden": "true"})).json()
        assert {entry["name"] for entry in hidden["data"]} == {"中文 #?.txt", ".hidden"}
        existing = (await client.get(base, params={"path": path("src")})).json()
        assert [(entry["name"], entry["exists"]) for entry in existing["data"]] == [("deleted.txt", False), ("keep.txt", True)]

        for name in ("old[1]", "old[1]/deep", "old[1]/deep/中文 #?.txt", "root.txt"):
            response = await client.get(base + "/diff", params={"path": path(name)})
            assert response.status_code == 200
            assert "missing from the working tree" in response.text
            assert ("Staged changes" if staged else "Unstaged changes") in response.text
            assert "diff-line-deletion" in response.text
            assert ">Preview</a>" not in response.text
            if name.startswith("old[1]"):
                assert "-&lt;deleted content&gt;" in response.text
                assert "root before deletion" not in response.text

        preview = await client.get(base + "/preview", params={"path": repo_path})
        assert 'data-exists="false"' in preview.text
        assert "Missing from working tree — View diff" in preview.text
        assert "/files/preview?path=" + path("root.txt") not in preview.text
        assert (await client.get(base, params={"path": path("old[1]/never-tracked")})).status_code == 404
        assert (await client.get(base + "/diff", params={"path": path("old[1]/missing.txt")})).status_code == 404

        git(repository, "add", "-u")
        git(repository, "commit", "-m", "Delete files")
        refreshed = (await client.get(base, params={"path": repo_path})).json()
        assert [entry["name"] for entry in refreshed["data"]] == ["src"]
        assert (await client.get(base, params={"path": path("old[1]")})).status_code == 404


@pytest.mark.asyncio
async def test_missing_entries_reject_filesystem_operations_and_symlink_paths(tmp_path: Path) -> None:
    project = tmp_path / "project"
    seed_repository(project, {"deleted/nested/file.txt": "deleted\n", "linked/file.txt": "tracked\n"})
    shutil.rmtree(project / "deleted")
    shutil.rmtree(project / "linked")
    (project / "linked").symlink_to(tmp_path, target_is_directory=True)
    base = "/api/projects/files_project/files"
    async with application_client(file_application(project)) as client:
        listing = (await client.get(base)).json()
        assert [entry["name"] for entry in listing["data"]] == ["deleted"]
        for suffix in ("", "/diff"):
            for path in ("linked/missing.txt", "../missing.txt", ".stream_journal/missing.txt"):
                assert (await client.get(base + suffix, params={"path": path})).status_code == 400
        for path in ("deleted", "deleted/nested/file.txt"):
            for suffix in ("/preview", "/download"):
                assert (await client.get(base + suffix, params={"path": path})).status_code == 404
            assert (await client.delete(base, params={"path": path})).status_code == 404
            assert (await client.patch(base, json={"path": path, "name": "renamed"})).status_code == 404
        assert (await client.post(base + "/directories", json={"path": "deleted", "name": "new"})).status_code == 404
        assert (await client.post(base + "/upload", params={"path": "deleted", "name": "new.txt"}, content=b"new")).status_code == 404
    assert not (project / "deleted").exists()


def test_renames_and_recreated_paths_do_not_produce_duplicate_or_false_deleted_rows(tmp_path: Path) -> None:
    seed_repository(tmp_path, {"source/old.txt": "renamed\n", "recreated.txt": "original\n", "replaced.txt": "original\n"})
    (tmp_path / "destination").mkdir()
    git(tmp_path, "mv", "source/old.txt", "destination/new.txt")
    git(tmp_path, "rm", "recreated.txt", "replaced.txt")
    (tmp_path / "recreated.txt").write_text("new untracked content\n")
    (tmp_path / "replaced.txt").mkdir()
    (tmp_path / "replaced.txt/new.txt").write_text("new directory\n")
    files = manager(tmp_path)
    assert files.list_directory("source")["data"] == []
    (tmp_path / "source").rmdir()
    entries = {entry["name"]: entry for entry in files.list_directory()["data"]}
    assert "source" not in entries
    assert entries["recreated.txt"]["exists"] is True
    assert entries["recreated.txt"]["git_status_code"] == "D "
    assert entries["replaced.txt"]["exists"] is True
    assert entries["replaced.txt"]["type"] == "directory"
    assert files.list_directory("destination")["data"][0]["git_status"] == "renamed"
    with pytest.raises(ProjectFileNotFoundError):
        files.diff("source/old.txt")


def test_deleted_worktree_file_preserves_both_staged_and_unstaged_changes(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    seed_repository(repository, {"file.txt": "original\n"})
    worktree = tmp_path / "worktree"
    git(repository, "worktree", "add", "-b", "worktree", str(worktree), "HEAD")
    (worktree / "file.txt").write_text("staged version\n")
    git(worktree, "add", "file.txt")
    (worktree / "file.txt").unlink()
    files = manager(tmp_path)
    entry = files.list_directory("worktree")["data"][0]
    assert entry["exists"] is False and entry["git_repository"] == "worktree"
    assert entry["git_status_code"] == "MD"
    assert entry["git_staged"] is True and entry["git_unstaged"] is True
    diff = files.diff("worktree/file.txt")
    unstaged, staged = diff["sections"]
    assert {line["text"] for line in unstaged["lines"] if line["kind"] == "deletion"} == {"-staged version"}
    assert {line["text"] for line in staged["lines"] if line["kind"] == "deletion"} == {"-original"}
    assert {line["text"] for line in staged["lines"] if line["kind"] == "addition"} == {"+staged version"}
    assert files.list_directory("repo")["data"][0]["git_status"] is None


def test_deleted_directory_with_a_staged_rename_keeps_the_correct_staging_state(tmp_path: Path) -> None:
    seed_repository(tmp_path, {"old/moved.txt": "moved\n", "old/deleted.txt": "deleted\n"})
    git(tmp_path, "mv", "old/moved.txt", "moved.txt")
    git(tmp_path, "rm", "old/deleted.txt")
    files = manager(tmp_path)
    entries = {entry["name"]: entry for entry in files.list_directory()["data"]}
    assert entries["old"]["exists"] is False
    assert entries["old"]["git_staged"] is True
    assert entries["old"]["git_unstaged"] is False
    assert [entry["name"] for entry in files.list_directory("old")["data"]] == ["deleted.txt"]
