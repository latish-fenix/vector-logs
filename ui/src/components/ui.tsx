import { createContext, useCallback, useContext, useEffect, useId, useRef, useState, type ReactNode } from "react";
import { ApiError } from "../api";
import { Icon, type IconName } from "./icons";

// ------------------------------------------------------------------ small bits

export function Spinner({ label = "Loading" }: { label?: string }) {
  return <span className="spinner" role="status" aria-label={label} />;
}

export function Loading({ what = "Loading…" }: { what?: string }) {
  return (
    <div className="empty" aria-busy="true">
      <Spinner />
      <span>{what}</span>
    </div>
  );
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="empty">
      <span className="t">{title}</span>
      {children && <span>{children}</span>}
    </div>
  );
}

type Tone = "info" | "success" | "warn" | "danger" | "neutral";
const TONE_ICON: Record<Tone, IconName> = { info: "info", success: "check", warn: "warn", danger: "alert", neutral: "info" };

export function Callout({ tone = "neutral", title, icon, children, role }: { tone?: Tone; title?: ReactNode; icon?: IconName; children?: ReactNode; role?: string }) {
  return (
    <div className={`callout ${tone === "neutral" ? "" : tone}`} role={role ?? (tone === "danger" ? "alert" : undefined)}>
      <Icon name={icon ?? TONE_ICON[tone]} />
      <div className="stack-sm" style={{ gap: 2, minWidth: 0 }}>
        {title && <span className="title">{title}</span>}
        {children && <div>{children}</div>}
      </div>
    </div>
  );
}

export function Badge({ tone, mono, children, title }: { tone?: "blue" | "green" | "amber" | "red"; mono?: boolean; children: ReactNode; title?: string }) {
  return <span className={`badge ${tone ?? ""} ${mono ? "mono" : ""}`} title={title}>{children}</span>;
}

export function SearchInput({ label, value, onChange, placeholder }: { label: string; value: string; onChange: (v: string) => void; placeholder?: string }) {
  const id = useId();
  return (
    <div className="search">
      <label htmlFor={id} className="sr-only">{label}</label>
      <Icon name="search" size={16} />
      <input id={id} className="input" type="search" value={value} placeholder={placeholder} onChange={(e) => onChange(e.target.value)} />
    </div>
  );
}

// ------------------------------------------------------------------ errors

interface Explained {
  title: string;
  hint?: ReactNode;
}

function explain(e: ApiError, ctx: { admin?: boolean }): Explained {
  switch (e.code) {
    case "PERMISSION_DENIED":
      return { title: "You don't have access to this cluster", hint: "Ask an admin to give you access in Administration → Users." };
    case "RANGE_TOO_LARGE":
      return { title: "Time range too long", hint: "One search can cover up to 7 days. It can start anywhere in the last 30 days." };
    case "TOO_MANY_FILES":
      return { title: "Too many log files in this range", hint: "Pick a shorter time range." };
    case "SEARCH_TOO_BIG":
      return { title: "The search needed too much memory", hint: "Pick a shorter time range or add filters." };
    case "UNKNOWN_FIELD":
      return { title: "Unknown column", hint: "Check the field name; the list of columns is in the Columns dialog." };
    case "INVALID_FILTER":
    case "INVALID_TIME":
      return { title: "Check the search" };
    case "LOGS_ACCESS_DENIED":
      return { title: "The server can't read the logs bucket", hint: ctx.admin ? "Add the read-only S3 statements from docs/iam-policy.json to the EC2 instance role." : "Tell an admin." };
    case "LOGS_UNREACHABLE":
    case "LOGS_UNAVAILABLE":
    case "LOGS_BUCKET_MISSING":
      return { title: "Can't read the logs from S3" };
    case "NETWORK_ERROR":
      return { title: "Can't reach the server" };
    default:
      return { title: e.status >= 500 ? "Something went wrong" : "Request refused" };
  }
}

