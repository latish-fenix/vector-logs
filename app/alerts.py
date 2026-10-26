"""Alerts to Slack: rules that check the viewer, the logs, ECS and the load balancers, and post to
a Slack incoming webhook when something goes wrong (and again when it is OK again).

Where things live
    state/alerts.json          rules + settings (console URL, time zone, paused,       S3 state store
                               thresholds, repeat cadence, weekly summary)
    state/alerts-state.json    what each rule found last time (firing / pending)       S3 state store
    state/alerts-history.json  the last 500 alerts sent (or not sent, and why)         S3 state store
    state/alerts-snoozes.json  snoozed alerts                                           S3 state store
    <prefix>alerts             {"slackWebhookUrl": ...}                                 Secrets Manager
    CACHE_DIR/alerts/          the checker's lock and the viewer's own 5xx responses   local disk

One checker runs per container (every API worker starts one; a file lock lets only one check).
It wakes up every ALERTS_INTERVAL_SECONDS and runs each enabled rule when it is due (CPU, memory
and disk every 5 minutes, ECS health every 2).

Levels. An alert is a warning or critical. CPU, memory, the server disk and the load balancer
error rates have two lines (Thresholds, per ECS cluster for CPU and memory); service down and
an instance leaving a cluster are always critical. A subject (a cluster, a target group, a
service …) that is bad becomes pending, and firing once it has been bad for the rule's confirm
checks in a row (CPU: 2) and forMinutes. Firing posts once; a change of level posts at once;
while it lasts a warning repeats every hour and a critical every 15 minutes (Settings), and an
"OK again" message is posted when it clears. It clears only 2 points below its line, so a value
going up and down around the line doesn't post again and again. A snoozed alert posts nothing
until the snooze ends. Every Friday 15:00 (the settings' time zone) a weekly summary is posted.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .errors import ApiError, bad_request, not_found
from .mutes import MuteFile, code_in
from .util import iso, iso_ms, new_id

log = logging.getLogger("vector_logs.alerts")

CONFIG_KEY = "state/alerts.json"
STATE_KEY = "state/alerts-state.json"
HISTORY_KEY = "state/alerts-history.json"
SNOOZE_KEY = "state/alerts-snoozes.json"
SECRET = "alerts"
CONFIG_VERSION = 2
LEVELS = ("warning", "critical")
RANK = {None: 0, "warning": 1, "critical": 2}
HYSTERESIS = 2                 # an alert clears only this many points below its line
DEPLOY_GRACE_MINUTES = 10      # a service below its tasks during a deployment gets this long
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DEFAULT_THRESHOLDS = {"cpu": {"warning": 70, "critical": 80}, "memory": {"warning": 70, "critical": 80},
                      "disk": {"warning": 70, "critical": 80}, "clusters": {}}
DEFAULT_REPEAT = {"warning": 60, "critical": 15}
DEFAULT_DIGEST = {"enabled": True, "day": "fri", "time": "15:00"}
SNOOZE_MINUTES = (60, 240, 1440)
DEFAULT_TIME_ZONE2 = "America/Los_Angeles"     # Slack times also in Pacific (PST / PDT); "" = only the first zone
HISTORY_MAX = 500
MAX_RULES = 100
MINUTE = 60_000
LB_LAG_MINUTES = 10          # ALB files arrive every 5 minutes and are converted every 5 minutes
WEBHOOK_RE = re.compile(r"^https://hooks\.slack\.com/(services|triggers|workflows)/[A-Za-z0-9/_\-]{10,300}$")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]{0,127}$")
ZONE_RE = re.compile(r"^[A-Za-z_]+(/[A-Za-z0-9_+\-]+){0,2}$|^UTC$")


def now_ms() -> int:
    return int(time.time() * 1000)


# ------------------------------------------------------------------ the catalog of rule types
def _num(key, label, default, lo, hi, unit="", help_=""):
    return {"key": key, "kind": "number", "label": label, "default": default, "min": lo, "max": hi,
            "unit": unit, "help": help_}


def _pick(key, kind, label, help_="Leave empty for all.", required=False):
    return {"key": key, "kind": kind, "label": label, "default": [], "required": required, "help": help_}


def _select(key, label, options, default, help_=""):
    return {"key": key, "kind": "select", "label": label, "default": default,
            "options": [{"value": v, "label": lab} for v, lab in options], "help": help_}


INCLUDE_LOGS = {"key": "includeLogs", "kind": "bool", "default": True,
                "label": "Explain it: add the service's own error lines from the same minutes",
                "help": "When the target group's service has application logs (tg-<cluster>), the alert shows how many "
                        "ERROR lines it wrote, the top exceptions and the newest message (up to 200 characters; log "
                        "messages can hold customer details)."}

VS_USUAL = {"key": "vsUsual", "kind": "number", "default": 0, "min": 0, "max": 100, "unit": "× its usual rate (0 = always)",
            "label": "Only when unusual: at least",
            "help": "Compares with the same target group over the 24 hours before. 2 = alert only when the rate is at "
                    "least double what is normal for it. Stops alerts for errors that are always there."}

CATALOG: list[dict] = [
    {"type": "app_aws", "group": "Vector Logs viewer", "title": "Viewer can't reach AWS", "level": "critical",
     "description": "The viewer can't list the application logs in S3, read the ECS and load balancer state, "
                    "or read CloudWatch metrics (access denied, wrong region, no network).",
     "every": 1, "forMinutes": 2, "params": []},
    {"type": "app_converter", "group": "Vector Logs viewer", "title": "Load balancer log converter failing",
     "level": "warning",
     "description": "The converter that turns the ALB access logs into the dashboard's data reports an error, "
                    "or hasn't updated for longer than the limit.",
     "every": 1, "forMinutes": 0,
     "params": [_num("stalledMinutes", "Stalled after", 30, 10, 1440, "min")]},
    {"type": "app_disk", "group": "Vector Logs viewer", "title": "Viewer server disk", "level": "two",
     "description": "The disk of the server that runs this viewer (its cache: Parquet copies, converted load "
                    "balancer data) is fuller than the warning or critical line in Thresholds.",
     "every": 5, "forMinutes": 0, "params": []},
    {"type": "app_errors", "group": "Vector Logs viewer", "title": "Viewer server errors", "level": "warning",
     "description": "The viewer answered with server errors (HTTP 5xx) at least this often. Searches that are too "
                    "big for the memory limit (507) don't count.",
     "every": 1, "forMinutes": 0,
     "params": [_num("threshold", "At least", 10, 1, 100000, "errors"),
                _num("minutes", "Within", 15, 5, 1440, "min")]},
    {"type": "logs_stale", "group": "Application logs", "title": "Application logs stopped arriving",
     "level": "warning",
     "description": "Vector hasn't written any new log file for a cluster for longer than the limit "
                    "(the service is down, Vector stopped, or it can't write to S3).",
     "every": 5, "forMinutes": 0,
     "params": [_pick("clusters", "logClusters", "Clusters"),
                _num("minutes", "No new files for", 30, 10, 1440, "min")]},
    {"type": "log_errors", "group": "Application logs", "title": "Error lines in application logs",
     "level": "warning",
     "description": "A search over a cluster's logs (by default level:error) finds at least this many lines "
                    "in the window. The search uses the same syntax as the Logs page.",
     "every": 5, "forMinutes": 0,
     "params": [_pick("clusters", "logClusters", "Clusters", "Pick at least one.", required=True),
                {"key": "query", "kind": "text", "label": "Search", "default": "level:error",
                 "help": 'Words, "phrases", field:value, -word, e.g. level:error -"timeout"'},
                _num("threshold", "At least", 10, 1, 1_000_000, "lines"),
                _num("minutes", "Within", 15, 5, 240, "min"),
                {"key": "includeSample", "kind": "bool", "label": "Add the newest matching message to the alert",
                 "default": True, "help": "Up to 200 characters. Log messages can hold customer details."}]},
    {"type": "lb_logs_stale", "group": "Load balancers", "title": "Load balancer logs stopped arriving",
     "level": "warning",
     "description": "AWS hasn't delivered new access log files to S3. AWS writes no file for 5 minutes without "
                    "requests, so watch quiet load balancers with \"All of them\".",
     "every": 5, "forMinutes": 0,
     "params": [_pick("lbs", "lbs", "Load balancers"),
                _select("mode", "Alert when", [("all", "All of them have stopped (delivery is broken)"),
                                               ("each", "Any one of them has stopped")], "all"),
                _num("minutes", "No new files for", 60, 15, 1440, "min")]},
    {"type": "lb_errors", "group": "Load balancers", "title": "HTTP error rate", "level": "two",
     "description": f"Share of requests answered with 5xx (or 4xx) per target group, from the ALB access logs. "
                    f"The data is about {LB_LAG_MINUTES} minutes behind.",
     "every": 5, "forMinutes": 0,
     "params": [_pick("targetGroups", "targetGroups", "Target groups"),
                _select("statusClass", "Status", [("5xx", "5xx (server errors)"), ("4xx", "4xx (client errors)")], "5xx"),
                _num("thresholdPercent", "Warning at", 5, 0.1, 100, "% of requests"),
                _num("criticalPercent", "Critical at", 0, 0, 100, "% of requests (0 = double the warning)"),
                _num("minRequests", "Only with at least", 50, 1, 10_000_000, "requests"),
                _num("minutes", "Within", 15, 5, 240, "min"),
                VS_USUAL,
                INCLUDE_LOGS]},
    {"type": "lb_status", "group": "Load balancers", "title": "Specific status codes (e.g. 400)", "level": "two",
     "description": f"Requests answered with the given status codes per target group, e.g. 400, 404, 502,503 or a "
                    f"whole class like 4xx, from the ALB access logs. Combine \"Within\" with \"Alert only after it "
                    f"has been bad for\" to alert on codes that keep coming. The data is about {LB_LAG_MINUTES} minutes behind.",
     "every": 5, "forMinutes": 0,
     "params": [_pick("targetGroups", "targetGroups", "Target groups"),
                {"key": "codes", "kind": "codes", "label": "Status codes", "default": "400",
                 "help": "Comma-separated: 400 · 400,404 · 502,503,504 · 4xx"},
                _num("threshold", "Warning at", 100, 1, 100_000_000, "requests with these codes"),
                _num("criticalThreshold", "Critical at", 0, 0, 100_000_000, "requests (0 = double the warning)"),
                _num("thresholdPercent", "and at least", 0, 0, 100, "% of the requests (0 = any share)"),
                _num("minutes", "Within", 15, 5, 240, "min"),
                VS_USUAL,
                INCLUDE_LOGS]},
    {"type": "lb_latency", "group": "Load balancers", "title": "Slow responses (p95)", "level": "warning",
     "description": f"95th percentile of the target's response time per target group, from the ALB access logs "
                    f"(approximate). The data is about {LB_LAG_MINUTES} minutes behind.",
     "every": 5, "forMinutes": 0,
     "params": [_pick("targetGroups", "targetGroups", "Target groups"),
                _num("p95Ms", "p95 at least", 2000, 10, 600_000, "ms"),
                _num("minRequests", "Only with at least", 50, 1, 10_000_000, "requests"),
                _num("minutes", "Within", 15, 5, 240, "min")]},
    {"type": "ecs_targets", "group": "ECS", "title": "Unhealthy targets", "level": "two", "confirm": 2,
     "description": "Targets of an ECS cluster's target groups fail their health checks (unhealthy, draining, "
                    "unavailable or still starting). Warning when some are unhealthy, critical when a target "
                    "group has no healthy target left. Checked every 2 minutes; alerts when 2 checks in a row agree.",
     "every": 2, "forMinutes": 0,
     "params": [_pick("clusters", "ecsClusters", "ECS clusters"),
                _num("minUnhealthy", "At least", 1, 1, 1000, "targets")]},
    {"type": "ecs_tasks", "group": "ECS", "title": "Service down or below its desired tasks", "level": "two",
     "confirm": 2,
     "description": "An ECS service runs no tasks at all (service down: critical) or fewer than it should "
                    f"(warning; during a deployment only after {DEPLOY_GRACE_MINUTES} minutes). Checked every "
                    "2 minutes; alerts when 2 checks in a row agree.",
     "every": 2, "forMinutes": 0,
     "params": [_pick("clusters", "ecsClusters", "ECS clusters")]},
    {"type": "ecs_instances", "group": "ECS", "title": "Instance left the cluster", "level": "critical",
     "confirm": 2, "memo": True,
     "description": "An EC2 instance of an ECS cluster lost its ECS agent or stopped being ACTIVE, or disappeared "
                    "while the cluster's services are below their desired tasks (a planned scale-in that keeps every "
                    "service at its count doesn't alert). Always critical; stays until the services are back. "
                    "Checked every 2 minutes; alerts when 2 checks in a row agree.",
     "every": 2, "forMinutes": 0,
     "params": [_pick("clusters", "ecsClusters", "ECS clusters")]},
    {"type": "ecs_usage", "group": "ECS", "title": "High CPU or memory", "level": "two",
     "description": "Average CPU or memory use of an ECS cluster (CloudWatch) is above the warning or critical "
                    "line in Thresholds (per cluster). Checked every 5 minutes; CPU alerts when 2 checks in a "
                    "row are high.",
     "every": 5, "forMinutes": 0,
     "params": [_pick("clusters", "ecsClusters", "ECS clusters"),
                _select("metric", "Metric", [("cpu", "CPU"), ("memory", "Memory")], "cpu"),
                _num("minutes", "Average over the last", 10, 5, 60, "min")]},
]
TYPES = {t["type"]: t for t in CATALOG}

# Rules created on first use (they only send messages once a webhook is set).
DEFAULT_RULES = [
    ("app_aws", "Viewer can't reach AWS", {}),
    ("app_converter", "Load balancer log converter failing", {}),
    ("app_disk", "Viewer server disk", {}),
    ("app_errors", "Viewer server errors", {}),
    ("logs_stale", "Application logs stopped arriving", {}),
    ("lb_logs_stale", "Load balancer logs stopped arriving", {}),
    ("ecs_targets", "Unhealthy targets", {}),
    ("ecs_tasks", "Service down or below its desired tasks", {}),
    ("ecs_instances", "Instance left the cluster", {}),
    ("ecs_usage", "High CPU", {"metric": "cpu"}),
    ("ecs_usage", "High memory", {"metric": "memory"}),
    ("lb_errors", "5xx rate above 5%", {}),
]
RENAMED = {"Server disk almost full": "Viewer server disk",
           "Service below its desired tasks": "Service down or below its desired tasks"}


def migrate_config(doc: dict) -> bool:
    """Version 1 → 2 (levels): thresholds move to Settings, repeats to Settings, ECS health is
    checked every 2 minutes with 2 checks in a row instead of forMinutes, a rule for instances
    leaving a cluster. Returns True when it changed the document."""
    if int(doc.get("version", 1)) >= CONFIG_VERSION:
        return False
    for r in doc.get("rules", []):
        r.pop("repeatMinutes", None)
        if r.get("updatedBy") == "default" and r.get("name") in RENAMED:
            r["name"] = RENAMED[r["name"]]
        params = r.setdefault("params", {})
        if r["type"] in ("ecs_targets", "ecs_tasks"):
            r["forMinutes"] = 0
        if r["type"] in ("ecs_usage", "app_disk"):
            params.pop("thresholdPercent", None)
        if r["type"] == "ecs_usage" and params.get("minutes", 10) > 60:
            params["minutes"] = 60
        if r["type"] == "ecs_usage" and params.get("minutes") == 15:
            params["minutes"] = 10
    if not any(r["type"] == "ecs_instances" for r in doc.get("rules", [])):
        doc.setdefault("rules", []).append({"id": new_id()[:10], **validate_rule({"type": "ecs_instances",
                                            "name": "Instance left the cluster"}),
                                            "createdAt": iso(), "updatedBy": "default"})
    st = doc.setdefault("settings", {})
    st.setdefault("thresholds", json.loads(json.dumps(DEFAULT_THRESHOLDS)))
    st.setdefault("repeat", dict(DEFAULT_REPEAT))
    st.setdefault("digest", dict(DEFAULT_DIGEST))
    doc["version"] = CONFIG_VERSION
    return True


def settings_of(cfg: dict) -> dict:
    """The settings with every default filled in."""
    s = dict(cfg.get("settings") or {})
    t = s.get("thresholds") or {}
    s["thresholds"] = {**{k: dict(v) for k, v in DEFAULT_THRESHOLDS.items() if k != "clusters"},
                       **{k: v for k, v in t.items() if k in ("cpu", "memory", "disk")},
                       "clusters": dict(t.get("clusters") or {})}
    s["repeat"] = {**DEFAULT_REPEAT, **(s.get("repeat") or {})}
    s["digest"] = {**DEFAULT_DIGEST, **(s.get("digest") or {})}
    return {"consoleUrl": "", "timeZone": "UTC", "timeZone2": DEFAULT_TIME_ZONE2, "paused": False,
            "mutesPaused": False, **s}


def lines_for(cfg: dict, metric: str, cluster: str | None = None) -> dict:
    """{"warning", "critical", "h", "unit"} for cpu / memory (per cluster) or disk."""
    t = settings_of(cfg)["thresholds"]
    pair = ((t["clusters"].get(cluster) or {}).get(metric) if cluster else None) or t[metric]
    return {"warning": pair["warning"], "critical": pair["critical"], "h": HYSTERESIS, "unit": "%"}


def level_of(value: float | None, lines: dict | None, current: str | None = None) -> str | None:
    """warning / critical / None for a value against two lines. An alert that is already at a
    level keeps it until the value is `h` below that level's line (hysteresis)."""
    if value is None or not lines:
        return None
    h = lines.get("h", 0)
    if value >= lines["critical"] or (current == "critical" and value > lines["critical"] - h):
        return "critical"
    if value >= lines["warning"] or (current in LEVELS and value > lines["warning"] - h):
        return "warning"
    return None


