import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import update

from database import async_session
from main import create_app
from models import Schedule
from projects import Project, ProjectRegistry
from schedules import ScheduledPrompts, next_run_at
from tenancy import TenantWorkspaceManager
from tests.fakes import FakeCodex
from tests.http_client import application_client


@pytest.mark.parametrize(("zone", "time", "weekdays", "after", "expected"), [
    ("Asia/Taipei", "08:00", ["mon"], "2026-11-21T15:00:00+00:00", "2026-11-23T00:00:00+00:00"),
    ("America/New_York", "02:30", [], "2026-03-08T05:00:00+00:00", "2026-03-09T06:30:00+00:00"),
    ("America/New_York", "01:30", [], "2026-11-01T04:00:00+00:00", "2026-11-01T05:30:00+00:00"),
    ("America/New_York", "01:30", [], "2026-11-01T05:30:00+00:00", "2026-11-02T06:30:00+00:00"),
])
def test_next_run_uses_local_time_and_weekday(zone, time, weekdays, after, expected) -> None:
    assert next_run_at(time=time, timezone=zone, days_of_week=weekdays, after=datetime.fromisoformat(after)) == int(
        datetime.fromisoformat(expected).timestamp()
    )


@pytest.mark.asyncio
async def test_schedule_survives_restart_and_dispatches_once_for_its_owner(tmp_path: Path) -> None:
    alice_project = tmp_path / "scheduled-alice" / "workspace"
    alice_project.mkdir(parents=True)
    alice = {"X-Forwarded-User": "scheduled-alice", "X-Forwarded-Preferred-Username": "scheduled-alice"}
    bob = {"X-Forwarded-User": "scheduled-bob", "X-Forwarded-Preferred-Username": "scheduled-bob"}
    payload = {
        "project_key": "workspace", "prompt": "Summarize progress.", "model": "gpt-5.4",
        "reasoning_effort": "low", "frequency": "weekly", "days_of_week": ["wed"], "time": "09:00", "timezone": "Asia/Taipei",
    }
    api = "/api/schedules"

    def make_application():
        manager = TenantWorkspaceManager(mode="multi_tenant", base_root=tmp_path)
        fake = FakeCodex(alice_project)
        application = create_app(codex_client_factory=lambda: fake, codex_enabled=True, tenant_workspace_manager=manager)
        return application, manager, fake

    app, _, _ = make_application()
    async with application_client(app) as client:
        assert (await client.get(api)).status_code == 401
        for changes in ({"timezone": "Mars/Olympus"}, {"time": "24:00"}, {"days_of_week": []}, {"prompt": "  "}):
            assert (await client.post(api, headers=alice, json={**payload, **changes})).status_code == 422
        response = await client.post(api, headers=alice, json=payload)
        assert response.status_code == 201
        schedule = response.json()
        path = f"{api}/{schedule['id']}"
        assert schedule["next_run_at"].endswith("T09:00:00+08:00")
        assert (await client.get(api, headers=bob)).json() == {"data": []}
        assert (await client.put(path, headers=bob, json=payload)).status_code == 404
        assert (await client.patch(path, headers=bob, json={"enabled": False})).status_code == 404
        assert (await client.delete(path, headers=bob)).status_code == 404

        disabled = (await client.patch(path, headers=alice, json={"enabled": False})).json()
        assert disabled["enabled"] is False and disabled["next_run_at"] is None
        updated = await client.put(path, headers=alice, json={**payload, "time": "10:30", "enabled": False})
        assert updated.status_code == 200 and updated.json()["time"] == "10:30"
        still_disabled = await client.put(path, headers=alice, json={**payload, "time": "11:30"})
        assert still_disabled.status_code == 200 and still_disabled.json()["enabled"] is False
        assert still_disabled.json()["next_run_at"] is None
        enabled = await client.patch(path, headers=alice, json={"enabled": True})
        assert enabled.status_code == 200
        assert enabled.json()["next_run_at"].endswith("T11:30:00+08:00")
        still_enabled = await client.put(path, headers=alice, json={**payload, "time": "10:30"})
        assert still_enabled.status_code == 200 and still_enabled.json()["enabled"] is True
        schedule = still_enabled.json()
        assert schedule["next_run_at"].endswith("T10:30:00+08:00")

    app, manager, fake = make_application()
    async with application_client(app) as client:
        runner = ScheduledPrompts(app.state.codex_runtime, manager)
        due = datetime.fromisoformat(schedule["next_run_at"])
        await asyncio.gather(runner.run_due(now=due), runner.run_due(now=due))

        after = (await client.get(api, headers=alice)).json()["data"][0]
        thread_id = after["last_thread_id"]
        assert after["last_run_at"] == schedule["next_run_at"]
        assert after["next_run_at"] > after["last_run_at"]
        assert after["last_run_error"] is None
        assert fake.prompts == [(thread_id, payload["prompt"])]
        assert fake.turn_requests[0][2]["model"] == payload["model"]
        assert fake.turn_requests[0][2]["effort"] == payload["reasoning_effort"]
        thread_path = f"/api/codex/threads/{thread_id}"
        assert (await client.get(thread_path, headers=alice)).status_code == 200
        assert (await client.get(thread_path, headers=bob)).status_code == 404

        missed = datetime.fromisoformat(after["next_run_at"])
        await runner.run_due(now=missed + timedelta(minutes=6))
        after_miss = (await client.get(api, headers=alice)).json()["data"][0]
        assert after_miss["last_thread_id"] == thread_id
        assert datetime.fromisoformat(after_miss["next_run_at"]) > missed
        assert len(fake.prompts) == 1

        (alice_project.parent / "another").mkdir()
        moved = await client.put(path, headers=alice, json={**payload, "project_key": "another"})
        assert moved.status_code == 200 and moved.json()["last_thread_id"] is None
        assert (await client.delete(path, headers=alice)).status_code == 204
        assert (await client.get(api, headers=alice)).json() == {"data": []}
        assert (await client.get(thread_path, headers=alice)).status_code == 200


