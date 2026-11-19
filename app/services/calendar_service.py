from __future__ import annotations

import base64
import binascii
import html as html_lib
import re
from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from dateutil.relativedelta import relativedelta
from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.core.datetime_utils import normalize_dt
from app.core.timeutils import as_utc, day_bounds_utc, get_zone, utcnow
from app.models.calendar import (
    CalendarCategory,
    CalendarEvent,
    JournalAttachment,
    JournalDrawing,
    JournalEntry,
)
from app.models.note import Note
from app.models.reminder import Reminder
from app.models.task import Task
from app.schemas.calendar import (
    AnalyticsDayOut,
    AnalyticsOut,
    AnalyticsTotalsOut,
    CalendarCategoryOut,
    CalendarOverviewOut,
    DayBundleOut,
    DayStatsOut,
    DrawingData,
    DrawingPoint,
    DrawingStroke,
    JournalAttachmentCreate,
    JournalAttachmentOut,
    JournalOut,
    JournalPreviewOut,
    JournalSearchItem,
    JournalUpsert,
)
from app.schemas.core import EventCreate, EventOut, EventUpdate, NoteOut, TaskOut
from app.services import activity_service
from app.services.settings_service import get_settings

CATEGORY_PALETTE = [
    "#5FBF8B",
    "#6B8CC7",
    "#B07CC6",
    "#E3A857",
    "#C9302E",
    "#4FB6C9",
    "#8FAE5D",
    "#D4846A",
]
MAX_RECURRENCE = 120
RECURRENCE_HORIZON_MONTHS = 18
MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024


# --------------------------------------------------------------------- helpers


def _tz(db: Session) -> str:
    return get_settings(db).timezone


def _strip_html(value: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    text = re.sub(r"</(?:p|div|h[1-6]|li|blockquote)>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    return re.sub(r"[ \t]+", " ", text).strip()


def _block_text(block: dict[str, Any]) -> str:
    block_type = str(block.get("type") or "paragraph")
    if block_type == "divider":
        return ""
    if block_type == "image":
        return str(block.get("caption") or "")
    text = str(block.get("text") or "").strip()
    if not text:
        text = _strip_html(str(block.get("html") or ""))
    if block_type == "checklist":
        return f"[{'x' if block.get('checked') else ' '}] {text}"
    if block_type == "quote":
        return f"> {text}"
    return text


def _blocks_plain_text(blocks: Iterable[dict[str, Any]]) -> str:
    lines = [_block_text(block).strip() for block in blocks]
    return "\n".join(line for line in lines if line).strip()


def _word_count(text: str) -> int:
    return len(re.findall(r"[\w'-]+", text, flags=re.UNICODE))


def _snippet(text: str, query: str, length: int = 180) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if not compact:
        return ""
    if not query:
        return compact[:length]
    index = compact.lower().find(query.lower())
    if index < 0:
        return compact[:length]
    start = max(0, index - 60)
    end = min(len(compact), index + len(query) + 100)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(compact) else ""
    return f"{prefix}{compact[start:end].strip()}{suffix}"


def _local_date(tz: str, value: datetime | None) -> date | None:
    if value is None:
        return None
    aware = as_utc(value) or value
    return aware.astimezone(get_zone(tz)).date()


def _range_utc(db: Session, start_date: date, end_date: date) -> tuple[datetime, datetime]:
    if end_date < start_date:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "End date must be after start date"
        )
    zone = get_zone(_tz(db))
    start_local = datetime.combine(start_date, time.min, tzinfo=zone)
    end_local = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=zone)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def _advance_local(value: datetime, recurrence: str, zone) -> datetime | None:  # noqa: ANN001
    local = (as_utc(value) or value).astimezone(zone)
    if recurrence == "daily":
        return (local + timedelta(days=1)).astimezone(UTC)
    if recurrence == "weekdays":
        candidate = local + timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate += timedelta(days=1)
        return candidate.astimezone(UTC)
    if recurrence == "weekly":
        return (local + timedelta(weeks=1)).astimezone(UTC)
    if recurrence == "monthly":
        return (local + relativedelta(months=1)).astimezone(UTC)
    return None


def _recurrence_starts(start: datetime, recurrence: str, tz: str) -> list[datetime]:
    if recurrence in {"", "none"}:
        return []
    zone = get_zone(tz)
    horizon = (as_utc(start) or start) + relativedelta(months=RECURRENCE_HORIZON_MONTHS)
    values: list[datetime] = []
    cursor = as_utc(start) or start
    while len(values) < MAX_RECURRENCE:
        next_value = _advance_local(cursor, recurrence, zone)
        if next_value is None or next_value > horizon:
            break
        values.append(next_value)
        cursor = next_value
    return values


