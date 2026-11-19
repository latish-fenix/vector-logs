"""scripts/ab_session_counts.py: session ID kinds, reading the aggregation, the S3 files, re-runs and gaps."""
from __future__ import annotations

import csv
import datetime as dt
import gzip
import importlib.util
import io
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("ab_session_counts", Path(__file__).parent.parent / "scripts" / "ab_session_counts.py")
ab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ab)

UTC = dt.timezone.utc
SPO = "shoppremiumoutlets.myshopify.com"


@pytest.mark.parametrize("v, want", [
    ("MTc5NTA4NTIzODgyNC1zaG9wLnNpbW9uLmNvbS05MTkwOA==", "valid"),
    ("M", "valid"),
    ("NA", "NA"), ("ORDER_LOOKUP_CALL", "ORDER_LOOKUP_CALL"), ("xyz123", "xyz123"), ("novalue", "novalue"),
    ("fenix-health-vuori-cluster", "health"),
    ("", "empty"), (None, "missing"), ("x" * 300, "missing"),
    ("262838632", "numbers"),
    ("8628b242-3301-4180-b6b1-1a3efc0d9e11", "uuid"),
    ('"MTc3ODUxODcyNTU1OS12dW9yaWNsb', "other"), ("app-7640275878145-179500972792", "other"), ("2ozz0fyvjih", "other"),
    ("na", "other"),
])
def test_classify(v, want):
    assert ab.classify(v) == want


@pytest.mark.parametrize("index, store", [
    ("ammonyc.myshopify.com-session_history-2026.11", "ammonyc.myshopify.com"),
    ("bombas.myshopify.com-cop-derrh-2026.10", "bombas.myshopify.com"),
    ("fenixcommerce-session_history-2026.11", "fenixcommerce"),
    ("x.com-session_history-2026.W46", "x.com"),
    ("x.com-session_history-2026-47", "x.com"),
    ("x.com-session_history-2026.11.19", "x.com"),
    ("x.com-cop-derrh-2026.11-000002", "x.com"),
    ("something-else", "something-else"),
])
def test_store_from_index(index, store):
    assert ab.store_of(index) == store


def test_filters_have_every_category_and_other():
    f = ab.category_filters("sessionId.keyword")["filters"]
    assert list(f["filters"]) == ["valid", "health", "NA", "ORDER_LOOKUP_CALL", "xyz123", "novalue", "empty", "missing", "numbers", "uuid"]
    assert f["other_bucket_key"] == "other"
    assert f["filters"]["valid"] == {"prefix": {"sessionId.keyword": "M"}}


# ------------------------------------------------------------------ a fake cluster
T = dt.datetime(2026, 11, 19, 10, 0, tzinfo=UTC)


def cats(**n):
    keys = ["valid", "health", "NA", "ORDER_LOOKUP_CALL", "xyz123", "novalue", "empty", "missing", "numbers", "uuid", "other"]
    return {"buckets": {k: {"doc_count": n.get(k, 0)} for k in keys}}


def pt(key, total, variants=None, **n):
    b = {"key": key, "doc_count": total, "cat": cats(**n)}
    if variants is not None:
        b["test"] = {"buckets": [{"key": "EDD AB", "doc_count": sum(variants.values()),
                                  "var": {"buckets": [{"key": k, "doc_count": v} for k, v in variants.items()]}}]}
    return b


def agg(windows):
    """windows = {minute dt: {index: [pt buckets]}}"""
    return {"_shards": {"failed": 0}, "aggregations": {"t": {"buckets": [
        {"key": ab.ms(m), "doc_count": 0, "idx": {"buckets": [{"key": ix, "doc_count": 0, "pt": {"buckets": pts}} for ix, pts in idx.items()]}}
        for m, idx in windows.items()]}}}


class FakeES:
    def __init__(self, session=None, cop=None, hits=None):
        self.session, self.cop, self.hits = session or {}, cop or {}, hits or {}
        self.calls = []

    def __call__(self, cred, pattern, body, what):
        name = "session" if "session_history" in pattern else "cop"
        rng = body["query"]["range"] if "range" in body["query"] else body["query"]["bool"]["filter"][0]["range"]
        r = next(iter(rng.values()))
        a, b = ab.from_ms(r["gte"]), ab.from_ms(r["lt"])
        self.calls.append((name, body["size"], a, b))
        if body["size"] == 0:
            src = self.session if name == "session" else self.cop
            return agg({m: v for m, v in src.items() if a <= m < b})
        hs = [h for h in self.hits.get(name, []) if a <= ab.from_ms(h["sort"][0]) < b]
        return {"_shards": {"failed": 0}, "hits": {"total": {"value": len(hs)}, "hits": hs}}


