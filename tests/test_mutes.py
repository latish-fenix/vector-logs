"""The alert mute file (config/alert-mutes.yaml): parsing, reloading, and what each kind of entry mutes."""
from __future__ import annotations

import os
import time
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.alerts import MINUTE, TYPES, AlertService, now_ms, validate_rule
from app.ecs_health import HealthService
from app.main import create_app
from app.mutes import MuteFile, MuteFileError, parse
from conftest import NOW, ROOT_H, make_settings
from fake_aws import FakeAws

HOOK = "https://hooks.slack.com/services/T000/B000/abcdefghijklmnop"
ROOT = Path(__file__).resolve().parent.parent


class Slack:
    def __init__(self):
        self.sent = []

    def __call__(self, url, payload):
        self.sent.append(payload)

    def texts(self):
        return ["\n".join(b["text"]["text"] for b in p["blocks"] if b["type"] == "section") for p in self.sent]


@pytest.fixture()
def env(tmp_path, logs_dir):
    app = create_app(make_settings(tmp_path, logs_dir), health=HealthService("us-west-2", clients=FakeAws()))
    slack = Slack()
    svc: AlertService = app.state.alerts
    svc.sender = slack
    svc.set_webhook(HOOK, "root")
    return TestClient(app), svc, slack, Path(svc.mutes.path)


def write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    t = time.time() + (os.stat(path).st_mtime_ns % 1000) / 1e6 + 1      # a new mtime even within the same tick
    os.utime(path, (t, t))


def only(svc, *types):
    def mutate(doc):
        for r in doc["rules"]:
            r["enabled"] = r["type"] in types
    svc._update(mutate)


def rule(svc, rtype):
    return next(r for r in svc.config()["rules"] if r["type"] == rtype)


def test_the_repo_file_is_valid():
    ms = parse((ROOT / "config" / "alert-mutes.yaml").read_text(), set(TYPES))
    ids = [m.id for m in ms]
    assert len(ids) == len(set(ids)) >= 25 and all(m.enabled for m in ms)
    assert {"AL-001", "AL-025", "AL-038", "AL-051", "AL-063"} <= set(ids)
    al33 = next(m for m in ms if m.id == "AL-033")
    assert al33.kind == "signature" and al33.signatures == ["productInfos"]      # the code error only
    # AL-003 (searchByWebId :: Empty elastic search index name: null) must keep alerting: no entry matches it
    al003 = "searchByWebId :: Empty elastic search index name: null"
    assert not [m.id for m in ms if any(x.lower() in al003.lower() for x in m.signatures)]
    assert [m.id for m in ms if any(x.lower() in "no such index [x.myshopify.com-conversion_guided_transit_time_info]"
                                    for x in m.signatures)] == ["AL-001"]


def test_mistakes_are_explained():
    for text, says in [
        ("mutes: [{id: A, match: {targetGroup: [x]}}]", "unknown match key"),
        ("mutes: [{id: A, match: {targetGroups: [x]}}, {id: A, match: {targetGroups: [y]}}]", "used twice"),
        ("mutes: [{id: A}]", "'match' is required"),
        ("mutes: [{id: A, match: {codes: [40x], targetGroups: [x]}}]", "isn't a status code"),
        ("mutes: [{id: A, match: {rules: [nope], targetGroups: [x]}}]", "unknown rule type"),
        ("mutes: [{id: A, match: {codes: [400]}}]", "would mute everything"),
        ("mutes: [{id: A, enabled: maybe, match: {targetGroups: [x]}}]", "true or false"),
        ("mutes: [{id: A, match: {paths: [fenix/x]}}]", "must look like"),
        ("mutes:\n  - id: A\n    match: {targetGroups: [x]\n", "line"),
    ]:
        with pytest.raises(MuteFileError, match=says):
            parse(text, set(TYPES))


def test_reloads_and_keeps_the_last_good_version(tmp_path):
    p = tmp_path / "m.yaml"
    f = MuteFile(str(p), set(TYPES))
    assert f.get() == [] and not f.exists and f.error is None              # no file: nothing muted
    write(p, "mutes:\n  - id: A\n    match: {targetGroups: [tg-a]}\n")
    assert [m.id for m in f.get()] == ["A"]
    write(p, "mutes:\n  - id: A\n    match: {targetGroup: [tg-a]}\n")         # typo
    assert [m.id for m in f.get()] == ["A"] and "unknown match key" in f.error
    write(p, "mutes:\n  - id: B\n    enabled: false\n    match: {clusters: [x]}\n")
    assert [(m.id, m.enabled) for m in f.get()] == [("B", False)] and f.error is None
    p.unlink()
    assert f.get() == []


