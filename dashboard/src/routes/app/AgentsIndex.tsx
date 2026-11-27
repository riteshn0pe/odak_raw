// The agents index (§2). Every agent in the workspace as a card: identity band,
// status, metrics, last edit — and the overflow menu that owns create-adjacent
// lifecycle (duplicate, set default, rename, delete), which is why this screen
// replaces the agent switcher that used to live on the config page.
//
// Metrics (`source_count`, `conversations_7d`, `channels`) are not reported by
// GET /v1/agents yet (B3). Rather than print a confident `0 sources`, the card
// reserves the row and renders Skeletons behind a `metricsAvailable` gate, so the
// layout is final today and does not reflow when the backend lands. Deliberately
// NOT solved by N+1-ing the client with widget/whatsapp/document calls per card.
//
// The registry's swarm agents (plan agent.md §10) are materialized as real
// `agents` rows by the backend and arrive in this same list — same card, same
// Instructions screen, same template flow as a tenant-created agent. What
// differs is the lifecycle: a swarm agent is turned off (enabled=false) rather
// than deleted, and its status line carries the activation state (active /
// blocked / planned / off / not for you) computed from the tenant's fact base.
// They used to live in a separate read-only "swarm" section drawn from the graph
// payload; they are cards now because the product asks for exactly that: every
// agent visible as added, editable the same way.
import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, ApiError } from "../../lib/api";
import { tenantHeaders } from "../../lib/auth";
import { patchConfig } from "../../lib/agentConfig";
import { identityOf, identityPlate, identityStyle } from "../../lib/agentIdentity";
import { relativeTime } from "../../lib/format";
import type { AgentDetail, AgentSummary, DocumentOut } from "../../lib/types";
import {
  Alert, Badge, Button, Card, CardBody, Field, Input, Menu, Modal, PageHeader,
  Select, Skeleton, Spinner, EmptyState, type MenuItem,
} from "../../components/ui";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { SearchInput } from "../../components/SearchInput";
import { Icon } from "../../components/ui/icons";
import { useToast } from "../../components/ui/Toast";

// Below this the search box and the filters are chrome for nothing (§2.3).
const FILTER_THRESHOLD = 5;

/** A registry-managed swarm agent: same card, different lifecycle. */
function isSwarm(agent: AgentSummary): boolean {
  return agent.kind === "swarm";
}

/** Channels are the source of the Live/Draft split, and they are part of B3. */
function channelsKnown(agent: AgentSummary): boolean {
  return Array.isArray(agent.channels);
}

function isLive(agent: AgentSummary): boolean {
  return (agent.channels?.length ?? 0) > 0;
}

/** The activation badge a swarm card carries instead of Live/Draft. */
const STATE_BADGE: Record<string, { label: string; tone: "ok" | "warn" | "neutral" }> = {
  active: { label: "Active", tone: "ok" },
  blocked: { label: "Waiting on profile", tone: "warn" },
  planned: { label: "Planned", tone: "neutral" },
  disabled: { label: "Off", tone: "neutral" },
  not_applicable: { label: "Not for your setup", tone: "neutral" },
};

function StatusCell({ agent }: { agent: AgentSummary }) {
  if (isSwarm(agent)) {
    if (!agent.state) return <Skeleton className="h-5 w-20" />;
    const badge = STATE_BADGE[agent.state] ?? { label: agent.state, tone: "warn" as const };
    return <Badge tone={badge.tone} dot>{badge.label}</Badge>;
  }
  if (!channelsKnown(agent)) return <Skeleton className="h-5 w-16" />;
  return isLive(agent)
    ? <Badge tone="ok" dot>Live</Badge>
    : <Badge tone="neutral" dot>Draft</Badge>;
}

