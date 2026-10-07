"""Load balancer access logs: counts by status, paths and single requests (ALB logs from S3)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .errors import ApiError
from .identity import User, current_user
from .lb_logs import Access, LbQuery

router = APIRouter(prefix="/api/v1/lb", tags=["load balancers"])


class LbBody(BaseModel):
    start: str | int = Field("now-1h", description="'now-1h', 'now-7d', ISO date-time (UTC unless it has an "
                             "offset) or epoch milliseconds")
    end: str | int = Field("now", description="Same formats as start")
    lbs: list[str] = Field(default_factory=list, max_length=50, description="Load balancer names (default: all)")
    targetGroups: list[str] = Field(default_factory=list, max_length=200,
                                    description="Target group names; '-' = requests with no target group")
    domains: list[str] = Field(default_factory=list, max_length=200)
    statusClasses: list[Literal["2xx", "3xx", "4xx", "5xx", "other"]] = Field(default_factory=list)
    statusCodes: list[int] = Field(default_factory=list, max_length=50)
    source: Literal["app", "lb"] | None = Field(None, description="'app' = the target answered; 'lb' = the load "
                                                "balancer answered itself (no target response)")
    methods: list[str] = Field(default_factory=list, max_length=20)
    path: str = Field("", max_length=500, description="Path contains this (* = anything)")
    pathGroup: str = Field("", max_length=500, description="Exact grouped path, as /_paths returns it")
    client: str = Field("", max_length=64, description="Client IP starts with")
    target: str = Field("", max_length=64, description="Target ip:port starts with")
    minTargetSeconds: float | None = Field(None, ge=0, description="Target took at least this long")
    q: str = Field("", max_length=500, description="Words in the URL, user agent, trace id, error reason, IPs")

    def query(self) -> LbQuery:
        return LbQuery(start=self.start, end=self.end, lbs=self.lbs, target_groups=self.targetGroups,
                       domains=self.domains, status_classes=list(self.statusClasses), status_codes=self.statusCodes,
                       source=self.source, methods=self.methods, path=self.path, path_group=self.pathGroup,
                       client=self.client, target=self.target, min_target_seconds=self.minTargetSeconds, q=self.q)


class PathsBody(LbBody):
    sort: Literal["errors", "requests", "slow"] = "errors"
    limit: int = Field(50, ge=1, le=200)


class RequestsBody(LbBody):
    offset: int = Field(0, ge=0, le=1_000_000)
    size: int = Field(100, ge=1, le=500)
    order: Literal["desc", "asc"] = "desc"


class ExportBody(LbBody):
    format: Literal["csv", "json", "ndjson"] = "csv"
    limit: int = Field(10_000, ge=1)


def _service(request: Request):
    if not request.app.state.settings.lb_enabled:
        raise ApiError(404, "LB_DISABLED", "The load balancer pages are turned off (LB_ENABLED=false)")
    return request.app.state.lb


def _access(user: User) -> Access | None:
    return None if user.admin else Access.for_rules(user.clusters)


@router.get("", summary="Load balancers and target groups you can see (last 24 hours), converter state")
def overview(request: Request, user: User = Depends(current_user)):
    return _service(request).overview(_access(user))


@router.post("/_summary", summary="Totals, requests over time and the target group table")
def summary(body: LbBody, request: Request, user: User = Depends(current_user)):
    return _service(request).summary(body.query(), _access(user))


@router.post("/_paths", summary="Requests grouped by method and path (store names, ids and numbers grouped)")
def paths(body: PathsBody, request: Request, user: User = Depends(current_user)):
    return _service(request).paths(body.query(), _access(user), body.sort, body.limit)


@router.post("/_requests", summary="Single requests, newest first")
def requests_(body: RequestsBody, request: Request, user: User = Depends(current_user)):
    return _service(request).requests(body.query(), _access(user), body.offset, body.size, body.order)


@router.post("/_export", summary="Download single requests (CSV, JSON, NDJSON)")
def export(body: ExportBody, request: Request, user: User = Depends(current_user)):
    stream, rows = _service(request).export(body.query(), _access(user), body.format, body.limit)
    request.app.state.events.write({"action": "LB_EXPORT", "actor": user.username,
                                    "requestId": request.state.request_id, "start": body.start, "end": body.end,
                                    "format": body.format, "rows": rows})
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    media = {"csv": "text/csv; charset=utf-8", "json": "application/json",
             "ndjson": "application/x-ndjson"}[body.format]
    return StreamingResponse(stream, media_type=media, headers={
        "Content-Disposition": f'attachment; filename="lb-requests-{stamp}.{body.format}"',
        "X-Export-Rows": str(rows), "Cache-Control": "no-store",
        "Access-Control-Expose-Headers": "X-Export-Rows, Content-Disposition"})
