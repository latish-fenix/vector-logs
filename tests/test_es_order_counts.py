"""scripts/es_order_counts.py: reading index names, merging rollover parts, the S3 files, a failing cluster."""
from __future__ import annotations

import base64
import csv
import datetime as dt
import gzip
import io
import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("es_order_counts", Path(__file__).parent.parent / "scripts" / "es_order_counts.py")
eoc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(eoc)

# names and counts as the ES console shows them on elkop2
CAT = [{"index": n, "health": "green", "status": "open", "docs.count": str(c)} for n, c in [
    ("vuori-clothing.myshopify.com-order-2026.11.001", 135393),
    ("vuori-clothing.myshopify.com-order-2026.11.002", 169780),
    ("vuori-clothing.myshopify.com-order-2026.11.003", 19327),
    ("buck-mason-usa.myshopify.com-order-2026.10", 98245),
    ("buck-mason-usa.myshopify.com-order-2026.11", 50872),
    ("bulbhead-shop4.myshopify.com-order-2026", 44),
    ("bulbheadshop1.myshopify.com-order-2026", 9212),
    ("bulbheadstore2.myshopify.com-order-2026.07", 4375),
    ("burnd.com-jg-order-2026", 18147),
    ("burnd.com-order-2026", 18147),
    ("cadence.com-jg-order-2026", 3072),
    ("chubbies-sandbox.myshopify.com-order-2026", 368),
    ("chubbies-sandbox.myshopify.com-s-order-2026", 368),
    ("dermalogica-us.myshopify.com-s-order-2026.08", 120),
    ("chubbies.myshopify.com-order-2026.07", 34742),
    ("foo.myshopify.com-order-history-2026", 5),
]] + [{"index": "chubbies.myshopify.com-order-2026.06", "health": None, "status": "close", "docs.count": None},
      {"index": ".kibana-order-2026", "health": "green", "status": "open", "docs.count": "1"}]


def by_key(rows):
    return {(r["tenant"], r["year"], r["month"]): r for r in rows}


@pytest.mark.parametrize("name, want", [
    ("vuori-clothing.myshopify.com-order-2026.11.001", ("vuori-clothing.myshopify.com", None, 2026, 11, "001")),
    ("buck-mason-usa.myshopify.com-order-2026.10", ("buck-mason-usa.myshopify.com", None, 2026, 10, None)),
    ("bulbheadshop1.myshopify.com-order-2026", ("bulbheadshop1.myshopify.com", None, 2026, None, None)),
    ("dermalogica-us.myshopify.com-s-order-2026.08", ("dermalogica-us.myshopify.com", "s", 2026, 8, None)),
    ("burnd.com-jg-order-2026", ("burnd.com", "jg", 2026, None, None)),
    ("chubbies-sandbox.myshopify.com-s-order-2026", ("chubbies-sandbox.myshopify.com", "s", 2026, None, None)),
])
def test_names(name, want):
    p = eoc.parse_index(name)
    assert (p["tenant"], p["variant"], p["year"], p["month"], p["part"]) == want


@pytest.mark.parametrize("name", ["foo-order-history-2026", "foo-order-2026.13", "foo-order-26", "foo-orders-2026", "foo-order-2026.11.1"])
def test_not_order_indexes(name):
    assert eoc.parse_index(name) is None


def test_group_merges_parts_and_skips():
    g = eoc.group(CAT)
    rows = by_key(g["rows"])
    vuori = rows[("vuori-clothing.myshopify.com", 2026, 11)]
    assert vuori["total"] == 324500 and [p["part"] for p in vuori["parts"]] == ["001", "002", "003"]
    assert rows[("buck-mason-usa.myshopify.com", 2026, 10)]["total"] == 98245
    assert rows[("bulbheadshop1.myshopify.com", 2026, None)]["total"] == 9212      # whole year
    assert rows[("burnd.com", 2026, None)]["total"] == 18147                       # the -jg- copy is not added
    assert ("bulbhead-shop4.myshopify.com", 2026, None) in rows and ("bulbheadshop1.myshopify.com", 2026, None) in rows
    assert {x["index"]: x["why"] for x in g["skipped"]} == {
        "burnd.com-jg-order-2026": "-jg-", "cadence.com-jg-order-2026": "-jg-",
        "chubbies-sandbox.myshopify.com-s-order-2026": "-s-", "dermalogica-us.myshopify.com-s-order-2026.08": "-s-"}
    assert [x["index"] for x in g["unmatched"]] == ["foo.myshopify.com-order-history-2026"]
    assert [x["index"] for x in g["closed"]] == ["chubbies.myshopify.com-order-2026.06"]
    assert all(not r["tenant"].startswith(".") for r in g["rows"])
    s = g["summary"]
    assert s["tenants"] == 8 and s["indexes"] == 11 and s["orders"] == sum(r["total"] for r in g["rows"])


