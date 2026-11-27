"""Pure crawl domain logic: canonicalisation, scope rules, fingerprints, parsing.

No I/O lives here — no httpx, no database, no container, no Settings. Everything
in this module is a function of its arguments, which is what lets the API layer
validate a source at create time and the wave service decide a link's fate mid
crawl using the *same* code. A rule that lived in the service would be invisible
to the API and the two would drift.

The four things this module owns:

  CrawlConfig      the validated shape of ``sources.config``, clamped to the
                   deployment's hard ceilings once at create so the tenant is
                   told "40 pages max" up front instead of the crawl dying at
                   page 40.
  canonicalise     one URL, one spelling. Phantom duplicates from fragments,
                   tracking parameters and ``index.html`` aliases are the single
                   largest source of wasted fetches in a link graph.
  classify         why a URL was skipped, as a closed vocabulary string. The
                   review UI renders these, so ``reason`` is never prose — the
                   sentence a human reads is produced by ``reason_text`` here,
                   server side, so every consumer cannot disagree
                   about the copy.
  fingerprints     sha256 of the extracted markdown for exact duplicates, and a
                   64-bit SimHash over word 3-shingles for near duplicates.

The SSRF guard is deliberately NOT called from here. ``assert_fetchable`` and
``assert_peer_public`` (``app/core/urlguard.py``) resolve DNS and inspect a live
socket, which is I/O; they belong to the fetch path in the Phase-2 service layer
(``app/services/crawl/fetch.py``), called on every hop of every request. What
this module contributes to that defence is the cheap half that needs no network:
``canonicalise`` refuses any scheme other than http/https and any URL carrying
embedded credentials before such a URL can reach the frontier at all, and
``classify`` pins every candidate to the source's resolved origin.
"""

from __future__ import annotations

import fnmatch
import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

# ElementTree is the stdlib sitemap parser on purpose: adding a dependency to
# read an XML file the crawler already rate-limits is not a trade worth making.
# Sitemaps are fetched from the crawled site, so entity expansion is the risk —
# closed by never resolving external entities (ElementTree does not) and by the
# byte cap the fetcher applies before this function ever sees the text.
from xml.etree import ElementTree

from bs4 import BeautifulSoup

from app.core.constants import (
    CRAWL_DEFAULT_DELAY_MS,
    CRAWL_DEFAULT_MAX_DEPTH,
    CRAWL_DEFAULT_MAX_PAGES,
    CRAWL_FACET_PARAMS,
    CRAWL_HTML_MIMES,
    CRAWL_INDEX_FILENAMES,
    CRAWL_MAX_DELAY_MS,
    CRAWL_MAX_DEPTH_CEILING,
    CRAWL_MAX_PAGES_CEILING,
    CRAWL_MAX_PATTERN_LENGTH,
    CRAWL_MAX_PATTERNS,
    CRAWL_MAX_PDFS_PER_SOURCE,
    CRAWL_MAX_QUERY_PARAMS,
    CRAWL_MAX_TITLE_CHARS,
    CRAWL_MIN_DELAY_MS,
    CRAWL_NON_PAGE_EXTENSIONS,
    CRAWL_PDF_EXTENSIONS,
    CRAWL_PDF_MIME,
    CRAWL_SIMHASH_BITS,
    CRAWL_SIMHASH_MASK,
    CRAWL_SIMHASH_MAX_DISTANCE_DEFAULT,
    CRAWL_SIMHASH_SHINGLE_SIZE,
    CRAWL_SIMHASH_SIGN_BOUND,
    CRAWL_TRACKING_PARAMS,
    CRAWL_UNRESERVED_CHARS,
)
from app.core.errors import ValidationError

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_DEFAULT_PORTS = {"http": 80, "https": 443}
_UNRESERVED = frozenset(CRAWL_UNRESERVED_CHARS)

# ------------------------------------------------------------------ reasons

# The closed vocabulary of `source_links.reason`. Every value the crawler may
# write is here; anything else is a bug, not a new reason, because the review UI
# has exactly one sentence per member and a stranger renders as a blank excuse.
REASON_OUT_OF_SCOPE = "out_of_scope"
REASON_EXCLUDED_PATH = "excluded_path"
REASON_NOT_INCLUDED = "not_included"
REASON_ROBOTS_DISALLOWED = "robots_disallowed"
REASON_DEPTH_EXCEEDED = "depth_exceeded"
REASON_LOGIN_SIGNUP = "login_signup"
REASON_COMMERCE_FLOW = "commerce_flow"
REASON_SEARCH_RESULTS = "search_results"
REASON_ARCHIVE_OR_TAXONOMY = "archive_or_taxonomy"
REASON_PAGINATION = "pagination"
REASON_CALENDAR = "calendar"
REASON_ALT_FORMAT = "alt_format"
REASON_FACETED_NAVIGATION = "faceted_navigation"
REASON_NON_HTML = "non_html"
REASON_PDF_NOT_ENABLED = "pdf_not_enabled"
REASON_PDF_BUDGET_EXCEEDED = "pdf_budget_exceeded"
REASON_NOINDEX = "noindex"
REASON_NON_CANONICAL = "non_canonical"
REASON_DUPLICATE_CONTENT = "duplicate_content"
REASON_EMPTY_AFTER_EXTRACTION = "empty_after_extraction"
REASON_JAVASCRIPT_RENDERED = "javascript_rendered"
REASON_TOO_LARGE = "too_large"
REASON_BLOCKED_TARGET = "blocked_target"
REASON_REDIRECT_OFF_ORIGIN = "redirect_off_origin"
REASON_TOO_MANY_REDIRECTS = "too_many_redirects"
REASON_PAGE_BUDGET_EXCEEDED = "page_budget_exceeded"
REASON_DOCUMENT_QUOTA_EXHAUSTED = "document_quota_exhausted"
REASON_TIME_BUDGET_EXCEEDED = "time_budget_exceeded"
# the only reason a human puts there, not a rule: a page dropped at the review
# gate before indexing. kept distinct from the automatic reasons so the review
# list can say "you removed this" instead of inventing a rule that never ran
REASON_REVIEW_EXCLUDED = "review_excluded"

EXCLUSION_REASONS = frozenset(
    {
        REASON_OUT_OF_SCOPE,
        REASON_EXCLUDED_PATH,
        REASON_NOT_INCLUDED,
        REASON_ROBOTS_DISALLOWED,
        REASON_DEPTH_EXCEEDED,
        REASON_LOGIN_SIGNUP,
        REASON_COMMERCE_FLOW,
        REASON_SEARCH_RESULTS,
        REASON_ARCHIVE_OR_TAXONOMY,
        REASON_PAGINATION,
        REASON_CALENDAR,
        REASON_ALT_FORMAT,
        REASON_FACETED_NAVIGATION,
        REASON_NON_HTML,
        REASON_PDF_NOT_ENABLED,
        REASON_PDF_BUDGET_EXCEEDED,
        REASON_NOINDEX,
        REASON_NON_CANONICAL,
        REASON_DUPLICATE_CONTENT,
        REASON_EMPTY_AFTER_EXTRACTION,
        REASON_JAVASCRIPT_RENDERED,
        REASON_TOO_LARGE,
        REASON_BLOCKED_TARGET,
        REASON_REDIRECT_OFF_ORIGIN,
        REASON_TOO_MANY_REDIRECTS,
        REASON_PAGE_BUDGET_EXCEEDED,
        REASON_DOCUMENT_QUOTA_EXHAUSTED,
        REASON_TIME_BUDGET_EXCEEDED,
        REASON_REVIEW_EXCLUDED,
    }
)

