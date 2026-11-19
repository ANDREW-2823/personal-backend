from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import require_token
from app.db.session import get_db
from app.models.settings import ActivityLog, AppShortcut
from app.models.task import new_id
from app.schemas.core import (
    ActivityOut,
    AppSettingsSchema,
    MessageResponse,
    SettingsUpdate,
    ShortcutCreate,
    ShortcutOut,
)
from app.services import activity_service
from app.services.settings_service import get_settings, update_settings

router = APIRouter(tags=["settings"], dependencies=[Depends(require_token)])


@router.get("/settings", response_model=AppSettingsSchema)
def read_settings(db: Session = Depends(get_db)) -> AppSettingsSchema:
    return get_settings(db)


@router.put("/settings", response_model=AppSettingsSchema)
def write_settings(payload: SettingsUpdate, db: Session = Depends(get_db)) -> AppSettingsSchema:
    updated = update_settings(db, payload)
    activity_service.log(
        db,
        "settings.updated",
        "Application settings updated",
        detail={"timezone": updated.timezone, "ai_provider": updated.ai_provider},
    )
    db.commit()
    return updated


@router.get("/activity")
def list_activity(
    limit: int = Query(default=50, ge=1, le=300),
    kind: str | None = None,
    db: Session = Depends(get_db),
) -> dict:
    stmt = select(ActivityLog)
    if kind:
        stmt = stmt.where(ActivityLog.kind.like(f"{kind}%"))
    entries = list(db.scalars(stmt.order_by(ActivityLog.created_at.desc()).limit(limit)))
    return {"items": [ActivityOut.model_validate(e) for e in entries], "total": len(entries)}


@router.get("/shortcuts", response_model=list[ShortcutOut])
def list_shortcuts(db: Session = Depends(get_db)) -> list[ShortcutOut]:
    shortcuts = list(db.scalars(select(AppShortcut).order_by(AppShortcut.name.asc())))
    return [ShortcutOut.model_validate(s) for s in shortcuts]


@router.post("/shortcuts", response_model=ShortcutOut, status_code=201)
def create_shortcut(payload: ShortcutCreate, db: Session = Depends(get_db)) -> ShortcutOut:
    shortcut = AppShortcut(
        id=new_id(),
        name=payload.name,
        kind=payload.kind,
        target=payload.target,
        args=payload.args,
        approved=payload.approved,
    )
    db.add(shortcut)
    activity_service.log(
        db,
        "shortcut.created",
        f"Registered shortcut “{shortcut.name}”",
        detail={"kind": shortcut.kind, "target": shortcut.target, "approved": shortcut.approved},
        entity_type="shortcut",
        entity_id=shortcut.id,
    )
    db.commit()
    db.refresh(shortcut)
    return ShortcutOut.model_validate(shortcut)


@router.patch("/shortcuts/{shortcut_id}", response_model=ShortcutOut)
def update_shortcut(
    shortcut_id: str, payload: ShortcutCreate, db: Session = Depends(get_db)
) -> ShortcutOut:
    shortcut = db.get(AppShortcut, shortcut_id)
    if shortcut is None:
        from fastapi import HTTPException

        raise HTTPException(404, "Shortcut not found")
    for key, value in payload.model_dump().items():
        setattr(shortcut, key, value)
    db.commit()
    db.refresh(shortcut)
    return ShortcutOut.model_validate(shortcut)


@router.delete("/shortcuts/{shortcut_id}", response_model=MessageResponse)
def delete_shortcut(shortcut_id: str, db: Session = Depends(get_db)) -> MessageResponse:
    shortcut = db.get(AppShortcut, shortcut_id)
    if shortcut is not None:
        db.delete(shortcut)
        db.commit()
    return MessageResponse(message="Shortcut removed")
