"""ECS cluster → target group health (fake AWS clients shaped like our us-west-2 setup)."""
from __future__ import annotations

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

from app.ecs_health import HealthService
from app.main import create_app
from conftest import ROOT_H, add_user, as_user, make_settings
from fake_aws import FakeAws


@pytest.fixture()
def hclient(tmp_path, logs_dir):
    app = create_app(make_settings(tmp_path, logs_dir), health=HealthService("us-west-2", clients=FakeAws()))
    return TestClient(app)


def by_name(data):
    return {c["name"]: c for c in data["clusters"]}


def test_links_clusters_to_target_groups_three_ways(hclient):
    data = hclient.get("/api/v1/health/ecs", headers=ROOT_H).json()
    c = by_name(data)
    # by name convention + instances (no ECS service registers into it)
    tg = c["alpha-edds-1"]["targetGroups"]
    assert [t["name"] for t in tg] == ["tg-alpha-edds-1"] and tg[0]["linkedBy"] == ["instances", "name"]
    assert tg[0]["loadBalancers"][0]["name"] == "elb-alpha-prepurchase"
    # an instance of the cluster that is not behind the load balancer is reported
    inst = {i["ip"]: i for i in c["alpha-edds-1"]["instances"]}
    assert inst["172.0.31.4"]["behindLoadBalancer"] is False and inst["172.0.34.32"]["behindLoadBalancer"] is True
    # by service: post-eibp-01 has two target groups, one on port 8080
    names = {t["name"]: t for t in c["post-eibp-01"]["targetGroups"]}
    assert set(names) == {"tg-post-eibp-01", "tg-webhook-01"} and "service" in names["tg-webhook-01"]["linkedBy"]
    assert c["post-btp-01"]["loadBalancers"] == ["elb-alpha-postpurchase"]


def test_status_rules(hclient):
    data = hclient.get("/api/v1/health/ecs", headers=ROOT_H).json()
    c = by_name(data)
    assert c["alpha-edds-1"]["status"] == "healthy"
    edds2 = c["alpha-edds-2"]
    assert edds2["status"] == "degraded" and edds2["targets"] == {"healthy": 2, "total": 3, "bad": 1}
    bad = edds2["targetGroups"][0]["targets"][0]
    assert bad["state"] == "unhealthy" and bad["ip"] == "172.0.34.55" and bad["reason"] == "Target.FailedHealthChecks"
    assert edds2["services"][0]["running"] == 2 and edds2["services"][0]["desired"] == 3
    assert c["alpha-oms-routing"]["status"] == "down"            # 0 healthy targets, 0/1 tasks
    assert c["fenix-batch-1"]["status"] == "none" and c["fenix-batch-1"]["targetGroups"] == []
    # problems first
    assert [x["name"] for x in data["clusters"]][:2] == ["alpha-oms-routing", "alpha-edds-2"]
    s = data["summary"]
    assert s == {"clusters": 6, "withLoadBalancer": 5, "withoutLoadBalancer": 1, "down": 1, "degraded": 1,
                 "healthy": 3, "unhealthyTargets": 2, "servicesBelowDesired": 2}
    # target groups no ECS cluster uses (monitoring, Jenkins)
    assert [t["name"] for t in data["unlinkedTargetGroups"]] == ["elb-2026-jenkins", "tg-prod-monitoring"]


def test_members_see_only_their_clusters(hclient):
    add_user(hclient, "ana@fenixcommerce.com", {"alpha-edds-1": "view", "post-btp-01": "view"})
    data = hclient.get("/api/v1/health/ecs", headers=as_user("ana@fenixcommerce.com")).json()
    assert sorted(by_name(data)) == ["alpha-edds-1", "post-btp-01"]
    assert data["unlinkedTargetGroups"] == [] and data["summary"]["clusters"] == 2


def test_cached_for_a_minute(tmp_path):
    calls = []
    fake = FakeAws()
    real = fake.ecs.describe_clusters
    fake.ecs.describe_clusters = lambda **kw: calls.append(1) or real(**kw)
    svc = HealthService("us-west-2", cache_seconds=60, clients=fake)
    svc.snapshot()
    svc.snapshot()
    assert len(calls) == 1
    svc.snapshot(refresh=True)
    assert len(calls) == 2


def test_access_denied_is_explained(tmp_path, logs_dir):
    fake = FakeAws()

    def deny(**kw):
        raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "ListClusters")

    class P:
        def paginate(self, **kw):
            deny()
    fake.ecs.get_paginator = lambda op: P()
    c = TestClient(create_app(make_settings(tmp_path, logs_dir), health=HealthService("us-west-2", clients=fake)))
    r = c.get("/api/v1/health/ecs", headers=ROOT_H)
    assert r.status_code == 500 and r.json()["error"]["code"] == "AWS_ACCESS_DENIED"
    assert "iam-policy.json" in r.json()["error"]["message"]


def test_cpu_and_memory_per_cluster_and_service(tmp_path, logs_dir):
    aws = FakeAws()
    client = TestClient(create_app(make_settings(tmp_path, logs_dir), health=HealthService("us-west-2", clients=aws)))
    m = client.get("/api/v1/health/ecs/metrics?hours=3", headers=ROOT_H).json()
    assert m["period"] == 180 and set(m["clusters"]) == set(by_name(client.get("/api/v1/health/ecs", headers=ROOT_H).json()))
    edds2 = m["clusters"]["alpha-edds-2"]
    assert edds2["cpu"]["now"] == 92.0 and edds2["memory"]["now"] == 100.0 and edds2["cpuReserved"]["now"] == 100.0
    assert len(edds2["cpu"]["points"]) == 60
    assert m["clusters"]["fenix-batch-1"]["cpu"]["now"] is None          # nothing published
    calls = aws.cloudwatch.calls
    client.get("/api/v1/health/ecs/metrics?hours=3", headers=ROOT_H)
    assert aws.cloudwatch.calls == calls                                   # cached
    s = client.get("/api/v1/health/ecs/clusters/post-eibp-01/metrics", headers=ROOT_H).json()
    assert set(s["services"]) == {"eibp", "webhook"} and s["services"]["eibp"]["cpu"]["now"] == 51.0
    # members: only their clusters
    add_user(client, "dev@x.com", {"alpha-edds-1": "view"})
    m = client.get("/api/v1/health/ecs/metrics", headers=as_user("dev@x.com")).json()
    assert list(m["clusters"]) == ["alpha-edds-1"]
    r = client.get("/api/v1/health/ecs/clusters/post-eibp-01/metrics", headers=as_user("dev@x.com"))
    assert r.status_code == 403


def test_metrics_access_denied(tmp_path, logs_dir):
    aws = FakeAws()

    def denied(**kw):
        raise ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "GetMetricData")
    aws.cloudwatch.get_metric_data = denied
    client = TestClient(create_app(make_settings(tmp_path, logs_dir), health=HealthService("us-west-2", clients=aws)))
    r = client.get("/api/v1/health/ecs/metrics", headers=ROOT_H)
    assert r.status_code == 500 and r.json()["error"]["code"] == "METRICS_ACCESS_DENIED"
    assert client.get("/api/v1/health/ecs", headers=ROOT_H).status_code == 200   # health still works
