// Agent creation (§3): the template gallery, the 3-step template wizard, and the
// 6-step guided creator. One route, one lazy chunk, `useSearchParams` for the
// mode — the user is in this flow for ninety seconds, so nested routing and a
// second prefetch entry would cost more than they buy.
//
// Both paths write through the existing POST /v1/agents with the FULL config
// blob (§3.6 step 1): `create_agent` validates the payload, so any sub-block we
// omit gets a Pydantic default written over the template's own value. Identity
// (colour + glyph) rides in `config.widget.theme`, an untyped dict on the server,
// which is why the whole identity system needs no backend change (§6.1).
//
// The website step creates a real crawl source (POST /v1/sources/website, mode
// "crawl") with `review_before_index` on. The crawl follows the links out of the
// page it was given, so the review step can show every page on that site and the
// user unchecks what does not belong — nothing is embedded until they confirm.
// Nobody types a list of paths, which is the whole point: the crawl already knows
// what is on the site. Where that endpoint is missing or outside the plan, this
// degrades to the single-page POST /v1/documents/url, once per page.
//
// One thing §3.6 marks as blocked and this screen therefore does NOT fake:
// pre-enabled actions and pre-installed procedures have no endpoint and would be
// dropped on validate, so the review step names them as what the agent *will* be
// set up with rather than claiming they are installed.
import { useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api, ApiError } from "../../lib/api";
import { tenantHeaders } from "../../lib/auth";
import { identityStyle, IDENTITY_GLYPHS } from "../../lib/agentIdentity";
import type {
  AgentDetail, CrawlProgressOut, SourceOut, SourceReviewOut,
} from "../../lib/types";
import { defaultCrawlConfig, explainSourceFailure } from "../../components/AddSourceModal";
import { LinkTree } from "../../components/LinkTree";
import {
  AGENT_TEMPLATES, ACTION_LABELS, BASELINE_GUARDRAILS, BASELINE_RETRIEVAL, CATEGORY_FILTERS,
  CATEGORY_LABEL, PRESET_LABELS, PROCEDURE_LABELS, configFromTemplate, presetsOf, templateById,
  type AgentTemplate, type FullAgentConfig,
} from "../../lib/agentTemplates";
import {
  Alert, Badge, Banner, Button, Card, CardBody, ColorPicker, Field, Input, PageHeader,
  ProgressBar, SegmentedControl, TagInput, Textarea, Tooltip, cx,
} from "../../components/ui";
import { Icon } from "../../components/ui/icons";
import { WizardShell, type WizardStep } from "../../components/ui/WizardShell";

/* -------------------------------------------------------------- url helpers */

const HOSTNAME = /^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$/i;

// Not exported: main.tsx's `page()` adapter types a route module as
// Record<string, ComponentType>, so a non-component export here fails the build.
const URL_ERROR =
  "That doesn't look like a website address. Try something like www.yourcompany.com.";

/**
 * Normalise what someone typed into a URL, or null if it is not one (§3.5 step 2).
 * Prepends https://, strips a trailing slash, and rejects anything that is not a
 * hostname with a dot — `localhost` and `my site` are both mistakes here.
 */
function normaliseUrl(raw: string): string | null {
  const trimmed = raw.trim().replace(/\/+$/, "");
  if (!trimmed) return null;
  const withScheme = /^https?:\/\//i.test(trimmed) ? trimmed : `https://${trimmed}`;
  let parsed: URL;
  try {
    parsed = new URL(withScheme);
  } catch {
    return null;
  }
  if (!HOSTNAME.test(parsed.hostname)) return null;
  return parsed.toString().replace(/\/$/, "");
}

/** `/help, /docs` → ["/help", "/docs"]. Leading slash added, blanks dropped. */
function parsePaths(raw: string): string[] {
  return raw
    .split(",")
    .map((p) => p.trim())
    .filter(Boolean)
    .map((p) => (p.startsWith("/") ? p : `/${p}`));
}

/** `https://www.acme.com/help` → `www.acme.com`, falling back to "your site". */
function hostOf(url: string | undefined): string {
  if (!url) return "your site";
  try {
    return new URL(url).hostname;
  } catch {
    return "your site";
  }
}

/* --------------------------------------------------------------- the plate */

function tint(t: { color: string; glyph: keyof typeof Icon; name: string }): CSSProperties {
  return identityStyle({ color: t.color, glyph: t.glyph, monogram: t.name.slice(0, 1) });
}

/** The 88px identity plate with its overlapping glyph tile (§6.6). */
function TemplatePlate({ template }: { template: AgentTemplate }) {
  const Glyph = Icon[template.glyph];
  return (
    <div
      className="relative h-[88px] border-b transition-colors"
      style={{
        background: "var(--agent-tint)",
        borderBottomColor: "var(--agent-hairline)",
      }}
    >
      {/* Overlaps the plate's lower edge by 12px: the one detail that stops this
          reading as a coloured table row. */}
      <div className="absolute left-5 -bottom-3 flex h-10 w-10 items-center justify-center rounded-lg border border-line bg-surface/85 shadow-card backdrop-blur-[2px]">
        <Glyph size={20} style={{ color: "var(--agent-ink)" }} />
      </div>
    </div>
  );
}

/* --------------------------------------------------------- the gallery card */

function TemplateCard({ template, onPick }: { template: AgentTemplate; onPick: () => void }) {
  const chips = template.behaviourChips.slice(0, 3);
  const overflow = template.behaviourChips.slice(3);
  return (
    <Card
      role="button"
      tabIndex={0}
      aria-label={`Use the ${template.name} template`}
      onClick={onPick}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onPick();
        }
      }}
      style={tint(template)}
      data-agent-identity
      className={cx(
        "group flex min-h-[268px] cursor-pointer flex-col overflow-hidden p-0",
        "transition-[transform,box-shadow,border-color] duration-200 hover:-translate-y-0.5 hover:border-brand hover:shadow-pop",
        "focus:outline-none focus-visible:ring-4 focus-visible:ring-brand-soft motion-reduce:transform-none",
      )}
    >
      <TemplatePlate template={template} />

      <div className="flex flex-1 flex-col px-5 pb-4 pt-5">
        <div className="flex items-start justify-between gap-3">
          <h3 className="m-0 text-[15px] font-semibold tracking-tight">{template.name}</h3>
          {template.popular && <Badge tone="accent">Popular</Badge>}
        </div>
        <p className="m-0 mt-1 line-clamp-2 min-h-[2.6rem] text-[13px] text-muted">
          {template.tagline}
        </p>

        {/* Receipt 1 — behaviour chips. Neutral, because they are facts about the
            configuration, not selections (§6.2). */}
        <div className="mt-3 flex flex-wrap gap-1.5">
          {chips.map((chip) => (
            <span key={chip} className="text-[11px]">
              <Badge tone="neutral">{chip}</Badge>
            </span>
          ))}
          {overflow.length > 0 && (
            <Tooltip label={overflow.join(" · ")}>
              <span className="text-[11px]">
                <Badge tone="neutral">+{overflow.length}</Badge>
              </span>
            </Tooltip>
          )}
        </div>

        {/* Receipt 2 — the one dashed hairline in the product: "this line is a
            fact about the thing, not part of its description." */}
        <div className="mt-auto border-t border-dashed border-line pt-3">
          <span className="text-[11px] font-semibold uppercase tracking-wide text-faint">Needs</span>
          <div className="mt-0.5 text-[13px] font-medium text-ink">{template.needs.join(" · ")}</div>
        </div>
      </div>

      {/* Receipt 3 — category eyebrow + CTA. The card is one tab stop, so the
          button is taken out of the tab order rather than duplicating it. */}
      <div className="flex items-center justify-between gap-3 border-t border-line px-5 py-3">
        <span className="text-[11px] font-semibold uppercase tracking-wide text-faint">
          {CATEGORY_LABEL[template.category]}
        </span>
        <Button
          variant="secondary"
          size="sm"
          tabIndex={-1}
          className="group-hover:border-brand group-hover:bg-brand group-hover:text-brand-fg"
        >
          Use this <Icon.Chevron size={14} className="-rotate-90" />
        </Button>
      </div>
    </Card>
  );
}

