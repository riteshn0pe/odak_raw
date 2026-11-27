"""Business profile and growth settings: read, validate, write.

``growth_settings`` stores nine JSONB blocks, and this module is the only thing
that decides what is legal inside them. The schema is here rather than in the
database because these values are edited as a whole by one screen, gain fields
every stage, and are never query predicates — a CHECK constraint per field would
be a migration per product decision, buying a guarantee that a Pydantic model
already gives at the only door the data comes through.

The write rule is the ``agentConfig.ts`` lesson, enforced server-side: a PUT that
carries a partial block would, under a shallow merge, reset that block's other
fields to their defaults. So the service reads the stored blocks, applies the
caller's fields on top *inside each block*, validates the result, and hands whole
blocks to the repository. A caller can therefore send one field safely, and no
sibling is ever silently reset.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.adapters.postgres.growth_repo import SETTINGS_BLOCKS
from app.core.errors import ValidationError
from app.core.logging import get_logger
from app.domain.schedule import (
    DEFAULT_END_HOUR,
    DEFAULT_START_HOUR,
    DEFAULT_TIMEZONE,
    DEFAULT_WORKDAYS,
)

logger = get_logger(__name__)

# A = B2B outbound, B = D2C store, both = everything, "" = the owner has not
# chosen yet. Empty is a first-class value, never a default assumption: no
# gate may treat an unchosen type as B2B or D2C — unknown means applicable,
# and each agent is then judged purely on its facts.
TENANT_TYPES: frozenset[str] = frozenset({"", "A", "B", "both"})

# Features that can carry an approval level. Kept as a constant rather than a
# free-form dict so a typo in the settings screen cannot create a phantom feature
# whose level nothing ever reads — which would look exactly like level 0.
HITL_FEATURES: tuple[str, ...] = ("outbound_batch", "campaign", "review_ask")
DEFAULT_HITL_LEVEL = 1

# Agents whose final deliverable passes the grounding check. Advisory by default:
# a judge that blocks on its first day blocks good work too.
GROUNDED_AGENTS: tuple[str, ...] = (
    "growth_research",
    "growth_compose",
    "growth_replies",
    "growth_report",
    "growth_campaign",
    "growth_insights",
)

CUSTOMER_CHANNELS: tuple[str, ...] = ("whatsapp", "email")


class ProfileBlock(BaseModel):
    """Who this tenant is. Every downstream agent reads it.

    The identity fields below ``company_name`` … ``escalation_rule`` are the
    fact base (``FACT_FIELDS`` in app/services/agents/facts.py): the values the
    whoami agent fills from the tenant's website after a crawl and every
    ``{{company.*}}`` hole in an agent prompt renders from. Two fields are
    deliberately *not* here — ``languages`` (derived from the languages block,
    which is the authority on what a reply may be written in) and the ICP's
    presence is already the ``icp`` dict above.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    sells: str = Field(default="", max_length=2000)
    audience: str = Field(default="", max_length=2000)
    price_range: str = Field(default="", max_length=200)
    icp: dict[str, Any] = Field(default_factory=dict)
    website: str = Field(default="", max_length=500)
    competitors: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    tone: str = Field(default="formal", max_length=40)
    offer_min: int = 0
    offer_max: int = 0
    # Empty until the owner chooses. No assumed B2B/D2C: gates treat "" as
    # applicable and judge agents on facts alone.
    tenant_type: str = ""
    # The sender's real-world identity, required on every cold mail by CAN-SPAM
    # and expected under DPDP. Empty is allowed here and refused at compose time
    # rather than at save: an onboarding wizard that will not let you continue
    # without a postal address is an onboarding wizard people abandon, and the
    # honest place to enforce it is the moment before a stranger is contacted.
    postal_address: str = Field(default="", max_length=500)
    # Who outbound mail comes from. `sender_local` is the local part on the
    # tenant's verified sending subdomain — the domain itself lives on the
    # `resend_domain` integration row, because it has a verification status and
    # a profile field cannot carry one.
    sender_name: str = Field(default="", max_length=120)
    sender_local: str = Field(default="hello", max_length=64)

    # ---------------------------------------------------------------- facts
    # Filled by the whoami agent from the crawled site (or by hand); rendered
    # into agent prompts at invocation. Every one is optional in storage — an
    # agent whose required fact is empty shows as blocked on its card, which is
    # the honest state, rather than the save refusing here.
    company_name: str = Field(default="", max_length=200)
    legal_name: str = Field(default="", max_length=200)
    tagline: str = Field(default="", max_length=300)
    industry: str = Field(default="", max_length=120)
    city: str = Field(default="", max_length=120)
    region: str = Field(default="", max_length=120)
    country: str = Field(default="", max_length=120)
    services: str = Field(default="", max_length=2000)
    usp: str = Field(default="", max_length=1000)
    order_process: str = Field(default="", max_length=1000)
    certifications: list[str] = Field(default_factory=list)
    hours: str = Field(default="", max_length=300)
    contact_email: str = Field(default="", max_length=320)
    contact_phone: str = Field(default="", max_length=40)
    whatsapp_number: str = Field(default="", max_length=40)
    booking_link: str = Field(default="", max_length=500)
    delivery_areas: list[str] = Field(default_factory=list)
    delivery_time: str = Field(default="", max_length=200)
    policies: dict[str, str] = Field(default_factory=dict)
    brand_voice: str = Field(default="", max_length=400)
    avoid: str = Field(default="", max_length=1000)
    # Prompt copy only: the text agents are told to hand over to. Actual
    # escalation delivery lives in EscalationConfig, which has destinations and
    # triggers — this is what the *prompt* says, e.g. "the founder, Sameer".
    escalation_contact: str = Field(default="", max_length=200)
    escalation_rule: str = Field(default="", max_length=500)
    # Which fact came from where: {"sells": {"source": "crawl", "at": "…"}}.
    # Written by the whoami agent, read by the settings screen so "the agent
    # overwrote what I typed" is a question this dict can answer.
    facts_meta: dict[str, Any] = Field(default_factory=dict)
    # Bumped on every profile write; part of the agents' cache keys so a
    # re-crawl or an owner edit invalidates every rendered prompt without any
    # explicit fan-out.
    facts_version: int = 0

    @field_validator("tenant_type")
    @classmethod
    def _type(cls, value: str) -> str:
        if value not in TENANT_TYPES:
            raise ValueError(f"tenant_type must be one of {sorted(TENANT_TYPES)}")
        return value

    @field_validator("competitors", "keywords", "certifications", "delivery_areas")
    @classmethod
    def _short_list(cls, value: list[str]) -> list[str]:
        # 50 is generous for either and stops a paste of a whole spreadsheet
        # becoming a prompt the tenant pays for on every single agent run
        cleaned = [item.strip() for item in value if item and item.strip()]
        if len(cleaned) > 50:
            raise ValueError("at most 50 entries")
        return cleaned

    @field_validator("policies")
    @classmethod
    def _policies(cls, value: dict[str, str]) -> dict[str, str]:
        known = ("returns", "shipping", "warranty", "payment")
        unknown = set(value) - set(known)
        if unknown:
            raise ValueError(f"unknown policy key(s): {sorted(unknown)}")
        return {key: str(text or "").strip() for key, text in value.items() if str(text or "").strip()}

    @field_validator("facts_version")
    @classmethod
    def _version(cls, value: int) -> int:
        if value < 0:
            raise ValueError("facts_version must not be negative")
        return value

    @field_validator("website")
    @classmethod
    def _website(cls, value: str) -> str:
        # normalised, not fetched. The crawl that seeds RAG from this goes
        # through page_fetcher, which is where the SSRF rules live
        from app.domain.growth import normalise_domain

        if not value:
            return ""
        domain = normalise_domain(value)
        if not domain:
            raise ValueError("website must be a domain or URL")
        return domain


