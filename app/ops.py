"""Ops dashboard: API Gateway calls per stage and orders processed (OMS / CRS), today against the
same day last week. A "day" runs midnight to midnight in the zone the caller picks (IST, PST, EST or
UTC; PST and EST follow daylight saving, so a US day can be 23 or 25 hours). Last week = the same
date 7 days back in that zone, compared minute by minute from its midnight.

Where the numbers come from
    API Gateway  s3://LOGS_BUCKET/OPS_METRICS_PREFIX dt=YYYY-MM-DD/part-HH.csv.gz, one row per stage per
                 minute, written every 5 minutes by scripts/apigw_metrics_to_s3.py on another server
                 (6 files a day; HH = 00, 04, … 20; the newest file is rewritten until its 4 hours end).
    Orders       the Vector logs of the clusters in config/ops-dashboard.yaml, counted here: one unique
                 (store, order id) per service per UTC day, at the minute it is first seen. A day in
                 another zone joins the two UTC days it overlaps (an order seen on both counts once,
                 at its first sighting) and keeps the orders first seen inside it.

Where things live (local disk, all rebuilt from S3 if deleted)
    CACHE_DIR/ops/apigw/...             copies of the metric files (finished ones never change)
    CACHE_DIR/ops/orders/<day>.json     the orders counted per day, and which log files were read
    CACHE_DIR/ops/orders/status.json    the counter's last run (read by every API worker)

One counter runs per container (every API worker starts one; a file lock lets only one count).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import duckdb
import yaml
from botocore.exceptions import ClientError, EndpointConnectionError

from .errors import bad_request
from .settings import Settings

log = logging.getLogger("vector_logs.ops")

MIN = 60_000
DAY = 86_400_000
WEEK = 7 * DAY
BLOCK_HOURS = 4
SETTLE_MS = 40 * MIN          # a metric file is final this long after its 4 hours end
REFRESH_SECONDS = 60          # the newest metric file is read from S3 again after this
STALE_MS = 15 * MIN           # API numbers older than this are shown as stale (the copy job stopped?)
ORDERS_FINAL_MS = 3 * 3600_000  # a day's logs are read for the last time this long after it ends
KEEP_DAYS = 15                # order counts kept on disk
STATE_VERSION = 1
RANGE_MAX_DAYS = 31           # History: the longest range that can be picked
# bucket size for the charts of a range: up to 6 h per minute, 2 days per 5 min, 8 days per 15 min, then hourly
RANGE_STEPS = ((6 * 3600_000, MIN), (2 * DAY, 5 * MIN), (8 * DAY, 15 * MIN), (RANGE_MAX_DAYS * DAY, 60 * MIN))

CSV_COLUMNS = ("{'minute': 'VARCHAR', 'api': 'VARCHAR', 'stage': 'VARCHAR', 'calls': 'BIGINT', "
               "'err_4xx': 'BIGINT', 'err_5xx': 'BIGINT', 'latency_p50_ms': 'DOUBLE', "
               "'latency_p95_ms': 'DOUBLE', 'integration_p95_ms': 'DOUBLE'}")

DEFAULT_STATUS = {"minCalls": 100, "err5xxRedPct": 1.0, "err5xxAmberPct": 0.5, "err4xxTimesLastWeek": 2.0,
                  "err4xxMinPct": 5.0, "p95RedTimes": 2.0, "p95AmberTimes": 1.5, "p95MinMs": 300,
                  "quietPct": 50}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def day_start_ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def parse_day(value: str | None, today: date) -> date:
    if not value:
        return today
    try:
        d = date.fromisoformat(value)
    except ValueError:
        raise bad_request("INVALID_DAY", f"'{value}' is not a date (YYYY-MM-DD)")
    if d > today:
        raise bad_request("INVALID_DAY", f"{value} is in the future")
    return d


ZONES = {"IST": "Asia/Kolkata", "PST": "America/Los_Angeles", "EST": "America/New_York", "UTC": "UTC"}


def zone_key(name: str | None) -> str:
    key = (name or "UTC").strip().upper()
    if key not in ZONES:
        raise bad_request("INVALID_ZONE", f"zone must be one of {', '.join(ZONES)}")
    return key


@dataclass(frozen=True)
class ZoneDay:
    """One calendar day in a zone: [start, end) in epoch ms."""
    day: date
    zone: str
    start: int
    end: int

    @property
    def minutes(self) -> int:
        return (self.end - self.start) // MIN

    def utc_days(self) -> list[date]:
        first = datetime.fromtimestamp(self.start / 1000, timezone.utc).date()
        last = datetime.fromtimestamp((self.end - 1) / 1000, timezone.utc).date()
        return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def zone_day(d: date, zone: str = "UTC") -> ZoneDay:
    key = zone_key(zone)
    tz = ZoneInfo(ZONES[key])
    nxt = d + timedelta(days=1)
    start = datetime(d.year, d.month, d.day, tzinfo=tz)
    end = datetime(nxt.year, nxt.month, nxt.day, tzinfo=tz)
    return ZoneDay(d, key, int(start.timestamp() * 1000), int(end.timestamp() * 1000))


def zone_today(now: datetime, zone: str) -> date:
    return now.astimezone(ZoneInfo(ZONES[zone_key(zone)])).date()


# ------------------------------------------------------------------ config file
class OpsConfigError(ValueError):
    pass


@dataclass
class OrderLine:
    contains: str
    pattern: re.Pattern
    skip: dict[str, set[str]] = field(default_factory=dict)


@dataclass
class OrderService:
    name: str
    cluster: str
    lines: list[OrderLine]

    def signature(self) -> str:
        raw = json.dumps([self.cluster, [(l.contains, l.pattern.pattern, sorted((k, sorted(v)) for k, v in l.skip.items()))
                                         for l in self.lines]])
        return hashlib.sha1(raw.encode()).hexdigest()[:12]


@dataclass
class OpsConfig:
    services: list[OrderService] = field(default_factory=list)
    tenants: dict[str, str] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)
    status: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_STATUS))

    def name(self, key: str) -> str:
        if key in self.names:
            return self.names[key]
        return key[:-len(".myshopify.com")] if key.endswith(".myshopify.com") else key

    def service(self, name: str) -> OrderService | None:
        return next((s for s in self.services if s.name == name), None)


def parse_config(text: str) -> OpsConfig:
    try:
        doc = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise OpsConfigError(f"not valid YAML: {e}") from e
    if not isinstance(doc, dict):
        raise OpsConfigError("the file must be a mapping (orders:, tenants:, names:, status:)")
    cfg = OpsConfig()
    seen: set[str] = set()
    for i, s in enumerate(doc.get("orders") or [], 1):
        where = f"orders entry {i}"
        if not isinstance(s, dict) or not s.get("service") or not s.get("cluster"):
            raise OpsConfigError(f"{where}: needs service: and cluster:")
        name, cluster = str(s["service"]).strip(), str(s["cluster"]).strip()
        if name in seen:
            raise OpsConfigError(f"{where}: service '{name}' is listed twice")
        seen.add(name)
        lines = []
        for j, ln in enumerate(s.get("lines") or [], 1):
            w = f"{where} ({name}), line {j}"
            if not isinstance(ln, dict) or not ln.get("contains") or not ln.get("pattern"):
                raise OpsConfigError(f"{w}: needs contains: and pattern:")
            try:
                rx = re.compile(str(ln["pattern"]))
            except re.error as e:
                raise OpsConfigError(f"{w}: pattern is not a valid regular expression: {e}") from e
            groups = set(rx.groupindex)
            if "order" not in groups or not ({"store", "tenant"} & groups):
                raise OpsConfigError(f"{w}: pattern needs the named groups (?P<order>…) and (?P<store>…) or (?P<tenant>…)")
            skip = {}
            for k, vals in (ln.get("skip") or {}).items():
                if k not in groups:
                    raise OpsConfigError(f"{w}: skip: '{k}' is not a group in the pattern")
                skip[k] = {str(v) for v in (vals if isinstance(vals, list) else [vals])}
            lines.append(OrderLine(str(ln["contains"]), rx, skip))
        if not lines:
            raise OpsConfigError(f"{where} ({name}): needs at least one entry under lines:")
        cfg.services.append(OrderService(name, cluster, lines))
    cfg.tenants = {str(k): str(v) for k, v in (doc.get("tenants") or {}).items()}
    cfg.names = {str(k): str(v) for k, v in (doc.get("names") or {}).items()}
    for k, v in (doc.get("status") or {}).items():
        if k not in DEFAULT_STATUS:
            raise OpsConfigError(f"status: unknown setting '{k}' (known: {', '.join(DEFAULT_STATUS)})")
        try:
            cfg.status[k] = float(v)
        except (TypeError, ValueError):
            raise OpsConfigError(f"status: {k} must be a number")
    return cfg


class ConfigFile:
    """config/ops-dashboard.yaml, read again when it changes. A broken file keeps the last good one."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._sig: tuple | None = None
        self._checked = 0.0
        self.config = OpsConfig()
        self.error: str | None = None
        self.exists = False

    def get(self) -> OpsConfig:
        if time.monotonic() - self._checked < 5:
            return self.config
        self._checked = time.monotonic()
        try:
            st = os.stat(self.path)
            sig = (st.st_mtime_ns, st.st_size, st.st_ino)
        except FileNotFoundError:
            sig = None
        except OSError as e:
            self.error = f"Can't read {self.path}: {e}"
            return self.config
        with self._lock:
            if sig == self._sig and self._sig is not None:
                return self.config
            if sig is None:
                self._sig, self.exists, self.config = None, False, OpsConfig()
                self.error = f"{self.path} is missing, so no orders are counted"
                return self.config
            try:
                with open(self.path, encoding="utf-8") as fh:
                    cfg = parse_config(fh.read())
            except OpsConfigError as e:
                self._sig, self.exists, self.error = sig, True, f"{os.path.basename(self.path)}: {e}"
                log.warning("ops dashboard config %s has a mistake (keeping the last good version): %s", self.path, e)
                return self.config
            except OSError as e:
                self.error = f"Can't read {self.path}: {e}"
                return self.config
            self._sig, self.exists, self.config, self.error = sig, True, cfg, None
            log.info("ops dashboard config loaded: %d order services", len(cfg.services))
            return self.config


