"""Ops dashboard: API Gateway metric files + orders counted from the logs, against last week."""
from __future__ import annotations

import csv
import gzip
import io
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pytest
from fastapi.testclient import TestClient

from app.logs import LogService
from app.main import create_app
from app.ops import OpsService, judge, parse_config, OpsConfigError, DEFAULT_STATUS
from conftest import ROOT_H, make_settings

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config" / "ops-dashboard.yaml"
NOW = datetime(2026, 11, 9, 10, 30, 20, tzinfo=timezone.utc)
TODAY = NOW.date()
LW = TODAY - timedelta(days=7)
MS = lambda dt: int(dt.timestamp() * 1000)  # noqa: E731


def write_metrics(root: Path, day, upto: datetime | None, stages: dict) -> None:
    """stages: (api, stage) -> dict(calls, e4, e5, p95). One row per minute until `upto` (exclusive)."""
    for block in range(0, 24, 4):
        rows = []
        t = datetime(day.year, day.month, day.day, block, tzinfo=timezone.utc)
        for _ in range(240):
            if upto and t >= upto:
                break
            for (api, stage), v in stages.items():
                rows.append({"minute": t.strftime("%Y-%m-%dT%H:%MZ"), "api": api, "stage": stage, "calls": v["calls"],
                             "err_4xx": v["e4"], "err_5xx": v["e5"], "latency_p50_ms": v["p95"] / 2,
                             "latency_p95_ms": v["p95"], "integration_p95_ms": v["p95"] - 10})
            t += timedelta(minutes=1)
        if not rows:
            continue
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=list(rows[0]), lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
        p = root / "apigw-metrics" / f"dt={day:%Y-%m-%d}" / f"part-{block:02d}.csv.gz"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(gzip.compress(buf.getvalue().encode()))


def write_logs(root: Path, cluster: str, when: datetime, lines: list[tuple[datetime, str]], name="a") -> None:
    d = root / "vector" / cluster / f"dt={when:%Y-%m-%d}" / f"hour={when:%H}"
    d.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("CREATE TABLE t (log_time_ms BIGINT, msg VARCHAR, level VARCHAR)")
    con.executemany("INSERT INTO t VALUES (?, ?, 'INFO')", [(MS(t), m) for t, m in lines])
    con.execute(f"COPY t TO '{(d / f'{name}.parquet').as_posix()}' (FORMAT parquet)")
    con.close()


ROUTE = ("START :: com.fenixcommerce.oms.routing.routing.service.impl.OrderAllocationsBusinessServiceImpl.routeOrder() "
         "==> StoreName: dermalogica-us.myshopify.com , Create : false, OrderId : {}")
ROUTE_NO_ID = ("START :: com.fenixcommerce.oms.routing.routing.service.impl.OrderAllocationsBusinessServiceImpl.routeOrder() "
               "==> StoreName: dermalogica-us.myshopify.com , Create : false")
OMS11 = "getOrderRoutingInfo::Begin with tenantId: ad5643d2f6db419abd35178d12762b54, requestId: {}, orderId: {}"
RATE = "com.fenixcommerce.oms.rateshopping.service.impl.RateShoppingBusinessServiceImpl.rateShop() ==> StoreName: {} , OrderId : {}"
INSTANT = "InstantRateShoppingBusinessService::processOrder for Store: {}, order: {}"

FELINA = ("Alpha-EDDS-1", "felina")
VUORI = ("Vuori", "vuori")


