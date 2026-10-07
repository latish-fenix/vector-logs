"""A small in-memory stand-in for the ECS, ELBv2 and EC2 APIs (only what ecs_health.py calls).

Shaped like our real us-west-2 setup: shared ALBs, target groups named tg-<cluster>, an
instance that is in a cluster but not behind its load balancer, an unhealthy target, a
service below its desired count, a cluster without a load balancer and target groups that
belong to no ECS cluster."""
from __future__ import annotations

from datetime import datetime, timezone

ACCT = "823002541310"
REGION = "us-west-2"


def lb_arn(name, kind="app"):
    return f"arn:aws:elasticloadbalancing:{REGION}:{ACCT}:loadbalancer/{kind}/{name}/1234567890abcdef"


def tg_arn(name):
    return f"arn:aws:elasticloadbalancing:{REGION}:{ACCT}:targetgroup/{name}/abcdef1234567890"


def cl_arn(name):
    return f"arn:aws:ecs:{REGION}:{ACCT}:cluster/{name}"


LBS = {
    "elb-alpha-prepurchase": "app", "elb-alpha-postpurchase": "app", "elb-prod-monitoring": "app",
    "elb-2026-jenkins-pvt": "app",
}
# target group -> (load balancer, port, targets [(instance, port, state, reason)])
TGS = {
    "tg-alpha-edds-1": ("elb-alpha-prepurchase", 80, [("i-0018ced1d3ebbe05e", 80, "healthy", None),
                                                      ("i-00cc76cb809c61f3d", 80, "healthy", None)]),
    "tg-alpha-edds-2": ("elb-alpha-prepurchase", 80, [("i-0078d66dad4bd5459", 80, "unhealthy", "Target.FailedHealthChecks"),
                                                      ("i-0143b8b97916aa1f8", 80, "healthy", None),
                                                      ("i-0ed49719ea46af8dd", 80, "healthy", None)]),
    "tg-post-btp-01": ("elb-alpha-postpurchase", 80, [("i-04fd93a828ddef68d", 80, "healthy", None),
                                                     ("i-0e7a9f18ca3802d2c", 80, "healthy", None)]),
    "tg-post-eibp-01": ("elb-alpha-postpurchase", 80, [("i-0106942bc5249ba40", 80, "healthy", None)]),
    "tg-webhook-01": ("elb-alpha-postpurchase", 8080, [("i-0106942bc5249ba40", 8080, "healthy", None)]),
    "tg-alpha-oms-routing": ("elb-alpha-prepurchase", 80, [("i-0f4d665df072db15c", 80, "unhealthy", "Target.Timeout")]),
    "tg-prod-monitoring": ("elb-prod-monitoring", 5601, [("i-086fc0493abed21ed", 5601, "unhealthy", "Target.ResponseCodeMismatch")]),
    "elb-2026-jenkins": ("elb-2026-jenkins-pvt", 8080, [("i-04c11731a5dd1988c", 8080, "unused", "Target.NotInUse")]),
}
INSTANCES = {
    "i-0018ced1d3ebbe05e": ("172.0.34.32", "ECS Instance - alpha-edds-1"),
    "i-00cc76cb809c61f3d": ("172.0.32.21", "ECS Instance - alpha-edds-1"),
    "i-0b3d94af937825202": ("172.0.31.4", "ECS Instance - alpha-edds-1"),
    "i-0078d66dad4bd5459": ("172.0.34.55", "ECS Instance - alpha-edds-2"),
    "i-0143b8b97916aa1f8": ("172.0.32.18", "ECS Instance - alpha-edds-2"),
    "i-0ed49719ea46af8dd": ("172.0.31.26", "ECS Instance - alpha-edds-2"),
    "i-04fd93a828ddef68d": ("172.0.42.130", "ECS Instance - post-btp-01"),
    "i-0e7a9f18ca3802d2c": ("172.0.43.6", "ECS Instance - post-btp-01"),
    "i-0106942bc5249ba40": ("172.0.44.187", "ECS Instance - post-eibp-01"),
    "i-0f4d665df072db15c": ("172.0.32.4", "ECS Instance - alpha-oms-routing"),
    "i-0aaaaaaaaaaaaaaa1": ("172.0.50.10", "ECS Instance - fenix-batch-1"),
    "i-086fc0493abed21ed": ("172.0.51.65", "new elk monitoring"),
    "i-04c11731a5dd1988c": ("172.22.12.207", "Jenkins-AL3-2026"),
}
# cluster -> (instances, services [(name, desired, running, target group or None, rollout, event)])
CLUSTERS = {
    "alpha-edds-1": (["i-0018ced1d3ebbe05e", "i-00cc76cb809c61f3d", "i-0b3d94af937825202"],
                     [("deliveryestimate", 3, 3, None, "COMPLETED", "has reached a steady state.")]),
    "alpha-edds-2": (["i-0078d66dad4bd5459", "i-0143b8b97916aa1f8", "i-0ed49719ea46af8dd"],
                     [("deliveryestimate", 3, 2, "tg-alpha-edds-2", "IN_PROGRESS",
                       "is unable to consistently start tasks successfully.")]),
    "post-btp-01": (["i-04fd93a828ddef68d", "i-0e7a9f18ca3802d2c"],
                    [("fenix-track-processor", 2, 2, None, "COMPLETED", "has reached a steady state."),
                     ("fenix-order-sync", 2, 2, None, "COMPLETED", "has reached a steady state.")]),
    "post-eibp-01": (["i-0106942bc5249ba40"], [("eibp", 1, 1, "tg-post-eibp-01", "COMPLETED", "has reached a steady state."),
                                              ("webhook", 1, 1, "tg-webhook-01", "COMPLETED", "has reached a steady state.")]),
    "alpha-oms-routing": (["i-0f4d665df072db15c"], [("oms-routing", 1, 0, None, "FAILED", "task failed ELB health checks")]),
    "fenix-batch-1": (["i-0aaaaaaaaaaaaaaa1"], [("batch-worker", 2, 2, None, "COMPLETED", "has reached a steady state.")]),
}


