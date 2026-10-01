"""Write synthetic Vector-style Parquet logs for local testing (no real data).

    python local-test/make_sample_logs.py [out_dir] [--hours 30] [--files-per-hour 6]

Creates  <out_dir>/vector/<cluster>/dt=YYYY-MM-DD/hour=HH/<epoch>-<uuid>.parquet  with the
same columns as /etc/vector/fenix_app_log.schema, for three clusters, covering the last
N hours plus one hour 25 days ago. Default out_dir: local-test/logs
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb

COLUMNS = [
    ("log_time", "VARCHAR"), ("log_time_ms", "BIGINT"), ("ingest_time_ms", "BIGINT"), ("cluster", "VARCHAR"),
    ("instance_id", "VARCHAR"), ("host", "VARCHAR"), ("service", "VARCHAR"), ("container_id", "VARCHAR"),
    ("source_file", "VARCHAR"), ("app", "VARCHAR"), ("logger", "VARCHAR"), ("level", "VARCHAR"),
    ("class", "VARCHAR"), ("method", "VARCHAR"), ("line", "BIGINT"), ("msg", "VARCHAR"),
    ("exception", "VARCHAR"), ("body", "VARCHAR"), ("error_identifier", "VARCHAR"),
    ("error_module", "VARCHAR"), ("error_code", "VARCHAR"), ("error_message", "VARCHAR"),
    ("status_code", "BIGINT"), ("parent_log", "VARCHAR"), ("child_log", "VARCHAR"), ("tenant_id", "VARCHAR"),
    ("request_uuid", "VARCHAR"), ("web_id", "VARCHAR"), ("buyer_zip", "VARCHAR"), ("shipper_zip", "VARCHAR"),
    ("carrier", "VARCHAR"), ("page_type", "VARCHAR"), ("response_time_ms", "BIGINT"), ("kv_json", "VARCHAR"),
    ("format", "VARCHAR"), ("parse_ok", "BOOLEAN"), ("raw", "VARCHAR"),
]
NAMES = [c for c, _ in COLUMNS]

CLUSTERS = {
    "post-btp-01": [("ip-172-0-43-6.us-west-2.compute.internal", "i-0e7a9f18ca3802d2c"),
                    ("ip-172-0-44-17.us-west-2.compute.internal", "i-0b1c2d3e4f5a6b7c8")],
    "post-btp-02": [("ip-172-0-51-9.us-west-2.compute.internal", "i-0aa11bb22cc33dd44")],
    "pre-prod-01": [("ip-172-0-60-3.us-west-2.compute.internal", "i-0ff00ee11dd22cc33")],
}
SERVICES = {"fenix-track-processor": "9b46e4c2e113", "fenix-delest-api": "4f2a9c1d7e08",
            "fenix-order-sync": "c0ffee123456"}
CARRIERS = ["UPS", "USPS", "FEDEX", "SPEEDX", "DHL"]

TRACK_EVENTS = [
    ("INFO", "com.fenixcommerce.connector.speedx.SpeedxTrackConnector", "getSpeedxTrackResponse", 130,
     "Speedx API Response - Success: true, Code: 200, Message: SUCCESS", None, None),
    ("DEBUG", "com.fenixcommerce.connector.speedx.SpeedxTrackConnector", "syncTrackEvents", 64, "[STARTED]", None, None),
    ("DEBUG", "com.fenixcommerce.connector.speedx.SpeedxTrackConnector", "syncTrackEvents", 102, "[COMPLETED]", None, None),
    ("INFO", "com.fenixcommerce.connector.ups.UPSTrackV1Connector", "getAccessTokenFromCarrier", 220, "AuthUrl :: null", None, None),
    ("ERROR", "com.fenixcommerce.connector.ups.UPSTrackV1Connector", "getUPSTrackResponse", 179,
     '401 Unauthorized: [{"response":{"errors":[{"code":"250002","message":"Invalid Authentication Information."}]}}]', None, None),
    ("ERROR", "org.springframework.web.client.HttpClientErrorException", "create", 113, None,
     "org.springframework.web.client.HttpClientErrorException$NotFound",
     "org.springframework.web.client.HttpClientErrorException$NotFound: 404 Not Found: [<!DOCTYPE html>\n<html lang=\"en\">\n<head><title>Not found</title></head>\n</html>]"),
    ("ERROR", "org.elasticsearch.index.query.BaseTermQueryBuilder", "<init>", 116, None, "java.lang.IllegalArgumentException",
     "java.lang.IllegalArgumentException: value cannot be null\n\tat org.elasticsearch.index.query.BaseTermQueryBuilder.<init>(BaseTermQueryBuilder.java:116)\n\tat org.elasticsearch.index.query.TermQueryBuilder.<init>(TermQueryBuilder.java:84)"),
    ("WARN", "com.fenixcommerce.tracking.connector.usps.USPSTrackConnector", "getUspsTrackResponse", 142,
     "USPS API slow response, retrying", None, None),
]


def _row(cluster, host, instance, service, ts: datetime, rnd: random.Random) -> dict:
    cid = SERVICES[service]
    r = {n: None for n in NAMES}
    ms = int(ts.timestamp() * 1000)
    r.update(log_time=ts.strftime("%Y-%m-%d %H:%M:%S.") + f"{ts.microsecond // 1000:03d}", log_time_ms=ms,
             ingest_time_ms=ms + rnd.randint(300, 9000), cluster=cluster, instance_id=instance, host=host,
             service=service, container_id=cid, source_file=f"/var/log/fenix-apps/{service}_{cid}.log",
             app=service, parse_ok=True)
    if service == "fenix-delest-api":
        carrier = rnd.choice(CARRIERS)
        kv = {"tenantId": str(rnd.choice([1001, 1002, 2040])), "internalRequestUUID": str(uuid.UUID(int=rnd.getrandbits(128))),
              "webId": f"web-{rnd.randint(1, 40)}", "buyerZipCode": f"{rnd.randint(10000, 99999)}",
              "shipperZipCode": f"{rnd.randint(10000, 99999)}", "carrierName": carrier, "pageType": rnd.choice(["PDP", "CART", "CHECKOUT"]),
              "responseTime": str(rnd.randint(20, 2400))}
        r.update(format="delest", logger="com.fenixcommerce.common.log.util.LogIndexer",
                 **{"class": "com.fenixcommerce.delest.service.EstimateServiceImpl"}, method="getEstimate",
                 line=rnd.choice([88, 112]), parent_log="LOG_RESPONSE_TIME", child_log="DELEST_ESTIMATE",
                 tenant_id=kv["tenantId"], request_uuid=kv["internalRequestUUID"], web_id=kv["webId"],
                 buyer_zip=kv["buyerZipCode"], shipper_zip=kv["shipperZipCode"], carrier=carrier,
                 page_type=kv["pageType"], response_time_ms=int(kv["responseTime"]), kv_json=json.dumps(kv),
                 level="INFO" if int(kv["responseTime"]) < 2000 else "WARN")
        return r
    if service == "fenix-order-sync" and rnd.random() < 0.15:
        r.update(format="track", logger="com.fenixcommerce.common.log.util.LogUtils", level="ERROR",
                 **{"class": "com.fenixcommerce.track.page.service.impl.TrackingPageBusinessServiceImpl"},
                 method="getTrackingResponse", line=445, exception="com.fenixcommerce.base.exception.FenixException",
                 body='{"identifier":"ENTITY_NOT_FOUND","module_code":"PPE_INSIGHTS","error_code":"TR006","status_code":400,"error_message":"Tracking Number not found"}',
                 error_identifier="ENTITY_NOT_FOUND", error_module="PPE_INSIGHTS", error_code="TR006",
                 error_message="Tracking Number not found", status_code=400)
        return r
    if rnd.random() < 0.02:
        r.update(format="unparsed", parse_ok=False, raw=f"garbled line {rnd.randint(1, 9999)} without a header")
        return r
    level, klass, method, line, msg, exc, body = rnd.choice(TRACK_EVENTS)
    if msg and "Tracking Number" not in msg and rnd.random() < 0.3:
        msg = f"{msg} | Tracking Number: SPX{rnd.randint(10**11, 10**12 - 1)}"
    r.update(format="track", logger="com.fenixcommerce.common.log.util.LogIndexer" if level != "ERROR" else
             "com.fenixcommerce.common.log.util.LogUtils", level=level, method=method, line=line, msg=msg,
             exception=exc, body=body, **{"class": klass})
    return r


_CON = None


def write_file(path: Path, rows: list[dict]) -> None:
    """One Parquet file (zstd, like Vector's sink) with exactly the schema's column types."""
    global _CON
    if _CON is None:
        _CON = duckdb.connect()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".ndjson")
    tmp.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    cols = "{" + ", ".join(f"'{n}': '{t}'" for n, t in COLUMNS) + "}"
    try:
        _CON.execute(f"COPY (SELECT {', '.join(chr(34) + n + chr(34) for n in NAMES)} FROM "
                     f"read_json('{tmp.as_posix()}', columns={cols}, format='newline_delimited')) "
                     f"TO '{path.as_posix()}' (FORMAT parquet, COMPRESSION zstd)")
    finally:
        tmp.unlink(missing_ok=True)


def generate(out: Path, hours: int = 30, files_per_hour: int = 6, rows_per_file: int = 40,
             now: datetime | None = None, seed: int = 7, clusters: dict | None = None) -> int:
    rnd = random.Random(seed)
    now = (now or datetime.now(timezone.utc)).replace(second=0, microsecond=0)
    starts = [now.replace(minute=0) - timedelta(hours=h) for h in range(hours)]
    starts.append(now.replace(minute=0) - timedelta(days=25))
    count = 0
    for cluster, hosts in (clusters or CLUSTERS).items():
        for hour in starts:
            for host, instance in hosts:
                for i in range(files_per_hour):
                    lo = hour + timedelta(minutes=i * 60 // files_per_hour)
                    hi = min(lo + timedelta(minutes=60 // files_per_hour), now)
                    if lo >= now:
                        break
                    span = max(1, int((hi - lo).total_seconds() * 1000))
                    times = sorted(lo + timedelta(milliseconds=rnd.randint(0, span - 1)) for _ in range(rows_per_file))
                    rows = [_row(cluster, host, instance, rnd.choice(list(SERVICES)), t, rnd) for t in times]
                    name = f"{int(hi.timestamp())}-{uuid.UUID(int=rnd.getrandbits(128))}.parquet"
                    write_file(out / "vector" / cluster / f"dt={hour:%Y-%m-%d}" / f"hour={hour:%H}" / name, rows)
                    count += 1
    return count


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("out", nargs="?", default=str(Path(__file__).parent / "logs"))
    ap.add_argument("--hours", type=int, default=30)
    ap.add_argument("--files-per-hour", type=int, default=6)
    ap.add_argument("--rows-per-file", type=int, default=40)
    a = ap.parse_args()
    n = generate(Path(a.out), a.hours, a.files_per_hour, a.rows_per_file)
    print(f"Wrote {n} Parquet files under {a.out}/vector/", file=sys.stderr)
