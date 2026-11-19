from __future__ import annotations

from datetime import date as Date
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.core import EventOut, NoteOut, TaskOut


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class DrawingPoint(BaseModel):
    x: float = Field(ge=-0.25, le=1.25)
    y: float = Field(ge=-0.25, le=1.25)
    p: float | None = Field(default=None, ge=0, le=1)


class DrawingStroke(BaseModel):
    tool: Literal["pen", "pencil", "highlighter", "eraser"] = "pen"
    color: str = Field(default="#1f2937", max_length=30)
    size: float = Field(default=2.5, ge=0.5, le=40)
    points: list[DrawingPoint] = Field(default_factory=list, max_length=5000)


class DrawingData(BaseModel):
    strokes: list[DrawingStroke] = Field(default_factory=list, max_length=500)
    width: int = Field(default=1000, ge=200, le=4000)
    height: int = Field(default=600, ge=150, le=4000)


class JournalBlock(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    type: Literal[
        "heading", "paragraph", "checklist", "quote", "divider", "image", "callout"
    ] = "paragraph"
    html: str = Field(default="", max_length=30_000)
    text: str = Field(default="", max_length=30_000)
    checked: bool = False
    level: int = Field(default=2, ge=1, le=3)
    src: str | None = Field(default=None, max_length=4_000_000)
    caption: str | None = Field(default=None, max_length=500)


class JournalUpsert(BaseModel):
    title: str = Field(default="", max_length=300)
    mood: str | None = Field(default=None, max_length=30)
    summary: str | None = Field(default=None, max_length=20_000)
    highlight: str | None = Field(default=None, max_length=500)
    blocks: list[JournalBlock] = Field(default_factory=list, max_length=200)
    drawing: DrawingData | None = None
    plain_text: str | None = Field(default=None, max_length=60_000)
    expected_updated_at: datetime | None = None


class JournalAttachmentOut(ORMModel):
    id: str
    entry_id: str
    filename: str
    mime_type: str
    size_bytes: int
    created_at: datetime


class JournalAttachmentCreate(BaseModel):
    filename: str = Field(min_length=1, max_length=300)
    mime_type: str = Field(default="application/octet-stream", max_length=120)
    data: str = Field(min_length=1, max_length=12_000_000, description="Base64 data URL or payload")

    @field_validator("data")
    @classmethod
    def validate_base64(cls, value: str) -> str:
        payload = value.split(",", 1)[-1].strip()
        if not payload:
            raise ValueError("Attachment data is empty")
        return value


class JournalAttachmentDownload(JournalAttachmentOut):
    data: str


class JournalOut(BaseModel):
    id: str
    entry_date: Date
    title: str
    mood: str | None
    summary: str | None
    highlight: str | None
    blocks: list[dict[str, Any]]
    plain_text: str
    word_count: int
    drawing: DrawingData | None
    attachments: list[JournalAttachmentOut] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class JournalPreviewOut(BaseModel):
    date: Date
    title: str
    excerpt: str
    mood: str | None
    word_count: int
    has_drawing: bool
    drawing_preview: list[DrawingStroke] = Field(default_factory=list)
    event_count: int = 0
    task_count: int = 0
    task_done: int = 0
    updated_at: datetime


class CalendarCategoryOut(ORMModel):
    id: str
    name: str
    color: str
    icon: str | None
    position: int


class CalendarCategoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    color: str = Field(default="#5FBF8B", max_length=20)
    icon: str | None = Field(default=None, max_length=40)


class DayStatsOut(BaseModel):
    tasks_due: int = 0
    tasks_completed: int = 0
    tasks_overdue: int = 0
    events: int = 0
    journal_words: int = 0
    task_completion: float = 0


class DayBundleOut(BaseModel):
    date: Date
    journal: JournalOut | None
    events: list[EventOut]
    tasks: list[TaskOut]
    notes: list[NoteOut] = Field(default_factory=list)
    categories: list[CalendarCategoryOut] = Field(default_factory=list)
    stats: DayStatsOut


class CalendarOverviewOut(BaseModel):
    start: Date
    end: Date
    events: list[EventOut]
    tasks: list[TaskOut]
    journal: list[JournalPreviewOut]
    categories: list[CalendarCategoryOut]
    stats: DayStatsOut


class AnalyticsDayOut(BaseModel):
    date: Date
    tasks_completed: int = 0
    tasks_due: int = 0
    events: int = 0
    journal_words: int = 0
    focus_minutes: int = 0


class AnalyticsTotalsOut(BaseModel):
    tasks_total: int = 0
    tasks_completed: int = 0
    tasks_pending: int = 0
    tasks_overdue: int = 0
    events: int = 0
    journal_days: int = 0
    focus_minutes: int = 0
    completion_rate: float = 0
    productivity_score: float = 0


class AnalyticsOut(BaseModel):
    start: Date
    end: Date
    totals: AnalyticsTotalsOut
    per_day: list[AnalyticsDayOut]
    categories: list[dict[str, Any]] = Field(default_factory=list)
    upcoming_events: list[EventOut] = Field(default_factory=list)


class JournalSearchItem(BaseModel):
    date: Date
    title: str
    excerpt: str
    match: str
    updated_at: datetime


class CalendarSearchOut(BaseModel):
    entries: list[JournalSearchItem] = Field(default_factory=list)
    events: list[EventOut] = Field(default_factory=list)
    tasks: list[TaskOut] = Field(default_factory=list)
