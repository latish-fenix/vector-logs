"""Where every secret lives: AWS Secrets Manager (production) or local files (dev/tests).

Secret names (all under SECRETS_PREFIX, default ``vector-logs/``), each a JSON object:

    vector-logs/app                    {"sessionSecret": "...", "bootstrapAdminPassword": "..."}
    vector-logs/users/<email>          {"passwordHash": "scrypt$..."}   (console sign-in)

Nothing secret is written to S3, .env or the logs. Reads
are cached for a few minutes, so a value rotated in the AWS console is picked up without
a restart; the API's own writes update the cache at once.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets as pysecrets
import threading
import time
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from .errors import ApiError

log = logging.getLogger("vector_logs.secrets")

_NAME_RE = re.compile(r"^[A-Za-z0-9/_+=.@-]{1,512}$")


class SecretStore(Protocol):
    prefix: str
    backend: str

    def get(self, name: str) -> dict | None: ...
    def put(self, name: str, value: dict, description: str = "") -> None: ...
    def delete(self, name: str) -> None: ...
    def full_name(self, name: str) -> str: ...


def _check(name: str) -> str:
    if not _NAME_RE.match(name):
        raise ValueError(f"invalid secret name {name!r}")
    return name


class _Cache:
    def __init__(self, ttl: float):
        self.ttl = ttl
        self._data: dict[str, tuple[float, dict | None]] = {}
        self._lock = threading.Lock()

    def get(self, key: str):
        with self._lock:
            hit = self._data.get(key)
            if hit and time.monotonic() - hit[0] < self.ttl:
                return True, hit[1]
        return False, None

    def set(self, key: str, value: dict | None) -> None:
        with self._lock:
            self._data[key] = (time.monotonic(), value)


class AwsSecretStore:
    """AWS Secrets Manager. The EC2 instance role needs secretsmanager:GetSecretValue,
    CreateSecret, PutSecretValue, DeleteSecret, DescribeSecret and TagResource on
    arn:aws:secretsmanager:<region>:<account>:secret:<prefix>* (see docs/iam-policy.json)."""

    backend = "aws"

    def __init__(self, prefix: str = "vector-logs/", region: str | None = None,
                 kms_key_id: str | None = None, cache_seconds: float = 300, client=None,
                 endpoint_url: str | None = None):
        self.prefix = prefix
        self.kms_key_id = kms_key_id
        self.sm = client or boto3.client(
            "secretsmanager", region_name=region, endpoint_url=endpoint_url,
            config=Config(retries={"max_attempts": 5, "mode": "standard"}))
        self.cache = _Cache(cache_seconds)

    def full_name(self, name: str) -> str:
        return _check(f"{self.prefix}{name}")

    def get(self, name: str) -> dict | None:
        full = self.full_name(name)
        cached, value = self.cache.get(full)
        if cached:
            return value
        try:
            resp = self.sm.get_secret_value(SecretId=full)
            value = json.loads(resp.get("SecretString") or "{}")
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code == "ResourceNotFoundException":
                value = None
            elif code == "InvalidRequestException" and "deletion" in str(e).lower():
                value = None
            else:
                raise _aws_error("read", full, e) from e
        self.cache.set(full, value)
        return value

    def put(self, name: str, value: dict, description: str = "") -> None:
        full = self.full_name(name)
        body = json.dumps(value, separators=(",", ":"))
        try:
            self.sm.put_secret_value(SecretId=full, SecretString=body)
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code == "ResourceNotFoundException":
                kwargs = {"Name": full, "SecretString": body,
                          "Description": description or "Vector Logs viewer",
                          "Tags": [{"Key": "app", "Value": "vector-logs"}]}
                if self.kms_key_id:
                    kwargs["KmsKeyId"] = self.kms_key_id
                try:
                    self.sm.create_secret(**kwargs)
                except ClientError as e2:
                    raise _aws_error("create", full, e2) from e2
            elif code == "InvalidRequestException" and "deletion" in str(e).lower():
                try:
                    self.sm.restore_secret(SecretId=full)
                    self.sm.put_secret_value(SecretId=full, SecretString=body)
                except ClientError as e2:
                    raise _aws_error("restore", full, e2) from e2
            else:
                raise _aws_error("write", full, e) from e
        self.cache.set(full, value)

    def delete(self, name: str) -> None:
        full = self.full_name(name)
        try:
            self.sm.delete_secret(SecretId=full, ForceDeleteWithoutRecovery=True)
        except ClientError as e:
            if e.response["Error"]["Code"] != "ResourceNotFoundException":
                raise _aws_error("delete", full, e) from e
        self.cache.set(full, None)


def _aws_error(what: str, name: str, e: ClientError) -> ApiError:
    code = e.response["Error"]["Code"]
    msg = e.response["Error"].get("Message", str(e))
    if code in ("AccessDeniedException", "AccessDenied", "UnrecognizedClientException"):
        return ApiError(500, "SECRETS_ACCESS_DENIED",
                        f"The API may not {what} secret '{name}' in AWS Secrets Manager: add the "
                        "secretsmanager permissions from docs/iam-policy.json to the EC2 role",
                        {"awsError": code})
    return ApiError(502, "SECRETS_UNAVAILABLE",
                    f"AWS Secrets Manager could not {what} '{name}': {msg}", {"awsError": code})


class LocalSecretStore:
    """Files on disk, one JSON file per secret, mode 600. For local development and tests."""

    backend = "local"

    def __init__(self, directory: str, prefix: str = "vector-logs/"):
        self.dir = Path(directory)
        self.prefix = prefix
        self._lock = threading.Lock()

    def full_name(self, name: str) -> str:
        return _check(f"{self.prefix}{name}")

    def _path(self, full: str) -> Path:
        return self.dir / (quote(full, safe="") + ".json")

    def get(self, name: str) -> dict | None:
        p = self._path(self.full_name(name))
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None

    def put(self, name: str, value: dict, description: str = "") -> None:
        p = self._path(self.full_name(name))
        with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(self.dir, 0o700)
            except OSError:
                pass
            tmp = p.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(value, fh)
            os.replace(tmp, p)

    def delete(self, name: str) -> None:
        try:
            self._path(self.full_name(name)).unlink()
        except FileNotFoundError:
            pass


def build_secret_store(settings) -> SecretStore:
    if settings.secrets_backend == "aws":
        return AwsSecretStore(settings.secrets_prefix, settings.aws_region,
                              settings.secrets_kms_key_id,
                              endpoint_url=settings.secrets_endpoint_url)
    return LocalSecretStore(settings.local_secrets_dir, settings.secrets_prefix)


# --------------------------------------------------------------- app-level secrets
APP_SECRET = "app"


def user_secret(username: str) -> str:
    return f"users/{username}"


def resolve_app_secrets(store: SecretStore, env_session_secret: str,
                        env_bootstrap_password: str | None, need_bootstrap_password: bool
                        ) -> tuple[str, str | None]:
    """(session secret, bootstrap admin password) from the app secret.

    First run: a SESSION_SECRET / BOOTSTRAP_ADMIN_PASSWORD still in .env is copied into the
    secret (so existing sessions stay valid); otherwise a session secret is generated. After
    that the .env values can be deleted."""
    doc = dict(store.get(APP_SECRET) or {})
    changed = False
    if not doc.get("sessionSecret"):
        doc["sessionSecret"] = env_session_secret if len(env_session_secret or "") >= 32 \
            else pysecrets.token_hex(32)
        changed = True
        log.warning("Stored the session secret in %s%s", store.prefix, APP_SECRET)
    if need_bootstrap_password and not doc.get("bootstrapAdminPassword") and env_bootstrap_password:
        doc["bootstrapAdminPassword"] = env_bootstrap_password
        changed = True
        log.warning("Copied BOOTSTRAP_ADMIN_PASSWORD from .env into %s%s; remove it from .env",
                    store.prefix, APP_SECRET)
    if changed:
        store.put(APP_SECRET, doc, "Vector Logs viewer: session signing key and first admin password")
    elif env_session_secret and env_session_secret != doc["sessionSecret"]:
        log.warning("SESSION_SECRET in .env is ignored: the value in %s%s is used. Remove it from .env",
                    store.prefix, APP_SECRET)
    return doc["sessionSecret"], doc.get("bootstrapAdminPassword")
