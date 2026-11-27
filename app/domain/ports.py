"""Port protocols. Every external system is reached through one of these.

Services depend on these types only. Adapters implement them. Swapping a vendor
is writing one adapter and changing one setting, never touching a service.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from uuid import UUID

from app.domain.entities import Chunk, RetrievedChunk, WebResult

if TYPE_CHECKING:
    # Completion and Message are the LLM currency the adapters already speak.
    # The import is type-only because the adapter layer imports this module at
    # runtime to subclass LLMProvider, and a runtime import here would close
    # that cycle.
    from app.adapters.llm.base import Completion, Message
    from app.domain.actions import ActionContext, ActionDefinition, ActionOutcome
    from app.domain.email_channel import EmailAttachmentRef
    from app.domain.escalation import EscalationEvent


@dataclass(slots=True)
class SparseVector:
    indices: list[int]
    values: list[float]


@dataclass(slots=True)
class VectorRecord:
    id: str
    tenant_id: UUID
    dense: list[float]
    sparse: SparseVector | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SearchHit:
    id: str
    score: float
    payload: dict[str, Any]


@dataclass(slots=True)
class LLMMessage:
    role: str
    content: str


@runtime_checkable
class VectorStore(Protocol):
    async def ensure_collection(self, name: str, dimensions: int) -> None:
        """Create the collection and the tenant_id keyword index if absent.

        The payload index must exist before any ingest or every filtered query
        degrades to a full scan.
        """
        ...

    async def upsert(self, collection: str, records: Sequence[VectorRecord]) -> int: ...

    async def search_hybrid(
        self,
        collection: str,
        tenant_id: UUID,
        dense: list[float],
        sparse: SparseVector | None,
        limit: int,
        filters: dict[str, Any] | None = None,
    ) -> list[SearchHit]:
        """Dense and sparse prefetch fused server side. tenant_id is mandatory,
        never optional, so an unfiltered search is not expressible."""
        ...

    async def delete_by_document(self, collection: str, tenant_id: UUID, document_id: UUID) -> int:
        ...

    async def count(
        self, collection: str, tenant_id: UUID, document_id: UUID | None = None
    ) -> int: ...

    async def close(self) -> None: ...


@runtime_checkable
class DocumentStore(Protocol):
    async def put(self, path: str, data: bytes, content_type: str) -> str: ...

    async def get(self, path: str) -> bytes: ...

    async def delete(self, path: str) -> None: ...

    async def signed_url(self, path: str, ttl_seconds: int = 900) -> str: ...


@runtime_checkable
class EmbeddingProvider(Protocol):
    @property
    def model_id(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...


@runtime_checkable
class SparseEncoder(Protocol):
    async def encode_documents(self, texts: Sequence[str]) -> list[SparseVector]: ...

    async def encode_query(self, text: str) -> SparseVector: ...


@runtime_checkable
class RerankProvider(Protocol):
    async def rerank(
        self, query: str, documents: Sequence[RetrievedChunk], top_n: int
    ) -> list[RetrievedChunk]: ...


@runtime_checkable
class WebSearchProvider(Protocol):
    """Live web search, used to supplement indexed evidence when an agent has the
    web-search tool enabled. Failure returns an empty list — the answer degrades
    to document-only, it does not error."""

    async def search(self, query: str, max_results: int = 5) -> list[WebResult]: ...


@runtime_checkable
class DocumentParser(Protocol):
    """Turns raw bytes into markdown pages. One implementation per format."""

    supported_mimes: tuple[str, ...]

    async def parse(self, data: bytes, filename: str) -> Any: ...


@dataclass(slots=True)
class OCRResult:
    """One page of recovered text.

    `confidence` is the provider's own mean score where it reports one. Low
    confidence is surfaced as a warning on the page rather than dropped, because
    a partially recovered scan still answers some questions, and silently
    discarding it looks identical to a document with no content.
    """

    markdown: str
    confidence: float = 1.0
    warnings: list[str] = field(default_factory=list)


@runtime_checkable
class OCRProvider(Protocol):
    """Recovers text from a page raster. Used only for pages the classifier
    marked as escalation candidates — never for the whole document."""

    @property
    def name(self) -> str: ...

    async def extract(self, image: bytes, mime: str = "image/png") -> OCRResult: ...


@runtime_checkable
class LLMProvider(Protocol):
    """Chat completion, one implementation per vendor.

    A provider is constructed already bound to its model, temperature and token
    ceiling, so the per call overrides here are overrides and not required
    arguments. Everything after `messages` is keyword only: these are all
    optional adjustments and a positional call site would read as a puzzle.

    `tools` is the vendor neutral tool list, carried in the Anthropic shape —
    {"name", "description", "input_schema"} — because this platform is
    Anthropic first. Each adapter translates it into its own wire format. When
    it is None the outgoing request body is identical to one built before tools
    existed: absent means absent, never an empty list.
    """

    @property
    def provider_name(self) -> str: ...

    @property
    def model_id(self) -> str: ...

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tools: Sequence[dict[str, Any]] | None = None,
        api_key: str | None = None,
    ) -> Completion:
        """api_key overrides the platform key for this one call."""
        ...

    def stream(
        self,
        messages: Sequence[Message],
        *,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        """Text deltas only. Tool calls are not streamable through this port —
        a tool using turn goes through complete, where the call arrives whole."""
        ...


@runtime_checkable
class QueuePort(Protocol):
    async def enqueue(
        self, job_name: str, payload: dict[str, Any], delay_seconds: int = 0
    ) -> str: ...


@runtime_checkable
class CachePort(Protocol):
    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None: ...

    async def delete(self, key: str) -> None: ...

    async def incr(self, key: str, ttl_seconds: int | None = None) -> int: ...

    async def acquire_lease(self, key: str, owner_token: str, ttl_seconds: int) -> bool: ...

    async def release_lease(self, key: str, owner_token: str) -> bool: ...

    async def close(self) -> None: ...


@runtime_checkable
class Telemetry(Protocol):
    def start_span(self, name: str, **attributes: Any) -> Any: ...

    async def record_usage(
        self, tenant_id: UUID, kind: str, quantity: int, **meta: Any
    ) -> None: ...


@runtime_checkable
class ChunkRepository(Protocol):
    """Canonical chunk text lives in postgres, not the vector store."""

    async def bulk_insert(self, chunks: Sequence[Chunk]) -> int: ...

    async def get_many(self, tenant_id: UUID, chunk_ids: Sequence[str]) -> list[Chunk]: ...

    async def delete_by_document(self, tenant_id: UUID, document_id: UUID) -> int: ...


@dataclass(slots=True)
class FetchResult:
    """One crawl fetch, after every redirect hop was validated.

    Deliberately not an exception-based contract: a crawl has to record "404"
    against a link rather than abort the wave, so a non-2xx arrives here as a
    status. `headers` is lower-cased key to value, which is what the conditional
    request ledger (etag, last-modified) and the content-type allowlist read.
    `redirect_chain` is every intermediate URL in order, so a redirect that left
    the pinned origin can be reported as the reason it was refused instead of
    looking like a mysterious failure.
    """

    final_url: str
    status: int
    headers: dict[str, str]
    body: bytes
    redirect_chain: list[str] = field(default_factory=list)


@runtime_checkable
class PageFetcher(Protocol):
    """Fetches one crawl target over HTTP with the SSRF discipline applied.

    An implementation MUST follow redirects manually and validate every hop
    (`assert_fetchable` before the request, `assert_peer_public` on the open
    response before a byte of body is read), and MUST enforce the byte cap
    during the read rather than after it — a 5 KB response that inflates to 2 GB
    has to abort at the cap. Single-URL ingest keeps its own fetcher; this port
    exists because the crawler additionally needs conditional-request headers
    out, response headers back, and a non-raising status contract.
    """

    async def fetch(
        self,
        url: str,
        *,
        if_none_match: str | None = None,
        if_modified_since: str | None = None,
        max_bytes: int | None = None,
        accept: str = "text/html,application/xhtml+xml,text/plain;q=0.8,*/*;q=0.1",
    ) -> FetchResult: ...


@dataclass(slots=True)
class RenderResult:
    """HTML as a browser saw it, for a page whose text is client rendered."""

    html: str
    final_url: str
    status: int = 200


@runtime_checkable
class PageRenderer(Protocol):
    """Optional hosted browser rendering for JavaScript-only pages.

    Off by default. We do not run Chromium in-process: a headless
    browser peaks at several hundred MB RSS and adds cold-start weight to every
    wake, including ordinary chat requests. An adapter POSTs the URL to a hosted
    "return the rendered HTML" endpoint over plain httpx and gets HTML back.
    Unconfigured, the container yields None and the crawler degrades to the
    sitemap plus a loud warning, exactly as reranking and web search do.
    """

    @property
    def name(self) -> str: ...

    async def render(self, url: str, *, timeout_seconds: float | None = None) -> RenderResult:
        """Raise on transport failure. The caller records the page as
        javascript_rendered rather than retrying indefinitely."""
        ...


@runtime_checkable
class ActionDispatcher(Protocol):
    """Runs one tool call, whatever kind of action is behind it.

    The agent loop depends on this and not on the concrete executor, for one
    reason: a tenant action can be an outbound HTTP call, a built-in handler, a
    job handed to the visitor's browser, or a card the widget renders, and the
    loop must not branch on which. Everything the four paths differ in —
    the SSRF envelope, the circuit breaker, the ledger row, the durable pause —
    lives behind `dispatch`.

    `dispatch` never raises for an action that failed. A refused, blocked, timed
    out or quota-exhausted call comes back as an `ActionOutcome` with `ok=False`,
    because the model has to read it and tell the visitor something true. An
    exception here would abort the turn instead, losing the answer the model had
    already written.

    The signature carries no action id on purpose: the loop knows tool names, not
    database rows, and the implementation looks the id up itself.
    """

    async def dispatch(
        self,
        *,
        action: ActionDefinition,
        arguments: dict[str, Any],
        ctx: ActionContext,
    ) -> ActionOutcome: ...


@dataclass(slots=True)
class NotifyReceipt:
    """One notifier's outcome. A failure is data, not an exception."""

    channel: str
    provider_message_id: str | None = None
    delivered: bool = False
    error: str | None = None


