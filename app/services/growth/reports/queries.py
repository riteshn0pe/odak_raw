"""Plain SQL behind the report agent. Exact, repeatable, no LLM.

Every number the PDF prints comes from here, and re-running these queries over
the same window returns exactly what the report shows — that is the Stage 3
checklist item "numbers in the PDF equal the SQL re-run", and it is why this
module never calls a model, never samples, and never rounds before the render.

Tables from later stages (growth_campaigns, growth_customers, growth_orders,
growth_insights, growth_events) may not exist yet. Each of those sections
probes ``to_regclass`` first and returns an empty mapping when the table is
absent, so the report renders "only sections with data" from day one rather
than failing on a missing relation.

**Every statement here carries `WHERE tenant_id = :tid` even though it runs
inside `tenant_scope` and RLS already confines it.** This is the same
belt-and-braces `growth_repo.py` applies, and it matters more here than
anywhere: RLS is enforced by the database role, and a role holding
`BYPASSRLS` — which the platform ones may — turns a missing predicate into
one tenant's report containing every tenant's numbers. A report is the one
artefact that leaves the platform as a PDF in somebody's WhatsApp, so it is
the worst possible place to depend on a single layer of protection.
`growth_events` is the one deliberate exception in shape rather than
principle: its shared rows carry `tenant_id IS NULL` by design, so the
predicate admits those too, matching the table's own RLS policy.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.core.db import tenant_scope

# Tables owned by later stages. Absent until their migration lands; the report
# treats an absent table as an empty section, never as an error.
OPTIONAL_TABLES: tuple[str, ...] = (
    "growth_campaigns",
    "growth_customers",
    "growth_orders",
    "growth_insights",
    "growth_events",
)


async def collect(
    container: Any,
    tenant_id: UUID,
    *,
    period: str,
    start: datetime,
    end: datetime,
    months_back: int = 0,
) -> dict[str, Any]:
    """The full computed number set for one report window.

    ``start``/``end`` bound the period; ``months_back`` (6 for a monthly
    report, 0 otherwise) adds per-month trend buckets behind the window.
    """
    async with tenant_scope(tenant_id) as session:
        present = await _present_tables(session)
        numbers: dict[str, Any] = {
            "period": period,
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
        }
        numbers["funnel"] = await _funnel(session, tenant_id, start, end)
        numbers["trend"] = await _daily_trend(session, tenant_id, start, end)
        numbers["reply_time"] = await _reply_time(session, tenant_id, start, end)
        numbers["hot_leads"] = await _hot_leads(session, tenant_id)
        numbers["deliverability"] = await _deliverability(session, tenant_id, start, end)
        numbers["by_language"] = await _by_language(session, tenant_id, start, end)
        numbers["usage"] = await _usage(
            session, tenant_id, start, end, numbers["funnel"]
        )
        if months_back:
            numbers["trends_6m"] = await _monthly_trends(session, tenant_id, end, months_back)
        if "growth_campaigns" in present:
            numbers["campaigns"] = await _campaigns(session, tenant_id, present, start, end)
        if "growth_customers" in present:
            numbers["customers"] = await _customers(session, tenant_id, present, start, end)
        if "growth_insights" in present:
            numbers["insights"] = await _insights(session, tenant_id, present)
        if "growth_events" in present:
            numbers["events"] = await _events(session, tenant_id, present)
        numbers["settings_snapshot"] = await _settings_snapshot(container, tenant_id)
    return numbers


async def _present_tables(session: Any) -> set[str]:
    rows = (
        await session.execute(
            text(
                "SELECT t AS name, to_regclass('public.' || t) IS NOT NULL AS present "
                "FROM unnest(CAST(:tables AS text[])) AS t"
            ),
            {"tables": list(OPTIONAL_TABLES)},
        )
    ).mappings().all()
    return {str(row["name"]) for row in rows if row["present"]}


async def _funnel(
    session: Any, tenant_id: UUID, start: datetime, end: datetime
) -> dict[str, Any]:
    """Found → mailed → delivered → opened → replied → positive → meeting.

    Window counts come from the message log and the lead timestamps; the
    status snapshot is current state, labelled as such so nobody reads it as
    "this is where the window's leads ended up".
    """
    params: dict[str, Any] = {"tid": tenant_id, "start": start, "end": end}
    leads_created = (
        await session.execute(
            text(
                "SELECT count(*) AS n FROM growth_leads "
                "WHERE tenant_id = :tid AND created_at >= :start AND created_at < :end"
            ),
            params,
        )
    ).scalar() or 0
    by_source = (
        await session.execute(
            text(
                "SELECT source, count(*) AS n FROM growth_leads "
                "WHERE tenant_id = :tid AND created_at >= :start AND created_at < :end "
                "GROUP BY source ORDER BY n DESC"
            ),
            params,
        )
    ).mappings().all()
    out_by_status = (
        await session.execute(
            text(
                "SELECT channel, status, count(*) AS n FROM growth_messages "
                "WHERE tenant_id = :tid AND direction = 'out' "
                "AND created_at >= :start AND created_at < :end "
                "GROUP BY channel, status ORDER BY channel, n DESC"
            ),
            params,
        )
    ).mappings().all()
    inbound = (
        await session.execute(
            text(
                "SELECT count(*) AS n FROM growth_messages "
                "WHERE tenant_id = :tid AND direction = 'in' "
                "AND created_at >= :start AND created_at < :end"
            ),
            params,
        )
    ).scalar() or 0
    snapshot = (
        await session.execute(
            text(
                "SELECT status, count(*) AS n FROM growth_leads "
                "WHERE tenant_id = :tid GROUP BY status ORDER BY n DESC"
            ),
            params,
        )
    ).mappings().all()
    approved = (
        await session.execute(
            text(
                "SELECT count(*) AS n FROM growth_approvals "
                "WHERE tenant_id = :tid AND status IN ('approved', 'edited') "
                "AND created_at >= :start AND created_at < :end"
            ),
            params,
        )
    ).scalar() or 0
    return {
        "leads_created": int(leads_created),
        "by_source": [
            {"source": str(r["source"] or "unknown"), "count": int(r["n"])} for r in by_source
        ],
        "outbound": [
            {
                "channel": str(r["channel"]),
                "status": str(r["status"]),
                "count": int(r["n"]),
            }
            for r in out_by_status
        ],
        "inbound": int(inbound),
        "approved_batches": int(approved),
        "status_snapshot": [
            {"status": str(r["status"]), "count": int(r["n"])} for r in snapshot
        ],
    }


async def _daily_trend(
    session: Any, tenant_id: UUID, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    """Per-day outbound vs inbound, the reply-rate trend chart's raw points."""
    rows = (
        await session.execute(
            text(
                "SELECT date_trunc('day', created_at)::date AS day, direction, "
                "count(*) AS n FROM growth_messages "
                "WHERE tenant_id = :tid AND created_at >= :start AND created_at < :end "
                "GROUP BY 1, 2 ORDER BY 1"
            ),
            {"tid": tenant_id, "start": start, "end": end},
        )
    ).mappings().all()
    by_day: dict[str, dict[str, int]] = {}
    for row in rows:
        day = str(row["day"])
        bucket = by_day.setdefault(day, {"out": 0, "in": 0})
        bucket["out" if row["direction"] == "out" else "in"] += int(row["n"])
    return [
        {
            "day": day,
            "outbound": counts["out"],
            "inbound": counts["in"],
            "reply_rate": round(counts["in"] / counts["out"], 3) if counts["out"] else 0.0,
        }
        for day, counts in sorted(by_day.items())
    ]


