"""Application Load Balancer access logs: convert them once, then query them with DuckDB.

AWS writes one gzip text file per load balancer node every 5 minutes:

    s3://<LB_LOGS_BUCKET>/<LB_LOGS_PREFIX>AWSLogs/<account>/elasticloadbalancing/<region>/yyyy/mm/dd/
        <account>_elasticloadbalancing_<region>_app.<lb-name>.<lb-id>_<end yyyymmddThhmmZ>_<node-ip>_<rand>.log.gz

A background converter (one per container, chosen by a file lock) converts each file once and
writes the result back to S3 (LB_PARQUET_BUCKET / LB_PARQUET_PREFIX, v<format>/):

    hour/<lb>/<yyyy-mm-dd>/<HH>.parquet     every request of one load balancer and hour
    minute/<lb>/<yyyy-mm-dd>/<HH>.parquet   requests per minute, target group, domain, status, latency bin
    paths/<lb>/<yyyy-mm-dd>/<HH>.parquet    the same per method and grouped path (no domain)
    keys/<lb>/<yyyy-mm-dd>/<HH>.json        the AWS log files merged into that hour (written last)

CACHE_DIR/lb/ on the server holds the same layout as a cache: every minute and keys file (small),
the hour and paths files that were used recently (LB_CACHE_MAX_MB, oldest removed first), the
5-minute files of hours not finished yet (raw/), status.json and wanted/. Losing it loses nothing:
it is refilled from S3.

Counts, the chart, the target group table read the minute files; paths and the top failing path
the paths files; single requests read only the hour files that hold the page.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import boto3
import duckdb
from botocore.config import Config
from botocore.exceptions import ClientError, EndpointConnectionError

from .errors import ApiError, bad_request
from .logs import HOUR, _encode, histogram_step, parse_time
from .settings import Settings

log = logging.getLogger("vector_logs.lb")

MINUTE = 60_000
FORMAT_VERSION = "3"   # 3: converted files in S3, paths roll-up. A change converts everything again.
LB_NAME_RE = re.compile(r"^[A-Za-z0-9-]{1,32}$")
FILE_RE = re.compile(
    r"^(?P<acct>\d{12})_elasticloadbalancing_(?P<region>[a-z0-9-]+)_app\.(?P<lb>[A-Za-z0-9-]{1,32})\."
    r"(?P<lbid>[0-9a-f]+)_(?P<end>\d{8}T\d{4}Z)_(?P<ip>[0-9A-Fa-f.:]+)_(?P<rand>[A-Za-z0-9]+)\.log\.gz$")

# The documented Application Load Balancer access log fields, in order. AWS appends new fields
# at the end now and then, so a few spare columns absorb them.
ALB_FIELDS = [
    "type", "time", "elb", "client", "target", "request_processing_time", "target_processing_time",
    "response_processing_time", "elb_status_code", "target_status_code", "received_bytes", "sent_bytes",
    "request", "user_agent", "ssl_cipher", "ssl_protocol", "target_group_arn", "trace_id", "domain_name",
    "chosen_cert_arn", "matched_rule_priority", "request_creation_time", "actions_executed", "redirect_url",
    "error_reason", "target_port_list", "target_status_code_list", "classification", "classification_reason",
    "conn_trace_id", "transformed_host", "transformed_uri", "request_transform_status", "ip_address",
    "target_error_code", "elb_error_code", "spare1", "spare2", "spare3", "spare4"]

# One row per request in the converted files (ts_ms = epoch milliseconds, UTC).
REQUEST_COLUMNS = [
    "time", "ts_ms", "lb", "tg", "cluster", "domain", "method", "path", "path_group", "query", "url",
    "protocol", "elb_code", "tgt_code", "client_ip", "client_port", "target", "req_t", "tgt_t", "resp_t",
    "rx", "tx", "user_agent", "error_reason", "classification", "classification_reason", "actions",
    "elb_error_code", "target_error_code", "trace_id", "type", "ssl_protocol", "rule_priority",
    "request_created", "target_list", "target_code_list", "raw"]
ROLLUP_COLUMNS = ["minute_ms", "lb", "tg", "cluster", "domain", "elb_code", "tgt_code", "lat_bin", "n", "tgt_t_sum",
                  "tgt_t_n"]
PATHS_COLUMNS = ["minute_ms", "lb", "tg", "cluster", "method", "path_group", "elb_code", "tgt_code", "lat_bin", "n",
                 "tgt_t_sum", "tgt_t_n"]
# Target response time bins (milliseconds) kept in the per-minute files, so percentiles over days
# come from counts instead of millions of single values. Bin i holds LAT_EDGES[i] <= t < LAT_EDGES[i+1].
LAT_EDGES = [0, 5, 10, 20, 30, 50, 75, 100, 150, 200, 300, 500, 750, 1000, 1500, 2000, 3000, 5000, 7500,
             10000, 15000, 20000, 30000, 60000]
_LAT_LIST = "[" + ", ".join(str(e) for e in LAT_EDGES) + "]"


def _dash(col: str) -> str:
    return f"nullif({col}, '-')"


def _secs(col: str) -> str:  # -1 means "no answer" in ALB logs
    return f"CASE WHEN try_cast({col} AS DOUBLE) >= 0 THEN try_cast({col} AS DOUBLE) END"


# One token per field: a "quoted string" (backslash escapes inside) or a run of non-spaces.
_TOKEN_RE = '"(?:[^"\\\\]|\\\\.)*"|[^ ]+'


def _fields_sql(files_sql: str) -> str:
    """Each line as it is (raw) plus the documented fields, unquoted, by position."""
    cols = ", ".join(f"CASE WHEN t[{i}] LIKE '\"%\"' THEN t[{i}][2:-2] ELSE t[{i}] END AS \"{name}\""
                     for i, name in enumerate(ALB_FIELDS, start=1))
    return f"""SELECT raw, {cols}
      FROM (SELECT raw, regexp_extract_all(raw, '{_TOKEN_RE}') AS t
            FROM read_csv({files_sql}, columns = {{'raw': 'VARCHAR'}}, delim = '\\x1f', quote = '', escape = '',
                          header = false, auto_detect = false, ignore_errors = true)
            WHERE raw IS NOT NULL AND raw <> '')"""


def convert_sql(files_sql: str) -> str:
    """SELECT turning raw ALB log lines (read_csv over files_sql) into REQUEST_COLUMNS."""
    base = f"""
      SELECT raw, {_dash('"time"')} AS time,
             epoch_ms(try_cast("time" AS TIMESTAMP)) AS ts_ms,
             split_part(elb, '/', 2) AS lb,
             nullif(regexp_extract(target_group_arn, 'targetgroup/([^/]+)/', 1), '') AS tg,
             {_dash('domain_name')} AS domain,
             nullif(split_part(request, ' ', 1), '-') AS method,
             nullif(nullif(split_part(request, ' ', 2), '-'), '') AS url,
             nullif(nullif(split_part(request, ' ', 3), '-'), '') AS protocol,
             try_cast(elb_status_code AS SMALLINT) AS elb_code,
             try_cast(target_status_code AS SMALLINT) AS tgt_code,
             nullif(regexp_extract(client, '^(.*):[0-9]+$', 1), '') AS client_ip,
             try_cast(regexp_extract(client, ':([0-9]+)$', 1) AS INTEGER) AS client_port,
             {_dash('target')} AS target,
             {_secs('request_processing_time')} AS req_t,
             {_secs('target_processing_time')} AS tgt_t,
             {_secs('response_processing_time')} AS resp_t,
             try_cast(received_bytes AS BIGINT) AS rx,
             try_cast(sent_bytes AS BIGINT) AS tx,
             {_dash('user_agent')} AS user_agent,
             {_dash('error_reason')} AS error_reason,
             {_dash('classification')} AS classification,
             {_dash('classification_reason')} AS classification_reason,
             {_dash('actions_executed')} AS actions,
             {_dash('elb_error_code')} AS elb_error_code,
             {_dash('target_error_code')} AS target_error_code,
             {_dash('trace_id')} AS trace_id,
             type,
             {_dash('ssl_protocol')} AS ssl_protocol,
             {_dash('matched_rule_priority')} AS rule_priority,
             {_dash('request_creation_time')} AS request_created,
             {_dash('target_port_list')} AS target_list,
             {_dash('target_status_code_list')} AS target_code_list
      FROM ({_fields_sql(files_sql)}) f"""
    path = "coalesce(nullif(regexp_extract(url, '^[A-Za-z][A-Za-z0-9+.-]*://[^/?#]*([^?#]*)', 1), ''), " \
           "CASE WHEN url LIKE '/%' THEN split_part(split_part(url, '?', 1), '#', 1) END, '/')"
    group = "regexp_replace(regexp_replace(regexp_replace(regexp_replace(regexp_replace(p.path, " \
            "'/[A-Za-z0-9-]+\\.myshopify\\.com', '/<store>', 'g'), " \
            "'/[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}', '/<uuid>', 'g'), " \
            "'/[0-9A-Fa-f]{16,}(/|$)', '/<id>\\1', 'g'), '/[0-9]+(/|$)', '/<n>\\1', 'g'), '/[0-9]+(/|$)', '/<n>\\1', 'g')"
    return f"""
      SELECT p.raw, p.time, p.ts_ms, p.lb, p.tg,
             CASE WHEN p.tg LIKE 'tg-%' THEN substr(p.tg, 4) ELSE p.tg END AS cluster,
             p.domain, p.method, p.path, {group} AS path_group,
             nullif(regexp_extract(p.url, '\\?([^#]*)', 1), '') AS query,
             p.url, p.protocol, p.elb_code, p.tgt_code, p.client_ip, p.client_port, p.target,
             p.req_t, p.tgt_t, p.resp_t, p.rx, p.tx, p.user_agent, p.error_reason, p.classification,
             p.classification_reason, p.actions, p.elb_error_code, p.target_error_code, p.trace_id, p.type,
             p.ssl_protocol, p.rule_priority, p.request_created, p.target_list, p.target_code_list
      FROM (SELECT b.*, {path.replace('url', 'b.url')} AS path FROM ({base}) b) p"""


def _rollup(source_sql: str, dims: list[str]) -> str:
    lat_bin = f"CASE WHEN tgt_t IS NOT NULL THEN (len(list_filter({_LAT_LIST}, e -> e <= tgt_t * 1000)) - 1)::TINYINT END"
    return f"""SELECT (ts_ms // {MINUTE}) * {MINUTE} AS minute_ms, {', '.join(dims)},
                      {lat_bin} AS lat_bin, count(*)::BIGINT AS n, sum(tgt_t) AS tgt_t_sum,
                      count(tgt_t)::BIGINT AS tgt_t_n
               FROM {source_sql} GROUP BY ALL"""


def rollup_sql(source_sql: str) -> str:
    """Per minute, target group, domain, status and latency bin (ROLLUP_COLUMNS)."""
    return _rollup(source_sql, ["lb", "tg", "cluster", "domain", "elb_code", "tgt_code"])


def paths_rollup_sql(source_sql: str) -> str:
    """Per minute, target group, method, grouped path, status and latency bin (PATHS_COLUMNS)."""
    return _rollup(source_sql, ["lb", "tg", "cluster", "method", "path_group", "elb_code", "tgt_code"])


def percentile(bins: dict[int, int], p: float) -> float | None:
    """Approximate percentile (seconds) from latency bin counts, interpolated inside the bin."""
    total = sum(bins.values())
    if not total:
        return None
    want, seen = p * total, 0
    for b in sorted(bins):
        n = bins[b]
        if seen + n >= want:
            lo = LAT_EDGES[b]
            hi = LAT_EDGES[b + 1] if b + 1 < len(LAT_EDGES) else lo
            frac = (want - seen) / n if n else 0
            return (lo + (hi - lo) * frac) / 1000
        seen += n
    return LAT_EDGES[-1] / 1000


def _sql_list(paths: list[Path | str]) -> str:
    return "[" + ", ".join("'" + str(p).replace("'", "''") + "'" for p in paths) + "]"


# ------------------------------------------------------------------ time helpers
def hour_key(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H")


def hour_of_key(hk: str) -> datetime:
    return datetime.strptime(hk, "%Y-%m-%dT%H").replace(tzinfo=timezone.utc)


def file_hour(end: str) -> datetime:
    """The hour a 5-minute file belongs to (it covers the 5 minutes before its end time)."""
    t = datetime.strptime(end, "%Y%m%dT%H%MZ").replace(tzinfo=timezone.utc) - timedelta(minutes=1)
    return t.replace(minute=0, second=0, microsecond=0)


def hours_in(start_ms: int, end_ms: int) -> list[str]:
    t = datetime.fromtimestamp(start_ms / 1000, timezone.utc).replace(minute=0, second=0, microsecond=0)
    end = datetime.fromtimestamp(end_ms / 1000, timezone.utc)
    out = []
    while t <= end:
        out.append(hour_key(t))
        t += timedelta(hours=1)
    return out


def _hour_of_path(path: str) -> str:
    """Hour key of a converted file: .../<hour|minute|paths>/<lb>/<day>/<HH>.parquet or .../raw/<lb>/<day>/<HH>/<file>."""
    parts = Path(path).parts
    if parts[-4] in ("hour", "minute", "paths"):
        return f"{parts[-2]}T{parts[-1][:2]}"
    return f"{parts[-3]}T{parts[-2]}"


def _lb_of_path(path: str) -> str:
    parts = Path(path).parts
    return parts[-3] if parts[-4] in ("hour", "minute", "paths") else parts[-4]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None = None) -> str:
    return (dt or _now()).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class LogFile:
    key: str
    lb: str
    hour: str        # hour key, e.g. 2026-10-07T07
    end: str         # 20261007T0720Z

    @property
    def name(self) -> str:
        return self.key.rsplit("/", 1)[-1]

    @property
    def parquet_name(self) -> str:
        return self.name[: -len(".log.gz")] + ".parquet"


def parse_key(key: str) -> LogFile | None:
    m = FILE_RE.match(key.rsplit("/", 1)[-1])
    if not m:
        return None
    return LogFile(key=key, lb=m["lb"], hour=hour_key(file_hour(m["end"])), end=m["end"])


# ------------------------------------------------------------------ where the files are
def lb_error(e: Exception, what: str) -> ApiError:
    if isinstance(e, EndpointConnectionError):
        return ApiError(502, "LB_LOGS_UNREACHABLE", f"Can't reach S3 to {what}: {e}")
    code = e.response["Error"]["Code"] if isinstance(e, ClientError) else type(e).__name__
    if code in ("AccessDenied", "AccessDeniedException", "403", "InvalidAccessKeyId", "ExpiredToken"):
        return ApiError(500, "LB_LOGS_ACCESS_DENIED",
                        f"The server may not {what} in the load balancer logs bucket: add the "
                        "ListLoadBalancerLogs and ReadLoadBalancerLogs statements from docs/iam-policy.json "
                        "to the EC2 role",
                        {"awsError": code})
    if code == "NoSuchBucket":
        return ApiError(502, "LB_LOGS_BUCKET_MISSING", "LB_LOGS_BUCKET does not exist", {"awsError": code})
    return ApiError(502, "LB_LOGS_UNAVAILABLE", f"S3 error while trying to {what}: {e}", {"awsError": code})


class S3LbSource:
    def __init__(self, settings: Settings, client=None):
        self.bucket = settings.lb_logs_bucket
        self.prefix = settings.lb_logs_prefix
        self.s3 = client or boto3.client(
            "s3", region_name=settings.lb_logs_region, endpoint_url=settings.lb_logs_endpoint_url,
            config=Config(retries={"max_attempts": 5, "mode": "standard"}, max_pool_connections=16))

    def describe(self) -> str:
        return f"s3://{self.bucket}/{self.prefix}"

    def _dirs(self, prefix: str) -> list[str]:
        out = []
        try:
            for page in self.s3.get_paginator("list_objects_v2").paginate(
                    Bucket=self.bucket, Prefix=prefix, Delimiter="/"):
                out += [cp["Prefix"] for cp in page.get("CommonPrefixes", [])]
        except (ClientError, EndpointConnectionError) as e:
            raise lb_error(e, "list folders") from e
        return out

    def bases(self) -> list[str]:
        """<prefix>AWSLogs/<account>/elasticloadbalancing/<region>/ for every account and region."""
        out = []
        for acct in self._dirs(f"{self.prefix}AWSLogs/"):
            out += self._dirs(f"{acct}elasticloadbalancing/")
        return out

    def list_day(self, base: str, day: date) -> list[str]:
        prefix = f"{base}{day:%Y/%m/%d}/"
        keys = []
        try:
            for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
                keys += [o["Key"] for o in page.get("Contents", []) if o["Key"].endswith(".log.gz")]
        except (ClientError, EndpointConnectionError) as e:
            raise lb_error(e, "list log files") from e
        return keys

    def fetch(self, key: str, tmp_dir: Path) -> Path:
        dest = tmp_dir / key.rsplit("/", 1)[-1]
        try:
            self.s3.download_file(self.bucket, key, str(dest))
        except (ClientError, EndpointConnectionError) as e:
            raise lb_error(e, f"read {key}") from e
        return dest

    def cleanup(self, path: Path) -> None:
        path.unlink(missing_ok=True)


class LocalLbSource:
    """A folder laid out like the bucket (development and tests)."""

    def __init__(self, settings: Settings):
        self.root = Path(settings.lb_logs_local_dir)
        self.prefix = settings.lb_logs_prefix

    def describe(self) -> str:
        return f"{self.root}/{self.prefix}"

    def bases(self) -> list[str]:
        root = self.root / self.prefix / "AWSLogs"
        if not root.exists():
            return []
        return sorted(f"{self.prefix}AWSLogs/{a.name}/elasticloadbalancing/{r.name}/"
                      for a in root.iterdir() if (a / "elasticloadbalancing").is_dir()
                      for r in (a / "elasticloadbalancing").iterdir() if r.is_dir())

    def list_day(self, base: str, day: date) -> list[str]:
        d = self.root / base / f"{day:%Y/%m/%d}"
        return sorted(f"{base}{day:%Y/%m/%d}/{p.name}" for p in d.glob("*.log.gz")) if d.is_dir() else []

    def fetch(self, key: str, tmp_dir: Path) -> Path:
        return self.root / key

    def cleanup(self, path: Path) -> None:
        pass


def build_lb_source(settings: Settings):
    return LocalLbSource(settings) if settings.lb_logs_backend == "local" else S3LbSource(settings)


# ------------------------------------------------------------------ converted files: S3 and the local cache
def parquet_error(e: Exception, what: str) -> ApiError:
    if isinstance(e, EndpointConnectionError):
        return ApiError(502, "LB_PARQUET_UNREACHABLE", f"Can't reach S3 to {what}: {e}")
    code = e.response["Error"]["Code"] if isinstance(e, ClientError) else type(e).__name__
    if code in ("AccessDenied", "AccessDeniedException", "403", "InvalidAccessKeyId", "ExpiredToken"):
        return ApiError(500, "LB_PARQUET_ACCESS_DENIED",
                        f"The server may not {what} under the converted load balancer logs prefix: add the "
                        "ListLoadBalancerLogs and ReadWriteConvertedLoadBalancerLogs statements from "
                        "docs/iam-policy.json to the EC2 role", {"awsError": code})
    return ApiError(502, "LB_PARQUET_UNAVAILABLE", f"S3 error while trying to {what}: {e}", {"awsError": code})


class S3Converted:
    """Where converted files live for good: s3://<LB_PARQUET_BUCKET>/<LB_PARQUET_PREFIX>v<format>/."""

    def __init__(self, settings: Settings, client=None):
        self.bucket = settings.lb_parquet_bucket or settings.lb_logs_bucket
        self.prefix = f"{settings.lb_parquet_prefix}v{FORMAT_VERSION}/"
        self.s3 = client or boto3.client(
            "s3", region_name=settings.lb_logs_region, endpoint_url=settings.lb_logs_endpoint_url,
            config=Config(retries={"max_attempts": 5, "mode": "standard"}, max_pool_connections=32))

    def describe(self) -> str:
        return f"s3://{self.bucket}/{self.prefix}"

    def put(self, local: Path, rel: str) -> None:
        try:
            self.s3.upload_file(str(local), self.bucket, self.prefix + rel)
        except (ClientError, EndpointConnectionError) as e:
            raise parquet_error(e, f"write {rel}") from e

    def get(self, rel: str, dest: Path) -> bool:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{dest.name}.{os.getpid()}.{threading.get_ident()}.dl")
        try:
            self.s3.download_file(self.bucket, self.prefix + rel, str(tmp))
            os.replace(tmp, dest)
            return True
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise parquet_error(e, f"read {rel}") from e
        except EndpointConnectionError as e:
            raise parquet_error(e, f"read {rel}") from e
        finally:
            tmp.unlink(missing_ok=True)

    def list(self, rel_prefix: str) -> list[str]:
        out = []
        try:
            for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket,
                                                                          Prefix=self.prefix + rel_prefix):
                out += [o["Key"][len(self.prefix):] for o in page.get("Contents", [])]
        except (ClientError, EndpointConnectionError) as e:
            raise parquet_error(e, "list converted files") from e
        return out


