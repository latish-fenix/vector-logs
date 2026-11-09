#!/usr/bin/env python3
"""Copy API Gateway per-stage metrics from CloudWatch to S3, one row per stage per minute.

Run it every 5 minutes (cron). Each run reads the last LOOKBACK minutes (CloudWatch fills in
late data for a few minutes) and merges them into the 4-hour file they belong to, so S3 holds
exactly 6 files per day; the same file is rewritten until its 4 hours are over.

    s3://<bucket>/<prefix>dt=YYYY-MM-DD/part-HH.csv.gz      HH = 00, 04, 08, 12, 16, 20 (UTC)

Columns (one row per minute, API and stage; times in UTC):
    minute, api, stage, calls, err_4xx, err_5xx, latency_p50_ms, latency_p95_ms, integration_p95_ms

Needs: Python 3.9+, boto3. AWS access: cloudwatch:GetMetricData, and s3:GetObject / s3:PutObject
on the prefix (s3:ListBucket on the prefix too, so a missing file reads as "not there yet").

Examples
    python3 apigw_metrics_to_s3.py                      # normal 5-minute run
    python3 apigw_metrics_to_s3.py --backfill-days 8    # one-off: fill the last 8 days
    python3 apigw_metrics_to_s3.py --dry-run            # print what would be written, no S3 writes
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import io
import os
import sys
import time

import boto3
from botocore.exceptions import ClientError

BUCKET = os.environ.get("APIGW_METRICS_BUCKET", "fenix-vector-ecs-logs")
PREFIX = os.environ.get("APIGW_METRICS_PREFIX", "apigw-metrics/")
REGION = os.environ.get("AWS_REGION", "us-west-2")
LOOKBACK_MINUTES = int(os.environ.get("APIGW_METRICS_LOOKBACK_MINUTES", "20"))
BLOCK_HOURS = 4                                   # 24 / 4 = 6 files per day

# column -> (CloudWatch metric, statistic)
METRICS = {
    "calls": ("Count", "Sum"),
    "err_4xx": ("4XXError", "Sum"),
    "err_5xx": ("5XXError", "Sum"),
    "latency_p50_ms": ("Latency", "p50"),
    "latency_p95_ms": ("Latency", "p95"),
    "integration_p95_ms": ("IntegrationLatency", "p95"),
}
COLUMNS = ["minute", "api", "stage", *METRICS]
SCHEMA = "{AWS/ApiGateway,ApiName,Stage}"         # per stage only (not per resource/method)
LABEL = "${PROP('Dim.ApiName')}\t${PROP('Dim.Stage')}"


def log(msg: str) -> None:
    print(f"{dt.datetime.now(dt.timezone.utc):%Y-%m-%dT%H:%M:%SZ} {msg}", flush=True)


def fetch(cw, start: dt.datetime, end: dt.datetime) -> dict[tuple[str, str, str], dict]:
    """All stages, all metrics, 1-minute points in [start, end). Key: (minute, api, stage)."""
    queries = [{
        "Id": f"m{i}",
        "Expression": f"SEARCH('{SCHEMA} MetricName=\"{metric}\"', '{stat}', 60)",
        "Label": LABEL,
        "ReturnData": True,
    } for i, (metric, stat) in enumerate(METRICS.values())]
    col_of = {f"m{i}": col for i, col in enumerate(METRICS)}

    rows: dict[tuple[str, str, str], dict] = {}
    token = None
    while True:
        kw = {"MetricDataQueries": queries, "StartTime": start, "EndTime": end,
              "ScanBy": "TimestampAscending"}
        if token:
            kw["NextToken"] = token
        resp = cw.get_metric_data(**kw)
        for m in resp.get("Messages", []):
            log(f"cloudwatch: {m.get('Code')}: {m.get('Value')}")
        for res in resp["MetricDataResults"]:
            col = col_of[res["Id"]]
            if res.get("StatusCode") not in (None, "Complete", "PartialData"):
                log(f"cloudwatch: {col} {res['Label']} status {res['StatusCode']}")
            api, _, stage = res["Label"].partition("\t")
            for ts, val in zip(res["Timestamps"], res["Values"]):
                minute = ts.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
                row = rows.setdefault((minute, api, stage), {"minute": minute, "api": api, "stage": stage})
                row[col] = int(round(val)) if stat_is_sum(col) else round(val, 1)
        token = resp.get("NextToken")
        if not token:
            return rows


def stat_is_sum(col: str) -> bool:
    return METRICS[col][1] == "Sum"


def block_key(minute: str) -> str:
    """'2026-11-09T13:07Z' -> '<prefix>dt=2026-11-09/part-12.csv.gz'"""
    day, hh = minute[:10], int(minute[11:13])
    return f"{PREFIX}dt={day}/part-{hh - hh % BLOCK_HOURS:02d}.csv.gz"


def read_block(s3, key: str) -> dict[tuple[str, str, str], dict]:
    try:
        body = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
            return {}
        raise
    text = gzip.decompress(body).decode("utf-8")
    return {(r["minute"], r["api"], r["stage"]): r for r in csv.DictReader(io.StringIO(text))}


def write_block(s3, key: str, rows: dict[tuple[str, str, str], dict]) -> int:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for k in sorted(rows):
        w.writerow(rows[k])
    data = gzip.compress(buf.getvalue().encode("utf-8"))
    s3.put_object(Bucket=BUCKET, Key=key, Body=data, ContentType="application/gzip")
    return len(data)


def merge_and_save(s3, new_rows: dict, dry_run: bool) -> None:
    by_block: dict[str, dict] = {}
    for k, row in new_rows.items():
        by_block.setdefault(block_key(k[0]), {})[k] = row
    for key in sorted(by_block):
        fresh = by_block[key]
        old = read_block(s3, key)
        merged = {**old, **fresh}           # newer numbers replace older ones for the same minute
        if dry_run:
            log(f"dry run: {key}: {len(old)} rows there, {len(fresh)} read now, {len(merged)} after merge")
            continue
        size = write_block(s3, key, merged)
        log(f"wrote s3://{BUCKET}/{key}: {len(merged)} rows ({size / 1024:.0f} KB), {len(fresh)} from this run")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--lookback-minutes", type=int, default=LOOKBACK_MINUTES)
    p.add_argument("--backfill-days", type=float, default=0,
                   help="one-off: fill this many days back (CloudWatch keeps 1-minute data 15 days)")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()

    session = boto3.session.Session(region_name=REGION)
    cw, s3 = session.client("cloudwatch"), session.client("s3")
    end = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)   # leave out the minute in progress
    t0 = time.monotonic()

    if a.backfill_days:
        start = end - dt.timedelta(days=min(a.backfill_days, 15))
        start = start.replace(hour=start.hour - start.hour % BLOCK_HOURS, minute=0)
        while start < end:                  # one 4-hour block at a time keeps each call small
            stop = min(start + dt.timedelta(hours=BLOCK_HOURS), end)
            rows = fetch(cw, start, stop)
            log(f"backfill {start:%Y-%m-%d %H:%M} to {stop:%H:%M}: {len(rows)} rows")
            merge_and_save(s3, rows, a.dry_run)
            start = stop
    else:
        rows = fetch(cw, end - dt.timedelta(minutes=a.lookback_minutes), end)
        merge_and_save(s3, rows, a.dry_run)

    log(f"done in {time.monotonic() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
