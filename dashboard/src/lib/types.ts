// Response types mirroring app/api/schemas.py. Hand-written
// rather than generated to avoid a codegen step; the pairing is kept honest by
// review. Every field the API can return null is null here too — strictness is
// the point.

export interface DocumentOut {
  id: string;
  filename: string;
  mime: string;
  size_bytes: number;
  status: "queued" | "processing" | "ready" | "failed" | "deleted";
  error: string | null;
  page_count: number;
  chunk_count: number;
  tier_counts: Record<string, number>;
  tags: string[];
  collection: string | null;
  metadata: Record<string, unknown>;
  created_at: string | null;
  indexed_at: string | null;
}

export interface UploadResponse {
  document: DocumentOut;
  duplicate: boolean;
}

/** `config.actions` — the workspace-wide ceilings on action use. */
export interface ActionsConfig {
  web_search_mode?: "off" | "tool" | "native";
  max_writes_per_conversation?: number;
  max_calls_per_conversation?: number;
  confirm_all_writes?: boolean;
  require_identity_for_all?: boolean;
  show_action_names?: boolean;
}

// The agent config blob. Loose on purpose — the editor round-trips it and the
// server validates, so the client does not re-encode every nested default.
export interface AgentConfig {
  name: string;
  system_prompt: string;
  model: Record<string, unknown>;
  retrieval: Record<string, unknown>;
  tools: Record<string, unknown>;
  guardrails: Record<string, unknown>;
  context: Record<string, unknown>;
  widget: Record<string, unknown>;
  /** Served on every read; absent on a config written before it existed. */
  actions?: ActionsConfig;
  /**
   * Handoff settings. Typed rather than left a loose record because two screens
   * write it — the handoff panel under Instructions and the email card on
   * Channels — and the server's config merge is shallow at the top level, so
   * a block sent without every field replaces the stored one. A typed block is how
   * a caller knows what it has to send back.
   */
  escalation: EscalationConfig;
  version: number;
}

export interface AgentSummary {
  id: string;
  name: string;
  is_default: boolean;
  version: number;
  // Returned by GET /v1/agents and GET /v1/agents/{id}; optional because the
  // client also synthesises summaries from a create response.
  created_at?: string | null;
  updated_at?: string | null;
  // Index-card metrics (§2.5). The backend does not report these yet (B3), and
  // `undefined` means exactly that: the card renders a Skeleton in the metric
  // row rather than a confident `0 sources`, so the layout is final on day one
  // and never reflows when B3 lands.
  source_count?: number;
  conversations_7d?: number;
  channels?: string[];
  /** Template the agent was created from. Written by the create flow (Wave 3). */
  template?: string | null;
  /**
   * Registry-managed swarm agents (plan agent.md §10). The backend materializes
   * one `agents` row per registry entry with `kind: "swarm"`; they edit exactly
   * like template-added chat agents, so the card and the Instructions screen
   * treat them the same except where noted here. All optional because the
   * client also synthesises summaries from a create response (chat rows only).
   */
  /** Registry key, e.g. "growth_research". Null on tenant-created chat agents. */
  agent_key?: string | null;
  /** "chat" (tenant-authored) or "swarm" (registry-managed). */
  kind?: string;
  /** The owner's off switch. Only meaningful for swarm rows. */
  enabled?: boolean;
  /**
   * Activation state from the backend's state machine: planned | disabled |
   * not_applicable | blocked | active. Null when the backend could not compute
   * it (or a chat row) — render nothing rather than guessing.
   */
  state?: string | null;
  /** Why a non-active agent is not active, written for the owner to act on. */
  state_reason?: string;
  /** When this agent first had everything it needs. Null until then. */
  activated_at?: string | null;
}

/** POST /v1/agents/{id}/preview — the prompt template rendered with this
 *  tenant's facts right now: what the owner wrote, what it becomes, and the
 *  facts still missing. */
export interface AgentPromptPreview {
  template: string;
  rendered: string;
  missing: string[];
}

// --- audit log ---

export interface AuditRow {
  id: number;
  ts: string;
  actor_email: string | null;
  action: string;
  target_tenant: string | null;
  target_type: string | null;
  target_id: string | null;
  detail: Record<string, unknown>;
}