def hit(name, index, at, sid, page, order, _id="d1"):
    src = {"sessionId": sid, "pageType": page, "orderInfo": {"orderId": order}, "orderId": None} if name == "session" else \
          {"sessionTrackId": sid, "request": {"pageType": page, "orderId": order}, "orderId": order}
    return {"_index": index, "_id": _id, "_source": src, "sort": [ab.ms(at)]}


class FakeS3:
    def __init__(self):
        self.files: dict[str, bytes] = {}

    def get_object(self, Bucket, Key):
        if Key not in self.files:
            e = Exception("missing")
            e.response = {"Error": {"Code": "NoSuchKey"}}
            raise e
        return {"Body": io.BytesIO(self.files[Key])}

    def put_object(self, Bucket, Key, Body, **kw):
        self.files[Key] = Body

    def rows(self, key):
        return list(csv.DictReader(io.StringIO(gzip.decompress(self.files[ab.PREFIX + key]).decode())))

    def json(self, key):
        return json.loads(self.files[ab.PREFIX + key])


SH = f"{SPO}-session_history-2026.11"
CD = f"{SPO}-cop-derrh-2026.11"


def es_one_window():
    m = T + dt.timedelta(minutes=5)
    return FakeES(
        session={m: {SH: [pt("OC", 31, variants={"Original": 20, "Hidden": 11}, valid=25, ORDER_LOOKUP_CALL=5, xyz123=1),
                          pt("PDP", 400, valid=400)],
                     "fenixcommerce-session_history-2026.11": [pt("COP", 7, health=7)]}},
        cop={m: {CD: [pt("COP", 10, valid=8, xyz123=2), pt("cop", 5, valid=4, NA=1)]}},
        hits={"session": [hit("session", SH, m + dt.timedelta(seconds=40), "ORDER_LOOKUP_CALL", "OC", "19004270313532")],
              "cop": [hit("cop", CD, m + dt.timedelta(seconds=10), "xyz123", "cop", "7798832857326", "c1")]})


def test_counts_merge_pages_and_mark_every_window(monkeypatch):
    es = es_one_window()
    monkeypatch.setattr(ab, "search", es)
    s = ab.counts({}, "session", T, T + dt.timedelta(minutes=15))
    assert s[("2026-11-19T10:00Z", "session", "_all", "", "total", "")] == 0
    assert s[("2026-11-19T10:05Z", "session", "_all", "", "total", "")] == 438
    assert s[("2026-11-19T10:10Z", "session", "_all", "", "total", "")] == 0
    assert s[("2026-11-19T10:05Z", "session", SPO, "OC", "ORDER_LOOKUP_CALL", "")] == 5
    assert s[("2026-11-19T10:05Z", "session", SPO, "OC", "variant", "EDD AB / Original")] == 20
    assert ("2026-11-19T10:05Z", "session", SPO, "PDP", "NA", "") not in s          # zeros are not written
    c = ab.counts({}, "cop", T, T + dt.timedelta(minutes=15))
    assert c[("2026-11-19T10:05Z", "cop", SPO, "COP", "total", "")] == 15            # COP + cop
    assert c[("2026-11-19T10:05Z", "cop", SPO, "COP", "valid", "")] == 12


def test_bad_ids_rows(monkeypatch):
    monkeypatch.setattr(ab, "search", es_one_window())
    rows, left = ab.bad_ids({}, "session", T, T + dt.timedelta(minutes=15))
    assert left == 0
    assert rows == [{"minute": "2026-11-19T10:05Z", "source": "session", "store": SPO, "page": "OC",
                     "category": "ORDER_LOOKUP_CALL", "session_id": "ORDER_LOOKUP_CALL", "order_id": "19004270313532",
                     "doc_id": "d1", "at": "2026-11-19T10:05:40Z"}]
    rows, _ = ab.bad_ids({}, "cop", T, T + dt.timedelta(minutes=15))
    assert rows[0]["page"] == "COP" and rows[0]["order_id"] == "7798832857326" and rows[0]["category"] == "xyz123"


def test_bad_ids_query_keeps_oc_only_for_sessions():
    q = []

    def fake(cred, pattern, body, what):
        q.append(body)
        return {"_shards": {}, "hits": {"total": {"value": 0}, "hits": []}}
    old = ab.search
    ab.search = fake
    try:
        ab.bad_ids({}, "session", T, T + dt.timedelta(minutes=5))
        ab.bad_ids({}, "cop", T, T + dt.timedelta(minutes=5))
    finally:
        ab.search = old
    assert {"terms": {"pageType.keyword": ["OC", "oc"]}} in q[0]["query"]["bool"]["filter"]
    assert len(q[1]["query"]["bool"]["filter"]) == 1
    assert {"prefix": {"sessionTrackId.keyword": "fenix-health-"}} in q[1]["query"]["bool"]["must_not"]