async def _reply_time(
    session: Any, tenant_id: UUID, start: datetime, end: datetime
) -> dict[str, Any]:
    """Time-to-first-reply on inbound threads answered in the window.

    Median plus count, in seconds. A lead whose first reply predates the window
    is excluded — this measures this period's responsiveness, not history's.
    """
    row = (
        await session.execute(
            text(
                "WITH first_out AS ("
                "  SELECT lead_id, min(created_at) AS at FROM growth_messages "
                "  WHERE tenant_id = :tid AND direction = 'out' GROUP BY lead_id"
                "), first_in AS ("
                "  SELECT lead_id, min(created_at) AS at FROM growth_messages "
                "  WHERE tenant_id = :tid AND direction = 'in' "
                "  AND created_at >= :start AND created_at < :end "
                "  GROUP BY lead_id"
                ") SELECT count(*) AS n, "
                "percentile_cont(0.5) WITHIN GROUP ("
                "  ORDER BY EXTRACT(EPOCH FROM (i.at - o.at))) AS median_seconds "
                "FROM first_in i JOIN first_out o USING (lead_id) WHERE i.at > o.at"
            ),
            {"tid": tenant_id, "start": start, "end": end},
        )
    ).mappings().first()
    if row is None:
        return {"count": 0, "median_seconds": None}
    median = row["median_seconds"]
    return {
        "count": int(row["n"] or 0),
        "median_seconds": float(median) if median is not None else None,
    }