// --- integrations ---

export interface WhatsAppIntegration {
  id: string;
  agent_id: string;
  waba_id: string;
  phone_number_id: string;
  display_number: string | null;
  status: string;
  token_configured: boolean;
  created_at: string | null;
}

// POST /v1/integrations/whatsapp/{id}/verify → the WhatsApp connection handshake.
// `subscribed` is the webhook-field subscription the backend performs during
// verify — false means the app is not yet listening on that WABA and inbound
// messages will not arrive until it is (re-run Verify or subscribe in Meta).
export interface WhatsAppVerifyResult {
  ok: boolean;
  display_number: string | null;
  verified_name: string | null;
  quality_rating: string | null;
  subscribed: boolean;
  message: string;
}

// GET /v1/integrations/whatsapp/embedded-signup/state → whether the one-click
// Meta flow can launch here, and the values the browser's Meta JS SDK needs.
export interface WhatsAppEmbeddedSignupState {
  configured: boolean;
  app_id: string;
  config_id: string;
  graph_version: string;
}

// GET /v1/models → the model choices the editor offers: the managed default.
export interface ManagedModel {
  backend: string;
  model: string;
  label: string;
}

export interface ModelCatalog {
  managed: ManagedModel;
}

// --- agents (full detail carries the editable config) ---

export interface AgentDetail extends AgentSummary {
  config: AgentConfig;
  created_at?: string | null;
  updated_at?: string | null;
}

// GET /v1/analytics/performance
export interface TokenBurn {
  model: string | null;
  tokens_in: number;
  tokens_out: number;
  tokens_total: number;
}

export interface Performance {
  period_days: number;
  answers: number;
  latency_p50_ms: number | null;
  latency_p95_ms: number | null;
  fallback_answers: number;
  fallback_rate: number | null;
  token_burn_by_model: TokenBurn[];
}

/* ------------------------------------------------------------------------- *
 * Sources — crawler, sitemap, link lists, snippets, Q&A pairs
 * Transliterated from productisation-next-crawler.md §6 "Models".
 * Python `datetime | None` arrives as an ISO `string | null`; `UUID` as `string`.
 * ------------------------------------------------------------------------- */

// Nested under SourceCreate.config on POST /v1/sources
export interface CrawlConfigIn {
  include_paths: string[];
  exclude_paths: string[];
  max_pages: number | null;
  // null defers to the deployment's default depth (CRAWL_MAX_DEPTH)
  max_depth: number | null;
  include_subdomains: boolean;
  include_pdf: boolean;
  /**
   * Park the crawl once every page is fetched and wait for the URL list to be
   * approved before anything is indexed. Off by default, server side too, so a
   * caller that omits it gets the old straight-through crawl.
   */
  review_before_index: boolean;
}

// Request body for POST /v1/sources
export interface SourceCreate {
  mode: "crawl" | "sitemap" | "links";
  url?: string | null;
  urls?: string[];
  title?: string | null;
  config?: CrawlConfigIn;
  tags?: string[];
  collection?: string | null;
  auto_retrain_hours?: number;
}

// GET /v1/sources (list item), POST /v1/sources (created)
export interface SourceOut {
  id: string;
  kind: string;
  title: string;
  seed_url: string | null;
  origin: string | null;
  crawl_state: string;
  error: string | null;
  link_total: number;
  link_trained: number;
  link_failed: number;
  link_excluded: number;
  link_pending: number;
  size_bytes: number;
  chunk_count: number;
  tags: string[];
  collection: string | null;
  config: Record<string, unknown>;
  auto_retrain_hours: number;
  pending_changes: boolean;
  crawl_started_at: string | null;
  last_trained_at: string | null;
  last_checked_at: string | null;
  /** When the URL list was last approved. Null while a gated crawl is waiting. */
  review_approved_at: string | null;
  created_at: string | null;
}

// One discovered/crawled URL belonging to a source
export interface SourceLinkOut {
  id: string;
  url: string;
  title: string | null;
  state: string;
  reason: string | null;
  reason_detail: string | null;
  http_status: number | null;
  depth: number;
  discovered_via: string;
  content_bytes: number;
  document_id: string | null;
  fetched_at: string | null;
}

