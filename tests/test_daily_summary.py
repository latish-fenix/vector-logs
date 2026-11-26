"""End-of-day ops summary (#daily-ops-digest) and the second Slack channel for summaries."""
from __future__ import annotations

import json

import shutil
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.alerts import MINUTE
from app.daily_summary import problem_stages
from app.ecs_health import HealthService
from app.main import create_app
from app.ops import DEFAULT_STATUS
from conftest import ROOT_H, make_settings
from fake_aws import FakeAws
from test_alerts import Slack
from test_ops import FELINA, INSTANT, RATE, REPO_CONFIG, ROUTE, VUORI, write_logs, write_metrics

ALERTS_HOOK = "https://hooks.slack.com/services/T000/B000/alertsalertsalerts"
DIGEST_HOOK = "https://hooks.slack.com/services/T000/B111/digestdigestdigest"
URL = "/api/v1/admin/alerts"
# Mon 9 Nov 2026 00:30 US Eastern (EST, UTC-5): Sunday 8 Nov has just ended there
NOW = datetime(2026, 11, 9, 5, 30, tzinfo=timezone.utc)
MS = lambda dt: int(dt.timestamp() * 1000)  # noqa: E731


@pytest.fixture()
def env(tmp_path):
    root = tmp_path / "logs"
    good = dict(calls=10, e4=1, e5=0, p95=100)
    # the EST day 8 Nov = 8 Nov 05:00 – 9 Nov 05:00 UTC; last week 1 Nov (a 25-hour day: clocks went back)
    for d in (date(2026, 11, 8), date(2026, 11, 1), date(2026, 11, 2)):
        write_metrics(root, d, None, {FELINA: good, VUORI: dict(calls=100, e4=0, e5=0, p95=200)})
    write_metrics(root, date(2026, 11, 9), datetime(2026, 11, 9, 5, 25, tzinfo=timezone.utc),
                  {FELINA: good, VUORI: dict(calls=100, e4=0, e5=5, p95=200)})
    # vuori: 5 % 5xx on the 8th (from 05:00 UTC, i.e. all of the EST day)
    write_metrics(root, date(2026, 11, 8), None, {FELINA: good, VUORI: dict(calls=100, e4=0, e5=5, p95=200)})
    t = datetime(2026, 11, 8, 15, tzinfo=timezone.utc)
    write_logs(root, "alpha-oms-routing", t, [(t + timedelta(minutes=1), ROUTE.format("111")), (t + timedelta(minutes=2), ROUTE.format("222"))])
    write_logs(root, "bombas-crs-1", t, [(t + timedelta(minutes=3), RATE.format("bombas.myshopify.com", "9001")),
                                         (t + timedelta(minutes=4), INSTANT.format("felinagroup.myshopify.com", "9002"))])
    lw = datetime(2026, 11, 1, 15, tzinfo=timezone.utc)
    write_logs(root, "alpha-oms-routing", lw, [(lw + timedelta(minutes=1), ROUTE.format("100"))])
    cfg = tmp_path / "ops-dashboard.yaml"
    shutil.copy(REPO_CONFIG, cfg)
    app = create_app(make_settings(tmp_path, root, ops_config_file=str(cfg)), health=HealthService("us-west-2", clients=FakeAws()))
    ops, svc = app.state.ops, app.state.alerts
    ops.now = ops.orders.now = lambda: NOW
    ops.orders.run_once(days_back=9)
    slack = Slack()
    svc.sender = slack
    svc.set_webhook(ALERTS_HOOK, "root")
    # an alert that started on the 8th (EST), and one from the day before
    svc._append_history([
        {"id": "a1", "at": MS(datetime(2026, 11, 8, 18, tzinfo=timezone.utc)), "event": "firing", "level": "critical",
         "ruleName": "Tasks below desired", "subject": "bombas-crs-1", "cluster": None},
        {"id": "a0", "at": MS(datetime(2026, 11, 8, 3, tzinfo=timezone.utc)), "event": "firing", "level": "warning",
         "ruleName": "CPU", "subject": "x", "cluster": None}])
    return TestClient(app), svc, slack


