from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.core.datetime_utils import normalize_dt
from app.core.security import require_token
from app.core.timeutils import day_bounds_utc, month_bounds_utc, week_bounds_utc
from app.db.session import get_db
from app.models.task import Task
from app.schemas.calendar import (
    AnalyticsOut,
    CalendarCategoryCreate,
    CalendarCategoryOut,
    CalendarOverviewOut,
    CalendarSearchOut,
    DayBundleOut,
    JournalAttachmentCreate,
    JournalAttachmentDownload,
    JournalAttachmentOut,
    JournalOut,
    JournalSearchItem,
    JournalUpsert,
)
from app.schemas.core import (
    AgendaOut,
    EventCreate,
    EventOut,
    EventUpdate,
    MessageResponse,
    SubtaskOut,
    TaskOut,
)
from app.services import calendar_service
from app.services.settings_service import get_settings

router = APIRouter(prefix="/calendar", tags=["calendar"], dependencies=[Depends(require_token)])


# --------------------------------------------------------------------- agenda


@router.get("", response_model=AgendaOut)
def agenda(
    view: str = Query(default="day", pattern="^(day|week|month|custom)$"),
    day: date | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    db: Session = Depends(get_db),
) -> AgendaOut:
    tz = get_settings(db).timezone
    if view == "day":
        start_at, end_at = day_bounds_utc(tz, day)
    elif view == "week":
        start_at, end_at = week_bounds_utc(tz, day)
    elif view == "month":
        start_at, end_at = month_bounds_utc(tz, day)
    else:
        if start is None or end is None:
            start_at, end_at = day_bounds_utc(tz, day)
        else:
            start_at = normalize_dt(start, tz)
            end_at = normalize_dt(end, tz)
            assert start_at and end_at
    data = calendar_service.agenda(db, start_at, end_at)
    return AgendaOut(
        events=[EventOut.model_validate(e) for e in data["events"]],
        deadlines=[TaskOut.model_validate(t) for t in data["deadlines"]],
        conflicts=data["conflicts"],
    )


# ------------------------------------------------------------------- overview


@router.get("/overview", response_model=CalendarOverviewOut)
def overview(
    start: date, end: date, db: Session = Depends(get_db)
) -> CalendarOverviewOut:
    data = calendar_service.overview(db, start, end)
    db.commit()
    return data


@router.get("/day/{day}", response_model=DayBundleOut)
def day_bundle(day: date, db: Session = Depends(get_db)) -> DayBundleOut:
    data = calendar_service.day_bundle(db, day)
    db.commit()
    return data


# -------------------------------------------------------------------- journal


@router.get("/journal/search", response_model=list[JournalSearchItem])
def search_journal(
    q: str = Query(default="", max_length=200),
    start: date | None = None,
    end: date | None = None,
    limit: int = Query(default=30, ge=1, le=100),
    db: Session = Depends(get_db),
) -> list[JournalSearchItem]:
    return calendar_service.search_journal(
        db, q, start_date=start, end_date=end, limit=limit
    )


@router.get("/journal/{entry_date}", response_model=JournalOut | None)
def get_journal(entry_date: date, db: Session = Depends(get_db)) -> JournalOut | None:
    entry = calendar_service.get_journal(db, entry_date)
    return calendar_service.journal_payload(entry) if entry else None


@router.put("/journal/{entry_date}", response_model=JournalOut)
def upsert_journal(
    entry_date: date, payload: JournalUpsert, db: Session = Depends(get_db)
) -> JournalOut:
    entry = calendar_service.upsert_journal(db, entry_date, payload)
    db.commit()
    db.refresh(entry)
    return calendar_service.journal_payload(entry)


@router.delete("/journal/{entry_date}", response_model=MessageResponse)
def delete_journal(entry_date: date, db: Session = Depends(get_db)) -> MessageResponse:
    calendar_service.delete_journal(db, entry_date)
    db.commit()
    return MessageResponse(message="Journal entry deleted")


