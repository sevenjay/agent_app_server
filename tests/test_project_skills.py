import base64
import io
import json
import os
import stat
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from main import create_app
from project_files import InvalidFilePathError, ProjectFileError
from project_skills import ProjectSkillManager, SkillError, SkillImportStore, SkillLimits, validate_skill
from projects import Project, ProjectRegistry
from tenancy import TenantWorkspaceManager
from tests.fakes import FakeCodex
from tests.http_client import application_client


def skill_source(name="code-review", description="Review code when asked."):
    return f"---\n# Keep this comment\nname: {name}\ndescription: {description}\nmetadata:\n  version: '1.0'\nx-vendor: true\n---\n\n# Instructions\n\nReview the code.\n"


def archive(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as target:
        for path, content in files.items():
            target.writestr(path, content)
    return output.getvalue()


def manager_for(path, limits=None):
    return ProjectSkillManager(Project("project", "Project", path.resolve()), limits)


def install_skill(path, name="code-review"):
    root = path / ".agents" / "skills" / name
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(skill_source(name))
    return root


@pytest.mark.parametrize("name", ["Uppercase", "-prefix", "suffix-", "two--words", "under_score", "a" * 65])
def test_standard_rejects_invalid_names(name):
    assert not validate_skill(skill_source(name), name)["valid"]


@pytest.mark.parametrize("metadata", [
    "name: code-review\ndescription: 42", "name: code-review\ndescription: ok\ncompatibility: ''",
    "name: code-review\ndescription: ok\nmetadata: {version: 1}",
    "name: code-review\ndescription: ok\nlicense: [MIT]",
    "name: code-review\nname: overwritten\ndescription: ok",
    "name: &name code-review\ndescription: *name", "!!python/object:os.system {}",
])
def test_standard_rejects_invalid_metadata(metadata):
    assert not validate_skill(f"---\n{metadata}\n---\n", "code-review")["valid"]


@pytest.mark.parametrize("allowed_tools", [
    "Read Glob Grep WebFetch WebSearch",
    '"Read, Glob, Grep, WebFetch, WebSearch"',
    "[Read, Glob, Grep, WebFetch, WebSearch, 'Bash(git:*)']",
    "\n  - Read\n  - Glob\n  - Grep\n  - WebFetch\n  - WebSearch",
    "[]",
    "''",
])
def test_allowed_tools_accepts_strings_and_claude_code_lists(allowed_tools):
    result = validate_skill(f"---\nname: code-review\ndescription: Review code.\nallowed-tools: {allowed_tools}\n---\n", "code-review")
    assert result["valid"], result["errors"]
    assert result["errors"] == []


@pytest.mark.parametrize("allowed_tools", [
    "42", "true", "null", "{Read: true}",
    "[Read, 42]", "[Read, false]", "[Read, null]", "[Read, {tool: Grep}]", "[Read, [Grep]]",
])
def test_allowed_tools_rejects_non_strings_and_mixed_lists(allowed_tools):
    result = validate_skill(f"---\nname: code-review\ndescription: Review code.\nallowed-tools: {allowed_tools}\n---\n", "code-review")
    assert not result["valid"]
    assert result["errors"] == ["allowed-tools must be a string or a list of strings."]


def test_warnings_are_advisory_and_unknown_fields_are_allowed():
    result = validate_skill(skill_source() + "\n" * 501, "code-review")
    assert result["valid"]
    assert result["warnings"]
    assert not validate_skill(skill_source(), "other-name")["valid"]


def test_empty_listing_is_read_only_and_invalid_skills_remain_repairable(tmp_path):
    manager = manager_for(tmp_path)
    assert manager.list_skills()["data"] == []
    assert not (tmp_path / ".agents").exists()
    folder = tmp_path / ".agents/skills/broken"
    folder.mkdir(parents=True)
    detail = manager.detail("broken")
    assert detail["supported"] and not detail["valid"]
    assert "SKILL.md is missing." in detail["errors"]
    repaired = manager.save_file("broken", "SKILL.md", skill_source("broken").encode(), "missing")
    assert repaired["valid"]
    manager.delete("broken", repaired["revision"])
    assert not folder.exists()


def test_edit_conflicts_preserve_external_changes_and_script_permissions(tmp_path):
    root = install_skill(tmp_path)
    script = root / "run.sh"
    script.write_text("echo old\n")
    script.chmod(0o755)
    manager = manager_for(tmp_path)
    opened = manager.read_file("code-review", "run.sh")
    manager.save_file("code-review", "run.sh", b"echo new\n", opened["revision"])
    assert stat.S_IMODE(script.stat().st_mode) == 0o755
    with pytest.raises(SkillError, match="changed"):
        manager.save_file("code-review", "run.sh", b"lost update\n", opened["revision"])
    assert script.read_text() == "echo new\n"
    opened = manager.read_file("code-review", "SKILL.md")
    with pytest.raises(SkillError):
        manager.save_file("code-review", "SKILL.md", b"not a skill", opened["revision"])
    assert (root / "SKILL.md").read_text() == opened["content"]


def test_concurrent_saves_allow_one_winner_and_report_the_other_as_stale(tmp_path):
    install_skill(tmp_path)
    manager = manager_for(tmp_path)
    opened = manager.read_file("code-review", "SKILL.md")

    def save(content):
        try:
            manager_for(tmp_path).save_file("code-review", "SKILL.md", content.encode(), opened["revision"])
            return "saved"
        except SkillError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(save, [opened["content"] + "\nFirst edit", opened["content"] + "\nSecond edit"]))
    assert sorted(results) == ["saved", "skill_changed"]


