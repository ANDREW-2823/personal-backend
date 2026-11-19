from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

TriggerType = Literal["manual", "schedule_once", "recurring", "task_completed", "reminder_event"]
ActionType = Literal[
    "create_task",
    "create_reminder",
    "notify",
    "start_focus",
    "open_app",
    "open_folder",
    "run_script",
    "daily_summary",
]
StepKind = Literal["action", "condition"]


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class StepIn(BaseModel):
    kind: StepKind = "action"
    action_type: ActionType
    config: dict = Field(default_factory=dict)
    condition: dict | None = None
    position: int | None = Field(default=None, ge=0, le=100)


class StepOut(ORMModel):
    id: str
    workflow_id: str
    position: int
    kind: str
    action_type: str
    config: dict
    condition: dict | None


class WorkflowCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=2000)
    trigger_type: TriggerType = "manual"
    trigger_config: dict = Field(default_factory=dict)
    enabled: bool = True
    dry_run: bool = False
    retry_limit: int = Field(default=1, ge=0, le=5)
    steps: list[StepIn] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def validate_trigger(self) -> WorkflowCreate:
        if self.trigger_type in {"schedule_once", "recurring"} and "at" not in self.trigger_config:
            if self.trigger_type == "schedule_once":
                raise ValueError("schedule_once workflows require trigger_config.at")
            if self.trigger_config.get("frequency") not in {"daily", "weekly", "interval"}:
                raise ValueError(
                    "recurring workflows require a frequency of daily, weekly or interval"
                )
        if self.trigger_type == "recurring" and self.trigger_config.get("frequency") == "interval":
            minutes = self.trigger_config.get("interval_minutes")
            if not isinstance(minutes, int) or minutes < 5:
                raise ValueError("Interval workflows require interval_minutes >= 5")
        return self


class WorkflowUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=2000)
    trigger_type: TriggerType | None = None
    trigger_config: dict | None = None
    enabled: bool | None = None
    dry_run: bool | None = None
    retry_limit: int | None = Field(default=None, ge=0, le=5)
    steps: list[StepIn] | None = Field(default=None, min_length=1, max_length=30)


class WorkflowOut(ORMModel):
    id: str
    name: str
    description: str | None
    trigger_type: str
    trigger_config: dict
    enabled: bool
    dry_run: bool
    retry_limit: int
    last_run_at: datetime | None
    next_run_at: datetime | None
    steps: list[StepOut]
    created_at: datetime
    updated_at: datetime


class ExecutionOut(ORMModel):
    id: str
    workflow_id: str
    triggered_by: str
    status: str
    attempt: int
    started_at: datetime
    finished_at: datetime | None
    result: dict | None
    error: str | None


class RunRequest(BaseModel):
    dry_run: bool | None = None


class ConditionSpec(BaseModel):
    """Documented shape of a condition object for the workflow editor."""

    field: str
    op: Literal["equals", "not_equals", "contains", "gt", "lt", "exists"]
    value: str | int | float | bool | None = None

    @field_validator("field")
    @classmethod
    def allowed_field(cls, value: str) -> str:
        if value not in {"priority", "category", "title", "event", "source"}:
            raise ValueError("Unsupported condition field")
        return value
