"""Ops dashboard: API Gateway calls per stage and orders processed, today against the same day last week."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from .errors import ApiError
from .identity import User, current_user

router = APIRouter(prefix="/api/v1/ops", tags=["ops dashboard"])


def _ops(request: Request):
    ops = getattr(request.app.state, "ops", None)
    if ops is None:
        raise ApiError(404, "OPS_DISABLED", "The ops dashboard is turned off (OPS_ENABLED=false)")
    return ops


@router.get("/summary", summary="Calls and orders for one day (midnight to midnight in `zone`): totals, per minute, per stage, per store, vs last week")
def summary(request: Request,
            day: str | None = Query(None, description="YYYY-MM-DD in `zone`; default today there"),
            window: int = Query(60, ge=5, le=360, description="Minutes the stage status and p95 look back"),
            zone: str = Query("UTC", description="Time zone of the day: IST, PST, EST or UTC (PST/EST follow daylight saving)"),
            user: User = Depends(current_user)):
    return _ops(request).summary(day, window, zone)


@router.get("/health", summary="Each stage's last N minutes against the same minutes last week, worst first")
def health(request: Request, minutes: int = Query(60, ge=5, le=1440), user: User = Depends(current_user)):
    return _ops(request).health(minutes)


@router.get("/history", summary="Per-minute calls, errors, p95 and orders of one day (optionally one stage / service / store)")
def history(request: Request,
            day: str | None = Query(None, description="YYYY-MM-DD in `zone`; default today there"),
            zone: str = Query("UTC", description="Time zone of the day: IST, PST, EST or UTC (PST/EST follow daylight saving)"),
            stage: str | None = Query(None, description="'<api>/<stage>' as listed in stages"),
            service: str | None = Query(None, description="Order service (OMS, OMS-11, CRS …)"),
            store: str | None = Query(None, description="Store as listed in stores"),
            user: User = Depends(current_user)):
    return _ops(request).history(day, stage or None, service or None, store or None, zone)


@router.get("/status", summary="Where the numbers come from and whether the last updates worked")
def status(request: Request, user: User = Depends(current_user)):
    return _ops(request).status()