/** No plate, dashed border: absence of the plate is what marks it not-a-template. */
function ScratchCard({ onPick }: { onPick: () => void }) {
  return (
    <div className="sm:col-span-2 xl:col-span-3">
      <button
        type="button"
        onClick={onPick}
        className="flex w-full items-center gap-4 rounded-xl border border-dashed border-line bg-surface px-5 py-5 text-left transition-colors hover:border-brand focus:outline-none focus-visible:ring-4 focus-visible:ring-brand-soft"
      >
        <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg border border-line text-muted">
          <Icon.Plus size={18} />
        </span>
        <span className="min-w-0 flex-1">
          <span className="block text-[15px] font-semibold tracking-tight">Start from scratch</span>
          <span className="block text-[13px] text-muted">
            Answer five questions and we'll build the agent around your answers.
          </span>
        </span>
        <Button variant="secondary" size="sm" tabIndex={-1}>
          Start <Icon.Chevron size={14} className="-rotate-90" />
        </Button>
      </button>
    </div>
  );
}

/* ------------------------------------------------------------- the gallery */

function Gallery({ onPick, onScratch }: { onPick: (id: string) => void; onScratch: () => void }) {
  const [category, setCategory] = useState("all");
  const shown = useMemo(
    () => (category === "all" ? AGENT_TEMPLATES : AGENT_TEMPLATES.filter((t) => t.category === category)),
    [category],
  );

  return (
    <div>
      <Link
        to="/agents"
        className="mb-4 inline-flex items-center gap-1.5 text-[13px] font-medium text-muted no-underline hover:text-ink"
      >
        <Icon.Chevron size={14} className="rotate-90" /> All agents
      </Link>

      <PageHeader
        title="What should this agent do?"
        description="Pick a starting point. Everything is editable afterwards — the template just sets sensible instructions, safety rules and search settings for that job."
      />

      <div className="mb-5">
        <SegmentedControl items={CATEGORY_FILTERS} value={category} onChange={setCategory} />
      </div>

      <div className="grid gap-5 sm:grid-cols-2 xl:grid-cols-3">
        {shown.map((t) => (
          <TemplateCard key={t.id} template={t} onPick={() => onPick(t.id)} />
        ))}
        <ScratchCard onPick={onScratch} />
      </div>
    </div>
  );
}

/* ------------------------------------------------------------- step pieces */

/** Name + preset + identity (§3.5 step 1). The identity is set here and never
 *  asked about again: it is two clicks now and the agent is recognisable for the
 *  rest of its life. */
function IdentityFields({
  name, onName, color, onColor, glyph, onGlyph, presets, presetId, onPreset, nameHint,
}: {
  name: string;
  onName: (v: string) => void;
  color: string;
  onColor: (v: string) => void;
  glyph: keyof typeof Icon;
  onGlyph: (v: keyof typeof Icon) => void;
  presets: AgentTemplate[];
  presetId: string;
  onPreset: (v: string) => void;
  nameHint: string;
}) {
  return (
    <div className="space-y-6">
      <Field
        label="What will you call it?"
        hint={nameHint}
      >
        <Input value={name} onChange={(e) => onName(e.target.value)} placeholder="Support" autoFocus />
      </Field>

      {/* Only rendered when the template actually has presets: a control with one
          option is a decision the user did not need to make. */}
      {presets.length > 1 && (
        <Field label="This agent is for" hint="Changes the instructions and the suggested questions. Nothing else.">
          <SegmentedControl
            size="sm"
            items={presets.map((p) => ({ value: p.id, label: PRESET_LABELS[p.id] ?? p.name }))}
            value={presetId}
            onChange={onPreset}
          />
        </Field>
      )}

      <div className="grid gap-6 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        {/* `presets` is left to ColorPicker's own default, which is already
            IDENTITY_SWATCHES — the ten colours the templates draw from. */}
        <ColorPicker value={color} onChange={onColor} label="Colour" />
        <Field label="Glyph" hint="Shown wherever this agent appears.">
          <div className="flex flex-wrap gap-2">
            {IDENTITY_GLYPHS.map((key) => {
              const G = Icon[key];
              const on = key === glyph;
              return (
                <button
                  key={key}
                  type="button"
                  aria-label={key}
                  aria-pressed={on}
                  onClick={() => onGlyph(key)}
                  className={cx(
                    "flex h-9 w-9 items-center justify-center rounded-lg border transition-colors",
                    "focus:outline-none focus-visible:ring-4 focus-visible:ring-brand-soft",
                    on ? "border-brand bg-brand-soft text-brand" : "border-line text-muted hover:text-ink",
                  )}
                >
                  <G size={16} />
                </button>
              );
            })}
          </div>
        </Field>
      </div>

      {/* The preview is the point of the two controls above it. */}
      <div
        data-agent-identity
        style={{
          ...identityStyle({ color, glyph, monogram: (name.trim() || "A").slice(0, 1) }),
          background: "var(--agent-tint)",
          borderColor: "var(--agent-hairline)",
        }}
        className="flex items-center gap-3 rounded-xl border p-4"
      >
        <span className="flex h-10 w-10 items-center justify-center rounded-lg border border-line bg-surface shadow-card">
          {(() => {
            const G = Icon[glyph];
            return <G size={20} style={{ color: "var(--agent-ink)" }} />;
          })()}
        </span>
        <span className="min-w-0">
          <span className="block text-[13px] font-semibold">{name.trim() || "Untitled agent"}</span>
          <span className="block text-xs text-faint">This is how it will look in the sidebar.</span>
        </span>
      </div>
    </div>
  );
}

const DEFERRED_LABEL: Record<string, string> = {
  files: "files",
  text: "pasted text",
  qa: "Q&A pairs",
};

/**
 * Website + source hints (§3.5 step 2). Optional, and it says so — the ghost
 * `Skip for now` is the second-largest thing on the step because an agent with
 * no sources is still a working agent you can point at documents later.
 */
function SourcesStep({
  template, url, onUrl, paths, onPaths, deferred, onDeferred, onSkip, urlError,
}: {
  template: AgentTemplate;
  url: string;
  onUrl: (v: string) => void;
  paths: string;
  onPaths: (v: string) => void;
  deferred: string | null;
  onDeferred: (v: string | null) => void;
  onSkip: () => void;
  urlError: string | null;
}) {
  return (
    <div className="space-y-6">
      <div>
        <Field
          label="Your website"
          hint="We follow the links out of this page and list every page we find on the same site. You pick what to keep on the next step."
        >
          <div className="flex items-center gap-0">
            <span className="rounded-l-lg border border-r-0 border-line bg-canvas px-3 py-2 text-[13px] text-faint">
              https://
            </span>
            <Input
              value={url}
              onChange={(e) => onUrl(e.target.value)}
              placeholder="www.yourcompany.com"
              className="rounded-l-none"
            />
          </div>
        </Field>
        {urlError && <p className="mt-1.5 text-xs text-danger">{urlError}</p>}
      </div>

      <Field
        label="Only these sections"
        hint="Optional. Comma-separated paths on the same site — /help, /faq. Leave it empty and we look at the whole site."
      >
        <Input value={paths} onChange={(e) => onPaths(e.target.value)} placeholder="/help, /faq" />
      </Field>

      <div>
        <span className="text-[11px] font-semibold uppercase tracking-wide text-faint">
          Recommended for a {template.name.toLowerCase()} agent
        </span>
        <ul className="mt-2 space-y-1.5">
          {template.sourceHints.map((hint) => (
            <li key={`${hint.kind}:${hint.label}`} className="flex items-start gap-2 text-sm">
              <Icon.Check size={14} className="mt-0.5 shrink-0 text-ok" />
              <span>{hint.label}</span>
            </li>
          ))}
        </ul>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        {(["files", "text", "qa"] as const).map((kind) => (
          <Button
            key={kind}
            variant={deferred === kind ? "primary" : "secondary"}
            size="sm"
            onClick={() => onDeferred(deferred === kind ? null : kind)}
          >
            <Icon.Plus size={14} />
            {kind === "files" ? "Upload files" : kind === "text" ? "Paste text" : "Add Q&A pairs"}
          </Button>
        ))}
        <Button variant="ghost" size="sm" onClick={onSkip}>
          Skip for now
        </Button>
      </div>

      {deferred && (
        <Alert tone="neutral">
          Noted — you'll add {DEFERRED_LABEL[deferred]} from Knowledge (Workspace → Knowledge) once the agent
          exists. Creating it first means there is something to attach them to.
        </Alert>
      )}
    </div>
  );
}

