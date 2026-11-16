"""The end-of-day ops summary posted to the summaries Slack channel (see AlertService.send_daily).

One finished day in the chosen zone (US Eastern by default), against the same day last week:
    API calls        total, change, 4xx / 5xx share, busiest minute
    Orders           total and per service (OMS / OMS-11 / CRS), change
    Problem stages   most 5xx, p95 well above last week, much quieter than last week
                     (the colour rules of config/ops-dashboard.yaml, over the whole day)
    Top stores       orders per store, change
    Alerts           alerts that started that day (per rule), and what is still failing now
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .ops import ZONES, zone_day

ZONE_NAMES = {"EST": "US Eastern", "PST": "US Pacific", "IST": "India", "UTC": "UTC"}
MAX_PROBLEMS = 8
MAX_STORES = 8
QUIET_MIN_CALLS = 1000          # a stage needs this many calls last week before "much quieter" counts


def _esc(text: Any) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _num(v: float | int | None) -> str:
    return "—" if v is None else f"{v:,.0f}"


def _compact(v: float | int | None) -> str:
    if v is None:
        return "—"
    if abs(v) >= 1e6:
        return f"{v / 1e6:.1f}M".replace(".0M", "M")
    return f"{v:,.0f}"


def _chg(now: float | None, before: float | None) -> str:
    if now is None or not before:
        return ""
    pct = 100.0 * (now - before) / before
    arrow = "▲" if pct > 0 else "▼" if pct < 0 else "±"
    return f"{arrow} {abs(pct):.0f}%" if abs(pct) >= 10 else f"{arrow} {abs(pct):.1f}%"


def _pct(part: float, whole: float, digits: int = 2) -> str:
    return f"{100.0 * part / whole:.{digits}f}%" if whole else "—"


def _clock(ms: int, zone: str) -> str:
    d = datetime.fromtimestamp(ms / 1000, ZoneInfo(ZONES[zone]))
    return f"{d:%H:%M} {d.tzname()}"


def _stamp(ms: int, zone: str) -> str:
    """Sun 15 Nov 00:00 EST"""
    d = datetime.fromtimestamp(ms / 1000, ZoneInfo(ZONES[zone]))
    return f"{d:%a %d %b %H:%M} {d.tzname()}"


def _both(ms: int, zone: str) -> str:
    """14:21 EST (00:51 IST); India days show Pacific as the second zone."""
    other = "PST" if zone == "IST" else "IST"
    return f"{_clock(ms, zone)} ({_clock(ms, other)})"


def problem_stages(cur: list[dict], lw: list[dict], th: dict) -> list[tuple[int, str, str, str]]:
    """(rank, icon, stage, why) for the stages that looked wrong over the whole day; worst first."""
    before = {s["id"]: s for s in lw}
    out = []
    for s in cur:
        calls = s["calls"]
        if calls < th["minCalls"]:
            continue
        b = before.get(s["id"])
        who = f"*{_esc(s['name'])}* ({_esc(s['api'])})"
        e5 = 100.0 * s["err5xx"] / calls
        if s["err5xx"] >= 10 and e5 >= th["err5xxAmberPct"]:
            red = e5 >= th["err5xxRedPct"]
            out.append((2 if red else 1, ":red_circle:" if red else ":large_orange_circle:", who,
                        f"5xx {e5:.2f}% ({_num(s['err5xx'])} errors)"))
            continue
        if b and s.get("p95") and b.get("p95") and s["p95"] >= th["p95MinMs"] and b["calls"] >= th["minCalls"]:
            x = s["p95"] / b["p95"]
            if x >= th["p95AmberTimes"]:
                red = x >= th["p95RedTimes"]
                out.append((2 if red else 1, ":red_circle:" if red else ":large_orange_circle:", who,
                            f"p95 {s['p95']:,.0f} ms, {x:.1f}× last week ({b['p95']:,.0f} ms)"))
                continue
    seen = {s["id"] for s in cur}
    for b in lw:                                          # busy last week, (almost) gone this week
        if b["calls"] < QUIET_MIN_CALLS:
            continue
        s = next((x for x in cur if x["id"] == b["id"]), None)
        calls = s["calls"] if s else 0
        if b["id"] not in seen or calls < b["calls"] * th["quietPct"] / 100:
            out.append((0, ":zzz:", f"*{_esc(b['name'])}* ({_esc(b['api'])})",
                        f"{_num(calls)} calls, {100 * calls / b['calls']:.0f}% of last week ({_num(b['calls'])})"))
    out.sort(key=lambda p: -p[0])
    return out


def alerts_recap(svc, start: int, end: int, st: dict, cfg: dict) -> tuple[str, int]:
    items = [i for i in svc.history(500) if start <= i.get("at", 0) < end and i.get("event") in ("firing", "escalated")]
    per: dict[str, int] = {}
    started = critical = 0
    for i in items:
        if i["event"] == "firing":
            started += 1
            per[i["ruleName"]] = per.get(i["ruleName"], 0) + 1
            if (i.get("level") or "critical") == "critical":
                critical += 1
        else:
            critical += 1
    rules = {r["id"] for r in (cfg.get("rules") or [])}
    firing = sum(1 for rid, rs in (st.get("rules") or {}).items() if rid in rules
                 for cur in (rs.get("subjects") or {}).values() if cur.get("state") == "firing")
    top = sorted(per.items(), key=lambda kv: -kv[1])[:4]
    line = (f"*Alerts:* {started} started" + (f" ({critical} critical)" if critical else "")
            + (" · most: " + ", ".join(f"{_esc(n)} ×{c}" for n, c in top) if top else "")
            + f" · {firing or 'nothing'} still failing now")
    return line, started


def build(svc, cfg: dict, day: date, zone: str, now: int) -> dict:
    ops = svc.ops
    z, lz = zone_day(day, zone), zone_day(day - timedelta(days=7), zone)
    s = ops.summary(day.isoformat(), 60, zone)
    a, o = s["api"], s["orders"]
    # a finished day is compared with last week's whole day (it may be 23 or 25 hours long)
    lw_calls = a.get("callsLastWeekDay")
    lw_orders = o.get("totalLastWeekDay")
    full = lambda x: sum(v or 0 for v in x["lastWeekHours"]) if x.get("lastWeekHours") is not None else None  # noqa: E731
    th = ops.config.get().status
    cur = ops.time_range(z.start, z.end, zone)["stages"]
    lw = ops.time_range(lz.start, lz.end, zone)["stages"]
    title = f"{day:%a %d %b}"
    lw_label = f"{day - timedelta(days=7):%a %d %b}"
    parts = [f":sunrise: *Daily ops summary · {title}* ({ZONE_NAMES[zone]} day, vs {lw_label})"]

    if a["calls"]:
        line = (f"*API calls:* {_num(a['calls'])}" + (f" · {_chg(a['calls'], lw_calls)}" if lw_calls else "")
                + f" · 4xx {_pct(a['err4xx'], a['calls'])} · 5xx {_pct(a['err5xx'], a['calls'])}")
        if a.get("peak"):
            line += f" · busiest minute {_num(a['peak']['calls'])}/min at {_both(a['peak']['t'], zone)}"
        parts.append(line)
    else:
        parts.append("*API calls:* no numbers for this day (is the copy job on 172.0.23.77 running?)")

    if o.get("ready"):
        parts.append(f"*Orders:* {_num(o['total'])}" + (f" · {_chg(o['total'], lw_orders)}" if lw_orders else "")
                     + "  —  " + " · ".join(f"{_esc(x['service'])} {_num(x['count'])}"
                                            + (f" ({_chg(x['count'], full(x))})" if full(x) else "")
                                            for x in o["services"]))
    else:
        parts.append("*Orders:* not counted for this day")

    probs = problem_stages(cur, lw, th)
    if probs:
        parts.append("*Problem stages*")
        parts += [f"• {icon} {who} — {why}" for _, icon, who, why in probs[:MAX_PROBLEMS]]
        if len(probs) > MAX_PROBLEMS:
            parts.append(f"…and {len(probs) - MAX_PROBLEMS} more")
    elif a["calls"]:
        parts.append("*Problem stages:* none — every stage looked like last week :white_check_mark:")

    stores = [x for x in (o.get("stores") or []) if x["count"]][:MAX_STORES]
    if stores:
        parts.append("*Top stores* (orders)")
        parts += [f"• {_esc(x['name'])} ({_esc(x['service'])}) {_num(x['count'])}"
                  + (f" · {_chg(x['count'], full(x))}" if full(x) else "") for x in stores]

    recap, started = alerts_recap(svc, z.start, z.end, svc.state(), cfg)
    parts.append(recap)

    base = (cfg.get("settings") or {}).get("consoleUrl")
    if base:
        parts.append(f"<{base}/ui/ops?tab=history&range=custom&from={z.start}&to={z.end}|Open this day in the Ops dashboard>")

    text = "\n".join(parts)
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": chunk}} for chunk in _chunks(text, 2900)]
    covers = f"Covers {_stamp(z.start, zone)} – {_stamp(z.end, zone)} ({_stamp(z.start, 'IST')} – {_stamp(z.end, 'IST')})"
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": covers + " · " + svc._footer(cfg, now, None)}]})
    line = (f"{_compact(a['calls'])} calls" + (f" ({_chg(a['calls'], lw_calls)})" if lw_calls else "")
            + (f", {_num(o['total'])} orders" + (f" ({_chg(o['total'], lw_orders)})" if lw_orders else "")
               if o.get("ready") else "")
            + f", {len(probs)} problem stage{'s' if len(probs) != 1 else ''}, {started} alerts")
    return {"text": f"Daily ops summary {title}: {line}", "blocks": blocks,
            "summary": {"title": f"{title} ({ZONE_NAMES[zone]})", "line": line}}


def _chunks(text: str, size: int) -> list[str]:
    out, cur = [], ""
    for ln in text.split("\n"):
        if cur and len(cur) + len(ln) + 1 > size:
            out.append(cur)
            cur = ln
        else:
            cur = f"{cur}\n{ln}" if cur else ln
    return out + ([cur] if cur else [])