// GET /v1/sources/{id}
export interface SourceDetailOut extends SourceOut {
  trained: SourceLinkOut[];
  not_found: SourceLinkOut[];
  excluded: SourceLinkOut[];
}

// GET /v1/sources/{id}/progress — polled while crawl_state is live
export interface CrawlProgressOut {
  crawl_state: string;
  link_total: number;
  link_trained: number;
  link_failed: number;
  link_excluded: number;
  link_pending: number;
  size_bytes: number;
  error: string | null;
  recent: SourceLinkOut[];
  message: string;
}

/* --------------------------------------------------------------- review gate */

// One page in the approval list. Five fields, not SourceLinkOut: the review tree
// groups by URL and shows a title, and nothing else is drawn per row.
export interface ReviewLinkOut {
  id: string;
  url: string;
  title: string | null;
  depth: number;
  state: string;
}

// One node of the URL tree: a host, or a path prefix under a host.
export interface ReviewGroupOut {
  key: string;
  label: string;
  /** Every URL beneath this node, children included. What the header counts. */
  total: number;
  /** Only the pages that sit on this node itself, so /blog can be a page too. */
  urls: string[];
  children: ReviewGroupOut[];
}

// GET /v1/sources/{id}/review
export interface SourceReviewOut {
  crawl_state: string;
  /** False means this source never asked to gate; the tree is then a preview. */
  review_before_index: boolean;
  review_approved_at: string | null;
  total: number;
  excluded: number;
  /** True when the frontier is larger than the server's per-request cap. */
  truncated: boolean;
  groups: ReviewGroupOut[];
  /** Every URL in the tree, once, with the link id that bulk exclude needs. */
  links: ReviewLinkOut[];
}

// Request body for POST /v1/sources/snippets
export interface SnippetIn {
  title: string;
  body: string;
  tags: string[];
  collection: string | null;
}

// Request body for POST/PUT /v1/sources/qa/pairs
export interface QaPairIn {
  title?: string | null;
  questions: string[];
  answer: string;
  /** Explicit position in the set. Null lets the server append. */
  ordinal?: number | null;
}

// GET /v1/sources/qa/pairs (list item)
export interface QaPairOut {
  id: string;
  document_id: string | null;
  title: string;
  questions: string[];
  answer: string;
  status: string;
  error: string | null;
  times_asked: number;
  last_asked: string | null;
}

// POST /v1/sources/qa/import
export interface QaImportResult {
  created: number;
  errors: Array<Record<string, unknown>>;
}

// GET /v1/sources/summary
export interface SourcesSummaryOut {
  by_kind: Record<string, { sources: number; documents: number; bytes: number }>;
  documents: number;
  storage_bytes: number;
  needs_retrain: number;
  crawling: number;
  features: Record<string, boolean>;
}

/** GET /v1/sources/{id}/links */
export interface SourceLinkPage {
  items: SourceLinkOut[];
  total: number;
  counts: Record<string, number>;
}

/** GET /v1/sources/snippets/{id} */
export interface SnippetDetail {
  source: SourceOut;
  body: string;
}

/** GET /v1/sources/qa/pairs/{pair_id}/activity */
export interface QaPairActivity {
  points: Array<{ day: string; asked: number }>;
  times_asked: number;
  last_asked: string | null;
}

// --------------------------------------------------- handoffs to a person

/**
 * Why a conversation went to a person. One union for two uses, because the
 * server keeps one list: these are the values stored in `escalations.trigger`
 * and the names accepted in `config.escalation.triggers`.
 *
 * The plain-language label for each, and these are the only strings the UI
 * shows:
 * `low_confidence` → `Wasn't sure enough`;
 * `ungrounded` → `Answer wasn't backed by your sources`;
 * `user_requested` → `Customer asked for a person`;
 * `negative_feedback` → `Thumbs down`;
 * `crisis` → `Urgent or upsetting message`;
 * `blocklist` → `Blocked phrase`;
 * `model_requested` → `The agent asked for a person`;
 * `email_undeliverable` → `A reply could not be delivered`;
 * `email_thread_loop` → `An email thread went in circles`;
 * `draft` → `Drafted for a human to send`.
 *
 * `draft` is not a tenant opt-in and must not be offered as one: the email
 * channel's draft mode is the switch, and the server records this trigger without
 * consulting `config.escalation.triggers`. It appears in the numbers, never in a
 * checkbox.
 */