# ---------------------------------------------------------------------- events


def _sync_event_reminder(db: Session, event: CalendarEvent) -> None:
    existing = db.scalar(select(Reminder).where(Reminder.event_id == event.id))
    if event.reminder_minutes is None:
        if existing is not None:
            db.delete(existing)
        return
    remind_at = (as_utc(event.start_at) or event.start_at) - timedelta(
        minutes=max(0, event.reminder_minutes)
    )
    if remind_at <= utcnow():
        if existing is not None:
            db.delete(existing)
        return
    if existing is None:
        db.add(
            Reminder(
                title=f"Event: {event.title}",
                notes=event.description,
                remind_at=remind_at,
                repeat="none",
                timezone=_tz(db),
                status="scheduled",
                event_id=event.id,
            )
        )
    else:
        existing.title = f"Event: {event.title}"
        existing.notes = event.description
        existing.remind_at = remind_at
        existing.status = "scheduled"
        existing.snooze_until = None


def create_event(db: Session, data: EventCreate) -> CalendarEvent:
    tz = _tz(db)
    start = normalize_dt(data.start_at, tz)
    end = normalize_dt(data.end_at, tz)
    assert start and end
    if end < start:
        raise HTTPException(422, "Event end must be after start")
    from app.models.task import new_id

    base = CalendarEvent(
        id=new_id(),
        title=data.title.strip(),
        description=data.description,
        start_at=start,
        end_at=end,
        all_day=data.all_day,
        location=data.location,
        color=data.color,
        task_id=data.task_id,
        kind="event",
        source="local",
        category=data.category.strip() if data.category else None,
        priority=data.priority,
        status=data.status,
        recurrence=data.recurrence,
        reminder_minutes=data.reminder_minutes,
        attachment_url=data.attachment_url,
    )
    base.series_id = base.id
    duration = end - start
    events = [base]
    for occurrence_start in _recurrence_starts(start, data.recurrence, tz):
        events.append(
            CalendarEvent(
                id=new_id(),
                title=data.title.strip(),
                description=data.description,
                start_at=occurrence_start,
                end_at=occurrence_start + duration,
                all_day=data.all_day,
                location=data.location,
                color=data.color,
                task_id=data.task_id,
                kind="event",
                source="local",
                category=data.category.strip() if data.category else None,
                priority=data.priority,
                status=data.status,
                recurrence=data.recurrence,
                series_id=base.id,
                reminder_minutes=data.reminder_minutes,
                attachment_url=data.attachment_url,
            )
        )
    for event in events:
        db.add(event)
    db.flush()
    for event in events:
        _sync_event_reminder(db, event)
    if base.category:
        ensure_category(db, base.category)
    activity_service.log(
        db,
        "event.created",
        f"Created event “{base.title}”",
        detail={"recurrence": base.recurrence, "occurrences": len(events)},
        entity_type="event",
        entity_id=base.id,
    )
    return base


def get_event(db: Session, event_id: str) -> CalendarEvent:
    event = db.get(CalendarEvent, event_id)
    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Event not found")
    return event


def update_event(db: Session, event_id: str, data: EventUpdate) -> CalendarEvent:
    event = get_event(db, event_id)
    tz = _tz(db)
    payload = data.model_dump(exclude_unset=True)
    if payload.pop("clear_reminder", False):
        payload["reminder_minutes"] = None
    if "start_at" in payload and payload["start_at"] is not None:
        payload["start_at"] = normalize_dt(payload["start_at"], tz)
    if "end_at" in payload and payload["end_at"] is not None:
        payload["end_at"] = normalize_dt(payload["end_at"], tz)
    if "category" in payload and payload["category"]:
        payload["category"] = str(payload["category"]).strip()
        ensure_category(db, payload["category"])
    for key, value in payload.items():
        setattr(event, key, value)
    if as_utc(event.end_at) < as_utc(event.start_at):
        raise HTTPException(422, "Event end must be after start")
    _sync_event_reminder(db, event)
    db.flush()
    return event


def delete_event(db: Session, event_id: str, *, delete_series: bool = False) -> int:
    event = get_event(db, event_id)
    targets = [event]
    if delete_series and event.series_id:
        targets = list(
            db.scalars(select(CalendarEvent).where(CalendarEvent.series_id == event.series_id))
        )
    for target in targets:
        db.delete(target)
    db.flush()
    return len(targets)


def range_events(db: Session, start: datetime, end: datetime) -> list[CalendarEvent]:
    return list(
        db.scalars(
            select(CalendarEvent)
            .where(CalendarEvent.start_at < end, CalendarEvent.end_at >= start)
            .order_by(CalendarEvent.start_at.asc())
        )
    )


