"""Alembic environment.

Two things are deliberate here:

**Migrations use the DIRECT connection.** A transaction-mode pooler cannot hold
the session state DDL needs, and running migrations through one produces
failures that look random. The URL resolution order is
`RN_MIGRATION_DATABASE_URL` (used by tests against an ephemeral database), then
`DATABASE_URL_DIRECT` from settings. There is no URL in `alembic.ini`.

**Importing `rn_persistence.models` is what makes autogenerate work.** A model
module that is not imported there is invisible to Alembic, so the migration it
needs would silently never be generated.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

# Registers every model on Base.metadata. Import for the side effect.
import rn_persistence.models  # noqa: F401
from rn_persistence.base import METADATA

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = METADATA


def _database_url() -> str:
    """Resolve the migration DSN.

    Falls back to settings only when no explicit override is given, so a test
    against a throwaway database never depends on a developer's `.env`.
    """
    override = os.environ.get("RN_MIGRATION_DATABASE_URL")
    if override:
        return override

    # Imported lazily and only when no override is present: loading settings
    # validates the whole application configuration, and a migration run against
    # an explicitly-supplied throwaway database should not need a valid
    # application config to exist at all.
    from rn_core.settings import get_settings  # noqa: PLC0415

    return get_settings().database.url_direct.get_secret_value()


def _run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # Catch a column whose type drifted from the model. Without this a
        # String(64) silently stays String(32) forever.
        compare_type=True,
        # Server defaults are noisy to compare and we set few of them, but the
        # ones we do set (now(), gen_random_uuid()) are load-bearing.
        compare_server_default=True,
        # One transaction for the whole upgrade: a failed migration leaves the
        # schema exactly as it was, rather than half-applied.
        transaction_per_migration=False,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    """Emit SQL without a database connection, for review before production."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _database_url()
    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        # No pooling for a one-shot administrative connection.
        poolclass=pool.NullPool,
    )
    try:
        async with connectable.connect() as connection:
            await connection.run_sync(_run_migrations)
    finally:
        await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
