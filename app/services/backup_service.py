from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import Date, DateTime, delete, insert
from sqlalchemy.orm import Session

from app.core.timeutils import utcnow
from app.db.base import Base
from app.models import *  # noqa: F401,F403  (ensure all tables are registered)

SKIP_TABLES = {"alembic_version"}


def _serialize_row(row: Any) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for column in row.__table__.columns:
        value = getattr(row, column.name)
        if isinstance(value, (datetime, date)):
            data[column.name] = value.isoformat()
        else:
            data[column.name] = value
    return data


def export_all(db: Session) -> dict[str, Any]:
    tables = [t for t in Base.metadata.sorted_tables if t.name not in SKIP_TABLES]
    payload: dict[str, Any] = {
        "format": "jarvis-backup",
        "version": 1,
        "exported_at": utcnow().isoformat(),
        "tables": {},
    }
    for table in tables:
        rows = db.execute(table.select()).all()
        payload["tables"][table.name] = [
            {column.name: _serialize_row(row)[column.name] for column in table.columns}
            for row in rows
        ]
    return payload


def import_all(db: Session, payload: dict[str, Any], *, replace: bool = False) -> dict[str, int]:
    if payload.get("format") != "jarvis-backup" or payload.get("version") != 1:
        raise ValueError("Unsupported backup format or version.")
    data = payload.get("tables") or {}
    tables = [t for t in Base.metadata.sorted_tables if t.name not in SKIP_TABLES]
    restored: dict[str, int] = {}

    # Reverse dependency order for clearing existing rows.
    if replace:
        for table in reversed(tables):
            db.execute(delete(table))

    for table in tables:
        rows = data.get(table.name) or []
        prepared: list[dict[str, Any]] = []
        for raw in rows:
            row = dict(raw)
            for column in table.columns:
                if column.name not in row:
                    continue
                value = row[column.name]
                if value is None:
                    continue
                if isinstance(column.type, DateTime) and isinstance(value, str):
                    row[column.name] = datetime.fromisoformat(value)
                elif isinstance(column.type, Date) and isinstance(value, str):
                    row[column.name] = date.fromisoformat(value)
            prepared.append(row)
        if prepared:
            db.execute(insert(table), prepared)
        restored[table.name] = len(prepared)
    db.commit()
    return restored


def backup_stats(payload: dict[str, Any]) -> dict[str, int]:
    return {name: len(rows or []) for name, rows in (payload.get("tables") or {}).items()}
