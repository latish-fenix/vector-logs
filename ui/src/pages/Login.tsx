import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { ApiError, get, request } from "../api";
import { Icon } from "../components/icons";
import { Callout, usePageTitle } from "../components/ui";

const FEATURES = [
  { icon: "search" as const, t: "Search every cluster", d: "Words, exact phrases and column filters over the last 30 days." },
  { icon: "clock" as const, t: "See when it started", d: "A timeline of errors and warnings; click a bar to zoom in." },
  { icon: "bookmark" as const, t: "Save and share", d: "Keep the searches you run often; send a link to a teammate." },
];

function safeNext(next: string | null): string {
  // Only same-app paths: never an absolute URL an attacker could plant in ?next=.
  return next && next.startsWith("/") && !next.startsWith("//") && !next.startsWith("/login") ? next : "/";
}

export function Login() {
  usePageTitle("Sign in");
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [show, setShow] = useState(false);
  const cfg = useQuery({
    queryKey: ["auth-config"],
    queryFn: () => get<{ authMode: string; sessionHours: number; lockoutAttempts: number; lockoutMinutes: number }>("/auth/config"),
    staleTime: Infinity,
    retry: false,
  });

  const login = useMutation({
    mutationFn: () => request<{ user: { usingGeneratedPassword: boolean } }>("/auth/login", { method: "POST", body: { username: email.trim(), password } }),
    onSuccess: async () => {
      qc.clear();
      navigate(safeNext(params.get("next")), { replace: true });
    },
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!email.trim() || !password) return;
    login.mutate();
  };

  const err = login.error instanceof ApiError ? login.error : null;
  const wrong = err?.code === "INVALID_CREDENTIALS";
  return (
    <div className="login">
      <section className="login-hero">
        <div className="brand" style={{ padding: 0, gap: 12 }}>
          <span className="brand-mark" style={{ width: 40, height: 40, borderRadius: 10 }}><Icon name="logo" size={22} strokeWidth={2.4} style={{ color: "#fff" }} /></span>
          <span style={{ fontSize: 20, fontWeight: 700, color: "#fff" }}>Vector Logs</span>
        </div>
        <div className="stack" style={{ gap: 16, marginTop: 60 }}>
          <h1>Find the log line that matters.</h1>
          <p>The application logs from every Fenix app server, shipped to S3 by Vector, searchable in one place.</p>
        </div>
        <div className="stack" style={{ gap: 20 }}>
          {FEATURES.map((f) => (
            <div key={f.t} className="login-feature">
              <Icon name={f.icon} size={22} style={{ color: "#60a5fa", flexShrink: 0 }} />
              <div className="stack-sm" style={{ gap: 2 }}><span className="t">{f.t}</span><span className="d">{f.d}</span></div>
            </div>
          ))}
        </div>
      </section>
      <section className="login-side">
        <form className="login-card" onSubmit={submit} noValidate>
          <div className="stack-sm">
            <h2 style={{ fontSize: 24 }}>Sign in</h2>
            <p className="sub">Use the email and password your admin gave you.</p>
          </div>
          {params.get("expired") && !err && <Callout tone="info">Your session ended. Sign in again to continue.</Callout>}
          {err && (
            <Callout tone="danger" title={err.code === "ACCOUNT_LOCKED" ? "Account locked" : wrong ? undefined : "Can't sign in"}>
              {wrong ? "Wrong email or password." : err.message}
            </Callout>
          )}
          <div className="field">
            <label htmlFor="email" className="label" style={{ fontSize: 14 }}>Email</label>
            <input id="email" className="input" type="email" autoComplete="username" autoFocus required value={email} onChange={(e) => setEmail(e.target.value)} style={{ minHeight: 44 }} />
          </div>
          <div className="field">
            <label htmlFor="password" className="label" style={{ fontSize: 14 }}>Password</label>
            <div className="pw-wrap">
              <input id="password" className="input" type={show ? "text" : "password"} autoComplete="current-password" required value={password}
                onChange={(e) => setPassword(e.target.value)} aria-invalid={wrong || undefined} style={{ minHeight: 44 }} />
              <button type="button" className="btn btn-ghost btn-sm" onClick={() => setShow((s) => !s)} aria-pressed={show}>{show ? "Hide" : "Show"}</button>
            </div>
          </div>
          <button type="submit" className="btn btn-primary" style={{ minHeight: 46, fontSize: 15 }} disabled={login.isPending || !email.trim() || !password}>
            {login.isPending ? "Signing in…" : "Sign in"}
          </button>
          <p className="sub" style={{ fontSize: 13 }}>Forgot your password? Ask an admin to reset it; you'll get a new one as a CSV file.</p>
          {cfg.data?.authMode === "header" ? (
            <Callout tone="warn" title="Password sign-in is off">This server runs with AUTH_MODE=header (development only). The web console needs AUTH_MODE=password.</Callout>
          ) : (
            <Callout icon="lock">
              After {cfg.data?.lockoutAttempts ?? 5} wrong passwords the account locks for {cfg.data?.lockoutMinutes ?? 15} minutes. Sessions last {cfg.data?.sessionHours ?? 12} hours.
            </Callout>
          )}
        </form>
      </section>
    </div>
  );
}
