import { useEffect, useMemo, useRef, useState } from "react";
import { fmtTime } from "../format";

// Line chart for per-minute series (UTC), drawn in SVG like the other charts. Values are grouped
// into buckets (sum for counts, calls-weighted or plain average for latencies) before drawing.

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

function useWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [w, setW] = useState(800);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => setW(Math.max(280, Math.floor(e.contentRect.width))));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, w] as const;
}

function niceMax(v: number): number {
  if (v <= 4) return 4;
  const pw = 10 ** Math.floor(Math.log10(v));
  for (const m of [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) if (m * pw >= v) return m * pw;
  return 10 * pw;
}

const PAD = { l: 56, r: 10, t: 8, b: 22 };

export function LineChart({ start, step, series, height = 190, fmt, empty, label }: {
  start: number;
  /** ms per point */
  step: number;
  series: LineSeries[];
  height?: number;
  fmt: (v: number) => string;
  empty?: string;
  label: string;
}) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  const n = Math.max(0, ...series.map((s) => s.values.length));
  const max = useMemo(() => niceMax(Math.max(1, ...series.flatMap((s) => s.values.filter((v): v is number => v !== null)))), [series]);
  const any = series.some((s) => s.values.some((v) => v !== null && v !== 0));
  const plotW = width - PAD.l - PAD.r;
  const plotH = height - PAD.t - PAD.b;
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
  const tickEvery = Math.max(1, Math.ceil(n / Math.max(2, Math.floor(plotW / 70))));
  const span = n * step;

  return (
    <div className="histo" ref={ref}>
      <div className="histo-legend" style={{ paddingLeft: PAD.l }}>
        {series.map((s) => (
          <span key={s.key} className="histo-key">
            <svg width="18" height="8" aria-hidden="true"><line x1="0" x2="18" y1="4" y2="4" stroke={s.color} strokeWidth="2.5" strokeDasharray={s.dashed ? "4 3" : undefined} /></svg>
            {s.label}
          </span>
        ))}
        {!any && <span className="hint">{empty ?? "No data"}</span>}
      </div>
      <svg width={width} height={height} role="img" aria-label={label} onMouseLeave={() => setHover(null)}
        onMouseMove={(e) => {
          const r = (e.currentTarget as SVGSVGElement).getBoundingClientRect();
          const px = e.clientX - r.left;
          if (px < PAD.l - 4 || px > width - PAD.r + 4 || n === 0) { setHover(null); return; }
          setHover(Math.max(0, Math.min(n - 1, Math.round(((px - PAD.l) / Math.max(1, plotW)) * (n - 1)))));
        }}>
        {[0, 0.5, 1].map((f) => (
          <g key={f}>
            <line x1={PAD.l} x2={width - PAD.r} y1={y(max * f)} y2={y(max * f)} className="histo-grid" />
            <text x={PAD.l - 6} y={y(max * f) + 4} textAnchor="end" className="histo-axis">{fmt(max * f)}</text>
          </g>
        ))}
        {series.filter((s) => s.area).map((s) => <path key={`a-${s.key}`} d={areaPath(s.values)} fill={s.color} opacity={0.12} />)}
        {series.map((s) => (
          <path key={s.key} d={path(s.values)} fill="none" stroke={s.color} strokeWidth={s.dashed ? 1.6 : 2}
            strokeDasharray={s.dashed ? "4 3" : undefined} strokeLinejoin="round" strokeLinecap="round" />
        ))}
        <line x1={PAD.l} x2={width - PAD.r} y1={y(0)} y2={y(0)} className="histo-base" />
        {Array.from({ length: n }).map((_, i) => (i % tickEvery === 0 ? (
          <text key={i} x={x(i)} y={height - 6} textAnchor="middle" className="histo-axis">{fmtTime(start + i * step, "utc", false).slice(span > 2 * 86400_000 ? 5 : 11, 16)}</text>
        ) : null))}
        {hover !== null && (
          <g>
            <line x1={x(hover)} x2={x(hover)} y1={PAD.t} y2={y(0)} stroke="var(--border-strong)" strokeDasharray="2 3" />
            {series.map((s) => (s.values[hover] !== null && s.values[hover] !== undefined
              ? <circle key={s.key} cx={x(hover)} cy={y(s.values[hover]!)} r={3.5} fill={s.color} stroke="var(--surface)" strokeWidth={1.5} />
              : null))}
          </g>
        )}
      </svg>
      {hover !== null && (
        <div className="histo-tip" style={{ left: Math.min(Math.max(x(hover), 110), width - 110) }}>
          <div className="t">{fmtTime(start + hover * step, "utc", false).slice(11, 16)}{step > 60_000 ? `–${fmtTime(start + (hover + 1) * step, "utc", false).slice(11, 16)}` : ""} UTC</div>
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
