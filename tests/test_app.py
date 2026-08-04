import asyncio
import tempfile
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi import Request

import main
from database import async_session
from main import create_app
from models import ThreadUIMetadata
from projects import Project, ProjectRegistry
from tests.fakes import FakeCodex
from tests.http_client import application_client


class ElementAttributeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.elements: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self.elements.append((tag, dict(attrs)))


def fake_application():
    project_directory = tempfile.TemporaryDirectory(prefix="app-journal-test-")
    project_path = Path(project_directory.name).resolve()
    fake = FakeCodex(project_path)
    registry = ProjectRegistry(
        [Project("agent_app_server", "Agent App Server", project_path)]
    )
    application = create_app(
        codex_client_factory=lambda: fake,
        codex_enabled=True,
        registry=registry,
    )
    application.state.test_project_directory = project_directory
    application.state.test_project_path = project_path
    return application, fake


def test_recent_plan_history_keeps_the_latest_three_in_order() -> None:
    thread = {
        "turns": [
            {
                "items": [
                    {"id": "plan-1", "type": "plan", "text": "First"},
                    {"id": "note-1", "type": "agentMessage", "text": "Ignore"},
                ]
            },
            {
                "items": [
                    {"id": "plan-2", "type": "plan", "text": "Second"},
                    {"id": "plan-3", "type": "plan", "text": "Third"},
                    {"id": "plan-4", "type": "plan", "text": "Fourth"},
                ]
            },
        ]
    }

    assert main._recent_plan_history(thread) == [
        {"key": "plan-2", "text": "Second"},
        {"key": "plan-3", "text": "Third"},
        {"key": "plan-4", "text": "Fourth"},
    ]


@pytest.mark.asyncio
async def test_debug_logs_correlate_console_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages: list[str] = []

    def record_debug(message: str, *_args, **_kwargs) -> None:
        messages.append(message)

    monkeypatch.setattr(main, "LOGD", record_debug)
    application, _fake = fake_application()
    async with application_client(application) as client:
        response = await client.get(
            "/partials/threads/does_not_exist/timeline",
        )

    assert response.status_code == 404
    request_id = response.headers["x-request-id"]
    assert any(
        f"console_request_start request_id={request_id} " in message
        and "path=/partials/threads/does_not_exist/timeline" in message
        for message in messages
    )
    assert any(
        f"console_request_error request_id={request_id} " in message
        and "status=404 code=not_found" in message
        for message in messages
    )
    assert any(
        f"console_request_complete request_id={request_id} " in message
        and "status=404" in message
        for message in messages
    )


@pytest.mark.asyncio
async def test_status_api_and_static_shell_with_codex_disabled() -> None:
    async with application_client(main.app) as client:
        response = await client.get("/api/status")
        assert response.status_code == 200
        payload = response.json()
        assert payload["database"] == {
            "connected": True,
            "journal_mode": "wal",
        }
        assert payload["environment"] == "development"
        assert payload["scheduler"]["running"] is False
        assert payload["codex"]["enabled"] is False
        assert payload["codex"]["ready"] is False

        shell = await client.get("/")
        assert shell.status_code == 200
        assert "Codex Console" in shell.text
        assert "htmx.org@2.0.4" in shell.text
        assert "alpinejs@3.14.9" in shell.text

        runtime_status = await client.get("/partials/codex/status")
        assert runtime_status.status_code == 200
        assert "Runtime health" in runtime_status.text
        assert "0 turns running across all sessions" in runtime_status.text
        assert "Live updates" in runtime_status.text
        assert "0 live connections" in runtime_status.text
        assert "Permissions" in runtime_status.text
        assert "Agents.md" in runtime_status.text
        assert "Account" in runtime_status.text
        assert "Limit" in runtime_status.text
        assert runtime_status.text.count('role="tooltip"') == 7

        unavailable = await client.get("/api/codex/account")
        assert unavailable.status_code == 503
        assert unavailable.json()["error"]["code"] == "codex_unavailable"


