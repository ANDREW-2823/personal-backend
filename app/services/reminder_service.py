from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.datetime_utils import normalize_dt
from app.core.timeutils import as_utc, next_occurrence, utcnow
from app.models.reminder import Reminder, ReminderExecution
from app.schemas.core import ReminderCreate, ReminderUpdate
from app.services import activity_service
from app.services.desktop_bridge import desktop_hub
from app.services.settings_service import get_settings

logger = logging.getLogger("jarvis.reminders")


def _tz(db: Session) -> str:
    return get_settings(db).timezone


def create_reminder(db: Session, data: ReminderCreate) -> Reminder:
    tz = data.timezone or _tz(db)
    remind_at = normalize_dt(data.remind_at, tz)
    assert remind_at is not None
    if data.repeat == "none" and remind_at < utcnow() - timedelta(minutes=1):
        raise HTTPException(
            422,
            "A one-time reminder must be scheduled in the future.",
        )
    reminder = Reminder(
        title=data.title.strip(),
        notes=data.notes,
        remind_at=remind_at,
        repeat=data.repeat,
        repeat_interval_minutes=data.repeat_interval_minutes,
        timezone=tz,
        status="scheduled",
        task_id=data.task_id,
    )
    db.add(reminder)
    db.flush()
    activity_service.log(
        db,
        "reminder.created",
        f"Scheduled reminder “{reminder.title}”",
        detail={"remind_at": remind_at.isoformat(), "repeat": reminder.repeat},
        entity_type="reminder",
        entity_id=reminder.id,
    )
    return reminder


def get_reminder(db: Session, reminder_id: str) -> Reminder:
    reminder = db.get(Reminder, reminder_id)
    if reminder is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Reminder not found")
    return reminder


def update_reminder(db: Session, reminder_id: str, data: ReminderUpdate) -> Reminder:
    reminder = get_reminder(db, reminder_id)
    payload = data.model_dump(exclude_unset=True)
    if payload.get("remind_at") is not None:
        payload["remind_at"] = normalize_dt(
            payload["remind_at"], payload.get("timezone") or reminder.timezone
        )
    for key, value in payload.items():
        setattr(reminder, key, value)
    db.flush()
    return reminder


def delete_reminder(db: Session, reminder_id: str) -> None:
    reminder = get_reminder(db, reminder_id)
    db.delete(reminder)
    db.flush()


def snooze_reminder(db: Session, reminder_id: str, minutes: int) -> Reminder:
    reminder = get_reminder(db, reminder_id)
    reminder.status = "snoozed"
    reminder.snooze_until = utcnow() + timedelta(minutes=minutes)
    db.flush()
    activity_service.log(
        db,
        "reminder.snoozed",
        f"Snoozed “{reminder.title}” for {minutes} minutes",
        entity_type="reminder",
        entity_id=reminder.id,
    )
    return reminder


def dismiss_reminder(db: Session, reminder_id: str) -> Reminder:
    reminder = get_reminder(db, reminder_id)
    reminder.status = "dismissed"
    reminder.snooze_until = None
    pending = db.scalars(
        select(ReminderExecution).where(
            ReminderExecution.reminder_id == reminder.id,
            ReminderExecution.outcome == "pending",
        )
    ).all()
    for execution in pending:
        execution.outcome = "dismissed_in_app"
        execution.detail = "Dismissed in the JARVIS desktop app"
    db.flush()
    activity_service.log(
        db,
        "reminder.dismissed",
        f"Dismissed “{reminder.title}”",
        entity_type="reminder",
        entity_id=reminder.id,
    )
    return reminder


def acknowledge(db: Session, reminder_id: str, outcome: str = "delivered_in_app") -> None:
    executions = db.scalars(
        select(ReminderExecution).where(
            ReminderExecution.reminder_id == reminder_id,
            ReminderExecution.outcome == "pending",
        )
    ).all()
    for execution in executions:
        execution.outcome = outcome
        execution.detail = "Shown in the JARVIS desktop app"


def history(db: Session, reminder_id: str, limit: int = 50) -> list[ReminderExecution]:
    return list(
        db.scalars(
            select(ReminderExecution)
            .where(ReminderExecution.reminder_id == reminder_id)
            .order_by(ReminderExecution.fired_at.desc())
            .limit(limit)
        )
    )


def list_reminders(
    db: Session,
    *,
    include_inactive: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[Reminder], int]:
    stmt = select(Reminder)
    if not include_inactive:
        stmt = stmt.where(Reminder.status.in_(["scheduled", "snoozed"]))
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    stmt = stmt.order_by(Reminder.remind_at.asc()).limit(limit).offset(offset)
    return list(db.scalars(stmt)), total


