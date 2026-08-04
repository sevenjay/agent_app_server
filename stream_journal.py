"""Durable, append-only per-thread Stream Journal and timeline materializer."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any

from utility.log import LOGD, LOGW

JOURNAL_VERSION = 1
THREAD_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
TERMINAL_TYPES = frozenset({"turn.completed", "turn.error", "turn.interrupted"})
FSYNC_TYPES = frozenset(
    {
        "agent_message.completed",
        "command.completed",
        "turn.completed",
        "turn.error",
        "turn.interrupted",
    }
)
OUTPUT_LIMIT_BYTES = 64 * 1024
_SENSITIVE_VALUE = re.compile(
    r"(?i)("
    r"(?:authorization\s*:\s*(?:bearer|basic)\s+)[^\s,;]+"
    r"|(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|passwd|cookie|secret)"
    r"\s*[:=]\s*[^\s,;]+"
    r"|(?:sk|sess|ghp|github_pat)-[A-Za-z0-9_-]{12,}"
    r")"
)
_SENSITIVE_KEY = re.compile(r"(?i)(authorization|cookie|password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token)")


class InvalidThreadId(ValueError):
    """Raised before an untrusted thread id can become part of a path."""


class UnsafeJournalPath(RuntimeError):
    """Raised rather than following a symlink in the private Journal tree."""


@dataclass(frozen=True, slots=True)
class JournalRead:
    events: tuple[dict[str, Any], ...]
    cursor: int
    coverage: str
    damaged: bool
    exists: bool


@dataclass(frozen=True, slots=True)
class JournalAppend:
    record: dict[str, Any]
    duplicate: bool = False


@dataclass(slots=True)
class _AppendRequest:
    project_path: Path
    thread_id: str
    values: dict[str, Any]
    future: asyncio.Future[JournalAppend]


@dataclass(slots=True)
class _WriterState:
    next_sequence: int
    dedup_events: dict[str, dict[str, Any]]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()[:24]


def _value(data: dict[str, Any], snake: str, camel: str | None = None) -> Any:
    if snake in data:
        return data[snake]
    return data.get(camel) if camel else None


def _redact_text(value: str) -> str:
    return _SENSITIVE_VALUE.sub("<redacted>", value)


def _safe_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        return "<depth-limit>"
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, dict):
        safe: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            safe[key] = "<redacted>" if _SENSITIVE_KEY.search(key) else _safe_value(item, depth=depth + 1)
        return safe
    if isinstance(value, (list, tuple)):
        return [_safe_value(item, depth=depth + 1) for item in value]
    return _redact_text(str(value))


def _safe_fields(data: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {field: _safe_value(data[field]) for field in fields if field in data}


def _safe_goal(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict) and isinstance(value.get("root"), dict):
        value = value["root"]
    if not isinstance(value, dict):
        return None
    return _safe_fields(
        value,
        (
            "thread_id",
            "threadId",
            "objective",
            "status",
            "token_budget",
            "tokenBudget",
            "tokens_used",
            "tokensUsed",
            "time_used_seconds",
            "timeUsedSeconds",
            "created_at",
            "createdAt",
            "updated_at",
            "updatedAt",
        ),
    )


def _truncate_output(value: Any, *, limit: int = OUTPUT_LIMIT_BYTES) -> tuple[str, int, bool]:
    raw_text = str(value or "")
    byte_count = len(raw_text.encode("utf-8"))
    text = _redact_text(raw_text)
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text, byte_count, byte_count > limit
    marker = "\n… <output truncated> …\n"
    payload_limit = max(0, limit - len(marker.encode("utf-8")))
    head_size = (payload_limit * 3) // 4
    tail_size = payload_limit - head_size
    head = encoded[:head_size].decode("utf-8", errors="ignore")
    tail = encoded[-tail_size:].decode("utf-8", errors="ignore")
    return f"{head}{marker}{tail}", byte_count, True


def _root_item(data: dict[str, Any]) -> dict[str, Any]:
    item = data.get("item")
    if isinstance(item, dict) and isinstance(item.get("root"), dict):
        item = item["root"]
    return item if isinstance(item, dict) else {}


def _message_text(item: dict[str, Any]) -> str:
    text = item.get("text")
    if isinstance(text, str):
        return text
    values: list[str] = []
    for content in item.get("content", ()) if isinstance(item.get("content"), list) else ():
        if not isinstance(content, dict):
            continue
        candidate = content.get("text", content.get("value"))
        if isinstance(candidate, str):
            values.append(candidate)
    return "\n".join(values)


def _safe_tool_item(item: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "id",
        "type",
        "status",
        "command",
        "exit_code",
        "exitCode",
        "duration_ms",
        "durationMs",
        "aggregated_output",
        "aggregatedOutput",
        "output",
        "result",
        "error",
        "changes",
        "action",
        "name",
        "server",
        "tool",
        "arguments",
        "input",
        "title",
        "namespace",
        "success",
        "content_items",
        "contentItems",
        "agents_states",
        "agentsStates",
        "prompt",
        "model",
        "reasoning_effort",
        "reasoningEffort",
        "receiver_thread_ids",
        "receiverThreadIds",
        "sender_thread_id",
        "senderThreadId",
        "output_byte_count",
        "output_truncated",
    }
    safe: dict[str, Any] = {}
    for key, value in item.items():
        if key not in allowed:
            continue
        if key == "changes" and isinstance(value, list):
            safe[key] = [
                {field: _safe_value(change[field]) for field in ("path", "kind", "type", "status") if field in change}
                for change in value
                if isinstance(change, dict)
            ]
        elif key == "action" and isinstance(value, dict):
            safe[key] = {field: _safe_value(value[field]) for field in ("type", "query", "queries", "url", "pattern") if field in value}
        else:
            safe[key] = _safe_value(value)
    output_key = next(
        (
            key
            for key in (
                "aggregated_output",
                "aggregatedOutput",
                "output",
                "result",
                "error",
                "content_items",
                "contentItems",
                "agents_states",
                "agentsStates",
            )
            if key in safe
        ),
        None,
    )
    if output_key is not None:
        original = item[output_key]
        original_text = (
            original if isinstance(original, str) else json.dumps(original, ensure_ascii=False, separators=(",", ":"), default=str)
        )
        safe_value = safe[output_key]
        safe_text = (
            safe_value if isinstance(safe_value, str) else json.dumps(safe_value, ensure_ascii=False, separators=(",", ":"), default=str)
        )
        output, _safe_byte_count, truncated = _truncate_output(safe_text)
        original_byte_count = len(original_text.encode("utf-8"))
        if truncated or original_byte_count > OUTPUT_LIMIT_BYTES:
            safe[output_key] = output
        safe["output_byte_count"] = original_byte_count
        safe["output_truncated"] = truncated or original_byte_count > OUTPUT_LIMIT_BYTES
    return safe


def normalize_event(
    *,
    thread_id: str,
    event_type: str,
    method: str,
    data: dict[str, Any] | None,
    turn_id: str | None = None,
    source: str = "codex_stream",
    dedup_key: str | None = None,
) -> dict[str, Any] | None:
    """Normalize an SDK/console event without retaining its private raw payload."""
    raw = data if isinstance(data, dict) else {}
    turn_value = raw.get("turn")
    if isinstance(turn_value, dict) and isinstance(turn_value.get("root"), dict):
        turn_value = turn_value["root"]
    turn_data = turn_value if isinstance(turn_value, dict) else {}
    normalized_turn_id = str(_value(raw, "turn_id", "turnId") or turn_data.get("id") or turn_id or "") or None
    item = _root_item(raw)
    private_item_type = str(item.get("type") or "").lower() in {
        "reasoning",
        "hookprompt",
        "hook_prompt",
    }
    if private_item_type or "reasoning" in method.lower() or "hook" in method.lower():
        return None
    item_id = str(item.get("id") or _value(raw, "item_id", "itemId") or "") or None
    if method == "item/agentMessage/delta" and item_id is None and normalized_turn_id:
        item_id = f"agent-{normalized_turn_id}"
    normalized_type = event_type
    safe_data: dict[str, Any]

    if method == "turn/started":
        normalized_type = "turn.started"
        safe_data = {"status": "running"}
    elif method == "turn/completed":
        status = str(raw.get("status") or turn_data.get("status") or "completed").lower()
        if status in {"interrupted", "cancelled", "canceled"}:
            normalized_type = "turn.interrupted"
        elif status in {"error", "failed"}:
            normalized_type = "turn.error"
        else:
            normalized_type = "turn.completed"
        safe_data = {"status": status}
    elif method == "turn/error":
        normalized_type = "turn.error"
        safe_data = {"status": "error", "error_code": str(raw.get("error_code") or "stream_error")}
    elif method == "turn/interrupted":
        normalized_type = "turn.interrupted"
        safe_data = {"status": "interrupted"}
    elif method == "item/agentMessage/delta":
        normalized_type = "agent_message.delta"
        safe_data = {
            "delta": _redact_text(str(raw.get("delta") or "")),
            "item_id": item_id,
        }
    elif method == "item/agentMessage/checkpoint":
        normalized_type = "agent_message.checkpoint"
        safe_data = {
            "text": _redact_text(str(raw.get("text") or raw.get("checkpoint") or "")),
            "item_id": item_id,
        }
    elif method == "item/completed" and item:
        sdk_type = str(item.get("type") or "tool")
        type_map = {
            "userMessage": "user_message.completed",
            "agentMessage": "agent_message.completed",
            "commandExecution": "command.completed",
            "fileChange": "file_change.completed",
            "webSearch": "web_search.completed",
            "mcpToolCall": "mcp_tool.completed",
            "mcpTool": "mcp_tool.completed",
        }
        normalized_type = type_map.get(sdk_type, "tool.completed")
        if sdk_type in {"userMessage", "agentMessage"}:
            safe_data = {
                "text": _redact_text(_message_text(item)),
                "item_id": item_id,
                "item_type": sdk_type,
            }
        else:
            safe_data = {"item": _safe_tool_item(item)}
    elif method == "item/started" and item:
        sdk_type = str(item.get("type") or "tool")
        if sdk_type in {"userMessage", "agentMessage"}:
            return None
        normalized_type = "command.started" if sdk_type == "commandExecution" else "tool.started"
        safe_data = {"item": _safe_tool_item(item)}
    elif method == "turn/plan/updated":
        normalized_type = "plan.updated"
        explanation = _redact_text(str(raw.get("explanation") or ""))
        plan = (
            [
                {
                    "step": _redact_text(str(step.get("step") or "")),
                    "status": str(step.get("status") or "pending"),
                }
                for step in raw.get("plan", ())
                if isinstance(step, dict) and step.get("step")
            ]
            if isinstance(raw.get("plan"), list)
            else []
        )
        plan_text = "\n".join(f"{step['status']}: {step['step']}" for step in plan)
        safe_data = {
            "explanation": explanation,
            "plan": plan,
            "text": "\n".join(value for value in (explanation, plan_text) if value),
        }
    elif method == "turn/diff/updated":
        normalized_type = "file_change.updated"
        diff, byte_count, truncated = _truncate_output(raw.get("diff"))
        safe_data = {
            "diff": diff,
            "output_byte_count": byte_count,
            "output_truncated": truncated,
        }
    elif "usage" in method.lower():
        normalized_type = "usage.updated"
        safe_data = _safe_fields(
            raw,
            (
                "token_usage",
                "tokenUsage",
                "total",
                "last",
                "model_context_window",
                "modelContextWindow",
            ),
        )
    elif method == "thread/status/changed":
        normalized_type = "thread.status_changed"
        safe_data = _safe_fields(raw, ("thread_id", "threadId"))
        status = raw.get("status")
        if isinstance(status, dict) and isinstance(status.get("root"), dict):
            status = status["root"]
        if isinstance(status, dict):
            safe_data["status"] = _safe_fields(
                status,
                ("type", "active_flags", "activeFlags"),
            )
        elif status is not None:
            safe_data["status"] = _safe_value(status)
    elif method == "thread/goal/updated":
        normalized_type = "goal.updated"
        safe_data = _safe_fields(raw, ("thread_id", "threadId"))
        goal = _safe_goal(raw.get("goal"))
        if goal is not None:
            safe_data["goal"] = goal
    elif method == "thread/goal/cleared":
        normalized_type = "goal.cleared"
        safe_data = _safe_fields(raw, ("thread_id", "threadId"))
    elif event_type.startswith("console.turn."):
        mapping = {
            "console.turn.running": "turn.started",
            "console.turn.error": "turn.error",
        }
        normalized_type = mapping.get(event_type, event_type)
        safe_data = _safe_fields(
            raw,
            ("model", "reasoning_effort", "error_code", "accepted"),
        )
    elif event_type.startswith("console.goal."):
        mapping = {
            "console.goal.running": "turn.started",
            "console.goal.error": "turn.error",
        }
        normalized_type = mapping.get(event_type, event_type)
        safe_data = _safe_fields(
            raw,
            (
                "token_budget",
                "model",
                "reasoning_effort",
                "error_code",
                "accepted",
            ),
        )
        goal = _safe_goal(raw.get("goal"))
        if goal is not None:
            safe_data["goal"] = goal
    else:
        return None

    notification_fingerprint: str | None = None
    if dedup_key is None:
        stable_parts = [thread_id, normalized_turn_id or "-", item_id or "-", normalized_type]
        if normalized_type in {"user_message.completed", "agent_message.completed"}:
            notification_fingerprint = ":".join(
                [
                    thread_id,
                    normalized_turn_id or "-",
                    normalized_type,
                    _fingerprint({key: value for key, value in safe_data.items() if key != "item_id"}),
                ]
            )
        elif normalized_type in {
            "agent_message.delta",
            "plan.updated",
            "usage.updated",
            "thread.status_changed",
        }:
            notification_fingerprint = ":".join([*stable_parts, _fingerprint(safe_data)])
            stable_parts.extend((_fingerprint(safe_data), uuid.uuid4().hex))
        elif not normalized_turn_id and not item_id:
            stable_parts.append(uuid.uuid4().hex)
        dedup_key = ":".join(stable_parts)

    return {
        "type": normalized_type,
        "event_type": event_type,
        "method": method,
        "source": source,
        "thread_id": thread_id,
        "turn_id": normalized_turn_id,
        "item_id": item_id,
        "dedup_key": dedup_key,
        "notification_fingerprint": notification_fingerprint,
        "data": safe_data,
    }


def _history_item_event(
    thread_id: str,
    turn_id: str,
    item: dict[str, Any],
    ordinal: int,
) -> dict[str, Any] | None:
    sdk_type = str(item.get("type") or "tool")
    if sdk_type.lower() in {"reasoning", "hookprompt", "hook_prompt"}:
        return None
    item_id = str(item.get("id") or f"history-{sdk_type}-{ordinal}")
    type_map = {
        "userMessage": "user_message.completed",
        "agentMessage": "agent_message.completed",
        "plan": "plan.updated",
        "commandExecution": "command.completed",
        "fileChange": "file_change.completed",
        "webSearch": "web_search.completed",
        "mcpToolCall": "mcp_tool.completed",
        "mcpTool": "mcp_tool.completed",
    }
    normalized_type = type_map.get(sdk_type, "tool.completed")
    if sdk_type in {"userMessage", "agentMessage", "plan"}:
        data = {"text": _redact_text(_message_text(item))}
    else:
        data = {"item": _safe_tool_item(item)}
    return {
        "type": normalized_type,
        "event_type": "codex.history",
        "method": "history/item",
        "source": "codex_history",
        "thread_id": thread_id,
        "turn_id": turn_id,
        "item_id": item_id,
        "dedup_key": f"history:{thread_id}:{turn_id}:{ordinal}:{normalized_type}:{_fingerprint(data)}",
        "data": data,
    }


def history_watermark(thread: dict[str, Any]) -> dict[str, Any]:
    turns = thread.get("turns", ())
    return {
        "updated_at": thread.get("updated_at"),
        "turn_count": len(turns),
        "content_fingerprint": _fingerprint(turns),
    }


def complete_stream_turn_ids(journal: JournalRead) -> frozenset[str]:
    """Return Turn IDs whose live stream has both lifecycle boundaries."""
    types_by_turn: dict[str, set[str]] = defaultdict(set)
    for event in journal.events:
        if event.get("source") != "codex_stream":
            continue
        turn_id = event.get("turn_id")
        if isinstance(turn_id, str) and turn_id:
            types_by_turn[turn_id].add(str(event.get("type") or ""))
    return frozenset(
        turn_id
        for turn_id, event_types in types_by_turn.items()
        if "turn.started" in event_types and event_types.intersection(TERMINAL_TYPES)
    )


class StreamJournal:
    """One application-wide queue serializes every append across thread files."""

    def __init__(self) -> None:
        self._queue: deque[_AppendRequest] = deque()
        self._drain_lock = asyncio.Lock()
        self._states: dict[tuple[Path, str], _WriterState] = {}
        self._last_load_ms = 0.0

    @staticmethod
    def validate_thread_id(thread_id: str) -> str:
        if not THREAD_ID_PATTERN.fullmatch(thread_id):
            raise InvalidThreadId(thread_id)
        return thread_id

    @classmethod
    def journal_root(cls, project_path: Path) -> Path:
        return project_path.resolve() / ".stream_journal"

    @classmethod
    def thread_directory(cls, project_path: Path, thread_id: str) -> Path:
        return cls.journal_root(project_path) / cls.validate_thread_id(thread_id)

    @classmethod
    def events_path(cls, project_path: Path, thread_id: str) -> Path:
        return cls.thread_directory(project_path, thread_id) / "events.jsonl"

    def _read_file(self, project_path: Path, thread_id: str) -> JournalRead:
        path = self.events_path(project_path, thread_id)
        if any(
            candidate.is_symlink()
            for candidate in (
                self.journal_root(project_path),
                self.thread_directory(project_path, thread_id),
                path,
            )
        ):
            raise UnsafeJournalPath(thread_id)
        if not path.exists():
            return JournalRead((), 0, "absent", False, False)
        try:
            content = path.read_bytes()
        except OSError:
            LOGW(f"Stream Journal could not be read: thread_id={thread_id}")
            return JournalRead((), 0, "partial", True, True)

        damaged = bool(content and not content.endswith(b"\n"))
        lines = content.splitlines()
        events: list[dict[str, Any]] = []
        seen_event_ids: set[str] = set()
        seen_dedup: set[str] = set()
        expected = 1
        high_water = 0
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            is_incomplete_last = index == len(lines) - 1 and damaged
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                damaged = True
                LOGW(
                    f"Stream Journal contains {'an incomplete tail' if is_incomplete_last else 'a damaged line'}: "
                    f"thread_id={thread_id} line={index + 1}"
                )
                continue
            if not isinstance(event, dict) or event.get("v") != JOURNAL_VERSION:
                damaged = True
                LOGW(f"Stream Journal schema mismatch: thread_id={thread_id} line={index + 1}")
                continue
            required_strings = ("event_id", "dedup_key", "type", "source", "at")
            if (
                any(not isinstance(event.get(field), str) or not event[field] for field in required_strings)
                or event.get("source") not in {"codex_stream", "codex_history"}
                or event.get("thread_id") != thread_id
                or not isinstance(event.get("data"), dict)
                or any(event.get(field) is not None and not isinstance(event[field], str) for field in ("turn_id", "item_id"))
            ):
                damaged = True
                LOGW(f"Stream Journal event envelope is invalid: thread_id={thread_id} line={index + 1}")
                continue
            sequence = event.get("seq")
            if not isinstance(sequence, int) or sequence < 1:
                damaged = True
                continue
            high_water = max(high_water, sequence)
            if sequence < expected:
                damaged = True
                LOGW(f"Stream Journal sequence did not increase: thread_id={thread_id} expected={expected} actual={sequence}")
                continue
            if sequence > expected:
                damaged = True
                LOGW(f"Stream Journal sequence gap: thread_id={thread_id} expected={expected} actual={sequence}")
            expected = sequence + 1
            event_id = str(event.get("event_id") or "")
            dedup_key = str(event.get("dedup_key") or "")
            if (event_id and event_id in seen_event_ids) or (dedup_key and dedup_key in seen_dedup):
                damaged = True
                continue
            if event_id:
                seen_event_ids.add(event_id)
            if dedup_key:
                seen_dedup.add(dedup_key)
            events.append(event)

        cursor = high_water
        domain = [event for event in events if event.get("type") != "journal.opened"]
        if damaged:
            coverage = "partial"
        elif not domain:
            coverage = "absent"
        else:
            turns: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for event in domain:
                turn_id = event.get("turn_id")
                if isinstance(turn_id, str) and turn_id:
                    turns[turn_id].append(event)
            if not turns:
                coverage = "complete" if any(event.get("type") == "history.baseline_imported" for event in domain) else "partial"
            else:
                complete = True
                for turn_events in turns.values():
                    types = {str(event.get("type")) for event in turn_events}
                    has_start = "turn.started" in types
                    has_terminal = bool(types.intersection(TERMINAL_TYPES))
                    stream_events = [event for event in turn_events if event.get("source") == "codex_stream"]
                    if stream_events:
                        stream_types = {str(event.get("type")) for event in stream_events}
                        has_start = "turn.started" in stream_types
                        has_terminal = bool(stream_types.intersection(TERMINAL_TYPES))
                    if not (has_start and has_terminal):
                        complete = False
                        break
                coverage = "complete" if complete else "partial"
        return JournalRead(tuple(events), cursor, coverage, damaged, True)

    async def read(self, project_path: Path, thread_id: str) -> JournalRead:
        started = time.perf_counter()
        result = self._read_file(project_path, thread_id)
        self._last_load_ms = (time.perf_counter() - started) * 1000
        return result

    def stats(self, project_paths: list[Path]) -> dict[str, int | float]:
        thread_count = 0
        total_bytes = 0
        event_count = 0
        delta_count = 0
        max_command_output_bytes = 0
        for project_path in project_paths:
            root = self.journal_root(project_path)
            if root.is_symlink() or not root.is_dir():
                continue
            for directory in root.iterdir():
                if directory.name == ".trash" or directory.is_symlink() or not directory.is_dir():
                    continue
                path = directory / "events.jsonl"
                if path.is_symlink() or not path.is_file():
                    continue
                thread_count += 1
                try:
                    total_bytes += path.stat().st_size
                    with path.open("rb") as handle:
                        for line in handle:
                            if not line.endswith(b"\n"):
                                continue
                            event_count += 1
                            try:
                                record = json.loads(line)
                            except (json.JSONDecodeError, UnicodeDecodeError):
                                continue
                            if record.get("type") == "agent_message.delta":
                                delta_count += 1
                            if record.get("type") == "command.completed":
                                item = (record.get("data") or {}).get("item") or {}
                                byte_count = item.get("output_byte_count")
                                if isinstance(byte_count, int):
                                    max_command_output_bytes = max(
                                        max_command_output_bytes,
                                        byte_count,
                                    )
                except OSError:
                    continue
        return {
            "thread_count": thread_count,
            "total_bytes": total_bytes,
            "event_count": event_count,
            "delta_count": delta_count,
            "max_command_output_bytes": max_command_output_bytes,
            "last_load_ms": round(self._last_load_ms, 3),
        }

    def _prepare_path(self, project_path: Path, thread_id: str) -> Path:
        root = self.journal_root(project_path)
        directory = self.thread_directory(project_path, thread_id)
        for private_directory in (root, directory):
            if private_directory.is_symlink():
                raise UnsafeJournalPath(str(private_directory))
            private_directory.mkdir(mode=0o700, exist_ok=True)
            if not private_directory.is_dir():
                raise UnsafeJournalPath(str(private_directory))
        os.chmod(root, 0o700)
        os.chmod(directory, 0o700)
        path = directory / "events.jsonl"
        if path.is_symlink():
            raise UnsafeJournalPath(str(path))
        if path.exists():
            os.chmod(path, 0o600)
            content = path.read_bytes()
            if content and not content.endswith(b"\n"):
                last_newline = content.rfind(b"\n")
                with path.open("r+b") as handle:
                    handle.truncate(last_newline + 1 if last_newline >= 0 else 0)
                LOGW(f"Stream Journal incomplete tail removed before append: thread_id={thread_id}")
        return path

    def _state(self, project_path: Path, thread_id: str) -> _WriterState:
        key = (project_path.resolve(), thread_id)
        state = self._states.get(key)
        if state is not None:
            return state
        current = self._read_file(project_path, thread_id)
        state = _WriterState(
            next_sequence=current.cursor + 1,
            dedup_events={str(event["dedup_key"]): event for event in current.events if event.get("dedup_key")},
        )
        self._states[key] = state
        return state

    def _append_now(self, request: _AppendRequest) -> JournalAppend:
        path = self._prepare_path(request.project_path, request.thread_id)
        state = self._state(request.project_path, request.thread_id)
        dedup_key = str(request.values["dedup_key"])
        if dedup_key in state.dedup_events:
            return JournalAppend(state.dedup_events[dedup_key], duplicate=True)

        record = {
            "v": JOURNAL_VERSION,
            "seq": state.next_sequence,
            "event_id": request.values.get("event_id") or f"evt-{uuid.uuid4().hex}",
            "dedup_key": dedup_key,
            "type": request.values["type"],
            "event_type": request.values.get("event_type", "codex.notification"),
            "method": request.values.get("method", request.values["type"]),
            "source": request.values.get("source", "codex_stream"),
            "thread_id": request.thread_id,
            "turn_id": request.values.get("turn_id"),
            "item_id": request.values.get("item_id"),
            "at": request.values.get("at") or _utc_now(),
            "data": request.values.get("data") or {},
        }
        encoded = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        descriptor = os.open(
            path,
            os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            with os.fdopen(descriptor, "ab", buffering=0, closefd=False) as handle:
                handle.write(encoded)
            if record["type"] in FSYNC_TYPES:
                os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.chmod(path, 0o600)
        state.next_sequence += 1
        state.dedup_events[dedup_key] = record
        return JournalAppend(record)

    @staticmethod
    def _validate_values(values: dict[str, Any]) -> None:
        for field in ("type", "dedup_key"):
            if not isinstance(values.get(field), str) or not values[field]:
                raise ValueError(f"Journal event {field} must be a non-empty string")
        if values.get("source", "codex_stream") not in {
            "codex_stream",
            "codex_history",
        }:
            raise ValueError("Journal event source is not supported")
        if not isinstance(values.get("data", {}), dict):
            raise TypeError("Journal event data must be an object")
        for field in ("turn_id", "item_id"):
            if values.get(field) is not None and not isinstance(values[field], str):
                raise TypeError(f"Journal event {field} must be a string or null")

    async def append(
        self,
        project_path: Path,
        thread_id: str,
        values: dict[str, Any],
    ) -> JournalAppend:
        self.validate_thread_id(thread_id)
        self._validate_values(values)
        future: asyncio.Future[JournalAppend] = asyncio.get_running_loop().create_future()
        self._queue.append(_AppendRequest(project_path.resolve(), thread_id, dict(values), future))
        async with self._drain_lock:
            while self._queue:
                request = self._queue.popleft()
                if request.future.cancelled():
                    continue
                try:
                    result = self._append_now(request)
                except Exception as exc:  # noqa: BLE001 - every writer failure belongs to its Future
                    request.future.set_exception(exc)
                else:
                    request.future.set_result(result)
        return await asyncio.shield(future)

    async def ensure_opened(self, project_path: Path, thread_id: str) -> JournalAppend:
        return await self.append(
            project_path,
            thread_id,
            {
                "type": "journal.opened",
                "event_type": "journal.opened",
                "method": "journal.opened",
                "source": "codex_stream",
                "dedup_key": f"{thread_id}:journal.opened",
                "data": {},
            },
        )

    async def append_history(
        self,
        project_path: Path,
        thread_id: str,
        thread: dict[str, Any],
        *,
        skip_turn_ids: set[str] | frozenset[str] | None = None,
    ) -> list[JournalAppend]:
        appended_events = [await self.ensure_opened(project_path, thread_id)]
        skipped = frozenset(skip_turn_ids or ())
        for turn_index, turn in enumerate(thread.get("turns", ())):
            if not isinstance(turn, dict):
                continue
            turn_id = str(turn.get("id") or f"history-turn-{turn_index}")
            if turn_id in skipped:
                continue
            appended_events.append(
                await self.append(
                    project_path,
                    thread_id,
                    {
                        "type": "turn.started",
                        "event_type": "codex.history",
                        "method": "history/turn/started",
                        "source": "codex_history",
                        "thread_id": thread_id,
                        "turn_id": turn_id,
                        "dedup_key": f"history:{thread_id}:{turn_id}:turn.started",
                        "data": {"status": "running", "ordinal": turn_index},
                    },
                )
            )
            for item_index, item in enumerate(turn.get("items", ())):
                if isinstance(item, dict):
                    history_event = _history_item_event(
                        thread_id,
                        turn_id,
                        item,
                        item_index,
                    )
                    if history_event is not None:
                        appended_events.append(
                            await self.append(
                                project_path,
                                thread_id,
                                history_event,
                            )
                        )
            status = str(turn.get("status") or "completed").lower()
            terminal_type = (
                "turn.interrupted"
                if status in {"interrupted", "cancelled", "canceled"}
                else "turn.error"
                if status in {"error", "failed"}
                else "turn.completed"
                if status not in {"running", "in_progress", "active"}
                else None
            )
            if terminal_type:
                appended_events.append(
                    await self.append(
                        project_path,
                        thread_id,
                        {
                            "type": terminal_type,
                            "event_type": "codex.history",
                            "method": "history/turn/completed",
                            "source": "codex_history",
                            "thread_id": thread_id,
                            "turn_id": turn_id,
                            "dedup_key": f"history:{thread_id}:{turn_id}:{terminal_type}",
                            "data": {"status": status},
                        },
                    )
                )
        watermark = history_watermark(thread)
        appended_events.append(
            await self.append(
                project_path,
                thread_id,
                {
                    "type": "history.baseline_imported",
                    "event_type": "codex.history",
                    "method": "history/baseline/imported",
                    "source": "codex_history",
                    "dedup_key": f"history:{thread_id}:baseline:{_fingerprint(watermark)}",
                    "data": watermark,
                },
            )
        )
        return appended_events

    async def attach_aliases(
        self,
        project_path: Path,
        thread_id: str,
        aliases: list[dict[str, str]],
    ) -> list[JournalAppend]:
        appended_events: list[JournalAppend] = []
        for alias in aliases:
            appended_events.append(
                await self.append(
                    project_path,
                    thread_id,
                    {
                        "type": "item.alias_attached",
                        "event_type": "journal.alias",
                        "method": "journal/item/aliasAttached",
                        "source": "codex_history",
                        "turn_id": alias["turn_id"],
                        "item_id": alias["source_id"],
                        "dedup_key": (f"alias:{alias['turn_id']}:{alias['target_event_id']}:{alias['source']}:{alias['source_id']}"),
                        "data": alias,
                    },
                )
            )
        return appended_events

    async def trash_thread(self, project_path: Path, thread_id: str) -> Path | None:
        source = self.thread_directory(project_path, thread_id)
        self._states.pop((project_path.resolve(), thread_id), None)
        if not source.exists():
            return None
        trash = self.journal_root(project_path) / ".trash"
        if source.is_symlink() or trash.is_symlink():
            raise UnsafeJournalPath(thread_id)
        trash.mkdir(mode=0o700, exist_ok=True)
        os.chmod(trash, 0o700)
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        destination = trash / f"{thread_id}-{timestamp}"
        shutil.move(str(source), str(destination))
        return destination

    def prune_retention(self, project_paths: list[Path], *, days: int = 30) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=max(1, days))
        removed = 0
        for project_path in project_paths:
            root = self.journal_root(project_path)
            if root.is_symlink() or not root.is_dir():
                continue
            for directory in root.iterdir():
                if directory.is_symlink() or not directory.is_dir():
                    continue
                candidates = list(directory.iterdir()) if directory.name == ".trash" else [directory]
                for candidate in candidates:
                    if candidate.is_symlink() or not candidate.is_dir():
                        continue
                    try:
                        timestamp_target = (
                            candidate / "events.jsonl"
                            if candidate.parent.name != ".trash" and (candidate / "events.jsonl").exists()
                            else candidate
                        )
                        modified = datetime.fromtimestamp(
                            timestamp_target.stat().st_mtime,
                            UTC,
                        )
                    except OSError:
                        continue
                    if modified >= cutoff:
                        continue
                    if candidate.parent != root / ".trash":
                        self._states.pop(
                            (project_path.resolve(), candidate.name),
                            None,
                        )
                    shutil.rmtree(candidate)
                    removed += 1
        if removed:
            LOGD(f"stream_journal_retention_pruned count={removed} days={days}")
        return removed


def _ui_type(event_type: str, event: dict[str, Any]) -> str | None:
    item_type = str((event.get("data") or {}).get("item", {}).get("type") or "tool")
    return {
        "user_message.completed": "userMessage",
        "agent_message.delta": "agentMessage",
        "agent_message.checkpoint": "agentMessage",
        "agent_message.completed": "agentMessage",
        "plan.updated": "plan",
        "command.started": "commandExecution",
        "command.completed": "commandExecution",
        "file_change.completed": "fileChange",
        "web_search.completed": "webSearch",
        "mcp_tool.completed": "mcpToolCall",
        "tool.started": item_type,
        "tool.completed": item_type,
    }.get(event_type)


def materialize_timeline(
    thread_id: str,
    journal: JournalRead,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Build the UI timeline and high-confidence cross-source alias suggestions."""
    turn_order: list[str] = []
    turn_events: dict[str, list[dict[str, Any]]] = defaultdict(list)
    aliases_by_target: dict[str, dict[str, str]] = defaultdict(dict)
    for event in journal.events:
        if event.get("type") == "item.alias_attached":
            data = event.get("data") or {}
            target = str(data.get("target_event_id") or "")
            source = str(data.get("source") or "")
            source_id = str(data.get("source_id") or "")
            if target and source and source_id:
                aliases_by_target[target][source] = source_id
        turn_id = event.get("turn_id")
        if isinstance(turn_id, str) and turn_id:
            if turn_id not in turn_events:
                turn_order.append(turn_id)
            turn_events[turn_id].append(event)

    def turn_sort_key(turn_id: str) -> tuple[int, int]:
        events = turn_events[turn_id]
        history_ordinals = [
            int((event.get("data") or {}).get("ordinal"))
            for event in events
            if event.get("type") == "turn.started"
            and event.get("source") == "codex_history"
            and isinstance((event.get("data") or {}).get("ordinal"), int)
        ]
        if history_ordinals:
            return 0, min(history_ordinals)
        return 1, min(int(event.get("seq") or 0) for event in events)

    turn_order.sort(key=turn_sort_key)

    turns: list[dict[str, Any]] = []
    alias_suggestions: list[dict[str, str]] = []
    for turn_id in turn_order:
        events = turn_events[turn_id]
        states: list[dict[str, Any]] = []
        by_source_id: dict[tuple[str, str], dict[str, Any]] = {}
        status = "running"
        for event in events:
            event_type = str(event.get("type") or "")
            if event_type in TERMINAL_TYPES:
                status = str((event.get("data") or {}).get("status") or event_type.removeprefix("turn."))
                continue
            ui_type = _ui_type(event_type, event)
            if ui_type is None:
                continue
            source = str(event.get("source") or "codex_stream")
            source_id = str(event.get("item_id") or "")
            event_id = str(event.get("event_id") or "")
            state = by_source_id.get((source, source_id)) if source_id else None
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            text = str(data.get("text") or data.get("delta") or "")

            if state is None and source in {"codex_history", "codex_stream"}:
                other_source = "codex_stream" if source == "codex_history" else "codex_history"
                unmatched = [
                    candidate
                    for candidate in states
                    if candidate["type"] == ui_type and source not in candidate["source_ids"] and other_source in candidate["source_ids"]
                ]
                exact = [candidate for candidate in unmatched if text and candidate.get("text") == text]
                payload = data.get("item") if isinstance(data.get("item"), dict) else {}
                comparable_payload = {key: value for key, value in payload.items() if key != "id"}
                exact_tool = [
                    candidate
                    for candidate in unmatched
                    if comparable_payload
                    and _fingerprint({key: value for key, value in (candidate.get("item") or {}).items() if key != "id"})
                    == _fingerprint(comparable_payload)
                ]
                if ui_type in {"userMessage", "agentMessage", "plan"}:
                    state = exact[0] if len(exact) == 1 else None
                else:
                    state = exact_tool[0] if len(exact_tool) == 1 else None
                if state is not None and source_id:
                    state["source_ids"][source] = source_id
                    by_source_id[(source, source_id)] = state
                    alias_suggestions.append(
                        {
                            "turn_id": turn_id,
                            "target_event_id": state["target_event_id"],
                            "source": source,
                            "source_id": source_id,
                        }
                    )

            if state is None:
                source_ids = {source: source_id} if source_id else {}
                source_ids.update(aliases_by_target.get(event_id, {}))
                state = {
                    "target_event_id": event_id,
                    "console_item_id": f"timeline-{_fingerprint([thread_id, turn_id, event_id])}",
                    "source_ids": source_ids,
                    "type": ui_type,
                    "first_seq": int(event.get("seq") or 0),
                    "text": "",
                    "item": None,
                    "unresolved": False,
                }
                conflicting = [candidate for candidate in states if candidate["type"] == ui_type and source not in candidate["source_ids"]]
                if conflicting:
                    state["unresolved"] = True
                    for candidate in conflicting:
                        candidate["unresolved"] = True
                states.append(state)
                for alias_source, alias_id in source_ids.items():
                    by_source_id[(alias_source, alias_id)] = state
            elif source_id:
                state["source_ids"][source] = source_id
                by_source_id[(source, source_id)] = state

            if event_type == "agent_message.delta":
                state["text"] += str(data.get("delta") or "")
            elif event_type == "agent_message.checkpoint" or ui_type in {"userMessage", "agentMessage", "plan"}:
                state["text"] = text
            elif isinstance(data.get("item"), dict):
                state["item"] = dict(data["item"])

        items: list[dict[str, Any]] = []
        for state in sorted(states, key=lambda candidate: candidate["first_seq"]):
            if state["type"] in {"userMessage", "agentMessage", "plan"}:
                item: dict[str, Any] = {
                    "id": state["console_item_id"],
                    "console_item_id": state["console_item_id"],
                    "source_ids": state["source_ids"],
                    "type": state["type"],
                }
                if state["type"] == "userMessage":
                    item["content"] = [{"type": "text", "text": state["text"]}]
                else:
                    item["text"] = state["text"]
            else:
                item = dict(state["item"] or {"type": state["type"]})
                item.update(
                    {
                        "id": state["console_item_id"],
                        "console_item_id": state["console_item_id"],
                        "source_ids": state["source_ids"],
                        "type": state["type"],
                    }
                )
            items.append(item)
            if state["unresolved"]:
                item["unresolved"] = True
        turns.append({"id": turn_id, "status": status, "items": items})
    return turns, alias_suggestions
