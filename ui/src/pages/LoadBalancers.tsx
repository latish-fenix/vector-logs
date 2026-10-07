import { keepPreviousData, useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { Fragment, useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  downloadPost, enc, get, request, type LbBody, type LbCounts, type LbOverview, type LbPathRow, type LbRequest,
  type LbSummary, type LbTargetGroupRow, type StatusClass,
} from "../api";
import { Icon, type IconName } from "../components/icons";
import { Page } from "../components/Shell";
import { Callout, Dialog, Empty, ErrorCallout, Loading, Spinner, useToast } from "../components/ui";
import { ago, duration, fmtInterval, fmtTick, fmtTime, num, storedZone, storeZone, type Zone } from "../format";
import { useClusters, useMe } from "../session";
import { TimePicker, ZoneToggle } from "./Overview";

// Application Load Balancer access logs: what the load balancers answered, by status, target
// group, path and single request. The server converts the 5-minute log files AWS writes to S3;
// the page runs 5-10 minutes behind.

type Tab = "groups" | "paths" | "requests";
const PAGE = 100;

// Status filter choices (URL parameter st).
export const STATUS_CHOICES: [string, string][] = [
  ["", "All statuses"], ["err", "Errors (4xx + 5xx)"], ["2xx", "2xx"], ["3xx", "3xx"], ["4xx", "4xx"], ["5xx", "5xx (all)"],
  ["5xx-app", "5xx from the app"], ["5xx-lb", "5xx from the load balancer"],
];
export function statusBody(st: string): Pick<LbBody, "statusClasses" | "source"> {
  if (st === "err") return { statusClasses: ["4xx", "5xx"] };
  if (st === "5xx-app") return { statusClasses: ["5xx"], source: "app" };
  if (st === "5xx-lb") return { statusClasses: ["5xx"], source: "lb" };
  if (["2xx", "3xx", "4xx", "5xx", "other"].includes(st)) return { statusClasses: [st as StatusClass] };
  return {};
}

// Stack order, bottom to top: errors sit on the baseline where they are easiest to compare.
const SERIES = ["5xx", "4xx", "3xx", "2xx", "other"] as const;
const SERIES_VAR: Record<string, string> = {
  "2xx": "var(--st-2xx)", "3xx": "var(--st-3xx)", "4xx": "var(--st-4xx)", "5xx": "var(--st-5xx)", other: "var(--st-other)",
};
const SERIES_LABEL: Record<string, string> = { "2xx": "2xx", "3xx": "3xx", "4xx": "4xx", "5xx": "5xx", other: "No status" };

/** The URL parameters both load balancer pages share, as an API body. */
export function lbBodyFromParams(params: URLSearchParams): LbBody {
  const p = (k: string) => params.get(k) ?? "";
  return {
    start: p("start") || "now-1h", end: p("end") || "now",
    lbs: p("lb") ? [p("lb")] : [],
    targetGroups: p("tg") ? [p("tg")] : [],
    domains: p("domain") ? [p("domain")] : [],
    statusCodes: p("code") ? [Number(p("code"))] : [],
    ...statusBody(p("st")),
    methods: p("method") ? [p("method")] : [],
    path: p("path"),
    pathGroup: p("pg"),
    client: p("client"),
    target: p("target"),
    minTargetSeconds: p("slow") ? Number(p("slow")) : null,
    q: p("q"),
  };
}

/** The same filters on the other page (page-only parameters dropped). */
export function lbLink(to: "/lb" | "/lb-logs", params: URLSearchParams): string {
  const n = new URLSearchParams(params);
  for (const k of ["tab", "show", "sort", "order", "wrap"]) n.delete(k);
  const qs = n.toString();
  return qs ? `${to}?${qs}` : to;
}

export function secs(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  if (v < 1) return `${Math.round(v * 1000)} ms`;
  return `${v.toFixed(v < 10 ? 2 : 1)} s`;
}

/** A path that may break after each "/" instead of in the middle of a word. */
function slashBreaks(path: string | null | undefined): ReactNode {
  if (!path) return "—";
  return path.split("/").map((part, i) => <Fragment key={i}>{i > 0 && <>/<wbr /></>}{part}</Fragment>);
}

function pct(n: number, total: number): string {
  if (!total) return "—";
  const p = (100 * n) / total;
  return p === 0 ? "0%" : p < 0.1 ? "<0.1%" : `${p < 10 ? p.toFixed(1) : Math.round(p)}%`;
}

export function CodePill({ code, fromLb }: { code: number | null | undefined; fromLb?: boolean }) {
  if (code === null || code === undefined) return <span className="hpill none"><Icon name="minus" size={13} />none</span>;
  const tone = code >= 500 ? "down" : code >= 400 ? "degraded" : code >= 200 && code < 300 ? "ok" : "none";
  const icon: IconName = tone === "down" ? "alert" : tone === "degraded" ? "warn" : tone === "ok" ? "check" : "minus";
  return (
    <span className={`hpill ${tone}`} title={fromLb ? "Answered by the load balancer (no response from a target)" : undefined}>
      <Icon name={icon} size={13} strokeWidth={2.4} />{code}{fromLb ? " · LB" : ""}
    </span>
  );
}

export function LoadBalancers() {
  const me = useMe().data!;
  const logClusters = new Set((useClusters().data ?? []).map((c) => c.id));
  const toast = useToast();
  const [params, setParams] = useSearchParams();
  const [zone, setZoneState] = useState<Zone>(storedZone());
  const setZone = (z: Zone) => { storeZone(z); setZoneState(z); };
  const p = (k: string) => params.get(k) ?? "";
  const start = p("start") || "now-1h";
  const end = p("end") || "now";
  const tab = (p("tab") as Tab) || "groups";
  const set = (patch: Record<string, string | null>) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(patch)) {
      if (v === null || v === "") next.delete(k);
      else next.set(k, v);
    }
    setParams(next, { replace: false });
  };

  // eslint-disable-next-line react-hooks/exhaustive-deps
  const body: LbBody = useMemo(() => lbBodyFromParams(params), [params]);
  const lineLink = (extra: Record<string, string>) => {
    const n = new URLSearchParams(params);
    for (const [k, v] of Object.entries(extra)) n.set(k, v);
    return lbLink("/lb-logs", n);
  };

  const overview = useQuery({ queryKey: ["lb-overview"], queryFn: () => get<LbOverview>("/lb"), staleTime: 60_000, retry: false });
  const summary = useQuery({
    queryKey: ["lb-summary", body],
    queryFn: () => request<LbSummary>("/lb/_summary", { method: "POST", body }),
    placeholderData: keepPreviousData,
    retry: false,
    refetchInterval: (query) => {
      const pend = (query.state.data as LbSummary | undefined)?.pending;
      if (pend && (pend.files > 0 || pend.requested || pend.starting)) return 5_000;
      return end === "now" ? 60_000 : false;
    },
  });
  const d = summary.data;

  // the search/path boxes apply on submit
  const [qText, setQText] = useState(p("q"));
  const [pathText, setPathText] = useState(p("path"));
  useEffect(() => { setQText(p("q")); setPathText(p("path")); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [params]);
  const submit = (e: FormEvent) => { e.preventDefault(); set({ q: qText.trim() || null, path: pathText.trim() || null }); };

  const lbNames = overview.data?.items.map((i) => i.name) ?? [];
  const chips: [string, ReactNode, string[]][] = [];
  if (p("tg")) chips.push(["tg", <>target group <strong>{p("tg") === "-" ? "(none)" : p("tg")}</strong></>, ["tg"]]);
  if (p("pg")) chips.push(["pg", <>path <strong>{p("method") ? `${p("method")} ` : ""}{p("pg")}</strong></>, ["pg", "method"]]);
  else if (p("method")) chips.push(["method", <>method <strong>{p("method")}</strong></>, ["method"]]);
  if (p("code")) chips.push(["code", <>status <strong>{p("code")}</strong></>, ["code"]]);
  if (p("domain")) chips.push(["domain", <>domain <strong>{p("domain")}</strong></>, ["domain"]]);
  if (p("client")) chips.push(["client", <>client <strong>{p("client")}</strong></>, ["client"]]);
  if (p("target")) chips.push(["target", <>target <strong>{p("target")}</strong></>, ["target"]]);
  if (p("slow")) chips.push(["slow", <>target time ≥ <strong>{secs(Number(p("slow")))}</strong></>, ["slow"]]);

  const noData = overview.data && overview.data.items.length === 0 && d && d.source === "none";
  const conv = overview.data?.converter;

  return (
    <Page wide crumbs={[{ label: "Infrastructure" }, { label: "Load balancer dashboard" }]} title="Load balancer dashboard"
      actions={<span className="row" style={{ gap: 10 }}>{conv?.updatedAt && <span className="hint lb-updated">logs converted {ago(conv.updatedAt)}</span>}<ZoneToggle zone={zone} onChange={setZone} /></span>}>
      <div className="page-head">
        <div className="grow">
          <h1>Load balancer dashboard</h1>
          <p className="sub">Every request through the Application Load Balancers, from their access logs in S3: status codes, target groups, paths and single requests. About 5–10 minutes behind.</p>
        </div>
      </div>

      <section className="card">
        <form className="lv-bar" onSubmit={submit} style={{ alignItems: "flex-end" }}>
          <TimePicker start={start} end={end} zone={zone} maxHours={me.limits.maxSearchHours} compact
            onChange={(s, e) => set({ start: s, end: e === "now" ? null : e })} />
          <div className="field">
            <label className="sr-only" htmlFor="lb-pick">Load balancer</label>
            <select id="lb-pick" className="select" style={{ width: "auto" }} value={p("lb")} onChange={(e) => set({ lb: e.target.value || null })}>
              <option value="">All load balancers</option>
              {lbNames.map((n) => <option key={n} value={n}>{n}</option>)}
            </select>
          </div>
          <div className="field">
            <label className="sr-only" htmlFor="lb-status">Status</label>
            <select id="lb-status" className="select" style={{ width: "auto" }} value={p("st")} onChange={(e) => set({ st: e.target.value || null })}>
              {STATUS_CHOICES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </select>
          </div>
          <div className="field">
            <label className="sr-only" htmlFor="lb-path">Path contains</label>
            <input id="lb-path" className="input mono" style={{ width: 170 }} placeholder="Path contains…" value={pathText} onChange={(e) => setPathText(e.target.value)} />
          </div>
          <div className="field grow" style={{ minWidth: 160 }}>
            <label className="sr-only" htmlFor="lb-q">Search</label>
            <input id="lb-q" className="input" placeholder="Search URL, user agent, trace id, IP…" title="Words in the URL, user agent, trace id, client or target IP, error reason" value={qText} onChange={(e) => setQText(e.target.value)} />
          </div>
          <button type="submit" className="btn btn-primary"><Icon name="search" size={15} /> Apply</button>
          <button type="button" className="btn btn-ghost icon-btn" title="Refresh" aria-label="Refresh" onClick={() => summary.refetch()}>
            {summary.isFetching ? <Spinner /> : <Icon name="refresh" size={16} />}
          </button>
        </form>
        {chips.length > 0 && (
          <div className="lv-status" style={{ borderBottom: 0 }}>
            <span className="hint">Filtered to</span>
            {chips.map(([k, label, keys]) => (
              <span key={k} className="chip" style={{ fontFamily: "var(--font-sans)" }}>
                <span>{label}</span>
                <button type="button" aria-label="Remove filter" onClick={() => set(Object.fromEntries(keys.map((x) => [x, null])))}><Icon name="x" size={13} /></button>
              </span>
            ))}
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => set(Object.fromEntries(["tg", "pg", "method", "code", "domain", "client", "target", "slow"].map((x) => [x, null])))}>Clear all</button>
          </div>
        )}
      </section>

      <Status summary={d} admin={me.admin} overviewError={overview.error} />

      {noData ? (
        <section className="card">
          <Empty title="No load balancer logs yet">
            {me.admin ? <>Turn on access logs for a load balancer (S3 bucket <code>fenix-vector-ecs-logs</code>, prefix <code>loadbalancer-logs</code>); its requests show up here about 10 minutes later.</>
              : "None of the load balancers that serve your clusters has logs yet."}
          </Empty>
        </section>
      ) : summary.isLoading ? <section className="card"><Loading what="Counting requests…" /></section>
        : summary.error && !d ? <section className="card"><div className="card-body"><ErrorCallout error={summary.error} admin={me.admin} /></div></section>
        : d ? (
          <>
            <Tiles d={d} st={p("st")} onStatus={(st) => set({ st: p("st") === st ? null : st })} />
            <section className="card">
              <div className="card-head">
                <div className="grow">
                  <h2 className="card-title">Requests over time</h2>
                  <span className="hint">{fmtTime(d.start, zone, false)} – {fmtTime(d.end, zone, false)} · each bar {fmtInterval(d.interval)}</span>
                </div>
                <div className="tabs" role="group" aria-label="Chart shows">
                  <button type="button" className="tab" aria-pressed={p("show") !== "errors"} aria-selected={p("show") !== "errors"} onClick={() => set({ show: null })}>All requests</button>
                  <button type="button" className="tab" aria-pressed={p("show") === "errors"} aria-selected={p("show") === "errors"} onClick={() => set({ show: "errors" })}>Errors only</button>
                </div>
              </div>
              <StatusChart buckets={d.buckets} interval={d.interval} start={d.start} end={d.end} zone={zone} errorsOnly={p("show") === "errors"}
                onZoom={(a, b) => set({ start: new Date(a).toISOString(), end: new Date(b).toISOString() })} />
            </section>

            <section className="card" style={{ overflow: "hidden" }}>
              <div className="card-head">
                <div className="tabs" role="tablist" aria-label="Show">
                  {([["groups", `Target groups · ${d.targetGroups.length}`], ["paths", "Paths"], ["requests", "Requests"]] as [Tab, string][]).map(([v, l]) => (
                    <button key={v} type="button" role="tab" className="tab" aria-selected={tab === v} onClick={() => set({ tab: v === "groups" ? null : v })}>{l}</button>
                  ))}
                </div>
                <div className="grow" />
                <span className="hint">{num(d.totals.requests)} requests · {num(d.files)} files · {duration(d.tookMs)}</span>
              </div>
              {tab === "groups" && <GroupsTable rows={d.targetGroups} logClusters={logClusters} lineLink={lineLink}
                onPick={(tg) => set({ tg: tg ?? "-", tab: "paths" })} onPath={(tg, m, pg) => set({ tg: tg ?? "-", method: m, pg, tab: "requests" })} />}
              {tab === "paths" && <PathsTab body={body} sort={p("sort") || "errors"} setSort={(s) => set({ sort: s === "errors" ? null : s })}
                onPick={(m, pg) => set({ method: m, pg, tab: "requests" })} />}
              {tab === "requests" && <RequestsTab body={body} linesTo={lbLink("/lb-logs", params)} zone={zone} total={d.totals.requests} maxExport={me.limits.maxExportRows}
                onFilter={(k, v) => set({ [k]: v })} onExported={(rows, name) => toast(`Downloaded ${num(rows)} requests as ${name}`)} />}
            </section>
          </>
        ) : null}
    </Page>
  );
}

