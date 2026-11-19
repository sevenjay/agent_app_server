"""Persisted recurrence and dispatch into the normal Codex session flow."""

from __future__ import annotations

import asyncio
from datetime import datetime, time as local_time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select, update

import scheduler_runtime
from codex_runtime import CodexRuntime
from codex_service import ConsoleServiceError
from database import async_session
from models import Schedule
from tenancy import TenantWorkspaceManager
from utility.log import LOGW


WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DISPATCH_JOB_ID = "scheduled_prompts"
POLL_SECONDS = 5


def next_run_at(*, time: str, timezone: str, days_of_week: list[str], after: datetime) -> int:
    """Find the next local occurrence and return its UTC epoch seconds."""
    zone = ZoneInfo(timezone)
    wall_time = local_time.fromisoformat(time)
    date = after.astimezone(zone).date()
    cutoff = after.timestamp()
    while True:
        if not days_of_week or WEEKDAYS[date.weekday()] in days_of_week:
            local = datetime.combine(date, wall_time, tzinfo=zone)
            stamp = local.timestamp()
            # The default fold picks the first occurrence on a fall-back day.
            # Skip a wall time that falls in a spring-forward gap.
            if stamp > cutoff and datetime.fromtimestamp(stamp, zone) == local:
                return int(stamp)
        date += timedelta(days=1)


def schedule_view(schedule: Schedule) -> dict[str, Any]:
    zone = ZoneInfo(schedule.timezone)

    def local_iso(stamp: int | None) -> str | None:
        return datetime.fromtimestamp(stamp, zone).isoformat() if stamp is not None else None

    return {
        "id": schedule.id,
        "project_key": schedule.project_key,
        "prompt": schedule.prompt,
        "model": schedule.model,
        "reasoning_effort": schedule.reasoning_effort,
        "frequency": schedule.frequency,
        "days_of_week": schedule.days_of_week,
        "time": schedule.time,
        "timezone": schedule.timezone,
        "enabled": schedule.next_run_at is not None,
        "next_run_at": local_iso(schedule.next_run_at),
        "last_run_at": local_iso(schedule.last_run_at),
        "last_thread_id": schedule.last_thread_id,
        "last_run_error": schedule.last_run_error,
    }


class ScheduledPrompts:
    def __init__(self, runtime: CodexRuntime, tenants: TenantWorkspaceManager) -> None:
        self.runtime = runtime
        self.tenants = tenants
        self._scheduler = None
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        scheduler = scheduler_runtime.scheduler
        if scheduler is None or not scheduler.running:
            return
        self._scheduler = scheduler
        scheduler.add_job(
            asyncio.get_running_loop().call_soon_threadsafe,
            args=(self._start_tick,),
            trigger="interval",
            seconds=POLL_SECONDS,
            next_run_time=datetime.now(timezone.utc),
            id=DISPATCH_JOB_ID,
            replace_existing=True,
        )

    async def stop(self) -> None:
        scheduler = self._scheduler
        self._scheduler = None
        if scheduler is not None and scheduler.get_job(DISPATCH_JOB_ID) is not None:
            scheduler.remove_job(DISPATCH_JOB_ID)
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    def _start_tick(self) -> None:
        if self._scheduler is not None and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self.run_due(), name=DISPATCH_JOB_ID)

    async def run_due(self, *, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        try:
            async with async_session() as session:
                due = (await session.scalars(select(Schedule).where(Schedule.next_run_at <= int(now.timestamp())))).all()
            await asyncio.gather(*(self._dispatch(schedule, now) for schedule in due))
        except Exception as exc:
            LOGW(f"Could not check for due schedules: {type(exc).__name__}")

    async def _dispatch(self, schedule: Schedule, now: datetime) -> None:
        due = schedule.next_run_at
        if due is None:
            return
        grace = scheduler_runtime.JOB_DEFAULTS["misfire_grace_time"]
        run = now.timestamp() - due <= grace
        identity = (Schedule.tenant_id == schedule.tenant_id, Schedule.id == schedule.id)
        try:
            values = {"next_run_at": next_run_at(
                time=schedule.time, timezone=schedule.timezone, days_of_week=schedule.days_of_week, after=now,
            )}
            if run:
                values.update(last_run_at=due, last_thread_id=None, last_run_error=None)
            async with async_session() as session:
                # Advance the cursor before contacting Codex so a restart or another tick cannot replay this occurrence.
                claim = await session.scalar(
                    update(Schedule).where(*identity, Schedule.next_run_at == due).values(**values).returning(Schedule)
                )
                await session.commit()
            if claim is None or not run:
                return

            thread_id, error = await self._submit(claim)
            async with async_session() as session:
                await session.execute(
                    update(Schedule).where(*identity, Schedule.last_run_at == due)
                    .values(last_thread_id=thread_id, last_run_error=error)
                )
                await session.commit()
        except Exception as exc:
            LOGW(f"Could not dispatch schedule id={schedule.id}: {type(exc).__name__}")

    async def _submit(self, schedule: Schedule) -> tuple[str | None, str | None]:
        try:
            async with async_session() as session:
                owner = await self.tenants.resolve_stored(schedule.tenant_id, session)
            service = self.runtime.require_service(tenant_id=owner.tenant_id, registry=owner.registry)
            title = " ".join(schedule.prompt.split())[:160]
            thread = await service.create_thread_from_prompt(
                project_key=schedule.project_key,
                prompt=schedule.prompt,
                model=schedule.model,
                reasoning_effort=schedule.reasoning_effort,
                name=f"Scheduled: {title}",
            )
            return str(thread["id"]), None
        except Exception as exc:
            LOGW(f"Could not start scheduled session id={schedule.id}: {type(exc).__name__}")
            message = exc.safe_message if isinstance(exc, ConsoleServiceError) else "Could not start the scheduled session."
            return None, message