/* ------------------------------------------------------------ the write path */

/** How many pages the fallback is allowed to pull in. The URL endpoint is one page
 *  per call, so this is a real request count, not a crawl budget. */
const MAX_PAGES = 8;

// the server excludes in one statement, up to BULK_MAX_LINKS ids per request
const BULK_CAP = 500;

// The crawl is polled while it discovers and fetches. 2s matches the Sources
// screen, and the ceiling is there so a crawl that never reports back leaves a
// screen the user can still finish from rather than a spinner with no end.
const POLL_MS = 2000;
const POLL_CEILING = 5 * 60 * 1000;

// Crawl states that are done with us: nothing more will arrive by polling.
const SETTLED = new Set(["ready", "failed", "cancelled"]);

type CreateState =
  | { phase: "idle" }
  | { phase: "creating" }
  /** The crawl is discovering and fetching. `progress` is null until the first poll. */
  | { phase: "crawling"; agent: AgentDetail; sourceId: string; progress: CrawlProgressOut | null }
  /** Parked at `awaiting_review`: the list exists and nothing has been embedded. */
  | { phase: "review"; agent: AgentDetail; sourceId: string; review: SourceReviewOut }
  | { phase: "reading"; agent: AgentDetail; done: number; total: number; current: string }
  | { phase: "ready"; agent: AgentDetail; read: number; failed: number; note?: string }
  | { phase: "failed"; message: string };

export interface WebsitePlan {
  url: string;
  /** Optional narrowing. Empty means the whole site, which is the normal case. */
  includePaths: string[];
}

/**
 * The finish button says what pressing it does, which is three different things
 * depending on how far the crawl got.
 */
function finishLabelFor(state: CreateState): string {
  switch (state.phase) {
    case "failed":
      return "Try again";
    case "review":
      return "Confirm and index these pages";
    case "crawling":
      return "Finish, review pages later";
    default:
      return "Open my agent";
  }
}

/** 404/405/501, or no response at all — the endpoint is not there to be called. */
function notDeployed(e: unknown): boolean {
  return e instanceof ApiError && (e.status === 404 || e.status === 405 || e.status === 501 || e.status === 0);
}

/** 402/403 — the endpoint exists, this workspace may not use it. */
function notAllowed(e: unknown): boolean {
  return e instanceof ApiError && (e.status === 402 || e.status === 403);
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * The create write order (§3.6): POST /v1/agents first, then the website, then
 * hand back. The agent is created before the crawl starts on purpose — if the
 * crawl fails, the user still has an agent, and a source is a retry rather than
 * a lost form.
 *
 * `run` resolves as soon as the agent and its source exist, not when the crawl
 * finishes. The crawl is watched in the background so the review step can fill in
 * underneath the user, and so pressing the finish button while it is still going
 * means "leave it running, I'll review from Sources" instead of a locked button.
 *
 * `settle` is the other half: it applies what the user unchecked and releases the
 * crawl, in that order and as one action, because an exclusion applied without the
 * approval behind it would drop pages from a crawl nobody then indexed.
 */
function useCreateAgent() {
  const [state, setState] = useState<CreateState>({ phase: "idle" });
  const [excluded, setExcluded] = useState<Set<string>>(new Set());

  // The click handlers read state after an await, by which point the closure's
  // copy can be a render behind. These are the copies they read.
  const latest = useRef<CreateState>({ phase: "idle" });
  const dropped = useRef<Set<string>>(excluded);
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; }, []);

  function commit(next: CreateState) {
    latest.current = next;
    setState(next);
  }

  function exclude(next: Set<string>) {
    dropped.current = next;
    setExcluded(next);
  }

  /** The fallback when there is no crawl endpoint: one request per named page. */
  async function readPages(agent: AgentDetail, website: WebsitePlan, note?: string) {
    const origin = new URL(website.url).origin;
    const targets = [website.url, ...website.includePaths.map((p) => `${origin}${p}`)]
      .filter((u, i, all) => all.indexOf(u) === i)
      .slice(0, MAX_PAGES);

    let read = 0;
    let failed = 0;
    for (const target of targets) {
      commit({ phase: "reading", agent, done: read + failed, total: targets.length, current: target });
      try {
        await api.post<unknown>("/v1/documents/url", { url: target }, tenantHeaders());
        read += 1;
      } catch {
        // One unreachable page is not a failed create. It is counted, reported on
        // the review step, and the user can add it again from Knowledge.
        failed += 1;
      }
    }
    commit({ phase: "ready", agent, read, failed, note });
  }

  /**
   * Follow the crawl until it parks for review or finishes. Stops the moment the
   * user has settled it or left the screen — a poll that outlives the flow would
   * write state into an unmounted tree.
   */
  async function watch(agent: AgentDetail, sourceId: string) {
    const deadline = POLL_CEILING / POLL_MS;
    for (let i = 0; i < deadline; i += 1) {
      await sleep(POLL_MS);
      if (!alive.current || latest.current.phase !== "crawling") return;
      let progress: CrawlProgressOut;
      try {
        progress = await api.get<CrawlProgressOut>(
          `/v1/sources/${sourceId}/progress`,
          tenantHeaders(),
        );
      } catch {
        // A dropped poll is not a failed crawl. Keep asking.
        continue;
      }
      if (!alive.current || latest.current.phase !== "crawling") return;

      if (progress.crawl_state === "awaiting_review") {
        try {
          const review = await api.get<SourceReviewOut>(
            `/v1/sources/${sourceId}/review`,
            tenantHeaders(),
          );
          if (!alive.current || latest.current.phase !== "crawling") return;
          commit({ phase: "review", agent, sourceId, review });
        } catch {
          // The list is the only reason to stop here. Without it, treat the crawl
          // as finished and let the Sources screen be where they review it.
          commit({ phase: "ready", agent, read: progress.link_total, failed: progress.link_failed });
        }
        return;
      }
      if (SETTLED.has(progress.crawl_state)) {
        commit({
          phase: "ready",
          agent,
          read: progress.link_trained,
          failed: progress.link_failed,
          note: progress.crawl_state === "failed" ? progress.error ?? undefined : undefined,
        });
        return;
      }
      commit({ phase: "crawling", agent, sourceId, progress });
    }
    // Out of patience, not out of crawl. The source is still going and the Sources
    // screen is where it gets reviewed — say that rather than spin forever.
    if (alive.current && latest.current.phase === "crawling") {
      commit({
        phase: "ready",
        agent,
        read: 0,
        failed: 0,
        note: "Your site is bigger than a minute — the crawl is still running. Review and index the pages it found from Knowledge (Workspace → Knowledge).",
      });
    }
  }

  async function run(
    config: FullAgentConfig,
    website: WebsitePlan | null,
    onCreated?: (agent: AgentDetail) => void,
  ): Promise<AgentDetail | null> {
    commit({ phase: "creating" });
    exclude(new Set());
    let agent: AgentDetail;
    try {
      agent = await api.post<AgentDetail>(
        "/v1/agents",
        { name: config.name, config },
        tenantHeaders(),
      );
    } catch (e) {
      commit({
        phase: "failed",
        message: e instanceof ApiError ? e.message : "We couldn't create the agent. Try again.",
      });
      return null;
    }
    onCreated?.(agent);

    if (!website) {
      commit({ phase: "ready", agent, read: 0, failed: 0 });
      return agent;
    }

    try {
      // SourceCreate is extra="forbid", so the agent rides in the query string.
      const source = await api.post<SourceOut>(
        `/v1/sources/website?agent_id=${encodeURIComponent(agent.id)}`,
        {
          mode: "crawl",
          url: website.url,
          title: null,
          config: {
            ...defaultCrawlConfig(),
            include_paths: website.includePaths,
            // The whole reason this screen can show a list: the crawl fetches its
            // frontier and stops, and embedding waits for the user.
            review_before_index: true,
          },
          tags: [],
          collection: null,
        },
        tenantHeaders(),
      );
      commit({ phase: "crawling", agent, sourceId: source.id, progress: null });
      void watch(agent, source.id);
      return agent;
    } catch (e) {
      if (notDeployed(e) || notAllowed(e)) {
        // Degraded, and it says so on the review step rather than pretending the
        // whole site was read.
        await readPages(
          agent,
          website,
          notAllowed(e)
            ? `${explainSourceFailure(e, "Crawling a whole site")} We read the page you gave us instead.`
            : "Whole-site crawling isn't available on this workspace yet, so we read the page you gave us.",
        );
        return agent;
      }
      commit({ phase: "ready", agent, read: 0, failed: 1, note: explainSourceFailure(e, "That website") });
      return agent;
    }
  }

  /**
   * Apply the unchecked links and release the crawl. True means the flow may move
   * on; false means the message is on screen and the user is still here.
   */
  async function settle(): Promise<boolean> {
    const current = latest.current;
    if (current.phase !== "review") return true;
    const ids = new Map<string, string>();
    for (const link of current.review.links) ids.set(link.url, link.id);
    try {
      const remove = [...dropped.current]
        .map((url) => ids.get(url))
        .filter((id): id is string => !!id);
      // The server takes up to BULK_MAX_LINKS ids per request; a large exclusion
      // is several requests, all before the approve.
      for (let i = 0; i < remove.length; i += BULK_CAP) {
        await api.post<{ updated: number }>(
          `/v1/sources/${current.sourceId}/links/bulk`,
          // `review_excluded` rather than the default `manual`: same decision, but
          // the pages list can then say it was dropped before indexing
          { link_ids: remove.slice(i, i + BULK_CAP), action: "exclude", reason: "review_excluded" },
          tenantHeaders(),
        );
      }
      await api.post<SourceOut>(
        `/v1/sources/${current.sourceId}/approve`,
        undefined,
        tenantHeaders(),
      );
      return true;
    } catch (e) {
      commit({
        phase: "review",
        agent: current.agent,
        sourceId: current.sourceId,
        review: current.review,
      });
      throw new Error(explainSourceFailure(e, "Those pages"));
    }
  }

  return { state, run, settle, excluded, setExcluded: exclude };
}

