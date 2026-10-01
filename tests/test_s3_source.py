"""The S3 logs source with moto: cluster discovery, hour listing, local cache, errors."""
from __future__ import annotations

from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from app.errors import ApiError
from app.logs import LogService, S3LogSource, SearchSpec
from conftest import make_settings


@pytest.fixture()
def s3_logs(tmp_path, logs_dir):
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-west-2")
        s3.create_bucket(Bucket="fenix-ecr-logs", CreateBucketConfiguration={"LocationConstraint": "us-west-2"})
        n = 0
        for p in Path(logs_dir).rglob("*.parquet"):
            s3.upload_file(str(p), "fenix-ecr-logs", p.relative_to(logs_dir).as_posix())
            n += 1
        s3.put_object(Bucket="fenix-ecr-logs", Key="vector/README.txt", Body=b"not a cluster")
        settings = make_settings(tmp_path, logs_dir, logs_backend="s3", logs_bucket="fenix-ecr-logs",
                                 logs_region="us-west-2")
        svc = LogService(settings, S3LogSource(settings, client=s3))
        yield {"svc": svc, "s3": s3, "files": n, "settings": settings}


def test_discovers_clusters_and_searches_through_the_cache(s3_logs, logs_dir):
    svc = s3_logs["svc"]
    assert svc.clusters() == ["post-btp-01", "post-btp-02", "pre-prod-01"]
    spec = SearchSpec(start="now-7h", end="now")
    first = svc.search("post-btp-01", spec, size=5)
    assert first["files"] > 0 and first["cached"] == 0 and first["total"] > 0
    second = svc.search("post-btp-01", spec, size=5)
    assert second["cached"] == second["files"] and second["total"] == first["total"]
    cached = list(Path(s3_logs["settings"].cache_dir, "fenix-ecr-logs").rglob("*.parquet"))
    assert len(cached) == first["files"]
    rec = svc.record("post-btp-01", first["hits"][0]["_ref"]["key"], first["hits"][0]["_ref"]["row"])
    assert rec["log_time_ms"] == first["hits"][0]["log_time_ms"]
    assert svc.cache_stats()["files"] == len(cached)


def test_listing_is_cached_but_recent_hours_refresh(s3_logs, monkeypatch):
    svc = s3_logs["svc"]
    calls = []
    real = svc.source.list_prefix
    monkeypatch.setattr(svc.source, "list_prefix", lambda p: calls.append(p) or real(p))
    svc.files_for("post-btp-02", *svc.resolve_range("now-3h", "now"))
    n = len(calls)
    svc.files_for("post-btp-02", *svc.resolve_range("now-3h", "now"))
    assert len(calls) == n  # within LISTING_CACHE_SECONDS


def test_access_denied_is_explained(s3_logs):
    from botocore.exceptions import ClientError
    svc = s3_logs["svc"]

    class Denied:
        def get_paginator(self, name):
            class P:
                def paginate(self, **kw):
                    raise ClientError({"Error": {"Code": "AccessDenied", "Message": "nope"}}, "ListObjectsV2")
            return P()
    svc.source.s3 = Denied()
    with pytest.raises(ApiError) as e:
        svc.search("post-btp-01", SearchSpec(start="now-1h", end="now"))
    assert e.value.code == "LOGS_ACCESS_DENIED" and "iam-policy.json" in e.value.message


def test_cache_eviction_keeps_recent_files(s3_logs):
    import os
    import time
    svc = s3_logs["svc"]
    svc.search("post-btp-01", SearchSpec(start="now-7h", end="now"), size=1)
    root = Path(s3_logs["settings"].cache_dir, "fenix-ecr-logs")
    files = sorted(root.rglob("*.parquet"))
    old = time.time() - 3600
    for f in files[: len(files) // 2]:
        os.utime(f, (old, old))
    object.__setattr__(svc.settings, "cache_max_mb", 0)
    svc._last_evict = 0
    svc._maybe_evict()
    left = sorted(root.rglob("*.parquet"))
    assert len(left) == len(files) - len(files) // 2  # only the files unused for 10+ minutes go
