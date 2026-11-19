from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class DesktopAgentHub:
    """Bridges backend workflow requests to the Electron desktop allowlist.

    The backend never touches the operating system directly. It emits a
    structured action request over an authenticated WebSocket; Electron's main
    process validates it against user-approved permissions and returns a
    structured result. Requests time out instead of hanging a scheduler tick.
    """

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    @property
    def connected(self) -> bool:
        return bool(self._clients)

    async def accept(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.add(ws)
        logger.info("Desktop agent connected")

    def disconnect(self, ws: WebSocket) -> None:
        self._clients.discard(ws)
        logger.info("Desktop agent disconnected")

    def resolve(self, action_id: str, result: dict[str, Any]) -> None:
        future = self._pending.pop(action_id, None)
        if future and not future.done():
            future.set_result(result)

    async def request(
        self,
        action: str,
        *,
        target: str | None = None,
        args: list[str] | None = None,
        payload: dict[str, Any] | None = None,
        timeout: float = 25.0,
    ) -> dict[str, Any]:
        if not self._clients:
            return {
                "ok": False,
                "action": action,
                "detail": "Desktop agent is not connected (JARVIS desktop window may be closed).",
                "executed_at": None,
            }
        action_id = str(uuid.uuid4())
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[action_id] = future
        message = {
            "type": "action",
            "id": action_id,
            "request": {
                "type": action,
                "target": target,
                "args": args or [],
                "payload": payload or {},
            },
        }
        sent = False
        for ws in list(self._clients):
            try:
                await ws.send_json(message)
                sent = True
                break
            except Exception:
                self._clients.discard(ws)
        if not sent:
            self._pending.pop(action_id, None)
            return {
                "ok": False,
                "action": action,
                "detail": "Could not reach the desktop agent.",
                "executed_at": None,
            }
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError:
            self._pending.pop(action_id, None)
            return {
                "ok": False,
                "action": action,
                "detail": f"Desktop action timed out after {timeout:.0f}s.",
                "executed_at": None,
            }

    def request_sync(
        self,
        action: str,
        *,
        target: str | None = None,
        args: list[str] | None = None,
        payload: dict[str, Any] | None = None,
        timeout: float = 25.0,
    ) -> dict[str, Any]:
        """Thread-safe request used by APScheduler worker threads."""
        loop = self._loop
        if loop is None or not loop.is_running():
            return {
                "ok": False,
                "action": action,
                "detail": "Desktop bridge event loop is not available.",
                "executed_at": None,
            }
        if not self._clients:
            return {
                "ok": False,
                "action": action,
                "detail": "Desktop agent is not connected.",
                "executed_at": None,
            }
        future = asyncio.run_coroutine_threadsafe(
            self.request(action, target=target, args=args, payload=payload, timeout=timeout),
            loop,
        )
        try:
            return future.result(timeout=timeout + 5)
        except Exception as exc:
            return {
                "ok": False,
                "action": action,
                "detail": f"Desktop bridge error: {exc}",
                "executed_at": None,
            }


desktop_hub = DesktopAgentHub()
