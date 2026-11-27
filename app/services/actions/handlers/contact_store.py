"""Contacts, contact events, and the two side stores `collect_*` writes into.

Raw `text()` SQL through a single `tenant_scope` transaction per handler call, so
a lead capture is one atomic unit: the contact row, the event row, and the
conversation link either all land or none do. RLS is the isolation boundary —
every statement here is written as if `tenant_id` were untrusted anyway, because
a missing predicate in one place is how a cross-tenant read gets introduced.

This module exists because `ActionsRepo` deliberately owns only the actions
tables, and the contacts service that will own these statements belongs to the
identity stage. Nothing here holds business rules: the verified-contact
protection and the field policy live in the handlers.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.actions.handlers.base import json_dump

# Mirrors the CONTACT_MAX_ATTRIBUTES / CONTACT_ATTRIBUTE_MAX_LENGTH settings
# defaults. Both are also the shape the migration's comment documents: flat,
# string values only, because these get interpolated into action URL paths.
MAX_ATTRIBUTES = 25
ATTRIBUTE_MAX_LENGTH = 200
ATTRIBUTE_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,30}$")

CONTACT_COLUMNS = (
    "id, external_id, email, phone, name, attributes, source, verified_at, last_seen_at"
)

# Three complete statements rather than one with an interpolated predicate: a SQL
# string assembled from parts is the shape a scanner cannot clear, and there are
# only three lookup keys. A verified row wins the ordering so an unverified
# duplicate never shadows the real contact.
_FIND_BY_EXTERNAL_ID = (
    "SELECT id, external_id, email, phone, name, attributes, source, verified_at, "
    "last_seen_at FROM contacts "
    "WHERE tenant_id = :tenant_id AND external_id = :external_id "
    "ORDER BY verified_at DESC NULLS LAST, created_at ASC LIMIT 1"
)
_FIND_BY_EMAIL = (
    "SELECT id, external_id, email, phone, name, attributes, source, verified_at, "
    "last_seen_at FROM contacts "
    "WHERE tenant_id = :tenant_id AND lower(email) = lower(:email) "
    "ORDER BY verified_at DESC NULLS LAST, created_at ASC LIMIT 1"
)
_FIND_BY_PHONE = (
    "SELECT id, external_id, email, phone, name, attributes, source, verified_at, "
    "last_seen_at FROM contacts "
    "WHERE tenant_id = :tenant_id AND phone = :phone "
    "ORDER BY verified_at DESC NULLS LAST, created_at ASC LIMIT 1"
)


def attribute_key(raw: str) -> str | None:
    """A tenant-declared field name, coerced to the column's key shape.

    `Order Number` becomes `order_number`; anything that still fails the pattern
    after coercion is dropped rather than written, because the check is a CHECK
    constraint in spirit and a malformed key would poison every later
    interpolation of that contact.
    """
    candidate = re.sub(r"[^a-z0-9_]+", "_", raw.strip().lower()).strip("_")
    if not candidate or not candidate[0].isalpha():
        candidate = f"f_{candidate}" if candidate else ""
    candidate = candidate[:31]
    return candidate if candidate and ATTRIBUTE_KEY_RE.match(candidate) else None


def sanitise_attributes(
    values: Mapping[str, Any],
    *,
    max_keys: int = MAX_ATTRIBUTES,
    max_length: int = ATTRIBUTE_MAX_LENGTH,
) -> dict[str, str]:
    """Flatten to string values, drop unusable keys, and cap the count."""
    clean: dict[str, str] = {}
    for raw_key, raw_value in values.items():
        if len(clean) >= max_keys:
            break
        key = attribute_key(str(raw_key))
        if key is None or raw_value is None:
            continue
        text_value = raw_value if isinstance(raw_value, str) else json_dump(raw_value)
        clean[key] = text_value.strip()[:max_length]
    return clean


async def load_conversation_contact(
    session: AsyncSession, tenant_id: UUID, conversation_id: UUID
) -> dict[str, Any] | None:
    """The contact already attached to this conversation, if any."""
    row = (
        (
            await session.execute(
                text(
                    "SELECT c.id, c.external_id, c.email, c.phone, c.name, "
                    "c.attributes, c.source, c.verified_at "
                    "FROM conversations v "
                    "JOIN contacts c ON c.id = v.contact_id "
                    "WHERE v.id = :conversation_id AND v.tenant_id = :tenant_id "
                    "AND c.tenant_id = :tenant_id"
                ),
                {"conversation_id": conversation_id, "tenant_id": tenant_id},
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row is not None else None


async def find_contact(
    session: AsyncSession,
    tenant_id: UUID,
    *,
    external_id: str | None = None,
    email: str | None = None,
    phone: str | None = None,
) -> dict[str, Any] | None:
    """Match on the strongest available key, in the order §2.2 fixes.

    `external_id` first because it is the tenant's own identifier and the only
    one an unverified lead cannot claim; then email, then phone. A verified row
    wins a tie, so an unverified duplicate never shadows the real contact.
    """
    if external_id:
        query = _FIND_BY_EXTERNAL_ID
    elif email:
        query = _FIND_BY_EMAIL
    elif phone:
        query = _FIND_BY_PHONE
    else:
        return None
    row = (
        (
            await session.execute(
                text(query),
                {
                    "tenant_id": tenant_id,
                    "external_id": external_id,
                    "email": email,
                    "phone": phone,
                },
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row is not None else None


async def insert_contact(
    session: AsyncSession,
    tenant_id: UUID,
    *,
    external_id: str | None,
    email: str | None,
    phone: str | None,
    name: str | None,
    source: str,
    attributes: Mapping[str, str],
) -> UUID:
    """A new unverified contact. `verified_at` stays NULL: collected is not proven."""
    contact_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO contacts ("
            "id, tenant_id, external_id, email, phone, name, attributes, source, "
            "last_seen_at) VALUES ("
            ":id, :tenant_id, :external_id, :email, :phone, :name, "
            "CAST(:attributes AS jsonb), :source, now())"
        ),
        {
            "id": contact_id,
            "tenant_id": tenant_id,
            "external_id": external_id,
            "email": email,
            "phone": phone,
            "name": name,
            "attributes": json_dump(dict(attributes)),
            "source": source,
        },
    )
    return contact_id


async def fill_unverified_contact(
    session: AsyncSession,
    tenant_id: UUID,
    contact_id: UUID,
    *,
    email: str | None,
    phone: str | None,
    name: str | None,
) -> None:
    """COALESCE-merge onto an unverified row. Only the caller decides it is safe."""
    await session.execute(
        text(
            "UPDATE contacts SET "
            "email = COALESCE(:email, email), "
            "phone = COALESCE(:phone, phone), "
            "name = COALESCE(:name, name), "
            "last_seen_at = now(), updated_at = now() "
            "WHERE id = :id AND tenant_id = :tenant_id AND verified_at IS NULL"
        ),
        {
            "id": contact_id,
            "tenant_id": tenant_id,
            "email": email,
            "phone": phone,
            "name": name,
        },
    )


async def set_attributes(
    session: AsyncSession,
    tenant_id: UUID,
    contact_id: UUID,
    attributes: Mapping[str, str],
) -> None:
    """Write the whole attributes object.

    The merge is computed in Python from the row read in this same transaction
    rather than with a `||` in SQL, because the 25-key ceiling has to be applied
    to the *result*, and a jsonb concat cannot count.
    """
    await session.execute(
        text(
            "UPDATE contacts SET attributes = CAST(:attributes AS jsonb), "
            "last_seen_at = now(), updated_at = now() "
            "WHERE id = :id AND tenant_id = :tenant_id"
        ),
        {
            "id": contact_id,
            "tenant_id": tenant_id,
            "attributes": json_dump(dict(attributes)),
        },
    )


async def record_event(
    session: AsyncSession,
    tenant_id: UUID,
    *,
    contact_id: UUID,
    conversation_id: UUID | None,
    kind: str,
    payload: Mapping[str, Any],
) -> None:
    """One `contact_events` row. `kind` must be in the table's CHECK list."""
    await session.execute(
        text(
            "INSERT INTO contact_events ("
            "id, tenant_id, contact_id, conversation_id, kind, payload) VALUES ("
            ":id, :tenant_id, :contact_id, :conversation_id, :kind, "
            "CAST(:payload AS jsonb))"
        ),
        {
            "id": uuid4(),
            "tenant_id": tenant_id,
            "contact_id": contact_id,
            "conversation_id": conversation_id,
            "kind": kind,
            "payload": json_dump(dict(payload)),
        },
    )