# One sentence per reason, written for a non-technical buyer looking at the
# Excluded group and asking "why is my pricing page not in here". `{detail}` is
# filled from source_links.reason_detail; the fallback table below covers the
# case where the detail was not recorded, so a sentence is never half-written.
_REASON_SENTENCES: dict[str, str] = {
    REASON_OUT_OF_SCOPE: "On a different website.",
    REASON_EXCLUDED_PATH: "Matched your exclude path `{detail}`.",
    REASON_NOT_INCLUDED: "Outside your include paths.",
    REASON_ROBOTS_DISALLOWED: (
        "The site's robots.txt asks crawlers not to read this."
    ),
    REASON_DEPTH_EXCEEDED: "More than {detail} clicks from the start page.",
    REASON_LOGIN_SIGNUP: "A sign-in or account page — no public content.",
    REASON_COMMERCE_FLOW: "Part of the checkout flow.",
    REASON_SEARCH_RESULTS: (
        "A search results page — its content lives on other pages."
    ),
    REASON_ARCHIVE_OR_TAXONOMY: (
        "A tag or archive listing — the posts themselves are indexed."
    ),
    REASON_PAGINATION: "Page {detail} of a listing.",
    REASON_CALENDAR: "A calendar view.",
    REASON_ALT_FORMAT: "An alternate rendering of a page already indexed.",
    REASON_FACETED_NAVIGATION: (
        "A filtered listing view — thousands of these exist and they hold no "
        "new text."
    ),
    REASON_NON_HTML: "Not a web page.",
    REASON_PDF_NOT_ENABLED: (
        "PDFs are off for this source — turn them on in Advanced options."
    ),
    REASON_PDF_BUDGET_EXCEEDED: "Reached the {detail}-PDF limit for one source.",
    REASON_NOINDEX: "The page asks search engines not to index it.",
    REASON_NON_CANONICAL: "The site says the canonical version is {detail}.",
    REASON_DUPLICATE_CONTENT: "Same content as {detail}.",
    REASON_EMPTY_AFTER_EXTRACTION: (
        "No readable text after removing navigation and footers."
    ),
    REASON_JAVASCRIPT_RENDERED: (
        "Content is rendered by JavaScript, which this crawler cannot read."
    ),
    REASON_TOO_LARGE: "Larger than {detail} MB.",
    # the address is never echoed: the review list would otherwise be an
    # internal-network port scanner with a friendly UI
    REASON_BLOCKED_TARGET: "This link points somewhere we will not fetch.",
    REASON_REDIRECT_OFF_ORIGIN: (
        "Redirected to a different website, so it was not followed."
    ),
    REASON_TOO_MANY_REDIRECTS: "Redirected too many times.",
    REASON_PAGE_BUDGET_EXCEEDED: (
        "Beyond the {detail}-page limit for one website."
    ),
    REASON_DOCUMENT_QUOTA_EXHAUSTED: (
        "You have reached the document limit."
    ),
    REASON_TIME_BUDGET_EXCEEDED: (
        "The crawl ran out of time — recrawl to continue."
    ),
    REASON_REVIEW_EXCLUDED: "You removed this page before indexing.",
}

_REASON_FALLBACKS: dict[str, str] = {
    REASON_EXCLUDED_PATH: "Matched one of your exclude paths.",
    REASON_DEPTH_EXCEEDED: "Too many clicks from the start page.",
    REASON_PAGINATION: "A later page of a listing.",
    REASON_PDF_BUDGET_EXCEEDED: "Reached the PDF limit for one source.",
    REASON_NON_CANONICAL: "The site points at a different canonical version.",
    REASON_DUPLICATE_CONTENT: "Same content as another page on this site.",
    REASON_TOO_LARGE: "Larger than the per-page size limit.",
    REASON_PAGE_BUDGET_EXCEEDED: (
        "Beyond the page limit for one website."
    ),
}

_UNKNOWN_REASON_SENTENCE = "This page was skipped."


def reason_text(reason: str | None, detail: str | None = None) -> str:
    """Human sentence for a machine reason. Rendered server side, always.

    The widget, the dashboard review list and any future email digest must say
    the same thing about the same link, so the copy lives next to the rule that
    produced it rather than being reimplemented per client.
    """
    if not reason:
        return ""
    template = _REASON_SENTENCES.get(reason)
    if template is None:
        return _UNKNOWN_REASON_SENTENCE
    if "{detail}" not in template:
        return template
    if detail:
        return template.replace("{detail}", detail)
    return _REASON_FALLBACKS.get(reason, _UNKNOWN_REASON_SENTENCE)


def is_known_reason(reason: str) -> bool:
    """Guard for the write path: a reason outside the vocabulary is a bug."""
    return reason in EXCLUSION_REASONS


# ------------------------------------------------------------------ path globs


def normalise_pattern(pattern: str) -> str:
    """Tidy one tenant-supplied include/exclude pattern.

    A pattern that starts with neither `/` nor `*` gets a leading `/`, because a
    buyer types `blog` meaning `/blog` and rejecting that is pedantry. Matching
    is done lower-cased, so the stored form is lower-cased too and the UI shows
    back exactly what will be matched.
    """
    cleaned = pattern.strip()
    if not cleaned:
        return ""
    if not cleaned.startswith(("/", "*")):
        cleaned = f"/{cleaned}"
    return cleaned.lower()


def validate_patterns(patterns: Sequence[str], *, field_name: str) -> tuple[str, ...]:
    """Normalise and bound a pattern list, naming the offender on failure.

    Glob, not regex: an unbounded user regex is a ReDoS vector inside a loop that
    runs once per discovered link, and `^/blog/(?!draft).*$` is not a thing this
    audience writes. Glob is the notation they have already met in robots.txt and
    in Search Console, and it cannot backtrack pathologically.
    """
    cleaned: list[str] = []
    for raw in patterns:
        pattern = normalise_pattern(raw)
        if not pattern:
            continue
        if len(pattern) > CRAWL_MAX_PATTERN_LENGTH:
            raise ValidationError(
                f"{field_name} pattern is longer than "
                f"{CRAWL_MAX_PATTERN_LENGTH} characters: {raw[:60]}"
            )
        if not pattern.startswith(("/", "*")):
            raise ValidationError(f"{field_name} pattern must start with /: {raw}")
        if pattern not in cleaned:
            cleaned.append(pattern)
    if len(cleaned) > CRAWL_MAX_PATTERNS:
        raise ValidationError(
            f"{field_name} allows at most {CRAWL_MAX_PATTERNS} patterns, got {len(cleaned)}"
        )
    return tuple(cleaned)


