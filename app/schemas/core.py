from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

TaskStatus = Literal["todo", "in_progress", "blocked", "done", "cancelled"]
TaskPriority = Literal["low", "medium", "high", "critical"]
ReminderRepeat = Literal["none", "daily", "weekly", "monthly", "weekdays", "custom"]


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------------- tasks


class SubtaskIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    completed: bool = False


class SubtaskOut(ORMModel):
    id: str
    task_id: str
    title: str
    completed: bool
    position: int


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=20_000)
    status: TaskStatus = "todo"
    priority: TaskPriority = "medium"
    due_at: datetime | None = None
    category: str | None = Field(default=None, max_length=80)
    color: str | None = Field(default=None, max_length=20)
    tags: list[str] = Field(default_factory=list, max_length=20)
    estimated_minutes: int | None = Field(default=None, ge=1, le=24 * 60)
    recurrence: str | None = Field(default=None, max_length=80)
    blocked_by: str | None = None
    subtasks: list[SubtaskIn] = Field(default_factory=list, max_length=50)

    @field_validator("tags")
    @classmethod
    def clean_tags(cls, value: list[str]) -> list[str]:
        cleaned = []
        for tag in value:
            tag = tag.strip()[:60]
            if tag and tag.lower() not in {t.lower() for t in cleaned}:
                cleaned.append(tag)
        return cleaned


class TaskUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=20_000)
    status: TaskStatus | None = None
    priority: TaskPriority | None = None
    due_at: datetime | None = None
    category: str | None = Field(default=None, max_length=80)
    color: str | None = Field(default=None, max_length=20)
    tags: list[str] | None = Field(default=None, max_length=20)
    estimated_minutes: int | None = Field(default=None, ge=1, le=24 * 60)
    recurrence: str | None = Field(default=None, max_length=80)
    blocked_by: str | None = None
    subtasks: list[SubtaskIn] | None = Field(default=None, max_length=50)
    clear_due_at: bool = False


class TaskOut(ORMModel):
    id: str
    title: str
    description: str | None
    status: str
    priority: str
    due_at: datetime | None
    category: str | None
    color: str | None
    tags: list[str]
    estimated_minutes: int | None
    recurrence: str | None
    blocked_by: str | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    subtasks: list[SubtaskOut]


class TaskPage(BaseModel):
    items: list[TaskOut]
    total: int
    limit: int
    offset: int


class RescheduleRequest(BaseModel):
    due_at: datetime


# ----------------------------------------------------------------------- reminders


class ReminderCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    notes: str | None = Field(default=None, max_length=10_000)
    remind_at: datetime
    repeat: ReminderRepeat = "none"
    repeat_interval_minutes: int | None = Field(default=None, ge=5, le=60 * 24 * 30)
    timezone: str = Field(default="UTC", max_length=64)
    task_id: str | None = None

    @model_validator(mode="after")
    def validate_repeat(self) -> ReminderCreate:
        if self.repeat == "custom" and not self.repeat_interval_minutes:
            raise ValueError("Custom reminders require repeat_interval_minutes")
        return self


class ReminderUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    notes: str | None = Field(default=None, max_length=10_000)
    remind_at: datetime | None = None
    repeat: ReminderRepeat | None = None
    repeat_interval_minutes: int | None = Field(default=None, ge=5, le=60 * 24 * 30)
    timezone: str | None = Field(default=None, max_length=64)


class ReminderOut(ORMModel):
    id: str
    title: str
    notes: str | None
    remind_at: datetime
    repeat: str
    repeat_interval_minutes: int | None
    timezone: str
    status: str
    snooze_until: datetime | None
    last_fired_at: datetime | None
    fire_count: int
    task_id: str | None
    created_at: datetime
    updated_at: datetime


class ReminderExecutionOut(ORMModel):
    id: str
    reminder_id: str
    scheduled_for: datetime
    fired_at: datetime
    outcome: str
    detail: str | None
    is_late: bool


class ReminderDetail(ReminderOut):
    history: list[ReminderExecutionOut] = Field(default_factory=list)


class SnoozeRequest(BaseModel):
    minutes: int = Field(default=10, ge=1, le=1440)


# ------------------------------------------------------------------------ calendar


EventPriority = Literal["low", "medium", "high", "critical"]
EventStatus = Literal["confirmed", "tentative", "cancelled"]
EventRecurrence = Literal["none", "daily", "weekdays", "weekly", "monthly"]


class EventCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=20_000)
    start_at: datetime
    end_at: datetime
    all_day: bool = False
    location: str | None = Field(default=None, max_length=300)
    color: str | None = Field(default=None, max_length=20)
    task_id: str | None = None
    category: str | None = Field(default=None, max_length=80)
    priority: EventPriority = "medium"
    status: EventStatus = "confirmed"
    recurrence: EventRecurrence = "none"
    reminder_minutes: int | None = Field(default=None, ge=0, le=60 * 24 * 30)
    attachment_url: str | None = Field(default=None, max_length=2048)

    @model_validator(mode="after")
    def validate_range(self) -> EventCreate:
        if self.end_at < self.start_at:
            raise ValueError("Event end must be after start")
        return self


class EventUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=20_000)
    start_at: datetime | None = None
    end_at: datetime | None = None
    all_day: bool | None = None
    location: str | None = Field(default=None, max_length=300)
    color: str | None = Field(default=None, max_length=20)
    category: str | None = Field(default=None, max_length=80)
    priority: EventPriority | None = None
    status: EventStatus | None = None
    recurrence: EventRecurrence | None = None
    reminder_minutes: int | None = Field(default=None, ge=0, le=60 * 24 * 30)
    attachment_url: str | None = Field(default=None, max_length=2048)
    clear_reminder: bool = False


class EventOut(ORMModel):
    id: str
    title: str
    description: str | None
    start_at: datetime
    end_at: datetime
    all_day: bool
    location: str | None
    kind: str
    color: str | None
    source: str
    task_id: str | None
    category: str | None
    priority: str
    status: str
    recurrence: str
    series_id: str | None
    reminder_minutes: int | None
    attachment_url: str | None
    created_at: datetime
    updated_at: datetime


class AgendaOut(BaseModel):
    events: list[EventOut]
    deadlines: list[TaskOut]
    conflicts: list[dict]


# --------------------------------------------------------------------------- habits


class HabitCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    cadence: Literal["daily", "weekly"] = "daily"
    target_per_period: int = Field(default=1, ge=1, le=50)
    reminder_time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    color: str = Field(default="#5FBF8B", max_length=20)


class HabitOut(ORMModel):
    id: str
    name: str
    description: str | None
    cadence: str
    target_per_period: int
    reminder_time: str | None
    color: str
    archived: bool
    streak: int = 0
    completed_today: int = 0
    created_at: datetime
    updated_at: datetime


class HabitCheckIn(BaseModel):
    on: date | None = None
    count: int = Field(default=1, ge=1, le=50)


class WorkoutCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: str = Field(default="strength", max_length=40)
    duration_minutes: int = Field(ge=1, le=600)
    intensity: Literal["light", "moderate", "hard"] = "moderate"
    performed_at: datetime | None = None
    notes: str | None = Field(default=None, max_length=2000)


class WorkoutOut(ORMModel):
    id: str
    name: str
    kind: str
    duration_minutes: int
    intensity: str
    performed_at: datetime
    notes: str | None


# ---------------------------------------------------------------------------- notes


class NoteCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    content: str = Field(default="", max_length=200_000)
    category: str | None = Field(default=None, max_length=80)
    tags: list[str] = Field(default_factory=list, max_length=20)
    pinned: bool = False
    task_id: str | None = None


class NoteUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    content: str | None = Field(default=None, max_length=200_000)
    category: str | None = Field(default=None, max_length=80)
    tags: list[str] | None = Field(default=None, max_length=20)
    pinned: bool | None = None
    task_id: str | None = None


class NoteOut(ORMModel):
    id: str
    title: str
    content: str
    category: str | None
    tags: list[str]
    pinned: bool
    task_id: str | None
    ai_summary: str | None
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------- focus


class FocusCreate(BaseModel):
    label: str = Field(default="Focus", max_length=160)
    task_id: str | None = None
    planned_minutes: int = Field(default=25, ge=1, le=240)
    break_minutes: int = Field(default=5, ge=0, le=60)


class FocusUpdate(BaseModel):
    elapsed_seconds: int | None = Field(default=None, ge=0, le=60 * 60 * 12)
    state: Literal["running", "paused", "completed", "cancelled"] | None = None


class FocusOut(ORMModel):
    id: str
    label: str
    task_id: str | None
    planned_minutes: int
    break_minutes: int
    elapsed_seconds: int
    state: str
    started_at: datetime
    ended_at: datetime | None


# ------------------------------------------------------------------------- settings


def _default_gradient(from_color: str, via_color: str, to_color: str, angle: int) -> GradientSchema:
    return GradientSchema(
        angle=angle, from_color=from_color, via_color=via_color, to_color=to_color
    )


class GradientSchema(BaseModel):
    angle: int = Field(default=150, ge=0, le=360)
    from_color: str = Field(default="#0B0F0C", pattern=r"^#[0-9a-fA-F]{6}$")
    via_color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    to_color: str = Field(default="#090909", pattern=r"^#[0-9a-fA-F]{6}$")