/* ----------------------------------------------------------- the review step */

// The one honest caveat per template: the thing this agent cannot do on day one,
// said before the user finds out from a customer (§3.5 step 3).
const REVIEW_NOTES: Record<string, string> = {
  "customer-support":
    "It answers from your documents. It cannot see an individual customer's account, so account-specific questions get routed to a human until you connect an action.",
  "order-returns":
    "Order status needs a lookup action connected to your order system. Until then it explains your policy and routes the specific order question to a human.",
  "sales-enquiries":
    "It qualifies and captures. It will not quote a price that is not in your documents, and it cannot book into a calendar until you connect one.",
  "product-docs":
    "It answers from the docs you give it. Code it writes is copied from your documentation, not invented, so undocumented APIs get a straight 'not documented'.",
  "hr-policy":
    "Do not point this one at your public website — the handbook is the source. It has no access to personnel records and says so.",
  "it-helpdesk":
    "It walks people through your runbooks. It cannot reset a password or touch a system until you connect an action, so it hands those over.",
  "appointment-booking":
    "It takes the booking details and hands them over. It cannot write into a calendar until you connect one, so confirm slots your side.",
  "developer-support":
    "It runs on a larger model and quotes your docs exactly. It will not guess at an endpoint or a parameter that is not documented.",
  admissions:
    "It answers from your prospectus and course pages. Fee and deadline answers are only as current as the documents you upload.",
  "property-enquiry":
    "Listings change daily, so re-upload them or it will describe a property that is gone. It captures requirements when nothing matches.",
};

/** The "what it won't do" receipt, derived from the config being sent rather
 *  than written by hand — a summary that can disagree with the payload is worse
 *  than no summary. */
function wontDo(config: FullAgentConfig): string[] {
  const lines: string[] = [];
  if (config.guardrails.groundedness_check) {
    lines.push(
      config.guardrails.groundedness_mode === "block"
        ? "Answers that aren't supported by your documents are blocked before they're sent."
        : "Answers that aren't supported by your documents are flagged for you to review.",
    );
  }
  if (config.guardrails.refuse_out_of_scope) {
    lines.push("Questions outside your documents get the fallback message, not a guess.");
  }
  if (config.guardrails.pii_masking && config.guardrails.pii_labels.length > 0) {
    lines.push(
      `Personal data is masked before it's stored or sent to the model: ${config.guardrails.pii_labels.join(", ").toLowerCase()}.`,
    );
  }
  if (config.guardrails.custom_pii_patterns.length > 0) {
    lines.push(
      `Plus ${config.guardrails.custom_pii_patterns.length} pattern${config.guardrails.custom_pii_patterns.length === 1 ? "" : "s"} specific to this job: ${config.guardrails.custom_pii_patterns.map((p) => p.label.toLowerCase()).join(", ")}.`,
    );
  }
  if (config.guardrails.blocklist.length > 0) {
    lines.push(`It refuses anything about: ${config.guardrails.blocklist.join(", ")}.`);
  }
  if (config.guardrails.injection_screening) {
    lines.push("Attempts to talk it out of its instructions are screened out.");
  }
  return lines;
}

function ReviewSection({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div>
      <span className="text-[11px] font-semibold uppercase tracking-wide text-faint">{title}</span>
      <div className="mt-1.5 space-y-1.5 text-sm">{children}</div>
    </div>
  );
}

/**
 * §3.5 step 3. Everything on this card is either a fact about the config being
 * written or a fact about what is happening right now — no encouragement, no
 * "you're all set!", because the point of the step is that the user can catch a
 * wrong template before they hand the agent a customer.
 */