def path_matches(pattern: str, path: str, query: str = "") -> bool:
    """Glob one pattern against a canonical path, and the query when asked.

    Semantics, stated in the UI in these words: a pattern with no wildcard
    matches anything starting with it (`/blog` covers `/blog/how-to`); `*`
    matches any characters including `/`; excludes always beat includes; an empty
    include list means everything in scope.

    `fnmatchcase` over lower-cased inputs rather than `fnmatch`: `fnmatch`
    normalises case through `os.path.normcase`, which is a no-op on Linux, so it
    would be case-sensitive in production and case-insensitive on a developer's
    Mac. Lower-casing both sides makes the documented case-insensitivity real and
    identical everywhere.
    """
    if not pattern:
        return False
    compiled = normalise_pattern(pattern)
    if not compiled:
        return False
    subject = path.lower() or "/"
    if "?" in compiled:
        subject = f"{subject}?{query.lower()}" if query else subject
    if "*" not in compiled and "?" not in compiled:
        # a bare path is a prefix, so /blog covers /blog, /blog/ and /blog/post-1
        compiled = compiled.rstrip("/") + "*"
    return fnmatch.fnmatchcase(subject, compiled)


def matched_pattern(patterns: Sequence[str], path: str, query: str = "") -> str | None:
    """First pattern that matches, so `reason_detail` can name it."""
    for pattern in patterns:
        if path_matches(pattern, path, query):
            return pattern
    return None


# ------------------------------------------------------------------ CrawlConfig


@dataclass(frozen=True, slots=True)
class CrawlConfig:
    """Validated `sources.config`.

    Frozen, and holding tuples rather than lists, because a wave reads this
    object once per discovered link across an async fan-out: a config that could
    be mutated mid-crawl would make two links on the same page answer to
    different rules.

    `max_pages` is already clamped by the time it lands here — `from_json`
    applies the deployment's hard ceiling — so no downstream caller has to
    remember to re-check the bound.
    """

    include_paths: tuple[str, ...] = ()
    exclude_paths: tuple[str, ...] = ()
    max_pages: int = CRAWL_DEFAULT_MAX_PAGES
    max_depth: int = CRAWL_DEFAULT_MAX_DEPTH
    include_subdomains: bool = False
    include_pdf: bool = False
    delay_ms: int = CRAWL_DEFAULT_DELAY_MS
    # url_hash values the tenant said "include this anyway" about, from the
    # review list. checked after scope/robots/depth and before the automatic
    # rules, so a knowledge base that genuinely lives at /search?q=faq is
    # reachable without turning the whole rule set off
    force_include: frozenset[str] = frozenset()
    # when true the crawl fetches the whole frontier but trains nothing, parking
    # in `awaiting_review` so a human approves the URL list first. off by default:
    # every source created before this existed, and every API caller that does not
    # know about it, must keep completing on its own
    review_before_index: bool = False

    @classmethod
    def from_json(
        cls,
        raw: Mapping[str, Any] | None,
        *,
        depth_ceiling: int = CRAWL_MAX_DEPTH_CEILING,
        default_depth: int | None = None,
        default_delay_ms: int | None = None,
    ) -> CrawlConfig:
        """Build from the JSONB column or from an API payload.

        Unknown keys are ignored rather than rejected: the column outlives this
        code and an older row must keep loading. Pattern lists are the one bound
        raised as an error rather than silently truncated, because truncation
        would index the wrong pages without telling anyone.

        Whole-site policy: max_pages, max_depth, include_subdomains,
        include_pdf and review_before_index are fixed inside this method — the
        dashboard no longer exposes them, and a stored or submitted value for
        them is ignored so every source crawls the whole site. `delay_ms`
        remains configurable and is clamped to CRAWL_MIN/MAX_DELAY_MS;
        `default_delay_ms` is what a config that says nothing about the delay
        gets, a parameter rather than a constant so Settings can retune it while
        this module stays importable without Settings.
        """
        data: Mapping[str, Any] = raw or {}

        include_paths = validate_patterns(
            _as_str_list(data.get("include_paths")), field_name="include_paths"
        )
        exclude_paths = validate_patterns(
            _as_str_list(data.get("exclude_paths")), field_name="exclude_paths"
        )

        # Whole-site crawl policy: these five knobs are fixed here, not settings.
        # The dashboard no longer exposes them, and forcing them in this one
        # place means every reader — engine waves, retrain, review — agrees
        # without each call site having to remember. Bound values still respect
        # the ceilings: 2000 < CRAWL_MAX_PAGES_CEILING, 6 < CRAWL_MAX_DEPTH_CEILING.
        max_pages = 2000
        max_depth = 6
        include_subdomains = True
        include_pdf = True
        review_before_index = True

        delay_floor = CRAWL_DEFAULT_DELAY_MS if default_delay_ms is None else default_delay_ms

        delay_ms = _clamp(
            _as_int(data.get("delay_ms"), delay_floor),
            CRAWL_MIN_DELAY_MS,
            CRAWL_MAX_DELAY_MS,
        )

        return cls(
            include_paths=include_paths,
            exclude_paths=exclude_paths,
            max_pages=max_pages,
            max_depth=max_depth,
            include_subdomains=include_subdomains,
            include_pdf=include_pdf,
            delay_ms=delay_ms,
            force_include=frozenset(_as_str_list(data.get("force_include"))),
            review_before_index=review_before_index,
        )

    def to_json(self) -> dict[str, Any]:
        """Round-trip shape for `sources.config`. Lists, because JSONB has no
        tuple and a reader comparing two configs must not see a type difference
        that is only an artefact of how Python held them."""
        return {
            "include_paths": list(self.include_paths),
            "exclude_paths": list(self.exclude_paths),
            "max_pages": self.max_pages,
            "max_depth": self.max_depth,
            "include_subdomains": self.include_subdomains,
            "include_pdf": self.include_pdf,
            "delay_ms": self.delay_ms,
            "force_include": sorted(self.force_include),
            "review_before_index": self.review_before_index,
        }

    def with_force_include(self, url_hashes: Iterable[str]) -> CrawlConfig:
        """Frozen, so "include this anyway" returns a new config to persist."""
        merged = self.force_include | frozenset(url_hashes)
        return CrawlConfig(
            include_paths=self.include_paths,
            exclude_paths=self.exclude_paths,
            max_pages=self.max_pages,
            max_depth=self.max_depth,
            include_subdomains=self.include_subdomains,
            include_pdf=self.include_pdf,
            delay_ms=self.delay_ms,
            force_include=merged,
            review_before_index=self.review_before_index,
        )


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(value, high))


