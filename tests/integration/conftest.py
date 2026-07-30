"""Integration-test database fixtures.

**Every integration test runs against a real PostgreSQL that this suite owns.**
No SQLite stand-in: the schema uses composite foreign keys, partial indexes,
array containment, JSONB and a plpgsql trigger, none of which SQLite has. A test
that passes on SQLite would prove nothing about the database we actually ship.

Two ways to get that database, in priority order:

1. **`RN_TEST_DATABASE_URL`** — used by CI, which already has a Postgres service
   container. No second database is started.
2. **testcontainers** — used locally. Starts a throwaway container on an
   **ephemeral port** chosen by Docker.

The ephemeral port matters. Developers routinely already have something on 5432
— another project's container, a system install — and a suite that assumes that
port either collides or, far worse, connects to the wrong database and truncates
it. This suite never touches a database it did not create.

Schema is created by running the real Alembic migrations, not
`metadata.create_all()`. Creating tables from the models would test the models
against themselves and leave the migration — the thing that actually runs in
production — unexercised.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from rn_persistence.base import METADATA
from rn_persistence.engine import build_session_factory

# Matches the local compose stack and production, so behaviour that depends on
# the server version is exercised against the version we ship.
_POSTGRES_IMAGE = "pgvector/pgvector:pg17"

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    """An asyncpg DSN for a database this suite exclusively owns."""
    provided = os.environ.get("RN_TEST_DATABASE_URL")
    if provided:
        yield provided
        return

    try:
        # The `testcontainers.community` path is the current one; the old
        # top-level module still works but warns.
        from testcontainers.community.postgres import PostgresContainer
    except ImportError:  # pragma: no cover - depends on the installed version
        try:
            from testcontainers.postgres import PostgresContainer
        except ImportError as exc:
            pytest.skip(f"testcontainers is unavailable and RN_TEST_DATABASE_URL is unset: {exc}")

    with PostgresContainer(_POSTGRES_IMAGE, driver="asyncpg") as container:
        yield container.get_connection_url()


@pytest.fixture(scope="session")
def _migrated(database_url: str) -> Iterator[str]:
    """Apply the real migrations once per session.

    Also proves, on every run, that `upgrade head` works from an empty database —
    which is the only state a fresh production deploy ever starts from.
    """
    config = Config(os.path.join(_REPO_ROOT, "alembic.ini"))
    config.set_main_option(
        "script_location",
        os.path.join(_REPO_ROOT, "packages", "persistence", "src", "rn_persistence", "migrations"),
    )
    previous = os.environ.get("RN_MIGRATION_DATABASE_URL")
    os.environ["RN_MIGRATION_DATABASE_URL"] = database_url
    try:
        command.upgrade(config, "head")
        yield database_url
    finally:
        if previous is None:
            os.environ.pop("RN_MIGRATION_DATABASE_URL", None)
        else:
            os.environ["RN_MIGRATION_DATABASE_URL"] = previous


@pytest_asyncio.fixture
async def engine(_migrated: str) -> AsyncIterator[AsyncEngine]:
    """A fresh engine per test, against the migrated test database.

    Function-scoped deliberately. pytest-asyncio runs each test in its own event
    loop by default, and an asyncpg connection created in one loop cannot be used
    from another — a session-scoped engine produces cross-loop failures that look
    like random flakes. The container and the migrations are still session-scoped,
    so the expensive part is paid once; only the pool is rebuilt.

    Every test begins with an empty database. Truncation rather than a
    rolled-back outer transaction, because the Unit of Work commits for real and
    its tests would prove nothing inside a transaction that never lands.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    created = create_async_engine(
        _migrated,
        # asyncpg's prepared-statement cache is per-connection and interacts
        # badly with a recycled test database; disabling it keeps runs comparable.
        connect_args={"statement_cache_size": 0},
    )
    tables = ", ".join(f'"{name}"' for name in METADATA.tables)
    try:
        async with created.begin() as connection:
            await connection.execute(text(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE"))
        yield created
    finally:
        await created.dispose()


@pytest_asyncio.fixture
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """A session factory bound to the clean test database."""
    return build_session_factory(engine)


@pytest_asyncio.fixture
async def session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """A single session for tests that do not need explicit transactions."""
    async with session_factory() as created:
        yield created