@pytest.fixture()
def data(tmp_path) -> Path:
    root = tmp_path / "logs"
    # today until 10:26 (the copy job is a few minutes behind); vuori has 5% 5xx today
    write_metrics(root, TODAY, datetime(2026, 11, 9, 10, 26, tzinfo=timezone.utc),
                  {FELINA: dict(calls=10, e4=1, e5=0, p95=100), VUORI: dict(calls=100, e4=0, e5=5, p95=200)})
    write_metrics(root, LW, None, {FELINA: dict(calls=10, e4=1, e5=0, p95=100), VUORI: dict(calls=100, e4=0, e5=0, p95=200)})
    h9 = datetime(2026, 11, 9, 9, 0, tzinfo=timezone.utc)
    write_logs(root, "alpha-oms-routing", h9, [
        (h9 + timedelta(minutes=1), ROUTE.format("111")),
        (h9 + timedelta(minutes=1, seconds=1), ROUTE_NO_ID),
        (h9 + timedelta(minutes=5), ROUTE.format("111")),          # same order again
        (h9 + timedelta(minutes=7), ROUTE.format("222")),
    ])
    write_logs(root, "alpha-oms-routing", h9, [(h9 + timedelta(minutes=0, seconds=30), ROUTE.format("222"))], name="b")  # earlier, other file
    write_logs(root, "alpha-oms-routing-11", h9, [
        (h9 + timedelta(minutes=2), OMS11.format("53166f77-aaaa", "6334048174213")),
        (h9 + timedelta(minutes=3), OMS11.format("test", "null")),  # health check
    ])
    write_logs(root, "bombas-crs-1", h9, [
        (h9 + timedelta(minutes=10), RATE.format("bombas.myshopify.com", "10091104764258")),
        (h9 + timedelta(minutes=10, seconds=1), RATE.format("bombas.myshopify.com", "10091104764258")),
        (h9 + timedelta(minutes=11), INSTANT.format("bombas.myshopify.com", "10091104764258")),
        (h9 + timedelta(minutes=12), INSTANT.format("felinagroup.myshopify.com", "114-2331636-2065033")),
        (h9 + timedelta(minutes=30), "some other line"),
    ])
    lw9 = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
    write_logs(root, "alpha-oms-routing", lw9, [(lw9 + timedelta(minutes=3), ROUTE.format("900")),
                                                (lw9 + timedelta(hours=1), ROUTE.format("901"))])  # after the cut-off
    return root


@pytest.fixture()
def ops(tmp_path, data) -> OpsService:
    cfg = tmp_path / "ops-dashboard.yaml"
    shutil.copy(REPO_CONFIG, cfg)
    s = make_settings(tmp_path, data, ops_config_file=str(cfg))
    svc = OpsService(s, LogService(s), now=lambda: NOW)
    svc.orders.run_once(days_back=7)
    return svc


def test_repo_config_parses():
    cfg = parse_config(REPO_CONFIG.read_text())
    assert [s.name for s in cfg.services] == ["OMS", "OMS-11", "CRS"]
    assert cfg.tenants["ad5643d2f6db419abd35178d12762b54"] == "stevemadden"
    assert cfg.name("bombas.myshopify.com") == "Bombas"
    assert cfg.name("other.myshopify.com") == "other"


@pytest.mark.parametrize("text,msg", [
    ("orders: [{service: X}]", "needs service: and cluster:"),
    ("orders: [{service: X, cluster: c, lines: [{contains: a, pattern: '(?P<store>x)'}]}]", "named groups"),
    ("orders: [{service: X, cluster: c, lines: [{contains: a, pattern: '(?P<order>x'}]}]", "not a valid regular"),
    ("status: {nope: 1}", "unknown setting"),
])
def test_config_mistakes(text, msg):
    with pytest.raises(OpsConfigError, match=msg):
        parse_config(text)


def test_api_summary(ops):
    s = ops.summary()
    api = s["api"]
    minutes = 10 * 60 + 26                         # 00:00 … 10:25
    assert api["asOf"] == MS(datetime(2026, 11, 9, 10, 25, tzinfo=timezone.utc))
    assert api["calls"] == 110 * minutes
    assert api["callsLastWeek"] == 110 * minutes     # same time last week, not the whole day
    assert api["change"] == 0
    assert api["err5xx"] == 5 * minutes
    assert not api["stale"]
    m = api["minutes"]
    assert m["calls"][0] == 110 and m["calls"][minutes - 1] == 110 and m["calls"][minutes] is None
    assert m["lastWeek"][1439] == 110                # last week's whole day for the chart
    st = {x["id"]: x for x in api["stages"]}
    assert st["Vuori/vuori"]["status"] == "red" and "5xx 5.0%" in st["Vuori/vuori"]["reasons"][0]
    assert st["Alpha-EDDS-1/felina"]["status"] == "green"
    assert st["Alpha-EDDS-1/felina"]["recent"]["p95"] == 100
    assert api["stages"][0]["id"] == "Vuori/vuori"   # busiest first