def _as_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.lstrip("-").isdigit():
            return int(text)
    return default


def _as_str_list(value: Any) -> list[str]:
    """Accept a JSON list, or the space-separated string the UI input produces."""
    if value is None:
        return []
    if isinstance(value, str):
        return [part for part in value.replace(",", " ").split() if part]
    if isinstance(value, list | tuple | set | frozenset):
        return [str(item) for item in value if str(item).strip()]
    return []


# ------------------------------------------------------------------ canonical urls

_PERCENT_RE = re.compile(r"%([0-9A-Fa-f]{2})")


def _normalise_percent(value: str) -> str:
    """Uppercase the hex digits, decode the unreserved set, touch nothing else.

    Blanket `unquote` then `quote` would be wrong: it turns `%2F` into `/` and
    silently invents a path segment. Only escapes of characters that never needed
    escaping are decoded.
    """

    def replace(match: re.Match[str]) -> str:
        char = chr(int(match.group(1), 16))
        if char in _UNRESERVED:
            return char
        return f"%{match.group(1).upper()}"

    return _PERCENT_RE.sub(replace, value)


def _normalise_path(path: str) -> str:
    """Collapse duplicate slashes, resolve `.`/`..`, drop a directory index and
    the trailing slash. `/a//b/./c/../` and `/a/b/index.html` are one page."""
    segments: list[str] = []
    for segment in path.split("/"):
        if not segment or segment == ".":
            continue
        if segment == "..":
            if segments:
                segments.pop()
            continue
        segments.append(segment)
    if segments and segments[-1].lower() in CRAWL_INDEX_FILENAMES:
        segments.pop()
    # rule 8: the trailing slash is stripped, except when the path is exactly "/"
    return "/" + "/".join(segments)


def _normalise_query(query: str) -> str:
    """Drop tracking and session parameters by exact name, then sort and re-encode.

    An exact denylist, not a heuristic: "drop any long opaque value" would eat
    legitimate product SKUs and document ids, which are precisely the pages a
    buyer cares about.
    """
    if not query:
        return ""
    pairs = [
        (name, value)
        for name, value in parse_qsl(query, keep_blank_values=True)
        if name.lower() not in CRAWL_TRACKING_PARAMS
    ]
    if not pairs:
        return ""
    pairs.sort()
    return urlencode(pairs)


def host_of(url: str) -> str:
    """Lower-cased host of a URL or an origin. Empty string when there is none."""
    try:
        return (urlsplit(url.strip()).hostname or "").lower()
    except ValueError:
        return ""


def registrable_root(host: str) -> str:
    """The host minus a leading `www.`, used as the subdomain match root.

    Deliberately not a public-suffix list: shipping and refreshing one is real
    weight, and the failure mode is a `co.uk` site matching too broadly with
    `include_subdomains` on — which is why that flag's UI hint says to use it
    deliberately.
    """
    lowered = host.lower()
    return lowered[4:] if lowered.startswith("www.") else lowered


def origin_of(url: str) -> str | None:
    """`scheme://host[:port]` a crawl is pinned to. None when `url` is unusable.

    Resolved once from the seed, after its redirects, and stored on
    `sources.origin`. Every discovered link is checked against the stored value,
    so an open redirect found later cannot widen the crawl's scope.
    """
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        return None
    host = (parts.hostname or "").lower()
    if not host:
        return None
    if port is not None and port == _DEFAULT_PORTS[scheme]:
        port = None
    # `parts.hostname` strips an IPv6 literal's brackets; keep them, or the
    # origin is a string no parser can read a port back out of
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc = f"{netloc}:{port}"
    return f"{scheme}://{netloc}"


def _align_www(host: str, origin: str) -> str:
    """`example.com` and `www.example.com` are the same site, always. Whichever
    form the origin uses wins, so both spellings hash to one frontier row."""
    origin_host = host_of(origin)
    if not origin_host or host == origin_host:
        return host
    if origin_host == f"www.{host}" or host == f"www.{origin_host}":
        return origin_host
    return host


def canonicalise(url: str, base: str | None = None, origin: str | None = None) -> str | None:
    """One canonical spelling per page, or None when the URL is not fetchable.

    `base` is the page the link was found on, so a relative href resolves
    correctly; `origin` is the crawl's pinned origin, used only for the
    www alignment. None is returned — never an exception — for a scheme we do not
    fetch, a URL carrying credentials, or a hostless URL: the caller records the
    link as `excluded, reason='blocked_target'`, which is the cheap half of the
    SSRF defence and runs before the URL can enter the frontier at all.
    """
    candidate = url.strip()
    if not candidate:
        return None
    if base:
        candidate = urljoin(base, candidate)
    try:
        parts = urlsplit(candidate)
        port = parts.port
    except ValueError:
        return None

    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        return None
    # user:pass@host leaks credentials to whatever we resolve to and is a common
    # way to make a hostile URL look trusted
    if parts.username or parts.password:
        return None
    host = (parts.hostname or "").lower()
    if not host:
        return None

    if port is not None and port == _DEFAULT_PORTS[scheme]:
        port = None
    if origin:
        host = _align_www(host, origin)
    # `parts.hostname` strips the brackets off an IPv6 literal, and putting the
    # bare host back into a netloc makes `http://[::1]/` into
    # `http://::1/` — a string urlsplit cannot read a port from and httpx
    # refuses. an IPv6 host keeps its brackets.
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc = f"{netloc}:{port}"

    path = _normalise_percent(_normalise_path(parts.path))
    query = _normalise_query(parts.query)
    # rule 4: the fragment is dropped entirely, except a hashbang, which is
    # legacy SPA routing and genuinely addresses a different page
    fragment = parts.fragment if parts.fragment.startswith("!") else ""
    return urlunsplit((scheme, netloc, path, query, fragment))


def url_hash(url: str) -> str:
    """sha256 of the canonical URL — the identity of a crawled page.

    Identity is the URL, not the bytes. Two pages that render identical text (a
    print view, an /about vs /about-us alias) are separate frontier rows and are
    collapsed later by content hash or SimHash, with a reason the tenant can see.
    """
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def content_hash(markdown: str) -> str:
    """sha256 of the EXTRACTED markdown, never of the raw HTML.

    A page whose ads, CSRF token or "12 people are viewing this" counter rotate
    has stable content, and hashing the raw HTML would report it changed on every
    single recrawl — turning the conditional-GET saving into a permanent re-parse.
    """
    return hashlib.sha256(markdown.strip().encode("utf-8")).hexdigest()