class HitlLevelsBlock(BaseModel):
    """Approval level per feature: 0 automatic, 1 per batch, 2 per message."""

    model_config = ConfigDict(extra="forbid")

    outbound_batch: int = DEFAULT_HITL_LEVEL
    campaign: int = DEFAULT_HITL_LEVEL
    review_ask: int = DEFAULT_HITL_LEVEL

    @field_validator("outbound_batch", "campaign", "review_ask")
    @classmethod
    def _level(cls, value: int) -> int:
        if value not in (0, 1, 2):
            raise ValueError("approval level must be 0, 1 or 2")
        return value


class SendCapsBlock(BaseModel):
    """Volume and timing. The scheduler reads nothing else."""

    model_config = ConfigDict(extra="forbid")

    daily_cap: int = Field(default=50, ge=0, le=5000)
    warmup_day: int = Field(default=0, ge=0, le=365)
    start_hour: int = Field(default=DEFAULT_START_HOUR, ge=0, le=23)
    end_hour: int = Field(default=DEFAULT_END_HOUR, ge=1, le=24)
    days: list[int] = Field(default_factory=lambda: list(DEFAULT_WORKDAYS))
    timezone: str = Field(default=DEFAULT_TIMEZONE, max_length=64)

    @field_validator("days")
    @classmethod
    def _days(cls, value: list[int]) -> list[int]:
        cleaned = sorted({d for d in value if 0 <= d <= 6})
        if not cleaned:
            raise ValueError("at least one working day is required")
        return cleaned

    @field_validator("timezone")
    @classmethod
    def _tz(cls, value: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"unknown timezone {value!r}") from None
        return value


