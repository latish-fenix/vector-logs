import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useMemo, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import {
  get, request,
  type AlertEvent, type AlertFinding, type AlertOptions, type AlertParamSpec, type AlertRule, type AlertsOverview, type AlertType,
} from "../api";
import { Icon } from "../components/icons";
import { Page } from "../components/Shell";
import { Badge, Callout, Dialog, Empty, ErrorCallout, Loading, SearchInput, Spinner, useToast } from "../components/ui";
import { ago, fmtTime, localZoneName, num, storedZone } from "../format";
import { useMe } from "../session";

// Alerts: Slack webhook, the rules (what is checked and when it alerts) and what was sent.
// Admins manage everything; members see the alerts about their clusters.

const ZONES = ["Asia/Kolkata", "UTC", "America/New_York", "America/Chicago", "America/Los_Angeles", "Europe/London"];

export function Alerts() {
  const me = useMe().data!;
  return (
    <Page crumbs={[{ label: "Infrastructure" }, { label: "Alerts" }]} title="Alerts">
      <div className="page-head">
        <div className="grow">
          <h1>Alerts</h1>
          <span className="sub">
            {me.admin
              ? "The server checks these rules all the time and posts to Slack when something goes wrong, and again when it's back to normal."
              : "Alerts sent to Slack about the clusters you can see. Admins choose what is checked."}
          </span>
        </div>
      </div>
      {me.admin ? <AdminAlerts /> : <History />}
    </Page>
  );
}

// ------------------------------------------------------------------ admin

function AdminAlerts() {
  const q = useQuery({ queryKey: ["alerts"], queryFn: () => get<AlertsOverview>("/admin/alerts"), refetchInterval: 30_000 });
  const [editing, setEditing] = useState<{ rule?: AlertRule; type?: AlertType } | null>(null);
  const [adding, setAdding] = useState(false);
  if (q.isLoading) return <section className="card"><Loading /></section>;
  if (q.error) return <section className="card"><div className="card-body"><ErrorCallout error={q.error} admin /></div></section>;
  const d = q.data!;
  const firing = d.rules.reduce((n, r) => n + r.status.firing.length, 0);
  return (
    <>
      {!d.slack.configured && (
        <Callout tone="warn" icon="warn" title="No Slack webhook yet">The rules are checked and their alerts are kept in the history below, but nothing is posted to Slack until you add a webhook.</Callout>
      )}
      {d.settings.paused && <Callout tone="warn" icon="warn" title="Notifications are paused">Rules are still checked; nothing is posted to Slack until you resume.</Callout>}
      <div className="stats" style={{ gridTemplateColumns: "repeat(4, minmax(0, 1fr))" }}>
        <div className="stat"><span className="stat-label">Failing now</span><span className="stat-value" style={{ color: firing ? "var(--danger)" : undefined }}>{num(firing)}</span><span className="stat-note">{firing ? "see the red rules below" : "all clear"}</span></div>
        <div className="stat"><span className="stat-label">Rules on</span><span className="stat-value">{num(d.rules.filter((r) => r.enabled).length)}</span><span className="stat-note">of {num(d.rules.length)}</span></div>
        <div className="stat"><span className="stat-label">Slack</span><span className="stat-value" style={{ fontSize: 20 }}>{d.slack.configured ? (d.settings.paused ? "Paused" : "Connected") : "Not set"}</span><span className="stat-note">{d.slack.configured ? `set by ${d.slack.updatedBy ?? "?"} ${ago(d.slack.updatedAt)}` : "add a webhook below"}</span></div>
        <div className="stat"><span className="stat-label">Last check</span><span className="stat-value" style={{ fontSize: 20 }}>{d.checker.checkedAt ? ago(new Date(d.checker.checkedAt).toISOString()) : "not yet"}</span><span className="stat-note">{d.checker.background ? `every ${d.checker.intervalSeconds} s` : "the checker is off (ALERTS_BACKGROUND=false)"}</span></div>
      </div>
      <SlackCard d={d} />
      <section className="card" style={{ overflow: "hidden" }}>
        <div className="card-head">
          <div className="grow"><span className="card-title">Rules</span><span className="hint">Turn rules on or off, change their limits, or add more. A rule that can't run shows its error.</span></div>
          <button type="button" className="btn btn-primary" onClick={() => setAdding(true)}><Icon name="plus" /> Add alert</button>
        </div>
        <RulesTable d={d} onEdit={(rule) => setEditing({ rule, type: d.catalog.find((t) => t.type === rule.type) })} />
      </section>
      <History />
      {adding && <TypePicker catalog={d.catalog} onClose={() => setAdding(false)} onPick={(type) => { setAdding(false); setEditing({ type }); }} />}
      {editing?.type && <RuleDialog type={editing.type} rule={editing.rule} lag={d.lbLagMinutes} onClose={() => setEditing(null)} />}
    </>
  );
}