def test_message(env):
    """Template A (scoreboard): header, six boxes, bars per service, medals, problem stages, alerts, buttons."""
    from app.daily_summary import as_text
    _, svc, _ = env
    svc.update_settings({"consoleUrl": "https://logs.example.com"}, "root")
    msg = svc.daily_message("2026-11-08", now=MS(NOW))
    b = msg["blocks"]
    assert b[0] == {"type": "header", "text": {"type": "plain_text", "text": "🌅 Daily Ops Report · Sun 08 Nov", "emoji": True}}
    assert b[1]["elements"][0]["text"] == "🇺🇸 US Eastern day · compared with Sun 01 Nov"
    fields = {f["text"].split("\n")[0]: f["text"].split("\n")[1] for f in b[2]["fields"]}
    assert fields["*📈 API calls*"] == "*158,400*  📉 −4.0%"           # last week's day had 25 hours: 165,000
    assert fields["*🛒 Orders*"] == "*4*  📈 +300% 🚀"
    assert fields["*⚠️ 4xx rate*"] == "*0.91%*" and fields["*🔥 5xx rate*"] == "*4.55%* 🔴"
    assert fields["*⚡ Busiest minute*"] == "110/min at 00:00 EST (10:30 IST)"
    assert fields["*🚨 Alerts*"] == "1 started · nothing failing now ✅"
    text = as_text(b)
    assert "```\nOMS     ████████████████████  2  ▲ +100%\nCRS     ████████████████████  2\nOMS-11                        0\n```" in text
    assert "🥇 *Dermalogica* 2 📈 +100%   🥈 *Bombas* 1   🥉 *Felina* 1" in text
    assert "• 🔴 *vuori* (Vuori) — 5xx 5.00% (7,200 errors)" in text
    assert "1 started · 1 reached critical · most: Tasks below desired ×1" in text
    buttons = b[-2]["elements"]
    assert [x["text"]["text"] for x in buttons] == ["📊 Open the day", "🔔 Alerts page"] and buttons[0]["style"] == "primary"
    assert buttons[0]["url"].startswith("https://logs.example.com/ui/ops?tab=history&range=custom&from=")
    assert b[-1]["elements"][0]["text"].startswith(
        "Covers Sun 08 Nov 00:00 EST – Mon 09 Nov 00:00 EST (Sun 08 Nov 10:30 IST – Mon 09 Nov 10:30 IST)")
    assert msg["text"] == "Daily Ops Report Sun 08 Nov: 158,400 calls (📉 −4.0%), 4 orders (📈 +300%), 1 problem stage, 1 alert"
    assert msg["summary"]["title"] == "Sun 08 Nov (US Eastern)"
    assert len(b) <= 50 and all(len(f["text"]) <= 2000 for f in b[2]["fields"])     # Slack's limits


def test_critical_counted_once_per_alert(env):
    """An alert that starts as a warning and goes up to critical counts once ("20 started (23 critical)")."""
    _, svc, _ = env
    at = MS(datetime(2026, 11, 8, 20, tzinfo=timezone.utc))
    svc._append_history([
        {"id": "w1", "at": at, "event": "firing", "level": "warning", "ruleName": "CPU", "subject": "c1", "cluster": None},
        {"id": "w2", "at": at + 1, "event": "escalated", "level": "critical", "ruleName": "CPU", "subject": "c1", "cluster": None},
        {"id": "w3", "at": at + 2, "event": "escalated", "level": "critical", "ruleName": "CPU", "subject": "c1", "cluster": None}])
    from app.daily_summary import as_text
    text = as_text(svc.daily_message("2026-11-08", now=MS(NOW))["blocks"])
    assert "2 started · 2 reached critical" in text


def test_posts_once_after_midnight_to_the_summaries_channel(env):
    _, svc, slack = env
    cfg = svc.config()
    st: dict = {}
    assert svc._maybe_daily(cfg, st, MS(NOW) - 15 * MINUTE) is None              # 00:15 EST: not yet
    entry = svc._maybe_daily(cfg, st, MS(NOW))                                    # 00:30 EST
    assert entry["delivered"] and slack.sent[-1][0] == ALERTS_HOOK                # no summaries channel yet: alerts one
    svc.set_webhook(DIGEST_HOOK, "root", "digest")
    cfg = svc.config()
    assert svc._maybe_daily(cfg, st, MS(NOW) + 30 * MINUTE) is None               # once a day
    st.clear()
    assert svc._maybe_daily(cfg, st, MS(NOW) + 4 * 60 * MINUTE) is None           # 04:30 EST: too late (restart)
    svc._maybe_daily(cfg, st, MS(NOW) + 60 * MINUTE)
    assert slack.sent[-1][0] == DIGEST_HOOK and st["dailySentFor"] == "2026-11-08 EST"
    svc.update_settings({"daily": {"enabled": False}}, "root")
    st.clear()
    assert svc._maybe_daily(svc.config(), st, MS(NOW)) is None


