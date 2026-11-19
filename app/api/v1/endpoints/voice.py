from __future__ import annotations

import asyncio
import json
import logging
import time

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.ratelimit import limiter
from app.core.security import require_token, token_is_valid
from app.services import voice_service

logger = logging.getLogger("jarvis.voice")

router = APIRouter(prefix="/voice", tags=["voice"])


class SpeakIn(BaseModel):
    text: str = Field(min_length=1, max_length=2_000)
    voice: str | None = Field(default=None, max_length=80)
    language: str | None = Field(default=None, max_length=16)
    rate: int = Field(default=0, ge=-50, le=100, description="Percent change, e.g. -10")
    volume: int = Field(default=0, ge=-100, le=100, description="Percent change, e.g. +20")


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "local"


@router.get("/status", dependencies=[Depends(require_token)])
def status() -> dict:
    return voice_service.status()


@router.get("/voices", dependencies=[Depends(require_token)])
def voices() -> dict:
    return {"voices": voice_service.voice_catalog(), "default": settings.tts_voice}


@router.post("/transcribe", dependencies=[Depends(require_token)])
async def transcribe(
    request: Request,
    audio: UploadFile = File(...),
    language: str | None = Form(default=None),
) -> dict:
    limiter.check(f"stt:{_client_key(request)}", limit=30, window_seconds=60)
    content_type = (audio.content_type or "").lower()
    if content_type and not (
        content_type.startswith("audio/")
        or content_type in {"video/webm", "application/octet-stream"}
    ):
        raise HTTPException(status_code=415, detail="Upload an audio recording.")
    data = await audio.read(settings.max_audio_bytes + 1)
    if len(data) > settings.max_audio_bytes:
        raise HTTPException(status_code=413, detail="The recording is too large.")
    try:
        return await asyncio.to_thread(voice_service.transcribe, data, language=language)
    except voice_service.VoiceError as exc:
        status_code = 422 if exc.code in {"empty_audio", "audio_too_large"} else 503
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@router.post("/speak", dependencies=[Depends(require_token)])
async def speak(payload: SpeakIn, request: Request) -> Response:
    limiter.check(f"tts:{_client_key(request)}", limit=60, window_seconds=60)
    try:
        audio, voice = await voice_service.synthesize(
            payload.text,
            voice=payload.voice,
            language=payload.language,
            rate=payload.rate,
            volume=payload.volume,
        )
    except voice_service.VoiceError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return Response(
        content=audio,
        media_type="audio/mpeg",
        headers={
            "Cache-Control": "no-store",
            "X-Jarvis-Voice": voice,
            "Content-Disposition": "inline",
        },
    )


@router.websocket("/wake")
async def wake_socket(websocket: WebSocket, token: str | None = Query(default=None)) -> None:
    if not token or not token_is_valid(token):
        await websocket.close(code=4401)
        return
    await websocket.accept()
    threshold = settings.wake_threshold
    cooldown = max(0.5, settings.wake_cooldown_seconds)
    last_detection = 0.0
    buffer = bytearray()
    try:
        await websocket.send_json(
            {
                "type": "initializing",
                "detail": "Loading the local wake-word model…",
            }
        )
        await asyncio.to_thread(voice_service.warm_wake_model)
        await websocket.send_json(voice_service.wake_ready_message())
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            if message.get("bytes") is not None:
                buffer.extend(message["bytes"])
                while len(buffer) >= voice_service.WAKE_FRAME_BYTES:
                    frame = bytes(buffer[: voice_service.WAKE_FRAME_BYTES])
                    del buffer[: voice_service.WAKE_FRAME_BYTES]
                    score = await asyncio.to_thread(voice_service.feed_wake_frame, frame)
                    now = time.monotonic()
                    if score >= threshold and now - last_detection >= cooldown:
                        last_detection = now
                        await websocket.send_json(
                            {
                                "type": "wake",
                                "model": settings.wake_model,
                                "score": round(score, 4),
                                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            }
                        )
                continue
            raw = message.get("text")
            if not raw:
                continue
            try:
                command = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind = command.get("type")
            if kind == "config":
                value = command.get("threshold")
                if isinstance(value, (int, float)):
                    threshold = max(0.3, min(0.95, float(value)))
                await websocket.send_json({"type": "configured", "threshold": threshold})
            elif kind == "flush":
                buffer.clear()
                await asyncio.to_thread(voice_service.reset_wake_model)
                await websocket.send_json({"type": "flushed"})
            elif kind == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    except voice_service.VoiceError as exc:
        try:
            await websocket.send_json({"type": "error", "code": exc.code, "detail": str(exc)})
        except Exception:  # noqa: BLE001
            pass
        logger.warning("Wake socket unavailable: %s", exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Wake socket error: %s", exc)
    finally:
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass
