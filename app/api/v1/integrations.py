"""Tenant-managed WhatsApp connection, rotation, status, and disconnection endpoints."""

from __future__ import annotations

import json
import re
import secrets
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.api.deps import ContainerDep, TenantDep, require_role
from app.core.constants import (
    AUDIT_IDENTITY_SECRET_MINTED,
    AUDIT_IDENTITY_VERIFIER_CREATED,
    AUDIT_IDENTITY_VERIFIER_DELETED,
    CONTACT_ATTRIBUTE_KEY_PATTERN,
    IDENTITY_VERIFY_RATE_PER_MINUTE,
    RL_SCOPE_IDENTITY_VERIFY,
)
from app.core.db import tenant_scope
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.core.security import decrypt_secret, encrypt_secret
from app.services import audit

logger = get_logger(__name__)

router = APIRouter(
    prefix="/integrations",
    tags=["integrations"],
    dependencies=[require_role("owner", "admin")],
)


class WhatsAppIntegrationUpsert(BaseModel):
    """Connection details supplied by a dashboard administrator."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    integration_id: UUID | None = None
    agent_id: UUID | None = None
    waba_id: str = Field(min_length=1, max_length=128)
    phone_number_id: str = Field(min_length=1, max_length=128)
    display_number: str | None = Field(default=None, max_length=64)
    access_token: str | None = Field(default=None, min_length=1, max_length=8192)


class WhatsAppIntegrationOut(BaseModel):
    id: UUID
    agent_id: UUID
    waba_id: str
    phone_number_id: str
    display_number: str | None = None
    status: str
    token_configured: bool
    created_at: datetime | None = None


class WhatsAppEmbeddedSignupStateOut(BaseModel):
    """Whether the one-click Meta flow can launch, and what the browser needs.

    `app_id` and `graph_version` initialise Meta's JS SDK; `config_id` launches
    the Embedded Signup dialog. None of these is secret. `configured` is false
    when either is missing, or when `config_id` is a local placeholder — the
    dashboard then offers the manual connect path instead of a flow that would
    fail at Meta.
    """

    configured: bool = False
    app_id: str = ""
    config_id: str = ""
    graph_version: str = ""


class WhatsAppTemplate(BaseModel):
    """A template the tenant has registered and had approved at Meta.

    Stored locally so the send path can reopen a closed 24h service window with
    an approved template. This is the tenant's record of what exists at Meta, not
    the Meta-side definition; creation and approval happen in Meta's console.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=512)
    language: str = Field(default="en", min_length=2, max_length=16)
    purpose: str | None = Field(default=None, max_length=200)


class WhatsAppTemplatesUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    templates: list[WhatsAppTemplate] = Field(default_factory=list, max_length=200)


class EmbeddedSignupRequest(BaseModel):
    """Payload from Meta's Embedded Signup JS SDK.

    The SDK returns a short-lived ``code`` plus the newly-created ``waba_id`` and
    ``phone_number_id``. We exchange the code for a long-lived business token
    server-side (the app secret never reaches the browser) and store the result
    exactly like a manual connection.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    code: str = Field(min_length=1, max_length=4096)
    waba_id: str = Field(min_length=1, max_length=128)
    phone_number_id: str = Field(min_length=1, max_length=128)
    display_number: str | None = Field(default=None, max_length=64)
    agent_id: UUID | None = None


class WhatsAppVerifyResult(BaseModel):
    ok: bool
    display_number: str | None = None
    verified_name: str | None = None
    quality_rating: str | None = None
    subscribed: bool = False
    message: str



@router.get("/whatsapp", response_model=list[WhatsAppIntegrationOut])
async def list_whatsapp_integrations(
    tenant: TenantDep,
) -> list[WhatsAppIntegrationOut]:
    async with tenant_scope(tenant.tenant_id) as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT id, agent_id, waba_id, phone_number_id, display_number,
                           status, (token_ref IS NOT NULL) AS token_configured,
                           created_at
                      FROM whatsapp_integrations
                     ORDER BY created_at DESC
                    """
                )
            )
        ).mappings().all()
    return [WhatsAppIntegrationOut(**dict(row)) for row in rows]


@router.put("/whatsapp", response_model=WhatsAppIntegrationOut)
async def upsert_whatsapp_integration(
    payload: WhatsAppIntegrationUpsert,
    tenant: TenantDep,
    container: ContainerDep,
) -> WhatsAppIntegrationOut:
    """Create, reconnect, update, or rotate a tenant's WhatsApp connection."""

    new_ref: str | None = None
    ciphertext: str | None = None
    if payload.access_token is not None:
        new_ref = f"whatsapp:{uuid4().hex}"
        ciphertext = encrypt_secret(payload.access_token, container.settings.ENCRYPTION_KEY)

    try:
        async with tenant_scope(tenant.tenant_id) as session:
            if payload.integration_id is not None:
                existing = (
                    await session.execute(
                        text(
                            "SELECT id, agent_id, token_ref FROM whatsapp_integrations "
                            "WHERE id = :id FOR UPDATE"
                        ),
                        {"id": payload.integration_id},
                    )
                ).mappings().first()
                if existing is None:
                    raise NotFoundError("no such WhatsApp integration")
            else:
                existing = (
                    await session.execute(
                        text(
                            "SELECT id, agent_id, token_ref FROM whatsapp_integrations "
                            "WHERE phone_number_id = :phone_number_id FOR UPDATE"
                        ),
                        {"phone_number_id": payload.phone_number_id},
                    )
                ).mappings().first()

            agent_id = payload.agent_id or (
                existing["agent_id"] if existing is not None else tenant.agent_id
            )
            if agent_id is None:
                agent_id = await _default_agent_id(session)
            if agent_id is None or not await _agent_exists(session, agent_id):
                raise ValidationError("agent does not belong to this tenant")

            old_ref = existing["token_ref"] if existing is not None else None
            token_ref = new_ref or old_ref
            if token_ref is None:
                raise ValidationError("access_token is required for a new connection")

            if new_ref is not None and ciphertext is not None:
                await session.execute(
                    text(
                        """
                        INSERT INTO tenant_secrets (tenant_id, ref, provider, ciphertext)
                        VALUES (:tenant_id, :ref, 'whatsapp', :ciphertext)
                        """
                    ),
                    {
                        "tenant_id": tenant.tenant_id,
                        "ref": new_ref,
                        "ciphertext": ciphertext,
                    },
                )

            if existing is None:
                integration_id = uuid4()
                await session.execute(
                    text(
                        """
                        INSERT INTO whatsapp_integrations (
                            id, tenant_id, agent_id, waba_id, phone_number_id,
                            display_number, token_ref, status
                        ) VALUES (
                            :id, :tenant_id, :agent_id, :waba_id, :phone_number_id,
                            :display_number, :token_ref, 'active'
                        )
                        """
                    ),
                    {
                        "id": integration_id,
                        "tenant_id": tenant.tenant_id,
                        "agent_id": agent_id,
                        "waba_id": payload.waba_id,
                        "phone_number_id": payload.phone_number_id,
                        "display_number": payload.display_number,
                        "token_ref": token_ref,
                    },
                )
            else:
                integration_id = existing["id"]
                await session.execute(
                    text(
                        """
                        UPDATE whatsapp_integrations
                           SET agent_id = :agent_id,
                               waba_id = :waba_id,
                               phone_number_id = :phone_number_id,
                               display_number = :display_number,
                               token_ref = :token_ref,
                               status = 'active'
                         WHERE id = :id
                        """
                    ),
                    {
                        "id": integration_id,
                        "agent_id": agent_id,
                        "waba_id": payload.waba_id,
                        "phone_number_id": payload.phone_number_id,
                        "display_number": payload.display_number,
                        "token_ref": token_ref,
                    },
                )

            if new_ref is not None and old_ref is not None and old_ref != new_ref:
                await session.execute(
                    text(
                        "DELETE FROM tenant_secrets "
                        "WHERE tenant_id = :tenant_id AND ref = :ref AND provider = 'whatsapp'"
                    ),
                    {"tenant_id": tenant.tenant_id, "ref": old_ref},
                )

            row = (
                await session.execute(
                    text(
                        """
                        SELECT id, agent_id, waba_id, phone_number_id, display_number,
                               status, (token_ref IS NOT NULL) AS token_configured,
                               created_at
                          FROM whatsapp_integrations
                         WHERE id = :id
                        """
                    ),
                    {"id": integration_id},
                )
            ).mappings().one()
    except IntegrityError as exc:
        raise ConflictError("phone number is already connected") from exc

    return WhatsAppIntegrationOut(**dict(row))


