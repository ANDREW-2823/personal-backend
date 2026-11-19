from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, JSONType, TimestampMixin, utcnow
from app.models.task import new_id

if TYPE_CHECKING:
    from app.models.reminder import Reminder


class CalendarEvent(Base, TimestampMixin):
    __tablename__ = "calendar_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    all_day: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    location: Mapped[str | None] = mapped_column(String(300), nullable=True)
    kind: Mapped[str] = mapped_column(String(20), default="event", nullable=False)
    color: Mapped[str | None] = mapped_column(String(20), nullable=True)
    source: Mapped[str] = mapped_column(String(40), default="local", nullable=False)
    task_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    category: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    priority: Mapped[str] = mapped_column(String(20), default="medium", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="confirmed", nullable=False)
    recurrence: Mapped[str] = mapped_column(String(20), default="none", nullable=False)
    series_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    reminder_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attachment_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    reminders: Mapped[list[Reminder]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_events_start_end", "start_at", "end_at"),
        Index("ix_events_category_start", "category", "start_at"),
    )


class JournalEntry(Base, TimestampMixin):
    """A single diary page, keyed by one local calendar date.

    ``blocks`` stores the structured rich-text document. Drawing strokes live
    in their own table so the editor can autosave a large canvas without
    rewriting the whole document, and attachments are stored separately.
    """

    __tablename__ = "journal_entries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    entry_date: Mapped[date] = mapped_column(Date, nullable=False, unique=True, index=True)
    title: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    mood: Mapped[str | None] = mapped_column(String(30), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    highlight: Mapped[str | None] = mapped_column(String(500), nullable=True)
    blocks: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONType, default=list, nullable=False
    )
    plain_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    word_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    drawing: Mapped[JournalDrawing | None] = relationship(
        back_populates="entry", cascade="all, delete-orphan", uselist=False
    )
    attachments: Mapped[list[JournalAttachment]] = relationship(
        back_populates="entry",
        cascade="all, delete-orphan",
        order_by="JournalAttachment.created_at",
    )

    __table_args__ = (Index("ix_journal_entries_updated_at", "updated_at"),)


class JournalDrawing(Base, TimestampMixin):
    __tablename__ = "journal_drawings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    entry_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("journal_entries.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    strokes: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONType, default=list, nullable=False
    )
    width: Mapped[int] = mapped_column(Integer, default=1000, nullable=False)
    height: Mapped[int] = mapped_column(Integer, default=600, nullable=False)

    entry: Mapped[JournalEntry] = relationship(back_populates="drawing")


class JournalAttachment(Base):
    __tablename__ = "journal_attachments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    entry_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("journal_entries.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    filename: Mapped[str] = mapped_column(String(300), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(120), default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    data: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    entry: Mapped[JournalEntry] = relationship(back_populates="attachments")


class CalendarCategory(Base, TimestampMixin):
    __tablename__ = "calendar_categories"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(80), nullable=False, unique=True, index=True)
    color: Mapped[str] = mapped_column(String(20), default="#5FBF8B", nullable=False)
    icon: Mapped[str | None] = mapped_column(String(40), nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
