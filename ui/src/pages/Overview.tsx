import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useEffect, useId, useMemo, useRef, useState, type FormEvent } from "react";
import { Link, Navigate, useParams, useSearchParams } from "react-router-dom";
import {
  downloadPost, enc, get, request,
  type Column, type Facet, type FilterOp, type LogFilter, type LogHit, type SavedSearch, type SearchBody, type SearchResult,
} from "../api";
import { Histogram, LEVEL_VAR } from "../components/Histogram";
import { Icon } from "../components/icons";
import { Page } from "../components/Shell";
import { Badge, Callout, Dialog, Empty, ErrorCallout, Loading, Spinner, copyText, useToast } from "../components/ui";
import { bytes, duration, fmtTime, fromInput, localZoneName, num, storedZone, storeZone, toInput, type Zone } from "../format";
import { useClusters, useMe } from "../session";

// ------------------------------------------------------------------ constants

export const PAGE_SIZES = [25, 50, 100, 200];
export const PRESETS: [string, string][] = [
  ["now-15m", "Last 15 minutes"], ["now-1h", "Last hour"], ["now-4h", "Last 4 hours"], ["now-12h", "Last 12 hours"],
  ["now-24h", "Last 24 hours"], ["now-3d", "Last 3 days"], ["now-7d", "Last 7 days"],
];
export const DEFAULT_RANGE = "now-1h";
export const DEFAULT_COLS = ["log_time", "level", "service", "_message", "host"];
export const MESSAGE = "_message";

const OPS: { op: FilterOp; label: string }[] = [
  { op: "is", label: "is" }, { op: "is_not", label: "is not" },
  { op: "one_of", label: "is one of" }, { op: "not_one_of", label: "is not one of" },
  { op: "contains", label: "contains" }, { op: "not_contains", label: "does not contain" },
  { op: "gte", label: "≥ (at least)" }, { op: "lte", label: "≤ (at most)" }, { op: "between", label: "is between" },
  { op: "exists", label: "has a value" }, { op: "not_exists", label: "is empty" },
];
const OP_LABEL = Object.fromEntries(OPS.map((o) => [o.op, o.label])) as Record<FilterOp, string>;
export const NEGATIVE: FilterOp[] = ["is_not", "not_one_of", "not_contains", "not_exists"];
const FACET_TITLE: Record<string, string> = { level: "Level", service: "Service", host: "Host", exception: "Exception" };

// ------------------------------------------------------------------ helpers

export function readJson<T>(s: string | null, fallback: T): T {
  if (!s) return fallback;
  try {
    return JSON.parse(s) as T;
  } catch {
    return fallback;
  }
}

export function filterText(f: LogFilter): string {
  switch (f.op) {
    case "exists": return `${f.field} has a value`;
    case "not_exists": return `${f.field} is empty`;
    case "between": return `${f.field}: ${f.gte ?? "…"} to ${f.lte ?? "…"}`;
    case "one_of": case "not_one_of": return `${f.field} ${OP_LABEL[f.op]} ${(f.values ?? []).join(", ")}`;
    default: return `${f.field} ${OP_LABEL[f.op]} ${f.value ?? ""}`;
  }
}

export function cellText(v: unknown): string {
  if (v === undefined || v === null) return "";
  if (typeof v === "string") return v;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  return JSON.stringify(v);
}

/** The one-line summary shown in the Message column. */
export function messageOf(h: Record<string, unknown>): string {
  const pick = (k: string) => (typeof h[k] === "string" && (h[k] as string).trim() ? (h[k] as string) : null);
  if (!pick("msg") && (pick("parent_log") || pick("kv_json"))) {
    // "delest" lines carry key:value pairs instead of a message
    let kv = "";
    try {
      const o = JSON.parse(pick("kv_json") ?? "{}") as Record<string, unknown>;
      kv = Object.entries(o).filter(([k]) => k !== "internalRequestUUID").slice(0, 6).map(([k, v]) => `${k}=${String(v)}`).join("  ");
    } catch {
      kv = pick("kv_json") ?? "";
    }
    return [[pick("parent_log"), pick("child_log")].filter(Boolean).join(" / "), kv].filter(Boolean).join("  ·  ");
  }
  const first = pick("msg") ?? pick("error_message") ?? pick("exception") ?? pick("raw") ?? pick("body") ?? "";
  const line = first.split("\n")[0];
  const exc = pick("exception");
  return exc && first !== exc && !line.includes(exc) ? `${line}  ·  ${exc}` : line;
}

export function LevelBadge({ level }: { level: unknown }) {
  if (typeof level !== "string" || !level) return <span className="faint">—</span>;
  const u = level.toUpperCase();
  const key = u === "WARNING" ? "WARN" : u;
  return <span className={`lv lv-${key.toLowerCase()}`}><span className="swatch" style={{ background: LEVEL_VAR[key] ?? LEVEL_VAR.OTHER }} />{u}</span>;
}

export function isRelative(v: string) {
  return /^now(-\d+[smhdw])?$/.test(v);
}

export function rangeLabel(start: string, end: string, zone: Zone): string {
  const preset = PRESETS.find(([v]) => v === start && end === "now");
  if (preset) return preset[1];
  const f = (v: string) => (isRelative(v) ? v : fmtTime(Date.parse(v), zone, false));
  return `${f(start)} → ${f(end)}`;
}

// ------------------------------------------------------------------ page