@runtime_checkable
class EscalationNotifier(Protocol):
    """Tells a human that the agent could not finish.

    One notifier per destination kind. Failure is reported, never raised past the
    service: a Slack outage must not stop the email, and neither must fail the
    user's answer.
    """

    @property
    def name(self) -> str: ...

    async def notify(self, event: EscalationEvent) -> NotifyReceipt: ...

    async def close(self) -> None: ...


@dataclass(slots=True)
class OutboundEmail:
    """One message handed to the provider.

    ``headers`` carries the loop-suppression set that keeps our bot from talking
    to somebody else's bot (Auto-Submitted, X-Auto-Response-Suppress, X-CA-Loop)
    plus the threading trio (Message-ID, In-Reply-To, References). All of that is
    the adapter's business to transmit and the renderer's business to decide.
    """

    to: tuple[str, ...]
    subject: str
    text: str
    html: str | None = None
    from_address: str = ""
    from_name: str = ""
    reply_to: tuple[str, ...] = ()
    cc: tuple[str, ...] = ()
    headers: dict[str, str] = field(default_factory=dict)
    idempotency_key: str | None = None
    tags: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class ReceivedEmail:
    """One inbound message, already parsed by the provider.

    ``received_for`` is the routing key and comes from the ``for`` clause of the
    Received headers — under a forwarding rule ``to`` is still the tenant's own
    support address, so ``to`` alone cannot resolve the channel.
    """

    provider_id: str
    message_id: str
    from_address: str
    subject: str
    in_reply_to: str | None = None
    references: tuple[str, ...] = ()
    from_name: str = ""
    to: tuple[str, ...] = ()
    cc: tuple[str, ...] = ()
    received_for: tuple[str, ...] = ()
    text: str | None = None
    html: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    attachments: tuple[EmailAttachmentRef, ...] = ()
    received_at: datetime | None = None
    spam_score: float | None = None


