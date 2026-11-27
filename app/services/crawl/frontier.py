"""Frontier policy: which URLs enter the queue, in what state, and when a crawl
is finished.

The frontier itself is `source_links` and its SQL lives in
`app/adapters/postgres/source_repo.py`; this module is the decision layer between
"the site offered us these URLs" and "these rows go in the table". It is
deliberately pure and synchronous — no I/O — so the ordering rules stay testable
by reading them.

Two properties here matter more than the rest:

**A refused URL is still a row.** Everything the crawler decides not to fetch is
inserted as `state='excluded'` with a machine reason, not dropped. That is what
turns "why is my pricing page missing" from a support ticket into a line in the
review list with a sentence and a Restore button (§3.5).

**The page budget is applied at insert time, in discovery order.** Truncating at
fetch time would spend the whole budget on whatever the first page happened to
link to; refusing at insert with `page_budget_exceeded` keeps the shallowest
pages — the ones a tenant cares about — and makes the overflow visible.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.core.constants import CRAWL_MAX_PDFS_PER_SOURCE
from app.core.logging import get_logger
from app.domain.crawl import (
    REASON_DOCUMENT_QUOTA_EXHAUSTED,
    REASON_PAGE_BUDGET_EXCEEDED,
    REASON_PDF_BUDGET_EXCEEDED,
    CrawlConfig,
    classify_detailed,
    looks_like_pdf,
    url_hash,
)
from app.domain.robots import RobotsTxt

logger = get_logger(__name__)

# link states that mean "this URL will not be fetched again this cycle"
SETTLED_STATES = frozenset({"trained", "unchanged", "excluded", "gone"})
# states that keep a wave alive
ACTIVE_STATES = frozenset({"pending", "fetching", "fetched"})

# a link that has failed this many times in a row stops being retried. it is not
# deleted — an outage must never shrink a corpus (§4.5)
CRAWL_MAX_LINK_ATTEMPTS = 5


@dataclass(slots=True)
class LinkCandidate:
    """One URL the crawler was offered, before any decision was made about it."""

    url: str
    depth: int = 0
    discovered_via: str = "seed"
    # raw `<lastmod>` from a sitemap, kept as the site wrote it
    lastmod: str | None = None


@dataclass(slots=True)
class PlannedLinks:
    """Insert-ready rows plus the accounting the caller reports."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    allowed: int = 0
    excluded: int = 0
    pdfs: int = 0
    reasons: dict[str, int] = field(default_factory=dict)


def parse_lastmod(raw: str | None) -> datetime | None:
    """A sitemap `<lastmod>` as a timezone-aware datetime, or None.

    Sites write W3C dates, RFC 3339 with a `Z`, bare dates, and occasionally
    nonsense. A value we cannot read is None rather than an error: `lastmod` is an
    optimisation, and a crawl that refused to run because one date was malformed
    would be strictly worse than one that fetched a page it did not need to.
    """
    if not raw:
        return None
    text = raw.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def plan_links(
    candidates: Iterable[LinkCandidate],
    *,
    origin: str,
    config: CrawlConfig,
    robots: RobotsTxt,
    pdf_count: int = 0,
    pdf_budget: int = CRAWL_MAX_PDFS_PER_SOURCE,
    known_hashes: set[str] | None = None,
    page_headroom: int | None = None,
    document_headroom: int | None = None,
) -> PlannedLinks:
    """Turn offered URLs into `source_links` rows.

    `known_hashes` are URLs this source already has rows for; they are skipped
    entirely rather than re-planned, because `ON CONFLICT DO NOTHING` would ignore
    them anyway and re-planning a known URL as `excluded` would be a lie about
    what happened to it.

    `page_headroom` is what is left of `max_pages` after existing rows;
    `document_headroom` is what is left of the tenant's plan quota. Both refuse
    with their own reason so the review list distinguishes "your site is bigger
    than your source limit" from "your account is full", which are different
    problems with different fixes.

    `pdf_budget` is passed straight to `classify_detailed`; the caller supplies the
    operator's value so this module needs no Settings of its own.
    """
    seen: set[str] = set(known_hashes or ())
    plan = PlannedLinks(pdfs=pdf_count)
    allowed_budget = page_headroom
    quota_budget = document_headroom

    for candidate in candidates:
        digest = url_hash(candidate.url)
        if digest in seen:
            continue
        seen.add(digest)

        is_pdf = looks_like_pdf(candidate.url)
        decision = classify_detailed(
            candidate.url,
            origin=origin,
            config=config,
            depth=candidate.depth,
            robots_allowed=robots.allows(candidate.url),
            pdf_count=plan.pdfs,
            pdf_budget=pdf_budget,
        )

        reason = decision.reason
        detail = decision.detail

        if decision.allowed and allowed_budget is not None and allowed_budget <= 0:
            reason, detail = REASON_PAGE_BUDGET_EXCEEDED, str(config.max_pages)
        elif decision.allowed and quota_budget is not None and quota_budget <= 0:
            reason, detail = REASON_DOCUMENT_QUOTA_EXHAUSTED, None

        if reason is None:
            plan.rows.append(
                {
                    "url": candidate.url,
                    "url_hash": digest,
                    "depth": candidate.depth,
                    "discovered_via": candidate.discovered_via,
                    "state": "pending",
                    "reason": None,
                    "reason_detail": None,
                    "lastmod": parse_lastmod(candidate.lastmod),
                }
            )
            plan.allowed += 1
            if is_pdf:
                plan.pdfs += 1
            if allowed_budget is not None:
                allowed_budget -= 1
            if quota_budget is not None:
                quota_budget -= 1
            continue

        plan.rows.append(
            {
                "url": candidate.url,
                "url_hash": digest,
                "depth": candidate.depth,
                "discovered_via": candidate.discovered_via,
                "state": "excluded",
                "reason": reason,
                "reason_detail": detail,
                "lastmod": parse_lastmod(candidate.lastmod),
            }
        )
        plan.excluded += 1
        plan.reasons[reason] = plan.reasons.get(reason, 0) + 1
        if reason == REASON_PDF_BUDGET_EXCEEDED:
            # counted so the next batch does not re-litigate the same ceiling
            plan.pdfs += 1

    return plan


