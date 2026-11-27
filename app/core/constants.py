"""Platform constants that are not per tenant.

Anything a tenant can vary lives in the database, not here.
"""

from enum import StrEnum

# chunking defaults. tenants can override through agent retrieval_config
PARENT_CHUNK_SIZE = 2000
PARENT_CHUNK_OVERLAP = 200
CHILD_CHUNK_SIZE = 400
CHILD_CHUNK_OVERLAP = 50
MAX_STRUCTURED_CHUNK_SIZE = 3000
STRUCTURED_CHUNK_OVERLAP = 200

# retrieval defaults
DEFAULT_TOP_K = 20
DEFAULT_RERANK_TOP_N = 8
DEFAULT_MIN_CONFIDENCE = 0.45
RRF_K = 60

# conversation context defaults
DEFAULT_HISTORY_TURNS = 6
SUMMARY_EVERY_N_TURNS = 10
CHECKPOINT_TTL_DAYS = 30

TEXT_PREVIEW_CHARS = 500

# page classification thresholds for the parse tier decision
MIN_TEXT_CHARS_PER_PAGE = 120
MIN_TEXT_DENSITY = 0.35

# below this many characters a page is treated as having no text layer
OCR_MIN_CHARS_PER_PAGE = 80
# a page whose tables cover more than this fraction is worth a vision model
TABLE_AREA_RATIO_FOR_VLM = 0.55

# raster resolution for pages sent to an OCR provider. 200 dpi keeps small type
# legible without inflating the per-page image token bill
OCR_RASTER_DPI = 200
# bound concurrent OCR calls. providers rate limit, and a burst that trips the
# limit costs the whole document rather than one page
OCR_MAX_CONCURRENCY = 4
# quality gates that decide whether recovered text is usable. A page that fails
# these checks keeps whatever the free tier had instead of accepting garbage
MIN_PRINTABLE_RATIO = 0.7  # at least 70% letters/digits/punctuation
MAX_NGRAM_REPEAT_RATIO = 0.4  # no more than 40% repeated 3-grams

MAGIC_BYTES: dict[str, tuple[bytes, ...]] = {
    "application/pdf": (b"%PDF-",),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": (b"PK\x03\x04",),
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": (b"PK\x03\x04",),
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": (b"PK\x03\x04",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/webp": (b"RIFF",),
}

EXTENSION_TO_MIME: dict[str, str] = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".html": "text/html",
    ".htm": "text/html",
    ".csv": "text/csv",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}

# plain text formats need no magic byte check, they have no signature
TEXTUAL_MIMES = frozenset(
    {"text/plain", "text/markdown", "text/html", "text/csv"}
)

# zip bomb guard. a small archive expanding beyond this ratio is rejected
MAX_ARCHIVE_EXPANSION_RATIO = 120

# ------------------------------------------------------------------ website crawl
# Bounds and data tables the PURE domain logic in app/domain/crawl.py needs.
# crawl.py must stay importable without Settings, so anything it decides with
# lives here. Operational knobs an operator retunes per deployment (concurrency,
# budgets, timeouts, the user agent) live in app/core/config.py instead; where a
# number appears in both, the Settings default mirrors the constant below.

# depth. CrawlConfig clamps a requested depth into [1, CRAWL_MAX_DEPTH_CEILING];
# depth 5 reaches everything on a normal SMB site, depth 10 reaches every faceted
# search permutation, which is why the ceiling is not higher
CRAWL_DEFAULT_MAX_DEPTH = 5
CRAWL_MAX_DEPTH_CEILING = 10

# pages. the API accepts up to this many; an explicit request may use it all
CRAWL_MAX_PAGES_CEILING = 10000
# A crawl with no page budget of its own gets this many pages. The add-source
# form sends no budget (the owner types only a URL), so this number is the
# real per-source cap on a free-tier stack: every fetched page costs a
# crawl4ai browser read, storage in the documents bucket, and — for the pages
# that survive review — embedding tokens and vector rows. 300 reads a normal
# SMB site whole (a typical 5–15 page marketing site plus its blog) and
# bounds a runaway crawl of a faceted/large site; a tenant who genuinely
# needs more sets it explicitly in the crawl settings.
CRAWL_DEFAULT_MAX_PAGES = 300

