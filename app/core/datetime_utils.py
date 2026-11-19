from __future__ import annotations

from datetime import UTC, datetime

from app.core.timeutils import get_zone


def normalize_dt(value: datetime | None, tz_name: str) -> datetime | None:
    """Treat naive datetimes as user-local, return aware UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        zone = get_zone(tz_name)
        return value.replace(tzinfo=zone).astimezone(UTC)
    return value.astimezone(UTC)
