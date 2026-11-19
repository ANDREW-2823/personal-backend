from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timeutils import local_date
from app.models.habit import Habit, HabitCompletion, Workout
from app.schemas.core import HabitCheckIn, HabitCreate, WorkoutCreate
from app.services import activity_service
from app.services.settings_service import get_settings


def list_habits(db: Session, *, include_archived: bool = False) -> list[Habit]:
    stmt = select(Habit).order_by(Habit.archived.asc(), Habit.created_at.asc())
    if not include_archived:
        stmt = stmt.where(Habit.archived.is_(False))
    return list(db.scalars(stmt))


def create_habit(db: Session, data: HabitCreate) -> Habit:
    habit = Habit(**data.model_dump())
    db.add(habit)
    db.flush()
    activity_service.log(
        db,
        "habit.created",
        f"Created habit “{habit.name}”",
        entity_type="habit",
        entity_id=habit.id,
    )
    return habit


def update_habit(db: Session, habit_id: str, data: HabitCreate) -> Habit:
    habit = _get(db, habit_id)
    for key, value in data.model_dump().items():
        setattr(habit, key, value)
    db.flush()
    return habit


def archive_habit(db: Session, habit_id: str, archived: bool = True) -> Habit:
    habit = _get(db, habit_id)
    habit.archived = archived
    db.flush()
    return habit


def _get(db: Session, habit_id: str) -> Habit:
    habit = db.get(Habit, habit_id)
    if habit is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Habit not found")
    return habit


def check_in(db: Session, habit_id: str, data: HabitCheckIn) -> Habit:
    habit = _get(db, habit_id)
    tz = get_settings(db).timezone
    on = data.on or local_date(tz)
    completion = db.scalar(
        select(HabitCompletion).where(
            HabitCompletion.habit_id == habit.id, HabitCompletion.completed_on == on
        )
    )
    if completion is None:
        completion = HabitCompletion(habit_id=habit.id, completed_on=on, count=data.count)
        db.add(completion)
    else:
        completion.count = max(completion.count, data.count)
    db.flush()
    activity_service.log(
        db,
        "habit.checked_in",
        f"Checked in “{habit.name}”",
        detail={"date": str(on), "count": data.count},
        entity_type="habit",
        entity_id=habit.id,
    )
    return habit


def undo_check_in(db: Session, habit_id: str, on: date | None = None) -> Habit:
    habit = _get(db, habit_id)
    tz = get_settings(db).timezone
    target = on or local_date(tz)
    completion = db.scalar(
        select(HabitCompletion).where(
            HabitCompletion.habit_id == habit.id, HabitCompletion.completed_on == target
        )
    )
    if completion is not None:
        db.delete(completion)
    db.flush()
    return habit


def completion_data(
    db: Session, habit_ids: list[str], *, days: int = 180
) -> dict[str, dict[date, int]]:
    if not habit_ids:
        return {}
    start = local_date(get_settings(db).timezone) - timedelta(days=days)
    rows = db.execute(
        select(HabitCompletion).where(
            HabitCompletion.habit_id.in_(habit_ids), HabitCompletion.completed_on >= start
        )
    ).scalars()
    data: dict[str, dict[date, int]] = {hid: {} for hid in habit_ids}
    for row in rows:
        data.setdefault(row.habit_id, {})[row.completed_on] = row.count
    return data


def compute_streak(habit: Habit, completions: dict[date, int], today: date) -> int:
    if not completions:
        return 0
    target = max(1, habit.target_per_period)
    if habit.cadence == "weekly":
        streak = 0
        cursor = today - timedelta(days=today.weekday())
        current_total = sum(
            count
            for day, count in completions.items()
            if cursor <= day < cursor + timedelta(days=7)
        )
        if current_total < target:
            cursor -= timedelta(days=7)
        while streak < 260:
            total = sum(
                count
                for day, count in completions.items()
                if cursor <= day < cursor + timedelta(days=7)
            )
            if total < target:
                break
            streak += 1
            cursor -= timedelta(days=7)
        return streak

    streak = 0
    cursor = today
    if completions.get(cursor, 0) < target:
        cursor -= timedelta(days=1)
    while streak < 3000:
        if completions.get(cursor, 0) >= target:
            streak += 1
            cursor -= timedelta(days=1)
        else:
            break
    return streak


def habit_views(db: Session) -> list[dict]:
    tz = get_settings(db).timezone
    today = local_date(tz)
    habits = list_habits(db)
    by_id = completion_data(db, [h.id for h in habits])
    views = []
    for habit in habits:
        completions = by_id.get(habit.id, {})
        views.append(
            {
                "id": habit.id,
                "name": habit.name,
                "description": habit.description,
                "cadence": habit.cadence,
                "target_per_period": habit.target_per_period,
                "reminder_time": habit.reminder_time,
                "color": habit.color,
                "archived": habit.archived,
                "streak": compute_streak(habit, completions, today),
                "completed_today": completions.get(today, 0),
                "created_at": habit.created_at,
                "updated_at": habit.updated_at,
            }
        )
    return views


def add_workout(db: Session, data: WorkoutCreate) -> Workout:
    workout = Workout(
        name=data.name,
        kind=data.kind,
        duration_minutes=data.duration_minutes,
        intensity=data.intensity,
        notes=data.notes,
        performed_at=data.performed_at or datetime.now().astimezone(),
    )
    db.add(workout)
    db.flush()
    activity_service.log(
        db,
        "workout.logged",
        f"Logged workout “{workout.name}” ({workout.duration_minutes} min)",
        entity_type="workout",
        entity_id=workout.id,
    )
    return workout


def list_workouts(db: Session, limit: int = 50) -> list[Workout]:
    return list(db.scalars(select(Workout).order_by(Workout.performed_at.desc()).limit(limit)))


def workout_stats(db: Session, *, days: int = 30) -> dict:
    since = datetime.now().astimezone() - timedelta(days=days)
    workouts = list(db.scalars(select(Workout).where(Workout.performed_at >= since)))
    total_minutes = sum(w.duration_minutes for w in workouts)
    by_kind: dict[str, int] = {}
    for workout in workouts:
        by_kind[workout.kind] = by_kind.get(workout.kind, 0) + workout.duration_minutes
    return {
        "range_days": days,
        "sessions": len(workouts),
        "total_minutes": total_minutes,
        "by_kind": by_kind,
    }
