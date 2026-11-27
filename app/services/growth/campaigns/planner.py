"""Campaign planner: segment + offer + channel + text, drafted for approval.

The planner proposes; the owner disposes. It reads the tenant's profile
(tenant_type must include B — campaigns are D2C), the frozen segment via
``app/domain/segments.py``, the owner-set offer range (an offer outside it is
impossible by construction: the planner only ever receives the range), the
channel order, and the allowed languages. The draft text passes the grounding
helper; the frozen ``member_ids`` list is what the send reads, so the audience
cannot drift between approval and send.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.core.logging import get_logger
from app.domain import segments
from app.services.agents.render import MissingFacts

logger = get_logger(__name__)

AGENT_KEY = "growth_campaign"
FEATURE = "campaign"

TRIGGERS: tuple[str, ...] = ("owner", "insight", "event")

# One LLM call per language, bounded. More languages than this is a tenant
# misconfiguration, not a campaign.
MAX_LANGUAGES = 4
# Generous ceiling: Gemini 3 thinking (HIGH) shares the budget.
PLANNER_MAX_TOKENS = 20000

_SYSTEM = (
    "You write short promotional messages for a small store's existing "
    "customers — people who already bought. Plain, warm, one offer, one "
    "clear ask, at most 60 words, no shouting, no exclamation marks, no "
    "Markdown tables. The discount percent and the code appear exactly as "
    "given, never changed, never a second offer, never an invented deadline, "
    "never an invented product — if you are unsure of a fact, you leave it "
    "out."
)

_PROMPT = """STORE — what we sell, in our own words:
{knowledge}

OFFER: {offer_pct}% off with code {code}
SEGMENT: {segment_key} ({count} customers)
EXAMPLE CUSTOMER TAGS: {tags}

Write one message of at most 60 words in {language}, tone: {tone}.
Mention the discount percent and the code exactly. End with the store link {link}.
First output line: HEADLINE: <at most 50 characters>
Then a blank line, then the message body.
"""


class CampaignPlanner:
    """Drafts the campaign the owner approves. Sends nothing."""

    def __init__(self, container: Any) -> None:
        self._c = container

    async def _system_prompt(self, tenant_id: UUID, *, language: str) -> str:
        """The persona, rendered from this tenant's facts at invocation.

        Falls back to the module constant whenever the agent layer is absent or
        the tenant's template wants a fact the profile does not carry — the card
        names the missing fact; a campaign run must not break on it.
        """
        prompts = getattr(self._c, "agent_prompts", None)
        if prompts is None:
            return _SYSTEM
        try:
            return await prompts.system_for(
                tenant_id, AGENT_KEY, runtime={"language": language}
            )
        except MissingFacts:
            logger.warning(
                "agent_prompt_missing_facts",
                agent_key=AGENT_KEY,
                tenant_id=str(tenant_id),
            )
            return _SYSTEM

    async def draft(
        self, tenant_id: UUID, spec: dict[str, Any]
    ) -> dict[str, Any]:
        """Build the frozen draft: audience, offer, channels, texts.

        Raises ValidationError when the tenant may not run campaigns or the
        spec names an unknown segment.
        """
        from app.core.errors import ValidationError

        trigger = str(spec.get("trigger") or "owner")
        if trigger not in TRIGGERS:
            raise ValidationError(f"trigger must be one of {list(TRIGGERS)}")
        segment_key = str((spec.get("segment") or {}).get("key") or spec.get("segment_key") or "")
        if segment_key not in segments.SEGMENT_KEYS:
            raise ValidationError(
                f"unknown segment {segment_key!r}; expected one of {list(segments.SEGMENT_KEYS)}"
            )

        settings = await self._c.growth_profile.get(tenant_id)
        profile = settings.get("profile") or {}
        # Unchosen ("") counts as applicable — campaigns write to the
        # tenant's own customers, which needs no B2B/D2C assumption. Only an
        # explicit pure-B2B choice opts out.
        if str(profile.get("tenant_type") or "") not in ("", "B", "both"):
            raise ValidationError("campaigns need a store tenant (type B): connect a store first")

        offer_range = settings.get("offer_range") or {}
        lo = int(offer_range.get("discount_min_pct", 0) or 0)
        hi = int(offer_range.get("discount_max_pct", 0) or 0)
        # inside the owner-set range by construction: the requested value is
        # clamped, never trusted
        raw_offer = spec.get("offer")
        fallback = raw_offer.get("discount_pct", lo) if isinstance(raw_offer, dict) else lo
        asked_raw = spec.get("offer_pct", fallback)
        try:
            asked_int = int(str(asked_raw))
        except (TypeError, ValueError):
            asked_int = lo
        pct = max(lo, min(hi, asked_int))
        if hi <= 0:
            raise ValidationError("set an offer range on the growth profile first")

        customers = await self._c.growth.customers.all_for_segments(tenant_id)
        orders = await self._c.growth.orders.recent(tenant_id)
        now = datetime.now(UTC)
        params = dict((spec.get("segment") or {}).get("params") or {})
        members = segments.compute(segment_key, customers, orders, now, params)
        if not members:
            raise ValidationError(f"segment {segment_key!r} is empty right now")

        default_order = ["whatsapp", "email"]
        channel_order = list(
            (settings.get("channel_order") or {}).get("order") or default_order
        )
        allowed = list((settings.get("languages") or {}).get("allowed") or ["en"])[
            :MAX_LANGUAGES
        ]

        audience: list[dict[str, Any]] = []
        for member in members:
            channel = segments.channel_for(member, channel_order)
            if not channel:
                continue
            audience.append(
                {
                    "customer_id": str(member.get("id")),
                    "channel": channel,
                    "language": str(member.get("language") or "") or None,
                }
            )
        if not audience:
            raise ValidationError("no reachable customers in this segment (no consent or contact)")

        code = str(spec.get("discount_code") or "").strip().upper()[:24] or _new_code(
            str(spec.get("name") or segment_key)
        )
        texts = await self._texts(
            tenant_id, profile, segment_key, len(audience), pct, code, allowed, spec
        )

        verdict_meta: dict[str, Any] = {}
        if texts:
            sample = next(iter(texts.values()))
            verdict = await self._c.growth_grounding.check(
                tenant_id,
                agent_key=AGENT_KEY,
                draft=sample,
                evidence=_evidence(profile, segment_key, pct, lo=lo, hi=hi),
            )
            verdict_meta = verdict.as_meta()
            if not verdict.may_send:
                logger.info(
                    "growth_campaign_ungrounded",
                    tenant_id=str(tenant_id),
                    segment=segment_key,
                    state=verdict.state,
                )

        event_score = None
        if trigger == "event":
            event_score = await self.score_event(tenant_id, str(spec.get("event_name") or ""))

        return {
            "trigger": trigger,
            "segment": {
                "key": segment_key,
                "params": params,
                "member_ids": [a["customer_id"] for a in audience],
                "channels": {a["customer_id"]: a["channel"] for a in audience},
                "count": len(audience),
            },
            "offer": {
                "discount_pct": pct,
                "code": code,
                "currency": offer_range.get("currency", "INR"),
            },
            "channels": sorted({a["channel"] for a in audience}),
            "texts": texts,
            "audience_preview": audience[:10],
            "event_score": event_score,
            **verdict_meta,
        }

    async def score_event(self, tenant_id: UUID, event_name: str) -> dict[str, Any]:
        """Is this event worth a campaign for this tenant?

        Reads what similar past campaigns did from ``growth_campaigns.results``:
        average attributed revenue per event campaign. No past data means saying
        so, not inventing a score.
        """
        rows = await self._c.growth.campaigns.list(tenant_id, limit=100)
        past = [
            r
            for r in rows
            if str(r.get("trigger")) == "event"
            and isinstance(r.get("results"), dict)
            and r["results"]
        ]
        if not past:
            return {
                "event": event_name,
                "verdict": "unknown",
                "reason": "no past event campaigns to compare against yet",
            }
        revenues = [
            int((r.get("results") or {}).get("revenue_micros") or 0) for r in past
        ]
        avg = sum(revenues) // len(revenues) if revenues else 0
        return {
            "event": event_name,
            "verdict": "worth_it" if avg > 0 else "mixed",
            "past_event_campaigns": len(past),
            "avg_revenue_micros": avg,
            "reason": f"{len(past)} past event campaign(s), avg attributed revenue {avg} micros",
        }

    # ---------------------------------------------------------------- private

    async def _texts(
        self,
        tenant_id: UUID,
        profile: dict[str, Any],
        segment_key: str,
        count: int,
        pct: int,
        code: str,
        allowed: list[str],
        spec: dict[str, Any],
    ) -> dict[str, str]:
        from app.adapters.llm.base import Message
        from app.domain.growth import resolve_language

        knowledge = str(profile.get("sells") or "")[:1000]
        tone = str(profile.get("tone") or "formal")
        link = str(profile.get("website") or "")
        if link:
            link = (
                f"https://{link}?utm_source=campaign"
                f"&utm_medium={segment_key}&utm_campaign={code}"
            )
        tags = _example_tags(spec)
        texts: dict[str, str] = {}
        llm = await self._c.agent_models.llm_for(tenant_id, AGENT_KEY, "primary")
        for language in allowed:
            resolved = resolve_language(None, language, None, allowed)
            system = await self._system_prompt(tenant_id, language=resolved)
            prompt = _PROMPT.format(
                knowledge=knowledge or "(a local store)",
                offer_pct=pct,
                code=code,
                segment_key=segment_key,
                count=count,
                tags=tags,
                language=resolved,
                tone=tone,
                link=link or "(store link)",
            )
            try:
                completion = await llm.complete(
                    [
                        Message(role="system", content=system),
                        Message(role="user", content=prompt),
                    ],
                    max_tokens=PLANNER_MAX_TOKENS,
                )
            except Exception:
                logger.warning(
                    "growth_campaign_llm_failed",
                    tenant_id=str(tenant_id),
                    language=resolved,
                    exc_info=True,
                )
                continue
            from app.services.growth.metering import meter_llm

            await meter_llm(
                tenant_id,
                completion.usage,
                feature=FEATURE,
                agent_key=AGENT_KEY,
                model=getattr(completion, "model", ""),
            )
            body = (completion.text or "").strip()
            if body:
                texts[resolved] = body[:2000]
        return texts


def _new_code(name: str) -> str:
    """A unique, human-readable discount code from the campaign name."""
    import random
    import re
    import string

    stem = re.sub(r"[^A-Z0-9]", "", name.upper())[:8] or "OFFER"
    suffix = "".join(random.choice(string.ascii_uppercase + string.digits) for _ in range(4))  # noqa: S311 - a coupon code, not a secret
    return f"{stem}-{suffix}"


def _evidence(
    profile: dict[str, Any], segment_key: str, pct: int, *, lo: int, hi: int
) -> str:
    """What the judge checks the draft against.

    Two different ranges live in this draft and they are not interchangeable:
    ``offer_min``/``offer_max`` are product *prices* in rupees (profile facts,
    written by the profile agent), while ``lo``/``hi`` are the owner's
    *discount* clamp from ``growth_settings.offer_range``, which is what `pct`
    was actually clamped against. Labelling the price bounds as the offer range
    told the judge the campaign was discounting 250–1400%.
    """
    return (
        f"Store sells: {profile.get('sells', '')}\n"
        f"Segment: {segment_key}\nOffer: {pct}% off\n"
        f"Allowed discount range: {lo}-{hi}%\n"
        f"Product price range: {profile.get('offer_min', '')}-{profile.get('offer_max', '')}"
    )


def _example_tags(spec: dict[str, Any]) -> str:
    tags = (spec.get("segment") or {}).get("params", {}).get("category", "")
    return str(tags or "general")
