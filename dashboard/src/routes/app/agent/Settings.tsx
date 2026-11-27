// Agent settings (§4.7) — the third tier of the progressive disclosure rule.
//
// Everything here is real and occasionally necessary, and none of it belongs in
// front of someone writing an answering policy: which key pays for the model, the
// model to fall back to when the first one is down, how much prompt to allow, the
// exact labels the redactor applies, the regexes it adds, and the metadata filter
// pinned onto every search. Instructions links here; nothing links back except
// the sidebar.
//
// Three tabs, in the order a tenant meets them: General (what this agent is
// called and how to address it in the API), Advanced (the tier-3 fields), Danger
// zone (export and delete). Tabs rather than one long page because Danger zone
// next to a text field is how an agent gets deleted by accident.
//
// One Save changes for the whole screen (§4.8), disabled until dirty. The display
// name is two writes — a row column and a config key — issued in that order, and
// the config write is the one that carries the version guard.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useBlocker, useNavigate } from "react-router-dom";
import { api } from "../../../lib/api";
import { tenantHeaders } from "../../../lib/auth";
import { AgentVersionConflict, type AgentConfigShape, type DeepPartial } from "../../../lib/agentConfig";
import type { AgentDetail, AgentSummary, ModelCatalog } from "../../../lib/types";
import { DiffView, stableJson } from "../../../components/DiffView";
import { ConfirmDialog } from "../../../components/ConfirmDialog";
import {
  Alert, Badge, Banner, Button, Card, CardBody, CardHeader, CopyField, Field, Input, Modal,
  PageHeader, Skeleton, Tabs, TagInput, ToggleRow, Tooltip,
} from "../../../components/ui";
import { Icon } from "../../../components/ui/icons";
import { JsonEditor } from "../../../components/ui/JsonEditor";
import { useToast } from "../../../components/ui/Toast";
import { useAgent } from "./AgentLayout";

/* ------------------------------------------------------------------- copy */

// Verbatim from §4.2's field table, tier 3 rows. Same rule as Instructions: the
// tooltip says what the field does to an answer, never what it is called in the
// retrieval literature.
const TIP = {
  modelName: "The exact model this agent answers with. Leave it empty unless you have a reason to pin one — the default tracks the best model your key can reach.",
  fallbackModel: "Used only when the first model is unavailable. An answer from a second-choice model beats an error page.",
  maxPromptTokens: "The ceiling on how much of your documents plus conversation history goes into one request. Lower is cheaper and faster; too low and the agent stops seeing the passages it needs.",
  piiLabels: "The kinds of personal information to hide from the model. The agent still answers the question — it just never sees the name or the card number.",
  customPii: "Extra patterns to redact, one per line, written as regular expressions. Use this for internal identifiers we would not recognise, like an account number format that is yours alone.",
  filters: "Restricts every search this agent makes to documents whose metadata matches. Use it to point one agent at one product line without splitting your library.",
  webSearch: "Lets the agent search the public web when your own documents do not answer the question. Answers then cite pages you do not control.",
  customTools: "Extra tools this agent may call, as JSON. Actions gets a proper editor of its own; until then this is the raw definition, and a malformed entry disables the tool rather than the agent.",
} as const;

const EXPORT_NOTE = "Downloads this agent's full configuration as JSON — instructions, model, retrieval, guardrails, everything on this screen and on Instructions. Conversations and uploaded sources are not included; they are exported from their own screens.";

const DELETE_NOTE = "Deletes the agent, its settings, and its conversation history. Documents you uploaded stay in your library and remain available to your other agents. This cannot be undone.";

/* ---------------------------------------------------------------- plumbing */

type Block = Record<string, unknown>;

type BlockName = "model" | "retrieval" | "tools" | "guardrails";

function block(cfg: AgentConfigShape | null, key: BlockName): Block {
  const value = cfg ? (cfg as unknown as Block)[key] : undefined;
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Block) : {};
}

function readNumber(b: Block, key: string, fallback: number): number {
  const v = b[key];
  return typeof v === "number" && Number.isFinite(v) ? v : fallback;
}