class AppearanceSchema(BaseModel):
    preset: str = Field(default="crimson", max_length=40)
    layout: GradientSchema = Field(
        default_factory=lambda: _default_gradient("#101613", "#0C0C0C", "#090909", 158)
    )
    sidebar: GradientSchema = Field(
        default_factory=lambda: _default_gradient("#0E1A14", "#0A120E", "#07100B", 175)
    )
    nav_active: GradientSchema = Field(
        default_factory=lambda: _default_gradient("#A31C1B", "#6B1413", "#3B0C0C", 130)
    )
    button: GradientSchema = Field(
        default_factory=lambda: _default_gradient("#C9302E", "#A31C1B", "#7E1615", 135)
    )
    glow: GradientSchema = Field(
        default_factory=lambda: _default_gradient("#A31C1B", "#1B4530", "#06110C", 140)
    )
    glow_strength: int = Field(default=55, ge=0, le=100)
    glass: int = Field(default=72, ge=20, le=100)


class ProfileSchema(BaseModel):
    display_name: str = Field(default="", max_length=80)
    role: str = Field(default="Personal workspace", max_length=80)
    photo: str | None = Field(default=None, max_length=2_500_000)
    photo_updated_at: str | None = Field(default=None, max_length=40)

    @field_validator("photo")
    @classmethod
    def validate_photo(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        if not value.startswith(
            ("data:image/png;base64,", "data:image/jpeg;base64,", "data:image/webp;base64,")
        ):
            raise ValueError("Profile photo must be a PNG, JPEG or WebP data URL")
        return value


class AppSettingsSchema(BaseModel):
    assistant_name: str = "JARVIS"
    personality: Literal["concise", "balanced", "detailed"] = "balanced"
    theme: Literal["dark", "light", "system"] = "dark"
    accent: str = "#A31C1B"
    timezone: str = "UTC"
    ai_provider: Literal["mock", "openai", "anthropic", "ollama"] = "mock"
    ai_model: str = "gpt-4o-mini"
    voice_enabled: bool = False
    voice_provider: Literal["browser", "windows", "mock"] = "browser"
    wake_word_enabled: bool = False
    # HUD voice controls. The backend runs STT and neural TTS; the renderer
    # falls back to the installed Windows voice when the network TTS fails.
    voice_output: Literal["neural", "browser", "windows"] = "neural"
    voice_name: str = Field(default="en-GB-RyanNeural", max_length=80)
    voice_language: str = Field(default="en-GB", max_length=16)
    voice_rate: int = Field(default=0, ge=-50, le=100)
    voice_volume: int = Field(default=100, ge=0, le=100)
    voice_mode: Literal["push_to_talk", "wake_word"] = "push_to_talk"
    voice_auto_send: bool = True
    voice_silence_ms: int = Field(default=1200, ge=300, le=5000)
    voice_max_seconds: int = Field(default=20, ge=3, le=60)
    wake_sensitivity: float = Field(default=0.6, ge=0.3, le=0.95)
    wake_cooldown_seconds: float = Field(default=2.0, ge=0.5, le=10.0)
    spotify_fallback_enabled: bool = True
    notifications_enabled: bool = True
    notification_while_minimized: bool = True
    notification_while_tray: bool = True
    startup_launch: bool = False
    start_minimized: bool = False
    automation_enabled: bool = False
    onboarding_completed: bool = False
    automation_require_approval: bool = True
    approved_apps: list[str] = Field(default_factory=list)
    approved_folders: list[str] = Field(default_factory=list)
    weather_enabled: bool = False
    weather_location: str | None = None
    data_retention_days: int = Field(default=365, ge=1, le=3650)
    morning_briefing_time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    evening_review_time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    shortcuts: dict[str, str] = Field(default_factory=dict)
    logging_level: Literal["info", "debug", "warn"] = "info"
    appearance: AppearanceSchema = Field(default_factory=AppearanceSchema)
    profile: ProfileSchema = Field(default_factory=ProfileSchema)
    onboarding_completed: bool = False


class SettingsUpdate(AppSettingsSchema):
    pass


# --------------------------------------------------------------------------- common


class ActivityOut(ORMModel):
    id: str
    kind: str
    summary: str
    detail: dict | None
    entity_type: str | None
    entity_id: str | None
    created_at: datetime


class ShortcutCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: Literal["application", "url", "folder", "script", "project"] = "application"
    target: str = Field(min_length=1, max_length=2048)
    args: list[str] = Field(default_factory=list, max_length=20)
    approved: bool = False


class ShortcutOut(ORMModel):
    id: str
    name: str
    kind: str
    target: str
    args: list[str]
    approved: bool
    created_at: datetime


class MessageResponse(BaseModel):
    message: str


class Paginated(BaseModel):
    items: list
    total: int
    limit: int
    offset: int


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
