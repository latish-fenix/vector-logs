import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { ApiError, enc, get, request, type Access, type AdminCluster, type Credential, type UserRec } from "../../api";
import { Icon } from "../../components/icons";
import { Page } from "../../components/Shell";
import { Badge, Callout, Dialog, Empty, ErrorCallout, Loading, SearchInput, copyText, downloadFile, useToast } from "../../components/ui";
import { ago } from "../../format";
import { useMe } from "../../session";

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

function useAllClusters() {
  return useQuery({
    queryKey: ["admin-clusters"],
    queryFn: () => get<{ items: AdminCluster[] }>("/admin/clusters"),
    staleTime: 60_000,
  });
}

export function csvOf(rows: Credential[]): string {
  const q = (v: string) => (/[",\r\n]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v);
  return "username,password\r\n" + rows.map((r) => `${q(r.username)},${q(r.password)}\r\n`).join("");
}

function passwordStatus(u: UserRec) {
  if (u.lockedUntil && u.lockedUntil * 1000 > Date.now()) return <Badge tone="red" title="Too many failed sign-ins">Locked</Badge>;
  if (!u.hasPassword) return <Badge title="Can't sign in until an admin resets the password">No password</Badge>;
  if (u.usingGeneratedPassword && u.bootstrap) return <Badge tone="amber" title="Still using the first-admin password (Secrets Manager secret <prefix>app)">Initial</Badge>;
  if (u.usingGeneratedPassword) return <Badge tone="amber" title="Still using the password an admin generated">Generated</Badge>;
  return <Badge tone="green">Set by user</Badge>;
}

/** Which clusters the access map lets the user read (same rules as the API). */
function readable(access: Access, clusters: string[]): string[] {
  return clusters.filter((c) => (access[c] ?? access["*"]) === "view");
}

function accessText(u: UserRec, clusters: string[]): string {
  if (u.admin) return "All clusters (admin)";
  const all = u.clusters["*"] === "view";
  const except = Object.entries(u.clusters).filter(([k, v]) => k !== "*" && v === "none").map(([k]) => k);
  if (all) return except.length ? `All clusters except ${except.join(", ")}` : "All clusters";
  const list = readable(u.clusters, Array.from(new Set([...clusters, ...Object.keys(u.clusters).filter((k) => k !== "*")])));
  return list.length ? list.join(", ") : "No access";
}

export function Users() {
  const me = useMe().data!;
  const clusterList = (useAllClusters().data?.items ?? []).map((c) => c.id);
  const users = useQuery({ queryKey: ["admin-users"], queryFn: () => get<{ items: UserRec[] }>("/admin/users").then((r) => r.items) });
  const [selected, setSelected] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [filter, setFilter] = useState("");
  const rows = useMemo(() => {
    const f = filter.trim().toLowerCase();
    return (users.data ?? []).filter((u) => !f || u.username.includes(f));
  }, [users.data, filter]);
  const sel = users.data?.find((u) => u.username === selected) ?? null;

  return (
    <Page crumbs={[{ label: "Administration" }, { label: "Users" }]} title="Users">
      <div className="page-head" style={{ alignItems: "center" }}>
        <div className="grow">
          <h1>Users &amp; access</h1>
          <span className="sub">People sign in with their email and a password. Give each person the clusters whose logs they may read; admins read every cluster and manage users.</span>
        </div>
        <button type="button" className="btn btn-primary" onClick={() => setAdding(true)}><Icon name="plus" /> Add users</button>
      </div>
      <div className="grid-side users-grid">
        <section className="card" style={{ overflow: "hidden" }}>
          <div className="card-head">
            <div className="grow"><h2>{users.data ? `${users.data.length} user${users.data.length === 1 ? "" : "s"}` : "Users"}</h2></div>
            <div style={{ width: 260 }}><SearchInput label="Filter users" value={filter} onChange={setFilter} placeholder="Filter by email" /></div>
          </div>
          {users.isLoading ? <Loading /> : users.error ? <div className="card-body"><ErrorCallout error={users.error} /></div> : rows.length === 0 ? <Empty title="No users match" /> : (
            <div className="table-scroll" style={{ maxHeight: "calc(100vh - 260px)" }}>
              <table className="table compact">
                <thead><tr><th>User</th><th>Role</th><th>Clusters</th><th>Password</th></tr></thead>
                <tbody>
                  {rows.map((u) => (
                    <tr key={u.username} className={`clickable ${u.username === selected ? "selected" : ""}`} onClick={() => setSelected(u.username)}>
                      <td>
                        <button type="button" className="row" style={{ gap: 10, flexWrap: "nowrap", background: "none", border: 0, padding: 0, font: "inherit", color: "inherit", cursor: "pointer", textAlign: "left" }}
                          onClick={(e) => { e.stopPropagation(); setSelected(u.username); }} aria-pressed={u.username === selected}>
                          <span className="avatar" style={{ width: 30, height: 30, fontSize: 12, background: u.admin ? "#115e59" : "#475569" }} aria-hidden="true">{u.username[0]}</span>
                          <span className="stack-sm" style={{ gap: 0 }}>
                            <span style={{ fontWeight: 600, overflowWrap: "anywhere" }}>{u.username}{u.username === me.username && <span className="hint"> (you)</span>}</span>
                            <span className="hint">Last sign-in {ago(u.lastLoginAt)}</span>
                          </span>
                        </button>
                      </td>
                      <td>
                        <span className="row" style={{ gap: 6 }}>
                          {u.admin ? <Badge tone="blue">Admin</Badge> : <span>Member</span>}
                          {u.bootstrap && <Badge title="Admin from BOOTSTRAP_ADMINS in .env">Bootstrap</Badge>}
                        </span>
                      </td>
                      <td className="hint" style={{ maxWidth: 320 }}>{accessText(u, clusterList)}</td>
                      <td>{passwordStatus(u)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
        {sel ? <EditUser key={sel.username} user={sel} isMe={sel.username === me.username} passwordMode={me.authMode === "password"} onClose={() => setSelected(null)} /> : (
          <aside className="card"><Empty title="Select a user">Change which clusters they can read, reset their password or remove them.</Empty></aside>
        )}
      </div>
      {adding && <AddUsersDialog onClose={() => setAdding(false)} />}
    </Page>
  );
}

/** Per-cluster access: an "all clusters" switch plus one checkbox per cluster folder. */
function ClusterAccess({ value, onChange }: { value: Access; onChange: (v: Access) => void }) {
  const q = useAllClusters();
  const folders = (q.data?.items ?? []).map((c) => c.id);
  const extra = Object.keys(value).filter((k) => k !== "*" && !folders.includes(k));
  const all = value["*"] === "view";
  const toggleAll = (on: boolean) => {
    const next: Access = {};
    for (const [k, v] of Object.entries(value)) if (k !== "*" && v === "view") next[k] = "view";
    if (on) {
      // keep explicit exclusions empty: turning "all" on starts with every cluster readable
      for (const k of Object.keys(next)) delete next[k];
      next["*"] = "view";
    }
    onChange(next);
  };
  const toggle = (c: string, on: boolean) => {
    const next = { ...value };
    if (all) {
      if (on) delete next[c];
      else next[c] = "none";
    } else if (on) next[c] = "view";
    else delete next[c];
    onChange(next);
  };
  if (q.isLoading) return <Loading what="Loading clusters…" />;
  return (
    <div className="stack" style={{ gap: 8 }}>
      <label className="check" style={{ padding: "10px 12px", borderRadius: 10, border: "1px solid var(--border)" }}>
        <input type="checkbox" checked={all} onChange={(e) => toggleAll(e.target.checked)} />
        <span className="stack-sm" style={{ gap: 2 }}>
          <span style={{ fontWeight: 600 }}>All clusters</span>
          <span className="hint">Includes clusters that appear later. Untick single clusters below to leave them out.</span>
        </span>
      </label>
      <div className="access-list">
        {folders.length === 0 && <span className="hint">No cluster folders found in S3 yet.</span>}
        {[...folders, ...extra].map((c) => {
          const on = (value[c] ?? value["*"]) === "view";
          return (
            <label key={c} className="check">
              <input type="checkbox" checked={on} onChange={(e) => toggle(c, e.target.checked)} />
              <span className="mono grow" style={{ fontSize: 13.5 }}>{c}</span>
              {!folders.includes(c) && <span className="hint">no folder yet</span>}
            </label>
          );
        })}
      </div>
    </div>
  );
}

function clean(a: Access): Access {
  const out: Access = {};
  for (const [k, v] of Object.entries(a)) {
    if (k === "*" && v !== "view") continue;
    if (v === "none" && a["*"] !== "view") continue;
    out[k] = v;
  }
  return out;
}

function same(a: Access, b: Access) {
  const x = clean(a), y = clean(b);
  const ks = new Set([...Object.keys(x), ...Object.keys(y)]);
  return [...ks].every((k) => x[k] === y[k]);
}

function EditUser({ user, isMe, passwordMode, onClose }: { user: UserRec; isMe: boolean; passwordMode: boolean; onClose: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [admin, setAdmin] = useState(user.admin);
  const [access, setAccess] = useState<Access>(user.clusters);
  const [confirmRemove, setConfirmRemove] = useState(false);
  const [reset, setReset] = useState(false);
  useEffect(() => { setAdmin(user.admin); setAccess(user.clusters); }, [user]);
  const dirty = admin !== user.admin || !same(access, user.clusters);

  const save = useMutation({
    mutationFn: () => request(`/admin/users/${enc(user.username)}`, { method: "PUT", body: { admin, clusters: clean(access) } }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin-users"] });
      qc.invalidateQueries({ queryKey: ["admin-clusters"] });
      qc.invalidateQueries({ queryKey: ["me"] });
      toast(`Saved ${user.username}`);
    },
  });
  const remove = useMutation({
    mutationFn: () => request(`/admin/users/${enc(user.username)}`, { method: "DELETE" }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["admin-users"] }); toast(`Removed ${user.username}`); onClose(); },
  });
  const locked = !!(user.lockedUntil && user.lockedUntil * 1000 > Date.now());

  return (
    <aside className="card" aria-label="Edit user">
      <div className="card-head">
        <div className="grow"><h2>Edit user</h2><span className="hint">Saved to S3; passwords are in Secrets Manager.</span></div>
        <button type="button" className="btn btn-ghost icon-btn" aria-label="Close" onClick={onClose}><Icon name="x" /></button>
      </div>
      <div className="card-body" style={{ gap: 18 }}>
        <div className="field">
          <label className="label" htmlFor="u-email">Email</label>
          <input id="u-email" className="input" readOnly value={user.username} />
        </div>
        {passwordMode && (
          <div className={`callout ${locked ? "danger" : user.usingGeneratedPassword ? "warn" : ""}`} style={{ flexDirection: "column", gap: 10 }}>
            <div className="stack-sm" style={{ gap: 2 }}>
              <span className="title" style={{ fontSize: 14 }}>Password</span>
              <span>
                {locked ? "Locked after too many failed sign-ins. A reset unlocks it." : !user.hasPassword ? "No password yet: they can't sign in." : user.usingGeneratedPassword && user.bootstrap ? "Still using the first-admin password from Secrets Manager." : user.usingGeneratedPassword ? "Still using the generated password." : "Set by the user."}
                {" "}Last sign-in {ago(user.lastLoginAt)}.
              </span>
            </div>
            <button type="button" className="btn btn-sm" style={{ alignSelf: "flex-start" }} onClick={() => setReset(true)}><Icon name="key" size={15} /> Reset password…</button>
          </div>
        )}
        <label className="check" style={{ padding: "12px 14px", borderRadius: 10, border: "1px solid var(--border)" }}>
          <input type="checkbox" role="switch" checked={admin} disabled={user.bootstrap || isMe} onChange={(e) => setAdmin(e.target.checked)} />
          <span className="stack-sm" style={{ gap: 2 }}>
            <span style={{ fontWeight: 600 }}>Admin</span>
            <span className="hint">{user.bootstrap ? "Bootstrap admin from .env; can't be changed here" : isMe ? "You can't remove your own admin rights" : "Reads every cluster, manages users"}</span>
          </span>
        </label>
        <div className="stack-sm" style={{ gap: 10 }}>
          <span className="label">Clusters they can read</span>
          {admin ? <span className="hint">Admins read every cluster.</span> : <ClusterAccess value={access} onChange={setAccess} />}
        </div>
        {save.error && <ErrorCallout error={save.error} />}
        {remove.error && <ErrorCallout error={remove.error} />}
        {confirmRemove && (
          <Callout tone="danger" title={`Remove ${user.username}?`}>
            They can't sign in any more, and their saved searches are deleted.
            <div className="row" style={{ marginTop: 8 }}>
              <button type="button" className="btn btn-sm" onClick={() => setConfirmRemove(false)}>Keep</button>
              <button type="button" className="btn btn-danger btn-sm" onClick={() => remove.mutate()} disabled={remove.isPending}>Remove user</button>
            </div>
          </Callout>
        )}
      </div>
      <div className="card-foot">
        <button type="button" className="btn btn-ghost danger" style={{ marginRight: "auto" }} disabled={isMe || user.bootstrap || confirmRemove}
          title={isMe ? "You can't remove yourself" : user.bootstrap ? "Bootstrap admins come from .env" : undefined} onClick={() => setConfirmRemove(true)}>Remove user</button>
        <button type="button" className="btn" disabled={!dirty} onClick={() => { setAdmin(user.admin); setAccess(user.clusters); }}>Cancel</button>
        <button type="button" className="btn btn-primary" disabled={!dirty || save.isPending} onClick={() => save.mutate()}>{save.isPending ? "Saving…" : "Save"}</button>
      </div>
      {reset && <ResetPasswordDialog username={user.username} onClose={() => setReset(false)} />}
    </aside>
  );
}

// ------------------------------------------------------------ credentials

function CredentialsTable({ rows, onCopy }: { rows: Credential[]; onCopy: () => void }) {
  const toast = useToast();
  return (
    <div className="card" style={{ overflow: "hidden" }}>
      <table className="table">
        <thead><tr><th>Username</th><th>Password</th><th><span className="sr-only">Copy</span></th></tr></thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.username}>
              <td style={{ fontWeight: 600 }}>{r.username}</td>
              <td><code style={{ background: "var(--surface-3)", padding: "4px 8px", borderRadius: 6, fontSize: 14 }}>{r.password}</code></td>
              <td style={{ textAlign: "right" }}>
                <button type="button" className="btn icon-btn" aria-label={`Copy password for ${r.username}`}
                  onClick={() => copyText(r.password).then(() => { onCopy(); toast("Password copied"); })}>
                  <Icon name="copy" size={16} />
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function CredentialsFooter({ rows, filename, onDone }: { rows: Credential[]; filename: string; onDone: () => void }) {
  const [saved, setSaved] = useState(false);
  const [warned, setWarned] = useState(false);
  return (
    <>
      {warned && !saved && <span className="hint" style={{ color: "var(--danger)", marginRight: "auto" }}>Not downloaded. The passwords can't be shown again.</span>}
      <button type="button" className="btn" onClick={() => (saved || warned ? onDone() : setWarned(true))}>{warned && !saved ? "Close anyway" : "Done"}</button>
      <button type="button" className="btn btn-primary" onClick={() => { downloadFile(filename, csvOf(rows), "text/csv;charset=utf-8"); setSaved(true); }}>
        <Icon name="download" /> Download CSV
      </button>
    </>
  );
}

function OnceWarning() {
  return (
    <Callout tone="warn" icon="warn" role="status" title="Shown only once.">
      Download the CSV now and send each password privately. Anyone with the file can sign in as these users.
    </Callout>
  );
}

function AddUsersDialog({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const [emails, setEmails] = useState("");
  const [admin, setAdmin] = useState(false);
  const [access, setAccess] = useState<Access>({});
  const [creds, setCreds] = useState<Credential[] | null>(null);
  const [, setCopied] = useState(false);
  const list = useMemo(() => Array.from(new Set(emails.split(/[\s,;]+/).map((e) => e.trim().toLowerCase()).filter(Boolean))), [emails]);
  const bad = list.filter((e) => !EMAIL_RE.test(e));

  const create = useMutation({
    mutationFn: () => request<{ credentials: Credential[] }>("/admin/users/bulk", {
      method: "POST",
      body: { users: list.map((u) => ({ username: u, admin, clusters: admin ? {} : clean(access) })) },
    }),
    onSuccess: (r) => { setCreds(r.credentials); qc.invalidateQueries({ queryKey: ["admin-users"] }); },
  });
  const err = create.error instanceof ApiError ? create.error : null;
  const existing = (err?.details as { existing?: string[]; duplicates?: string[] } | null)?.existing;

  if (creds) {
    return (
      <Dialog title={`${creds.length} user${creds.length === 1 ? "" : "s"} created`} subtitle="They can sign in now with these passwords." onClose={onClose} wide
        footer={<CredentialsFooter rows={creds} filename="new-users-credentials.csv" onDone={onClose} />}>
        <OnceWarning />
        {creds.length ? <CredentialsTable rows={creds} onCopy={() => setCopied(true)} /> : <Callout>Password sign-in is off on this server, so no passwords were generated.</Callout>}
        <span className="hint">The CSV has two columns, <code>username,password</code>. Users can change their password after signing in (optional).</span>
      </Dialog>
    );
  }
  return (
    <Dialog title="Add users" subtitle="Passwords are generated for you." onClose={onClose} wide busy={create.isPending}
      footer={<>
        <button type="button" className="btn" onClick={onClose} disabled={create.isPending}>Cancel</button>
        <button type="button" className="btn btn-primary" disabled={!list.length || bad.length > 0 || create.isPending} onClick={() => create.mutate()}>
          {create.isPending ? "Creating…" : `Create ${list.length || ""} user${list.length === 1 ? "" : "s"}`}
        </button>
      </>}>
      <div className="field">
        <label htmlFor="emails" className="label" style={{ fontSize: 14 }}>Email addresses</label>
        <textarea id="emails" className="textarea mono" rows={4} value={emails} onChange={(e) => setEmails(e.target.value)} placeholder={"priya@fenixcommerce.com\nsam@fenixcommerce.com"} spellCheck={false} />
        <span className="hint">One per line (commas work too). Each person gets the same access below; you can change it per user afterwards.</span>
        {bad.length > 0 && <span className="hint" style={{ color: "var(--danger)" }}>Not an email address: {bad.join(", ")}</span>}
      </div>
      <div className="stack-sm" style={{ gap: 10 }}>
        <span className="label" style={{ fontSize: 14 }}>Clusters they can read</span>
        {admin ? <span className="hint">Admins read every cluster.</span> : <ClusterAccess value={access} onChange={setAccess} />}
        <label className="check"><input type="checkbox" checked={admin} onChange={(e) => setAdmin(e.target.checked)} /> Make admin</label>
      </div>
      <Callout icon="lock">A 16-character password is generated for each person. You'll see it once, on the next screen, and can download it as a CSV.</Callout>
      {err && (existing?.length ? (
        <Callout tone="danger" title="Some users already exist · nothing was created">{existing.join(", ")}. Use “Reset password” for existing users.</Callout>
      ) : <ErrorCallout error={err} />)}
    </Dialog>
  );
}

function ResetPasswordDialog({ username, onClose }: { username: string; onClose: () => void }) {
  const qc = useQueryClient();
  const [creds, setCreds] = useState<Credential[] | null>(null);
  const reset = useMutation({
    mutationFn: () => request<{ credentials: Credential }>(`/admin/users/${enc(username)}/reset-password`, { method: "POST" }),
    onSuccess: (r) => { setCreds([r.credentials]); qc.invalidateQueries({ queryKey: ["admin-users"] }); },
  });
  if (creds) {
    return (
      <Dialog title="Password reset" subtitle="The old password and all of this user's sessions no longer work." onClose={onClose} wide
        footer={<CredentialsFooter rows={creds} filename={`${username}-credentials.csv`} onDone={onClose} />}>
        <OnceWarning />
        <CredentialsTable rows={creds} onCopy={() => {}} />
      </Dialog>
    );
  }
  return (
    <Dialog title={`Reset password for ${username}?`} onClose={onClose} busy={reset.isPending}
      footer={<>
        <button type="button" className="btn" onClick={onClose} disabled={reset.isPending}>Cancel</button>
        <button type="button" className="btn btn-primary" onClick={() => reset.mutate()} disabled={reset.isPending}>{reset.isPending ? "Resetting…" : "Generate new password"}</button>
      </>}>
      <p>A new password is generated and shown once. Their current password stops working and they are signed out everywhere. This also unlocks a locked account.</p>
      {reset.error && <ErrorCallout error={reset.error} />}
    </Dialog>
  );
}
