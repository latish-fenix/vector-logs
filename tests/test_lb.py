"""Load balancer access logs: parsing, the converter, and the /api/v1/lb endpoints."""
from __future__ import annotations

import dataclasses
import gzip
import json
import shutil
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pytest
from fastapi.testclient import TestClient

from make_alb_logs import generate

from app.lb_logs import (ALB_FIELDS, Converter, LbStore, LAT_EDGES, convert_sql, evict_cache, file_hour,
                         parse_key, percentile, _sql_list)
from app.main import create_app
from conftest import ROOT_H, add_user, as_user, make_settings

NOW = datetime.now(timezone.utc)
START = (NOW - timedelta(hours=4)).replace(minute=0, second=0, microsecond=0)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture()
def lb_env(tmp_path, logs_dir):
    """Four finished hours plus the current one, converted; returns (client, written lines, settings)."""
    settings = make_settings(tmp_path, logs_dir)
    written = generate(Path(settings.lb_logs_local_dir), now=NOW, start=START, rows_per_file=12, seed=3)
    conv = Converter(settings)
    conv.run_once()
    app = create_app(settings)
    return TestClient(app), written, settings, conv


def body(**kw) -> dict:
    return {"start": iso(START), "end": iso(NOW + timedelta(minutes=1)), **kw}


# ------------------------------------------------------------------ parsing
def test_file_names_and_hours():
    f = parse_key("loadbalancer-logs/AWSLogs/823002541310/elasticloadbalancing/us-west-2/2026/10/07/"
                  "823002541310_elasticloadbalancing_us-west-2_app.elb-alpha-prepurchase.1a14a793ecf89f3e_"
                  "20261007T0720Z_35.163.34.234_5ms5iw47.log.gz")
    assert (f.lb, f.hour, f.parquet_name.endswith(".parquet")) == ("elb-alpha-prepurchase", "2026-10-07T07", True)
    # a file ending at midnight covers 23:55-00:00 of the day before
    assert file_hour("20261008T0000Z") == datetime(2026, 10, 7, 23, tzinfo=timezone.utc)
    assert parse_key("loadbalancer-logs/AWSLogs/x/ELBAccessLogTestFile") is None
    assert parse_key(".../123456789012_elasticloadbalancing_us-west-2_net.nlb.abc_20261007T0720Z_1.2.3.4_x.log.gz") is None


def test_lines_are_converted(tmp_path):
    lines = [
        # target answered
        'https 2026-10-07T07:15:01.101682Z app/elb-x/1a2b 198.51.100.7:65530 10.0.32.18:80 0.000 0.159 0.000 200 200 '
        '1458 8264 "POST https://api.example.com:443/fenixdelest/api/v1/alpha-shop.myshopify.com/orders/12345?x=1 '
        'HTTP/1.1" "Mozilla/5.0 (X11; Linux) Demo" ECDHE-RSA-AES128-GCM-SHA256 TLSv1.2 '
        'arn:aws:elasticloadbalancing:us-west-2:111122223333:targetgroup/tg-alpha-edds-2/cd18ee8d310e74a0 '
        '"Root=1-6ac5f174-4921dae5398ff4e017b72319" "api.example.com" "-" 10 2026-10-07T07:15:00.941000Z "forward" '
        '"-" "-" "10.0.32.18:80" "200" "-" "-" TID_a6e5 "-" "-" "-" 35.163.34.234 "-" "-"',
        # the load balancer answered itself: no target, -1 timings
        'https 2026-10-07T07:16:00.000000Z app/elb-x/1a2b 198.51.100.8:1000 - -1 -1 -1 503 - 100 200 '
        '"GET https://api.example.com:443/ HTTP/1.1" "curl/8" - - '
        'arn:aws:elasticloadbalancing:us-west-2:111122223333:targetgroup/webhook-01/abc "Root=1-x" "api.example.com" '
        '"-" 0 2026-10-07T07:16:00.000000Z "forward" "-" "-" "-" "-" "-" "-" TID_b "-" "-" "-" 35.163.34.234 "-" '
        '"-" "a-future-field"',
    ]
    src = tmp_path / "a.log.gz"
    with gzip.open(src, "wt") as fh:
        fh.write("\n".join(lines) + "\n")
    rows = duckdb.sql(convert_sql(_sql_list([src]))).fetchall()
    cols = [d[0] for d in duckdb.sql(convert_sql(_sql_list([src]))).description]
    a, b = (dict(zip(cols, r)) for r in rows)
    assert a["lb"] == "elb-x" and a["tg"] == "tg-alpha-edds-2" and a["cluster"] == "alpha-edds-2"
    assert a["method"] == "POST" and a["path"] == "/fenixdelest/api/v1/alpha-shop.myshopify.com/orders/12345"
    assert a["path_group"] == "/fenixdelest/api/v1/<store>/orders/<n>" and a["query"] == "x=1"
    assert a["user_agent"] == "Mozilla/5.0 (X11; Linux) Demo" and a["tgt_t"] == pytest.approx(0.159)
    assert a["client_ip"] == "198.51.100.7" and a["client_port"] == 65530 and a["ts_ms"] == 1791357301101
    assert (b["elb_code"], b["tgt_code"], b["target"], b["tgt_t"], b["path"]) == (503, None, None, None, "/")
    assert b["cluster"] == "webhook-01"  # target groups not named tg-<cluster> map to their own name
    assert len(ALB_FIELDS) == 40


