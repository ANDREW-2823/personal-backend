from __future__ import annotations

from typing import Any

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.core.timeutils import as_utc, utcnow
from app.models.task import Subtask, Tag, Task
from app.schemas.core import TaskCreate, TaskUpdate


class TaskRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get(self, task_id: str) -> Task | None:
        return self.db.scalar(
            select(Task)
            .options(selectinload(Task.subtasks), selectinload(Task.tag_objects))
            .where(Task.id == task_id)
        )

    def _resolve_tags(self, names: list[str]) -> list[Tag]:
        tags: list[Tag] = []
        for raw in names:
            name = raw.strip()
            if not name:
                continue
            tag = self.db.scalar(select(Tag).where(func.lower(Tag.name) == name.lower()))
            if tag is None:
                tag = Tag(name=name)
                self.db.add(tag)
                self.db.flush()
            tags.append(tag)
        return tags

    def create(self, data: TaskCreate) -> Task:
        task = Task(
            title=data.title.strip(),
            description=data.description,
            status=data.status,
            priority=data.priority,
            due_at=as_utc(data.due_at),
            category=data.category,
            color=data.color,
            estimated_minutes=data.estimated_minutes,
            recurrence=data.recurrence,
            blocked_by=data.blocked_by,
        )
        if data.status == "done":
            task.completed_at = utcnow()
        task.tag_objects = self._resolve_tags(data.tags)
        for index, sub in enumerate(data.subtasks):
            task.subtasks.append(Subtask(title=sub.title, completed=sub.completed, position=index))
        self.db.add(task)
        self.db.flush()
        return task

    def update(self, task: Task, data: TaskUpdate) -> Task:
        payload = data.model_dump(exclude_unset=True)
        if "clear_due_at" in payload:
            payload.pop("clear_due_at")
            task.due_at = None
        if "due_at" in payload:
            task.due_at = as_utc(payload.pop("due_at"))
        if "tags" in payload:
            task.tag_objects = self._resolve_tags(payload.pop("tags") or [])
        if "blocked_by" in payload:
            blocked = payload.pop("blocked_by")
            task.blocked_by = blocked
        if "subtasks" in payload:
            subtasks = payload.pop("subtasks") or []
            task.subtasks.clear()
            self.db.flush()
            for index, sub in enumerate(subtasks):
                value = sub if isinstance(sub, dict) else sub.model_dump()
                task.subtasks.append(
                    Subtask(
                        title=value["title"],
                        completed=bool(value.get("completed")),
                        position=index,
                    )
                )
        if "status" in payload:
            new_status = payload.pop("status")
            if new_status == "done" and task.status != "done":
                task.completed_at = utcnow()
            elif new_status != "done":
                task.completed_at = None
            task.status = new_status
        for key, value in payload.items():
            setattr(task, key, value)
        self.db.flush()
        return task

    def delete(self, task: Task) -> None:
        self.db.delete(task)
        self.db.flush()

    def list(
        self,
        *,
        status: list[str] | None = None,
        priority: list[str] | None = None,
        category: str | None = None,
        tag: str | None = None,
        search: str | None = None,
        due_from: Any = None,
        due_to: Any = None,
        overdue_before: Any = None,
        sort: str = "smart",
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[Task], int]:
        stmt = select(Task).options(selectinload(Task.subtasks), selectinload(Task.tag_objects))
        if status:
            stmt = stmt.where(Task.status.in_(status))
        if priority:
            stmt = stmt.where(Task.priority.in_(priority))
        if category:
            stmt = stmt.where(Task.category == category)
        if tag:
            stmt = stmt.join(Task.tag_objects).where(func.lower(Tag.name) == tag.lower())
        if search:
            pattern = f"%{search.strip()}%"
            stmt = stmt.where(or_(Task.title.ilike(pattern), Task.description.ilike(pattern)))
        if due_from is not None:
            stmt = stmt.where(Task.due_at >= due_from)
        if due_to is not None:
            stmt = stmt.where(Task.due_at < due_to)
        if overdue_before is not None:
            stmt = stmt.where(
                Task.due_at < overdue_before,
                Task.status.notin_(["done", "cancelled"]),
            )

        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int(self.db.scalar(count_stmt) or 0)

        if sort == "due":
            stmt = stmt.order_by(Task.due_at.is_(None), Task.due_at.asc())
        elif sort == "created":
            stmt = stmt.order_by(Task.created_at.desc())
        elif sort == "priority":
            stmt = stmt.order_by(
                case(
                    (Task.priority == "critical", 0),
                    (Task.priority == "high", 1),
                    (Task.priority == "medium", 2),
                    else_=3,
                ),
                Task.due_at.asc().nullslast(),
            )
        elif sort == "title":
            stmt = stmt.order_by(Task.title.asc())
        else:  # smart
            stmt = stmt.order_by(
                Task.status == "done",
                Task.due_at.is_(None),
                Task.due_at.asc(),
                case(
                    (Task.priority == "critical", 0),
                    (Task.priority == "high", 1),
                    (Task.priority == "medium", 2),
                    else_=3,
                ),
            )
        stmt = stmt.limit(limit).offset(offset)
        return list(self.db.scalars(stmt).unique()), total

    def stats(self) -> dict[str, int]:
        rows = self.db.execute(select(Task.status, func.count()).group_by(Task.status)).all()
        counts = {status: count for status, count in rows}
        return {
            "todo": counts.get("todo", 0),
            "in_progress": counts.get("in_progress", 0),
            "blocked": counts.get("blocked", 0),
            "done": counts.get("done", 0),
            "cancelled": counts.get("cancelled", 0),
        }

    def categories(self) -> list[str]:
        rows = self.db.execute(
            select(Task.category)
            .where(Task.category.is_not(None))
            .group_by(Task.category)
            .order_by(func.count().desc())
        ).all()
        return [row[0] for row in rows if row[0]]
