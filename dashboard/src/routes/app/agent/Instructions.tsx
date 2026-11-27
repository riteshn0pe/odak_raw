// Instructions — the Build workspace's first and most consequential screen (§4.2).
//
// The four tall sections are horizontal tabs at the top — Answer, Safety, When it
// needs a person, How it searches — so nothing hides below the fold and no section
// is missed. Model and Test sit on the sticky right rail beside the tabs, exactly
// where they were before tabs: they are small, and a tenant reaches for them while
// editing the prompt. The shared draft and the Save live in the header, visible on
// every tab. Nothing is hidden, everything is reachable, and the word "embedding"
// never appears.
//
// The prompt editor is typeset as prose, not configuration (§6.7): font-sans at
// 15px/1.7 with a 92-character measure. Monospace at 13px says "an engineer owns
// this, don't touch it", which is exactly wrong for the person whose answering
// policy this is. The `Aa` / `{ }` toggle in the card header is the mitigation
// for whitespace-sensitive prompts, persisted so one click sticks.
//
// Writes go through AgentLayout's `patch()` and nowhere else, so the shallow-merge
// rule and the concurrent-edit guard are implemented once (§4.8). No autosave: an
// agent's instructions are live in front of customers, and a keystroke should not
// deploy.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useBlocker, useNavigate } from "react-router-dom";
import { api, ApiError } from "../../../lib/api";
import { tenantHeaders } from "../../../lib/auth";
import { AgentVersionConflict, patchConfig, type AgentConfigShape, type DeepPartial } from "../../../lib/agentConfig";
import { templateById } from "../../../lib/agentTemplates";
import type { AgentPromptPreview, ModelCatalog } from "../../../lib/types";
import { DiffView, stableJson } from "../../../components/DiffView";
import { ConfirmDialog } from "../../../components/ConfirmDialog";
import {
  Alert, Badge, Banner, Button, Card, CardBody, CardHeader, Field, Input, Modal,
  PageHeader, SegmentedControl, Select, Skeleton, Slider, Tabs, TagInput, Textarea, ToggleRow, Tooltip, cx,
} from "../../../components/ui";
import { Icon } from "../../../components/ui/icons";
import { useToast } from "../../../components/ui/Toast";
import { useAgent } from "./AgentLayout";
import { EscalationPanel } from "./EscalationPanel";

/* ------------------------------------------------------------------ gating */

// B5: there is no way to run a model call that ignores the saved prompt, so
// Compare could only be built on the shadow-agent workaround — which creates a
// real agent, bills real retrieval, and leaves a "— draft" row behind whenever a
// tab closes mid-compare. §8.0 B5's own verdict: the button stays disabled until
// the override lands — visible, with the reason on hover, because a control that
// silently disappears teaches nothing.

/* --------------------------------------------------------------- unit maths */

// A word is roughly 1.3 tokens (§4.2 model copy). A non-technical buyer knows
// what 300 words is and does not know what 400 tokens is, so the field is in
// words and the conversion happens at the boundary.
const TOKENS_PER_WORD = 1.33;
function wordsFromTokens(tokens: number): number { return Math.round(tokens / TOKENS_PER_WORD); }
function tokensFromWords(words: number): number { return Math.round(words * TOKENS_PER_WORD); }

// Counter under the editor. Deliberately approximate and labelled "about": an
// exact tokeniser in the browser would be a dependency and a lie about precision.
const CHARS_PER_TOKEN = 4;

// The input budget the prompt counter is measured against. 128k is the
// conservative allowance most hosted models share. The counter is labelled
// "about", so this is an approximation, not a contract.
const PROMPT_INPUT_BUDGET = 128_000;

const MONO_KEY = "ctx.instructions.mono";

/* --------------------------------------------------------- tooltip copy (§4.2) */

