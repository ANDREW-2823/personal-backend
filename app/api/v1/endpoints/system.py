from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.security import require_token
from app.db.session import get_db
from app.services import system_service, weather_service

router = APIRouter(prefix="/system", tags=["system"], dependencies=[Depends(require_token)])


class VolumeIn(BaseModel):
    level: int | None = Field(default=None, ge=0, le=100)
    muted: bool | None = None


@router.get("/status")
def status(timezone: str | None = Query(default=None, max_length=64)) -> dict:
    return system_service.system_status(timezone)


@router.get("/time")
def time(timezone: str | None = Query(default=None, max_length=64)) -> dict:
    return system_service.current_time(timezone)


@router.get("/volume")
def volume() -> dict:
    try:
        return system_service.read_volume()
    except system_service.VolumeUnavailable as exc:
        return {"volume": None, "muted": None, "supported": False, "detail": str(exc)}


@router.put("/volume")
def set_volume(payload: VolumeIn) -> dict:
    try:
        return system_service.set_volume(payload.level, payload.muted)
    except system_service.VolumeUnavailable as exc:
        return {"volume": None, "muted": None, "supported": False, "detail": str(exc)}
    except ValueError as exc:
        from fastapi import HTTPException

        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/weather")
def weather(db: Session = Depends(get_db)) -> dict:
    return weather_service.get_weather(db)
