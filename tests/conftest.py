"""Test fixtures: synthetic Vector Parquet logs on disk (and in moto S3), local state + secrets."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "local-test"))
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

from make_sample_logs import generate  # noqa: E402

from app.main import create_app  # noqa: E402
from app.settings import Settings  # noqa: E402

NOW = datetime.now(timezone.utc)


@pytest.fixture(scope="session")
def logs_dir(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("logs")
    generate(out, hours=6, files_per_hour=2, rows_per_file=30, now=NOW, seed=11)
    return out


def make_settings(tmp: Path, logs_dir: Path, **kw) -> Settings:
    base = dict(logs_backend="local", logs_local_dir=str(logs_dir), logs_prefix="vector/", storage_backend="local",
                local_store_dir=str(tmp / "store"), secrets_backend="local",
                local_secrets_dir=str(tmp / "secrets"), auth_mode="header", bootstrap_admins=["root"],
                cache_dir=str(tmp / "cache"), duckdb_threads=2, lb_background=False,
                lb_logs_backend="local", lb_logs_local_dir=str(tmp / "alb-bucket"),
                lb_parquet_local_dir=str(tmp / "alb-parquet"))
    base.update(kw)
    return Settings(**base)


@pytest.fixture()
def app(tmp_path, logs_dir):
    return create_app(make_settings(tmp_path, logs_dir))


@pytest.fixture()
def client(app) -> TestClient:
    return TestClient(app)


def as_user(name: str) -> dict:
    return {"X-User": name}


ROOT_H = as_user("root")


def add_user(client: TestClient, name: str, clusters: dict, admin: bool = False):
    r = client.post("/api/v1/admin/users", json={"username": name, "admin": admin, "clusters": clusters},
                    headers=ROOT_H)
    assert r.status_code == 201, r.text
    return r.json()