def test_orders_counted_once_per_service(ops):
    o = ops.summary()["orders"]
    assert o["ready"]
    svc = {x["service"]: x["count"] for x in o["services"]}
    # OMS: 111, 222 · OMS-11: one (health check skipped) · CRS: bombas order once + felina
    assert svc == {"OMS": 2, "OMS-11": 1, "CRS": 2}
    assert o["total"] == 5
    stores = {(x["service"], x["name"]): x["count"] for x in o["stores"]}
    assert stores[("OMS-11", "Steve Madden")] == 1
    assert stores[("CRS", "Bombas")] == 1 and stores[("CRS", "Felina")] == 1
    # last week until the same minute: only order 900 (901 came later in the day)
    assert o["totalLastWeek"] == 1 and o["change"] == 400.0
    # first seen: 222 at 09:00:30 (the earlier file), 111 at 09:01
    start = MS(datetime(2026, 11, 9, tzinfo=timezone.utc))
    counts = o["minutes"]["counts"]
    assert counts[9 * 60] == 1 + 0 and counts[9 * 60 + 1] == 1
    assert o["minutes"]["lastWeek"][9 * 60 + 3] == 1 and o["minutes"]["lastWeek"][10 * 60] == 1
    assert o["asOf"] == MS(datetime(2026, 11, 9, 9, 30, tzinfo=timezone.utc))
    assert o["minutes"]["start"] == start


def test_new_files_only_and_recount_on_config_change(ops, data, tmp_path):
    h9 = datetime(2026, 11, 9, 9, 0, tzinfo=timezone.utc)
    st = ops.orders.load(TODAY)
    assert len(st["services"]["OMS"]["files"]) == 2
    write_logs(data, "alpha-oms-routing", h9, [(h9 + timedelta(minutes=40), ROUTE.format("333")),
                                               (h9 + timedelta(minutes=41), ROUTE.format("111"))], name="c")
    ops.orders.logs._lists = type(ops.orders.logs._lists)()     # the folder listing is cached 30 s
    ops.orders.run_once(days_back=0)
    oms = ops.summary()["orders"]["services"][0]
    assert oms["service"] == "OMS" and oms["count"] == 3          # 333 is new, 111 was counted already
    assert len(ops.orders.load(TODAY)["services"]["OMS"]["files"]) == 3
    # drop CRS from the config: its counts go, the others stay
    cfg = Path(ops.config.path)
    text = cfg.read_text()
    cfg.write_text(text[:text.index("  - service: CRS")] + text[text.index("# Lines that name a tenant"):])
    ops.config._checked = 0
    ops.orders.run_once(days_back=0)
    o = ops.summary()["orders"]
    assert {x["service"] for x in o["services"]} == {"OMS", "OMS-11"} and o["total"] == 4


def test_health_worst_first(ops):
    h = ops.health(60)
    assert [s["id"] for s in h["stages"]] == ["Vuori/vuori", "Alpha-EDDS-1/felina"]
    v = h["stages"][0]
    assert v["now"]["calls"] == 6000 and v["now"]["err5xxPct"] == 5.0 and v["lastWeek"]["err5xxPct"] == 0
    assert v["now"]["gatewayMs"] == 10
    assert h["lastWeekAvailable"]


def test_history_one_stage_and_store(ops):
    h = ops.history(stage="Alpha-EDDS-1/felina", service="CRS", store="bombas.myshopify.com")
    assert h["api"]["calls"][0] == 10 and h["api"]["p95"][0] == 100 and h["api"]["calls"][1000] is None  # not there yet
    assert h["api"]["shownMinutes"] == 626 and h["orders"]["minutes"][11 * 60] is None
    assert h["api"]["lastWeek"]["calls"][1000] == 10
    assert h["orders"]["total"] == 1 and h["orders"]["minutes"][9 * 60 + 10] == 1
    assert {s["id"] for s in h["stages"]} == {"Alpha-EDDS-1/felina", "Vuori/vuori"}
    assert h["stores"][0]["count"] >= 1


def test_past_day_is_whole_day(ops):
    s = ops.summary(day=LW.isoformat())
    assert s["api"]["calls"] == 110 * 1440 and s["api"]["callsLastWeek"] is None
    assert s["orders"]["total"] == 2


def test_judge_rules():
    th = dict(DEFAULT_STATUS)
    assert judge({"calls": 5, "err4xx": 0, "err5xx": 5}, {"calls": 5, "err4xx": 0}, th) == ("idle", [])
    assert judge({"calls": 0, "err4xx": 0, "err5xx": 0}, {"calls": 500, "err4xx": 0}, th)[0] == "red"
    assert judge({"calls": 200, "err4xx": 0, "err5xx": 0}, {"calls": 1000, "err4xx": 0}, th)[0] == "amber"   # quiet
    assert judge({"calls": 1000, "err4xx": 0, "err5xx": 0, "p95": 900}, {"calls": 1000, "err4xx": 0, "p95": 300}, th)[0] == "red"
    assert judge({"calls": 1000, "err4xx": 0, "err5xx": 0, "p95": 250}, {"calls": 1000, "err4xx": 0, "p95": 100}, th)[0] == "green"  # under 300 ms
    assert judge({"calls": 1000, "err4xx": 300, "err5xx": 0}, {"calls": 1000, "err4xx": 50}, th)[0] == "amber"


