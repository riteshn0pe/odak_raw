"""crawl4ai behind the app's crawl product: site discovery and page reads.

The wave engine's hand-rolled fetch core could walk a sitemap and parse static
HTML, but it could not render JavaScript without a metered hosted renderer and
could not enumerate a site that has no sitemap. crawl4ai (Apache-2.0,
pip-installed, no API key) is the industry answer for exactly this shape of
work: a real headless browser renders every page, and a bounded breadth-first
deep crawl discovers the site's URL space. This module is the only place the
library is touched, so swapping or upgrading it is a one-file change.

What deliberately stays outside this module:

* the frontier, the state machine, the review gate, budgets and ingest fan-out
  live in the engine and the repository, unchanged. The library is a fetch
  layer, not an orchestrator — a crawl is still a sequence of resumable waves.
* the SSRF discipline. The browser opens its own sockets, so the per-hop checks
  the httpx path enforced in ``CrawlFetcher._open`` are applied here instead:
  every URL is validated by ``assert_fetchable`` before the browser sees it
  (once per page for reads, once per distinct host for the discovery walk), and
  the deep-crawl filter refuses anything out of scope, robots-disallowed or
  structurally unsafe before the strategy even queues it. A browser follows
  redirects internally, which no wrapper can re-validate hop by hop; that
  residual is closed the same way the httpx path closes its redirects — the
  engine checks the *final* URL against the pinned origin and drops off-origin
  results.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from app.core.config import Settings
from app.core.errors import AppError, ValidationError
from app.core.logging import get_logger
from app.core.urlguard import assert_fetchable
from app.domain.crawl import canonicalise, extract_canonical, in_scope
from app.domain.robots import RobotsTxt

logger = get_logger(__name__)


class LibraryUnavailableError(AppError):
    """The browser engine could not start (missing binary, sandbox refusal)."""

    def __init__(self, message: str = "browser engine unavailable") -> None:
        super().__init__(message)


@dataclass(slots=True)
class LibraryPage:
    """One page as the library read it.

    `final_url` is where the browser actually landed (redirects included), which
    is what the engine's off-origin and canonical checks read; `head` carries
    the page's own indexing instructions, read once here so the extract phase
    never needs the HTML again.
    """

    requested_url: str
    final_url: str
    status: int
    markdown: str = ""
    title: str | None = None
    internal_links: list[str] = field(default_factory=list)
    head: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def noindex(self) -> bool:
        return bool(self.head.get("noindex"))

    @property
    def canonical_url(self) -> str | None:
        value = self.head.get("canonical")
        return str(value) if value else None


class SiteScopeFilter:
    """The deep crawl's admission filter: scope, robots and SSRF.

    Runs before the strategy queues a URL, so an out-of-scope or private link
    costs nothing — the browser never opens it. `apply` is synchronous (the
    library's contract), so the urlguard's DNS resolution blocks the loop for
    one lookup per *distinct* host and is cached after that; a single-host crawl,
    the overwhelmingly common case, pays it exactly once.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        origin: str,
        include_subdomains: bool,
        robots: RobotsTxt,
    ) -> None:
        self._origin = origin
        self._include_subdomains = include_subdomains
        self._robots = robots
        self._allow_private = settings.URL_FETCH_ALLOW_PRIVATE
        self._ports = settings.url_fetch_ports
        self._hosts: dict[str, bool] = {}

    def apply(self, url: str) -> bool:
        try:
            parts = urlsplit(url)
        except ValueError:
            return False
        if parts.scheme not in ("http", "https"):
            return False
        # embedded credentials would be handed to whatever the host resolves to
        if parts.username or parts.password:
            return False
        host = (parts.hostname or "").lower()
        if not host:
            return False
        if not in_scope(url, self._origin, self._include_subdomains):
            return False
        if not self._robots.allows(url):
            return False
        safe = self._hosts.get(host)
        if safe is None:
            try:
                assert_fetchable(
                    url, allow_private=self._allow_private, allowed_ports=self._ports
                )
                safe = True
            except ValidationError:
                safe = False
            self._hosts[host] = safe
        return safe