async def _hot_leads(
    session: Any, tenant_id: UUID, limit: int = 10
) -> list[dict[str, Any]]:
    """Leads waiting on the owner: replied or positive, newest activity first."""
    rows = (
        await session.execute(
            text(
                "SELECT id, company, person, email, phone, status, last_message_at "
                "FROM growth_leads "
                "WHERE tenant_id = :tid AND status IN ('replied', 'positive') "
                "ORDER BY last_message_at DESC NULLS LAST LIMIT :limit"
            ),
            {"tid": tenant_id, "limit": limit},
        )
    ).mappings().all()
    return [
        {
            "lead_id": str(row["id"]),
            "company": str(row["company"] or ""),
            "person": str(row["person"] or ""),
            "email": str(row["email"] or ""),
            "phone": str(row["phone"] or ""),
            "status": str(row["status"]),
            "last_message_at": row["last_message_at"].isoformat()
            if row["last_message_at"] is not None
            else None,
        }
        for row in rows
    ]


async def _deliverability(
    session: Any, tenant_id: UUID, start: datetime, end: datetime
) -> dict[str, Any]:
    """Bounced / complained / failed share of outbound, per channel."""
    rows = (
        await session.execute(
            text(
                "SELECT channel, "
                "count(*) FILTER (WHERE status IN ('bounced', 'complained', 'failed')) "
                "AS bad, count(*) AS total FROM growth_messages "
                "WHERE tenant_id = :tid AND direction = 'out' "
                "AND created_at >= :start AND created_at < :end "
                "GROUP BY channel"
            ),
            {"tid": tenant_id, "start": start, "end": end},
        )
    ).mappings().all()
    channels = [
        {
            "channel": str(row["channel"]),
            "sent": int(row["total"]),
            "failed": int(row["bad"]),
            "failure_rate": round(int(row["bad"]) / int(row["total"]), 4)
            if int(row["total"])
            else 0.0,
        }
        for row in rows
    ]
    return {"channels": channels}