def test_routes(tmp_path, data):
    cfg = tmp_path / "ops-dashboard.yaml"
    shutil.copy(REPO_CONFIG, cfg)
    app = create_app(make_settings(tmp_path, data, ops_config_file=str(cfg)))
    app.state.ops.now = lambda: NOW
    app.state.ops.orders.now = lambda: NOW
    app.state.ops.orders.run_once(days_back=7)
    c = TestClient(app)
    r = c.get("/api/v1/ops/summary", headers=ROOT_H)
    assert r.status_code == 200 and r.json()["orders"]["total"] == 5
    assert c.get("/api/v1/ops/health?minutes=30", headers=ROOT_H).json()["minutes"] == 30
    assert c.get("/api/v1/ops/history?stage=Vuori/vuori", headers=ROOT_H).status_code == 200
    assert c.get("/api/v1/ops/summary?day=2030-01-01", headers=ROOT_H).json()["error"]["code"] == "INVALID_DAY"
    assert c.get("/api/v1/ops/history?service=NOPE", headers=ROOT_H).json()["error"]["code"] == "UNKNOWN_SERVICE"
    assert c.get("/api/v1/ops/status", headers=ROOT_H).json()["config"]["exists"]
    assert c.get("/api/v1/ops/summary").status_code == 401


def test_new_shapes_for_the_ui(ops):
    s = ops.summary()
    st = {x["id"]: x for x in s["api"]["stages"]}
    v = st["Vuori/vuori"]
    # 00:00 … 10:25 known: 41 full 15-minute buckets (10:15–10:30 is still running)
    assert len(v["spark"]["now"]) == 96 and v["spark"]["now"][0] == 1500 and v["spark"]["now"][40] == 1500
    assert v["spark"]["now"][41] is None and v["spark"]["lastWeek"][95] == 1500
    assert round(sum(x["share"] for x in st.values())) == 100
    assert s["api"]["peak"]["calls"] == 110 and s["api"]["callsLastWeekDay"] == 110 * 1440
    o = s["orders"]
    crs = next(x for x in o["services"] if x["service"] == "CRS")
    assert crs["hours"][:9] == [0] * 9 and crs["hours"][9] is None   # counted to 09:30: the hour in progress is left out
    assert o["totalLastWeekDay"] == 2
    per = o["minutes"]["services"]
    assert set(per) == {"OMS", "OMS-11", "CRS"}
    assert [sum((arr[i] or 0) for arr in per.values()) for i in range(len(o["minutes"]["counts"]))] == [v or 0 for v in o["minutes"]["counts"]]
    bombas = next(x for x in o["stores"] if x["name"] == "Bombas")
    assert bombas["hours"][9] is None and len(bombas["lastWeekHours"]) == 24


# ------------------------------------------------------------------ days in IST / PST / EST
from datetime import date  # noqa: E402

from app.ops import zone_day  # noqa: E402


def test_zone_days_follow_daylight_saving():
    assert zone_day(date(2026, 11, 9), "UTC").start == MS(datetime(2026, 11, 9, tzinfo=timezone.utc))
    ist = zone_day(date(2026, 11, 9), "ist")
    assert ist.start == MS(datetime(2026, 11, 8, 18, 30, tzinfo=timezone.utc)) and ist.minutes == 1440
    assert ist.utc_days() == [date(2026, 11, 8), date(2026, 11, 9)]
    pst = zone_day(date(2026, 11, 9), "PST")
    assert pst.start == MS(datetime(2026, 11, 9, 8, tzinfo=timezone.utc))      # PST = UTC − 8 in November
    assert zone_day(date(2026, 7, 1), "PST").start == MS(datetime(2026, 7, 1, 7, tzinfo=timezone.utc))  # PDT in summer
    assert zone_day(date(2026, 11, 9), "EST").start == MS(datetime(2026, 11, 9, 5, tzinfo=timezone.utc))
    assert zone_day(date(2026, 11, 1), "PST").minutes == 1500                  # clocks went back: 25 hours
    assert zone_day(date(2026, 3, 8), "EST").minutes == 1380                   # clocks went forward: 23 hours


