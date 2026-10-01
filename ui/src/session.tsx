import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, type ReactNode } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";
import { ApiError, get, setUnauthenticatedHandler, type Cluster, type Me, type SavedSearch } from "./api";
import { Loading } from "./components/ui";

export function useMe() {
  return useQuery({ queryKey: ["me"], queryFn: () => get<Me>("/me"), staleTime: 60_000, retry: false });
}

export function useClusters() {
  return useQuery({ queryKey: ["clusters"], queryFn: () => get<{ items: Cluster[] }>("/clusters").then((r) => r.items), staleTime: 60_000 });
}

export function useSaved() {
  return useQuery({ queryKey: ["saved"], queryFn: () => get<{ items: SavedSearch[] }>("/saved-searches").then((r) => r.items), staleTime: 60_000 });
}

const LAST_CLUSTER = "vlg.lastCluster";
export function rememberCluster(id: string) {
  try {
    localStorage.setItem(LAST_CLUSTER, id);
  } catch {
    /* storage unavailable: fine */
  }
}
export function lastCluster(): string | null {
  try {
    return localStorage.getItem(LAST_CLUSTER);
  } catch {
    return null;
  }
}

/** Gate for signed-in pages: loads /me, sends signed-out visitors to /login. */
export function RequireAuth({ children }: { children: ReactNode }) {
  const me = useMe();
  const qc = useQueryClient();
  const navigate = useNavigate();
  const location = useLocation();

  useEffect(() => {
    setUnauthenticatedHandler(() => {
      qc.clear();
      const next = location.pathname + location.search;
      navigate(`/login?next=${encodeURIComponent(next)}&expired=1`, { replace: true });
    });
    return () => setUnauthenticatedHandler(() => {});
  }, [qc, navigate, location]);

  if (me.isLoading) return <div style={{ paddingTop: "30vh" }}><Loading what="Signing you in…" /></div>;
  if (me.error) {
    const e = me.error;
    if (e instanceof ApiError && e.status === 401) {
      const next = location.pathname + location.search;
      return <Navigate to={`/login${next && next !== "/" ? `?next=${encodeURIComponent(next)}` : ""}`} replace />;
    }
    return (
      <div className="login-side" style={{ minHeight: "100%" }}>
        <div className="login-card">
          <h2>Can't load your account</h2>
          <p className="sub">{(e as Error).message}</p>
          <button className="btn btn-primary" onClick={() => me.refetch()}>Try again</button>
        </div>
      </div>
    );
  }
  return <>{children}</>;
}

export function RequireAdmin({ children }: { children: ReactNode }) {
  const me = useMe().data;
  if (!me?.admin) return <Navigate to="/" replace />;
  return <>{children}</>;
}