def unchanged_by_lastmod(link: dict[str, Any], *, lastmod: datetime | None = None) -> bool:
    """True when the sitemap says this page has not changed since we fetched it.

    The cheapest tier in §4.3: no HTTP request at all, not even a conditional one.
    Only trusted when we have both a `lastmod` and a previous `fetched_at`, and
    only when the page is currently in a state that means we hold its content.
    """
    fetched_at = link.get("fetched_at")
    if fetched_at is None:
        return False
    if link.get("state") not in ("trained", "unchanged"):
        return False
    stamp = lastmod if lastmod is not None else link.get("lastmod")
    if not isinstance(stamp, datetime):
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    if not isinstance(fetched_at, datetime):
        return False
    reference = fetched_at if fetched_at.tzinfo else fetched_at.replace(tzinfo=UTC)
    return stamp <= reference


def conditional_headers(link: dict[str, Any]) -> tuple[str | None, str | None]:
    """`If-None-Match` / `If-Modified-Since` for a revalidation.

    Only sent when the page is one we already hold: sending them for a link in
    `failed` would turn a real retry into a 304 and leave the corpus stale.
    """
    if link.get("state") not in ("trained", "unchanged"):
        return None, None
    etag = link.get("etag")
    last_modified = link.get("last_modified")
    return (
        etag if isinstance(etag, str) and etag else None,
        last_modified if isinstance(last_modified, str) and last_modified else None,
    )


def exhausted(link: dict[str, Any], *, max_attempts: int = CRAWL_MAX_LINK_ATTEMPTS) -> bool:
    """True when a link has failed too many times to keep retrying.

    It stays in the table and stays visible under "Not found" with its last
    error — the row is never deleted, because an outage must not silently shrink
    a customer's corpus.
    """
    attempt = link.get("attempt")
    return isinstance(attempt, int) and attempt >= max_attempts


def is_finished(counts: dict[str, int]) -> bool:
    """True when no link is pending, fetching or fetched.

    Terminal detection is by state counts rather than by a wave's own bookkeeping,
    because the wave that finishes a crawl is frequently not the wave that started
    it and has no memory of what the others did.
    """
    return not any(counts.get(state, 0) > 0 for state in ACTIVE_STATES)


def state_counts(rows: Sequence[dict[str, Any]]) -> dict[str, int]:
    """Count rows by state, for the wave's own log line."""
    counts: dict[str, int] = {}
    for row in rows:
        state = str(row.get("state", "unknown"))
        counts[state] = counts.get(state, 0) + 1
    return counts
