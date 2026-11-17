#!/usr/bin/env python3
"""Inbound orders per tenant from one Elasticsearch cluster (elkop2), every 5 minutes, to S3.

Each run makes ONE metadata call (GET _cat/indices/*-order-*: no search, no load on the data),
adds up every tenant's order documents, and compares them with the previous run: the difference
is the orders that arrived in between. Written to S3 in the same layout as apigw-metrics/, so the
Ops dashboard reads both the same way:

    s3://<bucket>/<prefix>inbound/dt=YYYY-MM-DD/part-HH.csv.gz   HH = 00, 04, 08, 12, 16, 20 (UTC)
        minute,tenant,orders,docs,span_min,src
        2026-11-17T10:00Z,_all,1452,11787948,5,live
        2026-11-17T10:00Z,vuori-clothing.myshopify.com,87,324587,5,live
    s3://<bucket>/<prefix>status.json    every run: last run, last success, last error
    s3://<bucket>/<prefix>latest.json    when a count changed: every counted index, grouped per tenant and month

  minute    start of the 5-minute window (UTC) the previous run fell in: the 10:05 run compares with the
            10:00 run, so its orders go to 10:00
  orders    orders that arrived in the window; a tenant only gets a row when it is above 0
  _all      every tenant together; written for EVERY window, 0 too, so a window without it = not counted
  docs      the tenant's total at the end of the window (empty for backfilled rows)
  span_min  minutes the difference covers: 5, more after missed runs (all of it lands in one row)
  src       live = from the 5-minute counts; backfill = from the order date field (--backfill-days)

Rules: a new index counts as new orders only when it was created after the previous run (creation.date);
an older index that appears (reopened, restored) is a new starting point. A count that goes down
(documents or an index deleted) counts as 0. The first run only records the starting counts.

Index names (tenant = everything before "-order-"):
    vuori-clothing.myshopify.com-order-2026.11.001   tenant + year + month + rollover part
    buck-mason-usa.myshopify.com-order-2026.10       tenant + year + month
    bulbheadshop1.myshopify.com-order-2026           tenant + year
    dermalogica-us.myshopify.com-s-order-2026.08     skipped (-s-)
    burnd.com-jg-order-2026                          skipped (-jg-)

Login: a .env file next to this script (or the file ES_ORDERS_ENV_FILE names), readable only by its owner:
    ES_URL=http://node6.elkop2.prod.int.fenixcommerce.com:9200
    ES_USERNAME=...
    ES_PASSWORD=...
Variables already set in the environment win over the file. The password is never printed or written anywhere.

Needs: Python 3.9+, boto3. AWS access: s3:GetObject / s3:PutObject on <prefix>* (s3:ListBucket on the
prefix too, so a missing file reads as "not there yet"). Elasticsearch: "monitor" for the 5-minute runs,
read on the order indexes for --backfill-days.

Examples
    python3 es_order_counts.py                                          # normal 5-minute run (cron)
    python3 es_order_counts.py --dry-run                                # read the cluster, print the month rows, write nothing
    python3 es_order_counts.py --compare today                          # live windows vs order dates, per tenant
    python3 es_order_counts.py --backfill-days 8 --dry-run              # one-off fill of the last 8 days: print only
    python3 es_order_counts.py --backfill-days 8                        # ... and write it
Order dates: the first of order_summary.fenix_crtd_dttm, order_summary.crtd_dttm.actual_dttm, created_at
that an order has (--date-field to change).
"""
from __future__ import annotations

import argparse
import base64
import csv
import datetime as dt
import gzip
import hashlib
import io
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BUCKET = os.environ.get("ES_ORDERS_BUCKET", "fenix-vector-ecs-logs")
PREFIX = os.environ.get("ES_ORDERS_PREFIX", "es-order-counts/")
REGION = os.environ.get("AWS_REGION", "us-west-2")
ENV_FILE = Path(os.environ.get("ES_ORDERS_ENV_FILE", str(Path(__file__).resolve().parent / ".env")))
CLUSTER = os.environ.get("ES_ORDERS_CLUSTER", "elkop2")
PATTERN = os.environ.get("ES_ORDERS_PATTERN", "*-order-*")
TIMEOUT = float(os.environ.get("ES_ORDERS_TIMEOUT", "30"))
STATE_FILE = Path(os.environ.get("ES_ORDERS_STATE", str(Path(__file__).resolve().parent / ".last_counts")))
VERSION = 2
STEP = 300                                   # seconds per window
BLOCK_HOURS = 4                              # 24 / 4 = 6 files per day
ALL = "_all"
COLUMNS = ["minute", "tenant", "orders", "docs", "span_min", "src"]

