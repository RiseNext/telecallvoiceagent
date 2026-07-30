"""Schema invariants asserted against the live database.

These are drift guards. Each one encodes a rule that is easy to break silently
six months from now, and two of them exist because the rule *was* broken during
Phase 1 and only a test caught it:

* every `datetime` column is `timestamptz` — SQLAlchemy's default for a bare
  `Mapped[datetime]` is `TIMESTAMP WITHOUT TIME ZONE`, which is naive
* no identifier exceeds 63 characters — Postgres truncates silently, and a
  truncated constraint cannot be dropped by the name a migration expects

The third guard is the one required by the Phase-1 brief: the permission CHECK
in the database is a **frozen snapshot** taken by a migration, and this test
fails if the application catalog has drifted from it. That is what forces a new
migration when someone adds a permission, instead of letting an old migration
silently change meaning.
"""

from __future__ import annotations

import pytest
from sqlalchemy import DateTime, text
from sqlalchemy.ext.asyncio import AsyncEngine

from rn_domain.permissions import ALL_PERMISSIONS
from rn_persistence.base import METADATA
from rn_persistence.models import (
    NULLABLE_TENANT_TABLES,
    PLATFORM_TABLES,
    TENANT_OWNED_TABLES,
)

pytestmark = [pytest.mark.integration]

_POSTGRES_MAX_IDENTIFIER = 63


def test_every_timestamp_column_is_timezone_aware() -> None:
    """`timestamptz`, always, everywhere, no exceptions.

    A naive column accepts a naive value and loses the offset, which for a
    dialler that obeys calling windows is a compliance bug rather than a
    cosmetic one.
    """
    naive = [
        f"{table.name}.{column.name}"
        for table in METADATA.tables.values()
        for column in table.columns
        if isinstance(column.type, DateTime) and not column.type.timezone
    ]
    assert naive == [], f"naive timestamp columns: {naive}"


def test_no_identifier_exceeds_the_postgres_limit() -> None:
    """Postgres truncates identifiers at 63 characters, silently.

    A truncated constraint name cannot be dropped by the name the migration
    thinks it has, which breaks `downgrade` in a way that only shows up when you
    need it most.
    """
    too_long: list[str] = []
    for table in METADATA.tables.values():
        if len(table.name) > _POSTGRES_MAX_IDENTIFIER:
            too_long.append(table.name)
        for item in (*table.constraints, *table.indexes):
            name = getattr(item, "name", None)
            if name and len(str(name)) > _POSTGRES_MAX_IDENTIFIER:
                too_long.append(str(name))
    assert too_long == [], f"identifiers over {_POSTGRES_MAX_IDENTIFIER} chars: {too_long}"


def test_every_table_is_classified_as_tenant_owned_or_not() -> None:
    """A new table must be deliberately classified.

    Forgetting is how a tenant-owned table ends up reachable without a tenant
    predicate, so an unclassified table fails CI rather than defaulting to safe
    or unsafe.
    """
    classified = TENANT_OWNED_TABLES | NULLABLE_TENANT_TABLES | PLATFORM_TABLES
    actual = set(METADATA.tables)
    assert actual - classified == set(), f"unclassified tables: {sorted(actual - classified)}"
    assert classified - actual == set(), f"classified but missing: {sorted(classified - actual)}"


def test_tenant_owned_tables_have_a_non_nullable_tenant_key() -> None:
    problems: list[str] = []
    for name in sorted(TENANT_OWNED_TABLES):
        column = METADATA.tables[name].columns.get("organization_id")
        if column is None:
            problems.append(f"{name}: missing organization_id")
        elif column.nullable:
            problems.append(f"{name}: organization_id is nullable")
    assert problems == []


def test_platform_tables_have_no_tenant_key() -> None:
    """A stray `organization_id` on a platform table invites a bogus filter."""
    problems = [
        name
        for name in sorted(PLATFORM_TABLES)
        if "organization_id" in METADATA.tables[name].columns
    ]
    assert problems == []


async def test_permission_catalog_matches_the_frozen_migration_snapshot(
    engine: AsyncEngine,
) -> None:
    """The database CHECK must still cover the application's permission catalog.

    The CHECK is a literal snapshot written by migration 0001 — deliberately not
    an import of `rn_domain.permissions`, because an old migration whose meaning
    changes with today's code is not a migration.

    So adding a permission requires a new migration replacing the CHECK. This
    test is what makes that mandatory: without it, the new permission would be
    unstorable and every authorization check against it would silently fail.
    """
    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'ck_roles_permissions_catalog'"
            )
        )
        definition = result.scalar_one_or_none()

    assert definition is not None, "the roles permission CHECK is missing from the database"

    missing = sorted(p for p in ALL_PERMISSIONS if f"'{p}'" not in definition)
    assert missing == [], (
        "These permissions exist in rn_domain.permissions but are not allowed by the "
        "database CHECK. Add a migration that replaces ck_roles_permissions_catalog "
        f"with a new frozen snapshot: {missing}"
    )


async def test_agent_version_immutability_trigger_exists(engine: AsyncEngine) -> None:
    """Immutability is enforced by the database, not by convention."""
    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT tgname FROM pg_trigger "
                "WHERE tgname = 'agent_versions_freeze_trigger' AND NOT tgisinternal"
            )
        )
        assert result.scalar_one_or_none() == "agent_versions_freeze_trigger"


async def test_composite_unique_keys_exist_for_composite_foreign_keys(
    engine: AsyncEngine,
) -> None:
    """Every composite FK needs a matching `(organization_id, id)` unique key.

    Without it the FK cannot be created at all — but the point of asserting it is
    that these unique keys look redundant next to the primary key, and are
    exactly the sort of thing someone "cleans up" without realising they are
    what makes cross-tenant parentage impossible.
    """
    expected = {
        "agents",
        "agent_versions",
        "knowledge_bases",
        "contacts",
        "leads",
        "campaigns",
        "campaign_contacts",
        "calls",
    }
    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT tc.table_name FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage kcu "
                "  ON tc.constraint_name = kcu.constraint_name "
                "WHERE tc.constraint_type = 'UNIQUE' AND kcu.column_name = 'organization_id' "
                "GROUP BY tc.table_name"
            )
        )
        found = {row[0] for row in result}

    assert expected <= found, f"missing composite unique keys on: {sorted(expected - found)}"


async def test_no_vector_column_exists_in_phase_1(engine: AsyncEngine) -> None:
    """Open decision **D-8** is unresolved, so the schema carries no vector.

    The embedding width becomes part of the column type and partitioning cannot
    be retrofitted, which makes this the least reversible choice in the system.
    Phase 3 makes it, after a bake-off on real Indic data (ADR-010). This test
    exists because "just add the column while we're in here" is exactly how it
    would get decided by accident.
    """
    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND udt_name IN ('vector', 'halfvec')"
            )
        )
        vector_columns = [f"{row[0]}.{row[1]}" for row in result]
    assert vector_columns == [], f"unexpected vector columns: {vector_columns}"

    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = 'document_chunks'"
            )
        )
        assert result.scalar_one_or_none() is None
