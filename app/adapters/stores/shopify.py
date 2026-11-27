"""Shopify adapter: GraphQL Admin API only, OAuth, webhooks, discounts.

Public-app OAuth (``read_customers read_orders read_all_orders read_products
read_discounts write_discounts read_checkouts`` — ``read_all_orders`` is the
approval-gated one for history older than 60 days, see the manual doc). The
access token lives in ``tenant_secrets`` under a per-shop ref and the
``growth_integrations`` row carries only the shop domain (``external_ref``)
plus a ``secret_ref`` pointer — never the token.

Sync is GraphQL with an ``updated_at`` search filter plus keyset pagination;
the service persists the per-side bookmark on the integration row's
``health``. Webhooks (orders/create, customers/create|update,
checkouts/create|update, products/update) arrive via the unified ingress,
verified here with the app secret.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import httpx

from app.core.errors import ProviderError, ValidationError
from app.core.logging import get_logger
from app.domain.ports import (
    CustomerPage,
    DiscountReceipt,
    OrderPage,
    StoreCustomer,
    StoreOrder,
)

logger = get_logger(__name__)

PROVIDER = "shopify"

SCOPES = (
    "read_customers,read_orders,read_all_orders,read_products,"
    "read_discounts,write_discounts,read_checkouts"
)

API_VERSION = "2024-10"
PAGE_SIZE = 100

# Webhook topics this adapter understands. Anything else is acknowledged and
# ignored before any work is enqueued — a shop's app can emit topics this
# stage never asked for, and each one must not become a sync-shaped job.
TOPICS: frozenset[str] = frozenset(
    {
        "orders/create",
        "orders/updated",
        "customers/create",
        "customers/update",
        "checkouts/create",
        "checkouts/update",
        "products/update",
    }
)

# What we actually subscribe to at connect time. A subset of TOPICS: a shop may
# already carry a subscription for a topic we merely tolerate (orders/updated),
# and asking for one we do not need is a delivery we pay to ignore.
REGISTER_TOPICS: tuple[str, ...] = (
    "orders/create",
    "customers/create",
    "customers/update",
    "checkouts/create",
    "checkouts/update",
    "products/update",
)

_CUSTOMERS_QUERY = """
query customers($first: Int!, $after: String, $query: String) {
  customers(first: $first, after: $after, query: $query) {
    pageInfo { hasNextPage endCursor }
    edges { node {
      id email phone firstName lastName tags
      acceptsEmailMarketing acceptsSmsMarketing
      totalSpentV2 { amount currencyCode }
      numberOfOrders
      createdAt updatedAt
    } }
  }
}
"""

_ORDERS_QUERY = """
query orders($first: Int!, $after: String, $query: String) {
  orders(first: $first, after: $after, query: $query) {
    pageInfo { hasNextPage endCursor }
    edges { node {
      id name email phone
      totalPriceSet { shopMoney { amount currencyCode } }
      displayFinancialStatus displayFulfillmentStatus
      paymentGatewayNames
      discountCodes
      lineItems(first: 50) { edges { node {
        title productType vendor quantity sku
      } } }
      createdAt updatedAt
    } }
  }
}
"""


def verify_webhook(raw: bytes, hmac_header: str, api_secret: str) -> bool:
    """Shopify's ``X-Shopify-Hmac-Sha256`` check, constant time.

    The header is base64; anything that is not decodable base64 is a
    forgery rather than a formatting debate.
    """
    if not hmac_header or not api_secret:
        return False
    try:
        supplied = base64.b64decode(hmac_header.strip())
    except Exception:
        return False
    expected = hmac.new(api_secret.encode("utf-8"), raw, hashlib.sha256).digest()
    return hmac.compare_digest(expected, supplied)


def authorize_url(*, api_key: str, shop: str, redirect_uri: str, state: str) -> str:
    """The URL the owner visits to install the public app on their shop."""
    domain = _shop_domain(shop)
    params = urlencode(
        {
            "client_id": api_key,
            "scope": SCOPES,
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )
    return f"https://{domain}/admin/oauth/authorize?{params}"


class ShopifyStore:
    """GraphQL-backed ``StoreProvider`` bound to one shop's token."""

    def __init__(
        self,
        shop: str,
        access_token: str,
        *,
        api_version: str = API_VERSION,
        timeout: float = 30.0,
    ) -> None:
        if not shop or not access_token:
            raise ValueError("shop and access_token are required")
        domain = shop_domain(shop)
        # the Admin API lives on *.myshopify.com, always. Without this a
        # typo'd (or hostile) shop value would point the token at an
        # attacker's host — with the tenant's access token in the header.
        if not domain.endswith(".myshopify.com"):
            raise ValueError("a Shopify shop must be a *.myshopify.com domain")
        self._shop = domain
        self._token = access_token
        self._client = httpx.AsyncClient(
            base_url=f"https://{self._shop}/admin/api/{api_version}/graphql.json",
            timeout=httpx.Timeout(timeout, connect=min(timeout, 10.0)),
            headers={
                "X-Shopify-Access-Token": access_token,
                "Content-Type": "application/json",
            },
        )

    @property
    def name(self) -> str:
        return PROVIDER

    async def sync_customers(
        self, *, cursor: str = "", limit: int = PAGE_SIZE
    ) -> CustomerPage:
        """Buyers changed since the cursor's ``updated_at``, oldest first.

        Cursor packs ``"<since ISO>|<GraphQL after>"``; when the side is
        exhausted the bookmark advances to now, so the next run only asks for
        what changed since. A run that crashes mid-page re-pages the same
        cursor — the service upserts, so that is a repeat, not a loss.
        """
        since, after = _unpack(cursor)
        query = f"updated_at:>='{since}'" if since else None
        try:
            data = await self._graphql(
                _CUSTOMERS_QUERY,
                {"first": min(max(limit, 1), 250), "after": after, "query": query},
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ProviderError(PROVIDER, "Shopify is unreachable") from exc
        conn = (data.get("data") or {}).get("customers") or {}
        customers = []
        for edge in conn.get("edges") or []:
            parsed = _customer(edge.get("node") or {})
            if parsed is not None:
                customers.append(parsed)
        page = conn.get("pageInfo") or {}
        if page.get("hasNextPage") and page.get("endCursor"):
            return CustomerPage(
                customers=customers, next_cursor=_pack(since, str(page["endCursor"]))
            )
        return CustomerPage(customers=customers, next_cursor=_pack(_overlap_iso(), ""))

    async def sync_orders(
        self, *, cursor: str = "", limit: int = PAGE_SIZE
    ) -> OrderPage:
        """Orders changed since the cursor's ``updated_at``. Same bookmark
        discipline as ``sync_customers``, tracked separately."""
        since, after = _unpack(cursor)
        query = f"updated_at:>='{since}'" if since else None
        try:
            data = await self._graphql(
                _ORDERS_QUERY,
                {"first": min(max(limit, 1), 250), "after": after, "query": query},
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ProviderError(PROVIDER, "Shopify is unreachable") from exc
        conn = (data.get("data") or {}).get("orders") or {}
        orders = []
        for edge in conn.get("edges") or []:
            parsed = _order(edge.get("node") or {})
            if parsed is not None:
                orders.append(parsed)
        page = conn.get("pageInfo") or {}
        if page.get("hasNextPage") and page.get("endCursor"):
            return OrderPage(
                orders=orders, next_cursor=_pack(since, str(page["endCursor"]))
            )
        return OrderPage(orders=orders, next_cursor=_pack(_overlap_iso(), ""))

    async def create_discount(self, *, code: str, percent: int, name: str = "") -> DiscountReceipt:
        """A basic percentage code via the ``discountCodeBasicCreate`` mutation."""
        if not code or not 1 <= percent <= 100:
            raise ValidationError("discount needs a code and 1-100 percent")
        mutation = """
        mutation discountCodeBasicCreate($basicCodeDiscount: DiscountCodeBasicInput!) {
          discountCodeBasicCreate(basicCodeDiscount: $basicCodeDiscount) {
            codeDiscountNode { id }
            userErrors { field message }
          }
        }
        """
        payload = {
            "basicCodeDiscount": {
                "title": name or code,
                "code": code,
                "startsAt": datetime.now().astimezone().isoformat(),
                "customerSelection": {"all": True},
                "customerGets": {
                    "value": {"percentage": percent / 100.0},
                    "items": {"all": True},
                },
            }
        }
        try:
            data = await self._graphql(mutation, payload)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ProviderError(PROVIDER, "Shopify is unreachable") from exc
        node = ((data.get("data") or {}).get("discountCodeBasicCreate")) or {}
        errors = node.get("userErrors") or []
        if errors:
            detail = str(errors[0].get("message") or "unknown")[:200]
            raise ProviderError(PROVIDER, f"Shopify refused the discount: {detail}")
        discount_node = node.get("codeDiscountNode") or {}
        return DiscountReceipt(
            code=code, external_id=str(discount_node.get("id") or ""), detail={"percent": percent}
        )

    async def register_webhooks(
        self, callback_url: str, topics: tuple[str, ...] = REGISTER_TOPICS
    ) -> dict[str, Any]:
        """Subscribe this shop's topics to our ingress. Without it none arrive.

        A Shopify app does not receive webhooks because its scopes allow them;
        it receives them because a ``webhookSubscription`` exists per topic per
        shop. Nothing else in this codebase creates one, so this is the whole
        reason a store event ever reaches ``/webhooks/ingress/shopify``.

        Re-registering is the normal case (every reconnect runs this), and
        Shopify answers a duplicate with a userError rather than an HTTP error —
        "address for this topic has already been taken". That is success: the
        subscription we wanted exists. Any other userError is counted and
        returned so the caller can surface it on ``health``, because a partial
        registration (orders subscribed, customers not) is a silent half-feature
        otherwise.
        """
        mutation = """
        mutation webhookSubscriptionCreate(
          $topic: WebhookSubscriptionTopic!,
          $webhookSubscription: WebhookSubscriptionInput!
        ) {
          webhookSubscriptionCreate(
            topic: $topic, webhookSubscription: $webhookSubscription
          ) {
            webhookSubscription { id }
            userErrors { field message }
          }
        }
        """
        registered: list[str] = []
        failed: dict[str, str] = {}
        for topic in topics:
            variables = {
                "topic": _topic_enum(topic),
                "webhookSubscription": {"callbackUrl": callback_url, "format": "JSON"},
            }
            try:
                data = await self._graphql(mutation, variables)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                raise ProviderError(PROVIDER, "Shopify is unreachable") from exc
            except ProviderError as exc:
                failed[topic] = str(exc)[:200]
                continue
            node = ((data.get("data") or {}).get("webhookSubscriptionCreate")) or {}
            errors = node.get("userErrors") or []
            if not errors:
                registered.append(topic)
                continue
            message = str(errors[0].get("message") or "unknown")
            if "already been taken" in message.lower():
                # the subscription we wanted is already there — idempotent, not a
                # failure, and every reconnect takes this branch
                registered.append(topic)
                continue
            failed[topic] = message[:200]
        return {"registered": registered, "failed": failed}

    @staticmethod
    async def exchange_code_for_shop(
        *, shop: str, api_secret: str, api_key: str, code: str
    ) -> str:
        """OAuth callback: trade the one-time ``code`` for a permanent token."""
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"https://{_shop_domain(shop)}/admin/oauth/access_token",
                    data={
                        "client_id": api_key,
                        "client_secret": api_secret,
                        "code": code,
                    },
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ProviderError(PROVIDER, "Shopify is unreachable") from exc
        if resp.status_code >= 400:
            raise ProviderError(PROVIDER, f"Shopify OAuth failed: HTTP {resp.status_code}")
        try:
            oauth_body: dict[str, Any] = resp.json()
        except ValueError as exc:
            raise ProviderError(PROVIDER, "Shopify OAuth returned invalid JSON") from exc
        if not isinstance(oauth_body, dict):
            raise ProviderError(PROVIDER, "Shopify OAuth returned invalid JSON")
        token = str(oauth_body.get("access_token") or "")
        if not token:
            raise ProviderError(PROVIDER, "Shopify OAuth returned no access token")
        return token

    async def exchange_code(
        self, *, api_secret: str, api_key: str, code: str
    ) -> str:
        """OAuth callback: trade the one-time ``code`` for a permanent token."""
        return await ShopifyStore.exchange_code_for_shop(
            shop=self._shop, api_secret=api_secret, api_key=api_key, code=code
        )

    async def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        resp = await self._client.post("", json={"query": query, "variables": variables})
        if resp.status_code >= 400:
            raise ProviderError(PROVIDER, f"Shopify GraphQL HTTP {resp.status_code}")
        try:
            body: dict[str, Any] = resp.json()
        except ValueError as exc:
            raise ProviderError(PROVIDER, "Shopify returned invalid JSON") from exc
        if not isinstance(body, dict):
            raise ProviderError(PROVIDER, "Shopify returned invalid JSON")
        if body.get("errors"):
            first = body["errors"][0]
            raise ProviderError(
                PROVIDER, f"Shopify GraphQL error: {first.get('message', 'unknown')}"[:300]
            )
        return body

    async def close(self) -> None:
        await self._client.aclose()


async def exchange_shopify_code(
    *, shop: str, api_secret: str, api_key: str, code: str
) -> str:
    """OAuth callback without a store instance: the code is the credential."""
    return await ShopifyStore.exchange_code_for_shop(
        shop=shop, api_secret=api_secret, api_key=api_key, code=code
    )


def build_shopify(
    shop: str, access_token: str, *, timeout: float = 30.0
) -> ShopifyStore | None:
    """None when this shop has no token — the graceful-off posture."""
    if not shop or not access_token:
        return None
    return ShopifyStore(shop, access_token, timeout=timeout)


# ------------------------------------------------------------------ private


def shop_domain(raw: str) -> str:
    """Bare shop host, with the myshopify suffix when the owner typed one word."""
    value = raw.strip().lower()
    value = value.removeprefix("https://").removeprefix("http://").split("/")[0]
    if "." not in value:
        value = f"{value}.myshopify.com"
    return value


def _shop_domain(raw: str) -> str:
    return shop_domain(raw)


def _topic_enum(topic: str) -> str:
    """``orders/create`` as the GraphQL ``WebhookSubscriptionTopic`` enum member.

    The REST vocabulary (slash, lower case) is what the ``X-Shopify-Topic``
    header carries and therefore what the rest of this codebase speaks; the
    GraphQL mutation takes the enum spelling of the same thing. Converting at
    the one call site keeps a second topic vocabulary out of the module.
    """
    return topic.replace("/", "_").upper()


def verify_oauth_callback(params: dict[str, str], api_secret: str) -> bool:
    """Shopify's own ``hmac`` over the OAuth callback query string, constant time.

    Documented and required: the signed ``state`` proves *we* started the flow,
    but only this proves the redirect's parameters — the ``code`` above all —
    came from Shopify rather than from whoever is holding the browser. Without
    it a leaked authorize URL lets an attacker complete somebody else's install
    with a code from a shop they control.

    The message is every parameter except ``hmac`` (and the legacy
    ``signature``), sorted by key and joined as a query string; the digest is
    hex, not base64 — unlike the webhook header a few functions up.
    """
    supplied = str(params.get("hmac") or "")
    if not supplied or not api_secret:
        return False
    message = "&".join(
        f"{key}={params[key]}"
        for key in sorted(params)
        if key not in ("hmac", "signature")
    )
    expected = hmac.new(
        api_secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, supplied)


def _overlap_iso() -> str:
    """Where an exhausted side resumes: ten minutes before now.

    A row updated *during* the run can land ahead of the keyset cursor and
    never be paged over; without an overlap the next run's bookmark (now)
    would miss it forever. Re-scanning ten minutes costs a few repeated rows
    per hour, and the service upserts — a repeat is a no-op, a miss is lost
    money. Ten minutes comfortably covers one run's own duration.
    """
    from datetime import timedelta

    return (datetime.now(UTC) - timedelta(minutes=10)).isoformat()


def _unpack(cursor: str) -> tuple[str, str | None]:
    """A ``"<since ISO>|<GraphQL after>"`` bookmark back apart."""
    since, _, after = (cursor or "").partition("|")
    return since, after or None


def _pack(since: str, after: str) -> str:
    """Pack the bookmark back up. Empty ``after`` with a fresh ``since`` ends a side."""
    return f"{since}|{after}"


def _customer(node: dict[str, Any]) -> StoreCustomer | None:
    email = str(node.get("email") or "").strip()
    phone = str(node.get("phone") or "").strip()
    if not email and not phone:
        return None
    first = str(node.get("firstName") or "").strip()
    last = str(node.get("lastName") or "").strip()
    spent = node.get("totalSpentV2") or {}
    try:
        amount = float(str(spent.get("amount") or "0"))
    except ValueError:
        amount = 0.0
    return StoreCustomer(
        external_id=str(node.get("id") or ""),
        name=f"{first} {last}".strip(),
        phone=phone,
        email=email,
        tags=[str(t) for t in (node.get("tags") or []) if t],
        consent={
            "email": bool(node.get("acceptsEmailMarketing")),
            "whatsapp": bool(node.get("acceptsSmsMarketing")),
        },
        total_spend_micros=int(amount * 1_000_000),
        order_count=int(node.get("numberOfOrders") or 0),
        first_order_at=_dt(node.get("createdAt")),
        last_order_at=_dt(node.get("updatedAt")),
        updated_at=_dt(node.get("updatedAt")),
        raw={"id": node.get("id")},
    )


def _order(node: dict[str, Any]) -> StoreOrder | None:
    external = str(node.get("id") or node.get("name") or "")
    if not external:
        return None
    money = ((node.get("totalPriceSet") or {}).get("shopMoney")) or {}
    try:
        amount = float(str(money.get("amount") or "0"))
    except ValueError:
        amount = 0.0
    gateways = [str(g).lower() for g in (node.get("paymentGatewayNames") or [])]
    payment = "cod" if any("cod" in g or "cash" in g for g in gateways) else "prepaid"
    items: dict[str, Any] = {
        "line_items": [
            {
                "title": (item.get("node") or {}).get("title", ""),
                "product_type": (item.get("node") or {}).get("productType", ""),
                "vendor": (item.get("node") or {}).get("vendor", ""),
                "quantity": (item.get("node") or {}).get("quantity", 1),
            }
            for item in ((node.get("lineItems") or {}).get("edges") or [])
        ],
        "discount_codes": list(node.get("discountCodes") or []),
        "name": node.get("name", ""),
    }
    return StoreOrder(
        external_id=external,
        customer_email=str(node.get("email") or ""),
        customer_phone=str(node.get("phone") or ""),
        total_micros=int(amount * 1_000_000),
        payment=payment,
        status=str(
            node.get("displayFulfillmentStatus")
            or node.get("displayFinancialStatus")
            or ""
        ),
        items=items,
        ordered_at=_dt(node.get("createdAt")),
        updated_at=_dt(node.get("updatedAt")),
        raw={"id": node.get("id"), "name": node.get("name")},
    )


def _dt(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def order_from_webhook(topic: str, payload: dict[str, Any]) -> StoreOrder | None:
    """A REST-shaped webhook payload into the neutral order (any order topic)."""
    if "order" not in topic and "checkout" not in topic:
        return None
    external = str(payload.get("admin_graphql_api_id") or payload.get("id") or "")
    if not external:
        return None
    try:
        total = float(str(payload.get("total_price") or payload.get("total") or "0"))
    except ValueError:
        total = 0.0
    gateways = str(payload.get("payment_gateway_names") or payload.get("gateway") or "").lower()
    codes = payload.get("discount_codes") or payload.get("discount_applications") or []
    if isinstance(codes, list):
        discount_list = [str((c or {}).get("code", c)) for c in codes]
    else:
        discount_list = []
    customer = payload.get("customer") or {}
    return StoreOrder(
        external_id=external,
        customer_external_id=str(customer.get("admin_graphql_api_id") or customer.get("id") or ""),
        customer_email=str(payload.get("email") or customer.get("email") or ""),
        customer_phone=str(payload.get("phone") or customer.get("phone") or ""),
        total_micros=int(total * 1_000_000),
        payment="cod" if ("cod" in gateways or "cash" in gateways) else "prepaid",
        status=str(payload.get("fulfillment_status") or payload.get("financial_status") or ""),
        items={
            "line_items": payload.get("line_items") or [],
            "discount_codes": discount_list,
            "name": payload.get("name", ""),
            "topic": topic,
        },
        ordered_at=_dt(payload.get("created_at")),
        updated_at=_dt(payload.get("updated_at")),
        raw={"id": payload.get("id")},
    )


def customer_from_webhook(topic: str, payload: dict[str, Any]) -> StoreCustomer | None:
    """A REST-shaped customer webhook payload into the neutral customer."""
    if "customer" not in topic:
        return None
    email = str(payload.get("email") or "").strip()
    phone = str(payload.get("phone") or "").strip()
    if not email and not phone:
        return None
    return StoreCustomer(
        external_id=str(payload.get("admin_graphql_api_id") or payload.get("id") or ""),
        name=f"{payload.get('first_name', '')} {payload.get('last_name', '')}".strip(),
        phone=phone,
        email=email,
        tags=[t.strip() for t in str(payload.get("tags") or "").split(",") if t.strip()],
        consent={
            "email": bool(payload.get("accepts_marketing")),
            "whatsapp": bool(payload.get("accepts_marketing")),
        },
        total_spend_micros=int(float(str(payload.get("total_spent") or "0")) * 1_000_000),
        order_count=int(payload.get("orders_count") or 0),
        updated_at=_dt(payload.get("updated_at")),
        raw={"id": payload.get("id")},
    )
