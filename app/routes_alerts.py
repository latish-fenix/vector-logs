"""Alerts API. Admins: /api/v1/admin/alerts (rules, settings and thresholds, Slack webhook, test,
preview, snooze, weekly summary). Everyone: GET /api/v1/alerts/history (members see the alerts of
their clusters)."""
from __future__ import annotations

import copy
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from .alerts import (CATALOG, DAILY_ZONES, DAYS, LB_LAG_MINUTES, MAIN_CHANNEL_TYPES, SNOOZE_MINUTES, TYPES,
                     AlertService, _err, channel_of, confirm_of, now_ms, settings_of, validate_rule)
from .errors import ApiError, not_found
from .identity import User, current_user, require_admin

admin = APIRouter(prefix="/api/v1/admin/alerts", tags=["alerts"])
public = APIRouter(prefix="/api/v1/alerts", tags=["alerts"])


class RuleBody(BaseModel):
    type: str | None = Field(None, description="One of the catalog types (only when creating)")
    name: str | None = Field(None, max_length=80)
    enabled: bool | None = None
    params: dict[str, Any] | None = None
    forMinutes: int | None = Field(None, description="Must be bad this long before it alerts (0 = at once)")
    channel: str | None = Field(None, description='"main" or "infra": the Slack channel this rule posts to '
                                '(default: main for HTTP error-rate and status-code rules, infra for the rest)')


class SettingsBody(BaseModel):
    consoleUrl: str | None = Field(None, max_length=300, description="https://… the address people open; "
                                   "used for links in Slack")
    timeZone: str | None = Field(None, max_length=64, description="IANA zone for times in Slack, e.g. Asia/Kolkata")
    timeZone2: str | None = Field(None, max_length=64, description='A second zone for times in Slack, e.g. '
                                  'America/Los_Angeles (PST/PDT); "" = none')
    paused: bool | None = Field(None, description="true = check but send nothing")
    mutesPaused: bool | None = Field(None, description="true = ignore the mute file (every alert is posted)")
    thresholds: dict[str, Any] | None = Field(None, description='{"cpu": {"warning": 70, "critical": 80}, "memory": '
                                              '{...}, "disk": {...}, "clusters": {"<ECS cluster>": {"cpu": {...}}}}')
    repeat: dict[str, Any] | None = Field(None, description='Minutes between reminders while failing, '
                                          '{"warning": 60, "critical": 15} (0 = never)')
    digest: dict[str, Any] | None = Field(None, description='Weekly summary {"enabled": true, "day": "fri", '
                                          '"time": "15:00"} (the settings\' time zone)')
    daily: dict[str, Any] | None = Field(None, description='End-of-day ops summary {"enabled": true, "zone": "EST", '
                                         '"time": "00:20"}: the day that just ended in zone (EST, PST or IST), '
                                         'posted at that time there')


class SnoozeBody(BaseModel):
    ruleId: str = Field(..., max_length=40)
    subject: str = Field(..., max_length=300)
    minutes: int | None = Field(None, ge=1, le=10080, description="Snooze this long; leave out with untilClear")
    untilClear: bool = Field(False, description="Snooze until the alert clears (then the OK message is posted)")


class WebhookBody(BaseModel):
    webhookUrl: str = Field(..., max_length=400)


class TestBody(BaseModel):
    webhookUrl: str | None = Field(None, max_length=400, description="Test this URL instead of the saved one")


def _svc(request: Request) -> AlertService:
    svc = getattr(request.app.state, "alerts", None)
    if svc is None:
        raise ApiError(404, "ALERTS_DISABLED", "Alerts are turned off on this server (ALERTS_ENABLED=false)")
    return svc


def _event(request: Request, user: User, action: str, **fields: Any) -> None:
    request.app.state.events.write({"action": action, "actor": user.username, "outcome": "SUCCESS",
                                    "requestId": request.state.request_id, **fields})


def _public_rule(rule: dict, status: dict | None, snoozes: dict) -> dict:
    status = status or {}
    subjects = status.get("subjects", {})
    now = now_ms()

    def items(state):
        out = []
        for k, v in subjects.items():
            if v["state"] != state:
                continue
            sn = snoozes.get(f"{rule['id']}|{k}")
            if sn and sn.get("until") is not None and sn["until"] <= now:
                sn = None
            out.append({"subject": k, **v, "snooze": sn})
        return sorted(out, key=lambda x: (-{"critical": 2, "warning": 1}.get(x.get("level"), 0), x["subject"]))
    spec = TYPES.get(rule["type"], {})
    return {**rule, "channel": channel_of(rule), "title": spec.get("title", rule["type"]), "level": spec.get("level"),
            "every": spec.get("every"), "confirm": confirm_of(rule),
            "status": {"lastRunAt": status.get("lastRunAt"), "lastError": status.get("lastError"),
                       "tookMs": status.get("tookMs"), "firing": items("firing"), "pending": items("pending"),
                       "muted": sorted(({"subject": k, **v} for k, v in (status.get("muted") or {}).items()
                                        if v.get("state", "muted") == "muted"),
                                       key=lambda x: x["subject"])}}


