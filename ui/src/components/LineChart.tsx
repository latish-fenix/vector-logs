import { useEffect, useMemo, useRef, useState } from "react";
// Times are shown in IST, PST or EST (the page's switch). IST has no daylight saving (UTC + 5:30);
// PST and EST turn into PDT and EDT in summer, so the offset is looked up for each time.
export type ClockZone = "ist" | "pst" | "est";
export const ZONES: ClockZone[] = ["ist", "pst", "est"];
const TZ: Record<ClockZone, string> = { ist: "Asia/Kolkata", pst: "America/Los_Angeles", est: "America/New_York" };
/** Label on the switch. */
export const ZONE_LABEL: Record<ClockZone, string> = { ist: "IST", pst: "PST", est: "EST" };
export const ZONE_TITLE: Record<ClockZone, string> = {
  ist: "India time (UTC + 5:30)",
  pst: "US Pacific time (PST, PDT in summer)",
  est: "US Eastern time (EST, EDT in summer)",
};
const pad2 = (n: number) => String(n).padStart(2, "0");
const fmts = new Map<ClockZone, Intl.DateTimeFormat>();
function parts(ms: number, zone: ClockZone) {
  let f = fmts.get(zone);
  if (!f) {
    f = new Intl.DateTimeFormat("en-US", { timeZone: TZ[zone], hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
    fmts.set(zone, f);
  }
  const o: Record<string, number> = {};
  for (const p of f.formatToParts(new Date(ms))) if (p.type !== "literal") o[p.type] = Number(p.value);
  return { y: o.year, mo: o.month, d: o.day, h: o.hour % 24, mi: o.minute };
}
/** Local time minus UTC, in ms (e.g. −8 h for PST). */
export function offsetMs(ms: number, zone: ClockZone): number {
  if (zone === "ist") return 330 * 60_000;
  const p = parts(ms, zone);
  return Date.UTC(p.y, p.mo - 1, p.d, p.h, p.mi) - Math.floor(ms / 60_000) * 60_000;
}
/** "IST", "PST" / "PDT", "EST" / "EDT" at that moment. */
export function zoneName(ms: number, zone: ClockZone): string {
  if (zone === "ist") return "IST";
  const h = offsetMs(ms, zone) / 3600_000;
  if (zone === "pst") return h === -7 ? "PDT" : "PST";
  return h === -4 ? "EDT" : "EST";
}
/** "17:35" in the zone. */
export function clock(ms: number, zone: ClockZone): string {
  const p = parts(ms, zone);
  return `${pad2(p.h)}:${pad2(p.mi)}`;
}
/** "17:35 IST" (with the other zones after it when `both`). */
export function clockZ(ms: number | null | undefined, zone: ClockZone, both = false): string {
  if (ms === null || ms === undefined) return "—";
  const others = both ? ZONES.filter((z) => z !== zone).map((z) => ` · ${clock(ms, z)} ${zoneName(ms, z)}`).join("") : "";
  return `${clock(ms, zone)} ${zoneName(ms, zone)}${others}`;
}
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
/** "11 Nov" in the zone. */
export function dateLabel(ms: number, zone: ClockZone): string {
  const p = parts(ms, zone);
  return `${p.d} ${MONTHS[p.mo - 1]}`;
}
/** Today's date in the zone, "2026-11-11". */
export function dayIn(zone: ClockZone, ms = Date.now()): string {
  const p = parts(ms, zone);
  return `${p.y}-${pad2(p.mo)}-${pad2(p.d)}`;
}
/** "2026-11-11T00:05-08:00": a minute in the zone with its offset (for CSV files). */
export function localStamp(ms: number, zone: ClockZone): string {
  const p = parts(ms, zone);
  const off = offsetMs(ms, zone) / 60_000;
  const a = Math.abs(off);
  return `${p.y}-${pad2(p.mo)}-${pad2(p.d)}T${pad2(p.h)}:${pad2(p.mi)}${off < 0 ? "-" : "+"}${pad2(Math.floor(a / 60))}:${pad2(a % 60)}`;
}

// Line chart for per-minute series, drawn in SVG like the other charts. Values are grouped
// into buckets (sum for counts, average for latencies) or added up (running totals) before drawing.

export interface LineSeries {
  key: string;
  label: string;
  color: string;
  values: (number | null)[];
  dashed?: boolean;
  /** Fill under the line (the main series). */
  area?: boolean;
}

/** Group per-minute values into buckets of `size` minutes. Sums: a bucket with an empty minute
 *  (today's minutes not there yet) is left out, so the line does not drop at the end. Averages: the
 *  minutes that have a value; a bucket with none is null. */
export function bucket(values: (number | null)[] | null | undefined, size: number, mode: "sum" | "avg" = "sum"): (number | null)[] {
  if (!values) return [];
  const out: (number | null)[] = [];
  for (let i = 0; i < values.length; i += size) {
    let s = 0, n = 0, gaps = 0;
    for (let j = i; j < Math.min(i + size, values.length); j++) {
      const v = values[j];
      if (v !== null && v !== undefined) { s += v; n++; } else gaps++;
    }
    out.push(!n || (mode === "sum" && gaps) ? null : mode === "sum" ? s : s / n);
  }
  return out;
}

/** Running total; stays null where the values are null (today's minutes not there yet). */
export function cumulative(values: (number | null)[] | null | undefined): (number | null)[] {
  if (!values) return [];
  let s = 0;
  return values.map((v) => (v === null || v === undefined ? null : (s += v)));
}

export function useWidth<T extends HTMLElement>(min = 200) {
  const ref = useRef<T>(null);
  const [w, setW] = useState(800);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => setW(Math.max(min, Math.floor(e.contentRect.width))));
    ro.observe(el);
    return () => ro.disconnect();
  }, [min]);
  return [ref, w] as const;
}