def confirm_of(rule: dict) -> int:
    """Checks in a row a subject must be bad before it alerts."""
    spec = TYPES.get(rule["type"], {})
    if rule["type"] == "ecs_usage":
        return 2 if (rule.get("params") or {}).get("metric", "cpu") == "cpu" else 1
    return int(spec.get("confirm", 1))


# ------------------------------------------------------------------ validation
def _defaults(spec: dict) -> dict:
    return {p["key"]: p["default"] for p in spec["params"]}


def validate_rule(body: dict, existing: dict | None = None) -> dict:
    """A clean rule from an API body (unknown keys ignored, values checked against the catalog)."""
    rtype = (existing or {}).get("type") or body.get("type")
    spec = TYPES.get(rtype or "")
    if spec is None:
        raise bad_request("UNKNOWN_ALERT_TYPE", f"Unknown alert type '{rtype}'", {"types": list(TYPES)})
    name = str(body.get("name") or spec["title"]).strip()
    if not 1 <= len(name) <= 80:
        raise bad_request("INVALID_ALERT", "The name must have 1 to 80 characters")
    given = body.get("params") or {}
    if not isinstance(given, dict):
        raise bad_request("INVALID_ALERT", "params must be an object")
    params: dict[str, Any] = {}
    for p in spec["params"]:
        v = given.get(p["key"], p["default"])
        label = p["label"]
        if p["kind"] == "number":
            try:
                v = float(v)
            except (TypeError, ValueError):
                raise bad_request("INVALID_ALERT", f"'{label}' must be a number") from None
            if not p["min"] <= v <= p["max"]:
                raise bad_request("INVALID_ALERT", f"'{label}' must be between {p['min']} and {p['max']}")
            v = int(v) if v == int(v) else v
        elif p["kind"] in ("logClusters", "ecsClusters", "lbs", "targetGroups"):
            if not isinstance(v, list) or len(v) > 200 or not all(isinstance(x, str) and NAME_RE.match(x) for x in v):
                raise bad_request("INVALID_ALERT", f"'{label}' must be a list of names")
            v = sorted(set(v))
            if p.get("required") and not v:
                raise bad_request("INVALID_ALERT", f"Pick at least one in '{label}'")
        elif p["kind"] == "select":
            if v not in [o["value"] for o in p["options"]]:
                raise bad_request("INVALID_ALERT", f"'{label}' must be one of "
                                  + ", ".join(o["value"] for o in p["options"]))
        elif p["kind"] == "codes":
            v = ",".join(parse_codes(v, label))
        elif p["kind"] == "text":
            v = str(v or "").strip()
            if len(v) > 500:
                raise bad_request("INVALID_ALERT", f"'{label}' is too long (500 characters at most)")
        elif p["kind"] == "bool":
            v = bool(v)
        params[p["key"]] = v
    for warn, crit, label in (("thresholdPercent", "criticalPercent", "%"), ("threshold", "criticalThreshold", "")):
        if params.get(crit) and params[crit] <= params.get(warn, 0):
            raise bad_request("INVALID_ALERT", f"'Critical at' must be above 'Warning at' ({params[warn]}{label})")

    def minutes(key, default, hi):
        v = body.get(key, (existing or {}).get(key, default))
        try:
            v = int(v)
        except (TypeError, ValueError):
            raise bad_request("INVALID_ALERT", f"{key} must be a whole number of minutes") from None
        if not 0 <= v <= hi:
            raise bad_request("INVALID_ALERT", f"{key} must be between 0 and {hi}")
        return v
    return {"type": rtype, "name": name,
            "enabled": bool(body.get("enabled", (existing or {}).get("enabled", True))),
            "params": params,
            "forMinutes": minutes("forMinutes", spec["forMinutes"], 1440)}


def parse_codes(value: Any, label: str = "Status codes") -> list[str]:
    """'400, 502 503,4xx' → ['400', '502', '503', '4xx'] (codes 100–599, classes 1xx–5xx)."""
    out: list[str] = []
    for t in re.split(r"[\s,;]+", str(value or "").strip().lower()):
        if not t:
            continue
        if not re.fullmatch(r"[1-5](\d\d|xx)", t):
            raise bad_request("INVALID_ALERT", f"'{label}': '{t}' isn't a status code (like 400) or class (like 4xx)")
        if t not in out:
            out.append(t)
    if not out:
        raise bad_request("INVALID_ALERT", f"Give at least one code in '{label}'")
    if len(out) > 20:
        raise bad_request("INVALID_ALERT", f"At most 20 codes in '{label}'")
    return out


def mask_webhook(url: str) -> str:
    return url[:34] + "…" + url[-4:] if len(url) > 40 else "…"


# ------------------------------------------------------------------ the viewer's own server errors
class ServerErrors:
    """5xx answers of every API worker, appended to one file the checker counts."""

    KEEP_MS = 25 * 3600 * 1000

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def record(self, status: int, method: str, path: str) -> None:
        line = f"{now_ms()} {int(status)} {method[:10]} {path[:200].replace(' ', '%20')}\n"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:   # O_APPEND: whole lines from many workers
                fh.write(line)
        except OSError:
            log.debug("could not record a server error", exc_info=True)

    def since(self, start_ms: int) -> list[tuple[int, int, str, str]]:
        out = []
        try:
            with open(self.path, encoding="utf-8") as fh:
                for line in fh:
                    parts = line.split(" ", 3)
                    if len(parts) == 4 and parts[0].isdigit() and int(parts[0]) >= start_ms:
                        out.append((int(parts[0]), int(parts[1]), parts[2], parts[3].strip()))
        except (OSError, ValueError):
            pass
        return out

    def trim(self) -> None:
        """Keep the last day (the checker calls this now and then)."""
        with self._lock:
            try:
                if self.path.stat().st_size < 1_000_000:
                    return
                keep = [f"{t} {s} {m} {p}\n" for t, s, m, p in self.since(now_ms() - self.KEEP_MS)]
                tmp = self.path.with_suffix(f".{os.getpid()}.tmp")
                tmp.write_text("".join(keep), encoding="utf-8")
                os.replace(tmp, self.path)
            except OSError:
                pass


# ------------------------------------------------------------------ Slack
def post_slack(url: str, payload: dict, timeout: float = 10) -> None:
    """POST to a Slack incoming webhook; raises ApiError with Slack's reason on failure."""
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:   # noqa: S310 - URL checked by WEBHOOK_RE
            resp.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:200]
        hints = {"no_service": "the webhook was removed or the URL is wrong",
                 "invalid_token": "the webhook URL is wrong", "channel_is_archived": "the channel is archived",
                 "no_active_hooks": "the Slack app's webhooks are turned off",
                 "action_prohibited": "a Slack admin blocked this webhook"}
        hint = hints.get(body.strip(), "")
        raise ApiError(502, "SLACK_REFUSED", f"Slack refused the message ({e.code} {body.strip()}"
                       + (f": {hint}" if hint else "") + ")") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise ApiError(502, "SLACK_UNREACHABLE", f"Can't reach Slack from the server: {reason}") from None


def _esc(text: Any) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ------------------------------------------------------------------ findings
@dataclass
class Finding:
    subject: str
    bad: bool
    message: str
    value: float | None = None
    link: str | None = None
    cluster: str | None = None       # who may see it in the history (None = admins only)
    extra: dict = field(default_factory=dict)
    level: str | None = None         # warning / critical (None: the rule's level)
    lines: dict | None = None        # two lines {"warning", "critical", "h", "unit"}: the level follows value
    hold: int = 0                    # minutes it must be bad first (on top of the rule's forMinutes)
    muted_by: list = field(default_factory=list)   # ids from the mute file that stopped this alert

    def level_now(self, rule_level: str = "warning") -> str | None:
        """The level without history (for previews); the checker adds hysteresis."""
        if self.lines is not None:
            return level_of(self.value, self.lines) if self.bad else None
        return (self.level or rule_level) if self.bad else None


