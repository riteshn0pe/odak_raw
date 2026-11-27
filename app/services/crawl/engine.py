"""The wave engine: one bounded, resumable slice of a crawl per invocation.

A crawl is not a loop. It is a sequence of `crawl_wave` jobs, each of which does
as much as fits in `CRAWL_WAVE_BUDGET_SECONDS` and then either finishes the crawl
or enqueues its successor. That shape is forced by the runtime — a serverless
worker can be killed at any moment and a request has a hard deadline — and it is
also what makes a crawl restartable, cancellable and observable, because every
piece of state a wave depends on is a row rather than a local variable.

Three properties are load-bearing and easy to break by "simplifying" this file:

**The claim is the unit of work, not the job.** `pending -> fetching` is one
atomic `UPDATE … FOR UPDATE SKIP LOCKED` in the repository. A redelivered job (a
certainty with QStash, not a possibility) claims a different slice or none, so no
URL is fetched twice; a wave killed mid-flight leaves rows claimed with a stale
`claimed_at`, which the next wave reclaims by TTL.

**Counters are recomputed, never incremented.** Once per wave, derived from
`source_links`. At-least-once delivery makes `counter + 1` wrong, and wrong
counters are what a tenant reads on the dashboard.

**Deletions are collected, then applied behind a circuit breaker.** If more than
`CRAWL_MASS_DELETE_RATIO` of a source's pages disappear in one pass, nothing is
deleted and the source is marked failed. A site behind a broken deploy or a new
WAF would otherwise wipe a customer's knowledge base, and that is unrecoverable
without a full re-crawl.

Every socket this package opens goes through one of two SSRF-checked doors:
:class:`app.services.crawl.fetch.CrawlFetcher` (robots.txt, sitemaps, PDFs —
the plain-HTTP path) and :class:`app.services.crawl.library.SiteCrawler` (the
crawl4ai browser behind page reads and site-wide discovery). This module never
constructs an HTTP client or a browser of its own.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from app.adapters.parsers.web import UndecodableError, decode_html
from app.adapters.postgres.source_repo import REVIEWABLE_STATES
from app.adapters.queue.base import Job
from app.adapters.storage.base import document_path
from app.core.config import Settings
from app.core.constants import CRAWL_PDF_MIME, DocumentStatus, UsageKind
from app.core.logging import get_logger
from app.domain.crawl import (
    EXCLUSION_REASONS,
    REASON_BLOCKED_TARGET,
    REASON_DUPLICATE_CONTENT,
    REASON_JAVASCRIPT_RENDERED,
    REASON_NOINDEX,
    REASON_NON_CANONICAL,
    REASON_NON_HTML,
    REASON_PAGE_BUDGET_EXCEEDED,
    REASON_REDIRECT_OFF_ORIGIN,
    REASON_ROBOTS_DISALLOWED,
    REASON_TIME_BUDGET_EXCEEDED,
    REASON_TOO_LARGE,
    CrawlConfig,
    canonicalise,
    extract_links,
    in_scope,
    looks_like_pdf,
    normalise_content_type,
    parse_sitemap,
    to_signed_64,
)
from app.domain.ports import CachePort, PageRenderer
from app.domain.robots import RobotsTxt
from app.services.crawl.extract import (
    CRAWL_BOILERPLATE_SAMPLE,
    PageContent,
    extract_page,
    freeze_boilerplate,
    is_duplicate,
    page_from_markdown,
    rejection,
)
from app.services.crawl.fetch import (
    SITEMAP_MAX_BYTES,
    CrawlFetcher,
    CrawlFetchError,
    allowed_mimes_for,
    build_page_fetcher,
    content_type_of,
    crawl_raw_path,
    host_of,
    origin_of,
    retry_after_seconds,
)
from app.services.crawl.frontier import (
    LinkCandidate,
    conditional_headers,
    parse_lastmod,
    plan_links,
    unchanged_by_lastmod,
)
from app.services.crawl.library import LibraryUnavailableError, SiteCrawler

logger = get_logger(__name__)

# how many links one wave claims per fetch round: one full wave of concurrent
# fetches plus a second round already claimed behind it, so the last gather of a
# slice does not underfill. a claim held past the wave budget is still a lease
# another wave has to wait out, which CRAWL_LINK_CLAIM_TTL_SECONDS reclaims
CRAWL_FETCH_SLICE = 24
# extraction is I/O-bound at the wave's distance from the database and the
# blob store — read raw, judge, write link, write document — so a slice is
# processed with several pages in flight rather than one at a time; the HTML
# parse inside each page still runs off the event loop (asyncio.to_thread)
CRAWL_EXTRACT_SLICE = 24

# How long a gated wave may spend harvesting links before it parks for review. Only
# reached by a site with no sitemap, where links exist nowhere but inside pages: the
# seed and its neighbours are read, and the rest of the frontier waits behind the
# approval. Short on purpose — this budget is a person watching a list appear.
CRAWL_MAP_BUDGET_SECONDS = 12

# States that make a row part of the list under review: found, being read, read, or
# already known from a previous cycle. A `pending` row belongs here because approval
# is what fetches it — the tenant is approving the URL, not the bytes.
REVIEW_FRONTIER_STATES = ("pending", "fetching", *REVIEWABLE_STATES)

# How many URLs a gated discovery may list, regardless of the source's page budget.
# The review list answers "what is on this site", so the page budget is not applied
# while gathering it — a plan ceiling of 100 against a 600-page site would offer a
# list with no relationship to the site. This is the other bound: a sitemap index
# advertising fifty thousand URLs still becomes one reviewable list, not one
# fifty-thousand-row insert. Matches the review endpoint's own URL cap.
CRAWL_REVIEW_DISCOVERY_CEILING = 5000

# how many sitemap documents are read at once. a sitemap index for a large site
# points at dozens of files and they are small, unthrottled and independent, so
# reading them one after another is the difference between a second and a minute
SITEMAP_FETCH_CONCURRENCY = 16

# a single pass that loses more than this share of a source's pages is treated as
# one site-wide event, not as N page deletions
CRAWL_MASS_DELETE_RATIO = 0.30
MASS_DELETE_MESSAGE = (
    "This site returned errors for most of its pages, so nothing was removed. "
    "Check the site and retrain."
)

# a 404 has to happen twice before a page is deleted. transient 404s from CDN
# misconfiguration and blue/green deploys are common enough that one strike would
# silently shrink a corpus
GONE_MIN_ATTEMPTS = 2


# states a wave may run in. anything else (cancelled, ready) means stop
RUNNABLE_STATES = frozenset({"idle", "discovering", "fetching", "extracting", "failed"})


class _RenderBudget:
    """The wave's hosted-render allowance, shared by concurrently extracting pages.

    A render is billable, so the take is a check-and-decrement under a lock: a
    slice extracted eight pages at once must not read the same `renders_left`
    eight times and spend eight renders the budget only had one of.
    """

    def __init__(self, total: int) -> None:
        self._left = max(0, total)
        self._lock = asyncio.Lock()

    async def take(self) -> bool:
        async with self._lock:
            if self._left <= 0:
                return False
            self._left -= 1
            return True


@dataclass(slots=True)
class WaveResult:
    """What one wave did. Returned to the job handler, which logs it and answers 200."""

    source_id: UUID
    state: str
    discovered: int = 0
    fetched: int = 0
    trained: int = 0
    unchanged: int = 0
    excluded: int = 0
    failed: int = 0
    duplicates: int = 0
    gone: int = 0
    rendered: int = 0
    next_wave: bool = False
    # largest Retry-After any page asked for, folded into the next wave's delay
    retry_after: int = 0
    counters: dict[str, int] = field(default_factory=dict)
    detail: str | None = None


@dataclass(slots=True)
class _FetchOutcome:
    """One link's fetch result, plus the links it revealed."""

    state: str
    candidates: list[LinkCandidate] = field(default_factory=list)
    gone: tuple[UUID, UUID | None] | None = None
    retry_after: int = 0


