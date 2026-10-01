"""JSON object store with conditional writes.

S3Store is the production backend. S3 conditional writes (If-Match / If-None-Match
on PutObject) give optimistic concurrency, which is what makes S3 usable as the
only data store. LocalStore mimics the same semantics on disk for local dev.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Protocol

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from .settings import Settings


class PreconditionFailed(Exception):
    """The object changed (or exists / doesn't exist) since it was read."""


class ObjectStore(Protocol):
    def get_json(self, key: str) -> tuple[dict | None, str | None]: ...
    def put_json(self, key: str, data: dict, *, if_match: str | None = None,
                 if_none_match: bool = False) -> str: ...
    def delete(self, key: str) -> None: ...
    def list_keys(self, prefix: str, limit: int | None = None) -> list[str]: ...


def _encode(data: dict) -> bytes:
    return json.dumps(data, indent=2, sort_keys=True, default=str).encode()


class S3Store:
    def __init__(self, bucket: str, prefix: str = "", region: str | None = None,
                 endpoint_url: str | None = None, sse: str | None = None, client=None):
        self.bucket = bucket
        self.sse = sse
        self.prefix = prefix
        self.s3 = client or boto3.client(
            "s3", region_name=region, endpoint_url=endpoint_url,
            config=Config(retries={"max_attempts": 5, "mode": "standard"}),
        )

    def _k(self, key: str) -> str:
        return f"{self.prefix}{key}"

    def get_json(self, key: str) -> tuple[dict | None, str | None]:
        try:
            obj = self.s3.get_object(Bucket=self.bucket, Key=self._k(key))
        except ClientError as e:
            if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return None, None
            raise
        return json.loads(obj["Body"].read()), obj["ETag"]

    def put_json(self, key: str, data: dict, *, if_match: str | None = None,
                 if_none_match: bool = False) -> str:
        kwargs = dict(Bucket=self.bucket, Key=self._k(key), Body=_encode(data),
                      ContentType="application/json")
        if self.sse:
            kwargs["ServerSideEncryption"] = self.sse
        if if_match:
            kwargs["IfMatch"] = if_match
        elif if_none_match:
            kwargs["IfNoneMatch"] = "*"
        try:
            resp = self.s3.put_object(**kwargs)
        except ClientError as e:
            code = e.response["Error"]["Code"]
            status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            # 409 ConditionalRequestConflict = concurrent conditional write in flight
            if code in ("PreconditionFailed", "ConditionalRequestConflict") or status in (409, 412):
                raise PreconditionFailed(key) from e
            raise
        return resp["ETag"]

    def delete(self, key: str) -> None:
        self.s3.delete_object(Bucket=self.bucket, Key=self._k(key))

    def list_keys(self, prefix: str, limit: int | None = None) -> list[str]:
        """Keys under prefix in key order; with limit, only the first `limit` (S3 lists in
        key order, so this reads no more than needed)."""
        keys: list[str] = []
        paginator = self.s3.get_paginator("list_objects_v2")
        extra = {"PaginationConfig": {"MaxItems": limit}} if limit else {}
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self._k(prefix), **extra):
            for item in page.get("Contents", []):
                keys.append(item["Key"][len(self.prefix):])
        keys.sort()
        return keys[:limit] if limit else keys


class LocalStore:
    """Filesystem store with the same conditional semantics (single host, dev only)."""

    _lock = threading.Lock()

    def __init__(self, root: str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _p(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root.resolve() not in path.parents:
            raise ValueError("invalid key")
        return path

    @staticmethod
    def _etag(raw: bytes) -> str:
        return '"' + hashlib.md5(raw).hexdigest() + '"'

    def get_json(self, key: str) -> tuple[dict | None, str | None]:
        path = self._p(key)
        if not path.exists():
            return None, None
        raw = path.read_bytes()
        return json.loads(raw), self._etag(raw)

    def put_json(self, key: str, data: dict, *, if_match: str | None = None,
                 if_none_match: bool = False) -> str:
        path = self._p(key)
        raw = _encode(data)
        with self._lock:
            exists = path.exists()
            if if_none_match and exists:
                raise PreconditionFailed(key)
            if if_match and (not exists or self._etag(path.read_bytes()) != if_match):
                raise PreconditionFailed(key)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(raw)
            os.replace(tmp, path)
        return self._etag(raw)

    def delete(self, key: str) -> None:
        path = self._p(key)
        if path.exists():
            path.unlink()

    def list_keys(self, prefix: str, limit: int | None = None) -> list[str]:
        base = self.root
        keys = sorted(
            str(p.relative_to(base)).replace(os.sep, "/")
            for p in base.rglob("*") if p.is_file() and not p.name.endswith(".tmp")
            and str(p.relative_to(base)).replace(os.sep, "/").startswith(prefix)
        )
        return keys[:limit] if limit else keys


def build_store(settings: Settings) -> ObjectStore:
    if settings.storage_backend == "local":
        return LocalStore(settings.local_store_dir)
    return S3Store(settings.s3_bucket, settings.s3_prefix, settings.aws_region,
                   settings.s3_endpoint_url, settings.s3_sse)