@pytest.mark.asyncio
async def test_account_models_projects_and_thread_crud() -> None:
    application, fake = fake_application()
    async with application_client(application) as client:
        runtime_status = await client.get("/partials/codex/status")
        assert "workspace_write" in runtime_status.text
        assert "auto_review" in runtime_status.text
        assert "full_access = danger-full-access" in runtime_status.text
        assert "deny_all = CLI never" in runtime_status.text
        assert "developer@example.com (Business)" in runtime_status.text
        assert "Monthly limit" in runtime_status.text
        assert "75% left" in runtime_status.text
        assert "Weekly limit" in runtime_status.text
        assert "82% left" in runtime_status.text

        projects = await client.get("/api/projects")
        assert projects.json() == {
            "data": [
                {
                    "key": "agent_app_server",
                    "name": "Agent App Server",
                    "path": str(application.state.test_project_path),
                }
            ]
        }
        assert (await client.get("/api/codex/account")).status_code == 200
        models = await client.get("/api/codex/models")
        assert models.json()["data"][0]["id"] == "gpt-test"
        assert models.json()["data"][0]["default_reasoning_effort"] == "medium"
        assert {
            option["reasoning_effort"]
            for option in models.json()["data"][0]["supported_reasoning_efforts"]
        } >= {"low", "medium", "high", "xhigh", "max", "ultra"}

        page = await client.get(
            "/api/codex/threads",
            params={"project_key": "agent_app_server"},
        )
        assert page.status_code == 200
        assert {item["id"] for item in page.json()["data"]} == {
            "thr_one",
            "thr_two",
        }

        created = await client.post(
            "/api/codex/threads",
            json={
                "project_key": "agent_app_server",
                "name": "API created",
                "model": None,
            },
        )
        assert created.status_code == 201
        created_id = created.json()["id"]

        updated = await client.patch(
            f"/api/codex/threads/{created_id}",
            json={"name": "Updated", "pinned": True, "custom_label": "Important"},
        )
        assert updated.status_code == 200
        assert updated.json()["name"] == "Updated"
        assert updated.json()["pinned"] is True
        assert updated.json()["custom_label"] == "Important"

        forked = await client.post("/api/codex/threads/thr_one/fork")
        assert forked.status_code == 201
        assert forked.json()["id"] == "thr_one_fork"
        archived_response = await client.post("/api/codex/threads/thr_one/archive")
        assert archived_response.status_code == 200
        archived = await client.get(
            "/api/codex/threads",
            params={"project_key": "agent_app_server", "archived": True},
        )
        assert "thr_one" in {item["id"] for item in archived.json()["data"]}
        unarchived = await client.post("/api/codex/threads/thr_one/unarchive")
        assert unarchived.status_code == 200
        deleted = await client.delete(f"/api/codex/threads/{created_id}")
        assert deleted.status_code == 204
        assert deleted.content == b""
        assert fake.thread_delete_requests == [created_id]
        assert (await client.get(f"/api/codex/threads/{created_id}")).status_code == 404
        refreshed = await client.get(
            "/partials/threads",
            params={
                "project_key": "agent_app_server",
                "cache_bust": "after-delete",
            },
        )
        assert refreshed.headers["cache-control"] == "no-store"
        assert created_id not in refreshed.text

    async with async_session() as session:
        assert await session.get(ThreadUIMetadata, created_id) is None


