"""Alerts: rules, checks against fake AWS / sample logs / converted ALB logs, Slack messages, the API."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from make_alb_logs import generate

from app.alerts import MINUTE, AlertService, Finding, ServerErrors, now_ms, validate_rule
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
        return ["\n".join(b["text"]["text"] for b in p["blocks"] if b["type"] == "section") for _, p in self.sent]


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
    assert len(d["catalog"]) == 14 and d["slack"]["configured"] is False
    assert d["settings"]["timeZone"] == "Asia/Kolkata"
    assert d["settings"]["thresholds"]["cpu"] == {"warning": 70, "critical": 80}
    assert d["settings"]["repeat"] == {"warning": 60, "critical": 15}
    assert d["settings"]["digest"] == {"enabled": True, "day": "fri", "time": "15:00"}
    assert "ecs_instances" in types and all("repeatMinutes" not in r for r in d["rules"])
    every = {c["type"]: c["every"] for c in d["catalog"]}
    assert every["ecs_usage"] == 5 and every["app_disk"] == 5
    assert every["ecs_tasks"] == every["ecs_targets"] == every["ecs_instances"] == 2
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
    assert svc.run_once(t)["events"] == [] and not slack.sent       # 2 checks in a row first
    out = svc.run_once(t + 2 * MINUTE)
    fired = {(e["type"], e["subject"]): e["level"] for e in out["events"] if e["event"] == "firing"}
    assert fired[("ecs_targets", "alpha-edds-2")] == "warning"                  # 1 of 3 unhealthy
    assert fired[("ecs_targets", "alpha-oms-routing")] == "critical"            # no healthy target left
    assert fired[("ecs_tasks", "alpha-oms-routing / oms-routing")] == "critical"  # service down
    assert ("ecs_tasks", "alpha-edds-2 / deliveryestimate") not in fired       # deploying: 10 minutes
    assert ("ecs_targets", "alpha-edds-1") not in fired
    assert len(slack.sent) == 3                          # one message per rule and level
    text = "\n".join(slack.texts())
    assert ":warning: *WARNING* · *Unhealthy targets*" in text
    assert ":red_circle: *CRITICAL* · *Service down or below its desired tasks*" in text
    assert "tg-alpha-edds-2: 1 of 3 not healthy (Target.FailedHealthChecks)" in text
    assert "<https://logs.example.com/ui/health?q=alpha-edds-2|alpha-edds-2>" in text
    assert "service down: running 0 of 1 tasks" in text
    # still bad: nothing new until the deployment grace is over
    svc.run_once(t + 4 * MINUTE)
    assert len(slack.sent) == 3
    out = svc.run_once(t + 10 * MINUTE)
    assert [(e["subject"], e["level"]) for e in out["events"]] == [("alpha-edds-2 / deliveryestimate", "warning")]
    assert "running 2 of 3 tasks" in slack.texts()[-1] and "unable to consistently start tasks" in slack.texts()[-1]
    # fixed: OK again
    import fake_aws
    old = fake_aws.TGS["tg-alpha-edds-2"]
    fake_aws.TGS["tg-alpha-edds-2"] = (old[0], old[1], [(i, p, "healthy", None) for i, p, _, _ in old[2]])
    try:
        svc.health.snapshot(refresh=True)
        svc.run_once(t + 12 * MINUTE)
    finally:
        fake_aws.TGS["tg-alpha-edds-2"] = old
    resolved = [x for x in slack.texts() if ":large_green_circle:" in x]
    assert resolved and "*OK again* · *Unhealthy targets*" in resolved[0] and "alpha-edds-2" in resolved[0]
    hist = client.get("/api/v1/alerts/history", headers=ROOT_H).json()["items"]
    assert hist[0]["event"] == "resolved" and all(h["delivered"] for h in hist)


def run(svc, rule, values, start=0, step=5, repeat=None):
    """Feed values to _transition (one check every `step` minutes); [(minute, kind, level)]."""
    rs, out = {}, []
    lines = {"warning": 70, "critical": 80, "h": 2, "unit": "%"}
    for i, v in enumerate(values):
        m = start + i * step
        for kind, level, _, _ in AlertService._transition(rule, rs, [Finding("x", v >= 70, f"{v}%", value=v, lines=lines)],
                                                           m * MINUTE + 1, repeat):
            out.append((m, kind, level))
    return out


def test_levels_hysteresis_and_confirm():
    cpu = {"id": "r", "type": "ecs_usage", "params": {"metric": "cpu"}, "forMinutes": 0}
    mem = {"id": "r", "type": "ecs_usage", "params": {"metric": "memory"}, "forMinutes": 0}
    no_repeat = {"warning": 0, "critical": 0}
    # CPU needs 2 checks in a row: one spike does nothing
    assert run(None, cpu, [95, 50, 95, 50], repeat=no_repeat) == []
    # warning, up to critical (2 checks), stays critical just under its line, down, clears 2 below
    assert run(None, cpu, [75, 76, 85, 86, 79, 77, 69, 68], repeat=no_repeat) == [
        (5, "firing", "warning"), (15, "escalated", "critical"), (25, "lowered", "warning"), (35, "resolved", "warning")]
    # memory alerts at the first check; a value around the line doesn't flap
    assert run(None, mem, [71, 69, 71, 69, 71, 60], repeat=no_repeat) == [(0, "firing", "warning"), (25, "resolved", "warning")]
    # reminders: critical every 15 minutes, warning every hour
    crit = run(None, mem, [90] * 7, repeat={"warning": 60, "critical": 15})
    assert crit == [(0, "firing", "critical"), (15, "repeat", "critical"), (30, "repeat", "critical")]
    warn = run(None, mem, [75] * 14, repeat={"warning": 60, "critical": 15})
    assert warn == [(0, "firing", "warning"), (60, "repeat", "warning")]


def test_alerts_firing_before_the_upgrade_dont_post_again():
    rule = {"id": "r", "type": "ecs_tasks", "params": {}, "forMinutes": 0}
    rs = {"subjects": {"c / s": {"state": "firing", "since": 0, "firedAt": 0, "notifiedAt": 0, "message": "x"},
                       "c / t": {"state": "firing", "since": 0, "firedAt": 0, "notifiedAt": 0, "message": "x"}}}
    f = [Finding("c / s", True, "running 1 of 2", level="warning"), Finding("c / t", False, "ok")]
    ev = AlertService._transition(rule, rs, f, 5 * MINUTE, {"warning": 60, "critical": 15})
    assert [(k, lvl, x.subject) for k, lvl, x, _ in ev] == [("resolved", "warning", "c / t")]
    assert rs["subjects"]["c / s"]["level"] == "warning"
    ev = AlertService._transition(rule, rs, f[:1], 61 * MINUTE, {"warning": 60, "critical": 15})
    assert [(k, lvl) for k, lvl, _, _ in ev] == [("repeat", "warning")]


def test_thresholds_settings_and_per_cluster(env):
    client, svc, _ = env
    r = validate_rule({"type": "ecs_usage", "params": {"metric": "cpu"}})
    f = {x.subject: x for x in svc.evaluate(r)}
    assert f["alpha-edds-2"].bad and f["alpha-edds-2"].level_now() == "critical"    # 92%: over 80
    assert not f["post-eibp-01"].bad
    res = client.put(f"{URL}/settings", json={"thresholds": {"clusters": {"alpha-edds-2": {"cpu": {
        "warning": 90, "critical": 95}}}}}, headers=ROOT_H)
    assert res.status_code == 200, res.text
    assert res.json()["thresholds"]["cpu"] == {"warning": 70, "critical": 80}           # the rest kept
    f = {x.subject: x for x in svc.evaluate(r)}
    assert f["alpha-edds-2"].level_now() == "warning" and f["alpha-edds-2"].lines["critical"] == 95
    assert f["alpha-edds-2"].lines["warning"] == 90
    for bad in ({"thresholds": {"cpu": {"warning": 80, "critical": 70}}},
                {"thresholds": {"disk": {"warning": 0, "critical": 70}}},
                {"repeat": {"critical": -1}}, {"digest": {"day": "friday"}}, {"digest": {"time": "25:00"}}):
        assert client.put(f"{URL}/settings", json=bad, headers=ROOT_H).status_code == 400, bad
    d = client.put(f"{URL}/settings", json={"repeat": {"critical": 30}, "digest": {"time": "16:30"}},
                   headers=ROOT_H).json()
    assert d["repeat"] == {"warning": 60, "critical": 30} and d["digest"]["time"] == "16:30"
    # load balancer rules keep their numbers: the old limit is the warning, critical is double
    rr = validate_rule({"type": "lb_errors", "params": {"thresholdPercent": 5}})
    assert rr["params"]["criticalPercent"] == 0
    with pytest.raises(ApiError):
        validate_rule({"type": "lb_errors", "params": {"thresholdPercent": 5, "criticalPercent": 4}})
    with pytest.raises(ApiError):
        validate_rule({"type": "lb_status", "params": {"threshold": 100, "criticalThreshold": 50}})


def test_old_config_is_migrated(env):
    _, svc, _ = env
    svc.store.put_json("state/alerts.json", {"version": 1, "settings": {"timeZone": "UTC"}, "slack": {}, "rules": [
        {"id": "a1", "type": "ecs_usage", "name": "High CPU", "enabled": True, "forMinutes": 0, "repeatMinutes": 30,
         "params": {"clusters": [], "metric": "cpu", "thresholdPercent": 90, "minutes": 15}},
        {"id": "a2", "type": "ecs_tasks", "name": "Service below its desired tasks", "enabled": True, "forMinutes": 10,
         "params": {"clusters": []}, "updatedBy": "default"},
        {"id": "a3", "type": "app_disk", "name": "Mine", "enabled": True, "forMinutes": 0,
         "params": {"thresholdPercent": 85}}]})
    cfg = svc.config()
    rules = {r["id"]: r for r in cfg["rules"]}
    assert cfg["version"] == 2 and cfg["settings"]["thresholds"]["disk"] == {"warning": 70, "critical": 80}
    assert "repeatMinutes" not in rules["a1"] and rules["a1"]["params"] == {"clusters": [], "metric": "cpu", "minutes": 10}
    assert rules["a2"]["forMinutes"] == 0 and rules["a2"]["name"] == "Service down or below its desired tasks"
    assert rules["a3"]["params"] == {} and rules["a3"]["name"] == "Mine"
    assert any(r["type"] == "ecs_instances" for r in cfg["rules"]) and len(cfg["rules"]) == 4
    assert svc.config()["rules"] == cfg["rules"]                          # once only


def test_snooze_until_clear_and_for_a_while(env):
    client, svc, slack = env
    svc.set_webhook(HOOK, "root")
    only(svc, "app_disk")
    rid = rule(svc, "app_disk")["id"]
    svc.update_settings({"thresholds": {"disk": {"warning": 1, "critical": 2}}}, "root")
    t = now_ms()
    out = svc.run_once(t)
    assert [(e["event"], e["level"]) for e in out["events"]] == [("firing", "critical")]
    assert ":red_circle: *CRITICAL* · *Viewer server disk*" in slack.texts()[0] and "critical line 2%" in slack.texts()[0]
    r = client.post(f"{URL}/snooze", json={"ruleId": rid, "subject": "Server disk", "untilClear": True}, headers=ROOT_H)
    assert r.status_code == 200 and r.json()["until"] is None
    d = client.get(URL, headers=ROOT_H).json()
    assert d["snoozes"][0]["subject"] == "Server disk"
    firing = next(x for x in d["rules"] if x["id"] == rid)["status"]["firing"]
    assert firing[0]["level"] == "critical" and firing[0]["snooze"]["by"] == "root"
    assert svc.run_once(t + 15 * MINUTE)["events"] == [] and len(slack.sent) == 1   # no reminder
    svc.update_settings({"thresholds": {"disk": {"warning": 98, "critical": 99}}}, "root")
    out = svc.run_once(t + 20 * MINUTE)
    assert out["events"][0]["event"] == "resolved" and out["events"][0]["delivered"]  # the OK is posted
    assert ":large_green_circle: *OK again*" in slack.texts()[-1] and svc.snoozes() == {}
    # for a while: nothing is posted, the change of level is kept in the history as snoozed
    svc.update_settings({"thresholds": {"disk": {"warning": 1, "critical": 99}}}, "root")
    svc.run_once(t + 25 * MINUTE)
    n = len(slack.sent)
    client.post(f"{URL}/snooze", json={"ruleId": rid, "subject": "Server disk", "minutes": 60}, headers=ROOT_H)
    svc.update_settings({"thresholds": {"disk": {"warning": 1, "critical": 2}}}, "root")
    out = svc.run_once(t + 30 * MINUTE)
    assert out["events"][0]["event"] == "escalated" and out["events"][0]["snoozed"] is True
    assert out["events"][0]["error"].startswith("Snoozed until") and len(slack.sent) == n
    # unsnooze: back to normal reminders
    assert client.delete(f"{URL}/snooze", params={"ruleId": rid, "subject": "Server disk"}, headers=ROOT_H).status_code == 200
    assert client.delete(f"{URL}/snooze", params={"ruleId": rid, "subject": "Server disk"}, headers=ROOT_H).status_code == 404
    svc.run_once(t + 46 * MINUTE)
    assert len(slack.sent) == n + 1 and "still failing" in slack.texts()[-1]
    assert client.post(f"{URL}/snooze", json={"ruleId": "nope", "subject": "x", "minutes": 5},
                       headers=ROOT_H).status_code == 404


def test_snooze_ends_and_reminds(env):
    _, svc, slack = env
    svc.set_webhook(HOOK, "root")
    only(svc, "app_disk")
    rid = rule(svc, "app_disk")["id"]
    svc.update_settings({"thresholds": {"disk": {"warning": 1, "critical": 2}}, "repeat": {"critical": 0}}, "root")
    t = now_ms()
    svc.run_once(t)
    svc.snooze(rid, "Server disk", 60, "root")
    item = svc.snoozes()[f"{rid}|Server disk"]
    assert item["until"] > t
    svc.run_once(item["until"] + 1)             # snooze over and still failing: one reminder
    assert len(slack.sent) == 2 and "still failing" in slack.texts()[-1] and svc.snoozes() == {}


def test_instances_leaving_a_cluster(env):
    import fake_aws
    _, svc, slack = env
    svc.set_webhook(HOOK, "root")
    only(svc, "ecs_instances")
    t = now_ms()
    assert svc.run_once(t)["events"] == []
    memo = svc.state()["rules"][rule(svc, "ecs_instances")["id"]]["memo"]
    assert "i-0018ced1d3ebbe05e" in memo["seen"]["alpha-edds-1"]
    insts, svcs = fake_aws.CLUSTERS["alpha-edds-1"]
    old = fake_aws.CLUSTERS["alpha-edds-1"]
    try:
        # scale-in: an instance goes, the service keeps its 3 tasks → no alert
        fake_aws.CLUSTERS["alpha-edds-1"] = (insts[1:], svcs)
        svc.health.snapshot(refresh=True)
        assert svc.run_once(t + 2 * MINUTE)["events"] == []
        assert svc.run_once(t + 4 * MINUTE)["events"] == []
        # another one goes and the service is short: critical after 2 checks
        fake_aws.CLUSTERS["alpha-edds-1"] = (insts[2:], [("deliveryestimate", 3, 2, None, "COMPLETED", "x")])
        svc.health.snapshot(refresh=True)
        assert svc.run_once(t + 6 * MINUTE)["events"] == []
        out = svc.run_once(t + 8 * MINUTE)
        assert [(e["subject"], e["level"]) for e in out["events"]] == [("alpha-edds-1 / i-00cc76cb809c61f3d", "critical")]
        assert "left the cluster at" in out["events"][0]["message"] and "deliveryestimate 2/3 tasks" in out["events"][0]["message"]
        # the service is back at 3 tasks: OK again
        fake_aws.CLUSTERS["alpha-edds-1"] = (insts[2:], svcs)
        svc.health.snapshot(refresh=True)
        out = svc.run_once(t + 10 * MINUTE)
        assert [e["event"] for e in out["events"]] == ["resolved"]
    finally:
        fake_aws.CLUSTERS["alpha-edds-1"] = old
    # an agent that disconnects is critical too
    orig = fake_aws.FakeECS.describe_container_instances

    def disconnected(self, cluster, containerInstances):
        out = orig(self, cluster, containerInstances)
        for i in out["containerInstances"]:
            if i["ec2InstanceId"] == "i-0e7a9f18ca3802d2c":
                i["agentConnected"] = False
        return out
    fake_aws.FakeECS.describe_container_instances = disconnected
    try:
        svc.health.snapshot(refresh=True)
        f = [x for x in svc.evaluate(rule(svc, "ecs_instances")) if x.bad]
        assert [(x.subject, x.level_now()) for x in f] == [("post-btp-01 / i-0e7a9f18ca3802d2c", "critical")]
        assert "ECS agent disconnected" in f[0].message and "172.0.43.6" in f[0].message
    finally:
        fake_aws.FakeECS.describe_container_instances = orig


def test_weekly_summary(env):
    client, svc, slack = env
    assert client.post(f"{URL}/digest/_send", headers=ROOT_H).json()["error"]["code"] == "NO_WEBHOOK"
    svc.set_webhook(HOOK, "root")
    only(svc, "ecs_tasks")
    t = now_ms()
    svc.run_once(t)
    svc.run_once(t + 2 * MINUTE)
    r = client.post(f"{URL}/digest/_send", headers=ROOT_H)
    assert r.status_code == 200 and r.json()["event"] == "digest"
    text = slack.texts()[-1]
    assert "*Weekly alert summary*" in text and "1 alert started, 1 reached critical" in text
    assert "*Failing now:* 1" in text and "alpha-oms-routing / oms-routing" in text and "all rules can check" in text
    # on its own: Friday 15:00 in the settings' time zone, once
    fri = int(datetime(2026, 10, 23, 9, 35, tzinfo=timezone.utc).timestamp() * 1000)    # 15:05 IST
    n = len(slack.sent)
    st = svc.state()
    assert svc._maybe_digest(svc.config(), st, fri - 10 * MINUTE) is None                  # 14:55
    assert svc._maybe_digest(svc.config(), st, fri)["delivered"] is True
    assert svc._maybe_digest(svc.config(), st, fri + 60 * MINUTE) is None and len(slack.sent) == n + 1
    svc.update_settings({"digest": {"enabled": False}}, "root")
    st["digestSentFor"] = None
    assert svc._maybe_digest(svc.config(), st, fri) is None


def test_paused_or_no_webhook_keeps_history(env):
    client, svc, slack = env
    only(svc, "ecs_targets")
    t = now_ms()
    svc.run_once(t)
    out = svc.run_once(t + 2 * MINUTE)
    assert out["events"] and all(e["error"] == "No Slack webhook set" for e in out["events"]) and not slack.sent
    svc.set_webhook(HOOK, "root")
    svc.update_settings({"paused": True}, "root")
    only(svc, "ecs_tasks")
    svc.run_once(t + 4 * MINUTE)
    out = svc.run_once(t + 6 * MINUTE)
    assert out["events"][0]["error"] == "Notifications are paused" and not slack.sent


def test_slack_failure_is_recorded(env):
    _, svc, slack = env
    svc.set_webhook(HOOK, "root")
    slack.fail = ApiError(502, "SLACK_REFUSED", "Slack refused the message (404 no_service)")
    only(svc, "ecs_targets")
    t = now_ms()
    svc.run_once(t)
    out = svc.run_once(t + 2 * MINUTE)
    assert out["events"][0]["delivered"] is False and "no_service" in out["events"][0]["error"]


def test_ecs_usage_and_aws_access(env):
    _, svc, _ = env
    r = validate_rule({"type": "ecs_usage", "params": {"metric": "cpu"}})
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
    assert "% 5xx · " in hit.message and " requests · usual " in hit.message
    assert hit.link.startswith(f"/ui/lb?tg={hit.subject}&st=5xx")
    # explained: codes and who answered, failing paths, the usual rate, links to the failed requests
    svc.explain(hit)
    d = "\n".join(hit.extra["details"])
    assert d.startswith("Codes: 5") and ("service" in d or "load balancer" in d)
    assert "Paths: " in d and "Usual rate:" in d
    assert "App logs: none" in d                       # the demo target groups have no Vector log folder
    assert hit.extra["links"][0][0] == "Failed requests" and "/ui/lb-logs?tg=" in hit.extra["links"][0][1]
    msg = svc.message(svc.config(), {"name": "5xx", "id": "x"}, "firing", [(hit, {})], now_ms())
    text = "\n".join(b["text"]["text"] for b in msg["blocks"] if b["type"] == "section")
    # compact: one line, then the likely cause; no list of details; no links without a console address
    assert "◦ Codes:" not in text and "↳ `" in text and "Failed requests" not in text
    assert text.splitlines()[0].startswith(":red_circle: *CRITICAL* · *5xx* ·")
    assert "critical line 0.2%" in text                    # the 0.1% warning line doubled
    svc.update_settings({"consoleUrl": "https://logs.example.com"}, "root")
    msg = svc.message(svc.config(), {"name": "5xx", "id": "x"}, "firing", [(hit, {})], now_ms())
    text = "\n".join(b["text"]["text"] for b in msg["blocks"] if b["type"] == "section")
    assert "|Failed requests>" in text and "https://logs.example.com/ui/lb-logs?tg=" in text
    assert "|ECS health>" not in text                      # only when targets are unhealthy
    # several target groups: one header with the count and the window, a line each
    hits = [x for x in svc.evaluate(validate_rule({"type": "lb_errors", "params": {
        "thresholdPercent": 0.1, "minRequests": 1, "minutes": 60}})) if x.bad]
    for x in hits:
        svc.explain(x)
    msg = svc.message(svc.config(), {"name": "5xx", "id": "x"}, "firing", [(x, {}) for x in hits], now_ms())
    assert len(hits) > 1 and f"— {len(hits)} target groups ·" in msg["blocks"][0]["text"]["text"]
    # only when unusual: an impossible multiple of the usual rate stops it
    f = svc.evaluate(validate_rule({"type": "lb_errors", "params": {"thresholdPercent": 0.1, "minRequests": 1,
                                                                     "minutes": 60, "vsUsual": 50}}))
    assert f and not any(x.bad for x in f)
    f = svc.evaluate(validate_rule({"type": "lb_errors", "params": {"thresholdPercent": 99, "minutes": 60}}))
    assert f and not any(x.bad for x in f)
    # a specific code: 400 (count and share), a class, and a code inside a listed class counted once
    f = svc.evaluate(validate_rule({"type": "lb_status", "params": {"codes": "400", "threshold": 1, "minutes": 60}}))
    hit = next(x for x in f if x.bad)
    assert " × 400 · " in hit.message and "&code=400" in hit.link
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
    only(svc, "ecs_targets", "app_disk")
    svc.update_settings({"thresholds": {"disk": {"warning": 1, "critical": 2}}}, "root")
    t = now_ms()
    svc.run_once(t)
    svc.run_once(t + 2 * MINUTE)
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


def test_explain_adds_app_errors_and_target_health(env):
    """A target group named tg-<cluster> with Vector logs: the alert shows that service's ERROR lines
    from the same minutes and the target health from ECS."""
    from app.alerts import Finding
    client, svc, _ = env
    t = now_ms()
    f = Finding("tg-post-btp-01", True, "50% of requests failed with 5xx", cluster="post-btp-01",
                extra={"lb": {"start": t - 4 * 3600_000, "end": t, "classes": ["5xx"], "codes": [], "pct": 50,
                              "logs": True}})
    svc.explain(f, t)
    d = "\n".join(f.extra["details"])
    assert "App logs (post-btp-01):" in d and "ERROR lines" in d and "Newest: " in d
    assert "Targets now: 2 of 2 healthy" in d and "(ECS cluster post-btp-01)" in d
    labels = [x[0] for x in f.extra["links"]]
    assert labels[0] == "Failed requests" and "App errors" in labels and "ECS health" in labels
    # turned off: no log lines in the alert
    g = Finding("tg-post-btp-01", True, "x", cluster="post-btp-01",
                extra={"lb": {**f.extra["lb"], "logs": False}})
    svc.explain(g, t)
    assert not any(x.startswith("App logs") for x in g.extra["details"])


def test_slack_times_in_ist_and_pacific(env):
    client, svc, slack = env
    d = client.get(URL, headers=ROOT_H).json()
    assert d["settings"]["timeZone"] == "Asia/Kolkata" and d["settings"]["timeZone2"] == "America/Los_Angeles"
    cfg = svc.config()
    summer = int(datetime(2026, 10, 22, 3, 35, tzinfo=timezone.utc).timestamp() * 1000)   # PDT
    winter = int(datetime(2026, 12, 22, 3, 35, tzinfo=timezone.utc).timestamp() * 1000)   # PST
    assert svc._fmt(cfg, summer, True) == "22 Oct 09:05 IST / 21 Oct 20:35 PDT"
    assert svc._fmt(cfg, winter, True) == "22 Dec 09:05 IST / 21 Dec 19:35 PST"
    assert svc._fmt(cfg, summer) == "09:05 IST / 20:35 PDT"
    span = svc._span(cfg, summer - 15 * MINUTE, summer)       # a day is added where it isn't today
    assert "08:50–09:05 IST / " in span and span.endswith("20:20–20:35 PDT")
    # the Slack footer of every message carries both
    svc.set_webhook(HOOK, "root")
    client.post(f"{URL}/slack/_test", json={}, headers=ROOT_H)
    footer = slack.sent[-1][1]["blocks"][-1]["elements"][0]["text"]
    assert " IST / " in footer and ("PDT" in footer or "PST" in footer)
    # one zone only
    assert client.put(f"{URL}/settings", json={"timeZone2": ""}, headers=ROOT_H).json()["timeZone2"] == ""
    assert svc._fmt(svc.config(), summer, True) == "22 Oct 09:05 IST" and svc._fmt(svc.config(), summer) == "09:05"
    assert client.put(f"{URL}/settings", json={"timeZone2": "Mars/Base"}, headers=ROOT_H).status_code == 400


def test_slack_shows_the_whole_error(env):
    """The service's error is shown in full (a code block, up to 2,000 characters), without its stack trace."""
    from app.alerts import Finding, _error_text
    long = "Delivery Estimates:Delivery Estimates:com.fenix.RequestValidatorAndBuilderService " + "x" * 400
    assert _error_text({"msg": long + "\n\tat com.fenix.A.b(A.java:1)\n\tat c.D.e(D.java:2)",
                        "exception": "org.elasticsearch.ElasticsearchException"}) == \
        "org.elasticsearch.ElasticsearchException: " + long
    assert _error_text({"msg": "y" * 3000}).endswith("…") and len(_error_text({"msg": "y" * 3000})) == 2001
    client, svc, _ = env
    t = now_ms()
    f = Finding("tg-post-btp-01", True, "50 × 400", cluster="post-btp-01",
                extra={"lb": {"start": t - 4 * 3600_000, "end": t, "classes": ["4xx"], "codes": [], "pct": 50,
                              "logs": True}, "facts": {}})
    f.extra.pop("facts")
    svc.explain(f, t)
    msg = svc.message(svc.config(), {"name": "400s", "id": "x"}, "firing", [(f, {})], t)
    text = "\n".join(b["text"]["text"] for b in msg["blocks"] if b["type"] == "section")
    app = f.extra["facts"]["app"]
    # every kind of error is listed, with its count: not only the most common one
    assert "Errors in post-btp-01's logs, same minutes:" in text and f"{app['n']:,} ERROR lines" in text
    assert len(app["groups"]) >= 4 and sum(g["n"] for g in app["groups"]) == app["n"]
    for g in app["groups"]:
        assert f"{g['n']:,} × " in text
    assert "IllegalArgumentException: value cannot be null" in text and "404 Not Found" in text