class FakeS3:
    def __init__(self, fail=False):
        self.files, self.puts, self.fail = {}, [], fail

    def put_object(self, Bucket, Key, Body, **kw):
        if self.fail:
            raise RuntimeError("s3 down")
        self.files[Key] = Body
        self.puts.append(Key)

    def get_object(self, Bucket, Key):
        if Key not in self.files:
            e = RuntimeError("missing")
            e.response = {"Error": {"Code": "NoSuchKey"}}
            raise e
        return {"Body": io.BytesIO(self.files[Key])}

    def json(self, key):
        return json.loads(self.files[eoc.PREFIX + key])

    def rows(self, key):
        text = gzip.decompress(self.files[key]).decode()
        return {(r["minute"], r["tenant"]): r for r in csv.DictReader(io.StringIO(text))}


T0 = dt.datetime(2026, 11, 17, 6, 5, 3, tzinfo=dt.timezone.utc)
MS0 = int(T0.timestamp() * 1000)


def cat(changes=None, extra=()):
    """CAT with some counts changed ({index: count}) and extra indexes ((name, count, created_ms))."""
    out = []
    for x in CAT:
        x = dict(x, **{"creation.date": str(MS0 - 86_400_000)})
        if changes and x["index"] in changes:
            x["docs.count"] = str(changes[x["index"]])
        out.append(x)
    for name, count, created in extra:
        out.append({"index": name, "health": "green", "status": "open", "docs.count": str(count), "creation.date": str(created)})
    return out


def runs(s3, *steps, state=None):
    """steps = (seconds after T0, cat rows); returns the last (status, state)."""
    res = None
    for sec, rows in steps:
        res = eoc.run(s3, lambda rows=rows: rows, now=T0 + dt.timedelta(seconds=sec), state=state)
        state = res[1]
    return res


V3, BM11 = "vuori-clothing.myshopify.com-order-2026.11.003", "buck-mason-usa.myshopify.com-order-2026.10"
KEY = eoc.PREFIX + "inbound/dt=2026-11-17/part-04.csv.gz"


def test_first_run_only_saves_the_starting_counts():
    s3 = FakeS3()
    status, state = runs(s3, (0, cat()))
    assert status["ok"] and status["window"] is None
    assert set(s3.puts) == {eoc.PREFIX + "latest.json", eoc.PREFIX + "status.json"}       # no daily/, no inbound yet
    assert state["idx"][V3] == 19327 and state["at"] == "2026-11-17T06:05:03Z"
    assert "burnd.com-jg-order-2026" not in state["idx"] and "chubbies.myshopify.com-order-2026.06" not in state["idx"]


def test_orders_per_window():
    s3 = FakeS3()
    status, state = runs(s3, (0, cat()), (300, cat({V3: 19400, BM11: 98255})))
    rows = s3.rows(KEY)
    assert rows[("2026-11-17T06:05Z", "_all")]["orders"] == "83"                       # named by the previous run's slot
    assert rows[("2026-11-17T06:05Z", "vuori-clothing.myshopify.com")] == {
        "minute": "2026-11-17T06:05Z", "tenant": "vuori-clothing.myshopify.com", "orders": "73", "docs": "324573",
        "span_min": "5", "src": "live"}
    assert rows[("2026-11-17T06:05Z", "buck-mason-usa.myshopify.com")]["orders"] == "10"
    assert len(rows) == 3                                                            # tenants without orders get no row
    assert status["window"] == {"minute": "2026-11-17T06:05Z", "orders": 83, "spanMin": 5}
    assert status["inboundStart"] == "2026-11-17T06:05Z"
    # nothing new: an _all row with 0 (the window was counted), latest.json not rewritten
    s3.puts.clear()
    status, state = runs(s3, (600, cat({V3: 19400, BM11: 98255})), state=state)
    assert s3.rows(KEY)[("2026-11-17T06:10Z", "_all")]["orders"] == "0"
    assert eoc.PREFIX + "latest.json" not in s3.puts


