"""Alerts to Slack: rules that check the viewer, the logs, ECS and the load balancers, and post to
a Slack incoming webhook when something goes wrong (and again when it is back to normal).

Where things live
    state/alerts.json          rules + settings (console URL, time zone, paused)        S3 state store
    state/alerts-state.json    what each rule found last time (firing / pending)       S3 state store
    state/alerts-history.json  the last 500 alerts sent (or not sent, and why)         S3 state store
    <prefix>alerts             {"slackWebhookUrl": ...}                                 Secrets Manager
    CACHE_DIR/alerts/          the checker's lock and the viewer's own 5xx responses   local disk

One checker runs per container (every API worker starts one; a file lock lets only one check).
It wakes up every ALERTS_INTERVAL_SECONDS and runs each enabled rule when it is due.

A finding is per "subject" (a cluster, a target group, a service …). A subject that is bad
becomes pending, and firing once it has been bad for the rule's forMinutes; firing sends one
Slack message (grouped per rule), repeats every repeatMinutes if set, and a "resolved" message
is sent when the subject is fine again.
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
from .util import iso, iso_ms, new_id

log = logging.getLogger("vector_logs.alerts")

CONFIG_KEY = "state/alerts.json"
STATE_KEY = "state/alerts-state.json"
HISTORY_KEY = "state/alerts-history.json"
SECRET = "alerts"
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


CATALOG: list[dict] = [
    {"type": "app_aws", "group": "Vector Logs viewer", "title": "Viewer can't reach AWS",
     "description": "The viewer can't list the application logs in S3, read the ECS and load balancer state, "
                    "or read CloudWatch metrics (access denied, wrong region, no network).",
     "every": 1, "forMinutes": 2, "params": []},
    {"type": "app_converter", "group": "Vector Logs viewer", "title": "Load balancer log converter failing",
     "description": "The converter that turns the ALB access logs into the dashboard's data reports an error, "
                    "or hasn't updated for longer than the limit.",
     "every": 1, "forMinutes": 0,
     "params": [_num("stalledMinutes", "Stalled after", 30, 10, 1440, "min")]},
    {"type": "app_disk", "group": "Vector Logs viewer", "title": "Server disk almost full",
     "description": "The disk that holds the viewer's cache (Parquet copies, converted load balancer data) is "
                    "fuller than the limit.",
     "every": 5, "forMinutes": 0,
     "params": [_num("thresholdPercent", "Used at least", 85, 50, 99, "%")]},
    {"type": "app_errors", "group": "Vector Logs viewer", "title": "Viewer server errors",
     "description": "The viewer answered with server errors (HTTP 5xx) at least this often. Searches that are too "
                    "big for the memory limit (507) don't count.",
     "every": 1, "forMinutes": 0,
     "params": [_num("threshold", "At least", 10, 1, 100000, "errors"),
                _num("minutes", "Within", 15, 5, 1440, "min")]},
    {"type": "logs_stale", "group": "Application logs", "title": "Application logs stopped arriving",
     "description": "Vector hasn't written any new log file for a cluster for longer than the limit "
                    "(the service is down, Vector stopped, or it can't write to S3).",
     "every": 5, "forMinutes": 0,
     "params": [_pick("clusters", "logClusters", "Clusters"),
                _num("minutes", "No new files for", 30, 10, 1440, "min")]},
    {"type": "log_errors", "group": "Application logs", "title": "Error lines in application logs",
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
     "description": "AWS hasn't delivered new access log files to S3. AWS writes no file for 5 minutes without "
                    "requests, so watch quiet load balancers with \"All of them\".",
     "every": 5, "forMinutes": 0,
     "params": [_pick("lbs", "lbs", "Load balancers"),
                _select("mode", "Alert when", [("all", "All of them have stopped (delivery is broken)"),
                                               ("each", "Any one of them has stopped")], "all"),
                _num("minutes", "No new files for", 60, 15, 1440, "min")]},
    {"type": "lb_errors", "group": "Load balancers", "title": "HTTP error rate",
     "description": f"Share of requests answered with 5xx (or 4xx) per target group, from the ALB access logs. "
                    f"The data is about {LB_LAG_MINUTES} minutes behind.",
     "every": 5, "forMinutes": 0,
     "params": [_pick("targetGroups", "targetGroups", "Target groups"),
                _select("statusClass", "Status", [("5xx", "5xx (server errors)"), ("4xx", "4xx (client errors)")], "5xx"),
                _num("thresholdPercent", "At least", 5, 0.1, 100, "% of requests"),
                _num("minRequests", "Only with at least", 50, 1, 10_000_000, "requests"),
                _num("minutes", "Within", 15, 5, 240, "min")]},
    {"type": "lb_latency", "group": "Load balancers", "title": "Slow responses (p95)",
     "description": f"95th percentile of the target's response time per target group, from the ALB access logs "
                    f"(approximate). The data is about {LB_LAG_MINUTES} minutes behind.",
     "every": 5, "forMinutes": 0,
     "params": [_pick("targetGroups", "targetGroups", "Target groups"),
                _num("p95Ms", "p95 at least", 2000, 10, 600_000, "ms"),
                _num("minRequests", "Only with at least", 50, 1, 10_000_000, "requests"),
                _num("minutes", "Within", 15, 5, 240, "min")]},
    {"type": "ecs_targets", "group": "ECS", "title": "Unhealthy targets",
     "description": "Targets of an ECS cluster's target groups fail their health checks (unhealthy, draining, "
                    "unavailable or still starting).",
     "every": 1, "forMinutes": 5,
     "params": [_pick("clusters", "ecsClusters", "ECS clusters"),
                _num("minUnhealthy", "At least", 1, 1, 1000, "targets")]},
    {"type": "ecs_tasks", "group": "ECS", "title": "Service below its desired tasks",
     "description": "An ECS service runs fewer tasks than it should (crashing tasks, failed deployment, no capacity).",
     "every": 1, "forMinutes": 10,
     "params": [_pick("clusters", "ecsClusters", "ECS clusters")]},
    {"type": "ecs_usage", "group": "ECS", "title": "High CPU or memory",
     "description": "Average CPU or memory use of an ECS cluster (CloudWatch) over the window is above the limit.",
     "every": 5, "forMinutes": 0,
     "params": [_pick("clusters", "ecsClusters", "ECS clusters"),
                _select("metric", "Metric", [("cpu", "CPU"), ("memory", "Memory")], "cpu"),
                _num("thresholdPercent", "Average at least", 90, 1, 100, "%"),
                _num("minutes", "Over", 15, 5, 180, "min")]},
]
TYPES = {t["type"]: t for t in CATALOG}

# Rules created on first use (they only send messages once a webhook is set).
DEFAULT_RULES = [
    ("app_aws", "Viewer can't reach AWS", {}),
    ("app_converter", "Load balancer log converter failing", {}),
    ("app_disk", "Server disk almost full", {}),
    ("app_errors", "Viewer server errors", {}),
    ("logs_stale", "Application logs stopped arriving", {}),
    ("lb_logs_stale", "Load balancer logs stopped arriving", {}),
    ("ecs_targets", "Unhealthy targets", {}),
    ("ecs_tasks", "Service below its desired tasks", {}),
    ("ecs_usage", "High CPU", {"metric": "cpu"}),
    ("ecs_usage", "High memory", {"metric": "memory"}),
    ("lb_errors", "5xx rate above 5%", {}),
]


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
        elif p["kind"] == "text":
            v = str(v or "").strip()
            if len(v) > 500:
                raise bad_request("INVALID_ALERT", f"'{label}' is too long (500 characters at most)")
        elif p["kind"] == "bool":
            v = bool(v)
        params[p["key"]] = v

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
            "forMinutes": minutes("forMinutes", spec["forMinutes"], 1440),
            "repeatMinutes": minutes("repeatMinutes", 0, 10080)}


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
        self._stop = threading.Event()
        self._started = time.time()
        self._webhook_version = None
        self.thread: threading.Thread | None = None

    # -- config (rules + settings)
    def config(self) -> dict:
        data, _ = self.store.get_json(CONFIG_KEY)
        if data is not None:
            return data
        doc = {"version": 1, "rules": [], "settings": {"consoleUrl": "", "timeZone": "Asia/Kolkata",
                                                       "paused": False},
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
                    merged = {**r, **{k: v for k, v in body.items() if k in ("name", "enabled", "forMinutes",
                                                                            "repeatMinutes")}}
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
        if "paused" in body:
            s["paused"] = bool(body["paused"])

        def mutate(doc):
            doc.setdefault("settings", {}).update(s, updatedAt=iso(), updatedBy=actor)
            return doc["settings"]
        return self._update(mutate)

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

    def _fmt(self, cfg: dict, ms: int, date: bool = False) -> str:
        tz = (cfg.get("settings") or {}).get("timeZone") or "UTC"
        zone = self._zone(tz)
        d = datetime.fromtimestamp(ms / 1000, timezone.utc)
        d = d.astimezone(zone) if zone else d
        return d.strftime("%d %b %H:%M %Z" if date else "%H:%M")

    # -- links into the console
    @staticmethod
    def _q(**kw) -> str:
        from urllib.parse import urlencode
        return urlencode({k: v for k, v in kw.items() if v not in (None, "")})

    # ================================================================ checks
    def evaluate(self, rule: dict, now: int | None = None) -> list[Finding]:
        now = now or now_ms()
        fn = getattr(self, f"_check_{rule['type']}", None)
        if fn is None:
            raise bad_request("UNKNOWN_ALERT_TYPE", f"Unknown alert type '{rule['type']}'")
        return fn(rule.get("params") or {}, now)

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
            return [Finding(subject, True, f"{err.get('code')}: {err.get('message')}")]
        return [Finding(subject, False, "running")]

    def _check_app_disk(self, p: dict, now: int) -> list[Finding]:
        path = Path(self.settings.cache_dir)
        path.mkdir(parents=True, exist_ok=True)
        u = shutil.disk_usage(path)
        pct = u.used / u.total * 100 if u.total else 0
        return [Finding("Server disk", pct >= p["thresholdPercent"],
                        f"{pct:.0f}% used ({u.free / 2**30:.1f} GB free of {u.total / 2**30:.0f} GB)", value=round(pct, 1))]

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
            n = res["total"]
            msg = f"{n:,} lines matching {p['query'] or 'anything'} in {p['minutes']} min"
            if n and p.get("includeSample") and res["hits"]:
                h = res["hits"][0]
                sample = str(h.get("msg") or h.get("body") or h.get("raw") or "").replace("\n", " ")[:200]
                if sample:
                    msg += f"\nnewest: {('[' + str(h['service']) + '] ') if h.get('service') else ''}{sample}"
            link = f"/ui/logs/{c}?" + self._q(q=p["query"], start=iso_ms(start), end=iso_ms(now))
            out.append(Finding(c, n >= p["threshold"], msg, value=n, link=link, cluster=c))
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

    def _check_lb_errors(self, p: dict, now: int) -> list[Finding]:
        start, end, rows = self._lb_rows(p, now)
        cfg, cls, out = self.config(), p["statusClass"], []
        span = f"{self._fmt(cfg, start)}–{self._fmt(cfg, end, True)}"
        for r in rows:
            n = r["s5xx"] if cls == "5xx" else r["s4xx"]
            pct = n / r["requests"] * 100 if r["requests"] else 0
            bad = r["requests"] >= p["minRequests"] and pct >= p["thresholdPercent"]
            msg = f"{pct:.1f}% {cls}: {n:,} of {r['requests']:,} requests ({span})"
            te = r.get("topError")
            if bad and te and str(te.get("code", ""))[:1] == cls[0]:
                msg += f"\nmost: {te['code']} {te.get('method') or ''} {te.get('pathGroup') or ''} ×{te['count']:,}"
            out.append(Finding(r["tg"], bad, msg, value=round(pct, 2), cluster=r.get("cluster"),
                               link="/ui/lb?" + self._q(tg=r["tg"], st=cls, start=iso_ms(start), end=iso_ms(end))))
        return out

    def _check_lb_latency(self, p: dict, now: int) -> list[Finding]:
        start, end, rows = self._lb_rows(p, now)
        cfg, out = self.config(), []
        span = f"{self._fmt(cfg, start)}–{self._fmt(cfg, end, True)}"
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
        out = []
        for c in self._ecs_clusters(p["clusters"]):
            if not c["targetGroups"]:
                continue
            bad_n = c["targets"]["bad"]
            parts = []
            for tg in c["targetGroups"]:
                if tg["counts"].get("bad"):
                    reasons = sorted({t.get("reason") or t["state"] for t in tg["targets"] if t["state"] != "healthy"})
                    parts.append(f"{tg['name']}: {tg['counts']['bad']} of {tg['total']} not healthy"
                                 + (f" ({', '.join(reasons[:3])})" if reasons else ""))
            msg = "; ".join(parts) if parts else f"{c['targets']['healthy']} of {c['targets']['total']} targets healthy"
            out.append(Finding(c["name"], bad_n >= p["minUnhealthy"], msg, value=bad_n, cluster=c["name"],
                               link="/ui/health?" + self._q(q=c["name"])))
        return out

    def _check_ecs_tasks(self, p: dict, now: int) -> list[Finding]:
        out = []
        for c in self._ecs_clusters(p["clusters"]):
            for s in c["services"]:
                if (s.get("status") or "ACTIVE") != "ACTIVE" or not s["desired"]:
                    continue
                bad = s["running"] < s["desired"]
                msg = f"running {s['running']} of {s['desired']} tasks"
                if s.get("pending"):
                    msg += f" ({s['pending']} starting)"
                if bad and s.get("rollout") and s["rollout"] != "COMPLETED":
                    msg += f" · deployment {s['rollout'].lower().replace('_', ' ')}"
                if bad and s.get("lastEvent") and s["lastEvent"].get("message"):
                    msg += f"\nlast event: {str(s['lastEvent']['message'])[:200]}"
                out.append(Finding(f"{c['name']} / {s['name']}", bad, msg, value=s["running"], cluster=c["name"],
                                   link="/ui/health?" + self._q(q=c["name"])))
        return out

    def _check_ecs_usage(self, p: dict, now: int) -> list[Finding]:
        names = [c["name"] for c in self._ecs_clusters(p["clusters"])]
        data = self._need("metrics").clusters(3)["clusters"]
        out, label = [], "CPU" if p["metric"] == "cpu" else "Memory"
        for n in names:
            pts = [v for t, v in ((data.get(n) or {}).get(p["metric"]) or {}).get("points", [])
                   if t >= now - p["minutes"] * MINUTE]
            if not pts:
                continue
            avg = sum(pts) / len(pts)
            out.append(Finding(n, avg >= p["thresholdPercent"],
                               f"{label} {avg:.0f}% on average over {p['minutes']} min (peak {max(pts):.0f}%)",
                               value=round(avg, 1), cluster=n, link="/ui/health?" + self._q(q=n)))
        return out

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
        st = self.state()
        rstates = st.setdefault("rules", {})
        changes: list[tuple[dict, str, list[tuple[Finding, dict]]]] = []
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
                findings = self.evaluate(rule, now)
                rs["lastError"] = None
            except Exception as e:  # noqa: BLE001 - one rule failing must not stop the others
                if rs.get("lastError") != _err(e):      # log once per new error, not every check
                    log.warning("alert rule %s (%s) can't check: %s", rule["name"], rule["type"], _err(e))
                rs.update(lastRunAt=now, lastError=_err(e), tookMs=int((time.monotonic() - t0) * 1000))
                continue
            rs.update(lastRunAt=now, tookMs=int((time.monotonic() - t0) * 1000))
            fired, repeated, resolved = self._transition(rule, rs, findings, now)
            for kind, items in (("firing", fired), ("repeat", repeated), ("resolved", resolved)):
                if items:
                    changes.append((rule, kind, items))
        for rid in [r for r in rstates if r not in ids]:
            rstates.pop(rid)
        entries = self._notify(cfg, changes, now) if changes else []
        st.update(checkedAt=now, checker={"pid": os.getpid(), "since": int(self._started * 1000)})
        self.store.put_json(STATE_KEY, st)
        if entries:
            data, _ = self.store.get_json(HISTORY_KEY)
            items = entries + (data or {}).get("items", [])
            self.store.put_json(HISTORY_KEY, {"items": items[:HISTORY_MAX]})
        return {"checkedAt": now, "events": entries}

    @staticmethod
    def _transition(rule: dict, rs: dict, findings: list[Finding], now: int):
        subs = rs.setdefault("subjects", {})
        fired, repeated, resolved, seen = [], [], [], set()
        wait = rule.get("forMinutes", 0) * MINUTE
        repeat = rule.get("repeatMinutes", 0) * MINUTE
        for f in findings:
            seen.add(f.subject)
            cur = subs.get(f.subject)
            if f.bad:
                if cur is None:
                    cur = subs[f.subject] = {"state": "pending", "since": now}
                cur.update(message=f.message, value=f.value, link=f.link, cluster=f.cluster, checkedAt=now)
                if cur["state"] == "pending" and now - cur["since"] >= wait:
                    cur.update(state="firing", firedAt=now, notifiedAt=now)
                    fired.append((f, dict(cur)))
                elif cur["state"] == "firing" and repeat and now - cur.get("notifiedAt", now) >= repeat:
                    cur["notifiedAt"] = now
                    repeated.append((f, dict(cur)))
            else:
                if cur and cur["state"] == "firing":
                    resolved.append((f, dict(cur)))
                subs.pop(f.subject, None)
        for subj in [s for s in subs if s not in seen]:
            cur = subs.pop(subj)
            if cur["state"] == "firing":
                resolved.append((Finding(subj, False, "no longer checked (gone, or out of the rule's scope)",
                                         cluster=cur.get("cluster"), link=cur.get("link")), cur))
        return fired, repeated, resolved

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
            bits.append(f"<{base}/ui/alerts|Alerts page>")
        return " · ".join(bits)

    def message(self, cfg: dict, rule: dict, kind: str, items: list[tuple[Finding, dict]], now: int) -> dict:
        icon = {"firing": ":red_circle:", "repeat": ":red_circle:", "resolved": ":large_green_circle:"}[kind]
        what = {"firing": "", "repeat": " — still failing", "resolved": " — resolved"}[kind]
        lines = []
        for f, cur in items[:10]:
            link = self._link(cfg, f.link)
            subj = f"<{link}|{_esc(f.subject)}>" if link else f"*{_esc(f.subject)}*"
            text = _esc(f.message).replace("\n", "\n      ")
            if kind == "repeat":
                text += f" (since {self._fmt(cfg, cur.get('firedAt', now))})"
            if kind == "resolved":
                mins = (now - cur.get("firedAt", now)) / MINUTE
                text = f"back to normal after {_dur(mins)}" + (f": {text}" if f.message else "")
            lines.append(f"• {subj} — {text}")
        if len(items) > 10:
            lines.append(f"…and {len(items) - 10} more")
        head = f"{icon} *{_esc(rule['name'])}*{what}"
        fallback = f"{rule['name']}{what}: " + "; ".join(f.subject for f, _ in items[:5])
        return {"text": fallback, "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": (head + "\n" + "\n".join(lines))[:2900]}},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": self._footer(cfg, now, rule)}]}]}

    def _notify(self, cfg: dict, changes, now: int) -> list[dict]:
        paused = (cfg.get("settings") or {}).get("paused")
        url, why = None, None
        if paused:
            why = "Notifications are paused"
        else:
            try:
                url = self.webhook(cfg)
                why = None if url else "No Slack webhook set"
            except Exception as e:  # noqa: BLE001
                why = f"Can't read the Slack webhook: {_err(e)}"
        entries = []
        for rule, kind, items in changes:
            error = why
            if url:
                try:
                    self.sender(url, self.message(cfg, rule, kind, items, now))
                except Exception as e:  # noqa: BLE001
                    error = _err(e)
                    log.warning("alert %s: Slack message failed: %s", rule["name"], error)
            for f, cur in items:
                entries.append({"id": new_id()[:12], "at": now, "ruleId": rule["id"], "ruleName": rule["name"],
                                "type": rule["type"], "event": kind, "subject": f.subject, "message": f.message,
                                "cluster": f.cluster, "link": f.link, "since": cur.get("firedAt"),
                                "delivered": error is None, "error": error})
            log.info("alert %s %s: %s%s", rule["name"], kind, ", ".join(f.subject for f, _ in items),
                     f" (not sent: {error})" if error else "")
        return entries

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


def _dur(minutes: float) -> str:
    if minutes < 90:
        return f"{max(1, round(minutes))} min"
    if minutes < 48 * 60:
        return f"{minutes / 60:.1f} h"
    return f"{minutes / 1440:.1f} days"
