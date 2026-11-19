from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.automation import workflow_service
from app.automation.actions import ACTION_CONFIGS, ACTION_LABELS
from app.core.security import require_token
from app.db.session import get_db
from app.models.automation import WorkflowExecution
from app.schemas.automation import (
    ExecutionOut,
    RunRequest,
    WorkflowCreate,
    WorkflowOut,
    WorkflowUpdate,
)
from app.schemas.core import MessageResponse

router = APIRouter(prefix="/workflows", tags=["automation"], dependencies=[Depends(require_token)])


@router.get("", response_model=list[WorkflowOut])
def list_workflows(db: Session = Depends(get_db)) -> list[WorkflowOut]:
    return [WorkflowOut.model_validate(w) for w in workflow_service.list_workflows(db)]


@router.get("/meta")
def workflow_meta() -> dict:
    return {
        "actions": [
            {
                "type": name,
                "label": ACTION_LABELS.get(name, name),
                "fields": list(model.model_json_schema().get("properties", {}).keys()),
                "requires_desktop": name in {"notify", "open_app", "open_folder", "run_script"},
            }
            for name, model in ACTION_CONFIGS.items()
        ],
        "triggers": ["manual", "schedule_once", "recurring", "task_completed", "reminder_event"],
    }


@router.post("", response_model=WorkflowOut, status_code=201)
def create_workflow(payload: WorkflowCreate, db: Session = Depends(get_db)) -> WorkflowOut:
    workflow = workflow_service.create_workflow(db, payload)
    db.commit()
    db.refresh(workflow)
    return WorkflowOut.model_validate(workflow)


@router.get("/{workflow_id}", response_model=WorkflowOut)
def get_workflow(workflow_id: str, db: Session = Depends(get_db)) -> WorkflowOut:
    return WorkflowOut.model_validate(workflow_service.get_workflow(db, workflow_id))


@router.patch("/{workflow_id}", response_model=WorkflowOut)
def update_workflow(
    workflow_id: str, payload: WorkflowUpdate, db: Session = Depends(get_db)
) -> WorkflowOut:
    workflow = workflow_service.update_workflow(db, workflow_id, payload)
    db.commit()
    db.refresh(workflow)
    return WorkflowOut.model_validate(workflow)


@router.delete("/{workflow_id}", response_model=MessageResponse)
def delete_workflow(workflow_id: str, db: Session = Depends(get_db)) -> MessageResponse:
    workflow_service.delete_workflow(db, workflow_id)
    db.commit()
    return MessageResponse(message="Workflow deleted")


@router.post("/{workflow_id}/run", response_model=ExecutionOut)
def run_workflow(
    workflow_id: str, payload: RunRequest, db: Session = Depends(get_db)
) -> ExecutionOut:
    execution = workflow_service.run_workflow(db, workflow_id, dry_run=payload.dry_run)
    db.refresh(execution)
    return ExecutionOut.model_validate(execution)


@router.get("/{workflow_id}/executions", response_model=list[ExecutionOut])
def executions(workflow_id: str, db: Session = Depends(get_db)) -> list[ExecutionOut]:
    workflow_service.get_workflow(db, workflow_id)
    rows = list(
        db.scalars(
            select(WorkflowExecution)
            .where(WorkflowExecution.workflow_id == workflow_id)
            .order_by(WorkflowExecution.started_at.desc())
            .limit(50)
        )
    )
    return [ExecutionOut.model_validate(e) for e in rows]
