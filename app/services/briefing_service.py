from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.core.timeutils import local_now, utcnow
from app.models.settings import AppSetting
from app.schemas.insights import BriefingOut
from app.services import activity_service
from app.services.desktop_bridge import desktop_hub
from app.services.settings_service import get_settings

logger = logging.getLogger("jarvis.routines")


def _state(db: Session) -> dict:
    row = db.get(AppSetting, "routine_state")
    return dict(row.value) if row and isinstance(row.value, dict) else {}


def _save_state(db: Session, state: dict) -> None:
    row = db.get(AppSetting, "routine_state")
    if row is None:
        row = AppSetting(key="routine_state", value=state)
        db.add(row)
    else:
        row.value = state
        row.updated_at = utcnow()
    db.flush()


def build_briefing(db: Session, kind: str = "morning") -> BriefingOut:
    from app.services.dashboard_service import build_dashboard

    dashboard = build_dashboard(db)
    settings = get_settings(db)
    sections: list[dict] = []
    sections.append(
        {
            "title": "Today at a glance",
            "items": [
                f"{dashboard.counts.tasks_today} tasks due today",
                f"{dashboard.counts.tasks_overdue} overdue",
                f"{dashboard.counts.tasks_completed_today} completed",
                f"{dashboard.counts.events_today} calendar events",
            ],
        }
    )
    if dashboard.priority_tasks:
        sections.append(
            {
                "title": "Priorities",
                "items": [f"{t.title} — {t.priority}" for t in dashboard.priority_tasks[:5]],
            }
        )
    if dashboard.overdue_tasks:
        sections.append(
            {
                "title": "Needs rescheduling",
                "items": [t.title for t in dashboard.overdue_tasks[:5]],
            }
        )
    if dashboard.upcoming_reminders:
        sections.append(
            {
                "title": "Reminders in the next 24h",
                "items": [r.title for r in dashboard.upcoming_reminders[:5]],
            }
        )
    habits_pending = [h for h in dashboard.habits if h.completed_today < h.target_per_period]
    if habits_pending:
        sections.append({"title": "Habits left", "items": [h.name for h in habits_pending]})

    if kind == "evening":
        headline = (
            f"Evening review: {dashboard.counts.tasks_completed_today} tasks completed today, "
            f"{dashboard.counts.tasks_overdue} still overdue."
        )
    else:
        headline = (
            f"{settings.assistant_name} briefing: {dashboard.counts.tasks_today} tasks due, "
            f"{dashboard.counts.tasks_overdue} overdue, {len(dashboard.habits)} habits tracked."
        )

    return BriefingOut(
        kind=kind,
        generated_at=utcnow(),
        headline=headline,
        sections=sections,
    )


def deliver_briefing(db: Session, kind: str = "morning") -> BriefingOut:
    briefing = build_briefing(db, kind)
    activity_service.log(
        db,
        f"briefing.{kind}",
        briefing.headline,
        detail={"sections": briefing.sections},
    )
    response = desktop_hub.request_sync(
        "notify",
        payload={"title": f"JARVIS — {kind.title()} briefing", "body": briefing.headline},
    )
    activity_service.log(
        db,
        "briefing.delivery",
        "Briefing notification displayed"
        if response.get("ok")
        else "Briefing generated; desktop notification unavailable",
        detail={"delivered": bool(response.get("ok")), "detail": response.get("detail")},
    )
    db.commit()
    return briefing


def tick(db: Session, now: datetime | None = None) -> None:
    settings = get_settings(db)
    moment = now or local_now(settings.timezone)
    state = _state(db)
    today = moment.date().isoformat()
    current_hhmm = moment.strftime("%H:%M")

    if settings.morning_briefing_time and current_hhmm == settings.morning_briefing_time:
        if state.get("morning_date") != today:
            deliver_briefing(db, "morning")
            state["morning_date"] = today
            _save_state(db, state)
            db.commit()

    if settings.evening_review_time and current_hhmm == settings.evening_review_time:
        if state.get("evening_date") != today:
            deliver_briefing(db, "evening")
            state["evening_date"] = today
            _save_state(db, state)
            db.commit()
