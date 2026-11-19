from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.datetime_utils import normalize_dt
from app.core.timeutils import get_zone, utcnow
from app.models.automation import Workflow
from app.repositories.tasks import TaskRepository
from app.schemas.calendar import JournalBlock, JournalUpsert
from app.schemas.core import (
    EventCreate,
    EventUpdate,
    FocusCreate,
    HabitCreate,
    NoteCreate,
    ReminderCreate,
    TaskCreate,
    TaskUpdate,
)
from app.services import (
    activity_service,
    calendar_service,
    focus_service,
    habit_service,
    note_service,
    reminder_service,
    spotify_service,
    system_service,
    task_service,
    weather_service,
)
from app.services.dashboard_service import build_dashboard, build_insights
from app.services.settings_service import get_settings


class ToolError(ValueError):
    pass


@dataclass
class ToolSpec:
    name: str
    description: str
    params: type[BaseModel]
    handler: Callable[[Session, BaseModel], dict[str, Any]]
    requires_approval: bool = False
    category: str = "read"


# --------------------------------------------------------------------- params


class CreateTaskParams(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str | None = None
    priority: Literal["low", "medium", "high", "critical"] = "medium"
    due_at: str | None = Field(default=None, description="ISO-8601 datetime")
    due_in_minutes: int | None = Field(default=None, ge=1, le=525_600)
    category: str | None = None
    estimated_minutes: int | None = Field(default=None, ge=1, le=1440)
    tags: list[str] = Field(default_factory=list)


class UpdateTaskParams(BaseModel):
    task_id: str
    status: Literal["todo", "in_progress", "blocked", "done", "cancelled"] | None = None
    priority: Literal["low", "medium", "high", "critical"] | None = None
    due_at: str | None = None
    title: str | None = None


class TaskIdParams(BaseModel):
    task_id: str


class CreateReminderParams(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    remind_at: str | None = None
    in_minutes: int | None = Field(default=None, ge=1, le=525_600)
    repeat: Literal["none", "daily", "weekly", "monthly", "weekdays", "custom"] = "none"
    notes: str | None = None


class ListTasksParams(BaseModel):
    status: list[Literal["todo", "in_progress", "blocked", "done", "cancelled"]] | None = None
    priority: list[Literal["low", "medium", "high", "critical"]] | None = None
    limit: int = Field(default=10, ge=1, le=50)


class SearchTasksParams(BaseModel):
    query: str = Field(min_length=1, max_length=160)


class CreateNoteParams(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    content: str = ""
    category: str | None = None
    tags: list[str] = Field(default_factory=list)


class SearchNotesParams(BaseModel):
    query: str = Field(min_length=1, max_length=160)


class StartFocusParams(BaseModel):
    minutes: int = Field(default=25, ge=1, le=240)
    label: str = Field(default="Focus", max_length=160)


class CreateHabitParams(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    cadence: Literal["daily", "weekly"] = "daily"
    target_per_period: int = Field(default=1, ge=1, le=50)


class InsightsParams(BaseModel):
    range_days: int = Field(default=7, ge=1, le=90)


class OpenAppParams(BaseModel):
    name: str = Field(min_length=1, max_length=160, description="Approved shortcut name")
    args: list[str] = Field(default_factory=list)


class OpenProjectParams(BaseModel):
    path: str = Field(min_length=3, max_length=2048)


class RunWorkflowParams(BaseModel):
    workflow_id: str


class RescheduleParams(BaseModel):
    task_id: str
    due_at: str


class LaunchApplicationParams(BaseModel):
    application: str = Field(
        min_length=1,
        max_length=160,
        description=(
            "Application name such as 'Notepad' or 'Spotify'. Common apps launch "
            "without setup; other apps must be approved in Desktop Controls."
        ),
    )


class OpenWebsiteParams(BaseModel):
    url: str = Field(
        min_length=3,
        max_length=2048,
        description="Website address. The host must already be approved in Desktop Controls.",
    )


class SearchWebParams(BaseModel):
    query: str = Field(min_length=1, max_length=200)
    engine: Literal["google", "bing", "duckduckgo"] = "google"


class SpotifyControlParams(BaseModel):
    action: Literal[
        "status",
        "play",
        "pause",
        "resume",
        "next",
        "previous",
        "volume",
        "search",
        "devices",
        "open",
    ] = "status"
    query: str | None = Field(default=None, max_length=200, description="Track or search phrase")
    volume: int | None = Field(default=None, ge=0, le=100)
    device_id: str | None = Field(default=None, max_length=160)


class SystemVolumeParams(BaseModel):
    action: Literal["set", "up", "down", "mute", "unmute", "toggle", "status"] = "status"
    level: int | None = Field(default=None, ge=0, le=100)
    step: int = Field(default=10, ge=1, le=50)


class TimezoneParams(BaseModel):
    timezone: str | None = Field(default=None, max_length=64)


class ListRemindersParams(BaseModel):
    limit: int = Field(default=10, ge=1, le=50)


class CancelReminderParams(BaseModel):
    reminder_id: str | None = Field(default=None, max_length=36)
    title: str | None = Field(default=None, max_length=300)


# ------------------------------------------------------------------- handlers


def _iso(value: str | None, tz: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ToolError(f"'{value}' is not a valid ISO-8601 datetime") from exc
    return normalize_dt(parsed, tz)


def create_task_tool(db: Session, args: CreateTaskParams) -> dict[str, Any]:
    tz = get_settings(db).timezone
    due = _iso(args.due_at, tz)
    if args.due_in_minutes:
        due = utcnow() + timedelta(minutes=args.due_in_minutes)
    task = task_service.create_task(
        db,
        TaskCreate(
            title=args.title,
            description=args.description,
            priority=args.priority,
            due_at=due,
            category=args.category,
            estimated_minutes=args.estimated_minutes,
            tags=args.tags,
        ),
        source="assistant",
    )
    db.commit()
    return {
        "ok": True,
        "message": f"Created task “{task.title}”"
        + (f" due {task.due_at.isoformat()}" if task.due_at else ""),
        "data": {"task_id": task.id, "title": task.title},
    }


def update_task_tool(db: Session, args: UpdateTaskParams) -> dict[str, Any]:
    tz = get_settings(db).timezone
    due = _iso(args.due_at, tz)
    task = task_service.update_task(
        db,
        args.task_id,
        TaskUpdate(
            status=args.status,
            priority=args.priority,
            due_at=due,
            title=args.title,
        ),
    )
    db.commit()
    return {
        "ok": True,
        "message": f"Updated task “{task.title}” (status: {task.status})",
        "data": {"task_id": task.id},
    }


def complete_task_tool(db: Session, args: TaskIdParams) -> dict[str, Any]:
    task = task_service.update_task(db, args.task_id, TaskUpdate(status="done"))
    db.commit()
    return {
        "ok": True,
        "message": f"Marked “{task.title}” as done.",
        "data": {"task_id": task.id},
    }


def delete_task_tool(db: Session, args: TaskIdParams) -> dict[str, Any]:
    task = task_service.get_task(db, args.task_id)
    title = task.title
    task_service.delete_task(db, args.task_id)
    db.commit()
    return {"ok": True, "message": f"Deleted task “{title}”.", "data": {"task_id": args.task_id}}


def list_tasks_tool(db: Session, args: ListTasksParams) -> dict[str, Any]:
    items, total = TaskRepository(db).list(
        status=args.status or ["todo", "in_progress", "blocked"],
        priority=args.priority,
        sort="priority",
        limit=args.limit,
    )
    return {
        "ok": True,
        "message": f"Found {total} matching tasks.",
        "data": [
            {
                "id": t.id,
                "title": t.title,
                "status": t.status,
                "priority": t.priority,
                "due_at": t.due_at.isoformat() if t.due_at else None,
            }
            for t in items
        ],
    }


def search_tasks_tool(db: Session, args: SearchTasksParams) -> dict[str, Any]:
    items, _ = TaskRepository(db).list(search=args.query, limit=10)
    return {
        "ok": True,
        "message": f"Found {len(items)} tasks matching “{args.query}”.",
        "data": [{"id": t.id, "title": t.title, "status": t.status} for t in items],
    }


def create_reminder_tool(db: Session, args: CreateReminderParams) -> dict[str, Any]:
    tz = get_settings(db).timezone
    if args.remind_at:
        remind_at = _iso(args.remind_at, tz)
    elif args.in_minutes:
        remind_at = utcnow() + timedelta(minutes=args.in_minutes)
    else:
        raise ToolError("A reminder needs remind_at or in_minutes.")
    assert remind_at is not None
    reminder = reminder_service.create_reminder(
        db,
        ReminderCreate(
            title=args.title,
            notes=args.notes,
            remind_at=remind_at,
            repeat=args.repeat,
            timezone=tz,
        ),
    )
    db.commit()
    return {
        "ok": True,
        "message": f"Scheduled reminder “{reminder.title}” for {reminder.remind_at.isoformat()}.",
        "data": {"reminder_id": reminder.id, "remind_at": reminder.remind_at.isoformat()},
    }


def create_note_tool(db: Session, args: CreateNoteParams) -> dict[str, Any]:
    note = note_service.create_note(
        db,
        NoteCreate(
            title=args.title,
            content=args.content,
            category=args.category,
            tags=args.tags,
        ),
    )
    db.commit()
    return {"ok": True, "message": f"Saved note “{note.title}”.", "data": {"note_id": note.id}}


def search_notes_tool(db: Session, args: SearchNotesParams) -> dict[str, Any]:
    notes, total = note_service.search_notes(db, query=args.query, limit=8)
    return {
        "ok": True,
        "message": f"Found {total} notes matching “{args.query}”.",
        "data": [{"id": n.id, "title": n.title, "excerpt": (n.content or "")[:220]} for n in notes],
    }


def start_focus_tool(db: Session, args: StartFocusParams) -> dict[str, Any]:
    session = focus_service.start_session(
        db, FocusCreate(label=args.label, planned_minutes=args.minutes)
    )
    db.commit()
    return {
        "ok": True,
        "message": f"Started a {args.minutes}-minute focus session: {session.label}.",
        "data": {"focus_id": session.id},
    }


def create_habit_tool(db: Session, args: CreateHabitParams) -> dict[str, Any]:
    habit = habit_service.create_habit(
        db,
        HabitCreate(
            name=args.name,
            cadence=args.cadence,
            target_per_period=args.target_per_period,
        ),
    )
    db.commit()
    return {"ok": True, "message": f"Created habit “{habit.name}”.", "data": {"habit_id": habit.id}}


def dashboard_tool(db: Session, _: BaseModel) -> dict[str, Any]:
    dashboard = build_dashboard(db)
    return {
        "ok": True,
        "message": "Dashboard data retrieved.",
        "data": dashboard.model_dump(mode="json"),
    }


def insights_tool(db: Session, args: InsightsParams) -> dict[str, Any]:
    insights = build_insights(db, range_days=args.range_days)
    return {
        "ok": True,
        "message": f"Insights for the last {args.range_days} days retrieved.",
        "data": insights.model_dump(mode="json"),
    }


def list_workflows_tool(db: Session, _: BaseModel) -> dict[str, Any]:
    workflows = list(db.scalars(select(Workflow).order_by(Workflow.name.asc())))
    return {
        "ok": True,
        "message": f"Found {len(workflows)} workflows.",
        "data": [
            {
                "id": w.id,
                "name": w.name,
                "trigger": w.trigger_type,
                "enabled": w.enabled,
                "next_run_at": w.next_run_at.isoformat() if w.next_run_at else None,
            }
            for w in workflows
        ],
    }


def _desktop_tool(
    db: Session, action: str, target: str, args: list[str] | None = None
) -> dict[str, Any]:
    from app.services.desktop_bridge import desktop_hub

    response = desktop_hub.request_sync(action, target=target, args=args or [])
    if not response.get("ok"):
        raise ToolError(str(response.get("detail") or "Desktop action failed"))
    return {
        "ok": True,
        "message": str(response.get("detail") or "Desktop action completed."),
        "data": {"action": action, "target": target},
    }


def open_app_tool(db: Session, args: OpenAppParams) -> dict[str, Any]:
    from app.models.settings import AppShortcut

    shortcut = db.scalar(
        select(AppShortcut).where(func.lower(AppShortcut.name) == args.name.lower())
    )
    if shortcut is None:
        raise ToolError(
            f"No registered shortcut named “{args.name}”. Add it in Settings → Desktop shortcuts."
        )
    if not shortcut.approved:
        raise ToolError(
            f"The shortcut “{shortcut.name}” is not approved for the assistant. "
            "Approve it in Desktop Controls and add its target to the Windows allowlist."
        )
    action = (
        "open_in_vscode"
        if shortcut.kind == "project"
        else (
            "open_url"
            if shortcut.kind == "url"
            else (
                "open_folder"
                if shortcut.kind == "folder"
                else ("run_script" if shortcut.kind == "script" else "launch_app")
            )
        )
    )
    return _desktop_tool(db, action, shortcut.target, list(shortcut.args or []))


def open_project_tool(db: Session, args: OpenProjectParams) -> dict[str, Any]:
    return _desktop_tool(db, "open_in_vscode", args.path)


def run_workflow_tool(db: Session, args: RunWorkflowParams) -> dict[str, Any]:
    from app.automation import workflow_service

    execution = workflow_service.run_workflow(db, args.workflow_id)
    if execution is None:
        raise ToolError("Workflow execution was skipped.")
    return {
        "ok": execution.status == "succeeded",
        "message": f"Workflow finished with status: {execution.status}.",
        "data": {"execution_id": execution.id, "status": execution.status},
    }


def reschedule_tool(db: Session, args: RescheduleParams) -> dict[str, Any]:
    tz = get_settings(db).timezone
    due = _iso(args.due_at, tz)
    if due is None:
        raise ToolError("due_at is required")
    task = task_service.reschedule(db, args.task_id, due)
    db.commit()
    due_label = task.due_at.isoformat() if task.due_at else "no date"
    return {
        "ok": True,
        "message": f"Rescheduled “{task.title}” to {due_label}.",
        "data": {"task_id": task.id},
    }


def _spotify_unavailable(
    db: Session,
    action: str,
    query: str | None,
    exc: spotify_service.SpotifyError,
) -> dict[str, Any]:
    """Open the local Spotify UI or send a media key when the API cannot act.

    The fallback never claims verified playback. It only reports what the
    Windows bridge confirmed (or that the Spotify app was asked to open).
    """
    from app.services.desktop_bridge import desktop_hub

    settings = get_settings(db)
    reason = str(exc)
    if exc.code in {"clarification", "invalid_request"}:
        # Ambiguity must never be papered over with a blind media-key fallback.
        raise ToolError(reason)
    if not settings.spotify_fallback_enabled:
        raise ToolError(
            f"{reason} {exc.fallback or 'Reconnect Spotify or use the Spotify app directly.'}"
        )
    if action in {"play", "pause", "resume", "next", "previous", "open"}:
        if query and action in {"play", "open"}:
            result = desktop_hub.request_sync("spotify_search", target=query)
            if result.get("ok"):
                return {
                    "ok": True,
                    "message": (
                        f"{reason} I opened Spotify search for “{query}”. "
                        "Choose the track and press play — playback is not verified."
                    ),
                    "data": {
                        "fallback": "spotify_search",
                        "verified_playback": False,
                        "reason": exc.code,
                    },
                }
            raise ToolError(
                f"{reason} The Spotify search fallback also failed: {result.get('detail')}"
            )
        key = {
            "play": "play_pause",
            "resume": "play_pause",
            "pause": "play_pause",
            "next": "next",
            "previous": "previous",
            "open": "play_pause",
        }[action]
        result = desktop_hub.request_sync("media_control", target=key)
        if result.get("ok"):
            return {
                "ok": True,
                "message": (
                    f"{reason} I sent a Windows media key ({key.replace('_', ' ')}), but the "
                    "Spotify player state could not be verified."
                ),
                "data": {
                    "fallback": "media_key",
                    "verified_playback": False,
                    "reason": exc.code,
                    "key": key,
                },
            }
        raise ToolError(
            f"{reason} The Windows media-key fallback also failed: {result.get('detail')}"
        )
    if action == "search" and query:
        result = desktop_hub.request_sync("spotify_search", target=query)
        if result.get("ok"):
            return {
                "ok": True,
                "message": (
                    f"{reason} I opened Spotify search for “{query}”; results are not read back "
                    "from the app."
                ),
                "data": {"fallback": "spotify_search", "verified_playback": False},
            }
    raise ToolError(f"{reason} {exc.fallback or ''}".strip())


def control_spotify_tool(db: Session, args: SpotifyControlParams) -> dict[str, Any]:
    action = args.action
    try:
        if action == "status":
            state = spotify_service.playback(db)
            track = state.get("track") or {}
            if not state.get("active"):
                return {
                    "ok": True,
                    "message": "Spotify is connected but has no active playback.",
                    "data": state,
                }
            verb = "playing" if state.get("is_playing") else "paused"
            message = (
                f"Spotify is {verb} “{track.get('name')}” by {track.get('artist')}."
                if track.get("name")
                else f"Spotify is {verb}."
            )
            return {"ok": True, "message": message, "data": state}
        if action == "play":
            if not args.query and not args.device_id:
                raise spotify_service.SpotifyError(
                    "What would you like me to play?",
                    code="clarification",
                    fallback="Name a track, artist, or album.",
                )
            result = spotify_service.play(
                db, query=args.query, device_id=args.device_id
            )
            return {"ok": True, "message": result["message"], "data": result}
        if action == "pause":
            result = spotify_service.pause(db)
            return {"ok": True, "message": result["message"], "data": result}
        if action == "resume":
            result = spotify_service.resume(db)
            return {"ok": True, "message": result["message"], "data": result}
        if action in {"next", "previous"}:
            result = spotify_service.skip(db, direction=action)
            return {"ok": True, "message": result["message"], "data": result}
        if action == "volume":
            if args.volume is None:
                raise ToolError("What Spotify volume level would you like, from 0 to 100?")
            result = spotify_service.set_volume(db, args.volume)
            return {"ok": True, "message": result["message"], "data": result}
        if action == "search":
            if not args.query:
                raise ToolError("What should I search for on Spotify?")
            result = spotify_service.search(db, query=args.query, limit=6)
            tracks = result["tracks"]
            names = ", ".join(track.get("name") or "Untitled" for track in tracks[:5])
            return {
                "ok": True,
                "message": (
                    f"Top Spotify matches for “{args.query}”: {names}."
                    if names
                    else f"No Spotify tracks matched “{args.query}”."
                ),
                "data": result,
            }
        if action == "devices":
            items = spotify_service.devices(db)
            names = ", ".join(
                f"{device.get('name')} ({device.get('type')})" for device in items
            )
            return {
                "ok": True,
                "message": (
                    f"Available Spotify devices: {names}."
                    if names
                    else "Spotify reported no available devices."
                ),
                "data": {"devices": items},
            }
        if action == "open":
            from app.services.desktop_bridge import desktop_hub

            result = desktop_hub.request_sync("launch_app", target="spotify")
            if result.get("ok"):
                return {
                    "ok": True,
                    "message": str(result.get("detail") or "Opened Spotify."),
                    "data": {"opened": True, "via": "desktop_app"},
                }
            fallback = desktop_hub.request_sync("spotify_search", target="")
            if fallback.get("ok"):
                return {
                    "ok": True,
                    "message": "Opened the Spotify web player. Playback is not verified.",
                    "data": {"opened": True, "via": "web_player"},
                }
            raise ToolError(
                f"Spotify could not be opened: {result.get('detail') or 'unknown error'}"
            )
        raise ToolError(f"Unsupported Spotify action: {action}")
    except spotify_service.SpotifyError as exc:
        return _spotify_unavailable(db, action, args.query, exc)


def launch_application_tool(db: Session, args: LaunchApplicationParams) -> dict[str, Any]:
    from app.models.settings import AppShortcut

    shortcut = db.scalar(
        select(AppShortcut).where(func.lower(AppShortcut.name) == args.application.lower())
    )
    if shortcut is not None:
        if not shortcut.approved:
            raise ToolError(
                f"“{shortcut.name}” is not approved for the assistant. "
                "Approve it in Desktop Controls first."
            )
        if shortcut.kind == "application":
            return _desktop_tool(db, "launch_app", shortcut.target, list(shortcut.args or []))
        if shortcut.kind == "url":
            return _desktop_tool(db, "open_url", shortcut.target)
        if shortcut.kind == "folder":
            return _desktop_tool(db, "open_folder", shortcut.target)
        if shortcut.kind == "project":
            return _desktop_tool(db, "open_in_vscode", shortcut.target)
        raise ToolError("That shortcut is a script, which the launch tool cannot execute.")
    # No registered shortcut: pass the alias to Electron, which resolves only
    # known common install locations and still enforces the approved-app list.
    return _desktop_tool(db, "launch_app", args.application)


def open_website_tool(db: Session, args: OpenWebsiteParams) -> dict[str, Any]:
    raw = args.url.strip()
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", raw):
        raw = f"https://{raw}"
    try:
        parsed = urlsplit(raw)
        host = parsed.hostname
    except ValueError as exc:
        raise ToolError("That website address is not valid.") from exc
    if parsed.scheme not in {"http", "https"} or not host:
        raise ToolError("Only http(s) website addresses can be opened.")
    if parsed.username or parsed.password:
        raise ToolError("Website addresses with embedded credentials are not allowed.")
    if len(raw) > 2048:
        raise ToolError("That website address is too long.")
    return _desktop_tool(db, "open_url", raw)


def search_web_tool(db: Session, args: SearchWebParams) -> dict[str, Any]:
    query = re.sub(r"\s+", " ", args.query).strip()
    if not query or any(ord(char) < 32 for char in query):
        raise ToolError("A search query is required.")
    return _desktop_tool(db, "search_web", query, [args.engine])


def system_status_tool(db: Session, _: TimezoneParams) -> dict[str, Any]:
    tz = get_settings(db).timezone
    status = system_service.system_status(tz)
    battery = status.get("battery") or {}
    volume = status.get("volume") or {}
    parts = [
        f"CPU {status.get('cpu_percent')}%",
        f"memory {status.get('memory_percent')}%",
        f"disk {status.get('disk_percent')}%",
    ]
    if battery.get("percent") is not None:
        parts.append(
            f"battery {battery.get('percent')}%"
            + (" (plugged in)" if battery.get("plugged") else "")
        )
    if volume.get("supported"):
        parts.append(
            f"system volume {volume.get('volume')}%"
            + (" muted" if volume.get("muted") else "")
        )
    network = status.get("network") or {}
    parts.append("network online" if network.get("online") else "network offline")
    return {
        "ok": True,
        "message": ", ".join(parts) + ".",
        "data": status,
    }


def current_time_tool(db: Session, args: TimezoneParams) -> dict[str, Any]:
    tz = args.timezone or get_settings(db).timezone
    value = system_service.current_time(tz)
    return {
        "ok": True,
        "message": f"It is {value['clock']} on {value['date']} ({value['timezone']}).",
        "data": value,
    }


def system_volume_tool(db: Session, args: SystemVolumeParams) -> dict[str, Any]:
    try:
        if args.action == "status":
            state = system_service.read_volume()
            return {
                "ok": True,
                "message": (
                    f"System volume is {state['volume']}%"
                    + (" and muted." if state["muted"] else ".")
                ),
                "data": state,
            }
        current = system_service.read_volume()
        level = current["volume"]
        muted = current["muted"]
        if args.action == "set":
            if args.level is None:
                raise ToolError("What volume level would you like, from 0 to 100?")
            level = args.level
            muted = False
        elif args.action == "up":
            level = min(100, level + args.step)
        elif args.action == "down":
            level = max(0, level - args.step)
        elif args.action == "mute":
            muted = True
        elif args.action == "unmute":
            muted = False
        elif args.action == "toggle":
            muted = not muted
        state = system_service.set_volume(level, muted)
        return {
            "ok": True,
            "message": (
                f"System volume is now {state['volume']}%"
                + (" and muted." if state["muted"] else ".")
            ),
            "data": state,
        }
    except system_service.VolumeUnavailable as exc:
        raise ToolError(str(exc)) from exc
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


def list_reminders_tool(db: Session, args: ListRemindersParams) -> dict[str, Any]:
    reminders, total = reminder_service.list_reminders(db, limit=args.limit)
    data = [
        {
            "id": reminder.id,
            "title": reminder.title,
            "remind_at": reminder.remind_at.isoformat(),
            "snooze_until": reminder.snooze_until.isoformat() if reminder.snooze_until else None,
            "status": reminder.status,
            "repeat": reminder.repeat,
        }
        for reminder in reminders
    ]
    if not reminders:
        return {"ok": True, "message": "No reminders are scheduled.", "data": []}
    lines = [f"{reminder.title} — {reminder.remind_at.isoformat()}" for reminder in reminders[:6]]
    return {
        "ok": True,
        "message": f"{total} upcoming reminder(s): " + "; ".join(lines) + ".",
        "data": data,
    }


def cancel_reminder_tool(db: Session, args: CancelReminderParams) -> dict[str, Any]:
    from app.models.reminder import Reminder

    reminder: Reminder | None = None
    if args.reminder_id:
        reminder = db.get(Reminder, args.reminder_id)
        if reminder is None:
            raise ToolError("No reminder with that id exists.")
    elif args.title:
        matches = list(
            db.scalars(
                select(Reminder)
                .where(
                    Reminder.status.in_(["scheduled", "snoozed"]),
                    func.lower(Reminder.title).contains(args.title.lower()),
                )
                .order_by(Reminder.remind_at.asc())
                .limit(6)
            )
        )
        if not matches:
            raise ToolError(f"No scheduled reminder matched “{args.title}”.")
        if len(matches) > 1:
            options = "; ".join(f"“{item.title}” ({item.id})" for item in matches)
            raise ToolError(
                f"More than one reminder matches. Which one should I cancel: {options}?"
            )
        reminder = matches[0]
    else:
        raise ToolError("Which reminder should I cancel? Provide a title or reminder id.")
    if reminder.status in {"cancelled", "dismissed", "fired"}:
        raise ToolError(f"Reminder “{reminder.title}” is already {reminder.status}.")
    reminder.status = "cancelled"
    reminder.snooze_until = None
    activity_service.log(
        db,
        "reminder.cancelled",
        f"Cancelled reminder “{reminder.title}” via the assistant",
        entity_type="reminder",
        entity_id=reminder.id,
    )
    db.commit()
    return {
        "ok": True,
        "message": f"Cancelled reminder “{reminder.title}”.",
        "data": {"reminder_id": reminder.id, "title": reminder.title},
    }


def weather_tool(db: Session, _: EmptyParams) -> dict[str, Any]:
    value = weather_service.get_weather(db)
    if not value.get("available"):
        raise ToolError(value.get("note") or "Weather is unavailable.")
    return {
        "ok": True,
        "message": (
            f"{value.get('condition')} and {value.get('temperature')}°C in "
            f"{(value.get('location') or {}).get('label')}, high {value.get('high')}°C, "
            f"low {value.get('low')}°C."
        ),
        "data": value,
    }


class EmptyParams(BaseModel):
    pass


# ------------------------------------------------------------- calendar params


class CalendarDateParams(BaseModel):
    date: str | None = Field(default=None, description="ISO date (YYYY-MM-DD), defaults to today")


class CalendarRangeParams(BaseModel):
    start_date: str | None = Field(default=None, description="ISO date (YYYY-MM-DD)")
    end_date: str | None = Field(default=None, description="ISO date (YYYY-MM-DD)")


class CalendarMonthParams(BaseModel):
    month: int = Field(ge=1, le=12)
    year: int | None = Field(default=None, ge=1900, le=2200)


class CalendarJournalParams(BaseModel):
    date: str = Field(description="ISO date (YYYY-MM-DD)")


class CalendarJournalSearchParams(BaseModel):
    query: str = Field(min_length=1, max_length=200)
    start_date: str | None = None
    end_date: str | None = None


class CalendarEventsParams(BaseModel):
    start_date: str | None = None
    end_date: str | None = None


class CalendarTasksParams(BaseModel):
    start_date: str | None = None
    end_date: str | None = None
    status: list[Literal["todo", "in_progress", "blocked", "done", "cancelled"]] | None = None


class CalendarPendingTasksParams(BaseModel):
    limit: int = Field(default=30, ge=1, le=100)


class CalendarCompletedTasksParams(BaseModel):
    start_date: str | None = None
    end_date: str | None = None


class CalendarUpcomingEventsParams(BaseModel):
    days: int = Field(default=30, ge=1, le=365)
    limit: int = Field(default=20, ge=1, le=100)


class CalendarCreateEventParams(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    start_at: str = Field(description="ISO-8601 start datetime")
    end_at: str | None = Field(default=None, description="ISO-8601 end datetime")
    description: str | None = None
    location: str | None = None
    category: str | None = None
    priority: Literal["low", "medium", "high", "critical"] = "medium"
    color: str | None = None
    all_day: bool = False
    reminder_minutes: int | None = Field(default=None, ge=0, le=43_200)
    recurrence: Literal["none", "daily", "weekdays", "weekly", "monthly"] = "none"


class CalendarUpdateEventParams(BaseModel):
    event_id: str
    title: str | None = None
    description: str | None = None
    start_at: str | None = None
    end_at: str | None = None
    location: str | None = None
    category: str | None = None
    priority: Literal["low", "medium", "high", "critical"] | None = None
    status: Literal["confirmed", "tentative", "cancelled"] | None = None
    color: str | None = None
    reminder_minutes: int | None = Field(default=None, ge=0, le=43_200)
    clear_reminder: bool = False


class CalendarDeleteEventParams(BaseModel):
    event_id: str
    delete_series: bool = False


class CalendarCreateJournalParams(BaseModel):
    date: str = Field(description="ISO date (YYYY-MM-DD)")
    content: str = Field(min_length=1, max_length=20_000)
    title: str | None = None
    mood: str | None = None
    highlight: str | None = None


class CalendarDeleteJournalParams(BaseModel):
    date: str = Field(description="ISO date (YYYY-MM-DD)")


# ----------------------------------------------------------- calendar helpers


def _tool_date(value: str | None, tz: str, *, default: date | None = None) -> date:
    if value:
        try:
            return date.fromisoformat(value[:10])
        except ValueError as exc:
            raise ToolError(f"'{value}' is not a valid ISO date (YYYY-MM-DD)") from exc
    if default is not None:
        return default
    return datetime.now(get_zone(tz)).date()


def _tool_range(
    start_date: str | None,
    end_date: str | None,
    tz: str,
    *,
    default_days: int = 7,
) -> tuple[date, date]:
    today = datetime.now(get_zone(tz)).date()
    end = _tool_date(end_date, tz, default=today)
    start = _tool_date(start_date, tz, default=end - timedelta(days=default_days - 1))
    if end < start:
        raise ToolError("End date must be on or after the start date.")
    return start, end


def _clean_lines(text: str, limit: int) -> list[str]:
    lines = [line.strip(" -•\t") for line in (text or "").splitlines() if line.strip()]
    return lines[:limit]


def _daily_summary_message(data: dict[str, Any]) -> str:
    lines = [f"Calendar record for {data['date']} (timezone {data['timezone']})."]
    journal = data.get("journal")
    if journal and journal.get("recorded"):
        lines.append("Journal — activities you recorded:")
        for line in _clean_lines(journal.get("plain_text") or "", 12):
            lines.append(f"• {line[:220]}")
        if journal.get("highlight"):
            lines.append(f"Highlight: {journal['highlight'][:220]}")
        if journal.get("has_drawing"):
            lines.append("The page also contains a drawing.")
    else:
        lines.append("Journal: no activity was recorded for this date.")
    events = data.get("events_scheduled") or []
    if events:
        lines.append("Scheduled events (planned, not proof they happened):")
        for event in events[:10]:
            status_note = f" [{event['status']}]" if event.get("status") != "confirmed" else ""
            lines.append(
                f"• {event['title']} — {event['local_start'][11:16]}"
                f"{' (all day)' if event.get('all_day') else ''}{status_note}"
            )
    else:
        lines.append("Scheduled events: none.")
    completed = data.get("tasks_completed") or []
    if completed:
        lines.append("Tasks completed on this date:")
        for task in completed[:12]:
            lines.append(f"• {task['title']}")
    else:
        lines.append("Tasks completed on this date: none.")
    pending = data.get("tasks_pending") or []
    if pending:
        lines.append("Tasks still pending for this date:")
        for task in pending[:12]:
            lines.append(f"• {task['title']} ({task['status']}, {task['priority']} priority)")
    else:
        lines.append("Tasks still pending for this date: none.")
    if data.get("tasks_overdue"):
        lines.append(f"Overdue elsewhere: {len(data['tasks_overdue'])} task(s).")
    if not (journal and journal.get("recorded")) and not completed and not data.get("notes"):
        lines.append("No personal activity was recorded for this date.")
    return "\n".join(lines)


def _range_summary_message(data: dict[str, Any], label: str) -> str:
    totals = data.get("totals") or {}
    lines = [
        f"{label}: {data['start_date']} to {data['end_date']}.",
        (
            f"{totals.get('tasks_completed', 0)} tasks completed, "
            f"{totals.get('tasks_pending', 0)} still pending, "
            f"{totals.get('events', 0)} events scheduled, "
            f"{data.get('recorded_journal_days', 0)} journal day(s) recorded."
        ),
    ]
    journal_days = [item for item in (data.get("journal_days") or []) if item]
    if journal_days:
        lines.append("Journal activity recorded by you:")
        for entry in journal_days[:10]:
            excerpt = entry.get("summary") or entry.get("plain_text") or ""
            excerpt = " ".join(excerpt.split())[:160]
            if excerpt:
                lines.append(f"• {entry['date']}: {excerpt}")
    else:
        lines.append("Journal: no activity was recorded in this period.")
    completed = data.get("tasks_completed") or []
    if completed:
        lines.append("Completed tasks:")
        for task in completed[:12]:
            category = f" ({task['category']})" if task.get("category") else ""
            lines.append(f"• {task['title']}{category}")
    pending = data.get("tasks_pending") or []
    if pending:
        lines.append("Still pending from this period:")
        for task in pending[:12]:
            due = task.get("local_due") or "unscheduled"
            lines.append(f"• {task['title']} — {task['status']}, due {due}")
    events = data.get("events_scheduled") or []
    if events:
        lines.append("Scheduled events (planned, not confirmed as attended):")
        for event in events[:12]:
            lines.append(f"• {event['local_start'][:16]} — {event['title']}")
    if not journal_days and not completed and not pending and not events:
        lines.append("No activity was recorded and nothing was scheduled in this period.")
    return "\n".join(lines)


def _event_line(event: dict[str, Any]) -> str:
    when = event.get("local_start") or event.get("start_at") or ""
    suffix = f" at {event.get('location')}" if event.get("location") else ""
    return f"• {when[:16]} — {event['title']} [{event['status']}]{suffix}"


def calendar_daily_summary_tool(db: Session, args: CalendarDateParams) -> dict[str, Any]:
    tz = get_settings(db).timezone
    day = _tool_date(args.date, tz)
    data = calendar_service.daily_summary(db, day)
    return {
        "ok": True,
        "message": _daily_summary_message(data),
        "data": data,
    }


def calendar_weekly_summary_tool(db: Session, args: CalendarRangeParams) -> dict[str, Any]:
    tz = get_settings(db).timezone
    start, end = _tool_range(args.start_date, args.end_date, tz)
    data = calendar_service.range_summary(db, start, end)
    return {
        "ok": True,
        "message": _range_summary_message(data, "Weekly summary"),
        "data": data,
    }


def calendar_monthly_summary_tool(db: Session, args: CalendarMonthParams) -> dict[str, Any]:
    tz = get_settings(db).timezone
    year = args.year or datetime.now(get_zone(tz)).year
    data = calendar_service.monthly_summary(db, year, args.month)
    return {
        "ok": True,
        "message": _range_summary_message(data, f"Monthly summary — {year}-{args.month:02d}"),
        "data": data,
    }


def get_journal_entry_tool(db: Session, args: CalendarJournalParams) -> dict[str, Any]:
    tz = get_settings(db).timezone
    day = _tool_date(args.date, tz)
    entry = calendar_service.get_journal(db, day)
    if entry is None:
        return {
            "ok": True,
            "message": f"No journal entry was recorded for {day.isoformat()}.",
            "data": {"date": day.isoformat(), "journal": None},
        }
    data = calendar_service._journal_brief(entry)
    text = (entry.plain_text or "").strip()
    message = f"Journal entry for {day.isoformat()}"
    if entry.title:
        message += f": {entry.title}"
    message += ".\n" + (text[:6000] if text else "The page exists but has no written text.")
    if data and data.get("has_drawing"):
        message += "\nThe entry includes a drawing."
    return {"ok": True, "message": message, "data": data}


def search_journal_entries_tool(
    db: Session, args: CalendarJournalSearchParams
) -> dict[str, Any]:
    tz = get_settings(db).timezone
    start = _tool_date(args.start_date, tz) if args.start_date else None
    end = _tool_date(args.end_date, tz) if args.end_date else None
    results = calendar_service.search_journal(
        db, args.query, start_date=start, end_date=end, limit=20
    )
    if not results:
        return {
            "ok": True,
            "message": f"No journal entries matched “{args.query}”.",
            "data": [],
        }
    label = "entry" if len(results) == 1 else "entries"
    lines = [f"Found {len(results)} journal {label} for “{args.query}”:"]
    for item in results:
        lines.append(f"• {item.date.isoformat()} — {item.excerpt[:220]}")
    return {
        "ok": True,
        "message": "\n".join(lines),
        "data": [item.model_dump(mode="json") for item in results],
    }


def get_calendar_events_tool(db: Session, args: CalendarEventsParams) -> dict[str, Any]:
    tz = get_settings(db).timezone
    start, end = _tool_range(args.start_date, args.end_date, tz)
    range_start, range_end = calendar_service._range_utc(db, start, end)
    events = calendar_service.range_events(db, range_start, range_end)
    briefs = [calendar_service._event_brief(event, tz) for event in events]
    if not briefs:
        message = f"No events are scheduled between {start.isoformat()} and {end.isoformat()}."
    else:
        lines = [
            f"{len(briefs)} event(s) scheduled between {start.isoformat()} and {end.isoformat()}:"
        ]
        lines.extend(_event_line(event) for event in briefs[:30])
        message = "\n".join(lines)
    return {"ok": True, "message": message, "data": briefs}


def get_calendar_tasks_tool(db: Session, args: CalendarTasksParams) -> dict[str, Any]:
    tz = get_settings(db).timezone
    start, end = _tool_range(args.start_date, args.end_date, tz)
    range_start, range_end = calendar_service._range_utc(db, start, end)
    tasks = calendar_service.tasks_in_range(db, range_start, range_end)
    allowed = set(args.status or [])
    if allowed:
        tasks = [task for task in tasks if task.status in allowed]
    briefs = [calendar_service._task_brief(task, tz) for task in tasks]
    if not briefs:
        message = f"No tasks matched {start.isoformat()}–{end.isoformat()}."
    else:
        lines = [f"{len(briefs)} task(s) from {start.isoformat()} to {end.isoformat()}:"]
        for task in briefs[:30]:
            lines.append(
                f"• {task['title']} — {task['status']}, {task['priority']} priority"
                + (f", due {task['local_due']}" if task.get("local_due") else "")
            )
        message = "\n".join(lines)
    return {"ok": True, "message": message, "data": briefs}


def get_pending_tasks_tool(db: Session, args: CalendarPendingTasksParams) -> dict[str, Any]:
    tz = get_settings(db).timezone
    tasks = calendar_service.pending_tasks(db, limit=args.limit)
    briefs = [calendar_service._task_brief(task, tz) for task in tasks]
    if not briefs:
        return {"ok": True, "message": "There are no pending tasks.", "data": []}
    lines = [f"{len(briefs)} pending task(s):"]
    for task in briefs:
        due = f", due {task['local_due'][:16]}" if task.get("local_due") else ", no due date"
        lines.append(f"• {task['title']} — {task['status']}, {task['priority']} priority{due}")
    return {"ok": True, "message": "\n".join(lines), "data": briefs}


def get_completed_tasks_tool(
    db: Session, args: CalendarCompletedTasksParams
) -> dict[str, Any]:
    tz = get_settings(db).timezone
    start, end = _tool_range(args.start_date, args.end_date, tz)
    range_start, range_end = calendar_service._range_utc(db, start, end)
    tasks = calendar_service.completed_tasks(db, range_start, range_end)
    briefs = [calendar_service._task_brief(task, tz) for task in tasks]
    if not briefs:
        return {
            "ok": True,
            "message": (
                f"No tasks were completed between {start.isoformat()} "
                f"and {end.isoformat()}."
            ),
            "data": [],
        }
    lines = [f"{len(briefs)} completed task(s) between {start.isoformat()} and {end.isoformat()}:"]
    for task in briefs[:30]:
        lines.append(f"• {task['title']} — completed {task['local_completed'][:16]}")
    return {"ok": True, "message": "\n".join(lines), "data": briefs}


def get_upcoming_events_tool(
    db: Session, args: CalendarUpcomingEventsParams
) -> dict[str, Any]:
    tz = get_settings(db).timezone
    now = utcnow()
    events = calendar_service.range_events(
        db, now, now + timedelta(days=args.days)
    )[: args.limit]
    briefs = [calendar_service._event_brief(event, tz) for event in events]
    if not briefs:
        message = f"No events are scheduled in the next {args.days} day(s)."
    else:
        lines = [f"Next {len(briefs)} scheduled event(s):"]
        lines.extend(_event_line(event) for event in briefs)
        message = "\n".join(lines)
    return {"ok": True, "message": message, "data": briefs}


def get_productivity_summary_tool(
    db: Session, args: CalendarRangeParams
) -> dict[str, Any]:
    tz = get_settings(db).timezone
    start, end = _tool_range(args.start_date, args.end_date, tz)
    data = calendar_service.productivity_summary(db, start, end)
    totals = data["totals"]
    lines = [
        f"Productivity for {start.isoformat()} to {end.isoformat()}:",
        (
            f"• {totals['tasks_completed']} completed · {totals['tasks_pending']} pending · "
            f"{totals['tasks_overdue']} overdue"
        ),
        f"• Task completion rate: {totals['completion_rate']}%",
        f"• Productivity score: {totals['productivity_score']}%",
        f"• {totals['events']} events scheduled · {totals['journal_days']} journal day(s)",
        f"• Focus time: {totals['focus_minutes']} minute(s)",
    ]
    if data.get("categories"):
        top = ", ".join(
            f"{item['name']} ({item['count']})" for item in data["categories"][:5]
        )
        lines.append(f"• Top categories: {top}")
    return {"ok": True, "message": "\n".join(lines), "data": data}


def create_calendar_event_tool(
    db: Session, args: CalendarCreateEventParams
) -> dict[str, Any]:
    tz = get_settings(db).timezone
    start = _iso(args.start_at, tz)
    if start is None:
        raise ToolError("start_at is required")
    end = _iso(args.end_at, tz) if args.end_at else start + timedelta(hours=1)
    assert end is not None
    event = calendar_service.create_event(
        db,
        EventCreate(
            title=args.title,
            description=args.description,
            start_at=start,
            end_at=end,
            all_day=args.all_day,
            location=args.location,
            category=args.category,
            priority=args.priority,
            color=args.color,
            reminder_minutes=args.reminder_minutes,
            recurrence=args.recurrence,
        ),
    )
    db.commit()
    return {
        "ok": True,
        "message": (
            f"Created event “{event.title}” starting {args.start_at}"
            + (f" and repeating {event.recurrence}" if event.recurrence != "none" else "")
            + "."
        ),
        "data": {"event_id": event.id, "series_id": event.series_id},
    }


def update_calendar_event_tool(
    db: Session, args: CalendarUpdateEventParams
) -> dict[str, Any]:
    tz = get_settings(db).timezone
    payload: dict[str, Any] = {}
    for field in (
        "title",
        "description",
        "location",
        "category",
        "priority",
        "status",
        "color",
        "reminder_minutes",
    ):
        value = getattr(args, field)
        if value is not None:
            payload[field] = value
    if args.start_at:
        payload["start_at"] = _iso(args.start_at, tz)
    if args.end_at:
        payload["end_at"] = _iso(args.end_at, tz)
    if not payload and not args.clear_reminder:
        raise ToolError("No event fields were supplied.")
    event = calendar_service.update_event(
        db, args.event_id, EventUpdate(**payload, clear_reminder=args.clear_reminder)
    )
    db.commit()
    return {
        "ok": True,
        "message": f"Updated event “{event.title}”.",
        "data": {"event_id": event.id},
    }


def delete_calendar_event_tool(
    db: Session, args: CalendarDeleteEventParams
) -> dict[str, Any]:
    event = calendar_service.get_event(db, args.event_id)
    title = event.title
    count = calendar_service.delete_event(
        db, args.event_id, delete_series=args.delete_series
    )
    db.commit()
    suffix = (
        f" and {count - 1} related occurrence(s)." if count > 1 else "."
    )
    return {
        "ok": True,
        "message": f"Deleted event “{title}”{suffix}",
        "data": {"event_id": args.event_id, "deleted": count},
    }


def create_journal_entry_tool(
    db: Session, args: CalendarCreateJournalParams
) -> dict[str, Any]:
    tz = get_settings(db).timezone
    day = _tool_date(args.date, tz)
    existing = calendar_service.get_journal(db, day)
    paragraphs = [part.strip() for part in re.split(r"\n{2,}", args.content) if part.strip()]
    if not paragraphs:
        raise ToolError("The journal content is empty.")
    blocks = list(existing.blocks or []) if existing else []
    import html as html_lib

    for paragraph in paragraphs:
        text = paragraph.strip()[:30_000]
        blocks.append(
            JournalBlock(
                id=uuid.uuid4().hex,
                type="paragraph",
                html=html_lib.escape(text).replace("\n", "<br />"),
                text=text,
            ).model_dump(mode="json")
        )
    plain_text = "\n".join(
        ([existing.plain_text] if existing and existing.plain_text else [])
        + [part.strip() for part in paragraphs]
    )
    entry = calendar_service.upsert_journal(
        db,
        day,
        JournalUpsert(
            title=args.title or (existing.title if existing else ""),
            mood=args.mood or (existing.mood if existing else None),
            summary=existing.summary if existing else None,
            highlight=args.highlight or (existing.highlight if existing else None),
            blocks=blocks,
            plain_text=plain_text,
        ),
        source="assistant",
    )
    db.commit()
    return {
        "ok": True,
        "message": f"Saved {len(paragraphs)} journal block(s) for {day.isoformat()}.",
        "data": {"journal_id": entry.id, "date": day.isoformat()},
    }


def delete_journal_entry_tool(
    db: Session, args: CalendarDeleteJournalParams
) -> dict[str, Any]:
    tz = get_settings(db).timezone
    day = _tool_date(args.date, tz)
    calendar_service.delete_journal(db, day)
    db.commit()
    return {
        "ok": True,
        "message": f"Deleted the journal entry for {day.isoformat()}.",
        "data": {"date": day.isoformat()},
    }



TOOLS: dict[str, ToolSpec] = {
    "create_task": ToolSpec(
        "create_task",
        "Create a task in JARVIS. Use ISO-8601 for due_at in the user's time zone.",
        CreateTaskParams,
        create_task_tool,
    ),
    "update_task": ToolSpec(
        "update_task",
        "Update an existing task by id. Only supplied fields change.",
        UpdateTaskParams,
        update_task_tool,
    ),
    "complete_task": ToolSpec(
        "complete_task",
        "Mark a task as done by id.",
        TaskIdParams,
        complete_task_tool,
    ),
    "delete_task": ToolSpec(
        "delete_task",
        "Delete a task by id. Requires user approval.",
        TaskIdParams,
        delete_task_tool,
        requires_approval=True,
    ),
    "list_tasks": ToolSpec(
        "list_tasks",
        "List tasks from the database, filtered and ordered by priority.",
        ListTasksParams,
        list_tasks_tool,
        category="read",
    ),
    "search_tasks": ToolSpec(
        "search_tasks",
        "Search tasks by title or description.",
        SearchTasksParams,
        search_tasks_tool,
        category="read",
    ),
    "create_reminder": ToolSpec(
        "create_reminder",
        "Create a reminder. Provide remind_at (ISO-8601) or in_minutes.",
        CreateReminderParams,
        create_reminder_tool,
    ),
    "create_note": ToolSpec(
        "create_note",
        "Save a note to the personal knowledge library.",
        CreateNoteParams,
        create_note_tool,
    ),
    "search_notes": ToolSpec(
        "search_notes",
        "Search saved notes.",
        SearchNotesParams,
        search_notes_tool,
        category="read",
    ),
    "start_focus": ToolSpec(
        "start_focus",
        "Start a focus timer for a number of minutes.",
        StartFocusParams,
        start_focus_tool,
    ),
    "create_habit": ToolSpec(
        "create_habit",
        "Create a daily or weekly habit.",
        CreateHabitParams,
        create_habit_tool,
    ),
    "get_dashboard": ToolSpec(
        "get_dashboard",
        "Retrieve today's real dashboard data: tasks, reminders, events, habits, focus.",
        EmptyParams,
        dashboard_tool,
        category="read",
    ),
    "get_insights": ToolSpec(
        "get_insights",
        "Retrieve productivity metrics for a number of days.",
        InsightsParams,
        insights_tool,
        category="read",
    ),
    "list_workflows": ToolSpec(
        "list_workflows",
        "List automation workflows and whether they are enabled.",
        EmptyParams,
        list_workflows_tool,
        category="read",
    ),
    "open_application": ToolSpec(
        "open_application",
        "Open a registered, user-approved desktop application by shortcut name. Requires approval.",
        OpenAppParams,
        open_app_tool,
        requires_approval=True,
        category="desktop",
    ),
    "open_project": ToolSpec(
        "open_project",
        "Open an approved folder in VS Code. Requires approval.",
        OpenProjectParams,
        open_project_tool,
        requires_approval=True,
        category="desktop",
    ),
    "run_workflow": ToolSpec(
        "run_workflow",
        "Run an existing automation workflow by id. Requires approval.",
        RunWorkflowParams,
        run_workflow_tool,
        requires_approval=True,
        category="automation",
    ),
    "reschedule_task": ToolSpec(
        "reschedule_task",
        "Reschedule an existing task to a new ISO-8601 due date.",
        RescheduleParams,
        reschedule_tool,
    ),
    "launch_application": ToolSpec(
        "launch_application",
        (
            "Launch a Windows application by name, approved alias or registered shortcut. "
            "Common apps such as Notepad, Spotify, Chrome, Edge, Firefox, Brave, Explorer, "
            "VS Code and Office launch directly from their standard install locations. "
            "Other applications must first be approved in Settings → Desktop Controls; "
            "the Electron main process enforces that allowlist. No per-message approval is needed."
        ),
        LaunchApplicationParams,
        launch_application_tool,
        category="desktop",
    ),
    "open_website": ToolSpec(
        "open_website",
        "Open an http(s) website in the default browser. The hostname must be approved.",
        OpenWebsiteParams,
        open_website_tool,
        category="desktop",
    ),
    "search_web": ToolSpec(
        "search_web",
        "Search the web with a fixed, safe search engine and open the results in the browser.",
        SearchWebParams,
        search_web_tool,
        category="desktop",
    ),
    "control_spotify": ToolSpec(
        "control_spotify",
        (
            "Read or control Spotify: status, play a named track, pause, resume, next, "
            "previous, volume, search, devices, or open the app. Never claims playback "
            "unless the Spotify API confirms it."
        ),
        SpotifyControlParams,
        control_spotify_tool,
        category="desktop",
    ),
    "control_system_volume": ToolSpec(
        "control_system_volume",
        "Read, set, raise, lower, mute or unmute the Windows system volume (0–100).",
        SystemVolumeParams,
        system_volume_tool,
        category="desktop",
    ),
    "get_system_status": ToolSpec(
        "get_system_status",
        "Read live CPU, memory, disk, battery, network, volume and uptime status.",
        EmptyParams,
        system_status_tool,
        category="read",
    ),
    "get_current_time": ToolSpec(
        "get_current_time",
        "Get the current local date and time in the user's timezone.",
        TimezoneParams,
        current_time_tool,
        category="read",
    ),
    "list_reminders": ToolSpec(
        "list_reminders",
        "List upcoming scheduled and snoozed reminders with their ids.",
        ListRemindersParams,
        list_reminders_tool,
        category="read",
    ),
    "cancel_reminder": ToolSpec(
        "cancel_reminder",
        "Cancel an upcoming reminder by id or by unambiguous title. Requires user approval.",
        CancelReminderParams,
        cancel_reminder_tool,
        requires_approval=True,
    ),
    "get_weather": ToolSpec(
        "get_weather",
        "Read the configured local weather and hourly forecast.",
        EmptyParams,
        weather_tool,
        category="read",
    ),
    "get_daily_summary": ToolSpec(
        "get_daily_summary",
        (
            "Read one day's factual calendar history: written journal activities, scheduled "
            "events, completed tasks, pending tasks and notes. Never treat a scheduled event "
            "as something that happened."
        ),
        CalendarDateParams,
        calendar_daily_summary_tool,
        category="read",
    ),
    "get_weekly_summary": ToolSpec(
        "get_weekly_summary",
        (
            "Summarize journal activity, scheduled events, completed tasks and "
            "pending tasks for a date range."
        ),
        CalendarRangeParams,
        calendar_weekly_summary_tool,
        category="read",
    ),
    "get_monthly_summary": ToolSpec(
        "get_monthly_summary",
        "Summarize a calendar month from real journal, event and task records.",
        CalendarMonthParams,
        calendar_monthly_summary_tool,
        category="read",
    ),
    "get_journal_entry": ToolSpec(
        "get_journal_entry",
        "Read the structured journal entry recorded for one date.",
        CalendarJournalParams,
        get_journal_entry_tool,
        category="read",
    ),
    "search_journal_entries": ToolSpec(
        "search_journal_entries",
        "Search historical journal entries by keyword, with an optional date range.",
        CalendarJournalSearchParams,
        search_journal_entries_tool,
        category="read",
    ),
    "get_events": ToolSpec(
        "get_events",
        "List scheduled calendar events in a date range. Scheduling is not proof of attendance.",
        CalendarEventsParams,
        get_calendar_events_tool,
        category="read",
    ),
    "get_tasks": ToolSpec(
        "get_tasks",
        "List tasks due or completed in a date range, optionally filtered by status.",
        CalendarTasksParams,
        get_calendar_tasks_tool,
        category="read",
    ),
    "get_pending_tasks": ToolSpec(
        "get_pending_tasks",
        "List tasks that are not completed or cancelled, ordered by due date.",
        CalendarPendingTasksParams,
        get_pending_tasks_tool,
        category="read",
    ),
    "get_completed_tasks": ToolSpec(
        "get_completed_tasks",
        "List tasks verified as completed (completed_at) in a date range.",
        CalendarCompletedTasksParams,
        get_completed_tasks_tool,
        category="read",
    ),
    "get_upcoming_events": ToolSpec(
        "get_upcoming_events",
        "List scheduled events from now through the next N days.",
        CalendarUpcomingEventsParams,
        get_upcoming_events_tool,
        category="read",
    ),
    "get_productivity_summary": ToolSpec(
        "get_productivity_summary",
        "Read task completion, event, journal and focus metrics for a date range.",
        CalendarRangeParams,
        get_productivity_summary_tool,
        category="read",
    ),
    "create_calendar_event": ToolSpec(
        "create_calendar_event",
        "Create a calendar event. Use ISO-8601 datetimes in the user's local time.",
        CalendarCreateEventParams,
        create_calendar_event_tool,
    ),
    "update_calendar_event": ToolSpec(
        "update_calendar_event",
        "Update an existing calendar event by id. Only supplied fields change.",
        CalendarUpdateEventParams,
        update_calendar_event_tool,
    ),
    "delete_calendar_event": ToolSpec(
        "delete_calendar_event",
        "Delete an event, optionally its whole recurrence series. Requires approval.",
        CalendarDeleteEventParams,
        delete_calendar_event_tool,
        requires_approval=True,
    ),
    "create_journal_entry": ToolSpec(
        "create_journal_entry",
        "Append written journal content to a date's diary page.",
        CalendarCreateJournalParams,
        create_journal_entry_tool,
    ),
    "delete_journal_entry": ToolSpec(
        "delete_journal_entry",
        "Delete a date's journal page, including drawings and attachments. Requires approval.",
        CalendarDeleteJournalParams,
        delete_journal_entry_tool,
        requires_approval=True,
    ),
}


def tool_schemas() -> list[dict[str, Any]]:
    schemas = []
    for spec in TOOLS.values():
        schema = spec.params.model_json_schema()
        schema.pop("title", None)
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": schema,
                },
            }
        )
    return schemas


def validate_tool_call(name: str, arguments: dict[str, Any]) -> BaseModel:
    spec = TOOLS.get(name)
    if spec is None:
        raise ToolError(f"Unknown tool: {name}")
    try:
        return spec.params(**arguments)
    except ValidationError as exc:
        detail = exc.errors()[0]
        raise ToolError(f"{detail.get('loc')}: {detail.get('msg')}") from exc


def execute_tool(db: Session, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    spec = TOOLS.get(name)
    if spec is None:
        return {"ok": False, "message": f"Unknown tool: {name}", "data": None}
    try:
        parsed = validate_tool_call(name, arguments)
        result = spec.handler(db, parsed)
        return result
    except ToolError as exc:
        db.rollback()
        return {"ok": False, "message": str(exc), "data": None}
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        return {"ok": False, "message": f"Tool failed: {exc}", "data": None}


def register_tool(spec: ToolSpec) -> None:
    TOOLS[spec.name] = spec
