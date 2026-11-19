"""Offline assistant engine.

This is the no-credentials mode of JARVIS. It is intentionally deterministic:
it reads real database records, extracts simple intents, and emits *validated
tool calls* that run through the same registry as live AI providers. It never
fabricates task or reminder records, and it never claims a desktop action
succeeded unless the desktop agent confirms it.

It is not a language model. Responses are concise and honest about that.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from dateutil import parser as date_parser  # noqa: F401  (reserved for richer parsing)
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.core.timeutils import as_utc, day_bounds_utc, get_zone, utcnow
from app.models.reminder import Reminder
from app.models.settings import AppShortcut
from app.models.task import Task
from app.schemas.core import AppSettingsSchema
from app.services import calendar_service
from app.services.dashboard_service import build_dashboard, build_insights

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTH_NAMES = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}


@dataclass
class MockTurn:
    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


def _call(name: str, **arguments: Any) -> dict[str, Any]:
    return {"id": f"call_{uuid.uuid4().hex[:12]}", "name": name, "arguments": arguments}


def _local(value: datetime | None, tz_name: str) -> str:
    if value is None:
        return "no due date"
    zone = get_zone(tz_name)
    aware = as_utc(value) or value
    return aware.astimezone(zone).strftime("%a %d %b, %H:%M")


def _clean_title(text: str) -> str:
    cleaned = re.sub(
        r"\b(?:at|by|on)\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?\b",
        "",
        text,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\b(?:today|tomorrow|tonight|this evening|this morning|next week)\b",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\bin\s+\d+\s*(?:minutes?|mins?|hours?|hrs?|days?)\b", "", cleaned, flags=re.IGNORECASE
    )
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" .,-:")
    return cleaned[:300]


def parse_schedule(text: str, tz_name: str) -> tuple[datetime | None, str]:
    """Extract a schedule from natural language, returning UTC + cleaned title."""
    zone = get_zone(tz_name)
    now = datetime.now(zone)
    lower = text.lower()

    delta_match = re.search(r"\bin\s+(\d+)\s*(minutes?|mins?|hours?|hrs?|days?)\b", lower)
    if delta_match:
        amount = int(delta_match.group(1))
        unit = delta_match.group(2)
        if unit.startswith("min"):
            target = now + timedelta(minutes=amount)
        elif unit.startswith(("hour", "hr")):
            target = now + timedelta(hours=amount)
        else:
            target = now + timedelta(days=amount)
        return target.astimezone(UTC), _clean_title(text)

    time_match = re.search(r"\b(?:at|by)\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", lower)
    explicit_time: time | None = None
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2) or 0)
        meridiem = time_match.group(3)
        if meridiem == "pm" and hour < 12:
            hour += 12
        if meridiem == "am" and hour == 12:
            hour = 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            explicit_time = time(hour, minute)

    target_date = now.date()
    if "tomorrow" in lower:
        target_date = (now + timedelta(days=1)).date()
    elif "today" in lower or "tonight" in lower or "this evening" in lower:
        target_date = now.date()
    else:
        for index, name in enumerate(WEEKDAYS):
            if re.search(rf"\b{name}\b", lower):
                days_ahead = (index - now.weekday()) % 7 or 7
                target_date = (now + timedelta(days=days_ahead)).date()
                break

    if explicit_time is None:
        if target_date == now.date():
            return None, _clean_title(text)
        explicit_time = time(9, 0)

    candidate = datetime.combine(target_date, explicit_time, tzinfo=zone)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC), _clean_title(text)


def _open_tasks(db: Session, limit: int = 5) -> list[Task]:
    return list(
        db.scalars(
            select(Task)
            .where(Task.status.in_(["todo", "in_progress", "blocked"]))
            .order_by(
                Task.due_at.is_(None),
                Task.due_at.asc(),
                case(
                    (Task.priority == "critical", 0),
                    (Task.priority == "high", 1),
                    (Task.priority == "medium", 2),
                    else_=3,
                ),
            )
            .limit(limit)
        )
    )


def _plan_day(db: Session, settings: AppSettingsSchema) -> str:
    dashboard = build_dashboard(db)
    tz = settings.timezone
    lines: list[str] = []
    lines.append("Here is your plan for today, sir.")
    if dashboard.priority_tasks:
        lines.append("")
        lines.append("Priorities")
        for index, task in enumerate(dashboard.priority_tasks[:5], start=1):
            due = _local(task.due_at, tz)
            lines.append(f"{index}. {task.title} — {task.priority} priority, due {due}")
    else:
        lines.append(
            "No dated tasks are pending. Add the one outcome that would make today worthwhile."
        )
    if dashboard.overdue_tasks:
        lines.append("")
        lines.append("Overdue — reschedule or finish intentionally")
        for task in dashboard.overdue_tasks[:3]:
            lines.append(f"• {task.title} (was due {_local(task.due_at, tz)})")
    if dashboard.upcoming_reminders:
        lines.append("")
        lines.append("Reminders in the next 24 hours")
        for reminder in dashboard.upcoming_reminders[:4]:
            lines.append(
                f"• {reminder.title} at {_local(reminder.snooze_until or reminder.remind_at, tz)}"
            )
    if dashboard.events_today:
        lines.append("")
        lines.append("Calendar")
        for event in dashboard.events_today[:4]:
            lines.append(f"• {event.title} — {_local(event.start_at, tz)}")
    habit_total = len(dashboard.habits)
    if habit_total:
        lines.append("")
        done = sum(1 for h in dashboard.habits if h.completed_today >= max(1, h.target_per_period))
        lines.append(f"Habits: {done}/{habit_total} complete today.")
    if dashboard.focus_session:
        lines.append(
            f"Focus: “{dashboard.focus_session.label}” is {dashboard.focus_session.state} "
            f"({dashboard.focus_session.elapsed_seconds // 60}/"
            f"{dashboard.focus_session.planned_minutes} min)."
        )
    else:
        lines.append("Focus: no session running. A 25-minute session is a reasonable start.")
    lines.append("")
    lines.append(
        f"Daily progress: {dashboard.daily_progress:.0f}% — "
        f"{dashboard.counts.tasks_completed_today} completed, {dashboard.counts.tasks_today} due, "
        f"{dashboard.counts.tasks_overdue} overdue."
    )
    return "\n".join(lines)


def _summarize_week(db: Session, settings: AppSettingsSchema) -> str:
    insights = build_insights(db, range_days=7)
    since = utcnow() - timedelta(days=7)
    completed = list(
        db.scalars(
            select(Task)
            .where(Task.completed_at >= since)
            .order_by(Task.completed_at.desc())
            .limit(12)
        )
    )
    lines = [
        (
            f"This week: {insights.tasks_completed} tasks completed and "
            f"{insights.tasks_created} created."
        ),
        f"Focus time: {insights.focus_minutes} minutes across {insights.focus_sessions} sessions.",
        (
            f"Habit completion: {insights.habit_completion_rate:.0f}%. "
            f"Workflows: {insights.workflows_run} run, "
            f"{insights.workflow_success_rate:.0f}% successful."
        ),
    ]
    if completed:
        lines.append("")
        lines.append("Completed recently")
        for task in completed[:8]:
            lines.append(f"• {task.title} — {_local(task.completed_at, settings.timezone)}")
    else:
        lines.append("No tasks were marked complete in the last 7 days.")
    return "\n".join(lines)


def _why_behind(db: Session, settings: AppSettingsSchema) -> str:
    insights = build_insights(db, range_days=14)
    open_count = int(
        db.scalar(
            select(func.count())
            .select_from(Task)
            .where(Task.status.in_(["todo", "in_progress", "blocked"]))
        )
        or 0
    )
    overdue_count = int(
        db.scalar(
            select(func.count())
            .select_from(Task)
            .where(
                Task.due_at < utcnow(),
                Task.status.in_(["todo", "in_progress", "blocked"]),
            )
        )
        or 0
    )
    trend = "a growing" if insights.tasks_created > insights.tasks_completed else "a shrinking"
    lines = [
        "Here is what the data actually shows over the last 14 days:",
        (
            f"• {insights.tasks_completed} tasks completed, "
            f"{insights.tasks_created} created — {trend} backlog."
        ),
        f"• {insights.focus_minutes} focused minutes across {insights.focus_sessions} sessions.",
        f"• {overdue_count} tasks are overdue out of {open_count} open.",
    ]
    if insights.focus_minutes < 120:
        lines.append("The clearest constraint is focus time, not task volume.")
    elif overdue_count > 5:
        lines.append(
            "The clearest constraint is overcommitment: more is scheduled than is realistic."
        )
    else:
        lines.append(
            "No single metric explains it; the issue may be prioritisation rather than capacity."
        )
    lines.append("Which constraint should we fix first — reduce scope, or protect a focus block?")
    return "\n".join(lines)


def _list_open_tasks(db: Session, settings: AppSettingsSchema, limit: int = 5) -> str:
    tasks = _open_tasks(db, limit)
    if not tasks:
        return "There are no open tasks. The backlog is clear."
    lines = [f"Your top {len(tasks)} open {'task' if len(tasks) == 1 else 'tasks'}:"]
    for index, task in enumerate(tasks, start=1):
        if task.due_at is None:
            due_text = "no due date"
        else:
            due_text = f"due {_local(task.due_at, settings.timezone)}"
        lines.append(f"{index}. {task.title} — {task.priority} priority, {due_text}")
    return "\n".join(lines)


def _list_reminders(db: Session, settings: AppSettingsSchema) -> str:
    reminders = list(
        db.scalars(
            select(Reminder)
            .where(Reminder.status.in_(["scheduled", "snoozed"]))
            .order_by(Reminder.remind_at.asc())
            .limit(6)
        )
    )
    if not reminders:
        return "No reminders are scheduled."
    lines = ["Upcoming reminders:"]
    for reminder in reminders:
        repeat = f" ({reminder.repeat})" if reminder.repeat != "none" else ""
        when = _local(reminder.snooze_until or reminder.remind_at, settings.timezone)
        lines.append(f"• {reminder.title} — {when}{repeat}")
    return "\n".join(lines)


def _match_shortcut(db: Session, text: str) -> AppShortcut | None:
    shortcuts = list(db.scalars(select(AppShortcut).where(AppShortcut.approved.is_(True))))
    lower = text.lower()
    for shortcut in shortcuts:
        if shortcut.name.lower() in lower:
            return shortcut
    if "vs code" in lower or "vscode" in lower:
        return next((s for s in shortcuts if s.kind == "project"), None)
    return None


def _strip_wake(text: str) -> str:
    return re.sub(
        r"^\s*(?:(?:hey|hi|hello|ok|okay|yo)[\s,]+)?(?:jarvis|j\.a\.r\.v\.i\.s)[,!.]?\s*",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()


def _spotify_turn(text: str, lower: str) -> MockTurn | None:
    phrases = (
        "spotify",
        "what's playing",
        "whats playing",
        "what is playing",
        "pause music",
        "resume music",
        "next song",
        "previous song",
        "skip song",
    )
    if not any(phrase in lower for phrase in phrases):
        return None
    if re.search(r"\b(?:volume|louder|quieter|mute)\b", lower) and "spotify" in lower:
        percent = re.search(r"\b(\d{1,3})\s*%?", lower)
        if percent:
            return MockTurn(
                tool_calls=[
                    _call("control_spotify", action="volume", volume=int(percent.group(1)))
                ]
            )
        return MockTurn(
            content="What Spotify volume level would you like, from 0 to 100?"
        )
    if "device" in lower:
        return MockTurn(tool_calls=[_call("control_spotify", action="devices")])
    status_words = ["what's playing", "whats playing", "what is playing", "status"]
    if any(word in lower for word in status_words):
        return MockTurn(tool_calls=[_call("control_spotify", action="status")])
    if any(word in lower for word in ["pause", "stop music", "stop the music"]):
        return MockTurn(tool_calls=[_call("control_spotify", action="pause")])
    if any(word in lower for word in ["resume", "continue", "unpause"]):
        return MockTurn(tool_calls=[_call("control_spotify", action="resume")])
    if any(word in lower for word in ["next", "skip"]):
        return MockTurn(tool_calls=[_call("control_spotify", action="next")])
    if any(word in lower for word in ["previous", "go back", "last song"]):
        return MockTurn(tool_calls=[_call("control_spotify", action="previous")])
    search = re.search(r"(?:search|find)\s+(?:spotify\s+)?(?:for\s+)?(.+)", text, re.IGNORECASE)
    if search and "search" in lower:
        query = re.sub(r"\s+on\s+spotify\s*$", "", search.group(1), flags=re.IGNORECASE)
        return MockTurn(
            tool_calls=[_call("control_spotify", action="search", query=query.strip(" ."))]
        )
    if re.fullmatch(
        r"\s*(?:open|launch|start)\s+(?:the\s+)?spotify(?:\s+app)?\s*[.!]?\s*",
        lower,
    ):
        return MockTurn(tool_calls=[_call("launch_application", application="spotify")])
    play = re.search(
        r"\b(?:play|put on)\s+(?:some\s+)?(?:music|a song|song|track)?\s*(?:by|from|for)?\s*(.*)",
        text,
        re.IGNORECASE,
    )
    if play:
        query = re.sub(r"\s+(?:on|in)\s+spotify\s*$", "", play.group(1), flags=re.IGNORECASE)
        query = re.sub(r"\bspotify\b", "", query, flags=re.IGNORECASE).strip(" .,!?-")
        query = re.sub(r"^(?:the|a|an)\s+", "", query, flags=re.IGNORECASE)
        if query:
            return MockTurn(tool_calls=[_call("control_spotify", action="play", query=query)])
        return MockTurn(tool_calls=[_call("control_spotify", action="resume")])
    if "spotify" in lower:
        return MockTurn(tool_calls=[_call("control_spotify", action="status")])
    return None


def _mentioned_date(text: str, tz_name: str) -> date | None:
    """Resolve common natural-language dates without inventing missing detail."""
    lower = text.lower()
    zone = get_zone(tz_name)
    today = datetime.now(zone).date()
    if re.search(r"\byesterday\b", lower):
        return today - timedelta(days=1)
    if re.search(r"\btoday\b|\btonight\b", lower):
        return today
    if re.search(r"\btomorrow\b", lower):
        return today + timedelta(days=1)
    iso = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", lower)
    if iso:
        try:
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        except ValueError:
            return None
    month_day = re.search(
        r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(" + "|".join(MONTH_NAMES) + r")\b(?:\s+(\d{4}))?",
        lower,
    )
    if month_day:
        try:
            return date(
                int(month_day.group(3) or today.year),
                MONTH_NAMES[month_day.group(2)],
                int(month_day.group(1)),
            )
        except ValueError:
            return None
    day_month = re.search(
        r"\b(" + "|".join(MONTH_NAMES) + r")\s+(\d{1,2})(?:st|nd|rd|th)?\b(?:\s*,?\s*(\d{4}))?",
        lower,
    )
    if day_month:
        try:
            return date(
                int(day_month.group(3) or today.year),
                MONTH_NAMES[day_month.group(1)],
                int(day_month.group(2)),
            )
        except ValueError:
            return None
    for index, name in enumerate(WEEKDAYS):
        if not re.search(rf"\b(?:last\s+|previous\s+|on\s+|this\s+)?{name}\b", lower):
            continue
        if "last" in lower or "previous" in lower:
            days_back = (today.weekday() - index) % 7
            return today - timedelta(days=days_back or 7)
        days_ahead = (index - today.weekday()) % 7
        return today + timedelta(days=days_ahead)
    return None


def _mentioned_month(text: str, tz_name: str) -> tuple[int, int] | None:
    lower = text.lower()
    zone = get_zone(tz_name)
    today = datetime.now(zone).date()
    if "last month" in lower:
        first = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
        return first.year, first.month
    if "this month" in lower:
        return today.year, today.month
    for name, month in MONTH_NAMES.items():
        if re.search(rf"\b{name}\b", lower):
            year_match = re.search(rf"\b{name}\s+(\d{{4}})\b", lower)
            return (int(year_match.group(1)) if year_match else today.year), month
    return None


def _calendar_day_answer(db: Session, settings: AppSettingsSchema, day: date) -> str:
    data = calendar_service.daily_summary(db, day)
    title = day.strftime("%A, %d %B %Y")
    lines = [f"Here is the calendar record for {title}."]
    journal = data.get("journal")
    if journal and journal.get("recorded"):
        lines.append("")
        lines.append("Journal — activities you recorded")
        text = (journal.get("plain_text") or "").strip()
        for line in [part.strip() for part in text.splitlines() if part.strip()][:12]:
            lines.append(f"• {line[:220]}")
        if journal.get("highlight"):
            lines.append(f"Highlight: {journal['highlight'][:220]}")
    else:
        lines.append("")
        lines.append("Journal: no activity was recorded for this date.")
    events = data.get("events_scheduled") or []
    lines.append("")
    if events:
        lines.append("Scheduled events (planned — not proof they happened)")
        for event in events[:8]:
            when = event["local_start"][11:16] if not event.get("all_day") else "all day"
            lines.append(f"• {when} — {event['title']} [{event['status']}]")
    else:
        lines.append("Scheduled events: none.")
    completed = data.get("tasks_completed") or []
    pending = data.get("tasks_pending") or []
    lines.append("")
    if completed:
        lines.append("Tasks completed")
        for task in completed[:8]:
            lines.append(f"• {task['title']}")
    else:
        lines.append("Tasks completed: none recorded.")
    if pending:
        lines.append("")
        lines.append("Tasks still pending")
        for task in pending[:8]:
            lines.append(f"• {task['title']} ({task['status']}, {task['priority']} priority)")
    if not completed and not pending:
        lines.append("Tasks for this date: none recorded.")
    if not (journal and journal.get("recorded")) and not completed and not data.get("notes"):
        lines.append("")
        lines.append("No personal activity was recorded on this date.")
    return "\n".join(lines)


def _calendar_range_answer(
    db: Session, start: date, end: date, heading: str
) -> str:
    data = calendar_service.range_summary(db, start, end)
    totals = data["totals"]
    lines = [
        f"{heading} ({start.strftime('%d %b')} – {end.strftime('%d %b %Y')})",
        (
            f"• {totals['tasks_completed']} tasks completed, "
            f"{totals['tasks_pending']} still pending, "
            f"{totals['events']} events scheduled, "
            f"{data['recorded_journal_days']} journal day(s) recorded."
        ),
    ]
    entries = [item for item in data.get("journal_days") or [] if item]
    if entries:
        lines.append("")
        lines.append("Journal activity you recorded")
        for entry in entries[:8]:
            excerpt = " ".join((entry.get("summary") or entry.get("plain_text") or "").split())
            if excerpt:
                lines.append(f"• {entry['date']} — {excerpt[:180]}")
    completed = data.get("tasks_completed") or []
    if completed:
        lines.append("")
        lines.append("Completed tasks")
        for task in completed[:10]:
            lines.append(f"• {task['title']}")
    pending = data.get("tasks_pending") or []
    if pending:
        lines.append("")
        lines.append("Still pending from this period")
        for task in pending[:10]:
            lines.append(f"• {task['title']} ({task['status']})")
    if not entries and not completed and not pending:
        lines.append("")
        lines.append("No personal activity was recorded in this period.")
    return "\n".join(lines)


def _journal_search_answer(
    db: Session, settings: AppSettingsSchema, query: str, start: date | None, end: date | None
) -> str:
    results = calendar_service.search_journal(
        db, query, start_date=start, end_date=end, limit=10
    )
    if not results:
        return f"No journal entries matched “{query}”. Nothing was invented."
    lines = [f"Journal matches for “{query}”:"]
    for item in results:
        lines.append(f"• {item.date.strftime('%d %b %Y')} — {item.excerpt[:220]}")
    return "\n".join(lines)


def _pending_answer(db: Session, settings: AppSettingsSchema, overdue_only: bool = False) -> str:
    tasks = (
        calendar_service.overdue_tasks(db, limit=20)
        if overdue_only
        else calendar_service.pending_tasks(db, limit=20)
    )
    label = "overdue" if overdue_only else "pending"
    if not tasks:
        return f"There are no {label} tasks." if not overdue_only else "No tasks are overdue."
    lines = [f"{len(tasks)} {label} task(s):"]
    for task in tasks:
        due = f", due {_local(task.due_at, settings.timezone)}" if task.due_at else ""
        lines.append(f"• {task.title} — {task.priority} priority, {task.status}{due}")
    return "\n".join(lines)


def _completed_answer(db: Session, settings: AppSettingsSchema, day: date | None) -> str:
    if day is not None:
        start, end = day_bounds_utc(settings.timezone, day)
        heading = f"Tasks completed on {day.strftime('%d %b %Y')}"
    else:
        end = utcnow()
        start = end - timedelta(days=7)
        heading = "Tasks completed in the last 7 days"
    tasks = calendar_service.completed_tasks(db, start, end)
    if not tasks:
        return f"{heading}: none recorded."
    lines = [f"{heading}:"]
    for task in tasks[:20]:
        lines.append(f"• {task.title} — {_local(task.completed_at, settings.timezone)}")
    return "\n".join(lines)


def _productivity_answer(db: Session, start: date, end: date) -> str:
    data = calendar_service.productivity_summary(db, start, end)
    totals = data["totals"]
    return "\n".join(
        [
            f"Productivity {start.strftime('%d %b')} – {end.strftime('%d %b %Y')}",
            f"• Task completion: {totals['completion_rate']}%",
            (
                f"• {totals['tasks_completed']} completed · "
                f"{totals['tasks_pending']} pending · "
                f"{totals['tasks_overdue']} overdue"
            ),
            f"• Productivity score: {totals['productivity_score']}%",
            (
                f"• {totals['events']} events scheduled · "
                f"{totals['journal_days']} journal day(s) · "
                f"{totals['focus_minutes']} focus minutes"
            ),
        ]
    )


def _upcoming_answer(db: Session, settings: AppSettingsSchema, days: int = 14) -> str:
    now = utcnow()
    events = calendar_service.range_events(db, now, now + timedelta(days=days))
    pending = calendar_service.pending_tasks(db, limit=8)
    lines = [f"Next {days} days"]
    if events:
        lines.append("")
        lines.append("Scheduled events")
        for event in events[:10]:
            lines.append(f"• {_local(event.start_at, settings.timezone)} — {event.title}")
    else:
        lines.append("No events are scheduled.")
    if pending:
        lines.append("")
        lines.append("Approaching deadlines / pending tasks")
        for task in pending[:8]:
            due = _local(task.due_at, settings.timezone) if task.due_at else "no due date"
            lines.append(f"• {task.title} — {due}")
    return "\n".join(lines)


def reply(db: Session, text: str, settings: AppSettingsSchema) -> MockTurn:
    text = _strip_wake(text)
    lower = text.lower().strip()

    if any(
        phrase in lower
        for phrase in [
            "plan my day",
            "good morning",
            "briefing",
            "what should i do",
            "what's my day",
            "whats my day",
        ]
    ):
        return MockTurn(content=_plan_day(db, settings))

    if "cancel" in lower and "reminder" in lower:
        title = re.sub(r"^.*?\bcancel\b\s*", "", text, flags=re.IGNORECASE)
        title = re.sub(r"^\s*(?:the|my|a|an)\s+", "", title, flags=re.IGNORECASE)
        title = re.sub(
            r"^\s*reminder\s+(?:for|about|called|to)?\s*",
            "",
            title,
            flags=re.IGNORECASE,
        )
        title = re.sub(r"\s+reminder\s*$", "", title, flags=re.IGNORECASE)
        title = title.strip(" .,!?")
        if not title:
            return MockTurn(content="Which reminder should I cancel?")
        return MockTurn(tool_calls=[_call("cancel_reminder", title=title)])

    if re.search(r"\b(show|list|what are)\b.*\breminders?\b", lower) and "remind me" not in lower:
        return MockTurn(content=_list_reminders(db, settings))

    spotify_turn = _spotify_turn(text, lower)
    if spotify_turn is not None:
        return spotify_turn

    if re.search(r"\b(what(?:'s| is)? the time|current time|time is it|what time)\b", lower):
        return MockTurn(tool_calls=[_call("get_current_time")])
    if re.search(r"\b(today'?s? date|what(?:'s| is)? the date|what day is it)\b", lower):
        return MockTurn(tool_calls=[_call("get_current_time")])

    if any(
        phrase in lower
        for phrase in [
            "system status",
            "how is my computer",
            "how's my computer",
            "cpu",
            "memory usage",
            "ram usage",
            "battery",
            "network status",
            "system health",
        ]
    ):
        return MockTurn(tool_calls=[_call("get_system_status")])

    if any(word in lower for word in ["volume", "mute", "unmute", "louder", "quieter"]):
        if "mute" in lower and "unmute" not in lower:
            return MockTurn(tool_calls=[_call("control_system_volume", action="mute")])
        if "unmute" in lower:
            return MockTurn(tool_calls=[_call("control_system_volume", action="unmute")])
        if "toggle" in lower:
            return MockTurn(tool_calls=[_call("control_system_volume", action="toggle")])
        if "louder" in lower or "volume up" in lower or "turn it up" in lower:
            return MockTurn(tool_calls=[_call("control_system_volume", action="up", step=10)])
        if "quieter" in lower or "volume down" in lower or "turn it down" in lower:
            return MockTurn(tool_calls=[_call("control_system_volume", action="down", step=10)])
        level = re.search(r"\b(\d{1,3})\s*%?", lower)
        if level:
            return MockTurn(
                tool_calls=[
                    _call("control_system_volume", action="set", level=int(level.group(1)))
                ]
            )
        return MockTurn(tool_calls=[_call("control_system_volume", action="status")])

    if "weather" in lower or "forecast" in lower:
        return MockTurn(tool_calls=[_call("get_weather")])

    # --------------------------------------------------------------- calendar
    if any(
        phrase in lower
        for phrase in [
            "search my journal",
            "search journal",
            "find in my journal",
            "when did i last",
        ]
    ):
        query = re.sub(
            r"^.*?(?:search(?: my)? journal(?: for)?|"
            r"find(?: in)?(?: my)? journal(?: for)?|"
            r"when did i last(?: work on)?)\s*",
            "",
            text,
            flags=re.IGNORECASE,
        ).strip(" .?!\"'")
        if not query:
            return MockTurn(content="What should I search for in your journal?")
        return MockTurn(
            content=_journal_search_answer(db, settings, query, None, None)
        )

    if re.match(
        r"^(?:please\s+)?(?:write|add|save|note)\b.*\bjournal\b", lower
    ) or lower.startswith("journal:"):
        content = re.sub(
            r"^.*?\bjournal\b\s*:?\s*", "", text, flags=re.IGNORECASE
        ).strip()
        if not content:
            return MockTurn(content="What should I write in your journal?")
        day = _mentioned_date(text, settings.timezone) or datetime.now(
            get_zone(settings.timezone)
        ).date()
        return MockTurn(
            tool_calls=[
                _call(
                    "create_journal_entry",
                    date=day.isoformat(),
                    content=content,
                )
            ]
        )

    if re.search(
        r"\b(?:add|create|schedule|book)\b.*\b(?:meeting|event|appointment)\b", lower
    ):
        schedule, _ = parse_schedule(text, settings.timezone)
        if schedule is None:
            return MockTurn(
                content=(
                    "When should I schedule it? Give a date and time, for example "
                    "“tomorrow at 10:00”."
                )
            )
        raw_title = re.sub(
            r"^(?:please\s+)?(?:add|create|schedule|book)\s+(?:a|an|the)?\s*",
            "",
            text,
            flags=re.IGNORECASE,
        )
        title = _clean_title(raw_title)
        title = re.sub(r"\s+(?:for|with)\s*$", "", title, flags=re.IGNORECASE).strip()
        if not title:
            title = "Meeting"
        return MockTurn(
            tool_calls=[
                _call(
                    "create_calendar_event",
                    title=title[:300],
                    start_at=schedule.isoformat(),
                    end_at=(schedule + timedelta(hours=1)).isoformat(),
                )
            ]
        )

    day_reference = _mentioned_date(text, settings.timezone)
    history_phrases = [
        "what happened",
        "what did i",
        "what did i write",
        "what was recorded",
        "summary of",
        "summarize",
        "journal",
        "activities",
        "scheduled",
        "plans",
        "meetings",
        "meeting",
        "appointments",
        "on ",
    ]
    if day_reference is None and re.search(r"\bwhat happened\b", lower):
        day_reference = datetime.now(get_zone(settings.timezone)).date()
    if day_reference is not None and any(
        phrase in lower for phrase in history_phrases
    ):
        return MockTurn(
            content=_calendar_day_answer(db, settings, day_reference)
        )

    month_reference = _mentioned_month(text, settings.timezone)
    if month_reference and any(
        word in lower
        for word in [
            "summar",
            "activit",
            "accomplish",
            "event",
            "learn",
            "productive",
            "month",
            "journal",
        ]
    ):
        year, month = month_reference
        start = date(year, month, 1)
        end = (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(
            days=1
        )
        return MockTurn(
            content=_calendar_range_answer(
                db,
                start,
                end,
                f"Calendar history for {start.strftime('%B %Y')}",
            )
        )

    if any(
        phrase in lower
        for phrase in [
            "last week",
            "previous week",
            "this week",
            "weekly summary",
            "summarize my week",
            "summarise my week",
            "my week",
            "what did i accomplish this week",
            "what did i complete this week",
        ]
    ):
        today = datetime.now(get_zone(settings.timezone)).date()
        if "last week" in lower or "previous week" in lower:
            start = today - timedelta(days=today.weekday() + 7)
            end = start + timedelta(days=6)
            heading = "Calendar history for last week"
        else:
            start = today - timedelta(days=today.weekday())
            end = today
            heading = "Calendar history for this week"
        return MockTurn(
            content=_calendar_range_answer(db, start, end, heading)
        )

    if "overdue" in lower:
        return MockTurn(content=_pending_answer(db, settings, overdue_only=True))

    if any(
        phrase in lower
        for phrase in [
            "pending tasks",
            "unfinished tasks",
            "still pending",
            "open tasks",
            "remaining tasks",
        ]
    ):
        return MockTurn(content=_pending_answer(db, settings))

    if any(
        phrase in lower
        for phrase in [
            "completed tasks",
            "tasks did i finish",
            "what did i finish",
            "what did i complete",
            "accomplishments",
        ]
    ):
        completed_day = _mentioned_date(text, settings.timezone)
        return MockTurn(
            content=_completed_answer(db, settings, completed_day)
        )

    if any(
        phrase in lower
        for phrase in [
            "productive",
            "productivity",
            "completion percentage",
            "completion rate",
            "how am i doing",
        ]
    ):
        today = datetime.now(get_zone(settings.timezone)).date()
        return MockTurn(
            content=_productivity_answer(db, today - timedelta(days=29), today)
        )

    if any(
        phrase in lower
        for phrase in [
            "upcoming events",
            "next events",
            "deadlines approaching",
            "approaching deadlines",
            "deadlines are approaching",
            "plans for next week",
            "next week",
            "any meetings",
            "meetings this week",
        ]
    ):
        return MockTurn(content=_upcoming_answer(db, settings))

    search = re.search(
        r"\b(?:search(?: the web)?(?: for)?|google|look up|find online)\s+(.+)",
        text,
        re.IGNORECASE,
    )
    if search:
        query = re.sub(r"\s+on\s+(?:google|the web)\s*$", "", search.group(1), flags=re.IGNORECASE)
        return MockTurn(tool_calls=[_call("search_web", query=query.strip(" ."), engine="google")])

    if re.search(r"\b(falling behind)\b", lower) or "why am i" in lower:
        return MockTurn(content=_why_behind(db, settings))

    if any(
        phrase in lower
        for phrase in [
            "summarize my week",
            "summarise my week",
            "this week",
            "weekly summary",
            "what did i complete",
            "what i completed",
        ]
    ):
        return MockTurn(content=_summarize_week(db, settings))

    if any(
        phrase in lower
        for phrase in [
            "most important",
            "top priority",
            "top priorities",
            "what should i focus",
            "unfinished tasks",
            "open tasks",
        ]
    ):
        return MockTurn(content=_list_open_tasks(db, settings))

    if re.search(r"\b(show|list|what are)\b.*\breminders?\b", lower) and "remind me" not in lower:
        return MockTurn(content=_list_reminders(db, settings))

    if "remind me" in lower:
        schedule, title = parse_schedule(text, settings.timezone)
        raw_title = re.sub(r"^.*?remind me\s+(?:to\s+)?", "", text, flags=re.IGNORECASE).strip()
        clean = _clean_title(raw_title) or title
        if schedule is None:
            return MockTurn(
                content=(
                    "When should I remind you? Give a time such as “tomorrow at 10:00” or "
                    "“in 30 minutes”."
                )
            )
        if not clean:
            return MockTurn(content="What should the reminder say?")
        return MockTurn(
            tool_calls=[_call("create_reminder", title=clean[:300], remind_at=schedule.isoformat())]
        )

    if any(
        word in lower
        for word in ["create a task", "add a task", "create task", "new task", "task to "]
    ):
        priority = "medium"
        for level in ["critical", "high", "medium", "low"]:
            if level in lower:
                priority = level
                break
        schedule, _ = parse_schedule(text, settings.timezone)
        raw_title = re.sub(
            r"^.*?(?:task)\s+(?:to\s+|called\s+|for\s+)?",
            "",
            text,
            flags=re.IGNORECASE,
        ).strip()
        title = re.sub(
            r"^(?:a\s+)?(?:critical|high|medium|low)[\s-]priority\s+",
            "",
            raw_title,
            flags=re.IGNORECASE,
        ).strip()
        clean = _clean_title(title)
        if not clean:
            return MockTurn(content="What should the task be called?")
        arguments: dict[str, Any] = {"title": clean[:300], "priority": priority}
        if schedule is not None:
            arguments["due_at"] = schedule.isoformat()
        return MockTurn(tool_calls=[_call("create_task", **arguments)])

    focus_match = re.search(r"(\d+)\s*[-\s]?(?:minute|min)\b.*\b(focus|coding|work|study)", lower)
    if not focus_match:
        focus_match = re.search(r"\b(?:focus|coding|work)\b.*?(\d+)\s*(?:minutes?|min)\b", lower)
    if (
        "start" in lower
        and ("focus" in lower or "coding session" in lower or "session" in lower)
        and focus_match
    ):
        minutes = int(focus_match.group(1))
        return MockTurn(
            tool_calls=[
                _call("start_focus", minutes=max(1, min(240, minutes)), label="Focus session")
            ]
        )

    if "routine" in lower or "habit" in lower:
        name = re.sub(r"^.*?(?:create|add|start)\s+(?:a\s+)?", "", text, flags=re.IGNORECASE)
        name = re.sub(r"\b(?:routine|habit)\b", "", name, flags=re.IGNORECASE).strip(" .,-")
        if not name:
            name = "Daily routine"
        cadence = "weekly" if "weekly" in lower else "daily"
        return MockTurn(tool_calls=[_call("create_habit", name=name[:120], cadence=cadence)])
        # Note: no reminder is invented here; the habit is real and editable.

    if lower.startswith(("open ", "launch ", "start ")) or "open vs code" in lower:
        target = re.sub(r"^(?:open|launch|start)\s+", "", text, flags=re.IGNORECASE).strip(" .")
        target = re.sub(
            r"\s+(?:app|application|for me|please)\s*$",
            "",
            target,
            flags=re.IGNORECASE,
        )
        lowered = target.lower()
        if not target:
            return MockTurn(content="Which application or website should I open?")
        if "spotify" in lowered:
            return MockTurn(tool_calls=[_call("launch_application", application="spotify")])
        if re.match(r"^(?:https?://|www\.)", lowered) or "." in lowered:
            return MockTurn(tool_calls=[_call("open_website", url=target)])
        shortcut = _match_shortcut(db, lowered)
        application = shortcut.name if shortcut is not None else target
        return MockTurn(tool_calls=[_call("launch_application", application=application)])

    if "note" in lower and any(word in lower for word in ["create", "add", "save", "remember"]):
        content = re.sub(r"^.*?(?:note|remember)\s*:?\s*", "", text, flags=re.IGNORECASE).strip()
        if not content:
            return MockTurn(content="What should the note contain?")
        return MockTurn(
            tool_calls=[
                _call(
                    "create_note",
                    title=content.split(".")[0][:80] or "Quick note",
                    content=content,
                )
            ]
        )

    if "insight" in lower or "productivity" in lower or "progress" in lower:
        return MockTurn(content=_summarize_week(db, settings))

    return MockTurn(
        content=(
            "I'm running in offline mode, so I can work with your real data using a small set of "
            "understood commands:\n"
            "• “Plan my day”\n"
            "• “Create a high-priority task to finish my project tomorrow”\n"
            "• “Remind me to call HR at 10 AM”\n"
            "• “What are my reminders?” or “Cancel the call HR reminder”\n"
            "• “Start a 45-minute coding session”\n"
            "• “What's my CPU and battery status?”\n"
            "• “Set the volume to 35” or “Mute”\n"
            "• “What time is it?”\n"
            "• “Play Midnight City on Spotify” or “What's playing?”\n"
            "• “Open Spotify”, “Open Notepad”, or “Open github.com”\n"
            "• “Search the web for Windows 11 tips”\n\n"
            "Add an AI provider in Settings and credentials to enable free-form conversation. "
            "Every action still goes through the same validated tools and approval rules."
        )
    )