@router.delete("/whatsapp/{integration_id}", status_code=204)
async def disconnect_whatsapp_integration(
    integration_id: UUID,
    tenant: TenantDep,
) -> None:
    """Disable inbound routing and remove the decryptable access token."""

    async with tenant_scope(tenant.tenant_id) as session:
        existing = (
            await session.execute(
                text(
                    "SELECT token_ref FROM whatsapp_integrations "
                    "WHERE id = :id FOR UPDATE"
                ),
                {"id": integration_id},
            )
        ).mappings().first()
        if existing is None:
            raise NotFoundError("no such WhatsApp integration")

        await session.execute(
            text(
                "UPDATE whatsapp_integrations "
                "SET status = 'disconnected', token_ref = NULL WHERE id = :id"
            ),
            {"id": integration_id},
        )
        if existing["token_ref"] is not None:
            await session.execute(
                text(
                    "DELETE FROM tenant_secrets "
                    "WHERE tenant_id = :tenant_id AND ref = :ref AND provider = 'whatsapp'"
                ),
                {"tenant_id": tenant.tenant_id, "ref": existing["token_ref"]},
            )


@router.post("/whatsapp/{integration_id}/verify", response_model=WhatsAppVerifyResult)
async def verify_whatsapp_integration(
    integration_id: UUID, tenant: TenantDep, container: ContainerDep
) -> WhatsAppVerifyResult:
    """Confirm a stored WhatsApp token still authenticates against Meta.

    Reads the phone number's live metadata through the Graph API. A failure is
    reduced to a short reason; the token itself is never returned.
    """
    import httpx

    token_record = await container.whatsapp_repo.get_access_token_record(
        tenant.tenant_id, integration_id
    )
    if token_record is None or token_record.provider != "whatsapp":
        raise NotFoundError("no active WhatsApp integration with a stored token")

    integration = await container.whatsapp_repo.get_integration(
        tenant.tenant_id, integration_id
    )
    if integration is None:
        raise NotFoundError("no such WhatsApp integration")

    try:
        access_token = decrypt_secret(
            token_record.ciphertext, container.settings.ENCRYPTION_KEY
        )
    except Exception:
        logger.warning("whatsapp_verify_undecryptable", exc_info=True)
        return WhatsAppVerifyResult(ok=False, message="stored token could not be decrypted")

    version = container.settings.WHATSAPP_GRAPH_VERSION
    url = f"https://graph.facebook.com/{version}/{integration.phone_number_id}"
    try:
        async with httpx.AsyncClient(timeout=container.settings.HTTP_TIMEOUT_SECONDS) as client:
            resp = await client.get(
                url,
                params={"fields": "display_phone_number,verified_name,quality_rating"},
                headers={"Authorization": f"Bearer {access_token}"},
            )
    except httpx.HTTPError:
        logger.info("whatsapp_verify_unreachable", exc_info=True)
        return WhatsAppVerifyResult(ok=False, message="could not reach Meta")

    if resp.status_code != 200:
        return WhatsAppVerifyResult(ok=False, message=_whatsapp_failure_reason(resp))

    data = resp.json() if resp.content else {}
    # A verified token that receives nothing is the classic silent half-connect:
    # the app also has to be subscribed to the WABA's webhook fields, which is
    # normally a manual step in the Meta console. Do it here — the tenant just
    # clicked Verify — so inbound messages flow without another console visit.
    # Failure is reported, never fatal: a subscription made earlier still holds.
    subscribed = False
    if integration.waba_id:
        try:
            async with httpx.AsyncClient(
                timeout=container.settings.HTTP_TIMEOUT_SECONDS
            ) as client:
                sub = await client.post(
                    f"https://graph.facebook.com/{version}/{integration.waba_id}/subscribed_apps",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
            subscribed = sub.status_code == 200
            if not subscribed:
                logger.warning(
                    "whatsapp_webhook_subscribe_failed",
                    waba_id=integration.waba_id,
                    status=sub.status_code,
                )
        except httpx.HTTPError:
            logger.info("whatsapp_webhook_subscribe_unreachable", exc_info=True)
    return WhatsAppVerifyResult(
        ok=True,
        display_number=data.get("display_phone_number"),
        verified_name=data.get("verified_name"),
        quality_rating=data.get("quality_rating"),
        subscribed=subscribed,
        message="token verified"
        + (" and webhooks subscribed" if subscribed else ""),
    )


@router.post("/whatsapp/embedded-signup", response_model=WhatsAppIntegrationOut)
async def whatsapp_embedded_signup(
    payload: EmbeddedSignupRequest, tenant: TenantDep, container: ContainerDep
) -> WhatsAppIntegrationOut:
    """Complete Meta Embedded Signup: exchange the code, then store the token.

    This is the productisation.md §3.2 path — no manual token paste. The app
    secret exchanges the browser-supplied code for a business access token
    server-side; the token is then encrypted and stored exactly like the manual
    connection route.
    """
    import httpx

    settings = container.settings
    if not (settings.WHATSAPP_APP_ID and settings.WHATSAPP_APP_SECRET):
        raise ValidationError("Embedded Signup is not configured on this deployment")

    token_url = (
        f"https://graph.facebook.com/{settings.WHATSAPP_GRAPH_VERSION}/oauth/access_token"
    )
    async with httpx.AsyncClient(timeout=settings.HTTP_TIMEOUT_SECONDS) as client:
        resp = await client.get(
            token_url,
            params={
                "client_id": settings.WHATSAPP_APP_ID,
                "client_secret": settings.WHATSAPP_APP_SECRET,
                "code": payload.code,
            },
        )
    if resp.status_code != 200:
        raise ValidationError("Meta rejected the Embedded Signup code")
    access_token = resp.json().get("access_token")
    if not access_token:
        raise ValidationError("Meta did not return an access token")

    # reuse the same upsert path as manual connection
    return await upsert_whatsapp_integration(
        WhatsAppIntegrationUpsert(
            agent_id=payload.agent_id,
            waba_id=payload.waba_id,
            phone_number_id=payload.phone_number_id,
            display_number=payload.display_number,
            access_token=access_token,
        ),
        tenant,
        container,
    )


@router.get("/whatsapp/embedded-signup/state", response_model=WhatsAppEmbeddedSignupStateOut)
async def whatsapp_embedded_signup_state(
    container: ContainerDep,
) -> WhatsAppEmbeddedSignupStateOut:
    """The values the dashboard's Meta JS SDK needs to run Embedded Signup.

    Platform-level configuration read by the browser, not tenant data — the same
    shape as the email card's state endpoint. `configured` gates the one-click
    button on the dashboard; when false it falls back to manual entry.
    """
    settings = container.settings
    config_id = settings.WHATSAPP_CONFIG_ID
    configured = bool(
        settings.WHATSAPP_APP_ID
        and config_id
        and not config_id.lower().startswith("dummy")
    )
    return WhatsAppEmbeddedSignupStateOut(
        configured=configured,
        app_id=settings.WHATSAPP_APP_ID,
        config_id=config_id,
        graph_version=settings.WHATSAPP_GRAPH_VERSION,
    )


@router.get("/whatsapp/{integration_id}/templates", response_model=list[WhatsAppTemplate])
async def list_whatsapp_templates(
    integration_id: UUID, tenant: TenantDep
) -> list[WhatsAppTemplate]:
    async with tenant_scope(tenant.tenant_id) as session:
        row = (
            await session.execute(
                text("SELECT template_config FROM whatsapp_integrations WHERE id = :id"),
                {"id": integration_id},
            )
        ).mappings().first()
    if row is None:
        raise NotFoundError("no such WhatsApp integration")
    return _parse_templates(row["template_config"])


@router.put("/whatsapp/{integration_id}/templates", response_model=list[WhatsAppTemplate])
async def set_whatsapp_templates(
    integration_id: UUID, payload: WhatsAppTemplatesUpdate, tenant: TenantDep
) -> list[WhatsAppTemplate]:
    """Record the approved templates available to reopen a closed service window.

    Templates are created and approved in Meta's console; this stores the tenant's
    record of them, keyed by name + language, which the send path consults.
    """
    config = {"templates": [t.model_dump() for t in payload.templates]}
    async with tenant_scope(tenant.tenant_id) as session:
        result = await session.execute(
            text(
                "UPDATE whatsapp_integrations SET template_config = CAST(:cfg AS jsonb) "
                "WHERE id = :id"
            ),
            {"cfg": json.dumps(config), "id": integration_id},
        )
        if result.rowcount == 0:
            raise NotFoundError("no such WhatsApp integration")
    return payload.templates


def _parse_templates(raw: Any) -> list[WhatsAppTemplate]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    if not isinstance(raw, dict):
        return []
    items = raw.get("templates", [])
    result: list[WhatsAppTemplate] = []
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict) and item.get("name"):
                result.append(
                    WhatsAppTemplate(
                        name=str(item["name"]),
                        language=str(item.get("language", "en")),
                        purpose=item.get("purpose"),
                    )
                )
    return result


