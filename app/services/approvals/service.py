"""Human-in-the-loop approvals: one gate in front of every money or contact action.

The owner picks a level per feature:

    0  fully automatic — the agent acts and the owner reads about it after
    1  one approval per batch (the default)
    2  approve every single message

Level 0 still writes an approval row. It is marked ``approved`` by ``system`` at
creation and nothing is delivered — but the row exists, so "what did the agents
decide to do last Tuesday" has one answer regardless of which level each feature
was on at the time. An automatic action that left no trace would be the one thing
nobody could audit afterwards.

Why this is a new service and not an extension of escalation: an escalation row is
notification-only — ``pending -> notified | failed | suppressed``, per the CHECK
in ``migrations/007_escalation.sql``. It has no decision, no levels and no
expiry. An approval is ``pending -> approved | rejected | edited | expired`` with
a batch held behind it. Different state machine, so a different table, built
beside escalation and reusing its delivery shape rather than bending it.

**Expiry holds, it never sends.** A batch nobody decided on is a batch the owner
did not agree to, and the only safe reading of silence is no.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from app.core.errors import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.domain.ports import ApprovalRequest

logger = get_logger(__name__)

APPROVAL_NOTIFY_JOB = "approval_notify"
APPROVAL_EXPIRE_JOB = "approval_expire"
APPROVAL_USAGE_KIND = "approval"

# tenant_secrets provider names for the owner-facing destinations.
TELEGRAM_SECRET_PROVIDER = "telegram_bot"  # noqa: S105 - a provider name, not a credential
SLACK_SECRET_PROVIDER = "slack_webhook"  # noqa: S105 - a provider name, not a credential

# growth_integrations.provider values that carry an owner alert destination.
OWNER_DESTINATIONS: tuple[str, ...] = ("telegram", "slack", "whatsapp")

# Approved features and the job that consumes each one. A feature with no
# consumer yet is listed as None rather than omitted, so adding the agent later
# is one line here and the gap is visible in the meantime instead of being an
# approval that silently goes nowhere.
FEATURE_JOBS: dict[str, str | None] = {
    # an approved outbound batch is *released*, not sent: the release job moves
    # each lead researched -> queued and enqueues one compose job per lead, and
    # the scheduler sends the resulting drafts under the cap and the window.
    # Pointing this at the send job would have skipped the writing entirely
    "outbound_batch": "growth_batch_release",
    # an approved campaign is *released*, not sent: the release job creates the
    # discount code, submits the template, queues one message per audience
    # member and enqueues a send batch. The scheduler is what ultimately sends.
    "campaign": "growth_campaign_release",
    "review_ask": None,
}

DECISIONS: frozenset[str] = frozenset({"approved", "rejected", "edited"})


class ApprovalService:
    """Create, deliver, decide and expire approval requests."""

    def __init__(self, container: Any) -> None:
        self._container = container

    # ------------------------------------------------------------- lifecycle

    async def request_approval(
        self,
        tenant_id: UUID,
        *,
        feature: str,
        payload: dict[str, Any],
        batch_id: UUID | None = None,
        summary: str = "",
    ) -> dict[str, Any]:
        """Ask the owner to approve one batch, or auto-approve at level 0.

        The level is read from ``growth_settings.hitl_levels`` *now* and stored on
        the row. From that point the row's own level governs the decision, so an
        owner who lowers the level while a batch is pending cannot retroactively
        auto-approve something a human was already asked about.
        """
        if feature not in FEATURE_JOBS:
            raise ValidationError(f"unknown approval feature {feature!r}")

        settings = await self._container.growth_profile.get(tenant_id)
        level = int(settings.get("hitl_levels", {}).get(feature, 1))
        batch = batch_id or uuid4()

        stored = dict(payload)
        stored.setdefault("summary", summary or self._default_summary(feature, payload))

        expires_at = self._expiry()
        row = await self._container.growth.approvals.create(
            tenant_id,
            batch_id=batch,
            feature=feature,
            level=level,
            payload=stored,
            expires_at=expires_at,
            status="approved" if level == 0 else "pending",
            decided_by="system" if level == 0 else None,
        )

        await self._container.growth.audit.record(
            tenant_id,
            agent="approvals",
            action="approval_requested",
            input={"feature": feature, "level": level, "batch_id": str(batch)},
            output={"approval_id": str(row.get("id")), "status": row.get("status")},
            reason="auto-approved at level 0" if level == 0 else "awaiting owner decision",
        )
        await self._meter(tenant_id, feature)

        if level == 0:
            # nothing to deliver, but the consumer still has to run: level 0 means
            # "do it without asking", not "do not do it"
            await self._release(tenant_id, row)
            return dict(row)

        await self._enqueue_notify(tenant_id, UUID(str(row["id"])))
        return dict(row)

    async def decide(
        self,
        tenant_id: UUID,
        approval_id: UUID,
        *,
        decision: str,
        decided_by: str,
        edited_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record the owner's decision and release or drop the batch.

        Refuses anything that is not still pending. The repository's
        ``status = 'pending'`` predicate is what makes that a real guarantee
        rather than a check-then-act race between two open browser tabs.
        """
        if decision not in DECISIONS:
            raise ValidationError(f"decision must be one of {sorted(DECISIONS)}")
        if decision == "edited" and edited_payload is None:
            raise ValidationError("an edited decision must carry the edited payload")

        row = await self._container.growth.approvals.decide(
            tenant_id,
            approval_id,
            status=decision,
            decided_by=decided_by,
            payload=edited_payload,
        )
        if row is None:
            existing = await self._container.growth.approvals.get(tenant_id, approval_id)
            if existing is None:
                raise NotFoundError("no such approval")
            raise ValidationError(
                f"this approval is already {existing['status']} and cannot be changed"
            )

        await self._container.growth.audit.record(
            tenant_id,
            agent="approvals",
            action=f"approval_{decision}",
            input={"approval_id": str(approval_id), "feature": row["feature"]},
            output={"status": row["status"]},
            reason=f"decided by {decided_by}",
        )

        if decision in ("approved", "edited"):
            await self._release(tenant_id, row)
        elif row["feature"] == "campaign":
            # a rejection or an edit-shaped no still has to reach the campaign:
            # the release job marks it rejected and drops anything queued.
            # Silence must never send, but it must also never leave the row
            # pending_approval forever.
            await self._release(tenant_id, row)
        return dict(row)

    async def pending_for(self, tenant_id: UUID, limit: int = 50) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = await self._container.growth.approvals.pending_for(
            tenant_id, limit=limit
        )
        return rows

    async def expire_tenant(self, tenant_id: UUID) -> dict[str, Any]:
        """Flip one tenant's lapsed rows and re-ping the owner once.

        Once, not repeatedly: an owner who ignored the first request is not going
        to be persuaded by the fourth, and a re-ping loop on an expired batch is
        how a notification channel gets muted entirely.
        """
        now = datetime.now(UTC)
        expired = await self._container.growth.approvals.expire_due(tenant_id, now)
        if not expired:
            return {"expired": 0}

        for row in expired:
            await self._container.growth.audit.record(
                tenant_id,
                agent="approvals",
                action="approval_expired",
                input={"approval_id": str(row["id"]), "feature": row["feature"]},
                reason="no decision before the deadline; batch held, nothing sent",
            )
            if row["feature"] == "campaign":
                # expiries never enqueue (silence must not send), so the
                # campaign row is flipped directly instead of via the job
                try:
                    await self._container.growth_campaigns.on_decision(tenant_id, row)
                except Exception:
                    logger.debug("campaign_expiry_mark_failed", exc_info=True)
        await self._alert_expired(tenant_id, expired)
        logger.info(
            "growth_approvals_expired", tenant_id=str(tenant_id), count=len(expired)
        )
        return {"expired": len(expired), "features": [r["feature"] for r in expired]}

    async def expire_sweep(self, limit: int = 200) -> dict[str, Any]:
        """Cross-tenant expiry pass, driven by the ``approval_expiry`` cron task.

        The sweep *finds* the work and hands each tenant's share to an
        ``approval_expire`` job rather than doing all of it inline. Expiring a
        tenant's batch is not a single UPDATE — it writes an audit row per
        expired approval and then delivers an owner alert across up to three
        channels — so a hundred tenants in one cron callback is a callback that
        times out somewhere in the middle, having alerted some owners and not
        others. One job per tenant is retryable on its own and cannot half-finish
        the fleet.

        Candidate selection under ``admin_scope`` against ``tenants``, the
        per-tenant check inside that tenant's own scope, one try/except each.
        With no durable queue the work falls back to running inline, the same
        posture ``_enqueue_notify`` takes: a batch that silently never expires
        would sit "pending" forever and the owner would never be told.
        """
        from app.adapters.postgres.growth_repo import active_tenant_ids
        from app.adapters.queue.base import Job

        now = datetime.now(UTC)
        tenants = await active_tenant_ids(limit=limit)
        queued = 0
        expired = 0
        failures: list[str] = []
        for tenant_id in tenants:
            try:
                # the pending rows are only visible from inside the tenant's own
                # scope: growth_approvals carries FORCE RLS, so a cross-tenant
                # "who has expired rows" query would match nothing at all
                if not await self._container.growth.approvals.has_expired(tenant_id, now):
                    continue
                try:
                    await self._container.queue.enqueue(
                        Job(kind=APPROVAL_EXPIRE_JOB, tenant_id=tenant_id, payload={})
                    )
                    queued += 1
                except Exception:
                    logger.warning(
                        "approval_expire_enqueue_failed",
                        task="approval_expiry",
                        tenant_id=str(tenant_id),
                        exc_info=True,
                    )
                    result = await self.expire_tenant(tenant_id)
                    expired += int(result.get("expired", 0))
            except Exception:
                failures.append(str(tenant_id))
                logger.warning(
                    "approval_expiry_tenant_failed",
                    task="approval_expiry",
                    tenant_id=str(tenant_id),
                    exc_info=True,
                )
        return {
            "tenants": len(tenants),
            "queued": queued,
            "expired_inline": expired,
            "failed": failures,
        }

    # -------------------------------------------------------------- delivery

    async def notify(self, tenant_id: UUID, approval_id: UUID) -> list[dict[str, Any]]:
        """Deliver one approval request to every configured owner destination.

        Sequential rather than gathered, with a per-notifier timeout: one hung
        destination must not eat the budget the next one needs. Every failure
        becomes a receipt, never an exception — the batch is held either way, so
        a notifier outage costs the owner a ping, not a decision.
        """
        row = await self._container.growth.approvals.get(tenant_id, approval_id)
        if row is None:
            raise NotFoundError("no such approval")
        if row["status"] != "pending":
            # a redelivered job for a batch already decided. Not an error: the
            # queue is allowed to be at-least-once
            return []

        request = self._build_request(tenant_id, row)
        budget = self._container.settings.ESCALATION_NOTIFY_TIMEOUT_SECONDS
        receipts: list[dict[str, Any]] = []

        notifiers = await self._notifiers(tenant_id)
        if not notifiers:
            # the panel is always a destination — the row itself is what the
            # screen polls — so this is a warning, not a failure
            logger.warning(
                "approval_no_chat_destinations",
                tenant_id=str(tenant_id),
                approval_id=str(approval_id),
            )
            return []

        for notifier in notifiers:
            try:
                receipt = await asyncio.wait_for(notifier.request(request), timeout=budget)
            except TimeoutError:
                logger.warning(
                    "approval_notifier_timeout",
                    channel=notifier.name,
                    tenant_id=str(tenant_id),
                    approval_id=str(approval_id),
                    timeout_seconds=budget,
                )
                receipts.append(
                    {
                        "channel": notifier.name,
                        "delivered": False,
                        "error": f"timed out after {budget:g}s",
                    }
                )
                continue
            except Exception as exc:  # a notifier must never break the others
                logger.warning(
                    "approval_notifier_raised",
                    channel=notifier.name,
                    tenant_id=str(tenant_id),
                    approval_id=str(approval_id),
                    error=str(exc)[:200],
                    exc_info=True,
                )
                receipts.append(
                    {"channel": notifier.name, "delivered": False, "error": str(exc)[:200]}
                )
                continue
            finally:
                try:
                    await notifier.close()
                except Exception:
                    logger.debug("approval_notifier_close_failed", exc_info=True)

            receipts.append(
                {
                    "channel": receipt.channel,
                    "delivered": receipt.delivered,
                    "provider_message_id": receipt.provider_message_id,
                    "error": receipt.error,
                }
            )

        if not any(entry.get("delivered") for entry in receipts):
            logger.warning(
                "approval_undelivered",
                tenant_id=str(tenant_id),
                approval_id=str(approval_id),
                receipts=receipts,
            )
        return receipts

    async def alert(self, tenant_id: UUID, summary: str, items: list[str]) -> int:
        """Send an owner alert through the same fan-out, with no decision attached.

        Hot-lead pings, daily summaries and platform warnings all land here. They
        reuse the approval notifiers because the destinations are the same three
        and the owner should not have to configure Telegram twice — the message
        simply carries no buttons and no deadline.
        """
        request = ApprovalRequest(
            tenant_id=tenant_id,
            approval_id=uuid4(),
            feature="alert",
            level=0,
            summary=summary,
            items=tuple(items),
        )
        delivered = 0
        budget = self._container.settings.ESCALATION_NOTIFY_TIMEOUT_SECONDS
        for notifier in await self._notifiers(tenant_id):
            try:
                receipt = await asyncio.wait_for(notifier.request(request), timeout=budget)
                delivered += 1 if receipt.delivered else 0
            except Exception:
                logger.warning(
                    "owner_alert_failed",
                    channel=notifier.name,
                    tenant_id=str(tenant_id),
                    exc_info=True,
                )
            finally:
                try:
                    await notifier.close()
                except Exception:
                    logger.debug("approval_notifier_close_failed", exc_info=True)
        return delivered

    # --------------------------------------------------------------- private

    def _expiry(self) -> datetime:
        from datetime import timedelta

        hours = int(self._container.settings.GROWTH_APPROVAL_TTL_HOURS)
        return datetime.now(UTC) + timedelta(hours=hours)

    def _default_summary(self, feature: str, payload: dict[str, Any]) -> str:
        items = payload.get("items")
        count = len(items) if isinstance(items, list) else 0
        from app.services.approvals.render import feature_label

        return f"{feature_label(feature)}: {count} item(s) waiting for your decision."

    def _build_request(self, tenant_id: UUID, row: dict[str, Any]) -> ApprovalRequest:
        payload = row.get("payload") or {}
        raw_items = payload.get("items")
        items: tuple[str, ...] = ()
        if isinstance(raw_items, list):
            items = tuple(
                item if isinstance(item, str) else str(item.get("label", item))
                for item in raw_items
            )

        link = self._panel_url(UUID(str(row["id"])))
        return ApprovalRequest(
            tenant_id=tenant_id,
            approval_id=UUID(str(row["id"])),
            feature=str(row["feature"]),
            level=int(row["level"]),
            summary=str(payload.get("summary") or ""),
            items=items,
            expires_at=row.get("expires_at"),
            # both buttons point at the same panel row rather than at a one-click
            # decision endpoint. A GET that approves a money action is a link
            # anything can prefetch — a mail scanner, a chat client's preview
            # fetcher, a browser's speculative load
            approve_url=link,
            reject_url=link,
        )

    def _panel_url(self, approval_id: UUID) -> str:
        settings = self._container.settings
        base = (settings.DASHBOARD_URL or settings.PUBLIC_BASE_URL or "").rstrip("/")
        return f"{base}/growth/approvals?id={approval_id}"

    async def _release(self, tenant_id: UUID, row: dict[str, Any]) -> None:
        """Hand an approved batch to the job that consumes it.

        A feature with no consumer yet records the decision and stops there,
        loudly. That is the honest state during the stages where the approval
        component exists and the agent behind it does not.
        """
        feature = str(row["feature"])
        kind = FEATURE_JOBS.get(feature)
        if kind is None:
            logger.info(
                "approval_released_no_consumer",
                tenant_id=str(tenant_id),
                feature=feature,
                approval_id=str(row["id"]),
            )
            return

        from app.adapters.queue.base import Job

        try:
            await self._container.queue.enqueue(
                Job(
                    kind=kind,
                    tenant_id=tenant_id,
                    payload={
                        "approval_id": str(row["id"]),
                        "batch_id": str(row["batch_id"]),
                        "feature": feature,
                    },
                )
            )
        except Exception:
            logger.error(
                "approval_release_enqueue_failed",
                tenant_id=str(tenant_id),
                approval_id=str(row["id"]),
                kind=kind,
                exc_info=True,
            )

    async def _alert_expired(self, tenant_id: UUID, expired: list[dict[str, Any]]) -> None:
        from app.services.approvals.render import feature_label

        labels = sorted({feature_label(str(row["feature"])) for row in expired})
        await self.alert(
            tenant_id,
            summary=(
                f"{len(expired)} approval request(s) expired without a decision. "
                "Nothing was sent — the batch is held."
            ),
            items=labels,
        )

    async def _meter(self, tenant_id: UUID, feature: str) -> None:
        from app.services.growth.metering import meter

        await meter(
            tenant_id,
            APPROVAL_USAGE_KIND,
            feature="approvals",
            quantity=1,
            approval_feature=feature,
        )

    async def _enqueue_notify(self, tenant_id: UUID, approval_id: UUID) -> None:
        """Queue the delivery, falling back to doing it inline.

        Same posture as ``EscalationService._enqueue_notify``: with no durable
        queue configured, or when the enqueue itself fails, the notification is
        sent in-process. A request the owner never hears about is the failure
        this component exists to prevent, so the slower path is the right one.
        """
        from app.adapters.queue.base import Job

        try:
            await self._container.queue.enqueue(
                Job(
                    kind=APPROVAL_NOTIFY_JOB,
                    tenant_id=tenant_id,
                    payload={"approval_id": str(approval_id)},
                )
            )
        except Exception:
            logger.warning(
                "approval_notify_enqueue_failed",
                tenant_id=str(tenant_id),
                approval_id=str(approval_id),
                exc_info=True,
            )
            try:
                await self.notify(tenant_id, approval_id)
            except Exception:
                logger.error(
                    "approval_notify_inline_failed",
                    tenant_id=str(tenant_id),
                    approval_id=str(approval_id),
                    exc_info=True,
                )

    async def _notifiers(self, tenant_id: UUID) -> list[Any]:
        """Build one notifier per configured owner destination.

        Constructed per notification and never cached, for the reason
        ``EscalationService._notifiers`` states: the credentials are tenant
        secrets, so a long-lived notifier would either hold a decrypted
        credential in memory or serve a stale one.

        Each destination needs three things — an active ``growth_integrations``
        row, the deployment switch for that channel, and a resolvable secret. A
        missing leg is a warning and a skipped notifier, never an exception.
        """
        settings = self._container.settings
        rows = await self._container.growth.integrations.list(tenant_id)
        active = {
            str(row["provider"]): row
            for row in rows
            if str(row["provider"]) in OWNER_DESTINATIONS and row["status"] == "active"
        }
        notifiers: list[Any] = []

        telegram = active.get("telegram")
        if telegram is not None and settings.TELEGRAM_ENABLED:
            token = await self._secret(
                tenant_id, telegram.get("secret_ref"), TELEGRAM_SECRET_PROVIDER
            )
            if not token and settings.TELEGRAM_BOT_TOKEN:
                # provider mode: a tenant with no bot of their own is served by
                # the platform bot — their /start binding wrote a chat id,
                # never a secret
                token = settings.TELEGRAM_BOT_TOKEN
            if token:
                from app.adapters.channels.telegram import TelegramApprovalNotifier
                from app.services.approvals.render import render_telegram_message

                try:
                    notifiers.append(
                        TelegramApprovalNotifier(
                            token,
                            str(telegram.get("external_ref") or ""),
                            render_telegram_message,
                            timeout=settings.ESCALATION_NOTIFY_TIMEOUT_SECONDS,
                        )
                    )
                except Exception as exc:
                    logger.warning(
                        "approval_telegram_rejected",
                        tenant_id=str(tenant_id),
                        error=str(exc)[:200],
                    )
        elif telegram is not None:
            logger.warning("approval_telegram_disabled", tenant_id=str(tenant_id))

        slack = active.get("slack")
        if slack is not None and settings.SLACK_ENABLED:
            url = await self._secret(
                tenant_id, slack.get("secret_ref"), SLACK_SECRET_PROVIDER
            )
            if url:
                from app.adapters.channels.slack import SlackWebhookNotifier
                from app.services.approvals.render import render_slack_blocks

                try:
                    notifiers.append(
                        _SlackApprovalNotifier(
                            SlackWebhookNotifier(
                                url,
                                cast("Any", render_slack_blocks),
                                timeout=settings.ESCALATION_NOTIFY_TIMEOUT_SECONDS,
                            )
                        )
                    )
                except Exception as exc:
                    logger.warning(
                        "approval_slack_rejected",
                        tenant_id=str(tenant_id),
                        error=str(exc)[:200],
                    )

        whatsapp = active.get("whatsapp")
        if whatsapp is not None:
            notifier = await self._whatsapp_notifier(tenant_id, whatsapp)
            if notifier is not None:
                notifiers.append(notifier)

        return notifiers

    async def _whatsapp_notifier(
        self, tenant_id: UUID, row: dict[str, Any]
    ) -> Any | None:
        """Owner-facing WhatsApp, reusing the tenant's existing Meta connection.

        The credential is the one already stored for the customer-facing channel
        — the tenant connected WhatsApp once and both uses share it. Only the
        recipient differs: ``external_ref`` here is the owner's own number.
        """
        integration = await self._container.whatsapp_repo.active_for_tenant(tenant_id)
        if integration is None:
            logger.warning("approval_whatsapp_inactive", tenant_id=str(tenant_id))
            return None

        # the token lives in tenant_secrets and the integration carries only a
        # reference to it — the same split every other credential in this
        # codebase uses, so the record has to be fetched and decrypted rather
        # than read off the integration row
        record = await self._container.whatsapp_repo.get_access_token_record(
            tenant_id, integration.id
        )
        if record is None:
            logger.warning("approval_whatsapp_secret_missing", tenant_id=str(tenant_id))
            return None
        try:
            token = self._decrypt(str(record.ciphertext))
        except Exception:
            logger.warning(
                "approval_whatsapp_secret_undecryptable", tenant_id=str(tenant_id)
            )
            return None

        from app.services.approvals.render import render_whatsapp_text

        return _WhatsAppApprovalNotifier(
            container=self._container,
            access_token=token,
            phone_number_id=str(integration.phone_number_id),
            recipient=str(row.get("external_ref") or ""),
            renderer=render_whatsapp_text,
        )

    async def _secret(
        self, tenant_id: UUID, ref: Any, provider: str
    ) -> str | None:
        if not ref:
            return None
        from sqlalchemy import text

        from app.core.db import tenant_scope

        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT ciphertext FROM tenant_secrets "
                        "WHERE tenant_id = :tid AND ref = :ref AND provider = :provider"
                    ),
                    {"tid": tenant_id, "ref": str(ref), "provider": provider},
                )
            ).mappings().first()
        if row is None:
            logger.warning(
                "approval_secret_missing",
                tenant_id=str(tenant_id),
                provider=provider,
                ref=str(ref),
            )
            return None
        try:
            return self._decrypt(str(row["ciphertext"]))
        except Exception:
            logger.warning(
                "approval_secret_undecryptable",
                tenant_id=str(tenant_id),
                provider=provider,
            )
            return None

    def _decrypt(self, ciphertext: str) -> str:
        from app.core.security import decrypt_secret

        return decrypt_secret(ciphertext, self._container.settings.ENCRYPTION_KEY)