def test_matching_helpers():
    m = parse("mutes: [{id: A, match: {targetGroups: [tg-alpha-edds-*], paths: ['POST /x/<store>/info', '/y/*'],"
              " codes: [4xx], signatures: [boom]}}]")[0]
    assert m.tg_ok("tg-alpha-edds-11") and not m.tg_ok("tg-post-btp-01")
    assert m.path_ok("POST", "/x/<store>/info") and m.path_ok("post", "/x/vuori/info") and not m.path_ok("GET", "/x/a/info")
    assert m.path_ok("GET", "/y/a/b") and m.codes_ok(["400"]) and not m.codes_ok(["5xx"])
    assert m.cluster_ok("alpha-edds-2") and not m.cluster_ok("post-btp-01")


def test_subject_mute_is_kept_in_history_and_can_be_turned_off(env):
    client, svc, slack, path = env
    only(svc, "ecs_tasks")
    write(path, "mutes:\n  - id: AL-054\n    title: OMS routing capacity\n    match:\n"
                "      rules: [ecs_tasks]\n      clusters: [alpha-oms-routing]\n")
    t = now_ms()
    svc.run_once(t)
    out = svc.run_once(t + 2 * MINUTE)
    ev = {(e["event"], e["subject"]) for e in out["events"]}
    assert ("muted", "alpha-oms-routing / oms-routing") in ev and ("firing", "alpha-oms-routing / oms-routing") not in ev
    muted = next(e for e in out["events"] if e["event"] == "muted")
    assert muted["delivered"] is False and muted["mutedBy"] == ["AL-054"] and muted["level"] == "critical"
    assert "Muted by AL-054" in muted["error"] and not any("oms-routing" in x for x in slack.texts())
    assert svc.run_once(t + 4 * MINUTE)["events"] == []                     # recorded once, not every check
    text = svc.digest(svc.config(), t + 5 * MINUTE)["blocks"][0]["text"]["text"]
    assert "*Muted this week:* 1 — AL-054 ×1 · mute file: 1 of 1 entries on" in text
    d = client.get("/api/v1/admin/alerts", headers=ROOT_H).json()
    assert d["mutes"]["entries"][0]["id"] == "AL-054" and d["mutes"]["entries"][0]["muted24h"] == 1
    r = next(x for x in d["rules"] if x["type"] == "ecs_tasks")
    assert r["status"]["muted"][0]["subject"] == "alpha-oms-routing / oms-routing"
    # turned off in the file: it alerts again (after its 2 checks)
    write(path, "mutes:\n  - id: AL-054\n    enabled: false\n    match:\n      rules: [ecs_tasks]\n"
                "      clusters: [alpha-oms-routing]\n")
    svc.run_once(t + 6 * MINUTE)
    out = svc.run_once(t + 8 * MINUTE)
    assert ("firing", "alpha-oms-routing / oms-routing") in {(e["event"], e["subject"]) for e in out["events"]}
    assert any("oms-routing" in x for x in slack.texts())


def test_pause_all_mutes(env):
    client, svc, slack, path = env
    only(svc, "ecs_tasks")
    write(path, "mutes:\n  - id: X\n    match: {clusters: [alpha-oms-routing]}\n")
    assert client.put("/api/v1/admin/alerts/settings", json={"mutesPaused": True}, headers=ROOT_H).json()["mutesPaused"]
    t = now_ms()
    svc.run_once(t)
    out = svc.run_once(t + 2 * MINUTE)
    assert ("firing", "alpha-oms-routing / oms-routing") in {(e["event"], e["subject"]) for e in out["events"]}
    assert client.get("/api/v1/admin/alerts", headers=ROOT_H).json()["mutes"]["paused"] is True


