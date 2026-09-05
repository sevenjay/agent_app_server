import asyncio
from io import StringIO
from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import database
from config import BASE_DIR
from database_schema import INITIAL_REVISION, TENANT_REVISION
from main import create_app
from models import Base, Tenant
from tenancy import LOCAL_TENANT_ID, TenantWorkspaceManager
from tests.fakes import FakeCodex
from tests.http_client import application_client


def _config() -> Config:
    config = Config()
    config.set_main_option("script_location", str(BASE_DIR / "alembic"))
    return config


def _legacy_database(path: Path, *, version: str, tenants: bool) -> None:
    command.upgrade(_config(), INITIAL_REVISION)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO app_settings (setting_key, setting_value, updated_at) "
            "VALUES ('selected_project_key', 'workspace', '2026-08-01 12:00:00')"
        )
        connection.execute(
            "INSERT INTO thread_ui_metadata "
            "(thread_id, project_key, pinned, custom_label, last_opened_at, created_at, updated_at) "
            "VALUES ('thr_one', 'workspace', 1, 'Existing session', "
            "'2026-08-01 12:00:00', '2026-07-31 12:00:00', '2026-08-01 12:00:00')"
        )
        if version == "absent":
            connection.execute("DROP TABLE alembic_version")
        elif version == "empty":
            connection.execute("DELETE FROM alembic_version")
    if tenants:
        engine = create_engine(f"sqlite:///{path}")
        try:
            Tenant.__table__.create(engine)
        finally:
            engine.dispose()


@pytest.mark.parametrize("version", ["absent", "empty", "initial"])
@pytest.mark.parametrize("tenants", [False, True])
def test_upgrade_preserves_legacy_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str, tenants: bool,
) -> None:
    path = tmp_path / "legacy.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    _legacy_database(path, version=version, tenants=tenants)
    with sqlite3.connect(path) as connection:
        preferences = connection.execute("SELECT * FROM app_settings").fetchall()
        metadata = connection.execute("SELECT * FROM thread_ui_metadata").fetchall()

    command.upgrade(_config(), "head")
    command.upgrade(_config(), "head")

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("SELECT * FROM alembic_version").fetchall() == [(TENANT_REVISION,)]
        assert connection.execute("SELECT * FROM app_settings").fetchall() == [
            (LOCAL_TENANT_ID, *row) for row in preferences
        ]
        assert connection.execute("SELECT * FROM thread_ui_metadata").fetchall() == [
            (LOCAL_TENANT_ID, *row) for row in metadata
        ]


def test_upgrade_fresh_database_matches_models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "fresh.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    command.upgrade(_config(), "head")
    command.check(_config())


def test_upgrade_adopts_current_unversioned_schema_without_reassigning_tenants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "current.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    engine = create_engine(f"sqlite:///{path}")
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO tenants (tenant_id, external_subject, username, directory_name) "
            "VALUES ('alice-id', 'alice-subject', 'alice', 'alice')"
        )
        for tenant_id in (LOCAL_TENANT_ID, "alice-id"):
            connection.execute(
                "INSERT INTO app_settings (tenant_id, setting_key, setting_value) "
                "VALUES (?, 'selected_project_key', ?)", (tenant_id, tenant_id),
            )
            connection.execute(
                "INSERT INTO thread_ui_metadata (tenant_id, thread_id) VALUES (?, 'thr_one')",
                (tenant_id,),
            )
        before = {
            name: connection.execute(f"SELECT * FROM {name}").fetchall()
            for name in ("tenants", "app_settings", "thread_ui_metadata")
        }

    command.upgrade(_config(), "head")
    command.upgrade(_config(), "head")
    command.check(_config())

    with sqlite3.connect(path) as connection:
        for name, rows in before.items():
            assert connection.execute(f"SELECT * FROM {name}").fetchall() == rows
        assert connection.execute("SELECT * FROM alembic_version").fetchall() == [(TENANT_REVISION,)]


