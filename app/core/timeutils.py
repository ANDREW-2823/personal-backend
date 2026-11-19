from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.relativedelta import relativedelta


def get_zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def local_now(tz_name: str) -> datetime:
    return datetime.now(get_zone(tz_name))


def day_bounds_utc(tz_name: str, day: date | None = None) -> tuple[datetime, datetime]:
    zone = get_zone(tz_name)
    today = day or datetime.now(zone).date()
    start_local = datetime.combine(today, time.min, tzinfo=zone)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def week_bounds_utc(tz_name: str, day: date | None = None) -> tuple[datetime, datetime]:
    zone = get_zone(tz_name)
    today = day or datetime.now(zone).date()
    start_local = datetime.combine(today, time.min, tzinfo=zone) - timedelta(days=today.weekday())
    end_local = start_local + timedelta(days=7)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def month_bounds_utc(tz_name: str, day: date | None = None) -> tuple[datetime, datetime]:
    zone = get_zone(tz_name)
    today = day or datetime.now(zone).date()
    start_local = datetime.combine(today.replace(day=1), time.min, tzinfo=zone)
    end_local = start_local + relativedelta(months=1)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def local_date(tz_name: str, value: datetime | None = None) -> date:
    zone = get_zone(tz_name)
    if value is None:
        return datetime.now(zone).date()
    return as_utc(value).astimezone(zone).date()


def next_occurrence(
    base: datetime,
    repeat: str,
    *,
    interval_minutes: int | None = None,
    timezone_name: str = "UTC",
    after: datetime | None = None,
) -> datetime | None:
    """Return the next UTC occurrence strictly after ``after`` (default: now)."""
    if repeat in {"none", ""}:
        return None
    zone = get_zone(timezone_name)
    base_utc = as_utc(base) or utcnow()
    threshold = as_utc(after) or utcnow()

    local_base = base_utc.astimezone(zone)
    candidate = local_base
    guard = 0
    while guard < 2000:
        guard += 1
        if repeat == "daily":
            candidate = candidate + timedelta(days=1)
        elif repeat == "weekdays":
            candidate = candidate + timedelta(days=1)
            while candidate.weekday() >= 5:
                candidate = candidate + timedelta(days=1)
        elif repeat == "weekly":
            candidate = candidate + timedelta(weeks=1)
        elif repeat == "monthly":
            candidate = candidate + relativedelta(months=1)
        elif repeat == "custom":
            candidate = candidate + timedelta(minutes=interval_minutes or 60)
        else:
            return None
        if candidate.astimezone(UTC) > threshold:
            return candidate.astimezone(UTC)
    return None


def humanize_delta(target: datetime, reference: datetime | None = None) -> str:
    ref = as_utc(reference) or utcnow()
    delta = (as_utc(target) or ref) - ref
    seconds = int(delta.total_seconds())
    past = seconds < 0
    seconds = abs(seconds)
    if seconds < 60:
        text = "moments"
    elif seconds < 3600:
        text = f"{seconds // 60} min"
    elif seconds < 86400:
        text = f"{seconds // 3600} h"
    else:
        text = f"{seconds // 86400} d"
    return f"{text} ago" if past else f"in {text}"