@pytest.mark.asyncio
async def test_static_registry_rejects_project_creation() -> None:
    application, _fake = fake_application()

    async with application_client(application) as client:
        response = await client.post(
            "/api/projects",
            json={"name": "not-configured"},
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "project_root_unavailable"


@pytest.mark.asyncio
async def test_project_root_discovery_creation_and_new_session(
    tmp_path: Path,
) -> None:
    root = tmp_path / "codex-root"
    existing_path = root / "project1"
    existing_path.mkdir(parents=True)
    hidden_path = root / "private"
    hidden_path.mkdir()
    registry = ProjectRegistry.from_settings(
        type(
            "Settings",
            (),
            {
                "current_env": "development",
                "codex_projects_root": root,
                "codex_hidden_projects": ["private"],
            },
        )()
    )
    assert registry.get("private").path == hidden_path.resolve()
    fake = FakeCodex(existing_path)
    application = create_app(
        codex_client_factory=lambda: fake,
        codex_enabled=True,
        registry=registry,
    )

    async with application_client(application) as client:
        projects = await client.get("/api/projects")
        assert projects.json() == {
            "data": [
                {
                    "key": "project1",
                    "name": "project1",
                    "path": str(existing_path.resolve()),
                }
            ]
        }

        created = await client.post(
            "/api/projects",
            json={"name": "project2"},
        )
        assert created.status_code == 201
        assert created.json() == {
            "key": "project2",
            "name": "project2",
            "path": str((root / "project2").resolve()),
        }
        assert (root / "project2").is_dir()
        duplicate = await client.post(
            "/api/projects",
            json={"name": "project2"},
        )
        assert duplicate.status_code == 409
        assert duplicate.json()["error"]["code"] == "project_exists"
        traversal = await client.post(
            "/api/projects",
            json={"name": "../outside"},
        )
        assert traversal.status_code == 422
        assert not (tmp_path / "outside").exists()

        partial = await client.get("/partials/projects")
        assert partial.status_code == 200
        assert "project1" in partial.text
        assert "project2" in partial.text
        assert "private" not in partial.text
        assert str((root / "project2").resolve()) in partial.text

        session = await client.post(
            "/api/codex/threads",
            json={
                "project_key": "project2",
                "name": None,
                "model": None,
            },
        )
        assert session.status_code == 201
        assert session.json()["project_key"] == "project2"
        assert fake.threads[session.json()["id"]]["cwd"] == str(
            (root / "project2").resolve()
        )


@pytest.mark.asyncio
async def test_new_unlisted_thread_can_refresh_every_panel_and_connect_sse(
    tmp_path: Path,
) -> None:
    class UnlistedNewThreadFake(FakeCodex):
        async def thread_list(self, **kwargs):
            response = await super().thread_list(**kwargs)
            response.data = [
                thread
                for thread in response.data
                if not thread["id"].startswith("thr_created_")
            ]
            return response

    project_path = tmp_path.resolve()
    fake = UnlistedNewThreadFake(project_path)
    registry = ProjectRegistry(
        [Project("agent_app_server", "Agent App Server", project_path)]
    )
    application = create_app(
        codex_client_factory=lambda: fake,
        codex_enabled=True,
        registry=registry,
    )

    async with application_client(application) as client:
        created = await client.post(
            "/api/codex/threads",
            json={
                "project_key": "agent_app_server",
                "name": "Fresh session",
                "model": None,
            },
        )
        assert created.status_code == 201
        thread_id = created.json()["id"]

        listed = await client.get(
            "/partials/threads",
            params={"project_key": "agent_app_server"},
        )
        assert listed.status_code == 200
        assert thread_id in listed.text

        preference = await client.patch(
            "/api/preferences",
            json={"selected_thread_id": thread_id},
        )
        assert preference.status_code == 200

        for panel in ("timeline", "inspector", "changes", "composer"):
            partial = await client.get(
                f"/partials/threads/{thread_id}/{panel}",
            )
            assert partial.status_code == 200

        route = next(
            route
            for route in application.routes
            if getattr(route, "path", "")
            == "/api/codex/threads/{thread_id}/events"
        )
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": f"/api/codex/threads/{thread_id}/events",
                "headers": [],
                "query_string": b"",
                "server": ("testserver", 80),
                "client": ("testclient", 1),
                "scheme": "http",
                "app": application,
            }
        )
        response = await route.endpoint(request, thread_id)
        first_chunk = await anext(response.body_iterator)
        assert "console.stream.ready" in first_chunk
        await response.body_iterator.aclose()


