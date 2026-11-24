import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useSearchParams } from "react-router-dom";
import { get, type AbConvGroup, type AbEddGroup, type AbEddName, type AbIds, type AbPageRow, type AbSource, type AbStoreRow, type AbSummary, type AbUaDim, type AbUaItem } from "../api";
import { Icon } from "../components/icons";
import { clock, clockZ, dateLabel, dayIn, Legend, LineChart, localStamp, offsetMs, useHidden, ZONE_LABEL, ZONE_TITLE, ZONES, zoneName, type ClockZone, type LineSeries } from "../components/LineChart";
import { Page } from "../components/Shell";
import { Callout, copyText, Dialog, Empty, ErrorCallout, Loading, SearchSelect, downloadFile, useToast } from "../components/ui";
import { ago, num } from "../format";
import "../ops.css";

// A/B test sessions from the EDD session cluster (eddsderrh1), counted every 5 minutes by
// scripts/ab_session_counts.py: entries per store and page type (PDP, CART, COP, OC) and whether their
// session ID is usable (starts with M). Bad ones (NA, ORDER_LOOKUP_CALL, xyz123, …) can't be put in an
// A/B group; a click on a count lists the orders that have it.

type SortKey = "store" | "entries" | "PDP" | "CART" | "COP" | "OC" | "ocBadPct";
const H = 3600_000;
/** Page types of the session history index. */
const PAGES = ["PDP", "CART", "COP", "OC"];
const ZONE_KEY = "ops-zone";                       // shared with the Ops dashboard
function storedZone(): ClockZone {
  try { const v = localStorage.getItem(ZONE_KEY); return ZONES.includes(v as ClockZone) ? (v as ClockZone) : "ist"; } catch { return "ist"; }
}

type Preset = "6h" | "24h" | "today" | "yesterday" | "3d" | "7d" | "30d";
const PRESETS: [Preset, string][] = [["6h", "6 h"], ["24h", "24 h"], ["today", "Today"], ["yesterday", "Yesterday"], ["3d", "3 days"], ["7d", "7 days"], ["30d", "30 days"]];
function zonedMs(y: number, mo: number, d: number, h: number, mi: number, zone: ClockZone): number {
  const guess = Date.UTC(y, mo - 1, d, h, mi);
  return guess - offsetMs(guess - offsetMs(guess, zone), zone);
}
function midnight(day: string, zone: ClockZone): number {
  const [y, mo, d] = day.split("-").map(Number);
  return zonedMs(y, mo, d, 0, 0, zone);
}
function presetRange(p: Preset, zone: ClockZone, now: number): [number, number] {
  const today = midnight(dayIn(zone, now), zone);
  switch (p) {
    case "6h": return [now - 6 * H, now];
    case "24h": return [now - 24 * H, now];
    case "today": return [today, now];
    case "yesterday": return [midnight(dayIn(zone, today - 1), zone), today];
    case "7d": return [now - 7 * 24 * H, now];
    case "30d": return [now - 30 * 24 * H, now];
    default: return [now - 3 * 24 * H, now];
  }
}
const stepText = (step: number) => (step < H ? `per ${step / 60_000} minutes` : step === H ? "per hour" : `per ${step / H} hours`);
const pct = (v: number | null | undefined, digits = 1) => (v === null || v === undefined ? "—" : `${v.toFixed(digits)}%`);
const kAxis = (v: number) => (v >= 1e6 ? `${+(v / 1e6).toFixed(2)}M` : v >= 1000 ? `${+(v / 1e3).toFixed(1)}k` : String(v));
const compact = (v: number) => (v >= 1e6 ? `${(v / 1e6).toFixed(2)}M` : v >= 1e4 ? `${(v / 1e3).toFixed(1)}k` : num(Math.round(v)));
const stamp = (ms: number, zone: ClockZone) => localStamp(ms, zone).slice(0, 16).replace(/[:T-]/g, "");

const PAGE_NAME: Record<string, string> = { PDP: "Product page (PDP)", CART: "Cart (CART)", COP: "Checkout (COP)", OC: "Order confirmation (OC)" };
const PAGE_COLOR: Record<string, string> = { PDP: "var(--svc-1)", CART: "var(--svc-3)", COP: "var(--svc-2)", OC: "var(--accent)" };
const CAT_LABEL: Record<string, string> = {
  valid: "Valid (M…)", NA: "NA", ORDER_LOOKUP_CALL: "ORDER_LOOKUP_CALL", xyz123: "xyz123", novalue: "novalue",
  empty: "Empty", missing: "Missing", numbers: "Numbers only", uuid: "UUID", other: "Other", health: "Health checks",
};
const CAT_TITLE: Record<string, string> = {
  valid: "Session ID starts with M: decodes to an A/B group",
  empty: "Session ID is an empty string", missing: "No session ID (or one longer than 256 characters)",
  numbers: "Session ID is only digits (not the MT… format)", uuid: "Session ID is a UUID (not the MT… format)",
  other: "Any other value: open the list to see them", health: "fenix-health-… health checks: not shoppers, left out of the charts and the bad share",
};
const catLabel = (c: string) => CAT_LABEL[c] ?? c;
const isBad = (c: string) => c !== "valid" && c !== "health";
/** The ID list exists for every page (bad session IDs of session_history and cop-derrh). */
const listable = (_source: AbSource, _page: string) => true;

