"""Store-customer segments: pure functions, computed at campaign time.

Never materialised. The campaign planner calls these over the tenant's
``growth_customers`` + ``growth_orders`` rows when it drafts, freezes the
resulting member list into ``growth_campaigns.segment``, and the send reads
that frozen list. A segment that lived in its own table would silently change
size between the owner's approval and the send — which is exactly what the
frozen ``member_ids`` prevents.

No I/O, no imports outside the standard library — the same posture as
``growth.py`` and ``schedule.py``. Every function takes plain rows (mappings)
and a ``now``, and returns the matching customer rows in a deterministic
order (by customer id), so the same input always drafts the same audience.

Row shapes (only the fields read here):
  customer: {"id", "last_order_at", "first_order_at", "total_spend_micros",
             "order_count", "tags", "consent", "phone", "email"}
  order:    {"customer_id", "total_micros", "ordered_at", "items", "payment"}
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

# Segment keys. Closed set: the campaign composer offers exactly these, and an
# unknown key is refused rather than guessed at — drafting to the wrong
# audience is a money action aimed at the wrong people.
SEGMENT_KEYS: tuple[str, ...] = (
    "new",
    "repeat",
    "high_value",
    "at_risk",
    "lost",
    "by_category",
    "by_channel_consent",
    "dealers_by_territory",
)

CUSTOMER_CHANNELS: tuple[str, ...] = ("whatsapp", "email")


def _as_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return []


def _consent_allows(customer: Mapping[str, Any], channel: str) -> bool:
    """Does this customer's stored consent record allow this channel?"""
    consent = customer.get("consent")
    if isinstance(consent, dict):
        raw = consent.get(channel)
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            return raw.strip().lower() in ("true", "1", "yes", "opt_in", "opted_in")
    # a recorded opt-in in growth_consents is checked at send time by the
    # sender; here an absent record means "no evidence either way", which the
    # channel picker treats as email-ok (unsubscribe link carries the right)
    # and whatsapp-not-ok (Meta requires prior opt-in). This mirrors the
    # senders' gates without duplicating them.
    if channel == "email":
        return bool(customer.get("email"))
    if channel == "whatsapp":
        return bool(customer.get("phone"))
    return False


