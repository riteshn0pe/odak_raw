"""Source endpoints: crawled websites, sitemaps, link lists, snippets, Q&A sets.

A *source* is one logical thing a tenant added. `documents` stays the unit of
ingestion, chunking, indexing, retrieval and citation, so a crawled page is an
ordinary documents row whose `source_id` points at its parent. Nothing in
DocumentPipeline, Indexer or RetrievalService changes.

Two disciplines run through the whole module:

  * **Counters are recomputed, never incremented.** QStash redelivers, and an
    increment would make a redelivered wave double-count — after which the
    dashboard lies about a paying tenant's page count. Every write that can move
    a link's state ends with `_recompute_counters`.
  * **A URL the tenant supplied is checked before a packet leaves the process.**
    `assert_fetchable` runs synchronously inside the create and add-links
    handlers, so a tenant pointing the crawler at `169.254.169.254` gets a 422
    instead of a queued job.

Raw `text()` SQL under `tenant_scope`, matching `agents.py` and `keys.py`: RLS is
the isolation mechanism, so every statement here runs inside a session whose
`current_tenant_id()` is already set.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, File, Form, Query, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.postgres.source_repo import REVIEWABLE_STATES
from app.api.deps import ContainerDep, TenantDep
from app.core.constants import DocumentStatus
from app.core.db import tenant_scope
from app.core.errors import (
    ConflictError,
    NotFoundError,
    PayloadTooLargeError,
    ValidationError,
)
from app.core.logging import get_logger
from app.core.urlguard import assert_fetchable
from app.domain import crawl
from app.domain.entities import TenantContext
from app.services import audit

logger = get_logger(__name__)

router = APIRouter(prefix="/sources", tags=["sources"])

# ---------------------------------------------------------------- module bounds

# a crawl costs real money per page, so the rate of arrival is bounded as well as
# the total. mirrors documents.py's UPLOADS_PER_HOUR reasoning
SOURCE_CREATE_PER_HOUR = 30
# a retrain is a whole crawl, so its ceiling is per hour, not per minute, and it
# lives in Settings (SOURCE_RETRAIN_PER_HOUR) because an operator watching crawl
# spend needs to move it without a deploy

# the review screen's first paint carries one page of each group; the full lists
# come from GET /sources/{id}/links
LINK_GROUP_CAP = 25
LINKS_MAX_LIMIT = 200
SOURCES_MAX_LIMIT = 100
BULK_MAX_LINKS = 500
# a bulk exclude fans out to one vector delete per page. bounded so 500 pages do
# not open 500 simultaneous connections to the vector store
BULK_VECTOR_CONCURRENCY = 8

# The approval screen builds its whole tree in one request: a tree paged halfway
# is a tree whose counts are wrong, and wrong counts are what someone would
# approve. 5000 URLs of five short fields is about a megabyte, which is a lot for
# one response and still less than the cost of indexing a frontier nobody read.
# Past this the response says so and the screen shows what it has.
REVIEW_URL_CAP = 5000

# The tree answers one question — "what gets indexed if I say yes" — so it also
# carries pages the crawl has found but not read yet: they are in the frontier and
# approval indexes them too. Including them is what lets the list fill while the
# crawl is still running rather than only after the last page is fetched, which is
# the difference between a list that appears in a second and a progress bar.
REVIEW_LIST_STATES = (*REVIEWABLE_STATES, "pending", "fetching")

# an xlsx is a zip, so the bomb check is not optional (gate.py:75)
QA_IMPORT_MAX_BYTES = 2 * 1024 * 1024
QA_IMPORT_MAX_ROWS = 100
QA_MAX_VARIATIONS = 20
QA_MAX_QUESTION_CHARS = 500
QA_MAX_ANSWER_CHARS = 8000

SNIPPET_MIME = "text/markdown"
QA_MIME = "application/vnd.contextagent.qa+json"

# non-terminal crawl states: a source in one of these has a wave in flight, so a
# retrain is a 409 and a cancel is meaningful
LIVE_CRAWL_STATES = ("discovering", "fetching", "extracting")

# `awaiting_review` has no wave in flight — that is the point of it — but it is
# still an unfinished crawl the tenant must be able to abandon, so cancel accepts
# it. Retrain accepts it by omission: it is not in LIVE_CRAWL_STATES, so a parked
# source can be re-crawled from scratch instead of approved.
REVIEW_CRAWL_STATE = "awaiting_review"
CANCELLABLE_CRAWL_STATES = (*LIVE_CRAWL_STATES, REVIEW_CRAWL_STATE)

# Exclusion reasons a person is responsible for, as opposed to a rule the crawler
# applied. Restoring one of these undoes a decision, so it needs no `force_include`
# override; restoring a rule-driven exclusion does, or the next wave re-applies the
# rule. `crawl.REASON_REVIEW_EXCLUDED` is the review gate's; 'manual' predates the
# domain's reason vocabulary and is written by the two exclude endpoints.
HUMAN_EXCLUSION_REASONS = ("manual", crawl.REASON_REVIEW_EXCLUDED)

# the review UI's three groups, mapped onto the link states that belong to each.
# "Excluded means we chose not to, Failed means we could not" — so a 404 and a
# page that needs JavaScript both land under not_found, where the tenant can act
LINK_GROUPS: dict[str, tuple[str, ...]] = {
    "trained": ("trained", "unchanged"),
    "not_found": ("failed", "gone"),
    "excluded": ("excluded",),
    "pending": ("pending", "fetching", "fetched"),
}

# the per-source retrain cadence is clamped on write, platform-wide, so a source
# cannot be left retraining hourly
RETRAIN_MIN_HOURS = 168

# "times asked" is aggregated from messages.citations. bounded by the retention
# window so the containment scan stays an index scan
ACTIVITY_MAX_WINDOW_DAYS = 400


# ---------------------------------------------------------------------- models


class CrawlConfigIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    include_paths: list[str] = Field(default_factory=list, max_length=20)
    exclude_paths: list[str] = Field(default_factory=list, max_length=20)
    max_pages: int | None = Field(default=None, ge=1, le=10000)
    # None means "use the deployment's default depth" (CRAWL_MAX_DEPTH), the same
    # way max_pages defers to the plan ceiling. The bounds here are the hard ones a
    # tenant may never cross; the default behind them is the operator's to set
    max_depth: int | None = Field(default=None, ge=1, le=10)
    include_subdomains: bool = False
    include_pdf: bool = False
    # Park the crawl in `awaiting_review` once the frontier is fetched and let a
    # human approve the URL list before anything is trained. Off by default on
    # purpose: every source created before this field existed, and every API
    # caller that does not send it, keeps running straight through to `ready`.
    review_before_index: bool = False


class SourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    mode: Literal["crawl", "sitemap", "links"]
    # crawl: the homepage. sitemap: the .xml. links: ignored
    url: str | None = Field(default=None, max_length=2048)
    # links mode only
    urls: list[str] = Field(default_factory=list, max_length=100)
    title: str | None = Field(default=None, max_length=200)
    config: CrawlConfigIn = Field(default_factory=CrawlConfigIn)
    tags: list[str] = Field(default_factory=list, max_length=20)
    collection: str | None = Field(default=None, max_length=100)
    auto_retrain_hours: int = Field(default=0, ge=0, le=24 * 90)


class SourceUpdate(BaseModel):
    """Every field optional: a retag must not disturb the crawl config.

    Config edits are stored only. A changed exclude path takes effect on the next
    retrain, which is why the response carries `pending_changes`.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str | None = Field(default=None, max_length=200)
    config: CrawlConfigIn | None = None
    tags: list[str] | None = Field(default=None, max_length=20)
    collection: str | None = Field(default=None, max_length=100)
    auto_retrain_hours: int | None = Field(default=None, ge=0, le=24 * 90)


class SourceOut(BaseModel):
    id: UUID
    kind: str
    title: str
    seed_url: str | None
    origin: str | None
    crawl_state: str
    error: str | None
    link_total: int
    link_trained: int
    link_failed: int
    link_excluded: int
    link_pending: int
    size_bytes: int
    chunk_count: int
    tags: list[str]
    collection: str | None
    config: dict[str, Any]
    auto_retrain_hours: int
    pending_changes: bool
    crawl_started_at: datetime | None
    last_trained_at: datetime | None
    last_checked_at: datetime | None
    # When a human last approved this source's URL list. NULL with
    # `config.review_before_index` on means the next wave will park for review;
    # the flag itself stays on after approval so a retrain gates again.
    review_approved_at: datetime | None
    created_at: datetime | None


class SourceLinkOut(BaseModel):
    id: UUID
    url: str
    title: str | None
    state: str
    reason: str | None
    # rendered server side by crawl.reason_text, so the dashboard and every other
    # any future digest say the same thing about the same link
    reason_detail: str | None
    http_status: int | None
    depth: int
    discovered_via: str
    content_bytes: int
    document_id: UUID | None
    fetched_at: datetime | None


class SourceDetailOut(SourceOut):
    # first page of each group, so one request paints the review screen
    trained: list[SourceLinkOut]
    not_found: list[SourceLinkOut]
    excluded: list[SourceLinkOut]


class SourceLinkPage(BaseModel):
    items: list[SourceLinkOut]
    total: int
    # per-group totals for the tab strip, so switching tabs costs no request
    counts: dict[str, int]


class ReviewLinkOut(BaseModel):
    """One page in the approval list.

    Five fields, not `SourceLinkOut`: the review screen groups by URL and shows a
    title, and shipping http statuses, reasons and byte counts for thousands of
    rows to draw an accordion is bandwidth spent on nothing.
    """

    id: UUID
    url: str
    title: str | None
    depth: int
    state: str


class ReviewGroupOut(BaseModel):
    """One node of the URL tree: a host, or a path prefix under a host.

    `urls` holds only the pages that sit on this node itself, so `/blog` can be a
    page in its own right while `/blog/2024` is a child. `total` counts everything
    beneath the node, which is what the header shows and what the "remove this
    whole section" confirmation quotes.
    """

    key: str
    label: str
    total: int
    urls: list[str] = Field(default_factory=list)
    children: list[ReviewGroupOut] = Field(default_factory=list)


class SourceReviewOut(BaseModel):
    """The approval screen's one request.

    The tree carries URLs, not link rows, and `links` carries each row once. A
    page nested three levels deep would otherwise be serialised twice, and the
    client needs the link id anyway — that is what it sends to `POST /links/bulk`
    to drop a page.
    """

    crawl_state: str
    # false means this source was never asked to gate; the screen is then a preview
    # of what a gated crawl would ask about, with nothing to approve
    review_before_index: bool
    review_approved_at: datetime | None
    # how many pages are waiting, and how many were already dropped from this list
    total: int
    excluded: int
    # true when the frontier is larger than one screen's worth. The tree describes
    # the pages that were returned; approving still indexes everything.
    truncated: bool
    groups: list[ReviewGroupOut]
    links: list[ReviewLinkOut]


class CrawlProgressOut(BaseModel):
    crawl_state: str
    link_total: int
    link_trained: int
    link_failed: int
    link_excluded: int
    link_pending: int
    size_bytes: int
    error: str | None
    # the newest transitions, so the UI shows movement rather than a static bar
    recent: list[SourceLinkOut]
    # server-computed, so the copy is consistent everywhere
    message: str


class AddLinksIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    urls: list[str] = Field(min_length=1, max_length=100)


class BulkLinksIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    link_ids: list[UUID] = Field(min_length=1, max_length=BULK_MAX_LINKS)
    action: Literal["exclude", "restore"]
    # Both values mean "a person decided against this page", which is why neither
    # is undone by a retrain. They differ in when: `manual` is a page dropped from
    # a knowledge base that already had it, `review_excluded` is a page dropped at
    # the approval gate, before it was ever chunked or embedded. Kept apart so the
    # review list can say which happened. Ignored when action is "restore".
    reason: Literal["manual", "review_excluded"] = "manual"


class SnippetIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=200_000)
    tags: list[str] = Field(default_factory=list, max_length=20)
    collection: str | None = Field(default=None, max_length=100)


class SnippetDetailOut(BaseModel):
    source: SourceOut
    body: str


class QaPairIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str | None = Field(default=None, max_length=200)
    questions: list[str] = Field(min_length=1, max_length=QA_MAX_VARIATIONS)
    answer: str = Field(min_length=1, max_length=QA_MAX_ANSWER_CHARS)
    # saved-mode writes carry the row's position: reorder has to persist, and
    # qa_pairs.ordinal exists with qa_pairs_source_idx (source_id, ordinal)
    ordinal: int | None = Field(default=None, ge=0, le=100_000)


class QaSetCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str | None = Field(default=None, max_length=200)
    pairs: list[QaPairIn] = Field(min_length=1, max_length=200)
    tags: list[str] = Field(default_factory=list, max_length=20)
    collection: str | None = Field(default=None, max_length=100)


class QaPairOut(BaseModel):
    id: UUID
    document_id: UUID | None
    title: str
    questions: list[str]
    answer: str
    status: str
    error: str | None
    times_asked: int = 0
    last_asked: datetime | None = None


class QaImportResult(BaseModel):
    created: int
    errors: list[dict[str, Any]] = Field(default_factory=list)


class QaActivityPoint(BaseModel):
    day: str
    asked: int


class QaActivityOut(BaseModel):
    points: list[QaActivityPoint]
    times_asked: int
    last_asked: datetime | None


class SourcesSummaryOut(BaseModel):
    by_kind: dict[str, dict[str, int]]  # kind -> {sources, documents, bytes}
    documents: int
    storage_bytes: int
    needs_retrain: int  # sources with pending_changes
    crawling: int  # sources in a non-terminal crawl_state


# --------------------------------------------------------------------- helpers

# Every f-string SQL in this module carries an S608 suppression. It is safe by
# construction, not by assertion: the only values ever interpolated into
# query text are the module constants directly below, plus WHERE fragments and
# SET assignments assembled from literal strings in this file (see `clauses` in
# the list endpoints and `assignments` in PATCH). Every tenant-supplied value is
# a bound parameter — `:id`, `:q`, `:kind`, `:states` — so nothing a caller sends
# can reach the statement text. Adding a new f-string query here means checking
# that property still holds before adding the noqa, not the other way round.

SOURCE_COLUMNS = """
    id, kind, title, seed_url, origin, crawl_state, error,
    link_total, link_trained, link_failed, link_excluded, link_pending,
    size_bytes, chunk_count, tags, collection, config, auto_retrain_hours,
    pending_changes, crawl_started_at, last_trained_at, last_checked_at,
    review_approved_at, created_at
"""

LINK_COLUMNS = """
    id, url, title, state, reason, reason_detail, http_status,
    depth, discovered_via, content_bytes, document_id, fetched_at
"""

# A deleted source disappears from every read the moment DELETE returns, while
# its children are reaped by `delete_source` waves afterwards. 006 gives `sources`
# no soft-delete column, so the marker lives in `config`, which CrawlConfig
# ignores on load — see DELETE for why the row cannot simply be removed.
NOT_DELETED = "NOT COALESCE((config->>'deleted')::boolean, false)"


def _source_out(row: Any) -> SourceOut:
    """Project a `sources` row onto the wire schema.

    Explicit rather than `**row` so a new internal column — `boilerplate`, say —
    is never published by accident.
    """
    return SourceOut(
        id=row["id"],
        kind=row["kind"],
        title=row["title"],
        seed_url=row["seed_url"],
        origin=row["origin"],
        crawl_state=row["crawl_state"],
        error=row["error"],
        link_total=row["link_total"],
        link_trained=row["link_trained"],
        link_failed=row["link_failed"],
        link_excluded=row["link_excluded"],
        link_pending=row["link_pending"],
        size_bytes=row["size_bytes"],
        chunk_count=row["chunk_count"],
        tags=list(row["tags"] or []),
        collection=row["collection"],
        config=dict(row["config"] or {}),
        auto_retrain_hours=row["auto_retrain_hours"],
        pending_changes=row["pending_changes"],
        crawl_started_at=row["crawl_started_at"],
        last_trained_at=row["last_trained_at"],
        last_checked_at=row["last_checked_at"],
        review_approved_at=row["review_approved_at"],
        created_at=row["created_at"],
    )


def _link_out(row: Any) -> SourceLinkOut:
    """Project a `source_links` row, rendering the reason sentence server side.

    `crawl.reason_text` is the one renderer: the review list and any
    future email digest must say the same thing about the same link, so no client
    ever composes this copy.
    """
    return SourceLinkOut(
        id=row["id"],
        url=row["url"],
        title=row["title"],
        state=row["state"],
        reason=row["reason"],
        reason_detail=crawl.reason_text(row["reason"], row["reason_detail"]) or None,
        http_status=row["http_status"],
        depth=row["depth"],
        discovered_via=row["discovered_via"],
        content_bytes=row["content_bytes"],
        document_id=row["document_id"],
        fetched_at=row["fetched_at"],
    )


def _clamp_retrain(hours: int) -> int:
    """Clamp the per-source retrain cadence to the platform floor.

    Applied on write, so a source cannot be left retraining hourly.
    """
    if hours <= 0:
        return 0
    return max(hours, RETRAIN_MIN_HOURS)


def _config_json(existing: dict[str, Any] | None, config: crawl.CrawlConfig) -> dict[str, Any]:
    """Merge a validated config over the stored JSONB.

    `CrawlConfig.to_json` does not carry `force_exclude` — that list is written by
    the review screen, not by the config form — so writing `to_json()` alone would
    silently resurrect every manually excluded page on the next config edit.
    """
    merged = dict(existing or {})
    merged.update(config.to_json())
    return merged


def _force_list(config: dict[str, Any], key: str) -> list[str]:
    value = config.get(key)
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _guard(settings: Any, url: str, *, field: str = "url") -> str:
    """Canonicalise a tenant-supplied URL and refuse a non-public target.

    Both halves matter and the order is deliberate: canonicalisation rejects any
    scheme other than http/https and any URL carrying credentials, and only then
    is the host resolved and checked. Called synchronously from the create and
    add-links handlers, so a tenant aiming the crawler at a metadata endpoint
    learns immediately instead of watching a queued job fail.
    """
    canonical = crawl.canonicalise(url)
    if canonical is None:
        raise ValidationError(f"{field} is not a usable http or https address: {url[:120]}")
    assert_fetchable(
        canonical,
        allow_private=settings.URL_FETCH_ALLOW_PRIVATE,
        allowed_ports=settings.url_fetch_ports,
    )
    return canonical


async def _load(
    session: AsyncSession, source_id: UUID, *, kind: str | None = None
) -> Any:
    """Read one source or 404. `kind` guards the type-specific routes."""
    row = (
        await session.execute(
            text(f"SELECT {SOURCE_COLUMNS} FROM sources WHERE id = :id AND {NOT_DELETED}"),  # noqa: S608
            {"id": source_id},
        )
    ).mappings().first()
    if row is None:
        raise NotFoundError("no such source")
    if kind is not None and row["kind"] != kind:
        raise NotFoundError("no such source")
    return row


async def _recompute_counters(session: AsyncSession, source_id: UUID) -> None:
    """Recompute the denormalised aggregates from `source_links`.

    Recomputed, never incremented. A redelivered wave that incremented would
    double-count, and the tenant would see a page total that never comes back
    down. One statement over source_links_state_idx.
    """
    await session.execute(
        text(
            """
            UPDATE sources s SET
                link_total    = c.total,
                link_trained  = c.trained,
                link_failed   = c.failed,
                link_excluded = c.excluded,
                link_pending  = c.pending,
                size_bytes    = c.bytes,
                updated_at    = now()
              FROM (
                SELECT count(*)                                          AS total,
                       count(*) FILTER (WHERE state IN ('trained', 'unchanged')) AS trained,
                       count(*) FILTER (WHERE state IN ('failed', 'gone'))       AS failed,
                       count(*) FILTER (WHERE state = 'excluded')                AS excluded,
                       count(*) FILTER (
                           WHERE state IN ('pending', 'fetching', 'fetched'))     AS pending,
                       COALESCE(sum(content_bytes) FILTER (
                           WHERE state IN ('trained', 'unchanged')), 0)          AS bytes
                  FROM source_links WHERE source_id = :id
              ) c
             WHERE s.id = :id
            """
        ),
        {"id": source_id},
    )
    # chunk_count is owned by the children, so it is summed from documents rather
    # than from the frontier
    await session.execute(
        text(
            """
            UPDATE sources SET chunk_count = COALESCE((
                SELECT sum(chunk_count) FROM documents
                 WHERE source_id = :id AND status <> 'deleted'
            ), 0) WHERE id = :id
            """
        ),
        {"id": source_id},
    )


def _progress_message(row: Any) -> str:
    """Server-computed progress copy, so every client says the same thing."""
    state = row["crawl_state"]
    trained = int(row["link_trained"] or 0)
    pending = int(row["link_pending"] or 0)
    total = int(row["link_total"] or 0)
    if state == "discovering":
        return "Looking for pages…" if total == 0 else f"Found {total} pages so far…"
    if state == "fetching":
        return f"Reading pages — {trained} of {total} done, {pending} to go."
    if state == "extracting":
        return f"Making sense of {pending} pages…"
    if state == "cancelled":
        return f"Stopped. {trained} pages were read before it stopped and are still searchable."
    if state == "failed":
        return row["error"] or "This source could not be read."
    if state == "ready":
        if trained == 0:
            return "Nothing could be read from this source."
        return f"Ready — {trained} pages are searchable."
    return "Waiting to start."


async def _enqueue_wave(container: Any, tenant_id: UUID, source_id: UUID) -> bool:
    """Enqueue one crawl wave. False when the queue refused it.

    The source row already exists and is durable, so a queue outage must not throw
    the tenant's work away: the caller records the failure on the row instead,
    which is what the Retrain button re-enqueues.
    """
    from app.adapters.queue.base import Job

    try:
        await container.queue.enqueue(
            Job(
                kind="crawl_wave",
                tenant_id=tenant_id,
                payload={"source_id": str(source_id)},
            )
        )
        return True
    except Exception as exc:
        logger.error(
            "crawl_wave_enqueue_failed",
            source_id=str(source_id),
            tenant_id=str(tenant_id),
            error=str(exc),
            exc_info=True,
        )
        return False


async def _mark_enqueue_failure(session: AsyncSession, source_id: UUID) -> None:
    await session.execute(
        text(
            "UPDATE sources SET crawl_state = 'failed', error = :error, updated_at = now() "
            "WHERE id = :id"
        ),
        {
            "id": source_id,
            "error": "could not be queued for crawling; use Retrain to try again",
        },
    )


async def _record(
    session: AsyncSession,
    tenant: TenantContext,
    action: str,
    *,
    target_id: str,
    detail: dict[str, Any] | None = None,
) -> None:
    """Write an audit row inside the mutation's own transaction.

    Bound to the caller's session on purpose: an audit trail you can forget to
    write is worthless, so the row commits or rolls back with the action.
    """
    actor = (
        await session.execute(
            text("SELECT email FROM users WHERE id = :id"), {"id": tenant.user_id}
        )
    ).mappings().first()
    await audit.record(
        action,
        actor_type="user" if tenant.user_id else "api_key",
        actor_id=tenant.user_id,
        actor_email=actor["email"] if actor else None,
        target_tenant=tenant.tenant_id,
        target_type="source",
        target_id=target_id,
        detail=detail,
        session=session,
    )


