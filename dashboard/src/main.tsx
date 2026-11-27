// Mounts the router. The tenant app is guarded by its layout so an
// unauthenticated render redirects rather than flashing protected content.
//
// Perf: the Layout and Login gate everything so they load eagerly; every inner
// inner screen is code-split via React.lazy so the initial bundle stays small
// and each route's JS is fetched on demand. A <Suspense> boundary around the
// Outlet shows a full-page skeleton while a chunk loads.

import { StrictMode, lazy, Suspense, type ComponentType } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter, Navigate, RouterProvider } from "react-router-dom";

import "./styles/tailwind.css";
import "./styles/app.css";
import "./lib/theme";

// Staff console removed — single-tenant build.
import { AppLayout } from "./routes/app/Layout";
// Eager, alongside AppLayout: AgentLayout is the shell that paints the agent
// sub-nav, and lazy-loading it would flicker the sidebar on every entry into an
// agent.
import { AgentLayout } from "./routes/app/agent/AgentLayout";
// No login route in the SPA — sign-in lives on the API-origin landing page.
// routes/Login.tsx is kept unmounted for restore; a handoff session is adopted
// below via adoptHandoffSession().
import { adoptHandoffSession } from "./lib/auth";
import { PageSkeleton } from "./components/ui";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { ToastProvider } from "./components/ui/Toast";
import {
  preAgentCreate,
  preAgentSettings,
  preAgentsIndex,
  preAppAuditLog,
  preVisualise,
  preChannels,
  preDocumentDetail,
  preGrowthApprovals,
  preGrowthIntegrations,
  preGrowthReports,
  preGrowthSettings,
  preInstructions,
  preLinkReview,
  preSources,
} from "./lib/prefetch";

// Named exports become lazy default modules via a tiny adapter. The prop type is
// left open — `ComponentType<any>` is exactly what React.lazy accepts — because
// every mounted screen takes no props, which a narrower constraint would reject
// at the import rather than at the call site.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
function page<T extends Record<string, ComponentType<any>>>(
  loader: () => Promise<T>,
  name: keyof T,
) {
  return lazy(() => loader().then((m) => ({ default: m[name] })));
}

// Each route is (a) code-split via React.lazy and (b) given a matching prefetch
// function that uses the *same* module specifier. Because the specifier is
// identical, Vite serves both from one chunk — so calling the prefetch fn on
// nav hover/focus warms the module cache, and the later click resolves the
// already-loaded chunk instantly instead of paying a fresh network fetch.
// The warmers live in lib/prefetch.ts (F1 / §8.3 R1) so the Layout shells do
// not import from this module: a shell that depends on its own mount point is
// a cycle, and a cycle here blanks the app rather than one page.

const AgentsIndex = page(preAgentsIndex, "AgentsIndex");
const AgentCreate = page(preAgentCreate, "AgentCreate");
const DocumentDetail = page(preDocumentDetail, "DocumentDetail");
const Instructions = page(preInstructions, "Instructions");
const Sources = page(preSources, "Sources");
const Knowledge = page(preSources, "Knowledge");
const SourceLinkReview = page(preLinkReview, "SourceLinkReview");
const AgentSettings = page(preAgentSettings, "AgentSettings");
const Channels = page(preChannels, "Channels");
// EscalationPanel deliberately has no route: it mounts inside Instructions.tsx
// next to the safety card, where the handoff rules it edits already live.
const AppAuditLog = page(preAppAuditLog, "AuditLog");
const Visualise = page(preVisualise, "Visualise");
const GrowthSettings = page(preGrowthSettings, "GrowthSettings");
const GrowthIntegrations = page(preGrowthIntegrations, "GrowthIntegrations");
const GrowthApprovals = page(preGrowthApprovals, "GrowthApprovals");
const GrowthReports = page(preGrowthReports, "GrowthReports");

const NotFound = page(() => import("./routes/NotFound"), "NotFound");

