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

export interface Download { blob: Blob; filename: string; rows: number; total: number | null; firstMs: number | null; lastMs: number | null }

export async function downloadPost(path: string, body: unknown): Promise<Download> {
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
  const n = (h: string) => { const v = res.headers.get(h); return v ? Number(v) : null; };
  return { blob: await res.blob(), filename, rows: n("x-export-rows") ?? 0, total: n("x-export-total"), firstMs: n("x-export-first"), lastMs: n("x-export-last") };
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

// ------------------------------------------------------------ ECS health

export type ClusterStatus = "healthy" | "degraded" | "down" | "none";

export interface HealthTarget {
  id: string;
  port: number | null;
  state: string;
  reason: string | null;
  description: string | null;
  ip: string | null;
  name: string | null;
  inCluster?: boolean;
}

export interface HealthTargetGroup {
  arn: string;
  name: string;
  port: number | null;
  protocol: string | null;
  targetType: string | null;
  healthCheckPath: string | null;
  loadBalancers: { name: string; type: string | null; scheme: string | null; dns: string | null; state: string | null }[];
  targets: HealthTarget[];
  total: number;
  counts: { healthy: number; bad: number; other: number };
  error: string | null;
  linkedBy?: string[];
}

export interface HealthService {
  name: string;
  status: string;
  launchType: string | null;
  desired: number;
  running: number;
  pending: number;
  rollout: string | null;
  deployments: number;
  targetGroups: string[];
  lastEvent: { at: string; message: string } | null;
}

export interface HealthInstance {
  id: string;
  ip: string | null;
  name: string | null;
  status: string;
  agentConnected: boolean | null;
  runningTasks: number | null;
  behindLoadBalancer: boolean;
}

export interface HealthCluster {
  name: string;
  arn: string;
  clusterStatus: string;
  status: ClusterStatus;
  instancesRegistered: number;
  runningTasks: number;
  pendingTasks: number;
  activeServices: number;
  targetGroups: HealthTargetGroup[];
  services: HealthService[];
  instances: HealthInstance[];
  targets: { healthy: number; total: number; bad: number };
  servicesRunning: number;
  servicesDesired: number;
  loadBalancers: string[];
}

export interface HealthSnapshot {
  region: string;
  generatedAt: string;
  tookMs: number;
  summary: {
    clusters: number; withLoadBalancer: number; withoutLoadBalancer: number; down: number; degraded: number;
    healthy: number; unhealthyTargets: number; servicesBelowDesired: number;
  };
  clusters: HealthCluster[];
  unlinkedTargetGroups: HealthTargetGroup[];
}

// ---- load balancer access logs (/api/v1/lb)
export type StatusClass = "2xx" | "3xx" | "4xx" | "5xx" | "other";

export interface LbBody {
  start: string;
  end: string;
  lbs?: string[];
  targetGroups?: string[];
  domains?: string[];
  statusClasses?: StatusClass[];
  statusCodes?: number[];
  source?: "app" | "lb" | null;
  methods?: string[];
  path?: string;
  pathGroup?: string;
  client?: string;
  target?: string;
  minTargetSeconds?: number | null;
  q?: string;
}

export interface LbPending {
  files: number;
  of: number;
  requested?: boolean;
  starting?: boolean;
  converterUpdatedAt: string | null;
  lastError: { code: string; message: string; at: string } | null;
}

export interface LbCounts { "2xx": number; "3xx": number; "4xx": number; "5xx": number; other: number }

export interface LbTotals extends LbCounts {
  requests: number;
  s5xxApp: number;
  s5xxLb: number;
  p50: number | null;
  p95: number | null;
  p99: number | null;
  avg: number | null;
}

export interface LbTargetGroupRow {
  tg: string | null;
  cluster: string | null;
  lbs: string[];
  requests: number;
  s4xx: number;
  s5xx: number;
  s5xxApp: number;
  s5xxLb: number;
  avg: number | null;
  p95: number | null;
  topError: { method: string | null; pathGroup: string | null; code: number | null; count: number } | null;
}

export interface LbSummary {
  start: number;
  end: number;
  interval: number;
  files: number;
  source: "minute" | "paths" | "rows" | "none";
  pending: LbPending;
  totals: LbTotals;
  buckets: (LbCounts & { t: number })[];
  targetGroups: LbTargetGroupRow[];
  tookMs: number;
}

export interface LbPathRow extends LbCounts {
  method: string | null;
  pathGroup: string | null;
  example: string | null;
  requests: number;
  p95: number | null;
  avg: number | null;
  topCode: number | null;
  targetGroups: (string | null)[];
}

export type LbRequest = Record<string, string | number | null> & { ts_ms: number };

export interface LbOverview {
  items: { name: string; targetGroups: { name: string | null; cluster: string | null; requests24h: number }[] }[];
  converter: {
    updatedAt: string | null;
    lastError: { code: string; message: string; at: string } | null;
    warmDays: number;
    pollSeconds: number;
    filesListed: number;
    filesConverted: number;
    cycleMs: number | null;
  };
}

// ---- ECS CPU / memory (CloudWatch)
export interface MetricSeries { now: number | null; avg: number | null; max: number | null; points: [number, number][] }
export interface EcsClusterMetrics {
  hours: number;
  period: number;
  generatedAt: string;
  clusters: Record<string, { cpu?: MetricSeries; memory?: MetricSeries; cpuReserved?: MetricSeries; memoryReserved?: MetricSeries }>;
}
export interface EcsServiceMetrics {
  cluster: string;
  hours: number;
  period: number;
  generatedAt: string;
  services: Record<string, { cpu?: MetricSeries; memory?: MetricSeries }>;
}