async def _default_agent_id(session: Any) -> UUID | None:
    row = (
        await session.execute(
            text("SELECT id FROM agents WHERE is_default = true ORDER BY created_at LIMIT 1")
        )
    ).mappings().first()
    return row["id"] if row is not None else None


async def _agent_exists(session: Any, agent_id: UUID) -> bool:
    row = (
        await session.execute(
            text("SELECT id FROM agents WHERE id = :id"), {"id": agent_id}
        )
    ).first()
    return row is not None


def _whatsapp_failure_reason(resp: Any) -> str:
    """A short reason for a non-200 Graph response, without leaking the body."""
    if resp.status_code in (401, 403):
        return "token invalid or expired"
    if resp.status_code == 404:
        return "phone number not found"
    if resp.status_code == 429:
        return "rate limited by Meta"
    code: Any = None
    try:
        body = resp.json()
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict):
                code = error.get("code")
    except ValueError:
        pass
    suffix = f", code {code}" if code is not None else ""
    return f"Meta returned HTTP {resp.status_code}{suffix}"


# --------------------------------------------------------------- verified identity
#
# A verifier row is the tenant's half of the identity handshake: the shared HS256
# secret their backend signs a short-lived token with, plus the `iss`, `aud` and
# TTL that token must satisfy. Two rows may exist per (tenant, agent) at once, and
# that is the whole rotation story — mint the second, deploy it, delete the first.
#
# The plaintext secret leaves the system exactly once, in the 201 response. It is
# stored only as ciphertext in `tenant_secrets`, and every read path here returns
# the ref instead.