INDEX_RE = re.compile(
    r"^(?P<tenant>.+?)(?:-(?P<variant>s|jg))?-order-(?P<year>\d{4})"
    r"(?:\.(?P<month>0[1-9]|1[0-2]))?(?:\.(?P<part>\d{3}))?$")
SKIP_VARIANTS = {"s": "-s-", "jg": "-jg-"}


def log(msg: str) -> None:
    print(f"{dt.datetime.now(dt.timezone.utc):%Y-%m-%dT%H:%M:%SZ} {msg}", flush=True)


def iso(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(v: str) -> dt.datetime:
    return dt.datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)


def slot(t: dt.datetime) -> dt.datetime:
    """Start of the 5-minute window t falls in (UTC)."""
    t = t.astimezone(dt.timezone.utc).replace(second=0, microsecond=0)
    return t - dt.timedelta(minutes=t.minute % 5)


def minute_key(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%MZ")


# ------------------------------------------------------------------ reading the names
def parse_index(name: str) -> dict | None:
    """The parts of an order index name, or None when it is not one."""
    m = INDEX_RE.match(name)
    if not m:
        return None
    return {"tenant": m["tenant"], "variant": m["variant"], "year": int(m["year"]),
            "month": int(m["month"]) if m["month"] else None, "part": m["part"]}


def _count(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def group(indices: list[dict]) -> dict:
    """_cat/indices rows ({index, health, status, docs.count}) -> rows per tenant and month.

    Returns {"rows", "skipped", "unmatched", "closed", "summary"}; every list is sorted so the same
    cluster state always gives the same JSON."""
    rows: dict[tuple, dict] = {}
    skipped, unmatched, closed = [], [], []
    for ix in indices:
        name = ix.get("index") or ""
        if name.startswith("."):
            continue                                   # system / hidden indexes
        p = parse_index(name)
        count = _count(ix.get("docs.count"))
        if p is None:
            unmatched.append({"index": name, "count": count})
            continue
        if p["variant"] in SKIP_VARIANTS:
            skipped.append({"index": name, "why": SKIP_VARIANTS[p["variant"]], "count": count})
            continue
        if ix.get("status") == "close" or count is None:
            closed.append({"index": name, "tenant": p["tenant"], "year": p["year"], "month": p["month"]})
            continue
        key = (p["tenant"], p["year"], p["month"])
        row = rows.setdefault(key, {"tenant": p["tenant"], "year": p["year"], "month": p["month"],
                                    "total": 0, "parts": []})
        row["total"] += count
        row["parts"].append({"index": name, "part": p["part"], "count": count, "health": ix.get("health")})
    out = sorted(rows.values(), key=lambda r: (r["tenant"], r["year"], r["month"] or 0))
    for r in out:
        r["parts"].sort(key=lambda x: x["index"])
    return {
        "rows": out,
        "skipped": sorted(skipped, key=lambda x: x["index"]),
        "unmatched": sorted(unmatched, key=lambda x: x["index"]),
        "closed": sorted(closed, key=lambda x: x["index"]),
        "summary": {"tenants": len({r["tenant"] for r in out}), "rows": len(out),
                    "indexes": sum(len(r["parts"]) for r in out), "orders": sum(r["total"] for r in out),
                    "skipped": len(skipped), "unmatched": len(unmatched), "closed": len(closed)},
    }


# ------------------------------------------------------------------ the cluster
def read_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines; blank lines and # comments skipped; optional quotes around the value."""
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
    """{url, username, password} from the .env file; ES_URL / ES_USERNAME / ES_PASSWORD in the environment win."""
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
    if cred["username"] and not cred["password"]:
        raise RuntimeError(f"ES_USERNAME is set but ES_PASSWORD is empty in {path}")
    return cred


def es_call(cred: dict, path: str, body: dict | None = None, timeout: float = TIMEOUT, insecure: bool = False):
    """One request to the cluster (GET, or POST with a JSON body); the parsed JSON answer."""
    url = cred["url"].rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET",
                                 headers={"Accept": "application/json", "Content-Type": "application/json"})
    if cred.get("username"):
        token = base64.b64encode(f"{cred['username']}:{cred['password']}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    ctx = None
    if url.startswith("https") and insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        detail = e.read(300).decode("utf-8", "replace")
        raise RuntimeError(f"{CLUSTER} answered HTTP {e.code}: {detail}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RuntimeError(f"cannot reach {CLUSTER} at {cred['url']}: {getattr(e, 'reason', e)}") from None


def cat_indices(cred: dict, pattern: str = PATTERN, timeout: float = TIMEOUT, insecure: bool = False) -> list[dict]:
    """GET _cat/indices/<pattern> as JSON (open and closed indexes)."""
    body = es_call(cred, f"/_cat/indices/{pattern}?format=json&h=index,health,status,docs.count,creation.date"
                         "&expand_wildcards=open,closed", timeout=timeout, insecure=insecure)
    if not isinstance(body, list):
        raise RuntimeError(f"unexpected answer from {CLUSTER}: {str(body)[:300]}")
    return body


# ------------------------------------------------------------------ inbound orders
def counted(indices: list[dict]) -> dict[str, dict]:
    """index -> {tenant, count, created} for the indexes that count (open, matched, not -s- / -jg-)."""
    out = {}
    for ix in indices:
        name = ix.get("index") or ""
        p = parse_index(name) if not name.startswith(".") else None
        count = _count(ix.get("docs.count"))
        if p is None or p["variant"] in SKIP_VARIANTS or ix.get("status") == "close" or count is None:
            continue
        out[name] = {"tenant": p["tenant"], "count": count, "created": _count(ix.get("creation.date"))}
    return out


def inbound(now_idx: dict[str, dict], prev_idx: dict[str, int], prev_at: dt.datetime, now: dt.datetime) -> dict:
    """Orders per tenant between the previous run and this one, plus every tenant's total now."""
    orders: dict[str, int] = {}
    docs: dict[str, int] = {}
    since_ms = int(prev_at.timestamp() * 1000) - 60_000          # a minute of slack for clock drift
    for name, ix in now_idx.items():
        t = ix["tenant"]
        docs[t] = docs.get(t, 0) + ix["count"]
        if name in prev_idx:
            new = max(0, ix["count"] - prev_idx[name])            # fewer documents: deleted, not negative orders
        elif ix["created"] is not None and ix["created"] >= since_ms:
            new = ix["count"]                                      # created since the last run: all of it is new
        else:
            new = 0                                                # older index seen for the first time: a start point
        if new:
            orders[t] = orders.get(t, 0) + new
    return {"orders": orders, "docs": docs}


def block_key(minute: str) -> str:
    """'2026-11-17T13:05Z' -> '<prefix>inbound/dt=2026-11-17/part-12.csv.gz'"""
    day, hh = minute[:10], int(minute[11:13])
    return f"{PREFIX}inbound/dt={day}/part-{hh - hh % BLOCK_HOURS:02d}.csv.gz"


def read_block(s3, key: str) -> dict[tuple[str, str], dict]:
    try:
        body = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
    except Exception as e:                                        # noqa: BLE001
        code = getattr(e, "response", {}).get("Error", {}).get("Code")
        if code in ("NoSuchKey", "404") or type(e).__name__ == "NoSuchKey":
            return {}
        raise
    text = gzip.decompress(body).decode("utf-8")
    return {(r["minute"], r["tenant"]): r for r in csv.DictReader(io.StringIO(text))}


def write_block(s3, key: str, rows: dict[tuple[str, str], dict]) -> int:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for k in sorted(rows, key=lambda k: (k[0], k[1] != ALL, k[1])):
        w.writerow(rows[k])
    data = gzip.compress(buf.getvalue().encode("utf-8"))
    s3.put_object(Bucket=BUCKET, Key=key, Body=data, ContentType="application/gzip")
    return len(data)


def save_window(s3, minute: str, result: dict, span: int) -> int:
    """Adds one window's rows to its 4-hour file (two runs in the same window add up)."""
    key = block_key(minute)
    rows = read_block(s3, key)
    fresh = {t: n for t, n in result["orders"].items() if n > 0}
    fresh[ALL] = sum(fresh.values())
    for t, n in fresh.items():
        docs = sum(result["docs"].values()) if t == ALL else result["docs"].get(t, "")
        old = rows.get((minute, t))
        if old:
            n += int(old.get("orders") or 0)
            span_t = span + int(old.get("span_min") or 0)
        else:
            span_t = span
        rows[(minute, t)] = {"minute": minute, "tenant": t, "orders": n, "docs": docs, "span_min": span_t, "src": "live"}
    write_block(s3, key, rows)
    return fresh[ALL]


# ------------------------------------------------------------------ S3
def put_json(s3, key: str, data: dict) -> None:
    s3.put_object(Bucket=BUCKET, Key=PREFIX + key, Body=json.dumps(data, separators=(",", ":")).encode(),
                  ContentType="application/json", CacheControl="no-cache")


def fingerprint(doc: dict) -> str:
    """Changes only when a count or an index changes (not with the time of the run)."""
    keep = {k: doc[k] for k in ("rows", "skipped", "unmatched", "closed")}
    return hashlib.sha256(json.dumps(keep, sort_keys=True).encode()).hexdigest()


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


def run(s3, fetch, now: dt.datetime | None = None, force: bool = False, state: dict | None = None) -> tuple[dict, dict]:
    """One 5-minute run. `fetch()` returns the _cat rows. Returns (status, new state) and raises nothing:
    a failure is recorded in status.json; the state only moves on when the window was saved, so the next
    run counts the same orders again instead of losing them."""
    now = now or dt.datetime.now(dt.timezone.utc)
    state = dict(state or {})
    t0 = time.monotonic()
    status = {"version": VERSION, "cluster": CLUSTER, "lastRun": iso(now), "lastOk": state.get("lastOk"),
              "lastChange": state.get("lastChange"), "lastError": None, "lastErrorAt": state.get("lastErrorAt"),
              "inboundStart": state.get("inboundStart"), "inboundTo": state.get("inboundTo")}
    try:
        raw = fetch()
        grouped = group(raw)
        now_idx = counted(raw)
        took = int((time.monotonic() - t0) * 1000)
        window = None
        if state.get("idx") is not None and state.get("at"):
            prev_at = parse_iso(state["at"])
            if prev_at < now:
                minute = minute_key(slot(prev_at))
                span = max(1, round((now - prev_at).total_seconds() / 60))
                result = inbound(now_idx, state["idx"], prev_at, now)
                total = save_window(s3, minute, result, span)
                window = (minute, total, span)
                state.setdefault("inboundStart", minute)
                state["inboundTo"] = iso(now)
        doc = {"version": VERSION, "cluster": CLUSTER, "countedAt": iso(now), "tookMs": took, **grouped}
        fp = fingerprint(doc)
        changed = force or fp != state.get("fingerprint")
        if changed:
            put_json(s3, "latest.json", doc)
            state.update(fingerprint=fp, lastChange=iso(now))
    except Exception as e:                              # noqa: BLE001 - every failure goes to status.json
        status.update(ok=False, lastError=str(e)[:500], lastErrorAt=iso(now), tookMs=int((time.monotonic() - t0) * 1000))
        log(f"failed: {status['lastError']}")
        try:
            put_json(s3, "status.json", status)
        except Exception as e2:                          # noqa: BLE001
            log(f"cannot write status.json: {e2}")
        return status, state
    state.update(idx={k: v["count"] for k, v in now_idx.items()}, at=iso(now), lastOk=iso(now))
    s = grouped["summary"]
    status.update(ok=True, lastOk=iso(now), lastChange=state.get("lastChange"), tookMs=took, summary=s,
                  inboundStart=state.get("inboundStart"), inboundTo=state.get("inboundTo"),
                  window={"minute": window[0], "orders": window[1], "spanMin": window[2]} if window else None)
    put_json(s3, "status.json", status)
    w = f"{window[1]:,} orders in {window[0]} ({window[2]} min)" if window else "first run: starting counts saved"
    log(f"ok: {w}; {s['tenants']} tenants, {s['indexes']} indexes, {s['orders']:,} documents, "
        f"{s['skipped']} skipped, {s['unmatched']} unmatched, {s['closed']} closed ({took} ms)")
    return status, state


# ------------------------------------------------------------------ backfill from the order date field
DATE_FIELDS = "order_summary.fenix_crtd_dttm,order_summary.crtd_dttm.actual_dttm,created_at"


def _fields(field) -> list[str]:
    return [f.strip() for f in (field.split(",") if isinstance(field, str) else field) if f.strip()]


def _date_query(fields: list[str], i: int, start: dt.datetime, end: dt.datetime) -> dict:
    """Orders whose i-th date field is in [start, end) and that have none of the fields before it."""
    rng = {"range": {fields[i]: {"gte": int(start.timestamp() * 1000), "lt": int(end.timestamp() * 1000),
                                 "format": "epoch_millis"}}}
    if not i:
        return rng
    return {"bool": {"must": [rng], "must_not": [{"exists": {"field": f}} for f in fields[:i]]}}


def _search(cred: dict, body: dict, what: str, insecure: bool = False) -> dict:
    res = es_call(cred, f"/{PATTERN}/_search?ignore_unavailable=true&allow_no_indices=true", body, timeout=120,
                  insecure=insecure)
    failed = (res.get("_shards") or {}).get("failed")
    if failed:
        raise RuntimeError(f"{failed} shards failed for {what}: "
                           f"{json.dumps((res['_shards'].get('failures') or [])[:1])[:300]}")
    return res


def histogram(cred: dict, field, start: dt.datetime, end: dt.datetime, insecure: bool = False) -> dict:
    """Orders per tenant per 5 minutes in [start, end) by the order date: {(minute, tenant): n}.

    `field` = one field or several (list or "a,b,c"): an order is placed by the FIRST of them it has,
    so an order is never counted twice."""
    fields = _fields(field)
    out: dict[tuple[str, str], int] = {}
    for i, f in enumerate(fields):
        body = {"size": 0, "track_total_hits": False, "query": _date_query(fields, i, start, end),
                "aggs": {"idx": {"terms": {"field": "_index", "size": 5000},
                                 "aggs": {"t": {"date_histogram": {"field": f, "fixed_interval": "5m", "min_doc_count": 1}}}}}}
        res = _search(cred, body, f"{f} between {iso(start)} and {iso(end)}", insecure)
        for b in res["aggregations"]["idx"]["buckets"]:
            p = parse_index(b["key"])
            if p is None or p["variant"] in SKIP_VARIANTS:
                continue
            for h in b["t"]["buckets"]:
                m = minute_key(dt.datetime.fromtimestamp(h["key"] / 1000, dt.timezone.utc))
                out[(m, p["tenant"])] = out.get((m, p["tenant"]), 0) + h["doc_count"]
    return out


def compare(s3, cred: dict, field, day: str, insecure: bool = False) -> list[tuple[str, int, int]]:
    """The live windows of one UTC day against the order dates over the same time, per tenant:
    [(tenant, live, by date)], biggest first; ("_all", …) first. Shows whether the date fields fit."""
    live: dict[str, int] = {}
    minutes = []
    for hh in range(0, 24, BLOCK_HOURS):
        for (m, t), r in read_block(s3, f"{PREFIX}inbound/dt={day}/part-{hh:02d}.csv.gz").items():
            if r.get("src") != "live":
                continue
            if t == ALL:
                minutes.append(m)
            else:
                live[t] = live.get(t, 0) + int(r["orders"] or 0)
    if not minutes:
        raise RuntimeError(f"no live windows on {day} yet")
    p_ = lambda m: dt.datetime.strptime(m, "%Y-%m-%dT%H:%MZ").replace(tzinfo=dt.timezone.utc)  # noqa: E731
    start, end = p_(min(minutes)), p_(max(minutes)) + dt.timedelta(seconds=STEP)
    by_date: dict[str, int] = {}
    for (_, t), n in histogram(cred, field, start, end, insecure).items():
        by_date[t] = by_date.get(t, 0) + n
    tenants = sorted(set(live) | set(by_date), key=lambda t: (-live.get(t, 0), -by_date.get(t, 0), t))
    rows = [(t, live.get(t, 0), by_date.get(t, 0)) for t in tenants]
    log(f"{day}: {len(minutes)} live windows, {minute_key(start)} to {minute_key(end)}")
    return [(ALL, sum(live.values()), sum(by_date.values()))] + rows


def backfill(s3, cred: dict, field, days: int, now: dt.datetime, until: dt.datetime | None,
             dry_run: bool = False, insecure: bool = False) -> int:
    """Fills the inbound files from midnight UTC `days` days ago up to `until` (the first live window, else
    now) by the order date field, one hour per query. Live rows (from `until` on) are never touched."""
    start = (now - dt.timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = slot(until or now)
    total = 0
    t = start
    while t < end:
        block_end = min(t.replace(hour=t.hour - t.hour % BLOCK_HOURS) + dt.timedelta(hours=BLOCK_HOURS), end)
        counts: dict[tuple[str, str], int] = {}
        h = t
        while h < block_end:
            counts.update(histogram(cred, field, h, min(h + dt.timedelta(hours=1), block_end), insecure))
            h += dt.timedelta(hours=1)
        rows = {}
        w = t
        while w < block_end:                                        # an _all row for every window, 0 too
            m = minute_key(w)
            rows[(m, ALL)] = {"minute": m, "tenant": ALL, "orders": 0, "docs": "", "span_min": 5, "src": "backfill"}
            w += dt.timedelta(seconds=STEP)
        for (m, tenant), n in counts.items():
            rows[(m, tenant)] = {"minute": m, "tenant": tenant, "orders": n, "docs": "", "span_min": 5, "src": "backfill"}
            rows[(m, ALL)]["orders"] += n
        n_block = sum(r["orders"] for (m, tn), r in rows.items() if tn == ALL)
        total += n_block
        key = block_key(minute_key(t))
        if dry_run:
            log(f"dry run: {key}: {n_block:,} orders, {len(rows)} rows from {minute_key(t)} to {minute_key(block_end)}")
        else:
            lo, hi = minute_key(t), minute_key(block_end)
            kept = {k: r for k, r in read_block(s3, key).items() if not (lo <= k[0] < hi)}
            write_block(s3, key, {**kept, **rows})
            log(f"wrote s3://{BUCKET}/{key}: {n_block:,} orders from {lo} to {hi} ({len(kept)} live rows kept)")
        t = block_end
    return total


def print_rows(grouped: dict) -> None:
    for r in grouped["rows"]:
        when = f"{r['year']}-{r['month']:02d}" if r["month"] else f"{r['year']} (year)"
        print(f"{r['tenant']:<48} {when:<12} {len(r['parts']):>3} parts {r['total']:>12,}")
    for k in ("skipped", "unmatched", "closed"):
        for x in grouped[k]:
            print(f"  {k}: {x['index']}" + (f" ({x['why']})" if k == "skipped" else ""))
    s = grouped["summary"]
    print(f"{s['tenants']} tenants, {s['rows']} rows, {s['indexes']} indexes, {s['orders']:,} orders")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--dry-run", action="store_true", help="read the cluster and print; write nothing")
    p.add_argument("--force-write", action="store_true", help="write latest.json even if nothing changed")
    p.add_argument("--backfill-days", type=int, default=0, help="one-off: fill this many days back from order dates")
    p.add_argument("--date-field", default=DATE_FIELDS,
                   help=f"order date field(s) for --backfill-days / --compare; the first one an order has wins (default {DATE_FIELDS})")
    p.add_argument("--compare", metavar="DAY", help="print today's (or DAY's, YYYY-MM-DD) live windows per tenant against the order dates")
    p.add_argument("--insecure", action="store_true", help="https without certificate checks")
    a = p.parse_args()

    if a.compare:
        import boto3
        day = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d") if a.compare == "today" else a.compare
        try:
            rows = compare(boto3.client("s3", region_name=REGION), credentials(), a.date_field, day, a.insecure)
        except Exception as e:                          # noqa: BLE001
            log(f"compare failed: {e}")
            return 1
        print(f"{'tenant':<48}{'live':>8}{'by date':>9}")
        for t, n_live, n_date in rows:
            print(f"{t:<48}{n_live:>8}{n_date:>9}")
        return 0

    if a.backfill_days:
        if not 1 <= a.backfill_days <= 15:
            p.error("--backfill-days must be 1 to 15")
        state = read_state()
        until = None
        if state.get("inboundStart"):
            until = dt.datetime.strptime(state["inboundStart"], "%Y-%m-%dT%H:%MZ").replace(tzinfo=dt.timezone.utc)
        import boto3
        s3 = None if a.dry_run else boto3.client("s3", region_name=REGION)
        try:
            n = backfill(s3, credentials(), a.date_field, a.backfill_days, dt.datetime.now(dt.timezone.utc), until,
                         dry_run=a.dry_run, insecure=a.insecure)
        except Exception as e:                          # noqa: BLE001
            log(f"backfill failed: {e}")
            return 1
        log(f"backfill done: {n:,} orders up to {minute_key(until) if until else 'now'}")
        return 0

    fetch = lambda: cat_indices(credentials(), insecure=a.insecure)  # noqa: E731  (a missing .env = a failed run)
    if a.dry_run:
        try:
            print_rows(group(fetch()))
        except Exception as e:                          # noqa: BLE001
            log(f"failed: {e}")
            return 1
        return 0
    import boto3
    s3 = boto3.client("s3", region_name=REGION)
    status, state = run(s3, fetch, force=a.force_write, state=read_state())
    write_state(state)
    return 0 if status.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
