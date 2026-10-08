"""Searching, reading and exporting the synthetic logs (local folder backend)."""
from __future__ import annotations

import csv
import io
import json

import duckdb
import pytest

from conftest import ROOT_H, add_user, as_user

URL = "/api/v1/clusters/post-btp-01/logs"


def all_rows(logs_dir, cluster="post-btp-01", where="TRUE", params=()):
    con = duckdb.connect()
    glob = str(logs_dir / "vector" / cluster / "*" / "*" / "*.parquet")
    return con.execute(f"SELECT * FROM read_parquet('{glob}') WHERE {where}", list(params)).fetchall()


def search(client, body, url=URL, headers=ROOT_H):
    r = client.post(f"{url}/_search", json=body, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def test_lists_cluster_folders(client):
    r = client.get("/api/v1/clusters", headers=ROOT_H)
    assert [c["id"] for c in r.json()["items"]] == ["post-btp-01", "post-btp-02", "pre-prod-01"]


def test_search_counts_match_the_files(client, logs_dir):
    res = search(client, {"start": "now-7h", "end": "now", "size": 5})
    expected = len(all_rows(logs_dir, where="log_time_ms >= ?", params=[res["start"]]))
    assert res["total"] == expected > 0
    assert len(res["hits"]) == 5
    times = [h["log_time_ms"] for h in res["hits"]]
    assert times == sorted(times, reverse=True)
    assert sum(b["count"] for b in res["histogram"]["buckets"]) == res["total"]
    assert {f["value"] for f in res["facets"]["level"]} <= {"ERROR", "WARN", "INFO", "DEBUG"}
    assert res["columns"][0]["name"] == "log_time"
    assert res["hits"][0]["_ref"]["key"].startswith("vector/post-btp-01/dt=")


def test_paging_and_ascending_order(client):
    first = search(client, {"start": "now-3h", "end": "now", "size": 10, "order": "asc"})
    second = search(client, {"start": "now-3h", "end": "now", "size": 10, "offset": 10, "order": "asc",
                             "aggregations": False})
    assert "histogram" not in second
    t1 = [h["log_time_ms"] for h in first["hits"]]
    t2 = [h["log_time_ms"] for h in second["hits"]]
    assert t1 == sorted(t1) and t1[-1] <= t2[0]
    refs = {json.dumps(h["_ref"]) for h in first["hits"]} & {json.dumps(h["_ref"]) for h in second["hits"]}
    assert not refs


def test_free_text_phrases_negation_and_field_tokens(client, logs_dir):
    base = {"start": "now-7h", "end": "now", "size": 1}
    res = search(client, {**base, "query": '"value cannot be null"'})
    exp = all_rows(logs_dir, where="body ILIKE '%value cannot be null%' AND log_time_ms >= ?", params=[res["start"]])
    assert res["total"] == len(exp) > 0
    res2 = search(client, {**base, "query": 'level:error -"value cannot"'})
    exp2 = all_rows(logs_dir, where="upper(level)='ERROR' AND coalesce(body,'') NOT ILIKE '%value cannot%' "
                                    "AND log_time_ms >= ?", params=[res2["start"]])
    assert res2["total"] == len(exp2) > 0
    res3 = search(client, {**base, "query": "service:fenix-delest* carrier:UPS"})
    exp3 = all_rows(logs_dir, where="service LIKE 'fenix-delest%' AND carrier='UPS' AND log_time_ms >= ?",
                    params=[res3["start"]])
    assert res3["total"] == len(exp3) > 0
    # characters that mean something in LIKE are taken literally
    assert search(client, {**base, "query": "100%_sure"})["total"] == 0


def test_filters(client, logs_dir):
    base = {"start": "now-7h", "end": "now", "size": 1}
    res = search(client, {**base, "filters": [
        {"field": "level", "op": "one_of", "values": ["ERROR", "WARN"]},
        {"field": "response_time_ms", "op": "between", "gte": 500, "lte": 2400},
        {"field": "exception", "op": "not_exists"}]})
    exp = all_rows(logs_dir, where="level IN ('ERROR','WARN') AND response_time_ms BETWEEN 500 AND 2400 "
                                   "AND exception IS NULL AND log_time_ms >= ?", params=[res["start"]])
    assert res["total"] == len(exp) > 0
    res = search(client, {**base, "filters": [{"field": "level", "op": "is_not", "value": "INFO"},
                                              {"field": "msg", "op": "contains", "value": "tracking number"}]})
    exp = all_rows(logs_dir, where="coalesce(level,'') <> 'INFO' AND msg ILIKE '%tracking number%' "
                                   "AND log_time_ms >= ?", params=[res["start"]])
    assert res["total"] == len(exp)
    res = search(client, {**base, "filters": [{"field": "parse_ok", "op": "is", "value": "false"}]})
    assert res["total"] == len(all_rows(logs_dir, where="NOT parse_ok AND log_time_ms >= ?", params=[res["start"]]))


@pytest.mark.parametrize("body,code", [
    ({"start": "now-8d", "end": "now"}, "RANGE_TOO_LARGE"),
    ({"start": "now", "end": "now-1h"}, "INVALID_TIME"),
    ({"start": "yesterday", "end": "now"}, "INVALID_TIME"),
    ({"start": "now-1h", "end": "now", "filters": [{"field": "nope", "op": "is", "value": "x"}]}, "UNKNOWN_FIELD"),
    ({"start": "now-1h", "end": "now", "filters": [{"field": "line", "op": "is", "value": "abc"}]}, "INVALID_FILTER"),
    ({"start": "now-1h", "end": "now", "filters": [{"field": "level", "op": "one_of", "values": []}]}, "INVALID_FILTER"),
])
def test_bad_searches(client, body, code):
    r = client.post(f"{URL}/_search", json=body, headers=ROOT_H)
    assert r.status_code == 400 and r.json()["error"]["code"] == code, r.text


def test_old_logs_25_days_back_with_7_day_window(client, logs_dir):
    res = search(client, {"start": "now-27d", "end": "now-21d", "size": 1})
    assert res["total"] == 2 * 2 * 30  # 2 hosts x 2 files x 30 rows in the hour 25 days ago


def test_empty_range_and_iso_times(client):
    res = search(client, {"start": "2020-01-01T00:00:00Z", "end": "2020-01-01T05:00:00Z"})
    assert res["total"] == 0 and res["hits"] == [] and res["files"] == 0


def test_record_returns_the_whole_line(client, logs_dir):
    res = search(client, {"start": "now-7h", "end": "now", "size": 1, "query": '"<!DOCTYPE html>"'})
    hit = res["hits"][0]
    r = client.get(f"{URL}/_record", params=hit["_ref"], headers=ROOT_H)
    assert r.status_code == 200
    rec = r.json()
    assert rec["log_time_ms"] == hit["log_time_ms"] and rec["body"].startswith("org.springframework")
    # references outside the cluster folder are refused
    bad = client.get(f"{URL}/_record", params={"key": "vector/post-btp-02/dt=x/hour=1/a.parquet", "row": 0},
                     headers=ROOT_H)
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "INVALID_REF"
    bad = client.get(f"{URL}/_record", params={"key": "vector/post-btp-01/../../etc/passwd.parquet", "row": 0},
                     headers=ROOT_H)
    assert bad.status_code == 400


def test_long_fields_are_cut_in_the_list_only(tmp_path):
    from app.logs import LIST_TRUNCATE, LogService
    from conftest import make_settings
    from make_sample_logs import NAMES, write_file
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    row = {n: None for n in NAMES}
    row.update(log_time_ms=int(now.timestamp() * 1000) - 1000, level="ERROR", body="x" * 5000, msg="long")
    write_file(tmp_path / "logs" / "vector" / "c1" / f"dt={now:%Y-%m-%d}" / f"hour={now:%H}" / "1-a.parquet", [row])
    svc = LogService(make_settings(tmp_path, tmp_path / "logs"))
    from app.logs import SearchSpec
    res = svc.search("c1", SearchSpec(start="now-1h", end="now"))
    hit = res["hits"][0]
    assert len(hit["body"]) == LIST_TRUNCATE and hit["_truncated"]
    assert len(svc.record("c1", hit["_ref"]["key"], hit["_ref"]["row"])["body"]) == 5000


def test_export_csv_json_ndjson(client):
    body = {"start": "now-2h", "end": "now", "query": "level:error"}
    r = client.post(f"{URL}/_export", json={**body, "format": "csv", "limit": 7,
                                            "columns": ["log_time", "level", "msg"]}, headers=ROOT_H)
    assert r.status_code == 200 and r.headers["x-export-rows"] == "7"
    rows = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))
    assert rows[0] == ["log_time", "level", "msg"] and len(rows) == 8 and all(x[1] == "ERROR" for x in rows[1:])
    assert 'filename="logs-post-btp-01_' in r.headers["content-disposition"]
    assert r.headers["content-disposition"].endswith('_UTC_newest-first.csv"')
    r = client.post(f"{URL}/_export", json={**body, "format": "json", "limit": 3}, headers=ROOT_H)
    data = r.json()
    assert len(data) == 3 and data[0]["level"] == "ERROR" and "cluster" in data[0]
    r = client.post(f"{URL}/_export", json={**body, "format": "ndjson", "limit": 4}, headers=ROOT_H)
    assert len([json.loads(x) for x in r.text.splitlines()]) == 4
    r = client.post(f"{URL}/_export", json={**body, "format": "csv", "columns": ["nope"]}, headers=ROOT_H)
    assert r.status_code == 400


