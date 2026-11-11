"""Ops dashboard: API Gateway calls per stage and orders processed, today against the same day last week."""
from __future__ import annotations

from datetime import datetime, timezone

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


@router.get("/range", summary="Every stage's calls, 4xx, 5xx, p50 / p95, backend p95 between two times (≤ 31 days), totals over time and orders")
def range_(request: Request,
           start: str = Query(..., alias="from", description="Start: epoch ms or ISO date-time (no offset = UTC)"),
           end: str = Query(..., alias="to", description="End (exclusive): epoch ms or ISO date-time; capped at now"),
           zone: str = Query("UTC", description="IST, PST, EST or UTC: the chart buckets start on round times there"),
           stage: str | None = Query(None, description="'<api>/<stage>': the totals over time for one stage (the table always has every stage)"),
           service: str | None = Query(None, description="Order service for the orders over time"),
           store: str | None = Query(None, description="Store for the orders over time"),
           user: User = Depends(current_user)):
    return _ops(request).time_range(_ms(start), _ms(end), zone, stage or None, service or None, store or None)


def _ms(v: str) -> int:
    v = v.strip()
    if v.lstrip("-").isdigit():
        return int(v)
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        raise ApiError(400, "INVALID_TIME", f"'{v}' is not a time (epoch ms or ISO date-time)")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


@router.get("/status", summary="Where the numbers come from and whether the last updates worked")
def status(request: Request, user: User = Depends(current_user)):
    return _ops(request).status()