class LocalConverted:
    """A folder standing in for the S3 prefix (development and tests)."""

    def __init__(self, settings: Settings):
        self.root = Path(settings.lb_parquet_local_dir) / f"v{FORMAT_VERSION}"

    def describe(self) -> str:
        return str(self.root)

    def put(self, local: Path, rel: str) -> None:
        dest = self.root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{dest.name}.{os.getpid()}.up")
        shutil.copyfile(local, tmp)
        os.replace(tmp, dest)

    def get(self, rel: str, dest: Path) -> bool:
        src = self.root / rel
        if not src.exists():
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{dest.name}.{os.getpid()}.{threading.get_ident()}.dl")
        shutil.copyfile(src, tmp)
        os.replace(tmp, dest)
        return True

    def list(self, rel_prefix: str) -> list[str]:
        base = self.root / rel_prefix
        if not base.exists():
            return []
        return sorted(p.relative_to(self.root).as_posix() for p in base.rglob("*") if p.is_file()
                      and not p.name.startswith("."))


def build_converted(settings: Settings):
    backend = settings.lb_parquet_backend or settings.lb_logs_backend
    return LocalConverted(settings) if backend == "local" else S3Converted(settings)


def rel(kind: str, lb: str, hk: str) -> str:
    """Path of a converted file, the same in S3 and in the cache."""
    return f"{kind}/{lb}/{hk[:10]}/{hk[11:13]}{'.json' if kind == 'keys' else '.parquet'}"


