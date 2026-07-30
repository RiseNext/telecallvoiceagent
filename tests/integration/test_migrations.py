"""Migration round-trip against real PostgreSQL.

The Phase-1 definition of done: `alembic upgrade head` then
`alembic downgrade base` succeeds on a clean database, and `upgrade head`
succeeds again afterwards.

Why the round-trip and not just the upgrade: a `downgrade` that does not fully
reverse its `upgrade` is only discovered when you need to roll back, which is the
worst possible moment. The usual failure is a constraint or index whose generated
name differs from the name the downgrade tries to drop — exactly the class of bug
this caught twice while Phase 1 was being written.

These tests are **synchronous**. Alembic runs its own event loop internally, so
an async test calling `command.upgrade` would nest loops; the assertions use a
separate short-lived `asyncio.run` instead. No sync database driver is required.

This test manipulates the shared session database and restores it to `head`
before finishing. Every other test's `engine` fixture truncates first, so
ordering does not matter.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import asyncpg
import pytest
from alembic import command
from alembic.config import Config

pytestmark = [pytest.mark.integration]

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: `alembic_version` is Alembic's own bookkeeping, not part of our schema.
_EXPECTED_TABLE_COUNT = 21


def _alembic_config(url: str) -> Config:
    config = Config(os.path.join(_REPO_ROOT, "alembic.ini"))
    config.set_main_option(
        "script_location",
        os.path.join(_REPO_ROOT, "packages", "persistence", "src", "rn_persistence", "migrations"),
    )
    os.environ["RN_MIGRATION_DATABASE_URL"] = url
    return config


def _query(url: str, sql: str) -> list[Any]:
    """Run one query on a fresh connection, outside any running event loop."""

    async def _run() -> list[Any]:
        connection = await asyncpg.connect(url.replace("postgresql+asyncpg://", "postgresql://"))
        try:
            return list(await connection.fetch(sql))
        finally:
            await connection.close()

    return asyncio.run(_run())


def _table_names(url: str) -> set[str]:
    rows = _query(
        url,
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'",
    )
    return {row["table_name"] for row in rows}


def _counts(url: str) -> dict[str, int]:
    rows = _query(
        url,
        """
        SELECT
          (SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal)              AS triggers,
          (SELECT count(*) FROM pg_constraint WHERE contype = 'c'
             AND conname LIKE 'ck_%')                                          AS checks,
          (SELECT count(*) FROM pg_proc WHERE proname = 'agent_versions_freeze') AS functions
        """,
    )
    return dict(rows[0])


def test_upgrade_downgrade_upgrade_round_trip(_migrated: str) -> None:
    """Empty -> head -> base -> head, asserting the schema at each step."""
    url = _migrated
    config = _alembic_config(url)

    # The session fixture already ran `upgrade head` from an empty database,
    # which is itself the assertion that a fresh production deploy works.
    at_head = _table_names(url)
    assert len(at_head - {"alembic_version"}) == _EXPECTED_TABLE_COUNT

    before = _counts(url)
    assert before["triggers"] >= 1, "the agent_versions immutability trigger is missing"
    assert before["checks"] >= 30, "enum and range CHECK constraints are missing"
    assert before["functions"] == 1

    command.downgrade(config, "base")

    after_downgrade = _table_names(url)
    assert after_downgrade <= {"alembic_version"}, (
        f"downgrade left tables behind: {sorted(after_downgrade - {'alembic_version'})}"
    )
    after = _counts(url)
    assert after["triggers"] == 0, "downgrade left the trigger behind"
    assert after["checks"] == 0, "downgrade left CHECK constraints behind"
    assert after["functions"] == 0, "downgrade left the trigger function behind"

    command.upgrade(config, "head")

    assert _table_names(url) == at_head, "the schema after a round-trip differs from the original"
    assert _counts(url) == before, "objects after a round-trip differ from the original"


def test_models_and_migrations_have_not_diverged(_migrated: str) -> None:
    """The declared models and the applied migration must agree.

    `metadata.create_all()` is never used to build a test database, precisely so
    that the migration — the thing that runs in production — is what gets
    exercised. This catches the case where someone edits a model and forgets to
    generate a revision, which would otherwise pass every other test and fail on
    deploy.
    """
    from rn_persistence.base import METADATA

    live = _table_names(_migrated) - {"alembic_version"}
    declared = set(METADATA.tables)
    assert live == declared, (
        "models and migrations have diverged; run "
        "`uv run alembic revision --autogenerate -m '...'` and review the result. "
        f"Only in database: {sorted(live - declared)}. "
        f"Only in models: {sorted(declared - live)}."
    )