def upcoming(db: Session, *, within_hours: int = 24, limit: int = 10) -> list[Reminder]:
    now = utcnow()
    horizon = now + timedelta(hours=within_hours)
    return list(
        db.scalars(
            select(Reminder)
            .where(
                Reminder.status.in_(["scheduled", "snoozed"]),
                func.coalesce(Reminder.snooze_until, Reminder.remind_at) >= now,
                func.coalesce(Reminder.snooze_until, Reminder.remind_at) <= horizon,
            )
            .order_by(func.coalesce(Reminder.snooze_until, Reminder.remind_at).asc())
            .limit(limit)
        )
    )


def _due_reminders(db: Session, now: datetime) -> list[Reminder]:
    return list(
        db.scalars(
            select(Reminder)
            .where(
                Reminder.status.in_(["scheduled", "snoozed"]),
                func.coalesce(Reminder.snooze_until, Reminder.remind_at) <= now,
            )
            .order_by(Reminder.remind_at.asc())
            .limit(50)
        )
    )


def process_due(
    db: Session, *, deliver: bool = True, now: datetime | None = None
) -> list[dict[str, Any]]:
    """Fire every due reminder exactly once per occurrence.

    Returns a structured log of what actually happened; nothing is reported as
    delivered unless the desktop agent confirms it.
    """
    current = now or utcnow()
    settings = get_settings(db)
    results: list[dict[str, Any]] = []

    for reminder in _due_reminders(db, current):
        scheduled_for = as_utc(reminder.snooze_until or reminder.remind_at) or current
        execution = ReminderExecution(
            reminder_id=reminder.id,
            scheduled_for=scheduled_for,
            outcome="pending",
            is_late=(current - scheduled_for) > timedelta(minutes=5),
        )
        try:
            with db.begin_nested():
                db.add(execution)
                db.flush()
        except IntegrityError:
            # This occurrence was already handled (duplicate tick / restart).
            db.expunge(execution)
            continue

        reminder.last_fired_at = current
        reminder.fire_count += 1
        reminder.snooze_until = None
        if reminder.repeat == "none":
            reminder.status = "fired"
        else:
            upcoming_at = next_occurrence(
                reminder.remind_at,
                reminder.repeat,
                interval_minutes=reminder.repeat_interval_minutes,
                timezone_name=reminder.timezone,
                after=current,
            )
            reminder.remind_at = upcoming_at or reminder.remind_at
            reminder.status = "scheduled"

        activity_service.log(
            db,
            "reminder.fired",
            f"Reminder fired: “{reminder.title}”",
            detail={"scheduled_for": scheduled_for.isoformat(), "late": execution.is_late},
            entity_type="reminder",
            entity_id=reminder.id,
        )
        db.commit()

        delivered = False
        detail = "Waiting for desktop acknowledgement"
        if deliver and settings.notifications_enabled:
            response = desktop_hub.request_sync(
                "notify",
                payload={
                    "title": f"JARVIS — {reminder.title}",
                    "body": reminder.notes or "Reminder is due.",
                },
            )
            if response.get("ok"):
                delivered = True
                detail = "Windows notification displayed"
            else:
                detail = str(response.get("detail") or "Desktop agent unavailable")
        else:
            detail = "Windows notifications disabled; awaiting in-app acknowledgement"

        execution.outcome = "delivered" if delivered else "pending"
        execution.detail = detail
        db.commit()
        results.append(
            {
                "reminder_id": reminder.id,
                "title": reminder.title,
                "scheduled_for": scheduled_for.isoformat(),
                "delivered": delivered,
                "detail": detail,
                "late": execution.is_late,
            }
        )

    return results


def pending_inbox(db: Session) -> list[dict[str, Any]]:
    rows = db.execute(
        select(ReminderExecution, Reminder)
        .join(Reminder, Reminder.id == ReminderExecution.reminder_id)
        .where(ReminderExecution.outcome == "pending")
        .order_by(ReminderExecution.fired_at.desc())
        .limit(20)
    ).all()
    return [
        {
            "execution_id": execution.id,
            "reminder_id": reminder.id,
            "title": reminder.title,
            "notes": reminder.notes,
            "fired_at": execution.fired_at.astimezone(UTC).isoformat(),
            "late": execution.is_late,
        }
        for execution, reminder in rows
    ]
