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
            out.append((2 if red else 1, "🔴" if red else "🟠", who,
                        f"5xx {e5:.2f}% ({_num(s['err5xx'])} errors)"))
            continue
        if b and s.get("p95") and b.get("p95") and s["p95"] >= th["p95MinMs"] and b["calls"] >= th["minCalls"]:
            x = s["p95"] / b["p95"]
            if x >= th["p95AmberTimes"]:
                red = x >= th["p95RedTimes"]
                out.append((2 if red else 1, "🔴" if red else "🟠", who,
                            f"p95 {s['p95']:,.0f} ms, {x:.1f}× last week ({b['p95']:,.0f} ms)"))
                continue
    seen = {s["id"] for s in cur}
    for b in lw:                                          # busy last week, (almost) gone this week
        if b["calls"] < QUIET_MIN_CALLS:
            continue
        s = next((x for x in cur if x["id"] == b["id"]), None)
        calls = s["calls"] if s else 0
        if b["id"] not in seen or calls < b["calls"] * th["quietPct"] / 100:
            out.append((0, "💤", f"*{_esc(b['name'])}* ({_esc(b['api'])})",
                        f"{_num(calls)} calls, {100 * calls / b['calls']:.0f}% of last week ({_num(b['calls'])})"))
    out.sort(key=lambda p: -p[0])
    return out


def alerts_recap(svc, start: int, end: int, st: dict, cfg: dict) -> dict:
    """Alerts that started in [start, end): how many, how many of them reached critical (at the start or
    later the same day), per rule; and how many subjects are failing now."""
    items = [i for i in svc.history(500) if start <= i.get("at", 0) < end and i.get("event") in ("firing", "escalated")]
    per: dict[str, int] = {}
    started = 0
    crit: set[tuple[str, str]] = set()
    for i in items:
        key = (i.get("ruleName", ""), i.get("subject", ""))
        if i["event"] == "firing":
            started += 1
            per[key[0]] = per.get(key[0], 0) + 1
            if (i.get("level") or "critical") == "critical":
                crit.add(key)
        else:
            crit.add(key)
    rules = {r["id"] for r in (cfg.get("rules") or [])}
    firing = sum(1 for rid, rs in (st.get("rules") or {}).items() if rid in rules
                 for cur in (rs.get("subjects") or {}).values() if cur.get("state") == "firing")
    return {"started": started, "critical": len(crit), "firing": firing,
            "top": sorted(per.items(), key=lambda kv: -kv[1])[:4]}


FLAGS = {"EST": "🇺🇸", "PST": "🇺🇸", "IST": "🇮🇳", "UTC": "🌐"}
MEDALS = ("🥇", "🥈", "🥉")
BAR_WIDTH = 20
_EIGHTHS = " ▏▎▍▌▋▊▉"


def _delta(now: float | None, before: float | None, plain: bool = False) -> str:
    """📈 +10% / 📉 −16% / ➖ 0% (Slack can't colour text, so the emoji carries the direction);
    plain: ▲ +10% / ▼ −16% for code blocks, where emoji would break the columns."""
    if now is None or not before:
        return ""
    pct = 100.0 * (now - before) / before
    size = f"{abs(pct):.0f}" if abs(pct) >= 10 else f"{abs(pct):.1f}"
    if size in ("0", "0.0"):
        return "= 0%" if plain else "➖ 0%"
    up, down = ("▲", "▼") if plain else ("📈", "📉")
    return f"{up} +{size}%" if pct > 0 else f"{down} −{size}%"


def _big(v: float | int | None) -> str:
    """8.70M · 12.3M · 82,625"""
    if v is None:
        return "—"
    if abs(v) >= 1e7:
        return f"{v / 1e6:.1f}M"
    if abs(v) >= 1e6:
        return f"{v / 1e6:.2f}M"
    return f"{v:,.0f}"


