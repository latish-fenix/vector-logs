"""CPU and memory of ECS clusters and services, from CloudWatch (namespace AWS/ECS).

ECS publishes these every minute without any setup:

    ClusterName                  CPUUtilization, MemoryUtilization   what the tasks use, % of what the
                                 CPUReservation, MemoryReservation   cluster's instances offer / % reserved
    ClusterName + ServiceName    CPUUtilization, MemoryUtilization   % of what the service's tasks reserve

Read-only (cloudwatch:GetMetricData). Results are cached for a few minutes, and service metrics
are only read when someone opens a cluster, so a page left open costs little.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from .errors import ApiError, not_found

CLUSTER_METRICS = {"cpu": "CPUUtilization", "memory": "MemoryUtilization",
                   "cpuReserved": "CPUReservation", "memoryReserved": "MemoryReservation"}
SERVICE_METRICS = {"cpu": "CPUUtilization", "memory": "MemoryUtilization"}
MAX_QUERIES = 500   # GetMetricData limit per call


def period_for(hours: int) -> int:
    """About 60 points per chart, in whole minutes."""
    return max(60, (hours * 3600 // 60) // 60 * 60)


def _summary(points: list[list]) -> dict:
    vals = [v for _, v in points]
    return {"now": vals[-1] if vals else None, "avg": sum(vals) / len(vals) if vals else None,
            "max": max(vals) if vals else None, "points": points}


class MetricsService:
    def __init__(self, health, cache_seconds: int = 300):
        self.health = health
        self.cache_seconds = cache_seconds
        self._cache: dict[Any, tuple[float, dict]] = {}
        self._lock = threading.Lock()

    @property
    def cw(self):
        return self.health.aws.cloudwatch

    def _cached(self, key, build):
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < self.cache_seconds:
            return hit[1]
        with self._lock:
            hit = self._cache.get(key)
            if hit and time.time() - hit[0] < self.cache_seconds:
                return hit[1]
            data = build()
            if len(self._cache) > 500:
                self._cache.clear()
            self._cache[key] = (time.time(), data)
            return data

    def _fetch(self, queries: list[tuple[str, str, list[dict]]], hours: int) -> dict[str, list[list]]:
        """queries: (id, metric name, dimensions) → {id: [[epoch ms, value], …] oldest first}."""
        period = period_for(hours)
        end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        start = end - timedelta(hours=hours)
        out: dict[str, list[list]] = {q[0]: [] for q in queries}
        for i in range(0, len(queries), MAX_QUERIES):
            batch = [{"Id": qid, "ReturnData": True, "MetricStat": {
                "Metric": {"Namespace": "AWS/ECS", "MetricName": metric, "Dimensions": dims},
                "Period": period, "Stat": "Average"}} for qid, metric, dims in queries[i:i + MAX_QUERIES]]
            token = None
            while True:
                kw = {"MetricDataQueries": batch, "StartTime": start, "EndTime": end, "ScanBy": "TimestampAscending"}
                if token:
                    kw["NextToken"] = token
                try:
                    res = self.cw.get_metric_data(**kw)
                except (ClientError, BotoCoreError) as e:
                    raise _metrics_error(e) from e
                for r in res.get("MetricDataResults", []):
                    out[r["Id"]] += [[int(t.timestamp() * 1000), round(float(v), 2)]
                                     for t, v in zip(r.get("Timestamps", []), r.get("Values", []))]
                token = res.get("NextToken")
                if not token:
                    break
        for v in out.values():
            v.sort()
        return out

    # -- every cluster (the table)
    def clusters(self, hours: int) -> dict:
        def build():
            names = [c["name"] for c in self.health.snapshot()["clusters"]]
            queries, ids = [], {}
            for i, name in enumerate(names):
                for key, metric in CLUSTER_METRICS.items():
                    qid = f"c{i}_{key}"
                    ids[qid] = (name, key)
                    queries.append((qid, metric, [{"Name": "ClusterName", "Value": name}]))
            data = self._fetch(queries, hours)
            clusters: dict[str, dict] = {n: {} for n in names}
            for qid, (name, key) in ids.items():
                clusters[name][key] = _summary(data[qid])
            return {"hours": hours, "period": period_for(hours), "generatedAt": _iso(), "clusters": clusters}
        return self._cached(("clusters", hours), build)

    # -- one cluster's services (when it is opened)
    def services(self, cluster: str, hours: int) -> dict:
        def build():
            c = next((x for x in self.health.snapshot()["clusters"] if x["name"] == cluster), None)
            if c is None:
                raise not_found("CLUSTER_NOT_FOUND", f"There is no ECS cluster '{cluster}'")
            names = [s["name"] for s in c["services"] if s.get("name")]
            queries, ids = [], {}
            for i, svc in enumerate(names):
                for key, metric in SERVICE_METRICS.items():
                    qid = f"s{i}_{key}"
                    ids[qid] = (svc, key)
                    queries.append((qid, metric, [{"Name": "ClusterName", "Value": cluster},
                                                  {"Name": "ServiceName", "Value": svc}]))
            data = self._fetch(queries, hours) if queries else {}
            services: dict[str, dict] = {n: {} for n in names}
            for qid, (svc, key) in ids.items():
                services[svc][key] = _summary(data[qid])
            return {"cluster": cluster, "hours": hours, "period": period_for(hours), "generatedAt": _iso(),
                    "services": services}
        return self._cached(("services", cluster, hours), build)


def _iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _metrics_error(e: Exception) -> ApiError:
    if isinstance(e, ClientError):
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("AccessDenied", "AccessDeniedException", "UnauthorizedOperation"):
            return ApiError(500, "METRICS_ACCESS_DENIED",
                            "The server may not read CloudWatch metrics: add the 'EcsMetricsReadOnly' statement "
                            "(cloudwatch:GetMetricData) from docs/iam-policy.json to the EC2 instance role",
                            {"awsError": code})
        return ApiError(502, "AWS_ERROR", f"CloudWatch refused the request: {e}", {"awsError": code})
    return ApiError(502, "AWS_UNREACHABLE", f"Can't reach CloudWatch: {e}")