/** A round step so that about `count` steps cover `max` (1, 2, 2.5, 5 × 10ⁿ). */
function niceStep(max: number, count: number): number {
  const raw = Math.max(max, 1) / count;
  const pw = 10 ** Math.floor(Math.log10(raw));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * pw >= raw) return m * pw;
  return 10 * pw;
}

const TICK_STEPS = [5, 10, 15, 30, 60, 120, 180, 240, 360, 720, 1440, 2880, 4320, 10080].map((m) => m * 60_000);

export function Legend({ series }: { series: Pick<LineSeries, "key" | "label" | "color" | "dashed">[] }) {
  return (
    <div className="ops-legend" aria-hidden="true">
      {series.map((s) => (
        <span key={s.key}>
          <svg width="18" height="8"><line x1="0" x2="18" y1="4" y2="4" stroke={s.color} strokeWidth="2.5" strokeDasharray={s.dashed ? "4 3" : undefined} /></svg>
          {s.label}
        </span>
      ))}
    </div>
  );
}

export function LineChart({ start, step, series, height = 190, fmt, empty, label, legend = true, nowAt, zone = "ist" }: {
  start: number;
  /** ms per point */
  step: number;
  series: LineSeries[];
  height?: number;
  fmt: (v: number) => string;
  empty?: string;
  label: string;
  /** Draw the legend above the plot (off when the card shows it). */
  legend?: boolean;
  /** Mark this time (ms) with a dashed "now" line. */
  nowAt?: number | null;
  /** Time zone of the labels and the tooltip. */
  zone?: ClockZone;
}) {
  const [ref, width] = useWidth<HTMLDivElement>(260);
  const [hover, setHover] = useState<number | null>(null);
  const n = Math.max(0, ...series.map((s) => s.values.length));
  const top = useMemo(() => Math.max(0, ...series.flatMap((s) => s.values.filter((v): v is number => v !== null))), [series]);
  const any = series.some((s) => s.values.some((v) => v !== null && v !== 0));
  const tickStep = Math.max(1, niceStep(top, 4));          // whole numbers: no "1, 1, 1" axis for small counts
  const max = Math.max(tickStep, Math.ceil(top / tickStep) * tickStep);
  const yTicks = Array.from({ length: Math.round(max / tickStep) + 1 }, (_, i) => i * tickStep);
  const labelW = Math.max(30, ...yTicks.map((t) => fmt(t).length * 6.4 + 8));
  const PAD = { l: Math.ceil(labelW) + 6, r: 12, t: 10, b: 24 };
  const plotW = width - PAD.l - PAD.r;
  const plotH = height - PAD.t - PAD.b;
  const span = Math.max(1, (n - 1) * step);
  const xAt = (ms: number) => PAD.l + ((ms - start) / span) * plotW;
  const x = (i: number) => PAD.l + (n <= 1 ? plotW / 2 : (i / (n - 1)) * plotW);
  const y = (v: number) => PAD.t + plotH - (v / max) * plotH;
  const path = (vals: (number | null)[]) => {
    let d = "", pen = false;
    vals.forEach((v, i) => {
      if (v === null) { pen = false; return; }
      d += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`;
      pen = true;
    });
    return d;
  };
  const areaPath = (vals: (number | null)[]) => {
    const idx = vals.map((v, i) => (v === null ? -1 : i)).filter((i) => i >= 0);
    if (idx.length < 2) return "";
    return `${path(vals)}L${x(idx[idx.length - 1]).toFixed(1)},${y(0)}L${x(idx[0]).toFixed(1)},${y(0)}Z`;
  };
  // x labels on round times (every 3 h for a day), at most one per ~72 px
  const fullSpan = n * step;
  const xStep = TICK_STEPS.find((s) => fullSpan / s <= Math.max(2, plotW / 72)) ?? TICK_STEPS[TICK_STEPS.length - 1];
  const xTicks: number[] = [];
  const off = offsetMs(start, zone);
  for (let t = Math.ceil((start + off) / xStep) * xStep - off; t <= start + span; t += xStep) xTicks.push(t);
  const multiDay = fullSpan > 26 * 3600_000;
  const tickLabel = (t: number) => (xStep >= 86_400_000 || (multiDay && clock(t, zone) === "00:00") ? dateLabel(t, zone) : clock(t, zone));
  const hoverLabel = (t: number) => `${multiDay ? `${dateLabel(t, zone)} ` : ""}${clock(t, zone)}`;
  const gid = useMemo(() => `g${Math.random().toString(36).slice(2, 8)}`, []);

  return (
    <div className="histo" ref={ref}>
      {legend && (
        <div className="histo-legend" style={{ paddingLeft: PAD.l }}>
          {series.map((s) => (
            <span key={s.key} className="histo-key">
              <svg width="18" height="8" aria-hidden="true"><line x1="0" x2="18" y1="4" y2="4" stroke={s.color} strokeWidth="2.5" strokeDasharray={s.dashed ? "4 3" : undefined} /></svg>
              {s.label}
            </span>
          ))}
          {!any && <span className="hint">{empty ?? "No data"}</span>}
        </div>
      )}
      <svg width={width} height={height} role="img" aria-label={label} onMouseLeave={() => setHover(null)}
        onMouseMove={(e) => {
          const r = (e.currentTarget as SVGSVGElement).getBoundingClientRect();
          const px = e.clientX - r.left;
          if (px < PAD.l - 4 || px > width - PAD.r + 4 || n === 0) { setHover(null); return; }
          setHover(Math.max(0, Math.min(n - 1, Math.round(((px - PAD.l) / Math.max(1, plotW)) * (n - 1)))));
        }}>
        <defs>
          {series.filter((s) => s.area).map((s) => (
            <linearGradient key={s.key} id={`${gid}-${s.key}`} x1="0" x2="0" y1="0" y2="1">
              <stop offset="0%" stopColor={s.color} stopOpacity={0.22} />
              <stop offset="100%" stopColor={s.color} stopOpacity={0.02} />
            </linearGradient>
          ))}
        </defs>
        {yTicks.map((t) => (
          <g key={t}>
            {t > 0 && <line x1={PAD.l} x2={width - PAD.r} y1={y(t)} y2={y(t)} className="histo-grid" />}
            <text x={PAD.l - 8} y={y(t) + 4} textAnchor="end" className="histo-axis">{fmt(t)}</text>
          </g>
        ))}
        {series.filter((s) => s.area).map((s) => <path key={`a-${s.key}`} d={areaPath(s.values)} fill={`url(#${gid}-${s.key})`} />)}
        {series.map((s) => (
          <path key={s.key} d={path(s.values)} fill="none" stroke={s.color} strokeWidth={s.dashed ? 1.5 : 2}
            strokeDasharray={s.dashed ? "4 3" : undefined} strokeLinejoin="round" strokeLinecap="round" opacity={s.dashed ? 0.85 : 1} />
        ))}
        <line x1={PAD.l} x2={width - PAD.r} y1={y(0)} y2={y(0)} className="histo-base" />
        {nowAt !== null && nowAt !== undefined && nowAt > start && nowAt < start + span && (
          <g>
            <line x1={xAt(nowAt)} x2={xAt(nowAt)} y1={PAD.t} y2={y(0)} className="ops-now-line" />
            <text x={xAt(nowAt) + 4} y={PAD.t + 9} className="ops-now-label">now</text>
          </g>
        )}
        {xTicks.map((t) => (
          <text key={t} x={xAt(t)} y={height - 6} textAnchor="middle" className="histo-axis">{tickLabel(t)}</text>
        ))}
        {hover !== null && (
          <g>
            <line x1={x(hover)} x2={x(hover)} y1={PAD.t} y2={y(0)} stroke="var(--border-strong)" />
            {series.map((s) => (s.values[hover] !== null && s.values[hover] !== undefined
              ? <circle key={s.key} cx={x(hover)} cy={y(s.values[hover]!)} r={3.5} fill={s.color} stroke="var(--surface)" strokeWidth={1.5} />
              : null))}
          </g>
        )}
        {!any && !legend && <text x={PAD.l + plotW / 2} y={PAD.t + plotH / 2} textAnchor="middle" className="histo-axis">{empty ?? "No data"}</text>}
      </svg>
      {hover !== null && (
        <div className="histo-tip" style={{ left: Math.min(Math.max(x(hover), 110), width - 110), top: legend ? 34 : 8 }}>
          <div className="t">
            {hoverLabel(start + hover * step)}{step > 60_000 ? `–${clock(start + (hover + 1) * step, zone)}` : ""} {zoneName(start + hover * step, zone)}
            <span style={{ opacity: 0.7 }}>{ZONES.filter((z) => z !== zone).map((z) => ` · ${clock(start + hover * step, z)} ${zoneName(start + hover * step, z)}`).join("")}</span>
          </div>
          {series.map((s) => (
            <div key={s.key} className="row" style={{ justifyContent: "space-between", gap: 16 }}>
              <span className="row" style={{ gap: 6 }}><span className="swatch" style={{ background: s.color }} />{s.label}</span>
              <strong>{s.values[hover] === null || s.values[hover] === undefined ? "—" : fmt(s.values[hover]!)}</strong>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/** A tiny today-vs-last-week line for table rows. */
export function Spark({ now, lastWeek, width = 112, height = 26, color = "var(--accent)" }: {
  now: (number | null)[] | null | undefined; lastWeek?: (number | null)[] | null; width?: number; height?: number; color?: string;
}) {
  const a = now ?? [];
  const b = lastWeek ?? [];
  const n = Math.max(a.length, b.length);
  const max = Math.max(1, ...a.filter((v): v is number => v !== null), ...b.filter((v): v is number => v !== null));
  const x = (i: number) => 1 + (n <= 1 ? 0 : (i / (n - 1)) * (width - 4));
  const y = (v: number) => height - 2 - (v / max) * (height - 5);
  const path = (vals: (number | null)[]) => {
    let d = "", pen = false;
    vals.forEach((v, i) => {
      if (v === null || v === undefined) { pen = false; return; }
      d += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`;
      pen = true;
    });
    return d;
  };
  let last = -1;
  a.forEach((v, i) => { if (v !== null && v !== undefined) last = i; });
  return (
    <svg className="ops-spark" width={width} height={height} aria-hidden="true">
      {b.length > 0 && <path d={path(b)} fill="none" stroke="var(--faint)" strokeWidth={1.2} strokeDasharray="3 2" opacity={0.7} />}
      <path d={path(a)} fill="none" stroke={color} strokeWidth={1.6} strokeLinejoin="round" strokeLinecap="round" />
      {last >= 0 && <circle cx={x(last)} cy={y(a[last]!)} r={2.2} fill={color} />}
    </svg>
  );
}