@runtime_checkable
class EmailProvider(Protocol):
    """Transactional email, both directions. One adapter per vendor."""

    @property
    def name(self) -> str: ...

    async def send(self, message: OutboundEmail) -> str:
        """Returns the provider message id. Raises the ProviderError family."""
        ...

    async def fetch_received(self, provider_id: str) -> ReceivedEmail:
        """Pull the parsed body for an inbound event.

        Separate from the webhook because the webhook carries metadata only —
        which is what keeps a 25 MB attachment out of a scale-to-zero request
        body.
        """
        ...

    async def fetch_attachment(self, provider_id: str, attachment_id: str) -> bytes: ...

    async def close(self) -> None: ...


@dataclass(slots=True)
class ApprovalRequest:
    """One decision put in front of the owner.

    Deliberately not an ``EscalationEvent``. An escalation says "the agent could
    not finish"; this says "the agent is about to spend money or contact a
    stranger, and a human said they wanted to be asked first". The two differ in
    what the human does next — read, versus decide — and so in what a notifier
    has to render: an approval carries a deadline and a pair of actions, and an
    escalation carries neither.

    ``items`` is a short, already-rendered preview of the batch, not the batch
    itself. A notifier must never be handed the payload to summarise: what the
    owner sees on Telegram and what they see in the panel have to be the same
    text, and that is only true if one place produced it.
    """

    tenant_id: UUID
    approval_id: UUID
    feature: str
    level: int
    summary: str
    items: tuple[str, ...] = ()
    expires_at: datetime | None = None
    approve_url: str | None = None
    reject_url: str | None = None