function ReviewCard({
  template, config, state, website, notes, excluded, onExcluded,
}: {
  template: AgentTemplate;
  config: FullAgentConfig;
  state: CreateState;
  website: WebsitePlan | null;
  notes: string[];
  excluded: Set<string>;
  onExcluded: (next: Set<string>) => void;
}) {
  const caveat = REVIEW_NOTES[template.presetOf ?? template.id];
  return (
    <div className="space-y-6">
      <div
        data-agent-identity
        style={{
          ...identityStyle({ color: String(config.widget.theme.accent ?? template.color), glyph: template.glyph, monogram: config.name.slice(0, 1) }),
          background: "var(--agent-tint)",
          borderColor: "var(--agent-hairline)",
        }}
        className="rounded-xl border p-5"
      >
        <h2 className="m-0 text-2xl font-semibold tracking-tight">
          {config.name || template.name} is ready
        </h2>
        <p className="m-0 mt-1 text-sm text-muted">{template.resolved}</p>
      </div>

      {/* What is happening right now, with the page count, because "setting
          things up…" is the sentence that makes people reload. */}
      {state.phase === "creating" && (
        <ProgressBar value={0} indeterminate label="Creating the agent" sub="Writing its instructions and safety rules." />
      )}
      {state.phase === "crawling" && (
        <ProgressBar
          value={0}
          indeterminate
          label={
            (state.progress?.link_total ?? 0) > 1
              ? `Looking through your site — ${state.progress?.link_total} pages found so far`
              : "Looking through your site"
          }
          sub="We follow the links out of the page you gave us. Nothing is added to your assistant until you confirm the list."
        />
      )}
      {state.phase === "reading" && (
        <ProgressBar
          value={state.done}
          max={state.total}
          label={`Reading ${state.total === 1 ? "1 page" : `${state.done} of ${state.total} pages`}`}
          sub={state.current}
        />
      )}

      {/* The list, and the only place on this screen with something to decide.
          Above the summary card, because the summary can be read afterwards and
          this cannot. */}
      {state.phase === "review" && (
        <Card>
          <CardBody className="space-y-4">
            <div>
              <h3 className="m-0 text-base font-semibold tracking-tight">
                Here are the links we found
              </h3>
              <p className="m-0 mt-1 text-[13px] text-muted">
                {`Everything on ${hostOf(website?.url ?? state.review.links[0]?.url)}. Pages on other sites — your Instagram, your payment provider — were never followed, so they are not here.`}
              </p>
            </div>
            <LinkTree
              groups={state.review.groups}
              links={state.review.links}
              excluded={excluded}
              onExcludedChange={onExcluded}
              emptyMessage="No pages could be read from that address."
            />
            {state.review.truncated && (
              <Banner tone="warn">
                Your site is larger than this list. You are seeing the first{" "}
                {state.review.total} pages; indexing still covers every page that was read.
              </Banner>
            )}
          </CardBody>
        </Card>
      )}

      {state.phase === "ready" && state.read > 0 && (
        <Alert tone="ok">
          Read {state.read} page{state.read === 1 ? "" : "s"}
          {state.failed > 0
            ? ` — ${state.failed} couldn't be reached. Add ${state.failed === 1 ? "it" : "them"} again from Knowledge.`
            : ". It's searchable now."}
        </Alert>
      )}
      {state.phase === "ready" && state.read === 0 && !state.note && (
        <Alert tone="warn">
          {website
            ? "None of those pages could be read. The agent exists — add sources from Knowledge and try again."
            : "No sources yet. The agent will use the fallback message until you give it something to search."}
        </Alert>
      )}
      {state.phase === "ready" && state.note && <Alert tone="warn">{state.note}</Alert>}
      {state.phase === "failed" && <Alert tone="danger">{state.message}</Alert>}

      <Card>
        <CardBody className="space-y-5">
          <ReviewSection title="What it does">
            <p className="m-0">{template.business}</p>
            <p className="m-0 text-muted">Talking to it: {template.endUser}</p>
          </ReviewSection>

          <ReviewSection title="What it searches">
            {website ? (
              <p className="m-0">
                {hostOf(website.url)}
                {website.includePaths.length > 0
                  ? ` — only ${website.includePaths.join(", ")}`
                  : " — every page we can reach from that address"}
              </p>
            ) : (
              <p className="m-0 text-muted">Nothing yet — {template.needs.join(", ").toLowerCase()}.</p>
            )}
          </ReviewSection>

          <ReviewSection title="What it won't do">
            <ul className="m-0 list-none space-y-1.5 p-0">
              {wontDo(config).map((line) => (
                <li key={line} className="flex items-start gap-2">
                  <Icon.Shield size={14} className="mt-0.5 shrink-0 text-faint" />
                  <span>{line}</span>
                </li>
              ))}
            </ul>
          </ReviewSection>

          <ReviewSection title="When it can't answer">
            <p className="m-0 rounded-lg border border-line bg-canvas p-3 text-[13px]">
              “{config.guardrails.fallback_message}”
            </p>
          </ReviewSection>

          {(template.actions.length > 0 || template.procedures.length > 0) && (
            <ReviewSection title="Set up next">
              <p className="m-0 text-muted">
                This job usually needs these. They're configured on the agent's own screens, so
                nothing is switched on behind your back.
              </p>
              <div className="flex flex-wrap gap-1.5">
                {template.actions.map((id) => (
                  <Badge key={id} tone="neutral">{ACTION_LABELS[id] ?? id}</Badge>
                ))}
                {template.procedures.map((id) => (
                  <Badge key={id} tone="neutral">{PROCEDURE_LABELS[id] ?? id}</Badge>
                ))}
              </div>
            </ReviewSection>
          )}

          {notes.length > 0 && (
            <ReviewSection title="From your answers">
              <ul className="m-0 list-none space-y-1.5 p-0">
                {notes.map((line) => (
                  <li key={line} className="flex items-start gap-2">
                    <Icon.Check size={14} className="mt-0.5 shrink-0 text-ok" />
                    <span>{line}</span>
                  </li>
                ))}
              </ul>
            </ReviewSection>
          )}
        </CardBody>
      </Card>

      {caveat && (
        <Banner tone="warn" icon={<Icon.Info size={16} />}>
          {caveat}
        </Banner>
      )}
    </div>
  );
}

/* --------------------------------------------------------- the template path */

/** The wizard's aside: the card they clicked, so they can see they picked the
 *  right one without going back. */
function WizardAside({ template }: { template: AgentTemplate }) {
  const Glyph = Icon[template.glyph];
  return (
    <Card style={tint(template)} data-agent-identity className="overflow-hidden p-0">
      <div className="h-1.5" style={{ background: "var(--agent-color)" }} />
      <CardBody className="space-y-3">
        <div className="flex items-center gap-2.5">
          <span className="flex h-8 w-8 items-center justify-center rounded-lg border border-line bg-surface">
            <Glyph size={16} style={{ color: "var(--agent-ink)" }} />
          </span>
          <span className="text-[13px] font-semibold">{template.name}</span>
        </div>
        <p className="m-0 text-[13px] text-muted">{template.tagline}</p>
        <div>
          <span className="text-[11px] font-semibold uppercase tracking-wide text-faint">Needs</span>
          <div className="mt-0.5 text-[13px] font-medium">{template.needs.join(" · ")}</div>
        </div>
        <div className="flex flex-wrap gap-1.5">
          {template.behaviourChips.map((chip) => (
            <span key={chip} className="text-[11px]">
              <Badge tone="neutral">{chip}</Badge>
            </span>
          ))}
        </div>
      </CardBody>
    </Card>
  );
}

const TEMPLATE_REVIEW = 2;

/**
 * Three steps (§3.5): name and identity, sources, review. The create fires when
 * the review step is entered rather than when the wizard finishes, because §3.6
 * wants the pages being read while the user reads the summary — by the time they
 * press the button, the work is usually done.
 */
