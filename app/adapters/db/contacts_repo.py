"""Postgres access for contacts and verified identity.

Raw `text()` SQL through SQLAlchemy Core under `tenant_scope`, like every other
repository here — RLS is the isolation mechanism, so the tenant context is what
makes a query safe rather than a `WHERE tenant_id = …` a future edit could drop.

Three things in this file are not ordinary CRUD:

**An absent claim never overwrites a stored value.** Every contact upsert writes
`COALESCE(EXCLUDED.col, contacts.col)`. A tenant whose second token omits `phone`
must not erase the phone the first token carried; a null in a JWT means "not
asserted this time", not "cleared".

**The `jti` replay check fails closed.** `claim_jti` takes the cache fast path
*and* the durable table, and refuses on either a hit or an error. This is
deliberately the opposite of `RateLimiter`'s fail-open posture: a rate limiter
failing open costs money, a replay check failing open costs an account. Note
that `CachePort.acquire_lease` itself fails *open* on a cache error, so a lease
that claims success is confirmed with a read-back before it is believed.

**`load_verifier_candidates` returns live credentials.** It is the only method
here that does, it is named so that is readable at the call site, and it caches
nothing — a rotated secret must stop working on the next verification, and a
decrypted value held in a process is a value that ends up in a heap dump.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

from app.core.db import admin_scope, tenant_scope
from app.core.identity import (
    REJECT_REPLAY_UNAVAILABLE,
    REJECT_REPLAYED,
    IdentityRejected,
    VerifierSpec,
)
from app.core.logging import get_logger
from app.core.security import decrypt_secret
from sqlalchemy import text

logger = get_logger(__name__)

# `provider` value under which an identity signing secret is stored in
# tenant_secrets.
IDENTITY_SECRET_PROVIDER = "identity_hs256"  # noqa: S105 - a provider name, not a secret

CONTACT_COLUMNS = """
    id, tenant_id, external_id, email, phone, name, attributes, source,
    verified_at, external_user_refs, last_seen_at, created_at, updated_at
