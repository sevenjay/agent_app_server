import pytest

import main
from tests.http_client import application_client


def test_main_initializes_logging_database_scheduler_then_web_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    log_messages: list[str] = []

    monkeypatch.setattr(
        main,
        "ensure_log_directory",
        lambda: events.append("logs:ensure") or "logs",
    )

    def record_initial_log(name: str, path: str, level: str) -> None:
        assert name == "agent_app_server"
        assert path == f"logs{main.os.sep}"
        assert level == main.settings.log_level
        events.append("logging:initialize")

    monkeypatch.setattr(main, "initialLog", record_initial_log)
    monkeypatch.setattr(
        main,
        "LOGI",
        log_messages.append,
    )
    monkeypatch.setattr(
        main,
        "start_scheduler",
        lambda: events.append("scheduler:start"),
    )
    monkeypatch.setattr(
        main,
        "initialize_database",
        lambda: events.append("database:initialize"),
    )
    monkeypatch.setattr(
        main,
        "run_web_server",
        lambda _app, *, log_directory: events.append("web:start"),
    )
    main._main()

    assert events == [
        "logs:ensure",
        "logging:initialize",
        "database:initialize",
        "scheduler:start",
        "web:start",
    ]
    assert log_messages[0].startswith(f"==== version {main.APP_VERSION}  ")
    assert "env: development" in log_messages[0]


def test_shutdown_database_still_runs_when_scheduler_shutdown_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    logged_exceptions: list[Exception] = []

    def failing_scheduler_shutdown() -> None:
        events.append("scheduler:stop")
        raise RuntimeError("scheduler shutdown failed")

    monkeypatch.setattr(main, "stop_scheduler", failing_scheduler_shutdown)
    monkeypatch.setattr(
        main,
        "shutdown_database",
        lambda: events.append("database:shutdown"),
    )
    monkeypatch.setattr(main, "LOGException", logged_exceptions.append)
    monkeypatch.setattr(main, "LOGW", lambda _message: None)

    main._shutdown_resources()

    assert events == ["scheduler:stop", "database:shutdown"]
    assert len(logged_exceptions) == 1
    assert str(logged_exceptions[0]) == "scheduler shutdown failed"


@pytest.mark.asyncio
async def test_fastapi_does_not_manage_runtime_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        main,
        "initialize_database",
        lambda: pytest.fail("FastAPI initialized the database"),
    )
    monkeypatch.setattr(
        main,
        "shutdown_database",
        lambda: pytest.fail("FastAPI disposed the database"),
    )
    monkeypatch.setattr(
        main,
        "start_scheduler",
        lambda: pytest.fail("FastAPI started the scheduler"),
    )
    monkeypatch.setattr(
        main,
        "stop_scheduler",
        lambda: pytest.fail("FastAPI stopped the scheduler"),
    )

    async with application_client(main.app) as client:
        assert (await client.get("/api/status")).status_code == 200