async def attach_contact(
    session: AsyncSession,
    tenant_id: UUID,
    conversation_id: UUID,
    contact_id: UUID,
) -> bool:
    """Set `conversations.contact_id` only when it was null.

    Never a reassignment: a conversation that already resolved to a contact —
    especially a verified one — must not be re-pointed by something the visitor
    typed mid-thread.
    """
    result = await session.execute(
        text(
            "UPDATE conversations SET contact_id = :contact_id "
            "WHERE id = :conversation_id AND tenant_id = :tenant_id "
            "AND contact_id IS NULL"
        ),
        {
            "contact_id": contact_id,
            "conversation_id": conversation_id,
            "tenant_id": tenant_id,
        },
    )
    return int(getattr(result, "rowcount", 0) or 0) > 0


async def store_conversation_collected(
    session: AsyncSession,
    tenant_id: UUID,
    conversation_id: UUID,
    values: Mapping[str, Any],
) -> None:
    """Merge into `conversations.metadata.collected` for anonymous visitors."""
    await session.execute(
        text(
            "UPDATE conversations SET metadata = jsonb_set("
            "COALESCE(metadata, '{}'::jsonb), '{collected}', "
            "COALESCE(metadata -> 'collected', '{}'::jsonb) || CAST(:values AS jsonb), "
            "true) "
            "WHERE id = :conversation_id AND tenant_id = :tenant_id"
        ),
        {
            "conversation_id": conversation_id,
            "tenant_id": tenant_id,
            "values": json_dump(dict(values)),
        },
    )


async def store_run_variables(
    session: AsyncSession,
    tenant_id: UUID,
    run_id: UUID,
    values: Mapping[str, Any],
) -> None:
    """Merge into `procedure_runs.variables` so a later branch guard can read it.

    Top-level keys, not nested under a source name: a guard references a field by
    the name the tenant declared, and burying it one level down would make every
    template in the procedure gallery wrong.
    """
    await session.execute(
        text(
            "UPDATE procedure_runs SET variables = "
            "COALESCE(variables, '{}'::jsonb) || CAST(:values AS jsonb) "
            "WHERE id = :run_id AND tenant_id = :tenant_id"
        ),
        {"run_id": run_id, "tenant_id": tenant_id, "values": json_dump(dict(values))},
    )