def test_weekly_summary_goes_to_the_summaries_channel(env):
    _, svc, slack = env
    svc.send_digest("root")
    assert slack.sent[-1][0] == ALERTS_HOOK
    svc.set_webhook(DIGEST_HOOK, "root", "digest")
    svc.send_digest("root")
    assert slack.sent[-1][0] == DIGEST_HOOK
    # live alerts still go to the alerts channel
    assert svc.webhook() == ALERTS_HOOK


def test_routes(env):
    client, svc, slack = env
    r = client.put(f"{URL}/slack/digest", json={"webhookUrl": DIGEST_HOOK}, headers=ROOT_H)
    assert r.status_code == 200 and r.json()["configured"]
    d = client.get(URL, headers=ROOT_H).json()
    assert d["digestSlack"]["configured"] and d["digestSlack"]["hint"] != DIGEST_HOOK and d["dailyAvailable"]
    assert client.post(f"{URL}/slack/digest/_test", json={}, headers=ROOT_H).status_code == 200
    assert slack.sent[-1][0] == DIGEST_HOOK and "summaries will arrive here" in slack.sent[-1][1]["text"]
    svc.ops.now = lambda: NOW
    p = client.get(f"{URL}/daily/_preview?day=2026-11-08", headers=ROOT_H).json()
    assert "DAILY OPS REPORT · SUN 08 NOV" in p["text"] and p["summary"]["title"].startswith("Sun 08 Nov")
    assert client.get(f"{URL}/daily/_preview?day=2030-01-01", headers=ROOT_H).json()["error"]["code"] == "INVALID_DAY"
    r = client.post(f"{URL}/daily/_send?day=2026-11-08", headers=ROOT_H)
    assert r.status_code == 200 and r.json()["ruleName"] == "Daily summary" and slack.sent[-1][0] == DIGEST_HOOK
    for bad in ({"daily": {"zone": "CET"}}, {"daily": {"time": "24:00"}}):
        assert client.put(f"{URL}/settings", json=bad, headers=ROOT_H).json()["error"]["code"] == "INVALID_SETTINGS"
    s = client.put(f"{URL}/settings", json={"daily": {"zone": "PST", "time": "00:45"}}, headers=ROOT_H).json()
    assert s["daily"] == {"enabled": True, "zone": "PST", "time": "00:45"}
    assert client.delete(f"{URL}/slack/digest", headers=ROOT_H).json() == {"configured": False}
    assert svc.summary_webhook() == ALERTS_HOOK


def test_problem_rules():
    th = dict(DEFAULT_STATUS)
    row = lambda i, calls, e5=0, p95=200: {"id": i, "api": "A", "name": i, "calls": calls, "err5xx": e5, "p95": p95}  # noqa: E731
    out = problem_stages([row("a", 10_000, e5=60), row("b", 10_000, p95=900), row("c", 300), row("d", 10_000, e5=5)],
                         [row("a", 10_000), row("b", 10_000, p95=300), row("c", 5_000), row("e", 2_000), row("d", 10_000)], th)
    whys = {p[2].split("*")[1]: (p[1], p[3]) for p in out}
    assert whys["a"][0] == "🟠" and "5xx 0.60%" in whys["a"][1]                              # amber: ≥ 0.5 %
    assert whys["b"][0] == "🔴" and "3.0× last week" in whys["b"][1]
    assert "6% of last week" in whys["c"][1] and "0 calls" in whys["e"][1]                    # quiet / gone
    assert "d" not in whys                                                                    # 5 errors: too few
    assert out[0][0] == 2


# ------------------------------------------------------------------ which channel each alert goes to
INFRA_HOOK = "https://hooks.slack.com/services/T000/B222/infrainfrainfra1"


def _fire(svc, rtype: str, now: int):
    from app.alerts import Finding
    rule = next(r for r in svc.config()["rules"] if r["type"] == rtype)
    f = Finding(subject=f"{rtype}-subject", bad=True, message="bad")
    return svc._notify(svc.config(), [(rule, "firing", "critical", [(f, {"firedAt": now})], False)], now)