class IdentityVerifierCreate(BaseModel):
    """What a dashboard administrator supplies. The secret is not one of them —
    it is minted here, because a tenant-chosen shared secret is a weak one."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    agent_id: UUID | None = None
    issuer: str | None = Field(default=None, max_length=200)
    audience: str | None = Field(default=None, max_length=200)
    max_ttl_seconds: int = Field(default=300, ge=30, le=600)
    attribute_allowlist: list[str] | None = Field(default=None, max_length=32)


class IdentityVerifierPatch(BaseModel):
    """Claim expectations only. `alg` and `secret_ref` are absent by design:
    the algorithm is fixed by a CHECK constraint, and rotating a secret is a new
    row plus a delete, which is what gives both keys a window where they work."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    issuer: str | None = Field(default=None, max_length=200)
    audience: str | None = Field(default=None, max_length=200)
    max_ttl_seconds: int | None = Field(default=None, ge=30, le=600)
    attribute_allowlist: list[str] | None = Field(default=None, max_length=32)
    # Explicit clears, because `null` in a PATCH body already means "leave alone".
    clear_issuer: bool = False
    clear_audience: bool = False
    clear_allowlist: bool = False


class IdentityVerifierOut(BaseModel):
    id: UUID
    agent_id: UUID | None = None
    alg: str = "HS256"
    secret_ref: str
    issuer: str | None = None
    audience: str | None = None
    max_ttl_seconds: int
    attribute_allowlist: list[str] = Field(default_factory=list)
    last_verified_at: datetime | None = None
    last_rejected_at: datetime | None = None
    # Surfaced so a dashboard can warn about an integration that is minting
    # tokens nothing accepts. A rising count with no verification is the signal.
    reject_count: int = 0
    created_at: datetime | None = None


class IdentityVerifierCreated(IdentityVerifierOut):
    """The only response that carries the plaintext secret."""

    secret: str


class IdentityVerifyTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    token: str = Field(min_length=1, max_length=8192)
    agent_id: UUID | None = None
    # The audience the token claims, when the tenant is testing a value that is
    # not the tenant slug. It is added to the runtime audience set, never
    # substituted for it, so this cannot be used to accept a wider audience than
    # a real verification would.
    audience: str | None = Field(default=None, max_length=200)


class IdentityVerifyTestResult(BaseModel):
    verified: bool
    reason: str | None = None
    # The claims the verifier accepted, as strings. The signature segment is never
    # part of this: nothing here echoes it back.
    claims: dict[str, str] = Field(default_factory=dict)
    contact_id: UUID | None = None
    name: str | None = None


