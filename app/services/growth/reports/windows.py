"""Report period windows. Pure date math, no I/O — unit-testable.

Weekly reports cover Monday 00:00 → next Monday 00:00 in the tenant's own
timezone (from ``send_caps.timezone``), monthly reports cover the calendar
month, daily reports cover the local date. The cron tick is hourly, so each
``*_due_on`` predicate answers "should this period be enqueued at this tick":
weekly on Monday after 9am local, monthly on the 1st after 9am local, daily
after 9am local.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DUE_HOUR = 9


def tenant_tz(settings: dict[str, Any]) -> ZoneInfo:
    """The tenant's timezone, or UTC when it is missing or unparseable.

    Never raises: a bad timezone string must delay a report, never crash the
    tick that serves fifty other tenants.
    """
    name = str((settings.get("send_caps") or {}).get("timezone") or "UTC")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def tenant_today(settings: dict[str, Any], now: datetime | None = None) -> Any:
    """The tenant's current local date — the run-context day key.

    Pure date math off ``tenant_tz`` so every agent computes the same day
    from the same settings without each owning a timezone read. Returns a
    ``date``, not a datetime: the run context is keyed by the calendar day,
    and an instant would make "which day's rows" depend on the second a job
    happened to run.
    """
    return (now or datetime.now(UTC)).astimezone(tenant_tz(settings)).date()


def week_window(now: datetime, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """The Monday-to-Monday window containing ``now``."""
    local = now.astimezone(tz)
    monday = (local - timedelta(days=local.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return monday, monday + timedelta(days=7)


def month_window(now: datetime, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """The calendar-month window containing ``now``."""
    local = now.astimezone(tz)
    first = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if first.month == 12:
        end = first.replace(year=first.year + 1, month=1)
    else:
        end = first.replace(month=first.month + 1)
    return first, end


def day_window(now: datetime, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """The local-date window containing ``now``."""
    local = now.astimezone(tz)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def periods_due(
    settings: dict[str, Any], now: datetime
) -> list[dict[str, Any]]:
    """Which scheduled periods should be enqueued at this tick.

    Weekly and monthly are on by default; daily only when the owner turned it
    on. A period is due from its 9am local moment for the rest of the period,
    not only on the hour or the weekday it opens: gating weekly on "today is
    Monday" would lose a whole week's report to a single day of downtime,
    which the hourly cron's "a missed tick is safe" promise does not survive.
    Being due repeatedly costs nothing — the caller skips a period whose
    ledger row is already `ready`.
    """
    prefs = settings.get("report_prefs") or {}
    tz = tenant_tz(settings)
    due: list[dict[str, Any]] = []
    if prefs.get("weekly", True):
        start, _ = week_window(now, tz)
        if now >= start + timedelta(hours=DUE_HOUR):
            due.append({"period": "weekly", "start": start.date()})
    if prefs.get("monthly", True):
        start, _ = month_window(now, tz)
        if now >= start + timedelta(hours=DUE_HOUR):
            due.append({"period": "monthly", "start": start.date()})
    if prefs.get("daily", False):
        start, _ = day_window(now, tz)
        if now >= start + timedelta(hours=DUE_HOUR):
            due.append({"period": "daily", "start": start.date()})
    return due


def resolve_window(
    settings: dict[str, Any], period: str, period_start: str
) -> tuple[datetime, datetime]:
    """The query window for one run.

    Scheduled runs name their ``period_start`` date; on-demand covers the last
    7 days ending now. A named start that will not parse falls back to the
    current window for that period — a malformed queue payload must still
    produce a report, not a traceback.
    """
    tz = tenant_tz(settings)
    now = datetime.now(UTC)
    if period == "on_demand":
        end = now
        return end - timedelta(days=7), end
    try:
        day = datetime.strptime(period_start, "%Y-%m-%d").replace(tzinfo=tz)
    except ValueError:
        day = now.astimezone(tz)
    if period == "monthly":
        return month_window(day, tz)
    if period == "daily":
        return day_window(day, tz)
    return week_window(day, tz)
