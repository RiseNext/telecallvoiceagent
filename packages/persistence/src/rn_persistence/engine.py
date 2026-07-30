"""Engine and session construction.

**Engines are process-wide and created once.** An engine owns a connection pool;
building one per request means building a pool per request, which exhausts the
database's connection slots long before it exhausts anything else. The factories
here are cached, and the cache key includes the DSN so that the pooled and direct
engines stay distinct.

**Two engines, deliberately** (see `rn_core.settings.DatabaseSettings`):

* `get_engine()` — the pooled DSN, for all ordinary application traffic. Under
  transaction-mode pooling it cannot hold session state, so anything needing
  `SET` must use `SET LOCAL` inside an explicit transaction.
* `get_direct_engine()` — bypasses the pooler. Migrations, index builds and
  (later) the scheduler's advisory-lock leader lease. `NullPool`, because these
  are short-lived administrative connections and pooling them is pointless.

Everything is async. There is no synchronous database path in this codebase: a
blocking driver call inside an async request handler stalls the event loop, and
in the media plane that is audible.
"""

from __future__ import annotations

import functools
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from rn_core.errors import ConfigurationError
from rn_core.settings import DatabaseSettings, get_settings

__all__ = [
    "build_engine",
    "build_session_factory",
    "dispose_engines",
    "get_direct_engine",
    "get_engine",
    "get_session_factory",
    "session_scope",
]


def build_engine(settings: DatabaseSettings, *, direct: bool = False) -> AsyncEngine:
    """Create an engine. Prefer the cached `get_engine` / `get_direct_engine`.

    Exposed uncached so tests can build a throwaway engine against an ephemeral
    database without disturbing process-wide state.
    """
    dsn = (settings.url_direct if direct else settings.url).get_secret_value()
    if not dsn.startswith("postgresql+asyncpg://"):
        raise ConfigurationError(
            "The database DSN must use the postgresql+asyncpg driver.",
            # The DSN carries a password, so only its scheme is reported.
            detail={"scheme": dsn.split("://", 1)[0] if "://" in dsn else "unknown"},
        )

    if direct:
        return create_async_engine(
            dsn,
            poolclass=NullPool,
            echo=settings.echo_sql,
            connect_args={"server_settings": {"application_name": "rn-direct"}},
        )

    return create_async_engine(
        dsn,
        pool_size=settings.pool_size,
        max_overflow=settings.max_overflow,
        pool_timeout=settings.pool_timeout_seconds,
        # Recycle below typical pooler/proxy idle timeouts so we never hand out a
        # connection the far end has already closed.
        pool_recycle=settings.pool_recycle_seconds,
        # Cheap liveness check on checkout. Without it, the first query after an
        # idle period fails instead of transparently reconnecting.
        pool_pre_ping=True,
        echo=settings.echo_sql,
        connect_args={
            "server_settings": {
                "application_name": "rn-app",
                # A bounded statement timeout is the difference between one slow
                # query and a pool full of slow queries.
                "statement_timeout": str(settings.statement_timeout_ms),
            },
            # asyncpg caches prepared statements per connection, which breaks
            # against a transaction-mode pooler that may hand the next statement
            # to a different backend.
            "statement_cache_size": 0,
        },
    )


@functools.lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    """The process-wide pooled engine."""
    return build_engine(get_settings().database)


@functools.lru_cache(maxsize=1)
def get_direct_engine() -> AsyncEngine:
    """The process-wide direct (unpooled) engine."""
    return build_engine(get_settings().database, direct=True)


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build a session factory.

    `expire_on_commit=False` because the Unit of Work commits and then the caller
    frequently reads the entity it just wrote. With expiry on, that read triggers
    a lazy refresh against a closed transaction — an error at best, and an extra
    round trip at worst.

    `autoflush=False` so that flush points are explicit. Implicit flushes make it
    genuinely hard to reason about when a constraint violation will surface.
    """
    return async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        autoflush=False,
        class_=AsyncSession,
    )


@functools.lru_cache(maxsize=1)
def get_session_factory() -> async_sessionmaker[AsyncSession]:
    return build_session_factory(get_engine())


@asynccontextmanager
async def session_scope(
    factory: async_sessionmaker[AsyncSession] | None = None,
) -> AsyncIterator[AsyncSession]:
    """A session with guaranteed cleanup and no implicit commit.

    Deliberately does **not** commit. Transaction boundaries belong to the Unit
    of Work, which is the only thing that should decide that a set of changes is
    complete. A context manager that commits on exit turns every early return
    into an accidental commit.
    """
    session_factory = factory or get_session_factory()
    session = session_factory()
    try:
        yield session
    except SQLAlchemyError:
        await session.rollback()
        raise
    finally:
        await session.close()


async def dispose_engines() -> None:
    """Close pools on shutdown. Idempotent.

    Called from an application's graceful-shutdown path. Skipping it leaves
    connections held until the database times them out, which during a rolling
    deploy can briefly double the connection count.
    """
    for factory in (get_engine, get_direct_engine):
        if factory.cache_info().currsize:
            await factory().dispose()
        factory.cache_clear()
    get_session_factory.cache_clear()