def _verifier_out(row: dict[str, Any]) -> IdentityVerifierOut:
    return IdentityVerifierOut(
        id=row["id"],
        agent_id=row.get("agent_id"),
        alg=str(row.get("alg") or "HS256"),
        secret_ref=str(row.get("secret_ref") or ""),
        issuer=row.get("issuer"),
        audience=row.get("audience"),
        max_ttl_seconds=int(row.get("max_ttl_seconds") or 300),
        attribute_allowlist=list(row.get("attribute_allowlist") or []),
        last_verified_at=row.get("last_verified_at"),
        last_rejected_at=row.get("last_rejected_at"),
        reject_count=int(row.get("reject_count") or 0),
        created_at=row.get("created_at"),
    )


def _checked_allowlist(values: list[str] | None) -> list[str] | None:
    """Validate allowlisted attribute names here so the error names the bad one.

    These names end up interpolated into action URLs, so they are held to the same
    shape as a stored attribute key rather than accepted as free text.
    """
    if values is None:
        return None
    cleaned: list[str] = []
    for value in values:
        name = value.strip()
        if not re.fullmatch(CONTACT_ATTRIBUTE_KEY_PATTERN, name):
            raise ValidationError(f"'{name}' is not a valid attribute name")
        if name not in cleaned:
            cleaned.append(name)
    return cleaned


@router.post(
    "/identity/verifiers", response_model=IdentityVerifierCreated, status_code=201
)
async def create_identity_verifier(
    payload: IdentityVerifierCreate, tenant: TenantDep, container: ContainerDep
) -> IdentityVerifierCreated:
    """Mint a signing secret and the verifier row that trusts it.

    The plaintext secret is in this response and nowhere else, ever again: only
    the Fernet ciphertext is persisted, and no read path can recover it. A tenant
    that loses it mints a second verifier and deletes the first.
    """
    from app.adapters.db.contacts_repo import MAX_ACTIVE_VERIFIERS

    allowlist = _checked_allowlist(payload.attribute_allowlist)

    if payload.agent_id is not None:
        async with tenant_scope(tenant.tenant_id) as session:
            if not await _agent_exists(session, payload.agent_id):
                raise NotFoundError("no such agent")

    # Two is the rotation window, not a quota. A third row would mean a secret
    # nobody remembers deploying is still trusted.
    existing = await container.contacts_repo.count_verifiers(
        tenant.tenant_id, payload.agent_id
    )
    if existing >= MAX_ACTIVE_VERIFIERS:
        raise ConflictError(
            "two verifiers already exist for this agent; delete the retired one first"
        )

    # 32 bytes from the OS CSPRNG. Long enough that an HS256 secret is not the
    # weak link, and generated here so it never was a password someone chose.
    secret = secrets.token_urlsafe(32)
    ref = f"identity:{uuid4().hex}"
    row = await container.contacts_repo.create_verifier(
        tenant.tenant_id,
        agent_id=payload.agent_id,
        secret_ref=ref,
        ciphertext=encrypt_secret(secret, container.settings.ENCRYPTION_KEY),
        issuer=payload.issuer,
        audience=payload.audience,
        max_ttl_seconds=payload.max_ttl_seconds,
        attribute_allowlist=allowlist,
    )
    if not row:
        raise ConflictError("the verifier could not be created")

    await audit.record(
        AUDIT_IDENTITY_VERIFIER_CREATED,
        actor_type="user",
        actor_id=tenant.user_id,
        target_tenant=tenant.tenant_id,
        target_type="identity_verifier",
        target_id=str(row["id"]),
        detail={
            "agent_id": str(payload.agent_id) if payload.agent_id else None,
            "issuer": payload.issuer,
            "audience": payload.audience,
            "max_ttl_seconds": payload.max_ttl_seconds,
        },
    )
    # A separate row for the mint itself: the secret left the system at this
    # moment, and that is the fact an incident review needs, separate from the
    # configuration change.
    await audit.record(
        AUDIT_IDENTITY_SECRET_MINTED,
        actor_type="user",
        actor_id=tenant.user_id,
        target_tenant=tenant.tenant_id,
        target_type="tenant_secret",
        target_id=ref,
        detail={"provider": "identity_hs256"},
    )
    return IdentityVerifierCreated(**_verifier_out(row).model_dump(), secret=secret)


