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
    s3://<bucket>/es-ab-test/status.json                           every run: last run, last success, last error

  page      upper case (cop and COP are the same page)
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
ID_COLUMNS = ["minute", "source", "store", "page", "category", "session_id", "order_id", "doc_id", "at"]

SOURCES = {
    "session": {"pattern": "*-session_history-*", "ts": "requestDttm", "page": "pageType", "sid": "sessionId",
                "order": ["orderInfo.orderId", "orderId"], "test": "splitTestInfo.testName",
                "variant": "splitTestInfo.variantName", "id_pages": None},
    "cop": {"pattern": "*-cop-derrh-*", "ts": "triggeredDateTime", "page": "request.pageType", "sid": "sessionTrackId",
            "order": ["orderId", "request.orderId"], "test": None, "variant": None, "id_pages": None},
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
            "_source": [src["sid"], src["page"], *src["order"]],
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
                     "at": iso(at)})
    total = (res["hits"].get("total") or {}).get("value", len(rows))
    return rows, max(0, total - len(rows))


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
            t = u
        rows = [{"minute": k[0], "source": k[1], "store": k[2], "page": k[3], "category": k[4], "value": k[5], "count": n}
                for k, n in crow.items()]
        tot["windows"] += sum(1 for k in crow if k[2] == ALL) // len(SOURCES)
        for k, n in crow.items():
            if k[2] == ALL:
                tot["entries"][k[1]] += n
            elif k[4] not in ("total", "valid", "health", "variant"):
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
        if r["store"] == ALL or r["category"] == "variant":
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
        tot = count_range(s3, cred_fn(), start, end)
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
        + (f" ({tot['idsLeftOut']:,} left out)" if tot["idsLeftOut"] else "") + f" ({took} ms)")
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
            tot = count_range(None, credentials(), end - LOOKBACK, end, dry_run=True, verbose=True)
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
            tot = count_range(s3, credentials(), start, slot(now), verbose=True)
        except Exception as e:                                   # noqa: BLE001
            log(f"backfill failed: {e}")
            return 1
        state = read_state()
        if not state.get("first") or state["first"] > minute_key(start):
            state["first"] = minute_key(start)
            write_state(state)
        log(f"backfill done: {tot['windows']} windows, {tot['entries']}, {tot['bad']:,} bad session IDs, "
            f"{tot['ids']:,} listed" + (f", {tot['idsLeftOut']:,} left out" if tot["idsLeftOut"] else ""))
        return 0

    status, state = run(s3, credentials, now, read_state())
    write_state(state)
    return 0 if status.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