def _bar(v: float, top: float) -> str:
    """A bar of up to BAR_WIDTH characters, in eighths (a thin sliver for anything above zero)."""
    if top <= 0 or v <= 0:
        return ""
    eighths = max(1, round(8 * BAR_WIDTH * v / top))
    return "█" * (eighths // 8) + (_EIGHTHS[eighths % 8] if eighths % 8 else "")


def _field(title: str, value: str) -> dict:
    return {"type": "mrkdwn", "text": f"*{title}*\n{value}"}


def build(svc, cfg: dict, day: date, zone: str, now: int) -> dict:
    """Template A ("scoreboard"): header, six number boxes, orders per service as bars, top stores with
    medals, problem stages, alerts, buttons, and the covered times."""
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
    probs = problem_stages(cur, lw, th)
    rec = alerts_recap(svc, z.start, z.end, svc.state(), cfg)
    title = f"{day:%a %d %b}"
    lw_label = f"{day - timedelta(days=7):%a %d %b}"
    calls = a["calls"]
    ready = bool(o.get("ready"))
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": f"🌅 Daily Ops Report · {title}", "emoji": True}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"{FLAGS.get(zone, '')} {ZONE_NAMES[zone]} day · compared with {lw_label}"}]},
    ]

    # -- six boxes
    e5 = 100.0 * a["err5xx"] / calls if calls else None
    fields = [
        _field("📈 API calls", f"*{_big(calls)}*  {_delta(calls, lw_calls)}" if calls else "no numbers for this day"),
        _field("🛒 Orders", (f"*{_num(o['total'])}*  {_delta(o['total'], lw_orders)}"
                            + (" 🚀" if lw_orders and o["total"] >= 1.5 * lw_orders else "")) if ready else "not counted"),
        _field("⚠️ 4xx rate", f"*{_pct(a['err4xx'], calls)}*" if calls else "—"),
        _field("🔥 5xx rate", (f"*{_pct(a['err5xx'], calls)}*" + (" ✅" if e5 is not None and e5 < th["err5xxAmberPct"] else
                                                                  " 🔴" if e5 is not None and e5 >= th["err5xxRedPct"] else " 🟡"))
               if calls else "—"),
        _field("⚡ Busiest minute", f"{_num(a['peak']['calls'])}/min at {_both(a['peak']['t'], zone)}" if a.get("peak") else "—"),
        _field("🚨 Alerts", f"{rec['started']} started · *{rec['firing']} still failing*" if rec["firing"]
               else f"{rec['started']} started · nothing failing now ✅"),
    ]
    blocks.append({"type": "section", "fields": fields})
    if not calls:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "No API Gateway numbers for this day: is the copy job on 172.0.23.77 running?"}]})

    # -- orders per service, as bars
    if ready and o["services"]:
        svcs = sorted(o["services"], key=lambda x: -x["count"])
        top = max(x["count"] for x in svcs) or 1
        w_name = max(len(x["service"]) for x in svcs)
        w_num = max(len(_num(x["count"])) for x in svcs)
        rows = [f"{x['service']:<{w_name}}  {_bar(x['count'], top):<{BAR_WIDTH}}  {_num(x['count']):>{w_num}}  "
                + (_delta(x["count"], full(x), plain=True) or "") for x in svcs]
        blocks += [{"type": "divider"},
                   {"type": "section", "text": {"type": "mrkdwn", "text": "*🛍️ Orders by service*\n```\n" + "\n".join(r.rstrip() for r in rows) + "\n```"}}]

    # -- top stores
    stores = [x for x in (o.get("stores") or []) if x["count"]]
    if stores:
        podium = "   ".join(f"{MEDALS[i]} *{_esc(x['name'])}* {_num(x['count'])}" + (f" {_delta(x['count'], full(x))}" if full(x) else "")
                             for i, x in enumerate(stores[:3]))
        blocks += [{"type": "divider"},
                   {"type": "section", "text": {"type": "mrkdwn", "text": f"*🏆 Top stores*\n{podium}"}}]
        rest = stores[3:MAX_STORES]
        if rest:
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": " · ".join(
                f"{_esc(x['name'])} {_num(x['count'])}" + (f" {_delta(x['count'], full(x))}" if full(x) else "") for x in rest)}]})

    # -- problem stages and alerts
    blocks.append({"type": "divider"})
    if probs:
        lines = [f"• {icon} {who} — {why}" for _, icon, who, why in probs[:MAX_PROBLEMS]]
        if len(probs) > MAX_PROBLEMS:
            lines.append(f"…and {len(probs) - MAX_PROBLEMS} more")
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*🩺 Problem stages* (whole day vs last week)\n" + "\n".join(lines)}})
    head = "*🚨 Alerts*" + (f"   🔴 {rec['firing']} still failing" if rec["firing"] else "   ✅ nothing failing now")
    body = (f"{rec['started']} started" + (f" · {rec['critical']} reached critical" if rec["critical"] else "")
            + (" · most: " + " · ".join(f"{_esc(n)} ×{c}" for n, c in rec["top"]) if rec["top"] else ""))
    if not probs and calls:
        body += "\n✅ No problem stages: every stage looked like last week"
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"{head}\n{body}"}})

    # -- buttons (only when the console address is set on the Alerts page)
    base = (cfg.get("settings") or {}).get("consoleUrl")
    if base:
        blocks.append({"type": "actions", "elements": [
            {"type": "button", "action_id": "open_day", "style": "primary", "text": {"type": "plain_text", "text": "📊 Open the day", "emoji": True},
             "url": f"{base}/ui/ops?tab=history&range=custom&from={z.start}&to={z.end}"},
            {"type": "button", "action_id": "open_alerts", "text": {"type": "plain_text", "text": "🔔 Alerts page", "emoji": True},
             "url": f"{base}/ui/alerts"}]})
    covers = f"Covers {_stamp(z.start, zone)} – {_stamp(z.end, zone)} ({_stamp(z.start, 'IST')} – {_stamp(z.end, 'IST')}) · Vector Logs viewer"
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": covers}]})

    line = (f"{_big(calls)} calls" + (f" ({_delta(calls, lw_calls)})" if lw_calls else "")
            + (f", {_num(o['total'])} orders" + (f" ({_delta(o['total'], lw_orders)})" if lw_orders else "") if ready else "")
            + f", {len(probs)} problem stage{'s' if len(probs) != 1 else ''}, {rec['started']} alert{'s' if rec['started'] != 1 else ''}")
    return {"text": f"Daily Ops Report {title}: {line}", "blocks": blocks,
            "summary": {"title": f"{title} ({ZONE_NAMES[zone]})", "line": line}}


def as_text(blocks: list[dict]) -> str:
    """The blocks as plain text (the Alerts page preview)."""
    out = []
    for b in blocks:
        t = b["type"]
        if t == "header":
            out.append(b["text"]["text"].upper())
        elif t == "section":
            if b.get("text"):
                out.append(b["text"]["text"])
            if b.get("fields"):
                out.append("\n".join(f["text"].replace("\n", ": ", 1) for f in b["fields"]))
        elif t == "context":
            out.append(" · ".join(e["text"] for e in b["elements"]))
        elif t == "divider":
            out.append("─" * 40)
        elif t == "actions":
            out.append("   ".join(f"[{e['text']['text']}]" for e in b["elements"]))
    return "\n".join(out)
