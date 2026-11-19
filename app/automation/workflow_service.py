from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from datetime import time as dt_time
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.automation.actions import ActionConfigError, execute_action, validate_action_config
from app.core.datetime_utils import normalize_dt
from app.core.timeutils import get_zone, utcnow
from app.db.session import SessionLocal
from app.models.automation import Workflow, WorkflowExecution, WorkflowStep
from app.schemas.automation import WorkflowCreate, WorkflowUpdate
from app.services import activity_service

logger = logging.getLogger("jarvis.workflows")

MAX_EXECUTION_SECONDS = 180


def get_workflow(db: Session, workflow_id: str) -> Workflow:
    workflow = db.scalar(
        select(Workflow).options(selectinload(Workflow.steps)).where(Workflow.id == workflow_id)
    )
    if workflow is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workflow not found")
    return workflow


def list_workflows(db: Session) -> list[Workflow]:
    return list(
        db.scalars(
            select(Workflow)
            .options(selectinload(Workflow.steps))
            .order_by(Workflow.created_at.desc())
        ).unique()
    )


def _validate_steps(steps: list[Any]) -> None:
    seen_positions: set[int] = set()
    for index, step in enumerate(steps):
        position = step.position if step.position is not None else index
        if position in seen_positions:
            raise HTTPException(422, "Step positions must be unique")
        seen_positions.add(position)
        try:
            validate_action_config(step.action_type, step.config or {})
        except ActionConfigError as exc:
            raise HTTPException(
                422,
                f"Step {index + 1} ({step.action_type}) is invalid: {exc}",
            ) from exc
        if step.condition:
            field = step.condition.get("field")
            op = step.condition.get("op")
            if field not in {"priority", "category", "title", "event", "source"}:
                raise HTTPException(
                    422,
                    f"Step {index + 1}: unsupported condition field",
                )
            if op not in {"equals", "not_equals", "contains", "gt", "lt", "exists"}:
                raise HTTPException(
                    422,
                    f"Step {index + 1}: unsupported condition operator",
                )


def create_workflow(db: Session, data: WorkflowCreate) -> Workflow:
    _validate_steps(data.steps)
    workflow = Workflow(
        name=data.name,
        description=data.description,
        trigger_type=data.trigger_type,
        trigger_config=data.trigger_config,
        enabled=data.enabled,
        dry_run=data.dry_run,
        retry_limit=data.retry_limit,
    )
    for index, step in enumerate(data.steps):
        workflow.steps.append(
            WorkflowStep(
                position=step.position if step.position is not None else index,
                kind=step.kind,
                action_type=step.action_type,
                config=step.config,
                condition=step.condition,
            )
        )
    workflow.next_run_at = compute_next_run(
        workflow.trigger_type, workflow.trigger_config, after=utcnow()
    )
    db.add(workflow)
    db.flush()
    activity_service.log(
        db,
        "workflow.created",
        f"Created workflow “{workflow.name}”",
        entity_type="workflow",
        entity_id=workflow.id,
    )
    return workflow


def update_workflow(db: Session, workflow_id: str, data: WorkflowUpdate) -> Workflow:
    workflow = get_workflow(db, workflow_id)
    payload = data.model_dump(exclude_unset=True)
    steps = payload.pop("steps", None)
    if steps is not None:
        parsed_steps = data.steps or []
        _validate_steps(parsed_steps)
        workflow.steps.clear()
        db.flush()
        for index, step in enumerate(parsed_steps):
            workflow.steps.append(
                WorkflowStep(
                    position=step.position if step.position is not None else index,
                    kind=step.kind,
                    action_type=step.action_type,
                    config=step.config,
                    condition=step.condition,
                )
            )
    for key, value in payload.items():
        setattr(workflow, key, value)
    workflow.next_run_at = compute_next_run(
        workflow.trigger_type, workflow.trigger_config, after=utcnow()
    )
    db.flush()
    return workflow


def delete_workflow(db: Session, workflow_id: str) -> None:
    workflow = get_workflow(db, workflow_id)
    db.delete(workflow)
    db.flush()


