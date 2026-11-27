"""Typed settings. Only place environment variables are read."""

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "staging", "production"]

INSECURE_DEFAULT = "dev-only-insecure-change-me"


class Settings(BaseSettings):
    # two files, later wins: `.env` is the checkout's own config, and
    # `secrets/.env` is the mount a container platform drops in — Cloud Run
    # mounts a whole Secret Manager payload at that path rather than passing
    # forty separate --set-env-vars. Absent locally, where it is simply skipped,
    # and real process env still outranks both.
    model_config = SettingsConfigDict(
        env_file=(".env", "secrets/.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    ENVIRONMENT: Environment = "development"
    LOG_LEVEL: str = "INFO"
    LOG_JSON: bool = True

    # comma separated dashboard origins allowed to call the API. the one
    # cross-origin API route is /v1/growth/form, allowed by path in middleware
    CORS_ORIGINS: str = "http://localhost:5173"

    # postgres. asyncpg driver, app connects as a non-owner role so RLS applies
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/context_agent"
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 5
    DB_ECHO: bool = False
    # pre-ping is off by default: a ping per checkout costs ~0.4s against the
    # Supabase pooler, and db.py retries a dead checkout instead
    DB_POOL_PRE_PING: bool = False
    # 0 disables recycling — SQLAlchemy's own default. a value in seconds drops
    # connections older than it
    DB_POOL_RECYCLE_SECONDS: int = 0

    # SECRET_KEY signs session tokens, ENCRYPTION_KEY wraps tenant provider keys
    SECRET_KEY: str = INSECURE_DEFAULT
    ENCRYPTION_KEY: str = INSECURE_DEFAULT
    SESSION_TTL_SECONDS: int = 900

    # Optional shared secret for the /internal routers (jobs, cron). QStash
    # calls are already verified by signature; this covers the other backends,
    # where an internal port left open on a misconfigured ingress is a
    # job-triggering surface. Empty (default) changes nothing.
    INTERNAL_TOKEN: str = ""

    # adapter selection. each value picks a concrete implementation of a domain port
    VECTOR_BACKEND: Literal["qdrant", "memory"] = "memory"
    QUEUE_BACKEND: Literal["qstash", "arq", "inline"] = "inline"
    CACHE_BACKEND: Literal["redis", "memory"] = "memory"
    STORAGE_BACKEND: Literal["supabase", "local"] = "local"
    OCR_BACKEND: Literal["paddleocr_hosted", "paddleocr_selfhost", "deepseek", "gemini"] = (
        "paddleocr_hosted"
    )
    EMBEDDING_BACKEND: Literal["jina", "openai", "fake"] = "fake"
    RERANK_BACKEND: Literal["none", "cohere", "jina"] = "none"
    LLM_BACKEND: Literal["gemini", "openai", "anthropic", "openrouter", "nvidia"] = "gemini"
    # web search supplements retrieval when an agent enables the tool. "none"
    # (default) leaves the knob inert; a configured backend lights it up
    WEB_SEARCH_BACKEND: Literal["none", "tavily"] = "none"

    # ordered fallback chain, cheapest first. providers with no credentials are
    # skipped at build time, so an empty result disables ocr rather than failing
    OCR_BACKENDS: str = "paddleocr_hosted,gemini"
    # model used by whichever vision backend is in the chain
    OCR_VISION_MODEL: str = "gemini-3.8-flash"
    # default generation model for tenants on the managed tier
    DEFAULT_LLM_MODEL: str = "gemini-3.8-flash"
    # cheaper tier for utility calls (titles, summaries, intent). Empty means
    # "no separate tier" — Container.utility_llm then returns the main provider.
    UTILITY_LLM_MODEL: str = ""

    QDRANT_URL: str = ""
    QDRANT_API_KEY: str = ""
    QDRANT_COLLECTION_PREFIX: str = "chunks"

    REDIS_URL: str = ""
    # bearer token for a REST-style Redis. left empty for a plain endpoint
    REDIS_TOKEN: str = ""

    SUPABASE_URL: str = ""
    SUPABASE_KEY: str = ""
    SUPABASE_BUCKET: str = "documents"
    LOCAL_STORAGE_DIR: str = "./data/storage"

    QSTASH_URL: str = "https://qstash.upstash.io/v2"
    QSTASH_TOKEN: str = ""
    QSTASH_CURRENT_SIGNING_KEY: str = ""
    QSTASH_NEXT_SIGNING_KEY: str = ""
    PUBLIC_BASE_URL: str = "http://localhost:8000"

    # where the dashboard SPA is hosted (its own static origin). Used only to
    # build the "Sign in" link on the marketing page; empty = same-origin /login.
    DASHBOARD_URL: str = ""
    PRODUCT_NAME: str = "kado"

    # Google OAuth sign-in. Empty client id leaves the button and callback inert,
    # so email/password remains the only path until these are configured.
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_REDIRECT_URI: str = ""

    # Meta Embedded Signup (WhatsApp Tech Provider). The app id identifies this
    # application in the code-for-token exchange; the app secret authenticates it.
    # Empty app id falls back to manual token entry.
    #
    # `config_id` is the Meta Embedded Signup configuration the browser's FB.login
    # dialog launches with. The dashboard loads Meta's JS SDK and reads this value
    # back from GET /v1/integrations/whatsapp/embedded-signup/state; the API only
    # ever exchanges the code for a token. Empty, or a local "dummy_*" placeholder,
    # falls back to manual token entry.
    WHATSAPP_APP_ID: str = ""

    # Meta Embedded Signup configuration id, created in App Dashboard → WhatsApp →
    # Embedded Signup. Without a real one the one-click flow cannot launch.
    WHATSAPP_CONFIG_ID: str = ""

    # Meta WhatsApp Cloud API. Tenant access tokens remain encrypted in Postgres;
    # these platform values authenticate the single shared webhook/application.
    WHATSAPP_APP_SECRET: str = ""
    WHATSAPP_VERIFY_TOKEN: str = ""
    WHATSAPP_GRAPH_VERSION: str = "v23.0"
    WHATSAPP_MAX_MESSAGE_LENGTH: int = 3500
    WHATSAPP_LOCK_TTL_SECONDS: int = 120
    WHATSAPP_LOCK_WAIT_SECONDS: float = 10.0
    # a service window boundary is measured in hours, so seconds of staleness
    # cannot flip the decision
    WHATSAPP_WINDOW_CACHE_TTL_SECONDS: int = 30

    # platform provider keys for the managed tier
    GEMINI_API_KEY: str = ""
    OPENAI_API_KEY: str = ""
    ANTHROPIC_API_KEY: str = ""
    OPENROUTER_API_KEY: str = ""
    # NVIDIA NIM. The key is an nvapi- token from build.nvidia.com. BASE_URL is
    # only set to point at a self-hosted NIM container (its own .../v1); empty
    # uses NVIDIA's hosted catalogue
    NVIDIA_API_KEY: str = ""
    NVIDIA_BASE_URL: str = ""
    JINA_API_KEY: str = ""
    COHERE_API_KEY: str = ""
    PADDLEOCR_API_KEY: str = ""
    PADDLEOCR_BASE_URL: str = ""
    # web search provider key (Tavily). empty leaves WEB_SEARCH_BACKEND inert
    TAVILY_API_KEY: str = ""

    LANGFUSE_PUBLIC_KEY: str = ""
    LANGFUSE_SECRET_KEY: str = ""
    LANGFUSE_HOST: str = "https://cloud.langfuse.com"

    # ingestion limits
    MAX_UPLOAD_BYTES: int = 25 * 1024 * 1024
    UPLOAD_READ_CHUNK_BYTES: int = 1024 * 1024
    MAX_DOCUMENT_PAGES: int = 2000
    EMBED_BATCH_SIZE: int = 32

    HTTP_TIMEOUT_SECONDS: float = 30.0
    # exact-match answer cache. 0 disables it, which is the default: a cached
    # answer is only safe when retrieval for that query cannot have moved.
    ANSWER_CACHE_TTL_SECONDS: int = 0
    CIRCUIT_FAIL_MAX: int = 3
    CIRCUIT_RESET_SECONDS: int = 30

    # server-side URL ingestion (SSRF controls, see app/core/urlguard.py).
    # ALLOW_PRIVATE is a development convenience for localhost fixtures and is
    # refused in production by validate_runtime()
    URL_FETCH_ALLOW_PRIVATE: bool = False
    URL_FETCH_ALLOWED_PORTS: str = "80,443"
    URL_FETCH_MAX_REDIRECTS: int = 3
    # how often a URL-sourced document is re-fetched by the recrawl cron task.
    # 0 disables recrawl unless a document asks for it explicitly
    URL_RECRAWL_DEFAULT_HOURS: int = 0
    URL_RECRAWL_BATCH: int = 20

    # website crawl. operational knobs only: the bounds the pure domain logic in
    # app/domain/crawl.py decides with live in app/core/constants.py, and the
    # defaults below mirror those constants where a number appears in both.
    #
    # every fetch still goes through the urlguard checks above — a crawl widens
    # who supplies the URL (the site, not the tenant), not what we will fetch.
    CRAWL_ENABLED: bool = True
    # bounded by asyncio.Semaphore, same pattern as OCR_MAX_CONCURRENCY. the
    # effective rate is still gated by the per-host throttle below
    CRAWL_WAVE_CONCURRENCY: int = 16
    # one wave's wall clock. longer waves do more work per queue round-trip and
    # re-enqueue a crawl fewer times; a wave killed past the budget is
    # recoverable because every claim is a row reclaimed by TTL, not in-flight
    # state. on the qstash free tier the callback timeout is 30s: a wave that
    # answers after that costs a retry message (out of the daily 500) plus the
    # retry-delay gap, so the default stays under it with room for the finishing
    # writes
    CRAWL_WAVE_BUDGET_SECONDS: int = 20
    # whole-crawl wall clock. at the ceiling the remaining frontier is marked
    # excluded/time_budget_exceeded and the source finalises with what it has.
    # a full-site crawl of a few thousand pages cannot finish in half an hour
    # on any queue, so the default is one day; 0 disables the ceiling entirely
    CRAWL_TOTAL_BUDGET_SECONDS: int = 86400
    # per-host politeness in milliseconds: fetch starts on one host are spaced at
    # least this far apart, paced in-process and guarded by a CachePort lease
    # keyed on the host so two sources sharing a host cannot double the rate. a
    # robots Crawl-delay is clamped into [MIN, MAX]
    CRAWL_DEFAULT_DELAY_MS: int = 100
    CRAWL_MIN_DELAY_MS: int = 50
    CRAWL_MAX_DELAY_MS: int = 30000
    # enforced during the read, not after: a 5MB HTML page is a generated report
    CRAWL_MAX_PAGE_BYTES: int = 5 * 1024 * 1024
    CRAWL_FETCH_TIMEOUT_SECONDS: float = 20.0
    # an identifiable, contactable UA is what gets a crawler unblocked by a WAF.
    # empty derives ContextAgentBot/1.0 (+{PUBLIC_BASE_URL}/bot)
    CRAWL_USER_AGENT: str = ""
    # false ignores robots.txt entirely and is for local fixtures only. a missing,
    # 4xx or 5xx robots.txt already means "no restrictions" per RFC 9309
    CRAWL_RESPECT_ROBOTS: bool = True
    # nested <sitemapindex> follow depth and total URL cap across all sitemaps
    CRAWL_SITEMAP_MAX_DEPTH: int = 3
    CRAWL_SITEMAP_MAX_URLS: int = 50000
    # PDFs are opt-in per source because OCR is the one genuinely expensive step;
    # a crawl that finds 300 scanned brochures is a budget event
    CRAWL_MAX_PDFS_PER_SOURCE: int = 20
    # Hamming distance at or below which two pages are the same page. the first
    # thing to tune against a real corpus, which is why it is a setting
    CRAWL_SIMHASH_MAX_DISTANCE: int = 3
    # below this many characters of extracted markdown a page is empty after nav
    # and footer removal. mirrors MIN_TEXT_CHARS_PER_PAGE
    CRAWL_MIN_TEXT_CHARS: int = 120
    # default per-source depth ceiling; a per-source override is capped by the
    # hard ceiling in the crawl domain
    CRAWL_MAX_DEPTH: int = 5
    # a claimed link whose wave died is reclaimed after this, instead of being
    # stuck in 'fetching' forever
    CRAWL_LINK_CLAIM_TTL_SECONDS: int = 300
    # the stall sweeper: a source in discovering/fetching/extracting that has not
    # been touched for this long lost its wave and is re-enqueued
    CRAWL_STALL_TTL_SECONDS: int = 600
    # optional hosted JS rendering. "none" leaves the port unbuilt and the
    # crawler degrades to sitemap + warning
    CRAWL_RENDER_BACKEND: Literal["none", "http"] = "none"
    CRAWL_RENDER_URL: str = ""
    CRAWL_RENDER_API_KEY: str = ""
    CRAWL_MAX_RENDERS_PER_SOURCE: int = 50
    # the crawl4ai browser. one Chromium per process, started on the first page
    # read or site walk; a sitemap-only source never pays for it
    # browser tabs the crawler may hold open at once — a tab is far more
    # expensive than an httpx connection, so this stays far below wave concurrency
    CRAWL_BROWSER_CONCURRENCY: int = 8
    # comma separated flags handed to Chromium as extra_args. empty keeps
    # crawl4ai's own defaults, which is what a developer workstation wants. a
    # container needs --no-sandbox (Chromium refuses to start as root) and
    # --disable-dev-shm-usage (the 64MB /dev/shm a container gets by default is
    # smaller than one page render)
    CRAWL_BROWSER_ARGS: str = ""
    # extraction is mostly storage/DB round trips (read raw, judge, write link,
    # write document), not CPU, so a slice is processed with this many pages in
    # flight. the HTML parse inside each page still runs off the event loop
    CRAWL_EXTRACT_CONCURRENCY: int = 8
    # how long one wave may spend walking a site that has no sitemap, where the
    # URL space exists only inside the pages and every URL costs a page read
    CRAWL_DISCOVERY_SECONDS: int = 120
    # manual retrain is cheap to ask for and expensive to serve, so it is rate
    # limited per source
    SOURCE_RETRAIN_PER_HOUR: int = 6

    # crawled pages are ingested in batch jobs (ingest_documents), one queue
    # message per INGEST_BATCH_DOCS pages instead of one per page: the qstash
    # free tier counts messages (500/day) and 1800 pages at one message each
    # would blow the cap before lunch. the batch handler keeps each message
    # under the 30s callback timeout by re-enqueueing whatever did not fit
    INGEST_BATCH_DOCS: int = 8
    # documents one batch job may ingest concurrently. bounded because every
    # document ends in an embedding call and the free embedding tier rate limits
    INGEST_DOC_CONCURRENCY: int = 4

    # the actions engine (009_actions.sql)
    ACTIONS_ENABLED: bool = False
    # tool-call loop ceiling per turn. the loop is what makes a runaway turn
    # possible, so this is the first number to reach for when a turn burns 5x
    ACTIONS_MAX_ITERATIONS: int = 5
    # per HTTP action
    ACTIONS_TIMEOUT_SECONDS: float = 10.0
    # every action in one turn. bounds the whole turn, not each call
    ACTIONS_TOTAL_BUDGET_SECONDS: float = 25.0
    # a larger response body is truncated rather than fed whole to the model. 64 KiB
    ACTIONS_MAX_RESPONSE_BYTES: int = 65536
    # comma separated. validate_runtime() refuses 80 in production: a plaintext
    # callout carrying a tenant's API key is a credential leak, not a convenience
    ACTIONS_ALLOWED_PORTS: str = "443"
    ACTIONS_USER_AGENT: str = "ContextAgent-Actions/1.0"
    # action_suspensions expiry. a client action waits on the browser, a write
    # action waits on the visitor's confirmation — different patience, two knobs
    ACTIONS_CLIENT_SUSPENSION_TTL_SECONDS: int = 300
    ACTIONS_CONFIRMATION_TTL_SECONDS: int = 900
    # the action_calls ledger is pruned past this age by the checkpoint_expiry cron
    ACTION_CALLS_RETENTION_DAYS: int = 90

    # multi-step SOPs (010_procedures_contacts.sql)
    PROCEDURES_ENABLED: bool = False
    # instruction steps across the whole tree, mirrored by the
    # procedures_steps_bounded CHECK in 010 and by the Pydantic validator
    PROCEDURE_MAX_STEPS: int = 15
    # per decision point
    PROCEDURE_MAX_BRANCHES: int = 5
    # asserted == 1 by validate_runtime(): the branch editor cannot render deeper
    PROCEDURE_MAX_BRANCH_DEPTH: int = 1
    # chars, mirrored by the procedures_trigger_length CHECK in 010
    PROCEDURE_TRIGGER_MAX_LENGTH: int = 400
    # trigger lines injected into the prompt per turn. this is the per-turn cost of
    # having procedures at all, so it is capped independently of how many exist
    PROCEDURE_TRIGGER_MAX_INJECTED: int = 12
    # "none" needs nothing extra; "embedding" reuses the query vector from
    # retrieval to shortlist triggers and fills procedures.trigger_embedding
    PROCEDURE_SHORTLIST_MODE: Literal["none", "embedding"] = "none"
    # per-run budgets. exhausting one ends the run as failed with the matching
    # ended_reason rather than looping: turn_budget_exhausted, action_budget_exhausted
    PROCEDURE_MAX_TURNS: int = 25
    PROCEDURE_MAX_ACTION_CALLS: int = 12
    # advance_procedure calls per turn. bookkeeping must not become the work
    PROCEDURE_MAX_BOOKKEEPING_CALLS: int = 4
    # same procedure, same conversation
    PROCEDURE_MAX_RESTARTS: int = 3
    # 48 h. an untouched run past this is swept to abandoned/ttl_expired, which is
    # what procedure_runs.expires_at is for
    PROCEDURE_RUN_TTL_SECONDS: int = 172800
    PROCEDURE_RUNS_RETENTION_DAYS: int = 400

    # verified identity: a tenant's backend signs a short-lived HS256 JWT and we
    # verify it once at session creation. the signing secret lives per tenant in
    # tenant_secrets, referenced by identity_verifiers.secret_ref — never here.
    # off by default
    IDENTITY_VERIFICATION_ENABLED: bool = False
    # ceiling on exp - iat. asserted <= 600, and mirrored by the
    # identity_verifiers_ttl_bounded CHECK in 010
    IDENTITY_MAX_TTL_SECONDS: int = 300
    # tolerance for a tenant's clock. asserted <= 300
    IDENTITY_CLOCK_SKEW_SECONDS: int = 60
    # keys accepted in the token's attrs claim
    IDENTITY_MAX_ATTRIBUTES: int = 20
    # how long a jti stays in identity_jti_seen. the cache is the fast replay
    # check; that table is the cross-machine backstop, swept past this horizon
    IDENTITY_JTI_LEDGER_RETENTION_SECONDS: int = 900

    # contacts.attributes is flat and string-valued because it is interpolated into
    # action URL paths; a nested object or a novel-length value produces a
    # malformed request
    CONTACT_MAX_ATTRIBUTES: int = 25
    CONTACT_ATTRIBUTE_MAX_LENGTH: int = 200

    # human escalation (007_escalation.sql). the per-agent switch lives in
    # AgentConfig.escalation; these are the platform-wide bounds. SLACK_ENABLED
    # false leaves the notifier unbuilt, exactly as WEB_SEARCH_BACKEND=none leaves
    # web search inert — the webhook URL itself is a per-tenant secret, never here
    SLACK_ENABLED: bool = False
    # a cap that silences a real incident is worse than the flood, so the cap
    # suppresses individual notifications and still sends one hourly summary.
    # a plan may raise it through features->>'max_escalations_hour'
    ESCALATION_DEFAULT_HOURLY_CAP: int = 20
    ESCALATION_RETENTION_DAYS: int = 400
    # per notifier, not per escalation: one slow Slack webhook must not hold the
    # email notification behind it
    ESCALATION_NOTIFY_TIMEOUT_SECONDS: float = 10.0

    # inbound email channel (008_email.sql). "none" leaves the provider unbuilt and
    # EmailService returns skipped rather than raising, the same posture as a None
    # reranker. every send and every body fetch goes through the one adapter
    EMAIL_BACKEND: Literal["none", "resend"] = "none"
    RESEND_API_KEY: str = ""
    # empty means verify_resend_signature() rejects everything, so every inbound
    # email is dropped as unsigned and no customer is ever answered. asserted
    # non-empty when EMAIL_BACKEND == "resend"
    RESEND_WEBHOOK_SECRET: str = ""
    # both the address suffix and the right-hand side of our Message-ID. empty
    # produces <ca-x-1@>, which conforming receivers reject
    EMAIL_INBOUND_DOMAIN: str = ""
    EMAIL_FROM_ADDRESS: str = ""
    # Send outbound mail from this address instead of the channel's own mailbox
    # address. Empty means "send from the channel address", which is right
    # whenever the mailbox domain can also send.
    #
    # It cannot always. A Resend-managed <id>.resend.app subdomain receives but
    # is not a verified sending domain, so mail sent from it is refused with 403
    # and nothing ever leaves - including the verification probe, which would
    # leave the channel stuck below `active` and silently drop every inbound
    # message. This is the demo escape hatch for that case: point it at a sender
    # the provider does accept (e.g. onboarding@resend.dev) and the mailbox keeps
    # receiving while replies go out under a different, sendable identity.
    # Leave empty in production, where the mailbox domain is a verified domain
    # and sending from it is both possible and correct.
    EMAIL_SEND_FROM_ADDRESS: str = ""
    EMAIL_FROM_NAME: str = ""
    # matches ChatRequest.question max_length and MAX_QUERY_CHARS. the inbound body
    # is stripped of quoted history first, then truncated to this
    EMAIL_MAX_BODY_CHARS: int = 8000
    # email has no hard length limit; this only bounds a runaway generation
    EMAIL_MAX_REPLY_CHARS: int = 20000
    # asserted <= MAX_UPLOAD_BYTES: a larger value would let an attachment bypass
    # the ceiling UploadGate enforces
    EMAIL_ATTACHMENT_MAX_BYTES: int = 10 * 1024 * 1024
    # longer than the WhatsApp pair because the email answer path deliberately
    # spends more time thinking. a lease that expires mid-answer is worse than no
    # lease, so this is asserted >= HTTP_TIMEOUT_SECONDS * 2
    EMAIL_LOCK_TTL_SECONDS: int = 180
    EMAIL_LOCK_WAIT_SECONDS: float = 15.0
    # if the customer has written back this many times the agent is not answering
    # the question. stop, escalate, and say so
    EMAIL_MAX_REPLIES_PER_THREAD: int = 3
    # headerless thread continuity falls back to sender + normalised subject inside
    # this window. outside it, a new conversation: a split costs a human a second
    # glance, a merge poisons the summary with an unrelated problem
    EMAIL_THREAD_WINDOW_HOURS: int = 72
    # must outlast the provider's own webhook retry ladder, or a very late
    # redelivery is reprocessed as new after its receipt was purged
    EMAIL_RECEIPT_RETENTION_DAYS: int = 90

    # the growth platform (016_growth_core.sql). GROWTH_ENABLED is the platform
    # kill switch: off, the routers still mount and answer, the tables still
    # exist, and the scheduler does nothing — so a deployment can carry the code
    # without any tenant's agents acting. Per-tenant activation is the profile's
    # tenant_type, not this.
    GROWTH_ENABLED: bool = False
    # A deliberate, documented exception for the local demo: allow the growth
    # platform on the inline queue (DEMO_LOCAL=1) so the swarm runs on a laptop
    # with no QStash. Production deployments keep the durable-queue requirement —
    # the flag is refused outright when ENVIRONMENT=production.
    DEMO_LOCAL: bool = False
    # In-process cron ticker (app/api/internal/cron.py, LOCAL_CRON_INTERVALS).
    # The hosted deployment gets its cron pings from QStash schedules; a laptop
    # or demo has none, and without them the whole swarm silently never runs.
    # LOCAL_CRON fires the same task bodies on the same cadences from the app's
    # own lifespan. Never on alongside real QStash schedules — every task is
    # idempotent, but double-firing is waste — so production refuses it.
    LOCAL_CRON: bool = False
    # how long an approval request waits before it expires and the batch is held.
    # 48 h spans a weekend, which is when an owner is least likely to be looking
    # and most likely to be annoyed by a batch that quietly went out anyway
    GROWTH_APPROVAL_TTL_HOURS: int = 48
    # owner-facing Telegram for approvals and alerts. Same posture as
    # SLACK_ENABLED: false leaves the notifier unbuilt, and the bot token itself
    # is a per-tenant secret in tenant_secrets, never here — except the one
    # below, the platform's own bot
    TELEGRAM_ENABLED: bool = False
    # The platform's own bot token (provider mode): one bot every tenant
    # connects to by messaging it, no tokens or chat ids pasted in the panel.
    # Empty is per-tenant mode, exactly as before. Must pass the same shape
    # check a pasted tenant token does — it travels the same URL paths.
    TELEGRAM_BOT_TOKEN: str = ""
    # one conversation is handled at a time per chat: a redelivered webhook must
    # not produce two answers. Same shape and same reasons as the WhatsApp lock
    # — TTL bounds a crashed holder, WAIT bounds the queue's patience
    TELEGRAM_LOCK_TTL_SECONDS: int = 120
    TELEGRAM_LOCK_WAIT_SECONDS: float = 10.0

    # Paid lead-data and verification vendors (017_growth_outbound.sql). All
    # three are optional and all three are platform keys: the research agent's
    # base case is web-only — search, fetch, extract — and these raise the
    # verified-contact rate rather than enabling the feature. Empty leaves the
    # container property None, exactly as an empty TAVILY_API_KEY does.
    #
    # A tenant who brings their own account overrides these per tenant through
    # growth_integrations + tenant_secrets; these are the platform's own keys,
    # used when the tenant has connected nothing.
    APOLLO_API_KEY: str = ""
    HUNTER_API_KEY: str = ""
    EMAIL_VERIFIER_BACKEND: Literal["none", "zerobounce", "neverbounce"] = "none"
    EMAIL_VERIFIER_API_KEY: str = ""

    # Store connections (Stage 4: 019_growth_store.sql). Public-app OAuth
    # credentials for Shopify; empty leaves the Shopify connect flow refusing
    # with a clear message rather than building a broken authorize URL.
    # WooCommerce needs no platform key — every store brings its own REST
    # keypair, which lives per store in tenant_secrets.
    SHOPIFY_API_KEY: str = ""
    SHOPIFY_API_SECRET: str = ""

    # CRM push (Stage 6: 021_growth_crm.sql). Public-app OAuth credentials for
    # the two supported CRMs; empty leaves that CRM's connect flow refusing
    # with a clear message rather than building a broken authorize URL. These
    # are the platform's apps — every tenant's *grant* is their own row in
    # tenant_secrets, never this keypair.
    ZOHO_CLIENT_ID: str = ""
    ZOHO_CLIENT_SECRET: str = ""
    HUBSPOT_CLIENT_ID: str = ""
    HUBSPOT_CLIENT_SECRET: str = ""

    @field_validator("CORS_ORIGINS")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()

    @field_validator("LOG_LEVEL")
    @classmethod
    def _log_level(cls, value: str) -> str:
        # configure_logging feeds this straight into logging.basicConfig() and
        # structlog's level mapping, either of which raises on an unknown name —
        # at create_app() time, so a typo'd or empty env var refuses the whole
        # boot. clamp instead: a wrong verbosity is a nuisance, a dead app is not
        allowed = {"DEBUG", "INFO", "WARNING", "WARN", "ERROR", "CRITICAL", "FATAL"}
        upper = value.strip().upper()
        return upper if upper in allowed else "INFO"

    @field_validator("TELEGRAM_BOT_TOKEN")
    @classmethod
    def _telegram_bot_token(cls, value: str) -> str:
        # empty is per-tenant mode and needs no check; set means provider mode,
        # and the token must pass the same shape check a pasted tenant token
        # does (assert_telegram_bot_token) — it travels the same URL paths, so
        # a stray slash or space would retarget requests at another endpoint
        if not value:
            return value
        from app.adapters.channels.telegram import assert_telegram_bot_token

        assert_telegram_bot_token(value)
        return value.strip()

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]

    @property
    def url_fetch_ports(self) -> frozenset[int]:
        """Ports a server-side URL fetch may target."""
        ports: set[int] = set()
        for item in self.URL_FETCH_ALLOWED_PORTS.split(","):
            item = item.strip()
            if item.isdigit():
                ports.add(int(item))
        return frozenset(ports or {80, 443})

    @property
    def actions_allowed_ports(self) -> frozenset[int]:
        """Ports a tenant-authored action callout may target.

        Separate from url_fetch_ports because an action carries the tenant's own
        credential in a header, so 443 only is the sane default here even where
        plain HTTP is tolerated for document ingestion.
        """
        ports: set[int] = set()
        for item in self.ACTIONS_ALLOWED_PORTS.split(","):
            item = item.strip()
            if item.isdigit():
                ports.add(int(item))
        return frozenset(ports or {443})

    @property
    def crawl_browser_args(self) -> list[str]:
        """Extra Chromium flags for the crawl4ai browser.

        A list rather than the raw string because that is what ``BrowserConfig``
        takes, and because blank entries are the common failure: a trailing
        comma in a container env var would otherwise be passed to Chromium as an
        empty argument, which it rejects at launch.
        """
        return [item.strip() for item in self.CRAWL_BROWSER_ARGS.split(",") if item.strip()]

    @property
    def crawl_user_agent(self) -> str:
        """Identifiable User-Agent for every crawl request.

        A crawler that does not say who it is gets blocked by every WAF, and a
        contactable URL in the UA is what gets us unblocked again.
        """
        if self.CRAWL_USER_AGENT:
            return self.CRAWL_USER_AGENT
        base = self.PUBLIC_BASE_URL.rstrip("/")
        return f"ContextAgentBot/1.0 (+{base}/bot)"

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"

    def validate_runtime(self) -> None:
        """Fail fast on misconfiguration. Called at startup, never at import."""
        problems: list[str] = []

        if self.is_production:
            if self.SECRET_KEY == INSECURE_DEFAULT:
                problems.append("SECRET_KEY must be set")
            if self.ENCRYPTION_KEY == INSECURE_DEFAULT:
                problems.append("ENCRYPTION_KEY must be set")
            for name, value in (
                ("QUEUE_BACKEND", self.QUEUE_BACKEND),
                ("CACHE_BACKEND", self.CACHE_BACKEND),
                ("STORAGE_BACKEND", self.STORAGE_BACKEND),
                ("VECTOR_BACKEND", self.VECTOR_BACKEND),
                ("EMBEDDING_BACKEND", self.EMBEDDING_BACKEND),
            ):
                if value in {"inline", "memory", "local", "fake"}:
                    problems.append(f"{name}={value} is development only")
            # DEMO_LOCAL relaxes the growth/inline-queue guard above; that
            # relaxation must never ride into a production deployment
            if self.DEMO_LOCAL:
                problems.append("DEMO_LOCAL is a local-demo flag and must be false in production")
            # an SSRF escape hatch in production would let a tenant reach the
            # private subnet through our own fetcher
            if self.URL_FETCH_ALLOW_PRIVATE:
                problems.append("URL_FETCH_ALLOW_PRIVATE must be false in production")
            # an action sends the tenant's own API key in a header. over port 80
            # that is a credential handed to anyone on the path
            if 80 in self.actions_allowed_ports:
                problems.append("ACTIONS_ALLOWED_PORTS must not include 80 in production")
            # production is the multi-machine deployment (deploy/fly.toml scales to
            # zero and back up), and a per-process cache cannot see a jti replayed
            # against a second machine with a cold cache
            if self.IDENTITY_VERIFICATION_ENABLED and self.CACHE_BACKEND == "memory":
                problems.append(
                    "IDENTITY_VERIFICATION_ENABLED requires a shared cache "
                    "(CACHE_BACKEND=redis): an in-process cache cannot detect a "
                    "cross-machine jti replay"
                )

        # the branch editor renders exactly one level of branching, so a deeper
        # ceiling would author procedures the UI can never show or edit again
        if self.PROCEDURE_MAX_BRANCH_DEPTH != 1:
            problems.append("PROCEDURE_MAX_BRANCH_DEPTH must be 1")
        if self.IDENTITY_MAX_TTL_SECONDS > 600:
            problems.append("IDENTITY_MAX_TTL_SECONDS must be <= 600")
        if self.IDENTITY_CLOCK_SKEW_SECONDS > 300:
            problems.append("IDENTITY_CLOCK_SKEW_SECONDS must be <= 300")

        if self.VECTOR_BACKEND == "qdrant" and not self.QDRANT_URL:
            problems.append("QDRANT_URL required when VECTOR_BACKEND=qdrant")
        if self.CACHE_BACKEND == "redis" and not self.REDIS_URL:
            problems.append("REDIS_URL required when CACHE_BACKEND=redis")
        if self.QUEUE_BACKEND == "arq":
            # "arq" is a valid Literal but there is no arq adapter — container.queue
            # would silently hand back LocalQueue in a deployment that believes it
            # has a durable queue, losing every job on the first scale-to-zero
            problems.append("QUEUE_BACKEND=arq is not implemented; use qstash or inline")
        if self.STORAGE_BACKEND == "supabase" and not (self.SUPABASE_URL and self.SUPABASE_KEY):
            problems.append("SUPABASE_URL and SUPABASE_KEY required for supabase storage")
        if self.QUEUE_BACKEND == "qstash" and not self.QSTASH_TOKEN:
            problems.append("QSTASH_TOKEN required when QUEUE_BACKEND=qstash")
        if self.CRAWL_RENDER_BACKEND == "http" and not self.CRAWL_RENDER_URL:
            problems.append("CRAWL_RENDER_URL required when CRAWL_RENDER_BACKEND=http")
        if self.QUEUE_BACKEND == "qstash":
            # QStash calls us back over the public internet, so it has to be given
            # a reachable URL. an empty one publishes to `/publish//internal/jobs`,
            # which QStash answers with a bare 404 that looks like a bad token
            if not self.PUBLIC_BASE_URL:
                problems.append("PUBLIC_BASE_URL required when QUEUE_BACKEND=qstash")
            elif self.PUBLIC_BASE_URL.startswith("http://localhost") or "127.0.0.1" in (
                self.PUBLIC_BASE_URL
            ):
                problems.append(
                    "PUBLIC_BASE_URL must be publicly reachable for qstash callbacks "
                    "(use a tunnel, or QUEUE_BACKEND=inline for local work)"
                )
            if not self.QSTASH_CURRENT_SIGNING_KEY:
                # without it every callback is rejected as unsigned and no
                # document ever leaves `queued`
                problems.append("QSTASH_CURRENT_SIGNING_KEY required when QUEUE_BACKEND=qstash")

        problems.extend(self._email_problems())
        problems.extend(self._escalation_problems())
        problems.extend(self._growth_problems())

        if problems:
            raise ValueError("invalid configuration: " + "; ".join(problems))

    def _growth_problems(self) -> list[str]:
        """Growth platform wiring.

        Nothing here checks tenant data, for the reason the email block states: a
        deployment that cannot start because a row exists is worse than one that
        starts with the feature erroring visibly.
        """
        problems: list[str] = []
        if self.GROWTH_APPROVAL_TTL_HOURS < 1:
            problems.append("GROWTH_APPROVAL_TTL_HOURS must be >= 1")
        if self.LOCAL_CRON and self.ENVIRONMENT == "production":
            # production gets its cron from QStash schedules; a local ticker
            # alongside them double-fires every task
            problems.append("LOCAL_CRON is a dev/demo flag and must not run in production")
        if not self.GROWTH_ENABLED:
            return problems
        # the scheduler is a cron task and the sends are queue jobs; with the
        # in-process queue every batch dies on the next scale-to-zero, and this
        # is a feature whose whole value is that it keeps running unattended.
        # DEMO_LOCAL is the one exception: a laptop demo that accepts losing
        # in-flight batches on restart rather than requiring a public queue
        if self.QUEUE_BACKEND == "inline" and not self.DEMO_LOCAL:
            problems.append("GROWTH_ENABLED requires a durable queue (QUEUE_BACKEND=qstash)")
        # the per-tenant send lock and the alert cooldown are cache leases. A
        # per-process cache cannot see the batch a second machine is already
        # sending, which is the exact failure the lock exists to prevent
        if self.CACHE_BACKEND == "memory":
            problems.append(
                "GROWTH_ENABLED requires a shared cache (CACHE_BACKEND=redis): the "
                "per-tenant send lock is a lease, and a per-process cache cannot "
                "see another machine holding it"
            )
        # the approval notification's whole content is a link to the decision
        if not (self.PUBLIC_BASE_URL or self.DASHBOARD_URL):
            problems.append("PUBLIC_BASE_URL or DASHBOARD_URL required for approval links")
        return problems

    def _email_problems(self) -> list[str]:
        """Inbound email. Nothing here depends on tenant data: boot must not.

        Whether any tenant has an active channel is deliberately not checked — a
        deployment that cannot start because a row exists is worse than one that
        starts with the channel erroring visibly.
        """
        problems: list[str] = []
        if self.EMAIL_BACKEND == "resend":
            if not self.RESEND_API_KEY:
                problems.append("RESEND_API_KEY required when EMAIL_BACKEND=resend")
            if not self.RESEND_WEBHOOK_SECRET:
                # empty rejects every inbound message as unsigned, so no customer
                # is ever answered — the failure QSTASH_CURRENT_SIGNING_KEY is
                # guarded against above
                problems.append("RESEND_WEBHOOK_SECRET required when EMAIL_BACKEND=resend")

        if self.EMAIL_BACKEND != "none":
            domain = self.EMAIL_INBOUND_DOMAIN.strip().lower()
            if not domain or "." not in domain or "@" in domain:
                problems.append(
                    "EMAIL_INBOUND_DOMAIN must be a bare domain when EMAIL_BACKEND is set"
                )
            sender = self.EMAIL_FROM_ADDRESS.strip().lower()
            if sender.count("@") != 1 or sender.startswith("@") or sender.endswith("@"):
                problems.append(
                    "EMAIL_FROM_ADDRESS must be a single address when EMAIL_BACKEND is set"
                )
            elif self.is_production and domain and sender.rsplit("@", 1)[1] != domain:
                # sending from a domain we do not receive on breaks replies and
                # DMARC alignment at the same time
                problems.append("EMAIL_FROM_ADDRESS domain must equal EMAIL_INBOUND_DOMAIN")

            # An override sender is a split identity: the mailbox receives on one
            # domain and replies leave under another. That is legitimate for a
            # demo on a receive-only provider, but never in production, where it
            # would put replies out of DMARC alignment with the domain we
            # advertise.
            override = self.EMAIL_SEND_FROM_ADDRESS.strip().lower()
            if override:
                if override.count("@") != 1 or override.startswith("@") or override.endswith("@"):
                    problems.append(
                        "EMAIL_SEND_FROM_ADDRESS must be a single address when set"
                    )
                elif self.is_production:
                    problems.append(
                        "EMAIL_SEND_FROM_ADDRESS must be empty in production; "
                        "send from the verified mailbox domain instead"
                    )

        if self.EMAIL_MAX_BODY_CHARS < 500:
            problems.append("EMAIL_MAX_BODY_CHARS must be >= 500")
        if self.EMAIL_MAX_REPLY_CHARS < 500:
            problems.append("EMAIL_MAX_REPLY_CHARS must be >= 500")
        if self.EMAIL_ATTACHMENT_MAX_BYTES > self.MAX_UPLOAD_BYTES:
            problems.append("EMAIL_ATTACHMENT_MAX_BYTES must be <= MAX_UPLOAD_BYTES")
        if self.EMAIL_LOCK_TTL_SECONDS <= 0:
            problems.append("EMAIL_LOCK_TTL_SECONDS must be > 0")
        elif self.EMAIL_LOCK_TTL_SECONDS < self.HTTP_TIMEOUT_SECONDS * 2:
            problems.append("EMAIL_LOCK_TTL_SECONDS must be >= HTTP_TIMEOUT_SECONDS * 2")
        if self.EMAIL_LOCK_WAIT_SECONDS <= 0:
            problems.append("EMAIL_LOCK_WAIT_SECONDS must be > 0")
        if self.EMAIL_MAX_REPLIES_PER_THREAD < 1:
            problems.append("EMAIL_MAX_REPLIES_PER_THREAD must be >= 1")
        if self.EMAIL_THREAD_WINDOW_HOURS < 1:
            problems.append("EMAIL_THREAD_WINDOW_HOURS must be >= 1")
        if self.EMAIL_RECEIPT_RETENTION_DAYS < 7:
            problems.append("EMAIL_RECEIPT_RETENTION_DAYS must be >= 7")
        return problems

    def _escalation_problems(self) -> list[str]:
        problems: list[str] = []
        if self.ESCALATION_DEFAULT_HOURLY_CAP < 1:
            problems.append("ESCALATION_DEFAULT_HOURLY_CAP must be >= 1")
        if self.ESCALATION_RETENTION_DAYS < 30:
            problems.append("ESCALATION_RETENTION_DAYS must be >= 30")
        if self.ESCALATION_NOTIFY_TIMEOUT_SECONDS <= 0:
            problems.append("ESCALATION_NOTIFY_TIMEOUT_SECONDS must be > 0")
        # the escalation deep link is the whole point of the notification: with
        # neither base URL the human is told something happened and given no way
        # to reach it
        if self.is_production and not (self.PUBLIC_BASE_URL or self.DASHBOARD_URL):
            problems.append("PUBLIC_BASE_URL or DASHBOARD_URL required for escalation links")
        return problems


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
