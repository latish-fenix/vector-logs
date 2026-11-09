import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { get, type OpsHealth, type OpsHistory, type OpsStatus, type OpsSummary } from "../api";
import { Icon, type IconName } from "../components/icons";
import { bucket, LineChart } from "../components/LineChart";
import { Page } from "../components/Shell";
import { Callout, Empty, ErrorCallout, Loading, SearchSelect, downloadFile } from "../components/ui";
import { ago, fmtTime, num } from "../format";
import { useMe } from "../session";

// API Gateway calls per stage (from the per-minute CloudWatch copies in S3) and orders processed by
// OMS / CRS (counted from the Vector logs), against the same time last week. Everything is UTC.

type Tab = "live" | "health" | "history";

const C_NOW = "var(--accent)";
const C_LW = "var(--faint)";
const C_4XX = "var(--st-4xx)";
const C_5XX = "var(--st-5xx)";
const C_BACKEND = "var(--st-3xx, #8b7fe0)";

const todayUtc = () => new Date().toISOString().slice(0, 10);
const hhmm = (ms: number | null | undefined) => (ms === null || ms === undefined ? "—" : fmtTime(ms, "utc", false).slice(11, 16));
const pct = (v: number | null | undefined, digits = 1) => (v === null || v === undefined ? "—" : `${v.toFixed(digits)}%`);
const ms = (v: number | null | undefined) => (v === null || v === undefined ? "—" : v >= 1000 ? `${(v / 1000).toFixed(2)} s` : `${Math.round(v)} ms`);
const share = (part: number, whole: number) => (whole ? (100 * part) / whole : null);
const compact = (v: number) => (v >= 1e6 ? `${(v / 1e6).toFixed(v >= 1e7 ? 0 : 1)}M` : v >= 1e4 ? `${Math.round(v / 1e3)}k` : num(Math.round(v)));

function Change({ value, invert }: { value: number | null | undefined; invert?: boolean }) {
  if (value === null || value === undefined) return <span className="hint">—</span>;
  const up = value > 0;
  const bad = invert ? up && value >= 25 : !up && value <= -25;
  return (
    <span className={`ops-chg ${value === 0 ? "" : up ? "up" : "down"} ${bad ? "bad" : ""}`} title="Compared with the same time last week">
      {value === 0 ? "±0%" : `${up ? "▲" : "▼"} ${Math.abs(value) >= 100 ? Math.round(Math.abs(value)) : Math.abs(value).toFixed(1)}%`}
    </span>
  );
}

const PILL: Record<OpsStatus, { tone: string; icon: IconName; label: string }> = {
  red: { tone: "down", icon: "alert", label: "Red" },
  amber: { tone: "degraded", icon: "warn", label: "Amber" },
  green: { tone: "ok", icon: "check", label: "Green" },
  idle: { tone: "none", icon: "minus", label: "Quiet" },
};

function StatusPill({ status, reasons }: { status: OpsStatus; reasons?: string[] }) {
  const p = PILL[status];
  const title = status === "idle" ? "Too few calls (now and last week) to judge" : reasons?.length ? reasons.join(" · ") : "Like last week";
  return <span className={`hpill ${p.tone}`} title={title}><Icon name={p.icon} size={13} strokeWidth={2.4} />{p.label}</span>;
}

function Tile({ label, value, note, tone, onClick }: { label: string; value: React.ReactNode; note: React.ReactNode; tone?: string; onClick?: () => void }) {
  const inner = (
    <>
      <span className="stat-label">{label}</span>
      <span className={`stat-value ${tone ? `tone-${tone}` : ""}`}>{value}</span>
      <span className="stat-note">{note}</span>
    </>
  );
  return onClick ? <button type="button" className="stat htile" onClick={onClick}>{inner}</button> : <div className="stat htile">{inner}</div>;
}

