import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { createContext, useContext, useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type ReactNode } from "react";
import { useSearchParams } from "react-router-dom";
import { get, type OpsHealth, type OpsRange, type OpsStatus, type OpsSummary } from "../api";
import { Icon } from "../components/icons";
import { bucket, clock, clockZ, cumulative, dateLabel, dayIn, Legend, LineChart, localStamp, offsetMs, Spark, ZONE_LABEL, ZONE_TITLE, ZONES, zoneName, type ClockZone, type LineSeries } from "../components/LineChart";
import { Page } from "../components/Shell";
import { Callout, Empty, ErrorCallout, Loading, SearchSelect, downloadFile } from "../components/ui";
import { ago, num } from "../format";
import { useMe } from "../session";
import "../ops.css";

// API Gateway calls per stage (per-minute CloudWatch copies in S3) and orders processed by OMS / CRS
// (counted from the Vector logs), against the same time last week. Times and the day follow the zone
// switch in the header (IST, PST or EST, remembered per browser): a day runs midnight to midnight there.

type Tab = "live" | "health" | "history";
interface Fresh { asOf: number | null; ageMs: number | null; stale: boolean; error: string | null; past?: boolean }

const C_NOW = "var(--accent)";
const C_LW = "var(--faint)";
const C_4XX = "var(--st-4xx)";
const C_5XX = "var(--st-5xx)";
const C_BACKEND = "#8b7fe0";

const ZoneCtx = createContext<ClockZone>("ist");
const useZone = () => useContext(ZoneCtx);
const ZONE_KEY = "ops-zone";
function storedOpsZone(): ClockZone {
  try { const v = localStorage.getItem(ZONE_KEY); return ZONES.includes(v as ClockZone) ? (v as ClockZone) : "ist"; } catch { return "ist"; }
}
/** A time in the page's zone, e.g. "17:35 IST"; hover shows the other zones. */
function T({ ms }: { ms: number | null | undefined }) {
  const z = useZone();
  if (ms === null || ms === undefined) return <>—</>;
  return <span title={clockZ(ms, z, true)} style={{ whiteSpace: "nowrap" }}>{clockZ(ms, z)}</span>;
}
const pct = (v: number | null | undefined, digits = 1) => (v === null || v === undefined ? "—" : `${v.toFixed(digits)}%`);
const ms = (v: number | null | undefined) => (v === null || v === undefined ? "—" : v >= 1000 ? `${(v / 1000).toFixed(2)} s` : `${Math.round(v)} ms`);
const share = (part: number, whole: number) => (whole ? (100 * part) / whole : null);
const compact = (v: number | null | undefined) => {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  const a = Math.abs(v);
  if (a >= 1e6) return `${(v / 1e6).toFixed(a >= 1e7 ? 1 : 2).replace(/\.?0+$/, "")}M`;
  if (a >= 1e4) return `${(v / 1e3).toFixed(a >= 1e5 ? 0 : 1).replace(/\.0$/, "")}k`;
  return num(Math.round(v));
};
const signed = (v: number) => `${v > 0 ? "+" : v < 0 ? "−" : "±"}${Math.abs(v) >= 100 ? Math.round(Math.abs(v)) : Math.abs(v).toFixed(1)}%`;

/** Last `n` known minutes (today's tail is null), and the same minutes in `other`. */
function lastKnown(values: (number | null)[], n: number, other?: (number | null)[] | null) {
  let end = values.length;
  while (end > 0 && values[end - 1] === null) end--;
  const from = Math.max(0, end - n);
  const pick = (arr: (number | null)[] | null | undefined) => (arr ? arr.slice(from, end).reduce<number>((s, v) => s + (v ?? 0), 0) : null);
  return { sum: pick(values) ?? 0, other: pick(other), minutes: end - from };
}

// ------------------------------------------------------------------ small pieces
function Delta({ value, title }: { value: number | null | undefined; title?: string }) {
  if (value === null || value === undefined) return <span className="ops-delta" title="Last week is not available yet">no comparison</span>;
  const cls = value <= -25 ? "bad" : value >= 0.5 ? "up" : value < 0 ? "down" : "";
  return (
    <span className={`ops-delta ${cls}`} title={title ?? "Compared with the same time last week"}>
      {value > 0 ? "▲" : value < 0 ? "▼" : ""} {signed(value)}
    </span>
  );
}

/** Change in a table cell: plain, red only for a big drop on a busy row. */
function Chg({ value, base }: { value: number | null | undefined; base?: number | null }) {
  if (value === null || value === undefined) return <span className="ops-chg none">—</span>;
  const bad = value <= -25 && (base ?? 0) >= 100;
  return <span className={`ops-chg ${bad ? "bad" : ""}`} title="vs the same time last week">{signed(value)}</span>;
}

function Dot({ status }: { status: OpsStatus }) {
  const label = { red: "Red", amber: "Amber", green: "Normal", idle: "Too quiet to judge" }[status];
  return <span className={`ops-dot ${status}`} role="img" aria-label={label} title={label} />;
}

function Mini({ label, value, note, title }: { label: string; value: ReactNode; note?: ReactNode; title?: string }) {
  return (
    <div className="ops-mini" title={title}>
      <span className="ops-mini-label">{label}</span>
      <span className="ops-mini-value">{value}</span>
      {note !== undefined && <span className="ops-mini-note">{note}</span>}
    </div>
  );
}

function Kpi({ label, value, note, tone, onClick, title }: { label: string; value: ReactNode; note: ReactNode; tone?: "ok" | "warn" | "bad"; onClick?: () => void; title?: string }) {
  const inner = (
    <>
      <span className="l">{label}</span>
      <span className={`v ${tone ?? ""}`}>{value}</span>
      <span className="n">{note}</span>
    </>
  );
  return onClick ? <button type="button" className="ops-kpi" onClick={onClick} title={title}>{inner}</button> : <div className="ops-kpi" title={title}>{inner}</div>;
}

function CardHead({ title, sub, right }: { title: string; sub?: ReactNode; right?: ReactNode }) {
  return (
    <div className="ops-card-head">
      <div className="grow"><span className="t">{title}</span>{sub && <span className="hint">{sub}</span>}</div>
      {right}
    </div>
  );
}

const ZONE_PLACE: Record<ClockZone, string> = { ist: "India", pst: "US Pacific", est: "US Eastern" };
function utcOffset(ms: number, z: ClockZone): string {
  const m = offsetMs(ms, z) / 60_000;
  const a = Math.abs(m);
  return `UTC ${m < 0 ? "−" : "+"}${Math.floor(a / 60)}${a % 60 ? `:${String(a % 60).padStart(2, "0")}` : ""}`;
}

