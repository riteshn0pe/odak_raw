"""The four Part B watchers and the bars every candidate must clear.

Each watcher is an async function returning ``Candidate`` objects; none of them
writes a row. The service decides what survives and what reaches the owner,
which keeps the "is this worth telling them" judgement in one place instead of
four.

Ordering is deliberate. ``own_numbers`` runs first and is the only watcher with
no external dependency, no rate limit and no empty-result day: every tenant with
orders or messages has numbers moving, and no CRM surfaces them as ranked action
items. The three external watchers are upside on top — when the search provider
is unconfigured or a competitor's site is down, the digest still has content.

Three bars every candidate clears before the service will write it:

* **New.** ``uid`` is sha256 over source + subject, checked against
  ``growth_insights`` before the insert, so a second run the same day adds
  nothing.
* **Sourced.** ``source_url`` is non-empty. For the external watchers that is the
  page or article; for ``own_numbers`` it is the tenant's own panel, which is
  where the number can be verified.
* **Actionable against this tenant's rows.** ``action_payload.evidence`` must
  carry at least one number that came from this tenant's data, and the segment —
  when present — must be a real key from ``app/domain/segments.py`` with at least
  one member. "Your competitor discounted X" is not an insight. "Your competitor
  discounted X and 300 of your customers bought X in six months" is.

There is no ``competitor_social`` watcher and there will not be one: Instagram,
Facebook and X publish no third-party read API for accounts we do not own, and
scraping them breaks their terms and breaks constantly. The type CHECK in
migration 020 makes that permanent rather than a convention.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from app.core.logging import get_logger

logger = get_logger(__name__)

AGENT_KEY = "growth_insights"
FEATURE = "insights"

# Per-call leashes. The whole run has its own budget in the service; these stop
# one slow competitor site or one slow model call from eating it.
FETCH_TIMEOUT_SECONDS = 20.0
LLM_TIMEOUT_SECONDS = 25.0
SEARCH_TIMEOUT_SECONDS = 20.0

# A competitor page is read for its product and price text, not indexed. 512 KB
# is generous for that and small enough that forty URLs is not a memory event.
PAGE_MAX_BYTES = 512 * 1024

# How much of a page's extracted text is kept for the next run's diff. The diff
# only ever looks at product/price/offer lines, so storing the whole page would
# be storing noise we have already decided to ignore.
EXTRACT_MAX_CHARS = 20_000

# Fan-out caps. The unfiltered daily run over every competitor URL and every
# keyword is the expensive failure mode this whole module is shaped around.
MAX_COMPETITOR_URLS = 8
MAX_NEWS_QUERIES = 6
MAX_NEWS_RESULTS = 4
MAX_EVENT_QUERIES = 2

# Lookahead for the calendar. Six weeks is long enough that a campaign can still
# be drafted, approved and sent before the date, and short enough that Diwali
# does not sit at the top of the panel for four months.
EVENT_LOOKAHEAD_DAYS = 45
# Inside this window an event stops being a heads-up and becomes this week's
# work, which is the difference between urgency 2 and urgency 3.
EVENT_URGENT_DAYS = 14

# own_numbers thresholds. All three are deliberately blunt: a 3% wobble in a
# reply rate is noise, and an insight that fires on noise is one the owner
# learns to ignore.
MIN_SENDS_FOR_RATE = 20
REPLY_DROP_RATIO = 0.75
BOUNCE_RATE_FLOOR = 0.05
MIN_ORDERS_FOR_CATEGORY = 3
CATEGORY_DROP_RATIO = 0.5
MIN_SEGMENT_MEMBERS = 5


@dataclass(slots=True)
class Candidate:
    """One thing a watcher found, before the service decides whether it lands.

    ``subject`` is not stored — it is the second half of the dedup key, and it is
    what makes "the same competitor page changed again" collapse onto one row
    while "a different competitor page changed" does not.
    """

    type: str
    subject: str
    summary: str
    why: str
    suggested_action: str
    source_url: str
    confidence: int
    urgency: int
    action_payload: dict[str, Any] = field(default_factory=dict)

    @property
    def uid(self) -> str:
        return make_uid(self.source_url, self.subject)

    def as_row(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "type": self.type,
            "summary": self.summary,
            "why": self.why,
            "suggested_action": self.suggested_action,
            "action_payload": self.action_payload,
            "source_url": self.source_url,
            "confidence": _bucket(self.confidence),
            "urgency": _bucket(self.urgency),
        }


def make_uid(source: str, subject: str) -> str:
    """The dedup key: sha256 over source plus subject.

    Both halves matter. Source alone would collapse every article from one
    publisher onto one row; subject alone would let two outlets covering the same
    GST change produce two identical insights.
    """
    digest = hashlib.sha256(f"{source.strip()}|{subject.strip()}".encode())
    return digest.hexdigest()


def _bucket(value: Any) -> int:
    """Coerce anything a model or a watcher produced into the 1-3 CHECK.

    A model asked for "1, 2 or 3" will occasionally answer 0.73 or 5. The column
    rejects both, and a rejected insert would lose the whole batch, so the
    clamping happens here where the intent — "low, medium, high" — is still
    legible.
    """
    try:
        number = round(float(value))
    except (TypeError, ValueError):
        return 1
    return max(1, min(3, number))


def clears_action_bar(candidate: Candidate) -> bool:
    """The hard bar: does this action cite this tenant's own rows?

    A summary of an article is not an insight, and this is where that is
    enforced rather than stored and ranked low. ``evidence`` must carry at least
    one number that came from the tenant's data; a segment, when the candidate
    names one, must have members — a campaign to nobody is not an action.
    """
    payload = candidate.action_payload or {}
    evidence = payload.get("evidence")
    if not isinstance(evidence, dict) or not evidence:
        return False
    numbers = [v for v in evidence.values() if isinstance(v, (int, float))]
    if not any(n > 0 for n in numbers):
        return False
    segment = payload.get("segment")
    if segment is not None:
        from app.domain import segments as segment_defs

        if not isinstance(segment, dict):
            return False
        if str(segment.get("key") or "") not in segment_defs.SEGMENT_KEYS:
            return False
        if int(payload.get("members") or 0) <= 0:
            return False
    return bool(candidate.suggested_action.strip())


# ---------------------------------------------------------------- own numbers


async def own_numbers(container: Any, tenant_id: UUID, panel_base: str) -> list[Candidate]:
    """The watcher that carries the stage: this tenant's own movements.

    Four signals, all computed rather than modelled — a dropping reply rate, a
    rising bounce rate, a segment gone quiet, a category falling off. No LLM
    call, no external provider, no empty day. The source URL is the tenant's own
    panel, which is where each number can be checked.
    """
    from app.domain import segments as segment_defs

    out: list[Candidate] = []
    reports_url = f"{panel_base}/growth/reports" if panel_base else ""
    customers_url = f"{panel_base}/growth/customers" if panel_base else ""
    if not reports_url:
        # Every insight needs a source URL and own_numbers' source is the panel.
        # With no configured panel base there is nowhere to point the owner, and
        # a row with a fabricated link is worse than no row.
        logger.info("growth_insights_no_panel_base", tenant_id=str(tenant_id))
        return out

    stats = await _message_stats(tenant_id)
    out.extend(_reply_rate_signal(stats, reports_url))
    out.extend(_bounce_signal(stats, reports_url))

    try:
        customers = await container.growth.customers.all_for_segments(tenant_id)
        orders = await container.growth.orders.recent(tenant_id)
    except Exception:
        logger.warning(
            "growth_insights_own_numbers_store_failed",
            tenant_id=str(tenant_id),
            exc_info=True,
        )
        return out

    now = datetime.now(UTC)
    out.extend(_quiet_segment_signal(segment_defs, customers, orders, now, customers_url))
    out.extend(_category_signal(segment_defs, customers, orders, now, customers_url))
    return out


async def _message_stats(tenant_id: UUID) -> dict[str, int]:
    """Outbound/inbound/failed counts for the last 14 days and the 14 before.

    Two windows in one statement: comparing this fortnight against the last is
    the only way a rate change becomes a fact rather than a number, and two round
    trips for two halves of one comparison is a waste of a connection.
    """
    from sqlalchemy import text

    from app.core.db import tenant_scope

    async with tenant_scope(tenant_id) as session:
        row = (
            await session.execute(
                text(
                    "SELECT "
                    "count(*) FILTER (WHERE direction = 'out' "
                    "  AND created_at >= now() - interval '14 days') AS out_recent, "
                    "count(*) FILTER (WHERE direction = 'in' "
                    "  AND created_at >= now() - interval '14 days') AS in_recent, "
                    "count(*) FILTER (WHERE direction = 'out' "
                    "  AND status IN ('bounced', 'complained', 'failed') "
                    "  AND created_at >= now() - interval '14 days') AS bad_recent, "
                    "count(*) FILTER (WHERE direction = 'out' "
                    "  AND created_at >= now() - interval '28 days' "
                    "  AND created_at <  now() - interval '14 days') AS out_prior, "
                    "count(*) FILTER (WHERE direction = 'in' "
                    "  AND created_at >= now() - interval '28 days' "
                    "  AND created_at <  now() - interval '14 days') AS in_prior, "
                    "count(*) FILTER (WHERE direction = 'out' "
                    "  AND status IN ('bounced', 'complained', 'failed') "
                    "  AND created_at >= now() - interval '28 days' "
                    "  AND created_at <  now() - interval '14 days') AS bad_prior "
                    "FROM growth_messages WHERE tenant_id = :tid"
                ),
                {"tid": tenant_id},
            )
        ).mappings().first()
    if row is None:
        return {}
    return {key: int(value or 0) for key, value in dict(row).items()}


def _reply_rate_signal(stats: dict[str, int], source_url: str) -> list[Candidate]:
    out_recent = stats.get("out_recent", 0)
    out_prior = stats.get("out_prior", 0)
    if out_recent < MIN_SENDS_FOR_RATE or out_prior < MIN_SENDS_FOR_RATE:
        return []
    recent = stats.get("in_recent", 0) / out_recent
    prior = stats.get("in_prior", 0) / out_prior
    if prior <= 0 or recent >= prior * REPLY_DROP_RATIO:
        return []
    drop = round((1 - recent / prior) * 100)
    return [
        Candidate(
            type="own_numbers",
            subject="reply_rate_drop",
            summary=(
                f"Your reply rate fell {drop}% — "
                f"{recent * 100:.1f}% this fortnight against {prior * 100:.1f}% the one before."
            ),
            why=(
                f"You sent {out_recent} messages in the last 14 days and got "
                f"{stats.get('in_recent', 0)} replies, against {stats.get('in_prior', 0)} "
                f"replies on {out_prior} messages the fortnight before. Same effort, "
                "fewer conversations."
            ),
            suggested_action=(
                "Re-work the opening line and the subject before the next batch, and "
                "warm the quiet customers with a short offer rather than another cold send."
            ),
            source_url=source_url,
            confidence=3,
            urgency=2 if drop < 40 else 3,
            action_payload={
                "evidence": {
                    "sent_14d": out_recent,
                    "replies_14d": stats.get("in_recent", 0),
                    "reply_rate_drop_pct": drop,
                },
            },
        )
    ]


def _bounce_signal(stats: dict[str, int], source_url: str) -> list[Candidate]:
    out_recent = stats.get("out_recent", 0)
    if out_recent < MIN_SENDS_FOR_RATE:
        return []
    bad = stats.get("bad_recent", 0)
    rate = bad / out_recent
    if rate < BOUNCE_RATE_FLOOR:
        return []
    prior_total = stats.get("out_prior", 0)
    prior_rate = (stats.get("bad_prior", 0) / prior_total) if prior_total else 0.0
    return [
        Candidate(
            type="own_numbers",
            subject="bounce_rate_high",
            summary=(
                f"{bad} of your last {out_recent} sends bounced or failed "
                f"({rate * 100:.1f}%)."
            ),
            why=(
                f"Anything above {BOUNCE_RATE_FLOOR * 100:.0f}% starts costing you "
                "deliverability on every later send, including the ones to customers "
                f"who would have replied. The fortnight before was {prior_rate * 100:.1f}%."
            ),
            suggested_action=(
                f"Clean the {bad} failing addresses out of the list before the next "
                "campaign — they are spending your sender reputation for nothing."
            ),
            source_url=source_url,
            confidence=3,
            urgency=3 if rate >= BOUNCE_RATE_FLOOR * 2 else 2,
            action_payload={
                "evidence": {
                    "failed_14d": bad,
                    "sent_14d": out_recent,
                    "failure_rate_pct": round(rate * 100, 1),
                },
            },
        )
    ]


def _quiet_segment_signal(
    segment_defs: Any,
    customers: list[dict[str, Any]],
    orders: list[dict[str, Any]],
    now: datetime,
    source_url: str,
) -> list[Candidate]:
    """Customers who used to buy and have stopped, as a segment with a count."""
    if not customers:
        return []
    try:
        at_risk = segment_defs.compute("at_risk", customers, orders, now, {})
    except Exception:
        logger.warning("growth_insights_segment_failed", segment="at_risk", exc_info=True)
        return []
    members = len(at_risk)
    if members < MIN_SEGMENT_MEMBERS:
        return []
    return [
        Candidate(
            type="own_numbers",
            subject="at_risk_segment",
            summary=f"{members} customers who used to buy from you have gone quiet.",
            why=(
                f"They are in your at-risk segment: {members} people with orders behind "
                f"them and nothing recent, out of {len(customers)} customers on file. "
                "Winning one back costs a message; replacing one costs a lead."
            ),
            suggested_action=(
                f"Send the at-risk segment ({members} customers) a win-back offer this "
                "week."
            ),
            source_url=source_url,
            confidence=3,
            urgency=2,
            action_payload={
                "segment": {"key": "at_risk", "params": {}},
                "members": members,
                "offer_pct": 10,
                "evidence": {"at_risk_customers": members, "customers": len(customers)},
            },
        )
    ]


def _category_signal(
    segment_defs: Any,
    customers: list[dict[str, Any]],
    orders: list[dict[str, Any]],
    now: datetime,
    source_url: str,
) -> list[Candidate]:
    """A product category whose orders halved against the month before."""
    if not orders:
        return []
    recent_cut = now - timedelta(days=30)
    prior_cut = now - timedelta(days=60)
    recent: dict[str, int] = {}
    prior: dict[str, int] = {}
    for order in orders:
        stamp = segment_defs.order_timestamp(order.get("ordered_at"))
        if stamp is None:
            continue
        when = datetime.fromtimestamp(stamp, tz=UTC)
        bucket = recent if when >= recent_cut else (prior if when >= prior_cut else None)
        if bucket is None:
            continue
        for item in segment_defs.order_items(order.get("items")):
            category = str(item.get("category") or "").strip().lower()
            if category:
                bucket[category] = bucket.get(category, 0) + 1

    out: list[Candidate] = []
    for category, before in sorted(prior.items(), key=lambda kv: -kv[1]):
        if before < MIN_ORDERS_FOR_CATEGORY:
            continue
        after = recent.get(category, 0)
        if after >= before * CATEGORY_DROP_RATIO:
            continue
        params = {"category": category}
        try:
            members = len(
                segment_defs.compute("by_category", customers, orders, now, params)
            )
        except Exception:
            logger.warning(
                "growth_insights_segment_failed", segment="by_category", exc_info=True
            )
            continue
        if members <= 0:
            continue
        out.append(
            Candidate(
                type="own_numbers",
                subject=f"category_drop:{category}",
                summary=(
                    f"Orders in {category} fell from {before} to {after} against "
                    "the month before."
                ),
                why=(
                    f"{members} of your customers have bought {category} at some point, "
                    f"and only {after} orders came in over the last 30 days against "
                    f"{before} the month before. The buyers are still on your list; the "
                    "orders stopped."
                ),
                suggested_action=(
                    f"Send the {members} customers who have bought {category} a reason to "
                    "come back — a restock note or a small offer on that category."
                ),
                source_url=source_url,
                confidence=2,
                urgency=2,
                action_payload={
                    "segment": {"key": "by_category", "params": params},
                    "members": members,
                    "offer_pct": 10,
                    "evidence": {
                        "orders_30d": after,
                        "orders_prior_30d": before,
                        "buyers": members,
                    },
                },
            )
        )
        # one category per run: four falling categories is a business problem to
        # talk about, not four separate campaign drafts to approve
        break
    return out


# ----------------------------------------------------------- competitor sites


# Lines that change on a page every single day and mean nothing: dates, view and
# stock counters, cart totals, cookie banners, rotating testimonials. Filtering
# them here rather than in the prompt is what stops a banner swap costing an LLM
# call per competitor per day.
_NOISE = re.compile(
    r"(cookie|newsletter|subscribe|follow us|copyright|all rights reserved"
    r"|©|privacy policy|terms of|sign in|log in|search|menu|share this"
    r"|\bviews?\b|\bviewing\b|people are looking|left in stock|cart\b"
    r"|posted on|updated on|read more|\bmin read\b)",
    re.IGNORECASE,
)
_DATEISH = re.compile(
    r"^\W*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}"
    r"|(mon|tue|wed|thu|fri|sat|sun)[a-z]*[ ,]"
    r"|(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.? \d{1,2})",
    re.IGNORECASE,
)
# What a material change looks like: a price, a percentage, or the vocabulary a
# shop uses when a product or an offer moves.
_MATERIAL = re.compile(
    r"(₹|rs\.?\s*\d|inr\s*\d|\$\s*\d|\b\d+\s*%\s*(off|discount)"
    r"|\b(price|priced|pricing|mrp|offer|discount|sale|deal|combo|bundle"
    r"|launch|launching|new arrival|now available|back in stock|out of stock"
    r"|sold out|free shipping|introducing)\b)",
    re.IGNORECASE,
)


def normalise_extract(text_body: str) -> str:
    """Collapse a page to the lines a diff should be taken over.

    Whitespace is squeezed and the known-churn lines are dropped *before* the
    hash, so a page whose only change is a cookie banner or a "posted on" stamp
    hashes identically to yesterday and never reaches the diff, let alone the
    model.
    """
    lines: list[str] = []
    for raw in text_body.splitlines():
        line = " ".join(raw.split())
        if len(line) < 3:
            continue
        if _NOISE.search(line) or _DATEISH.match(line):
            continue
        lines.append(line)
    return "\n".join(lines)[:EXTRACT_MAX_CHARS]


def material_changes(previous: str, current: str) -> list[str]:
    """Lines that are new since last time *and* look like product/price/offer.

    Two filters, both cheap, both before the LLM: what is new, then what of that
    is material. A copy rewrite on an About page produces new lines and no
    material ones, so it produces no insight and no model call — only the
    snapshot moves.
    """
    seen = set(previous.splitlines())
    fresh = [line for line in current.splitlines() if line not in seen]
    return [line for line in fresh if _MATERIAL.search(line)][:40]


async def competitor_sites(
    container: Any, tenant_id: UUID, competitors: list[str], categories: list[str]
) -> list[Candidate]:
    """Diff each listed competitor URL and describe only the material moves."""
    urls = [u for u in competitors if str(u).lower().startswith(("http://", "https://"))]
    if not urls:
        return []

    from app.services.growth.metering import meter

    out: list[Candidate] = []
    for url in urls[:MAX_COMPETITOR_URLS]:
        try:
            extract = await asyncio.wait_for(
                _fetch_extract(container, url), timeout=FETCH_TIMEOUT_SECONDS
            )
        except Exception:
            logger.info(
                "growth_insights_competitor_fetch_failed",
                tenant_id=str(tenant_id),
                url=url,
                exc_info=True,
            )
            continue
        if extract is None:
            continue
        await meter(tenant_id, "page_fetch", feature=FEATURE, url=url)

        digest = hashlib.sha256(extract.encode("utf-8")).hexdigest()
        snapshot = await container.growth.page_snapshots.get(tenant_id, url)
        previous = str((snapshot or {}).get("extract") or "")
        unchanged = snapshot is not None and str(snapshot.get("content_hash")) == digest
        # the snapshot is written whether or not anything material moved, so a
        # page that churns cosmetically does not re-diff against a stale copy
        await container.growth.page_snapshots.upsert(
            tenant_id, url, content_hash=digest, extract=extract
        )
        if snapshot is None or unchanged:
            # first sight of a page is a baseline, not news
            continue
        changes = material_changes(previous, extract)
        if not changes:
            continue
        candidate = await _describe_competitor_change(
            container, tenant_id, url, changes, categories
        )
        if candidate is not None:
            out.append(candidate)
    return out


async def _fetch_extract(container: Any, url: str) -> str | None:
    """One competitor page as normalised text, through the SSRF-safe door.

    ``container.page_fetcher`` is the only object in the codebase allowed to open
    a socket to a tenant-supplied URL — it validates every redirect hop and
    enforces the byte cap — so a competitor URL a tenant typed cannot become a
    request to an internal address.
    """
    from app.services.crawl.extract import extract_page

    fetcher = getattr(container, "page_fetcher", None)
    if fetcher is None:
        return None
    result = await fetcher.fetch(url, max_bytes=PAGE_MAX_BYTES)
    if int(getattr(result, "status", 0)) != 200 or not result.body:
        return None
    content_type = (result.headers or {}).get("content-type")
    page = extract_page(
        result.body,
        getattr(result, "final_url", url),
        content_type=content_type,
        collect_links=False,
    )
    return normalise_extract(page.markdown)


_COMPETITOR_SYSTEM = (
    "You read a diff of a competitor's web page for a small business owner. "
    "You answer only about what changed to a product, a price or an offer. "
    "If the changed lines are navigation, copy, blog or banner text, you "
    "answer material false and stop — a quiet diff is the expected outcome. "
    "Never invent a price, a product or a number that is not in the lines; "
    "every figure you write must appear in the lines verbatim, and the only "
    "URL you may name is the one you were given."
)

_COMPETITOR_PROMPT = """COMPETITOR PAGE: {url}
THE CUSTOMER'S OWN PRODUCT CATEGORIES: {categories}

