// Thin client for the Vector Logs API. Same origin: the session cookie rides along, and the
// X-Requested-With header satisfies the API's CSRF check for writes.

export class ApiError extends Error {
  status: number;
  code: string;
  details: unknown;
  requestId: string | null;

  constructor(status: number, code: string, message: string, details: unknown, requestId: string | null) {
    super(message);
    this.status = status;
    this.code = code;
    this.details = details;
    this.requestId = requestId;
  }
}

type Query = Record<string, string | number | boolean | null | undefined>;

interface RequestOptions {
  method?: string;
  body?: unknown;
  query?: Query;
  signal?: AbortSignal;
}

let onUnauthenticated: (() => void) | null = null;
export function setUnauthenticatedHandler(fn: () => void) {
  onUnauthenticated = fn;
}

const CSRF = { "X-Requested-With": "vector-logs-ui" };

function buildUrl(path: string, query?: Query) {
  const url = new URL(`/api/v1${path}`, window.location.origin);
  for (const [k, v] of Object.entries(query ?? {})) {
    if (v !== undefined && v !== null && v !== "" && v !== false) url.searchParams.set(k, String(v));
  }
  return url.pathname + url.search;
}

async function fail(res: globalThis.Response, path: string, fallback: string): Promise<never> {
  let err: { code?: string; message?: string; details?: unknown } | undefined;
  try {
    err = (await res.json())?.error;
  } catch {
    err = undefined;
  }
  const e = new ApiError(res.status, err?.code ?? `HTTP_${res.status}`, err?.message ?? (res.statusText || fallback),
    err?.details ?? null, res.headers.get("x-request-id"));
  if (res.status === 401 && (e.code === "NOT_AUTHENTICATED" || e.code === "SESSION_EXPIRED") && !path.startsWith("/auth/")) {
    onUnauthenticated?.();
  }
  throw e;
}

export async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json", ...CSRF };
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  let res: globalThis.Response;
  try {
    res = await fetch(buildUrl(path, opts.query), {
      method: opts.method ?? "GET",
      headers,
      credentials: "same-origin",
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
      signal: opts.signal,
    });
  } catch (e) {
    if ((e as Error)?.name === "AbortError") throw e;
    throw new ApiError(0, "NETWORK_ERROR", "Can't reach the server. Check your connection and try again.", null, null);
  }
  if (!res.ok) return fail(res, path, "Request failed");
  const text = await res.text();
  return (text ? JSON.parse(text) : null) as T;
}

export const get = <T>(path: string, query?: Query) => request<T>(path, { query });

export async function downloadPost(path: string, body: unknown): Promise<{ blob: Blob; filename: string; rows: number }> {
  let res: globalThis.Response;
  try {
    res = await fetch(buildUrl(path), {
      method: "POST",
      headers: { Accept: "*/*", "Content-Type": "application/json", ...CSRF },
      credentials: "same-origin",
      body: JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, "NETWORK_ERROR", "Can't reach the server. Check your connection and try again.", null, null);
  }
  if (!res.ok) return fail(res, path, "Export failed");
  const cd = res.headers.get("content-disposition") ?? "";
  const filename = /filename="([^"]+)"/.exec(cd)?.[1] ?? "logs-export";
  return { blob: await res.blob(), filename, rows: Number(res.headers.get("x-export-rows") ?? 0) };
}

export const enc = encodeURIComponent;

// ------------------------------------------------------------------ types

/** A user's access: {clusterId or "*": "view" | "none"}. */
export type Access = Record<string, "view" | "none">;

export interface Me {
  username: string;
  admin: boolean;
  authMode: "password" | "header";
  usingGeneratedPassword: boolean;
  lastLoginAt: string | null;
  clusters: Access;
  limits: { maxSearchHours: number; maxExportRows: number };
}

export interface Cluster {
  id: string;
}

export interface AdminCluster {
  id: string;
  users: string[];
}

export type FilterOp = "is" | "is_not" | "one_of" | "not_one_of" | "contains" | "not_contains" | "exists" | "not_exists" | "gte" | "lte" | "between";

export interface LogFilter {
  field: string;
  op: FilterOp;
  value?: string;
  values?: string[];
  gte?: string;
  lte?: string;
}

export interface SearchBody {
  start: string;
  end: string;
  query: string;
  filters: LogFilter[];
  order: "desc" | "asc";
}

export interface LogRef {
  key: string;
  row: number;
}

export type LogHit = Record<string, unknown> & { _ref: LogRef; _truncated?: boolean };

export interface Column {
  name: string;
  type: string;
}

export interface Bucket {
  t: number;
  count: number;
  levels: Record<string, number>;
}

export interface Facet {
  value: string;
  count: number;
}

export interface SearchResult {
  cluster: string;
  start: number;
  end: number;
  files: number;
  bytes: number;
  cached: number;
  downloadMs: number;
  tookMs: number;
  total: number;
  hits: LogHit[];
  columns: Column[];
  histogram?: { interval: number; buckets: Bucket[] };
  facets?: Record<string, Facet[]>;
}

export interface SavedSearch {
  id: string;
  name: string;
  cluster: string;
  params: string;
  createdAt: string;
  updatedAt: string;
}

export interface UserRec {
  username: string;
  admin: boolean;
  bootstrap: boolean;
  clusters: Access;
  hasPassword: boolean;
  usingGeneratedPassword: boolean;
  lastLoginAt?: string | null;
  lockedUntil?: number | null;
  createdAt?: string;
  createdBy?: string;
  updatedAt?: string;
  updatedBy?: string;
}

export interface Credential {
  username: string;
  password: string;
}