class _Pager:
    def __init__(self, pages):
        self.pages = pages

    def paginate(self, **kw):
        return self.pages(**kw)


class FakeECS:
    def get_paginator(self, op):
        if op == "list_clusters":
            return _Pager(lambda **kw: [{"clusterArns": [cl_arn(c) for c in CLUSTERS]}])
        if op == "list_services":
            return _Pager(lambda cluster, **kw: [{"serviceArns": [f"{cluster}/svc/{s[0]}" for s in CLUSTERS[cluster.split('/')[-1]][1]]}])
        if op == "list_container_instances":
            return _Pager(lambda cluster, **kw: [{"containerInstanceArns": [f"{cluster}/ci/{i}" for i in CLUSTERS[cluster.split('/')[-1]][0]]}])
        raise AssertionError(op)

    def describe_clusters(self, clusters):
        out = []
        for arn in clusters:
            name = arn.split("/")[-1]
            inst, svcs = CLUSTERS[name]
            out.append({"clusterArn": arn, "clusterName": name, "status": "ACTIVE",
                        "registeredContainerInstancesCount": len(inst), "runningTasksCount": sum(s[2] for s in svcs),
                        "pendingTasksCount": 0, "activeServicesCount": len(svcs)})
        return {"clusters": out}

    def describe_services(self, cluster, services):
        assert len(services) <= 10
        name = cluster.split("/")[-1]
        out = []
        for sarn in services:
            sname = sarn.split("/")[-1]
            _, desired, running, tg, rollout, event = next(s for s in CLUSTERS[name][1] if s[0] == sname)
            out.append({"serviceName": sname, "status": "ACTIVE", "launchType": "EC2", "desiredCount": desired,
                        "runningCount": running, "pendingCount": desired - running,
                        "loadBalancers": [{"targetGroupArn": tg_arn(tg), "containerName": sname, "containerPort": 8080}] if tg else [],
                        "deployments": [{"status": "PRIMARY", "rolloutState": rollout}],
                        "events": [{"createdAt": datetime(2026, 10, 7, 6, 0, tzinfo=timezone.utc), "message": event if event.startswith("(service") else f"(service {sname}) {event}"}]})
        return {"services": out}

    def describe_container_instances(self, cluster, containerInstances):
        return {"containerInstances": [{"ec2InstanceId": a.split("/")[-1], "status": "ACTIVE", "agentConnected": True,
                                        "runningTasksCount": 1} for a in containerInstances]}


class FakeELB:
    def get_paginator(self, op):
        if op == "describe_target_groups":
            return _Pager(lambda **kw: [{"TargetGroups": [
                {"TargetGroupArn": tg_arn(n), "TargetGroupName": n, "Port": port, "Protocol": "HTTP", "TargetType": "instance",
                 "HealthCheckPath": "/health", "LoadBalancerArns": [lb_arn(lb)]} for n, (lb, port, _) in TGS.items()]}])
        if op == "describe_load_balancers":
            return _Pager(lambda **kw: [{"LoadBalancers": [
                {"LoadBalancerArn": lb_arn(n), "LoadBalancerName": n, "Type": "application", "Scheme": "internal",
                 "DNSName": f"internal-{n}.{REGION}.elb.amazonaws.com", "State": {"Code": "active"}} for n in LBS]}])
        raise AssertionError(op)

    def describe_target_health(self, TargetGroupArn):
        name = TargetGroupArn.split("/")[1]
        return {"TargetHealthDescriptions": [
            {"Target": {"Id": i, "Port": p}, "TargetHealth": {"State": st, **({"Reason": r, "Description": r.split(".")[-1]} if r else {})}}
            for i, p, st, r in TGS[name][2]]}


class FakeEC2:
    def describe_instances(self, InstanceIds):
        return {"Reservations": [{"Instances": [
            {"InstanceId": i, "PrivateIpAddress": INSTANCES[i][0], "State": {"Name": "running"},
             "Tags": [{"Key": "Name", "Value": INSTANCES[i][1]}]} for i in InstanceIds if i in INSTANCES]}]}


class FakeAws:
    def __init__(self):
        self.ecs, self.elbv2, self.ec2 = FakeECS(), FakeELB(), FakeEC2()