LINES THAT ARE NEW ON THAT PAGE SINCE LAST TIME:
{changes}

Reply with JSON only:
{{"material": true|false,
  "subject": "<short stable name for what changed, e.g. 'price cut: running shoes'>",
  "summary": "<one line: what the competitor did>",
  "category": "<the ONE category from the customer's list this touches, or "">",
  "urgency": 1|2|3,
  "confidence": 1|2|3}}

material is false unless a product, a price or an offer actually moved."""


async def _describe_competitor_change(
    container: Any,
    tenant_id: UUID,
    url: str,
    changes: list[str],
    categories: list[str],
) -> Candidate | None:
    """Turn material diff lines into an insight, or into nothing."""
    verdict = await _ask_json(
        container,
        tenant_id,
        system=_COMPETITOR_SYSTEM,
        prompt=_COMPETITOR_PROMPT.format(
            url=url,
            categories=", ".join(categories) or "(none on file)",
            changes="\n".join(f"- {line}" for line in changes)[:4000],
        ),
        # Generous ceiling: Gemini 3 thinking (HIGH) shares the budget.
        max_tokens=20000,
    )
    if not verdict or not bool(verdict.get("material")):
        return None
    subject = str(verdict.get("subject") or "").strip()[:200]
    summary = str(verdict.get("summary") or "").strip()[:500]
    if not subject or not summary:
        return None
    category = str(verdict.get("category") or "").strip().lower()
    payload: dict[str, Any] = {"evidence": {"changed_lines": len(changes)}}
    why = (
        "This is a change on a competitor you asked us to watch, and it touches "
        "what you sell."
    )
    action = "Match or answer the move before your customers see only their price."
    if category and category in {c.lower() for c in categories}:
        payload["segment"] = {"key": "by_category", "params": {"category": category}}
        payload["offer_pct"] = 10
        why = (
            f"They moved on {category}, which is a category you sell and your "
            "customers have bought."
        )
        action = (
            f"Draft a campaign to the customers who have bought {category} before "
            "they see only the competitor's price."
        )
    return Candidate(
        type="competitor_site",
        subject=subject,
        summary=summary,
        why=why,
        suggested_action=action,
        source_url=url,
        confidence=_bucket(verdict.get("confidence")),
        urgency=_bucket(verdict.get("urgency")),
        action_payload=payload,
    )


# ------------------------------------------------------------------- the news


_NEWS_SYSTEM = (
    "You decide whether one news item changes anything for one specific small "
    "business. You are not a summariser: an article that does not change what "
    "this business should do this month is not relevant, however interesting. "
    "Regulation, GST and e-invoicing thresholds, budget changes, input costs, a "
    "competitor raising money or opening a location — those change things. "
    "General industry coverage does not. Every number, price and date you "
    "write must appear in the article extract verbatim; never invent a "
    "number, never name a source that was not supplied."
)

_NEWS_PROMPT = """THE BUSINESS
sells: {sells}
audience: {audience}
their product categories: {categories}
their customers on file: {customers}