def test_rename_preserves_comments_extensions_and_resources(tmp_path):
    root = install_skill(tmp_path)
    (root / "reference.md").write_text("resource")
    manager = manager_for(tmp_path)
    renamed = manager.rename("code-review", "review-code", manager.detail("code-review")["revision"])
    updated = tmp_path / ".agents/skills/review-code"
    assert renamed["valid"] and not root.exists()
    assert "# Keep this comment" in (updated / "SKILL.md").read_text()
    assert "x-vendor: true" in (updated / "SKILL.md").read_text()
    assert (updated / "reference.md").read_text() == "resource"


@pytest.mark.parametrize("wrapper", ["", "code-review/"])
def test_zip_import_is_previewed_and_replaces_entire_skill(tmp_path, wrapper):
    root = install_skill(tmp_path)
    (root / "obsolete.txt").write_text("obsolete")
    store = SkillImportStore()
    manager = manager_for(tmp_path)
    preview = store.prepare(manager, "local", archive({wrapper + "SKILL.md": skill_source(), wrapper + "assets/file.bin": b"\x00\xff"}), "zip")
    try:
        assert preview["exists"] and preview["valid"]
        assert (root / "obsolete.txt").exists()
        with pytest.raises(SkillError, match="Confirm replacement"):
            store.commit(preview["token"], manager, "local", False)
        result = store.commit(preview["token"], manager, "local", True)
        assert result["valid"]
        assert not (root / "obsolete.txt").exists()
        assert (root / "assets/file.bin").read_bytes() == b"\x00\xff"
    finally:
        store.close()


def test_directory_manifest_import_and_zip_execute_bits(tmp_path):
    store = SkillImportStore()
    manager = manager_for(tmp_path)
    raw = json.dumps({"files": [{"path": "code-review/SKILL.md", "content": base64.b64encode(skill_source().encode()).decode()}]}).encode()
    try:
        preview = store.prepare(manager, "local", raw, "directory")
        assert not (tmp_path / ".agents").exists()
        store.commit(preview["token"], manager, "local", False)
        script = zipfile.ZipInfo("code-review/run.sh")
        script.create_system = 3
        script.external_attr = (stat.S_IFREG | 0o755) << 16
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as zipped:
            zipped.writestr("code-review/SKILL.md", skill_source())
            zipped.writestr(script, "echo hello\n")
        preview = store.prepare(manager, "local", output.getvalue(), "zip")
        store.commit(preview["token"], manager, "local", True)
        assert stat.S_IMODE((tmp_path / ".agents/skills/code-review/run.sh").stat().st_mode) == 0o755
    finally:
        store.close()