def test_error_rate_alerts_on_main_channel_the_rest_on_infra(env):
    client, svc, slack = env
    now = MS(NOW)
    # no infra webhook yet: everything still goes to the main channel
    assert _fire(svc, "ecs_tasks", now)[0]["delivered"] and slack.sent[-1][0] == ALERTS_HOOK
    r = client.put(f"{URL}/slack/infra", json={"webhookUrl": INFRA_HOOK}, headers=ROOT_H)
    assert r.status_code == 200 and client.get(URL, headers=ROOT_H).json()["infraSlack"]["configured"]
    for rtype, hook in (("lb_errors", ALERTS_HOOK), ("lb_status", ALERTS_HOOK), ("ecs_tasks", INFRA_HOOK),
                        ("lb_latency", INFRA_HOOK), ("logs_stale", INFRA_HOOK), ("app_disk", INFRA_HOOK)):
        if not any(r["type"] == rtype for r in svc.config()["rules"]):
            client.post(f"{URL}/rules", json={"type": rtype, **({"params": {"codes": "400"}} if rtype == "lb_status" else {})},
                        headers=ROOT_H)
        _fire(svc, rtype, now)
        assert slack.sent[-1][0] == hook, rtype
    # a rule can be moved to the other channel
    rules = client.get(URL, headers=ROOT_H).json()["rules"]
    ecs = next(r for r in rules if r["type"] == "ecs_tasks")
    assert ecs["channel"] == "infra" and next(r for r in rules if r["type"] == "lb_errors")["channel"] == "main"
    assert client.put(f"{URL}/rules/{ecs['id']}", json={"channel": "main"}, headers=ROOT_H).json()["channel"] == "main"
    _fire(svc, "ecs_tasks", now)
    assert slack.sent[-1][0] == ALERTS_HOOK
    assert client.put(f"{URL}/rules/{ecs['id']}", json={"channel": "slack"}, headers=ROOT_H).json()["error"]["code"] == "INVALID_ALERT"
    assert client.post(f"{URL}/slack/infra/_test", json={}, headers=ROOT_H).status_code == 200
    assert slack.sent[-1][0] == INFRA_HOOK and "Infra alerts" in slack.sent[-1][1]["text"]
    assert client.delete(f"{URL}/slack/infra", headers=ROOT_H).json() == {"configured": False}
    _fire(svc, "app_disk", now)
    assert slack.sent[-1][0] == ALERTS_HOOK                                    # back to the main channel


def test_api_gateway_p95_alert(env):
    from app.alerts import validate_rule
    from app.errors import ApiError
    client, svc, slack = env
    svc._update(lambda doc: [r.update(enabled=False) for r in doc["rules"]])
    r = client.post(f"{URL}/rules", json={"type": "apigw_latency", "params": {"p95Ms": 150, "minCalls": 100, "minutes": 15}},
                    headers=ROOT_H)
    assert r.status_code == 201
    opts = client.get(f"{URL}/options", headers=ROOT_H).json()
    assert {"Vuori/vuori", "Alpha-EDDS-1/felina"} <= set(opts["apiStages"])
    # vuori p95 200 ms (warning at 150, critical at the doubled 300); felina 100 ms is fine
    out = svc.run_once(MS(NOW))
    fired = [(e["subject"], e["level"]) for e in out["events"] if e["event"] == "firing"]
    assert len(fired) == 1 and fired[0][1] == "warning" and "vuori" in fired[0][0].lower()
    sent = [(u, json.dumps(m)) for u, m in slack.sent if "Slow responses (p95)" in json.dumps(m)]
    assert len(sent) == 1 and "p95 200 ms over 1,500 calls" in sent[0][1]
    assert sent[0][0] == ALERTS_HOOK                               # no infra webhook: the main one
    # a custom critical line; the backend-only measure; a stage filter
    f = svc.evaluate(validate_rule({"type": "apigw_latency", "params": {"p95Ms": 150, "criticalMs": 180}}), MS(NOW))
    v = next(x for x in f if x.bad)
    assert v.level_now("warning") == "critical" and v.link.startswith("/ui/ops?tab=history&stage=Vuori")
    f = svc.evaluate(validate_rule({"type": "apigw_latency", "params": {"p95Ms": 195, "measure": "integration"}}), MS(NOW))
    assert not any(x.bad for x in f) and any("backend p95 190 ms" in x.message for x in f)
    f = svc.evaluate(validate_rule({"type": "apigw_latency", "params": {"p95Ms": 50, "stages": ["Alpha-EDDS-1/felina"]}}), MS(NOW))
    assert len(f) == 1 and f[0].bad
    # API names with spaces and brackets can be picked
    r = client.post(f"{URL}/rules", json={"type": "apigw_latency", "params": {"stages": ["Fenix EDD API (prod)/v1", "Vuori/vuori"]}},
                    headers=ROOT_H)
    assert r.status_code == 201 and r.json()["params"]["stages"] == ["Fenix EDD API (prod)/v1", "Vuori/vuori"]
    assert client.post(f"{URL}/rules", json={"type": "apigw_latency", "params": {"stages": ["bad\nname"]}},
                       headers=ROOT_H).status_code == 400
    # too few calls: watched but never bad
    f = svc.evaluate(validate_rule({"type": "apigw_latency", "params": {"p95Ms": 50, "minCalls": 1000}}), MS(NOW))
    assert [x.bad for x in f if "felina" in x.link] == [False]
    # numbers too old: an error, not a judgement
    with pytest.raises(ApiError):
        svc.evaluate(validate_rule({"type": "apigw_latency"}), MS(NOW) + 60 * MINUTE)