# PDFs found by a crawl are opt-in and capped per source: the pipeline handles
# them fine, but OCR is the one genuinely expensive step, so a crawl that finds
# 300 scanned brochures is a budget event rather than a nice surprise
CRAWL_MAX_PDFS_PER_SOURCE = 20

# include/exclude glob patterns, validated at source create so a bad pattern is a
# 422 naming the offender rather than a crawl that silently indexes nothing
CRAWL_MAX_PATTERNS = 20
CRAWL_MAX_PATTERN_LENGTH = 200

# per host politeness, milliseconds. a robots Crawl-delay is clamped into this
# range: a site asking for 120s would hold a 500 page crawl for 17 hours, which
# is worse for the tenant than a slightly impolite crawl. mirrors the
# CRAWL_DEFAULT_DELAY_MS / CRAWL_MIN_DELAY_MS defaults in Settings
CRAWL_DEFAULT_DELAY_MS = 100
CRAWL_MIN_DELAY_MS = 50
CRAWL_MAX_DELAY_MS = 30_000

# more than this many surviving query params after canonicalisation reads as a
# faceted listing view. thousands of those exist per site and hold no new text
CRAWL_MAX_QUERY_PARAMS = 2

# near duplicate detection. 64 bit simhash over word 3-shingles of the extracted
# markdown. distance <= 3 on 64 bits is roughly "95% of shingles shared", which
# catches a print view or an AMP variant while leaving two posts on one topic
# (typically 15-25) apart. the live threshold is CRAWL_SIMHASH_MAX_DISTANCE in
# Settings; this is the domain default when no Settings value is threaded through
CRAWL_SIMHASH_BITS = 64
CRAWL_SIMHASH_SHINGLE_SIZE = 3
CRAWL_SIMHASH_MAX_DISTANCE_DEFAULT = 3
# signed BIGINT range, because postgres has no uint64 and source_links.simhash is
# a BIGINT. the round trip is exact, not lossy
CRAWL_SIMHASH_SIGN_BOUND = 1 << 63
CRAWL_SIMHASH_MASK = (1 << 64) - 1

# a page with less extracted markdown than this has no readable content after nav
# and footer removal. mirrors MIN_TEXT_CHARS_PER_PAGE for the same reason
CRAWL_MIN_TEXT_CHARS = 120
# documents.filename is the citation label a reader sees, so it is truncated
CRAWL_MAX_TITLE_CHARS = 255

# SPA detector. all three must hold, so a genuinely short page is not misclassified
CRAWL_SPA_MAX_MARKDOWN_CHARS = 200
CRAWL_SPA_MIN_HTML_BYTES = 4000
CRAWL_SPA_MAX_TEXT_RATIO = 0.02
# root mount points whose empty subtree means the content is client rendered
CRAWL_SPA_MOUNT_SELECTORS: tuple[str, ...] = (
    "#root",
    "#app",
    "#__next",
    "[data-reactroot]",
    "[ng-app]",
    "[data-server-rendered]",
)

# tracking and session parameters dropped by name during canonicalisation. an
# exact denylist, not a heuristic: "drop any long opaque value" would eat
# legitimate product SKUs and document ids
CRAWL_TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "gclid",
        "gbraid",
        "wbraid",
        "dclid",
        "fbclid",
        "msclkid",
        "twclid",
        "ttclid",
        "igshid",
        "yclid",
        "mc_cid",
        "mc_eid",
        "_ga",
        "_gl",
        "ref",
        "ref_src",
        "referrer",
        "source",
        "campaign",
        "sessionid",
        "session_id",
        "phpsessid",
        "jsessionid",
        "aspsessionid",
        "sid",
        "s_kwcid",
        "vero_id",
        "hsa_acc",
        "hsctatracking",
        "replytocom",
        "share",
        "from",
    }
)