def parse_rel(r: str) -> tuple[str, str, str] | None:
    """(kind, lb, hour key) of a converted file path."""
    parts = r.split("/")
    if len(parts) != 4 or not LB_NAME_RE.match(parts[1]):
        return None
    try:
        hk = f"{parts[2]}T{parts[3][:2]}"
        hour_of_key(hk)
    except ValueError:
        return None
    return parts[0], parts[1], hk


class LbStore:
    """CACHE_DIR/lb: the local copies (same layout as S3) and the converter's working files."""

    def __init__(self, root: Path):
        self.root = root

    def file(self, kind: str, lb: str, hk: str) -> Path:
        return self.root / rel(kind, lb, hk)

    def hour_file(self, lb: str, hk: str) -> Path:
        return self.file("hour", lb, hk)

    def keys_file(self, lb: str, hk: str) -> Path:
        return self.file("keys", lb, hk)

    def minute_file(self, lb: str, hk: str) -> Path:
        return self.file("minute", lb, hk)

    def paths_file(self, lb: str, hk: str) -> Path:
        return self.file("paths", lb, hk)

    def raw_dir(self, lb: str, hk: str) -> Path:
        return self.root / "raw" / lb / hk[:10] / hk[11:13]

    @property
    def status_file(self) -> Path:
        return self.root / "status.json"

    @property
    def wanted_dir(self) -> Path:
        return self.root / "wanted"

    @property
    def tmp_dir(self) -> Path:
        return self.root / ".tmp"

    @property
    def trash_dir(self) -> Path:
        return self.root / ".trash"

    def lbs(self) -> list[str]:
        names = set()
        for kind in ("minute", "raw"):
            d = self.root / kind
            if d.is_dir():
                names |= {p.name for p in d.iterdir() if p.is_dir() and LB_NAME_RE.match(p.name)}
        return sorted(names)

    def merged_keys(self, lb: str, hk: str) -> set[str]:
        try:
            return set(json.loads(self.keys_file(lb, hk).read_text()))
        except (OSError, ValueError):
            return set()

    def raw_files(self, lb: str, hk: str) -> list[Path]:
        d = self.raw_dir(lb, hk)
        return sorted(d.glob("*.parquet")) if d.is_dir() else []

    def read_status(self) -> dict:
        try:
            return json.loads(self.status_file.read_text())
        except (OSError, ValueError):
            return {}


_evict_lock = threading.Lock()
_last_evict = [0.0]


