// Tenant app shell: white sidebar, grouped nav with icons, workspace switcher,
// user footer. Light, spacious, production-grade. Guards child routes.
//
// The nav block has two modes (§1.4). Workspace mode is the list below. Agent
// mode is owned by `AgentLayout`, which portals its sub-nav into the host div
// this shell renders in the nav's place — brand mark, workspace switcher and
// user footer stay put, so the chrome does not jump when you open an
// agent. Deciding the mode here rather than after AgentLayout mounts is what
// stops the workspace items flashing on the way in.
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { logout, landingUrl, switchTenant, tenantSession } from "../../lib/auth";
import { Icon } from "../../components/ui/icons";
import { cx, ThemeToggle, Avatar, Spinner } from "../../components/ui";
import { GlobalProgress } from "../../components/GlobalProgress";
import { GlobalSettleToast } from "../../components/GlobalSettleToast";
import { prefetchFor } from "../../lib/prefetch";

type NavLinkSpec = [string, string, ReactNode];

// Workspace nav per §1.4. Agent-scoped screens (sources, channels, agent
// settings) left this list when they moved under /agents/:agentId. The list
// below carries only the screens that are still mounted, so nothing here
// dead-ends.
const TOP_LINKS: NavLinkSpec[] = [
  ["/agents", "Agents", <Icon.Spark size={17} />],
];

const GROUPS: { label: string; links: NavLinkSpec[] }[] = [
  // Growth sits above Workspace because it is where the daily work is: an
  // approval waiting on a decision is time-sensitive in a way that nothing in
  // Workspace is. Deliberately not gated on `tenant_type` — a nav section that
  // appears only once you have configured it is a section nobody can find the
  // first time.
  {
    label: "Growth", links: [
      ["/growth/approvals", "Approvals", <Icon.Check size={17} />],
      ["/growth/reports", "Reports", <Icon.Chart size={17} />],
      ["/growth/integrations", "Connections", <Icon.Plug size={17} />],
      ["/growth/settings", "Growth settings", <Icon.Settings size={17} />],
    ]
  },
  {
    label: "Workspace", links: [
      ["/knowledge", "Knowledge", <Icon.Book size={17} />],
      ["/visualise", "Visualise", <Icon.Bolt size={17} />],
      ["/audit", "Audit log", <Icon.Shield size={17} />],
    ]
  },
];

// The element AgentLayout portals its sub-nav into. Null in workspace mode, and
// null for one frame on first entry into an agent — the consumer renders nothing
// until it lands, which is a slot that is briefly empty, never a wrong nav.
const SidebarNavHostContext = createContext<HTMLElement | null>(null);

export function useSidebarNavHost(): HTMLElement | null {
  return useContext(SidebarNavHostContext);
}

export const NAV_ITEM_CLASS =
  "flex items-center gap-2.5 px-2.5 py-2 rounded-md text-sm font-medium transition-colors no-underline";
export const NAV_GROUP_CLASS =
  "text-[11px] uppercase tracking-wide font-semibold text-faint px-2 pt-3 pb-1";

/** One nav row, shared by both sidebar modes so the two cannot drift apart. */
export function SidebarLink({ to, label, icon, end }: { to: string; label: ReactNode; icon?: ReactNode; end?: boolean }) {
  return (
    <NavLink
      to={to}
      end={end}
      onMouseEnter={() => prefetchFor(to)}
      onFocus={() => prefetchFor(to)}
      className={({ isActive }) => cx(
        NAV_ITEM_CLASS,
        isActive ? "bg-brand-soft text-brand" : "text-muted hover:text-ink hover:bg-canvas",
      )}
    >
      {icon && <span className="shrink-0">{icon}</span>}{label}
    </NavLink>
  );
}