function readBool(b: Block, key: string, fallback: boolean): boolean {
  const v = b[key];
  return typeof v === "boolean" ? v : fallback;
}

function readString(b: Block, key: string, fallback: string): string {
  const v = b[key];
  return typeof v === "string" ? v : fallback;
}

function readStrings(b: Block, key: string): string[] {
  const v = b[key];
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : [];
}

/** Pretty-print a nested map for the JSON editors. Empty renders as empty, not `{}`. */
function readJson(b: Block, key: string): string {
  const v = b[key];
  if (v === undefined || v === null) return "";
  if (Array.isArray(v) ? v.length === 0 : typeof v === "object" && Object.keys(v as Block).length === 0) return "";
  return JSON.stringify(v, null, 2);
}

interface Draft {
  name: string;
  modelName: string;
  fallbackModel: string;
  maxPromptTokens: number;
  piiLabels: string[];
  customPii: string[];
  filtersText: string;
  webSearch: boolean;
  customToolsText: string;
}

function draftFrom(agent: AgentDetail): Draft {
  const cfg = agent.config;
  const model = block(cfg, "model");
  const retrieval = block(cfg, "retrieval");
  const tools = block(cfg, "tools");
  const guardrails = block(cfg, "guardrails");
  return {
    name: agent.name ?? readString(cfg as unknown as Block, "name", ""),
    modelName: readString(model, "model", ""),
    fallbackModel: readString(model, "fallback_model", ""),
    maxPromptTokens: readNumber(model, "max_prompt_tokens", 0),
    piiLabels: readStrings(guardrails, "pii_labels"),
    customPii: readStrings(guardrails, "custom_pii_patterns"),
    filtersText: readJson(retrieval, "filters"),
    webSearch: readBool(tools, "web_search_enabled", false),
    customToolsText: readJson(tools, "custom_tools"),
  };
}

/** Parse an editor's text back to a value. `null` means "the text is not valid JSON". */
function parseJson(text: string, empty: unknown): unknown | null {
  if (text.trim() === "") return empty;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return null;
  }
}

interface Edits {
  /** Row column, sent as its own PATCH. Undefined when the name is untouched. */
  name?: string;
  config: DeepPartial<AgentConfigShape>;
  /** Blocks that must be sent verbatim so a removed filter key actually goes. */
  replace: string[];
}

/**
 * Diff the form against the stored config and return only what moved.
 *
 * `retrieval` and `tools` carry a free-form map each, so when one of those maps
 * changed the whole block goes as a replace — a deep merge cannot express a
 * deleted key, and "I removed that filter and it came back" is the bug that
 * costs a tenant an afternoon.
 */
function editsFrom(agent: AgentDetail, draft: Draft): Edits {
  const cfg = agent.config;
  const model = block(cfg, "model");
  const retrieval = block(cfg, "retrieval");
  const tools = block(cfg, "tools");
  const guardrails = block(cfg, "guardrails");

  const config: Block = {};
  const replace: string[] = [];

  const storedName = agent.name ?? readString(cfg as unknown as Block, "name", "");
  const name = draft.name.trim() !== storedName ? draft.name.trim() : undefined;
  if (name !== undefined) config.name = name;

  const modelEdit: Block = {};
  if (draft.fallbackModel !== readString(model, "fallback_model", "")) {
    modelEdit.fallback_model = draft.fallbackModel;
  }
  if (draft.modelName !== readString(model, "model", "")) {
    modelEdit.model = draft.modelName;
  }
  if (draft.maxPromptTokens !== readNumber(model, "max_prompt_tokens", 0)) {
    modelEdit.max_prompt_tokens = draft.maxPromptTokens;
  }
  if (Object.keys(modelEdit).length > 0) config.model = modelEdit;

  const guardEdit: Block = {};
  if (stableJson(draft.piiLabels) !== stableJson(readStrings(guardrails, "pii_labels"))) {
    guardEdit.pii_labels = draft.piiLabels;
  }
  if (stableJson(draft.customPii) !== stableJson(readStrings(guardrails, "custom_pii_patterns"))) {
    guardEdit.custom_pii_patterns = draft.customPii;
  }
  if (Object.keys(guardEdit).length > 0) config.guardrails = guardEdit;

  if (draft.filtersText.trim() !== readJson(retrieval, "filters").trim()) {
    const parsed = parseJson(draft.filtersText, {});
    if (parsed !== null) {
      config.retrieval = { ...retrieval, filters: parsed };
      replace.push("retrieval");
    }
  }

  const toolsChanged = draft.webSearch !== readBool(tools, "web_search_enabled", false)
    || draft.customToolsText.trim() !== readJson(tools, "custom_tools").trim();
  if (toolsChanged) {
    const parsed = parseJson(draft.customToolsText, []);
    if (parsed !== null) {
      config.tools = { ...tools, web_search_enabled: draft.webSearch, custom_tools: parsed };
      replace.push("tools");
    } else if (draft.webSearch !== readBool(tools, "web_search_enabled", false)) {
      // The tools JSON is mid-edit and unparseable, but the toggle moved. Send the
      // toggle on its own rather than refusing the save outright.
      config.tools = { web_search_enabled: draft.webSearch };
    }
  }

  return { name, config: config as DeepPartial<AgentConfigShape>, replace };
}

