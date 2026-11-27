// Agent-scoped shell (§4.1). Owns the agent identity (name, colour, default
// flag) and the sub-nav, so every section screen is just content. Fetches the
// summary once and shares it by context rather than each section refetching a
// name.
//
// It paints its chrome immediately and never blocks on the fetch: the sub-nav
// comes from useParams, and the name comes from navigation state, then a
// sessionStorage cache, then a Skeleton chip. A guessed-and-corrected name beats
// a shimmering rectangle where the name should be.
//
// The nav is rendered here but lives in AppLayout's sidebar — §1.4 replaces the
// nav block only — so it goes over by portal into the host div that shell
// exposes. Nav items appear as their screens land; a link to a route that is not
// mounted yet would be a dead end, so this list grows wave by wave.
import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Outlet, useLocation, useNavigate, useParams } from "react-router-dom";
import { api, ApiError } from "../../../lib/api";
import { tenantHeaders } from "../../../lib/auth";
import { AgentVersionConflict, patchConfig, type AgentConfigShape, type DeepPartial } from "../../../lib/agentConfig";
import { identityOf, identityPlate, identityStyle } from "../../../lib/agentIdentity";
import type { AgentDetail } from "../../../lib/types";
import { Button, EmptyState, Skeleton } from "../../../components/ui";
import { Icon } from "../../../components/ui/icons";
import { NAV_GROUP_CLASS, SidebarLink, useSidebarNavHost } from "../Layout";

const NAME_CACHE_PREFIX = "ctx.agent.name.";

function cachedName(agentId: string): string | null {
  try { return sessionStorage.getItem(NAME_CACHE_PREFIX + agentId); } catch { return null; }
}

function cacheName(agentId: string, name: string): void {
  try { sessionStorage.setItem(NAME_CACHE_PREFIX + agentId, name); } catch { /* storage unavailable */ }
}

export interface AgentContextValue {
  agentId: string;
  /** Null until the first read lands. Sections own a content-shaped skeleton. */
  agent: AgentDetail | null;
  loading: boolean;
  error: string;
  reload: () => Promise<void>;
  /**
   * The single write path (§4.8). Reads the row first and refuses to write when
   * `version` moved under the open editor, throwing `AgentVersionConflict` so the
   * section can offer discard / diff / save-anyway. `force` is that third choice.
   *
   * `replace` names blocks the caller wants sent verbatim rather than deep-merged
   * over the stored copy — the only way to delete a key from a free-form map such
   * as `retrieval.filters`. Pass it only when the edit carries the whole block.
   */
  patch: (
    edits: DeepPartial<AgentConfigShape>,
    opts?: { force?: boolean; replace?: readonly string[] },
  ) => Promise<AgentDetail>;
}

const AgentContext = createContext<AgentContextValue | null>(null);

export function useAgent(): AgentContextValue {
  const value = useContext(AgentContext);
  if (!value) throw new Error("useAgent() must be called inside an AgentLayout route");
  return value;
}

