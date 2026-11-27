"""Dashboard identity — Google sign-in only.

`tenancy.from_user_jwt` only requires a signed token carrying `sub` (a user id);
it does not care who issued it. That is what lets this module mint tokens with
our own `sign_token`/`verify_token` now and be replaced by a Supabase Auth (or
any other) adapter later without touching tenant resolution or route code.

Email/password login was removed by owner decision (2026-09-13): the sign-in
button goes straight to Google, and the callback provisions a tenant + owner +
default agent for a first-time Google identity exactly as the old signup did.
"""

from __future__ import annotations

import secrets
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError

from app.adapters.postgres.platform_repo import (
    add_membership,
    create_default_agent,
    create_tenant,
    create_user,
    generate_unique_tenant_slug,
    get_user_by_email,
    get_user_by_id,
    list_memberships_for_user,
)
from app.api.deps import ContainerDep, client_ip
from app.core.errors import AuthenticationError, ConflictError
from app.core.security import hash_password, verify_token
from app.services.auth.google import build_authorization_url, exchange_code

router = APIRouter(prefix="/auth", tags=["auth"])

_ACCESS_TOKEN_TTL_SECONDS = 60 * 60 * 24 * 14  # Two weeks; a signed-out dashboard user re-logs in.

# The Google callback runs an external token exchange, so an unthrottled caller
# can drive paid outbound requests; cap it per IP.
_GOOGLE_IP_PER_MINUTE = 10


class GoogleStartResponse(BaseModel):
    authorization_url: str
    state: str


class GoogleCallbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    code: str = Field(min_length=1, max_length=2048)
    # The SPA echoes back the redirect_uri it used so the exchange matches the
    # consent request; absent, we fall back to the server-configured one.
    redirect_uri: str | None = Field(default=None, max_length=2048)
    # New Google users need a tenant; if the SPA does not collect one we derive
    # a reasonable name rather than block first sign-in.
    tenant_name: str | None = Field(default=None, max_length=200)


class MembershipOut(BaseModel):
    tenant_id: UUID
    slug: str
    name: str
    role: str


class AuthResult(BaseModel):
    access_token: str
    expires_in: int
    tenant_slug: str
    memberships: list[MembershipOut]


class UserOut(BaseModel):
    id: UUID
    email: str
    name: str | None = None
    memberships: list[MembershipOut]


@router.get("/google/start", response_model=GoogleStartResponse)
async def google_start(container: ContainerDep) -> GoogleStartResponse:
    """Hand the caller a Google consent URL to redirect the browser to.

    The `state` is minted here and returned to the caller; the landing page's
    sign-in card stashes it in the `ca.g.state` cookie, and the API-origin
    `/login` callback (`app/api/public.py::login_callback`) requires the same
    value back before it spends an outbound call on the token exchange — so a
    callback that did not originate from this start call is rejected.
    """
    state = secrets.token_urlsafe(24)
    authorization_url = build_authorization_url(
        container.settings.GOOGLE_CLIENT_ID,
        container.settings.GOOGLE_REDIRECT_URI,
        state,
    )
    return GoogleStartResponse(authorization_url=authorization_url, state=state)


@router.post("/google/callback", response_model=AuthResult)
async def google_callback(
    payload: GoogleCallbackRequest, request: Request, container: ContainerDep
) -> AuthResult:
    """Exchange a Google authorization code for one of our session tokens."""
    # Cap per IP before the token exchange: it is an outbound call to Google, so
    # an unthrottled caller here turns into paid egress and provider rate-limit
    # pressure regardless of whether the code is even valid.
    await container.rate_limiter.check(
        "auth-google-ip", client_ip(request), _GOOGLE_IP_PER_MINUTE, 60
    )
    identity = await exchange_code(
        payload.code,
        payload.redirect_uri or container.settings.GOOGLE_REDIRECT_URI,
        container.settings.GOOGLE_CLIENT_ID,
        container.settings.GOOGLE_CLIENT_SECRET,
        container.settings.HTTP_TIMEOUT_SECONDS,
    )
    return await google_identity_to_session(identity, payload.tenant_name, container.settings)