export type EscalationTrigger =
  | "low_confidence"
  | "ungrounded"
  | "user_requested"
  | "negative_feedback"
  | "crisis"
  | "blocklist"
  | "model_requested"
  | "email_undeliverable"
  | "email_thread_loop"
  | "draft";

/**
 * `config.escalation` — when the agent gives up, who hears about it.
 *
 * Every field is required here even though every field has a server-side
 * default, because the server's config merge is shallow at the top level: a
 * caller that sends this block sends all of it or silently resets the rest. Read
 * the stored block, change what the form owns, send the whole thing back.
 *
 * The `email_*` fields live in this block rather than one of their own because
 * they are the same decision as a handoff — answer, or hand over — on a channel
 * where the alternative to answering is silence. They are edited on Channels,
 * beside the mailbox they govern; everything else is edited on the handoff panel
 * under Instructions.
 */
export interface EscalationConfig {
  /** Off until a tenant turns it on: a feature that starts emailing people
   * unprompted is a support incident. */
  enabled: boolean;
  /** Which triggers are live. A list, so a new trigger is additive and an
   * unknown value from an older or newer config is dropped, not fatal. */
  triggers: EscalationTrigger[];
  /** At most five. Each must be a workspace member's address or on a verified
   * domain — enforced by the API, so a rejected address comes back as a 400. */
  destination_emails: string[];
  /** A ref into the tenant's stored secrets, never the webhook URL itself: a
   * Slack incoming-webhook URL *is* the credential. Read-only in the dashboard
   * until there is a route that stores one. */
  slack_webhook_secret_ref: string | null;
  /** Suppression window for the same conversation and trigger. 60–86400. */
  cooldown_seconds: number;
  /** What the customer is told. Empty means say nothing extra. */
  user_message: string;
  /** Ask for a way to reach the person first, where we do not already know it. */
  collect_contact: boolean;
  collect_contact_required: boolean;
  /** Transcript turns included in the notification. 0–50; 0 sends the question
   * alone. */
  transcript_turns: number;
  /** Email channel only. `draft` never writes to the customer — it sends the
   * proposed reply to the workspace instead. */
  email_reply_mode: "off" | "draft" | "auto";
  /** 0–1, and higher than chat's floor on purpose: nobody is watching an email
   * arrive and no follow-up turn can correct it. */
  email_auto_reply_min_confidence: number;
  email_require_grounded: boolean;
  email_require_citation: boolean;
  email_signature: string;
}

/**
 * `GET /v1/escalations/stats?days=` exactly as the server sends it: one row per
 * (day, trigger) pair, and nothing else.
 *
 * There is no `agent_id` parameter — the read is tenant-wide, like every other
 * analytics read — so an agent-scoped mount says that on screen rather than
 * sending a filter the server would ignore.
 */
export interface EscalationStatsRow {
  /** ISO date, or null for a row the server could not date. */
  day: string | null;
  trigger: EscalationTrigger;
  total: number;
}

export interface EscalationStatsOut {
  days: number;
  rows: EscalationStatsRow[];
}

/**
 * What the stat band and the `Handoffs` card actually render. Derived from
 * `EscalationStatsOut` by `summariseEscalations` in `lib/api.ts`, because the
 * endpoint reports counts and the screen reports a rate.
 *
 * `escalation_rate` is `handed_to_a_person ÷ answered_questions`, and
 * `answered_questions` comes from `GET /v1/analytics/performance` over the same
 * window. The denominator is questions, not conversations, because one
 * conversation can hand over once while containing ten answers — and both
 * formulas are shown in a `Tooltip`, because a buyer will not trust a rate whose
 * denominator is hidden. The rate is null below 50 answered questions: render
 * null as an em dash with *"Not enough answers yet to be meaningful."* — never as
 * a `Skeleton`. "Not measured yet" and "still loading" are different states and
 * must look different.
 *
 * There is deliberately no `handled_without_a_person` and no `handled_rate`. The
 * endpoint reports handoffs per day and per trigger; a per-conversation "nobody
 * had to step in" figure cannot be derived from that without inventing it, so
 * the fallback-rate proxy keeps that slot under its own name,
 * `Answered without falling back`.
 */
