"""Convert unstable SDK objects into stable, JSON-safe console view models."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel


def to_primitive(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return to_primitive(value.value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, BaseModel):
        return to_primitive(value.model_dump(mode="json", by_alias=False))
    if is_dataclass(value):
        return to_primitive(asdict(value))
    if isinstance(value, dict):
        return {str(key): to_primitive(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_primitive(item) for item in value]
    if hasattr(value, "__dict__"):
        return {
            key: to_primitive(item)
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    return {"type": type(value).__name__, "summary": str(value)[:500]}


def field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def thread_view(thread: Any, *, project_key: str) -> dict[str, Any]:
    view = to_primitive(thread)
    if not isinstance(view, dict):
        view = {"id": str(field(thread, "id", ""))}
    view.pop("cwd", None)
    view.pop("path", None)
    view["project_key"] = project_key
    view.setdefault("turns", [])
    return view


def notification_view(notification: Any) -> tuple[str, dict[str, Any]]:
    method = str(field(notification, "method", "unknown"))
    payload = field(notification, "payload", {})
    data = to_primitive(payload)
    if not isinstance(data, dict):
        data = {"value": data}
    return method, data