export function Ops() {
  const [params, setParams] = useSearchParams();
  const tab = (params.get("tab") as Tab) || "live";
  const set = (patch: Record<string, string | null>) => {
    const n = new URLSearchParams(params);
    for (const [k, v] of Object.entries(patch)) (v ? n.set(k, v) : n.delete(k));
    setParams(n, { replace: false });
  };
  const openStage = (id: string) => set({ tab: "history", stage: id, day: null });
  return (
    <Page crumbs={[{ label: "Ops dashboard" }]} title="Ops dashboard" wide>
      <div className="page-head">
        <div className="grow">
          <h1>Ops dashboard</h1>
          <p className="sub">API Gateway calls per stage and orders processed by OMS and CRS, against the same time last week. All times are UTC; “today” is the UTC day.</p>
        </div>
      </div>
      <div className="tabs" role="tablist" aria-label="Page" style={{ alignSelf: "flex-start" }}>
        {([["live", "Live"], ["health", "Health"], ["history", "History"]] as [Tab, string][]).map(([v, l]) => (
          <button key={v} type="button" role="tab" className="tab" aria-selected={tab === v} onClick={() => set({ tab: v === "live" ? null : v })}>{l}</button>
        ))}
      </div>
      {tab === "live" && <Live onStage={openStage} onHealth={() => set({ tab: "health" })} />}
      {tab === "health" && <HealthTab params={params} set={set} onStage={openStage} />}
      {tab === "history" && <History params={params} set={set} />}
    </Page>
  );
}