def deadlines(db: Session, start: datetime, end: datetime) -> list[Task]:
    return list(
        db.scalars(
            select(Task)
            .options(selectinload(Task.subtasks))
            .where(
                Task.due_at >= start,
                Task.due_at < end,
                Task.status.notin_(["done", "cancelled"]),
            )
            .order_by(Task.due_at.asc())
        )
    )


def detect_conflicts(events: list[CalendarEvent]) -> list[dict]:
    conflicts: list[dict] = []
    ordered = sorted(events, key=lambda e: as_utc(e.start_at) or e.start_at)
    for i, first in enumerate(ordered):
        for second in ordered[i + 1 :]:
            first_end = as_utc(first.end_at) or first.end_at
            second_start = as_utc(second.start_at) or second.start_at
            if second_start >= first_end:
                break
            conflicts.append(
                {
                    "event_ids": [first.id, second.id],
                    "titles": [first.title, second.title],
                    "overlap_start": max(
                        as_utc(first.start_at) or first.start_at,
                        second_start,
                    ).isoformat(),
                }
            )
    return conflicts[:20]


def agenda(db: Session, start: datetime, end: datetime) -> dict:
    events = range_events(db, start, end)
    return {
        "events": events,
        "deadlines": deadlines(db, start, end),
        "conflicts": detect_conflicts(events),
    }


# ----------------------------------------------------------------------- tasks


def tasks_in_range(db: Session, start: datetime, end: datetime) -> list[Task]:
    return list(
        db.scalars(
            select(Task)
            .options(selectinload(Task.subtasks))
            .where(
                or_(
                    (Task.due_at >= start) & (Task.due_at < end),
                    (Task.completed_at >= start) & (Task.completed_at < end),
                )
            )
            .order_by(Task.due_at.asc().nullslast(), Task.created_at.asc())
        ).unique()
    )


def tasks_for_date(db: Session, day: date) -> list[Task]:
    start, end = day_bounds_utc(_tz(db), day)
    return tasks_in_range(db, start, end)


def pending_tasks(db: Session, limit: int = 100) -> list[Task]:
    return list(
        db.scalars(
            select(Task)
            .options(selectinload(Task.subtasks))
            .where(Task.status.in_(["todo", "in_progress", "blocked"]))
            .order_by(Task.due_at.asc().nullslast(), Task.priority.asc())
            .limit(limit)
        ).unique()
    )


def overdue_tasks(db: Session, limit: int = 100) -> list[Task]:
    return list(
        db.scalars(
            select(Task)
            .options(selectinload(Task.subtasks))
            .where(Task.due_at < utcnow(), Task.status.in_(["todo", "in_progress", "blocked"]))
            .order_by(Task.due_at.asc())
            .limit(limit)
        ).unique()
    )


def completed_tasks(db: Session, start: datetime, end: datetime) -> list[Task]:
    return list(
        db.scalars(
            select(Task)
            .options(selectinload(Task.subtasks))
            .where(Task.completed_at >= start, Task.completed_at < end)
            .order_by(Task.completed_at.asc())
        ).unique()
    )


def _task_stats(db: Session, start: datetime, end: datetime) -> DayStatsOut:
    due = int(
        db.scalar(
            select(func.count())
            .select_from(Task)
            .where(
                Task.due_at >= start,
                Task.due_at < end,
                Task.status != "cancelled",
            )
        )
        or 0
    )
    completed = int(
        db.scalar(
            select(func.count())
            .select_from(Task)
            .where(Task.completed_at >= start, Task.completed_at < end)
        )
        or 0
    )
    overdue = int(
        db.scalar(
            select(func.count())
            .select_from(Task)
            .where(
                Task.due_at < start,
                Task.status.in_(["todo", "in_progress", "blocked"]),
            )
        )
        or 0
    )
    rate = (completed / due * 100) if due else 0.0
    events = int(
        db.scalar(
            select(func.count())
            .select_from(CalendarEvent)
            .where(CalendarEvent.start_at >= start, CalendarEvent.start_at < end)
        )
        or 0
    )
    return DayStatsOut(
        tasks_due=due,
        tasks_completed=completed,
        tasks_overdue=overdue,
        events=events,
        task_completion=round(min(100.0, rate), 1),
    )


def _notes_for_range(db: Session, start: datetime, end: datetime) -> list[Note]:
    return list(
        db.scalars(
            select(Note)
            .where(Note.created_at >= start, Note.created_at < end)
            .order_by(Note.created_at.asc())
        )
    )


# --------------------------------------------------------------------- journal


def _journal_query():
    return select(JournalEntry).options(
        selectinload(JournalEntry.drawing),
        selectinload(JournalEntry.attachments),
    )