def test_summary_in_pst(ops):
    s = ops.summary(zone="PST")
    assert s["zone"] == "PST" and s["day"] == "2026-11-09"          # 02:30 in the morning in California
    api = s["api"]
    start = MS(datetime(2026, 11, 9, 8, tzinfo=timezone.utc))
    assert api["minutes"]["start"] == start and s["dayStart"] == start
    assert api["calls"] == 110 * 146                                # 08:00–10:26 UTC = 00:00–02:26 PST
    assert api["callsLastWeek"] == 110 * 146                        # 2 Nov 00:00–02:26 PST
    assert api["minutes"]["calls"][0] == 110 and api["minutes"]["calls"][146] is None
    o = s["orders"]
    assert o["total"] == 5 and o["minutes"]["start"] == start
    assert o["minutes"]["counts"][60] == 1                          # 222 first seen 09:00:30 UTC = 01:00 PST
    assert o["totalLastWeek"] == 1                                  # 900 at 2 Nov 09:03 UTC; 901 came after the cut-off


def test_summary_in_ist_and_bad_zone(ops):
    s = ops.summary(zone="IST")
    assert s["day"] == "2026-11-09"
    m = s["api"]["minutes"]
    assert m["start"] == MS(datetime(2026, 11, 8, 18, 30, tzinfo=timezone.utc))
    assert m["calls"][329] == 0 and m["calls"][330] == 110          # the copies start at 00:00 UTC = 05:30 IST
    assert s["api"]["calls"] == 110 * 626
    with pytest.raises(Exception) as e:
        ops.summary(zone="CET")
    assert getattr(e.value, "code", "") == "INVALID_ZONE"


def test_orders_across_two_utc_days(ops, data):
    """An IST day spans two UTC days: an order seen on both counts once, at its first sighting."""
    late = datetime(2026, 11, 8, 23, 0, tzinfo=timezone.utc)
    write_logs(data, "alpha-oms-routing", late, [(late + timedelta(minutes=50), ROUTE.format("555"))], name="x")
    early = datetime(2026, 11, 9, 0, 0, tzinfo=timezone.utc)
    write_logs(data, "alpha-oms-routing", early, [(early + timedelta(minutes=10), ROUTE.format("555")),
                                                  (early + timedelta(minutes=20), ROUTE.format("666"))], name="y")
    ops.orders.logs._lists = type(ops.orders.logs._lists)()
    ops.orders.path(date(2026, 11, 8)).unlink()                     # 8 Nov was final already: count it again
    ops.orders.run_once(days_back=7)
    ist = ops.summary(zone="IST")["orders"]
    oms = next(x for x in ist["services"] if x["service"] == "OMS")
    assert oms["count"] == 4                                         # 111, 222, 555 (once), 666
    assert ist["minutes"]["counts"][(23 * 60 + 50) - (18 * 60 + 30)] == 1   # 555 at 23:50 UTC = 05:20 IST
    utc = ops.summary()["orders"]
    assert next(x for x in utc["services"] if x["service"] == "OMS")["count"] == 4   # UTC 9 Nov: 111, 222, 555, 666
    pst8 = ops.summary(day="2026-11-08", zone="PST")["orders"]       # 8 Nov PST = 8 Nov 08:00 – 9 Nov 08:00 UTC
    assert next(x for x in pst8["services"] if x["service"] == "OMS")["count"] == 2  # 555, 666


def test_history_in_est_and_routes(ops, tmp_path, data):
    h = ops.history(zone="EST")
    assert h["zone"] == "EST" and h["api"]["start"] == MS(datetime(2026, 11, 9, 5, tzinfo=timezone.utc))
    assert h["api"]["shownMinutes"] == 326 and h["api"]["calls"][0] == 110 and h["api"]["calls"][326] is None
    assert len(h["api"]["calls"]) == 1440 and h["orders"]["total"] == 5
    past = ops.history(day="2026-11-01", zone="PST")                 # 25 hours long
    assert len(past["api"]["calls"]) == 1500 and past["dayEnd"] - past["dayStart"] == 25 * 3600_000
    cfg = tmp_path / "ops-dashboard.yaml"
    app = create_app(make_settings(tmp_path, data, ops_config_file=str(cfg)))
    app.state.ops.now = lambda: NOW
    c = TestClient(app)
    assert c.get("/api/v1/ops/summary?zone=pst", headers=ROOT_H).json()["zone"] == "PST"
    assert c.get("/api/v1/ops/history?zone=EST", headers=ROOT_H).json()["zone"] == "EST"
    assert c.get("/api/v1/ops/summary?zone=XYZ", headers=ROOT_H).json()["error"]["code"] == "INVALID_ZONE"