def host_in_scope(host: str, origin: str, include_subdomains: bool = False) -> bool:
    """Host-level same-origin test.

    Subdomains are excluded unless asked for: `blog.example.com` is usually
    wanted, while `shop.`, `id.`, `status.` and `mail.` are not, and a wildcard
    on a large organisation explodes the frontier.
    """
    candidate = host.lower()
    origin_host = host_of(origin)
    if not candidate or not origin_host:
        return False
    if candidate == origin_host:
        return True
    if not include_subdomains:
        return False
    root = registrable_root(origin_host)
    return candidate == root or candidate.endswith(f".{root}")


def scheme_in_scope(scheme: str, origin: str) -> bool:
    """http -> https is an upgrade and allowed; https -> http is a downgrade and
    is refused, because a site that has moved to TLS does not want us quoting its
    plaintext mirror."""
    candidate = scheme.lower()
    origin_scheme = urlsplit(origin).scheme.lower()
    if candidate == origin_scheme:
        return True
    return candidate == "https" and origin_scheme == "http"


def in_scope(url: str, origin: str, include_subdomains: bool = False) -> bool:
    """Whole same-origin test, scheme included. Used by discovery before insert."""
    canonical = canonicalise(url, origin=origin)
    if canonical is None:
        return False
    parts = urlsplit(canonical)
    return scheme_in_scope(parts.scheme, origin) and host_in_scope(
        parts.hostname or "", origin, include_subdomains
    )


# ------------------------------------------------------- automatic exclusion rules

# Path segments, matched as whole segments anywhere in the path. Segment equality
# rather than substring, so `/blog/orders-of-magnitude` is not read as a checkout
# page while `/shop/cart` still is.
_LOGIN_SEGMENTS = frozenset(
    {
        "login",
        "signin",
        "sign-in",
        "log-in",
        "register",
        "signup",
        "sign-up",
        "auth",
        "oauth",
        "sso",
        "logout",
        "password",
        "forgot",
        "reset-password",
        "account",
        "my-account",
        "profile",
        "dashboard",
        "admin",
        "wp-admin",
        "wp-login",
        "user",
    }
)
_COMMERCE_SEGMENTS = frozenset(
    {
        "cart",
        "basket",
        "checkout",
        "order",
        "orders",
        "payment",
        "billing",
        "thank-you",
        "wishlist",
        "compare",
    }
)
_COMMERCE_SUBSTRINGS = ("add-to-cart",)
_COMMERCE_PARAMS = frozenset({"add_to_cart", "remove_item"})
_SEARCH_SEGMENTS = frozenset({"search", "find", "results"})
_SEARCH_PARAMS = frozenset({"s", "q", "query", "keyword", "search"})
_TAXONOMY_SEGMENTS = frozenset(
    {
        "tag",
        "tags",
        "category",
        "categories",
        "topics",
        "author",
        "authors",
        "archive",
        "archives",
        "label",
    }
)
# a bare year or year/month path is a date archive listing, not an article
_DATE_ARCHIVE_RE = re.compile(r"^/\d{4}(/\d{2})?$")
_PAGE_PATH_RE = re.compile(r"/page/(\d+)")
_PAGINATION_PARAMS = ("page", "paged", "p")
_CALENDAR_SEGMENTS = frozenset({"calendar"})
_EVENT_MONTH_RE = re.compile(r"/events/\d{4}-\d{2}")
_CALENDAR_PARAMS = frozenset({"date", "month", "year", "ical", "eventdate"})
_ALT_FORMAT_SEGMENTS = frozenset({"print", "amp", "feed", "wp-json", "oembed", "embed"})
_ALT_FORMAT_SUFFIXES = (".amp", ".rss", ".atom")
_ALT_FORMAT_PARAMS = frozenset({"print", "format", "output", "_escaped_fragment_"})


def _segments(path: str) -> list[str]:
    return [segment.lower() for segment in path.split("/") if segment]


def _segment_hit(segments: Sequence[str], names: frozenset[str]) -> str | None:
    """Whole-segment match, also trying the segment with its extension removed so
    `/wp-login.php` is recognised as `wp-login`."""
    for segment in segments:
        if segment in names:
            return segment
        stem = segment.rsplit(".", 1)[0]
        if stem != segment and stem in names:
            return stem
    return None


def _params_of(query: str) -> dict[str, str]:
    """Lower-cased parameter names to their first value. Canonicalisation already
    dropped the tracking set, so what survives here is meaningful."""
    params: dict[str, str] = {}
    for name, value in parse_qsl(query, keep_blank_values=True):
        key = name.lower()
        if key not in params:
            params[key] = value
    return params


def path_extension(path: str) -> str:
    """Lower-cased extension of the last path segment, `.pdf` style, or ""."""
    segments = _segments(path)
    if not segments:
        return ""
    last = segments[-1]
    if "." not in last:
        return ""
    return "." + last.rsplit(".", 1)[1]


def normalise_content_type(content_type: str | None) -> str:
    """`text/html; charset=utf-8` -> `text/html`."""
    if not content_type:
        return ""
    return content_type.split(";", 1)[0].strip().lower()


def looks_like_pdf(path: str, content_type: str | None = None) -> bool:
    return (
        path_extension(path) in CRAWL_PDF_EXTENSIONS
        or normalise_content_type(content_type) == CRAWL_PDF_MIME
    )


def _pagination_detail(path: str, params: Mapping[str, str]) -> str | None:
    match = _PAGE_PATH_RE.search(path)
    if match and int(match.group(1)) > 1:
        return match.group(1)
    for name in _PAGINATION_PARAMS:
        raw = params.get(name, "")
        if raw.isdigit() and int(raw) > 1:
            return raw
    return None


def _faceted_detail(params: Mapping[str, str]) -> str | None:
    for name in params:
        if name in CRAWL_FACET_PARAMS:
            return name
    if len(params) > CRAWL_MAX_QUERY_PARAMS:
        return str(len(params))
    return None


@dataclass(frozen=True, slots=True)
class Decision:
    """The outcome of the rule set for one candidate URL.

    `reason` is None when the URL is crawlable. Otherwise it is a member of
    `EXCLUSION_REASONS` and `detail` is whatever the sentence needs — the pattern
    that matched, the page number, the winning canonical URL — destined for
    `source_links.reason_detail`.
    """

    reason: str | None = None
    detail: str | None = None

    @property
    def allowed(self) -> bool:
        return self.reason is None

    @property
    def text(self) -> str:
        """The sentence a human reads. Rendered here so every client agrees."""
        return reason_text(self.reason, self.detail)


ALLOWED = Decision()