// Wrap a lazy element in its own Suspense boundary so the eager Layout shell
// (sidebar, nav) stays painted while only the inner screen shows a skeleton.
// The fallback is overridable because PageSkeleton is shaped like a three-stat
// list page: right for the sources list, wrong for Instructions, where the
// mismatch shows up as a visible layout jump when the chunk lands.
function suspend(el: React.ReactNode, fallback: React.ReactNode = <PageSkeleton />) {
  return <Suspense fallback={fallback}>{el}</Suspense>;
}

const router = createBrowserRouter([
  {
    path: "/",
    element: <AppLayout />,
    children: [
      // The first thing a returning user sees is their agents, not a file list.
      { index: true, element: <Navigate to="/agents" replace /> },

      { path: "agents", element: suspend(<AgentsIndex />) },

      // Declared before "agents/:agentId" so /agents/new is the create flow, not
      // an agent whose id happens to be "new". AppLayout already excludes it from
      // agent-scoped chrome for the same reason.
      { path: "agents/new", element: suspend(<AgentCreate />) },

      // Each leaf keeps its own suspend() boundary so the agent sub-nav stays
      // painted while a section chunk loads.
      {
        path: "agents/:agentId",
        element: <AgentLayout />,
        children: [
          { index: true, element: <Navigate to="instructions" replace /> },
          { path: "instructions", element: suspend(<Instructions />) },
          { path: "settings", element: suspend(<AgentSettings />) },
          { path: "channels", element: suspend(<Channels />) },
          // Static before dynamic. react-router scores static segments above
          // parametric ones, so "sources/document/:id" wins over
          // "sources/:sourceId" regardless of order — the order is explicit
          // because that is how the rest of this file reads.
          { path: "sources", element: suspend(<Sources />) },
          { path: "sources/:sourceId", element: suspend(<SourceLinkReview />) },
          { path: "sources/document/:id", element: suspend(<DocumentDetail />) },
        ],
      },

      { path: "audit", element: suspend(<AppAuditLog />) },

      // Universal knowledge base (P2): workspace-level mounts of the sources
      // machinery. Static before dynamic, same rule as the agent tree below —
      // "sources/document/:id" wins over "sources/:sourceId" by segment score.
      { path: "knowledge", element: suspend(<Knowledge />) },
      { path: "knowledge/sources/:sourceId", element: suspend(<SourceLinkReview />) },
      { path: "knowledge/document/:id", element: suspend(<DocumentDetail />) },
      // The agent graph, read-only, for the owner.
      { path: "visualise", element: suspend(<Visualise />) },
      // Growth is a flat section rather than an agent-scoped one: a send cap
      // and an approval level belong to the workspace, not to one assistant,
      // and every growth agent reads the same settings row.
      { path: "growth/settings", element: suspend(<GrowthSettings />) },
      { path: "growth/integrations", element: suspend(<GrowthIntegrations />) },
      { path: "growth/approvals", element: suspend(<GrowthApprovals />) },
      { path: "growth/reports", element: suspend(<GrowthReports />) },
    ],
  },
  { path: "*", element: suspend(<NotFound />) },
],
{
  // The API serves the SPA under /app: its own "/" is the landing page and its
  // /login is the Google callback, so a root-mounted SPA would swallow both and
  // sign-in would 404. Vite reports its own `base` here as BASE_URL, so the dev
  // server ("/") and the container ("/app/") both work with no second source of
  // truth. Trailing slash stripped because react-router wants a bare prefix.
  basename: import.meta.env.BASE_URL.replace(/\/$/, ""),
});

// Adopt a session handed over from the landing page's sign-in form (URL
// fragment) before anything mounts, so the very first render already has it.
adoptHandoffSession();

// ToastProvider sits AROUND RouterProvider, not inside it (§8.3 R11): with
// createBrowserRouter there is no "inside" short of a root route element, and a
// provider that throws there blanks the whole app. useToast() degrades to no-ops
// without a provider, so the worst case here is silence.
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ToastProvider>
      <ErrorBoundary>
        <RouterProvider router={router} />
      </ErrorBoundary>
    </ToastProvider>
  </StrictMode>,
);