function SlackCard({ d }: { d: AlertsOverview }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [url, setUrl] = useState("");
  const [consoleUrl, setConsoleUrl] = useState(d.settings.consoleUrl || "");
  const [tz, setTz] = useState(d.settings.timeZone || "UTC");
  const [error, setError] = useState<unknown>(null);
  const done = (text: string) => { setError(null); toast(text); qc.invalidateQueries({ queryKey: ["alerts"] }); };
  const save = useMutation({ mutationFn: () => request("/admin/alerts/slack", { method: "PUT", body: { webhookUrl: url.trim() } }),
    onSuccess: () => { setUrl(""); done("Slack webhook saved"); }, onError: setError });
  const test = useMutation({ mutationFn: () => request("/admin/alerts/slack/_test", { method: "POST", body: { webhookUrl: url.trim() || null } }),
    onSuccess: () => done("Test message sent: check the Slack channel"), onError: setError });
  const remove = useMutation({ mutationFn: () => request("/admin/alerts/slack", { method: "DELETE" }),
    onSuccess: () => done("Slack webhook removed"), onError: setError });
  const settings = useMutation({ mutationFn: (body: Record<string, unknown>) => request("/admin/alerts/settings", { method: "PUT", body }),
    onSuccess: () => done("Saved"), onError: setError });
  const here = window.location.origin;
  const zones = ZONES.includes(tz) ? ZONES : [tz, ...ZONES];
  const busy = save.isPending || test.isPending || remove.isPending || settings.isPending;
  return (
    <section className="card">
      <div className="card-head">
        <div className="grow"><span className="card-title">Slack</span><span className="hint">Alerts go to one channel through a Slack incoming webhook. The URL is kept in AWS Secrets Manager and never shown again.</span></div>
        {d.slack.configured && (
          <button type="button" className="btn btn-sm" onClick={() => settings.mutate({ paused: !d.settings.paused })} disabled={busy}>
            {d.settings.paused ? "Resume notifications" : "Pause notifications"}
          </button>
        )}
      </div>
      <div className="card-body stack">
        <div className="field">
          <label className="label" htmlFor="hook">{d.slack.configured ? "Change the webhook URL" : "Webhook URL"} {d.slack.configured && <span className="hint">· now <span className="mono">{d.slack.hint}</span></span>}</label>
          <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
            <input id="hook" className="input mono" style={{ flex: "1 1 360px" }} type="password" autoComplete="off" spellCheck={false}
              placeholder="https://hooks.slack.com/services/T…/B…/…" value={url} onChange={(e) => setUrl(e.target.value)} />
            <button type="button" className="btn btn-primary" disabled={!url.trim() || busy} onClick={() => save.mutate()}>{save.isPending ? <Spinner /> : null} Save</button>
            <button type="button" className="btn" disabled={(!url.trim() && !d.slack.configured) || busy} onClick={() => test.mutate()}>{test.isPending ? <Spinner /> : <Icon name="external" />} Send test message</button>
            {d.slack.configured && <button type="button" className="btn btn-ghost btn-danger" disabled={busy} onClick={() => { if (window.confirm("Remove the Slack webhook? Alerts will only be kept in the history.")) remove.mutate(); }}>Remove</button>}
          </div>
          <span className="hint">In Slack: <strong>Apps → Incoming WebHooks</strong> (or your Slack app's <em>Incoming Webhooks</em> page) → <strong>Add New Webhook</strong>, pick the channel, copy the URL. "Send test message" tests the URL in the box, or the saved one when the box is empty.</span>
        </div>
        <div className="grid-2" style={{ gap: 16 }}>
          <div className="field">
            <label className="label" htmlFor="curl">Console address <span className="hint">· for links in Slack</span></label>
            <div className="row" style={{ gap: 8 }}>
              <input id="curl" className="input" placeholder="https://app-logs-int.infra.fenixcommerce.com" value={consoleUrl} onChange={(e) => setConsoleUrl(e.target.value)} />
              {!consoleUrl && <button type="button" className="btn btn-sm" onClick={() => setConsoleUrl(here)}>Use {here.replace(/^https?:\/\//, "")}</button>}
            </div>
          </div>
          <div className="field">
            <label className="label" htmlFor="tz">Times in Slack</label>
            <select id="tz" className="select" value={tz} onChange={(e) => setTz(e.target.value)}>
              {zones.map((z) => <option key={z} value={z}>{z === "Asia/Kolkata" ? "India (IST)" : z}</option>)}
            </select>
          </div>
        </div>
        {(consoleUrl !== (d.settings.consoleUrl || "") || tz !== d.settings.timeZone) && (
          <div className="row" style={{ gap: 8 }}>
            <button type="button" className="btn btn-primary btn-sm" disabled={busy} onClick={() => settings.mutate({ consoleUrl, timeZone: tz })}>Save address and time zone</button>
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => { setConsoleUrl(d.settings.consoleUrl || ""); setTz(d.settings.timeZone); }}>Cancel</button>
          </div>
        )}
        {error ? <ErrorCallout error={error} admin /> : null}
      </div>
    </section>
  );
}

// ------------------------------------------------------------------ rules

function scope(v: unknown, all: string): string {
  const xs = (v as string[]) ?? [];
  if (!xs.length) return all;
  return xs.length <= 3 ? xs.join(", ") : `${xs.slice(0, 3).join(", ")} +${xs.length - 3}`;
}

/** One line saying when the rule alerts. */
export function describeRule(r: { type: string; params: Record<string, unknown>; forMinutes: number; repeatMinutes: number }): string {
  const p = r.params as Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any
  const text: Record<string, () => string> = {
    app_aws: () => "Any read from S3, ECS, load balancers or CloudWatch fails",
    app_converter: () => `An error, or no update for ${p.stalledMinutes} min`,
    app_disk: () => `Disk ${p.thresholdPercent}% or more used`,
    app_errors: () => `${num(p.threshold)}+ server errors within ${p.minutes} min`,
    logs_stale: () => `${scope(p.clusters, "All clusters")}: no new log file for ${p.minutes} min`,
    log_errors: () => `${scope(p.clusters, "")}: ${num(p.threshold)}+ lines of “${p.query || "anything"}” within ${p.minutes} min`,
    lb_logs_stale: () => `${scope(p.lbs, "All load balancers")}: ${p.mode === "all" ? "none of them" : "any one"} delivered a file for ${p.minutes} min`,
    lb_errors: () => `${scope(p.targetGroups, "All target groups")}: ${p.statusClass} ≥ ${p.thresholdPercent}% of requests (${num(p.minRequests)}+) within ${p.minutes} min`,
    lb_status: () => `${scope(p.targetGroups, "All target groups")}: ${num(p.threshold)}+ requests with ${p.codes}${Number(p.thresholdPercent) ? ` and ≥ ${p.thresholdPercent}%` : ""} within ${p.minutes} min`,
    lb_latency: () => `${scope(p.targetGroups, "All target groups")}: p95 ≥ ${num(p.p95Ms)} ms (${num(p.minRequests)}+ requests) within ${p.minutes} min`,
    ecs_targets: () => `${scope(p.clusters, "All clusters")}: ${p.minUnhealthy}+ unhealthy target${p.minUnhealthy === 1 ? "" : "s"}`,
    ecs_tasks: () => `${scope(p.clusters, "All clusters")}: a service runs fewer tasks than desired`,
    ecs_usage: () => `${scope(p.clusters, "All clusters")}: ${p.metric === "cpu" ? "CPU" : "memory"} ≥ ${p.thresholdPercent}% on average over ${p.minutes} min`,
  };
  let s = text[r.type]?.() ?? r.type;
  if (r.forMinutes) s += ` · for ${r.forMinutes} min`;
  if (r.repeatMinutes) s += ` · remind every ${r.repeatMinutes} min`;
  return s;
}

function RuleState({ r }: { r: AlertRule }) {
  if (!r.enabled) return <Badge>Off</Badge>;
  if (r.status.lastError) return <Badge tone="amber" title={r.status.lastError}>Can't check</Badge>;
  if (r.status.firing.length) return <Badge tone="red">Failing · {r.status.firing.length}</Badge>;
  if (r.status.pending.length) return <Badge tone="amber" title="Bad, waiting for the rule's 'for' time">Pending · {r.status.pending.length}</Badge>;
  if (!r.status.lastRunAt) return <Badge>Not checked yet</Badge>;
  return <Badge tone="green">OK</Badge>;
}

function RulesTable({ d, onEdit }: { d: AlertsOverview; onEdit: (r: AlertRule) => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [open, setOpen] = useState<Set<string>>(new Set());
  const groups = useMemo(() => {
    const order = [...new Set(d.catalog.map((t) => t.group))];
    const typeGroup = Object.fromEntries(d.catalog.map((t) => [t.type, t.group]));
    return order.map((g) => [g, d.rules.filter((r) => typeGroup[r.type] === g)] as const).filter(([, rs]) => rs.length);
  }, [d]);
  const toggle = useMutation({
    mutationFn: (r: AlertRule) => request(`/admin/alerts/rules/${r.id}`, { method: "PUT", body: { enabled: !r.enabled } }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["alerts"] }),
    onError: (e) => toast(String((e as Error).message), "error"),
  });
  const remove = useMutation({
    mutationFn: (r: AlertRule) => request(`/admin/alerts/rules/${r.id}`, { method: "DELETE" }),
    onSuccess: () => { toast("Rule deleted"); qc.invalidateQueries({ queryKey: ["alerts"] }); },
    onError: (e) => toast(String((e as Error).message), "error"),
  });
  if (!d.rules.length) return <Empty title="No alert rules">Add one with “Add alert”.</Empty>;
  return (
    <div className="table-scroll">
      <table className="table">
        <thead><tr><th style={{ width: 54 }}>On</th><th>Rule</th><th>Alerts when</th><th>State</th><th>Checked</th><th><span className="sr-only">Actions</span></th></tr></thead>
        <tbody>
          {groups.map(([g, rules]) => (
            <Fragment key={g}>
              <tr><td colSpan={6} style={{ background: "var(--surface-2)", fontWeight: 600, fontSize: 12, textTransform: "uppercase", letterSpacing: ".04em", color: "var(--text-2)", padding: "8px 18px" }}>{g}</td></tr>
              {rules.map((r) => {
                const subjects = [...r.status.firing, ...r.status.pending];
                const isOpen = open.has(r.id);
                return (
                  <Fragment key={r.id}>
                    <tr>
                      <td><label className="check" title={r.enabled ? "Turn off" : "Turn on"}><input type="checkbox" checked={r.enabled} disabled={toggle.isPending} onChange={() => toggle.mutate(r)} /><span className="sr-only">On</span></label></td>
                      <td>
                        <div style={{ fontWeight: 600 }}>{r.name}</div>
                        {r.name !== r.title && <div className="hint">{r.title}</div>}
                      </td>
                      <td style={{ maxWidth: 460 }}>{describeRule(r)}</td>
                      <td className="nowrap">
                        {subjects.length || r.status.lastError ? (
                          <button type="button" className="linkish" onClick={() => setOpen((s) => { const n = new Set(s); if (n.has(r.id)) n.delete(r.id); else n.add(r.id); return n; })}>
                            <RuleState r={r} /> <Icon name="caret" size={14} style={{ transform: isOpen ? "rotate(180deg)" : undefined, verticalAlign: "middle" }} />
                          </button>
                        ) : <RuleState r={r} />}
                      </td>
                      <td className="nowrap hint">{r.enabled && r.status.lastRunAt ? ago(new Date(r.status.lastRunAt).toISOString()) : "—"}</td>
                      <td className="nowrap" style={{ textAlign: "right" }}>
                        <button type="button" className="btn btn-ghost btn-sm" onClick={() => onEdit(r)}>Edit</button>
                        <button type="button" className="btn btn-ghost btn-sm" aria-label={`Delete ${r.name}`} title="Delete"
                          onClick={() => { if (window.confirm(`Delete the rule “${r.name}”?`)) remove.mutate(r); }}><Icon name="trash" size={16} /></button>
                      </td>
                    </tr>
                    {isOpen && (
                      <tr><td colSpan={6} style={{ background: "var(--surface-2)" }}>
                        <div className="stack-sm">
                          {r.status.lastError && <Callout tone="warn" icon="warn" title="This rule can't check right now">{r.status.lastError}</Callout>}
                          {subjects.map((s) => (
                            <div key={s.subject} className="row" style={{ gap: 10, alignItems: "flex-start" }}>
                              <Badge tone={s.state === "firing" ? "red" : "amber"}>{s.state === "firing" ? "failing" : "pending"}</Badge>
                              <div style={{ minWidth: 0 }}>
                                <div><strong className="mono">{s.subject}</strong> <span className="hint">since {ago(new Date(s.firedAt ?? s.since).toISOString())}</span></div>
                                <div style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{s.message}</div>
                                {s.link && <Link to={s.link.replace(/^\/ui/, "")} className="hlink">Open</Link>}
                              </div>
                            </div>
                          ))}
                        </div>
                      </td></tr>
                    )}
                  </Fragment>
                );
              })}
            </Fragment>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function TypePicker({ catalog, onPick, onClose }: { catalog: AlertType[]; onPick: (t: AlertType) => void; onClose: () => void }) {
  const groups = [...new Set(catalog.map((t) => t.group))];
  return (
    <Dialog title="Add an alert" subtitle="What should be checked? Every type can be added more than once with other limits or scopes." onClose={onClose} wide>
      <div className="stack">
        {groups.map((g) => (
          <div key={g} className="stack-sm">
            <span className="label">{g}</span>
            <div className="grid-2" style={{ gap: 10 }}>
              {catalog.filter((t) => t.group === g).map((t) => (
                <button key={t.type} type="button" className="link-card" style={{ textAlign: "left", cursor: "pointer" }} onClick={() => onPick(t)}>
                  <strong>{t.title}</strong>
                  <span className="hint" style={{ display: "block", marginTop: 4 }}>{t.description}</span>
                </button>
              ))}
            </div>
          </div>
        ))}
      </div>
    </Dialog>
  );
}

// ------------------------------------------------------------------ the rule editor

function RuleDialog({ type, rule, lag, onClose }: { type: AlertType; rule?: AlertRule; lag: number; onClose: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const needsOptions = type.params.some((p) => ["logClusters", "ecsClusters", "lbs", "targetGroups"].includes(p.kind));
  const opts = useQuery({ queryKey: ["alert-options"], queryFn: () => get<AlertOptions>("/admin/alerts/options"), enabled: needsOptions, staleTime: 60_000 });
  const [name, setName] = useState(rule?.name ?? type.title);
  const [enabled, setEnabled] = useState(rule?.enabled ?? true);
  const [params, setParams] = useState<Record<string, unknown>>(() => ({
    ...Object.fromEntries(type.params.map((p) => [p.key, p.default])), ...(rule?.params ?? {}),
  }));
  const [forMinutes, setFor] = useState(rule?.forMinutes ?? type.forMinutes);
  const [repeatMinutes, setRepeat] = useState(rule?.repeatMinutes ?? 0);
  const [error, setError] = useState<unknown>(null);
  const [preview, setPreview] = useState<{ findings: AlertFinding[]; bad: number } | null>(null);
  const body = { type: type.type, name, enabled, params, forMinutes, repeatMinutes };
  const save = useMutation({
    mutationFn: () => rule
      ? request(`/admin/alerts/rules/${rule.id}`, { method: "PUT", body: { name, enabled, params, forMinutes, repeatMinutes } })
      : request("/admin/alerts/rules", { method: "POST", body }),
    onSuccess: () => { toast(rule ? "Rule saved" : "Rule added"); qc.invalidateQueries({ queryKey: ["alerts"] }); onClose(); },
    onError: setError,
  });
  const check = useMutation({
    mutationFn: () => request<{ findings: AlertFinding[]; bad: number }>("/admin/alerts/_preview", { method: "POST", body }),
    onSuccess: (r) => { setError(null); setPreview(r); },
    onError: (e) => { setPreview(null); setError(e); },
  });
  const set = (k: string, v: unknown) => { setParams((p) => ({ ...p, [k]: v })); setPreview(null); };
  const busy = save.isPending || check.isPending;
  return (
    <Dialog title={rule ? `Edit “${rule.name}”` : `New alert: ${type.title}`} subtitle={type.description} onClose={onClose} wide busy={busy}
      footer={<>
        <button type="button" className="btn" onClick={() => check.mutate()} disabled={busy}>{check.isPending ? <Spinner label="Checking" /> : <Icon name="search" />} Check now</button>
        <div className="grow" />
        <button type="button" className="btn" onClick={onClose} disabled={busy}>Cancel</button>
        <button type="button" className="btn btn-primary" onClick={() => save.mutate()} disabled={busy}>{save.isPending ? <Spinner /> : null} {rule ? "Save" : "Add alert"}</button>
      </>}>
      <div className="stack">
        <div className="grid-2" style={{ gap: 16 }}>
          <div className="field">
            <label className="label" htmlFor="rname">Name <span className="hint">· shown in Slack</span></label>
            <input id="rname" className="input" maxLength={80} value={name} onChange={(e) => setName(e.target.value)} />
          </div>
          <label className="check" style={{ alignSelf: "end", paddingBottom: 10 }}><input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} /> On</label>
        </div>
        {type.params.map((p) => (
          <ParamField key={p.key} spec={p} value={params[p.key]} onChange={(v) => set(p.key, v)} options={opts.data} loading={opts.isLoading} />
        ))}
        {opts.data?.errors.length ? <Callout tone="warn" icon="warn" title="Some names couldn't be listed">{opts.data.errors.join(" · ")}</Callout> : null}
        <div className="grid-2" style={{ gap: 16 }}>
          <div className="field">
            <label className="label" htmlFor="rfor">Alert only after it has been bad for</label>
            <div className="row" style={{ gap: 8 }}><input id="rfor" className="input" type="number" min={0} max={1440} value={forMinutes} onChange={(e) => setFor(Number(e.target.value))} style={{ width: 110 }} /> <span className="hint">min · 0 = at once</span></div>
          </div>
          <div className="field">
            <label className="label" htmlFor="rrep">Remind while it's still failing every</label>
            <div className="row" style={{ gap: 8 }}><input id="rrep" className="input" type="number" min={0} max={10080} value={repeatMinutes} onChange={(e) => setRepeat(Number(e.target.value))} style={{ width: 110 }} /> <span className="hint">min · 0 = never</span></div>
          </div>
        </div>
        <span className="hint">
          Checked every {type.every === 1 ? "minute" : `${type.every} minutes`}. One Slack message when it starts failing, one when it's back to normal.
          {["lb_errors", "lb_status", "lb_latency"].includes(type.type) ? ` Load balancer data is about ${lag} minutes behind.` : ""}
        </span>
        <span className="hint" style={{ fontStyle: "italic" }}>{describeRule({ type: type.type, params, forMinutes, repeatMinutes })}</span>
        {preview && <Preview data={preview} />}
        {error ? <ErrorCallout error={error} admin /> : null}
      </div>
    </Dialog>
  );
}

function ParamField({ spec, value, onChange, options, loading }: {
  spec: AlertParamSpec; value: unknown; onChange: (v: unknown) => void; options?: AlertOptions; loading: boolean;
}) {
  const id = `p-${spec.key}`;
  const help = spec.help ? <span className="hint">{spec.help}</span> : null;
  if (spec.kind === "number") {
    return (
      <div className="field">
        <label className="label" htmlFor={id}>{spec.label}</label>
        <div className="row" style={{ gap: 8 }}>
          <input id={id} className="input" type="number" min={spec.min} max={spec.max} step="any" style={{ width: 140 }}
            value={value as number} onChange={(e) => onChange(e.target.value === "" ? "" : Number(e.target.value))} />
          {spec.unit && <span className="hint">{spec.unit}</span>}
        </div>
        {help}
      </div>
    );
  }
  if (spec.kind === "select") {
    return (
      <div className="field">
        <label className="label" htmlFor={id}>{spec.label}</label>
        <select id={id} className="select" value={value as string} onChange={(e) => onChange(e.target.value)}>
          {spec.options!.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
        </select>
        {help}
      </div>
    );
  }
  if (spec.kind === "text" || spec.kind === "codes") {
    return (
      <div className="field">
        <label className="label" htmlFor={id}>{spec.label}</label>
        <input id={id} className="input mono" value={value as string} onChange={(e) => onChange(e.target.value)} />
        {help}
      </div>
    );
  }
  if (spec.kind === "bool") {
    return (
      <div className="field">
        <label className="check"><input type="checkbox" checked={!!value} onChange={(e) => onChange(e.target.checked)} /> {spec.label}</label>
        {help}
      </div>
    );
  }
  const all = (options?.[spec.kind as "logClusters"] ?? []) as string[];
  return <NamePicker label={spec.label} help={spec.help} required={spec.required} names={all} value={(value as string[]) ?? []} onChange={onChange} loading={loading} />;
}

function NamePicker({ label, help, required, names, value, onChange, loading }: {
  label: string; help?: string; required?: boolean; names: string[]; value: string[]; onChange: (v: string[]) => void; loading: boolean;
}) {
  const [filter, setFilter] = useState("");
  const list = [...new Set([...value, ...names])].sort();
  const f = filter.trim().toLowerCase();
  const shown = list.filter((n) => n.toLowerCase().includes(f));
  const on = new Set(value);
  const flip = (n: string) => onChange(on.has(n) ? value.filter((x) => x !== n) : [...value, n]);
  const allShownOn = shown.length > 0 && shown.every((n) => on.has(n));
  const selectShown = () => onChange([...new Set([...value, ...shown])]);
  const clearShown = () => onChange(f ? value.filter((n) => !shown.includes(n)) : []);
  return (
    <div className="field">
      <span className="label">{label} <span className="hint">· {value.length ? `${value.length} of ${list.length} picked` : required ? "pick at least one" : "none picked = all, including new ones"}</span></span>
      {loading ? <Loading what="Listing names…" /> : (
        <div className="picker">
          <div className="picker-bar">
            <div style={{ flex: "1 1 200px" }}><SearchInput label={`Search ${label}`} placeholder={`Search ${label.toLowerCase()}`} value={filter} onChange={setFilter} /></div>
            <button type="button" className="btn btn-sm" onClick={selectShown} disabled={!shown.length || allShownOn}>
              {f ? `Select ${shown.length} shown` : "Select all"}
            </button>
            <button type="button" className="btn btn-sm btn-ghost" onClick={clearShown} disabled={!value.length}>
              {f ? "Clear shown" : required ? "Clear" : "Clear (= all)"}
            </button>
          </div>
          <div className="picker-list">
            {shown.map((n) => (
              <label key={n} className="check" style={{ fontSize: 13 }}><input type="checkbox" checked={on.has(n)} onChange={() => flip(n)} /> <span className="mono" style={{ overflowWrap: "anywhere" }}>{n}</span></label>
            ))}
            {!shown.length && <span className="hint">{list.length ? "No name matches the search." : "Nothing to pick yet."}</span>}
          </div>
        </div>
      )}
      {help && <span className="hint">{help}</span>}
    </div>
  );
}

function Preview({ data }: { data: { findings: AlertFinding[]; bad: number } }) {
  if (!data.findings.length) return <Callout tone="neutral" icon="info" title="Nothing to check">The rule found nothing in its scope right now (no data, or no names match).</Callout>;
  return (
    <div className="stack-sm">
      <span className="label">Right now: {data.bad ? <span className="danger-text">{data.bad} would alert</span> : "nothing would alert"} · {data.findings.length} checked</span>
      <div style={{ border: "1px solid var(--border)", borderRadius: 10, maxHeight: 240, overflow: "auto" }}>
        <table className="table" style={{ fontSize: 13 }}>
          <tbody>
            {[...data.findings].sort((a, b) => Number(b.bad) - Number(a.bad)).map((f) => (
              <tr key={f.subject}>
                <td className="nowrap" style={{ width: 1 }}>{f.bad ? <Badge tone="red">alert</Badge> : <Badge tone="green">ok</Badge>}</td>
                <td className="mono nowrap">{f.subject}</td>
                <td style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{f.message}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ history

function History() {
  const zone = storedZone();
  const q = useQuery({ queryKey: ["alert-history"], queryFn: () => get<{ items: AlertEvent[] }>("/alerts/history", { limit: 200 }), refetchInterval: 60_000 });
  const items = q.data?.items ?? [];
  return (
    <section className="card" style={{ overflow: "hidden" }}>
      <div className="card-head">
        <div className="grow"><span className="card-title">Recent alerts</span><span className="hint">The last {num(200)} messages, newest first. Times in {zone === "utc" ? "UTC" : localZoneName()}.</span></div>
      </div>
      {q.isLoading ? <Loading /> : q.error ? <div className="card-body"><ErrorCallout error={q.error} /></div> : !items.length ? (
        <Empty title="No alerts yet">Nothing has gone wrong since alerts were turned on.</Empty>
      ) : (
        <div className="table-scroll">
          <table className="table">
            <thead><tr><th>Time</th><th>Event</th><th>Rule</th><th>About</th><th>Details</th><th>Slack</th></tr></thead>
            <tbody>
              {items.map((e) => (
                <tr key={e.id}>
                  <td className="nowrap mono" style={{ fontSize: 13 }}>{fmtTime(e.at, zone, false)}</td>
                  <td>{e.event === "resolved" ? <Badge tone="green">resolved</Badge> : <Badge tone="red">{e.event === "repeat" ? "still failing" : "failing"}</Badge>}</td>
                  <td>{e.ruleName}</td>
                  <td className="mono nowrap">{e.link ? <Link to={e.link.replace(/^\/ui/, "")} className="hlink">{e.subject}</Link> : e.subject}</td>
                  <td style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere", maxWidth: 520, fontSize: 13 }}>{e.message}</td>
                  <td className="nowrap">{e.delivered ? <span className="hint">sent</span> : <span className="warn-text" title={e.error ?? ""}>not sent</span>}{!e.delivered && e.error ? <Detail text={e.error} /> : null}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function Detail({ text }: { text: string }): ReactNode {
  return <div className="hint" style={{ whiteSpace: "normal", maxWidth: 220, fontSize: 12 }}>{text}</div>;
}
