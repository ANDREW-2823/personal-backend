from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.security import require_token
from app.db.session import get_db
from app.schemas.insights import BriefingOut, DashboardOut, InsightsOut
from app.services import briefing_service
from app.services.dashboard_service import build_dashboard, build_insights

router = APIRouter(tags=["productivity"], dependencies=[Depends(require_token)])


@router.get("/dashboard", response_model=DashboardOut)
def dashboard(db: Session = Depends(get_db)) -> DashboardOut:
    return build_dashboard(db)


@router.get("/insights", response_model=InsightsOut)
def insights(
    days: int = Query(default=7, ge=1, le=90), db: Session = Depends(get_db)
) -> InsightsOut:
    return build_insights(db, range_days=days)


@router.get("/briefing/{kind}", response_model=BriefingOut)
def briefing(kind: str, db: Session = Depends(get_db)) -> BriefingOut:
    if kind not in {"morning", "evening"}:
        kind = "morning"
    return briefing_service.build_briefing(db, kind)


@router.post("/briefing/{kind}/deliver", response_model=BriefingOut)
def deliver_briefing(kind: str, db: Session = Depends(get_db)) -> BriefingOut:
    if kind not in {"morning", "evening"}:
        kind = "morning"
    return briefing_service.deliver_briefing(db, kind)
