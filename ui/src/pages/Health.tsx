import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { enc, get, type ClusterStatus, type EcsClusterMetrics, type EcsServiceMetrics, type HealthCluster, type HealthSnapshot, type HealthTargetGroup, type MetricSeries } from "../api";
import { Icon, type IconName } from "../components/icons";
import { Page } from "../components/Shell";
import { Empty, ErrorCallout, Loading, SearchInput, Spinner } from "../components/ui";
import { ago, duration, fmtTime, num, storedZone } from "../format";
import { useClusters, useMe } from "../session";

// ECS clusters → the load balancer target groups they sit behind → target health.
// Everything comes from AWS (read-only) and refreshes every minute.

type View = "all" | "problems" | "nolb" | "healthy";

const STATUS: Record<ClusterStatus, { label: string; tone: string; icon: IconName; hint: string }> = {
  down: { label: "Down", tone: "down", icon: "alert", hint: "A target group has no healthy target, or a service runs 0 tasks" },
  degraded: { label: "Degraded", tone: "degraded", icon: "warn", hint: "Some targets are unhealthy, or a service runs fewer tasks than desired" },
  healthy: { label: "Healthy", tone: "ok", icon: "check", hint: "Every target is healthy and every service runs its desired tasks" },
  none: { label: "No load balancer", tone: "none", icon: "minus", hint: "No target group is attached to this cluster" },
};

export function StatusPill({ status }: { status: ClusterStatus }) {
  const s = STATUS[status];
  return <span className={`hpill ${s.tone}`} title={s.hint}><Icon name={s.icon} size={13} strokeWidth={2.4} />{s.label}</span>;
}

function StatePill({ state }: { state: string }) {
  const tone = state === "healthy" ? "ok" : ["unhealthy", "unavailable"].includes(state) ? "down" : ["draining", "initial"].includes(state) ? "degraded" : "none";
  const icon: IconName = tone === "ok" ? "check" : tone === "down" ? "alert" : tone === "degraded" ? "warn" : "minus";
  return <span className={`hpill ${tone}`}><Icon name={icon} size={13} strokeWidth={2.4} />{state}</span>;
}

/** "2/3" plus one small cell per target (green healthy, red unhealthy, grey e.g. unused). */
function TargetsBar({ healthy, total, bad }: { healthy: number; total: number; bad: number }) {
  if (!total) return <span className="hint">no targets</span>;
  const tone = healthy === total ? "ok" : bad === 0 ? "none" : healthy === 0 ? "down" : "degraded";
  const cells = Math.min(total, 12);
  const scale = cells / total;
  const okCells = Math.round(healthy * scale);
  const badCells = Math.min(cells - okCells, Math.round(bad * scale) || (bad ? 1 : 0));
  return (
    <span className="tbar" title={`${healthy} healthy, ${bad} unhealthy, ${total - healthy - bad} other, of ${total} targets`}>
      <span className={`tbar-n ${tone}`}>{healthy}/{total}</span>
      <span className="tbar-track" aria-hidden="true">
        {Array.from({ length: cells }).map((_, i) => <span key={i} className={`tbar-cell ${i < okCells ? "ok" : i < okCells + badCells ? "bad" : "other"}`} />)}
      </span>
    </span>
  );
}