async def google_identity_to_session(
    identity: dict, tenant_name: str | None, settings: Any
) -> AuthResult:
    """Session for a verified Google identity — the whole sign-up-or-sign-in.

    Existing email → sign in with their memberships. New email → provision a
    tenant, owner, and default agent, the one-step onboarding the old signup
    endpoint offered. Shared by the SPA's JSON callback and the landing page's
    redirect callback (app/api/public.py) so both hand the same session over.
    """
    if not identity["email_verified"]:
        raise AuthenticationError("this Google account has no verified email")

    email = identity["email"]
    existing = await get_user_by_email(email)
    if existing is not None:
        if existing.get("status") != "active":
            raise AuthenticationError("account is unavailable")
        memberships = await list_memberships_for_user(existing["id"])
        if not memberships:
            raise AuthenticationError("this account has no tenant to sign in to")
        token = _issue_token(existing["id"], settings.SECRET_KEY)
        return AuthResult(
            access_token=token,
            expires_in=_ACCESS_TOKEN_TTL_SECONDS,
            tenant_slug=memberships[0]["slug"],
            memberships=[MembershipOut(**_membership_fields(m)) for m in memberships],
        )

    # First sign-in: same provisioning path as `signup`, minus a chosen password.
    tenant_name = tenant_name or _derive_tenant_name(email, identity.get("name"))
    slug = await generate_unique_tenant_slug(tenant_name)
    try:
        tenant = await create_tenant(tenant_name, slug)
    except IntegrityError:
        # Same race as signup: two first Google sign-ins deriving the same slug.
        slug = await generate_unique_tenant_slug(f"{tenant_name} {secrets.token_hex(4)}")
        tenant = await create_tenant(tenant_name, slug)
    try:
        # No password was set, so store an unusable hash of a random secret: it is
        # a well-formed hash that no password login can ever match.
        user_id = await create_user(
            email, identity.get("name"), hash_password(secrets.token_urlsafe(32))
        )
    except IntegrityError as exc:
        # A concurrent first sign-in with the same email raced us to create_user.
        raise ConflictError("an account with this email already exists") from exc
    await add_membership(tenant.id, user_id, role="owner")
    await create_default_agent(tenant.id)

    token = _issue_token(user_id, settings.SECRET_KEY)
    return AuthResult(
        access_token=token,
        expires_in=_ACCESS_TOKEN_TTL_SECONDS,
        tenant_slug=slug,
        memberships=[
            MembershipOut(
                tenant_id=tenant.id, slug=slug, name=tenant.name, role="owner"
            )
        ],
    )


@router.get("/me", response_model=UserOut)
async def me(
    container: ContainerDep,
    authorization: Annotated[str | None, Header()] = None,
) -> UserOut:
    """Identity plus every tenant this user can act in.

    Deliberately independent of `current_tenant`: a dashboard needs this list
    *before* a tenant is chosen, and `current_tenant` requires one already.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise AuthenticationError("missing bearer token")
    payload = verify_token(
        authorization.removeprefix("Bearer ").strip(), container.settings.SECRET_KEY
    )
    try:
        user_id = UUID(str(payload["sub"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise AuthenticationError("invalid session token") from exc

    user = await get_user_by_id(user_id)
    if user is None or user.get("status") != "active":
        raise AuthenticationError("account is unavailable")

    memberships = await list_memberships_for_user(user_id)
    return UserOut(
        id=user["id"],
        email=user["email"],
        name=user.get("name"),
        memberships=[MembershipOut(**_membership_fields(m)) for m in memberships],
    )


def _issue_token(user_id: UUID, secret: str) -> str:
    from app.core.security import sign_token

    return sign_token(
        {"sub": str(user_id), "typ": "dashboard_session"}, secret, _ACCESS_TOKEN_TTL_SECONDS
    )


def _derive_tenant_name(email: str, name: str | None) -> str:
    """A workable default org name when a Google first sign-in supplies none.

    Prefer the person's name, else the email's domain label, else the local part —
    `generate_unique_tenant_slug` handles turning it into a unique slug.
    """
    if name and name.strip():
        return name.strip()[:200]
    local, _, domain = email.partition("@")
    label = domain.split(".")[0] if domain else local
    return (label or local or "workspace")[:200]


def _membership_fields(row: dict) -> dict:
    return {
        "tenant_id": row["tenant_id"],
        "slug": row["slug"],
        "name": row["name"],
        "role": row["role"],
    }