def test_a_firing_alert_stops_quietly_when_muted(env):
    _, svc, slack, path = env
    only(svc, "ecs_tasks")
    t = now_ms()
    svc.run_once(t)
    svc.run_once(t + 2 * MINUTE)
    n = len(slack.sent)
    assert any("oms-routing" in x for x in slack.texts())
    write(path, "mutes:\n  - id: X\n    match: {subjects: ['alpha-oms-routing / *']}\n")
    out = svc.run_once(t + 4 * MINUTE)
    assert [(e["event"], e["subject"]) for e in out["events"]] == [("muted", "alpha-oms-routing / oms-routing")]
    assert "stopped without an OK message" in out["events"][0]["error"] and len(slack.sent) == n


def test_health_reason_mute(env):
    _, svc, _, path = env
    write(path, "mutes:\n  - id: AL-051\n    match:\n      rules: [ecs_targets]\n"
                "      healthReasons: [Target.FailedHealthChecks]\n")
    f = {x.subject: x for x in svc.evaluate(rule(svc, "ecs_targets"))}
    assert not f["alpha-edds-2"].bad and f["alpha-edds-2"].muted_by == ["AL-051"]
    assert f["alpha-oms-routing"].bad and not f["alpha-oms-routing"].muted_by               # Target.Timeout


def test_converter_error_mute(env, monkeypatch):
    _, svc, _, path = env
    write(path, "mutes:\n  - id: AL-063\n    match: {rules: [app_converter], converterErrors: [OutOfMemory]}\n")
    from app.util import iso
    monkeypatch.setattr(svc, "_converter_status", lambda: {"updatedAt": iso(), "lastError": {
        "code": "LB_HOURS_FAILED", "message": "1 hour failed: OutOfMemoryException: could not allocate 89 MiB"}})
    f = svc.evaluate(rule(svc, "app_converter"))
    assert f[0].muted_by == ["AL-063"] and not f[0].bad
    monkeypatch.setattr(svc, "_converter_status", lambda: {"updatedAt": iso(), "lastError": {
        "code": "LB_LOGS_ACCESS_DENIED", "message": "AccessDenied"}})
    assert svc.evaluate(rule(svc, "app_converter"))[0].bad                                   # any other error alerts


def test_error_text_mutes_use_the_services_own_errors(env):
    """post-btp-01 writes 4 kinds of ERROR lines in the sample logs: muted only when all 4 are in the file."""
    _, svc, _, path = env
    start, end = int((NOW - timedelta(hours=6)).timestamp() * 1000), int(NOW.timestamp() * 1000)
    three = ["401 Unauthorized", "value cannot be null", "404 Not Found"]
    entry = ("mutes:\n  - id: K\n    match:\n      targetGroups: [tg-post-btp-01]\n      codes: [5xx]\n"
             "      paths: ['POST /api/x']\n      signatures: [{}]\n")
    write(path, entry.format(", ".join(f'"{x}"' for x in three)))
    rows = [("tg-post-btp-01", "POST", "/api/x", 500, 10)]
    assert svc._lb_sig_mutes("lb_errors", ["5xx"], "tg-post-btp-01", None, start, end, rows) is None   # ENTITY_NOT_FOUND
    write(path, entry.format(", ".join(f'"{x}"' for x in three + ["ENTITY_NOT_FOUND"])))
    assert svc._lb_sig_mutes("lb_errors", ["5xx"], "tg-post-btp-01", None, start, end, rows) == ["K"]
    # requests failing on a path the entry doesn't name: posted
    rows2 = rows + [("tg-post-btp-01", "GET", "/api/other", 502, 3)]
    assert svc._lb_sig_mutes("lb_errors", ["5xx"], "tg-post-btp-01", None, start, end, rows2) is None
    # another status class, or a service without logs: posted
    assert svc._lb_sig_mutes("lb_status", ["400"], "tg-post-btp-01", None, start, end, rows) is None
    assert svc._lb_sig_mutes("lb_errors", ["5xx"], "tg-post-btp-01", "no-such-cluster", start, end, rows) is None
    # the application-log rule doesn't count the muted lines
    write(path, "mutes:\n  - id: K\n    match:\n      clusters: [post-btp-01]\n      signatures: "
                "[\"401 Unauthorized\", \"value cannot be null\", \"404 Not Found\", ENTITY_NOT_FOUND]\n")
    r = validate_rule({"type": "log_errors", "params": {"clusters": ["post-btp-01"], "minutes": 240, "threshold": 1}})
    f = svc.evaluate(r, end)
    assert not f[0].bad and f[0].muted_by == ["K"] and "muted lines not counted" in f[0].message


