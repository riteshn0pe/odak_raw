"""Seed a demo tenant for local work.

Creates a tenant, an owner user, a default agent, and one API key, then prints
the key once — it is stored hashed and cannot be recovered, exactly as in
production, so this also demonstrates the real key lifecycle.

The demo tenant is also onboarded onto the growth platform so the swarm has
something to do on its first tick: the default growth_settings row is written
through ProfileService.put (the same path PUT /v1/growth/profile takes), and one
small demo website source is created with the crawl review gate on — the
frontier parks for review, which is the demo's "sources are gated" story.

Run from the repo root:  python -m scripts.seed
"""

from __future__ import annotations

import asyncio
import os
import sys
from uuid import uuid4

from app.core.config import get_settings
from app.core.db import admin_scope, dispose_engine, init_engine
from app.core.security import generate_api_key, hash_password
from sqlalchemy import text

DEMO_EMAIL = "owner@demo.test"
DEMO_PASSWORD = "demo-password-change-me"
DEMO_SLUG = "demo"
# small, stable, one page — the review list is short and the crawl is quick.
# Override with DEMO_SEED_URL to point the demo at a real business site.
DEMO_SEED_URL = os.environ.get("DEMO_SEED_URL", "https://example.com")


async def seed() -> None:
    init_engine(get_settings())
    try:
        async with admin_scope() as session:
            existing = (
                await session.execute(
                    text("SELECT id FROM tenants WHERE slug = :slug"), {"slug": DEMO_SLUG}
                )
            ).first()
            if existing is not None:
                print(f"demo tenant '{DEMO_SLUG}' already exists; nothing to do")
                return

            tenant_id = uuid4()
            user_id = uuid4()
            agent_id = uuid4()
            plaintext, key_hash, prefix = generate_api_key()

            await session.execute(
                text("INSERT INTO tenants (id, name, slug) VALUES (:id, :name, :slug)"),
                {"id": tenant_id, "name": "Demo Co", "slug": DEMO_SLUG},
            )
            await session.execute(
                text(
                    "INSERT INTO users (id, email, password_hash, name) "
                    "VALUES (:id, :email, :ph, :name)"
                ),
                {
                    "id": user_id,
                    "email": DEMO_EMAIL,
                    "ph": hash_password(DEMO_PASSWORD),
                    "name": "Demo Owner",
                },
            )
            await session.execute(
                text(
                    "INSERT INTO memberships (tenant_id, user_id, role) "
                    "VALUES (:t, :u, 'owner')"
                ),
                {"t": tenant_id, "u": user_id},
            )
            await session.execute(
                text(
                    "INSERT INTO agents (id, tenant_id, name, config, is_default) "
                    "VALUES (:id, :t, 'Assistant', '{}'::jsonb, true)"
                ),
                {"id": agent_id, "t": tenant_id},
            )
            await session.execute(
                text(
                    "INSERT INTO api_keys (tenant_id, name, key_hash, key_prefix) "
                    "VALUES (:t, 'seed key', :h, :p)"
                ),
                {"t": tenant_id, "h": key_hash, "p": prefix},
            )

        await _onboard_growth(tenant_id, agent_id)

        print("seeded demo tenant")
        print(f"  tenant slug : {DEMO_SLUG}")
        print(f"  login       : {DEMO_EMAIL} / {DEMO_PASSWORD}")
        print(f"  api key     : {plaintext}")
        print("  (the api key is shown once and cannot be recovered)")
    finally:
        await dispose_engine()


async def _onboard_growth(tenant_id, agent_id) -> None:
    """Onboard the demo tenant onto the growth platform, through the services.

    The settings row is written by ProfileService.put with the module's own
    ``defaults()`` — the same blocks the PUT /v1/growth/profile path validates
    and materialises — so there is no second hand-rolled copy of the shape. The
    website source goes through SourceService.create (quota, SSRF check, page
    ceiling, wave enqueue all live there) with the review gate on.
    """
    from uuid import UUID

    from app.api.main import _make_local_handler
    from app.core.container import Container
    from app.services.growth.profile import defaults

    container = Container(get_settings())

    try:
        settings_row = await container.growth_profile.put(
            UUID(str(tenant_id)), defaults(), actor="seed"
        )
        # on the inline queue, run the first crawl wave right here so the
        # frontier parks for review before the demo even opens. On qstash the
        # wave is published and will be delivered whenever the API is up.
        from app.adapters.queue.base import LocalQueue

        if isinstance(container.queue, LocalQueue):
            container.queue.set_handler(_make_local_handler(container))

        source = await container.sources.create(
            UUID(str(tenant_id)),
            kind="crawl",
            title="Demo website",
            seed_url=DEMO_SEED_URL,
            config={"review_before_index": True},
            agent_id=UUID(str(agent_id)),
        )

        if isinstance(container.queue, LocalQueue):
            await container.queue.drain(timeout=60)

        print("growth onboarded")
        print(f"  settings blocks : {sorted(settings_row)}")
        print(f"  demo source     : {source['title']} ({source['crawl_state']})")
        print("  (crawl parks for review — approve it from the Sources screen)")
    finally:
        await container.aclose()


if __name__ == "__main__":
    try:
        asyncio.run(seed())
    except Exception as exc:  # pragma: no cover
        print(f"seed failed: {exc}", file=sys.stderr)
        sys.exit(1)
