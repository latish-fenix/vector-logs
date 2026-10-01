"""Sign in, sign out, change password: /api/v1/auth/..."""
from __future__ import annotations

import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from .auth import (hash_password, issue_token, normalize_username, password_problems,
                   verify_password)
from .errors import ApiError, bad_request
from .identity import SESSION_COOKIE, User, current_user, session_from_request

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class LoginBody(BaseModel):
    username: str = Field(..., description="Your email address")
    password: str


class ChangePasswordBody(BaseModel):
    currentPassword: str
    newPassword: str


def _ip(request: Request) -> str | None:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else None)


def _audit(request: Request, action: str, actor: str, outcome: str, **fields) -> None:
    request.app.state.events.write({"action": action, "actor": actor, "outcome": outcome,
                                   "requestId": request.state.request_id,
                                   "sourceIp": _ip(request), **fields})


def _require_password_mode(request: Request) -> None:
    if request.app.state.settings.auth_mode != "password":
        raise ApiError(404, "AUTH_DISABLED", "Password sign-in is off (AUTH_MODE=header)")


def _start_session(request: Request, response: Response, username: str, version: int) -> dict:
    s = request.app.state.settings
    token, exp = issue_token(s.session_secret, username, version, s.session_hours)
    response.set_cookie(SESSION_COOKIE, token, max_age=int(s.session_hours * 3600), path="/",
                        httponly=True, samesite="strict", secure=s.cookie_secure)
    return {"token": token, "tokenType": "Bearer",
            "expiresAt": datetime.fromtimestamp(exp, timezone.utc).isoformat().replace("+00:00", "Z")}


@router.get("/config", summary="Public sign-in settings (for the sign-in page)")
def auth_config(request: Request):
    s = request.app.state.settings
    return {"authMode": s.auth_mode, "sessionHours": s.session_hours,
            "lockoutAttempts": s.lockout_attempts, "lockoutMinutes": s.lockout_minutes}


@router.post("/login", summary="Sign in with email + password; returns a session token")
def login(body: LoginBody, request: Request, response: Response):
    _require_password_mode(request)
    s = request.app.state.settings
    users = request.app.state.users
    username = normalize_username(body.username)
    rec = users.get_auth(username)

    locked_until = (rec or {}).get("lockedUntil")
    if locked_until and locked_until > time.time():
        minutes = max(1, int((locked_until - time.time() + 59) // 60))
        _audit(request, "AUTH_LOGIN", username, "REJECTED", error={"code": "ACCOUNT_LOCKED"})
        raise ApiError(423, "ACCOUNT_LOCKED",
                       f"Too many failed sign-ins. Try again in {minutes} minute(s) or ask an admin "
                       "to reset your password", {"retryAfterMinutes": minutes})

    ok = verify_password(body.password, users.password_hash(username) if rec else None)
    if not ok:
        state = users.record_login(username, False, s.lockout_attempts, s.lockout_minutes) \
            if rec else {"locked": False}
        _audit(request, "AUTH_LOGIN", username, "REJECTED",
               error={"code": "INVALID_CREDENTIALS"}, lockedNow=state.get("locked", False))
        raise ApiError(401, "INVALID_CREDENTIALS", "Wrong email or password")

    users.record_login(username, True, s.lockout_attempts, s.lockout_minutes)
    session = _start_session(request, response, username, int(rec.get("tokenVersion", 0)))
    _audit(request, "AUTH_LOGIN", username, "SUCCESS")
    public = users.get(username)
    return {**session, "user": {"username": username, "admin": public["admin"],
                                "usingGeneratedPassword": public["usingGeneratedPassword"]}}


@router.post("/logout", summary="Sign out of this browser")
def logout(request: Request, response: Response):
    s = request.app.state.settings
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="strict",
                           secure=s.cookie_secure)
    try:
        _, rec = session_from_request(request)
        _audit(request, "AUTH_LOGOUT", rec["username"], "SUCCESS")
    except ApiError:
        pass
    return {"signedOut": True}


@router.post("/logout-all", summary="Sign out everywhere (ends every session of yours)")
def logout_all(request: Request, response: Response, user: User = Depends(current_user)):
    _require_password_mode(request)
    request.app.state.users.bump_token_version(user.username)
    response.delete_cookie(SESSION_COOKIE, path="/")
    _audit(request, "AUTH_LOGOUT_ALL", user.username, "SUCCESS")
    return {"signedOut": True, "allSessions": True}


@router.post("/change-password", summary="Change your own password")
def change_password(body: ChangePasswordBody, request: Request, response: Response,
                    user: User = Depends(current_user)):
    _require_password_mode(request)
    users = request.app.state.users
    rec = users.get_auth(user.username)
    if not verify_password(body.currentPassword, users.password_hash(user.username)):
        _audit(request, "AUTH_PASSWORD_CHANGE", user.username, "REJECTED",
               error={"code": "INVALID_CREDENTIALS"})
        raise ApiError(401, "INVALID_CREDENTIALS", "Current password is wrong")
    problems = password_problems(body.newPassword, user.username)
    if body.newPassword == body.currentPassword:
        problems.append("must be different from the current password")
    if problems:
        raise bad_request("WEAK_PASSWORD", "New password " + "; ".join(problems), problems)
    version = users.set_password(user.username, hash_password(body.newPassword), False,
                                 user.username)
    session = _start_session(request, response, user.username, version)
    _audit(request, "AUTH_PASSWORD_CHANGE", user.username, "SUCCESS")
    return {"changed": True, "otherSessionsEnded": True, **session}
