from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.security import require_token
from app.db.session import get_db
from app.schemas.core import FocusCreate, FocusOut, FocusUpdate
from app.services import focus_service

router = APIRouter(prefix="/focus", tags=["focus"], dependencies=[Depends(require_token)])


@router.get("", response_model=list[FocusOut])
def list_sessions(
    limit: int = Query(default=50, ge=1, le=200), db: Session = Depends(get_db)
) -> list[FocusOut]:
    return [FocusOut.model_validate(s) for s in focus_service.list_sessions(db, limit)]


@router.get("/active", response_model=FocusOut | None)
def active(db: Session = Depends(get_db)) -> FocusOut | None:
    session = focus_service.active_session(db)
    return FocusOut.model_validate(session) if session else None


@router.get("/stats")
def stats(db: Session = Depends(get_db)) -> dict:
    return focus_service.stats(db)


@router.post("", response_model=FocusOut, status_code=201)
def start(payload: FocusCreate, db: Session = Depends(get_db)) -> FocusOut:
    session = focus_service.start_session(db, payload)
    db.commit()
    db.refresh(session)
    return FocusOut.model_validate(session)


@router.patch("/{session_id}", response_model=FocusOut)
def update(session_id: str, payload: FocusUpdate, db: Session = Depends(get_db)) -> FocusOut:
    session = focus_service.update_session(db, session_id, payload)
    db.commit()
    db.refresh(session)
    return FocusOut.model_validate(session)
