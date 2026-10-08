import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { Fragment, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { Link, Navigate, useParams, useSearchParams } from "react-router-dom";
import { downloadPost, enc, get, request, type Column, type LogFilter, type LogHit, type SearchBody, type SearchResult } from "../api";
import { Icon } from "../components/icons";
import { Page } from "../components/Shell";
import { Empty, ErrorCallout, Loading, Spinner, copyText, useToast } from "../components/ui";
import { duration, exportZone, fmtTime, localZoneName, num, storedZone, storeZone, type Zone } from "../format";
import { useClusters, useMe } from "../session";
import {
  Cell, ColumnsDialog, DEFAULT_COLS, DEFAULT_RANGE, DETAIL_ORDER, ExportDialog, FacetList, FilterDialog, LevelBadge, MESSAGE, NEGATIVE,
  SaveDialog, SavedMenu, TimePicker, ZoneToggle, cellText, exportedText, filterText, rangeLabel, readJson, saveDownload,
} from "./Overview";

// The logs-first view: the search bar and filters on top, the log lines fill the rest of the
// screen and load more as you scroll. Same URL parameters as Overview, so a link or a saved
// search opens the same search in either view.

const BATCH = 200;
const MAX_ROWS = 5000;
const WRAP_KEY = "vlg.wrap";
const FIELDS_KEY = "vlg.fields";

function stored(key: string, fallback: boolean): boolean {
  try {
    const v = localStorage.getItem(key);
    return v === null ? fallback : v === "1";
  } catch {
    return fallback;
  }
}
function store(key: string, v: boolean) {
  try {
    localStorage.setItem(key, v ? "1" : "0");
  } catch {
    /* storage unavailable */
  }
}

export function LogView() {
  const { clusterId = "" } = useParams();
  const me = useMe().data!;
  const clusters = useClusters();
  const [params, setParams] = useSearchParams();
  const toast = useToast();
  const base = `/clusters/${enc(clusterId)}/logs`;

  const start = params.get("start") || DEFAULT_RANGE;
  const end = params.get("end") || "now";
  const q = params.get("q") ?? "";
  const filters = useMemo(() => readJson<LogFilter[]>(params.get("f"), []), [params]);
  const order = params.get("order") === "asc" ? "asc" : "desc";
  const cols = useMemo(() => (params.get("cols") ? params.get("cols")!.split(",").filter(Boolean) : DEFAULT_COLS), [params]);
  const set = (patch: Record<string, string | null>) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(patch)) {
      if (v === null || v === "") next.delete(k);
      else next.set(k, v);
    }
    next.delete("off");
    setParams(next);
  };

  const [zone, setZoneState] = useState<Zone>(storedZone());
  const setZone = (z: Zone) => { storeZone(z); setZoneState(z); };
  const [wrap, setWrapState] = useState(stored(WRAP_KEY, false));
  const setWrap = (v: boolean) => { store(WRAP_KEY, v); setWrapState(v); };
  const [showFields, setShowFieldsState] = useState(stored(FIELDS_KEY, false));
  const setShowFields = (v: boolean) => { store(FIELDS_KEY, v); setShowFieldsState(v); };
  const [qDraft, setQDraft] = useState(q);
  useEffect(() => setQDraft(q), [q]);
  const [nonce, setNonce] = useState(0);
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [filterEdit, setFilterEdit] = useState<{ i: number | null; f: LogFilter } | null>(null);
  const [colsOpen, setColsOpen] = useState(false);
  const [exportOpen, setExportOpen] = useState(false);
  const [saveOpen, setSaveOpen] = useState(false);
  const [savedMenu, setSavedMenu] = useState(false);
  const [full, setFull] = useState(false);

  const body: SearchBody = useMemo(() => ({ start, end, query: q, filters, order }), [start, end, q, filters, order]);
  const search = useInfiniteQuery({
    queryKey: ["logview", clusterId, body, nonce],
    initialPageParam: { offset: 0 } as { offset: number; start?: number; end?: number },
    queryFn: ({ pageParam, signal }) => {
      // Later batches reuse the first batch's resolved window, so "last hour" doesn't slide.
      const b = pageParam.start ? { ...body, start: String(pageParam.start), end: String(pageParam.end) } : body;
      return request<SearchResult>(`${base}/_search`, { method: "POST", signal, body: { ...b, offset: pageParam.offset, size: BATCH, aggregations: pageParam.offset === 0 } });
    },
    getNextPageParam: (last, pages) => {
      const loaded = pages.reduce((n, p) => n + p.hits.length, 0);
      return loaded < last.total && loaded < MAX_ROWS && last.hits.length > 0
        ? { offset: loaded, start: pages[0].start, end: pages[0].end } : undefined;
    },
    enabled: !!clusterId,
    retry: false,
    staleTime: 60_000,
  });
  const first = search.data?.pages[0];
  const hits = useMemo(() => (search.data?.pages ?? []).flatMap((p) => p.hits), [search.data]);
  const total = first?.total ?? 0;
  const columns: Column[] = first?.columns ?? [];
  useEffect(() => setOpen(new Set()), [bodyKeyOf(clusterId, body, nonce)]);

  // load more when the bottom of the list comes into view
  const scrollRef = useRef<HTMLDivElement>(null);
  const sentinel = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = sentinel.current;
    if (!el || !scrollRef.current) return;
    const io = new IntersectionObserver((entries) => {
      if (entries[0].isIntersecting && search.hasNextPage && !search.isFetchingNextPage) search.fetchNextPage();
    }, { root: scrollRef.current, rootMargin: "400px" });
    io.observe(el);
    return () => io.disconnect();
  }, [search.hasNextPage, search.isFetchingNextPage, search, hits.length]);
  useEffect(() => { scrollRef.current?.scrollTo({ top: 0 }); }, [body, nonce]);

  // full screen: the browser's full screen plus hiding the side menu
  useEffect(() => {
    const onChange = () => { if (!document.fullscreenElement) setFull(false); };
    document.addEventListener("fullscreenchange", onChange);
    return () => document.removeEventListener("fullscreenchange", onChange);
  }, []);
  useEffect(() => {
    document.body.classList.toggle("focus-mode", full);
    return () => document.body.classList.remove("focus-mode");
  }, [full]);
  const toggleFull = () => {
    if (!full) {
      setFull(true);
      document.documentElement.requestFullscreen?.().catch(() => { /* not allowed: the focus mode still applies */ });
    } else {
      setFull(false);
      if (document.fullscreenElement) document.exitFullscreen?.().catch(() => {});
    }
  };

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
  const toggleRow = (id: string) => setOpen((o) => { const n = new Set(o); if (n.has(id)) n.delete(id); else n.add(id); return n; });
  const describe = [rangeLabel(start, end, zone), q && `“${q}”`, ...filters.map(filterText)].filter(Boolean).join(" · ");
  const overviewLink = `/overview/${enc(clusterId)}?${params.toString()}`;

  if (clusters.data && !clusters.data.some((c) => c.id === clusterId)) {
    return clusters.data.length ? <Navigate to={`/logs/${enc(clusters.data[0].id)}`} replace /> : <Navigate to="/" replace />;
  }

  return (
    <Page fill crumbs={[{ label: "Logs", to: "/" }, { label: clusterId }]} title={`Logs · ${clusterId}`}
      actions={<ZoneToggle zone={zone} onChange={setZone} />}>
      <section className="card logview">
        <form className="lv-bar" onSubmit={submit}>
          <div className="search grow" style={{ minWidth: 260 }}>
            <label htmlFor="lv-q" className="sr-only">Search the logs</label>
            <Icon name="search" size={16} />
            <input id="lv-q" className="input mono" value={qDraft} spellCheck={false} autoComplete="off"
              placeholder='Search: words, "exact phrase", -exclude, column:value' onChange={(e) => setQDraft(e.target.value)} />
          </div>
          <TimePicker start={start} end={end} zone={zone} maxHours={me.limits.maxSearchHours} onChange={setRange} compact draft={draft} />
          <button type="submit" className="btn btn-primary"><Icon name="search" /> Search</button>
        </form>
        <div className="lv-bar" style={{ paddingTop: 0 }}>
          <div className="row filter-row grow">
            <button type="button" className={`btn btn-sm ${showFields ? "btn-on" : ""}`} aria-pressed={showFields} onClick={() => setShowFields(!showFields)}
              title="Top values you can click to filter"><Icon name="filter" size={15} /> Fields</button>
            {filters.map((f, i) => (
              <span key={i} className={`chip ${NEGATIVE.includes(f.op) ? "deny" : ""}`}>
                <button type="button" className="chip-text" onClick={() => setFilterEdit({ i, f })} title="Edit filter">{filterText(f)}</button>
                <button type="button" aria-label={`Remove filter ${filterText(f)}`} onClick={() => setFilters(filters.filter((_, j) => j !== i))}><Icon name="x" size={14} /></button>
              </span>
            ))}
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => setFilterEdit({ i: null, f: { field: "level", op: "is", value: "" } })}>
              <Icon name="plus" size={15} /> Add filter
            </button>
            {filters.length > 1 && <button type="button" className="btn btn-ghost btn-sm" onClick={() => setFilters([])}>Clear</button>}
          </div>
          <div className="row" style={{ gap: 6 }}>
            <SavedMenu open={savedMenu} setOpen={setSavedMenu} clusterId={clusterId} />
            <button type="button" className="btn btn-sm" onClick={() => setSaveOpen(true)}><Icon name="bookmark" size={15} /> Save</button>
            <button type="button" className="btn btn-sm" onClick={() => copyText(window.location.href).then(() => toast("Link copied"))}><Icon name="link" size={15} /> Link</button>
          </div>
        </div>

        <div className="lv-status">
          <strong>{first ? `${num(total)} log line${total === 1 ? "" : "s"}` : "Searching…"}</strong>
          {first && <span className="hint" title={`${num(first.files)} Parquet files read in ${duration(first.tookMs)}`}>{fmtTime(first.start, zone, false).slice(5)} → {fmtTime(first.end, zone, false).slice(5)} {zone === "utc" ? "UTC" : localZoneName()} · {duration(first.tookMs)}</span>}
          {search.isFetching && <Spinner label="Searching" />}
          <div className="grow" />
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => set({ order: order === "desc" ? "asc" : null })}>
            <Icon name="caret" size={14} style={{ transform: order === "asc" ? "rotate(180deg)" : undefined }} /> {order === "desc" ? "Newest first" : "Oldest first"}
          </button>
          <label className="check lv-toggle" title="Show long messages on several lines"><input type="checkbox" checked={wrap} onChange={(e) => setWrap(e.target.checked)} /> Wrap</label>
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => setColsOpen(true)} disabled={!columns.length}><Icon name="columns" size={15} /> Columns</button>
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => setExportOpen(true)} disabled={!total}><Icon name="download" size={15} /> Export</button>
          <Link className="btn btn-ghost btn-sm" to={overviewLink} title="Chart and top values for this search"><Icon name="overview" size={15} /> Overview</Link>
          <button type="button" className="btn btn-ghost btn-sm" onClick={toggleFull} aria-pressed={full}>
            <Icon name={full ? "collapse" : "expand"} size={15} /> {full ? "Exit full screen" : "Full screen"}
          </button>
        </div>

        <div className="lv-body">
          {showFields && first && (
            <aside className="lv-fields" aria-label="Top values">
              {Object.entries(first.facets ?? {}).map(([field, items]) => (
                <FacetList key={field} field={field} items={items} total={total} filters={filters} onFilter={addFilter} />
              ))}
              {!Object.keys(first.facets ?? {}).length && <span className="hint" style={{ padding: 12 }}>No values to show.</span>}
            </aside>
          )}
          <div className="lv-scroll" ref={scrollRef}>
            {search.error ? (
              <div className="card-body"><ErrorCallout error={search.error} admin={me.admin} /></div>
            ) : !first ? (
              <Loading what="Searching… the first search of a time range downloads its files from S3; later ones are faster." />
            ) : hits.length === 0 ? (
              <Empty title="No log lines match">Try a longer time range, fewer words or remove a filter.</Empty>
            ) : (
              <table className={`table compact data-table logs-table lv-table ${wrap ? "wrap" : ""}`}>
                <thead>
                  <tr>
                    <th style={{ width: 28 }}><span className="sr-only">Expand</span></th>
                    {cols.map((c) => <th key={c}>{c === MESSAGE ? "message" : c === "log_time" ? `time (${zone === "utc" ? "UTC" : localZoneName()})` : c}</th>)}
                  </tr>
                </thead>
                <tbody>
                  {hits.map((h) => {
                    const id = `${h._ref.key}#${h._ref.row}`;
                    const isOpen = open.has(id);
                    return (
                      <Fragment key={id}>
                        <tr className={`clickable ${String(h.level ?? "").toUpperCase() === "ERROR" ? "row-error" : ""} ${isOpen ? "open" : ""}`}
                          onClick={() => toggleRow(id)} tabIndex={0} aria-expanded={isOpen} onKeyDown={(e) => { if (e.key === "Enter") toggleRow(id); }}>
                          <td className="lv-caret"><Icon name="chevron" size={14} style={{ transform: isOpen ? "rotate(90deg)" : undefined }} /></td>
                          {cols.map((c) => <Cell key={c} col={c} hit={h} zone={zone} />)}
                        </tr>
                        {isOpen && (
                          <tr className="lv-detail-row">
                            <td colSpan={cols.length + 1}><Expanded base={base} hit={h} zone={zone} onFilter={addFilter} /></td>
                          </tr>
                        )}
                      </Fragment>
                    );
                  })}
                </tbody>
              </table>
            )}
            {first && hits.length > 0 && (
              <div ref={sentinel} className="lv-more">
                {search.isFetchingNextPage ? <><Spinner /> Loading more…</>
                  : search.hasNextPage ? <button type="button" className="btn btn-sm" onClick={() => search.fetchNextPage()}>Load more</button>
                  : hits.length >= MAX_ROWS && total > hits.length
                    ? <span className="hint">Showing the first {num(MAX_ROWS)} of {num(total)} lines. Narrow the time range or add filters, or use Export.</span>
                    : <span className="hint">End of results · {num(hits.length)} line{hits.length === 1 ? "" : "s"}</span>}
              </div>
            )}
          </div>
        </div>
      </section>

      {filterEdit && (
        <FilterDialog columns={columns} facets={first?.facets ?? {}} initial={filterEdit.f} editing={filterEdit.i !== null}
          onClose={() => setFilterEdit(null)}
          onSave={(f) => {
            if (filterEdit.i === null) setFilters([...filters, f]);
            else setFilters(filters.map((x, j) => (j === filterEdit.i ? f : x)));
            setFilterEdit(null);
          }} />
      )}
      {colsOpen && (
        <ColumnsDialog columns={columns} selected={cols} onClose={() => setColsOpen(false)}
          onSave={(c) => { set({ cols: c.join(",") === DEFAULT_COLS.join(",") ? null : c.join(",") }); setColsOpen(false); }} />
      )}
      {exportOpen && (
        <ExportDialog total={total} shownColumns={cols.filter((c) => c !== MESSAGE)} max={me.limits.maxExportRows} describe={describe} order={order} zone={zone} onClose={() => setExportOpen(false)}
          run={async (format, columns, limit) => {
            const anchored = first ? { ...body, start: String(first.start), end: String(first.end) } : body;
            const d = await downloadPost(`${base}/_export`, { ...anchored, format, columns: columns.length ? columns : null, limit, timeZone: exportZone(zone), utcOffsetMinutes: zone === "utc" ? 0 : -new Date().getTimezoneOffset() });
            saveDownload(d);
            toast(exportedText(d, zone));
            setExportOpen(false);
          }} />
      )}
      {saveOpen && <SaveDialog clusterId={clusterId} params={params.toString()} describe={describe} onClose={() => setSaveOpen(false)} />}
    </Page>
  );
}

