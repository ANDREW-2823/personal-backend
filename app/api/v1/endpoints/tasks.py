from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import require_token
from app.core.timeutils import day_bounds_utc
from app.db.session import get_db
from app.models.task import Tag
from app.repositories.tasks import TaskRepository
from app.schemas.core import (
    MessageResponse,
    RescheduleRequest,
    TaskCreate,
    TaskOut,
    TaskPage,
    TaskUpdate,
)
from app.services import task_service
from app.services.settings_service import get_settings

router = APIRouter(prefix="/tasks", tags=["tasks"], dependencies=[Depends(require_token)])


@router.get("", response_model=TaskPage)
def list_tasks(
    db: Session = Depends(get_db),
    task_status: list[str] | None = Query(default=None, alias="status"),
    priority: list[str] | None = Query(default=None),
    category: str | None = None,
    tag: str | None = None,
    search: str | None = None,
    view: str | None = Query(default=None, pattern="^(today|upcoming|overdue|all)$"),
    sort: str = Query(default="smart", pattern="^(smart|due|created|priority|title)$"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0, le=100_000),
) -> TaskPage:
    tz = get_settings(db).timezone
    start, end = day_bounds_utc(tz)
    due_from = due_to = overdue_before = None
    statuses = task_status
    if view == "today":
        due_from, due_to = start, end
        statuses = statuses or ["todo", "in_progress", "blocked"]
    elif view == "upcoming":
        due_from = end
        statuses = statuses or ["todo", "in_progress", "blocked"]
    elif view == "overdue":
        overdue_before = start
        statuses = statuses or ["todo", "in_progress", "blocked"]
    elif view == "all" and not statuses:
        statuses = ["todo", "in_progress", "blocked", "done", "cancelled"]

    items, total = TaskRepository(db).list(
        status=statuses,
        priority=priority,
        category=category,
        tag=tag,
        search=search,
        due_from=due_from,
        due_to=due_to,
        overdue_before=overdue_before,
        sort=sort,
        limit=limit,
        offset=offset,
    )
    return TaskPage(
        items=[TaskOut.model_validate(t) for t in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/meta")
def task_meta(db: Session = Depends(get_db)) -> dict:
    repo = TaskRepository(db)
    tags = list(db.scalars(select(Tag).order_by(Tag.name.asc())))
    return {
        "stats": repo.stats(),
        "categories": repo.categories(),
        "tags": [{"name": t.name, "color": t.color} for t in tags],
        "summary": task_service.today_summary(db),
    }


@router.post("", response_model=TaskOut, status_code=status.HTTP_201_CREATED)
def create_task(payload: TaskCreate, db: Session = Depends(get_db)) -> TaskOut:
    task = task_service.create_task(db, payload)
    db.commit()
    db.refresh(task)
    return TaskOut.model_validate(task)


@router.get("/{task_id}", response_model=TaskOut)
def get_task(task_id: str, db: Session = Depends(get_db)) -> TaskOut:
    return TaskOut.model_validate(task_service.get_task(db, task_id))


@router.patch("/{task_id}", response_model=TaskOut)
def update_task(task_id: str, payload: TaskUpdate, db: Session = Depends(get_db)) -> TaskOut:
    task = task_service.update_task(db, task_id, payload)
    db.commit()
    db.refresh(task)
    return TaskOut.model_validate(task)


@router.delete("/{task_id}", response_model=MessageResponse)
def delete_task(task_id: str, db: Session = Depends(get_db)) -> MessageResponse:
    task_service.delete_task(db, task_id)
    db.commit()
    return MessageResponse(message="Task deleted")


@router.post("/import", response_model=list[TaskOut], status_code=status.HTTP_201_CREATED)
def import_tasks(payload: list[TaskCreate], db: Session = Depends(get_db)) -> list[TaskOut]:
    if len(payload) > 500:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Import at most 500 tasks at a time."
        )
    created = []
    for item in payload:
        task = task_service.create_task(db, item, source="import")
        created.append(task)
    db.commit()
    for task in created:
        db.refresh(task)
    return [TaskOut.model_validate(task) for task in created]


@router.post("/{task_id}/reschedule", response_model=TaskOut)
def reschedule_task(
    task_id: str, payload: RescheduleRequest, db: Session = Depends(get_db)
) -> TaskOut:
    task = task_service.reschedule(db, task_id, payload.due_at)
    db.commit()
    db.refresh(task)
    return TaskOut.model_validate(task)
