import { Link, Navigate, Route, Routes } from "react-router-dom";
import { Page, Shell } from "./components/Shell";
import { Empty, Loading } from "./components/ui";
import { Clusters } from "./pages/admin/Clusters";
import { Users } from "./pages/admin/Users";
import { ChangePassword } from "./pages/ChangePassword";
import { Login } from "./pages/Login";
import { Health } from "./pages/Health";
import { LogView } from "./pages/LogView";
import { Overview } from "./pages/Overview";
import { Saved } from "./pages/Saved";
import { lastCluster, RequireAdmin, RequireAuth, useClusters, useMe } from "./session";

function Home() {
  const clusters = useClusters();
  const me = useMe().data!;
  if (clusters.isLoading) return <Loading />;
  const items = clusters.data ?? [];
  const remembered = lastCluster();
  const target = items.find((c) => c.id === remembered) ?? items[0];
  if (target) return <Navigate to={`/logs/${encodeURIComponent(target.id)}`} replace />;
  return (
    <Page crumbs={[{ label: "Home" }]} title="No clusters">
      <section className="card">
        <Empty title={me.admin ? "No cluster folders found in S3" : "You don't have access to any cluster yet"}>
          {me.admin ? <>Check the logs location on the <Link to="/admin/clusters">Clusters</Link> page.</> : "Ask an admin to give you access."}
        </Empty>
      </section>
    </Page>
  );
}

function NotFound() {
  return (
    <Page crumbs={[{ label: "Not found" }]} title="Not found">
      <section className="card">
        <Empty title="This page doesn't exist"><Link to="/">Go to the logs</Link></Empty>
      </section>
    </Page>
  );
}

export function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route element={<RequireAuth><Shell /></RequireAuth>}>
        <Route index element={<Home />} />
        <Route path="logs/:clusterId" element={<LogView />} />
        <Route path="overview/:clusterId" element={<Overview />} />
        <Route path="saved" element={<Saved />} />
        <Route path="health" element={<Health />} />
        <Route path="account/password" element={<ChangePassword />} />
        <Route path="admin/clusters" element={<RequireAdmin><Clusters /></RequireAdmin>} />
        <Route path="admin/users" element={<RequireAdmin><Users /></RequireAdmin>} />
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  );
}
