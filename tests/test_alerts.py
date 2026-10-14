"""Alerts: rules, checks against fake AWS / sample logs / converted ALB logs, Slack messages, the API."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from make_alb_logs import generate

from app.alerts import MINUTE, AlertService, ServerErrors, now_ms, validate_rule
from app.ecs_health import HealthService
from app.errors import ApiError
from app.lb_logs import Converter
from app.main import create_app
from conftest import NOW, ROOT_H, add_user, as_user, make_settings
from fake_aws import FakeAws

HOOK = "https://hooks.slack.com/services/T000/B000/abcdefghijklmnop"
URL = "/api/v1/admin/alerts"


class Slack:
    def __init__(self):
        self.sent: list[tuple[str, dict]] = []
        self.fail: Exception | None = None

    def __call__(self, url, payload):
        if self.fail:
            raise self.fail
        self.sent.append((url, payload))

    def texts(self):
        return [p["blocks"][0]["text"]["text"] for _, p in self.sent]


@pytest.fixture()
def env(tmp_path, logs_dir):
    app = create_app(make_settings(tmp_path, logs_dir), health=HealthService("us-west-2", clients=FakeAws()))
    slack = Slack()
    app.state.alerts.sender = slack
    return TestClient(app), app.state.alerts, slack


def rule(svc: AlertService, rtype: str) -> dict:
    return next(r for r in svc.config()["rules"] if r["type"] == rtype)


def only(svc: AlertService, *types: str, **params) -> None:
    """Keep one enabled rule per given type (with params), disable the rest."""
    def mutate(doc):
        for r in doc["rules"]:
            r["enabled"] = r["type"] in types
            if r["type"] in types:
                r["params"].update(params)
                r["forMinutes"] = 0
    svc._update(mutate)


def test_default_rules_and_catalog(env):
    client, svc, _ = env
    d = client.get(URL, headers=ROOT_H).json()
    types = [r["type"] for r in d["rules"]]
    assert {"app_aws", "app_converter", "logs_stale", "ecs_targets", "lb_errors"} <= set(types)
    assert len(d["catalog"]) == 13 and d["slack"]["configured"] is False
    assert d["settings"]["timeZone"] == "Asia/Kolkata"
    # members can't see or change rules
    add_user(client, "m@x.com", {"alpha-edds-2": "view"})
    assert client.get(URL, headers=as_user("m@x.com")).status_code == 403


def test_validation():
    r = validate_rule({"type": "lb_errors", "params": {"thresholdPercent": "2.5", "targetGroups": ["tg-a", "tg-a"]}})
    assert r["params"]["thresholdPercent"] == 2.5 and r["params"]["targetGroups"] == ["tg-a"]
    assert r["params"]["statusClass"] == "5xx" and r["forMinutes"] == 0 and r["name"] == "HTTP error rate"
    for bad in ({"type": "nope"}, {"type": "lb_errors", "params": {"thresholdPercent": 500}},
                {"type": "lb_errors", "params": {"statusClass": "2xx"}},
                {"type": "log_errors", "params": {"clusters": []}},
                {"type": "ecs_targets", "params": {"clusters": ["bad name!"]}}):
        with pytest.raises(ApiError):
            validate_rule(bad)


def test_webhook_saved_in_secrets_and_masked(env, tmp_path):
    client, svc, slack = env
    r = client.put(f"{URL}/slack", json={"webhookUrl": "https://example.com/hook"}, headers=ROOT_H)
    assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_WEBHOOK"
    r = client.put(f"{URL}/slack", json={"webhookUrl": HOOK}, headers=ROOT_H)
    assert r.status_code == 200 and r.json()["hint"].endswith("mnop") and "abcdefghijkl" not in r.json()["hint"]
    assert svc.secrets.get("alerts") == {"slackWebhookUrl": HOOK}
    assert HOOK not in json.dumps(svc.store.get_json("state/alerts.json")[0])      # never in S3
    assert HOOK not in client.get(URL, headers=ROOT_H).text                         # never returned
    assert client.post(f"{URL}/slack/_test", json={}, headers=ROOT_H).json() == {"sent": True}
    assert slack.sent[0][0] == HOOK and "Test message" in slack.texts()[0]
    # change it: the next message goes to the new URL
    new = HOOK.replace("abcdefghijklmnop", "zyxwvutsrqponmlk")
    client.put(f"{URL}/slack", json={"webhookUrl": new}, headers=ROOT_H)
    client.post(f"{URL}/slack/_test", json={}, headers=ROOT_H)
    assert slack.sent[-1][0] == new
    assert client.delete(f"{URL}/slack", headers=ROOT_H).json() == {"configured": False}
    assert client.post(f"{URL}/slack/_test", json={}, headers=ROOT_H).json()["error"]["code"] == "NO_WEBHOOK"


def test_ecs_rules_fire_once_then_resolve(env):
    client, svc, slack = env
    client.put(f"{URL}/slack", json={"webhookUrl": HOOK}, headers=ROOT_H)
    client.put(f"{URL}/settings", json={"consoleUrl": "https://logs.example.com/ui/"}, headers=ROOT_H)
    only(svc, "ecs_targets", "ecs_tasks")
    t = now_ms()
    out = svc.run_once(t)
    fired = {(e["type"], e["subject"]) for e in out["events"]}
    assert ("ecs_targets", "alpha-edds-2") in fired and ("ecs_targets", "alpha-oms-routing") in fired
    assert ("ecs_tasks", "alpha-edds-2 / deliveryestimate") in fired and ("ecs_targets", "alpha-edds-1") not in fired
    assert len(slack.sent) == 2                          # one message per rule
    text = "\n".join(slack.texts())
    assert "tg-alpha-edds-2: 1 of 3 not healthy (Target.FailedHealthChecks)" in text
    assert "<https://logs.example.com/ui/health?q=alpha-edds-2|alpha-edds-2>" in text
    assert "running 2 of 3 tasks" in text and "unable to consistently start tasks" in text
    # still bad a minute later: nothing new
    svc.run_once(t + MINUTE)
    assert len(slack.sent) == 2
    # fixed: resolved message
    import fake_aws
    old = fake_aws.TGS["tg-alpha-edds-2"]
    fake_aws.TGS["tg-alpha-edds-2"] = (old[0], old[1], [(i, p, "healthy", None) for i, p, _, _ in old[2]])
    try:
        svc.health.snapshot(refresh=True)
        svc.run_once(t + 2 * MINUTE)
    finally:
        fake_aws.TGS["tg-alpha-edds-2"] = old
    resolved = [p for _, p in slack.sent if ":large_green_circle:" in p["blocks"][0]["text"]["text"]]
    assert resolved and "alpha-edds-2" in resolved[0]["blocks"][0]["text"]["text"]
    hist = client.get("/api/v1/alerts/history", headers=ROOT_H).json()["items"]
    assert hist[0]["event"] == "resolved" and all(h["delivered"] for h in hist)


def test_for_minutes_and_repeat(env):
    _, svc, slack = env
    svc.set_webhook(HOOK, "root")
    only(svc, "ecs_targets")
    svc.update_rule(rule(svc, "ecs_targets")["id"], {"forMinutes": 5, "repeatMinutes": 60}, "root")
    t = now_ms()
    svc.run_once(t)
    assert slack.sent == []                                       # pending
    st = svc.state()["rules"][rule(svc, "ecs_targets")["id"]]["subjects"]
    assert st["alpha-edds-2"]["state"] == "pending"
    svc.run_once(t + 5 * MINUTE)
    assert len(slack.sent) == 1                                   # firing after 5 minutes
    svc.run_once(t + 30 * MINUTE)
    assert len(slack.sent) == 1
    svc.run_once(t + 66 * MINUTE)
    assert len(slack.sent) == 2 and "still failing" in slack.texts()[1]


def test_paused_or_no_webhook_keeps_history(env):
    client, svc, slack = env
    only(svc, "ecs_targets")
    out = svc.run_once()
    assert out["events"] and all(e["error"] == "No Slack webhook set" for e in out["events"]) and not slack.sent
    svc.set_webhook(HOOK, "root")
    svc.update_settings({"paused": True}, "root")
    only(svc, "ecs_tasks")
    out = svc.run_once()
    assert out["events"][0]["error"] == "Notifications are paused" and not slack.sent


def test_slack_failure_is_recorded(env):
    _, svc, slack = env
    svc.set_webhook(HOOK, "root")
    slack.fail = ApiError(502, "SLACK_REFUSED", "Slack refused the message (404 no_service)")
    only(svc, "ecs_targets")
    out = svc.run_once()
    assert out["events"][0]["delivered"] is False and "no_service" in out["events"][0]["error"]


def test_ecs_usage_and_aws_access(env):
    _, svc, _ = env
    r = validate_rule({"type": "ecs_usage", "params": {"metric": "cpu", "thresholdPercent": 90}})
    bad = {f.subject for f in svc.evaluate(r) if f.bad}
    assert bad == {"alpha-edds-2"}                               # 92% in the fake CloudWatch
    found = {f.subject: f.bad for f in svc.evaluate(validate_rule({"type": "app_aws"}))}
    assert found["Application logs in S3"] is False and found["ECS and load balancer APIs"] is False


def test_rule_errors_are_shown_not_raised(env):
    client, svc, _ = env
    only(svc, "ecs_targets")
    svc.health = None
    svc.run_once()
    d = client.get(URL, headers=ROOT_H).json()
    r = next(x for x in d["rules"] if x["type"] == "ecs_targets")
    assert "NOT_AVAILABLE" in r["status"]["lastError"]


def test_logs_stale_and_log_errors(env):
    client, svc, _ = env
    # the sample logs end at NOW, so with a 10-minute limit nothing is stale yet
    f = svc.evaluate(validate_rule({"type": "logs_stale", "params": {"minutes": 30}}), now_ms())
    assert f and not any(x.bad for x in f) and {x.subject for x in f} == {"post-btp-01", "post-btp-02", "pre-prod-01"}
    later = int((NOW + timedelta(minutes=90)).timestamp() * 1000)
    f = svc.evaluate(validate_rule({"type": "logs_stale", "params": {"clusters": ["post-btp-01"], "minutes": 30}}), later)
    assert len(f) == 1 and f[0].bad and "no new log files for" in f[0].message
    r = validate_rule({"type": "log_errors", "params": {"clusters": ["post-btp-01"], "minutes": 240, "threshold": 1}})
    f = svc.evaluate(r)
    assert f[0].bad and "lines matching level:error" in f[0].message and "newest:" in f[0].message
    assert f[0].link.startswith("/ui/logs/post-btp-01?q=level%3Aerror")
    # preview endpoint
    res = client.post(f"{URL}/_preview", json={"type": "log_errors", "params": {"clusters": ["post-btp-01"],
                      "minutes": 240, "threshold": 1000000}}, headers=ROOT_H).json()
    assert res["bad"] == 0 and res["findings"][0]["subject"] == "post-btp-01"


def test_lb_rules(tmp_path, logs_dir):
    settings = make_settings(tmp_path, logs_dir)
    now = datetime.now(timezone.utc)
    generate(Path(settings.lb_logs_local_dir), now=now, start=now - timedelta(hours=2), rows_per_file=40, seed=5)
    Converter(settings).run_once()
    app = create_app(settings, health=HealthService("us-west-2", clients=FakeAws()))
    svc = app.state.alerts
    f = svc.evaluate(validate_rule({"type": "lb_errors", "params": {"thresholdPercent": 0.1, "minRequests": 1,
                                                                     "minutes": 60}}))
    assert f and any(x.bad for x in f)
    hit = next(x for x in f if x.bad)
    assert "% 5xx:" in hit.message and hit.link.startswith(f"/ui/lb?tg={hit.subject}&st=5xx")
    f = svc.evaluate(validate_rule({"type": "lb_errors", "params": {"thresholdPercent": 99, "minutes": 60}}))
    assert f and not any(x.bad for x in f)
    # a specific code: 400 (count and share), a class, and a code inside a listed class counted once
    f = svc.evaluate(validate_rule({"type": "lb_status", "params": {"codes": "400", "threshold": 1, "minutes": 60}}))
    hit = next(x for x in f if x.bad)
    assert "× 400 (" in hit.message and "&code=400" in hit.link
    a = {x.subject: x.value for x in f}
    f = svc.evaluate(validate_rule({"type": "lb_status", "params": {"codes": "4xx, 400", "threshold": 1, "minutes": 60}}))
    b = {x.subject: x.value for x in f}
    assert all(b[k] >= a[k] for k in a) and "&st=4xx" not in f[0].link
    f = svc.evaluate(validate_rule({"type": "lb_status", "params": {"codes": "4xx", "threshold": 1, "minutes": 60}}))
    assert {x.subject: x.value for x in f} == b and "st=4xx" in f[0].link
    f = svc.evaluate(validate_rule({"type": "lb_status", "params": {"codes": "400", "threshold": 1,
                                                                     "thresholdPercent": 100, "minutes": 60}}))
    assert not any(x.bad for x in f)
    with pytest.raises(ApiError):
        validate_rule({"type": "lb_status", "params": {"codes": "40x"}})
    assert validate_rule({"type": "lb_status", "params": {"codes": " 502 503;504,502"}})["params"]["codes"] == "502,503,504"
    f = svc.evaluate(validate_rule({"type": "lb_latency", "params": {"p95Ms": 10, "minRequests": 1, "minutes": 60}}))
    assert any(x.bad for x in f)
    # newest ALB file per load balancer, from the converter status
    st = svc.lb.store.read_status()
    assert st["latestFile"]
    f = svc.evaluate(validate_rule({"type": "lb_logs_stale", "params": {"mode": "each", "minutes": 15}}))
    assert f and not any(x.bad for x in f)
    later = now_ms() + 120 * MINUTE
    f = svc.evaluate(validate_rule({"type": "lb_logs_stale", "params": {"mode": "all", "minutes": 60}}), later)
    assert len(f) == 1 and f[0].bad and f[0].subject == "All load balancers"
    f = svc.evaluate(validate_rule({"type": "app_converter"}))
    assert f[0].bad is False


def test_server_errors_counted(env, tmp_path):
    client, svc, _ = env
    errs = svc.errors
    for _ in range(3):
        errs.record(500, "POST", "/api/v1/clusters/x/logs/_search")
    errs.record(507, "POST", "/api/v1/clusters/x/logs/_search")
    f = svc.evaluate(validate_rule({"type": "app_errors", "params": {"threshold": 3, "minutes": 15}}))
    assert f[0].bad and "3 server errors" in f[0].message and "500 POST" in f[0].message
    assert isinstance(errs, ServerErrors)


def test_members_see_history_of_their_clusters(env):
    client, svc, _ = env
    only(svc, "ecs_targets", "app_disk", thresholdPercent=50)
    svc.update_rule(rule(svc, "app_disk")["id"], {"params": {"thresholdPercent": 50}}, "root")
    svc.run_once()
    add_user(client, "m@x.com", {"alpha-edds-2": "view"})
    items = client.get("/api/v1/alerts/history", headers=as_user("m@x.com")).json()["items"]
    assert items and {i["cluster"] for i in items} == {"alpha-edds-2"}


def test_rule_crud(env):
    client, svc, _ = env
    r = client.post(f"{URL}/rules", json={"type": "log_errors", "name": "edds errors",
                                          "params": {"clusters": ["post-btp-01"], "threshold": 5}}, headers=ROOT_H)
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    r = client.put(f"{URL}/rules/{rid}", json={"enabled": False, "params": {"threshold": 7}}, headers=ROOT_H)
    assert r.json()["enabled"] is False and r.json()["params"]["threshold"] == 7
    assert r.json()["params"]["clusters"] == ["post-btp-01"] and r.json()["type"] == "log_errors"
    assert client.delete(f"{URL}/rules/{rid}", headers=ROOT_H).json() == {"deleted": rid}
    assert client.delete(f"{URL}/rules/{rid}", headers=ROOT_H).status_code == 404
    opts = client.get(f"{URL}/options", headers=ROOT_H).json()
    assert "alpha-edds-2" in opts["ecsClusters"] and "tg-alpha-edds-2" in opts["targetGroups"]
    assert "post-btp-01" in opts["logClusters"]