class CrawlEngine:
    """Runs one wave of one source's crawl.

    Constructed once per process by the container and reused, because the fetcher
    it holds pools connections — a 200-page crawl of one host is exactly the case
    connection reuse exists for.
    """

    def __init__(
        self,
        settings: Settings,
        source_repo: Any,
        documents: Any,
        storage: Any,
        queue: Any,
        cache: CachePort | None = None,
        *,
        renderer: PageRenderer | None = None,
        indexer: Any = None,
        fetcher: CrawlFetcher | None = None,
        site_crawler: SiteCrawler | None = None,
    ) -> None:
        self._settings = settings
        self._repo = source_repo
        self._documents = documents
        self._storage = storage
        self._queue = queue
        self._cache = cache
        self._renderer = renderer
        self._indexer = indexer
        # built here when the container does not supply one, so no caller is ever
        # tempted to hand in an httpx client of its own
        self._fetcher = fetcher or build_page_fetcher(settings, cache)
        # the browser engine behind page reads and site-wide discovery; None
        # leaves the engine on the httpx path alone (tests, or a build without
        # playwright), where only sitemap discovery and PDFs still work
        self._site_crawler = site_crawler
        self._semaphore = asyncio.Semaphore(max(1, settings.CRAWL_WAVE_CONCURRENCY))

    @property
    def name(self) -> str:
        return "crawl_engine"

    async def run_wave(self, tenant_id: UUID, source_id: UUID) -> WaveResult:
        """One bounded slice of a crawl.

        Guarded by a lease so overlapping deliveries do not both run. A wave that
        cannot take the lease returns `in_flight` and the handler answers 200: a
        non-2xx would make QStash redeliver the job forever, turning a harmless
        race into an infinite one.
        """
        token = uuid4().hex
        key = f"crawl:{source_id}"
        ttl = max(30, self._settings.CRAWL_WAVE_BUDGET_SECONDS * 2)
        held = False
        if self._cache is not None:
            held = await self._cache.acquire_lease(key, token, ttl)
            if not held:
                logger.info("crawl_wave_in_flight", source_id=str(source_id))
                return WaveResult(source_id=source_id, state="in_flight")
        try:
            return await self._wave(tenant_id, source_id)
        except LibraryUnavailableError as exc:
            # the browser binary is a deployment prerequisite, like the database.
            # failing the source with the fix in the error beats churning the
            # frontier through a crawl that cannot read a single page
            logger.error("crawl_browser_unavailable", source_id=str(source_id), error=str(exc))
            try:
                await self._repo.set_crawl_state(
                    tenant_id, source_id, "failed", error=str(exc), pending_changes=True
                )
            except Exception:  # pragma: no cover - state write must not mask the cause
                logger.error("crawl_state_write_failed", source_id=str(source_id))
            return WaveResult(source_id=source_id, state="failed", detail=str(exc))
        finally:
            if held and self._cache is not None:
                await self._cache.release_lease(key, token)

    async def close(self) -> None:
        await self._fetcher.close()

    # -------------------------------------------------------------------- wave

    async def _wave(self, tenant_id: UUID, source_id: UUID) -> WaveResult:
        source = await self._repo.get_source(tenant_id, source_id)
        if source is None:
            # deleted between enqueue and delivery. not an error: the job is done
            return WaveResult(source_id=source_id, state="missing")

        state = str(source.get("crawl_state") or "idle")
        if state not in RUNNABLE_STATES:
            return WaveResult(source_id=source_id, state=state, detail="not runnable")

        kind = str(source.get("kind") or "crawl")
        if kind in ("snippet", "qa"):
            # authored content has no frontier; the counters are still worth
            # refreshing because a document may have finished indexing since
            counters = await self._repo.recompute_counters(tenant_id, source_id)
            return WaveResult(source_id=source_id, state="ready", counters=counters)

        seed = str(source.get("seed_url") or "")
        origin = str(source.get("origin") or "") or origin_of(seed)
        config = self._config_of(source)
        boilerplate = _boilerplate_of(source)
        deadline = time.monotonic() + max(5, self._settings.CRAWL_WAVE_BUDGET_SECONDS)
        result = WaveResult(source_id=source_id, state=state)

        await self._repo.reclaim_stale(
            tenant_id, source_id, self._settings.CRAWL_LINK_CLAIM_TTL_SECONDS
        )

        robots = await self._robots(origin)
        delay_ms = self._delay_for(config, robots)

        # Does this wave train anything, or is it fetching a list for a human to
        # approve first? Resolved once, before any phase runs, so a wave cannot
        # fetch under one answer and extract under another.
        gated = _awaiting_approval(source, config)

        # How much of the site discovery already knows about. A sitemap is a list of
        # the site, so when one exists this number is the whole answer and no page
        # has to be fetched before a human can read it. Discovery already counted
        # what it inserted, so the pending recount only runs when it found nothing
        # new — a retrain of an unchanged site — where the existing frontier is the
        # only number worth consulting.
        listed = 0
        mapped = False
        if state in ("idle", "discovering"):
            result.discovered = await self._discover(
                tenant_id, source_id, kind=kind, seed=seed, origin=origin,
                config=config, robots=robots, gated=gated,
            )
            listed = result.discovered or await self._repo.pending_count(tenant_id, source_id)
            mapped = gated and listed > 1
            # A mapped wave parks one statement later, so it never enters
            # 'fetching' — that flip would be a whole round trip spent announcing
            # a state the park immediately replaces. The stamps it carries
            # (`crawl_started_at`, `origin`, clearing the error) ride on the park.
            if not mapped:
                await self._repo.set_crawl_state(
                    tenant_id, source_id, "fetching", started=True, clear_error=True, origin=origin
                )

        # The gate's whole product is the URL list, so a gated wave stops as soon as
        # it has one. Two cases:
        #
        #   * A sitemap answered. The frontier is already the site, so nothing is
        #     fetched at all — the list is on screen a second after the seed URL was
        #     typed, which is the difference between this and a progress bar.
        #   * No sitemap. Links only exist inside pages, so a short harvest runs:
        #     enough to read the seed and what it points at, bounded by
        #     CRAWL_MAP_BUDGET_SECONDS rather than the wave budget, and then it parks
        #     with the rest of the frontier still pending.
        #
        # Either way the pages the tenant approves are fetched *after* approval, so a
        # site whose list is rejected costs us the discovery requests and nothing else.
        # (`mapped` was decided above, where discovery ran — a wave resuming in
        # 'fetching' never re-discovers, so it harvests instead.)
        phase_deadline = (
            min(deadline, time.monotonic() + max(2, CRAWL_MAP_BUDGET_SECONDS))
            if gated
            else deadline
        )

        gone: list[tuple[UUID, UUID | None]] = []
        if not mapped:
            gone = await self._fetch_phase(
                tenant_id,
                source_id,
                origin=origin,
                config=config,
                robots=robots,
                delay_ms=delay_ms,
                deadline=phase_deadline,
                result=result,
            )

        # Frozen while gated whenever anything was fetched — the fingerprint is
        # derived from raw bytes this wave already holds, it is frozen once per
        # source and never recomputed, and extraction cannot start without it — so
        # doing it now is what lets an approval resume straight into extracting
        # instead of spending a wave here. A mapped wave fetched nothing, so there
        # is no sample to learn from; skipping it saves the count round trip that
        # would only prove the same.
        if not boilerplate and not mapped:
            boilerplate = await self._freeze(tenant_id, source_id)

        # A gated crawl trains none of what it found. Embedding, OCR and vector
        # storage are the costs worth withholding, and they all sit behind this call.
        if not gated and (boilerplate or not await self._repo.pending_count(tenant_id, source_id)):
            await self._repo.set_crawl_state(tenant_id, source_id, "extracting")
            await self._extract_phase(
                tenant_id,
                source_id,
                origin=origin,
                config=config,
                boilerplate=boilerplate,
                site_name=str(source.get("title") or "") or None,
                deadline=deadline,
                result=result,
                render_budget=_render_budget(self._settings, source),
            )

        return await self._finish(
            tenant_id,
            source_id,
            source=source,
            delay_ms=delay_ms,
            gone=gone,
            result=result,
            gated=gated,
        )

    # --------------------------------------------------------------- discovery

    async def _robots(self, origin: str) -> RobotsTxt:
        if not origin:
            return RobotsTxt.permissive()
        return await self._fetcher.robots(origin)

    def _config_of(self, source: Mapping[str, Any]) -> CrawlConfig:
        """Parse `sources.config` with the operator's defaults applied.

        Every place the engine needs a config goes through here, so a source whose
        row leaves `max_depth` or `delay_ms` unset picks up CRAWL_MAX_DEPTH and
        CRAWL_DEFAULT_DELAY_MS from Settings rather than the domain constants. A row
        that states either value still wins — this only supplies the blank.
        """
        return CrawlConfig.from_json(
            source.get("config"),
            default_depth=self._settings.CRAWL_MAX_DEPTH,
            default_delay_ms=self._settings.CRAWL_DEFAULT_DELAY_MS,
        )

    def _delay_for(self, config: CrawlConfig, robots: RobotsTxt) -> int:
        """The politeness delay actually used: the slower of ours and the site's.

        A site that asks for 10 s gets 10 s. Ignoring `Crawl-delay` is how a
        crawler earns a permanent block, which costs the tenant their source.
        """
        delay = max(config.delay_ms, robots.crawl_delay_ms or 0)
        floor: int = self._settings.CRAWL_MIN_DELAY_MS
        ceiling: int = self._settings.CRAWL_MAX_DELAY_MS
        return min(max(delay, floor), ceiling)

    async def _discover(
        self,
        tenant_id: UUID,
        source_id: UUID,
        *,
        kind: str,
        seed: str,
        origin: str,
        config: CrawlConfig,
        robots: RobotsTxt,
        gated: bool = False,
    ) -> int:
        """Seed the frontier: the seed itself, plus whatever the sitemaps list.

        Runs on the first wave and again at the start of every retrain, because
        discovery is how a crawl notices pages that did not exist last time.
        `ON CONFLICT DO NOTHING` in the repository is what makes re-running it
        harmless: a known URL keeps its `etag`, `content_hash` and `state`, which
        is exactly what the conditional-request path depends on across cycles.
        """
        candidates: list[LinkCandidate] = []
        if kind == "crawl" and seed:
            candidates.append(LinkCandidate(url=seed, depth=0, discovered_via="seed"))

        sitemap_urls = list(robots.sitemaps)
        if kind == "sitemap" and seed:
            sitemap_urls.insert(0, seed)
        elif origin:
            # the conventional location, tried even when robots.txt is silent —
            # most sites have one and never advertise it
            sitemap_urls.append(f"{origin.rstrip('/')}/sitemap.xml")

        candidates.extend(await self._walk_sitemaps(sitemap_urls, origin=origin, config=config))
        if kind == "crawl" and len(candidates) <= 1 and self._site_crawler is not None:
            # no sitemap answered, so the site's URL space exists only inside its
            # pages. The library walks it — rendered, scope- and robots-filtered,
            # bounded by the review ceiling while gated — which replaces the old
            # hand-rolled harvest of the seed and its neighbours.
            walked = await self._walk_site(
                seed, origin=origin, config=config, robots=robots, gated=gated
            )
            candidates.extend(
                LinkCandidate(url=url, depth=depth, discovered_via="link")
                for url, depth in walked
                if url != seed
            )
        if not candidates:
            return 0

        return await self._insert(
            tenant_id, source_id, candidates, origin=origin, config=config, robots=robots,
            gated=gated,
        )

    async def _walk_sitemaps(
        self, seeds: list[str], *, origin: str, config: CrawlConfig
    ) -> list[LinkCandidate]:
        """Breadth-first walk of a sitemap index, one level at a time, in parallel.

        Bounded because sitemaps are tenant-supplied data reached over the network:
        depth stops an index that points at itself, the URL cap stops a 50 000-entry
        sitemap from becoming a 50 000-row insert, and the `seen` set stops the
        common case of two sitemaps listing each other.

        A level is read concurrently rather than one document after another. Sitemaps
        are small, static and not subject to the politeness lease, and a large site
        splits itself into dozens of them — read serially that is a minute of waiting
        for a list the site was willing to hand over at once.
        """
        seen: set[str] = set()
        # Canonicalised against the origin before anything is fetched, so the
        # `https://www.example.com/sitemap.xml` robots.txt advertises and the
        # `https://example.com/sitemap.xml` probe below are recognised as one
        # document. Left alone they are two strings, and the walk pays two fetches
        # and two parses to learn the same five hundred URLs twice.
        level: list[str] = []
        for url in seeds:
            if not url:
                continue
            candidate = canonicalise(url, origin=origin or None) or url
            if candidate not in level:
                level.append(candidate)
        found: list[LinkCandidate] = []
        max_depth = self._settings.CRAWL_SITEMAP_MAX_DEPTH
        max_urls = self._settings.CRAWL_SITEMAP_MAX_URLS
        limit = asyncio.Semaphore(SITEMAP_FETCH_CONCURRENCY)

        async def read(url: str) -> str | None:
            async with limit:
                try:
                    status, body = await self._fetcher.fetch_text(
                        url, max_bytes=SITEMAP_MAX_BYTES
                    )
                except CrawlFetchError as exc:
                    # a missing or hostile sitemap is normal; link discovery still works
                    logger.info("sitemap_unavailable", url=url, reason=exc.reason)
                    return None
                return body if status == 200 and body.strip() else None

        for depth in range(max_depth + 1):
            batch = [url for url in level if url not in seen]
            if not batch or len(found) >= max_urls:
                break
            seen.update(batch)
            bodies = await asyncio.gather(*[read(url) for url in batch])

            nested: list[str] = []
            for url, body in zip(batch, bodies, strict=True):
                if body is None:
                    continue
                document = parse_sitemap(body, base_url=url, origin=origin or None)
                for entry in document.urls:
                    if len(found) >= max_urls:
                        break
                    found.append(
                        LinkCandidate(
                            url=entry.url, depth=0, discovered_via="sitemap", lastmod=entry.lastmod
                        )
                    )
                nested.extend(
                    candidate
                    for candidate in (
                        canonicalise(url, origin=origin or None) or url
                        for url in document.sitemaps
                    )
                    if candidate not in nested
                )
            level = nested
            if depth == max_depth and nested:
                logger.info("sitemap_depth_reached", pending=len(nested))

        logger.info("sitemap_walk_done", sitemaps=len(seen), urls=len(found))
        return found

    async def _walk_site(
        self,
        seed: str,
        *,
        origin: str,
        config: CrawlConfig,
        robots: RobotsTxt,
        gated: bool = False,
    ) -> list[tuple[str, int]]:
        """Enumerate a sitemap-less site with the crawler library's deep crawl.

        Bounded by the review ceiling while gated (the list is the gate's whole
        product, exactly as with a sitemap) and by the source's page budget
        otherwise, and by CRAWL_DISCOVERY_SECONDS in both cases so one walk
        cannot eat the wave's whole budget.
        """
        assert self._site_crawler is not None  # guarded by every caller
        ceiling = CRAWL_REVIEW_DISCOVERY_CEILING if gated else config.max_pages
        deadline = time.monotonic() + max(10, self._settings.CRAWL_DISCOVERY_SECONDS)
        return await self._site_crawler.discover(
            seed,
            origin=origin,
            include_subdomains=config.include_subdomains,
            max_depth=config.max_depth,
            max_pages=ceiling,
            delay_ms=self._delay_for(config, robots),
            robots=robots,
            deadline=deadline,
        )

    async def _insert(
        self,
        tenant_id: UUID,
        source_id: UUID,
        candidates: list[LinkCandidate],
        *,
        origin: str,
        config: CrawlConfig,
        robots: RobotsTxt,
        gated: bool = False,
    ) -> int:
        """Classify candidates and add them to the frontier.

        The page budget is measured against the rows that already exist rather than
        against this batch, so a crawl cannot exceed `max_pages` by discovering the
        same overflow twice.

        Except while gated, where the budget is not applied at all. The review list
        is the answer to "what is on this site", and a plan ceiling of 100 against a
        589-page site would make it the answer to "what are the first hundred URLs
        we happened to read" — a list with no relationship to the site, offered to
        someone whose whole job on that screen is to choose from it. The tenant
        unchecks what they do not want, and the budget is applied to what they kept,
        after approval, in :meth:`_finish`. Bounded by
        :data:`CRAWL_REVIEW_DISCOVERY_CEILING` so a pathological sitemap cannot turn
        one gated wave into a fifty-thousand-row insert.
        """
        existing = await self._repo.count_links(tenant_id, source_id)
        # only the PDF budget reads this, and every read is a transaction against a
        # database that may be a continent away, so it is not paid when PDFs are off
        pdfs = (
            await self._repo.count_links(tenant_id, source_id, state="trained")
            if config.include_pdf
            else 0
        )
        ceiling = (
            max(config.max_pages, CRAWL_REVIEW_DISCOVERY_CEILING) if gated else config.max_pages
        )
        plan = plan_links(
            candidates,
            origin=origin,
            config=config,
            robots=robots,
            pdf_count=0 if not config.include_pdf else min(pdfs, config.max_pages),
            pdf_budget=self._settings.CRAWL_MAX_PDFS_PER_SOURCE,
            page_headroom=max(0, ceiling - existing),
        )
        if not plan.rows:
            return 0
        inserted: int = await self._repo.insert_links(tenant_id, source_id, plan.rows)
        logger.info(
            "crawl_links_inserted",
            source_id=str(source_id),
            offered=len(plan.rows),
            inserted=inserted,
            excluded=plan.excluded,
        )
        return inserted

    # ------------------------------------------------------------------ fetching

    async def _fetch_phase(
        self,
        tenant_id: UUID,
        source_id: UUID,
        *,
        origin: str,
        config: CrawlConfig,
        robots: RobotsTxt,
        delay_ms: int,
        deadline: float,
        result: WaveResult,
    ) -> list[tuple[UUID, UUID | None]]:
        """Claim and fetch slices of the frontier until the wave's budget runs out.

        Returns the pages that disappeared, deliberately *without* having deleted
        anything: the mass-delete circuit breaker in :meth:`_finish` decides whether
        the deletions are applied at all, and it cannot decide that per page.

        Discovered links are inserted once per slice rather than once per page, so
        a 400-link page costs one INSERT and the rowcount still reports how many
        URLs were genuinely new.
        """
        allowed = allowed_mimes_for(include_pdf=config.include_pdf)
        gone: list[tuple[UUID, UUID | None]] = []

        while time.monotonic() < deadline:
            links = await self._repo.claim_pending(tenant_id, source_id, CRAWL_FETCH_SLICE)
            if not links:
                break

            outcomes = await self._read_slice(
                tenant_id,
                source_id,
                links,
                origin=origin,
                config=config,
                robots=robots,
                delay_ms=delay_ms,
                allowed=allowed,
            )

            candidates: list[LinkCandidate] = []
            for _link, outcome in zip(links, outcomes, strict=True):
                candidates.extend(outcome.candidates)
                result.retry_after = max(result.retry_after, outcome.retry_after)
                if outcome.gone is not None:
                    gone.append(outcome.gone)
                if outcome.state == "fetched":
                    result.fetched += 1
                elif outcome.state == "unchanged":
                    result.unchanged += 1
                elif outcome.state == "excluded":
                    result.excluded += 1
                elif outcome.state == "failed":
                    result.failed += 1
                elif outcome.state == "gone":
                    result.gone += 1

            if candidates:
                result.discovered += await self._insert(
                    tenant_id, source_id, candidates, origin=origin, config=config, robots=robots
                )
            # heartbeat, so the stall sweeper leaves a long but healthy wave alone
            await self._repo.touch_source(tenant_id, source_id)

        return gone

    async def _read_slice(
        self,
        tenant_id: UUID,
        source_id: UUID,
        links: list[dict[str, Any]],
        *,
        origin: str,
        config: CrawlConfig,
        robots: RobotsTxt,
        delay_ms: int,
        allowed: frozenset[str],
    ) -> list[_FetchOutcome]:
        """Read one claimed slice of the frontier: one browser batch per slice.

        Every URL the browser sees has passed the same three doors the httpx path
        enforced per request — robots, the urlguard, and (for results) the pinned
        origin — so widening the fetch layer to a browser widened nothing about
        who may be fetched. PDFs stay on the httpx path: they have no DOM to
        render and the existing byte pipeline trains them untouched.
        """
        outcomes: dict[UUID, _FetchOutcome] = {}
        pages: list[dict[str, Any]] = []

        for link in links:
            link_id: UUID = link["id"]
            url = str(link["url"])

            if unchanged_by_lastmod(link):
                # cheapest tier in §4.3: the sitemap already told us nothing moved
                await self._repo.update_link(
                    tenant_id, link_id, state="unchanged", fetched=True, clear_claim=True
                )
                outcomes[link_id] = _FetchOutcome(state="unchanged")
                continue

            if config.include_pdf and looks_like_pdf(url):
                outcome = await self._fetch_one(
                    tenant_id,
                    source_id,
                    link,
                    origin=origin,
                    config=config,
                    robots=robots,
                    delay_ms=delay_ms,
                    allowed=allowed,
                )
                outcomes[link_id] = outcome
                continue

            if not robots.allows(url):
                await self._repo.update_link(
                    tenant_id,
                    link_id,
                    state="excluded",
                    reason=REASON_ROBOTS_DISALLOWED,
                    reason_set=True,
                    clear_claim=True,
                )
                outcomes[link_id] = _FetchOutcome(state="excluded")
                continue

            if self._site_crawler is None:
                # no browser engine wired (a build without playwright): the httpx
                # path still reads the page, statically, as it always did
                outcomes[link_id] = await self._fetch_one(
                    tenant_id,
                    source_id,
                    link,
                    origin=origin,
                    config=config,
                    robots=robots,
                    delay_ms=delay_ms,
                    allowed=allowed,
                )
                continue

            if not await self._site_crawler.assert_safe(url):
                await self._repo.update_link(
                    tenant_id,
                    link_id,
                    state="excluded",
                    reason=REASON_BLOCKED_TARGET,
                    reason_set=True,
                    reason_detail="target refused by the fetch guard",
                    clear_claim=True,
                )
                outcomes[link_id] = _FetchOutcome(state="excluded")
                continue

            pages.append(link)

        crawler = self._site_crawler
        if pages and crawler is not None:
            by_request = {str(row["url"]): row for row in pages}
            async for page in crawler.read(
                [str(row["url"]) for row in pages], delay_ms=delay_ms
            ):
                row = by_request.pop(page.requested_url, None)
                if row is None:
                    continue
                try:
                    outcomes[row["id"]] = await self._read_one(
                        tenant_id,
                        source_id,
                        row,
                        page,
                        origin=origin,
                        config=config,
                    )
                except Exception as exc:
                    # an unexpected exception must not lose the whole slice: the
                    # link is recorded as failed and the wave keeps going
                    logger.warning(
                        "crawl_fetch_crashed",
                        source_id=str(source_id),
                        url=str(row.get("url")),
                        error=str(exc),
                    )
                    await self._repo.update_link(
                        tenant_id,
                        row["id"],
                        state="failed",
                        reason="fetch_error",
                        reason_set=True,
                        reason_detail=str(exc)[:500],
                        clear_claim=True,
                    )
                    outcomes[row["id"]] = _FetchOutcome(state="failed")

            # claimed but never answered: the batch died before this page was
            # read. recorded as failed so the attempt counter retires it, never
            # deleted — an outage must not shrink a corpus
            for url, row in by_request.items():
                logger.warning(
                    "crawl_read_unanswered", source_id=str(source_id), url=url[:200]
                )
                await self._repo.update_link(
                    tenant_id,
                    row["id"],
                    state="failed",
                    reason="fetch_error",
                    reason_set=True,
                    reason_detail="page was not returned by the read batch",
                    clear_claim=True,
                )
                outcomes[row["id"]] = _FetchOutcome(state="failed")

        return [outcomes.get(link["id"], _FetchOutcome(state="failed")) for link in links]

    async def _read_one(
        self,
        tenant_id: UUID,
        source_id: UUID,
        link: dict[str, Any],
        page: Any,
        *,
        origin: str,
        config: CrawlConfig,
    ) -> _FetchOutcome:
        """Record one browser-read page: verdicts first, then the raw markdown.

        The URL-level verdicts (gone, failed, off-origin, non-canonical,
        noindex) are decided here because they need the rendered response; the
        content-level ones (thin, duplicate) stay in the extract phase, which
        reads the stored markdown — the same split of concerns the httpx path
        had between fetch and extract.
        """
        link_id: UUID = link["id"]
        url = str(link["url"])
        depth = int(link.get("depth") or 0)

        if page.error or not page.status:
            await self._repo.update_link(
                tenant_id,
                link_id,
                state="failed",
                reason="fetch_error",
                reason_set=True,
                reason_detail=str(page.error or "no response")[:500],
                clear_claim=True,
            )
            return _FetchOutcome(state="failed")

        status = int(page.status)
        if status == 410 or (status == 404 and int(link.get("attempt") or 0) >= GONE_MIN_ATTEMPTS):
            await self._repo.update_link(
                tenant_id, link_id, state="gone", http_status=status,
                reason=None, reason_set=True, reason_detail=f"HTTP {status}", clear_claim=True,
            )
            document_id = link.get("document_id")
            return _FetchOutcome(
                state="gone",
                gone=(link_id, document_id if isinstance(document_id, UUID) else None),
            )

        if status >= 400:
            await self._repo.update_link(
                tenant_id, link_id, state="failed", http_status=status,
                reason="fetch_error", reason_set=True, reason_detail=f"HTTP {status}",
                clear_claim=True,
            )
            return _FetchOutcome(state="failed")

        final_url = str(page.final_url or url)
        if origin and not in_scope(final_url, origin, config.include_subdomains):
            # a redirect walked off the pinned origin; the browser followed it,
            # which is exactly why the final URL is re-checked here
            await self._repo.update_link(
                tenant_id, link_id, state="excluded", http_status=status,
                reason=REASON_REDIRECT_OFF_ORIGIN, reason_set=True, clear_claim=True,
            )
            return _FetchOutcome(state="excluded")

        here = canonicalise(url, origin=origin or None)
        there = canonicalise(final_url, origin=origin or None)
        if there is not None and here is not None and there != here:
            # the content lives at the redirect target; queue that and stand down
            await self._repo.update_link(
                tenant_id, link_id, state="excluded", http_status=status,
                reason=REASON_NON_CANONICAL, reason_set=True, reason_detail=there,
                clear_claim=True,
            )
            return _FetchOutcome(
                state="excluded",
                candidates=[LinkCandidate(url=there, depth=depth, discovered_via="redirect")],
            )

        if page.noindex:
            # the site's own instruction, same as the httpx path honoured
            await self._repo.update_link(
                tenant_id, link_id, state="excluded", http_status=status,
                reason=REASON_NOINDEX, reason_set=True, clear_claim=True,
            )
            return _FetchOutcome(state="excluded")

        if page.canonical_url:
            canonical = canonicalise(page.canonical_url, base=url, origin=origin or None)
            if canonical is not None and here is not None and canonical != here:
                await self._repo.update_link(
                    tenant_id, link_id, state="excluded", http_status=status,
                    reason=REASON_NON_CANONICAL, reason_set=True,
                    reason_detail=page.canonical_url, clear_claim=True,
                )
                return _FetchOutcome(
                    state="excluded",
                    candidates=[
                        LinkCandidate(url=canonical, depth=depth, discovered_via="canonical")
                    ],
                )

        markdown = str(page.markdown or "")
        body = markdown.encode("utf-8")
        if len(body) > self._settings.CRAWL_MAX_PAGE_BYTES:
            await self._repo.update_link(
                tenant_id, link_id, state="excluded", http_status=status,
                reason=REASON_TOO_LARGE, reason_set=True, clear_claim=True,
            )
            return _FetchOutcome(state="excluded")

        raw_path = crawl_raw_path(tenant_id, source_id, link_id)
        await self._storage.put(raw_path, body, "text/markdown")

        candidates: list[LinkCandidate] = []
        if depth < config.max_depth:
            candidates = [
                LinkCandidate(url=href, depth=depth + 1, discovered_via="link")
                for href in page.internal_links
            ]

        await self._repo.update_link(
            tenant_id,
            link_id,
            state="fetched",
            http_status=status,
            content_type="text/markdown",
            title=page.title,
            content_bytes=len(body),
            raw_path=raw_path,
            raw_path_set=True,
            reason=None,
            reason_set=True,
            fetched=True,
            clear_claim=True,
            reset_attempt=True,
        )
        return _FetchOutcome(state="fetched", candidates=candidates)

    async def _fetch_one(
        self,
        tenant_id: UUID,
        source_id: UUID,
        link: dict[str, Any],
        *,
        origin: str,
        config: CrawlConfig,
        robots: RobotsTxt,
        delay_ms: int,
        allowed: frozenset[str],
    ) -> _FetchOutcome:
        """Fetch one link and write its outcome. Never raises for an HTTP status.

        A status is data: a 404 is a row a tenant can see and restore, not an
        exception. Only a transport or policy failure comes back as
        :class:`CrawlFetchError`, and even that is recorded rather than propagated.
        """
        link_id: UUID = link["id"]
        url = str(link["url"])
        depth = int(link.get("depth") or 0)

        async with self._semaphore:
            decision = await self._fetcher.throttle(host_of(url), delay_ms)
            if not decision.allowed:
                # another wave holds this host's politeness lease. back to pending;
                # the attempt counter is left as claimed so a host that is
                # permanently contended still eventually stops being retried
                await self._repo.update_link(tenant_id, link_id, state="pending", clear_claim=True)
                return _FetchOutcome(
                    state="deferred", retry_after=math.ceil(decision.wait_ms / 1000)
                )

            if unchanged_by_lastmod(link):
                # cheapest tier in §4.3: the sitemap already told us nothing moved
                await self._repo.update_link(
                    tenant_id, link_id, state="unchanged", fetched=True, clear_claim=True
                )
                return _FetchOutcome(state="unchanged")

            etag, last_modified = conditional_headers(link)
            try:
                response = await self._fetcher.fetch(
                    url,
                    if_none_match=etag,
                    if_modified_since=last_modified,
                    allowed_mimes=allowed,
                )
            except CrawlFetchError as exc:
                return await self._record_error(tenant_id, link_id, exc)

        status = response.status
        if status == 304:
            await self._repo.update_link(
                tenant_id, link_id, state="unchanged", http_status=304, fetched=True,
                clear_claim=True, reset_attempt=True,
            )
            return _FetchOutcome(state="unchanged")

        if status == 410 or (status == 404 and int(link.get("attempt") or 0) >= GONE_MIN_ATTEMPTS):
            await self._repo.update_link(
                tenant_id, link_id, state="gone", http_status=status,
                reason=None, reason_set=True, reason_detail=f"HTTP {status}", clear_claim=True,
            )
            document_id = link.get("document_id")
            return _FetchOutcome(
                state="gone",
                gone=(link_id, document_id if isinstance(document_id, UUID) else None),
            )

        if status >= 400:
            retry_after = retry_after_seconds(response.headers) or 0
            await self._repo.update_link(
                tenant_id, link_id, state="failed", http_status=status,
                reason="fetch_error", reason_set=True, reason_detail=f"HTTP {status}",
                clear_claim=True,
            )
            return _FetchOutcome(state="failed", retry_after=retry_after)

        final_url = response.final_url
        if (
            response.redirect_chain
            and origin
            and not in_scope(final_url, origin, config.include_subdomains)
        ):
            # a redirect walked off the pinned origin. the row records why
            # without echoing where, because that target reaches the dashboard
            await self._repo.update_link(
                tenant_id, link_id, state="excluded", http_status=status,
                reason=REASON_REDIRECT_OFF_ORIGIN, reason_set=True, clear_claim=True,
            )
            return _FetchOutcome(state="excluded")

        here = canonicalise(url, origin=origin or None)
        there = canonicalise(final_url, origin=origin or None)
        if there is not None and here is not None and there != here:
            # the content lives at the redirect target; queue that and stand down
            await self._repo.update_link(
                tenant_id, link_id, state="excluded", http_status=status,
                reason=REASON_NON_CANONICAL, reason_set=True, reason_detail=there,
                clear_claim=True,
            )
            return _FetchOutcome(
                state="excluded",
                candidates=[LinkCandidate(url=there, depth=depth, discovered_via="redirect")],
            )

        mime = normalise_content_type(content_type_of(response.headers))
        if not mime:
            mime = CRAWL_PDF_MIME if looks_like_pdf(url) else "text/html"

        raw_path = crawl_raw_path(tenant_id, source_id, link_id)
        await self._storage.put(raw_path, response.body, mime)

        candidates: list[LinkCandidate] = []
        if mime != CRAWL_PDF_MIME and depth < config.max_depth:
            candidates = self._links_from(response.body, final_url, mime, origin, depth)

        await self._repo.update_link(
            tenant_id,
            link_id,
            state="fetched",
            http_status=status,
            content_type=mime,
            etag=response.headers.get("etag"),
            etag_set=True,
            last_modified=response.headers.get("last-modified"),
            last_modified_set=True,
            content_bytes=len(response.body),
            raw_path=raw_path,
            raw_path_set=True,
            reason=None,
            reason_set=True,
            fetched=True,
            clear_claim=True,
            reset_attempt=True,
        )
        return _FetchOutcome(state="fetched", candidates=candidates)

    async def _record_error(
        self, tenant_id: UUID, link_id: UUID, exc: CrawlFetchError
    ) -> _FetchOutcome:
        """Record a transport or policy failure against the link.

        A reason in the closed exclusion vocabulary is a decision — the page will
        never be fetchable under this config — so the row becomes `excluded` and
        stops costing requests. Anything else (timeout, connection reset) is
        `failed`: the document and its vectors are kept, because an outage must
        never delete a corpus.
        """
        excluded = exc.reason in EXCLUSION_REASONS
        await self._repo.update_link(
            tenant_id,
            link_id,
            state="excluded" if excluded else "failed",
            reason=exc.reason,
            reason_set=True,
            reason_detail=str(exc)[:500],
            clear_claim=True,
        )
        return _FetchOutcome(state="excluded" if excluded else "failed")

    def _links_from(
        self, body: bytes, base_url: str, mime: str, origin: str, depth: int
    ) -> list[LinkCandidate]:
        """`<a href>` targets one page revealed, at depth + 1.

        Extracted in the fetch phase rather than the extract phase so discovery
        keeps moving even when extraction is behind: the frontier grows from the
        bytes we already hold, and a wave that dies before extracting has still
        contributed its links.
        """
        try:
            html = decode_html(body, mime)
        except UndecodableError:
            return []
        return [
            LinkCandidate(url=url, depth=depth + 1, discovered_via="link")
            for url in extract_links(html, base_url, origin or None)
        ]

    # ---------------------------------------------------------------- boilerplate

    async def _freeze(self, tenant_id: UUID, source_id: UUID) -> frozenset[str]:
        """Learn this source's chrome once, from the pages already fetched.

        Deferred until either the sample is full or the frontier is drained, so a
        400-page site does not freeze its fingerprint from the two pages that
        happened to arrive first. Extraction waits for this, which is what keeps it
        single-pass: no page is ever re-indexed later just to strip a nav bar.
        """
        fetched = await self._repo.count_links(tenant_id, source_id, state="fetched")
        if fetched == 0:
            return frozenset()
        if fetched < CRAWL_BOILERPLATE_SAMPLE and await self._repo.pending_count(
            tenant_id, source_id
        ):
            return frozenset()

        rows = await self._repo.list_links(
            tenant_id, source_id, state="fetched", limit=CRAWL_BOILERPLATE_SAMPLE
        )
        markdowns: list[str] = []
        for row in rows:
            raw_path = row.get("raw_path")
            mime = normalise_content_type(str(row.get("content_type") or ""))
            if not raw_path or mime == CRAWL_PDF_MIME:
                continue
            data = await self._read_raw(str(raw_path))
            if data is None:
                continue
            if mime == "text/markdown":
                # the library already extracted this page; the blob is the sample
                markdowns.append(data.decode("utf-8", errors="replace"))
                continue
            try:
                sample = await asyncio.to_thread(
                    extract_page,
                    data, str(row["url"]), content_type=mime or None, collect_links=False,
                )
            except UndecodableError:
                continue
            markdowns.append(sample.markdown)

        fingerprints = freeze_boilerplate(markdowns)
        await self._repo.set_boilerplate(tenant_id, source_id, sorted(fingerprints))
        logger.info(
            "crawl_boilerplate_frozen",
            source_id=str(source_id),
            sample=len(markdowns),
            blocks=len(fingerprints),
        )
        return fingerprints

    # ----------------------------------------------------------------- extracting

    async def _extract_phase(
        self,
        tenant_id: UUID,
        source_id: UUID,
        *,
        origin: str,
        config: CrawlConfig,
        boilerplate: frozenset[str],
        site_name: str | None,
        deadline: float,
        result: WaveResult,
        render_budget: int = 0,
    ) -> None:
        """Turn fetched pages into documents, several at a time.

        Concurrent on purpose: at this layer a page is storage and repository
        round trips (read the raw blob, judge it, write the link, write the
        document), not CPU — the parse inside each page runs off the event loop
        via :func:`asyncio.to_thread`, so pages in flight do not stall each
        other's writes.

        The near-duplicate index is loaded once per phase and appended to as
        pages train. Without it every page pays a full-candidates SELECT, and a
        source with N pages runs N of them — O(N²) rows moved for decisions the
        wave could have made from one load.

        Trained pages are handed to the ingest queue in batches
        (:meth:`_enqueue_ingests`) rather than one message per page, because a
        queue that bills by the message makes 1800 pages at one message each a
        quota event, not a crawl.
        """
        renders = _RenderBudget(render_budget)
        simhash_index: list[tuple[str, int]] = list(
            await self._repo.simhash_candidates(tenant_id, source_id)
        )
        in_flight = asyncio.Semaphore(max(1, self._settings.CRAWL_EXTRACT_CONCURRENCY))

        while time.monotonic() < deadline:
            rows = await self._repo.claim_fetched(
                tenant_id,
                source_id,
                CRAWL_EXTRACT_SLICE,
                ttl_seconds=self._settings.CRAWL_LINK_CLAIM_TTL_SECONDS,
            )
            if not rows:
                break

            async def one(row: dict[str, Any]) -> tuple[str, bool, tuple[UUID, str] | None]:
                async with in_flight:
                    try:
                        return await self._extract_one(
                            tenant_id,
                            source_id,
                            row,
                            origin=origin,
                            config=config,
                            boilerplate=boilerplate,
                            site_name=site_name,
                            simhash_index=simhash_index,
                            renders=renders,
                        )
                    except Exception as exc:
                        logger.warning(
                            "crawl_extract_failed",
                            source_id=str(source_id),
                            url=str(row.get("url")),
                            error=str(exc),
                            exc_info=True,
                        )
                        await self._repo.update_link(
                            tenant_id,
                            row["id"],
                            state="failed",
                            reason="extract_error",
                            reason_set=True,
                            reason_detail=str(exc)[:500],
                            clear_claim=True,
                        )
                        return "failed", False, None

            outcomes = await asyncio.gather(*(one(row) for row in rows))

            ingests: list[tuple[UUID, str]] = []
            for state, used_render, ingest in outcomes:
                if used_render:
                    result.rendered += 1
                if state == "trained":
                    result.trained += 1
                    if ingest is not None:
                        ingests.append(ingest)
                elif state == "unchanged":
                    result.unchanged += 1
                elif state == "duplicate":
                    result.duplicates += 1
                    result.excluded += 1
                elif state == "excluded":
                    result.excluded += 1
                elif state == "failed":
                    result.failed += 1

            if ingests:
                await self._enqueue_ingests(tenant_id, ingests)
            # heartbeat, so the stall sweeper leaves a long but healthy wave alone
            await self._repo.touch_source(tenant_id, source_id)

    async def _extract_one(
        self,
        tenant_id: UUID,
        source_id: UUID,
        row: dict[str, Any],
        *,
        origin: str,
        config: CrawlConfig,
        boilerplate: frozenset[str],
        site_name: str | None,
        simhash_index: list[tuple[str, int]] | None = None,
        renders: _RenderBudget | None = None,
    ) -> tuple[str, bool, tuple[UUID, str] | None]:
        """Extract, judge and (if it survives) index one fetched page.

        Returns `(state, used_render, ingest)`; `ingest` is the
        `(document_id, storage_path)` pair the caller batches into one ingest
        queue message, or None when nothing was trained.
        """
        link_id: UUID = row["id"]
        url = str(row["url"])
        raw_path = row.get("raw_path")

        if not raw_path:
            # nothing to extract from: fetch again rather than invent content
            await self._repo.update_link(tenant_id, link_id, state="pending", clear_claim=True)
            return "requeued", False, None

        data = await self._read_raw(str(raw_path))
        if data is None:
            await self._repo.update_link(
                tenant_id, link_id, state="pending", clear_claim=True,
                raw_path=None, raw_path_set=True,
            )
            return "requeued", False, None

        mime = normalise_content_type(str(row.get("content_type") or ""))
        if mime == CRAWL_PDF_MIME:
            document_id, path = await self._train_pdf(
                tenant_id, source_id, row, data=data, url=url
            )
            await self._drop_raw(str(raw_path))
            return "trained", False, (document_id, path)

        rendered = False
        if mime == "text/markdown":
            # a library-read page: the browser rendered it and the library turned
            # it into markdown at fetch time. noindex, canonical and scope were
            # judged during the read, so this is content-only — hash, simhash,
            # boilerplate, thinness
            page = page_from_markdown(
                data.decode("utf-8", errors="replace"),
                url,
                title=row.get("title"),
                boilerplate=boilerplate,
                lang=None,
                min_text_chars=self._settings.CRAWL_MIN_TEXT_CHARS,
            )
            verdict = rejection(page, url, origin=origin or None)
        else:
            try:
                # off the event loop: parsing is CPU-bound and a slice of pages must
                # not stall the wave's fetches while it runs
                page = await asyncio.to_thread(
                    extract_page,
                    data,
                    url,
                    content_type=mime or None,
                    boilerplate=boilerplate,
                    site_name=site_name,
                    origin=origin or None,
                    collect_links=False,
                    min_text_chars=self._settings.CRAWL_MIN_TEXT_CHARS,
                )
            except UndecodableError as exc:
                await self._repo.update_link(
                    tenant_id, link_id, state="excluded", reason=REASON_NON_HTML,
                    reason_set=True, reason_detail=str(exc)[:200], clear_claim=True,
                    raw_path=None, raw_path_set=True,
                )
                await self._drop_raw(str(raw_path))
                return "excluded", False, None

            verdict = rejection(page, url, origin=origin or None)
        if (
            verdict is not None
            and verdict[0] == REASON_JAVASCRIPT_RENDERED
            and self._renderer is not None
            and renders is not None
            and await renders.take()
        ):
            replacement = await self._render(
                tenant_id, url, boilerplate=boilerplate, site_name=site_name, origin=origin
            )
            if replacement is not None:
                page, rendered = replacement, True
                verdict = rejection(page, url, origin=origin or None)

        if verdict is not None:
            reason, detail = verdict
            await self._repo.update_link(
                tenant_id, link_id, state="excluded", reason=reason, reason_set=True,
                reason_detail=detail, title=page.title, clear_claim=True,
                raw_path=None, raw_path_set=True,
            )
            await self._drop_raw(str(raw_path))
            return "excluded", rendered, None

        existing_document = row.get("document_id")
        if row.get("content_hash") == page.content_hash and isinstance(existing_document, UUID):
            # §4.3's fallback tier: the site sent no usable validators but the
            # extracted markdown is identical, so nothing is re-embedded
            await self._repo.update_link(
                tenant_id, link_id, state="unchanged", clear_claim=True,
                raw_path=None, raw_path_set=True,
            )
            await self._drop_raw(str(raw_path))
            return "unchanged", rendered, None

        duplicate = await is_duplicate(
            self._repo,
            tenant_id,
            source_id,
            page=page,
            exclude_id=link_id,
            max_distance=self._settings.CRAWL_SIMHASH_MAX_DISTANCE,
            simhash_index=simhash_index,
        )
        if duplicate:
            await self._repo.update_link(
                tenant_id, link_id, state="excluded", reason=REASON_DUPLICATE_CONTENT,
                reason_set=True, reason_detail=duplicate, title=page.title,
                content_hash=page.content_hash, simhash=to_signed_64(page.simhash),
                clear_claim=True, raw_path=None, raw_path_set=True,
            )
            await self._drop_raw(str(raw_path))
            return "duplicate", rendered, None

        document_id, path = await self._train(
            tenant_id, source_id, row, page=page, url=url
        )
        await self._repo.update_link(
            tenant_id,
            link_id,
            state="trained",
            content_hash=page.content_hash,
            simhash=to_signed_64(page.simhash),
            title=page.title,
            document_id=document_id,
            document_set=True,
            reason=None,
            reason_set=True,
            clear_claim=True,
            raw_path=None,
            raw_path_set=True,
        )
        await self._drop_raw(str(raw_path))
        # visible to this wave's later pages: the simhash scan only reads the
        # index, and a page trained a moment ago is exactly the near-duplicate
        # the next one has to be compared against
        if simhash_index is not None and page.simhash:
            simhash_index.append((url, to_signed_64(page.simhash)))
        return "trained", rendered, (document_id, path)

    # -------------------------------------------------------------------- indexing

    async def _train(
        self, tenant_id: UUID, source_id: UUID, row: dict[str, Any], *, page: PageContent, url: str
    ) -> tuple[UUID, str]:
        """Make (or refresh) the document behind one crawled page.

        A crawled page is an ordinary `documents` row whose bytes are the extracted
        markdown, indexed by the existing ingest pipeline. There is no
        parallel indexing path: `TextParser` passes markdown through untouched and
        `has_structure` picks the structured splitter because the markdown has
        headings, so a crawled page chunks exactly like an uploaded one.

        Returns `(document_id, storage_path)`; the caller batches these into
        :meth:`_enqueue_ingests` so a whole-site crawl costs one queue message
        per handful of pages rather than one per page.
        """
        body = page.markdown.encode("utf-8")
        digest = hashlib.sha256(body).hexdigest()
        existing = row.get("document_id")

        if isinstance(existing, UUID):
            # §4.8: the sha256 has to move with the bytes, or find_by_hash misses
            # forever and the page is re-indexed on every single cycle
            await self._documents.replace_content(
                tenant_id, existing, sha256=digest, size_bytes=len(body), mime="text/markdown"
            )
            document_id = existing
        else:
            document = await self._documents.create(
                tenant_id=tenant_id,
                filename=_filename_for(page.title, url),
                mime="text/markdown",
                size_bytes=len(body),
                sha256=digest,
                storage_path="",
                metadata={
                    "source_url": url,
                    "heading_path": page.heading_path,
                    "lang": page.lang,
                    "crawl_depth": int(row.get("depth") or 0),
                },
                source="crawl",
                source_id=source_id,
                source_url=url,
            )
            document_id = document.id
        path = document_path(tenant_id, document_id)
        await self._storage.put(path, body, "text/markdown")
        return document_id, path

    async def _train_pdf(
        self, tenant_id: UUID, source_id: UUID, row: dict[str, Any], *, data: bytes, url: str
    ) -> tuple[UUID, str]:
        """A crawled PDF: raw bytes straight into the existing pipeline.

        No web extraction at all — `PdfParser` and, where enabled, OCR already do
        this better than anything this module could. The link's `content_hash` is
        the hash of the bytes here rather than of extracted markdown, because there
        is no extraction step to hash.

        Returns `(document_id, storage_path)` for the caller to batch, exactly as
        :meth:`_train` does.
        """
        link_id: UUID = row["id"]
        digest = hashlib.sha256(data).hexdigest()
        existing = row.get("document_id")

        if isinstance(existing, UUID):
            await self._documents.replace_content(
                tenant_id, existing, sha256=digest, size_bytes=len(data), mime=CRAWL_PDF_MIME
            )
            document_id = existing
        else:
            document = await self._documents.create(
                tenant_id=tenant_id,
                filename=_filename_for(None, url),
                mime=CRAWL_PDF_MIME,
                size_bytes=len(data),
                sha256=digest,
                storage_path="",
                metadata={"source_url": url, "crawl_depth": int(row.get("depth") or 0)},
                source="crawl",
                source_id=source_id,
                source_url=url,
            )
            document_id = document.id

        path = document_path(tenant_id, document_id)
        await self._storage.put(path, data, CRAWL_PDF_MIME)
        await self._repo.update_link(
            tenant_id,
            link_id,
            state="trained",
            content_hash=digest,
            content_bytes=len(data),
            title=_filename_for(None, url),
            document_id=document_id,
            document_set=True,
            reason=None,
            reason_set=True,
            clear_claim=True,
            raw_path=None,
            raw_path_set=True,
        )
        return document_id, path

    async def _enqueue_ingests(self, tenant_id: UUID, pairs: list[tuple[UUID, str]]) -> None:
        """Hand trained pages to the ingest pipeline, batched per queue message.

        One `ingest_documents` message per INGEST_BATCH_DOCS pages: a queue that
        bills by the message makes 1800 pages at one message each a quota event
        rather than a crawl. The bytes are already durable, so a queue outage
        must not lose them: the documents are marked failed with a reason the
        dashboard's Retry button re-enqueues, exactly as the upload endpoint
        does.
        """
        batch = max(1, self._settings.INGEST_BATCH_DOCS)
        for start in range(0, len(pairs), batch):
            chunk = pairs[start : start + batch]
            try:
                await self._queue.enqueue(
                    Job(
                        kind="ingest_documents",
                        tenant_id=tenant_id,
                        payload={
                            "docs": [
                                {"document_id": str(document_id), "storage_path": path}
                                for document_id, path in chunk
                            ]
                        },
                    )
                )
            except Exception as exc:
                logger.error(
                    "crawl_ingest_enqueue_failed",
                    documents=[str(document_id) for document_id, _ in chunk],
                    error=str(exc),
                    exc_info=True,
                )
                for document_id, _ in chunk:
                    await self._documents.set_status(
                        tenant_id,
                        document_id,
                        DocumentStatus.FAILED,
                        "could not be queued for processing; retry this document",
                    )

    # -------------------------------------------------------------------- renderer

    async def _render(
        self,
        tenant_id: UUID,
        url: str,
        *,
        boilerplate: frozenset[str],
        site_name: str | None,
        origin: str,
    ) -> PageContent | None:
        """One hosted browser render, metered. None means "keep the verdict".

        A render is billable, so a failure is recorded and abandoned rather than
        retried: a retry loop around a metered call is a billing incident.
        """
        if self._renderer is None:
            return None
        try:
            result = await self._renderer.render(url)
        except Exception as exc:
            logger.warning("crawl_render_failed", url=url, error=str(exc))
            return None

        await self._meter_render(tenant_id, url)
        try:
            return await asyncio.to_thread(
                extract_page,
                result.html.encode("utf-8"),
                url,
                content_type="text/html; charset=utf-8",
                boilerplate=boilerplate,
                site_name=site_name,
                origin=origin or None,
                collect_links=False,
                min_text_chars=self._settings.CRAWL_MIN_TEXT_CHARS,
            )
        except UndecodableError:
            return None

    async def _meter_render(self, tenant_id: UUID, url: str) -> None:
        """Record the render as usage. Metering failure must not lose the page."""
        try:
            await self._repo.record_usage(
                tenant_id, UsageKind.RENDER_PAGE.value, 1, meta={"url": url[:500]}
            )
        except Exception as exc:  # pragma: no cover - ledger outage
            logger.warning("crawl_render_usage_failed", error=str(exc))

    # ------------------------------------------------------------------ finishing

    async def _finish(
        self,
        tenant_id: UUID,
        source_id: UUID,
        *,
        source: dict[str, Any],
        delay_ms: int,
        gone: list[tuple[UUID, UUID | None]],
        result: WaveResult,
        gated: bool = False,
    ) -> WaveResult:
        """Recompute counters, apply deletions, then terminate or enqueue the next wave."""
        counters = await self._repo.recompute_counters(tenant_id, source_id)
        result.counters = counters

        if await self._apply_gone(tenant_id, source_id, gone=gone, counters=counters):
            result.state = "failed"
            result.detail = MASS_DELETE_MESSAGE
            return result

        config = self._config_of(source)
        # Not while gated. The gate's product is a list of the site, and discovery
        # does not apply the page budget while gathering it, so a gated wave holds
        # every URL the site offered — capping here would refuse the tenant's own
        # review list before they had read it. The budget belongs on the far side of
        # the gate: they uncheck what they do not want, approval applies that, and
        # the wave that then fetches is not gated, so this cap runs on the set they
        # actually asked for and keeps a budget's worth of it.
        if not gated and counters.get("link_total", 0) >= config.max_pages:
            capped = await self._cap_frontier(
                tenant_id, source_id, max_pages=config.max_pages, detail=str(config.max_pages)
            )
            if capped:
                result.excluded += capped
                counters = await self._repo.recompute_counters(tenant_id, source_id)
                result.counters = counters

        # The wall-clock ceiling for the whole source, not for one wave. A wave has
        # its own budget and simply enqueues the next one when it runs out, so a
        # site that answers slowly, or keeps offering links faster than they train,
        # would otherwise re-queue itself forever. Checked here rather than mid-wave
        # because this is the only place that decides whether another wave happens,
        # and the pages already fetched must still be kept.
        #
        # The remaining frontier is refused as `time_budget_exceeded` rather than
        # left pending: pending rows are what the stall sweeper restarts, so leaving
        # them would defeat the budget on the next tick, and the tenant needs to see
        # a reason with a Restore next to it.
        elapsed = _elapsed_seconds(source)
        budget = max(0, self._settings.CRAWL_TOTAL_BUDGET_SECONDS)
        if (
            budget
            and elapsed is not None
            and elapsed >= budget
            and counters.get("link_pending", 0) > 0
        ):
            capped = await self._cap_frontier(
                tenant_id, source_id, max_pages=0, reason=REASON_TIME_BUDGET_EXCEEDED
            )
            logger.warning(
                "crawl_time_budget_exceeded",
                source_id=str(source_id),
                elapsed_seconds=round(elapsed),
                budget_seconds=budget,
                capped=capped,
            )
            if capped:
                result.excluded += capped
            counters = await self._repo.recompute_counters(tenant_id, source_id)
            result.counters = counters

        # The review gate. Tested before the ordinary completion check because that
        # one reads `link_pending`, and a parked crawl is exactly a crawl with rows
        # left in it — it would re-enqueue itself forever with nothing to do.
        #
        # What counts as reviewable is any row the approval would index: a page that
        # has been read, and a page that has only been found. The second is the point
        # — a sitemap tells us the whole site in one request, so the list is complete
        # long before any page is fetched, and waiting for the fetch is waiting for
        # nothing. Fetching happens after approval, on the pages that survived it.
        #
        # `awaiting_review` is deliberately outside RUNNABLE_STATES and outside the
        # stall sweeper's index: a parked crawl has not stalled, and restarting it is
        # the one thing that would defeat the gate.
        if gated:
            reviewable = await self._repo.park_for_review(
                tenant_id, source_id, states=REVIEW_FRONTIER_STATES
            )
            # nothing found means nothing to approve. falling through to the normal
            # ready/failed verdict is what stops a site that refused every URL from
            # parking on an empty list nobody can act on
            if reviewable > 0:
                result.state = "awaiting_review"
                logger.info(
                    "crawl_awaiting_review",
                    source_id=str(source_id),
                    reviewable=reviewable,
                    excluded=counters.get("link_excluded", 0),
                )
                return result
            logger.info(
                "crawl_review_skipped_empty",
                source_id=str(source_id),
                excluded=counters.get("link_excluded", 0),
            )

        if counters.get("link_pending", 0) == 0:
            trained = counters.get("link_trained", 0)
            state = "ready" if trained > 0 else "failed"
            await self._repo.set_crawl_state(
                tenant_id,
                source_id,
                state,
                trained=trained > 0,
                checked=True,
                pending_changes=False,
                error=None if trained > 0 else "No pages could be trained from this site.",
                clear_error=trained > 0,
            )
            if state == "ready":
                # P4, trigger (audit G6): there is no crawl completion event,
                # and this verdict is the one place every crawl converges —
                # gated and ungated alike, which for a fresh website source
                # means right after review approval applied the frontier and
                # the pages are actually indexed. Enqueued, not run inline, so
                # the profile's LLM call can never be what a crawl's 200/500
                # answer hangs on. Which crawl is the tenant's own site is the
                # job's question to answer (profile.website match), not the
                # engine's: deciding here would pull growth settings into the
                # crawl path.
                await self._queue_profile(tenant_id, source_id)
            result.state = state
            logger.info(
                "crawl_finished",
                source_id=str(source_id),
                state=state,
                trained=counters.get("link_trained", 0),
                excluded=counters.get("link_excluded", 0),
                failed=counters.get("link_failed", 0),
            )
            return result

        delay = max(math.ceil(delay_ms / 1000), result.retry_after, 1)
        try:
            await self._queue.enqueue(
                Job(
                    kind="crawl_wave",
                    tenant_id=tenant_id,
                    payload={"source_id": str(source_id)},
                    delay_seconds=delay,
                )
            )
            result.next_wave = True
            result.state = "fetching"
        except Exception as exc:
            # the frontier is durable, so the stall sweeper will pick this up. the
            # crawl is not lost, only paused
            logger.error(
                "crawl_wave_enqueue_failed", source_id=str(source_id), error=str(exc), exc_info=True
            )
            result.detail = "next wave could not be queued"
        return result

    async def _queue_profile(self, tenant_id: UUID, source_id: UUID) -> None:
        """Hand a source that just reached ready to the profile agent (P4).

        Failure is logged, never raised: the wave finished, the pages are
        indexed, and a profile job the queue refused must not turn that into a
        failed crawl. The owner's on-demand endpoint covers the missed run.
        """
        try:
            await self._queue.enqueue(
                Job(
                    kind="growth_profile",
                    tenant_id=tenant_id,
                    payload={"source_id": str(source_id)},
                )
            )
        except Exception as exc:
            logger.warning(
                "crawl_profile_enqueue_failed",
                source_id=str(source_id),
                error=str(exc),
            )

    async def _apply_gone(
        self,
        tenant_id: UUID,
        source_id: UUID,
        *,
        gone: list[tuple[UUID, UUID | None]],
        counters: dict[str, int],
    ) -> bool:
        """Delete the documents behind vanished pages — unless too many vanished.

        THE circuit breaker. A site behind a broken deploy, an expired certificate
        or a new WAF answers 404/410 for everything, and applying those deletions
        would wipe a customer's knowledge base with no way back except a full
        re-crawl. Above :data:`CRAWL_MASS_DELETE_RATIO` the whole pass is treated as
        one site-wide event: nothing is deleted, the source is marked failed with an
        explanation, and `pending_changes` invites a retrain once the site is fixed.
        """
        if not gone:
            return False

        trained = counters.get("link_trained", 0)
        ratio = len(gone) / max(1, trained + len(gone))
        if ratio > CRAWL_MASS_DELETE_RATIO:
            logger.error(
                "crawl_mass_delete_blocked",
                source_id=str(source_id),
                gone=len(gone),
                trained=trained,
                ratio=round(ratio, 3),
            )
            await self._repo.set_crawl_state(
                tenant_id, source_id, "failed", error=MASS_DELETE_MESSAGE, pending_changes=True
            )
            return True

        for link_id, document_id in gone:
            if document_id is None:
                continue
            if self._indexer is not None:
                await self._indexer.remove(tenant_id, document_id)
            await self._documents.soft_delete(tenant_id, document_id)
            # the link row stays, so the UI can still say "this page 404'd", but it
            # no longer points at a document that does not exist
            await self._repo.update_link(
                tenant_id, link_id, document_id=None, document_set=True
            )
        return False

    async def _cap_frontier(
        self,
        tenant_id: UUID,
        source_id: UUID,
        *,
        max_pages: int = 0,
        reason: str = REASON_PAGE_BUDGET_EXCEEDED,
        detail: str | None = None,
    ) -> int:
        """Refuse the part of the frontier that does not fit inside a budget.

        Recorded with a machine reason rather than deleted, so the review list can
        show a tenant exactly which pages their limit cost them and Restore remains
        meaningful after they raise it. `reason` says which limit stopped the crawl:
        the page budget, or the wall-clock budget for the whole source.

        `max_pages` is how many pages this source may hold in total, counting what
        it already has — so the page budget keeps a budget's worth of the frontier
        and refuses the rest. The wall-clock budget passes 0, which refuses all of
        it: once time is up no page is worth keeping over another.
        """
        capped = await self._repo.cap_frontier(
            tenant_id,
            source_id,
            max_pages=max_pages,
            reason=reason,
            reason_detail=detail,
        )
        return int(capped)

    async def _read_raw(self, path: str) -> bytes | None:
        try:
            return bytes(await self._storage.get(path))
        except Exception:
            # the blob is scratch; if it is gone the page is simply fetched again
            logger.info("crawl_raw_missing", path=path)
            return None

    async def _drop_raw(self, path: str) -> None:
        try:
            await self._storage.delete(path)
        except Exception:
            # a leaked scratch blob costs storage, not correctness
            logger.info("crawl_raw_delete_failed", path=path)


