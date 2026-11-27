"""Shared vendor plumbing for `book_meeting` and `check_availability`.

Prebuilt code with a `provider` setting rather than two prefilled HTTP templates,
for the three reasons §2.6 gives: the agent templates reference the bare id
`book_meeting` and it has to stay stable, the two vendors need different request
shapes *and* a two-call sequence, and visitor-local ↔ vendor-expected timezone
normalisation is not expressible in a generic HTTP body template.

**Where the configuration lives.** §2.6 asks for a config surface — provider,
base URL, paths, event-type id, duration, timezone — but `ActionDefinition`
forbids extra fields and a `system` action may not carry an `http` block, so
there is nowhere on the definition to put it in this checkout. It is therefore
carried in the `tenant_secrets` row the definition already references through
`auth.secret_ref`: the plaintext is a JSON object holding the bearer token *and*
the non-secret settings beside it. A plaintext that is not JSON is read as a bare
token with provider defaults, so a hand-created secret still works. The
credential stays in the one store built for it and nothing is written to a plain
column.

Raw httpx, no vendor SDK. Every failure becomes a `CalendarError` carrying an
`ActionOutcomeCode`, so both handlers report the same vocabulary the HTTP
executor does and the tenant can diagnose from the Actions card.
"""

from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from app.adapters.db.actions_repo import ActionSecretResolver
from app.core.logging import get_logger
from app.domain.actions import ActionContext, ActionDefinition, ActionOutcomeCode
from app.services.actions.handlers.base import settings_for

logger = get_logger(__name__)

PROVIDERS = ("calcom", "calendly", "custom")
MAX_SLOTS = 10
DEFAULT_TIMEOUT = 10.0
DEFAULT_DURATION_MINUTES = 30
# Vendor version headers are passed through; §5.7's denylist governs what a
# tenant may set, and these two are ours, not the tenant's.
CALCOM_SLOTS_VERSION = "2024-09-04"
CALCOM_BOOKINGS_VERSION = "2024-08-13"

DEFAULTS: dict[str, dict[str, str]] = {
    "calcom": {
        "base_url": "https://api.cal.com/v2",
        "availability_path": "/slots",
        "booking_path": "/bookings",
    },
    "calendly": {
        "base_url": "https://api.calendly.com",
        "availability_path": "/event_type_available_times",
        "booking_path": "",
    },
    "custom": {
        "base_url": "",
        "availability_path": "/availability",
        "booking_path": "/bookings",
    },
}


class CalendarError(Exception):
    """A vendor call that failed, already mapped to an outcome code."""

    def __init__(self, code: str, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


@dataclass(slots=True)
class Slot:
    """One normalised opening. `label` is the only field a model should quote."""

    start: str
    end: str | None
    label: str
    scheduling_url: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"start": self.start, "label": self.label}
        if self.end:
            payload["end"] = self.end
        if self.scheduling_url:
            payload["scheduling_url"] = self.scheduling_url
        return payload


@dataclass(slots=True)
class CalendarConfig:
    """The vendor settings, decrypted from `tenant_secrets` at call time."""

    provider: str = "calcom"
    token: str = ""
    base_url: str = ""
    availability_path: str = ""
    booking_path: str = ""
    event_type: str = ""
    scheduling_url: str = ""
    duration_minutes: int = DEFAULT_DURATION_MINUTES
    timezone: str = "UTC"
    extra_headers: dict[str, str] = field(default_factory=dict)

    @property
    def zone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            logger.warning("calendar_timezone_unknown", timezone=self.timezone)
            return ZoneInfo("UTC")

    def url(self, path: str) -> str:
        return f"{self.base_url.rstrip('/')}/{path.lstrip('/')}"

    def headers(self, *, api_version: str = "") -> dict[str, str]:
        headers = {"Accept": "application/json", **self.extra_headers}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if api_version and self.provider == "calcom":
            headers["cal-api-version"] = api_version
        return headers