// ------------------------------------------------------------------ status line

function Status({ summary, admin, overviewError }: { summary: LbSummary | undefined; admin: boolean; overviewError: unknown }) {
  const pend = summary?.pending;
  if (overviewError) return <ErrorCallout error={overviewError} admin={admin} />;
  if (!pend) return null;
  if (pend.lastError) {
    return (
      <Callout tone="danger" icon="alert" title="The server can't read the load balancer logs right now">
        {admin ? <>{pend.lastError.message} <span className="hint">({pend.lastError.code}, {ago(pend.lastError.at)})</span></>
          : "The numbers below may be out of date. An admin can see the reason here."}
      </Callout>
    );
  }
  if (pend.starting) return <Callout tone="neutral" icon="clock" title="Getting the load balancer logs ready">The server has just started converting the logs; the first numbers show up within a few minutes. This page refreshes by itself.</Callout>;
  if (pend.files > 0) {
    const done = pend.of - pend.files;
    return (
      <Callout tone="neutral" icon="clock" title={`Converting ${num(pend.files)} of ${num(pend.of)} log files for this range`}>
        <div className="stack-sm">
          <span>{pend.requested ? "This range is older than the days the server keeps ready, so its files are being converted now." : "The server is catching up."} Counts grow as files are converted; this page refreshes every few seconds.</span>
          <div className="lbprogress" role="progressbar" aria-valuemin={0} aria-valuemax={pend.of} aria-valuenow={done}><span style={{ width: `${pend.of ? (100 * done) / pend.of : 0}%` }} /></div>
        </div>
      </Callout>
    );
  }
  if (pend.requested) return <Callout tone="neutral" icon="clock" title="Looking for the log files of this older range">The server converts them now; this page refreshes by itself.</Callout>;
  return null;
}