export function ErrorCallout({ error, admin }: { error: unknown; admin?: boolean }) {
  if (!error) return null;
  if (!(error instanceof ApiError)) {
    return <Callout tone="danger" title="Something went wrong">{String((error as Error)?.message ?? error)}</Callout>;
  }
  const ex = explain(error, { admin });
  return (
    <Callout tone="danger" title={ex.title}>
      <div className="stack-sm" style={{ gap: 4 }}>
        <span>{error.message}</span>
        {ex.hint && <span>{ex.hint}</span>}
        <span className="meta">
          {error.code}
          {error.status ? ` (${error.status})` : ""}
          {error.requestId ? ` · request ${error.requestId.slice(0, 8)}` : ""}
        </span>
      </div>
    </Callout>
  );
}

export function errorMessage(e: unknown): string {
  return e instanceof ApiError ? e.message : String((e as Error)?.message ?? e);
}

// ------------------------------------------------------------------ dialog

export function Dialog({ title, subtitle, onClose, children, footer, wide, busy }: {
  title: ReactNode;
  subtitle?: ReactNode;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  wide?: boolean;
  busy?: boolean;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  const busyRef = useRef(busy);
  busyRef.current = busy;

  useEffect(() => {
    const prev = document.activeElement as HTMLElement | null;
    const el = ref.current;
    const first = el?.querySelector<HTMLElement>("[autofocus], input:not([type=hidden]):not([disabled]), textarea, select, button:not([disabled])");
    (first ?? el)?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !busyRef.current) {
        e.stopPropagation();
        closeRef.current();
      }
      if (e.key === "Tab" && el) {
        const items = Array.from(el.querySelectorAll<HTMLElement>("a[href], button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex='-1'])"));
        if (!items.length) return;
        const i = items.indexOf(document.activeElement as HTMLElement);
        if (e.shiftKey && i <= 0) {
          e.preventDefault();
          items[items.length - 1].focus();
        } else if (!e.shiftKey && i === items.length - 1) {
          e.preventDefault();
          items[0].focus();
        }
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("keydown", onKey, true);
      prev?.focus?.();
    };
  }, []);

  return (
    <div className="overlay" onMouseDown={(e) => { if (e.target === e.currentTarget && !busy) onClose(); }}>
      <div ref={ref} className={`dialog ${wide ? "wide" : ""}`} role="dialog" aria-modal="true" aria-labelledby={titleId} tabIndex={-1}>
        <div className="dialog-head">
          <div>
            <h2 id={titleId}>{title}</h2>
            {subtitle && <span className="sub" style={{ fontSize: 13 }}>{subtitle}</span>}
          </div>
          <button type="button" className="btn btn-ghost icon-btn" aria-label="Close" onClick={onClose} disabled={busy}>
            <Icon name="x" />
          </button>
        </div>
        <div className="dialog-body">{children}</div>
        {footer && <div className="dialog-foot">{footer}</div>}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ toasts

interface ToastItem {
  id: number;
  text: string;
  tone: "ok" | "error";
}
const ToastCtx = createContext<(text: string, tone?: "ok" | "error") => void>(() => {});

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const push = useCallback((text: string, tone: "ok" | "error" = "ok") => {
    const id = Date.now() + Math.random();
    setItems((xs) => [...xs, { id, text, tone }]);
    setTimeout(() => setItems((xs) => xs.filter((x) => x.id !== id)), tone === "error" ? 8000 : 4500);
  }, []);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="toasts" aria-live="polite">
        {items.map((t) => (
          <div key={t.id} className={`toast ${t.tone === "error" ? "error" : ""}`} role="status">
            <Icon name={t.tone === "error" ? "alert" : "check"} />
            <span style={{ flex: 1 }}>{t.text}</span>
            <button type="button" aria-label="Dismiss" onClick={() => setItems((xs) => xs.filter((x) => x.id !== t.id))}>×</button>
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}

export const useToast = () => useContext(ToastCtx);

// ------------------------------------------------------------------ misc helpers

export function copyText(text: string): Promise<void> {
  if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
  // Plain-HTTP deployments have no Clipboard API: fall back to a hidden textarea.
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  try {
    document.execCommand("copy");
  } finally {
    ta.remove();
  }
  return Promise.resolve();
}

export function downloadFile(name: string, content: string, type: string) {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function usePageTitle(title: string) {
  useEffect(() => {
    document.title = title ? `${title} · Vector Logs` : "Vector Logs";
  }, [title]);
}