def _boilerplate_of(source: dict[str, Any]) -> frozenset[str]:
    """The frozen fingerprint set off a source row, whatever shape the driver gave."""
    raw = source.get("boilerplate")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return frozenset()
    if isinstance(raw, list):
        return frozenset(str(item) for item in raw)
    return frozenset()


def _awaiting_approval(source: Mapping[str, Any], config: CrawlConfig) -> bool:
    """True when this source wants a review gate and has not been through one.

    Two facts, not one. The config flag is the tenant's standing preference and it
    survives approval, so the next retrain gates again — which is the whole point of
    asking for review. `review_approved_at` is the fact that this crawl's list has
    been approved. Testing only the flag would park forever; testing only the
    timestamp would let one approval disarm the gate for good.
    """
    if not config.review_before_index:
        return False
    return source.get("review_approved_at") is None


def _elapsed_seconds(source: Mapping[str, Any]) -> float | None:
    """Seconds since this crawl cycle started, or None when that is unknown.

    `crawl_started_at` is stamped by `set_crawl_state(..., started=True)` at create
    and at every retrain, so it measures the current cycle rather than the source's
    age. None when the column is null — an older row that predates the column, or a
    source that never ran — and None is deliberately not treated as "expired": a
    missing timestamp must never cap a frontier.
    """
    raw = source.get("crawl_started_at")
    if isinstance(raw, str):
        parsed = parse_lastmod(raw)
    elif isinstance(raw, datetime):
        parsed = raw
    else:
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return max(0.0, (datetime.now(UTC) - parsed).total_seconds())


def _render_budget(settings: Settings, source: dict[str, Any]) -> int:
    """How many hosted renders this source may spend in one wave.

    Zero unless `config.js_rendering` is true, which `SourceService` writes from
    what the tenant asked. The engine deliberately reads the decision off the
    row rather than resolving it here, so a change takes effect on the next
    config write with no extra lookup per page.
    """
    raw = source.get("config")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return 0
    if not isinstance(raw, dict) or raw.get("js_rendering") is not True:
        return 0
    return max(0, settings.CRAWL_MAX_RENDERS_PER_SOURCE)


def _filename_for(title: str | None, url: str) -> str:
    """The label a citation shows for a crawled page.

    The page's own title when it has one, because that is what a human recognises;
    the last path segment otherwise, and the host for a bare root URL. Never empty,
    because `documents.filename` is `NOT NULL` and an empty citation label is worse
    than an ugly one.
    """
    if title and title.strip():
        return title.strip()[:255]
    tail = url.split("?", 1)[0].split("#", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    if tail and tail not in ("http:", "https:"):
        return tail[:255]
    return (host_of(url) or "page")[:255]