def classify_detailed(
    url: str,
    *,
    origin: str,
    config: CrawlConfig,
    depth: int = 0,
    robots_allowed: bool = True,
    content_type: str | None = None,
    pdf_count: int = 0,
    pdf_budget: int = CRAWL_MAX_PDFS_PER_SOURCE,
) -> Decision:
    """Decide one candidate URL. First matching rule wins, in this exact order.

    Order is not incidental. Scope, tenant patterns, robots and depth come first
    because they are the tenant's or the site's explicit instructions and must not
    be overridden by a heuristic. `force_include` is honoured after those four and
    before the automatic rules, so "include this anyway" on a `/search?q=faq`
    knowledge base works without disabling the rule set for the whole source, but
    cannot be used to crawl a different website or to defy robots.txt.

    `pdf_budget` is how many PDFs one source may take. It is a parameter, not a
    constant read here, because an operator retunes it (CRAWL_MAX_PDFS_PER_SOURCE
    in Settings) and this module must stay importable without Settings. The default
    is the constant the Settings default mirrors, so a caller that passes nothing
    decides exactly what it decided before.
    """
    canonical = canonicalise(url, origin=origin)
    if canonical is None:
        return Decision(REASON_BLOCKED_TARGET)

    parts = urlsplit(canonical)
    host = parts.hostname or ""
    path = parts.path or "/"
    query = parts.query

    # 1. same origin, scheme included
    if not scheme_in_scope(parts.scheme, origin) or not host_in_scope(
        host, origin, config.include_subdomains
    ):
        return Decision(REASON_OUT_OF_SCOPE)

    # 2. excludes always beat includes
    excluded = matched_pattern(config.exclude_paths, path, query)
    if excluded is not None:
        return Decision(REASON_EXCLUDED_PATH, excluded)

    # 3. an empty include list means everything in scope
    if config.include_paths and matched_pattern(config.include_paths, path, query) is None:
        return Decision(REASON_NOT_INCLUDED)

    # 4. the site's own instruction. recorded, not silently dropped, so the tenant
    #    can see that robots.txt is why their page is missing
    if not robots_allowed:
        return Decision(REASON_ROBOTS_DISALLOWED)

    # 5. depth
    if depth > config.max_depth:
        return Decision(REASON_DEPTH_EXCEEDED, str(config.max_depth))

    if url_hash(canonical) in config.force_include:
        return ALLOWED

    segments = _segments(path)
    params = _params_of(query)

    # 6. sign-in and account pages hold no public content
    hit = _segment_hit(segments, _LOGIN_SEGMENTS)
    if hit is not None:
        return Decision(REASON_LOGIN_SIGNUP, f"/{hit}")

    # 7. checkout flow
    hit = _segment_hit(segments, _COMMERCE_SEGMENTS)
    if hit is not None:
        return Decision(REASON_COMMERCE_FLOW, f"/{hit}")
    lowered_path = path.lower()
    if any(needle in lowered_path for needle in _COMMERCE_SUBSTRINGS) or (
        params.keys() & _COMMERCE_PARAMS
    ):
        return Decision(REASON_COMMERCE_FLOW)

    # 8. a search results page: its content lives on the pages it lists
    if _segment_hit(segments, _SEARCH_SEGMENTS) is not None or (
        params.keys() & _SEARCH_PARAMS
    ):
        return Decision(REASON_SEARCH_RESULTS)

    # 9. tag / category / author / date archives. the posts themselves are indexed
    if _segment_hit(segments, _TAXONOMY_SEGMENTS) is not None or _DATE_ARCHIVE_RE.match(
        path
    ):
        return Decision(REASON_ARCHIVE_OR_TAXONOMY)

    # 10. page 2..n of a listing
    page_number = _pagination_detail(path, params)
    if page_number is not None:
        return Decision(REASON_PAGINATION, page_number)

    # 11. calendar views generate an unbounded number of URLs
    if (
        _segment_hit(segments, _CALENDAR_SEGMENTS) is not None
        or _EVENT_MONTH_RE.search(path)
        or (params.keys() & _CALENDAR_PARAMS)
    ):
        return Decision(REASON_CALENDAR)

    # 12. print / AMP / feed / oEmbed renderings of a page already indexed
    if (
        _segment_hit(segments, _ALT_FORMAT_SEGMENTS) is not None
        or lowered_path.endswith(_ALT_FORMAT_SUFFIXES)
        or (params.keys() & _ALT_FORMAT_PARAMS)
    ):
        return Decision(REASON_ALT_FORMAT)

    # 13. faceted navigation: thousands of these exist and they hold no new text
    facet = _faceted_detail(params)
    if facet is not None:
        return Decision(REASON_FACETED_NAVIGATION, facet)

    # 14/15/16. PDFs are recognised before the non-HTML rule can reject them, so
    #           `include_pdf` gets its own explaining sentence either way
    if looks_like_pdf(path, content_type):
        if not config.include_pdf:
            return Decision(REASON_PDF_NOT_ENABLED)
        budget = max(0, pdf_budget)
        if pdf_count >= budget:
            return Decision(REASON_PDF_BUDGET_EXCEEDED, str(budget))
        return ALLOWED

    extension = path_extension(path)
    if extension and extension in CRAWL_NON_PAGE_EXTENSIONS:
        return Decision(REASON_NON_HTML, extension)
    resolved_type = normalise_content_type(content_type)
    if resolved_type and resolved_type not in CRAWL_HTML_MIMES:
        return Decision(REASON_NON_HTML, resolved_type)

    return ALLOWED


def classify(
    url: str,
    *,
    origin: str,
    config: CrawlConfig,
    depth: int = 0,
    robots_allowed: bool = True,
    content_type: str | None = None,
    pdf_count: int = 0,
    pdf_budget: int = CRAWL_MAX_PDFS_PER_SOURCE,
) -> str | None:
    """`source_links.reason` for a candidate URL, or None when it is crawlable."""
    return classify_detailed(
        url,
        origin=origin,
        config=config,
        depth=depth,
        robots_allowed=robots_allowed,
        content_type=content_type,
        pdf_count=pdf_count,
        pdf_budget=pdf_budget,
    ).reason


# ------------------------------------------------------------------ fingerprints

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def shingles(text: str, size: int = CRAWL_SIMHASH_SHINGLE_SIZE) -> list[str]:
    """Word n-shingles of the extracted markdown, lower-cased and alphanumeric.

    Shingles rather than bare words because word bags cannot tell two pages built
    from the same vocabulary apart, and it is exactly that pair — a print view and
    its article — that has to be caught.
    """
    tokens = _TOKEN_RE.findall(text.lower())
    if not tokens:
        return []
    if len(tokens) <= size:
        return [" ".join(tokens)]
    return [" ".join(tokens[index : index + size]) for index in range(len(tokens) - size + 1)]


def simhash(text: str, size: int = CRAWL_SIMHASH_SHINGLE_SIZE) -> int:
    """64-bit SimHash of the extracted markdown, as an UNSIGNED integer.

    blake2b at an 8-byte digest, one hash per shingle, each bit position
    accumulating +1 or -1, and the fingerprint is the sign of each position. About
    fifty lines and no dependency, which is the whole reason it beats a MinHash
    library here.

    Store it with `to_signed_64` — `source_links.simhash` is a BIGINT because
    postgres has no uint64.
    """
    weights = [0] * CRAWL_SIMHASH_BITS
    pieces = shingles(text, size)
    if not pieces:
        return 0
    for shingle in pieces:
        digest = hashlib.blake2b(shingle.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest, "big")
        for bit in range(CRAWL_SIMHASH_BITS):
            weights[bit] += 1 if (value >> bit) & 1 else -1
    fingerprint = 0
    for bit, weight in enumerate(weights):
        if weight > 0:
            fingerprint |= 1 << bit
    return fingerprint


