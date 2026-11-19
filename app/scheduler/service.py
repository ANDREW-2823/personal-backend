from __future__ import annotations

import logging
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.timeutils import utcnow
from app.db.session import SessionLocal

logger = logging.getLogger("jarvis.scheduler")


class SchedulerService:
    """Scheduling abstraction.

    Reminders and workflows are persisted in PostgreSQL; APScheduler only
    provides the tick cadence. Swapping in a durable worker later requires
    replacing this class, not the domain logic.
    """

    def __init__(self) -> None:
        self._scheduler = BackgroundScheduler(timezone="UTC", daemon=True)
        self._started = False
        self.last_errors: dict[str, str] = {}

    @property
    def running(self) -> bool:
        return self._started and self._scheduler.running

    def start(
        self, *, reminder_seconds: int | None = None, workflow_seconds: int | None = None
    ) -> None:
        if self._started:
            return
        reminder_interval = reminder_seconds or settings.reminder_tick_seconds
        workflow_interval = workflow_seconds or settings.workflow_tick_seconds

        self._scheduler.add_job(
            self._reminder_job,
            IntervalTrigger(seconds=reminder_interval),
            id="reminder-tick",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        self._scheduler.add_job(
            self._workflow_job,
            IntervalTrigger(seconds=workflow_interval),
            id="workflow-tick",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        self._scheduler.add_job(
            self._routine_job,
            IntervalTrigger(seconds=60),
            id="routine-tick",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        self._scheduler.start()
        self._started = True
        logger.info(
            "Scheduler started (reminders=%ss, workflows=%ss)", reminder_interval, workflow_interval
        )

    def stop(self) -> None:
        if self._started:
            self._scheduler.shutdown(wait=False)
            self._started = False
            logger.info("Scheduler stopped")

    def _run(self, name: str, fn) -> None:
        session: Session = SessionLocal()
        try:
            fn(session)
        except Exception as exc:  # noqa: BLE001
            self.last_errors[name] = str(exc)
            logger.exception("Scheduler job %s failed: %s", name, exc)
            session.rollback()
        finally:
            session.close()

    def _reminder_job(self) -> None:
        from app.services import reminder_service

        self._run("reminders", lambda db: reminder_service.process_due(db))

    def _workflow_job(self) -> None:
        from app.automation import workflow_service

        self._run("workflows", lambda db: workflow_service.tick(db))

    def _routine_job(self) -> None:
        from app.services import briefing_service

        self._run("routines", lambda db: briefing_service.tick(db))

    def info(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "jobs": [job.id for job in self._scheduler.get_jobs()] if self._started else [],
            "errors": self.last_errors,
            "time": utcnow().isoformat(),
        }


scheduler_service = SchedulerService()
