"""A/B test page: sessions per store and page type, and how many carry a usable session ID.

Where the numbers come from
    s3://LOGS_BUCKET/ABTEST_PREFIX counts/dt=YYYY-MM-DD/part-HH.csv.gz   (minute,source,store,page,category,value,count)
    s3://LOGS_BUCKET/ABTEST_PREFIX ids/dt=YYYY-MM-DD/part-HH.csv.gz      the entries with a bad session ID
    s3://LOGS_BUCKET/ABTEST_PREFIX status.json                          the collector's last run
written every 5 minutes by scripts/ab_session_counts.py from the EDD session cluster (eddsderrh1):
source "session" = <store>-session_history-* (PDP, CART, COP, OC), source "cop" = <store>-cop-derrh-* ("COP DI").
An "_all" total row marks every window that was counted, so a missing window shows as a gap, not as 0.

Categories: valid (session ID starts with M), health (fenix-health-… checks: not shoppers, left out of
the charts and the bad share), the known bad values (NA, ORDER_LOOKUP_CALL, xyz123, novalue), empty,
missing, numbers, uuid, other. "variant" rows count the entries of each A/B test / variant.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import duckdb
from botocore.exceptions import ClientError

from .errors import bad_request
from .ops import DAY, MIN, ZONES, MetricFiles, zone_key
from .settings import Settings

ALL = "_all"
STEP = 5 * MIN
GOOD = ("total", "valid", "health", "variant")
PAGE_ORDER = ["PDP", "CART", "COP", "OC"]
CAT_ORDER = ["valid", "NA", "ORDER_LOOKUP_CALL", "xyz123", "novalue", "empty", "missing", "numbers", "uuid", "other", "health"]
RANGE_MAX_DAYS = 31
# bucket size for a range: up to 6 h per 5 minutes, 2 days per 15 minutes, 8 days per hour, then 3 hours
RANGE_STEPS = ((6 * 3600_000, 5 * MIN), (2 * DAY, 15 * MIN), (8 * DAY, 60 * MIN), (RANGE_MAX_DAYS * DAY, 180 * MIN))
COUNT_COLUMNS = ("{'minute': 'VARCHAR', 'source': 'VARCHAR', 'store': 'VARCHAR', 'page': 'VARCHAR', "
                 "'category': 'VARCHAR', 'value': 'VARCHAR', 'count': 'BIGINT'}")
ID_COLUMNS = ("{'minute': 'VARCHAR', 'source': 'VARCHAR', 'store': 'VARCHAR', 'page': 'VARCHAR', 'category': 'VARCHAR', "
              "'session_id': 'VARCHAR', 'order_id': 'VARCHAR', 'doc_id': 'VARCHAR', 'at': 'VARCHAR'}")
SOURCES = ("session", "cop")
STATUS_SECONDS = 60


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _page_key(p: str) -> tuple:
    return (PAGE_ORDER.index(p) if p in PAGE_ORDER else len(PAGE_ORDER), p)


def _cat_key(c: str) -> tuple:
    return (CAT_ORDER.index(c) if c in CAT_ORDER else CAT_ORDER.index("other") - 0.5, c)


def _bad(cats: dict[str, int]) -> int:
    return sum(n for c, n in cats.items() if c not in GOOD)


def _pct(part: int, whole: int) -> float | None:
    return round(100 * part / whole, 2) if whole else None


class AbTestService:
    def __init__(self, settings: Settings, logs, now=_now):
        self.settings = settings
        self.now = now
        self.prefix = settings.abtest_prefix
        src = logs.source
        # a backfill can rewrite finished files: checked again every 5 minutes
        self.counts = MetricFiles(settings, src, self.prefix + "counts/", "abtest-counts", revalidate_seconds=300)
        self.ids = MetricFiles(settings, src, self.prefix + "ids/", "abtest-ids", revalidate_seconds=300)
        self.s3 = getattr(src, "s3", None)
        self.bucket = getattr(src, "bucket", None)
        self.local_dir = settings.logs_local_dir if self.s3 is None else None
        self._status: tuple[float, dict] | None = None
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- reading
    def _range(self, start_ms: int, end_ms: int) -> tuple[int, int, int]:
        now_ms = int(self.now().timestamp() * 1000)
        start, end = int(start_ms) // STEP * STEP, -(-int(end_ms) // STEP) * STEP
        end = min(end, -(-now_ms // STEP) * STEP)
        if end <= start:
            raise bad_request("INVALID_RANGE", "The end must be after the start (and not in the future)")
        if end - start > RANGE_MAX_DAYS * DAY:
            raise bad_request("RANGE_TOO_LARGE", f"Pick at most {RANGE_MAX_DAYS} days")
        return start, end, now_ms

    def _rows(self, files: MetricFiles, columns: str, select: str, where: str, params: list, start: int, end: int,
              now_ms: int) -> list[tuple]:
        paths = files.paths(start, end, now_ms)
        if not paths:
            return []
        con = duckdb.connect(config={"threads": 2, "memory_limit": "512MB"})
        try:
            return con.execute(
                f"SELECT {select} FROM (SELECT epoch_ms(strptime(minute, '%Y-%m-%dT%H:%MZ')) AS t, * "
                f"FROM read_csv(?, header=true, columns={columns}, compression='gzip', nullstr='\\x01')) "
                f"WHERE t >= ? AND t < ? {where}", [paths, start, end, *params]).fetchall()
        finally:
            con.close()

    def read_status(self) -> dict:
        """The collector's status.json (read again after a minute); {} when there is none yet."""
        with self._lock:
            if self._status and time.monotonic() - self._status[0] < STATUS_SECONDS:
                return self._status[1]
        key = self.prefix + "status.json"
        data: dict = {}
        try:
            if self.s3 is not None:
                data = json.loads(self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read())
            elif self.local_dir:
                from pathlib import Path
                p = Path(self.local_dir) / key
                data = json.loads(p.read_text()) if p.exists() else {}
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            data = {} if code in ("NoSuchKey", "404") else {"readError": f"Can't read s3://{self.bucket}/{key}: {code or e}"}
        except (OSError, ValueError) as e:
            data = {"readError": f"Can't read {key}: {e}"}
        with self._lock:
            self._status = (time.monotonic(), data)
        return data

    def status(self) -> dict:
        return {"counts": self.counts.describe(), "ids": self.ids.describe(), "error": self.counts.error or self.ids.error,
                "collector": self.read_status()}

    # ---------------------------------------------------------------- the page
    def summary(self, start_ms: int, end_ms: int, zone: str = "UTC", store: str | None = None) -> dict:
        start, end, now_ms = self._range(start_ms, end_ms)
        zone = zone_key(zone)
        span = end - start
        step = next(st for lim, st in RANGE_STEPS if span <= lim)
        off = int(datetime.fromtimestamp(start / 1000, ZoneInfo(ZONES[zone])).utcoffset().total_seconds() * 1000)
        first = (start + off) // step * step - off           # buckets on round times in the zone
        nb = -(-(end - first) // step)
        rows = self._rows(self.counts, COUNT_COLUMNS,
                          "t, source, store, coalesce(page, ''), category, coalesce(value, ''), coalesce(count, 0)",
                          "", [], start, end, now_ms)

        counted: dict[str, set[int]] = {s: set() for s in SOURCES}
        # whole range, per source / store / page: {category: n}; variants per store / page
        per: dict[tuple[str, str, str], dict[str, int]] = {}
        variants: dict[tuple[str, str, str], int] = {}
        for t, source, st, page, cat, value, n in rows:
            if st == ALL:
                if source in counted:
                    counted[source].add(int(t))
                continue
            if cat == "variant":
                variants[(st, page, value)] = variants.get((st, page, value), 0) + int(n)
                continue
            d = per.setdefault((source, st, page), {})
            d[cat] = d.get(cat, 0) + int(n)

        stores: dict[str, dict] = {}
        for (source, st, page), cats in per.items():
            s = stores.setdefault(st, {"store": st, "entries": 0, "pages": {}, "oc": 0, "ocBad": 0, "cop": 0, "copBad": 0,
                                       "health": 0, "abTest": False})
            shoppers = cats.get("total", 0) - cats.get("health", 0)
            s["health"] += cats.get("health", 0)
            if source == "session":
                s["entries"] += shoppers
                s["pages"][page] = s["pages"].get(page, 0) + shoppers
                if page == "OC":
                    s["oc"] += shoppers
                    s["ocBad"] += _bad(cats)
            else:
                s["cop"] += shoppers
                s["copBad"] += _bad(cats)
        for (st, _, _), n in variants.items():
            if n and st in stores:
                stores[st]["abTest"] = True
        for s in stores.values():
            s["ocBadPct"] = _pct(s["ocBad"], s["oc"])
            s["copBadPct"] = _pct(s["copBad"], s["cop"])
        store_list = sorted(stores.values(), key=lambda s: (-s["entries"], -s["cop"], s["store"]))

        last = [max(v) for v in counted.values() if v]
        out: dict[str, Any] = {
            "from": start, "to": end, "zone": zone, "step": step, "start": first, "n": nb,
            "asOf": min(last) + STEP if last else None,
            "countedFrom": min((min(v) for v in counted.values() if v), default=None),
            "windows": {s: len(v) for s, v in counted.items()},
            "stores": store_list,
            "store": None,
            "status": self.read_status(),
            "error": self.counts.error,
        }
        if not store:
            return out
        if store not in stores:
            out["store"] = {"store": store, "found": False}
            return out

        # per bucket: which buckets have a counted window (else null), shoppers per page, bad OC
        seen = [0] * nb
        for t in counted["session"]:
            i = (t - first) // step
            if 0 <= i < nb:
                seen[i] += 1
        series: dict[str, list] = {}
        bad_oc = [0] * nb
        tables: dict[str, dict[str, dict[str, int]]] = {s: {} for s in SOURCES}
        var_rows: dict[tuple[str, str], int] = {}
        var_series: dict[str, list[int]] = {}
        for t, source, st, page, cat, value, n in rows:
            if st != store:
                continue
            n = int(n)
            i = (int(t) - first) // step
            if cat == "variant":
                var_rows[(page, value)] = var_rows.get((page, value), 0) + n
                if source == "session" and page == "OC" and 0 <= i < nb:
                    var_series.setdefault(value, [0] * nb)[i] += n
                continue
            tab = tables[source].setdefault(page, {})
            tab[cat] = tab.get(cat, 0) + n
            if source != "session" or not 0 <= i < nb:
                continue
            if cat == "total":
                series.setdefault(page, [0] * nb)[i] += n
            elif cat == "health":
                series.setdefault(page, [0] * nb)[i] -= n
            elif page == "OC" and cat not in GOOD:
                bad_oc[i] += n

        per_bucket = step // STEP

        def masked(vals: list[int]) -> list[int | None]:
            """Whole buckets only: one with a window not counted (the edges of the range, the newest
            minutes, a gap) is null, so a line never drops because a bucket is half full."""
            return [v if seen[i] >= per_bucket else None for i, v in enumerate(vals)]

        def table(src: str) -> list[dict]:
            out_rows = []
            for page in sorted(tables[src], key=_page_key):
                cats = tables[src][page]
                total = cats.get("total", 0)
                shoppers = total - cats.get("health", 0)
                bad = _bad(cats)
                out_rows.append({"page": page, "total": total, "shoppers": shoppers, "valid": cats.get("valid", 0),
                                 "bad": bad, "badPct": _pct(bad, shoppers), "health": cats.get("health", 0),
                                 "categories": [{"category": c, "count": cats[c], "pct": _pct(cats[c], shoppers)}
                                                for c in sorted(cats, key=_cat_key) if c not in ("total",) and cats[c]]})
            return out_rows

        out["store"] = {
            "store": store, "found": True, **{k: stores[store][k] for k in ("entries", "oc", "ocBad", "ocBadPct", "cop", "copBad",
                                                                             "copBadPct", "abTest", "health")},
            "series": {p: masked(v) for p, v in sorted(series.items(), key=lambda x: _page_key(x[0]))},
            "badOc": masked(bad_oc) if "OC" in series else None,
            "sessionCheck": table("session"),
            "copCheck": table("cop"),
            "variants": [{"page": p, "variant": v, "count": n, "pct": None}
                         for (p, v), n in sorted(var_rows.items(), key=lambda x: (_page_key(x[0][0]), -x[1]))],
            "variantSeries": {v: masked(vals) for v, vals in sorted(var_series.items())},
        }
        for page in {r["page"] for r in out["store"]["variants"]}:
            tot = sum(r["count"] for r in out["store"]["variants"] if r["page"] == page)
            for r in out["store"]["variants"]:
                if r["page"] == page:
                    r["pct"] = _pct(r["count"], tot)
        return out

    def id_list(self, start_ms: int, end_ms: int, store: str, source: str = "session", page: str | None = None,
                category: str | None = None, limit: int = 500) -> dict:
        """The entries with a bad session ID (OC of session_history, every entry of cop-derrh), newest first."""
        if source not in SOURCES:
            raise bad_request("INVALID_SOURCE", f"source must be one of {', '.join(SOURCES)}")
        start, end, now_ms = self._range(start_ms, end_ms)
        where, params = "AND store = ? AND source = ?", [store, source]
        if page:
            where += " AND page = ?"
            params.append(page.upper())
        if category and category != "bad":
            where += " AND category = ?"
            params.append(category)
        rows = self._rows(self.ids, ID_COLUMNS, "\"at\", page, category, coalesce(session_id, ''), coalesce(order_id, ''), "
                          "coalesce(doc_id, '')", where, params, start, end, now_ms)
        rows.sort(key=lambda r: r[0] or "", reverse=True)
        values: dict[str, int] = {}
        for r in rows:
            values[r[3]] = values.get(r[3], 0) + 1
        return {
            "from": start, "to": end, "store": store, "source": source, "page": page, "category": category,
            "total": len(rows), "truncated": len(rows) > limit,
            "values": [{"value": v, "count": n} for v, n in sorted(values.items(), key=lambda x: -x[1])[:50]],
            "rows": [{"at": a, "page": p, "category": c, "sessionId": s, "orderId": o, "docId": d}
                     for a, p, c, s, o, d in rows[:limit]],
            "error": self.ids.error,
        }