function ChannelsLine({ agent }: { agent: AgentSummary }) {
  if (isSwarm(agent)) {
    // the reason line is the card's call to action: "waiting on profile: sells,
    // audience" tells the owner exactly what to fill in
    return (
      <span className="text-[13px] text-muted">
        {agent.state_reason || "Runs on the platform's schedule"}
      </span>
    );
  }
  if (!channelsKnown(agent)) return <Skeleton className="h-3.5 w-28" />;
  const list = agent.channels ?? [];
  if (list.length === 0) return <span className="text-[13px] text-faint">No channels yet</span>;
  return <span className="text-[13px] text-muted">{list.join(" · ")}</span>;
}

function MetricsLine({ agent }: { agent: AgentSummary }) {
  if (typeof agent.source_count !== "number" || typeof agent.conversations_7d !== "number") {
    return <Skeleton className="h-3.5 w-24" />;
  }
  const sources = `${agent.source_count} ${agent.source_count === 1 ? "source" : "sources"}`;
  return (
    <span className="text-[13px] text-muted tabular-nums">
      {sources}   {agent.conversations_7d} chats · 7d
    </span>
  );
}

/** The colour band plus its glyph plate — the thing that makes identity legible. */
function IdentityBand({ agent }: { agent: AgentSummary }) {
  const identity = identityOf(agent);
  const Glyph = Icon[identity.glyph];
  return (
    <div
      data-agent-identity
      style={identityStyle(identity)}
      className="h-16 flex items-center px-5 bg-[var(--agent-tint)] rounded-t-xl"
    >
      <span
        className="grid place-items-center w-7 h-7 rounded-lg text-white shrink-0"
        style={{ background: identityPlate(identity) }}
      >
        <Glyph size={15} />
      </span>
    </div>
  );
}

function AgentCard({ agent, menu, onOpen }: { agent: AgentSummary; menu: MenuItem[]; onOpen: () => void }) {
  return (
    <Card className="overflow-hidden hover:shadow-pop transition-shadow cursor-pointer p-0">
      <div onClick={onOpen}>
        <IdentityBand agent={agent} />
        <CardBody className="flex flex-col gap-3">
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <span className="text-[15px] font-semibold tracking-tight truncate">{agent.name}</span>
                {agent.is_default && <Badge tone="brand">Default</Badge>}
                {isSwarm(agent) && agent.enabled === false && <Badge tone="neutral">Off</Badge>}
              </div>
              {agent.template && <div className="text-[13px] text-muted truncate">{agent.template}</div>}
            </div>
          </div>

          <div className="flex items-center gap-2.5 min-w-0">
            <StatusCell agent={agent} />
            <ChannelsLine agent={agent} />
          </div>

          <div className="flex flex-col gap-1">
            <MetricsLine agent={agent} />
            <span className="text-[13px] text-faint">
              {agent.activated_at
                ? `Active since ${relativeTime(agent.activated_at)}`
                : `Edited ${relativeTime(agent.updated_at)}`}
            </span>
          </div>

          <div className="flex items-center justify-between pt-1">
            <Button variant="secondary" size="sm" onClick={(e) => { e.stopPropagation(); onOpen(); }}>Open</Button>
            {/* The menu is inside the card's click target, so its own clicks stop
                here rather than also opening the agent. */}
            <span onClick={(e) => e.stopPropagation()}>
              <Menu trigger={<Icon.Settings size={16} />} items={menu} />
            </span>
          </div>
        </CardBody>
      </div>
    </Card>
  );
}

function NewAgentCard({ onClick }: { onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className="flex flex-col items-center justify-center gap-1.5 min-h-[220px] rounded-xl border border-dashed border-line-strong bg-transparent text-muted hover:text-ink hover:border-brand hover:bg-brand-soft/40 transition-colors cursor-pointer"
    >
      <span className="grid place-items-center w-10 h-10 rounded-full bg-brand-soft text-brand"><Icon.Plus size={20} /></span>
      <span className="text-sm font-semibold text-ink">New agent</span>
      <span className="text-[13px]">Start from a template</span>
    </button>
  );
}

function AgentCardSkeleton() {
  return (
    <Card className="overflow-hidden p-0">
      <div className="h-16 bg-canvas" />
      <CardBody className="flex flex-col gap-3">
        <Skeleton className="h-4 w-40" />
        <Skeleton className="h-5 w-24" />
        <Skeleton className="h-3.5 w-32" />
        <Skeleton className="h-8 w-20" />
      </CardBody>
    </Card>
  );
}

