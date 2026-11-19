"""A/B test page: the files of scripts/ab_session_counts.py read back per store, page and session ID kind."""
from __future__ import annotations

import csv
import gzip
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.abtest import AbTestService
from app.logs import LogService
from app.main import create_app
from conftest import ROOT_H, make_settings

UTC = timezone.utc
NOW = datetime(2026, 11, 19, 12, 2, 10, tzinfo=UTC)
MS = lambda d: int(d.timestamp() * 1000)  # noqa: E731
SPO, BOM = "shoppremiumoutlets.myshopify.com", "bombas.myshopify.com"
COUNTS = ["minute", "source", "store", "page", "category", "value", "count"]
IDS = ["minute", "source", "store", "page", "category", "session_id", "order_id", "doc_id", "at"]


def write(root: Path, kind: str, cols: list[str], rows: list[dict]) -> None:
    blocks: dict[str, list[dict]] = {}
    for r in rows:
        hh = int(r["minute"][11:13])
        blocks.setdefault(f"dt={r['minute'][:10]}/part-{hh - hh % 4:02d}.csv.gz", []).append(r)
    for name, rs in blocks.items():
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=cols, lineterminator="\n", extrasaction="ignore")
        w.writeheader()
        w.writerows(rs)
        p = root / "es-ab-test" / kind / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(gzip.compress(buf.getvalue().encode()))


def r(minute, source, store, page, category, count, value=""):
    return dict(minute=minute, source=source, store=store, page=page, category=category, value=value, count=count)


@pytest.fixture()
def data(tmp_path) -> Path:
    root = tmp_path / "logs"
    rows = []
    t = datetime(2026, 11, 19, 8, 0, tzinfo=UTC)
    while t < datetime(2026, 11, 19, 12, 0, tzinfo=UTC):              # 08:00 … 11:55 counted
        m = t.strftime("%Y-%m-%dT%H:%MZ")
        rows += [r(m, "session", "_all", "", "total", 0), r(m, "cop", "_all", "", "total", 0)]
        if t.hour == 10 and t.minute < 10:                             # two windows with data
            rows += [r(m, "session", SPO, "PDP", "total", 100), r(m, "session", SPO, "PDP", "valid", 100),
                     r(m, "session", SPO, "OC", "total", 10), r(m, "session", SPO, "OC", "valid", 7),
                     r(m, "session", SPO, "OC", "ORDER_LOOKUP_CALL", 2), r(m, "session", SPO, "OC", "xyz123", 1),
                     r(m, "session", SPO, "OC", "variant", 6, "EDD AB / Original"),
                     r(m, "session", SPO, "OC", "variant", 4, "EDD AB / Hidden"),
                     r(m, "session", SPO, "COP", "total", 5), r(m, "session", SPO, "COP", "health", 1),
                     r(m, "session", SPO, "COP", "valid", 4),
                     r(m, "cop", SPO, "COP", "total", 6), r(m, "cop", SPO, "COP", "valid", 4), r(m, "cop", SPO, "COP", "xyz123", 2),
                     r(m, "session", BOM, "OC", "total", 20), r(m, "session", BOM, "OC", "numbers", 20),
                     r(m, "session", "fenixcommerce", "COP", "total", 9), r(m, "session", "fenixcommerce", "COP", "health", 9)]
        t += timedelta(minutes=5)
    write(root, "counts", COUNTS, rows)
    write(root, "ids", IDS, [
        dict(minute="2026-11-19T10:00Z", source="session", store=SPO, page="OC", category="ORDER_LOOKUP_CALL",
             session_id="ORDER_LOOKUP_CALL", order_id="1001", doc_id="a", at="2026-11-19T10:01:00Z"),
        dict(minute="2026-11-19T10:05Z", source="session", store=SPO, page="OC", category="xyz123",
             session_id="xyz123", order_id="1002", doc_id="b", at="2026-11-19T10:06:00Z"),
        dict(minute="2026-11-19T10:05Z", source="session", store=SPO, page="OC", category="ORDER_LOOKUP_CALL",
             session_id="ORDER_LOOKUP_CALL", order_id="1003", doc_id="c", at="2026-11-19T10:07:00Z"),
        dict(minute="2026-11-19T10:05Z", source="cop", store=SPO, page="COP", category="xyz123",
             session_id="xyz123", order_id="2001", doc_id="d", at="2026-11-19T10:08:00Z"),
    ])
    (root / "es-ab-test" / "status.json").write_text(json.dumps({"ok": True, "doneTo": "2026-11-19T12:00Z"}))
    return root


