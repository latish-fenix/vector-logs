"""ECS clusters and the health of the load balancer target groups they sit behind."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from .ecs_health import filter_for_user
from .errors import ApiError
from .identity import User, current_user, require_cluster

router = APIRouter(prefix="/api/v1/health", tags=["health"])


@router.get("/ecs", summary="ECS clusters → target groups → target health (refreshed at most every minute)")
def ecs_health(request: Request, refresh: bool = Query(False, description="Read AWS now instead of the last minute's picture"),
               user: User = Depends(current_user)):
    s = request.app.state.settings
    if not s.health_enabled:
        raise ApiError(404, "HEALTH_DISABLED", "The health page is turned off (HEALTH_ENABLED=false)")
    data = request.app.state.health.snapshot(refresh=refresh and user.admin)
    return filter_for_user(data, user.can_view, user.admin)


def _enabled(request: Request) -> None:
    if not request.app.state.settings.health_enabled:
        raise ApiError(404, "HEALTH_DISABLED", "The health page is turned off (HEALTH_ENABLED=false)")


@router.get("/ecs/metrics", summary="CPU and memory of every ECS cluster you may see (CloudWatch, cached 5 minutes)")
def ecs_metrics(request: Request, hours: int = Query(3, ge=1, le=72, description="How far back the charts go"),
                user: User = Depends(current_user)):
    _enabled(request)
    data = request.app.state.metrics.clusters(hours)
    if user.admin:
        return data
    return dict(data, clusters={k: v for k, v in data["clusters"].items() if user.can_view(k)})


@router.get("/ecs/clusters/{cluster}/metrics", summary="CPU and memory of one cluster's services")
def ecs_service_metrics(cluster: str, request: Request, hours: int = Query(3, ge=1, le=72),
                        user: User = Depends(current_user)):
    _enabled(request)
    require_cluster(user, cluster)
    return request.app.state.metrics.services(cluster, hours)
