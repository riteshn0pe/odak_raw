"""Per-tenant analytics for the dashboard.

Reads usage from the rollups and quality from the message record. Two sources on
purpose: usage is high-volume and pre-aggregated, while quality signals
(latency, feedback, confidence) live on the messages a tenant already owns and
are cheap to read directly for a single tenant's window.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.api.deps import ContainerDep, TenantDep
from app.core.constants import DEFAULT_MIN_CONFIDENCE
from app.core.db import tenant_scope

router = APIRouter(prefix="/analytics", tags=["analytics"])


class QualityOut(BaseModel):
    answers: int
    avg_latency_ms: float | None = None
    avg_confidence: float | None = None
    thumbs_up: int = 0
    thumbs_down: int = 0


@router.get("/quality", response_model=QualityOut)
async def quality(
    tenant: TenantDep, container: ContainerDep, days: int = Query(default=30, ge=1, le=365)
) -> QualityOut:
    """Latency, confidence, and the feedback split over a window.

    The numbers a tenant checks to know their assistant is healthy — a rising
    thumbs-down or falling confidence is the early signal of a content gap.
    """
    since = datetime.now(UTC) - timedelta(days=days)
    stats = await container.conversations.message_stats(tenant.tenant_id, since=since)
    return QualityOut(
        answers=int(stats.get("answers") or 0),
        avg_latency_ms=stats.get("avg_latency_ms"),
        avg_confidence=stats.get("avg_confidence"),
        thumbs_up=int(stats.get("thumbs_up") or 0),
        thumbs_down=int(stats.get("thumbs_down") or 0),
    )


@router.get("/usage", response_model=list[dict[str, Any]])
async def usage(
    tenant: TenantDep, days: int = Query(default=30, ge=1, le=365)
) -> list[dict[str, Any]]:
    """Daily usage by kind from the rollups.

    The dashboard charts this; it never touches usage_events, because a busy
    tenant's ledger is too large to scan per page load.

    `usage_rollups` is one of the platform-scoped tables from 004 and carries no
    RLS policy, so opening a `tenant_scope` sets `app.tenant_id` and nothing
    reads it. On this table the explicit `tenant_id` predicate *is* the isolation
    boundary — not a belt-and-braces extra on top of a policy — which is why it
    is stated here and why it must never be dropped from this query.

    The SUM collapses the `feature` dimension added in 016. This endpoint charts
    totals per kind; the per-feature split is a separate, opt-in view, and
    without the aggregate one metered kind would arrive as several rows for the
    same day and the chart would draw each of them as its own point.
    """
    day_to = date.today()
    day_from = day_to - timedelta(days=days)
    async with tenant_scope(tenant.tenant_id) as session:
        from sqlalchemy import text

        rows = (
            await session.execute(
                text(
                    """
                    SELECT day, kind,
                           SUM(quantity)::bigint AS quantity,
                           SUM(cost_micros)::bigint AS cost_micros
                      FROM usage_rollups
                     WHERE tenant_id = :tenant_id
                       AND day BETWEEN :start AND :end
                     GROUP BY day, kind
                     ORDER BY day
                    """
                ),
                {
                    "tenant_id": tenant.tenant_id,
                    "start": day_from,
                    "end": day_to,
                },
            )
        ).mappings().all()
    return [dict(row) for row in rows]


@router.get("/usage/features", response_model=list[dict[str, Any]])
async def usage_by_feature(
    tenant: TenantDep, days: int = Query(default=30, ge=1, le=365)
) -> list[dict[str, Any]]:
    """Per-feature cost breakdown.

    Every growth event stamps `meta.feature`, so the breakdown is a read over
    history that was already collected, not a collection toggle.
    """
    from sqlalchemy import text

    day_to = date.today()
    day_from = day_to - timedelta(days=days)
    async with tenant_scope(tenant.tenant_id) as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT feature,
                           SUM(quantity)::bigint AS quantity,
                           SUM(cost_micros)::bigint AS cost_micros
                      FROM usage_rollups
                     WHERE tenant_id = :tenant_id
                       AND day BETWEEN :start AND :end
                       AND feature <> ''
                     GROUP BY feature
                     ORDER BY cost_micros DESC
                    """
                ),
                {
                    "tenant_id": tenant.tenant_id,
                    "start": day_from,
                    "end": day_to,
                },
            )
        ).mappings().all()
    return [dict(row) for row in rows]


class TokenBurnOut(BaseModel):
    model: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_total: int = 0