def test_stale_replacement_and_failed_install_keep_old_skill(tmp_path, monkeypatch):
    root = install_skill(tmp_path)
    store = SkillImportStore()
    manager = manager_for(tmp_path)
    try:
        preview = store.prepare(manager, "local", archive({"SKILL.md": skill_source()}), "zip")
        (root / "external.txt").write_text("do not lose")
        with pytest.raises(SkillError, match="changed"):
            store.commit(preview["token"], manager, "local", True)
        assert (root / "external.txt").read_text() == "do not lose"
        store.discard(preview["token"], manager, "local")
        preview = store.prepare(manager, "local", archive({"SKILL.md": skill_source()}), "zip")
        original_rename = os.rename

        def fail_install(source, destination):
            if Path(source).name.startswith(".skill-install-"):
                raise OSError("simulated install failure")
            return original_rename(source, destination)

        monkeypatch.setattr("project_skills.os.rename", fail_install)
        with pytest.raises(ProjectFileError):
            store.commit(preview["token"], manager, "local", True)
        assert (root / "external.txt").read_text() == "do not lose"
        assert (root / "SKILL.md").read_text() == skill_source()
    finally:
        store.close()


@pytest.mark.parametrize("path", ["../escape", "/absolute", "C:/windows", "code-review/../escape", "code-review\\escape"])
def test_import_rejects_escaping_paths(tmp_path, path):
    store = SkillImportStore()
    with pytest.raises(InvalidFilePathError):
        store.prepare(manager_for(tmp_path), "local", archive({"SKILL.md": skill_source(), path: "bad"}), "zip")
    assert not (tmp_path / ".agents").exists()


def test_symlink_duplicate_and_oversized_zip_are_rejected(tmp_path):
    store = SkillImportStore()
    manager = manager_for(tmp_path)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as zipped:
        zipped.writestr("SKILL.md", skill_source())
        link = zipfile.ZipInfo("link")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        zipped.writestr(link, "/etc/passwd")
    with pytest.raises(SkillError):
        store.prepare(manager, "local", output.getvalue(), "zip")
    tiny = SkillImportStore(SkillLimits(max_bytes=100, max_files=1))
    with pytest.raises(SkillError):
        tiny.prepare(manager_for(tmp_path, tiny.limits), "local", archive({"SKILL.md": skill_source()}), "zip")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as zipped:
        zipped.writestr("SKILL.md", skill_source())
        with pytest.warns(UserWarning):
            zipped.writestr("SKILL.md", skill_source())
    with pytest.raises(SkillError, match="duplicate"):
        store.prepare(manager, "local", output.getvalue(), "zip")


def test_invalid_preview_and_multiple_skills_never_install(tmp_path):
    store = SkillImportStore()
    manager = manager_for(tmp_path)
    preview = store.prepare(manager, "local", archive({"wrong-name/SKILL.md": skill_source()}), "zip")
    assert not preview["valid"] and preview["token"] is None
    with pytest.raises(SkillError, match="one skill"):
        store.prepare(manager, "local", archive({"one/SKILL.md": skill_source("one"), "two/SKILL.md": skill_source("two")}), "zip")
    assert not (tmp_path / ".agents").exists()