function TemplateWizard({ template, onExit }: { template: AgentTemplate; onExit: () => void }) {
  const navigate = useNavigate();
  const family = useMemo(() => presetsOf(template.presetOf ?? template.id), [template]);
  const [presetId, setPresetId] = useState(template.id);
  const active = templateById(presetId) ?? template;

  const [name, setName] = useState("");
  const [color, setColor] = useState(template.color);
  const [glyph, setGlyph] = useState<keyof typeof Icon>(template.glyph);
  const [url, setUrl] = useState("");
  const [paths, setPaths] = useState("");
  const [deferred, setDeferred] = useState<string | null>(null);
  const [step, setStep] = useState(0);

  const { state, run, settle, excluded, setExcluded } = useCreateAgent();
  const pending = useRef<Promise<AgentDetail | null> | null>(null);

  const urlBroken = url.trim().length > 0 && normaliseUrl(url) === null;
  const website = useMemo<WebsitePlan | null>(() => {
    const normalised = normaliseUrl(url);
    return normalised ? { url: normalised, includePaths: parsePaths(paths) } : null;
  }, [url, paths]);

  const config = useMemo(
    () => configFromTemplate(active, { name: name.trim() || active.name, color, glyph }),
    [active, name, color, glyph],
  );

  const notes = useMemo(() => {
    const out: string[] = [];
    if (deferred) out.push(`You'll add ${DEFERRED_LABEL[deferred]} from Knowledge (Workspace → Knowledge).`);
    if (family.length > 1) out.push(`Instructions written for: ${PRESET_LABELS[active.id] ?? active.name}.`);
    return out;
  }, [deferred, family.length, active]);

  function start(): Promise<AgentDetail | null> {
    pending.current = run(config, website);
    return pending.current;
  }

  // Entering the review step is the trigger. `pending` guards it so a re-render
  // (or StrictMode's double effect) cannot create two agents.
  useEffect(() => {
    if (step === TEMPLATE_REVIEW && !pending.current) void start();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [step]);

  async function finish(): Promise<void> {
    const agent = await (pending.current ?? start());
    if (!agent) {
      // Failed. The review card is already showing why; drop the guard so the
      // next press retries instead of resolving the dead promise again.
      pending.current = null;
      return;
    }
    // Throws with a tenant-facing message when the approve is refused, which
    // WizardShell shows under the footer and leaves the list on screen.
    await settle();
    navigate(`/agents/${agent.id}/instructions`, { replace: true });
  }

  const steps: WizardStep[] = [
    {
      id: "identity",
      title: "Name it",
      render: () => (
        <IdentityFields
          name={name}
          onName={setName}
          color={color}
          onColor={setColor}
          glyph={glyph}
          onGlyph={setGlyph}
          presets={family}
          presetId={presetId}
          onPreset={setPresetId}
          nameHint="Your team sees this in the sidebar. Pick something you'd say out loud — “Support”, not “Agent 2”."
        />
      ),
      validate: () =>
        name.trim().length === 0
          ? "Give it a name so you can tell it apart from your other agents."
          : null,
    },
    {
      id: "sources",
      title: "Point it at your content",
      optional: true,
      render: ({ next }) => (
        <SourcesStep
          template={active}
          url={url}
          onUrl={setUrl}
          paths={paths}
          onPaths={setPaths}
          deferred={deferred}
          onDeferred={setDeferred}
          onSkip={() => {
            setUrl("");
            setPaths("");
            next();
          }}
          urlError={urlBroken ? URL_ERROR : null}
        />
      ),
      validate: () => (urlBroken ? URL_ERROR : null),
    },
    {
      id: "review",
      title: "Check it over",
      render: () => (
        <ReviewCard
          template={active}
          config={config}
          state={state}
          website={website}
          notes={notes}
          excluded={excluded}
          onExcluded={setExcluded}
        />
      ),
    },
  ];

  return (
    <div>
      <button
        type="button"
        onClick={onExit}
        className="mb-4 inline-flex items-center gap-1.5 border-0 bg-transparent p-0 text-[13px] font-medium text-muted hover:text-ink"
      >
        <Icon.Chevron size={14} className="rotate-90" /> All templates
      </button>
      <WizardShell
        steps={steps}
        current={step}
        onStepChange={setStep}
        onFinish={finish}
        onCancel={onExit}
        finishLabel={finishLabelFor(state)}
        aside={<WizardAside template={active} />}
      />
    </div>
  );
}

/* ---------------------------------------------------------- the scratch path */

/**
 * The base for "Something else" (§3.7 step 1). Not a gallery card — the ten
 * templates exist because they are specific, and a general assistant on that
 * grid would be the one every unsure person picked. Here it is the honest answer
 * to "none of these is my job".
 */
const SOMETHING_ELSE: AgentTemplate = {
  id: "general",
  name: "General assistant",
  tagline: "Answers questions from whatever documents you give it, and says so when it can't.",
  glyph: "Chat",
  color: "#2563eb",
  category: "customer",
  business:
    "A team with documents worth searching and no single job title for the agent that searches them.",
  endUser: "Whoever you give the link to.",
  resolved:
    "The question was answered from your documents with a citation, or the person was told plainly that the documents don't cover it.",
  behaviourChips: ["Cites its sources", "Refuses what it can't ground", "Masks personal data"],
  needs: ["Any documents", "Or a website"],
  systemPrompt: `You answer questions using only the documents provided to you.

How to answer
- Answer in plain language, as briefly as the question allows.
- Name the document you took the answer from.
- If two documents disagree, say so and quote both.

Hard limits
- Never state a fact, a number, a price or a date that is not in the documents.
- If the documents do not cover the question, say so and say what you would
  need to answer it. Never fill the gap with a guess.
- Never give legal, medical or financial advice.

Tone
- Direct and useful. No filler, no apologising twice.`,
  retrieval: {},
  guardrails: {
    fallback_message:
      "I don't have that in my documents. If you can tell me a bit more, I'll check again — otherwise someone from the team can pick this up.",
  },
  model: {},
  context: {},
  actions: ["escalate_to_human"],
  procedures: [],
  sourceHints: [
    { kind: "website", label: "Your website, or the section of it that answers questions" },
    { kind: "files", label: "Any PDFs, docs or exports worth searching" },
  ],
};

/** Step 1's grid: the same ten templates, small enough to compare at a glance. */
function CompactTemplateGrid({
  value, onChange,
}: {
  value: string;
  onChange: (id: string) => void;
}) {
  const options = [...AGENT_TEMPLATES, SOMETHING_ELSE];
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      {options.map((t) => {
        const G = Icon[t.glyph];
        const on = t.id === value;
        return (
          <button
            key={t.id}
            type="button"
            aria-pressed={on}
            onClick={() => onChange(t.id)}
            data-agent-identity
            style={tint(t)}
            className={cx(
              "flex items-start gap-3 rounded-xl border p-3 text-left transition-colors",
              "focus:outline-none focus-visible:ring-4 focus-visible:ring-brand-soft",
              on ? "border-brand bg-brand-soft" : "border-line bg-surface hover:border-brand",
            )}
          >
            <span
              className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-line bg-surface"
              style={{ background: "var(--agent-tint)" }}
            >
              <G size={16} style={{ color: "var(--agent-ink)" }} />
            </span>
            <span className="min-w-0">
              <span className="block text-[13px] font-semibold">{t.name}</span>
              <span className="mt-0.5 block line-clamp-2 text-xs text-muted">{t.tagline}</span>
            </span>
          </button>
        );
      })}
    </div>
  );
}

/** A real radio group, because §3.7 step 6's four options are sentences and a
 *  SegmentedControl would put four sentences on one line. */
function RadioGroup({
  value, onChange, options, label,
}: {
  value: string;
  onChange: (v: string) => void;
  options: { value: string; label: string; hint?: string }[];
  label: string;
}) {
  return (
    <div role="radiogroup" aria-label={label} className="space-y-2">
      {options.map((o) => {
        const on = o.value === value;
        return (
          <button
            key={o.value}
            type="button"
            role="radio"
            aria-checked={on}
            onClick={() => onChange(o.value)}
            className={cx(
              "flex w-full items-start gap-3 rounded-lg border p-3 text-left transition-colors",
              "focus:outline-none focus-visible:ring-4 focus-visible:ring-brand-soft",
              on ? "border-brand bg-brand-soft" : "border-line bg-surface hover:border-brand",
            )}
          >
            <span
              className={cx(
                "mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full border",
                on ? "border-brand" : "border-line",
              )}
            >
              {on && <span className="h-2 w-2 rounded-full bg-brand" />}
            </span>
            <span className="min-w-0">
              <span className="block text-sm font-medium">{o.label}</span>
              {o.hint && <span className="mt-0.5 block text-xs text-faint">{o.hint}</span>}
            </span>
          </button>
        );
      })}
    </div>
  );
}