export function Health() {
  const me = useMe().data!;
  const logClusters = new Set((useClusters().data ?? []).map((c) => c.id));
  const qc = useQueryClient();
  const [params, setParams] = useSearchParams();
  const view = (params.get("view") as View) || "all";
  const q = params.get("q") ?? "";
  const setParam = (k: string, v: string | null) => {
    const n = new URLSearchParams(params);
    if (v) n.set(k, v); else n.delete(k);
    setParams(n, { replace: true });
  };
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [refreshing, setRefreshing] = useState(false);
  const [, tick] = useState(0);
  useEffect(() => { const t = setInterval(() => tick((x) => x + 1), 15_000); return () => clearInterval(t); }, []);

  const health = useQuery({
    queryKey: ["ecs-health"],
    queryFn: () => get<HealthSnapshot>("/health/ecs"),
    refetchInterval: 60_000,
    staleTime: 30_000,
    retry: false,
  });
  const hours = Number(params.get("hours") || 3);
  const metrics = useQuery({
    queryKey: ["ecs-metrics", hours],
    queryFn: () => get<EcsClusterMetrics>("/health/ecs/metrics", { hours }),
    refetchInterval: 300_000,
    staleTime: 120_000,
    retry: false,
  });
  const refresh = async () => {
    setRefreshing(true);
    try {
      qc.setQueryData(["ecs-health"], await get<HealthSnapshot>("/health/ecs", { refresh: true }));
    } catch {
      /* the next poll shows the error */
    } finally {
      setRefreshing(false);
    }
  };
  const d = health.data;
  const rows = useMemo(() => {
    const f = q.trim().toLowerCase();
    return (d?.clusters ?? []).filter((c) =>
      (!f || c.name.toLowerCase().includes(f) || c.loadBalancers.some((l) => l.toLowerCase().includes(f)) || c.targetGroups.some((t) => t.name.toLowerCase().includes(f))) &&
      (view === "all" || (view === "problems" && (c.status === "down" || c.status === "degraded")) ||
        (view === "nolb" && c.targetGroups.length === 0) || (view === "healthy" && c.status === "healthy")));
  }, [d, q, view]);
  const toggle = (n: string) => setOpen((o) => { const x = new Set(o); if (x.has(n)) x.delete(n); else x.add(n); return x; });
  const s = d?.summary;
  const problems = s ? s.down + s.degraded : 0;

  return (
    <Page wide crumbs={[{ label: "Infrastructure" }, { label: "ECS health" }]} title="ECS health"
      actions={d ? (
        <span className="row" style={{ gap: 8 }}>
          <span className="hint">{d.region} · updated {ago(d.generatedAt)}</span>
          {me.admin && <button type="button" className="btn btn-sm" onClick={refresh} disabled={refreshing} title="Read AWS now">{refreshing ? <Spinner /> : <Icon name="refresh" size={15} />} Refresh</button>}
        </span>
      ) : undefined}>
      <div className="page-head">
        <div className="grow">
          <h1>ECS health</h1>
          <p className="sub">Every ECS cluster, the load balancer target groups it sits behind, and whether each target is healthy. Read live from AWS and refreshed every minute.</p>
        </div>
      </div>

      {health.isLoading ? <section className="card"><Loading what="Reading ECS clusters, services and target groups from AWS…" /></section>
        : health.error ? <section className="card"><div className="card-body"><ErrorCallout error={health.error} admin={me.admin} /></div></section>
        : d && s ? (
          <>
            <div className="stats htiles">
              <Tile label="Problems" value={problems} tone={problems ? (s.down ? "down" : "degraded") : "ok"} note={problems ? `${s.down} down · ${s.degraded} degraded` : "all clear"} active={view === "problems"} onClick={() => setParam("view", view === "problems" ? null : "problems")} />
              <Tile label="Unhealthy targets" value={s.unhealthyTargets} tone={s.unhealthyTargets ? "degraded" : "ok"} note="in target groups of these clusters" />
              <Tile label="Services below desired" value={s.servicesBelowDesired} tone={s.servicesBelowDesired ? "degraded" : "ok"} note="running fewer tasks than desired" />
              <Tile label="Clusters" value={s.clusters} note={`${s.healthy} healthy`} active={view === "healthy"} onClick={() => setParam("view", view === "healthy" ? null : "healthy")} />
              <Tile label="Without a load balancer" value={s.withoutLoadBalancer} note="no target group attached" active={view === "nolb"} onClick={() => setParam("view", view === "nolb" ? null : "nolb")} />
            </div>

            <section className="card" style={{ overflow: "hidden" }}>
              <div className="card-head" style={{ flexWrap: "wrap", gap: 10 }}>
                <div className="tabs" role="tablist" aria-label="Show">
                  {([["all", `All · ${s.clusters}`], ["problems", `Problems · ${problems}`], ["nolb", `No load balancer · ${s.withoutLoadBalancer}`], ["healthy", `Healthy · ${s.healthy}`]] as [View, string][]).map(([v, l]) => (
                    <button key={v} type="button" role="tab" className="tab" aria-selected={view === v} onClick={() => setParam("view", v === "all" ? null : v)}>{l}</button>
                  ))}
                </div>
                <div className="grow" />
                <div className="tabs" role="group" aria-label="CPU and memory over">
                  {[3, 24].map((h) => (
                    <button key={h} type="button" className="tab" aria-pressed={hours === h} aria-selected={hours === h}
                      onClick={() => setParam("hours", h === 3 ? null : String(h))} title={`CPU and memory charts: the last ${h} hours`}>{h} h</button>
                  ))}
                </div>
                <div style={{ width: 300 }}><SearchInput label="Filter clusters" value={q} onChange={(v) => setParam("q", v || null)} placeholder="Cluster, load balancer or target group" /></div>
                <button type="button" className="btn btn-ghost btn-sm" onClick={() => setOpen(open.size ? new Set() : new Set(rows.map((r) => r.name)))}>{open.size ? "Collapse all" : "Expand all"}</button>
              </div>
              {rows.length === 0 ? <Empty title={view === "problems" ? "No problems right now" : "No clusters match"} /> : (
                <div className="table-scroll">
                  <table className="table htable">
                    <thead>
                      <tr>
                        <th style={{ width: 28 }}><span className="sr-only">Expand</span></th>
                        <th>Cluster</th><th>Status</th><th>CPU</th><th>Memory</th><th>Target groups · load balancer</th><th>Healthy targets</th><th>Tasks</th><th>Instances</th>
                      </tr>
                    </thead>
                    <tbody>
                      {rows.map((c) => (
                        <Fragment key={c.name}>
                          <tr className={`clickable ${open.has(c.name) ? "open" : ""}`} onClick={() => toggle(c.name)} tabIndex={0} aria-expanded={open.has(c.name)}
                            onKeyDown={(e) => { if (e.key === "Enter") toggle(c.name); }}>
                            <td className="lv-caret"><Icon name="chevron" size={14} style={{ transform: open.has(c.name) ? "rotate(90deg)" : undefined }} /></td>
                            <td className="nowrap"><span className="mono" style={{ fontWeight: 600 }}>{c.name}</span>
                              {logClusters.has(c.name) && <Link className="hlink" to={`/logs/${enc(c.name)}`} onClick={(e) => e.stopPropagation()}>logs</Link>}</td>
                            <td><StatusPill status={c.status} /></td>
                            <td className="nowrap"><Usage s={metrics.data?.clusters[c.name]?.cpu} reserved={metrics.data?.clusters[c.name]?.cpuReserved} what="CPU" loading={metrics.isLoading} /></td>
                            <td className="nowrap"><Usage s={metrics.data?.clusters[c.name]?.memory} reserved={metrics.data?.clusters[c.name]?.memoryReserved} what="Memory" loading={metrics.isLoading} /></td>
                            <td className="nowrap" style={{ fontSize: 13 }}>
                              {c.targetGroups.length ? c.targetGroups.map((t) => <div key={t.arn} className="mono">{t.name}</div>) : <span className="hint">—</span>}
                              {c.loadBalancers.length > 0 && <div className="hint mono" style={{ fontSize: 12 }}>{c.loadBalancers.join(", ")}</div>}
                            </td>
                            <td><TargetsBar healthy={c.targets.healthy} total={c.targets.total} bad={c.targets.bad} /></td>
                            <td className="nowrap"><span className={c.servicesRunning < c.servicesDesired ? "warn-text" : ""}>{c.servicesRunning}/{c.servicesDesired}</span> <span className="hint">· {c.services.length} svc</span></td>
                            <td className="nowrap">{c.instances.length}{c.targetGroups.length > 0 && c.instances.some((i) => !i.behindLoadBalancer) && <span className="hint" title="Instances of this cluster that are not a target of its load balancer"> · {c.instances.filter((i) => !i.behindLoadBalancer).length} not in LB</span>}</td>
                          </tr>
                          {open.has(c.name) && (
                            <tr className="hdetail-row"><td colSpan={9}><ClusterDetail c={c} hours={hours} m={metrics.data?.clusters[c.name]} /></td></tr>
                          )}
                        </Fragment>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              {metrics.error ? (
                <div className="lv-status" style={{ borderBottom: 0 }}>
                  <Icon name="info" size={15} />
                  <span className="hint">CPU and memory are not available: {me.admin ? (metrics.error as Error).message : "the server can't read CloudWatch right now."}</span>
                </div>
              ) : null}
              <div className="card-foot" style={{ justifyContent: "flex-start" }}>
                <span className="hint">Read in {duration(d.tookMs)} · CPU and memory: what the tasks use, as a share of what the cluster's instances offer (CloudWatch, averaged over {metrics.data ? Math.round(metrics.data.period / 60) : "a few"} min, the last {hours} h) · a cluster is linked to a target group when one of its services registers there, one of its instances is a target there, or the target group is named <code>tg-&lt;cluster&gt;</code>.</span>
              </div>
            </section>

            {me.admin && d.unlinkedTargetGroups.length > 0 && (
              <details className="card hunlinked">
                <summary>
                  <strong>Target groups not linked to an ECS cluster</strong> <span className="hint">· {d.unlinkedTargetGroups.length} · e.g. monitoring, Jenkins, internal apps</span>
                  {d.unlinkedTargetGroups.some((t) => t.counts.bad) && <span className="hpill down" style={{ marginLeft: 8 }}><Icon name="alert" size={13} />{d.unlinkedTargetGroups.filter((t) => t.counts.bad).length} with unhealthy targets</span>}
                </summary>
                <div className="card-body" style={{ gap: 14 }}>
                  {d.unlinkedTargetGroups.map((t) => <TargetGroupBlock key={t.arn} tg={t} />)}
                </div>
              </details>
            )}
          </>
        ) : null}
    </Page>
  );
}

function Tile({ label, value, note, tone, active, onClick }: { label: string; value: number; note: string; tone?: string; active?: boolean; onClick?: () => void }) {
  const inner = (
    <>
      <span className="stat-label">{label}</span>
      <span className={`stat-value ${tone ? `tone-${tone}` : ""}`}>{num(value)}</span>
      <span className="stat-note">{note}</span>
    </>
  );
  return onClick
    ? <button type="button" className={`stat htile ${active ? "active" : ""}`} onClick={onClick} aria-pressed={active}>{inner}</button>
    : <div className="stat htile">{inner}</div>;
}

function ClusterDetail({ c, hours, m }: { c: HealthCluster; hours: number; m: EcsClusterMetrics["clusters"][string] | undefined }) {
  const outside = c.instances.filter((i) => !i.behindLoadBalancer);
  const svc = useQuery({
    queryKey: ["ecs-svc-metrics", c.name, hours],
    queryFn: () => get<EcsServiceMetrics>(`/health/ecs/clusters/${enc(c.name)}/metrics`, { hours }),
    enabled: c.services.length > 0,
    staleTime: 120_000,
    retry: false,
  });
  return (
    <div className="hdetail">
      {c.targetGroups.length === 0 && (
        <div className="hint">No target group is attached to this cluster: none of its services registers into one, none of its instances is a target, and there is no <code>tg-{c.name}</code>.</div>
      )}
      {c.targetGroups.map((t) => <TargetGroupBlock key={t.arn} tg={t} />)}

      {m && (m.cpu?.points.length || m.memory?.points.length) ? (
        <div className="stack-sm" style={{ gap: 6 }}>
          <span className="hsub">CPU and memory · last {hours} h</span>
          <div className="usage-charts">
            <UsageChart title="CPU" used={m.cpu} reserved={m.cpuReserved} />
            <UsageChart title="Memory" used={m.memory} reserved={m.memoryReserved} />
          </div>
        </div>
      ) : null}

      <div className="stack-sm" style={{ gap: 6 }}>
        <span className="hsub">ECS services · {c.services.length}</span>
        {c.services.length === 0 ? <span className="hint">No services.</span> : (
          <table className="table compact">
            <thead><tr><th>Service</th><th>Tasks running / desired</th><th>CPU</th><th>Memory</th><th>Rollout</th><th>Target group</th><th>Latest event</th></tr></thead>
            <tbody>
              {c.services.map((s) => (
                <tr key={s.name}>
                  <td className="mono">{s.name}</td>
                  <td className="nowrap">
                    {s.running < s.desired ? <span className={`hpill ${s.running === 0 && s.desired > 0 ? "down" : "degraded"}`}><Icon name={s.running === 0 ? "alert" : "warn"} size={13} />{s.running}/{s.desired}</span> : <span className="hpill ok"><Icon name="check" size={13} />{s.running}/{s.desired}</span>}
                    {s.pending > 0 && <span className="hint"> · {s.pending} pending</span>}
                  </td>
                  <td className="nowrap" title="% of the CPU this service's tasks reserve"><Usage s={svc.data?.services[s.name ?? ""]?.cpu} what="CPU" loading={svc.isLoading} /></td>
                  <td className="nowrap" title="% of the memory this service's tasks reserve"><Usage s={svc.data?.services[s.name ?? ""]?.memory} what="Memory" loading={svc.isLoading} /></td>
                  <td className="nowrap">{s.rollout ? <span className={s.rollout === "FAILED" ? "danger-text" : s.rollout === "IN_PROGRESS" ? "warn-text" : "hint"}>{s.rollout.toLowerCase().replace("_", " ")}</span> : <span className="hint">—</span>}{s.deployments > 1 && <span className="hint"> · {s.deployments} deployments</span>}</td>
                  <td className="mono nowrap" style={{ fontSize: 12.5 }}>{s.targetGroups.join(", ") || <span className="hint">—</span>}</td>
                  <td className="hint" style={{ maxWidth: 520, whiteSpace: "normal" }}>{s.lastEvent ? <>{ago(s.lastEvent.at)}: {s.lastEvent.message}</> : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {outside.length > 0 && c.targetGroups.length > 0 && (
        <div className="stack-sm" style={{ gap: 6 }}>
          <span className="hsub">Instances of this cluster not behind its load balancer · {outside.length}</span>
          <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
            {outside.map((i) => <span key={i.id} className="badge mono" title={i.name ?? undefined}>{i.ip ?? i.id} · {i.id}</span>)}
          </div>
        </div>
      )}
    </div>
  );
}

function TargetGroupBlock({ tg }: { tg: HealthTargetGroup }) {
  return (
    <div className="htg">
      <div className="row" style={{ gap: 10, flexWrap: "wrap" }}>
        <span className="mono" style={{ fontWeight: 600 }}>{tg.name}</span>
        <TargetsBar healthy={tg.counts.healthy} total={tg.total} bad={tg.counts.bad} />
        <span className="hint">
          {tg.loadBalancers.length ? tg.loadBalancers.map((l) => `${l.name}${l.type ? ` (${l.type === "application" ? "ALB" : l.type === "network" ? "NLB" : l.type}${l.scheme ? `, ${l.scheme}` : ""})` : ""}`).join(", ") : "not attached to a load balancer"}
          {" · "}{tg.protocol ?? ""} {tg.port ?? ""}{tg.healthCheckPath ? ` · health check ${tg.healthCheckPath}` : ""}
          {tg.linkedBy?.length ? ` · linked by ${tg.linkedBy.join(", ")}` : ""}
        </span>
      </div>
      {tg.error && <span className="danger-text" style={{ fontSize: 13 }}>Couldn't read target health: {tg.error}</span>}
      {tg.targets.length > 0 && (
        <table className="table compact">
          <thead><tr><th>Target</th><th>Private IP</th><th>Name</th><th>Port</th><th>State</th><th>Reason</th></tr></thead>
          <tbody>
            {tg.targets.map((t) => (
              <tr key={`${t.id}:${t.port}`}>
                <td className="mono">{t.id}{t.inCluster === false && <span className="hint" title="Not an instance of this cluster"> · other cluster</span>}</td>
                <td className="mono">{t.ip ?? "—"}</td>
                <td>{t.name ?? <span className="hint">—</span>}</td>
                <td className="mono">{t.port ?? "—"}</td>
                <td><StatePill state={t.state} /></td>
                <td className="hint" style={{ whiteSpace: "normal" }}>{t.reason ? `${t.reason}${t.description && !t.reason.endsWith(t.description) ? `: ${t.description}` : ""}` : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ CPU / memory

function pctText(v: number | null | undefined): string {
  return v === null || v === undefined ? "—" : `${v < 10 ? v.toFixed(1) : Math.round(v)}%`;
}

/** Current % with a small trend line; amber from 75%, red from 90%. */
function Usage({ s, reserved, what, loading }: { s: MetricSeries | undefined; reserved?: MetricSeries; what: string; loading?: boolean }) {
  if (!s || s.now === null) return <span className="hint" title={loading ? "Loading" : `No ${what} data in CloudWatch for this period`}>{loading ? "…" : "—"}</span>;
  const tone = s.now >= 90 ? "danger-text" : s.now >= 75 ? "warn-text" : "";
  const title = `${what} now ${pctText(s.now)} · average ${pctText(s.avg)} · peak ${pctText(s.max)}${reserved?.now != null ? ` · reserved ${pctText(reserved.now)}` : ""}`;
  return (
    <span className="usage" title={title}>
      <span className={`usage-n ${tone}`}>{s.now >= 75 && <Icon name={s.now >= 90 ? "alert" : "warn"} size={12} strokeWidth={2.4} />}{pctText(s.now)}</span>
      <Spark points={s.points} />
    </span>
  );
}

function Spark({ points }: { points: [number, number][] }) {
  if (points.length < 2) return null;
  const w = 52, h = 18;
  const t0 = points[0][0], t1 = points[points.length - 1][0];
  const x = (t: number) => ((t - t0) / Math.max(1, t1 - t0)) * (w - 2) + 1;
  const y = (v: number) => h - 1 - (Math.min(100, Math.max(0, v)) / 100) * (h - 2);
  return (
    <svg width={w} height={h} className="spark" aria-hidden="true">
      <line x1={0} x2={w} y1={h - 1} y2={h - 1} className="spark-base" />
      <polyline points={points.map(([t, v]) => `${x(t).toFixed(1)},${y(v).toFixed(1)}`).join(" ")} className="spark-line" />
    </svg>
  );
}

/** One measure over time, 0-100%: what the tasks use (solid) and what they reserve (dashed). */
function UsageChart({ title, used, reserved }: { title: string; used?: MetricSeries; reserved?: MetricSeries }) {
  const zone = storedZone();
  const [hover, setHover] = useState<number | null>(null);
  const W = 520, H = 150, L = 36, R = 8, T = 8, B = 20;
  const pts = used?.points ?? [];
  const rpts = reserved?.points ?? [];
  const all = [...pts, ...rpts];
  if (!all.length) return null;
  const t0 = Math.min(...all.map((p) => p[0])), t1 = Math.max(...all.map((p) => p[0]));
  const x = (t: number) => L + ((t - t0) / Math.max(1, t1 - t0)) * (W - L - R);
  const y = (v: number) => T + (1 - Math.min(100, Math.max(0, v)) / 100) * (H - T - B);
  const path = (ps: [number, number][]) => ps.map(([t, v], i) => `${i ? "L" : "M"}${x(t).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const hp = hover !== null ? pts[hover] : null;
  const hr = hp ? rpts.find((r) => r[0] === hp[0]) : null;
  return (
    <div className="usage-chart">
      <div className="row" style={{ gap: 12, fontSize: 12.5 }}>
        <strong>{title}</strong>
        <span className="histo-key"><span className="swatch" style={{ background: "var(--lv-info)" }} />used {pctText(used?.now)}</span>
        {rpts.length > 0 && <span className="histo-key"><span className="swatch dashed" />reserved {pctText(reserved?.now)}</span>}
        <span className="hint" style={{ marginLeft: "auto" }}>peak {pctText(used?.max)}</span>
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img" aria-label={`${title} use over time`} onMouseLeave={() => setHover(null)}
        onMouseMove={(e) => {
          const box = e.currentTarget.getBoundingClientRect();
          const tx = t0 + (((e.clientX - box.left) / box.width) * W - L) / (W - L - R) * (t1 - t0);
          let best = 0;
          pts.forEach((p, i) => { if (Math.abs(p[0] - tx) < Math.abs(pts[best][0] - tx)) best = i; });
          setHover(pts.length ? best : null);
        }}>
        {[0, 50, 100].map((v) => (
          <g key={v}>
            <line x1={L} x2={W - R} y1={y(v)} y2={y(v)} className="histo-grid" />
            <text x={L - 6} y={y(v) + 4} textAnchor="end" className="histo-axis">{v}%</text>
          </g>
        ))}
        <line x1={L} x2={W - R} y1={y(90)} y2={y(90)} className="usage-limit" />
        {rpts.length > 1 && <path d={path(rpts)} className="usage-reserved" />}
        {pts.length > 1 && <path d={path(pts)} className="usage-used" />}
        {hp && <>
          <line x1={x(hp[0])} x2={x(hp[0])} y1={T} y2={H - B} className="histo-base" />
          <circle cx={x(hp[0])} cy={y(hp[1])} r={4} className="usage-dot" />
        </>}
        <text x={L} y={H - 5} className="histo-axis">{fmtTime(t0, zone, false).slice(5, 16)}</text>
        <text x={W - R} y={H - 5} textAnchor="end" className="histo-axis">{fmtTime(t1, zone, false).slice(5, 16)}</text>
      </svg>
      {hp && <div className="hint" style={{ fontSize: 12 }}>{fmtTime(hp[0], zone, false)} · used {pctText(hp[1])}{hr ? ` · reserved ${pctText(hr[1])}` : ""}</div>}
    </div>
  );
}
