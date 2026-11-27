"""Email channel and escalation settings, for the dashboard.

A separate module from `integrations.py` for the reason the plan gives — that file
is already very large — and because these two screens share every helper in here:
provisioning an inbound address, the destination allowlist, and the Slack webhook
SSRF check.

Two rules shape the whole file. Escalation settings live inside the agent's
`config.escalation` JSONB rather than a table of their own, so they are versioned
and cache-invalidated exactly like every other agent setting. And a Slack webhook
URL is a credential: it goes to `tenant_secrets` encrypted, and only its `ref`
ever reaches the config or a response body.
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import datetime
from typing import Any, cast
from uuid import UUID, uuid4

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from app.adapters.channels.email import loop_suppression_headers
from app.adapters.channels.slack import assert_slack_webhook_url
from app.api.deps import ContainerDep, TenantDep, invalidate_agent_config, require_role
from app.core.db import tenant_scope
from app.core.errors import NotFoundError, ProviderError, ValidationError
from app.core.logging import get_logger
from app.core.security import encrypt_secret
from app.domain.agent_config import AgentConfig, EscalationConfig
from app.domain.email_channel import EmailChannel, EmailChannelStatus, EmailReplyMode
from app.domain.email_text import mask_address, normalise_address
from app.domain.escalation import EscalationTrigger
from app.domain.ports import OutboundEmail
from app.services import audit
from app.services.audit import (
    AUDIT_EMAIL_ADDRESS_ROTATED,
    AUDIT_EMAIL_CHANNEL_CREATED,
    AUDIT_EMAIL_CHANNEL_DISCONNECTED,
    AUDIT_EMAIL_CHANNEL_UPDATED,
    AUDIT_ESCALATION_SETTINGS_UPDATED,
    AUDIT_ESCALATION_SLACK_BLOCKED_SSRF,
)
from app.services.escalation.service import SLACK_SECRET_PROVIDER

logger = get_logger(__name__)

# The local part is `<tenant slug>-<8 hex>`, and inbound routing reads the slug
# back out of it under `admin_scope` before any tenant is known. So the slug in
# the address has to be the tenant's slug byte for byte: sanitising it here would
# route mail to nobody.
_SLUG_SAFE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61})$")

# Random half of the address. Without it the address is guessable from the slug,
# and anybody could mail a tenant's agent directly.
_SUFFIX_BYTES = 4

MAX_DESTINATIONS = 5
MAX_FORWARDERS = 10

router = APIRouter(
    prefix="/integrations",
    tags=["integrations"],
    dependencies=[require_role("owner", "admin")],
)

_MEMBER_EMAILS_SQL = """
    SELECT lower(u.email) AS email
      FROM memberships m
      JOIN users u ON u.id = m.user_id
"""
_VERIFIED_DOMAINS_SQL = """
    SELECT DISTINCT lower(sending_domain) AS domain
      FROM email_channels
     WHERE sending_domain IS NOT NULL
       AND sending_domain_verified_at IS NOT NULL
       AND status <> 'disconnected'
"""
_ACTOR_EMAIL_SQL = "SELECT email FROM users WHERE id = :user_id"
_AGENT_FOR_WRITE_SQL = """
    SELECT id, config, version FROM agents
     WHERE (CAST(:agent_id AS uuid) IS NULL AND is_default)
        OR id = CAST(:agent_id AS uuid)
     ORDER BY created_at
     LIMIT 1
"""
_AGENT_WRITE_SQL = """
    UPDATE agents
       SET config = CAST(:config AS jsonb), version = :version, updated_at = now()
     WHERE id = :id