def test_percentile_from_bins():
    assert percentile({}, 0.95) is None
    # 100 requests in the 100-150 ms bin: the 95th is 95% of the way through it
    b = LAT_EDGES.index(100)
    assert percentile({b: 100}, 0.95) == pytest.approx(0.1475)


# ------------------------------------------------------------------ the converter
def test_converter_merges_finished_hours(lb_env):
    _, written, settings, conv = lb_env
    store = LbStore(Path(settings.cache_dir) / "lb")
    finished = {w.key for w in written if parse_key(w.key).hour < NOW.strftime("%Y-%m-%dT%H")}
    for lb in ("elb-demo-prepurchase", "elb-demo-postpurchase"):
        hk = START.strftime("%Y-%m-%dT%H")
        assert store.hour_file(lb, hk).exists() and store.minute_file(lb, hk).exists()
        assert not store.raw_files(lb, hk)
    merged = set()
    for f in (Path(settings.cache_dir) / "lb" / "keys").rglob("*.json"):
        merged |= set(json.loads(f.read_text()))
    # every merged hour is stored for good outside the server (S3; a folder in tests)
    remote = Path(settings.lb_parquet_local_dir) / "v3"
    for kind in ("hour", "minute", "paths", "keys"):
        assert len(list((remote / kind).rglob("*.*"))) == len(list((Path(settings.cache_dir) / "lb" / "keys").rglob("*.json")))
    # an hour still within its settle time stays as 5-minute files
    assert merged <= finished and len(merged) >= len(finished) - 2 * 5 * 12
    status = json.loads(store.status_file.read_text())
    assert status["lastError"] is None and sorted(status["lbs"]) == ["elb-demo-postpurchase", "elb-demo-prepurchase"]
    assert all(v[0] == v[1] for v in status["hours"].values())


def test_late_file_is_merged_again(lb_env):
    client, written, settings, conv = lb_env
    hk_dt = START
    late = generate(Path(settings.lb_logs_local_dir), now=hk_dt + timedelta(minutes=10), start=hk_dt,
                    rows_per_file=5, seed=99, current_hour=True)
    late_keys = {w.key for w in late}
    conv.run_once()
    store = LbStore(Path(settings.cache_dir) / "lb")
    keys = set(json.loads(store.keys_file("elb-demo-prepurchase", hk_dt.strftime("%Y-%m-%dT%H")).read_text()))
    assert {k for k in late_keys if "prepurchase" in k} <= keys
    r = client.post("/api/v1/lb/_summary", json=body(), headers=ROOT_H).json()
    assert r["totals"]["requests"] == len(written) + len(late)