function isDirty(agent: AgentDetail | null, draft: Draft | null): boolean {
  if (!agent || !draft) return false;
  const stored = draftFrom(agent);
  return stableJson(stored) !== stableJson(draft);
}

/** Number input that clamps on blur, never mid-keystroke (§4.8). */
function NumberField({
  value, onCommit, min, max, suffix, placeholder,
}: {
  value: number;
  onCommit: (v: number) => void;
  min: number;
  max: number;
  suffix?: string;
  placeholder?: string;
}) {
  const [text, setText] = useState(value === 0 ? "" : String(value));
  const focused = useRef(false);
  useEffect(() => {
    if (!focused.current) setText(value === 0 ? "" : String(value));
  }, [value]);

  const commit = () => {
    const raw = text.trim();
    if (raw === "") { onCommit(0); return; }
    const n = Number(raw);
    if (!Number.isFinite(n)) { setText(value === 0 ? "" : String(value)); return; }
    const clamped = Math.min(max, Math.max(min, Math.round(n)));
    setText(String(clamped));
    onCommit(clamped);
  };

  return (
    <span className="flex items-center gap-2">
      <Input
        type="number"
        className="max-w-[160px] tabular-nums"
        value={text}
        placeholder={placeholder}
        onFocus={() => { focused.current = true; }}
        onChange={(e) => setText(e.target.value)}
        onBlur={() => { focused.current = false; commit(); }}
        onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); commit(); } }}
      />
      {suffix && <span className="text-xs text-faint">{suffix}</span>}
    </span>
  );
}

/** Label plus the little info affordance the field tables all specify. */
function labelWith(text: string, tip: string) {
  return (
    <span className="inline-flex items-center gap-1.5">
      {text}
      <Tooltip label={tip}>
        <span className="text-faint cursor-help inline-grid place-items-center"><Icon.Info size={13} /></span>
      </Tooltip>
    </span>
  );
}

/** A regex the redactor could not compile is worse than no pattern at all. */
function regexError(pattern: string): string | null {
  try {
    new RegExp(pattern);
    return null;
  } catch {
    return "Not a valid regular expression.";
  }
}

/* --------------------------------------------------------------- the screen */

const TABS = [
  { value: "general", label: "General" },
  { value: "advanced", label: "Advanced" },
  { value: "danger", label: "Danger zone" },
];

