"""Growth platform: profile, settings, integrations, approvals, compliance.

One router for the whole tenant-facing half of the growth spine.

Everything here is dashboard-user work, so the router carries `require_role` and
the two destructive routes tighten it to owner. An API key has no role and is
rejected: erasing a tenant's whole growth history should not be reachable with a
credential that lives in a config file.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, File, Query, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import ContainerDep, TenantDep, require_role
from app.core.errors import ValidationError
from app.core.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(
    prefix="/growth",
    tags=["growth"],
    dependencies=[require_role("owner", "admin", "member")],
)

# Every provider the platform knows about, with the state it is in when the
# tenant has no row for it. The Integrations screen renders this whole list.
#
# Items the platform will never build are not listed at all: an empty promise
# is worse than no promise.
#
# Deliberately absent, do not re-add: `salesforce` and `cal`. Salesforce is on
# the never-build list. `cal` was traced before removal — the shipped chatbot's
# `book_meeting` / `check_availability` actions read their vendor settings from
# the `tenant_secrets` row behind `action.auth.secret_ref`
# (`app/services/actions/handlers/calendar_provider.py:load_config`) and never
# from `growth_integrations`, so the card connected nothing. Those handlers stay.
PROVIDER_CATALOGUE: tuple[dict[str, str], ...] = (
    {"provider": "resend_domain", "label": "Sending domain", "group": "email",
     "default_status": "not_connected", "note": ""},
    {"provider": "whatsapp", "label": "WhatsApp Business", "group": "channel",
     "default_status": "not_connected", "note": ""},
    {"provider": "telegram", "label": "Telegram (owner alerts)", "group": "owner",
     "default_status": "not_connected", "note": ""},
    {"provider": "slack", "label": "Slack (owner alerts)", "group": "owner",
     "default_status": "not_connected", "note": ""},
    {"provider": "shopify", "label": "Shopify", "group": "store",
     "default_status": "not_connected",
     "note": "Connect via OAuth from this screen."},
    {"provider": "woocommerce", "label": "WooCommerce", "group": "store",
     "default_status": "not_connected",
     "note": "Connect with per-store REST keys from this screen."},
    {"provider": "apollo", "label": "Apollo", "group": "leads",
     "default_status": "not_connected",
     "note": "Optional. Research works without it; Apollo raises the hit rate."},
    {"provider": "hunter", "label": "Hunter", "group": "leads",
     "default_status": "not_connected",
     "note": "Optional. Finds the email pattern behind a company domain."},
    {"provider": "email_verifier", "label": "Email verification", "group": "leads",
     "default_status": "not_connected",
     "note": "Optional. Drops addresses that would hard-bounce before we send."},
    {"provider": "zoho", "label": "Zoho CRM", "group": "crm",
     "default_status": "not_connected",
     "note": "Connect via OAuth. Leads and their activity push one way to Zoho."},
    {"provider": "hubspot", "label": "HubSpot", "group": "crm",
     "default_status": "not_connected",
     "note": "Connect via OAuth. Leads and their activity push one way to HubSpot."},
    {"provider": "indiamart", "label": "IndiaMART", "group": "inbound",
     "default_status": "not_connected",
     "note": "Pulls the enquiry feed hourly. Backfill up to a year from the card."},
    {"provider": "justdial", "label": "JustDial", "group": "inbound",
     "default_status": "not_connected",
     "note": "Forward your JustDial enquiry mails to your inbound address."},
    {"provider": "website_form", "label": "Website form", "group": "inbound",
     "default_status": "not_connected",
     "note": "Connect to get a script tag and a webhook URL for your own form."},
)

# Providers a tenant can actually connect today. Anything else is display-only,
# and a PUT against it is refused rather than writing a row for a provider no
# code reads — which would look connected and do nothing.
CONNECTABLE: frozenset[str] = frozenset(
    {
        "telegram",
        "slack",
        "whatsapp",
        "resend_domain",
        "apollo",
        "hunter",
        "email_verifier",
        "website_form",
        "indiamart",
        "justdial",
        "zoho",
        "hubspot",
    }
)

# CRMs are connectable only through their OAuth flow: the credential is a
# token grant the platform exchanges, never a string the tenant pastes. The
# generic PUT therefore refuses to move one to `active` by hand — the only
# path to `active` is the callback that stored a real grant.
OAUTH_CRM_PROVIDERS: frozenset[str] = frozenset({"zoho", "hubspot"})

# Segments that select nobody without a value, so the counts endpoint reports
# them as `key:value` entries rather than one meaningless bare key.
_PARAMETERISED_SEGMENTS: frozenset[str] = frozenset(
    {"by_channel_consent", "by_category", "dealers_by_territory"}
)

# Which tenant_secrets provider name each connectable integration stores under.
# A provider absent from this map needs no credential — `website_form` is
# authenticated by a signed key the platform issues, not by one the tenant
# brings, so there is nothing to store.
SECRET_PROVIDERS: dict[str, str] = {
    "telegram": "telegram_bot",
    "slack": "slack_webhook",
    "apollo": "apollo",
    "hunter": "hunter",
    "email_verifier": "email_verifier",
    "indiamart": "indiamart",
}


# --------------------------------------------------------------------- models


class SettingsOut(BaseModel):
    """Complete settings, every block filled in from the defaults."""

    model_config = ConfigDict(extra="allow")

    profile: dict[str, Any] = Field(default_factory=dict[str, Any])
    hitl_levels: dict[str, Any] = Field(default_factory=dict[str, Any])
    send_caps: dict[str, Any] = Field(default_factory=dict[str, Any])
    follow_up: dict[str, Any] = Field(default_factory=dict[str, Any])
    offer_range: dict[str, Any] = Field(default_factory=dict[str, Any])
    languages: dict[str, Any] = Field(default_factory=dict[str, Any])
    grounding: dict[str, Any] = Field(default_factory=dict[str, Any])
    report_prefs: dict[str, Any] = Field(default_factory=dict[str, Any])
    channel_order: dict[str, Any] = Field(default_factory=dict[str, Any])


class SettingsIn(BaseModel):
    """A partial edit: only the blocks being changed, only the fields inside them.

    Every field is optional and every one takes a *partial* block. The service
    layers each over what is stored and validates the whole result, so sending
    one field never resets its siblings — the `agentConfig.ts` hazard, enforced
    on the server this time rather than trusted to every caller.
    """

    model_config = ConfigDict(extra="forbid")

    profile: dict[str, Any] | None = None
    hitl_levels: dict[str, Any] | None = None
    send_caps: dict[str, Any] | None = None
    follow_up: dict[str, Any] | None = None
    offer_range: dict[str, Any] | None = None
    languages: dict[str, Any] | None = None
    grounding: dict[str, Any] | None = None
    report_prefs: dict[str, Any] | None = None
    channel_order: dict[str, Any] | None = None

    def blocks(self) -> dict[str, Any]:
        """Only the blocks the caller actually sent.

        `exclude_none` is what makes "not editing this block" different from
        "reset this block": a field left out of the request never reaches the
        service, and the service only writes what it is handed.
        """
        return dict(self.model_dump(exclude_none=True))


class IntegrationOut(BaseModel):
    provider: str
    label: str
    group: str
    status: str
    note: str = ""
    external_ref: str = ""
    connected: bool = False
    # true only for telegram while the platform bot token is set: the card then
    # offers the /start binding instead of the token-and-chat-id form
    provider_mode: bool = False
    health: dict[str, Any] = Field(default_factory=dict[str, Any])
    updated_at: Any = None


class IntegrationIn(BaseModel):
    """Connect or update one provider.

    `secret` is write-only and never comes back. It is encrypted into
    `tenant_secrets` and only its reference is stored on the integration row, so
    a compromised `growth_integrations` read gives an attacker a pointer and
    nothing to point it at.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    external_ref: str = Field(default="", max_length=200)
    secret: str | None = Field(default=None, max_length=4000)
    status: str = "active"


class ApprovalOut(BaseModel):
    id: str
    batch_id: str
    feature: str
    level: int
    status: str
    payload: dict[str, Any] = Field(default_factory=dict[str, Any])
    expires_at: Any = None
    created_at: Any = None
    decided_by: str | None = None
    decided_at: Any = None


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: str
    payload: dict[str, Any] | None = None


# ------------------------------------------------------------------- profile


@router.get("/profile", response_model=SettingsOut)
async def get_profile(tenant: TenantDep, container: ContainerDep) -> SettingsOut:
    """The tenant's complete growth settings, defaults included.

    A tenant who has never opened the wizard gets the defaults rather than a 404:
    the screen's job is to show what would happen today, and "nothing configured"
    and "configured to the defaults" behave identically.
    """
    return SettingsOut(**await container.growth_profile.get(tenant.tenant_id))


@router.put("/profile", response_model=SettingsOut)
async def put_profile(
    body: SettingsIn, tenant: TenantDep, container: ContainerDep
) -> SettingsOut:
    """Apply a partial edit and return the complete result."""
    blocks = body.blocks()
    if not blocks:
        raise ValidationError("no settings blocks supplied")
    result = await container.growth_profile.put(
        tenant.tenant_id, blocks, actor=str(tenant.user_id or "")
    )
    return SettingsOut(**result)


