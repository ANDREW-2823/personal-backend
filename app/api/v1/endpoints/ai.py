from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.ai import service as ai_service
from app.core.security import require_token
from app.db.session import get_db
from app.schemas.ai import (
    AiStatusOut,
    ApproveActionRequest,
    ChatResponse,
    ConversationOut,
    MessageCreate,
    MessageOut,
    PendingActionOut,
)
from app.schemas.core import MessageResponse

router = APIRouter(prefix="/ai", tags=["assistant"], dependencies=[Depends(require_token)])


@router.get("/status", response_model=AiStatusOut)
def ai_status(db: Session = Depends(get_db)) -> AiStatusOut:
    return ai_service.ai_status(db)


@router.get("/conversations", response_model=list[ConversationOut])
def list_conversations(
    limit: int = Query(default=50, ge=1, le=200), db: Session = Depends(get_db)
) -> list[ConversationOut]:
    return [
        ConversationOut.model_validate(c) for c in ai_service.list_conversations(db, limit=limit)
    ]


@router.post("/conversations", response_model=ConversationOut, status_code=201)
def create_conversation(db: Session = Depends(get_db)) -> ConversationOut:
    return ConversationOut.model_validate(ai_service.create_conversation(db))


@router.delete("/conversations/{conversation_id}", response_model=MessageResponse)
def delete_conversation(conversation_id: str, db: Session = Depends(get_db)) -> MessageResponse:
    ai_service.delete_conversation(db, conversation_id)
    return MessageResponse(message="Conversation deleted")


@router.get("/conversations/{conversation_id}/messages", response_model=list[MessageOut])
def list_messages(conversation_id: str, db: Session = Depends(get_db)) -> list[MessageOut]:
    ai_service.get_conversation(db, conversation_id)
    return [MessageOut.model_validate(m) for m in ai_service.list_messages(db, conversation_id)]


@router.get("/conversations/{conversation_id}/actions", response_model=list[PendingActionOut])
def list_pending_actions(
    conversation_id: str,
    status_filter: str = Query(default="pending", alias="status"),
    db: Session = Depends(get_db),
) -> list[PendingActionOut]:
    ai_service.get_conversation(db, conversation_id)
    return [
        PendingActionOut.model_validate(a)
        for a in ai_service.list_pending_actions(db, conversation_id, status_filter)
    ]


@router.post("/conversations/{conversation_id}/messages", response_model=ChatResponse)
def send_message(
    conversation_id: str, payload: MessageCreate, db: Session = Depends(get_db)
) -> ChatResponse:
    return ai_service.send_message(db, conversation_id, payload.content)


@router.post("/conversations/{conversation_id}/stream")
def stream_message(
    conversation_id: str, payload: MessageCreate, db: Session = Depends(get_db)
) -> StreamingResponse:
    """Progressive SSE delivery of a completed, verified assistant turn.

    The assistant's work (tools, validation, approvals) is finished before
    streaming starts, so the UI never shows an unverified success message.
    """
    turn = ai_service.send_message(db, conversation_id, payload.content)
    message = turn.message
    meta = {
        "conversation_id": turn.conversation_id,
        "message_id": message.id,
        "status": message.status,
        "pending_actions": [a.model_dump(mode="json") for a in turn.pending_actions],
        "tool_calls": message.tool_calls,
    }

    async def events():
        yield f"event: meta\ndata: {json.dumps(meta)}\n\n"
        text = message.content
        chunk_size = 28
        for index in range(0, len(text), chunk_size):
            chunk = text[index : index + chunk_size]
            yield f"event: token\ndata: {json.dumps({'text': chunk})}\n\n"
            await asyncio.sleep(0.008)
        yield "event: done\ndata: {}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/actions/{action_id}/resolve", response_model=ChatResponse)
def resolve_action(
    action_id: str, payload: ApproveActionRequest, db: Session = Depends(get_db)
) -> ChatResponse:
    return ai_service.resolve_action(db, action_id, payload.approve)
