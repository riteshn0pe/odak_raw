// Typed fetch wrapper. Attaches the bearer token, unwraps the {error:{code}}
// envelope the API returns into a thrown ApiError so callers branch on `code`
// (stable) rather than message text (not), and keeps upload as multipart.

import type {
  EscalationStats,
  EscalationStatsOut,
  EscalationTestResult,
  EscalationTrigger,
  GrowthSettings,
  GrowthSettingsIn,
  GrowthIntegration,
  GrowthIntegrationIn,
  GrowthApproval,
  GrowthDecisionIn,
  GrowthFormSetup,
  GrowthReport,
  GrowthTelegramSetup,
  CrmConnectResponse,
  GraphView,
} from "./types";

import { beginRequest, endRequest } from "./progress";

export class ApiError extends Error {
  constructor(
    readonly code: string,
    message: string,
    readonly status: number,
    readonly details: Record<string, unknown> = {},
  ) {
    super(message);
    this.name = "ApiError";
  }
}

// where the token comes from is the caller's concern; auth.ts sets this. keeping
// it a getter rather than a header argument means every call site stays terse
type TokenSource = () => string | null;
let tokenSource: TokenSource = () => null;

export function setTokenSource(source: TokenSource): void {
  tokenSource = source;
}

interface RequestOptions {
  method?: string;
  body?: unknown;
  // some routes take an explicit tenant slug for a multi-tenant user
  headers?: Record<string, string>;
  signal?: AbortSignal;
  // long-running polls (crawl progress, settle loops) opt out of the global bar
  silent?: boolean;
  // skip the GET cache entirely — for reads whose whole point is freshness.
  // An AbortSignal is NOT this: signals are the standard unmount-cleanup
  // pattern and almost every screen passes one on mount, so treating a signal
  // as "wants fresh data" silently opted most tabs out of the cache.
  fresh?: boolean;
}

// ── GET cache ────────────────────────────────────────────────────────────────
// Revisiting a tab re-pays the full network RTT for data fetched moments ago,
// and several screens fetch the same endpoint on the same mount (AgentLayout
// and its section screens both load `/v1/agents/:id` on entry). With the backend
// on slow cross-region stores this reads as "tab switch hangs". A short-TTL URL
// cache plus in-flight dedup turns a re-enter into a render from memory. Only
// successful GETs are cached; mutations pass through untouched, a caller that
// genuinely needs uncached data passes `fresh`, and cached hits skip the global
// bar, so no spinner flash either.
const GET_CACHE_TTL_MS = 15_000;
const getCache = new Map<string, { at: number; value: unknown }>();
const inflight = new Map<string, Promise<unknown>>();
// bumped on every mutation, so a background stale-refresh that raced a write
// cannot cache the pre-write snapshot over the cleared state
let cacheGeneration = 0;

/** Drop every cached GET. For raw `fetch` writes that bypass this client. */
export function bustGetCache(): void {
  getCache.clear();
  inflight.clear();
  cacheGeneration++;
}

function cacheKeyFor(path: string, options: RequestOptions): string | null {
  if (options.method !== "GET" || options.fresh) return null;
  const headerPart = options.headers
    ? Object.entries(options.headers)
        .filter(([, v]) => v !== undefined)
        .map(([k, v]) => `${k}=${v}`)
        .sort()
        .join("&")
    : "";
  // token in the key so a logout/login into another account never serves the
  // previous account's cached rows
  const token = tokenSource();
  return headerPart || token ? `${path}\n${headerPart}\n${token ?? ""}` : path;
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const silent = options.silent ?? false;
  const cacheKey = cacheKeyFor(path, options);
  // a request carrying a signal owns its own round-trip: sharing it through the
  // dedup map would let one screen's unmount abort a sibling's fetch
  const shareable = cacheKey !== null && !options.signal;

  const run = async (forceSilent = false, dropSignal = false): Promise<T> => {
    const showBar = !silent && !forceSilent;
    if (showBar) beginRequest(options.method !== "GET");
    try {
      return await doFetch<T>(path, dropSignal ? { ...options, signal: undefined } : options);
    } finally {
      if (showBar) endRequest();
    }
  };

  if (cacheKey) {
    const hit = getCache.get(cacheKey);
    if (hit && Date.now() - hit.at < GET_CACHE_TTL_MS) {
      return hit.value as T;
    }
    if (hit) {
      // stale-while-revalidate: the revisit renders the snapshot it already
      // has instead of hanging on the slow backend, and a silent refresh
      // updates the cache for the next visit. The refresh drops the caller's
      // signal — the caller may be gone by the time it lands — and the
      // generation guard drops it if a mutation landed mid-flight.
      if (!inflight.has(cacheKey) && !options.signal?.aborted) {
        const generation = cacheGeneration;
        const refresh = run(true, true)
          .then((value) => {
            if (generation === cacheGeneration) {
              getCache.set(cacheKey, { at: Date.now(), value });
            }
            return value;
          })
          .catch(() => undefined)
          .finally(() => {
            if (shareable) inflight.delete(cacheKey);
          });
        if (shareable) inflight.set(cacheKey, refresh);
      }
      return hit.value as T;
    }
    // a sibling mount already asked for the same URL — share its promise
    // instead of firing a duplicate round-trip to a slow backend
    if (shareable) {
      const pending = inflight.get(cacheKey);
      if (pending) return pending as Promise<T>;
    }
  }

  if (!cacheKey) {
    const value = await run();
    // A successful mutation (POST/PUT/PATCH/DELETE) can make cached GETs stale
    // — a save that is followed by a refetch must not serve the pre-save
    // snapshot for up to the TTL, which reads as "my save did not apply". Coarse
    // clear is correct: we cannot know which endpoints a mutation touched, and
    // mutations are rare against reads. In-flight GETs are cleared too so a
    // deduped read that raced the mutation re-fires fresh.
    if (options.method && options.method !== "GET") {
      bustGetCache();
    }
    return value;
  }

  // captured before the fetch: if a mutation lands while this GET is in
  // flight, its response is pre-write data and must not be cached
  const generation = cacheGeneration;
  const promise = run();
  if (shareable) inflight.set(cacheKey, promise);
  try {
    const value = await promise;
    if (generation === cacheGeneration) {
      getCache.set(cacheKey, { at: Date.now(), value });
    }
    return value;
  } finally {
    if (shareable) inflight.delete(cacheKey);
  }
}

