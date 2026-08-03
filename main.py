"""FastAPI composition root and executable process entrypoint."""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Query, Request, Response, status
from fastapi import Path as PathParameter
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.trustedhost import TrustedHostMiddleware
from utility.log import LOGD, LOGI, LOGW, initialLog, LOGException

from codex_runtime import CodexRuntime
from codex_service import (
    CodexService,
    ConsoleBadRequest,
    ConsoleNotFound,
    ConsoleProjectExists,
    ConsoleProjectUnavailable,
    ConsoleServiceError,
)
from config import BASE_DIR, environment_settings, settings
from database import database_status, dispose_engine, get_session, init_db
from event_hub import EventEnvelope
from models import AppSetting, ThreadUIMetadata
from projects import (
    InvalidProjectNameError,
    ProjectAlreadyExistsError,
    ProjectCreationError,
    ProjectRegistry,
    ProjectRegistryError,
    ProjectRootNotConfiguredError,
    UnknownProjectError,
)
from project_files import (
    ProjectFileError,
    ProjectFileManager,
    ProjectNotFoundError,
)
from runtime import ensure_log_directory, run_web_server
from scheduler_runtime import scheduler_status, start_scheduler, stop_scheduler
from schemas import (
    GoalStart,
    GoalUpdate,
    PreferencesUpdate,
    ProjectCreate,
    ProjectDirectoryCreate,
    ProjectFileRename,
    ProjectKey,
    ThreadCreate,
    ThreadUpdate,
    TurnStart,
    TurnSteer,
)

STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"
templates = Jinja2Templates(directory=TEMPLATES_DIR)
ThreadId = Annotated[
    str,
    PathParameter(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"),
]


def _request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", "untracked"))


def initialize_database() -> None:
    """Initialize database resources before the blocking Web server starts."""
    asyncio.run(init_db())
    LOGI("SQLite database initialized with WAL mode")


def shutdown_database() -> None:
    """Release database resources after scheduler and Web server stop."""
    asyncio.run(dispose_engine())
    LOGI("Database engine disposed")


async def require_web_user() -> str:
    """Single-user placeholder; this is an integration seam, not authentication."""
    return "local-service-user"


def _service(request: Request) -> CodexService:
    runtime: CodexRuntime = request.app.state.codex_runtime
    return runtime.require_service()


async def _metadata_for_threads(
    session: AsyncSession,
    thread_ids: list[str],
) -> dict[str, ThreadUIMetadata]:
    if not thread_ids:
        return {}
    result = await session.execute(
        select(ThreadUIMetadata).where(ThreadUIMetadata.thread_id.in_(thread_ids))
    )
    return {item.thread_id: item for item in result.scalars()}


def _decorate_thread(
    thread: dict[str, Any],
    metadata: ThreadUIMetadata | None,
) -> dict[str, Any]:
    return {
        **thread,
        "pinned": bool(metadata.pinned) if metadata else False,
        "custom_label": metadata.custom_label if metadata else None,
        "last_opened_at": (
            metadata.last_opened_at.isoformat()
            if metadata and metadata.last_opened_at
            else None
        ),
    }


def _recent_plan_history(
    thread: dict[str, Any],
    *,
    limit: int = 3,
) -> list[dict[str, str]]:
    plans: list[dict[str, str]] = []
    for turn in thread.get("turns", []):
        for item in turn.get("items", []):
            text = item.get("text")
            if item.get("type") != "plan" or not isinstance(text, str) or not text.strip():
                continue
            plans.append(
                {
                    "key": str(item.get("id") or f"history-plan-{len(plans)}"),
                    "text": text,
                }
            )
    return plans[-limit:]


async def _touch_thread_metadata(
    session: AsyncSession,
    thread: dict[str, Any],
    *,
    opened: bool = False,
) -> ThreadUIMetadata:
    thread_id = str(thread["id"])
    project_key = str(thread["project_key"])
    opened_at = datetime.now(timezone.utc) if opened else None
    update_values: dict[str, Any] = {
        "project_key": project_key,
        "updated_at": datetime.now(timezone.utc),
    }
    if opened:
        update_values["last_opened_at"] = opened_at
    statement = (
        sqlite_insert(ThreadUIMetadata)
        .values(
            thread_id=thread_id,
            project_key=project_key,
            pinned=False,
            last_opened_at=opened_at,
        )
        .on_conflict_do_update(
            index_elements=[ThreadUIMetadata.thread_id],
            set_=update_values,
        )
    )
    await session.execute(statement)
    await session.commit()
    metadata = await session.get(ThreadUIMetadata, thread_id)
    if metadata is None:
        raise RuntimeError("Thread metadata upsert did not return a row")
    return metadata


async def _status_payload(
    request: Request,
    session: AsyncSession,
) -> dict[str, object]:
    runtime: CodexRuntime = request.app.state.codex_runtime
    return {
        "service": str(getattr(settings, "app_name", "agent_app_server")),
        "environment": str(settings.current_env),
        "time_utc": datetime.now(timezone.utc).isoformat(),
        "database": await database_status(session),
        "scheduler": scheduler_status(),
        "codex": await runtime.status(),
    }


def _format_sse(event: EventEnvelope) -> str:
    payload = json.dumps(
        event.as_dict(),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"id: {event.sequence}\ndata: {payload}\n\n"


def _synthetic_event(
    *,
    sequence: int,
    thread_id: str,
    event_type: str,
) -> EventEnvelope:
    return EventEnvelope(
        sequence=sequence,
        thread_id=thread_id,
        turn_id=None,
        type=event_type,
        method=event_type,
        data={},
    )


def create_app(
    *,
    codex_client_factory: Callable[[], Any] | None = None,
    codex_enabled: bool | None = None,
    registry: ProjectRegistry | None = None,
) -> FastAPI:
    project_registry = registry or ProjectRegistry.from_settings(settings)
    runtime_kwargs: dict[str, Any] = {}
    if codex_client_factory is not None:
        runtime_kwargs["client_factory"] = codex_client_factory
    codex_runtime = CodexRuntime(
        settings_obj=settings,
        registry=project_registry,
        enabled=codex_enabled,
        **runtime_kwargs,
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        LOGD("console_lifespan_start")
        await application.state.codex_runtime.start()
        LOGD("console_lifespan_ready")
        try:
            yield
        finally:
            LOGD("console_lifespan_shutdown")
            await application.state.codex_runtime.close()

    application = FastAPI(
        title=str(getattr(settings, "app_name", "agent_app_server")),
        version=str(getattr(settings, "app_version", "0.1.0")),
        lifespan=lifespan,
    )
    application.state.codex_runtime = codex_runtime
    application.state.project_registry = project_registry

    selected_environment = environment_settings()
    trusted_hosts = list(getattr(selected_environment, "trusted_hosts", ()))
    if not trusted_hosts:
        raise RuntimeError(f"No trusted_hosts configured for env={settings.current_env!r}")
    application.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts)

    @application.exception_handler(ConsoleServiceError)
    async def console_error_handler(
        request: Request,
        exc: ConsoleServiceError,
    ) -> JSONResponse:
        cause = type(exc.__cause__).__name__ if exc.__cause__ is not None else "none"
        LOGD(
            f"console_request_error request_id={_request_id(request)} "
            f"method={request.method} path={request.url.path} "
            f"status={exc.status_code} code={exc.code} "
            f"exception={type(exc).__name__} cause={cause}"
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.safe_message,
                }
            },
        )

    @application.exception_handler(ProjectRegistryError)
    async def project_registry_error_handler(
        request: Request,
        exc: ProjectRegistryError,
    ) -> JSONResponse:
        LOGD(
            f"console_request_error request_id={_request_id(request)} "
            f"method={request.method} path={request.url.path} "
            f"status=503 code=project_root_unavailable "
            f"exception={type(exc).__name__}"
        )
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "error": {
                    "code": ConsoleProjectUnavailable.code,
                    "message": ConsoleProjectUnavailable.safe_message,
                }
            },
        )

    @application.exception_handler(ProjectFileError)
    async def project_file_error_handler(
        request: Request,
        exc: ProjectFileError,
    ) -> JSONResponse:
        LOGD(
            f"console_request_error request_id={_request_id(request)} "
            f"method={request.method} path={request.url.path} "
            f"status={exc.status_code} code={exc.code} "
            f"exception={type(exc).__name__}"
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.safe_message,
                }
            },
        )

    @application.middleware("http")
    async def security_headers_middleware(request: Request, call_next):
        request_id = uuid.uuid4().hex[:12]
        request.state.request_id = request_id
        started_at = time.perf_counter()
        LOGD(
            f"console_request_start request_id={request_id} "
            f"method={request.method} path={request.url.path}"
        )
        try:
            response = await call_next(request)
        except asyncio.CancelledError:
            elapsed_ms = (time.perf_counter() - started_at) * 1000
            LOGD(
                f"console_request_cancelled request_id={request_id} "
                f"method={request.method} path={request.url.path} "
                f"elapsed_ms={elapsed_ms:.1f}"
            )
            raise
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - started_at) * 1000
            LOGD(
                f"console_request_failed request_id={request_id} "
                f"method={request.method} path={request.url.path} "
                f"exception={type(exc).__name__} elapsed_ms={elapsed_ms:.1f}",
                exc_info=True,
            )
            raise
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        LOGD(
            f"console_request_complete request_id={request_id} "
            f"method={request.method} path={request.url.path} "
            f"status={response.status_code} elapsed_ms={elapsed_ms:.1f}"
        )
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = "; ".join(
            (
                "default-src 'self'",
                "base-uri 'self'",
                "object-src 'none'",
                "frame-ancestors 'none'",
                "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://unpkg.com",
                "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
                "font-src 'self' data: https://fonts.gstatic.com",
                "connect-src 'self'",
                "img-src 'self' data:",
            )
        )
        return response

    application.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @application.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @application.get("/api/status")
    async def api_status(
        request: Request,
        session: AsyncSession = Depends(get_session),
    ) -> dict[str, object]:
        return await _status_payload(request, session)

    @application.get(
        "/api/projects",
        dependencies=[Depends(require_web_user)],
    )
    async def api_projects(request: Request) -> dict[str, Any]:
        current_registry: ProjectRegistry = request.app.state.project_registry
        return {"data": current_registry.public_view()}

    @application.post(
        "/api/projects",
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(require_web_user)],
    )
    async def api_create_project(
        request: Request,
        command: ProjectCreate,
    ) -> dict[str, str]:
        current_registry: ProjectRegistry = request.app.state.project_registry
        LOGD(
            f"console_project_create_start request_id={_request_id(request)}"
        )
        try:
            project = await asyncio.to_thread(current_registry.create, command.name)
        except ProjectAlreadyExistsError as exc:
            raise ConsoleProjectExists from exc
        except InvalidProjectNameError as exc:
            raise ConsoleBadRequest from exc
        except (ProjectRootNotConfiguredError, ProjectCreationError) as exc:
            raise ConsoleProjectUnavailable from exc
        LOGD(
            f"console_project_create_complete request_id={_request_id(request)} "
            f"project_key={project.key}"
        )
        return project.public_view()

    def project_file_manager(request: Request, project_key: str) -> ProjectFileManager:
        try:
            project = request.app.state.project_registry.get(project_key)
        except UnknownProjectError as exc:
            raise ProjectNotFoundError from exc
        return ProjectFileManager(project)

    @application.get(
        "/api/projects/{project_key}/files",
        dependencies=[Depends(require_web_user)],
    )
    async def api_project_files(
        request: Request,
        project_key: ProjectKey,
        path: Annotated[str, Query(max_length=4096)] = "",
    ) -> dict[str, Any]:
        manager = project_file_manager(request, project_key)
        return await asyncio.to_thread(manager.list_directory, path)

    @application.get(
        "/api/projects/{project_key}/files/download",
        response_class=FileResponse,
        dependencies=[Depends(require_web_user)],
    )
    async def api_download_project_file(
        request: Request,
        project_key: ProjectKey,
        path: Annotated[str, Query(min_length=1, max_length=4096)],
    ) -> FileResponse:
        manager = project_file_manager(request, project_key)
        target = await asyncio.to_thread(manager.download_file, path)
        return FileResponse(
            target,
            filename=target.name,
            media_type="application/octet-stream",
        )

    @application.post(
        "/api/projects/{project_key}/files/directories",
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(require_web_user)],
    )
    async def api_create_project_directory(
        request: Request,
        project_key: ProjectKey,
        command: ProjectDirectoryCreate,
    ) -> dict[str, Any]:
        manager = project_file_manager(request, project_key)
        return await asyncio.to_thread(
            manager.create_directory,
            command.path,
            command.name,
        )

    @application.post(
        "/api/projects/{project_key}/files/upload",
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(require_web_user)],
    )
    async def api_upload_project_file(
        request: Request,
        project_key: ProjectKey,
        name: Annotated[str, Query(min_length=1, max_length=255)],
        path: Annotated[str, Query(max_length=4096)] = "",
        overwrite: bool = False,
    ) -> dict[str, Any]:
        manager = project_file_manager(request, project_key)
        content = await request.body()
        return await asyncio.to_thread(
            manager.upload_file,
            path,
            name,
            content,
            overwrite=overwrite,
        )

    @application.patch(
        "/api/projects/{project_key}/files",
        dependencies=[Depends(require_web_user)],
    )
    async def api_rename_project_file(
        request: Request,
        project_key: ProjectKey,
        command: ProjectFileRename,
    ) -> dict[str, Any]:
        manager = project_file_manager(request, project_key)
        return await asyncio.to_thread(manager.rename, command.path, command.name)

    @application.delete(
        "/api/projects/{project_key}/files",
        status_code=status.HTTP_204_NO_CONTENT,
        dependencies=[Depends(require_web_user)],
    )
    async def api_delete_project_file(
        request: Request,
        project_key: ProjectKey,
        path: Annotated[str, Query(min_length=1, max_length=4096)],
    ) -> Response:
        manager = project_file_manager(request, project_key)
        await asyncio.to_thread(manager.delete, path)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @application.get(
        "/api/codex/account",
        dependencies=[Depends(require_web_user)],
    )
    async def api_codex_account(request: Request) -> dict[str, Any]:
        return await _service(request).account()

    @application.get(
        "/api/codex/models",
        dependencies=[Depends(require_web_user)],
    )
    async def api_codex_models(
        request: Request,
        include_hidden: bool = False,
    ) -> dict[str, Any]:
        return await _service(request).list_models(include_hidden=include_hidden)

    @application.get(
        "/api/codex/threads",
        dependencies=[Depends(require_web_user)],
    )
    async def api_threads(
        request: Request,
        project_key: Annotated[
            str,
            Query(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$"),
        ],
        archived: bool = False,
        cursor: Annotated[str | None, Query(max_length=2048)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 30,
        session: AsyncSession = Depends(get_session),
    ) -> dict[str, Any]:
        payload = await _service(request).list_threads(
            project_key=project_key,
            archived=archived,
            cursor=cursor,
            limit=limit,
        )
        metadata = await _metadata_for_threads(
            session,
            [str(thread["id"]) for thread in payload["data"]],
        )
        active_threads = (
            await request.app.state.codex_runtime.turn_manager.status()
        )["active_threads"]
        payload["data"] = [
            {
                **_decorate_thread(thread, metadata.get(str(thread["id"]))),
                "active_turn": active_threads.get(str(thread["id"])),
            }
            for thread in payload["data"]
        ]
        return payload

    @application.post(
        "/api/codex/threads",
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(require_web_user)],
    )
    async def api_create_thread(
        request: Request,
        command: ThreadCreate,
        session: AsyncSession = Depends(get_session),
    ) -> dict[str, Any]:
        LOGD(
            f"console_thread_create_start request_id={_request_id(request)} "
            f"project_key={command.project_key} has_name={command.name is not None} "
            f"model_selected={command.model is not None}"
        )
        thread = await _service(request).create_thread(
            project_key=command.project_key,
            name=command.name,
            model=command.model,
        )
        metadata = await _touch_thread_metadata(session, thread, opened=True)
        decorated = _decorate_thread(thread, metadata)
        LOGD(
            f"console_thread_create_complete request_id={_request_id(request)} "
            f"project_key={command.project_key} thread_id={decorated['id']}"
        )
        return decorated

    @application.get(
        "/api/codex/threads/{thread_id}",
        dependencies=[Depends(require_web_user)],
    )
    async def api_read_thread(
        request: Request,
        thread_id: ThreadId,
        session: AsyncSession = Depends(get_session),
    ) -> dict[str, Any]:
        thread = await _service(request).read_thread(thread_id, include_turns=True)
        metadata = await _touch_thread_metadata(session, thread, opened=True)
        return _decorate_thread(thread, metadata)

    @application.patch(
        "/api/codex/threads/{thread_id}",
        dependencies=[Depends(require_web_user)],
    )
    async def api_update_thread(
        request: Request,
        thread_id: ThreadId,
        command: ThreadUpdate,
        session: AsyncSession = Depends(get_session),
    ) -> dict[str, Any]:
        if not command.model_fields_set:
            raise ConsoleBadRequest
        service = _service(request)
        thread = (
            await service.rename_thread(thread_id, command.name)
            if "name" in command.model_fields_set and command.name is not None
            else await service.read_thread(thread_id, include_turns=True)
        )
        metadata = await _touch_thread_metadata(session, thread)
        if "pinned" in command.model_fields_set and command.pinned is not None:
            metadata.pinned = command.pinned
        if "custom_label" in command.model_fields_set:
            metadata.custom_label = command.custom_label
        await session.commit()
        await session.refresh(metadata)
        return _decorate_thread(thread, metadata)

    @application.delete(
        "/api/codex/threads/{thread_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        dependencies=[Depends(require_web_user)],
    )
    async def api_delete_thread(
        request: Request,
        thread_id: ThreadId,
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> Response:
        await _service(request).delete_thread(thread_id)
        metadata = await session.get(ThreadUIMetadata, thread_id)
        if metadata is not None:
            await session.delete(metadata)
            await session.commit()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @application.post(
        "/api/codex/threads/{thread_id}/fork",
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(require_web_user)],
    )
    async def api_fork_thread(
        request: Request,
        thread_id: ThreadId,
        session: AsyncSession = Depends(get_session),
    ) -> dict[str, Any]:
        thread = await _service(request).fork_thread(thread_id)
        metadata = await _touch_thread_metadata(session, thread, opened=True)
        return _decorate_thread(thread, metadata)

    @application.post(
        "/api/codex/threads/{thread_id}/archive",
        dependencies=[Depends(require_web_user)],
    )
    async def api_archive_thread(request: Request, thread_id: ThreadId) -> dict[str, Any]:
        return await _service(request).archive_thread(thread_id)

    @application.post(
        "/api/codex/threads/{thread_id}/unarchive",
        dependencies=[Depends(require_web_user)],
    )
    async def api_unarchive_thread(
        request: Request,
        thread_id: ThreadId,
    ) -> dict[str, Any]:
        return await _service(request).unarchive_thread(thread_id)

    @application.get(
        "/api/codex/threads/{thread_id}/goal",
        dependencies=[Depends(require_web_user)],
    )
    async def api_get_goal(
        request: Request,
        thread_id: ThreadId,
    ) -> dict[str, Any]:
        return {"goal": await _service(request).get_goal(thread_id)}

    @application.post(
        "/api/codex/threads/{thread_id}/goal",
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(require_web_user)],
    )
    async def api_start_goal(
        request: Request,
        thread_id: ThreadId,
        command: GoalStart,
    ) -> dict[str, Any]:
        return await _service(request).start_goal(
            thread_id,
            objective=command.objective,
            token_budget=command.token_budget,
            model=command.model,
            reasoning_effort=command.reasoning_effort,
        )

    @application.patch(
        "/api/codex/threads/{thread_id}/goal",
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(require_web_user)],
    )
    async def api_update_goal(
        request: Request,
        thread_id: ThreadId,
        command: GoalUpdate,
    ) -> dict[str, Any]:
        service = _service(request)
        return (
            await service.resume_goal(
                thread_id,
                model=command.model,
                reasoning_effort=command.reasoning_effort,
            )
            if command.status == "active"
            else await service.pause_goal(thread_id)
        )

    @application.delete(
        "/api/codex/threads/{thread_id}/goal",
        dependencies=[Depends(require_web_user)],
    )
    async def api_clear_goal(
        request: Request,
        thread_id: ThreadId,
    ) -> dict[str, Any]:
        return await _service(request).clear_goal(thread_id)

    @application.post(
        "/api/codex/threads/{thread_id}/turns",
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(require_web_user)],
    )
    async def api_start_turn(
        request: Request,
        thread_id: ThreadId,
        command: TurnStart,
    ) -> dict[str, Any]:
        return await _service(request).start_turn(
            thread_id,
            prompt=command.prompt,
            model=command.model,
            reasoning_effort=command.reasoning_effort,
        )

    @application.post(
        "/api/codex/threads/{thread_id}/steer",
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(require_web_user)],
    )
    async def api_steer_turn(
        request: Request,
        thread_id: ThreadId,
        command: TurnSteer,
    ) -> dict[str, Any]:
        return await _service(request).steer_turn(thread_id, command.prompt)

    @application.post(
        "/api/codex/threads/{thread_id}/interrupt",
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(require_web_user)],
    )
    async def api_interrupt_turn(
        request: Request,
        thread_id: ThreadId,
    ) -> dict[str, Any]:
        return await _service(request).interrupt_turn(thread_id)

    @application.get(
        "/api/codex/threads/{thread_id}/events",
        dependencies=[Depends(require_web_user)],
    )
    async def api_thread_events(
        request: Request,
        thread_id: ThreadId,
        after_sequence: Annotated[int | None, Query(ge=0)] = None,
    ) -> StreamingResponse:
        service = _service(request)
        request_id = _request_id(request)
        LOGD(
            f"console_sse_authorize_start request_id={request_id} "
            f"thread_id={thread_id}"
        )
        await service.read_thread(thread_id, include_turns=False)
        LOGD(
            f"console_sse_authorize_complete request_id={request_id} "
            f"thread_id={thread_id}"
        )
        raw_last_id = request.headers.get("last-event-id")
        replay_after_sequence = after_sequence
        if raw_last_id:
            try:
                replay_after_sequence = int(raw_last_id)
            except ValueError as exc:
                raise ConsoleBadRequest from exc
            if replay_after_sequence < 0:
                raise ConsoleBadRequest
        heartbeat_seconds = float(
            getattr(settings, "codex_sse_heartbeat_seconds", 15)
        )

        async def stream():
            LOGD(
                f"console_sse_subscribe_start request_id={request_id} "
                f"thread_id={thread_id} after_sequence={replay_after_sequence}"
            )
            subscription = await service.event_hub.subscribe(
                thread_id,
                after_sequence=replay_after_sequence,
            )
            LOGD(
                f"console_sse_subscribe_complete request_id={request_id} "
                f"thread_id={thread_id} replay_count={len(subscription.initial_events)} "
                f"resync_required={subscription.resync_required}"
            )
            try:
                if subscription.resync_required:
                    sequence = await service.event_hub.current_sequence(thread_id)
                    yield _format_sse(
                        _synthetic_event(
                            sequence=sequence,
                            thread_id=thread_id,
                            event_type="console.stream.resync_required",
                        )
                    )
                else:
                    for event in subscription.initial_events:
                        yield _format_sse(event)
                sequence = await service.event_hub.current_sequence(thread_id)
                yield _format_sse(
                    _synthetic_event(
                        sequence=sequence,
                        thread_id=thread_id,
                        event_type="console.stream.ready",
                    )
                )
                while True:
                    try:
                        event = await asyncio.wait_for(
                            service.event_hub.next_event(subscription),
                            timeout=heartbeat_seconds,
                        )
                    except TimeoutError:
                        yield ": heartbeat\n\n"
                        continue
                    if event is None:
                        LOGD(
                            f"console_sse_resync_required request_id={request_id} "
                            f"thread_id={thread_id}"
                        )
                        sequence = await service.event_hub.current_sequence(thread_id)
                        yield _format_sse(
                            _synthetic_event(
                                sequence=sequence,
                                thread_id=thread_id,
                                event_type="console.stream.resync_required",
                            )
                        )
                        return
                    yield _format_sse(event)
            except asyncio.CancelledError:
                LOGD(
                    f"console_sse_cancelled request_id={request_id} "
                    f"thread_id={thread_id}"
                )
                raise
            finally:
                await service.event_hub.close(subscription)
                LOGD(
                    f"console_sse_closed request_id={request_id} "
                    f"thread_id={thread_id}"
                )

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @application.get(
        "/api/preferences",
        dependencies=[Depends(require_web_user)],
    )
    async def api_preferences(
        session: AsyncSession = Depends(get_session),
    ) -> dict[str, str]:
        result = await session.execute(select(AppSetting))
        return {
            setting.setting_key: setting.setting_value
            for setting in result.scalars()
        }

    @application.patch(
        "/api/preferences",
        dependencies=[Depends(require_web_user)],
    )
    async def api_update_preferences(
        request: Request,
        command: PreferencesUpdate,
        session: AsyncSession = Depends(get_session),
    ) -> dict[str, str | None]:
        values = command.model_dump(exclude_unset=True)
        if not values:
            raise ConsoleBadRequest
        if project_key := values.get("selected_project_key"):
            try:
                request.app.state.project_registry.get(project_key)
            except UnknownProjectError as exc:
                raise ConsoleNotFound from exc
        if thread_id := values.get("selected_thread_id"):
            LOGD(
                f"console_preference_thread_validate_start "
                f"request_id={_request_id(request)} thread_id={thread_id}"
            )
            await _service(request).read_thread(thread_id, include_turns=False)
            LOGD(
                f"console_preference_thread_validate_complete "
                f"request_id={_request_id(request)} thread_id={thread_id}"
            )
        for key, value in values.items():
            setting = await session.get(AppSetting, key)
            if value is None:
                if setting is not None:
                    await session.delete(setting)
            elif setting is None:
                session.add(AppSetting(setting_key=key, setting_value=value))
            else:
                setting.setting_value = value
        await session.commit()
        return values

    @application.get(
        "/partials/codex/status",
        response_class=HTMLResponse,
        dependencies=[Depends(require_web_user)],
    )
    async def codex_status_partial(
        request: Request,
        session: AsyncSession = Depends(get_session),
    ):
        return templates.TemplateResponse(
            request,
            "_codex_status.html",
            {"status": await _status_payload(request, session)},
        )

    @application.get(
        "/partials/projects",
        response_class=HTMLResponse,
        dependencies=[Depends(require_web_user)],
    )
    async def projects_partial(request: Request):
        return templates.TemplateResponse(
            request,
            "_project_selector.html",
            {"projects": request.app.state.project_registry.public_view()},
        )

    @application.get(
        "/partials/threads",
        response_class=HTMLResponse,
        dependencies=[Depends(require_web_user)],
    )
    async def threads_partial(
        request: Request,
        project_key: Annotated[
            str,
            Query(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$"),
        ],
        archived: bool = False,
        cursor: Annotated[str | None, Query(max_length=2048)] = None,
        session: AsyncSession = Depends(get_session),
    ):
        payload = await _service(request).list_threads(
            project_key=project_key,
            archived=archived,
            cursor=cursor,
            limit=30,
        )
        metadata = await _metadata_for_threads(
            session,
            [str(thread["id"]) for thread in payload["data"]],
        )
        active_threads = (
            await request.app.state.codex_runtime.turn_manager.status()
        )["active_threads"]
        threads = [
            {
                **_decorate_thread(thread, metadata.get(str(thread["id"]))),
                "active_turn": active_threads.get(str(thread["id"])),
            }
            for thread in payload["data"]
        ]
        response = templates.TemplateResponse(
            request,
            "_thread_list.html",
            {
                "threads": threads,
                "project_key": project_key,
                "archived": archived,
                "next_cursor": payload["next_cursor"],
            },
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    async def _thread_partial_context(
        request: Request,
        thread_id: str,
        session: AsyncSession,
    ) -> dict[str, Any]:
        LOGD(
            f"console_thread_partial_start request_id={_request_id(request)} "
            f"path={request.url.path} thread_id={thread_id}"
        )
        thread = await _service(request).read_thread(thread_id, include_turns=True)
        metadata = await _touch_thread_metadata(session, thread, opened=True)
        state = await request.app.state.codex_runtime.turn_manager.status()
        LOGD(
            f"console_thread_partial_complete request_id={_request_id(request)} "
            f"path={request.url.path} thread_id={thread_id}"
        )
        return {
            "thread": _decorate_thread(thread, metadata),
            "active": state["active_threads"].get(thread_id),
            "plan_history": _recent_plan_history(thread),
        }

    @application.get(
        "/partials/threads/{thread_id}/timeline",
        response_class=HTMLResponse,
        dependencies=[Depends(require_web_user)],
    )
    async def thread_timeline_partial(
        request: Request,
        thread_id: ThreadId,
        session: AsyncSession = Depends(get_session),
    ):
        return templates.TemplateResponse(
            request,
            "_thread_timeline.html",
            await _thread_partial_context(request, thread_id, session),
        )

    @application.get(
        "/partials/threads/{thread_id}/inspector",
        response_class=HTMLResponse,
        dependencies=[Depends(require_web_user)],
    )
    async def thread_inspector_partial(
        request: Request,
        thread_id: ThreadId,
        session: AsyncSession = Depends(get_session),
    ):
        context = await _thread_partial_context(request, thread_id, session)
        context["goal"] = await _service(request).get_goal(thread_id)
        return templates.TemplateResponse(
            request,
            "_thread_inspector.html",
            context,
        )

    @application.get(
        "/partials/threads/{thread_id}/changes",
        response_class=HTMLResponse,
        dependencies=[Depends(require_web_user)],
    )
    async def thread_changes_partial(
        request: Request,
        thread_id: ThreadId,
        session: AsyncSession = Depends(get_session),
    ):
        return templates.TemplateResponse(
            request,
            "_thread_changes.html",
            await _thread_partial_context(request, thread_id, session),
        )

    @application.get(
        "/partials/threads/{thread_id}/composer",
        response_class=HTMLResponse,
        dependencies=[Depends(require_web_user)],
    )
    async def thread_composer_partial(
        request: Request,
        thread_id: ThreadId,
        session: AsyncSession = Depends(get_session),
    ):
        return templates.TemplateResponse(
            request,
            "_composer.html",
            await _thread_partial_context(request, thread_id, session),
        )

    return application


app = create_app()


def _main() -> None:
    log_directory = ensure_log_directory()
    # utility.log.initialLog concatenates path and filename, so it needs a separator.
    initialLog("agent_app_server", f"{log_directory}{os.sep}", settings.log_level)
    modified = time.strptime(time.ctime(os.path.getmtime("main.py")))
    modify_time = time.strftime("%H:%M:%S %m-%d", modified)
    LOGI(
        f"==== version {settings.app_version}  {modify_time}, "
        f"env: {settings.current_env} =========================================="
    )

    initialize_database()
    if bool(getattr(settings, "scheduler_enabled", True)):
        start_scheduler()
    run_web_server(app, log_directory=log_directory)


def _shutdown_resources() -> None:
    """Release scheduler and database independently after Uvicorn returns."""
    try:
        stop_scheduler()
    except Exception as exc:
        LOGException(exc)
        LOGW("Failed to stop scheduler")

    try:
        shutdown_database()
    except Exception as exc:
        LOGException(exc)
        LOGW("Failed to dispose database engine")


if __name__ == "__main__":
    try:
        _main()
    except SystemExit as exc:
        if exc.code not in (None, 0):
            LOGW(
                f"System exit with non-zero code {exc.code!r}: "
                "stopping scheduler and web application"
            )
            raise
        LOGI("System exit requested: stopping scheduler and web application")
    except KeyboardInterrupt:
        LOGW("Keyboard interrupt: stopping scheduler and web application")
    except Exception as exc:
        LOGException(exc)
        LOGW("Application stopped after an unhandled exception")
    else:
        LOGI("Web application stopped normally")
    finally:
        _shutdown_resources()
        LOGI("Stopped running")
