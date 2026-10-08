"""Signed-in user's API: who am I, which clusters, search / read / export logs, saved searches."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .errors import bad_request
from .identity import User, current_user, require_cluster
from .logs import Filter, SearchSpec, zone_time
from .repos import CLUSTER_RE

router = APIRouter(prefix="/api/v1", tags=["logs"])


class FilterBody(BaseModel):
    field: str = Field(..., max_length=128)
    op: Literal["is", "is_not", "one_of", "not_one_of", "contains", "not_contains", "exists",
                "not_exists", "gte", "lte", "between"]
    value: str | int | float | bool | None = None
    values: list[str | int | float | bool] | None = None
    gte: str | int | float | None = None
    lte: str | int | float | None = None


class SearchBody(BaseModel):
    start: str | int = Field("now-15m", description="'now-15m', 'now-7d', ISO date-time (UTC unless it has an "
                             "offset) or epoch milliseconds")
    end: str | int = Field("now", description="Same formats as start")
    query: str = Field("", max_length=2000,
                       description='Free text: words must all appear (case-insensitive) in the message, body, '
                                   'exception, class… "quoted phrase" matches exactly, -word excludes, '
                                   'field:value matches one column (* wildcard)')
    filters: list[FilterBody] = Field(default_factory=list, max_length=50)
    order: Literal["desc", "asc"] = "desc"

    def spec(self) -> SearchSpec:
        return SearchSpec(start=self.start, end=self.end, query=self.query, order=self.order,
                          filters=[Filter(**f.model_dump()) for f in self.filters])


class SearchPage(SearchBody):
    offset: int = Field(0, ge=0, le=100_000)
    size: int = Field(50, ge=1, le=500)
    aggregations: bool = Field(True, description="Also return the histogram and top values")


class ExportBody(SearchBody):
    format: Literal["csv", "json", "ndjson"] = "csv"
    columns: list[str] | None = Field(None, description="Columns to export (default: all)")
    limit: int = Field(10_000, ge=1)
    timeZone: str | None = Field(None, max_length=64,
                                 description="IANA zone (e.g. Asia/Kolkata) to write log_time in; default UTC")


class SavedBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    cluster: str = Field(..., max_length=128)
    params: str = Field("", max_length=8000, description="The console's URL query string")


def _event(request: Request, user: User, action: str, **fields) -> None:
    request.app.state.events.write({"action": action, "actor": user.username,
                                    "requestId": request.state.request_id, **fields})


def _describe(body: SearchBody) -> dict:
    return {"start": body.start, "end": body.end, "query": body.query,
            "filters": [f"{f.field} {f.op}" for f in body.filters]}


@router.get("/me", summary="Your account and access")
def me(request: Request, user: User = Depends(current_user)):
    rec = request.app.state.users.get(user.username) or {}
    s = request.app.state.settings
    return {"username": user.username, "admin": user.admin, "authMode": s.auth_mode,
            "usingGeneratedPassword": bool(rec.get("usingGeneratedPassword")),
            "lastLoginAt": rec.get("lastLoginAt"), "clusters": user.clusters,
            "limits": {"maxSearchHours": s.max_search_hours, "maxExportRows": s.max_export_rows}}


@router.get("/clusters", summary="Clusters whose logs you can read")
def clusters(request: Request, user: User = Depends(current_user)):
    names = request.app.state.logs.clusters()
    return {"items": [{"id": c} for c in names if user.can_view(c)]}


@router.post("/clusters/{cluster_id}/logs/_search", summary="Search one cluster's logs")
def search(cluster_id: str, body: SearchPage, request: Request, user: User = Depends(current_user)):
    require_cluster(user, cluster_id)
    res = request.app.state.logs.search(cluster_id, body.spec(), body.offset, body.size, body.aggregations)
    if body.offset == 0:
        _event(request, user, "LOGS_SEARCH", clusterId=cluster_id, **_describe(body),
               total=res["total"], files=res["files"], tookMs=res["tookMs"])
    return res


@router.get("/clusters/{cluster_id}/logs/_record", summary="One complete log line (the detail view)")
def record(cluster_id: str, request: Request, key: str = Query(..., max_length=1024),
           row: int = Query(..., ge=0), user: User = Depends(current_user)):
    require_cluster(user, cluster_id)
    return request.app.state.logs.record(cluster_id, key, row)


@router.post("/clusters/{cluster_id}/logs/_export", summary="Download matching log lines (CSV, JSON, NDJSON)")
def export(cluster_id: str, body: ExportBody, request: Request, user: User = Depends(current_user)):
    require_cluster(user, cluster_id)
    ex = request.app.state.logs.export(cluster_id, body.spec(), body.format, body.columns, body.limit,
                                       body.timeZone)
    _event(request, user, "LOGS_EXPORT", clusterId=cluster_id, **_describe(body), format=ex.fmt, rows=ex.rows,
           order=body.order, firstMs=ex.first_ms, lastMs=ex.last_ms)
    filename = _export_name(cluster_id, ex, body.order)
    media = {"csv": "text/csv; charset=utf-8", "json": "application/json",
             "ndjson": "application/x-ndjson"}[ex.fmt]
    return StreamingResponse(ex.stream, media_type=media, headers={
        "Content-Disposition": f'attachment; filename="{filename}"', "X-Export-Rows": str(ex.rows),
        "X-Export-Total": str(ex.total), "X-Export-First": str(ex.first_ms or ""),
        "X-Export-Last": str(ex.last_ms or ""),
        "Cache-Control": "no-store",
        "Access-Control-Expose-Headers": "X-Export-Rows, X-Export-Total, X-Export-First, X-Export-Last, "
                                         "Content-Disposition"})


def _export_name(cluster_id: str, ex, order: str) -> str:
    """logs-alpha-edds_2026-10-07_0857-0900_newest.json: the times the file covers, in its zone."""
    if ex.first_ms is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        return f"logs-{cluster_id}-{stamp}.{ex.fmt}"
    a, b = sorted([ex.first_ms, ex.last_ms])
    da, db = (zone_time(a, ex.zone, False), zone_time(b, ex.zone, False))
    day, hm_a = da[:10], da[11:16].replace(":", "")
    hm_b = (db[11:16] if db[:10] == day else db[:16].replace(" ", "_")).replace(":", "")
    zone = "UTC" if ex.zone is None else da[-6:].replace(":", "")
    return f"logs-{cluster_id}_{day}_{hm_a}-{hm_b}_{zone}_{'newest' if order == 'desc' else 'oldest'}-first.{ex.fmt}"


# -------------------------------------------------------------- saved searches
@router.get("/saved-searches", summary="Your saved searches")
def list_saved(request: Request, user: User = Depends(current_user)):
    return {"items": request.app.state.saved.list(user.username)}


@router.post("/saved-searches", status_code=201, summary="Save a search (same name = replace)")
def add_saved(body: SavedBody, request: Request, user: User = Depends(current_user)):
    if not CLUSTER_RE.match(body.cluster):
        raise bad_request("INVALID_CLUSTER", f"'{body.cluster}' is not a valid cluster name")
    require_cluster(user, body.cluster)
    return request.app.state.saved.add(user.username, body.name.strip(), body.cluster, body.params.lstrip("?"))


@router.delete("/saved-searches/{item_id}", summary="Delete one of your saved searches")
def delete_saved(item_id: str, request: Request, user: User = Depends(current_user)):
    return {"deleted": request.app.state.saved.delete(user.username, item_id)["id"]}
