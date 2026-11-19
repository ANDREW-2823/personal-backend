from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, selectinload

from app.core.config import settings as env_settings
from app.core.timeutils import day_bounds_utc, get_zone, local_now, utcnow
from app.models.automation import WorkflowExecution
from app.models.calendar import CalendarEvent
from app.models.focus import FocusSession
from app.models.habit import Habit
from app.models.reminder import Reminder, ReminderExecution
from app.models.task import Task
from app.schemas.insights import (
    AssistantStatus,
    CategoryCount,
    DashboardCounts,
    DashboardOut,
    DayPoint,
    InsightsOut,
    WeatherNow,
)
from app.services import focus_service, habit_service, reminder_service, task_service
from app.services.settings_service import get_settings


def greeting_for(tz_name: str, assistant_name: str) -> str:
    hour = local_now(tz_name).hour
    if hour < 5:
        part = "Working late"
    elif hour < 12:
        part = "Good morning"
    elif hour < 18:
        part = "Good afternoon"
    else:
        part = "Good evening"
    return f"{part}. I'm {assistant_name}."


def build_dashboard(db: Session) -> DashboardOut:
    app_settings = get_settings(db)
    tz = app_settings.timezone
    start, end = day_bounds_utc(tz)
    now = utcnow()

    counts = task_service.today_summary(db)

    priority_tasks = list(
        db.scalars(
            select(Task)
            .options(selectinload(Task.subtasks), selectinload(Task.tag_objects))
            .where(
                Task.status.notin_(["done", "cancelled"]),
                Task.due_at.is_not(None),
                Task.due_at < end,
            )
            .order_by(
                case(
                    (Task.priority == "critical", 0),
                    (Task.priority == "high", 1),
                    (Task.priority == "medium", 2),
                    else_=3,
                ),
                Task.due_at.asc(),
            )
            .limit(5)
        )
    )
    overdue_tasks = list(
        db.scalars(
            select(Task)
            .options(selectinload(Task.subtasks), selectinload(Task.tag_objects))
            .where(
                Task.status.notin_(["done", "cancelled"]),
                Task.due_at.is_not(None),
                Task.due_at < start,
            )
            .order_by(Task.due_at.asc())
            .limit(5)
        )
    )
    upcoming_reminders = reminder_service.upcoming(db, within_hours=24, limit=5)
    events_today = list(
        db.scalars(
            select(CalendarEvent)
            .where(CalendarEvent.start_at < end, CalendarEvent.end_at >= start)
            .order_by(CalendarEvent.start_at.asc())
        )
    )

    habit_views = habit_service.habit_views(db)
    habits_due = len(habit_views)
    habits_done = sum(
        1 for h in habit_views if h["completed_today"] >= max(1, h["target_per_period"])
    )

    focus = focus_service.active_session(db)
    recent_executions = list(
        db.scalars(select(WorkflowExecution).order_by(WorkflowExecution.started_at.desc()).limit(5))
    )

    planned = counts["tasks_completed_today"] + counts["tasks_today"] + counts["tasks_overdue"]
    progress = round((counts["tasks_completed_today"] / planned) * 100, 1) if planned else 0.0

    morning_reminder_count = int(
        db.scalar(
            select(func.count())
            .select_from(Reminder)
            .where(
                Reminder.status.in_(["scheduled", "snoozed"]),
                func.coalesce(Reminder.snooze_until, Reminder.remind_at) >= now,
                func.coalesce(Reminder.snooze_until, Reminder.remind_at)
                <= now + timedelta(hours=24),
            )
        )
        or 0
    )

    return DashboardOut(
        generated_at=now,
        greeting=greeting_for(tz, app_settings.assistant_name),
        timezone=tz,
        counts=DashboardCounts(
            tasks_open=counts["tasks_open"],
            tasks_today=counts["tasks_today"],
            tasks_overdue=counts["tasks_overdue"],
            tasks_completed_today=counts["tasks_completed_today"],
            reminders_upcoming=morning_reminder_count,
            events_today=len(events_today),
            habits_due=habits_due,
            habits_done=habits_done,
        ),
        priority_tasks=priority_tasks,
        overdue_tasks=overdue_tasks,
        upcoming_reminders=upcoming_reminders,
        events_today=events_today,
        habits=habit_views,
        focus_session=focus,
        recent_executions=recent_executions,
        daily_progress=progress,
        assistant=AssistantStatus(
            provider=app_settings.ai_provider,
            mode="live" if _ai_is_live(app_settings.ai_provider) else "mock",
            voice_enabled=app_settings.voice_enabled,
        ),
        weather=_weather(app_settings),
    )


