from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.datetime_utils import normalize_dt
from app.core.timeutils import day_bounds_utc, next_occurrence, utcnow
from app.models.task import Task
from app.repositories.tasks import TaskRepository
from app.schemas.core import TaskCreate, TaskUpdate
from app.services import activity_service
from app.services.settings_service import get_settings


def _tz(db: Session) -> str:
    return get_settings(db).timezone


def create_task(db: Session, data: TaskCreate, *, source: str = "api") -> Task:
    tz = _tz(db)
    payload = data.model_copy(update={"due_at": normalize_dt(data.due_at, tz)})
    if payload.blocked_by and db.get(Task, payload.blocked_by) is None:
        raise HTTPException(422, "blocked_by task does not exist")
    repo = TaskRepository(db)
    task = repo.create(payload)
    activity_service.log(
        db,
        "task.created",
        f"Created task “{task.title}”",
        detail={"source": source, "priority": task.priority},
        entity_type="task",
        entity_id=task.id,
    )
    return task


def update_task(db: Session, task_id: str, data: TaskUpdate) -> Task:
    repo = TaskRepository(db)
    task = repo.get(task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")
    tz = _tz(db)
    # Only mark due_at as set when the caller actually supplied it. Passing
    # None through model_copy would add it to the update set and silently
    # clear an existing due date on unrelated patches.
    patch = data
    if data.due_at is not None:
        patch = data.model_copy(update={"due_at": normalize_dt(data.due_at, tz)})
    if patch.blocked_by:
        if patch.blocked_by == task_id:
            raise HTTPException(422, "A task cannot block itself")
        if db.get(Task, patch.blocked_by) is None:
            raise HTTPException(422, "blocked_by task does not exist")
    was_done = task.status == "done"
    task = repo.update(task, patch)
    if task.status == "done" and not was_done:
        activity_service.log(
            db,
            "task.completed",
            f"Completed task “{task.title}”",
            entity_type="task",
            entity_id=task.id,
        )
        _spawn_recurrence(db, task)
        _notify_workflows(db, task)
    elif task.status != "done" and was_done:
        activity_service.log(
            db,
            "task.reopened",
            f"Reopened task “{task.title}”",
            entity_type="task",
            entity_id=task.id,
        )
    return task


def _spawn_recurrence(db: Session, task: Task) -> None:
    if not task.recurrence:
        return
    base = task.due_at or utcnow()
    upcoming = next_occurrence(
        base,
        task.recurrence,
        timezone_name=_tz(db),
        after=utcnow(),
    )
    if not upcoming:
        return
    clone = Task(
        title=task.title,
        description=task.description,
        status="todo",
        priority=task.priority,
        due_at=upcoming,
        category=task.category,
        estimated_minutes=task.estimated_minutes,
        recurrence=task.recurrence,
    )
    clone.tag_objects = list(task.tag_objects)
    db.add(clone)
    db.flush()
    activity_service.log(
        db,
        "task.recurred",
        f"Scheduled next occurrence of “{task.title}”",
        detail={"due_at": upcoming.isoformat()},
        entity_type="task",
        entity_id=clone.id,
    )


def _notify_workflows(db: Session, task: Task) -> None:
    try:
        from app.automation.workflow_service import trigger_event

        trigger_event(
            db,
            "task_completed",
            {
                "task_id": task.id,
                "title": task.title,
                "priority": task.priority,
                "category": task.category,
                "source": "task",
            },
        )
    except Exception as exc:  # automation must never break task completion
        activity_service.log(
            db,
            "automation.trigger_failed",
            "Task-completion workflow trigger failed",
            detail={"error": str(exc)},
            entity_type="task",
            entity_id=task.id,
        )


def get_task(db: Session, task_id: str) -> Task:
    task = TaskRepository(db).get(task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")
    return task


def delete_task(db: Session, task_id: str) -> None:
    repo = TaskRepository(db)
    task = repo.get(task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")
    activity_service.log(
        db, "task.deleted", f"Deleted task “{task.title}”", entity_type="task", entity_id=task.id
    )
    repo.delete(task)


def reschedule(db: Session, task_id: str, due_at: datetime) -> Task:
    task = get_task(db, task_id)
    task.due_at = normalize_dt(due_at, _tz(db))
    if task.status == "done":
        task.status = "todo"
        task.completed_at = None
    db.flush()
    activity_service.log(
        db,
        "task.rescheduled",
        f"Rescheduled “{task.title}”",
        detail={"due_at": task.due_at.isoformat() if task.due_at else None},
        entity_type="task",
        entity_id=task.id,
    )
    return task


def today_summary(db: Session) -> dict[str, Any]:
    tz = _tz(db)
    start, end = day_bounds_utc(tz)
    open_statuses = ["todo", "in_progress", "blocked"]
    today_count = db.scalar(
        select(func.count())
        .select_from(Task)
        .where(Task.due_at >= start, Task.due_at < end, Task.status.in_(open_statuses))
    )
    overdue_count = db.scalar(
        select(func.count())
        .select_from(Task)
        .where(Task.due_at < utcnow(), Task.status.in_(open_statuses))
    )
    completed_today = db.scalar(
        select(func.count())
        .select_from(Task)
        .where(Task.completed_at >= start, Task.completed_at < end)
    )
    open_count = db.scalar(
        select(func.count()).select_from(Task).where(Task.status.in_(open_statuses))
    )
    return {
        "tasks_today": int(today_count or 0),
        "tasks_overdue": int(overdue_count or 0),
        "tasks_completed_today": int(completed_today or 0),
        "tasks_open": int(open_count or 0),
    }
