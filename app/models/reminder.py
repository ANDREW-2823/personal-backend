from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, utcnow
from app.models.task import new_id

if TYPE_CHECKING:
    from app.models.calendar import CalendarEvent


class Reminder(Base, TimestampMixin):
    __tablename__ = "reminders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    remind_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    repeat: Mapped[str] = mapped_column(String(20), default="none", nullable=False)
    repeat_interval_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="scheduled", nullable=False, index=True)
    snooze_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fire_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    task_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    event_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("calendar_events.id", ondelete="CASCADE"), nullable=True, index=True
    )

    executions: Mapped[list[ReminderExecution]] = relationship(
        back_populates="reminder", cascade="all, delete-orphan"
    )
    event: Mapped[CalendarEvent | None] = relationship(back_populates="reminders")

    __table_args__ = (Index("ix_reminders_status_remind_at", "status", "remind_at"),)


class ReminderExecution(Base):
    """Idempotency record for reminder delivery.

    A unique (reminder_id, scheduled_for) pair guarantees that a reminder
    occurrence is never delivered twice, even across restarts or racing ticks.
    """

    __tablename__ = "reminder_executions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    reminder_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("reminders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    outcome: Mapped[str] = mapped_column(String(30), default="pending", nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_late: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    reminder: Mapped[Reminder] = relationship(back_populates="executions")

    __table_args__ = (
        UniqueConstraint("reminder_id", "scheduled_for", name="uq_reminder_execution_occurrence"),
    )