# a query parameter with one of these names identifies a filtered listing view
CRAWL_FACET_PARAMS = frozenset(
    {
        "filter",
        "facet",
        "sort",
        "orderby",
        "order",
        "color",
        "size",
        "price",
        "brand",
        "per_page",
        "view",
        "layout",
    }
)

# extensions that are not a web page. checked on the canonical path before any
# fetch, so an asset never costs a request
CRAWL_NON_PAGE_EXTENSIONS = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".webp",
        ".avif",
        ".svg",
        ".ico",
        ".css",
        ".js",
        ".mjs",
        ".map",
        ".zip",
        ".tar",
        ".gz",
        ".rar",
        ".7z",
        ".exe",
        ".dmg",
        ".pkg",
        ".mp4",
        ".mp3",
        ".wav",
        ".avi",
        ".mov",
        ".webm",
        ".woff",
        ".woff2",
        ".ttf",
        ".eot",
        ".xml",
        ".json",
        ".yaml",
        ".csv",
        ".xls",
        ".doc",
        ".ppt",
    }
)

# directory index filenames stripped during canonicalisation: /a/index.html and
# /a/ are the same page and must not both enter the frontier
CRAWL_INDEX_FILENAMES: tuple[str, ...] = (
    "index.html",
    "index.htm",
    "index.php",
    "default.aspx",
)

# content types the crawler will extract from. application/pdf is allowed only
# when the source opted in with include_pdf
CRAWL_HTML_MIMES = frozenset(
    {"text/html", "application/xhtml+xml", "text/plain", "text/markdown"}
)
CRAWL_PDF_MIME = "application/pdf"
CRAWL_PDF_EXTENSIONS = frozenset({".pdf"})

# unreserved characters per RFC 3986: percent encoding of these is decoded during
# canonicalisation so %7Euser and ~user are one URL
CRAWL_UNRESERVED_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"


class DocumentStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"
    DELETED = "deleted"


class JobStep(StrEnum):
    """Ingestion state machine. Each step is separately resumable, so a retry
    never redoes paid work such as OCR."""

    VALIDATE = "validate"
    STORE = "store"
    CLASSIFY = "classify"
    PARSE = "parse"
    CHUNK = "chunk"
    EMBED = "embed"
    INDEX = "index"
    UPSERT = "upsert"
    VERIFY = "verify"
    FINALIZE = "finalize"
    DONE = "done"


class JobState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ParseTier(StrEnum):
    """Which parser handled a page. FAST and STRUCTURED are free, the rest cost."""

    FAST = "tier0_text"
    TEXT = "tier0_text"
    STRUCTURED = "tier1_structured"
    OCR = "tier2_ocr"
    VLM = "tier2_vlm"
    OCR_FALLBACK = "tier3_ocr_fallback"


class ChunkKind(StrEnum):
    STRUCTURED = "structured"
    PARENT_CHILD = "parent_child"
    TABULAR = "tabular"
    QA = "qa"  # question variations embedded, answer returned


class Channel(StrEnum):
    WIDGET = "widget"
    WHATSAPP = "whatsapp"
    API = "api"
    # inbound email, forwarded to a per-agent address. a turn arriving this way
    # has no session and no live visitor, which is why identity on this channel is
    # established from the verified sender rather than from a signed token
    EMAIL = "email"
    # a Telegram chat with the tenant's default chat agent. identity is the chat
    # id (hashed per tenant like a phone number), so a returning chat resumes its
    # own thread. the owner's chat is the same bot on the same channel — it is
    # the growth bot's commands that make it look like a different surface
    TELEGRAM = "telegram"