# ------------------------------------------------------------------ the API
def test_summary_counts_match(lb_env):
    client, written, _, _ = lb_env
    r = client.post("/api/v1/lb/_summary", json=body(), headers=ROOT_H)
    assert r.status_code == 200, r.text
    d = r.json()
    t = d["totals"]
    assert t["requests"] == len(written)
    assert t["2xx"] == sum(1 for w in written if 200 <= w.elb_code < 300)
    assert t["3xx"] == sum(1 for w in written if 300 <= w.elb_code < 400)
    assert t["4xx"] == sum(1 for w in written if 400 <= w.elb_code < 500)
    assert t["s5xxLb"] == sum(1 for w in written if w.elb_code >= 500 and w.tgt_code is None)
    assert t["s5xxApp"] == sum(1 for w in written if w.elb_code >= 500 and w.tgt_code is not None)
    assert sum(b["2xx"] + b["3xx"] + b["4xx"] + b["5xx"] + b["other"] for b in d["buckets"]) == len(written)
    by_tg = {g["tg"]: g for g in d["targetGroups"]}
    assert by_tg["tg-demo-edds-1"]["requests"] == sum(1 for w in written if w.tg == "tg-demo-edds-1")
    assert by_tg["tg-demo-edds-1"]["cluster"] == "demo-edds-1"
    assert None in by_tg                         # redirects have no target group
    assert d["source"] == "minute" and t["p95"] and 0 < t["p95"] < 1
    top = by_tg["tg-demo-edds-1"]["topError"]
    assert top and top["pathGroup"].startswith("/fenixdelest/")
    # rows with 5xx sort first
    assert d["targetGroups"][0]["s5xx"] >= d["targetGroups"][-1]["s5xx"]


def test_filters(lb_env):
    client, written, _, _ = lb_env
    r = client.post("/api/v1/lb/_summary", json=body(statusClasses=["5xx"], source="lb"), headers=ROOT_H).json()
    assert r["totals"]["requests"] == sum(1 for w in written if w.elb_code >= 500 and w.tgt_code is None)
    r = client.post("/api/v1/lb/_summary", json=body(lbs=["elb-demo-postpurchase"]), headers=ROOT_H).json()
    assert r["totals"]["requests"] == sum(1 for w in written if w.lb == "elb-demo-postpurchase")
    r = client.post("/api/v1/lb/_summary", json=body(path="storeinfo"), headers=ROOT_H).json()
    assert r["source"] == "rows"
    assert r["totals"]["requests"] == sum(1 for w in written if "storeinfo" in w.path)
    r = client.post("/api/v1/lb/_summary", json=body(targetGroups=["-"]), headers=ROOT_H).json()
    assert r["totals"]["requests"] == sum(1 for w in written if w.tg is None)


def test_members_see_only_their_clusters(lb_env):
    client, written, _, _ = lb_env
    add_user(client, "dev@x.com", {"demo-edds-1": "view"})
    add_user(client, "all@x.com", {"*": "view", "webhook-01": "none"})
    r = client.post("/api/v1/lb/_summary", json=body(), headers=as_user("dev@x.com")).json()
    assert {g["tg"] for g in r["targetGroups"]} == {"tg-demo-edds-1"}
    assert r["totals"]["requests"] == sum(1 for w in written if w.tg == "tg-demo-edds-1")
    r = client.post("/api/v1/lb/_summary", json=body(), headers=as_user("all@x.com")).json()
    tgs = {g["tg"] for g in r["targetGroups"]}
    assert "tg-webhook-01" not in tgs and None not in tgs and "tg-demo-ib-01" in tgs
    reqs = client.post("/api/v1/lb/_requests", json=body(size=500), headers=as_user("dev@x.com")).json()
    assert reqs["hits"] and {h["tg"] for h in reqs["hits"]} == {"tg-demo-edds-1"}
    lbs = client.get("/api/v1/lb", headers=as_user("dev@x.com")).json()
    assert [x["name"] for x in lbs["items"]] == ["elb-demo-prepurchase"]
    assert [t["name"] for t in lbs["items"][0]["targetGroups"]] == ["tg-demo-edds-1"]


