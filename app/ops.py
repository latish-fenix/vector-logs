"""Ops dashboard: API Gateway calls per stage and orders processed (OMS / CRS), today against the
same day last week. Times and "today" are UTC.

Where the numbers come from
    API Gateway  s3://LOGS_BUCKET/OPS_METRICS_PREFIX dt=YYYY-MM-DD/part-HH.csv.gz, one row per stage per
                 minute, written every 5 minutes by scripts/apigw_metrics_to_s3.py on another server
                 (6 files a day; HH = 00, 04, … 20; the newest file is rewritten until its 4 hours end).
    Orders       the Vector logs of the clusters in config/ops-dashboard.yaml, counted here: one unique
                 (store, order id) per service per UTC day, at the minute it is first seen.

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

    def _orders_view(self, d: date, cutoff_ms: int | None, cfg: OpsConfig, service: str | None = None,
                     store: str | None = None) -> dict | None:
        """Per minute, per service and per store counts of one day (only orders first seen before
        cutoff_ms when given). None when the day has not been counted yet."""
        st = self._day_orders(d)
        if st is None:
            return None
        start = day_start_ms(d)
        minutes = [0] * 1440
        services, stores = {}, {}
        svc_hours: dict[str, list[int]] = {}
        store_hours: dict[tuple[str, str], list[int]] = {}
        for name in [s.name for s in cfg.services]:
            svc = st["services"].get(name)
            if svc is None or (service and name != service):
                continue
            n = 0
            sh = svc_hours[name] = [0] * 24
            for s, ids in svc["orders"].items():
                if store and s != store:
                    continue
                c = 0
                hh = [0] * 24
                for ms in ids.values():
                    if cutoff_ms is not None and ms >= cutoff_ms:
                        continue
                    i = (ms - start) // MIN
                    if 0 <= i < 1440:
                        minutes[i] += 1
                        hh[i // 60] += 1
                        sh[i // 60] += 1
                    c += 1
                if c:
                    stores[(name, s)] = c
                    store_hours[(name, s)] = hh
                n += c
            services[name] = n
        return {"minutes": minutes, "services": services, "stores": stores, "total": sum(services.values()),
                "serviceHours": svc_hours, "storeHours": store_hours,
                "newestMs": st.get("newestMs"), "final": st.get("final"), "updated": st.get("updated")}

    # -- API metrics (read side)
    def _api_as_of(self, con, d: date, now_ms: int) -> int | None:
        start = day_start_ms(d)
        if now_ms >= start + DAY + SETTLE_MS:
            return start + DAY - MIN                      # a past day: all of it
        row = con.execute("SELECT max(t) FROM m WHERE t >= ? AND t < ?", [start, start + DAY]).fetchone()
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

    def summary(self, day: str | None = None, window_minutes: int = 60) -> dict:
        now = self.now()
        now_ms = int(now.timestamp() * 1000)
        d = parse_day(day, now.date())
        is_today = d == now.date()
        cfg = self.config.get()
        start = day_start_ms(d)
        con = _db()
        try:
            paths = self.metrics.paths(start - WEEK, start - WEEK + DAY, now_ms) + self.metrics.paths(start, start + DAY, now_ms)
            _load_metrics(con, paths, [(start - WEEK, start - WEEK + DAY), (start, start + DAY)])
            as_of = self._api_as_of(con, d, now_ms)
            upto = (as_of + MIN) if as_of is not None else start
            per_min = con.execute("SELECT t, sum(calls), sum(e4), sum(e5) FROM m WHERE t >= ? AND t < ? GROUP BY t",
                                  [start, start + DAY]).fetchall()
            lw_min = con.execute("SELECT t + ?, sum(calls) FROM m WHERE t >= ? AND t < ? GROUP BY t",
                                 [WEEK, start - WEEK, start - WEEK + DAY]).fetchall()
            day_rows = self._stage_rows(con, start, upto)
            lw_rows = self._stage_rows(con, start - WEEK, upto - WEEK)
            w0 = max(start, upto - window_minutes * MIN)
            win = self._stage_rows(con, w0, upto)
            lw_win = self._stage_rows(con, w0 - WEEK, upto - WEEK)
            lw_have = con.execute("SELECT count(*) FROM m WHERE t >= ? AND t < ?", [start - WEEK, start - WEEK + DAY]).fetchone()[0]
            q15 = 15 * MIN
            spark_rows = con.execute("SELECT api, stage, (t - ?) // ?, sum(calls) FROM m WHERE t >= ? AND t < ? GROUP BY 1, 2, 3",
                                     [start, q15, start, start + DAY]).fetchall()
            spark_lw = con.execute("SELECT api, stage, (t - ?) // ?, sum(calls) FROM m WHERE t >= ? AND t < ? GROUP BY 1, 2, 3",
                                   [start - WEEK, q15, start - WEEK, start - WEEK + DAY]).fetchall()
        finally:
            con.close()
        n_shown = 1440 if not is_today else (0 if as_of is None else (as_of - start) // MIN + 1)
        # 15-minute buckets for the small charts; today's bucket in progress is left out
        b_done = 96 if not is_today else (0 if as_of is None else (as_of + MIN - start) // q15)
        sparks: dict[tuple[str, str], list] = {}
        for api_n, st_n, b, c in spark_rows:
            arr = sparks.setdefault((api_n, st_n), [0] * 96)
            if 0 <= b < 96:
                arr[int(b)] = int(c or 0)
        sparks_lw: dict[tuple[str, str], list] = {}
        for api_n, st_n, b, c in spark_lw:
            arr = sparks_lw.setdefault((api_n, st_n), [0] * 96)
            if 0 <= b < 96:
                arr[int(b)] = int(c or 0)
        calls = _minute_array([(t, c) for t, c, _, _ in per_min], start)
        e4 = _minute_array([(t, v) for t, _, v, _ in per_min], start)
        e5 = _minute_array([(t, v) for t, _, _, v in per_min], start)
        lastweek = _minute_array(lw_min, start) if lw_have else None
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
                "spark": {"now": [v if i < b_done else None for i, v in enumerate(sparks.get(key) or [0] * 96)],
                          "lastWeek": (sparks_lw.get(key) or [0] * 96) if lw_have else None},
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
        return {"day": d.isoformat(), "today": is_today, "generatedAt": now.isoformat(timespec="seconds"),
                "api": api, "orders": self._orders_summary(d, is_today, now_ms, cfg)}

    def _orders_summary(self, d: date, is_today: bool, now_ms: int, cfg: OpsConfig) -> dict:
        status = self.orders.read_status()
        base = {"lastRun": status.get("lastRun"), "lastOk": status.get("lastOk"), "running": bool(status.get("running")),
                "error": status.get("error"), "configError": self.config.error,
                "services": [], "stores": [], "total": None, "totalLastWeek": None, "change": None,
                "minutes": None, "asOf": None, "ready": False, "lastWeekReady": False}
        if not self.settings.ops_enabled:
            return base
        cur = self._orders_view(d, None, cfg)
        if cur is None:
            return base
        as_of = cur["newestMs"] if is_today else day_start_ms(d) + DAY - MIN
        cutoff = (as_of + MIN - WEEK) if as_of is not None else None
        lw = self._orders_view(d - timedelta(days=7), cutoff, cfg) if cutoff is not None else None
        lw_full = self._orders_view(d - timedelta(days=7), None, cfg)
        start = day_start_ms(d)
        n_shown = 1440 if not is_today else (0 if as_of is None else (as_of - start) // MIN + 1)
        # whole hours only: the hour in progress would look like a drop at the end of the small charts
        h_shown = 24 if not is_today else (0 if as_of is None else (as_of + MIN - start) // 3600_000)
        hours = lambda arr: [v if i < h_shown else None for i, v in enumerate(arr)] if arr else None  # noqa: E731
        services = [{"service": s.name, "cluster": s.cluster, "count": cur["services"].get(s.name, 0),
                     "lastWeek": lw["services"].get(s.name, 0) if lw else None,
                     "hours": hours(cur["serviceHours"].get(s.name) or [0] * 24),
                     "lastWeekHours": (lw_full["serviceHours"].get(s.name) or [0] * 24) if lw_full else None}
                    for s in cfg.services]
        for s in services:
            s["change"] = _change(s["count"], s["lastWeek"])
        keys = set(cur["stores"]) | (set(lw["stores"]) if lw else set())
        stores = [{"service": k[0], "store": k[1], "name": cfg.name(k[1]), "count": cur["stores"].get(k, 0),
                   "lastWeek": lw["stores"].get(k, 0) if lw else None,
                   "hours": hours(cur["storeHours"].get(k) or [0] * 24),
                   "lastWeekHours": (lw_full["storeHours"].get(k) or [0] * 24) if lw_full else None} for k in keys]
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
                store: str | None = None) -> dict:
        now = self.now()
        now_ms = int(now.timestamp() * 1000)
        d = parse_day(day, now.date())
        if d < now.date() - timedelta(days=60):
            raise bad_request("INVALID_DAY", "History goes back 60 days at most")
        cfg = self.config.get()
        if service and not cfg.service(service):
            raise bad_request("UNKNOWN_SERVICE", f"No order service called '{service}'")
        start = day_start_ms(d)
        con = _db()
        try:
            paths = self.metrics.paths(start - WEEK, start - WEEK + DAY, now_ms) + self.metrics.paths(start, start + DAY, now_ms)
            _load_metrics(con, paths, [(start - WEEK, start - WEEK + DAY), (start, start + DAY)])
            all_stages = con.execute("SELECT api, stage, sum(calls) FROM m WHERE t >= ? GROUP BY 1, 2 ORDER BY 3 DESC",
                                     [start]).fetchall()
            filt, fp = "", []
            if stage:
                api_name, _, st_name = stage.rpartition("/")
                filt, fp = " AND api = ? AND stage = ?", [api_name, st_name]

            def series(a: int, shift: int):
                return con.execute(
                    "SELECT t + ?, sum(calls), sum(e4), sum(e5), "
                    "sum(p95 * calls) / nullif(sum(CASE WHEN p95 IS NOT NULL THEN calls END), 0), "
                    "sum(ip95 * calls) / nullif(sum(CASE WHEN ip95 IS NOT NULL THEN calls END), 0) "
                    f"FROM m WHERE t >= ? AND t < ?{filt} GROUP BY t", [shift, a, a + DAY, *fp]).fetchall()
            cur = series(start, 0)
            lw = series(start - WEEK, WEEK)
        finally:
            con.close()
        col = lambda rows, i, rnd=False: _minute_array([(r[0], (_r(r[i]) if rnd else r[i])) for r in rows], start)  # noqa: E731
        api = {"start": start, "step": MIN, "calls": col(cur, 1), "err4xx": col(cur, 2), "err5xx": col(cur, 3),
               "p95": col(cur, 4, True), "integrationP95": col(cur, 5, True),
               "lastWeek": {"calls": col(lw, 1), "err4xx": col(lw, 2), "err5xx": col(lw, 3), "p95": col(lw, 4, True)} if lw else None,
               "hasData": bool(cur), "shownMinutes": 1440}
        for arr in (api["p95"], api["integrationP95"]):
            for i, v in enumerate(arr):
                if v == 0:
                    arr[i] = None
        if api["lastWeek"]:
            api["lastWeek"]["p95"] = [v or None for v in api["lastWeek"]["p95"]]
        o = self._orders_view(d, None, cfg, service, store)
        olw = self._orders_view(d - timedelta(days=7), None, cfg, service, store)
        all_orders = self._orders_view(d, None, cfg)
        if d == now.date():
            # today: the minutes not copied / counted yet are empty, not 0
            api_n = 0 if not cur else (max(int(r[0]) for r in cur) - start) // MIN + 1
            for k in ("calls", "err4xx", "err5xx", "p95", "integrationP95"):
                api[k] = [v if i < api_n else None for i, v in enumerate(api[k])]
            api["shownMinutes"] = api_n
            if o:
                o_n = 0 if not o["newestMs"] else (o["newestMs"] - start) // MIN + 1
                o["minutes"] = [v if i < o_n else None for i, v in enumerate(o["minutes"])]
        return {
            "day": d.isoformat(), "stage": stage, "service": service, "store": store,
            "api": api,
            "orders": {"counted": o is not None, "minutes": o["minutes"] if o else None, "total": o["total"] if o else None,
                       "lastWeek": olw["minutes"] if olw else None, "totalLastWeek": olw["total"] if olw else None},
            "stages": [{"id": f"{a}/{s}", "api": a, "stage": s, "name": cfg.name(s), "calls": int(c or 0)}
                       for a, s, c in all_stages],
            "services": [{"service": s.name, "cluster": s.cluster} for s in cfg.services],
            "stores": sorted(({"service": k[0], "store": k[1], "name": cfg.name(k[1]), "count": c}
                              for k, c in (all_orders or {"stores": {}})["stores"].items()), key=lambda s: -s["count"]),
        }

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
