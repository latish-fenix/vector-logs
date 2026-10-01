"""Admin API: /api/v1/admin/... (admin users only): users, their cluster access, clusters."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .auth import credentials_csv, generate_password, hash_password, is_email, normalize_username
from .errors import ApiError, bad_request, conflict, not_found
from .identity import User, cluster_level, require_admin
from .repos import validate_permissions

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


class UserBody(BaseModel):
    admin: bool = False
    clusters: dict[str, Any] = Field(
        default_factory=dict,
        description="{clusterId or '*': 'view' | 'none'}. '*' covers every cluster without its own "
                    "entry, including clusters that appear later; 'none' on a cluster overrides '*'.")


class NewUserBody(UserBody):
    username: str = Field(..., description="The user's email address")


class BulkUsersBody(BaseModel):
    users: list[NewUserBody]


def _password_mode(request: Request) -> bool:
    return request.app.state.settings.auth_mode == "password"


def _check_new_username(request: Request, username: str) -> str:
    username = normalize_username(username)
    if _password_mode(request) and not is_email(username):
        raise bad_request("INVALID_EMAIL", f"'{username}' is not a valid email address")
    return username


def _credentials_response(rows: list[tuple[str, str]], fmt: str | None, payload: dict,
                          filename: str, status: int = 200):
    """JSON by default; ?format=csv gives the username,password CSV as a download."""
    if fmt == "csv":
        return Response(credentials_csv(rows), status_code=status, media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"',
                                 "Cache-Control": "no-store"})
    return payload


def _event(request: Request, admin: User, action: str, **fields: Any) -> None:
    fwd = request.headers.get("x-forwarded-for")
    request.app.state.events.write({
        "action": action, "actor": admin.username, "outcome": "SUCCESS",
        "requestId": request.state.request_id,
        "sourceIp": fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else None),
        **fields,
    })


# ------------------------------------------------------------------ clusters
@router.get("/clusters", summary="Every cluster folder under LOGS_PREFIX, with who can read it")
def admin_clusters(request: Request, refresh: bool = Query(False, description="Re-list S3 now"),
                   admin: User = Depends(require_admin)):
    names = request.app.state.logs.clusters(refresh=refresh)
    users = request.app.state.users.list()
    items = []
    for c in names:
        readers = [u["username"] for u in users
                   if not u["admin"] and cluster_level(u.get("clusters") or {}, c) == "view"]
        items.append({"id": c, "users": readers})
    return {"items": items, "source": request.app.state.logs.source.describe(),
            "cache": request.app.state.logs.cache_stats()}


# --------------------------------------------------------------------- users
@router.get("/users", summary="All users")
def list_users(request: Request, admin: User = Depends(require_admin)):
    return {"items": request.app.state.users.list()}


@router.get("/users/{username}", summary="One user")
def get_user(username: str, request: Request, admin: User = Depends(require_admin)):
    rec = request.app.state.users.get(normalize_username(username))
    if rec is None:
        raise not_found("USER_NOT_FOUND", f"Unknown user '{username}'")
    return rec


def _create(request: Request, admin: User, body: NewUserBody) -> tuple[dict, str | None]:
    users = request.app.state.users
    username = _check_new_username(request, body.username)
    if users.exists(username) or username in users.bootstrap_admins:
        raise conflict("USER_EXISTS", f"User '{username}' already exists; use reset-password "
                       "to give them a new password")
    clusters = validate_permissions(body.clusters)
    password = generate_password() if _password_mode(request) else None
    change = users.put(username, body.admin, clusters, admin.username,
                       password_hash=hash_password(password) if password else None,
                       generated=True)
    _event(request, admin, "ADMIN_USER_CREATE", targetUser=username,
           after={"admin": body.admin, "clusters": clusters}, passwordGenerated=bool(password))
    return change["after"], password


@router.post("/users", status_code=201, summary="Add a user; returns a generated password ONCE")
def create_user(body: NewUserBody, request: Request,
                format: str | None = Query(None, description="csv = download username,password"),
                admin: User = Depends(require_admin)):
    user, password = _create(request, admin, body)
    rows = [(user["username"], password)] if password else []
    return _credentials_response(rows, format, {
        "user": user, "credentials": {"username": user["username"], "password": password}
        if password else None,
        "note": "The password is shown only now. Download the CSV and hand it over securely."},
        f"{user['username']}-credentials.csv", 201)


@router.post("/users/bulk", status_code=201, summary="Add several users; returns their passwords ONCE")
def create_users_bulk(body: BulkUsersBody, request: Request,
                      format: str | None = Query(None, description="csv = download username,password"),
                      admin: User = Depends(require_admin)):
    if not body.users:
        raise bad_request("NO_USERS", "Send at least one user")
    names = [normalize_username(u.username) for u in body.users]
    dupes = sorted({n for n in names if names.count(n) > 1})
    existing = sorted(n for n in names if request.app.state.users.exists(n)
                      or n in request.app.state.users.bootstrap_admins)
    if dupes or existing:
        raise conflict("USER_EXISTS", "Some users are duplicated or already exist; nothing was created",
                       {"duplicates": dupes, "existing": existing})
    for u in body.users:  # validate everything before creating anything
        _check_new_username(request, u.username)
        validate_permissions(u.clusters)
    created = [_create(request, admin, u) for u in body.users]
    rows = [(u["username"], p) for u, p in created if p]
    return _credentials_response(rows, format, {
        "users": [u for u, _ in created],
        "credentials": [{"username": u, "password": p} for u, p in rows]},
        "new-users-credentials.csv", 201)


@router.post("/users/{username}/reset-password",
             summary="Give a user a new generated password (returned ONCE); ends their sessions")
def reset_password(username: str, request: Request,
                   format: str | None = Query(None, description="csv = download username,password"),
                   admin: User = Depends(require_admin)):
    if not _password_mode(request):
        raise ApiError(404, "AUTH_DISABLED", "Passwords are off (AUTH_MODE=header)")
    username = normalize_username(username)
    if request.app.state.users.get(username) is None:
        raise not_found("USER_NOT_FOUND", f"Unknown user '{username}'")
    password = generate_password()
    request.app.state.users.set_password(username, hash_password(password), True, admin.username)
    _event(request, admin, "ADMIN_PASSWORD_RESET", targetUser=username)
    return _credentials_response([(username, password)], format, {
        "credentials": {"username": username, "password": password},
        "note": "The password is shown only now. Their existing sessions have ended."},
        f"{username}-credentials.csv")


@router.put("/users/{username}", summary="Update a user's admin flag and cluster access "
            "(creates the user, with a generated password, if new)")
def put_user(username: str, body: UserBody, request: Request,
             format: str | None = Query(None, description="csv = download username,password (new users)"),
             admin: User = Depends(require_admin)):
    users = request.app.state.users
    username = normalize_username(username)
    if not users.exists(username) and username not in users.bootstrap_admins:
        return create_user(NewUserBody(username=username, admin=body.admin, clusters=body.clusters),
                           request, format, admin)
    if username == admin.username and not body.admin and not users.get(username)["bootstrap"]:
        raise bad_request("CANNOT_DEMOTE_SELF", "You cannot remove your own admin rights")
    clusters = validate_permissions(body.clusters)
    change = users.put(username, body.admin, clusters, admin.username)
    _event(request, admin, "ADMIN_USER_UPDATE", targetUser=username,
           before={"admin": (change["before"] or {}).get("admin"), "clusters": (change["before"] or {}).get("clusters")},
           after={"admin": body.admin, "clusters": clusters})
    return change["after"]


@router.put("/users/{username}/permissions", summary="Set a user's cluster access")
def put_permissions(username: str, request: Request,
                    body: dict[str, Any] = Body(..., examples=[{"post-btp-01": "view", "*": "none"}]),
                    admin: User = Depends(require_admin)):
    username = normalize_username(username)
    clusters = validate_permissions(body)
    change = request.app.state.users.set_permissions(username, clusters, admin.username)
    _event(request, admin, "ADMIN_PERMISSIONS_UPDATE", targetUser=username, **change)
    return request.app.state.users.get(username)


@router.delete("/users/{username}", summary="Remove a user (and their saved searches)")
def delete_user(username: str, request: Request, admin: User = Depends(require_admin)):
    username = normalize_username(username)
    if username == admin.username:
        raise bad_request("CANNOT_DELETE_SELF", "You cannot delete your own account")
    before = request.app.state.users.delete(username)
    request.app.state.saved.delete_all(username)
    _event(request, admin, "ADMIN_USER_DELETE", targetUser=username,
           before={"admin": before.get("admin"), "clusters": before.get("clusters")})
    return {"deleted": username}
