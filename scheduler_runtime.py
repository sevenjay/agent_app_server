"""APScheduler registration and lifecycle management."""

from __future__ import annotations

from datetime import datetime, timezone
import threading
from typing import Any

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, EVENT_JOB_MISSED
from apscheduler.schedulers.background import BackgroundScheduler
from utility.log import LOGD, LOGI, LOGW, LOGException

from config import settings


JOB_DEFAULTS = {
    "coalesce": True,
    "max_instances": 1,
    "misfire_grace_time": 300,
}

_state_lock = threading.Lock()
_last_health_sample: datetime | None = None
scheduler: BackgroundScheduler | None = None


def _create_scheduler() -> BackgroundScheduler:
    return BackgroundScheduler(
        job_defaults=JOB_DEFAULTS,
        timezone=str(getattr(settings, "scheduler_timezone", "UTC")),
    )


def health_sample_job() -> None:
    """Record a low-frequency scheduler liveness sample."""
    global _last_health_sample
    with _state_lock:
        _last_health_sample = datetime.now(timezone.utc)
    LOGD("Scheduler health sample completed")


def _job_listener(event: Any) -> None:
    if event.code == EVENT_JOB_MISSED:
        LOGW(f"Scheduled job missed: {event.job_id}")
    elif getattr(event, "exception", None):
        LOGException(event.exception)
        LOGW(f"Scheduled job failed: {event.job_id}")
    else:
        LOGD(f"Scheduled job succeeded: {event.job_id}")


def start_scheduler() -> None:
    global scheduler
    if scheduler is not None and scheduler.running:
        return

    scheduler = _create_scheduler()
    scheduler.add_listener(
        _job_listener,
        EVENT_JOB_EXECUTED | EVENT_JOB_ERROR | EVENT_JOB_MISSED,
    )
    scheduler.add_job(
        health_sample_job,
        trigger="interval",
        seconds=int(getattr(settings, "scheduler_health_sample_seconds", 300)),
        id="runtime_health_sample",
        replace_existing=True,
    )
    scheduler.start()
    LOGI("APScheduler started")


def stop_scheduler() -> None:
    global scheduler
    if scheduler is not None and scheduler.running:
        scheduler.shutdown(wait=False)
        LOGI("APScheduler stopped")
    scheduler = None


def scheduler_status() -> dict[str, Any]:
    with _state_lock:
        health_sample = _last_health_sample
    active_scheduler = scheduler
    return {
        "running": bool(active_scheduler and active_scheduler.running),
        "job_count": len(active_scheduler.get_jobs()) if active_scheduler else 0,
        "last_health_sample_at": health_sample.isoformat() if health_sample else None,
    }
