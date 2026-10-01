import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { enc, get, type AdminCluster } from "../../api";
import { Icon } from "../../components/icons";
import { Page } from "../../components/Shell";
import { Badge, Empty, ErrorCallout, Loading, Spinner } from "../../components/ui";
import { num } from "../../format";

interface AdminClusters {
  items: AdminCluster[];
  source: string;
  cache: { files?: number; mb?: number; maxMb?: number; backend?: string };
}

export function Clusters() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["admin-clusters"], queryFn: () => get<AdminClusters>("/admin/clusters") });
  const refresh = async () => {
    const data = await get<AdminClusters>("/admin/clusters", { refresh: true });
    qc.setQueryData(["admin-clusters"], data);
    qc.invalidateQueries({ queryKey: ["clusters"] });
  };
  const d = q.data;
  return (
    <Page crumbs={[{ label: "Administration" }, { label: "Clusters" }]} title="Clusters">
      <div className="page-head" style={{ alignItems: "center" }}>
        <div className="grow">
          <h1>Clusters</h1>
          <span className="sub">Every folder under the logs prefix is a cluster; new folders appear here by themselves (the list refreshes every 5 minutes). Give people access in <Link to="/admin/users">Users</Link>.</span>
        </div>
        <button type="button" className="btn" onClick={refresh} disabled={q.isFetching}>{q.isFetching ? <Spinner /> : <Icon name="refresh" />} Refresh from S3</button>
      </div>
      {d && (
        <div className="stats" style={{ gridTemplateColumns: "repeat(3, minmax(0, 1fr))" }}>
          <div className="stat"><span className="stat-label">Logs location</span><span className="mono" style={{ fontSize: 14, overflowWrap: "anywhere" }}>{d.source}</span></div>
          <div className="stat"><span className="stat-label">Cluster folders</span><span className="stat-value">{num(d.items.length)}</span></div>
          <div className="stat"><span className="stat-label">Server cache</span>
            <span className="stat-value">{d.cache.backend === "local" ? "local folder" : `${num(d.cache.mb ?? 0)} MB`}</span>
            {d.cache.maxMb ? <span className="stat-note">{num(d.cache.files ?? 0)} files · limit {num(d.cache.maxMb)} MB (CACHE_MAX_MB)</span> : null}
          </div>
        </div>
      )}
      <section className="card" style={{ overflow: "hidden" }}>
        {q.isLoading ? <Loading /> : q.error ? <div className="card-body"><ErrorCallout error={q.error} admin /></div> : !d?.items.length ? (
          <Empty title="No cluster folders found">Vector writes to <span className="mono">{d?.source}</span>&lt;cluster&gt;/dt=…/hour=…/. Check LOGS_BUCKET and LOGS_PREFIX in .env.</Empty>
        ) : (
          <table className="table">
            <thead><tr><th>Cluster</th><th>Members with access</th><th><span className="sr-only">Open</span></th></tr></thead>
            <tbody>
              {d.items.map((c) => (
                <tr key={c.id}>
                  <td className="mono" style={{ fontWeight: 600 }}>{c.id}</td>
                  <td>
                    {c.users.length ? (
                      <span className="row" style={{ gap: 4, flexWrap: "wrap" }}>{c.users.map((u) => <Badge key={u}>{u}</Badge>)}</span>
                    ) : <span className="hint">Only admins</span>}
                  </td>
                  <td style={{ textAlign: "right" }}><Link className="btn btn-ghost btn-sm" to={`/logs/${enc(c.id)}`}>Open logs <Icon name="chevron" size={14} /></Link></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </Page>
  );
}
