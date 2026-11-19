from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.orm import Session

from app.core.datetime_utils import normalize_dt
from app.core.timeutils import utcnow
from app.models.focus import FocusSession
from app.schemas.core import ReminderCreate, TaskCreate
from app.services import activity_service
from app.services.desktop_bridge import desktop_hub
from app.services.reminder_service import create_reminder
from app.services.settings_service import get_settings
from app.services.task_service import create_task

logger = logging.getLogger("jarvis.automation")


class ActionConfigError(ValueError):
    pass


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateTaskConfig(_Base):
    title: str = Field(min_length=1, max_length=300)
    description: str | None = None
    priority: Literal["low", "medium", "high", "critical"] = "medium"
    category: str | None = None
    due_in_minutes: int | None = Field(default=None, ge=1, le=60 * 24 * 365)
    tags: list[str] = Field(default_factory=list, max_length=20)


class CreateReminderConfig(_Base):
    title: str = Field(min_length=1, max_length=300)
    notes: str | None = None
    in_minutes: int | None = Field(default=None, ge=1, le=60 * 24 * 365)
    at: str | None = None
    repeat: Literal["none", "daily", "weekly", "monthly", "weekdays", "custom"] = "none"
    repeat_interval_minutes: int | None = Field(default=None, ge=5)


class NotifyConfig(_Base):
    title: str = Field(default="JARVIS", max_length=120)
    body: str = Field(min_length=1, max_length=500)


class StartFocusConfig(_Base):
    minutes: int = Field(default=25, ge=1, le=240)
    label: str = Field(default="Focus", max_length=160)
    break_minutes: int = Field(default=5, ge=0, le=60)


class OpenAppConfig(_Base):
    path: str = Field(min_length=3, max_length=2048)
    args: list[str] = Field(default_factory=list, max_length=20)


class OpenFolderConfig(_Base):
    path: str = Field(min_length=3, max_length=2048)


class RunScriptConfig(_Base):
    path: str = Field(min_length=3, max_length=2048)


class DailySummaryConfig(_Base):
    pass


ACTION_CONFIGS: dict[str, type[_Base]] = {
    "create_task": CreateTaskConfig,
    "create_reminder": CreateReminderConfig,
    "notify": NotifyConfig,
    "start_focus": StartFocusConfig,
    "open_app": OpenAppConfig,
    "open_folder": OpenFolderConfig,
    "run_script": RunScriptConfig,
    "daily_summary": DailySummaryConfig,
}

DESKTOP_ACTIONS = {"open_app", "open_folder", "run_script", "notify"}

ACTION_LABELS = {
    "create_task": "Create a task",
    "create_reminder": "Create a reminder",
    "notify": "Show a Windows notification",
    "start_focus": "Start a focus session",
    "open_app": "Open an approved application",
    "open_folder": "Open an approved folder",
    "run_script": "Run a registered script",
    "daily_summary": "Produce a daily summary",
}


def validate_action_config(action_type: str, config: dict[str, Any]) -> dict[str, Any]:
    model = ACTION_CONFIGS.get(action_type)
    if model is None:
        raise ActionConfigError(f"Unsupported action type: {action_type}")
    try:
        parsed = model(**config)
    except ValidationError as exc:
        first = exc.errors()[0]
        field = ".".join(str(p) for p in first.get("loc", [])) or "config"
        raise ActionConfigError(f"{field}: {first.get('msg')}") from exc
    return parsed.model_dump(exclude_none=True)


def _desktop(action: str, **kwargs: Any) -> dict[str, Any]:
    return desktop_hub.request_sync(action, **kwargs)


def execute_action(db: Session, action_type: str, config: dict[str, Any]) -> str:
    parsed = validate_action_config(action_type, config)
    settings = get_settings(db)

    if action_type == "create_task":
        due_at = None
        if parsed.get("due_in_minutes"):
            due_at = utcnow() + timedelta(minutes=int(parsed["due_in_minutes"]))
        task = create_task(
            db,
            TaskCreate(
                title=parsed["title"],
                description=parsed.get("description"),
                priority=parsed.get("priority", "medium"),
                category=parsed.get("category"),
                due_at=due_at,
                tags=parsed.get("tags") or [],
            ),
            source="automation",
        )
        return f"Created task “{task.title}”"

    if action_type == "create_reminder":
        if parsed.get("at"):
            remind_at = normalize_dt(datetime.fromisoformat(parsed["at"]), settings.timezone)
            assert remind_at is not None
        else:
            minutes = int(parsed.get("in_minutes") or 15)
            remind_at = utcnow() + timedelta(minutes=minutes)
        reminder = create_reminder(
            db,
            ReminderCreate(
                title=parsed["title"],
                notes=parsed.get("notes"),
                remind_at=remind_at,
                repeat=parsed.get("repeat", "none"),
                repeat_interval_minutes=parsed.get("repeat_interval_minutes"),
                timezone=settings.timezone,
            ),
        )
        return f"Scheduled reminder “{reminder.title}”"

    if action_type == "notify":
        response = _desktop("notify", payload={"title": parsed["title"], "body": parsed["body"]})
        if not response.get("ok"):
            raise RuntimeError(str(response.get("detail") or "Notification failed"))
        return "Notification displayed"

    if action_type == "start_focus":
        active = (
            db.query(FocusSession).filter(FocusSession.state.in_(["running", "paused"])).first()
        )
        if active:
            raise RuntimeError(f"A focus session is already active: “{active.label}”")
        session = FocusSession(
            label=parsed["label"],
            planned_minutes=parsed["minutes"],
            break_minutes=parsed.get("break_minutes", 5),
            state="running",
        )
        db.add(session)
        db.flush()
        return f"Started a {parsed['minutes']}-minute focus session"

    if action_type == "open_app":
        response = _desktop("launch_app", target=parsed["path"], args=parsed.get("args") or [])
        if not response.get("ok"):
            raise RuntimeError(str(response.get("detail") or "Application launch failed"))
        return str(response.get("detail"))

    if action_type == "open_folder":
        response = _desktop("open_folder", target=parsed["path"])
        if not response.get("ok"):
            raise RuntimeError(str(response.get("detail") or "Folder open failed"))
        return str(response.get("detail"))

    if action_type == "run_script":
        response = _desktop("run_script", target=parsed["path"])
        if not response.get("ok"):
            raise RuntimeError(str(response.get("detail") or "Script failed"))
        return str(response.get("detail"))

    if action_type == "daily_summary":
        from app.services.dashboard_service import build_dashboard

        dashboard = build_dashboard(db)
        summary = (
            f"{dashboard.counts.tasks_today} tasks due today, "
            f"{dashboard.counts.tasks_completed_today} completed, "
            f"{dashboard.counts.tasks_overdue} overdue."
        )
        activity_service.log(
            db, "summary.daily", "Daily summary generated", detail={"summary": summary}
        )
        response = _desktop(
            "notify",
            payload={"title": "JARVIS — Daily summary", "body": summary},
        )
        if not response.get("ok"):
            raise RuntimeError(
                str(response.get("detail") or "Could not display summary notification")
            )
        return summary

    raise ActionConfigError(f"Unsupported action type: {action_type}")