def get_journal(db: Session, entry_date: date) -> JournalEntry | None:
    return db.scalar(_journal_query().where(JournalEntry.entry_date == entry_date))


def _drawing_payload(entry: JournalEntry) -> DrawingData | None:
    if entry.drawing is None:
        return None
    return DrawingData(
        strokes=[
            DrawingStroke.model_validate(stroke) for stroke in (entry.drawing.strokes or [])
        ],
        width=entry.drawing.width,
        height=entry.drawing.height,
    )


def journal_payload(entry: JournalEntry) -> JournalOut:
    return JournalOut(
        id=entry.id,
        entry_date=entry.entry_date,
        title=entry.title,
        mood=entry.mood,
        summary=entry.summary,
        highlight=entry.highlight,
        blocks=entry.blocks or [],
        plain_text=entry.plain_text,
        word_count=entry.word_count,
        drawing=_drawing_payload(entry),
        attachments=[JournalAttachmentOut.model_validate(item) for item in entry.attachments],
        created_at=entry.created_at,
        updated_at=entry.updated_at,
    )


def _check_version(entry: JournalEntry | None, expected: datetime | None) -> None:
    if entry is None or expected is None:
        return
    expected_utc = as_utc(expected)
    actual_utc = as_utc(entry.updated_at)
    if expected_utc is None or actual_utc is None:
        return
    if abs((actual_utc - expected_utc).total_seconds()) > 1:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This journal page changed elsewhere. Reload it before saving again.",
        )


def upsert_journal(
    db: Session, entry_date: date, data: JournalUpsert, *, source: str = "ui"
) -> JournalEntry:
    entry = get_journal(db, entry_date)
    _check_version(entry, data.expected_updated_at)
    created = entry is None
    if entry is None:
        entry = JournalEntry(entry_date=entry_date)
        db.add(entry)
    blocks = [block.model_dump(mode="json") for block in data.blocks]
    plain_text = (
        data.plain_text.strip()
        if data.plain_text is not None
        else _blocks_plain_text(blocks)
    )
    entry.title = data.title.strip()
    entry.mood = data.mood
    entry.summary = data.summary
    entry.highlight = data.highlight
    entry.blocks = blocks
    entry.plain_text = plain_text
    entry.word_count = _word_count(plain_text)
    if data.drawing is not None:
        strokes = [stroke.model_dump(mode="json") for stroke in data.drawing.strokes]
        if entry.drawing is None:
            entry.drawing = JournalDrawing(
                strokes=strokes,
                width=data.drawing.width,
                height=data.drawing.height,
            )
        else:
            entry.drawing.strokes = strokes
            entry.drawing.width = data.drawing.width
            entry.drawing.height = data.drawing.height
    db.flush()
    if created:
        activity_service.log(
            db,
            "journal.created",
            f"Started a journal page for {entry_date.isoformat()}",
            detail={"source": source, "words": entry.word_count},
            entity_type="journal",
            entity_id=entry.id,
        )
    return entry


def delete_journal(db: Session, entry_date: date) -> None:
    entry = get_journal(db, entry_date)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Journal entry not found")
    db.delete(entry)
    db.flush()


def add_attachment(
    db: Session, entry_date: date, data: JournalAttachmentCreate
) -> JournalAttachment:
    entry = get_journal(db, entry_date)
    if entry is None:
        entry = JournalEntry(entry_date=entry_date)
        db.add(entry)
        db.flush()
    raw = data.data
    mime_type = data.mime_type
    if raw.startswith("data:"):
        header, _, payload = raw.partition(",")
        if ";base64" in header:
            mime_type = header[5:].split(";", 1)[0] or mime_type
        raw = payload
    try:
        decoded = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "Attachment is not valid base64"
        ) from exc
    if len(decoded) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            "Attachments are limited to 8 MB.",
        )
    attachment = JournalAttachment(
        entry_id=entry.id,
        filename=data.filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1][:300],
        mime_type=mime_type[:120],
        size_bytes=len(decoded),
        data=data.data,
    )
    db.add(attachment)
    db.flush()
    return attachment


def get_attachment(db: Session, attachment_id: str) -> JournalAttachment:
    attachment = db.get(JournalAttachment, attachment_id)
    if attachment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found")
    return attachment


def delete_attachment(db: Session, attachment_id: str) -> None:
    attachment = get_attachment(db, attachment_id)
    db.delete(attachment)
    db.flush()


