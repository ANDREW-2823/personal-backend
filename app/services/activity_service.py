from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models.settings import ActivityLog
from app.models.task import new_id


def log(
    db: Session,
    kind: str,
    summary: str,
    *,
    detail: dict[str, Any] | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
) -> ActivityLog:
    entry = ActivityLog(
        id=new_id(),
        kind=kind,
        summary=summary[:400],
        detail=detail,
        entity_type=entity_type,
        entity_id=entity_id,
    )
    db.add(entry)
    return entry