def _destination_ok(url: str) -> bool:
    """HTTPS, a real host, and never a private address.

    The HTTP executor's SSRF guard covers tenant-authored `server` actions; this
    destination comes from a tenant secret, which is the same trust level, so it
    gets the same refusal rather than being trusted for being a credential.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host:
        return False
    if host in ("localhost", "localhost.localdomain") or host.endswith(".local"):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
    )


async def load_config(
    service: Any, action: ActionDefinition, ctx: ActionContext
) -> CalendarConfig | None:
    """Decrypt and normalise the calendar settings, or None when unconfigured."""
    settings = settings_for(service)
    secret_ref = action.auth.secret_ref
    if settings is None or not secret_ref:
        return None
    plaintext = await ActionSecretResolver(settings).resolve(ctx.tenant_id, secret_ref)
    if not plaintext:
        return None

    raw: dict[str, Any]
    try:
        parsed = json.loads(plaintext)
        raw = parsed if isinstance(parsed, dict) else {"token": str(parsed)}
    except ValueError:
        raw = {"token": plaintext.strip()}

    provider = str(raw.get("provider") or "calcom").strip().lower()
    if provider not in PROVIDERS:
        logger.warning("calendar_provider_unknown", provider=provider)
        return None
    defaults = DEFAULTS[provider]
    headers = {
        str(name): str(value)
        for name, value in (raw.get("headers") or {}).items()
        if str(name).lower() not in ("authorization", "host", "content-length")
    }
    config = CalendarConfig(
        provider=provider,
        token=str(raw.get("token") or raw.get("api_key") or ""),
        base_url=str(raw.get("base_url") or defaults["base_url"]),
        availability_path=str(
            raw.get("availability_path") or defaults["availability_path"]
        ),
        booking_path=str(raw.get("booking_path") or defaults["booking_path"]),
        event_type=str(
            raw.get("event_type") or raw.get("event_type_id") or raw.get("service") or ""
        ),
        scheduling_url=str(raw.get("scheduling_url") or ""),
        duration_minutes=int(raw.get("duration_minutes") or DEFAULT_DURATION_MINUTES),
        timezone=str(raw.get("timezone") or "UTC"),
        extra_headers=headers,
    )
    if not config.base_url or not _destination_ok(config.base_url):
        logger.warning("calendar_base_url_rejected", provider=provider)
        return None
    return config


# ----------------------------------------------------------------- the wire


def _timeout(service: Any) -> float:
    settings = settings_for(service)
    raw = getattr(settings, "ACTIONS_TIMEOUT_SECONDS", DEFAULT_TIMEOUT)
    try:
        return max(1.0, min(float(raw), 30.0))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT


async def _request(
    service: Any,
    config: CalendarConfig,
    method: str,
    url: str,
    *,
    params: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
    api_version: str = "",
) -> dict[str, Any]:
    """One vendor call. Every failure leaves as a `CalendarError`."""
    if not _destination_ok(url):
        raise CalendarError(
            ActionOutcomeCode.BLOCKED_SSRF, "the calendar destination is not allowed"
        )
    try:
        async with httpx.AsyncClient(timeout=_timeout(service)) as client:
            response = await client.request(
                method,
                url,
                params=params,
                json=json_body,
                headers=config.headers(api_version=api_version),
            )
    except httpx.TimeoutException as exc:
        raise CalendarError(
            ActionOutcomeCode.TIMEOUT, "the calendar did not respond in time"
        ) from exc
    except httpx.HTTPError as exc:
        logger.warning("calendar_transport_failed", error=str(exc))
        raise CalendarError(
            ActionOutcomeCode.TRANSPORT_ERROR, "the calendar could not be reached"
        ) from exc
    if response.status_code >= 400:
        logger.warning(
            "calendar_http_error",
            provider=config.provider,
            status=response.status_code,
        )
        raise CalendarError(
            ActionOutcomeCode.HTTP_ERROR,
            "the calendar refused the request",
            http_status=response.status_code,
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise CalendarError(
            ActionOutcomeCode.BAD_JSON, "the calendar returned something unreadable"
        ) from exc
    if isinstance(payload, dict):
        return dict(payload)
    if isinstance(payload, list):
        return {"data": payload}
    raise CalendarError(
        ActionOutcomeCode.BAD_JSON, "the calendar returned something unreadable"
    )


# ----------------------------------------------------------- normalisation


def parse_moment(value: str) -> datetime | None:
    """ISO 8601 with an offset, including the `Z` form. Naive input is read as UTC."""
    text = value.strip().replace(" ", "T")
    if text.endswith(("z", "Z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment


def _label(moment: datetime, config: CalendarConfig) -> str:
    """The human form, in the tenant's timezone — never the vendor's.

    A visitor told "14:00" for a slot the vendor returned in UTC is the timezone
    bug that costs a no-show, so the conversion happens once, here.
    """
    local = moment.astimezone(config.zone)
    return f"{local:%a %d %b %Y, %H:%M} ({config.timezone})"


def _raw_slots(payload: Any) -> list[Any]:
    """Every shape the three providers return, flattened to one list.

    cal.com answers `{"data": {"2026-08-20": [{"start": ...}]}}` on 2024-09-04 and
    `{"data": {"slots": {...}}}` on the older version; Calendly answers
    `{"collection": [...]}`; a custom endpoint answers a bare list or
    `{"slots": [...]}`. Tolerating all of them costs a dozen lines and removes a
    whole class of "worked in the test, empty in production".
    """
    if isinstance(payload, list):
        return list(payload)
    if not isinstance(payload, dict):
        return []
    for key in ("collection", "slots", "available_times", "availability"):
        candidate = payload.get(key)
        if isinstance(candidate, list):
            return list(candidate)
        if isinstance(candidate, dict):
            return [item for values in candidate.values() for item in _raw_slots(values)]
    data = payload.get("data")
    if isinstance(data, (list, dict)):
        return _raw_slots(data)
    return []


def normalise_slots(config: CalendarConfig, payload: Any) -> list[Slot]:
    """Up to `MAX_SLOTS` openings, deduplicated and sorted, in tenant time."""
    slots: dict[str, Slot] = {}
    for item in _raw_slots(payload):
        if isinstance(item, str):
            raw_start, raw_end, link, status = item, "", "", "available"
        elif isinstance(item, dict):
            raw_start = str(
                item.get("start") or item.get("start_time") or item.get("time") or ""
            )
            raw_end = str(item.get("end") or item.get("end_time") or "")
            link = str(item.get("scheduling_url") or item.get("booking_url") or "")
            status = str(item.get("status") or "available").lower()
        else:
            continue
        if status not in ("available", "open", "free", ""):
            continue
        moment = parse_moment(raw_start) if raw_start else None
        if moment is None:
            continue
        finish = parse_moment(raw_end) if raw_end else None
        if finish is None:
            finish = moment + timedelta(minutes=config.duration_minutes)
        key = moment.isoformat()
        if key not in slots:
            slots[key] = Slot(
                start=key,
                end=finish.isoformat(),
                label=_label(moment, config),
                scheduling_url=link or None,
            )
        if len(slots) >= MAX_SLOTS * 4:
            break
    ordered = sorted(slots.values(), key=lambda slot: slot.start)
    return ordered[:MAX_SLOTS]


def _window(config: CalendarConfig, from_date: str, to_date: str) -> tuple[str, str]:
    """The requested range, clamped. Calendly refuses a window over seven days."""
    try:
        start = date.fromisoformat(from_date[:10])
    except ValueError as exc:
        raise CalendarError(
            ActionOutcomeCode.INVALID_ARGUMENTS, "from_date must be a date like 2026-03-14"
        ) from exc
    try:
        end = date.fromisoformat(to_date[:10]) if to_date else start + timedelta(days=7)
    except ValueError as exc:
        raise CalendarError(
            ActionOutcomeCode.INVALID_ARGUMENTS, "to_date must be a date like 2026-03-14"
        ) from exc
    if end < start:
        end = start
    if (end - start).days > 7:
        end = start + timedelta(days=7)
    zone = config.zone
    opening = datetime.combine(start, datetime.min.time(), tzinfo=zone)
    closing = datetime.combine(end, datetime.max.time().replace(microsecond=0), tzinfo=zone)
    return opening.astimezone(UTC).isoformat(), closing.astimezone(UTC).isoformat()


async def fetch_slots(
    service: Any,
    config: CalendarConfig,
    *,
    service_name: str | None,
    from_date: str,
    to_date: str,
) -> list[Slot]:
    """Real openings from the vendor. Never invents, never pads."""
    start_iso, end_iso = _window(config, from_date, to_date)
    event_type = (service_name or config.event_type).strip()
    if not event_type and config.provider != "custom":
        raise CalendarError(
            ActionOutcomeCode.NOT_CONFIGURED, "no calendar event type is configured"
        )
    url = config.url(config.availability_path)
    if config.provider == "calcom":
        params = {
            "start": start_iso,
            "end": end_iso,
            "eventTypeId": event_type,
            "timeZone": config.timezone,
        }
        api_version = CALCOM_SLOTS_VERSION
    elif config.provider == "calendly":
        params = {
            "event_type": event_type,
            "start_time": start_iso,
            "end_time": end_iso,
        }
        api_version = ""
    else:
        params = {
            "from": start_iso,
            "to": end_iso,
            "service": event_type,
            "duration": str(config.duration_minutes),
        }
        api_version = ""
    payload = await _request(
        service, config, "GET", url, params=params, api_version=api_version
    )
    return normalise_slots(config, payload)


def _reference(payload: Any) -> str:
    """The vendor's booking id, wherever this one keeps it."""
    if isinstance(payload, dict):
        for key in ("uid", "reference", "booking_id", "id"):
            value = payload.get(key)
            if value:
                return str(value)
        for key in ("data", "booking"):
            nested = payload.get(key)
            if isinstance(nested, (dict, list)):
                found = _reference(nested)
                if found:
                    return found
    if isinstance(payload, list) and payload:
        return _reference(payload[0])
    return ""