@pytest.mark.parametrize("change", [
    "DROP TABLE app_settings",
    "ALTER TABLE app_settings ADD COLUMN unexpected TEXT",
    "ALTER TABLE app_settings ADD COLUMN tenant_id TEXT",
    "DROP INDEX ix_thread_ui_metadata_project_key",
    "CREATE TABLE tenants (tenant_id TEXT PRIMARY KEY)",
])
def test_upgrade_rejects_unknown_schema_without_changing_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    path = tmp_path / "unknown.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    _legacy_database(path, version="empty", tenants=False)
    with sqlite3.connect(path) as connection:
        connection.execute(change)
        metadata = connection.execute("SELECT * FROM thread_ui_metadata").fetchall()

    with pytest.raises(RuntimeError, match="schema; refusing to adopt"):
        command.upgrade(_config(), "head")

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT * FROM thread_ui_metadata").fetchall() == metadata
        assert connection.execute("SELECT * FROM alembic_version").fetchall() == []


def test_current_does_not_adopt_unversioned_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "legacy.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    _legacy_database(path, version="empty", tenants=True)
    command.current(_config())
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT * FROM alembic_version").fetchall() == []


def test_offline_upgrade_still_emits_complete_schema() -> None:
    config = _config()
    config.output_buffer = StringIO()
    command.upgrade(config, "head", sql=True)
    sql = config.output_buffer.getvalue()
    assert "CREATE TABLE thread_ui_metadata (" in sql
    assert "CREATE TABLE tenants (" in sql
    assert "INSERT INTO app_settings_tenant" in sql


@pytest.mark.asyncio
async def test_legacy_startup_requires_migration_before_serving_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "legacy.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    await asyncio.to_thread(_legacy_database, path, version="absent", tenants=False)
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    monkeypatch.setattr(database, "engine", engine)
    try:
        with pytest.raises(RuntimeError, match="poetry run alembic upgrade head"):
            await database.init_db()
    finally:
        await engine.dispose()
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='tenants'").fetchall() == []


@pytest.mark.asyncio
async def test_migrated_single_user_preferences_and_sessions_work_without_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "legacy.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    await asyncio.to_thread(_legacy_database, path, version="empty", tenants=True)
    await asyncio.to_thread(command.upgrade, _config(), "head")
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    event.listen(engine.sync_engine, "connect", database._configure_sqlite)
    monkeypatch.setattr(database, "engine", engine)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def get_session():
        async with sessions() as session:
            yield session

    root = tmp_path / "projects"
    project = root / "workspace"
    project.mkdir(parents=True)
    fake = FakeCodex(project)
    application = create_app(
        codex_client_factory=lambda: fake,
        codex_enabled=True,
        tenant_workspace_manager=TenantWorkspaceManager(mode="single_user", base_root=root),
    )
    application.dependency_overrides[database.get_session] = get_session
    try:
        await database.init_db()
        async with application_client(application) as client:
            preferences = await client.get("/api/preferences")
            assert preferences.status_code == 200
            assert preferences.json() == {"selected_project_key": "workspace"}
            projects = await client.get("/api/projects")
            assert projects.status_code == 200
            assert projects.json()["data"][0]["path"] == str(project.resolve())
            threads = await client.get("/api/codex/threads", params={"project_key": "workspace"})
            assert threads.status_code == 200
            thread = next(row for row in threads.json()["data"] if row["id"] == "thr_one")
            assert thread["pinned"] is True
            assert thread["custom_label"] == "Existing session"
            partial = await client.get("/partials/threads", params={"project_key": "workspace"})
            assert partial.status_code == 200
            assert "Existing session" in partial.text
            updated = await client.patch("/api/preferences", json={"selected_thread_id": "thr_one"})
            assert updated.status_code == 200
            assert (await client.get("/api/preferences")).json()["selected_thread_id"] == "thr_one"
    finally:
        await engine.dispose()
