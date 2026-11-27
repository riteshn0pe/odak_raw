"""The research agent: turn a business profile into a queue of real leads.

The pipeline is search → fetch → extract → dedupe → enrich → verify → summarise
→ write → ask, and every step exists to make the step after it cheaper or the
final mail truer:

* **search** finds companies, from the tenant's connected lead database when
  they have one and from web search otherwise. Web-only is the base case, not a
  degraded mode.
* **fetch and extract** read the company's *own* site through ``page_fetcher``
  — the one SSRF-safe door — because "one specific reason they may need you" has
  to come from something they wrote, not from something a model imagined.
* **dedupe** is ``lead_uid`` plus ``dedupe_lead`` from the domain layer, so a
  company found twice through two sources is one row with the better fields.
* **freshness** is ``research_runs``: a company researched inside the window is
  skipped before any paid call, which is what stops a second run from
  re-billing Apollo for last week's work.
* **enrich and verify** cost credits and are therefore last, and only for
  candidates that survived everything above.
* **summarise** is one LLM call per lead through ``modelconfig.llm_for`` —
  never ``container.llm`` — and the result passes the grounding judge before it
  is stored.
* **ask** hands the whole batch to the HITL approval component. Research is not
  a money action and needs no approval of its own; nothing may be *mailed*
  until the owner has seen the batch.

Nothing in this module sends anything. The only outbound side effect is the
approval request, and the batch stays held until a human decides.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from app.core.logging import get_logger
from app.domain.growth import (
    dedupe_lead,
    is_suppressed,
    lead_uid,
    normalise_domain,
    normalise_email,
    normalise_phone,
)
from app.domain.ports import LeadCandidate
from app.services.agents.render import MissingFacts

logger = get_logger(__name__)

AGENT_KEY = "growth_research"
RESEARCH_JOB = "growth_research"
FEATURE = "outbound"

# A company researched inside this window is never researched again. Long enough
# that a weekly run costs nothing the second week, short enough that a company
# that has since started hiring, moved, or launched a product is looked at again.
FRESHNESS_DAYS = 30

# Hard ceiling on one run, whatever the caller asked for. The bound is the
# callback's wall clock and the tenant's credit balance, not the caller's
# optimism: each surviving candidate costs a page fetch, an enrichment call, a
# verification call and an LLM summary.
MAX_LEADS_PER_RUN = 25

# How many raw candidates to consider before filtering. Search and dedup throw
# most of them away, so asking for a multiple of the target is what makes a
# 25-lead run actually produce 25 leads.
CANDIDATE_MULTIPLE = 3

# How long one search is claimed for. Comfortably longer than QStash's retry
# ladder, so a redelivered job cannot re-buy the same company search — and far
# shorter than FRESHNESS_DAYS, which is a different question about a different
# subject (a company, not a job).
RESEARCH_CLAIM_TTL_SECONDS = 30 * 60

# Bytes of a company's homepage worth reading. Their pitch is above the fold;
# past this is navigation, footers and a cookie banner.
MAX_PAGE_BYTES = 400_000
MAX_PAGE_CHARS = 6000

# Words of "why they may need us". Two sentences. A long research summary is a
# model filling space, and it is what a cold mail then quotes at a stranger.
# Generous ceiling: Gemini 3 thinking (HIGH) shares the budget, and the
# provider floors every call at 20000 anyway.
SUMMARY_MAX_TOKENS = 20000

_SUMMARY_SYSTEM = (
    "You research a prospective customer for a business. You write at most two "
    "sentences, only what the SOURCE supports — never guessing a company's "
    "size, revenue, needs or plans, and never inventing a company, a person "
    "or an email address. If the source supports no reason they might need "
    "what we sell, you reply with exactly: NO BASIS"
)

_SUMMARY_PROMPT = """We sell: {sells}
To: {audience}

SOURCE — this company's own website text:
{page}

