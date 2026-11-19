from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from app.schemas.automation import ExecutionOut
from app.schemas.core import (
    EventOut,
    FocusOut,
    HabitOut,
    ReminderOut,
    TaskOut,
)


class DashboardCounts(BaseModel):
    tasks_open: int
    tasks_today: int
    tasks_overdue: int
    tasks_completed_today: int
    reminders_upcoming: int
    events_today: int
    habits_due: int
    habits_done: int


class AssistantStatus(BaseModel):
    provider: str
    mode: str
    voice_enabled: bool


class WeatherNow(BaseModel):
    temperature: float
    condition: str
    location: str


class DashboardOut(BaseModel):
    generated_at: datetime
    greeting: str
    timezone: str
    counts: DashboardCounts
    priority_tasks: list[TaskOut]
    overdue_tasks: list[TaskOut]
    upcoming_reminders: list[ReminderOut]
    events_today: list[EventOut]
    habits: list[HabitOut]
    focus_session: FocusOut | None
    recent_executions: list[ExecutionOut]
    daily_progress: float
    assistant: AssistantStatus
    weather: WeatherNow | None = None


class DayPoint(BaseModel):
    date: str
    completed: int
    focus_minutes: int


class CategoryCount(BaseModel):
    category: str
    count: int


class InsightsOut(BaseModel):
    range_days: int
    tasks_completed: int
    tasks_created: int
    focus_minutes: int
    focus_sessions: int
    habit_completion_rate: float
    reminders_fired: int
    workflows_run: int
    workflow_success_rate: float
    per_day: list[DayPoint]
    top_categories: list[CategoryCount]


class HealthOut(BaseModel):
    status: str
    version: str
    database: str
    database_detail: str | None = None
    scheduler: str
    ai_mode: str
    time: datetime


class BriefingOut(BaseModel):
    kind: str
    generated_at: datetime
    headline: str
    sections: list[dict]
