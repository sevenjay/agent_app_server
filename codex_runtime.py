"""ASGI-loop-owned lifecycle for the single AsyncCodex process client."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from openai_codex import ApprovalMode, AsyncCodex, Sandbox
from openai_codex.generated.v2_all import GetAccountRateLimitsResponse

from codex_serializers import field, notification_view, to_primitive
from codex_service import CodexService, ConsoleNotFound, ConsoleUnavailable
from event_hub import EventHub
from projects import ProjectRegistry
from stream_journal import StreamJournal
from turn_manager import TurnManager
from utility.log import LOGD, LOGI, LOGW


def _approval_mode(value: str) -> ApprovalMode:
    try:
        return ApprovalMode(value)
    except ValueError as exc:
        raise RuntimeError(f"Unsupported codex_approval_mode: {value!r}") from exc


def _sandbox(value: str) -> Sandbox:
    try:
        return Sandbox(value.replace("_", "-"))
    except ValueError as exc:
        raise RuntimeError(f"Unsupported codex_sandbox: {value!r}") from exc


_PLAN_LABELS = {
    "apiKey": "API key",
    "business": "Business",
    "edu": "Edu",
    "enterprise": "Enterprise",
    "enterprise_cbp_usage_based": "Enterprise",
    "free": "Free",
    "go": "Go",
    "plus": "Plus",
    "pro": "Pro",
    "prolite": "Pro Lite",
    "self_serve_business_usage_based": "Business",
    "team": "Team",
}

def _account_label(account: Any) -> str | None:
    view = to_primitive(account)
    if not isinstance(view, dict):
        return None
    account_type = str(view.get("type") or "")
    if account_type == "chatgpt":
        email = view.get("email")
        plan = view.get("plan_type") or view.get("planType")
        plan_label = _PLAN_LABELS.get(str(plan), str(plan).replace("_", " ").title()) if plan else None
        if email and plan_label:
            return f"{email} ({plan_label})"
        return str(email or plan_label or "ChatGPT")
    return _PLAN_LABELS.get(account_type, account_type.replace("_", " ").title()) or None


def _selected_agents_file(directory: Path) -> Path | None:
    for filename in ("AGENTS.override.md", "AGENTS.md"):
        candidate = directory / filename
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def _discover_agents_md(
    cwd: Path,
    *,
    codex_home: Path | None = None,
) -> list[str]:
    """Return the AGENTS.md paths that describe the startup working directory."""
    cwd = cwd.resolve()
    home = Path.home().resolve()
    configured_codex_home = codex_home
    if configured_codex_home is None:
        configured_codex_home = Path(os.environ.get("CODEX_HOME", home / ".codex"))

    discovered: list[tuple[Path, bool]] = []
    global_agents = _selected_agents_file(configured_codex_home.expanduser().resolve())
    if global_agents is not None:
        discovered.append((global_agents, False))

    ancestors = [cwd, *cwd.parents]
    project_root = next(
        (directory for directory in ancestors if (directory / ".git").exists()),
        cwd,
    )
    project_directories = list(reversed(ancestors[: ancestors.index(project_root) + 1]))
    for directory in project_directories:
        agents_file = _selected_agents_file(directory)
        if agents_file is not None and all(agents_file != path for path, _ in discovered):
            discovered.append((agents_file, True))

    labels: list[str] = []
    for path, is_project_file in discovered:
        if is_project_file:
            labels.append(os.path.relpath(path, cwd))
            continue
        try:
            labels.append(f"~/{path.relative_to(home)}")
            continue
        except ValueError:
            pass
        labels.append(str(path))
    return labels


def _mapping_value(mapping: dict[str, Any], snake_name: str, camel_name: str) -> Any:
    return mapping.get(snake_name, mapping.get(camel_name))


def _limit_reset(timestamp: Any, *, timezone_name: str) -> tuple[str | None, str | None]:
    if not isinstance(timestamp, int | float):
        return None, None
    reset_utc = datetime.fromtimestamp(timestamp, timezone.utc)
    try:
        local_timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        local_timezone = timezone.utc
    reset_local = reset_utc.astimezone(local_timezone)
    return reset_utc.isoformat(), f"{reset_local:%H:%M} on {reset_local.day} {reset_local:%b}"


def _window_label(duration_minutes: Any) -> str:
    if not isinstance(duration_minutes, int | float) or duration_minutes <= 0:
        return "Usage limit"
    if duration_minutes >= 28 * 24 * 60:
        return "Monthly limit"
    if duration_minutes >= 7 * 24 * 60:
        return "Weekly limit"
    if duration_minutes % 60 == 0:
        return f"{int(duration_minutes // 60)}h limit"
    return f"{int(duration_minutes)}m limit"


def _rate_limit_rows(response: Any, *, timezone_name: str) -> list[dict[str, Any]]:
    data = to_primitive(response)
    if not isinstance(data, dict):
        return []
    snapshot = _mapping_value(data, "rate_limits", "rateLimits")
    if not isinstance(snapshot, dict):
        return []

    rows: dict[str, dict[str, Any]] = {}
    for window_name in ("primary", "secondary"):
        window = snapshot.get(window_name)
        if not isinstance(window, dict):
            continue
        used_percent = _mapping_value(window, "used_percent", "usedPercent")
        if not isinstance(used_percent, int | float):
            continue
        duration = _mapping_value(window, "window_duration_mins", "windowDurationMins")
        label = _window_label(duration)
        resets_at, reset_label = _limit_reset(
            _mapping_value(window, "resets_at", "resetsAt"),
            timezone_name=timezone_name,
        )
        rows[label] = {
            "label": label,
            "remaining_percent": max(0, min(100, round(100 - used_percent))),
            "resets_at": resets_at,
            "reset_label": reset_label,
        }

    individual = _mapping_value(snapshot, "individual_limit", "individualLimit")
    if isinstance(individual, dict):
        remaining_percent = _mapping_value(individual, "remaining_percent", "remainingPercent")
        if isinstance(remaining_percent, int | float):
            resets_at, reset_label = _limit_reset(
                _mapping_value(individual, "resets_at", "resetsAt"),
                timezone_name=timezone_name,
            )
            rows["Monthly limit"] = {
                "label": "Monthly limit",
                "remaining_percent": max(0, min(100, round(remaining_percent))),
                "resets_at": resets_at,
                "reset_label": reset_label,
            }

    order = {"Monthly limit": 0, "Weekly limit": 1}
    return sorted(rows.values(), key=lambda row: (order.get(row["label"], 2), row["label"]))


class CodexRuntime:
    def __init__(
        self,
        *,
        settings_obj: Any,
        registry: ProjectRegistry,
        client_factory: Callable[[], Any] = AsyncCodex,
        enabled: bool | None = None,
    ) -> None:
        self.settings = settings_obj
        self.registry = registry
        self.client_factory = client_factory
        self.enabled = (
            bool(getattr(settings_obj, "codex_enabled", True))
            if enabled is None
            else enabled
        )
        self.event_hub = EventHub(
            history_limit=int(getattr(settings_obj, "codex_event_history_limit", 2000)),
            subscriber_queue_limit=int(
                getattr(settings_obj, "codex_subscriber_queue_limit", 1000)
            ),
        )
        self.stream_journal = StreamJournal()
        self.turn_manager = TurnManager()
        self.client: Any = None
        self.service: CodexService | None = None
        self._global_notification_task: asyncio.Task[None] | None = None
        self._health_sample_task: asyncio.Task[None] | None = None
        self.ready = False
        self.account_available = False
        self.agents_md: list[str] = []
        self.account_label: str | None = None
        self.rate_limits: list[dict[str, Any]] = []
        self.rate_limits_sampled_at: datetime | None = None

    async def _operation(self, awaitable: Any, *, operation: str) -> Any:
        started_at = time.perf_counter()
        LOGD(f"codex_runtime_rpc_start operation={operation}")
        try:
            result = await asyncio.wait_for(
                awaitable,
                timeout=float(
                    getattr(self.settings, "codex_operation_timeout_seconds", 30)
                ),
            )
        except asyncio.CancelledError:
            elapsed_ms = (time.perf_counter() - started_at) * 1000
            LOGD(
                f"codex_runtime_rpc_cancelled operation={operation} "
                f"elapsed_ms={elapsed_ms:.1f}"
            )
            raise
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - started_at) * 1000
            LOGD(
                f"codex_runtime_rpc_failed operation={operation} "
                f"exception={type(exc).__name__} elapsed_ms={elapsed_ms:.1f}",
                exc_info=True,
            )
            raise
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        LOGD(
            f"codex_runtime_rpc_complete operation={operation} "
            f"elapsed_ms={elapsed_ms:.1f}"
        )
        return result

    async def start(self) -> None:
        LOGD(f"codex_runtime_start_requested enabled={self.enabled}")
        await self.turn_manager.enable()
        self.ready = False
        self.account_available = False
        self.agents_md = _discover_agents_md(Path.cwd())
        self.account_label = None
        self.rate_limits = []
        self.rate_limits_sampled_at = None
        if not self.enabled:
            LOGW("Codex runtime is disabled by configuration")
            return
        client = self.client_factory()
        self.client = client
        LOGD(f"codex_runtime_client_created client_type={type(client).__name__}")
        try:
            await self._operation(
                client.__aenter__(),
                operation="client_enter",
            )
            account_response = await self._operation(
                client.account(),
                operation="account_health",
            )
            account = field(account_response, "account")
            requires_auth = bool(field(account_response, "requires_openai_auth", False))
            if account is None and requires_auth:
                raise ConsoleUnavailable("The service account is not logged in to Codex")
            self.account_available = account is not None
            self.account_label = _account_label(account)
            self.service = CodexService(
                client,
                registry=self.registry,
                event_hub=self.event_hub,
                turn_manager=self.turn_manager,
                approval_mode=_approval_mode(
                    str(getattr(self.settings, "codex_approval_mode", "auto_review"))
                ),
                sandbox=_sandbox(
                    str(getattr(self.settings, "codex_sandbox", "workspace_write"))
                ),
                stream_journal=self.stream_journal,
                operation_timeout=float(
                    getattr(self.settings, "codex_operation_timeout_seconds", 30)
                ),
                lookup_page_limit=int(
                    getattr(self.settings, "codex_thread_lookup_page_limit", 50)
                ),
            )
            self.stream_journal.prune_retention(
                [project.path for project in self.registry],
                days=int(getattr(self.settings, "codex_journal_retention_days", 30)),
            )
            await self._sample_rate_limits(client)
            self.ready = True
            self._start_global_notification_pump(client)
            self._start_health_sample_loop(client)
            LOGD(
                f"codex_runtime_ready account_available={self.account_available} "
                f"project_count={len(self.registry)}"
            )
            LOGI("Codex runtime started and account health check completed")
        except Exception as exc:
            LOGD(
                f"codex_runtime_start_failed exception={type(exc).__name__}",
                exc_info=True,
            )
            try:
                await self._close_client(client)
            except Exception as close_exc:
                LOGW(f"Codex startup cleanup failed: {type(close_exc).__name__}")
            self.client = None
            self.service = None
            self.ready = False
            self.account_available = False
            self.account_label = None
            self.rate_limits = []
            self.rate_limits_sampled_at = None
            raise

    async def close(self) -> None:
        turn_status = await self.turn_manager.status()
        LOGD(
            f"codex_runtime_close_requested ready={self.ready} "
            f"client_present={self.client is not None} "
            f"active_turn_count={turn_status['active_turn_count']} "
            f"subscriber_count={await self.event_hub.subscriber_count()}"
        )
        self.ready = False
        self.account_available = False
        await self._stop_health_sample_loop()
        try:
            await self.turn_manager.shutdown(
                timeout=float(
                    getattr(self.settings, "codex_shutdown_timeout_seconds", 10)
                )
            )
        except Exception as exc:
            LOGW(f"Failed to drain every active Codex turn: {type(exc).__name__}")
        client = self.client
        self.client = None
        self.service = None
        self.account_label = None
        self.rate_limits = []
        self.rate_limits_sampled_at = None
        await self._stop_global_notification_pump()
        if client is not None:
            try:
                await self._close_client(client)
            except Exception as exc:
                LOGW(f"Codex client close failed: {type(exc).__name__}")
        LOGD("codex_runtime_close_complete")
        LOGI("Codex runtime stopped")

    def _global_notification_reader(
        self,
        client: Any,
    ) -> Callable[[], Any] | None:
        direct_reader = getattr(client, "next_notification", None)
        if callable(direct_reader):
            return direct_reader

        # AsyncCodex 0.144 exposes global notifications on its wrapped async
        # client, while keeping turn-scoped notifications on TurnHandle.stream().
        sdk_client = getattr(client, "_client", None)
        sdk_reader = getattr(sdk_client, "next_notification", None)
        return sdk_reader if callable(sdk_reader) else None

    def _start_global_notification_pump(self, client: Any) -> None:
        reader = self._global_notification_reader(client)
        if reader is None:
            LOGW("Codex client does not expose global notifications; live session status is limited")
            return
        self._global_notification_task = asyncio.create_task(
            self._pump_global_notifications(reader),
            name="codex-global-notifications",
        )

    async def _read_rate_limits(self, client: Any) -> Any:
        for method_name in ("rate_limits", "account_rate_limits"):
            method = getattr(client, method_name, None)
            if callable(method):
                return await method()

        sdk_client = getattr(client, "_client", None)
        request = getattr(sdk_client, "request", None)
        if not callable(request):
            raise RuntimeError("Codex client does not expose account rate limits")
        return await request(
            "account/rateLimits/read",
            None,
            response_model=GetAccountRateLimitsResponse,
        )

    async def _sample_rate_limits(self, client: Any) -> None:
        try:
            response = await self._operation(
                self._read_rate_limits(client),
                operation="rate_limits_health",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGW(f"Codex rate-limit health sample failed: {type(exc).__name__}")
            return
        self.rate_limits = _rate_limit_rows(
            response,
            timezone_name=str(getattr(self.settings, "scheduler_timezone", "UTC")),
        )
        self.rate_limits_sampled_at = datetime.now(timezone.utc)
        LOGD("Codex rate-limit health sample completed")

    def _start_health_sample_loop(self, client: Any) -> None:
        self._health_sample_task = asyncio.create_task(
            self._health_sample_loop(client),
            name="codex-runtime-health-samples",
        )

    async def _stop_health_sample_loop(self) -> None:
        task = self._health_sample_task
        self._health_sample_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _health_sample_loop(self, client: Any) -> None:
        interval = max(
            0.01,
            float(getattr(self.settings, "scheduler_health_sample_seconds", 300)),
        )
        try:
            while True:
                await asyncio.sleep(interval)
                if not self.ready or self.client is not client:
                    return
                await self._sample_rate_limits(client)
        except asyncio.CancelledError:
            raise

    async def _stop_global_notification_pump(self) -> None:
        task = self._global_notification_task
        self._global_notification_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _pump_global_notifications(
        self,
        next_notification: Callable[[], Any],
    ) -> None:
        LOGD("codex_global_notifications_start")
        try:
            while True:
                notification = await next_notification()
                method, data = notification_view(notification)
                thread_id = data.get("thread_id") or data.get("threadId")
                if not isinstance(thread_id, str) or not thread_id:
                    continue
                service = self.service
                if service is None:
                    continue
                try:
                    await service.publish_notification(
                        thread_id,
                        method=method,
                        data=data,
                    )
                except ConsoleNotFound:
                    # Ephemeral helper threads are intentionally absent from the
                    # project session list and must not stop the global pump.
                    continue
        except asyncio.CancelledError:
            LOGD("codex_global_notifications_cancelled")
            raise
        except Exception as exc:
            if self.ready:
                LOGW(
                    f"Codex global notification stream failed: {type(exc).__name__}"
                )
        finally:
            LOGD("codex_global_notifications_stopped")

    async def _close_client(self, client: Any) -> None:
        LOGD(f"codex_runtime_client_close_start client_type={type(client).__name__}")
        try:
            await asyncio.wait_for(
                client.__aexit__(None, None, None),
                timeout=float(
                    getattr(self.settings, "codex_shutdown_timeout_seconds", 10)
                ),
            )
        except Exception as exc:
            LOGD(
                f"codex_runtime_client_close_failed "
                f"client_type={type(client).__name__} "
                f"exception={type(exc).__name__}",
                exc_info=True,
            )
            raise
        LOGD(f"codex_runtime_client_close_complete client_type={type(client).__name__}")

    def require_service(self) -> CodexService:
        if not self.ready or self.service is None:
            LOGD(
                f"codex_runtime_service_unavailable ready={self.ready} "
                f"service_present={self.service is not None} "
                f"client_present={self.client is not None}"
            )
            raise ConsoleUnavailable
        return self.service

    async def status(self) -> dict[str, Any]:
        turn_status = await self.turn_manager.status()
        approval_mode = str(
            getattr(self.settings, "codex_approval_mode", "auto_review")
        )
        sandbox = str(getattr(self.settings, "codex_sandbox", "workspace_write"))
        return {
            "enabled": self.enabled,
            "ready": self.ready,
            "account_available": self.account_available,
            "account_label": self.account_label,
            "agents_md": list(self.agents_md),
            "limits": [dict(limit) for limit in self.rate_limits],
            "limits_sampled_at": (
                self.rate_limits_sampled_at.isoformat()
                if self.rate_limits_sampled_at
                else None
            ),
            "approval_mode": approval_mode,
            "sandbox": sandbox,
            "accepting_turns": turn_status["accepting_turns"] and self.ready,
            "active_turn_count": turn_status["active_turn_count"],
            "subscriber_count": await self.event_hub.subscriber_count(),
            "dropped_subscriber_count": self.event_hub.dropped_subscriber_count,
            "journal": self.stream_journal.stats(
                [project.path for project in self.registry]
            ),
        }
