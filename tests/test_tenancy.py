from pathlib import Path

import pytest

from main import create_app
from tenancy import TenantWorkspaceManager
from tests.fakes import FakeCodex
from tests.http_client import application_client


def _headers(subject: str, username: str) -> dict[str, str]:
    return {
        "X-Forwarded-User": subject,
        "X-Forwarded-Preferred-Username": username,
    }


@pytest.mark.asyncio
async def test_multi_tenant_requires_proxy_identity_and_scopes_workspaces(
    tmp_path: Path,
) -> None:
    root = tmp_path / "tenant-workspaces"
    alice_project = root / "alice" / "workspace"
    bob_project = root / "bob" / "workspace"
    alice_project.mkdir(parents=True)
    bob_project.mkdir(parents=True)
    manager = TenantWorkspaceManager(
        mode="multi_tenant",
        base_root=root,
    )
    fake = FakeCodex(alice_project)
    application = create_app(
        codex_client_factory=lambda: fake,
        codex_enabled=True,
        tenant_workspace_manager=manager,
    )

    async with application_client(application) as client:
        status = await client.get("/api/status")
        assert status.status_code == 200
        assert status.json()["deployment_mode"] == "multi_tenant"

        missing = await client.get("/api/projects")
        assert missing.status_code == 401
        assert missing.json()["error"]["code"] == "web_identity_required"

        invalid = await client.get(
            "/api/projects",
            headers=_headers("subject-invalid", "../invalid"),
        )
        assert invalid.status_code == 403
        assert invalid.json()["error"]["code"] == "invalid_web_identity"

        alice = await client.get(
            "/api/projects",
            headers=_headers("subject-alice", "Alice"),
        )
        bob = await client.get(
            "/api/projects",
            headers=_headers("subject-bob", "bob"),
        )
        assert alice.json()["data"][0]["path"] == str(alice_project.resolve())
        assert bob.json()["data"][0]["path"] == str(bob_project.resolve())

        alice_thread = await client.get(
            "/api/codex/threads/thr_one",
            headers=_headers("subject-alice", "alice"),
        )
        assert alice_thread.status_code == 200
        hidden_from_bob = await client.get(
            "/api/codex/threads/thr_one",
            headers=_headers("subject-bob", "bob"),
        )
        assert hidden_from_bob.status_code == 404

        updated = await client.patch(
            "/api/preferences",
            headers=_headers("subject-alice", "alice"),
            json={"selected_project_key": "workspace"},
        )
        assert updated.status_code == 200
        alice_preferences = await client.get(
            "/api/preferences",
            headers=_headers("subject-alice", "alice"),
        )
        bob_preferences = await client.get(
            "/api/preferences",
            headers=_headers("subject-bob", "bob"),
        )
        assert alice_preferences.json() == {"selected_project_key": "workspace"}
        assert bob_preferences.json() == {}

        renamed = await client.get(
            "/api/projects",
            headers=_headers("subject-alice", "Alice-Renamed"),
        )
        assert renamed.json()["data"][0]["path"] == str(alice_project.resolve())
        assert not (root / "alice-renamed").exists()

        conflict = await client.get(
            "/api/projects",
            headers=_headers("different-subject", "alice"),
        )
        assert conflict.status_code == 403
        assert conflict.json()["error"]["code"] == "web_identity_conflict"


@pytest.mark.asyncio
async def test_multi_tenant_lazily_creates_private_user_and_project_directories(
    tmp_path: Path,
) -> None:
    root = tmp_path / "tenant-workspaces"
    root.mkdir()
    manager = TenantWorkspaceManager(
        mode="multi_tenant",
        base_root=root,
    )
    fake = FakeCodex(root)
    application = create_app(
        codex_client_factory=lambda: fake,
        codex_enabled=True,
        tenant_workspace_manager=manager,
    )

    async with application_client(application) as client:
        projects = await client.get(
            "/api/projects",
            headers=_headers("subject-charlie", "Charlie"),
        )
        assert projects.json() == {"data": []}
        created = await client.post(
            "/api/projects",
            headers=_headers("subject-charlie", "charlie"),
            json={"name": "first-project"},
        )
        assert created.status_code == 201

    user_root = root / "charlie"
    project_root = user_root / "first-project"
    assert user_root.is_dir()
    assert project_root.is_dir()
    assert user_root.stat().st_mode & 0o777 == 0o700
    assert project_root.stat().st_mode & 0o777 == 0o700


@pytest.mark.asyncio
async def test_single_user_ignores_proxy_headers_and_keeps_existing_layout(
    tmp_path: Path,
) -> None:
    root = tmp_path / "single-workspaces"
    project = root / "workspace"
    project.mkdir(parents=True)
    manager = TenantWorkspaceManager(
        mode="single_user",
        base_root=root,
    )
    fake = FakeCodex(project)
    application = create_app(
        codex_client_factory=lambda: fake,
        codex_enabled=True,
        tenant_workspace_manager=manager,
    )

    async with application_client(application) as client:
        projects = await client.get(
            "/api/projects",
            headers=_headers("spoofed-subject", "mallory"),
        )

    assert projects.status_code == 200
    assert projects.json()["data"][0]["path"] == str(project.resolve())
    assert not (root / "mallory").exists()
