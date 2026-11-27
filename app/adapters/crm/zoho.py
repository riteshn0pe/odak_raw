"""Zoho CRM adapter: push leads and activity notes into a tenant's Zoho.

Server-based OAuth client (scopes ``ZohoCRM.modules.ALL``,
``ZohoCRM.notifications.ALL``). Two Zoho-specific facts shape this module:

*Data centres.* Zoho is not one host. A tenant's account lives in ``.com``,
``.eu``, ``.in``, ``.com.au``, ``.jp`` or ``.ca``, the OAuth consent screen tells
us which in the ``location`` / ``accounts-server`` parameters it sends back to
the callback, and the token response then hands over the ``api_domain`` for
every subsequent call. Guessing the DC is the classic Zoho integration bug: the
token exchange succeeds against the wrong accounts host for nobody, and against
the right one it returns an ``api_domain`` we must then actually use. So the
DC is discovered, never configured, and the resulting host is stored with the
tokens.

*Picklists.* ``Lead_Status`` and ``Lead_Source`` are picklists the tenant can
edit, and Zoho rejects a value that is not in theirs with ``INVALID_DATA``
rather than coercing it. ``Lead_Source`` is therefore never sent — none of our
sources ("indiamart", "website_form") is in anybody's default picklist, and the
information goes in the description where it cannot fail. ``Lead_Status`` *is*
sent, mapped onto Zoho's stock picklist, and a record rejected for it is
retried once without the field: a tenant who renamed their statuses should get
a synced lead with no status rather than a permanently red row.

One-way: this module can create, update and annotate. It has no method that
reads a record to decide anything, and ``health`` is the one GET — it asks the
Leads module for a single row purely to find out whether the grant still works,
and its answer colours the integration card and nothing else.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode, urlparse

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

PROVIDER = "zoho"

# modules.ALL covers Leads and Notes (Notes is a module). notifications.ALL is
# reserved for the change-notification channel a later stage may want; asking
# for it at connect time avoids a second consent screen then.
SCOPES = "ZohoCRM.modules.ALL,ZohoCRM.notifications.ALL"

API_VERSION = "v8"

# The module we push into. Leads rather than Contacts: a lead the tenant has
# not qualified is a Zoho Lead, and pushing unqualified prospects into Contacts
# is how you pollute somebody's actual customer list.
MODULE = "Leads"

# One timeout for every unauthenticated OAuth call in this module. A parameter
# would be a knob nobody turns and, on an async def, a lint rule everyone
# suppresses; the bound adapter still takes one because a tenant with a huge
# Zoho org genuinely is slower to write to.
HTTP_TIMEOUT = 20.0

DEFAULT_ACCOUNTS_HOST = "https://accounts.zoho.com"

# Every accounts host Zoho operates. The callback names one of these; anything
# not in this set is refused rather than requested, because the accounts host
# is a URL we take from a redirect and then post a client secret to.
ACCOUNTS_HOSTS: frozenset[str] = frozenset(
    {
        "https://accounts.zoho.com",
        "https://accounts.zoho.eu",
        "https://accounts.zoho.in",
        "https://accounts.zoho.com.au",
        "https://accounts.zoho.jp",
        "https://accounts.zohocloud.ca",
        "https://accounts.zoho.sa",
        "https://accounts.zoho.uk",
    }
)

# ``location`` arrives as a bare code; this is the map to the host above.
LOCATIONS: dict[str, str] = {
    "us": "https://accounts.zoho.com",
    "eu": "https://accounts.zoho.eu",
    "in": "https://accounts.zoho.in",
    "au": "https://accounts.zoho.com.au",
    "jp": "https://accounts.zoho.jp",
    "ca": "https://accounts.zohocloud.ca",
    "sa": "https://accounts.zoho.sa",
    "uk": "https://accounts.zoho.uk",
}

# Our lead status onto Zoho's stock Lead_Status picklist. Deliberately lossy:
# Zoho's list has no "won" — a won lead in Zoho is a *converted* one, which is a
# different API call that creates an Account, a Contact and a Deal, and doing
# that on the tenant's behalf from a status change here would be us restructuring
# their CRM without being asked.
STATUS_MAP: dict[str, str] = {
    "new": "Not Contacted",
    "researched": "Not Contacted",
    "queued": "Not Contacted",
    "mailed": "Contacted",
    "replied": "Contacted",
    "positive": "Pre Qualified",
    "meeting": "Pre Qualified",
    "won": "Pre Qualified",
    "lost": "Lost Lead",
    "later": "Contact in Future",
    "suppressed": "Not Qualified",
}

# Zoho's own field API names, in one place. A field map that lives in the
# adapter is the point of the port: the neutral CrmContact never learns the word
# "Designation".
FIELD_MAP: dict[str, str] = {
    "person_last": "Last_Name",
    "person_first": "First_Name",
    "company": "Company",
    "role": "Designation",
    "email": "Email",
    "phone": "Phone",
    "website": "Website",
    "summary": "Description",
    "status": "Lead_Status",
}

# Zoho refuses a Lead with either of these empty. Blank-safe placeholders beat a
# refused push: a lead we found on a marketplace often has a company and no
# person's surname, and dropping it entirely would be worse than one row reading
# "Unknown".
FALLBACK_LAST_NAME = "Unknown"
FALLBACK_COMPANY = "Unknown"


# ----------------------------------------------------------------------- OAuth


def accounts_host_for(location: str, accounts_server: str = "") -> str:
    """The Zoho accounts host for one callback, allow-listed.

    ``accounts_server`` is a URL Zoho puts in the redirect and is preferred when
    it is one we recognise; ``location`` is the fallback. Anything unrecognised
    falls back to the default host rather than being used, because the next
    thing that happens to this value is a POST carrying our client secret.
    """
    candidate = (accounts_server or "").strip().rstrip("/")
    if candidate in ACCOUNTS_HOSTS:
        return candidate
    return LOCATIONS.get((location or "").strip().lower(), DEFAULT_ACCOUNTS_HOST)


def authorize_url(
    client_id: str,
    redirect_uri: str,
    state: str,
    *,
    accounts_host: str = "",
    verifier: str = "",
) -> str:
    """The consent screen URL.

    ``access_type=offline`` plus ``prompt=consent`` is what makes Zoho return a
    refresh token. Without both, a reconnect returns an access token only and
    the integration dies silently an hour later.

    ``verifier`` adds PKCE: only the challenge derived from it goes on this
    URL; the verifier itself is presented at the exchange (``exchange_code``).
    """
    host = accounts_host if accounts_host in ACCOUNTS_HOSTS else DEFAULT_ACCOUNTS_HOST
    form = {
        "scope": SCOPES,
        "client_id": client_id,
        "response_type": "code",
        "access_type": "offline",
        "prompt": "consent",
        "redirect_uri": redirect_uri,
        "state": state,
    }
    if verifier:
        form["code_challenge"] = challenge_for(verifier)
        form["code_challenge_method"] = "S256"
    return f"{host}/oauth/v2/auth?{urlencode(form)}"


async def exchange_code(
    code: str,
    *,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    accounts_host: str,
    code_verifier: str = "",
) -> CrmTokens:
    """Authorization code into a stored grant. Raises ``CrmAuthError``.

    ``code_verifier`` completes the PKCE pair the authorize URL started: a
    code issued against a different challenge (or none) fails here, which is
    what stops somebody completing a connect with a code from their own Zoho
    account and a state they captured.
    """
    host = accounts_host if accounts_host in ACCOUNTS_HOSTS else DEFAULT_ACCOUNTS_HOST
    form = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "code": code,
    }
    if code_verifier:
        form["code_verifier"] = code_verifier
    body = await _token_call(f"{host}/oauth/v2/token", form)
    refresh = str(body.get("refresh_token") or "")
    if not refresh:
        raise CrmAuthError(
            PROVIDER,
            "Zoho returned no refresh token. Disconnect the app in Zoho "
            "(Settings, Apps and Integrations, Connected Apps) and connect again.",
        )
    return CrmTokens(
        access_token=str(body.get("access_token") or ""),
        refresh_token=refresh,
        api_domain=str(body.get("api_domain") or "https://www.zohoapis.com"),
        expires_at=expiry_from(body.get("expires_in")),
        extra={"accounts_host": host},
    )


async def refresh_tokens(
    tokens: CrmTokens,
    *,
    client_id: str,
    client_secret: str,
) -> CrmTokens:
    """A fresh access token from the stored refresh token.

    Zoho does not rotate the refresh token, so the returned grant keeps the one
    we already hold — reading ``refresh_token`` out of the response and storing
    the empty string it contains is how integrations quietly stop working.
    """
    host = str(tokens.extra.get("accounts_host") or DEFAULT_ACCOUNTS_HOST)
    if host not in ACCOUNTS_HOSTS:
        host = DEFAULT_ACCOUNTS_HOST
    body = await _token_call(
        f"{host}/oauth/v2/token",
        {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": tokens.refresh_token,
        },
    )
    access = str(body.get("access_token") or "")
    if not access:
        raise CrmAuthError(
            PROVIDER, "Zoho refused the refresh token. Reconnect Zoho."
        )
    return CrmTokens(
        access_token=access,
        refresh_token=tokens.refresh_token,
        api_domain=str(body.get("api_domain") or tokens.api_domain),
        expires_at=expiry_from(body.get("expires_in")),
        account=tokens.account,
        extra=dict(tokens.extra),
    )


async def _token_call(url: str, form: dict[str, str]) -> dict[str, Any]:
    """One form-encoded OAuth call, with Zoho's in-body errors raised."""
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            response = await client.post(url, data=form)
    except httpx.HTTPError as exc:
        raise ProviderError(PROVIDER, f"Zoho token endpoint unreachable: {exc}") from exc
    try:
        body: dict[str, Any] = response.json()
    except ValueError as exc:
        raise CrmAuthError(PROVIDER, "Zoho returned an unreadable token response") from exc
    # Zoho answers 200 with {"error": "invalid_code"} rather than a 4xx, so the
    # status code alone is not the check.
    if response.status_code >= 400 or body.get("error"):
        detail = body.get("error") or response.status_code
        raise CrmAuthError(PROVIDER, f"Zoho refused the grant: {detail}")
    return body