@router.post("/profile/discover", response_model=dict[str, Any])
async def discover_profile(
    tenant: TenantDep, container: ContainerDep
) -> dict[str, Any]:
    """Queue the profile agent to (re-)discover the business profile.

    The automatic trigger fires once, when the tenant's own site finishes
    indexing; this is the owner's door for every run after that — new pages
    crawled, or fields to fill that were missing the first time. The agent
    still refuses to overwrite anything set by hand, so a re-run can only add
    to empty fields. Enqueued rather than run inline for the same reason as
    Find leads: retrieval plus one paid extraction call belong on the queue,
    retryable and attributable in the job log.
    """
    from app.adapters.queue.base import Job

    await container.queue.enqueue(
        Job(
            kind="growth_profile",
            tenant_id=tenant.tenant_id,
            payload={"force": True},
        )
    )
    await container.growth.audit.record(
        tenant.tenant_id,
        agent="owner",
        action="profile_discovery_requested",
        input={},
        reason=f"requested by {tenant.user_id or 'owner'}",
    )
    return {"status": "queued"}


# -------------------------------------------------------------- integrations


def _row_preferred(candidate: dict[str, Any], current: dict[str, Any]) -> bool:
    """Whether ``candidate`` should represent its provider over ``current``.

    Active beats inactive outright; ties fall to the more recent ``updated_at``
    (matching ``GrowthIntegrationsRepository.get_by_provider``), with a missing
    timestamp losing to a present one.
    """
    candidate_active = candidate.get("status") == "active"
    current_active = current.get("status") == "active"
    if candidate_active != current_active:
        return candidate_active
    c_updated = candidate.get("updated_at")
    k_updated = current.get("updated_at")
    if c_updated is None or k_updated is None:
        return c_updated is not None
    return c_updated > k_updated


@router.get("/integrations", response_model=list[IntegrationOut])
async def list_integrations(
    tenant: TenantDep, container: ContainerDep
) -> list[IntegrationOut]:
    """Every provider the platform knows about, with this tenant's state.

    The catalogue is the spine and the rows are an overlay, not the other way
    round — so a provider that is not connected, or connected and then
    disconnected, still renders as a card with a reason instead of silently
    vanishing from the screen.
    """
    # A provider can legitimately hold several rows — a telegram binding beside
    # a stale per-tenant-era row, a rebind's `not_connected` leftover. The card
    # must show the connection that works, not whichever external_ref sorts
    # last: collapse to the active row when one exists, else the most recently
    # updated. Without this a stale `not_connected` row alphabetically after a
    # live one makes the panel report a working integration as disconnected —
    # the tenant then "reconnects" a connection that never broke.
    rows: dict[str, dict[str, Any]] = {}
    for row in await container.growth.integrations.list(tenant.tenant_id):
        provider = str(row["provider"])
        current = rows.get(provider)
        if current is None:
            rows[provider] = row
        elif _row_preferred(row, current):
            rows[provider] = row
    # the Telegram card's shape depends on provider mode (nothing to paste —
    # the tenant connects by messaging the platform bot), so the screen needs
    # the flag with this list rather than after a second call
    telegram_provider_mode = bool(str(container.settings.TELEGRAM_BOT_TOKEN or ""))
    out: list[IntegrationOut] = []
    for entry in PROVIDER_CATALOGUE:
        row = rows.get(entry["provider"])
        out.append(
            IntegrationOut(
                provider=entry["provider"],
                label=entry["label"],
                group=entry["group"],
                note=entry["note"],
                status=str(row["status"]) if row else entry["default_status"],
                external_ref=str(row["external_ref"]) if row else "",
                connected=bool(row and row["status"] == "active"),
                provider_mode=(
                    entry["provider"] == "telegram" and telegram_provider_mode
                ),
                health=dict[str, Any](row["health"] or {}) if row else {},
                updated_at=row["updated_at"] if row else None,
            )
        )
    return out


@router.put("/integrations/{provider}", response_model=IntegrationOut)
async def put_integration(
    provider: str,
    body: IntegrationIn,
    tenant: TenantDep,
    container: ContainerDep,
) -> IntegrationOut:
    """Connect or update one provider.

    Refuses a provider that is display-only. Writing a row for something no code
    reads would give the tenant a card that says `active` and a feature that
    never runs, which is the worst of both states.
    """
    entry = next((e for e in PROVIDER_CATALOGUE if e["provider"] == provider), None)
    if entry is None:
        raise ValidationError(f"unknown provider {provider!r}")
    if provider not in CONNECTABLE:
        raise ValidationError(
            f"{entry['label']} is not connectable yet: {entry['note'] or 'not built'}"
        )
    if body.status not in ("active", "not_connected"):
        raise ValidationError("status must be 'active' or 'not_connected'")

    existing = await container.growth.integrations.get(
        tenant.tenant_id, provider, body.external_ref
    )

    if provider == "resend_domain" and body.status == "active":
        # The sending domain is the one integration whose `active` cannot be
        # typed into existence: the cold-mail sender refuses to run until the
        # domain's SPF/DKIM really resolve at Resend. Connect registers the
        # domain and parks the row at `pending` with the DNS records the tenant
        # must add; the separate verify endpoint is the only path to `active`.
        return await _connect_resend_domain(container, tenant, body, entry)

    if provider in OAUTH_CRM_PROVIDERS and (body.secret or body.status == "active"):
        # The CRM grant is one packed OAuth blob the callback stores under its
        # own ref scheme. A pasted secret would overwrite the row's
        # `secret_ref` with a ref whose contents the push cannot use —
        # silently killing a working connection — and a hand-set `active`
        # with no grant at all is a card that pushes nothing. The only write
        # this door accepts for a CRM is the disconnect.
        raise ValidationError(
            f"{entry['label']} connects via OAuth — use the Connect button"
        )
    # A provider that needs a credential must not be able to reach `active`
    # without one. Otherwise the card reads "Connected", the notifier resolves no
    # secret, skips itself with a warning, and the owner's approvals silently go
    # nowhere — a dead button that looks like a working one.
    needs_secret = provider in SECRET_PROVIDERS
    has_stored = bool(existing and existing.get("secret_ref"))
    if body.status == "active" and needs_secret and not body.secret and not has_stored:
        raise ValidationError(
            f"{entry['label']} needs its credential before it can be connected"
        )

    secret_ref: str | None = None
    if body.secret:
        secret_ref = await _store_secret(
            container, tenant.tenant_id, provider, body.secret
        )

    # `health` is carried forward, not reset. The column is a whole-value
    # replace at the SQL layer, and it is where the sync cursors, the failure
    # counters and the webhook-registration state live — writing `{}` here
    # would make a plain status edit re-sync a store from the beginning and
    # forget that its webhooks are already registered. A connect that supplies
    # a new credential is the one case that starts clean, because the old
    # health describes a connection that no longer exists.
    health = {} if body.secret else dict((existing or {}).get("health") or {})

    row = await container.growth.integrations.upsert(
        tenant.tenant_id,
        provider,
        status=body.status,
        external_ref=body.external_ref,
        secret_ref=secret_ref,
        health=health,
    )
    await container.growth.audit.record(
        tenant.tenant_id,
        agent="owner",
        action="integration_updated",
        input={"provider": provider, "status": body.status},
        reason=f"updated by {tenant.user_id or 'owner'}",
    )
    return IntegrationOut(
        provider=provider,
        label=entry["label"],
        group=entry["group"],
        note=entry["note"],
        status=str(row["status"]),
        external_ref=str(row["external_ref"]),
        connected=row["status"] == "active",
        health=dict[str, Any](row["health"] or {}),
        updated_at=row["updated_at"],
    )


async def _connect_resend_domain(
    container: Any, tenant: Any, body: "IntegrationIn", entry: dict[str, str]
) -> "IntegrationOut":
    """Register the sending domain at Resend; the row lands `pending`.

    The tenant typed a subdomain and clicked connect — that is the whole of
    their effort. Everything else is the provider's paperwork: Resend returns
    the exact DNS records to add, and they are stored in `health` so the card
    can show them and the verify endpoint can find the domain id again.
    """
    domain = body.external_ref.strip().lower()
    if "." not in domain or any(c.isspace() for c in domain):
        raise ValidationError("enter the sending subdomain, e.g. mail.yourbusiness.com")

    provider = container.email
    if provider is None:
        raise ValidationError(
            "this deployment has no Resend email backend configured"
        )

    try:
        created = await provider.create_domain(domain)
    except Exception as exc:
        logger.warning("resend_domain_create_failed", domain=domain, exc_info=True)
        raise ValidationError(f"Resend would not accept this domain: {exc}") from exc

    domain_id = str(created.get("id") or "")
    records = [
        {k: str(v) for k, v in (r or {}).items() if k in ("record", "name", "type", "ttl", "status", "priority")}
        for r in (created.get("records") or [])
        if isinstance(r, dict)
    ]
    if not domain_id:
        raise ValidationError("Resend did not return a domain id")

    row = await container.growth.integrations.upsert(
        tenant.tenant_id,
        "resend_domain",
        status="pending",
        external_ref=domain,
        secret_ref=None,
        health={"resend_domain_id": domain_id, "dns_records": records},
    )
    await container.growth.audit.record(
        tenant.tenant_id,
        agent="owner",
        action="integration_updated",
        input={"provider": "resend_domain", "status": "pending", "domain": domain},
        reason=f"updated by {tenant.user_id or 'owner'}",
    )
    return IntegrationOut(
        provider="resend_domain",
        label=entry["label"],
        group=entry["group"],
        note=entry["note"],
        status=str(row["status"]),
        external_ref=domain,
        connected=False,
        health=dict[str, Any](row["health"] or {}),
        updated_at=row["updated_at"],
    )