def _preview_drawing(entry: JournalEntry) -> list[DrawingStroke]:
    if entry.drawing is None or not entry.drawing.strokes:
        return []
    result: list[DrawingStroke] = []
    for stroke in (entry.drawing.strokes or [])[:3]:
        points = list(stroke.get("points") or [])
        if len(points) > 40:
            step = max(1, len(points) // 40)
            points = points[::step][:40]
        try:
            result.append(
                DrawingStroke(
                    tool=stroke.get("tool", "pen"),
                    color=stroke.get("color", "#1f2937"),
                    size=stroke.get("size", 2),
                    points=[DrawingPoint.model_validate(point) for point in points],
                )
            )
        except Exception:  # noqa: BLE001 - malformed legacy strokes are skipped
            continue
    return result


def journal_previews(
    db: Session, start: date, end: date
) -> list[JournalPreviewOut]:
    entries = list(
        db.scalars(
            _journal_query()
            .where(JournalEntry.entry_date >= start, JournalEntry.entry_date <= end)
            .order_by(JournalEntry.entry_date.asc())
        )
    )
    tz = _tz(db)
    start_utc, end_utc = _range_utc(db, start, end)
    event_counts: dict[date, int] = {}
    for event in range_events(db, start_utc, end_utc):
        day_key = _local_date(tz, event.start_at)
        if day_key is not None:
            event_counts[day_key] = event_counts.get(day_key, 0) + 1
    task_counts: dict[date, list[int]] = {}
    for task in tasks_in_range(db, start_utc, end_utc):
        key = _local_date(tz, task.due_at) or _local_date(tz, task.completed_at)
        if key is None:
            continue
        bucket = task_counts.setdefault(key, [0, 0])
        bucket[0] += 1
        if task.status == "done":
            bucket[1] += 1
    previews: list[JournalPreviewOut] = []
    for entry in entries:
        excerpt = _snippet(entry.plain_text or entry.summary or "", "", 160)
        counts = task_counts.get(entry.entry_date, [0, 0])
        previews.append(
            JournalPreviewOut(
                date=entry.entry_date,
                title=entry.title,
                excerpt=excerpt,
                mood=entry.mood,
                word_count=entry.word_count,
                has_drawing=bool(entry.drawing and entry.drawing.strokes),
                drawing_preview=_preview_drawing(entry),
                event_count=event_counts.get(entry.entry_date, 0),
                task_count=counts[0],
                task_done=counts[1],
                updated_at=entry.updated_at,
            )
        )
    return previews


def search_journal(
    db: Session,
    query: str,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    limit: int = 30,
) -> list[JournalSearchItem]:
    stmt = _journal_query()
    if query.strip():
        pattern = f"%{query.strip()}%"
        stmt = stmt.where(
            or_(
                JournalEntry.title.ilike(pattern),
                JournalEntry.plain_text.ilike(pattern),
                JournalEntry.summary.ilike(pattern),
                JournalEntry.highlight.ilike(pattern),
            )
        )
    if start_date is not None:
        stmt = stmt.where(JournalEntry.entry_date >= start_date)
    if end_date is not None:
        stmt = stmt.where(JournalEntry.entry_date <= end_date)
    entries = list(
        db.scalars(stmt.order_by(JournalEntry.entry_date.desc()).limit(limit))
    )
    results: list[JournalSearchItem] = []
    for entry in entries:
        haystack = f"{entry.title}\n{entry.plain_text}\n{entry.summary or ''}"
        results.append(
            JournalSearchItem(
                date=entry.entry_date,
                title=entry.title or entry.entry_date.strftime("%d %B %Y"),
                excerpt=_snippet(haystack, query, 220),
                match="journal",
                updated_at=entry.updated_at,
            )
        )
    return results


def search_calendar(
    db: Session,
    query: str,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
) -> dict[str, Any]:
    entries = search_journal(db, query, start_date=start_date, end_date=end_date)
    start = (
        datetime.combine(start_date, time.min, tzinfo=UTC)
        if start_date
        else datetime(1970, 1, 1, tzinfo=UTC)
    )
    end = (
        datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=UTC)
        if end_date
        else datetime(2100, 1, 1, tzinfo=UTC)
    )
    pattern = f"%{query.strip()}%"
    events = list(
        db.scalars(
            select(CalendarEvent)
            .where(
                CalendarEvent.start_at >= start,
                CalendarEvent.start_at < end,
                or_(
                    CalendarEvent.title.ilike(pattern),
                    CalendarEvent.description.ilike(pattern),
                    CalendarEvent.location.ilike(pattern),
                ),
            )
            .order_by(CalendarEvent.start_at.desc())
            .limit(30)
        )
    )
    tasks = list(
        db.scalars(
            select(Task)
            .options(selectinload(Task.subtasks))
            .where(
                or_(Task.title.ilike(pattern), Task.description.ilike(pattern)),
                or_(
                    (Task.due_at >= start) & (Task.due_at < end),
                    (Task.completed_at >= start) & (Task.completed_at < end),
                    (Task.due_at.is_(None)),
                ),
            )
            .order_by(Task.created_at.desc())
            .limit(30)
        ).unique()
    )
    return {"entries": entries, "events": events, "tasks": tasks}


