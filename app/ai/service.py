from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai import mock_assistant
from app.ai.providers import ProviderError, build_provider
from app.ai.tools import TOOLS, execute_tool, tool_schemas, validate_tool_call
from app.core.timeutils import get_zone, utcnow
from app.models.ai import Conversation, Message, PendingAction
from app.schemas.ai import AiStatusOut, ChatResponse, MessageOut, PendingActionOut, ToolInfo
from app.schemas.core import AppSettingsSchema
from app.services import activity_service
from app.services.dashboard_service import build_dashboard
from app.services.settings_service import get_settings

logger = logging.getLogger("jarvis.ai")

APPROVAL_ACTIONS = {name for name, spec in TOOLS.items() if spec.requires_approval}


def _system_prompt(name: str, settings: AppSettingsSchema) -> str:
    now_local = datetime.now(get_zone(settings.timezone))
    return (
        f"You are {name}, a private desktop assistant running on the user's Windows machine. "
        f"The user's time zone is {settings.timezone}; local time is "
        f"{now_local.strftime('%A %d %B %Y, %H:%M')}.\n\n"
        "Operating rules:\n"
        "1. Use tools to read or change the user's data. Never invent tasks, reminders, events, "
        "notes, workflow results, or notification deliveries.\n"
        "2. Only state that an action happened if a tool result confirms it. "
        "If a tool fails, say so plainly and include the reason.\n"
        "3. Some tools require explicit approval. If you call one, the system pauses it. "
        "Tell the user what needs approval; do not pretend it ran.\n"
        "4. Ask a short follow-up question when essential details are missing (time, target, "
        "application name).\n"
        "5. Be concise and direct. Prefer concrete next actions over motivational filler.\n"
        "6. Never ask for passwords, tokens, or full file contents. Never propose shell commands.\n"
        "7. Times in tool arguments must be ISO-8601 in UTC when the user gave a local time; "
        "use the context below to resolve relative dates.\n"
    )


def _context(db: Session, settings: AppSettingsSchema) -> str:
    dashboard = build_dashboard(db)
    tasks = [
        {
            "id": t.id,
            "title": t.title,
            "priority": t.priority,
            "status": t.status,
            "due_at": t.due_at.isoformat() if t.due_at else None,
        }
        for t in list(dashboard.priority_tasks) + list(dashboard.overdue_tasks)
    ]
    deduped: dict[str, dict] = {t["id"]: t for t in tasks}
    from app.repositories.tasks import TaskRepository

    repo = TaskRepository(db)
    open_items, _ = repo.list(status=["todo", "in_progress", "blocked"], sort="priority", limit=12)
    for task in open_items:
        deduped.setdefault(
            task.id,
            {
                "id": task.id,
                "title": task.title,
                "priority": task.priority,
                "status": task.status,
                "due_at": task.due_at.isoformat() if task.due_at else None,
            },
        )
    payload = {
        "open_tasks": list(deduped.values())[:15],
        "counts": dashboard.counts.model_dump(),
        "upcoming_reminders": [
            {"id": r.id, "title": r.title, "remind_at": r.remind_at.isoformat()}
            for r in dashboard.upcoming_reminders[:6]
        ],
        "events_today": [
            {"title": e.title, "start_at": e.start_at.isoformat(), "end_at": e.end_at.isoformat()}
            for e in dashboard.events_today[:6]
        ],
        "habits": [
            {"name": h.name, "streak": h.streak, "done_today": h.completed_today}
            for h in dashboard.habits[:10]
        ],
        "focus_session": {
            "label": dashboard.focus_session.label,
            "state": dashboard.focus_session.state,
            "planned_minutes": dashboard.focus_session.planned_minutes,
        }
        if dashboard.focus_session
        else None,
    }
    return "Current database context (authoritative, do not contradict it):\n" + json.dumps(
        payload, ensure_ascii=False
    )


def _history(db: Session, conversation_id: str, limit: int = 10) -> list[dict[str, str]]:
    messages = list(
        db.scalars(
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.role.in_(["user", "assistant"]),
                Message.status != "error",
            )
            .order_by(Message.created_at.desc())
            .limit(limit)
        )
    )
    return [{"role": m.role, "content": m.content} for m in reversed(messages) if m.content.strip()]


def _describe(tool: str, args: dict[str, Any]) -> str:
    if tool == "open_application":
        return f"Open approved application “{args.get('name')}”"
    if tool == "open_project":
        return f"Open project “{args.get('path')}” in VS Code"
    if tool == "run_workflow":
        return f"Run workflow {args.get('workflow_id')}"
    if tool == "delete_task":
        return f"Delete task {args.get('task_id')}"
    if tool == "delete_calendar_event":
        scope = "the whole series" if args.get("delete_series") else "this event"
        return f"Delete {scope}: {args.get('event_id')}"
    if tool == "delete_journal_entry":
        return f"Delete the journal entry for {args.get('date')}"
    return f"Run {tool}"