const TIP = {
  mode: "Keyword search finds exact words like product codes. Meaning-based search finds passages that answer the question even when the words differ. Both together is the safest choice and what we recommend.",
  topK: "How many passages from your sources the agent looks at before deciding what to use. Higher finds more, but costs a little more per answer. 20 suits most agents.",
  rerank: "A second, slower pass that reads the passages properly and puts the most relevant one first. Worth turning on when the right answer is in your sources but the agent keeps quoting the wrong page.",
  rerankTopN: "How many of the passages above go through the second pass. Keep it well below “Passages to consider” — that's the point of the first pass.",
  minConfidence: "Below this, the agent says it doesn't know instead of guessing. Raise it if you're seeing confident wrong answers. Lower it if it refuses things it should be able to answer.",
  parentChild: "When a short passage matches, also send the section it came from, so the agent sees the context. Helps with long documents. Turn it off for short, self-contained sources like listings or price lists.",
  contextual: "Each passage is stored with a note about where it sits in its document, which makes matching more accurate on large documentation sets. Costs more when you add sources, nothing when you answer. Changing this needs a re-index.",
  maxContextChunks: "How many passages actually go into the answer. More gives fuller answers and costs more per message. 8 suits most agents; documentation agents do better with 12 or more.",
  historyTurns: "How much of the current conversation the agent keeps in mind. Higher is better for step-by-step troubleshooting or booking; lower keeps answers focused and cheaper.",
  rollingSummary: "In a long conversation, older messages are condensed into a short summary rather than dropped, so the agent still knows what was agreed earlier.",
  refuseOutOfScope: "The agent declines anything your sources don't cover, instead of answering from general knowledge. Leave this on unless you specifically want it to chat more broadly — sales agents sometimes do.",
  piiMasking: "Email addresses, phone numbers, card numbers and ID numbers in a message are replaced with placeholders before the model sees them, and in the saved conversation.",
  injection: "Detects messages that try to talk the agent out of its instructions — “ignore your rules”, “you are now a different assistant”. Leave this on.",
  groundedness: "After writing an answer, the agent checks that your sources actually support it. Catches confident-sounding invention.",
  groundednessMode: "“Warn me” still sends the answer and records the warning, so you can see how often it happens. “Don't send it” replaces the answer with your message below. Use “don't send” for anything involving money, policy, medical or legal matters.",
  blocklist: "Any message containing one of these phrases is refused straight away, before the agent looks anything up. Use it for phrases you always want a human to handle.",
  fallback: "Shown whenever the agent declines — no matching sources, low confidence, or a blocked phrase. Make it useful: say what to do next.",
  crisis: "Shown instead of a normal answer when someone raises something that needs a human right away. Leave empty if that doesn't apply to this agent.",
  model: "Which AI model writes the answers. “Managed” is the platform's own model and needs no setup.",
  temperature: "Low sticks closely to the words in your documents. High lets the agent rephrase more freely. Support and policy agents want low; sales agents can go higher.",
  maxOutput: "A cap on answer length. Shown in words; a word is roughly 1.3 tokens.",
} as const;

const CONTEXTUAL_WARNING =
  "Changing this only affects sources added from now on. To apply it to what you already have, re-index your sources from the Sources page.";

/* ------------------------------------------------------- config block reading */

type Block = Record<string, unknown>;

function block(cfg: AgentConfigShape | null, key: "model" | "retrieval" | "guardrails" | "context"): Block {
  const value = cfg?.[key];
  return value && typeof value === "object" ? (value as Block) : {};
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

/* ------------------------------------------------------------- the form draft */

// Exactly the fields this screen owns. Every one is initialised from the loaded
// config, never from undefined, so there is no "empty means default" ambiguity
// (§4.8). Fields the screen does not own are absent and therefore never sent.
interface Draft {
  system_prompt: string;
  // model
  modelChoice: string;          // "managed"
  temperature: number;
  maxOutputWords: number;
  // retrieval
  mode: string;
  top_k: number;
  rerank_enabled: boolean;
  rerank_top_n: number;
  min_confidence: number;       // 0..1 in the draft; the slider works in percent
  parent_child: boolean;
  contextual_retrieval: boolean;
  max_context_chunks: number;
  // guardrails
  refuse_out_of_scope: boolean;
  pii_masking: boolean;
  injection_screening: boolean;
  groundedness_check: boolean;
  groundedness_mode: string;
  blocklist: string[];
  fallback_message: string;
  crisis_message: string;
  // context
  history_turns: number;
  rolling_summary: boolean;
}

const MANAGED_VALUE = "managed";

function draftFrom(cfg: AgentConfigShape): Draft {
  const model = block(cfg, "model");
  const retrieval = block(cfg, "retrieval");
  const guardrails = block(cfg, "guardrails");
  const context = block(cfg, "context");
  return {
    system_prompt: typeof cfg.system_prompt === "string" ? cfg.system_prompt : "",
    modelChoice: MANAGED_VALUE,
    temperature: readNumber(model, "temperature", 0.2),
    maxOutputWords: wordsFromTokens(readNumber(model, "max_output_tokens", 2048)),
    mode: readString(retrieval, "mode", "hybrid"),
    top_k: readNumber(retrieval, "top_k", 20),
    rerank_enabled: readBool(retrieval, "rerank_enabled", false),
    rerank_top_n: readNumber(retrieval, "rerank_top_n", 8),
    min_confidence: readNumber(retrieval, "min_confidence", 0.45),
    parent_child: readBool(retrieval, "parent_child", true),
    contextual_retrieval: readBool(retrieval, "contextual_retrieval", false),
    max_context_chunks: readNumber(retrieval, "max_context_chunks", 8),
    refuse_out_of_scope: readBool(guardrails, "refuse_out_of_scope", true),
    pii_masking: readBool(guardrails, "pii_masking", true),
    injection_screening: readBool(guardrails, "injection_screening", true),
    groundedness_check: readBool(guardrails, "groundedness_check", false),
    groundedness_mode: readString(guardrails, "groundedness_mode", "advisory"),
    blocklist: readStrings(guardrails, "blocklist"),
    fallback_message: readString(guardrails, "fallback_message", ""),
    crisis_message: readString(guardrails, "crisis_message", ""),
    history_turns: readNumber(context, "history_turns", 6),
    rolling_summary: readBool(context, "rolling_summary", true),
  };
}

/**
 * Turn the draft into the sub-block edits AgentLayout.patch() expects. Every
 * block named here is sent complete, because the server's merge is shallow —
 * `patchConfig` does the completing; this only decides *what* the values are.
 */
function editsFrom(draft: Draft): DeepPartial<AgentConfigShape> {
  return {
    system_prompt: draft.system_prompt,
    model: {
      temperature: draft.temperature,
      max_output_tokens: tokensFromWords(draft.maxOutputWords),
    },
    retrieval: {
      mode: draft.mode,
      top_k: draft.top_k,
      rerank_enabled: draft.rerank_enabled,
      rerank_top_n: draft.rerank_top_n,
      min_confidence: draft.min_confidence,
      parent_child: draft.parent_child,
      contextual_retrieval: draft.contextual_retrieval,
      max_context_chunks: draft.max_context_chunks,
    },
    guardrails: {
      refuse_out_of_scope: draft.refuse_out_of_scope,
      pii_masking: draft.pii_masking,
      injection_screening: draft.injection_screening,
      groundedness_check: draft.groundedness_check,
      groundedness_mode: draft.groundedness_mode,
      blocklist: draft.blocklist,
      fallback_message: draft.fallback_message,
      // An empty crisis message is "this agent has none", which the server models
      // as null rather than "".
      crisis_message: draft.crisis_message.trim() === "" ? null : draft.crisis_message,
    },
    context: {
      history_turns: draft.history_turns,
      rolling_summary: draft.rolling_summary,
    },
  } as DeepPartial<AgentConfigShape>;
}

/**
 * The blocks that actually differ from what was loaded. Sending an unchanged
 * block is not harmful — the values are identical — but it widens the window in
 * which this screen clobbers another editor's concurrent change, so it is worth
 * the eight lines to narrow.
 */
function changedBlocks(
  cfg: AgentConfigShape,
  draft: Draft,
): Partial<AgentConfigShape> {
  const complete = patchConfig(cfg, editsFrom(draft)) as Record<string, unknown>;
  const stored = cfg as unknown as Record<string, unknown>;
  const out: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(complete)) {
    if (stableJson(value) !== stableJson(stored[key])) out[key] = value;
  }
  return out as Partial<AgentConfigShape>;
}