THE ARTICLE
title: {title}
url: {url}
extract: {extract}

Reply with JSON only:
{{"relevant": true|false,
  "subject": "<short stable name for the thing that happened>",
  "summary": "<one line: what happened>",
  "why": "<one line: what it changes for THIS business, naming their category or customers>",
  "action": "<one line: the concrete thing to do about it>",
  "category": "<the ONE category from their list this touches, or "">",
  "urgency": 1|2|3,
  "confidence": 1|2|3}}

relevant is false unless you can write why and action about THIS business."""


async def news(
    container: Any,
    tenant_id: UUID,
    *,
    competitors: list[str],
    keywords: list[str],
    profile: dict[str, Any],
    categories: list[str],
    customer_count: int,
) -> list[Candidate]:
    """Competitor and industry news, read for consequence rather than listed.

    Both news types run the same shape and differ only in what was searched for,
    which is why they are one function: search, read, then decide whether this
    changes anything for this tenant given their products, segments and order
    history. Everything that survives is something the owner would have to act
    on; everything else is dropped here, before it is written and ranked low.
    """
    search = getattr(container, "web_search", None)
    if search is None:
        return []

    from app.services.growth.metering import meter

    queries: list[tuple[str, str]] = []
    for name in competitors[:3]:
        label = _domain_label(name)
        if label:
            queries.append(("competitor_news", f"{label} news"))
    for keyword in keywords[:3]:
        queries.append(("industry_news", f"{keyword} India regulation OR GST OR price"))

    out: list[Candidate] = []
    seen_urls: set[str] = set()
    for kind, query in queries[:MAX_NEWS_QUERIES]:
        try:
            hits = await asyncio.wait_for(
                search.search(query, max_results=MAX_NEWS_RESULTS),
                timeout=SEARCH_TIMEOUT_SECONDS,
            )
        except Exception:
            logger.info(
                "growth_insights_search_failed",
                tenant_id=str(tenant_id),
                query=query,
                exc_info=True,
            )
            continue
        await meter(tenant_id, "web_search", feature=FEATURE, query=query)

        for hit in hits:
            url = str(getattr(hit, "url", "") or "")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            verdict = await _ask_json(
                container,
                tenant_id,
                system=_NEWS_SYSTEM,
                prompt=_NEWS_PROMPT.format(
                    sells=str(profile.get("sells") or "")[:400],
                    audience=str(profile.get("audience") or "")[:400],
                    categories=", ".join(categories) or "(none on file)",
                    customers=customer_count,
                    title=str(getattr(hit, "title", "") or "")[:300],
                    url=url,
                    extract=str(getattr(hit, "content", "") or "")[:2000],
                ),
                # Generous ceiling: Gemini 3 thinking (HIGH) shares the budget.
                max_tokens=20000,
            )
            if not verdict or not bool(verdict.get("relevant")):
                continue
            candidate = _news_candidate(kind, url, verdict, categories, customer_count)
            if candidate is not None:
                out.append(candidate)
    return out


def _news_candidate(
    kind: str,
    url: str,
    verdict: dict[str, Any],
    categories: list[str],
    customer_count: int,
) -> Candidate | None:
    subject = str(verdict.get("subject") or "").strip()[:200]
    summary = str(verdict.get("summary") or "").strip()[:500]
    why = str(verdict.get("why") or "").strip()[:1000]
    action = str(verdict.get("action") or "").strip()[:1000]
    if not (subject and summary and why and action):
        return None
    payload: dict[str, Any] = {"evidence": {"customers": customer_count}}
    category = str(verdict.get("category") or "").strip().lower()
    if category and category in {c.lower() for c in categories}:
        payload["segment"] = {"key": "by_category", "params": {"category": category}}
        payload["offer_pct"] = 10
    return Candidate(
        type=kind,
        subject=subject,
        summary=summary,
        why=why,
        suggested_action=action,
        source_url=url,
        confidence=_bucket(verdict.get("confidence")),
        urgency=_bucket(verdict.get("urgency")),
        action_payload=payload,
    )


def _domain_label(value: str) -> str:
    """A competitor entry as something worth searching for.

    Tenants type competitors either as a name or as a URL; a search for
    "https://acme.example.com/shop news" returns nothing useful, so a URL is
    reduced to its registrable-looking host.
    """
    raw = value.strip()
    if not raw:
        return ""
    if "://" in raw:
        host = raw.split("://", 1)[1].split("/", 1)[0]
        host = host.removeprefix("www.")
        return host.split(":")[0]
    return raw[:100]


# ------------------------------------------------------------------- calendar


async def events(
    container: Any,
    tenant_id: UUID,
    *,
    categories: list[str],
    customer_count: int,
    profile: dict[str, Any],
) -> list[Candidate]:
    """What is coming, from the seeded calendar plus the variable categories.

    The fixed festival and sale calendar is table data seeded by migration 020,
    so no run ever pays a web search to learn when Diwali is. The search leg only
    covers what actually varies year to year — trade fairs, industry events,
    regulation and budget dates — and it writes what it finds back to
    ``growth_events`` so next year's run reads it as data too.
    """
    await _search_variable_events(container, tenant_id, profile)

    try:
        upcoming = await container.growth.events.upcoming(
            tenant_id, days=EVENT_LOOKAHEAD_DAYS
        )
    except Exception:
        logger.warning(
            "growth_insights_events_read_failed", tenant_id=str(tenant_id), exc_info=True
        )
        return []
    if not upcoming or customer_count <= 0:
        # an event with nobody to sell to is a calendar entry, not an insight
        return []

    today = datetime.now(UTC).date()
    out: list[Candidate] = []
    for row in upcoming[:3]:
        when = row.get("date")
        if not hasattr(when, "isoformat"):
            continue
        days = (when - today).days
        if days < 0:
            continue
        name = str(row.get("name") or "").strip()
        source = str(row.get("source_url") or "").strip()
        if not name or not source:
            continue
        segment_key = "repeat" if row.get("category") in ("festival", "sale") else "new"
        out.append(
            Candidate(
                type="event",
                subject=f"event:{name}:{when.isoformat()}",
                summary=f"{name} is {days} days away ({when.isoformat()}).",
                why=(
                    f"You have {customer_count} customers on file and the lead time to "
                    "get a campaign approved, templated and sent is most of a week."
                ),
                suggested_action=(
                    f"Draft the {name} campaign now so it is approved before the date, "
                    f"not after it."
                ),
                source_url=source,
                confidence=3,
                urgency=3 if days <= EVENT_URGENT_DAYS else 2,
                action_payload={
                    "segment": {"key": segment_key, "params": {}},
                    "members": customer_count,
                    "offer_pct": 10,
                    "event_name": name,
                    "evidence": {"customers": customer_count, "days_away": days},
                },
            )
        )
    return out


_EVENT_SYSTEM = (
    "You list dated business events for a small Indian business. Only real, "
    "dated things: trade fairs, industry exhibitions, filing deadlines, budget "
    "and regulation dates. Never festivals or shopping sales — those are "
    "already known. Every date and URL comes from the search results "
    "verbatim; never invent a date, never guess a year. Return an empty list "
    "rather than a vague date."
)

_EVENT_PROMPT = """THE BUSINESS sells: {sells}
SEARCH RESULTS:
{results}