export interface EscalationStats {
  days: number;
  /** Total handoffs in the window. The raw count leads; the rate supports it. */
  handed_to_a_person: number;
  answered_questions: number | null;
  escalation_rate: number | null;
  by_trigger: Array<{ trigger: EscalationTrigger; count: number }>;
  points: Array<{ day: string; handoffs: number }>;
}

/** `POST /v1/escalations/test` — owner or admin. */
export interface EscalationTestResult {
  escalated: boolean;
  escalation_id: string | null;
  /**
   * Why nothing was sent, when nothing was sent: `disabled`, `trigger_off`,
   * `no_destination`, `no_contact`, `rate_limited` or `cooldown`. That answer is
   * the whole point of the test — it names the gate instead of failing silently.
   */
  suppressed_reason: string | null;
}

// ------------------------------------------------------------ email channel

/**
 * One mailbox. Never fetched on its own — it arrives inside
 * `EmailChannelState.channel`, because the card has to render a not-connected
 * state too and that needs the deployment's backend and inbound domain as well
 * as the row.
 *
 * `address` is provisioned by the server as `<slug>-<hex>@<inbound domain>` and
 * is the address the tenant points a forwarding rule at. It is never chosen by
 * the client: the local part is the inbound routing key, so a tenant-chosen one
 * either collides or resolves to somebody else's mail.
 */
