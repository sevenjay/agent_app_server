from pathlib import Path
import subprocess

import pytest

from project_files import ProjectFileManager, ProjectGitDiffError
from projects import Project
from tests.http_client import application_client
from tests.test_project_files import file_application, git


def manager(path: Path) -> ProjectFileManager:
    return ProjectFileManager(Project("diff_project", "Diff project", path))


@pytest.mark.asyncio
async def test_file_diff_shows_staged_and_unstaged_before_after_with_safe_html(tmp_path: Path) -> None:
    git(tmp_path, "init", "-b", "main")
    target = tmp_path / "example.txt"
    target.write_text("keep\noriginal\nend\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "Initial")
    target.write_text("keep\nstaged\nend\n")
    git(tmp_path, "add", "example.txt")
    target.write_text('keep\n<script>alert("working")</script>\nend\n')
    # Local Git converters must not execute while viewing a diff.
    git(tmp_path, "config", "diff.external", "false")
    git(tmp_path, "config", "diff.test.textconv", "false")
    (tmp_path / ".gitattributes").write_text("*.txt diff=test\n")

    diff = manager(tmp_path).diff("example.txt")
    unstaged, staged = diff["sections"]
    assert diff["has_changes"] is True
    assert (unstaged["additions"], unstaged["deletions"]) == (1, 1)
    assert {line["text"]: (line["before"], line["after"]) for line in unstaged["lines"] if line["kind"] in {"addition", "deletion"}} == {
        "-staged": (2, None), '+<script>alert("working")</script>': (None, 2),
    }
    assert {line["text"] for line in staged["lines"] if line["kind"] in {"addition", "deletion"}} == {"-original", "+staged"}

    async with application_client(file_application(tmp_path)) as client:
        response = await client.get("/api/projects/files_project/files/diff", params={"path": "example.txt"})
    assert response.status_code == 200
    assert "Unstaged changes" in response.text and "Staged changes" in response.text
    assert "Before: index" in response.text and "Before: last commit (HEAD)" in response.text
    assert "&lt;script&gt;" in response.text and "<script>" not in response.text
    assert 'class="diff-line project-diff-line diff-line-addition"' in response.text
    assert 'class="diff-line project-diff-line diff-line-deletion"' in response.text
    assert response.headers["cache-control"] == "no-store"
    assert "default-src 'none'" in response.headers["content-security-policy"]

    # Staged and unstaged changes remain visible even if they cancel out against HEAD.
    target.write_text("keep\noriginal\nend\n")
    assert all(section["lines"] for section in manager(tmp_path).diff("example.txt")["sections"])


@pytest.mark.asyncio
async def test_directory_diff_includes_only_its_changes_and_handles_binary_and_deleted_files(tmp_path: Path) -> None:
    git(tmp_path, "init", "-b", "main")
    (tmp_path / "src").mkdir()
    for name in ("src/edit.txt", "src/deleted.txt", "outside.txt"):
        (tmp_path / name).write_text("before\n")
    (tmp_path / "src/image.bin").write_bytes(b"\0before")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "Initial")
    (tmp_path / "src/edit.txt").write_text("after\n")
    (tmp_path / "src/deleted.txt").unlink()
    (tmp_path / "src/image.bin").write_bytes(b"\0after")
    (tmp_path / "outside.txt").write_text("outside private change\n")
    (tmp_path / "src/new.txt").write_text("new staged file\n")
    git(tmp_path, "add", "src/new.txt")

    async with application_client(file_application(tmp_path)) as client:
        response = await client.get("/api/projects/files_project/files/diff", params={"path": "src"})
        preview = await client.get("/api/projects/files_project/files/preview", params={"path": "src"})
    assert response.status_code == 200
    assert "src/edit.txt" in response.text and "src/deleted.txt" in response.text
    assert "Binary files" in response.text and "src/image.bin" in response.text
    assert "new staged file" in response.text
    assert "outside.txt" not in response.text and "outside private change" not in response.text
    assert '/files/diff?path=src%2Fedit.txt' in preview.text
    assert 'target="_blank" rel="noopener noreferrer"' in preview.text


def test_diff_pathspecs_are_literal_and_work_with_worktrees(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    for folder in ("src[1]", "src1"):
        (repo / folder).mkdir()
        (repo / folder / "中文 #?.txt").write_text("before\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "Initial")
    worktree = tmp_path / "worktree"
    git(repo, "worktree", "add", "-b", "worktree", str(worktree), "HEAD")
    (worktree / "src[1]/中文 #?.txt").write_text("selected change\n")
    (worktree / "src1/中文 #?.txt").write_text("unrelated change\n")
    for path in ("src[1]", "src[1]/中文 #?.txt"):
        diff = manager(worktree).diff(path)
        text = "\n".join(line["text"] for section in diff["sections"] for line in section["lines"])
        assert "selected change" in text and "unrelated change" not in text


@pytest.mark.asyncio
async def test_diff_rejects_escape_reserved_paths_and_unavailable_git(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (tmp_path / "secret.txt").write_text("secret")
    (project / "link").symlink_to(tmp_path, target_is_directory=True)
    (project / ".stream_journal").mkdir()
    (project / ".stream_journal/private.txt").write_text("private")
    (project / "normal.txt").write_text("normal")
    async with application_client(file_application(project)) as client:
        base = "/api/projects/files_project/files/diff"
        for path in ("../secret.txt", str(tmp_path / "secret.txt"), "link/secret.txt", ".stream_journal", ".stream_journal/private.txt"):
            assert (await client.get(base, params={"path": path})).status_code == 400
        assert (await client.get(base, params={"path": "missing.txt"})).status_code == 404
        assert (await client.get(base, params={"path": ""})).status_code == 422
        assert (await client.get("/api/projects/unknown/files/diff", params={"path": "normal.txt"})).status_code == 404
        unavailable = await client.get(base, params={"path": "normal.txt"})
    assert unavailable.status_code == 503
    assert unavailable.json()["error"]["code"] == "git_diff_unavailable"


@pytest.mark.asyncio
async def test_diff_without_a_commit_clean_state_truncation_and_timeout(tmp_path: Path, monkeypatch) -> None:
    git(tmp_path, "init", "-b", "main")
    (tmp_path / "new.txt").write_text("new content\n" * 100)
    git(tmp_path, "add", ".")
    diff = manager(tmp_path).diff("new.txt")
    assert not diff["sections"][0]["lines"]
    assert diff["sections"][1]["additions"] == 100
    monkeypatch.setattr("project_files.MAX_DIFF_BYTES", 200)
    assert manager(tmp_path).diff("new.txt")["sections"][1]["truncated"] is True
    git(tmp_path, "commit", "-m", "Initial")
    async with application_client(file_application(tmp_path)) as client:
        response = await client.get("/api/projects/files_project/files/diff", params={"path": "new.txt"})
    assert response.status_code == 200
    assert "No staged or unstaged changes" in response.text

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("git", 10)

    monkeypatch.setattr("project_files.subprocess.run", timeout)
    with pytest.raises(ProjectGitDiffError):
        manager(tmp_path).diff("new.txt")