function bodyKeyOf(cluster: string, body: SearchBody, nonce: number) {
  return JSON.stringify([cluster, body, nonce]);
}

/** The whole log line, under its row: every non-empty column, long texts in full, filter buttons. */
function Expanded({ base, hit, zone, onFilter }: { base: string; hit: LogHit; zone: Zone; onFilter: (f: LogFilter) => void }) {
  const toast = useToast();
  const full = useQuery({
    queryKey: ["record", base, hit._ref.key, hit._ref.row],
    queryFn: () => get<LogHit>(`${base}/_record`, { key: hit._ref.key, row: hit._ref.row }),
    staleTime: Infinity,
  });
  const rec = full.data ?? hit;
  const entries = useMemo(() => {
    const rank = (k: string) => { const i = DETAIL_ORDER.indexOf(k); return i < 0 ? DETAIL_ORDER.length : i; };
    return Object.entries(rec).filter(([k, v]) => k !== "_ref" && k !== "_truncated" && v !== null && v !== "")
      .sort((a, b) => rank(a[0]) - rank(b[0]));
  }, [rec]);
  const BIG = ["body", "raw", "kv_json", "msg"];
  const long = entries.filter(([k, v]) => BIG.includes(k) && typeof v === "string" && (v.length > 120 || v.includes("\n")));
  const short = entries.filter((e) => !long.includes(e));
  const json = JSON.stringify(Object.fromEntries(entries), null, 2);
  return (
    <div className="lv-detail" onClick={(e) => e.stopPropagation()}>
      <div className="row" style={{ gap: 8 }}>
        <LevelBadge level={rec.level} />
        <span className="mono" style={{ fontSize: 13 }}>{fmtTime(rec.log_time_ms as number, zone)}</span>
        {full.isFetching && <Spinner label="Loading the full line" />}
        <div className="grow" />
        <button type="button" className="btn btn-ghost btn-sm" onClick={() => copyText(json).then(() => toast("Copied the log line as JSON"))}><Icon name="copy" size={14} /> Copy JSON</button>
      </div>
      {full.error ? <ErrorCallout error={full.error} /> : null}
      {long.map(([k, v]) => (
        <div key={k} className="stack-sm" style={{ gap: 4 }}>
          <span className="label mono" style={{ fontSize: 12 }}>{k}</span>
          <pre className="code-block" style={{ maxHeight: 360, whiteSpace: "pre-wrap", overflowWrap: "anywhere", margin: 0 }}>{String(v)}</pre>
        </div>
      ))}
      <div className="lv-kv">
        {short.map(([k, v]) => {
          const t = k === "log_time_ms" || k === "ingest_time_ms" ? `${v} (${fmtTime(v as number, zone)})` : k === "log_time" ? `${String(v)} UTC` : cellText(v);
          const filterable = !["log_time", "log_time_ms", "ingest_time_ms"].includes(k) && String(v).length <= 500;
          return (
            <div key={k} className="lv-kv-row">
              <span className="mono lv-k">{k}</span>
              <span className="mono lv-v">{t}</span>
              {filterable ? (
                <span className="lv-kv-actions">
                  <button type="button" className="btn btn-ghost mini-btn" title="Only lines with this value" aria-label={`Filter for ${k} = ${t}`} onClick={() => onFilter({ field: k, op: "is", value: String(v) })}><Icon name="plus" size={14} /></button>
                  <button type="button" className="btn btn-ghost mini-btn" title="Hide lines with this value" aria-label={`Filter out ${k} = ${t}`} onClick={() => onFilter({ field: k, op: "is_not", value: String(v) })}><Icon name="minus" size={14} /></button>
                </span>
              ) : <span />}
            </div>
          );
        })}
      </div>
    </div>
  );
}