def hamming_distance(left: int, right: int) -> int:
    """Bits that differ between two fingerprints."""
    return ((left ^ right) & CRAWL_SIMHASH_MASK).bit_count()


def to_signed_64(value: int) -> int:
    """Unsigned 64-bit fingerprint -> the signed BIGINT postgres will store.

    Exact and reversible, not lossy: the bit pattern is preserved and only its
    interpretation changes.
    """
    masked = value & CRAWL_SIMHASH_MASK
    if masked >= CRAWL_SIMHASH_SIGN_BOUND:
        return masked - (CRAWL_SIMHASH_MASK + 1)
    return masked


def from_signed_64(value: int) -> int:
    """The signed BIGINT read back out of postgres -> unsigned fingerprint."""
    return value & CRAWL_SIMHASH_MASK


def is_near_duplicate(
    left: int, right: int, max_distance: int = CRAWL_SIMHASH_MAX_DISTANCE_DEFAULT
) -> bool:
    """On 64 bits, distance <= 3 is roughly "95% of shingles shared" — a print
    view, an AMP variant, or a page whose only difference is a rotated
    testimonial. Two different posts on one topic land at 15-25, so the threshold
    separates them cleanly; above ~6 genuinely different product pages sharing a
    long spec table start to merge."""
    return hamming_distance(left, right) <= max_distance


def find_near_duplicate(
    fingerprint: int,
    candidates: Iterable[tuple[str, int]],
    max_distance: int = CRAWL_SIMHASH_MAX_DISTANCE_DEFAULT,
) -> str | None:
    """First already-trained page within `max_distance`, by its identifier.

    `candidates` is `(url_or_id, unsigned_fingerprint)` for this source's trained
    links — at most `max_pages` int comparisons, so a Python loop is the right
    tool and no index is needed. The winner is the first trained page, which under
    breadth-first crawling is the shallower and therefore more canonical one.
    """
    for identifier, other in candidates:
        if hamming_distance(fingerprint, other) <= max_distance:
            return identifier
    return None


# ------------------------------------------------------------------ html parsing

# the en dash and em dash are deliberate: real page titles separate the site name
# with whichever dash their CMS emits, and matching only the ASCII hyphen leaves
# the suffix on two thirds of them
_SITE_SUFFIX_SEPARATORS = (" | ", " - ", " – ", " — ", " :: ", " · ")  # noqa: RUF001


def extract_links(html: str, base_url: str, origin: str | None = None) -> list[str]:
    """Canonical, de-duplicated in-document-order links from one page.

    `<a href>` only — not `<link>`, not `<form action>`, not URL-shaped strings
    inside scripts. A crawler that follows `<link>` collects stylesheets and
    alternate feeds; one that scrapes script strings collects API endpoints. The
    frontier claim orders by depth, so document order here is the tie-break within
    a page and shallow pages still go first.
    """
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    links: list[str] = []
    for anchor in soup.find_all("a", href=True):
        raw = anchor.get("href")
        if not isinstance(raw, str):
            continue
        canonical = canonicalise(raw, base=base_url, origin=origin)
        if canonical is None or canonical in seen:
            continue
        seen.add(canonical)
        links.append(canonical)
    return links


def extract_canonical(html: str, base_url: str, origin: str | None = None) -> str | None:
    """`<link rel="canonical">`, resolved and canonicalised.

    The site's own declaration of its canonical URL is better information than any
    rule this module can write, so it overrides canonicalisation entirely — but
    only when it is same-origin, which the caller checks against the pinned origin
    before acting on it. It is read for canonicalisation, never followed as a new
    link.
    """
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.find_all("link"):
        rel = node.get("rel")
        values = rel if isinstance(rel, list) else [rel]
        if not any(isinstance(value, str) and value.lower() == "canonical" for value in values):
            continue
        href = node.get("href")
        if isinstance(href, str) and href.strip():
            return canonicalise(href, base=base_url, origin=origin)
    return None


def strip_site_suffix(title: str, site_name: str | None) -> str:
    """Drop a trailing " | Acme" / " - Acme" when it repeats the site's own name.

    The title becomes `documents.filename`, which is the label a user reads on a
    citation. Repeating the site name on all 400 pages makes every citation look
    the same and buries the part that identifies the page.
    """
    if not site_name:
        return title
    lowered = title.lower()
    target = site_name.strip().lower()
    if not target:
        return title
    for separator in _SITE_SUFFIX_SEPARATORS:
        marker = f"{separator}{target}"
        if lowered.endswith(marker):
            trimmed = title[: len(title) - len(marker)].strip()
            if trimmed:
                return trimmed
    return title


def extract_title(html: str, site_name: str | None = None) -> str | None:
    """`<title>`, whitespace-collapsed, site suffix stripped, length-capped."""
    soup = BeautifulSoup(html, "html.parser")
    node = soup.title
    if node is None:
        return None
    text = " ".join(node.get_text().split())
    if not text:
        return None
    return strip_site_suffix(text, site_name)[:CRAWL_MAX_TITLE_CHARS] or None


# ------------------------------------------------------------------ sitemaps


@dataclass(frozen=True, slots=True)
class SitemapEntry:
    """One `<url>` from a sitemap. `lastmod` is kept as the raw string the site
    published; parsing it into a timestamp is the repository's job, and a
    malformed value must not lose the URL."""

    url: str
    lastmod: str | None = None


@dataclass(frozen=True, slots=True)
class SitemapDocument:
    """A parsed sitemap: page URLs, plus nested sitemaps still to be fetched."""

    urls: tuple[SitemapEntry, ...] = ()
    sitemaps: tuple[str, ...] = ()


def _local_name(tag: object) -> str:
    """Namespace-agnostic element name. Real sitemaps disagree about the namespace
    URI and a meaningful fraction are served as text/html, so matching on the
    fully-qualified tag silently returns zero URLs on those sites."""
    return str(tag).rsplit("}", 1)[-1].lower()


