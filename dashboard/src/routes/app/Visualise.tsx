// Visualise — the agent graph, read-only. The owner sees the swarm the staff
// console draws (same fixed layout from the server) without any of the model
// routing: what runs, what triggers it, which flows wait on an approval.
//
// Hand-rolled SVG, matching the admin screen's approach — a graph library
// would be the largest dependency in a build that currently has three.
// Nodes render as agent cards (glyph + status ring); edges on a live path
// animate continuous flowing dots from agent A to B (dash-offset flow + a
// travelling dot via animateMotion). The fullscreen button opens an overlay
// with the whole flow centred. Polls every 20s so the animation tracks the
// backend's real activation state; Demo flow showcases the path when idle.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { graphApi, ApiError } from "../../lib/api";
import { tenantHeaders } from "../../lib/auth";
import type { AgentGraphEdge, GraphNode, GraphView } from "../../lib/types";
import {
  Alert, Badge, Button, Card, CardBody, CardHeader, PageHeader, Skeleton,
} from "../../components/ui";
import { Icon } from "../../components/ui/icons";

const NODE_W = 176;
const NODE_H = 64;
// Room for the widest node plus the label that hangs under the bottom row.
const CANVAS_W = 920;
// 500, not the tidy 400: the lowest graph row (growth_campaign + hitl:campaign,
// y=410 in agent_models.py) ends at y+64, and a 400 canvas clipped the whole
// campaign flow off-screen.
const CANVAS_H = 500;

const POLL_MS = 20_000;
const DEMO_STEP_MS = 1200;
const DEMO_MAX_MS = 30_000;

// Registry glyphs (app/services/agents/registry.py) so nodes read as agents
// without a second call — the graph payload carries no glyph.
const GLYPH: Record<string, string> = {
  orchestrator: "◈",
  growth_profile: "◎",
  support: "◆",
  growth_research: "⌕",
  growth_compose: "✎",
  growth_followup: "↻",
  growth_replies: "⇄",
  growth_inbound: "⚡",
  growth_campaign: "◐",
  growth_insights: "◔",
  growth_report: "▤",
  growth_crm: "⇉",
  builtin_things: "⌸",
  growth_talkprep: "◌",
  growth_reviews: "◌",
};

const KIND_CLASS: Record<string, string> = {
  agent: "fill-surface stroke-line-strong",
  trigger: "fill-brand-soft stroke-brand",
  gate: "fill-warn-soft stroke-warn-border",
};

const KIND_BADGE: Record<string, { label: string; tone: "brand" | "warn" | "neutral" }> = {
  agent: { label: "Agent", tone: "neutral" },
  trigger: { label: "Trigger", tone: "brand" },
  gate: { label: "Approval gate", tone: "warn" },
};

/** Status ring colour per activation state (backend `state`, fallback `status`). */
function statusColor(node: GraphNode): string {
  if (node.kind !== "agent") return "";
  const s = (node.state ?? "").toLowerCase();
  if (s === "active") return "#059669";
  if (s === "blocked" || s === "not_applicable") return "#b45309";
  if (s === "disabled") return "#94a3b8";
  if ((node.status ?? "") === "planned") return "#94a3b8";
  return "#2563eb";
}

function statusLabel(node: GraphNode): string {
  if (node.kind !== "agent") return KIND_BADGE[node.kind]?.label ?? node.kind;
  if (node.state) {
    return node.state === "not_applicable" ? "Not applicable"
      : node.state.charAt(0).toUpperCase() + node.state.slice(1);
  }
  return (node.status ?? "") === "planned" ? "Planned" : "Agent";
}

/** Where an edge leaves and enters, so lines meet box edges rather than centres. */
function anchor(x: number, y: number, side: "out" | "in") {
  return { x: side === "out" ? x + NODE_W : x, y: y + NODE_H / 2 };
}

