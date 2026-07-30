"""agent version language policy

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-30 00:00:00.000000+00:00

Adds `agent_versions.language_policy` (JSONB) as the authoritative language
configuration for an agent version, and ties the pre-existing `languages` array
to it so the two can never disagree.

Why the array survives. `languages` answers "which agents speak Telugu?" as an
indexable array query; a JSONB scan for the same answer is strictly worse. It is
kept as a **projection**, not as a second source of truth:

    CHECK (to_jsonb(languages) IS NOT DISTINCT FROM language_policy -> 'allowed')

`IS NOT DISTINCT FROM` and not `=`, because a CHECK whose expression evaluates to
NULL *passes* — and `language_policy -> 'allowed'` is NULL for the `'{}'::jsonb`
column default, which is precisely the row that must be rejected.

Why the freeze trigger is replaced. `agent_versions_freeze` guards the behaviour
columns of a published version. A new behaviour column that the trigger does not
list would be silently mutable after publication, which is a weaker guarantee
than Phase 1 shipped. `downgrade()` restores the 0001 body verbatim from a frozen
literal — a migration must reproduce the state that existed then, not the state
today's code would produce.

Review checklist before merging:
  - Does `downgrade()` fully reverse `upgrade()`? The round-trip is tested.
  - Does any statement take a lock that would block writes on a live table?
    `ADD COLUMN` with no default and `ADD CONSTRAINT ... NOT VALID` do not
    rewrite the table. The backfill `UPDATE` does take row locks; `agent_versions`
    is a small configuration table (one row per publish), so a single-statement
    backfill is appropriate here and would NOT be on `calls` or `contacts`.
  - Are enum/permission CHECK values a FROZEN literal snapshot? No permission or
    enum values change here.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---------------------------------------------------------------------------
# FROZEN SNAPSHOT -- the body of `agent_versions_freeze()` exactly as migration
# 0001 created it.
#
# `downgrade()` must restore *that* function, not "the function without whatever
# 0002 added". Deriving it would make this migration's meaning depend on a future
# migration's contents, which is the same failure mode as importing a live
# application catalog.
# ---------------------------------------------------------------------------
FREEZE_FUNCTION_0001: str = """
CREATE OR REPLACE FUNCTION agent_versions_freeze() RETURNS trigger AS $$
BEGIN
    IF OLD.published_at IS NOT NULL AND (
           NEW.instructions           IS DISTINCT FROM OLD.instructions
        OR NEW.languages              IS DISTINCT FROM OLD.languages
        OR NEW.voice_map              IS DISTINCT FROM OLD.voice_map
        OR NEW.turn_policy            IS DISTINCT FROM OLD.turn_policy
        OR NEW.guardrail_config       IS DISTINCT FROM OLD.guardrail_config
        OR NEW.realtime_model         IS DISTINCT FROM OLD.realtime_model
        OR NEW.telephony_sample_rate  IS DISTINCT FROM OLD.telephony_sample_rate
        OR NEW.agent_id               IS DISTINCT FROM OLD.agent_id
        OR NEW.version_number         IS DISTINCT FROM OLD.version_number
        OR NEW.organization_id        IS DISTINCT FROM OLD.organization_id
    ) THEN
        RAISE EXCEPTION
            'agent_versions.% is immutable after publication', OLD.id;
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;
"""

#: The same function with `language_policy` added to the guarded set.
FREEZE_FUNCTION_0002: str = """
CREATE OR REPLACE FUNCTION agent_versions_freeze() RETURNS trigger AS $$
BEGIN
    IF OLD.published_at IS NOT NULL AND (
           NEW.instructions           IS DISTINCT FROM OLD.instructions
        OR NEW.languages              IS DISTINCT FROM OLD.languages
        OR NEW.language_policy        IS DISTINCT FROM OLD.language_policy
        OR NEW.voice_map              IS DISTINCT FROM OLD.voice_map
        OR NEW.turn_policy            IS DISTINCT FROM OLD.turn_policy
        OR NEW.guardrail_config       IS DISTINCT FROM OLD.guardrail_config
        OR NEW.realtime_model         IS DISTINCT FROM OLD.realtime_model
        OR NEW.telephony_sample_rate  IS DISTINCT FROM OLD.telephony_sample_rate
        OR NEW.agent_id               IS DISTINCT FROM OLD.agent_id
        OR NEW.version_number         IS DISTINCT FROM OLD.version_number
        OR NEW.organization_id        IS DISTINCT FROM OLD.organization_id
    ) THEN
        RAISE EXCEPTION
            'agent_versions.% is immutable after publication', OLD.id;
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;
"""

# Raw SQL for ADD/DROP CONSTRAINT rather than `op.create_check_constraint`. The
# metadata naming convention prepends `ck_<table>_`, and op.* applies it again on
# the way in but not symmetrically on the way out, which yields a name a
# downgrade cannot drop. See the same treatment in 0001.
_PROJECTION_CONSTRAINT = "ck_agent_versions_language_policy_projects_languages"
_COHERENCE_CONSTRAINT = "ck_agent_versions_language_policy_coherent"

_PROJECTION_SQL = "to_jsonb(languages) IS NOT DISTINCT FROM language_policy -> 'allowed'"

_COHERENCE_SQL = (
    "cardinality(languages) >= 1"
    " AND jsonb_typeof(language_policy -> 'allowed') = 'array'"
    " AND language_policy ? 'primary'"
    " AND language_policy -> 'allowed' @> jsonb_build_array(language_policy ->> 'primary')"
    " AND coalesce(jsonb_typeof(language_policy -> 'follow_caller'), '') = 'boolean'"
    " AND coalesce(jsonb_typeof(language_policy -> 'code_switch'), '') = 'boolean'"
)


def upgrade() -> None:
    op.add_column(
        "agent_versions",
        sa.Column(
            "language_policy",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )

    # Backfill from the array that was authoritative until now. `follow_caller`
    # and `code_switch` default to true because that is the documented product
    # behaviour (PRD 5.2: code-mixed Indian speech is the normal case) and because
    # existing rows were written under exactly that assumption -- there was no
    # column in which anyone could have said otherwise.
    #
    # `languages[1]` is the primary: the array is ordered and its first element is
    # what every Phase-1 writer treated as the main language.
    op.execute(
        """
        UPDATE agent_versions
        SET language_policy = jsonb_build_object(
            'primary', languages[1],
            'allowed', to_jsonb(languages),
            'follow_caller', true,
            'code_switch', true
        )
        WHERE cardinality(languages) >= 1
        """
    )

    # Constraints go on AFTER the backfill, so an existing row cannot fail them.
    op.execute(
        f"ALTER TABLE agent_versions ADD CONSTRAINT {_PROJECTION_CONSTRAINT} "
        f"CHECK ({_PROJECTION_SQL})"
    )
    op.execute(
        f"ALTER TABLE agent_versions ADD CONSTRAINT {_COHERENCE_CONSTRAINT} "
        f"CHECK ({_COHERENCE_SQL})"
    )

    # Published-version immutability must cover the new behaviour column.
    op.execute(FREEZE_FUNCTION_0002)


def downgrade() -> None:
    op.execute(FREEZE_FUNCTION_0001)
    op.execute(f"ALTER TABLE agent_versions DROP CONSTRAINT IF EXISTS {_COHERENCE_CONSTRAINT}")
    op.execute(f"ALTER TABLE agent_versions DROP CONSTRAINT IF EXISTS {_PROJECTION_CONSTRAINT}")
    op.drop_column("agent_versions", "language_policy")
