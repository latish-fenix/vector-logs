#!/usr/bin/env python3
"""A/B test sessions per store, page type and session ID kind, every 5 minutes, from the EDD session
cluster (eddsderrh1, "RH1") to S3, for the A/B test page of Vector Logs.

Two kinds of index, one per store per month (any rollover works: the queries go by the documents'
own time, over every index of the pattern):
    <store>-session_history-YYYY.MM   source "session": PDP, CART, COP and OC (order confirmation) entries
                                      time requestDttm, page pageType, session ID sessionId,
                                      order orderInfo.orderId, A/B variant splitTestInfo.testName / variantName
    <store>-cop-derrh-YYYY.MM         source "cop": the unique COP (cart / checkout) entries ("COP DI")
                                      time triggeredDateTime, page request.pageType, session ID sessionTrackId,
                                      order orderId

Every run counts the last 15 minutes again (entries saved late are picked up) and catches up after
missed runs (up to 12 hours). The windows it counts replace the ones already in S3, so a run, a re-run
and a backfill never add up twice.

    s3://<bucket>/es-ab-test/counts/dt=YYYY-MM-DD/part-HH.csv.gz    HH = 00, 04, … 20 (UTC, 4 hours each)
        minute,source,store,page,category,value,count
        2026-11-19T10:00Z,session,_all,,total,,5412           one per source per window, 0 too: "counted"
        2026-11-19T10:00Z,session,shoppremiumoutlets.myshopify.com,OC,total,,31
        2026-11-19T10:00Z,session,shoppremiumoutlets.myshopify.com,OC,valid,,25
        2026-11-19T10:00Z,session,shoppremiumoutlets.myshopify.com,OC,ORDER_LOOKUP_CALL,,5
        2026-11-19T10:00Z,session,shoppremiumoutlets.myshopify.com,OC,variant,EDD-ShipCost AB Test (45/45/5/5) / EDD-Original/ShipCost-Test,12
    s3://<bucket>/es-ab-test/ids/dt=YYYY-MM-DD/part-HH.csv.gz       the entries with a bad session ID, for the list
        minute,source,store,page,category,session_id,order_id,doc_id,at   behind a click: every page of session and of
                                                                           cop (health checks left out)
    s3://<bucket>/es-ab-test/ua/dt=YYYY-MM-DD/part-HH.csv.gz        browser / device of the shoppers, per hour
        minute,store,page,dim,value,entries,bad                     (session_history only, health checks left out):
                                                                    dim = browser, os, brand, app (in-app webview);
                                                                    the top 10 values per store / page / hour, the
                                                                    rest as _other; the current hour is rewritten
    s3://<bucket>/es-ab-test/conv/dt=YYYY-MM-DD/part-00.csv.gz      unique valid session IDs per UTC day (conversion):
        minute,store,page,group,sessions                            page PDP / COP / OC, group hidden / shown / none;
                                                                    today refreshed every 15 minutes
    s3://<bucket>/es-ab-test/status.json                           every run: last run, last success, last error

  page      upper case (cop and COP are the same page)
  EDD rows  OC only (session): category edd (orders), edd_cart / edd_ship (cents), edd:<kind> (orders per session
            ID kind), value = hidden (responses.eddsHidden), shown (responseMetadata.atleastSingleEDDShown), none
  category  total, valid (session ID starts with M), health (fenix-health-… health checks),
            one per known bad value (NA, ORDER_LOOKUP_CALL, xyz123, novalue; AB_KNOWN_VALUES to change),
            empty (""), missing (no session ID, or longer than 256 characters), numbers (digits only),
            uuid, other (anything else); variant rows: entries per A/B test / variant (session only)

Login: .env.rh1 next to this script (ES_URL, ES_USERNAME, ES_PASSWORD; chmod 600); the environment wins.
--cluster NAME reads .env.NAME and writes es-ab-test/NAME/ instead (rh1 keeps the plain prefix).

Needs: Python 3.9+, boto3; s3:GetObject / s3:PutObject / s3:ListBucket on es-ab-test/; read on the indexes.

Examples
    python3 ab_session_counts.py                       # normal 5-minute run (cron)
    python3 ab_session_counts.py --dry-run             # count the last 15 minutes and print; write nothing
    python3 ab_session_counts.py --backfill-days 7     # one-off: count from midnight UTC 7 days ago up to now
"""
from __future__ import annotations