@admin.get("", summary="Rules with their current state, the settings and the catalog of alert types")
def overview(request: Request, user: User = Depends(require_admin)):
    svc = _svc(request)
    cfg = svc.config()
    st = svc.state()
    slack = cfg.get("slack") or {}
    dslack = cfg.get("digestSlack") or {}
    islack = cfg.get("infraSlack") or {}
    snoozes = svc.snoozes()
    now = now_ms()
    return {"settings": settings_of(cfg),
            "slack": {"configured": bool(slack.get("configured")), "hint": slack.get("hint"),
                      "updatedAt": slack.get("updatedAt"), "updatedBy": slack.get("updatedBy")},
            "digestSlack": {"configured": bool(dslack.get("configured")), "hint": dslack.get("hint"),
                            "updatedAt": dslack.get("updatedAt"), "updatedBy": dslack.get("updatedBy")},
            "infraSlack": {"configured": bool(islack.get("configured")), "hint": islack.get("hint"),
                           "updatedAt": islack.get("updatedAt"), "updatedBy": islack.get("updatedBy")},
            "mainChannelTypes": sorted(MAIN_CHANNEL_TYPES),
            "dailyAvailable": svc.ops is not None, "dailyZones": list(DAILY_ZONES),
            "checker": {"checkedAt": st.get("checkedAt"), **(st.get("checker") or {}),
                        "intervalSeconds": request.app.state.settings.alerts_interval_seconds,
                        "background": request.app.state.settings.alerts_background,
                        "digestSentFor": st.get("digestSentFor"), "dailySentFor": st.get("dailySentFor")},
            "rules": [_public_rule(r, st.get("rules", {}).get(r["id"]), snoozes) for r in cfg.get("rules", [])],
            "snoozes": sorted((s for s in snoozes.values() if s.get("until") is None or s["until"] > now),
                              key=lambda s: s.get("at", 0), reverse=True),
            "snoozeMinutes": list(SNOOZE_MINUTES), "days": list(DAYS),
            "mutes": svc.mute_overview(cfg, st),
            "catalog": CATALOG, "lbLagMinutes": LB_LAG_MINUTES}


@admin.get("/options", summary="Names to pick in rules: log clusters, ECS clusters, load balancers, target groups, API Gateway stages")
def options(request: Request, user: User = Depends(require_admin)):
    st = request.app.state
    out: dict[str, Any] = {"logClusters": [], "ecsClusters": [], "lbs": [], "targetGroups": [], "apiStages": [],
                           "errors": []}
    try:
        out["logClusters"] = st.logs.clusters()
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"Application logs: {_err(e)}")
    try:
        snap = st.health.snapshot()
        out["ecsClusters"] = sorted(c["name"] for c in snap["clusters"])
        out["targetGroups"] = sorted({t["name"] for c in snap["clusters"] for t in c["targetGroups"]}
                                     | {t["name"] for t in snap.get("unlinkedTargetGroups", [])})
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"ECS: {_err(e)}")
    try:
        ov = st.lb.overview(None)
        out["lbs"] = sorted({x["name"] for x in ov["items"]} | set((st.lb.store.read_status() or {}).get("latestFile", {})))
        out["targetGroups"] = sorted(set(out["targetGroups"]) | {t["name"] for x in ov["items"]
                                                                 for t in x["targetGroups"] if t.get("name")})
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"Load balancers: {_err(e)}")
    try:
        if getattr(st, "ops", None) is not None:
            out["apiStages"] = st.ops.stage_ids()
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"API Gateway: {_err(e)}")
    return out


@admin.post("/rules", status_code=201, summary="Add an alert rule")
def add_rule(body: RuleBody, request: Request, user: User = Depends(require_admin)):
    rule = _svc(request).add_rule(body.model_dump(exclude_none=True), user.username)
    _event(request, user, "ALERT_RULE_ADD", ruleId=rule["id"], type=rule["type"], name=rule["name"])
    return rule