@router.get("/identity/verifiers", response_model=list[IdentityVerifierOut])
async def list_identity_verifiers(
    tenant: TenantDep, container: ContainerDep
) -> list[IdentityVerifierOut]:
    """Every verifier for the tenant: refs and metadata, never the ciphertext."""
    rows = await container.contacts_repo.list_verifiers(tenant.tenant_id)
    return [_verifier_out(row) for row in rows]


@router.patch(
    "/identity/verifiers/{verifier_id}", response_model=IdentityVerifierOut
)
async def update_identity_verifier(
    verifier_id: UUID,
    payload: IdentityVerifierPatch,
    tenant: TenantDep,
    container: ContainerDep,
) -> IdentityVerifierOut:
    """Edit `issuer`, `audience`, `max_ttl_seconds` and `attribute_allowlist`."""
    allowlist = _checked_allowlist(payload.attribute_allowlist)
    row = await container.contacts_repo.update_verifier(
        tenant.tenant_id,
        verifier_id,
        issuer=payload.issuer,
        audience=payload.audience,
        max_ttl_seconds=payload.max_ttl_seconds,
        attribute_allowlist=allowlist,
        clear_issuer=payload.clear_issuer,
        clear_audience=payload.clear_audience,
        clear_allowlist=payload.clear_allowlist,
    )
    if row is None:
        raise NotFoundError("no such verifier")
    return _verifier_out(row)


@router.delete("/identity/verifiers/{verifier_id}", status_code=204)
async def delete_identity_verifier(
    verifier_id: UUID, tenant: TenantDep, container: ContainerDep
) -> None:
    """Complete a rotation by retiring the old verifier and its secret."""
    ref = await container.contacts_repo.delete_verifier(tenant.tenant_id, verifier_id)
    if ref is None:
        raise NotFoundError("no such verifier")

    await audit.record(
        AUDIT_IDENTITY_VERIFIER_DELETED,
        actor_type="user",
        actor_id=tenant.user_id,
        target_tenant=tenant.tenant_id,
        target_type="identity_verifier",
        target_id=str(verifier_id),
        detail={"secret_ref": ref},
    )


@router.post("/identity/verify-test", response_model=IdentityVerifyTestResult)
async def verify_identity_token_test(
    payload: IdentityVerifyTestRequest, tenant: TenantDep, container: ContainerDep
) -> IdentityVerifyTestResult:
    """Paste a token; get back the claims that were accepted, or why it was not.

    This calls `IdentityService.establish` — the same verification path any
    server-side token check runs, with the same verifier rows, the same four
    checks and the same replay claim. It is deliberately not a parallel
    implementation: a debugging tool that accepts something the runtime would
    reject teaches a tenant that their broken integration works.

    Two consequences follow from that and are intended. The token's `jti` is
    consumed, so the same token tested twice is correctly reported as replayed on
    the second attempt. And a token that verifies upserts a real contact, because
    that is what verifying a token does.

    The response never contains the signature segment, and the raw token is never
    stored or logged.
    """
    from app.core.identity import REJECT_RATE_LIMITED

    # Its own limiter identifier, so an administrator hammering the debug tool
    # cannot exhaust the budget live verification runs under. A 429 here is a real
    # 429 rather than a rejection reason, because the token was never examined.
    await container.rate_limiter.check(
        RL_SCOPE_IDENTITY_VERIFY,
        f"test:{tenant.tenant_id}",
        IDENTITY_VERIFY_RATE_PER_MINUTE,
        60,
    )

    # The audience a token minted for this tenant would present: its slug. The
    # caller may add one more when testing a value not minted against the slug;
    # it is appended, never substituted, so this cannot widen what verification
    # accepts.
    audiences = [tenant.tenant_slug] if tenant.tenant_slug else []
    if payload.audience:
        audiences.append(payload.audience)

    outcome = await container.identity_service.establish(
        tenant_id=tenant.tenant_id,
        agent_id=payload.agent_id,
        token=payload.token,
        audiences=tuple(audiences),
        session_ttl_seconds=container.settings.SESSION_TTL_SECONDS,
    )
    if not outcome.verified and outcome.reason == REJECT_RATE_LIMITED:
        from app.core.errors import RateLimitError

        raise RateLimitError("too many identity verification attempts")

    return IdentityVerifyTestResult(
        verified=outcome.verified,
        reason=outcome.reason,
        claims=outcome.claims,
        contact_id=outcome.contact_id,
        name=outcome.name,
    )