def test_run_writes_files_and_a_rerun_replaces_instead_of_adding(monkeypatch):
    es = es_one_window()
    monkeypatch.setattr(ab, "search", es)
    s3 = FakeS3()
    now = T + dt.timedelta(minutes=15, seconds=20)          # counts 10:00 to 10:15
    st, state = ab.run(s3, lambda: {}, now=now, state={})
    assert st["ok"] and state["doneTo"] == "2026-11-19T10:15Z" and state["first"] == "2026-11-19T10:00Z"
    rows = s3.rows("counts/dt=2026-11-19/part-08.csv.gz")
    assert [r for r in rows if r["store"] == "_all" and r["source"] == "session"] == [
        {"minute": f"2026-11-19T10:{m}Z", "source": "session", "store": "_all", "page": "", "category": "total", "value": "", "count": c}
        for m, c in (("00", "0"), ("05", "438"), ("10", "0"))]
    ids = s3.rows("ids/dt=2026-11-19/part-08.csv.gz")
    assert [(r["source"], r["order_id"]) for r in ids] == [("cop", "7798832857326"), ("session", "19004270313532")]
    assert st["counted"]["bad"] == 9 and st["counted"]["ids"] == 2
    n = len(rows)
    st, state = ab.run(s3, lambda: {}, now=now + dt.timedelta(minutes=5), state=state)   # 10:05 to 10:20
    rows = s3.rows("counts/dt=2026-11-19/part-08.csv.gz")
    assert len(rows) == n + 2                                # only the new 10:15 window's two _all rows
    oc = [r for r in rows if r["page"] == "OC" and r["category"] == "total"]
    assert [r["count"] for r in oc] == ["31"]                # counted twice, written once
    assert len(s3.rows("ids/dt=2026-11-19/part-08.csv.gz")) == 2
    assert s3.json("status.json")["doneTo"] == "2026-11-19T10:20Z"


def test_a_gap_is_caught_up_and_a_long_one_capped(monkeypatch):
    es = FakeES()
    monkeypatch.setattr(ab, "search", es)
    s3 = FakeS3()
    st, _ = ab.run(s3, lambda: {}, now=T + dt.timedelta(hours=1, seconds=5), state={"doneTo": "2026-11-19T10:00Z"})
    assert st["counted"]["from"] == "2026-11-19T10:00Z" and st["counted"]["windows"] == 12
    st, _ = ab.run(s3, lambda: {}, now=T + dt.timedelta(days=2), state={"doneTo": "2026-11-19T10:00Z"})
    assert st["counted"]["from"] == "2026-11-20T22:00Z"      # 12 hours back at most
    assert st["counted"]["windows"] == 144


def test_queries_are_cut_into_chunks_and_files(monkeypatch):
    es = FakeES()
    monkeypatch.setattr(ab, "search", es)
    s3 = FakeS3()
    a = dt.datetime(2026, 11, 19, 11, 30, tzinfo=UTC)
    ab.count_range(s3, {}, a, a + dt.timedelta(hours=1))
    spans = sorted({(c[2], c[3]) for c in es.calls})
    assert spans[0] == (a, a + dt.timedelta(minutes=15))
    assert all(b - x <= ab.CHUNK for x, b in spans)
    assert ab.PREFIX + "counts/dt=2026-11-19/part-08.csv.gz" in s3.files
    assert ab.PREFIX + "counts/dt=2026-11-19/part-12.csv.gz" in s3.files


def test_failure_keeps_the_state(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("cannot reach rh1")
    monkeypatch.setattr(ab, "search", boom)
    s3 = FakeS3()
    st, state = ab.run(s3, lambda: {}, now=T, state={"doneTo": "2026-11-19T09:50Z"})
    assert not st["ok"] and "cannot reach" in st["lastError"]
    assert state == {"doneTo": "2026-11-19T09:50Z"}
    assert s3.json("status.json")["ok"] is False


def test_another_cluster_gets_its_own_files(monkeypatch):
    for k in ("AB_ENV_FILE", "AB_STATE", "AB_PREFIX"):
        monkeypatch.delenv(k, raising=False)
    old = (ab.CLUSTER, ab.ENV_FILE, ab.STATE_FILE, ab.PREFIX)
    try:
        ab.use_cluster("rh2")
        assert ab.PREFIX == "es-ab-test/rh2/" and ab.ENV_FILE.name == ".env.rh2" and ab.STATE_FILE.name == ".ab_state.rh2"
        ab.use_cluster("rh1")
        assert ab.PREFIX == "es-ab-test/"
    finally:
        ab.CLUSTER, ab.ENV_FILE, ab.STATE_FILE, ab.PREFIX = old