class FollowUpBlock(BaseModel):
    """Day offsets and the ceiling. Three, then stop."""

    model_config = ConfigDict(extra="forbid")

    days: list[int] = Field(default_factory=lambda: [3, 7, 14])
    max_follow_ups: int = Field(default=3, ge=0, le=5)

    @field_validator("days")
    @classmethod
    def _offsets(cls, value: list[int]) -> list[int]:
        cleaned = sorted({d for d in value if 1 <= d <= 90})
        if len(cleaned) > 5:
            raise ValueError("at most 5 follow-up offsets")
        return cleaned


class OfferRangeBlock(BaseModel):
    """The band an agent may propose inside. Outside it, a human decides."""

    model_config = ConfigDict(extra="forbid")

    currency: str = Field(default="INR", max_length=8)
    discount_min_pct: int = Field(default=0, ge=0, le=100)
    discount_max_pct: int = Field(default=0, ge=0, le=100)

    def model_post_init(self, _context: Any) -> None:
        if self.discount_max_pct < self.discount_min_pct:
            raise ValueError("discount_max_pct must be >= discount_min_pct")


class LanguagesBlock(BaseModel):
    """Which languages a reply may be written in. English is always in the set.

    Not because English is special, but because it is the resolver's floor: a
    tenant whose list excludes it would have the resolver return a language the
    tenant said it does not support.
    """

    model_config = ConfigDict(extra="forbid")

    allowed: list[str] = Field(default_factory=lambda: ["en"])

    @field_validator("allowed")
    @classmethod
    def _allowed(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip().lower() for item in value if item and item.strip()]
        if "en" not in cleaned:
            cleaned.append("en")
        return sorted(set(cleaned))


class GroundingBlock(BaseModel):
    """Per-agent groundedness toggle and mode."""

    model_config = ConfigDict(extra="forbid")

    mode: str = "advisory"
    agents: dict[str, bool] = Field(
        default_factory=lambda: dict.fromkeys(GROUNDED_AGENTS, True)
    )

    @field_validator("mode")
    @classmethod
    def _mode(cls, value: str) -> str:
        if value not in ("advisory", "block"):
            raise ValueError("grounding mode must be 'advisory' or 'block'")
        return value

    @field_validator("agents")
    @classmethod
    def _agents(cls, value: dict[str, bool]) -> dict[str, bool]:
        unknown = set(value) - set(GROUNDED_AGENTS)
        if unknown:
            raise ValueError(f"unknown agent(s): {sorted(unknown)}")
        return {k: bool(v) for k, v in value.items()}


class ReportPrefsBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    weekly: bool = True
    monthly: bool = True
    on_demand: bool = True
    # Stage 3: the owner may ask for a report every morning instead of Mondays.
    # Off by default — a daily PDF nobody reads is cost without value.
    daily: bool = False


class ChannelOrderBlock(BaseModel):
    """Preference order for customer-facing channels."""

    model_config = ConfigDict(extra="forbid")

    order: list[str] = Field(default_factory=lambda: ["whatsapp", "email"])

    @field_validator("order")
    @classmethod
    def _order(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip().lower() for item in value if item and item.strip()]
        unknown = set(cleaned) - set(CUSTOMER_CHANNELS)
        if unknown:
            raise ValueError(f"unknown channel(s): {sorted(unknown)}")
        if not cleaned:
            raise ValueError("at least one channel is required")
        # a channel omitted entirely is a channel that never gets tried, which is
        # a legitimate choice — but a duplicate is a mistake
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("duplicate channel in order")
        return cleaned


BLOCK_MODELS: dict[str, type[BaseModel]] = {
    "profile": ProfileBlock,
    "hitl_levels": HitlLevelsBlock,
    "send_caps": SendCapsBlock,
    "follow_up": FollowUpBlock,
    "offer_range": OfferRangeBlock,
    "languages": LanguagesBlock,
    "grounding": GroundingBlock,
    "report_prefs": ReportPrefsBlock,
    "channel_order": ChannelOrderBlock,
}