def test_new_old_and_shrinking_indexes():
    s3 = FakeS3()
    new_ix = ("vuori-clothing.myshopify.com-order-2026.11.004", 40, MS0 + 60_000)          # created after the last run
    old_ix = ("cakeworthy.com-order-2026", 31039, MS0 - 30 * 86_400_000)                    # reopened, made long ago
    skip_ix = ("cakeworthy.com-jg-order-2025", 500, MS0 + 60_000)                          # -jg-: never counted
    status, _ = runs(s3, (0, cat()), (300, cat({BM11: 98000}, extra=[new_ix, old_ix, skip_ix])))
    rows = s3.rows(KEY)
    assert rows[("2026-11-17T06:05Z", "vuori-clothing.myshopify.com")]["orders"] == "40"
    assert ("2026-11-17T06:05Z", "cakeworthy.com") not in rows                              # a start point, not 31,039 orders
    assert ("2026-11-17T06:05Z", "buck-mason-usa.myshopify.com") not in rows               # went down: 0, not negative
    assert rows[("2026-11-17T06:05Z", "_all")]["orders"] == "40"


def test_two_runs_in_one_window_add_up_and_gaps_keep_their_span():
    s3 = FakeS3()
    _, state = runs(s3, (0, cat()), (300, cat({V3: 19337})), (420, cat({V3: 19350})))     # 06:10:03 and a manual 06:12:03
    rows = s3.rows(KEY)
    assert rows[("2026-11-17T06:05Z", "_all")]["orders"] == "10"
    r = rows[("2026-11-17T06:10Z", "vuori-clothing.myshopify.com")]
    assert (r["orders"], r["span_min"]) == ("13", "2")
    _, state = runs(s3, (600, cat({V3: 19360})), state=state)                              # 06:15:03, same 06:10 window
    r = s3.rows(KEY)[("2026-11-17T06:10Z", "vuori-clothing.myshopify.com")]
    assert (r["orders"], r["span_min"], r["docs"]) == ("23", "5", "324500".replace("324500", str(135393 + 169780 + 19360)))
    # two missed runs: the next one covers 15 minutes in one row
    runs(s3, (1500, cat({V3: 19400})), state=state)
    r = s3.rows(KEY)[("2026-11-17T06:15Z", "_all")]
    assert (r["orders"], r["span_min"]) == ("40", "15")


def test_failure_keeps_the_state_so_nothing_is_lost():
    s3 = FakeS3()
    _, state = runs(s3, (0, cat()))

    def boom():
        raise RuntimeError("cannot reach elkop2 at http://x:9200: timed out")
    s3.puts.clear()
    status, state2 = eoc.run(s3, boom, now=T0 + dt.timedelta(minutes=5), state=state)
    assert s3.puts == [eoc.PREFIX + "status.json"] and status["ok"] is False and "timed out" in status["lastError"]
    assert state2 == state
    status, _ = runs(s3, (600, cat({V3: 19350})), state=state2)                              # the next run covers both
    assert s3.rows(KEY)[("2026-11-17T06:05Z", "_all")] == {
        "minute": "2026-11-17T06:05Z", "tenant": "_all", "orders": "23", "docs": str(sum(state2["idx"].values()) + 23),
        "span_min": "10", "src": "live"}
    # S3 down: the window is not saved, so the state does not move on either
    _, state3 = eoc.run(FakeS3(fail=True), lambda: cat({V3: 19400}), now=T0 + dt.timedelta(minutes=15), state=state)
    assert state3 == state
    eoc.run(FakeS3(fail=True), boom, now=T0)


