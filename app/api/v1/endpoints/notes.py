from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.security import require_token
from app.db.session import get_db
from app.schemas.core import MessageResponse, NoteCreate, NoteOut, NoteUpdate
from app.services import note_service

router = APIRouter(prefix="/notes", tags=["notes"], dependencies=[Depends(require_token)])


@router.get("")
def list_notes(
    query: str | None = None,
    category: str | None = None,
    tag: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> dict:
    notes, total = note_service.search_notes(
        db, query=query, category=category, tag=tag, limit=limit, offset=offset
    )
    return {
        "items": [NoteOut.model_validate(n) for n in notes],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/categories")
def note_categories(db: Session = Depends(get_db)) -> list[str]:
    return note_service.categories(db)


@router.post("", response_model=NoteOut, status_code=201)
def create_note(payload: NoteCreate, db: Session = Depends(get_db)) -> NoteOut:
    note = note_service.create_note(db, payload)
    db.commit()
    db.refresh(note)
    return NoteOut.model_validate(note)


@router.get("/{note_id}", response_model=NoteOut)
def get_note(note_id: str, db: Session = Depends(get_db)) -> NoteOut:
    return NoteOut.model_validate(note_service.get_note(db, note_id))


@router.patch("/{note_id}", response_model=NoteOut)
def update_note(note_id: str, payload: NoteUpdate, db: Session = Depends(get_db)) -> NoteOut:
    note = note_service.update_note(db, note_id, payload)
    db.commit()
    db.refresh(note)
    return NoteOut.model_validate(note)


@router.delete("/{note_id}", response_model=MessageResponse)
def delete_note(note_id: str, db: Session = Depends(get_db)) -> MessageResponse:
    note_service.delete_note(db, note_id)
    db.commit()
    return MessageResponse(message="Note deleted")


@router.post("/{note_id}/summarize", response_model=NoteOut)
def summarize_note(note_id: str, db: Session = Depends(get_db)) -> NoteOut:
    note = note_service.get_note(db, note_id)
    note = note_service.summarize(db, note)
    db.commit()
    db.refresh(note)
    return NoteOut.model_validate(note)