Write at most two sentences saying what this company does and one specific
reason, taken from the SOURCE, that they might need what we sell. Name the thing
from their own site that makes you say it. If the source supports no such
reason, reply with exactly: NO BASIS
"""

NO_BASIS = "NO BASIS"


class ResearchService:
    """Finds, researches and queues leads. Sends nothing."""

    def __init__(self, container: Any) -> None:
        self._c = container

    async def _system_prompt(self, tenant_id: UUID) -> str:
        """The persona, rendered from this tenant's facts at invocation.

        Falls back to the module constant whenever the agent layer is absent
        or the tenant's template wants a fact the profile does not carry —
        the card names the missing fact; the run must not break on it.
        """
        prompts = getattr(self._c, "agent_prompts", None)
        if prompts is None:
            return _SUMMARY_SYSTEM
        try:
            return await prompts.system_for(tenant_id, AGENT_KEY)
        except MissingFacts:
            logger.warning(
                "agent_prompt_missing_facts",
                agent_key=AGENT_KEY,
                tenant_id=str(tenant_id),
            )
            return _SUMMARY_SYSTEM

    async def run(self, tenant_id: UUID, payload: dict[str, Any]) -> dict[str, Any]:
        """One ``growth_research`` job: a batch of leads, then an approval ask."""
        settings = await self._c.growth_profile.get(tenant_id)
        profile = settings.get("profile") or {}

        if not _is_outbound_tenant(profile):
            # type B (D2C store) tenants have no outbound loop. Refusing here
            # rather than at the send is what keeps a mis-clicked button from
            # spending a store owner's credits on B2B lead data
            logger.info("growth_research_skipped_tenant_type", tenant_id=str(tenant_id))
            return {"status": "skipped", "reason": "this tenant is not set up for outbound"}

        limit = max(1, min(int(payload.get("limit") or MAX_LEADS_PER_RUN), MAX_LEADS_PER_RUN))
        query = str(payload.get("query_seed") or "").strip() or _icp_query(profile)
        if not query:
            return {
                "status": "skipped",
                "reason": "the business profile has no ICP to search on yet",
            }

        if not await self._claim(tenant_id, query):
            # a redelivered callback. The freshness window protects the paid
            # *per-candidate* calls, but the company search itself runs before any
            # of that and would be bought again — so the claim is what stops a
            # retried job re-billing the search it already paid for
            logger.info(
                "growth_research_already_running", tenant_id=str(tenant_id), query=query[:80]
            )
            return {"status": "skipped", "reason": "this search already ran recently"}

        candidates = await self._discover(tenant_id, query, limit * CANDIDATE_MULTIPLE, profile)
        if not candidates:
            await self._c.growth.research.record(
                tenant_id, query=query, source="search", results={"found": 0}
            )
            await self._note(
                tenant_id,
                settings,
                kind="note",
                payload={
                    "query": query,
                    "candidates": 0,
                    "leads": 0,
                    "note": "no companies found for this search",
                },
            )
            return {"status": "ok", "found": 0, "leads": 0, "reason": "no companies found"}

        fresh = await self._survivors(tenant_id, candidates, limit)
        leads: list[dict[str, Any]] = []
        for candidate in fresh:
            try:
                lead = await self._research_one(tenant_id, candidate, profile)
            except Exception:
                # one unreachable site or one refused enrichment must not cost
                # the tenant the other twenty-four
                logger.warning(
                    "growth_research_candidate_failed",
                    tenant_id=str(tenant_id),
                    domain=candidate.domain,
                    exc_info=True,
                )
                continue
            if lead is not None:
                leads.append(lead)

        await self._c.growth.research.record(
            tenant_id,
            query=query,
            source="mixed",
            results={"candidates": len(candidates), "researched": len(leads)},
            lead_ids=[UUID(str(lead["id"])) for lead in leads],
        )
        await self._note(
            tenant_id,
            settings,
            payload={
                "query": query,
                "candidates": len(candidates),
                "leads": len(leads),
                "companies": [_label(lead) for lead in leads[:5]],
            },
        )

        if not leads:
            return {"status": "ok", "found": len(candidates), "leads": 0}

        approval = await self._ask(tenant_id, leads)
        return {
            "status": "ok",
            "found": len(candidates),
            "leads": len(leads),
            "approval_id": str(approval.get("id") or ""),
            "approval_status": approval.get("status"),
        }

    # ------------------------------------------------------- run context

    async def _note(
        self,
        tenant_id: UUID,
        settings: dict[str, Any],
        *,
        payload: dict[str, Any],
        kind: str = "finding",
    ) -> None:
        """Append one entry to the tenant's run-context day.

        Guarded, because the scratchpad must never fail the paid work that
        produced the finding: a context-write outage costs the report a
        sentence, not the tenant their leads. The day key comes from the
        profile timezone so this run and the report land on the same date
        however close to midnight they run.
        """
        from app.services.growth.reports.windows import tenant_today

        try:
            await self._c.growth.run_context.append(
                tenant_id,
                local_date=tenant_today(settings),
                agent_key=AGENT_KEY,
                kind=kind,
                payload=payload,
            )
        except Exception:
            logger.warning(
                "growth_research_note_failed", tenant_id=str(tenant_id), exc_info=True
            )

    # ------------------------------------------------------------- discovery

    async def _discover(
        self, tenant_id: UUID, query: str, want: int, profile: dict[str, Any]
    ) -> list[LeadCandidate]:
        """Companies from the tenant's lead database, then from web search.

        Both, not either: Apollo knows companies that have no findable website
        and web search knows companies too small to be in Apollo, and the Indian
        SME market this launches into is full of both.
        """
        found: list[LeadCandidate] = []
        location = str(profile.get("icp", {}).get("location") or "")

        async with self._c.growth_providers.lead_provider(tenant_id, "apollo") as apollo:
            if apollo is not None:
                results = await apollo.search_companies(
                    query, limit=want, location=location
                )
                if results:
                    await self._meter_credits(
                        tenant_id, "apollo_credit", len(results), action="search_companies"
                    )
                found.extend(results)

        if len(found) < want:
            found.extend(await self._web_candidates(query, want - len(found)))
        return found

    async def _web_candidates(self, query: str, want: int) -> list[LeadCandidate]:
        """Search results, read as companies.

        The search provider is optional — an unconfigured one yields nothing and
        the run falls back to whatever the lead database returned, exactly as an
        unconfigured reranker degrades retrieval to fusion order.
        """
        search = getattr(self._c, "web_search", None)
        if search is None or want <= 0:
            return []
        try:
            results = await search.search(query, max_results=want)
        except Exception:
            logger.warning("growth_research_web_search_failed", exc_info=True)
            return []

        out: list[LeadCandidate] = []
        for hit in results:
            domain = normalise_domain(getattr(hit, "url", ""))
            if not domain:
                continue
            out.append(
                LeadCandidate(
                    company=str(getattr(hit, "title", "") or domain),
                    domain=domain,
                    description=str(getattr(hit, "content", ""))[:1000],
                    source="web_search",
                    raw={"url": getattr(hit, "url", "")},
                )
            )
        return out

    async def _survivors(
        self, tenant_id: UUID, candidates: list[LeadCandidate], limit: int
    ) -> list[LeadCandidate]:
        """Drop the duplicates, the recently-researched and the suppressed.

        All three filters run *before* any paid call, which is the whole reason
        they are one method: a freshness check that happened after enrichment
        would save nothing worth saving.
        """
        seen: set[str] = set()
        unique: list[LeadCandidate] = []
        for candidate in candidates:
            subject = _subject(candidate)
            if not subject or subject in seen:
                continue
            seen.add(subject)
            unique.append(candidate)

        since = datetime.now(UTC) - timedelta(days=FRESHNESS_DAYS)
        already = await self._c.growth.research.fresh_subjects(
            tenant_id, [_subject(c) for c in unique], since
        )
        suppressions = await self._c.growth.consent.suppressions(tenant_id)

        out: list[LeadCandidate] = []
        for candidate in unique:
            if _subject(candidate) in already:
                continue
            address = normalise_email(candidate.email) or normalise_phone(candidate.phone)
            if address and is_suppressed(
                suppressions, "email" if "@" in address else "whatsapp", address
            ):
                continue
            out.append(candidate)
            if len(out) >= limit:
                break
        return out

    # ------------------------------------------------------------ one lead

    async def _research_one(
        self, tenant_id: UUID, candidate: LeadCandidate, profile: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Read the company's site, find a contact, write one lead row."""
        page = await self._read_site(candidate.domain)
        candidate = await self._enrich(tenant_id, candidate)

        summary, links = await self._summarise(tenant_id, candidate, profile, page)
        candidate = await self._verify_email(tenant_id, candidate)

        try:
            uid = lead_uid(
                str(tenant_id),
                phone=candidate.phone,
                email=candidate.email,
                domain=candidate.domain,
            )
        except ValueError:
            # nothing identifying survived enrichment. A lead we cannot name is
            # a lead we can never dedupe, and one row per re-run is worse than
            # no row at all
            return None

        incoming: dict[str, Any] = {
            "uid": uid,
            "company": candidate.company,
            "person": candidate.person,
            "role": candidate.role,
            "email": normalise_email(candidate.email),
            "phone": normalise_phone(candidate.phone),
            "domain": normalise_domain(candidate.domain),
            "source": candidate.source or "research",
            "research_summary": summary,
            "research_links": links,
            "status": "researched",
        }

        existing = await self._c.growth.leads.get_by_uid(tenant_id, uid)
        merged = dedupe_lead(existing, incoming) if existing else incoming
        # dedupe_lead protects a lead that has moved past `researched`; a company
        # we are already mailing must not be dragged back into a fresh batch
        if existing and merged.get("status") != "researched":
            return None
        merged["uid"] = uid

        row: dict[str, Any] = await self._c.growth.leads.upsert(tenant_id, merged)
        # CRM push is queued, never blocking this path (on_lead_changed swallows all errors)
        await self._c.growth_crm.on_lead_changed(tenant_id, UUID(str(row["id"])))
        await self._c.growth.audit.record(
            tenant_id,
            agent=AGENT_KEY,
            action="lead_researched",
            input={"domain": candidate.domain, "source": candidate.source},
            output={"lead_id": str(row.get("id")), "has_email": bool(merged["email"])},
            reason=summary[:500] or "no supported reason found; contact stored only",
        )
        return row

    async def _read_site(self, domain: str) -> str:
        """The company's homepage, as text, or "".

        Through ``page_fetcher`` and nothing else. That is not a style rule: the
        domain came out of a search result or a vendor's database, which makes
        it tenant-adjacent input, and ``urlguard`` is what stops a crafted
        listing from turning this into an internal port scan.
        """
        domain = normalise_domain(domain)
        if not domain:
            return ""
        fetcher = getattr(self._c, "page_fetcher", None)
        if fetcher is None:
            return ""
        try:
            result = await fetcher.fetch(f"https://{domain}/", max_bytes=MAX_PAGE_BYTES)
        except Exception:
            logger.info("growth_research_fetch_failed", domain=domain, exc_info=True)
            return ""
        if result.status >= 400 or not result.body:
            return ""
        try:
            from app.services.crawl.extract import extract_page

            page = extract_page(
                result.body,
                result.final_url,
                content_type=result.headers.get("content-type"),
                collect_links=False,
            )
        except Exception:
            logger.info("growth_research_extract_failed", domain=domain, exc_info=True)
            return ""
        return page.markdown[:MAX_PAGE_CHARS]

    async def _enrich(self, tenant_id: UUID, candidate: LeadCandidate) -> LeadCandidate:
        """Put a name and an address on a company, if a vendor knows one.

        Apollo first, Hunter second, and it stops at the first hit: both would be
        two credits for one contact, and one named person per small company is
        the correct outreach volume anyway.
        """
        if candidate.email and candidate.person:
            return candidate
        domain = normalise_domain(candidate.domain)
        if not domain:
            return candidate

        for vendor, kind in (("apollo", "apollo_credit"), ("hunter", "hunter_credit")):
            async with self._c.growth_providers.lead_provider(tenant_id, vendor) as provider:
                if provider is None:
                    continue
                try:
                    found = await provider.enrich_contact(
                        domain=domain, company=candidate.company
                    )
                except Exception:
                    logger.warning(
                        "growth_enrich_failed", vendor=vendor, domain=domain, exc_info=True
                    )
                    continue
                await self._meter_credits(tenant_id, kind, 1, action="enrich_contact")
                if found is None:
                    continue
                return _merge_candidate(candidate, found)
        return candidate

    async def _verify_email(
        self, tenant_id: UUID, candidate: LeadCandidate
    ) -> LeadCandidate:
        """Drop an address the verifier says will hard-bounce.

        Only ``undeliverable`` is dropped. ``risky`` — overwhelmingly a
        catch-all domain, which is most Indian SME hosting — stays, because
        refusing those would silently remove the majority of the addressable
        market for a signal that is not evidence of a bounce.
        """
        email = normalise_email(candidate.email)
        if not email:
            return candidate
        # Through the builtin-things registry (P3), not the provider resolver
        # directly — the first real consumer of that path, and the proof the
        # registry dispatches onto live services. The registry meters the credit
        # and writes the run-context note; this method keeps only the drop
        # decision. ``checked: False`` (no verifier configured) is pass-through
        # here, not a drop.
        result = await self._c.growth_builtin.call(tenant_id, "verify_email", email=email)
        if result.get("checked") and not result.get("sendable"):
            logger.info(
                "growth_research_email_dropped",
                tenant_id=str(tenant_id),
                domain=candidate.domain,
                detail=result.get("detail"),
            )
            candidate.email = ""
        return candidate

    async def _summarise(
        self,
        tenant_id: UUID,
        candidate: LeadCandidate,
        profile: dict[str, Any],
        page: str,
    ) -> tuple[str, list[dict[str, str]]]:
        """Two sentences about this company, or nothing.

        Nothing is a real outcome, and an important one: a lead with no
        supported reason still enters the queue as a contact, and the cold-mail
        agent writes to it without inventing one. The alternative — letting the
        model produce a reason anyway — is exactly the failure this whole
        pipeline exists to avoid.
        """
        source = page or candidate.description
        if not source.strip():
            return "", []

        links = [{"url": f"https://{normalise_domain(candidate.domain)}/", "kind": "website"}]
        from app.adapters.llm.base import Message

        llm = await self._c.agent_models.llm_for(tenant_id, AGENT_KEY, "primary")
        system = await self._system_prompt(tenant_id)
        prompt = _SUMMARY_PROMPT.format(
            sells=str(profile.get("sells") or "our product"),
            audience=str(profile.get("audience") or "businesses"),
            page=source[:MAX_PAGE_CHARS],
        )
        try:
            completion = await llm.complete(
                [
                    Message(role="system", content=system),
                    Message(role="user", content=prompt),
                ],
                max_tokens=SUMMARY_MAX_TOKENS,
            )
        except Exception:
            logger.warning(
                "growth_research_summary_failed", domain=candidate.domain, exc_info=True
            )
            return "", links

        from app.services.growth.metering import meter_llm

        await meter_llm(
            tenant_id,
            completion.usage,
            feature=FEATURE,
            agent_key=AGENT_KEY,
            model=getattr(completion, "model", ""),
        )

        summary = (completion.text or "").strip()
        if not summary or summary.upper().startswith(NO_BASIS):
            return "", links

        verdict = await self._c.growth_grounding.check(
            tenant_id, agent_key=AGENT_KEY, draft=summary, evidence=source
        )
        if verdict.checked and verdict.state != "grounded":
            # advisory or block, the answer is the same here: an ungrounded
            # claim is dropped from the summary rather than carried forward for
            # a cold mail to quote. The block-mode gate that stops a *send* is in
            # the compose agent; this one is cheaper and earlier
            logger.info(
                "growth_research_summary_dropped",
                tenant_id=str(tenant_id),
                domain=candidate.domain,
                state=verdict.state,
            )
            return "", links
        return summary, links

    # -------------------------------------------------------------- approval

    async def _ask(self, tenant_id: UUID, leads: list[dict[str, Any]]) -> dict[str, Any]:
        """Hand the batch to the owner. Nothing mails until they answer.

        The rendered ``items`` and the stored ``lead_ids`` are both here on
        purpose: the notifier is never given the payload to summarise, so what
        the owner reads on Telegram is the same text the panel shows.
        """
        items = [
            {
                "lead_id": str(lead["id"]),
                "label": _label(lead),
            }
            for lead in leads
        ]
        result: dict[str, Any] = await self._c.approvals.request_approval(
            tenant_id,
            feature="outbound_batch",
            payload={
                "items": items,
                "lead_ids": [str(lead["id"]) for lead in leads],
            },
            summary=(
                f"{len(leads)} researched leads are ready to mail. "
                "Approve to write and queue one mail each."
            ),
        )
        return result

    async def _claim(self, tenant_id: UUID, query: str) -> bool:
        """Take a short lease on this exact search. False means it already ran.

        Keyed on the query rather than on the job, because a QStash redelivery
        arrives as the same payload and a hand-pressed "Find leads" for the same
        seed within the window is the same purchase either way.

        The lease is short — long enough to cover a retry ladder, far short of
        the 30-day freshness window that governs re-researching a *company*. The
        two answer different questions: this one is "did this job already run",
        that one is "do we already know this company".

        A cache outage lets the run through. A duplicated search costs one search;
        a skipped one costs a tenant their sales day, and the per-candidate
        freshness check still stops the expensive half from repeating.
        """
        import hashlib

        digest = hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]
        key = f"t:{tenant_id}:growth:research:run:{digest}"
        try:
            if await self._c.cache.get(key):
                return False
            await self._c.cache.set(key, "1", ttl_seconds=RESEARCH_CLAIM_TTL_SECONDS)
        except Exception:
            logger.debug("growth_research_claim_unavailable", exc_info=True)
        return True

    async def _meter_credits(
        self, tenant_id: UUID, kind: str, quantity: int, **meta: Any
    ) -> None:
        from app.services.growth.metering import meter

        await meter(
            tenant_id, kind, feature=FEATURE, quantity=quantity, agent=AGENT_KEY, **meta
        )