/** Freshness + time zone in one control: "● Live 12:14 IST ⌄"; the menu switches the zone. */
function LiveClock({ fresh, zone, onZone }: { fresh: Fresh | null; zone: ClockZone; onZone: (z: ClockZone) => void }) {
  const [open, setOpen] = useState(false);
  const wrap = useRef<HTMLDivElement>(null);
  const items = useRef<(HTMLButtonElement | null)[]>([]);
  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => { if (!wrap.current?.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", away);
    items.current[ZONES.indexOf(zone)]?.focus();
    return () => document.removeEventListener("mousedown", away);
  }, [open, zone]);
  const at = fresh?.asOf ?? null;
  const ref = at ?? Date.now();
  const late = !!fresh && (fresh.stale || !!fresh.error);
  const state = !fresh ? "wait" : fresh.past ? "past" : at === null ? "down" : late ? "late" : "ok";
  const mins = fresh?.ageMs === null || fresh?.ageMs === undefined ? null : Math.round(fresh.ageMs / 60_000);
  const word = { wait: "Loading", past: "Past range", down: "No data yet", late: mins !== null ? `Delayed ${mins} min` : "Delayed", ok: "Live" }[state];
  const pick = (z: ClockZone) => { onZone(z); setOpen(false); };
  const onKey = (e: ReactKeyboardEvent) => {
    const i = items.current.findIndex((el) => el === document.activeElement);
    if (e.key === "Escape") { setOpen(false); (wrap.current?.querySelector(".ops-clock") as HTMLElement | null)?.focus(); }
    else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const n = (i + (e.key === "ArrowDown" ? 1 : ZONES.length - 1)) % ZONES.length;
      items.current[n]?.focus();
    }
  };
  return (
    <div className="ops-clock-wrap" ref={wrap} onKeyDown={onKey}>
      <button type="button" className={`ops-clock ${state}`} aria-haspopup="listbox" aria-expanded={open} onClick={() => setOpen(!open)}
        title={fresh?.error ?? (fresh?.past ? "A past range: the numbers don't change. Click to change the time zone." : at !== null ? `Data to ${clockZ(at, zone, true)}. Copied from CloudWatch every 5 minutes; the page refreshes every minute.` : "Change the time zone")}>
        <span className="pulse" aria-hidden="true" />
        <span className="word">{word}</span>
        <span className="sep" aria-hidden="true" />
        <span className="time">{at !== null ? clock(at, zone) : "--:--"}</span>
        <span className="zn">{zoneName(ref, zone)}</span>
        <Icon name="caret" size={15} className="caret" />
      </button>
      {open && (
        <div className="ops-zone-menu" role="listbox" aria-label="Time zone">
          <div className="ops-zone-menu-head">Time zone</div>
          {ZONES.map((z, i) => (
            <button key={z} ref={(el) => { items.current[i] = el; }} type="button" role="option" aria-selected={z === zone}
              className="ops-zone-item" onClick={() => pick(z)}>
              <span className="code">{ZONE_LABEL[z]}</span>
              <span className="what"><span className="place">{ZONE_PLACE[z]}</span><span className="off">{zoneName(ref, z)} · {utcOffset(ref, z)}</span></span>
              <span className="now">{clock(ref, z)}</span>
              <span className="tick" aria-hidden="true">{z === zone && <Icon name="check" size={16} strokeWidth={2.4} />}</span>
            </button>
          ))}
          <div className="ops-zone-menu-foot">Days run midnight to midnight in this zone, and last week is the same date 7 days back.</div>
        </div>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ page
export function Ops() {
  const [params, setParams] = useSearchParams();
  const tab = (params.get("tab") as Tab) || "live";
  const [fresh, setFresh] = useState<Fresh | null>(null);
  const [zone, setZoneState] = useState<ClockZone>(storedOpsZone());
  const setZone = (z: ClockZone) => { setZoneState(z); try { localStorage.setItem(ZONE_KEY, z); } catch { /* private window */ } };
  const set = (patch: Record<string, string | null>) => {
    const n = new URLSearchParams(params);
    for (const [k, v] of Object.entries(patch)) (v ? n.set(k, v) : n.delete(k));
    setParams(n, { replace: false });
  };
  const openStage = (id: string) => set({ tab: "history", stage: id, minutes: null, view: null });
  return (
    <ZoneCtx.Provider value={zone}>
    <Page crumbs={[{ label: "Ops dashboard" }]} title="Ops dashboard" wide>
      <div className="ops-head">
        <div className="grow">
          <h1>Ops dashboard</h1>
          <p className="sub">API Gateway calls and orders processed, against the same time last week. A day runs midnight to midnight {zone === "ist" ? "India time" : zone === "pst" ? "US Pacific time" : "US Eastern time"} ({ZONE_LABEL[zone]}).</p>
        </div>
        <div className="ops-head-right">
          <LiveClock fresh={fresh} zone={zone} onZone={setZone} />
          <div className="tabs ops-views" role="tablist" aria-label="View">
            {([["live", "Live"], ["health", "Health"], ["history", "History"]] as [Tab, string][]).map(([v, l]) => (
              <button key={v} type="button" role="tab" className="tab" aria-selected={tab === v}
                onClick={() => set({ tab: v === "live" ? null : v, view: null })}>{l}</button>
            ))}
          </div>
        </div>
      </div>
      {tab === "live" && <Live onStage={openStage} onHealth={() => set({ tab: "health", view: "problems" })} onFresh={setFresh} />}
      {tab === "health" && <HealthTab params={params} set={set} onStage={openStage} onFresh={setFresh} />}
      {tab === "history" && <History params={params} set={set} onFresh={setFresh} />}
    </Page>
    </ZoneCtx.Provider>
  );
}

function SourceProblems({ api, orders }: {
  api: { stale: boolean; ageMs: number | null; error: string | null; asOf: number | null };
  orders: { error: string | null; configError: string | null };
}) {
  const me = useMe().data!;
  return (
    <>
      {api.error && <Callout tone="danger" title="Can't read the API Gateway numbers">{api.error}</Callout>}
      {!api.error && api.stale && (
        <Callout tone="warn" title={api.asOf === null ? "No API Gateway numbers for today yet" : `API Gateway numbers are ${Math.round((api.ageMs ?? 0) / 60_000)} minutes old`}>
          They are copied from CloudWatch every 5 minutes by <span className="mono">apigw_metrics_to_s3.py</span> (cron on 172.0.23.77). {me.admin ? "Check ~/apigw-metrics/run.log there." : "Ask an admin to check the copy job."}
        </Callout>
      )}
      {orders.configError && <Callout tone="warn" title="The ops dashboard config has a mistake (the last good version is used)">{orders.configError}</Callout>}
      {orders.error && <Callout tone="warn" title="Counting orders failed last time">{orders.error}</Callout>}
    </>
  );
}

// ------------------------------------------------------------------ live
function Live({ onStage, onHealth, onFresh }: { onStage: (id: string) => void; onHealth: () => void; onFresh: (f: Fresh) => void }) {
  const zone = useZone();
  const [all, setAll] = useState(false);
  const q = useQuery({ queryKey: ["ops-summary", zone], queryFn: () => get<OpsSummary>("/ops/summary", { zone: zone.toUpperCase() }), refetchInterval: 60_000, retry: false, placeholderData: keepPreviousData });
  const d = q.data;
  useEffect(() => { if (d) onFresh(d.api); }, [d, onFresh]);
  const callsCum = useMemo(() => (d ? { now: cumulative(d.api.minutes.calls), lw: cumulative(d.api.minutes.lastWeek) } : null), [d]);
  const ordersCum = useMemo(() => (d?.orders.minutes ? { now: cumulative(d.orders.minutes.counts), lw: cumulative(d.orders.minutes.lastWeek) } : null), [d]);
  if (q.isLoading) return <section className="card"><Loading what="Reading today's numbers…" /></section>;
  if (q.error || !d || !callsCum) return <section className="card"><div className="card-body"><ErrorCallout error={q.error} /></div></section>;
  const { api: a, orders: o } = d;
  const m = a.minutes;
  const rate = lastKnown(m.calls, 5, m.lastWeek);
  const nowPerMin = rate.minutes ? rate.sum / rate.minutes : null;
  const lwPerMin = rate.minutes && rate.other !== null ? rate.other / rate.minutes : null;
  const projected = a.callsLastWeek && a.callsLastWeekDay ? (a.calls * a.callsLastWeekDay) / a.callsLastWeek : null;
  const lastHour = o.minutes ? lastKnown(o.minutes.counts, 60, o.minutes.lastWeek) : null;
  const oProjected = o.total !== null && o.totalLastWeek && o.totalLastWeekDay ? (o.total * o.totalLastWeekDay) / o.totalLastWeek : null;
  const problems = a.stages.filter((s) => s.status === "red" || s.status === "amber").sort((x, y) => (x.status === y.status ? 0 : x.status === "red" ? -1 : 1));
  const e4 = share(a.err4xx, a.calls), e5 = share(a.err5xx, a.calls);
  const stages = all ? a.stages : a.stages.slice(0, 15);
  const maxShare = Math.max(1, ...a.stages.map((s) => s.share ?? 0));
  const weighted = (pick: (s: OpsSummary["api"]["stages"][number]) => number | null) => {
    let w = 0, t = 0;
    for (const s of a.stages) { const v = pick(s); if (v !== null && s.recent.calls) { w += v * s.recent.calls; t += s.recent.calls; } }
    return t ? w / t : null;
  };
  const p95 = { now: weighted((s) => s.recent.p95), lw: weighted((s) => s.recent.p95LastWeek) };
  const seriesLegend = [{ key: "now", label: "Today", color: C_NOW }, { key: "lw", label: "Last week", color: C_LW, dashed: true }];

  return (
    <>
      <SourceProblems api={a} orders={o} />
      <div className="ops-hero-grid">
        <section className="ops-hero" aria-label="API calls today">
          <div className="ops-hero-top">
            <div className="ops-eyebrow"><span>API calls today</span><span className="grow" /><Delta value={a.change} /></div>
            <div className="ops-big-row">
              <span className="ops-big">{num(a.calls)}</span>
              {a.callsLastWeek !== null && <span className="ops-vs">vs <strong>{num(a.callsLastWeek)}</strong> by this time last week</span>}
            </div>
          </div>
          <div className="ops-minis">
            <Mini label="Right now" value={nowPerMin === null ? "—" : `${num(Math.round(nowPerMin))}/min`}
              note={lwPerMin !== null ? `last week ${num(Math.round(lwPerMin))}/min` : "last 5 minutes"} title="Average of the last 5 minutes" />
            <Mini label="Projected today" value={compact(projected)} note={a.callsLastWeekDay ? `last week ${compact(a.callsLastWeekDay)}` : "needs last week"}
              title="Today so far × (last week's whole day ÷ last week by this time)" />
            <Mini label="Busiest minute" value={a.peak ? `${num(a.peak.calls)}/min` : "—"} note={a.peak ? <>at <T ms={a.peak.t} /></> : ""} />
          </div>
          <CardHead title="Running total" right={<Legend series={seriesLegend} />} />
          <LineChart label="API calls added up through the day, today and last week" start={m.start} step={60_000} legend={false} height={150} fmt={compact} zone={zone}
            nowAt={a.asOf} series={[{ key: "now", label: "Today", color: C_NOW, values: callsCum.now, area: true },
              ...(m.lastWeek ? [{ key: "lw", label: "Last week", color: C_LW, values: callsCum.lw, dashed: true }] : [])]} />
          <div className="ops-hero-fill" />
          {a.stages.length > 0 && (
            <div className="ops-services" aria-label="Busiest stages">
              {a.stages.slice(0, 3).map((s) => (
                <div key={s.id} className="ops-svc clickable" role="button" tabIndex={0} style={{ cursor: "pointer" }}
                  onClick={() => onStage(s.id)} onKeyDown={(e) => { if (e.key === "Enter") onStage(s.id); }} title="Open this stage's history">
                  <span className="name">{s.name}<span className="hint">{s.api}</span></span>
                  <Spark now={s.spark.now} lastWeek={s.spark.lastWeek} />
                  <span className="n">{compact(s.calls)}</span>
                  <span className="d"><Chg value={s.change} base={s.callsLastWeek} /></span>
                </div>
              ))}
            </div>
          )}
        </section>

        <section className="ops-hero" aria-label="Orders today">
          <div className="ops-hero-top">
            <div className="ops-eyebrow"><span>Orders today</span><span className="grow" />{o.ready ? <Delta value={o.change} /> : null}</div>
            <div className="ops-big-row">
              <span className="ops-big">{o.ready ? num(o.total) : "…"}</span>
              {o.ready && o.totalLastWeek !== null && <span className="ops-vs">vs <strong>{num(o.totalLastWeek)}</strong> by this time last week</span>}
              {!o.ready && <span className="ops-vs">counting today's logs…</span>}
            </div>
          </div>
          <div className="ops-minis">
            <Mini label="Last hour" value={lastHour ? num(lastHour.sum) : "—"} note={lastHour?.other !== null && lastHour?.other !== undefined ? `last week ${num(lastHour.other)}` : "—"} />
            <Mini label="Projected today" value={compact(oProjected)} note={o.totalLastWeekDay !== null ? `last week ${compact(o.totalLastWeekDay)}` : "needs last week"}
              title="Today so far × (last week's whole day ÷ last week by this time)" />
            <Mini label="Counted to" value={<T ms={o.asOf} />} note={o.lastRun ? `checked ${ago(o.lastRun)}` : "not yet"} title="Orders are counted from the logs every 5 minutes" />
          </div>
          {ordersCum ? (
            <>
              <CardHead title="Running total" right={<Legend series={seriesLegend} />} />
              <LineChart label="Orders added up through the day, today and last week" start={o.minutes!.start} step={60_000} legend={false} height={150} fmt={compact} zone={zone}
                nowAt={o.asOf} series={[{ key: "now", label: "Today", color: C_NOW, values: ordersCum.now, area: true },
                  ...(o.minutes!.lastWeek ? [{ key: "lw", label: "Last week", color: C_LW, values: ordersCum.lw, dashed: true }] : [])]} />
            </>
          ) : <Empty title="Orders are being counted">The first count after a restart takes a few minutes.</Empty>}
          {o.services.length > 0 && (
            <div className="ops-services">
              {o.services.map((s) => (
                <div key={s.service} className="ops-svc">
                  <span className="name">{s.service}<span className="hint">{s.cluster}</span></span>
                  <Spark now={s.hours} lastWeek={s.lastWeekHours} />
                  <span className="n">{num(s.count)}</span>
                  <span className="d"><Chg value={s.change} base={s.lastWeek} /></span>
                </div>
              ))}
            </div>
          )}
        </section>
      </div>

      <div className="ops-kpis">
        <Kpi label="Stage health" onClick={onHealth} title="Open the Health view"
          tone={problems.some((p) => p.status === "red") ? "bad" : problems.length ? "warn" : "ok"}
          value={problems.length ? (
            <>
              {a.statusCounts.red > 0 && <span className="ops-count"><Dot status="red" />{a.statusCounts.red} red</span>}
              {a.statusCounts.amber > 0 && <span className="ops-count"><Dot status="amber" />{a.statusCounts.amber} amber</span>}
            </>
          ) : <span className="ops-count"><Dot status="green" />All normal</span>}
          note={`${a.stages.length} stages · last ${a.windowMinutes} min vs last week`} />
        <Kpi label="4xx rate today" value={pct(e4, 2)} tone={e4 !== null && e4 >= 5 ? "warn" : undefined} note={`${num(a.err4xx)} client errors`} />
        <Kpi label="5xx rate today" value={pct(e5, 2)} tone={e5 !== null && e5 >= 1 ? "bad" : e5 !== null && e5 >= 0.5 ? "warn" : undefined} note={`${num(a.err5xx)} server errors`} />
        <Kpi label={`p95 latency · last ${a.windowMinutes} min`} value={ms(p95.now)} title="Average of the stages' p95, weighted by their calls"
          tone={p95.now !== null && p95.lw && p95.now >= 1.5 * p95.lw && p95.now >= 300 ? "warn" : undefined}
          note={p95.lw ? `last week ${ms(p95.lw)}` : "all stages, weighted by calls"} />
      </div>

      {problems.length > 0 && (
        <section className={`card ops-attn ${problems.some((p) => p.status === "red") ? "" : "amber"}`} aria-label="Needs attention">
          <CardHead title={`Needs attention · ${problems.length}`} sub={`Stages that look different from the same ${a.windowMinutes} minutes last week`}
            right={<button type="button" className="btn btn-sm" onClick={onHealth}>Open health <Icon name="chevron" size={14} /></button>} />
          <div style={{ marginTop: 8 }}>
            {problems.slice(0, 6).map((s) => (
              <div key={s.id} className="ops-attn-row">
                <Dot status={s.status} />
                <span className="who">{s.name}<span className="hint">{s.api}</span></span>
                <span className="why">{s.reasons.join(" · ")}</span>
                <button type="button" className="btn btn-ghost btn-sm" onClick={() => onStage(s.id)}>History</button>
              </div>
            ))}
          </div>
        </section>
      )}

      <div className="ops-charts">
        <section className="card">
          <CardHead title="API calls" sub="per 5 minutes, all stages" right={<Legend series={seriesLegend} />} />
          <LineChart label="API calls per 5 minutes today and last week" start={m.start} step={300_000} legend={false} fmt={compact} nowAt={a.asOf} zone={zone}
            series={[{ key: "now", label: "Today", color: C_NOW, values: bucket(m.calls, 5), area: true },
              ...(m.lastWeek ? [{ key: "lw", label: "Last week", color: C_LW, values: bucket(m.lastWeek, 5), dashed: true }] : [])]} />
        </section>
        <section className="card">
          <CardHead title="Orders" sub="per 15 minutes, every service" right={<Legend series={seriesLegend} />} />
          {o.minutes ? (
            <LineChart label="Orders per 15 minutes today and last week" start={o.minutes.start} step={900_000} legend={false} fmt={compact} nowAt={o.asOf} zone={zone}
              series={[{ key: "now", label: "Today", color: C_NOW, values: bucket(o.minutes.counts, 15), area: true },
                ...(o.minutes.lastWeek ? [{ key: "lw", label: "Last week", color: C_LW, values: bucket(o.minutes.lastWeek, 15), dashed: true }] : [])]} />
          ) : <Empty title="Orders are being counted" />}
        </section>
      </div>

      <section className="card" style={{ overflow: "hidden" }}>
        <CardHead title="Stages" sub={`Busiest first · status and p95 over the last ${a.windowMinutes} minutes · click a row for its history`}
          right={<Legend series={[{ key: "now", label: "Today (per 15 min)", color: C_NOW }, { key: "lw", label: "Last week", color: C_LW, dashed: true }]} />} />
        {a.stages.length === 0 ? <Empty title="No calls yet today" /> : (
          <div className="table-scroll" style={{ maxHeight: "none" }}>
            <table className="table ops-table">
              <thead>
                <tr><th>Stage</th><th>Today vs last week</th><th className="num">Calls today</th><th className="num">Share</th><th className="num">vs last week</th>
                  <th className="num">4xx</th><th className="num">5xx</th><th className="num">p95</th></tr>
              </thead>
              <tbody>
                {stages.map((s) => (
                  <tr key={s.id} className={`clickable ${s.status === "red" ? "problem" : s.status === "amber" ? "problem amber" : ""}`} tabIndex={0}
                    onClick={() => onStage(s.id)} onKeyDown={(e) => { if (e.key === "Enter") onStage(s.id); }} title={s.reasons.join(" · ") || undefined}>
                    <td><div className="stage"><Dot status={s.status} /><div><div className="nm">{s.name}</div><div className="sub" title={s.api}>{s.api}</div></div></div></td>
                    <td><Spark now={s.spark.now} lastWeek={s.spark.lastWeek} width={128} /></td>
                    <td className="num">{num(s.calls)}</td>
                    <td className="num"><span className="ops-share"><span className="ops-bar"><span style={{ width: `${Math.min(100, (100 * (s.share ?? 0)) / maxShare)}%` }} /></span><span className="pc">{pct(s.share)}</span></span></td>
                    <td className="num"><Chg value={s.change} base={s.callsLastWeek} /></td>
                    <td className="num">{s.err4xxPct === null ? <span className="dim">—</span> : pct(s.err4xxPct)}</td>
                    <td className="num">{s.err5xx ? <span className={s.err5xxPct !== null && s.err5xxPct >= 1 ? "danger-text" : ""}>{pct(s.err5xxPct, 2)}</span> : <span className="dim">0</span>}</td>
                    <td className="num" title={s.recent.p95LastWeek ? `last week ${ms(s.recent.p95LastWeek)}` : undefined}>{ms(s.recent.p95)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {a.stages.length > 15 && (
          <div className="card-foot"><button type="button" className="btn btn-ghost btn-sm" onClick={() => setAll(!all)}>{all ? "Show the top 15" : `Show all ${a.stages.length} stages`}</button></div>
        )}
      </section>

      <section className="card" style={{ overflow: "hidden" }}>
        <CardHead title="Orders by store" sub="Unique order ids per service · last week until the same time"
          right={<Legend series={[{ key: "now", label: "Today (per hour)", color: C_NOW }, { key: "lw", label: "Last week", color: C_LW, dashed: true }]} />} />
        {!o.ready ? <Empty title="Orders are being counted" /> : o.stores.length === 0 ? <Empty title="No orders yet today" /> : (
          <div className="table-scroll" style={{ maxHeight: "none" }}>
            <table className="table ops-table">
              <thead><tr><th>Store</th><th>Service</th><th>Today vs last week</th><th className="num">Today</th><th className="num">Last week</th><th className="num">Change</th></tr></thead>
              <tbody>
                {o.stores.map((s) => (
                  <tr key={`${s.service}/${s.store}`}>
                    <td><div className="stage"><div><div className="nm">{s.name}</div>{s.name !== s.store && <div className="sub">{s.store}</div>}</div></div></td>
                    <td>{s.service}</td>
                    <td><Spark now={s.hours} lastWeek={s.lastWeekHours} width={128} /></td>
                    <td className="num" style={{ fontWeight: 600 }}>{num(s.count)}</td>
                    <td className="num">{s.lastWeek === null ? <span className="dim">—</span> : num(s.lastWeek)}</td>
                    <td className="num"><Chg value={s.change} base={s.lastWeek} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </>
  );
}

// ------------------------------------------------------------------ health
function HealthTab({ params, set, onStage, onFresh }: {
  params: URLSearchParams; set: (p: Record<string, string | null>) => void; onStage: (id: string) => void; onFresh: (f: Fresh) => void;
}) {
  const zone = useZone();
  const minutes = Number(params.get("minutes") || 60);
  const problems = params.get("view") === "problems";
  const q = useQuery({ queryKey: ["ops-health", minutes], queryFn: () => get<OpsHealth>("/ops/health", { minutes }), refetchInterval: 60_000, retry: false, placeholderData: keepPreviousData });
  const d = q.data;
  useEffect(() => { if (d) onFresh(d.api); }, [d, onFresh]);
  if (q.isLoading) return <section className="card"><Loading what="Comparing every stage with last week…" /></section>;
  if (q.error || !d) return <section className="card"><div className="card-body"><ErrorCallout error={q.error} /></div></section>;
  const red = d.stages.filter((s) => s.status === "red");
  const bad = d.stages.filter((s) => s.status === "red" || s.status === "amber");
  const rows = problems ? bad : d.stages;
  const th = d.thresholds;
  const win = minutes < 60 ? `${minutes} min` : `${minutes / 60} h`;
  return (
    <>
      <SourceProblems api={d.api} orders={d.orders} />
      <div className="ops-kpis">
        <Kpi label="Problems" onClick={() => set({ view: problems ? null : "problems" })} title={problems ? "Show every stage" : "Show only the problems"}
          tone={red.length ? "bad" : bad.length ? "warn" : "ok"}
          value={bad.length ? (
            <>
              {red.length > 0 && <span className="ops-count"><Dot status="red" />{red.length} red</span>}
              {bad.length - red.length > 0 && <span className="ops-count"><Dot status="amber" />{bad.length - red.length} amber</span>}
            </>
          ) : <span className="ops-count"><Dot status="green" />None</span>}
          note={`of ${d.stages.length} stages with calls`} />
        <Kpi label="Window" value={<span title={`${clockZ(d.from, zone, true)} to ${clockZ(d.to, zone, true)}`}>{clock(d.from, zone)}–{clock(d.to, zone)} <span style={{ fontSize: 13, fontWeight: 600, color: "var(--muted)" }}>{zoneName(d.to, zone)}</span></span>} note={d.lastWeekAvailable ? "vs the same minutes last week" : "last week not available"} />
        <Kpi label="API data to" value={<T ms={d.api.asOf} />} tone={d.api.stale || d.api.error ? "warn" : undefined}
          note={d.api.ageMs === null ? "no data yet" : `${Math.round(d.api.ageMs / 60_000)} min ago · copied every 5 min`} />
        <Kpi label="Orders counted" value={d.orders.lastRun ? ago(d.orders.lastRun) : "not yet"} tone={d.orders.error ? "warn" : undefined}
          note={d.orders.newestMs ? <>logs to <T ms={d.orders.newestMs} /> · every 5 min</> : "every 5 minutes"} />
      </div>


      <section className="card" style={{ overflow: "hidden" }}>
        <div className="ops-card-head" style={{ paddingBottom: 12 }}>
          <div className="tabs" role="tablist" aria-label="Show">
            <button type="button" role="tab" className="tab" aria-selected={!problems} onClick={() => set({ view: null })}>All stages · {d.stages.length}</button>
            <button type="button" role="tab" className="tab" aria-selected={problems} onClick={() => set({ view: "problems" })}>Problems · {bad.length}</button>
          </div>
          <div className="grow" />
          <span className="hint" style={{ alignSelf: "center" }}>Last {win} · grey numbers: the same {win} last week</span>
          <div className="tabs" role="group" aria-label="Window">
            {[15, 60, 180].map((mm) => (
              <button key={mm} type="button" className="tab" aria-pressed={minutes === mm} aria-selected={minutes === mm} onClick={() => set({ minutes: mm === 60 ? null : String(mm) })}>
                {mm < 60 ? `${mm} min` : `${mm / 60} h`}
              </button>
            ))}
          </div>
        </div>
        {rows.length === 0 ? <Empty title={problems ? "No problems: every stage looks like last week" : "No calls in this window"} /> : (
          <div className="table-scroll" style={{ maxHeight: "none" }}>
            <table className="table ops-table">
              <thead>
                <tr>
                  <th>Stage</th><th>Why</th><th className="num">Calls</th><th className="num">4xx</th><th className="num">5xx</th>
                  <th className="num">p95</th><th className="num" title="IntegrationLatency p95: time spent in the backend">Backend p95</th>
                  <th className="num" title="p95 latency minus backend p95: roughly the time API Gateway itself adds">Gateway</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((s) => {
                  const lw = s.lastWeek;
                  return (
                    <tr key={s.id} className={`clickable ${s.status === "red" ? "problem" : s.status === "amber" ? "problem amber" : ""}`} tabIndex={0}
                      onClick={() => onStage(s.id)} onKeyDown={(e) => { if (e.key === "Enter") onStage(s.id); }} title="Open this stage's history">
                      <td><div className="stage"><Dot status={s.status} /><div><div className="nm">{s.name}</div><div className="sub" title={s.api}>{s.api}</div></div></div></td>
                      <td style={{ maxWidth: 280, fontSize: 13 }}>{s.reasons.length ? s.reasons.join(" · ") : <span className="dim">{s.status === "idle" ? "too few calls to judge" : "like last week"}</span>}</td>
                      <td className="num">{num(s.now.calls)}<span className="ops-sub2">{lw ? <>{num(lw.calls)} · <Chg value={s.change} base={lw.calls} /></> : "—"}</span></td>
                      <td className="num">{pct(s.now.err4xxPct)}<span className="ops-sub2">{lw ? pct(lw.err4xxPct) : "—"}</span></td>
                      <td className="num">
                        <span className={s.now.err5xxPct !== null && s.now.err5xxPct >= th.err5xxRedPct ? "danger-text" : s.now.err5xxPct !== null && s.now.err5xxPct >= th.err5xxAmberPct ? "warn-text" : ""}>{pct(s.now.err5xxPct, 2)}</span>
                        <span className="ops-sub2">{lw ? pct(lw.err5xxPct, 2) : "—"}</span>
                      </td>
                      <td className="num">{ms(s.now.p95)}<span className="ops-sub2">{lw ? ms(lw.p95) : "—"}</span></td>
                      <td className="num">{ms(s.now.integrationP95)}<span className="ops-sub2">{lw ? ms(lw.integrationP95) : "—"}</span></td>
                      <td className="num">{ms(s.now.gatewayMs)}<span className="ops-sub2">{lw ? ms(lw.gatewayMs) : "—"}</span></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        <div className="card-foot hint" style={{ lineHeight: 1.5 }}>
          Red: 5xx ≥ {th.err5xxRedPct}%, p95 ≥ {th.p95RedTimes}× last week (and ≥ {th.p95MinMs} ms), or no calls.
          Amber: 5xx ≥ {th.err5xxAmberPct}%, p95 ≥ {th.p95AmberTimes}×, 4xx ≥ {th.err4xxTimesLastWeek}× last week (and ≥ {th.err4xxMinPct}%), or calls below {th.quietPct}% of last week.
          Fewer than {th.minCalls} calls: too quiet to judge. The rules are in config/ops-dashboard.yaml.
        </div>
      </section>
    </>
  );
}

// ------------------------------------------------------------------ history
function ChartCard({ title, sub, series, ...rest }: { title: string; sub: string; series: LineSeries[] } & Omit<Parameters<typeof LineChart>[0], "series" | "label" | "legend" | "zone">) {
  const zone = useZone();
  return (
    <section className="card">
      <CardHead title={title} sub={sub} right={<Legend series={series} />} />
      <LineChart label={`${title} ${sub}`} series={series} legend={false} zone={zone} {...rest} />
    </section>
  );
}

type Preset = "1h" | "6h" | "24h" | "today" | "yesterday" | "7d" | "30d" | "custom";
const PRESETS: [Preset, string][] = [["1h", "1 h"], ["6h", "6 h"], ["24h", "24 h"], ["today", "Today"], ["yesterday", "Yesterday"], ["7d", "7 days"], ["30d", "30 days"]];
const MAX_DAYS = 31;
const H = 3600_000;

/** Epoch ms of a wall-clock time in the zone. */
function zonedMs(y: number, mo: number, d: number, h: number, mi: number, zone: ClockZone): number {
  const guess = Date.UTC(y, mo - 1, d, h, mi);
  const first = guess - offsetMs(guess, zone);
  return guess - offsetMs(first, zone);
}
function midnight(day: string, zone: ClockZone): number {
  const [y, mo, d] = day.split("-").map(Number);
  return zonedMs(y, mo, d, 0, 0, zone);
}
/** "2026-11-11T09:30" for <input type="datetime-local">, in the zone. */
function toInput(ms: number, zone: ClockZone): string {
  return localStamp(ms, zone).slice(0, 16);
}
function fromInput(v: string, zone: ClockZone): number | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(v);
  return m ? zonedMs(+m[1], +m[2], +m[3], +m[4], +m[5], zone) : null;
}
function presetRange(p: Preset, zone: ClockZone, now: number): [number, number] {
  const today = midnight(dayIn(zone, now), zone);
  switch (p) {
    case "1h": return [now - H, now];
    case "6h": return [now - 6 * H, now];
    case "24h": return [now - 24 * H, now];
    case "yesterday": return [midnight(dayIn(zone, today - 1), zone), today];
    case "7d": return [now - 7 * 24 * H, now];
    case "30d": return [now - 30 * 24 * H, now];
    default: return [today, now];
  }
}
const spanText = (ms: number) => {
  const m = Math.round(ms / 60_000);
  if (m < 60) return `${m} min`;
  if (m < 48 * 60) return `${+(m / 60).toFixed(1)} h`;
  return `${+(m / 1440).toFixed(1)} days`;
};
const stepText = (step: number) => (step === 60_000 ? "per minute" : step < H ? `per ${step / 60_000} minutes` : "per hour");

type SortKey = "name" | "calls" | "share" | "err4xxPct" | "err5xxPct" | "p50" | "p95" | "integrationP95" | "gatewayMs";

function History({ params, set, onFresh }: { params: URLSearchParams; set: (p: Record<string, string | null>) => void; onFresh: (f: Fresh) => void }) {
  const zone = useZone();
  const preset = (params.get("range") as Preset) || "today";
  const stage = params.get("stage") || "";
  const service = params.get("service") || "";
  const store = params.get("store") || "";
  // presets that end "now" move forward every minute
  const [now, setNow] = useState(() => Math.floor(Date.now() / 60_000) * 60_000);
  const live = preset !== "custom" && preset !== "yesterday";
  useEffect(() => {
    if (!live) return;
    const t = setInterval(() => setNow(Math.floor(Date.now() / 60_000) * 60_000), 60_000);
    return () => clearInterval(t);
  }, [live]);
  const [from, to] = preset === "custom"
    ? [Number(params.get("from")) || now - 24 * H, Number(params.get("to")) || now]
    : presetRange(preset, zone, now);
  const [fromText, setFromText] = useState(toInput(from, zone));
  const [toText, setToText] = useState(toInput(to, zone));
  useEffect(() => { setFromText(toInput(from, zone)); setToText(toInput(to, zone)); }, [from, to, zone]);
  const [sort, setSort] = useState<{ key: SortKey; desc: boolean }>({ key: "calls", desc: true });
  const [all, setAll] = useState(false);

  const q = useQuery({
    queryKey: ["ops-range", from, to, stage, service, store, zone],
    queryFn: () => get<OpsRange>("/ops/range", { from, to, stage, service, store, zone: zone.toUpperCase() }),
    retry: false, placeholderData: keepPreviousData, staleTime: 30_000,
  });
  const d = q.data;
  useEffect(() => {
    if (!d) return;
    const at = d.api.asOf;
    const age = at === null ? null : Math.max(0, Date.now() - at - 60_000);
    onFresh(live ? { asOf: at, ageMs: age, stale: age === null || age > 15 * 60_000, error: null } : { asOf: at, ageMs: null, stale: false, error: null, past: true });
  }, [d, live, onFresh]);

  const fFrom = fromInput(fromText, zone), fTo = fromInput(toText, zone);
  const customErr = fFrom === null || fTo === null ? "Pick both times" : fTo <= fFrom ? "The end must be after the start"
    : fTo - fFrom > MAX_DAYS * 24 * H ? `Pick at most ${MAX_DAYS} days` : null;
  const dirty = fFrom !== from || fTo !== to;
  const apply = () => { if (!customErr && fFrom !== null && fTo !== null) set({ range: "custom", from: String(fFrom), to: String(fTo) }); };

  const stageOpts = useMemo(() => (d?.stages ?? []).map((s) => ({ value: s.id, label: `${s.name} · ${s.api}` })), [d]);
  const storeOpts = useMemo(() => {
    const seen = new Map<string, string>();
    for (const s of d?.orders.stores ?? []) if (!service || s.service === service) seen.set(s.store, s.name);
    return [...seen].map(([value, label]) => ({ value, label: label === value ? value : `${label} · ${value}` }));
  }, [d, service]);
  const rows = useMemo(() => {
    const list = [...(d?.stages ?? [])];
    const k = sort.key;
    list.sort((x, y) => {
      const a = x[k], b = y[k];
      if (a === b) return y.calls - x.calls;
      if (a === null || a === undefined) return 1;
      if (b === null || b === undefined) return -1;
      const c = typeof a === "string" ? a.localeCompare(b as string) : (a as number) - (b as number);
      return sort.desc ? -c : c;
    });
    return list;
  }, [d, sort]);

  const exportCsv = () => {
    if (!d) return;
    const head = ["from", "to", "stage_id", "api", "stage", "name", "calls", "share_pct", "err_4xx", "err_4xx_pct", "err_5xx", "err_5xx_pct", "p50_ms", "p95_ms", "backend_p95_ms", "gateway_ms"];
    const esc = (v: unknown) => { const t = v === null || v === undefined ? "" : String(v); return /[",\n]/.test(t) ? `"${t.replace(/"/g, '""')}"` : t; };
    const f = localStamp(d.from, zone), t = localStamp(d.to, zone);
    const lines = [head.join(","), ...rows.map((r) => [f, t, r.id, r.api, r.stage, r.name, r.calls, r.share, r.err4xx, r.err4xxPct, r.err5xx, r.err5xxPct, r.p50, r.p95, r.integrationP95, r.gatewayMs].map(esc).join(","))];
    downloadFile(`ops-stages-${toInput(d.from, zone).replace(/[:T]/g, "")}-${toInput(d.to, zone).replace(/[:T]/g, "")}-${zone}.csv`, lines.join("\n") + "\n", "text/csv");
  };

  const a = d?.api;
  const sel = stage ? stageOpts.find((s) => s.value === stage)?.label.split(" · ")[0] ?? stage : null;
  const longWait = to - from > 3 * 24 * H;
  const shown = all ? rows : rows.slice(0, 25);
  const nowAt = d && live ? a?.asOf ?? null : null;
  const Th = ({ k, children, num: isNum = true, title }: { k: SortKey; children: ReactNode; num?: boolean; title?: string }) => (
    <th className={isNum ? "num" : undefined} aria-sort={sort.key === k ? (sort.desc ? "descending" : "ascending") : "none"} title={title}>
      <button type="button" className={`ops-sort ${sort.key === k ? "on" : ""}`} onClick={() => setSort({ key: k, desc: sort.key === k ? !sort.desc : k !== "name" })}>
        {children}<span className="ar" aria-hidden="true">{sort.key === k ? (sort.desc ? "↓" : "↑") : "↕"}</span>
      </button>
    </th>
  );

  return (
    <>
      <section className="card ops-range-card">
        <div className="ops-range-top">
          <div className="tabs ops-presets" role="group" aria-label="Time range">
            {PRESETS.map(([k, l]) => (
              <button key={k} type="button" className="tab" aria-pressed={preset === k} aria-selected={preset === k}
                onClick={() => set({ range: k === "today" ? null : k, from: null, to: null })}>{l}</button>
            ))}
            <button type="button" className="tab" aria-pressed={preset === "custom"} aria-selected={preset === "custom"} onClick={apply} disabled={!!customErr}>Custom</button>
          </div>
          <div className="ops-range-pick">
            <label className="ops-dt"><span>From</span>
              <input type="datetime-local" className="input" value={fromText} max={toText} onChange={(e) => setFromText(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter") apply(); }} />
            </label>
            <span className="ops-dt-arrow" aria-hidden="true">→</span>
            <label className="ops-dt"><span>To</span>
              <input type="datetime-local" className="input" value={toText} min={fromText} onChange={(e) => setToText(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter") apply(); }} />
            </label>
            <span className="ops-dt-zone" title={ZONE_TITLE[zone]}>{zoneName(from, zone)}</span>
            {dirty && <button type="button" className="btn btn-primary btn-sm" onClick={apply} disabled={!!customErr} title={customErr ?? "Show this range"}>Apply</button>}
          </div>
        </div>
        {dirty && customErr && <div className="ops-range-err">{customErr}</div>}
        <div className="ops-toolbar">
          <SearchSelect id="ops-stage" label="Stage" value={stage} options={stageOpts} allLabel="All stages" onChange={(v) => set({ stage: v || null })} maxWidth={340} />
          <select aria-label="Order service" className="select" value={service} onChange={(e) => set({ service: e.target.value || null, store: null })}>
            <option value="">All order services</option>
            {(d?.services ?? []).map((s) => <option key={s.service} value={s.service}>{s.service} · {s.cluster}</option>)}
          </select>
          <SearchSelect id="ops-store" label="Store" value={store} options={storeOpts} allLabel="All stores" onChange={(v) => set({ store: v || null })} maxWidth={300} />
          <div className="grow" />
          <span className="hint ops-range-span">{dateLabel(from, zone)} {clock(from, zone)} – {dateLabel(to, zone)} {clock(to, zone)} {zoneName(to, zone)} · {spanText(to - from)}</span>
          <button type="button" className="btn btn-sm" onClick={exportCsv} disabled={!d || !d.stages.length} title="Every stage's numbers for this range"><Icon name="download" size={15} /> Download CSV</button>
        </div>
        {d && a && (
          <div className="ops-stats ops-stats-5">
            <Mini label={sel ? `Calls · ${sel}` : "Calls · all stages"} value={num(a.calls)} note={a.asOf ? <>data to <T ms={a.asOf} /></> : "no data in this range"} />
            <Mini label="4xx rate" value={pct(share(a.err4xx, a.calls), 2)} note={`${num(a.err4xx)} client errors`} />
            <Mini label="5xx rate" value={pct(share(a.err5xx, a.calls), 2)} note={`${num(a.err5xx)} server errors`} />
            <Mini label="p95 latency" value={ms(a.p95)} note="weighted by calls" />
            <Mini label={`Orders · ${store ? storeOpts.find((s) => s.value === store)?.label.split(" · ")[0] ?? store : service || "all services"}`}
              value={d.orders.counted ? num(d.orders.total) : "—"} note={d.orders.counted ? (d.orders.missingDays.length ? "part of the range" : "unique order ids") : `kept ${d.orders.keptDays} days`} />
          </div>
        )}
      </section>
      {q.isLoading ? <section className="card"><Loading what={longWait ? `Reading ${spanText(to - from)} of numbers… the first time can take up to a minute` : "Reading the numbers…"} /></section>
        : q.error || !d || !a ? <section className="card"><div className="card-body"><ErrorCallout error={q.error} /></div></section> : (
          <>
            {!a.hasData && <Callout tone="neutral" title="No API Gateway numbers in this range">The per-minute copies start on 1 Nov 2026.</Callout>}
            {d.orders.counted && d.orders.missingDays.length > 0 && (
              <Callout tone="neutral" title="Orders are only counted for part of this range">Orders are kept for the last {d.orders.keptDays} days; not counted: {d.orders.missingDays[0]}{d.orders.missingDays.length > 1 ? ` to ${d.orders.missingDays[d.orders.missingDays.length - 1]}` : ""} (UTC days).</Callout>
            )}
            <div className={`ops-charts ${q.isFetching ? "ops-fetching" : ""}`}>
              <ChartCard title="API calls" sub={stepText(d.step)} start={d.start} step={d.step} fmt={compact} nowAt={nowAt}
                series={[{ key: "now", label: sel ?? "All stages", color: C_NOW, values: a.series.calls, area: true }]} />
              {d.orders.series ? (
                <ChartCard title="Orders" sub={stepText(d.step)} start={d.start} step={d.step} fmt={compact} nowAt={nowAt}
                  series={[{ key: "now", label: store || service || "All services", color: C_NOW, values: d.orders.series, area: true }]} />
              ) : (
                <section className="card"><CardHead title="Orders" sub={stepText(d.step)} /><Empty title="Orders were not counted for this range">Orders are kept for the last {d.orders.keptDays} days.</Empty></section>
              )}
              <ChartCard title="Errors" sub={stepText(d.step)} start={d.start} step={d.step} fmt={compact} nowAt={nowAt}
                series={[{ key: "4xx", label: "4xx", color: C_4XX, values: a.series.err4xx }, { key: "5xx", label: "5xx", color: C_5XX, values: a.series.err5xx }]} />
              <ChartCard title="Latency p95" sub={`${stepText(d.step)}, weighted by calls`} start={d.start} step={d.step} fmt={(v) => ms(v)} nowAt={nowAt}
                series={[{ key: "p95", label: "p95", color: C_NOW, values: a.series.p95 }, { key: "backend", label: "Backend", color: C_BACKEND, values: a.series.integrationP95 }]} />
            </div>

            <section className="card" style={{ overflow: "hidden" }}>
              <CardHead title={`Stages · ${rows.length}`} sub={`${spanText(d.to - d.from)} · click a column to sort, a row to chart only that stage`}
                right={<button type="button" className="btn btn-ghost btn-sm" onClick={exportCsv} disabled={!rows.length}><Icon name="download" size={15} /> CSV</button>} />
              {rows.length === 0 ? <Empty title="No calls in this range" /> : (
                <div className="table-scroll" style={{ maxHeight: "none" }}>
                  <table className="table ops-table">
                    <thead>
                      <tr>
                        <Th k="name" num={false}>Stage</Th><Th k="calls">Calls</Th><Th k="share">Share</Th><Th k="err4xxPct">4xx</Th><Th k="err5xxPct">5xx</Th>
                        <Th k="p50">p50</Th><Th k="p95">p95</Th><Th k="integrationP95" title="IntegrationLatency p95: time spent in the backend">Backend p95</Th>
                        <Th k="gatewayMs" title="p95 minus backend p95: roughly the time API Gateway itself adds">Gateway</Th>
                      </tr>
                    </thead>
                    <tbody>
                      {shown.map((s) => (
                        <tr key={s.id} className={`clickable ${stage === s.id ? "selected" : ""}`} tabIndex={0} aria-selected={stage === s.id}
                          onClick={() => set({ stage: stage === s.id ? null : s.id })} onKeyDown={(e) => { if (e.key === "Enter") set({ stage: stage === s.id ? null : s.id }); }}
                          title={stage === s.id ? "Show all stages in the charts" : "Chart only this stage"}>
                          <td><div className="stage"><div><div className="nm">{s.name}</div><div className="sub" title={s.api}>{s.api}</div></div></div></td>
                          <td className="num" style={{ fontWeight: 600 }}>{num(s.calls)}</td>
                          <td className="num"><span className="ops-share"><span className="ops-bar"><span style={{ width: `${Math.min(100, s.share ?? 0)}%` }} /></span><span className="pc">{pct(s.share)}</span></span></td>
                          <td className="num">{s.err4xx ? pct(s.err4xxPct, 2) : <span className="dim">0</span>}</td>
                          <td className="num">{s.err5xx ? <span className={s.err5xxPct !== null && s.err5xxPct >= 1 ? "danger-text" : s.err5xxPct !== null && s.err5xxPct >= 0.5 ? "warn-text" : ""}>{pct(s.err5xxPct, 2)}</span> : <span className="dim">0</span>}</td>
                          <td className="num">{ms(s.p50)}</td>
                          <td className="num">{ms(s.p95)}</td>
                          <td className="num">{ms(s.integrationP95)}</td>
                          <td className="num">{ms(s.gatewayMs)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              {rows.length > 25 && (
                <div className="card-foot"><button type="button" className="btn btn-ghost btn-sm" onClick={() => setAll(!all)}>{all ? "Show the top 25" : `Show all ${rows.length} stages`}</button></div>
              )}
            </section>
          </>
        )}
    </>
  );
}