def _ordered(customers: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Deterministic audience order: by customer id, always."""
    return sorted((dict(c) for c in customers), key=lambda c: str(c.get("id") or ""))


def new(
    customers: Sequence[Mapping[str, Any]],
    orders: Sequence[Mapping[str, Any]],
    now: datetime,
    *,
    within_days: int = 30,
) -> list[dict[str, Any]]:
    """First order within the last ``within_days`` days."""
    _ = orders
    cutoff = now.timestamp() - within_days * 86400
    out = []
    for c in customers:
        first = _ts(c.get("first_order_at"))
        if first is not None and first >= cutoff:
            out.append(c)
    return _ordered(out)


def repeat(
    customers: Sequence[Mapping[str, Any]],
    orders: Sequence[Mapping[str, Any]],
    now: datetime,
) -> list[dict[str, Any]]:
    """More than one order, ever."""
    _ = orders
    _ = now
    return _ordered([c for c in customers if int(c.get("order_count") or 0) > 1])


def high_value(
    customers: Sequence[Mapping[str, Any]],
    orders: Sequence[Mapping[str, Any]],
    now: datetime,
    *,
    top_pct: float = 20.0,
) -> list[dict[str, Any]]:
    """Top ``top_pct`` percent by total spend. At least one customer when non-empty."""
    _ = orders
    _ = now
    ranked = sorted(
        [c for c in customers if int(c.get("total_spend_micros") or 0) > 0],
        key=lambda c: (-int(c.get("total_spend_micros") or 0), str(c.get("id") or "")),
    )
    if not ranked:
        return []
    take = max(1, int(len(ranked) * top_pct / 100.0 + 0.9999))
    return _ordered(ranked[:take])


def at_risk(
    customers: Sequence[Mapping[str, Any]],
    orders: Sequence[Mapping[str, Any]],
    now: datetime,
    *,
    min_days: int = 60,
    max_days: int = 90,
) -> list[dict[str, Any]]:
    """Bought more than once, last order ``min_days``-``max_days`` ago."""
    _ = orders
    low = now.timestamp() - max_days * 86400
    high = now.timestamp() - min_days * 86400
    out = []
    for c in customers:
        if int(c.get("order_count") or 0) <= 1:
            continue
        last = _ts(c.get("last_order_at"))
        if last is not None and low <= last <= high:
            out.append(c)
    return _ordered(out)


def lost(
    customers: Sequence[Mapping[str, Any]],
    orders: Sequence[Mapping[str, Any]],
    now: datetime,
    *,
    days: int = 180,
) -> list[dict[str, Any]]:
    """No order in ``days`` days (and at least one order ever)."""
    _ = orders
    cutoff = now.timestamp() - days * 86400
    out = []
    for c in customers:
        last = _ts(c.get("last_order_at"))
        if last is not None and last < cutoff:
            out.append(c)
    return _ordered(out)


def by_category(
    customers: Sequence[Mapping[str, Any]],
    orders: Sequence[Mapping[str, Any]],
    now: datetime,
    *,
    category: str = "",
) -> list[dict[str, Any]]:
    """Customers whose orders include an item in ``category``.

    The category is matched against each order item's ``category``/``product_type``/
    ``tags`` fields (lower-cased, substring). Orders are joined to customers by
    ``customer_id``; customers with no matching order are excluded.
    """
    _ = now
    want = category.strip().lower()
    if not want:
        return []
    matching: set[str] = set()
    for order in orders:
        items = _items(order.get("items"))
        for item in items:
            hay = " ".join(
                str(item.get(k) or "") for k in ("category", "product_type", "title", "name")
            ).lower()
            tags = _as_list(item.get("tags"))
            hay += " " + " ".join(tags).lower()
            if want in hay:
                cid = str(order.get("customer_id") or "")
                if cid:
                    matching.add(cid)
                break
    by_id = {str(c.get("id") or ""): c for c in customers}
    return _ordered([by_id[cid] for cid in matching if cid in by_id])


def by_channel_consent(
    customers: Sequence[Mapping[str, Any]],
    orders: Sequence[Mapping[str, Any]],
    now: datetime,
    *,
    channel: str = "whatsapp",
) -> list[dict[str, Any]]:
    """Customers reachable on ``channel`` per their stored consent record."""
    _ = orders
    _ = now
    if channel not in CUSTOMER_CHANNELS:
        return []
    return _ordered([c for c in customers if _consent_allows(c, channel)])


def dealers_by_territory(
    customers: Sequence[Mapping[str, Any]],
    orders: Sequence[Mapping[str, Any]],
    now: datetime,
    *,
    territory: str = "",
) -> list[dict[str, Any]]:
    """Customers tagged ``dealer*`` whose tags mention ``territory``.

    A dealer profile is a B2B tenant selling through dealers: tags like
    ["dealer", "territory:north"] or ["dealer-north"]. Matching is
    lower-cased substring on both halves, so "north" matches either spelling.
    """
    _ = orders
    _ = now
    want = territory.strip().lower()
    if not want:
        return []
    out = []
    for customer in customers:
        tags = [t.lower() for t in _as_list(customer.get("tags"))]
        if not any("dealer" in t for t in tags):
            continue
        if any(want in t for t in tags):
            out.append(customer)
    return _ordered(out)


def categories(orders: Sequence[Mapping[str, Any]], limit: int = 12) -> list[str]:
    """The category values actually present in this tenant's order items.

    `by_category` needs a value to mean anything, so the panel cannot show a
    count for it without knowing which values exist. Enumerating them here —
    pure, from the same rows the segment reads — is what lets the count be a
    real number per category instead of a zero for the unparameterised key.

    Ranked by how many orders carry each value, so a shop with a long tail
    shows the categories worth a campaign first.
    """
    tally: dict[str, int] = {}
    for order in orders:
        seen: set[str] = set()
        for item in _items(order.get("items")):
            for field in ("category", "product_type"):
                value = str(item.get(field) or "").strip()
                if value and value.lower() not in seen:
                    seen.add(value.lower())
                    tally[value] = tally.get(value, 0) + 1
    ranked = sorted(tally.items(), key=lambda pair: (-pair[1], pair[0]))
    return [value for value, _ in ranked[:limit]]


def territories(customers: Sequence[Mapping[str, Any]], limit: int = 12) -> list[str]:
    """The territory values present on this tenant's dealer tags.

    Reads both shapes `dealers_by_territory` matches: an explicit
    ``territory:north`` tag, and the compound ``dealer-north`` form. Same
    reason as `categories` — a parameterised segment with no known values
    cannot be counted, only guessed at.
    """
    tally: dict[str, int] = {}
    for customer in customers:
        tags = [t.strip().lower() for t in _as_list(customer.get("tags"))]
        if not any("dealer" in tag for tag in tags):
            continue
        for tag in tags:
            value = ""
            if tag.startswith("territory:"):
                value = tag.split(":", 1)[1]
            elif tag.startswith("dealer-") and len(tag) > len("dealer-"):
                value = tag[len("dealer-") :]
            value = value.strip()
            if value:
                tally[value] = tally.get(value, 0) + 1
    ranked = sorted(tally.items(), key=lambda pair: (-pair[1], pair[0]))
    return [value for value, _ in ranked[:limit]]


def compute(
    key: str,
    customers: Sequence[Mapping[str, Any]],
    orders: Sequence[Mapping[str, Any]],
    now: datetime,
    params: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """One entry point the planner calls. Unknown keys raise, never guess."""
    fn_map = {
        "new": new,
        "repeat": repeat,
        "high_value": high_value,
        "at_risk": at_risk,
        "lost": lost,
        "by_category": by_category,
        "by_channel_consent": by_channel_consent,
        "dealers_by_territory": dealers_by_territory,
    }
    fn = fn_map.get(key)
    if fn is None:
        raise ValueError(f"unknown segment {key!r}; expected one of {list(fn_map)}")
    clean: dict[str, Any] = dict(params or {})
    if key == "new":
        return new(customers, orders, now, within_days=int(clean.get("within_days", 30)))
    if key == "high_value":
        return high_value(customers, orders, now, top_pct=float(clean.get("top_pct", 20.0)))
    if key == "at_risk":
        return at_risk(
            customers,
            orders,
            now,
            min_days=int(clean.get("min_days", 60)),
            max_days=int(clean.get("max_days", 90)),
        )
    if key == "lost":
        return lost(customers, orders, now, days=int(clean.get("days", 180)))
    if key == "by_category":
        return by_category(customers, orders, now, category=str(clean.get("category", "")))
    if key == "by_channel_consent":
        return by_channel_consent(
            customers, orders, now, channel=str(clean.get("channel", "whatsapp"))
        )
    if key == "dealers_by_territory":
        return dealers_by_territory(
            customers, orders, now, territory=str(clean.get("territory", ""))
        )
    return repeat(customers, orders, now)


def channel_for(
    customer: Mapping[str, Any],
    order: Sequence[str] | None = None,
) -> str:
    """Cheapest consented channel for one customer, in tenant preference order.

    WhatsApp first only when the stored record allows it (high intent, priced
    per conversation); otherwise email (detail, near-free). A customer with
    neither lands on "" — the planner drops them rather than blind-sending.
    """
    for channel in order or ("whatsapp", "email"):
        if channel not in CUSTOMER_CHANNELS:
            continue
        if _consent_allows(customer, channel):
            if channel == "whatsapp" and not customer.get("phone"):
                continue
            if channel == "email" and not customer.get("email"):
                continue
            return channel
    return ""


# ------------------------------------------------------------------ private


def _ts(value: Any) -> float | None:
    """Epoch seconds for a datetime, ISO string, or epoch number. None when absent."""
    if value is None:
        return None
    if isinstance(value, datetime):
        ts = value.timestamp()
        return ts
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _items(raw: Any) -> list[dict[str, Any]]:
    """Order line items as a list, whatever envelope the provider used."""
    if isinstance(raw, dict):
        for key in ("line_items", "items", "products"):
            nested = raw.get(key)
            if isinstance(nested, list):
                return [i for i in nested if isinstance(i, dict)]
        return [raw] if raw else []
    if isinstance(raw, list):
        return [i for i in raw if isinstance(i, dict)]
    return []


# Public names for the two parsers above. The insights watcher buckets the same
# orders into time windows and counts the same line items; a second definition
# of "when was this ordered" or "what is a line item" would drift from this one
# the first time a store provider changed its envelope shape.
order_timestamp = _ts
order_items = _items