@runtime_checkable
class ApprovalNotifier(Protocol):
    """Asks a human to approve or reject, on one destination.

    Mirrors ``EscalationNotifier`` exactly, including the rule that failure is
    data: a Telegram outage must leave the WhatsApp ping and the web panel
    working, and must never fail the job that requested the approval. The batch
    is held either way — an approval nobody received simply expires, which is the
    safe direction.
    """

    @property
    def name(self) -> str: ...

    async def request(self, approval: ApprovalRequest) -> NotifyReceipt: ...

    async def close(self) -> None: ...


@dataclass(slots=True)
class LeadCandidate:
    """One company a lead-data provider or a web search turned up.

    Deliberately thin, and deliberately not a ``growth_leads`` row. A provider
    returns what it happens to know — Apollo has employee counts, a search
    result has a snippet, a directory has neither — and the service is what
    turns any of those into the tenant's own lead record via ``lead_uid`` and
    ``dedupe_lead``. Letting an adapter build the row would put the dedup rules
    in three places.

    ``raw`` carries the provider's untouched payload so a summary that reads
    oddly can be traced back to what it was written from without re-paying for
    the lookup.
    """

    company: str = ""
    domain: str = ""
    person: str = ""
    role: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    size: str = ""
    description: str = ""
    source: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class LeadDataProvider(Protocol):
    """A paid B2B contact database. One adapter per vendor.

    Every method here costs the tenant a credit, so every implementation writes
    a ``usage_events`` row through the caller — the adapter reports how many
    credits it burned and the service meters it, rather than each adapter
    reaching for the ledger and three of them getting it subtly different.

    Failure returns empty rather than raising: web-only research is the base
    case and a provider outage must degrade to it, not stop the run. The
    container yields ``None`` for an unconfigured vendor, which is the same
    graceful-off posture as the reranker and web search.
    """

    @property
    def name(self) -> str: ...

    async def search_companies(
        self, query: str, *, limit: int = 25, location: str = "", industry: str = ""
    ) -> list[LeadCandidate]:
        """Companies matching an ICP description. Empty on any failure."""
        ...

    async def enrich_contact(
        self, *, domain: str, company: str = "", role: str = ""
    ) -> LeadCandidate | None:
        """A named human at that company, or None when the vendor has nobody."""
        ...

    async def close(self) -> None: ...


