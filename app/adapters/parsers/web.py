"""Web page extraction: HTML in, retrieval-grade markdown out.

Deliberately **not** registered in `ParserRegistry`. The registry resolves by
MIME, so registering this would hijack ordinary `.html` uploads and change
shipped behaviour; and it takes a per-source boilerplate fingerprint set that the
registry has no way to supply. The crawl extract phase constructs it directly.

Why the existing `HtmlParser` (`office.py:165`) is not enough: its denylist is
`script/style/nav/footer/noscript/svg`, which on a modern site misses `<header>`,
`<aside>`, cookie banners, newsletter modals, breadcrumbs, related-posts and
share buttons. Every page would then carry the same few hundred words of chrome,
which is the worst thing you can do to a retrieval corpus — the boilerplate
becomes the most frequent text in the index, dominates the tenant-scoped sparse
IDF statistics, and drags every chunk embedding toward the mean of "site chrome".

The pipeline is ordered with fallbacks so a page always gets the best available
extraction rather than an all-or-nothing result:

  stage 0  decode           charset header, then <meta charset>, then detection
  stage 1  structural strip tags, ARIA roles, and a class/id denylist
  stage 2  semantic container  main/article/[role=main]/.entry-content, accepted
                              only when it holds most of the surviving text
  stage 3  density scoring  a readability-style scorer, when stage 2 finds nothing
  stage 4  markdown         the HtmlParser walk, extended
  stage 5  boilerplate      drop blocks the frozen per-source fingerprint set names
  stage 6  quality gate     reported, not decided — the caller records the reason

Stage 4's heading walk emits `#` lines, so `split_structured` and
`build_structured_chunks` give a crawled page heading-aware chunking with no
change to the chunker.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from app.core.constants import (
    CRAWL_MAX_TITLE_CHARS,
    CRAWL_SPA_MAX_MARKDOWN_CHARS,
    CRAWL_SPA_MAX_TEXT_RATIO,
    CRAWL_SPA_MIN_HTML_BYTES,
    CRAWL_SPA_MOUNT_SELECTORS,
)
from app.core.logging import get_logger
from app.domain.crawl import extract_canonical, strip_site_suffix
from bs4 import BeautifulSoup

logger = get_logger(__name__)

# Stage 1. Applied as a filter over whole `class`/`id` tokens rather than a
# substring match on the attribute, so `related-products` is caught and
# `unrelated-content` is not. Crude, cheap, and it removes the large share of
# chrome that names itself honestly.
CRAWL_CLASS_DENYLIST_RE = re.compile(
    r"cookie|consent|gdpr|newsletter|subscribe|signup-modal|social|share|breadcrumb|"
    r"related|recommend|sidebar|widget-area|promo|banner|popup|modal|overlay|"
    r"skip-link|pagination|pager|comment|disqus|advert|\bads?\b|sponsor|"
    r"site-header|site-footer|masthead|colophon|off-canvas|menu-toggle|back-to-top",
    re.IGNORECASE,
)

# Stage 1. Structural tags with no prose in them, plus the interactive shells.
CRAWL_STRIP_TAGS: tuple[str, ...] = (
    "script",
    "style",
    "noscript",
    "svg",
    "iframe",
    "object",
    "embed",
    "form",
    "template",
    "button",
    "dialog",
    "nav",
    "header",
    "footer",
    "aside",
)

# Stage 1. ARIA and HTML attributes that say "this is not the content".
CRAWL_STRIP_ROLES = frozenset(
    {"navigation", "banner", "contentinfo", "complementary", "search", "dialog", "alert"}
)

# Stage 2, in priority order. The first match whose text is a large enough share
# of the page wins. These cover the large majority of WordPress, Ghost, Webflow,
# Squarespace, Docusaurus and Next.js marketing sites, because they all emit one.
CRAWL_SEMANTIC_SELECTORS: tuple[str, ...] = (
    "main",
    "[role=main]",
    "article",
    "[itemprop=articleBody]",
    '[property="articleBody"]',
    ".post-content",
    ".entry-content",
    ".article-body",
    "#content",
    "#main-content",
)

# Stage 2 acceptance threshold. Below this the element is a decorative wrapper
# around a layout grid and using it would drop the real content.
CRAWL_CONTAINER_MIN_TEXT_RATIO = 0.40

# Stage 3 scoring thresholds.
CRAWL_DENSITY_MIN_CANDIDATE_CHARS = 140
CRAWL_DENSITY_MAX_LINK_DENSITY = 0.5
CRAWL_DENSITY_MIN_ACCEPTED_CHARS = 200

# Stage 4. Alt text shorter than this is an icon or a decoration, not content.
CRAWL_MIN_ALT_CHARS = 15

# Stage 5. A block fingerprint is the first this-many characters of the
# normalised block; 200 is long enough to separate two footers and short enough
# that a reflowed paragraph still matches itself.
CRAWL_FINGERPRINT_PREFIX_CHARS = 200
CRAWL_FINGERPRINT_HEX_CHARS = 16
# a repeated block longer than this is a genuine legal notice on a small site,
# which is content; a repeated 40-word footer is not
CRAWL_BOILERPLATE_MAX_BLOCK_CHARS = 600

# JSON-LD is malformed on a large share of real sites and can be tens of KB, so
# only these scalar fields are kept and a parse failure is ignored.
CRAWL_JSONLD_FIELDS = ("headline", "datePublished", "dateModified", "name", "about")

_HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")
_WALK_TAGS = (
    *_HEADING_TAGS,
    "p",
    "li",
    "pre",
    "table",
    "blockquote",
    "dl",
    "img",
)
_META_CHARSET_RE = re.compile(rb"""charset\s*=\s*["']?\s*([A-Za-z0-9_\-.:]+)""", re.IGNORECASE)
_PUNCTUATION_RE = re.compile(r"[^a-z0-9\s]+")
_DIGIT_RE = re.compile(r"\d+")
_WHITESPACE_RE = re.compile(r"\s+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


class UndecodableError(Exception):
    """The bytes are not text in any encoding we could establish.

    Raised rather than falling back to `errors="replace"` over the whole
    document: a page of replacement characters indexes as noise and looks
    identical to a page with no content, so it has to be recorded as
    `failed, reason='undecodable'` instead.
    """


@dataclass(slots=True)
class WebExtraction:
    """Everything one page yields. The caller decides what is fatal.

    Stage 6 is reported here rather than enforced, because "too short" is
    `empty_after_extraction` while "too short and the HTML is big and there is a
    root mount point" is `javascript_rendered`, and only the crawl service knows
    whether a renderer is available to try instead.
    """

    markdown: str
    title: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # breadcrumb trail, captured before stage 1 strips it, else URL segments.
    # feeds chunks.heading_path, which is already carried into the vector payload
    # and rendered into the evidence block — retrieval signal for zero plumbing
    heading_path: list[str] = field(default_factory=list)
    canonical_url: str | None = None
    lang: str | None = None
    noindex: bool = False
    # stage-5 accounting, so a crawl can log how much chrome it removed
    blocks_total: int = 0
    blocks_dropped: int = 0
    # §3.2 detector. all three of its conditions have to hold
    spa_suspected: bool = False
    text_ratio: float = 0.0
    container: str = "body"


def normalise_block(block: str) -> str:
    """Canonical form of one markdown block, for fingerprinting.

    Lower-cased, punctuation and digits removed, whitespace collapsed. Digits go
    because a footer carrying "© 2024" must fingerprint the same as the same
    footer carrying "© 2025", which is the whole point of doing this across a
    recrawl rather than within one wave.
    """
    lowered = block.lower()
    lowered = _PUNCTUATION_RE.sub(" ", lowered)
    lowered = _DIGIT_RE.sub(" ", lowered)
    return _WHITESPACE_RE.sub(" ", lowered).strip()


def block_fingerprint(block: str) -> str:
    """16 hex characters identifying one block's text."""
    normalised = normalise_block(block)[:CRAWL_FINGERPRINT_PREFIX_CHARS]
    digest = hashlib.sha1(normalised.encode("utf-8"), usedforsecurity=False)
    return digest.hexdigest()[:CRAWL_FINGERPRINT_HEX_CHARS]


def block_fingerprints(markdown: str) -> list[str]:
    """Fingerprints of every short-enough block in a page, in document order.

    Only blocks under `CRAWL_BOILERPLATE_MAX_BLOCK_CHARS` are fingerprinted:
    a long block repeated across a twelve-page documentation site is a genuine
    legal notice and removing it would delete content.
    """
    out: list[str] = []
    for block in split_blocks(markdown):
        if len(block) < CRAWL_BOILERPLATE_MAX_BLOCK_CHARS and normalise_block(block):
            out.append(block_fingerprint(block))
    return out


def split_blocks(markdown: str) -> list[str]:
    """Blocks at markdown block boundaries — blank-line separated."""
    return [block.strip() for block in markdown.split("\n\n") if block.strip()]


def decode_html(data: bytes, content_type: str | None = None) -> str:
    """Stage 0. Charset from the header, then `<meta charset>`, then detection.

    Never `errors="replace"` over the whole document silently — see
    :class:`UndecodableError`.
    """
    candidates: list[str] = []
    if content_type and "charset=" in content_type.lower():
        declared = content_type.lower().split("charset=", 1)[1].split(";")[0].strip()
        if declared:
            candidates.append(declared.strip("\"'"))

    head = data[:4096]
    meta = _META_CHARSET_RE.search(head)
    if meta is not None:
        candidates.append(meta.group(1).decode("ascii", errors="ignore"))

    candidates.append("utf-8")

    for encoding in candidates:
        if not encoding:
            continue
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue

    # last resort: bs4's own detection, which wraps chardet/cchardet when present
    try:
        from bs4 import UnicodeDammit

        dammit = UnicodeDammit(data)
        if dammit.unicode_markup:
            return str(dammit.unicode_markup)
    except Exception:  # pragma: no cover - detection is best effort
        logger.debug("unicode_dammit_failed")

    raise UndecodableError("page bytes could not be decoded as text")
def _node_text(node: Any) -> str:
    """Visible text of one node, whitespace collapsed."""
    return " ".join(node.get_text(" ").split())


def _link_density(node: Any) -> float:
    """Share of a node's characters that sit inside `<a>`.

    The single most useful signal for telling a navigation list from a paragraph
    list: a link menu is ~1.0, prose with two references is ~0.05.
    """
    total = len(_node_text(node))
    if total == 0:
        return 1.0
    linked = sum(len(_node_text(anchor)) for anchor in node.find_all("a"))
    return min(1.0, linked / total)


def _attr_tokens(node: Any) -> str:
    """`class` and `id` tokens of a node as one space-joined string."""
    parts: list[str] = []
    classes = node.get("class")
    if isinstance(classes, list):
        parts.extend(token for token in classes if isinstance(token, str))
    elif isinstance(classes, str):
        parts.append(classes)
    node_id = node.get("id")
    if isinstance(node_id, str):
        parts.append(node_id)
    return " ".join(parts)


def _meta_value(soup: Any, *, name: str | None = None, prop: str | None = None) -> str | None:
    """`<meta name=…>` or `<meta property=…>` content, or None."""
    attrs: dict[str, str] = {}
    if name is not None:
        attrs["name"] = name
    if prop is not None:
        attrs["property"] = prop
    node = soup.find("meta", attrs=attrs)
    if node is None:
        return None
    content = node.get("content")
    if not isinstance(content, str):
        return None
    collapsed = " ".join(content.split())
    return collapsed or None


def _jsonld_fields(soup: Any) -> dict[str, str]:
    """Scalar fields of interest out of `application/ld+json` blocks.

    Malformed JSON-LD is common enough that a parse failure has to be ignored
    rather than fail the page, and only scalars are kept because the nested
    objects can be tens of kilobytes of publisher metadata.
    """
    found: dict[str, str] = {}
    for node in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = node.string or node.get_text()
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            continue
        candidates = parsed if isinstance(parsed, list) else [parsed]
        for item in candidates:
            if not isinstance(item, dict):
                continue
            for key in CRAWL_JSONLD_FIELDS:
                value = item.get(key)
                if isinstance(value, str) and value.strip() and key not in found:
                    found[key] = " ".join(value.split())[:500]
    return found


def collect_metadata(soup: Any) -> dict[str, Any]:
    """§3.4. Page metadata worth carrying into `documents.metadata`.

    Everything here is either a retrieval signal or an answer the model can cite
    (a published date lets it say "as of March 2024"), so it is kept even though
    it costs nothing to skip.
    """
    metadata: dict[str, Any] = {}
    pairs = (
        ("description", _meta_value(soup, name="description")),
        ("og_title", _meta_value(soup, prop="og:title")),
        ("og_description", _meta_value(soup, prop="og:description")),
        ("og_type", _meta_value(soup, prop="og:type")),
        ("site_name", _meta_value(soup, prop="og:site_name")),
        ("published_at", _meta_value(soup, prop="article:published_time")),
        ("modified_at", _meta_value(soup, prop="article:modified_time")),
        ("author", _meta_value(soup, name="author")),
        ("keywords", _meta_value(soup, name="keywords")),
    )
    for key, value in pairs:
        if value:
            metadata[key] = value[:1000]

    for key, value in _jsonld_fields(soup).items():
        metadata.setdefault(f"jsonld_{key}", value)

    published = soup.find("time", attrs={"datetime": True})
    if published is not None and "published_at" not in metadata:
        stamp = published.get("datetime")
        if isinstance(stamp, str) and stamp.strip():
            metadata["published_at"] = stamp.strip()[:100]
    return metadata


def has_noindex(soup: Any) -> bool:
    """True when the page asks robots not to index it.

    Honoured for the same reason `robots.txt` is: a page marked `noindex` is a
    page the owner does not consider canonical content, and indexing it produces
    citations to URLs they do not want surfaced.
    """
    for name in ("robots", "googlebot"):
        value = _meta_value(soup, name=name)
        if value and "noindex" in value.lower():
            return True
    return False


def breadcrumb_trail(soup: Any) -> list[str]:
    """The site's own breadcrumb, read before stage 1 strips the navigation.

    This is the highest-value item in §3.4: it is a human-curated topical path
    ("Support / Billing / Refunds") that goes straight into `heading_path`, which
    the chunker already carries into the vector payload and the evidence block.
    Deriving the same thing from URL segments is the fallback, not the plan.
    """
    selectors = (
        '[itemtype*="BreadcrumbList"]',
        'nav[aria-label*="readcrumb"]',
        ".breadcrumb",
        ".breadcrumbs",
        "#breadcrumbs",
        '[class*="breadcrumb"]',
    )
    for selector in selectors:
        try:
            node = soup.select_one(selector)
        except (ValueError, NotImplementedError):
            continue
        if node is None:
            continue
        items = [_node_text(item) for item in node.find_all(["a", "li", "span"])]
        trail: list[str] = []
        for item in items:
            cleaned = item.strip(" >/|›»")  # noqa: RUF001 - real breadcrumb separators
            if not cleaned or len(cleaned) > 80:
                continue
            if cleaned.lower() in {"home", "homepage"}:
                continue
            if trail and trail[-1] == cleaned:
                continue
            if cleaned not in trail:
                trail.append(cleaned)
        if trail:
            # the last crumb is the page itself, which the title already says
            return trail[:6]
    return []


def url_heading_path(url: str) -> list[str]:
    """Fallback `heading_path`: readable path segments of the URL."""
    path = urlsplit(url).path
    out: list[str] = []
    for raw in path.split("/"):
        segment = raw.strip()
        if not segment or "." in segment:
            continue
        readable = segment.replace("-", " ").replace("_", " ").strip()
        if readable and len(readable) <= 80:
            out.append(readable[:1].upper() + readable[1:])
    return out[:6]


def strip_structural(soup: Any) -> int:
    """Stage 1. Remove tags, ARIA roles and denylisted class/id subtrees.

    Order matters: tags go first so the class scan walks a much smaller tree.
    """
    removed = 0
    for node in soup.find_all(CRAWL_STRIP_TAGS):
        node.decompose()
        removed += 1
    for node in soup.find_all(attrs={"role": True}):
        role = node.get("role")
        if isinstance(role, str) and role.strip().lower() in CRAWL_STRIP_ROLES:
            node.decompose()
            removed += 1
    for node in soup.find_all(attrs={"aria-hidden": "true"}):
        node.decompose()
        removed += 1
    for node in soup.find_all(True):
        if node.decomposed:
            continue
        tokens = _attr_tokens(node)
        if tokens and CRAWL_CLASS_DENYLIST_RE.search(tokens):
            node.decompose()
            removed += 1
    return removed


def pick_container(soup: Any) -> tuple[Any, str]:
    """Stages 2 and 3. The subtree holding the page's content.

    Stage 2 trusts the site's own semantics but verifies them: a `<main>` that
    holds 8% of the surviving text is a decorative wrapper and using it would
    throw the page away. Stage 3 is the readability-style fallback for the sites
    that emit nothing but `<div>`.
    """
    body = soup.body or soup
    total = len(_node_text(body))
    if total == 0:
        return body, "body"

    for selector in CRAWL_SEMANTIC_SELECTORS:
        try:
            node = soup.select_one(selector)
        except (ValueError, NotImplementedError):
            continue
        if node is None:
            continue
        share = len(_node_text(node)) / total
        if share >= CRAWL_CONTAINER_MIN_TEXT_RATIO:
            return node, selector

    scored = score_density(body)
    if scored is not None:
        return scored, "density"
    return body, "body"


def score_density(body: Any) -> Any | None:
    """Stage 3. Readability-style scoring over block containers.

    Score is paragraph-weighted text length discounted by link density, because
    the thing that separates content from chrome is not size — a mega-menu is
    large — but whether the characters are prose or anchors.
    """
    best: Any | None = None
    best_score = 0.0
    for node in body.find_all(["div", "section", "article", "td", "main"]):
        text_length = len(_node_text(node))
        if text_length < CRAWL_DENSITY_MIN_CANDIDATE_CHARS:
            continue
        density = _link_density(node)
        if density > CRAWL_DENSITY_MAX_LINK_DENSITY:
            continue
        paragraphs = len(node.find_all(["p", "pre", "blockquote"]))
        commas = _node_text(node).count(",")
        score = text_length * (1.0 - density) + paragraphs * 25.0 + commas * 3.0
        # a nested node only wins if it scores better than its own parent, which
        # keeps the result the outermost element that still holds the content
        if score > best_score:
            best = node
            best_score = score
    if best is None or len(_node_text(best)) < CRAWL_DENSITY_MIN_ACCEPTED_CHARS:
        return None
    return best


def _code_language(node: Any) -> str:
    for token in (_attr_tokens(node) or "").split():
        lowered = token.lower()
        for prefix in ("language-", "lang-", "highlight-"):
            if lowered.startswith(prefix) and len(lowered) > len(prefix):
                return lowered[len(prefix) :]
    return ""


def _table_markdown(node: Any) -> str:
    """A pipe table, so `split_structured` keeps the rows together."""
    rows: list[list[str]] = []
    for row in node.find_all("tr"):
        cells = [
            _node_text(cell).replace("|", "\\|") for cell in row.find_all(["th", "td"])
        ]
        if any(cell for cell in cells):
            rows.append(cells)
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    lines: list[str] = []
    for index, row in enumerate(rows):
        padded = row + [""] * (width - len(row))
        lines.append("| " + " | ".join(padded) + " |")
        if index == 0:
            lines.append("| " + " | ".join(["---"] * width) + " |")
    return "\n".join(lines)


def to_markdown(container: Any) -> str:
    """Stage 4. The `HtmlParser` walk, extended.

    Extends it with `blockquote`, `dl`, tables that survive as tables, fenced
    code with its language, and image `alt` text long enough to be a description
    rather than an icon label. Emitting `#` headings is what makes the existing
    structured chunker heading-aware on a crawled page.
    """
    parts: list[str] = []
    consumed: set[int] = set()
    for node in container.find_all(_WALK_TAGS):
        if id(node) in consumed:
            continue
        name = node.name
        if name in ("pre", "table", "dl", "blockquote"):
            # a self-contained block: its descendants must not be emitted again
            for child in node.find_all(True):
                consumed.add(id(child))

        if name in _HEADING_TAGS:
            text = _node_text(node)
            if text:
                parts.append("#" * int(name[1]) + " " + text)
            continue
        if name == "p":
            text = _node_text(node)
            if text:
                parts.append(text)
            continue
        if name == "li":
            text = _node_text(node)
            if text:
                parts.append("- " + text)
            continue
        if name == "blockquote":
            text = _node_text(node)
            if text:
                parts.append("> " + text)
            continue
        if name == "pre":
            code = node.get_text("\n").strip("\n")
            if code.strip():
                language = _code_language(node) or _code_language(node.find("code") or node)
                parts.append(f"```{language}\n{code}\n```")
            continue
        if name == "dl":
            entries: list[str] = []
            for child in node.find_all(["dt", "dd"]):
                text = _node_text(child)
                if not text:
                    continue
                entries.append(f"**{text}**" if child.name == "dt" else text)
            if entries:
                parts.append("\n".join(entries))
            continue
        if name == "table":
            table = _table_markdown(node)
            if table:
                parts.append(table)
            continue
        if name == "img":
            alt = node.get("alt")
            if isinstance(alt, str):
                collapsed = " ".join(alt.split())
                if len(collapsed) >= CRAWL_MIN_ALT_CHARS:
                    parts.append(collapsed)
            continue

    # a page whose content is bare text in divs produces nothing above, and
    # falling through to the whole subtree's text beats reporting it as empty
    if not parts:
        text = _node_text(container)
        if text:
            parts.append(text)

    joined = "\n\n".join(part.strip() for part in parts if part.strip())
    return _BLANK_LINES_RE.sub("\n\n", joined).strip()


def _mount_is_empty(soup: Any) -> bool:
    """True when a known client-render root exists and holds no text."""
    for selector in CRAWL_SPA_MOUNT_SELECTORS:
        try:
            node = soup.select_one(selector)
        except (ValueError, NotImplementedError):
            continue
        if node is not None and len(_node_text(node)) < CRAWL_SPA_MAX_MARKDOWN_CHARS:
            return True
    return False


def _noscript_hint(soup: Any) -> bool:
    """`<noscript>` telling the reader to enable JavaScript."""
    for node in soup.find_all("noscript"):
        text = _node_text(node).lower()
        if "javascript" in text or "enable js" in text:
            return True
    return False


class WebPageParser:
    """HTML bytes to retrieval-grade markdown for one crawled page.

    Constructed per source, not per page, because `boilerplate` is the frozen
    fingerprint set for that source: the same parser instance extracts every page
    of a wave and drops the blocks the site repeats everywhere. An empty set
    (the first wave, before freezing) simply skips stage 5.
    """

    def __init__(self, boilerplate: frozenset[str] = frozenset()) -> None:
        self.boilerplate = boilerplate

    def parse(
        self,
        data: bytes,
        url: str,
        *,
        content_type: str | None = None,
        site_name: str | None = None,
        origin: str | None = None,
    ) -> WebExtraction:
        """Extract one page. Raises :class:`UndecodableError` on stage 0 only."""
        html = decode_html(data, content_type)
        soup: Any = BeautifulSoup(html, "html.parser")

        # everything read before stage 1, because stage 1 deletes the nav that
        # carries the breadcrumb and the <noscript> that proves it is an SPA
        title = self._title(soup, site_name)
        lang = self._lang(soup)
        metadata = collect_metadata(soup)
        noindex = has_noindex(soup)
        trail = breadcrumb_trail(soup)
        mount_empty = _mount_is_empty(soup)
        noscript_hint = _noscript_hint(soup)
        raw_text_length = len(_node_text(soup))
        text_ratio = raw_text_length / len(data) if data else 0.0
        canonical = extract_canonical(html, url, origin)

        strip_structural(soup)
        container, container_name = pick_container(soup)
        markdown = to_markdown(container)

        blocks = split_blocks(markdown)
        kept, dropped = self._drop_boilerplate(blocks)
        markdown = "\n\n".join(kept)

        spa = (
            len(markdown) < CRAWL_SPA_MAX_MARKDOWN_CHARS
            and len(data) > CRAWL_SPA_MIN_HTML_BYTES
            and (noscript_hint or mount_empty or text_ratio < CRAWL_SPA_MAX_TEXT_RATIO)
        )

        if not trail:
            trail = url_heading_path(url)

        return WebExtraction(
            markdown=markdown,
            title=title,
            metadata=metadata,
            heading_path=trail,
            canonical_url=canonical,
            lang=lang,
            noindex=noindex,
            blocks_total=len(blocks),
            blocks_dropped=dropped,
            spa_suspected=spa,
            text_ratio=text_ratio,
            container=container_name,
        )

    def _drop_boilerplate(self, blocks: list[str]) -> tuple[list[str], int]:
        """Stage 5. Drop blocks the frozen fingerprint set names."""
        if not self.boilerplate:
            return blocks, 0
        kept: list[str] = []
        dropped = 0
        for block in blocks:
            if (
                len(block) < CRAWL_BOILERPLATE_MAX_BLOCK_CHARS
                and block_fingerprint(block) in self.boilerplate
            ):
                dropped += 1
                continue
            kept.append(block)
        return kept, dropped

    def _title(self, soup: Any, site_name: str | None) -> str | None:
        """`<title>`, then `og:title`, then the first `<h1>`.

        Read off the soup we already have rather than through
        `crawl.extract_title`, which reparses the document; the site-suffix rule
        itself is still the domain function.
        """
        candidates: list[str] = []
        if soup.title is not None:
            candidates.append(_node_text(soup.title))
        og_title = _meta_value(soup, prop="og:title")
        if og_title:
            candidates.append(og_title)
        heading = soup.find("h1")
        if heading is not None:
            candidates.append(_node_text(heading))
        for candidate in candidates:
            text = strip_site_suffix(candidate, site_name).strip()
            if text:
                return text[:CRAWL_MAX_TITLE_CHARS]
        return None

    def _lang(self, soup: Any) -> str | None:
        """`<html lang>`, normalised to its primary subtag."""
        node = soup.find("html")
        if node is None:
            return None
        value = node.get("lang")
        if not isinstance(value, str) or not value.strip():
            return None
        return value.strip().split("-")[0].lower()[:8]