async def create_booking(
    service: Any,
    config: CalendarConfig,
    *,
    slot_start: str,
    name: str,
    email: str,
    phone: str | None,
    notes: str | None,
    service_name: str | None,
) -> dict[str, Any]:
    """Book, or — under Calendly — hand back a scheduling link.

    Calendly does not offer creating a booking on an invitee's behalf on the tiers
    a small business is on, so §2.6 makes that provider a read-plus-link flow. The
    link is the slot's own `scheduling_url` when the availability call returned
    one, and the configured scheduling page otherwise.
    """
    moment = parse_moment(slot_start)
    if moment is None:
        raise CalendarError(
            ActionOutcomeCode.INVALID_ARGUMENTS,
            "slot_start must be an ISO 8601 time with an offset, like "
            "2026-03-14T15:00:00+05:30",
        )
    event_type = (service_name or config.event_type).strip()

    if config.provider == "calendly":
        day = moment.astimezone(config.zone).date().isoformat()
        link = config.scheduling_url
        try:
            for slot in await fetch_slots(
                service,
                config,
                service_name=service_name,
                from_date=day,
                to_date=day,
            ):
                if slot.start == moment.isoformat() and slot.scheduling_url:
                    link = slot.scheduling_url
                    break
        except CalendarError:
            # A failed re-read must not lose the configured page; the visitor can
            # still pick their own time on it.
            logger.info("calendar_link_lookup_failed", provider=config.provider)
        if not link or not _destination_ok(link):
            raise CalendarError(
                ActionOutcomeCode.NOT_CONFIGURED,
                "no scheduling page is configured for this calendar",
            )
        return {
            "mode": "link",
            "scheduling_url": link,
            "start": moment.isoformat(),
            "label": _label(moment, config),
        }

    if not config.booking_path:
        raise CalendarError(
            ActionOutcomeCode.NOT_CONFIGURED, "no booking endpoint is configured"
        )
    if config.provider == "calcom":
        try:
            event_type_id: int | str = int(event_type)
        except ValueError as exc:
            raise CalendarError(
                ActionOutcomeCode.NOT_CONFIGURED,
                "the calendar event type id is not a number",
            ) from exc
        attendee: dict[str, Any] = {
            "name": name,
            "email": email,
            "timeZone": config.timezone,
            "language": "en",
        }
        if phone:
            attendee["phoneNumber"] = phone
        body: dict[str, Any] = {
            "start": moment.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "eventTypeId": event_type_id,
            "attendee": attendee,
            "metadata": {"source": "assistant"},
        }
        if notes:
            body["metadata"]["notes"] = notes[:400]
        api_version = CALCOM_BOOKINGS_VERSION
    else:
        body = {
            "start": moment.isoformat(),
            "duration_minutes": config.duration_minutes,
            "service": event_type,
            "name": name,
            "email": email,
            "phone": phone,
            "notes": notes,
            "timezone": config.timezone,
        }
        api_version = ""
    payload = await _request(
        service,
        config,
        "POST",
        config.url(config.booking_path),
        json_body=body,
        api_version=api_version,
    )
    reference = _reference(payload)
    if not reference:
        # A 2xx with no identifier is not a booking anyone can cancel or find.
        raise CalendarError(
            ActionOutcomeCode.BAD_JSON,
            "the calendar accepted the request but returned no booking reference",
        )
    return {
        "mode": "booked",
        "reference": reference,
        "start": moment.isoformat(),
        "label": _label(moment, config),
    }