@router.post("/integrations/resend_domain/verify", response_model=IntegrationOut)
async def verify_resend_domain(
    tenant: TenantDep, container: ContainerDep
) -> IntegrationOut:
    """Read the domain's verification state back from Resend, then flip it.

    The only path from `pending` to `active` — the cold-mail sender's gate
    reads that status, so a domain that has not resolved its DNS records can
    never send. A `pending`/`failed` verdict is returned honestly, with the
    records still in `health` for the tenant to add.
    """
    entry = next(
        (e for e in PROVIDER_CATALOGUE if e["provider"] == "resend_domain"), None
    )
    assert entry is not None  # the catalogue is a literal; this is for the type checker
    # the verify call carries neither the domain nor the ref — the row itself
    # knows both. Single-connection provider: provider-level lookup.
    row = await container.growth.integrations.get_by_provider(
        tenant.tenant_id, "resend_domain"
    )
    if row is None or row.get("status") not in ("pending", "active"):
        raise ValidationError("connect a sending domain first")

    health = dict[str, Any](row.get("health") or {})
    domain_id = str(health.get("resend_domain_id") or "")
    if not domain_id:
        raise ValidationError("the integration has no Resend domain id — reconnect it")

    provider = container.email
    if provider is None:
        raise ValidationError("this deployment has no Resend email backend configured")

    try:
        state = await provider.get_domain(domain_id)
    except Exception as exc:
        logger.warning("resend_domain_check_failed", exc_info=True)
        raise ValidationError(f"could not reach Resend to check the domain: {exc}") from exc

    # Resend's vocabulary: not_started | pending | verified | failed. SPF and
    # DKIM both matter — a domain Resend calls verified but whose records
    # dropped would flip back on the next check, so read both flags now.
    status = str(state.get("status") or "")
    spf_ok = bool(state.get("spf"))
    dkim_ok = bool(state.get("dkim"))
    verified = status == "verified" and spf_ok and dkim_ok
    health = {**health, "spf": spf_ok, "dkim": dkim_ok, "resend_status": status}
    records = [
        {k: str(v) for k, v in (r or {}).items() if k in ("record", "name", "type", "ttl", "status", "priority")}
        for r in (state.get("records") or [])
        if isinstance(r, dict)
    ]
    if records:
        health["dns_records"] = records

    new_status = "active" if verified else "pending"
    updated = await container.growth.integrations.upsert(
        tenant.tenant_id,
        "resend_domain",
        status=new_status,
        external_ref=str(row.get("external_ref") or ""),
        secret_ref=None,
        health=health,
    )
    await container.growth.audit.record(
        tenant.tenant_id,
        agent="owner",
        action="integration_updated",
        input={"provider": "resend_domain", "status": new_status},
        reason=f"verified by {tenant.user_id or 'owner'}",
    )
    return IntegrationOut(
        provider="resend_domain",
        label=entry["label"],
        group=entry["group"],
        note=entry["note"],
        status=str(updated["status"]),
        external_ref=str(updated["external_ref"]),
        connected=new_status == "active",
        health=dict[str, Any](updated["health"] or {}),
        updated_at=updated["updated_at"],
    )


async def _store_secret(
    container: Any, tenant_id: UUID, provider: str, secret: str
) -> str:
    """Encrypt a provider credential into `tenant_secrets` and return its ref.

    The plaintext never leaves this function, is never logged, and is never
    written to `growth_integrations`. The ref is deterministic per provider so a
    re-connect rotates the stored value in place rather than accumulating dead
    ciphertext rows nobody can tell apart.
    """
    from sqlalchemy import text

    from app.core.db import tenant_scope
    from app.core.security import encrypt_secret

    secret_provider = SECRET_PROVIDERS.get(provider, provider)
    ref = f"growth:{provider}"
    ciphertext = encrypt_secret(secret, container.settings.ENCRYPTION_KEY)

    async with tenant_scope(tenant_id) as session:
        await session.execute(
            text(
                "INSERT INTO tenant_secrets (tenant_id, ref, provider, ciphertext) "
                "VALUES (:tid, :ref, :provider, :ciphertext) "
                "ON CONFLICT (tenant_id, ref) DO UPDATE SET "
                "ciphertext = EXCLUDED.ciphertext, provider = EXCLUDED.provider, "
                "rotated_at = now()"
            ),
            {
                "tid": tenant_id,
                "ref": ref,
                "provider": secret_provider,
                "ciphertext": ciphertext,
            },
        )
    return ref


# ----------------------------------------------------------------- approvals


