from pathlib import Path

import pytest
from openai_codex.errors import InvalidRequestError

from database import async_session
from main import create_app
from models import ThreadUIMetadata
from projects import ProjectRegistry
from tenancy import LOCAL_TENANT_ID
from tests.fakes import FakeCodex
from tests.http_client import application_client


def project_application(tmp_path: Path):
    root = tmp_path / "projects"
    project = root / "actions_demo"
    project.mkdir(parents=True)
    (project / "notes.txt").write_text("keep these notes", encoding="utf-8")
    fake = FakeCodex(project)
    app = create_app(
        codex_client_factory=lambda: fake,
        codex_enabled=True,
        registry=ProjectRegistry.from_root(root),
    )
    return app, fake, project


@pytest.mark.asyncio
async def test_project_rename_preserves_sessions_and_delete_removes_files(tmp_path: Path):
    app, fake, project = project_application(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (project / "outside-link").symlink_to(outside)
    async with application_client(app) as client:
        assert (await client.patch("/api/codex/threads/thr_one", json={"pinned": True})).status_code == 200
        await client.patch("/api/preferences", json={"selected_project_key": "actions_demo", "selected_thread_id": "thr_one"})
        renamed = await client.patch("/api/projects/actions_demo", json={"name": "新專案 & notes"})
        assert renamed.status_code == 200
        updated = renamed.json()
        new_path = Path(updated["path"])
        assert not project.exists()
        assert new_path.name == "新專案 & notes"
        assert (new_path / "notes.txt").read_text(encoding="utf-8") == "keep these notes"
        assert fake.threads["thr_one"]["cwd"] == str(new_path)
        assert fake.threads["thr_archived"]["cwd"] == str(new_path)
        assert fake.threads["thr_archived"]["archived"] is True
        thread = await client.get("/api/codex/threads/thr_one")
        assert thread.status_code == 200
        assert thread.json()["project_key"] == updated["key"]
        assert thread.json()["pinned"] is True
        assert (await client.get("/api/preferences")).json()["selected_project_key"] == updated["key"]
        listed = await client.get("/partials/projects")
        assert "新專案 &amp; notes" in listed.text
        assert "actions_demo" not in listed.text

        deleted = await client.delete(f'/api/projects/{updated["key"]}')
        assert deleted.status_code == 204
        assert not new_path.exists()
        assert outside.read_text(encoding="utf-8") == "outside"
        assert (await client.get("/api/projects")).json() == {"data": []}
        assert (await client.get("/api/preferences")).json() == {}
        assert (await client.get("/api/codex/threads/thr_one")).status_code == 404
    async with async_session() as session:
        assert await session.get(ThreadUIMetadata, (LOCAL_TENANT_ID, "thr_one")) is None


@pytest.mark.asyncio
async def test_project_actions_reject_active_sessions_invalid_names_and_collisions(tmp_path: Path):
    app, _fake, project = project_application(tmp_path)
    (project.parent / "occupied").mkdir()
    async with application_client(app) as client:
        assert (await client.patch("/api/projects/actions_demo", json={"name": "../outside"})).status_code == 422
        assert (await client.patch("/api/projects/actions_demo", json={"name": "occupied"})).status_code == 409
        assert (await client.delete("/api/projects/unknown")).status_code == 404
        manager = app.state.codex_runtime.turn_manager
        await manager.reserve("thr_one", owner_id=LOCAL_TENANT_ID)
        try:
            assert (await client.patch("/api/projects/actions_demo", json={"name": "renamed"})).status_code == 409
            assert (await client.delete("/api/projects/actions_demo")).status_code == 409
        finally:
            await manager.finish("thr_one", owner_id=LOCAL_TENANT_ID)
        assert project.is_dir()
        assert not (project.parent / "renamed").exists()
        # Failed operations release their locks, allowing the next rename.
        assert (await client.patch("/api/projects/actions_demo", json={"name": "renamed"})).status_code == 200


@pytest.mark.asyncio
async def test_failed_session_relocation_restores_project_directory_and_sessions(tmp_path: Path):
    app, fake, project = project_application(tmp_path)
    resume = fake.thread_resume
    failed = False

    async def fail_second_session(thread_id, **kwargs):
        nonlocal failed
        if thread_id == "thr_two" and not failed:
            failed = True
            raise InvalidRequestError(-32600, "thread already has an active writer")
        return await resume(thread_id, **kwargs)

    fake.thread_resume = fail_second_session
    async with application_client(app) as client:
        response = await client.patch("/api/projects/actions_demo", json={"name": "renamed"})
        assert response.status_code == 503
        assert project.is_dir()
        assert not (project.parent / "renamed").exists()
        assert fake.threads["thr_one"]["cwd"] == str(project)
        assert fake.threads["thr_two"]["cwd"] == str(project)
        assert (await client.get("/api/codex/threads/thr_one")).status_code == 200