class AlertService:
    """Rules, the webhook, the checker thread and the history. `logs`, `lb`, `health` and
    `metrics` are the app's services (any may be None: rules that need it report an error)."""

    def __init__(self, settings, store, secrets, logs=None, lb=None, health=None, metrics=None,
                 sender: Callable[[str, dict], None] = post_slack):
        self.settings = settings
        self.store = store
        self.secrets = secrets
        self.logs, self.lb, self.health, self.metrics = logs, lb, health, metrics
        self.sender = sender
        self.root = Path(settings.cache_dir) / "alerts"
        self.errors = ServerErrors(self.root / "server-errors.log")
        self.mutes = MuteFile(getattr(settings, "alert_mutes_file", "") or "", set(TYPES))
        self._mutes_paused = False
        self._stop = threading.Event()
        self._started = time.time()
        self._webhook_version = None
        self.thread: threading.Thread | None = None

    # -- config (rules + settings)
    def config(self) -> dict:
        data, _ = self.store.get_json(CONFIG_KEY)
        if data is not None:
            if int(data.get("version", 1)) < CONFIG_VERSION:
                from .repos import _update_with_retry
                _update_with_retry(self.store, CONFIG_KEY, migrate_config, {"rules": []})
                data, _ = self.store.get_json(CONFIG_KEY)
            return data
        doc = {"version": CONFIG_VERSION, "rules": [],
               "settings": {"consoleUrl": "", "timeZone": "Asia/Kolkata", "paused": False,
                            "thresholds": json.loads(json.dumps(DEFAULT_THRESHOLDS)),
                            "repeat": dict(DEFAULT_REPEAT), "digest": dict(DEFAULT_DIGEST)},
               "slack": {"configured": False, "version": 0}, "createdAt": iso()}
        for rtype, name, params in DEFAULT_RULES:
            doc["rules"].append({"id": new_id()[:10], **validate_rule({"type": rtype, "name": name, "params": params}),
                                 "createdAt": iso(), "updatedBy": "default"})
        try:
            self.store.put_json(CONFIG_KEY, doc, if_none_match=True)
        except Exception:  # noqa: BLE001 - another worker created it first
            data, _ = self.store.get_json(CONFIG_KEY)
            return data or doc
        return doc

    def _update(self, mutate) -> Any:
        from .repos import _update_with_retry
        self.config()
        return _update_with_retry(self.store, CONFIG_KEY, mutate, {"rules": []})

    def add_rule(self, body: dict, actor: str) -> dict:
        rule = {"id": new_id()[:10], **validate_rule(body), "createdAt": iso(), "updatedAt": iso(), "updatedBy": actor}

        def mutate(doc):
            if len(doc["rules"]) >= MAX_RULES:
                raise bad_request("TOO_MANY_ALERTS", f"At most {MAX_RULES} alert rules")
            doc["rules"].append(rule)
            return rule
        return self._update(mutate)

    def update_rule(self, rule_id: str, body: dict, actor: str) -> dict:
        def mutate(doc):
            for i, r in enumerate(doc["rules"]):
                if r["id"] == rule_id:
                    merged = {**r, **{k: v for k, v in body.items() if k in ("name", "enabled", "forMinutes")}}
                    merged["params"] = {**r.get("params", {}), **(body.get("params") or {})}
                    doc["rules"][i] = {**r, **validate_rule(merged, existing=r), "updatedAt": iso(), "updatedBy": actor}
                    return doc["rules"][i]
            raise not_found("ALERT_NOT_FOUND", "No alert rule with that id")
        return self._update(mutate)

    def delete_rule(self, rule_id: str) -> dict:
        def mutate(doc):
            hit = next((r for r in doc["rules"] if r["id"] == rule_id), None)
            if hit is None:
                raise not_found("ALERT_NOT_FOUND", "No alert rule with that id")
            doc["rules"].remove(hit)
            return hit
        return self._update(mutate)

    def update_settings(self, body: dict, actor: str) -> dict:
        s: dict[str, Any] = {}
        if "consoleUrl" in body:
            url = str(body["consoleUrl"] or "").strip().rstrip("/")
            if url and not re.match(r"^https?://[A-Za-z0-9.\-]+(:\d+)?(/[\w.\-/]*)?$", url):
                raise bad_request("INVALID_URL", "The console address must look like https://host")
            s["consoleUrl"] = url.removesuffix("/ui")
        if "timeZone" in body:
            tz = str(body["timeZone"] or "UTC")
            if not ZONE_RE.match(tz) or self._zone(tz) is None and tz != "UTC":
                raise bad_request("INVALID_TIME_ZONE", f"Unknown time zone '{tz}'")
            s["timeZone"] = tz
        if "timeZone2" in body:
            tz = str(body["timeZone2"] or "")
            if tz and (not ZONE_RE.match(tz) or self._zone(tz) is None and tz != "UTC"):
                raise bad_request("INVALID_TIME_ZONE", f"Unknown time zone '{tz}'")
            s["timeZone2"] = tz
        if "paused" in body:
            s["paused"] = bool(body["paused"])
        if "mutesPaused" in body:
            s["mutesPaused"] = bool(body["mutesPaused"])
        if body.get("thresholds") is not None:
            s["thresholds"] = _thresholds(body["thresholds"])
        if body.get("repeat") is not None:
            r = body["repeat"]
            if not isinstance(r, dict):
                raise bad_request("INVALID_SETTINGS", "repeat must be an object")
            s["repeat"] = {}
            for lvl in LEVELS:
                if lvl in r:
                    v = _int(r[lvl], f"Repeat {lvl}s every", 0, 1440)
                    s["repeat"][lvl] = v
        if body.get("digest") is not None:
            d = body["digest"]
            if not isinstance(d, dict):
                raise bad_request("INVALID_SETTINGS", "digest must be an object")
            s["digest"] = {}
            if "enabled" in d:
                s["digest"]["enabled"] = bool(d["enabled"])
            if "day" in d:
                if d["day"] not in DAYS:
                    raise bad_request("INVALID_SETTINGS", "The summary day must be one of " + ", ".join(DAYS))
                s["digest"]["day"] = d["day"]
            if "time" in d:
                if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(d["time"])):
                    raise bad_request("INVALID_SETTINGS", "The summary time must look like 15:00")
                s["digest"]["time"] = d["time"]

        def mutate(doc):
            cur = doc.setdefault("settings", {})
            for k in ("thresholds", "repeat", "digest"):
                if k in s:
                    merged = {**(cur.get(k) or {}), **s.pop(k)}
                    cur[k] = merged
            cur.update(s, updatedAt=iso(), updatedBy=actor)
            return settings_of(doc)
        return self._update(mutate)

    # -- snoozes ("ruleId|subject" → {until: ms or None = until it clears, by, at})
    def snoozes(self) -> dict:
        data, _ = self.store.get_json(SNOOZE_KEY)
        return (data or {}).get("items", {})

    def _update_snoozes(self, mutate) -> Any:
        from .repos import _update_with_retry

        def wrap(doc):
            return mutate(doc.setdefault("items", {}))
        return _update_with_retry(self.store, SNOOZE_KEY, wrap, {"items": {}})

    def snooze(self, rule_id: str, subject: str, minutes: int | None, actor: str) -> dict:
        """Post nothing about this subject for `minutes` (None: until it clears)."""
        rule = next((r for r in self.config().get("rules", []) if r["id"] == rule_id), None)
        if rule is None:
            raise not_found("ALERT_NOT_FOUND", "No alert rule with that id")
        if not subject or len(subject) > 300:
            raise bad_request("INVALID_SNOOZE", "Give the subject to snooze")
        if minutes is not None and not 1 <= int(minutes) <= 7 * 1440:
            raise bad_request("INVALID_SNOOZE", "Snooze for 1 minute to 7 days, or until it clears")
        now = now_ms()
        item = {"ruleId": rule_id, "ruleName": rule["name"], "subject": subject,
                "until": now + int(minutes) * MINUTE if minutes is not None else None, "by": actor, "at": now}

        def mutate(items):
            items[f"{rule_id}|{subject}"] = item
            return item
        return self._update_snoozes(mutate)

    def unsnooze(self, rule_id: str, subject: str) -> bool:
        def mutate(items):
            return items.pop(f"{rule_id}|{subject}", None) is not None
        return self._update_snoozes(mutate)

    # -- the webhook (Secrets Manager)
    def set_webhook(self, url: str, actor: str) -> dict:
        url = (url or "").strip()
        if not WEBHOOK_RE.match(url):
            raise bad_request("INVALID_WEBHOOK", "Paste a Slack incoming webhook URL "
                              "(https://hooks.slack.com/services/…)")
        self.secrets.put(SECRET, {"slackWebhookUrl": url}, "Vector Logs viewer: Slack webhook for alerts")

        def mutate(doc):
            slack = doc.setdefault("slack", {})
            slack.update(configured=True, hint=mask_webhook(url), updatedAt=iso(), updatedBy=actor,
                         version=int(slack.get("version", 0)) + 1)
            return slack
        return self._update(mutate)

    def clear_webhook(self, actor: str) -> dict:
        self.secrets.put(SECRET, {"slackWebhookUrl": ""}, "Vector Logs viewer: Slack webhook for alerts")

        def mutate(doc):
            slack = doc.setdefault("slack", {})
            slack.update(configured=False, hint=None, updatedAt=iso(), updatedBy=actor,
                         version=int(slack.get("version", 0)) + 1)
            return slack
        return self._update(mutate)

    def webhook(self, cfg: dict | None = None) -> str | None:
        cfg = cfg or self.config()
        version = (cfg.get("slack") or {}).get("version", 0)
        if version != self._webhook_version and hasattr(self.secrets, "forget"):
            self.secrets.forget(SECRET)      # changed in another worker: don't trust the cache
        self._webhook_version = version
        if not (cfg.get("slack") or {}).get("configured"):
            return None
        return ((self.secrets.get(SECRET) or {}).get("slackWebhookUrl") or None)

    def send_test(self, url: str | None, actor: str) -> dict:
        url = (url or "").strip() or self.webhook()
        if not url:
            raise bad_request("NO_WEBHOOK", "Save a Slack webhook URL first, or paste one to test")
        if not WEBHOOK_RE.match(url):
            raise bad_request("INVALID_WEBHOOK", "That isn't a Slack incoming webhook URL")
        cfg = self.config()
        text = f":wave: Test message from the Vector Logs viewer, sent by {actor}. Alerts will arrive here."
        self.sender(url, {"text": text, "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": _esc(text)}},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": self._footer(cfg, now_ms(), None)}]}]})
        return {"sent": True}

    # -- time
    def _zone(self, tz: str):
        from .logs import _zone
        return None if tz == "UTC" else _zone(tz)

    def _zones(self, cfg: dict) -> list[str]:
        """The zones Slack times are shown in: the main one, and the second one if set (IST and PST)."""
        st = settings_of(cfg)
        first = st.get("timeZone") or "UTC"
        second = st.get("timeZone2") or ""
        return [first] + ([second] if second and second != first else [])

    def _at(self, tz: str, ms: int) -> datetime:
        zone = self._zone(tz)
        d = datetime.fromtimestamp(ms / 1000, timezone.utc)
        return d.astimezone(zone) if zone else d

    @staticmethod
    def _abbr(d: datetime) -> str:
        return d.strftime("%Z") or "UTC"

    def _fmt(self, cfg: dict, ms: int, date: bool = False) -> str:
        """22 Oct 09:05 IST / 21 Oct 20:35 PDT (date) or 09:05 IST / 20:35 PDT; one zone: as before."""
        zones = self._zones(cfg)
        parts, first_day = [], None
        for i, tz in enumerate(zones):
            d = self._at(tz, ms)
            if date:
                day = "" if i and d.date() == first_day else d.strftime("%d %b ")
                parts.append(f"{day}{d:%H:%M} {self._abbr(d)}")
            else:
                parts.append(f"{d:%H:%M}" + (f" {self._abbr(d)}" if len(zones) > 1 else ""))
            first_day = first_day or d.date()
        return " / ".join(parts)

    def _span(self, cfg: dict, start: int, end: int) -> str:
        """04:40–04:55 IST / 16:10–16:25 PDT (with the day when it isn't today there)."""
        parts = []
        for tz in self._zones(cfg):
            a, b = self._at(tz, start), self._at(tz, end)
            today = datetime.now(b.tzinfo).date()
            day = "" if b.date() == today else b.strftime("%d %b ")
            parts.append(f"{day}{a:%H:%M}–{b:%H:%M} {self._abbr(b)}")
        return " / ".join(parts)

    # -- links into the console
    @staticmethod
    def _q(**kw) -> str:
        from urllib.parse import urlencode
        return urlencode({k: v for k, v in kw.items() if v not in (None, "")})

    # ================================================================ checks
    def evaluate(self, rule: dict, now: int | None = None, memo: dict | None = None,
                 firing: set[str] | frozenset = frozenset()) -> list[Finding]:
        """What the rule finds now, with the mute file applied. `memo` is the rule's memory between
        checks (instances seen); `firing` the subjects alerting now (so a new mute stops them)."""
        now = now or now_ms()
        fn = getattr(self, f"_check_{rule['type']}", None)
        spec = TYPES.get(rule["type"])
        if fn is None or spec is None:
            raise bad_request("UNKNOWN_ALERT_TYPE", f"Unknown alert type '{rule['type']}'")
        params = {**_defaults(spec), **(rule.get("params") or {})}
        self._mutes_paused = bool(settings_of(self.config()).get("mutesPaused"))
        self._rtype, self._firing = rule["type"], firing
        findings = fn(params, now, memo if memo is not None else {}) if spec.get("memo") else fn(params, now)
        self._mute_subjects(rule["type"], params, findings, firing)
        return findings

    # ================================================================ muted alerts (config/alert-mutes.yaml)
    def _mutes_for(self, rtype: str, kind: str | None = None) -> list:
        if self._mutes_paused:
            return []
        return [m for m in self.mutes.get() if m.enabled and m.for_rule(rtype) and (kind is None or m.kind == kind)]

    @staticmethod
    def _rule_codes(rtype: str, p: dict) -> list[str]:
        if rtype == "lb_errors":
            return [p.get("statusClass", "5xx")]
        if rtype == "lb_status":
            return parse_codes(p.get("codes", "400"))
        return []

    def _mute(self, f: Finding, ids: list[str], level: str | None = None) -> None:
        """Stop this finding from alerting; the checker records it as muted."""
        spec = TYPES.get(getattr(self, "_rtype", ""), {})
        base = spec.get("level") if spec.get("level") in LEVELS else "warning"
        f.extra["mutedLevel"] = level or f.level_now(base) or base
        f.muted_by = sorted(set(f.muted_by) | set(ids))
        f.bad, f.lines = False, None

    def _mute_subjects(self, rtype: str, p: dict, findings: list[Finding], firing) -> None:
        """Entries that name target groups / clusters / subjects only: that alert isn't posted."""
        ms = self._mutes_for(rtype, "subject")
        if not ms:
            return
        codes = self._rule_codes(rtype, p)
        for f in findings:
            if f.muted_by or not (f.bad or f.subject in firing):
                continue
            hit = [m.id for m in ms if m.subject_ok(rtype, f.subject, f.cluster) and m.codes_ok(codes)]
            if hit:
                self._mute(f, hit)

    def _lb_path_mutes(self, rtype: str, rule_codes: list[str], start: int, end: int,
                       tgs: list[str]) -> tuple[dict, dict, list]:
        """Requests on muted paths, per target group: ({tg: (matching requests, all requests)},
        {tg: mute ids}, the per-path rows for later)."""
        ms = [m for m in self._mutes_for(rtype, "path") if m.codes_ok(rule_codes)]
        if not ms or not tgs:
            return {}, {}, []
        from .lb_logs import LbQuery
        try:
            rows = self._need("lb").path_code_counts(LbQuery(start=start, end=end, target_groups=tgs))
        except Exception as e:  # noqa: BLE001 - without the breakdown nothing is taken out (it is posted)
            log.warning("alert mutes: can't read the paths of %s: %s", ", ".join(tgs[:5]), _err(e))
            return {}, {}, []
        removed: dict[str, list[int]] = {}
        ids: dict[str, set[str]] = {}
        for tg, method, pg, code, n in rows:
            for m in ms:
                if not m.tg_ok(tg) or not m.path_ok(method, pg) or (m.codes and (code is None or not code_in(code, m.codes))):
                    continue
                r = removed.setdefault(tg, [0, 0])
                r[1] += n
                if code is not None and code_in(code, rule_codes):
                    r[0] += n
                ids.setdefault(tg, set()).add(m.id)
                break
        return {k: tuple(v) for k, v in removed.items()}, {k: sorted(v) for k, v in ids.items()}, rows

    def _lb_sig_mutes(self, rtype: str, rule_codes: list[str], tg: str, cluster: str | None, start: int, end: int,
                      rows: list | None = None) -> list[str] | None:
        """Mute ids when every ERROR line the service wrote in these minutes is a muted error (and the
        failing requests are on the paths those entries name). None = post it."""
        ms = [m for m in self._mutes_for(rtype, "signature") if m.tg_ok(tg) and m.codes_ok(rule_codes)]
        if not ms or self.logs is None:
            return None
        if any(m.paths for m in ms):
            from .lb_logs import LbQuery
            if rows is None:
                rows = self._need("lb").path_code_counts(LbQuery(start=start, end=end, target_groups=[tg]))
            path_ms = [m for m in self._mutes_for(rtype, "path") if m.codes_ok(rule_codes) and m.tg_ok(tg)]
            failing = {(meth, pg) for t, meth, pg, code, n in rows
                       if t == tg and n and code is not None and code_in(code, rule_codes)
                       and not any(m.path_ok(meth, pg) and (not m.codes or code_in(code, m.codes)) for m in path_ms)}
            if not all(any(not m.paths or m.path_ok(meth, pg) for m in ms) for meth, pg in failing):
                return None                  # requests failing on a path no entry names
        cluster = cluster or (tg[3:] if tg.startswith("tg-") else tg)
        return self._only_muted_errors(cluster, ms, start - 2 * MINUTE, end + 2 * MINUTE)

    def _only_muted_errors(self, cluster: str, ms: list, start: int, end: int) -> list[str] | None:
        """The ids of the entries whose errors the service wrote, when it wrote no other ERROR line."""
        from .logs import SearchSpec
        if cluster not in self.logs.clusters():
            return None                      # no application logs: can't tell which error it was
        sigs = list(dict.fromkeys(s for m in ms for s in m.signatures))
        total = self.logs.search(cluster, SearchSpec(start=start, end=end, query="level:error"), size=1,
                                 with_aggs=False)["total"]
        if not total:
            return None                      # no error lines at all: not one of the known errors
        rest = self.logs.search(cluster, SearchSpec(start=start, end=end, query="level:error " + " ".join(
            f'-"{x}"' for x in sigs)), size=1, with_aggs=False)["total"]
        if rest:
            return None
        hit = []
        for m in ms[:10]:
            for sig in m.signatures:
                if self.logs.search(cluster, SearchSpec(start=start, end=end, query=f'level:error "{sig}"'), size=1,
                                    with_aggs=False)["total"]:
                    hit.append(m.id)
                    break
        return hit or [m.id for m in ms]

    def _need(self, name: str):
        svc = getattr(self, name)
        if svc is None:
            raise ApiError(500, "NOT_AVAILABLE", f"This server doesn't run the {name} service")
        return svc

    def _converter_status(self) -> dict:
        lb = self._need("lb")
        return lb.store.read_status() or {}

    # -- the viewer
    def _check_app_aws(self, p: dict, now: int) -> list[Finding]:
        out = []
        if self.logs is not None:
            try:
                self.logs.source.list_clusters()
                out.append(Finding("Application logs in S3", False, "readable"))
            except Exception as e:  # noqa: BLE001
                out.append(Finding("Application logs in S3", True, _err(e)))
        if self.health is not None and self.settings.health_enabled:
            try:
                self.health.snapshot()
                out.append(Finding("ECS and load balancer APIs", False, "readable"))
            except Exception as e:  # noqa: BLE001
                out.append(Finding("ECS and load balancer APIs", True, _err(e)))
            if self.metrics is not None:
                try:
                    self.metrics.clusters(3)
                    out.append(Finding("CloudWatch metrics", False, "readable"))
                except Exception as e:  # noqa: BLE001
                    out.append(Finding("CloudWatch metrics", True, _err(e)))
        if self.lb is not None and self.settings.lb_enabled:
            err = (self._converter_status().get("lastError") or {})
            access = str(err.get("code", "")).startswith(("LB_LOGS_", "LB_PARQUET_"))
            out.append(Finding("Load balancer logs in S3", access,
                               f"{err.get('code')}: {err.get('message')}" if access else "readable"))
        return out

    def _check_app_converter(self, p: dict, now: int) -> list[Finding]:
        if self.lb is None or not self.settings.lb_enabled:
            return []
        st = self._converter_status()
        subject = "Load balancer log converter"
        updated = _parse_iso(st.get("updatedAt"))
        if updated is None:
            if time.time() - self._started < 15 * 60:
                return []            # just started: give it time
            return [Finding(subject, True, "has never run on this server (LB_ENABLED and LB_BACKGROUND?)")]
        age = (now - updated) / MINUTE
        if age >= p["stalledMinutes"]:
            return [Finding(subject, True, f"hasn't updated for {age:.0f} min (last {self._fmt(self.config(), updated, True)})",
                            value=round(age))]
        err = st.get("lastError")
        if err:
            f = Finding(subject, True, f"{err.get('code')}: {err.get('message')}")
            hit = [m.id for m in self._mutes_for("app_converter", "converter")
                   if any(x.lower() in f.message.lower() for x in m.converter_errors)]
            if hit:
                self._mute(f, hit)
            return [f]
        return [Finding(subject, False, "running")]

    def _check_app_disk(self, p: dict, now: int) -> list[Finding]:
        path = Path(self.settings.cache_dir)
        path.mkdir(parents=True, exist_ok=True)
        u = shutil.disk_usage(path)
        pct = u.used / u.total * 100 if u.total else 0
        lines = lines_for(self.config(), "disk")
        return [Finding("Server disk", pct >= lines["warning"],
                        f"{pct:.0f}% used on the viewer's server ({u.free / 2**30:.1f} GB free of "
                        f"{u.total / 2**30:.0f} GB)", value=round(pct, 1), lines=lines)]

    def _check_app_errors(self, p: dict, now: int) -> list[Finding]:
        rows = [r for r in self.errors.since(now - p["minutes"] * MINUTE) if r[1] != 507]
        top: dict[str, int] = {}
        for _, status, method, path in rows:
            short = re.sub(r"/[0-9a-f-]{8,}|/\d+", "/…", path.split("?")[0])
            k = f"{status} {method} {short}"
            top[k] = top.get(k, 0) + 1
        most = ", ".join(f"{k} ×{n}" for k, n in sorted(top.items(), key=lambda kv: -kv[1])[:3])
        self.errors.trim()
        return [Finding("Viewer API", len(rows) >= p["threshold"],
                        f"{len(rows)} server errors in {p['minutes']} min" + (f" (most: {most})" if most else ""),
                        value=len(rows))]

    # -- application logs
    def _log_clusters(self, wanted: list[str]) -> list[str]:
        logs = self._need("logs")
        have = logs.clusters(refresh=True)
        return [c for c in have if c in wanted] if wanted else have

    def _newest_log_file(self, cluster: str, now: int, minutes: int) -> int | None:
        """Time of the newest file Vector wrote for the cluster (from its <epoch>-<uuid>.parquet
        name), looking back over the hours the rule covers plus one."""
        logs = self._need("logs")
        hour = datetime.fromtimestamp(now / 1000, timezone.utc).replace(minute=0, second=0, microsecond=0)
        for i in range(int(math.ceil(minutes / 60)) + 2):
            h = hour - timedelta(hours=i)
            keys = [k for k, _ in logs.source.list_prefix(logs._hour_prefix(cluster, h))]
            if not keys:
                continue
            stamps = [int(m.group(1)) * 1000 for k in keys
                      if (m := re.match(r"^(\d{9,11})[-_.]", k.rsplit("/", 1)[-1]))]
            # files without an epoch in the name: all we know is the hour they belong to
            return max(stamps) if stamps else int((h + timedelta(hours=1)).timestamp() * 1000) - 1
        return None

    def _check_logs_stale(self, p: dict, now: int) -> list[Finding]:
        out = []
        cfg = self.config()
        for c in self._log_clusters(p["clusters"]):
            newest = self._newest_log_file(c, now, p["minutes"])
            link = f"/ui/logs/{c}"
            if newest is None:
                out.append(Finding(c, True, f"no log files for more than {p['minutes']} min", link=link, cluster=c))
                continue
            age = (now - newest) / MINUTE
            out.append(Finding(c, age >= p["minutes"],
                               f"no new log files for {age:.0f} min (last {self._fmt(cfg, newest, True)})"
                               if age >= p["minutes"] else f"newest file {age:.0f} min ago",
                               value=round(age), link=link, cluster=c))
        return out

    def _check_log_errors(self, p: dict, now: int) -> list[Finding]:
        from .logs import SearchSpec
        logs = self._need("logs")
        start, out = now - p["minutes"] * MINUTE, []
        for c in self._log_clusters(p["clusters"]):
            res = logs.search(c, SearchSpec(start=start, end=now, query=p["query"]), size=1, with_aggs=False)
            n0 = n = res["total"]
            ms = [m for m in self._mutes_for("log_errors", "signature") if m.cluster_ok(c)]
            if n and ms:                     # the muted errors don't count
                sigs = list(dict.fromkeys(x for m in ms for x in m.signatures))
                res = logs.search(c, SearchSpec(start=start, end=now, query=(p["query"] + " " + " ".join(
                    f'-"{x}"' for x in sigs)).strip()), size=1, with_aggs=False)
                n = res["total"]
            msg = f"{n:,} lines matching {p['query'] or 'anything'} in {p['minutes']} min"
            if n0 != n:
                msg += f" ({n0 - n:,} muted lines not counted)"
            if n and p.get("includeSample") and res["hits"]:
                h = res["hits"][0]
                sample = str(h.get("msg") or h.get("body") or h.get("raw") or "").replace("\n", " ")[:200]
                if sample:
                    msg += f"\nnewest: {('[' + str(h['service']) + '] ') if h.get('service') else ''}{sample}"
            link = f"/ui/logs/{c}?" + self._q(q=p["query"], start=iso_ms(start), end=iso_ms(now))
            f = Finding(c, n >= p["threshold"], msg, value=n, link=link, cluster=c)
            if n0 >= p["threshold"] > n:
                self._mute(f, [m.id for m in ms])
            out.append(f)
        return out

    # -- load balancers
    def _check_lb_logs_stale(self, p: dict, now: int) -> list[Finding]:
        st = self._converter_status()
        latest = {lb: _alb_end(t) for lb, t in (st.get("latestFile") or {}).items()}
        if not latest:
            if not st:
                raise ApiError(500, "NO_CONVERTER_STATUS", "The load balancer log converter hasn't reported yet")
            return []
        names = [n for n in latest if not p["lbs"] or n in p["lbs"]]
        names += [n for n in p["lbs"] if n not in latest]
        cfg = self.config()
        ages = {n: ((now - latest[n]) / MINUTE if latest.get(n) else None) for n in names}

        def text(n):
            a = ages[n]
            return "no files in the last 30 days" if a is None else \
                f"no new files for {a:.0f} min (last {self._fmt(cfg, latest[n], True)})"
        if p["mode"] == "all":
            known = [a for a in ages.values() if a is not None]
            newest = min(known) if known else None
            bad = newest is None or newest >= p["minutes"]
            label = "All load balancers" if not p["lbs"] else ", ".join(sorted(names)[:5]) + ("…" if len(names) > 5 else "")
            return [Finding(label, bad, (f"none of {len(names)} load balancers delivered a file for {newest:.0f} min"
                                         if newest is not None else "no files at all") if bad else
                            f"newest file {newest:.0f} min ago", value=None if newest is None else round(newest),
                            link="/ui/lb")]
        return [Finding(n, ages[n] is None or ages[n] >= p["minutes"],
                        text(n) if ages[n] is None or ages[n] >= p["minutes"] else f"newest file {ages[n]:.0f} min ago",
                        value=None if ages[n] is None else round(ages[n]), link="/ui/lb?" + self._q(lb=n))
                for n in sorted(names)]

    def _lb_rows(self, p: dict, now: int) -> tuple[int, int, list[dict]]:
        from .lb_logs import LbQuery
        lb = self._need("lb")
        end = now - LB_LAG_MINUTES * MINUTE
        start = end - p["minutes"] * MINUTE
        res = lb.summary(LbQuery(start=start, end=end, target_groups=p["targetGroups"]), None)
        return start, end, [r for r in res.get("targetGroups", []) if r.get("tg")]

    def _tg_rates(self, start: int, end: int, tgs: list[str], classes: list[str], codes: list[int]) -> dict[str, tuple]:
        """{tg: (requests with these codes, all requests, cluster)} for the window."""
        from .lb_logs import LbQuery
        lb = self._need("lb")
        totals = {r["tg"]: r for r in lb.summary(LbQuery(start=start, end=end, target_groups=tgs), None)
                  .get("targetGroups", []) if r.get("tg")}
        matched: dict[str, int] = {}
        for q in ([LbQuery(start=start, end=end, target_groups=tgs, status_classes=classes)] if classes else []) + \
                 ([LbQuery(start=start, end=end, target_groups=tgs, status_codes=codes)] if codes else []):
            for r in lb.summary(q, None).get("targetGroups", []):
                if r.get("tg"):
                    matched[r["tg"]] = matched.get(r["tg"], 0) + r["requests"]
        return {tg: (matched.get(tg, 0), r["requests"], r.get("cluster")) for tg, r in totals.items()}

    def _usual(self, start: int, tgs: list[str], classes: list[str], codes: list[int]) -> dict[str, float | None]:
        """The same share over the 24 hours before the window, per target group (None: no traffic)."""
        day = self._tg_rates(start - 24 * 3600_000, start, tgs, classes, codes)
        return {tg: (n / total * 100 if total else None) for tg, (n, total, _) in day.items()}

    @staticmethod
    def _unusual(pct: float, usual: float | None, times: float) -> bool:
        return not times or usual is None or (usual == 0 and pct > 0) or pct >= usual * times

    @staticmethod
    def _pct(x: float | None) -> str:
        if x is None:
            return "–"
        return f"{x:.0f}%" if x >= 10 else f"{x:.1f}%" if x >= 0.1 or x == 0 else f"{x:.2f}%"

    def _lb_finish(self, rtype: str, rule_codes: list[str], start: int, end: int, raw: dict, cand: list[str],
                   judge) -> dict:
        """Apply the mute file to load balancer numbers. raw = {tg: (n, total, cluster)}; judge(tg, n, total)
        → (watch, bad). Returns {tg: (n, total, watch, bad, muted ids, muted level, requests taken out)}."""
        removed, path_ids, rows = self._lb_path_mutes(rtype, rule_codes, start, end, cand)
        out = {}
        for tg, (n, total, cluster) in raw.items():
            dn, dt = removed.get(tg, (0, 0))
            n2, t2 = max(0, n - dn), max(0, total - dt)
            watch0, bad0 = judge(tg, n, total)
            watch, bad = judge(tg, n2, t2) if (dn or dt) else (watch0, bad0)
            ids: list[str] = []
            if bad0 and not bad and tg in path_ids:
                ids = path_ids[tg]
            elif bad or (watch and tg in self._firing):
                try:
                    ids = self._lb_sig_mutes(rtype, rule_codes, tg, cluster, start, end, rows or None) or []
                except Exception as e:  # noqa: BLE001 - can't tell: post it
                    log.warning("alert mutes: can't check the errors of %s: %s", tg, _err(e))
            out[tg] = (n2, t2, watch, bad, ids, bad0 or bad, dn)
        return out

    def _check_lb_errors(self, p: dict, now: int) -> list[Finding]:
        start, end, rows = self._lb_rows(p, now)
        cfg, cls, out = self.config(), p["statusClass"], []
        span = self._span(cfg, start, end)
        lines = _lb_lines(p["thresholdPercent"], p.get("criticalPercent"), pct=True)
        floor = lines["warning"] - lines["h"]          # firing ones stay until they are below this
        raw = {r["tg"]: ((r["s5xx"] if cls == "5xx" else r["s4xx"]), r["requests"], r.get("cluster")) for r in rows}
        cand = [tg for tg, (n, t, _) in raw.items() if t >= p["minRequests"] and n / t * 100 > floor]
        usual = self._usual(start, cand, [cls], []) if cand else {}

        def judge(tg, n, t):
            pct = n / t * 100 if t else 0
            watch = tg in cand and t >= p["minRequests"] and pct > floor and \
                self._unusual(pct, usual.get(tg), p.get("vsUsual", 0))
            return watch, watch and pct >= lines["warning"]
        done = self._lb_finish("lb_errors", [cls], start, end, raw, cand, judge)
        for tg, (n, total, watch, bad, ids, was_bad, taken) in done.items():
            pct = n / total * 100 if total else 0
            u = usual.get(tg)
            msg = f"{self._pct(pct)} {cls} · {n:,} of {total:,} requests"
            if tg in usual:
                msg += f" · usual {self._pct(u)}"
            if taken:
                msg += f" · {taken:,} on muted paths not counted"
            f = Finding(tg, bad, msg, value=round(pct, 2), lines=lines if watch else None, cluster=raw[tg][2],
                        link="/ui/lb?" + self._q(tg=tg, st=cls, start=iso_ms(start), end=iso_ms(end)),
                        extra={"span": span, "lb": {"start": start, "end": end, "classes": [cls], "codes": [],
                                                    "pct": pct, "usual": u, "logs": p.get("includeLogs", True)}})
            if ids:
                n0, t0, _ = raw[tg]
                self._mute(f, ids, level_of(n0 / t0 * 100 if t0 else 0, lines) or "warning")
            out.append(f)
        return out

    def _check_lb_status(self, p: dict, now: int) -> list[Finding]:
        codes = parse_codes(p["codes"])
        classes = [c for c in codes if c.endswith("xx")]
        singles = [int(c) for c in codes if not c.endswith("xx") and f"{c[0]}xx" not in classes]
        end = now - LB_LAG_MINUTES * MINUTE
        start = end - p["minutes"] * MINUTE
        rates = self._tg_rates(start, end, p["targetGroups"], classes, singles)
        cfg, out = self.config(), []
        span = self._span(cfg, start, end)
        label = ", ".join(codes)
        link_code = {"code": codes[0]} if len(codes) == 1 and not classes else \
            {"st": codes[0]} if codes == classes and len(classes) == 1 else {}
        lines = _lb_lines(p["threshold"], p.get("criticalThreshold"), pct=False)
        floor = lines["warning"] - lines["h"]
        cand = [tg for tg, (n, total, _) in rates.items()
                if n > floor and (n / total * 100 if total else 0) >= p["thresholdPercent"]]
        usual = self._usual(start, cand, classes, singles) if cand else {}

        def judge(tg, n, t):
            pct = n / t * 100 if t else 0
            watch = tg in cand and n > floor and pct >= p["thresholdPercent"] and \
                self._unusual(pct, usual.get(tg), p.get("vsUsual", 0))
            return watch, watch and n >= lines["warning"]
        done = self._lb_finish("lb_status", codes, start, end, rates, cand, judge)
        for tg, (n, total, watch, bad, ids, was_bad, taken) in done.items():
            pct, u = (n / total * 100 if total else 0), usual.get(tg)
            msg = f"{n:,} × {label} · {self._pct(pct)} of {total:,} requests"
            if tg in usual:
                msg += f" · usual {self._pct(u)}"
            if taken:
                msg += f" · {taken:,} on muted paths not counted"
            f = Finding(tg, bad, msg, value=n, lines=lines if watch else None, cluster=rates[tg][2],
                        link="/ui/lb?" + self._q(tg=tg, start=iso_ms(start), end=iso_ms(end), **link_code),
                        extra={"span": span, "lb": {"start": start, "end": end, "classes": classes,
                                                    "codes": singles, "pct": pct, "usual": u,
                                                    "logs": p.get("includeLogs", True)}})
            if ids:
                self._mute(f, ids, level_of(rates[tg][0], lines) or "warning")
            out.append(f)
        return out

    def _check_lb_latency(self, p: dict, now: int) -> list[Finding]:
        start, end, rows = self._lb_rows(p, now)
        cfg, out = self.config(), []
        span = self._span(cfg, start, end)
        for r in rows:
            if r.get("p95") is None:
                continue
            ms = r["p95"] * 1000
            bad = r["requests"] >= p["minRequests"] and ms >= p["p95Ms"]
            out.append(Finding(r["tg"], bad, f"p95 {ms:,.0f} ms over {r['requests']:,} requests ({span})",
                               value=round(ms), cluster=r.get("cluster"),
                               link="/ui/lb?" + self._q(tg=r["tg"], start=iso_ms(start), end=iso_ms(end))))
        return out

    # -- ECS
    def _ecs_clusters(self, wanted: list[str]) -> list[dict]:
        snap = self._need("health").snapshot()
        return [c for c in snap["clusters"] if not wanted or c["name"] in wanted]

    def _check_ecs_targets(self, p: dict, now: int) -> list[Finding]:
        from .ecs_health import BAD_STATES
        from .mutes import _glob
        out = []
        all_ms = self._mutes_for("ecs_targets", "health")
        for c in self._ecs_clusters(p["clusters"]):
            if not c["targetGroups"]:
                continue
            ms = [m for m in all_ms if m.cluster_ok(c["name"])]
            pats = [(m.id, _glob(r)) for m in ms for r in m.health_reasons]

            def muted_by(t):
                return [mid for mid, rx in pats if rx.match(t.get("reason") or t["state"] or "")]

            def judge(skip: bool):
                bad_n, parts, empty, ids = 0, [], [], set()
                for tg in c["targetGroups"]:
                    bad_t = []
                    for t in tg["targets"]:
                        if t["state"] not in BAD_STATES:
                            continue
                        hit = muted_by(t) if skip else []
                        if hit:
                            ids.update(hit)
                        else:
                            bad_t.append(t)
                    bad_n += len(bad_t)
                    if tg["total"] and not tg["counts"].get("healthy") and (bad_t or not tg["counts"].get("bad")):
                        empty.append(tg["name"])
                    if bad_t:
                        reasons = sorted({t.get("reason") or t["state"] for t in bad_t})
                        parts.append(f"{tg['name']}: {len(bad_t)} of {tg['total']} not healthy"
                                     + (f" ({', '.join(reasons[:3])})" if reasons else ""))
                return bad_n, parts, empty, sorted(ids)
            bad0, _, empty0, _ = judge(False)
            bad_n, parts, empty, ids = judge(True) if pats else (bad0, *judge(False)[1:3], [])
            msg = "; ".join(parts) if parts else f"{c['targets']['healthy']} of {c['targets']['total']} targets healthy"
            if empty:
                msg = f"no healthy target left in {', '.join(empty[:3])} · " + msg
            level = "critical" if empty else "warning"
            f = Finding(c["name"], bool(empty) or bad_n >= p["minUnhealthy"], msg, value=bad_n, cluster=c["name"],
                        level=level, link="/ui/health?" + self._q(q=c["name"]))
            was_bad = bool(empty0) or bad0 >= p["minUnhealthy"]
            if was_bad and not f.bad and ids:
                self._mute(f, ids, "critical" if empty0 else "warning")
            out.append(f)
        return out

    def _check_ecs_tasks(self, p: dict, now: int) -> list[Finding]:
        out = []
        for c in self._ecs_clusters(p["clusters"]):
            for s in c["services"]:
                if (s.get("status") or "ACTIVE") != "ACTIVE" or not s["desired"]:
                    continue
                bad = s["running"] < s["desired"]
                down = s["running"] == 0
                deploying = bad and not down and s.get("rollout") == "IN_PROGRESS"
                msg = ("service down: " if down else "") + f"running {s['running']} of {s['desired']} tasks"
                if s.get("pending"):
                    msg += f" ({s['pending']} starting)"
                if bad and s.get("rollout") and s["rollout"] != "COMPLETED":
                    msg += f" · deployment {s['rollout'].lower().replace('_', ' ')}"
                if bad and s.get("lastEvent") and s["lastEvent"].get("message"):
                    msg += f"\nlast event: {str(s['lastEvent']['message'])[:200]}"
                out.append(Finding(f"{c['name']} / {s['name']}", bad, msg, value=s["running"], cluster=c["name"],
                                   level="critical" if down else "warning",
                                   hold=DEPLOY_GRACE_MINUTES if deploying else 0,
                                   link="/ui/health?" + self._q(q=c["name"])))
        return out

    def _check_ecs_instances(self, p: dict, now: int, memo: dict) -> list[Finding]:
        """Instances that lost their agent or aren't ACTIVE, and instances that were in the cluster at
        the last check and are gone while services are short of tasks (or no instance is left)."""
        seen = memo.setdefault("seen", {})
        gone = memo.setdefault("gone", {})
        out = []
        for c in self._ecs_clusters(p["clusters"]):
            name = c["name"]
            cur = {i["id"]: i for i in c["instances"] if i.get("id")}
            short = [s for s in c["services"] if (s.get("status") or "ACTIVE") == "ACTIVE"
                     and s["desired"] and s["running"] < s["desired"]]
            below = ", ".join(f"{s['name']} {s['running']}/{s['desired']} tasks" for s in short[:3])
            link = "/ui/health?" + self._q(q=name)
            for iid, info in (seen.get(name) or {}).items():
                if iid not in cur and iid not in gone:
                    gone[iid] = {**info, "cluster": name, "at": now}
            seen[name] = {iid: {"ip": i.get("ip"), "name": i.get("name")} for iid, i in cur.items()}

            def who(iid, info):
                bits = [x for x in (info.get("ip"), info.get("name")) if x]
                return f"{iid} ({', '.join(bits)})" if bits else iid
            for iid, i in sorted(cur.items()):
                gone.pop(iid, None)                      # it came back
                status = str(i.get("status") or "ACTIVE").upper()
                problem = ("ECS agent disconnected" if i.get("agentConnected") is False and status == "ACTIVE"
                           else f"status {status}" if status not in ("ACTIVE", "DRAINING") else None)
                msg = f"{who(iid, i)}: {problem}" if problem else f"{who(iid, i)}: {status.lower()}"
                if problem and below:
                    msg += f" · services short: {below}"
                out.append(Finding(f"{name} / {iid}", problem is not None, msg, cluster=name, level="critical",
                                   link=link))
            for iid, g in sorted(gone.items()):
                if g["cluster"] != name:
                    continue
                if (short or not cur) and now - g["at"] < 24 * 3600_000:
                    msg = f"{who(iid, g)} left the cluster at {self._fmt(self.config(), g['at'])}"
                    msg += f" · services short: {below}" if below else " · no instance left in the cluster"
                    out.append(Finding(f"{name} / {iid}", True, msg, cluster=name, level="critical", link=link))
                else:
                    gone.pop(iid)
                    out.append(Finding(f"{name} / {iid}", False, f"{who(iid, g)} left; the services have their tasks",
                                       cluster=name, link=link))
        return out

    def _check_ecs_usage(self, p: dict, now: int) -> list[Finding]:
        names = [c["name"] for c in self._ecs_clusters(p["clusters"])]
        data = self._need("metrics").clusters(3)["clusters"]
        cfg = self.config()
        out, label = [], "CPU" if p["metric"] == "cpu" else "Memory"
        for n in names:
            series = ((data.get(n) or {}).get(p["metric"]) or {}).get("points", [])
            pts = [v for t, v in series if t >= now - p["minutes"] * MINUTE]
            if not pts:      # CloudWatch can be a few minutes late: the last two points, if recent
                pts = [v for t, v in series[-2:] if t >= now - (p["minutes"] + 15) * MINUTE]
            if not pts:
                continue
            avg = sum(pts) / len(pts)
            lines = lines_for(cfg, p["metric"], n)
            out.append(Finding(n, avg >= lines["warning"],
                               f"{label} {avg:.0f}% on average over {p['minutes']} min (peak {max(pts):.0f}%)",
                               value=round(avg, 1), lines=lines, cluster=n, link="/ui/health?" + self._q(q=n)))
        return out

    # ================================================================ explaining an alert
    def explain(self, f: Finding, now: int | None = None) -> None:
        """Add the evidence someone would look up next to f.extra["details"] / ["links"]: for a
        load balancer finding, the codes and who answered them, the failing paths, the usual rate,
        target health, and the service's own error lines in the same minutes. Best effort."""
        info = f.extra.get("lb")
        if not info or f.extra.get("details") is not None:
            return
        from .lb_logs import LbQuery
        cfg, tg, details, links, facts = self.config(), f.subject, [], [], {}
        start, end = info["start"], info["end"]
        q = dict(start=start, end=end, target_groups=[tg], status_classes=info["classes"], status_codes=info["codes"])
        if info["classes"] and info["codes"]:          # both: the class covers what matters for "who answered"
            q["status_codes"] = []
        lb = self._need("lb")

        def step(name, fn):
            try:
                fn()
            except Exception as e:  # noqa: BLE001 - one missing piece must not lose the alert
                log.info("alert details (%s) for %s: %s", name, tg, _err(e))

        def codes():
            counts = lb.code_counts(LbQuery(**q))
            if not counts:
                return
            app = sum(c["app"] for c in counts.values())
            total = sum(c["n"] for c in counts.values())
            by = ", ".join(f"{code} ×{c['n']:,}" for code, c in list(counts.items())[:5])
            facts["who"] = "service" if app == total else "lb" if app == 0 else "mixed"
            if app == total:
                who = "the service itself answered all of them (the load balancer reached it)"
            elif app == 0:
                who = ("the load balancer answered them itself: no healthy target, target timed out "
                       "or refused the connection")
            else:
                who = f"the service answered {app:,}, the load balancer {total - app:,} (no target / timeout)"
            details.append(f"Codes: {by} · {who}")

        def paths():
            res = lb.paths(LbQuery(**q), None, sort="requests", limit=3)
            items = [i for i in res.get("items", []) if i["requests"]]
            if not items:
                return
            alls = {(i["method"], i["pathGroup"]): i["requests"] for i in lb.paths(
                LbQuery(start=start, end=end, target_groups=[tg]), None, sort="requests", limit=200).get("items", [])}
            facts["path"] = f"{items[0]['method'] or ''} {items[0]['pathGroup']}".strip()
            facts["pathN"] = items[0]["requests"]
            parts = []
            for i in items:
                total = alls.get((i["method"], i["pathGroup"]))
                share = f" of {total:,}" if total else ""
                parts.append(f"{i['method'] or ''} {i['pathGroup']} — {i['requests']:,}{share} ({i['topCode']})")
            details.append("Paths: " + " · ".join(parts))

        def usual():
            if "usual" in info:          # worked out by the rule already
                u = info["usual"]
                details.append("Usual rate: " + (f"{self._pct(u)} over the 24 hours before" if u is not None
                                                 else "no traffic in the 24 hours before"))
                return
            day0 = start - 24 * 3600 * 1000
            base = {r["tg"]: r for r in lb.summary(LbQuery(start=day0, end=start, target_groups=[tg]), None)
                    .get("targetGroups", []) if r.get("tg")}.get(tg)
            if not base or not base["requests"]:
                details.append("Usual rate: no traffic for this target group in the 24 hours before")
                return
            bad = sum(c["n"] for c in lb.code_counts(LbQuery(**{**q, "start": day0, "end": start})).values())
            details.append(f"Usual rate: {bad / base['requests'] * 100:.2f}% over the 24 hours before "
                           f"({bad:,} of {base['requests']:,} requests)")

        def health():
            if self.health is None:
                return
            snap = self.health.snapshot()
            for c in snap["clusters"]:
                for t in c["targetGroups"]:
                    if t["name"] == tg:
                        facts["health"] = (t["counts"].get("healthy", 0), t["total"], c["name"])
                        line = f"Targets now: {t['counts'].get('healthy', 0)} of {t['total']} healthy"
                        bad = sorted({x.get("reason") or x["state"] for x in t["targets"] if x["state"] != "healthy"})
                        if bad:
                            line += f" ({', '.join(bad[:3])})"
                        svcs = [s for s in c["services"] if tg in (s.get("targetGroups") or [])] or \
                            [s for s in c["services"] if s.get("desired")]
                        if svcs:
                            line += " · " + ", ".join(f"{s['name']} {s['running']}/{s['desired']} tasks" for s in svcs[:3])
                        details.append(line + f" (ECS cluster {c['name']})")
                        links.append(("ECS health", "/ui/health?" + self._q(q=c["name"])))
                        return

        def app_logs():
            if not info.get("logs") or self.logs is None:
                return
            from .logs import SearchSpec
            cluster = f.cluster or (tg[3:] if tg.startswith("tg-") else tg)
            if cluster not in self.logs.clusters():
                details.append(f"App logs: none — there is no Vector log folder '{cluster}'")
                return
            s, e = start - 2 * MINUTE, end + 2 * MINUTE
            res = self.logs.search(cluster, SearchSpec(start=s, end=e, query="level:error"), size=1, with_aggs=True)
            n = res["total"]
            links.append(("App errors", f"/ui/logs/{cluster}?" + self._q(q="level:error", start=iso_ms(s), end=iso_ms(e))))
            facts["app"] = {"n": n}
            if not n:
                details.append(f"App logs ({cluster}): no ERROR lines in these minutes — the cause is likely "
                               "outside this service's own logging (crash, timeout, a dependency)")
                return
            top = [x for x in (res.get("facets") or {}).get("exception", []) if x.get("value")][:3]
            if top:
                facts["app"]["top"] = top[0]["value"]
                facts["app"]["topN"] = top[0].get("count")
            line = f"App logs ({cluster}): {n:,} ERROR lines"
            if top:
                line += " · " + ", ".join(f"{x['value']} ×{x['count']:,}" for x in top)
            details.append(line)
            h = res["hits"][0] if res["hits"] else None
            if top:                          # the newest line of the most common error, so the two match
                try:
                    r2 = self.logs.search(cluster, SearchSpec(start=s, end=e, query='level:error exception:"'
                                                              + top[0]["value"].replace('"', "") + '"'),
                                          size=1, with_aggs=False)
                    h = r2["hits"][0] if r2["hits"] else h
                except Exception:  # noqa: BLE001 - no exception column: the newest error line will do
                    pass
            if h:
                text = _error_text(h)
                if text:
                    facts["app"]["newest"] = text
                    facts["app"]["service"] = h.get("service")
                    details.append(f"Newest: {('[' + str(h['service']) + '] ') if h.get('service') else ''}{text}")
        for name, fn in (("codes", codes), ("paths", paths), ("usual", usual), ("health", health), ("logs", app_logs)):
            step(name, fn)
        links.insert(0, ("Failed requests", "/ui/lb-logs?" + self._q(
            tg=tg, start=iso_ms(start), end=iso_ms(end),
            **({"st": info["classes"][0]} if len(info["classes"]) == 1 and not info["codes"] else
               {"code": str(info["codes"][0])} if len(info["codes"]) == 1 and not info["classes"] else {}))))
        f.extra["details"], f.extra["links"], f.extra["facts"] = details, links, facts

    # ================================================================ the checker
    def state(self) -> dict:
        data, _ = self.store.get_json(STATE_KEY)
        return data or {"rules": {}}

    def history(self, limit: int = 100, can_view: Callable[[str], bool] | None = None,
                admin: bool = True) -> list[dict]:
        data, _ = self.store.get_json(HISTORY_KEY)
        items = (data or {}).get("items", [])
        if not admin:
            items = [i for i in items if i.get("cluster") and can_view and can_view(i["cluster"])]
        return items[:limit]

    def run_once(self, now: int | None = None) -> dict:
        now = now or now_ms()
        cfg = self.config()
        conf = settings_of(cfg)
        st = self.state()
        rstates = st.setdefault("rules", {})
        snoozes = self.snoozes()
        drop: set[str] = set()
        # snoozes that ended: remind at the next check if it is still failing
        for key, sn in snoozes.items():
            if sn.get("until") is not None and sn["until"] <= now:
                drop.add(key)
                cur = (rstates.get(sn.get("ruleId")) or {}).get("subjects", {}).get(sn.get("subject"))
                if cur and cur.get("state") == "firing":
                    cur["remind"] = True
        changes: dict[tuple, list] = {}
        ids = set()
        for rule in cfg.get("rules", []):
            ids.add(rule["id"])
            spec = TYPES.get(rule["type"])
            if not rule.get("enabled") or spec is None:
                rstates.pop(rule["id"], None)
                continue
            rs = rstates.setdefault(rule["id"], {"subjects": {}})
            if rs.get("lastRunAt") and now - rs["lastRunAt"] < spec["every"] * MINUTE - 5000:
                continue
            t0 = time.monotonic()
            try:
                memo = rs.setdefault("memo", {}) if spec.get("memo") else None
                firing = {k for k, v in (rs.get("subjects") or {}).items() if v.get("state") == "firing"}
                findings = self.evaluate(rule, now, memo, firing)
                rs["lastError"] = None
            except Exception as e:  # noqa: BLE001 - one rule failing must not stop the others
                if rs.get("lastError") != _err(e):      # log once per new error, not every check
                    log.warning("alert rule %s (%s) can't check: %s", rule["name"], rule["type"], _err(e))
                rs.update(lastRunAt=now, lastError=_err(e), tookMs=int((time.monotonic() - t0) * 1000))
                continue
            rs.update(lastRunAt=now, tookMs=int((time.monotonic() - t0) * 1000))
            events = self._transition(rule, rs, findings, now, conf["repeat"])
            explained = 0
            for kind, level, f, cur in events:
                key = f"{rule['id']}|{f.subject}"
                sn = snoozes.get(key) if key not in drop else None
                snoozed = sn is not None and (sn.get("until") is None or sn["until"] > now)
                if kind == "resolved" and sn is not None:
                    if sn.get("until") is None:          # "until it clears": cleared, so say so
                        drop.add(key)
                        snoozed = False
                if kind == "muted":
                    stats = st.setdefault("muteStats", {})
                    for mid in f.muted_by:
                        stats.setdefault(mid, []).append(now)
                    changes.setdefault((rule["id"], kind, level, False), []).append((f, cur))
                    continue
                if snoozed and kind == "repeat":
                    continue                             # nothing to record either
                if snoozed:
                    cur["snoozedUntil"] = sn.get("until")
                elif kind != "resolved" and explained < 5:
                    self.explain(f, now)
                    explained += 1
                changes.setdefault((rule["id"], kind, level, snoozed), []).append((f, cur))
        for rid in [r for r in rstates if r not in ids]:
            rstates.pop(rid)
        rules = {r["id"]: r for r in cfg.get("rules", [])}
        batches = [(rules[rid], kind, level, items, snoozed) for (rid, kind, level, snoozed), items in changes.items()]
        entries = self._notify(cfg, batches, now) if batches else []
        if drop:
            def mutate(items):
                for k in drop:
                    sn = items.get(k)
                    if sn and (sn.get("until") is None or sn["until"] <= now):
                        items.pop(k, None)
            try:
                self._update_snoozes(mutate)
            except Exception:  # noqa: BLE001
                log.warning("could not tidy the snoozes", exc_info=True)
        week = now - 7 * 1440 * MINUTE
        stats = st.setdefault("muteStats", {})
        for mid in list(stats):
            stats[mid] = [t for t in stats[mid] if t >= week]
            if not stats[mid]:
                stats.pop(mid)
        self._mute_file_warning(cfg, st, now)
        st.update(checkedAt=now, checker={"pid": os.getpid(), "since": int(self._started * 1000)})
        digest = self._maybe_digest(cfg, st, now)
        if digest:
            entries.insert(0, digest)
        self.store.put_json(STATE_KEY, st)
        if entries:
            self._append_history(entries)
        return {"checkedAt": now, "events": entries}

    def _append_history(self, entries: list[dict]) -> None:
        data, _ = self.store.get_json(HISTORY_KEY)
        items = entries + (data or {}).get("items", [])
        self.store.put_json(HISTORY_KEY, {"items": items[:HISTORY_MAX]})

    @staticmethod
    def _transition(rule: dict, rs: dict, findings: list[Finding], now: int,
                    repeat: dict | None = None) -> list[tuple[str, str, Finding, dict]]:
        """Move each subject between OK → pending → firing (at a level) and back. Returns
        [(kind, level, finding, state)] with kind firing / escalated / lowered / repeat / resolved."""
        repeat = repeat or DEFAULT_REPEAT
        spec = TYPES.get(rule["type"], {})
        base = spec.get("level") if spec.get("level") in LEVELS else "warning"
        confirm = confirm_of(rule)
        wait = rule.get("forMinutes", 0) * MINUTE
        subs = rs.setdefault("subjects", {})
        muted = rs.setdefault("muted", {})
        events: list[tuple[str, str, Finding, dict]] = []
        seen = set()
        for f in findings:
            seen.add(f.subject)
            cur = subs.get(f.subject)
            firing = cur is not None and cur["state"] == "firing"
            if f.muted_by:                       # the mute file stops it: kept in the history, never posted
                level = cur.get("level") if firing and cur.get("level") in LEVELS else \
                    f.extra.get("mutedLevel") if f.extra.get("mutedLevel") in LEVELS else base
                m = muted.get(f.subject)
                if firing:                       # it was alerting: stop it quietly (no OK message)
                    subs.pop(f.subject)
                    muted[f.subject] = {"state": "muted", "since": now, "count": confirm, "ids": f.muted_by,
                                        "level": level, "checkedAt": now}
                    events.append(("muted", level, f, {**cur, "mutedIds": f.muted_by, "wasFiring": True}))
                    continue
                if m is None:                    # it would have been pending: the same checks in a row first
                    old = subs.pop(f.subject, None) or {}
                    m = muted[f.subject] = {"state": "pending", "since": old.get("since", now),
                                            "count": old.get("count", 0)}
                m.update(count=m.get("count", 0) + 1, ids=f.muted_by, level=level, checkedAt=now,
                         message=f.message)
                if m["state"] == "pending" and m["count"] >= confirm and now - m["since"] >= max(wait, f.hold * MINUTE):
                    m["state"] = "muted"
                    events.append(("muted", level, f, {"mutedIds": f.muted_by}))
                continue
            muted.pop(f.subject, None)
            if firing and cur.get("level") not in LEVELS:     # fired before levels existed
                cur["level"] = (f.level_now(base) or base) if f.bad or f.lines is not None else base
            if f.lines is not None:
                lvl = level_of(f.value, f.lines, cur.get("level") if firing else None)
            else:
                lvl = (f.level or base) if f.bad else None
            if lvl is None:
                if firing:
                    events.append(("resolved", cur["level"], f, dict(cur)))
                subs.pop(f.subject, None)
                continue
            if cur is None:
                cur = subs[f.subject] = {"state": "pending", "since": now, "count": 0}
            cur["count"] = cur.get("count", 0) + 1
            cur.update(message=f.message, value=f.value, link=f.link, cluster=f.cluster, checkedAt=now,
                       lines=f.lines)
            if cur["state"] == "pending":
                cur["level"] = lvl
                if cur["count"] >= confirm and now - cur["since"] >= max(wait, f.hold * MINUTE):
                    cur.update(state="firing", firedAt=now, notifiedAt=now, up=0)
                    events.append(("firing", lvl, f, dict(cur)))
                continue
            old = cur["level"]
            if RANK[lvl] > RANK[old]:
                cur["up"] = cur.get("up", 0) + 1         # going up needs the same checks in a row
                if cur["up"] >= confirm:
                    cur.update(level=lvl, notifiedAt=now, up=0, was=old, levelAt=now)
                    events.append(("escalated", lvl, f, dict(cur)))
                    continue
                lvl = old
            else:
                cur["up"] = 0
            if RANK[lvl] < RANK[old]:
                cur.update(level=lvl, notifiedAt=now, was=old, levelAt=now)
                events.append(("lowered", lvl, f, dict(cur)))
            elif cur.pop("remind", False) or (repeat.get(old) and
                                              now - cur.get("notifiedAt", now) >= repeat[old] * MINUTE - 5000):
                cur["notifiedAt"] = now
                events.append(("repeat", old, f, dict(cur)))
        for subj in [s for s in muted if s not in seen]:
            muted.pop(subj)
        for subj in [s for s in subs if s not in seen]:
            cur = subs.pop(subj)
            if cur["state"] == "firing":
                events.append(("resolved", cur.get("level") or base,
                               Finding(subj, False, "no longer checked (gone, or out of the rule's scope)",
                                       cluster=cur.get("cluster"), link=cur.get("link")), cur))
        return events

    # -- messages
    def _link(self, cfg: dict, path: str | None) -> str | None:
        base = (cfg.get("settings") or {}).get("consoleUrl")
        return f"{base}{path}" if base and path else None

    def _footer(self, cfg: dict, now: int, rule: dict | None) -> str:
        bits = ["Vector Logs viewer"]
        if rule:
            bits.append(f"rule “{_esc(rule['name'])}”")
        bits.append(self._fmt(cfg, now, True))
        base = (cfg.get("settings") or {}).get("consoleUrl")
        if base:
            bits.append(f"<{base}/ui/alerts|Alerts page>" + (" (snooze there)" if rule else ""))
        return " · ".join(bits)

    @staticmethod
    def _short_exc(name: str) -> str:
        return name.rsplit(".", 1)[-1]

    def _why(self, f: Finding) -> str | None:
        """One line: where it fails and what the service logged about it."""
        facts = f.extra.get("facts") or {}
        bits = []
        if facts.get("path"):
            bits.append(f"`{facts['path']}`" + (f" ({facts['pathN']:,})" if facts.get("pathN") else ""))
        if facts.get("who") == "lb":
            bits.append("answered by the load balancer: no healthy target, timeout or refused connection")
        app = facts.get("app")
        if app is not None and not app.get("n"):
            bits.append("no ERROR lines in its logs")
        return " — ".join(bits) if bits else None

    def _error_lines(self, f: Finding) -> list[str]:
        """The service's error in full (a code block), with how often it came in those minutes."""
        app = (f.extra.get("facts") or {}).get("app") or {}
        if not app.get("n") or not (app.get("newest") or app.get("top")):
            return []
        exc, n, top_n = app.get("top"), app["n"], app.get("topN")
        head = "Most common error" if exc else "Newest error"
        head += f": `{_esc(self._short_exc(exc))}`" if exc else ""
        head += f" ×{top_n:,} of {n:,} ERROR lines" if exc and top_n else f" ({n:,} ERROR lines)"
        if app.get("service"):
            head += f" · {_esc(app['service'])}"
        out = [f"      {head}"]
        if app.get("newest"):
            out.append("```" + _esc(app["newest"]).replace("```", "'''") + "```")
        return out

    @staticmethod
    def _line_text(lines: dict | None, level: str) -> str:
        if not lines or level not in LEVELS:
            return ""
        v = lines[level]
        v = int(v) if v == int(v) else v
        return f"{level} line {v:,}{lines.get('unit', '')}"

    def message(self, cfg: dict, rule: dict, kind: str, items: list[tuple[Finding, dict]], now: int,
                level: str = "critical") -> dict:
        icon = ":large_green_circle:" if kind == "resolved" else ":warning:" if level == "warning" else ":red_circle:"
        tag = "OK again" if kind == "resolved" else level.upper()
        was = next((cur.get("was") for _, cur in items if cur.get("was")), None)
        what = {"firing": "", "repeat": " — still failing", "resolved": "",
                "escalated": f" (up from {was})" if was else "", "lowered": f" (down from {was})" if was else ""}[kind]
        span = next((f.extra.get("span") for f, _ in items if f.extra.get("span")), None)
        count = f"{len(items)} target groups" if len(items) > 1 and span else ""
        head = (f"{icon} *{tag}*{what} · *{_esc(rule['name'])}*" + (f" — {count}" if count else "")
                + (f" · {span}" if span else ""))
        sections = []
        for f, cur in items[:10]:
            link = self._link(cfg, f.link)
            subj = f"<{link}|{_esc(f.subject)}>" if link else f"*{_esc(f.subject)}*"
            text = _esc(f.message).replace("\n", "\n      ")
            line = self._line_text(f.lines or cur.get("lines"), level)
            if kind != "resolved" and line:
                text += f" · {line}"
            if kind == "repeat":
                text += f" (since {self._fmt(cfg, cur.get('firedAt', now))})"
            if kind == "resolved":
                mins = (now - cur.get("firedAt", now)) / MINUTE
                text = f"OK after {_dur(mins)}" + (f": {text}" if f.message else "")
            lines = [f"• {subj} — {text}"]
            if kind != "resolved" and f.extra.get("facts") is not None:     # load balancer: compact
                why = self._why(f)
                if why:
                    lines.append(f"      ↳ {_esc(why)}")
                lines += self._error_lines(f)
                h = (f.extra.get("facts") or {}).get("health")
                if h and h[0] < h[1]:
                    lines.append(f"      ⚠ only {h[0]} of {h[1]} targets healthy (ECS cluster {_esc(h[2])})")
                keep = {"Failed requests", "App errors"} | ({"ECS health"} if h and h[0] < h[1] else set())
                ls = [f"<{u}|{_esc(label)}>" for label, p in f.extra.get("links") or []
                      if label in keep and (u := self._link(cfg, p))]
                if ls:
                    lines.append("      " + " · ".join(ls))
            elif kind != "resolved":
                lines += [f"      ◦ {_esc(d)}" for d in f.extra.get("details") or []]
                ls = [f"<{u}|{_esc(label)}>" for label, p in f.extra.get("links") or [] if (u := self._link(cfg, p))]
                if ls:
                    lines.append("      " + " · ".join(ls))
            sections.append("\n".join(lines))
        if len(items) > 10:
            sections.append(f"…and {len(items) - 10} more")
        fallback = f"{tag}{what}: {rule['name']}: " + "; ".join(f.subject for f, _ in items[:5])
        text = "\n".join(sections)
        if len(text) <= 2900:
            blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": head + "\n" + text}}]
        else:
            blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": head}}]
            blocks += [{"type": "section", "text": {"type": "mrkdwn", "text": t[:2900]}} for t in sections]
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": self._footer(cfg, now, rule)}]})
        return {"text": fallback, "blocks": blocks}

    def _delivery(self, cfg: dict) -> tuple[str | None, str | None]:
        """(webhook URL, why nothing is sent)."""
        if (cfg.get("settings") or {}).get("paused"):
            return None, "Notifications are paused"
        try:
            url = self.webhook(cfg)
            return url, None if url else "No Slack webhook set"
        except Exception as e:  # noqa: BLE001
            return None, f"Can't read the Slack webhook: {_err(e)}"

    def _notify(self, cfg: dict, batches, now: int) -> list[dict]:
        url, why = self._delivery(cfg)
        entries = []
        order = {"muted": -1, "resolved": 0, "lowered": 1, "repeat": 2, "firing": 3, "escalated": 4}
        for rule, kind, level, items, snoozed in sorted(batches, key=lambda b: (order[b[1]], RANK[b[2]])):
            error = why
            if kind == "muted":
                for f, cur in items:
                    entries.append({"id": new_id()[:12], "at": now, "ruleId": rule["id"], "ruleName": rule["name"],
                                    "type": rule["type"], "event": "muted", "level": level, "subject": f.subject,
                                    "message": f.message, "details": [], "links": [], "cluster": f.cluster,
                                    "link": f.link, "since": cur.get("firedAt"), "delivered": False,
                                    "error": f"Muted by {', '.join(f.muted_by)} (alert-mutes.yaml)"
                                             + (" — it was alerting; stopped without an OK message"
                                                if cur.get("wasFiring") else ""),
                                    "snoozed": False, "mutedBy": f.muted_by})
                log.info("alert %s muted by %s: %s", rule["name"], ",".join(sorted({i for f, _ in items for i in f.muted_by})),
                         ", ".join(f.subject for f, _ in items))
                continue
            if snoozed:
                until = next((cur.get("snoozedUntil") for _, cur in items), None)
                error = "Snoozed " + (f"until {self._fmt(cfg, until, True)}" if until else "until it clears")
            elif url:
                try:
                    self.sender(url, self.message(cfg, rule, kind, items, now, level))
                except Exception as e:  # noqa: BLE001
                    error = _err(e)
                    log.warning("alert %s: Slack message failed: %s", rule["name"], error)
            for f, cur in items:
                entries.append({"id": new_id()[:12], "at": now, "ruleId": rule["id"], "ruleName": rule["name"],
                                "type": rule["type"], "event": kind, "level": level, "was": cur.get("was"),
                                "subject": f.subject, "message": f.message,
                                "details": f.extra.get("details") or [], "links": [list(x) for x in f.extra.get("links") or []],
                                "cluster": f.cluster, "link": f.link, "since": cur.get("firedAt"),
                                "delivered": error is None, "error": error, "snoozed": bool(snoozed)})
            log.info("alert %s %s %s: %s%s", rule["name"], kind, level, ", ".join(f.subject for f, _ in items),
                     f" (not sent: {error})" if error else "")
        return entries

    # -- the mute file on the page and in Slack
    def mute_overview(self, cfg: dict | None = None, st: dict | None = None) -> dict:
        cfg = cfg or self.config()
        entries = self.mutes.get()
        stats = (st if st is not None else self.state()).get("muteStats") or {}
        day = now_ms() - 1440 * MINUTE
        return {"file": self.mutes.path, "exists": self.mutes.exists, "error": self.mutes.error,
                "loadedAt": int(self.mutes.loaded_at * 1000) if self.mutes.loaded_at else None,
                "paused": bool(settings_of(cfg).get("mutesPaused")),
                "entries": [{"id": m.id, "enabled": m.enabled, "title": m.title, "owner": m.owner, "reason": m.reason,
                             "kind": m.kind, "rules": m.rules, "matches": m.describe(), "match": m.match,
                             "muted24h": sum(1 for t in stats.get(m.id, []) if t >= day),
                             "muted7d": len(stats.get(m.id, []))} for m in entries]}

    def _mute_file_warning(self, cfg: dict, st: dict, now: int) -> None:
        """Post once when the mute file gets a mistake (the last good version stays in use)."""
        self.mutes.get()
        err = self.mutes.error
        if not err:
            st.pop("muteFileError", None)
            return
        if st.get("muteFileError") == err:
            return
        st["muteFileError"] = err
        url, _ = self._delivery(cfg)
        if not url:
            return
        text = (f":warning: *The alert mute file has a mistake* — the last good version is still used.\n"
                f"`{_esc(os.path.basename(self.mutes.path))}`: {_esc(err)}")
        try:
            self.sender(url, {"text": "The alert mute file has a mistake", "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}},
                {"type": "context", "elements": [{"type": "mrkdwn", "text": self._footer(cfg, now, None)}]}]})
        except Exception as e:  # noqa: BLE001
            log.warning("could not post the mute file warning: %s", _err(e))

    # -- the weekly summary
    def _local(self, cfg: dict, ms: int) -> datetime:
        zone = self._zone((cfg.get("settings") or {}).get("timeZone") or "UTC")
        d = datetime.fromtimestamp(ms / 1000, timezone.utc)
        return d.astimezone(zone) if zone else d

    def _maybe_digest(self, cfg: dict, st: dict, now: int) -> dict | None:
        d = settings_of(cfg)["digest"]
        if not d.get("enabled"):
            return None
        local = self._local(cfg, now)
        hh, mm = (int(x) for x in d["time"].split(":"))
        if DAYS[local.weekday()] != d["day"] or (local.hour, local.minute) < (hh, mm):
            return None
        day = local.date().isoformat()
        if st.get("digestSentFor") == day:
            return None
        st["digestSentFor"] = day
        try:
            return self.send_digest(None, now, st=st, cfg=cfg)
        except Exception as e:  # noqa: BLE001
            log.warning("weekly alert summary failed: %s", _err(e))
            return None

    def digest(self, cfg: dict, now: int, st: dict | None = None) -> dict:
        """The weekly summary message: alerts of the last 7 days per rule, what fails now, what is
        snoozed, and whether the checker itself is well."""
        st = st if st is not None else self.state()
        week = now - 7 * 1440 * MINUTE
        items = [i for i in self.history(HISTORY_MAX) if i.get("at", 0) >= week and i.get("event") not in ("digest", "muted")]
        per: dict[str, dict] = {}
        for i in items:
            r = per.setdefault(i["ruleName"], {"started": 0, "critical": 0, "subjects": {}})
            if i["event"] == "firing":
                r["started"] += 1
                r["subjects"][i["subject"]] = r["subjects"].get(i["subject"], 0) + 1
                if (i.get("level") or "critical") == "critical":
                    r["critical"] += 1
            elif i["event"] == "escalated":
                r["critical"] += 1
        started = sum(r["started"] for r in per.values())
        crit = sum(r["critical"] for r in per.values())
        a, b = self._local(cfg, week), self._local(cfg, now)
        head = f":bar_chart: *Weekly alert summary* · {a:%d %b}–{b:%d %b}"
        parts = [f"*This week:* {started} alert{'s' if started != 1 else ''} started"
                 + (f", {crit} reached critical" if crit else "")
                 + ("" if items else " — nothing happened")]
        for name, r in sorted(per.items(), key=lambda kv: (-kv[1]["critical"], -kv[1]["started"]))[:12]:
            if not r["started"] and not r["critical"]:
                continue
            top = sorted(r["subjects"].items(), key=lambda kv: -kv[1])[:3]
            parts.append(f"• {_esc(name)} — {r['started']}" + (f" ({r['critical']} critical)" if r["critical"] else "")
                         + (" · most: " + ", ".join(f"{_esc(s)} ×{n}" for s, n in top) if top else ""))
        rules = {r["id"]: r for r in (cfg.get("rules") or [])}
        now_bad = []
        for rid, rs in (st.get("rules") or {}).items():
            for subj, cur in (rs.get("subjects") or {}).items():
                if cur.get("state") == "firing" and rid in rules:
                    now_bad.append((RANK.get(cur.get("level"), 1), rules[rid]["name"], subj, cur))
        now_bad.sort(key=lambda x: (-x[0], x[1], x[2]))
        parts.append(f"*Failing now:* {len(now_bad) or 'nothing'}")
        for _, name, subj, cur in now_bad[:10]:
            icon = ":red_circle:" if cur.get("level") == "critical" else ":warning:"
            parts.append(f"• {icon} {_esc(name)} — {_esc(subj)} since {self._fmt(cfg, cur.get('firedAt', now), True)}")
        if len(now_bad) > 10:
            parts.append(f"…and {len(now_bad) - 10} more")
        snoozed = [s for s in self.snoozes().values() if s.get("until") is None or s["until"] > now]
        if snoozed:
            parts.append(f"*Snoozed:* {len(snoozed)}")
            for s in snoozed[:10]:
                until = f"until {self._fmt(cfg, s['until'], True)}" if s.get("until") else "until it clears"
                parts.append(f"• {_esc(s.get('ruleName', ''))} — {_esc(s['subject'])} {until} (by {_esc(s.get('by', ''))})")
        stats = st.get("muteStats") or {}
        counts = sorted(((mid, sum(1 for t in ts if t >= week)) for mid, ts in stats.items()), key=lambda x: -x[1])
        counts = [(mid, n) for mid, n in counts if n]
        mo = self.mute_overview(cfg, st)
        on = sum(1 for m in mo["entries"] if m["enabled"])
        parts.append(f"*Muted this week:* {sum(n for _, n in counts) or 'nothing'}"
                     + (" — " + ", ".join(f"{_esc(mid)} ×{n}" for mid, n in counts[:15]) if counts else "")
                     + f" · mute file: {on} of {len(mo['entries'])} entries on"
                     + (" · *mutes paused*" if mo["paused"] else "")
                     + (f" · *file has a mistake:* {_esc(mo['error'][:150])}" if mo["error"] else ""))
        broken = [(rules[rid]["name"], rs["lastError"]) for rid, rs in (st.get("rules") or {}).items()
                  if rs.get("lastError") and rid in rules]
        parts.append("*Checker:* " + ("all rules can check" if not broken else
                                     f"{len(broken)} rule(s) can't check: "
                                     + "; ".join(f"{_esc(n)} ({_esc(e[:120])})" for n, e in broken[:5])))
        text = head + "\n" + "\n".join(parts)
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": t[:2900]}}
                  for t in _chunks_of(text, 2900)]
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": self._footer(cfg, now, None)}]})
        return {"text": f"Weekly alert summary: {started} alerts, {len(now_bad)} failing now", "blocks": blocks,
                "summary": {"started": started, "critical": crit, "failing": len(now_bad), "snoozed": len(snoozed)}}

    def send_digest(self, actor: str | None, now: int | None = None, st: dict | None = None,
                    cfg: dict | None = None) -> dict:
        """Post the weekly summary now (the checker on its day, or an admin from the Alerts page)."""
        now = now or now_ms()
        cfg = cfg or self.config()
        msg = self.digest(cfg, now, st)
        summary = msg.pop("summary")
        if actor:                                     # asked for: send even when paused
            url = self.webhook(cfg)
            error = None if url else "No Slack webhook set"
            if not url:
                raise bad_request("NO_WEBHOOK", "Save a Slack webhook URL first")
        else:
            url, error = self._delivery(cfg)
        if url:
            try:
                self.sender(url, msg)
            except Exception as e:  # noqa: BLE001
                error = _err(e)
                if actor:
                    raise
        entry = {"id": new_id()[:12], "at": now, "ruleId": None, "ruleName": "Weekly summary", "type": "digest",
                 "event": "digest", "level": None, "subject": "All alerts",
                 "message": f"{summary['started']} alerts this week ({summary['critical']} critical), "
                            f"{summary['failing']} failing now, {summary['snoozed']} snoozed",
                 "details": [], "links": [], "cluster": None, "link": "/ui/alerts", "since": None,
                 "delivered": error is None, "error": error, "by": actor}
        if actor:
            self._append_history([entry])
        return entry

    # -- background thread
    def start(self) -> None:
        self.thread = threading.Thread(target=self._loop, name="alerts", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        from .lb_logs import _FileLock
        lock = _FileLock(self.root / ".checker.lock")
        self._stop.wait(20)                      # let the app finish starting
        while not self._stop.is_set():
            if lock.fh is None and not lock.acquire():
                self._stop.wait(60)              # another worker checks
                continue
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - keep the thread alive
                log.exception("alert check failed")
            self._stop.wait(max(10, self.settings.alerts_interval_seconds))


# ------------------------------------------------------------------ helpers
def _err(e: Exception) -> str:
    if isinstance(e, ApiError):
        return f"{e.code}: {e.message}"[:400]
    return f"{type(e).__name__}: {e}"[:400]


def _lb_lines(warning: float, critical: float | None, pct: bool) -> dict:
    """Two lines for a load balancer rule: critical given or double the warning. A rate clears
    2 points (at most a fifth of the line) below; a count a tenth below."""
    crit = critical or (min(100.0, warning * 2) if pct else warning * 2)
    crit = max(crit, warning)
    h = min(HYSTERESIS, warning * 0.2) if pct else max(1, round(warning * 0.1))
    return {"warning": warning, "critical": crit, "h": h, "unit": "%" if pct else ""}


def _int(v: Any, label: str, lo: int, hi: int) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise bad_request("INVALID_SETTINGS", f"'{label}' must be a whole number") from None
    if not lo <= n <= hi:
        raise bad_request("INVALID_SETTINGS", f"'{label}' must be between {lo} and {hi}")
    return n


def _pair(v: Any, label: str) -> dict:
    if not isinstance(v, dict):
        raise bad_request("INVALID_SETTINGS", f"{label}: give warning and critical")
    w = _int(v.get("warning"), f"{label} warning", 1, 100)
    c = _int(v.get("critical"), f"{label} critical", 1, 100)
    if c <= w:
        raise bad_request("INVALID_SETTINGS", f"{label}: critical ({c}%) must be above warning ({w}%)")
    return {"warning": w, "critical": c}


def _thresholds(t: Any) -> dict:
    """{cpu, memory, disk: {warning, critical}, clusters: {name: {cpu?, memory?}}}, checked."""
    if not isinstance(t, dict):
        raise bad_request("INVALID_SETTINGS", "thresholds must be an object")
    out: dict[str, Any] = {}
    for k, label in (("cpu", "CPU"), ("memory", "Memory"), ("disk", "Server disk")):
        if k in t:
            out[k] = _pair(t[k], label)
    if "clusters" in t:
        cl = t["clusters"] or {}
        if not isinstance(cl, dict) or len(cl) > 200:
            raise bad_request("INVALID_SETTINGS", "clusters must be an object (200 at most)")
        out["clusters"] = {}
        for name, v in cl.items():
            if not NAME_RE.match(str(name)) or not isinstance(v, dict):
                raise bad_request("INVALID_SETTINGS", f"Bad cluster override '{name}'")
            one = {m: _pair(v[m], f"{name} {label}") for m, label in (("cpu", "CPU"), ("memory", "memory")) if v.get(m)}
            if one:
                out["clusters"][name] = one
    return out


def _parse_iso(text: str | None) -> int | None:
    if not text:
        return None
    try:
        return int(datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def _alb_end(text: str) -> int | None:
    try:
        return int(datetime.strptime(text, "%Y%m%dT%H%MZ").replace(tzinfo=timezone.utc).timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def _error_text(h: dict, limit: int = 2000) -> str:
    """An application log line's error in full: the message (or body), without the stack trace
    lines ("at …") that follow it, up to `limit` characters."""
    text = str(h.get("msg") or h.get("body") or h.get("raw") or "").strip()
    exc = str(h.get("exception") or "").strip()
    if exc and exc not in text:
        text = f"{exc}: {text}" if text else exc
    text = re.split(r"\n\s*at [\w$.<>]+\(", text, maxsplit=1)[0]          # drop the stack trace
    text = re.sub(r"[ \t]+", " ", text).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def _chunks_of(text: str, size: int) -> list[str]:
    """Split on line ends into pieces of at most `size` characters."""
    out, cur = [], ""
    for line in text.split("\n"):
        if cur and len(cur) + 1 + len(line) > size:
            out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    return out + ([cur] if cur else [])


def _dur(minutes: float) -> str:
    if minutes < 90:
        return f"{max(1, round(minutes))} min"
    if minutes < 48 * 60:
        return f"{minutes / 60:.1f} h"
    return f"{minutes / 1440:.1f} days"
