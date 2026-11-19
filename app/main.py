from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.ai.service import ai_status
from app.api.v1.router import router as v1_router
from app.core.config import settings
from app.core.logging import configure_logging
from app.core.timeutils import utcnow
from app.db.session import SessionLocal, engine
from app.scheduler.service import scheduler_service
from app.schemas.insights import HealthOut
from app.services.desktop_bridge import desktop_hub

logger = logging.getLogger("jarvis")


class UnhandledErrorMiddleware:
    """Turn uncaught HTTP errors into a safe JSON 500.

    This middleware is installed *inside* CORSMiddleware so the error response
    still carries CORS headers. That matters for the packaged renderer, which
    loads from ``file://`` and otherwise reports a misleading network failure
    instead of the real server error.
    """

    def __init__(self, app) -> None:  # noqa: ANN001 - ASGI callable
        self.app = app

    async def __call__(self, scope, receive, send) -> None:  # noqa: ANN001
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False

        async def tracked_send(message) -> None:  # noqa: ANN001
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, tracked_send)
        except Exception:  # noqa: BLE001 - last-resort guard, logged with traceback
            logger.exception("Unhandled error on %s %s", scope.get("method"), scope.get("path"))
            if response_started:
                raise
            response = JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={
                    "detail": (
                        "JARVIS hit an unexpected internal error. "
                        "The backend log contains the full traceback."
                    )
                },
            )
            await response(scope, receive, send)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    desktop_hub.set_loop(asyncio.get_running_loop())
    if settings.scheduler_enabled:
        scheduler_service.start()
    logger.info("JARVIS backend started on %s:%s", settings.api_host, settings.api_port)
    yield
    scheduler_service.stop()
    engine.dispose()
    logger.info("JARVIS backend stopped")


app = FastAPI(
    title="JARVIS Personal API",
    version=settings.app_version,
    docs_url="/docs" if settings.environment == "development" else None,
    redoc_url=None,
    lifespan=lifespan,
)

# Added first so CORSMiddleware wraps it; the safe 500 response then keeps
# the CORS headers the renderer needs to read the error body.
app.add_middleware(UnhandledErrorMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-Jarvis-Token"],
)


@app.exception_handler(IntegrityError)
async def integrity_error_handler(request: Request, exc: IntegrityError) -> JSONResponse:
    logger.warning("Database constraint violation on %s", request.url.path)
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={
            "detail": "That operation conflicts with existing data (duplicate or constraint)."
        },
    )


@app.exception_handler(SQLAlchemyError)
async def database_error_handler(request: Request, exc: SQLAlchemyError) -> JSONResponse:
    logger.exception("Database error on %s", request.url.path)
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": "The database is unavailable or returned an error. Check PostgreSQL."},
    )


def _database_health() -> tuple[str, str | None]:
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return "ok", None
    except Exception as exc:  # noqa: BLE001
        return "error", type(exc).__name__


@app.get("/health", response_model=HealthOut, tags=["health"])
def health() -> HealthOut:
    db_state, detail = _database_health()
    ai_mode = "mock"
    if db_state == "ok":
        session = SessionLocal()
        try:
            ai_mode = ai_status(session).mode
        except Exception:
            ai_mode = "mock"
        finally:
            session.close()
    else:
        from app.core.config import settings as cfg

        ai_mode = (
            "live"
            if cfg.ai_provider in {"openai", "anthropic", "ollama"}
            and (bool(cfg.ai_api_key) or cfg.ai_provider == "ollama")
            else "mock"
        )
    return HealthOut(
        status="ok" if db_state == "ok" and scheduler_service.running else "degraded",
        version=settings.app_version,
        database=db_state,
        database_detail=detail,
        scheduler="running" if scheduler_service.running else "stopped",
        ai_mode=ai_mode,
        time=utcnow(),
    )


app.include_router(v1_router)