def compute_next_run(
    trigger_type: str,
    config: dict[str, Any],
    *,
    after: datetime | None = None,
    timezone_name: str = "UTC",
) -> datetime | None:
    reference = after or utcnow()
    zone = get_zone(timezone_name)
    if trigger_type == "schedule_once":
        raw = config.get("at")
        if not raw:
            return None
        try:
            value = datetime.fromisoformat(str(raw))
        except ValueError:
            return None
        normalized = normalize_dt(value, timezone_name)
        return normalized if normalized and normalized > reference else None

    if trigger_type == "recurring":
        frequency = config.get("frequency")
        if frequency == "interval":
            minutes = int(config.get("interval_minutes") or 60)
            return reference + timedelta(minutes=max(5, minutes))
        try:
            hour, minute = [int(part) for part in str(config.get("time", "09:00")).split(":")]
        except ValueError:
            hour, minute = 9, 0
        local_ref = reference.astimezone(zone)
        candidate = datetime.combine(local_ref.date(), dt_time(hour, minute), tzinfo=zone)
        if frequency == "weekly":
            weekdays = config.get("weekdays") or [0]
            weekdays = sorted({int(d) for d in weekdays if 0 <= int(d) <= 6})
            for offset in range(0, 15):
                day = candidate + timedelta(days=offset)
                if day.weekday() in weekdays and day.astimezone(UTC) > reference:
                    return day.astimezone(UTC)
            return None
        if candidate.astimezone(UTC) <= reference:
            candidate += timedelta(days=1)
        return candidate.astimezone(UTC)
    return None


def _evaluate(condition: dict[str, Any] | None, context: dict[str, Any]) -> bool:
    if not condition:
        return True
    field = condition.get("field")
    op = condition.get("op")
    expected = condition.get("value")
    actual = context.get(field)
    if op == "exists":
        return actual is not None
    if actual is None:
        return False
    if op == "equals":
        return str(actual).lower() == str(expected).lower()
    if op == "not_equals":
        return str(actual).lower() != str(expected).lower()
    if op == "contains":
        return str(expected).lower() in str(actual).lower()
    if op == "gt":
        try:
            return float(actual) > float(expected)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False
    if op == "lt":
        try:
            return float(actual) < float(expected)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False
    return False


def execute_workflow(
    db: Session,
    workflow: Workflow,
    *,
    triggered_by: str = "manual",
    dry_run: bool | None = None,
    dedupe_key: str | None = None,
    context: dict[str, Any] | None = None,
) -> WorkflowExecution | None:
    is_dry_run = workflow.dry_run if dry_run is None else dry_run
    execution = WorkflowExecution(
        workflow_id=workflow.id,
        triggered_by=triggered_by,
        status="running",
        dedupe_key=dedupe_key,
        result={"dry_run": is_dry_run, "steps": []},
    )
    db.add(execution)
    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError:
        db.expunge(execution)
        logger.info("Duplicate workflow execution prevented for %s", workflow.id)
        return None

    step_results: list[dict[str, Any]] = []
    started = time.monotonic()
    failed = False
    any_success = False
    steps = sorted(workflow.steps, key=lambda s: s.position)
    trigger_context = {**(context or {}), "source": triggered_by}

    for step in steps:
        if time.monotonic() - started > MAX_EXECUTION_SECONDS:
            step_results.append(
                {
                    "step": step.position,
                    "action": step.action_type,
                    "status": "skipped",
                    "detail": "Execution time limit reached",
                }
            )
            failed = True
            continue
        if not _evaluate(step.condition, trigger_context):
            step_results.append(
                {
                    "step": step.position,
                    "action": step.action_type,
                    "status": "condition_not_met",
                    "detail": "Condition evaluated to false",
                }
            )
            continue
        if is_dry_run:
            step_results.append(
                {
                    "step": step.position,
                    "action": step.action_type,
                    "status": "dry_run",
                    "detail": f"Would {step.action_type.replace('_', ' ')}",
                }
            )
            any_success = True
            continue

        attempts_allowed = max(1, workflow.retry_limit + 1)
        attempts = 0
        last_error = ""
        step_ok = False
        while attempts < attempts_allowed:
            attempts += 1
            try:
                detail = execute_action(db, step.action_type, step.config or {})
                step_ok = True
                step_results.append(
                    {
                        "step": step.position,
                        "action": step.action_type,
                        "status": "succeeded",
                        "detail": detail,
                        "attempts": attempts,
                    }
                )
                any_success = True
                break
            except Exception as exc:  # noqa: BLE001  (recorded, not swallowed silently)
                last_error = str(exc)
                logger.warning(
                    "Workflow %s step %s failed (attempt %s): %s",
                    workflow.id,
                    step.position,
                    attempts,
                    exc,
                )
                if attempts < attempts_allowed:
                    time.sleep(0.3)
        if not step_ok:
            failed = True
            step_results.append(
                {
                    "step": step.position,
                    "action": step.action_type,
                    "status": "failed",
                    "detail": last_error,
                    "attempts": attempts,
                }
            )
            break

    execution.result = {"dry_run": is_dry_run, "steps": step_results}
    execution.finished_at = utcnow()
    if failed:
        execution.status = "partial" if any_success else "failed"
        execution.error = next(
            (s["detail"] for s in step_results if s["status"] == "failed"), "Workflow failed"
        )
    elif not any_success and step_results:
        execution.status = "skipped"
    else:
        execution.status = "succeeded"

    workflow.last_run_at = utcnow()
    activity_service.log(
        db,
        "workflow.executed",
        f"Workflow “{workflow.name}” {execution.status}" + (" (dry run)" if is_dry_run else ""),
        detail={"execution_id": execution.id, "triggered_by": triggered_by},
        entity_type="workflow",
        entity_id=workflow.id,
    )
    db.commit()
    return execution


def run_workflow(
    db: Session, workflow_id: str, *, dry_run: bool | None = None
) -> WorkflowExecution:
    workflow = get_workflow(db, workflow_id)
    execution = execute_workflow(db, workflow, dry_run=dry_run)
    if execution is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Workflow execution was skipped")
    return execution


def tick(db: Session, *, now: datetime | None = None) -> list[WorkflowExecution]:
    reference = now or utcnow()
    due = list(
        db.scalars(
            select(Workflow)
            .options(selectinload(Workflow.steps))
            .where(
                Workflow.enabled.is_(True),
                Workflow.trigger_type.in_(["schedule_once", "recurring"]),
                Workflow.next_run_at.is_not(None),
                Workflow.next_run_at <= reference,
            )
            .limit(25)
        ).unique()
    )
    executions: list[WorkflowExecution] = []
    for workflow in due:
        scheduled_at = workflow.next_run_at
        dedupe_key = f"wf:{workflow.id}:{scheduled_at.isoformat() if scheduled_at else 'manual'}"
        execution = execute_workflow(db, workflow, triggered_by="schedule", dedupe_key=dedupe_key)
        if execution is not None:
            executions.append(execution)
        workflow.next_run_at = compute_next_run(
            workflow.trigger_type, workflow.trigger_config, after=reference
        )
        if workflow.trigger_type == "schedule_once":
            workflow.enabled = False
        db.commit()
    return executions


def trigger_event(db: Session, event: str, context: dict[str, Any]) -> list[str]:
    """Run workflows matching an event in a fresh session (never commits caller state)."""
    if event not in {"task_completed", "reminder_event"}:
        return []
    executed: list[str] = []
    session = SessionLocal()
    try:
        workflows = list(
            session.scalars(
                select(Workflow)
                .options(selectinload(Workflow.steps))
                .where(Workflow.enabled.is_(True), Workflow.trigger_type == event)
            ).unique()
        )
        for workflow in workflows:
            try:
                execution = execute_workflow(
                    session,
                    workflow,
                    triggered_by=event,
                    dedupe_key=f"wf:{workflow.id}:{event}:{utcnow().isoformat()}",
                    context={"event": event, **context},
                )
                if execution is not None:
                    executed.append(execution.id)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Event workflow %s failed: %s", workflow.id, exc)
                session.rollback()
    finally:
        session.close()
    return executed