def test_paths_group_stores_and_numbers(lb_env):
    client, written, _, _ = lb_env
    r = client.post("/api/v1/lb/_paths", json=body(sort="requests", limit=100), headers=ROOT_H).json()
    groups = {(i["method"], i["pathGroup"]): i for i in r["items"]}
    assert ("GET", "/fenixdelest/api/v1/<store>/storeinfo") in groups
    assert ("GET", "/dp/api/v1/orders/<n>") in groups
    want = sum(1 for w in written if w.path.endswith("/storeinfo"))
    assert groups[("GET", "/fenixdelest/api/v1/<store>/storeinfo")]["requests"] == want


def test_request_pages(lb_env):
    client, written, _, _ = lb_env
    seen, prev_last = [], None
    for page in range(3):
        r = client.post("/api/v1/lb/_requests", json=body(offset=page * 100, size=100), headers=ROOT_H).json()
        assert r["total"] == len(written)
        ts = [h["ts_ms"] for h in r["hits"]]
        assert ts == sorted(ts, reverse=True)
        if prev_last is not None:
            assert ts[0] <= prev_last
        prev_last = ts[-1]
        seen += [h["trace_id"] for h in r["hits"]]
    assert len(seen) == len(set(seen)) == 300
    newest = max(written, key=lambda w: w.ts)
    first = client.post("/api/v1/lb/_requests", json=body(size=1), headers=ROOT_H).json()["hits"][0]
    assert first["ts_ms"] == int(newest.ts.timestamp() * 1000)
    asc = client.post("/api/v1/lb/_requests", json=body(size=5, order="asc"), headers=ROOT_H).json()["hits"]
    assert asc[0]["ts_ms"] == int(min(w.ts for w in written).timestamp() * 1000)


def test_export(lb_env):
    client, written, _, _ = lb_env
    r = client.post("/api/v1/lb/_export", json=body(format="csv", limit=50, statusClasses=["4xx"]), headers=ROOT_H)
    assert r.status_code == 200 and r.headers["x-export-rows"] == "50"
    lines = r.content.decode("utf-8-sig").strip().splitlines()
    assert lines[0].startswith("time,ts_ms,lb,tg") and len(lines) == 51


def test_limits_and_errors(lb_env, tmp_path, logs_dir):
    client, _, _, _ = lb_env
    r = client.post("/api/v1/lb/_summary", json={"start": "now-8d", "end": "now"}, headers=ROOT_H)
    assert r.status_code == 400 and r.json()["error"]["code"] == "RANGE_TOO_LARGE"
    r = client.post("/api/v1/lb/_summary", json={"start": "now", "end": "now-1h"}, headers=ROOT_H)
    assert r.json()["error"]["code"] == "INVALID_TIME"
    off = TestClient(create_app(make_settings(tmp_path / "x", logs_dir, lb_enabled=False)))
    assert off.post("/api/v1/lb/_summary", json={}, headers=ROOT_H).json()["error"]["code"] == "LB_DISABLED"


def test_old_ranges_are_converted_on_demand(tmp_path, logs_dir):
    settings = make_settings(tmp_path, logs_dir, lb_warm_days=1)
    old_start = (NOW - timedelta(days=3)).replace(minute=0, second=0, microsecond=0)
    old = generate(Path(settings.lb_logs_local_dir), now=old_start + timedelta(hours=2), start=old_start,
                   rows_per_file=4, seed=5)
    conv = Converter(settings)
    conv.run_once()                                  # outside the warm day: not converted yet
    client = TestClient(create_app(settings))
    rng = {"start": iso(old_start), "end": iso(old_start + timedelta(hours=2))}
    r = client.post("/api/v1/lb/_summary", json=rng, headers=ROOT_H).json()
    assert r["totals"]["requests"] == 0 and r["pending"].get("requested")
    assert list((Path(settings.cache_dir) / "lb" / "wanted").glob("*.json"))
    assert conv.process_wanted()
    r = client.post("/api/v1/lb/_summary", json=rng, headers=ROOT_H).json()
    assert r["totals"]["requests"] == len(old) and r["pending"]["files"] == 0
    assert not list((Path(settings.cache_dir) / "lb" / "wanted").glob("*.json"))


