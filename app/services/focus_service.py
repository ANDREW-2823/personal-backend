from __future__ import annotations

from datetime import timedelta

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.timeutils import day_bounds_utc, utcnow
from app.models.focus import FocusSession
from app.schemas.core import FocusCreate, FocusUpdate
from app.services import activity_service
from app.services.settings_service import get_settings


def active_session(db: Session) -> FocusSession | None:
    return db.scalar(
        select(FocusSession)
        .where(FocusSession.state.in_(["running", "paused"]))
        .order_by(FocusSession.started_at.desc())
    )


def start_session(db: Session, data: FocusCreate) -> FocusSession:
    if active_session(db) is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "A focus session is already active. Complete or cancel it first.",
        )
    session = FocusSession(
        label=data.label,
        task_id=data.task_id,
        planned_minutes=data.planned_minutes,
        break_minutes=data.break_minutes,
        state="running",
    )
    db.add(session)
    db.flush()
    activity_service.log(
        db,
        "focus.started",
        f"Started focus session “{session.label}” ({session.planned_minutes} min)",
        entity_type="focus",
        entity_id=session.id,
    )
    return session


def get_session(db: Session, session_id: str) -> FocusSession:
    session = db.get(FocusSession, session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Focus session not found")
    return session


def update_session(db: Session, session_id: str, data: FocusUpdate) -> FocusSession:
    session = get_session(db, session_id)
    payload = data.model_dump(exclude_unset=True)
    if "elapsed_seconds" in payload and payload["elapsed_seconds"] is not None:
        session.elapsed_seconds = payload.pop("elapsed_seconds")
    if "state" in payload and payload["state"] is not None:
        new_state = payload.pop("state")
        if new_state in {"completed", "cancelled"} and session.state not in {
            "completed",
            "cancelled",
        }:
            session.ended_at = utcnow()
            if new_state == "completed":
                activity_service.log(
                    db,
                    "focus.completed",
                    f"Completed focus session “{session.label}”",
                    detail={"elapsed_seconds": session.elapsed_seconds},
                    entity_type="focus",
                    entity_id=session.id,
                )
        session.state = new_state
    elif data.elapsed_seconds is not None and data.elapsed_seconds >= session.planned_minutes * 60:
        session.state = "completed"
        session.ended_at = utcnow()
    db.flush()
    return session


def list_sessions(db: Session, limit: int = 50) -> list[FocusSession]:
    return list(
        db.scalars(select(FocusSession).order_by(FocusSession.started_at.desc()).limit(limit))
    )


def stats(db: Session) -> dict:
    tz = get_settings(db).timezone
    start, end = day_bounds_utc(tz)
    today = list(
        db.scalars(
            select(FocusSession).where(
                FocusSession.started_at >= start, FocusSession.started_at < end
            )
        )
    )
    week_start = utcnow() - timedelta(days=7)
    week_minutes = db.scalar(
        select(func.coalesce(func.sum(FocusSession.elapsed_seconds), 0)).where(
            FocusSession.started_at >= week_start,
            FocusSession.state.in_(["running", "paused", "completed"]),
        )
    )
    return {
        "today_sessions": len(today),
        "today_minutes": sum(s.elapsed_seconds for s in today) // 60,
        "week_minutes": int(week_minutes or 0) // 60,
        "active": active_session(db) is not None,
    }