def get_conversation(db: Session, conversation_id: str) -> Conversation:
    conversation = db.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
    return conversation


def create_conversation(db: Session, title: str = "New conversation") -> Conversation:
    conversation = Conversation(title=title[:200] or "New conversation")
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    return conversation


def list_conversations(db: Session, limit: int = 50) -> list[Conversation]:
    return list(
        db.scalars(
            select(Conversation)
            .where(Conversation.archived.is_(False))
            .order_by(Conversation.updated_at.desc())
            .limit(limit)
        )
    )


def delete_conversation(db: Session, conversation_id: str) -> None:
    conversation = get_conversation(db, conversation_id)
    db.delete(conversation)
    db.commit()


def list_messages(db: Session, conversation_id: str) -> list[Message]:
    return list(
        db.scalars(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.asc())
        )
    )


def list_pending_actions(
    db: Session, conversation_id: str, status_filter: str = "pending"
) -> list[PendingAction]:
    stmt = select(PendingAction).where(PendingAction.conversation_id == conversation_id)
    if status_filter and status_filter != "all":
        stmt = stmt.where(PendingAction.status == status_filter)
    return list(db.scalars(stmt.order_by(PendingAction.created_at.asc())))


def _finish_assistant_message(
    db: Session,
    conversation: Conversation,
    *,
    content: str,
    tool_records: list[dict[str, Any]],
    pending: list[PendingAction],
    message_status: str,
) -> Message:
    message = Message(
        conversation_id=conversation.id,
        role="assistant",
        content=content,
        tool_calls=tool_records or None,
        status=message_status,
    )
    db.add(message)
    conversation.message_count += 2
    conversation.updated_at = utcnow()
    db.commit()
    db.refresh(message)
    return message


def send_message(db: Session, conversation_id: str, text: str) -> ChatResponse:
    conversation = get_conversation(db, conversation_id)
    settings = get_settings(db)
    provider, mode_note = build_provider(settings)

    user_message = Message(conversation_id=conversation.id, role="user", content=text)
    db.add(user_message)
    if conversation.title in {"New conversation", ""}:
        conversation.title = text.strip()[:80] or "New conversation"
    conversation.updated_at = utcnow()
    db.flush()

    tool_records: list[dict[str, Any]] = []
    pending_actions: list[PendingAction] = []
    content = ""
    calls: list[dict[str, Any]] = []

    if provider is None:
        turn = mock_assistant.reply(db, text, settings)
        content = turn.content
        calls = turn.tool_calls
    else:
        messages = [
            {"role": "system", "content": _system_prompt(settings.assistant_name, settings)}
        ]
        messages.append({"role": "system", "content": _context(db, settings)})
        messages.extend(_history(db, conversation.id, limit=10))
        messages.append({"role": "user", "content": text})
        try:
            result = provider.complete(messages, tool_schemas())
            content = result.content
            calls = result.tool_calls
        except ProviderError as exc:
            logger.warning("AI provider error: %s", exc)
            message = Message(
                conversation_id=conversation.id,
                role="assistant",
                content=(
                    f"The AI provider failed: {exc}. No changes were made. "
                    "You can retry, or switch to the offline assistant in Settings."
                ),
                status="error",
                tool_calls=None,
            )
            db.add(message)
            conversation.message_count += 2
            db.commit()
            db.refresh(message)
            return ChatResponse(
                message=MessageOut.model_validate(message),
                pending_actions=[],
                conversation_id=conversation.id,
            )

    for call in calls[:4]:
        name = str(call.get("name") or "")
        arguments = call.get("arguments") or {}
        spec = TOOLS.get(name)
        if spec is None:
            tool_records.append(
                {
                    "name": name,
                    "arguments": arguments,
                    "status": "rejected",
                    "detail": "Unknown tool",
                }
            )
            continue
        try:
            validate_tool_call(name, arguments)
        except Exception as exc:  # noqa: BLE001
            tool_records.append(
                {
                    "name": name,
                    "arguments": arguments,
                    "status": "invalid",
                    "detail": str(exc),
                }
            )
            continue

        if spec.requires_approval:
            action = PendingAction(
                conversation_id=conversation.id,
                tool=name,
                args=arguments,
                description=_describe(name, arguments),
                requires_approval=True,
                status="pending",
            )
            db.add(action)
            db.flush()
            pending_actions.append(action)
            tool_records.append(
                {
                    "name": name,
                    "arguments": arguments,
                    "status": "pending_approval",
                    "detail": action.description,
                    "pending_action_id": action.id,
                }
            )
        else:
            outcome = execute_tool(db, name, arguments)
            tool_records.append(
                {
                    "name": name,
                    "arguments": arguments,
                    "status": "succeeded" if outcome.get("ok") else "failed",
                    "detail": outcome.get("message"),
                }
            )

    if provider is not None and tool_records and not pending_actions:
        summary_messages = [
            {"role": "system", "content": _system_prompt(settings.assistant_name, settings)},
            {
                "role": "system",
                "content": "Verified tool results:\n"
                + json.dumps(tool_records, ensure_ascii=False)
                + "\nAnswer the user's request using only these results.",
            },
            {"role": "user", "content": text},
        ]
        try:
            follow_up = provider.complete(summary_messages, [])
            if follow_up.content.strip():
                content = follow_up.content
        except ProviderError as exc:
            logger.info("Follow-up summary failed; using tool messages: %s", exc)

    if not pending_actions:
        executed = [r for r in tool_records if r["status"] in {"succeeded", "failed", "invalid"}]
        if executed:
            lines = []
            if content.strip():
                lines.append(content.strip())
            for record in executed:
                prefix = "✓" if record["status"] == "succeeded" else "✗"
                lines.append(f"{prefix} {record.get('detail') or record['name']}")
            content = "\n".join(lines)
        elif not content.strip():
            content = (
                "I don't have a verified action for that request. "
                "Could you clarify what you want me to do?"
            )
    else:
        lines = [content.strip()] if content.strip() else []
        for action in pending_actions:
            lines.append(f"Approval required: {action.description}. Review and approve it below.")
        if not lines:
            lines.append("This action needs your approval before it can run.")
        content = "\n".join(lines)

    assistant_message = _finish_assistant_message(
        db,
        conversation,
        content=content,
        tool_records=tool_records,
        pending=pending_actions,
        message_status="pending_approval" if pending_actions else "complete",
    )

    activity_service.log(
        db,
        "ai.message",
        f"{settings.assistant_name} handled a request"
        + (f" ({len(pending_actions)} pending approvals)" if pending_actions else ""),
        detail={"provider": mode_note, "tools": [r["name"] for r in tool_records]},
        entity_type="conversation",
        entity_id=conversation.id,
    )
    db.commit()

    return ChatResponse(
        message=MessageOut.model_validate(assistant_message),
        pending_actions=[PendingActionOut.model_validate(a) for a in pending_actions],
        conversation_id=conversation.id,
    )


