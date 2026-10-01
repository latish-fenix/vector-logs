import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { enc, request, type LogFilter, type SavedSearch } from "../api";
import { Icon } from "../components/icons";
import { Page } from "../components/Shell";
import { Empty, ErrorCallout, Loading, copyText, useToast } from "../components/ui";
import { ago } from "../format";
import { useClusters, useSaved } from "../session";

function summary(params: string): string {
  const p = new URLSearchParams(params);
  const parts: string[] = [];
  const start = p.get("start") || "now-1h";
  const end = p.get("end") || "now";
  parts.push(end === "now" && start.startsWith("now-") ? `last ${start.slice(4)}` : `${start} → ${end}`);
  if (p.get("q")) parts.push(`“${p.get("q")}”`);
  try {
    const f = JSON.parse(p.get("f") || "[]") as LogFilter[];
    if (f.length) parts.push(`${f.length} filter${f.length === 1 ? "" : "s"}: ${f.map((x) => `${x.field} ${x.op.replace("_", " ")} ${x.value ?? (x.values ?? []).join(",")}`).join("; ")}`);
  } catch {
    /* ignore */
  }
  return parts.join(" · ");
}

export function Saved() {
  const saved = useSaved();
  const clusters = useClusters().data ?? [];
  const qc = useQueryClient();
  const toast = useToast();
  const [confirm, setConfirm] = useState<string | null>(null);
  const remove = useMutation({
    mutationFn: (id: string) => request(`/saved-searches/${enc(id)}`, { method: "DELETE" }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["saved"] }); setConfirm(null); toast("Deleted"); },
  });
  const link = (s: SavedSearch) => `/logs/${enc(s.cluster)}?${s.params}`;
  const items = saved.data ?? [];
  return (
    <Page crumbs={[{ label: "Saved searches" }]} title="Saved searches">
      <div className="page-head">
        <div className="grow">
          <h1>Saved searches</h1>
          <p className="sub">Your own searches, kept on the server. Only you see this list; to share one with a colleague, copy its link (they need access to the same cluster).</p>
        </div>
      </div>
      <section className="card" style={{ overflow: "hidden" }}>
        {saved.isLoading ? <Loading /> : saved.error ? <div className="card-body"><ErrorCallout error={saved.error} /></div> : items.length === 0 ? (
          <Empty title="No saved searches yet">On the Search page, set up a search and click <strong>Save search</strong>.</Empty>
        ) : (
          <div className="table-scroll">
            <table className="table">
              <thead><tr><th>Name</th><th>Cluster</th><th>Search</th><th>Saved</th><th><span className="sr-only">Actions</span></th></tr></thead>
              <tbody>
                {items.map((s) => {
                  const gone = !clusters.some((c) => c.id === s.cluster);
                  return (
                    <tr key={s.id}>
                      <td style={{ fontWeight: 600 }}>{gone ? s.name : <Link to={link(s)}>{s.name}</Link>}</td>
                      <td className="mono nowrap">{s.cluster}{gone && <span className="hint"> · no access</span>}</td>
                      <td className="hint" style={{ maxWidth: 520 }}>{summary(s.params)}</td>
                      <td className="hint nowrap">{ago(s.updatedAt)}</td>
                      <td className="nowrap" style={{ textAlign: "right" }}>
                        {confirm === s.id ? (
                          <span className="row" style={{ gap: 6, justifyContent: "flex-end" }}>
                            <button type="button" className="btn btn-sm" onClick={() => setConfirm(null)}>Keep</button>
                            <button type="button" className="btn btn-danger btn-sm" disabled={remove.isPending} onClick={() => remove.mutate(s.id)}>Delete</button>
                          </span>
                        ) : (
                          <span className="row" style={{ gap: 4, justifyContent: "flex-end" }}>
                            <button type="button" className="btn btn-ghost btn-sm" onClick={() => copyText(window.location.origin + "/ui" + link(s)).then(() => toast("Link copied"))}><Icon name="link" size={15} /> Copy link</button>
                            <button type="button" className="btn btn-ghost btn-sm danger" aria-label={`Delete ${s.name}`} onClick={() => setConfirm(s.id)}><Icon name="trash" size={15} /></button>
                          </span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        {remove.error && <div className="card-body"><ErrorCallout error={remove.error} /></div>}
      </section>
    </Page>
  );
}