function edgePath(a: { x: number; y: number }, b: { x: number; y: number }): string {
  // A cubic with horizontal control points: the curve leaves and arrives
  // horizontally, so a line never appears to enter the side of a box it did
  // not come from.
  const mid = (a.x + b.x) / 2;
  return `M ${a.x} ${a.y} C ${mid} ${a.y}, ${mid} ${b.y}, ${b.x} ${b.y}`;
}

export function Visualise() {
  const [view, setView] = useState<GraphView | null>(null);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [fullscreen, setFullscreen] = useState(false);
  const [updatedAt, setUpdatedAt] = useState<string>("");
  const [demo, setDemo] = useState(false);
  const [demoStep, setDemoStep] = useState(0);
  const demoTimer = useRef<number | null>(null);

  const load = useCallback(async (signal?: AbortSignal, silent = false) => {
    try {
      // NOTE: api.ts caches successful GETs for 15s; polling at 20s plus the
      // manual refresh both land past the TTL, so this screen stays fresh
      // without a `fresh` bypass option on the graph client.
      const v = await graphApi.graph(tenantHeaders(), signal);
      setView(v);
      setUpdatedAt(new Date().toLocaleTimeString());
      if (!silent) setError("");
    } catch (e) {
      if (signal?.aborted) return;
      setError(e instanceof ApiError ? e.message : "could not load the agent graph");
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    // Live updates: poll so the flow animation tracks real activation state.
    // No websocket on the backend; polling with cleanup, never overlapping.
    const id = window.setInterval(() => {
      const c = new AbortController();
      void load(c.signal, true);
      window.setTimeout(() => c.abort(), POLL_MS - 1000);
    }, POLL_MS);
    return () => {
      controller.abort();
      window.clearInterval(id);
    };
  }, [load]);

  const nodeById = useMemo(
    () => new Map((view?.nodes ?? []).map((n) => [n.key, n])),
    [view],
  );

  // An edge is "live" when its target agent is active (triggers/gates have no
  // state — a trigger into an active agent is a live path). Selected node
  // highlights its own connections; everything else dims.
  const liveTargets = useMemo(() => {
    const s = new Set<string>();
    for (const n of view?.nodes ?? []) {
      if (n.kind === "agent" && (n.state ?? "").toLowerCase() === "active") s.add(n.key);
    }
    return s;
  }, [view]);

  const isLiveEdge = useCallback((e: AgentGraphEdge): boolean => {
    if (liveTargets.size === 0) return true; // unknown state: show the flow, don't hide it
    return liveTargets.has(e.to) || (liveTargets.has(e.from) && liveTargets.size > 0);
  }, [liveTargets]);

  // Demo flow: walk the live edges in order so the A→B dots showcase even on
  // an idle tenant. Guarded by a max runtime — no infinite loop.
  const liveEdges = useMemo(
    () => (view?.edges ?? []).filter(isLiveEdge),
    [view, isLiveEdge],
  );

  useEffect(() => {
    if (!demo || liveEdges.length === 0) return;
    setDemoStep(0);
    const step = window.setInterval(
      () => setDemoStep((s) => (s + 1) % liveEdges.length),
      DEMO_STEP_MS,
    );
    const stop = window.setTimeout(() => setDemo(false), DEMO_MAX_MS);
    demoTimer.current = step;
    return () => {
      window.clearInterval(step);
      window.clearTimeout(stop);
    };
  }, [demo, liveEdges.length]);

  // ESC exits fullscreen; backdrop click too (on the overlay div).
  useEffect(() => {
    if (!fullscreen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setFullscreen(false);
    };
    window.addEventListener("keydown", onKey);
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = "";
    };
  }, [fullscreen]);

  // Everything the click panel shows is derived from the graph itself.
  const connections = useMemo(() => {
    const incoming: AgentGraphEdge[] = [];
    const outgoing: AgentGraphEdge[] = [];
    if (!selected) return { incoming, outgoing };
    for (const edge of view?.edges ?? []) {
      if (edge.to === selected) incoming.push(edge);
      if (edge.from === selected) outgoing.push(edge);
    }
    return { incoming, outgoing };
  }, [selected, view]);

  const selectedNode = selected ? nodeById.get(selected) : undefined;

  const renderGraph = (large: boolean) => (
    <svg
      viewBox={`0 0 ${CANVAS_W} ${CANVAS_H}`}
      className={large ? "w-full h-auto max-h-[76vh]" : "w-full min-w-[720px] h-full"}
      role="img"
      aria-label="Agent flow graph"
    >
      <defs>
        <marker
          id={large ? "graph-arrow-lg" : "graph-arrow"}
          viewBox="0 0 10 10"
          refX="9"
          refY={5}
          markerWidth={6}
          markerHeight={6}
          orient="auto-start-reverse"
        >
          <path d="M 0 0 L 10 5 L 0 10 z" className="fill-line-strong" />
        </marker>
        <marker
          id={large ? "graph-arrow-live-lg" : "graph-arrow-live"}
          viewBox="0 0 10 10"
          refX="9"
          refY={5}
          markerWidth={6}
          markerHeight={6}
          orient="auto-start-reverse"
        >
          <path d="M 0 0 L 10 5 L 0 10 z" fill="#2563eb" />
        </marker>
      </defs>

      {(view?.edges ?? []).map((edge, i) => {
        const from = nodeById.get(edge.from);
        const to = nodeById.get(edge.to);
        if (!from || !to) return null;
        const a = anchor(from.x, from.y, "out");
        const b = anchor(to.x, to.y, "in");
        const d = edgePath(a, b);
        const mid = (a.x + b.x) / 2;
        const live = isLiveEdge(edge);
        const connected = selected != null && (edge.from === selected || edge.to === selected);
        const dimmed = selected != null && !connected;
        const demoCurrent = demo && liveEdges[demoStep] === edge;
        return (
          <g key={i} opacity={dimmed ? 0.3 : 1}>
            {/* base line */}
            <path
              d={d}
              className="fill-none"
              stroke={live || demoCurrent ? "#2563eb" : "var(--color-line-strong)"}
              strokeWidth={connected || demoCurrent ? 2.5 : live ? 2 : 1.5}
              strokeDasharray={live || demoCurrent ? "7 5" : undefined}
              opacity={live || demoCurrent ? 0.9 : 0.7}
              markerEnd={`url(#${large ? (live ? "graph-arrow-live-lg" : "graph-arrow-lg") : (live ? "graph-arrow-live" : "graph-arrow")})`}
              style={live || demoCurrent ? { animation: "ca-flow 0.9s linear infinite" } : undefined}
            />
            {/* continuous travelling dots A→B on the live path */}
            {(live || demoCurrent) && (
              <circle r={demoCurrent ? 4 : 3} fill={demoCurrent ? "#f97316" : "#2563eb"}>
                <animateMotion dur={demoCurrent ? "1.1s" : "1.8s"} repeatCount="indefinite" path={d} />
              </circle>
            )}
            <text
              x={mid}
              y={(a.y + b.y) / 2 - 6}
              textAnchor="middle"
              className="fill-faint"
              style={{ fontSize: 10, paintOrder: "stroke", stroke: "var(--color-surface)", strokeWidth: 3 }}
            >
              {edge.label}
            </text>
          </g>
        );
      })}

      {(view?.nodes ?? []).map((node) => {
        const active = selected === node.key;
        const ring = statusColor(node);
        const planned = (node.status ?? "") === "planned" && node.kind === "agent";
        return (
          <g
            key={node.key}
            transform={`translate(${node.x}, ${node.y})`}
            onClick={() => setSelected(node.key)}
            style={{ cursor: "pointer" }}
          >
            <rect
              width={NODE_W}
              height={NODE_H}
              rx={10}
              className={KIND_CLASS[node.kind] ?? KIND_CLASS.agent}
              stroke={ring || undefined}
              strokeWidth={active ? 2.5 : ring ? 2 : 1.25}
              strokeDasharray={planned ? "5 4" : undefined}
              opacity={planned ? 0.75 : 1}
            />
            {/* agent glyph chip */}
            <circle
              cx={22}
              cy={22}
              r={11}
              fill={ring ? `${ring}1a` : "var(--color-brand-soft)"}
              stroke={ring || "var(--color-brand)"}
              strokeWidth={1.25}
            />
            <text
              x={22}
              y={27}
              textAnchor="middle"
              style={{ fontSize: 12, fontWeight: 700, fill: ring || "var(--color-brand)" }}
            >
              {GLYPH[node.key] ?? (node.kind === "trigger" ? "↯" : node.kind === "gate" ? "⏸" : "●")}
            </text>
            <text
              x={40}
              y={20}
              className="fill-ink"
              style={{ fontSize: 12, fontWeight: 600 }}
            >
              {node.label.length > 20 ? `${node.label.slice(0, 19)}…` : node.label}
            </text>
            {/* status dot + state */}
            <circle
              cx={46}
              cy={40}
              r={4}
              fill={ring || "var(--color-faint)"}
              style={node.state === "active" ? { animation: "ca-breathe 2s ease-in-out infinite" } : undefined}
            />
            <text x={56} y={44} className="fill-muted" style={{ fontSize: 11 }}>
              {statusLabel(node)}
            </text>
          </g>
        );
      })}
    </svg>
  );

  return (
    <div className="flex flex-col min-h-[calc(100vh-8rem)]">
      <style>{`@keyframes ca-flow { to { stroke-dashoffset: -12; } }`}</style>
      <PageHeader
        title="Visualise"
        description="Your agent swarm: what runs, what triggers it, and where a decision waits on you."
      />

      {error && (
        <div className="mb-4">
          <Alert>{error}</Alert>
        </div>
      )}

      {!view && !error && (
        <Card>
          <CardBody>
            <Skeleton className="h-96 w-full" />
          </CardBody>
        </Card>
      )}

      {view && (
        <div className="grid xl:grid-cols-[1fr_320px] gap-6 flex-1">
          <Card className="flex flex-col">
            <CardHeader
              title="Flow"
              description="Cron and webhook triggers, the approval gate, and the agents they drive."
              actions={
                <div className="flex items-center gap-2">
                  <span className="text-[11px] text-faint hidden md:inline" title={`Last refreshed ${updatedAt}`}>
                    <span className="inline-block w-1.5 h-1.5 rounded-full bg-ok mr-1" style={{ animation: "ca-breathe 2s ease-in-out infinite" }} />
                    Live{updatedAt ? ` · ${updatedAt}` : ""}
                  </span>
                  <Button
                    size="sm"
                    variant="secondary"
                    onClick={() => setDemo((d) => !d)}
                    title="Animate the A→B task flow step by step for the demo"
                  >
                    {demo ? "⏸ Stop demo" : "▶ Demo flow"}
                  </Button>
                  <Button
                    size="sm"
                    variant="secondary"
                    onClick={() => { const c = new AbortController(); void load(c.signal, true); }}
                    title="Refresh the graph now"
                  >
                    ⟳
                  </Button>
                  <Button
                    size="sm"
                    variant="secondary"
                    onClick={() => setFullscreen(true)}
                    title="Open fullscreen"
                    aria-label="Open graph fullscreen"
                  >
                    <Icon.External size={15} /> Fullscreen
                  </Button>
                </div>
              }
            />
            <CardBody className="flex-1 flex flex-col">
              <div className="overflow-x-auto flex-1">
                {renderGraph(false)}
              </div>
              {/* legend + flow showcase */}
              <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-[12px] text-muted">
                <span><span className="inline-block w-2 h-2 rounded-full mr-1" style={{ background: "#059669" }} />Active</span>
                <span><span className="inline-block w-2 h-2 rounded-full mr-1" style={{ background: "#b45309" }} />Waiting on facts</span>
                <span><span className="inline-block w-2 h-2 rounded-full mr-1" style={{ background: "#94a3b8" }} />Off / Planned</span>
                <span><span className="inline-block w-4 h-0 border-t-2 border-dashed border-brand mr-1 align-middle" />Flowing dots = task moving A→B</span>
                <span className="ml-auto">Click any node to see what it does and what it connects to.</span>
              </div>
            </CardBody>
          </Card>

          <Card className="self-start">
            <CardHeader title="Node" />
            <CardBody>
              {selectedNode ? (
                <div className="flex flex-col gap-3">
                  <div className="flex items-center gap-2">
                    <span className="font-semibold">{selectedNode.label}</span>
                    <Badge tone={KIND_BADGE[selectedNode.kind]?.tone ?? "neutral"}>
                      {KIND_BADGE[selectedNode.kind]?.label ?? selectedNode.kind}
                    </Badge>
                  </div>
                  {selectedNode.state && (
                    <div>
                      <Badge tone={selectedNode.state === "active" ? "brand" : "warn"}>
                        {statusLabel(selectedNode)}
                      </Badge>
                      {selectedNode.reason && (
                        <p className="text-sm text-muted mt-1">{selectedNode.reason}</p>
                      )}
                    </div>
                  )}
                  {selectedNode.goal && (
                    <p className="text-sm"><span className="font-medium">Goal: </span><span className="text-muted">{selectedNode.goal}</span></p>
                  )}
                  {selectedNode.description && (
                    <p className="text-sm text-muted">{selectedNode.description}</p>
                  )}

                  {connections.incoming.length > 0 && (
                    <div>
                      <div className="text-[11px] uppercase tracking-wide text-faint mb-1">
                        Runs from
                      </div>
                      <ul className="text-sm text-muted flex flex-col gap-1">
                        {connections.incoming.map((e, i) => (
                          <li key={i}>
                            {nodeById.get(e.from)?.label ?? e.from}
                            {e.label ? ` — ${e.label}` : ""}
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}

                  {connections.outgoing.length > 0 && (
                    <div>
                      <div className="text-[11px] uppercase tracking-wide text-faint mb-1">
                        Hands off to
                      </div>
                      <ul className="text-sm text-muted flex flex-col gap-1">
                        {connections.outgoing.map((e, i) => (
                          <li key={i}>
                            {nodeById.get(e.to)?.label ?? e.to}
                            {e.label ? ` — ${e.label}` : ""}
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}
                </div>
              ) : (
                <p className="text-sm text-muted">
                  Nothing selected. Click a node in the graph to read what it does.
                </p>
              )}
            </CardBody>
          </Card>
        </div>
      )}

      {/* fullscreen overlay: the whole flow centred, ESC / ✕ / backdrop to exit */}
      {fullscreen && view && (
        <div
          className="fixed inset-0 z-50 bg-canvas/95 backdrop-blur-sm overflow-auto p-4 md:p-8"
          onClick={(e) => { if (e.target === e.currentTarget) setFullscreen(false); }}
          role="dialog"
          aria-modal="true"
          aria-label="Agent flow graph fullscreen"
        >
          <div className="max-w-6xl mx-auto">
            <Card className="shadow-pop">
              <CardHeader
                title="Agent swarm — full flow"
                description="All connections at once. Dots flow along every live path; click a node for detail."
                actions={
                  <Button size="sm" variant="secondary" onClick={() => setFullscreen(false)} aria-label="Close fullscreen">
                    <Icon.X size={15} /> Close (ESC)
                  </Button>
                }
              />
              <CardBody>
                <div className="overflow-x-auto">
                  {renderGraph(true)}
                </div>
                <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-[12px] text-muted">
                  <span><span className="inline-block w-2 h-2 rounded-full mr-1" style={{ background: "#059669" }} />Active</span>
                  <span><span className="inline-block w-2 h-2 rounded-full mr-1" style={{ background: "#b45309" }} />Waiting on facts</span>
                  <span><span className="inline-block w-2 h-2 rounded-full mr-1" style={{ background: "#94a3b8" }} />Off / Planned</span>
                  <span><span className="inline-block w-4 h-0 border-t-2 border-dashed border-brand mr-1 align-middle" />Flowing dots = task moving A→B</span>
                </div>
              </CardBody>
            </Card>
          </div>
        </div>
      )}
    </div>
  );
}
