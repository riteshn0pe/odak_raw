"""Usage and analytics endpoints.

Per `productisation.md`, observability is not an internal nicety here — it is
a retention feature. A tenant that can see its own volume, latency, and (most
importantly) which questions the agent answered badly is a tenant that keeps
uploading documents instead of churning. Both routes are read-only and scoped
to the caller's own tenant.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.api.deps import ContainerDep, TenantDep

router = APIRouter(prefix="/usage", tags=["usage"])


class UsageByKindOut(BaseModel):
    kind: str
    quantity: int
    events: int


class UsageSummaryOut(BaseModel):
    period_days: int
    usage_by_kind: list[UsageByKindOut] = Field(default_factory=list)
    answers: int
    avg_latency_ms: float | None = None
    avg_confidence: float | None = None
    thumbs_up: int
    thumbs_down: int
    documents: int
    storage_bytes: int


class UnansweredItemOut(BaseModel):
    question: str | None
    confidence: float
    created_at: datetime


class UnansweredOut(BaseModel):
    items: list[UnansweredItemOut] = Field(default_factory=list)


@router.get("/summary", response_model=UsageSummaryOut)
async def usage_summary(
    tenant: TenantDep,
    container: ContainerDep,
    days: int = Query(default=30, ge=1, le=365),
) -> UsageSummaryOut:
    since = datetime.now(UTC) - timedelta(days=days)

    usage = await container.conversations.usage_summary(tenant.tenant_id, since)
    stats = await container.conversations.message_stats(tenant.tenant_id, since)
    totals = await container.documents.usage_totals(tenant.tenant_id)

    return UsageSummaryOut(
        period_days=days,
        usage_by_kind=[UsageByKindOut(**row) for row in usage],
        answers=stats["answers"] or 0,
        avg_latency_ms=stats["avg_latency_ms"],
        avg_confidence=stats["avg_confidence"],
        thumbs_up=stats["thumbs_up"] or 0,
        thumbs_down=stats["thumbs_down"] or 0,
        documents=totals["documents"],
        storage_bytes=totals["storage_bytes"],
    )


@router.get("/unanswered", response_model=UnansweredOut)
async def unanswered(
    tenant: TenantDep,
    container: ContainerDep,
    days: int = Query(default=30, ge=1, le=365),
    limit: int = Query(default=20, ge=1, le=100),
    threshold: float = Query(default=0.45, ge=0.0, le=1.0),
) -> UnansweredOut:
    """Top low-confidence answers — the "what to upload next" list.

    `days` bounds the window, matching `/summary`; the repository applies it as a
    `since` filter on the assistant turn's timestamp.
    """
    since = datetime.now(UTC) - timedelta(days=days)

    rows = await container.conversations.low_confidence_answers(
        tenant.tenant_id, threshold=threshold, limit=limit, since=since
    )
    return UnansweredOut(items=[UnansweredItemOut(**row) for row in rows])