import argparse
import base64
import csv
import datetime as dt
import gzip
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUCKET = os.environ.get("AB_BUCKET", "fenix-vector-ecs-logs")
REGION = os.environ.get("AWS_REGION", "us-west-2")
CLUSTER = "rh1"
PREFIX = os.environ.get("AB_PREFIX", "es-ab-test/")
ENV_FILE = Path(os.environ.get("AB_ENV_FILE", str(HERE / ".env.rh1")))
STATE_FILE = Path(os.environ.get("AB_STATE", str(HERE / ".ab_state.rh1")))
TIMEOUT = float(os.environ.get("AB_TIMEOUT", "120"))
VERSION = 1
STEP = 300                                     # seconds per window
BLOCK_HOURS = 4                                # 6 files per day
LOOKBACK = dt.timedelta(minutes=15)            # counted again every run (late entries)
CATCH_UP = dt.timedelta(hours=12)              # longest gap a run fills by itself
CHUNK = dt.timedelta(minutes=int(os.environ.get("AB_CHUNK_MIN", "15")))   # time per query
MAX_IDS = 10_000                               # bad-ID entries read per query
ALL = "_all"
KNOWN = [v for v in (x.strip() for x in os.environ.get("AB_KNOWN_VALUES", "NA,ORDER_LOOKUP_CALL,xyz123,novalue").split(","))
         if v and not v.startswith("M") and not v.startswith("fenix-health-")]
COUNT_COLUMNS = ["minute", "source", "store", "page", "category", "value", "count"]
ID_COLUMNS = ["minute", "source", "store", "page", "category", "session_id", "order_id", "doc_id", "at",
              "browser", "os", "device", "brand", "app", "edd", "cart", "ship"]
UA_COLUMNS = ["minute", "store", "page", "dim", "value", "entries", "bad"]
# browser / device of the shopper (session_history only): dimension -> field under userAgentInfo
UA_DIMS = {"browser": "browserName", "os": "osName", "brand": "deviceBrand", "app": "webviewAppName"}
UA_TOP = 10                                    # values kept per store / page / dimension and hour; the rest = "_other"
UA_OTHER = "_other"
UA_UNKNOWN = "(unknown)"
# order confirmations (session_history): EDD hidden / shown / none, per session ID kind, with cart and shipping (cents)
EDD_PAGES = ["OC", "oc"]
EDD_HIDDEN = {"term": {"responses.eddsHidden": True}}
CART_FIELD, SHIP_FIELD = "request.billingInfo.cartTotal.amount", "request.billingInfo.shipCost.amount"
# conversion: unique valid session IDs per day, store, page and EDD group
CONV_PAGES = ["PDP", "pdp", "COP", "cop", "OC", "oc"]
CONV_COLUMNS = ["minute", "store", "page", "group", "sessions"]
CONV_EVERY = dt.timedelta(minutes=15)           # today's unique counts are refreshed this often
CONV_PRECISION = 3000                           # cardinality precision_threshold (exact below, ~1-2 % above)

SOURCES = {
    "session": {"pattern": "*-session_history-*", "ts": "requestDttm", "page": "pageType", "sid": "sessionId",
                "order": ["orderInfo.orderId", "orderId"], "test": "splitTestInfo.testName",
                "variant": "splitTestInfo.variantName", "id_pages": None, "ua": "request.userAgentInfo."},
    "cop": {"pattern": "*-cop-derrh-*", "ts": "triggeredDateTime", "page": "request.pageType", "sid": "sessionTrackId",
            "order": ["orderId", "request.orderId"], "test": None, "variant": None, "id_pages": None,
            "ua": "userAgentDetail."},
}
INDEX_RE = re.compile(r"^(?P<store>.+?)-(?P<kind>session_history|cop-derrh)-(?P<y>\d{4})[.-](?P<period>W?\d{2})"
                      r"(?:[.-]\d{2})?(?:[.-]\d{3,6})?$")
NUMBERS_RE = "[0-9]+"
UUID_RE = "[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"


def use_cluster(name: str) -> None:
    global CLUSTER, ENV_FILE, STATE_FILE, PREFIX
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,40}", name):
        raise SystemExit(f"--cluster: '{name}' is not a valid name (letters, digits, - and _)")
    CLUSTER = name
    ENV_FILE = Path(os.environ.get("AB_ENV_FILE", str(HERE / f".env.{name}")))
    STATE_FILE = Path(os.environ.get("AB_STATE", str(HERE / f".ab_state.{name}")))
    PREFIX = os.environ.get("AB_PREFIX", "es-ab-test/" if name == "rh1" else f"es-ab-test/{name}/")


def log(msg: str) -> None:
    print(f"{dt.datetime.now(dt.timezone.utc):%Y-%m-%dT%H:%M:%SZ} {msg}", flush=True)