def test_histogram_reads_the_aggregation(monkeypatch):
    def fake_call(cred, path, body=None, **kw):
        assert body["aggs"]["idx"]["aggs"]["t"]["date_histogram"]["fixed_interval"] == "5m"
        assert body["query"]["range"]["created_at"]["format"] == "epoch_millis"
        b = lambda m, n: {"key": int(dt.datetime(2026, 11, 16, 10, m, tzinfo=dt.timezone.utc).timestamp() * 1000), "doc_count": n}  # noqa: E731
        return {"_shards": {"failed": 0}, "aggregations": {"idx": {"buckets": [
            {"key": "vuori-clothing.myshopify.com-order-2026.11.002", "t": {"buckets": [b(0, 5), b(5, 2)]}},
            {"key": "vuori-clothing.myshopify.com-order-2026.11.003", "t": {"buckets": [b(5, 3)]}},
            {"key": "dermalogica-us.myshopify.com-s-order-2026.11", "t": {"buckets": [b(0, 9)]}},
            {"key": "burnd.com-jg-order-2026", "t": {"buckets": [b(0, 9)]}}]}}}
    monkeypatch.setattr(eoc, "es_call", fake_call)
    got = eoc.histogram({}, "created_at", dt.datetime(2026, 11, 16, 10, tzinfo=dt.timezone.utc), dt.datetime(2026, 11, 16, 11, tzinfo=dt.timezone.utc))
    assert got == {("2026-11-16T10:00Z", "vuori-clothing.myshopify.com"): 5, ("2026-11-16T10:05Z", "vuori-clothing.myshopify.com"): 5}
    monkeypatch.setattr(eoc, "es_call", lambda *a, **k: {"_shards": {"failed": 2, "failures": [{"reason": "no field"}]}})
    with pytest.raises(RuntimeError, match="2 shards failed"):
        eoc.histogram({}, "created_at", dt.datetime(2026, 11, 16, tzinfo=dt.timezone.utc), dt.datetime(2026, 11, 16, 1, tzinfo=dt.timezone.utc))


def test_backfill_stops_where_live_rows_start(monkeypatch):
    calls = []

    def fake_hist(cred, field, start, end, insecure=False):
        calls.append((start, end))
        if start.hour == 9:                                            # 3 orders at 09:20 every day
            m = minute_key = start.strftime("%Y-%m-%dT09:20Z")
            return {(m, "bombas.myshopify.com"): 3}
        return {}
    monkeypatch.setattr(eoc, "histogram", fake_hist)
    s3 = FakeS3()
    live = eoc.PREFIX + "inbound/dt=2026-11-17/part-08.csv.gz"
    eoc.write_block(s3, live, {("2026-11-17T11:15Z", "_all"): {"minute": "2026-11-17T11:15Z", "tenant": "_all", "orders": 7,
                                                                "docs": 1, "span_min": 5, "src": "live"}})
    now = dt.datetime(2026, 11, 17, 11, 40, tzinfo=dt.timezone.utc)
    until = dt.datetime(2026, 11, 17, 11, 15, tzinfo=dt.timezone.utc)
    total = eoc.backfill(s3, {}, "created_at", 1, now, until)
    assert total == 6                                                   # 16 Nov and 17 Nov, 09:20 each
    assert calls[0][0] == dt.datetime(2026, 11, 16, tzinfo=dt.timezone.utc) and calls[-1][1] == until
    day16 = s3.rows(eoc.PREFIX + "inbound/dt=2026-11-16/part-08.csv.gz")
    assert len([k for k in day16 if k[1] == "_all"]) == 48             # every window has its _all row, 0 too
    assert day16[("2026-11-16T09:20Z", "bombas.myshopify.com")]["src"] == "backfill"
    assert day16[("2026-11-16T09:20Z", "_all")]["orders"] == "3"
    today = s3.rows(live)
    assert today[("2026-11-17T11:15Z", "_all")]["orders"] == "7"         # the live row is kept
    assert today[("2026-11-17T11:10Z", "_all")]["src"] == "backfill" and ("2026-11-17T11:20Z", "_all") not in today
    # running it again gives the same files (rows in its range are replaced, not added)
    before = dict(s3.files)
    eoc.backfill(s3, {}, "created_at", 1, now, until)
    assert {k: gzip.decompress(v) for k, v in s3.files.items()} == {k: gzip.decompress(v) for k, v in before.items()}


def test_cat_indices_over_http_with_basic_auth():
    seen = {}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            seen["path"], seen["auth"] = self.path, self.headers.get("Authorization")
            body = json.dumps(CAT[:3]).encode()
            self.send_response(200 if seen["auth"] == "Basic " + base64.b64encode(b"u:p w").decode() else 401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}"
    try:
        rows = eoc.cat_indices({"url": url + "/", "username": "u", "password": "p w"})
        assert len(rows) == 3 and seen["path"].startswith("/_cat/indices/*-order-*?format=json")
        assert "expand_wildcards=open,closed" in seen["path"]
        with pytest.raises(RuntimeError, match="HTTP 401"):
            eoc.cat_indices({"url": url, "username": "u", "password": "wrong"})
    finally:
        srv.shutdown()
    with pytest.raises(RuntimeError, match="cannot reach"):
        eoc.cat_indices({"url": "http://127.0.0.1:9", "username": "", "password": ""}, timeout=2)


