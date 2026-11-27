"""IndiaMART — enquiries a tenant's seller account already received.

IndiaMART has no push API a tenant can point at this platform: the only
documented integration surface is a pull endpoint, ``GET /seller/v1/leads``,
authenticated by the seller's own key, that returns the enquiries received in
a time window. So this adapter is polled — by the hourly cron task and by the
tenant's own backfill button — rather than subscribed, and the service layer
owns the schedule (``app/services/growth/indiamart.py``).

The response shape this adapter reads is the documented one: ``CODE`` /
``MESSAGE`` / ``RECORDS``, one record per enquiry, with the sender's name,
company, email and mobile, the product and the message body. Field names are
the vendor's uppercase spellings kept verbatim inside ``_record_to_lead`` and
nowhere else — the rest of the platform speaks the ``InboundLead`` port, so a
field IndiaMART renames is a change in this one function.

Every transport failure raises ``ProviderUnavailableError`` — an empty window
and a failed pull must not look alike, because the caller records the second
on the integration card and the first means only that nobody enquired. The
vendor's own error envelope (bad key, quota exceeded) is still an empty list,
logged here: retrying it cannot help and the next poll costs nothing.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from app.core.breaker import guard
from app.core.errors import CircuitOpenError, ProviderUnavailableError
from app.core.logging import get_logger
from app.domain.ports import InboundLead

logger = get_logger(__name__)

INDIAMART_BASE_URL = "https://api.indiamart.com/seller/v1"
PROVIDER = "indiamart"

# The API is windowed on epoch seconds, and the docs say results for very wide
# windows are truncated server-side. The service slices big backfills into
# windows of this size; a poll uses one.
MAX_WINDOW_DAYS = 15

_MAX_RECORD_CHARS = 4000


class IndiaMartSource:
    """The ``InboundLeadSource`` port, over IndiaMART's seller leads API."""

    def __init__(self, api_key: str, timeout: float = 15.0) -> None:
        self._api_key = api_key
        self._client = httpx.AsyncClient(timeout=timeout)

    @property
    def name(self) -> str:
        return PROVIDER

    async def fetch_window(self, start: datetime, end: datetime) -> list[InboundLead]:
        """Enquiries whose QUERY_DATE falls in ``[start, end)``.

        ``end`` is exclusive on our side because the caller tiles windows back
        to back; IndiaMART's own boundary behaviour is unspecified, so the
        caller's tiling plus the ``UNIQUE_QUERY_ID`` dedupe key is what keeps a
        boundary enquiry from becoming two leads, not the vendor's semantics.

        Raises ``ProviderUnavailableError`` on a transport failure (timeout,
        network, breaker open) rather than returning empty: an empty window is
        a normal result — no enquiries — and a pull the caller cannot tell
        apart from an outage is a silent failure. The vendor's own error
        envelope (bad key, quota) is still an empty list, logged here, because
        retrying it will not help and the next poll costs nothing.
        """
        payload = await self._get(
            "/leads",
            {
                "glusr-crt-key": self._api_key,
                "start-time": str(int(start.timestamp())),
                "end-time": str(int(end.timestamp())),
            },
        )
        if payload is None:
            raise ProviderUnavailableError(PROVIDER, "the leads pull failed")
        if str(payload.get("CODE") or "") not in ("200", "OK", ""):
            # the vendor's own error envelope ("invalid key", "quota exceeded")
            # is a normal outcome of a poll, logged here and returned as empty
            logger.warning(
                "indiamart_error_envelope",
                code=str(payload.get("CODE")),
                message=str(payload.get("MESSAGE"))[:200],
            )
            return []

        records = payload.get("RECORDS")
        if not isinstance(records, list):
            return []
        leads: list[InboundLead] = []
        for record in records:
            if isinstance(record, dict):
                lead = _record_to_lead(record)
                if lead is not None:
                    leads.append(lead)
        return leads

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any] | None:
        """One guarded call. None on every failure, having logged it."""
        try:
            with guard(PROVIDER):
                try:
                    response = await self._client.get(
                        f"{INDIAMART_BASE_URL}{path}", params=params
                    )
                    if response.status_code != 200:
                        raise ProviderUnavailableError(
                            PROVIDER, f"http {response.status_code}"
                        )
                    body = response.json()
                    return dict(body) if isinstance(body, dict) else None
                except (httpx.HTTPError, ValueError) as exc:
                    raise ProviderUnavailableError(PROVIDER, str(exc)) from exc
        except CircuitOpenError:
            logger.info("indiamart_circuit_open", path=path)
            return None
        except ProviderUnavailableError as exc:
            logger.warning("indiamart_call_failed", path=path, error=str(exc))
            return None

    async def close(self) -> None:
        await self._client.aclose()


def _record_to_lead(record: dict[str, Any]) -> InboundLead | None:
    """One RECORDS row to an ``InboundLead``, or None when it is not an enquiry.

    None only for a row without the vendor's own enquiry id: a record with no
    id cannot be deduped, and ingesting it twice under two pulls is worse than
    dropping it. A row with no contact at all still passes through — the
    shared ``ingest`` validation records it as a parse failure on the raw row,
    visible, rather than the adapter guessing whether a mobile number the
    vendor masked is "usable".
    """
    external_id = str(
        record.get("UNIQUE_QUERY_ID") or record.get("QUERY_ID") or ""
    ).strip()
    if not external_id:
        return None
    return InboundLead(
        external_id=external_id,
        name=_clean(record.get("SENDER_NAME")),
        company=_clean(record.get("SENDER_COMPANY")),
        email=_clean(record.get("SENDER_EMAIL")).lower(),
        phone=_clean(record.get("SENDER_MOBILE") or record.get("GLUSR_MOB")),
        location=_clean(record.get("ENQ_ADDRESS") or record.get("SENDER_CITY")),
        message=_clean(record.get("QUERY_MESSG")),
        product=_clean(record.get("QUERY_PRODUCT")),
        raw={
            key: _clean(record.get(key))[:_MAX_RECORD_CHARS]
            for key in ("QUERY_DATE", "QUERY_PRODUCT", "SENDER_COMPANY")
            if _clean(record.get(key))
        },
    )


def _clean(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def build_indiamart(api_key: str, timeout: float = 15.0) -> IndiaMartSource | None:
    """IndiaMART, or None when no key is configured."""
    if not api_key:
        return None
    return IndiaMartSource(api_key, timeout)
