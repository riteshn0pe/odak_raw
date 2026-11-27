"""Consent, opt-out and deletion.

DPDP (India), GDPR (EU contacts) and CAN-SPAM (US) differ in the paperwork and
agree on the substance: record consent, state the purpose, make opting out easy,
honour it everywhere, and delete on request. This module is where the last two
live; the first two live at the point of collection.

Two rules worth stating because everything here follows from them:

* **Opting out is cross-channel.** Someone who replied STOP on WhatsApp did not
  consent to being emailed instead. So a per-contact delete or an unsubscribe
  writes a suppression on *every* channel, and the gate — the pure
  ``is_suppressed`` in ``app/domain/growth.py`` — matches on the normalised
  address regardless of which channel is asking.
* **Deleting a person leaves a tombstone.** The suppression row survives the
  deletion of everything else about them, because otherwise the next scrape
  re-imports them and the deletion request has achieved nothing. A tombstone is
  an address and a reason; it is the minimum that makes "forget me" durable, and
  it is itself deletable on request through support.

Both paths are batched through the queue with the ``DELETE_BATCH`` idiom, because
a tenant with a hundred thousand leads cannot be erased inside one HTTP callback.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from app.core.errors import ValidationError
from app.core.logging import get_logger
from app.domain.growth import normalise_email, normalise_phone

logger = get_logger(__name__)

GROWTH_DELETE_JOB = "growth_delete"

# Suppression reason written by each path. These strings reach a support screen
# and, eventually, a person asking why they stopped hearing from a business, so
# they are written for that reader rather than for a log grep.
REASON_CONTACT_DELETE = "deleted at the contact's request"
REASON_UNSUBSCRIBE = "unsubscribed"
REASON_COMPLAINT = "marked as spam"

# Channels a suppression is written on. Both, always — see the module docstring.
SUPPRESSED_CHANNELS: tuple[str, ...] = ("email", "whatsapp")


class ComplianceService:
    """Per-contact erasure, per-tenant erasure, and the opt-out write path."""

    def __init__(self, container: Any) -> None:
        self._container = container

    # ------------------------------------------------------------- opting out

    async def unsubscribe(
        self,
        tenant_id: UUID,
        address: str,
        *,
        reason: str = REASON_UNSUBSCRIBE,
        source: str = "",
    ) -> dict[str, Any]:
        """Record an opt-out on every channel and revoke any consent.

        Called by the unsubscribe link in outbound mail, by the WhatsApp STOP
        keyword handler, and by the owner from the lead drawer. All three mean
        the same thing, so all three do the same thing.
        """
        addresses = self._normalise(address)
        if not addresses:
            raise ValidationError("not a valid email address or phone number")

        for value in addresses:
            channel = "whatsapp" if value.startswith("+") else "email"
            await self._container.growth.consent.suppress(
                tenant_id,
                channel=channel,
                address=value,
                reason=reason,
                source=source,
            )
            # the suppression is written on the other channel too, so a later
            # lookup by the wrong channel still finds it. Cheap, and it removes
            # the only way this rule can be got wrong at a call site
            for other in SUPPRESSED_CHANNELS:
                if other != channel:
                    await self._container.growth.consent.suppress(
                        tenant_id,
                        channel=other,
                        address=value,
                        reason=reason,
                        source=source,
                    )
            await self._container.growth.consent.revoke_consent(tenant_id, value)

        await self._mirror_to_email_suppressions(tenant_id, addresses, reason)
        cancelled = await self._stop_queued(tenant_id, addresses)

        await self._container.growth.audit.record(
            tenant_id,
            agent="compliance",
            action="unsubscribed",
            input={"addresses": len(addresses)},
            output={"cancelled_messages": cancelled},
            reason=reason,
        )
        return {
            "status": "suppressed",
            "addresses": len(addresses),
            "cancelled": cancelled,
        }

    async def _mirror_to_email_suppressions(
        self, tenant_id: UUID, addresses: list[str], reason: str
    ) -> None:
        """Write the opt-out into the *email channel's* own list as well.

        Two lists is not duplication that crept in: ``email_suppressions`` (008)
        is what the support mailbox checks before it answers anybody, and
        ``growth_suppressions`` is cross-channel and growth-owned. A person who
        unsubscribes from outbound must also stop getting automated support
        replies to a thread they abandoned, and the only way both are true is if
        the opt-out lands in both.

        Failure is logged and swallowed: the growth suppression is already
        written, which is what actually stops the next send, and losing the
        mirror must not turn a successful opt-out into a 500 the person sees.
        """
        repo = getattr(self._container, "email_repo", None)
        if repo is None:
            return
        from app.core.security import hash_identifier

        secret = self._container.settings.SECRET_KEY
        for value in addresses:
            if value.startswith("+"):
                continue
            try:
                await repo.add_suppression(
                    tenant_id,
                    address=value,
                    address_hash=hash_identifier(value, f"{secret}:{tenant_id}"),
                    reason=reason[:200],
                )
            except Exception:
                logger.warning(
                    "growth_email_suppression_mirror_failed",
                    tenant_id=str(tenant_id),
                    exc_info=True,
                )

    async def _stop_queued(self, tenant_id: UUID, addresses: list[str]) -> int:
        """Drop anything already written but not yet sent to this person.

        Without this, an opt-out stops the *next* mail and the one already
        sitting in the send queue goes out anyway — which is the single most
        complained-about failure mode in outbound tooling, and it looks
        deliberate from the outside.
        """
        cancelled = 0
        for value in addresses:
            lead = await self._container.growth.leads.get_by_address(
                tenant_id,
                email="" if value.startswith("+") else value,
                phone=value if value.startswith("+") else "",
            )
            if lead is not None:
                cancelled += await self._container.growth.messages.cancel_queued_for_lead(
                    tenant_id, UUID(str(lead["id"]))
                )
                await self._container.growth.leads.set_status(
                    tenant_id, UUID(str(lead["id"])), "suppressed"
                )
                # the lead row survives suppression, so the CRM push is a real
                # sync, not a no-op (on_lead_changed swallows all errors)
                await self._container.growth_crm.on_lead_changed(
                    tenant_id, UUID(str(lead["id"]))
                )
            # buyers are not leads: their queued rows point at customer_id,
            # and the same queued-mail complaint applies to a campaign send
            customer = await self._container.growth.customers.get_by_address(
                tenant_id,
                email="" if value.startswith("+") else value,
                phone=value if value.startswith("+") else "",
            )
            if customer is not None:
                cancelled += (
                    await self._container.growth.messages.cancel_queued_for_customers(
                        tenant_id, [UUID(str(customer["id"]))]
                    )
                )
        return cancelled

    async def record_consent(
        self,
        tenant_id: UUID,
        address: str,
        *,
        channel: str,
        source: str,
        proof: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record an opt-in, with whatever evidence the collecting surface has.

        ``proof`` is stored verbatim — a form submission id, a timestamp, the
        page the box was ticked on. It is never interpreted here; its only job is
        to exist when someone asks what the basis for contact was.
        """
        if channel not in SUPPRESSED_CHANNELS:
            raise ValidationError(f"unknown channel {channel!r}")
        addresses = self._normalise(address)
        if not addresses:
            raise ValidationError("not a valid email address or phone number")

        for value in addresses:
            await self._container.growth.consent.consent(
                tenant_id, channel=channel, address=value, source=source, proof=proof
            )
        await self._container.growth.audit.record(
            tenant_id,
            agent="compliance",
            action="consent_recorded",
            input={"channel": channel, "source": source},
            reason="opt-in captured",
        )
        return {"status": "recorded", "addresses": len(addresses)}

    # --------------------------------------------------------------- deletion

    async def delete_contact(
        self, tenant_id: UUID, address: str, *, actor: str = ""
    ) -> dict[str, Any]:
        """Erase one person and tombstone them so they cannot be re-imported.

        Synchronous, unlike the tenant-wide path: one person is a handful of rows
        and the requester is entitled to be told it is done rather than that it
        has been scheduled.
        """
        addresses = self._normalise(address)
        if not addresses:
            raise ValidationError("not a valid email address or phone number")

        # tombstone first. If the delete half fails, the person is at least
        # already blocked; the reverse order would leave a window where they are
        # deleted, unblocked, and about to be re-scraped
        for value in addresses:
            for channel in SUPPRESSED_CHANNELS:
                await self._container.growth.consent.suppress(
                    tenant_id,
                    channel=channel,
                    address=value,
                    reason=REASON_CONTACT_DELETE,
                    source=actor or "owner",
                )

        counts = await self._container.growth.purge_contact(tenant_id, list(addresses))
        # no CRM push here: purge_contact just deleted the lead rows, and a push
        # for a deleted lead is a no-op that returns {"status": "missing"}
        await self._container.growth.audit.record(
            tenant_id,
            agent="compliance",
            action="contact_deleted",
            input={"addresses": len(addresses)},
            output=counts,
            reason=f"per-contact erasure requested by {actor or 'owner'}",
        )
        logger.info(
            "growth_contact_deleted",
            tenant_id=str(tenant_id),
            addresses=len(addresses),
            removed=counts,
        )
        return {"status": "deleted", "removed": counts, "suppressed": len(addresses)}

    async def request_tenant_delete(
        self, tenant_id: UUID, *, actor: str = ""
    ) -> dict[str, Any]:
        """Queue the erasure of every growth row for this tenant.

        Queued rather than done inline because it is unbounded work, and the
        ``growth_delete`` job re-enqueues itself until the tables are empty. The
        RAG side — documents, sources, vectors — is deliberately untouched: that
        already has its own delete path, and a second one would be a second thing
        to keep correct.
        """
        from app.adapters.queue.base import Job

        await self._container.growth.audit.record(
            tenant_id,
            agent="compliance",
            action="tenant_delete_requested",
            reason=f"requested by {actor or 'owner'}",
        )
        try:
            await self._container.queue.enqueue(
                Job(
                    kind=GROWTH_DELETE_JOB,
                    tenant_id=tenant_id,
                    payload={"scope": "tenant", "actor": actor},
                )
            )
        except Exception:
            logger.error(
                "growth_delete_enqueue_failed",
                tenant_id=str(tenant_id),
                exc_info=True,
            )
            # inline, for the same reason the approval notify falls back inline:
            # a deletion request that silently did not happen is the worst
            # possible outcome for this particular feature
            return await self.purge_tenant(tenant_id, actor=actor)
        return {"status": "queued"}

    async def purge_tenant(self, tenant_id: UUID, *, actor: str = "") -> dict[str, Any]:
        """Do the erasure. The ``growth_delete`` job's body.

        The audit row is written *before* the delete, because the delete removes
        ``growth_agent_actions`` too — a record written afterwards would be a
        record of a deletion in a table that has just been emptied of everything
        including itself.
        """
        await self._container.growth.audit.record(
            tenant_id,
            agent="compliance",
            action="tenant_delete_executing",
            reason=f"erasing all growth data at the request of {actor or 'owner'}",
        )
        counts = await self._container.growth.purge_tenant(tenant_id)
        logger.info(
            "growth_tenant_purged", tenant_id=str(tenant_id), removed=counts
        )
        return {"status": "deleted", "removed": counts}

    # --------------------------------------------------------------- private

    def _normalise(self, address: str) -> list[str]:
        """Every normalised form of what the caller handed us.

        An input is either an email or a phone number, and the caller frequently
        does not know which — an unsubscribe link carries whatever was in the
        recipient field. Returning the list lets one call site handle both
        without branching, and returning empty is how "this is neither" is said.
        """
        raw = (address or "").strip()
        out: list[str] = []
        email = normalise_email(raw)
        if email:
            out.append(email)
        phone = normalise_phone(raw)
        if phone:
            out.append(phone)
        return out
