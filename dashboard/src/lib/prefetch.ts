// Route chunk-warmers, extracted out of main.tsx (F1 / §8.3 R1).
//
// Both Layout shells used to import `prefetch` from main.tsx at module-eval
// time, so the shell depended on the module that mounts it. That cycle does not
// break one page, it breaks the app: an unlucky hoisting order leaves the
// sidebar with an undefined map before a single route renders. This module
// imports nothing, so the cycle cannot exist.
//
// Every entry uses the *same* module specifier as main.tsx's React.lazy call.
// Because the specifier is identical, Vite serves both from one chunk — calling
// a warmer on nav hover/focus loads the module, and the later click resolves it
// from cache instead of paying a fresh network fetch.
//
// The `pre*` functions are deliberately left un-annotated: main.tsx's `page()`
// adapter recovers the exact module type from the inferred return type, which a
// `Record<string, () => Promise<unknown>>` annotation would erase.

export const preAgentsIndex = () => import("../routes/app/AgentsIndex");
export const preAgentCreate = () => import("../routes/app/AgentCreate");
export const preDocumentDetail = () => import("../routes/app/DocumentDetail");
export const preInstructions = () => import("../routes/app/agent/Instructions");
export const preAgentSettings = () => import("../routes/app/agent/Settings");
export const preSources = () => import("../routes/app/agent/Sources");
export const preLinkReview = () => import("../routes/app/agent/SourceLinkReview");
export const preChannels = () => import("../routes/app/agent/Channels");
export const preAppAuditLog = () => import("../routes/app/AuditLog");
export const preVisualise = () => import("../routes/app/Visualise");

// Growth platform (migrations 016-018): the tenant screens under /growth.
export const preGrowthSettings = () => import("../routes/app/growth/Settings");
export const preGrowthIntegrations = () => import("../routes/app/growth/Integrations");
export const preGrowthApprovals = () => import("../routes/app/growth/Approvals");
export const preGrowthReports = () => import("../routes/app/growth/Reports");

// Nav path → chunk-warmer. Keys are the exact route paths used in the nav.
export const prefetch: Record<string, () => Promise<unknown>> = {
  "/agents": preAgentsIndex,
  // The create flow's own key. It is warmed from the index's three "new agent"
  // call sites, which is the only way into it — and the one place where a cold
  // chunk fetch lands on an empty screen instead of over existing content.
  "/agents/new": preAgentCreate,
  "/audit": preAppAuditLog,
  "/knowledge": preSources,
  "/visualise": preVisualise,
  "/growth/settings": preGrowthSettings,
  "/growth/integrations": preGrowthIntegrations,
  "/growth/approvals": preGrowthApprovals,
  "/growth/reports": preGrowthReports,
};

// Agent-scoped nav paths carry an id, so exact-key lookup cannot work. Keys here
// are the trailing segment after `/agents/:agentId/`; `prefetchFor` strips the
// prefix before looking one up.
export const agentPrefetch: Record<string, () => Promise<unknown>> = {
  instructions: preInstructions,
  settings: preAgentSettings,
  channels: preChannels,
  sources: preSources,
  // Trailing slash, the detail-route convention: the review screen is warmed
  // from a row link that passes `sources/`, not from a sidebar item. Without
  // the slash the suffix fallback in prefetchFor would resolve `sources/<uuid>`
  // back to the list screen.
  "sources/": preLinkReview,
  "sources/document": preDocumentDetail,
};

// The one entry point both shells call, so a nav item never has to know which
// map it lives in. Exact match first, then the agent-scoped suffix; a path with
// no entry is a no-op rather than a throw, so adding a nav item before its
// warmer never breaks the shell.
export function prefetchFor(to: string): void {
  const direct = prefetch[to];
  if (direct) { void direct(); return; }
  // Workspace-level detail routes under /knowledge share the suffix convention
  // with the agent-scoped ones below.
  const knowledge = /^\/knowledge\/(.+)$/.exec(to);
  if (knowledge) {
    const suffix = knowledge[1]!;
    void (knowledgePrefetch[suffix]
      ?? knowledgePrefetch[suffix.slice(0, suffix.lastIndexOf("/"))])?.();
    return;
  }
  const scoped = /^\/agents\/[^/]+\/(.+)$/.exec(to);
  if (!scoped) return;
  const suffix = scoped[1]!;
  const warm = agentPrefetch[suffix]
    // Detail routes carry their own id (`sources/document/<uuid>`), so fall back
    // to the longest registered prefix rather than missing the warm entirely.
    ?? agentPrefetch[suffix.slice(0, suffix.lastIndexOf("/"))];
  void warm?.();
}

/** Trailing segments after /knowledge/ for the workspace-level detail routes. */
const knowledgePrefetch: Record<string, () => Promise<unknown>> = {
  "sources/": preLinkReview,
  "sources/document": preDocumentDetail,
};