# ------------------------------------------------------------------ any time range (History tab)
def test_range_table_series_and_orders(ops):
    a, b = datetime(2026, 11, 9, 9, 0, tzinfo=timezone.utc), datetime(2026, 11, 9, 9, 30, tzinfo=timezone.utc)
    r = ops.time_range(MS(a), MS(b), "IST")
    assert r["step"] == 60_000 and r["buckets"] == 30 and r["start"] == MS(a)
    st = {x["id"]: x for x in r["stages"]}
    v = st["Vuori/vuori"]
    assert v["calls"] == 3000 and v["err5xxPct"] == 5.0 and v["p95"] == 200 and v["integrationP95"] == 190 and v["gatewayMs"] == 10
    assert st["Alpha-EDDS-1/felina"]["err4xxPct"] == 10.0 and r["stages"][0]["id"] == "Vuori/vuori"
    assert round(sum(x["share"] for x in r["stages"])) == 100
    assert r["api"]["calls"] == 3300 and r["api"]["series"]["calls"][0] == 110 and r["api"]["p95"] is not None
    o = r["orders"]
    assert o["counted"] and o["total"] == 5 and {x["service"]: x["count"] for x in o["services"]} == {"OMS": 2, "OMS-11": 1, "CRS": 2}
    one = ops.time_range(MS(a), MS(b), "IST", stage="Alpha-EDDS-1/felina", service="CRS", store="bombas.myshopify.com")
    assert one["api"]["calls"] == 300 and len(one["stages"]) == 2           # the table keeps every stage
    assert one["orders"]["total"] == 1 and sum(v or 0 for v in one["orders"]["series"]) == 1
    # one line per service for the chart; together they make the total line
    ss = o["serviceSeries"]
    assert set(ss) == {"OMS", "OMS-11", "CRS"} and {k: sum(v or 0 for v in arr) for k, arr in ss.items()} == {"OMS": 2, "OMS-11": 1, "CRS": 2}
    assert [sum(arr[i] or 0 for arr in ss.values()) for i in range(30)] == [v or 0 for v in o["series"]]
    assert list(one["orders"]["serviceSeries"]) == ["CRS"]


def test_range_long_buckets_and_limits(ops):
    end = NOW
    r = ops.time_range(MS(end - timedelta(days=7)), MS(end), "PST")     # 7 days → 15-minute buckets
    assert r["step"] == 15 * 60_000
    assert r["start"] % (15 * 60_000) == 0
    calls = r["api"]["series"]["calls"]
    assert calls[-1] is None or calls[-1] >= 0                          # after the newest minute: empty
    assert r["api"]["calls"] == 110 * 810 + 110 * 626                  # 2 Nov from 10:30 + today so far
    assert r["orders"]["missingDays"] == [] and r["orders"]["total"] == 5   # 900 and 901 (2 Nov 09:03, 10:03) are before the start
    wk = ops.time_range(MS(end - timedelta(days=20)), MS(end), "IST")
    assert wk["step"] == 3600_000 and wk["orders"]["missingDays"]       # not counted that far back
    with pytest.raises(Exception) as e:
        ops.time_range(MS(end - timedelta(days=32)), MS(end))
    assert e.value.code == "RANGE_TOO_LARGE"
    with pytest.raises(Exception) as e:
        ops.time_range(MS(end), MS(end - timedelta(hours=1)))
    assert e.value.code == "INVALID_RANGE"
    ist_hours = ops.time_range(MS(end - timedelta(days=20)), MS(end), "IST")["start"]
    assert (ist_hours + 330 * 60_000) % 3600_000 == 0                   # hourly buckets start on IST hours


def test_range_route(tmp_path, data):
    cfg = tmp_path / "ops-dashboard.yaml"
    shutil.copy(REPO_CONFIG, cfg)
    app = create_app(make_settings(tmp_path, data, ops_config_file=str(cfg)))
    app.state.ops.now = lambda: NOW
    c = TestClient(app)
    r = c.get("/api/v1/ops/range?from=2026-11-09T09:00:00Z&to=2026-11-09T10:00:00Z&zone=EST", headers=ROOT_H)
    assert r.status_code == 200 and r.json()["api"]["calls"] == 110 * 60 and r.json()["zone"] == "EST"
    ms = MS(datetime(2026, 11, 9, 9, tzinfo=timezone.utc))
    assert c.get(f"/api/v1/ops/range?from={ms}&to={ms + 600_000}", headers=ROOT_H).json()["api"]["calls"] == 1100
    assert c.get("/api/v1/ops/range?from=nope&to=1", headers=ROOT_H).json()["error"]["code"] == "INVALID_TIME"
    assert c.get("/api/v1/ops/range?from=200000&to=100000", headers=ROOT_H).json()["error"]["code"] == "INVALID_RANGE"


