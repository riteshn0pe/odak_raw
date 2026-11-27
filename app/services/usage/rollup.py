"""Usage rollups.

Aggregates the append-only usage_events ledger into daily per-tenant rows the
dashboard and staff cost views read. Nothing reads the raw ledger at request
time — a month of one busy tenant's events is millions of rows, and a dashboard
that scans them per load does not survive contact with a real customer.

Idempotent by cursor: the job records the last event id it consumed and only
aggregates newer ones, and it upserts each (tenant, day, kind, feature) row, so a
re-run or an overlapping run corrects a day rather than doubling it.

The `feature` dimension comes from `meta->>'feature'`, stamped by the growth
metering helper. Events from before the growth platform, and every event that is
not a growth surface, fold into the empty-string bucket — which is why the column
is NOT NULL DEFAULT '' rather than nullable: NULL is distinct from NULL in a
unique index, and a nullable dimension would silently stop the upsert
deduplicating and grow one row per tick for every pre-growth kind.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from app.core.db import admin_scope
from app.core.logging import get_logger

logger = get_logger(__name__)

# how many ledger rows to fold per run. bounds the work of a single cron tick so
# a long backlog is drained across ticks rather than in one unbounded scan
BATCH = 10000


async def run_rollup() -> dict[str, int]:
    """Fold new usage_events into usage_rollups. Safe to call on any cadence.

    Reads the cursor, aggregates the next batch of events grouped by tenant, day
    and kind, upserts them, and advances the cursor — all in one transaction, so
    a crash mid-run leaves the cursor untouched and the batch is simply retried.
    """
    async with admin_scope() as session:
        cursor = (
            await session.execute(text("SELECT last_event_id FROM rollup_state WHERE id = 1"))
        ).scalar_one_or_none() or 0

        rows = (
            await session.execute(
                text(
                    """
                    SELECT id, tenant_id, ts::date AS day, kind, quantity,
                           unit_cost_micros,
                           COALESCE(meta->>'feature', '') AS feature
                      FROM usage_events
                     WHERE id > :cursor
                     ORDER BY id
                     LIMIT :batch
                    """
                ),
                {"cursor": cursor, "batch": BATCH},
            )
        ).mappings().all()

        if not rows:
            await session.execute(
                text("UPDATE rollup_state SET last_run_at = now() WHERE id = 1")
            )
            return {"events": 0, "max_id": int(cursor)}

        # aggregate in memory before writing, so the upsert is one row per
        # (tenant, day, kind, feature) rather than one per event
        agg: dict[tuple[str, Any, str, str], dict[str, int]] = {}
        max_id = int(cursor)
        for row in rows:
            max_id = max(max_id, int(row["id"]))
            key = (str(row["tenant_id"]), row["day"], row["kind"], str(row["feature"]))
            bucket = agg.setdefault(key, {"quantity": 0, "cost": 0})
            bucket["quantity"] += int(row["quantity"])
            bucket["cost"] += int(row["quantity"]) * int(row["unit_cost_micros"])

        await session.execute(
            text(
                """
                INSERT INTO usage_rollups
                    (tenant_id, day, kind, feature, quantity, cost_micros)
                VALUES (:tenant_id, :day, :kind, :feature, :quantity, :cost_micros)
                ON CONFLICT (tenant_id, day, kind, feature) DO UPDATE
                    SET quantity = usage_rollups.quantity + EXCLUDED.quantity,
                        cost_micros = usage_rollups.cost_micros + EXCLUDED.cost_micros,
                        updated_at = now()
                """
            ),
            [
                {
                    "tenant_id": tenant_id,
                    "day": day,
                    "kind": kind,
                    "feature": feature,
                    "quantity": bucket["quantity"],
                    "cost_micros": bucket["cost"],
                }
                for (tenant_id, day, kind, feature), bucket in agg.items()
            ],
        )

        await session.execute(
            text(
                "UPDATE rollup_state SET last_event_id = :max_id, last_run_at = now() "
                "WHERE id = 1"
            ),
            {"max_id": max_id},
        )

    logger.info("rollup_complete", events=len(rows), max_id=max_id)
    return {"events": len(rows), "max_id": max_id}
