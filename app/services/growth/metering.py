"""Cost metering for the growth surfaces.

One helper, ``meter``, writing into the same ``usage_events`` ledger the rest of
the platform records to. Nothing here is a new pipeline: ``run_rollup`` already
folds any kind, and the ``unit_cost_micros`` column has existed since migration
002 — this is simply the first writer that sets it.

Two rules the rest of the growth code depends on:

* **Every provider call writes an event, including the free ones.** A kind priced
  at zero still gets a row, exactly as ``email_sent`` and ``escalation`` do. You
  cannot price what you never counted, and the first question anyone asks about
  a new channel is "how much are we doing of it".
* **The rate lives on the event, not in a plan table, where the vendor's price
  varies.** Meta charges per WhatsApp conversation by country and category, so a
  single platform-wide number would be wrong for most tenants. Those kinds carry
  their real cost in ``unit_cost_micros`` per row.

Failure is swallowed and logged, never raised. Metering is bookkeeping about work
that already happened — a ledger write that fails must not undo a mail that was
already sent, and the loud log line is what makes the gap visible.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.core.db import tenant_scope
from app.core.logging import get_logger

logger = get_logger(__name__)

# the registry of valid metered kinds — `meter` refuses a kind that is not in
# it, so a typo in a caller fails loudly instead of quietly writing a row the
# cost breakdown can never see
METERED_KINDS: frozenset[str] = frozenset(
    {
        "query",
        "ocr_page",
        "llm_token_out",
        "llm_token_in",
        "storage_byte",
        # one HTTP request plus a ledger row: a runaway action loop has to be
        # visible as a line on the ledger, even though the real cost of the loop
        # is prefill tokens, which llm_token_in already carries
        "action_call",
        "procedure_run",
        "escalation",
        "email_sent",
        # ---- growth platform (016_growth_core.sql) ----
        # The two WhatsApp conversation categories carry their real price on the
        # event itself, in unit_cost_micros, because Meta prices a conversation by
        # the recipient's country and the category — one platform-wide number
        # would be wrong for most tenants. The country/category table that
        # actually sets it is WHATSAPP_RATE_MICROS in
        # app/services/growth/senders/whatsapp_sender.py.
        "whatsapp_conversation_marketing",
        "whatsapp_conversation_utility",
        # Lead-data and verification providers, metered while the tenant's own
        # credits are being spent: the events exist from day one because a credit
        # burn nobody counted is a surprise nobody predicted.
        "apollo_credit",
        "hunter_credit",
        "email_verification",
        # One record pushed to a tenant's CRM, and one rendered report PDF.
        "crm_push",
        "report_pdf",
        # One approval request: a tenant must be able to see how often a human
        # was asked without that costing them anything.
        "approval",
        # One completed store sync pass (Shopify or WooCommerce) — we pay nobody
        # for it, but a tenant whose catalogue re-syncs every hour and a tenant
        # whose store is quiet cost very different amounts of our own capacity,
        # and that only becomes visible if the pass is counted.
        "store_sync",
        # ---- growth insights (020_growth_insights.sql) ----
        # One web-search provider call made by the insights watchers: the search
        # provider bills per request and the insights run is the only growth
        # surface that fans out over a tenant's whole keyword and competitor
        # list, so an unfiltered daily run is visible as a number before it is
        # visible as a cost.
        "web_search",
        # One competitor page fetched and diffed, like store_sync: a tenant
        # listing forty competitor URLs and a tenant whose store is quiet cost
        # very different amounts of our egress.
        "page_fetch",
    }
)

# The feature dimension stamped into meta, folded into usage_rollups.feature by
# run_rollup and split out by the admin cost screen. A growth event without one
# is a bug, so `meter` refuses rather than writing an untagged row that would
# quietly land in the pre-growth bucket alongside every legacy event.
FEATURES: frozenset[str] = frozenset(
    {
        "outbound",
        "inbound",
        "report",
        "campaign",
        "insights",
        "crm",
        "talkprep",
        "reviews",
        "approvals",
        # the profile agent's one extraction call (P4) — its own line so the
        # discovery run never hides inside another feature's tokens
        "profile",
    }
)


async def meter(
    tenant_id: UUID,
    kind: str,
    *,
    feature: str,
    quantity: int = 1,
    unit_cost_micros: int = 0,
    **meta: Any,
) -> None:
    """Append one usage event for a growth action.

    ``feature`` is keyword-only and validated, because it is the whole point of
    this helper over the generic ``conversation_repo.record``: an event tagged
    with the wrong feature is worse than an untagged one, since it makes a
    breakdown that looks right and is not.
    """
    if feature not in FEATURES:
        raise ValueError(f"unknown growth feature {feature!r}")
    if kind not in METERED_KINDS:
        # a kind the registry has never heard of is a bug in the caller, not a
        # row to write quietly into a bucket the cost breakdown can never see;
        # refusing is the same posture as the feature check one line up
        raise ValueError(f"unknown usage kind {kind!r}; add it to METERED_KINDS")

    payload = dict(meta)
    payload["feature"] = feature

    try:
        async with tenant_scope(tenant_id) as session:
            await session.execute(
                text(
                    "INSERT INTO usage_events "
                    "(tenant_id, kind, quantity, unit_cost_micros, meta) "
                    "VALUES (:tenant_id, :kind, :quantity, :unit_cost_micros, "
                    "CAST(:meta AS jsonb))"
                ),
                {
                    "tenant_id": tenant_id,
                    "kind": kind,
                    "quantity": quantity,
                    "unit_cost_micros": unit_cost_micros,
                    "meta": json.dumps(_jsonable(payload)),
                },
            )
    except Exception:
        # the work this describes already happened. Losing the ledger row costs
        # visibility; raising here would cost the tenant the action itself
        logger.warning(
            "growth_meter_failed",
            tenant_id=str(tenant_id),
            kind=kind,
            feature=feature,
            exc_info=True,
        )


async def meter_llm(
    tenant_id: UUID,
    usage: Any,
    *,
    feature: str,
    agent_key: str,
    model: str,
) -> None:
    """Write the token pair for one growth LLM call.

    Uses the existing ``llm_token_in`` / ``llm_token_out`` kinds rather than
    inventing growth-specific ones, so a growth agent's tokens land in the same
    ledger line as the RAG agent's and nobody has to reconcile two ledgers.
    ``meta.agent`` and ``meta.feature`` are what separate them again for the
    per-agent and per-feature views.

    ``usage`` is the ``Usage`` dataclass from the LLM adapter base, duck-typed so
    this module does not import an adapter.
    """
    tokens_in = int(getattr(usage, "input_tokens", 0) or 0)
    tokens_out = int(getattr(usage, "output_tokens", 0) or 0)
    if tokens_in:
        await meter(
            tenant_id,
            "llm_token_in",
            feature=feature,
            quantity=tokens_in,
            agent=agent_key,
            model=model,
        )
    if tokens_out:
        await meter(
            tenant_id,
            "llm_token_out",
            feature=feature,
            quantity=tokens_out,
            agent=agent_key,
            model=model,
        )


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