async def _insert_links(
    session: AsyncSession,
    tenant_id: UUID,
    source_id: UUID,
    urls: list[str],
    *,
    discovered_via: str,
    depth: int = 0,
) -> None:
    """Insert frontier rows, ignoring URLs the source already knows about.

    `ON CONFLICT (source_id, url_hash)` is what makes adding the same link twice a
    no-op rather than an error, and it is the same guarantee that lets a
    redelivered wave re-insert its discoveries safely.
    """
    if not urls:
        return
    await session.execute(
        text(
            """
            INSERT INTO source_links (
                tenant_id, source_id, url, url_hash, depth, discovered_via, state
            ) VALUES (
                :tenant_id, :source_id, :url, :url_hash, :depth, :via, 'pending'
            )
            ON CONFLICT (source_id, url_hash) DO NOTHING
            """
        ),
        [
            {
                "tenant_id": tenant_id,
                "source_id": source_id,
                "url": url,
                "url_hash": crawl.url_hash(url),
                "depth": depth,
                "via": discovered_via,
            }
            for url in urls
        ],
    )


# ------------------------------------------------------------------- websites


@router.post("/website", response_model=SourceOut, status_code=202)
async def create_website_source(
    body: SourceCreate,
    tenant: TenantDep,
    container: ContainerDep,
    agent_id: UUID | None = Query(
        default=None,
        description="scope the source to one agent; omitted means workspace-wide",
    ),
) -> SourceOut:
    """Register a crawl, a sitemap or a list of links and start the first wave.

    The URL check is synchronous and happens here, before the 202: a tenant who
    points the crawler at a link-local metadata address gets a 422 they can read
    rather than a queued job that fails somewhere they cannot see. The crawl
    itself is not synchronous — one wave is enqueued and the frontier resumes.

    `agent_id` rides as a query param because `SourceCreate` forbids extra body
    fields and is shared verbatim with the dashboard's typed client.
    """
    await container.rate_limiter.check(
        "source_create", str(tenant.tenant_id), SOURCE_CREATE_PER_HOUR, 3600
    )

    settings = container.settings
    seed: str | None = None
    origin: str | None = None
    seeds: list[str] = []

    if body.mode == "links":
        if not body.urls:
            raise ValidationError("add at least one link")
        # every URL is guarded, not just the first: a list is exactly as good a
        # place to hide an internal address as a seed is
        seeds = [_guard(settings, url, field="urls") for url in body.urls]
        origin = crawl.origin_of(seeds[0])
    else:
        if not body.url:
            raise ValidationError("a website address is required")
        seed = _guard(settings, body.url, field="url")
        origin = crawl.origin_of(seed)
        seeds = [seed]

    title = body.title or (crawl.host_of(seeds[0]) if seeds else "Website")

    async with tenant_scope(tenant.tenant_id) as session:
        config = crawl.CrawlConfig.from_json(
            body.config.model_dump(),
            default_depth=container.settings.CRAWL_MAX_DEPTH,
            default_delay_ms=container.settings.CRAWL_DEFAULT_DELAY_MS,
        )
        # 'links' has nothing to discover — its frontier is complete at create, so
        # it starts a phase further along than a crawl does
        state = "fetching" if body.mode == "links" else "discovering"
        # The frontier this row is about to get, deduplicated the same way the
        # insert's ON CONFLICT would, so the counters can be written by the insert
        # itself. A crawl starts with the seed; a sitemap's seed is replaced by the
        # wave with the sitemap's own entries.
        frontier = (
            seeds
            if body.mode == "links"
            else ([seed] if seed is not None else [])
        )
        unique = list(dict.fromkeys(crawl.url_hash(url) for url in frontier))
        row = (
            await session.execute(
                text(
                    f"""
                    INSERT INTO sources (
                        tenant_id, agent_id, kind, title, seed_url, origin, config,
                        tags, collection, crawl_state, auto_retrain_hours,
                        crawl_started_at, link_total, link_pending
                    ) VALUES (
                        :tenant_id, :agent_id, :kind, :title, :seed_url, :origin,
                        CAST(:config AS jsonb), :tags, :collection, :state,
                        :retrain_hours, now(), :links, :links
                    )
                    RETURNING {SOURCE_COLUMNS}
                    """  # noqa: S608
                ),
                {
                    "tenant_id": tenant.tenant_id,
                    "agent_id": agent_id,
                    "kind": body.mode,
                    "title": title,
                    "seed_url": seed,
                    "origin": origin,
                    "config": json.dumps(config.to_json()),
                    "tags": body.tags,
                    "collection": body.collection,
                    "state": state,
                    "retrain_hours": _clamp_retrain(body.auto_retrain_hours),
                    "links": len(unique),
                },
            )
        ).mappings().one()
        source_id = row["id"]

        # Counters were written above rather than recomputed here: at create the
        # frontier is exactly `unique` and nothing else has touched it, so the
        # recompute would spend two more round trips proving what we already know.
        await _insert_links(
            session,
            tenant.tenant_id,
            source_id,
            frontier,
            discovered_via=(
                "manual"
                if body.mode == "links"
                else ("sitemap" if body.mode == "sitemap" else "seed")
            ),
        )
        await _record(
            session,
            tenant,
            "source.create",
            target_id=str(source_id),
            detail={
                "kind": body.mode,
                "seed_url": seed,
                "urls": len(seeds),
                "max_pages": config.max_pages,
            },
        )
        # The insert's own RETURNING is the response: nothing after it changed a
        # column this row carries, so re-selecting would be a round trip spent
        # reading back what we just wrote.
        out = _source_out(row)

    if not await _enqueue_wave(container, tenant.tenant_id, source_id):
        # the row is already durable, so a queue outage is reported on the source
        # rather than throwing the tenant's configuration away. Retrain re-queues.
        async with tenant_scope(tenant.tenant_id) as session:
            await _mark_enqueue_failure(session, source_id)
        out = out.model_copy(
            update={
                "crawl_state": "failed",
                "error": "could not be queued for crawling; use Retrain to try again",
            }
        )
    return out