def test_prune_removes_hours_past_retention(lb_env):
    _, _, settings, conv = lb_env
    store = LbStore(Path(settings.cache_dir) / "lb")
    hk = START.strftime("%Y-%m-%dT%H")
    conv.now = lambda: NOW + timedelta(days=settings.lb_retention_days + 1)
    conv._prune(conv.now())
    assert not store.hour_file("elb-demo-prepurchase", hk).exists()
    assert not store.minute_file("elb-demo-prepurchase", hk).exists()


def test_s3_source_and_access_denied(tmp_path, logs_dir):
    import boto3
    from botocore.exceptions import ClientError
    from moto import mock_aws

    from app.lb_logs import S3LbSource
    local = tmp_path / "gen"
    written = generate(local, now=START + timedelta(hours=1, minutes=30), start=START, rows_per_file=3, seed=8)
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-west-2")
        s3.create_bucket(Bucket="fenix-vector-ecs-logs", CreateBucketConfiguration={"LocationConstraint": "us-west-2"})
        for p in local.rglob("*.log.gz"):
            s3.upload_file(str(p), "fenix-vector-ecs-logs", p.relative_to(local).as_posix())
        s3.put_object(Bucket="fenix-vector-ecs-logs", Key="loadbalancer-logs/AWSLogs/111122223333/ELBAccessLogTestFile",
                      Body=b"test")
        settings = make_settings(tmp_path, logs_dir, lb_logs_backend="s3", lb_warm_days=7)
        src = S3LbSource(settings, client=s3)
        assert src.bases() == ["loadbalancer-logs/AWSLogs/111122223333/elasticloadbalancing/us-west-2/"]
        conv = Converter(settings, source=src)
        conv.run_once()
        client = TestClient(create_app(settings))
        r = client.post("/api/v1/lb/_summary", json=body(), headers=ROOT_H).json()
        assert r["totals"]["requests"] == len(written)
        assert not list((Path(settings.cache_dir) / "lb" / ".tmp").rglob("*.log.gz"))   # downloads removed
        stored = [o["Key"] for o in s3.list_objects_v2(Bucket="fenix-vector-ecs-logs",
                                                       Prefix="loadbalancer-parquet/v3/keys/").get("Contents", [])]
        assert stored and all(k.endswith(".json") for k in stored)

        class Denied:
            def get_paginator(self, op):
                class P:
                    def paginate(self, **kw):
                        raise ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "ListObjectsV2")
                return P()
        bad = Converter(settings, source=S3LbSource(settings, client=Denied()))
        bad.run_once()
        status = json.loads((Path(settings.cache_dir) / "lb" / "status.json").read_text())
        assert status["lastError"]["code"] == "LB_LOGS_ACCESS_DENIED"
        lbs = client.get("/api/v1/lb", headers=ROOT_H).json()
        assert lbs["converter"]["lastError"]["code"] == "LB_LOGS_ACCESS_DENIED"


def test_original_lines_are_kept_and_searchable(lb_env):
    client, written, settings, _ = lb_env
    r = client.post("/api/v1/lb/_requests", json=body(size=5), headers=ROOT_H).json()
    h = r["hits"][0]
    key = next(w.key for w in written if int(w.ts.timestamp() * 1000) == h["ts_ms"])
    with gzip.open(Path(settings.lb_logs_local_dir) / key, "rt") as fh:
        lines = fh.read().splitlines()
    assert h["raw"] in lines and h["raw"].startswith(("https ", "h2 ")) and h["trace_id"] in h["raw"]
    # every word must appear somewhere in the line; -word excludes
    def count(q):
        return client.post("/api/v1/lb/_requests", json=body(q=q, size=1), headers=ROOT_H).json()["total"]
    all_store = sum(1 for w in written if w.path.endswith("/storeinfo"))
    with_shop, without_shop = count("storeinfo alpha-shop.myshopify.com"), count("storeinfo -alpha-shop.myshopify.com")
    assert count("storeinfo") == all_store and with_shop > 0 and without_shop > 0
    assert with_shop + without_shop == all_store


