from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.security import require_token
from app.db.session import get_db
from app.schemas.core import (
    MessageResponse,
    ReminderCreate,
    ReminderDetail,
    ReminderExecutionOut,
    ReminderOut,
    ReminderUpdate,
    SnoozeRequest,
)
from app.services import reminder_service

router = APIRouter(prefix="/reminders", tags=["reminders"], dependencies=[Depends(require_token)])


@router.get("")
def list_reminders(
    include_inactive: bool = False,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> dict:
    items, total = reminder_service.list_reminders(
        db, include_inactive=include_inactive, limit=limit, offset=offset
    )
    return {
        "items": [ReminderOut.model_validate(r) for r in items],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/inbox")
def reminder_inbox(db: Session = Depends(get_db)) -> list[dict]:
    return reminder_service.pending_inbox(db)


@router.post("", response_model=ReminderOut, status_code=201)
def create_reminder(payload: ReminderCreate, db: Session = Depends(get_db)) -> ReminderOut:
    reminder = reminder_service.create_reminder(db, payload)
    db.commit()
    db.refresh(reminder)
    return ReminderOut.model_validate(reminder)


@router.get("/{reminder_id}", response_model=ReminderDetail)
def get_reminder(reminder_id: str, db: Session = Depends(get_db)) -> ReminderDetail:
    reminder = reminder_service.get_reminder(db, reminder_id)
    history = reminder_service.history(db, reminder_id)
    base = ReminderOut.model_validate(reminder).model_dump()
    return ReminderDetail(
        **base,
        history=[ReminderExecutionOut.model_validate(h) for h in history],
    )


@router.patch("/{reminder_id}", response_model=ReminderOut)
def update_reminder(
    reminder_id: str, payload: ReminderUpdate, db: Session = Depends(get_db)
) -> ReminderOut:
    reminder = reminder_service.update_reminder(db, reminder_id, payload)
    db.commit()
    db.refresh(reminder)
    return ReminderOut.model_validate(reminder)


@router.delete("/{reminder_id}", response_model=MessageResponse)
def delete_reminder(reminder_id: str, db: Session = Depends(get_db)) -> MessageResponse:
    reminder_service.delete_reminder(db, reminder_id)
    db.commit()
    return MessageResponse(message="Reminder deleted")


@router.post("/{reminder_id}/snooze", response_model=ReminderOut)
def snooze_reminder(
    reminder_id: str, payload: SnoozeRequest, db: Session = Depends(get_db)
) -> ReminderOut:
    reminder = reminder_service.snooze_reminder(db, reminder_id, payload.minutes)
    db.commit()
    db.refresh(reminder)
    return ReminderOut.model_validate(reminder)


@router.post("/{reminder_id}/dismiss", response_model=ReminderOut)
def dismiss_reminder(reminder_id: str, db: Session = Depends(get_db)) -> ReminderOut:
    reminder = reminder_service.dismiss_reminder(db, reminder_id)
    db.commit()
    db.refresh(reminder)
    return ReminderOut.model_validate(reminder)


@router.post("/{reminder_id}/acknowledge", response_model=MessageResponse)
def acknowledge_reminder(reminder_id: str, db: Session = Depends(get_db)) -> MessageResponse:
    reminder_service.get_reminder(db, reminder_id)
    reminder_service.acknowledge(db, reminder_id)
    db.commit()
    return MessageResponse(message="Acknowledgement recorded")