def resolve_action(db: Session, action_id: str, approve: bool) -> ChatResponse:
    action = db.get(PendingAction, action_id)
    if action is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Pending action not found")
    if action.status != "pending":
        raise HTTPException(status.HTTP_409_CONFLICT, f"Action already {action.status}")
    conversation = get_conversation(db, action.conversation_id)

    if not approve:
        action.status = "rejected"
        action.result = {"ok": False, "message": "Rejected by user"}
        content = f"Cancelled: {action.description}. Nothing was executed."
    else:
        outcome = execute_tool(db, action.tool, action.args)
        action.status = "approved" if outcome.get("ok") else "failed"
        action.result = outcome
        if outcome.get("ok"):
            content = f"✓ {outcome.get('message')}"
        else:
            content = (
                f"✗ The action failed: {outcome.get('message')}. It was not reported as successful."
            )
    action.resolved_at = utcnow()

    message = Message(
        conversation_id=conversation.id,
        role="assistant",
        content=content,
        status="complete",
        tool_calls=[
            {
                "name": action.tool,
                "arguments": action.args,
                "status": action.status,
                "detail": (action.result or {}).get("message"),
            }
        ],
    )
    db.add(message)
    conversation.message_count += 1
    conversation.updated_at = utcnow()
    activity_service.log(
        db,
        "ai.action_resolved",
        f"{'Approved' if approve else 'Rejected'}: {action.description}",
        detail={"tool": action.tool, "status": action.status},
        entity_type="pending_action",
        entity_id=action.id,
    )
    db.commit()
    db.refresh(message)

    return ChatResponse(
        message=MessageOut.model_validate(message),
        pending_actions=[],
        conversation_id=conversation.id,
    )


def ai_status(db: Session) -> AiStatusOut:
    from app.core.config import settings as env_settings
    from app.services.settings_service import get_settings

    app_settings = get_settings(db)
    provider = app_settings.ai_provider
    live = provider in {"openai", "anthropic", "ollama"} and (
        bool(env_settings.ai_api_key) or provider == "ollama"
    )
    return AiStatusOut(
        provider=provider,
        model=app_settings.ai_model,
        mode="live" if live else "mock",
        tools=[
            ToolInfo(
                name=spec.name,
                description=spec.description,
                requires_approval=spec.requires_approval,
                parameters=spec.params.model_json_schema(),
            )
            for spec in TOOLS.values()
        ],
        note=None
        if live
        else (
            "Offline assistant active. Configure a provider and AI_API_KEY "
            "to enable free-form conversation."
        ),
    )