async function doFetch<T>(path: string, options: RequestOptions): Promise<T> {
  const headers: Record<string, string> = { ...options.headers };
  const token = tokenSource();
  if (token) headers["Authorization"] = `Bearer ${token}`;

  let body: BodyInit | undefined;
  if (options.body instanceof FormData) {
    body = options.body; // let the browser set the multipart boundary
  } else if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(options.body);
  }

  const response = await fetch(path, {
    method: options.method ?? (body ? "POST" : "GET"),
    headers,
    body,
    signal: options.signal,
  });

  if (response.status === 204) return undefined as T;

  const text = await response.text();
  const payload = text ? JSON.parse(text) : null;

  if (!response.ok) {
    const err = payload?.error ?? {};
    throw new ApiError(
      err.code ?? "unknown",
      err.message ?? response.statusText,
      response.status,
      err.details ?? {},
    );
  }
  return payload as T;
}

export const api = {
  get: <T>(path: string, headers?: Record<string, string>, signal?: AbortSignal, opts?: { silent?: boolean; fresh?: boolean }) =>
    request<T>(path, { method: "GET", headers, signal, ...opts }),
  post: <T>(path: string, body?: unknown, headers?: Record<string, string>, signal?: AbortSignal, opts?: { silent?: boolean }) =>
    request<T>(path, { method: "POST", body, headers, signal, ...opts }),
  put: <T>(path: string, body?: unknown, headers?: Record<string, string>, opts?: { silent?: boolean }) =>
    request<T>(path, { method: "PUT", body, headers, ...opts }),
  patch: <T>(path: string, body?: unknown, opts?: { silent?: boolean }) =>
    request<T>(path, { method: "PATCH", body, ...opts }),
  del: <T>(path: string, opts?: { silent?: boolean }) =>
    request<T>(path, { method: "DELETE", ...opts }),
  upload: <T>(path: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<T>(path, { method: "POST", body: form });
  },
};

type ReqHeaders = Record<string, string>;

