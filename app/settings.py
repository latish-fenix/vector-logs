"""Runtime configuration, read once from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _csv(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def _slash(value: str) -> str:
    return value if not value or value.endswith("/") else value + "/"


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    # ---- where the logs are (read-only)
    logs_backend: str = "s3"                  # "s3" or "local" (a folder with the same layout; dev/tests)
    logs_bucket: str = "fenix-ecr-logs"
    logs_prefix: str = "vector/"              # one folder per cluster below this
    logs_region: str | None = "us-west-2"
    logs_endpoint_url: str | None = None      # only for S3-compatible test stores
    logs_local_dir: str = "./local-test/logs"

    # ---- search limits
    max_search_hours: int = 168               # one search may span at most this many hours (7 days)
    max_files_per_search: int = 30000
    max_export_rows: int = 10000
    download_threads: int = 32
    cache_dir: str = "/app/cache"             # downloaded Parquet files (immutable, so safe to keep)
    cache_max_mb: int = 4096
    duckdb_threads: int = 4
    duckdb_memory_mb: int = 1536
    listing_cache_seconds: int = 30           # recent hours; older hours are cached longer
    clusters_cache_seconds: int = 300

    # ---- ECS / load balancer health page (read-only AWS calls)
    health_enabled: bool = True
    ecs_region: str = "us-west-2"
    health_cache_seconds: int = 60

    # ---- app state (users, saved searches): S3 or a local folder
    storage_backend: str = "s3"               # "s3" or "local" (local = dev only)
    s3_bucket: str = ""
    s3_prefix: str = "vector-logs/"
    aws_region: str | None = None
    s3_endpoint_url: str | None = None
    s3_sse: str | None = None
    local_store_dir: str = "./.local-store"

    # ---- secrets: AWS Secrets Manager ("aws", production) or local files ("local", dev/tests)
    secrets_backend: str = "aws"
    secrets_prefix: str = "vector-logs/"
    secrets_kms_key_id: str | None = None
    secrets_endpoint_url: str | None = None
    local_secrets_dir: str = "./.local-secrets"

    # ---- sign-in
    auth_mode: str = "password"               # "password" (production) | "header" (dev/tests only)
    user_header: str = "X-User"
    bootstrap_admins: list[str] = field(default_factory=list)
    bootstrap_admin_password: str | None = None
    session_secret: str = ""
    session_hours: float = 12.0
    cookie_secure: bool = False               # true once served over HTTPS
    lockout_attempts: int = 5
    lockout_minutes: int = 15

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ.get
        storage = env("STORAGE_BACKEND", "s3").lower()
        return cls(
            logs_backend=env("LOGS_BACKEND", "s3").lower(),
            logs_bucket=env("LOGS_BUCKET", "fenix-ecr-logs"),
            logs_prefix=_slash(env("LOGS_PREFIX", "vector/").lstrip("/")),
            logs_region=env("LOGS_REGION", "us-west-2") or None,
            logs_endpoint_url=env("LOGS_ENDPOINT_URL") or None,
            logs_local_dir=env("LOGS_LOCAL_DIR", "./local-test/logs"),
            max_search_hours=int(env("MAX_SEARCH_HOURS", "168")),
            max_files_per_search=int(env("MAX_FILES_PER_SEARCH", "30000")),
            max_export_rows=int(env("MAX_EXPORT_ROWS", "10000")),
            download_threads=int(env("DOWNLOAD_THREADS", "32")),
            cache_dir=env("CACHE_DIR", "/app/cache"),
            cache_max_mb=int(env("CACHE_MAX_MB", "4096")),
            duckdb_threads=int(env("DUCKDB_THREADS", "4")),
            duckdb_memory_mb=int(env("DUCKDB_MEMORY_MB", "1536")),
            listing_cache_seconds=int(env("LISTING_CACHE_SECONDS", "30")),
            clusters_cache_seconds=int(env("CLUSTERS_CACHE_SECONDS", "300")),
            health_enabled=_bool(env("HEALTH_ENABLED"), True),
            ecs_region=env("ECS_REGION", "us-west-2"),
            health_cache_seconds=int(env("HEALTH_CACHE_SECONDS", "60")),
            storage_backend=storage,
            s3_bucket=env("S3_BUCKET", ""),
            s3_prefix=_slash(env("S3_PREFIX", "vector-logs/")),
            aws_region=env("AWS_REGION") or env("AWS_DEFAULT_REGION"),
            s3_endpoint_url=env("S3_ENDPOINT_URL") or None,
            s3_sse=env("S3_SSE") or None,
            local_store_dir=env("LOCAL_STORE_DIR", "./.local-store"),
            secrets_backend=env("SECRETS_BACKEND", "aws" if storage == "s3" else "local").lower(),
            secrets_prefix=_slash(env("SECRETS_PREFIX", "vector-logs/")),
            secrets_kms_key_id=env("SECRETS_KMS_KEY_ID") or None,
            secrets_endpoint_url=env("SECRETS_ENDPOINT_URL") or None,
            local_secrets_dir=env("LOCAL_SECRETS_DIR", "./.local-secrets"),
            auth_mode=env("AUTH_MODE", "password").lower(),
            user_header=env("USER_HEADER", "X-User"),
            bootstrap_admins=[a.lower() for a in _csv(env("BOOTSTRAP_ADMINS"))],
            bootstrap_admin_password=env("BOOTSTRAP_ADMIN_PASSWORD") or None,
            session_secret=env("SESSION_SECRET", ""),
            session_hours=float(env("SESSION_HOURS", "12")),
            cookie_secure=_bool(env("COOKIE_SECURE"), False),
            lockout_attempts=int(env("LOCKOUT_ATTEMPTS", "5")),
            lockout_minutes=int(env("LOCKOUT_MINUTES", "15")),
        )

    def validate(self) -> None:
        if self.storage_backend not in ("s3", "local"):
            raise ValueError("STORAGE_BACKEND must be 's3' or 'local'")
        if self.storage_backend == "s3" and not self.s3_bucket:
            raise ValueError("S3_BUCKET is required when STORAGE_BACKEND=s3")
        if self.logs_backend not in ("s3", "local"):
            raise ValueError("LOGS_BACKEND must be 's3' or 'local'")
        if self.logs_backend == "s3" and not self.logs_bucket:
            raise ValueError("LOGS_BUCKET is required")
        if self.auth_mode not in ("password", "header"):
            raise ValueError("AUTH_MODE must be 'password' or 'header'")
        if self.secrets_backend not in ("aws", "local"):
            raise ValueError("SECRETS_BACKEND must be 'aws' (AWS Secrets Manager) or 'local' (dev only)")
        if not 1 <= self.max_search_hours <= 24 * 31:
            raise ValueError("MAX_SEARCH_HOURS must be between 1 and 744")
