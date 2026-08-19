"""Validated request bodies for the Codex console HTTP boundary."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ProjectKey = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")]
ProjectFilePath = Annotated[str, Field(max_length=4096)]
ProjectFileName = Annotated[str, Field(min_length=1, max_length=255)]
ModelId = Annotated[str, Field(min_length=1, max_length=100)]
Prompt = Annotated[str, Field(min_length=1, max_length=200_000)]
ReasoningEffort = Literal[
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
]
GoalObjective = Annotated[str, Field(min_length=1, max_length=200_000)]
GoalTokenBudget = Annotated[int, Field(ge=1, le=2_000_000_000)]


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectCreate(StrictRequest):
    name: Annotated[str, Field(min_length=1, max_length=255)]

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if (
            not value
            or value in {".", ".."}
            or "/" in value
            or "\\" in value
            or "\x00" in value
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
            or len(value.encode("utf-8")) > 255
        ):
            raise ValueError("name must be one visible directory name")
        return value


class ProjectDirectoryCreate(StrictRequest):
    path: ProjectFilePath = ""
    name: ProjectFileName


class ProjectFileRename(StrictRequest):
    path: Annotated[str, Field(min_length=1, max_length=4096)]
    name: ProjectFileName


class ThreadCreate(StrictRequest):
    project_key: ProjectKey
    name: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    model: ModelId | None = None
    initial_prompt: Prompt | None = None
    initial_goal: GoalObjective | None = None
    reasoning_effort: ReasoningEffort | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("name must not be blank")
        return value

    @model_validator(mode="after")
    def validate_creation_mode(self) -> ThreadCreate:
        creation_modes = (
            self.name is not None,
            self.initial_prompt is not None,
            self.initial_goal is not None,
        )
        if sum(creation_modes) > 1:
            raise ValueError(
                "name, initial_prompt, and initial_goal are mutually exclusive"
            )
        if (
            self.reasoning_effort is not None
            and self.initial_prompt is None
            and self.initial_goal is None
        ):
            raise ValueError("reasoning_effort requires initial_prompt or initial_goal")
        return self


class ThreadUpdate(StrictRequest):
    name: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    pinned: bool | None = None
    custom_label: Annotated[str, Field(max_length=200)] | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("name must not be blank")
        return value

    @field_validator("custom_label")
    @classmethod
    def normalize_label(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class TurnStart(StrictRequest):
    prompt: Prompt
    model: ModelId | None = None
    reasoning_effort: ReasoningEffort | None = None

    @field_validator("prompt")
    @classmethod
    def validate_prompt(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("prompt must not be blank")
        return value


class TurnSteer(StrictRequest):
    prompt: Prompt

    @field_validator("prompt")
    @classmethod
    def validate_prompt(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("prompt must not be blank")
        return value


class GoalStart(StrictRequest):
    objective: GoalObjective
    token_budget: GoalTokenBudget | None = None
    model: ModelId | None = None
    reasoning_effort: ReasoningEffort | None = None

    @field_validator("objective")
    @classmethod
    def validate_objective(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("objective must not be blank")
        return value


class GoalUpdate(StrictRequest):
    status: Literal["active", "paused"]
    model: ModelId | None = None
    reasoning_effort: ReasoningEffort | None = None


class PreferencesUpdate(StrictRequest):
    selected_project_key: ProjectKey | None = None
    selected_thread_id: Annotated[str, Field(min_length=1, max_length=128)] | None = None
