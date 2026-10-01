import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { request } from "../api";
import { Icon } from "../components/icons";
import { Page } from "../components/Shell";
import { Callout, ErrorCallout, useToast } from "../components/ui";
import { useMe } from "../session";

function rules(pw: string, current: string, username: string) {
  const classes = [/[a-z]/, /[A-Z]/, /[0-9]/].filter((r) => r.test(pw)).length;
  const lower = pw.trim().toLowerCase();
  return [
    { text: "At least 12 characters", ok: pw.length >= 12 && pw.length <= 128 },
    { text: "Two of lowercase, uppercase and digits (or 20+ characters of anything)", ok: pw.length > 0 && (classes >= 2 || pw.length >= 20) },
    { text: "Not your email address", ok: pw.length > 0 && lower !== username.toLowerCase() && lower !== username.split("@")[0].toLowerCase() },
    { text: "Different from your current password", ok: pw.length > 0 && pw !== current },
  ];
}

export function ChangePassword() {
  const me = useMe().data!;
  const qc = useQueryClient();
  const toast = useToast();
  const [cur, setCur] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [done, setDone] = useState(false);

  const change = useMutation({
    mutationFn: () => request("/auth/change-password", { method: "POST", body: { currentPassword: cur, newPassword: next } }),
    onSuccess: () => {
      setDone(true);
      setCur("");
      setNext("");
      setConfirm("");
      qc.invalidateQueries({ queryKey: ["me"] });
      toast("Password changed");
    },
  });

  const list = rules(next, cur, me.username);
  const valid = cur.length > 0 && list.every((r) => r.ok) && next === confirm;
  const mismatch = confirm.length > 0 && next !== confirm;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (valid) change.mutate();
  };

  return (
    <Page crumbs={[{ label: "Account" }, { label: "Change password" }]} title="Change password">
      <div className="stack-sm">
        <h1>Change password</h1>
        <span className="sub">{me.username}</span>
      </div>
      <div className="stack" style={{ maxWidth: 560 }}>
        {done ? (
          <Callout tone="success" icon="check">Password changed. You're still signed in here; other browsers and scripts must sign in again.</Callout>
        ) : me.usingGeneratedPassword ? (
          <Callout tone="info">You're using the password your admin generated. Changing it is optional, but recommended.</Callout>
        ) : null}
        <form className="card" onSubmit={submit} noValidate>
          <div className="card-body" style={{ gap: 18, padding: "22px 24px" }}>
            <div className="field">
              <label htmlFor="cur" className="label" style={{ fontSize: 14 }}>Current password</label>
              <input id="cur" className="input" type="password" autoComplete="current-password" value={cur} onChange={(e) => setCur(e.target.value)} style={{ minHeight: 44 }} />
            </div>
            <div className="field">
              <label htmlFor="new" className="label" style={{ fontSize: 14 }}>New password</label>
              <input id="new" className="input" type="password" autoComplete="new-password" aria-describedby="pw-rules" value={next} onChange={(e) => setNext(e.target.value)} style={{ minHeight: 44 }} />
            </div>
            <div className="field">
              <label htmlFor="confirm" className="label" style={{ fontSize: 14 }}>Confirm new password</label>
              <input id="confirm" className="input" type="password" autoComplete="new-password" aria-invalid={mismatch || undefined} value={confirm} onChange={(e) => setConfirm(e.target.value)} style={{ minHeight: 44 }} />
              {mismatch && <span className="hint" style={{ color: "var(--danger)" }}>The passwords don't match</span>}
            </div>
            <ul id="pw-rules" className="checklist">
              {list.map((r) => (
                <li key={r.text} className={r.ok ? "ok" : "todo"}>
                  <Icon name={r.ok ? "check" : "circle"} size={16} strokeWidth={2.2} />
                  <span>{r.text}</span>
                  <span className="sr-only">{r.ok ? "(met)" : "(not met yet)"}</span>
                </li>
              ))}
            </ul>
            {change.error && <ErrorCallout error={change.error} />}
          </div>
          <div className="card-foot" style={{ justifyContent: "flex-end", padding: "14px 24px" }}>
            <Link to="/" className="btn">Cancel</Link>
            <button type="submit" className="btn btn-primary" disabled={!valid || change.isPending}>{change.isPending ? "Changing…" : "Change password"}</button>
          </div>
        </form>
        <SignOutEverywhere />
      </div>
    </Page>
  );
}

function SignOutEverywhere() {
  const qc = useQueryClient();
  const out = useMutation({
    mutationFn: () => request("/auth/logout-all", { method: "POST" }),
    onSuccess: () => {
      qc.clear();
      window.location.assign("/ui/login");
    },
  });
  return (
    <p className="sub" style={{ fontSize: 13 }}>
      Changing your password signs you out everywhere else. To end all sessions without changing it, use{" "}
      <button type="button" className="btn btn-ghost btn-sm" style={{ padding: 0, minHeight: 0 }} onClick={() => out.mutate()} disabled={out.isPending}>
        Sign out everywhere
      </button>
      .
    </p>
  );
}