@router.post(
    "/journal/{entry_date}/attachments",
    response_model=JournalAttachmentOut,
    status_code=status.HTTP_201_CREATED,
)
def upload_attachment(
    entry_date: date,
    payload: JournalAttachmentCreate,
    db: Session = Depends(get_db),
) -> JournalAttachmentOut:
    attachment = calendar_service.add_attachment(db, entry_date, payload)
    db.commit()
    db.refresh(attachment)
    return JournalAttachmentOut.model_validate(attachment)


@router.get("/attachments/{attachment_id}", response_model=JournalAttachmentDownload)
def download_attachment(
    attachment_id: str, db: Session = Depends(get_db)
) -> JournalAttachmentDownload:
    attachment = calendar_service.get_attachment(db, attachment_id)
    return JournalAttachmentDownload(
        id=attachment.id,
        entry_id=attachment.entry_id,
        filename=attachment.filename,
        mime_type=attachment.mime_type,
        size_bytes=attachment.size_bytes,
        created_at=attachment.created_at,
        data=attachment.data,
    )


@router.delete("/attachments/{attachment_id}", response_model=MessageResponse)
def delete_attachment(
    attachment_id: str, db: Session = Depends(get_db)
) -> MessageResponse:
    calendar_service.delete_attachment(db, attachment_id)
    db.commit()
    return MessageResponse(message="Attachment deleted")


# ---------------------------------------------------------------------- events


@router.get("/events", response_model=list[EventOut])
def list_events(
    start: date,
    end: date,
    db: Session = Depends(get_db),
) -> list[EventOut]:
    range_start, range_end = calendar_service._range_utc(db, start, end)
    events = calendar_service.range_events(db, range_start, range_end)
    return [EventOut.model_validate(event) for event in events]