/* The four prefilled boundary chips (§3.7 step 5), and the prompt line each one
 * becomes. The fourth is different in kind: it maps to a config field
 * (`refuse_out_of_scope`) rather than to prompt text, which is why it is listed
 * here but excluded from the Boundaries block. */
const BOUNDARY_CHIPS = [
  "Give legal advice",
  "Give medical advice",
  "Promise prices or dates not in your documents",
  "Answer questions your documents don't cover",
];

const OUT_OF_SCOPE_CHIP = BOUNDARY_CHIPS[3]!;

const BOUNDARY_LINES: Record<string, string> = {
  "Give legal advice":
    "Never give legal advice. Say that a qualified professional has to answer that.",
  "Give medical advice":
    "Never give medical advice. Say that a qualified professional has to answer that.",
  "Promise prices or dates not in your documents":
    "Never promise a price, a date or a delivery time that is not written in the documents.",
  "Answer questions your documents don't cover":
    "If the documents do not cover the question, say so instead of answering it.",
};

/** ≤ 5 words is the test §3.7 step 5 sets for "this is a phrase, so it can also
 *  go in the blocklist". A sentence goes in the prompt only. */
function looksLikeAPhrase(entry: string): boolean {
  return entry.trim().split(/\s+/).filter(Boolean).length <= 5;
}

/** Append the Boundaries block. The template's own prompt is left exactly as it
 *  was — this reads as a list the owner added, because that is what it is. */
function withBoundaries(prompt: string, chips: string[]): string {
  const lines = chips.map((chip) => BOUNDARY_LINES[chip] ?? `Do not: ${chip}`);
  if (lines.length === 0) return prompt;
  return `${prompt}\n\nBoundaries\n${lines.map((l) => `- ${l}`).join("\n")}`;
}

const SCRATCH_REVIEW = 6;

const ESCALATION_OPTIONS = [
  { value: "handoff", label: "Say so and offer to pass it on", hint: "Offers a human. Nothing is sent anywhere automatically." },
  { value: "email", label: "Say so and email us", hint: "Needs an address." },
  { value: "slack", label: "Say so and post to Slack", hint: "Needs a channel." },
  { value: "none", label: "Just say so", hint: "No handoff. The agent gets slightly more cautious about answering at all." },
];

/**
 * §3.7. Six questions about the business, then the same review card as the
 * template path. The principle is in the spec and it is the whole reason this
 * path exists: ask about the business, infer the configuration. Nobody on this
 * screen sees the word "retrieval", and every field it never asks about
 * (top_k, rerank, temperature, history turns, PII labels…) is inherited from the
 * job picked in step 1 and editable afterwards in Instructions → Advanced.
 */
