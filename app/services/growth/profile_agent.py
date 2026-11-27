"""The profile agent: fill the business profile from the tenant's own site.

The owner is never asked "what do you sell / who buys it". The crawl has
already read their website into the shared knowledge base; this agent
retrieves those pages, makes one schema-constrained extraction call, and
writes the discoverable fields through the existing profile service — the
same ``PUT`` path the (silenced, SF1) questionnaire used, not a new writer.

Three rules shape everything here:

* **Never overwrite the owner.** The agent fills empty fields and appends a
  run-context note; it does not silently replace what the owner wrote by
  hand. ``tone`` is a preference and ``tenant_type`` gates whole agents
  (outbound research turns off for type B), so neither is ever auto-written:
  a wrong guess there is worse than an empty field, and both are one setting
  away. When the site's signals disagree with the stored tenant type, the
  notification says so instead of changing it.
* **Retrieve, don't re-fetch.** The pages are already crawled, chunked and
  embedded; the agent searches them through the ordinary retrieval service,
  restricted to the website source's documents, rather than paying for and
  SSRF-checking a second fetch of a site the platform already holds.
* **Failure is a skip, never a crash.** The automatic trigger hangs off the
  crawl engine's ready verdict; a profile failure must not fail the wave that
  finished, and an unconfigured LLM must not fail the job.

**Trigger (audit G6):** there is no crawl completion event — crawls run as
self-re-enqueueing waves, and "done" is a verdict inside
``CrawlEngine._finish``. That verdict is where both gated and ungated crawls
land, and for a fresh website source it is reached right after review
approval applies the frontier and the pages are actually indexed, so it is
the moment the agent hooks: the engine enqueues one ``growth_profile`` job
carrying the source id. The on-demand endpoint
(``POST /v1/growth/profile/discover``) enqueues the same kind with ``force``,
which is also how a re-run after the owner edits fields stays safe — the
merge rule still refuses to touch anything the owner set.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

from app.core.errors import ValidationError
from app.core.logging import get_logger
from app.domain.growth import normalise_domain
from app.services.agents.render import MissingFacts

logger = get_logger(__name__)

AGENT_KEY = "growth_profile"
JOB_KIND = "growth_profile"
FEATURE = "profile"

# The extraction contract's version. v2 widened the schema from the nine
# decision fields to the full ProfileBlock fact base (identity, service area,
# contact details, policies) — the version names which shape a given run's
# JSON answered, for the run-context note and any future migration.
EXTRACTION_SCHEMA_VERSION = "v2"

# One extraction call. v2's schema asks for ~30 keys; a response cut mid-JSON
# fails parse_json_object and the whole run. 20000 is deliberately generous:
# Gemini 3 thinking (HIGH) shares the maxOutputTokens budget, and a starved
# run costs a retry while a generous ceiling costs nothing extra.
EXTRACTION_MAX_TOKENS = 20000
LLM_TIMEOUT_SECONDS = 90.0

# How much of the tenant's own site the extraction prompt may read. The crawl
# keeps whole pages; the profile needs the pitch, the about page and the
# contact block, which is a fraction of even a small site.
MAX_CONTEXT_CHARS = 14_000

# Retrieval width against the website's own documents only. Wide on purpose:
# the facets differ (offer vs audience vs address) and one query has to
# surface all three, so over-fetching and letting the model pick beats a
# second embedding call per facet.
RETRIEVAL_TOP_K = 16

# The kinds whose pages are the tenant's own site. `snippet` and `qa` are
# authored content with no frontier and never reach the trigger anyway.
WEBSITE_KINDS = ("crawl", "sitemap", "links")

_SYSTEM = (
    "You read a company's own website and extract its business profile — "
    "extraction, not description. You write only what the SOURCE supports; a "
    "field the source does not support is returned empty and named in "
    "not_found — never guessed, never inferred from the company's name or "
    "industry alone. You never fill tone, tenant_type or brand_voice: those "
    "are the owner's, and a wrong guess there blocks whole agents "
    "downstream. Prices are integers in rupees exactly as the site states "
    "them, 0 when the site states no price."
)

_PROMPT = """SOURCE — pages from this company's own website:
{context}
{search}Extract the company's business profile. Reply with exactly ONE JSON
object, no prose before or after, with these keys:

- "sells": what the company sells, one or two sentences
- "audience": who buys it, one sentence
- "price_range": the price band as the site states it (e.g. "Rs 500-2,000"), or ""
- "offer_min", "offer_max": integers, the lowest and highest product price the
  site states, in rupees; 0 when the site states no price
- "keywords": up to 10 short phrases a customer would search to find this business
- "competitors": up to 5 company names the source names as alternatives
- "icp": an object with any of "industry", "size", "location" the source supports
- "postal_address": the postal address as printed, or ""
- "sender_name": the person or company name outbound mail should come from, or ""
- "tenant_type_signal": "B2B", "D2C" or "" — only what the source shows
- "company_name": the trading name the site uses for itself, or ""
- "legal_name": the registered legal name, only if the site prints it (footer,
  terms, imprint), or ""
- "tagline": the site's own one-line tagline, or ""
- "industry": the industry the site places itself in, or ""
- "city", "region", "country": where the business operates, each only if stated
- "services": the services it lists, as one or two sentences
- "usp": its stated unique selling point, or ""
- "order_process": how a customer orders, as the site describes it, or ""
- "certifications": up to 10 certifications or credentials the site shows
- "delivery_areas": the areas it delivers to, up to 10 entries
- "hours": its opening or working hours as printed, or ""
- "contact_email", "contact_phone", "whatsapp_number": the contact details the
  site prints, each or ""
- "booking_link": the link for booking an appointment or order slot, or ""
- "delivery_time": the stated delivery turnaround, or ""
- "policies": an object with only the keys the site actually states content
  for: "returns", "shipping", "warranty", "payment", each a short summary
