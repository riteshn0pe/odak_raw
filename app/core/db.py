"""Async database engine and the tenant scoping helper.

The single most important function here is tenant_scope. RLS policies read
app.tenant_id from the postgres session. It must be set with SET LOCAL inside a
transaction, otherwise the value survives on a pooled connection and leaks into
whichever request checks that connection out next.
"""

import contextlib
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import Settings
from app.core.errors import ConfigError
from app.core.logging import get_logger

logger = get_logger(__name__)

TENANT_SETTING = "app.tenant_id"

# One retry when the connection handed out of the pool turns out to be dead.
#
# `DB_POOL_PRE_PING` is off in the deployment this runs in, deliberately: the
# Supabase pooler is a region away and a ping per checkout costs ~0.4s of every
# request. The cost of that choice showed up as a network blip killing every
# pooled connection at once, after which each request opened a transaction on a
# corpse and 500'd on its very first statement
# (`asyncpg.ConnectionDoesNotExistError: connection was closed in the middle of
# operation`, raised by the `set_config` below).
#
# This is SQLAlchemy's own optimistic alternative to pre-ping: let the first
# statement fail, and retry. It costs nothing on a healthy checkout, and one
# retry is enough because a disconnect invalidates the whole pool, so the second
# attempt is guaranteed a fresh connection. Only the setup — open, begin, set the
# tenant — is retried; once the caller's work has started, nothing is replayed.
_DISCONNECT_RETRIES = 1


class Base(DeclarativeBase):
    pass


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def init_engine(settings: Settings) -> AsyncEngine:
    global _engine, _session_factory

    _engine = create_async_engine(
        settings.DATABASE_URL,
        echo=settings.DB_ECHO,
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_pre_ping=settings.DB_POOL_PRE_PING,
        pool_recycle=settings.DB_POOL_RECYCLE_SECONDS,
        # The database sits behind the Supabase pooler (pgbouncer, transaction
        # mode), which swaps the underlying server connection out from under
        # the client between transactions. Prepared statements then break in
        # two ways, so all three knobs below are needed:
        #  * statement_cache_size=0 disables asyncpg's own statement cache;
        #  * prepared_statement_cache_size=0 disables the second cache that
        #    SQLAlchemy's asyncpg dialect keeps on top of it — a cached
        #    prepared statement is bound to whichever server connection it was
        #    prepared on, which pgbouncer no longer guarantees;
        #  * prepared_statement_name_func gives every prepare a unique name.
        #    Without it SQLAlchemy asks asyncpg to auto-name statements, and
        #    asyncpg names them from a per-connection counter, so two pooled
        #    connections both try to PREPARE "__asyncpg_stmt_1__" on the same
        #    reused backend and the second one gets
        #    DuplicatePreparedStatementError.
        connect_args={
            "statement_cache_size": 0,
            "prepared_statement_cache_size": 0,
            "prepared_statement_name_func": lambda: f"__asyncpg_{uuid4()}__",
        },
    )
    _session_factory = async_sessionmaker(
        _engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    return _engine


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


def get_engine() -> AsyncEngine:
    if _engine is None:
        raise ConfigError("database engine not initialised")
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        raise ConfigError("database engine not initialised")
    return _session_factory


async def _discard(stack: AsyncExitStack, exc: BaseException) -> None:
    """Unwind a failed attempt, swallowing whatever the unwind itself raises.

    The rollback runs against the connection that just died, so it failing is the
    expected case and says nothing the original error did not already say.
    """
    with contextlib.suppress(Exception):
        _ = await stack.__aexit__(type(exc), exc, exc.__traceback__)


async def _open_scope(
    stack: AsyncExitStack, tenant_id: str | None
) -> AsyncSession:
    """Open a session and a transaction, and set the tenant if there is one.

    A dead pooled connection is only discovered by the first statement, so the
    statement has to be part of what gets retried — which means the session and
    the transaction do too. Each attempt owns its own exit stack; a failed one is
    unwound immediately and only the successful one is handed to the caller's.
    """
    last: DBAPIError | None = None
    for attempt in range(_DISCONNECT_RETRIES + 1):
        attempt_stack = AsyncExitStack()
        try:
            factory = get_session_factory()
            session = await attempt_stack.enter_async_context(factory())
            await attempt_stack.enter_async_context(session.begin())
            if tenant_id is not None:
                # set_config with is_local=true is the parameterised form of SET
                # LOCAL. SET LOCAL itself does not accept bind parameters
                await session.execute(
                    text("SELECT set_config(:key, :value, true)"),
                    {"key": TENANT_SETTING, "value": tenant_id},
                )
        except DBAPIError as exc:
            # unwound with the exception, not with aclose(): a plain close would
            # try to *commit* the failed attempt, and committing an already-dead
            # connection raises on the way out. Passing the error in makes it a
            # rollback, which is what a failed attempt wants.
            await _discard(attempt_stack, exc)
            if not exc.connection_invalidated or attempt >= _DISCONNECT_RETRIES:
                raise
            last = exc
            logger.warning("db_connection_invalidated_retrying", attempt=attempt + 1)
            continue
        except BaseException as exc:
            await _discard(attempt_stack, exc)
            raise

        _ = await stack.enter_async_context(attempt_stack)
        if last is not None:
            logger.info("db_connection_recovered")
        return session

    # unreachable: the loop either returns or raises
    raise ConfigError("database scope could not be opened")


@asynccontextmanager
async def tenant_scope(tenant_id: UUID | str) -> AsyncIterator[AsyncSession]:
    """Open a transaction with the tenant context set for RLS.

    Every read or write of tenant owned data goes through here. The SET LOCAL is
    scoped to this transaction and is discarded when it ends, so the pooled
    connection carries no tenant context back to the pool.
    """
    async with AsyncExitStack() as stack:
        yield await _open_scope(stack, str(tenant_id))


@asynccontextmanager
async def admin_scope() -> AsyncIterator[AsyncSession]:
    """Transaction with no tenant context, for platform level tables only.

    Used for tenant lookup during request resolution, before a tenant is known.
    Never use for tenant owned data.
    """
    async with AsyncExitStack() as stack:
        yield await _open_scope(stack, None)


async def healthcheck() -> dict[str, Any]:
    engine = get_engine()
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))
    return {"database": "ok"}