/** Query string from the params that actually have a value. Returns "" when none do. */
function qs(params: Record<string, string | number | boolean | null | undefined>): string {
  const parts = Object.entries(params)
    .filter(([, v]) => v !== undefined && v !== null && v !== "")
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`);
  return parts.length ? `?${parts.join("&")}` : "";
}

export const escalationsApi = {
  /**
   * The handoff numbers in the `Handoffs` card and the stat band on the handoff
   * panel. `days` defaults to 30 server-side and is capped at 180.
   *
   * The response is per-day, per-trigger counts and nothing else, so pass it
   * through `summariseEscalations` to get what a screen renders. The read is
   * tenant-wide — there is no agent filter to send, and an agent-scoped mount
   * says so on screen rather than pretending otherwise.
   */
  stats: (
    params: { days?: number } = {},
    headers?: ReqHeaders,
    signal?: AbortSignal,
  ) =>
    request<EscalationStatsOut>(`/v1/escalations/stats${qs({ days: params.days })}`, {
      method: "GET",
      headers,
      signal,
    }),

  /**
   * Fire one real handoff through the workspace's own settings. Owner or admin.
   *
   * `suppressed_reason` comes back when a gate stopped it — disabled, the trigger
   * switched off, no destination, no contact, rate limited, or still inside the
   * cooldown. Naming the gate is the point of the test, so surface that string
   * rather than reporting a generic failure.
   */
  test: (headers?: ReqHeaders) =>
    request<EscalationTestResult>("/v1/escalations/test", {
      method: "POST",
      body: {},
      headers,
    }),
};

/**
 * Below this many answered questions a rate is noise, so it is not shown at all.
 * Shared by every surface that renders one, so the two never disagree.
 */
export const MIN_ANSWERS_FOR_RATE = 50;/**
 * Turn `GET /v1/escalations/stats` into the figures a screen renders.
 *
 * `answeredQuestions` is the denominator, from `GET /v1/analytics/performance`
 * over the same window; pass null when that read failed and the rate is simply
 * not available. Below `MIN_ANSWERS_FOR_RATE` the rate stays null rather than
 * being shown as a confident percentage of a handful of answers.
 *
 * Returns null for a null response, so a screen can keep "the endpoint did not
 * answer" and "the endpoint answered with nothing" as the different states they
 * are.
 */
export function summariseEscalations(
  raw: EscalationStatsOut | null,
  answeredQuestions: number | null,
): EscalationStats | null {
  if (!raw) return null;
  const byTrigger = new Map<EscalationTrigger, number>();
  const byDay = new Map<string, number>();
  let total = 0;
  for (const row of raw.rows ?? []) {
    const count = Number(row.total) || 0;
    total += count;
    byTrigger.set(row.trigger, (byTrigger.get(row.trigger) ?? 0) + count);
    if (row.day) byDay.set(row.day, (byDay.get(row.day) ?? 0) + count);
  }
  const enough = answeredQuestions != null && answeredQuestions >= MIN_ANSWERS_FOR_RATE;
  return {
    days: raw.days,
    handed_to_a_person: total,
    answered_questions: answeredQuestions,
    escalation_rate: enough ? total / answeredQuestions : null,
    // Biggest cause first: the chart is read top-down and the tenant wants the
    // one worth fixing at the top.
    by_trigger: Array.from(byTrigger.entries())
      .map(([trigger, count]) => ({ trigger, count }))
      .sort((a, b) => b.count - a.count),
    points: Array.from(byDay.entries())
      .sort((a, b) => a[0].localeCompare(b[0]))
      .map(([day, handoffs]) => ({ day, handoffs })),
  };
}


// ───────────────────── Growth platform (migration 016) ─────────────────────
// Reads carry an AbortSignal for unmount cleanup, which no longer opts them
// out of the 15s GET cache. Approvals ride stale-while-revalidate like every
// other screen: a row decided elsewhere surfaces as the 422 on `decide`, which
// the screen already renders, rather than as a slow tab.

export const growthApi = {
  /** Complete settings, every block filled from defaults. Never 404s. */
  settings: (headers?: ReqHeaders, signal?: AbortSignal) =>
    request<GrowthSettings>("/v1/growth/profile", { method: "GET", headers, signal }),

  /**
   * Partial edit. Send only the blocks you changed and only the fields inside
   * them; anything omitted is left exactly as stored. Returns the complete
   * settings, so a screen re-seeds its draft from the response rather than
   * guessing what the server made of the write.
   */
  saveSettings: (body: GrowthSettingsIn, headers?: ReqHeaders) =>
    request<GrowthSettings>("/v1/growth/profile", { method: "PUT", body, headers }),

  /**
   * Every provider the platform knows about, not only the connected ones:
   * `planned` entries carry a `note` saying when they land, and render as a
   * disabled card rather than being silently absent.
   */
  integrations: (headers?: ReqHeaders, signal?: AbortSignal) =>
    request<GrowthIntegration[]>("/v1/growth/integrations", { method: "GET", headers, signal }),

  /** 422 for a provider that is display-only, with the reason in the message. */
  connect: (provider: string, body: GrowthIntegrationIn, headers?: ReqHeaders) =>
    request<GrowthIntegration>(`/v1/growth/integrations/${provider}`, {
      method: "PUT",
      body,
      headers,
    }),

  /**
   * Re-check the sending domain's DNS records at Resend. The only path from
   * `pending` to `active` — the response carries the records to add while it
   * is still pending, so the card can show them.
   */
  verifyDomain: (headers?: ReqHeaders) =>
    request<GrowthIntegration>("/v1/growth/integrations/resend_domain/verify", {
      method: "POST",
      body: {},
      headers,
    }),

  approvals: (headers?: ReqHeaders, signal?: AbortSignal) =>
    request<GrowthApproval[]>("/v1/growth/approvals", { method: "GET", headers, signal }),

  /** 422 when the row is no longer pending — someone decided it in another tab. */
  decide: (id: string, body: GrowthDecisionIn, headers?: ReqHeaders) =>
    request<GrowthApproval>(`/v1/growth/approvals/${id}/decide`, {
      method: "POST",
      body,
      headers,
    }),

  /**
   * The website-form script tag and webhook URL. `connected: false` until the
   * tenant connects the `website_form` integration — the key is derived from
   * the tenant, so there is nothing stored to go stale, and disconnecting is
   * what revokes it.
   */
  formSetup: (headers?: ReqHeaders, signal?: AbortSignal) =>
    request<GrowthFormSetup>("/v1/growth/form-setup", { method: "GET", headers, signal }),

  /**
   * Newest reports first, numbers inline. The screen renders the numbers when
   * the PDF is still rendering — a queued row is a promise, not an absence.
   */
  reports: (
    params: { limit?: number; offset?: number },
    headers?: ReqHeaders,
    signal?: AbortSignal,
  ) =>
    request<GrowthReport[]>(`/v1/growth/reports${qs(params)}`, {
      method: "GET",
      headers,
      signal,
    }),

  /** One report with its full numbers set. */
  report: (id: string, headers?: ReqHeaders, signal?: AbortSignal) =>
    request<GrowthReport>(`/v1/growth/reports/${id}`, {
      method: "GET",
      headers,
      signal,
    }),

  /**
   * Short-lived signed link to the report PDF. Minted per click rather than
   * stored on the row, so a link copied out of the page stops working.
   */
  reportDownload: (id: string, headers?: ReqHeaders) =>
    request<{ url: string; filename: string }>(
      `/v1/growth/reports/${id}/download`,
      { method: "GET", headers },
    ),

  /**
   * Queue an on-demand report. Same-day repeat returns the finished row
   * instead of rebuilding it — the server dedupes on (period, period_start).
   */
  runReport: (headers?: ReqHeaders) =>
    request<{ status: string; report_id?: string }>("/v1/growth/reports/run", {
      method: "POST",
      body: { period: "on_demand" },
      headers,
    }),

  /**
   * The Telegram bot webhook URL carrying this tenant's ingress key — or, in
   * provider mode, the /start binding code. `refresh` retires the code and
   * issues a new one; it is the tenant's "new code" action, not a cache bypass.
   */
  telegramSetup: (headers?: ReqHeaders, signal?: AbortSignal, refresh = false) =>
    request<GrowthTelegramSetup>(`/v1/growth/telegram-setup${refresh ? "?refresh=1" : ""}`, {
      method: "GET",
      headers,
      signal,
    }),

  // ── CRM connect (OAuth start; the sync itself runs server-side) ──

  /**
   * Start CRM OAuth. Returns the authorize URL — open it in a new tab.
   * `region` is Zoho's data centre and cannot be guessed: their token
   * and API hosts differ per DC, so the wrong one fails at the token exchange.
   */
  crmConnect: (provider: string, region?: string, headers?: ReqHeaders) =>
    request<CrmConnectResponse>(
      `/v1/growth/crm/${provider}/connect${qs({ region })}`,
      { method: "GET", headers },
    ),

  // ── Store connect (Stage 4) ───────────────────────────────────────────────

  /**
   * Start the Shopify public-app install. Returns the authorize URL — open it
   * in a new tab. `shop` is the *.myshopify.com domain; the state parameter is
   * minted server-side and only the callback may redeem it.
   */
  shopifyConnect: (shop: string, headers?: ReqHeaders) =>
    request<{ authorize_url: string; shop: string }>(
      `/v1/growth/stores/shopify/connect${qs({ shop })}`,
      { method: "GET", headers },
    ),

  // ── Inbound lead sources ──────────────────────────────────────────────────

  /**
   * Queue a history pull for one inbound source. Only the IndiaMART feed has
   * history to pull — any other source is a 400, with the reason in the
   * message. Returns as soon as the job is enqueued; the enquiries land on
   * the board as leads later, not in this response.
   */
  backfillInbound: (body: { source: string; days: number }, headers?: ReqHeaders) =>
    request<{ status: string; source: string; days: number }>(
      "/v1/growth/inbound/backfill",
      { method: "POST", body, headers },
    ),
};

// The owner-facing graph: same layout the staff console draws, read-only, no
// model routing. One fetch, no per-node calls.
export const graphApi = {
  graph: (headers?: ReqHeaders, signal?: AbortSignal) =>
    request<GraphView>("/v1/graph", { method: "GET", headers, signal }),
};
