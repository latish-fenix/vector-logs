import { useMutation, useQueryClient } from "@tanstack/react-query";
import { createContext, Fragment, useContext, useEffect, useId, useState, type ReactNode } from "react";
import { Link, NavLink, Outlet, useLocation, useNavigate, useParams } from "react-router-dom";
import { request } from "../api";
import { lastCluster, rememberCluster, useClusters, useMe } from "../session";
import { Icon, type IconName } from "./icons";
import { usePageTitle } from "./ui";

const NavCtx = createContext<() => void>(() => {});

function NavItem({ to, icon, label, end }: { to: string; icon: IconName; label: string; end?: boolean }) {
  return (
    <NavLink to={to} end={end} className={({ isActive }) => `nav-link ${isActive ? "active" : ""}`}>
      <Icon name={icon} strokeWidth={1.8} />
      <span>{label}</span>
    </NavLink>
  );
}

function ClusterSwitch({ current }: { current: string | undefined }) {
  const clusters = useClusters().data ?? [];
  const navigate = useNavigate();
  const location = useLocation();
  const id = useId();
  if (!clusters.length) return null;
  const value = current && clusters.some((c) => c.id === current) ? current : clusters[0].id;
  const onChange = (next: string) => {
    // Keep the search (time range, query, filters) when switching clusters.
    const section = location.pathname.startsWith("/overview/") ? "overview" : "logs";
    navigate(`/${section}/${encodeURIComponent(next)}${/^\/(logs|overview)\//.test(location.pathname) ? location.search : ""}`);
  };
  return (
    <div className="cluster-switch">
      <label htmlFor={id} className="cs-label">Cluster</label>
      <span className="cs-dot"><Icon name="server" size={14} /></span>
      <select id={id} value={value} onChange={(e) => onChange(e.target.value)}>
        {clusters.map((c) => <option key={c.id} value={c.id}>{c.id}</option>)}
      </select>
      <span className="cs-caret"><Icon name="caret" size={16} /></span>
    </div>
  );
}

export function Shell() {
  const me = useMe().data!;
  const params = useParams();
  const clusters = useClusters().data;
  const clusterId = params.clusterId ?? lastCluster() ?? clusters?.[0]?.id;
  const qc = useQueryClient();
  const navigate = useNavigate();
  const location = useLocation();
  const [navOpen, setNavOpen] = useState(false);
  useEffect(() => setNavOpen(false), [location.pathname]);
  useEffect(() => {
    if (params.clusterId) rememberCluster(params.clusterId);
  }, [params.clusterId]);

  const logout = useMutation({
    mutationFn: () => request("/auth/logout", { method: "POST" }),
    onSettled: () => {
      qc.clear();
      navigate("/login", { replace: true });
    },
  });

  return (
    <NavCtx.Provider value={() => setNavOpen((o) => !o)}>
    <div className="app">
      {navOpen && <div className="nav-scrim" onClick={() => setNavOpen(false)} aria-hidden="true" />}
      <nav className={`sidebar ${navOpen ? "open" : ""}`} aria-label="Main" id="main-nav">
        <Link to="/" className="brand" style={{ textDecoration: "none" }}>
          <span className="brand-mark"><Icon name="logo" strokeWidth={2.4} style={{ color: "#fff" }} /></span>
          <span className="stack-sm" style={{ gap: 0 }}>
            <span className="brand-name">Vector Logs</span>
            <span className="brand-sub">Log viewer</span>
          </span>
        </Link>
        <ClusterSwitch current={clusterId} />
        <div className="nav-group">
          <span className="nav-label">Logs</span>
          {clusterId && clusters?.some((c) => c.id === clusterId) && <>
            <NavItem to={`/logs/${encodeURIComponent(clusterId)}${location.pathname.startsWith("/overview/") ? location.search : ""}`} icon="terminal" label="Logs" />
            <NavItem to={`/overview/${encodeURIComponent(clusterId)}${location.pathname.startsWith("/logs/") ? location.search : ""}`} icon="overview" label="Overview" />
          </>}
          <NavItem to="/saved" icon="bookmark" label="Saved searches" />
        </div>
        <div className="nav-group">
          <span className="nav-label">Infrastructure</span>
          <NavItem to="/health" icon="activity" label="ECS health" />
        </div>
        {me.admin && (
          <div className="nav-group">
            <span className="nav-label">Administration</span>
            <NavItem to="/admin/clusters" icon="server" label="Clusters" />
            <NavItem to="/admin/users" icon="users" label="Users" />
          </div>
        )}
        <div className="grow" />
        <div className="user-box">
          <div className="who">
            <span className="avatar" aria-hidden="true">{me.username[0]}</span>
            <span className="stack-sm" style={{ gap: 0, minWidth: 0 }}>
              <span className="user-name" title={me.username}>{me.username}</span>
              <span className="user-role">{me.admin ? "Admin" : "Member"}</span>
            </span>
          </div>
          <div className="user-actions">
            {me.authMode === "password" && <Link to="/account/password" style={{ flex: 1 }}>Change password</Link>}
            <button type="button" onClick={() => logout.mutate()} disabled={logout.isPending}>Sign out</button>
          </div>
        </div>
      </nav>
      <div className="main-col">
        <Outlet />
      </div>
    </div>
    </NavCtx.Provider>
  );
}

export interface Crumb {
  label: string;
  to?: string;
}

export function Page({ crumbs, title, actions, children, wide, fill }: {
  crumbs: Crumb[];
  title: string;
  actions?: ReactNode;
  children: ReactNode;
  wide?: boolean;
  /** The page fills the window (no page scroll); the content scrolls inside. */
  fill?: boolean;
}) {
  usePageTitle(title);
  const toggleNav = useContext(NavCtx);
  return (
    <>
      <header className="topbar">
        <button type="button" className="btn btn-ghost icon-btn menu-btn" aria-label="Menu" aria-controls="main-nav" onClick={toggleNav}>
          <Icon name="logo" />
        </button>
        <nav className="crumbs" aria-label="Breadcrumb">
          {crumbs.map((cr, i) => (
            <Fragment key={i}>
              {i > 0 && <span aria-hidden="true">/</span>}
              {cr.to && i < crumbs.length - 1 ? <Link to={cr.to}>{cr.label}</Link> : <span className={i === crumbs.length - 1 ? "here" : ""} aria-current={i === crumbs.length - 1 ? "page" : undefined}>{cr.label}</span>}
            </Fragment>
          ))}
        </nav>
        {actions}
      </header>
      <main className={`page ${fill ? "fill" : ""}`} id="main">
        <div className={`page-inner ${wide || fill ? "wide" : ""}`}>{children}</div>
      </main>
    </>
  );
}
