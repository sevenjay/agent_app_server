"""Codex application service with project authorization and Turn orchestration."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Any

from openai_codex import ApprovalMode, Sandbox
from openai_codex.errors import InvalidParamsError, InvalidRequestError
from openai_codex.generated.v2_all import ThreadDeleteParams, ThreadDeleteResponse

from codex_goal_adapter import CodexGoalAdapter
from codex_serializers import field, notification_view, thread_view, to_primitive
from event_hub import EventEnvelope, EventHub, Subscription
from projects import Project, ProjectRegistry, UnknownProjectError
from stream_journal import (
    StreamJournal,
    complete_stream_turn_ids,
    history_watermark,
    materialize_timeline,
    normalize_event,
)
from turn_manager import (
    TurnConflictError,
    TurnManager,
    TurnNotActiveError,
    TurnsUnavailableError,
)
from utility.log import LOGD, LOGW


class ConsoleServiceError(RuntimeError):
    status_code = 500
    code = "console_error"
    safe_message = "The console could not complete the request."


class ConsoleBadRequest(ConsoleServiceError):
    status_code = 400
    code = "invalid_request"
    safe_message = "The request is not valid."


class ConsoleNotFound(ConsoleServiceError):
    status_code = 404
    code = "not_found"
    safe_message = "The requested project or session was not found."


class ConsoleConflict(ConsoleServiceError):
    status_code = 409
    code = "active_turn_conflict"
    safe_message = "The session state does not allow this operation."


class ConsoleProjectExists(ConsoleServiceError):
    status_code = 409
    code = "project_exists"
    safe_message = "A project with this directory name already exists."


class ConsoleProjectUnavailable(ConsoleServiceError):
    status_code = 503
    code = "project_root_unavailable"
    safe_message = "The configured project root is not available."


class ConsoleUnavailable(ConsoleServiceError):
    status_code = 503
    code = "codex_unavailable"
    safe_message = "Codex is temporarily unavailable."


class ConsoleTimeout(ConsoleServiceError):
    status_code = 504
    code = "codex_timeout"
    safe_message = "Codex did not respond before the operation timed out."


@dataclass(slots=True)
class _PendingThread:
    """Server-created thread not yet discoverable through Codex thread/list."""

    project: Project
    handle: Any
    view: dict[str, Any]
    archived: bool = False


class CodexService:
    def __init__(
        self,
        codex: Any,
        *,
        registry: ProjectRegistry,
        event_hub: EventHub,
        turn_manager: TurnManager,
        approval_mode: ApprovalMode,
        sandbox: Sandbox,
        stream_journal: StreamJournal | None = None,
        goal_adapter: Any | None = None,
        operation_timeout: float = 30,
        lookup_page_limit: int = 50,
    ) -> None:
        self.codex = codex
        self.registry = registry
        self.event_hub = event_hub
        self.turn_manager = turn_manager
        self.approval_mode = approval_mode
        self.sandbox = sandbox
        self.stream_journal = stream_journal or StreamJournal()
        self.goal_adapter = goal_adapter or CodexGoalAdapter(codex)
        self.operation_timeout = operation_timeout
        self.lookup_page_limit = lookup_page_limit
        self._pending_threads: dict[str, _PendingThread] = {}
        self._thread_projects: dict[str, Project] = {}
        self._publish_lock = asyncio.Lock()
        self._recent_stream_fingerprints: dict[str, tuple[str, float]] = {}

    async def _fan_out_record(self, record: dict[str, Any]) -> EventEnvelope | None:
        thread_id = str(record["thread_id"])
        sequence = int(record["seq"])
        if sequence <= await self.event_hub.current_sequence(thread_id):
            return None
        return await self.event_hub.publish(
            thread_id,
            event_type=str(record.get("event_type") or "codex.notification"),
            method=str(record.get("method") or record.get("type") or "unknown"),
            data=dict(record.get("data") or {}),
            turn_id=(str(record["turn_id"]) if record.get("turn_id") else None),
            sequence=sequence,
        )

    async def _publish(
        self,
        thread_id: str,
        *,
        event_type: str,
        method: str,
        data: dict[str, Any] | None = None,
        turn_id: str | None = None,
        dedup_key: str | None = None,
        channel: str = "console",
    ) -> EventEnvelope | None:
        """Persist one normalized event before exposing its sequence to SSE."""
        async with self._publish_lock:
            project = self._thread_projects.get(thread_id)
            if project is None:
                project, _thread = await self._find_thread(thread_id)
                self._thread_projects[thread_id] = project
            opened = await self.stream_journal.ensure_opened(project.path, thread_id)
            if not opened.duplicate:
                await self._fan_out_record(opened.record)
            normalized = normalize_event(
                thread_id=thread_id,
                event_type=event_type,
                method=method,
                data=data,
                turn_id=turn_id,
                dedup_key=dedup_key,
            )
            if normalized is None:
                return None
            fingerprint = normalized.get("notification_fingerprint")
            if isinstance(fingerprint, str) and fingerprint:
                now = time.monotonic()
                previous = self._recent_stream_fingerprints.get(fingerprint)
                if previous is not None and previous[0] != channel and now - previous[1] <= 5:
                    return None
                self._recent_stream_fingerprints[fingerprint] = (channel, now)
                if len(self._recent_stream_fingerprints) > 2_000:
                    cutoff = now - 10
                    self._recent_stream_fingerprints = {
                        key: value for key, value in self._recent_stream_fingerprints.items() if value[1] >= cutoff
                    }
            appended = await self.stream_journal.append(
                project.path,
                thread_id,
                normalized,
            )
            if appended.duplicate:
                return None
            return await self._fan_out_record(appended.record)

    async def publish_notification(
        self,
        thread_id: str,
        *,
        method: str,
        data: dict[str, Any],
        turn_id: str | None = None,
        channel: str = "global",
    ) -> EventEnvelope | None:
        if turn_id is None:
            turn_id = str(data.get("turn_id") or data.get("turnId") or "") or None
        if turn_id is None:
            active = await self.turn_manager.current(thread_id)
            turn_id = active.turn_id if active is not None else None
        return await self._publish(
            thread_id,
            event_type="codex.notification",
            method=method,
            data=data,
            turn_id=turn_id,
            channel=channel,
        )

    def _remember_pending_thread(
        self,
        *,
        project: Project,
        handle: Any,
        view: dict[str, Any],
    ) -> None:
        thread_id = str(view.get("id", ""))
        handle_id = str(field(handle, "id", ""))
        if not thread_id or handle_id != thread_id:
            LOGD(f"codex_pending_thread_rejected view_thread_id={thread_id or 'missing'} handle_matches={handle_id == thread_id}")
            raise ConsoleUnavailable
        self._pending_threads[thread_id] = _PendingThread(
            project=project,
            handle=handle,
            view=view,
            archived=bool(view.get("archived", False)),
        )
        self._thread_projects[thread_id] = project
        LOGD(f"codex_pending_thread_registered thread_id={thread_id} project_key={project.key}")

    def _refresh_pending_thread(
        self,
        thread_id: str,
        *,
        handle: Any,
        view: dict[str, Any],
    ) -> None:
        pending = self._pending_threads.get(thread_id)
        if pending is None:
            return
        pending.handle = handle
        pending.view = view
        pending.archived = bool(view.get("archived", pending.archived))

    async def _call(
        self,
        awaitable,
        *,
        operation: str,
        invalid_is_bad_request: bool = False,
    ):
        started_at = time.perf_counter()
        LOGD(f"codex_rpc_start operation={operation}")
        try:
            result = await asyncio.wait_for(
                awaitable,
                timeout=self.operation_timeout,
            )
        except asyncio.CancelledError:
            elapsed_ms = (time.perf_counter() - started_at) * 1000
            LOGD(f"codex_rpc_cancelled operation={operation} elapsed_ms={elapsed_ms:.1f}")
            raise
        except TimeoutError as exc:
            elapsed_ms = (time.perf_counter() - started_at) * 1000
            LOGD(f"codex_rpc_timeout operation={operation} elapsed_ms={elapsed_ms:.1f}")
            raise ConsoleTimeout from exc
        except ConsoleServiceError as exc:
            LOGD(f"codex_rpc_console_error operation={operation} exception={type(exc).__name__}")
            raise
        except (
            TurnConflictError,
            TurnNotActiveError,
            TurnsUnavailableError,
        ) as exc:
            LOGD(f"codex_rpc_turn_state_error operation={operation} exception={type(exc).__name__}")
            raise
        except (InvalidParamsError, InvalidRequestError) as exc:
            LOGD(
                f"codex_rpc_invalid operation={operation} exception={type(exc).__name__} invalid_is_bad_request={invalid_is_bad_request}",
                exc_info=True,
            )
            LOGW(f"Codex operation rejected invalid parameters: {type(exc).__name__}")
            if invalid_is_bad_request:
                raise ConsoleBadRequest from exc
            raise ConsoleUnavailable from exc
        except Exception as exc:
            LOGD(
                f"codex_rpc_failed operation={operation} exception={type(exc).__name__}",
                exc_info=True,
            )
            LOGW(f"Codex operation failed: {type(exc).__name__}")
            raise ConsoleUnavailable from exc
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        LOGD(f"codex_rpc_complete operation={operation} elapsed_ms={elapsed_ms:.1f}")
        return result

    def _project(self, project_key: str) -> Project:
        try:
            return self.registry.get(project_key)
        except UnknownProjectError as exc:
            raise ConsoleNotFound from exc

    async def account(self) -> dict[str, Any]:
        response = await self._call(
            self.codex.account(),
            operation="account",
        )
        data = to_primitive(response)
        return data if isinstance(data, dict) else {"account": data}

    async def list_models(self, *, include_hidden: bool = False) -> dict[str, Any]:
        response = await self._call(
            self.codex.models(include_hidden=include_hidden),
            operation="models",
        )
        data = to_primitive(response)
        return data if isinstance(data, dict) else {"data": data}

    async def list_threads(
        self,
        *,
        project_key: str,
        archived: bool = False,
        cursor: str | None = None,
        limit: int = 30,
    ) -> dict[str, Any]:
        project = self._project(project_key)
        LOGD(f"codex_thread_list_start project_key={project.key} archived={archived} cursor_present={cursor is not None} limit={limit}")
        response = await self._call(
            self.codex.thread_list(
                archived=archived,
                cursor=cursor,
                cwd=str(project.path),
                limit=limit,
            ),
            operation="thread_list",
            invalid_is_bad_request=True,
        )
        threads = [
            thread_view(thread, project_key=project.key)
            for thread in field(response, "data", ())
            if self.registry.project_for_path(field(thread, "cwd", "")) == project
        ]
        listed_ids = {str(thread["id"]) for thread in threads}
        discovered_pending_ids = listed_ids.intersection(self._pending_threads)
        for thread_id in discovered_pending_ids:
            self._pending_threads.pop(thread_id, None)
            LOGD(f"codex_pending_thread_discovered thread_id={thread_id} project_key={project.key}")

        pending_views: list[dict[str, Any]] = []
        if cursor is None:
            pending_views = [
                dict(pending.view)
                for pending in reversed(tuple(self._pending_threads.values()))
                if pending.project == project and pending.archived is archived and str(pending.view["id"]) not in listed_ids
            ]
            if pending_views:
                LOGD(f"codex_thread_list_pending_merged project_key={project.key} archived={archived} pending_count={len(pending_views)}")
        threads = [*pending_views, *threads]
        payload = {
            "data": threads,
            "next_cursor": field(response, "next_cursor"),
            "backwards_cursor": field(response, "backwards_cursor"),
        }
        LOGD(
            f"codex_thread_list_complete project_key={project.key} "
            f"archived={archived} result_count={len(threads)} "
            f"next_cursor_present={payload['next_cursor'] is not None}"
        )
        return payload

    async def _find_thread(self, thread_id: str) -> tuple[Project, Any]:
        pending = self._pending_threads.get(thread_id)
        if pending is not None:
            LOGD(f"codex_thread_lookup_pending thread_id={thread_id} project_key={pending.project.key}")
            self._thread_projects[thread_id] = pending.project
            return pending.project, pending.handle

        scanned_pages = 0
        LOGD(f"codex_thread_lookup_start thread_id={thread_id} project_count={len(self.registry)}")
        for project in self.registry:
            for archived in (False, True):
                cursor: str | None = None
                for page_number in range(self.lookup_page_limit):
                    response = await self._call(
                        self.codex.thread_list(
                            archived=archived,
                            cursor=cursor,
                            cwd=str(project.path),
                            limit=100,
                        ),
                        operation="thread_list_lookup",
                    )
                    scanned_pages += 1
                    listed_threads = tuple(field(response, "data", ()))
                    LOGD(
                        f"codex_thread_lookup_page thread_id={thread_id} "
                        f"project_key={project.key} archived={archived} "
                        f"page={page_number + 1} result_count={len(listed_threads)}"
                    )
                    for listed_thread in listed_threads:
                        if (
                            str(field(listed_thread, "id", "")) == thread_id
                            and self.registry.project_for_path(field(listed_thread, "cwd", "")) == project
                        ):
                            LOGD(
                                f"codex_thread_lookup_found thread_id={thread_id} "
                                f"project_key={project.key} archived={archived} "
                                f"page={page_number + 1} scanned_pages={scanned_pages}"
                            )
                            self._thread_projects[thread_id] = project
                            return project, listed_thread
                    cursor = field(response, "next_cursor")
                    if not cursor:
                        break
        LOGD(f"codex_thread_lookup_miss thread_id={thread_id} scanned_pages={scanned_pages}")
        raise ConsoleNotFound

    async def _authorized_thread(
        self,
        thread_id: str,
        *,
        include_turns: bool,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> tuple[Project, Any, Any]:
        LOGD(f"codex_thread_authorize_start thread_id={thread_id} include_turns={include_turns}")
        pending = self._pending_threads.get(thread_id)
        if pending is not None and model is None and reasoning_effort is None:
            project = pending.project
            thread = pending.handle
            LOGD(f"codex_thread_authorize_fresh thread_id={thread_id} project_key={project.key}")
        else:
            if pending is not None:
                project = pending.project
            else:
                project, _listed = await self._find_thread(thread_id)
            thread = await self._call(
                self.codex.thread_resume(
                    thread_id,
                    approval_mode=self.approval_mode,
                    sandbox=self.sandbox,
                    model=model,
                    config=({"model_reasoning_effort": reasoning_effort} if reasoning_effort is not None else None),
                ),
                operation="thread_resume",
            )
        response = await self._call(
            thread.read(include_turns=include_turns),
            operation="thread_read",
        )
        actual_thread = field(response, "thread")
        if str(field(actual_thread, "id", "")) != thread_id or self.registry.project_for_path(field(actual_thread, "cwd", "")) != project:
            LOGD(f"codex_thread_authorize_mismatch thread_id={thread_id} project_key={project.key}")
            self._pending_threads.pop(thread_id, None)
            raise ConsoleNotFound
        self._refresh_pending_thread(
            thread_id,
            handle=thread,
            view=self._read_view(response, project),
        )
        self._thread_projects[thread_id] = project
        LOGD(f"codex_thread_authorize_complete thread_id={thread_id} project_key={project.key} include_turns={include_turns}")
        return project, thread, response

    def _read_view(self, response: Any, project: Project) -> dict[str, Any]:
        return thread_view(field(response, "thread"), project_key=project.key)

    async def create_thread(
        self,
        *,
        project_key: str,
        name: str | None,
        model: str | None,
    ) -> dict[str, Any]:
        project = self._project(project_key)
        LOGD(f"codex_thread_create_start project_key={project.key} has_name={name is not None} model_selected={model is not None}")
        thread = await self._call(
            self.codex.thread_start(
                cwd=str(project.path),
                model=model,
                approval_mode=self.approval_mode,
                sandbox=self.sandbox,
            ),
            operation="thread_start",
        )
        created_thread_id = str(field(thread, "id", ""))
        LOGD(f"codex_thread_create_started project_key={project.key} thread_id={created_thread_id or 'missing'}")
        if name:
            await self._call(
                thread.set_name(name),
                operation="thread_set_name",
            )
        response = await self._call(
            thread.read(include_turns=True),
            operation="thread_read_created",
        )
        actual = field(response, "thread")
        if self.registry.project_for_path(field(actual, "cwd", "")) != project:
            LOGD(f"codex_thread_create_project_mismatch project_key={project.key} thread_id={created_thread_id or 'missing'}")
            raise ConsoleNotFound
        view = self._read_view(response, project)
        self._remember_pending_thread(
            project=project,
            handle=thread,
            view=view,
        )
        LOGD(f"codex_thread_create_complete project_key={project.key} thread_id={view['id']}")
        return view

    @staticmethod
    def _journal_envelope(record: dict[str, Any]) -> EventEnvelope:
        return EventEnvelope(
            sequence=int(record["seq"]),
            thread_id=str(record["thread_id"]),
            turn_id=(str(record["turn_id"]) if record.get("turn_id") else None),
            type=str(record.get("event_type") or "codex.notification"),
            method=str(record.get("method") or record.get("type") or "unknown"),
            data=dict(record.get("data") or {}),
        )

    async def _materialized_view(
        self,
        project: Project,
        history_view: dict[str, Any],
    ) -> dict[str, Any]:
        thread_id = str(history_view["id"])
        journal = await self.stream_journal.read(project.path, thread_id)
        current_watermark = history_watermark(history_view)
        imported_watermarks = [event.get("data") for event in journal.events if event.get("type") == "history.baseline_imported"]
        if journal.coverage != "complete" or current_watermark not in imported_watermarks:
            async with self._publish_lock:
                journal = await self.stream_journal.read(project.path, thread_id)
                imported_watermarks = [event.get("data") for event in journal.events if event.get("type") == "history.baseline_imported"]
                if journal.coverage != "complete" or current_watermark not in imported_watermarks:
                    skip_turn_ids = frozenset() if journal.damaged else complete_stream_turn_ids(journal)
                    imported = await self.stream_journal.append_history(
                        project.path,
                        thread_id,
                        history_view,
                        skip_turn_ids=skip_turn_ids,
                    )
                    for appended in imported:
                        if not appended.duplicate:
                            await self._fan_out_record(appended.record)
            journal = await self.stream_journal.read(project.path, thread_id)

        turns, aliases = materialize_timeline(thread_id, journal)
        if aliases:
            async with self._publish_lock:
                attached = await self.stream_journal.attach_aliases(
                    project.path,
                    thread_id,
                    aliases,
                )
                for appended in attached:
                    if not appended.duplicate:
                        await self._fan_out_record(appended.record)
            journal = await self.stream_journal.read(project.path, thread_id)
            turns, _unused_aliases = materialize_timeline(thread_id, journal)
        journal_diff = ""
        journal_usage: dict[str, Any] | None = None
        for event in journal.events:
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            if event.get("type") == "file_change.updated":
                journal_diff = str(data.get("diff") or "")
            elif event.get("type") == "usage.updated":
                journal_usage = dict(data)
        await self.event_hub.advance_sequence(thread_id, journal.cursor)
        return {
            **history_view,
            "turns": turns,
            "journal_cursor": journal.cursor,
            "journal_coverage": journal.coverage,
            "journal_diff": journal_diff,
            "journal_usage": journal_usage,
        }

    async def read_thread(self, thread_id: str, *, include_turns: bool = True) -> dict[str, Any]:
        LOGD(f"codex_thread_read_start thread_id={thread_id} include_turns={include_turns}")
        project, _thread, response = await self._authorized_thread(
            thread_id,
            include_turns=include_turns,
        )
        view = self._read_view(response, project)
        if include_turns:
            view = await self._materialized_view(project, view)
        LOGD(f"codex_thread_read_complete thread_id={thread_id} project_key={project.key} include_turns={include_turns}")
        return view

    async def snapshot_thread(self, thread_id: str) -> dict[str, Any]:
        return await self.read_thread(thread_id, include_turns=True)

    async def journal_cursor(self, thread_id: str) -> int:
        project = self._thread_projects.get(thread_id)
        if project is None:
            project, _listed = await self._find_thread(thread_id)
        journal = await self.stream_journal.read(project.path, thread_id)
        await self.event_hub.advance_sequence(thread_id, journal.cursor)
        return journal.cursor

    async def subscribe_events(
        self,
        thread_id: str,
        *,
        after_sequence: int | None,
    ) -> tuple[Subscription, list[EventEnvelope], int, bool]:
        """Register live delivery first, then read durable replay without a gap."""
        project = self._thread_projects.get(thread_id)
        if project is None:
            project, _listed = await self._find_thread(thread_id)
        subscription = await self.event_hub.subscribe(
            thread_id,
            after_sequence=after_sequence,
        )
        journal = await self.stream_journal.read(project.path, thread_id)
        await self.event_hub.advance_sequence(thread_id, journal.cursor)
        durable_replay = (
            [self._journal_envelope(event) for event in journal.events if int(event["seq"]) > after_sequence]
            if after_sequence is not None
            else []
        )
        replay_by_sequence = {event.sequence: event for event in [*subscription.initial_events, *durable_replay]}
        replay = [replay_by_sequence[key] for key in sorted(replay_by_sequence)]
        replay_boundary = journal.cursor if journal.exists else await self.event_hub.current_sequence(thread_id)
        resync_required = bool(
            after_sequence is not None
            and (
                after_sequence > replay_boundary
                or journal.damaged
                or (subscription.resync_required and not journal.exists)
                or (replay and replay[0].sequence != after_sequence + 1)
            )
        )
        return subscription, replay, replay_boundary, resync_required

    async def rename_thread(self, thread_id: str, name: str) -> dict[str, Any]:
        project, thread, _response = await self._authorized_thread(thread_id, include_turns=False)
        await self._call(
            thread.set_name(name),
            operation="thread_set_name",
        )
        response = await self._call(
            thread.read(include_turns=True),
            operation="thread_read_renamed",
        )
        if self.registry.project_for_path(field(field(response, "thread"), "cwd", "")) != project:
            self._pending_threads.pop(thread_id, None)
            raise ConsoleNotFound
        view = self._read_view(response, project)
        self._refresh_pending_thread(
            thread_id,
            handle=thread,
            view=view,
        )
        return view

    async def archive_thread(self, thread_id: str) -> dict[str, Any]:
        try:
            await self.turn_manager.reserve_mutation(thread_id)
        except TurnConflictError as exc:
            raise ConsoleConflict from exc
        try:
            project, _thread, _response = await self._authorized_thread(
                thread_id,
                include_turns=False,
            )
            await self._call(
                self.codex.thread_archive(thread_id),
                operation="thread_archive",
            )
            pending = self._pending_threads.get(thread_id)
            if pending is not None:
                pending.archived = True
                pending.view = {**pending.view, "archived": True}
            return {
                "thread_id": thread_id,
                "project_key": project.key,
                "archived": True,
            }
        finally:
            await self.turn_manager.finish_mutation(thread_id)

    async def delete_thread(self, thread_id: str) -> dict[str, Any]:
        try:
            await self.turn_manager.reserve_mutation(thread_id)
        except TurnConflictError as exc:
            raise ConsoleConflict from exc
        try:
            project, _thread, _response = await self._authorized_thread(
                thread_id,
                include_turns=False,
            )
            pending = thread_id in self._pending_threads
            try:
                await self._call(
                    self._delete_thread_request(thread_id),
                    operation="thread_delete",
                )
            except (ConsoleTimeout, ConsoleUnavailable) as delete_error:
                if pending:
                    raise
                try:
                    still_listed = await self._thread_is_listed_in_project(
                        thread_id,
                        project,
                    )
                except ConsoleServiceError:
                    raise delete_error
                if still_listed is not False:
                    raise
                LOGW(f"Codex delete reported failure after removing the session: thread_id={thread_id} project_key={project.key}")
            self._pending_threads.pop(thread_id, None)
            await self.stream_journal.trash_thread(project.path, thread_id)
            self._thread_projects.pop(thread_id, None)
            return {
                "thread_id": thread_id,
                "project_key": project.key,
                "deleted": True,
            }
        finally:
            await self.turn_manager.finish_mutation(thread_id)

    async def _thread_is_listed_in_project(
        self,
        thread_id: str,
        project: Project,
    ) -> bool | None:
        """Return False only after exhaustively checking active and archived lists."""
        for archived in (False, True):
            cursor: str | None = None
            for _page_number in range(self.lookup_page_limit):
                response = await self._call(
                    self.codex.thread_list(
                        archived=archived,
                        cursor=cursor,
                        cwd=str(project.path),
                        limit=100,
                    ),
                    operation="thread_list_delete_verify",
                )
                if any(
                    str(field(thread, "id", "")) == thread_id and self.registry.project_for_path(field(thread, "cwd", "")) == project
                    for thread in field(response, "data", ())
                ):
                    return True
                cursor = field(response, "next_cursor")
                if not cursor:
                    break
            else:
                return None
        return False

    async def _delete_thread_request(self, thread_id: str) -> Any:
        """Bridge SDK releases that have the generated RPC types but no flat method."""
        delete = getattr(self.codex, "thread_delete", None)
        if callable(delete):
            return await delete(thread_id)

        client = getattr(self.codex, "_client", None)
        request = getattr(client, "request", None)
        if not callable(request):
            raise ConsoleUnavailable
        params = ThreadDeleteParams(thread_id=thread_id)
        return await request(
            "thread/delete",
            params.model_dump(mode="json", by_alias=True, exclude_none=True),
            response_model=ThreadDeleteResponse,
        )

    async def unarchive_thread(self, thread_id: str) -> dict[str, Any]:
        try:
            await self.turn_manager.reserve_mutation(thread_id)
        except TurnConflictError as exc:
            raise ConsoleConflict from exc
        try:
            project, _listed = await self._find_thread(thread_id)
            thread = await self._call(
                self.codex.thread_unarchive(thread_id),
                operation="thread_unarchive",
            )
            response = await self._call(
                thread.read(include_turns=True),
                operation="thread_read_unarchived",
            )
            if self.registry.project_for_path(field(field(response, "thread"), "cwd", "")) != project:
                self._pending_threads.pop(thread_id, None)
                raise ConsoleNotFound
            view = self._read_view(response, project)
            self._refresh_pending_thread(
                thread_id,
                handle=thread,
                view=view,
            )
            pending = self._pending_threads.get(thread_id)
            if pending is not None:
                pending.archived = False
                pending.view = {**pending.view, "archived": False}
            return view
        finally:
            await self.turn_manager.finish_mutation(thread_id)

    async def fork_thread(self, thread_id: str) -> dict[str, Any]:
        try:
            await self.turn_manager.reserve_mutation(thread_id)
        except TurnConflictError as exc:
            raise ConsoleConflict from exc
        try:
            project, _thread, _response = await self._authorized_thread(
                thread_id,
                include_turns=False,
            )
            forked = await self._call(
                self.codex.thread_fork(
                    thread_id,
                    approval_mode=self.approval_mode,
                    sandbox=self.sandbox,
                ),
                operation="thread_fork",
            )
            response = await self._call(
                forked.read(include_turns=True),
                operation="thread_read_forked",
            )
            if self.registry.project_for_path(field(field(response, "thread"), "cwd", "")) != project:
                raise ConsoleNotFound
            view = self._read_view(response, project)
            self._remember_pending_thread(
                project=project,
                handle=forked,
                view=view,
            )
            return view
        finally:
            await self.turn_manager.finish_mutation(thread_id)

    async def start_turn(
        self,
        thread_id: str,
        *,
        prompt: str,
        model: str | None,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        try:
            await self.turn_manager.reserve(
                thread_id,
                model=model,
                reasoning_effort=reasoning_effort,
            )
        except (TurnConflictError, TurnsUnavailableError) as exc:
            raise ConsoleConflict from exc

        await self._publish(
            thread_id,
            event_type="console.turn.starting",
            method="console.turn.starting",
            data={
                "model": model,
                "reasoning_effort": reasoning_effort,
            },
        )
        try:
            _project, thread, _response = await self._authorized_thread(
                thread_id,
                include_turns=False,
            )
            handle = await self._call(
                thread.turn(
                    prompt,
                    effort=reasoning_effort,
                    model=model,
                    approval_mode=self.approval_mode,
                    sandbox=self.sandbox,
                ),
                operation="turn_start",
                invalid_is_bad_request=True,
            )
            turn_id = str(field(handle, "id", ""))
            if not turn_id:
                raise ConsoleUnavailable
            await self.turn_manager.mark_running(
                thread_id,
                turn_id=turn_id,
                handle=handle,
            )
            user_item_id = f"console-user-{uuid.uuid4().hex}"
            await self._publish(
                thread_id,
                event_type="codex.notification",
                method="item/completed",
                data={
                    "item": {
                        "id": user_item_id,
                        "type": "userMessage",
                        "content": [{"type": "text", "text": prompt}],
                    }
                },
                turn_id=turn_id,
            )
            task = asyncio.create_task(
                self._pump_turn(thread_id, turn_id, handle),
                name=f"codex-turn:{thread_id}:{turn_id}",
            )
            await self.turn_manager.attach_task(thread_id, task)
            running_event = await self._publish(
                thread_id,
                event_type="console.turn.running",
                method="console.turn.running",
                data={
                    "model": model,
                    "reasoning_effort": reasoning_effort,
                },
                turn_id=turn_id,
            )
            return {
                "accepted": True,
                "thread_id": thread_id,
                "turn_id": turn_id,
                "journal_cursor": running_event.sequence if running_event else None,
            }
        except asyncio.CancelledError:
            await self.turn_manager.finish(thread_id)
            await self._publish(
                thread_id,
                event_type="console.turn.idle",
                method="console.turn.idle",
            )
            raise
        except Exception:
            await self.turn_manager.finish(thread_id)
            await self._publish(
                thread_id,
                event_type="console.turn.error",
                method="console.turn.error",
                data={"error_code": "turn_start_failed"},
            )
            raise

    @staticmethod
    def _goal_view(goal: Any | None) -> dict[str, Any] | None:
        if goal is None:
            return None
        view = to_primitive(goal)
        if not isinstance(view, dict):
            raise ConsoleUnavailable
        return view

    async def get_goal(self, thread_id: str) -> dict[str, Any] | None:
        await self._authorized_thread(thread_id, include_turns=False)
        goal = await self._call(
            self.goal_adapter.get(thread_id),
            operation="thread_goal_get",
        )
        return self._goal_view(goal)

    async def start_goal(
        self,
        thread_id: str,
        *,
        objective: str,
        token_budget: int | None,
        model: str | None,
        reasoning_effort: str | None,
    ) -> dict[str, Any]:
        try:
            await self.turn_manager.reserve(
                thread_id,
                kind="goal",
                model=model,
                reasoning_effort=reasoning_effort,
            )
        except (TurnConflictError, TurnsUnavailableError) as exc:
            raise ConsoleConflict from exc

        await self._publish(
            thread_id,
            event_type="console.goal.starting",
            method="console.goal.starting",
            data={
                "token_budget": token_budget,
                "model": model,
                "reasoning_effort": reasoning_effort,
            },
        )
        try:
            await self._authorized_thread(
                thread_id,
                include_turns=False,
                model=model,
                reasoning_effort=reasoning_effort,
            )
            handle, raw_goal = await self._call(
                self.goal_adapter.start(
                    thread_id,
                    objective=objective,
                    token_budget=token_budget,
                ),
                operation="thread_goal_start",
                invalid_is_bad_request=True,
            )
            turn_id = str(field(handle, "id", ""))
            if not turn_id:
                raise ConsoleUnavailable
            goal = self._goal_view(raw_goal)
            if goal is None:
                raise ConsoleUnavailable
            await self.turn_manager.mark_running(
                thread_id,
                turn_id=turn_id,
                handle=handle,
            )
            task = asyncio.create_task(
                self._pump_goal(thread_id, turn_id, handle),
                name=f"codex-goal:{thread_id}:{turn_id}",
            )
            await self.turn_manager.attach_task(thread_id, task)
            await self._publish(
                thread_id,
                event_type="console.goal.running",
                method="console.goal.running",
                data={
                    "goal": goal,
                    "model": model,
                    "reasoning_effort": reasoning_effort,
                },
                turn_id=turn_id,
            )
            return {
                "accepted": True,
                "thread_id": thread_id,
                "turn_id": turn_id,
                "goal": goal,
                "model": model,
                "reasoning_effort": reasoning_effort,
            }
        except asyncio.CancelledError:
            await self.turn_manager.finish(thread_id)
            await self._publish(
                thread_id,
                event_type="console.goal.idle",
                method="console.goal.idle",
            )
            raise
        except Exception:
            await self.turn_manager.finish(thread_id)
            await self._publish(
                thread_id,
                event_type="console.goal.error",
                method="console.goal.error",
                data={"error_code": "goal_start_failed"},
            )
            raise

    async def resume_goal(
        self,
        thread_id: str,
        *,
        model: str | None,
        reasoning_effort: str | None,
    ) -> dict[str, Any]:
        try:
            await self.turn_manager.reserve(
                thread_id,
                kind="goal",
                model=model,
                reasoning_effort=reasoning_effort,
            )
        except (TurnConflictError, TurnsUnavailableError) as exc:
            raise ConsoleConflict from exc

        await self._publish(
            thread_id,
            event_type="console.goal.starting",
            method="console.goal.starting",
            data={"model": model, "reasoning_effort": reasoning_effort},
        )
        try:
            await self._authorized_thread(
                thread_id,
                include_turns=False,
                model=model,
                reasoning_effort=reasoning_effort,
            )
            existing = await self._call(
                self.goal_adapter.get(thread_id),
                operation="thread_goal_get",
            )
            if existing is None:
                raise ConsoleBadRequest
            handle, raw_goal = await self._call(
                self.goal_adapter.resume(thread_id),
                operation="thread_goal_resume",
                invalid_is_bad_request=True,
            )
            turn_id = str(field(handle, "id", ""))
            goal = self._goal_view(raw_goal)
            if not turn_id or goal is None:
                raise ConsoleUnavailable
            await self.turn_manager.mark_running(
                thread_id,
                turn_id=turn_id,
                handle=handle,
            )
            task = asyncio.create_task(
                self._pump_goal(thread_id, turn_id, handle),
                name=f"codex-goal:{thread_id}:{turn_id}",
            )
            await self.turn_manager.attach_task(thread_id, task)
            await self._publish(
                thread_id,
                event_type="console.goal.running",
                method="console.goal.running",
                data={
                    "goal": goal,
                    "model": model,
                    "reasoning_effort": reasoning_effort,
                },
                turn_id=turn_id,
            )
            return {
                "accepted": True,
                "thread_id": thread_id,
                "turn_id": turn_id,
                "goal": goal,
                "model": model,
                "reasoning_effort": reasoning_effort,
            }
        except asyncio.CancelledError:
            await self.turn_manager.finish(thread_id)
            await self._publish(
                thread_id,
                event_type="console.goal.idle",
                method="console.goal.idle",
            )
            raise
        except Exception:
            await self.turn_manager.finish(thread_id)
            await self._publish(
                thread_id,
                event_type="console.goal.error",
                method="console.goal.error",
                data={"error_code": "goal_resume_failed"},
            )
            raise

    async def pause_goal(self, thread_id: str) -> dict[str, Any]:
        await self._authorized_thread(thread_id, include_turns=False)
        active = await self.turn_manager.current(thread_id)
        reserved_mutation = False
        if active is not None and active.kind != "goal":
            raise ConsoleConflict
        try:
            if active is not None:
                if active.handle is None:
                    raise ConsoleConflict
                await self._call(
                    self.turn_manager.interrupt(thread_id),
                    operation="thread_goal_pause",
                )
            else:
                try:
                    await self.turn_manager.reserve_mutation(thread_id)
                except TurnConflictError as exc:
                    raise ConsoleConflict from exc
                reserved_mutation = True
                await self._call(
                    self.goal_adapter.pause(thread_id),
                    operation="thread_goal_pause",
                    invalid_is_bad_request=True,
                )
            raw_goal = await self._call(
                self.goal_adapter.get(thread_id),
                operation="thread_goal_get",
            )
            goal = self._goal_view(raw_goal)
            if goal is None:
                raise ConsoleBadRequest
            await self._publish(
                thread_id,
                event_type="console.goal.updated",
                method="thread/goal/updated",
                data={"thread_id": thread_id, "goal": goal},
                turn_id=active.turn_id if active is not None else None,
            )
            return {"accepted": True, "thread_id": thread_id, "goal": goal}
        finally:
            if reserved_mutation:
                await self.turn_manager.finish_mutation(thread_id)

    async def clear_goal(self, thread_id: str) -> dict[str, Any]:
        await self._authorized_thread(thread_id, include_turns=False)
        active = await self.turn_manager.current(thread_id)
        reserved_mutation = False
        if active is not None and active.kind != "goal":
            raise ConsoleConflict
        try:
            if active is not None:
                if active.handle is None:
                    raise ConsoleConflict
                await self._call(
                    self.turn_manager.interrupt(thread_id),
                    operation="thread_goal_stop_before_clear",
                )
            else:
                try:
                    await self.turn_manager.reserve_mutation(thread_id)
                except TurnConflictError as exc:
                    raise ConsoleConflict from exc
                reserved_mutation = True
            cleared = await self._call(
                self.goal_adapter.clear(thread_id),
                operation="thread_goal_clear",
                invalid_is_bad_request=True,
            )
            await self._publish(
                thread_id,
                event_type="console.goal.cleared",
                method="thread/goal/cleared",
                data={"thread_id": thread_id},
                turn_id=active.turn_id if active is not None else None,
            )
            return {"cleared": bool(cleared), "thread_id": thread_id}
        finally:
            if reserved_mutation:
                await self.turn_manager.finish_mutation(thread_id)

    async def _pump_goal(self, thread_id: str, turn_id: str, handle: Any) -> None:
        try:
            async for notification in handle.stream():
                method, data = notification_view(notification)
                await self._publish(
                    thread_id,
                    event_type="codex.notification",
                    method=method,
                    data=data,
                    turn_id=turn_id,
                    channel="scoped",
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGW(f"Codex goal stream failed: {type(exc).__name__}")
            await self._publish(
                thread_id,
                event_type="console.goal.error",
                method="console.goal.error",
                data={"error_code": "goal_stream_failed"},
                turn_id=turn_id,
            )
        finally:
            await self.turn_manager.finish(thread_id, turn_id=turn_id)
            await self._publish(
                thread_id,
                event_type="console.goal.idle",
                method="console.goal.idle",
                turn_id=turn_id,
            )

    async def _pump_turn(self, thread_id: str, turn_id: str, handle: Any) -> None:
        try:
            async for notification in handle.stream():
                method, data = notification_view(notification)
                await self._publish(
                    thread_id,
                    event_type="codex.notification",
                    method=method,
                    data=data,
                    turn_id=turn_id,
                    channel="scoped",
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGW(f"Codex turn stream failed: {type(exc).__name__}")
            await self._publish(
                thread_id,
                event_type="console.turn.error",
                method="console.turn.error",
                data={"error_code": "turn_stream_failed"},
                turn_id=turn_id,
            )
        finally:
            await self.turn_manager.finish(thread_id, turn_id=turn_id)
            await self._publish(
                thread_id,
                event_type="console.turn.idle",
                method="console.turn.idle",
                turn_id=turn_id,
            )

    async def steer_turn(self, thread_id: str, prompt: str) -> dict[str, Any]:
        try:
            active = await self.turn_manager.active(thread_id)
            await self._call(
                active.handle.steer(prompt),
                operation="turn_steer",
            )
        except TurnNotActiveError as exc:
            raise ConsoleConflict from exc
        user_item_id = f"console-user-{uuid.uuid4().hex}"
        await self._publish(
            thread_id,
            event_type="codex.notification",
            method="item/completed",
            data={
                "item": {
                    "id": user_item_id,
                    "type": "userMessage",
                    "content": [{"type": "text", "text": prompt}],
                }
            },
            turn_id=active.turn_id,
        )
        steered_event = await self._publish(
            thread_id,
            event_type="console.turn.steered",
            method="console.turn.steered",
            data={"accepted": True},
            turn_id=active.turn_id,
        )
        return {
            "accepted": True,
            "thread_id": thread_id,
            "turn_id": active.turn_id,
            "journal_cursor": steered_event.sequence if steered_event else None,
        }

    async def interrupt_turn(self, thread_id: str) -> dict[str, Any]:
        try:
            active = await self.turn_manager.active(thread_id)
            await self._call(
                self.turn_manager.interrupt(thread_id),
                operation="turn_interrupt",
            )
        except TurnNotActiveError as exc:
            raise ConsoleConflict from exc
        await self._publish(
            thread_id,
            event_type=("console.goal.stopping" if active.kind == "goal" else "console.turn.stopping"),
            method=("console.goal.stopping" if active.kind == "goal" else "console.turn.stopping"),
            turn_id=active.turn_id,
        )
        return {"accepted": True, "thread_id": thread_id, "turn_id": active.turn_id}
