import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { Fragment, useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { get, request, type LbOverview, type LbRequest } from "../api";
import { Icon } from "../components/icons";
import { Page } from "../components/Shell";
import { Empty, ErrorCallout, Loading, Spinner, copyText, useToast } from "../components/ui";
import { duration, fmtTime, localZoneName, num, storedZone, storeZone, type Zone } from "../format";
import { useMe } from "../session";
import { CodePill, DETAIL, LbExportDialog, STATUS_CHOICES, lbBodyFromParams, lbLink, secs } from "./LoadBalancers";
import { TimePicker, ZoneToggle } from "./Overview";

// The load balancer access logs line by line, as AWS wrote them: pick a load balancer and
// target group, the lines fill the window and keep loading as you scroll. Same URL parameters
// as the Load balancers page, so the two link to each other with the same filters.

const BATCH = 200;
const MAX_ROWS = 5000;
const WRAP_KEY = "vlg.lbwrap";

function storedWrap(): boolean {
  try {
    return localStorage.getItem(WRAP_KEY) === "1";
  } catch {
    return false;
  }
}

type LinesPage = { start: number; end: number; total: number; hits: LbRequest[]; tookMs: number };

export function LbLogs() {
  const me = useMe().data!;
  const toast = useToast();
  const [params, setParams] = useSearchParams();
  const p = (k: string) => params.get(k) ?? "";
  const set = (patch: Record<string, string | null>) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(patch)) {
      if (v === null || v === "") next.delete(k);
      else next.set(k, v);
    }
    setParams(next);
  };
  const [zone, setZoneState] = useState<Zone>(storedZone());
  const setZone = (z: Zone) => { storeZone(z); setZoneState(z); };
  const [wrap, setWrapState] = useState(storedWrap());
  const setWrap = (v: boolean) => {
    try { localStorage.setItem(WRAP_KEY, v ? "1" : "0"); } catch { /* storage unavailable */ }
    setWrapState(v);
  };
  const [qDraft, setQDraft] = useState(p("q"));
  useEffect(() => setQDraft(p("q")), [params]); // eslint-disable-line react-hooks/exhaustive-deps
  const [nonce, setNonce] = useState(0);
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [full, setFull] = useState(false);
  const [exporting, setExporting] = useState(false);
  const order = p("order") === "asc" ? "asc" : "desc";
  const start = p("start") || "now-1h";
  const end = p("end") || "now";

  const body = useMemo(() => lbBodyFromParams(params), [params]);
  const overview = useQuery({ queryKey: ["lb-overview"], queryFn: () => get<LbOverview>("/lb"), staleTime: 60_000, retry: false });
  const lines = useInfiniteQuery({
    queryKey: ["lb-lines", body, order, nonce],
    initialPageParam: { offset: 0 } as { offset: number; start?: number; end?: number },
    queryFn: ({ pageParam, signal }) => {
      // later batches reuse the first batch's resolved window, so "last hour" doesn't slide
      const b = pageParam.start ? { ...body, start: String(pageParam.start), end: String(pageParam.end) } : body;
      return request<LinesPage>("/lb/_requests", { method: "POST", signal, body: { ...b, order, offset: pageParam.offset, size: BATCH } });
    },
    getNextPageParam: (last, pages) => {
      const loaded = pages.reduce((n, pg) => n + pg.hits.length, 0);
      return loaded < last.total && loaded < MAX_ROWS && last.hits.length > 0
        ? { offset: loaded, start: pages[0].start, end: pages[0].end } : undefined;
    },
    retry: false,
    staleTime: 60_000,
  });
  const first = lines.data?.pages[0];
  const hits = useMemo(() => (lines.data?.pages ?? []).flatMap((pg) => pg.hits), [lines.data]);
  const total = first?.total ?? 0;
  useEffect(() => setOpen(new Set()), [body, order, nonce]);

  // load more when the bottom of the list comes into view
  const scrollRef = useRef<HTMLDivElement>(null);
  const sentinel = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = sentinel.current;
    if (!el || !scrollRef.current) return;
    const io = new IntersectionObserver((entries) => {
      if (entries[0].isIntersecting && lines.hasNextPage && !lines.isFetchingNextPage) lines.fetchNextPage();
    }, { root: scrollRef.current, rootMargin: "400px" });
    io.observe(el);
    return () => io.disconnect();
  }, [lines.hasNextPage, lines.isFetchingNextPage, lines, hits.length]);
  useEffect(() => { scrollRef.current?.scrollTo({ top: 0 }); }, [body, order, nonce]);

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

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (qDraft.trim() === p("q")) setNonce((n) => n + 1);
    else set({ q: qDraft.trim() || null });
  };
  const toggleRow = (id: string) => setOpen((o) => { const n = new Set(o); if (n.has(id)) n.delete(id); else n.add(id); return n; });

  // load balancer and target group choices (only what this person may see)
  const items = overview.data?.items ?? [];
  const lbNames = items.map((i) => i.name);
  const tgNames = useMemo(() => {
    const src = p("lb") ? items.filter((i) => i.name === p("lb")) : items;
    const names = new Set<string>();
    let none = false;
    for (const i of src) for (const t of i.targetGroups) { if (t.name) names.add(t.name); else none = true; }
    if (p("tg") && p("tg") !== "-") names.add(p("tg"));
    return { names: [...names].sort(), none };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items, params]);

  const chips: [string, ReactNode, string[]][] = [];
  if (p("pg")) chips.push(["pg", <>path <strong>{p("method") ? `${p("method")} ` : ""}{p("pg")}</strong></>, ["pg", "method"]]);
  else if (p("method")) chips.push(["method", <>method <strong>{p("method")}</strong></>, ["method"]]);
  if (p("path")) chips.push(["path", <>path contains <strong>{p("path")}</strong></>, ["path"]]);
  if (p("code")) chips.push(["code", <>status <strong>{p("code")}</strong></>, ["code"]]);
  if (p("domain")) chips.push(["domain", <>domain <strong>{p("domain")}</strong></>, ["domain"]]);
  if (p("client")) chips.push(["client", <>client <strong>{p("client")}</strong></>, ["client"]]);
  if (p("target")) chips.push(["target", <>target <strong>{p("target")}</strong></>, ["target"]]);
  if (p("slow")) chips.push(["slow", <>target time ≥ <strong>{secs(Number(p("slow")))}</strong></>, ["slow"]]);
  const lastError = overview.data?.converter.lastError;

  return (
    <Page fill crumbs={[{ label: "Infrastructure" }, { label: "Load balancer logs" }]} title="Load balancer logs"
      actions={<ZoneToggle zone={zone} onChange={setZone} />}>
      <section className="card logview">
        <form className="lv-bar" onSubmit={submit}>
          <div className="search grow" style={{ minWidth: 260 }}>
            <label htmlFor="lbl-q" className="sr-only">Search the lines</label>
            <Icon name="search" size={16} />
            <input id="lbl-q" className="input mono" value={qDraft} spellCheck={false} autoComplete="off"
              placeholder='Words anywhere in the line: path, IP, trace id, user agent… "exact phrase", -exclude' onChange={(e) => setQDraft(e.target.value)} />
          </div>
          <TimePicker start={start} end={end} zone={zone} maxHours={me.limits.maxSearchHours} compact
            onChange={(s, e) => set({ start: s === "now-1h" && e === "now" ? null : s, end: e === "now" ? null : e })} />
          <button type="submit" className="btn btn-primary"><Icon name="search" /> Search</button>
        </form>
        <div className="lv-bar" style={{ paddingTop: 0 }}>
          <div className="field">
            <label className="sr-only" htmlFor="lbl-lb">Load balancer</label>
            <select id="lbl-lb" className="select" style={{ width: "auto" }} value={p("lb")} onChange={(e) => set({ lb: e.target.value || null, tg: null })}>
              <option value="">All load balancers</option>
              {lbNames.map((n) => <option key={n} value={n}>{n}</option>)}
            </select>
          </div>
          <div className="field">
            <label className="sr-only" htmlFor="lbl-tg">Target group</label>
            <select id="lbl-tg" className="select mono" style={{ width: "auto", maxWidth: 300 }} value={p("tg")} onChange={(e) => set({ tg: e.target.value || null })}>
              <option value="">All target groups</option>
              {tgNames.names.map((n) => <option key={n} value={n}>{n}</option>)}
              {(tgNames.none || p("tg") === "-") && <option value="-">(no target group)</option>}
            </select>
          </div>
          <div className="field">
            <label className="sr-only" htmlFor="lbl-st">Status</label>
            <select id="lbl-st" className="select" style={{ width: "auto" }} value={p("st")} onChange={(e) => set({ st: e.target.value || null })}>
              {STATUS_CHOICES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </select>
          </div>
          <div className="row filter-row grow">
            {chips.map(([k, label, keys]) => (
              <span key={k} className="chip" style={{ fontFamily: "var(--font-sans)" }}>
                <span>{label}</span>
                <button type="button" aria-label="Remove filter" onClick={() => set(Object.fromEntries(keys.map((x) => [x, null])))}><Icon name="x" size={13} /></button>
              </span>
            ))}
          </div>
        </div>

        {lastError && (
          <div className="lv-status" style={{ background: "var(--danger-soft)", color: "var(--danger-text)" }}>
            <Icon name="alert" size={15} /> The server can't read the load balancer logs right now{me.admin ? `: ${lastError.message}` : "; the newest lines may be missing."}
          </div>
        )}
        <div className="lv-status">
          <strong>{first ? `${num(total)} line${total === 1 ? "" : "s"}` : "Searching…"}</strong>
          {first && <span className="hint">{fmtTime(first.start, zone, false).slice(5)} → {fmtTime(first.end, zone, false).slice(5)} {zone === "utc" ? "UTC" : localZoneName()} · {duration(first.tookMs)} · 5–10 min behind</span>}
          {lines.isFetching && <Spinner label="Searching" />}
          <div className="grow" />
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => set({ order: order === "desc" ? "asc" : null })}>
            <Icon name="caret" size={14} style={{ transform: order === "asc" ? "rotate(180deg)" : undefined }} /> {order === "desc" ? "Newest first" : "Oldest first"}
          </button>
          <label className="check lv-toggle" title="Show whole lines on several rows"><input type="checkbox" checked={wrap} onChange={(e) => setWrap(e.target.checked)} /> Wrap</label>
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => setExporting(true)} disabled={!total}><Icon name="download" size={15} /> Export</button>
          <Link className="btn btn-ghost btn-sm" to={lbLink("/lb", params)} title="Counts, chart, target groups and paths for these filters"><Icon name="overview" size={15} /> Dashboard</Link>
          <button type="button" className="btn btn-ghost btn-sm" onClick={toggleFull} aria-pressed={full}>
            <Icon name={full ? "collapse" : "expand"} size={15} /> {full ? "Exit full screen" : "Full screen"}
          </button>
        </div>

        <div className="lv-body">
          <div className="lv-scroll" ref={scrollRef}>
            {lines.error ? (
              <div className="card-body"><ErrorCallout error={lines.error} admin={me.admin} /></div>
            ) : !first ? (
              <Loading what="Reading the load balancer logs…" />
            ) : hits.length === 0 ? (
              <Empty title="No lines match">Try a longer time range, another target group or fewer words. Lines arrive 5–10 minutes after the request.</Empty>
            ) : (
              <table className={`table compact data-table logs-table lv-table ${wrap ? "wrap" : ""}`}>
                <thead>
                  <tr>
                    <th style={{ width: 28 }}><span className="sr-only">Expand</span></th>
                    <th>time ({zone === "utc" ? "UTC" : localZoneName()})</th>
                    <th>status</th>
                    <th>line, as written by the load balancer</th>
                  </tr>
                </thead>
                <tbody>
                  {hits.map((h, i) => {
                    const id = `${h.trace_id ?? ""}|${h.ts_ms}|${i}`;
                    const isOpen = open.has(id);
                    const code = h.elb_code as number | null;
                    const fromLb = (code ?? 0) >= 500 && (h.tgt_code === null || h.tgt_code === undefined);
                    return (
                      <Fragment key={id}>
                        <tr className={`clickable ${(code ?? 0) >= 500 ? "row-error" : ""} ${isOpen ? "open" : ""}`} onClick={() => toggleRow(id)}
                          tabIndex={0} aria-expanded={isOpen} onKeyDown={(e) => { if (e.key === "Enter") toggleRow(id); }}>
                          <td className="lv-caret"><Icon name="chevron" size={14} style={{ transform: isOpen ? "rotate(90deg)" : undefined }} /></td>
                          <td className="cell-mono nowrap">{fmtTime(h.ts_ms, zone)}</td>
                          <td className="nowrap"><CodePill code={code} fromLb={fromLb} /></td>
                          <td className="cell-mono data-cell msg-cell" title={!wrap && h.raw ? String(h.raw).slice(0, 1500) : undefined}>{h.raw ?? <span className="faint">—</span>}</td>
                        </tr>
                        {isOpen && (
                          <tr className="lv-detail-row">
                            <td colSpan={4}><LineDetail h={h} zone={zone} onFilter={(k, v) => set({ [k]: v })} onCopied={(what) => toast(`Copied the ${what}`)} /></td>
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
                {lines.isFetchingNextPage ? <><Spinner /> Loading more…</>
                  : lines.hasNextPage ? <button type="button" className="btn btn-sm" onClick={() => lines.fetchNextPage()}>Load more</button>
                  : hits.length >= MAX_ROWS && total > hits.length
                    ? <span className="hint">Showing the first {num(MAX_ROWS)} of {num(total)} lines. Narrow the time range or add filters, or use Export.</span>
                    : <span className="hint">End of results · {num(hits.length)} line{hits.length === 1 ? "" : "s"}</span>}
              </div>
            )}
          </div>
        </div>
      </section>
      {exporting && (
        <LbExportDialog body={first ? { ...body, start: String(first.start), end: String(first.end) } : body} total={total} max={me.limits.maxExportRows}
          onClose={() => setExporting(false)} onDone={(rows, name) => { setExporting(false); toast(`Downloaded ${num(rows)} lines as ${name}`); }} />
      )}
    </Page>
  );
}

/** One line opened in place: the line as written, then every field with filter buttons. */
function LineDetail({ h, zone, onFilter, onCopied }: {
  h: LbRequest; zone: Zone; onFilter: (k: string, v: string) => void; onCopied: (what: string) => void;
}) {
  const fields = DETAIL.filter(([f]) => h[f] !== undefined && h[f] !== null);
  const filterKey: Record<string, string> = { client_ip: "client", target: "target", tg: "tg", domain: "domain", elb_code: "code", path_group: "pg", lb: "lb" };
  return (
    <div className="lv-detail" onClick={(e) => e.stopPropagation()}>
      <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
        <span className="mono" style={{ fontSize: 13 }}>{fmtTime(h.ts_ms, zone)}</span>
        <span className="hint">· {h.lb ?? ""}{h.tg ? ` → ${h.tg}` : ""}{h.target ? ` → ${h.target}` : ""} · target took {secs(h.tgt_t as number | null)}</span>
        <div className="grow" />
        <button type="button" className="btn btn-ghost btn-sm" onClick={() => copyText(String(h.raw ?? "")).then(() => onCopied("line"))}><Icon name="copy" size={14} /> Copy line</button>
        <button type="button" className="btn btn-ghost btn-sm" onClick={() => copyText(JSON.stringify(Object.fromEntries(fields.map(([f]) => [f, h[f]])), null, 2)).then(() => onCopied("fields as JSON"))}><Icon name="copy" size={14} /> Copy JSON</button>
      </div>
      {h.raw && <pre className="code-block" style={{ margin: 0, whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{String(h.raw)}</pre>}
      <div className="lv-kv">
        {fields.map(([f, label]) => (
          <div key={f} className="lv-kv-row">
            <span className="lv-k" title={f}>{label}</span>
            <span className="mono lv-v">{String(h[f])}</span>
            <span className="lv-kv-actions">
              {filterKey[f] && (
                <button type="button" className="btn btn-ghost mini-btn" title="Only lines with this value" aria-label={`Filter on ${label}`}
                  onClick={() => onFilter(filterKey[f], String(h[f]))}><Icon name="filter" size={13} /></button>
              )}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