def iso(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def minute_key(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%MZ")


def parse_minute(v: str) -> dt.datetime:
    return dt.datetime.strptime(v, "%Y-%m-%dT%H:%MZ").replace(tzinfo=dt.timezone.utc)


def slot(t: dt.datetime) -> dt.datetime:
    t = t.astimezone(dt.timezone.utc).replace(second=0, microsecond=0)
    return t - dt.timedelta(minutes=t.minute % 5)


def ms(t: dt.datetime) -> int:
    return int(t.timestamp() * 1000)


def from_ms(v: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(v / 1000, dt.timezone.utc)


def store_of(index: str) -> str:
    m = INDEX_RE.match(index)
    return m["store"] if m else index


# ------------------------------------------------------------------ session ID kinds
def classify(v) -> str:
    """The category of one session ID (the same rules as the query's filters)."""
    if v is None:
        return "missing"
    v = str(v)
    if v.startswith("M"):
        return "valid"
    if v.startswith("fenix-health-"):
        return "health"
    if v in KNOWN:
        return v
    if v == "":
        return "empty"
    if len(v) > 256:
        return "missing"                       # not in the keyword field: the query counts it as missing
    if re.fullmatch(NUMBERS_RE, v):
        return "numbers"
    if re.fullmatch(UUID_RE, v):
        return "uuid"
    return "other"


def category_filters(sid: str) -> dict:
    """filters aggregation: one bucket per category, exclusive; whatever is left is "other"."""
    f = {"valid": {"prefix": {sid: "M"}},
         "health": {"prefix": {sid: "fenix-health-"}}}
    for v in KNOWN:
        f[v] = {"term": {sid: v}}
    f["empty"] = {"term": {sid: ""}}
    f["missing"] = {"bool": {"must_not": {"exists": {"field": sid}}}}
    f["numbers"] = {"regexp": {sid: NUMBERS_RE}}
    f["uuid"] = {"regexp": {sid: UUID_RE}}
    return {"filters": {"filters": f, "other_bucket_key": "other"}}


# ------------------------------------------------------------------ the cluster
def read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        out[k] = v
    return out


def credentials(env_file: Path | None = None) -> dict:
    path = env_file or ENV_FILE
    vals: dict[str, str] = {}
    if path.exists():
        if os.name == "posix" and path.stat().st_mode & 0o077:
            log(f"warning: {path} can be read by other users; run: chmod 600 {path}")
        vals = read_env_file(path)
    vals.update({k: os.environ[k] for k in ("ES_URL", "ES_USERNAME", "ES_PASSWORD") if os.environ.get(k)})
    cred = {"url": vals.get("ES_URL", ""), "username": vals.get("ES_USERNAME", ""), "password": vals.get("ES_PASSWORD", "")}
    if not cred["url"]:
        raise RuntimeError(f"no ES_URL: put ES_URL, ES_USERNAME and ES_PASSWORD in {path}")
    return cred


def es_call(cred: dict, path: str, body: dict) -> dict:
    url = cred["url"].rstrip("/") + path
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Accept": "application/json", "Content-Type": "application/json"})
    if cred.get("username"):
        token = base64.b64encode(f"{cred['username']}:{cred['password']}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        detail = e.read(400).decode("utf-8", "replace")
        raise RuntimeError(f"{CLUSTER} answered HTTP {e.code}: {detail}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RuntimeError(f"cannot reach {CLUSTER} at {cred['url']}: {getattr(e, 'reason', e)}") from None


def search(cred: dict, pattern: str, body: dict, what: str) -> dict:
    res = es_call(cred, f"/{pattern}/_search?ignore_unavailable=true&allow_no_indices=true", body)
    failed = (res.get("_shards") or {}).get("failed")
    if failed:
        raise RuntimeError(f"{failed} shards failed for {what}: "
                           f"{json.dumps((res['_shards'].get('failures') or [])[:1])[:300]}")
    return res


def _range(src: dict, a: dt.datetime, b: dt.datetime) -> dict:
    return {"range": {src["ts"]: {"gte": ms(a), "lt": ms(b), "format": "epoch_millis"}}}


def count_query(src: dict, a: dt.datetime, b: dt.datetime) -> dict:
    pt_aggs = {"cat": category_filters(src["sid"] + ".keyword")}
    if src["variant"]:
        pt_aggs["test"] = {"terms": {"field": src["test"] + ".keyword", "size": 10},
                           "aggs": {"var": {"terms": {"field": src["variant"] + ".keyword", "size": 20}}}}
    return {"size": 0, "track_total_hits": False, "query": _range(src, a, b),
            "aggs": {"t": {"date_histogram": {"field": src["ts"], "fixed_interval": "5m", "min_doc_count": 1},
                           "aggs": {"idx": {"terms": {"field": "_index", "size": 2000},
                                            "aggs": {"pt": {"terms": {"field": src["page"] + ".keyword", "size": 20,
                                                                      "missing": "(none)"},
                                                            "aggs": pt_aggs}}}}}}}


def counts(cred: dict, name: str, a: dt.datetime, b: dt.datetime) -> dict[tuple, int]:
    """{(minute, source, store, PAGE, category, value): n} for [a, b); the "_all" total of every window
    in the range is there, 0 too."""
    src = SOURCES[name]
    res = search(cred, src["pattern"], count_query(src, a, b), f"{name} counts {iso(a)} to {iso(b)}")
    out: dict[tuple, int] = {}

    def add(k, n):
        if n:
            out[k] = out.get(k, 0) + n

    w = a
    while w < b:
        out[(minute_key(w), name, ALL, "", "total", "")] = 0
        w += dt.timedelta(seconds=STEP)
    for tb in res["aggregations"]["t"]["buckets"]:
        m = minute_key(from_ms(tb["key"]))
        if (m, name, ALL, "", "total", "") not in out:
            continue                                    # a bucket outside the range (cannot happen; to be safe)
        for ib in tb["idx"]["buckets"]:
            store = store_of(ib["key"])
            for pb in ib["pt"]["buckets"]:
                page = str(pb["key"]).upper()
                add((m, name, store, page, "total", ""), pb["doc_count"])
                out[(m, name, ALL, "", "total", "")] += pb["doc_count"]
                for cat, cb in pb["cat"]["buckets"].items():
                    add((m, name, store, page, cat, ""), cb["doc_count"])
                for tb2 in (pb.get("test") or {}).get("buckets", []):
                    for vb in tb2["var"]["buckets"]:
                        add((m, name, store, page, "variant", f"{tb2['key']} / {vb['key']}"), vb["doc_count"])
    return out


def _get(doc: dict, path: str):
    for p in path.split("."):
        if not isinstance(doc, dict):
            return None
        doc = doc.get(p)
    return doc


def bad_ids(cred: dict, name: str, a: dt.datetime, b: dt.datetime) -> tuple[list[dict], int]:
    """Entries with a bad session ID in [a, b) (health checks left out): (rows, how many were left out
    because there were more than MAX_IDS)."""
    src = SOURCES[name]
    flt = [_range(src, a, b)]
    if src["id_pages"]:
        flt.append({"terms": {src["page"] + ".keyword": src["id_pages"]}})
    sid = src["sid"] + ".keyword"
    body = {"size": MAX_IDS, "track_total_hits": True, "sort": [{src["ts"]: "asc"}],
            "_source": [src["sid"], src["page"], *src["order"],
                        *(src["ua"] + f for f in ("browserName", "osName", "deviceName", "deviceBrand", "webviewAppName")),
                        *(["responses.eddsHidden", "responseMetadata.atleastSingleEDDShown", CART_FIELD, SHIP_FIELD]
                          if name == "session" else [])],
            "query": {"bool": {"filter": flt, "must_not": [{"prefix": {sid: "M"}}, {"prefix": {sid: "fenix-health-"}}]}}}
    res = search(cred, src["pattern"], body, f"{name} bad IDs {iso(a)} to {iso(b)}")
    rows = []
    for h in res["hits"]["hits"]:
        s = h.get("_source") or {}
        at = from_ms(int(h["sort"][0])) if h.get("sort") else a
        v = _get(s, src["sid"])
        order = next((str(o) for o in (_get(s, f) for f in src["order"]) if o not in (None, "")), "")
        rows.append({"minute": minute_key(slot(at)), "source": name, "store": store_of(h["_index"]),
                     "page": str(_get(s, src["page"]) or "(none)").upper(), "category": classify(v),
                     "session_id": "" if v is None else str(v)[:120], "order_id": order, "doc_id": h.get("_id", ""),
                     "at": iso(at), **_ua(s, src["ua"]), **(_edd(s) if name == "session" else {})})
    total = (res["hits"].get("total") or {}).get("value", len(rows))
    return rows, max(0, total - len(rows))


def _ua(doc: dict, prefix: str) -> dict:
    """Browser, OS, device, brand and in-app (webview) app of one entry, as short strings."""
    def g(f):
        v = _get(doc, prefix + f)
        return "" if v in (None, "", "??") else str(v)[:60]
    return {"browser": g("browserName"), "os": g("osName"), "device": g("deviceName"), "brand": g("deviceBrand"),
            "app": g("webviewAppName")}


def _edd(doc: dict) -> dict:
    """EDD group (hidden / shown / none), cart total and shipping cost of one session_history entry."""
    resp = doc.get("responses")
    resp = resp if isinstance(resp, list) else [resp] if isinstance(resp, dict) else []
    if any(isinstance(r, dict) and r.get("eddsHidden") is True for r in resp):
        grp = "hidden"
    elif _get(doc, "responseMetadata.atleastSingleEDDShown") is True:
        grp = "shown"
    else:
        grp = "none"

    def money(f):
        v = _get(doc, f)
        return "" if v is None or isinstance(v, (dict, list)) else f"{float(v):.2f}"
    return {"edd": grp, "cart": money(CART_FIELD), "ship": money(SHIP_FIELD)}


def edd_filters() -> dict:
    """filters aggregation: hidden (the response hid the EDD), shown (an EDD was shown), none (the rest)."""
    return {"filters": {"filters": {
        "hidden": EDD_HIDDEN,
        "shown": {"bool": {"filter": [{"term": {"responseMetadata.atleastSingleEDDShown": True}}], "must_not": [EDD_HIDDEN]}}},
        "other_bucket_key": "none"}}


def oc_query(a: dt.datetime, b: dt.datetime) -> dict:
    src = SOURCES["session"]
    sid = src["sid"] + ".keyword"
    eg = edd_filters()
    eg["aggs"] = {"cat": category_filters(sid), "cart": {"sum": {"field": CART_FIELD}}, "ship": {"sum": {"field": SHIP_FIELD}}}
    return {"size": 0, "track_total_hits": False,
            "query": {"bool": {"filter": [_range(src, a, b), {"terms": {src["page"] + ".keyword": EDD_PAGES}}],
                               "must_not": [{"prefix": {sid: "fenix-health-"}}]}},
            "aggs": {"t": {"date_histogram": {"field": src["ts"], "fixed_interval": "5m", "min_doc_count": 1},
                           "aggs": {"idx": {"terms": {"field": "_index", "size": 2000}, "aggs": {"edd": eg}}}}}}


def oc_counts(cred: dict, a: dt.datetime, b: dt.datetime) -> dict[tuple, int]:
    """Order confirmations of session_history in [a, b) per 5-minute window, store and EDD group:
    {(minute, "session", store, "OC", category, group): n} with category edd (orders), edd_cart / edd_ship (cents)
    and edd:<session ID kind> (orders with that kind of session ID). Health checks left out; zeros not written."""
    res = search(cred, SOURCES["session"]["pattern"], oc_query(a, b), f"OC EDD {iso(a)} to {iso(b)}")
    out: dict[tuple, int] = {}

    def add(k, n):
        if n:
            out[k] = out.get(k, 0) + n

    for tb in res["aggregations"]["t"]["buckets"]:
        m = minute_key(from_ms(tb["key"]))
        for ib in tb["idx"]["buckets"]:
            store = store_of(ib["key"])
            for grp, eb in ib["edd"]["buckets"].items():
                add((m, "session", store, "OC", "edd", grp), eb["doc_count"])
                add((m, "session", store, "OC", "edd_cart", grp), round(((eb.get("cart") or {}).get("value") or 0) * 100))
                add((m, "session", store, "OC", "edd_ship", grp), round(((eb.get("ship") or {}).get("value") or 0) * 100))
                for cat, cb in eb["cat"]["buckets"].items():
                    add((m, "session", store, "OC", f"edd:{cat}", grp), cb["doc_count"])
    return out


def conv_query(a: dt.datetime, b: dt.datetime) -> dict:
    src = SOURCES["session"]
    sid = src["sid"] + ".keyword"
    eg = edd_filters()
    eg["aggs"] = {"u": {"cardinality": {"field": sid, "precision_threshold": CONV_PRECISION}}}
    return {"size": 0, "track_total_hits": False,
            "query": {"bool": {"filter": [_range(src, a, b), {"terms": {src["page"] + ".keyword": CONV_PAGES}},
                                          {"prefix": {sid: "M"}}]}},
            "aggs": {"idx": {"terms": {"field": "_index", "size": 2000},
                             "aggs": {"pt": {"terms": {"field": src["page"] + ".keyword", "size": 10}, "aggs": {"edd": eg}}}}}}


def conv_day(cred: dict, day: dt.date, until: dt.datetime) -> list[dict]:
    """Unique valid session IDs of one UTC day (up to `until` for today) per store, page and EDD group."""
    a = dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc)
    b = min(a + dt.timedelta(days=1), until)
    if b <= a:
        return []
    res = search(cred, SOURCES["session"]["pattern"], conv_query(a, b), f"unique sessions {day}")
    acc: dict[tuple, int] = {}
    for ib in res["aggregations"]["idx"]["buckets"]:
        store = store_of(ib["key"])
        for pb in ib["pt"]["buckets"]:
            for grp, eb in pb["edd"]["buckets"].items():
                n = int((eb.get("u") or {}).get("value") or 0)
                if n:
                    k = (store, str(pb["key"]).upper(), grp)
                    acc[k] = acc.get(k, 0) + n          # two indexes of one store in a day (month change): added
    return [{"minute": minute_key(a), "store": st, "page": pg, "group": g, "sessions": n} for (st, pg, g), n in sorted(acc.items())]


def conv_write(s3, day: dt.date, rows: list[dict]) -> None:
    write_rows(s3, block_key("conv", f"{day:%Y-%m-%d}T00:00Z"), CONV_COLUMNS, rows)


def ua_query(dim: str, a: dt.datetime, b: dt.datetime) -> dict:
    src = SOURCES["session"]
    field = src["ua"] + UA_DIMS[dim] + ".keyword"
    sid = src["sid"] + ".keyword"
    bad = {"bool": {"must_not": [{"prefix": {sid: "M"}}]}}
    return {"size": 0, "track_total_hits": False,
            "query": {"bool": {"filter": [_range(src, a, b)], "must_not": [{"prefix": {sid: "fenix-health-"}}]}},
            "aggs": {"idx": {"terms": {"field": "_index", "size": 2000},
                             "aggs": {"pt": {"terms": {"field": src["page"] + ".keyword", "size": 20, "missing": "(none)"},
                                             "aggs": {"bad": {"filter": bad},
                                                      "v": {"terms": {"field": field, "size": UA_TOP, "missing": UA_UNKNOWN},
                                                            "aggs": {"bad": {"filter": bad}}}}}}}}}


def ua_counts(cred: dict, a: dt.datetime, b: dt.datetime) -> list[dict]:
    """Shopper entries (health checks left out) and bad session IDs of session_history in [a, b) (inside one hour)
    per store, page, dimension and value: the top UA_TOP values, the rest as "_other". One query per dimension."""
    hour = minute_key(a.replace(minute=0))
    acc: dict[tuple, list[int]] = {}
    for dim in UA_DIMS:
        res = search(cred, SOURCES["session"]["pattern"], ua_query(dim, a, b), f"{dim} counts {iso(a)} to {iso(b)}")
        for ib in res["aggregations"]["idx"]["buckets"]:
            store = store_of(ib["key"])
            for pb in ib["pt"]["buckets"]:
                page = str(pb["key"]).upper()
                n_top = b_top = 0
                for vb in pb["v"]["buckets"]:
                    k = (store, page, dim, str(vb["key"])[:60] or UA_UNKNOWN)
                    e = acc.setdefault(k, [0, 0])
                    e[0] += vb["doc_count"]
                    e[1] += vb["bad"]["doc_count"]
                    n_top += vb["doc_count"]
                    b_top += vb["bad"]["doc_count"]
                if pb["doc_count"] > n_top:
                    e = acc.setdefault((store, page, dim, UA_OTHER), [0, 0])
                    e[0] += pb["doc_count"] - n_top
                    e[1] += max(0, pb["bad"]["doc_count"] - b_top)
    return [{"minute": hour, "store": st, "page": pg, "dim": d, "value": v, "entries": n, "bad": bd}
            for (st, pg, d, v), (n, bd) in acc.items() if n]


def ua_range(s3, cred: dict, a: dt.datetime, b: dt.datetime, dry_run: bool = False) -> int:
    """Hourly browser / device counts from the hour a falls in up to b (the last hour may be partial; it is counted
    again by the next run). Replaces those hours in S3. Returns the rows written."""
    a = a.replace(minute=0, second=0, microsecond=0)
    n = 0
    for lo, hi in pieces(a, b):
        rows: list[dict] = []
        h = lo
        while h < hi:
            rows += ua_counts(cred, h, min(h + dt.timedelta(hours=1), hi))
            h += dt.timedelta(hours=1)
        n += len(rows)
        if not dry_run:
            replace_rows(s3, "ua", UA_COLUMNS, minute_key(lo), minute_key(hi), rows)
    return n


# ------------------------------------------------------------------ S3 files
def block_key(kind: str, minute: str) -> str:
    day, hh = minute[:10], int(minute[11:13])
    return f"{PREFIX}{kind}/dt={day}/part-{hh - hh % BLOCK_HOURS:02d}.csv.gz"


def read_rows(s3, key: str) -> list[dict]:
    try:
        body = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
    except Exception as e:                                        # noqa: BLE001
        code = getattr(e, "response", {}).get("Error", {}).get("Code")
        if code in ("NoSuchKey", "404") or type(e).__name__ == "NoSuchKey":
            return []
        raise
    return list(csv.DictReader(io.StringIO(gzip.decompress(body).decode("utf-8"))))


def write_rows(s3, key: str, columns: list[str], rows: list[dict]) -> int:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    data = gzip.compress(buf.getvalue().encode("utf-8"))
    s3.put_object(Bucket=BUCKET, Key=key, Body=data, ContentType="application/gzip")
    return len(data)


def replace_rows(s3, kind: str, columns: list[str], lo: str, hi: str, rows: list[dict]) -> None:
    """Rows of the minutes [lo, hi) (one 4-hour file) replace the ones already there."""
    key = block_key(kind, lo)
    kept = [r for r in read_rows(s3, key) if not (lo <= r["minute"] < hi)]
    allr = kept + rows
    if kind == "counts":
        allr.sort(key=lambda r: (r["minute"], r["source"], r["store"] != ALL, r["store"], r["page"], r["category"], r["value"]))
    elif kind == "conv":
        allr.sort(key=lambda r: (r["minute"], r["store"], r["page"], r["group"]))
    elif kind == "ua":
        allr.sort(key=lambda r: (r["minute"], r["store"], r["page"], r["dim"], -int(r["entries"] or 0)))
    else:
        allr.sort(key=lambda r: (r["at"], r["source"], r["store"]))
    write_rows(s3, key, columns, allr)


def put_json(s3, key: str, data: dict) -> None:
    s3.put_object(Bucket=BUCKET, Key=PREFIX + key, Body=json.dumps(data, separators=(",", ":")).encode(),
                  ContentType="application/json", CacheControl="no-cache")


# ------------------------------------------------------------------ counting a time range
def pieces(a: dt.datetime, b: dt.datetime) -> list[tuple[dt.datetime, dt.datetime]]:
    """[a, b) cut at the 4-hour file boundaries."""
    out = []
    t = a
    while t < b:
        nxt = min(t.replace(hour=t.hour - t.hour % BLOCK_HOURS, minute=0) + dt.timedelta(hours=BLOCK_HOURS), b)
        out.append((t, nxt))
        t = nxt
    return out


def count_range(s3, cred: dict, a: dt.datetime, b: dt.datetime, dry_run: bool = False, verbose: bool = False) -> dict:
    """Counts [a, b) (whole 5-minute windows) and replaces those windows in S3. Returns totals."""
    tot = {"windows": 0, "entries": {n: 0 for n in SOURCES}, "bad": 0, "ids": 0, "idsLeftOut": 0, "rows": 0}
    for lo, hi in pieces(a, b):
        crow: dict[tuple, int] = {}
        irows: list[dict] = []
        t = lo
        while t < hi:
            u = min(t + CHUNK, hi)
            for name in SOURCES:
                crow.update(counts(cred, name, t, u))
                got, left = bad_ids(cred, name, t, u)
                irows += got
                tot["idsLeftOut"] += left
            crow.update(oc_counts(cred, t, u))
            t = u
        rows = [{"minute": k[0], "source": k[1], "store": k[2], "page": k[3], "category": k[4], "value": k[5], "count": n}
                for k, n in crow.items()]
        tot["windows"] += sum(1 for k in crow if k[2] == ALL) // len(SOURCES)
        for k, n in crow.items():
            if k[2] == ALL:
                tot["entries"][k[1]] += n
            elif k[4] not in ("total", "valid", "health", "variant") and not k[4].startswith("edd"):
                tot["bad"] += n
        tot["ids"] += len(irows)
        tot["rows"] += len(rows)
        if dry_run:
            if verbose:
                print_rows(rows)
            continue
        replace_rows(s3, "counts", COUNT_COLUMNS, minute_key(lo), minute_key(hi), rows)
        replace_rows(s3, "ids", ID_COLUMNS, minute_key(lo), minute_key(hi), irows)
        if verbose:
            log(f"wrote {minute_key(lo)} to {minute_key(hi)}: {len(rows)} count rows, {len(irows)} bad-ID rows")
    return tot


def print_rows(rows: list[dict]) -> None:
    agg: dict[tuple, dict] = {}
    for r in rows:
        if r["store"] == ALL or r["category"] == "variant" or r["category"].startswith("edd"):
            continue
        d = agg.setdefault((r["source"], r["store"], r["page"]), {})
        d[r["category"]] = d.get(r["category"], 0) + r["count"]
    print(f"{'source':8} {'store':42} {'page':5} {'total':>8} {'valid':>8}  bad / health")
    for (s, st, p), d in sorted(agg.items(), key=lambda x: -x[1].get("total", 0)):
        rest = ", ".join(f"{k}={v}" for k, v in sorted(d.items(), key=lambda x: -x[1]) if k not in ("total", "valid"))
        print(f"{s:8} {st[:42]:42} {p[:5]:5} {d.get('total', 0):>8} {d.get('valid', 0):>8}  {rest}")


def read_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def write_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state))
    except OSError as e:
        log(f"warning: cannot write {STATE_FILE}: {e}")


def run(s3, cred_fn, now: dt.datetime | None = None, state: dict | None = None) -> tuple[dict, dict]:
    """One 5-minute run: the last 15 minutes (more after missed runs, up to 12 hours). Never raises:
    a failure goes to status.json and the next run counts the same windows again."""
    now = now or dt.datetime.now(dt.timezone.utc)
    state = dict(state or {})
    t0 = time.monotonic()
    end = slot(now)
    start = end - LOOKBACK
    if state.get("doneTo"):
        done = parse_minute(state["doneTo"])
        if done < start:
            start = max(done, end - CATCH_UP)
    status = {"version": VERSION, "cluster": CLUSTER, "lastRun": iso(now), "lastOk": state.get("lastOk"),
              "lastError": None, "lastErrorAt": state.get("lastErrorAt"), "first": state.get("first"),
              "doneTo": state.get("doneTo"), "knownValues": KNOWN}
    try:
        cred = cred_fn()
        tot = count_range(s3, cred, start, end)
        tot["uaRows"] = ua_range(s3, cred, start, end)
        today = end.date()
        new_day = state.get("convDay") != str(today)
        if new_day or not state.get("convAt") or now - dt.datetime.strptime(state["convAt"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=dt.timezone.utc) >= CONV_EVERY:
            days = ([today - dt.timedelta(days=1)] if new_day else []) + [today]   # after midnight: yesterday's final count
            tot["convRows"] = 0
            for d in days:
                rows = conv_day(cred, d, end)
                conv_write(s3, d, rows)
                tot["convRows"] += len(rows)
            state.update(convAt=iso(now), convDay=str(today))
    except Exception as e:                                       # noqa: BLE001
        status.update(ok=False, lastError=str(e)[:500], lastErrorAt=iso(now), tookMs=int((time.monotonic() - t0) * 1000))
        log(f"failed: {status['lastError']}")
        try:
            put_json(s3, "status.json", status)
        except Exception as e2:                                  # noqa: BLE001
            log(f"cannot write status.json: {e2}")
        return status, state
    took = int((time.monotonic() - t0) * 1000)
    state.setdefault("first", minute_key(start))
    state.update(doneTo=minute_key(end), lastOk=iso(now))
    status.update(ok=True, lastOk=iso(now), first=state["first"], doneTo=state["doneTo"], tookMs=took,
                  counted={"from": minute_key(start), "to": minute_key(end), **tot})
    put_json(s3, "status.json", status)
    log(f"ok: {minute_key(start)} to {minute_key(end)}: session {tot['entries']['session']:,}, cop {tot['entries']['cop']:,} "
        f"entries, {tot['bad']:,} bad session IDs, {tot['ids']:,} listed"
        + (f" ({tot['idsLeftOut']:,} left out)" if tot["idsLeftOut"] else "")
        + f", {tot['uaRows']:,} browser / device rows"
        + (f", {tot['convRows']:,} unique-session rows" if "convRows" in tot else "") + f" ({took} ms)")
    return status, state


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--dry-run", action="store_true", help="count the last 15 minutes and print; write nothing")
    p.add_argument("--backfill-days", type=int, default=0, help="one-off: count from midnight UTC this many days ago up to now")
    p.add_argument("--cluster", help="another cluster: login .env.NAME, state .ab_state.NAME, S3 es-ab-test/NAME/")
    a = p.parse_args()
    if a.cluster:
        use_cluster(a.cluster)
    now = dt.datetime.now(dt.timezone.utc)

    if a.dry_run:
        try:
            end = slot(now)
            cred = credentials()
            tot = count_range(None, cred, end - LOOKBACK, end, dry_run=True, verbose=True)
            ua = [r for h in (end - LOOKBACK,) for r in ua_counts(cred, h, end)]
            by: dict[tuple, list[int]] = {}
            for r in ua:
                e = by.setdefault((r["dim"], r["value"]), [0, 0])
                e[0] += r["entries"]
                e[1] += r["bad"]
            for dim in UA_DIMS:
                top = sorted(((v, n) for (d, v), n in by.items() if d == dim), key=lambda x: -x[1][0])[:6]
                print(f"{dim:8} " + ", ".join(f"{v} {n:,} ({bd:,} bad)" for v, (n, bd) in top))
            oc = oc_counts(cred, end - LOOKBACK, end)
            g: dict[str, list[int]] = {}
            for (_, _, _, _, cat, grp), n in oc.items():
                e = g.setdefault(grp, [0, 0, 0, 0])
                if cat == "edd":
                    e[0] += n
                elif cat == "edd_cart":
                    e[1] += n
                elif cat == "edd_ship":
                    e[2] += n
                elif cat.startswith("edd:") and cat not in ("edd:valid", "edd:health"):
                    e[3] += n
            print("OC EDD   " + ", ".join(f"{k} {o:,} orders (cart ${c / 100:,.0f}, ship ${sh / 100:,.0f}, {b:,} bad)"
                                          for k, (o, c, sh, b) in sorted(g.items())))
            cv: dict[tuple, int] = {}
            for r in conv_day(cred, end.date(), end):
                cv[(r["page"], r["group"])] = cv.get((r["page"], r["group"]), 0) + r["sessions"]
            print("sessions " + ", ".join(f"{p}/{gr} {n:,}" for (p, gr), n in sorted(cv.items())) + "  (unique, today UTC so far)")
        except Exception as e:                                   # noqa: BLE001
            log(f"failed: {e}")
            return 1
        log(f"dry run: {tot['windows']} windows, {tot['entries']}, {tot['bad']:,} bad session IDs, {tot['ids']:,} would be listed")
        return 0

    import boto3
    s3 = boto3.client("s3", region_name=REGION)
    if a.backfill_days:
        if not 1 <= a.backfill_days <= 31:
            p.error("--backfill-days must be 1 to 31")
        start = (now - dt.timedelta(days=a.backfill_days)).replace(hour=0, minute=0, second=0, microsecond=0)
        try:
            cred = credentials()
            tot = count_range(s3, cred, start, slot(now), verbose=True)
            log("browser / device counts per hour …")
            tot["uaRows"] = ua_range(s3, cred, start, slot(now))
            log("unique sessions per day (conversion) …")
            tot["convRows"] = 0
            d = start.date()
            while d <= now.date():
                rows = conv_day(cred, d, slot(now))
                conv_write(s3, d, rows)
                tot["convRows"] += len(rows)
                d += dt.timedelta(days=1)
        except Exception as e:                                   # noqa: BLE001
            log(f"backfill failed: {e}")
            return 1
        state = read_state()
        if not state.get("first") or state["first"] > minute_key(start):
            state["first"] = minute_key(start)
            write_state(state)
        log(f"backfill done: {tot['windows']} windows, {tot['entries']}, {tot['bad']:,} bad session IDs, "
            f"{tot['ids']:,} listed" + (f", {tot['idsLeftOut']:,} left out" if tot["idsLeftOut"] else "")
            + f", {tot['uaRows']:,} browser / device rows, {tot['convRows']:,} unique-session rows")
        return 0

    status, state = run(s3, credentials, now, read_state())
    write_state(state)
    return 0 if status.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
