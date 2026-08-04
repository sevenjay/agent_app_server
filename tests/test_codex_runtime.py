import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_runtime import (
    CodexRuntime,
    _discover_agents_md,
    _rate_limit_rows,
)
from projects import Project, ProjectRegistry
from tests.fakes import FakeCodex, FakeNotification

_PROJECT_DIRECTORY = tempfile.TemporaryDirectory(prefix="runtime-journal-test-")
TEST_PROJECT_PATH = Path(_PROJECT_DIRECTORY.name).resolve()


def runtime_settings() -> SimpleNamespace:
    return SimpleNamespace(
        codex_enabled=True,
        codex_event_history_limit=20,
        codex_subscriber_queue_limit=10,
        codex_shutdown_timeout_seconds=1,
        codex_operation_timeout_seconds=1,
        codex_thread_lookup_page_limit=10,
        codex_approval_mode="auto_review",
        codex_sandbox="workspace_write",
    )


def registry() -> ProjectRegistry:
    return ProjectRegistry(
        [Project("agent_app_server", "Agent App Server", TEST_PROJECT_PATH)]
    )


@pytest.mark.asyncio
async def test_runtime_starts_health_checks_and_closes_client() -> None:
    fake = FakeCodex(TEST_PROJECT_PATH)
    runtime = CodexRuntime(
        settings_obj=runtime_settings(),
        registry=registry(),
        client_factory=lambda: fake,
    )
    await runtime.start()
    assert runtime.ready is True
    assert runtime.account_available is True
    assert runtime.account_label == "developer@example.com (Business)"
    assert runtime.agents_md[-1] == "AGENTS.md"
    assert [limit["label"] for limit in runtime.rate_limits] == [
        "Monthly limit",
        "Weekly limit",
    ]
    assert runtime.rate_limits[0]["remaining_percent"] == 75
    assert runtime.rate_limits[1]["remaining_percent"] == 82
    assert runtime.rate_limits_sampled_at is not None
    assert fake.rate_limit_requests == 1
    assert runtime.require_service() is not None

    await runtime.close()
    assert runtime.ready is False
    assert runtime.account_available is False
    assert fake.entered == 1
    assert fake.exited == 1


@pytest.mark.asyncio
async def test_runtime_health_sample_loop_refreshes_limits() -> None:
    fake = FakeCodex(TEST_PROJECT_PATH)
    settings = runtime_settings()
    settings.scheduler_health_sample_seconds = 0.01
    runtime = CodexRuntime(
        settings_obj=settings,
        registry=registry(),
        client_factory=lambda: fake,
    )
    await runtime.start()
    try:
        fake.rate_limits_response["rate_limits"]["individual_limit"][
            "remaining_percent"
        ] = 40
        for _ in range(20):
            if fake.rate_limit_requests >= 2:
                break
            await asyncio.sleep(0.01)

        assert fake.rate_limit_requests >= 2
        assert runtime.rate_limits[0]["remaining_percent"] == 40
    finally:
        await runtime.close()