// ------------------------------------------------------------------ tiles

function Tiles({ d, st, onStatus }: { d: LbSummary; st: string; onStatus: (st: string) => void }) {
  const t = d.totals;
  const minutes = Math.max(1, (d.end - d.start) / 60_000);
  return (
    <div className="stats lbtiles">
      <Tile label="Requests" value={num(t.requests)} note={`≈ ${num(Math.round(t.requests / minutes))} per minute`} />
      <Tile label="2xx" value={pct(t["2xx"], t.requests)} note={`${num(t["2xx"])} succeeded`} active={st === "2xx"} onClick={() => onStatus("2xx")} />
      <Tile label="4xx" value={pct(t["4xx"], t.requests)} note={`${num(t["4xx"])} client errors`} tone={t["4xx"] ? "degraded" : undefined} icon={t["4xx"] ? "warn" : undefined}
        active={st === "4xx"} onClick={() => onStatus("4xx")} />
      <Tile label="5xx from app" value={num(t.s5xxApp)} note={t.s5xxApp ? `${pct(t.s5xxApp, t.requests)} · the target answered 5xx` : "the target answered 5xx"}
        tone={t.s5xxApp ? "down" : "ok"} icon={t.s5xxApp ? "alert" : "check"} active={st === "5xx-app"} onClick={() => onStatus("5xx-app")} />
      <Tile label="5xx from LB" value={num(t.s5xxLb)} note={t.s5xxLb ? `${pct(t.s5xxLb, t.requests)} · the load balancer answered, no target did` : "the load balancer answered, no target did"}
        tone={t.s5xxLb ? "down" : "ok"} icon={t.s5xxLb ? "alert" : "check"} active={st === "5xx-lb"} onClick={() => onStatus("5xx-lb")} />
      <Tile label="Target time p95" value={secs(t.p95)} note={`p50 ${secs(t.p50)} · p99 ${secs(t.p99)} · approx.`} />
    </div>
  );
}