def test_added_channels(env):
    client, svc, slack = env
    hook = "https://hooks.slack.com/services/T000/B222/latencylatencylatency"
    assert client.post(f"{URL}/channels", json={"name": "#api-latency", "webhookUrl": "nope"}, headers=ROOT_H).status_code == 400
    r = client.post(f"{URL}/channels", json={"name": "#api-latency", "webhookUrl": hook}, headers=ROOT_H)
    assert r.status_code == 201
    ch = r.json()
    assert ch["id"].startswith("c_") and ch["hint"] and hook not in json.dumps(ch)
    assert client.post(f"{URL}/channels", json={"name": "#API-latency", "webhookUrl": hook}, headers=ROOT_H).json()["error"]["code"] == "CHANNEL_EXISTS"
    assert svc.secrets.get(f"alerts-ch-{ch['id']}") == {"slackWebhookUrl": hook}
    assert hook not in json.dumps(svc.config()) and hook not in client.get(URL, headers=ROOT_H).text
    assert client.post(f"{URL}/channels/{ch['id']}/_test", json={}, headers=ROOT_H).json() == {"sent": True}
    assert slack.sent[-1][0] == hook and "#api-latency" in json.dumps(slack.sent[-1][1])
    # a rule posts to it
    rule = client.post(f"{URL}/rules", json={"type": "apigw_latency", "channel": ch["id"]}, headers=ROOT_H).json()
    assert client.post(f"{URL}/rules", json={"type": "apigw_latency", "channel": "c_00000000"}, headers=ROOT_H).status_code == 400
    listed = client.get(URL, headers=ROOT_H).json()
    assert next(x for x in listed["rules"] if x["id"] == rule["id"])["channel"] == ch["id"]
    assert [c["name"] for c in listed["channels"]] == ["#api-latency"]
    assert _fire(svc, "apigw_latency", MS(NOW))[0]["delivered"] and slack.sent[-1][0] == hook
    # rename + new webhook
    hook2 = hook.replace("B222", "B333")
    r = client.put(f"{URL}/channels/{ch['id']}", json={"name": "#p95", "webhookUrl": hook2}, headers=ROOT_H)
    assert r.status_code == 200 and r.json()["name"] == "#p95"
    _fire(svc, "apigw_latency", MS(NOW))
    assert slack.sent[-1][0] == hook2
    # removed: the rule goes back to its default (infra, else main)
    assert client.delete(f"{URL}/channels/{ch['id']}", headers=ROOT_H).json() == {"removed": "#p95", "rulesMoved": 1}
    assert svc.secrets.get(f"alerts-ch-{ch['id']}") is None
    assert next(x for x in client.get(URL, headers=ROOT_H).json()["rules"] if x["id"] == rule["id"])["channel"] == "infra"
    _fire(svc, "apigw_latency", MS(NOW))
    assert slack.sent[-1][0] == ALERTS_HOOK