def test_agents_md_discovery_uses_global_and_scoped_project_files(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    project = tmp_path / "project"
    child = project / "child"
    codex_home.mkdir()
    child.mkdir(parents=True)
    (codex_home / "AGENTS.md").write_text("global", encoding="utf-8")
    (project / ".git").mkdir()
    (project / "AGENTS.md").write_text("project", encoding="utf-8")
    (child / "AGENTS.override.md").write_text("child", encoding="utf-8")

    assert _discover_agents_md(child, codex_home=codex_home) == [
        str(codex_home / "AGENTS.md"),
        "../AGENTS.md",
        "AGENTS.override.md",
    ]


def test_rate_limit_rows_support_monthly_and_weekly_windows() -> None:
    rows = _rate_limit_rows(
        {
            "rateLimits": {
                "primary": {
                    "usedPercent": 0,
                    "windowDurationMins": 43_200,
                    "resetsAt": 1_788_266_880,
                },
                "secondary": {
                    "usedPercent": 21,
                    "windowDurationMins": 10_080,
                    "resetsAt": 1_785_624_480,
                },
            }
        },
        timezone_name="UTC",
    )

    assert [(row["label"], row["remaining_percent"]) for row in rows] == [
        ("Monthly limit", 100),
        ("Weekly limit", 79),
    ]
    assert rows[0]["resets_at"] == "2026-09-01T12:48:00+00:00"


@pytest.mark.asyncio
async def test_runtime_forwards_global_session_status_notifications() -> None:
    fake = FakeCodex(TEST_PROJECT_PATH)
    runtime = CodexRuntime(
        settings_obj=runtime_settings(),
        registry=registry(),
        client_factory=lambda: fake,
    )
    await runtime.start()
    subscription = await runtime.event_hub.subscribe("thr_one")

    await fake.global_notifications.put(
        FakeNotification(
            "thread/status/changed",
            {
                "thread_id": "thr_one",
                "status": {
                    "type": "active",
                    "active_flags": ["waitingOnApproval"],
                },
            },
        )
    )
    event = None
    for _ in range(2):
        candidate = await asyncio.wait_for(
            runtime.event_hub.next_event(subscription),
            timeout=0.1,
        )
        if candidate is not None and candidate.method == "thread/status/changed":
            event = candidate
            break

    assert event is not None
    assert event.method == "thread/status/changed"
    assert event.data["status"]["active_flags"] == ["waitingOnApproval"]
    await runtime.event_hub.close(subscription)
    await runtime.close()


@pytest.mark.asyncio
async def test_runtime_fails_fast_when_sdk_or_account_is_unavailable() -> None:
    failed = FakeCodex(TEST_PROJECT_PATH, fail_start=True)
    runtime = CodexRuntime(
        settings_obj=runtime_settings(),
        registry=registry(),
        client_factory=lambda: failed,
    )
    with pytest.raises(RuntimeError, match="startup failed"):
        await runtime.start()
    assert runtime.ready is False
    assert failed.exited == 1

    unauthenticated = FakeCodex(TEST_PROJECT_PATH, unauthenticated=True)
    runtime = CodexRuntime(
        settings_obj=runtime_settings(),
        registry=registry(),
        client_factory=lambda: unauthenticated,
    )
    with pytest.raises(Exception, match="not logged in"):
        await runtime.start()
    assert unauthenticated.exited == 1


@pytest.mark.asyncio
async def test_runtime_account_health_check_has_a_bounded_timeout() -> None:
    class HangingAccountFake(FakeCodex):
        async def account(self):
            await asyncio.Event().wait()

    settings = runtime_settings()
    settings.codex_operation_timeout_seconds = 0.01
    hanging = HangingAccountFake(TEST_PROJECT_PATH)
    runtime = CodexRuntime(
        settings_obj=settings,
        registry=registry(),
        client_factory=lambda: hanging,
    )

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(runtime.start(), timeout=0.1)

    assert runtime.ready is False
    assert hanging.exited == 1


@pytest.mark.asyncio
async def test_runtime_close_failure_and_timeout_do_not_escape_cleanup() -> None:
    class FailingCloseFake(FakeCodex):
        async def __aexit__(self, *_args):
            raise RuntimeError("close failed")

    failing = FailingCloseFake(TEST_PROJECT_PATH)
    runtime = CodexRuntime(
        settings_obj=runtime_settings(),
        registry=registry(),
        client_factory=lambda: failing,
    )
    await runtime.start()
    await runtime.close()
    assert runtime.ready is False

    class HangingCloseFake(FakeCodex):
        async def __aexit__(self, *_args):
            await asyncio.Event().wait()

    settings = runtime_settings()
    settings.codex_shutdown_timeout_seconds = 0.01
    hanging = HangingCloseFake(TEST_PROJECT_PATH)
    runtime = CodexRuntime(
        settings_obj=settings,
        registry=registry(),
        client_factory=lambda: hanging,
    )
    await runtime.start()
    await asyncio.wait_for(runtime.close(), timeout=0.1)
    assert runtime.ready is False
