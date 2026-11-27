"""The builtin-things agent: the platform's ready-made capabilities, callable.

It prepares nothing and researches nothing. Every other agent in the swarm
exists to *decide* something; this one exposes what the platform can already do
— retrieve from the shared knowledge base, send on a channel, push to a CRM,
read store orders, verify an address, render the report — as one registry with
one dispatch function.

The "agent" framing is for the graph, the A2A card and the demo: it has an
``AGENT_KEYS`` row and routes through ``modelconfig`` like everyone else, so it
can also narrate its own capabilities when asked. The implementation is code,
not a table — a capability is a declaration plus a handler method, and the
dispatch is a dict lookup with input validation in front of it.

How other agents call it: in-process, as a function, during the day's context.
Agents in this codebase call services directly, never each other over HTTP.
Every successful call appends its outcome to the tenant's run-context day
(P1), so the report can say what the platform *did*, not just what the
deciding agents thought.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.core.errors import ValidationError
from app.core.logging import get_logger
from app.domain.growth import normalise_email

logger = get_logger(__name__)

AGENT_KEY = "builtin_things"
FEATURE = "outbound"

# Per-chunk text ceiling on the retrieve result. The capability returns chunk
# text because that is the point, but an unbounded return would also be an
# unbounded run-context note; the consumer that needs more re-retrieves with a
# higher top_k rather than parsing a novel out of one dict.
MAX_CHUNK_CHARS = 2000


@dataclass(slots=True, frozen=True)
class Capability:
    """One thing the platform can do, declared for callers and for the card.

    ``inputs`` is field name -> one line of shape, declared for callers and for
    the card. ``required`` is the subset dispatch refuses to run without; the
    rest are optional with handler-side defaults (an either/or pair like
    ``lead_id``/``customer_id`` is left to the handler, which can name the
    mistake better than a presence check can). Neither is a schema: the handlers
    are the same functions the services already expose, and a second validation
    layer would drift from them. ``cost`` is the class, not a number — the
    number lives in the metering ledger where the vendor's price varies.
    """

    key: str
    description: str
    inputs: dict[str, str]
    required: tuple[str, ...] = ()
    cost: str = "free"  # "free" | "credit" | "money" | "llm"


CAPABILITIES: dict[str, Capability] = {
    "retrieve": Capability(
        key="retrieve",
        description="Search the shared knowledge base and return matching passages.",
        inputs={"query": "what to look for", "top_k": "passages to return, default 8"},
        required=("query",),
        cost="free",
    ),
    "send_email": Capability(
        key="send_email",
        description="Queue and deliver one outbound mail to a lead or customer.",
        inputs={
            "content": "the mail body",
            "lead_id": "recipient lead (or customer_id)",
            "customer_id": "recipient store customer (or lead_id)",
        },
        required=("content",),
        cost="money",
    ),
    "send_whatsapp": Capability(
        key="send_whatsapp",
        description="Queue and deliver one outbound WhatsApp message.",
        inputs={
            "content": "the message body",
            "lead_id": "recipient lead (or customer_id)",
            "customer_id": "recipient store customer (or lead_id)",
        },
        required=("content",),
        cost="money",
    ),
    "crm_push": Capability(
        key="crm_push",
        description="Push one lead and its new activity notes to HubSpot or Zoho.",
        inputs={
            "provider": "hubspot or zoho",
            "lead_id": "the lead to push",
            "force": "re-push even when already synced, default false",
        },
        required=("provider", "lead_id"),
        cost="free",
    ),
    "store_orders": Capability(
        key="store_orders",
        description="Recent store orders, newest first, with totals and items.",
        inputs={"limit": "how many orders, default 50"},
        cost="free",
    ),
    "verify_email": Capability(
        key="verify_email",
        description="Check one address for deliverability; costs one credit.",
        inputs={"email": "the address to check"},
        required=("email",),
        cost="credit",
    ),
    "render_report": Capability(
        key="render_report",
        description="Build and deliver the on-demand report (numbers, PDF, channels).",
        inputs={"via": "who asked, for the audit line, default 'builtin'"},
        cost="llm",
    ),
}


class BuiltinService:
    """Dispatches capability calls onto the services that already implement them."""

    def __init__(self, container: Any) -> None:
        self._c = container

    async def call(self, tenant_id: UUID, capability: str, **kwargs: Any) -> dict[str, Any]:
        """Run one capability. Validates the declared inputs, then dispatches.

        Raises ``ValidationError`` for an unknown key or a missing input — the
        same readable refusal the rest of the growth surface uses — because a
        caller that typos a capability name should fail now, not discover an
        empty dict three steps later when the report asks what happened.
        """
        spec = CAPABILITIES.get(capability)
        if spec is None:
            raise ValidationError(
                f"unknown capability {capability!r}; known: {sorted(CAPABILITIES)}"
            )
        for field_name in spec.required:
            if kwargs.get(field_name) in (None, ""):
                raise ValidationError(f"capability {capability!r} needs {field_name!r}")

        handler: Any = getattr(self, f"_call_{capability}")
        result: dict[str, Any] = await handler(tenant_id, kwargs)
        await self._note(tenant_id, capability, result)
        return result

    def describe(self) -> list[dict[str, Any]]:
        """The registry as plain data — for the A2A card and the demo screen."""
        return [
            {
                "key": spec.key,
                "description": spec.description,
                "inputs": dict(spec.inputs),
                "required": list(spec.required),
                "cost": spec.cost,
            }
            for spec in CAPABILITIES.values()
        ]

    # ------------------------------------------------------------- handlers

    async def _call_retrieve(self, tenant_id: UUID, kwargs: dict[str, Any]) -> dict[str, Any]:
        """Shared-KB search — the same retrieval the support agent answers from."""
        from app.domain.agent_config import RetrievalConfig

        top_k = _positive_int(kwargs.get("top_k"), default=8, ceiling=50)
        result = await self._c.retrieval.search(
            tenant_id, str(kwargs["query"]), RetrievalConfig(top_k=top_k)
        )
        return {
            "strategy": result.strategy,
            "confidence": result.confidence,
            "chunks": [
                {
                    "document_id": str(chunk.document_id),
                    "filename": chunk.filename,
                    "score": chunk.score,
                    "text": chunk.text[:MAX_CHUNK_CHARS],
                }
                for chunk in result.chunks
            ],
        }

    async def _call_send(
        self, tenant_id: UUID, kwargs: dict[str, Any], channel: str
    ) -> dict[str, Any]:
        """Queue one message, deliver it, and mark the row the way the dispatcher does.

        The row insert is not bookkeeping: the sender builds its idempotency key
        from the row id, so a caller's retry cannot deliver the same mail twice
        even when the first attempt timed out mid-flight.
        """
        lead_id = kwargs.get("lead_id")
        customer_id = kwargs.get("customer_id")
        if bool(lead_id) == bool(customer_id):
            # exactly one recipient handle, the same rule the senders enforce
            # two frames later — caught here so the refusal names the caller's
            # mistake rather than "this message is not attached to a lead"
            raise ValidationError(
                "give exactly one of lead_id or customer_id"
            )
        row = await self._c.growth.messages.add(
            tenant_id,
            lead_id=UUID(str(lead_id)) if lead_id else None,
            customer_id=UUID(str(customer_id)) if customer_id else None,
            channel=channel,
            direction="out",
            content=str(kwargs["content"]),
            status="queued",
        )
        if row is None:
            raise ValidationError("the message could not be queued")

        receipt = await self._c.growth_senders.for_channel(channel).send(tenant_id, row)
        await self._c.growth.messages.set_status(
            tenant_id,
            UUID(str(row["id"])),
            "sent",
            provider_id=receipt.provider_id,
            cost_micros=int(receipt.cost_micros),
        )
        return {"message_id": str(row["id"]), "provider_id": receipt.provider_id}

    async def _call_send_email(self, tenant_id: UUID, kwargs: dict[str, Any]) -> dict[str, Any]:
        return await self._call_send(tenant_id, kwargs, "email")

    async def _call_send_whatsapp(self, tenant_id: UUID, kwargs: dict[str, Any]) -> dict[str, Any]:
        return await self._call_send(tenant_id, kwargs, "whatsapp")

    async def _call_crm_push(self, tenant_id: UUID, kwargs: dict[str, Any]) -> dict[str, Any]:
        return dict(
            await self._c.growth_crm.push_lead(
                tenant_id,
                str(kwargs["provider"]),
                UUID(str(kwargs["lead_id"])),
                force=bool(kwargs.get("force")),
            )
        )

    async def _call_store_orders(self, tenant_id: UUID, kwargs: dict[str, Any]) -> dict[str, Any]:
        limit = _positive_int(kwargs.get("limit"), default=50, ceiling=500)
        orders = await self._c.growth.orders.recent(tenant_id, limit)
        return {"orders": [dict(row) for row in orders]}

    async def _call_verify_email(self, tenant_id: UUID, kwargs: dict[str, Any]) -> dict[str, Any]:
        """One address, one verdict, one credit — the same call research makes.

        ``checked: False`` is the no-verifier deployment state, not an error:
        callers treat it as pass-through, exactly as research did before this
        registry existed. Only ``sendable: False`` is a drop signal.
        """
        email = normalise_email(str(kwargs["email"]))
        if not email:
            raise ValidationError("a usable email address is required")

        async with self._c.growth_providers.email_verifier(tenant_id) as verifier:
            if verifier is None:
                return {"checked": False, "status": "unknown", "sendable": True, "detail": ""}
            result = await verifier.verify(email)

            from app.services.growth.metering import meter

            await meter(
                tenant_id,
                "email_verification",
                feature=FEATURE,
                quantity=1,
                agent=AGENT_KEY,
                action="verify",
                status=result.status,
            )
            return {
                "checked": True,
                "status": result.status,
                "sendable": bool(result.sendable),
                "detail": result.detail,
            }

    async def _call_render_report(self, tenant_id: UUID, kwargs: dict[str, Any]) -> dict[str, Any]:
        return dict(
            await self._c.growth_reports.request_on_demand(
                tenant_id, via=str(kwargs.get("via") or "builtin")
            )
        )

    # ---------------------------------------------------------------- private

    async def _note(self, tenant_id: UUID, capability: str, result: dict[str, Any]) -> None:
        """Append the outcome to the tenant's run-context day, P1's scratchpad.

        Guarded for the same reason research's ``_note`` is: a context-write
        outage must cost the report a sentence, never the caller the work the
        result of. The day key comes from the profile timezone so this call and
        the report land on the same date however close to midnight they run.
        """
        try:
            from app.services.growth.reports.windows import tenant_today

            growth = getattr(self._c, "growth", None)
            run_context = getattr(growth, "run_context", None)
            if run_context is None:
                return
            settings = await self._c.growth_profile.get(tenant_id)
            await run_context.append(
                tenant_id,
                local_date=tenant_today(settings or {}),
                agent_key=AGENT_KEY,
                kind="capability",
                payload={"capability": capability, **result},
            )
        except Exception:
            logger.warning(
                "builtin_things_note_failed",
                tenant_id=str(tenant_id),
                capability=capability,
                exc_info=True,
            )


def _positive_int(value: Any, *, default: int, ceiling: int) -> int:
    """Clamp an optional caller-supplied count. Defaults, never trusts."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(parsed, ceiling))