export function Overview() {
  const { clusterId = "" } = useParams();
  const me = useMe().data!;
  const clusters = useClusters();
  const [params, setParams] = useSearchParams();
  const toast = useToast();
  const base = `/clusters/${enc(clusterId)}/logs`;
  const maxHours = me.limits.maxSearchHours;

  // Everything that defines the search lives in the URL, so a link reproduces it.
  const start = params.get("start") || DEFAULT_RANGE;
  const end = params.get("end") || "now";
  const q = params.get("q") ?? "";
  const filters = useMemo(() => readJson<LogFilter[]>(params.get("f"), []), [params]);
  const order = params.get("order") === "asc" ? "asc" : "desc";
  const size = PAGE_SIZES.includes(Number(params.get("size"))) ? Number(params.get("size")) : 50;
  const offset = Math.max(0, Number(params.get("off")) || 0);
  const cols = useMemo(() => (params.get("cols") ? params.get("cols")!.split(",").filter(Boolean) : DEFAULT_COLS), [params]);

  const set = (patch: Record<string, string | null>, resetPage = true) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(patch)) {
      if (v === null || v === "") next.delete(k);
      else next.set(k, v);
    }
    if (resetPage && !("off" in patch)) next.delete("off");
    setParams(next);
  };

  const [zone, setZoneState] = useState<Zone>(storedZone());
  const setZone = (z: Zone) => { storeZone(z); setZoneState(z); };
  const [qDraft, setQDraft] = useState(q);
  useEffect(() => setQDraft(q), [q]);
  const [filterEdit, setFilterEdit] = useState<{ i: number | null; f: LogFilter } | null>(null);
  const [colsOpen, setColsOpen] = useState(false);
  const [exportOpen, setExportOpen] = useState(false);
  const [saveOpen, setSaveOpen] = useState(false);
  const [savedMenu, setSavedMenu] = useState(false);
  const [detailAt, setDetailAt] = useState<number | null>(null);
  const [nonce, setNonce] = useState(0);

  const body: SearchBody = useMemo(() => ({ start, end, query: q, filters, order }), [start, end, q, filters, order]);
  // Page 1 decides the time window; later pages reuse it so "last hour" doesn't slide while you page.
  const [anchor, setAnchor] = useState<{ key: string; start: number; end: number } | null>(null);
  const bodyKey = JSON.stringify([clusterId, body, nonce]);
  const pageBody = offset > 0 && anchor?.key === bodyKey ? { ...body, start: String(anchor.start), end: String(anchor.end) } : body;

  const search = useQuery({
    queryKey: ["logs", clusterId, pageBody, offset, size, offset === 0 ? nonce : 0],
    queryFn: ({ signal }) => request<SearchResult>(`${base}/_search`, { method: "POST", body: { ...pageBody, offset, size, aggregations: offset === 0 }, signal }),
    enabled: !!clusterId,
    placeholderData: keepPreviousData,
    retry: false,
    staleTime: 60_000,
  });
  const res = search.data;
  // keep the first page's histogram and top values while paging
  const [aggs, setAggs] = useState<SearchResult | null>(null);
  useEffect(() => {
    if (res && res.histogram && !search.isPlaceholderData) {
      setAggs(res);
      setAnchor({ key: bodyKey, start: res.start, end: res.end });
    }
  }, [res, search.isPlaceholderData, bodyKey]);
  const shown = aggs && aggs.cluster === clusterId ? aggs : null;
  const columns: Column[] = res?.columns ?? shown?.columns ?? [];
  const hits = res?.hits ?? [];
  const total = res?.total ?? 0;
  const describe = [rangeLabel(start, end, zone), q && `“${q}”`, ...filters.map(filterText)].filter(Boolean).join(" · ");

  const draft = useRef<[string, string] | null>(null);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const range = draft.current;      // custom times typed but not applied: Search applies them too
    if (qDraft.trim() === q && !range) setNonce((n) => n + 1);
    else set({ q: qDraft.trim(), ...(range ? { start: range[0], end: range[1] } : {}) });
  };
  const setFilters = (next: LogFilter[]) => set({ f: next.length ? JSON.stringify(next) : null });
  const addFilter = (f: LogFilter) => {
    const opposite = filters.findIndex((x) => x.field === f.field && x.value === f.value && (x.op === "is") !== (f.op === "is"));
    setFilters(opposite >= 0 ? filters.map((x, i) => (i === opposite ? f : x)) : [...filters, f]);
  };
  const setRange = (s: string, e: string) => set({ start: s === DEFAULT_RANGE && e === "now" ? null : s, end: e === "now" ? null : e });

  const copyLink = () => copyText(window.location.href).then(() => toast("Link copied. Anyone with access to this cluster can open it."));

  if (clusters.data && !clusters.data.some((c) => c.id === clusterId)) {
    return clusters.data.length ? <Navigate to={`/overview/${enc(clusters.data[0].id)}`} replace /> : <Navigate to="/" replace />;
  }

  const lastOff = Math.max(0, Math.floor((total - 1) / size) * size);
  return (
    <Page wide crumbs={[{ label: "Overview", to: "/" }, { label: clusterId }]} title={`Overview · ${clusterId}`}
      actions={<>
        <Link className="btn btn-sm" to={`/logs/${enc(clusterId)}?${(() => { const p = new URLSearchParams(params); p.delete("off"); p.delete("size"); return p.toString(); })()}`}><Icon name="terminal" size={15} /> Open in Logs</Link>
        <ZoneToggle zone={zone} onChange={setZone} />
      </>}>
      <section className="card">
        <form className="data-bar" onSubmit={submit}>
          <div className="field grow" style={{ minWidth: 280 }}>
            <label className="label" htmlFor="lg-q">Search <span className="hint">· all words must appear · <code>"exact phrase"</code> · <code>-word</code> to exclude · <code>column:value</code></span></label>
            <input id="lg-q" className="input mono" value={qDraft} placeholder='e.g. "Tracking Number not found" carrier:UPS -DEBUG' onChange={(e) => setQDraft(e.target.value)} spellCheck={false} autoComplete="off" />
          </div>
          <TimePicker start={start} end={end} zone={zone} maxHours={maxHours} onChange={setRange} draft={draft} />
          <div className="field" style={{ justifyContent: "flex-end" }}>
            <button type="submit" className="btn btn-primary"><Icon name="search" /> Search</button>
          </div>
        </form>
        <div className="data-bar" style={{ paddingTop: 0, alignItems: "center" }}>
          <div className="row filter-row grow">
            <Icon name="filter" size={16} style={{ color: "var(--faint)" }} />
            {filters.map((f, i) => (
              <span key={i} className={`chip ${NEGATIVE.includes(f.op) ? "deny" : ""}`}>
                <button type="button" className="chip-text" onClick={() => setFilterEdit({ i, f })} title="Edit filter">{filterText(f)}</button>
                <button type="button" aria-label={`Remove filter ${filterText(f)}`} onClick={() => setFilters(filters.filter((_, j) => j !== i))}><Icon name="x" size={14} /></button>
              </span>
            ))}
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => setFilterEdit({ i: null, f: { field: "level", op: "is", value: "" } })}>
              <Icon name="plus" size={15} /> Add filter
            </button>
            {filters.length > 1 && <button type="button" className="btn btn-ghost btn-sm" onClick={() => setFilters([])}>Clear filters</button>}
          </div>
          <div className="row" style={{ gap: 6 }}>
            <SavedMenu open={savedMenu} setOpen={setSavedMenu} clusterId={clusterId} />
            <button type="button" className="btn btn-sm" onClick={() => setSaveOpen(true)}><Icon name="bookmark" size={15} /> Save search</button>
            <button type="button" className="btn btn-sm" onClick={copyLink}><Icon name="link" size={15} /> Copy link</button>
          </div>
        </div>
      </section>

      {search.error && !res ? (
        <section className="card"><div className="card-body"><ErrorCallout error={search.error} admin={me.admin} /></div></section>
      ) : (
        <>
          <section className="card histo-card">
            <div className="data-toolbar">
              <div className="row" style={{ gap: 10, minWidth: 0 }}>
                <strong style={{ fontSize: 15 }}>{shown ? `${num(shown.total)} log line${shown.total === 1 ? "" : "s"}` : "Searching…"}</strong>
                {shown && <span className="hint">{rangeLabel(start, end, zone)} · {fmtTime(shown.start, zone, false)} → {fmtTime(shown.end, zone, false)}{zone === "utc" ? " UTC" : ` ${localZoneName()}`}</span>}
                {search.isFetching && <Spinner label="Searching" />}
              </div>
              <div className="grow" />
              {shown && (
                <span className="hint" title="Files are downloaded from S3 once and kept in the server's cache">
                  {num(shown.files)} file{shown.files === 1 ? "" : "s"} ({bytes(shown.bytes)}) · {num(shown.cached)} from cache · {duration(shown.tookMs)}
                </span>
              )}
            </div>
            {search.error ? (
              <div className="card-body"><ErrorCallout error={search.error} admin={me.admin} /></div>
            ) : shown?.histogram ? (
              shown.total ? (
                <Histogram buckets={shown.histogram.buckets} interval={shown.histogram.interval} start={shown.start} end={shown.end} zone={zone}
                  onZoom={(a, b) => setRange(new Date(a).toISOString(), new Date(b).toISOString())} />
              ) : <Empty title="No log lines match">Try a longer time range, fewer words or remove a filter.{shown.files === 0 && " There are no log files for this cluster in this time range."}</Empty>
            ) : <Loading what="Searching… the first search of a time range downloads its files from S3; later searches are faster." />}
          </section>

          {shown && shown.total > 0 && (
            <div className="logs-grid">
              <aside className="card facets" aria-label="Top values">
                {Object.entries(shown.facets ?? {}).map(([field, items]) => (
                  <FacetList key={field} field={field} items={items} total={shown.total} filters={filters} onFilter={addFilter} />
                ))}
              </aside>
              <section className="card" style={{ overflow: "hidden", minWidth: 0 }}>
                <div className="data-toolbar">
                  <div className="row" style={{ gap: 8 }}>
                    <button type="button" className="btn btn-ghost btn-sm" onClick={() => set({ order: order === "desc" ? "asc" : null })} title="Change the order">
                      <Icon name="caret" size={14} style={{ transform: order === "asc" ? "rotate(180deg)" : undefined }} /> {order === "desc" ? "Newest first" : "Oldest first"}
                    </button>
                  </div>
                  <div className="grow" />
                  <button type="button" className="btn btn-sm" onClick={() => setColsOpen(true)} disabled={!columns.length}><Icon name="columns" size={15} /> Columns · {cols.length}</button>
                  <button type="button" className="btn btn-sm" onClick={() => setExportOpen(true)}><Icon name="download" size={15} /> Export</button>
                </div>
                {hits.length === 0 ? <Empty title="No more log lines on this page" /> : (
                  <div className="table-scroll data-scroll">
                    <table className="table compact data-table logs-table">
                      <thead>
                        <tr>{cols.map((c) => <th key={c}>{c === MESSAGE ? "message" : c === "log_time" ? `time (${zone === "utc" ? "UTC" : localZoneName()})` : c}</th>)}</tr>
                      </thead>
                      <tbody>
                        {hits.map((h, i) => (
                          <tr key={`${h._ref.key}#${h._ref.row}`} className={`clickable ${String(h.level ?? "").toUpperCase() === "ERROR" ? "row-error" : ""}`}
                            onClick={() => setDetailAt(i)} tabIndex={0} onKeyDown={(e) => { if (e.key === "Enter") setDetailAt(i); }}>
                            {cols.map((c) => <Cell key={c} col={c} hit={h} zone={zone} />)}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
                <div className="data-toolbar" style={{ borderTop: "1px solid var(--row-border)", borderBottom: 0 }}>
                  <div className="grow" />
                  <label className="row hint" style={{ gap: 6 }}>Rows per page
                    <select className="select" style={{ width: 78, minHeight: 32 }} value={size} onChange={(e) => set({ size: e.target.value === "50" ? null : e.target.value })}>
                      {PAGE_SIZES.map((n) => <option key={n} value={n}>{n}</option>)}
                    </select>
                  </label>
                  <span className="hint nowrap">{num(Math.min(offset + 1, total))}–{num(Math.min(offset + size, total))} of {num(total)}</span>
                  <div className="row" style={{ gap: 2 }}>
                    <button type="button" className="btn btn-ghost icon-btn" aria-label="First page" disabled={offset === 0} onClick={() => set({ off: null }, false)}><Icon name="first" /></button>
                    <button type="button" className="btn btn-ghost icon-btn" aria-label="Previous page" disabled={offset === 0} onClick={() => set({ off: offset - size > 0 ? String(offset - size) : null }, false)}><Icon name="chevronLeft" /></button>
                    <button type="button" className="btn btn-ghost icon-btn" aria-label="Next page" disabled={offset + size >= total} onClick={() => set({ off: String(offset + size) }, false)}><Icon name="chevron" /></button>
                    <button type="button" className="btn btn-ghost icon-btn" aria-label="Last page" disabled={offset >= lastOff} onClick={() => set({ off: String(lastOff) }, false)}><Icon name="last" /></button>
                  </div>
                </div>
              </section>
            </div>
          )}
        </>
      )}

      {filterEdit && (
        <FilterDialog columns={columns} facets={shown?.facets ?? {}} initial={filterEdit.f} editing={filterEdit.i !== null}
          onClose={() => setFilterEdit(null)}
          onSave={(f) => {
            if (filterEdit.i === null) setFilters([...filters, f]);
            else setFilters(filters.map((x, j) => (j === filterEdit.i ? f : x)));
            setFilterEdit(null);
          }} />
      )}
      {colsOpen && (
        <ColumnsDialog columns={columns} selected={cols} onClose={() => setColsOpen(false)}
          onSave={(c) => { set({ cols: c.join(",") === DEFAULT_COLS.join(",") ? null : c.join(",") }, false); setColsOpen(false); }} />
      )}
      {exportOpen && (
        <ExportDialog total={total} shownColumns={cols.filter((c) => c !== MESSAGE)} max={me.limits.maxExportRows} describe={describe} onClose={() => setExportOpen(false)}
          run={async (format, columns, limit) => {
            const anchored = anchor?.key === bodyKey ? { ...body, start: String(anchor.start), end: String(anchor.end) } : body;
            const { blob, filename, rows } = await downloadPost(`${base}/_export`, { ...anchored, format, columns: columns.length ? columns : null, limit });
            const url = URL.createObjectURL(blob);
            const a = document.createElement("a");
            a.href = url;
            a.download = filename;
            document.body.appendChild(a);
            a.click();
            a.remove();
            setTimeout(() => URL.revokeObjectURL(url), 1000);
            toast(`Downloaded ${num(rows)} log lines as ${filename}`);
            setExportOpen(false);
          }} />
      )}
      {saveOpen && <SaveDialog clusterId={clusterId} params={params.toString()} describe={describe} onClose={() => setSaveOpen(false)} />}
      {detailAt !== null && hits[detailAt] && (
        <DetailDialog base={base} hit={hits[detailAt]} zone={zone} position={`${num(offset + detailAt + 1)} of ${num(total)}`}
          onPrev={detailAt > 0 ? () => setDetailAt(detailAt - 1) : undefined}
          onNext={detailAt < hits.length - 1 ? () => setDetailAt(detailAt + 1) : undefined}
          onClose={() => setDetailAt(null)}
          onFilter={(f) => { addFilter(f); setDetailAt(null); }} />
      )}
    </Page>
  );
}

export function Cell({ col, hit, zone }: { col: string; hit: LogHit; zone: Zone }) {
  if (col === "log_time") return <td className="cell-mono nowrap">{fmtTime(hit.log_time_ms as number, zone)}</td>;
  if (col === "level") return <td className="nowrap"><LevelBadge level={hit.level} /></td>;
  const t = col === MESSAGE ? messageOf(hit) : cellText(hit[col]);
  return <td className={`cell-mono data-cell ${col === MESSAGE ? "msg-cell" : ""}`} title={t.length > 60 ? t.slice(0, 1500) : undefined}>{t || <span className="faint">—</span>}</td>;
}

// ------------------------------------------------------------------ time

export function ZoneToggle({ zone, onChange }: { zone: Zone; onChange: (z: Zone) => void }) {
  return (
    <div className="tabs" role="group" aria-label="Time zone" style={{ marginLeft: "auto" }}>
      <button type="button" className="tab" aria-pressed={zone === "local"} aria-selected={zone === "local"} onClick={() => onChange("local")} title="Your browser's time zone">{localZoneName()}</button>
      <button type="button" className="tab" aria-pressed={zone === "utc"} aria-selected={zone === "utc"} onClick={() => onChange("utc")} title="Times as written in the log files">UTC</button>
    </div>
  );
}

/** Times typed in the custom range but not applied yet; the page's Search button applies them too. */
export type RangeDraft = { current: [string, string] | null };

export function TimePicker({ start, end, zone, maxHours, onChange, compact, draft }: {
  start: string; end: string; zone: Zone; maxHours: number; onChange: (s: string, e: string) => void; compact?: boolean;
  draft?: RangeDraft;
}) {
  const preset = end === "now" && PRESETS.some(([v]) => v === start) ? start : "custom";
  const [custom, setCustom] = useState(preset === "custom");
  const now = Date.now();
  const toMs = (v: string) => (isRelative(v) ? now - relMs(v) : Date.parse(v));
  const [from, setFrom] = useState(toInput(toMs(start), zone));
  const [to, setTo] = useState(toInput(toMs(end), zone));
  const [edited, setEdited] = useState(false);
  useEffect(() => {
    setEdited(false);
    setFrom(toInput(toMs(start), zone));
    setTo(toInput(toMs(end), zone));
    setCustom(!(end === "now" && PRESETS.some(([v]) => v === start)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [start, end, zone]);
  const a = fromInput(from, zone);
  const b = fromInput(to, zone);
  const err = a === null || b === null ? "Pick both times" : b <= a ? "“To” must be after “From”"
    : b - a > maxHours * 3600_000 ? `At most ${maxHours / 24} days per search` : a < now - 31 * 86400_000 ? "S3 keeps 30 days of logs" : null;
  const pending = custom && edited && !err;
  const blocking = !!err && !err.startsWith("S3");
  if (draft) draft.current = pending ? [new Date(a!).toISOString(), new Date(b!).toISOString()] : null;
  return (
    <div className="field" style={compact ? { gap: 2 } : undefined}>
      <label className={compact ? "sr-only" : "label"} htmlFor="lg-range">Time range</label>
      <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
        <select id="lg-range" className="select" style={{ width: "auto" }} value={custom ? "custom" : preset}
          onChange={(e) => {
            const v = e.target.value;
            if (v === "custom") setCustom(true);
            else { setCustom(false); onChange(v, "now"); }
          }}>
          {PRESETS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
          <option value="custom">Custom range…</option>
        </select>
        {custom && (
          <>
            <input type="datetime-local" className="input" aria-label="From" style={{ width: "auto" }} value={from} onChange={(e) => { setFrom(e.target.value); setEdited(true); }} />
            <span className="hint">to</span>
            <input type="datetime-local" className="input" aria-label="To" style={{ width: "auto" }} value={to} onChange={(e) => { setTo(e.target.value); setEdited(true); }} />
            <button type="button" className={`btn ${pending ? "btn-primary" : ""}`} disabled={blocking} title={err ?? undefined}
              onClick={() => onChange(new Date(a!).toISOString(), new Date(b!).toISOString())}>Apply</button>
          </>
        )}
      </div>
      {custom && err && <span className="hint" style={{ color: err.startsWith("S3") ? undefined : "var(--danger)" }}>{err}</span>}
      {pending && <span className="hint" style={{ color: "var(--warn-text)" }}>These times are not applied yet: press Apply or Search.</span>}
    </div>
  );
}

function relMs(v: string): number {
  const m = /^now-(\d+)([smhdw])$/.exec(v);
  if (!m) return 0;
  return Number(m[1]) * { s: 1, m: 60, h: 3600, d: 86400, w: 604800 }[m[2] as "s"] * 1000;
}

// ------------------------------------------------------------------ facets

export function FacetList({ field, items, total, filters, onFilter }: {
  field: string; items: Facet[]; total: number; filters: LogFilter[]; onFilter: (f: LogFilter) => void;
}) {
  if (!items.length) return null;
  const top = items[0].count;
  return (
    <div className="facet">
      <div className="facet-head">{FACET_TITLE[field] ?? field}</div>
      <ul>
        {items.map((it) => {
          const active = filters.some((f) => f.field === field && f.op === "is" && f.value === it.value);
          return (
            <li key={it.value} className={active ? "active" : ""}>
              <button type="button" className="facet-val" title={`Only ${field} = ${it.value}`} onClick={() => onFilter({ field, op: "is", value: it.value })}>
                <span className="facet-label">{field === "level" ? <LevelBadge level={it.value} /> : <span className="mono">{field === "exception" ? it.value.split(".").pop() : field === "host" ? it.value.split(".")[0] : it.value}</span>}</span>
                <span className="facet-count">{num(it.count)}</span>
                <span className="facet-bar" style={{ width: `${Math.max(2, (it.count / top) * 100)}%` }} aria-hidden="true" />
              </button>
              <button type="button" className="btn btn-ghost mini-btn" title={`Hide ${field} = ${it.value}`} aria-label={`Exclude ${field} ${it.value}`}
                onClick={() => onFilter({ field, op: "is_not", value: it.value })}><Icon name="minus" size={14} /></button>
            </li>
          );
        })}
      </ul>
      {items.length >= 8 && <span className="hint" style={{ padding: "0 12px" }}>Top 8 of {num(total)} lines</span>}
    </div>
  );
}

// ------------------------------------------------------------------ saved searches

export function SavedMenu({ open, setOpen, clusterId }: { open: boolean; setOpen: (o: boolean) => void; clusterId: string }) {
  const saved = useQuery({ queryKey: ["saved"], queryFn: () => get<{ items: SavedSearch[] }>("/saved-searches").then((r) => r.items), staleTime: 60_000 });
  const items = saved.data ?? [];
  return (
    <div className="menu-wrap">
      <button type="button" className="btn btn-sm" aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen(!open)}>
        Saved <Icon name="caret" size={14} />
      </button>
      {open && (
        <div className="menu" role="menu" onMouseLeave={() => setOpen(false)} style={{ minWidth: 280 }}>
          {items.length === 0 && <span className="hint" style={{ padding: "8px 10px" }}>No saved searches yet. Use “Save search” to keep this one.</span>}
          {items.map((s) => (
            <Link key={s.id} role="menuitem" className="menu-link" to={`/logs/${enc(s.cluster)}?${s.params}`} onClick={() => setOpen(false)}>
              <span className="grow" style={{ overflow: "hidden", textOverflow: "ellipsis" }}>{s.name}</span>
              {s.cluster !== clusterId && <span className="hint mono">{s.cluster}</span>}
            </Link>
          ))}
          <Link role="menuitem" className="menu-link" to="/saved" onClick={() => setOpen(false)} style={{ borderTop: items.length ? "1px solid var(--row-border)" : undefined }}>Manage saved searches…</Link>
        </div>
      )}
    </div>
  );
}

export function SaveDialog({ clusterId, params, describe, onClose }: { clusterId: string; params: string; describe: string; onClose: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [name, setName] = useState("");
  const clean = new URLSearchParams(params);
  clean.delete("off");
  const save = useMutation({
    mutationFn: () => request<SavedSearch>("/saved-searches", { method: "POST", body: { name: name.trim(), cluster: clusterId, params: clean.toString() } }),
    onSuccess: (s) => { qc.invalidateQueries({ queryKey: ["saved"] }); toast(`Saved “${s.name}”`); onClose(); },
  });
  return (
    <Dialog title="Save this search" subtitle="Only you see your saved searches. To share a search, use Copy link." onClose={onClose} busy={save.isPending}
      footer={<>
        <button type="button" className="btn" onClick={onClose}>Cancel</button>
        <button type="button" className="btn btn-primary" disabled={!name.trim() || save.isPending} onClick={() => save.mutate()}>Save</button>
      </>}>
      <form className="stack" onSubmit={(e) => { e.preventDefault(); if (name.trim()) save.mutate(); }}>
        <div className="field">
          <label className="label" htmlFor="sv-name">Name</label>
          <input id="sv-name" className="input" maxLength={120} value={name} autoFocus placeholder="e.g. UPS auth errors" onChange={(e) => setName(e.target.value)} />
          <span className="hint">Saving under an existing name replaces it.</span>
        </div>
        <div className="stack-sm">
          <span className="label">What is saved</span>
          <span className="hint"><span className="mono">{clusterId}</span> · {describe}. A relative range like “Last hour” stays relative.</span>
        </div>
        {save.error && <ErrorCallout error={save.error} />}
      </form>
    </Dialog>
  );
}

// ------------------------------------------------------------------ filter dialog

export function FilterDialog({ columns, facets, initial, editing, onSave, onClose }: {
  columns: Column[];
  facets: Record<string, Facet[]>;
  initial: LogFilter;
  editing: boolean;
  onSave: (f: LogFilter) => void;
  onClose: () => void;
}) {
  const [f, setF] = useState<LogFilter>(initial);
  const [valuesText, setValuesText] = useState((initial.values ?? []).join("\n"));
  const listId = useId();
  const valId = useId();
  const meta = columns.find((c) => c.name === f.field);
  const numeric = !!meta && /INT|DOUBLE|FLOAT|DECIMAL/.test(meta.type);
  const needsValue = ["is", "is_not", "contains", "not_contains", "gte", "lte"].includes(f.op);
  const needsValues = f.op === "one_of" || f.op === "not_one_of";
  const values = valuesText.split(/[\n,]/).map((v) => v.trim()).filter(Boolean);
  const valid = !!f.field.trim() && (!needsValue || !!(f.value ?? "").trim()) && (!needsValues || values.length > 0) &&
    (f.op !== "between" || !!(f.gte || f.lte));
  const save = () => {
    const out: LogFilter = { field: f.field.trim(), op: f.op };
    if (needsValue) out.value = (f.value ?? "").trim();
    if (needsValues) out.values = values;
    if (f.op === "between") {
      if (f.gte) out.gte = f.gte.trim();
      if (f.lte) out.lte = f.lte.trim();
    }
    onSave(out);
  };
  const suggestions = facets[f.field] ?? [];
  return (
    <Dialog title={editing ? "Edit filter" : "Add filter"} onClose={onClose}
      footer={<>
        <button type="button" className="btn" onClick={onClose}>Cancel</button>
        <button type="button" className="btn btn-primary" disabled={!valid} onClick={save}>{editing ? "Save filter" : "Add filter"}</button>
      </>}>
      <form className="stack" onSubmit={(e) => { e.preventDefault(); if (valid) save(); }}>
        <div className="field">
          <label className="label" htmlFor="flt-field">Column</label>
          <input id="flt-field" className="input mono" list={listId} value={f.field} autoComplete="off" spellCheck={false}
            placeholder="e.g. error_code" onChange={(e) => setF({ ...f, field: e.target.value })} />
          <datalist id={listId}>{columns.map((c) => <option key={c.name} value={c.name}>{c.type}</option>)}</datalist>
          {meta && <span className="hint">Type: {meta.type.toLowerCase()}{numeric ? " (number)" : ""}</span>}
        </div>
        <div className="field">
          <label className="label" htmlFor="flt-op">Operator</label>
          <select id="flt-op" className="select" value={f.op} onChange={(e) => setF({ ...f, op: e.target.value as FilterOp })}>
            {OPS.map((o) => <option key={o.op} value={o.op}>{o.label}</option>)}
          </select>
        </div>
        {needsValue && (
          <div className="field">
            <label className="label" htmlFor="flt-value">Value</label>
            <input id="flt-value" className="input mono" list={valId} value={f.value ?? ""} onChange={(e) => setF({ ...f, value: e.target.value })} autoComplete="off" />
            <datalist id={valId}>{suggestions.map((s) => <option key={s.value} value={s.value} />)}</datalist>
            <span className="hint">{f.op === "contains" || f.op === "not_contains" ? "Case-insensitive, anywhere in the value." : f.op === "is" || f.op === "is_not" ? "Exact value (case-sensitive)." : "A number."}</span>
          </div>
        )}
        {needsValues && (
          <div className="field">
            <label className="label" htmlFor="flt-values">Values <span className="hint">· one per line or comma separated</span></label>
            <textarea id="flt-values" className="textarea mono" rows={4} value={valuesText} onChange={(e) => setValuesText(e.target.value)} />
          </div>
        )}
        {f.op === "between" && (
          <div className="grid-2" style={{ gap: 12 }}>
            <div className="field">
              <label className="label" htmlFor="flt-gte">From <span className="hint">· inclusive</span></label>
              <input id="flt-gte" className="input mono" value={f.gte ?? ""} onChange={(e) => setF({ ...f, gte: e.target.value })} />
            </div>
            <div className="field">
              <label className="label" htmlFor="flt-lte">To <span className="hint">· inclusive</span></label>
              <input id="flt-lte" className="input mono" value={f.lte ?? ""} onChange={(e) => setF({ ...f, lte: e.target.value })} />
            </div>
          </div>
        )}
        <button type="submit" hidden />
      </form>
    </Dialog>
  );
}

// ------------------------------------------------------------------ columns dialog

export function ColumnsDialog({ columns, selected, onSave, onClose }: {
  columns: Column[]; selected: string[]; onSave: (c: string[]) => void; onClose: () => void;
}) {
  const [sel, setSel] = useState<string[]>(selected);
  const [filter, setFilter] = useState("");
  const all: Column[] = [{ name: MESSAGE, type: "summary: msg, else error message, exception or first line" }, ...columns];
  const shown = all.filter((c) => !filter || c.name.toLowerCase().includes(filter.toLowerCase()));
  const toggle = (n: string) => setSel((s) => (s.includes(n) ? s.filter((x) => x !== n) : [...s, n]));
  const move = (i: number, d: -1 | 1) => setSel((s) => {
    const j = i + d;
    if (j < 0 || j >= s.length) return s;
    const c = [...s];
    [c[i], c[j]] = [c[j], c[i]];
    return c;
  });
  return (
    <Dialog wide title="Columns" subtitle="Pick the columns to show in the table. The detail view always shows every column." onClose={onClose}
      footer={<>
        <button type="button" className="btn btn-ghost" onClick={() => setSel(DEFAULT_COLS)}>Reset to default</button>
        <div className="grow" />
        <button type="button" className="btn" onClick={onClose}>Cancel</button>
        <button type="button" className="btn btn-primary" disabled={!sel.length} onClick={() => onSave(sel)}>Show {sel.length} column{sel.length === 1 ? "" : "s"}</button>
      </>}>
      <div className="grid-2" style={{ gap: 16, alignItems: "start" }}>
        <div className="stack-sm">
          <span className="label">All columns · {all.length}</span>
          <input className="input" aria-label="Filter columns" placeholder="Filter columns" value={filter} onChange={(e) => setFilter(e.target.value)} />
          <div className="cols-list">
            {shown.map((c) => (
              <label key={c.name} className="check cols-item">
                <input type="checkbox" checked={sel.includes(c.name)} onChange={() => toggle(c.name)} />
                <span className="mono grow" style={{ fontSize: 13 }}>{c.name === MESSAGE ? "message" : c.name}</span>
                <span className="hint">{c.type.toLowerCase()}</span>
              </label>
            ))}
          </div>
        </div>
        <div className="stack-sm">
          <span className="label">Shown, in order · {sel.length}</span>
          <ol className="cols-order">
            {sel.map((n, i) => (
              <li key={n}>
                <span className="mono grow" style={{ fontSize: 13, overflowWrap: "anywhere" }}>{n === MESSAGE ? "message" : n}</span>
                <button type="button" className="btn btn-ghost icon-btn" aria-label={`Move ${n} up`} disabled={i === 0} onClick={() => move(i, -1)}><Icon name="caret" size={14} style={{ transform: "rotate(180deg)" }} /></button>
                <button type="button" className="btn btn-ghost icon-btn" aria-label={`Move ${n} down`} disabled={i === sel.length - 1} onClick={() => move(i, 1)}><Icon name="caret" size={14} /></button>
                <button type="button" className="btn btn-ghost icon-btn" aria-label={`Hide ${n}`} onClick={() => toggle(n)}><Icon name="x" size={14} /></button>
              </li>
            ))}
          </ol>
        </div>
      </div>
    </Dialog>
  );
}

// ------------------------------------------------------------------ export dialog

export function ExportDialog({ total, shownColumns, max, describe, run, onClose }: {
  total: number;
  shownColumns: string[];
  max: number;
  describe: string;
  run: (format: "csv" | "json" | "ndjson", columns: string[], limit: number) => Promise<void>;
  onClose: () => void;
}) {
  const [format, setFormat] = useState<"csv" | "json" | "ndjson">("csv");
  const [which, setWhich] = useState<"shown" | "all">("all");
  const cap = Math.max(1, Math.min(total, max));
  const [limit, setLimit] = useState(cap);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const go = async () => {
    setBusy(true);
    setError(null);
    try {
      await run(format, which === "shown" ? shownColumns : [], Math.max(1, Math.min(limit, cap)));
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog title="Export log lines" subtitle={describe} onClose={onClose} busy={busy}
      footer={<>
        <button type="button" className="btn" onClick={onClose} disabled={busy}>Cancel</button>
        <button type="button" className="btn btn-primary" onClick={go} disabled={busy}>{busy ? <Spinner label="Exporting" /> : <Icon name="download" />} Download</button>
      </>}>
      <div className="stack">
        <fieldset className="stack-sm" style={{ border: 0, padding: 0, margin: 0 }}>
          <legend className="label" style={{ marginBottom: 6 }}>Format</legend>
          {([["csv", "CSV", "opens in Excel; one row per log line"], ["json", "JSON", "an array of objects"], ["ndjson", "NDJSON", "one JSON object per line"]] as const).map(([v, l, h]) => (
            <label key={v} className="check"><input type="radio" name="fmt" checked={format === v} onChange={() => setFormat(v)} /> <span><strong>{l}</strong> <span className="hint">· {h}</span></span></label>
          ))}
        </fieldset>
        <fieldset className="stack-sm" style={{ border: 0, padding: 0, margin: 0 }}>
          <legend className="label" style={{ marginBottom: 6 }}>Columns</legend>
          <label className="check"><input type="radio" name="cols" checked={which === "all"} onChange={() => setWhich("all")} /> <span>Every column (full message bodies)</span></label>
          <label className="check"><input type="radio" name="cols" checked={which === "shown"} onChange={() => setWhich("shown")} disabled={!shownColumns.length} /> <span>Only the shown columns <span className="hint">· {shownColumns.join(", ") || "none"}</span></span></label>
        </fieldset>
        <div className="field" style={{ maxWidth: 260 }}>
          <label className="label" htmlFor="exp-limit">Log lines <span className="hint">· up to {num(cap)}{total > max ? ` (the first ${num(max)} in the current order)` : ""}</span></label>
          <input id="exp-limit" className="input" type="number" min={1} max={cap} value={limit} onChange={(e) => setLimit(Number(e.target.value))} />
        </div>
        <Callout tone="neutral" icon="info">Logs can hold customer details (tracking numbers, zip codes). Treat the file accordingly.</Callout>
        {error ? <ErrorCallout error={error} /> : null}
      </div>
    </Dialog>
  );
}

// ------------------------------------------------------------------ detail dialog

const SKIP_EMPTY = true;
export const DETAIL_ORDER = ["log_time", "level", "service", "msg", "exception", "body", "error_code", "error_message", "error_identifier",
  "error_module", "status_code", "class", "method", "line", "logger", "parent_log", "child_log", "tenant_id", "request_uuid",
  "web_id", "carrier", "page_type", "response_time_ms", "buyer_zip", "shipper_zip", "kv_json", "raw", "host", "instance_id",
  "container_id", "app", "cluster", "source_file", "format", "parse_ok", "log_time_ms", "ingest_time_ms"];

export function DetailDialog({ base, hit, zone, position, onPrev, onNext, onClose, onFilter }: {
  base: string;
  hit: LogHit;
  zone: Zone;
  position: string;
  onPrev?: () => void;
  onNext?: () => void;
  onClose: () => void;
  onFilter: (f: LogFilter) => void;
}) {
  const toast = useToast();
  const [tab, setTab] = useState<"fields" | "json">("fields");
  const [filter, setFilter] = useState("");
  const [showEmpty, setShowEmpty] = useState(!SKIP_EMPTY);
  const full = useQuery({
    queryKey: ["record", base, hit._ref.key, hit._ref.row],
    queryFn: () => get<LogHit>(`${base}/_record`, { key: hit._ref.key, row: hit._ref.row }),
    staleTime: Infinity,
  });
  const rec = full.data ?? hit;
  const entries = useMemo(() => {
    const all = Object.entries(rec).filter(([k]) => k !== "_ref" && k !== "_truncated");
    const rank = (k: string) => { const i = DETAIL_ORDER.indexOf(k); return i < 0 ? DETAIL_ORDER.length : i; };
    return all.sort((a, b) => rank(a[0]) - rank(b[0]));
  }, [rec]);
  const json = useMemo(() => JSON.stringify(Object.fromEntries(entries.filter(([, v]) => v !== null)), null, 2), [entries]);
  const rows = entries.filter(([k, v]) => (showEmpty || (v !== null && v !== "")) && (!filter || k.toLowerCase().includes(filter.toLowerCase())));
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA") return;
      if (e.key === "ArrowLeft" && onPrev) onPrev();
      if (e.key === "ArrowRight" && onNext) onNext();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onPrev, onNext]);
  const BIG = ["body", "raw", "msg", "kv_json"];
  return (
    <Dialog wide title={<span className="row" style={{ gap: 10 }}><LevelBadge level={rec.level} /><span className="mono" style={{ fontSize: 16 }}>{fmtTime(rec.log_time_ms as number, zone)}</span></span>}
      subtitle={<>{String(rec.service ?? "")} · {String(rec.host ?? "").split(".")[0]} · line {position}</>} onClose={onClose}
      footer={<>
        <button type="button" className="btn btn-ghost" onClick={onPrev} disabled={!onPrev}><Icon name="chevronLeft" /> Previous</button>
        <button type="button" className="btn btn-ghost" onClick={onNext} disabled={!onNext}>Next <Icon name="chevron" /></button>
        <div className="grow" />
        <button type="button" className="btn" onClick={() => copyText(json).then(() => toast("Copied the log line as JSON"))}><Icon name="copy" /> Copy JSON</button>
        <button type="button" className="btn btn-primary" onClick={onClose}>Close</button>
      </>}>
      <div className="stack">
        {full.error ? <ErrorCallout error={full.error} /> : null}
        {(rec.exception || rec.msg || rec.error_message) ? (
          <div className="detail-summary">
            {rec.msg ? <div className="mono">{String(rec.msg)}</div> : null}
            {rec.exception ? <div className="mono" style={{ color: "var(--danger-text)" }}>{String(rec.exception)}</div> : null}
            {rec.error_message ? <div>{String(rec.error_code ?? "")} {String(rec.error_message)}</div> : null}
          </div>
        ) : null}
        <div className="row" style={{ gap: 12 }}>
          <div className="tabs" role="tablist" aria-label="Log line view">
            <button type="button" role="tab" className="tab" aria-selected={tab === "fields"} onClick={() => setTab("fields")}>Fields</button>
            <button type="button" role="tab" className="tab" aria-selected={tab === "json"} onClick={() => setTab("json")}>JSON</button>
          </div>
          {full.isFetching && <Spinner label="Loading the full line" />}
          <div className="grow" />
          {tab === "fields" && <>
            <label className="check" style={{ fontSize: 13 }}><input type="checkbox" checked={showEmpty} onChange={(e) => setShowEmpty(e.target.checked)} /> Empty columns</label>
            <input className="input" aria-label="Filter fields" placeholder="Filter fields" style={{ maxWidth: 220, minHeight: 34 }} value={filter} onChange={(e) => setFilter(e.target.value)} />
          </>}
        </div>
        {tab === "json" ? (
          <pre className="code-block" style={{ maxHeight: "60vh", whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{json}</pre>
        ) : (
          <div className="table-scroll" style={{ maxHeight: "60vh", border: "1px solid var(--border)", borderRadius: "var(--radius)" }}>
            <table className="table compact">
              <thead><tr><th style={{ width: "24%" }}>Column</th><th>Value</th><th style={{ width: 80 }}><span className="sr-only">Filter</span></th></tr></thead>
              <tbody>
                {rows.map(([k, v]) => {
                  const t = k === "log_time_ms" || k === "ingest_time_ms" ? `${v}  (${fmtTime(v as number, zone)}${zone === "utc" ? " UTC" : ""})`
                    : k === "log_time" && v ? `${String(v)} UTC (as written in the file)` : cellText(v);
                  const scalar = v !== null && v !== undefined && typeof v !== "object" && String(v).length <= 500;
                  return (
                    <Fragment key={k}>
                      <tr>
                        <td style={{ verticalAlign: "top" }}><span className="mono" style={{ fontSize: 13 }}>{k}</span></td>
                        <td className="cell-mono" style={{ overflowWrap: "anywhere", whiteSpace: "pre-wrap", verticalAlign: "top" }}>
                          {t ? (BIG.includes(k) && t.length > 300 ? <pre className="code-block" style={{ maxHeight: 260, whiteSpace: "pre-wrap", margin: 0 }}>{t}</pre> : t) : <span className="faint">{v === null ? "null" : "—"}</span>}
                        </td>
                        <td style={{ whiteSpace: "nowrap", verticalAlign: "top" }}>
                          {scalar && k !== "log_time" && k !== "log_time_ms" && k !== "ingest_time_ms" && (
                            <>
                              <button type="button" className="btn btn-ghost mini-btn" title="Filter for this value" aria-label={`Filter for ${k} = ${t}`} onClick={() => onFilter({ field: k, op: "is", value: String(v) })}><Icon name="plus" size={15} /></button>
                              <button type="button" className="btn btn-ghost mini-btn" title="Filter out this value" aria-label={`Filter out ${k} = ${t}`} onClick={() => onFilter({ field: k, op: "is_not", value: String(v) })}><Icon name="minus" size={15} /></button>
                            </>
                          )}
                        </td>
                      </tr>
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        <span className="hint mono" style={{ overflowWrap: "anywhere" }}>s3 file: {hit._ref.key} · row {hit._ref.row}</span>
        {hit._truncated && !full.data && <Badge tone="amber">Loading the full text…</Badge>}
      </div>
    </Dialog>
  );
}