function ScratchWizard({ onExit }: { onExit: () => void }) {
  const navigate = useNavigate();
  const [step, setStep] = useState(0);

  const [baseId, setBaseId] = useState("");
  const base = useMemo(() => templateById(baseId) ?? (baseId === SOMETHING_ELSE.id ? SOMETHING_ELSE : null), [baseId]);

  const [name, setName] = useState("");
  const [nameTouched, setNameTouched] = useState(false);
  const [url, setUrl] = useState("");
  const [paths, setPaths] = useState("");
  const [filesInstead, setFilesInstead] = useState(false);
  const [audience, setAudience] = useState("");
  const [boundaries, setBoundaries] = useState<string[]>(BOUNDARY_CHIPS);
  const [escalation, setEscalation] = useState("handoff");
  const [destination, setDestination] = useState("");
  const [fallback, setFallback] = useState("");
  const [fallbackTouched, setFallbackTouched] = useState(false);

  const { state, run, settle, excluded, setExcluded } = useCreateAgent();
  const pending = useRef<Promise<AgentDetail | null> | null>(null);

  // Step 2 is "prefilled from step 1", and step 6's textarea is prefilled from
  // the same place. Both stop following the template the moment the user types.
  useEffect(() => {
    if (!base) return;
    if (!nameTouched) setName(base.name);
    if (!fallbackTouched) {
      setFallback(base.guardrails.fallback_message ?? BASELINE_GUARDRAILS.fallback_message);
    }
  }, [base, nameTouched, fallbackTouched]);

  const urlBroken = url.trim().length > 0 && normaliseUrl(url) === null;
  const website = useMemo<WebsitePlan | null>(() => {
    const normalised = normaliseUrl(url);
    return normalised ? { url: normalised, includePaths: parsePaths(paths) } : null;
  }, [url, paths]);

  const promptChips = boundaries.filter((c) => c !== OUT_OF_SCOPE_CHIP);
  const freeText = boundaries.filter((c) => !BOUNDARY_CHIPS.includes(c));
  const phrases = freeText.filter(looksLikeAPhrase);

  const config = useMemo<FullAgentConfig | null>(() => {
    if (!base) return null;
    const baseMin = base.retrieval.min_confidence ?? BASELINE_RETRIEVAL.min_confidence;
    return configFromTemplate(base, {
      name: name.trim() || base.name,
      systemPrompt: withBoundaries(base.systemPrompt, promptChips),
      guardrails: {
        refuse_out_of_scope: boundaries.includes(OUT_OF_SCOPE_CHIP),
        blocklist: [...(base.guardrails.blocklist ?? []), ...phrases],
        fallback_message: fallback.trim() || (base.guardrails.fallback_message ?? BASELINE_GUARDRAILS.fallback_message),
      },
      // The one derived number, and the only place deriving it is honest: picking
      // a handoff route means the tenant accepts handoffs, so the template's own
      // threshold stands. "Just say so" means there is no route, so the agent has
      // to be more sure before it answers at all.
      retrieval: escalation === "none" ? { min_confidence: Number((baseMin + 0.05).toFixed(2)) } : {},
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [base, name, boundaries, fallback, escalation]);

  const notes = useMemo(() => {
    const out: string[] = [];
    if (audience === "team") out.push("Internal only — no public chat window, and personal data stays masked.");
    if (audience === "customers") out.push("Customer-facing — answers people who message it directly.");
    if (audience === "both") out.push("Used by customers and your team.");
    if (promptChips.length > 0) out.push(`${promptChips.length} boundaries added to its instructions.`);
    if (phrases.length > 0) out.push(`Refuses anything about: ${phrases.join(", ")}.`);
    if (!boundaries.includes(OUT_OF_SCOPE_CHIP)) {
      out.push("Allowed to answer beyond your documents — it will still say when it isn't sure.");
    }
    if (escalation === "email" || escalation === "slack") {
      out.push(`Handoff to ${destination.trim() || "a destination"} still needs switching on from the agent's escalation screen.`);
    }
    if (escalation === "none") out.push("No handoff — it needs to be more certain before it answers.");
    if (filesInstead) out.push("You'll upload files from Knowledge (Workspace → Knowledge).");
    return out;
  }, [audience, promptChips.length, phrases, boundaries, escalation, destination, filesInstead]);

  function start(): Promise<AgentDetail | null> {
    if (!config) return Promise.resolve(null);
    pending.current = run(config, filesInstead ? null : website);
    return pending.current;
  }

  useEffect(() => {
    if (step === SCRATCH_REVIEW && !pending.current) void start();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [step]);

  async function finish(): Promise<void> {
    const agent = await (pending.current ?? start());
    if (!agent) {
      pending.current = null;
      return;
    }
    await settle();
    navigate(`/agents/${agent.id}/instructions`, { replace: true });
  }

  const steps: WizardStep[] = [
    {
      id: "job",
      title: "What will this agent do?",
      render: () => (
        <div className="space-y-4">
          <p className="m-0 text-sm text-muted">
            Pick the closest job. We'll set it up for that and you can change anything later.
          </p>
          <CompactTemplateGrid value={baseId} onChange={setBaseId} />
        </div>
      ),
      validate: () => (base ? null : "Pick the closest job — everything is editable afterwards."),
    },
    {
      id: "name",
      title: "What should we call it?",
      render: () => (
        <Field label="Name" hint="Your team sees this name. Customers see it in the chat header.">
          <Input
            value={name}
            onChange={(e) => {
              setNameTouched(true);
              setName(e.target.value);
            }}
            maxLength={60}
            autoFocus
          />
        </Field>
      ),
      validate: () => {
        const trimmed = name.trim();
        if (trimmed.length === 0) return "Give it a name — one word is fine.";
        if (trimmed.length > 60) return "Keep the name under 60 characters.";
        return null;
      },
    },
    {
      id: "website",
      title: "What's your website?",
      optional: true,
      render: () => (
        <div className="space-y-6">
          <p className="m-0 text-sm text-muted">
            We follow the links out of the page you give us and list everything we find on the
            same site. You pick what to keep before any of it is indexed.
          </p>
          <div>
            <Field label="Website" hint="The page — or the help section — that answers the questions you get asked.">
              <div className="flex items-center gap-0">
                <span className="rounded-l-lg border border-r-0 border-line bg-canvas px-3 py-2 text-[13px] text-faint">
                  https://
                </span>
                <Input
                  value={url}
                  onChange={(e) => {
                    setFilesInstead(false);
                    setUrl(e.target.value);
                  }}
                  placeholder="www.yourcompany.com/help"
                  className="rounded-l-none"
                />
              </div>
            </Field>
            {urlBroken && <p className="mt-1.5 text-xs text-danger">{URL_ERROR}</p>}
          </div>
          <Field label="Only these sections" hint="Optional. Comma-separated paths, same site — /help, /faq, /pricing. Leave it empty for the whole site.">
            <Input value={paths} onChange={(e) => setPaths(e.target.value)} placeholder="/help, /faq" />
          </Field>
          <div className="flex items-center gap-2">
            <Button
              variant={filesInstead ? "primary" : "secondary"}
              size="sm"
              onClick={() => setFilesInstead(!filesInstead)}
            >
              <Icon.Upload size={14} /> Upload files instead
            </Button>
            {filesInstead && (
              <span className="text-xs text-faint">
                We'll skip the website — you can upload files from Knowledge once it's set up.
              </span>
            )}
          </div>
          {audience === "team" && url.trim().length > 0 && (
            <Alert tone="warn">
              This one is for your team, and your public website usually answers a different
              question than your handbook does. Upload the internal documents instead.
            </Alert>
          )}
        </div>
      ),
      validate: () => (urlBroken && !filesInstead ? URL_ERROR : null),
    },
    {
      id: "audience",
      title: "Who will be talking to it?",
      render: () => (
        <div className="space-y-4">
          <SegmentedControl
            items={[
              { value: "customers", label: "Our customers" },
              { value: "team", label: "Our team" },
              { value: "both", label: "Both" },
            ]}
            value={audience}
            onChange={setAudience}
          />
          {audience === "team" && (
            <Alert tone="neutral">
              Internal. We won't nudge you to embed a chat window, and personal data stays masked —
              internal doesn't mean unmasked.
            </Alert>
          )}
          {audience === "customers" && (
            <Alert tone="neutral">
              Customer-facing. Connect WhatsApp or email on the agent's channels screen when
              you're ready to go live.
            </Alert>
          )}
        </div>
      ),
      validate: () => (audience ? null : "Tell us who's talking to it — it changes the defaults."),
    },
    {
      id: "boundaries",
      title: "What should it never do?",
      render: () => (
        <div className="space-y-4">
          <p className="m-0 text-sm text-muted">
            Add anything it must refuse. Most teams leave this as it is.
          </p>
          <TagInput
            value={boundaries}
            onChange={setBoundaries}
            placeholder="+ Add your own"
            tone="warn"
          />
          <p className="m-0 text-xs text-faint">
            Short entries also become a hard refusal list. Longer ones are added to its
            instructions as written.
          </p>
        </div>
      ),
    },
    {
      id: "fallback",
      title: "What should happen when it can't answer?",
      render: () => (
        <div className="space-y-6">
          <RadioGroup
            label="What should happen when it can't answer?"
            value={escalation}
            onChange={setEscalation}
            options={ESCALATION_OPTIONS}
          />
          {(escalation === "email" || escalation === "slack") && (
            <Field
              label={escalation === "email" ? "Email address" : "Slack channel"}
              hint="Saved with the agent. We'll ask you to confirm it when handoffs are switched on."
            >
              <Input
                value={destination}
                onChange={(e) => setDestination(e.target.value)}
                placeholder={escalation === "email" ? "support@yourcompany.com" : "#support"}
              />
            </Field>
          )}
          <Field label="What the person sees" hint="Plain language. This is the message they get when there's no answer in your documents.">
            <Textarea
              value={fallback}
              onChange={(e) => {
                setFallbackTouched(true);
                setFallback(e.target.value);
              }}
              rows={4}
              maxLength={400}
            />
          </Field>
        </div>
      ),
      validate: () => {
        const trimmed = fallback.trim();
        if (trimmed.length === 0) return "Write the message the person sees. One sentence is enough.";
        if (trimmed.length > 400) return "Keep the message under 400 characters.";
        if (escalation === "email" && !destination.includes("@")) {
          return "That email address needs an @ in it.";
        }
        if (escalation === "slack" && destination.trim().length === 0) {
          return "Which channel should it post to?";
        }
        return null;
      },
    },
    {
      id: "review",
      title: "Check it over",
      render: () =>
        base && config ? (
          <ReviewCard
            template={base}
            config={config}
            state={state}
            website={filesInstead ? null : website}
            notes={notes}
            excluded={excluded}
            onExcluded={setExcluded}
          />
        ) : (
          <Alert tone="warn">Go back and pick what this agent is for.</Alert>
        ),
    },
  ];

  return (
    <div>
      <button
        type="button"
        onClick={onExit}
        className="mb-4 inline-flex items-center gap-1.5 border-0 bg-transparent p-0 text-[13px] font-medium text-muted hover:text-ink"
      >
        <Icon.Chevron size={14} className="rotate-90" /> All templates
      </button>
      <WizardShell
        steps={steps}
        current={step}
        onStepChange={setStep}
        onFinish={finish}
        onCancel={onExit}
        finishLabel={finishLabelFor(state)}
        aside={base ? <WizardAside template={base} /> : undefined}
      />
    </div>
  );
}

/* ------------------------------------------------------------------ the route */

/**
 * `/agents/new` (§3.1). One route, one chunk, three modes read off the query
 * string: no params is the gallery, `?template=<id>` is the three-step wizard,
 * `?custom=1` is the guided creator. An unknown template id falls back to the
 * gallery rather than dead-ending, because a stale bookmark should cost a click,
 * not a support ticket.
 */
export function AgentCreate() {
  const [params, setParams] = useSearchParams();
  const templateId = params.get("template");
  const custom = params.get("custom") === "1";
  const template = templateById(templateId);

  const toGallery = () => setParams({}, { replace: true });

  if (custom) return <ScratchWizard onExit={toGallery} />;
  if (template) {
    // Keyed on the id so switching templates resets the wizard's state instead of
    // carrying one template's name and colour into another's.
    return <TemplateWizard key={template.id} template={template} onExit={toGallery} />;
  }
  return (
    <Gallery
      onPick={(id) => setParams({ template: id })}
      onScratch={() => setParams({ custom: "1" })}
    />
  );
}