def test_credentials_from_env_file(tmp_path, monkeypatch):
    for k in ("ES_URL", "ES_USERNAME", "ES_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    f = tmp_path / ".env"
    f.write_text("# elkop2\nES_URL=http://node6.example:9200\nES_USERNAME=reader\nexport ES_PASSWORD='p=a#ss \"x\"'\n\n")
    f.chmod(0o600)
    assert eoc.credentials(f) == {"url": "http://node6.example:9200", "username": "reader", "password": 'p=a#ss "x"'}
    monkeypatch.setenv("ES_USERNAME", "other")                    # the environment wins
    assert eoc.credentials(f)["username"] == "other"
    monkeypatch.delenv("ES_USERNAME")
    f.write_text("ES_URL=http://x:9200\nES_USERNAME=reader\n")
    with pytest.raises(RuntimeError, match="ES_PASSWORD is empty"):
        eoc.credentials(f)
    with pytest.raises(RuntimeError, match="no ES_URL"):
        eoc.credentials(tmp_path / "missing.env")


def test_histogram_uses_the_first_date_field_an_order_has(monkeypatch):
    seen = []

    def fake_call(cred, path, body=None, **kw):
        seen.append(body["query"])
        f = body["aggs"]["idx"]["aggs"]["t"]["date_histogram"]["field"]
        n = {"order_summary.fenix_crtd_dttm": 7, "order_summary.crtd_dttm.actual_dttm": 3, "created_at": 1}[f]
        k = int(dt.datetime(2026, 11, 16, 10, 5, tzinfo=dt.timezone.utc).timestamp() * 1000)
        return {"_shards": {"failed": 0}, "aggregations": {"idx": {"buckets": [
            {"key": "aquiskhairpep.myshopify.com-order-2026", "t": {"buckets": [{"key": k, "doc_count": n}]}}]}}}
    monkeypatch.setattr(eoc, "es_call", fake_call)
    got = eoc.histogram({}, eoc.DATE_FIELDS, dt.datetime(2026, 11, 16, 10, tzinfo=dt.timezone.utc), dt.datetime(2026, 11, 16, 11, tzinfo=dt.timezone.utc))
    assert got == {("2026-11-16T10:05Z", "aquiskhairpep.myshopify.com"): 11}
    assert "range" in seen[0]                                                   # first field: no exclusions
    assert [x["exists"]["field"] for x in seen[2]["bool"]["must_not"]] == ["order_summary.fenix_crtd_dttm", "order_summary.crtd_dttm.actual_dttm"]
    assert "created_at" in seen[2]["bool"]["must"][0]["range"]


def test_compare_lines_up_live_and_dates(monkeypatch):
    s3 = FakeS3()
    runs(s3, (0, cat()), (300, cat({V3: 19400, BM11: 98255})))                  # live: vuori 73, buck-mason 10 at 06:05
    monkeypatch.setattr(eoc, "histogram", lambda cred, field, start, end, insecure=False: (
        {("2026-11-17T06:05Z", "vuori-clothing.myshopify.com"): 70, ("2026-11-17T06:05Z", "kuiu-shop.myshopify.com"): 2}
        if (start, end) == (dt.datetime(2026, 11, 17, 6, 5, tzinfo=dt.timezone.utc), dt.datetime(2026, 11, 17, 6, 10, tzinfo=dt.timezone.utc)) else {}))
    rows = eoc.compare(s3, {}, eoc.DATE_FIELDS, "2026-11-17")
    assert rows == [("_all", 83, 72), ("vuori-clothing.myshopify.com", 73, 70), ("buck-mason-usa.myshopify.com", 10, 0),
                    ("kuiu-shop.myshopify.com", 0, 2)]
    with pytest.raises(RuntimeError, match="no live windows"):
        eoc.compare(s3, {}, eoc.DATE_FIELDS, "2026-11-16")