"""

# Hashed pseudonyms a contact has also browsed as. Capped so a busy shared
# browser cannot grow one row without bound.
MAX_EXTERNAL_USER_REFS = 20

# Two active verifier rows per (tenant, agent) is the rotation window.
MAX_ACTIVE_VERIFIERS = 2

MAX_CONTACTS_PAGE = 200


class ContactsRepo:
    """`contacts`, `contact_events`, `identity_verifiers`, `identity_jti_seen`,
    and the identity columns on `conversations`. No business rules, only storage."""

    # ------------------------------------------------------------- verifiers

    async def load_verifier_candidates(
        self, tenant_id: UUID, agent_id: UUID | None, encryption_key: str
    ) -> list[tuple[VerifierSpec, str]]:
        """The tenant's active verifier rows with their plaintext secrets.

        **Returns live credentials.** Agent-specific rows are preferred over the
        `agent_id IS NULL` catch-all, and within a tier the order is `created_at`
        so the verifier tries the older secret first during a rotation. At most
        `MAX_ACTIVE_VERIFIERS` are returned; a row whose secret cannot be
        decrypted is dropped rather than failing the others, because a tenant
        mid-rotation must keep verifying on the key that still works.
        """
        async with tenant_scope(tenant_id) as session:
            rows = (
                (
                    await session.execute(
                        text(
                            """
                            SELECT v.id, v.tenant_id, v.agent_id, v.alg, v.secret_ref,
                                   v.issuer, v.audience, v.max_ttl_seconds,
                                   v.attribute_allowlist, s.ciphertext
                              FROM identity_verifiers v
                              JOIN tenant_secrets s
                                ON s.tenant_id = v.tenant_id AND s.ref = v.secret_ref
                             WHERE v.agent_id = :agent_id OR v.agent_id IS NULL
                             ORDER BY (v.agent_id IS NULL), v.created_at ASC
                             LIMIT :limit
                            """
                        ),
                        {"agent_id": agent_id, "limit": MAX_ACTIVE_VERIFIERS},
                    )
                )
                .mappings()
                .all()
            )

        candidates: list[tuple[VerifierSpec, str]] = []
        for row in rows:
            try:
                secret = decrypt_secret(row["ciphertext"], encryption_key)
            except Exception:
                # A wrong ENCRYPTION_KEY and a corrupt ciphertext are the same
                # symptom. Log the ref, never the ciphertext or the exception
                # text, which in some Fernet builds carries payload fragments.
                logger.warning(
                    "identity_secret_undecryptable",
                    tenant_id=str(tenant_id),
                    ref=str(row["secret_ref"]),
                )
                continue
            allowlist = row["attribute_allowlist"]
            candidates.append(
                (
                    VerifierSpec(
                        id=row["id"],
                        tenant_id=row["tenant_id"],
                        agent_id=row["agent_id"],
                        alg=str(row["alg"]),
                        secret_ref=str(row["secret_ref"]),
                        issuer=row["issuer"],
                        audience=row["audience"],
                        max_ttl_seconds=int(row["max_ttl_seconds"]),
                        attribute_allowlist=tuple(allowlist) if allowlist else None,
                    ),
                    secret,
                )
            )
        return candidates

    async def list_verifiers(self, tenant_id: UUID) -> list[dict[str, Any]]:
        """Verifier rows for the dashboard. Never the secret, only its ref."""
        async with tenant_scope(tenant_id) as session:
            rows = (
                (
                    await session.execute(
                        text(
                            """
                            SELECT id, agent_id, alg, secret_ref, issuer, audience,
                                   max_ttl_seconds, attribute_allowlist,
                                   last_verified_at, last_rejected_at, reject_count,
                                   created_at
                              FROM identity_verifiers
                             ORDER BY created_at DESC
                            """
                        )
                    )
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    async def count_verifiers(self, tenant_id: UUID, agent_id: UUID | None) -> int:
        async with tenant_scope(tenant_id) as session:
            value = await session.scalar(
                text(
                    "SELECT count(*) FROM identity_verifiers "
                    "WHERE agent_id IS NOT DISTINCT FROM :agent_id"
                ),
                {"agent_id": agent_id},
            )
        return int(value or 0)

    async def create_verifier(
        self,
        tenant_id: UUID,
        *,
        agent_id: UUID | None,
        secret_ref: str,
        ciphertext: str,
        issuer: str | None,
        audience: str | None,
        max_ttl_seconds: int,
        attribute_allowlist: list[str] | None,
    ) -> dict[str, Any]:
        """Store the ciphertext and the row that points at it, in one transaction.

        The secret row is written first because `identity_verifiers_secret_fk`
        references `(tenant_id, ref)`; a half-written pair would leave a verifier
        that can never resolve a secret.
        """
        async with tenant_scope(tenant_id) as session:
            await session.execute(
                text(
                    """
                    INSERT INTO tenant_secrets (tenant_id, ref, provider, ciphertext)
                    VALUES (:tenant_id, :ref, :provider, :ciphertext)
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "ref": secret_ref,
                    "provider": IDENTITY_SECRET_PROVIDER,
                    "ciphertext": ciphertext,
                },
            )
            row = (
                (
                    await session.execute(
                        text(
                            """
                            INSERT INTO identity_verifiers (
                                id, tenant_id, agent_id, alg, secret_ref, issuer,
                                audience, max_ttl_seconds, attribute_allowlist
                            ) VALUES (
                                :id, :tenant_id, :agent_id, 'HS256', :secret_ref,
                                :issuer, :audience, :max_ttl_seconds,
                                CAST(:attribute_allowlist AS text[])
                            )
                            RETURNING id, agent_id, alg, secret_ref, issuer, audience,
                                      max_ttl_seconds, attribute_allowlist,
                                      last_verified_at, last_rejected_at,
                                      reject_count, created_at
                            """
                        ),
                        {
                            "id": uuid4(),
                            "tenant_id": tenant_id,
                            "agent_id": agent_id,
                            "secret_ref": secret_ref,
                            "issuer": issuer,
                            "audience": audience,
                            "max_ttl_seconds": max_ttl_seconds,
                            "attribute_allowlist": attribute_allowlist,
                        },
                    )
                )
                .mappings()
                .first()
            )
        return dict(row) if row else {}

    async def delete_verifier(self, tenant_id: UUID, verifier_id: UUID) -> str | None:
        """Drop a verifier and its secret. Returns the ref that was removed.

        The row goes first: `identity_verifiers_secret_fk` is `ON DELETE RESTRICT`,
        so deleting the secret while the row still points at it would fail.
        """
        async with tenant_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(
                            "DELETE FROM identity_verifiers WHERE id = :id "
                            "RETURNING secret_ref"
                        ),
                        {"id": verifier_id},
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            ref = str(row["secret_ref"])
            await session.execute(
                text(
                    "DELETE FROM tenant_secrets WHERE tenant_id = :tenant_id "
                    "AND ref = :ref AND provider = :provider"
                ),
                {"tenant_id": tenant_id, "ref": ref, "provider": IDENTITY_SECRET_PROVIDER},
            )
        return ref

    async def record_verification(self, tenant_id: UUID, verifier_id: UUID) -> None:
        async with tenant_scope(tenant_id) as session:
            await session.execute(
                text(
                    "UPDATE identity_verifiers SET last_verified_at = now() "
                    "WHERE id = :id"
                ),
                {"id": verifier_id},
            )

    async def record_rejection(self, tenant_id: UUID, agent_id: UUID | None) -> None:
        """Bump `reject_count` on every row that could have verified this token.

        The rejected token names no row — that is the point of rejecting before a
        claim is read — so the counter is raised on the candidate set. It drives a
        dashboard warning, not a control, so an over-count on a tenant that runs
        two rows during a rotation is the right trade against a silent zero.
        """
        async with tenant_scope(tenant_id) as session:
            await session.execute(
                text(
                    """
                    UPDATE identity_verifiers
                       SET reject_count = reject_count + 1,
                           last_rejected_at = now()
                     WHERE agent_id = :agent_id OR agent_id IS NULL
                    """
                ),
                {"agent_id": agent_id},
            )

    # ----------------------------------------------------------- replay ledger

    async def claim_jti(
        self,
        tenant_id: UUID,
        jti: str,
        *,
        ttl_seconds: int,
        cache: Any,
        ledger_seconds: int | None = None,
    ) -> None:
        """Record a token's `jti` exactly once. Raises on a replay or on doubt.

        Cache-first with a durable backstop, and **fails closed on both**. The
        cache is the fast path because session bootstrap happens about once per
        visitor per fifteen minutes; the table is what makes "not replayable" true
        across machines rather than per process.

        `ttl_seconds` is the token's remaining lifetime plus the clock skew, which
        is exactly how long a replay is possible. `ledger_seconds` is the separate
        retention horizon the sweeper works against, defaulting to the cache TTL
        when the caller does not set one.

        `CachePort.acquire_lease` returns `True` when its own backend errored —
        correct for a lock that must not deadlock a tenant, wrong here — so a
        successful lease is confirmed by reading the value back. A cache that
        cannot retain a key it just set cannot detect a replay, and an
        undetectable replay is an account takeover, so verification is refused and
        the visitor becomes anonymous.
        """
        key = f"idjti:{tenant_id}:{jti}"
        ttl = max(1, ttl_seconds)
        retention = max(ttl, ledger_seconds or 0)

        try:
            fresh = await cache.acquire_lease(key, jti, ttl)
            retained = await cache.get(key) if fresh else None
        except Exception as exc:
            logger.warning("identity_jti_cache_unavailable", tenant_id=str(tenant_id))
            raise IdentityRejected(REJECT_REPLAY_UNAVAILABLE, "cache error") from exc

        if not fresh:
            raise IdentityRejected(REJECT_REPLAYED, "seen in cache")
        if retained != jti:
            raise IdentityRejected(REJECT_REPLAY_UNAVAILABLE, "cache did not retain the jti")

        try:
            async with tenant_scope(tenant_id) as session:
                result = await session.execute(
                    text(
                        """
                        INSERT INTO identity_jti_seen (tenant_id, jti, expires_at)
                        VALUES (:tenant_id, :jti, now() + make_interval(secs => :ttl))
                        ON CONFLICT (tenant_id, jti) DO NOTHING
                        """
                    ),
                    {"tenant_id": tenant_id, "jti": jti, "ttl": retention},
                )
                inserted = int(getattr(result, "rowcount", 0) or 0)
        except Exception as exc:
            logger.warning("identity_jti_ledger_unavailable", tenant_id=str(tenant_id))
            raise IdentityRejected(REJECT_REPLAY_UNAVAILABLE, "ledger error") from exc

        if inserted == 0:
            raise IdentityRejected(REJECT_REPLAYED, "seen in ledger")

    async def sweep_jti_ledger(self, *, limit: int = 20000) -> int:
        """Delete expired replay-ledger rows, across all tenants.

        Runs under `admin_scope` like every other cross-tenant sweeper, and deletes
        by a bounded key set rather than a bare `WHERE expires_at <` so the delete
        stays off a table-wide lock while bootstraps are landing.
        """
        async with admin_scope() as session:
            result = await session.execute(
                text(
                    """
                    DELETE FROM identity_jti_seen
                     WHERE (tenant_id, jti) IN (
                        SELECT tenant_id, jti FROM identity_jti_seen
                         WHERE expires_at < now()
                         LIMIT :limit
                     )
                    """
                ),
                {"limit": limit},
            )
        return int(getattr(result, "rowcount", 0) or 0)

    # --------------------------------------------------------------- contacts

    async def upsert_by_external_id(
        self,
        tenant_id: UUID,
        *,
        external_id: str,
        source: str,
        verified: bool,
        email: str | None = None,
        phone: str | None = None,
        name: str | None = None,
        attributes: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Create or refresh one contact, keyed on `(tenant_id, external_id)`.

        `COALESCE(EXCLUDED.col, contacts.col)` on every nullable column is the rule
        from §4.8: a claim absent from this token means "not asserted this time",
        never "cleared". `attributes` merges rather than replaces for the same
        reason. `source` is only re-stamped when this pass was verified, so a
        verified contact is never downgraded by a later unverified touch.
        """
        payload = json.dumps(attributes or {})
        async with tenant_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(
                            f"""
                            INSERT INTO contacts (
                                id, tenant_id, external_id, email, phone, name,
                                attributes, source, verified_at, last_seen_at
                            ) VALUES (
                                :id, :tenant_id, :external_id, :email, :phone, :name,
                                CAST(:attributes AS jsonb), :source,
                                CASE WHEN :verified THEN now() ELSE NULL END, now()
                            )
                            ON CONFLICT (tenant_id, external_id) DO UPDATE
                               SET email = COALESCE(EXCLUDED.email, contacts.email),
                                   phone = COALESCE(EXCLUDED.phone, contacts.phone),
                                   name = COALESCE(EXCLUDED.name, contacts.name),
                                   attributes = contacts.attributes
                                                || EXCLUDED.attributes,
                                   source = CASE
                                              WHEN EXCLUDED.verified_at IS NOT NULL
                                              THEN EXCLUDED.source
                                              ELSE contacts.source
                                            END,
                                   verified_at = COALESCE(EXCLUDED.verified_at,
                                                          contacts.verified_at),
                                   last_seen_at = now(),
                                   updated_at = now()
                            RETURNING {CONTACT_COLUMNS}
                            """  # noqa: S608 - CONTACT_COLUMNS is a module constant
                        ),
                        {
                            "id": uuid4(),
                            "tenant_id": tenant_id,
                            "external_id": external_id,
                            "email": email,
                            "phone": phone,
                            "name": name,
                            "attributes": payload,
                            "source": source,
                            "verified": verified,
                        },
                    )
                )
                .mappings()
                .first()
            )
        return dict(row) if row else {}

    async def get(self, tenant_id: UUID, contact_id: UUID) -> dict[str, Any] | None:
        """One contact by id, under tenant scope.

        RLS is what makes a `contact_id` carried in a session token safe to read
        without re-checking it against the tenant: a cross-tenant id returns
        nothing rather than somebody else's row.
        """
        async with tenant_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(
                            f"SELECT {CONTACT_COLUMNS} FROM contacts WHERE id = :id"  # noqa: S608
                        ),
                        {"id": contact_id},
                    )
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    async def search(
        self,
        tenant_id: UUID,
        *,
        query: str = "",
        verified: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Contacts for the dashboard list, most recent activity first.

        The substring match is why `email` is stored in plaintext and why
        `contacts_email_trgm_idx` exists: finding a person by a fragment of their
        address is the whole reason the column is readable.
        """
        term = query.strip()
        bounded = max(1, min(limit, MAX_CONTACTS_PAGE))
        async with tenant_scope(tenant_id) as session:
            rows = (
                (
                    await session.execute(
                        text(
                            f"""
                            SELECT {CONTACT_COLUMNS} FROM contacts
                             WHERE (:term = ''
                                OR email ILIKE '%' || :term || '%'
                                OR phone ILIKE '%' || :term || '%'
                                OR name ILIKE '%' || :term || '%'
                                OR external_id ILIKE '%' || :term || '%')
                               AND (:verified IS NULL
                                OR (verified_at IS NOT NULL) = :verified)
                             ORDER BY last_seen_at DESC NULLS LAST, created_at DESC
                             LIMIT :limit OFFSET :offset
                            """  # noqa: S608 - CONTACT_COLUMNS is a module constant
                        ),
                        {
                            "term": term,
                            "verified": verified,
                            "limit": bounded,
                            "offset": max(0, offset),
                        },
                    )
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    async def count(self, tenant_id: UUID) -> int:
        async with tenant_scope(tenant_id) as session:
            value = await session.scalar(text("SELECT count(*) FROM contacts"))
        return int(value or 0)

    async def update(
        self,
        tenant_id: UUID,
        contact_id: UUID,
        *,
        name: str | None = None,
        phone: str | None = None,
        attributes: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        """Edit the three editable fields.

        `external_id` and `verified_at` are deliberately not parameters here:
        they are assertions about who someone is and how we came to believe it,
        not fields a dashboard user may retype.
        """
        async with tenant_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(
                            f"""
                            UPDATE contacts
                               SET name = COALESCE(:name, name),
                                   phone = COALESCE(:phone, phone),
                                   attributes = CASE
                                     WHEN :attributes IS NULL THEN attributes
                                     ELSE CAST(:attributes AS jsonb)
                                   END,
                                   updated_at = now()
                             WHERE id = :id
                            RETURNING {CONTACT_COLUMNS}
                            """  # noqa: S608 - CONTACT_COLUMNS is a module constant
                        ),
                        {
                            "id": contact_id,
                            "name": name,
                            "phone": phone,
                            "attributes": json.dumps(attributes)
                            if attributes is not None
                            else None,
                        },
                    )
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    async def conversations_for(
        self, tenant_id: UUID, contact_id: UUID, *, limit: int = 20
    ) -> list[dict[str, Any]]:
        """This contact's recent threads, off `conversations_contact_idx`."""
        async with tenant_scope(tenant_id) as session:
            rows = (
                (
                    await session.execute(
                        text(
                            """
                            SELECT id, channel, started_at, identity_verified
                              FROM conversations
                             WHERE contact_id = :contact_id
                             ORDER BY started_at DESC
                             LIMIT :limit
                            """
                        ),
                        {"contact_id": contact_id, "limit": max(1, min(limit, 200))},
                    )
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    async def update_verifier(
        self,
        tenant_id: UUID,
        verifier_id: UUID,
        *,
        issuer: str | None = None,
        audience: str | None = None,
        max_ttl_seconds: int | None = None,
        attribute_allowlist: list[str] | None = None,
        clear_issuer: bool = False,
        clear_audience: bool = False,
        clear_allowlist: bool = False,
    ) -> dict[str, Any] | None:
        """Edit a verifier's claim expectations.

        `alg` and `secret_ref` are not editable: the algorithm is fixed by a CHECK
        constraint and rotating a secret is a new row plus a delete, which is what
        gives the tenant a window where both keys work.
        """
        async with tenant_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(
                            """
                            UPDATE identity_verifiers
                               SET issuer = CASE WHEN :clear_issuer THEN NULL
                                                 ELSE COALESCE(:issuer, issuer) END,
                                   audience = CASE WHEN :clear_audience THEN NULL
                                                  ELSE COALESCE(:audience, audience) END,
                                   max_ttl_seconds = COALESCE(:max_ttl_seconds,
                                                              max_ttl_seconds),
                                   attribute_allowlist = CASE
                                     WHEN :clear_allowlist THEN NULL
                                     WHEN :attribute_allowlist IS NULL
                                       THEN attribute_allowlist
                                     ELSE CAST(:attribute_allowlist AS text[])
                                   END
                             WHERE id = :id
                            RETURNING id, agent_id, alg, secret_ref, issuer, audience,
                                      max_ttl_seconds, attribute_allowlist,
                                      last_verified_at, last_rejected_at,
                                      reject_count, created_at
                            """
                        ),
                        {
                            "id": verifier_id,
                            "issuer": issuer,
                            "audience": audience,
                            "max_ttl_seconds": max_ttl_seconds,
                            "attribute_allowlist": attribute_allowlist,
                            "clear_issuer": clear_issuer,
                            "clear_audience": clear_audience,
                            "clear_allowlist": clear_allowlist,
                        },
                    )
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    async def delete(self, tenant_id: UUID, contact_id: UUID) -> bool:
        """Erase a contact. `conversations.contact_id` is `ON DELETE SET NULL`, so
        the threads survive as anonymous rather than vanishing with the person."""
        async with tenant_scope(tenant_id) as session:
            result = await session.execute(
                text("DELETE FROM contacts WHERE id = :id"), {"id": contact_id}
            )
        return int(getattr(result, "rowcount", 0) or 0) > 0

    async def append_external_user_ref(
        self, tenant_id: UUID, contact_id: UUID, ref: str
    ) -> None:
        """Record a hashed pseudonym this contact also browsed as, capped.

        Insertion-ordered with the oldest dropped past the cap, so the trace a
        support agent reads is the recent history rather than an arbitrary subset.
        """
        async with tenant_scope(tenant_id) as session:
            await session.execute(
                text(
                    """
                    UPDATE contacts
                       SET external_user_refs = CASE
                             WHEN :ref = ANY(external_user_refs) THEN external_user_refs
                             ELSE (
                               array_append(external_user_refs, :ref)
                             )[greatest(
                                 1,
                                 array_length(array_append(external_user_refs, :ref), 1)
                                 - :cap + 1
                               ):array_length(
                                 array_append(external_user_refs, :ref), 1
                               )]
                           END,
                           updated_at = now()
                     WHERE id = :id
                    """
                ),
                {"id": contact_id, "ref": ref, "cap": MAX_EXTERNAL_USER_REFS},
            )

    async def record_event(
        self,
        tenant_id: UUID,
        contact_id: UUID,
        *,
        kind: str,
        conversation_id: UUID | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """One `contact_events` row. The payload never carries the raw token."""
        async with tenant_scope(tenant_id) as session:
            await session.execute(
                text(
                    """
                    INSERT INTO contact_events (
                        id, tenant_id, contact_id, conversation_id, kind, payload
                    ) VALUES (
                        :id, :tenant_id, :contact_id, :conversation_id, :kind,
                        CAST(:payload AS jsonb)
                    )
                    """
                ),
                {
                    "id": uuid4(),
                    "tenant_id": tenant_id,
                    "contact_id": contact_id,
                    "conversation_id": conversation_id,
                    "kind": kind,
                    "payload": json.dumps(payload or {}),
                },
            )

    async def list_events(
        self, tenant_id: UUID, contact_id: UUID, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        async with tenant_scope(tenant_id) as session:
            rows = (
                (
                    await session.execute(
                        text(
                            """
                            SELECT id, conversation_id, kind, payload, at
                              FROM contact_events
                             WHERE contact_id = :contact_id
                             ORDER BY at DESC
                             LIMIT :limit
                            """
                        ),
                        {"contact_id": contact_id, "limit": max(1, min(limit, 200))},
                    )
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    # ----------------------------------------------------- conversations link

    async def attach_conversation(
        self,
        tenant_id: UUID,
        conversation_id: UUID,
        *,
        contact_id: UUID | None,
        verified: bool,
    ) -> None:
        """Join a conversation to a person, and stamp whether it is verified.

        `identity_verified_at` only ever moves forward: a conversation that began
        verified stays auditable as such.
        """
        async with tenant_scope(tenant_id) as session:
            await session.execute(
                text(
                    """
                    UPDATE conversations
                       SET contact_id = COALESCE(:contact_id, contact_id),
                           identity_verified = :verified,
                           identity_verified_at = CASE
                             WHEN :verified THEN COALESCE(identity_verified_at, now())
                             ELSE identity_verified_at
                           END
                     WHERE id = :conversation_id
                    """
                ),
                {
                    "conversation_id": conversation_id,
                    "contact_id": contact_id,
                    "verified": verified,
                },
            )

    async def conversation_identity(
        self, tenant_id: UUID, conversation_id: UUID
    ) -> dict[str, Any] | None:
        """The identity columns for one conversation, or None if it is not ours."""
        async with tenant_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT contact_id, identity_verified, identity_verified_at "
                            "FROM conversations WHERE id = :id"
                        ),
                        {"id": conversation_id},
                    )
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