class PerformanceOut(BaseModel):
    period_days: int
    answers: int
    latency_p50_ms: float | None = None
    latency_p95_ms: float | None = None
    fallback_answers: int = 0
    fallback_rate: float | None = None
    token_burn_by_model: list[TokenBurnOut] = Field(default_factory=list)


@router.get("/performance", response_model=PerformanceOut)
async def performance(
    tenant: TenantDep,
    days: int = Query(default=30, ge=1, le=365),
    min_confidence: float = Query(default=DEFAULT_MIN_CONFIDENCE, ge=0.0, le=1.0),
) -> PerformanceOut:
    """Latency percentiles, fallback rate, and token burn by model over a window.

    The operational cut of quality: p95 is where a tenant feels slowness the
    average hides, the fallback rate is how often the assistant punted to the
    low-confidence path, and token burn by model is what that traffic costs to
    serve. Latency and fallback read from messages a tenant already owns;
    tokens come from the usage ledger where each answer's model was recorded.
    """
    from sqlalchemy import text

    since = datetime.now(UTC) - timedelta(days=days)
    async with tenant_scope(tenant.tenant_id) as session:
        perf = (
            await session.execute(
                text(
                    """
                    SELECT count(*) FILTER (WHERE role = 'assistant') AS answers,
                           count(*) FILTER (
                               WHERE role = 'assistant' AND confidence IS NOT NULL
                           ) AS answers_scored,
                           count(*) FILTER (
                               WHERE role = 'assistant' AND confidence IS NOT NULL
                                 AND confidence < :threshold
                           ) AS fallback_answers,
                           percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms)
                               FILTER (WHERE role = 'assistant' AND latency_ms IS NOT NULL)
                               AS latency_p50_ms,
                           percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms)
                               FILTER (WHERE role = 'assistant' AND latency_ms IS NOT NULL)
                               AS latency_p95_ms
                      FROM messages
                     WHERE created_at >= :since
                    """
                ),
                {"since": since, "threshold": min_confidence},
            )
        ).mappings().one()
        by_model = (
            await session.execute(
                text(
                    """
                    SELECT meta->>'model' AS model,
                           sum(quantity) FILTER (WHERE kind = 'llm_token_in') AS tokens_in,
                           sum(quantity) FILTER (WHERE kind = 'llm_token_out') AS tokens_out
                      FROM usage_events
                     WHERE ts >= :since
                       AND kind IN ('llm_token_in', 'llm_token_out')
                     GROUP BY meta->>'model'
                     ORDER BY sum(quantity) DESC
                    """
                ),
                {"since": since},
            )
        ).mappings().all()

    scored = int(perf.get("answers_scored") or 0)
    fallback_answers = int(perf.get("fallback_answers") or 0)
    burn = []
    for row in by_model:
        tokens_in = int(row.get("tokens_in") or 0)
        tokens_out = int(row.get("tokens_out") or 0)
        burn.append(
            TokenBurnOut(
                model=row.get("model"),
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                tokens_total=tokens_in + tokens_out,
            )
        )
    return PerformanceOut(
        period_days=days,
        answers=int(perf.get("answers") or 0),
        latency_p50_ms=perf.get("latency_p50_ms"),
        latency_p95_ms=perf.get("latency_p95_ms"),
        fallback_answers=fallback_answers,
        fallback_rate=(fallback_answers / scored) if scored else None,
        token_burn_by_model=burn,
    )


@router.get("/ingestion", response_model=dict[str, Any])
async def ingestion_health(tenant: TenantDep) -> dict[str, Any]:
    """Document counts by status, and the recent failures with their reason.

    Ingestion health belongs next to quality: a document stuck in `failed` is a
    silent hole in what the assistant can answer, and this is where a tenant
    notices it.
    """
    from sqlalchemy import text

    async with tenant_scope(tenant.tenant_id) as session:
        by_status = (
            await session.execute(
                text(
                    "SELECT status, COUNT(*) AS n FROM documents "
                    "WHERE status <> 'deleted' GROUP BY status"
                )
            )
        ).mappings().all()
        failures = (
            await session.execute(
                text(
                    "SELECT id, filename, error FROM documents "
                    "WHERE status = 'failed' ORDER BY created_at DESC LIMIT 10"
                )
            )
        ).mappings().all()

    return {
        "by_status": {row["status"]: int(row["n"]) for row in by_status},
        "recent_failures": [dict(row) for row in failures],
    }