# ---------------------------------------------------------------- the adapter


class ZohoCrm:
    """``CrmProvider`` bound to one tenant's Zoho grant."""

    def __init__(self, tokens: CrmTokens, *, timeout: float = 20.0) -> None:
        self._tokens = tokens
        base = (tokens.api_domain or "https://www.zohoapis.com").rstrip("/")
        # the api_domain comes out of the stored grant, and the grant row is
        # tenant-writable — so it is checked here, at the only place it turns
        # into a URL the platform will POST lead data to. The token exchange
        # puts a genuine Zoho host in it; this is the backstop against a
        # hand-edited blob pointing the push somewhere else. The leading dot
        # matters: "evil-zohoapis.com" must not clear a ".zohoapis.com" test.
        parsed = urlparse(base)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not host.endswith(
            (
                ".zohoapis.com",
                ".zohoapis.eu",
                ".zohoapis.in",
                ".zohoapis.com.au",
                ".zohoapis.jp",
                ".zohoapis.com.cn",
                ".zohoapis.sa",
                ".zohoapis.uk",
            )
        ):
            raise ProviderError(
                PROVIDER, f"refusing a non-Zoho API host: {host[:100]}"
            )
        self._client = httpx.AsyncClient(
            base_url=f"{base}/crm/{API_VERSION}",
            timeout=timeout,
            headers={
                "Authorization": f"Zoho-oauthtoken {tokens.access_token}",
                "Content-Type": "application/json",
            },
        )

    @property
    def name(self) -> str:
        return PROVIDER

    async def upsert_contact(
        self, contact: CrmContact, *, external_id: str = ""
    ) -> CrmReceipt:
        """Create, adopt or update one Zoho Lead.

        With no ``external_id`` this uses ``/upsert`` with an email duplicate
        check, which makes the *first* push adopt a record the tenant already
        had for this person rather than creating a second one. With an
        ``external_id`` it is a plain update by id and no search happens at all.
        """
        fields = self._fields(contact)
        payload: dict[str, Any]
        if external_id:
            payload = {"data": [{**fields, "id": external_id}]}
            body = await self._call("PUT", f"/{MODULE}", payload, retryable=fields)
        else:
            payload = {
                "data": [fields],
                "duplicate_check_fields": ["Email"] if contact.email else [],
            }
            body = await self._call("POST", f"/{MODULE}/upsert", payload, retryable=fields)
        record = _first_record(body)
        new_id = str((record.get("details") or {}).get("id") or external_id)
        if not new_id:
            raise ProviderError(PROVIDER, "Zoho accepted the lead but returned no id")
        return CrmReceipt(
            external_id=new_id,
            created=str(record.get("action") or "") == "insert",
            detail={"action": record.get("action"), "message": record.get("message")},
        )

    async def log_activity(self, activity: CrmActivity) -> None:
        """Attach one note to a Lead.

        Zoho notes carry no caller-set timestamp — ``Created_Time`` is when the
        API call happened — so a backfilled note that does not say when the
        thing actually happened is misleading inside the tenant's own CRM. The
        occurrence time is therefore folded into the body here rather than in
        the service, because HubSpot *does* take a timestamp and the service
        should not have to know which CRM it is talking to.
        """
        if not activity.external_id:
            raise ProviderError(PROVIDER, "cannot attach a note without a record id")
        stamp = (
            activity.occurred_at.strftime("%Y-%m-%d %H:%M UTC")
            if activity.occurred_at
            else ""
        )
        body = f"{stamp}\n\n{activity.body}".strip() if stamp else activity.body
        note = {
            "Parent_Id": {
                "module": {"api_name": MODULE},
                "id": activity.external_id,
            },
            "Note_Title": (activity.title or "Outreach")[:120],
            "Note_Content": (body or "")[:32000],
        }
        await self._call("POST", "/Notes", {"data": [note]})

    async def health(self) -> dict[str, Any]:
        """One cheap authenticated read, purely to prove the grant still works."""
        try:
            response = await self._client.get(
                f"/{MODULE}", params={"per_page": 1, "fields": "Last_Name"}
            )
        except httpx.HTTPError as exc:
            raise ProviderError(PROVIDER, f"Zoho unreachable: {exc}") from exc
        _raise_for_auth(response)
        # 204 means the module is empty, which is a perfectly healthy account
        if response.status_code not in (200, 204):
            raise ProviderError(PROVIDER, f"Zoho health check failed ({response.status_code})")
        return {
            "provider": PROVIDER,
            "api_domain": self._tokens.api_domain,
            "module": MODULE,
        }

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------- internals

    def _fields(self, contact: CrmContact) -> dict[str, Any]:
        """One ``CrmContact`` into Zoho Lead field names.

        Zoho splits a person into first and last name and requires the last;
        we hold one string, so the last word becomes the surname and everything
        before it the forename. Wrong for some names, which is why the whole
        original string also goes into the description — the mapping is a
        convenience for Zoho's own UI, not the record of what we know.
        """
        first, last = _split_name(contact.person)
        summary = "\n".join(
            part
            for part in (
                contact.summary.strip(),
                f"Source: {contact.source}" if contact.source else "",
                f"Pipeline status: {contact.status}" if contact.status else "",
                f"Name as captured: {contact.person}" if contact.person else "",
            )
            if part
        )
        fields: dict[str, Any] = {
            FIELD_MAP["person_last"]: (last or FALLBACK_LAST_NAME)[:100],
            FIELD_MAP["company"]: (contact.company or FALLBACK_COMPANY)[:200],
        }
        if first:
            fields[FIELD_MAP["person_first"]] = first[:100]
        if contact.role:
            fields[FIELD_MAP["role"]] = contact.role[:100]
        if contact.email:
            fields[FIELD_MAP["email"]] = contact.email[:100]
        if contact.phone:
            fields[FIELD_MAP["phone"]] = contact.phone[:50]
        if contact.website:
            fields[FIELD_MAP["website"]] = contact.website[:255]
        if summary:
            fields[FIELD_MAP["summary"]] = summary[:32000]
        mapped = STATUS_MAP.get(contact.status)
        if mapped:
            fields[FIELD_MAP["status"]] = mapped
        return fields

    async def _call(
        self,
        method: str,
        path: str,
        payload: dict[str, Any],
        *,
        retryable: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """One write, with the picklist retry.

        A tenant who renamed their ``Lead_Status`` picklist gets ``INVALID_DATA``
        naming that field. Rather than leaving the lead permanently unsyncable,
        the record is sent once more with the status dropped — the contact is
        the payload that matters, the status is decoration.
        """
        body = await self._request(method, path, payload)
        record = _first_record(body)
        if str(record.get("status") or "") != "error":
            return body
        api_name = str((record.get("details") or {}).get("api_name") or "")
        code = str(record.get("code") or "")
        if (
            retryable is not None
            and code == "INVALID_DATA"
            and api_name == FIELD_MAP["status"]
            and FIELD_MAP["status"] in retryable
        ):
            logger.info("growth_crm_picklist_retry", provider=PROVIDER, field=api_name)
            sent = payload.get("data")
            first = dict(sent[0]) if isinstance(sent, list) and sent else {}
            first.pop(FIELD_MAP["status"], None)
            body = await self._request(method, path, {**payload, "data": [first]})
            record = _first_record(body)
            if str(record.get("status") or "") != "error":
                return body
        message = str(record.get("message") or "")
        # a deleted-in-their-CRM record: the id is dead weight and retrying it
        # at backoff forever helps nobody. Zoho's codes for this are matched
        # best-effort (INVALID_ID for a vanished id, RECORD_IN_ABNORMAL_STATE
        # for one in the recycle bin) because they are not documented as one
        # stable list — a miss simply keeps today's row-error behaviour.
        if code in ("INVALID_ID", "RECORD_IN_ABNORMAL_STATE") or (
            "recycle bin" in message.lower()
        ):
            raise CrmRecordGoneError(
                PROVIDER, "the Zoho record is gone; the next push recreates it"
            )
        raise ProviderError(
            PROVIDER,
            f"Zoho rejected the record: {record.get('message') or record.get('code') or 'unknown'}",
        )

    async def _request(
        self, method: str, path: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            response = await self._client.request(method, path, json=payload)
        except httpx.HTTPError as exc:
            raise ProviderError(PROVIDER, f"Zoho unreachable: {exc}") from exc
        _raise_for_auth(response)
        if response.status_code == 429:
            raise ProviderError(PROVIDER, "Zoho rate limit reached; the push will retry")
        if response.status_code >= 500:
            raise ProviderError(PROVIDER, f"Zoho is failing ({response.status_code})")
        try:
            body: dict[str, Any] = response.json()
        except ValueError as exc:
            raise ProviderError(PROVIDER, "Zoho returned an unreadable response") from exc
        return body


def _raise_for_auth(response: httpx.Response) -> None:
    """401/403 are connection problems, not record problems."""
    if response.status_code == 401:
        raise CrmAuthError(PROVIDER, "Zoho rejected the token. Reconnect Zoho.")
    if response.status_code == 403:
        raise CrmAuthError(
            PROVIDER,
            "Zoho refused the call for a missing scope. Reconnect Zoho to grant "
            "access to leads and notes.",
        )


def _first_record(body: dict[str, Any]) -> dict[str, Any]:
    data = body.get("data")
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return dict(data[0])
    # a top-level Zoho error (INVALID_TOKEN, OAUTH_SCOPE_MISMATCH) has no data
    if body.get("code"):
        return {"status": "error", "code": body.get("code"), "message": body.get("message")}
    return {}


def _split_name(person: str) -> tuple[str, str]:
    """"Priya Nair" into ("Priya", "Nair"). One word is a surname."""
    parts = [p for p in (person or "").split() if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return "", parts[0]
    return " ".join(parts[:-1]), parts[-1]


def build_zoho(tokens: CrmTokens, *, timeout: float = 20.0) -> ZohoCrm | None:
    """The bound adapter, or None when the grant is unusable."""
    if not tokens.access_token:
        return None
    return ZohoCrm(tokens, timeout=timeout)
