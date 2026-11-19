from __future__ import annotations

import json
import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from app.core.security import require_token, token_is_valid
from app.services.desktop_bridge import desktop_hub

logger = logging.getLogger("jarvis.desktop")

router = APIRouter(prefix="/desktop", tags=["desktop"])

DesktopAction = Literal[
    "launch_app",
    "open_url",
    "open_folder",
    "open_in_vscode",
    "run_script",
    "create_directory",
    "list_directory",
    "move_file",
    "notify",
]


class DesktopActionIn(BaseModel):
    type: DesktopAction
    target: str | None = Field(default=None, max_length=2048)
    args: list[str] = Field(default_factory=list, max_length=20)
    payload: dict[str, Any] = Field(default_factory=dict)


class DesktopActionOut(BaseModel):
    ok: bool
    action: str
    detail: str
    output: str | None = None
    data: Any | None = None
    duration_ms: int = 0
    executed_at: str | None = None


@router.get("/status", dependencies=[Depends(require_token)])
def desktop_status() -> dict:
    return {
        "agent_connected": desktop_hub.connected,
        "note": "The Electron main process enforces the OS-level allowlist.",
    }


@router.post("/execute", response_model=DesktopActionOut, dependencies=[Depends(require_token)])
async def execute_action(payload: DesktopActionIn) -> DesktopActionOut:
    result = await desktop_hub.request(
        payload.type,
        target=payload.target,
        args=payload.args,
        payload=payload.payload,
    )
    return DesktopActionOut(**result)


@router.websocket("/agent")
async def desktop_agent(websocket: WebSocket, token: str | None = Query(default=None)) -> None:
    if not token or not token_is_valid(token):
        await websocket.close(code=4401)
        return
    await desktop_hub.accept(websocket)
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if message.get("type") == "result" and message.get("id"):
                result = message.get("result") or {}
                desktop_hub.resolve(str(message["id"]), result)
    except WebSocketDisconnect:
        desktop_hub.disconnect(websocket)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Desktop agent socket error: %s", exc)
        desktop_hub.disconnect(websocket)
