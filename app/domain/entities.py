"""Domain entities. Pure data, no infrastructure imports.

These are the shapes services pass around. Database models and API schemas are
separate and map onto these.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from app.core.constants import (
    Channel,
    ChunkKind,
    DocumentStatus,
    JobState,
    JobStep,
    MessageRole,
    ParseTier,
)


@dataclass(slots=True)
class Tenant:
    id: UUID
    name: str
    slug: str
    status: str = "active"

    @property
    def is_active(self) -> bool:
        return self.status == "active"


@dataclass(slots=True)
class Document:
    id: UUID
    tenant_id: UUID
    filename: str
    mime: str
    size_bytes: int
    sha256: str
    storage_path: str
    status: DocumentStatus = DocumentStatus.QUEUED
    agent_id: UUID | None = None
    page_count: int = 0
    chunk_count: int = 0
    token_count: int = 0
    error: str | None = None
    tier_counts: dict[str, int] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
    indexed_at: datetime | None = None


@dataclass(slots=True)
class ParsedPage:
    """One page normalised to markdown, with the tier that produced it.

    `image` holds a raster of the page, populated only when the page classifier
    decided this page needs a paid tier. Rendering every page would waste memory
    on documents that never escalate, so it stays None on the free path.
    """

    page_number: int
    markdown: str
    tier: ParseTier
    degraded: bool = False
    warnings: list[str] = field(default_factory=list)
    image: bytes | None = None
    image_mime: str = "image/png"
    needs_escalation: bool = False


@dataclass(slots=True)
class ParsedDocument:
    pages: list[ParsedPage]
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def markdown(self) -> str:
        return "\n\n".join(page.markdown for page in self.pages)

    def tier_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for page in self.pages:
            counts[page.tier.value] = counts.get(page.tier.value, 0) + 1
        return counts


@dataclass(slots=True)
class Chunk:
    """A unit of retrieval. Postgres holds the canonical text, the vector store
    holds embeddings plus a pointer back here."""

    id: str
    tenant_id: UUID
    document_id: UUID
    ordinal: int
    kind: ChunkKind
    text: str
    parent_text: str | None = None
    parent_id: str | None = None
    heading_path: list[str] = field(default_factory=list)
    page_from: int = 0
    page_to: int = 0
    token_count: int = 0
    # what actually gets embedded. normally the chunk text; when contextual
    # retrieval is on it is a short document-situating prefix plus the text, so a
    # chunk that reads "the limit is 30 days" is findable by "refund window".
    # the canonical `text` is unchanged, so this never affects what the model reads
    embed_text: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def text_to_embed(self) -> str:
        return self.embed_text or self.text

    @property
    def context_text(self) -> str:
        """Text the model should see. Parent when present, else the chunk itself."""
        return self.parent_text or self.text


@dataclass(slots=True)
class RetrievedChunk:
    chunk_id: str
    document_id: UUID
    score: float
    text: str
    parent_text: str | None = None
    heading_path: list[str] = field(default_factory=list)
    page_from: int = 0
    page_to: int = 0
    filename: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def context_text(self) -> str:
        return self.parent_text or self.text


@dataclass(slots=True)
class Citation:
    chunk_id: str
    document_id: UUID
    filename: str
    page_from: int
    page_to: int
    heading_path: list[str] = field(default_factory=list)
    # set for web-search sources: a citation the UI opens directly rather than
    # minting a signed document URL. None for indexed-document citations.
    url: str | None = None


@dataclass(slots=True)
class WebResult:
    """One web-search hit, normalised across search providers."""

    title: str
    url: str
    content: str
    score: float = 0.0


@dataclass(slots=True)
class Message:
    id: UUID
    tenant_id: UUID
    conversation_id: UUID
    role: MessageRole
    content: str
    citations: list[Citation] = field(default_factory=list)
    confidence: float | None = None
    latency_ms: int | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    model: str | None = None
    created_at: datetime | None = None


@dataclass(slots=True)
class Conversation:
    id: UUID
    tenant_id: UUID
    agent_id: UUID
    channel: Channel
    external_user_ref: str | None = None
    summary: str | None = None
    turn_count: int = 0
    started_at: datetime | None = None
    last_activity: datetime | None = None

    @property
    def thread_id(self) -> str:
        """LangGraph checkpointer key. Tenant is inside the key so a conversation
        id guessed from another tenant cannot resolve to a thread."""
        return f"{self.tenant_id}:{self.id}"


@dataclass(slots=True)
class DocumentJob:
    id: UUID
    tenant_id: UUID
    document_id: UUID
    step: JobStep
    state: JobState = JobState.PENDING
    attempt: int = 0
    cursor: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass(slots=True)
class TenantContext:
    """Resolved per request in middleware. Everything downstream reads this."""

    tenant_id: UUID
    tenant_slug: str
    channel: Channel
    agent_id: UUID | None = None
    user_id: UUID | None = None
    role: str | None = None
    api_key_id: UUID | None = None
    external_user_ref: str | None = None
    # Verified identity, established by an identity-verification flow and carried
    # on the context. `identity_verified` is the only field downstream may treat
    # as proof of who someone is; a `contact_id` on its own means a contact row
    # was resolved, never that the person proved they own it.
    contact_id: UUID | None = None
    identity_verified: bool = False
    # hash_identifier(sub, tenant_salt) — the pseudonym that keeps one signed-in
    # person on one thread across devices. Never the raw subject.
    sub_hash: str | None = None