@pytest.mark.asyncio
async def test_thread_partial_exposes_cursor_load_more() -> None:
    application, fake = fake_application()
    for index in range(35):
        thread_id = f"thr_page_{index}"
        fake.threads[thread_id] = fake._thread(thread_id, name=f"Page {index}")
    async with application_client(application) as client:
        first = await client.get(
            "/partials/threads",
            params={"project_key": "agent_app_server"},
        )
        assert first.status_code == 200
        assert first.headers["cache-control"] == "no-store"
        assert "Load more sessions" in first.text
        assert "cursor=" in first.text


@pytest.mark.asyncio
async def test_turn_conflict_steer_interrupt_and_allow_list() -> None:
    application, fake = fake_application()
    async with application_client(application) as client:
        first = await client.post(
            "/api/codex/threads/thr_one/turns",
            json={
                "prompt": "Run checks",
                "model": None,
                "reasoning_effort": "max",
            },
        )
        assert first.status_code == 202
        assert first.json()["accepted"] is True
        assert fake.turn_requests[0][2]["effort"] == "max"
        conflict = await client.post(
            "/api/codex/threads/thr_one/turns",
            json={"prompt": "Duplicate"},
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "active_turn_conflict"

        other = await client.post(
            "/api/codex/threads/thr_two/turns",
            json={"prompt": "Parallel"},
        )
        assert other.status_code == 202
        steered = await client.post(
            "/api/codex/threads/thr_one/steer",
            json={"prompt": "Focus on API"},
        )
        assert steered.status_code == 202
        interrupted = await client.post(
            "/api/codex/threads/thr_one/interrupt"
        )
        assert interrupted.status_code == 202
        outside = await client.get("/api/codex/threads/thr_outside")
        assert outside.status_code == 404
        invalid = await client.post(
            "/api/codex/threads/thr_two/turns",
            json={"prompt": "   "},
        )
        assert invalid.status_code == 422
        invalid_effort = await client.post(
            "/api/codex/threads/thr_two/turns",
            json={"prompt": "Valid prompt", "reasoning_effort": "extreme"},
        )
        assert invalid_effort.status_code == 422


@pytest.mark.asyncio
async def test_goal_api_set_view_pause_resume_clear_and_validation() -> None:
    application, fake = fake_application()
    async with application_client(application) as client:
        empty = await client.get("/api/codex/threads/thr_one/goal")
        assert empty.status_code == 200
        assert empty.json() == {"goal": None}

        invalid = await client.post(
            "/api/codex/threads/thr_one/goal",
            json={"objective": "   ", "token_budget": 0},
        )
        assert invalid.status_code == 422
        invalid_effort = await client.post(
            "/api/codex/threads/thr_one/goal",
            json={"objective": "Valid objective", "reasoning_effort": "extreme"},
        )
        assert invalid_effort.status_code == 422

        started = await client.post(
            "/api/codex/threads/thr_one/goal",
            json={
                "objective": "Finish the release",
                "token_budget": 20_000,
                "model": "gpt-test",
                "reasoning_effort": "xhigh",
            },
        )
        assert started.status_code == 202
        assert started.json()["model"] == "gpt-test"
        assert started.json()["reasoning_effort"] == "xhigh"
        assert started.json()["goal"]["objective"] == "Finish the release"
        assert started.json()["goal"]["token_budget"] == 20_000
        assert fake.thread_resume_requests[-1][1]["model"] == "gpt-test"
        assert fake.thread_resume_requests[-1][1]["config"] == {
            "model_reasoning_effort": "xhigh"
        }
        conflict = await client.post(
            "/api/codex/threads/thr_one/turns",
            json={"prompt": "Conflicting turn"},
        )
        assert conflict.status_code == 409

        viewed = await client.get("/api/codex/threads/thr_one/goal")
        assert viewed.json()["goal"]["status"] == "active"
        inspector = await client.get("/partials/threads/thr_one/inspector")
        assert inspector.status_code == 200
        assert "Finish the release" in inspector.text
        assert '"token_budget": 20000' in inspector.text
        paused = await client.patch(
            "/api/codex/threads/thr_one/goal",
            json={"status": "paused"},
        )
        assert paused.status_code == 202
        assert paused.json()["goal"]["status"] == "paused"
        for _ in range(20):
            if not await application.state.codex_runtime.turn_manager.is_active("thr_one"):
                break
            await asyncio.sleep(0)

        resumed = await client.patch(
            "/api/codex/threads/thr_one/goal",
            json={
                "status": "active",
                "model": "gpt-test",
                "reasoning_effort": "xhigh",
            },
        )
        assert resumed.status_code == 202
        assert resumed.json()["model"] == "gpt-test"
        assert resumed.json()["reasoning_effort"] == "xhigh"
        assert resumed.json()["goal"]["status"] == "active"
        assert fake.thread_resume_requests[-1][1]["model"] == "gpt-test"
        assert fake.thread_resume_requests[-1][1]["config"] == {
            "model_reasoning_effort": "xhigh"
        }
        cleared = await client.delete("/api/codex/threads/thr_one/goal")
        assert cleared.status_code == 200
        assert cleared.json()["cleared"] is True
        assert (await client.get("/api/codex/threads/thr_one/goal")).json() == {
            "goal": None
        }
        assert (await client.get("/api/codex/threads/thr_outside/goal")).status_code == 404


@pytest.mark.asyncio
async def test_partials_escape_model_content_and_metadata_preferences() -> None:
    application, fake = fake_application()
    fake.threads["thr_one"]["turns"][0]["items"][1]["text"] = (
        "<script>alert('unsafe')</script>"
    )
    fake.threads["thr_one"]["turns"][0]["items"].append(
        {
            "id": "search-1",
            "type": "webSearch",
            "query": "outer query should stay hidden",
            "action": {
                "type": "search",
                "queries": ["first query", "second query"],
            },
        }
    )
    async with application_client(application) as client:
        projects = await client.get("/partials/projects")
        assert projects.status_code == 200
        assert "Agent App Server" in projects.text

        timeline = await client.get("/partials/threads/thr_one/timeline")
        assert timeline.status_code == 200
        assert "<script>alert('unsafe')</script>" not in timeline.text
        assert "&lt;script&gt;" in timeline.text
        assert "pytest -q" in timeline.text
        assert "12 passed" in timeline.text
        assert "webSearch" in timeline.text
        assert "first query" in timeline.text
        assert "second query" in timeline.text
        assert ">search</dd>" in timeline.text
        assert ">first query · second query</dd>" in timeline.text
        assert "outer query should stay hidden" not in timeline.text
        changes = await client.get("/partials/threads/thr_one/changes")
        assert changes.status_code == 200
        assert "Latest changes" in changes.text
        assert "main.py" in changes.text
        assert "1 file in the latest update" in changes.text

        saved = await client.patch(
            "/api/preferences",
            json={
                "selected_project_key": "agent_app_server",
                "selected_thread_id": "thr_one",
            },
        )
        assert saved.status_code == 200
        preferences = (await client.get("/api/preferences")).json()
        assert preferences["selected_project_key"] == "agent_app_server"
        assert preferences["selected_thread_id"] == "thr_one"
        cleared = await client.patch(
            "/api/preferences",
            json={"selected_thread_id": None},
        )
        assert cleared.status_code == 200
        assert cleared.json() == {"selected_thread_id": None}
        preferences = (await client.get("/api/preferences")).json()
        assert preferences["selected_project_key"] == "agent_app_server"
        assert "selected_thread_id" not in preferences
        unknown_project = await client.patch(
            "/api/preferences",
            json={"selected_project_key": "unknown"},
        )
        assert unknown_project.status_code == 404


@pytest.mark.asyncio
async def test_thread_snapshot_exposes_durable_journal_cursor_and_coverage() -> None:
    application, _fake = fake_application()
    async with application_client(application) as client:
        response = await client.get("/api/codex/threads/thr_one/snapshot")

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == "thr_one"
    assert isinstance(payload["journal_cursor"], int)
    assert payload["journal_cursor"] > 0
    assert payload["journal_coverage"] in {"complete", "partial"}
    assert any(
        item["type"] == "commandExecution"
        for turn in payload["turns"]
        for item in turn["items"]
    )


@pytest.mark.asyncio
async def test_rendered_fragment_alpine_attributes_are_valid_html() -> None:
    application, fake = fake_application()
    fake.threads["thr_one"]["status"] = {"type": "idle"}
    async with application_client(application) as client:
        projects = await client.get("/partials/projects")
        threads = await client.get(
            "/partials/threads",
            params={"project_key": "agent_app_server"},
        )
        timeline = await client.get("/partials/threads/thr_one/timeline")
        composer = await client.get("/partials/threads/thr_one/composer")
        inspector = await client.get("/partials/threads/thr_one/inspector")

    parser = ElementAttributeParser()
    parser.feed(
        projects.text
        + threads.text
        + timeline.text
        + composer.text
        + inspector.text
    )
    attributes = [attrs for _tag, attrs in parser.elements]

    assert any(
        attrs.get("@click") == 'selectProject("agent_app_server")'
        for attrs in attributes
    )
    assert any(
        attrs.get("@click") == 'selectThread("thr_one")'
        for attrs in attributes
    )
    assert any(
        attrs.get("x-show") == 'false || isRunning("thr_one")'
        for attrs in attributes
    )
    composer_init = next(
        attrs["x-init"]
        for attrs in attributes
        if attrs.get("x-init", "").startswith(
            'syncThreadActive("thr_one", false, null, null, null)'
        )
    )
    assert '$watch("prompt"' in composer_init
    assert "resizeComposer($refs.composerInput)" in composer_init
    assert ">{'type': 'idle'}<" not in inspector.text
    assert ">idle</dd>" in inspector.text
    assert any(
        attrs.get("x-init") == 'syncSessionStatus("thr_one", {"type": "idle"})'
        for attrs in attributes
    )
    assert any(attrs.get("x-text") == "sessionStatusLabel" for attrs in attributes)
    assert any(attrs.get("x-text") == "currentModelId || 'default'" for attrs in attributes)
    assert any(attrs.get("x-text") == "currentReasoningEffortLabel" for attrs in attributes)
    assert any(
        attrs.get("x-init", "").startswith("syncPlanHistory(")
        for attrs in attributes
    )
    assert any(
        attrs.get("x-init") == 'syncGoalSnapshot("thr_one", null)'
        for attrs in attributes
    )
    assert any(
        attrs.get("x-text") == "formatTokenCount(liveUsage.total.total, true)"
        for attrs in attributes
    )
    assert any(
        attrs.get("x-text") == "liveToolOutput(item.tool)"
        for attrs in attributes
    )


@pytest.mark.asyncio
async def test_inspector_bootstraps_sdk_waiting_status() -> None:
    application, fake = fake_application()
    fake.threads["thr_one"]["status"] = {
        "type": "active",
        "active_flags": ["waitingOnUserInput"],
    }

    async with application_client(application) as client:
        inspector = await client.get("/partials/threads/thr_one/inspector")

    parser = ElementAttributeParser()
    parser.feed(inspector.text)
    attributes = [attrs for _tag, attrs in parser.elements]
    assert ">active</dd>" in inspector.text
    assert any(
        attrs.get("x-init")
        == (
            'syncSessionStatus("thr_one", '
            '{"active_flags": ["waitingOnUserInput"], "type": "active"})'
        )
        for attrs in attributes
    )


def test_codex_and_partial_routes_use_web_user_dependency() -> None:
    protected_routes = [
        route
        for route in main.app.routes
        if getattr(route, "path", "").startswith("/api/codex/")
        or getattr(route, "path", "").startswith("/api/projects")
        or getattr(route, "path", "").startswith("/partials/")
    ]
    assert protected_routes
    for route in protected_routes:
        dependency_calls = {
            dependency.call for dependency in route.dependant.dependencies
        }
        assert main.require_web_user in dependency_calls, route.path


@pytest.mark.asyncio
async def test_sse_response_headers_ready_event_and_cleanup() -> None:
    application, _fake = fake_application()
    runtime = application.state.codex_runtime
    await runtime.start()
    route = next(
        route
        for route in application.routes
        if getattr(route, "path", "") == "/api/codex/threads/{thread_id}/events"
    )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/codex/threads/thr_one/events",
            "headers": [],
            "query_string": b"",
            "server": ("testserver", 80),
            "client": ("testclient", 1),
            "scheme": "http",
            "app": application,
        }
    )
    response = await route.endpoint(request, "thr_one")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    assert response.media_type == "text/event-stream"
    first_chunk = await anext(response.body_iterator)
    assert "console.stream.ready" in first_chunk
    await response.body_iterator.aclose()
    assert await runtime.event_hub.subscriber_count() == 0

    first = await runtime.event_hub.publish(
        "thr_one",
        event_type="codex.notification",
        method="event/first",
    )
    second = await runtime.event_hub.publish(
        "thr_one",
        event_type="codex.notification",
        method="event/second",
    )
    request_with_query_cursor = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/codex/threads/thr_one/events",
            "headers": [],
            "query_string": f"after_sequence={first.sequence}".encode(),
            "server": ("testserver", 80),
            "client": ("testclient", 1),
            "scheme": "http",
            "app": application,
        }
    )
    query_replay_response = await route.endpoint(
        request_with_query_cursor,
        "thr_one",
        after_sequence=first.sequence,
    )
    replay_chunk = await anext(query_replay_response.body_iterator)
    assert f"id: {second.sequence}" in replay_chunk
    assert "event/second" in replay_chunk
    await query_replay_response.body_iterator.aclose()

    request_with_header_and_query = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/codex/threads/thr_one/events",
            "headers": [(b"last-event-id", str(second.sequence).encode())],
            "query_string": f"after_sequence={first.sequence}".encode(),
            "server": ("testserver", 80),
            "client": ("testclient", 1),
            "scheme": "http",
            "app": application,
        }
    )
    header_precedence_response = await route.endpoint(
        request_with_header_and_query,
        "thr_one",
        after_sequence=first.sequence,
    )
    ready_chunk = await anext(header_precedence_response.body_iterator)
    assert "console.stream.ready" in ready_chunk
    assert "event/second" not in ready_chunk
    await header_precedence_response.body_iterator.aclose()

    request_with_stale_id = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/codex/threads/thr_one/events",
            "headers": [(b"last-event-id", b"1042")],
            "query_string": b"",
            "server": ("testserver", 80),
            "client": ("testclient", 1),
            "scheme": "http",
            "app": application,
        }
    )
    replay_response = await route.endpoint(request_with_stale_id, "thr_one")
    resync_chunk = await anext(replay_response.body_iterator)
    assert "console.stream.resync_required" in resync_chunk
    await replay_response.body_iterator.aclose()

    request_with_stale_query_cursor = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/codex/threads/thr_one/events",
            "headers": [],
            "query_string": b"after_sequence=1042",
            "server": ("testserver", 80),
            "client": ("testclient", 1),
            "scheme": "http",
            "app": application,
        }
    )
    stale_query_response = await route.endpoint(
        request_with_stale_query_cursor,
        "thr_one",
        after_sequence=1042,
    )
    stale_query_chunk = await anext(stale_query_response.body_iterator)
    assert "console.stream.resync_required" in stale_query_chunk
    await stale_query_response.body_iterator.aclose()
    await runtime.close()