@router.get("", response_model=list[SourceOut])
async def list_sources(
    tenant: TenantDep,
    kind: str | None = Query(default=None),
    q: str | None = Query(default=None, max_length=200),
    agent_id: UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=SOURCES_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> list[SourceOut]:
    """Parents only, one flat select.

    The child counts come from the denormalised columns rather than from a join
    over `source_links`, which is the whole reason those columns exist: a
    workspace with fifty crawled sites still costs one query here.

    `agent_id` narrows to one agent's sources; None means no clause at all, so a
    caller that omits it gets exactly today's tenant-wide result set.
    """
    clauses = [NOT_DELETED]
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if kind:
        clauses.append("kind = :kind")
        params["kind"] = kind
    if q:
        clauses.append("(title ILIKE :q OR seed_url ILIKE :q)")
        params["q"] = f"%{q}%"
    if agent_id:
        clauses.append("agent_id = :agent_id")
        params["agent_id"] = agent_id
    where = " AND ".join(clauses)

    async with tenant_scope(tenant.tenant_id) as session:
        rows = (
            await session.execute(
                text(
                    f"SELECT {SOURCE_COLUMNS} FROM sources WHERE {where} "  # noqa: S608
                    "ORDER BY created_at DESC LIMIT :limit OFFSET :offset"
                ),
                params,
            )
        ).mappings().all()
    return [_source_out(row) for row in rows]


@router.get("/summary", response_model=SourcesSummaryOut)
async def sources_summary(
    tenant: TenantDep,
    container: ContainerDep,
    agent_id: UUID | None = Query(default=None),
) -> SourcesSummaryOut:
    """The usage counter and the retrain badge in one call.

    Declared before `/{source_id}` on purpose — FastAPI matches in declaration
    order, and a literal path registered after a UUID path would never be reached.

    `agent_id` narrows the by_kind/needs_retrain/crawling tallies to one agent;
    None means no clause at all — tenant-wide. The usage numbers are always
    workspace-level, so they never narrow.
    """
    totals = await container.documents.usage_totals(tenant.tenant_id)
    agent_clause = " AND s.agent_id = :agent_id" if agent_id else ""

    async with tenant_scope(tenant.tenant_id) as session:
        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT s.kind                                AS kind,
                           count(DISTINCT s.id)                  AS sources,
                           count(d.id)                           AS documents,
                           COALESCE(sum(d.size_bytes), 0)        AS bytes
                      FROM sources s
                      LEFT JOIN documents d
                        ON d.source_id = s.id AND d.status <> 'deleted'
                     WHERE NOT COALESCE((s.config->>'deleted')::boolean, false){agent_clause}
                     GROUP BY s.kind
                    """  # noqa: S608
                ),
                {"agent_id": agent_id} if agent_id else {},
            )
        ).mappings().all()
        flags = (
            await session.execute(
                text(
                    f"""
                    SELECT count(*) FILTER (WHERE pending_changes)          AS needs_retrain,
                           count(*) FILTER (WHERE crawl_state = ANY(:live)) AS crawling
                      FROM sources WHERE {NOT_DELETED}{" AND agent_id = :agent_id" if agent_id else ""}
                    """  # noqa: S608
                ),
                {
                    "live": list(LIVE_CRAWL_STATES),
                    **({"agent_id": agent_id} if agent_id else {}),
                },
            )
        ).mappings().one()

    return SourcesSummaryOut(
        by_kind={
            str(row["kind"]): {
                "sources": int(row["sources"]),
                "documents": int(row["documents"]),
                "bytes": int(row["bytes"]),
            }
            for row in rows
        },
        documents=int(totals.get("documents", 0)),
        storage_bytes=int(totals.get("storage_bytes", 0)),
        needs_retrain=int(flags["needs_retrain"]),
        crawling=int(flags["crawling"]),
    )


@router.get("/{source_id}", response_model=SourceDetailOut)
async def get_source(source_id: UUID, tenant: TenantDep) -> SourceDetailOut:
    """The source plus the first page of each review group.

    Capped at 25 links per group: this response paints the screen, and the full
    lists are paged from `/links`. Without the cap a 10,000-page crawl would send
    a 10,000-row JSON body to render three tabs.
    """
    async with tenant_scope(tenant.tenant_id) as session:
        row = await _load(session, source_id)
        groups: dict[str, list[SourceLinkOut]] = {}
        for group in ("trained", "not_found", "excluded"):
            links = (
                await session.execute(
                    text(
                        f"SELECT {LINK_COLUMNS} FROM source_links "  # noqa: S608
                        "WHERE source_id = :id AND state = ANY(:states) "
                        "ORDER BY updated_at DESC LIMIT :cap"
                    ),
                    {
                        "id": source_id,
                        "states": list(LINK_GROUPS[group]),
                        "cap": LINK_GROUP_CAP,
                    },
                )
            ).mappings().all()
            groups[group] = [_link_out(link) for link in links]

    return SourceDetailOut(
        **_source_out(row).model_dump(),
        trained=groups["trained"],
        not_found=groups["not_found"],
        excluded=groups["excluded"],
    )


@router.get("/{source_id}/links", response_model=SourceLinkPage)
async def list_source_links(
    source_id: UUID,
    tenant: TenantDep,
    state: Literal["trained", "not_found", "excluded", "pending", "all"] = Query(
        default="all"
    ),
    q: str | None = Query(default=None, max_length=200),
    reason: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=50, ge=1, le=LINKS_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> SourceLinkPage:
    """One page of the review list, with the tab-strip totals alongside.

    `counts` ships with every page so switching tabs is a filter, not a request.
    """
    clauses = ["source_id = :id"]
    params: dict[str, Any] = {"id": source_id, "limit": limit, "offset": offset}
    if state != "all":
        clauses.append("state = ANY(:states)")
        params["states"] = list(LINK_GROUPS[state])
    if q:
        clauses.append("(url ILIKE :q OR title ILIKE :q)")
        params["q"] = f"%{q}%"
    if reason:
        clauses.append("reason = :reason")
        params["reason"] = reason
    where = " AND ".join(clauses)

    async with tenant_scope(tenant.tenant_id) as session:
        await _load(session, source_id)
        rows = (
            await session.execute(
                text(
                    f"SELECT {LINK_COLUMNS} FROM source_links WHERE {where} "  # noqa: S608
                    "ORDER BY updated_at DESC LIMIT :limit OFFSET :offset"
                ),
                params,
            )
        ).mappings().all()
        total = (
            await session.execute(
                text(f"SELECT count(*) AS n FROM source_links WHERE {where}"),  # noqa: S608
                {k: v for k, v in params.items() if k not in ("limit", "offset")},
            )
        ).mappings().one()
        grouped = (
            await session.execute(
                text(
                    "SELECT state, count(*) AS n FROM source_links "
                    "WHERE source_id = :id GROUP BY state"
                ),
                {"id": source_id},
            )
        ).mappings().all()

    by_state = {str(item["state"]): int(item["n"]) for item in grouped}
    counts = {
        group: sum(by_state.get(member, 0) for member in members)
        for group, members in LINK_GROUPS.items()
    }
    counts["all"] = sum(by_state.values())
    return SourceLinkPage(
        items=[_link_out(row) for row in rows], total=int(total["n"]), counts=counts
    )


@router.get("/{source_id}/progress", response_model=CrawlProgressOut)
async def crawl_progress(source_id: UUID, tenant: TenantDep) -> CrawlProgressOut:
    """Polled every two seconds while a crawl is live, so it stays cheap.

    Two indexed reads and no aggregation: the counters were already recomputed by
    the wave that wrote them.
    """
    async with tenant_scope(tenant.tenant_id) as session:
        row = await _load(session, source_id)
        recent = (
            await session.execute(
                text(
                    f"SELECT {LINK_COLUMNS} FROM source_links "  # noqa: S608
                    "WHERE source_id = :id AND state <> 'pending' "
                    "ORDER BY updated_at DESC LIMIT 12"
                ),
                {"id": source_id},
            )
        ).mappings().all()

    return CrawlProgressOut(
        crawl_state=row["crawl_state"],
        link_total=row["link_total"],
        link_trained=row["link_trained"],
        link_failed=row["link_failed"],
        link_excluded=row["link_excluded"],
        link_pending=row["link_pending"],
        size_bytes=row["size_bytes"],
        error=row["error"],
        recent=[_link_out(link) for link in recent],
        message=_progress_message(row),
    )


@router.patch("/{source_id}", response_model=SourceOut)
async def update_source(
    source_id: UUID, body: SourceUpdate, tenant: TenantDep, container: ContainerDep
) -> SourceOut:
    """Edit title, facets, config or cadence. Stored only — never auto-crawling.

    A tenant who tightens an exclude path is editing a rule, not asking for a
    fresh crawl of a thousand pages; the change takes effect on the next retrain
    and `pending_changes` turns the button on so the choice stays theirs.
    """
    async with tenant_scope(tenant.tenant_id) as session:
        row = await _load(session, source_id)

        assignments = ["updated_at = now()"]
        params: dict[str, Any] = {"id": source_id}
        if body.title is not None:
            assignments.append("title = :title")
            params["title"] = body.title
        if body.tags is not None:
            assignments.append("tags = :tags")
            params["tags"] = body.tags
        if body.collection is not None:
            assignments.append("collection = :collection")
            params["collection"] = body.collection
        if body.auto_retrain_hours is not None:
            assignments.append("auto_retrain_hours = :retrain_hours")
            params["retrain_hours"] = _clamp_retrain(body.auto_retrain_hours)
        if body.config is not None:
            config = crawl.CrawlConfig.from_json(
                body.config.model_dump(),
                default_depth=container.settings.CRAWL_MAX_DEPTH,
                default_delay_ms=container.settings.CRAWL_DEFAULT_DELAY_MS,
            )
            assignments.append("config = CAST(:config AS jsonb)")
            # merged, not replaced: force_exclude is written by the review screen
            # and to_json does not carry it, so a plain overwrite would resurrect
            # every page the tenant excluded by hand
            params["config"] = json.dumps(_config_json(dict(row["config"] or {}), config))
            assignments.append("pending_changes = true")

        updated = (
            await session.execute(
                text(
                    f"UPDATE sources SET {', '.join(assignments)} "  # noqa: S608
                    f"WHERE id = :id RETURNING {SOURCE_COLUMNS}"
                ),
                params,
            )
        ).mappings().one()
        await _record(
            session,
            tenant,
            "source.update",
            target_id=str(source_id),
            detail={"fields": sorted(body.model_dump(exclude_none=True).keys())},
        )
        # child documents inherit the parent's facets, re-applied on edit so a
        # retrieval filter set here covers pages that were crawled before it
        if body.tags is not None or body.collection is not None:
            await session.execute(
                text(
                    "UPDATE documents SET tags = :tags, collection = :collection "
                    "WHERE source_id = :id AND status <> 'deleted'"
                ),
                {
                    "id": source_id,
                    "tags": list(updated["tags"] or []),
                    "collection": updated["collection"],
                },
            )
    return _source_out(updated)


@router.post("/{source_id}/retrain", status_code=202)
async def retrain_source(
    source_id: UUID, tenant: TenantDep, container: ContainerDep
) -> dict[str, Any]:
    """Re-crawl now: re-open the frontier and enqueue a wave.

    Known links go back to `pending` so the wave re-checks them, which is cheap
    because each carries an etag and a last-modified — an unchanged page costs a
    304 and lands in `unchanged`, not a re-parse. Manually excluded links are left
    alone: a retrain must never undo a tenant's decision. `fetched` links reopen
    too: those exist only on a source that parked for review or was cancelled
    mid-wave, and a retrain is a request to re-read the site, not to resume.

    `review_approved_at` is cleared for the same reason. The approval belongs to
    the list that was approved; a fresh crawl finds fresh URLs, so a source with
    `review_before_index` on gates again rather than indexing them unseen.

    The response is a superset — the `{status, source_id}` the spec names, plus the
    whole updated source, because the dashboard's card re-renders from it.
    """
    await container.rate_limiter.check(
        "source_retrain",
        str(tenant.tenant_id),
        container.settings.SOURCE_RETRAIN_PER_HOUR,
        3600,
    )
    async with tenant_scope(tenant.tenant_id) as session:
        row = await _load(session, source_id)
        if row["crawl_state"] in LIVE_CRAWL_STATES:
            raise ConflictError("this source is already being read")

        await session.execute(
            text(
                """
                UPDATE source_links
                   SET state = 'pending', claimed_at = NULL, attempt = 0,
                       updated_at = now()
                 WHERE source_id = :id
                   AND state IN ('trained', 'unchanged', 'failed', 'gone', 'fetched')
                """
            ),
            {"id": source_id},
        )
        if row["seed_url"]:
            await _insert_links(
                session,
                tenant.tenant_id,
                source_id,
                [row["seed_url"]],
                discovered_via="sitemap" if row["kind"] == "sitemap" else "seed",
            )
        updated = (
            await session.execute(
                text(
                    f"""
                    UPDATE sources
                       SET crawl_state = 'discovering', error = NULL,
                           pending_changes = false, crawl_started_at = now(),
                           last_checked_at = now(), review_approved_at = NULL,
                           updated_at = now()
                     WHERE id = :id RETURNING {SOURCE_COLUMNS}
                    """  # noqa: S608
                ),
                {"id": source_id},
            )
        ).mappings().one()
        await _recompute_counters(session, source_id)
        await _record(session, tenant, "source.retrain", target_id=str(source_id))

    out = _source_out(updated)
    if not await _enqueue_wave(container, tenant.tenant_id, source_id):
        async with tenant_scope(tenant.tenant_id) as session:
            await _mark_enqueue_failure(session, source_id)
        out = out.model_copy(
            update={
                "crawl_state": "failed",
                "error": "could not be queued for crawling; use Retrain to try again",
            }
        )
    return {"status": "queued", "source_id": str(source_id), **out.model_dump(mode="json")}


@router.post("/{source_id}/cancel", response_model=SourceOut)
async def cancel_crawl(source_id: UUID, tenant: TenantDep) -> SourceOut:
    """Stop a running crawl. A partial crawl is a partial success, not a rollback.

    Pages already trained stay searchable and the outstanding frontier is closed
    off as excluded, so the tenant keeps what was read and the wave that wakes up
    next finds nothing to claim.

    `awaiting_review` counts as cancellable even though no wave is in flight. The
    tenant is looking at a list they do not want indexed; "no thanks" has to be an
    answer, or the only way out of the gate would be to approve it first. The
    existing UPDATE already closes off `fetched` rows, which is exactly what a
    parked frontier is made of.
    """
    async with tenant_scope(tenant.tenant_id) as session:
        row = await _load(session, source_id)
        if row["crawl_state"] not in CANCELLABLE_CRAWL_STATES:
            raise ConflictError("this source is not being read right now")

        await session.execute(
            text(
                """
                UPDATE source_links
                   SET state = 'excluded', reason = 'cancelled', claimed_at = NULL,
                       updated_at = now()
                 WHERE source_id = :id AND state IN ('pending', 'fetching', 'fetched')
                """
            ),
            {"id": source_id},
        )
        # No RETURNING: the row is re-read below anyway, because
        # _recompute_counters rewrites the very counters a RETURNING here would
        # have captured a moment too early.
        await session.execute(
            text(
                "UPDATE sources SET crawl_state = 'cancelled', updated_at = now() "
                "WHERE id = :id"
            ),
            {"id": source_id},
        )
        await _recompute_counters(session, source_id)
        await _record(session, tenant, "source.cancel", target_id=str(source_id))
        fresh = (
            await session.execute(
                text(f"SELECT {SOURCE_COLUMNS} FROM sources WHERE id = :id"),  # noqa: S608
                {"id": source_id},
            )
        ).mappings().one()
    return _source_out(fresh)


# ---------------------------------------------------------------------- review


def _review_group_out(group: crawl.UrlGroup) -> ReviewGroupOut:
    """Project one domain `UrlGroup` onto the wire, recursing into its children."""
    return ReviewGroupOut(
        key=group.key,
        label=group.label,
        total=group.total,
        urls=list(group.urls),
        children=[_review_group_out(child) for child in group.children],
    )


@router.get("/{source_id}/review", response_model=SourceReviewOut)
async def get_source_review(source_id: UUID, tenant: TenantDep) -> SourceReviewOut:
    """The URL list a crawl is waiting to have approved, grouped for reading.

    Answers one question: *what exactly will be indexed if I say yes?* The pages
    are fetched and their raw bytes are in storage, but nothing has been chunked,
    embedded or made retrievable — so a page removed here costs nothing and a page
    removed after training is a deletion.

    Readable in any crawl state, not only `awaiting_review`. A tenant who wants to
    see the frontier of a crawl that already finished gets the same tree; what
    changes is that there is nothing to approve, which `crawl_state` tells them.

    Removal is not here. A page is dropped with the existing
    `POST /{source_id}/links/bulk`, which also records the exclusion in
    `config.force_exclude` so the next retrain does not rediscover it.
    """
    async with tenant_scope(tenant.tenant_id) as session:
        row = await _load(session, source_id)
        rows = (
            await session.execute(
                text(
                    "SELECT id, url, title, depth, state FROM source_links "
                    "WHERE source_id = :id AND state = ANY(CAST(:states AS text[])) "
                    "ORDER BY url ASC LIMIT :limit"
                ),
                {
                    "id": source_id,
                    "states": list(REVIEW_LIST_STATES),
                    # one over the cap, so "there are more" is a fact about the rows
                    # rather than a guess from a count that equals the limit
                    "limit": REVIEW_URL_CAP + 1,
                },
            )
        ).mappings().all()

    truncated = len(rows) > REVIEW_URL_CAP
    links = [
        ReviewLinkOut(
            id=r["id"],
            url=r["url"],
            title=r["title"],
            depth=r["depth"],
            state=r["state"],
        )
        for r in rows[:REVIEW_URL_CAP]
    ]
    config = crawl.CrawlConfig.from_json(dict(row["config"] or {}))
    groups = crawl.group_urls([link.url for link in links])
    return SourceReviewOut(
        crawl_state=row["crawl_state"],
        review_before_index=config.review_before_index,
        review_approved_at=row["review_approved_at"],
        total=len(links),
        excluded=row["link_excluded"],
        truncated=truncated,
        groups=[_review_group_out(group) for group in groups],
        links=links,
    )


@router.post("/{source_id}/approve", status_code=202)
async def approve_source_review(
    source_id: UUID, tenant: TenantDep, container: ContainerDep
) -> dict[str, Any]:
    """Approve the URL list and let the crawl index it. The one commit point.

    Everything the gate withheld happens after this call: fetching whatever was not
    read yet, extraction, chunking, embedding, vector storage. Whatever the tenant
    removed first is already an `excluded` row, so the wave that resumes cannot pick
    it up.

    Resumes into `fetching` when pages are still unread and into `extracting` when
    they are all in hand. The gate parks as soon as the *list* is known — a sitemap
    hands over the whole site before a single page is fetched — so the common case is
    that approval is what starts the reading.

    `review_approved_at` is what disarms the gate, not the config flag — the flag
    stays on so the next retrain asks again. The response is the same
    `{status, source_id}`-plus-source shape as Retrain, for the same reason.
    """
    async with tenant_scope(tenant.tenant_id) as session:
        row = await _load(session, source_id)
        if row["crawl_state"] != REVIEW_CRAWL_STATE:
            raise ConflictError("this source is not waiting for review")
        unread = int(
            (
                await session.execute(
                    text(
                        "SELECT count(*) FROM source_links "
                        "WHERE source_id = :id AND state = 'pending'"
                    ),
                    {"id": source_id},
                )
            ).scalar_one()
        )
        resume = "fetching" if unread else "extracting"
        updated = (
            await session.execute(
                text(
                    f"""
                    UPDATE sources
                       SET crawl_state = :resume, error = NULL,
                           review_approved_at = now(), last_checked_at = now(),
                           updated_at = now()
                     WHERE id = :id RETURNING {SOURCE_COLUMNS}
                    """  # noqa: S608
                ),
                {"id": source_id, "resume": resume},
            )
        ).mappings().one()
        await _record(
            session,
            tenant,
            "source.review.approve",
            target_id=str(source_id),
            detail={
                "pending": row["link_pending"],
                "excluded": row["link_excluded"],
            },
        )

    out = _source_out(updated)
    if not await _enqueue_wave(container, tenant.tenant_id, source_id):
        async with tenant_scope(tenant.tenant_id) as session:
            await _mark_enqueue_failure(session, source_id)
        out = out.model_copy(
            update={
                "crawl_state": "failed",
                "error": "could not be queued for crawling; use Retrain to try again",
            }
        )
    return {"status": "queued", "source_id": str(source_id), **out.model_dump(mode="json")}


# ----------------------------------------------------------------------- links


@router.post("/{source_id}/links", response_model=list[SourceLinkOut], status_code=202)
async def add_source_links(
    source_id: UUID, body: AddLinksIn, tenant: TenantDep, container: ContainerDep
) -> list[SourceLinkOut]:
    """Add individual URLs to an existing source.

    Each URL goes through the same synchronous guard as a seed. A URL the source
    already knows is a no-op rather than an error, so a tenant pasting an
    overlapping list twice does not get a 409 they cannot act on.
    """
    settings = container.settings
    urls = [_guard(settings, url, field="urls") for url in body.urls]

    async with tenant_scope(tenant.tenant_id) as session:
        await _load(session, source_id)
        await _insert_links(
            session, tenant.tenant_id, source_id, urls, discovered_via="manual"
        )
        await session.execute(
            text(
                "UPDATE sources SET crawl_state = 'fetching', error = NULL, "
                "crawl_started_at = now(), updated_at = now() WHERE id = :id"
            ),
            {"id": source_id},
        )
        await _recompute_counters(session, source_id)
        await _record(
            session,
            tenant,
            "source.links.add",
            target_id=str(source_id),
            detail={"count": len(urls)},
        )
        rows = (
            await session.execute(
                text(
                    f"SELECT {LINK_COLUMNS} FROM source_links "  # noqa: S608
                    "WHERE source_id = :id AND url_hash = ANY(:hashes)"
                ),
                {"id": source_id, "hashes": [crawl.url_hash(url) for url in urls]},
            )
        ).mappings().all()

    if not await _enqueue_wave(container, tenant.tenant_id, source_id):
        async with tenant_scope(tenant.tenant_id) as session:
            await _mark_enqueue_failure(session, source_id)
    return [_link_out(row) for row in rows]


@router.post("/{source_id}/links/{link_id}/exclude", response_model=SourceLinkOut)
async def exclude_link(
    source_id: UUID, link_id: UUID, tenant: TenantDep, container: ContainerDep
) -> SourceLinkOut:
    """Drop one page: vectors gone now, and it stays gone across retrains.

    Recording the `url_hash` in `config.force_exclude` is the durable half. Without
    it the next crawl would rediscover the page from a link on the homepage and
    quietly put it back, and the tenant would have to exclude it again every week.
    """
    async with tenant_scope(tenant.tenant_id) as session:
        await _load(session, source_id)
        link = (
            await session.execute(
                text(
                    f"""
                    UPDATE source_links
                       SET state = 'excluded', reason = 'manual', reason_detail = NULL,
                           claimed_at = NULL, updated_at = now()
                     WHERE id = :link_id AND source_id = :source_id
                     RETURNING {LINK_COLUMNS}, url_hash
                    """  # noqa: S608
                ),
                {"link_id": link_id, "source_id": source_id},
            )
        ).mappings().first()
        if link is None:
            raise NotFoundError("no such link")

        await session.execute(
            text(
                """
                UPDATE sources SET config = jsonb_set(
                        config, '{force_exclude}',
                        COALESCE(config->'force_exclude', '[]'::jsonb)
                            || to_jsonb(CAST(:hash AS text)), true),
                    updated_at = now()
                 WHERE id = :id
                   AND NOT COALESCE(config->'force_exclude', '[]'::jsonb)
                           @> to_jsonb(CAST(:hash AS text))
                """
            ),
            {"id": source_id, "hash": link["url_hash"]},
        )
        # and it must stop being reachable through include-anyway too, or the two
        # lists would contradict each other on the next wave
        await session.execute(
            text(
                """
                UPDATE sources SET config = jsonb_set(
                        config, '{force_include}',
                        COALESCE(config->'force_include', '[]'::jsonb)
                            - CAST(:hash AS text), true)
                 WHERE id = :id
                """
            ),
            {"id": source_id, "hash": link["url_hash"]},
        )
        await _recompute_counters(session, source_id)
        await _record(
            session,
            tenant,
            "source.link.exclude",
            target_id=str(link_id),
            detail={"source_id": str(source_id)},
        )

    document_id = link["document_id"]
    if document_id is not None:
        # vectors first: a chunk without a document row is unattributable and
        # would keep answering questions about a page the tenant just removed
        await container.indexer.remove(tenant.tenant_id, document_id)
        await container.documents.soft_delete(tenant.tenant_id, document_id)
    return _link_out(link)


@router.post("/{source_id}/links/{link_id}/restore", response_model=SourceLinkOut, status_code=202)
async def restore_link(
    source_id: UUID, link_id: UUID, tenant: TenantDep, container: ContainerDep
) -> SourceLinkOut:
    """Put an excluded page back and fetch it on the next wave.

    Two lists change, not one. Dropping `force_exclude` undoes the tenant's own
    earlier decision; adding `force_include` is what overrides an automatic rule,
    so a knowledge base that genuinely lives under `/search` can be pulled back in
    without switching the whole rule set off.
    """
    async with tenant_scope(tenant.tenant_id) as session:
        await _load(session, source_id)
        current = (
            await session.execute(
                text(
                    "SELECT url_hash, reason FROM source_links "
                    "WHERE id = :link_id AND source_id = :source_id"
                ),
                {"link_id": link_id, "source_id": source_id},
            )
        ).mappings().first()
        if current is None:
            raise NotFoundError("no such link")

        link = (
            await session.execute(
                text(
                    f"""
                    UPDATE source_links
                       SET state = 'pending', reason = NULL, reason_detail = NULL,
                           attempt = 0, claimed_at = NULL, updated_at = now()
                     WHERE id = :link_id AND source_id = :source_id
                     RETURNING {LINK_COLUMNS}
                    """  # noqa: S608
                ),
                {"link_id": link_id, "source_id": source_id},
            )
        ).mappings().one()

        await session.execute(
            text(
                """
                UPDATE sources SET config = jsonb_set(
                        config, '{force_exclude}',
                        COALESCE(config->'force_exclude', '[]'::jsonb)
                            - CAST(:hash AS text), true),
                    updated_at = now()
                 WHERE id = :id
                """
            ),
            {"id": source_id, "hash": current["url_hash"]},
        )
        if current["reason"] and current["reason"] not in HUMAN_EXCLUSION_REASONS:
            await session.execute(
                text(
                    """
                    UPDATE sources SET config = jsonb_set(
                            config, '{force_include}',
                            COALESCE(config->'force_include', '[]'::jsonb)
                                || to_jsonb(CAST(:hash AS text)), true)
                     WHERE id = :id
                       AND NOT COALESCE(config->'force_include', '[]'::jsonb)
                               @> to_jsonb(CAST(:hash AS text))
                    """
                ),
                {"id": source_id, "hash": current["url_hash"]},
            )
        await session.execute(
            text(
                "UPDATE sources SET crawl_state = 'fetching', error = NULL, "
                "updated_at = now() WHERE id = :id"
            ),
            {"id": source_id},
        )
        await _recompute_counters(session, source_id)
        await _record(
            session,
            tenant,
            "source.link.restore",
            target_id=str(link_id),
            detail={"source_id": str(source_id), "was": current["reason"]},
        )

    if not await _enqueue_wave(container, tenant.tenant_id, source_id):
        async with tenant_scope(tenant.tenant_id) as session:
            await _mark_enqueue_failure(session, source_id)
    return _link_out(link)


@router.post("/{source_id}/links/bulk")
async def bulk_links(
    source_id: UUID, body: BulkLinksIn, tenant: TenantDep, container: ContainerDep
) -> dict[str, int]:
    """Exclude or restore up to 500 links in one statement.

    One UPDATE and one counter recompute, because the review screen's use case is
    "select all of these 300 archive pages and drop them" and 300 round-trips
    would time out. Vector removal is fanned out with a small semaphore rather
    than unbounded: 500 concurrent deletes would exhaust the connection pool the
    rest of the request path shares.
    """
    async with tenant_scope(tenant.tenant_id) as session:
        await _load(session, source_id)
        rows = (
            await session.execute(
                text(
                    "SELECT id, url_hash, document_id, reason FROM source_links "
                    "WHERE source_id = :source_id AND id = ANY(:ids)"
                ),
                {"source_id": source_id, "ids": body.link_ids},
            )
        ).mappings().all()
        if not rows:
            return {"updated": 0}

        ids = [row["id"] for row in rows]
        hashes = [row["url_hash"] for row in rows]
        if body.action == "exclude":
            await session.execute(
                text(
                    "UPDATE source_links SET state = 'excluded', reason = :reason, "
                    "reason_detail = NULL, claimed_at = NULL, updated_at = now() "
                    "WHERE source_id = :source_id AND id = ANY(:ids)"
                ),
                {"source_id": source_id, "ids": ids, "reason": body.reason},
            )
            await session.execute(
                text(
                    """
                    UPDATE sources SET config = jsonb_set(
                            config, '{force_exclude}',
                            COALESCE(config->'force_exclude', '[]'::jsonb)
                                || CAST(:hashes AS jsonb), true),
                        updated_at = now()
                     WHERE id = :id
                    """
                ),
                {"id": source_id, "hashes": json.dumps(sorted(set(hashes)))},
            )
        else:
            await session.execute(
                text(
                    "UPDATE source_links SET state = 'pending', reason = NULL, "
                    "reason_detail = NULL, attempt = 0, claimed_at = NULL, "
                    "updated_at = now() WHERE source_id = :source_id AND id = ANY(:ids)"
                ),
                {"source_id": source_id, "ids": ids},
            )
            for url_hash in sorted(set(hashes)):
                await session.execute(
                    text(
                        """
                        UPDATE sources SET config = jsonb_set(
                                config, '{force_exclude}',
                                COALESCE(config->'force_exclude', '[]'::jsonb)
                                    - CAST(:hash AS text), true),
                            updated_at = now()
                         WHERE id = :id
                        """
                    ),
                    {"id": source_id, "hash": url_hash},
                )
        await _recompute_counters(session, source_id)
        await _record(
            session,
            tenant,
            f"source.links.{body.action}",
            target_id=str(source_id),
            detail={"count": len(ids)},
        )

    if body.action == "exclude":
        document_ids = [row["document_id"] for row in rows if row["document_id"]]
        semaphore = asyncio.Semaphore(BULK_VECTOR_CONCURRENCY)

        async def drop(document_id: UUID) -> None:
            async with semaphore:
                await container.indexer.remove(tenant.tenant_id, document_id)
                await container.documents.soft_delete(tenant.tenant_id, document_id)

        results = await asyncio.gather(
            *(drop(document_id) for document_id in document_ids), return_exceptions=True
        )
        for outcome in results:
            if isinstance(outcome, BaseException):
                # the links are excluded either way; an orphaned vector is caught
                # by the same sweep that catches an orphaned blob
                logger.warning(
                    "bulk_vector_remove_failed",
                    source_id=str(source_id),
                    error=str(outcome),
                )
    elif not await _enqueue_wave(container, tenant.tenant_id, source_id):
        async with tenant_scope(tenant.tenant_id) as session:
            await _mark_enqueue_failure(session, source_id)
    return {"updated": len(rows)}


@router.delete("/{source_id}", status_code=204)
async def delete_source(
    source_id: UUID, tenant: TenantDep, container: ContainerDep
) -> None:
    """Delete a source and everything under it.

    The cascade is batched over `delete_source` waves because a 10,000-page source
    means 10,000 vector deletes, which no request can finish. So the source is
    marked gone and vanishes from every read immediately — the tenant's intent is
    honoured at once — while the children are reaped in batches of fifty.

    006 gives `sources` no soft-delete column, so the marker is `config.deleted`,
    which `CrawlConfig.from_json` ignores on load. The row itself is removed by the
    last wave, once no child is left to orphan.
    """
    async with tenant_scope(tenant.tenant_id) as session:
        await _load(session, source_id)
        await session.execute(
            text(
                """
                UPDATE sources
                   SET config = jsonb_set(config, '{deleted}', 'true'::jsonb, true),
                       crawl_state = 'cancelled', updated_at = now()
                 WHERE id = :id
                """
            ),
            {"id": source_id},
        )
        # closes the frontier so a wave already in flight claims nothing more
        await session.execute(
            text(
                "UPDATE source_links SET state = 'excluded', reason = 'cancelled', "
                "claimed_at = NULL, updated_at = now() "
                "WHERE source_id = :id AND state IN ('pending', 'fetching', 'fetched')"
            ),
            {"id": source_id},
        )
        await _record(session, tenant, "source.delete", target_id=str(source_id))

    from app.adapters.queue.base import Job

    try:
        await container.queue.enqueue(
            Job(
                kind="delete_source",
                tenant_id=tenant.tenant_id,
                payload={"source_id": str(source_id)},
            )
        )
    except Exception:
        # the source is already invisible to the tenant; the stall sweep re-enqueues
        logger.error("delete_source_enqueue_failed", source_id=str(source_id), exc_info=True)


# -------------------------------------------------------------------- snippets


async def _enqueue_ingest(
    container: Any, tenant_id: UUID, document_id: UUID, storage_path: str
) -> bool:
    """Hand a stored document to the ordinary ingestion pipeline."""
    from app.adapters.queue.base import Job

    try:
        await container.queue.enqueue(
            Job(
                kind="ingest_document",
                tenant_id=tenant_id,
                payload={"document_id": str(document_id), "storage_path": storage_path},
            )
        )
        return True
    except Exception:
        logger.error("ingest_enqueue_failed", document_id=str(document_id), exc_info=True)
        await container.documents.set_status(
            tenant_id,
            document_id,
            DocumentStatus.FAILED,
            "could not be queued for processing; save it again to retry",
        )
        return False


@router.post("/snippets", response_model=SourceOut, status_code=202)
async def create_snippet(
    body: SnippetIn,
    tenant: TenantDep,
    container: ContainerDep,
    agent_id: UUID | None = Query(default=None),
) -> SourceOut:
    """Store a piece of text the tenant typed, as an ordinary document.

    Nothing about ingestion is special-cased: the body is stored as markdown bytes
    and goes through parse, chunk, embed and index exactly like an upload, so a
    snippet is citable, deletable and quota-counted with no new code path.
    """
    data = body.body.encode("utf-8")
    async with tenant_scope(tenant.tenant_id) as session:
        row = (
            await session.execute(
                text(
                    f"""
                    INSERT INTO sources (
                        tenant_id, agent_id, kind, title, tags, collection,
                        crawl_state, link_total, link_pending
                    ) VALUES (
                        :tenant_id, :agent_id, 'snippet', :title, :tags, :collection,
                        'idle', 1, 1
                    )
                    RETURNING {SOURCE_COLUMNS}
                    """  # noqa: S608
                ),
                {
                    "tenant_id": tenant.tenant_id,
                    "agent_id": agent_id,
                    "title": body.title,
                    "tags": body.tags,
                    "collection": body.collection,
                },
            )
        ).mappings().one()
        source_id = row["id"]
        await _record(session, tenant, "source.snippet.create", target_id=str(source_id))

    document_id, storage_path = await _write_snippet_document(
        container, tenant, source_id, body, data
    )
    await _enqueue_ingest(container, tenant.tenant_id, document_id, storage_path)

    async with tenant_scope(tenant.tenant_id) as session:
        fresh = (
            await session.execute(
                text(
                    f"UPDATE sources SET size_bytes = :size, crawl_state = 'ready', "  # noqa: S608
                    f"last_trained_at = now(), link_trained = 1, link_pending = 0, "
                    f"updated_at = now() WHERE id = :id RETURNING {SOURCE_COLUMNS}"
                ),
                {"id": source_id, "size": len(data)},
            )
        ).mappings().one()
    return _source_out(fresh)


async def _write_snippet_document(
    container: Any,
    tenant: TenantContext,
    source_id: UUID,
    body: SnippetIn,
    data: bytes,
) -> tuple[UUID, str]:
    """Create the snippet's document row and store its bytes.

    The sha is over the text alone. Source-owned documents are outside the
    tenant-wide uniqueness index — 006 narrowed it to `source_id IS NULL` — so two
    snippets with the same words are two documents, which is what a tenant who
    wrote the same sentence in two places expects.
    """
    from app.adapters.storage.base import document_path

    document = await container.documents.create(
        tenant_id=tenant.tenant_id,
        filename=body.title,
        mime=SNIPPET_MIME,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        storage_path="",
        agent_id=tenant.agent_id,
        page_count=1,
        source="snippet",
        source_id=source_id,
        source_url=None,
    )
    path = document_path(tenant.tenant_id, document.id)
    await container.storage.put(path, data, SNIPPET_MIME)
    # facets are inherited from the parent, so a filter set on the source covers it
    await container.documents.update_metadata(
        tenant.tenant_id, document.id, tags=body.tags, collection=body.collection
    )
    return document.id, path


@router.put("/snippets/{source_id}", response_model=SourceOut, status_code=202)
async def update_snippet(
    source_id: UUID, body: SnippetIn, tenant: TenantDep, container: ContainerDep
) -> SourceOut:
    """Replace a snippet's text and re-index it.

    The document id is kept rather than replaced: chunks, citations already written
    into message history and `source_links.document_id` all point at it, and a new
    row would orphan every one of them.
    """
    data = body.body.encode("utf-8")
    async with tenant_scope(tenant.tenant_id) as session:
        await _load(session, source_id, kind="snippet")
        child = (
            await session.execute(
                text(
                    "SELECT id FROM documents WHERE source_id = :id "
                    "AND status <> 'deleted' ORDER BY created_at LIMIT 1"
                ),
                {"id": source_id},
            )
        ).mappings().first()

    from app.adapters.storage.base import document_path

    if child is None:
        # the original document was deleted from the Files tab; write a new one
        # rather than fail the save — the tenant's text is what matters
        document_id, path = await _write_snippet_document(
            container, tenant, source_id, body, data
        )
    else:
        document_id = child["id"]
        path = document_path(tenant.tenant_id, document_id)
        await container.storage.put(path, data, SNIPPET_MIME)
        await container.documents.replace_content(
            tenant.tenant_id,
            document_id,
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
            mime=SNIPPET_MIME,
            page_count=1,
        )
        await container.documents.update_metadata(
            tenant.tenant_id,
            document_id,
            tags=body.tags,
            collection=body.collection,
        )

    await _enqueue_ingest(container, tenant.tenant_id, document_id, path)

    async with tenant_scope(tenant.tenant_id) as session:
        fresh = (
            await session.execute(
                text(
                    f"""
                    UPDATE sources
                       SET title = :title, tags = :tags, collection = :collection,
                           size_bytes = :size, crawl_state = 'ready',
                           last_trained_at = now(), updated_at = now()
                     WHERE id = :id RETURNING {SOURCE_COLUMNS}
                    """  # noqa: S608
                ),
                {
                    "id": source_id,
                    "title": body.title,
                    "tags": body.tags,
                    "collection": body.collection,
                    "size": len(data),
                },
            )
        ).mappings().one()
        await _record(session, tenant, "source.snippet.update", target_id=str(source_id))
    return _source_out(fresh)


@router.get("/snippets/{source_id}", response_model=SnippetDetailOut)
async def get_snippet(
    source_id: UUID, tenant: TenantDep, container: ContainerDep
) -> SnippetDetailOut:
    """The editor's read: the source row plus the text itself from the store.

    The bytes are the truth, not the extracted markdown — a tenant reopening the
    editor must see exactly what they typed.
    """
    async with tenant_scope(tenant.tenant_id) as session:
        row = await _load(session, source_id, kind="snippet")
        child = (
            await session.execute(
                text(
                    "SELECT id FROM documents WHERE source_id = :id "
                    "AND status <> 'deleted' ORDER BY created_at LIMIT 1"
                ),
                {"id": source_id},
            )
        ).mappings().first()

    text_body = ""
    if child is not None:
        from app.adapters.storage.base import document_path

        try:
            data = await container.storage.get(document_path(tenant.tenant_id, child["id"]))
            text_body = data.decode("utf-8", errors="replace")
        except Exception:
            # an unreadable blob must not break the editor; the tenant can retype
            logger.warning("snippet_read_failed", source_id=str(source_id), exc_info=True)
    return SnippetDetailOut(source=_source_out(row), body=text_body)


# ------------------------------------------------------------------------- q&a

# `messages.citations` is an array of citation objects; containment over the
# jsonb_path_ops GIN index added in 006 turns "how often was this pair used" from
# a full scan of the tenant's history into an index probe
_QA_CITED = (
    "citations @> jsonb_build_array(jsonb_build_object('document_id', CAST(:doc AS text)))"
)

QA_PAIR_SELECT = """
    SELECT p.id, p.document_id, p.title, p.questions, p.answer, p.ordinal,
           COALESCE(d.status, 'queued') AS status, d.error AS error,
           COALESCE(m.asked, 0)         AS times_asked,
           m.last_asked                 AS last_asked
      FROM qa_pairs p
      LEFT JOIN documents d ON d.id = p.document_id AND d.status <> 'deleted'
      LEFT JOIN LATERAL (
          SELECT count(*) AS asked, max(created_at) AS last_asked
            FROM messages
           WHERE p.document_id IS NOT NULL
             AND citations @> jsonb_build_array(
                     jsonb_build_object('document_id', CAST(p.document_id AS text)))
      ) m ON true
"""


def _qa_pair_out(row: Any) -> QaPairOut:
    return QaPairOut(
        id=row["id"],
        document_id=row["document_id"],
        title=row["title"],
        questions=list(row["questions"] or []),
        answer=row["answer"],
        status=row["status"],
        error=row["error"],
        times_asked=int(row["times_asked"] or 0),
        last_asked=row["last_asked"],
    )


def _clean_questions(questions: list[str]) -> list[str]:
    """Trim, drop blanks, de-duplicate, and refuse an over-long phrasing.

    A blank variation would become an embedded chunk that matches everything
    weakly, which is worse than not having it.
    """
    seen: list[str] = []
    for question in questions:
        value = question.strip()
        if not value:
            continue
        if len(value) > QA_MAX_QUESTION_CHARS:
            raise ValidationError(
                f"a question may be at most {QA_MAX_QUESTION_CHARS} characters"
            )
        if value.lower() not in {item.lower() for item in seen}:
            seen.append(value)
    if not seen:
        raise ValidationError("at least one question is required")
    return seen


async def _qa_document(
    container: Any,
    tenant: TenantContext,
    source_id: UUID,
    pair_id: UUID,
    title: str,
    answer: str,
) -> UUID:
    """Give a pair its documents row, so nothing downstream needs a Q&A special case.

    No object is stored: the authored pair in `qa_pairs` IS the content, and a
    re-index rebuilds the chunks from it. The sha is over the pair id and the
    answer, which keeps it unique per pair and stable when the same answer is
    saved twice.
    """
    document = await container.documents.create(
        tenant_id=tenant.tenant_id,
        filename=title,
        mime=QA_MIME,
        size_bytes=len(answer.encode("utf-8")),
        sha256=hashlib.sha256(f"{pair_id}:{answer}".encode()).hexdigest(),
        storage_path="",
        agent_id=tenant.agent_id,
        page_count=1,
        source="qa",
        source_id=source_id,
        source_url=None,
    )
    return UUID(str(document.id))


async def _enqueue_qa(
    container: Any, tenant_id: UUID, source_id: UUID, pair_ids: list[UUID]
) -> None:
    """Chunk, embed and index the named pairs off the request path."""
    from app.adapters.queue.base import Job

    try:
        await container.queue.enqueue(
            Job(
                kind="ingest_qa",
                tenant_id=tenant_id,
                payload={
                    "source_id": str(source_id),
                    "pair_ids": [str(pair_id) for pair_id in pair_ids],
                },
            )
        )
    except Exception:
        logger.error("ingest_qa_enqueue_failed", source_id=str(source_id), exc_info=True)
        async with tenant_scope(tenant_id) as session:
            await session.execute(
                text(
                    "UPDATE documents SET status = 'failed', error = :error "
                    "WHERE id IN (SELECT document_id FROM qa_pairs "
                    "WHERE id = ANY(:ids) AND document_id IS NOT NULL)"
                ),
                {
                    "ids": pair_ids,
                    "error": "could not be queued for indexing; save it again to retry",
                },
            )


async def _recount_qa(session: AsyncSession, source_id: UUID) -> None:
    """Recompute a Q&A set's counters from its pairs. Recomputed, never incremented."""
    await session.execute(
        text(
            """
            UPDATE sources s SET
                link_total   = c.total,
                link_trained = c.ready,
                link_pending = c.total - c.ready,
                size_bytes   = c.bytes,
                chunk_count  = c.chunks,
                updated_at   = now()
              FROM (
                SELECT count(*) AS total,
                       count(*) FILTER (WHERE d.status = 'ready') AS ready,
                       COALESCE(sum(octet_length(p.answer)), 0)   AS bytes,
                       COALESCE(sum(d.chunk_count), 0)            AS chunks
                  FROM qa_pairs p
                  LEFT JOIN documents d
                    ON d.id = p.document_id AND d.status <> 'deleted'
                 WHERE p.source_id = :id
              ) c
             WHERE s.id = :id
            """
        ),
        {"id": source_id},
    )


@router.post("/qa", response_model=SourceOut, status_code=202)
async def create_qa_set(
    body: QaSetCreate,
    tenant: TenantDep,
    container: ContainerDep,
    agent_id: UUID | None = Query(default=None),
) -> SourceOut:
    """Create a Q&A set. Each pair becomes one document and one group of chunks.

    A pair is already a chunk, so there is no parse and no OCR — but it still gets
    a documents row, which is what makes delete, quota, retrieval facets and
    citations work with no special case anywhere downstream.
    """
    prepared = [(_clean_questions(pair.questions), pair) for pair in body.pairs]

    async with tenant_scope(tenant.tenant_id) as session:
        row = (
            await session.execute(
                text(
                    f"""
                    INSERT INTO sources (
                        tenant_id, agent_id, kind, title, tags, collection, crawl_state
                    ) VALUES (
                        :tenant_id, :agent_id, 'qa', :title, :tags, :collection, 'idle'
                    )
                    RETURNING {SOURCE_COLUMNS}
                    """  # noqa: S608
                ),
                {
                    "tenant_id": tenant.tenant_id,
                    "agent_id": agent_id,
                    "title": body.title or "Q&A",
                    "tags": body.tags,
                    "collection": body.collection,
                },
            )
        ).mappings().one()
        source_id = row["id"]
        pair_ids: list[UUID] = []
        for index, (questions, pair) in enumerate(prepared):
            pair_id = uuid4()
            await session.execute(
                text(
                    """
                    INSERT INTO qa_pairs (
                        id, tenant_id, source_id, title, questions, answer, ordinal
                    ) VALUES (
                        :id, :tenant_id, :source_id, :title, :questions, :answer, :ordinal
                    )
                    """
                ),
                {
                    "id": pair_id,
                    "tenant_id": tenant.tenant_id,
                    "source_id": source_id,
                    "title": pair.title or questions[0][:200],
                    "questions": questions,
                    "answer": pair.answer,
                    "ordinal": pair.ordinal if pair.ordinal is not None else index,
                },
            )
            pair_ids.append(pair_id)
        await _record(
            session,
            tenant,
            "source.qa.create",
            target_id=str(source_id),
            detail={"pairs": len(pair_ids)},
        )

    # the documents rows are created through the repo so storage, quota and the
    # step machine see them exactly as they see an upload
    for pair_id, (questions, pair) in zip(pair_ids, prepared, strict=True):
        document_id = await _qa_document(
            container,
            tenant,
            source_id,
            pair_id,
            pair.title or questions[0][:200],
            pair.answer,
        )
        async with tenant_scope(tenant.tenant_id) as session:
            await session.execute(
                text("UPDATE qa_pairs SET document_id = :doc WHERE id = :id"),
                {"doc": document_id, "id": pair_id},
            )
        await container.documents.update_metadata(
            tenant.tenant_id, document_id, tags=body.tags, collection=body.collection
        )

    await _enqueue_qa(container, tenant.tenant_id, source_id, pair_ids)

    async with tenant_scope(tenant.tenant_id) as session:
        await _recount_qa(session, source_id)
        fresh = (
            await session.execute(
                text(f"SELECT {SOURCE_COLUMNS} FROM sources WHERE id = :id"),  # noqa: S608
                {"id": source_id},
            )
        ).mappings().one()
    return _source_out(fresh)


@router.get("/qa/{source_id}/pairs", response_model=list[QaPairOut])
async def list_qa_pairs(source_id: UUID, tenant: TenantDep) -> list[QaPairOut]:
    """Every pair in a set, each with how often its answer was actually cited.

    The usage numbers come from one lateral over the citations index rather than a
    query per pair: a 200-pair set would otherwise be 200 round-trips.
    """
    async with tenant_scope(tenant.tenant_id) as session:
        await _load(session, source_id, kind="qa")
        rows = (
            await session.execute(
                text(f"{QA_PAIR_SELECT} WHERE p.source_id = :id ORDER BY p.ordinal, p.created_at"),
                {"id": source_id},
            )
        ).mappings().all()
    return [_qa_pair_out(row) for row in rows]


@router.post("/qa/{source_id}/pairs", response_model=QaPairOut, status_code=202)
async def add_qa_pair(
    source_id: UUID, body: QaPairIn, tenant: TenantDep, container: ContainerDep
) -> QaPairOut:
    """Append one pair to an existing set and index it."""
    questions = _clean_questions(body.questions)
    title = body.title or questions[0][:200]

    async with tenant_scope(tenant.tenant_id) as session:
        source = await _load(session, source_id, kind="qa")
        pair_id = uuid4()
        ordinal = body.ordinal
        if ordinal is None:
            nxt = (
                await session.execute(
                    text(
                        "SELECT COALESCE(max(ordinal), -1) + 1 AS n FROM qa_pairs "
                        "WHERE source_id = :id"
                    ),
                    {"id": source_id},
                )
            ).mappings().one()
            ordinal = int(nxt["n"])
        await session.execute(
            text(
                """
                INSERT INTO qa_pairs (
                    id, tenant_id, source_id, title, questions, answer, ordinal
                ) VALUES (
                    :id, :tenant_id, :source_id, :title, :questions, :answer, :ordinal
                )
                """
            ),
            {
                "id": pair_id,
                "tenant_id": tenant.tenant_id,
                "source_id": source_id,
                "title": title,
                "questions": questions,
                "answer": body.answer,
                "ordinal": ordinal,
            },
        )
        await _record(
            session,
            tenant,
            "source.qa.pair.create",
            target_id=str(pair_id),
            detail={"source_id": str(source_id)},
        )

    document_id = await _qa_document(container, tenant, source_id, pair_id, title, body.answer)
    await container.documents.update_metadata(
        tenant.tenant_id,
        document_id,
        tags=list(source["tags"] or []),
        collection=source["collection"],
    )
    async with tenant_scope(tenant.tenant_id) as session:
        await session.execute(
            text("UPDATE qa_pairs SET document_id = :doc WHERE id = :id"),
            {"doc": document_id, "id": pair_id},
        )
        await _recount_qa(session, source_id)

    await _enqueue_qa(container, tenant.tenant_id, source_id, [pair_id])

    async with tenant_scope(tenant.tenant_id) as session:
        row = (
            await session.execute(
                text(f"{QA_PAIR_SELECT} WHERE p.id = :id"), {"id": pair_id}
            )
        ).mappings().one()
    return _qa_pair_out(row)


@router.put("/qa/pairs/{pair_id}", response_model=QaPairOut, status_code=202)
async def update_qa_pair(
    pair_id: UUID, body: QaPairIn, tenant: TenantDep, container: ContainerDep
) -> QaPairOut:
    """Edit one pair and re-index that pair only.

    Re-chunking one pair is cheap; re-indexing the whole set because one answer
    changed would re-embed every question in it.
    """
    questions = _clean_questions(body.questions)
    async with tenant_scope(tenant.tenant_id) as session:
        current = (
            await session.execute(
                text("SELECT id, source_id, document_id, title FROM qa_pairs WHERE id = :id"),
                {"id": pair_id},
            )
        ).mappings().first()
        if current is None:
            raise NotFoundError("no such Q&A pair")
        title = body.title or questions[0][:200]
        await session.execute(
            text(
                """
                UPDATE qa_pairs
                   SET title = :title, questions = :questions, answer = :answer,
                       ordinal = COALESCE(:ordinal, ordinal), updated_at = now()
                 WHERE id = :id
                """
            ),
            {
                "id": pair_id,
                "title": title,
                "questions": questions,
                "answer": body.answer,
                "ordinal": body.ordinal,
            },
        )
        source_id = current["source_id"]
        await _record(
            session,
            tenant,
            "source.qa.pair.update",
            target_id=str(pair_id),
            detail={"source_id": str(source_id)},
        )

    document_id = current["document_id"]
    if document_id is None:
        # the pair lost its document (deleted from the Files tab); give it a new one
        document_id = await _qa_document(container, tenant, source_id, pair_id, title, body.answer)
        async with tenant_scope(tenant.tenant_id) as session:
            await session.execute(
                text("UPDATE qa_pairs SET document_id = :doc WHERE id = :id"),
                {"doc": document_id, "id": pair_id},
            )
    else:
        await container.documents.replace_content(
            tenant.tenant_id,
            document_id,
            sha256=hashlib.sha256(f"{pair_id}:{body.answer}".encode()).hexdigest(),
            size_bytes=len(body.answer.encode("utf-8")),
            mime=QA_MIME,
            page_count=1,
        )
    await _enqueue_qa(container, tenant.tenant_id, source_id, [pair_id])

    async with tenant_scope(tenant.tenant_id) as session:
        await _recount_qa(session, source_id)
        row = (
            await session.execute(
                text(f"{QA_PAIR_SELECT} WHERE p.id = :id"), {"id": pair_id}
            )
        ).mappings().one()
    return _qa_pair_out(row)


@router.delete("/qa/pairs/{pair_id}", status_code=204)
async def delete_qa_pair(
    pair_id: UUID, tenant: TenantDep, container: ContainerDep
) -> None:
    """Remove one pair, its document and its vectors."""
    async with tenant_scope(tenant.tenant_id) as session:
        current = (
            await session.execute(
                text("SELECT id, source_id, document_id FROM qa_pairs WHERE id = :id"),
                {"id": pair_id},
            )
        ).mappings().first()
        if current is None:
            raise NotFoundError("no such Q&A pair")
        await session.execute(text("DELETE FROM qa_pairs WHERE id = :id"), {"id": pair_id})
        await _record(
            session,
            tenant,
            "source.qa.pair.delete",
            target_id=str(pair_id),
            detail={"source_id": str(current["source_id"])},
        )

    if current["document_id"] is not None:
        # vectors first: a chunk whose pair is gone would keep answering questions
        await container.indexer.remove(tenant.tenant_id, current["document_id"])
        await container.documents.soft_delete(tenant.tenant_id, current["document_id"])

    async with tenant_scope(tenant.tenant_id) as session:
        await _recount_qa(session, current["source_id"])


@router.post("/qa/import", response_model=QaImportResult)
async def import_qa(
    tenant: TenantDep,
    container: ContainerDep,
    file: UploadFile = File(...),
    source_id: UUID | None = Form(default=None),
) -> QaImportResult:
    """Bulk-import pairs from a spreadsheet, reporting per row.

    Deliberately not routed through `UploadGate.run`: the registry would sniff the
    xlsx and hand it to the spreadsheet parser, which would index the sheet as a
    table instead of importing it as pairs. The two controls that matter for a zip
    upload are called directly — the byte cap and the expansion-ratio guard, since
    an xlsx is a zip.

    One bad row never loses the sheet: rows are reported individually and the good
    ones are created.
    """
    data = await file.read(QA_IMPORT_MAX_BYTES + 1)
    if len(data) > QA_IMPORT_MAX_BYTES:
        raise PayloadTooLargeError(
            f"the file must be smaller than {QA_IMPORT_MAX_BYTES // (1024 * 1024)} MB"
        )
    if not data:
        raise ValidationError("the file is empty")

    from app.services.ingestion.gate import check_zip_bomb

    check_zip_bomb(data)

    rows, errors = _parse_qa_workbook(data)
    if not rows:
        return QaImportResult(created=0, errors=errors)

    target = source_id
    async with tenant_scope(tenant.tenant_id) as session:
        if target is not None:
            source = await _load(session, target, kind="qa")
        else:
            created_row = (
                await session.execute(
                    text(
                        f"""
                        INSERT INTO sources (tenant_id, agent_id, kind, title, crawl_state)
                        VALUES (:tenant_id, :agent_id, 'qa', :title, 'idle')
                        RETURNING {SOURCE_COLUMNS}
                        """  # noqa: S608
                    ),
                    {
                        "tenant_id": tenant.tenant_id,
                        "agent_id": tenant.agent_id,
                        "title": (file.filename or "Imported Q&A")[:200],
                    },
                )
            ).mappings().one()
            source = created_row
            target = created_row["id"]

        start = (
            await session.execute(
                text(
                    "SELECT COALESCE(max(ordinal), -1) + 1 AS n FROM qa_pairs "
                    "WHERE source_id = :id"
                ),
                {"id": target},
            )
        ).mappings().one()
        ordinal = int(start["n"])

        pair_ids: list[UUID] = []
        titles: dict[UUID, tuple[str, str]] = {}
        for number, title, questions, answer in rows:
            pair_id = uuid4()
            await session.execute(
                text(
                    """
                    INSERT INTO qa_pairs (
                        id, tenant_id, source_id, title, questions, answer, ordinal
                    ) VALUES (
                        :id, :tenant_id, :source_id, :title, :questions, :answer, :ordinal
                    )
                    """
                ),
                {
                    "id": pair_id,
                    "tenant_id": tenant.tenant_id,
                    "source_id": target,
                    "title": title,
                    "questions": questions,
                    "answer": answer,
                    "ordinal": ordinal,
                },
            )
            titles[pair_id] = (title, answer)
            pair_ids.append(pair_id)
            ordinal += 1
            logger.info("qa_import_row", row=number, pair_id=str(pair_id))
        await _record(
            session,
            tenant,
            "source.qa.import",
            target_id=str(target),
            detail={"created": len(pair_ids), "errors": len(errors)},
        )

    for pair_id in pair_ids:
        title, answer = titles[pair_id]
        document_id = await _qa_document(container, tenant, target, pair_id, title, answer)
        async with tenant_scope(tenant.tenant_id) as session:
            await session.execute(
                text("UPDATE qa_pairs SET document_id = :doc WHERE id = :id"),
                {"doc": document_id, "id": pair_id},
            )
        await container.documents.update_metadata(
            tenant.tenant_id,
            document_id,
            tags=list(source["tags"] or []),
            collection=source["collection"],
        )

    await _enqueue_qa(container, tenant.tenant_id, target, pair_ids)
    async with tenant_scope(tenant.tenant_id) as session:
        await _recount_qa(session, target)
    return QaImportResult(created=len(pair_ids), errors=errors)


_QA_TITLE_HEADERS = ("title", "name", "topic")
_QA_QUESTION_HEADERS = ("question", "questions")
_QA_ANSWER_HEADERS = ("answer", "response")


def _parse_qa_workbook(
    data: bytes,
) -> tuple[list[tuple[int, str, list[str], str]], list[dict[str, Any]]]:
    """Read the first sheet into pairs, plus one error entry per unusable row.

    Header matching is case- and space-insensitive, and numbered variation columns
    (`question 2`, `variation 3`) fold into the same pair, because that is the
    shape support teams already keep their FAQs in.
    """
    import io

    import openpyxl  # type: ignore[import-untyped]

    errors: list[dict[str, Any]] = []
    try:
        workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise ValidationError(f"this file could not be read as a spreadsheet: {exc}") from exc

    sheet = workbook.worksheets[0] if workbook.worksheets else None
    if sheet is None:
        workbook.close()
        raise ValidationError("the spreadsheet has no sheets")

    rows_iter = sheet.iter_rows(values_only=True)
    header = next(rows_iter, None)
    if header is None:
        workbook.close()
        raise ValidationError("the spreadsheet is empty")

    title_at: int | None = None
    answer_at: int | None = None
    question_at: list[int] = []
    for index, cell in enumerate(header):
        name = str(cell or "").strip().lower()
        if not name:
            continue
        collapsed = " ".join(name.split())
        if collapsed in _QA_TITLE_HEADERS and title_at is None:
            title_at = index
        elif collapsed in _QA_ANSWER_HEADERS and answer_at is None:
            answer_at = index
        elif collapsed in _QA_QUESTION_HEADERS or collapsed.startswith(
            ("question", "variation")
        ):
            question_at.append(index)

    if answer_at is None or not question_at:
        workbook.close()
        raise ValidationError(
            "the sheet needs a 'question' column and an 'answer' column in the first row"
        )

    parsed: list[tuple[int, str, list[str], str]] = []
    seen: dict[str, int] = {}
    for offset, row in enumerate(rows_iter, start=2):
        if len(parsed) >= QA_IMPORT_MAX_ROWS:
            errors.append(
                {
                    "row": offset,
                    "status": "skipped",
                    "reason": f"only the first {QA_IMPORT_MAX_ROWS} rows are imported",
                }
            )
            break
        if row is None or all(cell is None or str(cell).strip() == "" for cell in row):
            continue

        def cell_at(index: int, current: tuple[Any, ...] = row) -> str:
            return str(current[index]).strip() if index < len(current) and current[index] else ""

        answer = cell_at(answer_at)
        questions: list[str] = []
        for index in question_at:
            raw = cell_at(index)
            for part in raw.replace(";", "\n").split("\n"):
                value = part.strip()
                if value and len(value) <= QA_MAX_QUESTION_CHARS:
                    questions.append(value)

        if not questions:
            errors.append({"row": offset, "status": "error", "reason": "question is empty"})
            continue
        if not answer:
            errors.append({"row": offset, "status": "error", "reason": "answer is empty"})
            continue
        if len(answer) > QA_MAX_ANSWER_CHARS:
            errors.append(
                {
                    "row": offset,
                    "status": "error",
                    "reason": f"answer is longer than {QA_MAX_ANSWER_CHARS} characters",
                }
            )
            continue

        key = questions[0].lower()
        if key in seen:
            errors.append(
                {"row": offset, "status": "skipped", "reason": f"duplicate of row {seen[key]}"}
            )
            continue
        seen[key] = offset

        title = cell_at(title_at) if title_at is not None else ""
        deduped: list[str] = []
        for question in questions:
            if question.lower() not in {item.lower() for item in deduped}:
                deduped.append(question)
        parsed.append(
            (offset, (title or deduped[0])[:200], deduped[:QA_MAX_VARIATIONS], answer)
        )

    workbook.close()
    return parsed, errors


@router.get("/qa/pairs/{pair_id}/activity", response_model=QaActivityOut)
async def qa_pair_activity(
    pair_id: UUID,
    tenant: TenantDep,
    days: int = Query(default=30, ge=1, le=365),
) -> QaActivityOut:
    """How often this pair's answer was cited, per day.

    Derived from `messages.citations` rather than from a counter, because a counter
    would have to be written on the answer path — where a failed increment either
    loses the number or fails the answer.
    """
    async with tenant_scope(tenant.tenant_id) as session:
        pair = (
            await session.execute(
                text("SELECT id, document_id FROM qa_pairs WHERE id = :id"),
                {"id": pair_id},
            )
        ).mappings().first()
        if pair is None:
            raise NotFoundError("no such Q&A pair")
        if pair["document_id"] is None:
            return QaActivityOut(points=[], times_asked=0, last_asked=None)

        params = {"doc": str(pair["document_id"]), "days": days}
        points = (
            await session.execute(
                text(
                    f"""
                    SELECT to_char(date_trunc('day', created_at), 'YYYY-MM-DD') AS day,
                           count(*) AS asked
                      FROM messages
                     WHERE created_at >= now() - make_interval(days => :days)
                       AND {_QA_CITED}
                     GROUP BY 1 ORDER BY 1
                    """  # noqa: S608
                ),
                params,
            )
        ).mappings().all()
        totals = (
            await session.execute(
                text(
                    f"""
                    SELECT count(*) AS asked, max(created_at) AS last_asked
                      FROM messages
                     WHERE created_at >= now() - make_interval(days => :window)
                       AND {_QA_CITED}
                    """  # noqa: S608
                ),
                {"doc": str(pair["document_id"]), "window": ACTIVITY_MAX_WINDOW_DAYS},
            )
        ).mappings().one()

    return QaActivityOut(
        points=[
            QaActivityPoint(day=str(row["day"]), asked=int(row["asked"])) for row in points
        ],
        times_asked=int(totals["asked"] or 0),
        last_asked=totals["last_asked"],
    )