def test_import_tokens_are_scoped_and_cancelled_and_expired(tmp_path, monkeypatch):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    store = SkillImportStore()
    manager = manager_for(first)
    preview = store.prepare(manager, "alice", archive({"SKILL.md": skill_source()}), "zip")
    token = preview["token"]
    temporary = store._imports[token].temporary
    for target, tenant in [(manager_for(second), "alice"), (manager, "bob")]:
        with pytest.raises(SkillError) as error:
            store.commit(token, target, tenant, False)
        assert error.value.status_code == 404
    store.discard(token, manager, "alice")
    assert not temporary.exists()
    preview = store.prepare(manager, "alice", archive({"SKILL.md": skill_source()}), "zip")
    temporary = store._imports[preview["token"]].temporary
    monkeypatch.setattr("project_skills.time.monotonic", lambda: float("inf"))
    with pytest.raises(SkillError):
        store.commit(preview["token"], manager, "alice", False)
    assert not temporary.exists()


def test_project_symlinks_are_not_followed(tmp_path):
    outside = tmp_path / "outside"
    project = tmp_path / "project"
    outside.mkdir()
    project.mkdir()
    (project / ".agents").symlink_to(outside, target_is_directory=True)
    with pytest.raises(InvalidFilePathError):
        manager_for(project).list_skills()
    with pytest.raises(InvalidFilePathError):
        manager_for(project).create("safe", "description")
    assert list(outside.iterdir()) == []


@pytest.mark.asyncio
async def test_skills_api_complete_workflow_and_reload(tmp_path):
    fake = FakeCodex(tmp_path)
    app = create_app(codex_client_factory=lambda: fake, codex_enabled=True,
                     registry=ProjectRegistry([Project("project", "Project", tmp_path)]))
    base = "/api/projects/project/skills"
    async with application_client(app) as client:
        assert (await client.get(base)).json()["data"] == []
        created = await client.post(base, json={"name": "code-review", "description": "Review code."})
        assert created.status_code == 201
        assert created.json()["reload"]["status"] == "reloaded"
        opened = (await client.get(base + "/code-review/file", params={"path": "SKILL.md"})).json()
        saved = await client.put(base + "/code-review/file", json={**opened, "content": opened["content"] + "\nAdded instructions.\n"})
        assert saved.status_code == 200
        stale = await client.put(base + "/code-review/file", json=opened)
        assert stale.status_code == 409
        renamed = await client.patch(base + "/code-review", json={"name": "review-code", "revision": saved.json()["skill"]["revision"]})
        assert renamed.status_code == 200
        assert (await client.get(base + "/code-review")).status_code == 404
        uploaded = await client.post(base + "/review-code/upload", params={"path": "assets/blob.bin", "revision": "missing"}, content=b"\x00\xff")
        assert uploaded.status_code == 200
        assert (await client.get(base + "/review-code/file", params={"path": "assets/blob.bin"})).status_code == 400
        protected = await client.delete(base + "/review-code/file", params={"path": "SKILL.md", "revision": uploaded.json()["skill"]["revision"]})
        assert protected.status_code == 400
        downloaded = await client.get("/api/projects/project/files/download", params={"path": ".agents/skills/review-code"})
        assert downloaded.status_code == 200
        assert any(path.endswith("SKILL.md") for path in zipfile.ZipFile(io.BytesIO(downloaded.content)).namelist())
        deleted = await client.delete(base + "/review-code", params={"revision": uploaded.json()["skill"]["revision"]})
        assert deleted.status_code == 200
        assert (await client.get(base)).json()["data"] == []
        assert (await client.get("/api/projects/unknown/skills")).status_code == 404
        assert fake.skills_reload_requests[-1] == {"cwds": [str(tmp_path)], "forceReload": True}


