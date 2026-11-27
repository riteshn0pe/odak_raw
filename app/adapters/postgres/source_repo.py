"""Postgres access for sources, the crawl frontier, and Q&A pairs.

Raw `text()` SQL through SQLAlchemy Core under `tenant_scope`, like every other
repository here — RLS is the isolation mechanism, so the tenant context is what
makes a query safe rather than a `WHERE tenant_id = …` a future edit could drop.

Two things in this file are not ordinary CRUD and are the reason it exists:

**The frontier claim.** `claim_pending` is `SELECT … FOR UPDATE SKIP LOCKED`
followed by an UPDATE to `fetching` in one transaction. That is what makes a wave
resumable: a redelivered QStash job, or a second wave that overlaps the first,
claims a *different* slice instead of re-fetching the same pages. `claimed_at` is
the lease stamp, so a wave that dies mid-fetch has its rows reclaimed by TTL
rather than leaving them stuck in `fetching` forever.

**Counters are recomputed, never incremented.** `recompute_counters` derives every
`sources.link_*` value from `source_links` in a single statement. An incremented
counter is wrong the first time a job is redelivered, and a crawl is a
serverless, at-least-once pipeline where redelivery is normal. Recomputation is
idempotent, so running it twice is free.

The two cross-tenant selectors (`due_sources`, `stalled_sources`) run under
`admin_scope()` because a scheduler has no tenant. They return `(id, tenant_id)`
only: every subsequent read or write goes back through `tenant_scope`, so no
cross-tenant write path exists.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any
from uuid import UUID, uuid4

from app.core.db import admin_scope, tenant_scope
from app.core.logging import get_logger
from sqlalchemy import text

logger = get_logger(__name__)

# states a link can be in. mirrors the CHECK constraint in 006_sources.sql
LINK_STATES = (
    "pending",
    "fetching",
    "fetched",
    "trained",
    "failed",
    "excluded",
    "unchanged",
    "gone",
)

# what a link is in while the fetch frontier still owes work. NOT the same set as
# `link_pending` in recompute_counters, which also counts 'fetched': a gated crawl
# parks with every row 'fetched', so a completion test that included it would
# never fire and the wave would re-enqueue itself forever
FETCH_FRONTIER_STATES = ("pending", "fetching")

# the rows a review gate is asking a human about: fetched but not yet trained,
# plus the already-trained rows of a re-crawl, which the tenant can still drop
REVIEWABLE_STATES = ("fetched", "trained", "unchanged")

SOURCE_COLUMNS = """
    id, tenant_id, agent_id, kind, title, seed_url, origin, config, tags,
    collection, crawl_state, error, link_total, link_trained, link_failed,
    link_excluded, link_pending, size_bytes, chunk_count, boilerplate,
    auto_retrain_hours, pending_changes, crawl_started_at, last_trained_at,
    last_checked_at, review_approved_at, created_at, updated_at
"""

LINK_COLUMNS = """
    id, tenant_id, source_id, url, url_hash, depth, discovered_via, state,
    reason, reason_detail, http_status, content_type, etag, last_modified,
    content_hash, simhash, content_bytes, title, document_id, lastmod, raw_path,
    attempt, claimed_at, first_seen_at, fetched_at, updated_at