@pytest.fixture()
def ab(tmp_path, data) -> AbTestService:
    s = make_settings(tmp_path, data)
    return AbTestService(s, LogService(s), now=lambda: NOW)


A, B = MS(datetime(2026, 11, 19, 8, 0, tzinfo=UTC)), MS(datetime(2026, 11, 19, 12, 0, tzinfo=UTC))


def test_store_list(ab):
    s = ab.summary(A, B, "UTC")
    assert s["asOf"] == B and s["windows"] == {"session": 48, "cop": 48} and s["step"] == 5 * 60_000
    names = [x["store"] for x in s["stores"]]
    assert names == [SPO, BOM, "fenixcommerce"]                   # by shopper entries; health checks are no shoppers
    spo = s["stores"][0]
    assert spo["entries"] == 2 * (100 + 10 + 4) and spo["pages"] == {"PDP": 200, "OC": 20, "COP": 8}
    assert (spo["oc"], spo["ocBad"], spo["ocBadPct"]) == (20, 6, 30.0)
    assert (spo["cop"], spo["copBad"], spo["copBadPct"]) == (12, 4, 33.33)
    assert spo["abTest"] and not s["stores"][1]["abTest"]
    assert s["stores"][1]["ocBadPct"] == 100.0
    assert s["status"]["doneTo"] == "2026-11-19T12:00Z" and s["store"] is None


def test_one_store(ab):
    s = ab.summary(A, B, "UTC", SPO)["store"]
    i = (MS(datetime(2026, 11, 19, 10, 0, tzinfo=UTC)) - A) // (5 * 60_000)
    assert list(s["series"]) == ["PDP", "COP", "OC"]
    assert s["series"]["PDP"][i:i + 3] == [100, 100, 0] and s["series"]["COP"][i] == 4    # health left out
    assert s["badOc"][i] == 3
    oc = next(x for x in s["sessionCheck"] if x["page"] == "OC")
    assert (oc["total"], oc["valid"], oc["bad"], oc["badPct"]) == (20, 14, 6, 30.0)
    assert [(c["category"], c["count"]) for c in oc["categories"]] == [("valid", 14), ("ORDER_LOOKUP_CALL", 4), ("xyz123", 2)]
    assert [x["page"] for x in s["sessionCheck"]] == ["PDP", "COP", "OC"]
    assert s["copCheck"][0]["badPct"] == 33.33
    assert (s["cop"], s["copBad"]) == (12, 4)                     # the counts are not overwritten by the tables
    assert s["variants"] == [{"page": "OC", "variant": "EDD AB / Original", "count": 12, "pct": 60.0},
                             {"page": "OC", "variant": "EDD AB / Hidden", "count": 8, "pct": 40.0}]
    assert s["variantSeries"]["EDD AB / Hidden"][i] == 4