@admin.put("/rules/{rule_id}", summary="Change an alert rule (its type stays)")
def update_rule(rule_id: str, body: RuleBody, request: Request, user: User = Depends(require_admin)):
    rule = _svc(request).update_rule(rule_id, body.model_dump(exclude_none=True), user.username)
    _event(request, user, "ALERT_RULE_UPDATE", ruleId=rule_id, name=rule["name"], enabled=rule["enabled"])
    return rule


@admin.delete("/rules/{rule_id}", summary="Delete an alert rule")
def delete_rule(rule_id: str, request: Request, user: User = Depends(require_admin)):
    rule = _svc(request).delete_rule(rule_id)
    _event(request, user, "ALERT_RULE_DELETE", ruleId=rule_id, name=rule["name"])
    return {"deleted": rule_id}


@admin.post("/_preview", summary="Run a rule (saved or draft) now and show what it finds; sends nothing")
def preview(body: RuleBody, request: Request, rule_id: str | None = Query(None, alias="ruleId"),
            user: User = Depends(require_admin)):
    svc = _svc(request)
    if rule_id:
        rule = next((r for r in svc.config().get("rules", []) if r["id"] == rule_id), None)
        if rule is None:
            raise not_found("ALERT_NOT_FOUND", "No alert rule with that id")
    else:
        rule = validate_rule(body.model_dump(exclude_none=True))
    memo = None
    if rule_id and TYPES.get(rule["type"], {}).get("memo"):     # a copy: a preview must not change it
        memo = copy.deepcopy((svc.state().get("rules", {}).get(rule_id) or {}).get("memo") or {})
    try:
        findings = svc.evaluate(rule, None, memo)
    except ApiError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ApiError(502, "CHECK_FAILED", f"The check failed: {_err(e)}") from e
    for f in [f for f in findings if f.bad][:3]:
        svc.explain(f)
    base = TYPES[rule["type"]].get("level")
    base = base if base in ("warning", "critical") else "warning"
    findings.sort(key=lambda f: (not f.bad, not f.muted_by, f.subject))
    return {"findings": [{"subject": f.subject, "bad": f.bad, "level": f.level_now(base), "message": f.message,
                          "mutedBy": f.muted_by, "mutedLevel": f.extra.get("mutedLevel"),
                          "value": f.value, "lines": f.lines, "link": f.link, "details": f.extra.get("details") or [],
                          "links": [list(x) for x in f.extra.get("links") or []]} for f in findings],
            "bad": sum(1 for f in findings if f.bad), "forMinutes": rule.get("forMinutes", 0),
            "confirm": confirm_of(rule)}


@admin.put("/settings", summary="Console address, time zone, pause, thresholds, repeat cadence, weekly summary")
def put_settings(body: SettingsBody, request: Request, user: User = Depends(require_admin)):
    out = _svc(request).update_settings(body.model_dump(exclude_none=True), user.username)
    _event(request, user, "ALERT_SETTINGS", changed=sorted(body.model_dump(exclude_none=True)))
    return out


@admin.post("/snooze", summary="Snooze one alert (rule + subject): post nothing about it for a while or until it clears")
def snooze(body: SnoozeBody, request: Request, user: User = Depends(require_admin)):
    if body.minutes is None and not body.untilClear:
        raise ApiError(400, "INVALID_SNOOZE", "Give minutes, or untilClear: true")
    out = _svc(request).snooze(body.ruleId, body.subject, None if body.untilClear else body.minutes, user.username)
    _event(request, user, "ALERT_SNOOZE", ruleId=body.ruleId, subject=body.subject, until=out.get("until"))
    return out


@admin.delete("/snooze", summary="End a snooze")
def unsnooze(request: Request, rule_id: str = Query(..., alias="ruleId"), subject: str = Query(...),
             user: User = Depends(require_admin)):
    if not _svc(request).unsnooze(rule_id, subject):
        raise not_found("SNOOZE_NOT_FOUND", "That alert isn't snoozed")
    _event(request, user, "ALERT_UNSNOOZE", ruleId=rule_id, subject=subject)
    return {"unsnoozed": True}


@admin.post("/digest/_send", summary="Post the weekly summary to Slack now")
def send_digest(request: Request, user: User = Depends(require_admin)):
    entry = _svc(request).send_digest(user.username)
    _event(request, user, "ALERT_DIGEST_SEND")
    return entry


