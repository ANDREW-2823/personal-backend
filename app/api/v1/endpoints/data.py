from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.security import require_token
from app.db.session import get_db
from app.services import backup_service

router = APIRouter(prefix="/data", tags=["data"], dependencies=[Depends(require_token)])


class ImportRequest(BaseModel):
    payload: dict[str, Any]
    replace: bool = False
    confirm: str = ""


@router.get("/export")
def export_data(db: Session = Depends(get_db)) -> dict[str, Any]:
    return backup_service.export_all(db)


@router.get("/backup-info")
def backup_info(db: Session = Depends(get_db)) -> dict[str, Any]:
    payload = backup_service.export_all(db)
    return {
        "exported_at": payload["exported_at"],
        "tables": backup_service.backup_stats(payload),
    }


@router.post("/import")
def import_data(payload: ImportRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    if payload.replace and payload.confirm != "REPLACE-ALL":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Replacing existing data requires confirm = 'REPLACE-ALL'.",
        )
    try:
        restored = backup_service.import_all(db, payload.payload, replace=payload.replace)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return {"restored": restored}