def _head_of(html: str, url: str) -> dict[str, Any]:
    """The indexing-relevant parts of a rendered page's head, off the event loop.

    Deliberately only what a fetch decision needs — noindex, the canonical URL,
    the language tag. The readability work is the library's job now; this is the
    page's own metadata, which no markdown generator preserves.
    """
    from bs4 import BeautifulSoup

    from app.adapters.parsers.web import has_noindex

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:  # pragma: no cover - bs4 does not raise on any input
        return {}
    node = soup.find("html")
    lang = None
    if node is not None:
        value = node.get("lang")
        if isinstance(value, str) and value.strip():
            lang = value.strip().split("-")[0].lower()[:8]
    return {
        "noindex": has_noindex(soup),
        "canonical": extract_canonical(html, base_url=url) or None,
        "lang": lang,
    }


class SiteCrawler:
    """One crawl4ai browser for the process, started on first use.

    Constructed by the container and closed with it. The browser is expensive
    to start (a Chromium process), so it is never started for sitemap-only
    sources; the first page read or deep crawl pays the startup once and every
    later wave reuses it.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._crawler: Any = None
        self._lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return "site_crawler"

    # ------------------------------------------------------------------ lifecycle

    async def _start(self) -> Any:
        crawler = self._crawler
        if crawler is not None:
            return crawler
        async with self._lock:
            # re-read under the lock: another task may have started it already
            crawler = self._crawler
            if crawler is None:
                from crawl4ai import AsyncWebCrawler, BrowserConfig

                browser = BrowserConfig(
                    headless=True,
                    user_agent=self._settings.crawl_user_agent,
                    verbose=False,
                    # parity with the httpx path, which verifies certificates
                    ignore_https_errors=False,
                    # empty on a workstation, which is crawl4ai's own default --
                    # the flags only matter in a container, where Chromium will
                    # not start as root without --no-sandbox
                    extra_args=self._settings.crawl_browser_args,
                )
                started = AsyncWebCrawler(config=browser)
                try:
                    await started.__aenter__()
                except Exception as exc:
                    raise LibraryUnavailableError(
                        f"browser engine could not start: {exc}"
                    ) from exc
                crawler = started
                self._crawler = crawler
        return crawler

    async def close(self) -> None:
        crawler, self._crawler = self._crawler, None
        if crawler is None:
            return
        try:
            await crawler.__aexit__(None, None, None)
        except Exception as exc:  # pragma: no cover - shutdown is best effort
            logger.warning("site_crawler_close_failed", error=str(exc))

    # ------------------------------------------------------------------ public API

    async def assert_safe(self, url: str) -> bool:
        """The urlguard check, off the event loop. False means do not fetch.

        The frontier may hold rows inserted before this engine version, so
        safety is re-established per read rather than trusted from insert time.
        """
        try:
            await asyncio.to_thread(
                assert_fetchable,
                url,
                allow_private=self._settings.URL_FETCH_ALLOW_PRIVATE,
                allowed_ports=self._settings.url_fetch_ports,
            )
        except ValidationError:
            return False
        return True

    async def discover(
        self,
        seed: str,
        *,
        origin: str,
        include_subdomains: bool,
        max_depth: int,
        max_pages: int,
        delay_ms: int,
        robots: RobotsTxt,
        deadline: float,
    ) -> list[tuple[str, int]]:
        """Enumerate a site's URL space with a bounded breadth-first deep crawl.

        Used when the sitemap said nothing — a site with no sitemap keeps its
        link graph only inside its pages, and reading those pages is the only
        way to learn it. Every URL the strategy queues has passed the scope
        filter, and `should_cancel` turns the wave's discovery deadline into a
        hard stop even mid-walk.
        """
        from crawl4ai import CacheMode, CrawlerRunConfig
        from crawl4ai.deep_crawling import BFSDeepCrawlStrategy
        from crawl4ai.deep_crawling.filters import FilterChain

        crawler = await self._start()
        scope = SiteScopeFilter(
            self._settings,
            origin=origin,
            include_subdomains=include_subdomains,
            robots=robots,
        )
        config = CrawlerRunConfig(
            stream=True,
            cache_mode=CacheMode.BYPASS,
            verbose=False,
            mean_delay=max(0.05, delay_ms / 1000),
            semaphore_count=self._browser_concurrency(),
            page_timeout=int(self._settings.CRAWL_FETCH_TIMEOUT_SECONDS * 1000),
            deep_crawl_strategy=BFSDeepCrawlStrategy(
                max_depth=max(0, max_depth),
                max_pages=max(1, max_pages),
                filter_chain=FilterChain([scope]),
                should_cancel=lambda: time.monotonic() >= deadline,
            ),
        )

        found: list[tuple[str, int]] = []
        seen: set[str] = set()
        try:
            async for result in await crawler.arun(seed, config=config):
                if not result.success:
                    continue
                url = str(result.redirected_url or result.url)
                here = canonicalise(url, origin=origin or None)
                if here is None or here in seen:
                    continue
                seen.add(here)
                depth = int((result.metadata or {}).get("depth") or 0)
                found.append((here, depth))
        except LibraryUnavailableError:
            raise
        except Exception as exc:
            # a walk that dies mid-site keeps what it found; the frontier rows
            # already inserted are the durable part and the next wave re-walks
            logger.warning("site_discovery_interrupted", seed=seed, error=str(exc))
        logger.info("site_discovery_done", seed=seed, urls=len(found))
        return found

    async def read(
        self, urls: Sequence[str], *, delay_ms: int
    ) -> AsyncIterator[LibraryPage]:
        """Read a slice of pages: rendered, markdown extracted, links enumerated.

        One `arun_many` call per slice so the library can pipeline the pages
        behind its own semaphore; the caller maps results back onto frontier
        rows by canonical URL.
        """
        from crawl4ai import CacheMode, CrawlerRunConfig

        crawler = await self._start()
        config = CrawlerRunConfig(
            stream=True,
            cache_mode=CacheMode.BYPASS,
            verbose=False,
            mean_delay=max(0.05, delay_ms / 1000),
            semaphore_count=self._browser_concurrency(),
            page_timeout=int(self._settings.CRAWL_FETCH_TIMEOUT_SECONDS * 1000),
            # robots is enforced by the caller (and the discovery filter), from
            # the same RobotsTxt the whole engine shares
            check_robots_txt=False,
        )
        try:
            async for result in await crawler.arun_many(list(urls), config=config):
                yield LibraryPage(
                    requested_url=str(result.url),
                    final_url=str(result.redirected_url or result.url),
                    status=int(result.status_code or 0),
                    markdown=str(result.markdown.raw_markdown) if result.markdown else "",
                    title=(result.metadata or {}).get("title"),
                    internal_links=_hrefs(result.links or {}),
                    head=await asyncio.to_thread(
                        _head_of, result.html or "", str(result.url)
                    ),
                    error=None if result.success else str(result.error_message or ""),
                )
        except Exception as exc:
            if isinstance(exc, LibraryUnavailableError):
                raise
            # a batch-level failure is reported as one failed page per URL: the
            # caller records them like any other fetch error and retries later
            logger.warning("site_read_batch_failed", count=len(urls), error=str(exc))
            for url in urls:
                yield LibraryPage(
                    requested_url=str(url),
                    final_url=str(url),
                    status=0,
                    error=str(exc)[:500],
                )

    def _browser_concurrency(self) -> int:
        return max(1, self._settings.CRAWL_BROWSER_CONCURRENCY)


def _hrefs(links: dict[str, Any]) -> list[str]:
    """`<a href>` targets the library found on a page, as bare strings."""
    out: list[str] = []
    for entry in links.get("internal") or []:
        href = entry.get("href") if isinstance(entry, dict) else None
        if href:
            out.append(str(href))
    return out


def build_site_crawler(settings: Settings) -> SiteCrawler:
    """Composition-root entry point, matching the other adapter builders."""
    return SiteCrawler(settings)
