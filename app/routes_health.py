"""ECS clusters and the health of the load balancer target groups they sit behind."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from .ecs_health import filter_for_user
from .errors import ApiError
from .identity import User, current_user

router = APIRouter(prefix="/api/v1/health", tags=["health"])


@router.get("/ecs", summary="ECS clusters → target groups → target health (refreshed at most every minute)")
def ecs_health(request: Request, refresh: bool = Query(False, description="Read AWS now instead of the last minute's picture"),
               user: User = Depends(current_user)):
    s = request.app.state.settings
    if not s.health_enabled:
        raise ApiError(404, "HEALTH_DISABLED", "The health page is turned off (HEALTH_ENABLED=false)")
    data = request.app.state.health.snapshot(refresh=refresh and user.admin)
    return filter_for_user(data, user.can_view, user.admin)