class Role(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class UsageKind(StrEnum):
    INGEST_PAGE = "ingest_page"
    OCR_PAGE = "ocr_page"
    # one hosted browser render of a JavaScript-rendered crawl page. billable and
    # plan gated, so a crawl that hits an SPA is visible in the cost breakdown
    RENDER_PAGE = "render_page"
    EMBED_TOKEN = "embed_token"
    LLM_TOKEN_IN = "llm_token_in"
    LLM_TOKEN_OUT = "llm_token_out"
    QUERY = "query"
    STORAGE_BYTE = "storage_byte"
    # one dispatched action call. metered, and priced small-and-positive rather
    # than zero so a runaway tool loop shows up as a line on the usage ledger.
    # it is not where the money goes — prefill tokens are, and
    # LLM_TOKEN_IN/OUT carry those per turn
    ACTION_CALL = "action_call"
    # metered, never billed, exactly like an escalation: visible in the usage
    # breakdown so a tenant can see procedure volume, charged at zero
    PROCEDURE_RUN = "procedure_run"
    # one handoff to a human. metered at zero: it is the outcome the product is
    # meant to produce when the agent cannot answer, so charging for it would
    # price a tenant out of doing the right thing
    ESCALATION = "escalation"
    # one outbound email we sent. metered at zero for the same reason, and kept
    # separate from ESCALATION because a reply is not a handoff
    EMAIL_SENT = "email_sent"


MAX_JOB_ATTEMPTS = 5
JOB_BACKOFF_BASE_SECONDS = 5


# --------------------------------------------------------------------- actions
#
# The actions engine's vocabulary lives here rather than in the services that use
# it, because three separate layers have to agree on the same strings: the
# dispatch loop, the HTTP endpoints, and the executor. A name invented twice is a
# quota that silently does not apply.

# Tool names the platform owns. A tenant action may not claim one of these,
# because the dispatch loop resolves the reserved handler first and a shadowing
# action would simply never fire — a defect that looks like a model failure.
RESERVED_TOOL_NAMES = frozenset(
    {
        "start_procedure",
        "end_procedure",
        "search_documents",
        "escalate",
        "system",
    }
)

# Audit events. Every mutation of an action, and the three runtime events that
# are security-relevant rather than volume: an SSRF block, an injection flag, and
# quota exhaustion (written once per period, not once per refused call).
AUDIT_ACTION_CREATED = "action.created"
AUDIT_ACTION_UPDATED = "action.updated"
AUDIT_ACTION_DELETED = "action.deleted"
AUDIT_ACTION_ENABLED = "action.enabled"
AUDIT_ACTION_DISABLED = "action.disabled"
AUDIT_ACTION_TESTED = "action.tested"
AUDIT_ACTION_SECRET_SET = "action.secret_set"  # noqa: S105 - audit event name
AUDIT_ACTION_SECRET_ROTATED = "action.secret_rotated"  # noqa: S105 - audit event name
AUDIT_ACTION_SECRET_DELETED = "action.secret_deleted"  # noqa: S105 - audit event name
AUDIT_ACTION_LINKED_TO_AGENT = "action.linked_to_agent"
AUDIT_ACTION_UNLINKED_FROM_AGENT = "action.unlinked_from_agent"
AUDIT_ACTION_BLOCKED_SSRF = "action.blocked_ssrf"
AUDIT_ACTION_INJECTION_FLAGGED = "action.injection_flagged"
AUDIT_ACTION_TRANSCRIPT_EGRESS = "action.transcript_egress"

ACTION_AUDIT_EVENTS = frozenset(
    {
        AUDIT_ACTION_CREATED,
        AUDIT_ACTION_UPDATED,
        AUDIT_ACTION_DELETED,
        AUDIT_ACTION_ENABLED,
        AUDIT_ACTION_DISABLED,
        AUDIT_ACTION_TESTED,
        AUDIT_ACTION_SECRET_SET,
        AUDIT_ACTION_SECRET_ROTATED,
        AUDIT_ACTION_SECRET_DELETED,
        AUDIT_ACTION_LINKED_TO_AGENT,
        AUDIT_ACTION_UNLINKED_FROM_AGENT,
        AUDIT_ACTION_BLOCKED_SSRF,
        AUDIT_ACTION_INJECTION_FLAGGED,
        AUDIT_ACTION_TRANSCRIPT_EGRESS,
    }
)

# Rate-limiter scopes. Independent windows, because each one above can be
# evaded: a per-action ceiling does not stop one visitor driving forty different
# actions, and a per-visitor ceiling does not stop one conversation looping.
RL_SCOPE_ACTION_ONE = "action:one"
RL_SCOPE_ACTION_CONVERSATION = "action:conv"
RL_SCOPE_ACTION_VISITOR = "action:visitor"

# Platform constants for the two scopes a plan does not parameterise.
ACTION_CONVERSATION_RATE_PER_MINUTE = 10
ACTION_VISITOR_RATE_PER_MINUTE = 20

# Per-conversation ceilings. The call ceiling is a platform constant; the write
# ceiling is the ActionsConfig default and is tenant-tunable below it.
ACTION_MAX_CALLS_PER_CONVERSATION = 10
ACTION_MAX_WRITES_PER_CONVERSATION = 3

# The escape hatch is never rate limited and never metered as a billable
# action call. That is what makes "actions are unavailable" honest rather than
# a silent degradation: the worst case is being handed to a person.
ACTION_QUOTA_EXEMPT_HANDLERS = frozenset({"escalate_to_human"})

# An `a[].b` projection over a 5000-row response is a prompt bomb, so the
# projector truncates every array it walks.
ACTION_ARRAY_PROJECTION_LIMIT = 20

# Circuit breaker name for all action egress. Per-action breakers are keyed
# `action:<action_id>` on top of this one, so one tenant's dead endpoint cannot
# trip another tenant's integration.
ACTION_BREAKER_NAME = "actions"


# ------------------------------------------------- contacts and verified identity
#
# The identity vocabulary lives here for the same reason the actions vocabulary
# does: the verifier, the session endpoint, the executor and the dashboard all
# have to agree on the same strings, and a reason invented twice is a refusal that
# silently does not happen.

# `contacts.source`, mirroring the contacts_source_valid CHECK in
# migrations/010_procedures_contacts.sql.
CONTACT_SOURCE_IDENTITY_JWT = "identity_jwt"
CONTACT_SOURCE_COLLECT_LEAD = "collect_lead"
CONTACT_SOURCE_COLLECT_DATA = "collect_data"
CONTACT_SOURCE_WHATSAPP = "whatsapp"
CONTACT_SOURCE_EMAIL = "email"
CONTACT_SOURCE_DASHBOARD = "dashboard"
CONTACT_SOURCE_API = "api"

CONTACT_SOURCES = frozenset(
    {
        CONTACT_SOURCE_IDENTITY_JWT,
        CONTACT_SOURCE_COLLECT_LEAD,
        CONTACT_SOURCE_COLLECT_DATA,
        CONTACT_SOURCE_WHATSAPP,
        CONTACT_SOURCE_EMAIL,
        CONTACT_SOURCE_DASHBOARD,
        CONTACT_SOURCE_API,
    }
)

# `contact_events.kind`, mirroring the contact_events_kind_valid CHECK in 010.
CONTACT_EVENT_IDENTITY_VERIFIED = "identity_verified"
CONTACT_EVENT_LEAD_COLLECTED = "lead_collected"
CONTACT_EVENT_DATA_COLLECTED = "data_collected"
CONTACT_EVENT_ATTRIBUTES_UPDATED = "attributes_updated"
CONTACT_EVENT_MERGE_DECLINED = "merge_declined"
CONTACT_EVENT_EXPORTED = "exported"
CONTACT_EVENT_ERASED = "erased"

CONTACT_EVENT_KINDS = frozenset(
    {
        CONTACT_EVENT_IDENTITY_VERIFIED,
        CONTACT_EVENT_LEAD_COLLECTED,
        CONTACT_EVENT_DATA_COLLECTED,
        CONTACT_EVENT_ATTRIBUTES_UPDATED,
        CONTACT_EVENT_MERGE_DECLINED,
        CONTACT_EVENT_EXPORTED,
        CONTACT_EVENT_ERASED,
    }
)

# Columns are tried before `attributes`, so `{{contact.email}}` cannot be shadowed
# by a tenant-supplied attribute of the same name while `{{contact.plan}}` still
# finds `attributes->>'plan'`.
CONTACT_COLUMN_FIELDS = ("email", "name", "phone", "external_id")

# `attributes` keys are interpolated into URL paths and query strings, so the
# shape is constrained rather than merely validated.
CONTACT_ATTRIBUTE_KEY_PATTERN = r"^[a-z][a-z0-9_]{0,30}$"

# The two template namespaces a verified identity fills. `user.*` is what the
# tenant asserted this session and `contact.*` is what we have stored; the
# authoring UI surfaces only the canonical `contact.` form and documents `user.`
# as accepted, for Chatbase parity with snippets tenants copy.
IDENTITY_CONTACT_NAMESPACES = frozenset({"contact", "user"})

# The two refusal outcomes of §4.6. Same strings as
# ActionOutcomeCode.IDENTITY_REQUIRED / IDENTITY_INCOMPLETE, named here so a
# caller that has no ActionOutcome to hand still uses the same word.
IDENTITY_OUTCOME_REQUIRED = "identity_required"
IDENTITY_OUTCOME_INCOMPLETE = "identity_incomplete"

# The fixed tool result the model receives when an action needed a signed-in
# visitor. It names no attribute, so it cannot be used to enumerate what the
# tenant stores.
IDENTITY_REQUIRED_TOOL_MESSAGE = (
    "This needs the visitor to be signed in. Ask them to sign in on the website "
    "and try again."
)

# The one line the system prompt gains when an identity-gated action exists and
# the visitor is unverified.
IDENTITY_UNVERIFIED_PROMPT = (
    "Some lookups need the visitor signed in. If they ask for something personal "
    "to their account, ask them to sign in on the website first."
)

# Verification attempts get their own rate-limit scope, separate from the session
# quota, so a broken integration minting bad tokens in a loop cannot exhaust
# session creation for everyone else.
RL_SCOPE_IDENTITY_VERIFY = "identity_verify"
IDENTITY_VERIFY_RATE_PER_MINUTE = 60

# A tenant whose minting is broken produces one rejection per page load. Log one
# in this many; the first is always logged.
IDENTITY_REJECT_LOG_SAMPLE = 20

# Audit events. Minting a verifier secret is the security-relevant one: it is the
# moment a tenant gains the ability to assert who a visitor is.
AUDIT_IDENTITY_VERIFIER_CREATED = "identity.verifier_created"
AUDIT_IDENTITY_VERIFIER_DELETED = "identity.verifier_deleted"
AUDIT_IDENTITY_SECRET_MINTED = "identity.secret_minted"  # noqa: S105 - audit event name
AUDIT_CONTACT_UPDATED = "contact.updated"
AUDIT_CONTACT_DELETED = "contact.deleted"
AUDIT_CONTACT_EXPORTED = "contact.exported"

IDENTITY_AUDIT_EVENTS = frozenset(
    {
        AUDIT_IDENTITY_VERIFIER_CREATED,
        AUDIT_IDENTITY_VERIFIER_DELETED,
        AUDIT_IDENTITY_SECRET_MINTED,
        AUDIT_CONTACT_UPDATED,
        AUDIT_CONTACT_DELETED,
        AUDIT_CONTACT_EXPORTED,
    }
)


# Growth platform (016_growth_core.sql). Only the two events that are genuinely
# platform-level reach the shared audit log: erasing a tenant's growth data, and
# toggling feature cost visibility. Everything else an agent decides lives in
# growth_agent_actions, which is the per-tenant audit trail and is far too
# high-volume for this table. (The agent-model events died with the admin
# console that wrote them, 2026-09-15.)
AUDIT_GROWTH_DATA_DELETED = "growth.data_deleted"
AUDIT_FEATURE_COST_VISIBILITY = "growth.feature_cost_visibility"

GROWTH_AUDIT_EVENTS = frozenset(
    {
        AUDIT_GROWTH_DATA_DELETED,
        AUDIT_FEATURE_COST_VISIBILITY,
    }
)