def defaults() -> dict[str, Any]:
    """The settings a tenant has before they have touched anything.

    Materialised from the models rather than written out again, so the defaults
    the API returns and the defaults validation applies can never drift.
    """
    return {name: model().model_dump(mode="json") for name, model in BLOCK_MODELS.items()}


def merged(stored: dict[str, Any] | None) -> dict[str, Any]:
    """Stored settings with every missing field filled from the defaults.

    A row written before a block gained a field still reads back complete, which
    is what lets the agents treat every field as present instead of defending
    against None at each use.
    """
    out = defaults()
    if not stored:
        return out
    for name, model in BLOCK_MODELS.items():
        raw = stored.get(name)
        if not isinstance(raw, dict) or not raw:
            continue
        try:
            out[name] = model.model_validate({**out[name], **raw}).model_dump(mode="json")
        except Exception:
            # a stored block that no longer validates — a field removed in a
            # later stage, say — must not make the whole tenant unreadable. The
            # default is served and the row is left alone for a human to look at
            logger.warning("growth_settings_block_invalid", block=name, exc_info=True)
    return out


class ProfileService:
    """Read and write the tenant's growth settings."""

    def __init__(self, container: Any) -> None:
        self._container = container

    async def get(self, tenant_id: UUID) -> dict[str, Any]:
        stored = await self._container.growth.settings.get(tenant_id)
        return merged(stored)

    async def put(
        self,
        tenant_id: UUID,
        blocks: dict[str, Any],
        *,
        actor: str = "",
        source: str = "owner",
    ) -> dict[str, Any]:
        """Apply a partial edit and return the complete settings.

        ``blocks`` maps a block name to the fields being changed inside it. Each
        one is layered over what is stored, validated as a whole, and written as
        a whole — so sending ``{"send_caps": {"daily_cap": 80}}`` changes the cap
        and leaves the working hours exactly as they were.

        ``source`` records who edited the profile block: "owner" (the settings
        screen) or "whoami_agent" (the post-crawl extraction). It lands on the
        fact revision row, which is the evidence trail for the never-overwrite
        rule — and the one place that can prove it was kept.
        """
        unknown = set(blocks) - set(SETTINGS_BLOCKS)
        if unknown:
            raise ValidationError(f"unknown settings block(s): {sorted(unknown)}")

        current = await self.get(tenant_id)
        to_write: dict[str, Any] = {}
        for name, edit in blocks.items():
            if not isinstance(edit, dict):
                raise ValidationError(f"{name} must be an object")
            model = BLOCK_MODELS[name]
            try:
                validated = model.model_validate({**current[name], **edit})
            except Exception as exc:
                raise ValidationError(f"{name}: {exc}") from exc
            to_write[name] = validated.model_dump(mode="json")

        if not to_write:
            return current

        # A changed profile block is a fact revision: bump the version every
        # reader caches on, append the history row, and drop the cached fact
        # base so the next render — not the next TTL expiry — sees the change.
        if "profile" in to_write and to_write["profile"] != current["profile"]:
            to_write["profile"]["facts_version"] = int(current["profile"].get("facts_version") or 0) + 1
            await self._append_fact_revision(
                tenant_id, current["profile"], to_write["profile"], source=source
            )

        await self._container.growth.settings.upsert(tenant_id, to_write)
        await self._container.growth.audit.record(
            tenant_id,
            agent="owner",
            action="settings_updated",
            input={"blocks": sorted(to_write)},
            reason=f"updated by {actor}" if actor else "updated",
        )

        if "profile" in to_write:
            await self._invalidate_fact_cache(tenant_id)

        result = {**current, **to_write}
        website = str(result.get("profile", {}).get("website") or "")
        if website and website != str(current.get("profile", {}).get("website") or ""):
            await self._seed_from_website(tenant_id, website)
        return result

    async def _append_fact_revision(
        self,
        tenant_id: UUID,
        before: dict[str, Any],
        after: dict[str, Any],
        *,
        source: str,
    ) -> None:
        """One append-only row per profile change (tenant_fact_revisions).

        Best-effort by design: a revision row that fails to write must not
        refuse the settings edit it describes. The bump and the audit row are
        the durable record; this is the human-readable diff on top.
        """
        changed: dict[str, Any] = {}
        for key, value in after.items():
            if key in ("facts_meta", "facts_version"):
                continue
            if before.get(key) != value:
                changed[key] = {"from": before.get(key), "to": value}
        try:
            from sqlalchemy import text

            from app.core.db import tenant_scope

            async with tenant_scope(tenant_id) as session:
                await session.execute(
                    text(
                        """
                        INSERT INTO tenant_fact_revisions (tenant_id, revision, actor, profile, changed)
                        VALUES (
                            :tid,
                            (SELECT COALESCE(MAX(r.revision), 0) + 1
                               FROM tenant_fact_revisions r WHERE r.tenant_id = :tid),
                            :actor,
                            CAST(:profile AS jsonb),
                            CAST(:changed AS jsonb)
                        )
                        """
                    ),
                    {
                        "tid": tenant_id,
                        "actor": source if source in ("owner", "whoami_agent") else "owner",
                        "profile": json.dumps(after),
                        "changed": json.dumps(changed),
                    },
                )
        except Exception:
            logger.warning("fact_revision_append_failed", tenant_id=str(tenant_id), exc_info=True)

    async def _invalidate_fact_cache(self, tenant_id: UUID) -> None:
        """Drop the rendered-facts cache the agents package reads."""
        prompts = getattr(self._container, "agent_prompts", None)
        if prompts is not None:
            await prompts.invalidate_facts(tenant_id)

    async def _seed_from_website(self, tenant_id: UUID, domain: str) -> None:
        """Register the tenant's own site as a crawl source and start a wave.

        Goes through ``SourceService.create``, not the crawl engine directly. The
        service is where the plan gate, the source quota, the page ceiling, the
        seed SSRF check and the ``crawl_wave`` enqueue all live — reaching past
        it into the engine would be a second, weaker copy of five controls.

        Failure is logged, never raised. A profile save must succeed even when
        crawling is off, the plan excludes it, or the quota is spent; the owner
        can add the same source by hand from the Sources screen, where crawl
        failures are already surfaced properly.
        """
        settings = self._container.settings
        if not settings.CRAWL_ENABLED:
            logger.info(
                "growth_website_seed_skipped",
                tenant_id=str(tenant_id),
                reason="crawl_disabled",
            )
            return
        if await self._website_source_exists(tenant_id, domain):
            # the profile agent (P4) writes the website it just extracted from,
            # and the owner may have added the same site by hand from the
            # Sources screen. Either way a second source would double-crawl one
            # site for nothing
            logger.info(
                "growth_website_seed_skipped",
                tenant_id=str(tenant_id),
                reason="source_exists",
                domain=domain,
            )
            return
        try:
            await self._container.sources.create(
                tenant_id,
                kind="crawl",
                title=domain,
                seed_url=f"https://{domain}",
                agent_id=await self._default_agent_id(tenant_id),
            )
        except Exception:
            # a ValidationError lands here too, on purpose: it is "we did not
            # seed", and not a reason to reject the profile the owner just wrote
            logger.warning(
                "growth_website_seed_failed",
                tenant_id=str(tenant_id),
                domain=domain,
                exc_info=True,
            )

    async def _website_source_exists(self, tenant_id: UUID, domain: str) -> bool:
        """Does a website-kind source already crawl this exact site?

        Matched on the normalised seed domain rather than the raw seed URL, the
        same reduction every other domain comparison in growth uses — a tenant
        who pasted ``https://www.example.com/`` and one who pasted
        ``example.com`` have one site, not two.
        """
        from app.domain.growth import normalise_domain

        try:
            sources = await self._container.source_repo.list_sources(
                tenant_id, limit=200
            )
        except Exception:
            # a read failure must not block the seed; the duplicate the guard
            # would have caught is recoverable from the Sources screen
            logger.warning(
                "growth_website_seed_check_failed", tenant_id=str(tenant_id), exc_info=True
            )
            return False
        return any(
            str(source.get("kind") or "") in ("crawl", "sitemap", "links")
            and normalise_domain(str(source.get("seed_url") or "")) == domain
            for source in sources
        )

    async def _default_agent_id(self, tenant_id: UUID) -> UUID | None:
        """The tenant's default agent, so the seeded knowledge is scoped to it.

        None when there is no agent yet — a source with no agent is still valid
        and still ingests; it is simply visible to all of them, which is the
        better failure than refusing to seed at all.
        """
        from sqlalchemy import text

        from app.core.db import tenant_scope

        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT id FROM agents WHERE is_default "
                        "ORDER BY created_at LIMIT 1"
                    )
                )
            ).mappings().first()
        return UUID(str(row["id"])) if row is not None else None