class _SlackApprovalNotifier:
    """Adapts the escalation Slack notifier to the approval port.

    The Slack adapter is already correct — the host allowlist, the refused
    redirects, the peer check, the receipt-not-exception contract — and the only
    thing that differs for an approval is what the injected renderer returns.
    Wrapping it is six lines; a second Slack adapter would be a second copy of
    the SSRF controls to keep in step, which is how one of them drifts.

    The rename is the whole adapter: ``notify`` on the escalation port is
    ``request`` on this one, and the payload the adapter posts is whatever the
    renderer it was constructed with produced.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    @property
    def name(self) -> str:
        return "slack"

    async def request(self, approval: ApprovalRequest) -> Any:
        return await self._inner.notify(approval)

    async def close(self) -> None:
        await self._inner.close()


class _WhatsAppApprovalNotifier:
    """Owner-facing WhatsApp, built per notification from the tenant's own token.

    Free-form first, template on refusal. The owner is a business contact of
    their own WhatsApp number, so the 24-hour service window applies to them like
    anyone else — but unlike a customer, an owner who has never messaged the
    number is normal, which is why the template path is not an error case here.
    """

    def __init__(
        self,
        *,
        container: Any,
        access_token: str,
        phone_number_id: str,
        recipient: str,
        renderer: Any,
    ) -> None:
        self._container = container
        self._token = access_token
        self._phone_number_id = phone_number_id
        self._recipient = recipient
        self._render = renderer
        self._client: Any = None

    @property
    def name(self) -> str:
        return "whatsapp"

    async def request(self, approval: ApprovalRequest) -> Any:
        from app.adapters.channels.whatsapp import WhatsAppClient
        from app.domain.ports import NotifyReceipt

        if not self._recipient:
            return NotifyReceipt(channel="whatsapp", error="no owner number configured")

        settings = self._container.settings
        self._client = WhatsAppClient(
            self._token,
            self._phone_number_id,
            version=settings.WHATSAPP_GRAPH_VERSION,
            timeout=settings.HTTP_TIMEOUT_SECONDS,
            max_message_length=settings.WHATSAPP_MAX_MESSAGE_LENGTH,
        )
        try:
            ids = await self._client.send_text(self._recipient, self._render(approval))
            return NotifyReceipt(
                channel="whatsapp",
                delivered=bool(ids),
                provider_message_id=ids[0] if ids else None,
            )
        except Exception as exc:
            # outside the 24-hour window Meta refuses free-form outright. That is
            # a normal state for an owner who never writes to their own number,
            # so it is reported as a receipt and the other destinations carry it
            return NotifyReceipt(channel="whatsapp", error=str(exc)[:200])

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