def evict_cache(store: LbStore, max_mb: int, keep_seconds: int = 600, force: bool = False) -> int:
    """Keep the cached hour and paths files under max_mb (oldest use first; never ones used in the
    last keep_seconds). Minute and keys files are small and always kept. Returns files removed."""
    if not force and time.monotonic() - _last_evict[0] < 30:
        return 0
    if not _evict_lock.acquire(blocking=False):
        return 0
    try:
        _last_evict[0] = time.monotonic()
        entries, total = [], 0
        for kind in ("hour", "paths"):
            for dirpath, _, names in os.walk(store.root / kind):
                for n in names:
                    if n.startswith("."):
                        continue
                    p = os.path.join(dirpath, n)
                    try:
                        st = os.stat(p)
                    except OSError:
                        continue
                    entries.append((st.st_mtime, st.st_size, p))
                    total += st.st_size
        limit = max_mb * 1024 * 1024
        if total <= limit:
            return 0
        entries.sort()
        target, keep_after, removed = int(limit * 0.8), time.time() - keep_seconds, 0
        for mtime, size, p in entries:
            if total <= target or mtime > keep_after:
                break
            try:
                os.unlink(p)
                total -= size
                removed += 1
            except OSError:
                pass
        if removed:
            log.info("load balancer cache: removed %d files, %d MB left", removed, total // (1024 * 1024))
        return removed
    finally:
        _evict_lock.release()


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")))
    os.replace(tmp, path)


# ------------------------------------------------------------------ the converter
class _FileLock:
    """Non-blocking, process-wide lock on a file (only one converter per container)."""

    def __init__(self, path: Path):
        self.path = path
        self.fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")
        try:
            try:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except ImportError:  # Windows (local runs)
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            fh.close()
            return False
        self.fh = fh
        return True


class Converter:
    """Lists the bucket, converts new files, merges finished hours and stores them in S3. Call
    run_once() (tests, CLI) or start() for the background thread."""

    SETTLE = timedelta(minutes=15)       # an hour is merged this long after it ends
    PAST_LISTING_TTL = 3600              # days before yesterday are listed again at most hourly
    BASES_TTL = 3600
    INDEX_TTL = 3600                     # how often the cache's index is compared with S3

    def __init__(self, settings: Settings, source=None, now=_now, converted=None):
        self.settings = settings
        self.source = source or build_lb_source(settings)
        self.converted = converted or build_converted(settings)
        self.store = LbStore(Path(settings.cache_dir) / "lb")
        self.now = now
        self._listings: dict[tuple[str, str], tuple[float, list[str]]] = {}
        self._bases: tuple[float, list[str]] | None = None
        self._done: dict[tuple[str, str], set[str]] = {}
        self._known: dict[tuple[str, str], set[str]] = {}
        self._last_cycle = 0.0
        self._last_index = 0.0
        self._last_error: dict | None = None
        self._cycle_ms = 0
        self.converted_files = 0         # 5-minute AWS files converted by this process (tests, logs)
        self._pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="lb-fetch")
        self._stop = threading.Event()
        self._lock = _FileLock(self.store.root / ".converter.lock")
        self.thread: threading.Thread | None = None

    # -- background thread
    def start(self) -> None:
        self.thread = threading.Thread(target=self._loop, name="lb-converter", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        try:  # lower this thread's CPU priority (Linux: per thread)
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)
        except (AttributeError, OSError):
            pass
        while not self._stop.is_set():
            if self._lock.fh is None and not self._lock.acquire():
                self._stop.wait(60)          # another worker converts; check again later
                continue
            try:
                if time.monotonic() - self._last_cycle >= self.settings.lb_poll_seconds or not self._last_cycle:
                    self.run_once()
                else:
                    if self.process_wanted():
                        self.write_status()
            except Exception:  # noqa: BLE001 - keep the thread alive, report on the page
                log.exception("load balancer log converter failed")
            self._stop.wait(2)

    # -- one cycle
    def _check_format(self) -> None:
        """Local copies from an older version of this code are removed (S3 has a folder per version)."""
        mark = self.store.root / "FORMAT"
        try:
            current = mark.read_text().strip()
        except OSError:
            current = None
        if current == FORMAT_VERSION:
            return
        for kind in ("hour", "minute", "paths", "keys", "raw"):
            d = self.store.root / kind
            if d.exists():
                self.store.trash_dir.mkdir(parents=True, exist_ok=True)
                os.replace(d, self.store.trash_dir / f"{kind}-v{current}-{time.time_ns()}")
        self._done.clear()
        self._last_index = 0.0
        mark.parent.mkdir(parents=True, exist_ok=True)
        mark.write_text(FORMAT_VERSION)
        if current is not None:
            log.warning("load balancer logs: local files are from format %s; refilling from S3", current)

    def sync_index(self) -> int:
        """Copy every keys and minute file S3 has (within the retention) that the cache lacks, so a
        new or wiped server knows what is converted without converting it again."""
        oldest = self.now() - timedelta(days=self.settings.lb_retention_days)
        have = set()
        missing = []
        for r in self.converted.list("keys/"):
            parsed = parse_rel(r)
            if not parsed:
                continue
            _, lb, hk = parsed
            if hour_of_key(hk) + timedelta(hours=1) < oldest:
                continue
            have.add((lb, hk))
            if not self.store.keys_file(lb, hk).exists() or not self.store.minute_file(lb, hk).exists():
                missing.append((lb, hk))

        def pull(item):
            lb, hk = item
            if self.converted.get(rel("minute", lb, hk), self.store.minute_file(lb, hk)):
                self.converted.get(rel("keys", lb, hk), self.store.keys_file(lb, hk))
                self._done.pop((lb, hk), None)
        list(self._pool.map(pull, missing))
        self._last_index = time.monotonic()
        if missing:
            log.info("load balancer logs: copied the index of %d converted hours from %s", len(missing),
                     self.converted.describe())
        return len(missing)

    def run_once(self) -> None:
        t0 = time.monotonic()
        self._last_cycle = t0
        try:
            self._check_format()
            if not self._last_index or time.monotonic() - self._last_index > self.INDEX_TTL:
                self.sync_index()
            self._empty_trash()
            now = self.now()
            warm_from = now - timedelta(days=self.settings.lb_warm_days)
            days = self._days(warm_from - timedelta(hours=1), now)
            for f in self._listed(days):
                self._known.setdefault((f.lb, f.hour), set()).add(f.key)
            last_status = time.monotonic()
            for (lb, hk), keys in sorted(self._known.items(), key=lambda kv: kv[0][1], reverse=True):
                if hour_of_key(hk) + timedelta(hours=1) <= warm_from:
                    continue
                self._sync_hour(lb, hk, keys, now)
                if time.monotonic() - last_status > 5:      # the page shows progress while catching up
                    self.write_status()
                    last_status = time.monotonic()
                if self._stop.is_set():
                    break
            self.process_wanted()
            self._prune(now)
            self._last_error = None
        except ApiError as e:
            self._last_error = {"code": e.code, "message": e.message, "at": _iso()}
            log.warning("load balancer logs: %s", e.message)
        finally:
            self._cycle_ms = int((time.monotonic() - t0) * 1000)
            self.write_status()

    def _days(self, start: datetime, end: datetime) -> list[date]:
        d, out = start.date(), []
        while d <= end.date():
            out.append(d)
            d += timedelta(days=1)
        return out

    def _base_list(self) -> list[str]:
        if self._bases and time.monotonic() - self._bases[0] < self.BASES_TTL:
            return self._bases[1]
        bases = self.source.bases()
        self._bases = (time.monotonic(), bases)
        return bases

    def _listed(self, days: list[date]) -> list[LogFile]:
        today = self.now().date()
        out = []
        for base in self._base_list():
            for day in days:
                k = (base, day.isoformat())
                hit = self._listings.get(k)
                fresh = hit and (day < today - timedelta(days=1)) and time.monotonic() - hit[0] < self.PAST_LISTING_TTL
                keys = hit[1] if fresh else self.source.list_day(base, day)
                if not fresh:
                    self._listings[k] = (time.monotonic(), keys)
                out += [f for f in map(parse_key, keys) if f and LB_NAME_RE.match(f.lb)]
        return out

    def done_keys(self, lb: str, hk: str) -> set[str]:
        k = (lb, hk)
        if k not in self._done:
            names = {p.name for p in self.store.raw_files(lb, hk)}
            merged = self.store.merged_keys(lb, hk)
            known = self._known.get(k, set())
            self._done[k] = merged | {key for key in known if parse_key(key).parquet_name in names}
        return self._done[k]

    def _sync_hour(self, lb: str, hk: str, keys: set[str], now: datetime) -> None:
        done = self.done_keys(lb, hk)
        missing = sorted(keys - done)
        finished = now >= hour_of_key(hk) + timedelta(hours=1) + self.SETTLE
        merged = self.store.keys_file(lb, hk).exists()
        raw = self.store.raw_files(lb, hk)
        if finished and not merged and not raw and missing:
            self._convert_hour(lb, hk, missing)                 # whole hour in one go
        elif missing:
            for key in missing:                                 # recent data: file by file
                self._convert_one(lb, hk, key)
            if finished:
                self._merge(lb, hk)
        elif finished and raw:
            self._merge(lb, hk)

    # -- conversions
    def _db(self) -> duckdb.DuckDBPyConnection:
        con = duckdb.connect(config={"threads": 1, "memory_limit": f"{self.settings.lb_converter_memory_mb}MB",
                                     "preserve_insertion_order": False})
        con.execute("SET enable_progress_bar = false")
        tmp = self.store.tmp_dir / "duckdb"
        tmp.mkdir(parents=True, exist_ok=True)
        con.execute(f"SET temp_directory = '{tmp.as_posix()}'")
        return con

    def _fetch_all(self, keys: list[str], tmp: Path) -> list[Path]:
        tmp.mkdir(parents=True, exist_ok=True)
        return list(self._pool.map(lambda k: self.source.fetch(k, tmp), keys))

    def _convert_one(self, lb: str, hk: str, key: str) -> None:
        f = parse_key(key)
        tmp = self.store.tmp_dir / f"one-{os.getpid()}"
        local = self._fetch_all([key], tmp)[0]
        dest = self.store.raw_dir(lb, hk) / f.parquet_name
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(f".{dest.name}.part")
        con = self._db()
        try:
            con.execute(f"COPY ({convert_sql(_sql_list([local]))}) TO '{part.as_posix()}' "
                        "(FORMAT parquet, COMPRESSION zstd)")
            os.replace(part, dest)
            self.done_keys(lb, hk).add(key)
            self.converted_files += 1
        finally:
            con.close()
            self.source.cleanup(local)
            part.unlink(missing_ok=True)

    def _convert_hour(self, lb: str, hk: str, keys: list[str]) -> None:
        tmp = self.store.tmp_dir / f"hour-{os.getpid()}"
        locals_ = self._fetch_all(keys, tmp)
        try:
            self._write_hour(lb, hk, convert_sql(_sql_list(locals_)), set(keys))
            self.converted_files += len(keys)
        finally:
            for p in locals_:
                self.source.cleanup(p)

    def _merge(self, lb: str, hk: str) -> None:
        """Fold the hour's 5-minute files (and an older merged file, for late arrivals) into one."""
        raw = self.store.raw_files(lb, hk)
        if not raw:
            return
        hour = self.store.hour_file(lb, hk)
        if self.store.keys_file(lb, hk).exists() and not hour.exists():
            self.converted.get(rel("hour", lb, hk), hour)       # merged before; the cache let it go
        parts = raw + ([hour] if hour.exists() else [])
        keys = self.store.merged_keys(lb, hk) | {k for k in self._known.get((lb, hk), set())
                                                  if parse_key(k).parquet_name in {p.name for p in raw}}
        self._write_hour(lb, hk, f"SELECT {', '.join(REQUEST_COLUMNS)} FROM read_parquet({_sql_list(parts)}, "
                                 "union_by_name = true)", keys, retire=self.store.raw_dir(lb, hk))

    def _write_hour(self, lb: str, hk: str, select_sql: str, keys: set[str], retire: Path | None = None) -> None:
        """Hour, minute and paths files: written here, stored in S3 (keys last: that marks the hour
        done), then put in the cache. Nothing changes locally if S3 refuses."""
        tag = f"{os.getpid()}.{threading.get_ident()}"
        finals = {k: self.store.file(k, lb, hk) for k in ("hour", "minute", "paths", "keys")}
        for p in finals.values():
            p.parent.mkdir(parents=True, exist_ok=True)
        parts = {k: p.with_name(f".{p.name}.{tag}.part") for k, p in finals.items()}
        con = self._db()
        try:
            con.execute(f"COPY ({select_sql}) TO '{parts['hour'].as_posix()}' (FORMAT parquet, COMPRESSION zstd, "
                        "ROW_GROUP_SIZE 100000)")
            src = f"read_parquet({_sql_list([parts['hour']])})"
            con.execute(f"COPY ({rollup_sql(src)}) TO '{parts['minute'].as_posix()}' (FORMAT parquet, COMPRESSION zstd)")
            con.execute(f"COPY ({paths_rollup_sql(src)}) TO '{parts['paths'].as_posix()}' "
                        "(FORMAT parquet, COMPRESSION zstd)")
            parts["keys"].write_text(json.dumps(sorted(keys), separators=(",", ":")))
            for kind in ("hour", "paths", "minute", "keys"):
                self.converted.put(parts[kind], rel(kind, lb, hk))
            if retire is not None and retire.exists():   # a search reads either these or the hour file
                self.store.trash_dir.mkdir(parents=True, exist_ok=True)
                os.replace(retire, self.store.trash_dir / f"{lb}-{hk}-{time.time_ns()}")
            for kind in ("minute", "paths", "hour", "keys"):
                os.replace(parts[kind], finals[kind])
            self._done[(lb, hk)] = set(keys)
        finally:
            con.close()
            for p in parts.values():
                p.unlink(missing_ok=True)
        evict_cache(self.store, self.settings.lb_cache_max_mb)

    # -- older ranges asked for by searches
    def process_wanted(self) -> bool:
        d = self.store.wanted_dir
        if not d.is_dir():
            return False
        worked = False
        now = self.now()
        oldest = now - timedelta(days=self.settings.lb_retention_days)
        for p in sorted(d.glob("*.json")):
            try:
                req = json.loads(p.read_text())
            except (OSError, ValueError):
                p.unlink(missing_ok=True)
                continue
            if time.time() - req.get("at", 0) > 1800:
                p.unlink(missing_ok=True)
                continue
            start = max(datetime.fromtimestamp(req["start"] / 1000, timezone.utc), oldest)
            end = min(datetime.fromtimestamp(req["end"] / 1000, timezone.utc), now)
            if start >= end:
                p.unlink(missing_ok=True)
                continue
            wanted_hours = set(hours_in(int(start.timestamp() * 1000), int(end.timestamp() * 1000)))
            for f in self._listed(self._days(start - timedelta(hours=1), end + timedelta(hours=1))):
                if f.hour in wanted_hours:
                    self._known.setdefault((f.lb, f.hour), set()).add(f.key)
            todo = [(lb, hk) for (lb, hk) in self._known if hk in wanted_hours
                    and set(self._known[(lb, hk)]) - self.done_keys(lb, hk)]
            for lb, hk in sorted(todo, key=lambda x: x[1], reverse=True):
                self._sync_hour(lb, hk, self._known[(lb, hk)], now)
                worked = True
                self.write_status()
                if self._stop.is_set():
                    return worked
            p.unlink(missing_ok=True)
        return worked

    # -- housekeeping
    def _empty_trash(self) -> None:
        d = self.store.trash_dir
        if d.is_dir():
            for p in d.iterdir():
                try:
                    if time.time() - p.stat().st_mtime > 120:
                        shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
                except OSError:
                    pass

    def _prune(self, now: datetime) -> None:
        """Local copies past the retention go (S3 removes its own with a lifecycle rule); the cache of
        hour and paths files is kept under LB_CACHE_MAX_MB."""
        keep_until = now - timedelta(days=self.settings.lb_retention_days)
        for kind in ("hour", "minute", "paths", "keys", "raw"):
            root = self.store.root / kind
            if not root.is_dir():
                continue
            for lb_dir in root.iterdir():
                for day_dir in (lb_dir.iterdir() if lb_dir.is_dir() else []):
                    try:
                        old = datetime.strptime(day_dir.name, "%Y-%m-%d").replace(tzinfo=timezone.utc) \
                            + timedelta(days=1) < keep_until
                    except ValueError:
                        continue
                    if old:
                        shutil.rmtree(day_dir, ignore_errors=True)
        for k in [k for k in self._known if hour_of_key(k[1]) + timedelta(hours=1) < keep_until]:
            self._known.pop(k, None)
            self._done.pop(k, None)
        evict_cache(self.store, self.settings.lb_cache_max_mb, force=True)

    def write_status(self) -> None:
        hours = {f"{lb}|{hk}": [len(keys), len(keys & self.done_keys(lb, hk))]
                 for (lb, hk), keys in self._known.items()}
        _write_json(self.store.status_file, {
            "updatedAt": _iso(), "pid": os.getpid(), "source": self.source.describe(),
            "converted": self.converted.describe(),
            "warmDays": self.settings.lb_warm_days, "pollSeconds": self.settings.lb_poll_seconds,
            "cycleMs": self._cycle_ms, "lastError": self._last_error,
            "lbs": sorted({lb for lb, _ in self._known}), "hours": hours,
            "days": sorted({d for _, d in self._listings})})


