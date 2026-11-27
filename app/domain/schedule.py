"""The scheduler brain: when may this tenant's next outbound message go out?

Pure and deterministic, with no I/O and no clock of its own — ``now`` is always
passed in. The cron dispatcher's whole job is to ask this module one question
("is now allowed, and if not, when?") and either enqueue a batch or do nothing.
There are no sleep loops anywhere in the system; a tick that is too early simply
returns and the next tick re-asks.

Determinism is the point. Send timing is the difference between a warmed domain
and a blocked one, and a rule you cannot unit-test is a rule you find out about
from a deliverability report three weeks later.

Randomness is therefore *not* generated in here. ``next_send_time`` returns the
earliest allowed instant; the caller spreads a batch across the window with
``spread_gaps``, which takes an explicit ``Random`` so a test can pin the seed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from random import Random
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Indian business hours, Monday to Friday, in the market the platform launches
# into. Every one of these is overridable per tenant in growth_settings.send_caps
# — they are the defaults, not the policy.
DEFAULT_TIMEZONE = "Asia/Kolkata"
DEFAULT_START_HOUR = 9
DEFAULT_END_HOUR = 18
DEFAULT_WORKDAYS: tuple[int, ...] = (0, 1, 2, 3, 4)  # Monday..Friday, as weekday()

# Warmup ramp. A brand-new sending domain that opens at 200 mails a day is a
# domain that gets filtered before it has a reputation to defend, so volume
# climbs by 10 a day from 10 to 200 over three working weeks and then holds.
# These are the numbers every deliverability guide agrees on within a factor of
# two; they are constants rather than settings because a tenant asking to warm
# up faster is a tenant asking to be blocked.
WARMUP_START = 10
WARMUP_STEP = 10
WARMUP_DAYS = 20
WARMUP_MAX = WARMUP_START + WARMUP_STEP * (WARMUP_DAYS - 1)

# Bounds on the gap between two consecutive sends in one batch. Identical gaps
# are a machine signature; the point of the jitter is that the pattern carries
# no information.
MIN_GAP_SECONDS = 45
MAX_GAP_SECONDS = 600


@dataclass(slots=True, frozen=True)
class WorkingHours:
    """The window a tenant is willing to have mail leave in, in their own zone."""

    start_hour: int = DEFAULT_START_HOUR
    end_hour: int = DEFAULT_END_HOUR
    days: tuple[int, ...] = DEFAULT_WORKDAYS
    timezone: str = DEFAULT_TIMEZONE

    @classmethod
    def from_settings(cls, raw: object) -> WorkingHours:
        """Build from the ``growth_settings.send_caps`` JSONB blob.

        Every field is defended, because this blob is round-tripped through an
        HTTP PUT and a bad value must degrade to the default rather than take
        the dispatcher down for that tenant.
        """
        data = raw if isinstance(raw, dict) else {}
        start = _bounded_int(data.get("start_hour"), DEFAULT_START_HOUR, 0, 23)
        end = _bounded_int(data.get("end_hour"), DEFAULT_END_HOUR, 1, 24)
        if end <= start:
            start, end = DEFAULT_START_HOUR, DEFAULT_END_HOUR
        raw_days = data.get("days")
        days: tuple[int, ...] = DEFAULT_WORKDAYS
        if isinstance(raw_days, (list, tuple)):
            parsed = tuple(
                sorted({int(d) for d in raw_days if isinstance(d, int) and 0 <= d <= 6})
            )
            if parsed:
                days = parsed
        zone = data.get("timezone")
        return cls(
            start_hour=start,
            end_hour=end,
            days=days,
            timezone=zone if isinstance(zone, str) and zone else DEFAULT_TIMEZONE,
        )

    def zone(self) -> ZoneInfo:
        """The tenant's zone, falling back to the default on an unknown name.

        A tenant who typed a timezone that no longer exists in the tz database
        should still get mail sent during plausible hours, not none at all.
        """
        try:
            return ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            return ZoneInfo(DEFAULT_TIMEZONE)


@dataclass(slots=True, frozen=True)
class SendCaps:
    """How much this tenant may still send today."""

    daily_cap: int = 50
    warmup_day: int = 0
    sent_today: int = 0
    hours: WorkingHours = field(default_factory=WorkingHours)

    @property
    def effective_cap(self) -> int:
        """The lower of what the tenant asked for and what warmup allows.

        ``warmup_day`` of 0 means the tenant is not warming up — an established
        domain moved onto the platform, or a channel with no reputation to build
        — and only their own cap applies.
        """
        if self.warmup_day <= 0:
            return max(0, self.daily_cap)
        return max(0, min(self.daily_cap, warmup_cap(self.warmup_day)))

    @property
    def remaining(self) -> int:
        return max(0, self.effective_cap - max(0, self.sent_today))


def warmup_cap(day: int) -> int:
    """Messages allowed on day ``day`` of a sending domain's warmup.

    Day 1 is 10 and each further day adds 10, holding at 200 from day 20. Days
    at or below zero return 0: day 0 is "not warming up", which is a different
    question, answered by ``SendCaps.effective_cap``.
    """
    if day <= 0:
        return 0
    if day >= WARMUP_DAYS:
        return WARMUP_MAX
    return WARMUP_START + WARMUP_STEP * (day - 1)


def is_within_hours(moment: datetime, hours: WorkingHours) -> bool:
    """Is this instant inside the tenant's working window?

    ``moment`` may be in any zone, or naive; it is converted into the tenant's
    zone before the comparison, because "9am" means 9am where the customer is,
    not where the server happens to run.
    """
    local = _in_zone(moment, hours)
    if local.weekday() not in hours.days:
        return False
    return hours.start_hour <= local.hour < hours.end_hour


def next_send_time(
    now: datetime,
    caps: SendCaps,
    horizon_days: int = 14,
) -> datetime | None:
    """The earliest instant this tenant may send at, or None.

    Returns ``now`` itself when sending is allowed right now — the dispatcher
    reads that as "enqueue the batch". A later datetime means "not yet, and the
    tick that lands after this time will be the one that sends".

    ``None`` means *never under this configuration*: the daily cap is zero, or
    the working-hours window contains no days. Both are owner mistakes, and the
    caller surfaces them rather than retrying silently forever.

    The daily cap is spent, not deferred: once ``sent_today`` reaches the
    effective cap, the answer is the start of the next working day, because the
    counter resets with the tenant's local date and not with the server's.
    """
    hours = caps.hours
    if caps.effective_cap <= 0 or not hours.days:
        return None

    if caps.remaining > 0 and is_within_hours(now, hours):
        return now

    local = _in_zone(now, hours)
    # when the cap is spent, today is over regardless of the clock
    search_from = local if caps.remaining > 0 else _next_day_start(local, hours)
    opening = _next_opening(search_from, hours, horizon_days)
    if opening is None:
        return None
    return opening.astimezone(now.tzinfo) if now.tzinfo else opening


def spread_gaps(count: int, rng: Random) -> list[int]:
    """Seconds to wait before each send in a batch of ``count``.

    The first element is always 0 — the batch starts immediately — and each
    later one is an independent draw inside ``[MIN_GAP_SECONDS,
    MAX_GAP_SECONDS]``. Independent rather than a fixed cadence with noise
    added, because a jittered cadence is still a cadence and the autocorrelation
    is exactly what a filter looks for.

    The ``Random`` is passed in so a test can seed it and the same batch is
    reproducible; nothing here reads the global RNG.
    """
    if count <= 0:
        return []
    return [0] + [rng.randint(MIN_GAP_SECONDS, MAX_GAP_SECONDS) for _ in range(count - 1)]


def _in_zone(moment: datetime, hours: WorkingHours) -> datetime:
    """Move an instant into the tenant's zone, treating naive input as UTC.

    A naive datetime reaching here is a caller bug, but reading it as local
    server time would make the answer depend on where the process runs, which is
    a far worse failure than a documented assumption.
    """
    zone = hours.zone()
    if moment.tzinfo is None:

        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(zone)


def _next_day_start(local: datetime, hours: WorkingHours) -> datetime:
    return datetime.combine(
        local.date() + timedelta(days=1), time(0, 0), tzinfo=local.tzinfo
    )


def _next_opening(
    local: datetime, hours: WorkingHours, horizon_days: int
) -> datetime | None:
    """First moment on or after ``local`` that falls inside the window.

    Bounded by ``horizon_days`` so a configuration with an empty day set cannot
    spin — though ``next_send_time`` rejects that case before we get here, this
    is the second lock on the same door.
    """
    day = local
    for _ in range(horizon_days + 1):
        if day.weekday() in hours.days:
            opening = day.replace(
                hour=hours.start_hour, minute=0, second=0, microsecond=0
            )
            if opening >= local:
                return opening
            if hours.start_hour <= day.hour < hours.end_hour:
                return day
        day = datetime.combine(
            day.date() + timedelta(days=1), time(0, 0), tzinfo=day.tzinfo
        )
    return None


def _bounded_int(value: object, default: int, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value if low <= value <= high else default