export function AgentSettings() {
  const { agentId, agent, loading, error, reload, patch } = useAgent();
  const nav = useNavigate();
  const toast = useToast();

  const [tab, setTab] = useState("general");
  const [draft, setDraft] = useState<Draft | null>(null);
  const [catalog, setCatalog] = useState<ModelCatalog | null>(null);
  const [siblings, setSiblings] = useState<AgentSummary[] | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [savedFlash, setSavedFlash] = useState(false);
  const [conflict, setConflict] = useState<AgentVersionConflict | null>(null);
  const [conflictDiffOpen, setConflictDiffOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deleteError, setDeleteError] = useState("");
  const [deleting, setDeleting] = useState(false);
  const [discardLoading, setDiscardLoading] = useState(false);

  // Same re-seed rule as Instructions: keyed on the row version, so a save or a
  // reload refreshes the form and an unrelated render never discards an edit.
  const initKey = agent ? `${agentId}:${agent.version}` : null;
  const initialised = useRef<string | null>(null);
  useEffect(() => {
    if (!agent || initKey === initialised.current) return;
    initialised.current = initKey;
    setDraft(draftFrom(agent));
    setSaveError("");
  }, [agent, initKey]);

  useEffect(() => {
    let live = true;
    api.get<ModelCatalog>("/v1/models", tenantHeaders())
      .then((c) => { if (live) setCatalog(c); })
      .catch(() => { /* the model name degrades to whatever is already stored */ });
    api.get<AgentSummary[]>("/v1/agents", tenantHeaders())
      .then((rows) => { if (live) setSiblings(rows); })
      .catch(() => { if (live) setSiblings(null); });
    return () => { live = false; };
  }, []);

  const dirty = useMemo(() => isDirty(agent, draft), [agent, draft]);

  const edit = useCallback(<K extends keyof Draft>(key: K, value: Draft[K]) => {
    setDraft((d) => (d ? { ...d, [key]: value } : d));
    setSavedFlash(false);
  }, []);

  const filtersBroken = draft !== null && draft.filtersText.trim() !== "" && parseJson(draft.filtersText, {}) === null;
  const toolsBroken = draft !== null && draft.customToolsText.trim() !== "" && parseJson(draft.customToolsText, []) === null;
  const nameBroken = draft !== null && draft.name.trim() === "";

  const blocker = useBlocker(({ currentLocation, nextLocation }) =>
    dirty && !saving && currentLocation.pathname !== nextLocation.pathname);

  useEffect(() => {
    if (!dirty) return;
    const warn = (e: BeforeUnloadEvent) => { e.preventDefault(); };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  const commit = useCallback(async (force: boolean) => {
    if (!agent || !draft) return;
    if (draft.name.trim() === "") {
      setSaveError("Give the agent a name — it is what the sidebar and every report call it.");
      return;
    }
    if (filtersBroken || toolsBroken) {
      setSaveError("One of the JSON fields on Advanced is not valid JSON yet. Fix it or clear it, then save.");
      return;
    }
    const { name, config, replace } = editsFrom(agent, draft);
    if (name === undefined && Object.keys(config).length === 0) return;

    setSaving(true);
    setSaveError("");
    try {
      // Config first: it is the write that carries the version guard, and a name
      // PATCH bumps the row version, which would make the guard fire on itself.
      if (Object.keys(config).length > 0) {
        await patch(config, { force, replace });
      }
      if (name !== undefined) {
        await api.patch<AgentDetail>(`/v1/agents/${agentId}`, { name });
        // The row moved outside patch()'s bookkeeping, so re-read rather than let
        // the next save compare against a version that no longer exists.
        initialised.current = null;
        await reload();
      }
      setConflict(null);
      setConflictDiffOpen(false);
      setSavedFlash(true);
      window.setTimeout(() => setSavedFlash(false), 2500);
      toast.success("Settings saved.");
    } catch (e) {
      if (e instanceof AgentVersionConflict) setConflict(e);
      else setSaveError(e instanceof Error ? e.message : "could not save these settings");
    } finally {
      setSaving(false);
    }
  }, [agent, draft, agentId, filtersBroken, toolsBroken, patch, reload, toast]);

  const discardRemote = useCallback(async () => {
    setDiscardLoading(true);
    setConflict(null);
    setConflictDiffOpen(false);
    setSaveError("");
    initialised.current = null;
    try {
      await reload();
    } finally {
      setDiscardLoading(false);
    }
  }, [reload]);

  const exportConfig = useCallback(() => {
    if (!agent) return;
    // Client-side on purpose: the config is already in memory, so an export
    // endpoint would be a round trip that can fail for no gain.
    const payload = stableJson({
      agent_id: agent.id,
      name: agent.name,
      version: agent.version,
      config: agent.config,
    });
    const url = URL.createObjectURL(new Blob([payload], { type: "application/json" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = `agent-${agent.name.replace(/[^a-z0-9]+/gi, "-").toLowerCase() || agent.id}.json`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    toast.success("Configuration downloaded.");
  }, [agent, toast]);

  const removeAgent = useCallback(async () => {
    setDeleting(true);
    setDeleteError("");
    try {
      await api.del(`/v1/agents/${agentId}`);
      toast.success("Agent deleted.");
      nav("/agents", { replace: true });
    } catch (e) {
      setDeleteError(e instanceof Error ? e.message : "could not delete this agent");
    } finally {
      setDeleting(false);
    }
  }, [agentId, nav, toast]);

  if (error && !agent) {
    return (
      <div>
        <PageHeader title="Agent settings" description="Naming, keys, limits, and the things you only change on purpose." />
        <Alert>{error}</Alert>
      </div>
    );
  }

  if (!agent || !draft || (loading && !agent)) return <AgentSettingsSkeleton />;

  const onlyAgent = siblings !== null && siblings.length <= 1;

  return (
    <div>
      <PageHeader
        title="Agent settings"
        description="Naming, keys, limits, and the things you only change on purpose."
        actions={
          <div className="flex items-center gap-2">
            {dirty
              ? <Badge tone="warn" dot>Unsaved changes</Badge>
              : savedFlash
                ? <Badge tone="ok" dot>Saved</Badge>
                : <Badge>v{agent.version}</Badge>}
            <Button variant="primary" onClick={() => void commit(false)} loading={saving} disabled={!dirty}>
              Save changes
            </Button>
          </div>
        }
      />

      {conflict && (
        <div className="mb-6">
          <Banner
            tone="warn"
            icon={<Icon.Alert size={17} />}
            action={
              <div className="flex items-center gap-2">
                <Button variant="secondary" size="sm" onClick={() => setConflictDiffOpen(true)}>See what changed</Button>
                <Button variant="secondary" size="sm" onClick={() => void discardRemote()} loading={discardLoading}>Discard mine and reload</Button>
                <Button variant="primary" size="sm" onClick={() => void commit(true)} loading={saving}>Save mine anyway</Button>
              </div>
            }
          >
            Someone else changed this agent while you were editing.
          </Banner>
        </div>
      )}

      {saveError && <div className="mb-6"><Alert>{saveError}</Alert></div>}

      <Tabs items={TABS} value={tab} onChange={setTab} className="mb-6" />

      {tab === "general" && (
        <div className="flex flex-col gap-6 max-w-[720px]">
          <Card>
            <CardHeader title="Name" description="Shown in your sidebar and in reports." />
            <CardBody className="flex flex-col gap-4">
              <Field label="Agent name">
                <Input
                  value={draft.name}
                  onChange={(e) => edit("name", e.target.value)}
                  placeholder="Support assistant"
                />
              </Field>
              {nameBroken && <p className="m-0 text-xs text-danger">An agent needs a name.</p>}
              {agent.is_default && (
                <p className="m-0 text-[13px] text-muted">
                  <Badge tone="brand">Default</Badge>{" "}
                  New conversations that do not name an agent come here. Change that from the agents list.
                </p>
              )}
            </CardBody>
          </Card>

          <Card>
            <CardHeader title="Addressing this agent" description="What to send in an API call." />
            <CardBody className="flex flex-col gap-4">
              <CopyField label="Agent ID" value={agent.id} mono />
              <div className="grid gap-4 sm:grid-cols-2 text-[13px]">
                <div>
                  <div className="text-xs text-faint mb-1">Created</div>
                  <div className="text-ink">{agent.created_at ? new Date(agent.created_at).toLocaleString() : "—"}</div>
                </div>
                <div>
                  <div className="text-xs text-faint mb-1">Last changed</div>
                  <div className="text-ink">{agent.updated_at ? new Date(agent.updated_at).toLocaleString() : "—"}</div>
                </div>
              </div>
            </CardBody>
          </Card>
        </div>
      )}

      {tab === "advanced" && (
        <div className="flex flex-col gap-6 max-w-[720px]">
          <Card>
            <CardHeader title="Model and limits" description="Which model writes the answers, what to fall back to, and how much to send." />
            <CardBody className="flex flex-col gap-4">
              <Field
                label={labelWith("Model name", TIP.modelName)}
                hint="Leave empty to use the platform default."
              >
                <Input
                  className="font-mono text-[13px]"
                  value={draft.modelName}
                  onChange={(e) => edit("modelName", e.target.value)}
                  placeholder={catalog ? catalog.managed.label : "Platform default"}
                />
              </Field>

              <Field
                label={labelWith("Fallback model", TIP.fallbackModel)}
                hint="Leave empty to return an error instead of a second-choice answer."
              >
                <Input
                  className="font-mono text-[13px]"
                  value={draft.fallbackModel}
                  onChange={(e) => edit("fallbackModel", e.target.value)}
                  placeholder="Optional — e.g. claude-haiku-4-5-20251001"
                />
              </Field>

              <Field
                label={labelWith("Largest request", TIP.maxPromptTokens)}
                hint="Leave empty for the default."
              >
                <NumberField
                  value={draft.maxPromptTokens}
                  onCommit={(v) => edit("maxPromptTokens", v)}
                  min={0}
                  max={400000}
                  suffix="tokens"
                  placeholder="Default"
                />
              </Field>
            </CardBody>
          </Card>

          <Card>
            <CardHeader title="Personal information" description="What gets hidden from the model when masking is on." />
            <CardBody className="flex flex-col gap-4">
              <Field
                label={labelWith("Kinds to hide", TIP.piiLabels)}
                hint="Masking itself is switched on from Instructions."
              >
                <TagInput
                  value={draft.piiLabels}
                  onChange={(v) => edit("piiLabels", v)}
                  placeholder="email, phone, credit_card…"
                />
              </Field>
              <Field
                label={labelWith("Your own patterns", TIP.customPii)}
                hint="Regular expressions. An invalid one is rejected as you type."
              >
                <TagInput
                  value={draft.customPii}
                  onChange={(v) => edit("customPii", v)}
                  placeholder="ACC-\d{8}"
                  tone="warn"
                  validate={regexError}
                />
              </Field>
            </CardBody>
          </Card>

          <Card>
            <CardHeader title="Which sources this agent may read" description="Leave empty and it searches everything in your library." />
            <CardBody className="flex flex-col gap-2">
              <div className="text-[13px] text-muted">{labelWith("Metadata filter", TIP.filters)}</div>
              <JsonEditor
                value={draft.filtersText}
                onChange={(text) => edit("filtersText", text)}
                placeholder={'{ "product": "payments" }'}
                minHeight={140}
              />
              {filtersBroken && <p className="m-0 text-xs text-danger">Not valid JSON yet, so this will not be saved.</p>}
            </CardBody>
          </Card>

          <Card>
            <CardHeader
              title="Tools"
              description="Extra abilities beyond reading your documents."
              actions={<Badge tone="warn">Moving to Actions</Badge>}
            />
            <CardBody className="flex flex-col gap-4">
              <ToggleRow
                label="Search the public web when your documents fall short"
                tooltip={TIP.webSearch}
                checked={draft.webSearch}
                onChange={(v) => edit("webSearch", v)}
              />
              <div className="flex flex-col gap-2">
                <div className="text-[13px] text-muted">{labelWith("Tool definitions", TIP.customTools)}</div>
                <JsonEditor
                  value={draft.customToolsText}
                  onChange={(text) => edit("customToolsText", text)}
                  placeholder='[ { "name": "lookup_order", "description": "…" } ]'
                  minHeight={160}
                />
                {toolsBroken && <p className="m-0 text-xs text-danger">Not valid JSON yet, so this will not be saved.</p>}
                <p className="m-0 text-xs text-faint">
                  These two fields live here until the Actions screen ships with a proper editor. Nothing you set here
                  is lost when it does.
                </p>
              </div>
            </CardBody>
          </Card>
        </div>
      )}

      {tab === "danger" && (
        <div className="flex flex-col gap-6 max-w-[720px]">
          <Card>
            <CardHeader title="Export this agent" description="Everything you have configured, as one JSON file." />
            <CardBody className="flex flex-col gap-4">
              <p className="m-0 text-[13px] text-muted">{EXPORT_NOTE}</p>
              <Button variant="secondary" onClick={exportConfig}>
                <Icon.Doc size={15} /> Download configuration
              </Button>
            </CardBody>
          </Card>

          <Card>
            <CardHeader title="Delete this agent" description="Immediate and irreversible." />
            <CardBody className="flex flex-col gap-4">
              <p className="m-0 text-[13px] text-muted">{DELETE_NOTE}</p>
              {onlyAgent ? (
                <div className="flex flex-col gap-2">
                  <Tooltip label="Your workspace needs at least one agent. Create another one first, then delete this.">
                    <span className="self-start"><Button variant="danger" disabled>Delete agent</Button></span>
                  </Tooltip>
                  <p className="m-0 text-xs text-faint">This is your only agent.</p>
                </div>
              ) : (
                <Button variant="danger" className="self-start" onClick={() => { setDeleteError(""); setDeleteOpen(true); }}>
                  <Icon.Trash size={15} /> Delete agent
                </Button>
              )}
            </CardBody>
          </Card>
        </div>
      )}

      <Modal
        open={conflictDiffOpen}
        onClose={() => setConflictDiffOpen(false)}
        title="What changed while you were editing"
        description="Saving anyway replaces the blocks you edited and keeps theirs everywhere else."
        size="lg"
        footer={<Button variant="secondary" onClick={() => setConflictDiffOpen(false)}>Close</Button>}
      >
        {conflict && (
          <DiffView
            before={stableJson(agent.config)}
            after={stableJson(conflict.remote.config)}
            beforeLabel={`What you loaded (v${conflict.localVersion})`}
            afterLabel={`Saved by someone else (v${conflict.remote.version})`}
            context={2}
          />
        )}
      </Modal>

      {deleteOpen && (
        <ConfirmDialog
          danger
          title="Delete this agent?"
          message={DELETE_NOTE}
          confirmWord={agent.name}
          error={deleteError || undefined}
          busy={deleting}
          onCancel={() => { if (!deleting) setDeleteOpen(false); }}
          onConfirm={() => { void removeAgent(); }}
        />
      )}

      {blocker.state === "blocked" && (
        <ConfirmDialog
          title="You have unsaved changes to this agent's settings."
          message="Leaving now discards them. Nothing has been sent to the agent yet."
          onCancel={() => blocker.reset()}
          onConfirm={() => blocker.proceed()}
        />
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ skeleton */

export function AgentSettingsSkeleton() {
  return (
    <div>
      <div className="flex items-start justify-between gap-4 mb-6">
        <div className="flex flex-col gap-2">
          <Skeleton className="h-7 w-44" />
          <Skeleton className="h-4 w-80" />
        </div>
        <Skeleton className="h-9 w-32" />
      </div>
      <div className="flex items-center gap-4 border-b border-line pb-2 mb-6">
        <Skeleton className="h-4 w-16" />
        <Skeleton className="h-4 w-20" />
        <Skeleton className="h-4 w-24" />
      </div>
      <div className="flex flex-col gap-6 max-w-[720px]">
        {[0, 1].map((i) => (
          <Card key={i}>
            <div className="px-5 py-4 border-b border-line flex flex-col gap-2">
              <Skeleton className="h-5 w-32" />
              <Skeleton className="h-3.5 w-64" />
            </div>
            <CardBody className="flex flex-col gap-4">
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-10 w-2/3" />
            </CardBody>
          </Card>
        ))}
      </div>
    </div>
  );
}
