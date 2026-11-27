"""The crawl package's only door to the network.

Everything the crawler fetches — seed URLs, `robots.txt`, sitemaps, discovered
links, PDFs, conditional revalidations — goes through :meth:`CrawlFetcher._open`.
There is deliberately one function that owns "open a socket to a URL a tenant or
a tenant's website supplied", so the SSRF discipline is reviewable by reading it
rather than by auditing every call site:

* :func:`assert_fetchable` runs before **every** request, including every
  redirect hop, because a URL that resolved to a public address a moment ago can
  redirect to `169.254.169.254`.
* `follow_redirects=False` on the client. httpx's automatic following would
  perform hops we never validated, which is precisely the hole. The chain is
  walked here instead, re-validating each `Location`.
* :func:`assert_peer_public` runs on the open response before a byte of body is
  read, which closes the DNS-rebinding gap between our resolution and the
  client's.
* The byte cap is enforced *during* the read via `read_capped`, so a response
  that claims 5 KB and streams 2 GB aborts at the cap.

There is no `allow_private` parameter on this class's public surface. The only
private-address escape hatch is `URL_FETCH_ALLOW_PRIVATE`, which
`Settings.validate_runtime()` refuses in production, and it is threaded from
settings rather than accepted per call so no caller can opt out.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from app.core.config import Settings
from app.core.constants import CRAWL_HTML_MIMES, CRAWL_PDF_MIME
from app.core.errors import AppError, PayloadTooLargeError, ValidationError
from app.core.logging import get_logger
from app.core.urlguard import assert_fetchable, assert_peer_public
from app.domain.ports import CachePort, FetchResult
from app.domain.robots import RobotsTxt
from app.services.ingestion.gate import read_capped

logger = get_logger(__name__)

# statuses that carry a Location we may follow. 303 is included because a site
# that answers a GET with 303 still expects the GET to be repeated
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

# read this much at a time out of the response stream
FETCH_CHUNK_BYTES = 64 * 1024

# robots.txt is small; a site serving 10 MB of it is not serving robots.txt
ROBOTS_MAX_BYTES = 512 * 1024
# a sitemap index can legitimately be large — the standard's own cap is 50 MB
SITEMAP_MAX_BYTES = 20 * 1024 * 1024

# how long a fetched robots.txt is trusted. long enough that a 400-page crawl
# fetches it once, short enough that a fix on the site is picked up the same day
ROBOTS_CACHE_TTL_SECONDS = 6 * 3600

# Retry-After larger than this is a "come back tomorrow" that a wave cannot wait
# for, so the link goes back to pending and the next wave tries again
MAX_RETRY_AFTER_SECONDS = 300


class CrawlFetchError(AppError):
    """A fetch that produced no usable response.

    Carries the exclusion/failure `reason` the frontier records, because "why"
    is what the dashboard shows a tenant and the reason vocabulary is fixed by
    §3.5. A non-2xx HTTP status is *not* this — that arrives as a
    :class:`FetchResult` with a status, so a 404 is recorded rather than raised.
    """

    def __init__(self, reason: str, message: str | None = None) -> None:
        super().__init__(message or reason)
        self.reason = reason


@dataclass(slots=True)
class ThrottleDecision:
    """Whether this host may be fetched now, and how long until it may be."""

    allowed: bool
    wait_ms: int = 0


def content_type_of(headers: dict[str, str]) -> str:
    """Bare MIME out of a `Content-Type` header, lower-cased."""
    raw = headers.get("content-type", "")
    return raw.split(";", 1)[0].strip().lower()


def allowed_mimes_for(*, include_pdf: bool) -> frozenset[str]:
    """The content-type allowlist for one source.

    An allowlist rather than a denylist: a crawler that fetches whatever a site
    serves ends up feeding zip files and video to the parser, and the reason a
    page was skipped stops being explainable.
    """
    if include_pdf:
        return CRAWL_HTML_MIMES | {CRAWL_PDF_MIME}
    return CRAWL_HTML_MIMES


def retry_after_seconds(headers: dict[str, str]) -> int | None:
    """`Retry-After`, as seconds, whether it was sent as seconds or as a date.

    Honoured rather than ignored because ignoring it is how a crawler earns a
    permanent block, and the link simply returns to `pending` for a later wave.
    """
    raw = headers.get("retry-after", "").strip()
    if not raw:
        return None
    if raw.isdigit():
        return min(int(raw), MAX_RETRY_AFTER_SECONDS)
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    now = datetime.now(UTC)
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    delta = int((when - now).total_seconds())
    if delta <= 0:
        return None
    return min(delta, MAX_RETRY_AFTER_SECONDS)


class CrawlFetcher:
    """`PageFetcher` for the crawler. The only place in this package that opens a
    socket to a tenant-supplied or site-supplied URL.

    Holds one `httpx.AsyncClient` for connection reuse across a wave — a crawl of
    200 pages on one host is the case connection pooling exists for — and is
    closed by the container like every other closeable adapter.
    """

    def __init__(self, settings: Settings, cache: CachePort | None = None) -> None:
        self._settings = settings
        self._cache = cache
        self._allow_private = settings.URL_FETCH_ALLOW_PRIVATE
        self._ports = settings.url_fetch_ports
        self._max_redirects = settings.URL_FETCH_MAX_REDIRECTS
        self._max_bytes = settings.CRAWL_MAX_PAGE_BYTES
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.CRAWL_FETCH_TIMEOUT_SECONDS, connect=10.0),
            # NEVER True. automatic following would perform hops that no
            # assert_fetchable ever saw, which is the whole vulnerability
            follow_redirects=False,
            headers={
                "User-Agent": settings.crawl_user_agent,
                "Accept-Language": "en;q=0.9,*;q=0.5",
                # identify as a crawler to a WAF that reads it, and ask for the
                # cheapest encoding both ends already support
                "Accept-Encoding": "gzip, deflate",
            },
        )
        # per-host pacing state for sub-second politeness delays (see `throttle`).
        # the fetcher is one per process, so these also pace two sources in this
        # process that share a host
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._host_ready: dict[str, float] = {}
        self._lease_until: dict[str, float] = {}

    @property
    def name(self) -> str:
        return "crawl_fetcher"

    async def fetch(
        self,
        url: str,
        *,
        if_none_match: str | None = None,
        if_modified_since: str | None = None,
        max_bytes: int | None = None,
        accept: str = "text/html,application/xhtml+xml,text/plain;q=0.8,*/*;q=0.1",
        allowed_mimes: frozenset[str] | None = None,
    ) -> FetchResult:
        """Fetch one crawl target.

        Conditional headers are passed through so an unchanged page costs one
        round trip and zero embeddings: a 304 comes back as a `FetchResult` with
        status 304 and an empty body, which the engine records as `unchanged`.

        `allowed_mimes` is checked on the response headers *before* the body is
        read, so a video the site linked from an `<a>` never enters memory.
        """
        headers: dict[str, str] = {"Accept": accept}
        if if_none_match:
            headers["If-None-Match"] = if_none_match
        if if_modified_since:
            headers["If-Modified-Since"] = if_modified_since
        return await self._open(
            url,
            headers=headers,
            max_bytes=max_bytes or self._max_bytes,
            allowed_mimes=allowed_mimes,
        )

    async def fetch_text(
        self,
        url: str,
        *,
        max_bytes: int,
        accept: str = "text/plain,text/xml,application/xml;q=0.9,*/*;q=0.5",
    ) -> tuple[int, str]:
        """Status and decoded text for a small non-HTML resource.

        Used for `robots.txt` and sitemaps, which are text protocols where a
        decode failure is not worth a distinct outcome — `errors="replace"` on a
        sitemap loses one URL, while raising loses the whole discovery phase.
        """
        result = await self._open(url, headers={"Accept": accept}, max_bytes=max_bytes)
        return result.status, result.body.decode("utf-8", errors="replace")

    async def robots(self, origin: str) -> RobotsTxt:
        """`robots.txt` for one origin, cached, permissive on anything but a 2xx.

        A missing, 4xx or 5xx `robots.txt` means "no restrictions" per RFC 9309 —
        and so does a fetch that fails outright, because refusing to crawl a site
        whose robots.txt times out is a worse answer for the tenant than crawling
        it. Same-origin and urlguard controls do not depend on this file.
        """
        if not self._settings.CRAWL_RESPECT_ROBOTS:
            return RobotsTxt.permissive()

        base = origin.rstrip("/")
        cache_key = f"crawl:robots:{base}"
        cached = await self._cache_get(cache_key)
        if cached is not None:
            return RobotsTxt.parse(cached, self._settings.crawl_user_agent)

        try:
            status, body = await self.fetch_text(f"{base}/robots.txt", max_bytes=ROBOTS_MAX_BYTES)
        except (CrawlFetchError, ValidationError) as exc:
            logger.info("robots_unavailable", origin=base, error=str(exc))
            return RobotsTxt.permissive()

        if status != 200 or not body.strip():
            await self._cache_set(cache_key, "", ROBOTS_CACHE_TTL_SECONDS)
            return RobotsTxt.permissive()

        await self._cache_set(cache_key, body[:200_000], ROBOTS_CACHE_TTL_SECONDS)
        return RobotsTxt.parse(body, self._settings.crawl_user_agent)

    async def throttle(self, host: str, delay_ms: int) -> ThrottleDecision:
        """Per-host politeness, held across processes.

        A lease on the host rather than a sleep in the wave, because two sources
        pointing at the same host would otherwise double the request rate, and a
        serverless wave has no memory of the previous one. Delays of a second or
        more are expressed by the lease's TTL alone, and concurrent fetchers
        that lose the race defer. Sub-second delays cannot be expressed by the
        lease at all — cache TTLs have one-second granularity — so they are
        paced in-process instead: fetch starts on one host queue behind a
        per-host lock and are spaced `delay_ms` apart, so wave concurrency
        pipelines a single-host crawl rather than deferring every fetch behind
        the one-second lease.
        """
        delay = max(0, delay_ms)
        if delay == 0:
            return ThrottleDecision(allowed=True)

        if delay < 1000:
            async with self._host_lock(host):
                now = time.monotonic()
                ready_at = self._host_ready.get(host, 0.0)
                if ready_at > now:
                    await asyncio.sleep(ready_at - now)
                self._host_ready[host] = time.monotonic() + delay / 1000
            if self._cache is None:
                return ThrottleDecision(allowed=True)
            # this process holds the host's lease: only another process's
            # fetches may be deferred behind it, never our own paced ones
            if time.monotonic() < self._lease_until.get(host, 0.0):
                return ThrottleDecision(allowed=True)
            ttl = 1
        else:
            if self._cache is None:
                await asyncio.sleep(delay / 1000)
                return ThrottleDecision(allowed=True)
            ttl = delay // 1000

        token = uuid.uuid4().hex
        try:
            acquired = await self._cache.acquire_lease(f"crawl:host:{host}", token, ttl)
        except Exception:  # pragma: no cover - a cache outage must not stop a crawl
            logger.warning("crawl_throttle_lease_failed", host=host)
            if delay >= 1000:
                # no lease was granted and no pacing happened above, so the
                # delay has to be slept here
                await asyncio.sleep(delay / 1000)
            return ThrottleDecision(allowed=True)
        if not acquired:
            return ThrottleDecision(allowed=False, wait_ms=max(delay, 1000))
        self._lease_until[host] = time.monotonic() + ttl
        return ThrottleDecision(allowed=True)

    def _host_lock(self, host: str) -> asyncio.Lock:
        """The host's pacing lock, created on first use."""
        lock = self._host_locks.get(host)
        if lock is None:
            lock = self._host_locks[host] = asyncio.Lock()
        return lock

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ internals

    async def _open(
        self,
        url: str,
        *,
        headers: dict[str, str],
        max_bytes: int,
        allowed_mimes: frozenset[str] | None = None,
    ) -> FetchResult:
        """THE choke point. Every socket this package opens is opened here.

        Do not add a bypass parameter, do not construct a second client
        elsewhere, and do not accept `allow_private` from a caller. The redirect
        chain is walked by hand precisely so that each hop is validated.
        """
        target = url
        chain: list[str] = []

        for _hop in range(self._max_redirects + 1):
            # (1) pre-connect check, on the seed URL and on EVERY redirect hop.
            # the loop is what makes the second and later checks happen
            try:
                assert_fetchable(
                    target, allow_private=self._allow_private, allowed_ports=self._ports
                )
            except ValidationError as exc:
                # never echo the resolved address: this response text reaches a
                # tenant, and echoing it turns the crawler into a network scanner
                raise CrawlFetchError("blocked_target", str(exc)) from exc

            request = self._client.build_request("GET", target, headers=headers)
            try:
                response = await self._client.send(request, stream=True)
            except httpx.TimeoutException as exc:
                raise CrawlFetchError("fetch_timeout", "request timed out") from exc
            except httpx.InvalidURL as exc:
                # `build_request`/`send` raise this for a URL the guard's
                # urlparse accepted — a bare IPv6 host without brackets, a
                # non-printable character. not an HTTPError, so it is listed
                # explicitly or it escapes the error contract
                raise CrawlFetchError("fetch_error", "unusable target url") from exc
            except httpx.HTTPError as exc:
                raise CrawlFetchError("fetch_error", str(exc)) from exc

            try:
                # (2) post-connect check, before a single byte of body is read.
                # closes the DNS-rebinding gap between our resolution and the
                # client's
                try:
                    assert_peer_public(response, allow_private=self._allow_private)
                except ValidationError as exc:
                    raise CrawlFetchError("blocked_target", str(exc)) from exc

                if response.status_code in REDIRECT_STATUSES:
                    location = response.headers.get("location", "").strip()
                    if not location:
                        raise CrawlFetchError("fetch_error", "redirect without a location")
                    # resolve relative Locations against the URL we actually
                    # reached, then loop — which re-runs assert_fetchable above.
                    # a malformed Location (broken IPv6 literal, out-of-range
                    # port, stray control character) makes httpx raise
                    # InvalidURL, which is not an HTTPError and would otherwise
                    # escape this package's error contract
                    try:
                        next_target = str(response.url.join(location))
                    except httpx.InvalidURL as exc:
                        raise CrawlFetchError("fetch_error", "unusable redirect target") from exc
                    chain.append(target)
                    target = next_target
                    continue

                out_headers = {key.lower(): value for key, value in response.headers.items()}

                if allowed_mimes is not None and 200 <= response.status_code < 300:
                    mime = content_type_of(out_headers)
                    # a server that sends no content-type is given the benefit of
                    # the doubt and sniffed after the read; one that sends a type
                    # we do not extract from is refused before the read
                    if mime and mime not in allowed_mimes:
                        raise CrawlFetchError(
                            "non_html", f"content-type {mime} not extractable"
                        )

                body = b""
                if response.status_code != 304:
                    try:
                        body = await read_capped(
                            response.aiter_bytes(FETCH_CHUNK_BYTES), max_bytes, FETCH_CHUNK_BYTES
                        )
                    except PayloadTooLargeError as exc:
                        raise CrawlFetchError("too_large", str(exc)) from exc
                    except httpx.TimeoutException as exc:
                        raise CrawlFetchError("fetch_timeout", "read timed out") from exc
                    except httpx.HTTPError as exc:
                        raise CrawlFetchError("fetch_error", str(exc)) from exc

                return FetchResult(
                    final_url=str(response.url),
                    status=response.status_code,
                    headers=out_headers,
                    body=body,
                    redirect_chain=chain,
                )
            finally:
                await response.aclose()

        raise CrawlFetchError("too_many_redirects", "too many redirects")

    async def _cache_get(self, key: str) -> str | None:
        if self._cache is None:
            return None
        try:
            return await self._cache.get(key)
        except Exception:  # pragma: no cover - a cache outage must not stop a crawl
            logger.warning("crawl_cache_get_failed", key=key)
            return None

    async def _cache_set(self, key: str, value: str, ttl: int) -> None:
        if self._cache is None:
            return
        try:
            await self._cache.set(key, value, ttl)
        except Exception:  # pragma: no cover - same
            logger.warning("crawl_cache_set_failed", key=key)


def host_of(url: str) -> str:
    """Lower-cased host of a URL, for the politeness key."""
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        # a malformed URL (unbracketed IPv6, bad port) must not kill the wave
        return ""


def origin_of(url: str) -> str:
    """`scheme://host[:port]` of a URL."""
    parts = urlsplit(url)
    host = parts.netloc
    return f"{parts.scheme}://{host}" if parts.scheme and host else ""


def crawl_raw_path(tenant_id: UUID, source_id: UUID, link_id: UUID) -> str:
    """Where a fetched page's raw bytes live between the fetch and extract phases.

    Tenant-prefixed like `document_path`, so a bucket policy can enforce isolation
    at the storage layer too. Separate from `documents/` because these blobs are
    scratch: the extract phase deletes each one as soon as the page has a document,
    and nothing may build a citation link to them.
    """
    return f"tenants/{tenant_id}/crawl/{source_id}/{link_id}"


def build_page_fetcher(settings: Settings, cache: Any | None = None) -> CrawlFetcher:
    """Composition-root entry point, matching the other adapter builders."""
    return CrawlFetcher(settings, cache)