@router.get("/upcoming-events", response_model=list[EventOut])
def upcoming_events(
    days: int = Query(default=30, ge=1, le=365),
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> list[EventOut]:
    from app.core.timeutils import utcnow

    now = utcnow()
    events = calendar_service.range_events(db, now, now + timedelta(days=days))[:limit]
    return [EventOut.model_validate(event) for event in events]


@router.post("/events", response_model=EventOut, status_code=201)
def create_event(payload: EventCreate, db: Session = Depends(get_db)) -> EventOut:
    event = calendar_service.create_event(db, payload)
    db.commit()
    db.refresh(event)
    return EventOut.model_validate(event)


@router.patch("/events/{event_id}", response_model=EventOut)
def update_event(
    event_id: str, payload: EventUpdate, db: Session = Depends(get_db)
) -> EventOut:
    event = calendar_service.update_event(db, event_id, payload)
    db.commit()
    db.refresh(event)
    return EventOut.model_validate(event)


@router.delete("/events/{event_id}", response_model=MessageResponse)
def delete_event(
    event_id: str,
    delete_series: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> MessageResponse:
    count = calendar_service.delete_event(db, event_id, delete_series=delete_series)
    db.commit()
    suffix = f" ({count} occurrences)" if count > 1 else ""
    return MessageResponse(message=f"Event deleted{suffix}")


# ----------------------------------------------------------------------- tasks


def _task_out(task: Task) -> TaskOut:
    return TaskOut(
        id=task.id,
        title=task.title,
        description=task.description,
        status=task.status,
        priority=task.priority,
        due_at=task.due_at,
        category=task.category,
        color=task.color,
        tags=task.tags,
        estimated_minutes=task.estimated_minutes,
        recurrence=task.recurrence,
        blocked_by=task.blocked_by,
        completed_at=task.completed_at,
        created_at=task.created_at,
        updated_at=task.updated_at,
        subtasks=[SubtaskOut.model_validate(item) for item in task.subtasks],
    )


@router.get("/tasks", response_model=list[TaskOut])
def calendar_tasks(
    start: date,
    end: date,
    status_filter: str | None = Query(default=None, alias="status"),
    priority: str | None = None,
    category: str | None = None,
    db: Session = Depends(get_db),
) -> list[TaskOut]:
    range_start, range_end = calendar_service._range_utc(db, start, end)
    tasks = calendar_service.tasks_in_range(db, range_start, range_end)
    statuses = {item.strip() for item in (status_filter or "").split(",") if item.strip()}
    priority_values = {item.strip() for item in (priority or "").split(",") if item.strip()}
    if statuses:
        tasks = [task for task in tasks if task.status in statuses]
    if priority_values:
        tasks = [task for task in tasks if task.priority in priority_values]
    if category:
        tasks = [task for task in tasks if (task.category or "").lower() == category.lower()]
    return [_task_out(task) for task in tasks]


@router.get("/pending-tasks", response_model=list[TaskOut])
def pending_tasks(
    limit: int = Query(default=100, ge=1, le=300), db: Session = Depends(get_db)
) -> list[TaskOut]:
    return [_task_out(task) for task in calendar_service.pending_tasks(db, limit=limit)]


@router.get("/overdue-tasks", response_model=list[TaskOut])
def overdue_tasks(
    limit: int = Query(default=100, ge=1, le=300), db: Session = Depends(get_db)
) -> list[TaskOut]:
    return [_task_out(task) for task in calendar_service.overdue_tasks(db, limit=limit)]


@router.get("/completed-tasks", response_model=list[TaskOut])
def completed_tasks(
    start: date,
    end: date,
    db: Session = Depends(get_db),
) -> list[TaskOut]:
    range_start, range_end = calendar_service._range_utc(db, start, end)
    return [
        _task_out(task)
        for task in calendar_service.completed_tasks(db, range_start, range_end)
    ]


# ------------------------------------------------------------------ analytics


@router.get("/analytics", response_model=AnalyticsOut)
def analytics(
    start: date, end: date, db: Session = Depends(get_db)
) -> AnalyticsOut:
    return calendar_service.analytics(db, start, end)


@router.get("/summary/day")
def summary_day(day: date, db: Session = Depends(get_db)) -> dict:
    return calendar_service.daily_summary(db, day)


@router.get("/summary/week")
def summary_week(start: date, end: date, db: Session = Depends(get_db)) -> dict:
    return calendar_service.range_summary(db, start, end)


@router.get("/summary/month")
def summary_month(
    month: int = Query(ge=1, le=12),
    year: int = Query(ge=1900, le=2200),
    db: Session = Depends(get_db),
) -> dict:
    return calendar_service.monthly_summary(db, year, month)


@router.get("/summary/productivity")
def summary_productivity(
    start: date, end: date, db: Session = Depends(get_db)
) -> dict:
    return calendar_service.productivity_summary(db, start, end)


# ------------------------------------------------------------------ search


@router.get("/search", response_model=CalendarSearchOut)
def search(
    q: str = Query(min_length=1, max_length=200),
    start: date | None = None,
    end: date | None = None,
    db: Session = Depends(get_db),
) -> CalendarSearchOut:
    data = calendar_service.search_calendar(db, q, start_date=start, end_date=end)
    return CalendarSearchOut(
        entries=data["entries"],
        events=[EventOut.model_validate(event) for event in data["events"]],
        tasks=[_task_out(task) for task in data["tasks"]],
    )


# ----------------------------------------------------------------- categories


@router.get("/categories", response_model=list[CalendarCategoryOut])
def list_categories(db: Session = Depends(get_db)) -> list[CalendarCategoryOut]:
    categories = calendar_service.list_categories(db)
    db.commit()
    return [CalendarCategoryOut.model_validate(item) for item in categories]


@router.post(
    "/categories", response_model=CalendarCategoryOut, status_code=status.HTTP_201_CREATED
)
def create_category(
    payload: CalendarCategoryCreate, db: Session = Depends(get_db)
) -> CalendarCategoryOut:
    category = calendar_service.ensure_category(db, payload.name, payload.color)
    if payload.icon is not None:
        category.icon = payload.icon
    db.commit()
    db.refresh(category)
    return CalendarCategoryOut.model_validate(category)
