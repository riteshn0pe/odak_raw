"""HubSpot CRM adapter: push leads and activity notes into a tenant's HubSpot.

Public OAuth app. Three HubSpot-specific facts shape this module:

*Deduplication without reading.* HubSpot enforces email uniqueness on contacts
itself and answers a duplicate create with ``409 CONFLICT`` whose message names
the record that already exists: ``"Contact already exists. Existing ID: 551"``.
That is how the first push adopts a contact the tenant already had instead of
either creating a second one or searching for it. Searching would make this a
read dependency, and a search that gets rate limited quietly creates the
duplicate anyway — the 409 cannot.

*Lifecycle stage is not ours to set.* ``lifecyclestage`` drives the tenant's
own workflows and HubSpot refuses to move it backwards without an explicit
override. Pushing our pipeline status onto it would silently re-fire somebody's
marketing automation. ``hs_lead_status`` is the stock property that exists for
exactly this, with stock internal values, and nothing keys off it by default —
so that is where our status goes.

*Properties can be missing.* A portal can have a property archived or renamed,
and HubSpot answers with ``400 VALIDATION_ERROR`` naming it. Rather than
leaving that lead permanently unsyncable, the named property is dropped and the
record sent once more: a contact with a missing job title beats a red row.

One-way: create, update, annotate. No method reads a record to decide anything.
The ``crm.objects.contacts.read`` scope in ``SCOPES`` is not ours to want — the
Notes API refuses to run without it — and nothing here calls a read endpoint.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlencode

import httpx

from app.adapters.crm.tokens import (
    CrmAuthError,
    CrmRecordGoneError,
    CrmTokens,
    challenge_for,
    expiry_from,
)
from app.core.errors import ProviderError
from app.core.logging import get_logger
from app.domain.ports import CrmActivity, CrmContact, CrmReceipt

logger = get_logger(__name__)

PROVIDER = "hubspot"

# See the note in zoho.py: one constant rather than a per-call parameter.
HTTP_TIMEOUT = 20.0

# HubSpot has one global host, unlike Zoho.
API_HOST = "https://api.hubapi.com"
AUTH_HOST = "https://app.hubspot.com"

# Pinned rather than tracking HubSpot's date-versioned paths (/crm/objects/
# 2026-03/...). Same reasoning as the stores adapters' API_VERSION: a version
# that moves on its own moves in production without a deploy.
API_VERSION = "v3"
OAUTH_VERSION = "v1"

# Space-separated, HubSpot's format. contacts.read is required by the Notes API
# (HubSpot's rule, not our need) — see the module docstring.
SCOPES = "oauth crm.objects.contacts.read crm.objects.contacts.write"

CONTACTS_PATH = f"/crm/{API_VERSION}/objects/contacts"
NOTES_PATH = f"/crm/{API_VERSION}/objects/notes"

# HUBSPOT_DEFINED association: note -> contact.
NOTE_TO_CONTACT_TYPE_ID = 202

# Our lead status onto HubSpot's stock hs_lead_status internal values.
# Deliberately lossy in the same place Zoho is: a won lead in HubSpot is a Deal,
# and creating deals in somebody's pipeline off a status change here would be us
# restructuring their CRM uninvited.
STATUS_MAP: dict[str, str] = {
    "new": "NEW",
    "researched": "NEW",
    "queued": "NEW",
    "mailed": "ATTEMPTED_TO_CONTACT",
    "replied": "CONNECTED",
    "positive": "OPEN_DEAL",
    "meeting": "OPEN_DEAL",
    "won": "OPEN_DEAL",
    "lost": "UNQUALIFIED",
    "later": "BAD_TIMING",
    "suppressed": "UNQUALIFIED",
}

# HubSpot's own property names, in one place — the neutral CrmContact never
# learns the word "jobtitle".
FIELD_MAP: dict[str, str] = {
    "person_first": "firstname",
    "person_last": "lastname",
    "company": "company",
    "role": "jobtitle",
    "email": "email",
    "phone": "phone",
    "website": "website",
    "status": "hs_lead_status",
}

# "Contact already exists. Existing ID: 551"
_EXISTING_ID = re.compile(r"Existing ID:\s*(\d+)")


# ----------------------------------------------------------------------- OAuth


def authorize_url(
    client_id: str, redirect_uri: str, state: str, *, verifier: str = ""
) -> str:
    """The consent screen URL, with PKCE when a verifier was minted."""
    form: dict[str, str] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "state": state,
    }
    if verifier:
        form["code_challenge"] = challenge_for(verifier)
        form["code_challenge_method"] = "S256"
    return f"{AUTH_HOST}/oauth/authorize?{urlencode(form)}"


async def exchange_code(
    code: str,
    *,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    code_verifier: str = "",
) -> CrmTokens:
    """Authorization code into a stored grant. Raises ``CrmAuthError``."""
    form = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "code": code,
    }
    if code_verifier:
        # the other half of PKCE: a challenge sent on the authorize URL is
        # only proof if the exchange answers it with the verifier that made it
        form["code_verifier"] = code_verifier
    body = await _token_call(form)
    refresh = str(body.get("refresh_token") or "")
    if not refresh:
        raise CrmAuthError(PROVIDER, "HubSpot returned no refresh token. Reconnect HubSpot.")
    return CrmTokens(
        access_token=str(body.get("access_token") or ""),
        refresh_token=refresh,
        api_domain=API_HOST,
        expires_at=expiry_from(body.get("expires_in")),
    )


async def refresh_tokens(
    tokens: CrmTokens,
    *,
    client_id: str,
    client_secret: str,
) -> CrmTokens:
    """A fresh access token from the stored refresh token.

    HubSpot's refresh token does not rotate and the response omits it, so the
    stored one is carried forward rather than read back out of the response.
    """
    body = await _token_call(
        {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": tokens.refresh_token,
        },
    )
    access = str(body.get("access_token") or "")
    if not access:
        raise CrmAuthError(PROVIDER, "HubSpot refused the refresh token. Reconnect HubSpot.")
    return CrmTokens(
        access_token=access,
        refresh_token=str(body.get("refresh_token") or tokens.refresh_token),
        api_domain=API_HOST,
        expires_at=expiry_from(body.get("expires_in")),
        account=tokens.account,
        extra=dict(tokens.extra),
    )


async def account_info(access_token: str) -> dict[str, Any]:
    """Portal id, user and granted scopes for one access token.

    Called once at connect time to label the integration card with the portal
    the tenant actually authorised — "HubSpot" alone is not enough for anyone
    who has more than one. The token travels in the path because that is
    HubSpot's endpoint design; it needs no scope and it is the only call in this
    module shaped that way.

    That shape is a standing trap: never ``raise_for_status()`` here and never
    log ``exc.request.url`` from this call — either would put the access token
    into a log line (and, via the failure handlers, onto the dashboard).
    """
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            response = await client.get(
                f"{API_HOST}/oauth/{OAUTH_VERSION}/access-tokens/{access_token}"
            )
    except httpx.HTTPError as exc:
        raise ProviderError(PROVIDER, f"HubSpot unreachable: {exc}") from exc
    if response.status_code >= 400:
        raise CrmAuthError(PROVIDER, "HubSpot rejected the token")
    try:
        body: dict[str, Any] = response.json()
    except ValueError as exc:
        raise ProviderError(PROVIDER, "HubSpot returned an unreadable token info response") from exc
    return body


async def _token_call(form: dict[str, str]) -> dict[str, Any]:
    """One form-encoded OAuth call."""
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            response = await client.post(
                f"{API_HOST}/oauth/{OAUTH_VERSION}/token",
                data=form,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
    except httpx.HTTPError as exc:
        raise ProviderError(PROVIDER, f"HubSpot token endpoint unreachable: {exc}") from exc
    try:
        body: dict[str, Any] = response.json()
    except ValueError as exc:
        raise CrmAuthError(PROVIDER, "HubSpot returned an unreadable token response") from exc
    if response.status_code >= 400:
        raise CrmAuthError(
            PROVIDER, f"HubSpot refused the grant: {body.get('message') or response.status_code}"
        )
    return body


# ---------------------------------------------------------------- the adapter


class HubspotCrm:
    """``CrmProvider`` bound to one tenant's HubSpot grant."""

    def __init__(self, tokens: CrmTokens, *, timeout: float = 20.0) -> None:
        self._tokens = tokens
        self._client = httpx.AsyncClient(
            base_url=API_HOST,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {tokens.access_token}",
                "Content-Type": "application/json",
            },
        )

    @property
    def name(self) -> str:
        return PROVIDER

    async def upsert_contact(
        self, contact: CrmContact, *, external_id: str = ""
    ) -> CrmReceipt:
        """Create, adopt or update one HubSpot contact.

        With a known ``external_id`` this is a plain PATCH. Without one it is a
        POST, and a 409 naming an existing record is treated as success: the id
        HubSpot hands back in the conflict is adopted and patched, so the second
        push onward is an update and no duplicate is ever created.
        """
        properties = self._properties(contact)
        if external_id:
            body = await self._write(
                "PATCH", f"{CONTACTS_PATH}/{external_id}", {"properties": properties}
            )
            return CrmReceipt(
                external_id=str(body.get("id") or external_id),
                created=False,
                detail={"action": "update"},
            )

        created, body, adopted = await self._create(properties)
        if created:
            return CrmReceipt(
                external_id=str(body.get("id") or ""), created=True, detail={"action": "create"}
            )
        # HubSpot told us which record already holds this email; take it over
        # and bring it up to date in the same push.
        patched = await self._write(
            "PATCH", f"{CONTACTS_PATH}/{adopted}", {"properties": properties}
        )
        return CrmReceipt(
            external_id=str(patched.get("id") or adopted),
            created=False,
            detail={"action": "adopted"},
        )

    async def log_activity(self, activity: CrmActivity) -> None:
        """Attach one note to a contact.

        ``hs_timestamp`` is required and is the note's own displayed time, so a
        backfilled note lands in the tenant's timeline where the thing actually
        happened rather than where the sync happened.
        """
        if not activity.external_id:
            raise ProviderError(PROVIDER, "cannot attach a note without a record id")
        properties: dict[str, Any] = {
            "hs_note_body": _note_body(activity)[:65000],
        }
        if activity.occurred_at is not None:
            properties["hs_timestamp"] = int(activity.occurred_at.timestamp() * 1000)
        payload = {
            "properties": properties,
            "associations": [
                {
                    "to": {"id": activity.external_id},
                    "types": [
                        {
                            "associationCategory": "HUBSPOT_DEFINED",
                            "associationTypeId": NOTE_TO_CONTACT_TYPE_ID,
                        }
                    ],
                }
            ],
        }
        await self._write("POST", NOTES_PATH, payload)

    async def health(self) -> dict[str, Any]:
        """Whether the grant still works, without reading anybody's records."""
        body = await account_info(self._tokens.access_token)
        return {
            "provider": PROVIDER,
            "hub_id": body.get("hub_id"),
            "user": body.get("user"),
            "scopes": body.get("scopes"),
        }

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------- internals

    def _properties(self, contact: CrmContact) -> dict[str, Any]:
        """One ``CrmContact`` into HubSpot property names.

        HubSpot splits a person into first and last name, so the last word
        becomes the surname. Wrong for some names, which is why the summary the
        push writes as the first note carries the name exactly as captured —
        the split is a convenience for HubSpot's UI, not our record of truth.

        No ``lifecyclestage``: see the module docstring.
        """
        first, last = _split_name(contact.person)
        properties: dict[str, Any] = {}
        if contact.email:
            properties[FIELD_MAP["email"]] = contact.email[:100]
        if first:
            properties[FIELD_MAP["person_first"]] = first[:100]
        if last:
            properties[FIELD_MAP["person_last"]] = last[:100]
        if contact.company:
            properties[FIELD_MAP["company"]] = contact.company[:200]
        if contact.role:
            properties[FIELD_MAP["role"]] = contact.role[:100]
        if contact.phone:
            properties[FIELD_MAP["phone"]] = contact.phone[:50]
        if contact.website:
            properties[FIELD_MAP["website"]] = contact.website[:255]
        mapped = STATUS_MAP.get(contact.status)
        if mapped:
            properties[FIELD_MAP["status"]] = mapped
        return properties

    async def _create(
        self, properties: dict[str, Any]
    ) -> tuple[bool, dict[str, Any], str]:
        """POST a contact. Returns ``(created, body, adopted_id)``.

        A 409 is not an error here — it is HubSpot answering the deduplication
        question we deliberately never asked it.
        """
        response = await self._request("POST", CONTACTS_PATH, {"properties": properties})
        if response.status_code == 409:
            existing = _existing_id(_json(response))
            if existing:
                logger.info("growth_crm_contact_adopted", provider=PROVIDER, external_id=existing)
                return False, {}, existing
        return True, _checked(response), ""

    async def _write(
        self, method: str, path: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return _checked(await self._request(method, path, payload))

    async def _request(
        self, method: str, path: str, payload: dict[str, Any]
    ) -> httpx.Response:
        """One call, with the one retry that is worth making.

        A property HubSpot does not recognise is dropped and the record resent
        once. Anything else is handed back for the caller to judge.
        """
        response = await self._send(method, path, payload)
        if response.status_code != 400:
            return response
        rejected = _rejected_property(_json(response))
        properties = payload.get("properties")
        if not rejected or not isinstance(properties, dict) or rejected not in properties:
            return response
        logger.info("growth_crm_property_dropped", provider=PROVIDER, property=rejected)
        trimmed = {k: v for k, v in properties.items() if k != rejected}
        return await self._send(method, path, {**payload, "properties": trimmed})

    async def _send(
        self, method: str, path: str, payload: dict[str, Any]
    ) -> httpx.Response:
        try:
            return await self._client.request(method, path, json=payload)
        except httpx.HTTPError as exc:
            raise ProviderError(PROVIDER, f"HubSpot unreachable: {exc}") from exc


def _checked(response: httpx.Response) -> dict[str, Any]:
    """Raise on failure, otherwise hand back the parsed body."""
    _raise_for_auth(response)
    if response.status_code == 429:
        raise ProviderError(PROVIDER, "HubSpot rate limit reached; the push will retry")
    if response.status_code >= 500:
        raise ProviderError(PROVIDER, f"HubSpot is failing ({response.status_code})")
    # the contact we hold an id for was deleted in the portal. Unlike a 404 on
    # some random path this one is actionable: the id is dead weight, and the
    # push loop's answer (drop it, recreate, adopt by email) is a full recovery
    if response.status_code == 404:
        raise CrmRecordGoneError(PROVIDER, "the HubSpot record is gone")
    body = _json(response)
    if response.status_code >= 400:
        raise ProviderError(
            PROVIDER,
            f"HubSpot rejected the record: {body.get('message') or response.status_code}",
        )
    return body


def _raise_for_auth(response: httpx.Response) -> None:
    """401/403 are connection problems, not record problems."""
    if response.status_code == 401:
        raise CrmAuthError(PROVIDER, "HubSpot rejected the token. Reconnect HubSpot.")
    if response.status_code == 403:
        raise CrmAuthError(
            PROVIDER,
            "HubSpot refused the call for a missing scope. Reconnect HubSpot to "
            "grant access to contacts and notes.",
        )


def _json(response: httpx.Response) -> dict[str, Any]:
    """A body, or an empty dict. HubSpot answers 204 with no body on some paths."""
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _existing_id(body: dict[str, Any]) -> str:
    """The record id out of a 409 CONFLICT message."""
    match = _EXISTING_ID.search(str(body.get("message") or ""))
    return match.group(1) if match else ""


def _rejected_property(body: dict[str, Any]) -> str:
    """The property name out of a 400 validation error, if it names one."""
    for error in body.get("errors") or []:
        if not isinstance(error, dict):
            continue
        name = (error.get("context") or {}).get("propertyName")
        if isinstance(name, list) and name:
            return str(name[0])
        if isinstance(name, str) and name:
            return name
    context = body.get("context")
    if isinstance(context, dict):
        name = context.get("propertyName")
        if isinstance(name, list) and name:
            return str(name[0])
        if isinstance(name, str):
            return name
    # last resort: HubSpot's prose form, "Property \"jobtitle\" does not exist"
    match = re.search(r'Property "([^"]+)"', str(body.get("message") or ""))
    return match.group(1) if match else ""


def _note_body(activity: CrmActivity) -> str:
    title = (activity.title or "").strip()
    body = (activity.body or "").strip()
    return f"{title}\n\n{body}".strip() if title else body


def _split_name(person: str) -> tuple[str, str]:
    """"Priya Nair" into ("Priya", "Nair"). One word is a surname."""
    parts = [p for p in (person or "").split() if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return "", parts[0]
    return " ".join(parts[:-1]), parts[-1]


def build_hubspot(tokens: CrmTokens, *, timeout: float = 20.0) -> HubspotCrm | None:
    """The bound adapter, or None when the grant is unusable."""
    if not tokens.access_token:
        return None
    return HubspotCrm(tokens, timeout=timeout)
