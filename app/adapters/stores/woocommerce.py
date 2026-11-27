"""WooCommerce adapter: per-store REST keys, webhooks, coupons.

Auth is a consumer key/secret pair per store (HTTP Basic on every call). Both
halves live together in ``tenant_secrets`` under a per-store ref as one JSON
document ``{"key": ..., "secret": ...}`` — one ref, one rotation — and the
``growth_integrations`` row carries only the store URL (``external_ref``).

Sync pages ``/wp-json/wc/v3/customers`` and ``/orders`` in ``modified``-ascending
order behind an ``after`` timestamp: the bookmark is the newest ``modified``
seen, persisted on the integration row's ``health``. Ascending matters — with
newest-first paging a new order shifts every page and rows are skipped; with
oldest-first the window only ever grows. Webhooks arrive via the unified
ingress, verified here with the consumer secret.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any

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

PROVIDER = "woocommerce"
PAGE_SIZE = 100

# Topics this adapter understands. Order topics fold money in, customer topics
# fold buyers in; everything else (products, coupons) is acknowledged and
# ignored before any work is enqueued.
ORDER_TOPICS: frozenset[str] = frozenset(
    {"order.created", "order.updated", "order.deleted"}
)
CUSTOMER_TOPICS: frozenset[str] = frozenset(
    {"customer.created", "customer.updated", "customer.deleted"}
)

# What we subscribe to at connect time: exactly the topics above, in a stable
# order so a registration run and its ``health`` record read the same each time.
REGISTER_TOPICS: tuple[str, ...] = tuple(sorted(ORDER_TOPICS | CUSTOMER_TOPICS))


def verify_webhook(raw: bytes, signature: str, consumer_secret: str) -> bool:
    """WooCommerce's ``X-WC-Webhook-Signature`` (base64 HMAC-SHA256), constant time."""
    if not signature or not consumer_secret:
        return False
    try:
        supplied = base64.b64decode(signature.strip())
    except Exception:
        return False
    expected = hmac.new(consumer_secret.encode("utf-8"), raw, hashlib.sha256).digest()
    return hmac.compare_digest(expected, supplied)


def pack_secret(consumer_key: str, consumer_secret: str) -> str:
    """One JSON document holding both halves of the REST keypair."""
    return json.dumps({"key": consumer_key, "secret": consumer_secret})


def unpack_secret(packed: str) -> tuple[str, str]:
    """The keypair back out. Empty halves mean "not connected", never an error."""
    try:
        body = json.loads(packed or "")
    except ValueError:
        return "", ""
    if not isinstance(body, dict):
        return "", ""
    return str(body.get("key") or ""), str(body.get("secret") or "")