# ---------------------------------------------------------------- categories


def ensure_category(db: Session, name: str, color: str | None = None) -> CalendarCategory:
    clean = name.strip()[:80]
    existing = db.scalar(
        select(CalendarCategory).where(
            func.lower(CalendarCategory.name) == clean.lower()
        )
    )
    if existing is not None:
        return existing
    count = int(db.scalar(select(func.count()).select_from(CalendarCategory)) or 0)
    category = CalendarCategory(
        name=clean,
        color=color or CATEGORY_PALETTE[count % len(CATEGORY_PALETTE)],
        position=count,
    )
    db.add(category)
    db.flush()
    return category


def list_categories(db: Session) -> list[CalendarCategory]:
    names = {
        row[0]
        for row in db.execute(
            select(Task.category).where(Task.category.is_not(None)).distinct()
        ).all()
        if row[0]
    }
    names.update(
        row[0]
        for row in db.execute(
            select(CalendarEvent.category).where(CalendarEvent.category.is_not(None)).distinct()
        ).all()
        if row[0]
    )
    existing = list(
        db.scalars(
            select(CalendarCategory).order_by(
                CalendarCategory.position.asc(), CalendarCategory.name.asc()
            )
        )
    )
    known = {item.name.lower() for item in existing}
    for name in sorted(names):
        if name.lower() not in known:
            existing.append(ensure_category(db, name))
    return existing


# ------------------------------------------------------------------- bundles


def day_bundle(db: Session, day: date) -> DayBundleOut:
    tz = _tz(db)
    start, end = day_bounds_utc(tz, day)
    journal = get_journal(db, day)
    events = range_events(db, start, end)
    tasks = tasks_in_range(db, start, end)
    notes = _notes_for_range(db, start, end)
    stats = _task_stats(db, start, end)
    stats.journal_words = journal.word_count if journal else 0
    return DayBundleOut(
        date=day,
        journal=journal_payload(journal) if journal else None,
        events=[EventOut.model_validate(event) for event in events],
        tasks=[TaskOut.model_validate(task) for task in tasks],
        notes=[NoteOut.model_validate(note) for note in notes],
        categories=[CalendarCategoryOut.model_validate(item) for item in list_categories(db)],
        stats=stats,
    )


def overview(db: Session, start_date: date, end_date: date) -> CalendarOverviewOut:
    start, end = _range_utc(db, start_date, end_date)
    events = range_events(db, start, end)
    tasks = tasks_in_range(db, start, end)
    previews = journal_previews(db, start_date, end_date)
    stats = _task_stats(db, start, end)
    stats.journal_words = sum(item.word_count for item in previews)
    return CalendarOverviewOut(
        start=start_date,
        end=end_date,
        events=[EventOut.model_validate(event) for event in events],
        tasks=[TaskOut.model_validate(task) for task in tasks],
        journal=previews,
        categories=[CalendarCategoryOut.model_validate(item) for item in list_categories(db)],
        stats=stats,
    )


# ------------------------------------------------------------------ analytics


