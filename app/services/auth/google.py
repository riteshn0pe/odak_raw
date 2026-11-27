"""Google OAuth helper for dashboard sign-in.

The dashboard is an SPA, so the browser owns the redirect: the server hands out a
consent URL, Google redirects back to the SPA with a `code`, and the SPA posts
that code here to be exchanged for an identity. This module holds the two pure-ish
steps of that dance — building the consent URL and exchanging the code — so the
route in `auth.py` stays about our own user/tenant provisioning.
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any
from urllib.parse import urlencode

import httpx

from app.core.errors import AuthenticationError, ConfigError

_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_SCOPE = "openid email profile"


def build_authorization_url(client_id: str, redirect_uri: str, state: str) -> str:
    """Consent URL the SPA redirects the browser to.

    `access_type=online` + no refresh token: we only need a one-shot identity
    assertion, not long-lived Google API access on the user's behalf.
    """
    if not client_id:
        raise ConfigError("Google sign-in is not configured")
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": _SCOPE,
            "state": state,
            "access_type": "online",
            "prompt": "select_account",
        }
    )
    return f"{_AUTHORIZE_URL}?{query}"


def _decode_id_token_claims(id_token: str) -> dict[str, Any]:
    """Read the id_token payload without verifying its signature.

    This is safe *only* because the id_token was just fetched directly from
    Google's token endpoint over TLS in `exchange_code` — the transport is the
    trust anchor, which is the standard trust model for the authorization-code
    exchange (Google's own docs permit skipping local signature verification when
    the token comes straight from the token endpoint). Do not reuse this on a
    token received from anywhere else.
    """
    parts = id_token.split(".")
    if len(parts) != 3:
        raise AuthenticationError("malformed id_token from Google")
    payload_b64 = parts[1]
    padding = "=" * (-len(payload_b64) % 4)
    try:
        claims: dict[str, Any] = json.loads(base64.urlsafe_b64decode(payload_b64 + padding))
    except (ValueError, binascii.Error, json.JSONDecodeError) as exc:
        raise AuthenticationError("could not read Google identity") from exc
    return claims


async def exchange_code(
    code: str,
    redirect_uri: str,
    client_id: str,
    client_secret: str,
    timeout: float,
) -> dict[str, Any]:
    """Trade an authorization code for the signed-in user's identity.

    Returns a dict with `email`, `name`, and `email_verified`.
    """
    if not client_id or not client_secret:
        raise ConfigError("Google sign-in is not configured")

    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            _TOKEN_URL,
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
    if response.status_code != 200:
        raise AuthenticationError("Google rejected the sign-in attempt")

    try:
        body = response.json()
    except ValueError as exc:
        # A non-JSON body (proxy error page, truncated response) must not 500.
        raise AuthenticationError("could not read Google's sign-in response") from exc
    if not isinstance(body, dict):
        raise AuthenticationError("could not read Google's sign-in response")
    id_token = body.get("id_token")
    if not id_token:
        raise AuthenticationError("Google returned no identity token")

    claims = _decode_id_token_claims(id_token)
    email = claims.get("email")
    if not isinstance(email, str) or not email:
        # Some Google account types (workspace domain-wide, service-ish) omit it.
        raise AuthenticationError("Google identity has no email")
    return {
        "email": email,
        "name": claims.get("name") if isinstance(claims.get("name"), str) else None,
        "email_verified": claims.get("email_verified") is True,
    }
