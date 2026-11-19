from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ConversationOut(ORMModel):
    id: str
    title: str
    message_count: int
    created_at: datetime
    updated_at: datetime


class MessageOut(ORMModel):
    id: str
    conversation_id: str
    role: str
    content: str
    tool_calls: list[dict] | None
    status: str
    created_at: datetime


class MessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=20_000)


class PendingActionOut(ORMModel):
    id: str
    conversation_id: str
    tool: str
    args: dict
    description: str
    requires_approval: bool
    status: str
    result: dict | None
    created_at: datetime


class ChatResponse(BaseModel):
    message: MessageOut
    pending_actions: list[PendingActionOut] = Field(default_factory=list)
    conversation_id: str


class ApproveActionRequest(BaseModel):
    approve: bool = True


class ToolInfo(BaseModel):
    name: str
    description: str
    requires_approval: bool
    parameters: dict


class AiStatusOut(BaseModel):
    provider: str
    model: str
    mode: str
    tools: list[ToolInfo]
    note: str | None = None
