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
    bombas = next(x for x in o["stores"] if x["name"] == "Bombas")
    assert bombas["hours"][9] is None and len(bombas["lastWeekHours"]) == 24