/* --------------------------------------------------------------- small parts */

const MODE_LABEL: Record<string, string> = {
  hybrid: "Both (recommended)",
  dense: "By meaning",
  sparse: "By keyword",
};

/** The accordion subtitle: the audit line that makes tier 2 honest (§4.2). */
function advancedSummary(draft: Draft): string {
  const search = draft.mode === "hybrid" ? "Hybrid search" : draft.mode === "dense" ? "Meaning-based search" : "Keyword search";
  const rerank = draft.rerank_enabled ? "rerank on" : "rerank off";
  const floor = Math.round(draft.min_confidence * 100);
  return `${search} · ${draft.top_k} passages · ${rerank} · answers below ${floor}% confidence are refused`;
}

/** The live sentence under the temperature slider (§4.2). */
function temperatureSentence(t: number): string {
  const value = t.toFixed(2).replace(/0$/, "");
  if (t <= 0.3) return `${value} — sticks closely to your documents. Recommended.`;
  if (t <= 0.7) return `${value} — rephrases a little, still led by your documents.`;
  return `${value} — rephrases freely. Right for sales copy, risky for policy answers.`;
}

/** Label after the field label, with the spec's tooltip on an info icon. */
function InfoTip({ text }: { text: string }) {
  return (
    <Tooltip label={text}>
      <span className="inline-flex items-center align-middle text-faint ml-1"><Icon.Info size={14} /></span>
    </Tooltip>
  );
}

function labelWith(text: string, tip: string) {
  return <span className="inline-flex items-baseline">{text}<InfoTip text={tip} /></span>;
}

/**
 * Number input that clamps on blur rather than on change (§4.8), so typing `1`
 * on the way to `15` is not rewritten under the cursor. Enter commits too, which
 * is what a user who types a value and immediately hits Save expects.
 */
function ClampedNumber({
  value, onCommit, min, max, step = 1, suffix, id,
}: {
  value: number;
  onCommit: (v: number) => void;
  min: number;
  max: number;
  step?: number;
  suffix?: string;
  id?: string;
}) {
  const [text, setText] = useState(String(value));
  const focused = useRef(false);
  // While the field is focused the text is the source of truth; outside that,
  // a save or a reload that moves the value has to show through.
  useEffect(() => { if (!focused.current) setText(String(value)); }, [value]);

  const commit = () => {
    const parsed = Number(text);
    const next = Number.isFinite(parsed) ? Math.min(max, Math.max(min, parsed)) : value;
    setText(String(next));
    if (next !== value) onCommit(next);
  };

  return (
    <span className="inline-flex items-center gap-2">
      <Input
        id={id}
        type="number"
        className="w-28"
        min={min}
        max={max}
        step={step}
        value={text}
        onFocus={() => { focused.current = true; }}
        onChange={(e) => setText(e.target.value)}
        onBlur={() => { focused.current = false; commit(); }}
        onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); commit(); } }}
      />
      {suffix && <span className="text-xs text-faint">{suffix}</span>}
    </span>
  );
}

/* --------------------------------------------------------- prompt preview */

