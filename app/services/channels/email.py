"""Inbound email processing: one message in, at most one durable turn and one send.

A near-mirror of ``app/services/channels/whatsapp.py`` so the two channels stay
legible side by side. The differences are all consequences of email being
asynchronous and permanent rather than conversational:

* ``answer()`` rather than ``stream()``, because there is no token consumer and
  because ``grounded`` has to be a decidable value *before* anything is sent.
* Retrieval and verification are widened for this one call via
  ``model_copy`` — chat behaviour is untouched.
* A reply is only sent when every gate in :func:`decide_email_reply` holds.
  Otherwise the answer becomes a draft attached to an escalation, which is the
  default for a newly connected mailbox: the failure mode of a wrong auto-reply
  is silent, external and permanent.
* The lock is keyed on the sender rather than the thread, and a lost lock
  re-enqueues instead of failing — thirty seconds is invisible in email.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from app.adapters.cache.base import KeyedLock, LockAcquisitionError
from app.adapters.channels.email import SourceLink, render_answer_email
from app.adapters.queue.base import Job
from app.api.deps import load_agent_config
from app.core.constants import EXTENSION_TO_MIME, MAX_JOB_ATTEMPTS
from app.core.errors import (
    AuthenticationError,
    NotFoundError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.core.logging import get_logger
from app.core.security import encrypt_secret, hash_identifier
from app.domain.agent_config import AgentConfig
from app.domain.email_channel import (
    EmailAttachmentRef,
    EmailChannel,
    EmailDeliveryStatus,
    EmailDeliveryUpdate,
    EmailReceiptState,
    EmailReplyMode,
    EmailThread,
    email_channel,
)
from app.domain.email_text import (
    mask_address,
    normalise_address,
    normalise_subject,
    parse_message_ids,
    parse_own_message_id,
    reply_subject,
    strip_quoted,
)
from app.domain.entities import TenantContext
from app.domain.escalation import (
    CONTACT_SOURCE_EMAIL_FROM,
    EscalationTrigger,
    SourceRef,
    VisitorContact,
    wants_human,
)
from app.domain.ports import OutboundEmail, ReceivedEmail
from app.services.audit import (
    AUDIT_EMAIL_ATTACHMENT_INGESTED,
    AUDIT_EMAIL_ATTACHMENT_REJECTED,
    AUDIT_EMAIL_AUTO_REPLIED,
    AUDIT_EMAIL_BOUNCED,
    AUDIT_EMAIL_CHANNEL_SUSPENDED,
    AUDIT_EMAIL_COMPLAINED,
    AUDIT_EMAIL_DRAFTED,
    AUDIT_EMAIL_RECEIVED,
    record,
)

logger = get_logger(__name__)

EMAIL_JOB_KIND = "email_reply"

# platform-wide inbound rate limit, per tenant, per minute
EMAIL_RATE_LIMIT_RPM = 60

# Usage ledger key for one outbound reply. A plain string rather than the
# `UsageKind` enum because the member does not exist yet in `app/core/constants.py`
# and `record_usage` takes the value, not the enum.
EMAIL_USAGE_KIND = "email_sent"
EMPTY_BODY_PLACEHOLDER = "(no message body)"
SPAM_COMPLAINT_REASON = "spam_complaint"

# Transient provider failures are re-raised so the queue retries them; anything
# else is permanent for this recipient and is answered with an escalation rather
# than an infinite retry ladder.
_TRANSIENT_SEND_ERRORS = (
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)

# Which gate declined the auto-reply. Recorded on the audit row and in the
# escalation note, because "the bot drafted instead of sending" is only useful
# to a tenant if they can see why.
GATE_MODE_OFF = "mode_off"
GATE_MODE_DRAFT = "mode_draft"
GATE_BLOCKED = "blocked"
GATE_ANSWER_REASON = "answer_reason"
GATE_CONFIDENCE = "confidence"
GATE_UNGROUNDED = "ungrounded"
GATE_NO_CITATION = "no_citation"
GATE_WANTS_HUMAN = "wants_human"
GATE_REPLY_CAP = "reply_cap"
GATE_MULTI_RECIPIENT = "multi_recipient"
GATE_UNDELIVERABLE = "undeliverable"
GATE_CHANNEL_INACTIVE = "channel_inactive"
GATE_EMPTY_BODY = "empty_body"

# One inbound message can carry many parts, and each accepted part costs a fetch,
# a gate run, a storage write and an ingest job. The number processed per message
# is therefore bounded; the remainder are reported in the reply as not read.
EMAIL_MAX_ATTACHMENTS_PER_MESSAGE = 5

# Why a part was not ingested. Written into the reply footer and the escalation
# note verbatim, so these read as sentences a customer can understand rather than
# as internal codes.
ATTACHMENT_SKIP_DISABLED = "attachments are not read on this mailbox"
ATTACHMENT_SKIP_UNSUPPORTED = "unsupported file type"
ATTACHMENT_SKIP_TOO_LARGE = "larger than the attachment size limit"
ATTACHMENT_SKIP_FETCH_FAILED = "could not be downloaded"
ATTACHMENT_SKIP_REJECTED = "rejected by the upload checks"
ATTACHMENT_SKIP_CAPPED = "beyond the per-message attachment limit"


def _attachment_supported(attachment: EmailAttachmentRef) -> bool:
    """Whether the parser registry has any chance with this part.

    The extension is the cheap pre-filter and the declared content type is the
    second chance; neither is trusted further than this, because `UploadGate`
    resolves the real type from the bytes it reads.
    """
    name = (attachment.filename or "").strip().lower()
    suffix = name[name.rfind(".") :] if "." in name else ""
    if suffix in EXTENSION_TO_MIME:
        return True
    declared = (attachment.content_type or "").split(";", 1)[0].strip().lower()
    return declared in set(EXTENSION_TO_MIME.values())


@dataclass(frozen=True, slots=True)
class AttachmentReport:
    """What became of each part of one inbound message.

    An unreadable attachment is never a reason to leave a customer unanswered
    (§C4), so every outcome here is reported rather than raised.
    """

    ingested: tuple[str, ...] = ()
    duplicates: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    # exact rejection reasons, kept apart from `skipped` because those are read by
    # the customer and these are read by the tenant: a gate message is internal
    # text and belongs in the escalation note and the audit row, not in a reply
    rejections: tuple[str, ...] = ()

    @property
    def seen(self) -> bool:
        return bool(self.ingested or self.duplicates or self.skipped)

    @property
    def note(self) -> str:
        """One line for the reply footer."""
        parts: list[str] = []
        if self.ingested:
            parts.append(f"added to our knowledge base: {', '.join(self.ingested)}")
        if self.duplicates:
            parts.append(f"already on file: {', '.join(self.duplicates)}")
        if self.skipped:
            parts.append(f"not read: {'; '.join(self.skipped)}")
        if not parts:
            return ""
        return "Attachments — " + ". ".join(parts) + "."

    @property
    def escalation_note(self) -> str:
        """The same line, plus the reason each rejected part was refused (§C4)."""
        note = self.note
        if not self.rejections:
            return note
        detail = "attachment rejections: " + "; ".join(self.rejections)
        return f"{note} {detail}".strip()


@dataclass(frozen=True, slots=True)
class EmailReplyDecision:
    """The outcome of the §B4 gate ladder."""

    send: bool
    escalate: bool
    trigger: str
    failed_gates: tuple[str, ...] = ()

    @property
    def drafted(self) -> bool:
        return not self.send


def _gate_trigger(gates: tuple[str, ...]) -> str:
    """Map the failed gates onto the trigger a human will read.

    Order is by how much it tells the reader, not by how the gates are listed.
    A message that asked for a person outranks the reason the model gave up.
    """
    if GATE_WANTS_HUMAN in gates:
        return EscalationTrigger.USER_REQUESTED
    if GATE_UNDELIVERABLE in gates:
        return EscalationTrigger.EMAIL_UNDELIVERABLE
    if GATE_REPLY_CAP in gates:
        return EscalationTrigger.EMAIL_THREAD_LOOP
    if GATE_UNGROUNDED in gates:
        return EscalationTrigger.UNGROUNDED
    if GATE_CONFIDENCE in gates or GATE_ANSWER_REASON in gates or GATE_BLOCKED in gates:
        return EscalationTrigger.LOW_CONFIDENCE
    # Every other case is a healthy answer handed over deliberately — `draft`
    # mode, a CC'd thread, a suspended channel. `model_requested` would say "the
    # agent asked for a human", which is a lie about why the row exists: nothing
    # went wrong and the agent asked for nothing. `draft` says what happened.
    # It bypasses the tenant trigger list in EscalationService.decide, because
    # the channel setting is the opt-in. See migration 011.
    return EscalationTrigger.DRAFT


def decide_email_reply(
    result: Any,
    config: AgentConfig,
    thread: EmailThread | None,
    channel: EmailChannel,
    *,
    question: str,
    recipient_count: int,
    max_replies: int,
) -> EmailReplyDecision:
    """Every condition must hold for an auto-reply. Otherwise: draft and escalate.

    Deliberately a pure function of already-fetched state so the gate can be read
    in one screen and changed without touching the orchestration around it.
    """
    rules = config.escalation
    gates: list[str] = []

    if rules.email_reply_mode == str(EmailReplyMode.OFF):
        gates.append(GATE_MODE_OFF)
    elif rules.email_reply_mode != str(EmailReplyMode.AUTO):
        gates.append(GATE_MODE_DRAFT)

    if getattr(result, "blocked", False):
        gates.append(GATE_BLOCKED)
    if getattr(result, "reason", ""):
        gates.append(GATE_ANSWER_REASON)
    if float(getattr(result, "confidence", 0.0)) < rules.email_auto_reply_min_confidence:
        gates.append(GATE_CONFIDENCE)
    # A verifier that could not run must not count as a pass. `_verify` returns
    # True on exception, which is right for chat — the text is already streamed —
    # and wrong for email's strictest gate (§C4): a provider timeout would
    # otherwise let an unverifiable answer out. Tri-state, deliberately: `None`
    # means no verifier ran at all, which is not a failure, so a deployment with
    # no groundedness check configured still replies. Only an explicit `False` is
    # a failed verification. `getattr` because the field is optional on the
    # result object.
    verified = getattr(result, "verified", None)
    if rules.email_require_grounded and (
        not getattr(result, "grounded", False) or verified is False
    ):
        gates.append(GATE_UNGROUNDED)
    if rules.email_require_citation and not getattr(result, "citations", ()):
        gates.append(GATE_NO_CITATION)
    if wants_human(question):
        gates.append(GATE_WANTS_HUMAN)
    if question.strip() in ("", EMPTY_BODY_PLACEHOLDER):
        gates.append(GATE_EMPTY_BODY)
    if thread is not None and thread.agent_reply_count >= max_replies:
        gates.append(GATE_REPLY_CAP)
    if recipient_count > 1:
        gates.append(GATE_MULTI_RECIPIENT)
    if thread is not None and not thread.deliverable:
        gates.append(GATE_UNDELIVERABLE)
    if not channel.is_active:
        gates.append(GATE_CHANNEL_INACTIVE)

    failed = tuple(gates)
    if not failed:
        return EmailReplyDecision(send=True, escalate=False, trigger="", failed_gates=())
    return EmailReplyDecision(
        send=False, escalate=True, trigger=_gate_trigger(failed), failed_gates=failed
    )


def _tune_for_email(config: AgentConfig) -> AgentConfig:
    """Wider retrieval and hard groundedness, for this one call only.

    Email has no time-to-first-token to protect, so it can afford to think
    longer; and it has nothing already streamed to excuse an unverified claim, so
    groundedness is upgraded from advisory to blocking. Rerank is on for the
    same reason — the extra pass costs latency the mail channel does not have a
    budget for — and `RetrievalConfig`'s `_rerank_within_top_k` validator keeps
    `rerank_top_n` sane by itself.
    """
    retrieval = config.retrieval.model_copy(
        update={
            "top_k": min(config.retrieval.top_k * 2, 60),
            "rerank_enabled": True,
            "max_context_chunks": min(config.retrieval.max_context_chunks + 4, 20),
        }
    )
    guardrails = config.guardrails.model_copy(
        update={"groundedness_check": True, "groundedness_mode": "block"}
    )
    return config.model_copy(update={"retrieval": retrieval, "guardrails": guardrails})


def _sources(citations: Any, *, link_sources: bool) -> tuple[SourceLink, ...]:
    """Citations rendered as the Sources block, with links only when opted in.

    A signed document URL is short-lived by design, so putting one in an
    indefinitely-retained, forwardable store is a document-access token left
    lying around. Default is filename and page, no link.
    """
    links: list[SourceLink] = []
    for citation in citations or ():
        filename = str(getattr(citation, "filename", "") or "").strip()
        page = getattr(citation, "page_from", None)
        label = filename or "source"
        if isinstance(page, int) and page > 0:
            label = f"{label}, p. {page}"
        url = getattr(citation, "url", None) if link_sources else None
        links.append(SourceLink(label=label, url=str(url) if url else None))
    return tuple(links)


def _considered(citations: Any) -> tuple[SourceRef, ...]:
    """What the agent retrieved, for the escalation payload."""
    refs: list[SourceRef] = []
    for citation in citations or ():
        page = getattr(citation, "page_from", None)
        document_id = getattr(citation, "document_id", None)
        refs.append(
            SourceRef(
                filename=str(getattr(citation, "filename", "") or ""),
                page_from=int(page) if isinstance(page, int) else 0,
                document_id=document_id if isinstance(document_id, UUID) else None,
            )
        )
    return tuple(refs)


def _required(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise NotFoundError(f"email job payload is missing {key}")
    return value


class EmailService:
    """Consumes one queue payload and produces one durable email turn."""

    def __init__(self, container: Any) -> None:
        self._c = container

    async def process_reply(self, tenant_id: UUID, payload: dict[str, Any]) -> dict[str, Any]:
        provider_id = _required(payload, "provider_id")
        address = _required(payload, "address")
        message_id = _required(payload, "message_id")

        repo = self._c.email_repo
        channel = await repo.resolve_active_by_address(address)
        if channel is None or channel.tenant_id != tenant_id:
            raise NotFoundError("email channel is unavailable")

        provider = getattr(self._c, "email", None)
        if provider is None:
            # Same posture as a None reranker or a None OCR engine: nothing
            # crashes, the receipt says why, and the tenant sees the channel fail
            # rather than silently swallowing every customer email.
            logger.warning("email_backend_unconfigured", tenant_id=str(tenant_id))
            await repo.set_inbound_receipt_state(
                tenant_id,
                message_id,
                EmailReceiptState.FAILED,
                error="email_backend_unconfigured",
            )
            return {"status": "skipped", "reason": "email_backend_unconfigured"}

        from app.adapters.postgres.platform_repo import get_tenant_by_id

        tenant_record = await get_tenant_by_id(tenant_id)
        if tenant_record is None or not tenant_record.is_active:
            raise AuthenticationError("tenant is unavailable")
        # the inbound rate limit is an abuse control rather than a feature gate
        await self._c.rate_limiter.check(
            "email", str(tenant_id), EMAIL_RATE_LIMIT_RPM, 60
        )

        try:
            outcome = await self._handle(
                tenant_id=tenant_id,
                channel=channel,
                tenant_slug=tenant_record.slug,
                provider=provider,
                provider_id=provider_id,
                message_id=message_id,
                payload=payload,
            )
        except LockAcquisitionError:
            return await self._requeue(tenant_id, channel, payload)
        except Exception as exc:
            await repo.set_inbound_receipt_state(
                tenant_id, message_id, EmailReceiptState.FAILED, error=type(exc).__name__
            )
            raise

        state = (
            EmailReceiptState.IGNORED
            if outcome.get("status") == "ignored"
            else EmailReceiptState.PROCESSED
        )
        await repo.set_inbound_receipt_state(
            tenant_id,
            message_id,
            state,
            ignored_reason=outcome.get("reason") if state == EmailReceiptState.IGNORED else None,
        )
        return outcome

    async def _growth_reply(
        self, tenant_id: UUID, sender: str, question: str, message_id: str
    ) -> dict[str, Any] | None:
        """Route an outbound-thread reply to the growth reply agent, or None.

        Wrapped in its own try/except rather than trusting the caller's: if the
        growth half is broken, an inbound customer email must still reach the
        support agent. Failing open to support is the safe direction — the worst
        case is a sales reply answered in a support voice, and the alternative
        worst case is a customer email dropped.

        `message_id` is passed as the provider id, and it is not decoration: it
        is the only thing that makes the reply agent's redelivery guard work on
        this channel. That guard fires when the `growth_messages` insert collides
        on `(tenant_id, channel, provider_id)` — with no id it can never collide,
        and a redelivered `email_reply` job would re-classify the message, draft
        a second reply and ping the owner twice.
        """
        service = getattr(self._c, "growth_replies", None)
        if service is None:
            return None
        try:
            return await service.maybe_handle_email(  # type: ignore[no-any-return]
                tenant_id, sender=sender, body=question, provider_id=message_id
            )
        except Exception:
            logger.warning(
                "growth_email_branch_failed", tenant_id=str(tenant_id), exc_info=True
            )
            return None

    async def _requeue(
        self, tenant_id: UUID, channel: EmailChannel, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """A busy sender is a delay, not a failure — thirty seconds is invisible here.

        Past `MAX_JOB_ATTEMPTS` the message is handed to a human rather than
        dropped: something is holding that sender's lock for a very long time.
        """
        attempt = int(payload.get("attempt") or 0) + 1
        if attempt < MAX_JOB_ATTEMPTS:
            await self._c.queue.enqueue(
                Job(
                    kind=EMAIL_JOB_KIND,
                    tenant_id=tenant_id,
                    payload={**payload, "attempt": attempt},
                    attempt=attempt,
                    delay_seconds=30,
                )
            )
            return {"status": "requeued", "attempt": attempt}

        config = await self._config_only(tenant_id, channel)
        if config is not None:
            await self._escalate(
                tenant_id=tenant_id,
                channel=channel,
                config=config,
                trigger=EscalationTrigger.EMAIL_THREAD_LOOP,
                question=payload.get("address", ""),
                note="the sender lock could not be acquired after every attempt",
            )
        return {"status": "abandoned", "reason": "lock_unavailable"}

    async def _handle(
        self,
        *,
        tenant_id: UUID,
        channel: EmailChannel,
        tenant_slug: str,
        provider: Any,
        provider_id: str,
        message_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        settings = self._c.settings
        repo = self._c.email_repo

        received = await provider.fetch_received(provider_id)
        # Strip before masking, and before truncating. A forty-message thread run
        # through mask_pii first produces hundreds of tokens and a restore() that
        # rewrites the reply unpredictably.
        stripped = strip_quoted(received.text or "", received.html)
        question = (
            stripped.text[: int(settings.EMAIL_MAX_BODY_CHARS)].strip() or EMPTY_BODY_PLACEHOLDER
        )
        sender = normalise_address(received.from_address)
        sender_ref = hash_identifier(sender, f"{settings.SECRET_KEY}:{tenant_id}")
        subject_normalised = normalise_subject(received.subject)

        await record(
            AUDIT_EMAIL_RECEIVED,
            actor_type="system",
            target_tenant=tenant_id,
            target_type="email_channel",
            target_id=str(channel.id),
            detail={
                "sender": mask_address(sender),
                "subject": subject_normalised[:200],
                "stripped_chars": stripped.stripped_chars,
                "method": stripped.method,
                "confident": stripped.confident,
            },
        )

        if await repo.is_suppressed(tenant_id, sender_ref):
            return {"status": "ignored", "reason": "suppressed"}

        # The growth branch. A reply from somebody this tenant is *mailing* is a
        # sales conversation, not a support question, and answering it with the
        # RAG agent would be the wrong voice, the wrong content and — for an
        # "unsubscribe" — the wrong action entirely.
        #
        # `maybe_handle_email` returns None for anyone who is not a growth lead,
        # and that is the whole safety property: the support path below is
        # untouched for every existing tenant, and a growth service outage falls
        # back to it rather than swallowing a customer's mail.
        growth = await self._growth_reply(tenant_id, sender, question, message_id)
        if growth is not None:
            return growth

        # §B2 step 7, and outside the thread lock on purpose: the fetch, the gate
        # and the storage write are per message rather than per thread, and holding
        # a thread lease through a ten-megabyte download would serialise a whole
        # mailbox behind one slow attachment.
        attachments = await self._ingest_attachments(
            tenant_id=tenant_id,
            channel=channel,
            provider=provider,
            provider_id=provider_id,
            received=received,
        )

        async with KeyedLock(
            self._c.cache,
            f"t:{tenant_id}:email-thread:{sender_ref}",
            ttl_seconds=int(settings.EMAIL_LOCK_TTL_SECONDS),
            wait_timeout=float(settings.EMAIL_LOCK_WAIT_SECONDS),
        ):
            thread = await self._resolve_thread(
                tenant_id,
                channel,
                received,
                sender_ref=sender_ref,
                subject_normalised=subject_normalised,
            )
            tenant = TenantContext(
                tenant_id=tenant_id,
                tenant_slug=tenant_slug,
                channel=email_channel(),
                agent_id=channel.agent_id,
                external_user_ref=sender_ref,
            )
            _, config = await load_agent_config(tenant, self._c, channel.agent_id)
            email_config = _tune_for_email(config)

            try:
                result = await self._c.agent.answer(
                    tenant_id=tenant_id,
                    config=email_config,
                    question=question,
                    agent_id=channel.agent_id,
                    conversation_id=None if thread is None else thread.conversation_id,
                    channel=email_channel(),
                    external_user_ref=sender_ref,
                )
            except ProviderError:
                # No customer reply, but not silence either: a human is told the
                # model was unavailable and can answer by hand.
                await self._escalate(
                    tenant_id=tenant_id,
                    channel=channel,
                    config=email_config,
                    trigger=EscalationTrigger.MODEL_REQUESTED,
                    question=question,
                    conversation_id=None if thread is None else thread.conversation_id,
                    sender=sender,
                    sender_name=received.from_name,
                    external_user_ref=sender_ref,
                    note="model unavailable",
                    )
                raise

            conversation_id = getattr(result, "conversation_id", None)
            if conversation_id is None:  # pragma: no cover - answer() always sets it
                raise ProviderError("email", "the answer produced no conversation")

            if thread is None:
                thread = await repo.upsert_thread(
                    tenant_id,
                    channel.id,
                    conversation_id,
                    external_user_ref=sender_ref,
                    participant_ciphertext=encrypt_secret(sender, settings.ENCRYPTION_KEY),
                    participant_masked=mask_address(sender),
                    subject_normalised=subject_normalised,
                    message_ids=[received.message_id],
                    last_message_id=received.message_id,
                )
            else:
                thread = (
                    await repo.append_message_id(
                        tenant_id,
                        thread.id,
                        received.message_id,
                        inbound=True,
                        subject_normalised=subject_normalised or None,
                    )
                    or thread
                )

            decision = decide_email_reply(
                result,
                email_config,
                thread,
                channel,
                question=question,
                recipient_count=self._recipient_count(received, channel),
                max_replies=int(settings.EMAIL_MAX_REPLIES_PER_THREAD),
            )

            if decision.send:
                return await self._send_reply(
                    tenant_id=tenant_id,
                    channel=channel,
                    config=email_config,
                    thread=thread,
                    received=received,
                    result=result,
                    sender=sender,
                        attachment_note=attachments.note,
                )
            return await self._draft(
                tenant_id=tenant_id,
                channel=channel,
                config=email_config,
                thread=thread,
                received=received,
                result=result,
                decision=decision,
                question=question,
                sender=sender,
                sender_ref=sender_ref,
                attachment_note=attachments.escalation_note,
            )

    async def _resolve_thread(
        self,
        tenant_id: UUID,
        channel: EmailChannel,
        received: ReceivedEmail,
        *,
        sender_ref: str,
        subject_normalised: str,
    ) -> EmailThread | None:
        """Headers first, our own Message-ID shape before any lookup at all.

        Merging two unrelated threads is worse than splitting one: a split costs a
        human a second glance, a merge poisons the rolling summary and the
        retrieved context with somebody else's problem. Hence the subject
        fallback is scoped to one sender, one channel and one time window.
        """
        repo = self._c.email_repo
        candidates = list(parse_message_ids(received.in_reply_to)) + list(received.references)

        for candidate in candidates:
            parsed = parse_own_message_id(candidate)
            if parsed is None:
                continue
            try:
                thread_id = UUID(hex=parsed[0])
            except ValueError:
                continue
            thread = await repo.get_thread(tenant_id, thread_id)
            if thread is not None and thread.channel_id == channel.id:
                return cast(EmailThread, thread)

        resolved = await repo.resolve_thread(
            tenant_id,
            channel.id,
            candidate_message_ids=candidates,
            external_user_ref=sender_ref,
            subject_normalised=subject_normalised,
            window_hours=int(self._c.settings.EMAIL_THREAD_WINDOW_HOURS),
        )
        return cast("EmailThread | None", resolved)

    @staticmethod
    def _recipient_count(received: ReceivedEmail, channel: EmailChannel) -> int:
        """How many humans are on this thread besides us.

        A CC'd thread means several people, possibly the tenant's own staff
        mid-discussion. Replying into that is at best noise.
        """
        own = {normalise_address(channel.address)}
        others = {
            normalise_address(address)
            for address in tuple(received.to) + tuple(received.cc)
            if address
        }
        return max(len(others - own), 1)

    async def _meter_send(self, tenant_id: UUID) -> None:
        """Metered, never billed at list rate.

        Same posture as the escalation counter: outbound volume has to be visible
        in the usage breakdown before it becomes a deliverability problem, and a
        failed meter must never cost the tenant a reply that was already sent.
        """
        telemetry = getattr(self._c, "telemetry", None)
        if telemetry is None:
            return
        try:
            await telemetry.record_usage(tenant_id, EMAIL_USAGE_KIND, 1)
        except Exception:
            logger.debug("email_usage_not_recorded", exc_info=True)

    # --------------------------------------------------------- attachments

    @staticmethod
    def _part_name(attachment: EmailAttachmentRef) -> str:
        """A display name that is safe in a reply and never a path."""
        raw = (attachment.filename or "attachment").replace("\\", "/").rsplit("/", 1)[-1]
        clean = raw.strip() or "attachment"
        return clean[:120]

    async def _ingest_attachments(
        self,
        *,
        tenant_id: UUID,
        channel: EmailChannel,
        provider: Any,
        provider_id: str,
        received: ReceivedEmail,
    ) -> AttachmentReport:
        """§B2 step 7 — a customer's attachments, through the existing upload path.

        `UploadGate` is reused rather than trusted metadata being believed, because
        the gate is where magic-byte validation, the zip-bomb ratio check and the
        upload limits live, and an attachment is the least trusted input in the whole
        system. Indexing is enqueued and never inline: the customer's reply must
        not wait on a parse.

        Nothing in here raises. An unreadable attachment is noted in the reply and
        the escalation, and the answer is still sent (§C4).
        """
        parts = tuple(received.attachments or ())
        if not parts:
            return AttachmentReport()
        if not channel.ingest_attachments:
            # The documented default: metadata only, and the reply says so, because
            # a customer who attached an invoice needs to know it was not read.
            return AttachmentReport(
                skipped=tuple(
                    f"{self._part_name(part)} ({ATTACHMENT_SKIP_DISABLED})" for part in parts
                )
            )

        from app.adapters.storage.base import document_path
        from app.api.v1.documents import url_recrawl_quota
        from app.services.ingestion.gate import bytes_stream

        settings = self._c.settings
        # never above the manual-upload ceiling: a larger attachment limit would be
        # a way to post bytes the upload endpoint itself would refuse
        max_bytes = min(int(settings.EMAIL_ATTACHMENT_MAX_BYTES), int(settings.MAX_UPLOAD_BYTES))
        # read once per message rather than once per part: the same quota the
        # recrawl task uses, since neither path has a request to resolve it from
        quota = await url_recrawl_quota(self._c, tenant_id)

        ingested: list[str] = []
        duplicates: list[str] = []
        skipped: list[str] = []
        rejections: list[str] = []

        for index, part in enumerate(parts):
            name = self._part_name(part)

            if index >= EMAIL_MAX_ATTACHMENTS_PER_MESSAGE:
                skipped.append(f"{name} ({ATTACHMENT_SKIP_CAPPED})")
                continue
            if not _attachment_supported(part):
                skipped.append(f"{name} ({ATTACHMENT_SKIP_UNSUPPORTED})")
                await self._audit_attachment(
                    tenant_id, channel, name, rejected=ATTACHMENT_SKIP_UNSUPPORTED
                )
                continue
            if int(part.size_bytes or 0) > max_bytes:
                skipped.append(f"{name} ({ATTACHMENT_SKIP_TOO_LARGE})")
                await self._audit_attachment(
                    tenant_id, channel, name, rejected=ATTACHMENT_SKIP_TOO_LARGE
                )
                continue

            try:
                data = await provider.fetch_attachment(provider_id, part.attachment_id)
            except Exception as exc:
                logger.warning(
                    "email_attachment_fetch_failed",
                    tenant_id=str(tenant_id),
                    error=type(exc).__name__,
                )
                skipped.append(f"{name} ({ATTACHMENT_SKIP_FETCH_FAILED})")
                rejections.append(f"{name}: {type(exc).__name__}")
                await self._audit_attachment(
                    tenant_id, channel, name, rejected=ATTACHMENT_SKIP_FETCH_FAILED
                )
                continue

            try:
                gated = await self._c.gate.run(
                    tenant_id,
                    name,
                    bytes_stream(bytes(data or b"")),
                    quota,
                    max_bytes=max_bytes,
                )
            except Exception as exc:
                # bad magic bytes, a zip bomb, an exhausted upload quota, an empty
                # part. The gate's reason is the tenant's diagnosis, so it goes to
                # the escalation note and the audit row, not into the reply.
                reason = str(exc) or type(exc).__name__
                logger.info(
                    "email_attachment_rejected",
                    tenant_id=str(tenant_id),
                    error=type(exc).__name__,
                )
                skipped.append(f"{name} ({ATTACHMENT_SKIP_REJECTED})")
                rejections.append(f"{name}: {reason[:200]}")
                await self._audit_attachment(tenant_id, channel, name, rejected=reason[:200])
                continue

            try:
                existing = await self._c.documents.find_by_hash(tenant_id, gated.sha256)
                if existing is not None:
                    # identical bytes are already indexed; say so and skip paying
                    # to parse them a second time
                    duplicates.append(gated.filename)
                    continue

                document = await self._c.documents.create(
                    tenant_id=tenant_id,
                    filename=gated.filename,
                    mime=gated.mime,
                    size_bytes=gated.size_bytes,
                    sha256=gated.sha256,
                    storage_path="",
                    agent_id=channel.agent_id,
                    page_count=gated.page_count,
                    # provenance rides in the existing metadata bag rather than new
                    # columns, exactly as the crawl path records its source
                    metadata={
                        "origin": "email_attachment",
                        "email_channel_id": str(channel.id),
                        "email_message_id": received.message_id,
                        "received_at": datetime.now(UTC).isoformat(),
                    },
                )
                path = document_path(tenant_id, document.id)
                await self._c.storage.put(path, gated.data, gated.mime)
                await self._c.queue.enqueue(
                    Job(
                        kind="ingest_document",
                        tenant_id=tenant_id,
                        payload={"document_id": str(document.id), "storage_path": path},
                    )
                )
            except Exception as exc:
                logger.warning(
                    "email_attachment_ingest_failed",
                    tenant_id=str(tenant_id),
                    error=type(exc).__name__,
                )
                skipped.append(f"{name} ({ATTACHMENT_SKIP_FETCH_FAILED})")
                rejections.append(f"{name}: {type(exc).__name__}")
                await self._audit_attachment(
                    tenant_id, channel, name, rejected=type(exc).__name__
                )
                continue

            ingested.append(gated.filename)
            await self._audit_attachment(
                tenant_id,
                channel,
                gated.filename,
                document_id=str(document.id),
                mime=gated.mime,
                size_bytes=gated.size_bytes,
            )

        return AttachmentReport(
            ingested=tuple(ingested),
            duplicates=tuple(duplicates),
            skipped=tuple(skipped),
            rejections=tuple(rejections),
        )

    async def _audit_attachment(
        self,
        tenant_id: UUID,
        channel: EmailChannel,
        filename: str,
        *,
        rejected: str | None = None,
        document_id: str | None = None,
        mime: str = "",
        size_bytes: int = 0,
    ) -> None:
        """One audit row per part, accepted or refused.

        The tenant has to be able to answer "did you read what I sent you" months
        later, and the refusal reason is the only thing that explains a document
        that never appeared in the workspace.
        """
        detail: dict[str, Any] = {"filename": filename, "channel_id": str(channel.id)}
        if rejected is not None:
            detail["reason"] = rejected
        else:
            detail.update({"document_id": document_id, "mime": mime, "size_bytes": size_bytes})
        try:
            await record(
                AUDIT_EMAIL_ATTACHMENT_REJECTED if rejected else AUDIT_EMAIL_ATTACHMENT_INGESTED,
                actor_type="system",
                target_tenant=tenant_id,
                target_type="email_channel",
                target_id=str(channel.id),
                detail=detail,
            )
        except Exception:
            logger.debug("email_attachment_audit_failed", exc_info=True)

    async def _send_reply(
        self,
        *,
        tenant_id: UUID,
        channel: EmailChannel,
        config: AgentConfig,
        thread: EmailThread,
        received: ReceivedEmail,
        result: Any,
        sender: str,
        attachment_note: str = "",
    ) -> dict[str, Any]:
        settings = self._c.settings
        repo = self._c.email_repo
        reply_seq = await repo.next_reply_seq(tenant_id, thread.id)
        show_citations = bool(getattr(config.widget, "show_citations", True))
        # the attachment line rides at the foot of the body: `render_answer_email`
        # puts the reply marker and the signature after it, so it reads as a
        # postscript rather than as part of the answer
        answer_body = str(getattr(result, "answer", "") or "").strip()
        if attachment_note:
            answer_body = f"{answer_body}\n\n{attachment_note}".strip()
        rendered = render_answer_email(
            answer=answer_body,
            subject=reply_subject(received.subject),
            thread_key=thread.thread_key,
            reply_seq=reply_seq,
            inbound_message_id=received.message_id,
            inbound_references=tuple(received.references),
            inbound_domain=str(settings.EMAIL_INBOUND_DOMAIN),
            loop_token=f"{tenant_id}:{thread.thread_key}",
            signature=config.escalation.email_signature,
            sources=(
                _sources(getattr(result, "citations", ()), link_sources=channel.link_sources)
                if show_citations
                else ()
            ),
            link_sources=channel.link_sources,
            max_chars=int(settings.EMAIL_MAX_REPLY_CHARS),
        )
        message = OutboundEmail(
            to=(sender,),
            subject=rendered.subject,
            text=rendered.text,
            html=rendered.html,
            # The mailbox answers here; whether it can also *send* is a property
            # of the domain, not of this code. When the mailbox domain is
            # receive-only, EMAIL_SEND_FROM_ADDRESS carries the reply under a
            # sendable identity instead - see its comment in config.py. reply_to
            # stays the mailbox either way, so the customer's next message comes
            # back to the address we can actually read.
            from_address=str(settings.EMAIL_SEND_FROM_ADDRESS).strip() or channel.address,
            from_name=channel.display_name or str(settings.EMAIL_FROM_NAME),
            reply_to=(channel.address,),
            headers=rendered.headers,
            # Survives a queue redelivery: the key is derived from the inbound
            # message so a redelivery of the same reply is deduplicated by the
            # provider, and is deliberately NOT derived from `reply_seq` (which
            # changes on retry).
            idempotency_key=f"{thread.id}:{received.message_id or received.provider_id}",
            tags={"channel": "email", "tenant": str(tenant_id)},
        )

        try:
            provider_id = await self._c.email.send(message)
        except _TRANSIENT_SEND_ERRORS:
            raise
        except ProviderError as exc:
            await repo.mark_undeliverable(tenant_id, thread.id)
            await self._escalate(
                tenant_id=tenant_id,
                channel=channel,
                config=config,
                trigger=EscalationTrigger.EMAIL_UNDELIVERABLE,
                question=received.subject,
                answer=str(getattr(result, "answer", "") or ""),
                confidence=float(getattr(result, "confidence", 0.0)),
                considered=_considered(getattr(result, "citations", ())),
                conversation_id=thread.conversation_id,
                sender=sender,
                sender_name=received.from_name,
                external_user_ref=thread.external_user_ref,
                note=f"the reply could not be delivered: {type(exc).__name__}",
            )
            return {"status": "undeliverable", "thread_id": str(thread.id)}

        await repo.append_message_id(tenant_id, thread.id, rendered.message_id, inbound=False)
        await repo.record_delivery(
            tenant_id,
            channel.id,
            EmailDeliveryUpdate(
                provider_id=provider_id,
                status=EmailDeliveryStatus.SENT,
                recipient_masked=mask_address(sender),
                provider_timestamp=datetime.now(UTC),
            ),
        )
        # The compliance-critical row: the only immutable record of what was sent
        # on the tenant's behalf, hashed so the bytes can be proved later.
        await self._meter_send(tenant_id)
        await record(
            AUDIT_EMAIL_AUTO_REPLIED,
            actor_type="system",
            target_tenant=tenant_id,
            target_type="conversation",
            target_id=str(thread.conversation_id),
            detail={
                "provider_id": provider_id,
                "recipient": mask_address(sender),
                "confidence": round(float(getattr(result, "confidence", 0.0)), 4),
                "grounded": bool(getattr(result, "grounded", False)),
                "citations": len(getattr(result, "citations", ()) or ()),
                "model": str(getattr(result, "model", "") or ""),
                "body_sha256": hashlib.sha256(rendered.text.encode("utf-8")).hexdigest(),
                "reply_seq": reply_seq,
            },
        )
        await self._audit_copy(
            tenant_id=tenant_id,
            channel=channel,
            config=config,
            thread=thread,
            received=received,
            result=result,
            sender=sender,
        )
        return {
            "status": "sent",
            "provider_id": provider_id,
            "thread_id": str(thread.id),
            "conversation_id": str(thread.conversation_id),
            "reply_seq": reply_seq,
        }

    async def _audit_copy(
        self,
        *,
        tenant_id: UUID,
        channel: EmailChannel,
        config: AgentConfig,
        thread: EmailThread,
        received: ReceivedEmail,
        result: Any,
        sender: str,
    ) -> None:
        """Copy the tenant on the first sends from a channel, then stop.

        Cheap, bounded and it means nobody discovers what the bot has been saying
        a month later. The budget is decremented in the WHERE clause, so two
        concurrent sends cannot both spend the last credit.
        """
        remaining = await self._c.email_repo.consume_audit_send(tenant_id, channel.id)
        if remaining is None:
            return
        await self._escalate(
            tenant_id=tenant_id,
            channel=channel,
            config=config,
            trigger=EscalationTrigger.MODEL_REQUESTED,
            question=received.subject,
            answer=str(getattr(result, "answer", "") or ""),
            confidence=float(getattr(result, "confidence", 0.0)),
            considered=_considered(getattr(result, "citations", ())),
            conversation_id=thread.conversation_id,
            sender=sender,
            sender_name=received.from_name,
            external_user_ref=thread.external_user_ref,
            note=f"copy of an auto-sent reply ({remaining} audit copies left)",
        )

    async def _draft(
        self,
        *,
        tenant_id: UUID,
        channel: EmailChannel,
        config: AgentConfig,
        thread: EmailThread,
        received: ReceivedEmail,
        result: Any,
        decision: EmailReplyDecision,
        question: str,
        sender: str,
        sender_ref: str,
        attachment_note: str = "",
    ) -> dict[str, Any]:
        """Never writes to the customer. The proposed reply goes to the tenant."""
        gates = ", ".join(decision.failed_gates)
        await record(
            AUDIT_EMAIL_DRAFTED,
            actor_type="system",
            target_tenant=tenant_id,
            target_type="conversation",
            target_id=str(thread.conversation_id),
            detail={
                "confidence": round(float(getattr(result, "confidence", 0.0)), 4),
                "declined_gates": list(decision.failed_gates),
                "trigger": str(decision.trigger),
            },
        )
        # the gate that declined and what happened to the attachments are the two
        # things a human reading this escalation needs, so both go in the note
        note_parts: list[str] = []
        if gates:
            note_parts.append(f"auto-reply declined: {gates}")
        if attachment_note:
            note_parts.append(attachment_note)
        outcome = await self._escalate(
            tenant_id=tenant_id,
            channel=channel,
            config=config,
            trigger=decision.trigger,
            question=question,
            answer=str(getattr(result, "answer", "") or ""),
            confidence=float(getattr(result, "confidence", 0.0)),
            considered=_considered(getattr(result, "citations", ())),
            conversation_id=thread.conversation_id,
            message_id=getattr(result, "message_id", None),
            sender=sender,
            sender_name=received.from_name,
            external_user_ref=sender_ref,
            note=" | ".join(note_parts) or None,
        )
        return {
            "status": "drafted",
            "thread_id": str(thread.id),
            "conversation_id": str(thread.conversation_id),
            "declined_gates": list(decision.failed_gates),
            "escalated": bool(outcome is not None and getattr(outcome, "escalated", False)),
        }

    # ------------------------------------------------------------- callbacks

    async def record_delivery(
        self,
        tenant_id: UUID,
        channel: EmailChannel | None,
        update: EmailDeliveryUpdate,
        *,
        recipient: str = "",
    ) -> dict[str, Any]:
        """Apply one provider callback. Bounces and complaints per §B8.

        `recipient` is the raw address from the webhook, needed because the
        callback names a person rather than a thread and because the suppression
        list is keyed on the same salted hash the inbound path uses. It is hashed
        or masked here and never stored in the clear.
        """
        repo = self._c.email_repo
        await repo.record_delivery(
            tenant_id, None if channel is None else channel.id, update
        )
        if channel is None:
            return {"status": "recorded"}

        masked = mask_address(recipient) if recipient else (update.recipient_masked or "")

        if update.status == EmailDeliveryStatus.COMPLAINED:
            # The hardest stop in the design: at Stage 0 the tenant sends on our
            # shared domain, so one tenant's complaint rate is every tenant's
            # deliverability. Re-enabling is a deliberate dashboard action.
            await repo.mark_channel_error(tenant_id, channel.id, SPAM_COMPLAINT_REASON)
            for action in (AUDIT_EMAIL_COMPLAINED, AUDIT_EMAIL_CHANNEL_SUSPENDED):
                await record(
                    action,
                    actor_type="system",
                    target_tenant=tenant_id,
                    target_type="email_channel",
                    target_id=str(channel.id),
                    detail={"recipient": masked, "reason": SPAM_COMPLAINT_REASON},
                )
            await self._notify_channel_event(
                tenant_id,
                channel,
                note="a spam complaint suspended this channel; sending is stopped "
                "until it is re-enabled",
                recipient=masked,
            )
            return {"status": "suspended", "reason": SPAM_COMPLAINT_REASON}

        if update.status == EmailDeliveryStatus.BOUNCED:
            await record(
                AUDIT_EMAIL_BOUNCED,
                actor_type="system",
                target_tenant=tenant_id,
                target_type="email_channel",
                target_id=str(channel.id),
                detail={
                    "recipient": masked,
                    "bounce_type": update.bounce_type or "",
                    "hard": update.is_hard_bounce,
                },
            )
            if not update.is_hard_bounce:
                # Soft bounce: recorded, no state change, no retry. A retry loop
                # against a full mailbox only harms our sending reputation.
                return {"status": "recorded", "reason": "soft_bounce"}

            thread = None
            if recipient:
                address_hash = hash_identifier(
                    normalise_address(recipient),
                    f"{self._c.settings.SECRET_KEY}:{tenant_id}",
                )
                await repo.add_suppression(
                    tenant_id,
                    address=recipient,
                    address_hash=address_hash,
                    reason="hard_bounce",
                )
                thread = await repo.latest_thread_for_participant(
                    tenant_id, channel.id, address_hash
                )
            if thread is not None:
                await repo.mark_undeliverable(tenant_id, thread.id)
            await self._notify_channel_event(
                tenant_id,
                channel,
                note="a reply hard-bounced, so the customer never received the answer",
                recipient=masked,
                conversation_id=None if thread is None else thread.conversation_id,
                external_user_ref=None if thread is None else thread.external_user_ref,
            )
            return {
                "status": "undeliverable",
                "thread_id": None if thread is None else str(thread.id),
            }

        return {"status": "recorded"}

    async def _notify_channel_event(
        self,
        tenant_id: UUID,
        channel: EmailChannel,
        *,
        note: str,
        recipient: str,
        conversation_id: UUID | None = None,
        external_user_ref: str | None = None,
    ) -> None:
        config = await self._config_only(tenant_id, channel)
        if config is None:
            return
        await self._escalate(
            tenant_id=tenant_id,
            channel=channel,
            config=config,
            trigger=EscalationTrigger.EMAIL_UNDELIVERABLE,
            question=f"{note} ({recipient})",
            conversation_id=conversation_id,
            external_user_ref=external_user_ref,
            note=note,
        )

    # ------------------------------------------------------------- helpers

    async def _escalate(
        self,
        *,
        tenant_id: UUID,
        channel: EmailChannel,
        config: AgentConfig,
        trigger: str,
        question: str,
        answer: str | None = None,
        confidence: float = 0.0,
        considered: tuple[SourceRef, ...] = (),
        conversation_id: UUID | None = None,
        message_id: UUID | None = None,
        sender: str = "",
        sender_name: str | None = None,
        external_user_ref: str | None = None,
        note: str | None = None,
    ) -> Any:
        """One place that talks to EscalationService, and it never raises.

        A broken notification is a lost metric; a failed job is a customer email
        answered twice or not at all.
        """
        service = getattr(self._c, "escalation", None)
        if service is None:
            logger.warning("escalation_service_unconfigured", tenant_id=str(tenant_id))
            return None
        contact = (
            VisitorContact(
                email=sender, name=sender_name or None, source=CONTACT_SOURCE_EMAIL_FROM
            )
            if sender
            else None
        )
        try:
            return await service.maybe_escalate(
                tenant_id=tenant_id,
                config=config,
                trigger=trigger,
                question=question,
                channel=str(email_channel()),
                agent_id=channel.agent_id,
                conversation_id=conversation_id,
                message_id=message_id,
                answer=answer,
                confidence=confidence,
                considered=considered,
                contact=contact,
                external_user_ref=external_user_ref,
                note=note,
            )
        except Exception:
            logger.warning("email_escalation_failed", tenant_id=str(tenant_id), exc_info=True)
            return None

    async def _config_only(
        self, tenant_id: UUID, channel: EmailChannel
    ) -> AgentConfig | None:
        """Load the agent config outside a request, for the callback paths."""
        from app.adapters.postgres.platform_repo import get_tenant_by_id

        try:
            tenant_record = await get_tenant_by_id(tenant_id)
            if tenant_record is None:
                return None
            tenant = TenantContext(
                tenant_id=tenant_id,
                tenant_slug=tenant_record.slug,
                channel=email_channel(),
                agent_id=channel.agent_id,
            )
            _, config = await load_agent_config(tenant, self._c, channel.agent_id)
            return config
        except Exception:
            logger.debug("email_agent_config_unavailable", exc_info=True)
            return None
