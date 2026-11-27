"""Turning fetched bytes into a page worth indexing — or into a recorded reason.

Three jobs, in the order a wave needs them:

1. :func:`extract_page` — bytes to markdown plus everything derived from it that
   the frontier stores (`content_hash`, `simhash`, outbound links).
2. :func:`freeze_boilerplate` — the per-source chrome fingerprint, computed once
   from a sample of the first pages and then reused for every later page and every
   later re-crawl, so the same nav is removed from page 1 and page 400.
3. :func:`is_duplicate` — exact hash first, near-duplicate second.

Nothing here decides a page's fate on its own; each function reports and the
engine records. "Too short" is `empty_after_extraction`, "too short with a big
HTML body and a root mount div" is `javascript_rendered`, and only the engine
knows whether a renderer exists to try instead.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from app.adapters.parsers.web import (
    CRAWL_BOILERPLATE_MAX_BLOCK_CHARS,
    UndecodableError,
    WebPageParser,
    block_fingerprint,
    block_fingerprints,
    decode_html,
    split_blocks,
    url_heading_path,
)
from app.adapters.postgres.source_repo import SourceRepository
from app.core.constants import CRAWL_MIN_TEXT_CHARS
from app.core.logging import get_logger
from app.domain.crawl import (
    REASON_EMPTY_AFTER_EXTRACTION,
    REASON_JAVASCRIPT_RENDERED,
    REASON_NOINDEX,
    REASON_NON_CANONICAL,
    canonicalise,
    content_hash,
    extract_links,
    find_near_duplicate,
    from_signed_64,
    simhash,
)

logger = get_logger(__name__)

# how many pages the boilerplate fingerprint is learned from. small enough that a
# 6-page site still freezes on its first wave, large enough that one unusual page
# cannot make its own paragraphs look like site chrome
CRAWL_BOILERPLATE_SAMPLE = 12

# a block has to appear on this share of the sample to count as chrome. 0.6 keeps
# a footer that is missing from the contact page while sparing a paragraph that
# two pages happen to share
CRAWL_BOILERPLATE_MIN_FREQUENCY = 0.6

# below this many sampled pages, no fingerprint is frozen at all: with two pages
# every shared block looks like boilerplate, and the "About" heading would go
CRAWL_BOILERPLATE_MIN_PAGES = 3


@dataclass(slots=True)
class PageContent:
    """One extracted page and everything the frontier stores about it."""

    markdown: str
    title: str | None = None
    # sha256 of the markdown, never of the raw HTML — see domain.crawl.content_hash
    content_hash: str = ""
    # unsigned 64-bit fingerprint. the repository stores it as a signed BIGINT
    simhash: int = 0
    links: list[str] = field(default_factory=list)
    heading_path: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    canonical_url: str | None = None
    lang: str | None = None
    noindex: bool = False
    spa_suspected: bool = False
    blocks_total: int = 0
    blocks_dropped: int = 0
    text_ratio: float = 0.0
    container: str = "body"
    # the floor `thin` compares against. carried on the page rather than read from
    # the constant inside the property, so an operator's CRAWL_MIN_TEXT_CHARS
    # reaches the decision; the default is the constant that Settings default
    # mirrors, so a PageContent built without it behaves as before
    min_text_chars: int = CRAWL_MIN_TEXT_CHARS

    @property
    def char_count(self) -> int:
        return len(self.markdown)

    @property
    def thin(self) -> bool:
        """Below the floor where a chunk carries retrievable meaning."""
        return len(self.markdown.strip()) < self.min_text_chars


def extract_page(
    data: bytes,
    url: str,
    *,
    content_type: str | None = None,
    boilerplate: frozenset[str] = frozenset(),
    site_name: str | None = None,
    origin: str | None = None,
    collect_links: bool = True,
    min_text_chars: int = CRAWL_MIN_TEXT_CHARS,
) -> PageContent:
    """Extract one HTML page. Raises :class:`UndecodableError` if it is not text.

    `collect_links` is off for a re-crawl of a page at max depth, where the links
    would be classified and immediately refused — the work of parsing them is
    wasted and a large page's `<a>` list is not small.

    `min_text_chars` is stored on the result rather than applied here: the thinness
    verdict is read later by `rejection`, and putting the floor on the page keeps
    that one call site free of a second argument to thread.
    """
    parser = WebPageParser(boilerplate=boilerplate)
    extraction = parser.parse(
        data, url, content_type=content_type, site_name=site_name, origin=origin
    )

    links: list[str] = []
    if collect_links:
        try:
            html = decode_html(data, content_type)
        except UndecodableError:  # pragma: no cover - parse() would have raised first
            html = ""
        if html:
            links = extract_links(html, url, origin)

    markdown = extraction.markdown
    return PageContent(
        markdown=markdown,
        title=extraction.title,
        content_hash=content_hash(markdown) if markdown.strip() else "",
        simhash=simhash(markdown) if markdown.strip() else 0,
        links=links,
        heading_path=extraction.heading_path,
        metadata=extraction.metadata,
        canonical_url=extraction.canonical_url,
        lang=extraction.lang,
        noindex=extraction.noindex,
        spa_suspected=extraction.spa_suspected,
        blocks_total=extraction.blocks_total,
        blocks_dropped=extraction.blocks_dropped,
        text_ratio=extraction.text_ratio,
        container=extraction.container,
        min_text_chars=min_text_chars,
    )


def strip_boilerplate(markdown: str, fingerprints: frozenset[str]) -> tuple[str, int]:
    """Drop the blocks whose fingerprints this source froze as chrome.

    The markdown twin of :meth:`WebPageParser._drop_boilerplate`: the library
    produces the markdown now, so the frozen fingerprints are applied to blocks
    of that markdown rather than to DOM nodes. Same rules — short blocks only,
    fingerprinted through the same normalisation — so a fingerprint frozen from
    one representation matches the other.
    """
    if not fingerprints or not markdown:
        return markdown, 0
    kept: list[str] = []
    dropped = 0
    for block in split_blocks(markdown):
        if len(block) < CRAWL_BOILERPLATE_MAX_BLOCK_CHARS and block_fingerprint(
            block
        ) in fingerprints:
            dropped += 1
            continue
        kept.append(block)
    if not dropped:
        return markdown, 0
    return "\n\n".join(kept), dropped


def page_from_markdown(
    markdown: str,
    url: str,
    *,
    title: str | None = None,
    boilerplate: frozenset[str] = frozenset(),
    lang: str | None = None,
    noindex: bool = False,
    canonical_url: str | None = None,
    min_text_chars: int = CRAWL_MIN_TEXT_CHARS,
) -> PageContent:
    """A :class:`PageContent` for a page the crawler library already rendered.

    The library owns bytes-to-markdown; this rebuilds everything downstream that
    the frontier and the review list depend on — the content hash, the simhash,
    boilerplate removal, the heading path — from that markdown, so duplicate
    detection, incremental retrain and the corpus itself behave identically for
    a library-read page and a legacy one.
    """
    body, dropped = strip_boilerplate(markdown, boilerplate)
    text = body.strip()
    return PageContent(
        markdown=body,
        title=(title or None),
        content_hash=content_hash(text) if text else "",
        simhash=simhash(text) if text else 0,
        heading_path=url_heading_path(url),
        lang=lang,
        noindex=noindex,
        canonical_url=canonical_url,
        blocks_total=len(split_blocks(markdown)),
        blocks_dropped=dropped,
        min_text_chars=min_text_chars,
    )


def rejection(
    page: PageContent, url: str, *, origin: str | None = None
) -> tuple[str, str | None] | None:
    """`(reason, detail)` if this page must not be indexed, else None.

    Ordered by how much the tenant can do about it. `noindex` is the site's own
    instruction and is obeyed first. A page that names a different canonical is
    skipped in favour of the page it points at, which discovery will have queued.
    An SPA is reported as `javascript_rendered` rather than as empty, because the
    engine may be able to retry it through a renderer, and because the fix a
    tenant is shown differs ("enable JS rendering" vs "this page has no text").
    """
    if page.noindex:
        return REASON_NOINDEX, None

    if page.canonical_url:
        here = canonicalise(url, origin=origin)
        there = canonicalise(page.canonical_url, base=url, origin=origin)
        if there is not None and here is not None and there != here:
            return REASON_NON_CANONICAL, page.canonical_url

    if page.spa_suspected:
        return REASON_JAVASCRIPT_RENDERED, None

    if page.thin:
        return REASON_EMPTY_AFTER_EXTRACTION, str(len(page.markdown.strip()))

    return None


def freeze_boilerplate(markdowns: Sequence[str]) -> frozenset[str]:
    """The set of block fingerprints that repeat across a source's pages.

    Frozen once per source and stored on the row, not recomputed per wave: a
    fingerprint that drifted between waves would remove different blocks from page
    1 and page 400 of the same site, and the corpus would stop being coherent.

    Fewer than :data:`CRAWL_BOILERPLATE_MIN_PAGES` samples yields an empty set —
    with two pages the frequency test cannot tell chrome from content, and
    guessing wrong deletes real text.
    """
    pages = [markdown for markdown in markdowns if markdown.strip()]
    if len(pages) < CRAWL_BOILERPLATE_MIN_PAGES:
        return frozenset()

    counts: dict[str, int] = {}
    for markdown in pages[:CRAWL_BOILERPLATE_SAMPLE]:
        # a block repeated twice inside one page is still one page's worth of
        # evidence, so fingerprints are de-duplicated per page before counting
        for digest in set(block_fingerprints(markdown)):
            counts[digest] = counts.get(digest, 0) + 1

    sample = min(len(pages), CRAWL_BOILERPLATE_SAMPLE)
    threshold = sample * CRAWL_BOILERPLATE_MIN_FREQUENCY
    return frozenset(digest for digest, count in counts.items() if count >= threshold)


async def is_duplicate(
    repo: SourceRepository,
    tenant_id: UUID,
    source_id: UUID,
    *,
    page: PageContent,
    exclude_id: UUID | None = None,
    max_distance: int = 3,
    simhash_index: list[tuple[str, int]] | None = None,
) -> str | None:
    """The URL of the page this one duplicates, or None.

    Exact hash first: it is an index lookup, and print views, `?page=1` aliases
    and `/about` vs `/about-us` extract byte-identically far more often than they
    extract nearly-identically. The simhash scan only runs when the cheap check
    misses.

    Scoped to one source deliberately — see `SourceRepository.simhash_candidates`.

    `simhash_index` is this wave's already-loaded `(url, signed simhash)` pairs,
    kept and appended to by the extract phase as pages train. Without it every
    page pays a full-candidates SELECT, and a source with N pages does N of
    those — O(N²) rows moved for no decision the caller did not already have.
    The exact-hash lookup stays per page against the database: it is an indexed
    single-row read, and it stays correct when another wave trained a page this
    index has not heard about.
    """
    if not page.content_hash:
        return None

    match = await repo.find_content_match(
        tenant_id, source_id, page.content_hash, exclude_id=exclude_id
    )
    if match is not None:
        return str(match.get("url") or "")

    if not page.simhash:
        return None

    if simhash_index is None:
        candidates = await repo.simhash_candidates(tenant_id, source_id, exclude_id=exclude_id)
        if not candidates:
            return None
        unsigned = [(url, from_signed_64(value)) for url, value in candidates]
    else:
        # the index only ever holds trained/unchanged rows, so the page being
        # judged (state 'fetched') cannot be in it — no self-exclusion needed
        unsigned = [(url, from_signed_64(value)) for url, value in simhash_index]
    return find_near_duplicate(page.simhash, unsigned, max_distance)
