"""Escalation decisions and delivery: the moment the agent gives up and a human is told.

Five gates stand between a low-confidence answer and somebody's inbox, and every
one of them exists because of a specific way this feature turns into an incident:

1. **enabled** — off until a tenant turns it on, so a plan upgrade never starts
   mailing people.
2. **plan feature** — `escalation` is plan-gated like every other paid capability.
3. **trigger** — the tenant chooses which triggers are live.
4. **cooldown** — an `INSERT ... ON CONFLICT` on `(tenant_id, dedupe_key)`, so the
   suppression window is a database guarantee and not a cache TTL that fails open.
5. **hourly cap** — counted from the table. One broken document can otherwise
   produce a hundred pages in a minute.

Notification happens out of band, through the queue. Nothing about telling a human
may slow down or break the visitor's answer, and a Slack webhook that takes eleven
seconds must not hold the request open.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.adapters.postgres.escalation_repo import EscalationRow
from app.adapters.queue.base import Job
from app.core.db import tenant_scope
from app.core.logging import get_logger
from app.core.security import decrypt_secret, encrypt_secret
from app.domain.agent_config import AgentConfig, EscalationConfig
from app.domain.escalation import (
    CONTACT_SOURCE_UNKNOWN,
    ESCALATION_MASK_LABELS,
    SUPPRESSED_COOLDOWN,
    SUPPRESSED_DISABLED,
    SUPPRESSED_NO_CONTACT,
    SUPPRESSED_NO_DESTINATION,
    SUPPRESSED_RATE_LIMITED,
    SUPPRESSED_TRIGGER_OFF,
    EscalationDecision,
    EscalationEvent,
    EscalationTrigger,
    SourceRef,
    TranscriptTurn,
    VisitorContact,
)
from app.domain.pii import compile_extra_rules, mask_pii
from app.services.audit import (
    AUDIT_ESCALATION_CREATED,
    AUDIT_ESCALATION_FAILED,
    AUDIT_ESCALATION_NOTIFIED,
    AUDIT_ESCALATION_SUPPRESSED,
    record,
)
from app.services.escalation.render import render_escalation_email, render_slack_blocks

logger = get_logger(__name__)

ESCALATION_JOB_KIND = "escalation_notify"
ESCALATION_USAGE_KIND = "escalation"
SLACK_SECRET_PROVIDER = "slack_webhook"  # noqa: S105 - tenant_secrets provider name

_SLACK_SECRET_SQL = (
    "SELECT ciphertext FROM tenant_secrets "  # noqa: S105 - a query, not a credential
    "WHERE tenant_id = :tenant_id AND ref = :ref AND provider = :provider"
)


class EscalationService:
    """Decides whether to escalate, records it, and tells the humans."""

    def __init__(self, container: Any) -> None:
        self._container = container

    # ---------------------------------------------------------------- decision

    async def maybe_escalate(
        self,
        *,
        tenant_id: UUID,
        config: AgentConfig,
        trigger: EscalationTrigger | str,
        question: str,
        channel: str,
        agent_id: UUID | None = None,
        conversation_id: UUID | None = None,
        message_id: UUID | None = None,
        answer: str | None = None,
        confidence: float = 0.0,
        considered: tuple[SourceRef, ...] = (),
        contact: VisitorContact | None = None,
        external_user_ref: str | None = None,
        page_url: str | None = None,
        note: str | None = None,
    ) -> EscalationDecision:
        """Run the five gates. Never raises — the caller has an answer to deliver.

        A suppressed decision still carries `user_message` empty and
        `collect_contact` false, so a caller can treat "not escalated" as "say
        nothing extra" without inspecting the reason.
        """
        settings = self._container.settings
        rules: EscalationConfig = config.escalation
        trigger_value = str(trigger)

        if not rules.enabled:
            return EscalationDecision(suppressed_reason=SUPPRESSED_DISABLED)

        # `draft` is the one trigger that is not a tenant opt-in. The channel's
        # draft setting is the opt-in, and it has already been read by the time
        # this is called: suppressing here would mean the agent wrote a reply,
        # declined to send it because a human is meant to, and then told nobody.
        # That loses the answer outright, so draft skips the trigger list. Every
        # other trigger still has to be enabled to fire.
        if trigger_value != EscalationTrigger.DRAFT and trigger_value not in set(rules.triggers):
            return EscalationDecision(suppressed_reason=SUPPRESSED_TRIGGER_OFF)

        if not rules.destination_emails and not rules.slack_webhook_secret_ref:
            await self._audit_suppressed(tenant_id, trigger_value, SUPPRESSED_NO_DESTINATION)
            return EscalationDecision(suppressed_reason=SUPPRESSED_NO_DESTINATION)

        if rules.collect_contact_required and (contact is None or contact.is_empty):
            # Not a failure: the caller is expected to ask the visitor and call
            # back. `collect_contact` is what tells the caller to collect the
            # details first.
            return EscalationDecision(
                suppressed_reason=SUPPRESSED_NO_CONTACT,
                collect_contact=True,
                contact_required=True,
            )

        # the cap is an abuse control, not a feature gate: the platform default
        # applies to every tenant, and 0 means uncapped
        cap = self._hourly_cap(settings)
        if cap > 0:
            used = await self._container.escalation_repo.count_recent(tenant_id, hours=1)
            if used >= cap:
                logger.warning(
                    "escalation_rate_limited", tenant_id=str(tenant_id), used=used, cap=cap
                )
                await self._audit_suppressed(tenant_id, trigger_value, SUPPRESSED_RATE_LIMITED)
                return EscalationDecision(suppressed_reason=SUPPRESSED_RATE_LIMITED)

        dedupe_key = self.dedupe_key(
            trigger=trigger_value,
            cooldown_seconds=rules.cooldown_seconds,
            conversation_id=conversation_id,
            external_user_ref=external_user_ref,
        )

        contact_ciphertext: str | None = None
        contact_masked: str | None = None
        contact_source = CONTACT_SOURCE_UNKNOWN
        if contact is not None and not contact.is_empty:
            contact_ciphertext, contact_masked = self._seal_contact(contact)
            contact_source = contact.source

        row = await self._container.escalation_repo.reserve(
            tenant_id,
            trigger=trigger_value,
            channel=channel,
            dedupe_key=dedupe_key,
            question=question,
            confidence=confidence,
            agent_id=agent_id,
            conversation_id=conversation_id,
            message_id=message_id,
            answer=answer,
            considered=considered,
            contact_ciphertext=contact_ciphertext,
            contact_masked=contact_masked,
            contact_source=contact_source,
            external_user_ref=external_user_ref,
            page_url=page_url,
            note=note,
        )
        if row is None:
            return EscalationDecision(suppressed_reason=SUPPRESSED_COOLDOWN)

        await self._meter(tenant_id, trigger_value)
        await record(
            AUDIT_ESCALATION_CREATED,
            actor_type="system",
            target_tenant=tenant_id,
            target_type="escalation",
            target_id=str(row.id),
            detail={
                "trigger": trigger_value,
                "channel": channel,
                "confidence": round(confidence, 3),
                "destinations": len(rules.destination_emails),
                "slack": bool(rules.slack_webhook_secret_ref),
            },
        )

        await self._enqueue_notify(tenant_id, row.id)
        return EscalationDecision(
            escalation_id=row.id,
            user_message=rules.user_message,
            collect_contact=rules.collect_contact and (contact is None or contact.is_empty),
            contact_required=rules.collect_contact_required,
        )

    @staticmethod
    def dedupe_key(
        *,
        trigger: str,
        cooldown_seconds: int,
        conversation_id: UUID | None,
        external_user_ref: str | None,
    ) -> str:
        """Stable within one cooldown bucket, different in the next one.

        Dividing the clock into buckets is what makes a UNIQUE constraint express a
        time window: the same conversation and trigger produce the same key until
        the bucket rolls over, and the second insert loses.
        """
        subject = str(conversation_id) if conversation_id else (external_user_ref or "anonymous")
        window = max(cooldown_seconds, 1)
        bucket = int(time.time()) // window
        return hashlib.sha256(f"{subject}:{trigger}:{bucket}".encode()).hexdigest()

    async def set_contact(
        self, tenant_id: UUID, escalation_id: UUID, contact: VisitorContact
    ) -> bool:
        """Attach a contact the visitor supplied after the escalation was created."""
        if contact.is_empty:
            return False
        ciphertext, masked = self._seal_contact(contact)
        row = await self._container.escalation_repo.set_contact(
            tenant_id,
            escalation_id,
            contact_ciphertext=ciphertext,
            contact_masked=masked,
            contact_source=contact.source,
        )
        if row is None:
            return False
        # The notification has not gone out yet (set_contact only touches pending
        # rows), so re-queueing is what makes the contact reach the human.
        await self._enqueue_notify(tenant_id, escalation_id)
        return True

    # ------------------------------------------------------------- delivery

    async def notify(self, tenant_id: UUID, escalation_id: UUID) -> list[dict[str, Any]]:
        """Send one escalation to every configured destination. Called by the worker.

        Failures are collected, not raised: Slack being down must not stop the
        email, and an escalation that reached nobody has to be visible as `failed`
        rather than lost in a traceback.
        """
        repo = self._container.escalation_repo
        row = await repo.get(tenant_id, escalation_id)
        if row is None:
            logger.warning("escalation_missing", escalation_id=str(escalation_id))
            return []

        config = await self._load_config(tenant_id, row.agent_id)
        rules = config.escalation if config is not None else EscalationConfig()
        event = await self.build_event(tenant_id, row, config)

        receipts: list[dict[str, Any]] = []
        # The budget is per notifier, not per escalation: one hung destination must
        # not eat the time the next one needs. It is applied here as well as inside
        # each notifier's HTTP client because not every destination is a single
        # request — the email notifier can talk to a provider that retries — and a
        # notifier that never returns would otherwise pin this queue job open.
        budget = self._container.settings.ESCALATION_NOTIFY_TIMEOUT_SECONDS
        for notifier in await self._notifiers(tenant_id, rules, config):
            try:
                receipt = await asyncio.wait_for(notifier.notify(event), timeout=budget)
            except TimeoutError:
                # str(TimeoutError()) is empty, so it needs its own message or the
                # receipt would record a failure with no reason
                message = f"timed out after {budget:g}s"
                logger.warning(
                    "escalation_notifier_timeout",
                    channel=notifier.name,
                    escalation_id=str(escalation_id),
                    timeout_seconds=budget,
                )
                receipts.append(
                    {"channel": notifier.name, "delivered": False, "error": message}
                )
                continue
            except Exception as exc:  # a notifier must never break the others
                logger.warning(
                    "escalation_notifier_raised",
                    channel=notifier.name,
                    escalation_id=str(escalation_id),
                    error=str(exc),
                )
                receipts.append(
                    {"channel": notifier.name, "delivered": False, "error": str(exc)[:200]}
                )
                continue
            receipts.append(
                {
                    "channel": receipt.channel,
                    "delivered": receipt.delivered,
                    "provider_message_id": receipt.provider_message_id,
                    "error": receipt.error,
                }
            )

        delivered = any(entry.get("delivered") for entry in receipts)
        if delivered:
            await repo.mark_notified(tenant_id, escalation_id, receipts)
            await record(
                AUDIT_ESCALATION_NOTIFIED,
                actor_type="system",
                target_tenant=tenant_id,
                target_type="escalation",
                target_id=str(escalation_id),
                detail={"receipts": receipts},
            )
        else:
            await repo.mark_failed(
                tenant_id,
                escalation_id,
                receipts,
                note="no destination accepted the notification",
            )
            await record(
                AUDIT_ESCALATION_FAILED,
                actor_type="system",
                target_tenant=tenant_id,
                target_type="escalation",
                target_id=str(escalation_id),
                detail={"receipts": receipts},
            )
        return receipts

    async def build_event(
        self, tenant_id: UUID, row: EscalationRow, config: AgentConfig | None
    ) -> EscalationEvent:
        """Turn a stored row into the payload a notifier renders.

        This is where re-masking happens, and it happens exactly once. The
        transcript, the question and the answer all leave our control from here, so
        `ESCALATION_MASK_LABELS` is applied to every one of them — card and
        government-identifier labels only, because masking the visitor's own email
        would make the escalation unanswerable.
        """
        settings = self._container.settings
        extra = (
            compile_extra_rules(config.guardrails.custom_pii_patterns)
            if config is not None
            else []
        )

        def scrub(value: str) -> str:
            if not value:
                return ""
            return mask_pii(value, extra, only_labels=ESCALATION_MASK_LABELS).text

        contact = self._open_contact(row)
        turns = await self._transcript(
            tenant_id,
            row.conversation_id,
            config.escalation.transcript_turns if config is not None else 6,
        )
        transcript = tuple(
            TranscriptTurn(role=turn.role, content=scrub(turn.content), created_at=turn.created_at)
            for turn in turns
        )

        considered = tuple(
            SourceRef(
                filename=str(entry.get("filename", "")),
                page_from=int(entry.get("page_from") or 0),
                score=float(entry.get("score") or 0.0),
                document_id=_as_uuid(entry.get("document_id")),
            )
            for entry in row.considered
            if isinstance(entry, dict)
        )

        tenant_name, agent_name = await self._names(tenant_id, row.agent_id, config)
        base = (settings.DASHBOARD_URL or settings.PUBLIC_BASE_URL or "").rstrip("/")
        dashboard_url = f"{base}/escalations/{row.id}" if base else ""

        return EscalationEvent(
            id=row.id,
            tenant_id=tenant_id,
            tenant_name=tenant_name,
            agent_id=row.agent_id,
            agent_name=agent_name,
            trigger=EscalationTrigger(row.trigger),
            channel=row.channel,  # type: ignore[arg-type]
            conversation_id=row.conversation_id,
            message_id=row.message_id,
            question=scrub(row.question),
            answer=scrub(row.answer) if row.answer else None,
            confidence=row.confidence,
            considered=considered,
            transcript=transcript,
            contact=contact,
            external_user_ref=row.external_user_ref,
            page_url=row.page_url,
            occurred_at=row.created_at or datetime.now(UTC),
            dashboard_url=dashboard_url,
            note=row.note,
        )

    # -------------------------------------------------------------- internals

    async def _notifiers(
        self, tenant_id: UUID, rules: EscalationConfig, config: AgentConfig | None
    ) -> list[Any]:
        """Build one notifier per configured destination, skipping what is broken.

        Constructed per escalation rather than cached on the container: the Slack
        URL is a tenant secret and the destination list is tenant config, so a
        long-lived notifier would either hold a decrypted credential in memory or
        serve a stale one.
        """
        from app.adapters.notify.email import EmailEscalationNotifier

        notifiers: list[Any] = []
        settings = self._container.settings
        provider = self._container.email
        if rules.destination_emails and provider is not None:
            notifiers.append(
                EmailEscalationNotifier(
                    provider,
                    render_escalation_email,
                    destinations=rules.destination_emails,
                    from_address=settings.EMAIL_FROM_ADDRESS,
                    from_name=settings.EMAIL_FROM_NAME,
                )
            )
        elif rules.destination_emails:
            logger.warning("escalation_email_unconfigured", tenant_id=str(tenant_id))

        if rules.slack_webhook_secret_ref and settings.SLACK_ENABLED:
            url = await self._slack_url(tenant_id, rules.slack_webhook_secret_ref)
            if url:
                from app.adapters.channels.slack import SlackWebhookNotifier

                try:
                    notifiers.append(
                        SlackWebhookNotifier(
                            url,
                            render_slack_blocks,
                            timeout=settings.ESCALATION_NOTIFY_TIMEOUT_SECONDS,
                        )
                    )
                except Exception as exc:
                    # a rejected URL is tenant misconfiguration, not our outage
                    logger.warning(
                        "escalation_slack_url_rejected",
                        tenant_id=str(tenant_id),
                        error=str(exc),
                    )
        _ = config
        return notifiers

    async def _slack_url(self, tenant_id: UUID, ref: str) -> str | None:
        async with tenant_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(_SLACK_SECRET_SQL),
                        {
                            "tenant_id": tenant_id,
                            "ref": ref,
                            "provider": SLACK_SECRET_PROVIDER,
                        },
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            logger.warning("slack_webhook_secret_missing", tenant_id=str(tenant_id), ref=ref)
            return None
        try:
            return decrypt_secret(row["ciphertext"], self._container.settings.ENCRYPTION_KEY)
        except Exception:
            logger.warning(
                "slack_webhook_secret_undecryptable", tenant_id=str(tenant_id), ref=ref
            )
            return None

    def _seal_contact(self, contact: VisitorContact) -> tuple[str, str]:
        """Encrypt the contact, keep a display mask. The plaintext is never stored."""
        from app.domain.email_text import mask_address

        key = self._container.settings.ENCRYPTION_KEY
        payload = "\x1f".join(
            [contact.email or "", contact.name or "", contact.phone or "", contact.source]
        )
        masked = mask_address(contact.email) if contact.email else (contact.phone or "")[-4:]
        return encrypt_secret(payload, key), masked or "***"

    def _open_contact(self, row: EscalationRow) -> VisitorContact | None:
        if not row.contact_ciphertext:
            return None
        try:
            plaintext = decrypt_secret(
                row.contact_ciphertext, self._container.settings.ENCRYPTION_KEY
            )
        except Exception:
            logger.warning("escalation_contact_undecryptable", escalation_id=str(row.id))
            return None
        parts = plaintext.split("\x1f")
        while len(parts) < 4:
            parts.append("")
        return VisitorContact(
            email=parts[0] or None,
            name=parts[1] or None,
            phone=parts[2] or None,
            source=parts[3] or row.contact_source,
        )

    async def _transcript(
        self, tenant_id: UUID, conversation_id: UUID | None, turns: int
    ) -> list[TranscriptTurn]:
        conversations = getattr(self._container, "conversations", None)
        if conversation_id is None or turns <= 0 or conversations is None:
            return []
        try:
            rows = await conversations.recent_messages(
                tenant_id, conversation_id, limit=turns * 2
            )
        except Exception:
            logger.warning("escalation_transcript_unavailable", exc_info=True)
            return []
        return [
            TranscriptTurn(
                role=str(entry.get("role", "")),
                content=str(entry.get("content", "")),
                created_at=entry.get("created_at"),
            )
            for entry in rows
        ]

    async def _names(
        self, tenant_id: UUID, agent_id: UUID | None, config: AgentConfig | None
    ) -> tuple[str, str]:
        from app.adapters.postgres.platform_repo import get_tenant_by_id

        agent_name = config.name if config is not None else "Assistant"
        tenant_name = str(tenant_id)
        try:
            tenant = await get_tenant_by_id(tenant_id)
            if tenant is not None:
                tenant_name = getattr(tenant, "name", None) or tenant.slug
        except Exception:
            logger.debug("escalation_tenant_name_unavailable", exc_info=True)
        _ = agent_id
        return tenant_name, agent_name

    async def _load_config(self, tenant_id: UUID, agent_id: UUID | None) -> AgentConfig | None:
        if agent_id is None:
            return None
        async with tenant_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text("SELECT config FROM agents WHERE id = :id"),
                        {"id": agent_id},
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        raw = row["config"]
        if isinstance(raw, str):
            import json

            try:
                raw = json.loads(raw)
            except ValueError:
                raw = {}
        try:
            return AgentConfig.model_validate(raw or {})
        except Exception:
            logger.warning("escalation_agent_config_invalid", agent_id=str(agent_id))
            return None

    @staticmethod
    def _hourly_cap(settings: Any) -> int:
        """Platform default cap; 0 means uncapped."""
        return int(getattr(settings, "ESCALATION_DEFAULT_HOURLY_CAP", 20))

    async def _meter(self, tenant_id: UUID, trigger: str) -> None:
        """Metered for the usage ledger, never billed: visible in the usage table
        as a record of what the tenant's workspace did."""
        telemetry = getattr(self._container, "telemetry", None)
        if telemetry is None:
            return
        try:
            await telemetry.record_usage(
                tenant_id, ESCALATION_USAGE_KIND, 1, trigger=trigger
            )
        except Exception:
            logger.debug("escalation_usage_not_recorded", exc_info=True)

    async def _enqueue_notify(self, tenant_id: UUID, escalation_id: UUID) -> None:
        queue = getattr(self._container, "queue", None)
        if queue is None:
            # No queue configured: notify inline rather than silently dropping the
            # escalation. Slower, but a page that never arrives is the worse bug.
            await self.notify(tenant_id, escalation_id)
            return
        try:
            await queue.enqueue(
                Job(
                    kind=ESCALATION_JOB_KIND,
                    tenant_id=tenant_id,
                    payload={"escalation_id": str(escalation_id)},
                )
            )
        except Exception:
            logger.warning("escalation_enqueue_failed", escalation_id=str(escalation_id))
            await self.notify(tenant_id, escalation_id)

    async def _audit_suppressed(self, tenant_id: UUID, trigger: str, reason: str) -> None:
        await record(
            AUDIT_ESCALATION_SUPPRESSED,
            actor_type="system",
            target_tenant=tenant_id,
            target_type="escalation",
            detail={"trigger": trigger, "reason": reason},
        )


def _as_uuid(value: object) -> UUID:
    """A missing or malformed document id becomes the nil UUID.

    The escalation is still worth sending: a source reference we cannot link is
    less useful than one we can, and far more useful than no notification at all.
    """
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError:
            return UUID(int=0)
    return UUID(int=0)