def test_long_alerts_are_split_into_several_messages(env):
    from app.alerts import Finding
    _, svc, _ = env
    f = Finding("tg-x", True, "1 × 400", extra={"facts": {"app": {"n": 9000, "kinds": 120, "cluster": "x", "groups": [
        {"n": 100 - i % 50, "exception": "com.a.BoomException", "error": f"com.a.BoomException: kind {i} " + "z" * 300,
         "stores": [f"store-{j}" for j in range(5)]} for i in range(120)]}}})
    msg = svc.message(svc.config(), {"name": "400s", "id": "x"}, "firing", [(f, {})], now_ms())
    assert all(len(b["text"]["text"]) <= 2900 for b in msg["blocks"] if b["type"] == "section")
    text = "\n".join(b["text"]["text"] for b in msg["blocks"] if b["type"] == "section")
    assert all(f"kind {i} " in text for i in range(120))                   # all of them
    assert all(b["text"]["text"].count("```") % 2 == 0 for b in msg["blocks"] if b["type"] == "section")
    parts = svc._payloads(msg)
    assert len(parts) > 1 and all(len(p["blocks"]) <= 50 for p in parts)
    assert parts[0]["text"].endswith(f"(1/{len(parts)})") and "Vector Logs viewer" in parts[-1]["blocks"][-1]["elements"][0]["text"]