- "escalation_contact": the named person or role for complaints/escalations, or ""
- "not_found": the keys above the source did not support
"""

# Fields the agent may fill while empty. tone and tenant_type are deliberately
# absent (see the module docstring): preferences and agent gates are the
# owner's, and an empty string is the only reliable "the owner has not set
# this" signal those two do not have. brand_voice, avoid, escalation_rule and
# facts_version/facts_meta are owner-domain or written by the merge itself.
_FILLABLE_STR = (
    "sells",
    "audience",
    "price_range",
    "postal_address",
    "sender_name",
    "company_name",
    "legal_name",
    "tagline",
    "industry",
    "city",
    "region",
    "country",
    "services",
    "usp",
    "order_process",
    "hours",
    "contact_email",
    "contact_phone",
    "whatsapp_number",
    "booking_link",
    "delivery_time",
    "escalation_contact",
)
_FILLABLE_LISTS = ("keywords", "competitors", "certifications", "delivery_areas")

# The fields the owner's notification names as still missing. The v2 schema
# added many optional facets; naming all of them every run would drown the
# two that gate the swarm (what you sell, who buys it) in ones that do not.
_MISSING_FIELDS = (
    "sells",
    "audience",
    "price_range",
    "postal_address",
    "sender_name",
    "keywords",
    "offer_min",
    "offer_max",
)

_PRETTY = {
    "sells": "what you sell",
    "audience": "who buys it",
    "price_range": "price range",
    "postal_address": "postal address",
    "sender_name": "sender name",
    "keywords": "keywords",
    "competitors": "competitors",
    "icp": "customer profile",
    "offer_min": "lowest price",
    "offer_max": "highest price",
    "website": "website",
    "company_name": "company name",
    "legal_name": "legal name",
    "tagline": "tagline",
    "industry": "industry",
    "city": "city",
    "region": "region",
    "country": "country",
    "services": "services",
    "usp": "unique selling point",
    "order_process": "how customers order",
    "certifications": "certifications",
    "delivery_areas": "delivery areas",
    "hours": "opening hours",
    "contact_email": "contact email",
    "contact_phone": "contact phone",
    "whatsapp_number": "WhatsApp number",
    "booking_link": "booking link",
    "delivery_time": "delivery time",
    "policies": "policies",
    "escalation_contact": "escalation contact",
}


class ProfileAgentService:
    """Discovers the business profile. Writes only what the owner left empty."""

    def __init__(self, container: Any) -> None:
        self._c = container

    async def _system_prompt(self, tenant_id: UUID) -> str:
        """The persona, rendered from this tenant's facts at invocation.

        Only the persona is rendered: the JSON extraction contract (the exact
        key list the merge reads) lives in the user prompt and parse_json_object,
        both untouched here. Falls back to the module constant whenever the
        agent layer is absent or the tenant's template wants a fact the profile
        does not carry — the card names the missing fact; a profile fill must
        not break on one, least of all this agent, whose whole job is that the
        profile is still empty.
        """
        prompts = getattr(self._c, "agent_prompts", None)
        if prompts is None:
            return _SYSTEM
        try:
            return await prompts.system_for(tenant_id, AGENT_KEY)
        except MissingFacts:
            logger.warning(
                "agent_prompt_missing_facts",
                agent_key=AGENT_KEY,
                tenant_id=str(tenant_id),
            )
            return _SYSTEM

    async def run(self, tenant_id: UUID, payload: dict[str, Any]) -> dict[str, Any]:
        """One ``growth_profile`` job: retrieve, extract, merge, notify."""
        force = bool(payload.get("force"))
        settings = await self._c.growth_profile.get(tenant_id)
        profile = settings.get("profile") or {}

        source = await self._resolve_source(tenant_id, profile, payload)
        if source is None:
            return {"status": "skipped", "reason": "no crawled source for this tenant's own site"}
        source_id = UUID(str(source["id"]))
        domain = normalise_domain(str(source.get("seed_url") or ""))

        if not force and _discovery_done(profile):
            # the once-guard on the automatic trigger: a retrain or a second
            # site finishing must not re-bill the extraction. Re-discovery is
            # the on-demand endpoint's job, where the merge rule still holds
            logger.info(
                "growth_profile_already_filled", tenant_id=str(tenant_id)
            )
            return {"status": "skipped", "reason": "the profile is already filled"}

        context = await self._site_context(tenant_id, source_id)
        if not context:
            await self._note(
                tenant_id,
                settings,
                payload={"note": "no readable pages in the crawl", "website": domain},
            )
            return {"status": "skipped", "reason": "the crawl indexed no readable pages"}

        extracted = await self._extract(tenant_id, context, domain)
        if extracted is None:
            await self._note(
                tenant_id,
                settings,
                payload={"note": "extraction failed", "website": domain},
            )
            return {"status": "failed", "reason": "the extraction call failed"}

        # re-read before merging: the profile was read at the top of the run,
        # and the owner may have edited it during the retrieval + LLM window.
        # Merging into the stale copy would compute the edit against fields
        # the owner has since filled — a fresh read shrinks the race to the
        # put itself, and the merge rule does the rest
        settings = await self._c.growth_profile.get(tenant_id)
        profile = settings.get("profile") or {}

        edit, filled, missing = merge_extracted(profile, extracted, source_domain=domain)
        if edit:
            # provenance for this run's fills, on the same PUT: which fields
            # the crawl produced and the page they came from. Kept to what this
            # run filled — merged over whatever earlier runs or the owner's
            # imports recorded — because put() replaces dict fields whole
            seed_url = str(source.get("seed_url") or "") or domain
            edit["facts_meta"] = {
                **(profile.get("facts_meta") or {}),
                **{name: {"source": "crawl", "url": seed_url} for name in filled},
            }
            # through the profile service's PUT path — the one writer that
            # validates the whole block, records the audit row and seeds the
            # crawl (already deduped by _seed_from_website's origin check)
            try:
                await self._c.growth_profile.put(
                    tenant_id, {"profile": edit}, actor=AGENT_KEY, source="whoami_agent"
                )
            except ValidationError:
                # a clip the merge missed (or a later schema change) must not
                # crash the job on every retry — and put() refuses the whole
                # edit, not the one bad field, so the note below is the only
                # record the tenant gets of the run either way
                logger.warning(
                    "growth_profile_put_refused",
                    tenant_id=str(tenant_id),
                    exc_info=True,
                )
                return {
                    "status": "failed",
                    "reason": "the profile edit was refused by validation",
                }

        await self._note(
            tenant_id,
            settings,
            payload={
                "filled": filled,
                "missing": missing,
                "website": domain,
                "trigger": "on_demand" if force else "crawl_ready",
                "schema": EXTRACTION_SCHEMA_VERSION,
            },
        )
        await self._notify(tenant_id, filled, missing, extracted, profile)
        logger.info(
            "growth_profile_filled",
            tenant_id=str(tenant_id),
            filled=filled,
            missing=missing,
        )
        return {"status": "ok", "filled": filled, "missing": missing}

    # ------------------------------------------------------------- the source

    async def _resolve_source(
        self, tenant_id: UUID, profile: dict[str, Any], payload: dict[str, Any]
    ) -> dict[str, Any] | None:
        """The tenant's own website source, or None when this run is not about it.

        The engine enqueues a job for whatever source reached ready, because
        which crawl is the tenant's own site is a profile question and the
        engine must not reach into growth settings. This method answers it,
        on both the automatic and the on-demand path: a stored
        ``profile.website`` must match the source's seed domain, and when none
        is stored the source is adopted only if it is the tenant's single
        website source — with two or more, guessing which one is the business
        would fill the profile with a competitor's pitch.
        """
        web = await self._website_sources(tenant_id)

        source: dict[str, Any] | None = None
        requested = str(payload.get("source_id") or "")
        if requested:
            source = next((s for s in web if str(s["id"]) == requested), None)
            if source is None:
                return None

        website = normalise_domain(str(profile.get("website") or ""))
        if source is None:
            if website:
                source = next(
                    (s for s in web if normalise_domain(str(s.get("seed_url") or "")) == website),
                    None,
                )
            else:
                source = web[0] if len(web) == 1 else None
        if source is None:
            return None
        if website and normalise_domain(str(source.get("seed_url") or "")) != website:
            return None
        if not website and len(web) > 1:
            # the single-source rule holds for the engine's source_id path
            # too: the first of several crawls to finish must not get to
            # name itself the tenant's website
            return None
        return source

    async def _website_sources(self, tenant_id: UUID) -> list[dict[str, Any]]:
        """Every website-kind source, kind-filtered rather than truncated.

        ``list_sources`` pages by a limit like every other list; a mixed-kind
        listing would silently drop a website source past the cap. One call
        per website kind is three indexed reads and no drop.
        """
        sources: list[dict[str, Any]] = []
        for kind in WEBSITE_KINDS:
            sources.extend(
                await self._c.source_repo.list_sources(tenant_id, kind=kind, limit=200)
            )
        return sources

    # ------------------------------------------------------------- the inputs

    async def _site_context(self, tenant_id: UUID, source_id: UUID) -> str:
        """The source's indexed pages as prompt text, or "".

        Retrieval restricted to the source's document ids, so the extraction
        reads the tenant's own site and not the rest of the knowledge base —
        the same restriction any agent's retrieval scoping would apply.
        """
        from app.domain.agent_config import RetrievalConfig

        rows = await self._c.documents.list_documents(
            tenant_id, limit=60, source_id=source_id
        )
        document_ids = [UUID(str(row["id"])) for row in rows if str(row.get("status")) == "ready"]
        if not document_ids:
            return ""

        config = RetrievalConfig(top_k=RETRIEVAL_TOP_K, max_context_chunks=RETRIEVAL_TOP_K)
        try:
            result = await self._c.retrieval.search(
                tenant_id,
                "what this business sells, who its customers are, its products "
                "and prices, and its contact and address details",
                config,
                document_ids=document_ids,
            )
        except Exception:
            logger.warning(
                "growth_profile_retrieval_failed", tenant_id=str(tenant_id), exc_info=True
            )
            return ""
        if getattr(result, "is_empty", False) or not result.chunks:
            return ""
        context = str(result.context_block(MAX_CONTEXT_CHARS))
        # Second pass, contact-focused. One broad query ranks policy/terms
        # pages above the CONTACT US footers every product page carries, so
        # contact_phone and hours — which gate the inbound agent — were
        # missed while sitting in the knowledge base. Cheap (one embedding
        # call) and capped by the same char budget.
        try:
            contact = await self._c.retrieval.search(
                tenant_id,
                "contact us phone customer care working hours office address",
                config,
                document_ids=document_ids,
            )
        except Exception:
            logger.warning(
                "growth_profile_contact_retrieval_failed",
                tenant_id=str(tenant_id),
                exc_info=True,
            )
            return context
        if not getattr(contact, "is_empty", False) and contact.chunks:
            seen = {getattr(chunk, "chunk_id", None) for chunk in result.chunks}
            fresh = [
                chunk for chunk in contact.chunks
                if getattr(chunk, "chunk_id", None) not in seen
            ]
            if fresh:
                from app.services.retrieval.search import RetrievalResult

                extra = str(
                    RetrievalResult(chunks=fresh).context_block(MAX_CONTEXT_CHARS)
                )
                context = (context + "\n" + extra)[:MAX_CONTEXT_CHARS]
        return context

    async def _search_evidence(self, domain: str) -> str:
        """One web-search call for competitors, as a second SOURCE block.

        Optional by the same rule as everywhere else the search provider
        appears: unconfigured yields an empty string and the extraction runs
        on the site alone, which is the base case.
        """
        search = getattr(self._c, "web_search", None)
        if search is None or not domain:
            return ""
        try:
            results = await search.search(f"{domain} alternatives competitors", max_results=5)
        except Exception:
            logger.warning("growth_profile_web_search_failed", domain=domain, exc_info=True)
            return ""
        lines = [
            f"- {getattr(hit, 'title', '')} ({getattr(hit, 'url', '')}): "
            f"{str(getattr(hit, 'content', '') or '')[:200]}"
            for hit in results
        ]
        if not lines:
            return ""
        return (
            "\nSOURCE — web search results about this company's market:\n"
            + "\n".join(lines)
            + "\n"
        )

    # ----------------------------------------------------------- the one call

    async def _extract(self, tenant_id: UUID, context: str, domain: str) -> dict[str, Any] | None:
        """One schema-constrained extraction call, or None on every failure."""
        from app.adapters.llm.base import Message
        from app.services.growth.insights.watchers import parse_json_object
        from app.services.growth.metering import meter_llm

        search_block = await self._search_evidence(domain)
        prompt = _PROMPT.format(context=context, search=search_block)

        try:
            llm = await self._c.agent_models.llm_for(tenant_id, AGENT_KEY, "primary")
        except Exception:
            logger.warning("growth_profile_no_provider", exc_info=True)
            return None
        if llm is None:
            return None
        system = await self._system_prompt(tenant_id)
        try:
            completion = await asyncio.wait_for(
                llm.complete(
                    [
                        Message(role="system", content=system),
                        Message(role="user", content=prompt),
                    ],
                    max_tokens=EXTRACTION_MAX_TOKENS,
                ),
                timeout=LLM_TIMEOUT_SECONDS,
            )
        except Exception:
            logger.warning("growth_profile_llm_failed", tenant_id=str(tenant_id), exc_info=True)
            return None

        await meter_llm(
            tenant_id,
            completion.usage,
            feature=FEATURE,
            agent_key=AGENT_KEY,
            model=getattr(completion, "model", ""),
        )
        return parse_json_object(completion.text or "")

    # ---------------------------------------------------------- the surfaces

    async def _note(
        self, tenant_id: UUID, settings: dict[str, Any], *, payload: dict[str, Any]
    ) -> None:
        """Append one entry to the tenant's run-context day. Guarded, like
        every agent's scratchpad write: a context outage must not cost the
        tenant the profile the extraction just produced."""
        from app.services.growth.reports.windows import tenant_today

        try:
            await self._c.growth.run_context.append(
                tenant_id,
                local_date=tenant_today(settings),
                agent_key=AGENT_KEY,
                kind="note",
                payload=payload,
            )
        except Exception:
            logger.warning(
                "growth_profile_note_failed", tenant_id=str(tenant_id), exc_info=True
            )

    async def _notify(
        self,
        tenant_id: UUID,
        filled: list[str],
        missing: list[str],
        extracted: dict[str, Any],
        profile: dict[str, Any],
    ) -> None:
        """The owner's alert: what was filled, what was not, what to check.

        This message doubles as the review surface while the profile page is
        silenced (SF1) — it is where the owner learns the swarm filled the
        profile and what to set by hand. Delivered through the approvals
        fan-out, the same Telegram/WhatsApp/Slack destinations every other
        owner alert uses.
        """
        items: list[str] = []
        if filled:
            items.append("Filled: " + ", ".join(_PRETTY.get(f, f) for f in filled) + ".")
        if missing:
            items.append(
                "Could not find: " + ", ".join(_PRETTY.get(f, f) for f in missing) + "."
            )
        # tenant_type is never auto-written (a wrong guess gates whole agents);
        # the signal is surfaced for the owner to decide instead. "" means
        # the owner has not chosen — never presented as B2B or D2C.
        signal = str(extracted.get("tenant_type_signal") or "").upper()
        current = str(profile.get("tenant_type") or "")
        if signal == "D2C" and not current:
            items.append(
                "Your site reads direct-to-consumer — set the tenant type in "
                "Growth settings if you want the store pipeline."
            )
        elif signal == "B2B" and not current:
            items.append(
                "Your site reads business-to-business — set the tenant type in "
                "Growth settings if you want the outbound pipeline."
            )
        elif signal == "D2C" and current == "A":
            items.append(
                "Your site reads direct-to-consumer — tenant type is left as "
                "business-to-business; change it in Growth settings if that is wrong."
            )
        elif signal == "B2B" and current in ("B",):
            items.append(
                "Your site reads business-to-business — tenant type is left as "
                "direct-to-consumer; change it in Growth settings if that is wrong."
            )
        if not items:
            return
        try:
            await self._c.approvals.alert(
                tenant_id,
                summary="I filled your business profile from your website.",
                items=items,
            )
        except Exception:
            logger.warning(
                "growth_profile_notify_failed", tenant_id=str(tenant_id), exc_info=True
            )


# ------------------------------------------------------------------- helpers


def _discovery_done(profile: dict[str, Any]) -> bool:
    """True when the discoverable fields are all carrying a value."""
    return bool(
        str(profile.get("sells") or "").strip()
        and str(profile.get("audience") or "").strip()
        and (profile.get("keywords") or [])
    )


# ProfileBlock's own max_lengths, applied where the value enters the edit:
# an over-long extraction must be clipped, not refused — put() validates the
# whole edit, so one long string would take every other filled field down
# with it, deterministically, on every retry
_MAX_LEN = {
    "sells": 2000,
    "audience": 2000,
    "price_range": 200,
    "postal_address": 500,
    "sender_name": 120,
    "company_name": 200,
    "legal_name": 200,
    "tagline": 300,
    "industry": 120,
    "city": 120,
    "region": 120,
    "country": 120,
    "services": 2000,
    "usp": 1000,
    "order_process": 1000,
    "hours": 300,
    "contact_email": 320,
    "contact_phone": 40,
    "whatsapp_number": 40,
    "booking_link": 500,
    "delivery_time": 200,
    "escalation_contact": 200,
}

# ProfileBlock validates policy *keys* but caps no value length; a policy is a
# summary ("30-day returns, unused items"), and a page of terms pasted as one
# is padding the next render pays for on every agent run
_POLICY_MAX_LEN = 500


def merge_extracted(
    current: dict[str, Any],
    extracted: dict[str, Any],
    *,
    source_domain: str,
) -> tuple[dict[str, Any], list[str], list[str]]:
    """Layer an extraction over the stored profile, filling only what is empty.

    The never-overwrite rule, as one pure function so the test can hold it
    without a container: a field the owner set by hand — anything with a
    non-empty current value — is returned untouched, whatever the extraction
    says. Returns ``(edit, filled, missing)``: the profile-service edit, the
    fields it fills, and the discoverable fields still empty after the merge
    (what the owner's notification names).
    """
    edit: dict[str, Any] = {}
    filled: list[str] = []

    for field_name in _FILLABLE_STR:
        value = str(extracted.get(field_name) or "").strip()
        if value and not str(current.get(field_name) or "").strip():
            edit[field_name] = value[: _MAX_LEN[field_name]]
            filled.append(field_name)

    for field_name in _FILLABLE_LISTS:
        raw = extracted.get(field_name)
        if not isinstance(raw, list):
            continue
        items = [str(item).strip() for item in raw if str(item or "").strip()]
        # 50 is ProfileBlock's own ceiling; clipping here rather than letting
        # the put() validation refuse the whole edit for one long list
        items = items[:50]
        if items and not (current.get(field_name) or []):
            edit[field_name] = items
            filled.append(field_name)

    # the website is the input, not a discovery: whatever source the run was
    # about, that domain is what the profile should carry
    if source_domain and not str(current.get("website") or "").strip():
        edit["website"] = source_domain
        filled.append("website")

    for field_name in ("offer_min", "offer_max"):
        number = _as_int(extracted.get(field_name))
        if number > 0 and not _as_int(current.get(field_name)):
            edit[field_name] = number
            filled.append(field_name)

    raw_icp = extracted.get("icp")
    raw_current_icp = current.get("icp")
    current_icp: dict[str, Any] = (
        raw_current_icp if isinstance(raw_current_icp, dict) else {}
    )
    icp_edit: dict[str, str] = {}
    if isinstance(raw_icp, dict):
        for key, value in raw_icp.items():
            text = str(value or "").strip()
            if text and not str(current_icp.get(key) or "").strip():
                icp_edit[str(key)] = text[:200]
    if icp_edit:
        # the whole icp dict, owner keys first: ProfileService.put merges
        # top-level profile fields only, so a bare icp_edit here would REPLACE
        # the stored dict and delete every key the owner had set — the one
        # way this merge could destroy owner data rather than merely fail to
        # add to it
        edit["icp"] = {**current_icp, **icp_edit}
        filled.append("icp")

    # policies merge the same key-by-key way: only the four keys ProfileBlock
    # knows, only when the stored policy is empty, and the whole dict in the
    # edit so put()'s top-level merge cannot drop the owner's own policies
    raw_policies = extracted.get("policies")
    raw_current_policies = current.get("policies")
    current_policies: dict[str, Any] = (
        raw_current_policies if isinstance(raw_current_policies, dict) else {}
    )
    policy_edit: dict[str, str] = {}
    if isinstance(raw_policies, dict):
        for key in ("returns", "shipping", "warranty", "payment"):
            text = str(raw_policies.get(key) or "").strip()
            if text and not str(current_policies.get(key) or "").strip():
                policy_edit[key] = text[:_POLICY_MAX_LEN]
    if policy_edit:
        edit["policies"] = {**current_policies, **policy_edit}
        filled.append("policies")

    # sender_name fallback: outbound mail must come from someone (CAN-SPAM),
    # and the trading name is the honest sender when the site prints no
    # personal name. Without this, one empty field blocks compose, follow-up,
    # campaign and report together. Never overwrites the owner: only when
    # both stored and extracted sender_name are empty and a company_name is
    # known (newly filled or already stored).
    if not str(current.get("sender_name") or "").strip() and "sender_name" not in edit:
        fallback = str(
            edit.get("company_name") or current.get("company_name") or ""
        ).strip()
        if fallback:
            edit["sender_name"] = fallback[: _MAX_LEN["sender_name"]]
            filled.append("sender_name")

    # escalation_contact fallback: the customer-facing agents must hand over
    # to someone, and the site's own CONTACT US block is the business's
    # published support route — using it is reading, not guessing. Only when
    # the extraction named no dedicated escalation person and the owner set
    # nothing. Built purely from site-printed facts.
    if not str(current.get("escalation_contact") or "").strip() and "escalation_contact" not in edit:
        contact_parts = [
            str(edit.get("contact_email") or current.get("contact_email") or "").strip(),
            str(edit.get("contact_phone") or current.get("contact_phone") or "").strip(),
        ]
        contact_parts = [part for part in contact_parts if part]
        if contact_parts:
            edit["escalation_contact"] = " / ".join(contact_parts)[
                : _MAX_LEN["escalation_contact"]
            ]
            filled.append("escalation_contact")

    missing: list[str] = []
    for field_name in _MISSING_FIELDS:
        if field_name in edit or current.get(field_name):
            continue
        if field_name in ("offer_min", "offer_max"):
            if _as_int(extracted.get(field_name)) <= 0:
                missing.append(field_name)
        elif field_name == "keywords":
            if not isinstance(extracted.get("keywords"), list) or not [
                item for item in extracted.get("keywords") or [] if str(item or "").strip()
            ]:
                missing.append(field_name)
        elif not str(extracted.get(field_name) or "").strip():
            missing.append(field_name)

    return edit, filled, missing


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