def _ai_is_live(provider: str) -> bool:
    if provider == "ollama":
        return True
    if provider in {"openai", "anthropic"}:
        return bool(env_settings.ai_api_key)
    return False


def _weather(app_settings) -> WeatherNow | None:
    # No weather provider is bundled. Never fabricate weather data.
    if not app_settings.weather_enabled:
        return None
    return None


def build_insights(db: Session, range_days: int = 7) -> InsightsOut:
    app_settings = get_settings(db)
    zone = get_zone(app_settings.timezone)
    now_local = datetime.now(zone)
    end = now_local.astimezone(UTC)
    start = (
        (now_local - timedelta(days=range_days - 1))
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .astimezone(UTC)
    )

    completed = int(
        db.scalar(
            select(func.count())
            .select_from(Task)
            .where(Task.completed_at >= start, Task.completed_at <= end)
        )
        or 0
    )
    created = int(
        db.scalar(
            select(func.count())
            .select_from(Task)
            .where(Task.created_at >= start, Task.created_at <= end)
        )
        or 0
    )

    sessions = list(
        db.scalars(
            select(FocusSession).where(
                FocusSession.started_at >= start,
                FocusSession.state.in_(["running", "paused", "completed"]),
            )
        )
    )
    focus_minutes = sum(s.elapsed_seconds for s in sessions) // 60

    reminders_fired = int(
        db.scalar(
            select(func.count())
            .select_from(ReminderExecution)
            .where(ReminderExecution.fired_at >= start)
        )
        or 0
    )
    executions = list(
        db.scalars(select(WorkflowExecution).where(WorkflowExecution.started_at >= start))
    )
    workflows_run = len(executions)
    successes = sum(1 for e in executions if e.status in {"succeeded", "skipped"})
    success_rate = round((successes / workflows_run) * 100, 1) if workflows_run else 0.0

    habit_rows = list(db.scalars(select(Habit).where(Habit.archived.is_(False))))
    completion_map = habit_service.completion_data(db, [h.id for h in habit_rows], days=range_days)
    total_target = 0
    for habit in habit_rows:
        periods = range_days if habit.cadence == "daily" else max(1, range_days // 7)
        total_target += max(1, habit.target_per_period) * periods
    completed_habit_units = sum(sum(counts.values()) for counts in completion_map.values())
    habit_rate = (
        round(min(100.0, (completed_habit_units / total_target) * 100), 1) if total_target else 0.0
    )

    per_day: list[DayPoint] = []
    for offset in range(range_days):
        day_local = (now_local - timedelta(days=range_days - 1 - offset)).date()
        day_start = datetime.combine(day_local, datetime.min.time(), tzinfo=zone).astimezone(UTC)
        day_end = (
            datetime.combine(day_local, datetime.min.time(), tzinfo=zone) + timedelta(days=1)
        ).astimezone(UTC)
        day_completed = int(
            db.scalar(
                select(func.count())
                .select_from(Task)
                .where(Task.completed_at >= day_start, Task.completed_at < day_end)
            )
            or 0
        )
        day_focus = (
            int(
                db.scalar(
                    select(func.coalesce(func.sum(FocusSession.elapsed_seconds), 0)).where(
                        FocusSession.started_at >= day_start, FocusSession.started_at < day_end
                    )
                )
                or 0
            )
            // 60
        )
        per_day.append(
            DayPoint(date=day_local.isoformat(), completed=day_completed, focus_minutes=day_focus)
        )

    category_rows = db.execute(
        select(Task.category, func.count())
        .where(Task.completed_at >= start, Task.category.is_not(None))
        .group_by(Task.category)
        .order_by(func.count().desc())
        .limit(6)
    ).all()

    return InsightsOut(
        range_days=range_days,
        tasks_completed=completed,
        tasks_created=created,
        focus_minutes=focus_minutes,
        focus_sessions=len(sessions),
        habit_completion_rate=habit_rate,
        reminders_fired=reminders_fired,
        workflows_run=workflows_run,
        workflow_success_rate=success_rate,
        per_day=per_day,
        top_categories=[CategoryCount(category=cat, count=count) for cat, count in category_rows],
    )
