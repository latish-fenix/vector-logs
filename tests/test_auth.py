"""Password sign-in (AUTH_MODE=password), with secrets in moto Secrets Manager and state in moto S3."""
from __future__ import annotations

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from app.main import create_app
from app.secret_store import AwsSecretStore
from app.storage import S3Store
from conftest import make_settings

ADMIN = "latish.madapada@fenixcommerce.com"
CSRF = {"X-Requested-With": "vector-logs-ui"}


@pytest.fixture()
def papp(tmp_path, logs_dir):
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="state")
        settings = make_settings(tmp_path, logs_dir, auth_mode="password", bootstrap_admins=[ADMIN],
                                 storage_backend="s3", s3_bucket="state", secrets_backend="aws",
                                 lockout_attempts=3)
        secrets = AwsSecretStore("vector-logs/", "us-east-1",
                                 client=boto3.client("secretsmanager", region_name="us-east-1"))
        app = create_app(settings, store=S3Store("state", "vector-logs/", client=s3), secrets=secrets)
        yield {"app": app, "secrets": secrets, "s3": s3}


def login(c, user, pw):
    return c.post("/api/v1/auth/login", json={"username": user, "password": pw})


def test_first_admin_password_is_generated_into_secrets_manager(papp):
    app_secret = papp["secrets"].get("app")
    assert len(app_secret["sessionSecret"]) >= 32
    pw = app_secret["bootstrapAdminPassword"]
    assert papp["secrets"].get(f"users/{ADMIN}")["passwordHash"].startswith("scrypt$")
    # no hash in S3
    body = papp["s3"].get_object(Bucket="state", Key="vector-logs/state/users.json")["Body"].read().decode()
    assert "scrypt" not in body
    c = TestClient(papp["app"])
    r = login(c, ADMIN.upper(), pw)
    assert r.status_code == 200, r.text
    assert r.json()["user"]["usingGeneratedPassword"] is True
    assert c.cookies.get("vlg_session")
    me = c.get("/api/v1/me")
    assert me.status_code == 200 and me.json()["admin"] is True
    # header identity is ignored in password mode
    c2 = TestClient(papp["app"])
    assert c2.get("/api/v1/clusters", headers={"X-User": ADMIN}).status_code == 401
    # bearer token works too
    tok = r.json()["token"]
    assert c2.get("/api/v1/clusters", headers={"Authorization": f"Bearer {tok}"}).status_code == 200


def test_csrf_header_required_for_cookie_writes(papp):
    c = TestClient(papp["app"])
    pw = papp["secrets"].get("app")["bootstrapAdminPassword"]
    login(c, ADMIN, pw)
    body = {"name": "x", "cluster": "post-btp-01", "params": ""}
    r = c.post("/api/v1/saved-searches", json=body)
    assert r.status_code == 403 and r.json()["error"]["code"] == "CSRF_CHECK_FAILED"
    assert c.post("/api/v1/saved-searches", json=body, headers=CSRF).status_code == 201


def test_lockout_change_password_and_reset(papp):
    c = TestClient(papp["app"])
    pw = papp["secrets"].get("app")["bootstrapAdminPassword"]
    login(c, ADMIN, pw)
    r = c.post("/api/v1/admin/users", json={"username": "ana@fenixcommerce.com", "clusters": {"*": "view"}},
               headers=CSRF)
    ana_pw = r.json()["credentials"]["password"]
    a = TestClient(papp["app"])
    for _ in range(3):
        assert login(a, "ana@fenixcommerce.com", "wrong-password-1").status_code == 401
    r = login(a, "ana@fenixcommerce.com", ana_pw)
    assert r.status_code == 423 and r.json()["error"]["code"] == "ACCOUNT_LOCKED"
    r = c.post("/api/v1/admin/users/ana@fenixcommerce.com/reset-password", headers=CSRF)
    new_pw = r.json()["credentials"]["password"]
    assert login(a, "ana@fenixcommerce.com", new_pw).status_code == 200
    r = a.post("/api/v1/auth/change-password", json={"currentPassword": new_pw, "newPassword": "short"},
               headers=CSRF)
    assert r.status_code == 400 and r.json()["error"]["code"] == "WEAK_PASSWORD"
    r = a.post("/api/v1/auth/change-password",
               json={"currentPassword": new_pw, "newPassword": "Logs-Viewer-Pass-2026"}, headers=CSRF)
    assert r.status_code == 200
    assert a.get("/api/v1/me").json()["usingGeneratedPassword"] is False
    b = TestClient(papp["app"])
    assert login(b, "ana@fenixcommerce.com", new_pw).status_code == 401
    assert login(b, "ana@fenixcommerce.com", "Logs-Viewer-Pass-2026").status_code == 200


def test_logout_all_ends_sessions(papp):
    c = TestClient(papp["app"])
    pw = papp["secrets"].get("app")["bootstrapAdminPassword"]
    tok = login(c, ADMIN, pw).json()["token"]
    other = TestClient(papp["app"])
    assert other.get("/api/v1/me", headers={"Authorization": f"Bearer {tok}"}).status_code == 200
    assert c.post("/api/v1/auth/logout-all", headers=CSRF).status_code == 200
    r = other.get("/api/v1/me", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "SESSION_EXPIRED"


def test_second_start_reuses_secrets(papp, tmp_path, logs_dir):
    """A restart (or the second uvicorn worker) must not generate a new key or password."""
    before = papp["secrets"].get("app")
    settings = papp["app"].state.settings
    create_app(settings, store=S3Store("state", "vector-logs/", client=papp["s3"]), secrets=papp["secrets"])
    assert papp["secrets"].get("app") == before