def test_load_balancer_path_mutes(tmp_path, logs_dir):
    from datetime import datetime, timezone

    from make_alb_logs import generate

    from app.lb_logs import Converter, LbQuery
    settings = make_settings(tmp_path, logs_dir)
    now = datetime.now(timezone.utc)
    generate(Path(settings.lb_logs_local_dir), now=now, start=now - timedelta(hours=2), rows_per_file=40, seed=5)
    Converter(settings).run_once()
    app = create_app(settings, health=HealthService("us-west-2", clients=FakeAws()))
    svc = app.state.alerts
    params = {"thresholdPercent": 0.1, "minRequests": 1, "minutes": 60}
    hit = next(x for x in svc.evaluate(validate_rule({"type": "lb_errors", "params": params})) if x.bad)
    st = svc.lb.paths(LbQuery(start=hit.extra["lb"]["start"], end=hit.extra["lb"]["end"], target_groups=[hit.subject],
                              status_classes=["5xx"]), None, sort="requests", limit=50)["items"]
    failing = [i for i in st if i["requests"]]
    m, pg = failing[0]["method"], failing[0]["pathGroup"]
    # one failing path muted: those requests aren't counted (or the alert is muted when it was the only one)
    Path(svc.mutes.path).write_text(f"mutes:\n  - id: P\n    match:\n      targetGroups: [{hit.subject}]\n"
                                    f"      paths: ['{m} {pg}']\n")
    f = next(x for x in svc.evaluate(validate_rule({"type": "lb_errors", "params": params})) if x.subject == hit.subject)
    assert "on muted paths not counted" in f.message
    assert f.value < hit.value or f.muted_by == ["P"]
    # every failing path muted: muted, not posted
    Path(svc.mutes.path).write_text("mutes:\n  - id: P\n    match:\n      targetGroups: [" + hit.subject + "]\n"
                                    "      paths: [" + ", ".join(f"'{i['method']} {i['pathGroup']}'" for i in failing)
                                    + "]\n")
    os.utime(svc.mutes.path, (time.time() + 5, time.time() + 5))
    f = next(x for x in svc.evaluate(validate_rule({"type": "lb_errors", "params": params})) if x.subject == hit.subject)
    assert not f.bad and f.muted_by == ["P"]
    # a whole target group, for the load balancer rules only
    Path(svc.mutes.path).write_text(f"mutes:\n  - id: TG\n    match:\n      rules: [lb_errors]\n"
                                    f"      targetGroups: [{hit.subject}]\n")
    os.utime(svc.mutes.path, (time.time() + 10, time.time() + 10))
    f = next(x for x in svc.evaluate(validate_rule({"type": "lb_errors", "params": params})) if x.subject == hit.subject)
    assert not f.bad and f.muted_by == ["TG"]


def test_muted_errors_are_never_listed(env):
    """An error in the file is left out of every alert's list, for any target group and status code."""
    from app.alerts import Finding
    _, svc, _, path = env
    write(path, "mutes:\n  - id: AL-001\n    match:\n      signatures: [\"value cannot be null\"]\n")
    t = int(NOW.timestamp() * 1000)
    f = Finding("tg-post-btp-01", True, "50 × 500", cluster="post-btp-01",
                extra={"lb": {"start": t - 4 * 3600_000, "end": t, "classes": ["5xx"], "codes": [], "logs": True}})
    svc._rtype = "lb_errors"
    svc.explain(f, t)
    app = f.extra["facts"]["app"]
    assert app["known"]["ids"] == ["AL-001"] and app["known"]["n"] > 0
    assert not any("value cannot be null" in (g["error"] + (g.get("example") or "")) for g in app["groups"])
    text = "\n".join(b["text"]["text"] for b in svc.message(svc.config(), {"name": "5xx", "id": "x"}, "firing",
                                                             [(f, {})], t)["blocks"] if b["type"] == "section")
    assert "known (AL-001 in the mute file) not listed" in text and "value cannot be null" not in text