def test_export_matches_the_screen_and_time_zone(client):
    """The file holds the same lines, in the same order, as the screen; log_time in the viewer's zone."""
    first = client.post(f"{URL}/_search", json={"start": "now-6h", "end": "now", "size": 5}, headers=ROOT_H).json()
    for order in ("desc", "asc"):
        body = {"start": str(first["start"]), "end": str(first["end"]), "order": order}
        page = client.post(f"{URL}/_search", json={**body, "size": 20}, headers=ROOT_H).json()
        r = client.post(f"{URL}/_export", json={**body, "format": "json", "limit": 20, "timeZone": "Asia/Kolkata"},
                        headers=ROOT_H)
        data = r.json()
        assert [d["log_time_ms"] for d in data] == [h["log_time_ms"] for h in page["hits"]]
        assert r.headers["x-export-total"] == str(first["total"])
        assert r.headers["x-export-first"] == str(data[0]["log_time_ms"])
        assert r.headers["x-export-last"] == str(data[-1]["log_time_ms"])
        assert r.headers["content-disposition"].endswith(f'_+0530_{"newest" if order == "desc" else "oldest"}-first.json"')
        from datetime import datetime, timedelta, timezone
        utc = datetime.fromtimestamp(data[0]["log_time_ms"] / 1000, timezone.utc)
        assert data[0]["log_time"] == (utc + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d %H:%M:%S.") + \
            f"{data[0]['log_time_ms'] % 1000:03d} +05:30"
    r = client.post(f"{URL}/_export", json={"start": "now-1h", "end": "now", "timeZone": "Mars/Base"}, headers=ROOT_H)
    assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_TIME_ZONE"


def test_export_is_capped(tmp_path, logs_dir):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from conftest import make_settings
    c = TestClient(create_app(make_settings(tmp_path, logs_dir, max_export_rows=12)))
    r = c.post(f"{URL}/_export", json={"start": "now-7h", "end": "now", "format": "ndjson", "limit": 99999},
               headers=ROOT_H)
    assert r.headers["x-export-rows"] == "12"


def test_too_many_files(tmp_path, logs_dir):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from conftest import make_settings
    c = TestClient(create_app(make_settings(tmp_path, logs_dir, max_files_per_search=3)))
    r = c.post(f"{URL}/_search", json={"start": "now-7h", "end": "now"}, headers=ROOT_H)
    assert r.status_code == 400 and r.json()["error"]["code"] == "TOO_MANY_FILES"


def test_cluster_access(client):
    add_user(client, "ana@fenixcommerce.com", {"post-btp-01": "view"})
    add_user(client, "raj@fenixcommerce.com", {"*": "view", "pre-prod-01": "none"})
    add_user(client, "zoe@fenixcommerce.com", {})
    ana, raj, zoe = (as_user(n) for n in ("ana@fenixcommerce.com", "raj@fenixcommerce.com", "zoe@fenixcommerce.com"))
    ids = lambda h: [c["id"] for c in client.get("/api/v1/clusters", headers=h).json()["items"]]  # noqa: E731
    assert ids(ana) == ["post-btp-01"]
    assert ids(raj) == ["post-btp-01", "post-btp-02"]
    assert ids(zoe) == []
    body = {"start": "now-1h", "end": "now", "size": 1}
    assert client.post(f"{URL}/_search", json=body, headers=ana).status_code == 200
    r = client.post("/api/v1/clusters/post-btp-02/logs/_search", json=body, headers=ana)
    assert r.status_code == 403 and r.json()["error"]["code"] == "PERMISSION_DENIED"
    assert client.post("/api/v1/clusters/pre-prod-01/logs/_search", json=body, headers=raj).status_code == 403
    assert client.post("/api/v1/clusters/post-btp-02/logs/_export", json=body, headers=raj).status_code == 200
    ref = search(client, body)["hits"][0]["_ref"]
    assert client.get(f"{URL}/_record", params=ref, headers=zoe).status_code == 403
    assert client.get("/api/v1/me", headers=raj).json()["clusters"] == {"*": "view", "pre-prod-01": "none"}
