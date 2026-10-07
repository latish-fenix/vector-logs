"""ECS clusters → the load balancer target groups they sit behind → is each target healthy.

Everything is discovered from AWS on each refresh (nothing is configured by hand):

    ECS clusters ── services ──(service.loadBalancers)──────────────┐
         └── container instances (EC2 ids) ──(registered as targets)─┼─► target groups ─► load balancers
    target group named "tg-<cluster>" or "<cluster>" ────────────────┘        └─► target health

A cluster is linked to a target group by any of the three: an ECS service that registers
into it, one of the cluster's EC2 instances being a target in it, or the naming convention.
Read-only: only List*/Describe* calls are made.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from .errors import ApiError

log = logging.getLogger("vector_logs.health")

HEALTHY = "healthy"
# Target states that mean "not serving traffic" (unused = target group not attached to a
# listener/AZ, which is a setup issue rather than an outage)
BAD_STATES = {"unhealthy", "draining", "unavailable", "initial"}
STATUS_ORDER = {"down": 0, "degraded": 1, "healthy": 2, "none": 3}


def _chunks(items: list, n: int):
    for i in range(0, len(items), n):
        yield items[i:i + n]


def _name_from_arn(arn: str) -> str:
    # arn:aws:elasticloadbalancing:region:acct:loadbalancer/app/<name>/<id>  |  …:targetgroup/<name>/<id>
    parts = arn.split("/")
    if ":loadbalancer/" in arn and len(parts) >= 3:
        return parts[2]
    return parts[1] if len(parts) >= 2 else arn


def _aws_error(e: Exception, what: str) -> ApiError:
    if isinstance(e, ClientError):
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("AccessDenied", "AccessDeniedException", "UnauthorizedOperation", "UnrecognizedClientException"):
            return ApiError(500, "AWS_ACCESS_DENIED",
                            f"The server may not {what}: add the 'EcsAndLoadBalancerHealthReadOnly' statement "
                            "from docs/iam-policy.json to the EC2 instance role", {"awsError": code})
        return ApiError(502, "AWS_ERROR", f"AWS refused to {what}: {e}", {"awsError": code})
    return ApiError(502, "AWS_UNREACHABLE", f"Can't reach AWS to {what}: {e}")


class AwsClients:
    def __init__(self, region: str):
        cfg = Config(retries={"max_attempts": 8, "mode": "adaptive"}, max_pool_connections=20)
        self.ecs = boto3.client("ecs", region_name=region, config=cfg)
        self.elbv2 = boto3.client("elbv2", region_name=region, config=cfg)
        self.ec2 = boto3.client("ec2", region_name=region, config=cfg)


class HealthService:
    def __init__(self, region: str, cache_seconds: int = 60, clients: Any = None, threads: int = 8):
        self.region = region
        self.cache_seconds = cache_seconds
        self._clients = clients
        self.threads = threads
        self._lock = threading.Lock()
        self._cached: tuple[float, dict] | None = None

    @property
    def aws(self):
        if self._clients is None:
            self._clients = AwsClients(self.region)
        return self._clients

    # -- public
    def snapshot(self, refresh: bool = False) -> dict:
        """The latest picture, at most cache_seconds old. One refresh at a time; others wait for it."""
        hit = self._cached
        if not refresh and hit and time.time() - hit[0] < self.cache_seconds:
            return hit[1]
        with self._lock:
            hit = self._cached
            if not refresh and hit and time.time() - hit[0] < self.cache_seconds:
                return hit[1]
            data = self._collect()
            self._cached = (time.time(), data)
            return data

    # -- AWS reads
    def _paginate(self, client, op: str, key: str, what: str, **kw) -> list:
        out: list = []
        try:
            for page in client.get_paginator(op).paginate(**kw):
                out.extend(page.get(key, []))
        except (ClientError, BotoCoreError) as e:
            raise _aws_error(e, what) from e
        return out

    def _call(self, fn, what: str, **kw) -> dict:
        try:
            return fn(**kw)
        except (ClientError, BotoCoreError) as e:
            raise _aws_error(e, what) from e

    def _cluster_details(self, arn: str) -> dict:
        ecs = self.aws.ecs
        svc_arns = self._paginate(ecs, "list_services", "serviceArns", "list ECS services (ecs:ListServices)", cluster=arn)
        services = []
        for batch in _chunks(svc_arns, 10):
            services += self._call(ecs.describe_services, "describe ECS services (ecs:DescribeServices)",
                                   cluster=arn, services=batch).get("services", [])
        ci_arns = self._paginate(ecs, "list_container_instances", "containerInstanceArns",
                                 "list ECS container instances (ecs:ListContainerInstances)", cluster=arn)
        instances = []
        for batch in _chunks(ci_arns, 100):
            instances += self._call(ecs.describe_container_instances,
                                    "describe ECS container instances (ecs:DescribeContainerInstances)",
                                    cluster=arn, containerInstances=batch).get("containerInstances", [])
        return {"services": services, "instances": instances}

    def _target_health(self, tg_arn: str) -> dict:
        try:
            return {"targets": self.aws.elbv2.describe_target_health(TargetGroupArn=tg_arn)
                    .get("TargetHealthDescriptions", [])}
        except (ClientError, BotoCoreError) as e:
            err = _aws_error(e, "read target health (elasticloadbalancing:DescribeTargetHealth)")
            if err.code == "AWS_ACCESS_DENIED":
                raise err from e
            return {"targets": [], "error": str(e)[:300]}

    def _collect(self) -> dict:
        t0 = time.monotonic()
        aws = self.aws
        cluster_arns = self._paginate(aws.ecs, "list_clusters", "clusterArns", "list ECS clusters (ecs:ListClusters)")
        clusters = []
        for batch in _chunks(cluster_arns, 100):
            clusters += self._call(aws.ecs.describe_clusters, "describe ECS clusters (ecs:DescribeClusters)",
                                   clusters=batch).get("clusters", [])
        tgs = self._paginate(aws.elbv2, "describe_target_groups", "TargetGroups",
                             "list target groups (elasticloadbalancing:DescribeTargetGroups)")
        lbs = {lb["LoadBalancerArn"]: lb for lb in self._paginate(
            aws.elbv2, "describe_load_balancers", "LoadBalancers",
            "list load balancers (elasticloadbalancing:DescribeLoadBalancers)")}

        with ThreadPoolExecutor(max_workers=self.threads) as pool:
            details = dict(zip([c["clusterArn"] for c in clusters],
                               pool.map(lambda c: self._cluster_details(c["clusterArn"]), clusters)))
            health = dict(zip([t["TargetGroupArn"] for t in tgs],
                              pool.map(lambda t: self._target_health(t["TargetGroupArn"]), tgs)))

        # EC2 names and IPs for every instance we will show
        ids = {d["ec2InstanceId"] for v in details.values() for d in v["instances"] if d.get("ec2InstanceId")}
        ids |= {t["Target"]["Id"] for h in health.values() for t in h["targets"] if t["Target"]["Id"].startswith("i-")}
        ec2 = {}
        for batch in _chunks(sorted(ids), 200):
            for r in self._call(aws.ec2.describe_instances, "describe EC2 instances (ec2:DescribeInstances)",
                                InstanceIds=batch).get("Reservations", []):
                for i in r.get("Instances", []):
                    name = next((t["Value"] for t in i.get("Tags", []) if t.get("Key") == "Name"), None)
                    ec2[i["InstanceId"]] = {"ip": i.get("PrivateIpAddress"), "name": name,
                                            "state": (i.get("State") or {}).get("Name")}

        tg_by_arn = {t["TargetGroupArn"]: t for t in tgs}
        tg_view = {arn: self._tg_view(t, health.get(arn, {}), lbs, ec2) for arn, t in tg_by_arn.items()}
        linked: set[str] = set()
        out_clusters = []
        for c in clusters:
            d = details.get(c["clusterArn"], {"services": [], "instances": []})
            view = self._cluster_view(c, d, tg_by_arn, tg_view, ec2)
            linked |= {t["arn"] for t in view["targetGroups"]}
            out_clusters.append(view)
        out_clusters.sort(key=lambda c: (STATUS_ORDER.get(c["status"], 9), c["name"]))
        unlinked = sorted((v for a, v in tg_view.items() if a not in linked), key=lambda v: v["name"])

        summary = summarize(out_clusters)
        return {"region": self.region, "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "tookMs": int((time.monotonic() - t0) * 1000), "summary": summary,
                "clusters": out_clusters, "unlinkedTargetGroups": unlinked}

    # -- shaping
    @staticmethod
    def _tg_view(tg: dict, health: dict, lbs: dict, ec2: dict) -> dict:
        targets = []
        counts = {"healthy": 0, "bad": 0, "other": 0}
        for t in health.get("targets", []):
            tid = t["Target"]["Id"]
            th = t.get("TargetHealth", {})
            state = th.get("State", "unknown")
            info = ec2.get(tid, {})
            targets.append({"id": tid, "port": t["Target"].get("Port"), "state": state,
                            "reason": th.get("Reason"), "description": th.get("Description"),
                            "ip": info.get("ip") or (tid if not tid.startswith("i-") else None),
                            "name": info.get("name")})
            if state == HEALTHY:
                counts["healthy"] += 1
            elif state in BAD_STATES:
                counts["bad"] += 1
            else:
                counts["other"] += 1
        targets.sort(key=lambda x: (x["state"] == HEALTHY, x["ip"] or x["id"]))
        lb_list = []
        for arn in tg.get("LoadBalancerArns", []):
            lb = lbs.get(arn, {})
            lb_list.append({"name": lb.get("LoadBalancerName") or _name_from_arn(arn), "type": lb.get("Type"),
                            "scheme": lb.get("Scheme"), "dns": lb.get("DNSName"),
                            "state": (lb.get("State") or {}).get("Code")})
        return {"arn": tg["TargetGroupArn"], "name": tg["TargetGroupName"], "port": tg.get("Port"),
                "protocol": tg.get("Protocol"), "targetType": tg.get("TargetType"),
                "healthCheckPath": tg.get("HealthCheckPath"), "loadBalancers": lb_list,
                "targets": targets, "total": len(targets), "counts": counts, "error": health.get("error")}

    @staticmethod
    def _cluster_view(c: dict, d: dict, tg_by_arn: dict, tg_view: dict, ec2: dict) -> dict:
        name = c["clusterName"]
        links: dict[str, set[str]] = {}
        services = []
        for s in d["services"]:
            deployments = s.get("deployments", [])
            primary = next((x for x in deployments if x.get("status") == "PRIMARY"), deployments[0] if deployments else {})
            events = s.get("events", [])
            tg_names = []
            for lb in s.get("loadBalancers", []):
                arn = lb.get("targetGroupArn")
                if arn and arn in tg_by_arn:
                    links.setdefault(arn, set()).add("service")
                    tg_names.append(tg_by_arn[arn]["TargetGroupName"])
            services.append({
                "name": s.get("serviceName"), "status": s.get("status"), "launchType": s.get("launchType"),
                "desired": int(s.get("desiredCount", 0)), "running": int(s.get("runningCount", 0)),
                "pending": int(s.get("pendingCount", 0)), "rollout": primary.get("rolloutState"),
                "deployments": len(deployments), "targetGroups": tg_names,
                "lastEvent": ({"at": events[0]["createdAt"].isoformat() if hasattr(events[0].get("createdAt"), "isoformat")
                               else events[0].get("createdAt"), "message": events[0].get("message")} if events else None),
            })
        services.sort(key=lambda s: (s["running"] >= s["desired"], s["name"] or ""))
        inst_ids = {i.get("ec2InstanceId") for i in d["instances"] if i.get("ec2InstanceId")}
        for arn, v in tg_view.items():
            if any(t["id"] in inst_ids for t in v["targets"]):
                links.setdefault(arn, set()).add("instances")
            if v["name"] in (f"tg-{name}", name):
                links.setdefault(arn, set()).add("name")
        tgs = []
        in_lb: set[str] = set()
        for arn, how in links.items():
            v = dict(tg_view[arn])
            v["linkedBy"] = sorted(how)
            v["targets"] = [dict(t, inCluster=t["id"] in inst_ids) for t in v["targets"]]
            in_lb |= {t["id"] for t in v["targets"]}
            tgs.append(v)
        tgs.sort(key=lambda v: v["name"])
        instances = []
        for i in d["instances"]:
            iid = i.get("ec2InstanceId")
            info = ec2.get(iid, {})
            instances.append({"id": iid, "ip": info.get("ip"), "name": info.get("name"),
                              "status": i.get("status"), "agentConnected": i.get("agentConnected"),
                              "runningTasks": i.get("runningTasksCount"), "behindLoadBalancer": iid in in_lb})
        instances.sort(key=lambda x: (x["behindLoadBalancer"], x["ip"] or ""))

        down = any(t["total"] > 0 and t["counts"]["healthy"] == 0 for t in tgs) or \
            any(s["desired"] > 0 and s["running"] == 0 for s in services)
        degraded = any(t["counts"]["bad"] > 0 or t["error"] for t in tgs) or \
            any(s["running"] < s["desired"] or s["rollout"] == "FAILED" for s in services) or \
            any(i["agentConnected"] is False for i in instances)
        # Clusters without a load balancer can still be down/degraded through their services.
        status = "down" if down else "degraded" if degraded else ("healthy" if tgs else "none")
        return {
            "name": name, "arn": c["clusterArn"], "clusterStatus": c.get("status"), "status": status,
            "instancesRegistered": c.get("registeredContainerInstancesCount", 0),
            "runningTasks": c.get("runningTasksCount", 0), "pendingTasks": c.get("pendingTasksCount", 0),
            "activeServices": c.get("activeServicesCount", 0),
            "targetGroups": tgs, "services": services, "instances": instances,
            "targets": {"healthy": sum(t["counts"]["healthy"] for t in tgs), "total": sum(t["total"] for t in tgs),
                        "bad": sum(t["counts"]["bad"] for t in tgs)},
            "servicesRunning": sum(s["running"] for s in services), "servicesDesired": sum(s["desired"] for s in services),
            "loadBalancers": sorted({lb["name"] for t in tgs for lb in t["loadBalancers"]}),
        }


def summarize(clusters: list[dict]) -> dict:
    return {
        "clusters": len(clusters),
        "withLoadBalancer": sum(1 for c in clusters if c["targetGroups"]),
        "withoutLoadBalancer": sum(1 for c in clusters if not c["targetGroups"]),
        "down": sum(1 for c in clusters if c["status"] == "down"),
        "degraded": sum(1 for c in clusters if c["status"] == "degraded"),
        "healthy": sum(1 for c in clusters if c["status"] == "healthy"),
        "unhealthyTargets": sum(t["counts"].get("bad", 0) for c in clusters for t in c["targetGroups"]),
        "servicesBelowDesired": sum(1 for c in clusters for sv in c["services"] if sv["running"] < sv["desired"]),
    }


def filter_for_user(data: dict, can_view, admin: bool) -> dict:
    """Admins see every cluster and the target groups no cluster uses; others only their clusters."""
    if admin:
        return data
    clusters = [c for c in data["clusters"] if can_view(c["name"])]
    return dict(data, clusters=clusters, unlinkedTargetGroups=[], summary=summarize(clusters))