export interface EmailChannelOut {
  id: string;
  agent_id: string;
  address: string;
  forward_from: string | null;
  display_name: string | null;
  sending_domain: string | null;
  /** Replies can only be sent once this is true. */
  sending_domain_verified: boolean;
  allowed_forwarders: string[];
  ingest_attachments: boolean;
  link_sources: boolean;
  /**
   * Auto-sent replies still copied to the escalation destinations, so a newly
   * trusted mailbox is read by a human for its first few answers.
   */
  audit_sends_remaining: number;
  status: string;
  error_reason: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

/**
 * `GET /v1/integrations/email` — the card's whole state in one call, connected
 * or not. One mailbox per tenant, so there is no list to filter: `channel` is
 * either the tenant's mailbox or null.
 */
export interface EmailChannelState {
  connected: boolean;
  /** `none` when this deployment has no email provider configured. */
  backend: string;
  inbound_domain: string;
  channel: EmailChannelOut | null;
  /** Server-written onboarding paragraph, printed verbatim. Empty when not connected. */
  forwarding_instructions: string;
}

/**
 * `PUT /v1/integrations/email`. Absent fields are left alone, and the server
 * rejects any field not listed here.
 *
 * There is deliberately no `address`: see `EmailChannelOut.address`. Reply mode
 * and the confidence floor are not here either, because they live on the agent's
 * `escalation` block and are saved through `PATCH /v1/agents/{id}`.
 */
export interface EmailChannelIn {
  /** Only meaningful on the first write, which creates the mailbox. */
  agent_id?: string;
  forward_from?: string | null;
  display_name?: string | null;
  allowed_forwarders?: string[];
  ingest_attachments?: boolean;
  link_sources?: boolean;
  /**
   * Provision a new address and drop the old one. Absence means "leave alone"
   * for every other field, so it cannot also mean "rotate".
   */
  rotate_address?: boolean;
}

/**
 * `POST /v1/integrations/email/verify`. Send-side only: the probe goes to the
 * signed-in user's own address and nowhere else, and `probe_to` is masked.
 * `error` is set rather than raised when the provider refuses — "we tried and
 * the provider said no" is the answer the screen needs.
 */
export interface EmailVerifyResult {
  sent: boolean;
  status: string;
  probe_to: string;
  provider_message_id: string | null;
  error: string | null;
}

// ───────────────────── Growth platform (migration 016) ─────────────────────
// The agentic growth spine: business profile and settings, provider
// connections, and human-in-the-loop approvals.
//
// Every settings block comes back complete — the API fills each field from its
// defaults — so a screen reads `settings.send_caps.daily_cap` without defending
// against undefined. Writes are the mirror image: `GrowthSettingsIn` takes one
// or more *partial* blocks, and a block left out is untouched. That asymmetry is
// deliberate and is the `agentConfig.ts` lesson enforced on the server.

/** A = B2B outbound, B = D2C store, both = everything. Gates nav and agents. */
export type TenantType = "A" | "B" | "both";

/** 0 = automatic (report after), 1 = one approval per batch, 2 = every message. */
export type HitlLevel = 0 | 1 | 2;

export type GrowthFeature = "outbound_batch" | "campaign" | "review_ask";

export type IntegrationStatus =
  | "not_connected"
  | "pending"
  | "verifying"
  | "active"
  | "error"
  | "planned";

export type ApprovalStatus = "pending" | "approved" | "rejected" | "expired" | "edited";

export interface GrowthProfileBlock {
  sells: string;
  audience: string;
  price_range: string;
  icp: Record<string, unknown>;
  /** Bare host, normalised server-side. Saving a new one seeds a crawl of it. */
  website: string;
  competitors: string[];
  keywords: string[];
  tone: string;
  offer_min: number;
  offer_max: number;
  tenant_type: TenantType;
  /**
   * Required on every cold mail by CAN-SPAM. Empty is allowed here and refused
   * at compose time, so onboarding is not blocked by a field nobody has to hand
   * — but outbound will not start until it is filled in.
   */
  postal_address: string;
  /** Who outbound mail comes from. The domain lives on the resend_domain row. */
  sender_name: string;
  sender_local: string;
}

export interface GrowthHitlLevels {
  outbound_batch: HitlLevel;
  campaign: HitlLevel;
  review_ask: HitlLevel;
}

export interface GrowthSendCaps {
  daily_cap: number;
  /** 0 means "not warming up": an established domain, own cap only. */
  warmup_day: number;
  start_hour: number;
  end_hour: number;
  /** `Date.getDay()`-style but Monday-based: 0 = Monday … 6 = Sunday. */
  days: number[];
  timezone: string;
}

export interface GrowthFollowUp {
  days: number[];
  max_follow_ups: number;
}

export interface GrowthOfferRange {
  currency: string;
  discount_min_pct: number;
  discount_max_pct: number;
}

export interface GrowthLanguages {
  /** Always contains "en": it is the resolver's floor, not a preference. */
  allowed: string[];
}

export interface GrowthGrounding {
  mode: "advisory" | "block";
  agents: Record<string, boolean>;
}

export interface GrowthReportPrefs {
  weekly: boolean;
  monthly: boolean;
  on_demand: boolean;
  /** Stage 3: a report every morning instead of Mondays. Off by default. */
  daily: boolean;
}

export interface GrowthChannelOrder {
  order: string[];
}

/** Complete settings. Every block present, every field filled from defaults. */
export interface GrowthSettings {
  profile: GrowthProfileBlock;
  hitl_levels: GrowthHitlLevels;
  send_caps: GrowthSendCaps;
  follow_up: GrowthFollowUp;
  offer_range: GrowthOfferRange;
  languages: GrowthLanguages;
  grounding: GrowthGrounding;
  report_prefs: GrowthReportPrefs;
  channel_order: GrowthChannelOrder;
}

/**
 * A partial edit. Send only the blocks you changed, and inside each only the
 * fields you changed — the server layers them over what is stored and validates
 * the whole result, so no sibling field is ever reset.
 */
export interface GrowthSettingsIn {
  profile?: Partial<GrowthProfileBlock>;
  hitl_levels?: Partial<GrowthHitlLevels>;
  send_caps?: Partial<GrowthSendCaps>;
  follow_up?: Partial<GrowthFollowUp>;
  offer_range?: Partial<GrowthOfferRange>;
  languages?: Partial<GrowthLanguages>;
  grounding?: Partial<GrowthGrounding>;
  report_prefs?: Partial<GrowthReportPrefs>;
  channel_order?: Partial<GrowthChannelOrder>;
}

export interface GrowthIntegration {
  provider: string;
  label: string;
  group: string;
  status: IntegrationStatus;
  /** Why a `planned` provider is not connectable yet. Empty when it is. */
  note: string;
  external_ref: string;
  connected: boolean;
  /**
   * Telegram only, and only while the platform runs its own bot: the card then
   * offers the /start binding instead of the token-and-chat-id form.
   */
  provider_mode?: boolean;
  health: Record<string, unknown>;
  updated_at: string | null;
}

export interface GrowthIntegrationIn {
  external_ref?: string;
  /** Write-only. Encrypted into tenant_secrets; never returned. */
  secret?: string;
  status?: "active" | "not_connected";
}

export interface GrowthApproval {
  id: string;
  batch_id: string;
  feature: GrowthFeature;
  level: HitlLevel;
  status: ApprovalStatus;
  payload: { summary?: string; items?: unknown[] } & Record<string, unknown>;
  expires_at: string | null;
  created_at: string | null;
  decided_by: string | null;
  decided_at: string | null;
}

export interface GrowthDecisionIn {
  decision: "approved" | "rejected" | "edited";
  payload?: Record<string, unknown>;
}

/**
 * The two ways to wire a website form up. Both are returned because which one a
 * tenant can use depends on software we know nothing about: a script tag for a
 * page they control, a webhook URL for a form builder that only posts to a URL.
 */
export interface GrowthFormSetup {
  connected: boolean;
  snippet: string;
  /** The endpoint, with no credential in it. Send `key` in the header or body. */
  webhook_url: string;
  /** The header name to send `key` in — `x-ingress-key`. */
  webhook_header: string;
  /**
   * Fallback for form builders that accept a URL and nothing else: the key is in
   * the query string, and therefore in every access log and browser history on
   * its path. The screen offers it last and says so.
   */
  webhook_url_with_key: string;
  key: string;
}

// ───────────────── Stage 3: the report agent ─────────────────
// One row per (period, period_start). `numbers` is the full computed set, so the
// screen renders the fallback while the PDF is still rendering.

export type GrowthReportPeriod = "weekly" | "monthly" | "daily" | "on_demand";

export interface GrowthReport {
  id: string;
  period: GrowthReportPeriod;
  period_start: string | null;
  numbers: Record<string, unknown>;
  pdf_path: string;
  summary: string;
  status: "queued" | "ready" | "failed";
  created_at: string | null;
}

/** `GET /v1/growth/crm/{provider}/connect` — open `authorize_url` in a new tab. */
export interface CrmConnectResponse {
  authorize_url: string;
}

/**
 * The Telegram setup the Connections screen shows. Per-tenant mode: the
 * webhook URL carrying this tenant's ingress key. Provider mode (the
 * platform's own bot): the /start binding code, the bot's @username (null
 * when getMe failed — the screen then shows plain instructions), and the
 * platform owner's one-time setWebhook URL.
 */
export interface GrowthTelegramSetup {
  connected: boolean;
  webhook_url: string;
  webhook_url_with_key: string;
  key: string;
  provider_mode?: boolean;
  bot_username?: string | null;
  binding_code?: string;
}

// ───────────────── The agent graph (read by /v1/graph) ─────────────────

export interface AgentGraphNode {
  key: string;
  label: string;
  kind: "agent" | "trigger" | "gate";
  x: number;
  y: number;
  /** Registry facts (plan agent.md §3): what the agent is for and whether it
   *  is built. Optional — older payloads and non-agent nodes carry neither. */
  goal?: string;
  /** implemented | planned */
  status?: string;
  /** This tenant's activation state, when the backend could compute it. */
  state?: string | null;
  /** Why the state is not active. */
  reason?: string;
  /** identity | customer | outbound | insight | platform */
  category?: string;
}

export interface AgentGraphEdge {
  from: string;
  to: string;
  label: string;
}

// --- visualise (read-only agent graph for the owner) ---

export interface GraphNode extends AgentGraphNode {
  description: string;
}

export interface GraphView {
  nodes: GraphNode[];
  edges: AgentGraphEdge[];
}