# ------------------------------------------------------------------ queries
CLASSES = ("2xx", "3xx", "4xx", "5xx")


@dataclass
class LbQuery:
    start: Any = "now-1h"
    end: Any = "now"
    lbs: list[str] = field(default_factory=list)
    target_groups: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)
    status_classes: list[str] = field(default_factory=list)    # "2xx".."5xx", "other"
    status_codes: list[int] = field(default_factory=list)
    source: str | None = None                                   # "app" | "lb"
    methods: list[str] = field(default_factory=list)
    path: str = ""                                              # contains; * = anything
    path_group: str = ""                                        # exact grouped path
    client: str = ""                                            # IP prefix
    target: str = ""                                            # target ip:port prefix
    min_target_seconds: float | None = None
    q: str = ""

    def level(self) -> str:
        """The smallest files that can answer: "minute" (counts per minute), "paths" (also per method
        and grouped path) or "rows" (single requests)."""
        if self.path or self.client or self.target or self.min_target_seconds is not None or self.q:
            return "rows"
        if self.methods or self.path_group:
            return "rows" if self.domains else "paths"
        return "minute"


@dataclass
class Access:
    """A user's cluster rules as SQL (same meaning as identity.cluster_level); None = admin."""
    star: bool
    view: list[str]
    deny: list[str]

    @classmethod
    def for_rules(cls, rules: dict[str, str]) -> "Access":
        return cls(star=rules.get("*") == "view",
                   view=[k for k, v in rules.items() if k != "*" and v == "view"],
                   deny=[k for k, v in rules.items() if k != "*" and v != "view"])


def _like(text: str) -> str:
    esc = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return esc.replace("*", "%")


class _Where:
    def __init__(self, time_col: str):
        self.time_col = time_col
        self.sql: list[str] = []
        self.params: list[Any] = []

    def add(self, sql: str, *params) -> None:
        self.sql.append(sql)
        self.params.extend(params)

    def text(self) -> str:
        return " AND ".join(f"({s})" for s in self.sql) or "TRUE"


