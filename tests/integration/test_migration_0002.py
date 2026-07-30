"""Migration 0002: the `language_policy` column, its guards, and its round trip.

The specific sequence the Phase-2 brief asks for:

    Phase-1 schema -> upgrade 0002 -> Phase-2 schema -> downgrade 0002
                   -> Phase-1 schema -> upgrade 0002 again -> equivalent schema

Plus the two things that would make the column dangerous rather than merely present:

* **The backfill** must produce a coherent policy for rows written before the column
  existed. A row that survives `upgrade` but violates the new CHECK is a migration
  that cannot be applied to production, and the only way to find out is to try it
  against real rows.
* **The freeze trigger** must cover the new column. A behaviour column the trigger
  does not list would be silently mutable after publication — a weaker guarantee than
  Phase 1 shipped, introduced by adding a feature.

Synchronous, for the same reason as `test_migrations.py`: Alembic runs its own event
loop, so an async test calling `command.upgrade` would nest loops.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any

import asyncpg
import pytest
from alembic import command
from alembic.config import Config

pytestmark = [pytest.mark.integration]

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _alembic_config(url: str) -> Config:
    config = Config(os.path.join(_REPO_ROOT, "alembic.ini"))
    config.set_main_option(
        "script_location",
        os.path.join(_REPO_ROOT, "packages", "persistence", "src", "rn_persistence", "migrations"),
    )
    os.environ["RN_MIGRATION_DATABASE_URL"] = url
    return config


def _run(url: str, sql: str, *args: Any) -> list[Any]:
    async def _go() -> list[Any]:
        connection = await asyncpg.connect(url.replace("postgresql+asyncpg://", "postgresql://"))
        try:
            return list(await connection.fetch(sql, *args))
        finally:
            await connection.close()

    return asyncio.run(_go())


def _execute(url: str, sql: str, *args: Any) -> None:
    async def _go() -> None:
        connection = await asyncpg.connect(url.replace("postgresql+asyncpg://", "postgresql://"))
        try:
            await connection.execute(sql, *args)
        finally:
            await connection.close()

    asyncio.run(_go())


def _columns(url: str, table: str) -> set[str]:
    rows = _run(
        url,
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = $1",
        table,
    )
    return {row["column_name"] for row in rows}


def _check_constraints(url: str, table: str) -> set[str]:
    rows = _run(
        url,
        "SELECT conname FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
        "WHERE c.contype = 'c' AND t.relname = $1",
        table,
    )
    return {row["conname"] for row in rows}


def _freeze_function_body(url: str) -> str:
    rows = _run(url, "SELECT prosrc FROM pg_proc WHERE proname = 'agent_versions_freeze'")
    return str(rows[0]["prosrc"]) if rows else ""


_POLICY_JSON = (
    '{"primary": "hi-IN", "allowed": ["hi-IN", "en"], "follow_caller": true, "code_switch": true}'
)


def _seed_parents(url: str) -> tuple[uuid.UUID, uuid.UUID]:
    """An organization and an agent for a version to hang off."""
    organization_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    _execute(
        url,
        """
        INSERT INTO organizations (id, name, slug, status, timezone, created_at)
        VALUES ($1, 'Migration Co', $2, 'active', 'Asia/Kolkata', now())
        """,
        organization_id,
        f"migration-{organization_id.hex[:8]}",
    )
    _execute(
        url,
        """
        INSERT INTO agents (id, organization_id, name, status, created_at)
        VALUES ($1, $2, $3, 'active', now())
        """,
        agent_id,
        organization_id,
        f"Migration Agent {agent_id.hex[:8]}",
    )
    return organization_id, agent_id


def _seed_version_at_head(url: str, *, published: bool = True) -> uuid.UUID:
    """A version valid under the Phase-2 schema, with a coherent policy.

    Raw SQL rather than the ORM: these tests are about what the *database* refuses, and
    going through a model that always writes a coherent policy would test the model.
    """
    organization_id, agent_id = _seed_parents(url)
    version_id = uuid.uuid4()
    _execute(
        url,
        """
        INSERT INTO agent_versions
            (id, organization_id, agent_id, version_number, instructions, languages,
             language_policy, status, voice_map, turn_policy, guardrail_config,
             published_at, created_at)
        VALUES ($1, $2, $3, 1, 'Seeded for a migration test.',
                ARRAY['hi-IN','en']::text[], $4::jsonb, $5,
                '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, $6, now())
        """,
        version_id,
        organization_id,
        agent_id,
        _POLICY_JSON,
        "published" if published else "draft",
        # A published row must carry `published_at` — a separate Phase-1 CHECK.
        __import__("datetime").datetime.now(tz=__import__("datetime").UTC) if published else None,
    )
    return version_id


def _seed_version_at_0001(url: str) -> uuid.UUID:
    """A version exactly as a Phase-1 writer would have left it.

    Only `languages` is set, because before 0002 there was no other column in which
    anyone *could* have expressed a policy. The caller must have downgraded to `0001`
    first — this row is unstorable under the Phase-2 CHECKs, which is the whole point
    of the backfill.
    """
    organization_id, agent_id = _seed_parents(url)
    version_id = uuid.uuid4()
    _execute(
        url,
        """
        INSERT INTO agent_versions
            (id, organization_id, agent_id, version_number, instructions, languages,
             status, voice_map, turn_policy, guardrail_config, published_at, created_at)
        VALUES ($1, $2, $3, 1, 'Seeded before migration 0002 existed.',
                ARRAY['hi-IN','en']::text[], 'published',
                '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, now(), now())
        """,
        version_id,
        organization_id,
        agent_id,
    )
    return version_id


# ---------------------------------------------------------------------------
# The column and its guards exist at head
# ---------------------------------------------------------------------------


def test_the_column_exists_at_head(_migrated: str) -> None:
    assert "language_policy" in _columns(_migrated, "agent_versions")


def test_both_language_checks_are_installed(_migrated: str) -> None:
    """Guards the guards: a dropped CHECK would make the tests below vacuous."""
    names = _check_constraints(_migrated, "agent_versions")
    assert "ck_agent_versions_language_policy_projects_languages" in names
    assert "ck_agent_versions_language_policy_coherent" in names


def test_the_freeze_trigger_covers_the_new_column(_migrated: str) -> None:
    """Published-version immutability must not weaken because a column was added.

    A behaviour column the trigger does not list is silently mutable after
    publication — a regression introduced by adding a feature, which is the kind
    nobody looks for.
    """
    assert "language_policy" in _freeze_function_body(_migrated)


# ---------------------------------------------------------------------------
# What the CHECKs actually prevent
# ---------------------------------------------------------------------------


def test_a_row_whose_projection_disagrees_cannot_be_stored(_migrated: str) -> None:
    """The anti-dual-truth guard, tested against the database rather than trusted.

    A **draft** row, deliberately. `agent_versions_freeze` is a BEFORE UPDATE trigger,
    so on a published row it raises before Postgres evaluates any CHECK — which would
    make this test pass for the wrong reason and prove nothing about the constraint.
    """
    version_id = _seed_version_at_head(_migrated, published=False)
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        _execute(
            _migrated,
            "UPDATE agent_versions SET languages = ARRAY['te-IN']::text[] WHERE id = $1",
            version_id,
        )


def test_the_default_empty_policy_is_rejected(_migrated: str) -> None:
    """Why the CHECK uses `IS NOT DISTINCT FROM` rather than `=`.

    A CHECK whose expression evaluates to NULL **passes**. With `=`, a policy of
    `'{}'::jsonb` would make `policy->'allowed'` NULL, the comparison NULL, and the
    row would be accepted — which is exactly the shape the column default produces.
    """
    version_id = _seed_version_at_head(_migrated, published=False)
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        _execute(
            _migrated,
            "UPDATE agent_versions SET language_policy = '{}'::jsonb WHERE id = $1",
            version_id,
        )


@pytest.mark.parametrize(
    "policy",
    [
        # `primary` absent.
        '{"allowed": ["hi-IN", "en"], "follow_caller": true, "code_switch": true}',
        # `primary` not among `allowed`.
        '{"primary": "te-IN", "allowed": ["hi-IN", "en"], "follow_caller": true, "code_switch": true}',
        # A flag that is a string rather than a boolean — `"false"` would read as
        # truthy in Python and silently invert the policy.
        '{"primary": "hi-IN", "allowed": ["hi-IN", "en"], "follow_caller": "false", "code_switch": true}',
        # A flag missing entirely.
        '{"primary": "hi-IN", "allowed": ["hi-IN", "en"], "code_switch": true}',
    ],
)
def test_an_incoherent_policy_cannot_be_stored(_migrated: str, policy: str) -> None:
    version_id = _seed_version_at_head(_migrated, published=False)
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        _execute(
            _migrated,
            "UPDATE agent_versions SET language_policy = $2::jsonb WHERE id = $1",
            version_id,
            policy,
        )


def test_a_coherent_policy_change_is_accepted_on_a_draft(_migrated: str) -> None:
    """The CHECKs must not block a legitimate edit. Written together, as one fact."""
    version_id = _seed_version_at_head(_migrated, published=False)
    _execute(
        _migrated,
        """
        UPDATE agent_versions
        SET languages = ARRAY['te-IN','en']::text[],
            language_policy = '{"primary": "te-IN", "allowed": ["te-IN", "en"],'
                              ' "follow_caller": false, "code_switch": true}'::jsonb
        WHERE id = $1
        """,
        version_id,
    )
    rows = _run(
        _migrated,
        "SELECT language_policy ->> 'primary' AS primary FROM agent_versions WHERE id = $1",
        version_id,
    )
    assert rows[0]["primary"] == "te-IN"


def test_a_published_version_cannot_have_its_language_policy_changed(_migrated: str) -> None:
    """The trigger, not the docstring, is what makes immutability true."""
    version_id = _seed_version_at_head(_migrated)
    with pytest.raises(asyncpg.exceptions.RaiseError, match="immutable"):
        _execute(
            _migrated,
            """
            UPDATE agent_versions
            SET languages = ARRAY['en']::text[],
                language_policy = '{"primary": "en", "allowed": ["en"],'
                                  ' "follow_caller": true, "code_switch": true}'::jsonb
            WHERE id = $1
            """,
            version_id,
        )


# ---------------------------------------------------------------------------
# The round trip, and the backfill
# ---------------------------------------------------------------------------


def test_downgrade_upgrade_round_trip_backfills_a_phase_one_row(_migrated: str) -> None:
    """Phase-1 schema -> 0002 -> Phase-1 -> 0002, with a real row in the table.

    The backfill is the part a schema-only round trip would not exercise. A row
    written before the column existed has to come out of `upgrade` satisfying both new
    CHECKs, or the migration cannot be applied to a production database.
    """
    url = _migrated
    config = _alembic_config(url)

    at_head_columns = _columns(url, "agent_versions")
    at_head_checks = _check_constraints(url, "agent_versions")
    at_head_freeze = _freeze_function_body(url)

    # --- down to the Phase-1 schema -------------------------------------
    command.downgrade(config, "0001")
    assert "language_policy" not in _columns(url, "agent_versions")
    assert "ck_agent_versions_language_policy_projects_languages" not in _check_constraints(
        url, "agent_versions"
    )
    # The trigger function must be the 0001 body again, not "0002's minus a line".
    reverted = _freeze_function_body(url)
    assert "language_policy" not in reverted
    assert "instructions" in reverted, "downgrade dropped the freeze trigger entirely"

    # The row is seeded *here*, on the Phase-1 schema, because that is the only place
    # it can exist: a version with no `language_policy` is unstorable once 0002 has
    # been applied. Seeding at head and pretending would test nothing at all.
    version_id = _seed_version_at_0001(url)
    rows = _run(url, "SELECT languages FROM agent_versions WHERE id = $1", version_id)
    assert list(rows[0]["languages"]) == ["hi-IN", "en"]

    # --- and up again, over a genuine pre-0002 row ----------------------
    command.upgrade(config, "head")
    assert _columns(url, "agent_versions") == at_head_columns
    assert _check_constraints(url, "agent_versions") == at_head_checks
    assert _freeze_function_body(url) == at_head_freeze

    backfilled = _run(
        url,
        """
        SELECT language_policy ->> 'primary'            AS primary_tag,
               language_policy -> 'allowed'             AS allowed,
               language_policy -> 'follow_caller'       AS follow_caller,
               language_policy -> 'code_switch'         AS code_switch,
               to_jsonb(languages) = language_policy -> 'allowed' AS projection_matches
        FROM agent_versions WHERE id = $1
        """,
        version_id,
    )[0]
    # `languages[1]` becomes the primary: the array is ordered and its first element
    # is what every Phase-1 writer treated as the main language.
    assert backfilled["primary_tag"] == "hi-IN"
    assert backfilled["allowed"] == '["hi-IN", "en"]'
    assert backfilled["follow_caller"] == "true"
    assert backfilled["code_switch"] == "true"
    assert backfilled["projection_matches"] is True


def test_repeated_upgrade_produces_an_equivalent_schema(_migrated: str) -> None:
    """`upgrade` twice must be the same schema, not merely a successful command."""
    url = _migrated
    config = _alembic_config(url)

    first_columns = _columns(url, "agent_versions")
    first_checks = _check_constraints(url, "agent_versions")

    command.downgrade(config, "0001")
    command.upgrade(config, "head")
    command.downgrade(config, "0001")
    command.upgrade(config, "head")

    assert _columns(url, "agent_versions") == first_columns
    assert _check_constraints(url, "agent_versions") == first_checks
    # And no duplicate CHECK crept in from a second ADD CONSTRAINT.
    assert (
        len(
            [
                name
                for name in _check_constraints(url, "agent_versions")
                if "language_policy" in name
            ]
        )
        == 2
    )