function CardHead({ title, sub, right }: { title: ReactNode; sub?: ReactNode; right?: ReactNode }) {
  return (
    <div className="ops-card-head">
      <div className="grow"><span className="t">{title}</span>{sub && <span className="hint">{sub}</span>}</div>
      {right}
    </div>
  );
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
function BadPct({ v, n }: { v: number | null | undefined; n?: number }) {
  if (v === null || v === undefined) return <span className="dim">—</span>;
  const cls = v >= 10 ? "danger-text" : v >= 2 ? "warn-text" : "";
  return <span className={cls} title={n !== undefined ? `${num(n)} with a bad session ID` : undefined}>{pct(v)}</span>;
}

function Chart({ title, sub, series, start, step, zone, nowAt }: { title: string; sub: string; series: LineSeries[]; start: number; step: number; zone: ClockZone; nowAt: number | null }) {
  const [hidden, toggle] = useHidden();
  const keys = series.map((s) => s.key);
  let shown = series.filter((s) => !hidden.has(s.key));
  if (!shown.length) shown = series;
  const off = new Set(keys.filter((k) => !shown.some((s) => s.key === k)));
  return (
    <section className="card">
      <CardHead title={title} sub={sub} right={series.length > 1 ? <Legend series={series} hidden={off} onToggle={(k) => toggle(k, keys)} /> : <Legend series={series} />} />
      <LineChart label={`${title} ${sub}`} series={shown} legend={false} zone={zone} start={start} step={step} fmt={compact} axisFmt={kAxis} nowAt={nowAt} />
    </section>
  );
}

// ------------------------------------------------------------------ page
export function AbTest() {
  const [params, setParams] = useSearchParams();
  const set = (patch: Record<string, string | null>) => {
    const n = new URLSearchParams(params);
    for (const [k, v] of Object.entries(patch)) (v ? n.set(k, v) : n.delete(k));
    setParams(n, { replace: false });
  };
  const [zone, setZoneState] = useState<ClockZone>(storedZone());
  const setZone = (z: ClockZone) => { setZoneState(z); try { localStorage.setItem(ZONE_KEY, z); } catch { /* private window */ } };
  const preset = (params.get("range") as Preset) || "3d";
  const store = params.get("store") || "";
  const ids = params.get("ids");                    // "<source>|<page>|<category>": the open ID list
  const [all, setAll] = useState(false);
  const [now, setNow] = useState(() => Math.floor(Date.now() / 60_000) * 60_000);
  const live = preset !== "yesterday";
  useEffect(() => {
    if (!live) return;
    const t = setInterval(() => setNow(Math.floor(Date.now() / 60_000) * 60_000), 60_000);
    return () => clearInterval(t);
  }, [live]);
  const [from, to] = presetRange(preset, zone, now);

  const q = useQuery({
    queryKey: ["abtest", from, to, zone, store],
    queryFn: () => get<AbSummary>("/abtest/summary", { from, to, zone: zone.toUpperCase(), store }),
    retry: false, placeholderData: keepPreviousData, staleTime: 30_000, refetchInterval: live ? 120_000 : false,
  });
  const d = q.data;
  // no store picked: the busiest store in an A/B test, else the busiest
  useEffect(() => {
    if (!d || store || !d.stores.length) return;
    const list = [...d.stores].sort((a, b) => PAGES.reduce((t, p) => t + (b.pages[p] ?? 0), 0) - PAGES.reduce((t, p) => t + (a.pages[p] ?? 0), 0));
    const pick = list[0];
    set({ store: pick.store });
  }, [d, store]); // eslint-disable-line react-hooks/exhaustive-deps

  // entries of the session history index, busiest first
  const stores = useMemo(() => (d?.stores ?? [])
    .map((s) => ({ ...s, entries: PAGES.reduce((t, p) => t + (s.pages[p] ?? 0), 0) }))
    .filter((s) => s.entries > 0)
    .sort((a, b) => b.entries - a.entries || a.store.localeCompare(b.store)), [d]);
  const row = stores.find((s) => s.store === store);
  const storeOpts = useMemo(() => stores.map((s) => ({ value: s.store, label: s.store })), [stores]);
  const st = d?.store && d.store.store === store ? d.store : null;
  const nowAt = live ? d?.asOf ?? null : null;
  const status = d?.status;
  const ageMin = d?.asOf ? Math.round((Date.now() - d.asOf) / 60_000) : null;
  const late = ageMin !== null && ageMin > 20;
  const [sort, setSort] = useState<{ key: SortKey; desc: boolean }>({ key: "entries", desc: true });
  const sorted = useMemo(() => {
    const val = (s: AbStoreRow & { entries: number }): string | number | null => sort.key === "store" ? s.store : sort.key === "entries" ? s.entries
      : sort.key === "ocBadPct" ? s.ocBadPct : s.pages[sort.key] ?? 0;
    return [...stores].sort((x, y) => {
      const a = val(x), b = val(y);
      if (a === b) return y.entries - x.entries;
      if (a === null) return 1;                       // no OC: always last
      if (b === null) return -1;
      const c = typeof a === "string" ? a.localeCompare(b as string) : a - (b as number);
      return sort.desc ? -c : c;
    });
  }, [stores, sort]);
  const shownStores = all ? sorted : sorted.slice(0, 15);
  const Th = ({ k, children, title }: { k: SortKey; children: ReactNode; title?: string }) => (
    <th className={k === "store" ? undefined : "num"} title={title} aria-sort={sort.key === k ? (sort.desc ? "descending" : "ascending") : "none"}>
      <button type="button" className={`ops-sort ${sort.key === k ? "on" : ""}`} onClick={() => setSort({ key: k, desc: sort.key === k ? !sort.desc : k !== "store" })}>
        {children}<span className="ar" aria-hidden="true">{sort.key === k ? (sort.desc ? "↓" : "↑") : "↕"}</span>
      </button>
    </th>
  );
  const openIds = (source: AbSource, page: string, category: string, dim?: ListDim, value?: string) =>
    set({ ids: [source, page, category, ...(dim && value !== undefined ? [dim, value] : [])].join("|") });
  const [idSource, idPage, idCat, idDim, ...idRest] = (ids ?? "").split("|");
  const idValue = idRest.length ? idRest.join("|") : undefined;

  const exportStores = () => {
    if (!d) return;
    const head = ["from", "to", "store", "entries", "pdp", "cart", "cop", "oc", "oc_bad", "oc_bad_pct"];
    const f = localStamp(d.from, zone), t = localStamp(d.to, zone);
    const lines = [head.join(","), ...stores.map((s) => [f, t, s.store, s.entries, s.pages.PDP ?? 0, s.pages.CART ?? 0, s.pages.COP ?? 0,
      s.pages.OC ?? 0, s.ocBad, s.ocBadPct ?? ""].join(","))];
    downloadFile(`abtest-stores-${stamp(d.from, zone)}-${stamp(d.to, zone)}-${zone}.csv`, lines.join("\n") + "\n", "text/csv");
  };

  return (
    <Page crumbs={[{ label: "A/B test sessions" }]} title="A/B test sessions" wide>
      <div className="ops-head">
        <div className="grow">
          <h1>A/B test sessions</h1>
          <p className="sub">Entries in the EDD session indexes (eddsderrh1) per store and page, and whether their session ID can be put in an A/B group. A usable session ID starts with M.</p>
        </div>
        <div className="ops-head-right">
          <span className={`ab-fresh ${late || status?.ok === false ? "late" : ""}`}
            title={status?.lastError ? `Last error: ${status.lastError}` : d?.asOf ? `Counted every 5 minutes; data to ${clockZ(d.asOf, zone, true)}` : "Not counted yet"}>
            {d?.asOf ? <>Data to {clock(d.asOf, zone)} {zoneName(d.asOf, zone)}{late ? ` · ${ageMin} min behind` : ""}</> : "No data yet"}
          </span>
          <div className="tabs" role="group" aria-label="Time zone">
            {ZONES.map((z) => <button key={z} type="button" className="tab" aria-pressed={zone === z} aria-selected={zone === z} title={ZONE_TITLE[z]} onClick={() => setZone(z)}>{ZONE_LABEL[z]}</button>)}
          </div>
        </div>
      </div>

      <section className="card ops-range-card">
        <div className="ops-range-top">
          <div className="tabs ops-presets" role="group" aria-label="Time range">
            {PRESETS.map(([k, l]) => (
              <button key={k} type="button" className="tab" aria-pressed={preset === k} aria-selected={preset === k}
                onClick={() => set({ range: k === "3d" ? null : k })}>{l}</button>
            ))}
          </div>
          <div className="ops-toolbar" style={{ border: 0, padding: 0 }}>
            <SearchSelect id="ab-store" label="Store" value={store} options={storeOpts} onChange={(v) => set({ store: v || null, ids: null })} maxWidth={380} />
          </div>
        </div>
        <div className="ops-toolbar">
          <span className="hint ops-range-span">{dateLabel(from, zone)} {clock(from, zone)} – {dateLabel(to, zone)} {clock(to, zone)} {zoneName(to, zone)}{d ? ` · ${stepText(d.step)}` : ""}</span>
          <div className="grow" />
          {status?.lastRun && <span className="hint">Collector: last run {ago(status.lastRun)}{status.ok === false ? " · failed" : ""}</span>}
        </div>
        {st?.found && (
          <div className="ops-stats ab-stats-3">
            <Mini label="Shopper entries" value={num(row?.entries ?? 0)} note={`PDP, CART, COP and OC${st.health ? ` · ${num(st.health)} health checks left out` : ""}`} />
            <Mini label="Order confirmations (OC)" value={num(st.oc ?? 0)} note="orders in this range" />
            <Mini label="OC with a bad session ID" value={<BadPct v={st.ocBadPct} />} note={`${num(st.ocBad ?? 0)} orders`} title="Order confirmations whose session ID can't be put in an A/B group" />
          </div>
        )}
      </section>

      {q.isLoading ? <section className="card"><Loading what="Reading the session counts…" /></section>
        : q.error || !d ? <section className="card"><div className="card-body"><ErrorCallout error={q.error} /></div></section> : (
          <div className={`ab-stack ${q.isFetching ? "ops-fetching" : ""}`}>
            {d.error && <Callout tone="warn" title="Can't read the session counts">{d.error}</Callout>}
            {status?.ok === false && <Callout tone="warn" title="The collector's last run failed">{status.lastError} ({status.lastErrorAt ? ago(status.lastErrorAt) : ""}). The numbers stop at the last good run.</Callout>}
            {!d.windows.session && <Callout tone="neutral" title="No session counts in this range">scripts/ab_session_counts.py writes them every 5 minutes{d.countedFrom ? "" : "; nothing has been written yet"}.</Callout>}

            {st && !st.found && store && <Callout tone="neutral" title={`No entries for ${store} in this range`}>Pick another store or a longer range.</Callout>}
            {st?.found && (
              <>
                <h2 className="ab-h2">Session history index <span className="hint mono">{store}-session_history-*</span></h2>
                <div className="ops-charts">
                  {Object.entries(st.series ?? {}).map(([page, values]) => (
                    <Chart key={page} title={PAGE_NAME[page] ?? page} sub={`entries ${stepText(d.step)}`} start={d.start} step={d.step} zone={zone} nowAt={nowAt}
                      series={page === "OC" && st.badOc ? [
                        { key: "all", label: "All", color: PAGE_COLOR.OC, values, area: true },
                        ...(st.edd?.series.shown ? [{ key: "shown", label: "EDD shown", color: EDD_COLOR.shown, values: st.edd.series.shown }] : []),
                        ...(st.edd?.series.hidden ? [{ key: "hidden", label: "EDD hidden", color: EDD_COLOR.hidden, values: st.edd.series.hidden }] : []),
                        { key: "bad", label: "Bad session ID", color: "var(--st-5xx)", values: st.badOc },
                      ] : [{ key: "all", label: page, color: PAGE_COLOR[page] ?? "var(--accent)", values, area: true }]} />
                  ))}
                </div>
                <CheckTable title="Session ID check · session history" source="session" rows={st.sessionCheck ?? []} onOpen={openIds}
                  sub="Entries per page by session ID · a click on a count lists those entries with their session ID and order ID" />
                {(st.edd || st.conversion) && (
                  <EddSection edd={st.edd ?? null} conversion={st.conversion ?? null} store={store}
                    onOpen={(category, group) => openIds("session", "OC", category, "edd", group)} />
                )}
                {st.ua && <BrowserSection ua={st.ua} store={store} zone={zone} onOpen={(page, dim, value) => openIds("session", page, "bad", dim, value)} />}
              </>
            )}

            <section className="card" style={{ overflow: "hidden" }}>
              <CardHead title={`Stores · ${stores.length}`} sub="Every store with session entries in this range · click a column to sort, a row to show that store, an OC bad percentage to list those orders"
                right={<button type="button" className="btn btn-ghost btn-sm" onClick={exportStores} disabled={!stores.length}><Icon name="download" size={15} /> CSV</button>} />
              {stores.length === 0 ? <Empty title="No session entries in this range" /> : (
                <div className="table-scroll" style={{ maxHeight: "none" }}>
                  <table className="table ops-table">
                    <thead><tr>
                      <Th k="store">Store</Th><Th k="entries">Entries</Th><Th k="PDP">PDP</Th><Th k="CART">CART</Th><Th k="COP">COP</Th><Th k="OC">OC</Th>
                      <Th k="ocBadPct" title="Order confirmations with a bad session ID · click a percentage to list the orders">OC bad</Th>
                    </tr></thead>
                    <tbody>
                      {shownStores.map((s: AbStoreRow) => (
                        <tr key={s.store} className={`clickable ${store === s.store ? "selected" : ""}`} tabIndex={0} aria-selected={store === s.store}
                          onClick={() => set({ store: s.store, ids: null })} onKeyDown={(e) => { if (e.key === "Enter") set({ store: s.store, ids: null }); }}>
                          <td><div className="nm">{s.store}</div></td>
                          <td className="num" style={{ fontWeight: 600 }}>{num(s.entries)}</td>
                          {PAGES.map((p) => <td key={p} className="num">{s.pages[p] ? num(s.pages[p]) : <span className="dim">0</span>}</td>)}
                          <td className="num">
                            {s.ocBad ? (
                              <button type="button" className="ab-link" title={`List the ${num(s.ocBad)} orders with a bad session ID`}
                                onClick={(e) => { e.stopPropagation(); set({ store: s.store, ids: "session|OC|bad" }); }}
                                onKeyDown={(e) => e.stopPropagation()}><BadPct v={s.ocBadPct} n={s.ocBad} /></button>
                            ) : <BadPct v={s.ocBadPct} n={s.ocBad} />}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              {stores.length > 15 && (
                <div className="card-foot"><button type="button" className="btn btn-ghost btn-sm" onClick={() => setAll(!all)}>{all ? "Show the top 15" : `Show all ${stores.length} stores`}</button></div>
              )}
            </section>
          </div>
        )}
      {ids && store && (idSource === "session" || idSource === "cop") && (
        <IdDialog store={store} source={idSource} page={idPage} category={idCat} from={from} to={to} zone={zone} onClose={() => set({ ids: null })}
          dim={(["browser", "os", "brand", "app", "edd"] as string[]).includes(idDim) ? idDim as ListDim : undefined} value={idValue} />
      )}
    </Page>
  );
}

// ------------------------------------------------------------------ session ID tables
function CheckTable({ title, sub, source, rows, onOpen, empty }: {
  title: string; sub: string; source: AbSource; rows: AbPageRow[]; empty?: string;
  onOpen: (source: AbSource, page: string, category: string) => void;
}) {
  const cats = useMemo(() => {
    const seen = new Set<string>();
    for (const r of rows) for (const c of r.categories) seen.add(c.category);
    const order = ["valid", "NA", "ORDER_LOOKUP_CALL", "xyz123", "novalue", "empty", "missing", "numbers", "uuid", "other", "health"];
    return [...seen].sort((a, b) => (order.indexOf(a) < 0 ? 98 : order.indexOf(a)) - (order.indexOf(b) < 0 ? 98 : order.indexOf(b)));
  }, [rows]);
  return (
    <section className="card" style={{ overflow: "hidden" }}>
      <CardHead title={title} sub={sub} />
      {rows.length === 0 ? <Empty title={empty ?? "No entries in this range"} /> : (
        <div className="table-scroll" style={{ maxHeight: "none" }}>
          <table className="table ops-table ab-check">
            <thead><tr>
              <th>Page</th><th className="num">Entries</th>
              {cats.map((c) => <th key={c} className="num" title={CAT_TITLE[c]}>{catLabel(c)}</th>)}
              <th className="num" title="Share of entries (health checks left out) with a bad session ID">Bad</th>
            </tr></thead>
            <tbody>
              {rows.map((r) => {
                const can = listable(source, r.page);
                return (
                  <tr key={r.page}>
                    <td><div className="nm">{PAGE_NAME[r.page] ?? r.page}</div></td>
                    <td className="num" style={{ fontWeight: 600 }}>{num(r.shoppers)}</td>
                    {cats.map((c) => {
                      const x = r.categories.find((y) => y.category === c);
                      if (!x) return <td key={c} className="num"><span className="dim">0</span></td>;
                      const cell = <>{num(x.count)}{c !== "health" && <span className="ab-pc">{pct(x.pct)}</span>}</>;
                      return (
                        <td key={c} className={`num ${isBad(c) ? "ab-bad" : c === "valid" ? "ab-ok" : "dim"}`}>
                          {can && isBad(c)
                            ? <button type="button" className="ab-link" onClick={() => onOpen(source, r.page, c)} title={`List the ${r.page} entries with ${catLabel(c)}`}>{cell}</button>
                            : cell}
                        </td>
                      );
                    })}
                    <td className="num">
                      {can && r.bad ? <button type="button" className="ab-link" onClick={() => onOpen(source, r.page, "bad")} title="List every entry with a bad session ID"><BadPct v={r.badPct} n={r.bad} /></button> : <BadPct v={r.badPct} n={r.bad} />}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

// ------------------------------------------------------------------ order confirmations: EDD shown vs hidden
type ListDim = AbUaDim | "edd";
const EDD_LABEL: Record<AbEddName, string> = { shown: "EDD shown", hidden: "EDD hidden", none: "No EDD" };
const EDD_COLOR: Record<AbEddName, string> = { shown: "#2563eb", hidden: "#d97706", none: "var(--faint)" };
const usd = (v: number) => `$${v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const usdShort = (v: number) => (v >= 1e6 ? `$${(v / 1e6).toFixed(2)}M` : v >= 1e4 ? `$${(v / 1e3).toFixed(1)}k` : usd(v));
const pts = (v: number) => `${v > 0 ? "+" : v < 0 ? "−" : "±"}${Math.abs(v).toFixed(1)} pts`;

function EddSection({ edd, conversion, store, onOpen }: {
  edd: NonNullable<NonNullable<AbSummary["store"]>["edd"]> | null;
  conversion: NonNullable<NonNullable<AbSummary["store"]>["conversion"]> | null;
  store: string; onOpen: (category: string, group: AbEddName) => void;
}) {
  const main = (edd?.groups ?? []).filter((g) => g.group !== "none");
  return (
    <>
      <h2 className="ab-h2">Order confirmations: EDD shown vs hidden <span className="hint mono">{store}-session_history-* · pageType OC</span></h2>
      {edd && edd.groups.length > 0 ? (
        <div className="ab-edd-row">
          <section className="card" style={{ overflow: "hidden" }}>
            <CardHead title="Orders, cart total and shipping cost" sub="Hidden = the response hid the EDD (eddsHidden) · shown = an EDD was shown · amounts from billingInfo" />
            <div className="table-scroll" style={{ maxHeight: "none" }}>
              <table className="table ops-table">
                <thead><tr><th>Group</th><th className="num">Orders</th><th className="num">Share</th><th className="num">Cart total</th><th className="num">Avg cart</th>
                  <th className="num">Shipping cost</th><th className="num">Avg shipping</th></tr></thead>
                <tbody>
                  {edd.groups.map((g) => (
                    <tr key={g.group}>
                      <td className="nowrap"><span className="ab-dot" style={{ background: EDD_COLOR[g.group] }} />{EDD_LABEL[g.group]}</td>
                      <td className="num" style={{ fontWeight: 600 }}>{num(g.orders)}</td>
                      <td className="num">{g.share === null ? <span className="dim">–</span> : pct(g.share, 0)}</td>
                      <td className="num">{g.group === "none" ? <span className="dim">–</span> : usdShort(g.cart)}</td>
                      <td className="num">{g.group === "none" ? <span className="dim">–</span> : usd(g.avgCart)}</td>
                      <td className="num">{g.group === "none" ? <span className="dim">–</span> : usdShort(g.ship)}</td>
                      <td className="num">{g.group === "none" ? <span className="dim">–</span> : usd(g.avgShip)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
          <AvgBars groups={main} />
        </div>
      ) : <section className="card"><Empty title="No EDD split for this range yet">Counted from the collector's next run on (and by a backfill for past days).</Empty></section>}
      {edd && main.length > 0 && <EddBadTable groups={edd.groups} onOpen={onOpen} />}
      {conversion && conversion.groups.length > 0 && <ConversionCard conv={conversion} />}
    </>
  );
}

function AvgBars({ groups }: { groups: AbEddGroup[] }) {
  const metrics: [string, (g: AbEddGroup) => number][] = [["Avg cart", (g) => g.avgCart], ["Avg shipping", (g) => g.avgShip]];
  return (
    <section className="card">
      <CardHead title="Averages per order" sub="EDD shown vs hidden" />
      <div className="ab-avg">
        {metrics.map(([label, f]) => {
          const top = Math.max(0.01, ...groups.map(f));
          return (
            <div key={label} className="ab-avg-group">
              <div className="ab-avg-bars">
                {groups.map((g) => (
                  <div key={g.group} className="ab-avg-col">
                    <span className="v">{usd(f(g))}</span>
                    <span className="bar" style={{ height: `${(120 * f(g)) / top}px`, background: EDD_COLOR[g.group] }} title={`${EDD_LABEL[g.group]}: ${usd(f(g))}`} />
                  </div>
                ))}
              </div>
              <div className="ab-avg-label">{label}</div>
            </div>
          );
        })}
      </div>
      <div className="ab-ua-legend">{groups.map((g) => <span key={g.group}><i style={{ background: EDD_COLOR[g.group] }} />{EDD_LABEL[g.group]}</span>)}</div>
    </section>
  );
}

const EDD_BAD_COLOR: Record<string, string> = { NA: "#dc2626", ORDER_LOOKUP_CALL: "#f97316", xyz123: "#eab308", numbers: "#a855f7", uuid: "#0ea5e9" };
const VALID_COLOR = "color-mix(in srgb, var(--accent) 28%, var(--surface))";
const badColor = (c: string) => EDD_BAD_COLOR[c] ?? "#64748b";

function EddBadTable({ groups, onOpen }: { groups: AbEddGroup[]; onOpen: (category: string, group: AbEddName) => void }) {
  const cats = useMemo(() => {
    const seen = new Set<string>();
    for (const g of groups) for (const c of g.categories) if (c.category !== "valid") seen.add(c.category);
    const order = ["NA", "ORDER_LOOKUP_CALL", "xyz123", "novalue", "empty", "missing", "numbers", "uuid", "other"];
    return [...seen].sort((a, b) => (order.indexOf(a) < 0 ? 98 : order.indexOf(a)) - (order.indexOf(b) < 0 ? 98 : order.indexOf(b)));
  }, [groups]);
  return (
    <section className="card" style={{ overflow: "hidden" }}>
      <CardHead title="Bad session IDs by EDD group" sub="Order confirmations per group by session ID kind · a click on a count lists those orders with session ID, order ID, cart total and shipping cost" />
      <div className="table-scroll" style={{ maxHeight: "none" }}>
        <table className="table ops-table ab-check">
          <thead><tr><th>Group</th><th className="num">Orders</th><th aria-hidden="true" /><th className="num"><i className="ab-sw" style={{ background: VALID_COLOR }} />Valid (M…)</th>
            {cats.map((c) => <th key={c} className="num" title={CAT_TITLE[c]}><i className="ab-sw" style={{ background: badColor(c) }} />{catLabel(c)}</th>)}<th className="num">Bad %</th></tr></thead>
          <tbody>
            {groups.map((g) => {
              const by = Object.fromEntries(g.categories.map((c) => [c.category, c]));
              return (
                <tr key={g.group}>
                  <td className="nowrap"><span className="ab-dot" style={{ background: EDD_COLOR[g.group] }} />{EDD_LABEL[g.group]}</td>
                  <td className="num" style={{ fontWeight: 600 }}>{num(g.orders)}</td>
                  <td aria-hidden="true">
                    <div className="ab-stack-bar">
                      <span style={{ width: `${(100 * g.valid) / Math.max(1, g.orders)}%`, background: VALID_COLOR }}
                        title={`Valid (starts with M): ${num(g.valid)} · ${pct(by.valid?.pct)}`} />
                      {cats.map((c) => by[c] ? <span key={c} style={{ width: `${(100 * by[c].count) / Math.max(1, g.orders)}%`, background: badColor(c) }}
                        title={`${catLabel(c)}: ${num(by[c].count)} · ${pct(by[c].pct)}`} /> : null)}
                    </div>
                  </td>
                  <td className="num">{num(g.valid)}<span className="ab-pc">{pct(by.valid?.pct)}</span></td>
                  {cats.map((c) => (
                    <td key={c} className="num">{by[c]
                      ? <button type="button" className="ab-link" onClick={() => onOpen(c, g.group)} title={`List the ${EDD_LABEL[g.group]} orders with ${catLabel(c)}`}>{num(by[c].count)}<span className="ab-pc">{pct(by[c].pct)}</span></button>
                      : <span className="dim">0</span>}</td>
                  ))}
                  <td className="num">{g.bad ? <button type="button" className="ab-link" onClick={() => onOpen("bad", g.group)}><BadPct v={g.badPct} n={g.bad} /></button> : <BadPct v={g.badPct} n={g.bad} />}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <div className="ab-ua-legend ab-edd-legend">
        <span><i style={{ background: VALID_COLOR }} />Valid (starts with M)</span>
        {cats.map((c) => <span key={c} title={CAT_TITLE[c]}><i style={{ background: badColor(c) }} />{catLabel(c)}</span>)}
      </div>
    </section>
  );
}

function ConversionCard({ conv }: { conv: NonNullable<NonNullable<AbSummary["store"]>["conversion"]> }) {
  const shown = conv.groups.find((g) => g.group === "shown");
  const hidden = conv.groups.find((g) => g.group === "hidden");
  const steps: [string, keyof AbConvGroup, keyof AbConvGroup, keyof AbConvGroup][] = [
    ["PDP → COP", "pdpToCop", "pdp", "cop"], ["COP → OC", "copToOc", "cop", "oc"], ["PDP → OC (overall)", "pdpToOc", "pdp", "oc"]];
  const utcDay = (ms: number) => new Date(ms).toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" });
  const days = conv.fromDay && conv.toDay ? `${utcDay(conv.fromDay)} – ${utcDay(conv.toDay - 1)}` : "";
  return (
    <section className="card" style={{ overflow: "hidden" }}>
      <CardHead title="Conversion: EDD shown vs hidden" sub={`Unique valid session IDs per page · whole UTC days ${days} · session IDs that aren't valid can't be followed and are left out`} />
      <div className="ab-ua-legend">{(["shown", "hidden"] as AbEddName[]).map((g) => <span key={g}><i style={{ background: EDD_COLOR[g] }} />{EDD_LABEL[g]}</span>)}</div>
      {steps.map(([label, rate, from, to]) => {
        const rs = [shown, hidden].filter((g): g is AbConvGroup => !!g);
        const top = Math.max(0.01, ...rs.map((g) => (g[rate] as number | null) ?? 0)) * 1.12;
        const a = shown?.[rate] as number | null | undefined, b = hidden?.[rate] as number | null | undefined;
        const diff = a !== null && a !== undefined && b !== null && b !== undefined ? a - b : null;
        return (
          <div key={label} className="ab-conv-step">
            <div className="ab-conv-label">{label}</div>
            <div>
              {rs.map((g) => {
                const r = g[rate] as number | null;
                return (
                  <div key={g.group} className="ab-conv-bar">
                    <span className="bar" style={{ width: `${(70 * (r ?? 0)) / top}%`, background: EDD_COLOR[g.group] }} />
                    <b>{pct(r)}</b><span className="hint">{num(g[to] as number)} of {num(g[from] as number)} sessions</span>
                  </div>
                );
              })}
            </div>
            <div className="ab-conv-diff">{diff === null ? <span className="dim">—</span> : <><b className={diff >= 0 ? "ok" : "bad"}>{pts(diff)}</b><span className="hint">shown vs hidden</span></>}</div>
          </div>
        );
      })}
    </section>
  );
}

// ------------------------------------------------------------------ browser / device of the shoppers
const UA_TITLE: Record<AbUaDim, string> = { browser: "Browser", os: "OS", brand: "Device brand", app: "In-app browser" };
const UA_SUB: Record<AbUaDim, string> = {
  browser: "Top 10 by entries, the rest in Other", os: "Operating system of the shopper",
  brand: "Device maker (the full device name is in the list)", app: "Shoppers inside an app (webview) vs a normal browser",
};
/** A browser / device value as shown, and a tag for the ones that are not shoppers. */
function uaLabel(dim: AbUaDim | string, v: string): { text: string; tag?: string } {
  if (dim === "edd") return { text: EDD_LABEL[v as AbEddName] ?? v };
  if (v === "_other") return { text: "Other" };
  if (v === "(unknown)" || v === "") return { text: "Unknown", tag: "no browser data" };
  if (dim === "browser" && /^java$/i.test(v)) return { text: "Server call (Java)", tag: "not a shopper" };
  if (dim === "os" && /^cloud$/i.test(v)) return { text: "Cloud (server)", tag: "not a shopper" };
  if (dim === "app" && /^unknown$/i.test(v)) return { text: "None (normal browser)" };
  if (dim === "brand" && /^unknown$/i.test(v)) return { text: "Unknown / PC" };
  return { text: v };
}

function BrowserSection({ ua, store, zone, onOpen }: {
  ua: NonNullable<NonNullable<AbSummary["store"]>["ua"]>; store: string; zone: ClockZone;
  onOpen: (page: string, dim: AbUaDim, value: string) => void;
}) {
  const pages = Object.keys(ua.pages);
  // the page with the most bad session IDs first (usually COP or OC)
  const badOf = (p: string) => (ua.pages[p]?.browser ?? []).reduce((t, x) => t + x.bad, 0);
  const best = [...pages].sort((a, b) => badOf(b) - badOf(a))[0];
  const [pick, setPick] = useState<string | null>(null);
  const page = pick && pages.includes(pick) ? pick : best;
  return (
    <>
      <h2 className="ab-h2">Bad session IDs by browser <span className="hint mono">{store}-session_history-*{ua.from ? ` · counted per hour, ${dateLabel(ua.from, zone)} ${clock(ua.from, zone)} – ${ua.to ? `${dateLabel(ua.to, zone)} ${clock(ua.to, zone)}` : ""}` : ""}</span></h2>
      <section className="card">
        <CardHead title="Which browsers lose the session ID" sub="Entries and bad session IDs per browser for the selected page · click a bad count to list those entries"
          right={pages.length > 0 && (
            <div className="tabs" role="group" aria-label="Page">
              {PAGES.filter((p) => pages.includes(p)).concat(pages.filter((p) => !PAGES.includes(p))).map((p) => (
                <button key={p} type="button" className="tab" aria-pressed={page === p} aria-selected={page === p} onClick={() => setPick(p)}>{p}</button>
              ))}
            </div>
          )} />
        {!pages.length && <Empty title="No browser counts in this range yet">They are counted per hour from the collector's next run on.</Empty>}
      </section>
      {page && (
        <div className="ab-ua-grid">
          {(["browser", "os", "brand", "app"] as AbUaDim[]).map((dim) => (
            <UaCard key={dim} dim={dim} items={ua.pages[page]?.[dim] ?? []} onOpen={(v) => onOpen(page, dim, v)} />
          ))}
        </div>
      )}
    </>
  );
}

function UaCard({ dim, items, onOpen }: { dim: AbUaDim; items: AbUaItem[]; onOpen: (value: string) => void }) {
  const top = Math.max(1, ...items.map((x) => x.entries));
  return (
    <section className="card" style={{ overflow: "hidden" }}>
      <CardHead title={`Bad session IDs by ${dim === "os" ? "OS" : UA_TITLE[dim].toLowerCase()}`} sub={UA_SUB[dim]} />
      <div className="ab-ua-legend" aria-hidden="true"><span><i className="e" />Entries</span><span><i className="b" />Bad session ID</span></div>
      {items.length === 0 ? <Empty title="No entries" /> : (
        <table className="table ops-table ab-ua">
          <thead><tr><th>{UA_TITLE[dim]}</th><th aria-hidden="true" /><th className="num">Entries</th><th className="num">Bad</th><th className="num">Bad %</th></tr></thead>
          <tbody>
            {items.map((x) => {
              const l = uaLabel(dim, x.value);
              const w = (100 * x.entries) / top;
              const can = x.bad > 0 && x.value !== "_other";
              const open = () => onOpen(x.value);
              return (
                <tr key={x.value}>
                  <td><div className="nm">{l.text}{l.tag && <span className="ab-tag">{l.tag}</span>}</div></td>
                  <td className="ab-ua-barcell" aria-hidden="true">
                    <div className="ab-ua-bar"><span style={{ width: `${w}%` }} /><b style={{ width: `${x.entries ? (w * x.bad) / x.entries : 0}%` }} /></div>
                  </td>
                  <td className="num">{num(x.entries)}</td>
                  <td className="num">{can ? <button type="button" className="ab-link" onClick={open} title={`List the ${num(x.bad)} entries with a bad session ID`}>{num(x.bad)}</button> : num(x.bad)}</td>
                  <td className="num">{can ? <button type="button" className="ab-link" onClick={open}><BadPct v={x.pct} /></button> : <BadPct v={x.pct} />}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </section>
  );
}

// ------------------------------------------------------------------ the list behind a count
const SHOW = 500;                                   // rows read and drawn in the dialog
const ALL_ROWS = 500_000;                           // Copy order IDs / CSV read every row (up to this)

function IdDialog({ store, source, page, category, from, to, zone, onClose, dim, value }: {
  store: string; source: AbSource; page: string; category: string; from: number; to: number; zone: ClockZone; onClose: () => void;
  dim?: ListDim; value?: string;
}) {
  const toast = useToast();
  const q = useQuery({
    queryKey: ["abtest-ids", store, source, page, category, from, to, dim, value],
    queryFn: () => get<AbIds>("/abtest/ids", { store, source, page, category, from, to, limit: SHOW, dim, value: dim ? value : undefined }),
    retry: false, staleTime: 60_000,
  });
  const [busy, setBusy] = useState(false);
  /** Every row (the dialog only reads the newest SHOW). */
  const everything = async (): Promise<AbIds["rows"] | null> => {
    if (!d) return null;
    if (!d.truncated) return d.rows;
    setBusy(true);
    try { return (await get<AbIds>("/abtest/ids", { store, source, page, category, from: d.from, to: d.to, limit: ALL_ROWS, dim, value: dim ? value : undefined })).rows; }
    catch { toast?.("Couldn't read the full list"); return null; }
    finally { setBusy(false); }
  };
  const d = q.data;
  const what = category === "bad" ? "a bad session ID" : catLabel(category);
  const by = dim === "edd" && value ? ` · ${EDD_LABEL[value as AbEddName] ?? value}`
    : dim && value !== undefined ? ` · ${UA_TITLE[dim as AbUaDim]}: ${uaLabel(dim, value).text}` : "";
  const money = page === "OC" && source === "session";
  const csv = async () => {
    const rows = await everything();
    if (!rows) return;
    const esc = (v: string) => (/[",\n]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v);
    const lines = ["time,store,source,page,category,session_id,order_id,doc_id,browser,os,device,brand,in_app,edd,cart_total,shipping_cost",
      ...rows.map((r) => [localStamp(Date.parse(r.at), zone), store, source, r.page, r.category, r.sessionId, r.orderId, r.docId,
        r.browser ?? "", r.os ?? "", r.device ?? "", r.brand ?? "", r.app ?? "", r.edd ?? "",
        r.cart === null || r.cart === undefined ? "" : r.cart.toFixed(2), r.ship === null || r.ship === undefined ? "" : r.ship.toFixed(2)].map(esc).join(","))];
    downloadFile(`abtest-${store}-${source}-${page}-${category}-${stamp(from, zone)}-${stamp(to, zone)}.csv`, lines.join("\n") + "\n", "text/csv");
  };
  const copyOrders = async () => {
    const rows = await everything();
    if (!rows) return;
    const ids = [...new Set(rows.map((r) => r.orderId).filter(Boolean))];
    if (!ids.length) { toast?.("These entries have no order ID"); return; }
    await copyText(ids.join("\n"));
    toast?.(`${num(ids.length)} order IDs copied`);
  };
  return (
    <Dialog wide title={`${page} entries with ${what}${by}`} onClose={onClose}
      subtitle={`${store} · ${source === "session" ? "session_history" : "cop-derrh (COP DI)"} · ${dateLabel(from, zone)} ${clock(from, zone)} – ${dateLabel(to, zone)} ${clock(to, zone)} ${zoneName(to, zone)}`}
      footer={<>
        <button type="button" className="btn" onClick={copyOrders} disabled={!d?.rows.length || busy}><Icon name="copy" size={15} /> {busy ? "Reading…" : d?.truncated ? `Copy all ${num(d.total)} order IDs` : "Copy order IDs"}</button>
        <button type="button" className="btn" onClick={csv} disabled={!d?.rows.length || busy}><Icon name="download" size={15} /> CSV</button>
        <button type="button" className="btn btn-primary" onClick={onClose}>Close</button>
      </>}>
      {q.isLoading ? <Loading what="Reading the list…" /> : q.error || !d ? <ErrorCallout error={q.error} /> : d.rows.length === 0 ? (
        <Empty title="No entries listed">The list starts when the collector started writing it; counts from before can't be listed.</Empty>
      ) : (
        <>
          {d.mix && (d.mix.browser.length > 0 || d.mix.os.length > 0) && (
            <div className="ab-chips">
              {d.mix.browser.slice(0, 3).map((m, i) => <span key={`b${m.value}`} className="ab-chip">{i === 0 ? "Browser: " : ""}{uaLabel("browser", m.value).text} <b>{pct(m.pct, 0)}</b></span>)}
              {d.mix.os.slice(0, 2).map((m, i) => <span key={`o${m.value}`} className="ab-chip">{i === 0 ? "OS: " : ""}{uaLabel("os", m.value).text} <b>{pct(m.pct, 0)}</b></span>)}
            </div>
          )}
          <p className="hint" style={{ margin: "0 0 10px" }}>
            {num(d.total)} entries{d.truncated ? `; the newest ${num(d.rows.length)} are shown below. Copy order IDs and CSV have all of them.` : "."}
            {d.values.length > 1 && d.values.length <= 10 && <> Values: {d.values.slice(0, 8).map((v) => `${v.value || "(empty)"} × ${num(v.count)}`).join(" · ")}{d.values.length > 8 ? " …" : ""}</>}
          </p>
          <div className="table-scroll ab-ids" style={{ maxHeight: 460 }}>
            <table className="table ops-table">
              <thead><tr><th>Time</th><th>Order ID</th><th>Session ID</th>{money && <><th>EDD</th><th className="num">Cart total</th><th className="num">Shipping</th></>}<th>Browser</th><th>OS</th><th>Device</th><th>In-app</th></tr></thead>
              <tbody>
                {d.rows.map((r, i) => {
                  const t = Date.parse(r.at);
                  return (
                    <tr key={`${r.docId}${i}`}>
                      <td className="nowrap">{clock(t, zone)} {zoneName(t, zone)}<span className="hint"> · {dateLabel(t, zone)}</span></td>
                      <td className="mono">{r.orderId || <span className="dim">—</span>}</td>
                      <td className="mono ab-sid" title={r.sessionId}>{r.sessionId === "" ? <span className="dim">(empty)</span> : r.sessionId}
                        {r.sessionId !== r.category && <span className="hint"> · {catLabel(r.category)}</span>}</td>
                      {money && <>
                        <td>{r.edd ? <span className="ab-edd-tag" data-g={r.edd}>{EDD_LABEL[r.edd as AbEddName] ?? r.edd}</span> : <span className="dim">—</span>}</td>
                        <td className="num">{r.cart === null || r.cart === undefined ? <span className="dim">—</span> : usd(r.cart)}</td>
                        <td className="num">{r.ship === null || r.ship === undefined ? <span className="dim">—</span> : usd(r.ship)}</td>
                      </>}
                      <td>{r.browser ? uaLabel("browser", r.browser).text : <span className="dim">—</span>}</td>
                      <td>{r.os || <span className="dim">—</span>}</td>
                      <td>{r.device || r.brand || <span className="dim">—</span>}</td>
                      <td>{r.app && !/^unknown$/i.test(r.app) ? r.app : <span className="dim">—</span>}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </>
      )}
    </Dialog>
  );
}
