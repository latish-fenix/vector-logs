"""Who is calling, and which clusters' logs may they read.

AUTH_MODE=password (default, production): every request carries a signed session,
either the ``vlg_session`` cookie set by /auth/login (browser) or
``Authorization: Bearer <token>`` (scripts, curl, /docs). The token names the user and a
token version; a password change or reset bumps the version, ending older sessions.

AUTH_MODE=header (dev and tests only): the username in ``X-User`` is trusted as-is.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from fastapi import Request, Security
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from .auth import normalize_username, read_token
from .errors import ApiError, forbidden

_NAME_RE = re.compile(r"^[A-Za-z0-9._@+-]{1,128}$")
SESSION_COOKIE = "vlg_session"
CSRF_HEADER = "x-requested-with"
_UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}

_MODE = os.environ.get("AUTH_MODE", "password").lower()
if _MODE == "header":
    SCHEME = APIKeyHeader(name=os.environ.get("USER_HEADER", "X-User"), auto_error=False,
                          scheme_name="Username",
                          description="Dev/test mode: your username, trusted as-is.")
else:
    SCHEME = HTTPBearer(auto_error=False, scheme_name="Session token",
                        description="Get a token from POST /api/v1/auth/login, then paste it here.")


def cluster_level(clusters: dict[str, str], cluster_id: str) -> str | None:
    """'view' or None. A cluster's own entry wins over '*'; 'none' denies."""
    level = clusters.get(cluster_id, clusters.get("*"))
    return "view" if level == "view" else None


@dataclass
class User:
    username: str
    admin: bool = False
    clusters: dict[str, str] = field(default_factory=dict)

    def can_view(self, cluster_id: str) -> bool:
        return self.admin or cluster_level(self.clusters, cluster_id) == "view"


def _from_header(request: Request) -> str:
    header = request.app.state.settings.user_header
    name = normalize_username(request.headers.get(header) or "")
    if not name:
        raise ApiError(401, "MISSING_USER", f"Send your username in the '{header}' header")
    if not _NAME_RE.match(name):
        raise ApiError(400, "INVALID_USER", "Username may contain letters, digits, . _ @ + -")
    return name


def session_from_request(request: Request) -> tuple[dict, dict]:
    """(token payload, full user record) for a valid session, else 401."""
    settings = request.app.state.settings
    auth = request.headers.get("authorization") or ""
    token, via_cookie = None, False
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    elif request.cookies.get(SESSION_COOKIE):
        token, via_cookie = request.cookies[SESSION_COOKIE], True
    if not token:
        raise ApiError(401, "NOT_AUTHENTICATED", "Sign in first (POST /api/v1/auth/login)")
    payload = read_token(settings.session_secret, token)
    if payload is None:
        raise ApiError(401, "SESSION_EXPIRED", "Your session has expired; sign in again")
    rec = request.app.state.users.get_auth(payload["sub"])
    if rec is None or int(rec.get("tokenVersion", 0)) != int(payload.get("tv", -1)):
        raise ApiError(401, "SESSION_EXPIRED", "Your session has ended; sign in again")
    if via_cookie and request.method in _UNSAFE and not request.headers.get(CSRF_HEADER):
        # Cross-site forms cannot set custom headers; the UI always sends this one.
        raise forbidden("CSRF_CHECK_FAILED", f"Missing {CSRF_HEADER} header")
    return payload, rec


def current_user(request: Request, _cred: HTTPAuthorizationCredentials | str | None = Security(SCHEME)) -> User:
    settings = request.app.state.settings
    if settings.auth_mode == "header":
        name = _from_header(request)
        rec = request.app.state.users.get_auth(name)
        if rec is None:
            raise forbidden("UNKNOWN_USER", f"User '{name}' is not registered; ask an admin to add you")
    else:
        _, rec = session_from_request(request)
        name = rec["username"]
    clusters = rec.get("clusters") or {}
    user = User(username=name, admin=bool(rec.get("admin")),
                clusters={k: v for k, v in clusters.items() if isinstance(v, str)})
    request.state.user = user
    return user


def require_admin(request: Request, _cred: HTTPAuthorizationCredentials | str | None = Security(SCHEME)) -> User:
    user = current_user(request)
    if not user.admin:
        raise forbidden("ADMIN_REQUIRED", "This endpoint needs admin rights")
    return user


def require_cluster(user: User, cluster_id: str) -> None:
    if not user.can_view(cluster_id):
        raise forbidden("PERMISSION_DENIED", f"'{user.username}' has no access to the logs of cluster '{cluster_id}'")
