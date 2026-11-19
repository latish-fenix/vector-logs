"""A/B test page: sessions per store and page type and their session IDs (from scripts/ab_session_counts.py)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from .errors import ApiError
from .identity import User, current_user
from .routes_ops import _ms

router = APIRouter(prefix="/api/v1/abtest", tags=["a/b test"])


def _ab(request: Request):
    ab = getattr(request.app.state, "abtest", None)
    if ab is None:
        raise ApiError(404, "ABTEST_DISABLED", "The A/B test page is turned off (OPS_ENABLED=false)")
    return ab


@router.get("/summary", summary="Stores with their sessions and bad session IDs between two times; with `store`: its "
                                "page-type series, session ID tables (session_history and cop-derrh) and A/B variants")
def summary(request: Request,
            start: str = Query(..., alias="from", description="Start: epoch ms or ISO date-time (no offset = UTC)"),
            end: str = Query(..., alias="to", description="End (exclusive): epoch ms or ISO date-time; capped at now"),
            zone: str = Query("UTC", description="IST, PST, EST or UTC: the chart buckets start on round times there"),
            store: str | None = Query(None, description="Store (as listed in stores) for the charts and tables"),
            user: User = Depends(current_user)):
    return _ab(request).summary(_ms(start), _ms(end), zone, store or None)


@router.get("/ids", summary="Entries with a bad session ID (order confirmations of session_history, every cop-derrh "
                            "entry), newest first")
def ids(request: Request,
        store: str = Query(...),
        start: str = Query(..., alias="from"),
        end: str = Query(..., alias="to"),
        source: str = Query("session", description="session (session_history) or cop (cop-derrh)"),
        page: str | None = Query(None, description="Page type, e.g. OC"),
        category: str | None = Query(None, description="NA, ORDER_LOOKUP_CALL, xyz123, empty, missing, numbers, uuid, other … or bad (all of them)"),
        limit: int = Query(500, ge=1, le=5000),
        user: User = Depends(current_user)):
    return _ab(request).id_list(_ms(start), _ms(end), store, source, page or None, category or None, limit)


@router.get("/status", summary="Where the numbers come from and the collector's last run")
def status(request: Request, user: User = Depends(current_user)):
    return _ab(request).status()