def test_range_leaves_out_partial_buckets(ops):
    r = ops.time_range(MS(datetime(2026, 11, 9, 0, 7, tzinfo=timezone.utc)), MS(NOW), "UTC")   # 10 h → 5-minute buckets
    assert r["step"] == 300_000 and r["start"] == MS(datetime(2026, 11, 9, 0, 5, tzinfo=timezone.utc))
    calls = r["api"]["series"]["calls"]
    assert calls[0] is None and calls[1] == 550                       # 00:05–00:10 only partly in the range
    last = (MS(datetime(2026, 11, 9, 10, 25, tzinfo=timezone.utc)) - r["start"]) // 300_000
    assert calls[last] is None and calls[last - 1] == 550             # data to 10:25: that bucket is not over yet
    assert r["api"]["series"]["p95"][0] == r["api"]["series"]["p95"][1]   # averages are kept
    assert r["api"]["calls"] == 110 * (626 - 7)                       # the totals count every minute


# ------------------------------------------------------------------ inbound orders (elkop2, 5-minute windows)
def write_inbound(root: Path, windows: dict, prefix: str = "es-order-counts/inbound") -> None:
    """windows: datetime (window start) -> {tenant: orders}; an _all row is added for each window."""
    blocks: dict[str, list[dict]] = {}
    for t, tenants in sorted(windows.items()):
        m = t.strftime("%Y-%m-%dT%H:%MZ")
        key = f"dt={t:%Y-%m-%d}/part-{t.hour - t.hour % 4:02d}.csv.gz"
        rows = blocks.setdefault(key, [])
        rows.append({"minute": m, "tenant": "_all", "orders": sum(tenants.values()), "docs": "", "span_min": 5, "src": "live"})
        rows += [{"minute": m, "tenant": tn, "orders": n, "docs": "", "span_min": 5, "src": "live"} for tn, n in tenants.items() if n]
    for key, rows in blocks.items():
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=["minute", "tenant", "orders", "docs", "span_min", "src"], lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
        p = root / prefix / key
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(gzip.compress(buf.getvalue().encode()))


def windows(day, upto: datetime | None, per: dict):
    t = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    end = upto or t + timedelta(days=1)
    out = {}
    while t < end:
        out[t] = dict(per)
        t += timedelta(minutes=5)
    return out


@pytest.fixture()
def inbound(data):
    # today: windows 00:00 … 10:20 (the 10:25 window is written at 10:30:0x, just after NOW's file copy)
    write_inbound(data, {**windows(TODAY, datetime(2026, 11, 9, 10, 25, tzinfo=timezone.utc),
                                   {"vuori-clothing.myshopify.com": 3, "dolls-test.myshopify.com": 1}),
                         **windows(LW, None, {"vuori-clothing.myshopify.com": 2, "kuiu-shop.myshopify.com": 1})})
    return data


def test_inbound_summary_like_the_orders_card(ops, inbound):
    ib = ops.summary(zone="UTC")["inbound"]
    assert ib["ready"] and ib["lastWeekReady"] and not ib["stale"]
    assert ib["asOf"] == MS(datetime(2026, 11, 9, 10, 24, tzinfo=timezone.utc))      # the 10:20 window ends 10:25
    n_win = 10 * 12 + 5                                                                  # 00:00 … 10:20 = 125 windows
    assert ib["total"] == 4 * n_win and ib["totalLastWeek"] == 3 * n_win and ib["totalLastWeekDay"] == 3 * 288
    assert ib["change"] == round(100 * (4 / 3 - 1), 1)
    m = ib["minutes"]
    assert m["step"] == 60_000 and m["counts"][0] == 4 and m["counts"][1] == 0 and m["counts"][5] == 4
    assert m["counts"][624] == 0 and m["counts"][625] is None                           # shown up to 10:24
    assert sum(v or 0 for v in m["counts"]) == ib["total"] and len(m["lastWeek"]) == 1440
    names = {s["store"]: s for s in ib["stores"]}
    assert names["vuori-clothing.myshopify.com"]["name"] == "Vuori" and names["dolls-test.myshopify.com"]["name"] == "Dolls Test"
    v = names["vuori-clothing.myshopify.com"]
    assert v["count"] == 3 * n_win and v["lastWeek"] == 2 * n_win and v["hours"][9] == 36 and v["hours"][10] is None
    assert names["kuiu-shop.myshopify.com"]["count"] == 0 and names["kuiu-shop.myshopify.com"]["lastWeek"] == n_win
    assert ib["stores"][0]["store"] == "vuori-clothing.myshopify.com"