function Tile({ label, value, note, tone, icon, active, onClick }: {
  label: string; value: string; note: string; tone?: string; icon?: IconName; active?: boolean; onClick?: () => void;
}) {
  const inner = (
    <>
      <span className="stat-label">{label}</span>
      <span className={`stat-value ${tone ? `tone-${tone}` : ""}`}>{icon && <Icon name={icon} size={17} strokeWidth={2.4} style={{ marginRight: 6, verticalAlign: -2 }} />}{value}</span>
      <span className="stat-note">{note}</span>
    </>
  );
  return onClick
    ? <button type="button" className={`stat htile ${active ? "active" : ""}`} onClick={onClick} aria-pressed={active} title="Show only these">{inner}</button>
    : <div className="stat htile">{inner}</div>;
}

// ------------------------------------------------------------------ chart

function useWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [w, setW] = useState(800);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => setW(Math.max(280, Math.floor(e.contentRect.width))));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, w] as const;
}

function niceMax(v: number): number {
  if (v <= 4) return 4;
  const pw = 10 ** Math.floor(Math.log10(v));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * pw >= v) return m * pw;
  return 10 * pw;
}

const CH = 170;
const PAD = { l: 52, r: 8, t: 8, b: 22 };

/** Requests per time bucket, stacked by status class. Click a bar to zoom into it. */
function StatusChart({ buckets, interval, start, end, zone, errorsOnly, onZoom }: {
  buckets: (LbCounts & { t: number })[]; interval: number; start: number; end: number; zone: Zone; errorsOnly: boolean;
  onZoom: (from: number, to: number) => void;
}) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  const keys = errorsOnly ? (["5xx", "4xx"] as const) : SERIES;
  const series = useMemo(() => {
    const byT = new Map(buckets.map((b) => [b.t, b]));
    const out: { t: number; v: Record<string, number>; total: number }[] = [];
    for (let t = Math.floor(start / interval) * interval; t <= end; t += interval) {
      const b = byT.get(t);
      const v: Record<string, number> = {};
      for (const k of SERIES) v[k] = b ? b[k] : 0;
      out.push({ t, v, total: SERIES.reduce((a, k) => a + v[k], 0) });
    }
    return out;
  }, [buckets, interval, start, end]);
  const shown = (s: (typeof series)[number]) => keys.reduce((a, k) => a + s.v[k], 0);
  const present = keys.filter((k) => series.some((s) => s.v[k]));
  const max = niceMax(Math.max(1, ...series.map(shown)));
  const plotW = width - PAD.l - PAD.r;
  const plotH = CH - PAD.t - PAD.b;
  const slot = plotW / Math.max(1, series.length);
  const barW = Math.max(1, slot - 2);
  const y = (v: number) => PAD.t + plotH - (v / max) * plotH;
  const tickEvery = Math.max(1, Math.ceil(series.length / Math.max(2, Math.floor(plotW / 90))));
  const h = hover !== null ? series[hover] : null;

  return (
    <div className="histo" ref={ref}>
      <div className="histo-legend" aria-label="Legend" style={{ paddingLeft: PAD.l }}>
        {[...present].reverse().map((k) => (
          <span key={k} className="histo-key"><span className="swatch" style={{ background: SERIES_VAR[k] }} />{SERIES_LABEL[k]}</span>
        ))}
        {present.length === 0 && <span className="hint">{errorsOnly ? "No 4xx or 5xx in this range" : "No requests in this range"}</span>}
        <span className="hint" style={{ marginLeft: "auto" }}>click a bar to zoom in</span>
      </div>
      <svg width={width} height={CH} role="img" aria-label={`Requests over time by status, ${series.length} bars of ${fmtInterval(interval)}`}
        onMouseLeave={() => setHover(null)}>
        {[0, 0.5, 1].map((f) => (
          <g key={f}>
            <line x1={PAD.l} x2={width - PAD.r} y1={y(max * f)} y2={y(max * f)} className="histo-grid" />
            <text x={PAD.l - 6} y={y(max * f) + 4} textAnchor="end" className="histo-axis">{num(max * f)}</text>
          </g>
        ))}
        {series.map((s, i) => {
          const x = PAD.l + i * slot + 1;
          let acc = 0;
          const segs = keys.filter((k) => s.v[k]).map((k) => {
            const y0 = y(acc);
            acc += s.v[k];
            return { k, y0, y1: y(acc) };
          });
          return (
            <g key={s.t} opacity={hover === null || hover === i ? 1 : 0.55}>
              {segs.map((g, j) => {
                const top = j === segs.length - 1;
                const hgt = Math.max(1, g.y0 - g.y1);
                const r = top ? Math.min(4, barW / 2, hgt) : 0;
                const dd = r
                  ? `M${x},${g.y0} V${g.y1 + r} Q${x},${g.y1} ${x + r},${g.y1} H${x + barW - r} Q${x + barW},${g.y1} ${x + barW},${g.y1 + r} V${g.y0} Z`
                  : `M${x},${g.y0} V${g.y0 - hgt} H${x + barW} V${g.y0} Z`;
                return <path key={g.k} d={dd} fill={SERIES_VAR[g.k]} className="histo-seg" style={barW < 6 ? { strokeWidth: 0.5 } : undefined} />;
              })}
              <rect x={PAD.l + i * slot} y={PAD.t} width={slot} height={plotH} fill="transparent" style={{ cursor: s.total ? "zoom-in" : "default" }}
                onMouseEnter={() => setHover(i)} onClick={() => s.total && onZoom(s.t, Math.min(s.t + interval, end))} />
            </g>
          );
        })}
        <line x1={PAD.l} x2={width - PAD.r} y1={y(0)} y2={y(0)} className="histo-base" />
        {series.map((s, i) => (i % tickEvery === 0 ? (
          <text key={s.t} x={PAD.l + i * slot + slot / 2} y={CH - 6} textAnchor="middle" className="histo-axis">{fmtTick(s.t, zone, end - start)}</text>
        ) : null))}
      </svg>
      {h && (
        <div className="histo-tip" style={{ left: Math.min(Math.max(PAD.l + (hover ?? 0) * slot + slot / 2, 110), width - 110) }}>
          <div className="t">{fmtTime(h.t, zone, false)} – {fmtTime(Math.min(h.t + interval, end), zone, false).slice(11)}</div>
          <div className="row" style={{ justifyContent: "space-between", gap: 16 }}><strong>Requests</strong><strong>{num(h.total)}</strong></div>
          {[...SERIES].reverse().filter((k) => h.v[k]).map((k) => (
            <div key={k} className="row" style={{ justifyContent: "space-between", gap: 16 }}>
              <span className="row" style={{ gap: 6 }}><span className="swatch" style={{ background: SERIES_VAR[k] }} />{SERIES_LABEL[k]}</span>
              <span>{num(h.v[k])} <span className="hint">· {pct(h.v[k], h.total)}</span></span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ target groups

function GroupsTable({ rows, logClusters, lineLink, onPick, onPath }: {
  rows: LbTargetGroupRow[]; logClusters: Set<string>; lineLink: (extra: Record<string, string>) => string;
  onPick: (tg: string | null) => void; onPath: (tg: string | null, method: string, pg: string) => void;
}) {
  if (!rows.length) return <Empty title="No requests match" />;
  return (
    <div className="table-scroll">
      <table className="table htable">
        <thead>
          <tr>
            <th>Target group</th><th>Load balancer</th><th className="num">Requests</th><th className="num">4xx</th><th className="num">5xx</th>
            <th className="num">p95</th><th>Top failing path</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.tg ?? "-"} className="clickable" tabIndex={0} onClick={() => onPick(r.tg)} onKeyDown={(e) => { if (e.key === "Enter") onPick(r.tg); }}
              title="Show this target group's paths">
              <td className="nowrap">
                {r.tg ? <span className="mono" style={{ fontWeight: 600 }}>{r.tg}</span> : <span className="hint" title="Requests the load balancer answered without forwarding (redirects, fixed responses, bad requests)">(no target group)</span>}
                {r.cluster && logClusters.has(r.cluster) && <Link className="hlink" to={`/logs/${enc(r.cluster)}`} onClick={(e) => e.stopPropagation()}>logs</Link>}
                <Link className="hlink" to={lineLink({ tg: r.tg ?? "-" })} onClick={(e) => e.stopPropagation()} title="This target group's log lines">lines</Link>
                {r.tg && <Link className="hlink" to={`/health?q=${enc(r.tg)}`} onClick={(e) => e.stopPropagation()}>health</Link>}
              </td>
              <td className="mono nowrap" style={{ fontSize: 13 }}>{r.lbs.map((l) => <div key={l}>{l}</div>)}</td>
              <td className="num">{num(r.requests)}</td>
              <td className="num nowrap">{r.s4xx ? <span className="warn-text">{num(r.s4xx)}</span> : <span className="hint">0</span>} <span className="hint">{r.s4xx ? pct(r.s4xx, r.requests) : ""}</span></td>
              <td className="num nowrap">
                {r.s5xx ? <span className="danger-text" title={`${num(r.s5xxApp)} from the app, ${num(r.s5xxLb)} from the load balancer`}><Icon name="alert" size={13} style={{ verticalAlign: -2, marginRight: 3 }} />{num(r.s5xx)}</span> : <span className="hint">0</span>}
                {r.s5xxLb > 0 && <span className="hint" title="No target answered"> · {num(r.s5xxLb)} LB</span>}
              </td>
              <td className="num nowrap">{secs(r.p95)}</td>
              <td className="lbpath">
                {r.topError ? (
                  <button type="button" className="linkish" onClick={(e) => { e.stopPropagation(); onPath(r.tg, r.topError!.method ?? "", r.topError!.pathGroup ?? ""); }}
                    title="Show these requests">
                    <CodePill code={r.topError.code} /> <span className="mono">{r.topError.method} {slashBreaks(r.topError.pathGroup)}</span> <span className="hint">· {num(r.topError.count)}</span>
                  </button>
                ) : <span className="hint">—</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ------------------------------------------------------------------ paths

function PathsTab({ body, sort, setSort, onPick }: { body: LbBody; sort: string; setSort: (s: string) => void; onPick: (m: string, pg: string) => void }) {
  const q = useQuery({
    queryKey: ["lb-paths", body, sort],
    queryFn: () => request<{ items: LbPathRow[]; groups: number; tookMs: number }>("/lb/_paths", { method: "POST", body: { ...body, sort, limit: 100 } }),
    placeholderData: keepPreviousData,
    retry: false,
  });
  return (
    <>
      <div className="lv-status" style={{ borderTop: 0 }}>
        <span className="hint">Paths with store names, ids and numbers grouped (<code>&lt;store&gt;</code>, <code>&lt;n&gt;</code>, <code>&lt;uuid&gt;</code>). Click a row for its requests.</span>
        <div className="grow" />
        <label className="hint" htmlFor="lb-sort">Sort by</label>
        <select id="lb-sort" className="select" style={{ width: "auto", minHeight: 32 }} value={sort} onChange={(e) => setSort(e.target.value)}>
          <option value="errors">Most errors</option>
          <option value="requests">Most requests</option>
          <option value="slow">Slowest (p95)</option>
        </select>
      </div>
      {q.isLoading ? <Loading what="Grouping paths…" /> : q.error ? <div className="card-body"><ErrorCallout error={q.error} /></div>
        : !q.data?.items.length ? <Empty title="No requests match" /> : (
          <div className="table-scroll">
            <table className="table htable">
              <thead>
                <tr><th>Path</th><th className="num">Requests</th><th className="num">2xx</th><th className="num">4xx</th><th className="num">5xx</th><th className="num">p95</th><th>Most common status</th><th>Target groups</th></tr>
              </thead>
              <tbody>
                {q.data.items.map((r) => (
                  <tr key={`${r.method} ${r.pathGroup}`} className="clickable" tabIndex={0} onClick={() => onPick(r.method ?? "", r.pathGroup ?? "")}
                    onKeyDown={(e) => { if (e.key === "Enter") onPick(r.method ?? "", r.pathGroup ?? ""); }}>
                    <td className="mono lbpath" title={r.example ?? undefined}><strong>{r.method ?? "—"}</strong> {slashBreaks(r.pathGroup)}</td>
                    <td className="num">{num(r.requests)}</td>
                    <td className="num">{num(r["2xx"])}</td>
                    <td className="num">{r["4xx"] ? <span className="warn-text">{num(r["4xx"])}</span> : <span className="hint">0</span>}</td>
                    <td className="num">{r["5xx"] ? <span className="danger-text">{num(r["5xx"])}</span> : <span className="hint">0</span>}</td>
                    <td className="num nowrap">{secs(r.p95)}</td>
                    <td><CodePill code={r.topCode} /></td>
                    <td className="mono nowrap" style={{ fontSize: 12.5 }}>{r.targetGroups.map((t) => t ?? "(none)").join(", ")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      {q.data && <div className="card-foot"><span className="hint">{q.data.groups > q.data.items.length ? `The first ${q.data.items.length} of ${num(q.data.groups)} paths` : `${num(q.data.groups)} paths`} · {duration(q.data.tookMs)}</span></div>}
    </>
  );
}

// ------------------------------------------------------------------ single requests

export const DETAIL: [string, string][] = [
  ["time", "Time (UTC)"], ["elb_code", "Status (load balancer)"], ["tgt_code", "Status (target)"], ["method", "Method"], ["url", "URL"],
  ["path_group", "Path group"], ["domain", "Domain"], ["lb", "Load balancer"], ["tg", "Target group"], ["cluster", "Cluster"], ["target", "Target"],
  ["client_ip", "Client IP"], ["client_port", "Client port"], ["req_t", "Request time (s)"], ["tgt_t", "Target time (s)"], ["resp_t", "Response time (s)"],
  ["rx", "Bytes received"], ["tx", "Bytes sent"], ["user_agent", "User agent"], ["error_reason", "Error reason"], ["classification", "Classification"],
  ["classification_reason", "Classification reason"], ["actions", "Actions"], ["elb_error_code", "LB error code"], ["target_error_code", "Target error code"],
  ["trace_id", "Trace id"], ["type", "Type"], ["protocol", "Protocol"], ["ssl_protocol", "TLS"], ["rule_priority", "Rule priority"],
  ["request_created", "Request created"], ["target_list", "Targets tried"], ["target_code_list", "Target statuses"],
];

function RequestsTab({ body, linesTo, zone, total, maxExport, onFilter, onExported }: {
  body: LbBody; linesTo: string; zone: Zone; total: number; maxExport: number; onFilter: (k: string, v: string) => void; onExported: (rows: number, name: string) => void;
}) {
  const [open, setOpen] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);
  const q = useInfiniteQuery({
    queryKey: ["lb-requests", body],
    queryFn: ({ pageParam }) => request<{ total: number; hits: LbRequest[]; tookMs: number }>("/lb/_requests", { method: "POST", body: { ...body, offset: pageParam, size: PAGE } }),
    initialPageParam: 0,
    getNextPageParam: (last, pages) => {
      const n = pages.reduce((a, pg) => a + pg.hits.length, 0);
      return n < last.total && last.hits.length === PAGE ? n : undefined;
    },
    retry: false,
  });
  const hits = q.data?.pages.flatMap((pg) => pg.hits) ?? [];
  const matched = q.data?.pages[0]?.total ?? total;
  const key = (h: LbRequest, i: number) => `${h.trace_id ?? ""}-${h.ts_ms}-${i}`;
  return (
    <>
      <div className="lv-status" style={{ borderTop: 0 }}>
        <span><strong>{num(matched)}</strong> requests, newest first</span>
        <div className="grow" />
        <Link className="btn btn-sm" to={linesTo} title="The same requests as the original log lines, full screen"><Icon name="terminal" size={15} /> Open as log lines</Link>
        <button type="button" className="btn btn-sm" onClick={() => setExporting(true)} disabled={!matched}><Icon name="download" size={15} /> Export</button>
      </div>
      {q.isLoading ? <Loading what="Reading requests…" /> : q.error ? <div className="card-body"><ErrorCallout error={q.error} /></div>
        : !hits.length ? <Empty title="No requests match" /> : (
          <div className="table-scroll">
            <table className="table lv-table">
              <thead>
                <tr><th style={{ width: 28 }}><span className="sr-only">Expand</span></th><th>Time</th><th>Status</th><th>Request</th><th>Target group · target</th><th>Client</th><th className="num">Target time</th></tr>
              </thead>
              <tbody>
                {hits.map((h, i) => {
                  const k = key(h, i);
                  const isOpen = open === k;
                  const fromLb = (h.elb_code as number) >= 500 && (h.tgt_code === null || h.tgt_code === undefined);
                  return (
                    <Fragment key={k}>
                      <tr className={`clickable ${isOpen ? "open" : ""}`} tabIndex={0} aria-expanded={isOpen} onClick={() => setOpen(isOpen ? null : k)}
                        onKeyDown={(e) => { if (e.key === "Enter") setOpen(isOpen ? null : k); }}>
                        <td className="lv-caret"><Icon name="chevron" size={14} style={{ transform: isOpen ? "rotate(90deg)" : undefined }} /></td>
                        <td className="cell-mono nowrap">{fmtTime(h.ts_ms, zone)}</td>
                        <td className="nowrap"><CodePill code={h.elb_code as number} fromLb={fromLb} /></td>
                        <td className="cell-mono data-cell lbreq" title={(h.url as string) ?? undefined}><strong>{h.method ?? "—"}</strong> {h.path ?? "—"}{h.query ? <span className="faint">?{h.query}</span> : null} <span className="hint">· {h.domain ?? ""}</span></td>
                        <td className="cell-mono nowrap">{h.tg ?? <span className="faint">—</span>}<div className="hint" style={{ fontSize: 12 }}>{h.target ?? "no target"}</div></td>
                        <td className="cell-mono nowrap">{h.client_ip ?? "—"}</td>
                        <td className="num nowrap">{secs(h.tgt_t as number | null)}</td>
                      </tr>
                      {isOpen && (
                        <tr className="lv-detail-row"><td colSpan={7}>
                          <div className="lv-detail">
                            <div className="lv-kv">
                              {DETAIL.filter(([f]) => h[f] !== undefined && h[f] !== null).map(([f, label]) => (
                                <div key={f} className="lv-kv-row">
                                  <span className="lv-k" title={f}>{label}</span>
                                  <span className="mono lv-v">{String(h[f])}</span>
                                  <span className="lv-kv-actions">
                                    {["client_ip", "target", "tg", "domain"].includes(f) && (
                                      <button type="button" className="btn btn-ghost mini-btn" title="Filter on this value" aria-label={`Filter on ${label}`}
                                        onClick={() => onFilter(f === "client_ip" ? "client" : f, String(h[f]))}><Icon name="filter" size={13} /></button>
                                    )}
                                  </span>
                                </div>
                              ))}
                            </div>
                          </div>
                        </td></tr>
                      )}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
            <div className="lv-more">
              {q.hasNextPage ? (
                <button type="button" className="btn btn-sm" onClick={() => q.fetchNextPage()} disabled={q.isFetchingNextPage}>
                  {q.isFetchingNextPage ? <Spinner /> : null} Load {PAGE} more
                </button>
              ) : <span>All {num(hits.length)} shown</span>}
              <span className="hint">· {num(hits.length)} of {num(matched)}</span>
            </div>
          </div>
        )}
      {exporting && <LbExportDialog body={body} total={matched} max={maxExport} onClose={() => setExporting(false)}
        onDone={(rows, name) => { setExporting(false); onExported(rows, name); }} />}
    </>
  );
}

export function LbExportDialog({ body, total, max, onClose, onDone }: { body: LbBody; total: number; max: number; onClose: () => void; onDone: (rows: number, name: string) => void }) {
  const [format, setFormat] = useState<"csv" | "json" | "ndjson">("csv");
  const cap = Math.max(1, Math.min(total, max));
  const [limit, setLimit] = useState(cap);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const go = async () => {
    setBusy(true);
    setError(null);
    try {
      const { blob, filename, rows } = await downloadPost("/lb/_export", { ...body, format, limit: Math.max(1, Math.min(limit, cap)) });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 5000);
      onDone(rows, filename);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog title="Export requests" subtitle={`${num(total)} match, newest first`} onClose={onClose} busy={busy}
      footer={<>
        <button type="button" className="btn" onClick={onClose} disabled={busy}>Cancel</button>
        <button type="button" className="btn btn-primary" onClick={go} disabled={busy}>{busy ? <Spinner label="Exporting" /> : <Icon name="download" />} Download</button>
      </>}>
      <div className="stack">
        <fieldset className="stack-sm" style={{ border: 0, padding: 0, margin: 0 }}>
          <legend className="label" style={{ marginBottom: 6 }}>Format</legend>
          {([["csv", "CSV", "opens in Excel; one row per request"], ["json", "JSON", "an array of objects"], ["ndjson", "NDJSON", "one JSON object per line"]] as const).map(([v, l, h]) => (
            <label key={v} className="check"><input type="radio" name="fmt" checked={format === v} onChange={() => setFormat(v)} /> <span><strong>{l}</strong> <span className="hint">· {h}</span></span></label>
          ))}
        </fieldset>
        <div className="field" style={{ maxWidth: 260 }}>
          <label className="label" htmlFor="lbexp-limit">Requests <span className="hint">· up to {num(cap)}{total > max ? ` (the newest ${num(max)})` : ""}</span></label>
          <input id="lbexp-limit" className="input" type="number" min={1} max={cap} value={limit} onChange={(e) => setLimit(Number(e.target.value))} />
        </div>
        <Callout tone="neutral" icon="info">The file has client IP addresses and full URLs, including store names and query strings. Treat it accordingly.</Callout>
        {error ? <ErrorCallout error={error} /> : null}
      </div>
    </Dialog>
  );
}