def analytics(db: Session, start_date: date, end_date: date) -> AnalyticsOut:
    tz = _tz(db)
    start, end = _range_utc(db, start_date, end_date)
    days = (end_date - start_date).days + 1
    total_days = max(1, days)
    per_day: dict[date, AnalyticsDayOut] = {
        start_date + timedelta(days=offset): AnalyticsDayOut(
            date=start_date + timedelta(days=offset)
        )
        for offset in range(days)
    }
    due_tasks = list(
        db.scalars(
            select(Task).where(
                Task.due_at >= start,
                Task.due_at < end,
                Task.status != "cancelled",
            )
        )
    )
    completed = list(
        db.scalars(
            select(Task).where(Task.completed_at >= start, Task.completed_at < end)
        )
    )
    all_tasks = {task.id: task for task in [*due_tasks, *completed]}.values()
    for task in all_tasks:
        due_day = _local_date(tz, task.due_at)
        if due_day in per_day:
            per_day[due_day].tasks_due += 1
        done_day = _local_date(tz, task.completed_at)
        if done_day in per_day:
            per_day[done_day].tasks_completed += 1
    events = range_events(db, start, end)
    for event in events:
        event_day = _local_date(tz, event.start_at)
        if event_day in per_day:
            per_day[event_day].events += 1
    entries = list(
        db.scalars(
            select(JournalEntry).where(
                JournalEntry.entry_date >= start_date, JournalEntry.entry_date <= end_date
            )
        )
    )
    for entry in entries:
        if entry.entry_date in per_day:
            per_day[entry.entry_date].journal_words += entry.word_count
    try:
        from app.models.focus import FocusSession

        sessions = list(
            db.scalars(
                select(FocusSession).where(
                    FocusSession.started_at >= start, FocusSession.started_at < end
                )
            )
        )
        for session in sessions:
            session_day = _local_date(tz, session.started_at)
            if session_day in per_day:
                per_day[session_day].focus_minutes += max(0, session.elapsed_seconds // 60)
    except Exception:  # noqa: BLE001 - focus data is supplementary
        sessions = []
    due_count = len(due_tasks)
    done_due = sum(1 for task in due_tasks if task.status == "done")
    completion_rate = round(done_due / due_count * 100, 1) if due_count else 0.0
    focus_minutes = sum(item.focus_minutes for item in per_day.values())
    journal_days = len(entries)
    pending = int(
        db.scalar(
            select(func.count())
            .select_from(Task)
            .where(Task.status.in_(["todo", "in_progress", "blocked"]))
        )
        or 0
    )
    overdue = int(
        db.scalar(
            select(func.count())
            .select_from(Task)
            .where(Task.due_at < utcnow(), Task.status.in_(["todo", "in_progress", "blocked"]))
        )
        or 0
    )
    category_counts: dict[str, int] = {}
    for task in all_tasks:
        if task.category:
            category_counts[task.category] = category_counts.get(task.category, 0) + 1
    for event in events:
        if event.category:
            category_counts[event.category] = category_counts.get(event.category, 0) + 1
    categories = [
        {"name": name, "count": count}
        for name, count in sorted(category_counts.items(), key=lambda item: item[1], reverse=True)
    ]
    journal_component = journal_days / total_days * 100
    focus_component = min(100.0, (focus_minutes / (total_days * 60)) * 100) if total_days else 0.0
    score = round(
        0.55 * completion_rate + 0.25 * journal_component + 0.20 * focus_component,
        1,
    )
    upcoming = list(
        db.scalars(
            select(CalendarEvent)
            .where(CalendarEvent.start_at >= utcnow())
            .order_by(CalendarEvent.start_at.asc())
            .limit(8)
        )
    )
    return AnalyticsOut(
        start=start_date,
        end=end_date,
        totals=AnalyticsTotalsOut(
            tasks_total=due_count,
            tasks_completed=len(completed),
            tasks_pending=pending,
            tasks_overdue=overdue,
            events=len(events),
            journal_days=journal_days,
            focus_minutes=focus_minutes,
            completion_rate=completion_rate,
            productivity_score=score,
        ),
        per_day=[per_day[start_date + timedelta(days=offset)] for offset in range(days)],
        categories=categories,
        upcoming_events=[EventOut.model_validate(event) for event in upcoming],
    )


# --------------------------------------------------------------- AI summaries


def _event_brief(event: CalendarEvent, tz: str) -> dict[str, Any]:
    start = as_utc(event.start_at) or event.start_at
    end = as_utc(event.end_at) or event.end_at
    return {
        "id": event.id,
        "title": event.title,
        "description": event.description,
        "start_at": start.isoformat(),
        "end_at": end.isoformat(),
        "local_start": start.astimezone(get_zone(tz)).isoformat(),
        "all_day": event.all_day,
        "location": event.location,
        "category": event.category,
        "priority": event.priority,
        "status": event.status,
        "recurrence": event.recurrence,
        "series_id": event.series_id,
    }


def _task_brief(task: Task, tz: str) -> dict[str, Any]:
    due = as_utc(task.due_at) if task.due_at else None
    completed = as_utc(task.completed_at) if task.completed_at else None
    subtasks = list(task.subtasks or [])
    done_subtasks = sum(1 for item in subtasks if item.completed)
    return {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "status": task.status,
        "priority": task.priority,
        "category": task.category,
        "due_at": due.isoformat() if due else None,
        "local_due": due.astimezone(get_zone(tz)).isoformat() if due else None,
        "completed_at": completed.isoformat() if completed else None,
        "local_completed": (
            completed.astimezone(get_zone(tz)).isoformat() if completed else None
        ),
        "subtasks": [{"title": item.title, "completed": item.completed} for item in subtasks],
        "subtask_progress": (
            round(done_subtasks / len(subtasks) * 100, 1)
            if subtasks
            else (100.0 if task.status == "done" else 0.0)
        ),
    }


def _journal_brief(entry: JournalEntry | None) -> dict[str, Any] | None:
    if entry is None:
        return None
    recorded = bool((entry.plain_text or "").strip()) or any(
        _block_text(block).strip() for block in (entry.blocks or [])
    )
    return {
        "id": entry.id,
        "date": entry.entry_date.isoformat(),
        "title": entry.title,
        "mood": entry.mood,
        "summary": entry.summary,
        "highlight": entry.highlight,
        "plain_text": entry.plain_text,
        "blocks": entry.blocks or [],
        "word_count": entry.word_count,
        "has_drawing": bool(entry.drawing and entry.drawing.strokes),
        "attachment_count": len(entry.attachments or []),
        "recorded": recorded,
        "updated_at": entry.updated_at.isoformat() if entry.updated_at else None,
    }


def daily_summary(db: Session, day: date) -> dict[str, Any]:
    """Structured, factual data for one local calendar day.

    The four categories stay separate on purpose: journal activities are
    user-written, events are scheduled, completed tasks are verified by
    ``completed_at``, and pending tasks have not been completed.
    """

    tz = _tz(db)
    start, end = day_bounds_utc(tz, day)
    journal = get_journal(db, day)
    events = range_events(db, start, end)
    due_tasks = [
        task
        for task in tasks_in_range(db, start, end)
        if task.due_at is not None and start <= (as_utc(task.due_at) or task.due_at) < end
    ]
    completed = completed_tasks(db, start, end)
    pending = [task for task in due_tasks if task.status not in {"done", "cancelled"}]
    overdue = overdue_tasks(db, limit=20)
    notes = _notes_for_range(db, start, end)
    stats = _task_stats(db, start, end)
    stats.journal_words = journal.word_count if journal else 0
    return {
        "date": day.isoformat(),
        "timezone": tz,
        "journal": _journal_brief(journal),
        "journal_recorded": bool(_journal_brief(journal) and _journal_brief(journal)["recorded"]),
        "events_scheduled": [_event_brief(event, tz) for event in events],
        "tasks_due": [_task_brief(task, tz) for task in due_tasks],
        "tasks_completed": [_task_brief(task, tz) for task in completed],
        "tasks_pending": [_task_brief(task, tz) for task in pending],
        "tasks_overdue": [_task_brief(task, tz) for task in overdue],
        "notes": [
            {
                "id": note.id,
                "title": note.title,
                "content": note.content,
                "category": note.category,
            }
            for note in notes
        ],
        "stats": stats.model_dump(mode="json"),
        "has_recorded_activity": bool(
            (journal and (journal.plain_text or "").strip()) or completed or notes
        ),
    }


def range_summary(db: Session, start_date: date, end_date: date) -> dict[str, Any]:
    if end_date < start_date:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "End date must be after start date"
        )
    tz = _tz(db)
    start, end = _range_utc(db, start_date, end_date)
    analytics_data = analytics(db, start_date, end_date)
    entries = list(
        db.scalars(
            _journal_query()
            .where(JournalEntry.entry_date >= start_date, JournalEntry.entry_date <= end_date)
            .order_by(JournalEntry.entry_date.asc())
        )
    )
    events = range_events(db, start, end)
    completed = completed_tasks(db, start, end)
    pending = [
        task
        for task in tasks_in_range(db, start, end)
        if task.status not in {"done", "cancelled"}
    ]
    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "timezone": tz,
        "journal_days": [_journal_brief(entry) for entry in entries],
        "recorded_journal_days": sum(
            1 for entry in entries if (entry.plain_text or "").strip()
        ),
        "events_scheduled": [_event_brief(event, tz) for event in events],
        "tasks_completed": [_task_brief(task, tz) for task in completed],
        "tasks_pending": [_task_brief(task, tz) for task in pending],
        "totals": analytics_data.totals.model_dump(mode="json"),
        "per_day": [item.model_dump(mode="json") for item in analytics_data.per_day],
        "categories": analytics_data.categories,
    }


def monthly_summary(db: Session, year: int, month: int) -> dict[str, Any]:
    if month < 1 or month > 12:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Month must be between 1 and 12")
    start_date = date(year, month, 1)
    end_date = start_date + relativedelta(months=1) - timedelta(days=1)
    data = range_summary(db, start_date, end_date)
    data["month"] = month
    data["year"] = year
    return data


def productivity_summary(db: Session, start_date: date, end_date: date) -> dict[str, Any]:
    analytics_data = analytics(db, start_date, end_date)
    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "totals": analytics_data.totals.model_dump(mode="json"),
        "categories": analytics_data.categories,
        "per_day": [item.model_dump(mode="json") for item in analytics_data.per_day],
        "upcoming_events": [
            _event_brief(event, _tz(db)) for event in analytics_data.upcoming_events
        ],
    }
