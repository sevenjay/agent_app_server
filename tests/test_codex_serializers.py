from dataclasses import dataclass
from enum import Enum

from openai_codex.models import ItemCompletedNotification
from openai_codex.models import Notification as CodexNotification
from pydantic import BaseModel

from codex_serializers import notification_view, thread_view, to_primitive


class State(str, Enum):
    ready = "ready"


class Payload(BaseModel):
    item_id: str
    state: State


@dataclass
class Notification:
    method: str
    payload: Payload


def test_serializer_handles_dataclass_pydantic_enum_and_unknown() -> None:
    method, data = notification_view(
        Notification("item/example", Payload(item_id="item-1", state=State.ready))
    )
    assert method == "item/example"
    assert data == {"item_id": "item-1", "state": "ready"}
    assert to_primitive(object())["type"] == "object"


def test_unknown_notification_method_is_preserved() -> None:
    method, data = notification_view(
        Notification("future/unknown/event", Payload(item_id="x", state=State.ready))
    )
    assert method == "future/unknown/event"
    assert data["item_id"] == "x"


def test_completed_tool_notification_keeps_final_result_shape() -> None:
    payload = ItemCompletedNotification.model_validate(
        {
            "threadId": "thr_1",
            "turnId": "turn_1",
            "completedAtMs": 123,
            "item": {
                "id": "tool_1",
                "type": "mcpToolCall",
                "server": "files",
                "tool": "read",
                "arguments": {"path": "README.md"},
                "status": "completed",
                "result": {"content": [{"type": "text", "text": "contents"}]},
            },
        }
    )

    method, data = notification_view(CodexNotification("item/completed", payload))

    assert method == "item/completed"
    assert data["turn_id"] == "turn_1"
    assert data["item"]["type"] == "mcpToolCall"
    assert data["item"]["result"]["content"][0]["text"] == "contents"


def test_thread_view_removes_private_paths() -> None:
    view = thread_view(
        {"id": "thr_1", "cwd": "/secret/project", "path": "/secret/store"},
        project_key="demo",
    )
    assert "cwd" not in view
    assert "path" not in view
    assert view["project_key"] == "demo"
    assert view["turns"] == []