/**
 * What the template in the editor becomes for THIS business, right now.
 *
 * Swarm prompts are templates with `{{company.*}}` holes that the backend
 * renders from the tenant's fact base at invocation; a chat persona may carry
 * them too. This panel is the honest window onto that: the owner edits rules,
 * the preview shows the rules with their own business filled in — and names the
 * facts still missing, which is the difference between "it knows us" and a
 * prompt that reads "‹company.company_name›".
 *
 * The preview always sends the editor's current text (the backend renders what
 * it is given against the stored facts), so it reflects unsaved edits — that is
 * the point of a preview.
 */
function PromptPreview({ agentId, template }: { agentId: string; template: string }) {
  const [result, setResult] = useState<AgentPromptPreview | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  // A stale preview that looks current is worse than none: any edit to the
  // template drops the panel back to the button.
  useEffect(() => { setResult(null); setError(""); }, [template]);

  async function run() {
    setLoading(true);
    setError("");
    try {
      const res = await api.post<AgentPromptPreview>(
        `/v1/agents/${agentId}/preview`,
        { template },
        tenantHeaders(),
      );
      setResult(res);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "could not render this preview");
    } finally {
      setLoading(false);
    }
  }

  const hasHoles = /\{\{[^}]+\}\}/.test(template);

  return (
    <div className="flex flex-col gap-2.5 rounded-xl border border-line bg-canvas p-4">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="min-w-0">
          <p className="m-0 text-sm font-semibold text-ink">How it reads for your business</p>
          <p className="m-0 text-[13px] text-muted">
            {hasHoles
              ? "Details like your company name and what you sell are filled in from your business profile when this agent runs."
              : "This prompt has no fill-in fields — it runs exactly as written. The preview shows it unchanged."}
          </p>
        </div>
        <Button variant="secondary" size="sm" onClick={() => void run()} loading={loading}>
          <Icon.Eye size={15} /> Preview
        </Button>
      </div>

      {error && <Alert>{error}</Alert>}

      {result && (
        <>
          {result.missing.length > 0 && (
            <Banner tone="warn" icon={<Icon.Alert size={17} />}>
              Still missing from your business profile: {result.missing.join(", ")}. The agent
              fills these in from your website when it has read it — or you can add them on the
              Business profile screen.
            </Banner>
          )}
          <pre className="m-0 whitespace-pre-wrap text-[13px] leading-relaxed text-ink font-sans max-h-72 overflow-y-auto">
            {result.rendered}
          </pre>
        </>
      )}
    </div>
  );
}

/* --------------------------------------------------------------- the screen */

// The four tall sections as horizontal tabs, in the order a tenant meets them:
// the answer policy first, then the rails that constrain it, then the handoff,
// then how it searches. Model and Test sit beside them on a persistent right
// rail, exactly where they were before tabs — they are small, and burying them
// in a tab would hide what the tenant reaches for while editing the prompt.
// The shared draft and the Save stay in the header, visible on every tab.
const TABS = [
  { value: "answer",  label: "Answer" },
  { value: "safety",  label: "Safety" },
  { value: "handoff", label: "When it needs a person" },
  { value: "search",  label: "How it searches" },
];