@admin.post("/daily/_send", summary="Post the end-of-day ops summary to the summaries channel now")
def send_daily(request: Request, day: str | None = Query(None, description="YYYY-MM-DD in the summary's zone; default the last finished day"),
               user: User = Depends(require_admin)):
    entry = _svc(request).send_daily(user.username, day)
    _event(request, user, "ALERT_DAILY_SEND", day=day)
    return entry


@admin.get("/daily/_preview", summary="The end-of-day summary as it would be posted (nothing is sent)")
def preview_daily(request: Request, day: str | None = Query(None, description="YYYY-MM-DD; default the last finished day"),
                  user: User = Depends(require_admin)):
    from .daily_summary import as_text
    msg = _svc(request).daily_message(day)
    return {"text": as_text(msg["blocks"]), "summary": msg["summary"], "blocks": msg["blocks"]}


@admin.put("/slack/infra", summary="Save (or change) the infra channel webhook (every alert not on the main channel)")
def put_infra_slack(body: WebhookBody, request: Request, user: User = Depends(require_admin)):
    out = _svc(request).set_webhook(body.webhookUrl, user.username, "infra")
    _event(request, user, "ALERT_SLACK_SET", hint=out.get("hint"), channel="infra")
    return {"configured": True, "hint": out.get("hint"), "updatedAt": out.get("updatedAt"),
            "updatedBy": out.get("updatedBy")}


@admin.delete("/slack/infra", summary="Remove the infra channel webhook (infra alerts then go to the main channel)")
def delete_infra_slack(request: Request, user: User = Depends(require_admin)):
    _svc(request).clear_webhook(user.username, "infra")
    _event(request, user, "ALERT_SLACK_CLEAR", channel="infra")
    return {"configured": False}


@admin.post("/slack/infra/_test", summary="Send a test message to the infra channel")
def test_infra_slack(body: TestBody, request: Request, user: User = Depends(require_admin)):
    out = _svc(request).send_test(body.webhookUrl, user.username, "infra")
    _event(request, user, "ALERT_SLACK_TEST", channel="infra")
    return out


@admin.put("/slack/digest", summary="Save (or change) the summaries channel webhook (daily + weekly summaries)")
def put_digest_slack(body: WebhookBody, request: Request, user: User = Depends(require_admin)):
    out = _svc(request).set_webhook(body.webhookUrl, user.username, "digest")
    _event(request, user, "ALERT_SLACK_SET", hint=out.get("hint"), channel="digest")
    return {"configured": True, "hint": out.get("hint"), "updatedAt": out.get("updatedAt"),
            "updatedBy": out.get("updatedBy")}


@admin.delete("/slack/digest", summary="Remove the summaries channel webhook (summaries then go to the alerts channel)")
def delete_digest_slack(request: Request, user: User = Depends(require_admin)):
    _svc(request).clear_webhook(user.username, "digest")
    _event(request, user, "ALERT_SLACK_CLEAR", channel="digest")
    return {"configured": False}


@admin.post("/slack/digest/_test", summary="Send a test message to the summaries channel")
def test_digest_slack(body: TestBody, request: Request, user: User = Depends(require_admin)):
    out = _svc(request).send_test(body.webhookUrl, user.username, "digest")
    _event(request, user, "ALERT_SLACK_TEST", channel="digest")
    return out


@admin.put("/slack", summary="Save (or change) the Slack incoming webhook URL (kept in Secrets Manager)")
def put_slack(body: WebhookBody, request: Request, user: User = Depends(require_admin)):
    out = _svc(request).set_webhook(body.webhookUrl, user.username)
    _event(request, user, "ALERT_SLACK_SET", hint=out.get("hint"))
    return {"configured": True, "hint": out.get("hint"), "updatedAt": out.get("updatedAt"),
            "updatedBy": out.get("updatedBy")}


@admin.delete("/slack", summary="Remove the Slack webhook (alerts are then only kept in the history)")
def delete_slack(request: Request, user: User = Depends(require_admin)):
    _svc(request).clear_webhook(user.username)
    _event(request, user, "ALERT_SLACK_CLEAR")
    return {"configured": False}


@admin.post("/slack/_test", summary="Send a test message (to the saved webhook, or to the URL given)")
def test_slack(body: TestBody, request: Request, user: User = Depends(require_admin)):
    out = _svc(request).send_test(body.webhookUrl, user.username)
    _event(request, user, "ALERT_SLACK_TEST")
    return out


@public.get("/history", summary="Alerts sent recently (members: only those about their clusters)")
def history(request: Request, limit: int = Query(100, ge=1, le=500), user: User = Depends(current_user)):
    items = _svc(request).history(limit, user.can_view, user.admin)
    return {"items": items}
