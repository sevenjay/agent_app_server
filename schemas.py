"""Validated request bodies for the Codex console HTTP boundary."""

from __future__ import annotations

from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ProjectKey = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")]
ProjectFilePath = Annotated[str, Field(max_length=4096)]
ProjectFileName = Annotated[str, Field(min_length=1, max_length=255)]
ModelId = Annotated[str, Field(min_length=1, max_length=100)]
Prompt = Annotated[str, Field(min_length=1, max_length=200_000)]
AttachmentId = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
AttachmentIds = Annotated[list[AttachmentId], Field(max_length=5)]
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
Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


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


class SkillCreate(StrictRequest):
    name: Annotated[str, Field(min_length=1, max_length=64)]
    description: Annotated[str, Field(min_length=1, max_length=1024)]


class SkillRename(StrictRequest):
    name: Annotated[str, Field(min_length=1, max_length=64)]
    revision: Annotated[str, Field(min_length=1, max_length=64)]


class SkillFileSave(StrictRequest):
    path: Annotated[str, Field(min_length=1, max_length=4096)]
    content: Annotated[str, Field(max_length=1024 * 1024)]
    revision: Annotated[str, Field(min_length=1, max_length=64)]


class SkillImportCommit(StrictRequest):
    replace: bool = False


class ThreadCreate(StrictRequest):
    project_key: ProjectKey
    name: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    model: ModelId | None = None
    initial_prompt: Prompt | None = None
    initial_attachment_ids: AttachmentIds = Field(default_factory=list)
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
        if self.initial_attachment_ids and (not self.initial_prompt or not self.initial_prompt.strip()):
            raise ValueError("initial_attachment_ids requires a non-blank initial_prompt")
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


class PromptSettings(StrictRequest):
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


class TurnStart(PromptSettings):
    attachment_ids: AttachmentIds = Field(default_factory=list)


class ScheduleSettings(PromptSettings):
    project_key: ProjectKey
    frequency: Literal["daily", "weekly"]
    days_of_week: list[Weekday] = Field(default_factory=list, max_length=7)
    time: Annotated[str, Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")]
    timezone: Annotated[str, Field(min_length=1, max_length=255)]
    enabled: bool = Field(default=True, description="Defaults to true on creation. On replacement, omission preserves the current state.")

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("Use a valid IANA time zone such as Asia/Taipei.") from exc
        return value

    @model_validator(mode="after")
    def validate_repeat_days(self) -> ScheduleSettings:
        if self.frequency == "weekly" and not self.days_of_week:
            raise ValueError("Select at least one weekday for a weekly schedule.")
        if self.frequency == "daily" and self.days_of_week:
            raise ValueError("Leave days_of_week empty for a daily schedule.")
        if len(set(self.days_of_week)) != len(self.days_of_week):
            raise ValueError("Select each weekday only once.")
        return self


class ScheduleEnable(StrictRequest):
    enabled: bool


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
