"""Everything a route does to a source that is not serialisation.

The split is deliberate. Routes validate shapes and map errors to status codes;
this file owns the things that are easy to get wrong and must not be duplicated
per endpoint:

* **The config write.** `CrawlConfig.from_json` applies the deployment's
  defaults and hard ceilings once, so every config this service parses answers
  to the same bounds, and `js_rendering` is stored exactly as the tenant asked.
* **Fan-out.** A source's tags/collection belong to its children, a deleted
  source's children have vectors, and an excluded link has a document. Each of
  those is one repository call plus one adapter call, in an order where a failure
  leaves nothing indexed with no row behind it.

No SQL lives here — `SourceRepository` owns it — and no network call lives here
either, except `assert_fetchable` on a seed URL, which resolves DNS and opens no
socket. It runs synchronously at create so a tenant who typed an unreachable or
internal address is told at once rather than three minutes into a failed crawl.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from app.adapters.queue.base import Job
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.core.urlguard import assert_fetchable
from app.domain.crawl import CrawlConfig, canonicalise, origin_of, url_hash
from app.domain.robots import RobotsTxt
from app.services.crawl.frontier import ACTIVE_STATES, LinkCandidate, plan_links

logger = get_logger(__name__)

# kinds that have a frontier and therefore a crawl. `snippet` and `qa` are
# authored content: same parent row, no network, no links table
WEBSITE_KINDS = frozenset({"crawl", "sitemap", "links"})
SOURCE_KINDS = WEBSITE_KINDS | {"snippet", "qa"}

# states from which a retrain may start. anything else is either already running
# or cancelled, and a second wave on a running source is what the lease exists to
# prevent — better to refuse in the API than to no-op inside the engine
RETRAINABLE_STATES = frozenset({"idle", "ready", "failed", "cancelled"})

# minimum hours between automatic retrains, platform-wide, applied on write
RETRAIN_MIN_HOURS = 168
RETRAIN_MAX_HOURS = 24 * 30

# the reason recorded when a human excluded a page. outside the automatic
# vocabulary on purpose: `reason_text` renders unknown reasons with a neutral
# sentence, and a tenant reading "you excluded this" needs no explanation of a rule
MANUAL_REASON = "manual"
CANCELLED_REASON = "cancelled"

# bulk actions are bounded by the repository too; repeated here so the service
# refuses rather than silently truncating
MAX_BULK_LINK_IDS = 500

class SourceService:
    """Source lifecycle. Constructed by the container with the container itself,
    the same shape `WhatsAppService` uses, so an adapter added later is picked up
    without changing this signature."""

    def __init__(self, container: Any) -> None:
        self._container = container

    # ------------------------------------------------------------------ plumbing

    @property
    def _repo(self) -> Any:
        return self._container.source_repo

    @property
    def _documents(self) -> Any:
        return self._container.documents

    @property
    def _settings(self) -> Any:
        return self._container.settings

    def _parse_config(self, raw: Any) -> CrawlConfig:
        """`CrawlConfig.from_json` with the deployment's defaults supplied.

        Central so every config this service parses takes CRAWL_MAX_DEPTH and
        CRAWL_DEFAULT_DELAY_MS from Settings when the row or payload leaves them
        blank. A stated value still wins, and the hard ceilings are unchanged.
        """
        return CrawlConfig.from_json(
            raw,
            default_depth=self._settings.CRAWL_MAX_DEPTH,
            default_delay_ms=self._settings.CRAWL_DEFAULT_DELAY_MS,
        )

    async def enqueue_wave(self, tenant_id: UUID, source_id: UUID, *, delay: int = 0) -> None:
        """One `crawl_wave` job. The payload is the source id and nothing else —
        every other input a wave needs is a row it reads for itself, which is what
        makes a redelivered job safe."""
        await self._container.queue.enqueue(
            Job(
                kind="crawl_wave",
                tenant_id=tenant_id,
                payload={"source_id": str(source_id)},
                delay_seconds=delay,
            )
        )

    # -------------------------------------------------------------------- create

    async def create(
        self,
        tenant_id: UUID,
        *,
        kind: str,
        title: str,
        seed_url: str | None = None,
        urls: list[str] | None = None,
        config: dict[str, Any] | None = None,
        tags: list[str] | None = None,
        collection: str | None = None,
        agent_id: UUID | None = None,
        auto_retrain_hours: int = 0,
        enqueue: bool = True,
    ) -> dict[str, Any]:
        """Create a source of any kind, bounded and enqueued.

        Website kinds get a validated seed and a wave. Authored kinds
        (`snippet`, `qa`) get the same parent row and nothing else — their
        services add the child documents — so tags, collection, retrieval
        scoping and deletion behave identically for all five kinds.
        """
        if kind not in SOURCE_KINDS:
            raise ValidationError(f"unknown source kind {kind!r}")

        clean_title = (title or "").strip()
        if not clean_title:
            raise ValidationError("title is required")

        if kind not in WEBSITE_KINDS:
            row: dict[str, Any] = await self._repo.create_source(
                tenant_id,
                kind=kind,
                title=clean_title,
                config=dict(config or {}),
                tags=tags,
                collection=collection,
                agent_id=agent_id,
                crawl_state="ready",
            )
            return row

        seed, origin = self._validate_seed(seed_url, kind=kind, urls=urls)

        parsed = self._parse_config(config)
        stored = self._stored_config(parsed, requested=config)
        hours = self._clamp_retrain(auto_retrain_hours)

        row = await self._repo.create_source(
            tenant_id,
            kind=kind,
            title=clean_title,
            seed_url=seed,
            origin=origin,
            config=stored,
            tags=tags,
            collection=collection,
            agent_id=agent_id,
            auto_retrain_hours=hours,
            crawl_state="idle",
        )
        source_id = UUID(str(row["id"]))

        if kind == "links" and urls:
            added = await self.add_links(
                tenant_id, source_id, urls, config=parsed, origin=origin, enqueue=False
            )
            row["link_total"] = added

        if enqueue:
            await self.enqueue_wave(tenant_id, source_id)

        logger.info(
            "source_created",
            tenant_id=str(tenant_id),
            source_id=str(source_id),
            kind=kind,
            max_pages=parsed.max_pages,
        )
        return row

    def _validate_seed(
        self, seed_url: str | None, *, kind: str, urls: list[str] | None
    ) -> tuple[str, str]:
        """Canonicalise and SSRF-check a seed before a row exists.

        `assert_fetchable` here is not the crawler's check — the crawler runs its
        own on every request and every redirect hop — it is a courtesy: a tenant
        who pasted `http://localhost:8000` or an unresolvable host learns in the
        response to `POST /v1/sources/website` instead of from a failed crawl.
        """
        candidate = (seed_url or "").strip()
        if not candidate and kind == "links":
            first = next((u for u in (urls or []) if u.strip()), "")
            candidate = first.strip()
        if not candidate:
            raise ValidationError("a seed URL is required")

        seed = canonicalise(candidate)
        if not seed:
            raise ValidationError("the seed URL is not a valid http(s) address")

        origin = origin_of(seed) or ""
        if not origin:
            raise ValidationError("the seed URL has no host")

        assert_fetchable(
            seed,
            allow_private=self._settings.URL_FETCH_ALLOW_PRIVATE,
            allowed_ports=self._settings.url_fetch_ports,
        )
        return seed, origin

    def _stored_config(
        self,
        parsed: CrawlConfig,
        *,
        requested: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """`CrawlConfig.to_json` plus the keys the domain object does not model.

        `js_rendering` is written here rather than read at crawl time: the engine
        takes its render budget off the row (`_render_budget`), which keeps extra
        lookups out of the per-page path, and the value stored is exactly what the
        tenant asked for. `force_exclude` is carried through untouched because a
        config edit must not resurrect pages a human removed.
        """
        raw = requested or {}
        stored: dict[str, Any] = parsed.to_json()

        stored["js_rendering"] = bool(raw.get("js_rendering", True))

        excluded = raw.get("force_exclude")
        if isinstance(excluded, list | tuple | set | frozenset):
            stored["force_exclude"] = sorted({str(item) for item in excluded})
        return stored

    def _clamp_retrain(self, hours: int | None) -> int:
        """Clamp the auto-retrain interval to the platform bounds."""
        if not hours or hours <= 0:
            return 0
        return min(RETRAIN_MAX_HOURS, max(RETRAIN_MIN_HOURS, int(hours)))

    # -------------------------------------------------------------------- update

    async def update(
        self,
        tenant_id: UUID,
        source_id: UUID,
        *,
        title: str | None = None,
        config: dict[str, Any] | None = None,
        tags: list[str] | None = None,
        collection: str | None = None,
        collection_set: bool = False,
        auto_retrain_hours: int | None = None,
    ) -> dict[str, Any]:
        """Partial update. A config edit is stored, never crawled.

        Re-crawling on every config change would let a tenant burn the whole
        page budget by typing in a text field. The changed rules take effect on
        the next retrain, and `pending_changes` is set so the UI can offer the
        button.
        """
        source = await self._require(tenant_id, source_id)

        stored: dict[str, Any] | None = None
        if config is not None:
            merged = {**self._raw_config(source), **config}
            parsed = self._parse_config(merged)
            stored = self._stored_config(parsed, requested=merged)

        hours: int | None = None
        if auto_retrain_hours is not None:
            hours = self._clamp_retrain(auto_retrain_hours)

        row: dict[str, Any] | None = await self._repo.update_source(
            tenant_id,
            source_id,
            title=title.strip() if title else None,
            config=stored,
            tags=tags,
            collection=collection,
            collection_set=collection_set,
            auto_retrain_hours=hours,
        )
        if row is None:
            raise NotFoundError("source not found")

        if tags is not None or collection_set:
            # one statement for the whole site: a 500-page source would otherwise
            # be 500 round trips to change one tag, and a facet set on the parent
            # is meant to cover every page under it
            touched = await self._repo.apply_facets_to_children(
                tenant_id,
                source_id,
                tags=tags,
                collection=collection,
                collection_set=collection_set,
            )
            logger.info("source_facets_applied", source_id=str(source_id), documents=touched)

        if stored is not None and str(source.get("kind") or "") in WEBSITE_KINDS:
            await self._repo.set_crawl_state(
                tenant_id,
                source_id,
                str(row.get("crawl_state") or source.get("crawl_state") or "idle"),
                pending_changes=True,
            )
            row["pending_changes"] = True

        return row

    # --------------------------------------------------------- retrain and cancel

    async def retrain(self, tenant_id: UUID, source_id: UUID) -> dict[str, Any]:
        """Queue a wave for an existing source.

        The stored config is re-parsed rather than reused from create, so config
        edits made since always take effect on the next retrain.
        """
        source = await self._require(tenant_id, source_id)
        kind = str(source.get("kind") or "")
        if kind not in WEBSITE_KINDS:
            raise ValidationError("only website sources can be retrained")

        state = str(source.get("crawl_state") or "idle")
        if state in ACTIVE_STATES:
            raise ConflictError("this source is already crawling")
        if state not in RETRAINABLE_STATES:
            raise ConflictError(f"a source in state {state!r} cannot be retrained")

        raw = self._raw_config(source)
        parsed = self._parse_config(raw)
        await self._repo.update_source(
            tenant_id, source_id, config=self._stored_config(parsed, requested=raw)
        )

        # `checked` stamps last_checked_at, which is what stops the auto-retrain
        # selector from picking this source again on the very next cron tick
        await self._repo.set_crawl_state(
            tenant_id,
            source_id,
            "discovering",
            clear_error=True,
            started=True,
            checked=True,
            pending_changes=False,
        )
        await self.enqueue_wave(tenant_id, source_id)
        logger.info("source_retrain_queued", source_id=str(source_id), pages=parsed.max_pages)
        return {"status": "queued", "source_id": str(source_id)}

    async def cancel(self, tenant_id: UUID, source_id: UUID) -> dict[str, Any]:
        """Stop a running crawl, keeping everything it already trained.

        A cancelled crawl is a partial success, not a rollback: the pages already
        indexed stay queryable, and only the frontier is closed out. The running
        wave notices on its next claim — there is nothing to kill, because a wave
        holds no long-lived state beyond its lease.
        """
        source = await self._require(tenant_id, source_id)
        state = str(source.get("crawl_state") or "idle")
        if state not in ACTIVE_STATES:
            raise ConflictError("this source is not crawling")

        closed = await self._close_frontier(tenant_id, source_id)
        await self._repo.set_crawl_state(
            tenant_id, source_id, "cancelled", checked=True, pending_changes=False
        )
        counters = await self._repo.recompute_counters(tenant_id, source_id)

        row = await self._repo.get_source(tenant_id, source_id) or dict(source)
        row["cancelled_links"] = closed
        row["counters"] = counters
        logger.info("source_cancelled", source_id=str(source_id), pending_closed=closed)
        return row

    async def _close_frontier(self, tenant_id: UUID, source_id: UUID) -> int:
        """Move every still-pending link to `excluded, reason='cancelled'`.

        Batched at `MAX_BULK_LINK_IDS` with a bounded number of passes, so
        cancelling a 10k-page crawl cannot turn one request into an unbounded
        write loop. Anything left over is closed by the next wave's frontier cap.
        """
        closed = 0
        for _pass in range(40):
            rows = await self._repo.list_links(
                tenant_id, source_id, state="pending", limit=MAX_BULK_LINK_IDS
            )
            if not rows:
                break
            ids = [UUID(str(row["id"])) for row in rows]
            closed += await self._repo.bulk_set_state(
                tenant_id, source_id, ids, state="excluded", reason=CANCELLED_REASON
            )
            if len(rows) < MAX_BULK_LINK_IDS:
                break
        return closed

    # -------------------------------------------------------------------- delete

    async def delete(self, tenant_id: UUID, source_id: UUID) -> int:
        """Delete a source and unindex its children.

        Vectors first, rows second. The other order leaves a window where a query
        can retrieve a chunk whose document row is already gone, which surfaces to
        the tenant as a citation to a document the API then 404s.
        """
        await self._require(tenant_id, source_id)
        document_ids = await self._repo.delete_source(tenant_id, source_id)

        removed = 0
        for document_id in document_ids:
            try:
                await self._container.indexer.remove(tenant_id, document_id)
            except Exception:
                # the row still goes: a stranded vector is invisible to retrieval
                # once `resolve_document_ids` stops returning the document, and the
                # alternative is a source that cannot be deleted at all
                logger.warning(
                    "source_child_unindex_failed", document_id=str(document_id), exc_info=True
                )
            await self._documents.soft_delete(tenant_id, document_id)
            removed += 1

        logger.info("source_deleted", source_id=str(source_id), documents=removed)
        return removed

    # ---------------------------------------------------------------- link review

    async def add_links(
        self,
        tenant_id: UUID,
        source_id: UUID,
        urls: list[str],
        *,
        config: CrawlConfig | None = None,
        origin: str | None = None,
        enqueue: bool = True,
    ) -> int:
        """Add individual URLs to an existing source's frontier.

        Classified through `plan_links` exactly like a discovered link, so a URL a
        tenant pasted is subject to the same scope, robots and budget rules — with
        the refusals recorded as `excluded` rows rather than dropped, which is what
        makes the review list explain itself.
        """
        source: dict[str, Any] | None = None
        if config is None or origin is None:
            source = await self._require(tenant_id, source_id)
            config = self._parse_config(self._raw_config(source))
            origin = str(source.get("origin") or "") or origin_of(
                str(source.get("seed_url") or "")
            )
        if not origin:
            raise ValidationError("this source has no origin to add links to")

        candidates = [
            LinkCandidate(url=url.strip(), depth=0, discovered_via="manual")
            for url in urls
            if url.strip()
        ]
        if not candidates:
            return 0

        robots = await self._robots(origin)
        known = await self._repo.count_links(tenant_id, source_id)
        planned = plan_links(
            candidates,
            origin=origin,
            config=config,
            robots=robots,
            page_headroom=max(0, config.max_pages - known),
        )
        added: int = await self._repo.insert_links(tenant_id, source_id, planned.rows)

        if added and enqueue:
            await self._repo.set_crawl_state(
                tenant_id, source_id, "discovering", clear_error=True, pending_changes=False
            )
            await self.enqueue_wave(tenant_id, source_id)

        logger.info(
            "source_links_added",
            source_id=str(source_id),
            requested=len(candidates),
            added=added,
            excluded=planned.excluded,
        )
        return added

    async def _robots(self, origin: str) -> RobotsTxt:
        """`robots.txt` for a manual link addition, through the crawl fetcher.

        The fetcher is the only thing in this package that opens sockets, and its
        result is cached per origin, so this costs nothing on the common path. A
        failure is permissive, matching RFC 9309 and `CrawlFetcher.robots`.
        """
        fetcher = getattr(self._container, "page_fetcher", None)
        if fetcher is None:
            return RobotsTxt.permissive()
        try:
            robots: RobotsTxt = await fetcher.robots(origin)
        except Exception:
            logger.info("source_robots_unavailable", origin=origin)
            return RobotsTxt.permissive()
        return robots

    async def exclude_link(
        self, tenant_id: UUID, source_id: UUID, link_id: UUID
    ) -> dict[str, Any]:
        """Exclude one page for good: unindex it, forget its document, remember the
        decision in `config.force_exclude` so a retrain does not resurrect it."""
        link = await self._require_link(tenant_id, source_id, link_id)
        source = await self._require(tenant_id, source_id)

        await self._detach_document(tenant_id, link)
        await self._repo.update_link(
            tenant_id,
            link_id,
            state="excluded",
            reason=MANUAL_REASON,
            reason_set=True,
            reason_detail=None,
            document_id=None,
            document_set=True,
            clear_claim=True,
            raw_path=None,
            raw_path_set=True,
        )
        await self._remember_exclusion(tenant_id, source, link, excluded=True)
        counters = await self._repo.recompute_counters(tenant_id, source_id)

        row = await self._repo.get_link(tenant_id, link_id) or {}
        row["counters"] = counters
        return row

    async def restore_link(
        self, tenant_id: UUID, source_id: UUID, link_id: UUID
    ) -> dict[str, Any]:
        """Put an excluded page back in the frontier and crawl it.

        A page excluded by an automatic rule also earns a `force_include` entry:
        without it the next wave would apply the same rule and exclude it again,
        and the tenant would click Restore forever.
        """
        link = await self._require_link(tenant_id, source_id, link_id)
        source = await self._require(tenant_id, source_id)

        await self._repo.update_link(
            tenant_id,
            link_id,
            state="pending",
            reason=None,
            reason_set=True,
            reason_detail=None,
            clear_claim=True,
            reset_attempt=True,
        )
        await self._remember_exclusion(tenant_id, source, link, excluded=False)
        counters = await self._repo.recompute_counters(tenant_id, source_id)
        await self._repo.set_crawl_state(
            tenant_id, source_id, "discovering", clear_error=True, pending_changes=False
        )
        await self.enqueue_wave(tenant_id, source_id)

        row = await self._repo.get_link(tenant_id, link_id) or {}
        row["counters"] = counters
        return row

    async def bulk_links(
        self, tenant_id: UUID, source_id: UUID, link_ids: list[UUID], action: str
    ) -> dict[str, Any]:
        """Exclude or restore up to 500 links: one state statement, one recompute.

        The per-link work that cannot be batched is the vector removal, because a
        vector store delete is one call per document. Everything else — the state
        change and the counters — happens exactly once.
        """
        if action not in ("exclude", "restore"):
            raise ValidationError("action must be 'exclude' or 'restore'")
        if not link_ids:
            return {"updated": 0}
        if len(link_ids) > MAX_BULK_LINK_IDS:
            raise ValidationError(f"at most {MAX_BULK_LINK_IDS} links per bulk action")

        await self._require(tenant_id, source_id)

        if action == "exclude":
            for link_id in link_ids:
                link = await self._repo.get_link(tenant_id, link_id)
                if link and str(link.get("source_id")) == str(source_id):
                    await self._detach_document(tenant_id, link)
            updated = await self._repo.bulk_set_state(
                tenant_id,
                source_id,
                link_ids,
                state="excluded",
                reason=MANUAL_REASON,
                clear_document=True,
            )
        else:
            updated = await self._repo.bulk_set_state(
                tenant_id, source_id, link_ids, state="pending"
            )

        counters = await self._repo.recompute_counters(tenant_id, source_id)
        if action == "restore" and updated:
            await self._repo.set_crawl_state(
                tenant_id, source_id, "discovering", clear_error=True, pending_changes=False
            )
            await self.enqueue_wave(tenant_id, source_id)

        logger.info(
            "source_links_bulk", source_id=str(source_id), action=action, updated=updated
        )
        return {"updated": updated, "counters": counters}

    # ----------------------------------------------------------------- internals

    async def _require(self, tenant_id: UUID, source_id: UUID) -> dict[str, Any]:
        source: dict[str, Any] | None = await self._repo.get_source(tenant_id, source_id)
        if source is None:
            raise NotFoundError("source not found")
        return source

    async def _require_link(
        self, tenant_id: UUID, source_id: UUID, link_id: UUID
    ) -> dict[str, Any]:
        link: dict[str, Any] | None = await self._repo.get_link(tenant_id, link_id)
        if link is None or str(link.get("source_id")) != str(source_id):
            # a link id that belongs to another source is a 404, not a 403: telling
            # a caller that an id exists elsewhere is itself information
            raise NotFoundError("link not found")
        return link

    def _raw_config(self, source: dict[str, Any]) -> dict[str, Any]:
        raw = source.get("config")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                return {}
        return dict(raw) if isinstance(raw, dict) else {}

    async def _detach_document(self, tenant_id: UUID, link: dict[str, Any]) -> None:
        """Remove one link's document from the index and soft-delete the row."""
        document_id = link.get("document_id")
        if not document_id:
            return
        target = UUID(str(document_id))
        try:
            await self._container.indexer.remove(tenant_id, target)
        except Exception:
            logger.warning("link_unindex_failed", document_id=str(target), exc_info=True)
        await self._documents.soft_delete(tenant_id, target)

    async def _remember_exclusion(
        self, tenant_id: UUID, source: dict[str, Any], link: dict[str, Any], *, excluded: bool
    ) -> None:
        """Record or undo a manual exclusion in the source's config.

        Two lists, because they answer different questions on the next wave:
        `force_exclude` stops a page being re-queued at all, and `force_include`
        overrides the automatic rule that excluded it in the first place.
        """
        raw = self._raw_config(source)
        digest = str(link.get("url_hash") or "") or url_hash(str(link.get("url") or ""))
        if not digest:
            return

        excludes = {str(item) for item in raw.get("force_exclude") or []}
        includes = {str(item) for item in raw.get("force_include") or []}

        if excluded:
            excludes.add(digest)
            includes.discard(digest)
        else:
            excludes.discard(digest)
            reason = str(link.get("reason") or "")
            if reason and reason != MANUAL_REASON:
                includes.add(digest)

        raw["force_exclude"] = sorted(excludes)
        raw["force_include"] = sorted(includes)
        await self._repo.update_source(tenant_id, UUID(str(source["id"])), config=raw)