def test_uncounted_buckets_are_null(ab):
    a = MS(datetime(2026, 11, 19, 6, 0, tzinfo=UTC))
    s = ab.summary(a, B, "UTC", SPO)
    assert s["store"]["series"]["PDP"][0] is None                 # 06:00: no window counted
    assert s["store"]["series"]["PDP"][(A - a) // (5 * 60_000)] == 0
    assert s["countedFrom"] == A


def test_wide_range_uses_bigger_buckets_in_the_zone(ab):
    a = MS(datetime(2026, 11, 16, 0, 0, tzinfo=UTC))
    s = ab.summary(a, B, "IST", SPO)
    assert s["step"] == 3600_000 and (s["start"] + 330 * 60_000) % 3600_000 == 0
    assert sum(v for v in s["store"]["series"]["PDP"] if v) == 200


def test_unknown_store_and_bad_range(ab):
    assert ab.summary(A, B, "UTC", "nope.com")["store"] == {"store": "nope.com", "found": False}
    with pytest.raises(Exception):
        ab.summary(B, A)
    with pytest.raises(Exception):
        ab.summary(A - 40 * 86_400_000, B)


def test_id_list(ab):
    x = ab.id_list(A, B, SPO, "session", "OC")
    assert [r["orderId"] for r in x["rows"]] == ["1003", "1002", "1001"]
    assert x["values"] == [{"value": "ORDER_LOOKUP_CALL", "count": 2}, {"value": "xyz123", "count": 1}]
    x = ab.id_list(A, B, SPO, "session", "OC", "xyz123")
    assert [r["orderId"] for r in x["rows"]] == ["1002"]
    x = ab.id_list(A, B, SPO, "cop", None, "bad", limit=1)
    assert x["total"] == 1 and x["rows"][0]["orderId"] == "2001"


def test_api(tmp_path, data, monkeypatch):
    app = create_app(make_settings(tmp_path, data))
    monkeypatch.setattr(app.state.abtest, "now", lambda: NOW)
    c = TestClient(app)
    res = c.get("/api/v1/abtest/summary", params={"from": A, "to": B, "store": SPO}, headers=ROOT_H)
    assert res.status_code == 200 and res.json()["store"]["ocBad"] == 6
    res = c.get("/api/v1/abtest/ids", params={"from": "2026-11-19T08:00:00Z", "to": B, "store": SPO, "page": "OC"}, headers=ROOT_H)
    assert res.status_code == 200 and res.json()["total"] == 3
    assert c.get("/api/v1/abtest/ids", params={"from": A, "to": B, "store": SPO, "source": "x"}, headers=ROOT_H).status_code == 400
    assert c.get("/api/v1/abtest/status", headers=ROOT_H).json()["collector"]["ok"] is True


def test_finished_files_are_copied_again_when_a_backfill_rewrote_them(tmp_path):
    """A finished 4-hour file is kept locally, but with revalidate_seconds it is checked (HEAD) again and
    copied anew when S3 has a newer one (a backfill rewrote it after the page first read it)."""
    import os
    import time as _time
    from datetime import date
    from types import SimpleNamespace
    from app.ops import MetricFiles

    class S3:
        def __init__(self):
            self.body, self.modified, self.gets, self.heads = b"v1", datetime(2026, 11, 10, tzinfo=UTC), 0, 0

        def get_object(self, Bucket, Key):
            self.gets += 1
            return {"Body": io.BytesIO(self.body)}

        def head_object(self, Bucket, Key):
            self.heads += 1
            return {"LastModified": self.modified}

    s3 = S3()
    s = make_settings(tmp_path, tmp_path / "logs")
    f = MetricFiles(s, SimpleNamespace(s3=s3, bucket="b"), "es-ab-test/counts/", "t", revalidate_seconds=300)
    now = MS(datetime(2026, 11, 19, 12, tzinfo=UTC))
    p = f.get(date(2026, 11, 12), 0, now)
    assert Path(p).read_bytes() == b"v1" and s3.gets == 1
    assert f.get(date(2026, 11, 12), 0, now) == p and s3.heads == 0           # finished, checked a moment ago
    old = _time.time() - 600
    os.utime(p, (old, old))
    assert f.get(date(2026, 11, 12), 0, now) == p and s3.heads == 1 and s3.gets == 1   # unchanged in S3
    os.utime(p, (old, old))
    s3.body, s3.modified = b"v2", datetime.now(UTC)                           # the backfill rewrote it
    assert Path(f.get(date(2026, 11, 12), 0, now)).read_bytes() == b"v2" and s3.gets == 2
