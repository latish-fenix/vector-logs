"""Reading the Vector Parquet logs from S3 and searching them with DuckDB.

Layout written by Vector (see the aws_s3 sink in vector.yaml):

    s3://<LOGS_BUCKET>/<LOGS_PREFIX><cluster>/dt=YYYY-MM-DD/hour=HH/<epoch>-<uuid>.parquet

The folders are by event time (UTC), so a search lists only the hour folders its time range
touches. Each file is written once and never changed, so files are downloaded once into a
local cache (CACHE_DIR, size-capped) and every later search, page, histogram or export
reads the local copy. DuckDB then runs the query over the local files.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import boto3
import duckdb
from botocore.config import Config
from botocore.exceptions import ClientError, EndpointConnectionError

from .errors import ApiError, bad_request, not_found
from .repos import CLUSTER_RE
from .settings import Settings

log = logging.getLogger("vector_logs.logs")

KEY_RE = re.compile(r"^[A-Za-z0-9._=/+-]{1,1024}$")
HOUR = 3600_000

# Columns searched by free text (whichever exist in the files).
TEXT_COLUMNS = ["msg", "body", "raw", "exception", "error_message", "error_code", "error_identifier",
                "class", "method", "logger", "kv_json", "request_uuid", "tenant_id", "web_id", "carrier"]
# Columns counted for the "top values" lists next to the results.
FACET_COLUMNS = ["level", "service", "host", "exception"]
# Preferred column order in results; anything else follows in file order.
PREFERRED_ORDER = ["log_time", "level", "service", "msg", "exception", "error_code", "error_message",
                   "status_code", "class", "method", "line", "logger", "host", "instance_id",
                   "container_id", "app", "tenant_id", "request_uuid", "response_time_ms", "body",
                   "raw", "format", "parse_ok", "cluster", "source_file", "log_time_ms", "ingest_time_ms"]
RESULT_TTL = 600             # seconds a search result (the list of matching lines) is reused
LIST_TRUNCATE = 1500          # characters per field in the result list (the detail view has all)
HISTOGRAM_STEPS = [60_000, 120_000, 300_000, 600_000, 900_000, 1_800_000, HOUR, 2 * HOUR, 3 * HOUR,
                   6 * HOUR, 12 * HOUR, 24 * HOUR]
FILTER_OPS = ("is", "is_not", "one_of", "not_one_of", "contains", "not_contains", "exists",
              "not_exists", "gte", "lte", "between")
NUMERIC_TYPES = ("BIGINT", "INTEGER", "SMALLINT", "TINYINT", "HUGEINT", "DOUBLE", "FLOAT", "DECIMAL",
                 "UBIGINT", "UINTEGER")


# ------------------------------------------------------------------ time range
_REL_RE = re.compile(r"^now(?:-(\d+)([smhdw]))?$")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_time(value: str | int | float | None, now_ms: int, name: str) -> int:
    """'now', 'now-15m', 'now-7d', ISO 8601 (Z or offset; no offset = UTC) or epoch ms."""
    if value is None or value == "":
        raise bad_request("INVALID_TIME", f"'{name}' is required")
    if isinstance(value, (int, float)):
        return int(value)
    v = str(value).strip()
    m = _REL_RE.match(v)
    if m:
        n, unit = m.groups()
        return now_ms - (int(n) * _UNITS[unit] * 1000 if n else 0)
    if v.isdigit():
        return int(v)
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        raise bad_request("INVALID_TIME", f"'{name}' must be 'now', 'now-15m', an ISO date-time or "
                          f"epoch milliseconds, not {v!r}") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def hours_between(start_ms: int, end_ms: int) -> list[datetime]:
    """UTC hour starts whose folder can hold events in [start_ms, end_ms]."""
    t = datetime.fromtimestamp(start_ms / 1000, timezone.utc).replace(minute=0, second=0, microsecond=0)
    end = datetime.fromtimestamp(end_ms / 1000, timezone.utc)
    out = []
    while t <= end:
        out.append(t)
        t += timedelta(hours=1)
    return out


def histogram_step(span_ms: int, buckets: int = 60) -> int:
    for step in HISTOGRAM_STEPS:
        if span_ms / step <= buckets:
            return step
    return HISTOGRAM_STEPS[-1]


# ------------------------------------------------------------------ query model
@dataclass
class Filter:
    field: str
    op: str
    value: Any = None
    values: list | None = None
    gte: Any = None
    lte: Any = None


@dataclass
class SearchSpec:
    start: Any
    end: Any
    query: str = ""
    filters: list[Filter] | None = None
    order: str = "desc"


def tokenize(query: str) -> list[str]:
    """Words and "quoted phrases"; a leading '-' excludes."""
    return [m.group(0) for m in re.finditer(r'-?"[^"]*"|\S+', query or "")]


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


class WhereBuilder:
    """Turns the time range, free-text query and filters into SQL with ? parameters.
    Field names are checked against the columns that exist in the files."""

    def __init__(self, columns: dict[str, str]):
        self.columns = columns
        self.sql: list[str] = []
        self.params: list[Any] = []

    def _col(self, field: str) -> str:
        if field not in self.columns:
            raise bad_request("UNKNOWN_FIELD", f"There is no column '{field}' in these logs",
                              {"field": field, "columns": sorted(self.columns)})
        return quote_ident(field)

    def _numeric(self, field: str) -> bool:
        t = self.columns.get(field, "").upper()
        return any(t.startswith(n) for n in NUMERIC_TYPES)

    def _value(self, field: str, value: Any) -> Any:
        t = self.columns.get(field, "").upper()
        if self._numeric(field):
            try:
                return float(value) if any(t.startswith(x) for x in ("DOUBLE", "FLOAT", "DECIMAL")) else int(str(value).strip())
            except (TypeError, ValueError):
                raise bad_request("INVALID_FILTER", f"'{field}' holds numbers; {value!r} is not a number") from None
        if t == "BOOLEAN":
            s = str(value).strip().lower()
            if s not in ("true", "false"):
                raise bad_request("INVALID_FILTER", f"'{field}' is true or false")
            return s == "true"
        return str(value)

    def time(self, start_ms: int, end_ms: int) -> None:
        if "log_time_ms" in self.columns:
            self.sql.append('"log_time_ms" BETWEEN ? AND ?')
            self.params += [start_ms, end_ms]

    def text(self, query: str) -> None:
        cols = [c for c in TEXT_COLUMNS if c in self.columns and not self._numeric(c)]
        for token in tokenize(query):
            neg = token.startswith("-") and len(token) > 1
            if neg:
                token = token[1:]
            if token.startswith('"') and token.endswith('"') and len(token) >= 2:
                token = token[1:-1]
                field = None
            else:
                field, _, val = token.partition(":")
                if val and field in self.columns:
                    self._field_token(field, val, neg)
                    continue
                field = None
            if not token:
                continue
            if not cols:
                raise bad_request("NO_TEXT_COLUMNS", "These logs have no text columns to search")
            # one ILIKE per column (OR): no big concatenated strings, so memory stays flat
            pattern = f"%{_like_escape(token)}%"
            any_col = " OR ".join(f"coalesce({quote_ident(c)} ILIKE ? ESCAPE '\\', false)" for c in cols)
            self.sql.append(f"{'NOT ' if neg else ''}({any_col})")
            self.params.extend([pattern] * len(cols))

    def _field_token(self, field: str, val: str, neg: bool) -> None:
        col = self._col(field)
        if val.startswith('"') and val.endswith('"') and len(val) >= 2:
            val = val[1:-1]
        if self._numeric(field) or self.columns[field].upper() == "BOOLEAN":
            self.sql.append(f"{'NOT ' if neg else ''}coalesce({col} = ?, false)")
            self.params.append(self._value(field, val))
            return
        pattern = _like_escape(val).replace("*", "%")
        self.sql.append(f"{'NOT ' if neg else ''}coalesce({col} ILIKE ? ESCAPE '\\', false)")
        self.params.append(pattern)

    def filter(self, f: Filter) -> None:
        if f.op not in FILTER_OPS:
            raise bad_request("INVALID_FILTER", f"Unknown filter operator '{f.op}'", {"ops": FILTER_OPS})
        col = self._col(f.field)
        if f.op == "exists":
            self.sql.append(f"{col} IS NOT NULL")
        elif f.op == "not_exists":
            self.sql.append(f"{col} IS NULL")
        elif f.op in ("is", "is_not"):
            if f.value is None:
                raise bad_request("INVALID_FILTER", f"'{f.field} {f.op}' needs a value")
            expr = f"coalesce({col} = ?, false)"
            self.sql.append(expr if f.op == "is" else f"NOT {expr}")
            self.params.append(self._value(f.field, f.value))
        elif f.op in ("one_of", "not_one_of"):
            vals = [self._value(f.field, v) for v in (f.values or [])]
            if not vals or len(vals) > 500:
                raise bad_request("INVALID_FILTER", f"'{f.field} {f.op}' needs 1 to 500 values")
            expr = f"coalesce(list_contains(?, {col}), false)"
            self.sql.append(expr if f.op == "one_of" else f"NOT {expr}")
            self.params.append(vals)
        elif f.op in ("contains", "not_contains"):
            if not str(f.value or ""):
                raise bad_request("INVALID_FILTER", f"'{f.field} {f.op}' needs a value")
            expr = f"coalesce(CAST({col} AS VARCHAR) ILIKE ? ESCAPE '\\', false)"
            self.sql.append(expr if f.op == "contains" else f"NOT {expr}")
            self.params.append(f"%{_like_escape(str(f.value))}%")
        else:  # gte / lte / between
            lo = f.gte if f.op in ("gte", "between") else None
            hi = f.lte if f.op in ("lte", "between") else None
            if f.op == "gte":
                lo = f.value if f.value is not None else f.gte
            if f.op == "lte":
                hi = f.value if f.value is not None else f.lte
            if lo in (None, "") and hi in (None, ""):
                raise bad_request("INVALID_FILTER", f"'{f.field} {f.op}' needs a value")
            if lo not in (None, ""):
                self.sql.append(f"{col} >= ?")
                self.params.append(self._value(f.field, lo))
            if hi not in (None, ""):
                self.sql.append(f"{col} <= ?")
                self.params.append(self._value(f.field, hi))

    def where(self) -> str:
        return " AND ".join(f"({s})" for s in self.sql) or "TRUE"


# ------------------------------------------------------------------ sources
class _TTLCache:
    def __init__(self):
        self._data: dict[Any, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key, ttl: float):
        with self._lock:
            hit = self._data.get(key)
            if hit and time.monotonic() - hit[0] < ttl:
                return hit[1]
        return None

    def set(self, key, value) -> None:
        with self._lock:
            if len(self._data) > 50_000:
                self._data.clear()
            self._data[key] = (time.monotonic(), value)


def _s3_error(e: Exception, what: str) -> ApiError:
    if isinstance(e, EndpointConnectionError):
        return ApiError(502, "LOGS_UNREACHABLE", f"Can't reach S3 to {what}: {e}")
    code = e.response["Error"]["Code"] if isinstance(e, ClientError) else type(e).__name__
    if code in ("AccessDenied", "AccessDeniedException", "403", "InvalidAccessKeyId",
                "ExpiredToken", "SignatureDoesNotMatch"):
        return ApiError(500, "LOGS_ACCESS_DENIED",
                        f"The server may not {what} in the logs bucket: add the read-only statements from "
                        "docs/iam-policy.json to the EC2 instance role", {"awsError": code})
    if code in ("NoSuchBucket",):
        return ApiError(502, "LOGS_BUCKET_MISSING", "The logs bucket (LOGS_BUCKET) does not exist",
                        {"awsError": code})
    return ApiError(502, "LOGS_UNAVAILABLE", f"S3 error while trying to {what}: {e}", {"awsError": code})


class S3LogSource:
    """The logs bucket, read-only: ListBucket under LOGS_PREFIX and GetObject."""

    def __init__(self, settings: Settings, client=None):
        self.bucket = settings.logs_bucket
        self.prefix = settings.logs_prefix
        self.s3 = client or boto3.client(
            "s3", region_name=settings.logs_region, endpoint_url=settings.logs_endpoint_url,
            config=Config(retries={"max_attempts": 5, "mode": "standard"},
                          max_pool_connections=max(10, settings.download_threads + 8)))
        self.cache_root = Path(settings.cache_dir) / self.bucket

    def describe(self) -> str:
        return f"s3://{self.bucket}/{self.prefix}"

    def list_clusters(self) -> list[str]:
        out: list[str] = []
        try:
            for page in self.s3.get_paginator("list_objects_v2").paginate(
                    Bucket=self.bucket, Prefix=self.prefix, Delimiter="/"):
                for cp in page.get("CommonPrefixes", []):
                    out.append(cp["Prefix"][len(self.prefix):].rstrip("/"))
        except (ClientError, EndpointConnectionError) as e:
            raise _s3_error(e, "list the cluster folders") from e
        return out

    def list_prefix(self, prefix: str) -> list[tuple[str, int]]:
        out: list[tuple[str, int]] = []
        try:
            for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
                for item in page.get("Contents", []):
                    if item["Key"].endswith(".parquet"):
                        out.append((item["Key"], int(item.get("Size", 0))))
        except (ClientError, EndpointConnectionError) as e:
            raise _s3_error(e, "list log files") from e
        return out

    def local_path(self, key: str) -> Path:
        return self.cache_root / key

    def fetch(self, key: str) -> tuple[Path, bool]:
        """(local path, was already cached)."""
        path = self.local_path(key)
        if path.exists():
            try:
                os.utime(path)  # mark as recently used (cache eviction is oldest-first)
            except OSError:
                pass
            return path, True
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.part")
        try:
            self.s3.download_file(self.bucket, key, str(tmp))
            os.replace(tmp, path)
        except (ClientError, EndpointConnectionError) as e:
            raise _s3_error(e, f"read {key}") from e
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
        return path, False

    def key_of(self, local: str) -> str:
        return Path(local).resolve().relative_to(self.cache_root.resolve()).as_posix()


class LocalLogSource:
    """A folder with the same layout as the bucket (development and tests)."""

    def __init__(self, settings: Settings):
        self.root = Path(settings.logs_local_dir)
        self.prefix = settings.logs_prefix
        self.cache_root = self.root

    def describe(self) -> str:
        return f"{self.root}/{self.prefix}"

    def list_clusters(self) -> list[str]:
        base = self.root / self.prefix
        return sorted(p.name for p in base.iterdir() if p.is_dir()) if base.exists() else []

    def list_prefix(self, prefix: str) -> list[tuple[str, int]]:
        base = self.root / prefix
        if not base.exists():
            return []
        return sorted((p.relative_to(self.root).as_posix(), p.stat().st_size)
                      for p in base.rglob("*.parquet") if p.is_file())

    def local_path(self, key: str) -> Path:
        return self.root / key

    def fetch(self, key: str) -> tuple[Path, bool]:
        path = self.root / key
        if not path.exists():
            raise not_found("LOG_FILE_NOT_FOUND", f"{key} does not exist")
        return path, True

    def key_of(self, local: str) -> str:
        return Path(local).resolve().relative_to(self.root.resolve()).as_posix()


def build_source(settings: Settings):
    return LocalLogSource(settings) if settings.logs_backend == "local" else S3LogSource(settings)


# ------------------------------------------------------------------ the service
class LogService:
    def __init__(self, settings: Settings, source=None):
        self.settings = settings
        self.source = source or build_source(settings)
        self.prefix = settings.logs_prefix
        self._lists = _TTLCache()
        self._clusters = _TTLCache()
        self._pool = ThreadPoolExecutor(max_workers=max(1, settings.download_threads),
                                        thread_name_prefix="fetch")
        self._list_pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="list")
        self._evict_lock = threading.Lock()
        self._last_evict = 0.0
        self.tmp_dir = Path(settings.cache_dir) / ".duckdb-tmp"
        self.results_dir = Path(settings.cache_dir) / ".results"
        self._schemas = _TTLCache()
        self._last_results_clean = 0.0

    # -- clusters
    def clusters(self, refresh: bool = False) -> list[str]:
        if not refresh:
            hit = self._clusters.get("all", self.settings.clusters_cache_seconds)
            if hit is not None:
                return hit
        names = sorted(n for n in self.source.list_clusters() if CLUSTER_RE.match(n))
        self._clusters.set("all", names)
        return names

    def check_cluster(self, cluster: str) -> None:
        if not CLUSTER_RE.match(cluster or ""):
            raise bad_request("INVALID_CLUSTER", f"'{cluster}' is not a valid cluster name")

    # -- files
    def _hour_prefix(self, cluster: str, hour: datetime) -> str:
        return f"{self.prefix}{cluster}/dt={hour:%Y-%m-%d}/hour={hour:%H}/"

    def _list_hour(self, cluster: str, hour: datetime, now: datetime) -> list[tuple[str, int]]:
        prefix = self._hour_prefix(cluster, hour)
        # Files for an hour keep arriving for a while (Vector writes a batch at least every
        # 5 minutes and buffers on disk if S3 is slow), so recent hours are re-listed often.
        age = now - (hour + timedelta(hours=1))
        ttl = self.settings.listing_cache_seconds if age < timedelta(hours=3) else 900
        hit = self._lists.get(prefix, ttl)
        if hit is not None:
            return hit
        items = self.source.list_prefix(prefix)
        self._lists.set(prefix, items)
        return items

    def files_for(self, cluster: str, start_ms: int, end_ms: int) -> list[tuple[str, int]]:
        now = datetime.now(timezone.utc)
        hours = [h for h in hours_between(start_ms, end_ms) if h <= now]
        lists = list(self._list_pool.map(lambda h: self._list_hour(cluster, h, now), hours))
        files = [f for items in lists for f in items]
        if len(files) > self.settings.max_files_per_search:
            raise bad_request("TOO_MANY_FILES",
                              f"This time range has {len(files):,} log files; one search reads at most "
                              f"{self.settings.max_files_per_search:,}. Pick a shorter range.",
                              {"files": len(files), "max": self.settings.max_files_per_search})
        return files

    def materialize(self, keys: list[str]) -> tuple[list[str], dict]:
        t0 = time.monotonic()
        results = list(self._pool.map(self.source.fetch, keys))
        stats = {"files": len(keys), "cached": sum(1 for _, c in results if c),
                 "downloadMs": int((time.monotonic() - t0) * 1000)}
        if stats["cached"] < len(keys):
            self._maybe_evict()
        return [str(p) for p, _ in results], stats

    def _maybe_evict(self) -> None:
        if not isinstance(self.source, S3LogSource):
            return
        if time.monotonic() - self._last_evict < 60 or not self._evict_lock.acquire(blocking=False):
            return
        try:
            self._last_evict = time.monotonic()
            limit = self.settings.cache_max_mb * 1024 * 1024
            entries, total = [], 0
            for dirpath, _, names in os.walk(self.source.cache_root):
                for n in names:
                    p = os.path.join(dirpath, n)
                    try:
                        st = os.stat(p)
                    except OSError:
                        continue
                    entries.append((st.st_mtime, st.st_size, p))
                    total += st.st_size
            if total <= limit:
                return
            entries.sort()
            keep_after = time.time() - 600  # never evict files used in the last 10 minutes
            target = int(limit * 0.8)
            removed = 0
            for mtime, size, p in entries:
                if total <= target or mtime > keep_after:
                    break
                try:
                    os.unlink(p)
                    total -= size
                    removed += 1
                except OSError:
                    pass
            log.info("cache eviction: removed %d files, %d MB left", removed, total // (1024 * 1024))
        finally:
            self._evict_lock.release()

    def cache_stats(self) -> dict:
        if not isinstance(self.source, S3LogSource):
            return {"backend": "local"}
        count = size = 0
        for dirpath, _, names in os.walk(self.source.cache_root):
            for n in names:
                try:
                    size += os.stat(os.path.join(dirpath, n)).st_size
                    count += 1
                except OSError:
                    pass
        return {"files": count, "mb": round(size / 1024 / 1024, 1), "maxMb": self.settings.cache_max_mb}

    # -- duckdb
    def _db(self) -> duckdb.DuckDBPyConnection:
        con = duckdb.connect(config={"threads": max(1, self.settings.duckdb_threads),
                                     "memory_limit": f"{self.settings.duckdb_memory_mb}MB",
                                     "preserve_insertion_order": False})
        try:
            con.execute("SET enable_progress_bar = false")
            self.tmp_dir.mkdir(parents=True, exist_ok=True)
            con.execute(f"SET temp_directory = '{self.tmp_dir.as_posix()}'")
        except (OSError, duckdb.Error):
            pass
        return con

    @staticmethod
    def _src(param: str = "?") -> str:
        return f"read_parquet({param}, union_by_name=true, filename=true, file_row_number=true)"

    def _columns(self, con, paths: list[str]) -> dict[str, str]:
        rows = con.execute(f"DESCRIBE SELECT * FROM read_parquet(?, union_by_name=true)", [paths]).fetchall()
        return {r[0]: r[1] for r in rows}

    def resolve_range(self, start, end) -> tuple[int, int]:
        now_ms = int(time.time() * 1000)
        s = parse_time(start, now_ms, "start")
        e = parse_time(end, now_ms, "end")
        if e <= s:
            raise bad_request("INVALID_TIME", "'end' must be after 'start'")
        max_ms = self.settings.max_search_hours * HOUR
        if e - s > max_ms + 60_000:
            raise bad_request("RANGE_TOO_LARGE",
                              f"One search can cover at most {self.settings.max_search_hours} hours "
                              f"({self.settings.max_search_hours // 24} days). It can start anywhere in the "
                              "time S3 keeps; pick a shorter range.",
                              {"maxHours": self.settings.max_search_hours})
        return s, e

    def _prepare(self, cluster: str, spec: SearchSpec):
        self.check_cluster(cluster)
        start_ms, end_ms = self.resolve_range(spec.start, spec.end)
        files = self.files_for(cluster, start_ms, end_ms)
        return start_ms, end_ms, files

    def _where(self, columns, spec: SearchSpec, start_ms: int, end_ms: int) -> WhereBuilder:
        wb = WhereBuilder(columns)
        wb.time(start_ms, end_ms)
        wb.text(spec.query or "")
        for f in spec.filters or []:
            wb.filter(f)
        return wb

    @staticmethod
    def _order(columns, order: str, alias: str = "") -> str:
        d = "ASC" if order == "asc" else "DESC"
        a = f"{alias}." if alias else ""
        first = f'{a}"log_time_ms" {d}, ' if "log_time_ms" in columns else ""
        return f"{first}{a}filename {d}, {a}file_row_number {d}"

    # -- matches: the narrow list of matching lines, kept for a few minutes
    def _columns_cached(self, con, paths: list[str]) -> dict[str, str]:
        key = hashlib.sha1("\n".join(paths).encode()).hexdigest()
        hit = self._schemas.get(key, RESULT_TTL)
        if hit is None:
            hit = self._columns(con, paths)
            self._schemas.set(key, hit)
        return hit

    def _matches(self, con, cluster: str, spec: SearchSpec, start_ms: int, end_ms: int,
                 paths: list[str]) -> dict[str, str]:
        """Loads table m (time, level, service, host, exception, filename, row) for every matching
        line. The result is saved as a small Parquet file for RESULT_TTL seconds, so paging, the
        export and a second worker reuse it instead of scanning every log file again."""
        columns = self._columns_cached(con, paths)
        wb = self._where(columns, spec, start_ms, end_ms)
        key = hashlib.sha1(json.dumps([cluster, start_ms, end_ms, spec.query, [vars(f) for f in spec.filters or []],
                                       len(paths), paths[-1] if paths else ""], default=str).encode()).hexdigest()
        saved = self.results_dir / f"{key}.parquet"
        try:
            fresh = saved.exists() and time.time() - saved.stat().st_mtime < RESULT_TTL
        except OSError:
            fresh = False
        if fresh:
            try:
                con.execute(f"CREATE TEMP TABLE m AS SELECT * FROM read_parquet('{saved.as_posix()}')")
                return columns
            except duckdb.Error:
                pass  # half-written or removed meanwhile: compute again
        narrow = ["log_time_ms" if "log_time_ms" in columns else "NULL::BIGINT AS log_time_ms"]
        narrow += [quote_ident(c) if c in columns else f"NULL::VARCHAR AS {quote_ident(c)}" for c in FACET_COLUMNS]
        con.execute(f"CREATE TEMP TABLE m AS SELECT {', '.join(narrow)}, filename, file_row_number "
                    f"FROM {self._src()} WHERE {wb.where()}", [paths, *wb.params])
        try:
            self.results_dir.mkdir(parents=True, exist_ok=True)
            tmp = saved.with_name(f".{saved.name}.{os.getpid()}.{threading.get_ident()}.part")
            con.execute(f"COPY m TO '{tmp.as_posix()}' (FORMAT parquet)")
            os.replace(tmp, saved)
            self._clean_results()
        except (OSError, duckdb.Error):
            log.warning("could not keep the search result for reuse", exc_info=True)
        return columns

    def _clean_results(self) -> None:
        if time.monotonic() - self._last_results_clean < 60:
            return
        self._last_results_clean = time.monotonic()
        cutoff = time.time() - RESULT_TTL * 2
        for p in self.results_dir.glob("*"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                pass

    def _rows_for(self, con, has_time: dict, order: str, limit: int, offset: int, truncate: bool):
        """Full rows for the given slice of m, in order: only their files are read."""
        con.execute("DROP TABLE IF EXISTS p")
        con.execute(f"CREATE TEMP TABLE p AS SELECT filename, file_row_number, log_time_ms FROM m "
                    f"ORDER BY {self._order(has_time, order)} LIMIT ? OFFSET ?", [limit, offset])
        files = [r[0] for r in con.execute("SELECT DISTINCT filename FROM p").fetchall()]
        if not files:
            return [], []
        cur = con.execute(f"SELECT r.* FROM {self._src()} r JOIN p ON r.filename = p.filename "
                          f"AND r.file_row_number = p.file_row_number "
                          f"ORDER BY {self._order(has_time, order, 'p')}", [files])
        return cur.fetchall(), cur.description

    def search(self, cluster: str, spec: SearchSpec, offset: int = 0, size: int = 50,
               with_aggs: bool = True) -> dict:
        t0 = time.monotonic()
        start_ms, end_ms, files = self._prepare(cluster, spec)
        base = {"cluster": cluster, "start": start_ms, "end": end_ms, "files": len(files),
                "bytes": sum(s for _, s in files)}
        step = histogram_step(end_ms - start_ms)
        if not files:
            return {**base, "total": 0, "hits": [], "columns": [], "histogram": {"interval": step, "buckets": []},
                    "facets": {}, "cached": 0, "downloadMs": 0, "tookMs": int((time.monotonic() - t0) * 1000)}
        paths, stats = self.materialize([k for k, _ in files])
        con = self._db()
        try:
            columns = self._matches(con, cluster, spec, start_ms, end_ms, paths)
            total = con.execute("SELECT count(*) FROM m").fetchone()[0]
            has_time = {"log_time_ms": 1} if "log_time_ms" in columns else {}
            rows, desc = self._rows_for(con, has_time, spec.order, size, offset, truncate=True)
            hits = [self._hit(row, desc, truncate=True) for row in rows]
            out = {**base, "total": total, "hits": hits, "columns": self._ordered_columns(columns),
                   "cached": stats["cached"], "downloadMs": stats["downloadMs"]}
            if with_aggs:
                out["histogram"] = {"interval": step, "buckets": self._histogram(con, step)}
                out["facets"] = {c: self._facet(con, c) for c in FACET_COLUMNS if c in columns}
            out["tookMs"] = int((time.monotonic() - t0) * 1000)
            return out
        except duckdb.Error as e:
            raise self._db_error(e) from e
        finally:
            con.close()

    @staticmethod
    def _db_error(e: Exception) -> ApiError:
        msg = str(e).splitlines()[0][:400]
        if "Out of Memory" in msg or "memory" in msg.lower():
            return ApiError(507, "SEARCH_TOO_BIG", "The search needed more memory than allowed "
                            "(DUCKDB_MEMORY_MB); pick a shorter time range or add filters", {"detail": msg})
        return ApiError(500, "SEARCH_FAILED", f"The search failed: {msg}")

    @staticmethod
    def _histogram(con, step: int) -> list[dict]:
        rows = con.execute(
            "SELECT (log_time_ms // ?) * ? AS t, coalesce(upper(level), 'OTHER') AS lv, count(*) "
            "FROM m WHERE log_time_ms IS NOT NULL GROUP BY 1, 2 ORDER BY 1", [step, step]).fetchall()
        buckets: dict[int, dict] = {}
        for t, lv, n in rows:
            b = buckets.setdefault(int(t), {"t": int(t), "count": 0, "levels": {}})
            b["count"] += n
            b["levels"][lv] = b["levels"].get(lv, 0) + n
        return list(buckets.values())

    @staticmethod
    def _facet(con, column: str, limit: int = 8) -> list[dict]:
        rows = con.execute(f"SELECT {quote_ident(column)} AS v, count(*) AS n FROM m WHERE {quote_ident(column)} "
                           f"IS NOT NULL GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT ?", [limit]).fetchall()
        return [{"value": v, "count": n} for v, n in rows]

    @staticmethod
    def _ordered_columns(columns: dict[str, str]) -> list[dict]:
        names = [c for c in PREFERRED_ORDER if c in columns] + [c for c in columns if c not in PREFERRED_ORDER]
        return [{"name": n, "type": columns[n]} for n in names]

    def _hit(self, row, description, truncate: bool) -> dict:
        rec: dict[str, Any] = {}
        cut = False
        for (name, *_), v in zip(description, row):
            if name in ("filename", "file_row_number"):
                continue
            if isinstance(v, str) and truncate and len(v) > LIST_TRUNCATE:
                v = v[:LIST_TRUNCATE]
                cut = True
            rec[name] = v
        names = [d[0] for d in description]
        rec["_ref"] = {"key": self.source.key_of(row[names.index("filename")]),
                       "row": int(row[names.index("file_row_number")])}
        if cut:
            rec["_truncated"] = True
        return rec

    # -- one record, complete
    def record(self, cluster: str, key: str, row: int) -> dict:
        self.check_cluster(cluster)
        if not KEY_RE.match(key or "") or ".." in key or not key.startswith(f"{self.prefix}{cluster}/") \
                or not key.endswith(".parquet"):
            raise bad_request("INVALID_REF", "That log reference does not belong to this cluster")
        path, _ = self.source.fetch(key)
        con = self._db()
        try:
            cur = con.execute(f"SELECT * FROM {self._src()} WHERE file_row_number = ?", [[str(path)], int(row)])
            r = cur.fetchone()
            if r is None:
                raise not_found("LOG_NOT_FOUND", "No log line at that position")
            return self._hit(r, cur.description, truncate=False)
        except duckdb.Error as e:
            raise self._db_error(e) from e
        finally:
            con.close()

    # -- export
    def export(self, cluster: str, spec: SearchSpec, fmt: str, columns: list[str] | None,
               limit: int, tz: str | None = None, offset_minutes: int | None = None) -> "Export":
        """The first `limit` matching lines in spec.order. With tz (an IANA name such as
        Asia/Kolkata), log_time is written in that zone with its offset, as the viewer shows it."""
        zone = export_zone(tz, offset_minutes)
        start_ms, end_ms, files = self._prepare(cluster, spec)
        limit = max(1, min(int(limit), self.settings.max_export_rows))
        if not files:
            return Export(_encode(fmt, list(columns or []), []), 0, fmt, start_ms, end_ms, 0, None, None, zone)
        paths, _ = self.materialize([k for k, _ in files])
        con = self._db()
        try:
            cols = self._matches(con, cluster, spec, start_ms, end_ms, paths)
            wb = WhereBuilder(cols)
            names = list(columns) if columns else [c["name"] for c in self._ordered_columns(cols)]
            for c in names:
                wb._col(c)
            has_time = {"log_time_ms": 1} if "log_time_ms" in cols else {}
            total = con.execute("SELECT count(*) FROM m").fetchone()[0]
            rows, desc = self._rows_for(con, has_time, spec.order, limit, 0, truncate=False)
            first = last = None
            if rows:
                idx = {d[0]: i for i, d in enumerate(desc)}
                t = idx.get("log_time_ms")
                if t is not None:
                    first, last = rows[0][t], rows[-1][t]
                get = {n: (lambda r, i=idx[n]: r[i]) for n in names}
                if zone and t is not None and "log_time" in get:
                    get["log_time"] = lambda r: zone_time(r[t], zone)
                rows = [tuple(get[n](r) for n in names) for r in rows]
        except duckdb.Error as e:
            raise self._db_error(e) from e
        finally:
            con.close()
        return Export(_encode(fmt, names, rows), len(rows), fmt, start_ms, end_ms, total, first, last, zone)


@dataclass
class Export:
    stream: Iterator[bytes]
    rows: int
    fmt: str
    start_ms: int
    end_ms: int
    total: int            # all matching lines; the file holds the first `rows` of them
    first_ms: int | None  # log_time_ms of the first and last line in the file
    last_ms: int | None
    zone: Any = None      # ZoneInfo the times were written in (None: as stored, UTC)


def _zone(tz: str):
    """ZoneInfo from the system, else from the tzdata package (it also has the old names browsers
    still send, such as Asia/Calcutta, which newer system zone data leaves out)."""
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        pass
    try:
        import importlib.resources
        res = importlib.resources.files("tzdata.zoneinfo").joinpath(*tz.split("/"))
        with res.open("rb") as f:
            return ZoneInfo.from_file(f, key=tz)
    except Exception:  # noqa: BLE001 - no tzdata package or no such zone
        return None


def export_zone(tz: str | None, offset_minutes: int | None = None):
    """The zone to write times in. An IANA name if the server knows it; otherwise the browser's
    current UTC offset; None = UTC."""
    if tz and tz.upper() not in ("UTC", "ETC/UTC", "Z"):
        zone = _zone(tz)
        if zone is not None:
            return zone
        log.warning("unknown time zone %r; using the browser's offset", tz)
    if offset_minutes:
        return timezone(timedelta(minutes=offset_minutes))
    return None


def zone_time(ms: int | None, zone, with_ms: bool = True) -> str | None:
    """2026-10-07 01:03:16.571 +05:30"""
    if ms is None:
        return None
    d = datetime.fromtimestamp(ms / 1000, timezone.utc).astimezone(zone) if zone else \
        datetime.fromtimestamp(ms / 1000, timezone.utc)
    off = d.strftime("%z")
    off = f"{off[:3]}:{off[3:]}" if off else "+00:00"
    return d.strftime("%Y-%m-%d %H:%M:%S") + (f".{d.microsecond // 1000:03d}" if with_ms else "") + f" {off}"


def _cell(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat()
    return v


def _encode(fmt: str, names: list[str], rows: list[tuple]) -> Iterator[bytes]:
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        buf.write("﻿")  # Excel reads UTF-8 with a BOM
        w.writerow(names)
        for i, r in enumerate(rows):
            w.writerow(["" if v is None else _cell(v) for v in r])
            if i % 500 == 499:
                yield buf.getvalue().encode()
                buf.seek(0)
                buf.truncate()
        yield buf.getvalue().encode()
    elif fmt == "ndjson":
        for r in rows:
            yield (json.dumps({n: _cell(v) for n, v in zip(names, r) if v is not None},
                              ensure_ascii=False, default=str) + "\n").encode()
    else:
        yield b"["
        for i, r in enumerate(rows):
            yield ((",\n" if i else "\n") + json.dumps({n: _cell(v) for n, v in zip(names, r) if v is not None},
                                                       ensure_ascii=False, default=str)).encode()
        yield b"\n]\n"