export function AgentLayout() {
  const { agentId = "" } = useParams<{ agentId: string }>();
  const nav = useNavigate();
  const location = useLocation();
  const host = useSidebarNavHost();

  const [agent, setAgent] = useState<AgentDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [missing, setMissing] = useState(false);
  // The version this editor session started from. Compared before every write.
  const loadedVersion = useRef<number | null>(null);
  const agentRef = useRef<AgentDetail | null>(null);
  agentRef.current = agent;

  const hinted = (location.state as { name?: string } | null)?.name ?? null;
  const name = agent?.name ?? hinted ?? cachedName(agentId);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const detail = await api.get<AgentDetail>(`/v1/agents/${agentId}`, tenantHeaders());
      setAgent(detail);
      loadedVersion.current = detail.version;
      cacheName(agentId, detail.name);
      setError("");
      setMissing(false);
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) setMissing(true);
      else setError(e instanceof ApiError ? e.message : "could not load this agent");
    } finally {
      setLoading(false);
    }
  }, [agentId]);

  useEffect(() => {
    if (!agentId) return;
    // A different agent id is a different editor session: drop the old row so a
    // section never reads the previous agent's config for one render.
    setAgent(null);
    loadedVersion.current = null;
    void load();
  }, [agentId, load]);

  const patch = useCallback<AgentContextValue["patch"]>(async (edits, opts) => {
    // The version read must be fresh, not cache-served: the whole point is to
    // catch another session's save since this editor loaded. An AbortSignal
    // that is never aborted is the api.ts opt-out from the GET cache.
    const current = await api.get<AgentDetail>(`/v1/agents/${agentId}`, tenantHeaders(), new AbortController().signal);
    const started = loadedVersion.current;
    if (!opts?.force && started !== null && current.version !== started) {
      throw new AgentVersionConflict(started, current);
    }
    // Base is the config this editor read, not the one just fetched: the whole
    // point of `force` is that the local blocks win.
    const base = agentRef.current?.config ?? current.config;
    const updated = await api.patch<AgentDetail>(
      `/v1/agents/${agentId}`,
      { config: patchConfig(base, edits, { replace: opts?.replace }) },
    );
    setAgent(updated);
    loadedVersion.current = updated.version;
    cacheName(agentId, updated.name);
    return updated;
  }, [agentId]);

  const identity = identityOf({ id: agentId, name, config: agent?.config ?? null });
  const Glyph = Icon[identity.glyph];

  const subline = agent?.is_default ? "Default" : null;

  const subnav = (
    <nav className="flex flex-col gap-0.5">
      <SidebarLink to="/agents" label="All agents" icon={<Icon.Chevron size={14} className="rotate-90" />} end />

      {/* Identity chip: rail in the agent's colour, glyph plate, name, sub-line. */}
      <div
        data-agent-identity
        style={identityStyle(identity)}
        className="flex items-center gap-2.5 mt-2 mb-1 pl-2.5 pr-2 py-2 rounded-md border-l-4 bg-[var(--agent-tint)]"
      >
        <span
          className="shrink-0 grid place-items-center w-7 h-7 rounded-lg text-white"
          style={{ background: identityPlate(identity) }}
        >
          <Glyph size={15} />
        </span>
        <div className="min-w-0">
          {name ? (
            <div className="text-sm font-semibold truncate">{name}</div>
          ) : (
            <Skeleton className="h-3.5 w-28" />
          )}
          {subline && <div className="text-[11px] text-faint truncate">{subline}</div>}
        </div>
      </div>

      <div className={NAV_GROUP_CLASS}>Build</div>
      <SidebarLink to={`/agents/${agentId}/instructions`} label="Instructions" icon={<Icon.Book size={17} />} />
      {/* §1.4 puts Sources directly under Instructions in the Build group. The
          hover/focus warm comes free: SidebarLink calls prefetchFor(to), and
          `sources` is registered in agentPrefetch. */}
      <SidebarLink to={`/agents/${agentId}/sources`} label="Sources" icon={<Icon.Doc size={17} />} />

      {/* Deploy: you pick the agent's channels after it answers well. */}
      <div className={NAV_GROUP_CLASS}>Deploy</div>
      <SidebarLink to={`/agents/${agentId}/channels`} label="Channels" icon={<Icon.Plug size={17} />} />

      {/* Outside every group, at the bottom: settings is where you go on purpose,
          not part of the build-then-check rhythm above it (§1.4). */}
      <div className="mt-2 pt-2 border-t border-line">
        <SidebarLink to={`/agents/${agentId}/settings`} label="Agent settings" icon={<Icon.Settings size={17} />} />
      </div>
    </nav>
  );

  const value: AgentContextValue = { agentId, agent, loading, error, reload: load, patch };

  return (
    <AgentContext.Provider value={value}>
      {host && createPortal(subnav, host)}
      {missing ? (
        <EmptyState
          icon={<Icon.Alert size={22} />}
          title="That agent doesn't exist"
          description="It may have been deleted."
          action={<Button onClick={() => nav("/agents")}>Back to agents</Button>}
        />
      ) : (
        <Outlet />
      )}
    </AgentContext.Provider>
  );
}
