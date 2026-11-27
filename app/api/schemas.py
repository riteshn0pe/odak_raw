"""Request/response schemas.

Deliberately separate from `domain.entities`. Domain types describe what the
system knows; these describe what crosses the wire. Coupling them would mean an
internal refactor becomes a breaking API change, and every internal field becomes
accidentally public.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=8000)
    conversation_id: UUID | None = None
    agent_id: UUID | None = None
    # scope the answer to specific documents. useful when a tenant's corpus spans
    # several products and the caller already knows which one applies
    document_ids: list[UUID] | None = None


class CitationOut(BaseModel):
    chunk_id: str
    document_id: UUID
    filename: str
    page_from: int
    page_to: int
    heading_path: list[str] = Field(default_factory=list)
    # set for web-search sources; the UI opens it directly instead of minting a
    # signed document URL
    url: str | None = None


class ChatResponse(BaseModel):
    answer: str
    citations: list[CitationOut] = Field(default_factory=list)
    confidence: float
    grounded: bool = True
    conversation_id: UUID | None = None
    message_id: UUID | None = None
    latency_ms: int = 0
    model: str = ""
    # set when the answer was refused or gated, so a client can distinguish
    # "no answer" from "declined to answer"
    reason: str = ""
    # true when the turn was refused by a guardrail (injection, blocklist,
    # crisis, intent reject). The SSE path carries this as the frame type; the
    # JSON path needs it as a field or a non-streaming client cannot tell a
    # refusal from an answer without parsing `reason`.
    blocked: bool = False


class SearchRequest(BaseModel):
    """Retrieval without generation.

    Exposed on purpose: some tenants want our retrieval feeding their own model,
    and it is cheap to serve.
    """

    query: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=10, ge=1, le=100)
    document_ids: list[UUID] | None = None


class SearchHitOut(BaseModel):
    chunk_id: str
    document_id: UUID
    filename: str
    text: str
    score: float
    page_from: int
    page_to: int
    heading_path: list[str] = Field(default_factory=list)


class SearchResponse(BaseModel):
    hits: list[SearchHitOut] = Field(default_factory=list)
    confidence: float = 0.0
    strategy: str = ""


class DocumentOut(BaseModel):
    id: UUID
    filename: str
    mime: str
    size_bytes: int
    status: str
    error: str | None = None
    page_count: int = 0
    chunk_count: int = 0
    # per-tier page counts. the tenant can see how much of a document needed
    # paid OCR versus the free text path
    tier_counts: dict[str, int] = Field(default_factory=dict)
    # retrieval facets. free-form labels, an optional grouping name, and a
    # per-document bag — any of which can scope retrieval to a subset of the corpus
    tags: list[str] = Field(default_factory=list)
    collection: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None
    indexed_at: datetime | None = None


class UploadResponse(BaseModel):
    document: DocumentOut
    # true when the file was already present. the upload is accepted either way,
    # so a client does not have to treat a duplicate as an error
    duplicate: bool = False


class ConversationOut(BaseModel):
    id: UUID
    agent_id: UUID
    channel: str
    external_user_ref: str | None = None
    turn_count: int
    started_at: datetime | None = None
    last_activity: datetime | None = None


class MessageOut(BaseModel):
    id: UUID
    role: str
    content: str
    citations: list[dict[str, Any]] = Field(default_factory=list)
    confidence: float | None = None
    model: str | None = None
    feedback: str | None = None
    created_at: datetime | None = None


class FeedbackRequest(BaseModel):
    message_id: UUID
    rating: Literal["up", "down"]


class ErrorOut(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