Reply with JSON only:
{{"events": [{{"date": "YYYY-MM-DD", "name": "<short name>",
   "category": "trade_fair"|"industry"|"regulation",
   "source_url": "<the result url this came from>"}}]}}

Return [] rather than guessing a date. Only dates in the next six months."""


async def _search_variable_events(
    container: Any, tenant_id: UUID, profile: dict[str, Any]
) -> int:
    """Search only for what the seeded calendar cannot contain.

    Festivals and sale days are seeded and never searched. What is searched is
    the part that moves: this year's trade fairs, this year's filing and budget
    dates. Anything found is written to ``growth_events`` as a tenant-scoped row,
    so it becomes calendar data rather than being re-searched tomorrow.
    """
    search = getattr(container, "web_search", None)
    if search is None:
        return 0

    from app.services.growth.metering import meter

    sells = str(profile.get("sells") or "").strip()
    keywords = [str(k) for k in (profile.get("keywords") or [])][:1]
    topic = keywords[0] if keywords else sells
    if not topic:
        return 0
    queries = [
        f"{topic} India trade fair exhibition {datetime.now(UTC).year} dates",
        f"{topic} India compliance deadline {datetime.now(UTC).year} date",
    ][:MAX_EVENT_QUERIES]

    lines: list[str] = []
    for query in queries:
        try:
            hits = await asyncio.wait_for(
                search.search(query, max_results=3), timeout=SEARCH_TIMEOUT_SECONDS
            )
        except Exception:
            logger.info(
                "growth_insights_event_search_failed",
                tenant_id=str(tenant_id),
                exc_info=True,
            )
            continue
        await meter(tenant_id, "web_search", feature=FEATURE, query=query)
        for hit in hits:
            url = str(getattr(hit, "url", "") or "")
            if url:
                lines.append(
                    f"- {getattr(hit, 'title', '')} | {url} | "
                    f"{str(getattr(hit, 'content', ''))[:400]}"
                )
    if not lines:
        return 0

    verdict = await _ask_json(
        container,
        tenant_id,
        system=_EVENT_SYSTEM,
        prompt=_EVENT_PROMPT.format(sells=sells[:400], results="\n".join(lines)[:4000]),
        # Generous ceiling: Gemini 3 thinking (HIGH) shares the budget.
        max_tokens=20000,
    )
    found = (verdict or {}).get("events")
    if not isinstance(found, list) or not found:
        return 0

    today = datetime.now(UTC).date()
    horizon = today + timedelta(days=185)
    rows: list[dict[str, Any]] = []
    for item in found[:10]:
        if not isinstance(item, dict):
            continue
        try:
            when = datetime.strptime(str(item.get("date")), "%Y-%m-%d").date()
        except (TypeError, ValueError):
            continue
        if not today <= when <= horizon:
            continue
        category = str(item.get("category") or "")
        name = str(item.get("name") or "").strip()
        source = str(item.get("source_url") or "").strip()
        # a calendar row with no source is one nobody can check, and the event
        # watcher reads source_url straight onto the insight
        if category not in ("trade_fair", "industry", "regulation") or not name or not source:
            continue
        rows.append(
            {
                "date": when.isoformat(),
                "region": "IN",
                "category": category,
                "name": name,
                "source_url": source,
            }
        )
    if not rows:
        return 0
    try:
        return await container.growth.events.add_for_tenant(tenant_id, rows)
    except Exception:
        logger.warning(
            "growth_insights_event_write_failed", tenant_id=str(tenant_id), exc_info=True
        )
        return 0


# ----------------------------------------------------------------- LLM plumbing


async def _ask_json(
    container: Any,
    tenant_id: UUID,
    *,
    system: str,
    prompt: str,
    max_tokens: int,
) -> dict[str, Any] | None:
    """One JSON-answering model call, metered, tolerant of a fenced reply.

    Returns ``None`` on every failure path — no provider, a timeout, an
    unparseable answer. Every caller treats that as "no insight", which is the
    right outcome: a watcher that cannot read its evidence must produce nothing,
    not a guess.
    """
    from app.adapters.llm.base import Message
    from app.services.growth.metering import meter_llm

    try:
        llm = await container.agent_models.llm_for(tenant_id, AGENT_KEY, "utility")
    except Exception:
        logger.warning("growth_insights_no_provider", exc_info=True)
        return None
    if llm is None:
        return None
    try:
        completion = await asyncio.wait_for(
            llm.complete(
                [
                    Message(role="system", content=system),
                    Message(role="user", content=prompt),
                ],
                max_tokens=max_tokens,
            ),
            timeout=LLM_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.warning("growth_insights_llm_failed", exc_info=True)
        return None
    await meter_llm(
        tenant_id,
        completion.usage,
        feature=FEATURE,
        agent_key=AGENT_KEY,
        model=getattr(completion, "model", ""),
    )
    return parse_json_object(completion.text or "")


def parse_json_object(raw: str) -> dict[str, Any] | None:
    """The first JSON object in a model reply, fences and preamble tolerated."""
    body = raw.strip()
    if body.startswith("```"):
        body = body.split("```", 2)[1] if body.count("```") >= 2 else body[3:]
        body = body.removeprefix("json").strip()
    start = body.find("{")
    end = body.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(body[start : end + 1])
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None