export function Instructions() {
  const { agentId, agent, loading, error, reload, patch } = useAgent();
  const nav = useNavigate();
  const toast = useToast();

  const [draft, setDraft] = useState<Draft | null>(null);
  const [catalog, setCatalog] = useState<ModelCatalog | null>(null);
  // Separate from `catalog` so a failed /v1/models fetch does not permanently
  // disable the Model select: only a request genuinely in flight disables it,
  // and a failure degrades to managed-only with the current model still savable.
  const [catalogLoading, setCatalogLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [reloading, setReloading] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [savedFlash, setSavedFlash] = useState(false);
  const [conflict, setConflict] = useState<AgentVersionConflict | null>(null);
  const [conflictDiffOpen, setConflictDiffOpen] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [resetOpen, setResetOpen] = useState(false);
  const [mono, setMono] = useState(() => {
    try { return localStorage.getItem(MONO_KEY) === "1"; } catch { return false; }
  });
  const [tab, setTab] = useState("answer");

  const errorRef = useRef<HTMLDivElement | null>(null);

  const cfg = agent?.config ?? null;

  // Re-seed the form whenever the row this editor is based on changes: a
  // different agent, a successful save, or a discard-and-reload. Keyed on the
  // version so an unrelated re-render never wipes an in-progress edit.
  const initKey = agent ? `${agentId}:${agent.version}` : null;
  const initialised = useRef<string | null>(null);
  useEffect(() => {
    if (!agent || initKey === initialised.current) return;
    initialised.current = initKey;
    setDraft(draftFrom(agent.config));
    setSaveError("");
  }, [agent, initKey]);

  useEffect(() => {
    let live = true;
    setCatalogLoading(true);
    api.get<ModelCatalog>("/v1/models", tenantHeaders())
      .then((c) => { if (live) setCatalog(c); })
      .catch(() => { /* the Model select degrades to managed-only */ })
      .finally(() => { if (live) setCatalogLoading(false); });
    return () => { live = false; };
  }, []);

  useEffect(() => {
    try { localStorage.setItem(MONO_KEY, mono ? "1" : "0"); } catch { /* storage unavailable */ }
  }, [mono]);

  const changed = useMemo(
    () => (cfg && draft ? changedBlocks(cfg, draft) : {}),
    [cfg, draft],
  );
  const dirty = Object.keys(changed).length > 0;

  const edit = useCallback(<K extends keyof Draft>(key: K, value: Draft[K]) => {
    setDraft((d) => (d ? { ...d, [key]: value } : d));
    setSavedFlash(false);
  }, []);

  // Leaving with unsaved instructions is the one navigation worth interrupting.
  const blocker = useBlocker(({ currentLocation, nextLocation }) =>
    dirty && !saving && currentLocation.pathname !== nextLocation.pathname);

  useEffect(() => {
    if (!dirty) return;
    const warn = (e: BeforeUnloadEvent) => { e.preventDefault(); };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  const flashSaved = useCallback(() => {
    setSavedFlash(true);
    window.setTimeout(() => setSavedFlash(false), 2500);
  }, []);

  const commit = useCallback(async (force: boolean) => {
    if (!cfg || !draft) return;
    if (draft.fallback_message.trim() === "") {
      setSaveError("Give the agent something to say when it can't answer — an empty message would look broken to a customer.");
      return;
    }
    const body = changedBlocks(cfg, draft);
    if (Object.keys(body).length === 0) return;
    setSaving(true);
    setSaveError("");
    try {
      await patch(body as DeepPartial<AgentConfigShape>, force ? { force: true } : undefined);
      setConflict(null);
      setConflictDiffOpen(false);
      flashSaved();
      toast.success("Instructions saved.", { description: "New conversations use them straight away." });
    } catch (e) {
      if (e instanceof AgentVersionConflict) setConflict(e);
      else {
        const msg = e instanceof Error ? e.message : "could not save these instructions";
        setSaveError(msg);
        toast.error("Instructions not saved.", { description: msg });
      }
    } finally {
      setSaving(false);
    }
  }, [cfg, draft, patch, flashSaved, toast]);

  // A failed validation or save is already an Alert at the top of the screen;
  // move it into view and land focus on it so the reason is the next thing read
  // or seen, not something the user has to hunt for.
  useEffect(() => {
    if (!saveError) return;
    errorRef.current?.scrollIntoView({ behavior: "smooth", block: "center" });
    errorRef.current?.focus({ preventScroll: true });
  }, [saveError]);

  const discardRemote = useCallback(async () => {
    setConflict(null);
    setConflictDiffOpen(false);
    setSaveError("");
    initialised.current = null;
    setReloading(true);
    try {
      await reload();
    } finally {
      setReloading(false);
    }
  }, [reload]);

  // Which template this agent came from, so "reset the prompt" names it rather
  // than offering an anonymous list the tenant has to guess at.
  const template = useMemo(() => {
    const widget = cfg?.widget as Block | undefined;
    const theme = widget && typeof widget.theme === "object" && widget.theme !== null
      ? (widget.theme as Block)
      : undefined;
    return templateById(typeof theme?.template === "string" ? theme.template : null);
  }, [cfg]);

  if (error && !cfg) {
    return (
      <div>
        <PageHeader title="Instructions" description="What this agent is, how it answers, and what it must refuse." />
        <Alert>{error}</Alert>
      </div>
    );
  }

  if (!cfg || !draft || (loading && !agent)) return <InstructionsSkeleton />;

  const chars = draft.system_prompt.length;
  const approxTokens = Math.round(chars / CHARS_PER_TOKEN);
  const promptRatio = approxTokens / PROMPT_INPUT_BUDGET;
  const storedContextual = readBool(block(cfg, "retrieval"), "contextual_retrieval", false);
  // Phase 2.1's Textarea owns the font family + size (sans text-sm, or mono 13px
  // via the `mono` prop); these classes only set the prose measure and the min
  // height, so the emitted class carries exactly one font family and one size.
  const editorClass = mono
    ? "leading-[1.6] min-h-[420px] max-w-[92ch]"
    : "leading-[1.7] min-h-[420px] max-w-[92ch]";
  const expandedEditorClass = mono
    ? "leading-[1.6] min-h-[60vh] max-w-[92ch] mx-auto"
    : "leading-[1.7] min-h-[60vh] max-w-[92ch] mx-auto";

  const promptEditor = (className: string, opts: { autoFocus?: boolean; id?: string } = {}) => (
    <Textarea
      id={opts.id}
      aria-label="Describe who this agent is and how it should answer"
      className={className}
      mono={mono}
      autoGrow={false}
      resizable
      autoFocus={opts.autoFocus}
      spellCheck={!mono}
      value={draft.system_prompt}
      onChange={(e) => edit("system_prompt", e.target.value)}
      placeholder="Describe who this agent is, how it should answer, and what it must refuse."
    />
  );

  const modelSelect = (
    <Select
      value={draft.modelChoice}
      onChange={(e) => edit("modelChoice", e.target.value)}
      disabled={catalogLoading}
    >
      <option value={MANAGED_VALUE}>
        {catalog ? `Managed — ${catalog.managed.label} (default)` : "Managed (default)"}
      </option>
    </Select>
  );

  return (
    <div>
      <div aria-busy={saving}>
        <PageHeader
          title="Instructions"
          description="What this agent is, how it answers, and what it must refuse."
          actions={
            <div className="flex items-center gap-2">
              <Tooltip label="Comparing two sets of instructions side by side needs a model call that ignores the saved prompt. That's coming; until then, edit and save, and test on a channel.">
                <span><Button variant="ghost" size="sm" disabled>Compare</Button></span>
              </Tooltip>
              {dirty
                ? <Badge tone="warn" dot>Unsaved changes</Badge>
                : savedFlash
                  ? <Badge tone="ok" dot>Saved</Badge>
                  : <Badge tone="neutral">v{agent?.version ?? 1}</Badge>}
              <Button variant="primary" onClick={() => void commit(false)} loading={saving} disabled={!dirty}>
                Save changes
              </Button>
            </div>
          }
        />
      </div>

      {conflict && (
        <div className="mb-6">
          <Banner
            tone="warn"
            icon={<Icon.Alert size={17} />}
            action={
              <div className="flex items-center gap-2">
                <Button variant="secondary" size="sm" onClick={() => setConflictDiffOpen(true)}>See what changed</Button>
                <Button variant="secondary" size="sm" loading={reloading} onClick={() => void discardRemote()}>Discard mine and reload</Button>
                <Button variant="primary" size="sm" onClick={() => void commit(true)} loading={saving}>Save mine anyway</Button>
              </div>
            }
          >
            Someone else changed this agent while you were editing.
          </Banner>
        </div>
      )}

      {saveError && (
        <div ref={errorRef} tabIndex={-1} className="mb-6 outline-none">
          <Alert>{saveError}</Alert>
        </div>
      )}

      <div className="grid grid-cols-[1fr_380px] gap-8 max-lg:grid-cols-1">
        {/* ------------------------- left column ------------------------- */}
        <div className="flex flex-col gap-6 min-w-0">
          <Tabs items={TABS} value={tab} onChange={setTab} className="mb-6" />

          {tab === "answer" && (
            <Card>
              <CardHeader
                title="How it should answer"
                description="The agent reads this before every answer. Write it the way you would brief a new colleague."
                actions={
                  <div className="flex items-center gap-1">
                    <Tooltip label={mono ? "Show as prose" : "Show as monospace — keeps spacing and pasted tables aligned"}>
                      <span>
                        <Button variant="ghost" size="sm" onClick={() => setMono((m) => !m)} aria-label="Toggle monospace">
                          {mono ? "Aa" : "{ }"}
                        </Button>
                      </span>
                    </Tooltip>
                    <Tooltip label="Edit full screen">
                      <span>
                        <Button variant="ghost" size="sm" onClick={() => setExpanded(true)} aria-label="Expand the editor">
                          <Icon.External size={15} />
                        </Button>
                      </span>
                    </Tooltip>
                  </div>
                }
              />
              <CardBody className="flex flex-col gap-4">
                {promptEditor(editorClass, { id: "instructions-system-prompt" })}
                <div className="flex items-center justify-between gap-3">
                  <span
                    role="status"
                    aria-live="polite"
                    className={cx(
                      "text-xs tabular-nums",
                      promptRatio >= 1 ? "font-medium text-danger" : promptRatio >= 0.9 ? "text-warn" : "text-faint",
                    )}
                  >
                    {chars.toLocaleString()} characters · about {approxTokens.toLocaleString()} tokens
                  </span>
                  {template && (
                    <Button variant="ghost" size="sm" onClick={() => setResetOpen(true)}>
                      Reset to the {template.name} template
                    </Button>
                  )}
                </div>
                <PromptPreview agentId={agentId} template={draft.system_prompt} />
              </CardBody>
            </Card>
          )}

          {tab === "safety" && (
            <Card>
              <CardHeader title="Safety and boundaries" description="What the agent refuses, and what it says when it does." />
              <CardBody className="flex flex-col gap-4">
                <div className="flex flex-col gap-2.5">
                  <ToggleRow
                    label="Only answer from your own documents"
                    tooltip={TIP.refuseOutOfScope}
                    checked={draft.refuse_out_of_scope}
                    onChange={(v) => edit("refuse_out_of_scope", v)}
                  />
                  <ToggleRow
                    label="Hide personal information from the model"
                    tooltip={TIP.piiMasking}
                    checked={draft.pii_masking}
                    onChange={(v) => edit("pii_masking", v)}
                  />
                  <ToggleRow
                    label="Block attempts to change its instructions"
                    tooltip={TIP.injection}
                    checked={draft.injection_screening}
                    onChange={(v) => edit("injection_screening", v)}
                  />
                  <ToggleRow
                    label="Check answers against your documents"
                    tooltip={TIP.groundedness}
                    checked={draft.groundedness_check}
                    onChange={(v) => edit("groundedness_check", v)}
                  />
                </div>

                {draft.groundedness_check && (
                  <Field label={labelWith("If an answer isn't supported", TIP.groundednessMode)}>
                    <SegmentedControl
                      items={[{ value: "advisory", label: "Warn me" }, { value: "block", label: "Don't send it" }]}
                      value={draft.groundedness_mode}
                      onChange={(v) => edit("groundedness_mode", v)}
                    />
                  </Field>
                )}

                <Field label={labelWith("Refuse messages containing…", TIP.blocklist)}>
                  <TagInput
                    value={draft.blocklist}
                    onChange={(v) => edit("blocklist", v)}
                    placeholder="chargeback, legal action…"
                    tone="warn"
                    max={50}
                  />
                </Field>

                <Field
                  label={labelWith("What the person sees when it can't answer", TIP.fallback)}
                  hint="Shown for a refusal, a low-confidence answer, or a blocked phrase."
                >
                  <Textarea
                    autoGrow
                    resizable
                    value={draft.fallback_message}
                    onChange={(e) => edit("fallback_message", e.target.value)}
                    placeholder="I couldn't find that in our help docs — email support@ and a person will pick it up."
                  />
                </Field>

                <Field
                  label={labelWith("What the person sees in an urgent or distressing situation", TIP.crisis)}
                  hint="Leave empty if that doesn't apply to this agent."
                >
                  <Textarea
                    autoGrow
                    resizable
                    value={draft.crisis_message}
                    onChange={(e) => edit("crisis_message", e.target.value)}
                    placeholder="Optional"
                  />
                </Field>
              </CardBody>
            </Card>
          )}

          {/* §7.3 / part2 §8.1: the handoff block is a tab of its own. It is a
              self-contained panel — own save, own card — rather than a slice of the
              shared draft, so it sits exactly where it always has. */}
          {tab === "handoff" && (
            <EscalationPanel agentId={agentId} />
          )}

          {tab === "search" && (
            <Card>
              <CardHeader title="How it searches your sources" description={advancedSummary(draft)} />
              <CardBody className="flex flex-col gap-4">
                <div className="grid gap-4 sm:grid-cols-2">
                  <Field label={labelWith("How it searches", TIP.mode)}>
                    <Select value={draft.mode} onChange={(e) => edit("mode", e.target.value)}>
                      {["hybrid", "dense", "sparse"].map((m) => (
                        <option key={m} value={m}>{MODE_LABEL[m]}</option>
                      ))}
                    </Select>
                  </Field>
                  <Field label={labelWith("Passages to consider", TIP.topK)}>
                    <ClampedNumber value={draft.top_k} onCommit={(v) => edit("top_k", v)} min={1} max={200} />
                  </Field>
                </div>

                <div className="flex flex-col gap-2.5">
                  <ToggleRow
                    label="Re-read and re-order the best passages"
                    tooltip={TIP.rerank}
                    checked={draft.rerank_enabled}
                    onChange={(v) => edit("rerank_enabled", v)}
                  />
                  <ToggleRow
                    label="Include the surrounding section"
                    tooltip={TIP.parentChild}
                    checked={draft.parent_child}
                    onChange={(v) => edit("parent_child", v)}
                  />
                  <ToggleRow
                    label="Add document context to every passage"
                    tooltip={TIP.contextual}
                    checked={draft.contextual_retrieval}
                    onChange={(v) => edit("contextual_retrieval", v)}
                  />
                </div>

                {draft.contextual_retrieval !== storedContextual && (
                  <Banner tone="warn" icon={<Icon.Info size={17} />}>{CONTEXTUAL_WARNING}</Banner>
                )}

                {draft.rerank_enabled && (
                  <Field label={labelWith("Passages to re-read", TIP.rerankTopN)}>
                    <ClampedNumber value={draft.rerank_top_n} onCommit={(v) => edit("rerank_top_n", v)} min={1} max={50} />
                  </Field>
                )}

                <Field label={labelWith("How sure it must be before answering", TIP.minConfidence)}>
                  <Slider
                    min={0}
                    max={100}
                    step={5}
                    value={Math.round(draft.min_confidence * 100)}
                    onChange={(v) => edit("min_confidence", Math.round(v) / 100)}
                    minLabel="Answers more"
                    maxLabel="Refuses more"
                    valueLabel={`Currently ${Math.round(draft.min_confidence * 100)}%`}
                  />
                </Field>

                <div className="grid gap-4 sm:grid-cols-2">
                  <Field label={labelWith("Passages sent to the model", TIP.maxContextChunks)}>
                    <ClampedNumber value={draft.max_context_chunks} onCommit={(v) => edit("max_context_chunks", v)} min={1} max={40} />
                  </Field>
                  <Field label={labelWith("Earlier messages to remember", TIP.historyTurns)}>
                    <ClampedNumber value={draft.history_turns} onCommit={(v) => edit("history_turns", v)} min={0} max={50} />
                  </Field>
                </div>

                <ToggleRow
                  label="Summarise older messages instead of forgetting them"
                  tooltip={TIP.rollingSummary}
                  checked={draft.rolling_summary}
                  onChange={(v) => edit("rolling_summary", v)}
                />

                <p className="m-0 text-xs text-faint">
                  The rest — model name, fallback model, request size, what counts as personal information, and
                  which sources this agent may read — lives on{" "}
                  <button
                    type="button"
                    className="bg-transparent border-0 p-0 text-brand underline cursor-pointer"
                    onClick={() => nav(`/agents/${agentId}/settings`)}
                  >
                    agent settings
                  </button>.
                </p>
              </CardBody>
            </Card>
          )}

        </div>

        {/* ------------------------- right rail ------------------------- */}
        <div className="flex flex-col gap-4 min-w-0 lg:sticky lg:top-8 lg:self-start">
          <Card>
            <CardHeader title="Model" />
            <CardBody className="flex flex-col gap-4">
              <Field
                label={labelWith("Model", TIP.model)}
                hint={!catalog && !catalogLoading ? "The model list is unavailable right now — showing the current selection. You can still save." : undefined}
              >
                {modelSelect}
              </Field>
              <Field label={labelWith("Answer style", TIP.temperature)}>
                <Slider
                  min={0}
                  max={1}
                  step={0.05}
                  value={draft.temperature}
                  onChange={(v) => edit("temperature", Math.round(v * 100) / 100)}
                  minLabel="Literal"
                  maxLabel="Creative"
                  valueLabel={temperatureSentence(draft.temperature)}
                />
              </Field>
              <Field label={labelWith("Longest answer", TIP.maxOutput)}>
                <ClampedNumber
                  value={draft.maxOutputWords}
                  onCommit={(v) => edit("maxOutputWords", v)}
                  min={48}
                  max={24000}
                  suffix="words"
                />
              </Field>
            </CardBody>
          </Card>
        </div>
      </div>

      <Modal
        open={expanded}
        onClose={() => setExpanded(false)}
        title="How it should answer"
        description={`${chars.toLocaleString()} characters · about ${approxTokens.toLocaleString()} tokens`}
        size="xl"
        footer={<Button variant="primary" onClick={() => setExpanded(false)}>Done</Button>}
      >
        {promptEditor(expandedEditorClass, { autoFocus: true })}
      </Modal>

      <Modal
        open={conflictDiffOpen}
        onClose={() => setConflictDiffOpen(false)}
        title="What changed while you were editing"
        description="Yours on the left of each pair, theirs on the right. Saving anyway replaces the blocks you edited."
        size="lg"
        footer={<Button variant="secondary" onClick={() => setConflictDiffOpen(false)}>Close</Button>}
      >
        {conflict && (
          <DiffView
            before={stableJson(cfg)}
            after={stableJson(conflict.remote.config)}
            beforeLabel={`What you loaded (v${conflict.localVersion})`}
            afterLabel={`Saved by someone else (v${conflict.remote.version})`}
            context={2}
          />
        )}
      </Modal>

      {resetOpen && template && (
        <ConfirmDialog
          title={`Reset to the ${template.name} template?`}
          message={`This replaces everything in the editor with the ${template.name} instructions. Your other settings are untouched, and nothing is saved until you press Save changes.`}
          onCancel={() => setResetOpen(false)}
          onConfirm={() => {
            edit("system_prompt", template.systemPrompt);
            setResetOpen(false);
          }}
        />
      )}

      {blocker.state === "blocked" && (
        <ConfirmDialog
          title="You have unsaved changes to this agent's instructions."
          message="Leaving now discards them. Nothing has been sent to the agent yet."
          onCancel={() => blocker.reset()}
          onConfirm={() => blocker.proceed()}
        />
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ skeleton */

// Content-shaped, not a spinner: the two-column geometry and the card rhythm are
// already known, so the chunk landing should fill boxes rather than reflow them.
export function InstructionsSkeleton() {
  return (
    <div>
      <div className="flex items-start justify-between gap-4 mb-6">
        <div className="flex flex-col gap-2">
          <Skeleton className="h-7 w-40" />
          <Skeleton className="h-4 w-80" />
        </div>
        <div className="flex items-center gap-2">
          <Skeleton className="h-9 w-24" />
          <Skeleton className="h-9 w-32" />
        </div>
      </div>
      <div className="grid grid-cols-[1fr_380px] gap-8 max-lg:grid-cols-1">
        <div className="flex flex-col gap-6 min-w-0">
          <div className="flex items-center gap-1 border-b border-line mb-6">
            {["Answer", "Safety", "Handoff", "Search"].map((t) => (
              <Skeleton key={t} className="h-5 w-16" />
            ))}
          </div>
          <Card>
            <div className="px-5 py-4 border-b border-line flex flex-col gap-2">
              <Skeleton className="h-5 w-44" />
              <Skeleton className="h-3.5 w-72" />
            </div>
            <CardBody className="flex flex-col gap-4">
              <Skeleton className="h-[420px] w-full" />
              <Skeleton className="h-3.5 w-56" />
            </CardBody>
          </Card>
        </div>
        <div className="flex flex-col gap-4 min-w-0">
          <Card>
            <div className="px-5 py-4 border-b border-line"><Skeleton className="h-5 w-20" /></div>
            <CardBody className="flex flex-col gap-4">
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-10 w-28" />
            </CardBody>
          </Card>
          <Card>
            <div className="px-5 py-4 border-b border-line"><Skeleton className="h-5 w-32" /></div>
            <CardBody><Skeleton className="h-9 w-40" /></CardBody>
          </Card>
        </div>
      </div>
    </div>
  );
}