"""

# a bulk action is bounded so one request cannot rewrite a 10k-page frontier
MAX_BULK_LINKS = 500


class SourceRepository:
    """Sources, source_links and qa_pairs. No business rules, only storage."""

    # ------------------------------------------------------------------ sources

    async def create_source(
        self,
        tenant_id: UUID,
        *,
        kind: str,
        title: str,
        seed_url: str | None = None,
        origin: str | None = None,
        config: dict[str, Any] | None = None,
        tags: list[str] | None = None,
        collection: str | None = None,
        agent_id: UUID | None = None,
        auto_retrain_hours: int = 0,
        crawl_state: str = "idle",
    ) -> dict[str, Any]:
        source_id = uuid4()
        async with tenant_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(
                            f"""
                            INSERT INTO sources (
                                id, tenant_id, agent_id, kind, title, seed_url, origin,
                                config, tags, collection, crawl_state, auto_retrain_hours
                            ) VALUES (
                                :id, :tenant_id, :agent_id, :kind, :title, :seed_url, :origin,
                                CAST(:config AS jsonb), :tags, :collection, :crawl_state,
                                :auto_retrain_hours
                            )
                            RETURNING {SOURCE_COLUMNS}
                            """  # noqa: S608 - SOURCE_COLUMNS is a module constant
                        ),
                        {
                            "id": source_id,
                            "tenant_id": tenant_id,
                            "agent_id": agent_id,
                            "kind": kind,
                            "title": title,
                            "seed_url": seed_url,
                            "origin": origin,
                            "config": json.dumps(config or {}),
                            "tags": tags or [],
                            "collection": collection,
                            "crawl_state": crawl_state,
                            "auto_retrain_hours": auto_retrain_hours,
                        },
                    )
                )
                .mappings()
                .first()
            )
        return dict(row) if row else {}

    async def get_source(self, tenant_id: UUID, source_id: UUID) -> dict[str, Any] | None:
        async with tenant_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(f"SELECT {SOURCE_COLUMNS} FROM sources WHERE id = :id"),  # noqa: S608
                        {"id": source_id},
                    )
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    async def list_sources(
        self,
        tenant_id: UUID,
        *,
        kind: str | None = None,
        crawl_state: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if kind:
            clauses.append("kind = :kind")
            params["kind"] = kind
        if crawl_state:
            clauses.append("crawl_state = :crawl_state")
            params["crawl_state"] = crawl_state
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        async with tenant_scope(tenant_id) as session:
            rows = (
                (
                    await session.execute(
                        text(
                            f"SELECT {SOURCE_COLUMNS} FROM sources {where} "  # noqa: S608
                            "ORDER BY created_at DESC LIMIT :limit OFFSET :offset"
                        ),
                        params,
                    )
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    async def update_source(
        self,
        tenant_id: UUID,
        source_id: UUID,
        *,
        title: str | None = None,
        config: dict[str, Any] | None = None,
        tags: list[str] | None = None,
        collection: str | None = None,
        collection_set: bool = False,
        agent_id: UUID | None = None,
        agent_set: bool = False,
        auto_retrain_hours: int | None = None,
    ) -> dict[str, Any] | None:
        """Partial update. Only supplied fields are written.

        `collection_set` / `agent_set` distinguish "leave alone" from "clear to
        NULL", the same convention `DocumentRepository.update_metadata` uses.
        """
        sets: list[str] = ["updated_at = now()"]
        params: dict[str, Any] = {"id": source_id}
        if title is not None:
            sets.append("title = :title")
            params["title"] = title
        if config is not None:
            sets.append("config = CAST(:config AS jsonb)")
            params["config"] = json.dumps(config)
        if tags is not None:
            sets.append("tags = :tags")
            params["tags"] = tags
        if collection_set:
            sets.append("collection = :collection")
            params["collection"] = collection
        if agent_set:
            sets.append("agent_id = :agent_id")
            params["agent_id"] = agent_id
        if auto_retrain_hours is not None:
            sets.append("auto_retrain_hours = :auto_retrain_hours")
            params["auto_retrain_hours"] = auto_retrain_hours

        async with tenant_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(
                            f"UPDATE sources SET {', '.join(sets)} "  # noqa: S608
                            f"WHERE id = :id RETURNING {SOURCE_COLUMNS}"
                        ),
                        params,
                    )
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    async def set_crawl_state(
        self,
        tenant_id: UUID,
        source_id: UUID,
        state: str,
        *,
        error: str | None = None,
        clear_error: bool = False,
        origin: str | None = None,
        started: bool = False,
        checked: bool = False,
        trained: bool = False,
        pending_changes: bool | None = None,
        review_approved: bool | None = None,
    ) -> None:
        """Move a source's crawl state and stamp the timestamps that go with it.

        `updated_at` is always touched, because the stall sweeper's only signal
        that a wave is alive is that its source row moved recently.

        `review_approved` True stamps `review_approved_at`, False clears it. It
        rides along with the state change on purpose: approving a parked crawl and
        resuming it are one decision, and two statements could leave a source
        approved but still parked, or running with the gate still armed.
        """
        sets: list[str] = ["crawl_state = :state", "updated_at = now()"]
        params: dict[str, Any] = {"id": source_id, "state": state}
        if error is not None:
            sets.append("error = :error")
            params["error"] = error[:2000]
        elif clear_error:
            sets.append("error = NULL")
        if origin is not None:
            sets.append("origin = :origin")
            params["origin"] = origin
        if started:
            sets.append("crawl_started_at = now()")
        if checked:
            sets.append("last_checked_at = now()")
        if trained:
            sets.append("last_trained_at = now()")
        if pending_changes is not None:
            sets.append("pending_changes = :pending_changes")
            params["pending_changes"] = pending_changes
        if review_approved is not None:
            sets.append(
                "review_approved_at = now()" if review_approved else "review_approved_at = NULL"
            )

        async with tenant_scope(tenant_id) as session:
            await session.execute(
                text(f"UPDATE sources SET {', '.join(sets)} WHERE id = :id"),  # noqa: S608
                params,
            )

    async def touch_source(self, tenant_id: UUID, source_id: UUID) -> None:
        """Heartbeat for a long wave, so the stall sweeper leaves it alone."""
        async with tenant_scope(tenant_id) as session:
            await session.execute(
                text("UPDATE sources SET updated_at = now() WHERE id = :id"),
                {"id": source_id},
            )

    async def set_boilerplate(
        self, tenant_id: UUID, source_id: UUID, fingerprints: list[str]
    ) -> None:
        """Freeze the per-source boilerplate fingerprint set.

        Written once per crawl and reused on every recrawl, which is what keeps
        extraction single-pass: no page is ever re-indexed just to strip a nav bar
        that a later page revealed.
        """
        async with tenant_scope(tenant_id) as session:
            await session.execute(
                text(
                    "UPDATE sources SET boilerplate = CAST(:value AS jsonb), "
                    "updated_at = now() WHERE id = :id"
                ),
                {"id": source_id, "value": json.dumps(fingerprints)},
            )

    async def delete_source(self, tenant_id: UUID, source_id: UUID) -> list[UUID]:
        """Delete a source, returning its child document ids.

        The documents are *not* deleted here: they carry vectors, and removing a
        vector is an adapter call the caller has to make. Returning the ids lets
        the service remove them from the index first and soft-delete them after,
        in that order, so no document is left indexed with no row behind it.
        """
        async with tenant_scope(tenant_id) as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT id FROM documents "
                            "WHERE source_id = :source_id AND status <> 'deleted'"
                        ),
                        {"source_id": source_id},
                    )
                )
                .mappings()
                .all()
            )
            await session.execute(
                text("DELETE FROM sources WHERE id = :id"), {"id": source_id}
            )
        return [UUID(str(row["id"])) for row in rows]

    async def apply_facets_to_children(
        self,
        tenant_id: UUID,
        source_id: UUID,
        *,
        tags: list[str] | None = None,
        collection: str | None = None,
        collection_set: bool = False,
    ) -> int:
        """Push a parent's tags/collection onto every child document.

        One statement rather than a per-document `update_metadata` call: a 500-page
        source would otherwise be 500 round trips to change one tag, and a
        retrieval facet set on the parent is meant to cover the whole site.
        """
        sets: list[str] = []
        params: dict[str, Any] = {"source_id": source_id}
        if tags is not None:
            sets.append("tags = :tags")
            params["tags"] = tags
        if collection_set:
            sets.append("collection = :collection")
            params["collection"] = collection
        if not sets:
            return 0
        async with tenant_scope(tenant_id) as session:
            result = await session.execute(
                text(
                    f"UPDATE documents SET {', '.join(sets)} "  # noqa: S608
                    "WHERE source_id = :source_id AND status <> 'deleted'"
                ),
                params,
            )
        return int(getattr(result, "rowcount", 0) or 0)
    # ------------------------------------------------------------------ links

    async def insert_links(
        self, tenant_id: UUID, source_id: UUID, rows: list[dict[str, Any]]
    ) -> int:
        """Add discovered URLs to the frontier, ignoring ones already known.

        `ON CONFLICT (source_id, url_hash) DO NOTHING` is what makes discovery
        idempotent *and* what preserves the change ledger: a known URL keeps its
        `etag`, `content_hash` and `state`, which is exactly what the conditional
        request path depends on across cycles.

        One statement over unnested arrays rather than executemany, because the
        rowcount then tells the caller how many URLs were genuinely new — which is
        the signal the probe uses to set `pending_changes`.
        """
        if not rows:
            return 0
        async with tenant_scope(tenant_id) as session:
            result = await session.execute(
                text(
                    """
                    INSERT INTO source_links (
                        tenant_id, source_id, url, url_hash, depth, discovered_via,
                        state, reason, reason_detail, lastmod
                    )
                    SELECT :tenant_id, :source_id, u.url, u.url_hash, u.depth, u.via,
                           u.state, u.reason, u.reason_detail, u.lastmod
                      FROM unnest(
                               CAST(:urls AS text[]),
                               CAST(:hashes AS text[]),
                               CAST(:depths AS int[]),
                               CAST(:vias AS text[]),
                               CAST(:states AS text[]),
                               CAST(:reasons AS text[]),
                               CAST(:details AS text[]),
                               CAST(:lastmods AS timestamptz[])
                           ) AS u(url, url_hash, depth, via, state, reason,
                                  reason_detail, lastmod)
                    ON CONFLICT (source_id, url_hash) DO NOTHING
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "source_id": source_id,
                    "urls": [row["url"] for row in rows],
                    "hashes": [row["url_hash"] for row in rows],
                    "depths": [int(row.get("depth", 0)) for row in rows],
                    "vias": [row.get("discovered_via", "seed") for row in rows],
                    "states": [row.get("state", "pending") for row in rows],
                    "reasons": [row.get("reason") for row in rows],
                    "details": [row.get("reason_detail") for row in rows],
                    "lastmods": [row.get("lastmod") for row in rows],
                },
            )
        return int(getattr(result, "rowcount", 0) or 0)

    async def claim_pending(
        self, tenant_id: UUID, source_id: UUID, limit: int
    ) -> list[dict[str, Any]]:
        """Claim a slice of the frontier for fetching.

        `FOR UPDATE SKIP LOCKED` is the whole design: two overlapping waves — the
        normal state of affairs when QStash redelivers — claim disjoint slices
        instead of fetching the same pages twice. `claimed_at` is the lease stamp
        so a wave that dies has its slice reclaimed by TTL.

        Ordered by depth then discovery time, so a crawl is breadth-first: the
        pages nearest the seed are the ones a tenant cares about if the budget
        runs out.
        """
        async with tenant_scope(tenant_id) as session:
            rows = (
                (
                    await session.execute(
                        text(
                            f"""
                            WITH claimed AS (
                                -- aliased because RETURNING sees both this and the
                                -- target table: a bare `id` there is ambiguous
                                SELECT id AS link_id FROM source_links
                                 WHERE source_id = :source_id AND state = 'pending'
                                 ORDER BY depth ASC, first_seen_at ASC
                                 LIMIT :limit
                                 FOR UPDATE SKIP LOCKED
                            )
                            UPDATE source_links AS l
                               SET state = 'fetching',
                                   claimed_at = now(),
                                   attempt = l.attempt + 1,
                                   updated_at = now()
                              FROM claimed
                             WHERE l.id = claimed.link_id
                            RETURNING {LINK_COLUMNS}
                            """  # noqa: S608 - LINK_COLUMNS is a module constant
                        ),
                        {"source_id": source_id, "limit": limit},
                    )
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    async def claim_fetched(
        self, tenant_id: UUID, source_id: UUID, limit: int, *, ttl_seconds: int = 300
    ) -> list[dict[str, Any]]:
        """Claim fetched-but-not-extracted pages for the extract phase.

        The state stays `fetched` — the raw bytes are still the truth until a
        document exists — and the lease is `claimed_at`, refreshed here and
        honoured by the staleness clause so two extract phases cannot process one
        page twice.
        """
        async with tenant_scope(tenant_id) as session:
            rows = (
                (
                    await session.execute(
                        text(
                            f"""
                            WITH claimed AS (
                                -- aliased for the same reason as `claim_pending`
                                SELECT id AS link_id FROM source_links
                                 WHERE source_id = :source_id
                                   AND state = 'fetched'
                                   AND (claimed_at IS NULL
                                        OR claimed_at < now()
                                           - make_interval(secs => :ttl))
                                 ORDER BY fetched_at ASC
                                 LIMIT :limit
                                 FOR UPDATE SKIP LOCKED
                            )
                            UPDATE source_links AS l
                               SET claimed_at = now(), updated_at = now()
                              FROM claimed
                             WHERE l.id = claimed.link_id
                            RETURNING {LINK_COLUMNS}
                            """  # noqa: S608 - LINK_COLUMNS is a module constant
                        ),
                        {"source_id": source_id, "limit": limit, "ttl": float(ttl_seconds)},
                    )
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    async def reclaim_stale(self, tenant_id: UUID, source_id: UUID, ttl_seconds: int) -> int:
        """Return abandoned `fetching` claims to `pending`.

        A wave that was killed mid-fetch — a serverless timeout, a deploy — leaves
        rows claimed. Without this they are stuck forever and the crawl can never
        finish, which is the failure mode that makes a queue-driven crawler look
        broken while the queue looks healthy.
        """
        async with tenant_scope(tenant_id) as session:
            result = await session.execute(
                text(
                    """
                    UPDATE source_links
                       SET state = 'pending', claimed_at = NULL, updated_at = now()
                     WHERE source_id = :source_id
                       AND state = 'fetching'
                       AND claimed_at IS NOT NULL
                       AND claimed_at < now() - make_interval(secs => :ttl)
                    """
                ),
                {"source_id": source_id, "ttl": float(ttl_seconds)},
            )
        return int(getattr(result, "rowcount", 0) or 0)

    async def update_link(
        self,
        tenant_id: UUID,
        link_id: UUID,
        *,
        state: str | None = None,
        reason: str | None = None,
        reason_set: bool = False,
        reason_detail: str | None = None,
        http_status: int | None = None,
        content_type: str | None = None,
        etag: str | None = None,
        etag_set: bool = False,
        last_modified: str | None = None,
        last_modified_set: bool = False,
        content_hash: str | None = None,
        simhash: int | None = None,
        content_bytes: int | None = None,
        title: str | None = None,
        document_id: UUID | None = None,
        document_set: bool = False,
        raw_path: str | None = None,
        raw_path_set: bool = False,
        lastmod: Any | None = None,
        fetched: bool = False,
        clear_claim: bool = False,
        reset_attempt: bool = False,
    ) -> None:
        """Write one link's outcome. Only supplied fields are touched.

        The `*_set` flags exist because NULL is meaningful here: clearing `etag`
        after a page stopped sending one, or clearing `raw_path` once the bytes are
        consumed, is different from "do not change it".
        """
        sets: list[str] = ["updated_at = now()"]
        params: dict[str, Any] = {"id": link_id}

        simple: list[tuple[str, Any]] = [
            ("state", state),
            ("reason_detail", reason_detail),
            ("http_status", http_status),
            ("content_type", content_type),
            ("content_hash", content_hash),
            ("simhash", simhash),
            ("content_bytes", content_bytes),
            ("title", title),
            ("lastmod", lastmod),
        ]
        for column, value in simple:
            if value is not None:
                sets.append(f"{column} = :{column}")
                params[column] = value

        if reason is not None or reason_set:
            sets.append("reason = :reason")
            params["reason"] = reason
        if etag is not None or etag_set:
            sets.append("etag = :etag")
            params["etag"] = etag
        if last_modified is not None or last_modified_set:
            sets.append("last_modified = :last_modified")
            params["last_modified"] = last_modified
        if document_id is not None or document_set:
            sets.append("document_id = :document_id")
            params["document_id"] = document_id
        if raw_path is not None or raw_path_set:
            sets.append("raw_path = :raw_path")
            params["raw_path"] = raw_path
        if fetched:
            sets.append("fetched_at = now()")
        if clear_claim:
            sets.append("claimed_at = NULL")
        if reset_attempt:
            sets.append("attempt = 0")

        async with tenant_scope(tenant_id) as session:
            await session.execute(
                text(f"UPDATE source_links SET {', '.join(sets)} WHERE id = :id"),  # noqa: S608
                params,
            )

    async def bulk_set_state(
        self,
        tenant_id: UUID,
        source_id: UUID,
        link_ids: list[UUID],
        *,
        state: str,
        reason: str | None = None,
        clear_document: bool = False,
    ) -> int:
        """Set many links to one state in a single statement.

        Bounded at `MAX_BULK_LINKS` so a bulk action cannot rewrite a 10k-page
        frontier in one request; the caller recomputes counters once afterwards
        rather than once per link.
        """
        if not link_ids:
            return 0
        ids = link_ids[:MAX_BULK_LINKS]
        sets = ["state = :state", "reason = :reason", "updated_at = now()"]
        if clear_document:
            sets.append("document_id = NULL")
        async with tenant_scope(tenant_id) as session:
            result = await session.execute(
                text(
                    f"UPDATE source_links SET {', '.join(sets)} "  # noqa: S608
                    "WHERE source_id = :source_id AND id = ANY(CAST(:ids AS uuid[]))"
                ),
                {
                    "source_id": source_id,
                    "ids": [str(value) for value in ids],
                    "state": state,
                    "reason": reason,
                },
            )
        return int(getattr(result, "rowcount", 0) or 0)

    async def cap_frontier(
        self,
        tenant_id: UUID,
        source_id: UUID,
        *,
        max_pages: int,
        reason: str,
        reason_detail: str | None = None,
    ) -> int:
        """Refuse the pending rows that do not fit inside `max_pages`.

        One statement, and it keeps a budget's worth rather than refusing the whole
        frontier: a tenant on a hundred-page plan who approves five hundred pages
        gets their first hundred crawled, not nothing at all. Shallowest first, then
        oldest, which is the order the fetch phase would have reached them in — so
        the pages kept are the pages a crawl without a budget would have read first.

        `max_pages=0` refuses everything still pending, which is what the wall-clock
        budget wants: no page is worth keeping over another once time is up.

        Rows already fetched, trained or unchanged count against the budget and are
        never touched here — they are the pages this source already has.
        """
        async with tenant_scope(tenant_id) as session:
            result = await session.execute(
                text(
                    """
                    WITH kept AS (
                        SELECT count(*) AS n FROM source_links
                        WHERE source_id = :source_id
                          AND state NOT IN ('pending', 'excluded', 'failed')
                    ), ranked AS (
                        SELECT id, row_number() OVER (
                            ORDER BY depth ASC, first_seen_at ASC
                        ) AS rn
                        FROM source_links
                        WHERE source_id = :source_id AND state = 'pending'
                    )
                    UPDATE source_links AS l
                    SET state = 'excluded',
                        reason = :reason,
                        reason_detail = :reason_detail,
                        updated_at = now()
                    FROM ranked AS r, kept AS k
                    WHERE l.id = r.id
                      AND r.rn > GREATEST(0, :max_pages - k.n)
                    """
                ),
                {
                    "source_id": source_id,
                    "max_pages": max_pages,
                    "reason": reason,
                    "reason_detail": reason_detail,
                },
            )
        return int(getattr(result, "rowcount", 0) or 0)

    async def get_link(self, tenant_id: UUID, link_id: UUID) -> dict[str, Any] | None:
        async with tenant_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(f"SELECT {LINK_COLUMNS} FROM source_links WHERE id = :id"),  # noqa: S608
                        {"id": link_id},
                    )
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    async def get_link_by_hash(
        self, tenant_id: UUID, source_id: UUID, url_hash: str
    ) -> dict[str, Any] | None:
        async with tenant_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(
                            f"SELECT {LINK_COLUMNS} FROM source_links "  # noqa: S608
                            "WHERE source_id = :source_id AND url_hash = :url_hash"
                        ),
                        {"source_id": source_id, "url_hash": url_hash},
                    )
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    async def list_links(
        self,
        tenant_id: UUID,
        source_id: UUID,
        *,
        state: str | None = None,
        query: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses = ["source_id = :source_id"]
        params: dict[str, Any] = {"source_id": source_id, "limit": limit, "offset": offset}
        if state:
            clauses.append("state = :state")
            params["state"] = state
        if query:
            # pg_trgm-friendly prefix/substring match on the url and title, the
            # same shape document search already uses
            clauses.append("(url ILIKE :query OR coalesce(title, '') ILIKE :query)")
            params["query"] = f"%{query}%"
        async with tenant_scope(tenant_id) as session:
            rows = (
                (
                    await session.execute(
                        text(
                            f"SELECT {LINK_COLUMNS} FROM source_links "  # noqa: S608
                            f"WHERE {' AND '.join(clauses)} "
                            "ORDER BY depth ASC, first_seen_at ASC LIMIT :limit OFFSET :offset"
                        ),
                        params,
                    )
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    async def count_links(
        self,
        tenant_id: UUID,
        source_id: UUID,
        *,
        state: str | None = None,
        states: Sequence[str] | None = None,
    ) -> int:
        clauses = ["source_id = :source_id"]
        params: dict[str, Any] = {"source_id": source_id}
        if state:
            clauses.append("state = :state")
            params["state"] = state
        elif states:
            # the review gate counts several states at once — the fetch frontier is
            # 'pending' plus 'fetching' — which one equality cannot express
            clauses.append("state = ANY(CAST(:states AS text[]))")
            params["states"] = list(states)
        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT count(*) AS n FROM source_links "  # noqa: S608
                        f"WHERE {' AND '.join(clauses)}"
                    ),
                    params,
                )
            ).first()
        return int(row[0]) if row else 0

    async def find_content_match(
        self, tenant_id: UUID, source_id: UUID, content_hash: str, *, exclude_id: UUID | None = None
    ) -> dict[str, Any] | None:
        """An existing page in this source with byte-identical extracted markdown.

        Checked before the simhash scan because it is an index lookup rather than a
        scan, and an exact match is the common case: print views, `?page=1` aliases
        and `/about` vs `/about-us` all extract identically.
        """
        clauses = [
            "source_id = :source_id",
            "content_hash = :content_hash",
            "state IN ('trained', 'unchanged')",
        ]
        params: dict[str, Any] = {"source_id": source_id, "content_hash": content_hash}
        if exclude_id is not None:
            clauses.append("id <> :exclude_id")
            params["exclude_id"] = exclude_id
        async with tenant_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT id, url, document_id FROM source_links "  # noqa: S608
                            f"WHERE {' AND '.join(clauses)} LIMIT 1"
                        ),
                        params,
                    )
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    async def simhash_candidates(
        self, tenant_id: UUID, source_id: UUID, *, limit: int = 5000, exclude_id: UUID | None = None
    ) -> list[tuple[str, int]]:
        """`(url, simhash)` for pages already kept in this source.

        Near-duplicate detection is scoped to one source on purpose: two tenants,
        or two sites belonging to one tenant, legitimately say the same thing, and
        a cross-source dedupe would silently drop a page a tenant paid to add.
        """
        clauses = [
            "source_id = :source_id",
            "simhash IS NOT NULL",
            "state IN ('trained', 'unchanged')",
        ]
        params: dict[str, Any] = {"source_id": source_id, "limit": limit}
        if exclude_id is not None:
            clauses.append("id <> :exclude_id")
            params["exclude_id"] = exclude_id
        async with tenant_scope(tenant_id) as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT url, simhash FROM source_links "  # noqa: S608
                            f"WHERE {' AND '.join(clauses)} LIMIT :limit"
                        ),
                        params,
                    )
                )
                .mappings()
                .all()
            )
        return [(str(row["url"]), int(row["simhash"])) for row in rows]

    async def park_for_review(
        self, tenant_id: UUID, source_id: UUID, *, states: Sequence[str]
    ) -> int:
        """Count the review frontier and park the source in one transaction.

        The engine used to do this as `count_links` then `set_crawl_state` — two
        `tenant_scope` round trips for one decision. Against a database a continent
        away that is over two seconds spent deciding something the database already
        knew in the first statement, on the critical path between a sitemap read and
        the tenant seeing their link list. Returns the count so the caller can tell
        an empty frontier (0, nothing parked) from a parked one.
        """
        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT count(*) AS n FROM source_links "
                        "WHERE source_id = :source_id AND state = ANY(CAST(:states AS text[]))"
                    ),
                    {"source_id": source_id, "states": list(states)},
                )
            ).first()
            reviewable = int(row[0]) if row else 0
            if reviewable > 0:
                await session.execute(
                    text(
                        "UPDATE sources SET crawl_state = 'awaiting_review', "
                        "last_checked_at = now(), error = NULL, updated_at = now() "
                        "WHERE id = :source_id"
                    ),
                    {"source_id": source_id},
                )
        return reviewable

    async def pending_count(self, tenant_id: UUID, source_id: UUID) -> int:
        return await self.count_links(tenant_id, source_id, state="pending")

    async def has_unextracted(self, tenant_id: UUID, source_id: UUID) -> bool:
        """True while any link is still fetched-but-not-extracted."""
        return await self.count_links(tenant_id, source_id, state="fetched") > 0

    async def fetch_frontier_count(self, tenant_id: UUID, source_id: UUID) -> int:
        """How many links the fetch phase still owes work on.

        The review gate's completion test. `link_pending` cannot serve it: that
        counter also counts 'fetched', which is exactly the state a gated crawl
        parks every row in, so the wave would never see the frontier as drained.
        """
        return await self.count_links(
            tenant_id, source_id, states=FETCH_FRONTIER_STATES
        )

    # The review gate's two reads live in `app/api/v1/sources.py`, not here: that
    # router builds every query as `text()` SQL inside one `tenant_scope`, and a
    # repository method opens a session of its own, so calling one from inside a
    # request would nest sessions for a plain read.
    #
    # Its writes reuse `POST /links/bulk` rather than a prefix UPDATE, because that
    # endpoint already does the three things a bare state change would miss: it
    # records each `url_hash` in `config.force_exclude`, it removes the vectors and
    # soft-deletes the document for any row that was already trained, and it
    # recomputes the counters once.
    # ------------------------------------------------------------------ counters

    async def recompute_counters(self, tenant_id: UUID, source_id: UUID) -> dict[str, int]:
        """Recompute every denormalised counter on a source from its rows.

        RECOMPUTED, never incremented. A crawl is an at-least-once serverless
        pipeline: QStash redelivers, waves overlap, a link is claimed twice after a
        lease expiry. Every one of those makes `counter = counter + 1` wrong, and
        wrong counters are what a tenant sees on the dashboard. A single derived
        statement is idempotent, so running it twice costs nothing and fixes
        anything a crash left behind.

        Called once per wave, not once per page.
        """
        async with tenant_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(
                            """
                            WITH link_counts AS (
                                SELECT
                                    count(*) AS total,
                                    count(*) FILTER (
                                        WHERE state IN ('trained', 'unchanged')) AS trained,
                                    count(*) FILTER (
                                        WHERE state IN ('failed', 'gone')) AS failed,
                                    count(*) FILTER (WHERE state = 'excluded') AS excluded,
                                    count(*) FILTER (
                                        WHERE state IN ('pending', 'fetching', 'fetched')
                                    ) AS pending,
                                    coalesce(sum(content_bytes), 0) AS bytes
                                  FROM source_links
                                 WHERE source_id = :source_id
                            ),
                            doc_counts AS (
                                SELECT
                                    count(*) AS total,
                                    count(*) FILTER (WHERE status = 'ready') AS ready,
                                    count(*) FILTER (WHERE status = 'failed') AS failed,
                                    coalesce(sum(size_bytes), 0) AS bytes,
                                    coalesce(sum(chunk_count), 0) AS chunks
                                  FROM documents
                                 WHERE source_id = :source_id AND status <> 'deleted'
                            )
                            UPDATE sources SET
                                -- a snippet or Q&A source has no frontier, so its
                                -- counters describe its documents instead. one
                                -- statement keeps the two kinds reporting the same
                                -- shape to the dashboard
                                link_total = CASE WHEN l.total > 0
                                                  THEN l.total ELSE d.total END,
                                link_trained = CASE WHEN l.total > 0
                                                    THEN l.trained ELSE d.ready END,
                                link_failed = CASE WHEN l.total > 0
                                                   THEN l.failed ELSE d.failed END,
                                link_excluded = l.excluded,
                                link_pending = l.pending,
                                size_bytes = CASE WHEN l.bytes > 0 THEN l.bytes ELSE d.bytes END,
                                chunk_count = d.chunks,
                                updated_at = now()
                              FROM link_counts AS l, doc_counts AS d
                             WHERE sources.id = :source_id
                            RETURNING link_total, link_trained, link_failed, link_excluded,
                                      link_pending, size_bytes, chunk_count
                            """
                        ),
                        {"source_id": source_id},
                    )
                )
                .mappings()
                .first()
            )
        return {key: int(value or 0) for key, value in dict(row).items()} if row else {}

    async def record_usage(
        self, tenant_id: UUID, kind: str, quantity: int, *, meta: dict[str, Any] | None = None
    ) -> None:
        """Append one metered event to the shared usage ledger.

        Crawl-side metering (currently hosted renders) goes through the same
        `usage_events` table as ingestion and chat rather than a crawl-specific
        counter, so the cost breakdown a tenant sees stays one query and one shape.
        """
        if quantity <= 0:
            return
        async with tenant_scope(tenant_id) as session:
            await session.execute(
                text(
                    "INSERT INTO usage_events (tenant_id, kind, quantity, meta) "
                    "VALUES (:tenant_id, :kind, :quantity, CAST(:meta AS jsonb))"
                ),
                {
                    "tenant_id": tenant_id,
                    "kind": kind,
                    "quantity": int(quantity),
                    "meta": json.dumps(meta or {}),
                },
            )

    # ------------------------------------------------------------------ qa pairs

    async def create_qa_pair(
        self,
        tenant_id: UUID,
        source_id: UUID,
        *,
        title: str,
        questions: list[str],
        answer: str,
        ordinal: int = 0,
        document_id: UUID | None = None,
    ) -> dict[str, Any]:
        async with tenant_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(
                            """
                            INSERT INTO qa_pairs (
                                tenant_id, source_id, document_id, title, questions,
                                answer, ordinal
                            ) VALUES (
                                :tenant_id, :source_id, :document_id, :title, :questions,
                                :answer, :ordinal
                            )
                            RETURNING id, tenant_id, source_id, document_id, title,
                                      questions, answer, ordinal, created_at, updated_at
                            """
                        ),
                        {
                            "tenant_id": tenant_id,
                            "source_id": source_id,
                            "document_id": document_id,
                            "title": title,
                            "questions": questions,
                            "answer": answer,
                            "ordinal": ordinal,
                        },
                    )
                )
                .mappings()
                .first()
            )
        return dict(row) if row else {}

    async def update_qa_pair(
        self,
        tenant_id: UUID,
        pair_id: UUID,
        *,
        title: str | None = None,
        questions: list[str] | None = None,
        answer: str | None = None,
        ordinal: int | None = None,
        document_id: UUID | None = None,
        document_set: bool = False,
    ) -> dict[str, Any] | None:
        sets: list[str] = ["updated_at = now()"]
        params: dict[str, Any] = {"id": pair_id}
        if title is not None:
            sets.append("title = :title")
            params["title"] = title
        if questions is not None:
            sets.append("questions = :questions")
            params["questions"] = questions
        if answer is not None:
            sets.append("answer = :answer")
            params["answer"] = answer
        if ordinal is not None:
            sets.append("ordinal = :ordinal")
            params["ordinal"] = ordinal
        if document_id is not None or document_set:
            sets.append("document_id = :document_id")
            params["document_id"] = document_id

        async with tenant_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(
                            f"UPDATE qa_pairs SET {', '.join(sets)} "  # noqa: S608
                            "WHERE id = :id RETURNING id, tenant_id, source_id, document_id, "
                            "title, questions, answer, ordinal, created_at, updated_at"
                        ),
                        params,
                    )
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    async def get_qa_pair(self, tenant_id: UUID, pair_id: UUID) -> dict[str, Any] | None:
        async with tenant_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT id, tenant_id, source_id, document_id, title, questions, "
                            "answer, ordinal, created_at, updated_at FROM qa_pairs "
                            "WHERE id = :id"
                        ),
                        {"id": pair_id},
                    )
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    async def list_qa_pairs(
        self, tenant_id: UUID, source_id: UUID, *, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        async with tenant_scope(tenant_id) as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT id, tenant_id, source_id, document_id, title, questions, "
                            "answer, ordinal, created_at, updated_at FROM qa_pairs "
                            "WHERE source_id = :source_id "
                            "ORDER BY ordinal ASC, created_at ASC LIMIT :limit OFFSET :offset"
                        ),
                        {"source_id": source_id, "limit": limit, "offset": offset},
                    )
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    async def delete_qa_pair(self, tenant_id: UUID, pair_id: UUID) -> UUID | None:
        """Delete a pair, returning its document id so the caller can unindex it."""
        async with tenant_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text("DELETE FROM qa_pairs WHERE id = :id RETURNING document_id"),
                        {"id": pair_id},
                    )
                )
                .mappings()
                .first()
            )
        if not row or row["document_id"] is None:
            return None
        return UUID(str(row["document_id"]))

    async def next_qa_ordinal(self, tenant_id: UUID, source_id: UUID) -> int:
        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT coalesce(max(ordinal), -1) + 1 AS n FROM qa_pairs "
                        "WHERE source_id = :source_id"
                    ),
                    {"source_id": source_id},
                )
            ).first()
        return int(row[0]) if row else 0

    async def qa_ask_metrics(
        self, tenant_id: UUID, document_id: UUID, *, days: int = 30
    ) -> dict[str, Any]:
        """How often an answer citing this document was actually sent.

        "Times cited in an answer", not "times retrieved": a pair the retriever
        surfaced and the model discarded was not the answer, and a tenant tuning
        their Q&A set needs the number that reflects what users received. The data
        already exists in `messages.citations`, which `messages_citations_gin`
        makes a containment lookup rather than a scan.
        """
        probe = json.dumps([{"document_id": str(document_id)}])
        async with tenant_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(
                            """
                            SELECT count(*) AS times_asked, max(created_at) AS last_asked
                              FROM messages
                             WHERE role = 'assistant'
                               AND citations @> CAST(:probe AS jsonb)
                               AND created_at >= now() - make_interval(days => :days)
                            """
                        ),
                        {"probe": probe, "days": days},
                    )
                )
                .mappings()
                .first()
            )
        if not row:
            return {"times_asked": 0, "last_asked": None}
        return {"times_asked": int(row["times_asked"] or 0), "last_asked": row["last_asked"]}

    # ------------------------------------------------------------------ scheduler

    async def due_sources(self, *, batch: int, per_tenant: int = 1) -> list[dict[str, Any]]:
        """Sources whose auto-retrain interval has elapsed, across all tenants.

        `row_number()` partitioned by tenant with `rn <= per_tenant` is the
        starvation fix: a tenant with 200 due sources contributes exactly one per
        tick, so a small tenant's single site is never queued behind them.

        The claim happens inside this one transaction — `last_checked_at = now()`
        and `crawl_state = 'discovering'` are written as part of the select — so a
        double-fired cron cannot pick the same source twice.

        Runs under `admin_scope` because a scheduler has no tenant. It returns
        identifiers only; every subsequent read and write goes back through
        `tenant_scope`, so there is no cross-tenant write path here.
        """
        async with admin_scope() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            """
                            WITH ranked AS (
                                SELECT id, tenant_id,
                                       row_number() OVER (
                                           PARTITION BY tenant_id
                                           ORDER BY COALESCE(last_checked_at,
                                                             '-infinity'::timestamptz) ASC
                                       ) AS rn
                                  FROM sources
                                 WHERE auto_retrain_hours > 0
                                   AND crawl_state IN ('ready', 'failed')
                                   AND kind IN ('crawl', 'sitemap', 'links')
                                   AND (last_checked_at IS NULL
                                        OR last_checked_at < now()
                                           - make_interval(hours => auto_retrain_hours))
                            ),
                            picked AS (
                                SELECT id, tenant_id FROM ranked
                                 WHERE rn <= :per_tenant
                                 ORDER BY rn, id
                                 LIMIT :batch
                            )
                            UPDATE sources AS s
                               SET last_checked_at = now(),
                                   crawl_state = 'discovering',
                                   updated_at = now()
                              FROM picked
                             WHERE s.id = picked.id
                            RETURNING s.id, s.tenant_id, s.kind, s.seed_url, s.origin
                            """
                        ),
                        {"per_tenant": per_tenant, "batch": batch},
                    )
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    async def stalled_sources(self, *, ttl_seconds: int, batch: int = 20) -> list[dict[str, Any]]:
        """In-flight sources whose wave stopped reporting.

        A serverless wave that is killed between claiming work and enqueueing its
        successor leaves the source in `fetching` forever: the queue is empty, the
        source looks busy, and nothing ever moves again. This is the sweeper's
        input. Identifiers only, for the same reason as `due_sources`.
        """
        async with admin_scope() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            """
                            SELECT id, tenant_id, kind, crawl_state, updated_at
                              FROM sources
                             WHERE crawl_state IN ('discovering', 'fetching', 'extracting')
                               AND updated_at < now() - make_interval(secs => :ttl)
                             ORDER BY updated_at ASC
                             LIMIT :batch
                            """
                        ),
                        {"ttl": float(ttl_seconds), "batch": batch},
                    )
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