@pytest.mark.parametrize("through_api", [True, False])
@pytest.mark.asyncio
async def test_deleted_session_keeps_last_run_time(tmp_path: Path, through_api: bool) -> None:
    registry = ProjectRegistry([Project("workspace", "Workspace", tmp_path)])
    fake = FakeCodex(tmp_path)
    app = create_app(codex_client_factory=lambda: fake, codex_enabled=True, registry=registry)
    api = "/api/schedules"
    payload = {
        "project_key": "workspace", "prompt": "Test", "frequency": "daily", "time": "09:00", "timezone": "Asia/Taipei",
    }
    last_run = datetime.fromisoformat("2026-11-19T09:00:00+08:00")
    async with application_client(app) as client:
        created = await client.post(api, json={**payload, "enabled": False})
        assert created.status_code == 201
        schedule_id = created.json()["id"]
        path = f"{api}/{schedule_id}"

        async with async_session() as session:
            await session.execute(
                update(Schedule).where(Schedule.id == schedule_id).values(last_thread_id="thr_one", last_run_at=int(last_run.timestamp()))
            )
            await session.commit()
        schedule = next(item for item in (await client.get(api)).json()["data"] if item["id"] == schedule_id)
        assert schedule["last_thread_id"] == "thr_one"

        if through_api:
            assert (await client.delete("/api/codex/threads/thr_one")).status_code == 204
        else:
            await fake.thread_delete("thr_one")

        schedule = next(item for item in (await client.get(api)).json()["data"] if item["id"] == schedule_id)
        assert schedule["last_thread_id"] is None
        assert schedule["last_run_at"] == last_run.isoformat()
        edited = await client.put(path, json={**payload, "enabled": True})
        assert edited.status_code == 200
        assert edited.json()["last_thread_id"] is None and edited.json()["last_run_at"] == last_run.isoformat()
        toggled = await client.patch(path, json={"enabled": False})
        assert toggled.status_code == 200 and toggled.json()["last_thread_id"] is None
        assert (await client.delete(path)).status_code == 204
