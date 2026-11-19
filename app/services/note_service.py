from __future__ import annotations

import re

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.note import Note
from app.schemas.core import NoteCreate, NoteUpdate
from app.services import activity_service


def _postgres(db: Session) -> bool:
    return db.get_bind().dialect.name == "postgresql"


def create_note(db: Session, data: NoteCreate) -> Note:
    note = Note(**data.model_dump())
    db.add(note)
    db.flush()
    activity_service.log(
        db, "note.created", f"Created note “{note.title}”", entity_type="note", entity_id=note.id
    )
    return note


def get_note(db: Session, note_id: str) -> Note:
    note = db.get(Note, note_id)
    if note is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Note not found")
    return note


def update_note(db: Session, note_id: str, data: NoteUpdate) -> Note:
    note = get_note(db, note_id)
    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(note, key, value)
    db.flush()
    return note


def delete_note(db: Session, note_id: str) -> None:
    note = get_note(db, note_id)
    db.delete(note)
    db.flush()


def search_notes(
    db: Session,
    *,
    query: str | None = None,
    category: str | None = None,
    tag: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Note], int]:
    stmt = select(Note)
    if category:
        stmt = stmt.where(Note.category == category)
    if query:
        pattern = f"%{query.strip()}%"
        if _postgres(db):
            stmt = stmt.where(
                or_(
                    Note.title.ilike(pattern),
                    Note.content.ilike(pattern),
                    func.to_tsvector("english", Note.title + " " + Note.content).op("@@")(
                        func.plainto_tsquery("english", query)
                    ),
                )
            )
        else:
            stmt = stmt.where(or_(Note.title.ilike(pattern), Note.content.ilike(pattern)))
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    stmt = stmt.order_by(Note.pinned.desc(), Note.updated_at.desc()).limit(limit).offset(offset)
    notes = list(db.scalars(stmt))
    if tag:
        notes = [n for n in notes if any(t.lower() == tag.lower() for t in (n.tags or []))]
        total = len(notes)
    return notes, total


def categories(db: Session) -> list[str]:
    rows = db.execute(
        select(Note.category).where(Note.category.is_not(None)).group_by(Note.category)
    ).all()
    return sorted(row[0] for row in rows if row[0])


def summarize(db: Session, note: Note) -> Note:
    """Local extractive summary — no external processing, no fabricated facts."""
    text = re.sub(r"\s+", " ", note.content or "").strip()
    if not text:
        note.ai_summary = "This note is empty."
        db.flush()
        return note
    sentences = re.split(r"(?<=[.!?])\s+", text)
    summary = " ".join(sentences[:3])[:800]
    note.ai_summary = summary
    db.flush()
    activity_service.log(
        db,
        "note.summarized",
        f"Summarized note “{note.title}” locally",
        entity_type="note",
        entity_id=note.id,
    )
    return note
