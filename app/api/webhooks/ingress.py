"""The unified ingress: one door for every new inbound provider.

``POST /webhooks/ingress/{provider}``. Verify the provider's signature, resolve
the tenant, archive the payload, enqueue a job, return 200 — and do nothing slow
in between. The existing ``/webhooks/whatsapp``, ``/webhooks/email`` and
``/webhooks/payments`` routers are untouched; new providers never get a
top-level router of their own, because five routers is five copies of the same
four steps and the fifth one gets one of them wrong.

**Tenant resolution is inside the credential, not in a lookup.** Every growth
table carries FORCE RLS, so a cross-tenant "which tenant owns this webhook
secret" query under ``admin_scope`` matches no rows at all — the scheduler
comment in ``growth_repo.active_tenant_ids`` explains the same trap. So a
provider's ingress key is an HMAC-signed statement naming the tenant, and
verifying the signature *is* resolving the tenant. There is no database read
before authentication.

The tenant's own switch is still checked, after: the ``growth_integrations`` row
for that provider must be ``active``, read inside that tenant's scope. A key that
was issued and then disconnected stops working.

Stage 2 wires one provider — ``website_form``. IndiaMART and the other
directories are Stage 7; they are absent here rather than stubbed, and the
Integrations screen renders them as ``planned``, which is the honest state.

Stage 4 adds ``shopify`` and ``woocommerce``. Both authenticate twice: our own
ingress key in the webhook URL resolves the tenant (same as the website form —
the subscription URL handed to the store carries ``?key=``), and the
provider's HMAC proves the body came from the store. Either check failing is a
401 with the same rejection log line.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request, Response

from app.api.deps import ContainerDep
from app.core.errors import AuthenticationError, ValidationError
from app.core.logging import get_logger, tenant_id_var
from app.services.growth.stores import CONNECTED_STATUSES

logger = get_logger(__name__)

# `CONNECTED_STATUSES` is imported rather than restated: the store service owns
# the status machine, and the ingress and the sync must not drift apart on what
# "connected" means. `error` is in that set — a store whose pull is backing off
# must still be able to push us an order.

router = APIRouter(prefix="/webhooks/ingress", tags=["webhooks"], include_in_schema=False)

# Providers this ingress accepts today, and the job each one enqueues. A
# provider absent from this map is a 401 like any other unverifiable request:
# telling an unauthenticated caller which providers exist is free reconnaissance.
#
# `telegram` is not in this map: a bot update is not a lead, and which job (if
# any) it enqueues depends on the command inside it. It is branched in the
# handler below, behind the same key verification and rate limits.
PROVIDERS: dict[str, str] = {
    "website_form": "growth_inbound_lead",
    "shopify": "growth_store_webhook",
    "woocommerce": "growth_store_webhook",
}

# Store providers authenticate the body themselves (HMAC), so they skip the
# lead-raw archive: an order payload is upserted by external id, which is its
# own idempotency, and a redelivery collides rather than doubling.
STORE_PROVIDERS: frozenset[str] = frozenset({"shopify", "woocommerce"})

# A form post is somebody's browser, so the body is small by nature. The cap is
# here anyway: this endpoint is unauthenticated until the key is checked, and an
# unbounded body on an unauthenticated route is a denial-of-service primitive.
MAX_BODY_BYTES = 64 * 1024

# Store webhooks get their own, larger cap. A Shopify orders/create payload is
# the whole order resource — every line item with its properties, both
# addresses, the tax and discount breakdowns — and a wholesale order runs past
# 64 KB comfortably. The failure mode is not a rejected order: Shopify retries a
# non-2xx for two days and then *removes the subscription*, so the cap that was
# meant to bound an unauthenticated flood silently uninstalls the integration
# instead. 1 MB clears any real order and still bounds the door.
STORE_MAX_BODY_BYTES = 1024 * 1024


def _max_body_bytes(provider: str) -> int:
    """The size cap for one provider. Store payloads are the only large ones."""
    return STORE_MAX_BODY_BYTES if provider in STORE_PROVIDERS else MAX_BODY_BYTES


# Per-key and per-IP ceilings, and they are not optional here.
#
# An ingress key is not a platform secret: it is pasted into third-party form
# builders and is embedded client-side in `form.js`'s `data-key`, so it reaches
# anyone who views source on the tenant's site. That same key opens this route,
# which means this door has to be throttled exactly as hard as the one in
# `growth_public.py` — otherwise a leaked key routes around the rate limit there
# by coming here instead, and produces unbounded `growth_lead_raw` inserts and
# queue enqueues against one tenant. Revocation is disconnect-and-reconnect, so a
# leaked key is a durable exposure and the throttle is what bounds it.
#
# The webhook ceiling is higher than the browser one: a form builder legitimately
# batches, a single browser does not.
INGRESS_RATE_PER_MINUTE = 60
IP_RATE_PER_MINUTE = 30


@router.post("/{provider}")
async def ingress(
    provider: str, request: Request, container: ContainerDep
) -> dict[str, Any]:
    """Accept one inbound payload and hand it to a job. Fast, always."""
    if provider == "telegram":
        return await accept_telegram(container, request)
    job_kind = PROVIDERS.get(provider)
    if job_kind is None:
        logger.warning(
            "webhook_signature_rejected",
            provider=provider,
            reason="unknown provider",
            ip=_client_ip(request),
        )
        raise AuthenticationError("invalid ingress request")

    raw = await request.body()
    if len(raw) > _max_body_bytes(provider):
        raise ValidationError("payload is too large")

    # the body is parsed before the key is checked, which looks backwards and is
    # not: the key may travel *in* the body (see `_ingress_key`), the size cap
    # above already bounds this, and `_parse` touches nothing but the bytes. The
    # same order, for the same reason, as `POST /v1/growth/form`.
    payload = _parse(raw)
    tenant_id = _verify(container, request, payload, provider)
    tenant_id_var.set(str(tenant_id))

    # after the signature, before anything the caller can make expensive. An
    # unauthenticated flood must not be able to consume a legitimate tenant's
    # window, which is why this is not the first thing in the handler
    await container.rate_limiter.check(
        "webhook_ingress", str(tenant_id), INGRESS_RATE_PER_MINUTE, 60
    )
    ip = _client_ip(request)
    if ip:
        await container.rate_limiter.check("webhook_ingress_ip", ip, IP_RATE_PER_MINUTE, 60)

    if provider in STORE_PROVIDERS:
        await _verify_store_signature(container, request, raw, tenant_id, provider)
        result = await accept_store(container, request, tenant_id, provider, payload)
        return result

    result = await accept_inbound(container, tenant_id, provider, payload, job_kind)
    return result


async def accept_telegram(container: Any, request: Request) -> dict[str, Any]:
    """One Telegram bot update: verify, resolve the tenant, hand to the bot.

    The tenant points their bot's webhook at this URL with their ingress key
    in it (`GET /v1/growth/telegram-setup` shows the exact URL), so the key —
    not a cross-tenant secret lookup, which FORCE RLS forbids — is what names
    the tenant, exactly as for the website form.

    Provider mode (a platform bot token in `TELEGRAM_BOT_TOKEN`) adds a second
    key on the same URL: the platform key names the bot, and the bot service
    resolves the *chat* to a tenant by the row its /start wrote. A tenant's own
    key still works beside it — Telegram delivers to the one registered
    webhook, so whichever bot is pointed here is the one that arrives.

    Everything interactive — commands, callback queries, the settings menus —
    lives in the bot service (P6, `app/services/growth/telegram_bot.py`);
    this door only does what every ingress door does: key, rate limits,
    integration check. The bot's own sender gate (the owner's chat id on the
    integration row) keeps anything else at today's 200-ignore.
    """
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise ValidationError("payload is too large")
    payload = _parse(raw)
    if str(container.settings.TELEGRAM_BOT_TOKEN or ""):
        # provider mode is armed. A key that is not the platform key falls
        # through to the per-tenant path below, byte for byte
        platform = await _accept_telegram_platform(container, request, payload)
        if platform is not None:
            return platform
    tenant_id = _verify(container, request, payload, "telegram")
    tenant_id_var.set(str(tenant_id))

    # both checks in one round-trip window: they share nothing, and a remote
    # cache makes each await cost a full network hop
    ip = _client_ip(request)
    checks = [
        container.rate_limiter.check(
            "webhook_ingress", str(tenant_id), INGRESS_RATE_PER_MINUTE, 60
        )
    ]
    if ip:
        checks.append(
            container.rate_limiter.check("webhook_ingress_ip", ip, IP_RATE_PER_MINUTE, 60)
        )
    await asyncio.gather(*checks)

    # get_by_provider, not get with an empty ref: the per-tenant telegram row
    # is keyed by the owner's chat id, so a "" lookup would miss it and refuse
    # a key whose integration is live
    integration = await container.growth.integrations.get_by_provider(
        tenant_id, "telegram"
    )
    if integration is None or integration.get("status") != "active":
        raise AuthenticationError("this source is not connected")

    return await container.telegram_bot.handle(tenant_id, integration, payload)


async def _accept_telegram_platform(
    container: Any, request: Request, payload: dict[str, Any]
) -> dict[str, Any] | None:
    """The platform bot's door. None when the key is not the platform key.

    Key, rate limits, then the bot service — the same order as the per-tenant
    path, with the platform subject standing in for the tenant id in the
    per-key bucket: every chat on the one bot shares that ceiling, which is
    the point of it.
    """
    from app.services.growth.ingresskey import (
        PLATFORM_SUBJECT,
        parse_platform_ingress_key,
    )

    key = _peek_key(request, payload)
    try:
        subject = parse_platform_ingress_key(container.settings.SECRET_KEY, key)
    except AuthenticationError:
        return None
    if subject != PLATFORM_SUBJECT:
        # signed by us, but not this door's key — the same rule as a key
        # issued for another source
        logger.warning(
            "webhook_signature_rejected",
            provider="telegram",
            reason="key is for another door",
            ip=_client_ip(request),
        )
        raise AuthenticationError("invalid ingress request")

    # both checks in one round-trip window — same rationale as the per-tenant
    # telegram door above
    ip = _client_ip(request)
    checks = [
        container.rate_limiter.check(
            "webhook_ingress", subject, INGRESS_RATE_PER_MINUTE, 60
        )
    ]
    if ip:
        checks.append(
            container.rate_limiter.check("webhook_ingress_ip", ip, IP_RATE_PER_MINUTE, 60)
        )
    await asyncio.gather(*checks)
    return await container.telegram_bot.handle_platform(payload)


async def accept_store(
    container: Any,
    request: Request,
    tenant_id: UUID,
    provider: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """One store event: check the store is connected, enqueue, 200 fast.

    No raw archive: the order upsert is idempotent on
    (tenant, provider, external_id), so a redelivery collides rather than
    doubling — the archive's job (making a parse failure visible) is served by
    the job logging the topic it could not interpret.
    """
    headers = {k.lower(): v for k, v in request.headers.items()}
    if provider == "shopify":
        from app.adapters.stores.shopify import TOPICS as SHOPIFY_TOPICS

        topic = headers.get("x-shopify-topic", "")
        if topic not in SHOPIFY_TOPICS:
            # a topic this stage never asked for: acknowledged, never enqueued.
            # Enqueueing it would spend a job to learn it carries nothing.
            return {"status": "ignored", "reason": f"topic {topic!r} is not subscribed"}
        shop = headers.get("x-shopify-shop-domain", "")
        integration = None
        if shop:
            integration = await container.growth.integrations.get(
                tenant_id, provider, shop.strip().lower()
            )
        if integration is None:
            rows = await container.growth.integrations.list(tenant_id)
            actives = [
                r for r in rows
                if str(r.get("provider")) == provider and r.get("status") in CONNECTED_STATUSES
            ]
            integration = actives[0] if actives else None
    else:
        from app.adapters.stores.woocommerce import CUSTOMER_TOPICS, ORDER_TOPICS

        topic = (
            headers.get("x-wc-webhook-topic", "")
            or headers.get("x-wc-webhook-source", "")
            or "order.updated"
        )
        if topic not in ORDER_TOPICS and topic not in CUSTOMER_TOPICS:
            return {"status": "ignored", "reason": f"topic {topic!r} is not subscribed"}
        rows = await container.growth.integrations.list(tenant_id)
        actives = [
            r for r in rows
            if str(r.get("provider")) == provider and r.get("status") in CONNECTED_STATUSES
        ]
        integration = actives[0] if actives else None

    if integration is None:
        # connected, then disconnected — or a key for a store never added.
        # Refused rather than silently accepted, same as the inbound path.
        raise AuthenticationError("this source is not connected")

    from app.adapters.queue.base import Job

    await container.queue.enqueue(
        Job(
            kind="growth_store_webhook",
            tenant_id=tenant_id,
            payload={"provider": provider, "topic": topic, "payload": payload},
        )
    )
    return {"status": "accepted"}


async def _verify_store_signature(
    container: Any,
    request: Request,
    raw: bytes,
    tenant_id: UUID,
    provider: str,
) -> None:
    """The provider's own HMAC, after our key already resolved the tenant.

    Shopify signs with the platform app secret (one secret for all shops);
    WooCommerce signs with the store's consumer secret (per store, from
    tenant_secrets — now readable, because the tenant is known). A mismatch
    is the same 401 + rejection log as every other ingress verifier.
    """
    headers = {k.lower(): v for k, v in request.headers.items()}
    ok = False
    if provider == "shopify":
        from app.adapters.stores.shopify import verify_webhook

        ok = verify_webhook(
            raw,
            headers.get("x-shopify-hmac-sha256", ""),
            container.settings.SHOPIFY_API_SECRET,
        )
    elif provider == "woocommerce":
        ok = await _verify_woo_signature(container, request, raw, tenant_id)
    if not ok:
        logger.warning(
            "webhook_signature_rejected",
            provider=provider,
            reason="bad hmac",
            ip=_client_ip(request),
        )
        raise AuthenticationError("invalid ingress request")


async def _verify_woo_signature(
    container: Any, request: Request, raw: bytes, tenant_id: UUID
) -> bool:
    """Try each connected WooCommerce store's secret until one verifies.

    The webhook body carries no store URL, so the key cannot select the
    store — but a tenant has a handful of stores at most, and HMAC is cheap.
    """
    from app.adapters.stores.woocommerce import unpack_secret, verify_webhook

    headers = {k.lower(): v for k, v in request.headers.items()}
    signature = headers.get("x-wc-webhook-signature", "")
    if not signature:
        return False
    rows = await container.growth.integrations.list(tenant_id)
    refs = [
        (str(r.get("external_ref") or ""), r.get("secret_ref"))
        for r in rows
        if str(r.get("provider")) == "woocommerce"
        and r.get("status") in CONNECTED_STATUSES
    ]

    async def _check(ref: tuple[str, Any]) -> bool:
        external_ref, secret_ref = ref
        if not secret_ref:
            return False
        packed = await container.growth_stores._read_secret(
            tenant_id, "woocommerce", external_ref
        )
        if not packed:
            return False
        _, secret = unpack_secret(packed)
        # HMAC is CPU-only; run inline — a tenant's store count is single digits
        return verify_webhook(raw, signature, secret)

    for ref in refs:
        if await _check(ref):
            return True
    return False


async def accept_inbound(
    container: Any,
    tenant_id: UUID,
    provider: str,
    payload: dict[str, Any],
    job_kind: str,
) -> dict[str, Any]:
    """Archive, then enqueue. Shared with the tenant-facing form endpoint.

    Both doors into the inbound pipeline — this webhook and ``POST
    /v1/growth/form`` — run this, so the archive-before-parse rule and the
    integration check cannot be true of one and not the other.
    """
    integration = await container.growth.integrations.get(tenant_id, provider)
    if integration is None or integration.get("status") != "active":
        # issued, then disconnected. Refused rather than silently accepted: a
        # tenant who turned a source off is entitled to have it be off
        raise AuthenticationError("this source is not connected")

    raw_row = await container.growth.raw.store(
        tenant_id,
        source=provider,
        payload=payload,
        dedupe_key=_dedupe_key(payload),
    )
    if raw_row is None:
        # the provider's own idempotency handle collided: a redelivery, which is
        # a success that must not produce a second lead
        return {"status": "duplicate"}

    from app.adapters.queue.base import Job

    await container.queue.enqueue(
        Job(
            kind=job_kind,
            tenant_id=tenant_id,
            payload={"raw_id": str(raw_row["id"]), "source": provider},
        )
    )
    return {"status": "accepted"}


# Where an ingress key may travel, best first. The order is a security ordering,
# not a convenience one.
KEY_HEADER = "x-ingress-key"
KEY_FIELD = "key"


def _peek_key(request: Request, payload: dict[str, Any]) -> str:
    """The caller's key, wherever it travelled, without consuming it.

    The platform check runs *before* the per-tenant verifier, and the verifier
    pops a body key — so this look-but-don't-take exists, and the two must
    agree on where a key may travel.
    """
    header = request.headers.get(KEY_HEADER)
    if header:
        return header
    body_key = payload.get(KEY_FIELD)
    if isinstance(body_key, str) and body_key:
        return body_key
    return request.query_params.get(KEY_FIELD) or ""


def _ingress_key(request: Request, payload: dict[str, Any]) -> tuple[str, str]:
    """The caller's key and how it arrived, in descending order of safety.

    1. **The `x-ingress-key` header.** The only form that stays out of access
       logs, proxy logs, `Referer` headers and browser history. Preferred, and
       what the Connections screen tells a tenant to use.
    2. **A `key` field in the body.** For a form stack that can post a body but
       cannot set a header — which is most of them.
    3. **The `?key=` query string.** Kept, because some builders accept nothing
       but a URL, and removing it would break the feature for exactly the
       least-technical tenants it exists for. But a key in a URL is a credential
       written into every log on its path, so its use is recorded and the panel
       labels it a fallback.

    The key is popped out of the body when it is found there, so it never reaches
    the lead parser as a form field or gets archived into `growth_lead_raw`
    alongside the payload.
    """
    header = request.headers.get(KEY_HEADER)
    if header:
        return header, "header"

    body_key = payload.pop(KEY_FIELD, None)
    if isinstance(body_key, str) and body_key:
        return body_key, "body"

    query_key = request.query_params.get(KEY_FIELD)
    if query_key:
        return query_key, "query"
    return "", "absent"


def _verify(
    container: Any, request: Request, payload: dict[str, Any], provider: str
) -> UUID:
    """Check the provider's credential and return the tenant it names.

    One verifier per provider, selected here rather than by an ``if`` chain in
    the handler, so adding IndiaMART in Stage 7 is one entry in this map and one
    function — and so it is obvious at a glance that no provider reaches the
    body without one.
    """
    from app.services.growth.ingresskey import parse_ingress_key

    key, arrived_by = _ingress_key(request, payload)
    if arrived_by == "query":
        # not refused — a URL is all some form builders accept — but counted, so
        # "who is still sending credentials in a URL" is a query rather than a
        # guess, and so a tenant can be moved onto the header. The key itself is
        # never logged
        logger.warning(
            "ingress_key_in_query_string",
            provider=provider,
            ip=_client_ip(request),
        )
    try:
        tenant_id, key_provider = parse_ingress_key(container.settings.SECRET_KEY, key)
    except AuthenticationError:
        logger.warning(
            "webhook_signature_rejected",
            provider=provider,
            reason="bad key",
            ip=_client_ip(request),
        )
        raise
    if key_provider != provider:
        # a key issued for one source must not open another. Without this a
        # tenant's website-form key would be a valid IndiaMART key the day
        # IndiaMART is added
        logger.warning(
            "webhook_signature_rejected",
            provider=provider,
            reason="key is for another source",
            ip=_client_ip(request),
        )
        raise AuthenticationError("invalid ingress request")
    return tenant_id


def _parse(raw: bytes) -> dict[str, Any]:
    """JSON or form-encoded, because form stacks post both.

    A body we cannot read is a 422 rather than a 500: the sender will keep
    posting the same shape until a human changes it, and a retry ladder on a
    body that will never parse is noise.
    """
    import json
    from urllib.parse import parse_qsl

    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        raise ValidationError("empty payload")
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise ValidationError("payload is not valid JSON") from None
        if not isinstance(data, dict):
            raise ValidationError("payload must be a JSON object")
        return data
    parsed = dict(parse_qsl(text, keep_blank_values=True))
    if not parsed:
        raise ValidationError("payload could not be read as JSON or form data")
    return parsed


def _dedupe_key(payload: dict[str, Any]) -> str | None:
    """The provider's own idempotency handle, when it sent one.

    None means "no handle", and a null in the partial unique index is distinct
    from every other null — so a provider that sends no id gets no deduplication
    rather than colliding with every other such payload. That is the correct
    trade: a duplicated lead is visible and mergeable, a swallowed one is not.
    """
    for field in ("id", "event_id", "submission_id", "form_id", "idempotency_key"):
        value = payload.get(field)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value).strip()[:200]
    return None


def _client_ip(request: Request) -> str:
    """Best-effort caller address for the rejection log line.

    Reads the proxy header because the app runs behind one, and falls back to
    the socket. It is a log field and nothing authorises on it, so a spoofed
    value costs nothing.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return request.client.host if request.client else ""


@router.get("/{provider}")
async def ingress_probe(provider: str) -> Response:
    """Some form stacks GET the URL once to check it exists.

    204, with no hint about whether the provider or the key is valid. A 404 here
    would enumerate which providers the platform supports for anyone who asked.
    """
    return Response(status_code=204)