// ------------------------------------------------------------------ problems with the data itself
function SourceProblems({ api, orders }: {
  api: { stale: boolean; ageMs: number | null; error: string | null; asOf: number | null };
  orders: { error: string | null; configError: string | null; lastRun: string | null };
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
function Live({ onStage, onHealth }: { onStage: (id: string) => void; onHealth: () => void }) {
  const [all, setAll] = useState(false);
  const q = useQuery({ queryKey: ["ops-summary"], queryFn: () => get<OpsSummary>("/ops/summary"), refetchInterval: 60_000, retry: false, placeholderData: keepPreviousData });
  if (q.isLoading) return <section className="card"><Loading what="Reading today's numbers…" /></section>;
  if (q.error || !q.data) return <section className="card"><div className="card-body"><ErrorCallout error={q.error} /></div></section>;
  const { api: a, orders: o } = q.data;
  const m = a.minutes;
  const sc = a.statusCounts;
  const stages = all ? a.stages : a.stages.slice(0, 15);
  const e4 = share(a.err4xx, a.calls), e5 = share(a.err5xx, a.calls);
  return (
    <>
      <p className="hint" style={{ marginTop: -4 }}>
        API numbers to <strong>{hhmm(a.asOf)}</strong> · orders to <strong>{hhmm(o.asOf)}</strong> (counted {ago(o.lastRun)}) · this page refreshes every minute
      </p>
      <SourceProblems api={a} orders={o} />
      <div className="stats htiles">
        <Tile label="API calls today" value={num(a.calls)}
          note={a.callsLastWeek !== null ? <><Change value={a.change} /> vs {num(a.callsLastWeek)} by this time last week</> : "last week not available yet"} />
        <Tile label="Orders today" value={o.ready ? num(o.total) : "…"}
          note={!o.ready ? "counting the logs…" : o.lastWeekReady ? <><Change value={o.change} /> vs {num(o.totalLastWeek)} by this time last week</> : "last week still being counted"} />
        <Tile label="4xx today" value={pct(e4, 2)} note={`${num(a.err4xx)} client errors`} tone={e4 && e4 >= 5 ? "degraded" : undefined} />
        <Tile label="5xx today" value={pct(e5, 2)} note={`${num(a.err5xx)} server errors`} tone={e5 && e5 >= 1 ? "down" : e5 ? "degraded" : "ok"} />
        <Tile label="Stages" value={<>{sc.red ? <span className="danger-text">{sc.red} red</span> : null}{sc.red && sc.amber ? " · " : null}{sc.amber ? <span className="warn-text">{sc.amber} amber</span> : null}{!sc.red && !sc.amber ? "All green" : null}</>}
          note={`last ${a.windowMinutes} min vs last week · ${a.stages.length} stages`} tone={sc.red ? "down" : sc.amber ? "degraded" : "ok"} onClick={onHealth} />
      </div>

      <div className="grid-2">
        <section className="card">
          <div className="card-head"><div className="grow"><strong>API calls</strong><span className="hint">per 5 minutes, all stages</span></div></div>
          <LineChart label="API calls per 5 minutes today and last week" start={m.start} step={300_000} fmt={compact}
            series={[{ key: "now", label: "Today", color: C_NOW, values: bucket(m.calls, 5), area: true },
              ...(m.lastWeek ? [{ key: "lw", label: "Same day last week", color: C_LW, values: bucket(m.lastWeek, 5), dashed: true }] : [])]} />
        </section>
        <section className="card">
          <div className="card-head">
            <div className="grow"><strong>Orders</strong><span className="hint">per 15 minutes, every service</span></div>
            {o.services.map((s) => <span key={s.service} className="badge" title={s.cluster}>{s.service} <strong>{num(s.count)}</strong></span>)}
          </div>
          {o.minutes ? (
            <LineChart label="Orders per 15 minutes today and last week" start={o.minutes.start} step={900_000} fmt={compact}
              series={[{ key: "now", label: "Today", color: C_NOW, values: bucket(o.minutes.counts, 15), area: true },
                ...(o.minutes.lastWeek ? [{ key: "lw", label: "Same day last week", color: C_LW, values: bucket(o.minutes.lastWeek, 15), dashed: true }] : [])]} />
          ) : <Empty title="Orders are being counted">The first count reads today's logs; it takes a few minutes after a restart.</Empty>}
        </section>
      </div>

      <section className="card" style={{ overflow: "hidden" }}>
          <div className="card-head">
            <div className="grow"><strong>Stages</strong><span className="hint">busiest first · status and p95 from the last {a.windowMinutes} minutes · click for history</span></div>
          </div>
          {a.stages.length === 0 ? <Empty title="No calls yet today" /> : (
            <div className="table-scroll">
              <table className="table htable">
                <thead><tr><th>Status</th><th>Stage</th><th className="num">Calls today</th><th className="num">vs last week</th><th className="num">4xx</th><th className="num">5xx</th><th className="num">p95</th></tr></thead>
                <tbody>
                  {stages.map((s) => (
                    <tr key={s.id} className="clickable" tabIndex={0} onClick={() => onStage(s.id)} onKeyDown={(e) => { if (e.key === "Enter") onStage(s.id); }}>
                      <td><StatusPill status={s.status} reasons={s.reasons} /></td>
                      <td><div style={{ fontWeight: 600 }}>{s.name}</div><div className="hint mono">{s.api}</div></td>
                      <td className="num">{num(s.calls)}</td>
                      <td className="num"><Change value={s.change} /></td>
                      <td className="num">{pct(s.err4xxPct)}</td>
                      <td className="num">{s.err5xx ? <span className={s.err5xxPct && s.err5xxPct >= 1 ? "danger-text" : ""}>{pct(s.err5xxPct, 2)}</span> : <span className="hint">0</span>}</td>
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
          <div className="card-head"><div className="grow"><strong>Orders by store</strong><span className="hint">unique order ids per service · last week until the same time</span></div></div>
          {!o.ready ? <Empty title="Orders are being counted" /> : o.stores.length === 0 ? <Empty title="No orders yet today" /> : (
            <div className="table-scroll">
              <table className="table htable">
                <thead><tr><th>Store</th><th>Service</th><th className="num">Today</th><th className="num">Last week</th><th className="num">Change</th></tr></thead>
                <tbody>
                  {o.stores.map((s) => (
                    <tr key={`${s.service}/${s.store}`}>
                      <td><div style={{ fontWeight: 600 }}>{s.name}</div>{s.name !== s.store && <div className="hint mono">{s.store}</div>}</td>
                      <td>{s.service}</td>
                      <td className="num">{num(s.count)}</td>
                      <td className="num">{s.lastWeek === null ? "—" : num(s.lastWeek)}</td>
                      <td className="num"><Change value={s.change} /></td>
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
function HealthTab({ params, set, onStage }: { params: URLSearchParams; set: (p: Record<string, string | null>) => void; onStage: (id: string) => void }) {
  const minutes = Number(params.get("minutes") || 60);
  const problems = params.get("view") === "problems";
  const q = useQuery({ queryKey: ["ops-health", minutes], queryFn: () => get<OpsHealth>("/ops/health", { minutes }), refetchInterval: 60_000, retry: false, placeholderData: keepPreviousData });
  if (q.isLoading) return <section className="card"><Loading what="Comparing every stage with last week…" /></section>;
  if (q.error || !q.data) return <section className="card"><div className="card-body"><ErrorCallout error={q.error} /></div></section>;
  const d = q.data;
  const bad = d.stages.filter((s) => s.status === "red" || s.status === "amber");
  const rows = problems ? bad : d.stages;
  const th = d.thresholds;
  return (
    <>
      <SourceProblems api={d.api} orders={d.orders} />
      <div className="stats htiles">
        <Tile label="Problems" value={bad.length ? `${bad.length}` : "None"} tone={d.stages.some((s) => s.status === "red") ? "down" : bad.length ? "degraded" : "ok"}
          note={`${d.stages.filter((s) => s.status === "red").length} red · ${d.stages.filter((s) => s.status === "amber").length} amber`} onClick={() => set({ view: problems ? null : "problems" })} />
        <Tile label="Window" value={`${hhmm(d.from)}–${hhmm(d.to)}`} note={d.lastWeekAvailable ? "compared with the same minutes last week" : "last week not available"} />
        <Tile label="API numbers" value={hhmm(d.api.asOf)} tone={d.api.stale || d.api.error ? "degraded" : "ok"}
          note={d.api.ageMs === null ? "no data yet" : `newest minute, ${Math.round(d.api.ageMs / 60_000)} min ago · copied every 5 min`} />
        <Tile label="Orders counted" value={d.orders.lastRun ? ago(d.orders.lastRun) : "not yet"} tone={d.orders.error ? "degraded" : "ok"}
          note={d.orders.newestMs ? `logs up to ${hhmm(d.orders.newestMs)} · every 5 min` : "every 5 minutes"} />
        <Tile label="Stages" value={num(d.stages.length)} note="with calls now or last week" />
      </div>
      <section className="card" style={{ overflow: "hidden" }}>
        <div className="card-head">
          <div className="tabs" role="tablist" aria-label="Show">
            <button type="button" role="tab" className="tab" aria-selected={!problems} onClick={() => set({ view: null })}>All · {d.stages.length}</button>
            <button type="button" role="tab" className="tab" aria-selected={problems} onClick={() => set({ view: "problems" })}>Problems · {bad.length}</button>
          </div>
          <div className="grow" />
          <div className="tabs" role="group" aria-label="Window">
            {[15, 60, 180].map((mm) => (
              <button key={mm} type="button" className="tab" aria-pressed={minutes === mm} aria-selected={minutes === mm} onClick={() => set({ minutes: mm === 60 ? null : String(mm) })}>
                {mm < 60 ? `${mm} min` : `${mm / 60} h`}
              </button>
            ))}
          </div>
        </div>
        {rows.length === 0 ? <Empty title={problems ? "No problems: every stage looks like last week" : "No calls in this window"} /> : (
          <div className="table-scroll">
            <table className="table htable">
              <thead>
                <tr>
                  <th>Status</th><th>Stage</th><th className="num">Calls</th><th className="num">4xx</th><th className="num">5xx</th>
                  <th className="num">p95</th><th className="num" title="IntegrationLatency p95: time spent in the backend">Backend p95</th>
                  <th className="num" title="p95 latency minus backend p95: roughly the time API Gateway itself adds">Gateway</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((s) => {
                  const lw = s.lastWeek;
                  return (
                    <tr key={s.id} className="clickable" tabIndex={0} onClick={() => onStage(s.id)} onKeyDown={(e) => { if (e.key === "Enter") onStage(s.id); }} title="Open this stage's history">
                      <td><StatusPill status={s.status} reasons={s.reasons} />{s.reasons.length > 0 && <div className="hint" style={{ marginTop: 4, maxWidth: 260 }}>{s.reasons.join(" · ")}</div>}</td>
                      <td><div style={{ fontWeight: 600 }}>{s.name}</div><div className="hint mono">{s.api}</div></td>
                      <td className="num">{num(s.now.calls)}<div className="hint">{lw ? <>{num(lw.calls)} · <Change value={s.change} /></> : "—"}</div></td>
                      <td className="num">{pct(s.now.err4xxPct)}<div className="hint">{lw ? pct(lw.err4xxPct) : "—"}</div></td>
                      <td className="num"><span className={s.now.err5xxPct && s.now.err5xxPct >= th.err5xxRedPct ? "danger-text" : s.now.err5xxPct && s.now.err5xxPct >= th.err5xxAmberPct ? "warn-text" : ""}>{pct(s.now.err5xxPct, 2)}</span><div className="hint">{lw ? pct(lw.err5xxPct, 2) : "—"}</div></td>
                      <td className="num">{ms(s.now.p95)}<div className="hint">{lw ? ms(lw.p95) : "—"}</div></td>
                      <td className="num">{ms(s.now.integrationP95)}<div className="hint">{lw ? ms(lw.integrationP95) : "—"}</div></td>
                      <td className="num">{ms(s.now.gatewayMs)}<div className="hint">{lw ? ms(lw.gatewayMs) : "—"}</div></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        <div className="card-foot hint">
          Small grey numbers: the same minutes last week. Red: 5xx ≥ {th.err5xxRedPct}%, or p95 ≥ {th.p95RedTimes}× last week (and ≥ {th.p95MinMs} ms), or no calls.
          Amber: 5xx ≥ {th.err5xxAmberPct}%, p95 ≥ {th.p95AmberTimes}×, 4xx ≥ {th.err4xxTimesLastWeek}× last week (and ≥ {th.err4xxMinPct}%), or calls below {th.quietPct}% of last week.
          Stages with fewer than {th.minCalls} calls are “Quiet”. Set in config/ops-dashboard.yaml.
        </div>
      </section>
    </>
  );
}

// ------------------------------------------------------------------ history
function History({ params, set }: { params: URLSearchParams; set: (p: Record<string, string | null>) => void }) {
  const day = params.get("day") || todayUtc();
  const stage = params.get("stage") || "";
  const service = params.get("service") || "";
  const store = params.get("store") || "";
  const isToday = day === todayUtc();
  const q = useQuery({
    queryKey: ["ops-history", day, stage, service, store],
    queryFn: () => get<OpsHistory>("/ops/history", { day, stage, service, store }),
    refetchInterval: isToday ? 60_000 : false, retry: false, placeholderData: keepPreviousData,
  });
  const d = q.data;
  const stageOpts = useMemo(() => (d?.stages ?? []).map((s) => ({ value: s.id, label: `${s.name} · ${s.api}` })), [d]);
  const storeOpts = useMemo(() => {
    const seen = new Map<string, string>();
    for (const s of d?.stores ?? []) if (!service || s.service === service) seen.set(s.store, s.name);
    return [...seen].map(([value, label]) => ({ value, label: label === value ? value : `${label} · ${value}` }));
  }, [d, service]);

  const exportCsv = () => {
    if (!d) return;
    const a = d.api, o = d.orders;
    const head = ["minute_utc", "calls", "err_4xx", "err_5xx", "p95_ms", "integration_p95_ms", "calls_last_week", "p95_ms_last_week", "orders", "orders_last_week"];
    const lines = [head.join(",")];
    for (let i = 0; i < 1440; i++) {
      const t = fmtTime(a.start + i * 60_000, "utc", false).slice(0, 16).replace(" ", "T") + "Z";
      lines.push([t, a.calls[i], a.err4xx[i], a.err5xx[i], a.p95[i] ?? "", a.integrationP95[i] ?? "", a.lastWeek?.calls[i] ?? "", a.lastWeek?.p95[i] ?? "",
        o.minutes?.[i] ?? "", o.lastWeek?.[i] ?? ""].join(","));
    }
    const name = ["ops", day, stage.replace(/[^\w.-]+/g, "_"), service, store.replace(/[^\w.-]+/g, "_")].filter(Boolean).join("-");
    downloadFile(`${name}.csv`, lines.join("\n") + "\n", "text/csv");
  };

  const a = d?.api;
  const sum = (v: (number | null)[] | null | undefined, upto = 1440) => (v ?? []).slice(0, upto).reduce<number>((x, y) => x + (y ?? 0), 0);
  // today: compare with last week until the same minute, not last week's whole day
  const shown = (v: (number | null)[] | null | undefined) => { const arr = v ?? []; let n = arr.length; while (n > 0 && arr[n - 1] === null) n--; return n; };
  const apiUpto = a ? a.shownMinutes : 1440;
  const ordUpto = d?.orders.minutes ? (isToday ? shown(d.orders.minutes) : 1440) : 1440;
  const lwNote = isToday ? " by this time" : "";
  return (
    <>
      <section className="card">
        <div className="card-head" style={{ gap: 10 }}>
          <label className="row" style={{ gap: 6 }}>
            <span className="hint">Day (UTC)</span>
            <input type="date" className="input" style={{ width: 160 }} value={day} max={todayUtc()} onChange={(e) => set({ day: e.target.value && e.target.value !== todayUtc() ? e.target.value : null })} />
          </label>
          <SearchSelect id="ops-stage" label="Stage" value={stage} options={stageOpts} allLabel="All stages" onChange={(v) => set({ stage: v || null })} maxWidth={340} />
          <select aria-label="Order service" className="select" style={{ width: "auto" }} value={service} onChange={(e) => set({ service: e.target.value || null, store: null })}>
            <option value="">All order services</option>
            {(d?.services ?? []).map((s) => <option key={s.service} value={s.service}>{s.service} · {s.cluster}</option>)}
          </select>
          <SearchSelect id="ops-store" label="Store" value={store} options={storeOpts} allLabel="All stores" onChange={(v) => set({ store: v || null })} maxWidth={300} />
          <div className="grow" />
          <button type="button" className="btn btn-sm" onClick={exportCsv} disabled={!d}><Icon name="download" size={15} /> CSV (per minute)</button>
        </div>
        {d && (
          <div className="card-body" style={{ paddingTop: 10, paddingBottom: 10 }}>
            <span className="hint">
              {stage ? <strong>{stageOpts.find((s) => s.value === stage)?.label ?? stage}</strong> : "All stages"}: {num(sum(a!.calls))} calls
              {a!.lastWeek ? <> (last week{lwNote} {num(sum(a!.lastWeek.calls, apiUpto))})</> : null}
              {" · "}{service || "all services"}{store ? ` · ${storeOpts.find((s) => s.value === store)?.label ?? store}` : ""}: {d.orders.counted ? `${num(d.orders.total)} orders` : "orders not counted"}
              {d.orders.lastWeek ? <> (last week{lwNote} {num(sum(d.orders.lastWeek, ordUpto))})</> : null}
            </span>
          </div>
        )}
      </section>
      {q.isLoading ? <section className="card"><Loading /></section>
        : q.error || !d || !a ? <section className="card"><div className="card-body"><ErrorCallout error={q.error} /></div></section> : (
          <>
            {!a.hasData && <Callout tone="neutral" title="No API Gateway numbers for this day">The per-minute copies start on 1 Nov 2026.</Callout>}
            <div className="grid-2">
              <section className="card">
                <div className="card-head"><div className="grow"><strong>API calls</strong><span className="hint">per 5 minutes</span></div></div>
                <LineChart label="API calls per 5 minutes" start={a.start} step={300_000} fmt={compact}
                  series={[{ key: "now", label: day, color: C_NOW, values: bucket(a.calls, 5), area: true },
                    ...(a.lastWeek ? [{ key: "lw", label: "Same day last week", color: C_LW, values: bucket(a.lastWeek.calls, 5), dashed: true }] : [])]} />
              </section>
              <section className="card">
                <div className="card-head"><div className="grow"><strong>Orders</strong><span className="hint">per 15 minutes</span></div></div>
                {d.orders.counted ? (
                  <LineChart label="Orders per 15 minutes" start={a.start} step={900_000} fmt={compact}
                    series={[{ key: "now", label: day, color: C_NOW, values: bucket(d.orders.minutes, 15), area: true },
                      ...(d.orders.lastWeek ? [{ key: "lw", label: "Same day last week", color: C_LW, values: bucket(d.orders.lastWeek, 15), dashed: true }] : [])]} />
                ) : <Empty title="Orders were not counted for this day">Orders are kept for the last 15 days.</Empty>}
              </section>
              <section className="card">
                <div className="card-head"><div className="grow"><strong>Errors</strong><span className="hint">per 5 minutes</span></div></div>
                <LineChart label="4xx and 5xx per 5 minutes" start={a.start} step={300_000} fmt={compact}
                  series={[{ key: "4xx", label: "4xx", color: C_4XX, values: bucket(a.err4xx, 5) }, { key: "5xx", label: "5xx", color: C_5XX, values: bucket(a.err5xx, 5) },
                    ...(a.lastWeek ? [{ key: "5lw", label: "5xx last week", color: C_LW, values: bucket(a.lastWeek.err5xx, 5), dashed: true }] : [])]} />
              </section>
              <section className="card">
                <div className="card-head"><div className="grow"><strong>Latency p95</strong><span className="hint">average of the minute p95s, per 5 minutes</span></div></div>
                <LineChart label="p95 latency per 5 minutes" start={a.start} step={300_000} fmt={(v) => ms(v)}
                  series={[{ key: "p95", label: "p95", color: C_NOW, values: bucket(a.p95, 5, "avg") },
                    { key: "backend", label: "Backend p95", color: C_BACKEND, values: bucket(a.integrationP95, 5, "avg") },
                    ...(a.lastWeek ? [{ key: "lw", label: "p95 last week", color: C_LW, values: bucket(a.lastWeek.p95, 5, "avg"), dashed: true }] : [])]} />
              </section>
            </div>
          </>
        )}
    </>
  );
}