"""
_INSERT_SECRET_SQL = (
    "INSERT INTO tenant_secrets "  # noqa: S105 - SQL text, not a credential
    "(tenant_id, ref, provider, ciphertext) "
    "VALUES (:tenant_id, :ref, :provider, :ciphertext)"
)
_DELETE_SECRET_SQL = (
    "DELETE FROM tenant_secrets "  # noqa: S105 - SQL text, not a credential
    "WHERE tenant_id = :tenant_id AND ref = :ref AND provider = :provider"
)


class EmailChannelOut(BaseModel):
    """One provisioned mailbox. `address` is ours; `forward_from` is theirs."""

    id: UUID
    agent_id: UUID
    address: str
    forward_from: str | None = None
    display_name: str | None = None
    sending_domain: str | None = None
    sending_domain_verified: bool = False
    allowed_forwarders: list[str] = Field(default_factory=list)
    ingest_attachments: bool = False
    link_sources: bool = False
    # remaining auto-sent replies that also go to the escalation destinations, so
    # a newly trusted mailbox is read by a human for its first few answers
    audit_sends_remaining: int = 0
    status: str = "pending"
    error_reason: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class EmailChannelStateOut(BaseModel):
    """What the Integrations card renders, connected or not."""

    connected: bool = False
    backend: str = "none"
    inbound_domain: str = ""
    channel: EmailChannelOut | None = None
    forwarding_instructions: str = ""


class EmailChannelUpsert(BaseModel):
    """Connect or edit. Absent fields are left alone.

    The address is never accepted from the client: it is provisioned here and
    rotated only through `rotate_address`, because the local part is the routing
    key and a tenant-chosen one either collides or resolves to somebody else.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    agent_id: UUID | None = None
    # the tenant's real support address, the one whose forwarding rule points at
    # ours. Used to attribute a forwarded message and to address the probe.
    forward_from: str | None = Field(default=None, max_length=320)
    display_name: str | None = Field(default=None, max_length=120)
    allowed_forwarders: list[str] | None = Field(default=None, max_length=MAX_FORWARDERS)
    ingest_attachments: bool | None = None
    link_sources: bool | None = None
    rotate_address: bool = False


class EmailVerifyOut(BaseModel):
    sent: bool = False
    status: str = "pending"
    # masked: the probe goes to the signed-in user's own address and there is no
    # reason for the response to restate it in full
    probe_to: str = ""
    provider_message_id: str | None = None
    error: str | None = None


class EscalationSettingsUpsert(BaseModel):
    """Partial write of `config.escalation`. Absent fields are left alone.

    `slack_webhook_url` is write-only and never echoed back. `clear_slack` exists
    because absence means "leave alone" everywhere else in this model, so it
    cannot also mean "remove".
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    agent_id: UUID | None = None
    enabled: bool | None = None
    triggers: list[str] | None = Field(default=None, max_length=20)
    destination_emails: list[str] | None = Field(default=None, max_length=MAX_DESTINATIONS)
    slack_webhook_url: str | None = Field(default=None, max_length=2000)
    clear_slack: bool = False
    cooldown_seconds: int | None = Field(default=None, ge=60, le=86_400)
    user_message: str | None = Field(default=None, max_length=2000)
    collect_contact: bool | None = None
    collect_contact_required: bool | None = None
    transcript_turns: int | None = Field(default=None, ge=0, le=50)
    email_reply_mode: str | None = None
    email_auto_reply_min_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    email_require_grounded: bool | None = None
    email_require_citation: bool | None = None
    email_signature: str | None = Field(default=None, max_length=2000)


class EscalationSettingsOut(BaseModel):
    agent_id: UUID
    version: int
    slack_connected: bool = False
    escalation: dict[str, Any] = Field(default_factory=dict)


def _channel_out(channel: EmailChannel) -> EmailChannelOut:
    return EmailChannelOut(
        id=channel.id,
        agent_id=channel.agent_id,
        address=channel.address,
        forward_from=channel.forward_from,
        display_name=channel.display_name,
        sending_domain=channel.sending_domain,
        sending_domain_verified=channel.sending_domain_verified_at is not None,
        allowed_forwarders=list(channel.allowed_forwarders),
        ingest_attachments=channel.ingest_attachments,
        link_sources=channel.link_sources,
        audit_sends_remaining=channel.audit_sends_remaining,
        status=str(channel.status),
        error_reason=channel.error_reason,
        created_at=channel.created_at,
        updated_at=channel.updated_at,
    )


def _instructions(address: str) -> str:
    """Stage 0 onboarding, in one paragraph the dashboard can print verbatim."""
    return (
        f"In your mail provider, add a rule that forwards the mail you want "
        f"answered to {address}. Keep your own address as the one your customers "
        f"write to — replies are sent from it, not from ours. Nothing is answered "
        f"until you press Verify."
    )


def _provisioned_address(slug: str, domain: str) -> str:
    """`<tenant slug>-<8 hex>@<inbound domain>`, the form inbound routing parses."""
    candidate = (slug or "").strip().lower()
    if not _SLUG_SAFE.match(candidate):
        raise ValidationError(
            "this tenant's slug cannot be used in an email address; contact support"
        )
    return f"{candidate}-{secrets.token_hex(_SUFFIX_BYTES)}@{domain.strip().lower()}"


async def _require_email_backend(container: Any) -> None:
    settings = container.settings
    if str(settings.EMAIL_BACKEND) != "resend" or not settings.EMAIL_INBOUND_DOMAIN:
        raise ValidationError("the email channel is not configured on this deployment")


async def _current_channel(container: Any, tenant: Any) -> EmailChannel | None:
    """The tenant's live channel, if any. Disconnected rows are kept but ignored."""
    channels = await container.email_repo.list_channels(tenant.tenant_id)
    for channel in channels:
        if channel.status != EmailChannelStatus.DISCONNECTED:
            return cast(EmailChannel, channel)
    return None