// The scheduled growth swarm used to render here as a separate read-only
// section drawn from GET /v1/graph. The backend now materializes every
// registry agent as a real `agents` row, so the swarm is the grid above —
// same cards, same editor — and this section is gone. The graph payload still
// powers the Visualise tab.

export function AgentsIndex() {
  const nav = useNavigate();
  const toast = useToast();

  const [agents, setAgents] = useState<AgentSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  // Fetched once, and only in the one-agent case: it decides the "never trained"
  // empty state (§2.4). One call for the screen, never one per card.
  const [docCount, setDocCount] = useState<number | null>(null);

  const [query, setQuery] = useState("");
  const [template, setTemplate] = useState("all");

  const [trainUrl, setTrainUrl] = useState("");
  const [training, setTraining] = useState(false);
  const [trainErr, setTrainErr] = useState("");

  const [renameFor, setRenameFor] = useState<AgentSummary | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const [renameErr, setRenameErr] = useState("");
  const [renaming, setRenaming] = useState(false);

  const [dupeFor, setDupeFor] = useState<AgentSummary | null>(null);
  const [dupeErr, setDupeErr] = useState("");
  const [duplicating, setDuplicating] = useState(false);

  const [deleteFor, setDeleteFor] = useState<AgentSummary | null>(null);
  const [deleteErr, setDeleteErr] = useState("");
  const [deleting, setDeleting] = useState(false);
  const [defaulting, setDefaulting] = useState<string | null>(null);
  // A swarm agent's on/off switch. Chat agents are not toggleable (their
  // channels are the switch), so this only ever holds a swarm row's id.
  const [toggling, setToggling] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    api.get<AgentSummary[]>("/v1/agents", tenantHeaders())
      .then((list) => { if (live) { setAgents(list); setError(""); } })
      .catch((e) => { if (live) setError(e instanceof ApiError ? e.message : "could not load your agents"); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, []);

  // The "never trained" empty state counts chat agents only: the swarm rows the
  // backend materializes always exist, so counting them would hide the one
  // screen that turns a signup into a working agent.
  const chatAgents = useMemo(() => agents.filter((a) => !isSwarm(a)), [agents]);

  useEffect(() => {
    if (chatAgents.length !== 1 || docCount !== null) return;
    let live = true;
    api.get<DocumentOut[]>("/v1/documents?limit=1", tenantHeaders())
      .then((rows) => { if (live) setDocCount(rows.filter((d) => d.status !== "deleted").length); })
      // Fail closed to the normal grid: a failed count must not claim the agent
      // has nothing to answer from.
      .catch(() => { if (live) setDocCount(1); });
    return () => { live = false; };
  }, [chatAgents.length, docCount]);

  // The name rides along in navigation state so AgentLayout paints the identity
  // chip on the first frame instead of waiting for its own fetch.
  function open(agent: AgentSummary) {
    nav(`/agents/${agent.id}/instructions`, { state: { name: agent.name } });
  }

  async function toggle(agent: AgentSummary) {
    setToggling(agent.id);
    try {
      const updated = await api.patch<AgentSummary>(
        `/v1/agents/${agent.id}/enabled`,
        { enabled: !agent.enabled },
      );
      setAgents((list) => list.map((a) => (a.id === agent.id
        ? { ...a, enabled: updated.enabled ?? !a.enabled, state: updated.state ?? a.state, state_reason: updated.state_reason ?? a.state_reason }
        : a)));
      toast.success(updated.enabled === false
        ? `${agent.name} turned off. Queued work is cancelled.`
        : `${agent.name} turned on.`);
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : "could not change this agent");
    } finally {
      setToggling(null);
    }
  }

  async function setDefault(agent: AgentSummary) {
    setDefaulting(agent.id);
    try {
      const updated = await api.patch<AgentDetail>(`/v1/agents/${agent.id}`, { is_default: true });
      setAgents((list) => list.map((a) => (
        a.id === updated.id
          ? { ...a, is_default: true, version: updated.version, updated_at: updated.updated_at ?? a.updated_at }
          : { ...a, is_default: false }
      )));
      toast.success(`${updated.name} is now the default agent.`);
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : "could not change the default agent");
    } finally {
      setDefaulting(null);
    }
  }

  async function duplicate() {
    const agent = dupeFor;
    if (!agent) return;
    setDuplicating(true); setDupeErr("");
    try {
      const detail = await api.get<AgentDetail>(`/v1/agents/${agent.id}`, tenantHeaders());
      const name = `${agent.name} (copy)`;
      // A create sends the whole config, so it is spread in full — `version` is a
      // row column and echoing it back into the blob is what made them drift.
      const { version: _version, ...rest } = detail.config;
      const created = await api.post<AgentDetail>(
        "/v1/agents",
        { name, config: { ...rest, name } },
        tenantHeaders(),
      );
      setAgents((list) => [...list, {
        id: created.id, name: created.name, is_default: created.is_default, version: created.version,
        created_at: created.created_at, updated_at: created.updated_at,
      }]);
      setDupeFor(null);
      toast.success(`${created.name} created.`, { description: "Add to it from Knowledge when you're ready." });
    } catch (e) {
      setDupeErr(e instanceof ApiError ? e.message : "could not duplicate this agent");
    } finally {
      setDuplicating(false);
    }
  }

  async function rename() {
    const agent = renameFor;
    const name = renameValue.trim();
    if (!agent) return;
    if (!name) { setRenameErr("Give the agent a name."); return; }
    setRenaming(true); setRenameErr("");
    try {
      // config.name is written too, so replies match the row name.
      const detail = await api.get<AgentDetail>(`/v1/agents/${agent.id}`, tenantHeaders());
      const updated = await api.patch<AgentDetail>(
        `/v1/agents/${agent.id}`,
        { name, config: patchConfig(detail.config, { name }) },
      );
      setAgents((list) => list.map((a) => (a.id === agent.id
        ? { ...a, name: updated.name, version: updated.version, updated_at: updated.updated_at ?? a.updated_at }
        : a)));
      setRenameFor(null);
      toast.success(`Renamed to ${updated.name}.`);
    } catch (e) {
      setRenameErr(e instanceof ApiError ? e.message : "could not rename this agent");
    } finally {
      setRenaming(false);
    }
  }

  async function remove() {
    const agent = deleteFor;
    if (!agent) return;
    setDeleteErr("");
    setDeleting(true);
    try {
      await api.del(`/v1/agents/${agent.id}`);
      setAgents((list) => list.filter((a) => a.id !== agent.id));
      setDeleteFor(null);
      toast.success(`${agent.name} deleted.`);
    } catch (e) {
      // The API refuses when a WhatsApp integration points at the agent; that
      // message is the useful one, so it goes in verbatim.
      setDeleteErr(e instanceof ApiError ? e.message : "could not delete this agent");
    } finally {
      setDeleting(false);
    }
  }

  async function train(e: React.FormEvent) {
    e.preventDefault();
    const host = trainUrl.trim().replace(/^https?:\/\//i, "");
    if (!host) { setTrainErr("Enter your website address."); return; }
    setTraining(true); setTrainErr("");
    try {
      await api.post("/v1/documents/url", { url: `https://${host}` }, tenantHeaders());
      setDocCount(1);
      toast.success("Reading your website now.", { description: "Indexing runs in the background — check Knowledge in a few minutes." });
    } catch (err) {
      setTrainErr(err instanceof ApiError ? err.message : "could not start reading that address");
    } finally {
      setTraining(false);
    }
  }

  function menuItems(agent: AgentSummary): MenuItem[] {
    // A swarm agent's lifecycle is on/off: it is part of the platform, so it
    // cannot be duplicated or deleted, and it is never the tenant's default
    // chat agent. Everything else — Open, Rename, the Instructions editor — is
    // the same flow a template-added agent uses.
    if (isSwarm(agent)) {
      return [
        { label: "Open", icon: <Icon.External size={15} />, onSelect: () => open(agent) },
        { label: "Rename", icon: <Icon.Settings size={15} />, onSelect: () => { setRenameValue(agent.name); setRenameErr(""); setRenameFor(agent); } },
        { separator: true },
        {
          label: toggling === agent.id
            ? agent.enabled === false ? "Turning on…" : "Turning off…"
            : agent.enabled === false ? "Turn on" : "Turn off",
          icon: toggling === agent.id
            ? <Spinner className="w-4 h-4" />
            : agent.enabled === false ? <Icon.Bolt size={15} /> : <Icon.X size={15} />,
          disabled: toggling !== null,
          onSelect: () => void toggle(agent),
        },
      ];
    }
    const onlyAgent = agents.filter((a) => !isSwarm(a)).length <= 1;
    const items: MenuItem[] = [
      { label: "Open", icon: <Icon.External size={15} />, onSelect: () => open(agent) },
      { label: "Duplicate", icon: <Icon.Copy size={15} />, onSelect: () => { setDupeErr(""); setDupeFor(agent); } },
    ];
    if (!agent.is_default) {
      items.push({
        label: defaulting === agent.id ? "Setting as default…" : "Set as default",
        icon: defaulting === agent.id ? <Spinner className="w-4 h-4" /> : <Icon.Check size={15} />,
        disabled: defaulting !== null,
        onSelect: () => void setDefault(agent),
      });
    }
    items.push(
      { label: "Rename", icon: <Icon.Settings size={15} />, onSelect: () => { setRenameValue(agent.name); setRenameErr(""); setRenameFor(agent); } },
      { separator: true },
      {
        label: "Delete",
        icon: <Icon.Trash size={15} />,
        tone: "danger",
        disabled: onlyAgent,
        hint: onlyAgent ? "You can't delete your only agent." : undefined,
        onSelect: () => { setDeleteErr(""); setDeleteFor(agent); },
      },
    );
    return items;
  }

  const templates = useMemo(
    () => Array.from(new Set(agents.map((a) => a.template).filter((t): t is string => !!t))),
    [agents],
  );

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return agents.filter((a) => {
      if (q && !a.name.toLowerCase().includes(q) && !(a.template ?? "").toLowerCase().includes(q)) return false;
      if (template !== "all" && (a.template ?? "") !== template) return false;
      return true;
    });
  }, [agents, query, template]);

  const header = (
    <PageHeader
      title="Agents"
      description="Each agent has its own instructions and channels. Knowledge is shared across all of them."
      actions={
        <div className="flex items-center gap-2">
          <Button variant="primary" onClick={() => nav("/agents/new")}>
            <Icon.Plus size={16} /> New agent
          </Button>
        </div>
      }
    />
  );

  if (loading) {
    return (
      <div>
        {header}
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {[0, 1, 2].map((i) => <AgentCardSkeleton key={i} />)}
        </div>
      </div>
    );
  }

  if (error) return <div>{header}<Alert>{error}</Alert></div>;

  // agents.length === 0 cannot happen — every tenant is provisioned with one — so
  // the real empty state is one agent with nothing to answer from (§2.4). It is
  // the highest-leverage screen in the product: it turns "I signed up" into "it
  // works" without a page change.
  if (chatAgents.length === 1 && docCount === 0) {
    return (
      <div>
        {header}
        <EmptyState
          tone="brand"
          icon={<Icon.Bolt size={22} />}
          title="Your agent has nothing to answer from yet"
          description="Paste your website address and we'll read it for you. Most teams have a working agent in about ten minutes."
          action={
            <div className="flex flex-col items-center gap-2 w-full">
              <form onSubmit={train} className="flex items-center gap-2 w-full max-w-lg">
                <div className="flex items-stretch flex-1 min-w-0 rounded-md border border-line-strong bg-surface overflow-hidden focus-within:ring-4 focus-within:ring-brand-soft">
                  <span className="grid place-items-center px-2.5 bg-canvas border-r border-line text-[13px] text-muted shrink-0">https://</span>
                  <input
                    value={trainUrl}
                    onChange={(e) => setTrainUrl(e.target.value)}
                    placeholder="www.yourcompany.com"
                    aria-label="Your website address"
                    className="flex-1 min-w-0 border-0 bg-transparent px-2.5 py-2 text-sm text-ink outline-none"
                  />
                </div>
                <Button variant="primary" type="submit" loading={training}>Train it</Button>
              </form>
              {trainErr && <span className="text-[13px] text-danger">{trainErr}</span>}
            </div>
          }
          footer={
            <span className="text-[13px] text-muted">
              Or{" "}
              <button onClick={() => nav("/knowledge")} className="text-brand font-semibold hover:underline bg-transparent border-0 p-0 cursor-pointer">upload files</button>
              {" · "}
              <button onClick={() => nav("/agents/new")} className="text-brand font-semibold hover:underline bg-transparent border-0 p-0 cursor-pointer">start from a template</button>
            </span>
          }
        />
      </div>
    );
  }

  const showSearch = agents.length > FILTER_THRESHOLD;

  return (
    <div>
      {header}

      <div className="flex items-center gap-2 mb-4 flex-wrap">
        {showSearch && (
          <div className="flex-1 min-w-[200px] max-w-xs">
            <SearchInput
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search agents…"
              aria-label="Search agents"
            />
          </div>
        )}
        {/* Each filter appears only when the field it reads exists. */}
        {showSearch && templates.length > 0 && (
          <Select value={template} onChange={(e) => setTemplate(e.target.value)} aria-label="Filter by template">
            <option value="all">All templates</option>
            {templates.map((t) => <option key={t} value={t}>{t}</option>)}
          </Select>
        )}
        <span className="ml-auto text-[13px] text-muted tabular-nums">
          {filtered.length} {filtered.length === 1 ? "agent" : "agents"}
        </span>
      </div>

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
        {filtered.map((a) => (
          <AgentCard key={a.id} agent={a} menu={menuItems(a)} onOpen={() => open(a)} />
        ))}
        <NewAgentCard onClick={() => nav("/agents/new")} />
      </div>


      <Modal
        open={!!dupeFor}
        onClose={() => setDupeFor(null)}
        title={dupeFor ? `Duplicate ${dupeFor.name}?` : "Duplicate agent"}
        footer={
          <>
            <Button variant="secondary" onClick={() => setDupeFor(null)}>Cancel</Button>
            <Button variant="primary" loading={duplicating} onClick={() => void duplicate()}>Duplicate</Button>
          </>
        }
      >
        <p className="text-sm text-muted m-0">
          This copies the instructions, model, guardrails and appearance. Sources stay with the
          original agent — you'll add sources to the copy separately.
        </p>
        {dupeErr && <div className="mt-3"><Alert>{dupeErr}</Alert></div>}
      </Modal>

      <Modal
        open={!!renameFor}
        onClose={() => setRenameFor(null)}
        title="Rename agent"
        description="This is the name your team sees, and the name customers see when the agent replies."
        footer={
          <>
            <Button variant="secondary" onClick={() => setRenameFor(null)}>Cancel</Button>
            <Button variant="primary" loading={renaming} onClick={() => void rename()}>Save</Button>
          </>
        }
      >
        <Field label="Name">
          <Input
            value={renameValue}
            onChange={(e) => setRenameValue(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") void rename(); }}
            autoFocus
          />
        </Field>
        {renameErr && <div className="mt-3"><Alert>{renameErr}</Alert></div>}
      </Modal>

      {deleteFor && (
        <ConfirmDialog
          danger
          title={`Delete ${deleteFor.name}?`}
          message="Its conversations, sources and channel connections go with it. This cannot be undone."
          confirmWord={deleteFor.name}
          error={deleteErr}
          busy={deleting}
          onConfirm={() => void remove()}
          onCancel={() => { setDeleteFor(null); setDeleteErr(""); }}
        />
      )}
    </div>
  );
}