# ------------------------------------------------------------------- helpers


def _is_outbound_tenant(profile: dict[str, Any]) -> bool:
    """Unchosen ("") or A or both. Only an explicit owner choice opts out;
    never assume B2B or D2C. The gate every outbound agent checks at entry."""
    return str(profile.get("tenant_type") or "") in ("", "A", "both")


def _icp_query(profile: dict[str, Any]) -> str:
    """A search sentence built from the profile, when the caller gave no seed.

    Plain English rather than a boolean query: both the lead database's keyword
    matcher and the web search provider handle a sentence better than an
    operator soup, and the sentence is also what a human would read on the
    Leads screen to understand what was searched for.
    """
    raw_icp = profile.get("icp")
    icp: dict[str, Any] = raw_icp if isinstance(raw_icp, dict) else {}
    parts = [
        str(icp.get("industry") or ""),
        str(profile.get("audience") or ""),
        str(icp.get("size") or ""),
        str(icp.get("location") or ""),
    ]
    keywords = profile.get("keywords")
    if isinstance(keywords, list):
        parts.extend(str(k) for k in keywords[:3])
    return " ".join(part.strip() for part in parts if part and part.strip()).strip()


def _subject(candidate: LeadCandidate) -> str:
    """The freshness key: the domain, or the company name when there is no site."""
    return normalise_domain(candidate.domain) or candidate.company.strip().lower()


def _merge_candidate(base: LeadCandidate, found: LeadCandidate) -> LeadCandidate:
    """Fill the gaps in ``base`` from ``found``; never overwrite a known field."""
    # source included: the candidate's own origin survives enrichment — a
    # web_search find that Apollo filled in is still a web_search lead, and the
    # by-source report splits on where a lead came from, not who enriched it
    for field_name in (
        "company", "person", "role", "email", "phone", "location", "size", "source",
    ):
        if not getattr(base, field_name) and getattr(found, field_name):
            setattr(base, field_name, getattr(found, field_name))
    base.raw = {**base.raw, "enriched": found.raw}
    return base


def _label(lead: dict[str, Any]) -> str:
    """One line the owner reads on Telegram, in the panel, and nowhere else."""
    who = str(lead.get("person") or "").strip()
    company = str(lead.get("company") or lead.get("domain") or "unnamed company").strip()
    contact = str(lead.get("email") or lead.get("phone") or "no contact yet").strip()
    head = f"{who} at {company}" if who else company
    return f"{head} — {contact}"