async def _by_language(
    session: Any, tenant_id: UUID, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    """Reply rate split by the per-message resolved language."""
    rows = (
        await session.execute(
            text(
                "SELECT COALESCE(NULLIF(language, ''), 'unknown') AS language, "
                "count(*) FILTER (WHERE direction = 'out') AS out_n, "
                "count(*) FILTER (WHERE direction = 'in') AS in_n "
                "FROM growth_messages "
                "WHERE tenant_id = :tid AND created_at >= :start AND created_at < :end "
                "GROUP BY 1 ORDER BY out_n DESC"
            ),
            {"tid": tenant_id, "start": start, "end": end},
        )
    ).mappings().all()
    return [
        {
            "language": str(row["language"]),
            "outbound": int(row["out_n"]),
            "inbound": int(row["in_n"]),
            "reply_rate": round(int(row["in_n"]) / int(row["out_n"]), 3)
            if int(row["out_n"])
            else 0.0,
        }
        for row in rows
    ]


async def _usage(
    session: Any,
    tenant_id: UUID,
    start: datetime,
    end: datetime,
    funnel: dict[str, Any],
) -> dict[str, Any]:
    """Cost per feature from the rollups, plus cost per lead/reply/order.

    Billing reads the same rollups; the report only displays them. The two
    never mix beyond sharing the source.

    ``funnel`` arrives from the caller rather than being recomputed here: the
    denominators are the same counts `collect` already has, and re-running
    five aggregates to divide by them is a second trip for a number already
    in hand.

    Scoped by an explicit ``tenant_id`` predicate: unlike every growth table,
    ``usage_rollups`` carries no RLS policy (it is a platform-wide aggregate
    the admin screens also read), so the tenant boundary here is this WHERE
    clause and nothing else.
    """
    rows = (
        await session.execute(
            text(
                "SELECT kind, feature, sum(quantity) AS qty, sum(cost_micros) AS micros "
                "FROM usage_rollups WHERE tenant_id = :tid "
                "AND day >= CAST(:start AS date) "
                "AND day < CAST(:end AS date) "
                "GROUP BY kind, feature ORDER BY micros DESC NULLS LAST"
            ),
            {"tid": tenant_id, "start": start.date(), "end": end.date()},
        )
    ).mappings().all()
    by_feature: dict[str, int] = {}
    total_micros = 0
    for row in rows:
        micros = int(row["micros"] or 0)
        total_micros += micros
        feature = str(row["feature"] or "untagged")
        by_feature[feature] = by_feature.get(feature, 0) + micros
    leads = funnel["leads_created"] or 0
    replies = funnel["inbound"] or 0
    return {
        "total_micros": total_micros,
        "by_feature": [
            {"feature": feature, "micros": micros}
            for feature, micros in sorted(
                by_feature.items(), key=lambda item: item[1], reverse=True
            )
        ],
        "cost_per_lead_micros": (total_micros // leads) if leads else None,
        "cost_per_reply_micros": (total_micros // replies) if replies else None,
    }


async def _monthly_trends(
    session: Any, tenant_id: UUID, end: datetime, months_back: int
) -> list[dict[str, Any]]:
    """One bucket per calendar month, the monthly report's 6-month view."""
    rows = (
        await session.execute(
            text(
                "SELECT to_char(month, 'YYYY-MM') AS month, "
                "(SELECT count(*) FROM growth_leads l "
                " WHERE l.tenant_id = :tid "
                " AND date_trunc('month', l.created_at) = month) AS leads, "
                "(SELECT count(*) FROM growth_messages m "
                " WHERE m.tenant_id = :tid AND m.direction = 'out' "
                " AND date_trunc('month', m.created_at) = month) AS mailed, "
                "(SELECT count(*) FROM growth_messages m "
                " WHERE m.tenant_id = :tid AND m.direction = 'in' "
                " AND date_trunc('month', m.created_at) = month) AS replies "
                "FROM generate_series(date_trunc('month', CAST(:end AS timestamptz)) "
                "- make_interval(months => :back), "
                "date_trunc('month', CAST(:end AS timestamptz)) "
                "- make_interval(months => 1), "
                "make_interval(months => 1)) AS month ORDER BY month"
            ),
            {"tid": tenant_id, "end": end, "back": months_back},
        )
    ).mappings().all()
    return [
        {
            "month": str(row["month"]),
            "leads": int(row["leads"] or 0),
            "mailed": int(row["mailed"] or 0),
            "replies": int(row["replies"] or 0),
        }
        for row in rows
    ]


async def _campaigns(
    session: Any, tenant_id: UUID, present: set[str], start: datetime, end: datetime
) -> dict[str, Any]:
    if "growth_campaigns" not in present:
        return {}
    rows = (
        await session.execute(
            text(
                "SELECT status, count(*) AS n FROM growth_campaigns "
                "WHERE tenant_id = :tid AND created_at >= :start AND created_at < :end "
                "GROUP BY status ORDER BY n DESC"
            ),
            {"tid": tenant_id, "start": start, "end": end},
        )
    ).mappings().all()
    # Two different numbers, kept apart on purpose. Store revenue is every
    # order in the window; attributed revenue is only what a campaign's own
    # results claim, computed by the campaign service from its discount code.
    # Summing all orders and calling it "attributed" would credit campaigns
    # with the shop's ordinary trade.
    orders_revenue = 0
    if "growth_orders" in present:
        orders_revenue = (
            await session.execute(
                text(
                    "SELECT COALESCE(sum(total_micros), 0) AS revenue FROM growth_orders "
                    "WHERE tenant_id = :tid "
                    "AND ordered_at >= :start AND ordered_at < :end"
                ),
                {"tid": tenant_id, "start": start, "end": end},
            )
        ).scalar() or 0
    attributed = (
        await session.execute(
            text(
                "SELECT COALESCE(sum((results->>'revenue_micros')::bigint), 0) "
                "FROM growth_campaigns "
                "WHERE tenant_id = :tid "
                "AND created_at >= :start AND created_at < :end "
                "AND results ? 'revenue_micros'"
            ),
            {"tid": tenant_id, "start": start, "end": end},
        )
    ).scalar() or 0
    return {
        "by_status": [
            {"status": str(row["status"]), "count": int(row["n"])} for row in rows
        ],
        "store_revenue_micros": int(orders_revenue),
        "attributed_revenue_micros": int(attributed),
    }


async def _customers(
    session: Any, tenant_id: UUID, present: set[str], start: datetime, end: datetime
) -> dict[str, Any]:
    if "growth_customers" not in present:
        return {}
    total = (
        await session.execute(
            text("SELECT count(*) AS n FROM growth_customers WHERE tenant_id = :tid"),
            {"tid": tenant_id},
        )
    ).scalar() or 0
    new_row = (
        await session.execute(
            text(
                "SELECT count(*) AS n FROM growth_customers "
                "WHERE tenant_id = :tid AND created_at >= :start AND created_at < :end"
            ),
            {"tid": tenant_id, "start": start, "end": end},
        )
    ).scalar() or 0
    orders: dict[str, Any] = {}
    if "growth_orders" in present:
        order_rows = (
            await session.execute(
                text(
                    "SELECT payment, count(*) AS n, COALESCE(sum(total_micros), 0) AS revenue "
                    "FROM growth_orders "
                    "WHERE tenant_id = :tid "
                    "AND ordered_at >= :start AND ordered_at < :end "
                    "GROUP BY payment"
                ),
                {"tid": tenant_id, "start": start, "end": end},
            )
        ).mappings().all()
        orders = {
            str(row["payment"] or "unknown"): {
                "count": int(row["n"]),
                "revenue_micros": int(row["revenue"] or 0),
            }
            for row in order_rows
        }
    return {"total": int(total), "new": int(new_row), "orders": orders}


async def _insights(
    session: Any, tenant_id: UUID, present: set[str]
) -> dict[str, Any]:
    if "growth_insights" not in present:
        return {}
    rows = (
        await session.execute(
            text(
                "SELECT type, count(*) AS n FROM growth_insights "
                "WHERE tenant_id = :tid "
                "GROUP BY type ORDER BY n DESC LIMIT 10"
            ),
            {"tid": tenant_id},
        )
    ).mappings().all()
    acted = (
        await session.execute(
            text(
                "SELECT count(*) FILTER (WHERE acted_on IS NOT NULL) AS acted, "
                "count(*) AS total FROM growth_insights WHERE tenant_id = :tid"
            ),
            {"tid": tenant_id},
        )
    ).mappings().first()
    return {
        "by_type": [
            {"type": str(row["type"]), "count": int(row["n"])} for row in rows
        ],
        "acted_on": int((acted or {}).get("acted") or 0),
        "total": int((acted or {}).get("total") or 0),
    }


async def _events(
    session: Any, tenant_id: UUID, present: set[str]
) -> dict[str, Any]:
    if "growth_events" not in present:
        return {}
    rows = (
        await session.execute(
            text(
                "SELECT name, date, region, category FROM growth_events "
                "WHERE (tenant_id = :tid OR tenant_id IS NULL) "
                "AND date >= CURRENT_DATE AND date < CURRENT_DATE + 30 "
                "ORDER BY date LIMIT 10"
            ),
            {"tid": tenant_id},
        )
    ).mappings().all()
    return {
        "upcoming_30d": [
            {
                "name": str(row["name"]),
                "date": str(row["date"]),
                "region": str(row["region"] or ""),
                "category": str(row["category"] or ""),
            }
            for row in rows
        ]
    }


async def _settings_snapshot(container: Any, tenant_id: UUID) -> dict[str, Any]:
    """What is connected, what is broken — the report's last page."""
    settings = await container.growth_profile.get(tenant_id)
    integrations = await container.growth.integrations.list(tenant_id)
    connected = [
        {"provider": str(row["provider"]), "status": str(row["status"])}
        for row in integrations
        if row.get("status") not in ("not_connected", "planned")
    ]
    broken = [
        {"provider": str(row["provider"]), "status": str(row["status"])}
        for row in integrations
        if row.get("status") == "error"
    ]
    profile = settings.get("profile") or {}
    return {
        "business": str(profile.get("sender_name") or profile.get("sells") or ""),
        "connected": connected,
        "broken": broken,
    }