def test_old_converted_files_are_rebuilt(lb_env):
    client, written, settings, conv = lb_env
    root = Path(settings.cache_dir) / "lb"
    before = client.post("/api/v1/lb/_summary", json=body(), headers=ROOT_H).json()["totals"]["requests"]
    (root / "FORMAT").write_text("1")
    conv2 = Converter(settings)
    conv2.run_once()
    assert (root / "FORMAT").read_text() == "3"
    # merged hours come back from S3; only the 5-minute files of unfinished hours are converted again
    merged = sum(len(json.loads(f.read_text())) for f in (Path(settings.lb_parquet_local_dir) / "v3" / "keys").rglob("*.json"))
    assert merged and conv2.converted_files == len({w.key for w in written}) - merged
    after = client.post("/api/v1/lb/_summary", json=body(), headers=ROOT_H).json()["totals"]["requests"]
    assert before == after == len(written)



def test_a_new_server_refills_from_s3_and_fetches_only_what_it_reads(lb_env, tmp_path, logs_dir):
    client, written, settings, _ = lb_env
    before = client.post("/api/v1/lb/_summary", json=body(), headers=ROOT_H).json()
    shutil.rmtree(Path(settings.cache_dir) / "lb")              # a new instance, or `docker compose down -v`
    conv = Converter(settings)
    conv.run_once()
    store = LbStore(Path(settings.cache_dir) / "lb")
    assert not list((store.root / "hour").rglob("*.parquet"))   # only the small index came back
    after = client.post("/api/v1/lb/_summary", json=body(), headers=ROOT_H).json()
    strip = lambda d: {k: v for k, v in d.items() if k != "avg"}                   # noqa: E731 (float sums)
    assert strip(after["totals"]) == strip(before["totals"])
    assert after["totals"]["avg"] == pytest.approx(before["totals"]["avg"])
    assert [strip(g) for g in after["targetGroups"]] == [strip(g) for g in before["targetGroups"]]
    page = client.post("/api/v1/lb/_requests", json=body(size=10, order="asc"), headers=ROOT_H).json()
    assert page["total"] == len(written) and len(page["hits"]) == 10
    assert page["hits"][0]["ts_ms"] == int(min(w.ts for w in written).timestamp() * 1000)
    fetched = list((store.root / "hour").rglob("*.parquet"))
    assert 0 < len(fetched) <= 2                                # just the oldest hour (per load balancer)


def test_paths_level_filters_and_paging(lb_env):
    client, written, _, _ = lb_env
    pg = "/fenixdelest/api/v1/<store>/storeinfo"
    want = sum(1 for w in written if w.path.endswith("/storeinfo"))
    r = client.post("/api/v1/lb/_summary", json=body(pathGroup=pg, methods=["GET"]), headers=ROOT_H).json()
    assert r["source"] == "paths" and r["totals"]["requests"] == want
    seen = []
    for page in range(0, want, 50):
        res = client.post("/api/v1/lb/_requests", json=body(pathGroup=pg, methods=["GET"], offset=page, size=50),
                          headers=ROOT_H).json()
        assert res["total"] == want
        seen += [h["trace_id"] for h in res["hits"]]
    assert len(seen) == len(set(seen)) == want


def test_cache_limits(lb_env):
    client, written, settings, _ = lb_env
    store = LbStore(Path(settings.cache_dir) / "lb")
    assert list((store.root / "hour").rglob("*.parquet"))
    assert evict_cache(store, 0, keep_seconds=0, force=True) > 0
    assert not list((store.root / "hour").rglob("*.parquet")) and list((store.root / "minute").rglob("*.parquet"))
    # a search that would need too many hours of single requests at once is refused, not attempted
    small = TestClient(create_app(dataclasses.replace(settings, lb_max_fetch_files=1)))
    r = small.post("/api/v1/lb/_requests", json=body(q="storeinfo"), headers=ROOT_H)
    assert r.status_code == 400 and r.json()["error"]["code"] == "TOO_MUCH_DATA"
    # counts still work without any hour file on the server
    assert small.post("/api/v1/lb/_summary", json=body(), headers=ROOT_H).json()["totals"]["requests"] == len(written)