export function AppLayout() {
  const { pathname } = useLocation();
  const session = tenantSession();
  const [navHost, setNavHost] = useState<HTMLDivElement | null>(null);
  const [switching, setSwitching] = useState(false);
  // Tenant switch is a hard reload (the whole shell re-scopes). Block behind an
  // overlay for one frame so the reload does not read as a dead click.
  const switchWorkspace = (slug: string) => {
    switchTenant(slug);
    setSwitching(true);
    window.setTimeout(() => location.reload(), 150);
  };
  // No login route exists here — sign-in lives on the API-origin landing page.
  useEffect(() => { if (!session) location.href = landingUrl(); }, [session]);
  if (!session) return null;

  const active = session.tenants.find((t) => t.slug === session.activeSlug) ?? session.tenants[0];

  // /agents/new is the create flow, not an agent: it is a sibling route and it
  // keeps the workspace nav.
  const scoped = /^\/agents\/([^/]+)/.exec(pathname);
  const agentMode = !!scoped && scoped[1] !== "new";

  return (
    <SidebarNavHostContext.Provider value={navHost}>
      <GlobalProgress />
      <GlobalSettleToast />
      {switching && (
        <div className="fixed inset-0 z-[80] bg-canvas/80 grid place-items-center">
          <div className="flex flex-col items-center gap-3">
            <Spinner />
            <span className="text-sm font-medium text-ink">Switching workspace…</span>
          </div>
        </div>
      )}
      <div className="grid grid-cols-[264px_minmax(0,1fr)] h-screen">
        <aside className="flex flex-col bg-surface border-r border-line px-3 py-4 overflow-y-auto">
          <div className="flex items-center gap-2.5 px-2 pb-3 font-bold text-[15px] tracking-tight">
            <span className="w-7 h-7 rounded-lg bg-primary text-primary-fg grid place-items-center font-extrabold">K</span>
            kado
          </div>

          {/* Workspace switcher — always visible, mirrors the reference top-of-sidebar control. */}
          <div className="mb-2">
            {session.tenants.length > 1 ? (
              <div className="relative">
                <select
                  value={session.activeSlug ?? ""}
                  onChange={(e) => switchWorkspace(e.target.value)}
                  className="w-full appearance-none rounded-lg border border-line bg-surface hover:bg-canvas text-ink text-sm font-semibold pl-2.5 pr-8 py-2 cursor-pointer transition-colors"
                >
                  {session.tenants.map((t) => <option key={t.slug} value={t.slug}>{t.name}</option>)}
                </select>
                <span className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 text-faint"><Icon.Selector size={16} /></span>
              </div>
            ) : (
              <div className="flex items-center justify-between gap-2 rounded-lg border border-line px-2.5 py-2">
                <div className="min-w-0">
                  <div className="text-sm font-semibold truncate">{active?.name ?? "Workspace"}</div>
                </div>
                <Icon.Selector size={16} className="text-faint shrink-0" />
              </div>
            )}
          </div>

          {/* Both branches stay mounted; only one is visible. The agent subnav
              is portaled into the host div below (AgentLayout), and React's
              commitDeletion cannot remove a portal child from a host that a
              sibling unmount removed in the same pass — keep the node alive and
              hide it instead, so the portal target never disappears under
              React. */}
          <nav className={cx("flex flex-col gap-0.5", agentMode && "hidden")}>
            <div className="mb-1">
              {TOP_LINKS.map(([to, label, icon]) => (
                <SidebarLink key={to} to={to} label={label} icon={icon} />
              ))}
            </div>
            {GROUPS.map((g) => (
              <div key={g.label} className="mb-1">
                <div className={NAV_GROUP_CLASS}>{g.label}</div>
                {g.links.map(([to, label, icon]) => (
                  <SidebarLink key={to} to={to} label={label} icon={icon} />
                ))}
              </div>
            ))}
          </nav>
          <div ref={setNavHost} className={cx("flex flex-col gap-0.5", !agentMode && "hidden")} />

          <div className="flex-1" />

          <div className="border-t border-line pt-3">
            <div className="flex items-center gap-2.5 mb-2">
              <Avatar name={active?.name} size={32} />
              <div className="overflow-hidden">
                <div className="font-semibold text-sm truncate">{active?.name ?? "Workspace"}</div>
                <div className="text-xs text-muted truncate">{session.email || active?.role}</div>
              </div>
            </div>
            <button onClick={logout} className="flex items-center gap-2 w-full px-2.5 py-1.5 rounded-md text-sm text-muted hover:text-ink hover:bg-canvas transition-colors">
              <Icon.Logout size={16} /> Sign out
            </button>
            <ThemeToggle />
          </div>
        </aside>

        <main className="overflow-y-auto flex flex-col min-w-0">
          <div className="max-w-[1200px] w-full mx-auto px-8 py-8 min-w-0"><Outlet /></div>
        </main>
      </div>
    </SidebarNavHostContext.Provider>
  );
}
