"""FastAPI entry point: `uvicorn app.main:create_app --factory`."""
from __future__ import annotations

import dataclasses
import logging
import os
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from . import routes_admin, routes_auth, routes_health, routes_logs
from .auth import generate_password, hash_password, password_problems
from .errors import ApiError
from .ecs_health import HealthService
from .logs import LogService
from .repos import EventLog, LockRepo, SavedSearchRepo, UsersRepo
from .secret_store import SecretStore, build_secret_store, resolve_app_secrets
from .settings import Settings
from .storage import ObjectStore, build_store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("vector_logs")

STARTUP_LOCK = ("_app", "startup", "init")
STARTUP_LOCK_SECONDS = 120


def create_app(settings: Settings | None = None, store: ObjectStore | None = None,
               secrets: SecretStore | None = None, logs: LogService | None = None,
               health: HealthService | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    if store is None:
        settings.validate()
        store = build_store(settings)
    secrets = secrets or build_secret_store(settings)
    # Several API workers start at the same moment. On a fresh install each would otherwise
    # generate its own session key and first-admin password; one lock in S3 lets the first
    # worker create them and the others read them.
    locks = LockRepo(store, STARTUP_LOCK_SECONDS)
    token = _acquire_startup_lock(locks)
    try:
        return _create_app(settings, store, secrets, logs, health)
    finally:
        locks.release(*STARTUP_LOCK, token)


def _acquire_startup_lock(locks: LockRepo, wait_seconds: float = 90) -> str:
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            return locks.acquire(*STARTUP_LOCK, f"api worker {os.getpid()}")
        except ApiError as e:
            if e.code != "CHANGE_IN_PROGRESS" or time.monotonic() > deadline:
                raise
            time.sleep(0.5)


def _create_app(settings: Settings, store: ObjectStore, secrets: SecretStore,
                logs: LogService | None, health: HealthService | None = None) -> FastAPI:
    bootstrap_pw = settings.bootstrap_admin_password
    if settings.auth_mode == "password":
        session_secret, bootstrap_pw = resolve_app_secrets(
            secrets, settings.session_secret, settings.bootstrap_admin_password,
            need_bootstrap_password=bool(settings.bootstrap_admins))
        settings = dataclasses.replace(settings, session_secret=session_secret)

    app = FastAPI(
        title="Vector Logs viewer",
        version="1.0.0",
        description="Search the application logs that Vector writes to S3 as Parquet. "
                    + ("Sign in with `POST /api/v1/auth/login`, then click **Authorize** and paste "
                       "the token." if settings.auth_mode == "password" else
                       f"Dev mode: identify yourself with the `{settings.user_header}` header.")
                    + " The web console is at [/ui/](/ui/).",
    )
    app.state.settings = settings
    app.state.secrets = secrets
    app.state.users = UsersRepo(store, settings.bootstrap_admins, secrets)
    app.state.saved = SavedSearchRepo(store)
    app.state.events = EventLog()
    app.state.logs = logs or LogService(settings)
    app.state.health = health or HealthService(settings.ecs_region, settings.health_cache_seconds)
    if settings.auth_mode == "password":
        _bootstrap_passwords(settings, app.state.users, bootstrap_pw, secrets)

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if request.url.path.startswith("/ui"):
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Content-Security-Policy"] = UI_CSP
        return response

    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError):
        return JSONResponse(status_code=exc.status, content=exc.to_dict())

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return JSONResponse(status_code=400, content={"error": {
            "code": "INVALID_REQUEST", "message": "Request body or parameters are invalid",
            "details": exc.errors()}})

    @app.get("/healthz", tags=["ops"], summary="Liveness probe")
    def healthz():
        return {"status": "ok"}

    app.include_router(routes_auth.router)
    app.include_router(routes_logs.router)
    app.include_router(routes_health.router)
    app.include_router(routes_admin.router)
    _mount_ui(app)
    return app


def _bootstrap_passwords(settings: Settings, users: UsersRepo, pw: str | None,
                         secrets: SecretStore) -> None:
    """Give each BOOTSTRAP_ADMINS user the first-admin password, once (if they have none yet).

    The password comes from the app secret (field bootstrapAdminPassword); if neither it nor
    BOOTSTRAP_ADMIN_PASSWORD is set, one is generated and stored there for an admin to read."""
    for name in settings.bootstrap_admins:
        rec = users.get_auth(name)
        if rec and rec.get("hasPassword"):
            continue
        if not pw:
            pw = generate_password()
            doc = dict(secrets.get("app") or {})
            doc["bootstrapAdminPassword"] = pw
            secrets.put("app", doc)
            log.warning("Generated the first-admin password; read it from %sapp "
                        "(field bootstrapAdminPassword) and change it after signing in",
                        secrets.full_name(""))
        problems = password_problems(pw, name)
        if problems:
            raise RuntimeError("BOOTSTRAP_ADMIN_PASSWORD " + "; ".join(problems))
        users.set_password(name, hash_password(pw), True, "bootstrap")
        log.warning("Set the first-admin password for bootstrap admin %s (sign in and change it)", name)


UI_DIR = Path(__file__).parent / "static" / "ui"
# The UI is fully self-hosted (fonts included); styles may be inline (React style props).
UI_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
          "img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; "
          "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")


def _mount_ui(app: FastAPI) -> None:
    """Serve the built web UI (ui/ -> app/static/ui) at /ui, with an SPA fallback."""
    index = UI_DIR / "index.html"

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/ui/" if index.exists() else "/docs")

    @app.get("/ui", include_in_schema=False)
    def ui_slash():
        return RedirectResponse("/ui/")

    @app.get("/ui/{path:path}", include_in_schema=False)
    def ui(path: str):
        if not index.exists():
            return JSONResponse(status_code=404, content={"error": {
                "code": "UI_NOT_BUILT", "message": "The web UI has not been built (see ui/README.md)"}})
        target = (UI_DIR / path).resolve()
        if path and UI_DIR.resolve() in target.parents and target.is_file():
            cache = ("public, max-age=31536000, immutable" if "/assets/" in f"/{path}"
                     else "no-cache")
            return FileResponse(target, headers={"Cache-Control": cache})
        return FileResponse(index, headers={"Cache-Control": "no-cache"})