def build_where(q: LbQuery, start_ms: int, end_ms: int, access: Access | None, source: str) -> _Where:
    """WHERE for "minute" / "paths" roll-ups (filters they hold) or "rows" (every filter)."""
    rollup = source != "rows"
    w = _Where("minute_ms" if rollup else "ts_ms")
    if rollup:
        w.add("minute_ms BETWEEN ? AND ?", (start_ms // MINUTE) * MINUTE, end_ms)
    else:
        w.add("ts_ms BETWEEN ? AND ?", start_ms, end_ms)
    if access is not None:
        if access.star:
            w.add("cluster IS NOT NULL AND NOT list_contains(?::VARCHAR[], cluster)", access.deny)
        else:
            w.add("list_contains(?::VARCHAR[], cluster)", access.view)
    if q.lbs:
        w.add("list_contains(?::VARCHAR[], lb)", q.lbs)
    if q.target_groups:
        named = [t for t in q.target_groups if t != "-"]
        cond = "list_contains(?::VARCHAR[], tg)" + (" OR tg IS NULL" if "-" in q.target_groups else "")
        w.add(cond, named)
    if q.domains and source != "paths":
        w.add("list_contains(?::VARCHAR[], domain)", q.domains)
    if q.status_classes:
        parts = []
        nums = [int(c[0]) for c in q.status_classes if c in CLASSES]
        if nums:
            parts.append("list_contains(?::INTEGER[], (elb_code // 100)::INTEGER)")
            w.params.append(nums)
        if "other" in q.status_classes:
            parts.append("elb_code IS NULL OR elb_code < 200 OR elb_code >= 600")
        if not parts:
            raise bad_request("INVALID_FILTER", "statusClasses are 2xx, 3xx, 4xx, 5xx or other")
        w.sql.append(" OR ".join(f"({p})" for p in parts))
    if q.status_codes:
        w.add("list_contains(?::INTEGER[], elb_code::INTEGER)", [int(c) for c in q.status_codes])
    if q.source == "app":
        w.add("tgt_code IS NOT NULL")
    elif q.source == "lb":
        w.add("tgt_code IS NULL")
    if source == "minute":
        return w
    if q.methods:
        w.add("list_contains(?::VARCHAR[], method)", [m.upper() for m in q.methods])
    if q.path_group:
        w.add("path_group = ?", q.path_group)
    if source == "paths":
        return w
    if q.path:
        pat = _like(q.path)
        w.add("path ILIKE ? ESCAPE '\\'", pat if "*" in q.path else f"%{pat}%")
    if q.client:
        w.add("client_ip LIKE ? ESCAPE '\\'", _like(q.client) + "%")
    if q.target:
        w.add("target LIKE ? ESCAPE '\\'", _like(q.target) + "%")
    if q.min_target_seconds is not None:
        w.add("tgt_t >= ?", float(q.min_target_seconds))
    for word in re.findall(r'"[^"]+"|\S+', q.q or ""):      # every word, anywhere in the original line
        neg = word.startswith("-") and len(word) > 1
        word = word[1:] if neg else word
        word = word.strip('"')
        if word:
            w.add(f"{'NOT ' if neg else ''}coalesce(raw ILIKE ? ESCAPE '\\', false)", f"%{_like(word)}%")
    return w


@dataclass
class Sources:
    hours: list[tuple[str, str]]   # (lb, hour) converted and in S3; minute files are always local
    minutes: list[Path]
    raws: list[Path]               # 5-minute files of hours not merged yet (or late), local

    @property
    def count(self) -> int:
        return len(self.hours) + len(self.raws)


_EMPTY_ROLLUP = ("SELECT NULL::BIGINT AS minute_ms, NULL::VARCHAR AS lb, NULL::VARCHAR AS tg, NULL::VARCHAR AS cluster, "
                 "NULL::VARCHAR AS domain, NULL::SMALLINT AS elb_code, NULL::SMALLINT AS tgt_code, NULL::TINYINT AS lat_bin, "
                 "0::BIGINT AS n, 0::DOUBLE AS tgt_t_sum, 0::BIGINT AS tgt_t_n WHERE FALSE")


class LbService:
    KEEP_RECENT_SECONDS = 600          # cached files used this recently are never removed

    def __init__(self, settings: Settings, converted=None):
        self.settings = settings
        self.store = LbStore(Path(settings.cache_dir) / "lb")
        self.converted = converted or build_converted(settings)
        self._pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="lb-get")

    # -- ranges and files
    def resolve_range(self, start, end) -> tuple[int, int]:
        now_ms = int(time.time() * 1000)
        s, e = parse_time(start, now_ms, "start"), parse_time(end, now_ms, "end")
        if e <= s:
            raise bad_request("INVALID_TIME", "'end' must be after 'start'")
        if e - s > self.settings.max_search_hours * HOUR + MINUTE:
            raise bad_request("RANGE_TOO_LARGE", f"One view can cover at most {self.settings.max_search_hours} "
                              f"hours ({self.settings.max_search_hours // 24} days)",
                              {"maxHours": self.settings.max_search_hours})
        return s, e

    def sources(self, start_ms: int, end_ms: int, lbs: list[str]) -> Sources:
        names = [n for n in (lbs or self.store.lbs()) if LB_NAME_RE.match(n)]
        src = Sources([], [], [])
        for lb in names:
            for hk in hours_in(start_ms - 10 * MINUTE, end_ms):
                minute = self.store.minute_file(lb, hk)
                if minute.exists():
                    src.hours.append((lb, hk))
                    src.minutes.append(minute)
                src.raws += self.store.raw_files(lb, hk)
        return src

    def local(self, kind: str, items: list[tuple[str, str]]) -> list[Path]:
        """Cached copies of these hour or paths files, fetched from S3 when missing."""
        paths = {item: self.store.file(kind, *item) for item in items}
        need = [item for item, p in paths.items() if not p.exists()]
        if kind == "hour" and len(need) > self.settings.lb_max_fetch_files:
            raise bad_request("TOO_MUCH_DATA",
                              f"This needs {len(need):,} hours of single requests that are not on the server yet "
                              f"(at most {self.settings.lb_max_fetch_files:,} at a time). Pick a load balancer or "
                              "target group, or a shorter range.",
                              {"hours": len(need), "max": self.settings.lb_max_fetch_files})
        list(self._pool.map(lambda item: self.converted.get(rel(kind, *item), paths[item]), need))
        now = time.time()
        out = []
        for p in paths.values():
            try:
                os.utime(p, (now, now))      # recently used: the cache keeps it
                out.append(p)
            except OSError:
                pass                         # not in S3 (yet): nothing to read
        if need:
            evict_cache(self.store, self.settings.lb_cache_max_mb, self.KEEP_RECENT_SECONDS)
        return out

    def pending(self, start_ms: int, end_ms: int, lbs: list[str]) -> dict:
        """Files AWS has delivered for this range that are not converted yet."""
        status = self.store.read_status()
        hours = status.get("hours", {})
        names = lbs or status.get("lbs") or self.store.lbs()
        now = time.time()
        warm_from = now - self.settings.lb_warm_days * 86400
        listed = done = 0
        unknown_old = False
        listed_days = set(status.get("days", []))
        for hk in hours_in(start_ms, end_ms):
            h_end = hour_of_key(hk).timestamp() + 3600
            if h_end > now - 1200:
                continue      # the last few minutes are always still arriving
            found = False
            for lb in names:
                entry = hours.get(f"{lb}|{hk}")
                if entry:
                    found = True
                    listed += entry[0]
                    done += min(entry[1], entry[0])
            if not found and h_end < warm_from and hk[:10] not in listed_days:
                unknown_old = True
        out = {"files": max(0, listed - done), "of": listed, "converterUpdatedAt": status.get("updatedAt"),
               "lastError": status.get("lastError")}
        if unknown_old or (out["files"] and start_ms / 1000 < warm_from):
            self._want(start_ms, end_ms)
            out["requested"] = True
        if not status:
            out["starting"] = True
        return out

    def _want(self, start_ms: int, end_ms: int) -> None:
        key = hashlib.sha1(f"{start_ms // HOUR}-{end_ms // HOUR}".encode()).hexdigest()[:16]
        try:
            _write_json(self.store.wanted_dir / f"{key}.json", {"start": start_ms, "end": end_ms, "at": time.time()})
        except OSError:
            log.warning("could not queue an on-demand conversion", exc_info=True)

    # -- duckdb
    def _db(self) -> duckdb.DuckDBPyConnection:
        con = duckdb.connect(config={"threads": max(1, min(self.settings.duckdb_threads, 4)),
                                     "memory_limit": f"{self.settings.duckdb_memory_mb}MB",
                                     "preserve_insertion_order": False})
        con.execute("SET enable_progress_bar = false")
        tmp = Path(self.settings.cache_dir) / ".duckdb-tmp"
        try:
            tmp.mkdir(parents=True, exist_ok=True)
            con.execute(f"SET temp_directory = '{tmp.as_posix()}'")
        except (OSError, duckdb.Error):
            pass
        return con

    @staticmethod
    def _rows_sql(paths: list[Path], filename: bool = False) -> str:
        return f"read_parquet({_sql_list(paths)}, union_by_name = true{', filename = true' if filename else ''})"

    def _run(self, fn):
        """DuckDB errors as API errors; one retry if a file was merged or evicted mid-query."""
        for attempt in (1, 2):
            try:
                return fn()
            except duckdb.IOException as e:
                if attempt == 2:
                    raise ApiError(503, "LB_FILES_CHANGED", f"The files changed during the query; retry ({e})") from e
                time.sleep(0.2)
            except duckdb.Error as e:
                msg = str(e).splitlines()[0][:400]
                if "memory" in msg.lower():
                    raise ApiError(507, "SEARCH_TOO_BIG", "The query needed more memory than allowed "
                                   "(DUCKDB_MEMORY_MB); pick a shorter range or add filters", {"detail": msg}) from e
                raise ApiError(500, "LB_QUERY_FAILED", f"The query failed: {msg}") from e

    def _rollup_table(self, con, q: LbQuery, src: Sources, s: int, e: int, access: Access | None) -> str:
        """Temp table m (ROLLUP_COLUMNS) for the query, from the smallest files that hold its filters."""
        level = q.level()
        wr = build_where(q, s, e, access, "rows")
        parts, params = [], []
        if level == "minute" and src.minutes:
            w = build_where(q, s, e, access, "minute")
            parts.append(f"SELECT {', '.join(ROLLUP_COLUMNS)} FROM read_parquet({_sql_list(src.minutes)}) "
                         f"WHERE {w.text()}")
            params += w.params
        elif level == "paths" and src.hours:
            files = self.local("paths", src.hours)
            if files:
                w = build_where(q, s, e, access, "paths")
                cols = ", ".join("NULL::VARCHAR AS domain" if c == "domain" else c for c in ROLLUP_COLUMNS)
                parts.append(f"SELECT {cols} FROM read_parquet({_sql_list(files)}) WHERE {w.text()}")
                params += w.params
        elif level == "rows" and src.hours:
            files = self.local("hour", src.hours)
            if files:
                parts.append(rollup_sql(f"(SELECT * FROM {self._rows_sql(files)} WHERE {wr.text()})"))
                params += wr.params
        if src.raws:
            parts.append(rollup_sql(f"(SELECT * FROM {self._rows_sql(src.raws)} WHERE {wr.text()})"))
            params += wr.params
        con.execute(f"CREATE TEMP TABLE m AS {' UNION ALL '.join(parts) if parts else _EMPTY_ROLLUP}", params)
        return level

    def _paths_table(self, con, q: LbQuery, src: Sources, s: int, e: int, access: Access | None) -> None:
        """Temp table pt (PATHS_COLUMNS) for the query."""
        wr = build_where(q, s, e, access, "rows")
        parts, params = [], []
        if q.level() == "rows" or q.domains:
            files = self.local("hour", src.hours) if src.hours else []
            if files:
                parts.append(paths_rollup_sql(f"(SELECT * FROM {self._rows_sql(files)} WHERE {wr.text()})"))
                params += wr.params
        elif src.hours:
            files = self.local("paths", src.hours)
            if files:
                w = build_where(q, s, e, access, "paths")
                parts.append(f"SELECT {', '.join(PATHS_COLUMNS)} FROM read_parquet({_sql_list(files)}) WHERE {w.text()}")
                params += w.params
        if src.raws:
            parts.append(paths_rollup_sql(f"(SELECT * FROM {self._rows_sql(src.raws)} WHERE {wr.text()})"))
            params += wr.params
        empty = ("SELECT " + ", ".join(f"NULL::{t} AS {c}" for c, t in zip(
            PATHS_COLUMNS, ["BIGINT", "VARCHAR", "VARCHAR", "VARCHAR", "VARCHAR", "VARCHAR", "SMALLINT", "SMALLINT",
                            "TINYINT", "BIGINT", "DOUBLE", "BIGINT"])) + " WHERE FALSE")
        con.execute(f"CREATE TEMP TABLE pt AS {' UNION ALL '.join(parts) if parts else empty}", params)

    # -- the page
    def summary(self, q: LbQuery, access: Access | None) -> dict:
        t0 = time.monotonic()
        s, e = self.resolve_range(q.start, q.end)
        src = self.sources(s, e, q.lbs)
        step = histogram_step(e - s)
        out = {"start": s, "end": e, "interval": step, "files": src.count, "pending": self.pending(s, e, q.lbs)}
        empty_totals = {"requests": 0, **{c: 0 for c in CLASSES}, "other": 0, "s5xxApp": 0, "s5xxLb": 0,
                        "p50": None, "p95": None, "p99": None, "avg": None}
        if not src.count:
            return {**out, "source": "none", "totals": empty_totals, "buckets": [], "targetGroups": [],
                    "tookMs": int((time.monotonic() - t0) * 1000)}

        def go():
            con = self._db()
            try:
                out["source"] = self._rollup_table(con, q, src, s, e, access)
                cls = ", ".join(f"coalesce(sum(n) FILTER (WHERE elb_code BETWEEN {c[0]}00 AND {c[0]}99), 0) AS \"{c}\""
                                for c in CLASSES)
                other = "coalesce(sum(n) FILTER (WHERE elb_code IS NULL OR elb_code < 200 OR elb_code >= 600), 0) AS other"
                t = con.execute(f"SELECT coalesce(sum(n), 0), {cls}, {other}, "
                                "coalesce(sum(n) FILTER (WHERE elb_code >= 500 AND tgt_code IS NOT NULL), 0), "
                                "coalesce(sum(n) FILTER (WHERE elb_code >= 500 AND tgt_code IS NULL), 0), "
                                "sum(tgt_t_sum) / nullif(sum(tgt_t_n), 0) FROM m").fetchone()
                totals = {"requests": t[0], **{c: t[1 + i] for i, c in enumerate(CLASSES)}, "other": t[5],
                          "s5xxApp": t[6], "s5xxLb": t[7], "avg": t[8]}
                buckets = [{"t": r[0], **{c: r[1 + i] for i, c in enumerate(CLASSES)}, "other": r[5]}
                           for r in con.execute(f"SELECT (minute_ms // ?) * ? AS t, {cls}, {other} FROM m "
                                                "WHERE minute_ms IS NOT NULL GROUP BY 1 ORDER BY 1",
                                                [step, step]).fetchall()]
                tgs = con.execute("""SELECT tg, any_value(cluster), list(DISTINCT lb ORDER BY lb), sum(n),
                        coalesce(sum(n) FILTER (WHERE elb_code BETWEEN 400 AND 499), 0),
                        coalesce(sum(n) FILTER (WHERE elb_code >= 500 AND tgt_code IS NOT NULL), 0),
                        coalesce(sum(n) FILTER (WHERE elb_code >= 500 AND tgt_code IS NULL), 0),
                        sum(tgt_t_sum) / nullif(sum(tgt_t_n), 0)
                        FROM m GROUP BY tg""").fetchall()
                rows = {r[0]: {"tg": r[0], "cluster": r[1], "lbs": r[2], "requests": r[3], "s4xx": r[4],
                               "s5xxApp": r[5], "s5xxLb": r[6], "s5xx": r[5] + r[6], "avg": r[7], "p95": None,
                               "topError": None} for r in tgs}
                overall: dict[int, int] = {}
                per_tg: dict[Any, dict[int, int]] = {}
                for tg, b, n in con.execute("SELECT tg, lat_bin, sum(n) FROM m WHERE lat_bin IS NOT NULL "
                                            "GROUP BY 1, 2").fetchall():
                    overall[b] = overall.get(b, 0) + n
                    per_tg.setdefault(tg, {})[b] = n
                totals.update(p50=percentile(overall, 0.5), p95=percentile(overall, 0.95),
                              p99=percentile(overall, 0.99))
                for tg, bins in per_tg.items():
                    if tg in rows:
                        rows[tg]["p95"] = percentile(bins, 0.95)
                # the top failing path per target group, from the paths roll-up
                if not (q.domains and q.level() == "minute"):
                    self._paths_table(con, q, src, s, e, access)
                    for tg, method, pg, code, n in con.execute("""
                            SELECT tg, method, path_group, code, total FROM (
                              SELECT tg, method, path_group, arg_max(elb_code, cn) AS code, sum(cn) AS total,
                                     row_number() OVER (PARTITION BY tg ORDER BY sum(cn) DESC, path_group) AS rn
                              FROM (SELECT tg, method, path_group, elb_code, sum(n) AS cn FROM pt
                                    WHERE elb_code >= 400 GROUP BY ALL)
                              GROUP BY tg, method, path_group) WHERE rn = 1""").fetchall():
                        if tg in rows:
                            rows[tg]["topError"] = {"method": method, "pathGroup": pg, "code": code, "count": n}
                out["totals"] = totals
                out["buckets"] = buckets
                out["targetGroups"] = sorted(rows.values(), key=lambda r: (-(r["s5xx"]), -(r["s4xx"]),
                                                                          -(r["requests"]), r["tg"] or "~"))
            finally:
                con.close()
        self._run(go)
        out["tookMs"] = int((time.monotonic() - t0) * 1000)
        return out

    def paths(self, q: LbQuery, access: Access | None, sort: str = "errors", limit: int = 50) -> dict:
        t0 = time.monotonic()
        s, e = self.resolve_range(q.start, q.end)
        src = self.sources(s, e, q.lbs)
        if not src.count:
            return {"items": [], "groups": 0, "tookMs": 0}
        order = {"errors": "s5xx DESC, s4xx DESC, requests DESC", "requests": "requests DESC",
                 "slow": "p95 DESC NULLS LAST, requests DESC"}.get(sort, "requests DESC")
        cls = ", ".join(f"coalesce(sum(n) FILTER (WHERE elb_code BETWEEN {c[0]}00 AND {c[0]}99), 0) AS \"{c}\""
                        for c in CLASSES)

        def go():
            con = self._db()
            try:
                self._paths_table(con, q, src, s, e, access)
                # p95 per path from its latency bins, interpolated like the totals
                con.execute(f"""CREATE TEMP TABLE pb AS SELECT method, path_group, lat_bin, sum(n) AS n FROM pt
                                WHERE lat_bin IS NOT NULL GROUP BY ALL""")
                bins: dict[tuple, dict[int, int]] = {}
                for m, pg, b, n in con.execute("SELECT * FROM pb").fetchall():
                    bins.setdefault((m, pg), {})[b] = n
                con.execute("CREATE TEMP TABLE p95 (method VARCHAR, path_group VARCHAR, p95 DOUBLE)")
                if bins:
                    con.executemany("INSERT INTO p95 VALUES (?, ?, ?)",
                                    [[m, pg, percentile(b, 0.95)] for (m, pg), b in bins.items()])
                cur = con.execute(f"""
                    SELECT g.*, p.p95 FROM (
                      SELECT method, path_group, sum(n) AS requests, {cls},
                             coalesce(sum(n) FILTER (WHERE elb_code >= 500), 0) AS s5xx,
                             coalesce(sum(n) FILTER (WHERE elb_code BETWEEN 400 AND 499), 0) AS s4xx,
                             sum(tgt_t_sum) / nullif(sum(tgt_t_n), 0) AS avg,
                             list(DISTINCT tg ORDER BY tg)[1:3] AS tgs, count(*) OVER () AS groups
                      FROM pt GROUP BY method, path_group) g
                    LEFT JOIN p95 p ON p.method IS NOT DISTINCT FROM g.method AND p.path_group IS NOT DISTINCT FROM g.path_group
                    ORDER BY {order}, g.path_group LIMIT ?""", [int(limit)])
                names = [d[0] for d in cur.description]
                rows = [dict(zip(names, r)) for r in cur.fetchall()]
                codes = {(m, pg): c for m, pg, c in con.execute(
                    "SELECT method, path_group, arg_max(elb_code, cn) FROM (SELECT method, path_group, elb_code, "
                    "sum(n) AS cn FROM pt GROUP BY ALL) GROUP BY ALL").fetchall()}
            finally:
                con.close()
            return rows, codes
        rows, codes = self._run(go)
        groups = rows[0]["groups"] if rows else 0
        items = [{"method": r["method"], "pathGroup": r["path_group"], "example": None,
                  "requests": r["requests"], **{c: r[c] for c in CLASSES}, "p95": r["p95"], "avg": r["avg"],
                  "topCode": codes.get((r["method"], r["path_group"])), "targetGroups": r["tgs"]} for r in rows]
        return {"items": items, "groups": groups, "tookMs": int((time.monotonic() - t0) * 1000)}

    def requests(self, q: LbQuery, access: Access | None, offset: int = 0, size: int = 100,
                 order: str = "desc") -> dict:
        """One page of single requests. Counts per hour come from the roll-ups when the filters allow
        (no download); then only the hour files holding the page are read."""
        t0 = time.monotonic()
        s, e = self.resolve_range(q.start, q.end)
        src = self.sources(s, e, q.lbs)
        if not src.count:
            return {"start": s, "end": e, "total": 0, "hits": [], "tookMs": 0}
        level = q.level()
        wr = build_where(q, s, e, access, "rows")
        desc = order != "asc"

        def go():
            con = self._db()
            try:
                counts: dict[tuple[str, str], int] = {}       # (lb, hour) -> matching requests
                files: dict[tuple[str, str], list[Path]] = {}  # (lb, hour) -> files to read rows from
                if src.hours:
                    if level in ("minute", "paths"):
                        kind = "minute" if level == "minute" else "paths"
                        roll = src.minutes if kind == "minute" else self.local("paths", src.hours)
                        w = build_where(q, s, e, access, kind)
                        for f, n in con.execute(f"SELECT filename, sum(n) FROM read_parquet({_sql_list(roll)}, "
                                                f"filename = true) WHERE {w.text()} GROUP BY 1", w.params).fetchall():
                            k = (_lb_of_path(f), _hour_of_path(f))
                            counts[k] = counts.get(k, 0) + int(n)
                            files.setdefault(k, [])
                    else:
                        local = self.local("hour", src.hours)
                        for f, n in con.execute(f"SELECT filename, count(*) FROM {self._rows_sql(local, True)} "
                                                f"WHERE {wr.text()} GROUP BY 1", wr.params).fetchall():
                            k = (_lb_of_path(f), _hour_of_path(f))
                            counts[k] = counts.get(k, 0) + int(n)
                            files.setdefault(k, []).append(Path(f))
                if src.raws:
                    for f, n in con.execute(f"SELECT filename, count(*) FROM {self._rows_sql(src.raws, True)} "
                                            f"WHERE {wr.text()} GROUP BY 1", wr.params).fetchall():
                        k = (_lb_of_path(f), _hour_of_path(f))
                        counts[k] = counts.get(k, 0) + int(n)
                        files.setdefault(k, []).append(Path(f))
                total = sum(counts.values())
                by_hour: dict[str, list[tuple[str, str]]] = {}
                for k in counts:
                    by_hour.setdefault(k[1], []).append(k)
                chosen, before, seen = [], 0, 0
                for hk in sorted(by_hour, reverse=desc):
                    n = sum(counts[k] for k in by_hour[hk])
                    if n and seen + n > offset and seen < offset + size:
                        if not chosen:
                            before = seen
                        chosen += by_hour[hk]
                    seen += n
                    if seen >= offset + size:
                        break
                if not chosen:
                    return total, []
                read: list[Path] = []
                need_hours = [k for k in chosen if k in set(src.hours) and not any(
                    p.parts[-4] == "hour" for p in files.get(k, []))]
                hour_paths = dict(zip(need_hours, [self.store.hour_file(*k) for k in need_hours]))
                if need_hours:
                    self.local("hour", need_hours)
                for k in chosen:
                    read += files.get(k, [])
                    if k in hour_paths and hour_paths[k].exists():
                        read.append(hour_paths[k])
                d = "DESC" if desc else "ASC"
                cur = con.execute(f"SELECT {', '.join(REQUEST_COLUMNS)} FROM {self._rows_sql(read)} "
                                  f"WHERE {wr.text()} ORDER BY ts_ms {d}, trace_id LIMIT ? OFFSET ?",
                                  [*wr.params, size, offset - before])
                names = [x[0] for x in cur.description]
                return total, [{k: v for k, v in zip(names, r) if v is not None} for r in cur.fetchall()]
            finally:
                con.close()
        total, hits = self._run(go)
        return {"start": s, "end": e, "total": total, "hits": hits, "tookMs": int((time.monotonic() - t0) * 1000)}

    def export(self, q: LbQuery, access: Access | None, fmt: str, limit: int) -> tuple[Iterator[bytes], int]:
        res = self.requests(q, access, 0, max(1, min(int(limit), self.settings.max_export_rows)))
        rows = [tuple(h.get(c) for c in REQUEST_COLUMNS) for h in res["hits"]]
        return _encode(fmt, REQUEST_COLUMNS, rows), len(rows)

    def overview(self, access: Access | None) -> dict:
        """Load balancers and their target groups seen in the last 24 hours, and the converter state."""
        now_ms = int(time.time() * 1000)
        s = now_ms - 24 * HOUR
        src = self.sources(s, now_ms, [])
        status = self.store.read_status()
        lbs: dict[str, dict] = {lb: {"name": lb, "targetGroups": []} for lb in (status.get("lbs") or self.store.lbs())}
        if src.count:
            def go():
                con = self._db()
                try:
                    self._rollup_table(con, LbQuery(start=s, end=now_ms), src, s, now_ms, access)
                    return con.execute("SELECT lb, tg, any_value(cluster), sum(n) FROM m WHERE lb IS NOT NULL "
                                       "GROUP BY lb, tg ORDER BY lb, 4 DESC").fetchall()
                finally:
                    con.close()
            for lb, tg, cluster, n in self._run(go):
                lbs.setdefault(lb, {"name": lb, "targetGroups": []})["targetGroups"].append(
                    {"name": tg, "cluster": cluster, "requests24h": n})
        items = list(lbs.values())
        if access is not None:   # members: only load balancers that serve one of their clusters
            items = [x for x in items if x["targetGroups"]]
        hours = status.get("hours", {})
        warm_from = time.time() - self.settings.lb_warm_days * 86400
        warm = [v for k, v in hours.items() if hour_of_key(k.split("|")[1]).timestamp() + 3600 > warm_from]
        return {"items": items, "converter": {
            "updatedAt": status.get("updatedAt"), "lastError": status.get("lastError"),
            "warmDays": self.settings.lb_warm_days, "pollSeconds": self.settings.lb_poll_seconds,
            "filesListed": sum(v[0] for v in warm), "filesConverted": sum(min(v[1], v[0]) for v in warm),
            "cycleMs": status.get("cycleMs"), "converted": status.get("converted")}}