@dataclass(slots=True)
class InboundLead:
    """One enquiry an inbound source handed us, before it is a lead row.

    The inbound counterpart of ``LeadCandidate``: thin, vendor-shaped, and
    deliberately not a ``growth_leads`` row — the shared ``ingest`` tail owns
    the row. ``external_id`` is the vendor's own idempotency handle and becomes
    the ``growth_lead_raw`` dedupe key, so a re-pulled window creates no second
    lead.
    """

    external_id: str
    name: str = ""
    company: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    message: str = ""
    product: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class InboundLeadSource(Protocol):
    """A marketplace a tenant sells on, read for its enquiry feed. One per vendor.

    These are pull sources, not webhooks: there is nothing to verify a
    signature on, so the tenant's API key is the credential and the service
    calls ``fetch_window`` on a schedule and on demand. A transport failure
    raises ``ProviderUnavailableError`` rather than returning empty — an empty
    window is a normal result (no enquiries) and the two must not look alike,
    because the caller records the failure on the integration card and the
    next pull re-covers the window through the dedupe key.
    """

    @property
    def name(self) -> str: ...

    async def fetch_window(
        self, start: datetime, end: datetime
    ) -> list[InboundLead]:
        """Enquiries received in ``[start, end)``.

        Empty when the window genuinely held none; raises
        ``ProviderUnavailableError`` on a transport failure.
        """
        ...

    async def close(self) -> None: ...


@dataclass(slots=True)
class VerificationResult:
    """What an email verifier concluded about one address.

    ``status`` is normalised to three words on purpose. Vendors report a dozen
    sub-states ("catch_all", "role_based", "greylisted", "unknown") and the only
    decision the sender makes is send / do not send / send at your own risk, so
    the adapter collapses the vocabulary and keeps the vendor's own word in
    ``detail`` for the panel and for support.
    """

    address: str
    status: str  # "deliverable" | "undeliverable" | "risky"
    detail: str = ""

    @property
    def sendable(self) -> bool:
        """Risky counts as sendable; undeliverable does not.

        A catch-all domain is the most common "risky" verdict and refusing to
        mail those would drop a large share of real Indian SME addresses. A hard
        bounce is what actually costs reputation, and that is ``undeliverable``.
        """
        return self.status != "undeliverable"


@runtime_checkable
class EmailVerifier(Protocol):
    """Checks whether an address will accept mail, before the first send.

    One adapter per vendor behind one port: the vendors are interchangeable and
    the switching cost should be one container line, not a rewrite of the
    research agent.
    """

    @property
    def name(self) -> str: ...

    async def verify(self, email: str) -> VerificationResult:
        """Never raises. An unreachable verifier returns ``risky``, because
        refusing to send on a verifier outage would silently stop outbound for
        every tenant while looking like a quiet day."""
        ...

    async def close(self) -> None: ...


@dataclass(slots=True)
class StoreCustomer:
    """One buyer, in provider-neutral shape.

    The service turns these into ``growth_customers`` rows via the same
    normalisation the leads use — the adapter reports what the store said and
    the dedup rules stay in one place.
    """

    external_id: str = ""
    name: str = ""
    phone: str = ""
    email: str = ""
    tags: list[str] = field(default_factory=list)
    consent: dict[str, Any] = field(default_factory=dict)
    language: str | None = None
    first_order_at: datetime | None = None
    last_order_at: datetime | None = None
    total_spend_micros: int = 0
    order_count: int = 0
    updated_at: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class StoreOrder:
    """One order, in provider-neutral shape."""

    external_id: str = ""
    customer_external_id: str = ""
    customer_email: str = ""
    customer_phone: str = ""
    total_micros: int = 0
    payment: str = "prepaid"  # "prepaid" | "cod"
    status: str = ""
    items: dict[str, Any] = field(default_factory=dict)
    ordered_at: datetime | None = None
    updated_at: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CustomerPage:
    """One incremental page of buyers. ``next_cursor`` empty means done."""

    customers: list[StoreCustomer] = field(default_factory=list)
    next_cursor: str = ""


