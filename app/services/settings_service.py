from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.config import settings as env_settings
from app.core.timeutils import get_zone, utcnow
from app.models.settings import AppSetting
from app.schemas.core import AppSettingsSchema, SettingsUpdate


def get_settings(db: Session) -> AppSettingsSchema:
    row = db.get(AppSetting, "app")
    defaults = AppSettingsSchema(timezone=env_settings.default_timezone or "UTC")
    if row is None or not isinstance(row.value, dict):
        return defaults
    merged = defaults.model_dump()
    merged.update({k: v for k, v in row.value.items() if k in merged})
    try:
        return AppSettingsSchema(**merged)
    except Exception:
        return defaults


def update_settings(db: Session, payload: SettingsUpdate) -> AppSettingsSchema:
    current = get_settings(db)
    merged = {**current.model_dump(), **payload.model_dump()}
    validated = AppSettingsSchema(**merged)
    # Validate the timezone actually resolves.
    get_zone(validated.timezone)
    row = db.get(AppSetting, "app")
    if row is None:
        row = AppSetting(key="app", value=validated.model_dump())
        db.add(row)
    else:
        row.value = validated.model_dump()
        row.updated_at = utcnow()
    db.flush()
    return validated
