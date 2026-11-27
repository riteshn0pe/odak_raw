"""Apply migrations/*.sql in filename order.

Each file runs inside its own transaction, and applied filenames are recorded in
a schema_migrations table so a re-run is a no-op. This is deliberately not
Alembic: the migrations are plain SQL with no down-path, and a 30-line runner is
easier to reason about than a migration framework for that.

Run from the repo root:  python -m scripts.migrate
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import dispose_engine, get_session_factory, init_engine

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


async def _applied(session) -> set[str]:
    await session.execute(
        text(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
    )
    rows = (await session.execute(text("SELECT filename FROM schema_migrations"))).scalars().all()
    return set(rows)


async def _exec_script(conn: Any, sql: str) -> None:
    """Run a whole migration file as one script.

    `exec_driver_sql` cannot be used here. The asyncpg driver sends every
    statement through the extended query protocol, which prepares it first, and
    a prepared statement holds exactly one command — a migration file with more
    than one statement fails with `cannot insert multiple commands into a
    prepared statement`. Splitting the file on semicolons is not an option
    either: these migrations contain `DO $$ ... $$` blocks and function bodies
    whose semicolons are part of the body.

    asyncpg's own `Connection.execute` uses the simple query protocol when given
    no parameters, which accepts a multi-statement script. The connection is
    reached through the DBAPI wrapper rather than opened separately, so the
    script runs inside the transaction SQLAlchemy has already begun and the
    ledger row committed with it either applies or rolls back alongside the DDL.
    """
    raw = await conn.get_raw_connection()
    driver_connection = getattr(raw, "driver_connection", None)
    if driver_connection is None:  # non-asyncpg driver: the prepared path is fine
        await conn.exec_driver_sql(sql)
        return
    await driver_connection.execute(sql)


async def migrate() -> None:
    settings = get_settings()
    init_engine(settings)
    factory = get_session_factory()

    files = sorted(p for p in MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        print("no migrations found")
        return

    try:
        # a separate transaction records the ledger, so it exists before the
        # first migration and survives a mid-run failure of a later one
        async with factory() as session, session.begin():
            done = await _applied(session)

        for path in files:
            if path.name in done:
                print(f"skip  {path.name}")
                continue
            sql = path.read_text()
            # each migration is one transaction: it applies whole or not at all,
            # and the ledger row is written in the same transaction as the DDL
            async with factory() as session, session.begin():
                conn = await session.connection()
                await _exec_script(conn, sql)
                await session.execute(
                    text("INSERT INTO schema_migrations (filename) VALUES (:f)"),
                    {"f": path.name},
                )
            print(f"apply {path.name}")
    finally:
        await dispose_engine()


if __name__ == "__main__":
    try:
        asyncio.run(migrate())
    except Exception as exc:  # pragma: no cover - operator feedback
        print(f"migration failed: {exc}", file=sys.stderr)
        sys.exit(1)