@dataclass(slots=True)
class OrderPage:
    """One incremental page of orders. ``next_cursor`` empty means done."""

    orders: list[StoreOrder] = field(default_factory=list)
    next_cursor: str = ""


@dataclass(slots=True)
class DiscountReceipt:
    """A per-campaign discount code created on the store."""

    code: str = ""
    external_id: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class StoreProvider(Protocol):
    """A D2C storefront. One adapter per vendor (Shopify, WooCommerce).

    Every method costs the tenant nothing directly but every *sync run* writes
    a ``usage_events`` row through the caller — the adapter reports what it
    fetched and the service meters it, the same split the lead-data providers
    use. Failure raises: the sync job catches per-store, marks the integration
    ``error``, and continues with the next store — a down store must not stop
    the other one syncing.

    The container yields ``None`` for a store the tenant has not connected,
    the same graceful-off posture as the reranker and web search.
    """

    @property
    def name(self) -> str: ...

    async def sync_customers(
        self, *, cursor: str = "", limit: int = 100
    ) -> CustomerPage:
        """One incremental page of buyers after ``cursor``.

        ``cursor`` is opaque to the caller — the adapter's own ``updated_at``
        bookmark, persisted on the integration row's ``health``. Empty cursor
        means "from the beginning".
        """
        ...

    async def sync_orders(
        self, *, cursor: str = "", limit: int = 100
    ) -> OrderPage:
        """One incremental page of orders after ``cursor``. Same bookmark
        discipline as ``sync_customers``, tracked separately."""
        ...

    async def create_discount(
        self, *, code: str, percent: int, name: str = ""
    ) -> DiscountReceipt:
        """Create a unique per-campaign percentage code. Raises on refusal."""
        ...

    async def close(self) -> None: ...


@dataclass(slots=True)
class SendReceipt:
    """What one customer-facing send actually cost and where it landed.

    ``cost_micros`` is the denormalised mirror written onto ``growth_messages``
    for per-thread display; the authoritative ledger row is written separately
    into ``usage_events``. Both come from here so the two can never disagree
    about a single send.
    """

    provider_id: str = ""
    cost_micros: int = 0
    usage_kind: str = ""
    template_id: str | None = None


@runtime_checkable
class ChannelSender(Protocol):
    """Delivers one queued growth message on one customer-facing channel.

    The abstraction exists so SMS can slot in later behind the same port (RCS is
    on the never-build list) — ``growth_messages.channel`` is the only thing that
    grows, and its CHECK admits ``email | whatsapp`` today, so a new channel is
    one column constraint away, not zero. It is not a general "send a message"
    port: every implementation is required to check the suppression gate and the
    consent record itself, so that a future channel cannot be added without a
    compliance check simply by forgetting one.
    """

    @property
    def channel(self) -> str: ...

    async def send(self, tenant_id: UUID, message: Mapping[str, Any]) -> SendReceipt:
        """Send one ``growth_messages`` row. Raises on a refused send; the
        dispatcher marks that one row failed and continues with the batch."""
        ...