# ------------------------------------------------------------------ API Gateway metric files
class MetricFiles:
    """Local copies of the per-stage metric files. Finished 4-hour files are kept; the one still being
    written is read from S3 again when the copy is older than a minute."""

    def __init__(self, settings: Settings, logs_source):
        self.prefix = settings.ops_metrics_prefix
        self.root = Path(settings.cache_dir) / "ops" / "apigw"
        self.s3 = getattr(logs_source, "s3", None)
        self.bucket = getattr(logs_source, "bucket", None)
        self.local_dir = None if self.s3 is not None else Path(settings.logs_local_dir)
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="ops-fetch")
        self.error: str | None = None

    def describe(self) -> str:
        return f"s3://{self.bucket}/{self.prefix}" if self.s3 is not None else str(Path(self.local_dir) / self.prefix)

    def key(self, d: date, block: int) -> str:
        return f"{self.prefix}dt={d:%Y-%m-%d}/part-{block:02d}.csv.gz"

    def _lock_for(self, key: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(key, threading.Lock())

    def get(self, d: date, block: int, now_ms: int) -> str | None:
        key = self.key(d, block)
        start = day_start_ms(d) + block * 3600_000
        if start > now_ms:
            return None
        if self.local_dir is not None:
            p = self.local_dir / key
            return str(p) if p.exists() else None
        final = now_ms > start + BLOCK_HOURS * 3600_000 + SETTLE_MS
        path = self.root / key
        with self._lock_for(key):
            try:
                st = path.stat()
                if final and st.st_mtime * 1000 > start + BLOCK_HOURS * 3600_000 + SETTLE_MS:
                    return str(path)              # copied after the file was finished: never changes again
                if time.time() - st.st_mtime < REFRESH_SECONDS:
                    return str(path)
                have = True
            except FileNotFoundError:
                have = False
            missing = path.with_name(path.name + ".missing")
            if not have and missing.exists() and time.time() - missing.stat().st_mtime < REFRESH_SECONDS:
                return None
            try:
                body = self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
            except ClientError as e:
                code = e.response.get("Error", {}).get("Code", "")
                if code in ("NoSuchKey", "404"):
                    missing.parent.mkdir(parents=True, exist_ok=True)
                    missing.touch()
                    if have:
                        os.utime(path)
                    return str(path) if have else None
                if code in ("AccessDenied", "403"):
                    # without s3:ListBucket a missing file also reads as AccessDenied
                    self.error = (f"No access to s3://{self.bucket}/{self.prefix} (the server role needs s3:GetObject "
                                  f"and s3:ListBucket on it)")
                    if have:
                        os.utime(path)
                    return str(path) if have else None
                self.error = f"Can't read s3://{self.bucket}/{key}: {code or e}"
                return str(path) if have else None
            except EndpointConnectionError as e:
                self.error = f"Can't reach S3: {e}"
                return str(path) if have else None
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.part")
            tmp.write_bytes(body)
            os.replace(tmp, path)
            missing.unlink(missing_ok=True)
            self.error = None
            return str(path)

    def paths(self, start_ms: int, end_ms: int, now_ms: int) -> list[str]:
        """Every file that holds minutes in [start_ms, end_ms)."""
        blocks = []
        t = (start_ms // (BLOCK_HOURS * 3600_000)) * BLOCK_HOURS * 3600_000
        while t < end_ms:
            dt = datetime.fromtimestamp(t / 1000, timezone.utc)
            blocks.append((dt.date(), dt.hour))
            t += BLOCK_HOURS * 3600_000
        found = list(self._pool.map(lambda b: self.get(b[0], b[1], now_ms), blocks))
        return [p for p in found if p]


def _db() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(config={"threads": 2, "memory_limit": "256MB"})


def _load_metrics(con, paths: list[str], ranges: list[tuple[int, int]]) -> None:
    """Temp table m(t, api, stage, calls, …) with the minutes inside any of the ranges."""
    con.execute("CREATE OR REPLACE TEMP TABLE m (t BIGINT, api VARCHAR, stage VARCHAR, calls BIGINT, e4 BIGINT, "
                "e5 BIGINT, p50 DOUBLE, p95 DOUBLE, ip95 DOUBLE)")
    if not paths or not ranges:
        return
    cond = " OR ".join("(t >= ? AND t < ?)" for _ in ranges)
    params: list[Any] = [paths]
    for a, b in ranges:
        params += [a, b]
    con.execute(
        f"INSERT INTO m SELECT * FROM (SELECT epoch_ms(strptime(minute, '%Y-%m-%dT%H:%MZ')) AS t, api, stage, "
        f"coalesce(calls, 0), coalesce(err_4xx, 0), coalesce(err_5xx, 0), latency_p50_ms, latency_p95_ms, "
        f"integration_p95_ms FROM read_csv(?, header=true, columns={CSV_COLUMNS}, compression='gzip')) WHERE {cond}",
        params)


def _minute_array(rows, start_ms: int, n: int = 1440) -> list:
    out: list = [0] * n
    for t, v in rows:
        i = (int(t) - start_ms) // MIN
        if 0 <= i < n:
            out[i] = v
    return out


def _pct(part: float, whole: float) -> float | None:
    return round(100.0 * part / whole, 2) if whole else None


def _change(now: float | None, before: float | None) -> float | None:
    if now is None or not before:
        return None
    return round(100.0 * (now - before) / before, 1)


def judge(now: dict, lw: dict | None, th: dict) -> tuple[str, list[str]]:
    """green / amber / red / idle for one stage, with the reasons, from a window and the same
    window last week. Only shown on the page; nothing is sent anywhere."""
    calls, lw_calls = now["calls"], (lw or {}).get("calls") or 0
    if calls < th["minCalls"] and lw_calls < th["minCalls"]:
        return "idle", []
    red, amber = [], []
    if calls == 0:
        return "red", [f"no calls (last week {lw_calls:,})"]
    if lw_calls >= th["minCalls"] and calls < lw_calls * th["quietPct"] / 100:
        amber.append(f"calls {round(100 * calls / lw_calls)}% of last week")
    e5 = 100.0 * now["err5xx"] / calls
    if calls >= th["minCalls"]:
        if e5 >= th["err5xxRedPct"]:
            red.append(f"5xx {e5:.1f}%")
        elif e5 >= th["err5xxAmberPct"]:
            amber.append(f"5xx {e5:.1f}%")
        e4 = 100.0 * now["err4xx"] / calls
        lw_e4 = 100.0 * lw["err4xx"] / lw_calls if lw and lw_calls else None
        if lw_e4 is not None and e4 >= th["err4xxMinPct"] and e4 >= th["err4xxTimesLastWeek"] * max(lw_e4, 0.1):
            amber.append(f"4xx {e4:.1f}% (last week {lw_e4:.1f}%)")
        p95, lw_p95 = now.get("p95"), (lw or {}).get("p95")
        if p95 and lw_p95 and lw_calls >= th["minCalls"] and p95 >= th["p95MinMs"]:
            x = p95 / lw_p95
            if x >= th["p95RedTimes"]:
                red.append(f"p95 {p95:,.0f} ms, {x:.1f}× last week")
            elif x >= th["p95AmberTimes"]:
                amber.append(f"p95 {p95:,.0f} ms, {x:.1f}× last week")
    if red:
        return "red", red + amber
    if amber:
        return "amber", amber
    return "green", []


# ------------------------------------------------------------------ order counter
def _empty_state(day: date) -> dict:
    return {"version": STATE_VERSION, "day": day.isoformat(), "final": False, "services": {}, "newestMs": None,
            "updated": None}


class OrderCounter:
    """Reads new log files of the order clusters and keeps, per day, every (service, store, order id)
    with the time it was first seen. run_once() (tests) or start() for the background thread."""

    BATCH = 80

    def __init__(self, settings: Settings, logs, config: ConfigFile, now=_now):
        self.settings = settings
        self.logs = logs
        self.config = config
        self.now = now
        self.dir = Path(settings.cache_dir) / "ops" / "orders"
        self._stop = threading.Event()
        self._wake = threading.Event()

    # -- state files
    def path(self, d: date) -> Path:
        return self.dir / f"{d.isoformat()}.json"

    def load(self, d: date) -> dict | None:
        try:
            with open(self.path(d), encoding="utf-8") as fh:
                st = json.load(fh)
            return st if st.get("version") == STATE_VERSION else None
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None

    def _save(self, st: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        p = self.path(date.fromisoformat(st["day"]))
        tmp = p.with_name(f".{p.name}.{os.getpid()}.part")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(st, fh, separators=(",", ":"))
        os.replace(tmp, p)

    def _write_status(self, status: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / f".status.{os.getpid()}.part"
        tmp.write_text(json.dumps(status), encoding="utf-8")
        os.replace(tmp, self.dir / "status.json")

    def read_status(self) -> dict:
        try:
            return json.loads((self.dir / "status.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    # -- counting
    def run_once(self, days_back: int | None = None) -> dict:
        now = self.now()
        cfg = self.config.get()
        today = now.date()
        back = self.settings.ops_backfill_days if days_back is None else days_back
        days = [today] + [today - timedelta(days=i) for i in range(1, back + 1)]
        t0 = time.monotonic()
        status = {"lastRun": now.isoformat(timespec="seconds"), "running": True, "error": None,
                  "days": {}, "configError": self.config.error}
        prev = self.read_status()
        status["lastOk"] = prev.get("lastOk")
        self._write_status({**prev, "running": True})
        errors = []
        for d in days:
            try:
                st = self._count_day(d, cfg, now)
                status["days"][d.isoformat()] = {"final": st["final"], "files": sum(len(s["files"]) for s in st["services"].values()),
                                                 "orders": sum(len(ids) for s in st["services"].values() for ids in s["orders"].values())}
            except Exception as e:  # noqa: BLE001 - one bad day must not stop the others
                log.exception("ops: counting orders for %s failed", d)
                errors.append(f"{d}: {type(e).__name__}: {e}"[:300])
        self._clean(today)
        status.update(running=False, seconds=round(time.monotonic() - t0, 1), error="; ".join(errors) or None)
        if not errors:
            status["lastOk"] = status["lastRun"]
        self._write_status(status)
        return status

    def _count_day(self, d: date, cfg: OpsConfig, now: datetime) -> dict:
        st = self.load(d) or _empty_state(d)
        sigs = {s.name: s.signature() for s in cfg.services}
        changed = set(st["services"]) != set(sigs) or any(st["services"][n].get("sig") != sig
                                                            for n, sig in sigs.items() if n in st["services"])
        if st["final"] and not changed:
            return st
        start = day_start_ms(d)
        end = start + DAY
        now_ms = int(now.timestamp() * 1000)
        for svc in cfg.services:
            cur = st["services"].get(svc.name)
            if cur is None or cur.get("sig") != sigs[svc.name] or cur.get("cluster") != svc.cluster:
                cur = st["services"][svc.name] = {"sig": sigs[svc.name], "cluster": svc.cluster, "files": [], "orders": {}}
            done = set(cur["files"])
            keys: list[str] = []
            for h in range(24):
                hour = datetime(d.year, d.month, d.day, h, tzinfo=timezone.utc)
                if hour > now:
                    break
                keys += [k for k, _ in self.logs.hour_files(svc.cluster, hour) if k not in done]
            for i in range(0, len(keys), self.BATCH):
                batch = keys[i:i + self.BATCH]
                newest = self._read(svc, cfg, batch, cur["orders"], start, end)
                if newest and (st["newestMs"] is None or newest > st["newestMs"]):
                    st["newestMs"] = min(newest, now_ms)
                cur["files"] += batch
                st["updated"] = now.isoformat(timespec="seconds")
                self._save(st)             # progress survives a restart in the middle of a big day
        for gone in set(st["services"]) - set(sigs):
            del st["services"][gone]
        st["final"] = now_ms > end + ORDERS_FINAL_MS
        st["updated"] = now.isoformat(timespec="seconds")
        self._save(st)
        return st

    def _read(self, svc: OrderService, cfg: OpsConfig, keys: list[str], orders: dict, start: int, end: int) -> int | None:
        paths, _ = self.logs.materialize(keys)
        con = _db()
        try:
            cols = {r[0] for r in con.execute("DESCRIBE SELECT * FROM read_parquet(?, union_by_name=true)", [paths]).fetchall()}
            texts = [f'"{c}"' for c in ("msg", "body", "raw") if c in cols]
            if not texts or "log_time_ms" not in cols:
                return None
            text = f"coalesce({', '.join(texts)})" if len(texts) > 1 else texts[0]
            needles = sorted({ln.contains for ln in svc.lines})
            where = " OR ".join(f"contains({text}, ?)" for _ in needles)
            src = "read_parquet(?, union_by_name=true)"
            newest = con.execute(f"SELECT max(log_time_ms) FROM {src}", [paths]).fetchone()[0]
            rows = con.execute(f"SELECT log_time_ms, {text} FROM {src} WHERE log_time_ms IS NOT NULL AND ({where})",
                               [paths, *needles]).fetchall()
        finally:
            con.close()
        for ms, line in rows:
            if not line or not (start <= ms < end):
                continue
            for ln in svc.lines:
                if ln.contains not in line:
                    continue
                m = ln.pattern.search(line)
                if not m:
                    continue
                g = m.groupdict()
                order = (g.get("order") or "").strip()
                if not order or order.lower() == "null" or any(g.get(k) in vals for k, vals in ln.skip.items()):
                    break
                store = (g.get("store") or "").strip().lower()
                if not store:
                    tenant = (g.get("tenant") or "").strip()
                    store = cfg.tenants.get(tenant, tenant)
                ids = orders.setdefault(store, {})
                if order not in ids or ms < ids[order]:
                    ids[order] = ms
                break
        return newest

    def _clean(self, today: date) -> None:
        if not self.dir.is_dir():
            return
        cutoff = today - timedelta(days=KEEP_DAYS)
        for p in self.dir.glob("????-??-??.json"):
            try:
                if date.fromisoformat(p.stem) < cutoff:
                    p.unlink()
            except (ValueError, OSError):
                pass

    # -- background thread
    def start(self) -> None:
        threading.Thread(target=self._loop, name="ops-orders", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _loop(self) -> None:
        from .lb_logs import _FileLock
        lock = _FileLock(Path(self.settings.cache_dir) / "ops" / ".orders.lock")
        self._stop.wait(15)
        while not self._stop.is_set():
            if lock.fh is None and not lock.acquire():
                self._stop.wait(60)        # another worker counts
                continue
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - keep the thread alive
                log.exception("ops: order count failed")
            self._wake.wait(max(30, self.settings.ops_interval_seconds))
            self._wake.clear()


# ------------------------------------------------------------------ the service the routes use
class OpsService:
    def __init__(self, settings: Settings, logs, now=_now):
        self.settings = settings
        self.now = now
        self.config = ConfigFile(settings.ops_config_file)
        self.metrics = MetricFiles(settings, logs.source)
        self.orders = OrderCounter(settings, logs, self.config, now=now)
        self._order_cache: dict[str, tuple[float, dict | None]] = {}
        self._cache_lock = threading.Lock()

    def start(self) -> None:
        self.orders.start()

    # -- orders (read side)
    def _day_orders(self, d: date) -> dict | None:
        p = self.orders.path(d)
        try:
            mtime = p.stat().st_mtime
        except FileNotFoundError:
            return None
        key = d.isoformat()
        with self._cache_lock:
            hit = self._order_cache.get(key)
            if hit and hit[0] == mtime:
                return hit[1]
        st = self.orders.load(d)
        with self._cache_lock:
            self._order_cache[key] = (mtime, st)
            if len(self._order_cache) > 20:
                self._order_cache.pop(next(iter(self._order_cache)))
        return st

    def _zone_orders(self, z: ZoneDay, now_ms: int) -> dict | None:
        """The counted orders of the UTC days `z` overlaps, joined: per service, store and order id the
        first time it was seen. None when a day that has started is not counted yet (today's UTC day
        may be missing for a few minutes after midnight UTC: its orders show up on the next count)."""
        states = []
        today_utc = datetime.fromtimestamp(now_ms / 1000, timezone.utc).date()
        for ud in z.utc_days():
            if day_start_ms(ud) > now_ms:
                continue
            st = self._day_orders(ud)
            if st is None:
                if ud == today_utc and states:
                    continue
                return None
            states.append(st)
        if not states:
            return None
        if len(states) == 1:
            st = states[0]
            return {"services": {n: v["orders"] for n, v in st["services"].items()}, "newestMs": st.get("newestMs"),
                    "final": st.get("final"), "updated": st.get("updated")}
        merged: dict[str, dict[str, dict[str, int]]] = {}
        for st in states:
            for name, svc in st["services"].items():
                for s_name, ids in svc["orders"].items():
                    tgt = merged.setdefault(name, {}).setdefault(s_name, {})
                    for oid, ms in ids.items():
                        if oid not in tgt or ms < tgt[oid]:
                            tgt[oid] = ms
        newest = [st["newestMs"] for st in states if st.get("newestMs")]
        return {"services": merged, "newestMs": max(newest) if newest else None,
                "final": all(st.get("final") for st in states),
                "updated": max((st.get("updated") or "") for st in states) or None}

    def _orders_view(self, z: ZoneDay, cutoff_ms: int | None, cfg: OpsConfig, now_ms: int, service: str | None = None,
                     store: str | None = None) -> dict | None:
        """Per minute, per service and per store counts of one day (only orders first seen before
        cutoff_ms when given). None when the day has not been counted yet."""
        st = self._zone_orders(z, now_ms)
        if st is None:
            return None
        start, end = z.start, z.end
        n = z.minutes
        n_h = -(-n // 60)
        minutes = [0] * n
        services, stores = {}, {}
        svc_hours: dict[str, list[int]] = {}
        store_hours: dict[tuple[str, str], list[int]] = {}
        for name in [s.name for s in cfg.services]:
            svc = st["services"].get(name)
            if svc is None or (service and name != service):
                continue
            total = 0
            sh = svc_hours[name] = [0] * n_h
            for s, ids in svc.items():
                if store and s != store:
                    continue
                c = 0
                hh = [0] * n_h
                for ms in ids.values():
                    if not (start <= ms < end) or (cutoff_ms is not None and ms >= cutoff_ms):
                        continue
                    i = (ms - start) // MIN
                    minutes[i] += 1
                    hh[i // 60] += 1
                    sh[i // 60] += 1
                    c += 1
                if c:
                    stores[(name, s)] = c
                    store_hours[(name, s)] = hh
                total += c
            services[name] = total
        newest = st.get("newestMs")
        return {"minutes": minutes, "services": services, "stores": stores, "total": sum(services.values()),
                "serviceHours": svc_hours, "storeHours": store_hours,
                "newestMs": min(newest, end - 1) if newest else None, "final": st.get("final"), "updated": st.get("updated")}

    # -- API metrics (read side)
    def _api_as_of(self, con, z: ZoneDay, now_ms: int) -> int | None:
        start, end = z.start, z.end
        if now_ms >= end + SETTLE_MS:
            return end - MIN                              # a past day: all of it
        row = con.execute("SELECT max(t) FROM m WHERE t >= ? AND t < ?", [start, end]).fetchone()
        return int(row[0]) if row and row[0] is not None else None

    @staticmethod
    def _stage_rows(con, a: int, b: int) -> dict[tuple[str, str], dict]:
        rows = con.execute(
            "SELECT api, stage, sum(calls), sum(e4), sum(e5), "
            "sum(p50 * calls) / nullif(sum(CASE WHEN p50 IS NOT NULL THEN calls END), 0), "
            "sum(p95 * calls) / nullif(sum(CASE WHEN p95 IS NOT NULL THEN calls END), 0), "
            "sum(ip95 * calls) / nullif(sum(CASE WHEN ip95 IS NOT NULL THEN calls END), 0) "
            "FROM m WHERE t >= ? AND t < ? GROUP BY 1, 2", [a, b]).fetchall()
        return {(r[0], r[1]): {"calls": int(r[2] or 0), "err4xx": int(r[3] or 0), "err5xx": int(r[4] or 0),
                               "p50": _r(r[5]), "p95": _r(r[6]), "integrationP95": _r(r[7])} for r in rows}

    def _freshness(self, as_of: int | None, now_ms: int, today: bool) -> dict:
        age = None if as_of is None else max(0, now_ms - (as_of + MIN))
        return {"asOf": as_of, "ageMs": age, "stale": bool(today and (age is None or age > STALE_MS)),
                "error": self.metrics.error}

    def summary(self, day: str | None = None, window_minutes: int = 60, zone: str = "UTC") -> dict:
        now = self.now()
        now_ms = int(now.timestamp() * 1000)
        zone = zone_key(zone)
        today = zone_today(now, zone)
        d = parse_day(day, today)
        is_today = d == today
        cfg = self.config.get()
        z = zone_day(d, zone)
        lz = zone_day(d - timedelta(days=7), zone)
        start, end, n = z.start, z.end, z.minutes
        shift = start - lz.start                         # a week, give or take a daylight-saving hour
        con = _db()
        try:
            paths = self.metrics.paths(lz.start, lz.end, now_ms) + self.metrics.paths(start, end, now_ms)
            _load_metrics(con, paths, [(lz.start, lz.end), (start, end)])
            as_of = self._api_as_of(con, z, now_ms)
            upto = (as_of + MIN) if as_of is not None else start
            per_min = con.execute("SELECT t, sum(calls), sum(e4), sum(e5) FROM m WHERE t >= ? AND t < ? GROUP BY t",
                                  [start, end]).fetchall()
            lw_min = con.execute("SELECT t, sum(calls) FROM m WHERE t >= ? AND t < ? GROUP BY t",
                                 [lz.start, lz.end]).fetchall()
            day_rows = self._stage_rows(con, start, upto)
            lw_rows = self._stage_rows(con, lz.start, upto - shift)
            w0 = max(start, upto - window_minutes * MIN)
            win = self._stage_rows(con, w0, upto)
            lw_win = self._stage_rows(con, w0 - shift, upto - shift)
            lw_have = con.execute("SELECT count(*) FROM m WHERE t >= ? AND t < ?", [lz.start, lz.end]).fetchone()[0]
            q15 = 15 * MIN
            spark_rows = con.execute("SELECT api, stage, (t - ?) // ?, sum(calls) FROM m WHERE t >= ? AND t < ? GROUP BY 1, 2, 3",
                                     [start, q15, start, end]).fetchall()
            spark_lw = con.execute("SELECT api, stage, (t - ?) // ?, sum(calls) FROM m WHERE t >= ? AND t < ? GROUP BY 1, 2, 3",
                                   [lz.start, q15, lz.start, lz.end]).fetchall()
        finally:
            con.close()
        n_shown = n if not is_today else (0 if as_of is None else (as_of - start) // MIN + 1)
        # 15-minute buckets for the small charts; today's bucket in progress is left out
        nb, nb_lw = -(-n // 15), -(-lz.minutes // 15)
        b_done = nb if not is_today else (0 if as_of is None else (as_of + MIN - start) // q15)
        sparks: dict[tuple[str, str], list] = {}
        for api_n, st_n, b, c in spark_rows:
            arr = sparks.setdefault((api_n, st_n), [0] * nb)
            if 0 <= b < nb:
                arr[int(b)] = int(c or 0)
        sparks_lw: dict[tuple[str, str], list] = {}
        for api_n, st_n, b, c in spark_lw:
            arr = sparks_lw.setdefault((api_n, st_n), [0] * nb_lw)
            if 0 <= b < nb_lw:
                arr[int(b)] = int(c or 0)
        calls = _minute_array([(t, c) for t, c, _, _ in per_min], start, n)
        e4 = _minute_array([(t, v) for t, _, v, _ in per_min], start, n)
        e5 = _minute_array([(t, v) for t, _, _, v in per_min], start, n)
        lastweek = _minute_array(lw_min, lz.start, lz.minutes) if lw_have else None
        cut = lambda arr: [v if i < n_shown else None for i, v in enumerate(arr)]  # noqa: E731
        stages = []
        for key in sorted(set(day_rows) | set(win)):
            api, stage = key
            dr = day_rows.get(key) or {"calls": 0, "err4xx": 0, "err5xx": 0}
            lw = lw_rows.get(key) if lw_have else None
            w = win.get(key) or {"calls": 0, "err4xx": 0, "err5xx": 0, "p95": None}
            lww = lw_win.get(key) if lw_have else None
            status, reasons = judge(w, lww, cfg.status)
            stages.append({
                "id": f"{api}/{stage}", "api": api, "stage": stage, "name": cfg.name(stage),
                "calls": dr["calls"], "callsLastWeek": lw["calls"] if lw else (0 if lw_have else None),
                "change": _change(dr["calls"], lw["calls"] if lw else None),
                "err4xx": dr["err4xx"], "err5xx": dr["err5xx"],
                "err4xxPct": _pct(dr["err4xx"], dr["calls"]), "err5xxPct": _pct(dr["err5xx"], dr["calls"]),
                "recent": {"calls": w["calls"], "p95": w.get("p95"), "p95LastWeek": (lww or {}).get("p95"),
                           "err5xxPct": _pct(w["err5xx"], w["calls"]), "err4xxPct": _pct(w["err4xx"], w["calls"])},
                "status": status, "reasons": reasons,
                "spark": {"now": [v if i < b_done else None for i, v in enumerate(sparks.get(key) or [0] * nb)],
                          "lastWeek": (sparks_lw.get(key) or [0] * nb_lw) if lw_have else None},
            })
        stages.sort(key=lambda s: -s["calls"])
        total = sum(s["calls"] for s in stages)
        for st_row in stages:
            st_row["share"] = round(100.0 * st_row["calls"] / total, 2) if total else None
        peak_i = max(range(n_shown), key=lambda i: calls[i]) if n_shown else None
        lw_total_day = sum(lastweek) if lastweek else None
        total_lw = sum(r["calls"] for r in lw_rows.values()) if lw_have else None
        api = {
            **self._freshness(as_of, now_ms, is_today), "source": self.metrics.describe(),
            "windowMinutes": window_minutes,
            "calls": total, "callsLastWeek": total_lw, "change": _change(total, total_lw),
            "callsLastWeekDay": lw_total_day,
            "peak": {"t": start + peak_i * MIN, "calls": calls[peak_i]} if peak_i is not None and calls[peak_i] else None,
            "err4xx": sum(s["err4xx"] for s in stages), "err5xx": sum(s["err5xx"] for s in stages),
            "minutes": {"start": start, "step": MIN, "calls": cut(calls), "err4xx": cut(e4), "err5xx": cut(e5),
                        "lastWeek": lastweek},
            "stages": stages,
            "statusCounts": {k: sum(1 for s in stages if s["status"] == k) for k in ("red", "amber", "green", "idle")},
        }
        return {"day": d.isoformat(), "zone": zone, "dayStart": start, "dayEnd": end, "today": is_today,
                "generatedAt": now.isoformat(timespec="seconds"),
                "api": api, "orders": self._orders_summary(z, lz, is_today, now_ms, cfg)}

    def _orders_summary(self, z: ZoneDay, lz: ZoneDay, is_today: bool, now_ms: int, cfg: OpsConfig) -> dict:
        status = self.orders.read_status()
        base = {"lastRun": status.get("lastRun"), "lastOk": status.get("lastOk"), "running": bool(status.get("running")),
                "error": status.get("error"), "configError": self.config.error,
                "services": [], "stores": [], "total": None, "totalLastWeek": None, "change": None,
                "minutes": None, "asOf": None, "ready": False, "lastWeekReady": False}
        if not self.settings.ops_enabled:
            return base
        cur = self._orders_view(z, None, cfg, now_ms)
        if cur is None:
            return base
        start = z.start
        as_of = cur["newestMs"] if is_today else z.end - MIN
        cutoff = (as_of + MIN - (start - lz.start)) if as_of is not None else None
        lw = self._orders_view(lz, cutoff, cfg, now_ms) if cutoff is not None else None
        lw_full = self._orders_view(lz, None, cfg, now_ms)
        n = z.minutes
        n_h = -(-n // 60)
        n_shown = n if not is_today else (0 if as_of is None else (as_of - start) // MIN + 1)
        # whole hours only: the hour in progress would look like a drop at the end of the small charts
        h_shown = n_h if not is_today else (0 if as_of is None else (as_of + MIN - start) // 3600_000)
        hours = lambda arr: [v if i < h_shown else None for i, v in enumerate(arr)] if arr else None  # noqa: E731
        services = [{"service": s.name, "cluster": s.cluster, "count": cur["services"].get(s.name, 0),
                     "lastWeek": lw["services"].get(s.name, 0) if lw else None,
                     "hours": hours(cur["serviceHours"].get(s.name) or [0] * n_h),
                     "lastWeekHours": (lw_full["serviceHours"].get(s.name) or [0] * -(-lz.minutes // 60)) if lw_full else None}
                    for s in cfg.services]
        for s in services:
            s["change"] = _change(s["count"], s["lastWeek"])
        keys = set(cur["stores"]) | (set(lw["stores"]) if lw else set())
        stores = [{"service": k[0], "store": k[1], "name": cfg.name(k[1]), "count": cur["stores"].get(k, 0),
                   "lastWeek": lw["stores"].get(k, 0) if lw else None,
                   "hours": hours(cur["storeHours"].get(k) or [0] * n_h),
                   "lastWeekHours": (lw_full["storeHours"].get(k) or [0] * -(-lz.minutes // 60)) if lw_full else None} for k in keys]
        for s in stores:
            s["change"] = _change(s["count"], s["lastWeek"])
        stores.sort(key=lambda s: (-s["count"], s["name"]))
        return {**base, "ready": True, "lastWeekReady": lw is not None, "asOf": as_of,
                "total": cur["total"], "totalLastWeek": lw["total"] if lw else None,
                "totalLastWeekDay": lw_full["total"] if lw_full else None,
                "change": _change(cur["total"], lw["total"] if lw else None),
                "services": services, "stores": stores,
                "minutes": {"start": start, "step": MIN,
                            "counts": [v if i < n_shown else None for i, v in enumerate(cur["minutes"])],
                            "lastWeek": lw_full["minutes"] if lw_full else None}}

    def health(self, minutes: int = 60) -> dict:
        if not 5 <= minutes <= 24 * 60:
            raise bad_request("INVALID_WINDOW", "minutes must be between 5 and 1440")
        now = self.now()
        now_ms = int(now.timestamp() * 1000)
        cfg = self.config.get()
        con = _db()
        try:
            lo = now_ms - minutes * MIN - 2 * 3600_000     # enough to find the newest minute
            paths = self.metrics.paths(lo - WEEK, now_ms - WEEK, now_ms) + self.metrics.paths(lo, now_ms, now_ms)
            _load_metrics(con, paths, [(lo - WEEK, now_ms - WEEK + MIN), (lo, now_ms + MIN)])
            row = con.execute("SELECT max(t) FROM m WHERE t >= ?", [lo]).fetchone()
            as_of = int(row[0]) if row and row[0] is not None else None
            upto = (as_of + MIN) if as_of is not None else now_ms
            w0 = upto - minutes * MIN
            win = self._stage_rows(con, w0, upto)
            lw = self._stage_rows(con, w0 - WEEK, upto - WEEK)
            lw_have = con.execute("SELECT count(*) FROM m WHERE t < ?", [lo]).fetchone()[0] > 0
        finally:
            con.close()
        stages = []
        for key in sorted(set(win) | set(lw)):
            api, stage = key
            w = win.get(key) or {"calls": 0, "err4xx": 0, "err5xx": 0, "p50": None, "p95": None, "integrationP95": None}
            l = lw.get(key) if lw_have else None
            if w["calls"] == 0 and (not l or l["calls"] == 0):
                continue
            status, reasons = judge(w, l, cfg.status)
            stages.append({"id": f"{api}/{stage}", "api": api, "stage": stage, "name": cfg.name(stage),
                           "now": _shares(w), "lastWeek": _shares(l) if l else None,
                           "change": _change(w["calls"], l["calls"] if l else None),
                           "status": status, "reasons": reasons})
        rank = {"red": 0, "amber": 1, "green": 2, "idle": 3}
        stages.sort(key=lambda s: (rank[s["status"]], -s["now"]["calls"]))
        orders = self.orders.read_status()
        return {"minutes": minutes, "from": w0, "to": upto, "generatedAt": now.isoformat(timespec="seconds"),
                "lastWeekAvailable": lw_have, "api": self._freshness(as_of, now_ms, True), "stages": stages,
                "orders": {"lastRun": orders.get("lastRun"), "lastOk": orders.get("lastOk"), "error": orders.get("error"),
                           "running": bool(orders.get("running")), "configError": self.config.error,
                           "newestMs": (self._day_orders(now.date()) or {}).get("newestMs")},
                "thresholds": cfg.status}

    def history(self, day: str | None = None, stage: str | None = None, service: str | None = None,
                store: str | None = None, zone: str = "UTC") -> dict:
        now = self.now()
        now_ms = int(now.timestamp() * 1000)
        zone = zone_key(zone)
        today = zone_today(now, zone)
        d = parse_day(day, today)
        if d < today - timedelta(days=60):
            raise bad_request("INVALID_DAY", "History goes back 60 days at most")
        cfg = self.config.get()
        if service and not cfg.service(service):
            raise bad_request("UNKNOWN_SERVICE", f"No order service called '{service}'")
        z = zone_day(d, zone)
        lz = zone_day(d - timedelta(days=7), zone)
        start, n = z.start, z.minutes
        con = _db()
        try:
            paths = self.metrics.paths(lz.start, lz.end, now_ms) + self.metrics.paths(start, z.end, now_ms)
            _load_metrics(con, paths, [(lz.start, lz.end), (start, z.end)])
            all_stages = con.execute("SELECT api, stage, sum(calls) FROM m WHERE t >= ? GROUP BY 1, 2 ORDER BY 3 DESC",
                                     [start]).fetchall()
            filt, fp = "", []
            if stage:
                api_name, _, st_name = stage.rpartition("/")
                filt, fp = " AND api = ? AND stage = ?", [api_name, st_name]

            def series(zd: ZoneDay):
                return con.execute(
                    "SELECT t, sum(calls), sum(e4), sum(e5), "
                    "sum(p95 * calls) / nullif(sum(CASE WHEN p95 IS NOT NULL THEN calls END), 0), "
                    "sum(ip95 * calls) / nullif(sum(CASE WHEN ip95 IS NOT NULL THEN calls END), 0) "
                    f"FROM m WHERE t >= ? AND t < ?{filt} GROUP BY t", [zd.start, zd.end, *fp]).fetchall()
            cur = series(z)
            lw = series(lz)
        finally:
            con.close()

        def col(rows, i, rnd=False, zd: ZoneDay = z):
            return _minute_array([(r[0], (_r(r[i]) if rnd else r[i])) for r in rows], zd.start, zd.minutes)
        api = {"start": start, "step": MIN, "calls": col(cur, 1), "err4xx": col(cur, 2), "err5xx": col(cur, 3),
               "p95": col(cur, 4, True), "integrationP95": col(cur, 5, True),
               "lastWeek": {"calls": col(lw, 1, zd=lz), "err4xx": col(lw, 2, zd=lz), "err5xx": col(lw, 3, zd=lz),
                            "p95": col(lw, 4, True, lz)} if lw else None,
               "hasData": bool(cur), "shownMinutes": n}
        for arr in (api["p95"], api["integrationP95"]):
            for i, v in enumerate(arr):
                if v == 0:
                    arr[i] = None
        if api["lastWeek"]:
            api["lastWeek"]["p95"] = [v or None for v in api["lastWeek"]["p95"]]
        o = self._orders_view(z, None, cfg, now_ms, service, store)
        olw = self._orders_view(lz, None, cfg, now_ms, service, store)
        all_orders = self._orders_view(z, None, cfg, now_ms)
        if d == today:
            # today: the minutes not copied / counted yet are empty, not 0
            api_n = 0 if not cur else (max(int(r[0]) for r in cur) - start) // MIN + 1
            for k in ("calls", "err4xx", "err5xx", "p95", "integrationP95"):
                api[k] = [v if i < api_n else None for i, v in enumerate(api[k])]
            api["shownMinutes"] = api_n
            if o:
                o_n = 0 if not o["newestMs"] else (o["newestMs"] - start) // MIN + 1
                o["minutes"] = [v if i < o_n else None for i, v in enumerate(o["minutes"])]
        return {
            "day": d.isoformat(), "zone": zone, "dayStart": start, "dayEnd": z.end, "stage": stage, "service": service, "store": store,
            "api": api,
            "orders": {"counted": o is not None, "minutes": o["minutes"] if o else None, "total": o["total"] if o else None,
                       "lastWeek": olw["minutes"] if olw else None, "totalLastWeek": olw["total"] if olw else None},
            "stages": [{"id": f"{a}/{s}", "api": a, "stage": s, "name": cfg.name(s), "calls": int(c or 0)}
                       for a, s, c in all_stages],
            "services": [{"service": s.name, "cluster": s.cluster} for s in cfg.services],
            "stores": sorted(({"service": k[0], "store": k[1], "name": cfg.name(k[1]), "count": c}
                              for k, c in (all_orders or {"stores": {}})["stores"].items()), key=lambda s: -s["count"]),
        }

    # -- any time range (History tab)
    def time_range(self, start_ms: int, end_ms: int, zone: str = "UTC", stage: str | None = None,
              service: str | None = None, store: str | None = None) -> dict:
        """Every stage's calls, errors and latency between two times, the totals over time in buckets
        that fit the range, and the orders first seen in it. At most RANGE_MAX_DAYS long."""
        now = self.now()
        now_ms = int(now.timestamp() * 1000)
        zone = zone_key(zone)
        start, end = int(start_ms) // MIN * MIN, -(-int(end_ms) // MIN) * MIN
        end = min(end, -(-now_ms // MIN) * MIN)
        if end <= start:
            raise bad_request("INVALID_RANGE", "The end must be after the start (and not in the future)")
        if end - start > RANGE_MAX_DAYS * DAY:
            raise bad_request("RANGE_TOO_LARGE", f"Pick at most {RANGE_MAX_DAYS} days")
        cfg = self.config.get()
        if service and not cfg.service(service):
            raise bad_request("UNKNOWN_SERVICE", f"No order service called '{service}'")
        span = end - start
        step = next(st for lim, st in RANGE_STEPS if span <= lim)
        tz = ZoneInfo(ZONES[zone])
        off = int(datetime.fromtimestamp(start / 1000, tz).utcoffset().total_seconds() * 1000)
        first = (start + off) // step * step - off        # buckets on round times in the zone
        nb = -(-(end - first) // step)
        con = duckdb.connect(config={"threads": 2, "memory_limit": "512MB"})
        try:
            paths = self.metrics.paths(start, end, now_ms)
            rows = []
            if paths:
                rows = con.execute(
                    "SELECT api, stage, (t - ?) // ?, sum(calls), sum(e4), sum(e5), "
                    "sum(p50 * calls) FILTER (WHERE p50 IS NOT NULL), sum(calls) FILTER (WHERE p50 IS NOT NULL), "
                    "sum(p95 * calls) FILTER (WHERE p95 IS NOT NULL), sum(calls) FILTER (WHERE p95 IS NOT NULL), "
                    "sum(ip95 * calls) FILTER (WHERE ip95 IS NOT NULL), sum(calls) FILTER (WHERE ip95 IS NOT NULL), max(t) "
                    "FROM (SELECT epoch_ms(strptime(minute, '%Y-%m-%dT%H:%MZ')) AS t, api, stage, coalesce(calls, 0) AS calls, "
                    "coalesce(err_4xx, 0) AS e4, coalesce(err_5xx, 0) AS e5, latency_p50_ms AS p50, latency_p95_ms AS p95, "
                    f"integration_p95_ms AS ip95 FROM read_csv(?, header=true, columns={CSV_COLUMNS}, compression='gzip')) "
                    "WHERE t >= ? AND t < ? GROUP BY 1, 2, 3", [first, step, paths, start, end]).fetchall()
        finally:
            con.close()
        # per stage (whole range) and per bucket (all stages, or the chosen one)
        keys = ("calls", "e4", "e5", "p50w", "p50n", "p95w", "p95n", "ip95w", "ip95n")
        per_stage: dict[tuple[str, str], list[float]] = {}
        series = [[0.0] * len(keys) for _ in range(nb)]
        as_of = None
        want = stage.rpartition("/")[::2] if stage else None
        for api_n, st_n, b, *vals in rows:
            newest = vals.pop()
            vals = [float(v or 0) for v in vals]
            acc = per_stage.setdefault((api_n, st_n), [0.0] * len(keys))
            for i, v in enumerate(vals):
                acc[i] += v
            as_of = newest if as_of is None or newest > as_of else as_of
            if (want is None or (api_n, st_n) == want) and 0 <= b < nb:
                for i, v in enumerate(vals):
                    series[int(b)][i] += v
        w = lambda a, i: _r(a[i] / a[i + 1]) if a[i + 1] else None  # noqa: E731
        total = sum(a[0] for a in per_stage.values())
        stages = []
        for (api_n, st_n), a in per_stage.items():
            calls = int(a[0])
            p95, ip95 = w(a, 5), w(a, 7)
            stages.append({"id": f"{api_n}/{st_n}", "api": api_n, "stage": st_n, "name": cfg.name(st_n), "calls": calls,
                           "share": round(100.0 * calls / total, 2) if total else None,
                           "err4xx": int(a[1]), "err5xx": int(a[2]), "err4xxPct": _pct(a[1], calls), "err5xxPct": _pct(a[2], calls),
                           "p50": w(a, 3), "p95": p95, "integrationP95": ip95,
                           "gatewayMs": round(p95 - ip95, 1) if p95 is not None and ip95 is not None else None})
        stages.sort(key=lambda r: -r["calls"])
        last_b = None if as_of is None else (as_of - first) // step
        # sums of a bucket the range only partly covers (first / last) would look like a drop: left out
        whole = lambda b, upto: (first + b * step >= start) and (first + (b + 1) * step <= upto)  # noqa: E731
        known = lambda b: last_b is not None and b <= last_b and (step == MIN or whole(b, min(end, as_of + MIN)))  # noqa: E731
        known_avg = lambda b: last_b is not None and b <= last_b  # noqa: E731
        api = {
            "hasData": bool(rows), "asOf": as_of,
            "calls": int(sum(r[0] for r in series)), "err4xx": int(sum(r[1] for r in series)), "err5xx": int(sum(r[2] for r in series)),
            "p95": _r(sum(r[5] for r in series) / sum(r[6] for r in series)) if sum(r[6] for r in series) else None,
            "series": {
                "calls": [int(r[0]) if known(b) else None for b, r in enumerate(series)],
                "err4xx": [int(r[1]) if known(b) else None for b, r in enumerate(series)],
                "err5xx": [int(r[2]) if known(b) else None for b, r in enumerate(series)],
                "p95": [w(r, 5) if known_avg(b) else None for b, r in enumerate(series)],
                "integrationP95": [w(r, 7) if known_avg(b) else None for b, r in enumerate(series)],
            },
        }
        return {"from": start, "to": end, "zone": zone, "step": step, "start": first, "buckets": nb,
                "generatedAt": now.isoformat(timespec="seconds"), "stage": stage, "service": service, "store": store,
                "api": api, "stages": stages, "orders": self._range_orders(start, end, first, step, nb, cfg, now_ms, service, store),
                "services": [{"service": s.name, "cluster": s.cluster} for s in cfg.services]}

    def _range_orders(self, start: int, end: int, first: int, step: int, nb: int, cfg: OpsConfig, now_ms: int,
                      service: str | None, store: str | None) -> dict:
        """Orders first seen in [start, end): the UTC days it covers joined like a zone day."""
        d0 = datetime.fromtimestamp(start / 1000, timezone.utc).date()
        d1 = datetime.fromtimestamp((end - 1) / 1000, timezone.utc).date()
        today_utc = datetime.fromtimestamp(now_ms / 1000, timezone.utc).date()
        merged: dict[str, dict[str, dict[str, int]]] = {}
        missing, newest = [], None
        d = d0
        while d <= d1:
            st = self._day_orders(d)
            if st is None:
                if d != today_utc:
                    missing.append(d.isoformat())
            else:
                if st.get("newestMs") and (newest is None or st["newestMs"] > newest):
                    newest = st["newestMs"]
                for name, svc in st["services"].items():
                    for s_name, ids in svc["orders"].items():
                        tgt = merged.setdefault(name, {}).setdefault(s_name, {})
                        for oid, ms in ids.items():
                            if oid not in tgt or ms < tgt[oid]:
                                tgt[oid] = ms
            d += timedelta(days=1)
        counted = len(missing) < (d1 - d0).days + 1
        series = [0] * nb
        services, stores = [], {}
        for svc in cfg.services:
            n = 0
            for s_name, ids in merged.get(svc.name, {}).items():
                c = 0
                for ms in ids.values():
                    if start <= ms < end:
                        c += 1
                        if (not service or service == svc.name) and (not store or store == s_name):
                            b = (ms - first) // step
                            if 0 <= b < nb:
                                series[b] += 1
                if c:
                    stores[(svc.name, s_name)] = c
                n += c
            services.append({"service": svc.name, "cluster": svc.cluster, "count": n})
        upto = None if newest is None else min(newest + MIN, end)

        def shown(b: int) -> bool:          # whole buckets only (see time_range)
            if upto is None:
                return False
            a = first + b * step
            return a < upto and (step == MIN or (a >= start and a + step <= upto))
        total = sum(c for (sv, st_n), c in stores.items() if (not service or sv == service) and (not store or st_n == store))
        return {"counted": counted, "missingDays": missing, "keptDays": KEEP_DAYS, "asOf": newest, "total": total if counted else None,
                "services": services, "series": [v if shown(b) else None for b, v in enumerate(series)] if counted else None,
                "stores": sorted(({"service": k[0], "store": k[1], "name": cfg.name(k[1]), "count": c} for k, c in stores.items()),
                                 key=lambda r: -r["count"])}

    def status(self) -> dict:
        cfg = self.config.get()
        return {"metrics": {"source": self.metrics.describe(), "error": self.metrics.error},
                "orders": {**self.orders.read_status(), "configError": self.config.error,
                           "services": [{"service": s.name, "cluster": s.cluster} for s in cfg.services]},
                "config": {"path": self.config.path, "exists": self.config.exists, "error": self.config.error}}


def _r(v) -> float | None:
    return None if v is None else round(float(v), 1)


def _shares(w: dict) -> dict:
    c = w["calls"]
    return {**w, "err4xxPct": _pct(w["err4xx"], c), "err5xxPct": _pct(w["err5xx"], c),
            "gatewayMs": round(w["p95"] - w["integrationP95"], 1) if w.get("p95") is not None and w.get("integrationP95") is not None else None}