@router.get("/approvals", response_model=list[ApprovalOut])
async def list_approvals(
    tenant: TenantDep,
    container: ContainerDep,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[ApprovalOut]:
    """Everything waiting on this owner's decision, soonest deadline first."""
    rows = await container.approvals.pending_for(tenant.tenant_id, limit=limit)
    return [_approval_out(row) for row in rows]


@router.post("/approvals/{approval_id}/decide", response_model=ApprovalOut)
async def decide_approval(
    approval_id: UUID,
    body: DecisionIn,
    tenant: TenantDep,
    container: ContainerDep,
) -> ApprovalOut:
    """Approve, reject, or approve-with-edits one batch.

    A POST, not a GET on a link: the notification's buttons deep-link into this
    screen rather than at a decision endpoint, because a GET that spends money is
    a URL that a mail scanner, a chat client's link preview, or a browser's
    speculative prefetch will eventually press on the owner's behalf.
    """
    row = await container.approvals.decide(
        tenant.tenant_id,
        approval_id,
        decision=body.decision,
        decided_by=str(tenant.user_id or "owner"),
        edited_payload=body.payload,
    )
    return _approval_out(row)


def _approval_out(row: dict[str, Any]) -> ApprovalOut:
    return ApprovalOut(
        id=str(row["id"]),
        batch_id=str(row["batch_id"]),
        feature=str(row["feature"]),
        level=int(row["level"]),
        status=str(row["status"]),
        payload=dict[str, Any](row.get("payload") or {}),
        expires_at=row.get("expires_at"),
        created_at=row.get("created_at"),
        decided_by=row.get("decided_by"),
        decided_at=row.get("decided_at"),
    )


# --------------------------------------------------------------------- leads


class LeadCrmLinkOut(BaseModel):
    """The per-lead CRM state the board chips and the drawer render.

    Deliberately the four fields a chip needs, not the whole link row — the
    board asks for fifty leads at once and the detail lives one screen away.
    """

    provider: str
    status: str = "pending"
    external_id: str = ""
    last_error: str = ""


class InboundBackfillIn(BaseModel):
    """A catch-up pull for one connected inbound source.

    `days` is capped at a year because that is as far back as the source's own
    API reaches; asking for more would silently get less.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source: str
    days: int = Field(default=30, ge=1, le=365)


class LeadOut(BaseModel):
    id: str
    company: str = ""
    person: str = ""
    role: str = ""
    email: str = ""
    phone: str = ""
    domain: str = ""
    source: str = ""
    status: str = "new"
    language: str | None = None
    research_summary: str = ""
    research_links: list[dict[str, Any]] = Field(default_factory=list[dict[str, Any]])
    owner_notes: str = ""
    follow_up_count: int = 0
    last_outbound_at: Any = None
    paused_until: Any = None
    created_at: Any = None
    updated_at: Any = None
    # always present, possibly empty: the board renders chips off it and a
    # missing field would render as "unknown" rather than "not linked"
    crm_links: list[LeadCrmLinkOut] = Field(default_factory=list[LeadCrmLinkOut])


class LeadListOut(BaseModel):
    """The board: a page of leads plus the counters above it.

    Counts come back with the page rather than from a second endpoint, because
    the screen renders them together and two round trips would let the tabs and
    the list disagree about what is on the board.
    """

    leads: list[LeadOut] = Field(default_factory=list[LeadOut])
    counts: dict[str, int] = Field(default_factory=dict[str, int])
    # the distinct sources on the board, so the filter chips are built from
    # what exists rather than from a hardcoded list that goes stale the first
    # time a source is added
    sources: list[str] = Field(default_factory=list[str])


class LeadMessageOut(BaseModel):
    id: str
    channel: str
    direction: str
    content: str = ""
    language: str | None = None
    status: str = "queued"
    cost_micros: int = 0
    created_at: Any = None


class LeadDetailOut(BaseModel):
    lead: LeadOut
    thread: list[LeadMessageOut] = Field(default_factory=list[LeadMessageOut])


class LeadPatchIn(BaseModel):
    """The two things an owner changes by hand on a lead."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    status: str | None = None
    owner_notes: str | None = Field(default=None, max_length=4000)


class InboxRowOut(BaseModel):
    lead_id: str
    company: str = ""
    person: str = ""
    email: str = ""
    phone: str = ""
    status: str = "new"
    source: str = ""
    channel: str = ""
    direction: str = ""
    content: str = ""
    message_status: str = ""
    created_at: Any = None


class FindLeadsIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query_seed: str = Field(default="", max_length=500)
    limit: int = Field(default=25, ge=1, le=25)


class ReportOut(BaseModel):
    """One report row. `numbers` is the full computed set, so the screen can
    render a fallback while the PDF is still rendering."""

    id: str
    period: str
    period_start: Any = None
    numbers: dict[str, Any] = Field(default_factory=dict[str, Any])
    pdf_path: str = ""
    summary: str = ""
    status: str = "queued"
    created_at: Any = None


class RunReportIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    period: str = Field(default="on_demand", max_length=20)


class TelegramSetupOut(BaseModel):
    """The webhook URL the tenant pastes into BotFather's setWebhook.

    Telegram accepts one webhook URL per bot and no custom headers, so the
    key travels in the query string here — the one sanctioned use of the
    fallback form, and the panel labels it as such.

    Provider mode (a platform bot in TELEGRAM_BOT_TOKEN) replaces the paste
    with a binding code: ``provider_mode`` is true, ``bot_username`` is the
    platform bot (null when getMe failed — the panel then shows the plain
    instructions without the link), ``binding_code`` is what the tenant sends
    as ``/start <code>``, and ``webhook_url_with_key`` is the *platform
    owner's* one-time setWebhook URL, not a tenant's.
    """

    connected: bool
    webhook_url: str = ""
    webhook_url_with_key: str = ""
    key: str = ""
    provider_mode: bool = False
    bot_username: str | None = None
    binding_code: str = ""


def _report_out(row: dict[str, Any]) -> ReportOut:
    return ReportOut(
        id=str(row["id"]),
        period=str(row.get("period") or ""),
        period_start=row.get("period_start"),
        numbers=dict(row.get("numbers") or {}),
        pdf_path=str(row.get("pdf_path") or ""),
        summary=str(row.get("summary") or ""),
        status=str(row.get("status") or "queued"),
        created_at=row.get("created_at"),
    )


@router.get("/reports", response_model=list[ReportOut])
async def list_reports(
    tenant: TenantDep,
    container: ContainerDep,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[ReportOut]:
    """Newest reports first, with the numbers inline for the no-PDF fallback."""
    rows = await container.growth.reports.list(tenant.tenant_id, limit=limit, offset=offset)
    return [_report_out(row) for row in rows]


@router.get("/reports/{report_id}", response_model=ReportOut)
async def get_report(
    report_id: UUID, tenant: TenantDep, container: ContainerDep
) -> ReportOut:
    row = await container.growth.reports.get(tenant.tenant_id, report_id)
    if row is None:
        raise ValidationError("report not found")
    return _report_out(row)


@router.get("/reports/{report_id}/download", response_model=dict[str, str])
async def download_report(
    report_id: UUID, tenant: TenantDep, container: ContainerDep
) -> dict[str, str]:
    """Short-lived link to the rendered PDF.

    The path on the row is a storage key, not a URL — handing it to the browser
    would leak the bucket layout and still not open. The signed link is minted
    per request so a stale copy in someone's history stops working.
    """
    row = await container.growth.reports.get(tenant.tenant_id, report_id)
    if row is None:
        raise ValidationError("report not found")
    path = str(row.get("pdf_path") or "")
    if not path:
        raise ValidationError("this report has no PDF yet")
    url = await container.storage.signed_url(path)
    period = str(row.get("period") or "report")
    start = str(row.get("period_start") or "")
    return {"url": url, "filename": f"{period}-{start}.pdf"}


@router.post("/reports/run", response_model=dict[str, Any])
async def run_report(
    body: RunReportIn, tenant: TenantDep, container: ContainerDep
) -> dict[str, Any]:
    """Queue an on-demand report. Same period twice regenerates, never duplicates.

    The unique key on (tenant, period, period_start) is the dedupe: a second
    press while the first run is still queued lands on the same row, and a
    press after it finished returns the finished report instead of rebuilding
    one that is minutes old.
    """
    if body.period != "on_demand":
        raise ValidationError("only on_demand runs from here; the schedule owns the rest")
    result: dict[str, Any] = await container.growth_reports.request_on_demand(
        tenant.tenant_id, via="panel"
    )
    return result


@router.get("/telegram-setup", response_model=TelegramSetupOut)
async def telegram_setup(
    tenant: TenantDep,
    container: ContainerDep,
    refresh: bool = Query(default=False),
) -> TelegramSetupOut:
    """The bot webhook URL carrying this tenant's ingress key.

    Minted on read rather than stored, like the website-form key: the key is a
    signed statement about the tenant and the provider, so re-deriving it is
    free. Disconnect the telegram integration and the ingress refuses the key.

    In provider mode the answer is the binding code instead: the webhook URL it
    returns is the platform owner's (set once per deployment, not by this
    tenant), and ``refresh`` retires and remints the code — the tenant's "new
    code" action.
    """
    settings = container.settings
    if str(settings.TELEGRAM_BOT_TOKEN or ""):
        from app.services.growth.ingresskey import platform_ingress_url_with_key
        from app.services.growth.telegram_bot import (
            current_binding_code,
            platform_bot_username,
        )

        rows = await container.growth.integrations.list(tenant.tenant_id)
        connected = any(
            str(row.get("provider")) == "telegram" and row.get("status") == "active"
            for row in rows
        )
        return TelegramSetupOut(
            connected=connected,
            provider_mode=True,
            bot_username=await platform_bot_username(container),
            binding_code=await current_binding_code(
                container, tenant.tenant_id, refresh=refresh
            ),
            webhook_url_with_key=platform_ingress_url_with_key(settings, "telegram"),
        )

    row = await container.growth.integrations.get(tenant.tenant_id, "telegram")
    connected = bool(row and row.get("status") == "active")
    if not connected:
        return TelegramSetupOut(connected=False)

    from app.services.growth.ingresskey import (
        ingress_url,
        ingress_url_with_key,
        mint_ingress_key,
    )

    return TelegramSetupOut(
        connected=True,
        key=mint_ingress_key(settings.SECRET_KEY, tenant.tenant_id, "telegram"),
        webhook_url=ingress_url(settings, "telegram"),
        webhook_url_with_key=ingress_url_with_key(
            settings, tenant.tenant_id, "telegram"
        ),
    )


# Statuses an owner may set by hand. Deliberately not the whole ladder: `mailed`,
# `replied` and `researched` are set by agents from evidence, and letting a
# human assert them would make the reports describe a world nobody observed.
OWNER_SETTABLE: frozenset[str] = frozenset({"positive", "meeting", "won", "lost", "later"})


@router.get("/leads", response_model=LeadListOut)
async def list_leads(
    tenant: TenantDep,
    container: ContainerDep,
    status: str | None = Query(default=None),
    source: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> LeadListOut:
    """One page of the lead board, with the counters."""
    rows = await container.growth.leads.list(
        tenant.tenant_id, status=status, source=source, limit=limit, offset=offset
    )
    counts = await container.growth.leads.count_by_status(tenant.tenant_id)
    sources = await container.growth.leads.distinct_sources(tenant.tenant_id)
    grouped = await _crm_links_for(
        container, tenant.tenant_id, [UUID(str(r["id"])) for r in rows]
    )
    return LeadListOut(
        leads=[
            _lead_out(row, grouped.get(UUID(str(row["id"]))))
            for row in rows
        ],
        counts=counts,
        sources=sources,
    )


@router.get("/leads/{lead_id}", response_model=LeadDetailOut)
async def get_lead(
    lead_id: UUID, tenant: TenantDep, container: ContainerDep
) -> LeadDetailOut:
    """One lead and its whole conversation, for the drawer."""
    row = await container.growth.leads.get(tenant.tenant_id, lead_id)
    if row is None:
        from app.core.errors import NotFoundError

        raise NotFoundError("no such lead")
    thread = await container.growth.messages.thread_for_lead(tenant.tenant_id, lead_id)
    grouped = await _crm_links_for(container, tenant.tenant_id, [lead_id])
    return LeadDetailOut(
        lead=_lead_out(row, grouped.get(lead_id)),
        # oldest first in the drawer: a conversation reads downwards, even
        # though the query returns newest-first for the LIMIT to be cheap
        thread=[_message_out(item) for item in reversed(thread)],
    )


@router.patch("/leads/{lead_id}", response_model=LeadOut)
async def patch_lead(
    lead_id: UUID, body: LeadPatchIn, tenant: TenantDep, container: ContainerDep
) -> LeadOut:
    """Set a lead's outcome or leave a note.

    A status the owner is not allowed to assert is refused rather than ignored:
    silently dropping half a PATCH is the `agentConfig.ts` hazard in a different
    costume.
    """
    row = await container.growth.leads.get(tenant.tenant_id, lead_id)
    if row is None:
        from app.core.errors import NotFoundError

        raise NotFoundError("no such lead")

    if body.status is not None:
        if body.status not in OWNER_SETTABLE:
            raise ValidationError(
                f"status must be one of {sorted(OWNER_SETTABLE)}"
            )
        row["status"] = body.status
        if body.status in ("won", "lost"):
            # a closed lead stops being chased. Cancelling the queued drafts is
            # the part an owner assumes happened and would never think to check
            await container.growth.messages.cancel_queued_for_lead(
                tenant.tenant_id, lead_id
            )
    if body.owner_notes is not None:
        row["owner_notes"] = body.owner_notes

    updated = await container.growth.leads.upsert(tenant.tenant_id, row)
    await container.growth.audit.record(
        tenant.tenant_id,
        agent="owner",
        action="lead_updated",
        input={"lead_id": str(lead_id), "status": body.status},
        reason=f"updated by {tenant.user_id or 'owner'}",
    )
    if body.status is not None or body.owner_notes is not None:
        # the one owner-driven change: queue the CRM push for the new status
        # and/or notes. Never raises, never blocks the response on a CRM.
        await container.growth_crm.on_lead_changed(tenant.tenant_id, lead_id)
    grouped = await _crm_links_for(container, tenant.tenant_id, [lead_id])
    return _lead_out(updated, grouped.get(lead_id))


@router.post("/leads/find", response_model=dict[str, Any])
async def find_leads(
    body: FindLeadsIn, tenant: TenantDep, container: ContainerDep
) -> dict[str, Any]:
    """Queue a research run. Returns immediately; the batch arrives for approval.

    Enqueued rather than run inline, and that is not only about latency: a
    research run makes paid provider calls and a dozen page fetches, so it
    belongs on the queue where it is retryable, rate-limited by the queue's own
    concurrency, and attributable in the job log.
    """
    from app.adapters.queue.base import Job

    await container.queue.enqueue(
        Job(
            kind="growth_research",
            tenant_id=tenant.tenant_id,
            payload={"query_seed": body.query_seed, "limit": body.limit},
        )
    )
    await container.growth.audit.record(
        tenant.tenant_id,
        agent="owner",
        action="research_requested",
        input={"query_seed": body.query_seed, "limit": body.limit},
        reason=f"requested by {tenant.user_id or 'owner'}",
    )
    return {"status": "queued", "limit": body.limit}


@router.post("/inbound/backfill", response_model=dict[str, Any])
async def backfill_inbound(
    body: InboundBackfillIn, tenant: TenantDep, container: ContainerDep
) -> dict[str, Any]:
    """Queue a catch-up pull for a connected inbound source.

    IndiaMART only, and that is a property of the source, not an arbitrary
    restriction: it is a pull feed with history, while JustDial arrives as mail
    and there is no retroactive inbox to fetch — anything before the forward
    rule existed was never seen. Refused rather than accepted-and-ignored, so
    a button on the wrong card cannot look like it did something.
    """
    from app.adapters.queue.base import Job

    if body.source != "indiamart":
        raise ValidationError("only the IndiaMART feed has history to pull")

    integration = await container.growth.integrations.get(
        tenant.tenant_id, body.source
    )
    if integration is None or integration.get("status") != "active":
        raise ValidationError("connect IndiaMART before pulling its feed")

    await container.queue.enqueue(
        Job(
            kind="growth_inbound_backfill",
            tenant_id=tenant.tenant_id,
            payload={"source": body.source, "days": body.days},
        )
    )
    await container.growth.audit.record(
        tenant.tenant_id,
        agent="owner",
        action="inbound_backfill_requested",
        input={"source": body.source, "days": body.days},
        reason=f"requested by {tenant.user_id or 'owner'}",
    )
    return {"status": "queued", "source": body.source, "days": body.days}


@router.get("/inbox", response_model=list[InboxRowOut])
async def inbox(
    tenant: TenantDep,
    container: ContainerDep,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[InboxRowOut]:
    """Every conversation with a message in it, newest activity first."""
    rows = await container.growth.messages.inbox(
        tenant.tenant_id, limit=limit, offset=offset
    )
    return [
        InboxRowOut(
            lead_id=str(row["lead_id"]),
            company=str(row.get("company") or ""),
            person=str(row.get("person") or ""),
            email=str(row.get("email") or ""),
            phone=str(row.get("phone") or ""),
            status=str(row.get("status") or ""),
            source=str(row.get("source") or ""),
            channel=str(row.get("channel") or ""),
            direction=str(row.get("direction") or ""),
            content=str(row.get("content") or "")[:500],
            message_status=str(row.get("message_status") or ""),
            created_at=row.get("created_at"),
        )
        for row in rows
    ]


# --------------------------------------------------------------- inbound form


class FormSetupOut(BaseModel):
    """Everything a tenant needs to wire their own website form up.

    Several affordances at once, because what a tenant can actually use depends
    on software we know nothing about: a script tag for a page they control, a
    webhook plus a header for a builder that can send one, and — last — a URL
    carrying the key for a builder that accepts nothing else.

    The key is returned once, separately, rather than only baked into a URL. A
    credential in a URL is written into every access log, proxy log and browser
    history on its path, so the header form is what the screen leads with and
    `webhook_url_with_key` is explicitly the fallback.
    """

    connected: bool
    snippet: str = ""
    webhook_url: str = ""
    webhook_header: str = ""
    webhook_url_with_key: str = ""
    key: str = ""


@router.get("/form-setup", response_model=FormSetupOut)
async def form_setup(tenant: TenantDep, container: ContainerDep) -> FormSetupOut:
    """The tenant's website-form key, as a script tag and as a webhook URL.

    Minted on read rather than stored: the key is a signed statement about the
    tenant and the provider, so re-deriving it is free and there is nothing to
    keep in sync. Revocation is the integration row — disconnect and the ingress
    refuses the key, whatever pages still carry it.
    """
    row = await container.growth.integrations.get(tenant.tenant_id, "website_form")
    connected = bool(row and row.get("status") == "active")
    if not connected:
        return FormSetupOut(connected=False)

    from app.services.growth.ingresskey import (
        ingress_url,
        ingress_url_with_key,
        mint_ingress_key,
    )

    settings = container.settings
    key = mint_ingress_key(settings.SECRET_KEY, tenant.tenant_id, "website_form")
    base = str(settings.PUBLIC_BASE_URL or "").rstrip("/")
    return FormSetupOut(
        connected=True,
        key=key,
        webhook_url=ingress_url(settings, "website_form"),
        webhook_header="x-ingress-key",
        webhook_url_with_key=ingress_url_with_key(
            settings, tenant.tenant_id, "website_form"
        ),
        snippet=f'<script src="{base}/growth/form.js" data-key="{key}"></script>',
    )


def _lead_out(
    row: dict[str, Any], crm_links: list[dict[str, Any]] | None = None
) -> LeadOut:
    links = row.get("research_links") or []
    return LeadOut(
        id=str(row["id"]),
        company=str(row.get("company") or ""),
        person=str(row.get("person") or ""),
        role=str(row.get("role") or ""),
        email=str(row.get("email") or ""),
        phone=str(row.get("phone") or ""),
        domain=str(row.get("domain") or ""),
        source=str(row.get("source") or ""),
        status=str(row.get("status") or "new"),
        language=row.get("language"),
        research_summary=str(row.get("research_summary") or ""),
        research_links=[dict(item) for item in links if isinstance(item, dict)],
        owner_notes=str(row.get("owner_notes") or ""),
        follow_up_count=int(row.get("follow_up_count") or 0),
        last_outbound_at=row.get("last_outbound_at"),
        paused_until=row.get("paused_until"),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
        crm_links=[
            LeadCrmLinkOut(
                provider=str(link.get("provider") or ""),
                status=str(link.get("status") or "pending"),
                external_id=str(link.get("external_id") or ""),
                last_error=str(link.get("last_error") or ""),
            )
            for link in (crm_links or [])
        ],
    )


async def _crm_links_for(
    container: Any, tenant_id: UUID, lead_ids: list[UUID]
) -> dict[UUID, list[dict[str, Any]]]:
    """The link rows for a page of leads, grouped by lead for `_lead_out`.

    One query for the whole page (the repo's own docstring says why), then the
    group-by here so `_lead_out` never does I/O of its own.
    """
    rows = await container.growth.crm_links.for_leads(tenant_id, lead_ids)
    grouped: dict[UUID, list[dict[str, Any]]] = {}
    for row in rows:
        try:
            lead_id = UUID(str(row.get("lead_id")))
        except (ValueError, TypeError):
            continue
        grouped.setdefault(lead_id, []).append(row)
    return grouped


def _message_out(row: dict[str, Any]) -> LeadMessageOut:
    return LeadMessageOut(
        id=str(row["id"]),
        channel=str(row.get("channel") or ""),
        direction=str(row.get("direction") or ""),
        content=str(row.get("content") or ""),
        language=row.get("language"),
        status=str(row.get("status") or ""),
        cost_micros=int(row.get("cost_micros") or 0),
        created_at=row.get("created_at"),
    )


# ---------------------------------------------------------------- compliance


# -------------------------------------------------------------------- stores
#
# Store connections (Stage 4). Shopify connects via public-app OAuth — the
# secret never passes through the dashboard — and WooCommerce via per-store
# REST keys, verified against the store before they are stored. Both land in
# tenant_secrets with one ref per (provider, shop); the integration row keeps
# only the shop URL and the pointer.


class StoreOut(BaseModel):
    provider: str
    external_ref: str
    status: str
    health: dict[str, Any] = Field(default_factory=dict[str, Any])
    webhook_url: str = ""
    updated_at: Any = None


class WooConnectIn(BaseModel):
    """One WooCommerce store and its REST keypair.

    The pair is verified with a one-row read before anything is stored, so a
    typo'd key is a 422 now rather than an `error` integration that syncs
    nothing and says nothing.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    store_url: str = Field(max_length=200)
    consumer_key: str = Field(max_length=500)
    consumer_secret: str = Field(max_length=500)


def _store_out(
    row: dict[str, Any], webhook_url: str = ""
) -> StoreOut:
    return StoreOut(
        provider=str(row.get("provider") or ""),
        external_ref=str(row.get("external_ref") or ""),
        status=str(row.get("status") or ""),
        health=dict[str, Any](row.get("health") or {}),
        webhook_url=webhook_url,
        updated_at=row.get("updated_at"),
    )


def _store_webhook_url(container: Any, tenant_id: UUID, provider: str) -> str:
    from app.services.growth.ingresskey import ingress_url_with_key

    return ingress_url_with_key(container.settings, tenant_id, provider)


@router.get("/stores", response_model=list[StoreOut])
async def list_stores(tenant: TenantDep, container: ContainerDep) -> list[StoreOut]:
    """Connected stores with their sync state and webhook URL."""
    rows = await container.growth.integrations.list(tenant.tenant_id)
    out: list[StoreOut] = []
    for row in rows:
        if str(row.get("provider")) not in ("shopify", "woocommerce"):
            continue
        out.append(
            _store_out(
                row,
                webhook_url=_store_webhook_url(
                    container, tenant.tenant_id, str(row.get("provider"))
                ),
            )
        )
    return out


@router.get("/stores/shopify/connect", response_model=dict[str, Any])
async def shopify_connect(
    tenant: TenantDep,
    container: ContainerDep,
    shop: str = Query(default=""),
) -> dict[str, Any]:
    """Start the public-app OAuth install: returns the Shopify authorize URL.

    The owner visits it, approves the scopes, and Shopify redirects back to
    the callback with a one-time code. The `state` is an HMAC-signed
    (tenant, shop) pair, so the callback needs no session to trust it.
    """
    from app.adapters.stores.shopify import shop_domain

    app_config = container.shopify
    if app_config is None:
        raise ValidationError(
            "Shopify is not configured on this deployment yet (SHOPIFY_API_KEY/SECRET)"
        )
    domain = shop_domain(shop) if shop else ""
    if not domain:
        raise ValidationError("pass your shop domain as ?shop=you.myshopify.com")
    if not domain.endswith(".myshopify.com"):
        raise ValidationError("that is not a Shopify shop domain (*.myshopify.com)")
    state = _oauth_state(container, tenant.tenant_id, domain)
    redirect_uri = _oauth_redirect(container)
    from app.adapters.stores.shopify import authorize_url

    return {
        "authorize_url": authorize_url(
            api_key=str(app_config["api_key"]),
            shop=domain,
            redirect_uri=redirect_uri,
            state=state,
        ),
        "shop": domain,
    }


@router.post("/stores/woocommerce/connect", response_model=StoreOut)
async def woocommerce_connect(
    body: WooConnectIn, tenant: TenantDep, container: ContainerDep
) -> StoreOut:
    """Connect one WooCommerce store: verify the keys, then store them."""
    from app.adapters.stores.woocommerce import pack_secret

    store_url = body.store_url.strip().rstrip("/")
    consumer_key = body.consumer_key.strip()
    await container.growth_stores.verify_store_keys(
        store_url, consumer_key, body.consumer_secret
    )

    ref = await container.growth_stores.store_credential(
        tenant.tenant_id,
        "woocommerce",
        store_url,
        pack_secret(consumer_key, body.consumer_secret),
    )
    # a reconnect keeps its sync cursors: wiping health would force a full
    # re-page of the whole history, and the upserts make that merely slow
    # rather than wrong — but slow is still avoidable for free
    previous = await container.growth.integrations.get(
        tenant.tenant_id, "woocommerce", store_url
    )
    health = dict((previous or {}).get("health") or {})
    health["connected_at"] = datetime.now(UTC).isoformat()
    health.pop("last_error", None)
    row = await container.growth.integrations.upsert(
        tenant.tenant_id,
        "woocommerce",
        status="active",
        external_ref=store_url,
        secret_ref=ref,
        health=health,
    )
    # subscribe the store's topics now that the keypair is stored. Never fatal:
    # the service records the outcome on `health` and the hourly sync works
    # either way — a WordPress that refuses the webhooks endpoint must not cost
    # the tenant the connection
    await container.growth_stores.register_webhooks(
        tenant.tenant_id, "woocommerce", store_url
    )
    row = (
        await container.growth.integrations.get(
            tenant.tenant_id, "woocommerce", store_url
        )
        or row
    )
    await container.growth.audit.record(
        tenant.tenant_id,
        agent="owner",
        action="store_connected",
        input={"provider": "woocommerce", "store": store_url},
        reason=f"connected by {tenant.user_id or 'owner'}",
    )
    return _store_out(
        row, webhook_url=_store_webhook_url(container, tenant.tenant_id, "woocommerce")
    )


@router.post("/stores/sync", response_model=dict[str, Any])
async def stores_sync_now(tenant: TenantDep, container: ContainerDep) -> dict[str, Any]:
    """Run a sync pass now instead of waiting for the hourly tick."""
    from app.adapters.queue.base import Job

    try:
        await container.queue.enqueue(
            Job(kind="growth_store_sync", tenant_id=tenant.tenant_id, payload={})
        )
    except Exception:
        logger.warning("growth_store_sync_enqueue_failed", exc_info=True)
        synced: dict[str, Any] = await container.growth_stores.sync_tenant(tenant.tenant_id)
        return synced
    return {"status": "queued"}


# --------------------------------------------------------------------- crm
#
# One-way push: leads and their activity go out to the tenant's CRM; nothing
# ever comes back. The screen below is the receipt — what was pushed, what
# failed, what to retry — because a sync the tenant cannot see failing is a
# sync they will discover from their own sales team.


class CrmConnectOut(BaseModel):
    authorize_url: str


class CrmLinkOut(BaseModel):
    """One link row, joined to its lead so the screen shows a name."""

    id: str
    provider: str
    lead_id: str
    lead_company: str = ""
    lead_person: str = ""
    lead_status: str = ""
    external_id: str = ""
    synced_status: str = ""
    status: str = "pending"
    last_error: str = ""
    attempts: int = 0
    last_synced_at: str | None = None
    notes_through: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class CrmLinkSummaryOut(BaseModel):
    provider: str
    status: str
    total: int


class CrmLinksOut(BaseModel):
    items: list[CrmLinkOut] = Field(default_factory=list[CrmLinkOut])
    summary: list[CrmLinkSummaryOut] = Field(default_factory=list[CrmLinkSummaryOut])
    # rows under the same filters as `items`, so the screen can tell the end of
    # the window from the end of the data
    total: int = 0


class CrmResyncOut(BaseModel):
    queued: int


class CrmRetryIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    provider: str


def _iso(value: Any) -> str | None:
    """A timestamp from a link row as an ISO string, or None."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return str(value.isoformat())
    except AttributeError:
        return None


def _crm_link_out(row: dict[str, Any]) -> CrmLinkOut:
    return CrmLinkOut(
        id=str(row.get("id")),
        provider=str(row.get("provider") or ""),
        lead_id=str(row.get("lead_id")),
        lead_company=str(row.get("lead_company") or ""),
        lead_person=str(row.get("lead_person") or ""),
        lead_status=str(row.get("lead_status") or ""),
        external_id=str(row.get("external_id") or ""),
        synced_status=str(row.get("synced_status") or ""),
        status=str(row.get("status") or "pending"),
        last_error=str(row.get("last_error") or ""),
        attempts=int(row.get("attempts") or 0),
        last_synced_at=_iso(row.get("last_synced_at")),
        notes_through=_iso(row.get("notes_through")),
        created_at=_iso(row.get("created_at")),
        updated_at=_iso(row.get("updated_at")),
    )


@router.get("/crm/{provider}/connect", response_model=CrmConnectOut)
async def crm_connect(
    provider: str,
    tenant: TenantDep,
    container: ContainerDep,
    region: str = Query(default=""),
) -> CrmConnectOut:
    """Start the CRM OAuth flow: the consent screen URL for the owner to open.

    The `state` carries `provider.region` as its second field — the region
    because Zoho's consent screen lives on a per-data-centre host and the
    callback needs to know which one the owner chose, and the whole pair
    because it is HMAC-signed and so needs no session to be trusted on the
    way back.

    A PKCE verifier is minted here and rides as a third dot-segment of that
    field, so it comes back signed without a second store. Only the challenge
    derived from it goes to the CRM; the verifier itself is presented at the
    exchange, which is what stops an attacker finishing somebody else's
    connect with a code from their own consent screen.
    """
    provider = (provider or "").strip().lower()
    if provider not in OAUTH_CRM_PROVIDERS:
        raise ValidationError(f"unknown CRM {provider!r}")
    region = (region or "").strip().lower()
    # the region catalogue comes through the service so this layer never
    # imports an adapter; and HubSpot has no regions, so a stray value must be
    # refused here — signed into the state, a crafted region would surface
    # later as an "invalid OAuth state" that names neither cause nor fix
    regions = container.growth_crm.regions(provider)
    if provider == "hubspot" and region:
        raise ValidationError("HubSpot has no data centres to pick from")
    if provider == "zoho" and region and region not in regions:
        raise ValidationError(
            f"unknown Zoho region {region!r}: pick one of {sorted(regions)}"
        )
    import secrets

    verifier = secrets.token_urlsafe(32)
    # the second state field must contain a dot (parse_oauth_state enforces
    # it, to reject half-formed states), so `hubspot.app` — a constant the
    # callback ignores — stands in for HubSpot's lack of regions. The PKCE
    # verifier is dot-free by construction (token_urlsafe's alphabet is
    # [A-Za-z0-9_-]), so appending it keeps the field parseable by partition.
    state_name = f"{provider}.{region or 'app'}.{verifier}"
    state = _oauth_state(container, tenant.tenant_id, state_name)
    url = await container.growth_crm.authorize(
        tenant.tenant_id, provider, region=region, state=state, verifier=verifier
    )
    return CrmConnectOut(authorize_url=url)


@router.get("/crm/links", response_model=CrmLinksOut)
async def crm_links(
    tenant: TenantDep,
    container: ContainerDep,
    provider: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> CrmLinksOut:
    """The CRM links screen: one page of rows plus the per-provider header."""
    result = await container.growth_crm.links(
        tenant.tenant_id,
        provider=provider,
        status=status,
        limit=limit,
        offset=offset,
    )
    return CrmLinksOut(
        items=[_crm_link_out(row) for row in result.get("items") or []],
        summary=[
            CrmLinkSummaryOut(
                provider=str(row.get("provider") or ""),
                status=str(row.get("status") or ""),
                total=int(row.get("total") or 0),
            )
            for row in result.get("summary") or []
        ],
        total=int(result.get("total") or 0),
    )


@router.post("/crm/{provider}/resync", response_model=CrmResyncOut)
async def crm_resync(
    provider: str, tenant: TenantDep, container: ContainerDep
) -> CrmResyncOut:
    """Queue a push for every lead this CRM has never been linked to."""
    result = await container.growth_crm.resync(tenant.tenant_id, provider)
    return CrmResyncOut(queued=int(result.get("queued") or 0))


@router.post("/crm/links/{lead_id}/retry", response_model=dict[str, Any])
async def crm_retry_link(
    lead_id: UUID, body: CrmRetryIn, tenant: TenantDep, container: ContainerDep
) -> dict[str, Any]:
    """The per-row Retry button: one lead, pushed now, backoff waived."""
    await container.growth_crm.retry(tenant.tenant_id, lead_id, body.provider)
    return {"ok": True}



# How long a minted OAuth state stays usable. The round trip it covers is one
# owner pressing Approve on a screen that is already open, so this is generous.
OAUTH_STATE_TTL_SECONDS = 600


def _oauth_state(container: Any, tenant_id: UUID, shop: str) -> str:
    """Signed OAuth state: the callback trusts this, not a session."""
    import base64
    import hashlib
    import hmac
    import time

    nonce = str(int(time.time()))
    body = f"{tenant_id}:{shop}:{nonce}"
    sig = hmac.new(
        container.settings.SECRET_KEY.encode(), body.encode(), hashlib.sha256
    ).hexdigest()
    return base64.urlsafe_b64encode(f"{body}:{sig}".encode()).decode("ascii")


def parse_oauth_state(container: Any, state: str) -> tuple[UUID, str]:
    """Verify a callback state and return the (tenant, shop) it names.

    Expiring the nonce is what makes a leaked state useless. The signature never
    stops verifying, so without a TTL a `state` captured out of a browser
    history, a proxy log or a Referer header stays a valid credential for
    completing an install against that tenant forever. Ten minutes is the whole
    length of a real OAuth round trip — the owner is already sitting in front of
    the approve screen when it is minted.
    """
    import base64
    import hashlib
    import hmac
    import time

    try:
        raw_body = base64.urlsafe_b64decode(state.encode("ascii"))
        body = raw_body.decode("utf-8")
        tenant_raw, shop, nonce, sig = body.split(":", 3)
    except Exception:
        raise ValidationError("invalid OAuth state") from None
    if base64.urlsafe_b64encode(raw_body).decode("ascii") != state:
        # base64 silently drops a trailing partial quantum, so "valid state +
        # junk" would otherwise decode to the valid state. The re-encode is
        # what makes any mutation — append, truncate, flip — a mismatch.
        raise ValidationError("invalid OAuth state")
    expected = hmac.new(
        container.settings.SECRET_KEY.encode("utf-8"),
        body.rsplit(":", 1)[0].encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise ValidationError("invalid OAuth state")
    # only after the signature: an unauthenticated caller must not get a
    # different answer for "expired" than for "forged"
    try:
        age = time.time() - float(nonce)
    except (TypeError, ValueError):
        raise ValidationError("invalid OAuth state") from None
    if age < -OAUTH_STATE_TTL_SECONDS or age > OAUTH_STATE_TTL_SECONDS:
        # a future-dated nonce is rejected on the same rule as a stale one: it
        # can only come from a clock we do not control, which is to say from
        # somebody constructing the value rather than from _oauth_state
        raise ValidationError("that connect link has expired — start again")
    try:
        tenant_id = UUID(tenant_raw)
    except (ValueError, TypeError):
        raise ValidationError("invalid OAuth state") from None
    if not shop or "." not in shop:
        raise ValidationError("invalid OAuth state")
    return tenant_id, shop


def _oauth_redirect(container: Any) -> str:
    base = str(container.settings.PUBLIC_BASE_URL or "").rstrip("/")
    return f"{base}/v1/growth/stores/shopify/callback"


# ----------------------------------------------------------------- customers


class CustomerOut(BaseModel):
    id: str
    name: str = ""
    phone: str = ""
    email: str = ""
    tags: list[str] = Field(default_factory=list[str])
    consent: dict[str, Any] = Field(default_factory=dict[str, Any])
    language: str | None = None
    first_order_at: Any = None
    last_order_at: Any = None
    total_spend_micros: int = 0
    order_count: int = 0
    created_at: Any = None


class CustomerListOut(BaseModel):
    customers: list[CustomerOut] = Field(default_factory=list[CustomerOut])
    total: int = 0


class CustomerDetailOut(BaseModel):
    customer: CustomerOut
    orders: list[dict[str, Any]] = Field(default_factory=list[dict[str, Any]])


def _customer_out(row: dict[str, Any]) -> CustomerOut:
    return CustomerOut(
        id=str(row["id"]),
        name=str(row.get("name") or ""),
        phone=str(row.get("phone") or ""),
        email=str(row.get("email") or ""),
        tags=[str(t) for t in (row.get("tags") or []) if isinstance(t, str)],
        consent=dict[str, Any](row.get("consent") or {}),
        language=row.get("language"),
        first_order_at=row.get("first_order_at"),
        last_order_at=row.get("last_order_at"),
        total_spend_micros=int(row.get("total_spend_micros") or 0),
        order_count=int(row.get("order_count") or 0),
        created_at=row.get("created_at"),
    )


@router.get("/customers", response_model=CustomerListOut)
async def list_customers(
    tenant: TenantDep,
    container: ContainerDep,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> CustomerListOut:
    """Store customers, newest order first, with the total for the header."""
    rows = await container.growth.customers.list(tenant.tenant_id, limit=limit, offset=offset)
    total = await container.growth.customers.count(tenant.tenant_id)
    return CustomerListOut(customers=[_customer_out(r) for r in rows], total=total)


@router.get("/customers/segments", response_model=dict[str, int])
async def customer_segment_counts(
    tenant: TenantDep, container: ContainerDep
) -> dict[str, int]:
    """Live member count per segment, computed — never stored.

    Segments are pure functions over customers + orders evaluated at request
    time, so these counts always agree with what the campaign composer would
    draft right now.

    The three parameterised segments are keyed `segment:value` — one entry per
    channel, per category the orders actually carry, per dealer territory the
    tags actually carry. The bare key is not returned for those: `by_category`
    with no category selects nobody, and a count of zero on a segment that has
    members under every real value reads as "you have no such customers",
    which is a lie the composer would then repeat.
    """
    from datetime import UTC as _UTC
    from datetime import datetime as _datetime

    from app.domain import segments as _segments

    customers = await container.growth.customers.all_for_segments(tenant.tenant_id)
    orders = await container.growth.orders.recent(tenant.tenant_id)
    now = _datetime.now(_UTC)
    counts: dict[str, int] = {}
    for key in _segments.SEGMENT_KEYS:
        try:
            fanout: list[tuple[str, dict[str, Any]]] = []
            if key == "by_channel_consent":
                fanout = [
                    (channel, {"channel": channel}) for channel in ("whatsapp", "email")
                ]
            elif key == "by_category":
                fanout = [
                    (value, {"category": value})
                    for value in _segments.categories(orders)
                ]
            elif key == "dealers_by_territory":
                fanout = [
                    (value, {"territory": value})
                    for value in _segments.territories(customers)
                ]
            if key in _PARAMETERISED_SEGMENTS:
                # no values in the data means no entries at all, not a zero
                for label, params in fanout:
                    members = _segments.compute(key, customers, orders, now, params)
                    counts[f"{key}:{label}"] = len(members)
                continue
            members = _segments.compute(key, customers, orders, now, {})
            counts[key] = len(members)
        except Exception:
            logger.warning("growth_segment_count_failed", segment=key, exc_info=True)
    return counts


@router.get("/customers/{customer_id}", response_model=CustomerDetailOut)
async def get_customer(
    customer_id: UUID, tenant: TenantDep, container: ContainerDep
) -> CustomerDetailOut:
    """One customer and their recent orders, for the drawer."""
    row = await container.growth.customers.get(tenant.tenant_id, customer_id)
    if row is None:
        from app.core.errors import NotFoundError

        raise NotFoundError("no such customer")
    orders = await container.growth.orders.for_customer(tenant.tenant_id, customer_id)
    return CustomerDetailOut(
        customer=_customer_out(row),
        orders=[
            {
                "id": str(o.get("id")),
                "external_id": str(o.get("external_id") or ""),
                "store_provider": str(o.get("store_provider") or ""),
                "total_micros": int(o.get("total_micros") or 0),
                "payment": str(o.get("payment") or ""),
                "status": str(o.get("status") or ""),
                "items": o.get("items") or {},
                "ordered_at": o.get("ordered_at"),
            }
            for o in orders
        ],
    )


# One read from the upload's spooled file at a time. Small enough that the
# overshoot past the 5 MB cap is negligible, large enough not to be a syscall
# per kilobyte.
CSV_READ_CHUNK_BYTES = 64 * 1024


@router.post("/customers/import", response_model=dict[str, Any])
async def import_customers(
    tenant: TenantDep, container: ContainerDep, file: UploadFile = File(...)
) -> dict[str, Any]:
    """Structured CSV import: name/phone/email/last order date/spend/tags.

    The file is parsed as data, never indexed as a document — customer rows
    are structured, and the RAG pipeline would make them unqueryable text.
    Per-row errors come back with the row number; a bad row costs that row.

    The cap is enforced *while* reading. `await file.read()` would pull the whole
    upload into this process's memory first and only then measure it, which
    makes the limit advisory: a 2 GB body is already resident by the time we
    decide it was too big. Reading in chunks and stopping one chunk past the
    limit bounds what an authenticated tenant can make us hold.
    """
    limit = 5 * 1024 * 1024
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(CSV_READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise ValidationError("CSV is too large (max 5 MB)")
        chunks.append(chunk)
    data = b"".join(chunks)
    result: dict[str, Any] = await container.growth_stores.import_csv(tenant.tenant_id, data)
    if result.get("status") == "rejected":
        raise ValidationError(str(result.get("reason") or "could not read this CSV"))
    return result


# ----------------------------------------------------------------- campaigns


class CampaignSegmentIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    key: str = Field(max_length=40)
    params: dict[str, Any] = Field(default_factory=dict[str, Any])


class CampaignIn(BaseModel):
    """Draft a campaign. The audience freezes at draft time.

    `trigger` is owner (this endpoint), insight (Stage 5's one-click), or
    event (the events calendar). `offer_pct` is clamped into the owner-set
    range — asking for more gets the max, never an error and never the ask.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(default="", max_length=200)
    trigger: str = Field(default="owner", max_length=20)
    segment: CampaignSegmentIn
    offer_pct: int | None = Field(default=None, ge=0, le=100)
    event_name: str = Field(default="", max_length=200)


class CampaignOut(BaseModel):
    id: str
    trigger: str
    name: str
    segment: dict[str, Any] = Field(default_factory=dict[str, Any])
    offer: dict[str, Any] = Field(default_factory=dict[str, Any])
    channels: list[str] = Field(default_factory=list[str])
    status: str
    approval_id: str | None = None
    discount_code: str | None = None
    messages: dict[str, str] = Field(default_factory=dict[str, str])
    results: dict[str, Any] = Field(default_factory=dict[str, Any])
    created_at: Any = None
    updated_at: Any = None


def _campaign_out(row: dict[str, Any]) -> CampaignOut:
    messages = row.get("messages") or {}
    return CampaignOut(
        id=str(row["id"]),
        trigger=str(row.get("trigger") or ""),
        name=str(row.get("name") or ""),
        segment=dict[str, Any](row.get("segment") or {}),
        offer=dict[str, Any](row.get("offer") or {}),
        channels=[str(c) for c in (row.get("channels") or [])],
        status=str(row.get("status") or ""),
        approval_id=str(row["approval_id"]) if row.get("approval_id") else None,
        discount_code=str(row["discount_code"]) if row.get("discount_code") else None,
        messages={str(k): str(v) for k, v in messages.items() if isinstance(v, str)},
        results=dict[str, Any](row.get("results") or {}),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


@router.get("/campaigns", response_model=list[CampaignOut])
async def list_campaigns(
    tenant: TenantDep,
    container: ContainerDep,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[CampaignOut]:
    """Newest campaigns first, with audience counts and results inline."""
    rows = await container.growth.campaigns.list(tenant.tenant_id, limit=limit, offset=offset)
    return [_campaign_out(r) for r in rows]


@router.post("/campaigns", response_model=dict[str, Any])
async def create_campaign(
    body: CampaignIn, tenant: TenantDep, container: ContainerDep
) -> dict[str, Any]:
    """Draft a campaign and request the one approval that releases it.

    Returns the campaign, the approval, and the draft preview (audience count
    + sample messages per language) so the composer can show what the owner is
    being asked to approve.
    """
    spec: dict[str, Any] = {
        "name": body.name,
        "trigger": body.trigger,
        "segment": {"key": body.segment.key, "params": body.segment.params},
        "event_name": body.event_name,
    }
    if body.offer_pct is not None:
        spec["offer_pct"] = body.offer_pct
    result = await container.growth_campaigns.create(
        tenant.tenant_id, spec, actor=str(tenant.user_id or "owner")
    )
    return {
        "campaign": _campaign_out(result["campaign"]).model_dump(mode="json"),
        "approval": _approval_out(result["approval"]).model_dump(mode="json"),
        "audience": result["draft"]["segment"].get("count"),
        "channels": result["draft"].get("channels"),
        "samples": result["draft"].get("texts"),
    }


@router.get("/campaigns/{campaign_id}", response_model=CampaignOut)
async def get_campaign(
    campaign_id: UUID, tenant: TenantDep, container: ContainerDep
) -> CampaignOut:
    row = await container.growth.campaigns.get(tenant.tenant_id, campaign_id)
    if row is None:
        from app.core.errors import NotFoundError

        raise NotFoundError("no such campaign")
    return _campaign_out(row)


@router.get("/campaigns/{campaign_id}/results", response_model=dict[str, Any])
async def campaign_results(
    campaign_id: UUID, tenant: TenantDep, container: ContainerDep
) -> dict[str, Any]:
    """Revenue + cost: send stats plus orders attributed by discount code."""
    computed: dict[str, Any] = await container.growth_campaigns.results(
        tenant.tenant_id, campaign_id
    )
    return computed


@router.post("/campaigns/{campaign_id}/cancel", response_model=CampaignOut)
async def cancel_campaign(
    campaign_id: UUID, tenant: TenantDep, container: ContainerDep
) -> CampaignOut:
    """Hold a campaign that has not finished: drop the unsent rows."""
    row = await container.growth.campaigns.get(tenant.tenant_id, campaign_id)
    if row is None:
        from app.core.errors import NotFoundError

        raise NotFoundError("no such campaign")
    if str(row.get("status")) in ("done", "rejected"):
        raise ValidationError(f"a {row.get('status')} campaign cannot be cancelled")
    await container.growth.messages.cancel_queued_for_campaign(tenant.tenant_id, campaign_id)
    updated = await container.growth.campaigns.set_status(
        tenant.tenant_id, campaign_id, "rejected"
    )
    await container.growth.audit.record(
        tenant.tenant_id,
        agent="owner",
        action="campaign_cancelled",
        input={"campaign_id": str(campaign_id)},
        reason=f"cancelled by {tenant.user_id or 'owner'}",
    )
    return _campaign_out(updated or row)


# ------------------------------------------------------------------ insights


class InsightOut(BaseModel):
    """One insight as the panel renders it.

    `uid` is deliberately absent: it is the dedup key, not information, and
    exposing a hash invites a client to build against it. `can_draft` is the
    machine-readable flag; `draft` carries the query-param pre-fill the panel
    passes straight to the campaign composer — no second API call needed, and
    the owner still reviews the audience and offer before anything is sent.
    """

    id: str
    date: str = ""
    type: str
    summary: str
    why: str = ""
    suggested_action: str = ""
    source_url: str = ""
    confidence: int = 1
    urgency: int = 1
    acted_on: str | None = None
    can_draft: bool = False
    draft: dict[str, Any] | None = None
    evidence: dict[str, Any] = Field(default_factory=dict[str, Any])
    created_at: Any = None


class InsightListOut(BaseModel):
    items: list[InsightOut] = Field(default_factory=list[InsightOut])
    total: int = 0
    open: int = 0


class InsightActIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    action: str = Field(max_length=20)


@router.get("/insights", response_model=InsightListOut)
async def list_insights(
    tenant: TenantDep,
    container: ContainerDep,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    open_only: bool = Query(default=False),
) -> InsightListOut:
    """Ranked insights: urgency, then confidence, then newest.

    The panel shows everything, including the quiet-day rows that were never
    pushed to chat — that split is the whole point of the delivery rule.
    """
    result = await container.growth_insights.list(
        tenant.tenant_id, limit=limit, offset=offset, open_only=open_only
    )
    return InsightListOut(
        items=[InsightOut(**item) for item in result["items"]],
        total=int(result["total"]),
        open=int(result["open"]),
    )


@router.post("/insights/run", response_model=dict[str, str])
async def run_insights(tenant: TenantDep, container: ContainerDep) -> dict[str, str]:
    """"Look now" — queue a pass rather than run it inline.

    Same job the daily cron enqueues, so an owner pressing this twice costs one
    pass: every candidate is deduped on (tenant_id, uid).
    """
    queued: dict[str, Any] = await container.growth_insights.request(
        tenant.tenant_id, via=str(tenant.user_id or "owner")
    )
    return {"status": str(queued.get("status") or "queued")}


@router.post("/insights/{insight_id}/act", response_model=InsightOut)
async def act_on_insight(
    insight_id: UUID, body: InsightActIn, tenant: TenantDep, container: ContainerDep
) -> InsightOut:
    """Mark an insight handled: campaign, snooze or ignore.

    The values match the table's CHECK. Recording it here is what shrinks the
    panel's open list; drafting the campaign itself is the next call.
    """
    if body.action not in ("campaign", "snooze", "ignore"):
        raise ValidationError("action must be campaign, snooze or ignore")
    updated = await container.growth_insights.act(
        tenant.tenant_id, insight_id, body.action
    )
    if updated is None:
        from app.core.errors import NotFoundError

        raise NotFoundError("no such insight")
    return InsightOut(**updated)