@runtime_checkable
class RunContextStore(Protocol):
    """The swarm's per-day working memory, keyed by the tenant's calendar day.

    Date-keyed, not run-id-keyed, because no run exists to key on: the growth
    platform is a set of independent cron tasks, each enqueuing queue jobs
    whose work happens later in a worker, and an id minted in one cron never
    reaches the others. Every job that runs for a tenant on a day appends to
    that day; the report agent reads the whole day before composing. The key
    is computed from the profile timezone by the *caller* — the store stays a
    dumb append/read surface with no settings dependency.

    Append-only by contract: an agent revising its own finding appends a
    superseding entry and consumers take the latest per kind. The only delete
    is the age sweep, an interval cutoff driven by the rollup cron — never an
    end-of-run delete, which in a fan-out architecture erases rows before the
    jobs that would write them have run.
    """

    async def append(
        self,
        tenant_id: UUID,
        *,
        local_date: Any,
        agent_key: str,
        kind: str,
        payload: Any,
        run_id: str = "",
    ) -> dict[str, Any]:
        """Add one entry to the tenant's day. Returns the stored row."""
        ...

    async def read_day(
        self, tenant_id: UUID, local_date: Any
    ) -> dict[str, dict[str, list[dict[str, Any]]]]:
        """The day's entries grouped agent_key -> kind -> list, oldest first."""
        ...

    async def latest(
        self, tenant_id: UUID, local_date: Any, agent_key: str, kind: str
    ) -> dict[str, Any] | None:
        """The newest entry one agent left for one kind that day, or None."""
        ...

    async def sweep_old(self, *, days: int = 7) -> dict[str, int]:
        """Delete entries older than the cutoff. Returns {"tenants", "deleted"}."""
        ...


@dataclass(slots=True)
class CrmContact:
    """A lead flattened into the fields every CRM has.

    Deliberately the intersection, not the union: company, person, email, phone,
    website, a one-paragraph summary, where it came from and where it stands.
    Anything a particular CRM wants beyond this is the adapter's own business,
    built from ``extra`` and its own field map, because the moment this dataclass
    grows a Zoho-shaped field the port stops being a port.
    """

    lead_id: str = ""
    company: str = ""
    person: str = ""
    role: str = ""
    email: str = ""
    phone: str = ""
    website: str = ""
    summary: str = ""
    source: str = ""
    status: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CrmActivity:
    """One outreach event to record against a contact, as a note.

    Notes, not the CRM's own typed activity objects: every CRM models tasks,
    calls and meetings differently and half of them need an owner id we do not
    have, whereas every CRM has a free-text note attached to a contact. A note
    lands the same way in all of them and cannot half-fail on a required field
    the tenant added to their task layout.
    """

    external_id: str = ""
    title: str = ""
    body: str = ""
    occurred_at: datetime | None = None


@dataclass(slots=True)
class CrmReceipt:
    """What one upsert did. ``external_id`` is the CRM's id for the contact."""

    external_id: str = ""
    created: bool = False
    detail: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class CrmProvider(Protocol):
    """A tenant's CRM, as a push destination. One adapter per vendor.

    This port is write-only by design and has no ``fetch`` of any kind. Nothing
    in the product may read a CRM to decide anything — no lead created from a
    CRM record, no status taken from one — so the port offers no way to, and a
    later "just read the owner field" cannot be added without adding a method
    and being seen doing it.

    ``upsert_contact`` is required to be idempotent given an ``external_id``:
    the caller stores the id it returns and hands it back next time, so the
    adapter never has to search the CRM to find out whether it has met this
    person before. A search-to-dedupe would be exactly the read dependency this
    port exists to forbid, and it creates a duplicate the first time it is rate
    limited.

    Failure raises. The push job catches per lead, marks that link row
    ``error`` with the reason, and moves on: one lead the tenant's CRM refuses
    must not stop the other leads syncing, and must never block the outreach
    pipeline that produced them.
    """

    @property
    def name(self) -> str: ...

    async def upsert_contact(
        self, contact: CrmContact, *, external_id: str = ""
    ) -> CrmReceipt:
        """Create or update one contact. Empty ``external_id`` means create."""
        ...

    async def log_activity(self, activity: CrmActivity) -> None:
        """Attach one note to an existing contact. Raises on refusal."""
        ...

    async def health(self) -> dict[str, Any]:
        """A cheap authenticated call, for the integration card.

        Returns whatever identifies the connected account. Raises on a revoked
        token or a missing scope — which is the point: the card has to be able
        to say *why* it is broken, and a boolean cannot.
        """
        ...

    async def close(self) -> None: ...
