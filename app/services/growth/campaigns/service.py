"""Campaign service: draft -> HITL approval -> template -> send -> attribution.

The lifecycle walks ``draft -> pending_approval -> approved -> sending ->
done``, with ``rejected`` as the owner's no. Approval is one per campaign
(default level 1); the payload carries the audience count and sample messages
so the decision is made on the real content.

WhatsApp goes through the template flow: the planner's text is submitted as a
``growth_wa_templates`` row and the send waits for Meta's approval — a row
still ``pending`` holds those messages (they fail loudly at send time with the
template reason, and email members are unaffected). Email goes through the
existing sender. The 24-hour window is enforced per customer by the WhatsApp
sender itself via ``ServiceWindow.evaluate``; outside the window only the
approved template goes, which is Meta's rule, not ours.

Attribution is the unique per-campaign discount code (created on the first
connected store; code-only when no store is connected, with UTM links as the
fallback handle) joined back against ``growth_orders.items``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.core.logging import get_logger

logger = get_logger(__name__)

AGENT_KEY = "growth_campaign"
FEATURE = "campaign"
RELEASE_JOB = "growth_campaign_release"
SEND_BATCH_JOB = "growth_send_batch"

RESULTS_JOB = "growth_campaign_results"

# One release callback's queueing budget. A 5,000-member audience drains across
# callbacks via the queued-id resume, the DELETE_BATCH idiom — never in one
# unbounded insert loop inside a scale-to-zero timeout.
RELEASE_BATCH = 500

# Meta's template statuses, in the vocabulary of the growth_wa_templates CHECK
# (migration 017). Only what the submission response can carry; the fuller
# webhook vocabulary lives with the webhook that receives it. Anything unmapped
# stays 'pending' — the send gate reads this column, so a guess here is a
# message Meta would refuse.
_META_STATUS: dict[str, str] = {
    "APPROVED": "approved",
    "REJECTED": "rejected",
    "PAUSED": "paused",
    "DISABLED": "disabled",
    "PENDING": "pending",
}


class CampaignService:
    """Owns the campaign rows and the release that fans them out to sends."""

    def __init__(self, container: Any) -> None:
        self._c = container

    # ----------------------------------------------------------------- create

    async def create(
        self, tenant_id: UUID, spec: dict[str, Any], *, actor: str = ""
    ) -> dict[str, Any]:
        """Draft a campaign and put it in front of the owner.

        Returns ``{"campaign": ..., "approval": ...}``. At level 0 the approval
        is auto-approved and the release runs immediately — same semantics as
        every other feature, through the same ``request_approval`` door.
        """
        from app.services.growth.campaigns.planner import CampaignPlanner

        name = str(spec.get("name") or "").strip() or "Untitled campaign"
        planner = CampaignPlanner(self._c)
        draft = await planner.draft(tenant_id, spec)

        campaign = await self._c.growth.campaigns.create(
            tenant_id,
            trigger=str(draft["trigger"]),
            name=name,
            segment=dict(draft["segment"]),
            offer=dict(draft["offer"]),
            channels=list(draft["channels"]),
            messages=dict(draft.get("texts") or {}),
        )
        await self._c.growth.audit.record(
            tenant_id,
            agent=AGENT_KEY,
            action="campaign_drafted",
            input={"name": name, "segment": draft["segment"].get("key")},
            output={"campaign_id": str(campaign["id"]), "audience": draft["segment"].get("count")},
            reason=f"drafted by {actor or 'owner'}",
        )

        items = [
            f"{language}: {(text[:160] + '…') if len(text) > 160 else text}"
            for language, text in list(dict(draft.get("texts") or {}).items())[:4]
        ]
        approval = await self._c.approvals.request_approval(
            tenant_id,
            feature="campaign",
            payload={
                "campaign_id": str(campaign["id"]),
                "items": [
                    f"{draft['segment'].get('count')} customers "
                    f"({', '.join(draft['channels'])}) · "
                    f"{draft['offer'].get('discount_pct')}% off "
                    f"with code {draft['offer'].get('code')}",
                    *items,
                ],
                "member_ids": list(draft["segment"].get("member_ids") or [])[:200],
                "member_total": draft["segment"].get("count"),
                "offer": draft["offer"],
                "summary": (
                    f"Campaign {name!r}: {draft['segment'].get('count')} customers, "
                    f"{draft['offer'].get('discount_pct')}% off "
                    f"(code {draft['offer'].get('code')})."
                ),
            },
            summary=(
                f"Send campaign {name!r} to {draft['segment'].get('count')} customers?"
            ),
        )
        status = "pending_approval" if approval["status"] == "pending" else "approved"
        campaign = await self._c.growth.campaigns.set_status(
            tenant_id,
            UUID(str(campaign["id"])),
            status,
            approval_id=UUID(str(approval["id"])),
        )
        return {"campaign": campaign, "approval": approval, "draft": draft}

    async def draft_from_job(
        self, tenant_id: UUID, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """The ``growth_campaign_draft`` job: insight/event triggers land here.

        Same door as the owner endpoint — the spec carries
        ``trigger=insight|event`` and the planner scores the event — so an
        insight's "act on it" drafts exactly what the composer would have.
        """
        return await self.create(tenant_id, payload, actor="insight")

    # ---------------------------------------------------------------- release

    async def release(
        self, tenant_id: UUID, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """The ``growth_campaign_release`` job: an approved campaign fans out.

        Reads the *approval row*, not the job payload: the payload is what the
        queue carried and could be replayed, the row is what the owner decided.
        Rejected/expired approvals mark the campaign ``rejected`` and drop
        anything queued — never send on silence.
        """
        approval_id = payload.get("approval_id")
        if approval_id is None:
            return {"status": "skipped", "reason": "no approval attached"}
        approval = await self._c.growth.approvals.get(tenant_id, UUID(str(approval_id)))
        if approval is None:
            logger.warning(
                "growth_campaign_release_missing_approval",
                tenant_id=str(tenant_id),
                approval_id=str(approval_id),
            )
            return {"status": "skipped", "reason": "approval not found"}
        campaign = await self._campaign_for(tenant_id, approval)
        if campaign is None:
            return {"status": "skipped", "reason": "no campaign for this approval"}

        if str(campaign.get("status")) == "rejected":
            # the owner cancelled after approving: a release job enqueued
            # before the cancel must not resurrect it
            await self._c.growth.messages.cancel_queued_for_campaign(
                tenant_id, UUID(str(campaign["id"]))
            )
            return {"status": "held", "reason": "campaign was cancelled"}

        if approval["status"] not in ("approved", "edited"):
            await self._c.growth.campaigns.set_status(
                tenant_id, UUID(str(campaign["id"])), "rejected"
            )
            await self._c.growth.messages.cancel_queued_for_campaign(
                tenant_id, UUID(str(campaign["id"]))
            )
            await self._c.growth.audit.record(
                tenant_id,
                agent=AGENT_KEY,
                action="campaign_rejected",
                input={"campaign_id": str(campaign["id"])},
                output={"approval": approval["status"]},
                reason="owner declined or the request expired; nothing sent",
            )
            return {"status": "held", "reason": f"approval is {approval['status']}"}

        campaign_id = UUID(str(campaign["id"]))

        # idempotency first: a redelivered job for a campaign already fanning
        # out (or finished) must not queue the audience twice. The message
        # log is the record — the set of customer ids already holding a row,
        # which (unlike a positional offset) stays correct when a member was
        # skipped and wrote nothing.
        queued_ids = await self._c.growth.messages.campaign_customer_ids(
            tenant_id, campaign_id
        )
        member_ids = list((campaign.get("segment") or {}).get("member_ids") or [])
        remaining = [m for m in member_ids if str(m) not in queued_ids]
        if member_ids and not remaining:
            await self._c.growth.campaigns.set_status(tenant_id, campaign_id, "sending")
            await self._enqueue_send(tenant_id, campaign_id)
            return {"status": "ok", "queued": 0, "reason": "already released"}
        if str(campaign.get("status")) in ("sending", "done") and not member_ids:
            return {"status": "ok", "queued": 0, "reason": "already released"}

        await self._c.growth.campaigns.set_status(tenant_id, campaign_id, "approved")

        # the unique code, on the first connected store; code-only when no
        # store is connected (UTM links still attribute, relatively)
        code = str((campaign.get("offer") or {}).get("code") or "")
        pct = int((campaign.get("offer") or {}).get("discount_pct") or 0)
        receipt = await self._create_discount(
            tenant_id, code=code, percent=pct, name=str(campaign.get("name") or "")
        )
        if receipt is not None:
            code = receipt.get("code") or code
            await self._c.growth.campaigns.set_status(
                tenant_id, campaign_id, "approved", discount_code=code
            )

        await self._submit_template(tenant_id, campaign, code)
        queued = await self._queue_messages(
            tenant_id, campaign, code, skip_ids=queued_ids
        )
        await self._c.growth.audit.record(
            tenant_id,
            agent=AGENT_KEY,
            action="campaign_released",
            input={"campaign_id": str(campaign_id)},
            output={"queued": queued, "code": code, "already": len(queued_ids)},
            reason="owner approved; queued for the next send window",
        )

        if queued < RELEASE_BATCH or len(queued_ids) + queued >= len(member_ids):
            await self._c.growth.campaigns.set_status(tenant_id, campaign_id, "sending")
            await self._enqueue_send(tenant_id, campaign_id)
            return {"status": "ok", "queued": queued, "code": code}

        # more audience than one callback should write: continue on a fresh
        # job (the DELETE_BATCH idiom) rather than timing out halfway. The
        # queued-count offset above makes the continuation resume, not repeat.
        from app.adapters.queue.base import Job

        await self._c.queue.enqueue(
            Job(
                kind=RELEASE_JOB,
                tenant_id=tenant_id,
                payload={"approval_id": str(approval["id"])},
            )
        )
        await self._c.growth.campaigns.set_status(tenant_id, campaign_id, "sending")
        return {"status": "partial", "queued": queued, "code": code}

    async def _campaign_for(
        self, tenant_id: UUID, approval: dict[str, Any]
    ) -> dict[str, Any] | None:
        """The campaign behind one approval row, by link or by payload.

        The link is the normal handle. It cannot be the only one: at HITL level
        0 ``request_approval`` auto-approves and enqueues the release *inside*
        the call, before ``create`` has written ``approval_id`` onto the
        campaign — so a release that ran first found no campaign and quietly
        skipped, which is a level-0 tenant whose campaigns never send. The
        payload carries ``campaign_id`` for exactly this reason, and it also
        covers an approval whose link write failed.
        """
        campaign = await self._c.growth.campaigns.get_by_approval(
            tenant_id, UUID(str(approval["id"]))
        )
        if campaign is not None:
            return dict(campaign)
        campaign_id = (approval.get("payload") or {}).get("campaign_id")
        if not campaign_id:
            return None
        try:
            fallback = await self._c.growth.campaigns.get(
                tenant_id, UUID(str(campaign_id))
            )
        except (ValueError, TypeError):
            return None
        return dict(fallback) if fallback is not None else None

    async def _enqueue_send(self, tenant_id: UUID, campaign_id: UUID) -> None:
        from app.adapters.queue.base import Job

        try:
            await self._c.queue.enqueue(
                Job(
                    kind=SEND_BATCH_JOB,
                    tenant_id=tenant_id,
                    payload={"campaign_id": str(campaign_id)},
                )
            )
        except Exception:
            logger.warning(
                "growth_campaign_send_enqueue_failed",
                tenant_id=str(tenant_id),
                campaign_id=str(campaign_id),
                exc_info=True,
            )

    async def on_decision(self, tenant_id: UUID, approval: dict[str, Any]) -> None:
        """Keep the campaign row honest for decisions that carry no release job.

        Rejections and expiries never enqueue (silence must not send), so this
        is what flips their campaigns to ``rejected`` instead of leaving them
        ``pending_approval`` forever. Approved decisions already release through
        the job; calling this for them too is harmless (idempotent by status).
        """
        if str(approval.get("feature")) != "campaign":
            return
        try:
            campaign = await self._campaign_for(tenant_id, approval)
        except Exception:
            logger.debug("growth_campaign_lookup_failed", exc_info=True)
            return
        if campaign is None or campaign.get("status") not in ("pending_approval", "draft"):
            return
        if approval["status"] in ("approved", "edited"):
            return  # the release job owns this path
        await self._c.growth.campaigns.set_status(
            tenant_id, UUID(str(campaign["id"])), "rejected"
        )
        await self._c.growth.messages.cancel_queued_for_campaign(
            tenant_id, UUID(str(campaign["id"]))
        )

    # ---------------------------------------------------------------- results

    async def results(self, tenant_id: UUID, campaign_id: UUID) -> dict[str, Any]:
        """Revenue + cost for the panel: send stats plus code-attributed orders."""
        campaign = await self._c.growth.campaigns.get(tenant_id, campaign_id)
        if campaign is None:
            from app.core.errors import NotFoundError

            raise NotFoundError("no such campaign")
        send_stats = await self._c.growth.messages.campaign_stats(tenant_id, campaign_id)
        code = str(campaign.get("discount_code") or (campaign.get("offer") or {}).get("code") or "")
        attributed: list[dict[str, Any]] = []
        revenue = 0
        if code:
            # bounded to the campaign's own lifetime: an order placed before the
            # campaign existed cannot have been caused by it, and an unbounded
            # match hands the panel a revenue number that only ever grows
            attributed = await self._c.growth.orders.attributed_to_code(
                tenant_id, code, since=campaign.get("created_at")
            )
            revenue = sum(int(o.get("total_micros") or 0) for o in attributed)
        cost = await self._cost_for(tenant_id, campaign_id)
        computed = {
            "sent": send_stats,
            "orders": len(attributed),
            "revenue_micros": revenue,
            "cost_micros": cost,
            "code": code,
            "computed_at": datetime.now(UTC).isoformat(),
        }
        merged = {**(campaign.get("results") or {}), **computed}
        await self._c.growth.campaigns.set_status(
            tenant_id,
            campaign_id,
            str(campaign.get("status") or "sending"),
            results=merged,
        )
        if campaign.get("status") == "sending" and send_stats and send_stats.get("queued", 0) == 0:
            await self._c.growth.campaigns.set_status(
                tenant_id, campaign_id, "done", results=merged
            )
            merged = {**merged, "status": "done"}
        return {"campaign_id": str(campaign_id), **merged}

    async def recompute(self, tenant_id: UUID, payload: dict[str, Any]) -> dict[str, Any]:
        """The ``growth_campaign_results`` job: recompute one campaign's results.

        Results used to be computed only when somebody opened the panel, which
        made two things quietly wrong: a campaign nobody looked at stayed
        ``sending`` forever, and ``planner.score_event`` — which scores an event
        from ``growth_campaigns.results`` — saw "no past data" on real history.

        The recompute rides the send spine rather than a new cron schedule: the
        dispatcher enqueues one of these per campaign whose queue it just
        drained, so the pass runs exactly when there is something new to count
        and never for a tenant with nothing to send. ``results`` is idempotent
        (it recomputes from the message log and the order rows), so a redelivered
        job is harmless.
        """
        campaign_id = payload.get("campaign_id")
        if campaign_id is None:
            return {"status": "skipped", "reason": "no campaign in the payload"}
        try:
            return await self.results(tenant_id, UUID(str(campaign_id)))
        except Exception:
            # a deleted campaign, or an order table that briefly refused: the
            # next drain re-asks, and a raised job would be redelivered forever
            logger.warning(
                "growth_campaign_results_failed",
                tenant_id=str(tenant_id),
                campaign_id=str(campaign_id),
                exc_info=True,
            )
            return {"status": "failed", "campaign_id": str(campaign_id)}

    # ---------------------------------------------------------------- private

    async def _create_discount(
        self, tenant_id: UUID, *, code: str, percent: int, name: str
    ) -> dict[str, Any] | None:
        """First connected store wins. None means code-only (no store).

        `CONNECTED_STATUSES` rather than `active`: a store in sync backoff is
        still a store, and refusing to mint a discount code because an hourly
        pull timed out would silently strip the campaign of its only means of
        attribution.
        """
        from app.services.growth.stores import CONNECTED_STATUSES

        for provider in ("shopify", "woocommerce"):
            rows = await self._c.growth.integrations.list(tenant_id)
            connected = [
                r for r in rows
                if str(r.get("provider")) == provider
                and r.get("status") in CONNECTED_STATUSES
            ]
            for row in connected:
                adapter = await self._c.growth_stores.provider_for(
                    tenant_id, provider, str(row.get("external_ref") or "")
                )
                if adapter is None:
                    continue
                try:
                    receipt = await adapter.create_discount(code=code, percent=percent, name=name)
                    return {"code": receipt.code, "external_id": receipt.external_id}
                except Exception:
                    logger.warning(
                        "growth_campaign_discount_failed",
                        tenant_id=str(tenant_id),
                        provider=provider,
                        exc_info=True,
                    )
                finally:
                    try:
                        await adapter.close()
                    except Exception:
                        logger.debug("growth_store_close_failed", exc_info=True)
        return None

    async def _submit_template(
        self, tenant_id: UUID, campaign: dict[str, Any], code: str
    ) -> None:
        """Submit the WhatsApp copy as marketing templates, one per language.

        Meta approves per (name, language) and ``wa_templates.approved`` looks
        the row up the same way, while every queued row carries the customer's
        *resolved* language — so submitting only English meant every non-English
        recipient failed at send. Each language present in the frozen copy gets
        its own row and its own submission.

        Meta approves asynchronously; the send path requires an ``approved``
        row, so a still-pending template holds the WhatsApp leg while email
        members send normally. The row body is the frozen text — what the owner
        approved is what Meta reviews.
        """
        if "whatsapp" not in list(campaign.get("channels") or []):
            return
        name = f"campaign_{str(campaign['id'])[:8]}".lower().replace("-", "_")
        texts = await self._campaign_texts(tenant_id, campaign)
        bodies = {
            language: copy.replace("{code}", code)[:1024]
            for language, copy in texts.items()
            if copy
        }
        if not bodies:
            return
        for language, body in bodies.items():
            await self._c.growth.wa_templates.upsert(
                tenant_id,
                name=name,
                language=language,
                category="marketing",
                body=body,
                status="pending",
            )
        await self._c.growth.audit.record(
            tenant_id,
            agent=AGENT_KEY,
            action="campaign_template_submitted",
            input={
                "campaign_id": str(campaign["id"]),
                "template": name,
                "languages": sorted(bodies),
            },
            reason="WhatsApp leg waits for Meta approval",
        )
        await self._try_meta_submit(tenant_id, name=name, bodies=bodies)

    async def _try_meta_submit(
        self, tenant_id: UUID, *, name: str, bodies: dict[str, str]
    ) -> None:
        """Submit each language to Meta's template management API.

        Needs the WABA id and an access token, both of which hang off the
        tenant's WhatsApp integration. A tenant with neither leaves every row
        ``pending`` and says so in the log — the panel shows that state honestly
        rather than failing the campaign, and the send gate keeps the WhatsApp
        leg held until a real approval arrives.

        The response only ever moves a row from ``pending`` to what Meta
        actually said (normally still pending, occasionally an instant reject).
        The *approval* comes later, on the ``message_template_status_update``
        webhook — a submission that flipped its own row to approved would be a
        fire-and-forget claim, and the send gate reads that column.
        """
        try:
            integration = await self._c.whatsapp_repo.active_for_tenant(tenant_id)
        except Exception:
            logger.debug("growth_template_waba_lookup_failed", exc_info=True)
            return
        waba_id = str(getattr(integration, "waba_id", "") or "") if integration else ""
        token = await self._waba_token(tenant_id, integration) if waba_id else ""
        if not waba_id or not token:
            logger.info(
                "growth_campaign_template_pending",
                tenant_id=str(tenant_id),
                template=name,
                waba=waba_id,
                reason="no WhatsApp credentials; the template was not submitted",
            )
            return

        from app.adapters.channels.whatsapp import WhatsAppTemplateClient, meta_language

        client = WhatsAppTemplateClient(
            token,
            waba_id,
            version=self._c.settings.WHATSAPP_GRAPH_VERSION,
            timeout=self._c.settings.HTTP_TIMEOUT_SECONDS,
        )
        try:
            for language, body in bodies.items():
                try:
                    template_id, status = await client.create(
                        name=name,
                        language=meta_language(language),
                        category="MARKETING",
                        body=body,
                    )
                except Exception:
                    # one language Meta refused must not hold the others back:
                    # the row stays pending and the panel shows it that way
                    logger.warning(
                        "growth_campaign_template_submit_failed",
                        tenant_id=str(tenant_id),
                        template=name,
                        language=language,
                        exc_info=True,
                    )
                    continue
                await self._c.growth.wa_templates.upsert(
                    tenant_id,
                    name=name,
                    language=language,
                    category="marketing",
                    body=body,
                    status=_META_STATUS.get(status.upper(), "pending"),
                    external_ref=template_id,
                )
                logger.info(
                    "growth_campaign_template_submitted",
                    tenant_id=str(tenant_id),
                    template=name,
                    language=language,
                    meta_status=status,
                )
        finally:
            await client.close()

    async def _waba_token(self, tenant_id: UUID, integration: Any) -> str:
        """The integration's decrypted Graph token, or "" when unavailable.

        Returning "" rather than raising is deliberate: a missing token is the
        same situation as a missing WABA — nothing was submitted, the rows stay
        pending, and the campaign's email leg is untouched.
        """
        try:
            record = await self._c.whatsapp_repo.get_access_token_record(
                tenant_id, integration.id
            )
            if record is None or record.provider != "whatsapp":
                return ""
            from app.core.security import decrypt_secret

            return str(decrypt_secret(record.ciphertext, self._c.settings.ENCRYPTION_KEY))
        except Exception:
            logger.warning(
                "growth_template_token_unavailable", tenant_id=str(tenant_id), exc_info=True
            )
            return ""

    async def _campaign_texts(
        self, tenant_id: UUID, campaign: dict[str, Any]
    ) -> dict[str, str]:
        """The frozen copy: exactly what the planner wrote and the owner saw."""
        _ = tenant_id
        texts = campaign.get("messages") or {}
        return {str(k): str(v) for k, v in texts.items() if v}

    async def _queue_messages(
        self,
        tenant_id: UUID,
        campaign: dict[str, Any],
        code: str,
        *,
        skip_ids: set[str] | None = None,
    ) -> int:
        """One ``growth_messages`` row per audience member, on their channel.

        Bounded to ``RELEASE_BATCH`` new rows per call; the caller re-enqueues
        itself while audience remains. ``skip_ids`` resumes past members a
        previous attempt already wrote (read from the message log, which is
        the only record that cannot lie about what was queued).
        """
        from app.domain.growth import resolve_language

        campaign_id = UUID(str(campaign["id"]))
        segment = campaign.get("segment") or {}
        skip = skip_ids or set()
        member_ids = [m for m in list(segment.get("member_ids") or []) if str(m) not in skip]
        channels = dict(segment.get("channels") or {})
        settings = await self._c.growth_profile.get(tenant_id)
        allowed = list((settings.get("languages") or {}).get("allowed") or ["en"])
        texts = await self._campaign_texts(tenant_id, campaign)
        queued = 0
        for member_id in member_ids:
            if queued >= RELEASE_BATCH:
                break
            try:
                member_uuid = UUID(str(member_id))
            except (ValueError, TypeError):
                continue
            customer = await self._c.growth.customers.get(tenant_id, member_uuid)
            if customer is None:
                continue
            channel = str(channels.get(str(member_id)) or "")
            if channel not in ("email", "whatsapp"):
                continue
            language = resolve_language(
                None,
                str(customer.get("language") or "") or None,
                str(customer.get("phone") or "") or None,
                allowed,
            )
            text = self._render(customer, campaign, code, language, texts)
            # WhatsApp outside the window needs the approved template name on
            # the row; the sender enforces it. Email carries no template.
            template_name = (
                f"campaign_{str(campaign['id'])[:8]}".lower().replace("-", "_")
                if channel == "whatsapp"
                else None
            )
            try:
                row = await self._c.growth.messages.add(
                    tenant_id,
                    lead_id=None,
                    customer_id=member_uuid,
                    channel=channel,
                    direction="out",
                    content=text,
                    language=language,
                    template_id=template_name,
                    status="queued",
                    campaign_id=campaign_id,
                )
                if row is not None:
                    queued += 1
            except Exception:
                logger.warning(
                    "growth_campaign_queue_failed",
                    tenant_id=str(tenant_id),
                    campaign_id=str(campaign_id),
                    customer_id=str(member_id),
                    exc_info=True,
                )
        return queued

    def _render(
        self,
        customer: dict[str, Any],
        campaign: dict[str, Any],
        code: str,
        language: str,
        texts: dict[str, str],
    ) -> str:
        offer = campaign.get("offer") or {}
        pct = offer.get("discount_pct", 0)
        name = str(customer.get("name") or "there").split()[0]
        base = (
            texts.get(language)
            or texts.get("en")
            or next(iter(texts.values()), "")
        )
        if base and "{code}" not in base and code not in base:
            base = f"{base} Use code {code}."
        if base:
            return base.replace("{code}", code)[:2000]
        greeting = f"Hi {name}, " if name != "there" else "Hi, "
        return (
            f"{greeting}{campaign.get('name', 'our sale')} is on — "
            f"{pct}% off with code {code}."
        )[:2000]

    async def _cost_for(self, tenant_id: UUID, campaign_id: UUID) -> int:
        """Sum the denormalised per-message cost mirrors for this campaign."""
        from sqlalchemy import text

        from app.core.db import tenant_scope

        try:
            async with tenant_scope(tenant_id) as session:
                value = (
                    await session.execute(
                        text(
                            "SELECT COALESCE(sum(cost_micros), 0) FROM growth_messages "
                            "WHERE tenant_id = :tid AND campaign_id = :cid"
                        ),
                        {"tid": tenant_id, "cid": campaign_id},
                    )
                ).scalar_one_or_none()
            return int(value or 0)
        except Exception:
            logger.debug("growth_campaign_cost_failed", exc_info=True)
            return 0