class WooCommerceStore:
    """REST-backed ``StoreProvider`` bound to one store's keypair."""

    def __init__(
        self,
        store_url: str,
        consumer_key: str,
        consumer_secret: str,
        *,
        timeout: float = 30.0,
    ) -> None:
        if not store_url or not consumer_key or not consumer_secret:
            raise ValueError("store_url, consumer_key and consumer_secret are required")
        self._base = _base_url(store_url)
        self._auth = (consumer_key, consumer_secret)
        self._client = httpx.AsyncClient(
            base_url=self._base,
            timeout=httpx.Timeout(timeout, connect=min(timeout, 10.0)),
            auth=self._auth,
        )

    @property
    def name(self) -> str:
        return PROVIDER

    async def sync_customers(
        self, *, cursor: str = "", limit: int = PAGE_SIZE
    ) -> CustomerPage:
        """Buyers modified since the cursor, oldest first (see module docstring
        for why ascending is the only correct direction here)."""
        since, page = _unpack(cursor)
        per_page = min(max(limit, 1), 100)
        params: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
            "orderby": "modified",
            "order": "asc",
        }
        if since:
            params["modified_after"] = since
        try:
            rows = await self._get("/wp-json/wc/v3/customers", params)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ProviderError(PROVIDER, "WooCommerce store is unreachable") from exc
        customers = [c for c in (_customer(r) for r in rows) if c is not None]
        if len(rows) < per_page:
            # under-full page ends this window; resume at the overlap so rows
            # modified during the run are re-scanned rather than missed
            return CustomerPage(customers=customers, next_cursor=_pack(_overlap_iso(), 1))
        return CustomerPage(customers=customers, next_cursor=_pack(since, page + 1))

    async def sync_orders(
        self, *, cursor: str = "", limit: int = PAGE_SIZE
    ) -> OrderPage:
        """Orders modified since the cursor, oldest first. Same bookmark
        discipline as ``sync_customers``, tracked separately."""
        since, page = _unpack(cursor)
        per_page = min(max(limit, 1), 100)
        params: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
            "orderby": "modified",
            "order": "asc",
        }
        if since:
            params["modified_after"] = since
        try:
            rows = await self._get("/wp-json/wc/v3/orders", params)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ProviderError(PROVIDER, "WooCommerce store is unreachable") from exc
        orders = [o for o in (_order(r) for r in rows) if o is not None]
        if len(rows) < per_page:
            return OrderPage(orders=orders, next_cursor=_pack(_overlap_iso(), 1))
        return OrderPage(orders=orders, next_cursor=_pack(since, page + 1))

    async def create_discount(self, *, code: str, percent: int, name: str = "") -> DiscountReceipt:
        """A percentage coupon via ``POST /coupons``."""
        if not code or not 1 <= percent <= 100:
            raise ValidationError("discount needs a code and 1-100 percent")
        try:
            body = await self._post(
                "/wp-json/wc/v3/coupons",
                {
                    "code": code,
                    "discount_type": "percent",
                    "amount": str(percent),
                    "description": name or f"Campaign {code}",
                    "individual_use": True,
                },
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ProviderError(PROVIDER, "WooCommerce store is unreachable") from exc
        return DiscountReceipt(
            code=code, external_id=str(body.get("id") or ""), detail={"percent": percent}
        )

    async def register_webhooks(
        self, delivery_url: str, topics: tuple[str, ...] = REGISTER_TOPICS
    ) -> dict[str, Any]:
        """Subscribe this store's topics to our ingress. Without it none arrive.

        WooCommerce delivers a webhook only where a ``wc/v3/webhooks`` resource
        says to, and nothing else in this codebase creates one — so this is the
        whole reason a store event ever reaches ``/webhooks/ingress/woocommerce``.

        ``secret`` is the consumer secret on purpose: it is what WooCommerce
        signs ``X-WC-Webhook-Signature`` with, and it is the secret the ingress
        already reaches for when it verifies a Woo body. Handing a different one
        would make every delivery fail its HMAC check.

        Existing subscriptions are read first and matching topics skipped, because
        WooCommerce will happily create a second identical webhook and then
        deliver every order twice. A reconnect therefore adds nothing.
        """
        try:
            existing = await self._get(
                "/wp-json/wc/v3/webhooks", {"per_page": 100, "status": "active"}
            )
        except ProviderError:
            # a store that cannot list webhooks can still usually create them;
            # the duplicate risk is worth less than the feature not working
            existing = []
        already = {
            str(row.get("topic") or "")
            for row in existing
            if str(row.get("delivery_url") or "") == delivery_url
        }
        registered: list[str] = []
        failed: dict[str, str] = {}
        for topic in topics:
            if topic in already:
                registered.append(topic)
                continue
            try:
                await self._post(
                    "/wp-json/wc/v3/webhooks",
                    {
                        "name": f"Growth {topic}",
                        "topic": topic,
                        "delivery_url": delivery_url,
                        "secret": self._auth[1],
                        "status": "active",
                    },
                    what="the webhook",
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                raise ProviderError(PROVIDER, "WooCommerce store is unreachable") from exc
            except ProviderError as exc:
                failed[topic] = str(exc)[:200]
                continue
            registered.append(topic)
        return {"registered": registered, "failed": failed}

    async def _get(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        resp = await self._client.get(path, params=params)
        if resp.status_code == 401:
            raise ProviderError(PROVIDER, "WooCommerce rejected the REST keys (401)")
        if resp.status_code >= 400:
            raise ProviderError(PROVIDER, f"WooCommerce HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise ProviderError(PROVIDER, "WooCommerce returned invalid JSON") from exc
        return body if isinstance(body, list) else []

    async def _post(
        self, path: str, payload: dict[str, Any], *, what: str = "the coupon"
    ) -> dict[str, Any]:
        resp = await self._client.post(path, json=payload)
        if resp.status_code == 401:
            raise ProviderError(PROVIDER, "WooCommerce rejected the REST keys (401)")
        if resp.status_code >= 400:
            detail = resp.text[:200]
            raise ProviderError(PROVIDER, f"WooCommerce refused {what}: {detail}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise ProviderError(PROVIDER, "WooCommerce returned invalid JSON") from exc
        return body if isinstance(body, dict) else {}

    async def close(self) -> None:
        await self._client.aclose()


def build_woocommerce(
    store_url: str, consumer_key: str, consumer_secret: str, *, timeout: float = 30.0
) -> WooCommerceStore | None:
    """None when this store has no keys — the graceful-off posture."""
    if not store_url or not consumer_key or not consumer_secret:
        return None
    return WooCommerceStore(store_url, consumer_key, consumer_secret, timeout=timeout)


# ------------------------------------------------------------------ private


def _base_url(raw: str) -> str:
    value = raw.strip().rstrip("/")
    if not value.startswith("http"):
        value = f"https://{value}"
    parts = value.split("/")
    host = parts[2] if len(parts) > 2 else ""
    if not value.startswith(("https://", "http://")) or not host or "@" in host:
        # userinfo in a store URL would smuggle credentials into every API
        # call's Authorization path; refuse it rather than parse around it
        raise ValueError("store_url must be a plain http(s) URL without credentials")
    return value


def _unpack(cursor: str) -> tuple[str, int]:
    """A ``"<since ISO>|<page>"`` bookmark back apart."""
    since, _, page = (cursor or "").partition("|")
    try:
        return since, max(1, int(page or "1"))
    except ValueError:
        return since, 1


def _pack(since: str, page: int) -> str:
    return f"{since}|{page}"


def _overlap_iso() -> str:
    """Where an exhausted side resumes: ten minutes before now.

    Same reasoning as the Shopify adapter's overlap — a row modified during
    the run must be re-scanned rather than missed, and the upserts make the
    repeat free.
    """
    from datetime import timedelta

    return (datetime.now(UTC) - timedelta(minutes=10)).isoformat()


def _customer(row: dict[str, Any]) -> StoreCustomer | None:
    email = str(row.get("email") or "").strip()
    billing = row.get("billing") or {}
    phone = str(row.get("phone") or billing.get("phone") or "").strip()
    if not email and not phone:
        return None
    name = f"{row.get('first_name', '')} {row.get('last_name', '')}".strip()
    try:
        spent = float(str(row.get("total_spent") or "0"))
    except ValueError:
        spent = 0.0
    return StoreCustomer(
        external_id=str(row.get("id") or ""),
        name=name,
        phone=phone,
        email=email,
        tags=[],
        consent={"email": True, "whatsapp": bool(phone)},
        total_spend_micros=int(spent * 1_000_000),
        order_count=int(row.get("orders_count") or 0),
        first_order_at=_dt(row.get("date_created")),
        last_order_at=_dt(row.get("date_modified")),
        updated_at=_dt(row.get("date_modified")),
        raw={"id": row.get("id")},
    )


def _order(row: dict[str, Any]) -> StoreOrder | None:
    external = str(row.get("id") or row.get("number") or "")
    if not external:
        return None
    try:
        total = float(str(row.get("total") or "0"))
    except ValueError:
        total = 0.0
    billing = row.get("billing") or {}
    method = str(row.get("payment_method") or "").lower()
    payment = "cod" if method in ("cod", "cash") else "prepaid"
    items: dict[str, Any] = {
        "line_items": row.get("line_items") or [],
        "discount_codes": [str(c.get("code", c)) for c in (row.get("coupon_lines") or [])],
        "name": row.get("number", ""),
    }
    return StoreOrder(
        external_id=external,
        customer_external_id=str(row.get("customer_id") or ""),
        customer_email=str(billing.get("email") or ""),
        customer_phone=str(billing.get("phone") or ""),
        total_micros=int(total * 1_000_000),
        payment=payment,
        status=str(row.get("status") or ""),
        items=items,
        ordered_at=_dt(row.get("date_created")),
        updated_at=_dt(row.get("date_modified")),
        raw={"id": row.get("id")},
    )


def _dt(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def order_from_webhook(payload: dict[str, Any]) -> StoreOrder | None:
    """A WooCommerce order webhook body into the neutral order.

    The webhook body is the order resource itself, so this is the same parse
    as the sync path — one parser, not two that can disagree.
    """
    if not isinstance(payload, dict) or not payload.get("id"):
        return None
    return _order(payload)


def customer_from_webhook(payload: dict[str, Any]) -> StoreCustomer | None:
    """A WooCommerce customer webhook body into the neutral customer.

    Same shape as the REST customer the sync path reads, so the same parser.
    """
    if not isinstance(payload, dict) or not payload.get("id"):
        return None
    return _customer(payload)
