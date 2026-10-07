"""Synthetic Application Load Balancer access logs, laid out like the S3 bucket.

    python local-test/make_alb_logs.py [--hours 6] [--out local-test/logs/alb-bucket]

Writes gzip files named and formatted like AWS's (one per load balancer node and 5 minutes)
under <out>/loadbalancer-logs/AWSLogs/111122223333/elasticloadbalancing/us-west-2/yyyy/mm/dd/.
The lines are invented (no real IPs, stores or traces). Tests use generate() and its returned
list of what was written.

Real files downloaded from the bucket into local-test/logs/alb/ are copied into the same
layout too (that folder is git-ignored), so they show up next to the invented ones.
"""
from __future__ import annotations

import argparse
import gzip
import random
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

ACCOUNT = "111122223333"
REGION = "us-west-2"
PREFIX = "loadbalancer-logs/"

# load balancer -> (id, nodes, [(target group, domain, weight, [paths])])
LBS = {
    "elb-demo-prepurchase": ("1a14a793ecf89f3e", 3, [
        ("tg-demo-edds-1", "edds.example.com", 5, ["POST /fenixdelest/api/v3/deliveryestimates",
                                                   "GET /fenixdelest/api/v1/{store}/storeinfo",
                                                   "OPTIONS /fenixdelest/api/v3/deliveryestimates"]),
        ("tg-demo-edds-2", "edds.example.com", 5, ["POST /fenixdelest/api/v3/deliveryestimates",
                                                   "GET /fenixdelest/api/v1/{store}/storeinfo"]),
        ("tg-demo-dp-1", "dp.example.com", 1, ["GET /dp/api/v1/orders/{n}", "POST /dp/api/v1/orders"]),
    ]),
    "elb-demo-postpurchase": ("76f1ce31a00a661f", 2, [
        ("tg-demo-ib-01", "ib.example.com", 4, ["POST /inbound/api/v1/{store}/order/created"]),
        ("tg-webhook-01", "hooks.example.com", 2, ["POST /hooks/api/v1/{store}/fulfillmentEvents/created"]),
    ]),
}
STORES = ["alpha-shop.myshopify.com", "beta-store.myshopify.com", "gamma.myshopify.com"]
AGENTS = ["Shopify-Captain-Hook", "AmazonAPIGateway_abc123", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Demo/1.0"]


@dataclass
class Written:
    lb: str
    tg: str | None
    elb_code: int
    tgt_code: int | None
    ts: datetime
    path: str
    key: str


def _line(rng: random.Random, lb: str, lbid: str, ts: datetime, tg: str | None, domain: str, req: str,
          node_ip: str, kind: str) -> tuple[str, int, int | None]:
    method, path = req.split(" ", 1)
    path = path.replace("{store}", rng.choice(STORES)).replace("{n}", str(rng.randint(1000, 99999)))
    client = f"198.51.100.{rng.randint(1, 254)}:{rng.randint(1024, 65000)}"
    target = f"10.0.{rng.randint(1, 3)}.{rng.randint(1, 254)}:80"
    t = f"{rng.uniform(0.005, 0.4):.3f}"
    if kind == "ok":
        elb = tgt = 200
    elif kind == "4xx":
        elb = tgt = rng.choice([400, 400, 403, 404])
    elif kind == "5xx_app":
        elb = tgt = 500
    elif kind == "5xx_lb":       # no healthy target: the load balancer answers itself
        elb, tgt, target, t = 503, None, "-", "-1"
    else:                        # redirect, no target group
        elb, tgt, target, t, tg = 301, None, "-", "-1", None
    rpt, spt = ("0.000", "0.000") if t != "-1" else ("-1", "-1")
    tg_arn = f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:targetgroup/{tg}/0123456789abcdef" if tg else "-"
    ua = rng.choice(AGENTS)
    stamp = ts.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
    typ = "h2" if rng.random() < 0.05 else "https"
    fields = [
        typ, stamp, f"app/{lb}/{lbid}", client, target, rpt, t, spt, str(elb), "-" if tgt is None else str(tgt),
        str(rng.randint(200, 9000)), str(rng.randint(90, 9000)), f'"{method} https://{domain}:443{path} HTTP/1.1"',
        f'"{ua}"', "TLS_AES_128_GCM_SHA256", "TLSv1.3", tg_arn, f'"Root=1-{rng.getrandbits(32):08x}-{rng.getrandbits(96):024x}"',
        f'"{domain}"', '"-"', str(rng.choice([10, 100])), stamp, '"redirect"' if kind == "redirect" else '"forward"',
        '"-"', '"-"' if kind != "5xx_lb" else '"-"', f'"{target}"', f'"{"-" if tgt is None else tgt}"', '"-"', '"-"',
        f"TID_{rng.getrandbits(64):016x}", '"-"', '"-"', '"-"', node_ip, '"-"', '"-"']
    if rng.random() < 0.02:
        fields.append('"a-future-field"')   # AWS adds fields at the end now and then
    return " ".join(fields), elb, tgt


def generate(root: Path, hours: int = 6, now: datetime | None = None, seed: int = 7, rows_per_file: int = 20,
             current_hour: bool = True, start: datetime | None = None) -> list[Written]:
    """Files for every 5-minute slot from `hours` hours back up to `now`. Returns every line written."""
    rng = random.Random(seed)
    now = now or datetime.now(timezone.utc)
    first = start or (now - timedelta(hours=hours)).replace(minute=0, second=0, microsecond=0)
    out: list[Written] = []
    slot = first + timedelta(minutes=5)
    while slot <= now:
        if not current_hour and slot > now.replace(minute=0, second=0, microsecond=0):
            break
        for lb, (lbid, nodes, tgs) in LBS.items():
            for node in range(nodes):
                node_ip = f"192.0.2.{10 + node}"
                name = (f"{ACCOUNT}_elasticloadbalancing_{REGION}_app.{lb}.{lbid}_{slot:%Y%m%dT%H%M}Z_"
                        f"{node_ip}_{rng.getrandbits(32):08x}.log.gz")
                key = f"{PREFIX}AWSLogs/{ACCOUNT}/elasticloadbalancing/{REGION}/{slot:%Y/%m/%d}/{name}"
                lines = []
                for _ in range(rows_per_file):
                    tg, domain, _, paths = rng.choices(tgs, weights=[x[2] for x in tgs])[0]
                    kind = rng.choices(["ok", "4xx", "5xx_app", "5xx_lb", "redirect"], [80, 10, 3, 2, 1])[0]
                    ts = slot - timedelta(seconds=rng.uniform(0.5, 299.5))
                    req = rng.choice(paths)
                    text, elb, tgt = _line(rng, lb, lbid, ts, tg, domain, req, node_ip, kind)
                    lines.append((ts, text))
                    out.append(Written(lb, None if kind == "redirect" else tg, elb, tgt, ts,
                                       req.split(" ", 1)[1], key))
                lines.sort()
                path = root / key
                path.parent.mkdir(parents=True, exist_ok=True)
                with gzip.open(path, "wt") as fh:
                    fh.write("\n".join(t for _, t in lines) + "\n")
        slot += timedelta(minutes=5)
    return out


REAL_RE = re.compile(r"^(\d{12})_elasticloadbalancing_([a-z0-9-]+)_app\..+_(\d{8})T\d{4}Z_.+\.log\.gz$")


def import_real(src: Path, root: Path) -> int:
    """Copy downloaded ALB log files into the bucket layout under root."""
    n = 0
    for f in sorted(src.glob("*.log.gz")) if src.is_dir() else []:
        m = REAL_RE.match(f.name)
        if not m:
            continue
        acct, region, day = m.groups()
        dest = root / PREFIX / "AWSLogs" / acct / "elasticloadbalancing" / region / day[:4] / day[4:6] / day[6:] / f.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            shutil.copyfile(f, dest)
            n += 1
    return n


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--hours", type=int, default=6)
    ap.add_argument("--rows", type=int, default=40, help="lines per file")
    ap.add_argument("--out", default=str(Path(__file__).parent / "logs" / "alb-bucket"))
    ap.add_argument("--real", default=str(Path(__file__).parent / "logs" / "alb"),
                    help="folder with downloaded .log.gz files to copy in")
    a = ap.parse_args()
    written = generate(Path(a.out), hours=a.hours, rows_per_file=a.rows)
    print(f"wrote {len(written):,} requests in {len({w.key for w in written}):,} files under {a.out}")
    n = import_real(Path(a.real), Path(a.out))
    if n:
        print(f"copied {n} downloaded file(s) from {a.real}")
