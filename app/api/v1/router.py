from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.endpoints import (
    ai,
    calendar,
    data,
    desktop,
    focus,
    habits,
    notes,
    productivity,
    reminders,
    spotify,
    system,
    tasks,
    voice,
    workflows,
)
from app.api.v1.endpoints import (
    settings as settings_endpoints,
)

router = APIRouter(prefix="/api/v1")
router.include_router(tasks.router)
router.include_router(reminders.router)
router.include_router(calendar.router)
router.include_router(habits.router)
router.include_router(notes.router)
router.include_router(focus.router)
router.include_router(productivity.router)
router.include_router(settings_endpoints.router)
router.include_router(workflows.router)
router.include_router(ai.router)
router.include_router(desktop.router)
router.include_router(data.router)
router.include_router(system.router)
router.include_router(spotify.router)
router.include_router(voice.router)