async def _assert_destinations_allowed(tenant_id: UUID, addresses: list[str]) -> list[str]:
    """Every destination must be a team member or on a verified sending domain.

    Without this the field is an exfiltration primitive and a spam relay: an
    escalation carries the visitor's question, the answer and their contact
    details, and anyone with admin on a trial tenant could point it anywhere.
    """
    cleaned = [normalise_address(value) for value in addresses if value and value.strip()]
    cleaned = [value for value in cleaned if value]
    if not cleaned:
        return []
    if len(cleaned) > MAX_DESTINATIONS:
        raise ValidationError(f"at most {MAX_DESTINATIONS} destinations")

    async with tenant_scope(tenant_id) as session:
        members = {
            row["email"]
            for row in (await session.execute(text(_MEMBER_EMAILS_SQL))).mappings().all()
            if row["email"]
        }
        domains = {
            row["domain"]
            for row in (await session.execute(text(_VERIFIED_DOMAINS_SQL))).mappings().all()
            if row["domain"]
        }

    for value in cleaned:
        domain = value.partition("@")[2]
        if value not in members and domain not in domains:
            raise ValidationError(
                f"{mask_address(value)} is neither a team member nor on a verified domain"
            )
    return cleaned


async def _resolve_agent_id(tenant: Any, agent_id: UUID | None) -> UUID:
    """The agent the mailbox or the settings belong to, checked against RLS.

    A named agent is verified to exist inside `tenant_scope` rather than trusted:
    the id arrives from a browser, and a mailbox attached to another tenant's agent
    would answer their customers with our tenant's documents.
    """
    async with tenant_scope(tenant.tenant_id) as session:
        if agent_id is not None:
            row = (
                (
                    await session.execute(
                        text("SELECT id FROM agents WHERE id = :id"), {"id": agent_id}
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise ValidationError("agent does not belong to this tenant")
            return UUID(str(row["id"]))
        row = (
            (
                await session.execute(
                    text("SELECT id FROM agents WHERE is_default ORDER BY created_at LIMIT 1")
                )
            )
            .mappings()
            .first()
        )
    if row is None:
        raise ValidationError("this tenant has no agent to attach a mailbox to")
    return UUID(str(row["id"]))


async def _actor_email(tenant: Any) -> str:
    """The signed-in user's own address. The verification probe goes only here."""
    async with tenant_scope(tenant.tenant_id) as session:
        row = (
            (await session.execute(text(_ACTOR_EMAIL_SQL), {"user_id": tenant.user_id}))
            .mappings()
            .first()
        )
    email = normalise_address(str(row["email"])) if row and row["email"] else ""
    if not email:
        raise ValidationError("your account has no email address to send the probe to")
    return email


@router.get("/email", response_model=EmailChannelStateOut)
async def get_email_channel(
    tenant: TenantDep, container: ContainerDep
) -> EmailChannelStateOut:
    """The card's whole state, connected or not, in one call."""
    settings = container.settings
    channel = await _current_channel(container, tenant)
    return EmailChannelStateOut(
        connected=channel is not None,
        backend=str(settings.EMAIL_BACKEND),
        inbound_domain=str(settings.EMAIL_INBOUND_DOMAIN or ""),
        channel=_channel_out(channel) if channel is not None else None,
        forwarding_instructions=_instructions(channel.address) if channel is not None else "",
    )


@router.put("/email", response_model=EmailChannelOut)
async def upsert_email_channel(
    payload: EmailChannelUpsert, tenant: TenantDep, container: ContainerDep
) -> EmailChannelOut:
    """Connect the mailbox, or edit it. The address is ours to choose.

    A new channel starts `pending`, not `active`: until the probe in
    `POST /integrations/email/verify` succeeds we have no evidence that the
    tenant's forwarding rule exists, and a channel that claims to be live while
    nothing arrives is worse than one that admits it is not.
    """
    await _require_email_backend(container)

    repo = container.email_repo
    domain = str(container.settings.EMAIL_INBOUND_DOMAIN)
    forwarders = (
        [
            normalise_address(value)
            for value in payload.allowed_forwarders
            if value and value.strip()
        ]
        if payload.allowed_forwarders is not None
        else None
    )
    existing = await _current_channel(container, tenant)

    if existing is None:
        agent_id = await _resolve_agent_id(tenant, payload.agent_id)
        channel = await repo.create_channel(
            tenant.tenant_id,
            agent_id,
            address=_provisioned_address(tenant.tenant_slug, domain),
            forward_from=payload.forward_from,
            display_name=payload.display_name,
            allowed_forwarders=forwarders or [],
            ingest_attachments=bool(payload.ingest_attachments),
            link_sources=bool(payload.link_sources),
            status=EmailChannelStatus.PENDING,
        )
        await audit.record(
            AUDIT_EMAIL_CHANNEL_CREATED,
            actor_type="user",
            actor_id=tenant.user_id,
            target_tenant=tenant.tenant_id,
            target_type="email_channel",
            target_id=str(channel.id),
            detail={"address": channel.address, "agent_id": str(agent_id)},
        )
        return _channel_out(channel)

    rotated = payload.rotate_address
    updated = await repo.update_channel(
        tenant.tenant_id,
        existing.id,
        agent_id=(
            await _resolve_agent_id(tenant, payload.agent_id)
            if payload.agent_id is not None
            else None
        ),
        address=_provisioned_address(tenant.tenant_slug, domain) if rotated else None,
        forward_from=payload.forward_from,
        has_forward_from=payload.forward_from is not None,
        display_name=payload.display_name,
        has_display_name=payload.display_name is not None,
        allowed_forwarders=forwarders,
        ingest_attachments=payload.ingest_attachments,
        link_sources=payload.link_sources,
        # A rotation breaks the tenant's forwarding rule by definition, so the
        # channel goes back to pending and has to be verified again.
        status=EmailChannelStatus.PENDING if rotated else None,
        error_reason=None,
        has_error_reason=rotated,
    )
    if updated is None:
        raise NotFoundError("no email channel is connected")

    await audit.record(
        AUDIT_EMAIL_ADDRESS_ROTATED if rotated else AUDIT_EMAIL_CHANNEL_UPDATED,
        actor_type="user",
        actor_id=tenant.user_id,
        target_tenant=tenant.tenant_id,
        target_type="email_channel",
        target_id=str(updated.id),
        detail={
            "address": updated.address,
            "previous_address": existing.address if rotated else None,
            "fields": sorted(payload.model_dump(exclude_none=True).keys()),
        },
    )
    return _channel_out(updated)


@router.delete("/email", status_code=204)
async def disconnect_email_channel(tenant: TenantDep, container: ContainerDep) -> None:
    """Stop answering. The row is kept, so the threads and receipts still resolve."""
    channel = await _current_channel(container, tenant)
    if channel is None:
        raise NotFoundError("no email channel is connected")
    await container.email_repo.disconnect_channel(tenant.tenant_id, channel.id)
    await audit.record(
        AUDIT_EMAIL_CHANNEL_DISCONNECTED,
        actor_type="user",
        actor_id=tenant.user_id,
        target_tenant=tenant.tenant_id,
        target_type="email_channel",
        target_id=str(channel.id),
        detail={"address": channel.address},
    )


@router.post("/email/verify", response_model=EmailVerifyOut)
async def verify_email_channel(
    tenant: TenantDep, container: ContainerDep
) -> EmailVerifyOut:
    """Prove the sending half, then mark the channel active.

    Send-side only, and deliberately so. A true send-and-receive round trip would
    have to mail our own provisioned address, and the inbound loop guard rejects
    exactly that: anything carrying `X-CA-Loop`, anything auto-submitted, and
    anything whose sender is on our inbound domain. Those rules are what stop two
    robots talking to each other, so the probe cannot be exempted from them
    without opening the hole they exist to close. The receiving half is proven by
    the first real forwarded message, which is audited as `email.received`.

    The probe goes to the signed-in user's own address and nowhere else.
    """
    await _require_email_backend(container)
    channel = await _current_channel(container, tenant)
    if channel is None:
        raise NotFoundError("no email channel is connected")
    provider = container.email
    if provider is None:
        raise ValidationError("no email provider is configured on this deployment")

    recipient = await _actor_email(tenant)
    nonce = secrets.token_hex(4)
    body = (
        "This is a test message from your assistant's mailbox.\n\n"
        f"Your forwarding rule should point at {channel.address}. If this "
        "message reached you, sending works.\n\n"
        f"Reference: {nonce}\n"
    )
    message = OutboundEmail(
        to=(recipient,),
        subject=f"Test message from your assistant [{nonce}]",
        text=body,
        # Same split identity as a real reply: this probe has to be *sendable*
        # for the channel to reach active, which a receive-only mailbox domain
        # can never be. See EMAIL_SEND_FROM_ADDRESS in config.py.
        from_address=str(container.settings.EMAIL_SEND_FROM_ADDRESS).strip()
        or channel.address,
        from_name=channel.display_name or str(container.settings.EMAIL_FROM_NAME),
        reply_to=(channel.address,),
        headers=loop_suppression_headers(f"{tenant.tenant_id}:verify:{nonce}"),
        idempotency_key=f"verify:{channel.id}:{nonce}",
        tags={"channel": "email", "kind": "verify"},
    )

    try:
        provider_id = await provider.send(message)
    except ProviderError as exc:
        # Recorded on the row, not raised: "we tried and the provider said no" is
        # the answer the screen needs, and a 502 here reads as our bug.
        await container.email_repo.mark_channel_error(
            tenant.tenant_id, channel.id, f"verification send failed: {type(exc).__name__}"
        )
        logger.warning(
            "email_verify_failed", tenant_id=str(tenant.tenant_id), error=str(exc)[:200]
        )
        return EmailVerifyOut(
            sent=False,
            status=str(EmailChannelStatus.ERROR),
            probe_to=mask_address(recipient),
            error=str(exc)[:200],
        )

    updated = await container.email_repo.update_channel(
        tenant.tenant_id,
        channel.id,
        status=EmailChannelStatus.ACTIVE,
        error_reason=None,
        has_error_reason=True,
    )
    await audit.record(
        AUDIT_EMAIL_CHANNEL_UPDATED,
        actor_type="user",
        actor_id=tenant.user_id,
        target_tenant=tenant.tenant_id,
        target_type="email_channel",
        target_id=str(channel.id),
        detail={
            "verified": True,
            "provider_id": provider_id,
            "probe_to": mask_address(recipient),
        },
    )
    return EmailVerifyOut(
        sent=True,
        status=str(updated.status if updated is not None else EmailChannelStatus.ACTIVE),
        probe_to=mask_address(recipient),
        provider_message_id=provider_id,
    )


@router.put("/escalation", response_model=EscalationSettingsOut)
async def update_escalation_settings(
    payload: EscalationSettingsUpsert, tenant: TenantDep, container: ContainerDep
) -> EscalationSettingsOut:
    """Write `config.escalation` on one agent.

    Three things happen here that cannot happen in the generic agent PATCH: the
    destination list is checked against the member and verified-domain allowlist,
    the Slack URL is checked against the one-host allowlist and then encrypted into
    `tenant_secrets`, and the old secret is dropped in the same transaction as the
    config that stopped referring to it.
    """
    settings = container.settings

    simple = (
        "enabled",
        "cooldown_seconds",
        "user_message",
        "collect_contact",
        "collect_contact_required",
        "transcript_turns",
        "email_auto_reply_min_confidence",
        "email_require_grounded",
        "email_require_citation",
        "email_signature",
    )
    updates: dict[str, Any] = {}
    for name in simple:
        value = getattr(payload, name)
        if value is not None:
            updates[name] = value

    if payload.triggers is not None:
        allowed = {str(item) for item in EscalationTrigger}
        unknown = [item for item in payload.triggers if item not in allowed]
        if unknown:
            raise ValidationError(f"unknown escalation trigger: {unknown[0]}")
        updates["triggers"] = list(dict.fromkeys(payload.triggers))

    if payload.destination_emails is not None:
        updates["destination_emails"] = await _assert_destinations_allowed(
            tenant.tenant_id, payload.destination_emails
        )

    if payload.email_reply_mode is not None:
        if payload.email_reply_mode not in {str(mode) for mode in EmailReplyMode}:
            raise ValidationError("email_reply_mode must be off, draft or auto")
        updates["email_reply_mode"] = payload.email_reply_mode

    new_ref: str | None = None
    ciphertext: str | None = None
    if payload.slack_webhook_url:
        try:
            assert_slack_webhook_url(payload.slack_webhook_url)
        except ValidationError:
            # The attempt is the interesting part: a rejected URL is either a
            # mistake worth telling the tenant about or an exfiltration attempt
            # worth telling us about.
            await audit.record(
                AUDIT_ESCALATION_SLACK_BLOCKED_SSRF,
                actor_type="user",
                actor_id=tenant.user_id,
                target_tenant=tenant.tenant_id,
                target_type="escalation_settings",
                target_id=None,
                detail={"host": payload.slack_webhook_url[:120]},
            )
            raise
        new_ref = f"slack:{uuid4().hex}"
        ciphertext = encrypt_secret(payload.slack_webhook_url, settings.ENCRYPTION_KEY)

    async with tenant_scope(tenant.tenant_id) as session:
        row = (
            (
                await session.execute(
                    text(_AGENT_FOR_WRITE_SQL), {"agent_id": payload.agent_id}
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise NotFoundError("this tenant has no agent to configure")

        config = AgentConfig.model_validate(row["config"] or {})
        old_ref = config.escalation.slack_webhook_secret_ref
        block = {**config.escalation.model_dump(), **updates}
        if new_ref is not None:
            block["slack_webhook_secret_ref"] = new_ref
        elif payload.clear_slack:
            block["slack_webhook_secret_ref"] = None
        rules = EscalationConfig.model_validate(block)

        version = int(row["version"]) + 1
        merged = config.model_copy(update={"escalation": rules, "version": version})

        if new_ref is not None and ciphertext is not None:
            await session.execute(
                text(_INSERT_SECRET_SQL),
                {
                    "tenant_id": tenant.tenant_id,
                    "ref": new_ref,
                    "provider": SLACK_SECRET_PROVIDER,
                    "ciphertext": ciphertext,
                },
            )
        if old_ref and (new_ref is not None or payload.clear_slack):
            await session.execute(
                text(_DELETE_SECRET_SQL),
                {
                    "tenant_id": tenant.tenant_id,
                    "ref": old_ref,
                    "provider": SLACK_SECRET_PROVIDER,
                },
            )
        await session.execute(
            text(_AGENT_WRITE_SQL),
            {
                "id": row["id"],
                "config": json.dumps(merged.model_dump(mode="json")),
                "version": version,
            },
        )

    agent_id = UUID(str(row["id"]))
    await invalidate_agent_config(container, tenant.tenant_id, agent_id)
    await audit.record(
        AUDIT_ESCALATION_SETTINGS_UPDATED,
        actor_type="user",
        actor_id=tenant.user_id,
        target_tenant=tenant.tenant_id,
        target_type="escalation_settings",
        target_id=str(agent_id),
        detail={
            "fields": sorted(updates.keys()),
            "destinations": len(rules.destination_emails),
            "slack": rules.slack_webhook_secret_ref is not None,
            "slack_rotated": new_ref is not None,
        },
    )

    public = rules.model_dump(mode="json")
    # The ref is not a secret, but nothing renders it and echoing an identifier
    # for a credential is a habit worth not having.
    public.pop("slack_webhook_secret_ref", None)
    return EscalationSettingsOut(
        agent_id=agent_id,
        version=version,
        slack_connected=rules.slack_webhook_secret_ref is not None,
        escalation=public,
    )