def parse_sitemap(
    xml_text: str, base_url: str | None = None, origin: str | None = None
) -> SitemapDocument:
    """Parse `<urlset>` or `<sitemapindex>`; a malformed document yields nothing.

    Returning empty rather than raising is deliberate: discovery probes
    `/sitemap.xml` on every crawl whether or not robots.txt named one, so "there
    is no usable sitemap here" is the common case, not an error. Nested indexes
    are returned for the caller to fetch, which is where the follow-depth and
    total-URL caps are applied — this function has no I/O and cannot enforce them.
    """
    try:
        # entity expansion is the XML risk here, and ElementTree does not resolve
        # external entities; the byte cap upstream bounds the rest
        root = ElementTree.fromstring(xml_text)  # noqa: S314
    except ElementTree.ParseError:
        return SitemapDocument()

    urls: list[SitemapEntry] = []
    sitemaps: list[str] = []
    seen_urls: set[str] = set()
    seen_sitemaps: set[str] = set()

    for node in root.iter():
        kind = _local_name(node.tag)
        if kind not in ("url", "sitemap"):
            continue
        location = ""
        lastmod: str | None = None
        for child in node:
            name = _local_name(child.tag)
            if name == "loc":
                location = (child.text or "").strip()
            elif name == "lastmod":
                lastmod = (child.text or "").strip() or None
        if not location:
            continue
        canonical = canonicalise(location, base=base_url, origin=origin)
        if canonical is None:
            continue
        if kind == "sitemap":
            if canonical not in seen_sitemaps:
                seen_sitemaps.add(canonical)
                sitemaps.append(canonical)
        elif canonical not in seen_urls:
            seen_urls.add(canonical)
            urls.append(SitemapEntry(canonical, lastmod))

    return SitemapDocument(tuple(urls), tuple(sitemaps))


# ------------------------------------------------------------------ review tree

# A section is split into sub-sections only once it holds at least this many
# pages. Two, because a section is anything more than one page: `/legal` holding
# a privacy page and a terms page is a section a person wants one checkbox for,
# and a higher threshold produces a host row with four hundred loose URLs under
# it — the flat list the tree exists to replace.
CRAWL_REVIEW_SPLIT_THRESHOLD = 2

# How many path segments deep the tree may go. Two, because a section and a
# sub-section (`/blog/2019`) is as deep as a person checks in one pass, and past
# that the rows left are individual pages — the thing being reviewed. Below the
# limit a node keeps its remaining URLs as leaf rows, which read as their own
# full path, so nothing is hidden by the ceiling.
CRAWL_REVIEW_MAX_PATH_DEPTH = 2


@dataclass(frozen=True, slots=True)
class UrlGroup:
    """One expandable node of the pre-index review tree.

    `total` counts everything at or below this node, so a collapsed host row can
    say "412 pages" without the client summing its children. `urls` holds only
    what sits *directly* at this node — a URL appears exactly once in the whole
    tree, which is what makes "exclude this section" a well-defined set.
    """

    # stable identity for the client and for a prefix filter: "hissite.com" or
    # "hissite.com/blog". not a URL — it has no scheme, deliberately, because the
    # same section reached over http and https is one section to a human
    key: str
    # what to render: the bare host at depth 0, the path prefix below it
    label: str
    total: int
    urls: tuple[str, ...] = ()
    children: tuple[UrlGroup, ...] = ()


def group_urls(
    urls: Sequence[str],
    *,
    split_threshold: int = CRAWL_REVIEW_SPLIT_THRESHOLD,
    max_path_depth: int = CRAWL_REVIEW_MAX_PATH_DEPTH,
) -> tuple[UrlGroup, ...]:
    """Group URLs into a host / path-prefix tree for the review gate.

    Host first, because "which sites is this reading from" is the first question a
    person asks of a crawl, and a redirect or a subdomain rule can put more than
    one host in a frontier that was seeded with one. Path prefixes below it,
    because the pages a tenant actually wants to drop come in sections — the whole
    of `/careers`, the whole of `/blog/2019` — not one at a time out of four
    hundred.

    A node splits by path segment once two segments are in play — a lone bucket is
    an expander wrapping an expander, not a split — and the depth cap is where the
    recursion stops. Everything else stays a leaf row of its parent, labelled with
    its own full path. Those rules are why the shape of the tree tracks the shape
    of the site: a documentation site nests section under section, a landing page
    site is one host row with eight leaves.

    Pure and total: order is stable (hosts by page count then name, so the biggest
    section is the one you see first), unparseable URLs land under an empty host
    rather than being dropped, and no URL is ever counted twice.
    """
    by_host: dict[str, list[str]] = {}
    for url in urls:
        by_host.setdefault(host_of(url), []).append(url)

    groups = [
        _split_group(
            host,
            host,
            _with_segments(items),
            segment_index=0,
            depth_left=max(0, max_path_depth),
            split_threshold=split_threshold,
        )
        for host, items in by_host.items()
    ]
    return tuple(sorted(groups, key=lambda group: (-group.total, group.key)))


def _with_segments(urls: Sequence[str]) -> list[tuple[str, list[str]]]:
    """Pair each URL with its path segments, computed once for the whole recursion.

    The last segment is the page's own name, not a section — `/blog/2019/page-3`
    belongs to `/blog/2019` — so it is dropped before bucketing and the tree only
    ever groups by directory. A one-segment path keeps its segment, so `/blog`
    can be a section beside `/blog/2019` rather than nothing at all.
    """
    grouped: list[tuple[str, list[str]]] = []
    for url in urls:
        segments = _segments(urlsplit(url).path)
        grouped.append((url, segments[:-1] if len(segments) > 1 else segments))
    return grouped


def _split_group(
    key: str,
    label: str,
    items: Sequence[tuple[str, list[str]]],
    *,
    segment_index: int,
    depth_left: int,
    split_threshold: int,
) -> UrlGroup:
    """One node, recursing while splitting still tells the reader something."""
    total = len(items)
    if depth_left <= 0 or total < split_threshold:
        return UrlGroup(key=key, label=label, total=total, urls=_urls_of(items))

    # a URL with no segment at this position belongs to the node itself, not to any
    # child: `/blog` sits beside `/blog/2019`, it is not inside it
    here: list[tuple[str, list[str]]] = []
    buckets: dict[str, list[tuple[str, list[str]]]] = {}
    for url, segments in items:
        if segment_index >= len(segments):
            here.append((url, segments))
        else:
            buckets.setdefault(segments[segment_index], []).append((url, segments))

    # one bucket is not a split: the node would open onto a single child, an
    # expander wrapping an expander, which is a click that tells nobody anything.
    # everything it holds stays a leaf row of this node, rendered as its own
    # full path, `/compare/decagon` — the difference between this tree and a
    # folder browser
    if len(buckets) < 2:
        return UrlGroup(key=key, label=label, total=total, urls=_urls_of(items))

    children = [
        _split_group(
            f"{key}/{segment}",
            f"/{segment}",
            bucket,
            segment_index=segment_index + 1,
            depth_left=depth_left - 1,
            split_threshold=split_threshold,
        )
        for segment, bucket in buckets.items()
    ]
    return UrlGroup(
        key=key,
        label=label,
        total=total,
        urls=_urls_of(here),
        children=tuple(sorted(children, key=lambda group: (-group.total, group.key))),
    )


def _urls_of(items: Sequence[tuple[str, list[str]]]) -> tuple[str, ...]:
    return tuple(url for url, _ in items)





