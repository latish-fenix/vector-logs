import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useMemo, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import {
  get, request,
  type AlertEvent, type AlertFinding, type AlertLevel, type AlertMuteEntry, type AlertOptions, type AlertPair, type AlertParamSpec, type AlertRule,
  type AlertSettings, type AlertSubject, type AlertThresholds, type AlertsOverview, type AlertType,
} from "../api";
import { Icon } from "../components/icons";
import { Page } from "../components/Shell";
import { Badge, Callout, Dialog, Empty, ErrorCallout, Loading, SearchInput, SearchSelect, Spinner, useToast } from "../components/ui";
import { ago, fmtTime, localZoneName, num, storedZone } from "../format";
import { useMe } from "../session";

// Alerts: Slack webhook, the rules (what is checked and when it alerts) and what was sent.
// Admins manage everything; members see the alerts about their clusters.

const ZONES = ["Asia/Kolkata", "America/Los_Angeles", "UTC", "America/New_York", "America/Chicago", "Europe/London"];
const ZONE_LABELS: Record<string, string> = { "Asia/Kolkata": "India (IST)", "America/Los_Angeles": "Pacific (PST / PDT)" };
const zoneLabel = (z: string) => ZONE_LABELS[z] ?? z;

export function Alerts() {
  const me = useMe().data!;
  return (
    <Page crumbs={[{ label: "Infrastructure" }, { label: "Alerts" }]} title="Alerts">
      <div className="page-head">
        <div className="grow">
          <h1>Alerts</h1>
          <span className="sub">
            {me.admin
              ? "The server checks these rules all the time and posts to Slack when something goes wrong (warning or critical), when it gets worse or better, and when it's OK again."
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
  const critical = d.rules.reduce((n, r) => n + r.status.firing.filter((s) => s.level !== "warning").length, 0);
  const mutedNow = d.rules.reduce((n, r) => n + (r.status.muted?.length ?? 0), 0);
  return (
    <>
      {!d.slack.configured && (
        <Callout tone="warn" icon="warn" title="No Slack webhook yet">The rules are checked and their alerts are kept in the history below, but nothing is posted to Slack until you add a webhook.</Callout>
      )}
      {d.settings.paused && <Callout tone="warn" icon="warn" title="Notifications are paused">Rules are still checked; nothing is posted to Slack until you resume.</Callout>}
      <div className="stats" style={{ gridTemplateColumns: "repeat(4, minmax(0, 1fr))" }}>
        <div className="stat"><span className="stat-label">Failing now</span><span className="stat-value" style={{ color: critical ? "var(--danger)" : firing ? "var(--warn)" : undefined }}>{num(firing)}</span><span className="stat-note">{firing ? `${num(critical)} critical · ${num(firing - critical)} warning${d.snoozes.length ? ` · ${num(d.snoozes.length)} snoozed` : ""}` : "all clear"}{mutedNow ? ` · ${num(mutedNow)} muted` : ""}</span></div>
        <div className="stat"><span className="stat-label">Rules on</span><span className="stat-value">{num(d.rules.filter((r) => r.enabled).length)}</span><span className="stat-note">of {num(d.rules.length)}</span></div>
        <div className="stat"><span className="stat-label">Slack</span><span className="stat-value" style={{ fontSize: 20 }}>{d.slack.configured ? (d.settings.paused ? "Paused" : "Connected") : "Not set"}</span><span className="stat-note">{d.slack.configured ? `set by ${d.slack.updatedBy ?? "?"} ${ago(d.slack.updatedAt)}` : "add a webhook below"}</span></div>
        <div className="stat"><span className="stat-label">Last check</span><span className="stat-value" style={{ fontSize: 20 }}>{d.checker.checkedAt ? ago(new Date(d.checker.checkedAt).toISOString()) : "not yet"}</span><span className="stat-note">{d.checker.background ? `every ${d.checker.intervalSeconds} s` : "the checker is off (ALERTS_BACKGROUND=false)"}</span></div>
      </div>
      <SlackCard d={d} />
      <ThresholdsCard d={d} />
      <MutesCard d={d} />
      <section className="card" style={{ overflow: "hidden" }}>
        <div className="card-head">
          <div className="grow"><span className="card-title">Rules</span><span className="hint">Turn rules on or off, change their limits, or add more. A rule that can't run shows its error.</span></div>
          <button type="button" className="btn btn-primary" onClick={() => setAdding(true)}><Icon name="plus" /> Add alert</button>
        </div>
        <RulesTable d={d} onEdit={(rule) => setEditing({ rule, type: d.catalog.find((t) => t.type === rule.type) })} />
      </section>
      <History />
      {adding && <TypePicker catalog={d.catalog} onClose={() => setAdding(false)} onPick={(type) => { setAdding(false); setEditing({ type }); }} />}
      {editing?.type && <RuleDialog type={editing.type} rule={editing.rule} lag={d.lbLagMinutes} settings={d.settings} onClose={() => setEditing(null)} />}
    </>
  );
}

function SlackCard({ d }: { d: AlertsOverview }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [url, setUrl] = useState("");
  const [consoleUrl, setConsoleUrl] = useState(d.settings.consoleUrl || "");
  const [tz, setTz] = useState(d.settings.timeZone || "UTC");
  const [tz2, setTz2] = useState(d.settings.timeZone2 ?? "");
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
  const zones2 = !tz2 || ZONES.includes(tz2) ? ZONES : [tz2, ...ZONES];
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
              <input id="curl" className="input" style={{ flex: "1 1 240px", minWidth: 0 }} placeholder="https://app-logs-int.infra.fenixcommerce.com" value={consoleUrl} onChange={(e) => setConsoleUrl(e.target.value)} />
              {!consoleUrl && <button type="button" className="btn btn-sm nowrap" onClick={() => setConsoleUrl(here)}>Use {here.replace(/^https?:\/\//, "")}</button>}
            </div>
          </div>
          <div className="field">
            <label className="label" htmlFor="tz">Times in Slack <span className="hint">· shown in both zones, e.g. 09:05 IST / 20:35 PDT</span></label>
            <div className="row" style={{ gap: 8 }}>
              <select id="tz" aria-label="First time zone" className="select" style={{ flex: "1 1 0", minWidth: 0 }} value={tz} onChange={(e) => setTz(e.target.value)}>
                {zones.map((z) => <option key={z} value={z}>{zoneLabel(z)}</option>)}
              </select>
              <select id="tz2" aria-label="Second time zone" className="select" style={{ flex: "1 1 0", minWidth: 0 }} value={tz2} onChange={(e) => setTz2(e.target.value)}>
                <option value="">No second zone</option>
                {zones2.filter((z) => z !== tz).map((z) => <option key={z} value={z}>{zoneLabel(z)}</option>)}
              </select>
            </div>
          </div>
        </div>
        {(consoleUrl !== (d.settings.consoleUrl || "") || tz !== d.settings.timeZone || tz2 !== (d.settings.timeZone2 ?? "")) && (
          <div className="row" style={{ gap: 8 }}>
            <button type="button" className="btn btn-primary btn-sm" disabled={busy} onClick={() => settings.mutate({ consoleUrl, timeZone: tz, timeZone2: tz2 === tz ? "" : tz2 })}>Save address and time zones</button>
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => { setConsoleUrl(d.settings.consoleUrl || ""); setTz(d.settings.timeZone); setTz2(d.settings.timeZone2 ?? ""); }}>Cancel</button>
          </div>
        )}
        {error ? <ErrorCallout error={error} admin /> : null}
      </div>
    </section>
  );
}

// ------------------------------------------------------------------ thresholds, reminders, weekly summary

const DAY_NAMES: Record<string, string> = { mon: "Monday", tue: "Tuesday", wed: "Wednesday", thu: "Thursday", fri: "Friday", sat: "Saturday", sun: "Sunday" };

function PairInputs({ id, value, onChange }: { id: string; value: AlertPair; onChange: (v: AlertPair) => void }) {
  const bad = !(value.warning < value.critical);
  return (
    <>
      <td><input id={`${id}-w`} aria-label="Warning at" className="input" type="number" min={1} max={100} value={value.warning}
        onChange={(e) => onChange({ ...value, warning: Number(e.target.value) })} style={{ width: 78 }} /></td>
      <td><input id={`${id}-c`} aria-label="Critical at" className="input" aria-invalid={bad} type="number" min={1} max={100} value={value.critical}
        title={bad ? "Critical must be above warning" : undefined}
        onChange={(e) => onChange({ ...value, critical: Number(e.target.value) })} style={{ width: 78 }} /></td>
    </>
  );
}

function ThresholdsCard({ d }: { d: AlertsOverview }) {
  const qc = useQueryClient();
  const toast = useToast();
  const s = d.settings;
  const [t, setT] = useState<AlertThresholds>(() => JSON.parse(JSON.stringify(s.thresholds)));
  const [repeat, setRepeat] = useState(s.repeat);
  const [digest, setDigest] = useState(s.digest);
  const [picking, setPicking] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const opts = useQuery({ queryKey: ["alert-options"], queryFn: () => get<AlertOptions>("/admin/alerts/options"), enabled: picking, staleTime: 60_000 });
  const dirty = JSON.stringify([t, repeat, digest]) !== JSON.stringify([s.thresholds, s.repeat, s.digest]);
  const pairs = [t.cpu, t.memory, t.disk, ...Object.values(t.clusters).flatMap((c) => [c.cpu, c.memory])].filter(Boolean) as AlertPair[];
  const invalid = pairs.some((p) => !(p.warning < p.critical) || p.warning < 1 || p.critical > 100);
  const save = useMutation({
    mutationFn: () => request("/admin/alerts/settings", { method: "PUT", body: { thresholds: t, repeat, digest } }),
    onSuccess: () => { setError(null); toast("Saved"); qc.invalidateQueries({ queryKey: ["alerts"] }); },
    onError: setError,
  });
  const send = useMutation({
    mutationFn: () => request("/admin/alerts/digest/_send", { method: "POST" }),
    onSuccess: () => { setError(null); toast("Weekly summary posted to Slack"); qc.invalidateQueries({ queryKey: ["alert-history"] }); },
    onError: setError,
  });
  const reset = () => { setT(JSON.parse(JSON.stringify(s.thresholds))); setRepeat(s.repeat); setDigest(s.digest); setError(null); };
  const setCluster = (name: string, metric: "cpu" | "memory", v: AlertPair) =>
    setT((x) => ({ ...x, clusters: { ...x.clusters, [name]: { ...x.clusters[name], [metric]: v } } }));
  const dropCluster = (name: string) => setT((x) => { const c = { ...x.clusters }; delete c[name]; return { ...x, clusters: c }; });
  const overrides = Object.keys(t.clusters).sort();
  const candidates = (opts.data?.ecsClusters ?? []).filter((n) => !t.clusters[n]);
  const zone = s.timeZone === "Asia/Kolkata" ? "IST" : s.timeZone === "America/Los_Angeles" ? "Pacific time" : s.timeZone;
  return (
    <section className="card">
      <div className="card-head">
        <div className="grow"><span className="card-title">Thresholds, reminders and the weekly summary</span>
          <span className="hint">Two lines: warning and critical. An alert clears only 2 points below its line, so a value hovering around it doesn't post again and again. CPU alerts after 2 checks in a row (every 5 minutes).</span></div>
      </div>
      <div className="card-body stack">
        <div className="table-scroll no-max" style={{ border: "1px solid var(--border)", borderRadius: 10 }}>
          <table className="table" style={{ fontSize: 13 }}>
            <thead>
              <tr><th>ECS CPU and memory</th><th>CPU warning %</th><th>CPU critical %</th><th>Memory warning %</th><th>Memory critical %</th><th><span className="sr-only">Remove</span></th></tr>
            </thead>
            <tbody>
              <tr>
                <td className="nowrap"><strong>All clusters</strong></td>
                <PairInputs id="t-cpu" value={t.cpu} onChange={(v) => setT((x) => ({ ...x, cpu: v }))} />
                <PairInputs id="t-mem" value={t.memory} onChange={(v) => setT((x) => ({ ...x, memory: v }))} />
                <td />
              </tr>
              {overrides.map((name) => (
                <tr key={name}>
                  <td className="mono nowrap">{name}</td>
                  <PairInputs id={`t-${name}-cpu`} value={t.clusters[name].cpu ?? t.cpu} onChange={(v) => setCluster(name, "cpu", v)} />
                  <PairInputs id={`t-${name}-mem`} value={t.clusters[name].memory ?? t.memory} onChange={(v) => setCluster(name, "memory", v)} />
                  <td style={{ textAlign: "right" }}><button type="button" className="btn btn-ghost btn-sm" aria-label={`Remove ${name}`} title="Use the lines of all clusters" onClick={() => dropCluster(name)}><Icon name="trash" size={16} /></button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
          {!picking ? (
            <button type="button" className="btn btn-sm" onClick={() => setPicking(true)}><Icon name="plus" /> Other lines for one cluster</button>
          ) : opts.isLoading ? <Loading what="Listing ECS clusters…" /> : (
            <>
              <SearchSelect id="t-add" label="ECS cluster" value="" mono allLabel="Pick an ECS cluster…" options={candidates.map((n) => ({ value: n }))}
                onChange={(n) => { if (n) { setT((x) => ({ ...x, clusters: { ...x.clusters, [n]: { cpu: { ...x.cpu }, memory: { ...x.memory } } } })); setPicking(false); } }} />
              <button type="button" className="btn btn-ghost btn-sm" onClick={() => setPicking(false)}>Cancel</button>
            </>
          )}
        </div>
        <div className="grid-2" style={{ gap: 16 }}>
          <div className="field">
            <span className="label">Viewer server disk</span>
            <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
              <span className="hint">warning at</span>
              <input aria-label="Disk warning at" className="input" type="number" min={1} max={100} value={t.disk.warning} onChange={(e) => setT((x) => ({ ...x, disk: { ...x.disk, warning: Number(e.target.value) } }))} style={{ width: 78 }} />
              <span className="hint">% · critical at</span>
              <input aria-label="Disk critical at" className="input" type="number" min={1} max={100} value={t.disk.critical} onChange={(e) => setT((x) => ({ ...x, disk: { ...x.disk, critical: Number(e.target.value) } }))} style={{ width: 78 }} />
              <span className="hint">%</span>
            </div>
            <span className="hint">The server this console runs on. Load balancer error rates keep their own lines in their rules.</span>
          </div>
          <div className="field">
            <span className="label">Remind while it's still failing</span>
            <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
              <span className="hint">warnings every</span>
              <input aria-label="Warnings every" className="input" type="number" min={0} max={1440} value={repeat.warning} onChange={(e) => setRepeat((r) => ({ ...r, warning: Number(e.target.value) }))} style={{ width: 78 }} />
              <span className="hint">min · criticals every</span>
              <input aria-label="Criticals every" className="input" type="number" min={0} max={1440} value={repeat.critical} onChange={(e) => setRepeat((r) => ({ ...r, critical: Number(e.target.value) }))} style={{ width: 78 }} />
              <span className="hint">min · 0 = never</span>
            </div>
            <span className="hint">A change of level (warning ↔ critical) is posted at once. Snooze an alert from its rule below.</span>
          </div>
        </div>
        <div className="field">
          <span className="label">Weekly summary</span>
          <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
            <label className="check"><input type="checkbox" checked={digest.enabled} onChange={(e) => setDigest((x) => ({ ...x, enabled: e.target.checked }))} /> Post every</label>
            <select aria-label="Day" className="select" style={{ width: "auto" }} value={digest.day} disabled={!digest.enabled} onChange={(e) => setDigest((x) => ({ ...x, day: e.target.value }))}>
              {d.days.map((x) => <option key={x} value={x}>{DAY_NAMES[x] ?? x}</option>)}
            </select>
            <span className="hint">at</span>
            <input aria-label="Time" className="input" type="time" value={digest.time} disabled={!digest.enabled} onChange={(e) => setDigest((x) => ({ ...x, time: e.target.value }))} style={{ width: 120 }} />
            <span className="hint">{zone}</span>
            <div className="grow" />
            <button type="button" className="btn btn-sm" disabled={!d.slack.configured || send.isPending} onClick={() => send.mutate()}>{send.isPending ? <Spinner /> : null} Send it now</button>
          </div>
          <span className="hint">Alerts of the last 7 days per rule, what is failing now, what is snoozed, and rules that can't check.{d.checker.digestSentFor ? ` Last sent for ${d.checker.digestSentFor}.` : ""}</span>
        </div>
        {dirty && (
          <div className="row" style={{ gap: 8 }}>
            <button type="button" className="btn btn-primary btn-sm" disabled={save.isPending || invalid} onClick={() => save.mutate()}>{save.isPending ? <Spinner /> : null} Save</button>
            <button type="button" className="btn btn-ghost btn-sm" onClick={reset}>Cancel</button>
            {invalid && <span className="warn-text" style={{ fontSize: 13 }}>Each critical line must be above its warning line (1–100%).</span>}
          </div>
        )}
        {error ? <ErrorCallout error={error} admin /> : null}
      </div>
    </section>
  );
}

// ------------------------------------------------------------------ muted alerts (config/alert-mutes.yaml)

const MUTE_KIND: Record<AlertMuteEntry["kind"], string> = {
  subject: "names", path: "paths", signature: "error text", health: "target health", converter: "converter",
};

function MutesCard({ d }: { d: AlertsOverview }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [filter, setFilter] = useState("");
  const m = d.mutes;
  const pause = useMutation({
    mutationFn: (paused: boolean) => request("/admin/alerts/settings", { method: "PUT", body: { mutesPaused: paused } }),
    onSuccess: (_, paused) => { toast(paused ? "Mutes paused: every alert is posted" : "Mutes back on"); qc.invalidateQueries({ queryKey: ["alerts"] }); },
    onError: (e) => toast(String((e as Error).message), "error"),
  });
  const f = filter.trim().toLowerCase();
  const shown = m.entries.filter((x) => !f || [x.id, x.title, x.owner, x.reason, x.matches].join(" ").toLowerCase().includes(f));
  const on = m.entries.filter((x) => x.enabled).length;
  const file = m.file.replace(/^.*[\\/](config[\\/])/, "$1");
  return (
    <section className="card" style={{ overflow: "hidden" }}>
      <div className="card-head">
        <div className="grow">
          <span className="card-title">Muted alerts</span>
          <span className="hint">
            From <span className="mono">{file}</span> in the repo: edit it (or git pull on the server) and it's used within a minute, no restart.
            {" "}Muted alerts aren't posted to Slack; they stay in Recent alerts as “muted”. Turn one off with <span className="mono">enabled: false</span>.
          </span>
        </div>
        <button type="button" className={`btn btn-sm${m.paused ? " btn-primary" : ""}`} disabled={pause.isPending} onClick={() => pause.mutate(!m.paused)}>
          {m.paused ? "Turn mutes back on" : "Pause all mutes"}
        </button>
      </div>
      <div className="card-body stack-sm">
        {m.paused && <Callout tone="warn" icon="warn" title="All mutes are paused">Every alert is posted to Slack, including the ones in the file.</Callout>}
        {m.error && <Callout tone="danger" icon="warn" title="The mute file has a mistake: the last good version is still used">{m.error}</Callout>}
        {!m.exists && !m.error && <Callout tone="neutral" icon="info" title="No mute file">Nothing is muted: {m.file} doesn't exist on the server.</Callout>}
        <div className="row" style={{ gap: 12, flexWrap: "wrap" }}>
          <span className="hint">{num(on)} of {num(m.entries.length)} entries on{m.loadedAt ? ` · read ${ago(new Date(m.loadedAt).toISOString())}` : ""}</span>
          <div className="grow" />
          {m.entries.length > 6 && <div style={{ flex: "0 1 280px" }}><SearchInput label="Search muted alerts" placeholder="Search id, text, owner" value={filter} onChange={setFilter} /></div>}
        </div>
      </div>
      {m.entries.length ? (
        <div className="table-scroll" style={{ maxHeight: 420 }}>
          <table className="table" style={{ fontSize: 13 }}>
            <thead><tr><th>Id</th><th>What is muted</th><th>Owner · reason</th><th>On</th><th title="Times it stopped an alert">Muted 24 h / 7 d</th></tr></thead>
            <tbody>
              {shown.map((x) => (
                <tr key={x.id} style={x.enabled ? undefined : { opacity: 0.55 }}>
                  <td className="mono nowrap">{x.id}</td>
                  <td style={{ maxWidth: 520 }}>
                    <div style={{ fontWeight: 600 }}>{x.title || x.id}</div>
                    <div className="hint" style={{ overflowWrap: "anywhere" }}><Badge>{MUTE_KIND[x.kind]}</Badge> {x.matches}</div>
                  </td>
                  <td style={{ maxWidth: 260 }}><div>{x.owner || "—"}</div>{x.reason && <div className="hint">{x.reason}</div>}</td>
                  <td>{x.enabled ? (m.paused ? <Badge tone="amber">paused</Badge> : <Badge tone="green">on</Badge>) : <Badge>off</Badge>}</td>
                  <td className="nowrap">{num(x.muted24h)} / {num(x.muted7d)}</td>
                </tr>
              ))}
              {!shown.length && <tr><td colSpan={5} className="hint">No entry matches the search.</td></tr>}
            </tbody>
          </table>
        </div>
      ) : null}
    </section>
  );
}

// ------------------------------------------------------------------ rules

function scope(v: unknown, all: string): string {
  const xs = (v as string[]) ?? [];
  if (!xs.length) return all;
  return xs.length <= 3 ? xs.join(", ") : `${xs.slice(0, 3).join(", ")} +${xs.length - 3}`;
}

function pair(p: AlertPair | undefined): string {
  return p ? `${p.warning}% warning, ${p.critical}% critical` : "";
}

/** How many checks in a row a rule needs before it alerts. */
function confirmOf(type: string, params: Record<string, unknown>, catalogConfirm?: number): number {
  if (type === "ecs_usage") return params.metric === "memory" ? 1 : 2;
  return catalogConfirm ?? 1;
}

/** One line saying when the rule alerts. */
export function describeRule(r: { type: string; params: Record<string, unknown>; forMinutes: number }, settings?: AlertSettings): string {
  const p = r.params as Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any
  const t = settings?.thresholds;
  const lbCrit = (w: number, c: number, unit: string) => `${num(w)}${unit} warning, ${num(Number(c) || w * 2)}${unit} critical`;
  const usage = () => {
    const m = p.metric === "memory" ? "memory" : "cpu";
    const per = t ? Object.keys(t.clusters).filter((n) => t.clusters[n][m as "cpu"]).length : 0;
    return `${scope(p.clusters, "All clusters")}: ${m === "cpu" ? "CPU" : "memory"} on average over ${p.minutes} min ≥ ${t ? pair(t[m as "cpu"]) : "the Thresholds lines"}${per ? ` (${per} cluster${per === 1 ? "" : "s"} with own lines)` : ""}`;
  };
  const text: Record<string, () => string> = {
    app_aws: () => "Any read from S3, ECS, load balancers or CloudWatch fails",
    app_converter: () => `An error, or no update for ${p.stalledMinutes} min`,
    app_disk: () => `Disk used ≥ ${t ? pair(t.disk) : "the Thresholds lines"}`,
    app_errors: () => `${num(p.threshold)}+ server errors within ${p.minutes} min`,
    logs_stale: () => `${scope(p.clusters, "All clusters")}: no new log file for ${p.minutes} min`,
    log_errors: () => `${scope(p.clusters, "")}: ${num(p.threshold)}+ lines of “${p.query || "anything"}” within ${p.minutes} min`,
    lb_logs_stale: () => `${scope(p.lbs, "All load balancers")}: ${p.mode === "all" ? "none of them" : "any one"} delivered a file for ${p.minutes} min`,
    lb_errors: () => `${scope(p.targetGroups, "All target groups")}: ${p.statusClass} ≥ ${lbCrit(p.thresholdPercent, p.criticalPercent, "%")} of requests (${num(p.minRequests)}+) within ${p.minutes} min`,
    lb_status: () => `${scope(p.targetGroups, "All target groups")}: requests with ${p.codes} ≥ ${lbCrit(p.threshold, p.criticalThreshold, "")}${Number(p.thresholdPercent) ? ` and ≥ ${p.thresholdPercent}%` : ""} within ${p.minutes} min`,
    lb_latency: () => `${scope(p.targetGroups, "All target groups")}: p95 ≥ ${num(p.p95Ms)} ms (${num(p.minRequests)}+ requests) within ${p.minutes} min`,
    ecs_targets: () => `${scope(p.clusters, "All clusters")}: ${p.minUnhealthy}+ unhealthy target${p.minUnhealthy === 1 ? "" : "s"} (warning) · a target group with none healthy (critical)`,
    ecs_tasks: () => `${scope(p.clusters, "All clusters")}: a service runs no task (critical) or fewer than desired (warning)`,
    ecs_instances: () => `${scope(p.clusters, "All clusters")}: an instance loses its ECS agent, or leaves while services are short of tasks`,
    ecs_usage: usage,
  };
  let s = text[r.type]?.() ?? r.type;
  if (Number(p.vsUsual)) s += ` · only when ≥ ${p.vsUsual}× its usual rate`;
  if (r.forMinutes) s += ` · for ${r.forMinutes} min`;
  return s;
}

function levelText(level?: string): string {
  return level === "two" ? "warning or critical" : level === "critical" ? "always critical" : "warning";
}

function LevelBadge({ level, children }: { level?: AlertLevel | null; children?: ReactNode }) {
  return <Badge tone={level === "warning" ? "amber" : "red"}>{children ?? (level === "warning" ? "warning" : "critical")}</Badge>;
}

function RuleState({ r }: { r: AlertRule }) {
  if (!r.enabled) return <Badge>Off</Badge>;
  if (r.status.lastError) return <Badge tone="amber" title={r.status.lastError}>Can't check</Badge>;
  const crit = r.status.firing.filter((s) => s.level !== "warning").length;
  const warn = r.status.firing.length - crit;
  if (r.status.firing.length) {
    return (
      <span className="row" style={{ gap: 4, display: "inline-flex" }}>
        {crit ? <LevelBadge level="critical">Critical · {crit}</LevelBadge> : null}
        {warn ? <LevelBadge level="warning">Warning · {warn}</LevelBadge> : null}
      </span>
    );
  }
  if (r.status.pending.length) return <Badge tone="blue" title="Bad, waiting for the checks in a row or the rule's 'for' time">Pending · {r.status.pending.length}</Badge>;
  if (r.status.muted?.length) return <Badge title={`Muted by the mute file: ${r.status.muted.map((m) => m.subject).join(", ")}`}>OK · {r.status.muted.length} muted</Badge>;
  if (!r.status.lastRunAt) return <Badge>Not checked yet</Badge>;
  return <Badge tone="green">OK</Badge>;
}

function SnoozeControls({ rule, s, minutes }: { rule: AlertRule; s: AlertSubject; minutes: number[] }) {
  const qc = useQueryClient();
  const toast = useToast();
  const zone = storedZone();
  const go = useMutation({
    mutationFn: (m: number | null) => m === -1
      ? request(`/admin/alerts/snooze?${new URLSearchParams({ ruleId: rule.id, subject: s.subject })}`, { method: "DELETE" })
      : request("/admin/alerts/snooze", { method: "POST", body: m === null ? { ruleId: rule.id, subject: s.subject, untilClear: true } : { ruleId: rule.id, subject: s.subject, minutes: m } }),
    onSuccess: (_, m) => { toast(m === -1 ? "Snooze ended" : "Snoozed: nothing is posted about it until then"); qc.invalidateQueries({ queryKey: ["alerts"] }); },
    onError: (e) => toast(String((e as Error).message), "error"),
  });
  if (s.snooze) {
    return (
      <div className="row" style={{ gap: 8, flexWrap: "wrap", fontSize: 13 }}>
        <Badge tone="blue">snoozed</Badge>
        <span className="hint">{s.snooze.until ? `until ${fmtTime(s.snooze.until, zone, false)}` : "until it clears"} · by {s.snooze.by}</span>
        <button type="button" className="btn btn-ghost btn-sm" disabled={go.isPending} onClick={() => go.mutate(-1)}>Unsnooze</button>
      </div>
    );
  }
  return (
    <div className="row" style={{ gap: 4, flexWrap: "wrap", fontSize: 13 }}>
      <span className="hint">Snooze:</span>
      {minutes.map((m) => <button key={m} type="button" className="btn btn-ghost btn-sm" disabled={go.isPending} onClick={() => go.mutate(m)}>{m % 60 ? `${m} min` : `${m / 60} h`}</button>)}
      <button type="button" className="btn btn-ghost btn-sm" disabled={go.isPending} onClick={() => go.mutate(null)}>until it clears</button>
    </div>
  );
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
    <div className="table-scroll no-max">
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
                      <td style={{ maxWidth: 460 }}>{describeRule(r, d.settings)}<div className="hint" style={{ fontSize: 12 }}>{levelText(r.level)} · every {r.every === 1 ? "minute" : `${r.every ?? "?"} min`}{(r.confirm ?? 1) > 1 ? ` · ${r.confirm} checks in a row` : ""}</div></td>
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
                              {s.state === "firing" ? <LevelBadge level={s.level} /> : <Badge tone="blue" title={`Will be ${s.level ?? "an alert"} if it stays bad`}>pending</Badge>}
                              <div style={{ minWidth: 0, flex: 1 }}>
                                <div><strong className="mono">{s.subject}</strong> <span className="hint">since {ago(new Date(s.firedAt ?? s.since).toISOString())}{s.lines && s.level ? ` · ${s.level} line ${num(s.lines[s.level])}${s.lines.unit}` : ""}</span></div>
                                <div style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{s.message}</div>
                                <div className="row" style={{ gap: 12, flexWrap: "wrap", marginTop: 2 }}>
                                  {s.link && <Link to={s.link.replace(/^\/ui/, "")} className="hlink">Open</Link>}
                                  <SnoozeControls rule={r} s={s} minutes={d.snoozeMinutes} />
                                </div>
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

function RuleDialog({ type, rule, lag, settings, onClose }: { type: AlertType; rule?: AlertRule; lag: number; settings: AlertSettings; onClose: () => void }) {
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
  const [error, setError] = useState<unknown>(null);
  const [preview, setPreview] = useState<{ findings: AlertFinding[]; bad: number } | null>(null);
  const body = { type: type.type, name, enabled, params, forMinutes };
  const save = useMutation({
    mutationFn: () => rule
      ? request(`/admin/alerts/rules/${rule.id}`, { method: "PUT", body: { name, enabled, params, forMinutes } })
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
        {["ecs_usage", "app_disk"].includes(type.type) && (
          <Callout tone="neutral" icon="info" title="Warning and critical lines">
            {type.type === "app_disk" ? `Disk: ${pair(settings.thresholds.disk)}.` : `CPU: ${pair(settings.thresholds.cpu)} · memory: ${pair(settings.thresholds.memory)}${Object.keys(settings.thresholds.clusters).length ? `, other lines for ${Object.keys(settings.thresholds.clusters).join(", ")}` : ""}.`} Change them in “Thresholds” on the Alerts page.
          </Callout>
        )}
        <div className="field">
          <label className="label" htmlFor="rfor">Alert only after it has been bad for</label>
          <div className="row" style={{ gap: 8 }}><input id="rfor" className="input" type="number" min={0} max={1440} value={forMinutes} onChange={(e) => setFor(Number(e.target.value))} style={{ width: 110 }} /> <span className="hint">min · 0 = as soon as the checks agree</span></div>
        </div>
        <span className="hint">
          {levelText(type.level).replace(/^./, (c) => c.toUpperCase())}. Checked every {type.every === 1 ? "minute" : `${type.every} minutes`}{confirmOf(type.type, params, type.confirm) > 1 ? `; alerts when ${confirmOf(type.type, params, type.confirm)} checks in a row agree` : ""}.
          {" "}Slack gets a message when it starts, when it changes level, and when it's OK again; reminders: warnings every {settings.repeat.warning || "—"} min, criticals every {settings.repeat.critical || "—"} min (Thresholds card).
          {["lb_errors", "lb_status", "lb_latency"].includes(type.type) ? ` Load balancer data is about ${lag} minutes behind.` : ""}
        </span>
        <span className="hint" style={{ fontStyle: "italic" }}>{describeRule({ type: type.type, params, forMinutes }, settings)}</span>
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
      <span className="label">Right now: {data.bad ? <span className="danger-text">{data.bad} would alert</span> : "nothing would alert"}{data.findings.some((f) => f.mutedBy?.length) ? ` · ${data.findings.filter((f) => f.mutedBy?.length).length} muted` : ""} · {data.findings.length} checked</span>
      <div style={{ border: "1px solid var(--border)", borderRadius: 10, maxHeight: 240, overflow: "auto" }}>
        <table className="table" style={{ fontSize: 13 }}>
          <tbody>
            {[...data.findings].sort((a, b) => Number(b.bad) - Number(a.bad)).map((f) => (
              <tr key={f.subject}>
                <td className="nowrap" style={{ width: 1 }}>{f.bad ? <LevelBadge level={f.level} /> : f.mutedBy?.length ? <Badge title={`Would be ${f.mutedLevel ?? "an alert"}; muted by ${f.mutedBy.join(", ")}`}>muted · {f.mutedBy.join(", ")}</Badge> : <Badge tone="green">ok</Badge>}</td>
                <td className="mono nowrap">{f.subject}</td>
                <td style={{ overflowWrap: "anywhere" }}><div style={{ whiteSpace: "pre-wrap" }}>{f.message}</div><Evidence details={f.details} links={f.links} /></td>
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
                  <td className="nowrap"><EventBadge e={e} /></td>
                  <td>{e.ruleName}</td>
                  <td className="mono nowrap">{e.link ? <Link to={e.link.replace(/^\/ui/, "")} className="hlink">{e.subject}</Link> : e.subject}</td>
                  <td style={{ overflowWrap: "anywhere", maxWidth: 560, fontSize: 13 }}>
                    <div style={{ whiteSpace: "pre-wrap" }}>{e.message}</div>
                    <Evidence details={e.details} links={e.links} />
                  </td>
                  <td className="nowrap">{e.delivered ? <span className="hint">sent</span> : e.event === "muted" ? <span className="hint" title={e.error ?? ""}>muted by {(e.mutedBy ?? []).join(", ")}</span> : e.snoozed ? <span className="hint" title={e.error ?? ""}>snoozed</span> : <span className="warn-text" title={e.error ?? ""}>not sent</span>}{!e.delivered && e.error && e.event !== "muted" ? <Detail text={e.error} /> : null}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function EventBadge({ e }: { e: AlertEvent }) {
  if (e.event === "resolved") return <Badge tone="green">OK again</Badge>;
  if (e.event === "digest") return <Badge tone="blue">weekly summary</Badge>;
  if (e.event === "muted") return <span title={`Would have been ${e.level ?? "an alert"}`}><Badge>muted · {e.level ?? "alert"}</Badge></span>;
  const level = e.level ?? "critical";
  const text = e.event === "escalated" ? `${level} ↑` : e.event === "lowered" ? `${level} ↓` : e.event === "repeat" ? `still ${level}` : level;
  const title = e.was ? `${e.event === "escalated" ? "up" : "down"} from ${e.was}` : undefined;
  return <span title={title}><LevelBadge level={level}>{text}</LevelBadge></span>;
}

/** What the alert found out: codes, paths, usual rate, targets, the service's own errors, links. */
function Evidence({ details, links }: { details?: string[]; links?: [string, string][] }) {
  if (!details?.length && !links?.length) return null;
  return (
    <div className="stack-sm" style={{ marginTop: 6, gap: 3 }}>
      {details?.map((d, i) => <div key={i} className="hint" style={{ fontSize: 12.5, whiteSpace: "pre-wrap" }}>◦ {d}</div>)}
      {links?.length ? (
        <div className="row" style={{ gap: 10, fontSize: 12.5 }}>
          {links.map(([label, to]) => <Link key={label} className="hlink" to={to.replace(/^\/ui/, "")}>{label}</Link>)}
        </div>
      ) : null}
    </div>
  );
}

function Detail({ text }: { text: string }): ReactNode {
  return <div className="hint" style={{ whiteSpace: "normal", maxWidth: 220, fontSize: 12 }}>{text}</div>;
}