def test_inbound_exclude_and_missing(ops, inbound, tmp_path):
    cfg = Path(ops.config.path)
    cfg.write_text(cfg.read_text().replace("inbound:\n  exclude: []", "inbound:\n  exclude: ['test']"))
    ops.config._checked = 0
    ib = ops.summary(zone="UTC")["inbound"]
    assert "dolls-test.myshopify.com" not in {s["store"] for s in ib["stores"]} and ib["total"] == 3 * 125
    # a day with no inbound files: not ready (the dashboard says "no data yet")
    assert ops.summary(day="2026-11-05", zone="UTC")["inbound"]["ready"] is False


def test_inbound_in_ist_and_in_a_range(ops, inbound):
    ib = ops.summary(zone="IST")["inbound"]                       # IST day 9 Nov = 8 Nov 18:30 UTC … : starts at 00:00 UTC here
    assert ib["ready"] and ib["total"] == 4 * 125
    r = ops.time_range(MS(datetime(2026, 11, 9, 9, 0, tzinfo=timezone.utc)), MS(datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)), "UTC")
    i = r["inbound"]
    assert r["step"] == 60_000 and i["step"] == 300_000 and len(i["series"]) == 12        # never finer than a window
    assert i["series"] == [4] * 12 and i["total"] == 48
    assert {t["tenant"]: t["count"] for t in i["tenants"]} == {"vuori-clothing.myshopify.com": 36, "dolls-test.myshopify.com": 12}
    # up to now: the 10:25 window is not counted yet, so the last bucket of a 15-minute range is left out
    r = ops.time_range(MS(datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)), MS(NOW), "UTC")
    assert r["inbound"]["series"][:5] == [4, 4, 4, 4, 4] and r["inbound"]["series"][5:] == [None, None]
    one = ops.time_range(MS(datetime(2026, 11, 9, 9, 0, tzinfo=timezone.utc)), MS(datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)),
                         "UTC", store="dolls-test.myshopify.com")
    assert one["inbound"]["series"] == [1] * 12 and one["inbound"]["total"] == 12


def test_tenant_names():
    cfg = parse_config("names:\n  vuori-clothing.myshopify.com: Vuori\n")
    assert cfg.tenant_name("vuori-clothing.myshopify.com") == "Vuori"
    assert cfg.tenant_name("buck-mason-usa.myshopify.com") == "Buck Mason Usa"
    assert cfg.tenant_name("talons.com") == "Talons" and cfg.tenant_name("zenpatient") == "Zenpatient"
    with pytest.raises(OpsConfigError, match="exclude"):
        parse_config("inbound:\n  exclude: ['(']\n")
    with pytest.raises(OpsConfigError, match="unknown setting"):
        parse_config("inbound:\n  skip: []\n")


def test_inbound_from_two_clusters(ops, inbound):
    # elkop3 (Bombas) one window behind elkop2: the card waits for it, so no window is half counted
    write_inbound(inbound, windows(TODAY, datetime(2026, 11, 9, 10, 20, tzinfo=timezone.utc), {"bombas.myshopify.com": 10}),
                  prefix="es-order-counts/elkop3/inbound")
    ib = ops.summary(zone="UTC")["inbound"]
    assert ib["asOf"] == MS(datetime(2026, 11, 9, 10, 19, tzinfo=timezone.utc))
    assert ib["total"] == 4 * 124 + 10 * 124 and ib["stores"][0]["store"] == "bombas.myshopify.com"
    assert ib["totalLastWeek"] == 3 * 124                                            # elkop3 has no last week yet
    assert "elkop3" in ib["source"]
    r = ops.time_range(MS(datetime(2026, 11, 9, 9, 0, tzinfo=timezone.utc)), MS(datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)), "UTC")
    assert r["inbound"]["series"] == [14] * 12 and r["inbound"]["total"] == 14 * 12