@pytest.mark.asyncio
async def test_skills_api_supports_claude_code_tool_lists_and_preserves_source(tmp_path):
    tools = "allowed-tools:\n  - Read\n  - Glob\n  - Grep\n  - WebFetch\n  - WebSearch\n"
    fields = "compatibility: 'Claude Code 2.1.220+.'\ncontext: fork\nagent: backend-system-architect\n" + tools
    source = skill_source().replace("x-vendor: true\n", "x-vendor: true\n" + fields)
    root = install_skill(tmp_path)
    (root / "SKILL.md").write_text(source)
    app = create_app(codex_enabled=False, registry=ProjectRegistry([Project("project", "Project", tmp_path)]))
    base = "/api/projects/project/skills"
    async with application_client(app) as client:
        listed = (await client.get(base)).json()["data"]
        assert listed[0]["valid"] and listed[0]["errors"] == []
        detail = (await client.get(base + "/code-review")).json()
        assert detail["valid"]
        opened = (await client.get(base + "/code-review/file", params={"path": "SKILL.md"})).json()
        assert opened["content"] == source
        edited = source + "\nAdded instructions.\n"
        saved = await client.put(base + "/code-review/file", json={**opened, "content": edited})
        assert saved.status_code == 200
        assert saved.json()["skill"]["valid"]
        assert (root / "SKILL.md").read_text() == edited
        opened = (await client.get(base + "/code-review/file", params={"path": "SKILL.md"})).json()
        rejected = await client.put(base + "/code-review/file", json={**opened, "content": edited.replace("  - Read", "  - 42")})
        assert rejected.status_code == 400
        assert "allowed-tools must be a string or a list of strings." in rejected.text
        assert (root / "SKILL.md").read_text() == edited
        renamed = await client.patch(base + "/code-review", json={"name": "review-code", "revision": saved.json()["skill"]["revision"]})
        assert renamed.status_code == 200
        assert renamed.json()["skill"]["valid"]
        assert (tmp_path / ".agents/skills/review-code/SKILL.md").read_text() == edited.replace("name: code-review", 'name: "review-code"')
        preview = await client.post(base + "/imports", content=archive({"SKILL.md": source}))
        assert preview.status_code == 200
        assert preview.json()["valid"] and preview.json()["token"]
        imported = await client.post(base + "/imports/" + preview.json()["token"], json={"replace": False})
        assert imported.status_code == 200
        assert imported.json()["skill"]["valid"]
        assert (root / "SKILL.md").read_text() == source


@pytest.mark.asyncio
async def test_import_api_isolated_between_tenants_and_lifespan_cleans_up(tmp_path):
    root = tmp_path / "tenants"
    alice = root / "alice/workspace"
    bob = root / "bob/workspace"
    alice.mkdir(parents=True)
    bob.mkdir(parents=True)
    app = create_app(codex_enabled=False, tenant_workspace_manager=TenantWorkspaceManager(mode="multi_tenant", base_root=root))
    base = "/api/projects/workspace/skills"
    alice_headers = {"X-Forwarded-User": "subject-alice", "X-Forwarded-Preferred-Username": "alice"}
    bob_headers = {"X-Forwarded-User": "subject-bob", "X-Forwarded-Preferred-Username": "bob"}
    async with application_client(app) as client:
        assert (await client.get(base)).status_code == 401
        preview = await client.post(base + "/imports", content=archive({"SKILL.md": skill_source()}), headers=alice_headers)
        token = preview.json()["token"]
        blocked = await client.post(base + f"/imports/{token}", json={"replace": False}, headers=bob_headers)
        assert blocked.status_code == 404
        assert not (alice / ".agents").exists() and not (bob / ".agents").exists()
        installed = await client.post(base + f"/imports/{token}", json={"replace": False}, headers=alice_headers)
        assert installed.status_code == 200
        assert installed.json()["reload"]["status"] == "unavailable"
        assert (await client.get(base, headers=alice_headers)).json()["data"]
        assert not (await client.get(base, headers=bob_headers)).json()["data"]
        preview = await client.post(base + "/imports", content=archive({"SKILL.md": skill_source()}), headers=alice_headers)
        stage = app.state.skill_imports._imports[preview.json()["token"]].temporary
    assert not stage.exists()
