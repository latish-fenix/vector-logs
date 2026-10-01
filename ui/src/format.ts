export const nf = new Intl.NumberFormat("en-US");

export function num(v: string | number | null | undefined): string {
  if (v === null || v === undefined || v === "") return "—";
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? nf.format(n) : String(v);
}

export function bytes(b: number | null | undefined): string {
  if (b === null || b === undefined) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = b;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v >= 100 || i === 0 ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
}

export function duration(ms: number): string {
  if (ms < 1000) return `${ms} ms`;
  return `${(ms / 1000).toFixed(ms < 10_000 ? 1 : 0)} s`;
}

export function ago(iso: string | null | undefined): string {
  if (!iso) return "never";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  const d = Math.floor(s / 86400);
  return d === 1 ? "yesterday" : `${d} days ago`;
}

// ------------------------------------------------------------------ time zone
// Log times are UTC in the files. The console shows them in the browser's time zone by
// default, with a switch to UTC (remembered per browser).

export type Zone = "local" | "utc";
const ZONE_KEY = "vlg.zone";

export function storedZone(): Zone {
  try {
    return localStorage.getItem(ZONE_KEY) === "utc" ? "utc" : "local";
  } catch {
    return "local";
  }
}
export function storeZone(z: Zone) {
  try {
    localStorage.setItem(ZONE_KEY, z);
  } catch {
    /* storage unavailable */
  }
}

export function localZoneName(): string {
  const parts = new Intl.DateTimeFormat("en-US", { timeZoneName: "short" }).formatToParts(new Date());
  return parts.find((p) => p.type === "timeZoneName")?.value ?? "Local";
}

const pad = (n: number, w = 2) => String(n).padStart(w, "0");

/** 2026-10-01 05:30:00.157 in the chosen zone. */
export function fmtTime(ms: number | null | undefined, zone: Zone, withMs = true): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return "—";
  const d = new Date(ms);
  const [Y, M, D, h, m, s, x] = zone === "utc"
    ? [d.getUTCFullYear(), d.getUTCMonth() + 1, d.getUTCDate(), d.getUTCHours(), d.getUTCMinutes(), d.getUTCSeconds(), d.getUTCMilliseconds()]
    : [d.getFullYear(), d.getMonth() + 1, d.getDate(), d.getHours(), d.getMinutes(), d.getSeconds(), d.getMilliseconds()];
  return `${Y}-${pad(M)}-${pad(D)} ${pad(h)}:${pad(m)}:${pad(s)}${withMs ? "." + pad(x, 3) : ""}`;
}

/** Short label for a histogram tick. */
export function fmtTick(ms: number, zone: Zone, spanMs: number): string {
  const full = fmtTime(ms, zone, false);
  if (spanMs > 2 * 86400_000) return full.slice(5, 16).replace(" ", " ");
  return full.slice(11, 16);
}

/** Value for <input type="datetime-local"> in the chosen zone. */
export function toInput(ms: number, zone: Zone): string {
  return fmtTime(ms, zone, false).replace(" ", "T").slice(0, 16);
}

/** Parse a datetime-local value in the chosen zone to epoch ms. */
export function fromInput(v: string, zone: Zone): number | null {
  if (!v) return null;
  const ms = zone === "utc" ? Date.parse(v + ":00Z") : new Date(v).getTime();
  return Number.isFinite(ms) ? ms : null;
}

export function fmtInterval(ms: number): string {
  if (ms < 3600_000) return `${ms / 60_000} min`;
  if (ms < 86400_000) return `${ms / 3600_000} h`;
  return `${ms / 86400_000} d`;
}
