from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.security import require_token
from app.db.session import get_db
from app.schemas.core import (
    HabitCheckIn,
    HabitCreate,
    HabitOut,
    MessageResponse,
    WorkoutCreate,
    WorkoutOut,
)
from app.services import habit_service

router = APIRouter(tags=["habits"], dependencies=[Depends(require_token)])


@router.get("/habits", response_model=list[HabitOut])
def list_habits(include_archived: bool = False, db: Session = Depends(get_db)) -> list[HabitOut]:
    return [HabitOut(**view) for view in habit_service.habit_views(db)]


@router.post("/habits", response_model=HabitOut, status_code=201)
def create_habit(payload: HabitCreate, db: Session = Depends(get_db)) -> HabitOut:
    habit = habit_service.create_habit(db, payload)
    db.commit()
    views = {v["id"]: v for v in habit_service.habit_views(db)}
    return HabitOut(**views[habit.id])


@router.patch("/habits/{habit_id}", response_model=HabitOut)
def update_habit(habit_id: str, payload: HabitCreate, db: Session = Depends(get_db)) -> HabitOut:
    habit = habit_service.update_habit(db, habit_id, payload)
    db.commit()
    views = {v["id"]: v for v in habit_service.habit_views(db)}
    return HabitOut(**views[habit.id])


@router.delete("/habits/{habit_id}", response_model=MessageResponse)
def archive_habit(habit_id: str, db: Session = Depends(get_db)) -> MessageResponse:
    habit_service.archive_habit(db, habit_id, True)
    db.commit()
    return MessageResponse(message="Habit archived")


@router.post("/habits/{habit_id}/check-in", response_model=HabitOut)
def check_in(habit_id: str, payload: HabitCheckIn, db: Session = Depends(get_db)) -> HabitOut:
    habit = habit_service.check_in(db, habit_id, payload)
    db.commit()
    views = {v["id"]: v for v in habit_service.habit_views(db)}
    return HabitOut(**views[habit.id])


@router.delete("/habits/{habit_id}/check-in", response_model=HabitOut)
def undo_check_in(habit_id: str, on: date | None = None, db: Session = Depends(get_db)) -> HabitOut:
    habit = habit_service.undo_check_in(db, habit_id, on)
    db.commit()
    views = {v["id"]: v for v in habit_service.habit_views(db)}
    return HabitOut(**views[habit.id])


@router.get("/workouts", response_model=list[WorkoutOut])
def list_workouts(limit: int = Query(default=50, ge=1, le=200), db: Session = Depends(get_db)):
    return [WorkoutOut.model_validate(w) for w in habit_service.list_workouts(db, limit)]


@router.post("/workouts", response_model=WorkoutOut, status_code=201)
def create_workout(payload: WorkoutCreate, db: Session = Depends(get_db)) -> WorkoutOut:
    workout = habit_service.add_workout(db, payload)
    db.commit()
    db.refresh(workout)
    return WorkoutOut.model_validate(workout)


@router.get("/fitness/stats")
def fitness_stats(
    days: int = Query(default=30, ge=1, le=365), db: Session = Depends(get_db)
) -> dict:
    return habit_service.workout_stats(db, days=days)
