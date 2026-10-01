import { useEffect, useMemo, useRef, useState } from "react";
import type { Bucket } from "../api";
import { fmtInterval, fmtTick, fmtTime, num, type Zone } from "../format";

// Stack order, bottom to top: errors sit on the baseline where they are easiest to compare.
export const LEVELS = ["ERROR", "WARN", "INFO", "DEBUG", "OTHER"] as const;
export const LEVEL_VAR: Record<string, string> = {
  ERROR: "var(--lv-error)", WARN: "var(--lv-warn)", INFO: "var(--lv-info)", DEBUG: "var(--lv-debug)", OTHER: "var(--lv-other)",
};
const LEVEL_LABEL: Record<string, string> = { ERROR: "Error", WARN: "Warn", INFO: "Info", DEBUG: "Debug", OTHER: "Other / none" };

function levelKey(l: string): string {
  const u = l.toUpperCase();
  if (u === "WARNING") return "WARN";
  if (u === "FATAL" || u === "SEVERE" || u === "CRITICAL") return "ERROR";
  if (u === "TRACE") return "DEBUG";
  return (LEVELS as readonly string[]).includes(u) ? u : "OTHER";
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
  const p = 10 ** Math.floor(Math.log10(v));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= v) return m * p;
  return 10 * p;
}

const H = 150;
const PAD = { l: 44, r: 8, t: 8, b: 22 };

/** Log lines per time bucket, stacked by level. Click a bar to zoom into it. */
export function Histogram({ buckets, interval, start, end, zone, onZoom }: {
  buckets: Bucket[];
  interval: number;
  start: number;
  end: number;
  zone: Zone;
  onZoom: (from: number, to: number) => void;
}) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);

  const series = useMemo(() => {
    const byT = new Map(buckets.map((b) => [b.t, b]));
    const first = Math.floor(start / interval) * interval;
    const out: { t: number; total: number; levels: Record<string, number> }[] = [];
    for (let t = first; t <= end; t += interval) {
      const b = byT.get(t);
      const levels: Record<string, number> = {};
      for (const [k, v] of Object.entries(b?.levels ?? {})) levels[levelKey(k)] = (levels[levelKey(k)] ?? 0) + v;
      out.push({ t, total: b?.count ?? 0, levels });
    }
    return out;
  }, [buckets, interval, start, end]);
  const present = LEVELS.filter((l) => series.some((s) => s.levels[l]));
  const max = niceMax(Math.max(1, ...series.map((s) => s.total)));
  const plotW = width - PAD.l - PAD.r;
  const plotH = H - PAD.t - PAD.b;
  const slot = plotW / Math.max(1, series.length);
  const barW = Math.max(1, slot - 2);
  const y = (v: number) => PAD.t + plotH - (v / max) * plotH;
  const tickEvery = Math.max(1, Math.ceil(series.length / Math.max(2, Math.floor(plotW / 90))));
  const span = end - start;
  const h = hover !== null ? series[hover] : null;

  return (
    <div className="histo" ref={ref}>
      <div className="histo-legend" aria-label="Legend">
        {present.map((l) => (
          <span key={l} className="histo-key"><span className="swatch" style={{ background: LEVEL_VAR[l] }} />{LEVEL_LABEL[l]}</span>
        ))}
        <span className="hint" style={{ marginLeft: "auto" }}>Each bar = {fmtInterval(interval)} · click a bar to zoom in</span>
      </div>
      <svg width={width} height={H} role="img" aria-label={`Log lines over time, ${series.length} bars of ${fmtInterval(interval)}`}
        onMouseLeave={() => setHover(null)}>
        {[0, 0.5, 1].map((f) => (
          <g key={f}>
            <line x1={PAD.l} x2={width - PAD.r} y1={y(max * f)} y2={y(max * f)} className="histo-grid" />
            <text x={PAD.l - 6} y={y(max * f) + 4} textAnchor="end" className="histo-axis">{num(max * f)}</text>
          </g>
        ))}
        {series.map((s, i) => {
          const x = PAD.l + i * slot + 1;
          let acc = 0;
          const segs = present.filter((l) => s.levels[l]).map((l) => {
            const v = s.levels[l];
            const y0 = y(acc);
            acc += v;
            return { l, y0, y1: y(acc) };
          });
          return (
            <g key={s.t} opacity={hover === null || hover === i ? 1 : 0.55}>
              {segs.map((g, j) => {
                const top = j === segs.length - 1;
                const hgt = Math.max(1, g.y0 - g.y1);
                const r = top ? Math.min(4, barW / 2, hgt) : 0;
                const d = r
                  ? `M${x},${g.y0} V${g.y1 + r} Q${x},${g.y1} ${x + r},${g.y1} H${x + barW - r} Q${x + barW},${g.y1} ${x + barW},${g.y1 + r} V${g.y0} Z`
                  : `M${x},${g.y0} V${g.y0 - hgt} H${x + barW} V${g.y0} Z`;
                return <path key={g.l} d={d} fill={LEVEL_VAR[g.l]} className="histo-seg" style={barW < 6 ? { strokeWidth: 0.5 } : undefined} />;
              })}
              <rect x={PAD.l + i * slot} y={PAD.t} width={slot} height={plotH} fill="transparent" style={{ cursor: s.total ? "zoom-in" : "default" }}
                onMouseEnter={() => setHover(i)} onClick={() => s.total && onZoom(s.t, Math.min(s.t + interval, end))}>
                <title>{`${fmtTime(s.t, zone, false)}: ${num(s.total)} lines`}</title>
              </rect>
            </g>
          );
        })}
        <line x1={PAD.l} x2={width - PAD.r} y1={y(0)} y2={y(0)} className="histo-base" />
        {series.map((s, i) => (i % tickEvery === 0 ? (
          <text key={s.t} x={PAD.l + i * slot + slot / 2} y={H - 6} textAnchor="middle" className="histo-axis">{fmtTick(s.t, zone, span)}</text>
        ) : null))}
      </svg>
      {h && (
        <div className="histo-tip" style={{ left: Math.min(Math.max(PAD.l + (hover ?? 0) * slot + slot / 2, 110), width - 110) }}>
          <div className="t">{fmtTime(h.t, zone, false)} – {fmtTime(Math.min(h.t + interval, end), zone, false).slice(11)}</div>
          <div className="row" style={{ justifyContent: "space-between", gap: 16 }}><strong>Total</strong><strong>{num(h.total)}</strong></div>
          {LEVELS.filter((l) => h.levels[l]).map((l) => (
            <div key={l} className="row" style={{ justifyContent: "space-between", gap: 16 }}>
              <span className="row" style={{ gap: 6 }}><span className="swatch" style={{ background: LEVEL_VAR[l] }} />{LEVEL_LABEL[l]}</span>
              <span>{num(h.levels[l])}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
